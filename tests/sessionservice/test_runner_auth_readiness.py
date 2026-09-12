"""Credential transport and real REST failure-latch tests, without launching a provider."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from panopticon.client import TaskServiceClient
from panopticon.container import agent
from panopticon.harnesses import claude
from panopticon.harnesses.pi import API_KEY_AUTH_PROVIDERS, API_KEY_ENV_VARS, OAUTH_AUTH_PROVIDERS
from panopticon.sessionservice.auth_readiness import missing_repo_auth, missing_task_auth
from panopticon.sessionservice.spawner import Spawner, spawnable_tasks
from panopticon.taskservice.api import create_app
from panopticon.taskservice.artifacts_fs import FilesystemArtifactStore
from panopticon.taskservice.service import TaskService
from panopticon.taskservice.store_sqlalchemy import SqlAlchemyStore
from panopticon.workflows import Spike

# 2119-spec: task-auth-readiness

PI_ENV_TRANSPORTS = [
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "COPILOT_GITHUB_TOKEN",
    "ANT_LING_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "NVIDIA_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_API_KEY",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "XAI_API_KEY",
    "RADIUS_API_KEY",
    "OPENROUTER_API_KEY",
    "AI_GATEWAY_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_CN_API_KEY",
    "MISTRAL_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "MOONSHOT_API_KEY",
    "HF_TOKEN",
    "FIREWORKS_API_KEY",
    "TOGETHER_API_KEY",
    "OPENCODE_API_KEY",
    "KIMI_API_KEY",
    "CLOUDFLARE_API_KEY",
    "XIAOMI_API_KEY",
    "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
]
PI_API_PROVIDERS = [
    "anthropic",
    "ant-ling",
    "azure-openai-responses",
    "openai",
    "deepseek",
    "nvidia",
    "google",
    "amazon-bedrock",
    "mistral",
    "groq",
    "cerebras",
    "cloudflare-ai-gateway",
    "cloudflare-workers-ai",
    "xai",
    "openrouter",
    "vercel-ai-gateway",
    "zai",
    "zai-coding-cn",
    "opencode",
    "opencode-go",
    "radius",
    "huggingface",
    "fireworks",
    "together",
    "baseten",
    "kimi-coding",
    "minimax",
    "minimax-cn",
    "qwen-token-plan",
    "qwen-token-plan-cn",
    "xiaomi",
    "xiaomi-token-plan-cn",
    "xiaomi-token-plan-ams",
    "xiaomi-token-plan-sgp",
]
PI_OAUTH_PROVIDERS = ["openai-codex", "anthropic", "github-copilot", "xai", "radius"]


@pytest.fixture
def private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    monkeypatch.setattr(
        claude, "_probe_status", lambda *_args, **_kwargs: pytest.fail("No provider probe")
    )
    return secrets


@pytest.fixture
def forbid_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("Presence checking attempted provider/network/process execution")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "execv", forbidden)
    monkeypatch.setattr(os, "execve", forbidden)


# 2119: 3.1
# 2119: 3.2
def test_host_credentials_do_not_make_a_task_ready(
    private_config: Path, monkeypatch: pytest.MonkeyPatch, forbid_provider_calls: None
) -> None:
    for name in (
        *API_KEY_ENV_VARS,
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.setenv(name, "synthetic-host-only-credential")
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
    private_config: Path, harness: str, content: str, forbid_provider_calls: None
) -> None:
    (private_config / "repo.env").write_text(content + "\n")
    assert missing_repo_auth({"env_file": "repo.env"}, harness) is None


# 2119: 3.6
@pytest.mark.parametrize("harness", ["codex", "pi", "outfitter"])
def test_explicit_credential_directory_is_used(
    private_config: Path, harness: str, forbid_provider_calls: None
) -> None:
    credentials = private_config / "account"
    target = credentials / "pi" / "agent" if harness == "pi" else credentials
    target.mkdir(parents=True)
    (target / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "synthetic"}})
    )
    assert missing_repo_auth({"credential_dir": "account"}, harness) is None
    (target / "auth.json").unlink()
    assert missing_repo_auth({"credential_dir": "account"}, harness) is not None


# 2119: 3.6
def test_pi_transport_fixture_covers_registered_support() -> None:
    assert set(PI_ENV_TRANSPORTS) == set(API_KEY_ENV_VARS)
    assert set(PI_API_PROVIDERS) == API_KEY_AUTH_PROVIDERS
    assert set(PI_OAUTH_PROVIDERS) == OAUTH_AUTH_PROVIDERS


# 2119: 3.6
@pytest.mark.parametrize("name", PI_ENV_TRANSPORTS)
def test_each_pi_environment_transport_has_positive_and_absent_control(
    private_config: Path, name: str, forbid_provider_calls: None
) -> None:
    path = private_config / "pi.env"
    path.write_text(f"{name}=synthetic-provider-key\n")
    assert missing_repo_auth({"env_file": "pi.env"}, "pi") is None
    path.write_text(f"{name}=\n")
    assert missing_repo_auth({"env_file": "pi.env"}, "pi") is not None


# 2119: 3.6
@pytest.mark.parametrize(
    "provider,kind",
    [(p, "api_key") for p in PI_API_PROVIDERS] + [(p, "oauth") for p in PI_OAUTH_PROVIDERS],
)
def test_each_pi_file_transport_has_positive_and_invalid_control(
    private_config: Path, provider: str, kind: str, forbid_provider_calls: None
) -> None:
    path = private_config / "account" / "pi" / "agent" / "auth.json"
    path.parent.mkdir(parents=True)
    credential = (
        {"type": kind, "key": "synthetic-key"}
        if kind == "api_key"
        else {
            "type": "oauth",
            "access": "synthetic-access",
            "refresh": "synthetic-refresh",
            "expires": 0,
            "accountId": "synthetic-account",
        }
    )
    path.write_text(json.dumps({provider: credential}))
    assert missing_repo_auth({"credential_dir": "account"}, "pi") is None
    credential.pop("key" if kind == "api_key" else "refresh")
    path.write_text(json.dumps({provider: credential}))
    assert missing_repo_auth({"credential_dir": "account"}, "pi") is not None


# 2119: 3.6
def test_pi_custom_model_and_scoped_environment_credentials(
    private_config: Path, forbid_provider_calls: None
) -> None:
    directory = private_config / "account" / "pi" / "agent"
    directory.mkdir(parents=True)
    models = directory / "models.json"
    models.write_text(json.dumps({"providers": {"custom": {"apiKey": "${PRIVATE_GATEWAY_KEY}"}}}))
    (private_config / "pi.env").write_text("PRIVATE_GATEWAY_KEY=synthetic\n")
    repo = {"credential_dir": "account", "env_file": "pi.env", "default_model": "custom/model"}
    assert missing_repo_auth(repo, "pi") is None
    (private_config / "pi.env").write_text("")
    assert missing_repo_auth(repo, "pi") is not None
    (directory / "auth.json").write_text(
        json.dumps(
            {
                "anthropic": {
                    "type": "api_key",
                    "key": "${SCOPED_KEY}",
                    "env": {"SCOPED_KEY": "synthetic"},
                }
            }
        )
    )
    assert missing_repo_auth(repo, "pi") is None
    (directory / "auth.json").write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "!this-command-must-not-execute"}})
    )
    assert missing_repo_auth(repo, "pi") is None


# 2119: 3.1
# 2119: 3.3
# 2119: 3.4
# 2119: 3.8
@pytest.mark.parametrize(
    "harness,key",
    [
        ("claude", "ANTHROPIC_API_KEY"),
        ("codex", "OPENAI_API_KEY"),
        ("pi", "ANTHROPIC_API_KEY"),
        ("outfitter", "OPENAI_API_KEY"),
    ],
)
def test_missing_auth_latches_before_clone_and_only_explicit_retry_starts(
    private_config: Path, monkeypatch: pytest.MonkeyPatch, harness: str, key: str
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
        task = client.create_task(
            "repo", "spike", initial_prompt="Keep this prompt", harness=harness
        )
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
        with pytest.raises(
            RuntimeError, match=f"Connect {harness} in foreground setup, then retry this task"
        ):
            spawner.spawn_one(task)
        failed = client.get_task(task["id"])
        assert failed["claimed_by"] == "runner"
        assert failed["launch_paused"]
        assert (
            f"Connect {harness} in foreground setup, then retry this task"
            in failed["lifecycle_detail"]
        )
        assert launched == []
        assert not (private_config.parent / "tasks").exists()
        (private_config / "repo.env").write_text(f"{key}=synthetic\n")
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
def test_resume_keeps_persistent_container_auth_path(
    private_config: Path, monkeypatch: pytest.MonkeyPatch, forbid_provider_calls: None
) -> None:
    assert missing_task_auth({"harness": "codex", "clone": "/existing/task"}, {}) is None
    home = private_config.parent / "volume"
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"OPENAI_API_KEY":"synthetic-volume-key"}')
    for name in (
        *API_KEY_ENV_VARS,
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "PANOPTICON_CREDENTIALS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://service.invalid")
    monkeypatch.setenv("PANOPTICON_TASK_ID", "task")
    monkeypatch.setenv("PANOPTICON_RUNNER_ID", "runner")
    monkeypatch.setenv("PANOPTICON_HARNESS", "codex")
    launched, failures = [], []
    client = SimpleNamespace(
        list_skills=lambda _task: [],
        list_operations=lambda _task: {},
        workflow_overview=lambda _task: "Resume existing work.",
        report_lifecycle=lambda *args, **kwargs: failures.append((args, kwargs)),
    )

    def launch(harness, context):
        assert context.home == home and harness.name == "codex"
        assert json.loads(auth.read_text())["OPENAI_API_KEY"] == "synthetic-volume-key"
        launched.append(harness.name)

    agent.main(client_factory=lambda _url: client, home=home, launch=launch, on_exit=lambda: None)
    assert launched == ["codex"] and failures == []
    # If the actual in-container check is removed, this second launch incorrectly succeeds.
    auth.unlink()
    agent.main(client_factory=lambda _url: client, home=home, launch=launch, on_exit=lambda: None)
    assert launched == ["codex"]
    assert len(failures) == 1
    assert failures[0][1]["phase"] == "failed"
    assert "No codex credentials" in failures[0][1]["detail"]


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
        assert "Task launch is paused; finish setup and explicitly retry this task." in caplog.text


# 2119: 3.8
def test_restarted_runner_skips_unclaimed_and_nonfailed_held_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def service():
        return TaskService(
            SqlAlchemyStore(f"sqlite:///{tmp_path / 'tasks.db'}"),
            {"spike": Spike()},
            FilesystemArtifactStore(tmp_path / "artifacts"),
        )

    with TestClient(create_app(service())) as http:
        client = TaskServiceClient(http)
        http.post(
            "/repos", json={"id": "repo", "name": "Example", "git_url": "https://example.test/repo"}
        ).raise_for_status()
        unclaimed = client.create_task("repo", "spike")
        claimed = client.create_task("repo", "spike")
        client.claim(claimed["id"], "runner")
        client.report_lifecycle(
            claimed["id"], "runner", "failed", "Missing credentials", pause_launch=True
        )
        client.begin_repo_setup("repo")
        client.finish_repo_setup("repo")
    with TestClient(create_app(service())) as http:
        client = TaskServiceClient(http)
        snapshot = client.list_tasks()
        assert {task["id"] for task in snapshot} == {unclaimed["id"], claimed["id"]}
        assert all(task["container_status"] == "paused" for task in snapshot)
        assert {task["claimed_by"] for task in snapshot} == {None, "runner"}

        def forbidden(*_args, **_kwargs):
            pytest.fail("Held task reached a launch/heal/release side effect")

        monkeypatch.setattr(client, "claim", forbidden)
        monkeypatch.setattr(client, "release", forbidden)
        monkeypatch.setattr(client, "report_lifecycle", forbidden)
        runner = SimpleNamespace(
            delete_workspace_contents=forbidden,
            has_session=forbidden,
            is_running=forbidden,
            validate_configuration=forbidden,
        )
        spawner = Spawner(
            client,
            runner,
            runner_id="runner",
            cache=object(),
            tasks_root="unused",
            credential_check=forbidden,
        )  # type: ignore[arg-type]
        assert spawnable_tasks(client, snapshot)() == []
        for task in snapshot:
            assert spawner.spawn_one(task) is None
            assert spawner.heal(task) is None
            spawner.mark_healing(task)
        spawner.startup_reclaim(snapshot)
