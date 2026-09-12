"""Bounded identity and runner checks for integrated runtime startup."""

# 2119-spec: runtime-readiness
from __future__ import annotations

import asyncio
import http.server
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
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
from panopticon.workflows import Spike

# Independent contract inventory: changing production environment selection requires an explicit
# decision about whether the new control must be cleared from a reused tmux server.
EXPECTED_CONTROLLED_ENVIRONMENT = frozenset(
    {
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


@contextmanager
def _live_authenticated_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    advertised_instance_id: str | None = None,
    register_runner: bool = True,
) -> Iterator[SimpleNamespace]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    service_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True)
    auth_file = secrets / "runtime-auth.json"
    auth_secret = "runtime-write-secret-value"
    auth_file.write_text(json.dumps({"read": [], "write": [auth_secret]}))
    auth_file.chmod(0o600)
    for name in SESSION_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    environment = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", os.defpath),
        "PANOPTICON_CONFIG": str(config),
        "PANOPTICON_DATA": str(tmp_path / "data"),
        "PANOPTICON_STATE": str(tmp_path / "state"),
        "PANOPTICON_SERVICE_AUTH_FILE": auth_file.name,
        "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
        "PANOPTICON_SERVICE_URL": service_url,
        "PANOPTICON_RUNNER_ID": "local",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    runtime = resolve_runtime(service_url)
    store = SqlAlchemyStore()
    service = TaskService(
        store,
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(Repo(id="repo", name="repo", git_url="https://example.test/repo"))
    )
    task = asyncio.run(service.create_task("repo", "spike", memo="preserve me"))
    if register_runner:
        asyncio.run(
            service.register_runner("local", instance_id=runtime.instance_id, host="private-host")
        )
    requests: list[tuple[str, str]] = []
    app = create_app(
        service,
        auth_file=auth_file.name,
        auth_mode="enforced",
        instance_id=advertised_instance_id or runtime.instance_id,
        secrets_dir=secrets,
    )

    @app.middleware("http")
    async def record_requests(request: object, call_next: object) -> object:
        requests.append((request.method, request.url.path))  # type: ignore[attr-defined]
        return await call_next(request)  # type: ignore[operator]

    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        yield SimpleNamespace(
            runtime=runtime,
            service=service,
            task=task,
            service_url=service_url,
            auth_secret=auth_secret,
            headers={"Authorization": f"Bearer {auth_secret}"},
            requests=requests,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        asyncio.run(store.close())


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


def test_transient_shell_paths_do_not_split_one_compatible_runtime() -> None:
    stable = {
        "HOME": "/users/example",
        "TMUX_TMPDIR": "/stable/tmux",
        "DOCKER_HOST": "unix:///stable/docker.sock",
        "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
        "PANOPTICON_SERVICE_AUTH_FILE": "runtime-auth.json",
    }
    first = resolve_runtime(
        "http://127.0.0.1:8000",
        environ={
            **stable,
            "PATH": "/terminal/bin",
            "PYTHONPATH": "/terminal/python",
            "TMPDIR": "/terminal/tmp",
            "VIRTUAL_ENV": "/terminal/venv",
        },
    )
    second = resolve_runtime(
        "http://127.0.0.1:8000",
        environ={
            **stable,
            "PATH": "/editor/bin",
            "PYTHONPATH": "/editor/python",
            "TMPDIR": "/editor/tmp",
            "VIRTUAL_ENV": "/editor/venv",
        },
    )

    assert first.instance_id == second.instance_id
    assert first.environment["PATH"] != second.environment["PATH"]
    assert first.environment["TMPDIR"] != second.environment["TMPDIR"]


# 2119: 1.9
def test_resolve_runtime_derives_service_and_default_container_ports() -> None:
    runtime = resolve_runtime(
        "http://127.0.0.1:9123",
        environ={"HOME": "/tmp", "PATH": "/bin"},
    )

    assert runtime.environment["PANOPTICON_PORT"] == "9123"
    assert runtime.container_service_url == "http://host.docker.internal:9123"


@pytest.mark.parametrize(
    ("name", "first", "second"),
    [
        ("PANOPTICON_CONTAINER_SERVICE_URL", "http://first:8000", "http://second:8000"),
        ("PANOPTICON_SERVICE_AUTH_MODE", "disabled", "enforced"),
        ("PANOPTICON_SERVICE_AUTH_FILE", "first.json", "second.json"),
        ("PANOPTICON_OPERATOR_TOKEN_FILE", "first.json", "second.json"),
    ],
)
def test_callback_and_auth_policy_changes_cannot_reuse_runtime_identity(
    name: str, first: str, second: str
) -> None:
    common = {"HOME": "/tmp/example", "PATH": "/bin"}
    old = resolve_runtime("http://127.0.0.1:8000", environ={**common, name: first})
    new = resolve_runtime("http://127.0.0.1:8000", environ={**common, name: second})
    assert old.instance_id != new.instance_id


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PANOPTICON_DATA", "/runtime/data"),
        ("PANOPTICON_CONFIG", "/runtime/config"),
        ("PANOPTICON_CACHE", "/runtime/cache"),
        ("PANOPTICON_STATE", "/runtime/state"),
        ("PANOPTICON_DB", "sqlite:////runtime/database.db"),
        ("TMUX_TMPDIR", "/runtime/tmux"),
        ("DOCKER_API_VERSION", "1.45"),
        ("DOCKER_CERT_PATH", "/runtime/docker-certs"),
        ("DOCKER_CONFIG", "/runtime/docker-config"),
        ("DOCKER_CONTEXT", "runtime-context"),
        ("DOCKER_DEFAULT_PLATFORM", "linux/arm64"),
        ("DOCKER_HOST", "unix:///runtime/docker.sock"),
        ("DOCKER_TLS_VERIFY", "1"),
    ],
)
def test_each_stable_storage_tmux_and_docker_setting_changes_runtime_identity(
    name: str, value: str
) -> None:
    common = {"HOME": "/users/example", "PATH": "/bin"}
    baseline = resolve_runtime("http://127.0.0.1:8000", environ=common)
    changed = resolve_runtime("http://127.0.0.1:8000", environ={**common, name: value})
    assert changed.instance_id != baseline.instance_id


def test_service_origin_and_executable_change_runtime_identity() -> None:
    environment = {"HOME": "/users/example", "PATH": "/bin"}
    baseline = resolve_runtime(
        "http://127.0.0.1:8000", environ=environment, executable="/runtime/python-a"
    )
    changed_origin = resolve_runtime(
        "http://127.0.0.1:9000", environ=environment, executable="/runtime/python-a"
    )
    changed_executable = resolve_runtime(
        "http://127.0.0.1:8000", environ=environment, executable="/runtime/python-b"
    )
    assert changed_origin.instance_id != baseline.instance_id
    assert changed_executable.instance_id != baseline.instance_id


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PANOPTICON_DB", "postgresql://user:password@database/panopticon"),
        ("PANOPTICON_CONTAINER_SERVICE_URL", "http://host:token@service:8000"),
        ("DOCKER_HOST", "tcp://docker.example:2376?access_token=secret"),
        ("DOCKER_HOST", "tcp://docker.example:2376?api_key=secret"),
        ("DOCKER_HOST", "tcp://docker.example:2376?apiKey=secret"),
        ("DOCKER_HOST", "tcp://docker.example:2376?API-KEY=secret"),
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


def test_operator_secret_selects_only_a_stable_private_file_reference(tmp_path: Path) -> None:
    common = {
        "HOME": str(tmp_path),
        "PATH": "/bin",
        "PANOPTICON_CONFIG": str(tmp_path / "config"),
    }
    configured = resolve_runtime(
        "http://127.0.0.1:8000",
        environ={**common, "PANOPTICON_OPERATOR_TOKEN": "operator-secret-value"},
    )
    assert configured.environment["PANOPTICON_OPERATOR_TOKEN_FILE"] == "operator-auth.json"
    assert "PANOPTICON_OPERATOR_TOKEN" not in configured.environment
    assert "operator-secret-value" not in json.dumps(dict(configured.environment))

    secrets = tmp_path / "config" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "operator-auth.json").write_text("private-existing-content")
    reused = resolve_runtime("http://127.0.0.1:8000", environ=common)
    assert reused.environment["PANOPTICON_OPERATOR_TOKEN_FILE"] == "operator-auth.json"


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


# 2119: 1.2, 1.3, 1.4, 1.5, 1.7
def test_independent_inventory_and_real_child_environment_boundary() -> None:
    assert frozenset(SESSION_ENVIRONMENT) == EXPECTED_CONTROLLED_ENVIRONMENT
    file_secret = "credential-file-contents-must-not-appear"
    selected = {
        "HOME": "/selected/home",
        "PATH": os.environ["PATH"],
        "TMPDIR": "/selected/tmp",
        "TMUX_TMPDIR": "/selected/tmux",
        "PANOPTICON_SERVICE_URL": "http://127.0.0.1:9123",
        "PANOPTICON_CONTAINER_SERVICE_URL": "http://host.docker.internal:9123",
        "PANOPTICON_RUNNER_ID": "selected-runner",
        "PANOPTICON_SERVICE_AUTH_FILE": "/selected/private-auth.json",
        "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
        "DOCKER_CERT_PATH": "/selected/docker-certs",
        "DOCKER_CONTEXT": "selected-context",
        "DOCKER_HOST": "unix:///selected/docker.sock",
        "DOCKER_TLS_VERIFY": "1",
        "ANTHROPIC_API_KEY": "ambient-provider-secret",
        "PANOPTICON_OPERATOR_TOKEN": "ambient-operator-secret",
    }
    stale_parent = dict(os.environ)
    stale_parent.update({name: f"stale-{name}" for name in SESSION_ENVIRONMENT})
    stale_parent["PATH"] = os.environ["PATH"]
    command = session_environment_argv(
        [
            sys.executable,
            "-c",
            "import json,os; print(json.dumps(dict(os.environ), sort_keys=True))",
        ],
        environment=selected,
    )

    result = subprocess.run(command, env=stale_parent, check=True, capture_output=True, text=True)
    child = json.loads(result.stdout)
    controlled_child = {
        name: child[name] for name in EXPECTED_CONTROLLED_ENVIRONMENT if name in child
    }
    expected = {
        name: value
        for name, value in selected.items()
        if name
        not in {
            "ANTHROPIC_API_KEY",
            "PANOPTICON_OPERATOR_TOKEN",
        }
    }
    assert controlled_child == expected
    assert "PYTHONPATH" not in child
    assert "PANOPTICON_TASK_ID" not in child
    assert file_secret not in " ".join(command)
    assert "ambient-provider-secret" not in " ".join(command)
    assert "ambient-operator-secret" not in " ".join(command)


# 2119: 1.3, 1.4, 1.5, 1.7, 1.9, 4.10
def test_every_emitted_integrated_child_executes_with_the_resolved_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panopticon.terminal import __main__ as cli
    from panopticon.terminal import console as terminal_console

    credential_contents = "credential-file-secret-value"
    credential = tmp_path / "runtime-auth.json"
    credential.write_text(credential_contents)
    credential.chmod(0o600)
    for name in SESSION_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    selected = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(tmp_path / "python"),
        "TMPDIR": str(tmp_path / "tmp"),
        "TMUX_TMPDIR": str(tmp_path / "tmux"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "PANOPTICON_HOST": "127.0.0.1",
        "PANOPTICON_RUNNER_ID": "selected-runner",
        "PANOPTICON_SERVICE_AUTH_FILE": str(credential),
        "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
        "DOCKER_CERT_PATH": str(tmp_path / "docker-certificates"),
        "DOCKER_CONTEXT": "selected-context",
        "DOCKER_HOST": "unix:///selected/docker.sock",
        "DOCKER_TLS_VERIFY": "1",
        "ANTHROPIC_API_KEY": "ambient-provider-secret",
        "PANOPTICON_OPERATOR_TOKEN": "ambient-operator-secret",
    }
    for name, value in selected.items():
        monkeypatch.setenv(name, value)
    runtime = resolve_runtime("http://127.0.0.1:9123")
    for name, value in runtime.environment.items():
        monkeypatch.setenv(name, value)

    tmux_calls: list[list[str]] = []

    def capture_sessions(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        tmux_calls.append(command)
        return subprocess.CompletedProcess(command, 1 if "has-session" in command else 0)

    cli._start_sessions(run=capture_sessions)
    emitted = [shlex.split(call[-1]) for call in tmux_calls if "new-session" in call]
    dashboard: list[list[str]] = []
    with monkeypatch.context() as local_patch:
        local_patch.setattr(terminal_console, "wait_for_service", lambda _url: True)
        local_patch.setattr(
            terminal_console, "switch_file_path", lambda _socket: tmp_path / "switch"
        )
        local_patch.setattr(
            terminal_console,
            "ensure_dashboard_session",
            lambda command, **_kwargs: dashboard.append(command),
        )
        local_patch.setattr(
            terminal_console.subprocess,
            "run",
            lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
        )
        local_patch.setattr(
            terminal_console,
            "run_console",
            lambda *, show_dashboard, **_kwargs: show_dashboard(),
        )
        terminal_console.run_console_local(runtime.service_url)
    emitted.extend(dashboard)
    assert len(emitted) == 3

    stale_parent = dict(os.environ)
    stale_parent.update({name: f"stale-{name}" for name in SESSION_ENVIRONMENT})
    stale_parent["PATH"] = os.environ["PATH"]
    expected = dict(runtime.environment)
    for command in emitted:
        executable = command.index(sys.executable)
        probe = [
            *command[:executable],
            sys.executable,
            "-c",
            "import json,os; print(json.dumps(dict(os.environ), sort_keys=True))",
        ]
        result = subprocess.run(
            probe,
            env=stale_parent,
            check=True,
            capture_output=True,
            text=True,
        )
        child = json.loads(result.stdout)
        controlled_child = {
            name: child[name] for name in EXPECTED_CONTROLLED_ENVIRONMENT if name in child
        }
        assert controlled_child == expected
        assert "PANOPTICON_TASK_ID" not in child
        joined = " ".join(command)
        assert credential_contents not in joined
        assert "ambient-provider-secret" not in joined
        assert "ambient-operator-secret" not in joined

    assert expected["PANOPTICON_PORT"] == "9123"
    assert expected["PANOPTICON_CONTAINER_SERVICE_URL"] == "http://host.docker.internal:9123"


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


def test_migration_guard_refuses_non_connect_transport_failure() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    session_probes = 0

    def broken(_path: str, _timeout: float) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer disconnected")

    def session_exists() -> bool:
        nonlocal session_probes
        session_probes += 1
        return False

    with pytest.raises(RuntimeReadinessError, match=r"connection failed.*migration was not run"):
        guard_before_migration(runtime, get=broken, service_session_exists=session_exists)
    assert session_probes == 0


# 2119: 2.8, 3.4
def test_migration_guard_reuses_compatible_api_across_package_versions() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})

    decision = guard_before_migration(
        runtime,
        get=lambda _path, _timeout: _response(200, _identity(runtime.instance_id, version="88.0")),
    )

    assert decision is MigrationDecision.COMPATIBLE_SERVICE_LIVE


@pytest.mark.parametrize(
    ("response", "message", "remedy"),
    [
        (
            _response(401, {"detail": "authentication required"}),
            "rejected the configured credential",
            "reconnect Panopticon authentication",
        ),
        (
            _response(404, {"detail": "Not Found"}),
            "unverified legacy task service",
            "panopticon stop",
        ),
        (
            _response(200, {"service": "not-panopticon"}),
            "different service",
            "check PANOPTICON_SERVICE_URL",
        ),
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
            "use matching client and service versions",
        ),
        (
            _response(200, []),
            "identity response is malformed",
            "configured address points to Panopticon",
        ),
    ],
)
def test_migration_guard_distinguishes_unverified_services(
    response: httpx.Response, message: str, remedy: str
) -> None:
    # 2119: 2.3, 2.6, 2.7, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15
    # 2119: 3.6, 5.2, 5.3
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    with pytest.raises(RuntimeReadinessError, match=message) as raised:
        guard_before_migration(runtime, get=lambda _path, _timeout: response)
    assert remedy in str(raised.value)


def test_migration_guard_reports_missing_package_version_as_malformed() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    malformed = _identity(runtime.instance_id)
    malformed["version"] = None
    with pytest.raises(RuntimeReadinessError, match="no valid package version") as raised:
        guard_before_migration(
            runtime,
            get=lambda _path, _timeout: _response(200, malformed),
        )
    assert "PANOPTICON_SERVICE_URL" in str(raised.value)


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


def test_readiness_retries_a_transient_protocol_failure() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    clock = _Clock()
    attempts = 0

    def get(path: str, _timeout: float) -> httpx.Response:
        nonlocal attempts
        if attempts == 0:
            attempts += 1
            raise httpx.RemoteProtocolError("server was still starting")
        if path == "/identity":
            return _response(200, _identity(runtime.instance_id))
        return _response(
            200,
            [{"id": runtime.runner_id, "host": None, "instance_id": runtime.instance_id}],
        )

    wait_until_ready(
        runtime,
        timeout=1,
        interval=0.1,
        get=get,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert attempts == 1
    assert clock.now == pytest.approx(0.1)


# 2119: 4.1, 4.2, 4.11
def test_identity_request_duration_reduces_runner_request_budget() -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    clock = _Clock()
    budgets: list[tuple[str, float]] = []

    def get(path: str, timeout: float) -> httpx.Response:
        budgets.append((path, timeout))
        clock.now += 0.06
        if path == "/identity":
            return _response(200, _identity(runtime.instance_id))
        return _response(200, [])

    with pytest.raises(RuntimeReadinessError, match="runner"):
        wait_until_ready(
            runtime,
            timeout=0.1,
            interval=0.1,
            request_timeout=1,
            get=get,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert budgets[0] == ("/identity", pytest.approx(0.1))
    assert budgets[1] == ("/runners", pytest.approx(0.04))
    assert len(budgets) == 2


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
        RuntimeReadinessError, match=r"other live runners: other.*runner\.log"
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
    calls = 0

    def get(path: str, _timeout: float) -> httpx.Response:
        nonlocal calls
        calls += 1
        if path == "/identity":
            return _response(200, _identity(runtime.instance_id))
        return _response(
            200,
            [{"id": "wanted", "host": None, "instance_id": "stale-runtime"}],
        )

    with pytest.raises(RuntimeReadinessError, match="connected from a different runtime"):
        wait_until_ready(
            runtime,
            timeout=0.2,
            interval=0.1,
            get=get,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert calls == 2
    assert clock.now == 0


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
            timeout=0.2,
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


# 2119: 2.1, 4.3, 5.4, 5.5, 5.6, 5.7
def test_real_authenticated_readiness_preserves_fleet_state_and_invokes_no_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _live_authenticated_runtime(tmp_path, monkeypatch) as live:
        from panopticon.container import agent as container_agent

        agent_calls: list[str] = []
        monkeypatch.setattr(
            container_agent,
            "_run_agent",
            lambda *_args, **_kwargs: agent_calls.append("agent") or 1,
        )
        with httpx.Client(
            base_url=live.service_url, headers=live.headers, trust_env=False
        ) as client:
            before = (client.get("/repos").json(), client.get("/tasks").json())
            request_start = len(live.requests)
            identity = wait_until_ready(live.runtime, timeout=1)
            readiness_requests = live.requests[request_start:]
            after = (client.get("/repos").json(), client.get("/tasks").json())

        assert identity == {
            "service": "panopticon-task-service",
            "api_revision": 1,
            "version": version("panopticon-next"),
            "instance_id": live.runtime.instance_id,
        }
        assert readiness_requests == [("GET", "/identity"), ("GET", "/runners")]
        assert before == after
        assert agent_calls == []
        assert live.auth_secret not in json.dumps(identity)


# 2119: 1.1, 2.10, 2.11, 3.1, 3.5, 3.7, 3.8, 3.9, 4.4, 5.1
def test_integrated_startup_reuses_real_verified_service_and_runner_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panopticon.terminal import __main__ as cli
    from panopticon.terminal import runtime as runtime_module

    with _live_authenticated_runtime(tmp_path, monkeypatch) as live:
        with httpx.Client(
            base_url=live.service_url, headers=live.headers, trust_env=False
        ) as client:
            before = (client.get("/repos").json(), client.get("/tasks").json())
        tmux_calls: list[list[str]] = []

        def existing_sessions(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            tmux_calls.append(command)
            if command[-3:] not in (
                ["has-session", "-t", "service"],
                ["has-session", "-t", "runner"],
            ):
                pytest.fail(f"startup replaced an existing session: {command!r}")
            return subprocess.CompletedProcess(command, 0)

        real_resolve = runtime_module.resolve_runtime
        resolutions: list[str] = []

        def counted_resolve(service_url: str) -> object:
            resolutions.append(service_url)
            return real_resolve(service_url)

        monkeypatch.setattr(cli.subprocess, "run", existing_sessions)
        monkeypatch.setattr(
            cli,
            "_run_migrate",
            lambda: pytest.fail("migration ran beneath a verified live service"),
        )
        monkeypatch.setattr(runtime_module, "resolve_runtime", counted_resolve)

        assert cli._prepare_integrated_runtime(live.service_url, "panopticon start")
        with httpx.Client(
            base_url=live.service_url, headers=live.headers, trust_env=False
        ) as client:
            after = (client.get("/repos").json(), client.get("/tasks").json())

        assert resolutions == [live.service_url]
        assert [call[-3:] for call in tmux_calls] == [
            ["has-session", "-t", "service"],
            ["has-session", "-t", "runner"],
        ]
        assert before == after


# 2119: 2.10, 2.11, 2.16, 3.6, 3.7, 3.8, 3.9, 5.2, 5.3
def test_integrated_startup_refuses_real_incompatible_service_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from panopticon.terminal import __main__ as cli

    with _live_authenticated_runtime(
        tmp_path, monkeypatch, advertised_instance_id="different-instance"
    ) as live:
        with httpx.Client(
            base_url=live.service_url, headers=live.headers, trust_env=False
        ) as client:
            before = (client.get("/repos").json(), client.get("/tasks").json())
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("tmux was mutated after guard refusal"),
        )
        monkeypatch.setattr(
            cli, "_run_migrate", lambda: pytest.fail("migration ran after guard refusal")
        )

        assert not cli._prepare_integrated_runtime(live.service_url, "panopticon start")
        with httpx.Client(
            base_url=live.service_url, headers=live.headers, trust_env=False
        ) as client:
            after = (client.get("/repos").json(), client.get("/tasks").json())

        diagnosis = capsys.readouterr().out
        assert "different Panopticon runtime" in diagnosis
        assert "preserve it" in diagnosis
        assert live.auth_secret not in diagnosis
        assert before == after


# 2119: 4.13, 4.14, 5.4, 5.5, 5.6
def test_quickstart_missing_runner_cannot_register_or_rebind_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from panopticon.terminal import __main__ as cli
    from panopticon.terminal import doctor, quickstart, setup
    from panopticon.terminal import runtime as runtime_module
    from panopticon.terminal.setup_credentials import Connection
    from panopticon.terminal.source_selection import RepositorySource

    with _live_authenticated_runtime(tmp_path, monkeypatch, register_runner=False) as live:
        with httpx.Client(
            base_url=live.service_url, headers=live.headers, trust_env=False
        ) as client:
            before = (client.get("/repos").json(), client.get("/tasks").json())
        monkeypatch.setattr(
            setup, "configure_connection", lambda: Connection("claude", "candidate.env")
        )
        monkeypatch.setattr(
            quickstart,
            "select_source",
            lambda: RepositorySource("https://example.test/new", "new", "remote"),
        )
        monkeypatch.setattr(doctor, "run_checks", lambda **_kwargs: [])
        monkeypatch.setattr(doctor, "report", lambda _checks: 0)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
        )
        monkeypatch.setattr(
            cli, "_run_migrate", lambda: pytest.fail("migration ran beneath the live service")
        )
        real_wait = runtime_module.wait_until_ready
        monkeypatch.setattr(
            runtime_module,
            "wait_until_ready",
            lambda runtime: real_wait(runtime, timeout=0.1, interval=0.02),
        )
        monkeypatch.setattr(
            quickstart,
            "setup_repo",
            lambda *_args, **_kwargs: pytest.fail("repository registration ran before readiness"),
        )
        monkeypatch.setattr(
            setup,
            "configure_repo",
            lambda *_args, **_kwargs: pytest.fail("credential mutation ran before readiness"),
        )

        assert cli.main(["--service-url", live.service_url, "quickstart"]) == 1
        with httpx.Client(
            base_url=live.service_url, headers=live.headers, trust_env=False
        ) as client:
            after = (client.get("/repos").json(), client.get("/tasks").json())

        diagnosis = capsys.readouterr().out
        assert "runner 'local' did not connect" in diagnosis
        assert "runner.log" in diagnosis
        assert live.auth_secret not in diagnosis
        assert before == after


# 2119: 2.5, 2.9, 2.10, 2.11, 2.12, 5.2, 5.3
def test_integrated_startup_preserves_real_legacy_health_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from panopticon.terminal import __main__ as cli

    requests: list[tuple[str, str]] = []

    class LegacyHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(("GET", self.path))
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LegacyHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    service_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with httpx.Client(base_url=service_url, trust_env=False) as client:
            assert client.get("/healthz").status_code == 200
        requests.clear()
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("legacy service session was changed"),
        )
        monkeypatch.setattr(
            cli, "_run_migrate", lambda: pytest.fail("migration ran beneath legacy service")
        )

        assert not cli._prepare_integrated_runtime(service_url, "panopticon start")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert requests == [("GET", "/identity")]
    diagnosis = capsys.readouterr().out
    assert "unverified legacy task service" in diagnosis
    assert "leave it running" in diagnosis
    assert "panopticon stop" in diagnosis


# 2119: 3.3, 3.7, 3.8, 5.2, 5.3
@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_integrated_startup_preserves_real_inert_service_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from panopticon.terminal import __main__ as cli

    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="pn-runtime-", dir="/tmp"))
    tmux_tmpdir.chmod(0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_tmpdir))
    command = ["tmux", "-L", "panopticon"]
    try:
        created = subprocess.run(
            [*command, "new-session", "-d", "-s", "service", "sleep", "30"],
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        service_url = f"http://127.0.0.1:{probe.getsockname()[1]}"
        probe.close()
        monkeypatch.setattr(
            cli, "_run_migrate", lambda: pytest.fail("migration ran under an inert service")
        )
        monkeypatch.setattr(
            cli,
            "_start_sessions_with_help",
            lambda _command: pytest.fail("startup changed an inert service session"),
        )

        assert not cli._prepare_integrated_runtime(service_url, "panopticon start")
        assert (
            subprocess.run(
                [*command, "has-session", "-t", "service"], capture_output=True
            ).returncode
            == 0
        )
        diagnosis = capsys.readouterr().out
        assert "service tmux session exists" in diagnosis
        assert "migration was not run" in diagnosis
        assert "stop the fleet deliberately" in diagnosis
    finally:
        subprocess.run([*command, "kill-server"], capture_output=True)
        shutil.rmtree(tmux_tmpdir)


# 2119: 4.1, 4.2, 4.11
def test_production_http_client_bounds_a_nonresponsive_peer() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.05)
    stop = threading.Event()

    def accept_without_responding() -> None:
        while not stop.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            with connection:
                stop.wait(1)

    thread = threading.Thread(target=accept_without_responding)
    thread.start()
    runtime = resolve_runtime(
        f"http://127.0.0.1:{listener.getsockname()[1]}",
        environ={"HOME": "/tmp", "PATH": os.environ["PATH"]},
    )
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeReadinessError, match="did not become ready"):
            wait_until_ready(runtime, timeout=0.12, interval=0.01, request_timeout=0.03)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)

    assert 0.1 <= elapsed < 0.5


# 2119: 2.6, 2.7
@pytest.mark.parametrize("revision", [True, False, "1", 1.0, 2, None])
@pytest.mark.parametrize("check", [guard_before_migration, wait_until_ready])
def test_runtime_guard_rejects_noninteger_or_unsupported_api_revision(revision, check) -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    identity = _identity(runtime.instance_id)
    identity["api_revision"] = revision
    with pytest.raises(RuntimeReadinessError, match=r"revision .* is unsupported"):
        check(runtime, get=lambda _path, _timeout: _response(200, identity))


# 2119: 2.14
@pytest.mark.parametrize("body", [b"{", b"not JSON"])
def test_runtime_guard_diagnoses_malformed_json_identity(body: bytes) -> None:
    runtime = resolve_runtime("http://service", environ={"HOME": "/tmp", "PATH": "/bin"})
    response = httpx.Response(
        200, content=body, request=httpx.Request("GET", "http://service/identity")
    )
    with pytest.raises(RuntimeReadinessError, match="identity response is malformed") as raised:
        guard_before_migration(runtime, get=lambda _path, _timeout: response)
    assert "configured address points to Panopticon" in str(raised.value)


# 2119: 4.14
@pytest.mark.parametrize("runner_state", ["missing", "wrong-instance"])
def test_direct_repository_repair_requires_matching_live_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_state: str
) -> None:
    from panopticon.client import TaskServiceClient
    from panopticon.terminal import runtime as runtime_module
    from panopticon.terminal import setup

    with _live_authenticated_runtime(tmp_path, monkeypatch, register_runner=False) as live:
        if runner_state == "wrong-instance":
            asyncio.run(live.service.register_runner("local", instance_id="different-instance"))
        real_wait = runtime_module.wait_until_ready
        monkeypatch.setattr(
            runtime_module,
            "wait_until_ready",
            lambda runtime, **_kwargs: real_wait(runtime, timeout=0.12, interval=0.02),
        )
        secrets = tmp_path / "config" / "secrets"
        before_files = {p.name: p.read_bytes() for p in secrets.iterdir()}
        monkeypatch.setattr(
            setup, "write_private", lambda *_args: pytest.fail("credential write before readiness")
        )
        with httpx.Client(base_url=live.service_url, headers=live.headers, trust_env=False) as http:
            client = TaskServiceClient(http)
            before = (client.get_repo("repo"), client.list_tasks())
            with pytest.raises(RuntimeReadinessError, match="runner 'local'"):
                setup.configure_repo(
                    client, "repo", input_fn=lambda _: pytest.fail("prompt before readiness")
                )
            assert (client.get_repo("repo"), client.list_tasks()) == before
        assert {p.name: p.read_bytes() for p in secrets.iterdir()} == before_files
