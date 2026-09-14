"""An authenticated task may latch its own launcher failure, not administer runners."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.core.state import InitialState, TerminalState
from panopticon.core.workflow import Workflow
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import derive_task_capability
from panopticon.taskservice.auth_scope import AuthorizationClass
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Orchestrator, Spike

WRITE_TOKEN = "launcher-failure-fleet-write"
READ_TOKEN = "launcher-failure-fleet-read"
BODY = {"runner_id": "runner-1", "detail": "pi bootstrap failure: Permission denied"}
SCOPE_FAILURE = {"detail": "credential scope forbids operation"}


class _Archived(Workflow):
    name = "archived"

    class Active(InitialState):
        label = "ACTIVE"
        transitions = ("ARCHIVED",)

    class Archived(TerminalState):
        label = "ARCHIVED"

    initial = Active


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[TestClient, TaskServiceClient, TaskService]]:
    service = TaskService(
        SqlAlchemyStore(),
        {"spike": Spike(), "orchestrator": Orchestrator(), "archived": _Archived()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    asyncio.run(service.create_repo(Repo(id="repo", name="example", git_url="https://x/repo")))
    asyncio.run(service.register_runner("runner-1"))
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"write": [WRITE_TOKEN], "read": [READ_TOKEN]}))
    auth.chmod(0o600)
    with TestClient(
        create_app(service, auth_file=auth.name, auth_mode="enforced", secrets_dir=tmp_path)
    ) as http:
        yield http, TaskServiceClient(http, token=WRITE_TOKEN), service


def _task_headers(task_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {derive_task_capability(WRITE_TOKEN, task_id)}"}


# 2119: REQ-048.5.4
def test_task_failure_changes_only_its_failed_lifecycle(
    served: tuple[TestClient, TaskServiceClient, TaskService],
) -> None:
    http, fleet, service = served
    task_id = fleet.create_task("repo", "spike")["id"]
    fleet.claim(task_id, "runner-1")
    fleet.register(task_id, "container-1", runner_id="runner-1")
    before = asyncio.run(service.get_task(task_id))
    response = http.post(
        f"/tasks/{task_id}/launcher-failure", headers=_task_headers(task_id), json=BODY
    )
    assert response.status_code == 200
    assert response.json()["container_status"] == "failed"
    assert response.json()["lifecycle_detail"] == BODY["detail"]
    assert asyncio.run(service.get_task(task_id)) == before
    lifecycle = service.lifecycle(task_id)
    assert lifecycle is not None
    assert lifecycle.phase.value == "failed" and lifecycle.runner_id == "runner-1"
    assert len(service.registrations(task_id)) == 1
    policy = http.app.state.credential_scope_policy
    assert (
        policy.classification_for_rest("POST", "/tasks/{task_id}/launcher-failure")
        is AuthorizationClass.TASK_SCOPED
    )


# 2119: REQ-011.1.3
# 2119: REQ-048.5.4
def test_explicit_retry_clears_failure_after_the_container_deregisters(
    served: tuple[TestClient, TaskServiceClient, TaskService],
) -> None:
    http, fleet, service = served
    task_id = fleet.create_task("repo", "spike")["id"]
    fleet.claim(task_id, "runner-1")
    registration = fleet.register(task_id, "container-1", runner_id="runner-1")
    response = http.post(
        f"/tasks/{task_id}/launcher-failure", headers=_task_headers(task_id), json=BODY
    )
    assert response.status_code == 200
    assert response.json()["container_status"] == "failed"
    assert http.post(f"/tasks/{task_id}/retry").status_code == 409
    assert fleet.get_task(task_id)["container_status"] == "failed"
    fleet.deregister(registration["id"])
    retried = fleet.retry_task(task_id)
    assert retried["claimed_by"] is None
    assert retried["container_status"] == "queued"
    assert retried["lifecycle_detail"] is None
    assert service.lifecycle(task_id) is None
    fleet.claim(task_id, "runner-1")
    fleet.report_lifecycle(task_id, "runner-1", "starting")
    fleet.register(task_id, "container-2", runner_id="runner-1")
    assert fleet.get_task(task_id)["container_status"] == "live"


# 2119: REQ-048.5.4
# 2119: REQ-048.6.1
# 2119: REQ-048.6.3
# 2119: REQ-048.7.3
def test_scope_denies_other_missing_and_governed_tasks_before_body_validation(
    served: tuple[TestClient, TaskServiceClient, TaskService],
) -> None:
    http, fleet, service = served
    boss = fleet.create_task("repo", "orchestrator")["id"]
    child_response = http.post(
        "/tasks", json={"repo_id": "repo", "workflow": "spike", "governor_task_id": boss}
    )
    assert child_response.status_code == 201
    child = child_response.json()["id"]
    other = fleet.create_task("repo", "spike")["id"]
    for task_id in (boss, child, other):
        fleet.claim(task_id, "runner-1")
    version = service.tasks_version()
    for target in (child, other, "missing"):
        for body in (BODY, {"phase": "awaiting"}):
            response = http.post(
                f"/tasks/{target}/launcher-failure", headers=_task_headers(boss), json=body
            )
            assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)
    assert service.tasks_version() == version
    assert all(service.lifecycle(task_id) is None for task_id in (boss, child, other))


# 2119: REQ-048.5.4
@pytest.mark.parametrize("state", ["unclaimed", "other-runner", "COMPLETE", "DROPPED", "ARCHIVED"])
def test_task_failure_rejects_absent_or_mismatched_claims_and_terminal_workflows(
    served: tuple[TestClient, TaskServiceClient, TaskService], state: str
) -> None:
    http, fleet, service = served
    workflow = "archived" if state == "ARCHIVED" else "spike"
    task_id = fleet.create_task("repo", workflow)["id"]
    if state != "unclaimed":
        fleet.claim(task_id, "runner-2" if state == "other-runner" else "runner-1")
    if state not in {"unclaimed", "other-runner"}:
        fleet.set_state(task_id, state)
    before = asyncio.run(service.get_task(task_id))
    version = service.tasks_version()
    response = http.post(
        f"/tasks/{task_id}/launcher-failure", headers=_task_headers(task_id), json=BODY
    )
    assert response.status_code == 409
    assert asyncio.run(service.get_task(task_id)) == before
    assert service.tasks_version() == version
    assert service.lifecycle(task_id) is None


# 2119: REQ-048.5.4
@pytest.mark.parametrize(
    "change",
    [
        {"phase": "failed"},
        {"phase": "awaiting"},
        {"pause_launch": True},
        {"pause_launch": False},
        {"state": "COMPLETE"},
        {"claimed_by": "runner-2"},
        {"task_id": "other"},
        {"runner_id": ""},
        {"runner_id": " \t"},
        {"runner_id": None},
        {"runner_id": 1},
        {"detail": ""},
        {"detail": " \n"},
        {"detail": None},
        {"detail": 1},
        {"detail": "x" * 4097},
    ],
)
def test_task_failure_rejects_extra_control_fields_and_invalid_payloads(
    served: tuple[TestClient, TaskServiceClient, TaskService], change: dict[str, object]
) -> None:
    http, fleet, service = served
    task_id = fleet.create_task("repo", "spike")["id"]
    fleet.claim(task_id, "runner-1")
    version = service.tasks_version()
    response = http.post(
        f"/tasks/{task_id}/launcher-failure", headers=_task_headers(task_id), json=BODY | change
    )
    assert response.status_code == 422
    assert service.tasks_version() == version and service.lifecycle(task_id) is None


# 2119: REQ-048.5.4
def test_failure_detail_accepts_exactly_the_documented_limit(
    served: tuple[TestClient, TaskServiceClient, TaskService],
) -> None:
    http, fleet, _ = served
    task_id = fleet.create_task("repo", "spike")["id"]
    fleet.claim(task_id, "runner-1")
    response = http.post(
        f"/tasks/{task_id}/launcher-failure",
        headers=_task_headers(task_id),
        json=BODY | {"detail": "x" * 4096},
    )
    assert response.status_code == 200
    assert response.json()["lifecycle_detail"] == "x" * 4096


# 2119: REQ-048.6.2
def test_task_still_cannot_report_or_clear_general_runner_lifecycle(
    served: tuple[TestClient, TaskServiceClient, TaskService],
) -> None:
    http, fleet, service = served
    task_id = fleet.create_task("repo", "spike")["id"]
    fleet.claim(task_id, "runner-1")
    for phase in ("failed", "awaiting", "healing", "starting"):
        response = http.put(
            f"/tasks/{task_id}/lifecycle",
            headers=_task_headers(task_id),
            json=BODY | {"phase": phase},
        )
        assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)
    response = http.delete(f"/tasks/{task_id}/lifecycle", headers=_task_headers(task_id))
    assert (response.status_code, response.json()) == (403, SCOPE_FAILURE)
    assert service.lifecycle(task_id) is None
    assert fleet.report_lifecycle(task_id, "runner-1", "starting")["container_status"] == "starting"
    fleet.clear_lifecycle(task_id)
    assert service.lifecycle(task_id) is None


# 2119: REQ-035.4.1
# 2119: REQ-035.7.1
# 2119: REQ-048.5.4
@pytest.mark.parametrize("token", ["", "unknown-token", READ_TOKEN])
def test_failure_reporting_requires_a_write_or_task_capability(
    served: tuple[TestClient, TaskServiceClient, TaskService], token: str
) -> None:
    http, fleet, service = served
    task_id = fleet.create_task("repo", "spike")["id"]
    fleet.claim(task_id, "runner-1")
    response = http.post(
        f"/tasks/{task_id}/launcher-failure",
        headers={"Authorization": f"Bearer {token}"},
        json=BODY,
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert service.lifecycle(task_id) is None
