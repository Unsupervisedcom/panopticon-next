"""Presence checks for the credentials a repository actually transports to a new task.

These checks do not authenticate with a provider. An empty temporary home prevents a host CLI's
personal login from making an unconfigured task appear ready. Resumed tasks retain their existing
in-container checks because their persistent config volume can carry credentials of its own.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from panopticon.client import JsonObj
from panopticon.core.dirs import secrets_file_path
from panopticon.harnesses import get_harness


def configured_environment(env_file: str | None) -> dict[str, str]:
    """Read Docker env-file literals, without shell evaluation or ambient-variable fallback."""
    path = secrets_file_path(env_file)
    if path is None:
        return {}
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, UnicodeError):
        raise ValueError(
            "The configured repository environment file is unreadable; open setup."
        ) from None
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name] = value
    # The runner owns this mount location; env-file text cannot redirect a host filesystem check.
    values.pop("PANOPTICON_CREDENTIALS", None)
    return values


def missing_task_auth(task: JsonObj, repo: JsonObj) -> str | None:
    """Return an actionable missing-transport reason, or None when presence is sufficient.

    Provisioned tasks may have an authenticated persistent config volume. Leave those to the existing
    in-container check rather than rejecting a supported resume from host-only evidence.
    """
    if task.get("clone"):
        return None
    harness = get_harness(task.get("harness"))
    try:
        environ = configured_environment(repo.get("env_file"))
        credentials = secrets_file_path(repo.get("credential_dir"))
        if credentials:
            environ["PANOPTICON_CREDENTIALS"] = credentials
        if task.get("starting_model"):
            environ["PANOPTICON_STARTING_MODEL"] = task["starting_model"]
        if harness.name == "claude":
            # Claude's existing missing_auth includes a live HTTP probe, which does not belong
            # in runner readiness. Gate only absent transport; gateways own their auth semantics.
            configured = any(
                environ.get(name)
                for name in (
                    "ANTHROPIC_API_KEY",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                    "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY",
                )
            )
            missing = not configured
        else:
            with tempfile.TemporaryDirectory(prefix="panopticon-auth-check-") as empty_home:
                missing = harness.missing_auth(environ, home=Path(empty_home)) is not None
    except (OSError, ValueError):
        return f"Cannot read configured {harness.name} credentials. Open foreground setup, then retry this task."
    if missing:
        return f"Connect {harness.name} in foreground setup, then retry this task; no task credentials are configured."
    return None


def missing_repo_auth(repo: JsonObj, harness: str) -> str | None:
    """Foreground setup's view of a newly launched task using this repository's transport."""
    return missing_task_auth(
        {"harness": harness, "starting_model": repo.get("default_model")}, repo
    )
