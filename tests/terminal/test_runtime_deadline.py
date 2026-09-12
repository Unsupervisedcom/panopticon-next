"""Real transport evidence for the runtime readiness request deadline."""

# 2119-spec: runtime-readiness
from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from panopticon.terminal.runtime import (
    RuntimeReadinessError,
    guard_before_migration,
    resolve_runtime,
    wait_until_ready,
)


class _RuntimeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        body: object
        slow = False
        if self.path == "/identity":
            body = {
                "service": "panopticon-task-service",
                "api_revision": 1,
                "version": "deadline-test",
                "instance_id": server.instance_id,  # type: ignore[attr-defined]
            }
            slow = server.slow_path == self.path  # type: ignore[attr-defined]
        elif self.path == "/runners":
            body = [
                {
                    "id": "local",
                    "host": None,
                    "instance_id": server.instance_id,  # type: ignore[attr-defined]
                }
            ]
            slow = server.slow_path == self.path  # type: ignore[attr-defined]
        else:
            self.send_error(404)
            return

        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            if slow:
                for byte in payload:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(0.005)
            else:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            server.client_disconnected.set()  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextmanager
def _runtime_server(*, slow_path: str | None) -> Iterator[tuple[Any, str]]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RuntimeHandler)
    server.daemon_threads = True
    server.slow_path = slow_path  # type: ignore[attr-defined]
    server.instance_id = "pending"  # type: ignore[attr-defined]
    server.client_disconnected = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="runtime-deadline-server")
    thread.start()
    service_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield server, service_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _forbid_agent_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.container import agent

    monkeypatch.setattr(
        agent,
        "_run_agent",
        lambda *_args, **_kwargs: pytest.fail("runtime readiness invoked an agent provider"),
    )


# 2119: 4.1, 4.11
def test_identity_slow_drip_cannot_extend_migration_probe_deadline() -> None:
    with _runtime_server(slow_path="/identity") as (server, service_url):
        runtime = resolve_runtime(service_url, environ={"HOME": "/tmp", "PATH": "/bin"})
        server.instance_id = runtime.instance_id
        started = time.monotonic()
        with pytest.raises(RuntimeReadinessError, match=r"did not answer.*migration was not run"):
            guard_before_migration(runtime, request_timeout=0.08)
        elapsed = time.monotonic() - started

        assert elapsed < 0.3
        assert server.client_disconnected.wait(0.3)


# 2119: 4.2, 4.9, 4.11
def test_runner_slow_drip_cannot_extend_overall_readiness_deadline() -> None:
    with _runtime_server(slow_path="/runners") as (server, service_url):
        runtime = resolve_runtime(service_url, environ={"HOME": "/tmp", "PATH": "/bin"})
        server.instance_id = runtime.instance_id
        started = time.monotonic()
        with pytest.raises(RuntimeReadinessError, match=r"runner 'local'.*runner\.log"):
            wait_until_ready(
                runtime,
                timeout=0.16,
                interval=0.01,
                request_timeout=0.08,
            )
        elapsed = time.monotonic() - started

        assert elapsed < 0.4
        assert server.client_disconnected.wait(0.3)


# 2119: 4.1, 4.2, 4.3, 4.11
def test_total_request_deadline_retains_healthy_identity_and_runner_path() -> None:
    with _runtime_server(slow_path=None) as (server, service_url):
        runtime = resolve_runtime(service_url, environ={"HOME": "/tmp", "PATH": "/bin"})
        server.instance_id = runtime.instance_id

        identity = wait_until_ready(runtime, timeout=0.5, request_timeout=0.2)

        assert identity == {
            "service": "panopticon-task-service",
            "api_revision": 1,
            "version": "deadline-test",
            "instance_id": runtime.instance_id,
        }
