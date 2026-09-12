"""The terminal CLI (`panopticon`). The shared REST client it uses is covered in
test_client.py; the dashboard in test_dashboard.py. Quickstart helpers are in test_quickstart.py."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from panopticon.sessionservice.tmux_defaults import server_default_config_text
from panopticon.terminal import __main__ as cli


def test_version_reports_installed_distribution(capsys: pytest.CaptureFixture[str]) -> None:
    # 2119: REQ-054.1.3
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"panopticon {version('panopticon-next')}"


class _FakeClient:
    def list_tasks(self) -> list[dict[str, object]]:
        return [{"id": "t1", "state": "ITERATING", "turn": "agent", "slug": None}]


def test_cli_tasks_lists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["tasks"], client=_FakeClient())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "t1" in out and "ITERATING" in out and "agent" in out


class _FakeCompletedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


# 2119: REQ-030.1.1
# 2119: REQ-030.3.1
def test_start_sessions_loads_shipped_tmux_defaults_via_dash_f_for_service_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `panopticon start`/`host` run this before the dashboard or any task spawn — in the normal
    # startup flow it's the actual first thing to touch a fresh `-L panopticon` socket, so it must
    # carry the same shipped defaults (REQ-030) as the other three new-session call sites.
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        calls.append(args)
        return _FakeCompletedProcess(returncode=1)  # no session exists yet -> create it

    cli._start_sessions(run=fake_run)

    new_session_calls = [c for c in calls if "new-session" in c]
    assert len(new_session_calls) == 2  # "service" and "runner"
    for tmux_new in new_session_calls:
        assert tmux_new[:3] == ["tmux", "-L", "panopticon"]
        assert tmux_new[3] == "-f"
        config_path = Path(tmux_new[4])
        assert tmux_new[5] == "new-session"
        assert config_path.read_text() == server_default_config_text(clipboard=None)


# 2119: REQ-030.3.2
def test_start_sessions_places_dash_f_before_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        calls.append(args)
        return _FakeCompletedProcess(returncode=1)

    cli._start_sessions(run=fake_run)

    assert calls[0][:4] == ["tmux", "-L", "panopticon", "-f"]
    assert Path(calls[0][4]).read_text() == server_default_config_text(clipboard=None)
    for tmux_new in (c for c in calls if "new-session" in c):
        assert tmux_new.index("-f") < tmux_new.index("new-session")


# 2119: runtime-readiness.5.1
def test_start_sessions_skips_an_already_running_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> _FakeCompletedProcess:
        calls.append(args)
        return _FakeCompletedProcess(returncode=0)  # both already running

    cli._start_sessions(run=fake_run)

    assert not any("new-session" in c for c in calls)  # neither bounced


def test_dashboard_under_supervisor_wires_the_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    # With --switch-file (set by the supervisor, ADR 0009 §6) the dashboard is wired with the
    # `t` (on_switch), `s` (on_service), and `u` (on_runner) hooks; the dashboard stays running.
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard,
        "run",
        lambda _c, *, on_switch=None, on_service=None, on_runner=None, artifacts_root=None, draft_file=None: (
            seen.update(
                on_switch=on_switch,
                on_service=on_service,
                on_runner=on_runner,
                draft_file=draft_file,
            )
        ),
    )
    cli.main(["dashboard", "--switch-file", "/tmp/x"], client=_FakeClient())  # type: ignore[arg-type]
    assert (
        seen["on_switch"] is not None
        and seen["on_service"] is not None
        and seen["on_runner"] is not None
    )
    assert seen["draft_file"] == Path("/tmp/new-task-drafts.json")


def test_standalone_dashboard_has_no_switch_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import dashboard

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dashboard,
        "run",
        lambda _c, *, on_switch=None, on_service=None, on_runner=None, artifacts_root=None, draft_file=None: (
            seen.update(
                on_switch=on_switch,
                on_service=on_service,
                on_runner=on_runner,
                draft_file=draft_file,
            )
        ),
    )
    cli.main(["dashboard"], client=_FakeClient())  # type: ignore[arg-type]
    assert seen["on_switch"] is None and seen["on_service"] is None and seen["on_runner"] is None
    assert seen["draft_file"] is None


# 2119: foreground-setup.1.2
# 2119: runtime-readiness.4.13
def test_quickstart_invokes_foreground_steps_without_auth_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from panopticon.terminal import console, doctor, setup
    from panopticon.terminal import quickstart as qs
    from panopticon.terminal.setup_credentials import Connection
    from panopticon.terminal.source_selection import RepositorySource

    calls = []
    monkeypatch.setattr(
        setup,
        "configure_connection",
        lambda: (calls.append("connection"), Connection("codex", "connection.env"))[1],
    )
    monkeypatch.setattr(
        qs,
        "select_source",
        lambda: (
            calls.append("source"),
            RepositorySource("https://example.test/repo", "repo", "remote"),
        )[1],
    )
    monkeypatch.setattr(doctor, "run_checks", list)
    monkeypatch.setattr(doctor, "report", lambda _: (calls.append("doctor"), 0)[1])
    monkeypatch.setattr(
        cli, "_prepare_integrated_runtime", lambda *args: calls.append("runtime") or True
    )
    monkeypatch.setattr(
        qs, "setup_repo", lambda *args, **kwargs: (calls.append("register"), ("repo", "Example"))[1]
    )
    monkeypatch.setattr(
        setup, "configure_repo", lambda *args, **kwargs: calls.append("bind") or True
    )
    monkeypatch.setattr(
        qs,
        "ensure_setup_repo_task",
        lambda *args: pytest.fail("Authentication tasks are not onboarding"),
    )
    joined = {}
    monkeypatch.setattr(
        console,
        "run_console_local",
        lambda *args, **kwargs: (calls.append("console"), joined.update(kwargs)),
    )
    assert cli.main(["quickstart"]) == 0
    assert calls == ["connection", "source", "doctor", "runtime", "register", "bind", "console"]
    assert "join" not in joined


# 2119: REQ-054.2.4
@pytest.mark.parametrize("source", ["missing-checkout", "invalid.bundle"])
def test_invalid_source_prevents_runtime_and_registration(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panopticon.terminal import quickstart, setup
    from panopticon.terminal.setup_credentials import Connection
    from panopticon.terminal.source_selection import select_source

    monkeypatch.chdir(tmp_path)
    if source.endswith(".bundle"):
        (tmp_path / source).write_text("not a bundle")
    monkeypatch.setattr(setup, "configure_connection", lambda: Connection("claude", "saved.env"))
    entries = iter([source])

    def answer(_: str) -> str:
        try:
            return next(entries)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(quickstart, "select_source", lambda: select_source(input_fn=answer))
    monkeypatch.setattr(
        cli,
        "_prepare_integrated_runtime",
        lambda *args: pytest.fail("invalid source started runtime"),
    )
    monkeypatch.setattr(
        quickstart, "setup_repo", lambda *args, **kwargs: pytest.fail("invalid source registered")
    )
    assert cli.main(["quickstart"]) == 1


def test_quickstart_aborts_when_doctor_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from panopticon.terminal import console, doctor, quickstart, setup
    from panopticon.terminal.setup_credentials import Connection
    from panopticon.terminal.source_selection import RepositorySource

    monkeypatch.setattr(setup, "configure_connection", lambda: Connection("claude", "saved.env"))
    monkeypatch.setattr(
        quickstart,
        "select_source",
        lambda: RepositorySource("https://example.test/repo", "repo", "remote"),
    )

    calls: list[str] = []

    monkeypatch.setattr(doctor, "run_checks", list)
    monkeypatch.setattr(doctor, "report", lambda results: 1)
    monkeypatch.setattr(cli, "_run_migrate", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_start_sessions", lambda: calls.append("sessions"))
    monkeypatch.setattr(console, "run_console_local", lambda url, **kw: calls.append("console"))

    rc = cli.main(["quickstart"])
    assert rc == 1
    # A failing doctor aborts before migrations, sessions, or the console.
    assert calls == []
