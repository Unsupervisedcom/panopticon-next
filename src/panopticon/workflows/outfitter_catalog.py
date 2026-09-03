"""Outfitter catalog workflows — community-profiles packages as Panopticon lifecycles.

Outfitter publishes organization workflows as typed graphs (``workflows/<id>/workflow.yaml``, the
``ai-outfitter.com/schemas/workflow.schema.json`` contract): human, agent, tool, and system
**actors**; **environments**; **integrations**; and a DAG of **nodes** that each perform an
``action`` or delegate to a nested ``workflow``. Outfitter validates and distributes these
packages; it never schedules or executes them. Panopticon *is* an execution engine for exactly
this shape of lifecycle, so this module projects the three canonical implementation-plan
packages — ``founder``, ``engineer``, and ``software-factory`` — onto the workflow interface.

The packages are **vendored byte-for-byte** under ``workflows/outfitter/`` from the pinned
community-profiles release (:data:`CATALOG_RELEASE`) and hash-pinned in :data:`CATALOG_SHA256`,
so a drifted copy is rejected at load time and an upgrade is an explicit, reviewable change to
both the file and its digest. The ``adversarial-review`` package is vendored too because the
three workflows nest it: a nested reference must resolve by slug, as Outfitter requires.

**Workflows are code (ADR 0004).** The catalog YAML supplies the *declarative* half — the node
chain, descriptions, actors, environments, and integrations — while the Python classes below
own everything Outfitter deliberately leaves out: the projection rules, the gate policy, the
skills the agent runs, and the tools it reaches for. The projection is deterministic and
LLM-free:

- Each node becomes one state, in dependency order. Outfitter allows any DAG; Panopticon's
  happy path is a line, so a package is accepted only when its nodes form a single chain.
- A node that performs an ``action`` is agent work: the agent enters on its own turn, records
  the node's description as its one responsibility, and advances itself once it is met.
- A node that delegates to a nested ``workflow`` (``adversarial-review`` in all three) is the
  operator's sign-off gate: it is user-advanced, and its label is the node id — ``REVIEW`` — so
  a repo that declares a reviewer launch pair gets the governed cross-model review task from
  ADR 0014 / REQ-013 on entry. The built-ins here leave the pair unset (REQ-002.27).
- A node whose actor is a ``human`` is the human's work: user turn on entry, user-advanced,
  and no agent responsibilities.
- A node whose actor is a ``system`` (the platform merging after every gate) is observed by
  the agent, which advances once the platform has acted.

Skills are the existing GitHub-forge procedures, selected by the actions a package actually
performs (``open-draft-pr`` → ``open-pr``, ``wait-for-required-ci`` → ``babysit-ci``, the merge
actions → ``babysit-merge``) plus a small ``push-branch`` skill for the founder's
``push-as-human``. A task's harness is the operator's choice as usual; under the ``outfitter``
harness the task's starting model is the Outfitter **profile id**, so the actor profiles the
package names (``founder``, ``engineer``, ``resident-engineer``) are surfaced in each state's
description for the operator to pick.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml

from panopticon.core.models import Actor, Responsibility, Skill, Tool
from panopticon.core.state import BaseState, Complete, InitialState, State
from panopticon.core.workflow import InvalidWorkflow
from panopticon.workflows.github_forge import GithubForgeWorkflow

#: Where the vendored packages live: ``outfitter/<id>/workflow.yaml`` beside this module.
CATALOG_DIR: Path = Path(__file__).with_name("outfitter")
#: The catalog the packages are vendored from, and the exact release they are pinned to.
CATALOG_REPOSITORY = "ai-outfitter/community-profiles"
CATALOG_RELEASE = "v1.6.0"
CATALOG_COMMIT = "dd09281c986332c1ce7b40289b2e3c9c3b5c3b88"
#: SHA-256 of each vendored ``workflow.yaml``. Loading a package whose digest differs fails:
#: the copy must match the pinned release, and an upgrade edits this table alongside the file.
CATALOG_SHA256: Mapping[str, str] = {
    "adversarial-review": "c0e17842b0a90eb66a75db495c247efa6bbba0325572dbc550b40fae816fe130",
    "engineer": "087cd7d0792cc53a734a4f5e1d9b662c8824d7e18f4bb39b9ef8ec166ffd32e9",
    "founder": "5545c5e2c7437141de6f3ffea489db65992ea9cff440b5278e407eb172922056",
    "software-factory": "b5e2b7aeb76e0c17a50e5930128744fa6642cf2bcf004f97fd8c7c6ada5c1189",
}

#: Outfitter's ``id`` pattern for workflows and nodes (``workflow.schema.json``).
_SLUG = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_ACTOR_KINDS = frozenset({"human", "agent", "tool", "system"})

#: Which forge skills (from :class:`GithubForgeWorkflow`) a catalog action is carried out with.
_ACTION_SKILLS: Mapping[str, tuple[str, ...]] = {
    "open-draft-pr": ("open-pr", "babysit-ci"),  # a draft PR implies keeping its CI green
    "wait-for-required-ci": ("babysit-ci",),
    "merge-as-human": ("babysit-merge",),
    "merge-after-approval": ("babysit-merge",),
    "push-as-human": ("push-branch",),
}

_PUSH_BRANCH_SKILL = Skill(
    "push-branch",
    "Publish the reviewed local commits by pushing the task branch.",
    "1. Confirm the review verdict was addressed: every must-fix finding in the governor's "
    "`review.md` artifact (if one exists) is resolved in a local commit.\n"
    "2. Run `git -C /workspace status --porcelain` and stop if the working tree is dirty — "
    "commit or discard first; nothing unreviewed may ride along.\n"
    "3. Push the task branch: `git -C /workspace push --set-upstream origin "
    "$(git -C /workspace branch --show-current)`.\n"
    "4. Record the branch's forge URL with the `set_url` MCP tool "
    "(`gh browse --no-browser --branch <branch>` prints it), then advance.",
)


class InvalidCatalogWorkflow(InvalidWorkflow):
    """Raised when a catalog package is unreadable, off-contract, or not projectable."""


@dataclass(frozen=True)
class CatalogActor:
    """One ``actors`` entry: who performs a node, and (for agents) which profile runs it."""

    id: str
    kind: str
    profile: str | None = None


@dataclass(frozen=True)
class CatalogIntegration:
    """One ``integrations`` entry — a CLI, transport, MCP server, or pinned artifact."""

    id: str
    kind: str | None = None
    server: str | None = None


@dataclass(frozen=True)
class CatalogNode:
    """One ``nodes`` entry: exactly one of ``action`` / ``workflow``."""

    id: str
    description: str
    action: str | None
    workflow: str | None
    actor: str | None
    environment: str | None
    needs: tuple[str, ...]
    uses: tuple[str, ...]


@dataclass(frozen=True)
class CatalogWorkflow:
    """A validated Outfitter workflow package whose nodes form a single chain."""

    id: str
    title: str
    description: str
    actors: Mapping[str, CatalogActor]
    environments: Mapping[str, str]
    integrations: Mapping[str, CatalogIntegration]
    nodes: tuple[CatalogNode, ...]  # in dependency order: each node needs only its predecessor


def _require_str(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCatalogWorkflow(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _require_slug(value: object, *, where: str) -> str:
    text = _require_str(value, where=where)
    if not _SLUG.match(text):
        raise InvalidCatalogWorkflow(f"{where}: {text!r} is not a valid Outfitter id")
    return text


def _require_mapping(value: object, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidCatalogWorkflow(f"{where}: expected a mapping, got {type(value).__name__}")
    return cast(Mapping[str, Any], value)


def _optional_str(value: object, *, where: str) -> str | None:
    return None if value is None else _require_str(value, where=where)


def _str_list(value: object, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise InvalidCatalogWorkflow(f"{where}: expected a list, got {type(value).__name__}")
    items = tuple(_require_str(item, where=where) for item in value)
    if len(set(items)) != len(items):
        raise InvalidCatalogWorkflow(f"{where}: entries must be unique")
    return items


def parse_catalog_workflow(document: object, *, source: str = "workflow.yaml") -> CatalogWorkflow:
    """Validate a parsed ``workflow.yaml`` against Outfitter's contract and Panopticon's chain rule.

    Contract checks mirror ``workflow.schema.json`` plus the resolver's referential rules: ids,
    actor kinds (an agent needs a profile), exactly one of ``action``/``workflow`` per node, and
    every ``actor``/``environment``/``needs``/``uses`` reference resolving within the package.
    Panopticon then requires the nodes to form one chain (a single root, each node needing at
    most its predecessor, no fan-out): the happy path of a workflow is a line of states.
    """
    doc = _require_mapping(document, where=source)
    if doc.get("version") != 1:
        raise InvalidCatalogWorkflow(
            f"{source}: unsupported workflow version {doc.get('version')!r}"
        )
    workflow_id = _require_slug(doc.get("id"), where=f"{source}: id")
    title = _require_str(doc.get("title"), where=f"{source}: title")
    description = _require_str(doc.get("description"), where=f"{source}: description")

    actors: dict[str, CatalogActor] = {}
    for actor_id, raw in _require_mapping(doc.get("actors"), where=f"{source}: actors").items():
        spec = _require_mapping(raw, where=f"{source}: actors.{actor_id}")
        kind = _require_str(spec.get("kind"), where=f"{source}: actors.{actor_id}.kind")
        if kind not in _ACTOR_KINDS:
            raise InvalidCatalogWorkflow(f"{source}: actors.{actor_id}: unknown kind {kind!r}")
        profile = _optional_str(spec.get("profile"), where=f"{source}: actors.{actor_id}.profile")
        if kind == "agent" and profile is None:
            raise InvalidCatalogWorkflow(f"{source}: actors.{actor_id}: an agent needs a profile")
        actors[actor_id] = CatalogActor(actor_id, kind, profile)

    environments: dict[str, str] = {}
    if doc.get("environments") is not None:
        env_map = _require_mapping(doc["environments"], where=f"{source}: environments")
        for env_id, runtime in env_map.items():
            environments[env_id] = _require_str(runtime, where=f"{source}: environments.{env_id}")

    integrations: dict[str, CatalogIntegration] = {}
    if doc.get("integrations") is not None:
        int_map = _require_mapping(doc["integrations"], where=f"{source}: integrations")
        for int_id, raw in int_map.items():
            spec = _require_mapping(raw, where=f"{source}: integrations.{int_id}")
            integrations[int_id] = CatalogIntegration(
                int_id,
                _optional_str(spec.get("kind"), where=f"{source}: integrations.{int_id}.kind"),
                _optional_str(spec.get("server"), where=f"{source}: integrations.{int_id}.server"),
            )

    raw_nodes = doc.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise InvalidCatalogWorkflow(f"{source}: nodes must be a non-empty list")
    nodes: dict[str, CatalogNode] = {}
    for index, raw in enumerate(raw_nodes):
        where = f"{source}: nodes[{index}]"
        spec = _require_mapping(raw, where=where)
        node_id = _require_slug(spec.get("id"), where=f"{where}.id")
        if node_id in nodes:
            raise InvalidCatalogWorkflow(f"{where}: duplicate node id {node_id!r}")
        action = _optional_str(spec.get("action"), where=f"{where}.action")
        nested = _optional_str(spec.get("workflow"), where=f"{where}.workflow")
        if (action is None) == (nested is None):
            raise InvalidCatalogWorkflow(
                f"{where}: node {node_id!r} must have exactly one of action / workflow"
            )
        actor = _optional_str(spec.get("actor"), where=f"{where}.actor")
        if actor is not None and actor not in actors:
            raise InvalidCatalogWorkflow(f"{where}: node {node_id!r} names unknown actor {actor!r}")
        environment = _optional_str(spec.get("environment"), where=f"{where}.environment")
        if environment is not None and environment not in environments:
            raise InvalidCatalogWorkflow(
                f"{where}: node {node_id!r} names unknown environment {environment!r}"
            )
        uses = _str_list(spec.get("uses"), where=f"{where}.uses")
        for integration in uses:
            if integration not in integrations:
                raise InvalidCatalogWorkflow(
                    f"{where}: node {node_id!r} uses unknown integration {integration!r}"
                )
        nodes[node_id] = CatalogNode(
            id=node_id,
            description=_require_str(spec.get("description"), where=f"{where}.description"),
            action=action,
            workflow=nested,
            actor=actor,
            environment=environment,
            needs=_str_list(spec.get("needs"), where=f"{where}.needs"),
            uses=uses,
        )
    for node in nodes.values():
        for dep in node.needs:
            if dep == node.id or dep not in nodes:
                raise InvalidCatalogWorkflow(
                    f"{source}: node {node.id!r} needs unknown or self node {dep!r}"
                )

    return CatalogWorkflow(
        id=workflow_id,
        title=title,
        description=description,
        actors=actors,
        environments=environments,
        integrations=integrations,
        nodes=_chain(nodes, source=source),
    )


def _chain(nodes: Mapping[str, CatalogNode], *, source: str) -> tuple[CatalogNode, ...]:
    """Order the nodes as the single chain Panopticon projects, or reject the graph."""
    roots = [node for node in nodes.values() if not node.needs]
    if len(roots) != 1:
        raise InvalidCatalogWorkflow(
            f"{source}: expected exactly one root node (no `needs`), found {len(roots)}"
        )
    dependents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node in nodes.values():
        if len(node.needs) > 1:
            raise InvalidCatalogWorkflow(
                f"{source}: node {node.id!r} needs {len(node.needs)} nodes; a Panopticon lifecycle "
                "is a single chain (one predecessor per node)"
            )
        for dep in node.needs:
            dependents[dep].append(node.id)
    for node_id, children in dependents.items():
        if len(children) > 1:
            raise InvalidCatalogWorkflow(
                f"{source}: node {node_id!r} fans out to {sorted(children)}; a Panopticon lifecycle "
                "is a single chain (one successor per node)"
            )
    ordered = [roots[0]]
    while dependents[ordered[-1].id]:
        ordered.append(nodes[dependents[ordered[-1].id][0]])
    if len(ordered) != len(nodes):
        unreached = sorted(set(nodes) - {node.id for node in ordered})
        raise InvalidCatalogWorkflow(
            f"{source}: nodes {unreached} are not reachable from the root (a cycle or a second chain)"
        )
    return tuple(ordered)


def _package_path(workflow_id: str) -> Path:
    return CATALOG_DIR / workflow_id / "workflow.yaml"


def load_catalog_workflow(workflow_id: str, *, catalog_dir: Path | None = None) -> CatalogWorkflow:
    """Read, hash-check, parse, and validate one vendored package (and resolve its nesting).

    ``catalog_dir`` overrides :data:`CATALOG_DIR` (tests). The digest check applies to the
    vendored catalog only; an override directory is trusted as given.
    """
    path = (catalog_dir or CATALOG_DIR) / workflow_id / "workflow.yaml"
    try:
        content = path.read_bytes()
    except OSError as error:
        raise InvalidCatalogWorkflow(
            f"catalog package {workflow_id!r} is not readable at {path}: {error}"
        ) from error
    if catalog_dir is None:
        expected = CATALOG_SHA256.get(workflow_id)
        actual = hashlib.sha256(content).hexdigest()
        if expected is None or actual != expected:
            raise InvalidCatalogWorkflow(
                f"catalog package {workflow_id!r} at {path} does not match the pinned "
                f"{CATALOG_REPOSITORY} {CATALOG_RELEASE} digest (expected {expected}, got {actual})"
            )
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise InvalidCatalogWorkflow(f"{path}: not valid YAML: {error}") from error
    workflow = parse_catalog_workflow(document, source=str(path))
    if workflow.id != workflow_id:
        raise InvalidCatalogWorkflow(
            f"{path}: package id {workflow.id!r} does not match its directory {workflow_id!r}"
        )
    return _resolve_nested(workflow, catalog_dir=catalog_dir, stack=(workflow_id,))


def _resolve_nested(
    workflow: CatalogWorkflow, *, catalog_dir: Path | None, stack: tuple[str, ...]
) -> CatalogWorkflow:
    """Every ``workflow:`` reference must resolve to a valid package, without cycles."""
    for node in workflow.nodes:
        if node.workflow is None:
            continue
        if node.workflow in stack:
            raise InvalidCatalogWorkflow(
                f"catalog package {workflow.id!r}: nested workflow {node.workflow!r} forms a cycle "
                f"({' -> '.join((*stack, node.workflow))})"
            )
        nested = load_catalog_workflow(node.workflow, catalog_dir=catalog_dir)
        _resolve_nested(nested, catalog_dir=catalog_dir, stack=(*stack, nested.id))
    return workflow


def state_label(node_id: str) -> str:
    """The dashboard label for a node: ``wait-for.ci`` → ``WAIT_FOR_CI``."""
    return re.sub(r"[._-]", "_", node_id).upper()


def _describe(workflow: CatalogWorkflow, node: CatalogNode) -> str:
    """The state's briefing prose: the node's own description, then who/where/with-what."""
    parts = [node.description]
    actor = workflow.actors.get(node.actor) if node.actor else None
    if actor is not None:
        who = f"Actor: `{actor.id}` ({actor.kind}"
        if actor.profile:
            who += f", Outfitter profile `{actor.profile}`"
        parts.append(who + ").")
    if node.environment:
        parts.append(
            f"Environment: `{node.environment}` ({workflow.environments[node.environment]})."
        )
    if node.workflow:
        parts.append(f"Delegates to the `{node.workflow}` workflow.")
    if node.uses:
        parts.append("Uses: " + ", ".join(f"`{name}`" for name in node.uses) + ".")
    return " ".join(parts)


def project_states(workflow: CatalogWorkflow) -> tuple[type[BaseState], ...]:
    """Project a validated chain onto nested-state classes, in order (the first is initial).

    The gate policy is the module's, not the catalog's: action nodes are agent work (agent
    turn on entry, one responsibility, agent-advanced); nested-workflow nodes and human-actor
    nodes are user gates; system-actor nodes are agent-observed. The last state advances to
    ``COMPLETE``; ``DROPPED`` is inherited by every state.
    """
    states: list[type[BaseState]] = []
    for index, node in enumerate(workflow.nodes):
        actor = workflow.actors.get(node.actor) if node.actor else None
        base: type[State] = InitialState if index == 0 else State
        following = workflow.nodes[index + 1] if index + 1 < len(workflow.nodes) else None
        attrs: dict[str, Any] = {
            "label": state_label(node.id),
            "description": _describe(workflow, node),
            "transitions": (state_label(following.id),) if following else (Complete,),
            "__module__": __name__,
            "__qualname__": f"{workflow.id}.{node.id}",
        }
        if actor is not None and actor.kind == "human":
            attrs |= {"turn_on_enter": Actor.USER, "advanced_by": Actor.USER}
        elif node.workflow is not None:
            attrs["advanced_by"] = Actor.USER
            attrs["responsibilities"] = (Responsibility(node.workflow, node.description),)
        else:
            attrs["advanced_by"] = Actor.AGENT
            if index == 0:  # InitialState waits for the user's first instruction; keep that
                attrs["turn_on_enter"] = Actor.USER
            attrs["responsibilities"] = (Responsibility(node.action or node.id, node.description),)
        states.append(cast(type[BaseState], type(node.id.replace("-", "_"), (base,), attrs)))
    return tuple(states)


class OutfitterCatalogWorkflow(GithubForgeWorkflow):
    """Abstract base: a concrete subclass names its ``catalog_id`` and gets its states projected.

    Not registrable on its own (no ``name``). Subclass creation loads the vendored package,
    projects the chain onto nested state classes, attaches them to the class (so the workflow
    interface's nested-state discovery finds them), and sets ``initial``. Everything else — the
    forge plumbing, the plan convention it inherits (unused: these packages plan outside the
    task), ``opt_in`` — is ordinary workflow code.
    """

    #: The vendored package (``outfitter/<catalog_id>/workflow.yaml``) this workflow projects.
    catalog_id: ClassVar[str]
    #: The validated package, loaded at class creation.
    catalog: ClassVar[CatalogWorkflow]

    opt_in: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "catalog_id" not in cls.__dict__:
            return
        cls.catalog = load_catalog_workflow(cls.catalog_id)
        states = project_states(cls.catalog)
        for state in states:
            setattr(cls, state.__name__, state)
        cls.initial = states[0]
        cls.name = f"outfitter-{cls.catalog.id}"
        cls.when_to_use = (
            f"{cls.catalog.title} ({CATALOG_REPOSITORY} {CATALOG_RELEASE}): "
            f"{cls.catalog.description}"
        )

    def _actions(self) -> frozenset[str]:
        return frozenset(node.action for node in self.catalog.nodes if node.action)

    def skills(self) -> Sequence[Skill]:
        """The forge skills the package's actions call for, in the order the actions occur."""
        available = {skill.name: skill for skill in super().skills()}
        available[_PUSH_BRANCH_SKILL.name] = _PUSH_BRANCH_SKILL
        chosen: list[Skill] = []
        for node in self.catalog.nodes:
            for skill_name in _ACTION_SKILLS.get(node.action or "", ()):
                if available[skill_name] not in chosen:
                    chosen.append(available[skill_name])
        return tuple(chosen)

    def tools(self) -> Sequence[Tool]:
        """``gh`` whenever the package reaches GitHub — as a CLI or through the GitHub MCP server."""
        github = any(
            integration.id == "gh"
            or (integration.server is not None and "github" in integration.server)
            for integration in self.catalog.integrations.values()
        )
        return super().tools() if github else ()


class OutfitterFounder(OutfitterCatalogWorkflow):
    """``founder``: implement and verify locally, commit, get an independent review, push as the human."""

    catalog_id: ClassVar[str] = "founder"


class OutfitterEngineer(OutfitterCatalogWorkflow):
    """``engineer``: research, implement, open a draft PR as the human, get reviewed, merge as the human."""

    catalog_id: ClassVar[str] = "engineer"


class OutfitterSoftwareFactory(OutfitterCatalogWorkflow):
    """``software-factory``: a resident engineer takes a typed issue through CI-gated review to a
    platform-performed merge."""

    catalog_id: ClassVar[str] = "software-factory"
