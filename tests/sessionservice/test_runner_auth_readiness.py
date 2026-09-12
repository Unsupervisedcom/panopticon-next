"""Credential transport and real REST failure-latch tests, without launching a provider."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.harnesses import claude
from panopticon.sessionservice.auth_readiness import missing_repo_auth, missing_task_auth
from panopticon.sessionservice.spawner import Spawner, spawnable_tasks
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

# 2119-spec: task-auth-readiness


@pytest.fixture
def private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    monkeypatch.setattr(
        claude, "_probe_status", lambda *_args, **_kwargs: pytest.fail("No provider probe")
    )
    return secrets


# 2119: 3.1
# 2119: 3.2
def test_host_credentials_do_not_make_a_task_ready(
    private_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-host-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-host-key")
    monkeypatch.setenv("PANOPTICON_CREDENTIALS", str(private_config))
    (private_config / "auth.json").write_text('{"tokens": {"access_token": "synthetic"}}')
    (private_config / "repo.env").write_text("# Empty file\nANTHROPIC_API_KEY\n")
    for harness in ("claude", "codex", "pi", "outfitter"):
        assert missing_repo_auth({"env_file": "repo.env"}, harness) is not None


# 2119: 3.6
@pytest.mark.parametrize(
    "harness,content",
    [
        ("claude", "ANTHROPIC_API_KEY=synthetic"),
        ("claude", "CLAUDE_CODE_OAUTH_TOKEN=synthetic"),
        ("claude", "ANTHROPIC_BASE_URL=https://gateway.example.test"),
        ("codex", "CODEX_API_KEY=synthetic"),
        ("codex", "OPENAI_API_KEY=synthetic"),
        ("codex", "CODEX_ACCESS_TOKEN=synthetic"),
        ("pi", "ANTHROPIC_API_KEY=synthetic"),
        ("outfitter", "OPENAI_API_KEY=synthetic"),
    ],
)
def test_supported_env_transports_are_presence_only(
    private_config: Path, harness: str, content: str
) -> None:
    (private_config / "repo.env").write_text(content + "\n")
    assert missing_repo_auth({"env_file": "repo.env"}, harness) is None


# 2119: 3.6
@pytest.mark.parametrize("harness", ["codex", "pi", "outfitter"])
def test_explicit_credential_directory_is_used(private_config: Path, harness: str) -> None:
    credentials = private_config / "account"
    target = credentials / "pi" / "agent" if harness == "pi" else credentials
    target.mkdir(parents=True)
    (target / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "synthetic"}})
    )
    assert missing_repo_auth({"credential_dir": "account"}, harness) is None
    (target / "auth.json").unlink()
    assert missing_repo_auth({"credential_dir": "account"}, harness) is not None


# 2119: 3.1
# 2119: 3.3
# 2119: 3.4
# 2119: 3.8
def test_missing_auth_latches_before_clone_and_only_explicit_retry_starts(
    private_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike()},
        FilesystemArtifactStore(private_config.parent / "artifacts"),
    )
    runner = SimpleNamespace(
        delete_workspace_contents=lambda _path: None,
        has_session=lambda _task: False,
        is_running=lambda _task: False,
    )
    with TestClient(create_app(service)) as http:
        client = TaskServiceClient(http)
        http.post(
            "/repos", json={"id": "repo", "name": "Example", "git_url": "https://example.test/repo"}
        ).raise_for_status()
        task = client.create_task("repo", "spike", initial_prompt="Keep this prompt")
        launched = []
        spawner = Spawner(
            client,
            runner,
            runner_id="runner",
            cache=object(),
            tasks_root=str(private_config.parent / "tasks"),
        )  # type: ignore[arg-type]
        monkeypatch.setattr(
            spawner,
            "_spawn_container",
            lambda task, _repo: launched.append(task["id"]) or "container",
        )
        with pytest.raises(RuntimeError, match="Connect claude"):
            spawner.spawn_one(task)
        failed = client.get_task(task["id"])
        assert failed["claimed_by"] == "runner"
        assert failed["launch_paused"]
        assert "Connect claude" in failed["lifecycle_detail"]
        assert launched == []
        assert not (private_config.parent / "tasks").exists()
        (private_config / "repo.env").write_text("ANTHROPIC_API_KEY=synthetic\n")
        client.update_repo("repo", env_file="repo.env")
        # A new runner instance has no private memory of the failure.
        replacement = Spawner(
            client,
            runner,
            runner_id="runner",
            cache=object(),
            tasks_root=str(private_config.parent / "tasks"),
        )  # type: ignore[arg-type]
        replacement.startup_reclaim([failed])
        assert replacement.heal(failed) is None
        assert replacement.spawn_one(failed) is None
        assert spawnable_tasks(client)() == []
        assert client.get_task(task["id"])["claimed_by"] == "runner"
        retried = client.retry_task(task["id"])
        assert retried["initial_prompt"] == "Keep this prompt"
        assert not retried["launch_paused"]
        assert spawner.spawn_one(retried) == "container"
        assert launched == [task["id"]]


# 2119: 3.5
def test_shell_setup_does_not_need_container_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = SimpleNamespace(delete_workspace_contents=lambda _path: None)
    client = SimpleNamespace(get_repo=lambda _id: {}, report_lifecycle=lambda *_args: None)
    spawner = Spawner(
        client,
        runner,
        runner_id="runner",
        cache=object(),
        tasks_root="unused",
        executions=SimpleNamespace(is_shell=lambda _workflow: True),
        credential_check=lambda *_args: pytest.fail("Shell auth was checked"),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(spawner, "_spawn_shell", lambda *_args: "shell")
    assert spawner._spawn({"id": "task", "repo_id": "repo", "workflow": "setup-repo"}) == "shell"


# 2119: 3.9
def test_resume_keeps_persistent_container_auth_path(private_config: Path) -> None:
    assert missing_task_auth({"harness": "codex", "clone": "/existing/task"}, {}) is None


# 2119: 3.7
def test_stale_candidate_reports_admission_rejection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    with TestClient(create_app(service)) as http:
        http.post(
            "/repos", json={"id": "repo", "name": "Example", "git_url": "https://example.test/repo"}
        ).raise_for_status()
        client = TaskServiceClient(http)
        task = client.create_task("repo", "spike")
        client.begin_repo_setup("repo")
        runner = SimpleNamespace(delete_workspace_contents=lambda _path: None)
        spawner = Spawner(client, runner, runner_id="runner", cache=object(), tasks_root="unused")  # type: ignore[arg-type]
        assert spawner.spawn_one(task) is None
        assert "claim rejected" in caplog.text
        assert "paused" in caplog.text
