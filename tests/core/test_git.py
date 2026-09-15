"""Local git worktree ops: unit tests pin the emitted git commands + slug-gating; one
integration test exercises a real repo (skipped when git is unavailable)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from panopticon.core.git import GitClones, GitWorktrees, Worktree, branch_name, worktree_path


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, args: Sequence[str], *, check: bool = True) -> str:
        self.calls.append((list(args), check))
        return ""


def test_naming_is_slug_derived() -> None:
    assert branch_name("fix-the-widget") == "panopticon/fix-the-widget"
    assert (
        worktree_path("/wt/", "r1", "panopticon/fix-the-widget")
        == "/wt/r1/panopticon/fix-the-widget"
    )


def test_create_emits_worktree_add_and_returns_branch_and_path() -> None:
    rec = _Recorder()
    wt = GitWorktrees(run=rec).create(
        repo_path="/repos/r1", worktrees_root="/wt", repo_id="r1", slug="fix-it", base="main"
    )
    assert wt == Worktree(branch="panopticon/fix-it", path="/wt/r1/panopticon/fix-it")
    ((cmd, _),) = rec.calls
    assert cmd == [
        "git",
        "-C",
        "/repos/r1",
        "worktree",
        "add",
        "-b",
        "panopticon/fix-it",
        "/wt/r1/panopticon/fix-it",
        "main",
    ]


def test_create_is_slug_gated() -> None:
    rec = _Recorder()
    with pytest.raises(ValueError, match="slug"):
        GitWorktrees(run=rec).create(
            repo_path="/r", worktrees_root="/wt", repo_id="r1", slug=None, base="main"
        )
    assert rec.calls == []  # nothing run before the slug exists


def test_remove_force_tier_and_idempotent() -> None:
    rec = _Recorder()
    git = GitWorktrees(run=rec)
    git.remove(repo_path="/r", worktree_path="/wt/r1/panopticon/fix-it")
    git.remove(repo_path="/r", worktree_path="/wt/r1/panopticon/fix-it", force=True)
    assert rec.calls[0] == (
        ["git", "-C", "/r", "worktree", "remove", "/wt/r1/panopticon/fix-it"],
        False,
    )
    assert rec.calls[1][0][-1] == "--force"
    assert rec.calls[1][1] is False  # idempotent: never raises on an already-gone worktree


# -- per-task local clones (ADR 0011) -----------------------------------------------


def test_clone_local_emits_self_contained_clone() -> None:
    rec = _Recorder()
    GitClones(run=rec).clone_local(cache_path="/clones/r1", dest="/tasks/t1")
    assert rec.calls[0][0] == [
        "git",
        "clone",
        "/clones/r1",
        "/tasks/t1",
    ]


def test_create_branch_and_set_origin() -> None:
    rec = _Recorder()
    git = GitClones(run=rec)
    git.create_branch(repo_path="/tasks/t1", branch="panopticon/fix-it")
    git.set_origin(repo_path="/tasks/t1", url="https://forge/r1.git")
    assert rec.calls[0][0] == ["git", "-C", "/tasks/t1", "checkout", "-b", "panopticon/fix-it"]
    assert rec.calls[1][0] == [
        "git",
        "-C",
        "/tasks/t1",
        "remote",
        "set-url",
        "origin",
        "https://forge/r1.git",
    ]


# -- integration: a real git repo ---------------------------------------------------


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
@pytest.mark.parametrize("packed", [False, True], ids=["loose", "packed"])
@pytest.mark.parametrize(
    "cross_filesystem", [False, True], ids=["same-filesystem", "cross-filesystem"]
)
def test_local_clone_is_self_contained_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packed: bool, cross_filesystem: bool
) -> None:
    destination_root = Path("/dev/shm") if cross_filesystem else tmp_path
    if cross_filesystem and (
        not destination_root.is_dir() or destination_root.stat().st_dev == tmp_path.stat().st_dev
    ):
        pytest.skip("needs a separate temporary filesystem at /dev/shm")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    source = tmp_path / "cache"
    source.mkdir()

    def git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git(source, "init", "--initial-branch", "main", "--template=")
    git(source, "config", "user.name", "Clone Test")
    git(source, "config", "user.email", "clone@example.test")
    (source / "README").write_text("committed content\n")
    git(source, "add", "README")
    git(source, "commit", "--message", "Initial content")
    if packed:
        git(source, "repack", "-a", "-d")
    (source / "README").write_text("source-only uncommitted work\n")
    original = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    head = git(source, "rev-parse", "HEAD")
    with TemporaryDirectory(prefix="panopticon-clone-", dir=destination_root) as directory:
        destination = Path(directory) / "task"
        GitClones().clone_local(cache_path=str(source), dest=str(destination))
        assert git(destination, "rev-parse", "HEAD") == head
        assert (destination / "README").read_text() == "committed content\n"
        assert not (destination / ".git/objects/info/alternates").exists()
        objects = [path for path in (source / ".git/objects").rglob("*") if path.is_file()]
        assert objects
        for path in objects:
            copy = destination / path.relative_to(source)
            assert copy.read_bytes() == path.read_bytes()
            if cross_filesystem:
                assert not copy.samefile(path)
        assert original == {
            path.relative_to(source): path.read_bytes()
            for path in source.rglob("*")
            if path.is_file()
        }
        source.rename(tmp_path / "unavailable-cache")
        git(destination, "fsck", "--full")
        assert git(destination, "show", "HEAD:README") == "committed content"


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_create_and_remove_a_real_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "--initial-branch", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README").write_text("hi")
    run("git", "add", "--all")
    run("git", "commit", "--message", "init")

    git = GitWorktrees()
    wt = git.create(
        repo_path=str(repo),
        worktrees_root=str(tmp_path / "wt"),
        repo_id="r1",
        slug="fix-it",
        base="main",
    )
    assert Path(wt.path).is_dir()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "panopticon/fix-it"],
        capture_output=True,
        text=True,
    ).stdout
    assert "panopticon/fix-it" in branches

    git.remove(repo_path=str(repo), worktree_path=wt.path, force=True)
    assert not Path(wt.path).exists()
