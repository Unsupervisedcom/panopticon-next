"""Verify rendered skills with the pinned Pi loader, without starting an agent or using credentials."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from panopticon.core.artifact_skills import ARTIFACT_SKILL
from panopticon.core.provisioning import PROVISION_SKILL
from panopticon.harnesses import BootstrapContext, LaunchContext
from panopticon.harnesses.pi import NODE_VERSION, PI_VERSION, PiHarness


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=300
    ).stdout


def _docker_running() -> bool:
    return bool(
        shutil.which("docker")
        and subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    )


pytestmark = pytest.mark.skipif(not _docker_running(), reason="needs a working Docker daemon")


@pytest.fixture(scope="module")
def pi_loader_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    context = tmp_path_factory.mktemp("pi-skill-loader-image")
    (context / "Dockerfile").write_text(
        f"FROM node:{NODE_VERSION}-bookworm-slim\n"
        "RUN npm install --global --ignore-scripts "
        f"@earendil-works/pi-coding-agent@{PI_VERSION}\n"
    )
    image = f"panopticon-pi-loader-{uuid.uuid4().hex}"
    try:
        _docker("build", "--tag", image, str(context))
        yield image
    finally:
        subprocess.run(["docker", "image", "rm", image], capture_output=True)


_LOADER_PROBE = r"""
import { readFileSync } from 'node:fs';
import { parseArgs } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli/args.js';
import { DefaultResourceLoader } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/resource-loader.js';
import { buildSystemPrompt } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/system-prompt.js';
const input = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const args = parseArgs(input.argv.slice(1));
const loader = new DefaultResourceLoader({
    cwd: input.cwd,
    agentDir: process.env.PI_CODING_AGENT_DIR,
    additionalSkillPaths: args.skills ?? [],
    noSkills: args.noSkills,
    appendSystemPrompt: args.appendSystemPrompt,
    // Exercise startup's package discovery and skill loading without extension execution.
    noExtensions: true
});
await loader.reload();
const loaded = loader.getSkills();
console.log(JSON.stringify({
    argumentDiagnostics: args.diagnostics,
    diagnostics: loaded.diagnostics,
    skills: loaded.skills.map(skill => ({
        name: skill.name, description: skill.description,
        content: readFileSync(skill.filePath, 'utf8')
    })),
    prompt: buildSystemPrompt({
        cwd: input.cwd,
        skills: loaded.skills,
        appendSystemPrompt: loader.getAppendSystemPrompt().join('\n\n')
    })
}));
"""


# 2119: REQ-051.5.1
@pytest.mark.parametrize("resume", [False, True], ids=["first-run", "resume"])
def test_pinned_pi_loads_rendered_skill_content_from_real_launch_arguments(
    pi_loader_image: str, tmp_path: Path, resume: bool
) -> None:
    harness = PiHarness()
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    skills = (PROVISION_SKILL, ARTIFACT_SKILL)
    harness.bootstrap(
        BootstrapContext(
            home=home,
            cwd=workspace,
            service_url="http://service.invalid",
            task_id="skill-loader-test",
            skills=skills,
            overview="Use the available task skills.",
        )
    )
    if resume:
        sessions = home / ".pi" / "agent" / "sessions"
        sessions.mkdir()
        (sessions / "existing.jsonl").write_text('{"type":"session"}\n')
    context = LaunchContext(home=home, cwd=workspace)
    argv = harness.argv(context)
    assert ("--continue" in argv) is resume
    rendered = home / ".agents" / "skills" / "provision" / "SKILL.md"
    original_content = rendered.read_text()
    (tmp_path / "input.json").write_text(json.dumps({"argv": argv, "cwd": str(workspace)}))
    (tmp_path / "probe.mjs").write_text(_LOADER_PROBE)

    def probe() -> dict[str, Any]:
        return json.loads(
            _docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=bind,src={tmp_path},dst={tmp_path},readonly",
                "--env",
                f"HOME={home}",
                "--env",
                f"PI_CODING_AGENT_DIR={harness.env(context)['PI_CODING_AGENT_DIR']}",
                pi_loader_image,
                "node",
                str(tmp_path / "probe.mjs"),
                str(tmp_path / "input.json"),
            )
        )

    def assert_expected_skills(result: dict[str, Any]) -> None:
        loaded = {skill["name"]: skill for skill in result["skills"]}
        assert set(loaded) == {"provision", "artifacts"}
        for skill in skills:
            assert loaded[skill.name]["description"] == skill.description
            assert skill.instructions in loaded[skill.name]["content"]
            assert f"<name>{skill.name}</name>" in result["prompt"]
        assert "Use the available task skills." in result["prompt"]

    result = probe()
    assert result["argumentDiagnostics"] == []
    assert result["diagnostics"] == []
    assert_expected_skills(result)

    # The same startup pipeline and acceptance assertions must reject missing or invalid skills.
    rendered.unlink()
    missing = probe()
    with pytest.raises(AssertionError):
        assert_expected_skills(missing)
    assert {skill["name"] for skill in missing["skills"]} == {"artifacts"}
    assert "<name>provision</name>" not in missing["prompt"]

    rendered.write_text(
        original_content.replace(f"description: {PROVISION_SKILL.description}", "description: ''")
    )
    malformed = probe()
    with pytest.raises(AssertionError):
        assert_expected_skills(malformed)
    assert {skill["name"] for skill in malformed["skills"]} == {"artifacts"}
    assert "<name>provision</name>" not in malformed["prompt"]
    assert malformed["diagnostics"]
