"""The checked-in evaluator walkthrough stays executable and honest."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()
WALKTHROUGH = (ROOT / "docs" / "getting-started.md").read_text()
WALKTHROUGH_TEXT = " ".join(WALKTHROUGH.split())
WALKTHROUGH_FOLDED = WALKTHROUGH_TEXT.casefold()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
RELEASE_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_install_docs_name_one_public_install_and_onboarding_command() -> None:
    # 2119: REQ-054.1.1
    command = "pipx install panopticon-next && panopticon quickstart"
    for document in (README, WALKTHROUGH):
        assert command in document
        remaining = document.replace(command, "")
        remaining = remaining.replace("pipx install panopticon-next", "")
        remaining = remaining.replace(
            'pipx install "./panopticon_next-${PANOPTICON_RELEASE_VERSION}-py3-none-any.whl"',
            "",
        )
        assert "pipx install " not in remaining
        assert "panopticon-next @ git+" not in document
        assert "@main" not in document


def test_release_marker_cannot_consume_the_wheel_compatibility_tag() -> None:
    assert f"PANOPTICON_RELEASE_VERSION={RELEASE_VERSION} # x-release-please-version" in WALKTHROUGH
    assert "x-release-please-start-version\npipx install" not in WALKTHROUGH


def test_ci_installs_the_wheel_and_runs_its_executable_outside_the_checkout() -> None:
    # 2119: REQ-054.1.2
    smoke = CI.split("- name: Smoke-test clean wheel", 1)[1]
    assert "uv pip install --python .wheel-venv/bin/python dist/*.whl" in smoke
    assert "cd /tmp" in smoke
    assert '"$GITHUB_WORKSPACE/.wheel-venv/bin/panopticon" --version' in smoke
    assert '"$GITHUB_WORKSPACE/.wheel-venv/bin/python" -c' in smoke


def test_walkthrough_uses_self_review_and_distinguishes_peer_review() -> None:
    # 2119: REQ-054.4.2
    # 2119: REQ-054.4.3
    assert "Select `github-self-reviewed`" in WALKTHROUGH_TEXT
    assert "the initiating operator approves the work" in WALKTHROUGH_TEXT
    assert "`github-peer-reviewed`" in WALKTHROUGH_TEXT
    assert "requires another person to approve the pull request" in WALKTHROUGH_TEXT


def test_walkthrough_names_the_real_advance_command_for_supported_evaluator_harnesses() -> None:
    # 2119: REQ-054.7.4
    commands = {
        "Claude": "`/advance`",
        "Codex": "`$advance`",
    }
    for harness, command in commands.items():
        assert f"| {harness} | {command} |" in WALKTHROUGH

    # A bare instruction to type /advance was the former, Claude-only overclaim.
    assert "type `/advance`" not in README
    assert "type `/advance`" not in WALKTHROUGH


def test_walkthrough_limits_the_complete_github_path_to_compatible_harnesses() -> None:
    # 2119: REQ-054.7.4
    assert "complete GitHub walkthrough below currently requires Claude or Codex" in WALKTHROUGH
    assert "current adapters cannot execute workflow skills" in WALKTHROUGH


def test_walkthrough_requires_a_headed_artifact_and_pull_request_check() -> None:
    # 2119: REQ-054.7.10
    assert "headed machine used for the release rehearsal" in WALKTHROUGH_TEXT
    assert "shows `plan.md` in the configured document application" in WALKTHROUGH_TEXT
    assert "shows the exact task pull request in the browser" in WALKTHROUGH_TEXT


def test_walkthrough_has_ordered_success_checks_for_every_evaluation_stage() -> None:
    # 2119: REQ-054.7.3
    success_checks = [part.casefold() for part in WALKTHROUGH.split("Success check:")[1:]]
    expected_checks = (
        ("quickstart", "dashboard", "setup-repo"),
        ("fresh shell", "panopticon tasks", "401"),
        ("task", "queued"),
        ("container", "live"),
        ("attach", "ctrl-b d", "dashboard"),
        ("plan.md", "open"),
        ("approval", "iterating"),
        ("pull request", "diff"),
        ("merging", "complete", "merged"),
        ("docker", "tmux", "gone"),
    )

    check_index = 0
    for expected_words in expected_checks:
        while check_index < len(success_checks) and not all(
            word in success_checks[check_index] for word in expected_words
        ):
            check_index += 1
        assert check_index < len(success_checks), (
            f"missing ordered Success check containing {expected_words!r}"
        )
        check_index += 1


def test_walkthrough_documents_retention_and_verifiable_teardown() -> None:
    # 2119: REQ-054.6.5
    evidence = (
        "panopticon stop",
        "docker ps --all --quiet --filter label=panopticon.task",
        "tmux -L panopticon has-session",
        "configuration",
        "repository settings",
        "credentials",
        "task records",
        "retained artifacts",
        "remain on disk",
        "second stop",
    )
    assert all(item.casefold() in WALKTHROUGH_FOLDED for item in evidence)


def test_walkthrough_names_every_input_instead_of_relying_on_hidden_state() -> None:
    # 2119: REQ-054.7.4
    documented_inputs = (
        "pipx install panopticon-next && panopticon quickstart",
        "cd /path/to/disposable-repo",
        "git remote get-url origin",
        "working authentication for the selected harness",
        "gh_token",
        "open a new shell",
        "unset panopticon_service_auth_file panopticon_service_auth_mode",
    )
    forbidden_local_inputs = ("/users/tyler", "~/experiments", "mac-studio")
    assert all(item.casefold() in WALKTHROUGH_FOLDED for item in documented_inputs)
    assert not any(item in WALKTHROUGH_FOLDED for item in forbidden_local_inputs)


def _snapshot_files(paths: list[Path]) -> dict[Path, tuple[bytes, int, int, int]]:
    snapshot = {}
    for path in paths:
        metadata = path.stat(follow_symlinks=False)
        snapshot[path] = (
            path.read_bytes(),
            metadata.st_ino,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
        )
    return snapshot


def test_repeated_stop_removes_runtime_and_preserves_stored_state(tmp_path: Path) -> None:
    # 2119: REQ-054.6.1
    # 2119: REQ-054.6.2
    # 2119: REQ-054.6.3
    # 2119: REQ-054.6.4
    config_root = tmp_path / "panopticon-config"
    data_root = tmp_path / "panopticon-data"
    cache_root = tmp_path / "panopticon-cache"
    retained = {
        config_root / "workflows" / "local.py": b"repository configuration",
        config_root / "secrets" / "task-service-auth.json": b'{"read":["read-token"]}',
        config_root / "secrets" / "panopticon.env": b"GH_TOKEN=secret-value\n",
        data_root / "panopticon.db": b"task records",
        data_root / "artifacts" / "task-one" / "plan.md": b"retained task output",
        cache_root / "repos" / "sample" / "HEAD": b"cached repository state",
    }
    for path, contents in retained.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.chmod(0o600 if "secrets" in path.parts else 0o640)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    docker_removed = tmp_path / "docker-removed"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
printf 'docker:%s\\n' "$*" >> "$PANOPTICON_TEST_COMMAND_LOG"
if [ "$1" = "ps" ] && [ ! -e "$PANOPTICON_TEST_DOCKER_REMOVED" ]; then
    printf 'container-one\\ncontainer-two\\n'
elif [ "$1" = "rm" ]; then
    : > "$PANOPTICON_TEST_DOCKER_REMOVED"
fi
"""
    )
    docker.chmod(0o755)
    tmux = fake_bin / "tmux"
    tmux.write_text(
        """#!/bin/sh
printf 'tmux:%s\\n' "$*" >> "$PANOPTICON_TEST_COMMAND_LOG"
"""
    )
    tmux.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PANOPTICON_CONFIG": str(config_root),
            "PANOPTICON_DATA": str(data_root),
            "PANOPTICON_CACHE": str(cache_root),
            "PANOPTICON_TEST_COMMAND_LOG": str(command_log),
            "PANOPTICON_TEST_DOCKER_REMOVED": str(docker_removed),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    cli = "from panopticon.terminal.__main__ import main; raise SystemExit(main(['stop']))"
    retained_paths = list(retained)
    before = _snapshot_files(retained_paths)

    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", cli],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert _snapshot_files(retained_paths) == before

    commands = command_log.read_text().splitlines()
    assert commands.count("docker:ps --all --quiet --filter label=panopticon.task") == 2
    assert commands.count("docker:rm --force container-one container-two") == 1
    assert commands.count("tmux:-L panopticon kill-server") == 2
