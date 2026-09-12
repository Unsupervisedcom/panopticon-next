"""Bounded identity and runner checks for integrated runtime startup."""

# 2119-spec: runtime-readiness
from __future__ import annotations

import subprocess

import httpx
import pytest

from panopticon.terminal.runtime import (
    MigrationDecision,
    RuntimeReadinessError,
    guard_before_migration,
    integrated_service_session_exists,
    resolve_runtime,
    wait_until_ready,
)
from panopticon.terminal.session_environment import (
    SESSION_ENVIRONMENT,
    session_environment_argv,
)


def _response(status: int, body: object) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("GET", "http://service/identity"),
    )


def _identity(runtime_id: str, *, version: str = "99.8.7", revision: int = 1) -> dict[str, object]:
    return {
        "service": "panopticon-task-service",
        "api_revision": revision,
        "version": version,
        "instance_id": runtime_id,
    }


# 2119: 1.1, 1.3, 1.7
def test_resolve_runtime_pins_paths_addresses_tools_and_stable_instance() -> None:
    environ = {
        "HOME": "/users/example",
        "PATH": "/chosen/bin:/usr/bin",
        "TMPDIR": "/chosen/tmp",
        "TMUX_TMPDIR": "/chosen/tmux",
        "XDG_CONFIG_HOME": "/chosen/config",
        "PANOPTICON_DATA": "/chosen/data",
        "PANOPTICON_CONTAINER_SERVICE_URL": "http://container-service:9000",
        "PANOPTICON_RUNNER_ID": "runner-a",
        "DOCKER_CONTEXT": "chosen-docker",
        "PANOPTICON_INSTANCE_ID": "stale-must-be-replaced",
    }

    first = resolve_runtime(
        "http://127.0.0.1:9000/", environ=environ, executable="/usr/bin/python3"
    )
    second = resolve_runtime(
        "http://127.0.0.1:9000",
        environ={**environ, "PANOPTICON_RUNNER_ID": "runner-b"},
        executable="/usr/bin/python3",
    )

    assert first.instance_id == second.instance_id  # runner selection is verified separately
    assert first.service_url == "http://127.0.0.1:9000"
    assert first.environment["PANOPTICON_CONFIG"] == "/chosen/config/panopticon"
    assert first.environment["PANOPTICON_DATA"] == "/chosen/data"
    assert first.environment["PANOPTICON_CACHE"] == "/users/example/.cache/panopticon"
    assert first.environment["PANOPTICON_STATE"] == "/users/example/.local/state/panopticon"
    assert first.environment["PANOPTICON_SERVICE_URL"] == first.service_url
    assert first.environment["PANOPTICON_PORT"] == "9000"
    assert first.environment["PANOPTICON_CONTAINER_SERVICE_URL"] == "http://container-service:9000"
    assert first.environment["PATH"] == "/chosen/bin:/usr/bin"
    assert first.environment["TMPDIR"] == "/chosen/tmp"
    assert first.environment["TMUX_TMPDIR"] == "/chosen/tmux"
    assert first.environment["DOCKER_CONTEXT"] == "chosen-docker"
    assert first.environment["PANOPTICON_INSTANCE_ID"] == first.instance_id
    changed = resolve_runtime(
        first.service_url,
        environ={**environ, "PANOPTICON_DATA": "/different/data"},
        executable="/usr/bin/python3",
    )
    assert changed.instance_id != first.instance_id
    changed_tmux_socket = resolve_runtime(
        first.service_url,
        environ={**environ, "TMUX_TMPDIR": "/different/tmux"},
        executable="/usr/bin/python3",
    )
    assert changed_tmux_socket.instance_id != first.instance_id


# 2119: 1.9
def test_resolve_runtime_derives_service_and_default_container_ports() -> None:
    runtime = resolve_runtime(
        "http://127.0.0.1:9123",
        environ={"HOME": "/tmp", "PATH": "/bin"},
    )

    assert runtime.environment["PANOPTICON_PORT"] == "9123"
    assert runtime.container_service_url == "http://host.docker.internal:9123"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PANOPTICON_DB", "postgresql://user:password@database/panopticon"),
        ("PANOPTICON_CONTAINER_SERVICE_URL", "http://host:token@service:8000"),
        ("DOCKER_HOST", "tcp://docker.example:2376?access_token=secret"),
    ],
)
def test_resolve_runtime_rejects_embedded_credentials_without_echoing_them(
    name: str, value: str
) -> None:
    # 2119: 1.5, 1.8
    with pytest.raises(ValueError) as raised:
        resolve_runtime(
            "http://127.0.0.1:8000",
            environ={"HOME": "/tmp", "PATH": "/bin", name: value},
        )

    assert name in str(raised.value)
    assert "password" not in str(raised.value)
    assert "secret" not in str(raised.value)


# 2119: 1.2, 1.3, 1.4, 1.5, 1.7
def test_session_environment_clears_every_control_and_never_renders_secret_values() -> None:
    selected = {
        "PATH": "/current/bin",
        "TMPDIR": "/current/tmp",
        "TMUX_TMPDIR": "/current/tmux",
        "PANOPTICON_SERVICE_URL": "http://current:8000",
        "PANOPTICON_CONFIG": "/current/config",
        "DOCKER_HOST": "unix:///current.sock",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "PANOPTICON_OPERATOR_TOKEN": "operator-secret",
        "PANOPTICON_TASK_ID": "stale-task",
        "UNRELATED": "ignored",
    }

    argv = session_environment_argv(["python", "-m", "worker"], environment=selected)

    for name in SESSION_ENVIRONMENT:
        position = argv.index(name)
        assert argv[position - 1] == "-u"
    assert "PATH=/current/bin" in argv
    assert "TMPDIR=/current/tmp" in argv
    assert "TMUX_TMPDIR=/current/tmux" in argv
    assert "PANOPTICON_SERVICE_URL=http://current:8000" in argv
    assert "PANOPTICON_CONFIG=/current/config" in argv
    assert "DOCKER_HOST=unix:///current.sock" in argv
    assert not any("secret" in argument for argument in argv)
    assert not any(argument.startswith("PANOPTICON_TASK_ID=") for argument in argv)
    assert not any(argument.startswith("UNRELATED=") for argument in argv)


# 2119: 1.5, 1.8
def test_session_environment_rejects_embedded_url_secret_without_rendering_it() -> None:
    value = "postgresql://user:private-value@database/panopticon"

    with pytest.raises(ValueError) as raised:
        session_environment_argv(["worker"], environment={"PANOPTICON_DB": value})

    assert "PANOPTICON_DB" in str(raised.value)
    assert "private-value" not in str(raised.value)


def test_integrated_service_session_probe_is_exact_and_missing_tmux_is_absent() -> None:
    calls: list[list[str]] = []

    def present(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    assert integrated_service_session_exists(run=present)
    assert calls[0][-3:] == ["has-session", "-t", "service"]

    def missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("tmux")

    assert not integrated_service_session_exists(run=missing)


# 2119: 3.1, 3.2, 3.3
def test_migration_guard_permits_only_absent_endpoint_without_service_session() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})

    def unavailable(_path: str, _timeout: float) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert (
        guard_before_migration(runtime, get=unavailable, service_session_exists=lambda: False)
        is MigrationDecision.SERVICE_ABSENT
    )
    with pytest.raises(RuntimeReadinessError, match="service tmux session exists"):
        guard_before_migration(runtime, get=unavailable, service_session_exists=lambda: True)


# 2119: 2.8, 3.4
def test_migration_guard_reuses_compatible_api_across_package_versions() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})

    decision = guard_before_migration(
        runtime,
        get=lambda _path, _timeout: _response(200, _identity(runtime.instance_id, version="88.0")),
    )

    assert decision is MigrationDecision.COMPATIBLE_SERVICE_LIVE


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            _response(401, {"detail": "authentication required"}),
            "rejected the configured credential",
        ),
        (_response(404, {"detail": "Not Found"}), "unverified legacy task service"),
        (_response(200, {"service": "not-panopticon"}), "different service"),
        (
            _response(
                200,
                {
                    "service": "panopticon-task-service",
                    "api_revision": 2,
                    "version": "2",
                    "instance_id": "other",
                },
            ),
            "revision 2 is unsupported",
        ),
        (_response(200, []), "identity response is malformed"),
    ],
)
def test_migration_guard_distinguishes_unverified_services(
    response: httpx.Response, message: str
) -> None:
    # 2119: 2.3, 2.6, 2.7, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15
    # 2119: 3.6, 5.2, 5.3
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    with pytest.raises(RuntimeReadinessError, match=message):
        guard_before_migration(runtime, get=lambda _path, _timeout: response)


# 2119: 2.4, 2.16, 3.6, 5.2, 5.3
def test_migration_guard_rejects_another_panopticon_instance() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    with pytest.raises(RuntimeReadinessError, match="different Panopticon runtime"):
        guard_before_migration(
            runtime,
            get=lambda _path, _timeout: _response(200, _identity("another-instance")),
        )


# 2119: 2.5
def test_health_response_cannot_satisfy_runtime_identity() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})

    with pytest.raises(RuntimeReadinessError, match="different service"):
        guard_before_migration(
            runtime,
            get=lambda _path, _timeout: _response(200, {"status": "ok"}),
        )


# 2119: 3.7, 3.8, 3.9
def test_migration_guard_only_reads_identity_and_session_presence() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    events: list[str] = []

    def unavailable(path: str, _timeout: float) -> httpx.Response:
        events.append(f"GET {path}")
        raise httpx.ConnectError("refused")

    def session_exists() -> bool:
        events.append("tmux has-session service")
        return False

    assert (
        guard_before_migration(
            runtime,
            get=unavailable,
            service_session_exists=session_exists,
        )
        is MigrationDecision.SERVICE_ABSENT
    )
    assert events == ["GET /identity", "tmux has-session service"]


# 2119: 3.5
def test_integrated_runtime_skips_migration_for_verified_live_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from panopticon.terminal import __main__ as cli
    from panopticon.terminal import runtime as runtime_module

    calls: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "guard_before_migration",
        lambda _runtime: MigrationDecision.COMPATIBLE_SERVICE_LIVE,
    )
    monkeypatch.setattr(cli, "_run_migrate", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_start_sessions_with_help", lambda _command: True)
    monkeypatch.setattr(runtime_module, "wait_until_ready", lambda _runtime: calls.append("ready"))

    assert cli._prepare_integrated_runtime("http://service", "panopticon start")
    assert calls == ["ready"]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# 2119: 4.1, 4.2, 4.3, 4.11
def test_readiness_retries_until_intended_live_runner_appears() -> None:
    runtime = resolve_runtime(
        "http://service",
        environ={"HOME": "/tmp", "PATH": "/bin", "PANOPTICON_RUNNER_ID": "wanted"},
    )
    clock = _Clock()
    inventories = iter(
        [
            [{"id": "other", "host": None, "instance_id": "other-runtime"}],
            [{"id": "wanted", "host": None, "instance_id": runtime.instance_id}],
        ]
    )

    def get(path: str, timeout: float) -> httpx.Response:
        assert 0 < timeout <= 0.3
        if path == "/identity":
            return _response(200, _identity(runtime.instance_id))
        return _response(200, next(inventories))

    identity = wait_until_ready(
        runtime,
        timeout=1,
        interval=0.1,
        request_timeout=0.3,
        get=get,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert identity["version"] == "99.8.7"
    assert clock.now == pytest.approx(0.1)


# 2119: 4.1, 4.2, 4.7, 4.9, 4.10
def test_readiness_deadline_reports_ready_service_with_missing_runner() -> None:
    runtime = resolve_runtime(
        "http://service",
        environ={"HOME": "/tmp", "PATH": "/bin", "PANOPTICON_RUNNER_ID": "wanted"},
    )
    clock = _Clock()

    def get(path: str, _timeout: float) -> httpx.Response:
        if path == "/identity":
            return _response(200, _identity(runtime.instance_id))
        return _response(
            200,
            [
                {
                    "id": "other",
                    "host": "private-runner-host",
                    "instance_id": "other-runtime",
                }
            ],
        )

    with pytest.raises(
        RuntimeReadinessError, match=r"other live runners: other.*runner log"
    ) as raised:
        wait_until_ready(
            runtime,
            timeout=0.2,
            interval=0.1,
            get=get,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.now == pytest.approx(0.2)
    assert "private-runner-host" not in str(raised.value)


# 2119: 4.3, 4.5, 5.3
def test_readiness_rejects_matching_runner_id_from_wrong_runtime() -> None:
    runtime = resolve_runtime(
        "http://service",
        environ={"HOME": "/tmp", "PATH": "/bin", "PANOPTICON_RUNNER_ID": "wanted"},
    )
    clock = _Clock()

    def get(path: str, _timeout: float) -> httpx.Response:
        if path == "/identity":
            return _response(200, _identity(runtime.instance_id))
        return _response(
            200,
            [{"id": "wanted", "host": None, "instance_id": "stale-runtime"}],
        )

    with pytest.raises(RuntimeReadinessError, match="connected from a different runtime"):
        wait_until_ready(
            runtime,
            timeout=0.1,
            interval=0.1,
            get=get,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


# 2119: 4.1, 4.4, 4.7, 4.11, 5.3
def test_readiness_deadline_reports_unreachable_service() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    clock = _Clock()

    def unavailable(_path: str, _timeout: float) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(RuntimeReadinessError, match=r"service.*did not become ready.*service log"):
        wait_until_ready(
            runtime,
            timeout=0.2,
            interval=0.1,
            get=unavailable,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_readiness_reports_service_unavailable_when_it_disappears_before_deadline() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    clock = _Clock()
    attempts = 0

    def disappearing(path: str, _timeout: float) -> httpx.Response:
        nonlocal attempts
        if attempts == 0:
            if path == "/identity":
                return _response(200, _identity(runtime.instance_id))
            attempts += 1
            return _response(200, [])
        raise httpx.ConnectError("refused")

    with pytest.raises(RuntimeReadinessError, match=r"service.*did not become ready.*service log"):
        wait_until_ready(
            runtime,
            timeout=0.1,
            interval=0.1,
            get=disappearing,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


# 2119: 4.5
def test_readiness_fails_wrong_instance_immediately() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    clock = _Clock()
    with pytest.raises(RuntimeReadinessError, match="different Panopticon runtime"):
        wait_until_ready(
            runtime,
            timeout=10,
            get=lambda _path, _timeout: _response(200, _identity("wrong")),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.now == 0


@pytest.mark.parametrize(
    "response",
    [
        _response(401, {"detail": "authentication required"}),
        _response(200, {"service": "another-service"}),
        _response(200, _identity("ignored", revision=2)),
    ],
)
def test_readiness_fails_identity_rejections_without_waiting(response: httpx.Response) -> None:
    # 2119: 4.5
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    clock = _Clock()

    with pytest.raises(RuntimeReadinessError):
        wait_until_ready(
            runtime,
            timeout=10,
            get=lambda _path, _timeout: response,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.now == 0


# 2119: 5.4, 5.5, 5.6, 5.7
def test_readiness_uses_only_identity_and_runner_inventory_reads() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    paths: list[str] = []

    def get(path: str, _timeout: float) -> httpx.Response:
        paths.append(path)
        if path == "/identity":
            return _response(200, _identity(runtime.instance_id))
        return _response(
            200,
            [{"id": runtime.runner_id, "host": None, "instance_id": runtime.instance_id}],
        )

    wait_until_ready(runtime, get=get)

    assert paths == ["/identity", "/runners"]
