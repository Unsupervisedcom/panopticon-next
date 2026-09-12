"""Observe a newly eligible task's first prompt in a real pane, without Docker or an LLM."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawner import Spawner, spawnable_tasks
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.fixture
def socket_root():
    # Unix-domain socket paths have a small platform limit; pytest's macOS tmp_path is too long.
    with tempfile.TemporaryDirectory(prefix="pn-pane-", dir="/tmp") as directory:
        yield Path(directory)


# 2119: REQ-026.1.3
@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_dependency_completion_delivers_initial_prompt_to_actual_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_root: Path
) -> None:
    # Keep the real pane/server wholly separate from the operator's tmux sockets and config.
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TMUX", raising=False)
    socket = "delivery-" + uuid.uuid4().hex[:8]
    prefix = ["tmux", "-L", socket]
    home = tmp_path / "agent-home"
    home.mkdir()
    bootstrap = tmp_path / "bootstrap.py"
    # The actual container entry function and Codex argv renderer run in the pane. Only service
    # fetching and the provider executable are replaced; the latter prints its received argv.
    bootstrap.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from panopticon.container.agent import main\n"
        "client = SimpleNamespace(list_skills=lambda _: [], list_operations=lambda _: {}, "
        "workflow_overview=lambda _: 'Test task', "
        "report_lifecycle=lambda *a, **kw: print('UNEXPECTED_FAILURE', a, kw))\n"
        "def launch(harness, context):\n"
        "    argv = harness.argv(context)\n"
        "    receiver = 'import json,sys; print(\"PANE_RECEIVED=\" + json.dumps(sys.argv[1:]), flush=True)'\n"
        "    return subprocess.run([sys.executable, '-c', receiver, *argv[1:]], check=True).returncode\n"
        f"main(client_factory=lambda _: client, home=Path({str(home)!r}), launch=launch, on_exit=lambda: None)\n"
        "time.sleep(30)\n"
    )
    docker_env: dict[str, str] = {}

    def run(argv, *, check=True):
        args = list(argv)
        if args[:2] == ["docker", "run"]:
            docker_env.clear()
            for i, word in enumerate(args[:-1]):
                if word == "--env":
                    key, value = args[i + 1].split("=", 1)
                    docker_env[key] = value
            return "synthetic-container"
        if args[0] == "docker":
            assert args[1] in {"rm", "ps"}, args
            return ""
        if "respawn-pane" in args:
            docker_index = args.index("docker")
            # Substitute just the Docker exec boundary. Everything through LocalRunner's emitted
            # environment and the real container/harness first-prompt handling stays in the path.
            assert args[docker_index : docker_index + 2] == ["docker", "exec"]
            env_argv = ["env", *(f"{k}={v}" for k, v in docker_env.items())]
            env_argv += ["CODEX_API_KEY=synthetic-no-provider", sys.executable, str(bootstrap)]
            args = [*args[:docker_index], shlex.join(env_argv)]
        return subprocess.run(args, check=check, capture_output=True, text=True).stdout

    service = TaskService(
        SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path / "artifacts")
    )
    try:
        with TestClient(create_app(service)) as http:
            client = TaskServiceClient(http)
            client.create_repo("repo", "Example", "/unused")
            dependency = client.create_task("repo", "spike")
            prompt = "Synthesize the audit results; keep 'quoted' details."
            dependent = client.create_task("repo", "spike", initial_prompt=prompt, harness="codex")
            client.set_dependencies(dependent["id"], [dependency["id"]])
            runner = LocalRunner(
                "http://service.invalid", run=run, tmux_socket=socket, auth_file=""
            )
            spawner = Spawner(
                client,
                runner,
                runner_id="runner",
                cache=object(),
                tasks_root=str(tmp_path / "tasks"),
                images=SimpleNamespace(
                    build_base_if_missing=lambda **_: False, build=lambda *_a, **_k: "fake-image"
                ),
                credential_check=lambda *_: None,
            )
            monkeypatch.setattr(spawner, "_prepare_task_dir", lambda *_a, **_k: str(tmp_path))
            assert dependent["id"] not in {task["id"] for task in spawnable_tasks(client)()}
            client.set_state(dependency["id"], "COMPLETE")
            ready = {task["id"]: task for task in spawnable_tasks(client)()}
            session = spawner.spawn_one(ready[dependent["id"]])
            assert client.get_task(dependent["id"])["claimed_by"] == "runner"
            captured = ""
            for _ in range(100):
                captured = run([*prefix, "capture-pane", "-p", "-J", "-t", session])
                if "PANE_RECEIVED=" in captured:
                    break
                time.sleep(0.03)
            line = next(
                (line for line in captured.splitlines() if line.startswith("PANE_RECEIVED=")), None
            )
            assert line is not None, captured
            delivered_argv = json.loads(line.removeprefix("PANE_RECEIVED="))
            assert delivered_argv[-1] == prompt
            assert delivered_argv.count(prompt) == 1
    finally:
        subprocess.run([*prefix, "kill-server"], capture_output=True, env=os.environ)
