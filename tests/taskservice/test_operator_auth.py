"""Operator authorization survives process isolation without entering task credentials."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.core.models import Repo
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.auth import derive_task_capability
from panopticon.taskservice.operator_auth import (
    OPERATOR_AUTH_ENV,
    OPERATOR_AUTH_FILE,
    operator_token,
    persist_operator_token,
)
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.terminal.runtime import resolve_runtime
from panopticon.terminal.session_environment import session_environment_argv
from panopticon.workflows import Spike

TOKEN = "isolated-operator-test-credential"


def _file_state(path: Path) -> tuple[bytes, tuple[int, ...]]:
    contents = path.read_bytes()
    info = path.lstat()
    # Reading may update atime; preserve all identity, permission, and modification checks.
    return contents, (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "config"
    monkeypatch.setenv("PANOPTICON_CONFIG", str(root))
    monkeypatch.delenv("PANOPTICON_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv(OPERATOR_AUTH_ENV, raising=False)
    return root


def test_no_operator_configuration_remains_absent(config: Path) -> None:
    persist_operator_token(allow_create=True)
    assert operator_token() is None
    assert not config.exists()


def test_explicit_token_survives_actual_child_boundary_without_secret_in_argv(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_OPERATOR_TOKEN", TOKEN)
    runtime = resolve_runtime("http://127.0.0.1:8000")
    assert runtime.environment[OPERATOR_AUTH_ENV] == OPERATOR_AUTH_FILE
    assert not config.exists(), "resolution must stay read-only"
    persist_operator_token(allow_create=True)
    path = config / "secrets" / OPERATOR_AUTH_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_uid == os.geteuid()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    script = (
        "import os; from panopticon.taskservice.operator_auth import operator_token; "
        "assert 'PANOPTICON_OPERATOR_TOKEN' not in os.environ; "
        "assert operator_token(); print('operator available')"
    )
    argv = session_environment_argv([sys.executable, "-c", script], environment=runtime.environment)
    assert TOKEN not in repr(argv)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "operator available"
    assert TOKEN not in result.stdout + result.stderr
    monkeypatch.delenv("PANOPTICON_OPERATOR_TOKEN")
    assert operator_token() == TOKEN
    assert resolve_runtime("http://127.0.0.1:8000").instance_id == runtime.instance_id


def test_live_reuse_and_rotation_never_overwrite_existing_operator_file(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_OPERATOR_TOKEN", TOKEN)
    with pytest.raises(ValueError, match="Stop the running service"):
        persist_operator_token(allow_create=False)
    assert not config.exists()
    persist_operator_token(allow_create=True)
    path = config / "secrets" / OPERATOR_AUTH_FILE
    before = _file_state(path)
    persist_operator_token(allow_create=False)
    assert _file_state(path) == before
    monkeypatch.setenv("PANOPTICON_OPERATOR_TOKEN", "different-operator-test")
    for allow_create in (False, True):
        with pytest.raises(ValueError, match="differs") as failure:
            persist_operator_token(allow_create=allow_create)
        assert TOKEN not in str(failure.value)
        assert "different-operator-test" not in str(failure.value)
        assert _file_state(path) == before


@pytest.mark.parametrize("unsafe", ["symlink", "public", "malformed"])
def test_operator_file_rejects_unsafe_input_without_disclosure_or_mutation(
    config: Path, tmp_path: Path, unsafe: str
) -> None:
    path = config / "secrets" / OPERATOR_AUTH_FILE
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(json.dumps({"token": TOKEN}) if unsafe != "malformed" else TOKEN)
    path.chmod(0o644 if unsafe == "public" else 0o600)
    if unsafe == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    before = _file_state(path)
    with pytest.raises(ValueError) as failure:
        operator_token()
    assert TOKEN not in str(failure.value)
    assert _file_state(path) == before


def test_file_backed_client_and_service_require_distinct_operator_and_fleet_authority(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_OPERATOR_TOKEN", TOKEN)
    persist_operator_token(allow_create=True)
    monkeypatch.delenv("PANOPTICON_OPERATOR_TOKEN")
    root = config / "secrets"
    auth = root / "service.json"
    auth.write_text(json.dumps({"read": ["fleet-read-test"], "write": ["fleet-write-test"]}))
    auth.chmod(0o600)
    store = SqlAlchemyStore()
    service = TaskService(
        store, {"spike": Spike()}, FilesystemArtifactStore(tmp_path / "artifacts")
    )

    async def initialize():
        await service.init()
        await service.create_repo(Repo(id="r", name="Example", git_url="https://example.test/r"))
        task = await service.create_task("r", "spike")
        await service.set_slug(task.id, "move")
        await service.claim(task.id, "source")
        await service.record_provisioning(
            task.id, "panopticon/move", "/source/task", "source", True
        )
        await service.release(task.id)
        return task.id

    task_id = asyncio.run(initialize())
    body = {
        "source_runner": "source",
        "destination_runner": "destination",
        "workspace_disposition": "pending",
        "session_history_disposition": "omitted",
        "discarded_changes": [],
        "discard_authorized_by": None,
    }
    with TestClient(create_app(service, auth_file="service.json", secrets_dir=root)) as http:
        response = http.put(
            f"/tasks/{task_id}/migration",
            json=body,
            headers={"Authorization": "Bearer fleet-write-test"},
        )
        assert response.status_code == 403
        response = http.put(
            f"/tasks/{task_id}/migration",
            json=body,
            headers={
                "Authorization": f"Bearer {derive_task_capability('fleet-write-test', task_id)}",
                "X-Panopticon-Operator-Token": TOKEN,
            },
        )
        assert response.status_code == 403
        client = TaskServiceClient(http, token="fleet-write-test")
        result = client.record_migration(task_id, **body)
        assert result["migration"]["destination_runner"] == "destination"
    asyncio.run(store.close())
