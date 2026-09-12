"""Launch the real task service on the port derived from its selected service URL."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from panopticon.terminal.runtime import resolve_runtime
from panopticon.terminal.session_environment import session_environment_argv

# 2119-spec: runtime-readiness


# 2119: 1.9
def test_real_service_binds_selected_url_port_without_a_port_flag(tmp_path: Path) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        selected_port = reservation.getsockname()[1]
    assert selected_port != 8000
    selected_url = f"http://127.0.0.1:{selected_port}"
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True, mode=0o700)
    credential = secrets / "service.json"
    credential.write_text('{"read": [], "write": ["synthetic-port-test-token"]}')
    credential.chmod(0o600)
    database = tmp_path / "isolated.sqlite"
    runtime = resolve_runtime(
        selected_url,
        environ={
            "HOME": str(home),
            "PATH": os.defpath,
            "PANOPTICON_CONFIG": str(config),
            "PANOPTICON_DATA": str(tmp_path / "data"),
            "PANOPTICON_CACHE": str(tmp_path / "cache"),
            "PANOPTICON_STATE": str(tmp_path / "state"),
            "PANOPTICON_DB": f"sqlite:///{database}",
            "PANOPTICON_HOST": "127.0.0.1",
            "PANOPTICON_PORT": "8000",  # URL selection must override a stale port input.
            "PANOPTICON_SERVICE_AUTH_FILE": credential.name,
            "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
        },
    )
    assert runtime.environment["PANOPTICON_PORT"] == str(selected_port)
    assert urlsplit(runtime.container_service_url).port == selected_port
    command = session_environment_argv(
        [sys.executable, "-m", "panopticon.taskservice"], environment=runtime.environment
    )
    assert "--port" not in command
    # Empty test CWD/home/config prevent legacy discovery or migration from touching a real fleet.
    log = tmp_path / "service-output.log"
    log.touch(mode=0o600)
    with log.open("wb") as output:
        child = subprocess.Popen(
            command,
            cwd=tmp_path,
            env={"PATH": os.defpath, "PANOPTICON_PORT": "8000"},
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=selected_url, trust_env=False, timeout=0.3) as http:
                deadline = time.monotonic() + 10
                while True:
                    assert child.poll() is None, log.read_text()
                    try:
                        response = http.get(
                            "/identity",
                            headers={"Authorization": "Bearer synthetic-port-test-token"},
                        )
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.03)
                assert response.request.url.port == selected_port
                identity = response.json()
                assert identity["service"] == "panopticon-task-service"
                assert identity["instance_id"] == runtime.instance_id
                assert http.get("/identity").status_code == 401
                assert database.is_file()
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    assert child.poll() is not None
