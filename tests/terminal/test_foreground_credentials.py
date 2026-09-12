"""Credential persistence and isolation exercised against real private files."""

# 2119-spec: foreground-setup
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from panopticon.terminal import setup_credentials as credentials


@pytest.fixture
def private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "config"
    monkeypatch.setenv("PANOPTICON_CONFIG", str(root))
    return root / "secrets"


def answers(*values: str):
    sequence = iter(values)
    return lambda _: next(sequence)


# 2119: 2.1, 2.10, 2.13, 2.14, 3.1, 3.5
def test_saved_connection_is_private_harness_only_and_never_adopts_ambient(
    private_config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ambient-forge")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-provider")
    with credentials.setup_lock():
        connection = credentials.connect("claude", secret_fn=answers("sk-ant-api-test-only"))
    values = credentials.connection_values(connection)
    assert values == {"ANTHROPIC_API_KEY": "sk-ant-api-test-only"}
    output = capsys.readouterr().out
    assert "configured locally" in output
    assert "sk-ant-api-test-only" not in output
    assert "ambient" not in output
    assert private_config.stat().st_mode & 0o777 == 0o700
    for path in private_config.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    records = json.loads((private_config / "connections.json").read_text())
    assert records["claude"] == {"env_file": connection.env_file, "credential_dir": None}


# 2119: 3.1, 3.2
@pytest.mark.parametrize("unsafe", ["symlink", "public"])
def test_unsafe_write_preserves_destination(
    private_config: Path, tmp_path: Path, unsafe: str
) -> None:
    credentials.write_private("existing.env", "OLD=value\n")
    target = private_config / "existing.env"
    if unsafe == "symlink":
        other = tmp_path / "other"
        other.write_text("do not change")
        target.unlink()
        target.symlink_to(other)
    else:
        target.chmod(0o644)
    before = target.read_text()
    with pytest.raises((OSError, ValueError)):
        credentials.write_private("existing.env", "NEW=value\n")
    assert target.read_text() == before
    assert target.is_symlink() if unsafe == "symlink" else target.stat().st_mode & 0o777 == 0o644


# 2119: 2.1, 2.4
def test_corrupt_connection_never_supplies_forge_values(private_config: Path) -> None:
    credentials.write_private("bad.env", "GH_TOKEN=test-forge\nANTHROPIC_API_KEY=test-agent\n")
    with pytest.raises(ValueError, match="non-harness"):
        credentials.connection_values(credentials.Connection("claude", "bad.env"))


# 2119: 3.1
def test_atomic_write_failure_keeps_previous_file(
    private_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials.write_private("existing.env", "OLD=value\n")

    def fail(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(credentials.os, "replace", fail)
    with pytest.raises(OSError):
        credentials.write_private("existing.env", "NEW=value\n")
    assert credentials.read_private("existing.env") == "OLD=value\n"
    assert not list(private_config.glob(".setup-*"))


# 2119: 1.4, 1.5, 3.3
def test_cancel_preserves_saved_connection_and_resume_rechecks(
    private_config: Path,
) -> None:
    first = credentials.connect("claude", secret_fn=answers("first-test-token"))
    with pytest.raises(KeyboardInterrupt):
        credentials.connect(
            "claude",
            input_fn=answers("n"),
            secret_fn=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    assert credentials.load_connections()["claude"] == first
    assert credentials.connect("claude", input_fn=answers(""), secret_fn=answers()) == first
    (private_config / first.env_file).write_text("# incomplete\n")
    replacement = credentials.connect("claude", secret_fn=answers("new-test-token"))
    assert replacement != first
    assert credentials.connection_values(replacement) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "new-test-token"
    }
    (private_config / replacement.env_file).unlink()
    repaired = credentials.connect("claude", secret_fn=answers("repaired-test-token"))
    assert repaired != replacement
    assert credentials.load_connections()["claude"] == repaired


# 2119: 3.4, 3.5
def test_claude_login_is_direct_followed_by_hidden_paste(
    private_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credentials.shutil, "which", lambda _: "/test/claude")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    connection = credentials.connect("claude", secret_fn=answers("", "minted-test-token"), run=run)
    assert calls == [(["claude", "setup-token"], {"check": False})]
    assert (
        credentials.connection_values(connection)["CLAUDE_CODE_OAUTH_TOKEN"] == "minted-test-token"
    )


# 2119: 2.8, 2.12, 3.3, 3.6
def test_codex_login_is_isolated_and_failed_relogin_keeps_prior_account(
    private_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    personal = tmp_path / "personal-codex"
    personal.mkdir()
    (personal / "auth.json").write_text('{"personal":"untouched"}')
    monkeypatch.setenv("CODEX_HOME", str(personal))
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setattr(credentials.shutil, "which", lambda _: "/test/codex")
    homes = []

    def login(argv, **kwargs):
        assert argv == ["codex", "-c", 'cli_auth_credentials_store="file"', "login"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        home = Path(kwargs["env"]["CODEX_HOME"])
        homes.append(home)
        auth = home / "auth.json"
        auth.write_text('{"tokens":{"access_token":"disposable-test"}}')
        auth.chmod(0o600)
        return SimpleNamespace(returncode=0)

    first = credentials.connect("codex", secret_fn=answers(""), run=login)
    reused = credentials.connect("codex", input_fn=answers(""), secret_fn=answers())
    assert reused.credential_dir == first.credential_dir

    def failed_login(argv, **kwargs):
        assert kwargs["env"]["CODEX_HOME"] != str(homes[0])
        return SimpleNamespace(returncode=1)

    with pytest.raises(RuntimeError, match="cancelled"):
        credentials.connect("codex", input_fn=answers("n"), secret_fn=answers(""), run=failed_login)
    assert credentials.load_connections()["codex"] == first
    assert json.loads((personal / "auth.json").read_text()) == {"personal": "untouched"}
    assert credentials.configured(first)


# 2119: 3.7
def test_writer_lock_excludes_other_process_then_releases(private_config: Path) -> None:
    script = "from panopticon.terminal.setup_credentials import setup_lock\nwith setup_lock(): print('acquired')"
    with credentials.setup_lock():
        refused = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert refused.returncode != 0
    assert "Another setup is active" in refused.stderr
    accepted = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert accepted.returncode == 0
    assert accepted.stdout.strip() == "acquired"


# 2119: 2.5
def test_env_merge_preserves_unrelated_literal_values_and_rejects_multiline() -> None:
    existing = "# comment\nGH_TOKEN=repo-only\nCUSTOM=a=b $HOME\nANTHROPIC_API_KEY=old\n"
    assert credentials.merge_env(existing, {"ANTHROPIC_API_KEY": "new"}) == (
        "# comment\nGH_TOKEN=repo-only\nCUSTOM=a=b $HOME\nANTHROPIC_API_KEY=new\n"
    )
    with pytest.raises(ValueError, match="single line"):
        credentials.merge_env(existing, {"ANTHROPIC_API_KEY": "bad\nGH_TOKEN=wrong"})
