"""Opt-in clean-host acceptance for the documented new-user journey.

The ordinary test suite does not contact a package index, GitHub, or an agent model. This test is
intended to be run *on a disposable host* whose Docker/tmux namespace is dedicated to the run. It
installs an immutable, reachable artifact into a new pipx home and drives the shipped quickstart and
dashboard through one real ``github-self-reviewed`` task. REST, GitHub, and tmux calls after setup
are observation-only; the PTY carries every task mutation through the documented user interface.

Required inputs are deliberately verbose and have no fallback to a developer's native login or
credential files. See ``docs/getting-started.md`` for the invocation contract.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import pytest

_OPT_IN = "I_AM_RUNNING_ON_A_DISPOSABLE_HOST"
_REQUIRED = (
    "PANOPTICON_NEW_USER_ACCEPTANCE",
    "PANOPTICON_ACCEPTANCE_INSTALL_SPEC",
    "PANOPTICON_ACCEPTANCE_GITHUB_REPO",
    "PANOPTICON_ACCEPTANCE_BASE_SHA",
    "PANOPTICON_ACCEPTANCE_GH_TOKEN",
    "PANOPTICON_ACCEPTANCE_HARNESS",
    "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV",
    "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN",
)
_HARNESS_AUTH_ENV = {
    "claude": frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}),
    "codex": frozenset({"CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"}),
}
_REGISTERED_HARNESSES = frozenset({"claude", "codex", "outfitter", "pi"})
_ADVANCE_COMMAND = {"claude": "/advance", "codex": "$advance"}
_PTY_ROWS = 45
_PTY_COLUMNS = 180
_PTY_BUFFER_LIMIT = 128 * 1024
_DIAGNOSTIC_LIMIT = 12_000
_PINNED_DISTRIBUTION = re.compile(r"panopticon-app==[0-9]+(?:\.[0-9]+){2}[A-Za-z0-9.!+_-]*\Z")
_HASHED_WHEEL = re.compile(
    r"panopticon-app @ https://\S+\.whl#sha256=[0-9a-f]{64}\Z", re.IGNORECASE
)
_PINNED_GIT = re.compile(r"panopticon-app @ git\+https://\S+@[0-9a-f]{40}\Z", re.IGNORECASE)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PASSTHROUGH_ENV = (
    "ALL_PROXY",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "USER",
    "XDG_RUNTIME_DIR",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)


@dataclass(frozen=True)
class LiveConfiguration:
    install_spec: str
    repo_url: str
    base_sha: str
    github_token: str
    harness: str
    harness_auth_env: str
    harness_auth_token: str


class _PtyProcess:
    """One real terminal process with a continuously drained, bounded transcript tail."""

    def __init__(self, pid: int, master_fd: int) -> None:
        self.pid = pid
        self._master_fd = master_fd
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._status: int | None = None
        self._drainer = threading.Thread(
            target=self._drain, name="acceptance-pty-drain", daemon=True
        )
        self._drainer.start()

    @classmethod
    def start(cls, argv: list[str], *, env: Mapping[str, str], cwd: Path) -> _PtyProcess:
        """Fork ``argv`` onto a fixed-size POSIX PTY; ``argv[0]`` resolves through PATH."""
        winsize = struct.pack("HHHH", _PTY_ROWS, _PTY_COLUMNS, 0, 0)
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                os.chdir(cwd)
                os.execvpe(argv[0], argv, dict(env))
            except BaseException:
                os._exit(127)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        return cls(pid, master_fd)

    def _drain(self) -> None:
        while True:
            try:
                chunk = os.read(self._master_fd, 65_536)
            except OSError as exc:
                if exc.errno in {errno.EBADF, errno.EIO}:
                    return
                raise
            if not chunk:
                return
            with self._lock:
                self._buffer.extend(chunk)
                excess = len(self._buffer) - _PTY_BUFFER_LIMIT
                if excess > 0:
                    del self._buffer[:excess]

    def send(self, data: bytes) -> None:
        os.write(self._master_fd, data)

    def tail(self) -> str:
        with self._lock:
            data = bytes(self._buffer)
        return data.decode("utf-8", errors="replace")

    def wait_for_text(self, text: str, *, timeout: float = 60) -> None:
        _wait_until(
            repr(text),
            lambda: text if text in self.tail() else None,
            timeout=timeout,
            interval=0.1,
        )

    def poll(self) -> int | None:
        if self._status is not None:
            return self._status
        waited, status = os.waitpid(self.pid, os.WNOHANG)
        if waited:
            self._status = os.waitstatus_to_exitcode(status)
        return self._status

    def wait(self, *, timeout: float = 30) -> int:
        return int(
            _wait_until(
                "the PTY child to exit",
                self.poll,
                timeout=timeout,
                interval=0.1,
                accept=lambda result: result is not None,
            )
        )

    def terminate(self) -> None:
        if self.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGTERM)
            try:
                self.wait(timeout=5)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.pid, signal.SIGKILL)
                self.wait(timeout=5)
        with contextlib.suppress(OSError):
            os.close(self._master_fd)
        self._drainer.join(timeout=1)


def _configuration(environ: Mapping[str, str]) -> LiveConfiguration | None:
    """Return a live configuration only after every destructive/network gate is explicit."""
    if environ.get("PANOPTICON_NEW_USER_ACCEPTANCE") != _OPT_IN:
        return None
    if any(not environ.get(name) for name in _REQUIRED):
        return None

    install_spec = environ["PANOPTICON_ACCEPTANCE_INSTALL_SPEC"]
    distribution = _PINNED_DISTRIBUTION.fullmatch(install_spec)
    wheel = _HASHED_WHEEL.fullmatch(install_spec)
    git = _PINNED_GIT.fullmatch(install_spec)
    if not any((distribution, wheel, git)):
        return None
    if wheel:
        wheel_url = urlsplit(install_spec.split(" @ ", 1)[1])
        if (
            wheel_url.scheme != "https"
            or wheel_url.username is not None
            or wheel_url.password is not None
            or wheel_url.query
            or re.fullmatch(r"sha256=[0-9a-f]{64}", wheel_url.fragment, re.IGNORECASE) is None
        ):
            return None
    if git:
        git_url, _commit = install_spec.split(" @ ", 1)[1].rsplit("@", 1)
        parsed_git = urlsplit(git_url)
        if (
            parsed_git.scheme != "git+https"
            or parsed_git.username is not None
            or parsed_git.password is not None
            or parsed_git.query
            or parsed_git.fragment
        ):
            return None
    repo_url = environ["PANOPTICON_ACCEPTANCE_GITHUB_REPO"]
    parsed = urlsplit(repo_url)
    repo_parts = parsed.path.removesuffix(".git").strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or len(repo_parts) != 2
        or not all(repo_parts)
        or not repo_parts[1].startswith("panopticon-acceptance-")
    ):
        return None
    base_sha = environ["PANOPTICON_ACCEPTANCE_BASE_SHA"].lower()
    if _SHA.fullmatch(base_sha) is None:
        return None
    harness = environ["PANOPTICON_ACCEPTANCE_HARNESS"]
    auth_env = environ["PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV"]
    if auth_env not in _HARNESS_AUTH_ENV.get(harness, frozenset()):
        return None
    github_token = environ["PANOPTICON_ACCEPTANCE_GH_TOKEN"]
    harness_token = environ["PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN"]
    if any(any(char.isspace() for char in value) for value in (github_token, harness_token)):
        return None
    return LiveConfiguration(
        install_spec=install_spec,
        repo_url=repo_url,
        base_sha=base_sha,
        github_token=github_token,
        harness=harness,
        harness_auth_env=auth_env,
        harness_auth_token=harness_token,
    )


_LIVE_CONFIGURATION = _configuration(os.environ)


def _run(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )


def _wait_until(
    description: str,
    probe: Any,
    *,
    timeout: float = 1_200,
    interval: float = 2,
    accept: Any = bool,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if accept(last):
            return last
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {description}; last observation: {last!r}")


def _github_get(config: LiveConfiguration, path: str) -> Any:
    """Read GitHub state; the live driver has no direct forge mutation primitive."""
    response = httpx.get(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        trust_env=False,
    )
    assert response.status_code == 200, (
        f"GitHub GET {path} returned {response.status_code}: {response.text[:500]}"
    )
    return response.json() if response.content else None


def _github_status(config: LiveConfiguration, path: str) -> int:
    """Read only the status for a GitHub resource whose absence is part of the precondition."""
    response = httpx.get(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        trust_env=False,
    )
    assert response.status_code in {200, 404}, (
        f"GitHub GET {path} returned {response.status_code}: {response.text[:500]}"
    )
    return response.status_code


def _api_get(client: httpx.Client, token: str, path: str) -> Any:
    """Read task-service state; all live mutations must enter through the shipped UI."""
    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    return response.json() if response.content else None


def _capture_pane(
    env: Mapping[str, str], cwd: Path, session: str, *, scrollback: bool = False
) -> str:
    history = ["-S", "-"] if scrollback else []
    result = subprocess.run(
        ["tmux", "-L", "panopticon", "capture-pane", "-p", "-J", *history, "-t", session],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _pane_id(env: Mapping[str, str], cwd: Path, session: str) -> str:
    result = subprocess.run(
        [
            "tmux",
            "-L",
            "panopticon",
            "display-message",
            "-p",
            "-t",
            session,
            "#{pane_id}",
        ],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _client_sessions(env: Mapping[str, str], cwd: Path) -> list[str]:
    """Return the sessions of attached clients without changing tmux state."""
    result = subprocess.run(
        ["tmux", "-L", "panopticon", "list-clients", "-F", "#{client_session}"],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _wait_for_client_session(
    expected: str, *, env: Mapping[str, str], cwd: Path, timeout: float = 120
) -> None:
    _wait_until(
        f"the sole tmux client to attach to {expected!r}",
        lambda: observed if (observed := _client_sessions(env, cwd)) == [expected] else None,
        timeout=timeout,
        interval=0.1,
    )


def _wait_for_pane_text(
    session: str,
    text: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 120,
) -> str:
    return str(
        _wait_until(
            f"{text!r} in tmux session {session!r}",
            lambda: pane if text in (pane := _capture_pane(env, cwd, session)) else None,
            timeout=timeout,
            interval=0.1,
        )
    )


def _wait_for_pane_texts(
    session: str,
    texts: tuple[str, ...],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 120,
) -> str:
    def probe() -> str | None:
        pane = _capture_pane(env, cwd, session)
        return pane if all(text in pane for text in texts) else None

    return str(
        _wait_until(
            f"{texts!r} in tmux session {session!r}",
            probe,
            timeout=timeout,
            interval=0.1,
        )
    )


def _wait_for_pane_row_texts(
    session: str,
    texts: tuple[str, ...],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 120,
) -> str:
    def probe() -> str | None:
        pane = _capture_pane(env, cwd, session)
        return (
            pane if any(all(text in line for text in texts) for line in pane.splitlines()) else None
        )

    return str(
        _wait_until(
            f"one row containing {texts!r} in tmux session {session!r}",
            probe,
            timeout=timeout,
            interval=0.1,
        )
    )


def _advance_command(harness: str) -> str:
    return _ADVANCE_COMMAND[harness]


def _redacted_tail(text: str, tokens: tuple[str, ...], *, limit: int = _DIAGNOSTIC_LIMIT) -> str:
    for token in sorted(set(tokens), key=len, reverse=True):
        if token:
            text = text.replace(token, "[redacted]")
            if len(token) > 4:
                text = text.replace(f"...{token[-4:]}", "...[redacted]")
    return text[-limit:]


def _failure_diagnostics(
    driver: _PtyProcess | None,
    *,
    env: Mapping[str, str],
    cwd: Path,
    tokens: tuple[str, ...],
) -> str:
    sessions = _client_sessions(env, cwd)
    pane = _capture_pane(env, cwd, sessions[0]) if len(sessions) == 1 else ""
    pty_tail = driver.tail() if driver is not None else "<PTY not started>"
    combined = (
        f"tmux client sessions: {sessions!r}\n\ncurrent pane:\n{pane}\n\nPTY tail:\n{pty_tail}"
    )
    return _redacted_tail(combined, tokens)


def _complete_setup_task(
    setup_id: str,
    *,
    config: LiveConfiguration,
    driver: _PtyProcess,
    env: Mapping[str, str],
    cwd: Path,
    client: httpx.Client,
    write_token: str,
) -> None:
    session = f"panopticon-{setup_id}"
    _wait_for_client_session(session, env=env, cwd=cwd)
    responded: set[str] = set()
    last_pane = ""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        task = _api_get(client, write_token, f"/tasks/{setup_id}")
        if task["state"] == "COMPLETE":
            return
        # Setup is a scrolling shell, so prompt detection needs its history. Dashboard checks use
        # only the current viewport so an old Textual frame cannot satisfy a later state wait.
        last_pane = _capture_pane(env, cwd, session, scrollback=True)
        if "A Claude credential is already set" in last_pane and "claude-keep" not in responded:
            driver.send(b"\r")
            responded.add("claude-keep")
        elif "A GH_TOKEN is already set" in last_pane and "gh-keep" not in responded:
            driver.send(b"\r")
            responded.add("gh-keep")
        elif (
            "A CLAUDE_CODE_OAUTH_TOKEN is set" in last_pane
            or "A ANTHROPIC_API_KEY is set" in last_pane
        ) and "claude-adopt" not in responded:
            driver.send(b"y\r")
            responded.add("claude-adopt")
        elif (
            f"A {config.harness_auth_env} is set in your environment" in last_pane
            and "codex-adopt" not in responded
        ):
            driver.send(b"y\r")
            responded.add("codex-adopt")
        elif "Paste a Claude token to store it" in last_pane and "claude-paste" not in responded:
            driver.send(config.harness_auth_token.encode() + b"\r")
            responded.add("claude-paste")
        elif "A GH_TOKEN is set in your environment" in last_pane and "gh-adopt" not in responded:
            driver.send(b"y\r")
            responded.add("gh-adopt")
        elif "Paste a GitHub token to store it" in last_pane and "gh-paste" not in responded:
            driver.send(config.github_token.encode() + b"\r")
            responded.add("gh-paste")
        elif (
            "After updating the repo credentials, press Enter to re-check" in last_pane
            and "recheck" not in responded
        ):
            driver.send(b"\r")
            responded.add("recheck")
        elif (
            "All required task-container credentials are configured." in last_pane
            and "complete" not in responded
        ):
            driver.send(b"\r")
            responded.add("complete")
        time.sleep(0.1)
    pytest.fail(f"setup-repo did not complete; final pane:\n{last_pane[-4000:]}")


def _run_live_new_user_journey(tmp_path: Path, config: LiveConfiguration) -> None:
    for binary in ("docker", "git", "pipx", "tmux", config.harness):
        assert shutil.which(binary), f"{binary} must be installed on the disposable host"
    other_harnesses = _REGISTERED_HARNESSES - {config.harness}
    assert not [name for name in other_harnesses if shutil.which(name)], (
        "the disposable host must expose only the selected harness so quickstart's choice is deterministic"
    )
    config_root = tmp_path / "config"
    data_root = tmp_path / "data"
    cache_root = tmp_path / "cache"
    pipx_home = tmp_path / "pipx-home"
    pipx_bin = tmp_path / "pipx-bin"
    worktree = tmp_path / "disposable-repo"
    recorder_bin = tmp_path / "recorders"
    artifact_log = tmp_path / "xdg-open.log"
    browser_log = tmp_path / "browser.log"
    for path in (
        config_root,
        data_root,
        cache_root,
        pipx_home,
        pipx_bin,
        worktree,
        recorder_bin,
        artifact_log,
        browser_log,
    ):
        assert not path.exists()

    recorder_bin.mkdir()
    opener_script = '#!/bin/sh\nprintf \'%s\\n\' "$1" >> "$PANOPTICON_ACCEPTANCE_XDG_LOG"\n'
    for opener_name in ("open", "xdg-open"):
        opener = recorder_bin / opener_name
        opener.write_text(opener_script)
        opener.chmod(0o700)
    browser = recorder_bin / "record-browser"
    browser.write_text('#!/bin/sh\nprintf \'%s\\n\' "$1" >> "$PANOPTICON_ACCEPTANCE_BROWSER_LOG"\n')
    browser.chmod(0o700)

    env = {
        **{name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ},
        "HOME": str(tmp_path / "home"),
        "PANOPTICON_CONFIG": str(config_root),
        "PANOPTICON_DATA": str(data_root),
        "PANOPTICON_CACHE": str(cache_root),
        "PIPX_HOME": str(pipx_home),
        "PIPX_BIN_DIR": str(pipx_bin),
        config.harness_auth_env: config.harness_auth_token,
        "GH_TOKEN": config.github_token,
        "BROWSER": str(browser),
        "PANOPTICON_ACCEPTANCE_BROWSER_LOG": str(browser_log),
        "PANOPTICON_ACCEPTANCE_XDG_LOG": str(artifact_log),
        "TERM": "xterm-256color",
    }
    Path(env["HOME"]).mkdir(mode=0o700)
    env["PATH"] = os.pathsep.join((str(recorder_bin), env["PATH"]))

    # Prove emptiness in the exact HOME/PATH/Docker/tmux environment the journey uses. A dedicated
    # disposable host has no unrelated containers at all; this deliberately catches unlabeled and
    # legacy Panopticon containers instead of trusting only the current label convention.
    assert subprocess.run(["docker", "info"], env=env, capture_output=True).returncode == 0
    assert (
        subprocess.run(
            ["tmux", "-L", "panopticon", "has-session"], env=env, capture_output=True
        ).returncode
        != 0
    )
    existing_containers = subprocess.run(
        ["docker", "ps", "--all", "--quiet"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not existing_containers, "the dedicated disposable host must begin with no containers"

    owner, repo = urlsplit(config.repo_url).path.removesuffix(".git").strip("/").split("/")
    repository = _github_get(config, f"/repos/{owner}/{repo}")
    assert not repository["archived"]
    assert repository.get("permissions", {}).get("push") is True
    assert repo.startswith("panopticon-acceptance-")
    assert "panopticon-acceptance-disposable" in repository.get("topics", [])
    default_branch = str(repository["default_branch"])
    branch = _github_get(config, f"/repos/{owner}/{repo}/git/ref/heads/{quote(default_branch)}")
    assert branch["object"]["sha"] == config.base_sha, (
        "the disposable repo moved; supply its current base SHA only after reviewing the new state"
    )
    assert (
        _github_status(
            config,
            f"/repos/{owner}/{repo}/contents/hello-panopticon.txt?ref={quote(config.base_sha)}",
        )
        == 404
    ), "the disposable base must not already contain hello-panopticon.txt"

    askpass = tmp_path / "git-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' x-access-token ;; "
        "*) printf '%s\\n' \"$PANOPTICON_ACCEPTANCE_GH_TOKEN\" ;; esac\n"
    )
    askpass.chmod(0o700)
    clone_env = {
        **env,
        "GIT_ASKPASS": str(askpass),
        "GIT_ASKPASS_REQUIRE": "force",
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": config.github_token,
    }
    _run(
        [
            "git",
            "clone",
            "--branch",
            default_branch,
            "--single-branch",
            config.repo_url,
            str(worktree),
        ],
        env=clone_env,
        cwd=tmp_path,
    )
    assert (
        _run(["git", "rev-parse", "HEAD"], env=env, cwd=worktree).stdout.strip() == config.base_sha
    )

    assert shutil.which("panopticon", path=env["PATH"]) is None, (
        "the disposable host PATH must not already expose panopticon"
    )
    _run(["pipx", "install", "--force", config.install_spec], env=env, cwd=tmp_path, timeout=600)
    panopticon = pipx_bin / "panopticon"
    assert panopticon.is_file()
    assert shutil.which("panopticon", path=env["PATH"]) is None
    _run(["pipx", "ensurepath"], env=env, cwd=tmp_path)
    login_shell = env.get("SHELL")
    assert login_shell and Path(login_shell).is_file(), (
        "the clean host must identify its login shell"
    )
    fresh_path = (
        subprocess.run(
            [login_shell, "-ic", "command -v panopticon"],
            env=env,
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        .stdout.strip()
        .splitlines()
    )
    assert fresh_path and fresh_path[-1] == str(panopticon)
    env["PATH"] = os.pathsep.join((str(pipx_bin), env["PATH"]))
    assert shutil.which("panopticon", path=env["PATH"]) == str(panopticon)
    version = _run(["panopticon", "--version"], env=env, cwd=tmp_path).stdout.strip()
    package = _run(
        ["pipx", "runpip", "panopticon-app", "show", "panopticon-app"],
        env=env,
        cwd=tmp_path,
    ).stdout
    package_version = next(
        line.partition(":")[2].strip()
        for line in package.splitlines()
        if line.lower().startswith("version:")
    )
    assert version == f"panopticon {package_version}"
    _run(["panopticon", "doctor"], env=env, cwd=tmp_path)

    driver: _PtyProcess | None = None
    teardown_complete = False
    try:
        # 2119: REQ-054.7.2, REQ-054.7.8
        driver = _PtyProcess.start(["panopticon", "quickstart"], env=env, cwd=worktree)
        secrets_file = config_root / "secrets" / "panopticon.env"
        driver.wait_for_text(
            f"Use {config.harness} as this repo's default harness? Press Enter to continue.",
            timeout=60,
        )
        driver.send(b"\r")

        auth_file = config_root / "secrets" / "task-service-auth.json"
        _wait_until(
            "quickstart's service credential file",
            lambda: auth_file if auth_file.is_file() else None,
            timeout=60,
            interval=0.1,
        )
        for session in ("service", "runner"):
            subprocess.run(
                ["tmux", "-L", "panopticon", "has-session", "-t", session],
                env=env,
                cwd=worktree,
                check=True,
                capture_output=True,
            )

        auth = json.loads(auth_file.read_text())
        write_token = auth["write"][-1]
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30, trust_env=False) as client:
            setup = _wait_until(
                "the setup-repo task",
                lambda: next(
                    (
                        task
                        for task in _api_get(client, write_token, "/tasks")
                        if task["workflow"] == "setup-repo"
                    ),
                    None,
                ),
                timeout=120,
                interval=0.2,
            )
            setup_id = str(setup["id"])
            _complete_setup_task(
                setup_id,
                config=config,
                driver=driver,
                env=env,
                cwd=worktree,
                client=client,
                write_token=write_token,
            )
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            assert secrets_file.is_file() and not secrets_file.is_symlink()
            assert secrets_file.stat().st_mode & 0o077 == 0
            secret_lines = secrets_file.read_text().splitlines()
            assert f"{config.harness_auth_env}={config.harness_auth_token}" in secret_lines
            assert f"GH_TOKEN={config.github_token}" in secret_lines

            fresh_shell = dict(env)
            for name in (
                "PANOPTICON_SERVICE_AUTH_FILE",
                "PANOPTICON_SERVICE_AUTH_MODE",
                "PANOPTICON_SERVICE_AUTH_TOKEN",
            ):
                fresh_shell.pop(name, None)
            listed = _run(["panopticon", "tasks"], env=fresh_shell, cwd=tmp_path)
            assert "COMPLETE" in listed.stdout and "401" not in listed.stdout

            marker = "hello-panopticon.txt"
            content = "hello from Panopticon\n"
            prompt = (
                "Add a hello-panopticon.txt file containing hello from Panopticon and do not change "
                "any other files."
            )
            repos = _api_get(client, write_token, "/repos")
            assert len(repos) == 1, "the fresh quickstart should register exactly its current repo"
            configured_repo = next(
                item for item in repos if item["git_url"].rstrip("/") == config.repo_url.rstrip("/")
            )
            before_ids = {str(task["id"]) for task in _api_get(client, write_token, "/tasks")}
            workflow_infos = _api_get(
                client, write_token, f"/repos/{configured_repo['id']}/workflows"
            )
            workflow_names = [str(item["name"]) for item in workflow_infos]
            assert workflow_names == sorted(workflow_names)
            workflow_index = workflow_names.index("github-self-reviewed")

            # 2119: REQ-054.7.6, REQ-054.7.7
            _wait_for_pane_text("dashboard", "New task", env=env, cwd=worktree)
            dashboard_before_picker = _capture_pane(env, worktree, "dashboard")
            driver.send(b"n")
            _wait_until(
                "the repository picker modal",
                lambda: (
                    pane
                    if (pane := _capture_pane(env, worktree, "dashboard"))
                    != dashboard_before_picker
                    and str(configured_repo["id"]) in pane
                    and re.search(r"(?m)^\s*[│┃]?\s*repo\s*[│┃]?\s*$", pane)
                    else None
                ),
                timeout=30,
                interval=0.1,
            )
            driver.send(b"\r")
            _wait_for_pane_text("dashboard", "github-self-reviewed", env=env, cwd=worktree)
            driver.send((b"\x1b[B" * workflow_index) + b"\r")
            _wait_for_pane_text("dashboard", "enter: submit", env=env, cwd=worktree)
            driver.send(prompt.encode() + b"\r")

            task = _wait_until(
                "the sole dashboard-created task",
                lambda: (
                    created[0]
                    if len(
                        created := [
                            item
                            for item in _api_get(client, write_token, "/tasks")
                            if str(item["id"]) not in before_ids
                        ]
                    )
                    == 1
                    else None
                ),
                timeout=120,
                interval=0.2,
            )
            task_id = str(task["id"])
            assert task["workflow"] == "github-self-reviewed"
            assert task["memo"] == prompt
            assert task["harness"] == config.harness

            live = _wait_until(
                "a live task container",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))[
                        "container_status"
                    ]
                    == "live"
                    else None
                ),
                timeout=600,
            )
            assert live["state"] == "PLANNING"
            _wait_for_pane_row_texts("dashboard", (marker, "live"), env=env, cwd=worktree)

            # The completed setup row remains selected when the new active row appears above it.
            # Move to the new task, attach through `t`, then issue tmux's raw detach chord.
            # 2119: REQ-054.7.8
            _wait_for_pane_text("dashboard", marker, env=env, cwd=worktree)
            dashboard_pane = _pane_id(env, worktree, "dashboard")
            assert dashboard_pane
            driver.send(b"\x1b[A")
            time.sleep(0.2)
            driver.send(b"t")
            task_session = f"panopticon-{task_id}"
            _wait_for_client_session(task_session, env=env, cwd=worktree)
            assert _wait_until(
                "a rendered task pane after attachment",
                lambda: _capture_pane(env, worktree, task_session).strip() or None,
                timeout=30,
                interval=0.1,
            )
            driver.send(b"\x02d")
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            assert _pane_id(env, worktree, "dashboard") == dashboard_pane

            planned = _wait_until(
                "the agent's plan and planning handoff",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["turn"]
                    == "user"
                    and "plan.md" in _api_get(client, write_token, f"/tasks/{task_id}/artifacts")
                    else None
                ),
            )
            plan_response = client.get(
                f"/tasks/{task_id}/artifacts/plan.md",
                headers={"Authorization": f"Bearer {write_token}"},
            )
            plan_response.raise_for_status()
            assert plan_response.text.strip()
            assert marker in plan_response.text
            assert content.strip() in plan_response.text
            assert planned["state"] == "PLANNING"

            # 2119: REQ-054.7.9
            artifact_names = _api_get(client, write_token, f"/tasks/{task_id}/artifacts")
            visible_artifacts = [name for name in artifact_names if not str(name).startswith(".")]
            plan_index = visible_artifacts.index("plan.md")
            driver.send(b"a")
            _wait_for_pane_text("dashboard", "plan.md", env=env, cwd=worktree)
            driver.send((b"\x1b[B" * plan_index) + b"\r")
            opened_artifact = Path(
                _wait_until(
                    "the xdg-open artifact handoff",
                    lambda: (
                        lines[0]
                        if artifact_log.is_file()
                        and len(lines := artifact_log.read_text().splitlines()) == 1
                        else None
                    ),
                    timeout=30,
                    interval=0.1,
                )
            )
            assert opened_artifact.name == "plan.md"
            assert opened_artifact.parent.name == task_id
            assert opened_artifact.read_bytes() == plan_response.content

            # 2119: REQ-054.7.6, REQ-054.7.7
            driver.send(b"t")
            _wait_for_client_session(task_session, env=env, cwd=worktree)
            driver.send(_advance_command(config.harness).encode() + b"\r")
            advanced = _wait_until(
                "the harness command to advance the task to ITERATING",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["state"]
                    == "ITERATING"
                    else None
                ),
            )
            assert advanced["state"] == "ITERATING"
            if _client_sessions(env, worktree) == [task_session]:
                driver.send(b"\x02d")
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            _wait_for_pane_row_texts("dashboard", (marker, "ITERATING"), env=env, cwd=worktree)

            reviewable = _wait_until(
                "a reviewable pull request and ITERATING handoff",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["state"]
                    == "ITERATING"
                    and observed["turn"] == "user"
                    and observed.get("url")
                    else None
                ),
            )
            pr_match = re.fullmatch(
                rf"https://github\.com/{re.escape(owner)}/{re.escape(repo)}/pull/([0-9]+)",
                str(reviewable["url"]),
                re.IGNORECASE,
            )
            assert pr_match is not None
            pr_number = int(pr_match.group(1))

            # 2119: REQ-054.7.9
            driver.send(b"p")
            opened_url = _wait_until(
                "the BROWSER pull-request handoff",
                lambda: (
                    lines[0]
                    if browser_log.is_file()
                    and len(lines := browser_log.read_text().splitlines()) == 1
                    else None
                ),
                timeout=30,
                interval=0.1,
            )
            assert opened_url == reviewable["url"]

            changed_files = _github_get(config, f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
            assert [item["filename"] for item in changed_files] == [marker]
            assert changed_files[0]["status"] == "added"
            pull = _github_get(config, f"/repos/{owner}/{repo}/pulls/{pr_number}")
            blob = _github_get(
                config,
                f"/repos/{owner}/{repo}/contents/{quote(marker)}?ref={pull['head']['sha']}",
            )
            assert base64.b64decode(blob["content"]).decode() == content

            # 2119: REQ-054.7.6, REQ-054.7.7
            driver.send(b"t")
            _wait_for_client_session(task_session, env=env, cwd=worktree)
            driver.send(_advance_command(config.harness).encode() + b"\r")

            def observed_merging_history() -> Any:
                observed = _api_get(client, write_token, f"/tasks/{task_id}")
                return (
                    observed
                    if any(entry["to_state"] == "MERGING" for entry in observed["history"])
                    else None
                )

            merging = _wait_until(
                "a read-side history observation of the MERGING transition",
                observed_merging_history,
            )
            assert any(entry["to_state"] == "MERGING" for entry in merging["history"])
            if _client_sessions(env, worktree) == [task_session]:
                driver.send(b"\x02d")

            complete = _wait_until(
                "the merged pull request and COMPLETE task",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["state"]
                    == "COMPLETE"
                    else None
                ),
            )
            assert complete["container_status"] == "–"
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            assert _pane_id(env, worktree, "dashboard") == dashboard_pane
            _wait_for_pane_row_texts("dashboard", (marker, "COMPLETE"), env=env, cwd=worktree)
            merged_pull = _github_get(config, f"/repos/{owner}/{repo}/pulls/{pr_number}")
            assert merged_pull["merged_at"] is not None
            merged_blob = _github_get(
                config,
                f"/repos/{owner}/{repo}/contents/{quote(marker)}?ref={quote(default_branch)}",
            )
            assert base64.b64decode(merged_blob["content"]).decode() == content

            # Follow the documented teardown from a second shell while the dashboard is still
            # attached, then prove both the first stop and its idempotent repeat succeed.
            _run(["panopticon", "stop"], env=env, cwd=tmp_path)
            assert driver.wait(timeout=30) is not None
            assert not subprocess.run(
                ["docker", "ps", "--all", "--quiet"],
                env=env,
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert (
                subprocess.run(
                    ["tmux", "-L", "panopticon", "has-session"],
                    env=env,
                    cwd=tmp_path,
                    capture_output=True,
                ).returncode
                != 0
            )
            _run(["panopticon", "stop"], env=env, cwd=tmp_path)
            assert not subprocess.run(
                ["docker", "ps", "--all", "--quiet"],
                env=env,
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert (
                subprocess.run(
                    ["tmux", "-L", "panopticon", "has-session"],
                    env=env,
                    cwd=tmp_path,
                    capture_output=True,
                ).returncode
                != 0
            )
            teardown_complete = True
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        diagnostic = _failure_diagnostics(
            driver,
            env=env,
            cwd=worktree,
            tokens=(config.github_token, config.harness_auth_token),
        )
        message = _redacted_tail(
            f"live new-user journey failed: {exc}\n\n{diagnostic}",
            (config.github_token, config.harness_auth_token),
        )
        raise AssertionError(message) from exc
    finally:
        if not teardown_complete:
            subprocess.run(["panopticon", "stop"], env=env, cwd=tmp_path, capture_output=True)
        if driver is not None:
            driver.terminate()

    assert not subprocess.run(
        ["docker", "ps", "--all", "--quiet"],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        subprocess.run(
            ["tmux", "-L", "panopticon", "has-session"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
        ).returncode
        != 0
    )


def test_complete_configuration_enables_clean_host_acceptance() -> None:
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": "panopticon-app==1.2.3",
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }
    assert _configuration(values) is not None


@pytest.mark.parametrize(("harness", "command"), [("claude", "/advance"), ("codex", "$advance")])
def test_live_approval_command_matches_the_selected_harness(harness: str, command: str) -> None:
    assert _advance_command(harness) == command


def test_live_diagnostics_are_bounded_and_redact_both_credentials() -> None:
    github_token = "github-secret"
    harness_token = "harness-secret"
    diagnostic = _redacted_tail(
        ("x" * 200)
        + f" before {github_token} middle {harness_token} "
        + f"masked ...{github_token[-4:]} and ...{harness_token[-4:]} after",
        (github_token, harness_token),
        limit=80,
    )
    assert len(diagnostic) == 80
    assert github_token not in diagnostic
    assert harness_token not in diagnostic
    assert f"...{github_token[-4:]}" not in diagnostic
    assert f"...{harness_token[-4:]}" not in diagnostic


def test_pty_process_has_the_fixed_terminal_size_and_drains_output(tmp_path: Path) -> None:
    process = _PtyProcess.start(
        ["sh", "-c", "stty size; printf 'drained-output\\n'"],
        env={**os.environ, "TERM": "xterm-256color"},
        cwd=tmp_path,
    )
    try:
        process.wait_for_text("45 180", timeout=5)
        process.wait_for_text("drained-output", timeout=5)
        assert process.wait(timeout=5) == 0
    finally:
        process.terminate()


@pytest.mark.parametrize("missing", _REQUIRED)
def test_clean_host_acceptance_requires_every_explicit_input(missing: str) -> None:
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": "panopticon-app==1.2.3",
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }
    values.pop(missing)
    assert _configuration(values) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PANOPTICON_NEW_USER_ACCEPTANCE", "1"),
        ("PANOPTICON_ACCEPTANCE_INSTALL_SPEC", "panopticon-app"),
        ("PANOPTICON_ACCEPTANCE_INSTALL_SPEC", "panopticon-app @ git+https://github.com/a/b@main"),
        (
            "PANOPTICON_ACCEPTANCE_INSTALL_SPEC",
            "panopticon-app @ https://token@example.com/panopticon_app-1.2.3-py3-none-any.whl#sha256="
            + "a" * 64,
        ),
        (
            "PANOPTICON_ACCEPTANCE_INSTALL_SPEC",
            "panopticon-app @ git+https://token@example.com/a/b@" + "a" * 40,
        ),
        ("PANOPTICON_ACCEPTANCE_GITHUB_REPO", "https://token@github.com/acme/repo"),
        ("PANOPTICON_ACCEPTANCE_GITHUB_REPO", "https://gitlab.com/acme/repo"),
        ("PANOPTICON_ACCEPTANCE_GITHUB_REPO", "https://github.com/acme/ordinary-repo"),
        ("PANOPTICON_ACCEPTANCE_BASE_SHA", "main"),
        ("PANOPTICON_ACCEPTANCE_HARNESS", "unknown"),
        # Pi has no MCP-backed GitHub workflow approval path yet; keep this complete journey honest.
        ("PANOPTICON_ACCEPTANCE_HARNESS", "pi"),
        ("PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV", "GH_TOKEN"),
        ("PANOPTICON_ACCEPTANCE_GH_TOKEN", "line one\nline two"),
    ],
)
def test_clean_host_acceptance_rejects_ambiguous_or_unsafe_configuration(
    key: str, value: str
) -> None:
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": "panopticon-app==1.2.3",
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }
    values[key] = value
    assert _configuration(values) is None


# 2119: REQ-054.7.1, REQ-054.7.2, REQ-054.7.6, REQ-054.7.7, REQ-054.7.8, REQ-054.7.9
def test_new_user_completes_one_real_github_self_reviewed_task(tmp_path: Path) -> None:
    if _LIVE_CONFIGURATION is None:
        pytest.skip("set every documented clean-host acceptance input and disposable-host opt-in")
    _run_live_new_user_journey(tmp_path, _LIVE_CONFIGURATION)


def test_complete_live_configuration_enters_the_real_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LiveConfiguration(
        install_spec="panopticon-app==1.2.3",
        repo_url="https://github.com/acme/panopticon-acceptance-disposable.git",
        base_sha="a" * 40,
        github_token="github-token",
        harness="codex",
        harness_auth_env="OPENAI_API_KEY",
        harness_auth_token="model-token",
    )
    entered: list[tuple[Path, LiveConfiguration]] = []
    monkeypatch.setitem(globals(), "_LIVE_CONFIGURATION", config)
    monkeypatch.setitem(
        globals(),
        "_run_live_new_user_journey",
        lambda path, selected: entered.append((path, selected)),
    )

    test_new_user_completes_one_real_github_self_reviewed_task(tmp_path)

    assert entered == [(tmp_path, config)]
