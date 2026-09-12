"""Real service/store repair boundaries; no providers, containers, or operator credentials."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.core import Complete, InitialState, State, Workflow
from panopticon.core.models import Actor, LifecyclePhase, Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import scoped_task_token
from panopticon.taskservice.service import NotReady, TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

# 2119-spec: task-auth-readiness


class ProgressedWork(Workflow):
    name = "progressed-work"

    class Planning(InitialState):
        label = "PLANNING"
        transitions = ("WORKING",)

    class Working(State):
        label = "WORKING"
        transitions = (Complete,)

    initial = Planning


async def make_service(
    tmp_path: Path, *, persistent: bool = False
) -> tuple[TaskService, SqlAlchemyStore]:
    store = SqlAlchemyStore(f"sqlite:///{tmp_path / 'tasks.db'}" if persistent else "sqlite://")
    service = TaskService(
        store,
        {"spike": Spike(), "progressed-work": ProgressedWork()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    await service.init()
    if not await service.list_repos():
        await service.create_repo(
            Repo(id="repo", name="Example", git_url="https://example.test/repo")
        )
    return service, store


# 2119: 1.1
# 2119: 1.2
# 2119: 1.4
# 2119: 1.5
# 2119: 1.6
# 2119: 1.8
# 2119: 2.1
# 2119: 2.2
# 2119: 2.3
async def test_repair_preserves_live_work_and_requires_individual_retry(tmp_path: Path) -> None:
    service, store = await make_service(tmp_path)
    first = await service.create_task(
        "repo", "progressed-work", initial_prompt="Retain the requested work"
    )
    first = await service.apply_operation(first.id, "advance", note="Plan accepted")
    assert first.state == "WORKING" and len(first.history) == 2
    second = await service.create_task("repo", "spike")
    live = await service.create_task("repo", "spike")
    await service.claim(live.id, "runner")
    registration = await service.register(live.id, "container", "runner")
    before = await service.get_task(live.id)
    await service.begin_repo_setup("repo")
    during = await service.create_task("repo", "spike")
    for task in (first, second, during):
        assert (await service.get_task(task.id)).launch_paused
        with pytest.raises(NotReady, match="paused"):
            await service.claim(task.id, "runner")
        with pytest.raises(NotReady, match="Finish"):
            await service.retry_task(task.id)
    for blocked in (True, False):
        await service.set_blocked(first.id, blocked)
        current = await service.get_task(first.id)
        assert current.blocked is blocked
        assert current.launch_paused
    await service.set_turn(first.id, Actor.AGENT)
    assert (await service.get_task(first.id)).launch_paused
    assert await service.get_task(live.id) == before
    assert registration in service.registrations(live.id)
    await service.finish_repo_setup("repo")
    for task in (first, second, during):
        assert (await service.get_task(task.id)).launch_paused
    retried = await service.retry_task(first.id)
    assert not retried.launch_paused
    assert retried.initial_prompt == first.initial_prompt
    assert (retried.repo_id, retried.state, retried.history) == (
        first.repo_id,
        first.state,
        first.history,
    )
    await service.claim(first.id, "runner")
    assert (await service.get_task(second.id)).launch_paused
    assert (await service.get_task(live.id)).claimed_by == "runner"
    assert registration in service.registrations(live.id)
    with pytest.raises(NotReady):
        await service.retry_task(live.id)
    await store.close()


# 2119: 1.1
# 2119: 1.3
@pytest.mark.parametrize("claim_first", [True, False])
async def test_claim_and_repair_cannot_both_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claim_first: bool
) -> None:
    service, store = await make_service(tmp_path)
    task = await service.create_task("repo", "spike")
    entered, proceed = asyncio.Event(), asyncio.Event()
    if claim_first:
        original = store.save_task

        async def delay_claim(value):
            entered.set()
            await proceed.wait()
            await original(value)

        monkeypatch.setattr(store, "save_task", delay_claim)
        winner = asyncio.create_task(service.claim(task.id, "runner"))
        await entered.wait()
        loser = asyncio.create_task(service.begin_repo_setup("repo"))
    else:
        original_pause = store.set_repo_launch_pause

        async def delay_pause(repo_id, paused, task_ids=()):
            entered.set()
            await proceed.wait()
            await original_pause(repo_id, paused, task_ids)

        monkeypatch.setattr(store, "set_repo_launch_pause", delay_pause)
        winner = asyncio.create_task(service.begin_repo_setup("repo"))
        await entered.wait()
        loser = asyncio.create_task(service.claim(task.id, "runner"))
    await asyncio.sleep(0)
    assert not loser.done()
    proceed.set()
    await winner
    with pytest.raises(NotReady):
        await loser
    assert (await service.get_repo("repo")).launch_paused is (not claim_first)
    assert (await service.get_task(task.id)).launch_paused is (not claim_first)
    await store.close()


# 2119: 1.7
# 2119: 3.4
async def test_auth_failure_and_repair_holds_survive_service_restart(tmp_path: Path) -> None:
    service, store = await make_service(tmp_path, persistent=True)
    task = await service.create_task("repo", "spike")
    await service.claim(task.id, "runner")
    await service.report_lifecycle(
        task.id, "runner", LifecyclePhase.FAILED, "Connect Claude.", pause_launch=True
    )
    await service.begin_repo_setup("repo")
    await store.close()
    restarted, reopened = await make_service(tmp_path, persistent=True)
    assert (await restarted.get_repo("repo")).launch_paused
    restored = await restarted.get_task(task.id)
    assert restored.launch_paused
    assert restored.claimed_by == "runner"
    assert restarted.container_status(restored).value == "paused"
    await restarted.begin_repo_setup("repo")  # a crashed setup can be resumed
    await restarted.finish_repo_setup("repo")
    with pytest.raises(NotReady):
        await restarted.claim(task.id, "runner")
    await restarted.retry_task(task.id)
    assert (await restarted.claim(task.id, "runner")).claimed_by == "runner"
    await reopened.close()


# 2119: 1.8
async def test_stale_content_write_cannot_clear_execution_hold(tmp_path: Path) -> None:
    service, store = await make_service(tmp_path)
    task = await service.create_task("repo", "spike")
    stale = await service.get_task(task.id)
    await service.begin_repo_setup("repo")
    stale.blocked = True
    await store.save_task(stale)
    assert (await service.get_task(task.id)).launch_paused
    await store.close()


# 2119: 2.5
async def test_retry_refuses_claim_before_first_lifecycle_report(tmp_path: Path) -> None:
    service, store = await make_service(tmp_path)
    task = await service.create_task("repo", "spike")
    await service.claim(task.id, "runner")
    with pytest.raises(NotReady, match="current launch"):
        await service.retry_task(task.id)
    assert (await service.get_task(task.id)).claimed_by == "runner"
    await store.close()


# 2119: 2.1
# 2119: 2.5
@pytest.mark.parametrize(
    "condition,allowed",
    [
        ("unclaimed", True),
        ("paused", True),
        ("failed", True),
        ("terminal", False),
        ("live", False),
        ("repair", False),
    ]
    + [(phase.value, False) for phase in LifecyclePhase if phase is not LifecyclePhase.FAILED],
)
async def test_retry_admission_and_unrelated_claim_preservation(
    tmp_path: Path, condition: str, allowed: bool
) -> None:
    service, store = await make_service(tmp_path)
    task = await service.create_task("repo", "spike")
    other = await service.create_task("repo", "spike")
    await service.claim(other.id, "other-runner")
    registration = await service.register(other.id, "other-container", "other-runner")
    if condition in {"paused", "repair"}:
        await service.begin_repo_setup("repo")
        if condition == "paused":
            await service.finish_repo_setup("repo")
    elif condition == "terminal":
        await service.set_state(task.id, "COMPLETE")
    elif condition != "unclaimed":
        await service.claim(task.id, "runner")
        if condition == "live":
            await service.register(task.id, "container", "runner")
        else:
            await service.report_lifecycle(task.id, "runner", LifecyclePhase(condition))
    before = await service.get_task(task.id)
    if allowed:
        result = await service.retry_task(task.id)
        assert result.claimed_by is None and not result.launch_paused
    else:
        with pytest.raises(NotReady):
            await service.retry_task(task.id)
        assert await service.get_task(task.id) == before
    assert (await service.get_task(other.id)).claimed_by == "other-runner"
    assert registration in service.registrations(other.id)
    await store.close()


# 2119: 4.1
def test_repair_and_retry_require_fleet_write_authority(tmp_path: Path) -> None:
    service, store = asyncio.run(make_service(tmp_path))
    task = asyncio.run(service.create_task("repo", "spike"))
    credentials = tmp_path / "auth.json"
    credentials.write_text(
        json.dumps({"read": ["synthetic-reader"], "write": ["synthetic-writer"]})
    )
    credentials.chmod(0o600)
    with TestClient(
        create_app(service, auth_mode="enforced", auth_file="auth.json", secrets_dir=tmp_path)
    ) as http:
        assert (
            http.get("/repos", headers={"Authorization": "Bearer synthetic-reader"}).status_code
            == 200
        )
        task_token = scoped_task_token("synthetic-writer", task.id)
        assert (
            http.get(
                f"/tasks/{task.id}", headers={"Authorization": f"Bearer {task_token}"}
            ).status_code
            == 200
        )
        for token, denied in (("synthetic-reader", 401), (task_token, 403)):
            for path in (
                "/repos/repo/setup/begin",
                "/repos/repo/setup/finish",
                f"/tasks/{task.id}/retry",
            ):
                assert (
                    http.post(path, headers={"Authorization": f"Bearer {token}"}).status_code
                    == denied
                )
        writer = {"Authorization": "Bearer synthetic-writer"}
        assert http.post("/repos/repo/setup/begin", headers=writer).status_code == 200
        assert http.post("/repos/repo/setup/finish", headers=writer).status_code == 200
        assert http.post(f"/tasks/{task.id}/retry", headers=writer).status_code == 200
    asyncio.run(store.close())


# 2119: 1.3
@pytest.mark.parametrize("phase", [None, LifecyclePhase.STARTING])
async def test_setup_refusal_explains_ambiguous_claim_and_safe_recovery(
    tmp_path: Path, phase: LifecyclePhase | None
) -> None:
    service, store = await make_service(tmp_path)
    task = await service.create_task("repo", "spike")
    await service.claim(task.id, "possibly-disconnected-runner")
    if phase is not None:
        await service.report_lifecycle(task.id, "possibly-disconnected-runner", phase)
    with pytest.raises(NotReady) as error:
        await service.begin_repo_setup("repo")
    detail = str(error.value)
    assert task.id in detail and "possibly-disconnected-runner" in detail
    assert "without a live registration or a recorded launch failure" in detail
    assert "cannot tell whether that launch is still active" in detail
    assert "confirm the task is stopped and use dashboard R" in detail
    assert "resume repository setup" in detail
    assert not (await service.get_repo("repo")).launch_paused
    unchanged = await service.get_task(task.id)
    assert unchanged.claimed_by == "possibly-disconnected-runner" and not unchanged.launch_paused
    await store.close()
