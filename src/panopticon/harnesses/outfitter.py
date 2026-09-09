"""The Outfitter harness — ``@ai-outfitter/outfitter`` wrapping pi.

Verified from Outfitter's published docs and TypeScript source: it requires Node
``>=22.19.0``, agent CLIs are installed separately, and ``outfitter run <id>
--harness pi -- <args>`` passes the remaining arguments to pi. Outfitter agents own provider,
model, thinking, skills, extensions, and prompts, so Panopticon deliberately interprets a task's
``starting_model`` as the Outfitter **agent slug**, not as a model name.

Panopticon's additions ride through Outfitter's documented pass-through: the workflow overview
via pi's ``--append-system-prompt``, :data:`panopticon.harnesses.pi.TURN_EXTENSION` via
``--extension``, and rendered workflow skills via repeated ``--skill``. Core operations retain
pi's REST instructions because neither pi nor Outfitter provides an MCP client.

Bootstrap keeps ``~/.outfitter/profile_sources`` as the first local catalog source and writes the
modern settings file at ``~/.agents/settings.yml``. An operator can also point the repo's
``credential_dir/outfitter/.agents`` at a catalog payload. Its resource tree is materialized into
the container's global ``~/.agents`` layer, so project-level source settings cannot remove it;
normal Outfitter workspace-over-global overrides still apply.

Auth is pi auth, not Outfitter auth. Presence checking uses pi's provider environment variables,
while credential-dir linking targets Outfitter's native pi-state fallback; provider validity
remains pi's concern.

Resume uses Outfitter's documented Pi state fallback at ``~/.pi/agent/sessions``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Final

from panopticon.core.models import Skill
from panopticon.harnesses.base import INTERRUPT_PROMPT, BootstrapContext, Harness, LaunchContext
from panopticon.harnesses.codex import write_skills
from panopticon.harnesses.pi import (
    API_KEY_ENV_VARS,
    AUTH_FILE,
    NODE_VERSION,
    PI_VERSION,
    TURN_EXTENSION,
    operation_instructions,
    responsibility_instructions,
)

OUTFITTER_VERSION = "1.16.0"
SETTINGS_FILE = "settings.yml"
PROFILE_SOURCES_DIR = "profile_sources"
WORKFLOW_OVERVIEW_FILE = "workflow-overview.md"
EXTENSION_FILE = "turn.ts"
INJECTED_SKILLS_ROOT = Path(".agents") / "skills"

SETTINGS = "default_harness: pi\nsources:\n  - path: ../.outfitter/profile_sources\n"
PI_NATIVE_CONFIG_DIR = Path(".pi") / "agent"
PROFILE_LABEL_WIDTH: Final = 80


def _top_level_scalar(text: str, key: str) -> str | bool | None:
    """Read the small scalar metadata subset used by profile discovery."""
    match = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*)$", text)
    if match is None:
        return None
    value = match.group(1).strip()
    if not value or value.startswith(("|", ">")):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    return value or None


class OutfitterHarness(Harness):
    """Outfitter's agent composer/launcher, fixed to its primary pi adapter."""

    name: ClassVar[str] = "outfitter"
    config_dirname: ClassVar[str] = ".outfitter"
    host_binary: ClassVar[str] = "outfitter"
    install_hint: ClassVar[str] = (
        "Install Outfitter (`npm install --global @ai-outfitter/outfitter`)."
    )
    field_label: ClassVar[str] = "agent"
    requires_starting_model: ClassVar[bool] = True

    def __init__(self, profile_sources_root: Path | None = None) -> None:
        self.profile_sources_root = profile_sources_root

    def suggested_models(self) -> tuple[tuple[str, str], ...]:
        """Discover profiles to suggest, from the operator's native Outfitter install.

        The dashboard calls this host-side, so the default root is where a real
        Outfitter setup resolves its layered agent catalog — not the
        container's ``profile_sources`` mount, which the operator's host doesn't have.
        """
        roots: tuple[Path, ...]
        if self.profile_sources_root is not None:
            roots = (self.profile_sources_root,)
        else:
            configured_root = os.environ.get("PANOPTICON_AGENTS")
            catalog_root = Path(configured_root) if configured_root else Path.home() / ".agents"
            try:
                # Import lazily: the workflow catalog imports the harness registry while modules
                # are discovered, but picker rendering happens only after discovery completes.
                from panopticon.workflows.outfitter_catalog import (
                    InvalidCatalogWorkflow,
                    catalog_layers,
                )

                roots = tuple(layer / "agents" for layer in catalog_layers(catalog_root))
            except (InvalidCatalogWorkflow, OSError):
                return ()

        agents: dict[str, str] = {}
        for root in roots:
            try:
                paths = sorted(path / "agent.md" for path in root.iterdir() if path.is_dir())
            except OSError:
                continue
            for path in paths:
                try:
                    text = path.read_text()
                except (OSError, UnicodeError):
                    continue
                agent_slug = path.parent.name
                if agent_slug in agents or _top_level_scalar(text, "abstract") is True:
                    continue
                description = _top_level_scalar(text, "description")
                label = agent_slug
                if isinstance(description, str):
                    summary = " ".join(description.split())
                    label = textwrap.shorten(
                        f"{agent_slug} — {summary}", width=PROFILE_LABEL_WIDTH, placeholder="…"
                    )
                agents[agent_slug] = label
        return tuple(sorted(agents.items()))

    def image_layer(self) -> str:
        """Install pinned Node, pi, and Outfitter releases.

        The versions and separate pi install are verified from Outfitter 0.10.0's package
        manifest and installation docs; the resulting container launch is not yet smoke-tested.
        """
        return (
            "RUN set -eux; \\\n"
            '    arch="$(uname -m)"; \\\n'
            '    case "$arch" in \\\n'
            '      x86_64) node_arch="x64" ;; \\\n'
            '      aarch64) node_arch="arm64" ;; \\\n'
            '      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\\n'
            "    esac; \\\n"
            "    curl --fail --silent --show-error --location \\\n"
            f'      "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.gz" \\\n'
            "      | tar --extract --gzip --directory /usr/local --strip-components=1; \\\n"
            "    npm install --global --ignore-scripts "
            f"@earendil-works/pi-coding-agent@{PI_VERSION} "
            f"@ai-outfitter/outfitter@{OUTFITTER_VERSION}"
        )

    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        """Check pi credentials at Outfitter's actual durable-state source.

        Verified from Outfitter's ``resolvePiStateSourcePath``: a selected profile's existing
        ``cli_specific/pi/auth.json`` takes precedence; otherwise the composite ``auth.json`` is
        symlinked from ``~/.pi/agent/auth.json``. The credential-dir path is accepted because
        :meth:`bootstrap` links it to that fallback before Outfitter builds the composite.
        """
        if any(environ.get(var) for var in API_KEY_ENV_VARS):
            return None
        if (home / PI_NATIVE_CONFIG_DIR / AUTH_FILE).exists():
            return None
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            return None
        return (
            "No pi credentials for Outfitter — set one of pi's provider API-key env vars "
            "(ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, … — see pi's "
            "docs/providers.md for the full list), or give the repo a credential_dir holding "
            "a pi auth.json from `/login` (see docs/auth.md)"
        )

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self.config_dir(ctx.home)
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / PROFILE_SOURCES_DIR).mkdir(exist_ok=True)
        credentials = ctx.environ.get("PANOPTICON_CREDENTIALS")
        credential_profiles = (
            Path(credentials).resolve() / "outfitter" / ".agents" if credentials else None
        )
        agents_dir = ctx.home / ".agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        if credential_profiles is not None and credential_profiles.is_dir():
            self._materialize_catalog(credential_profiles, agents_dir)
        (agents_dir / SETTINGS_FILE).write_text(SETTINGS)
        (config_dir / WORKFLOW_OVERVIEW_FILE).write_text(ctx.overview)
        (config_dir / EXTENSION_FILE).write_text(TURN_EXTENSION)

        entries = list(ctx.skills) + [
            Skill(
                name=name,
                description=f"Apply the workflow's '{name}' operation.",
                instructions=operation_instructions(
                    name,
                    target_state,
                    ctx.task_id,
                    ctx.service_url,
                    authenticated=bool(ctx.environ.get("PANOPTICON_SERVICE_AUTH_TOKEN")),
                ),
            )
            for name, target_state in ctx.operations.items()
        ]
        entries.append(
            Skill(
                name="resolve-responsibility",
                description="Record one current workflow responsibility as met or failed.",
                instructions=responsibility_instructions(
                    ctx.task_id,
                    ctx.service_url,
                    authenticated=bool(ctx.environ.get("PANOPTICON_SERVICE_AUTH_TOKEN")),
                ),
            )
        )
        write_skills(entries, config_dir, ctx.task_id)
        self._ensure_auth(ctx.home, ctx.environ)

    @staticmethod
    def _materialize_catalog(source: Path, destination: Path) -> None:
        """Flatten a trusted, already-synced catalog into one global resource layer."""
        from panopticon.workflows.outfitter_catalog import catalog_layers

        layers = catalog_layers(source, default_cache_directory=source / "cache")
        for layer in reversed(layers):
            if not layer.is_dir():
                raise ValueError(
                    "Outfitter credential catalog source is not materialized; run `outfitter "
                    "sync --strict` with its parent as HOME before starting the task"
                )
            OutfitterHarness._copy_catalog_layer(layer, destination)

    @staticmethod
    def _copy_catalog_layer(source: Path, destination: Path) -> None:
        for entry in source.iterdir():
            if entry.name in {".git", "cache", SETTINGS_FILE, "settings.local.yml"}:
                continue
            target = destination / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            elif entry.is_file():
                if entry.name == "mcp.json" and target.is_file():
                    OutfitterHarness._merge_mcp_registry(target, entry)
                elif entry.name == "models.json" and target.is_file():
                    OutfitterHarness._merge_model_registry(target, entry)
                else:
                    shutil.copy2(entry, target)

    @staticmethod
    def _json_mapping(path: Path) -> dict[str, object]:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"Outfitter catalog registry {path.name!r} must be a JSON object")
        return value

    @staticmethod
    def _merge_mcp_registry(target: Path, higher: Path) -> None:
        """Flatten Outfitter's per-server MCP precedence into one root registry."""
        lower_doc = OutfitterHarness._json_mapping(target)
        higher_doc = OutfitterHarness._json_mapping(higher)
        merged = {**lower_doc, **higher_doc}
        for key in ("settings", "mcpServers"):
            lower = lower_doc.get(key)
            upper = higher_doc.get(key)
            if isinstance(lower, dict) and isinstance(upper, dict):
                merged[key] = {**lower, **upper}
        target.write_text(json.dumps(merged, indent=2) + "\n")

    @staticmethod
    def _merge_model_registry(target: Path, higher: Path) -> None:
        """Flatten Outfitter's provider/model-id merge semantics into one registry."""
        lower_doc = OutfitterHarness._json_mapping(target)
        higher_doc = OutfitterHarness._json_mapping(higher)
        lower_providers = lower_doc.get("providers")
        higher_providers = higher_doc.get("providers")
        if not isinstance(lower_providers, dict) or not isinstance(higher_providers, dict):
            raise ValueError("Outfitter models.json must contain an object-valued `providers` map")
        providers: dict[str, object] = dict(lower_providers)
        for provider_id, higher_provider in higher_providers.items():
            lower_provider = providers.get(provider_id)
            if not isinstance(lower_provider, dict) or not isinstance(higher_provider, dict):
                providers[provider_id] = higher_provider
                continue
            merged_provider = {**lower_provider, **higher_provider}
            lower_models = lower_provider.get("models")
            higher_models = higher_provider.get("models")
            models: dict[str, object] = {
                model["id"]: model
                for model in (lower_models if isinstance(lower_models, list) else [])
                if isinstance(model, dict) and isinstance(model.get("id"), str)
            }
            for model in higher_models if isinstance(higher_models, list) else []:
                if isinstance(model, dict) and isinstance(model.get("id"), str):
                    previous = models.get(model["id"])
                    models[model["id"]] = (
                        {**previous, **model} if isinstance(previous, dict) else model
                    )
            if models:
                merged_provider["models"] = list(models.values())
            providers[provider_id] = merged_provider
        merged_doc = {**lower_doc, **higher_doc, "providers": providers}
        target.write_text(json.dumps(merged_doc, indent=2) + "\n")

    def _ensure_auth(self, home: Path, environ: Mapping[str, str]) -> None:
        """Link credential-dir ``auth.json`` at pi's native Outfitter fallback location."""
        pi_config = home / PI_NATIVE_CONFIG_DIR
        pi_config.mkdir(parents=True, exist_ok=True)
        auth = pi_config / AUTH_FILE
        if auth.exists() or auth.is_symlink():
            return
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if credentials and (Path(credentials) / AUTH_FILE).exists():
            auth.symlink_to(Path(credentials) / AUTH_FILE)

    def argv(self, ctx: LaunchContext) -> list[str]:
        """Launch the selected Outfitter agent through pi with Panopticon pass-through args."""
        config_dir = self.config_dir(ctx.home)
        argv = ["outfitter", "run"]
        if ctx.starting_model:
            argv.append(ctx.starting_model)
        argv += ["--harness", "pi"]

        extension = config_dir / EXTENSION_FILE
        overview = config_dir / WORKFLOW_OVERVIEW_FILE
        if overview.exists() and overview.read_text().strip():
            argv += ["--append-prompt", str(overview)]
        argv.append("--")
        if extension.exists():
            argv += ["--extension", str(extension)]
        skills = config_dir / INJECTED_SKILLS_ROOT
        if skills.exists():
            for skill in sorted(path for path in skills.iterdir() if path.is_dir()):
                argv += ["--skill", str(skill)]

        sessions = ctx.home / ".pi" / "agent" / "sessions"
        if sessions.exists() and any(sessions.rglob("*.jsonl")):
            argv.append("--continue")
            if ctx.turn == "agent":
                argv.append(INTERRUPT_PROMPT)
            return argv
        if ctx.initial_prompt:
            argv.append(ctx.initial_prompt)
        return argv
