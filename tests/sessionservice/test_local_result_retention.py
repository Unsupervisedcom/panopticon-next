"""Local Git results survive terminal cleanup; Docker and file-manager calls are injected."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.spawner import Spawner
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.terminal import dashboard
from panopticon.workflows import Spike
from panopticon.workflows.local_git_self_reviewed import LocalGitSelfReviewed

# 2119-spec: skip-terminal-provisioner


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir()
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "Test Author")
    git(path, "config", "user.email", "test@example.invalid")


def make_service(tmp_path: Path) -> TaskService:
    return TaskService(
        SqlAlchemyStore(),
        {"spike": Spike(), "local-git-self-reviewed": LocalGitSelfReviewed()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )


def make_spawner(client, tmp_path: Path, task_id: str, *, running: bool):
    calls = []

    def command(argv, **_kwargs):
        nonlocal running
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return f"panopticon-{task_id}" if running else ""
        if argv[:3] == ["docker", "rm", "--force"]:
            running = False
        return ""

    runner = LocalRunner("http://service.invalid", run=command, auth_file="")
    runner._snapshot_dir = tmp_path
    snapshot = tmp_path / f"panopticon-service-auth-{task_id}-synthetic.json"
    snapshot.write_text('{"synthetic":"runtime-only"}')
    spawner = Spawner(
        client, runner, runner_id="runner", cache=object(), tasks_root=str(tmp_path / "tasks")
    )
    return spawner, snapshot, calls


# 2119: 2.1
# 2119: 2.2
# 2119: 2.3
# 2119: 2.6
@pytest.mark.parametrize("source_kind", ["checkout", "bundle"])
@pytest.mark.parametrize("running", [True, False])
def test_completed_result_can_be_retrieved_then_disposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str, running: bool
) -> None:
    source = tmp_path / "source"
    init_repo(source)
    (source / "baseline.txt").write_text("Original source\n")
    git(source, "add", "baseline.txt")
    git(source, "commit", "--message", "Initial source")
    source_head = git(source, "rev-parse", "HEAD")
    source_refs = git(source, "show-ref")
    # Uncommitted source work is also outside the task's result ownership.
    (source / "uncommitted.txt").write_text("Leave this alone\n")
    source_status = git(source, "status", "--porcelain")
    bundle = tmp_path / "source.bundle"
    git(source, "bundle", "create", str(bundle), "--all")
    bundle_digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    selected = source if source_kind == "checkout" else bundle

    with TestClient(create_app(make_service(tmp_path))) as http:
        client = TaskServiceClient(http)
        client.create_repo(
            "repo", "Example", str(selected), enabled_workflows=["local-git-self-reviewed"]
        )
        task = client.create_task("repo", "local-git-self-reviewed")
        client.claim(task["id"], "runner")
        client.set_slug(task["id"], "result")
        checkout = tmp_path / "tasks" / task["id"]
        checkout.parent.mkdir()
        git(tmp_path, "clone", "--no-hardlinks", str(selected), str(checkout))
        git(checkout, "config", "user.name", "Test Author")
        git(checkout, "config", "user.email", "test@example.invalid")
        git(checkout, "checkout", "-b", "panopticon/result")
        (checkout / "result.txt").write_text("The completed task result\n")
        git(checkout, "add", "result.txt")
        git(checkout, "commit", "--message", "Implement result")
        git(checkout, "checkout", "main")
        git(
            checkout, "merge", "--no-ff", "--message", "Merge completed result", "panopticon/result"
        )
        result_head = git(checkout, "rev-parse", "HEAD")
        assert len(git(checkout, "rev-list", "--parents", "--max-count=1", "HEAD").split()) == 3
        client.record_provisioning(task["id"], "panopticon/result", str(checkout), "runner", True)
        completed = client.set_state(task["id"], "COMPLETE")
        spawner, snapshot, calls = make_spawner(client, tmp_path, task["id"], running=running)

        spawner.cleanup(completed)

        persisted = client.get_task(task["id"])
        assert persisted["clone"] == str(checkout)
        assert persisted["claimed_by"] is None
        assert git(checkout, "rev-parse", "HEAD") == result_head
        assert git(checkout, "show", "HEAD:result.txt") == "The completed task result"
        assert not snapshot.exists()
        assert (["docker", "rm", "--force", f"panopticon-{task['id']}"] in calls) == running
        if running:
            assert any("kill-session" in call for call in calls)

        opened = []
        monkeypatch.setattr(dashboard, "_open_path", opened.append)
        app = dashboard.Dashboard(client)
        app._current = task["id"]
        app._tasks = {task["id"]: persisted}
        app.action_open_checkout()
        assert opened == [str(checkout)]

        destination = tmp_path / "retrieved"
        init_repo(destination)
        git(destination, "fetch", str(checkout), "main")
        assert git(destination, "rev-parse", "FETCH_HEAD") == result_head
        assert git(destination, "show", "FETCH_HEAD:result.txt") == "The completed task result"
        shutil.rmtree(checkout)
        spawner.cleanup(persisted)
        spawner.cleanup(client.get_task(task["id"]))
        assert not checkout.exists()
        assert git(destination, "show", "FETCH_HEAD:result.txt") == "The completed task result"
        assert git(source, "rev-parse", "HEAD") == source_head
        assert git(source, "show-ref") == source_refs
        assert git(source, "status", "--porcelain") == source_status
        assert (source / "baseline.txt").read_text() == "Original source\n"
        assert (source / "uncommitted.txt").read_text() == "Leave this alone\n"
        assert hashlib.sha256(bundle.read_bytes()).hexdigest() == bundle_digest


# 2119: 2.3
# 2119: 2.4
# 2119: 2.5
@pytest.mark.parametrize(
    ("workflow", "state"), [("local-git-self-reviewed", "DROPPED"), ("spike", "COMPLETE")]
)
def test_disposable_workspaces_still_get_deleted(tmp_path: Path, workflow: str, state: str) -> None:
    with TestClient(create_app(make_service(tmp_path))) as http:
        client = TaskServiceClient(http)
        client.create_repo(
            "repo", "Example", "/unused", enabled_workflows=["local-git-self-reviewed"]
        )
        task = client.create_task("repo", workflow)
        client.claim(task["id"], "runner")
        checkout = tmp_path / "tasks" / task["id"]
        checkout.mkdir(parents=True)
        (checkout / "disposable.txt").write_text("Disposable work")
        terminal = client.set_state(task["id"], state)
        spawner, snapshot, calls = make_spawner(client, tmp_path, task["id"], running=True)
        spawner.cleanup(terminal)
        assert not checkout.exists()
        assert not snapshot.exists()
        assert client.get_task(task["id"])["claimed_by"] is None
        assert ["docker", "rm", "--force", f"panopticon-{task['id']}"] in calls
