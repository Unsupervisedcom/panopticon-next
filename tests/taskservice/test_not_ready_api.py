"""HTTP conflict mapping for expected task-service admission refusals."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from panopticon.core.models import LifecyclePhase, Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import scoped_task_token
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

READ_TOKEN = "synthetic-reader-token-value"
WRITE_TOKEN = "synthetic-writer-token-value"


async def _service(tmp_path: Path) -> tuple[TaskService, SqlAlchemyStore]:
    store = SqlAlchemyStore("sqlite://")
    service = TaskService(
        store,
        {"spike": Spike()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    await service.init()
    await service.create_repo(Repo(id="repo", name="Example", git_url="https://example.test/repo"))
    return service, store


def _client(tmp_path: Path, service: TaskService) -> TestClient:
    credentials = tmp_path / "auth.json"
    credentials.write_text(json.dumps({"read": [READ_TOKEN], "write": [WRITE_TOKEN]}))
    credentials.chmod(0o600)
    return TestClient(
        create_app(service, auth_mode="enforced", auth_file="auth.json", secrets_dir=tmp_path)
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# 2119: task-auth-readiness.2.2
def test_retry_during_repository_setup_is_structured_conflict_without_mutation(
    tmp_path: Path,
) -> None:
    service, store = asyncio.run(_service(tmp_path))
    task = asyncio.run(service.create_task("repo", "spike"))
    asyncio.run(service.claim(task.id, "runner"))
    asyncio.run(
        service.report_lifecycle(
            task.id,
            "runner",
            LifecyclePhase.FAILED,
            "credential repair required",
            pause_launch=True,
        )
    )

    with _client(tmp_path, service) as http:
        writer = _auth(WRITE_TOKEN)
        assert http.post("/repos/repo/setup/begin", headers=writer).status_code == 200
        task_before = http.get(f"/tasks/{task.id}", headers=writer).json()
        repo_before = http.get("/repos/repo", headers=writer).json()

        for headers, expected_status in (
            ({}, 401),
            (_auth(READ_TOKEN), 401),
            (_auth(scoped_task_token(WRITE_TOKEN, task.id)), 403),
        ):
            response = http.post(f"/tasks/{task.id}/retry", headers=headers)
            assert response.status_code == expected_status

        response = http.post(f"/tasks/{task.id}/retry", headers=writer)

        assert response.status_code == 409
        assert response.json() == {"detail": "Finish repository setup before retrying this task."}
        assert http.get(f"/tasks/{task.id}", headers=writer).json() == task_before
        assert http.get("/repos/repo", headers=writer).json() == repo_before
        assert task_before["claimed_by"] == "runner"
        assert task_before["launch_paused"] is True
        assert repo_before["launch_paused"] is True
    asyncio.run(store.close())


# 2119: task-auth-readiness.1.3
def test_begin_setup_admission_refusal_is_structured_conflict_without_mutation(
    tmp_path: Path,
) -> None:
    service, store = asyncio.run(_service(tmp_path))
    task = asyncio.run(service.create_task("repo", "spike"))
    asyncio.run(service.claim(task.id, "possibly-active-runner"))

    with _client(tmp_path, service) as http:
        writer = _auth(WRITE_TOKEN)
        task_before = http.get(f"/tasks/{task.id}", headers=writer).json()
        repo_before = http.get("/repos/repo", headers=writer).json()

        response = http.post("/repos/repo/setup/begin", headers=writer)

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert task.id in detail
        assert "possibly-active-runner" in detail
        assert "cannot tell whether that launch is still active" in detail
        assert http.get(f"/tasks/{task.id}", headers=writer).json() == task_before
        assert http.get("/repos/repo", headers=writer).json() == repo_before
        assert task_before["claimed_by"] == "possibly-active-runner"
        assert task_before["launch_paused"] is False
        assert repo_before["launch_paused"] is False
    asyncio.run(store.close())
