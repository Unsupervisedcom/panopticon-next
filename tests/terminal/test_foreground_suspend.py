"""Foreground terminal suspension and interrupt handling at the real PTY boundary."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import textwrap
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from panopticon.terminal.foreground_suspend import run_suspended


class _SuspendRecorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    @contextmanager
    def suspend(self) -> Iterator[None]:
        self.events.append("suspended")
        yield
        self.events.append("resumed")


def test_callback_exception_is_propagated_only_after_terminal_resume() -> None:
    app = _SuspendRecorder()

    def fail() -> None:
        app.events.append("callback")
        raise ValueError("invalid setup input")

    with pytest.raises(ValueError, match="invalid setup input"):
        run_suspended(app, fail)

    assert app.events == ["suspended", "callback", "resumed"]


def test_default_interrupt_handler_is_scoped_to_callback_inside_asyncio() -> None:
    app = _SuspendRecorder()

    async def exercise() -> object:
        asyncio_handler = signal.getsignal(signal.SIGINT)
        callback_handler = run_suspended(app, lambda: signal.getsignal(signal.SIGINT))
        assert signal.getsignal(signal.SIGINT) is asyncio_handler
        return callback_handler

    assert asyncio.run(exercise()) is signal.default_int_handler


def _read_until(master: int, expected: bytes, *, timeout: float) -> bytearray:
    transcript = bytearray()
    deadline = time.monotonic() + timeout
    while expected not in transcript and time.monotonic() < deadline:
        if not select.select([master], [], [], 0.05)[0]:
            continue
        try:
            chunk = os.read(master, 8192)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        transcript.extend(chunk)
    return transcript


def _drain_until_exit(master: int, process: subprocess.Popen[bytes], *, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([master], [], [], 0.05)[0]:
            try:
                os.read(master, 8192)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
        returncode = process.poll()
        if returncode is not None:
            return returncode
    raise subprocess.TimeoutExpired(process.args, timeout)


def _child_terminal() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(0, os.getpgrp())


def _exercise_real_textual_prompt(tmp_path: Path, *, mode: str) -> None:
    marker = tmp_path / mode
    script = tmp_path / "dashboard_prompt.py"
    script.write_text(
        textwrap.dedent(
            """
            import asyncio
            import getpass
            import sys
            from pathlib import Path

            from textual.app import App, ComposeResult
            from textual.widgets import Static

            from panopticon.terminal.foreground_suspend import run_suspended


            class PromptDashboard(App[None]):
                BINDINGS = [("q", "quit", "Quit")]

                def compose(self) -> ComposeResult:
                    yield Static("dashboard")

                def on_mount(self) -> None:
                    self.set_timer(0.1, self.action_setup)

                def prompt(self) -> str:
                    if sys.argv[2] == "interrupt":
                        return getpass.getpass("Token: ")
                    input("Value: ")
                    raise ValueError("invalid setup input")

                def action_setup(self) -> None:
                    try:
                        run_suspended(self, self.prompt)
                    except (KeyboardInterrupt, ValueError) as exc:
                        marker = Path(sys.argv[1])
                        marker.write_text(type(exc).__name__)
                        asyncio.get_running_loop().call_later(
                            0.1,
                            lambda: marker.with_suffix(".live").write_text("live"),
                        )


            PromptDashboard().run()
            """
        )
    )
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, str(script), str(marker), mode],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=_child_terminal,
        close_fds=True,
    )
    os.close(slave)
    try:
        prompt = b"Token:" if mode == "interrupt" else b"Value:"
        transcript = _read_until(master, prompt, timeout=5)
        assert prompt in transcript

        os.write(master, b"\x03" if mode == "interrupt" else b"\n")
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        expected = "KeyboardInterrupt" if mode == "interrupt" else "ValueError"
        assert marker.read_text() == expected
        assert process.poll() is None

        resumed_output = _read_until(master, b"\x1b[?1049h", timeout=2)
        assert b"\x1b[?1049h" in resumed_output
        live_marker = marker.with_suffix(".live")
        deadline = time.monotonic() + 2
        while not live_marker.exists() and time.monotonic() < deadline:
            if select.select([master], [], [], 0.02)[0]:
                os.read(master, 8192)
        assert live_marker.read_text() == "live"

        os.write(master, b"q")
        assert _drain_until_exit(master, process, timeout=2) == 0
    finally:
        os.close(master)
        if process.poll() is None:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def test_one_ctrl_c_returns_real_textual_pty_to_live_dashboard(tmp_path: Path) -> None:
    _exercise_real_textual_prompt(tmp_path, mode="interrupt")


def test_setup_error_returns_real_textual_pty_to_live_dashboard(tmp_path: Path) -> None:
    _exercise_real_textual_prompt(tmp_path, mode="error")
