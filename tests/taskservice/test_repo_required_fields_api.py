"""Repository writes reject missing required text without changing persisted data."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    service = TaskService(SqlAlchemyStore(), {"spike": Spike()}, FilesystemArtifactStore(tmp_path))
    asyncio.run(service.init())
    asyncio.run(
        service.create_repo(
            Repo(
                id="r1",
                name="widgets",
                git_url="/work/project with spaces",
                capabilities={"preserved": True},
                default_base="stable",
            )
        )
    )
    with TestClient(create_app(service)) as test_client:
        response = test_client.post("/tasks", json={"repo_id": "r1", "workflow": "spike"})
        assert response.status_code == 201, response.text
        yield test_client


@pytest.mark.parametrize("field", ["name", "git_url"])
@pytest.mark.parametrize("blank", ["", " ", "\t\n", "\u2003"])
def test_create_rejects_blank_required_fields_without_persisting(
    client: TestClient, field: str, blank: str
) -> None:
    before_repos = client.get("/repos").json()
    before_tasks = client.get("/tasks").json()
    body = {"id": "r2", "name": "new repo", "git_url": "https://example.invalid/new.git"}
    body[field] = blank

    response = client.post("/repos", json=body)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == f"{field} is required."
    assert client.get("/repos").json() == before_repos
    assert client.get("/repos/r2").status_code == 404
    assert client.get("/tasks").json() == before_tasks


@pytest.mark.parametrize("field", ["name", "git_url"])
@pytest.mark.parametrize("blank", [None, "", " ", "\t\n", "\u2003"])
def test_patch_rejects_blank_or_null_required_fields_atomically(
    client: TestClient, field: str, blank: str | None
) -> None:
    before_repos = client.get("/repos").json()
    before_tasks = client.get("/tasks").json()
    task_id = before_tasks[0]["id"]
    before_history = client.get(f"/tasks/{task_id}/history").json()

    response = client.patch(
        "/repos/r1",
        json={field: blank, "default_base": "changed", "capabilities": {"changed": True}},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == f"{field} is required."
    assert client.get("/repos").json() == before_repos
    assert client.get("/tasks").json() == before_tasks
    assert client.get(f"/tasks/{task_id}/history").json() == before_history


@pytest.mark.parametrize(
    "source",
    [
        "https://example.invalid/org/repo.git",
        "git@example.invalid:org/repo.git",
        "ssh://git@example.invalid/org/repo.git",
        "/work/project with spaces",
        "/work/bundle with spaces.bundle",
        "file:///work/project with spaces",
        "  /work/project with spaces  ",
    ],
)
def test_nonblank_sources_and_names_round_trip_without_normalization(
    client: TestClient, source: str
) -> None:
    name = "  Project name  "
    created = client.post("/repos", json={"id": "r2", "name": name, "git_url": source})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == name
    assert created.json()["git_url"] == source

    updated = client.patch("/repos/r1", json={"name": name, "git_url": source})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == name
    assert updated.json()["git_url"] == source
    assert updated.json()["capabilities"] == {"preserved": True}
    assert updated.json()["default_base"] == "stable"
    assert client.get("/repos/r1").json() == updated.json()

    partial = client.patch("/repos/r1", json={"default_base": "next"})
    assert partial.status_code == 200, partial.text
    assert partial.json()["name"] == name
    assert partial.json()["git_url"] == source
