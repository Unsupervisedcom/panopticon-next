"""Per-repo clone cache (ADR 0010): unit tests pin the emitted `git` and the clone-vs-fetch
decision (fakes); one integration test clones a real local repo (skipped when git is absent)."""

from __future__ import annotations

import base64
import functools
import http.server
import os
import shutil
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.git_credentials import RepoGitTransport


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append(list(args))
        return ""


def test_path_is_repo_scoped_under_root() -> None:
    assert CloneCache("/clones/").path("r1") == "/clones/r1"  # trailing slash normalized


def test_clones_on_first_use() -> None:
    rec = _Recorder()
    cache = CloneCache("/clones", run=rec, exists=lambda _p: False, makedirs=lambda _p: None)
    path = cache.ensure("r1", "https://x/r1.git")
    assert path == "/clones/r1"
    assert rec.calls == [["git", "clone", "https://x/r1.git", "/clones/r1"]]


def test_fetches_when_the_clone_exists() -> None:
    rec = _Recorder()
    cache = CloneCache("/clones", run=rec, exists=lambda _p: True, makedirs=lambda _p: None)
    path = cache.ensure("r1", "https://x/r1.git")
    assert path == "/clones/r1"
    assert rec.calls == [
        ["git", "-C", "/clones/r1", "fetch", "--all", "--prune"],
        [
            "git",
            "-C",
            "/clones/r1",
            "merge",
            "--ff-only",
        ],  # advance local base to upstream (else stale)
    ]


def test_repo_credential_wraps_initial_clone_and_reused_cache_fetch_only() -> None:
    rec = _Recorder()
    exists = False
    cache = CloneCache("/clones", run=rec, exists=lambda _p: exists, makedirs=lambda _p: None)
    transport = RepoGitTransport(
        "git@github.com:acme/private.git",
        "https://github.com/acme/private.git",
        "repo-token-test",
    )

    cache.ensure("r1", transport.source_url, transport=transport)
    exists = True
    cache.ensure("r1", transport.source_url, transport=transport)

    clone, set_source_origin, fetch, merge = rec.calls
    assert clone[:4] == ["git", "-c", "credential.helper=", "-c"]
    assert clone[4].startswith("credential.helper=!")
    assert clone[5] == "-c"
    assert clone[6] == "credential.https://github.com.useHttpPath=true"
    assert clone[7:9] == ["-c", "http.extraHeader="]
    assert clone[-3:] == ["clone", "https://github.com/acme/private.git", "/clones/r1"]
    assert set_source_origin == [
        "git",
        "-C",
        "/clones/r1",
        "remote",
        "set-url",
        "origin",
        "git@github.com:acme/private.git",
    ]
    assert fetch[:4] == clone[:4]
    assert fetch[4].startswith("credential.helper=!")
    assert fetch[5:9] == clone[5:9]
    assert fetch[-5:] == ["-C", "/clones/r1", "fetch", "--all", "--prune"]
    assert (
        "url.https://github.com/acme/private.git.insteadOf=git@github.com:acme/private.git" in fetch
    )
    assert merge == ["git", "-C", "/clones/r1", "merge", "--ff-only"]
    assert all("repo-token-test" not in "\n".join(command) for command in rec.calls)
    assert all(
        not Path(setting.removeprefix("credential.helper=!")).exists()
        for command in (clone, fetch)
        for setting in command
        if setting.startswith("credential.helper=!")
    )


def test_ensure_creates_root_dir_before_cloning(tmp_path: Path) -> None:
    root = tmp_path / "clones"
    assert not root.exists()
    cache = CloneCache(str(root), run=lambda *_a, **_kw: "", exists=lambda _p: False)
    cache.ensure("r1", "https://x/r1.git")
    assert root.is_dir()


# -- integration: a real git repo ---------------------------------------------------


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_ensure_clones_then_fetches_a_real_repo(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *a: subprocess.run(a, cwd=origin, check=True, capture_output=True)
    run("git", "init", "--initial-branch", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (origin / "README").write_text("hi")
    run("git", "add", "--all")
    run("git", "commit", "--message", "init")

    cache = CloneCache(str(tmp_path / "clones"))
    path = cache.ensure("r1", str(origin))  # first use: clones
    assert (Path(path) / "README").read_text() == "hi"

    (origin / "README").write_text("updated")  # origin's base branch moves forward
    run("git", "commit", "--all", "--message", "second")
    assert cache.ensure("r1", str(origin)) == path  # second use: fetch + fast-forward, same path
    assert (
        Path(path) / "README"
    ).read_text() == "updated"  # the cache's base branch advanced (not stale)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_repo_token_authenticates_real_http_clone_and_fetch_without_url_or_trace_leakage(
    tmp_path: Path,
) -> None:
    token = "local-http-repository-token-test"
    expected_authorization = (
        "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    )
    observed_authorization: list[str | None] = []

    class AuthenticatedGitFiles(http.server.SimpleHTTPRequestHandler):
        def _authorized(self) -> bool:
            authorization = self.headers.get("Authorization")
            observed_authorization.append(authorization)
            if authorization == expected_authorization:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.end_headers()
            return False

        def do_GET(self) -> None:
            if self._authorized():
                super().do_GET()

        def do_HEAD(self) -> None:
            if self._authorized():
                super().do_HEAD()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

    git("init", "--initial-branch", "main", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    git("config", "user.name", "Test User", cwd=source)
    (source / "README").write_text("first")
    git("add", "README", cwd=source)
    git("commit", "--message", "first", cwd=source)

    web_root = tmp_path / "web"
    web_root.mkdir()
    bare = web_root / "private.git"
    git("clone", "--bare", str(source), str(bare))
    git("--git-dir", str(bare), "update-server-info")

    handler = functools.partial(AuthenticatedGitFiles, directory=str(web_root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    traces: list[str] = []
    wrong_askpass = tmp_path / "wrong-askpass"
    wrong_askpass_called = Path(f"{wrong_askpass}.called")
    wrong_askpass.write_text(
        "#!/bin/sh\nprintf called > \"$0.called\"\nprintf '%s\\n' wrong-ambient-credential\n"
    )
    wrong_askpass.chmod(0o700)
    process_environment = dict(os.environ)
    git_environment = {**process_environment, "GIT_ASKPASS": str(wrong_askpass)}

    def traced_run(args: Sequence[str], *, check: bool = True) -> str:
        result = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            env={**git_environment, "GIT_TRACE": "1", "GIT_TRACE_CURL": "1"},
        )
        traces.append(result.stderr)
        if check and result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, list(args), output=result.stdout, stderr=result.stderr
            )
        return result.stdout

    try:
        url = f"http://127.0.0.1:{server.server_port}/private.git"
        transport = RepoGitTransport("git@github.com:acme/private.git", url, token)
        cache = CloneCache(str(tmp_path / "cache"), run=traced_run)
        clone = Path(cache.ensure("repo", transport.source_url, transport=transport))
        assert (clone / "README").read_text() == "first"

        (source / "README").write_text("second")
        git("commit", "--all", "--message", "second", cwd=source)
        git("push", str(bare), "main", cwd=source)
        git("--git-dir", str(bare), "update-server-info")

        assert cache.ensure("repo", transport.source_url, transport=transport) == str(clone)
        assert (clone / "README").read_text() == "second"
        config = (clone / ".git" / "config").read_text()
        assert token not in config
        assert token not in url
        assert all(token not in trace for trace in traces)
        assert all(expected_authorization not in trace for trace in traces)
        assert expected_authorization in observed_authorization
        assert not wrong_askpass_called.exists()
        assert dict(os.environ) == process_environment
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
