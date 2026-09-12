"""Repository-scoped Git authentication for host network Git and task checkouts."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.sessionservice.git_credentials import (
    GitCredentialError,
    RepoGitTransport,
    configure_task_git_credentials,
    repo_git_transport,
)

_TOKEN = "repository-token-test"


def _write_env(root: Path, name: str, content: str) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return path


def _credential_helper_path(command: Sequence[str]) -> Path:
    setting = next(part for part in command if part.startswith("credential.helper=!"))
    return Path(setting.removeprefix("credential.helper=!"))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://GITHUB.com/Acme/private.git", "https://github.com/Acme/private.git"),
        ("git@github.com:Acme/private.git", "https://github.com/Acme/private.git"),
        ("ssh://git@GITHUB.com/Acme/private.git", "https://github.com/Acme/private.git"),
    ],
)
def test_supported_github_sources_use_only_the_repo_env_token(
    tmp_path: Path, source: str, expected: str
) -> None:
    secrets = tmp_path / "secrets"
    _write_env(secrets, "repo.env", f"OTHER=value\nGH_TOKEN={_TOKEN}\n")

    transport = repo_git_transport({"git_url": source, "env_file": "repo.env"}, secrets_dir=secrets)

    assert transport.source_url == source
    assert transport.operation_url == expected
    assert transport.credentialed
    assert _TOKEN not in repr(transport)


def test_non_github_and_unbound_repos_do_not_receive_ambient_or_other_repo_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    _write_env(secrets, "one.env", f"GH_TOKEN={_TOKEN}\n")
    _write_env(secrets, "two.env", "GH_TOKEN=second-repository-token-test\n")
    monkeypatch.setenv("GH_TOKEN", "ambient-host-token-test")

    first = repo_git_transport(
        {"git_url": "https://github.com/acme/one.git", "env_file": "one.env"},
        secrets_dir=secrets,
    )
    second = repo_git_transport(
        {"git_url": "https://github.com/acme/two.git", "env_file": "two.env"},
        secrets_dir=secrets,
    )
    unbound = repo_git_transport(
        {"git_url": "https://github.com/acme/public.git"}, secrets_dir=tmp_path / "missing"
    )
    other_host = repo_git_transport(
        {"git_url": "https://git.example/acme/private.git", "env_file": "one.env"},
        secrets_dir=secrets,
    )

    assert first.token == _TOKEN
    assert second.token == "second-repository-token-test"
    assert not unbound.credentialed
    assert not other_host.credentialed


def test_missing_repo_env_on_this_host_fails_without_falling_back_to_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ambient-host-token-test")

    with pytest.raises(GitCredentialError, match="credential file is unavailable on this runner"):
        repo_git_transport(
            {"git_url": "https://github.com/acme/private.git", "env_file": "remote.env"},
            secrets_dir=tmp_path / "different-host-secrets",
        )


@pytest.mark.parametrize("reference", ["../outside.env", "/outside.env"])
def test_repo_env_reference_cannot_escape_the_runner_secrets_dir(
    tmp_path: Path, reference: str
) -> None:
    with pytest.raises(GitCredentialError, match="credential reference is invalid"):
        repo_git_transport(
            {"git_url": "https://github.com/acme/private.git", "env_file": reference},
            secrets_dir=tmp_path,
        )


def test_repo_env_must_be_an_owner_only_regular_file(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    env_file = _write_env(secrets, "repo.env", f"GH_TOKEN={_TOKEN}\n")
    env_file.chmod(0o644)

    with pytest.raises(GitCredentialError, match="owner-only regular file"):
        repo_git_transport(
            {"git_url": "https://github.com/acme/private.git", "env_file": "repo.env"},
            secrets_dir=secrets,
        )


def test_repo_env_symlink_is_rejected_without_reading_its_target(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    outside = _write_env(tmp_path / "outside", "credential.env", f"GH_TOKEN={_TOKEN}\n")
    (secrets / "repo.env").symlink_to(outside)

    with pytest.raises(GitCredentialError, match="credential file is unavailable"):
        repo_git_transport(
            {"git_url": "https://github.com/acme/private.git", "env_file": "repo.env"},
            secrets_dir=secrets,
        )


def test_credential_files_are_private_functional_and_absent_from_command_and_helper() -> None:
    transport = RepoGitTransport(
        "git@github.com:acme/private.git", "https://github.com/acme/private.git", _TOKEN
    )

    with transport.git_command(
        ["git", "clone", transport.operation_url, "/workspace/cache"]
    ) as command:
        helper = _credential_helper_path(command)
        temporary = helper.parent
        token_file = temporary / "token"
        assert stat.S_IMODE(temporary.stat().st_mode) == 0o700
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(helper.stat().st_mode) == 0o700
        response = subprocess.run(
            [helper, "get"],
            input="protocol=https\nhost=github.com\npath=acme/private.git\n\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "username=x-access-token\n" in response
        assert f"password={_TOKEN}\n" in response
        other_repo = subprocess.run(
            [helper, "get"],
            input="protocol=https\nhost=github.com\npath=other/private.git\n\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert other_repo == ""
        assert _TOKEN not in "\n".join(command)
        assert _TOKEN not in helper.read_text()
        assert command.count("credential.helper=") == 1
        assert any(part.startswith("credential.helper=!") for part in command)
        assert "http.extraHeader=" in command

    assert not temporary.exists()


@pytest.mark.parametrize("failure", [RuntimeError("failed"), KeyboardInterrupt()])
def test_credential_files_are_removed_after_failure_or_cancellation(
    failure: BaseException,
) -> None:
    transport = RepoGitTransport(
        "https://github.com/acme/private.git",
        "https://github.com/acme/private.git",
        _TOKEN,
    )
    temporary: Path | None = None

    with (
        pytest.raises(type(failure)),
        transport.git_command(["git", "fetch"]) as command,
    ):
        temporary = _credential_helper_path(command).parent
        raise failure

    assert temporary is not None
    assert not temporary.exists()


def test_credentials_cannot_be_applied_to_a_non_git_command() -> None:
    transport = RepoGitTransport("source", "operation", _TOKEN)

    with (
        pytest.raises(ValueError, match="only to git commands"),
        transport.git_command(["curl", "https://github.com"]),
    ):
        pass


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_task_repo_config_stores_only_the_gh_helper_reference(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch", "main", str(tmp_path / "repo")],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = tmp_path / "repo"
    transport = RepoGitTransport(
        "git@github.com:acme/private.git", "https://github.com/acme/private.git", _TOKEN
    )

    configure_task_git_credentials(str(repo), transport)

    config = (repo / ".git" / "config").read_text()
    assert _TOKEN not in config
    assert "gh auth git-credential" in config
    assert "useHttpPath = true" in config

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "test \"$1 $2 $3\" = 'auth git-credential get' || exit 2\n"
        "printf '%s\\n' username=x-access-token \"password=$GH_TOKEN\"\n"
    )
    gh.chmod(0o700)
    wrong_askpass = fake_bin / "wrong-askpass"
    wrong_askpass.write_text(
        "#!/bin/sh\nprintf called > \"$0.called\"\nprintf '%s\\n' wrong-ambient\n"
    )
    wrong_askpass.chmod(0o700)
    before = dict(os.environ)
    result = subprocess.run(
        ["git", "-C", str(repo), "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=acme/private.git\n\n",
        check=True,
        capture_output=True,
        text=True,
        env={
            **before,
            "PATH": f"{fake_bin}{os.pathsep}{before['PATH']}",
            "GH_TOKEN": _TOKEN,
            "GIT_ASKPASS": str(wrong_askpass),
        },
    )
    assert f"password={_TOKEN}\n" in result.stdout
    assert not Path(f"{wrong_askpass}.called").exists()
    assert dict(os.environ) == before


def test_uncredentialed_task_repo_gets_no_git_config_commands() -> None:
    calls: list[list[str]] = []

    def record(args: Sequence[str], *, check: bool = True) -> str:
        calls.append(list(args))
        return ""

    configure_task_git_credentials(
        "/workspace/repo", RepoGitTransport("source", "source"), run=record
    )

    assert calls == []
