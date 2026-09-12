"""Real terminal entrypoint checks without providers or fleet infrastructure."""

# 2119-spec: foreground-setup
from __future__ import annotations

import errno
import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

from panopticon.terminal.setup_credentials import setup_lock
from panopticon.workflows.setup_repo import SetupRepo


# 2119: 1.1, 3.5
def test_setup_in_real_terminal_without_docker_tmux_or_service(tmp_path: Path) -> None:
    config = tmp_path / "config"
    environment = {
        "HOME": str(tmp_path),
        "PANOPTICON_CONFIG": str(config),
        "PATH": "/nonexistent",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "panopticon.terminal", "setup"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        cwd=tmp_path,
        start_new_session=True,
    )
    os.close(slave)
    transcript = bytearray()
    deadline = time.monotonic() + 10
    selected = pasted = False
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                transcript.extend(chunk)
                if not selected and b"Agent [1]:" in transcript:
                    os.write(master, b"1\n")
                    selected = True
                if not pasted and b"Paste a Claude token" in transcript:
                    os.write(master, b"disposable-terminal-test\n")
                    pasted = True
            if process.poll() is not None:
                break
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)
    assert selected and pasted
    assert b"disposable-terminal-test" not in transcript
    assert b"echoed" not in transcript
    assert b"configured locally" in transcript
    records = json.loads((config / "secrets" / "connections.json").read_text())
    saved = config / "secrets" / records["claude"]["env_file"]
    assert saved.read_text() == "CLAUDE_CODE_OAUTH_TOKEN=disposable-terminal-test\n"
    assert not (tmp_path / ".local" / "share" / "panopticon").exists()


# 2119: 3.8
def test_legacy_workflow_cannot_prompt_while_foreground_lock_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PANOPTICON_CONFIG", str(tmp_path / "config"))
    environment = {**os.environ, "PANOPTICON_PYTHON": sys.executable}
    with setup_lock():
        result = subprocess.run(
            ["sh", "-c", SetupRepo().shell_script()],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
        )
    assert result.returncode != 0
    assert "Another setup is active" in result.stdout
    assert "Paste" not in result.stdout
    assert not (tmp_path / "config" / "secrets" / "connections.json").exists()
