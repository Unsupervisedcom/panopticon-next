"""The Outfitter catalog workflows: `.agents`-resolved packages projected onto lifecycles.

Covers resolution through the Outfitter ``.agents`` root (layer precedence, the checkout-cache
key, settings.local.yml replacement, unavailability), Outfitter's contract and Panopticon's chain rule, the projection
(labels, gate policy, responsibilities), registration, and the skill/tool selection. No LLM.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

import panopticon.workflows.outfitter_catalog as outfitter_catalog
from panopticon.core import Actor
from panopticon.workflows.discovery import discover_workflows
from panopticon.workflows.outfitter_catalog import (
    AGENTS_ENV,
    CatalogUnavailable,
    CatalogWorkflow,
    InvalidCatalogWorkflow,
    OutfitterCatalogWorkflow,
    agents_root,
    catalog_workflow,
    enabled_workflows,
    load_catalog_workflow,
    parse_catalog_workflow,
    project_states,
    workflow_provider,
)

# 2119-spec: outfitter-workflows


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


def _provide(layer: Path, document: dict[str, object]) -> Path:
    """Write ``document`` as ``<layer>/workflows/<id>/workflow.yaml``; return the layer."""
    package_dir = layer / "workflows" / str(document["id"])
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "workflow.yaml").write_text(yaml.safe_dump(document))
    return layer


# -- 1: catalog resolution ------------------------------------------------------------


# 2119: 1.1
def test_packages_come_from_the_agents_root_and_nothing_is_shipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_root = _provide(tmp_path / "operator-agents", _package(title="From the environment"))
    monkeypatch.setenv(AGENTS_ENV, str(operator_root))
    assert agents_root() == operator_root
    assert load_catalog_workflow("sample").title == "From the environment"
    monkeypatch.delenv(AGENTS_ENV)
    fake_home = tmp_path / "home"
    default_root = _provide(fake_home / ".agents", _package(title="From the default root"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert agents_root() == default_root
    assert load_catalog_workflow("sample").title == "From the default root"
    # the default root is read only when no explicit root is passed
    root = _provide(tmp_path / "root", _package(title="From the root"))
    assert load_catalog_workflow("sample", root=root).title == "From the root"
    # and the repository ships no catalog: there is no package directory beside the module
    assert not (Path(outfitter_catalog.__file__).with_name("outfitter")).exists()
    fake_module = tmp_path / "installed" / "outfitter_catalog.py"
    fake_module.parent.mkdir()
    fake_module.write_text("")
    _provide(fake_module.parent, _package(title="Forbidden packaged fallback"))
    monkeypatch.setattr(outfitter_catalog, "__file__", str(fake_module))
    with pytest.raises(CatalogUnavailable):
        load_catalog_workflow("sample", root=tmp_path / "empty-root")


# 2119: 1.2
def test_the_root_layer_wins_then_sources_in_listed_order(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first = _provide(tmp_path / "first", _package(title="From the first source"))
    second = _provide(tmp_path / "second", _package(title="From the second source"))
    root.mkdir()
    (root / "settings.yml").write_text(
        yaml.safe_dump({"sources": [{"path": str(first)}, {"path": str(second)}]})
    )
    assert load_catalog_workflow("sample", root=root).title == "From the first source"
    first_only_other = tmp_path / "first-only-other"
    first_only_other.mkdir()
    (root / "settings.yml").write_text(
        yaml.safe_dump({"sources": [{"path": str(first_only_other)}, {"path": str(second)}]})
    )
    assert load_catalog_workflow("sample", root=root).title == "From the second source"
    _provide(root, _package(title="From the root itself"))
    assert load_catalog_workflow("sample", root=root).title == "From the root itself"


# 2119: 1.3
def test_a_remote_source_resolves_at_its_checkout_cache_key(tmp_path: Path) -> None:
    # the exact directory name Outfitter creates for this source on a real machine
    expected_key = (
        "Z2l0K2h0dHBzOi8vZ2l0aHViLmNvbS9haS1vdXRmaXR0ZXIvY29tbXVuaXR5LXByb2ZpbGVzLmdpdCNlOWVl"
        "OTcyNTI0NjAxM2IzZWQ4NzIxZTBkYjA3NDc1Zjc2Y2MwZDI4"
    )
    source = "git+https://github.com/ai-outfitter/community-profiles.git"
    ref = "e9ee9725246013b3ed8721e0db07475f76cc0d28"
    assert base64.urlsafe_b64encode(f"{source}#{ref}".encode()).decode().rstrip("=") == expected_key

    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yml").write_text(
        yaml.safe_dump({"sources": [{"github": "ai-outfitter/community-profiles", "ref": ref}]})
    )
    _provide(root / "cache" / "repos" / expected_key, _package(title="From the checkout"))
    assert load_catalog_workflow("sample", root=root).title == "From the checkout"
    # a `uri` source resolves the same way, and the key is unpadded url-safe base64
    uri = "git+https://example.test/¾.git"
    key = base64.urlsafe_b64encode(f"{uri}#v1.0.0".encode()).decode().rstrip("=")
    standard_key = base64.b64encode(f"{uri}#v1.0.0".encode()).decode().rstrip("=")
    assert "+" in standard_key and "-" in key
    assert "=" not in key and "+" not in key and "/" not in key
    (root / "settings.yml").write_text(yaml.safe_dump({"sources": [{"uri": uri, "ref": "v1.0.0"}]}))
    _provide(root / "cache" / "repos" / key, _package(title="From the uri checkout"))
    assert load_catalog_workflow("sample", root=root).title == "From the uri checkout"

    credentialed = "git+https://user:secret@example.test/private.git"
    redacted = "git+https://REDACTED@example.test/private.git"
    private_key = base64.urlsafe_b64encode(f"{redacted}#v1.0.0".encode()).decode().rstrip("=")
    custom_cache = tmp_path / "custom-cache"
    (root / "settings.yml").write_text(
        yaml.safe_dump(
            {
                "cache_directory": str(custom_cache),
                "sources": [{"uri": credentialed, "ref": "v1.0.0"}],
            }
        )
    )
    _provide(custom_cache / "repos" / private_key, _package(title="From the private checkout"))
    assert load_catalog_workflow("sample", root=root).title == "From the private checkout"

    (root / "settings.yml").write_text(
        yaml.safe_dump({"sources": [{"uri": uri, "ref": "v1.0.0", "path": "../escape"}]})
    )
    with pytest.raises(InvalidCatalogWorkflow, match="must stay inside"):
        load_catalog_workflow("sample", root=root)


# 2119: 1.4
def test_settings_local_sources_replace_the_settings_list_wholesale(tmp_path: Path) -> None:
    committed = _provide(tmp_path / "committed", _package(title="From settings.yml"))
    _provide(committed, _package(id="committed-only", title="Committed only"))
    local = _provide(tmp_path / "local", _package(title="From settings.local.yml"))
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yml").write_text(yaml.safe_dump({"sources": [{"path": str(committed)}]}))
    (root / "settings.local.yml").write_text(yaml.safe_dump({"sources": [{"path": str(local)}]}))
    assert load_catalog_workflow("sample", root=root).title == "From settings.local.yml"
    with pytest.raises(CatalogUnavailable, match="committed-only"):
        load_catalog_workflow("committed-only", root=root)
    (root / "settings.local.yml").write_text("sources: []\n")
    with pytest.raises(CatalogUnavailable, match="sample"):
        load_catalog_workflow("sample", root=root)
    # a local settings file without a `sources` key does not mask the committed list
    (root / "settings.local.yml").write_text(yaml.safe_dump({"default_agent": "founder"}))
    assert load_catalog_workflow("sample", root=root).title == "From settings.yml"


# 2119: 4.5
# 2119: 4.6
def test_enabled_workflows_are_an_ordered_union_and_nested_packages_stay_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _provide(
        tmp_path / "root",
        _package(
            id="root",
            title="Root",
            nodes=[{"id": "delegate", "workflow": "nested", "description": "Delegate."}],
        ),
    )
    _provide(root, _package(id="nested", title="Nested"))
    (root / "settings.yml").write_text("workflows: [root, shared]\n")
    (root / "settings.local.yml").write_text("workflows: [shared, local]\n")
    _provide(root, _package(id="shared", title="Shared"))
    _provide(root, _package(id="local", title="Local"))
    assert enabled_workflows(root) == ("root", "shared", "local")

    monkeypatch.setenv(AGENTS_ENV, str(root))
    registry = discover_workflows(_home_workflows=tmp_path / "none")
    assert "outfitter-root" in registry
    assert "outfitter-shared" in registry
    assert "outfitter-local" in registry
    assert "outfitter-nested" not in registry

    (root / "settings.local.yml").write_text("workflows: [local, nested]\n")
    registry = discover_workflows(_home_workflows=tmp_path / "none")
    assert "outfitter-nested" in registry


# 2119: 1.5
def test_a_package_no_layer_provides_is_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "empty-root"
    root.mkdir()
    with pytest.raises(CatalogUnavailable, match=rf"'sample'.*{root}"):
        load_catalog_workflow("sample", root=root)
    assert issubclass(CatalogUnavailable, InvalidCatalogWorkflow)


# 2119: 1.6
def test_an_unresolvable_nested_workflow_is_rejected(tmp_path: Path) -> None:
    root = _provide(
        tmp_path / "root",
        _package(nodes=[{"id": "review", "workflow": "adversarial-review", "description": "R."}]),
    )
    with pytest.raises(CatalogUnavailable, match="'adversarial-review'"):
        load_catalog_workflow("sample", root=root)
    # ...and a nested reference that loops back is a cycle, not a resolution
    _provide(
        root,
        _package(
            id="loop",
            nodes=[{"id": "again", "workflow": "loop", "description": "Loop."}],
        ),
    )
    with pytest.raises(InvalidCatalogWorkflow, match="cycle"):
        load_catalog_workflow("loop", root=root)
    _provide(
        root,
        _package(
            id="cycle-a",
            nodes=[{"id": "next", "workflow": "cycle-b", "description": "Next."}],
        ),
    )
    _provide(
        root,
        _package(
            id="cycle-b",
            nodes=[{"id": "back", "workflow": "cycle-a", "description": "Back."}],
        ),
    )
    with pytest.raises(InvalidCatalogWorkflow, match="cycle-a -> cycle-b -> cycle-a"):
        load_catalog_workflow("cycle-a", root=root)
    # Only a registered root must be a chain. A nested workflow remains one projected gate,
    # so its valid DAG may fan out without invalidating the root.
    _provide(
        root,
        _package(
            id="nested-dag",
            nodes=[
                {"id": "classify", "action": "classify", "description": "Classify."},
                {
                    "id": "bug",
                    "action": "file-bug",
                    "description": "Bug.",
                    "needs": ["classify"],
                },
                {
                    "id": "feature",
                    "action": "file-feature",
                    "description": "Feature.",
                    "needs": ["classify"],
                },
            ],
        ),
    )
    _provide(
        root,
        _package(
            id="delegates-to-dag",
            nodes=[{"id": "triage", "workflow": "nested-dag", "description": "Triage."}],
        ),
    )
    assert load_catalog_workflow("delegates-to-dag", root=root).id == "delegates-to-dag"


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
    # the projected workflows materialize from the suite's fixture root (tests/conftest.py)
    assert catalog_workflow("founder").ordered_phases() == ["WORK", "COMMIT", "PUSH", "COMPLETE"]
    assert catalog_workflow("engineer").ordered_phases() == [
        "RESEARCH",
        "DEVELOP",
        "MERGE",
        "COMPLETE",
    ]
    dotted = parse_catalog_workflow(
        _package(
            nodes=[
                {
                    "id": "ship",
                    "action": "b",
                    "description": "Ship.",
                    "needs": ["wait-for.ci_run"],
                },
                {"id": "wait-for.ci_run", "action": "a", "description": "Wait."},
            ]
        )
    )
    assert [state.label for state in project_states(dotted)] == ["WAIT_FOR_CI_RUN", "SHIP"]


# 2119: 3.2
def test_action_nodes_are_agent_work_with_one_responsibility() -> None:
    build, ship = project_states(parse_catalog_workflow(_package()))
    assert build.advanced_by is Actor.AGENT  # type: ignore[attr-defined]
    assert [(r.key, r.description) for r in build.responsibilities] == [  # type: ignore[attr-defined]
        ("build-it", "Build it.")
    ]
    assert build.turn_on_enter is Actor.USER  # the initial state waits for the user
    assert ship.turn_on_enter is Actor.AGENT
    assert [r.key for r in ship.responsibilities] == ["ship-it"]  # type: ignore[attr-defined]


# 2119: 3.3
def test_nested_workflow_nodes_are_user_gates() -> None:
    package = parse_catalog_workflow(
        _package(
            nodes=[
                {"id": "build", "action": "build-it", "description": "Build.", "actor": "dev"},
                {
                    "id": "review",
                    "workflow": "adversarial-review",
                    "description": "Get a review.",
                    "needs": ["build"],
                },
            ]
        )
    )
    _, review = project_states(package)
    assert review.advanced_by is Actor.USER  # type: ignore[attr-defined]
    assert [r.key for r in review.responsibilities] == ["adversarial-review"]  # type: ignore[attr-defined]
    assert "Delegates to the `adversarial-review` workflow" in review.description


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
    package = parse_catalog_workflow(
        _package(
            actors={
                "dev": {"kind": "agent", "profile": "resident-engineer"},
                "platform": {"kind": "system"},
            },
            nodes=[
                {"id": "build", "action": "build-it", "description": "Build.", "actor": "dev"},
                {
                    "id": "land",
                    "action": "merge-after-approval",
                    "description": "Merge.",
                    "actor": "platform",
                    "needs": ["build"],
                },
            ],
        )
    )
    build, land = project_states(package)
    assert "Outfitter profile `resident-engineer`" in build.description
    # a system actor has no profile and says so
    assert "`platform` (system)" in land.description


# -- 4: registration -----------------------------------------------------------------


# 2119: 4.1
def test_discovery_registers_every_provided_package_without_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the fixture root's packages appear, named outfitter-<id>
    registry = discover_workflows(_home_workflows=tmp_path / "none")
    names = {"outfitter-founder", "outfitter-engineer", "outfitter-software-factory"}
    assert names <= set(registry)
    for name in names:
        assert registry[name].opt_in is True
        assert registry[name].hidden is False
        assert "Outfitter catalog" in registry[name].when_to_use
    # a novel package id needs no Python class: provide it through a settings.yml source
    source = _provide(tmp_path / "source", _package(id="ship-it", title="Ship it"))
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yml").write_text(yaml.safe_dump({"sources": [{"path": str(source)}]}))
    (root / "settings.local.yml").write_text("workflows: [ship-it]\n")
    monkeypatch.setenv(AGENTS_ENV, str(root))
    registry = discover_workflows(_home_workflows=tmp_path / "none")
    shipped = registry["outfitter-ship-it"]
    assert isinstance(shipped, OutfitterCatalogWorkflow)
    assert shipped.opt_in is True and "Ship it" in shipped.when_to_use


# 2119: 4.2
def test_outfitter_workflows_leave_the_review_pair_unset() -> None:
    provided = list(workflow_provider())
    assert provided  # the fixture root provides packages
    for workflow in provided:
        assert workflow.review_harness is None and workflow.review_model is None


# 2119: 4.3
def test_an_empty_agents_root_skips_the_workflows_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-agents-root"
    _provide(empty, _package(id="disabled", title="Disabled"))
    (empty / "settings.yml").write_text("workflows: []\n")
    monkeypatch.setenv(AGENTS_ENV, str(empty))
    registry = discover_workflows(_home_workflows=tmp_path / "none")
    assert not {name for name in registry if name.startswith("outfitter-")}
    assert "spike" in registry  # the rest of the registry is unaffected
    (empty / "settings.yml").write_text("workflows:\n")
    assert enabled_workflows(empty) == ()


# 2119: 4.4
def test_a_broken_package_is_skipped_and_the_rest_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = _provide(tmp_path / "root", _package(id="good", title="Good"))
    fan_out = _package(
        id="broken",
        nodes=[
            {"id": "a", "action": "x", "description": "A."},
            {"id": "b", "action": "x", "description": "B.", "needs": ["a"]},
            {"id": "c", "action": "x", "description": "C.", "needs": ["a"]},
        ],
    )
    _provide(root, fan_out)
    duplicate_labels = _package(
        id="broken-projection",
        nodes=[
            {"id": "a-b", "action": "x", "description": "A."},
            {"id": "a_b", "action": "x", "description": "B.", "needs": ["a-b"]},
        ],
    )
    _provide(root, duplicate_labels)
    reserved_terminal = _package(
        id="reserved-terminal",
        nodes=[{"id": "complete", "action": "finish", "description": "Finish."}],
    )
    _provide(root, reserved_terminal)
    method_name = _package(
        id="method-name",
        integrations={},
        nodes=[{"id": "tools", "action": "inspect", "description": "Inspect."}],
    )
    _provide(root, method_name)
    (root / "settings.yml").write_text(
        "workflows: [good, broken, broken-projection, reserved-terminal, method-name]\n"
    )
    monkeypatch.setenv(AGENTS_ENV, str(root))
    registry = discover_workflows(_home_workflows=tmp_path / "none")
    assert "outfitter-good" in registry
    assert "outfitter-broken" not in registry
    assert "outfitter-broken-projection" not in registry
    assert "outfitter-reserved-terminal" not in registry
    assert registry["outfitter-method-name"].tools() == ()
    assert "spike" in registry
    assert "skipping catalog package 'broken'" in caplog.text
    assert "skipping catalog package 'broken-projection'" in caplog.text
    assert "skipping catalog package 'reserved-terminal'" in caplog.text


# -- 5: skills and tools -------------------------------------------------------------


def _workflow_for(package: CatalogWorkflow) -> OutfitterCatalogWorkflow:
    """A projected instance whose catalog is swapped for ``package`` (skills/tools read it)."""
    workflow = catalog_workflow("founder")
    workflow.catalog = package  # type: ignore[misc]
    return workflow


def _forge_package(*actions: str) -> CatalogWorkflow:
    nodes: list[dict[str, object]] = []
    for index, action in enumerate(actions):
        node: dict[str, object] = {
            "id": f"n{index}",
            "action": action,
            "description": f"{action}.",
            "actor": "dev",
        }
        if index:
            node["needs"] = [f"n{index - 1}"]
        nodes.append(node)
    return parse_catalog_workflow(_package(nodes=nodes))


# 2119: 5.1
# 2119: 5.2
def test_forge_skills_follow_the_package_actions() -> None:
    engineer_like = _forge_package("develop", "open-draft-pr", "merge-as-human")
    assert [s.name for s in _workflow_for(engineer_like).skills()] == [
        "open-pr",
        "babysit-ci",
        "babysit-merge",
    ]
    factory_like = _forge_package("open-draft-pr", "wait-for-required-ci", "merge-after-approval")
    assert [s.name for s in _workflow_for(factory_like).skills()] == [
        "open-pr",
        "babysit-ci",
        "babysit-merge",
    ]
    current_engineer = _forge_package("open-draft-pr", "verify-and-merge-as-human")
    assert [s.name for s in _workflow_for(current_engineer).skills()] == [
        "open-pr",
        "babysit-ci",
        "babysit-merge",
    ]


# 2119: 5.3
def test_a_push_as_human_package_gets_the_push_branch_skill() -> None:
    founder_like = _forge_package("work", "commit", "push-as-human")
    (push,) = _workflow_for(founder_like).skills()
    assert push.name == "push-branch"
    assert "git -C /workspace push" in push.instructions and "set_url" in push.instructions


# 2119: 5.4
def test_gh_tool_only_with_a_github_integration() -> None:
    cli = parse_catalog_workflow(_package())  # `gh` CLI integration
    assert [t.name for t in _workflow_for(cli).tools()] == ["gh"]
    mcp = parse_catalog_workflow(
        _package(
            integrations={"forge": {"kind": "mcp", "server": "github-write"}},
            nodes=[{"id": "x", "action": "a", "description": "X.", "uses": ["forge"]}],
        )
    )
    assert [t.name for t in _workflow_for(mcp).tools()] == ["gh"]
    local_only = parse_catalog_workflow(
        _package(
            integrations={"git": {"kind": "transport"}},
            nodes=[{"id": "x", "action": "a", "description": "X.", "uses": ["git"]}],
        )
    )
    assert _workflow_for(local_only).tools() == ()
    assert _workflow_for(local_only).skills() == ()  # and no forge skills without forge actions
