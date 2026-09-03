"""Outfitter catalog workflows — community-profiles packages as Panopticon lifecycles.

Outfitter publishes organization workflows as typed graphs (``workflows/<id>/workflow.yaml``, the
``ai-outfitter.com/schemas/workflow.schema.json`` contract): human, agent, tool, and system
**actors**; **environments**; **integrations**; and a DAG of **nodes** that each perform an
``action`` or delegate to a nested ``workflow``. Outfitter validates and distributes these
packages; it never schedules or executes them. Panopticon *is* an execution engine for exactly
this shape of lifecycle, so this module projects **every package the operator's ``.agents``
root carries** onto the workflow interface — no per-package Python class, and no code change
to adopt a new package.

The packages are **resolved through the operator's Outfitter ``.agents`` root**
(``$PANOPTICON_AGENTS``, default ``~/.agents``) at workflow instantiation, the same layered
graph Outfitter resolves: the root's own ``workflows/<id>/workflow.yaml`` first, then each
``sources`` entry of ``settings.yml`` (``settings.local.yml`` replaces the list wholesale) in
listed order, a remote source living at the checkout Outfitter caches under
``cache/repos/<base64url(uri#ref)>``. The pin is therefore the source ref in the ``.agents``
settings, and upgrading the catalog is an ``.agents`` change, not a Panopticon release. Nothing is vendored into this repository, and a host whose
root provides no packages registers no Outfitter workflows. A package that is present but
off-contract, or whose nodes do not form the single chain Panopticon projects, is skipped with
a diagnostic — one broken or fan-out package never blocks the rest of the catalog or service
startup. A nested reference (``adversarial-review`` in the canonical packages) must resolve by
slug from the same directory, as Outfitter requires.

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

import base64
import logging
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml

from panopticon.core.models import Actor, Responsibility, Skill, Tool
from panopticon.core.state import BaseState, Complete, InitialState, State
from panopticon.core.workflow import InvalidWorkflow, WorkflowUnavailable
from panopticon.workflows.github_forge import GithubForgeWorkflow

#: Environment override for the Outfitter ``.agents`` root the packages are resolved from.
AGENTS_ENV = "PANOPTICON_AGENTS"

#: Outfitter's ``id`` pattern for workflows and nodes (``workflow.schema.json``).
_SLUG = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_ACTOR_KINDS = frozenset({"human", "agent", "tool", "system"})

_log = logging.getLogger(__name__)

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


class CatalogUnavailable(InvalidCatalogWorkflow, WorkflowUnavailable):
    """Raised when the ``.agents`` root provides no such package — discovery skips, not fails."""


def agents_root() -> Path:
    """The Outfitter ``.agents`` root: ``$PANOPTICON_AGENTS`` → ``~/.agents``."""
    override = os.environ.get(AGENTS_ENV)
    return Path(override) if override else Path.home() / ".agents"


def _configured_sources(root: Path) -> tuple[Mapping[str, Any], ...]:
    """The ``sources`` list in effect: ``settings.local.yml``'s replaces ``settings.yml``'s wholesale."""
    for name in ("settings.local.yml", "settings.yml"):
        path = root / name
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as error:
            raise InvalidCatalogWorkflow(f"{path}: unreadable settings: {error}") from error
        if not isinstance(document, Mapping) or "sources" not in document:
            continue
        sources = document["sources"] or []
        if not isinstance(sources, list) or not all(isinstance(item, Mapping) for item in sources):
            raise InvalidCatalogWorkflow(f"{path}: `sources` must be a list of mappings")
        return tuple(cast("Mapping[str, Any]", item) for item in sources)
    return ()


def _source_checkout(root: Path, source: Mapping[str, Any]) -> Path:
    """Where one ``sources`` entry's payload lives on disk (Outfitter's checkout-cache layout).

    A remote source (``github``/``uri`` + optional ``ref``) is cached by Outfitter under
    ``<root>/cache/repos/`` keyed by the unpadded URL-safe base64 of ``<uri>#<ref>``, a
    ``github`` shorthand normalizing to ``git+https://github.com/<owner>/<repo>.git``. A local
    source's ``path`` (absolute, or relative to the root) is the payload itself. An optional
    ``path`` on a remote source selects a subdirectory of the checkout.
    """
    subpath = source.get("path")
    uri = source.get("uri")
    github = source.get("github")
    if uri is None and github is None:  # a local source: `path` is the payload
        if not isinstance(subpath, str) or not subpath:
            raise InvalidCatalogWorkflow(f"agents source {source!r}: a local source needs a `path`")
        local = Path(subpath)
        return local if local.is_absolute() else root / local
    target = uri if isinstance(uri, str) else f"git+https://github.com/{github}.git"
    ref = source.get("ref") or ""
    key = base64.urlsafe_b64encode(f"{target}#{ref}".encode()).decode().rstrip("=")
    checkout = root / "cache" / "repos" / key
    return checkout / subpath if isinstance(subpath, str) and subpath else checkout


def catalog_layers(root: Path) -> tuple[Path, ...]:
    """Every directory that may provide ``workflows/<id>/``, highest precedence first."""
    return (root, *(_source_checkout(root, source) for source in _configured_sources(root)))


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


def _package_path(workflow_id: str, root: Path) -> Path:
    """The first layer providing ``workflows/<id>/workflow.yaml`` wins; none is unavailability."""
    layers = catalog_layers(root)
    for layer in layers:
        path = layer / "workflows" / workflow_id / "workflow.yaml"
        if path.is_file():
            return path
    raise CatalogUnavailable(
        f"catalog package {workflow_id!r} is not provided by the agents root {root} "
        f"(searched its workflows/ directory and {len(layers) - 1} configured source(s))"
    )


def load_catalog_workflow(workflow_id: str, *, root: Path | None = None) -> CatalogWorkflow:
    """Resolve, read, parse, and validate one package from the ``.agents`` root (and its nesting).

    ``root`` defaults to :func:`agents_root`. Resolution follows Outfitter's own precedence:
    the root's ``workflows/`` directory, then each ``settings.yml`` source in listed order at
    its cached checkout. A package no layer provides raises
    :class:`CatalogUnavailable` (discovery skips the workflow); one that is present but
    off-contract raises :class:`InvalidCatalogWorkflow` — a hard error, because a broken
    catalog should be fixed, not silently ignored.
    """
    base = root if root is not None else agents_root()
    path = _package_path(workflow_id, base)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise InvalidCatalogWorkflow(
            f"catalog package {workflow_id!r} is not readable at {path}: {error}"
        ) from error
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise InvalidCatalogWorkflow(f"{path}: not valid YAML: {error}") from error
    workflow = parse_catalog_workflow(document, source=str(path))
    if workflow.id != workflow_id:
        raise InvalidCatalogWorkflow(
            f"{path}: package id {workflow.id!r} does not match its directory {workflow_id!r}"
        )
    return _resolve_nested(workflow, root=base, stack=(workflow_id,))


def _resolve_nested(
    workflow: CatalogWorkflow, *, root: Path, stack: tuple[str, ...]
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
        nested = load_catalog_workflow(node.workflow, root=root)
        _resolve_nested(nested, root=root, stack=(*stack, nested.id))
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

    Not registrable by class scanning (no ``name``/``catalog_id`` on the base): discovery calls
    this module's :func:`workflow_provider`, which subclasses it per package found under the
    ``.agents`` root — the registry is built at service start, so that is when the catalog is
    read. The package is (re)read from the root at instantiation; instantiating when the root
    no longer provides it raises :class:`CatalogUnavailable`, which discovery treats as "skip
    this workflow", never a startup failure. Everything else — the forge plumbing, the plan convention it inherits
    (unused: these packages plan outside the task), ``opt_in`` — is ordinary workflow code.
    """

    #: The catalog package (``workflows/<catalog_id>/workflow.yaml``) this workflow projects.
    catalog_id: ClassVar[str]
    #: The validated package, loaded at instantiation.
    catalog: ClassVar[CatalogWorkflow]
    #: State-class attribute names from the previous materialization (replaced by the next).
    _projected: ClassVar[tuple[str, ...]] = ()

    opt_in: ClassVar[bool] = True

    def __init__(self) -> None:
        type(self)._materialize()
        super().__init__()

    @classmethod
    def _materialize(cls) -> None:
        """(Re)load the package and attach its projected states to the class.

        Runs on every instantiation: the load is a handful of small YAML files, and
        re-projecting keeps the class honest when the agents root differs between
        instantiations (tests; an operator repointing ``$PANOPTICON_AGENTS``).
        """
        catalog = load_catalog_workflow(cls.catalog_id)
        states = project_states(catalog)
        for stale in cls._projected:
            if stale in cls.__dict__:
                delattr(cls, stale)
        for state in states:
            setattr(cls, state.__name__, state)
        cls.catalog = catalog
        cls.initial = states[0]
        cls._projected = tuple(state.__name__ for state in states)
        cls.when_to_use = f"{catalog.title} (Outfitter catalog): {catalog.description}"

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


def catalog_workflow(workflow_id: str) -> OutfitterCatalogWorkflow:
    """One package projected as a registrable workflow named ``outfitter-<id>``.

    Builds (and instantiates, which loads and validates) a dedicated
    :class:`OutfitterCatalogWorkflow` subclass for the package. Raises
    :class:`InvalidCatalogWorkflow` — :class:`CatalogUnavailable` when the root does not
    provide the package — exactly like :func:`load_catalog_workflow`.
    """
    cls = type(
        f"Outfitter_{re.sub(r'[.-]', '_', workflow_id)}",
        (OutfitterCatalogWorkflow,),
        {
            "name": f"outfitter-{workflow_id}",
            "catalog_id": workflow_id,
            "__module__": __name__,
            "__qualname__": f"catalog_workflow.{workflow_id}",
        },
    )
    return cast(OutfitterCatalogWorkflow, cls())


def workflow_provider() -> Iterator[OutfitterCatalogWorkflow]:
    """Every package the ``.agents`` root provides, projected — discovery's catalog hook.

    Enumerates ``workflows/*/workflow.yaml`` across the root and every ``settings.yml`` source
    checkout (per layer sorted, first layer to name an id claims it) and yields one workflow
    per package that loads, validates, and projects. A package that
    fails — off-contract, fan-in/fan-out nodes, an unresolvable nested reference — is skipped
    with a diagnostic so the rest of the catalog still registers; a missing or empty root
    yields nothing.
    """
    root = agents_root()
    try:
        layers = catalog_layers(root)
    except InvalidCatalogWorkflow as error:
        _log.warning("outfitter catalog disabled: %s", error)
        return
    seen: set[str] = set()
    for layer in layers:
        workflows_dir = layer / "workflows"
        if not workflows_dir.is_dir():
            continue
        for package_dir in sorted(workflows_dir.iterdir()):
            if package_dir.name in seen or not (package_dir / "workflow.yaml").is_file():
                continue
            seen.add(package_dir.name)
            try:
                yield catalog_workflow(package_dir.name)
            except InvalidCatalogWorkflow as error:
                _log.warning("skipping catalog package %r: %s", package_dir.name, error)
