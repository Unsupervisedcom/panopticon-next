"""Hermetic process environment for the test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_operator_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests opt into service auth explicitly; never consume the operator's live configuration."""
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_FILE", raising=False)
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_MODE", raising=False)
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PANOPTICON_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def _leave_auth_bootstrap_to_focused_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep legacy CLI-dispatch tests focused on their original subsystem boundary."""
    if request.node.path.name not in {"test_cli.py", "test_terminal.py"}:
        return
    from panopticon.terminal import __main__ as cli

    monkeypatch.setattr(cli, "_ensure_integrated_auth", lambda: None)


@pytest.fixture(autouse=True)
def _isolate_legacy_cli_dispatch_boundaries(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Old dispatch tests inject runtime I/O; dedicated readiness tests exercise the guard."""
    if request.node.path.name not in {"test_cli.py", "test_terminal.py"}:
        return
    from unittest.mock import MagicMock

    from panopticon.terminal import __main__ as cli
    from panopticon.terminal import runtime

    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path / "config"))
    monkeypatch.setattr(
        runtime, "guard_before_migration", lambda _: runtime.MigrationDecision.SERVICE_ABSENT
    )
    monkeypatch.setattr(runtime, "wait_until_ready", lambda _: None)
    monkeypatch.setattr(cli, "_make_client", lambda _: MagicMock())
