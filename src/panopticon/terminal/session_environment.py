"""Explicit environment boundary for integrated tmux children.

A tmux server keeps the environment from the process that first created it. Every new Panopticon
session therefore clears the complete set of launch controls before applying the foreground
invocation's selected, non-secret values.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit

# These values affect where the integrated service/runner execute, store data, or find tools.
# Keep the list explicit so absence can be propagated through an old tmux server. Secret-bearing
# variables are in this list only to clear them; they are never rendered as NAME=value arguments.
SESSION_ENVIRONMENT = (
    "HOME",
    "PATH",
    "PYTHONPATH",
    "TMPDIR",
    "TMUX_TMPDIR",
    "VIRTUAL_ENV",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
    "DOCKER_API_VERSION",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_DEFAULT_PLATFORM",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "PANOPTICON_BASE_IMAGE",
    "PANOPTICON_BROWSER_ORIGINS",
    "PANOPTICON_BASE_FINGERPRINT",
    "PANOPTICON_CACHE",
    "PANOPTICON_CONFIG",
    "PANOPTICON_CONTAINER_SERVICE_URL",
    "PANOPTICON_CONTAINER_ID",
    "PANOPTICON_CREDENTIALS",
    "PANOPTICON_DATA",
    "PANOPTICON_DB",
    "PANOPTICON_DOCKER_IN_DOCKER",
    "PANOPTICON_ENV_FILE",
    "PANOPTICON_GIT_URL",
    "PANOPTICON_HARNESS",
    "PANOPTICON_HOST",
    "PANOPTICON_2119_HONESTY_REVIEWER",
    "PANOPTICON_2119_REVIEWER_1",
    "PANOPTICON_2119_REVIEWER_2",
    "PANOPTICON_INSTANCE_ID",
    "PANOPTICON_INITIAL_PROMPT",
    "PANOPTICON_NO_STAGE_ENTRY_WAKE",
    "PANOPTICON_OPERATOR_TOKEN_FILE",
    "PANOPTICON_PGID",
    "PANOPTICON_PI_API_KEY_ENV_VARS",
    "PANOPTICON_PORT",
    "PANOPTICON_PROPOSED_SLUG",
    "PANOPTICON_PUID",
    "PANOPTICON_PYTHON",
    "PANOPTICON_RECONNECT_BACKOFF",
    "PANOPTICON_REPO_NAME",
    "PANOPTICON_RUNNER_HOST",
    "PANOPTICON_RUNNER_ID",
    "PANOPTICON_RUNTIME_ID",
    "PANOPTICON_SECRETS_DIR",
    "PANOPTICON_SERVICE_AUTH_FILE",
    "PANOPTICON_SERVICE_AUTH_MODE",
    "PANOPTICON_SERVICE_URL",
    "PANOPTICON_STATE",
    "PANOPTICON_STAGE_ENTRY_WAKE_TIMEOUT",
    "PANOPTICON_STARTING_MODEL",
    "PANOPTICON_TASK_ID",
    "PANOPTICON_TASK_TURN",
    "PANOPTICON_VERSION",
    "PANOPTICON_WHEEL",
    "PANOPTICON_WORKSPACE",
    "PANOPTICON_WORKFLOWS_PATH",
    # Never pass ambient credentials into a background service/runner command line.
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CODEX_ACCESS_TOKEN",
    "CODEX_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "PANOPTICON_OPERATOR_TOKEN",
    "PANOPTICON_SERVICE_AUTH_TOKEN",
)

_SECRET_ENVIRONMENT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "CODEX_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "PANOPTICON_OPERATOR_TOKEN",
        "PANOPTICON_SERVICE_AUTH_TOKEN",
    }
)

_CLEAR_ONLY_ENVIRONMENT = _SECRET_ENVIRONMENT | {
    "PANOPTICON_BASE_FINGERPRINT",
    "PANOPTICON_CONTAINER_ID",
    "PANOPTICON_CREDENTIALS",
    "PANOPTICON_DOCKER_IN_DOCKER",
    "PANOPTICON_ENV_FILE",
    "PANOPTICON_GIT_URL",
    "PANOPTICON_HARNESS",
    "PANOPTICON_INITIAL_PROMPT",
    "PANOPTICON_PGID",
    "PANOPTICON_PI_API_KEY_ENV_VARS",
    "PANOPTICON_PROPOSED_SLUG",
    "PANOPTICON_PUID",
    "PANOPTICON_PYTHON",
    "PANOPTICON_RECONNECT_BACKOFF",
    "PANOPTICON_REPO_NAME",
    "PANOPTICON_SECRETS_DIR",
    "PANOPTICON_SERVICE_AUTH_TOKEN",
    "PANOPTICON_STARTING_MODEL",
    "PANOPTICON_TASK_ID",
    "PANOPTICON_TASK_TURN",
    "PANOPTICON_VERSION",
    "PANOPTICON_WHEEL",
    "PANOPTICON_WORKSPACE",
}

_URL_ENVIRONMENT = frozenset(
    {
        "DOCKER_HOST",
        "PANOPTICON_CONTAINER_SERVICE_URL",
        "PANOPTICON_DB",
        "PANOPTICON_SERVICE_URL",
    }
)


def _reject_embedded_credentials(name: str, value: str) -> None:
    """Reject URL credentials before a controlled value is rendered into process argv."""

    try:
        parsed = urlsplit(value)
        password = parsed.password
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid URL") from exc
    sensitive_query = any(
        marker in key.lower().replace("-", "").replace("_", "")
        for key, _value in query
        for marker in ("password", "passwd", "token", "secret", "apikey", "credential")
    )
    if password is not None or sensitive_query:
        raise ValueError(
            f"{name} embeds credential material that cannot be placed on a background command "
            "line; use a private credential-file reference"
        )


def selected_session_environment(
    environ: Mapping[str, str] = os.environ,
) -> dict[str, str]:
    """Return current non-secret controlled values suitable for a child command line."""

    selected = {
        name: environ[name]
        for name in SESSION_ENVIRONMENT
        if name not in _CLEAR_ONLY_ENVIRONMENT and name in environ
    }
    for name in _URL_ENVIRONMENT & selected.keys():
        _reject_embedded_credentials(name, selected[name])
    return selected


def session_environment_argv(
    command: Sequence[str], *, environment: Mapping[str, str] | None = None
) -> list[str]:
    """Run ``command`` with selected controls pinned and stale tmux values cleared.

    ``environment`` is already resolved by the caller. Values outside the controlled allowlist
    are ignored, and secret-bearing names are always cleared rather than copied into argv.
    """

    selected = selected_session_environment(environment if environment is not None else os.environ)
    arguments = ["env"]
    for name in SESSION_ENVIRONMENT:
        arguments.extend(["-u", name])
    arguments.extend(f"{name}={selected[name]}" for name in SESSION_ENVIRONMENT if name in selected)
    return [*arguments, *command]


def session_environment_command(
    command: str, *, environment: Mapping[str, str] | None = None
) -> str:
    """Shell form of :func:`session_environment_argv` for tmux pane commands."""

    return shlex.join(session_environment_argv(["/bin/sh", "-c", command], environment=environment))
