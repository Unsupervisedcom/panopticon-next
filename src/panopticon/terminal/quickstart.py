"""Repository registration and workflow selection for foreground quickstart."""

from __future__ import annotations

import httpx

from panopticon.client import TaskServiceClient
from panopticon.terminal.source_selection import (
    RepositorySource as RepositorySource,
)
from panopticon.terminal.source_selection import (
    detect_git_url as detect_git_url,
)
from panopticon.terminal.source_selection import (
    is_github_source as is_github_source,
)
from panopticon.terminal.source_selection import (
    repo_id_candidates as repo_id_candidates,
)
from panopticon.terminal.source_selection import (
    repo_id_from_url as repo_id_from_url,
)
from panopticon.terminal.source_selection import (
    resolve_source as resolve_source,
)
from panopticon.terminal.source_selection import (
    select_source as select_source,
)
from panopticon.terminal.source_selection import (
    source_name as source_name,
)
from panopticon.terminal.source_selection import (
    sources_equivalent as sources_equivalent,
)

#: The opt-in coding workflows quickstart enables for a repo (kept in sync with the workflow
#: classes' ``name`` ClassVars): the forge lifecycle for supported GitHub sources, the forge-free
#: one for every other source.
_FORGE_WORKFLOWS = ("github-self-reviewed", "github-peer-reviewed")
_LOCAL_WORKFLOWS = ("local-git-self-reviewed",)


def choose_enabled_workflows(git_url: str) -> tuple[str, ...]:
    """The opt-in workflows quickstart enables for a repo, chosen from its remote URL.

    Supported github.com transports get both GitHub lifecycles. All other sources get the
    forge-free flow.
    """
    return _FORGE_WORKFLOWS if is_github_source(git_url) else _LOCAL_WORKFLOWS


def _ensure_workflows_enabled(
    client: TaskServiceClient, repo: dict[str, object], workflows: tuple[str, ...]
) -> None:
    """Add ``workflows`` to an existing repo's enabled set without removing operator choices.

    Merges rather than replaces, so a re-run (or a repo registered before quickstart enabled a
    workflow) gets the coding lifecycle without clobbering entries the operator set by hand. A
    no-op when it's already enabled.
    """
    raw = repo.get("enabled_workflows")
    enabled = [str(w) for w in raw] if isinstance(raw, list) else []
    merged = [*enabled, *(workflow for workflow in workflows if workflow not in enabled)]
    if merged == enabled:
        return
    repo_id = str(repo["id"])
    client.update_repo(repo_id, enabled_workflows=merged)
    print(f"  → Enabled workflows for repo {repo_id!r}: {', '.join(workflows)}.")


def _find_existing_repo(client: TaskServiceClient, git_url: str) -> dict[str, object] | None:
    """Return only an already-registered repository with an equivalent source."""
    for repo in client.list_repos():
        if sources_equivalent(str(repo.get("git_url", "")), git_url):
            return repo
    return None


def setup_repo(
    client: TaskServiceClient,
    git_url: str,
    env_file: str | None,
    *,
    default_harness: str | None = None,
) -> tuple[str, str]:
    """Register the repo quickstart is run in with the task service; return its ``(id, name)``.

    Enables the opt-in coding workflows appropriate to the repo's source — both GitHub lifecycles
    for supported github.com transports, or the forge-free ``local-git-self-reviewed`` workflow for
    every other source (see :func:`choose_enabled_workflows`).

    Idempotent: an already-registered repo is reused only when its source is equivalent. Existing
    credential and harness bindings are left untouched. A conflict refreshes repository state and
    either finds an equivalent racing create or retries with a wider source-identity suffix.
    """
    workflows = choose_enabled_workflows(git_url)
    existing = _find_existing_repo(client, git_url)
    if existing is not None:
        print(f"Repo already configured for {git_url!r} — skipping registration.")
        _ensure_workflows_enabled(client, existing, workflows)
        repo_id = str(existing["id"])
        return repo_id, str(existing.get("name") or repo_id)
    name = source_name(git_url)
    for repo_id in repo_id_candidates(git_url):
        try:
            client.create_repo(
                repo_id,
                name,
                git_url,
                env_file=env_file,
                enabled_workflows=list(workflows),
                default_harness=default_harness,
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code != 409:
                raise
            raced = _find_existing_repo(client, git_url)
            if raced is None:
                continue
            raced_id = str(raced["id"])
            print(f"Repo already configured for {git_url!r} — reusing {raced_id!r}.")
            _ensure_workflows_enabled(client, raced, workflows)
            return raced_id, str(raced.get("name") or raced_id)
        print(f"Registered repo {repo_id!r} (git_url={git_url!r}).")
        if env_file is not None:
            print(f"  → Secrets file: {env_file}")
        print(f"  → Enabled workflows: {', '.join(workflows)}.")
        return repo_id, name
    raise RuntimeError(f"could not allocate a repository id for source {git_url!r}")
