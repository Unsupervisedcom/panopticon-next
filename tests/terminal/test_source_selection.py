from __future__ import annotations

import ast
import socket
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from panopticon.terminal import quickstart as qs

# 2119-spec: source-selection


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _worktree(path: Path, *, origin: str | None = None) -> Path:
    _git("init", str(path))
    if origin is not None:
        _git("-C", str(path), "remote", "add", "origin", origin)
    return path


def _bundle(path: Path) -> Path:
    repo = path.parent / "bundle-source"
    _worktree(repo)
    _git("-C", str(repo), "config", "user.name", "Test User")
    _git("-C", str(repo), "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("snapshot\n")
    _git("-C", str(repo), "add", "README.md")
    _git("-C", str(repo), "commit", "--message", "fixture")
    _git("-C", str(repo), "bundle", "create", str(path), "HEAD")
    return path


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/repos")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_current_checkout_is_suggested_and_can_be_replaced(tmp_path: Path) -> None:
    # 2119: 1.1
    # 2119: 1.2
    # 2119: REQ-054.2.2
    checkout = _worktree(tmp_path / "current", origin="https://github.com/acme/current.git")
    other = _worktree(tmp_path / "other")
    prompts: list[str] = []

    selected = qs.select_source(
        cwd=checkout,
        input_fn=lambda prompt: (prompts.append(prompt), str(other))[1],
    )

    assert "https://github.com/acme/current.git" in prompts[0]
    assert selected.git_url == str(other.resolve())
    assert selected.name == "other"
    assert selected.kind == "checkout"


def test_current_checkout_suggestion_displays_remote_name_and_exact_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 2119: 1.1
    remote = "https://github.com/acme/friendly-widget.git"
    checkout = _worktree(tmp_path / "working-copy", origin=remote)

    selected = qs.select_source(cwd=checkout, input_fn=lambda _prompt: "")

    assert selected.git_url == remote
    assert selected.name == "friendly-widget"
    assert capsys.readouterr().out.splitlines()[:5] == [
        "Suggested repository: friendly-widget",
        f"  Source: {remote}",
        "Selected repository: friendly-widget",
        f"  Source: {remote}",
        "  Type: GitHub remote",
    ]


def test_checkout_without_origin_uses_canonical_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 2119: 1.3
    # 2119: 2.6
    checkout = _worktree(tmp_path / "project")

    selected = qs.select_source(cwd=checkout, input_fn=lambda _prompt: "")

    assert selected.git_url == str(checkout.resolve())
    assert selected.name == "project"
    output = capsys.readouterr().out
    assert "project" in output
    assert str(checkout.resolve()) in output


def test_relative_checkout_through_symlink_is_canonicalized(tmp_path: Path) -> None:
    # 2119: 1.3, 3.4, 3.5
    checkout = _worktree(tmp_path / "actual" / "project")
    alias = tmp_path / "project-link"
    alias.symlink_to(checkout, target_is_directory=True)
    bundle = _bundle(tmp_path / "snapshot.bundle")
    bundle_alias = tmp_path / "snapshot-link.bundle"
    bundle_alias.symlink_to(bundle)

    selected = qs.resolve_source("project-link", cwd=tmp_path)
    selected_bundle = qs.resolve_source("snapshot-link.bundle", cwd=tmp_path)

    assert selected.git_url == str(checkout.resolve())
    assert selected.name == "project"
    assert selected_bundle.git_url == str(bundle.resolve())


def test_non_repo_cwd_requests_an_explicit_source(tmp_path: Path) -> None:
    # 2119: 1.4
    checkout = _worktree(tmp_path / "chosen")
    prompts: list[str] = []

    selected = qs.select_source(
        cwd=tmp_path,
        input_fn=lambda prompt: (prompts.append(prompt), str(checkout))[1],
    )

    assert "Repository source" in prompts[0]
    assert "[" not in prompts[0]
    assert selected.git_url == str(checkout.resolve())


def test_selected_source_is_passed_unchanged_to_registration(tmp_path: Path) -> None:
    # 2119: 1.5
    selected = qs.resolve_source(str(_worktree(tmp_path / "selected")))
    created: dict[str, Any] = {}

    class _Client:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(self, repo_id: str, name: str, git_url: str, **fields: Any) -> None:
            created.update(repo_id=repo_id, name=name, git_url=git_url, **fields)

    qs.setup_repo(_Client(), selected.git_url, None, default_harness="codex")  # type: ignore[arg-type]

    assert created["git_url"] == selected.git_url
    assert created["name"] == selected.name
    assert created["env_file"] is None
    assert created["default_harness"] == "codex"


def test_local_checkout_validation_accepts_worktree_and_bare_repo(tmp_path: Path) -> None:
    # 2119: 2.1
    checkout = _worktree(tmp_path / "worktree")
    bare = tmp_path / "bare.git"
    _git("init", "--bare", str(bare))

    assert qs.resolve_source(str(checkout)).kind == "checkout"
    assert qs.resolve_source(str(bare)).git_url == str(bare.resolve())


def test_existing_non_git_directory_is_rejected(tmp_path: Path) -> None:
    # 2119: 2.1
    directory = tmp_path / "ordinary-directory"
    directory.mkdir()

    with pytest.raises(RuntimeError, match="invalid local Git checkout"):
        qs.resolve_source(str(directory))


def test_bundle_validation_and_snapshot_presentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 2119: 2.2
    # 2119: 2.4
    # 2119: 2.5
    bundle = _bundle(tmp_path / "friendly.bundle")
    outside_git = tmp_path / "outside-git"
    outside_git.mkdir()
    monkeypatch.chdir(outside_git)

    selected = qs.select_source(cwd=tmp_path, input_fn=lambda _prompt: str(bundle))

    assert selected.kind == "bundle"
    assert selected.name == "friendly"
    assert selected.git_url == str(bundle.resolve())
    assert "fixed snapshot" in capsys.readouterr().out


def test_bundle_with_missing_prerequisite_is_not_a_standalone_snapshot(tmp_path: Path) -> None:
    repo = _worktree(tmp_path / "source")
    _git("-C", str(repo), "config", "user.name", "Test User")
    _git("-C", str(repo), "config", "user.email", "test@example.invalid")
    for number in (1, 2):
        (repo / "state").write_text(str(number))
        _git("-C", str(repo), "add", "state")
        _git("-C", str(repo), "commit", "--message", f"state {number}")
    bundle = tmp_path / "prerequisite.bundle"
    _git("-C", str(repo), "bundle", "create", str(bundle), "HEAD~1..HEAD")

    with pytest.raises(RuntimeError, match="invalid Git bundle"):
        qs.resolve_source(str(bundle))


@pytest.mark.parametrize(
    "remote",
    [
        "https://token@github.com/acme/widget.git",
        "https://token:secret@github.com/acme/widget.git",
        "https://github.com/acme/widget.git?token=secret",
        "ssh://git:secret@github.com/acme/widget.git",
        "ssh://git@github.com/acme/widget.git#secret",
        "git@github.com:acme/widget.git?token=secret",
    ],
)
def test_credential_bearing_remote_is_rejected_without_echo(
    tmp_path: Path,
    remote: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 2119: 2.8
    checkout = _worktree(tmp_path / "safe")
    answers = iter((remote, str(checkout)))

    selected = qs.select_source(cwd=tmp_path, input_fn=lambda _prompt: next(answers))

    assert selected.git_url == str(checkout.resolve())
    output = capsys.readouterr().out
    assert "embedded credentials are not supported" in output
    assert "token" not in output
    assert "secret" not in output


def test_credential_bearing_cwd_origin_suggests_safe_checkout_path_without_echo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 2119: 1.1, 2.2, 2.8
    checkout = _worktree(
        tmp_path / "private", origin="https://token:secret@github.com/acme/private.git"
    )

    selected = qs.select_source(cwd=checkout, input_fn=lambda _prompt: "")

    assert selected.git_url == str(checkout.resolve())
    assert selected.kind == "checkout"
    output = capsys.readouterr().out
    assert str(checkout.resolve()) in output
    assert "token" not in output
    assert "secret" not in output


@pytest.mark.parametrize("invalid", ["missing", "invalid.bundle"])
def test_invalid_local_source_fails_before_any_registration(tmp_path: Path, invalid: str) -> None:
    # 2119: 2.3
    path = tmp_path / invalid
    if path.suffix == ".bundle":
        path.write_text("not a git bundle")

    with pytest.raises(RuntimeError, match=str(path)):
        qs.resolve_source(str(path))


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("https://github.com/acme/Widget.git", "Widget"),
        ("git@github.com:acme/widget.git", "widget"),
        ("ssh://git@github.com/acme/tools/widget/", "widget"),
    ],
)
def test_remote_friendly_name(source: str, name: str) -> None:
    # 2119: 2.7
    assert qs.resolve_source(source).name == name


def test_friendly_names_remove_only_one_terminal_suffix(tmp_path: Path) -> None:
    # 2119: 2.5, 2.7
    bundle = _bundle(tmp_path / "archive.bundle.bundle")

    assert qs.resolve_source(str(bundle)).name == "archive.bundle"
    assert qs.resolve_source("https://example.test/acme/widget.git.git").name == "widget.git"


def test_source_equivalence_is_bounded_by_source_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: 3.1
    # 2119: 3.2
    # 2119: 3.3
    # 2119: 3.4
    # 2119: 3.5
    # 2119: 3.6
    assert qs.sources_equivalent(
        " https://github.com/Acme/Widget.git/ ", "git@GITHUB.com:acme/widget"
    )
    assert qs.sources_equivalent(
        "ssh://git@GITHUB.com/Acme/Widget.git/", "https://github.com/acme/widget"
    )
    assert not qs.sources_equivalent(
        "https://github.com/acme/widget.git.git", "https://github.com/acme/widget"
    )
    assert not qs.sources_equivalent(
        "https://github.com/acme/widget.git", "https://github.com/other/widget.git"
    )
    assert not qs.sources_equivalent(
        "https://gitlab.com/acme/widget.git", "git@gitlab.com:acme/widget.git"
    )
    local = _worktree(tmp_path / "widget")
    dot_git = _worktree(tmp_path / "widget.git")
    local_alias = tmp_path / "widget-alias"
    local_alias.symlink_to(local, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert qs.sources_equivalent(f" {local} ", str(local.resolve()))
    assert qs.sources_equivalent(local_alias.as_posix(), str(local.resolve()))
    assert qs.sources_equivalent("widget-alias", str(local.resolve()))
    assert not qs.sources_equivalent(str(local), str(dot_git))

    first_bundle = _bundle(tmp_path / "first.bundle")
    first_without_suffix = tmp_path / "first"
    first_without_suffix.write_bytes(first_bundle.read_bytes())
    bundle_alias = tmp_path / "first-alias"
    bundle_alias.symlink_to(first_bundle)
    second_bundle = tmp_path / "second.bundle"
    second_bundle.write_bytes(first_bundle.read_bytes())
    assert qs.sources_equivalent("first-alias", str(first_bundle.resolve()))
    assert not qs.sources_equivalent(str(first_bundle), str(first_without_suffix))
    assert not qs.sources_equivalent(str(first_bundle), str(second_bundle))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "https://github.com/acme/widget.git",
            "https://git.example/acme/widget.git",
        ),
        (
            "https://github.com/acme/widget.git",
            "https://github.com/other/widget.git",
        ),
        (
            "https://github.com/acme/tools/widget.git",
            "https://github.com/acme/other/widget.git",
        ),
        (
            "https://gitlab.com/acme/widget.git",
            "https://gitlab.com/acme/widget",
        ),
        (
            "https://gitlab.com/acme/widget.git",
            "git@gitlab.com:acme/widget.git",
        ),
    ],
)
def test_source_equivalence_rejects_host_owner_path_and_non_github_inference(
    first: str, second: str
) -> None:
    # 2119: 3.3, 3.6
    assert not qs.sources_equivalent(first, second)


def test_non_github_remote_equivalence_accepts_only_exact_trimmed_text() -> None:
    # 2119: 3.6
    source = "https://gitlab.com/acme/widget.git"

    assert qs.sources_equivalent(f"  {source}  ", source)
    assert not qs.sources_equivalent(source, "https://gitlab.com/acme/widget")
    assert not qs.sources_equivalent(source, "git@gitlab.com:acme/widget.git")


def test_unsupported_legacy_file_url_is_not_equivalent_but_remains_invalid_input() -> None:
    legacy = "file://legacy-host/projects/widget.git"

    assert not qs.sources_equivalent(legacy, legacy)
    assert not qs.sources_equivalent(legacy, "/projects/widget.git")
    assert not qs.sources_equivalent("/projects/widget.git", legacy)
    with pytest.raises(RuntimeError, match="file URL has a remote host"):
        qs.resolve_source(legacy)


def test_same_basename_sources_get_distinct_stable_ids(tmp_path: Path) -> None:
    # 2119: 4.1
    one = _worktree(tmp_path / "one" / "widget")
    two = _worktree(tmp_path / "two" / "widget")

    one_id = qs.repo_id_from_url(str(one))
    two_id = qs.repo_id_from_url(str(two))

    assert one_id.startswith("widget-")
    assert two_id.startswith("widget-")
    assert one_id != two_id
    assert qs.repo_id_from_url(str(one)) == one_id


def test_existing_equivalent_source_is_reused_without_rebinding() -> None:
    # 2119: 4.2
    # 2119: 4.7
    # 2119: 4.8
    # 2119: 5.4
    # 2119: 6.1
    # 2119: 6.2
    existing = {
        "id": "legacy-widget",
        "name": "Team Widget",
        "git_url": "git@github.com:acme/widget.git",
        "enabled_workflows": ["custom", "github-peer-reviewed"],
        "default_harness": "claude",
        "default_model": "legacy-model",
        "env_file": "repo.env",
        "credential_dir": "repo-auth",
    }
    unrelated = {
        "id": "other-widget",
        "name": "Widget",
        "git_url": "https://github.com/other/widget.git",
        "enabled_workflows": ["local-git-self-reviewed"],
        "default_harness": "codex",
        "default_model": "other-model",
        "env_file": "other.env",
        "credential_dir": "other-auth",
    }
    unrelated_before = dict(unrelated)

    class _Client:
        def __init__(self) -> None:
            self.updates: list[tuple[str, dict[str, object]]] = []

        def list_repos(self) -> list[dict[str, object]]:
            return [unrelated, existing]

        def create_repo(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("equivalent source must be reused")

        def update_repo(self, repo_id: str, **changes: object) -> None:
            self.updates.append((repo_id, changes))

    client = _Client()
    result = qs.setup_repo(  # type: ignore[arg-type]
        client,
        "https://github.com/ACME/widget",
        None,
        default_harness="codex",
    )

    assert result == ("legacy-widget", "Team Widget")
    assert client.updates == [
        (
            "legacy-widget",
            {
                "enabled_workflows": [
                    "custom",
                    "github-peer-reviewed",
                    "github-self-reviewed",
                ]
            },
        )
    ]
    assert unrelated == unrelated_before


def test_existing_repo_scan_skips_unsupported_legacy_source() -> None:
    selected = "https://github.com/acme/widget.git"
    poisoned = {
        "id": "legacy-file-url",
        "name": "legacy",
        "git_url": "file://legacy-host/projects/widget.git",
    }
    unrelated = {
        "id": "unrelated",
        "name": "other-widget",
        "git_url": "https://github.com/other/widget.git",
    }
    current = {
        "id": "current",
        "name": "Current Widget",
        "git_url": "git@github.com:acme/widget.git",
        "enabled_workflows": ["github-self-reviewed", "github-peer-reviewed"],
    }

    class _Client:
        def list_repos(self) -> list[dict[str, object]]:
            return [poisoned, unrelated, current]

        def create_repo(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("equivalent source must be reused")

        def update_repo(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("fully configured source must not be mutated")

    assert qs.setup_repo(_Client(), selected, None) == (  # type: ignore[arg-type]
        "current",
        "Current Widget",
    )


def test_same_basename_existing_repo_is_not_reused_or_mutated() -> None:
    # 2119: 4.3
    # 2119: 4.6
    existing = {
        "id": "widget-deadbeef",
        "name": "widget",
        "git_url": "https://github.com/other/widget.git",
    }

    class _Client:
        def __init__(self) -> None:
            self.created: dict[str, object] = {}

        def list_repos(self) -> list[dict[str, object]]:
            return [existing]

        def create_repo(self, repo_id: str, name: str, git_url: str, **fields: object) -> None:
            self.created = {"id": repo_id, "name": name, "git_url": git_url, **fields}

        def update_repo(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("unrelated repo must not be changed")

    client = _Client()
    repo_id, _ = qs.setup_repo(  # type: ignore[arg-type]
        client,
        "https://github.com/acme/widget.git",
        None,
        default_harness="codex",
    )

    assert repo_id != existing["id"]
    assert client.created["git_url"] == "https://github.com/acme/widget.git"


def test_create_conflict_refreshes_then_reuses_only_equivalent_source() -> None:
    # 2119: 4.4
    source = "https://github.com/acme/widget.git"
    calls = 0

    class _Client:
        def list_repos(self) -> list[dict[str, object]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return [{"id": "winner", "name": "widget", "git_url": source}]

        def create_repo(self, *args: Any, **kwargs: Any) -> None:
            raise _http_status_error(409)

        def update_repo(self, *args: Any, **kwargs: Any) -> None:
            return None

    assert qs.setup_repo(_Client(), source, None) == ("winner", "widget")  # type: ignore[arg-type]


def test_unrelated_create_conflict_retries_distinct_id() -> None:
    # 2119: 4.5
    source = "https://github.com/acme/widget.git"

    class _Client:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def list_repos(self) -> list[dict[str, object]]:
            return [
                {
                    "id": self.ids[0] if self.ids else "unrelated",
                    "name": "widget",
                    "git_url": "https://github.com/other/widget.git",
                }
            ]

        def create_repo(self, repo_id: str, *args: Any, **kwargs: Any) -> None:
            self.ids.append(repo_id)
            if len(self.ids) == 1:
                raise _http_status_error(409)

    client = _Client()
    repo_id, _ = qs.setup_repo(client, source, None)  # type: ignore[arg-type]

    assert client.ids == [client.ids[0], repo_id]
    assert client.ids == list(qs.repo_id_candidates(source)[:2])


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com/acme/widget.git",
        "ssh://git@GITHUB.com/acme/widget.git",
        "git@github.com:acme/widget.git",
    ],
)
def test_only_supported_github_sources_enable_github_workflows(source: str) -> None:
    # 2119: 5.1
    assert qs.choose_enabled_workflows(source) == (
        "github-self-reviewed",
        "github-peer-reviewed",
    )


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com.evil.example/acme/widget.git",
        "https://prefixgithub.com/acme/widget.git",
        "https://github.com@evil.example/acme/widget.git",
        "https://github.com:443/acme/widget.git",
        "http://github.com/acme/widget.git",
        "ssh://git@github.com/acme/widget.git#fragment",
        "git@github.com:acme/widget.git?query",
        "https://gitlab.com/acme/widget.git",
        "file:///tmp/widget",
        "/tmp/widget",
        "/tmp/widget.bundle",
    ],
)
def test_non_github_and_deceptive_sources_get_only_local_workflow(source: str) -> None:
    # 2119: 5.2
    # 2119: 5.3
    assert qs.choose_enabled_workflows(source) == ("local-git-self-reviewed",)


def test_source_selection_module_has_no_llm_dependency_or_network_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2119: 6.3
    source = (Path(qs.__file__).parent / "source_selection.py").read_text()
    tree = ast.parse(source)
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imports.isdisjoint({"anthropic", "openai"})

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("source selection must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    selected = qs.select_source(
        cwd=tmp_path,
        input_fn=lambda _prompt: "https://github.com/acme/widget.git",
    )

    class _Client:
        def list_repos(self) -> list[dict[str, object]]:
            return []

        def create_repo(self, *_args: object, **_kwargs: object) -> None:
            return None

    assert selected.git_url == "https://github.com/acme/widget.git"
    assert qs.setup_repo(_Client(), selected.git_url, None)[1] == "widget"  # type: ignore[arg-type]
