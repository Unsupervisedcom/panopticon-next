"""Docker daemon reachability (REQ-031): the injectable-command-runner check both the operator
CLI's startup preflight and the spawn loop's daemon-down deferral are built on. No real Docker —
the command runner is a fake exit-status function."""

from __future__ import annotations

import pytest

from panopticon.sessionservice.docker_daemon import daemon_reachable, preflight_message


def _run_ok(command: object) -> int:
    return 0


def _run_fail(command: object) -> int:
    return 1


def test_daemon_reachable_true_when_docker_info_succeeds() -> None:
    # 2119: REQ-031.1.5
    assert daemon_reachable(run=_run_ok) is True


def test_daemon_reachable_false_when_docker_info_fails() -> None:
    # 2119: REQ-031.1.1
    assert daemon_reachable(run=_run_fail) is False


def test_daemon_reachable_probes_docker_info() -> None:
    seen: list[object] = []

    def _run(command: object) -> int:
        seen.append(command)
        return 0

    daemon_reachable(run=_run)
    assert seen == [["docker", "info"]]


def test_preflight_message_is_none_when_reachable() -> None:
    assert preflight_message("start", run=_run_ok) is None


@pytest.mark.parametrize("command", ["start", "host"])
def test_preflight_explains_service_and_socket_recovery(command: str) -> None:
    # 2119: REQ-031.1.3
    # 2119: REQ-031.2.2
    # A failed probe alone cannot diagnose whether the engine is stopped or inaccessible.
    assert preflight_message(command, run=_run_fail) == (
        "Docker daemon unreachable for this user.\n"
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
        "Confirm `docker info` works without sudo before retrying Panopticon.\n"
        f"Then rerun `panopticon {command}`."
    )
