"""Independent runtime contract boundary evidence; no fleet, provider, or credential use."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from test_runtime_readiness import _identity, _live_authenticated_runtime, _response

from panopticon.terminal import __main__ as cli
from panopticon.terminal import runtime as runtime_module
from panopticon.terminal.runtime import RuntimeReadinessError, resolve_runtime, wait_until_ready
from panopticon.terminal.session_environment import SESSION_ENVIRONMENT, session_environment_argv

# 2119-spec: runtime-readiness


@pytest.fixture
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in SESSION_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "HOME": str(tmp_path / "home"),
        "PATH": os.defpath,
        "PANOPTICON_CONFIG": str(tmp_path / "config"),
        "PANOPTICON_STATE": str(tmp_path / "state"),
        "PANOPTICON_DATA": str(tmp_path / "data"),
        "PANOPTICON_SERVICE_AUTH_MODE": "disabled",
    }.items():
        monkeypatch.setenv(key, value)
    return resolve_runtime("http://127.0.0.1:8137")


# 2119: 1.1, 3.1
@pytest.mark.parametrize("reachable", [False, True])
def test_integrated_cold_start_resolves_once_and_probes_before_migration_or_tmux(
    isolated_runtime, monkeypatch: pytest.MonkeyPatch, reachable: bool
) -> None:
    events = []
    resolved = []
    actual_resolve = runtime_module.resolve_runtime
    started = False

    def resolve(url):
        events.append("resolve")
        result = actual_resolve(url)
        resolved.append(result)
        return result

    def request(path, _timeout):
        events.append(f"GET {path}")
        if path == "/identity":
            if not reachable and not started:
                raise httpx.ConnectError("not listening")
            return _response(200, _identity(resolved[0].instance_id))
        assert path == "/runners"
        return _response(
            200, [{"id": resolved[0].runner_id, "instance_id": resolved[0].instance_id}]
        )

    def command(argv, **_kwargs):
        nonlocal started
        assert argv[0] == "tmux", argv
        if "has-session" in argv:
            events.append("tmux probe")
            return subprocess.CompletedProcess(argv, 1)  # no integrated sessions, on either branch
        assert "new-session" in argv, argv
        events.append("tmux create")
        started = True
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(runtime_module, "resolve_runtime", resolve)
    monkeypatch.setattr(runtime_module, "_default_get", lambda _runtime: request)
    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setattr(cli, "_run_migrate", lambda: events.append("migrate"))
    assert cli._prepare_integrated_runtime(isolated_runtime.service_url, "panopticon start")
    assert len(resolved) == 1
    assert events[:2] == ["resolve", "GET /identity"]
    assert events.index("GET /identity") < events.index("tmux probe")
    assert events.count("tmux create") == 2
    if reachable:
        assert "migrate" not in events
    else:
        assert events.index("GET /identity") < events.index("migrate") < events.index("tmux create")
    assert events[-2:] == ["GET /identity", "GET /runners"]


# 2119: 1.8
@pytest.mark.parametrize(
    "source,url,secret",
    [
        ("argument", "http://user:distinct-password@127.0.0.1:8137", "distinct-password"),
        ("argument", "http://127.0.0.1:8137?token=argument-query-value", "argument-query-value"),
        ("callback", "http://127.0.0.1:8137?token=callback-query-value", "callback-query-value"),
    ],
)
def test_integrated_url_credential_rejection_redacts_the_actual_value(
    isolated_runtime,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    url: str,
    secret: str,
) -> None:
    if source == "callback":
        monkeypatch.setenv("PANOPTICON_CONTAINER_SERVICE_URL", url)
        service_url = isolated_runtime.service_url
    else:
        service_url = url
    monkeypatch.setattr(runtime_module, "_default_get", lambda *_: pytest.fail("probed unsafe URL"))
    monkeypatch.setattr(cli, "_run_migrate", lambda: pytest.fail("migrated unsafe URL"))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("spawned unsafe URL"))
    assert not cli._prepare_integrated_runtime(service_url, "panopticon start")
    output = capsys.readouterr()
    assert "credential" in output.out.lower()
    assert secret not in output.out + output.err
    assert url not in output.out + output.err


# 2119: 2.1
def test_identity_reads_the_resolved_launch_environment_without_explicit_override(
    isolated_runtime, tmp_path: Path
) -> None:
    secrets = Path(isolated_runtime.environment["PANOPTICON_CONFIG"]) / "secrets"
    secrets.mkdir(parents=True)
    auth = secrets / "service.json"
    auth.write_text(
        json.dumps({"read": ["synthetic-read-token"], "write": ["synthetic-write-token"]})
    )
    auth.chmod(0o600)
    environment = dict(isolated_runtime.environment)
    environment.update(
        PANOPTICON_SERVICE_AUTH_FILE=auth.name, PANOPTICON_SERVICE_AUTH_MODE="enforced"
    )
    runtime = resolve_runtime(isolated_runtime.service_url, environ=environment)
    script = """
import json, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike
service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(Path(sys.argv[1])))
# No instance_id argument: only the actual child launch environment supplies it.
app = create_app(service, auth_mode="enforced", auth_file=os.environ["PANOPTICON_SERVICE_AUTH_FILE"])
with TestClient(app) as http:
    denied = http.get("/identity")
    response = http.get("/identity", headers={"Authorization": "Bearer synthetic-read-token"})
    print(json.dumps({"denied": denied.status_code, "status": response.status_code, "body": response.json()}))
"""
    command = session_environment_argv(
        [sys.executable, "-c", script, str(tmp_path / "artifacts")], environment=runtime.environment
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "PANOPTICON_INSTANCE_ID": "stale-parent-instance"},
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["denied"] == 401 and observed["status"] == 200
    body = observed["body"]
    assert body["instance_id"] == runtime.instance_id
    assert body["service"] == "panopticon-task-service"
    assert body["api_revision"] == 1
    assert isinstance(body["version"], str) and body["version"]
    assert "synthetic-read-token" not in result.stdout


# 2119: 2.16
@pytest.mark.parametrize("boundary", ["guard", "wait"])
def test_different_runtime_names_matching_installation_and_deliberate_stop_remedies(
    isolated_runtime, boundary: str
) -> None:
    action = runtime_module.guard_before_migration if boundary == "guard" else wait_until_ready
    with pytest.raises(RuntimeReadinessError) as raised:
        action(isolated_runtime, get=lambda *_: _response(200, _identity("another-runtime")))
    detail = str(raised.value)
    assert "different Panopticon runtime" in detail
    assert "preserve it" in detail
    assert "use the matching installation" in detail
    assert "stop it deliberately" in detail


# 2119: 4.5
@pytest.mark.parametrize(
    "defect", ["authentication", "service-kind", "runtime-identity", "api-revision"]
)
def test_each_single_identity_defect_fails_without_wait_or_runner_probe(
    isolated_runtime, defect: str
) -> None:
    body = _identity(isolated_runtime.instance_id)
    status = 200
    expected = {
        "authentication": "rejected the configured credential",
        "service-kind": "different service",
        "runtime-identity": "different Panopticon runtime",
        "api-revision": "revision 2 is unsupported",
    }[defect]
    if defect == "authentication":
        status = 401
    elif defect == "service-kind":
        body["service"] = "another-service"
    elif defect == "runtime-identity":
        body["instance_id"] = "another-runtime"
    else:
        body["api_revision"] = 2
    calls = []

    def request(path, _timeout):
        calls.append(path)
        assert path == "/identity"
        return _response(status, body)

    started = time.monotonic()
    with pytest.raises(RuntimeReadinessError, match=expected):
        wait_until_ready(
            isolated_runtime,
            timeout=30,
            get=request,
            monotonic=lambda: 0,
            sleep=lambda _: pytest.fail("permanent identity defect retried or slept"),
        )
    assert time.monotonic() - started < 0.5  # each isolated permanent defect fails immediately
    assert calls == ["/identity"]


# 2119: 5.2
@pytest.mark.parametrize("defect", ["unknown-kind", "unsupported-revision"])
def test_integrated_refusal_leaves_unknown_sessions_and_files_untouched(
    isolated_runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    body = _identity(isolated_runtime.instance_id)
    body["service" if defect == "unknown-kind" else "api_revision"] = (
        "foreign" if defect == "unknown-kind" else 2
    )
    preserved = tmp_path / "fleet-state"
    preserved.write_bytes(b"existing fleet state")
    before = preserved.read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail("incompatible fleet reached a mutation or process boundary")

    monkeypatch.setattr(runtime_module, "_default_get", lambda _: lambda *_: _response(200, body))
    monkeypatch.setattr(cli, "_run_migrate", forbidden)
    monkeypatch.setattr(cli, "_start_sessions_with_help", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    assert not cli._prepare_integrated_runtime(isolated_runtime.service_url, "panopticon start")
    assert preserved.read_bytes() == before


# 2119: 5.5
@pytest.mark.parametrize("compatible", [True, False])
def test_readiness_preserves_an_existing_claim_and_its_task_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compatible: bool
) -> None:
    with _live_authenticated_runtime(
        tmp_path, monkeypatch, advertised_instance_id=None if compatible else "another-runtime"
    ) as live:
        with httpx.Client(base_url=live.service_url, headers=live.headers, trust_env=False) as http:
            claimed = http.put(f"/tasks/{live.task.id}/claim", json={"runner_id": "existing-owner"})
            claimed.raise_for_status()
            before = http.get("/tasks").json()
            assert (
                next(task for task in before if task["id"] == live.task.id)["claimed_by"]
                == "existing-owner"
            )
            start = len(live.requests)
            if compatible:
                wait_until_ready(live.runtime, timeout=2)
            else:
                with pytest.raises(RuntimeReadinessError, match="different Panopticon runtime"):
                    wait_until_ready(live.runtime, timeout=2)
            requests = live.requests[start:]
            after = http.get("/tasks").json()
        assert before == after
        assert requests == (
            [("GET", "/identity"), ("GET", "/runners")] if compatible else [("GET", "/identity")]
        )


# 2119: 5.7
@pytest.mark.parametrize("compatible", [True, False])
def test_readiness_allows_only_local_service_http_and_no_provider_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compatible: bool
) -> None:
    with _live_authenticated_runtime(
        tmp_path, monkeypatch, advertised_instance_id=None if compatible else "another-runtime"
    ) as live:
        origin = urlsplit(live.service_url)
        endpoint = (origin.hostname, origin.port)
        real_popen = subprocess.Popen
        real_connect = socket.socket.connect
        helper_requests = []

        def forbidden(*_args, **_kwargs):
            pytest.fail("readiness escaped into a provider/process boundary")

        def local_connect(sock, address):
            assert address == endpoint, "readiness attempted a non-service socket"
            return real_connect(sock, address)

        # The production timeout-bounded HTTP helper is the sole permitted child. Audit its actual
        # network/process effects as well: a provider call added inside that child must fail too.
        audit = f"""
import sys
_allowed_endpoint = {endpoint!r}
def _check_readiness_io(event, args):
    if event == "socket.connect" and args[1] != _allowed_endpoint:
        raise RuntimeError("readiness child attempted non-service network")
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
        raise RuntimeError("readiness child attempted process execution")
sys.addaudithook(_check_readiness_io)
"""

        class LocalHttpChild:
            def __init__(self, argv, **kwargs):
                assert argv[:3] == [sys.executable, "-I", "-c"] and len(argv) == 4
                assert 'connection.request("GET"' in argv[3], "unexpected child executable"
                assert kwargs.get("env") == {}, "HTTP helper inherited provider credentials"
                self.child = real_popen([*argv[:3], audit + argv[3]], **kwargs)

            def communicate(self, payload=None, **kwargs):
                if payload is not None:
                    request = json.loads(payload)
                    assert request["origin"] == live.service_url
                    assert request["path"] in {"/identity", "/runners"}
                    helper_requests.append(request["path"])
                return self.child.communicate(payload, **kwargs)

            def __getattr__(self, name):
                return getattr(self.child, name)

        with monkeypatch.context() as guard:
            guard.setattr(subprocess, "Popen", LocalHttpChild)
            guard.setattr(subprocess, "run", forbidden)
            guard.setattr(os, "system", forbidden)
            guard.setattr(os, "execv", forbidden)
            guard.setattr(os, "execve", forbidden)
            guard.setattr(os, "posix_spawn", forbidden)
            guard.setattr(socket.socket, "connect", local_connect)
            guard.setattr(socket.socket, "connect_ex", forbidden)
            guard.setattr(socket.socket, "sendto", forbidden)
            guard.setattr(urllib.request, "urlopen", forbidden)
            guard.setattr(httpx.Client, "send", forbidden)
            guard.setattr(httpx.AsyncClient, "send", forbidden)
            if compatible:
                assert (
                    wait_until_ready(live.runtime, timeout=2)["instance_id"]
                    == live.runtime.instance_id
                )
            else:
                with pytest.raises(RuntimeReadinessError, match="different Panopticon runtime"):
                    wait_until_ready(live.runtime, timeout=2)
        assert helper_requests == (["/identity", "/runners"] if compatible else ["/identity"])
