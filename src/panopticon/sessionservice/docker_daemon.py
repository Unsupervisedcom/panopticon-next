"""Docker daemon reachability (REQ-031): the fail-loud preflight for `panopticon start`/`host`
and the per-host session-service daemon, plus the per-tick spawn-loop guard that distinguishes an
unreachable daemon (environmental, retried automatically once it returns) from a task-specific
crash — see `panopticon.sessionservice.spawner.Spawner.spawn_one`/`heal`.

Behind an injectable command-runner (the same pattern as `core.git`/`local_runner`'s
`CommandRunner`), so it's unit-testable without a real Docker daemon. LLM-free.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

#: Run a command and return its exit status (mirrors `panopticon.terminal.doctor.Run`).
Run = Callable[[Sequence[str]], int]


def _subprocess_status(command: Sequence[str]) -> int:
    """Default :data:`Run`: run ``command`` quietly and return its exit status."""
    try:
        return subprocess.run(list(command), capture_output=True).returncode
    except OSError:
        return 127


def daemon_reachable(run: Run = _subprocess_status) -> bool:
    """Whether the Docker daemon answers ``docker info``."""
    return run(["docker", "info"]) == 0


#: Shared by startup and doctor. A failed probe cannot distinguish a stopped engine from
#: denied socket access; give conditional remedies without pretending to diagnose either.
FIX_HINT = (
    "Run `docker info` to see the error; `docker context show` identifies the selected engine.\n"
    "macOS: start OrbStack or Docker Desktop. Linux system Docker: if stopped, run "
    "`sudo systemctl start docker`.\n"
    "For Linux system Docker socket permission denied, run "
    '`sudo usermod --append --groups docker "$USER"` from your own account, '
    "or ask an administrator to substitute your username. "
    "The docker group grants root-equivalent access. Log out completely and log back in "
    "(reconnect SSH) for the new group to take effect.\n"
    "Existing tmux servers and background services keep their old groups; restart affected "
    "processes after saving any running work.\n"
    "For a rootless or remote engine, check that engine and its Docker context instead. "
    "Confirm `docker info` works without sudo before retrying Panopticon."
)


def preflight_message(command: str, *, run: Run = _subprocess_status) -> str | None:
    """``None`` when the Docker daemon is reachable (clear to proceed); otherwise a
    human-readable, actionable refusal message for ``panopticon {command}`` naming the fix —
    the caller should refuse to start rather than spawn into failure (REQ-031.1/REQ-031.2)."""
    if daemon_reachable(run):
        return None
    return (
        f"Docker daemon unreachable for this user.\n{FIX_HINT}\nThen rerun `panopticon {command}`."
    )
