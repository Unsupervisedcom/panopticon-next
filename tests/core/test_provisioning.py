"""Execute the provisioning skill's REST fallback against enforced task-scoped authorization."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from panopticon.core.models import Repo
from panopticon.core.provisioning import PROVISION_SKILL
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import derive_task_capability
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
def test_emitted_provision_command_authenticates_only_for_its_own_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    writer = "provision-test-writer-token"
    credential = secrets / "service.json"
    credential.write_text(json.dumps({"write": [writer]}))
    credential.chmod(0o600)
    service = TaskService(
        SqlAlchemyStore(f"sqlite:///{tmp_path / 'tasks.db'}"),
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(Repo(id="repo", name="repo", git_url="https://example.test/repo"))
    )
    own = asyncio.run(service.create_task("repo", "spike"))
    other = asyncio.run(service.create_task("repo", "spike"))
    token = derive_task_capability(writer, own.id)
    task_credential = tmp_path / "task-credential.json"
    task_credential.write_text(json.dumps({"task": token}))
    task_credential.chmod(0o600)
    command = PROVISION_SKILL.instructions.split("```sh\n", 1)[1].split("\n```", 1)[0]
    command = command.replace("<slug>", "test-provision-fallback")
    app = create_app(service, auth_file="service.json", auth_mode="enforced")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        env = {
            "HOME": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "PANOPTICON_CONFIG": str(tmp_path / "client-config"),
            "PANOPTICON_SERVICE_URL": f"http://127.0.0.1:{listener.getsockname()[1]}",
            "PANOPTICON_SERVICE_AUTH_FILE": str(task_credential),
        }
        for task_id, succeeds in [(own.id, True), (other.id, False)]:
            result = subprocess.run(
                ["sh", "-xc", command],
                env={**env, "PANOPTICON_TASK_ID": task_id},
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert (result.returncode == 0) is succeeds, result.stderr
            assert token not in command and writer not in command
            assert token not in result.stderr and writer not in result.stderr
            if not succeeds:
                assert "403 Forbidden" in result.stderr
        assert asyncio.run(service.get_task(own.id)).slug == "test-provision-fallback"
        assert asyncio.run(service.get_task(other.id)).slug is None
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
