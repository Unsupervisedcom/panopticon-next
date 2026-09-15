"""Spawn-prep (ADR 0011): clone the per-task checkout before the container starts. Unit tests pin
the emitted `git` and the idempotency gate (fakes). No Docker, no LLM."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from panopticon.core.git import GitClones
from panopticon.sessionservice.clones import CloneCache
from panopticon.sessionservice.spawn import cleanup_workspace, prepare_workspace


def _recording_runner(
    origin: str = "https://forge/r1.git",
) -> tuple[list[list[str]], Callable[..., str]]:
    calls: list[list[str]] = []

    def run(args: object, *, check: bool = True) -> str:
        calls.append(list(args))  # type: ignore[arg-type]
        return origin if list(args)[-1] == "remote.origin.url" else ""

    return calls, run


_REPO = {"id": "r1", "git_url": "https://forge/r1.git"}


# 2119: REQ-004.1.1
# 2119: REQ-004.2.1
# 2119: REQ-004.3.1
def test_prepare_clones_the_cache_then_the_per_task_checkout() -> None:
    calls, run = _recording_runner()
    cache = CloneCache(
        "/cache", run=run, exists=lambda _p: False, makedirs=lambda _p: None
    )  # cache absent → clone

    clone = prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: False,
        makedirs=lambda _p: None,
    )

    assert clone == "/tasks/t1"
    assert calls == [
        [
            "git",
            "clone",
            "https://forge/r1.git",
            cache.path("r1", _REPO["git_url"]),
        ],  # ensure the repo's cache clone…
        [
            "git",
            "clone",
            cache.path("r1", _REPO["git_url"]),
            "/tasks/t1",
        ],  # …then the self-contained per-task clone
        # …then point origin at the forge (the git_url, verbatim) — not the cache path, which the
        # container can't push to and gh can't resolve (it would fork to the token's own account)
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"],
        ["git", "-C", "/tasks/t1", "config", "--local", "user.name", "Panopticon Agent"],
        [
            "git",
            "-C",
            "/tasks/t1",
            "config",
            "--local",
            "user.email",
            "panopticon-agent@users.noreply.github.com",
        ],
    ]


# 2119: REQ-004.1.1
# 2119: REQ-004.2.1
# 2119: REQ-004.3.1
def test_prepare_is_idempotent_but_still_asserts_origin_when_the_checkout_exists() -> None:
    calls, run = _recording_runner()
    cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

    clone = prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: True,
        makedirs=lambda _p: None,
    )

    assert clone == "/tasks/t1"
    # Existing checkout: verify source before reasserting the same origin, with no clone/fetch.
    assert calls == [
        ["git", "-C", "/tasks/t1", "config", "--local", "--get", "remote.origin.url"],
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "https://forge/r1.git"],
        ["git", "-C", "/tasks/t1", "config", "--local", "user.name", "Panopticon Agent"],
        [
            "git",
            "-C",
            "/tasks/t1",
            "config",
            "--local",
            "user.email",
            "panopticon-agent@users.noreply.github.com",
        ],
    ]


def test_prepare_uses_the_git_url_verbatim_as_origin() -> None:
    # The git_url is registered in the form the container should use (here SSH); spawn sets it as-is,
    # no rewriting — the URL scheme is the operator's choice at repo setup, not a conversion here.
    calls, run = _recording_runner("git@github.com:Org/repo.git")
    repo = {"id": "r1", "git_url": "git@github.com:Org/repo.git"}
    cache = CloneCache("/cache", run=run, exists=lambda _p: True, makedirs=lambda _p: None)

    prepare_workspace(
        "t1",
        repo,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: True,
        makedirs=lambda _p: None,
    )

    assert calls == [
        ["git", "-C", "/tasks/t1", "config", "--local", "--get", "remote.origin.url"],
        ["git", "-C", "/tasks/t1", "remote", "set-url", "origin", "git@github.com:Org/repo.git"],
        ["git", "-C", "/tasks/t1", "config", "--local", "user.name", "Panopticon Agent"],
        [
            "git",
            "-C",
            "/tasks/t1",
            "config",
            "--local",
            "user.email",
            "panopticon-agent@users.noreply.github.com",
        ],
    ]


def test_prepare_converts_github_ssh_to_credential_free_https_for_repo_token(
    tmp_path: Path,
) -> None:
    calls, run = _recording_runner()
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    env_file = secrets / "repo.env"
    env_file.write_text("GH_TOKEN=repo-token-test\n")
    env_file.chmod(0o600)
    repo = {
        "id": "r1",
        "git_url": "git@github.com:Acme/private.git",
        "env_file": "repo.env",
    }
    cache = CloneCache("/cache", run=run, exists=lambda _p: False, makedirs=lambda _p: None)

    prepare_workspace(
        "t1",
        repo,
        cache=cache,
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: False,
        makedirs=lambda _p: None,
        secrets_dir=secrets,
        git_credential_run=run,
    )

    network_clone = calls[0]
    assert network_clone[-3:] == [
        "clone",
        "https://github.com/Acme/private.git",
        cache.path("r1", repo["git_url"]),
    ]
    assert calls[3:7] == [
        [
            "git",
            "-C",
            "/tasks/t1",
            "remote",
            "set-url",
            "origin",
            "https://github.com/Acme/private.git",
        ],
        ["git", "-C", "/tasks/t1", "config", "--local", "credential.helper", ""],
        [
            "git",
            "-C",
            "/tasks/t1",
            "config",
            "--local",
            "credential.https://github.com.helper",
            "!gh auth git-credential",
        ],
        [
            "git",
            "-C",
            "/tasks/t1",
            "config",
            "--local",
            "credential.https://github.com.useHttpPath",
            "true",
        ],
    ]
    rendered_calls = "\n".join(part for command in calls for part in command)
    assert "repo-token-test" not in rendered_calls
    assert repo["git_url"] == "git@github.com:Acme/private.git"


def test_prepare_creates_tasks_root_before_cloning(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    assert not tasks_root.exists()
    created: list[str] = []

    cache = CloneCache(str(tmp_path / "cache"), run=lambda *_a, **_kw: "", exists=lambda _p: False)
    prepare_workspace(
        "t1",
        _REPO,
        cache=cache,
        tasks_root=str(tasks_root),
        git=GitClones(run=lambda *_a, **_kw: ""),
        exists=lambda _p: False,
        makedirs=lambda p: (created.append(p), Path(p).mkdir(parents=True, exist_ok=True)),  # type: ignore[func-returns-value]
    )

    assert str(tasks_root) in created
    assert tasks_root.is_dir()


def test_cleanup_removes_the_checkout_when_it_exists() -> None:
    removed: list[str] = []
    cleanup_workspace("t1", "/tasks", exists=lambda _p: True, rmtree=removed.append)
    assert removed == ["/tasks/t1"]


def test_cleanup_is_a_no_op_when_checkout_is_absent() -> None:
    removed: list[str] = []
    cleanup_workspace("t1", "/tasks", exists=lambda _p: False, rmtree=removed.append)
    assert removed == []


def _raise_permission_denied(_path: str) -> None:
    raise PermissionError(13, "Permission denied", "/tasks/t1/.mypy_cache")


def test_cleanup_quarantines_a_checkout_it_cannot_delete() -> None:
    # A container process that ran as root leaves files the daemon can't delete (e.g. a
    # root-owned .mypy_cache) — rmtree raises. The checkout is renamed aside instead of the
    # error propagating, so the host pass doesn't refail on it every tick.
    renamed: list[tuple[str, str]] = []
    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=_raise_permission_denied,
        rename=lambda src, dst: renamed.append((src, dst)),
    )
    assert renamed == [("/tasks/t1", "/tasks/t1.stale")]


def test_cleanup_swallows_a_failed_quarantine() -> None:
    # Even the rename failing (e.g. the quarantine path already exists) must not raise —
    # cleanup is best-effort; it never takes down the host pass.
    def rename_fails(_src: str, _dst: str) -> None:
        raise OSError("target exists")

    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=_raise_permission_denied,
        rename=rename_fails,
    )  # no exception is the assertion


def test_cleanup_uses_docker_to_scrub_root_owned_files() -> None:
    # When rmtree fails (root-owned files), docker_cleanup empties the directory so the
    # second rmtree call can remove the empty dir — no quarantine needed.
    docker_called: list[str] = []
    rmtree_calls = 0

    def rmtree_first_fails_then_succeeds(path: str) -> None:
        nonlocal rmtree_calls
        rmtree_calls += 1
        if rmtree_calls == 1:
            raise PermissionError(13, "Permission denied", "/tasks/t1/.mypy_cache")

    renamed: list[tuple[str, str]] = []
    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=rmtree_first_fails_then_succeeds,
        docker_cleanup=docker_called.append,
        rename=lambda src, dst: renamed.append((src, dst)),
    )
    assert docker_called == ["/tasks/t1"]
    assert rmtree_calls == 2  # first fails, second (on the now-empty dir) succeeds
    assert renamed == []  # quarantine not reached


def test_cleanup_quarantines_when_docker_cleanup_also_fails() -> None:
    # When both rmtree and docker_cleanup fail, fall back to quarantine.
    def docker_cleanup_fails(_path: str) -> None:
        raise OSError("docker not available")

    renamed: list[tuple[str, str]] = []
    cleanup_workspace(
        "t1",
        "/tasks",
        exists=lambda _p: True,
        rmtree=_raise_permission_denied,
        docker_cleanup=docker_cleanup_fails,
        rename=lambda src, dst: renamed.append((src, dst)),
    )
    assert renamed == [("/tasks/t1", "/tasks/t1.stale")]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_source_edit_uses_new_cache_and_preserves_existing_work(tmp_path: Path) -> None:
    def command(*args: str) -> str:
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

    sources = [tmp_path / "source-a", tmp_path / "source-b"]
    for source, marker in zip(sources, ["A", "B"], strict=True):
        command("git", "init", "--initial-branch", "main", str(source))
        command("git", "-C", str(source), "config", "user.name", "Source Test")
        command("git", "-C", str(source), "config", "user.email", "source@example.test")
        (source / "marker").write_text(marker)
        command("git", "-C", str(source), "add", "marker")
        command("git", "-C", str(source), "commit", "--message", "Initial marker")
    cache_root = tmp_path / "cache"
    legacy = cache_root / "repo"
    legacy.mkdir(parents=True)
    (legacy / "retained-work").write_text("legacy cache content")
    cache = CloneCache(str(cache_root))
    tasks_root = str(tmp_path / "tasks")
    repo = {"id": "repo", "git_url": str(sources[0])}
    first = Path(prepare_workspace("first", repo, cache=cache, tasks_root=tasks_root))
    (first / "marker").write_text("uncommitted task work")
    old_cache = Path(cache.path("repo", repo["git_url"]))
    (old_cache / "retained-work").write_text("old cache content")
    repo["git_url"] = str(sources[1])
    second = Path(prepare_workspace("second", repo, cache=cache, tasks_root=tasks_root))
    assert (second / "marker").read_text() == "B"
    assert command("git", "-C", str(second), "rev-parse", "HEAD") == command(
        "git", "-C", str(sources[1]), "rev-parse", "HEAD"
    )
    assert GitClones().origin(repo_path=str(second)) == str(sources[1])
    with pytest.raises(RuntimeError, match="create a new task for the edited repository"):
        prepare_workspace("first", repo, cache=cache, tasks_root=tasks_root)
    assert GitClones().origin(repo_path=str(first)) == str(sources[0])
    assert (first / "marker").read_text() == "uncommitted task work"
    assert (old_cache / "retained-work").read_text() == "old cache content"
    assert (legacy / "retained-work").read_text() == "legacy cache content"
    assert [(source / "marker").read_text() for source in sources] == ["A", "B"]
    repo["git_url"] = str(sources[0])
    assert prepare_workspace("first", repo, cache=cache, tasks_root=tasks_root) == str(first)
    assert (first / "marker").read_text() == "uncommitted task work"


@pytest.mark.parametrize("origin", ["", "https://user:synthetic-secret@example.invalid/other.git"])
def test_existing_checkout_source_mismatch_is_safe_and_actionable(origin: str) -> None:
    calls, run = _recording_runner(origin)
    with pytest.raises(RuntimeError) as raised:
        prepare_workspace(
            "t1",
            _REPO,
            cache=CloneCache("/cache"),
            tasks_root="/tasks",
            git=GitClones(run=run),
            exists=lambda _p: True,
        )
    assert "Existing work was preserved" in str(raised.value)
    assert "g, then e" in str(raised.value)
    assert "synthetic-secret" not in str(raised.value)
    assert calls == [["git", "-C", "/tasks/t1", "config", "--local", "--get", "remote.origin.url"]]


def test_existing_github_checkout_allows_equivalent_transport_repair() -> None:
    calls, run = _recording_runner("git@github.com:acme/repo.git")
    prepare_workspace(
        "t1",
        {"id": "r1", "git_url": "https://github.com/acme/repo.git"},
        cache=CloneCache("/cache"),
        tasks_root="/tasks",
        git=GitClones(run=run),
        exists=lambda _p: True,
    )
    assert calls[1] == [
        "git",
        "-C",
        "/tasks/t1",
        "remote",
        "set-url",
        "origin",
        "https://github.com/acme/repo.git",
    ]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
@pytest.mark.parametrize(
    "failure", ["none", "source_changed", "missing_objects", "missing_blob", "missing_index"]
)
def test_prepare_retries_interrupted_origin_setup_only_for_the_same_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def command(*args: str) -> str:
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

    source = tmp_path / "source"
    command("git", "init", "--initial-branch", "main", "--template=", str(source))
    command("git", "-C", str(source), "config", "user.name", "Retry Test")
    command("git", "-C", str(source), "config", "user.email", "retry@example.test")
    (source / "marker").write_text("original source")
    command("git", "-C", str(source), "add", "marker")
    command("git", "-C", str(source), "commit", "--message", "Initial marker")
    cache = CloneCache(str(tmp_path / "cache"))
    repo = {"id": "repo", "git_url": str(source)}

    class InterruptedGit(GitClones):
        def set_origin(self, *, repo_path: str, url: str) -> None:
            raise RuntimeError("simulated interruption after cloning")

    tasks_root = str(tmp_path / "tasks")
    with pytest.raises(RuntimeError, match="simulated interruption"):
        prepare_workspace("task", repo, cache=cache, tasks_root=tasks_root, git=InterruptedGit())
    checkout = Path(tasks_root) / "task"
    cached = cache.path(repo["id"], repo["git_url"])
    assert GitClones().origin(repo_path=str(checkout)) == cached
    (checkout / "untracked-work").write_text("keep this")
    if failure == "source_changed":
        # Even the expected cache path is insufficient when its stored source changed.
        GitClones().set_origin(repo_path=cached, url=str(tmp_path / "other-source"))
        with pytest.raises(RuntimeError, match="does not match the repository source"):
            prepare_workspace("task", repo, cache=cache, tasks_root=tasks_root)
        assert GitClones().origin(repo_path=str(checkout)) == cached
    elif failure in {"missing_objects", "missing_blob", "missing_index"}:
        if failure == "missing_objects":
            shutil.rmtree(checkout / ".git" / "objects")
            (checkout / ".git" / "objects").mkdir()
        elif failure == "missing_blob":
            blob = command("git", "-C", str(checkout), "rev-parse", "HEAD:marker")
            (checkout / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
            # HEAD alone still resolves; connectivity must check its referenced contents too.
            command("git", "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}")
        else:
            (checkout / ".git" / "index").unlink()
            # Packed objects can be complete before checkout writes the initial index.
            command("git", "-C", str(checkout), "fsck", "--connectivity-only")
        with pytest.raises(RuntimeError, match="incomplete Git objects or an unfinished"):
            prepare_workspace("task", repo, cache=cache, tasks_root=tasks_root)
        assert GitClones().origin(repo_path=str(checkout)) == cached
        command("git", "-C", cached, "fsck", "--connectivity-only")
    else:
        assert prepare_workspace("task", repo, cache=cache, tasks_root=tasks_root) == str(checkout)
        assert GitClones().origin(repo_path=str(checkout)) == str(source)
        assert command("git", "-C", str(checkout), "config", "user.name") == "Panopticon Agent"
    assert (checkout / "marker").read_text() == "original source"
    assert (checkout / "untracked-work").read_text() == "keep this"
