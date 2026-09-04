"""Secure zero-config authentication for integrated startup."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from panopticon.taskservice import auth
from panopticon.terminal import __main__ as cli


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _no_sessions(_args: list[str], **_kwargs: object) -> _Completed:
    return _Completed(1)


def test_default_integrated_auth_is_enforced_before_clients_or_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 2119: REQ-035.49.1
    # 2119: REQ-035.49.2
    # 2119: REQ-035.49.3
    # 2119: REQ-035.50.4
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))

    cli._ensure_integrated_auth(run=_no_sessions)

    assert os.environ["PANOPTICON_SERVICE_AUTH_FILE"] == auth.BOOTSTRAP_AUTH_FILE
    assert os.environ["PANOPTICON_SERVICE_AUTH_MODE"] == "enforced"
    assert auth.environment_token() == auth.load_tokens(auth.BOOTSTRAP_AUTH_FILE).write[-1]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("argv", [[], ["start"], ["host"], ["quickstart"]])
def test_every_integrated_entrypoint_reaches_auth_bootstrap(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.49.1
    # 2119: REQ-054.3.2
    class _ReachedBootstrap(RuntimeError):
        pass

    from panopticon.terminal import doctor, quickstart

    monkeypatch.setattr(
        "panopticon.sessionservice.docker_daemon.preflight_message", lambda _command: None
    )
    if not argv:
        monkeypatch.setattr(cli, "_has_bootstrap_credential", lambda: True)
    monkeypatch.setattr(doctor, "run_checks", list)
    monkeypatch.setattr(doctor, "report", lambda _results: 0)
    monkeypatch.setattr(quickstart, "detect_git_url", lambda: "https://github.com/acme/repo.git")
    monkeypatch.setattr(
        cli,
        "_ensure_integrated_auth",
        lambda: (_ for _ in ()).throw(_ReachedBootstrap),
    )

    with pytest.raises(_ReachedBootstrap):
        cli.main(argv)


def test_bootstrap_credential_is_private_and_uses_256_bits_of_randomness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.50.1
    # 2119: REQ-035.50.2
    calls: list[int] = []
    token = "a" * 43
    monkeypatch.setattr(
        auth.secrets,
        "token_urlsafe",
        lambda size: (calls.append(size), token)[1],
    )

    reference = auth.ensure_bootstrap_credential(secrets_dir=tmp_path)
    credential = tmp_path / reference

    assert calls == [32]
    assert json.loads(credential.read_text()) == {"read": [], "write": [token]}
    metadata = credential.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert not stat.S_ISLNK(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.geteuid()


def test_bootstrap_reuses_a_valid_credential_without_mutation(tmp_path: Path) -> None:
    # 2119: REQ-035.51.1
    reference = auth.ensure_bootstrap_credential(secrets_dir=tmp_path)
    credential = tmp_path / reference
    before = credential.stat()
    contents = credential.read_bytes()

    assert auth.ensure_bootstrap_credential(secrets_dir=tmp_path) == reference

    after = credential.stat()
    assert credential.read_bytes() == contents
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_uid) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
    )


@pytest.mark.parametrize("existing_kind", ["invalid", "insecure", "directory", "symlink"])
def test_bootstrap_never_repairs_or_replaces_an_invalid_destination(
    tmp_path: Path, existing_kind: str
) -> None:
    # 2119: REQ-035.50.3
    # 2119: REQ-035.51.2
    # 2119: REQ-035.51.3
    destination = tmp_path / auth.BOOTSTRAP_AUTH_FILE
    if existing_kind == "invalid":
        destination.write_text("invalid")
    elif existing_kind == "insecure":
        destination.write_text(json.dumps({"read": [], "write": ["valid-token-long"]}))
        destination.chmod(0o644)
    elif existing_kind == "directory":
        destination.mkdir()
    else:
        target = tmp_path / "target"
        target.write_text(json.dumps({"read": [], "write": ["valid-token-long"]}))
        target.chmod(0o600)
        destination.symlink_to(target)
    before = destination.lstat()
    before_bytes = destination.read_bytes() if destination.is_file() else None
    before_target = os.readlink(destination) if destination.is_symlink() else None

    with pytest.raises(ValueError, match="credential file is invalid"):
        auth.ensure_bootstrap_credential(secrets_dir=tmp_path)

    after = destination.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_size) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_size,
    )
    if before_bytes is not None:
        assert destination.read_bytes() == before_bytes
    if before_target is not None:
        assert os.readlink(destination) == before_target


def test_bootstrap_token_never_leaks_to_output_logs_process_args_or_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 2119: REQ-035.50.4
    token = "unique-bootstrap-secret-token-123456789"
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _size: token)

    with patch.object(subprocess, "run") as run:
        auth.ensure_bootstrap_credential(secrets_dir=tmp_path / "success")

    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert token not in caplog.text
    run.assert_not_called()

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("publish denied")

    monkeypatch.setattr(auth.os, "link", fail_publish)
    with pytest.raises(PermissionError) as raised:
        auth.ensure_bootstrap_credential(secrets_dir=tmp_path / "failure")
    assert token not in str(raised.value)


def test_concurrent_bootstraps_converge_on_one_complete_credential(tmp_path: Path) -> None:
    # 2119: REQ-035.51.4
    with ThreadPoolExecutor(max_workers=8) as pool:
        references = list(
            pool.map(
                lambda _index: auth.ensure_bootstrap_credential(secrets_dir=tmp_path), range(8)
            )
        )

    assert references == [auth.BOOTSTRAP_AUTH_FILE] * 8
    tokens = auth.load_tokens(auth.BOOTSTRAP_AUTH_FILE, secrets_dir=tmp_path)
    assert tokens.read == ()
    assert len(tokens.write) == 1
    assert not list(tmp_path.glob(".task-service-auth-*"))


@pytest.mark.parametrize(
    ("reference", "mode", "expected_error"),
    [
        (None, "enforced", "credential file is required"),
        (None, "unexpected", "mode must be disabled or enforced"),
    ],
)
def test_invalid_explicit_auth_configuration_fails_before_session_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str | None,
    mode: str,
    expected_error: str,
) -> None:
    # 2119: REQ-035.49.5
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    if reference is not None:
        monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", reference)
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_MODE", mode)

    with pytest.raises(ValueError, match=expected_error):
        cli._ensure_integrated_auth(run=lambda *_args, **_kwargs: pytest.fail("tmux was probed"))


def test_configured_credential_without_mode_selects_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.49.4
    secrets = tmp_path / "secrets"
    reference = auth.ensure_bootstrap_credential(secrets_dir=secrets)
    credential = secrets / reference
    before = credential.lstat()
    contents = credential.read_bytes()
    existing_paths = {path.name for path in secrets.iterdir()}
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", reference)

    cli._ensure_integrated_auth(run=lambda *_args, **_kwargs: pytest.fail("tmux was probed"))

    assert os.environ["PANOPTICON_SERVICE_AUTH_MODE"] == "enforced"
    assert os.environ["PANOPTICON_SERVICE_AUTH_FILE"] == reference
    assert auth.environment_token() == auth.load_tokens(reference).write[-1]
    after = credential.lstat()
    assert credential.read_bytes() == contents
    assert (after.st_ino, after.st_mode, after.st_uid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
    )
    assert {path.name for path in secrets.iterdir()} == existing_paths


def test_integrated_restart_reuses_bootstrap_credential_even_when_sessions_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-054.3.2
    secrets = tmp_path / "secrets"
    reference = auth.ensure_bootstrap_credential(secrets_dir=secrets)
    credential = secrets / reference
    before = credential.lstat()
    contents = credential.read_bytes()
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))

    cli._ensure_integrated_auth(run=lambda *_args, **_kwargs: _Completed(0))

    assert os.environ["PANOPTICON_SERVICE_AUTH_FILE"] == reference
    assert os.environ["PANOPTICON_SERVICE_AUTH_MODE"] == "enforced"
    after = credential.lstat()
    assert credential.read_bytes() == contents
    assert (after.st_ino, after.st_mode, after.st_uid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
    )


def test_client_only_selection_reuses_existing_bootstrap_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-054.3.3
    # 2119: REQ-054.3.5
    secrets = tmp_path / "secrets"
    reference = auth.ensure_bootstrap_credential(secrets_dir=secrets)
    credential = secrets / reference
    before = credential.lstat()
    contents = credential.read_bytes()
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))

    cli._select_existing_integrated_auth()

    assert os.environ["PANOPTICON_SERVICE_AUTH_FILE"] == reference
    assert auth.environment_token() == auth.load_tokens(reference).write[-1]
    after = credential.lstat()
    assert credential.read_bytes() == contents
    assert (after.st_ino, after.st_mode, after.st_uid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
    )


def test_client_only_selection_does_not_create_a_missing_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-054.3.4
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))

    cli._select_existing_integrated_auth()

    assert not (tmp_path / "secrets" / auth.BOOTSTRAP_AUTH_FILE).exists()
    assert "PANOPTICON_SERVICE_AUTH_FILE" not in os.environ


@pytest.mark.parametrize("kind", ["insecure", "symlink"])
def test_client_only_selection_rejects_unsafe_bootstrap_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # 2119: REQ-054.3.1
    # 2119: REQ-054.3.6
    # 2119: REQ-054.3.7
    # 2119: REQ-054.3.8
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True)
    destination = secrets / auth.BOOTSTRAP_AUTH_FILE
    if kind == "insecure":
        destination.write_text(json.dumps({"read": [], "write": ["valid-token-long"]}))
        destination.chmod(0o644)
    else:
        target = secrets / "target"
        target.write_text(json.dumps({"read": [], "write": ["valid-token-long"]}))
        target.chmod(0o600)
        destination.symlink_to(target)
    before = destination.lstat()
    before_contents = destination.read_bytes()
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))

    with pytest.raises(ValueError, match="credential file is invalid") as raised:
        cli._select_existing_integrated_auth()

    assert "valid-token-long" not in str(raised.value)
    after = destination.lstat()
    assert destination.read_bytes() == before_contents
    assert (after.st_ino, after.st_mode, after.st_uid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
    )


@pytest.mark.parametrize("command", ["tasks", "dashboard", "console"])
def test_client_only_entrypoints_select_auth_before_constructing_client(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-054.3.3
    calls: list[str] = []

    class _ClientConstructed(RuntimeError):
        pass

    monkeypatch.setattr(cli, "_select_existing_integrated_auth", lambda: calls.append("auth"))

    def make_client(_url: str) -> None:
        assert calls == ["auth"]
        raise _ClientConstructed

    monkeypatch.setattr(cli, "_make_client", make_client)
    with pytest.raises(_ClientConstructed):
        cli.main([command])


def test_bootstrap_refuses_to_mix_with_an_existing_integrated_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.49.6
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))

    with pytest.raises(RuntimeError, match="panopticon stop"):
        cli._ensure_integrated_auth(run=lambda *_args, **_kwargs: _Completed(0))

    assert not (tmp_path / "secrets" / auth.BOOTSTRAP_AUTH_FILE).exists()


def test_explicit_disabled_mode_does_not_touch_the_bootstrap_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: REQ-035.52.1
    # 2119: REQ-035.52.2
    # 2119: REQ-035.52.3
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_MODE", "disabled")
    destination = tmp_path / "secrets" / auth.BOOTSTRAP_AUTH_FILE
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps({"read": [], "write": ["existing-token-long"]}))
    destination.chmod(0o600)
    before = destination.lstat()
    contents = destination.read_bytes()

    cli._ensure_integrated_auth(run=lambda *_args, **_kwargs: pytest.fail("tmux was probed"))

    assert os.environ["PANOPTICON_SERVICE_AUTH_MODE"] == "disabled"
    assert "PANOPTICON_SERVICE_AUTH_FILE" not in os.environ
    assert os.path.lexists(destination)
    after = destination.lstat()
    assert destination.read_bytes() == contents
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_size) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_size,
    )
