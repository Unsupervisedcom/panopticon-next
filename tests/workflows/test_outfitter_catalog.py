"""The Outfitter catalog workflows: vendored community-profiles packages projected onto lifecycles.

Covers the pinned-digest guard, Outfitter's contract and Panopticon's chain rule, the projection
(labels, gate policy, responsibilities), registration, and the skill/tool selection. No LLM.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from panopticon.core import Actor
from panopticon.workflows.discovery import discover_workflows
from panopticon.workflows.outfitter_catalog import (
    CATALOG_DIR,
    CATALOG_RELEASE,
    CATALOG_SHA256,
    CatalogWorkflow,
    InvalidCatalogWorkflow,
    OutfitterEngineer,
    OutfitterFounder,
    OutfitterSoftwareFactory,
    load_catalog_workflow,
    parse_catalog_workflow,
    project_states,
)

# 2119-spec: outfitter-workflows

PINNED_NODE_IDS = {
    "founder": ["work", "commit", "review", "push"],
    "engineer": ["research", "develop", "draft", "review", "merge"],
    "software-factory": ["prepare", "implement", "draft", "ci", "review", "merge"],
    "adversarial-review": ["inspect", "decide", "deliver"],
}


def _package(**overrides: object) -> dict[str, object]:
    """A minimal valid package: two agent nodes in a chain."""
    doc: dict[str, object] = {
        "version": 1,
        "id": "sample",
        "title": "Sample",
        "description": "A sample package.",
        "actors": {"dev": {"kind": "agent", "profile": "engineer"}},
        "environments": {"local": "workstation"},
        "integrations": {"gh": {"kind": "cli"}},
        "nodes": [
            {
                "id": "build",
                "action": "build-it",
                "description": "Build it.",
                "actor": "dev",
                "environment": "local",
                "uses": ["gh"],
            },
            {
                "id": "ship",
                "action": "ship-it",
                "description": "Ship it.",
                "actor": "dev",
                "needs": ["build"],
            },
        ],
    }
    doc.update(overrides)
    return doc


def _copy_catalog(tmp_path: Path) -> Path:
    """A writable copy of the vendored catalog, trusted without the digest check."""
    root = tmp_path / "catalog"
    for package in PINNED_NODE_IDS:
        (root / package).mkdir(parents=True)
        (root / package / "workflow.yaml").write_bytes(
            (CATALOG_DIR / package / "workflow.yaml").read_bytes()
        )
    return root


# -- 1: pinned vendored packages ------------------------------------------------------


# 2119: 1.1
def test_a_drifted_vendored_package_is_rejected_by_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy_catalog(tmp_path)
    path = root / "founder" / "workflow.yaml"
    path.write_text(path.read_text().replace("Ship a verified", "Ship an unverified"))
    monkeypatch.setattr("panopticon.workflows.outfitter_catalog.CATALOG_DIR", root)
    with pytest.raises(InvalidCatalogWorkflow, match=rf"'founder'.*{CATALOG_RELEASE}"):
        load_catalog_workflow("founder")
    # a package the pin table does not know is rejected the same way, never trusted
    (root / "rogue").mkdir()
    (root / "rogue" / "workflow.yaml").write_text(yaml.safe_dump(_package(id="rogue")))
    with pytest.raises(InvalidCatalogWorkflow, match="'rogue'"):
        load_catalog_workflow("rogue")


# 2119: 1.2
@pytest.mark.parametrize("package", sorted(PINNED_NODE_IDS))
def test_vendored_packages_match_their_pins_and_load(package: str) -> None:
    content = (CATALOG_DIR / package / "workflow.yaml").read_bytes()
    assert hashlib.sha256(content).hexdigest() == CATALOG_SHA256[package]
    workflow = load_catalog_workflow(package)
    assert workflow.id == package
    assert [node.id for node in workflow.nodes] == PINNED_NODE_IDS[package]


# 2119: 1.3
def test_an_unresolvable_nested_workflow_is_rejected(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    (root / "adversarial-review" / "workflow.yaml").unlink()
    with pytest.raises(InvalidCatalogWorkflow, match="'adversarial-review' is not readable"):
        load_catalog_workflow("founder", catalog_dir=root)
    # ...and a nested reference that loops back is a cycle, not a resolution
    (root / "loop").mkdir()
    (root / "loop" / "workflow.yaml").write_text(
        yaml.safe_dump(
            _package(
                id="loop",
                nodes=[{"id": "again", "workflow": "loop", "description": "Loop."}],
            )
        )
    )
    with pytest.raises(InvalidCatalogWorkflow, match="cycle"):
        load_catalog_workflow("loop", catalog_dir=root)


# -- 2: contract validation ----------------------------------------------------------


# 2119: 2.1
def test_a_non_v1_package_is_rejected() -> None:
    with pytest.raises(InvalidCatalogWorkflow, match="version 2"):
        parse_catalog_workflow(_package(version=2))
    with pytest.raises(InvalidCatalogWorkflow, match="version None"):
        parse_catalog_workflow(_package(version=None))


# 2119: 2.2
def test_a_node_needs_exactly_one_of_action_and_workflow() -> None:
    both = {"id": "x", "action": "a", "workflow": "w", "description": "Both."}
    neither = {"id": "x", "description": "Neither."}
    for node in (both, neither):
        with pytest.raises(InvalidCatalogWorkflow, match="exactly one of action / workflow"):
            parse_catalog_workflow(_package(nodes=[node]))
    parse_catalog_workflow(_package(nodes=[{"id": "x", "action": "a", "description": "One."}]))


# 2119: 2.3
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("actor", "ghost", "unknown actor 'ghost'"),
        ("environment", "moon", "unknown environment 'moon'"),
        ("uses", ["slack"], "unknown integration 'slack'"),
        ("needs", ["nowhere"], "unknown or self node 'nowhere'"),
    ],
)
def test_dangling_node_references_are_rejected(field: str, value: object, message: str) -> None:
    node: dict[str, object] = {"id": "x", "action": "a", "description": "X.", field: value}
    with pytest.raises(InvalidCatalogWorkflow, match=message):
        parse_catalog_workflow(_package(nodes=[node]))


# 2119: 2.4
def test_an_agent_actor_needs_a_profile() -> None:
    with pytest.raises(InvalidCatalogWorkflow, match="an agent needs a profile"):
        parse_catalog_workflow(_package(actors={"dev": {"kind": "agent"}}))
    parse_catalog_workflow(_package(actors={"dev": {"kind": "human"}}))  # humans need none


# 2119: 2.5
def test_non_chain_graphs_are_rejected() -> None:
    def node(node_id: str, *needs: str) -> dict[str, object]:
        return {"id": node_id, "action": "a", "description": f"{node_id}.", "needs": list(needs)}

    two_roots = [node("a"), node("b")]
    fan_in = [node("a"), node("b", "a"), node("c", "a", "b")]
    fan_out = [node("a"), node("b", "a"), node("c", "a")]
    unreachable = [node("a"), node("b", "c"), node("c", "b")]
    for nodes, message in (
        (two_roots, "exactly one root"),
        (fan_in, "needs 2 nodes"),
        (fan_out, "fans out"),
        (unreachable, "not reachable"),
    ):
        with pytest.raises(InvalidCatalogWorkflow, match=message):
            parse_catalog_workflow(_package(nodes=nodes))
    chain = parse_catalog_workflow(_package(nodes=[node("b", "a"), node("a")]))  # any file order
    assert [n.id for n in chain.nodes] == ["a", "b"]


# -- 3: projection -------------------------------------------------------------------


# 2119: 3.1
def test_states_follow_the_chain_with_upper_cased_labels() -> None:
    assert OutfitterFounder().ordered_phases() == ["WORK", "COMMIT", "REVIEW", "PUSH", "COMPLETE"]
    assert OutfitterEngineer().ordered_phases() == [
        "RESEARCH",
        "DEVELOP",
        "DRAFT",
        "REVIEW",
        "MERGE",
        "COMPLETE",
    ]
    assert OutfitterSoftwareFactory().ordered_phases() == [
        "PREPARE",
        "IMPLEMENT",
        "DRAFT",
        "CI",
        "REVIEW",
        "MERGE",
        "COMPLETE",
    ]
    dotted = parse_catalog_workflow(
        _package(nodes=[{"id": "wait-for.ci_run", "action": "a", "description": "Wait."}])
    )
    assert [state.label for state in project_states(dotted)] == ["WAIT_FOR_CI_RUN"]


# 2119: 3.2
def test_action_nodes_are_agent_work_with_one_responsibility() -> None:
    factory = OutfitterSoftwareFactory()
    for label, action, description in (
        ("PREPARE", "receive-typed-issue", "Accept one typed issue."),
        ("CI", "wait-for-required-ci", "Wait for required checks to pass."),
        ("MERGE", "merge-after-approval", "Let the platform merge after every required gate."),
    ):
        assert factory.advanced_by(label) is Actor.AGENT
        assert [(r.key, r.description) for r in factory.responsibilities(label)] == [
            (action, description)
        ]
    assert factory.turn_on_enter("IMPLEMENT") is Actor.AGENT
    assert factory.turn_on_enter("PREPARE") is Actor.USER  # the initial state waits for the user


# 2119: 3.3
def test_nested_workflow_nodes_are_user_gates() -> None:
    for workflow in (OutfitterFounder(), OutfitterEngineer(), OutfitterSoftwareFactory()):
        assert workflow.advanced_by("REVIEW") is Actor.USER
        assert [r.key for r in workflow.responsibilities("REVIEW")] == ["adversarial-review"]
        assert "Delegates to the `adversarial-review` workflow" in workflow.description("REVIEW")


# 2119: 3.4
def test_human_actor_nodes_are_the_users_work() -> None:
    package = parse_catalog_workflow(
        _package(
            actors={"dev": {"kind": "agent", "profile": "engineer"}, "boss": {"kind": "human"}},
            nodes=[
                {"id": "build", "action": "build-it", "description": "Build.", "actor": "dev"},
                {
                    "id": "sign",
                    "action": "sign-off",
                    "description": "Sign off.",
                    "actor": "boss",
                    "needs": ["build"],
                },
            ],
        )
    )
    build, sign = project_states(package)
    assert build.advanced_by is Actor.AGENT and build.responsibilities  # type: ignore[attr-defined]
    assert sign.turn_on_enter is Actor.USER
    assert sign.advanced_by is Actor.USER  # type: ignore[attr-defined]
    assert sign.responsibilities == ()


# 2119: 3.5
def test_agent_actor_states_name_the_outfitter_profile() -> None:
    assert "Outfitter profile `founder`" in OutfitterFounder().description("WORK")
    assert "Outfitter profile `engineer`" in OutfitterEngineer().description("DEVELOP")
    assert "Outfitter profile `resident-engineer`" in OutfitterSoftwareFactory().description(
        "IMPLEMENT"
    )
    # a system actor has no profile and says so
    assert "`platform` (system)" in OutfitterSoftwareFactory().description("MERGE")


# -- 4: registration -----------------------------------------------------------------


# 2119: 4.1
def test_discovery_registers_the_three_opt_in_workflows(tmp_path: Path) -> None:
    registry = discover_workflows(_home_workflows=tmp_path / "none")
    names = {"outfitter-founder", "outfitter-engineer", "outfitter-software-factory"}
    assert names <= set(registry)
    for name in names:
        assert registry[name].opt_in is True
        assert registry[name].hidden is False
        assert f"community-profiles {CATALOG_RELEASE}" in registry[name].when_to_use


# 2119: 4.2
def test_outfitter_workflows_leave_the_review_pair_unset() -> None:
    for workflow in (OutfitterFounder(), OutfitterEngineer(), OutfitterSoftwareFactory()):
        assert workflow.review_harness is None and workflow.review_model is None


# -- 5: skills and tools -------------------------------------------------------------


# 2119: 5.1
# 2119: 5.2
# 2119: 5.3
def test_skills_follow_the_package_actions() -> None:
    assert [s.name for s in OutfitterFounder().skills()] == ["push-branch"]
    assert [s.name for s in OutfitterEngineer().skills()] == [
        "open-pr",
        "babysit-ci",
        "babysit-merge",
    ]
    assert [s.name for s in OutfitterSoftwareFactory().skills()] == [
        "open-pr",
        "babysit-ci",
        "babysit-merge",
    ]
    push = OutfitterFounder().skills()[0]
    assert "git -C /workspace push" in push.instructions and "set_url" in push.instructions


def _workflow_for(package: CatalogWorkflow) -> OutfitterFounder:
    """A founder instance whose catalog is swapped for ``package`` (skills/tools read it)."""
    workflow = OutfitterFounder()
    workflow.catalog = package  # type: ignore[misc]
    return workflow


# 2119: 5.4
def test_gh_tool_only_with_a_github_integration() -> None:
    assert [t.name for t in OutfitterFounder().tools()] == ["gh"]  # `gh` CLI integration
    assert [t.name for t in OutfitterSoftwareFactory().tools()] == ["gh"]  # github-write MCP
    local_only = parse_catalog_workflow(
        _package(
            integrations={"git": {"kind": "transport"}},
            nodes=[{"id": "x", "action": "a", "description": "X.", "uses": ["git"]}],
        )
    )
    assert _workflow_for(local_only).tools() == ()
    assert _workflow_for(local_only).skills() == ()  # and no forge skills without forge actions
