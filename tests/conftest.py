"""Hermetic process environment for the test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_operator_auth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests opt into service auth explicitly; never consume the operator's live configuration."""
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_FILE", raising=False)
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_MODE", raising=False)
    monkeypatch.delenv("PANOPTICON_SERVICE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PANOPTICON_CONFIG", raising=False)


def _catalog_package(package_id: str, title: str, nodes: str) -> str:
    """A minimal valid Outfitter catalog package for the shared fixture root."""
    return (
        "version: 1\n"
        f"id: {package_id}\n"
        f"title: {title}\n"
        f"description: Synthetic {package_id} package for the test suite.\n"
        "actors:\n"
        "  dev:\n"
        "    kind: agent\n"
        "    profile: engineer\n"
        f"nodes:\n{nodes}"
    )


@pytest.fixture(scope="session")
def agents_fixture_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic Outfitter ``.agents`` root providing the three catalog packages.

    Keeps the workflow registry identical on every host: without it, discovery would register
    the ``outfitter-*`` workflows only where the developer's real ``~/.agents`` provides them.
    """
    root = tmp_path_factory.mktemp("agents-root")
    packages = {
        "founder": ("Founder", ["work", "commit", "push"]),
        "engineer": ("Engineer", ["research", "develop", "merge"]),
        "software-factory": ("Software factory", ["prepare", "implement", "merge"]),
    }
    for package_id, (title, actions) in packages.items():
        nodes = ""
        for index, action in enumerate(actions):
            nodes += f"  - id: {action}\n    action: {action}\n"
            nodes += f"    description: {action.capitalize()}.\n    actor: dev\n"
            if index:
                nodes += f"    needs: [{actions[index - 1]}]\n"
        package_dir = root / "workflows" / package_id
        package_dir.mkdir(parents=True)
        (package_dir / "workflow.yaml").write_text(_catalog_package(package_id, title, nodes))
    return root


@pytest.fixture(autouse=True)
def _pin_agents_root(monkeypatch: pytest.MonkeyPatch, agents_fixture_root: Path) -> None:
    """Every test resolves the Outfitter catalog from the synthetic root, never ``~/.agents``."""
    monkeypatch.setenv("PANOPTICON_AGENTS", str(agents_fixture_root))
