"""Foreground repair through the real REST client, service and SQLite adapter."""

# 2119-spec: foreground-setup
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.terminal.setup import configure_repo, repo_configured
from panopticon.terminal.setup_credentials import (
    Connection,
    connect,
    env_values,
    read_private,
    write_private,
)
from panopticon.workflows import SetupRepo, Spike


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TaskServiceClient]:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path / "config"))
    # Runtime identity is covered separately; these tests exercise real repair REST/store writes.
    monkeypatch.setattr("panopticon.terminal.setup.require_local_runtime", lambda _: None)
    store = SqlAlchemyStore()
    service = TaskService(
        store,
        {"spike": Spike(), "setup-repo": SetupRepo()},
        FilesystemArtifactStore(tmp_path / "artifacts"),
    )
    asyncio.run(service.init())
    with TestClient(create_app(service)) as http:
        yield TaskServiceClient(http)
    asyncio.run(store.close())


def saved_connection() -> Connection:
    return connect("claude", secret_fn=lambda _: "new-provider-test")


# 2119: 2.2, 2.5, 2.6, 2.7, 2.11, 4.1, 4.2, 4.4
# 2119: runtime-readiness.4.15
def test_repair_copies_only_selected_connection_and_holds_backlog(
    client: TaskServiceClient,
) -> None:
    original = (
        "# retained\nGH_TOKEN=repo-one-test\nCUSTOM=literal=value\nANTHROPIC_API_KEY=old-test\n"
    )
    write_private("shared.env", original)
    for repo in ("one", "two"):
        client.create_repo(repo, repo, f"https://github.com/example/{repo}", env_file="shared.env")
    pending = client.create_task("one", "spike", memo="keep this work")
    legacy = client.create_task("one", "setup-repo")
    connection = saved_connection()
    assert configure_repo(client, "one", connection=connection, input_fn=lambda _: "y")
    one, two = client.get_repo("one"), client.get_repo("two")
    assert one["env_file"] != "shared.env"
    assert two["env_file"] == "shared.env"
    assert read_private("shared.env") == original
    values = env_values(read_private(one["env_file"]))
    assert values == {
        "GH_TOKEN": "repo-one-test",
        "CUSTOM": "literal=value",
        "CLAUDE_CODE_OAUTH_TOKEN": "new-provider-test",
    }
    assert "GH_TOKEN" not in read_private(connection.env_file)
    assert client.get_task(legacy["id"])["state"] == legacy["state"]
    task = client.get_task(pending["id"])
    assert task["launch_paused"]
    assert task["memo"] == "keep this work"
    assert not one["launch_paused"]
    client.retry_task(pending["id"])
    assert not client.get_task(pending["id"])["launch_paused"]
    assert client.get_task(legacy["id"])["launch_paused"]


# 2119: 2.3, 2.4, 2.9
def test_explicit_incomplete_binding_does_not_fall_back_to_saved_or_host_auth(
    client: TaskServiceClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_private("empty.env", "# no credential\n")
    client.create_repo("one", "one", "https://example.test/one", env_file="empty.env")
    saved_connection()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-test")
    assert not repo_configured(client.get_repo("one"))
    assert not configure_repo(client, "one", input_fn=lambda _: "n")
    assert client.get_repo("one")["env_file"] == "empty.env"
    assert read_private("empty.env") == "# no credential\n"


# 2119: 2.3, 2.7, 4.5
def test_keep_explicit_agent_and_add_repo_only_forge_token(client: TaskServiceClient) -> None:
    write_private("old.env", "ANTHROPIC_API_KEY=keep-test\n")
    client.create_repo("one", "one", "https://github.com/example/one", env_file="old.env")
    candidate = saved_connection()
    assert configure_repo(
        client,
        "one",
        connection=candidate,
        input_fn=lambda _: "n",
        secret_fn=lambda _: "scoped-forge-test",
    )
    repo = client.get_repo("one")
    assert env_values(read_private(repo["env_file"])) == {
        "ANTHROPIC_API_KEY": "keep-test",
        "GH_TOKEN": "scoped-forge-test",
    }
    assert read_private("old.env") == "ANTHROPIC_API_KEY=keep-test\n"
    assert "scoped-forge-test" not in read_private(candidate.env_file)


# 2119: 1.5, 4.2, 4.5
def test_cancel_during_repo_repair_preserves_previous_binding_and_work(
    client: TaskServiceClient,
) -> None:
    write_private("old.env", "ANTHROPIC_API_KEY=keep-test\n")
    client.create_repo("one", "one", "https://github.com/example/one", env_file="old.env")
    task = client.create_task("one", "spike")

    def cancel(_: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        configure_repo(client, "one", input_fn=lambda _: "n", secret_fn=cancel)
    assert client.get_repo("one")["env_file"] == "old.env"
    assert read_private("old.env") == "ANTHROPIC_API_KEY=keep-test\n"
    assert client.get_task(task["id"])["launch_paused"]
    assert client.get_repo("one")["launch_paused"]


# 2119: 4.6
# 2119: runtime-readiness.4.14
def test_unreachable_service_prevents_any_credential_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path / "config"))

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(offline), base_url="http://test") as http,
        pytest.raises((httpx.ConnectError, RuntimeError)),
    ):
        configure_repo(TaskServiceClient(http), "one")
    assert not (tmp_path / "config").exists()


# 2119: 4.3, 4.4
def test_running_legacy_login_blocks_foreground_mutation(client: TaskServiceClient) -> None:
    write_private("existing.env", "ANTHROPIC_API_KEY=keep-test\n")
    client.create_repo("one", "one", "https://example.test/one", env_file="existing.env")
    task = client.create_task("one", "setup-repo")
    client.claim(task["id"], "runner")
    registration = client.register(task["id"], "shell-session", "runner")
    with pytest.raises(RuntimeError, match="older authentication session"):
        configure_repo(client, "one")
    assert read_private("existing.env") == "ANTHROPIC_API_KEY=keep-test\n"
    assert client.get_repo("one")["env_file"] == "existing.env"
    assert not client.get_repo("one")["launch_paused"]
    assert client.get_task(task["id"])["state"] == "RUNNING"
    assert client.list_registrations(task["id"])[0]["id"] == registration["id"]
