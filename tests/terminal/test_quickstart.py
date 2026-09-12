"""Unit tests for panopticon.terminal.quickstart."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from panopticon.terminal import quickstart as qs


def test_detect_git_url_from_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 2119: REQ-054.2.1
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://github.com/example/repo.git",
        ],
        check=True,
    )
    monkeypatch.chdir(tmp_path)
    assert qs.detect_git_url() == "https://github.com/example/repo.git"


def test_detect_git_url_rejects_missing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="could not identify a Git repository"):
        qs.detect_git_url()


def test_detect_git_url_rejects_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **_: Any) -> Any:
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="could not identify a Git repository"):
        qs.detect_git_url()


def test_detect_git_url_uses_canonical_path_for_repo_without_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panopticon.terminal.source_selection import detect_current_source

    # 2119: REQ-054.2.1
    # 2119: REQ-054.2.3
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subdirectory = checkout / "nested"
    subdirectory.mkdir()
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(checkout, target_is_directory=True)

    monkeypatch.chdir(subdirectory)
    assert qs.detect_git_url() == str(checkout.resolve())
    detected_through_alias = detect_current_source(cwd=alias / "nested")
    assert detected_through_alias is not None
    assert detected_through_alias.git_url == str(checkout.resolve())


@pytest.mark.parametrize(
    "git_url",
    [
        "https://github.com/acme/widget.git",
        "git@github.com:acme/widget.git",
        "ssh://git@github.com/acme/widget.git",
    ],
)
def test_choose_enabled_workflows_forge(git_url: str) -> None:
    # 2119: REQ-054.4.1
    assert qs.choose_enabled_workflows(git_url) == (
        "github-self-reviewed",
        "github-peer-reviewed",
    )


@pytest.mark.parametrize(
    "git_url",
    [
        "/srv/repos/widget",
        "./widget",
        "file:///srv/repos/widget",
        "C:\\repos\\widget",
        "http://example.com/acme/widget",
        "git://github.com/acme/widget.git",
    ],
)
def test_choose_enabled_workflows_local(git_url: str) -> None:
    assert qs.choose_enabled_workflows(git_url) == ("local-git-self-reviewed",)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/Unsupervisedcom/panopticon.git", "panopticon-"),
        ("https://github.com/example/repo", "repo-"),
        ("git@github.com:acme/Widget.git", "widget-"),
        ("https://github.com/acme/thing.git/", "thing-"),
    ],
)
def test_repo_id_from_url(url: str, expected: str) -> None:
    repo_id = qs.repo_id_from_url(url)
    assert repo_id.startswith(expected)
    assert len(repo_id.rsplit("-", 1)[-1]) == 8


def test_setup_repo_dedups_on_remote_url(capsys: pytest.CaptureFixture[str]) -> None:
    # 2119: REQ-054.4.1
    # A registered repo whose git_url matches (modulo a trailing ``.git``) → no re-registration;
    # its (id, name) is returned.
    class _HasRepo:
        create_repo_called = False

        def __init__(self) -> None:
            self.updated: dict[str, Any] = {}

        def list_repos(self) -> list[dict[str, object]]:
            return [{"id": "other", "name": "acme/other", "git_url": "https://github.com/x/y"}]

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            self.create_repo_called = True
            return {}

        def update_repo(self, repo_id: str, **changes: Any) -> dict[str, object]:
            self.updated = {"repo_id": repo_id, **changes}
            return {}

    fake_client = _HasRepo()
    repo_id, name = qs.setup_repo(fake_client, "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
    assert (repo_id, name) == ("other", "acme/other")
    assert not fake_client.create_repo_called
    # The reused repo had no enabled workflows, so the forge lifecycle is merged in.
    assert fake_client.updated == {
        "repo_id": "other",
        "enabled_workflows": ["github-self-reviewed", "github-peer-reviewed"],
    }
    assert "already configured" in capsys.readouterr().out


def test_setup_repo_creates_when_absent() -> None:
    # 2119: REQ-054.4.1
    created: dict[str, Any] = {}

    class _Empty:
        def list_repos(self) -> list[dict[str, object]]:
            return [{"id": "unrelated", "git_url": "https://github.com/a/b.git"}]

        def create_repo(
            self, repo_id: str, name: str, git_url: str, **kw: Any
        ) -> dict[str, object]:
            created.update(repo_id=repo_id, name=name, git_url=git_url, **kw)
            return {}

    repo_id, name = qs.setup_repo(_Empty(), "https://github.com/x/y.git", "panopticon.env")  # type: ignore[arg-type]
    assert (repo_id, name) == (qs.repo_id_from_url("https://github.com/x/y.git"), "y")
    assert created["repo_id"] == repo_id
    assert created["name"] == "y"
    assert created["git_url"] == "https://github.com/x/y.git"
    assert created["env_file"] == "panopticon.env"
    # A hosted-forge remote enables the forge lifecycle so the repo can create a coding task.
    assert created["enabled_workflows"] == [
        "github-self-reviewed",
        "github-peer-reviewed",
    ]


def test_setup_repo_records_the_chosen_default_harness_without_a_model() -> None:
    created: dict[str, Any] = {}

    class _Empty:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(
            self, repo_id: str, name: str, git_url: str, **kw: Any
        ) -> dict[str, object]:
            created.update(kw)
            return {}

    qs.setup_repo(  # type: ignore[arg-type]
        _Empty(),
        "https://github.com/x/y.git",
        "panopticon.env",
        default_harness="codex",
    )

    assert created["default_harness"] == "codex"
    assert "default_model" not in created


def test_setup_repo_enables_local_workflow_for_local_remote() -> None:
    created: dict[str, Any] = {}

    class _Empty:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(
            self, repo_id: str, name: str, git_url: str, **kw: Any
        ) -> dict[str, object]:
            created.update(repo_id=repo_id, name=name, git_url=git_url, **kw)
            return {}

    repo_id, _ = qs.setup_repo(_Empty(), "/srv/repos/widget", "panopticon.env")  # type: ignore[arg-type]
    assert repo_id == qs.repo_id_from_url("/srv/repos/widget")
    # A local-only remote (a filesystem path) enables the forge-free lifecycle instead.
    assert created["enabled_workflows"] == ["local-git-self-reviewed"]


def test_setup_repo_dedups_on_derived_id_and_preserves_existing_workflows() -> None:
    # 2119: REQ-054.4.1
    # GitHub SSH and HTTPS spellings resolve to the same bounded source identity. The legacy id is
    # retained and its custom workflows survive the merge.
    class _HasRepo:
        create_repo_called = False

        def list_repos(self) -> list[dict[str, object]]:
            return [
                {
                    "id": "y",
                    "name": "x/y",
                    "git_url": "git@github.com:x/y.git",
                    "enabled_workflows": ["custom", "github-peer-reviewed"],
                }
            ]

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            self.create_repo_called = True
            return {}

        def update_repo(self, repo_id: str, **changes: Any) -> dict[str, object]:
            assert repo_id == "y"
            assert changes == {
                "enabled_workflows": [
                    "custom",
                    "github-peer-reviewed",
                    "github-self-reviewed",
                ]
            }
            return {}

    fake_client = _HasRepo()
    repo_id, name = qs.setup_repo(fake_client, "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
    assert (repo_id, name) == ("y", "x/y")
    assert not fake_client.create_repo_called


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/repos")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_setup_repo_recovers_from_create_conflict() -> None:
    # 2119: source-selection.4.5
    # Repeated conflicts without an equivalent source fail explicitly instead of reusing an
    # unrelated repository.
    class _Conflict:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            raise _http_status_error(409)

    with pytest.raises(RuntimeError, match="could not allocate a repository id"):
        qs.setup_repo(_Conflict(), "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]


def test_setup_repo_reraises_non_conflict_create_error() -> None:
    class _Boom:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(self, *a: Any, **kw: Any) -> dict[str, object]:
            raise _http_status_error(500)

    with pytest.raises(httpx.HTTPStatusError):
        qs.setup_repo(_Boom(), "https://github.com/x/y.git", "/tmp/env")  # type: ignore[arg-type]
