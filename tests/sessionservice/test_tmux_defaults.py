"""REQ-030: panopticon's shipped tmux server defaults (mouse, scrollback, clipboard wiring) for
its dedicated ``-L panopticon`` socket. Real-tmux tests exercise production startup and actual
mouse gestures through a PTY, capturing clipboard bytes without touching the operator clipboard.
Pure-function tests cover config generation and clipboard-tool resolution."""

from __future__ import annotations

import os
import pty
import select
import shlex
import shutil
import subprocess
import tempfile
import termios
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from panopticon.sessionservice import tmux_defaults
from panopticon.sessionservice.tmux_defaults import (
    HISTORY_LIMIT,
    clipboard_tool,
    default_config_path,
    new_session_argv,
    server_default_config_text,
    write_default_config,
)


def _which(present: set[str]):
    return lambda tool: f"/usr/bin/{tool}" if tool in present else None


# 2119: REQ-030.4.1
def test_clipboard_tool_selects_pbcopy_on_darwin_when_present() -> None:
    assert clipboard_tool(platform="darwin", which=_which({"pbcopy"})) == "pbcopy"
    assert (
        clipboard_tool(platform="darwin", which=_which({"pbcopy", "wl-copy", "xclip", "xsel"}))
        == "pbcopy"
    )


# 2119: REQ-030.4.1
def test_clipboard_tool_default_which_reflects_a_monkeypatched_shutil_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `which` must be resolved *inside* the call, not bound as an eager default argument — an
    # eager default would capture the real shutil.which once at import and never see this patch,
    # so every call site that omits `which=` (all of them) would silently ignore it.
    monkeypatch.setattr(
        "shutil.which", lambda tool: "/usr/bin/pbcopy" if tool == "pbcopy" else None
    )
    assert clipboard_tool(platform="darwin") == "pbcopy"
    monkeypatch.setattr("shutil.which", lambda _tool: None)
    assert clipboard_tool(platform="darwin") is None


# 2119: REQ-030.4.2
def test_clipboard_tool_selects_nothing_on_darwin_without_pbcopy() -> None:
    assert clipboard_tool(platform="darwin", which=_which(set())) is None
    # a linux-only tool present doesn't leak into the darwin resolution
    assert clipboard_tool(platform="darwin", which=_which({"wl-copy", "xclip", "xsel"})) is None


# 2119: REQ-030.4.3
@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_clipboard_tool_selects_wl_copy_on_linux_when_present(platform: str) -> None:
    assert clipboard_tool(platform=platform, which=_which({"wl-copy"})) == "wl-copy"
    # wl-copy wins even when the other candidates are also present
    assert (
        clipboard_tool(platform=platform, which=_which({"wl-copy", "xclip", "xsel"})) == "wl-copy"
    )


# 2119: REQ-030.4.4
@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_clipboard_tool_falls_back_to_xclip_without_wl_copy(platform: str) -> None:
    assert (
        clipboard_tool(platform=platform, which=_which({"xclip"})) == "xclip -selection clipboard"
    )
    assert (
        clipboard_tool(platform=platform, which=_which({"xclip", "xsel"}))
        == "xclip -selection clipboard"
    )


# 2119: REQ-030.4.5
@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_clipboard_tool_falls_back_to_xsel_without_wl_copy_or_xclip(platform: str) -> None:
    assert clipboard_tool(platform=platform, which=_which({"xsel"})) == "xsel --clipboard --input"


# 2119: REQ-030.4.6
@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_clipboard_tool_selects_nothing_on_linux_with_no_candidate_present(platform: str) -> None:
    assert clipboard_tool(platform=platform, which=_which(set())) is None
    # a darwin-only tool present doesn't leak into the non-darwin resolution
    assert clipboard_tool(platform=platform, which=_which({"pbcopy"})) is None


# 2119: REQ-030.1.1
# 2119: REQ-030.1.2
# 2119: REQ-030.1.3
def test_server_default_config_text_sets_mouse_history_and_clipboard_options() -> None:
    lines = server_default_config_text(clipboard="pbcopy").splitlines()
    assert "set-option -g mouse on" in lines
    assert f"set-option -g history-limit {HISTORY_LIMIT}" in lines
    assert HISTORY_LIMIT == "50000"
    assert "set-option -g set-clipboard on" in lines


# 2119: REQ-030.2.1
def test_server_default_config_text_binds_copy_mode_drag_release_to_the_clipboard_tool() -> None:
    lines = server_default_config_text(clipboard="pbcopy").splitlines()
    assert (
        'bind-key -T copy-mode MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "pbcopy"'
        in lines
    )


# 2119: REQ-030.2.2
def test_server_default_config_text_binds_copy_mode_vi_drag_release_to_the_clipboard_tool() -> None:
    lines = server_default_config_text(clipboard="xclip -selection clipboard").splitlines()
    assert (
        "bind-key -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-pipe-and-cancel "
        '"xclip -selection clipboard"'
    ) in lines


# 2119: REQ-030.2.3
# 2119: REQ-030.2.4
def test_server_default_config_text_binds_double_click_to_select_word_then_copy() -> None:
    lines = server_default_config_text(clipboard="pbcopy").splitlines()
    double_click = next(line for line in lines if "DoubleClick1Pane" in line)
    # copy-mode -M ; select the double-clicked word ; copy it — three chained sub-commands bound
    # to one key (config-file syntax, \\; between them), so the word selection (2.3) and its copy
    # (2.4) are both this one binding's action.
    assert double_click == (
        "bind-key -T root DoubleClick1Pane copy-mode -M \\; send-keys -X select-word \\; "
        'send-keys -X copy-pipe-and-cancel "pbcopy"'
    )


# 2119: REQ-030.2.5
def test_server_default_config_text_falls_back_to_plain_copy_without_a_clipboard_tool() -> None:
    lines = server_default_config_text(clipboard=None).splitlines()
    # every copy action becomes a plain in-tmux copy — no external command, no clipboard argument
    assert "bind-key -T copy-mode MouseDragEnd1Pane send-keys -X copy-selection-and-cancel" in lines
    assert (
        "bind-key -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-selection-and-cancel"
    ) in lines
    double_click = next(line for line in lines if "DoubleClick1Pane" in line)
    assert double_click.endswith("send-keys -X copy-selection-and-cancel")
    assert not any("copy-pipe-and-cancel" in line for line in lines)


def test_default_config_path_is_deterministic_per_socket(tmp_path: Path) -> None:
    a = default_config_path("panopticon", directory=tmp_path)
    b = default_config_path("panopticon", directory=tmp_path)
    assert a == b  # same socket -> same path, so repeated writes overwrite rather than litter
    other = default_config_path("other-socket", directory=tmp_path)
    assert other != a


def test_write_default_config_writes_the_resolved_text(tmp_path: Path) -> None:
    path = write_default_config("panopticon", directory=tmp_path, clipboard="pbcopy")
    assert path == default_config_path("panopticon", directory=tmp_path)
    assert path.read_text() == server_default_config_text(clipboard="pbcopy")


def test_write_default_config_resolves_clipboard_when_not_given(tmp_path: Path) -> None:
    path = write_default_config(
        "panopticon", directory=tmp_path, platform="darwin", which=_which({"pbcopy"})
    )
    assert 'copy-pipe-and-cancel "pbcopy"' in path.read_text()


def test_write_default_config_overwrites_via_atomic_rename_leaving_no_temp_files(
    tmp_path: Path,
) -> None:
    # A repeated write (every spawn/attach calls this) must fully replace the previous content —
    # and never leave the mkstemp scratch file it used to get there behind in the directory.
    write_default_config("panopticon", directory=tmp_path, clipboard="pbcopy")
    path = write_default_config(
        "panopticon", directory=tmp_path, clipboard="xclip -selection clipboard"
    )
    assert path.read_text() == server_default_config_text(clipboard="xclip -selection clipboard")
    assert list(tmp_path.iterdir()) == [path]  # only the final file — no leftover .tmp scratch


# -- integration: real tmux, isolated from operator sessions and clipboard ---------

_HAVE_TMUX = bool(shutil.which("tmux"))


@dataclass
class _TmuxServer:
    root: Path
    environment: dict[str, str]
    socket: str = "defaults"

    def run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["tmux", "-L", self.socket, *arguments],
            capture_output=True,
            env=self.environment,
            check=check,
            timeout=5,
        )

    def create(self, name: str = "test", command: str = "sleep 30") -> None:
        self.run(*new_session_argv(self.socket, "-d", "-s", name, command))


@pytest.fixture
def tmux_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_TmuxServer]:
    # Short, unique socket paths also fit Darwin's Unix-domain socket length limit.
    with tempfile.TemporaryDirectory(prefix="pn-tmux-", dir="/tmp") as directory:
        root = Path(directory)
        environment = {
            "PATH": os.environ["PATH"],
            "HOME": directory,
            "XDG_CONFIG_HOME": directory,
            "TMUX_TMPDIR": directory,
            "TERM": "xterm-256color",
        }
        monkeypatch.setattr(
            tmux_defaults, "default_config_path", lambda socket, **_: root / f"{socket}.conf"
        )
        # Never invoke the operator's real clipboard tools. Individual copy tests replace this
        # dependency with a capture command; the constructor/config/bindings remain production.
        monkeypatch.setattr(tmux_defaults, "clipboard_tool", lambda **_: None)
        server = _TmuxServer(root, environment)
        try:
            yield server
        finally:
            server.run("kill-server", check=False)


@contextmanager
def _attached_terminal(server: _TmuxServer) -> Iterator[int]:
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 80))
    client = subprocess.Popen(
        ["tmux", "-L", server.socket, "attach-session", "-t", "test"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=server.environment,
    )
    os.close(slave)
    try:
        # Wait for tmux to enable SGR mouse reporting and paint the text before clicking.
        output = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.02)[0]:
                output.extend(os.read(master, 65536))
            if b"\x1b[?1006h" in output and b"alpha bravo charlie" in output:
                break
        else:
            pytest.fail(f"tmux client did not become ready: {bytes(output)!r}")
        yield master
    finally:
        # Only our attached client and isolated server are stopped. OSC 52 remains inside this
        # PTY, so even no-tool copying cannot change the terminal application's clipboard.
        client.terminate()
        try:
            client.wait(timeout=5)
        finally:
            if client.poll() is None:
                client.kill()
                client.wait(timeout=5)
            os.close(master)


def _exercise_mouse_copy(
    server: _TmuxServer, *, mode: str, gesture: str, capture: Path | None
) -> None:
    server.create(command="printf 'alpha bravo charlie\\n'; sleep 30")
    server.run("set-window-option", "-t", "test", "mode-keys", mode)
    # A stale or empty buffer cannot accidentally satisfy the byte-for-byte assertion.
    server.run("set-buffer", "previous-buffer")
    with _attached_terminal(server) as terminal:
        if gesture == "drag":
            # SGR coordinates are one-based. Vi includes its endpoint; emacs excludes it.
            # Both gestures therefore select exactly the five letters at columns 7 through 11.
            endpoint = 11 if mode == "vi" else 12
            os.write(terminal, f"\x1b[<0;7;1M\x1b[<32;8;1M\x1b[<32;{endpoint};1M".encode())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (
                    server.run("display-message", "-t", "test", "-p", "#{pane_in_mode}").stdout
                    == b"1\n"
                ):
                    break
                time.sleep(0.02)
            else:
                pytest.fail("mouse drag did not enter copy-mode")
            assert server.run("save-buffer", "-").stdout == b"previous-buffer"
            if capture is not None:
                assert not capture.exists(), "copy must happen on release"
            os.write(terminal, f"\x1b[<0;{endpoint};1m".encode())
        else:
            # Two press/release pairs at column 9, within bravo. Actual mouse input exercises
            # root DoubleClick1Pane dispatch and the complete chained binding.
            os.write(terminal, b"\x1b[<0;9;1M\x1b[<0;9;1m\x1b[<0;9;1M\x1b[<0;9;1m")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.02)[0]:
                os.read(terminal, 65536)
            buffer = server.run("save-buffer", "-").stdout
            mode_active = server.run(
                "display-message", "-t", "test", "-p", "#{pane_in_mode}"
            ).stdout
            captured = capture.read_bytes() if capture is not None and capture.exists() else None
            if (
                buffer == b"bravo"
                and mode_active == b"0\n"
                and (capture is None or captured == b"bravo")
            ):
                break
        assert buffer == b"bravo"
        assert mode_active == b"0\n"
        if capture is not None:
            assert captured == b"bravo"


# 2119: REQ-030.2.1
# 2119: REQ-030.2.2
# 2119: REQ-030.2.3
# 2119: REQ-030.2.4
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
@pytest.mark.parametrize("mode", ["emacs", "vi"])
@pytest.mark.parametrize("gesture", ["drag", "double-click"])
def test_real_mouse_gesture_copies_exact_bytes_to_clipboard_tool(
    tmux_server: _TmuxServer, monkeypatch: pytest.MonkeyPatch, mode: str, gesture: str
) -> None:
    capture = tmux_server.root / "clipboard-stdin"
    monkeypatch.setattr(
        tmux_defaults, "clipboard_tool", lambda **_: f"/bin/cat > {shlex.quote(str(capture))}"
    )
    _exercise_mouse_copy(tmux_server, mode=mode, gesture=gesture, capture=capture)


# 2119: REQ-030.2.5
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
@pytest.mark.parametrize("mode", ["emacs", "vi"])
@pytest.mark.parametrize("gesture", ["drag", "double-click"])
def test_real_mouse_gesture_without_clipboard_tool_copies_exact_paste_buffer(
    tmux_server: _TmuxServer, mode: str, gesture: str
) -> None:
    _exercise_mouse_copy(tmux_server, mode=mode, gesture=gesture, capture=None)


# 2119: REQ-030.1.1
# 2119: REQ-030.3.1
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_production_new_session_repairs_defaults_on_an_existing_server(
    tmux_server: _TmuxServer,
) -> None:
    tmux = tmux_server.run
    tmux("new-session", "-d", "-s", "keeper", "sleep", "30")
    tmux("set-option", "-g", "mouse", "off")
    tmux("set-option", "-g", "history-limit", "123")
    tmux("set-option", "-g", "set-clipboard", "off")
    tmux("unbind-key", "-T", "root", "DoubleClick1Pane")

    tmux_server.create("corrected")
    assert tmux("show-options", "-g", "mouse").stdout.strip() == b"mouse on"
    assert tmux("show-options", "-g", "set-clipboard").stdout.strip() == b"set-clipboard on"
    assert (
        tmux("display-message", "-t", "corrected", "-p", "#{history_limit}").stdout.strip()
        == HISTORY_LIMIT.encode()
    )
    binding = tmux("list-keys", "-T", "root").stdout
    assert any(
        b"DoubleClick1Pane" in line and b"select-word" in line for line in binding.splitlines()
    )


# 2119: REQ-030.1.1
# 2119: REQ-030.1.2
# 2119: REQ-030.1.3
# 2119: REQ-030.2.3
# 2119: REQ-030.2.4
# 2119: REQ-030.3.1
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_config_loads_correctly_via_production_constructor_on_a_fresh_socket(
    tmux_server: _TmuxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tmux_defaults, "clipboard_tool", lambda **_: "cat")
    tmux_server.create()
    tmux = tmux_server.run
    assert tmux("show-options", "-g", "mouse").stdout.strip() == b"mouse on"
    assert (
        tmux("show-options", "-g", "history-limit").stdout.strip()
        == f"history-limit {HISTORY_LIMIT}".encode()
    )
    assert tmux("show-options", "-g", "set-clipboard").stdout.strip() == b"set-clipboard on"
    assert tmux("show-options", "-A", "-t", "test", "mouse").stdout.strip() == b"mouse* on"
    assert (
        tmux("show-options", "-A", "-t", "test", "set-clipboard").stdout.strip()
        == b"set-clipboard on"
    )
    assert (
        tmux("display-message", "-t", "test", "-p", "#{history_limit}").stdout.strip()
        == HISTORY_LIMIT.encode()
    )
    keys = tmux("list-keys", "-T", "root").stdout
    double_click = next(line for line in keys.splitlines() if b"DoubleClick1Pane" in line)
    assert b"copy-mode" in double_click
    assert b"select-word" in double_click
    assert b"copy-pipe-and-cancel" in double_click


# 2119: REQ-030.5.2
@pytest.mark.skipif(not _HAVE_TMUX, reason="needs tmux")
def test_production_constructor_skips_the_operators_own_tmux_conf(
    tmux_server: _TmuxServer,
) -> None:
    (tmux_server.root / ".tmux.conf").write_text(
        'set-option -g status-left "OPERATORS-OWN-CONFIG"\n'
    )
    control = _TmuxServer(tmux_server.root, tmux_server.environment, socket="control")
    try:
        # Negative control proves the fake operator config is discoverable on this tmux build.
        control.run("new-session", "-d", "-s", "control", "sleep", "30")
        assert b"OPERATORS-OWN-CONFIG" in control.run("show-options", "-g", "status-left").stdout
        tmux_server.create()
        assert (
            b"OPERATORS-OWN-CONFIG"
            not in tmux_server.run("show-options", "-g", "status-left").stdout
        )
    finally:
        control.run("kill-server", check=False)
