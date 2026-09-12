"""Foreground setup shared by the CLI and repository screen."""

from __future__ import annotations

import getpass
import uuid
from collections.abc import Callable
from typing import Any

from panopticon.client import TaskServiceClient
from panopticon.terminal.setup_credentials import (
    AUTH_KEYS,
    Connection,
    configured,
    connect,
    connection_values,
    env_values,
    merge_env,
    read_private,
    setup_lock,
    write_private,
)


def choose_agent(*, input_fn: Callable[[str], str] = input) -> str:
    names = ("claude", "codex", "pi")
    print("Choose the coding agent to connect: 1) Claude  2) Codex  3) Pi")
    while True:
        answer = input_fn("Agent [1]: ").strip().lower() or "1"
        if answer in {"1", "2", "3"}:
            return names[int(answer) - 1]
        if answer in names:
            return answer
        print("Choose 1, 2, or 3.")


def configure_connection(
    *, input_fn: Callable[[str], str] = input, secret_fn: Callable[[str], str] = getpass.getpass
) -> Connection:
    with setup_lock():
        return connect(choose_agent(input_fn=input_fn), input_fn=input_fn, secret_fn=secret_fn)


def _repo_connection(repo: dict[str, Any]) -> Connection | None:
    reference = repo.get("env_file")
    directory = repo.get("credential_dir")
    if not reference and not directory:
        return None
    return Connection(str(repo.get("default_harness") or "claude"), str(reference or ""), directory)


def repo_configured(repo: dict[str, Any]) -> bool:
    """Inspect only repo transport, never the operator's native login or process environment."""
    from panopticon.sessionservice.auth_readiness import missing_repo_auth

    return missing_repo_auth(repo, str(repo.get("default_harness") or "claude")) is None


def _legacy_running(client: TaskServiceClient, repo_id: str) -> bool:
    return any(
        task.get("repo_id") == repo_id
        and task.get("workflow") == "setup-repo"
        and task.get("state") not in {"COMPLETE", "DROPPED"}
        and task.get("container_status") in {"live", "starting", "awaiting"}
        for task in client.list_tasks()
    )


def require_local_runtime(client: TaskServiceClient) -> None:
    """Repository repair writes this host's secrets; verify its intended service and runner."""
    from panopticon.terminal.runtime import resolve_runtime, wait_until_ready

    wait_until_ready(resolve_runtime(client.service_url), timeout=2.0)


def configure_repo(
    client: TaskServiceClient,
    repo_id: str,
    *,
    connection: Connection | None = None,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> bool:
    """Configure one repo using new files, under durable service-side admission holds.

    Returns False if the operator keeps an incomplete explicit binding. No secret value crosses
    the task-service API; only the new runner-local file/directory references do.
    """
    from panopticon.terminal.quickstart import choose_enabled_workflows

    require_local_runtime(client)
    if _legacy_running(client, repo_id):
        raise RuntimeError(
            "An older authentication session is running for this project. Finish or exit it before foreground setup."
        )
    repo = client.get_repo(repo_id)
    print(f"Set up {repo.get('name') or repo_id} on this host.")
    existing = _repo_connection(repo)
    replace = existing is None
    if existing:
        status = "configured" if repo_configured(repo) else "incomplete"
        print(f"This repository has an explicit {existing.harness} connection ({status}).")
        replace = (
            input_fn("Replace this repository's agent connection? [y/N] ").strip().lower() == "y"
        )
        if not replace:
            if not repo_configured(repo):
                print(
                    "The existing connection is incomplete. Select replacement in setup to repair it."
                )
                return False
            connection = None
    with setup_lock():
        client.begin_repo_setup(repo_id)
        # Keep admission closed on any failure. Reopening setup resumes repair; finishing clears
        # the repo gate only, leaving the preserved tasks for an explicit individual Retry.
        if connection is None and replace:
            connection = connect(
                choose_agent(input_fn=input_fn), input_fn=input_fn, secret_fn=secret_fn
            )
        try:
            old_content = read_private(str(repo["env_file"])) if repo.get("env_file") else ""
        except FileNotFoundError:
            if not replace:
                raise
            old_content = ""
        values = env_values(old_content)
        changes: dict[str, Any] = {}
        if connection is not None:
            if not configured(connection):
                raise ValueError("Selected connection is incomplete; run `panopticon setup` again.")
            # Remove only the selected harness's superseded credentials. Other harness, gateway,
            # toolchain and repository-specific values remain byte-for-byte unless edited below.
            retained = "\n".join(
                line
                for line in old_content.splitlines()
                if line.split("=", 1)[0].strip() not in AUTH_KEYS[connection.harness]
            )
            old_content = merge_env(retained, connection_values(connection))
            changes.update(
                default_harness=connection.harness, credential_dir=connection.credential_dir
            )
            if connection.harness != (repo.get("default_harness") or "claude"):
                changes["default_model"] = None
        github = "github-self-reviewed" in choose_enabled_workflows(str(repo["git_url"]))
        if github and not values.get("GH_TOKEN", "").strip():
            print(
                "This GitHub workflow needs a token scoped to this repository. It is not saved in the reusable agent connection."
            )
            token = secret_fn(
                "GitHub token (input hidden; Enter to leave setup incomplete): "
            ).strip()
            if not token:
                print("GitHub setup remains incomplete; run setup for this repository to resume.")
                return False
            old_content = merge_env(old_content, {"GH_TOKEN": token})
        reference = f"repo-{uuid.uuid4().hex}.env"
        write_private(reference, old_content)
        changes["env_file"] = reference
        client.update_repo(repo_id, **changes)
        client.finish_repo_setup(repo_id)
    print(
        "Credentials configured on this host. Pending tasks remain paused; retry the task you want to start."
    )
    return True
