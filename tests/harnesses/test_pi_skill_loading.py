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


_TURN_PROBE = r"""
import http from 'node:http';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseArgs } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli/args.js';
import { createAgentSession } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/sdk.js';
import { DefaultResourceLoader } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/resource-loader.js';
import { SettingsManager } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/settings-manager.js';
import { SessionManager } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/session-manager.js';
import { AuthStorage } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/auth-storage.js';
import { ModelRegistry } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/model-registry.js';
import { createReadTool } from '/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/read.js';
const input = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const args = parseArgs(input.argv.slice(1));
const dir = mkdtempSync(join(tmpdir(), 'pi-turn-test-'));
const records = [], turns = [];
let turn = 'user', requests = 0, session, abort, promptError, auth, registry;
const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    if (req.url === '/tasks/turn-test/turn') {
        if (req.headers.authorization !== 'Bearer synthetic-task-token') {
            res.writeHead(401); res.end(); return;
        }
        turn = JSON.parse(body).turn;
        turns.push(turn);
        res.writeHead(204); res.end(); return;
    }
    requests++;
    records.push({ type: 'request', turn });
    if (input.scenario === 'queued-logout' && requests === 1) {
        auth.logout('synthetic');
        registry.refresh();
        await session.prompt('Queued while still streaming.', { streamingBehavior: 'followUp' });
        records.push({ type: 'queued_input', turn, isIdle: session.isIdle });
    }
    if (input.scenario === 'abort') {
        abort = session.abort();
        return;
    }
    if (['success', 'exhaustion'].includes(input.scenario) && (requests === 1 || input.scenario === 'exhaustion')) {
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: { message: 'synthetic temporary server error' } }));
        return;
    }
    const read = input.scenario === 'success' && requests === 2;
    const delta = read ? { tool_calls: [{ index: 0, id: 'read-proof', type: 'function',
        function: { name: 'read', arguments: JSON.stringify({ path: join(dir, 'proof.txt') }) }
    }] } : { content: 'SYNTHETIC_DONE' };
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.end('data: ' + JSON.stringify({ id: 'mock', object: 'chat.completion.chunk',
        created: 0, model: 'mock', choices: [{ index: 0, delta,
            finish_reason: read ? 'tool_calls' : 'stop' }]
    }) + '\n\ndata: [DONE]\n\n');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
    process.env.PANOPTICON_SERVICE_URL = `http://127.0.0.1:${server.address().port}`;
    process.env.PANOPTICON_TASK_ID = 'turn-test';
    process.env.PANOPTICON_SERVICE_AUTH_TOKEN = 'synthetic-task-token';
    const settings = SettingsManager.inMemory({ retry: {
        enabled: true, maxRetries: 1, baseDelayMs: 1, provider: { maxRetries: 0 }
    }, compaction: { enabled: false } });
    auth = AuthStorage.inMemory();
    const storedAuth = ['logout', 'stored-auth', 'queued-logout'].includes(input.scenario);
    if (storedAuth) auth.set('synthetic', { type: 'api_key', key: 'synthetic-provider-key' });
    process.env.PI_SYNTHETIC_API_KEY = 'synthetic-provider-key';
    const models = join(dir, 'models.json');
    writeFileSync(join(dir, 'proof.txt'), 'synthetic tool result');
    writeFileSync(models, JSON.stringify({ providers: { synthetic: {
        baseUrl: `${process.env.PANOPTICON_SERVICE_URL}/v1`, api: 'openai-completions',
        ...(storedAuth ? {} : { apiKey: input.scenario === 'env-auth' ? '${PI_SYNTHETIC_API_KEY}' : 'synthetic-provider-key' }),
        models: [{ id: 'mock', reasoning: false,
            contextWindow: 8192, maxTokens: 128 }]
    } } }));
    registry = new ModelRegistry(auth, models);
    const loader = new DefaultResourceLoader({ cwd: dir, agentDir: dir,
        settingsManager: settings, additionalExtensionPaths: args.extensions,
        noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
        noContextFiles: true });
    await loader.reload();
    if (loader.getExtensions().errors.length) throw new Error(JSON.stringify(loader.getExtensions().errors));
    ({ session } = await createAgentSession({ cwd: dir, agentDir: dir, authStorage: auth,
        modelRegistry: registry, model: registry.find('synthetic', 'mock'),
        settingsManager: settings, sessionManager: SessionManager.inMemory(dir),
        resourceLoader: loader, tools: [createReadTool(dir)] }));
    const initiallyAuthenticated = registry.hasConfiguredAuth(session.model);
    session.subscribe(event => {
        if (['auto_retry_start', 'agent_settled', 'tool_execution_start', 'tool_execution_end'].includes(event.type)) {
            records.push({ type: event.type, turn });
        }
    });
    if (input.scenario === 'logout') {
        auth.logout('synthetic');
        registry.refresh();
        try {
            await session.prompt('Synthetic response only.', {
                preflightResult: ok => records.push({ type: 'preflight', ok, turn })
            });
        } catch (error) { promptError = error.message; }
    } else if (input.scenario === 'autonomous') {
        await session.sendCustomMessage({ customType: 'synthetic', content: 'Synthetic response only.',
            display: false }, { triggerTurn: true });
    } else {
        await session.prompt('Synthetic response only.');
    }
    if (abort) await abort;
    console.log(JSON.stringify({ records, turns, requests, initiallyAuthenticated,
        configuredAuth: registry.hasConfiguredAuth(session.model), isIdle: session.isIdle, promptError,
        stopReason: session.messages.filter(message => message.role === 'assistant').at(-1)?.stopReason }));
} finally {
    session?.dispose();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    rmSync(dir, { recursive: true, force: true });
}
"""


# 2119: REQ-008.6.1
# 2119: REQ-016.3.1
@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "exhaustion",
        "abort",
        "autonomous",
        "logout",
        "stored-auth",
        "env-auth",
        "queued-logout",
    ],
)
def test_pinned_pi_turn_extension_tracks_the_complete_native_run(
    pi_loader_image: str, tmp_path: Path, scenario: str
) -> None:
    """Use native retries/tools/abort with synthetic HTTP responses; never call a real model."""
    harness = PiHarness()
    home = tmp_path / "home"
    harness.bootstrap(
        BootstrapContext(home=home, cwd=tmp_path, service_url="http://unused", task_id="turn-test")
    )
    (tmp_path / "input.json").write_text(
        json.dumps(
            {"argv": harness.argv(LaunchContext(home=home, cwd=tmp_path)), "scenario": scenario}
        )
    )
    (tmp_path / "turn-probe.mjs").write_text(_TURN_PROBE)
    try:
        result = json.loads(
            _docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=bind,src={tmp_path},dst={tmp_path},readonly",
                "--env",
                f"HOME={home}",
                pi_loader_image,
                "node",
                str(tmp_path / "turn-probe.mjs"),
                str(tmp_path / "input.json"),
            )
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(exc.stderr)
    assert result["initiallyAuthenticated"] is True
    assert result["isIdle"] is True
    if scenario == "logout":
        assert result["configuredAuth"] is False
        assert result["requests"] == 0
        assert result["turns"] == ["user"]
        assert result["records"] == [{"type": "preflight", "ok": False, "turn": "user"}]
        assert "No API key found for synthetic" in result["promptError"]
        assert "/login" in result["promptError"]
        return
    assert result["configuredAuth"] is (scenario != "queued-logout")
    assert "promptError" not in result
    assert result["requests"] == {"success": 3, "exhaustion": 2}.get(scenario, 1)
    assert result["stopReason"] == {
        "exhaustion": "error",
        "abort": "aborted",
        "queued-logout": "error",
    }.get(scenario, "stop")
    assert result["turns"][-1] == "user"
    assert result["turns"].count("user") == 1
    assert result["turns"][0] == "agent"
    assert all(
        event["turn"] == ("user" if event["type"] == "agent_settled" else "agent")
        for event in result["records"]
    )
    assert sum(event["type"] == "agent_settled" for event in result["records"]) == 1
    assert sum(event["type"] == "auto_retry_start" for event in result["records"]) == (
        scenario in {"success", "exhaustion"}
    )
    assert sum(event["type"] == "tool_execution_start" for event in result["records"]) == (
        scenario == "success"
    )
    if scenario == "queued-logout":
        assert {"type": "queued_input", "turn": "agent", "isIdle": False} in result["records"]
