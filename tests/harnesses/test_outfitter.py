"""Golden rendering tests for the Outfitter adapter (pinned 1.16.0).

The install requirements, ``run <agent> --harness pi --`` pass-through surface, catalog-source
settings shape, and Pi state fallback come from Outfitter's published docs/source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from panopticon.core.models import Skill
from panopticon.harnesses import INTERRUPT_PROMPT, BootstrapContext, LaunchContext
from panopticon.harnesses.outfitter import (
    EXTENSION_FILE,
    NODE_VERSION,
    OUTFITTER_VERSION,
    PI_NATIVE_CONFIG_DIR,
    PI_VERSION,
    PROFILE_SOURCES_DIR,
    SETTINGS,
    SETTINGS_FILE,
    TURN_EXTENSION,
    WORKFLOW_OVERVIEW_FILE,
    OutfitterHarness,
)

HARNESS = OutfitterHarness()


def _ctx(home: Path, **kwargs: str | None) -> LaunchContext:
    return LaunchContext(home=home, cwd=Path("/workspace"), **kwargs)  # type: ignore[arg-type]


def _bootstrap_ctx(home: Path, **kwargs: object) -> BootstrapContext:
    defaults: dict[str, object] = {
        "home": home,
        "cwd": Path("/workspace"),
        "service_url": "http://host.docker.internal:8000",
        "task_id": "t1",
        "skills": [Skill(name="open-pr", description="Open the PR.", instructions="gh pr create")],
        "operations": {"advance": "COMPLETE"},
        "overview": "# the workflow map",
        "environ": {},
    }
    defaults.update(kwargs)
    return BootstrapContext(**defaults)  # type: ignore[arg-type]


def test_bootstrap_pins_every_outfitter_artifact(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    config = tmp_path / ".outfitter"
    assert (tmp_path / ".agents" / SETTINGS_FILE).read_text() == SETTINGS
    assert SETTINGS == "default_harness: pi\nsources:\n  - path: ../.outfitter/profile_sources\n"
    assert (config / PROFILE_SOURCES_DIR).is_dir()
    assert (config / WORKFLOW_OVERVIEW_FILE).read_text() == "# the workflow map"
    assert (config / EXTENSION_FILE).read_text() == TURN_EXTENSION

    skill = (tmp_path / ".agents" / "skills" / "open-pr" / "SKILL.md").read_text()
    assert skill == (
        "---\nname: open-pr\ndescription: Open the PR.\n---\n"
        'gh pr create\n\nThis is task `t1` — pass `task_id="t1"` to every panopticon MCP '
        "tool you call here.\n"
    )
    operation = (tmp_path / ".agents" / "skills" / "advance" / "SKILL.md").read_text()
    assert operation == (
        "---\nname: advance\ndescription: Apply the workflow's 'advance' operation.\n---\n"
        "Apply this workflow's `advance` operation — it moves the task to **COMPLETE**. "
        "pi has no MCP client, so call the task service's REST API directly (no request body "
        "needed): `curl --disable --noproxy '*' --fail --silent --show-error --request POST "
        '"http://host.docker.internal:8000/tasks/t1/operations/advance"`. Don\'t edit the state '
        "directly. It's gated on the current state's responsibilities and starts a new turn.\n\n"
        'This is task `t1` — pass `task_id="t1"` to every panopticon MCP tool you call here.\n'
    )


def test_bootstrap_adds_existing_credential_profile_source(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    env = {"PANOPTICON_CREDENTIALS": str(credentials)}

    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ=env))
    assert (tmp_path / ".agents" / SETTINGS_FILE).read_text() == SETTINGS

    profiles = credentials / "outfitter" / ".agents"
    profiles.mkdir(parents=True)
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ=env))
    assert (tmp_path / ".agents" / SETTINGS_FILE).read_text() == (
        SETTINGS + f"  - path: {profiles}\n"
    )


def test_suggested_models_discovers_agents(tmp_path: Path) -> None:
    founder = tmp_path / "founder"
    founder.mkdir()
    (founder / "agent.md").write_text(
        "---\nlabel: Founder\ndescription: Founder-operator defaults for product and engineering.\n---\n"
    )
    data = tmp_path / "data-analyst"
    data.mkdir()
    (data / "agent.md").write_text(
        '---\nname: "data-analyst"\nlabel: Data Analyst\n'
        'description: "Analyze product data with concise evidence."\n---\n'
    )
    directory = tmp_path / "engineering-default"
    directory.mkdir()
    (directory / "agent.md").write_text(
        "---\nname: 'engineering-default'\nlabel: Engineering Default\n"
        "description: 'Review, build, and ship.'\n---\n"
    )

    harness = OutfitterHarness(profile_sources_root=tmp_path)

    assert harness.field_label == "agent"
    assert harness.suggested_models() == (
        ("data-analyst", "data-analyst — Analyze product data with concise evidence."),
        ("engineering-default", "engineering-default — Review, build, and ship."),
        ("founder", "founder — Founder-operator defaults for product and engineering."),
    )


def test_suggested_models_honors_the_configured_agents_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = tmp_path / "catalog"
    agent = catalog / "agents" / "resident-engineer"
    agent.mkdir(parents=True)
    (agent / "agent.md").write_text("---\ndescription: Work without supervision.\n---\n")
    source = tmp_path / "community"
    sourced_agent = source / "agents" / "resident-reviewer"
    sourced_agent.mkdir(parents=True)
    (sourced_agent / "agent.md").write_text("---\ndescription: Review independently.\n---\n")
    (catalog / "settings.yml").write_text(f"sources:\n  - path: {source}\n")
    monkeypatch.setenv("PANOPTICON_AGENTS", str(catalog))

    assert HARNESS.suggested_models() == (
        ("resident-engineer", "resident-engineer — Work without supervision."),
        ("resident-reviewer", "resident-reviewer — Review independently."),
    )


def test_suggested_models_block_description_degrades_to_id_only(tmp_path: Path) -> None:
    data = tmp_path / "data-analyst"
    data.mkdir()
    (data / "agent.md").write_text(
        "---\nname: data-analyst\ndescription: >-\n  Analyze product data with\n  concise evidence.\n---\n"
    )

    assert OutfitterHarness(profile_sources_root=tmp_path).suggested_models() == (
        ("data-analyst", "data-analyst"),
    )


def test_suggested_models_skips_abstract_and_unreadable_agents_and_truncates(
    tmp_path: Path,
) -> None:
    template = tmp_path / "base"
    template.mkdir()
    (template / "agent.md").write_text(
        "---\nname: base\nabstract: TrUe\ndescription: Not directly launchable.\n---\n"
    )
    long = tmp_path / "long"
    long.mkdir()
    (long / "agent.md").write_text("---\ndescription: " + "word " * 30 + "\n---\n")
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / "agent.md").write_bytes(b"\xff")

    suggestions = OutfitterHarness(profile_sources_root=tmp_path).suggested_models()

    assert len(suggestions) == 1
    assert suggestions[0][0] == "long"
    assert suggestions[0][1].startswith("long — word")
    assert suggestions[0][1].endswith("…")
    assert len(suggestions[0][1]) <= 80


def test_suggested_models_fails_soft_when_source_is_absent(tmp_path: Path) -> None:
    assert OutfitterHarness(profile_sources_root=tmp_path / "missing").suggested_models() == ()


def test_argv_passes_agent_and_panopticon_controls_through_to_pi(tmp_path: Path) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path))
    assert HARNESS.argv(
        _ctx(tmp_path, starting_model="engineering-default", initial_prompt="start now")
    ) == [
        "outfitter",
        "run",
        "engineering-default",
        "--harness",
        "pi",
        "--append-prompt",
        str(tmp_path / ".outfitter" / WORKFLOW_OVERVIEW_FILE),
        "--",
        "--extension",
        str(tmp_path / ".outfitter" / EXTENSION_FILE),
        "--skill",
        str(tmp_path / ".agents" / "skills" / "advance"),
        "--skill",
        str(tmp_path / ".agents" / "skills" / "open-pr"),
        "start now",
    ]


def test_starting_model_is_an_agent_slug_not_a_pi_model(tmp_path: Path) -> None:
    argv = HARNESS.argv(_ctx(tmp_path, starting_model="local-qwen-high"))
    assert argv == [
        "outfitter",
        "run",
        "local-qwen-high",
        "--harness",
        "pi",
        "--",
    ]
    assert "--model" not in argv
    assert "-p" not in argv and "--print" not in argv  # interactive tmux launch, not smoke mode


def test_blank_overview_and_absent_skills_still_render_required_turn_extension(
    tmp_path: Path,
) -> None:
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, overview=" ", skills=[], operations={}))
    with pytest.raises(ValueError, match="requires an agent slug"):
        HARNESS.argv(_ctx(tmp_path))
    assert HARNESS.argv(_ctx(tmp_path, starting_model="vega")) == [
        "outfitter",
        "run",
        "vega",
        "--harness",
        "pi",
        "--",
        "--extension",
        str(tmp_path / ".outfitter" / EXTENSION_FILE),
    ]


def test_resume_uses_pi_native_state_fallback_and_interrupt_prompt(tmp_path: Path) -> None:
    sessions = tmp_path / ".pi" / "agent" / "sessions" / "--workspace--"
    sessions.mkdir(parents=True)
    (sessions / "session-1.jsonl").write_text("{}")
    assert HARNESS.argv(
        _ctx(
            tmp_path,
            starting_model="ignored-on-resume",
            initial_prompt="ignored on resume",
            turn="agent",
        )
    ) == [
        "outfitter",
        "run",
        "ignored-on-resume",
        "--harness",
        "pi",
        "--",
        "--continue",
        INTERRUPT_PROMPT,
    ]


def test_auth_delegates_to_pi_presence_rules_and_links_credential_file(tmp_path: Path) -> None:
    assert HARNESS.missing_auth({"GROQ_API_KEY": "k"}, home=tmp_path) is None
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text("{}")
    env = {"PANOPTICON_CREDENTIALS": str(credentials)}
    assert HARNESS.missing_auth(env, home=tmp_path) is None
    HARNESS.bootstrap(_bootstrap_ctx(tmp_path, environ=env))
    auth = tmp_path / PI_NATIVE_CONFIG_DIR / "auth.json"
    assert auth.is_symlink() and auth.resolve() == (credentials / "auth.json").resolve()


def test_missing_auth_accepts_outfitters_native_pi_state_fallback(tmp_path: Path) -> None:
    auth = tmp_path / PI_NATIVE_CONFIG_DIR / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    assert HARNESS.missing_auth({}, home=tmp_path) is None


def test_missing_auth_does_not_mistake_direct_pi_harness_state_for_outfitter_state(
    tmp_path: Path,
) -> None:
    direct_pi_auth = tmp_path / ".pi" / "auth.json"
    direct_pi_auth.parent.mkdir(parents=True)
    direct_pi_auth.write_text("{}")
    assert HARNESS.missing_auth({}, home=tmp_path) is not None


def test_missing_auth_honestly_names_pi_credentials(tmp_path: Path) -> None:
    detail = HARNESS.missing_auth({}, home=tmp_path)
    assert detail is not None and "pi credentials" in detail and "credential_dir" in detail


def test_image_layer_installs_all_runtime_components_at_pinned_versions() -> None:
    layer = HARNESS.image_layer()
    assert NODE_VERSION == "22.19.0"
    assert PI_VERSION == "0.80.3"
    assert OUTFITTER_VERSION == "1.16.0"
    assert layer == (
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
    assert ".tar.xz" not in layer and "--xz" not in layer and "xz-utils" not in layer


def test_config_dir_is_the_outfitter_home_and_env_needs_no_override(tmp_path: Path) -> None:
    assert HARNESS.config_dir(tmp_path) == tmp_path / ".outfitter"
    assert HARNESS.env(_ctx(tmp_path)) == {}
