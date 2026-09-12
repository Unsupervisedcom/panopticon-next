"""Foreground repair through the real REST client, service and SQLite adapter."""

# 2119-spec: foreground-setup
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

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


# 2119: 2.3
def test_keep_complete_binding_is_read_only_and_does_not_pause_work(
    client: TaskServiceClient,
) -> None:
    content = "ANTHROPIC_API_KEY=keep-test\nGH_TOKEN=repo-only-test\n"
    write_private("existing.env", content)
    client.create_repo("one", "one", "https://github.com/example/one", env_file="existing.env")
    task = client.create_task("one", "spike")
    before = client.get_repo("one")
    assert configure_repo(client, "one", input_fn=lambda _: "n")
    assert client.get_repo("one") == before
    assert read_private("existing.env") == content
    assert not client.get_task(task["id"])["launch_paused"]


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


def test_changing_agent_drops_only_the_old_agent_model_default(client: TaskServiceClient) -> None:
    write_private("old.env", "ANTHROPIC_API_KEY=keep-test\n")
    client.create_repo(
        "one",
        "one",
        "https://example.test/one",
        env_file="old.env",
        default_harness="claude",
    )
    client.update_repo("one", default_model="claude-test-model:high")
    existing = client.create_task("one", "spike")
    connection = connect("codex", secret_fn=lambda _: "new-codex-test")
    assert configure_repo(client, "one", connection=connection, input_fn=lambda _: "y")
    repo = client.get_repo("one")
    assert repo["default_harness"] == "codex"
    assert env_values(read_private(repo["env_file"]))["PANOPTICON_RECONCILE_CODEX_AUTH"] == "1"
    assert "PANOPTICON_RECONCILE_CODEX_AUTH" not in read_private(connection.env_file)
    assert repo["default_model"] is None
    created = client.create_task("one", "spike")
    assert created["harness"] == "codex"
    assert created["starting_model"] is None
    preserved = client.get_task(existing["id"])
    assert preserved["harness"] == "claude"
    assert preserved["starting_model"] == "claude-test-model:high"


# 2119: 2.8
def test_two_repositories_explicitly_share_writable_codex_subscription(
    client: TaskServiceClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "panopticon.terminal.setup_credentials.shutil.which", lambda _: "/test/codex"
    )

    def login(argv, **kwargs):
        auth = Path(kwargs["env"]["CODEX_HOME"]) / "auth.json"
        auth.write_text(json.dumps({"tokens": {"access_token": "shared-test"}}))
        auth.chmod(0o600)
        return SimpleNamespace(returncode=0)

    connection = connect("codex", secret_fn=lambda _: "", run=login)
    for name in ("one", "two"):
        client.create_repo(name, name, f"https://example.test/{name}")
        assert configure_repo(client, name, connection=connection)
    one, two = client.get_repo("one"), client.get_repo("two")
    assert one["credential_dir"] == two["credential_dir"] == connection.credential_dir
    assert one["env_file"] != two["env_file"]
    directory = tmp_path / "config" / "secrets" / connection.credential_dir
    assert directory.stat().st_mode & 0o777 == 0o700
    assert directory.stat().st_uid == os.geteuid()
    assert os.access(directory, os.W_OK)
    auth = directory / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "rotated-test"}}))
    assert repo_configured(one) and repo_configured(two)


# 2119: 2.9
def test_personal_codex_login_cannot_complete_empty_repo_transport(
    client: TaskServiceClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    personal = tmp_path / "personal-codex"
    personal.mkdir()
    auth = personal / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "personal-test"}}))
    monkeypatch.setenv("CODEX_HOME", str(personal))
    monkeypatch.setenv("CODEX_API_KEY", "ambient-test")
    write_private("empty-codex.env", "# empty\n")
    client.create_repo(
        "one",
        "one",
        "https://example.test/one",
        default_harness="codex",
        env_file="empty-codex.env",
    )
    assert not repo_configured(client.get_repo("one"))
    assert not configure_repo(client, "one", input_fn=lambda _: "n")
    assert json.loads(auth.read_text())["tokens"]["access_token"] == "personal-test"


# 2119: REQ-054.5.1, REQ-054.5.2, REQ-054.5.4
def test_foreground_repository_files_are_private_and_repair_retains_old_snapshot(
    client: TaskServiceClient, tmp_path: Path
) -> None:
    import stat

    root = tmp_path / "config" / "secrets"
    assert not root.exists()
    client.create_repo("one", "one", "https://example.test/one")
    assert configure_repo(client, "one", connection=saved_connection())
    before = client.get_repo("one")
    old = root / before["env_file"]
    snapshot = old.read_bytes()
    assert root.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert not old.is_symlink() and stat.S_ISREG(old.stat().st_mode)
    assert old.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(old.stat().st_mode) == 0o600
    replacement = connect("claude", input_fn=lambda _: "n", secret_fn=lambda _: "replacement-test")
    assert configure_repo(client, "one", connection=replacement, input_fn=lambda _: "y")
    new = root / client.get_repo("one")["env_file"]
    assert new != old and old.read_bytes() == snapshot
    assert new.stat().st_uid == os.geteuid() and stat.S_IMODE(new.stat().st_mode) == 0o600
    assert not new.is_symlink()


# 2119: REQ-054.5.3, REQ-054.7.5
@pytest.mark.parametrize("failure", ["cancel", "empty-credential", "forge-declined"])
def test_incomplete_foreground_setup_never_creates_repo_env_or_claims_completion(
    client: TaskServiceClient, tmp_path: Path, failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    client.create_repo("one", "one", "https://github.com/example/one")
    if failure == "cancel":

        def cancel(_):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            configure_repo(client, "one", input_fn=cancel)
    elif failure == "empty-credential":
        with pytest.raises(ValueError, match="incomplete"):
            configure_repo(client, "one", connection=Connection("claude", "absent.env"))
    else:
        assert not configure_repo(
            client, "one", connection=saved_connection(), secret_fn=lambda _: ""
        )
    assert list((tmp_path / "config" / "secrets").glob("repo-*.env")) == []
    assert client.get_repo("one")["env_file"] is None
    assert client.get_repo("one")["launch_paused"]
    output = capsys.readouterr().out.lower()
    assert "credentials configured on this host" not in output
    assert "setup complete" not in output


# 2119: REQ-054.5.5, REQ-054.5.6
@pytest.mark.parametrize("unsafe", ["public-dir", "symlink-dir", "public-file", "symlink-file"])
def test_foreground_refuses_unsafe_paths_with_path_diagnostic_without_repair(
    client: TaskServiceClient, tmp_path: Path, unsafe: str
) -> None:
    root = tmp_path / "config" / "secrets"
    root.mkdir(parents=True, mode=0o700)
    leaf = root / "existing.env"
    leaf.write_text("ANTHROPIC_API_KEY=old-test\n")
    leaf.chmod(0o600)
    client.create_repo("one", "one", "https://example.test/one", env_file="existing.env")
    path = root if unsafe.endswith("dir") else leaf
    target = tmp_path / "original"
    if unsafe == "symlink-dir":
        root.rename(target)
        root.symlink_to(target, target_is_directory=True)
    elif unsafe == "symlink-file":
        leaf.rename(target)
        leaf.symlink_to(target)
    else:
        path.chmod(0o755 if unsafe.endswith("dir") else 0o644)
    before = (path.lstat(), leaf.read_bytes())
    with pytest.raises((ValueError, OSError)) as raised:
        configure_repo(
            client, "one", connection=Connection("claude", "existing.env"), input_fn=lambda _: "y"
        )
    assert str(path) in str(raised.value)
    assert (path.lstat(), leaf.read_bytes()) == before
    assert client.get_repo("one")["env_file"] == "existing.env"
