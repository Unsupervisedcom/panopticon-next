"""Opt-in clean-host acceptance for the documented new-user journey.

The ordinary test suite does not contact a package index, GitHub, or an agent model. This test is
intended to be run *on a disposable host* whose Docker/tmux namespace is dedicated to the run. It
installs an immutable, reachable artifact into a new pipx home and drives the shipped quickstart and
dashboard through one real ``github-self-reviewed`` task. REST, GitHub, and tmux calls after setup
are observation-only; the PTY carries every task mutation through the documented user interface.

Required inputs are deliberately verbose and have no fallback to a developer's native login or
credential files. See ``docs/getting-started.md`` for the invocation contract.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import pty
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WALKTHROUGH_PATH = _ROOT / "docs" / "getting-started.md"
_RELEASE_VERSION = cast(
    str, tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["version"]
)
_RELEASE_WHEEL = f"panopticon_next-{_RELEASE_VERSION}-py3-none-any.whl"
_WALKTHROUGH_SHA256 = "7b0c93a0b2f95016950e87822bd3d7a2b7b4f24f681928d701ab4676e0ffd2b8"
_ACCEPTANCE_SOURCE_AST_SHA256 = "ba1c67a9cca4ba1c59fbbaf3e21c934200357161f892bcef1d039c7cecc28939"
_OPT_IN = "I_AM_RUNNING_ON_A_DISPOSABLE_HOST"
_REQUIRED = (
    "PANOPTICON_NEW_USER_ACCEPTANCE",
    "PANOPTICON_ACCEPTANCE_INSTALL_SPEC",
    "PANOPTICON_ACCEPTANCE_GITHUB_REPO",
    "PANOPTICON_ACCEPTANCE_BASE_SHA",
    "PANOPTICON_ACCEPTANCE_GH_TOKEN",
    "PANOPTICON_ACCEPTANCE_HARNESS",
    "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV",
    "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN",
)
_HARNESS_AUTH_ENV = {
    "claude": frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}),
    "codex": frozenset({"CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"}),
}
_REGISTERED_HARNESSES = frozenset({"claude", "codex", "outfitter", "pi"})
_ADVANCE_COMMAND = {"claude": "/advance", "codex": "$advance"}
_PTY_ROWS = 45
_PTY_COLUMNS = 180
_PTY_BUFFER_LIMIT = 128 * 1024
_SESSION_SWITCH_PROBE_KEYS = b"\x00"
_DIAGNOSTIC_LIMIT = 12_000
_HASHED_LOCAL_WHEEL = re.compile(
    r"panopticon-next @ file:///\S+\.whl#sha256=[0-9a-f]{64}\Z", re.IGNORECASE
)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_LEGACY_WORKTREE_STATE = ("panopticon.db", "artifacts", "layers", "cache", "tasks")
_WALKTHROUGH_SEQUENCE = (
    f"pipx install ./{_RELEASE_WHEEL}",
    "panopticon --version",
    "panopticon doctor",
    "panopticon quickstart",
    "unset PANOPTICON_SERVICE_AUTH_FILE PANOPTICON_SERVICE_AUTH_MODE PANOPTICON_SERVICE_AUTH_TOKEN",
    "panopticon tasks",
    "Press `n`. Select the registered repository with the arrow keys and Enter.",
    "Select `github-self-reviewed` and press Enter.",
    "Add a hello-panopticon.txt file containing hello from Panopticon and do not change any",
    "press `t` to attach to its agent session",
    "session; `Ctrl-b d` returns you to the",
    "press `a`, select `plan.md`, and press Enter.",
    "Press `t` to attach, give any correction",
    "| Claude | `/advance` |",
    "| Codex | `$advance` |",
    "press `p` to open the pull request",
    "attach and invoke `advance` with the same harness-specific syntax",
    "panopticon stop",
)
_WALKTHROUGH_PLACEHOLDERS = (
    "/path/to/disposable-repo",
    "PANOPTICON_ACCEPTANCE_INSTALL_SPEC",
    "PANOPTICON_ACCEPTANCE_GITHUB_REPO",
    "PANOPTICON_ACCEPTANCE_BASE_SHA",
    "PANOPTICON_ACCEPTANCE_HARNESS",
    "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV",
    "PANOPTICON_ACCEPTANCE_GH_TOKEN",
    "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN",
)
_PASSTHROUGH_ENV = (
    "ALL_PROXY",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "USER",
    "XDG_RUNTIME_DIR",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)


@dataclass(frozen=True)
class LiveConfiguration:
    install_spec: str
    repo_url: str
    base_sha: str
    github_token: str = field(repr=False)
    harness: str
    harness_auth_env: str
    harness_auth_token: str = field(repr=False)


@dataclass(frozen=True)
class DocumentedWalkthrough:
    install_version: str
    install_argv: tuple[str, ...]
    quickstart_argv: tuple[str, ...]
    task_prompt: str
    advance_commands: Mapping[str, str]
    new_task_key: bytes
    attach_key: bytes
    artifact_key: bytes
    pull_request_key: bytes
    detach_keys: bytes
    submit_key: bytes
    next_choice_key: bytes
    previous_row_key: bytes


class _PostSetupRequestAudit:
    """Fail immediately if the acceptance driver mutates REST state after setup."""

    def __init__(self) -> None:
        self.active = False

    def __call__(self, request: httpx.Request) -> None:
        if self.active and request.method not in {"GET", "HEAD"}:
            raise AssertionError(
                f"post-setup acceptance REST traffic must be observation-only, got {request.method}"
            )


_ALLOWED_POST_SETUP_CALLS = frozenset(
    {
        "AssertionError",
        "Path",
        "_advance_command",
        "_api_get",
        "_assert_task_remains_user_gated",
        "_send_session_switch_while_attached",
        "_send_opener_key_while_unopened",
        "_send_user_approval_while_gated",
        "_capture_pane",
        "_client_sessions",
        "_failure_diagnostics",
        "_github_get",
        "_assert_no_panopticon_tmux_server",
        "_pane_id",
        "_pane_pid",
        "_redacted_tail",
        "_run",
        "_wait_for_client_session",
        "_wait_for_pane_row_texts",
        "_wait_for_pane_text",
        "_wait_until",
        "any",
        "artifact_log.is_file",
        "artifact_log.read_text",
        "base64.b64decode",
        "browser_log.is_file",
        "browser_log.read_text",
        "client.get",
        "config.repo_url.rstrip",
        "content.strip",
        "decode",
        "dict",
        "driver.send",
        "driver.terminate",
        "driver.wait",
        "encode",
        "fresh_shell.pop",
        "int",
        "invalid_client_credential.chmod",
        "invalid_client_credential.write_text",
        "isinstance",
        "json.dumps",
        "len",
        "line.split",
        "listed.stdout.splitlines",
        "next",
        "observed.get",
        "opened_artifact.read_bytes",
        "plan_response.raise_for_status",
        "plan_response.text.strip",
        "pr_match.group",
        "prompt.encode",
        "quote",
        "re.escape",
        "re.fullmatch",
        "re.search",
        "rstrip",
        "secrets_file.is_file",
        "secrets_file.is_symlink",
        "secrets_file.read_text",
        "secrets_file.stat",
        "sorted",
        "splitlines",
        "startswith",
        "stdout.strip",
        "str",
        "strip",
        "subprocess.run",
        "time.sleep",
        "visible_artifacts.index",
        "workflow_names.index",
    }
)
_ALLOWED_PRE_SETUP_CALLS = frozenset(
    {
        "Path",
        "_PostSetupRequestAudit",
        "_PtyProcess.start",
        "_WALKTHROUGH_PATH.read_text",
        "_api_get",
        "_assert_expected_origin",
        "_assert_installed_walkthrough_version",
        "_assert_no_legacy_worktree_state",
        "_assert_no_panopticon_tmux_server",
        "_complete_setup_task",
        "_documented_local_wheel",
        "_documented_walkthrough",
        "_github_get",
        "_github_status",
        "_run",
        "_wait_until",
        "askpass.chmod",
        "askpass.write_text",
        "auth_file.is_file",
        "auth_file.read_text",
        "browser.chmod",
        "browser.write_text",
        "driver.send",
        "driver.wait_for_text",
        "env.get",
        "get",
        "httpx.Client",
        "is_file",
        "json.loads",
        "line.lower",
        "line.partition",
        "list",
        "mkdir",
        "next",
        "opener.chmod",
        "opener.write_text",
        "os.pathsep.join",
        "package.splitlines",
        "panopticon.is_file",
        "path.exists",
        "path.removesuffix",
        "quote",
        "recorder_bin.mkdir",
        "repo.startswith",
        "repository.get",
        "shutil.which",
        "split",
        "splitlines",
        "startswith",
        "stdout.strip",
        "str",
        "strip",
        "subprocess.run",
        "urlsplit",
    }
)
_ALLOWED_DRIVER_INPUTS = frozenset(
    {
        "prompt.encode() + walkthrough.submit_key",
        "walkthrough.artifact_key",
        "walkthrough.attach_key",
        "walkthrough.detach_keys",
        "walkthrough.new_task_key",
        "walkthrough.next_choice_key * plan_index + walkthrough.submit_key",
        "walkthrough.next_choice_key * workflow_index + walkthrough.submit_key",
        "walkthrough.previous_row_key",
        "walkthrough.pull_request_key",
        "walkthrough.submit_key",
    }
)
_ALLOWED_SETUP_DRIVER_INPUTS = frozenset(
    {
        "b'\\r'",
        "b'y\\r'",
        "config.github_token.encode() + b'\\r'",
        "config.harness_auth_token.encode() + b'\\r'",
    }
)
_OBSERVER_HELPER_CALLS = {
    "_advance_command": frozenset(),
    "_api_get": frozenset({"client.get", "response.json", "response.raise_for_status"}),
    "_assert_task_remains_user_gated": frozenset({"_api_get", "time.monotonic", "time.sleep"}),
    "_assert_no_panopticon_tmux_server": frozenset(
        {"Path", "dict", "os.getuid", "socket_path.exists", "subprocess.run"}
    ),
    "_send_user_approval_while_gated": frozenset(
        {
            "_api_get",
            "_capture_pane",
            "_wait_until",
            "command.decode",
            "count",
            "driver.send",
            "failures.append",
            "input_delivered.is_set",
            "input_delivered.set",
            "monitor_ready.set",
            "monitor_ready.wait",
            "monitor_thread.join",
            "monitor_thread.start",
            "pane.count",
            "pane_before.count",
            "pytest.fail",
            "strip",
            "threading.Event",
            "threading.Thread",
            "time.sleep",
        }
    ),
    "_send_session_switch_while_attached": frozenset(
        {
            "_client_sessions",
            "driver.send",
            "failures.append",
            "input_delivered.is_set",
            "input_delivered.set",
            "monitor_ready.set",
            "monitor_ready.wait",
            "monitor_thread.join",
            "monitor_thread.start",
            "pytest.fail",
            "threading.Event",
            "threading.Thread",
            "time.sleep",
        }
    ),
    "_send_opener_key_while_unopened": frozenset(
        {
            "driver.send",
            "failures.append",
            "input_delivered.is_set",
            "input_delivered.set",
            "log_path.exists",
            "monitor_ready.set",
            "monitor_ready.wait",
            "monitor_thread.join",
            "monitor_thread.start",
            "pytest.fail",
            "threading.Event",
            "threading.Thread",
            "time.sleep",
        }
    ),
    "_capture_pane": frozenset({"dict", "subprocess.run"}),
    "_client_sessions": frozenset({"dict", "result.stdout.splitlines", "subprocess.run"}),
    "_complete_setup_task": frozenset(
        {
            "_api_get",
            "_capture_pane",
            "_wait_for_client_session",
            "config.github_token.encode",
            "config.harness_auth_token.encode",
            "driver.send",
            "pytest.fail",
            "responded.add",
            "set",
            "time.monotonic",
            "time.sleep",
        }
    ),
    "_failure_diagnostics": frozenset(
        {"_capture_pane", "_client_sessions", "_redacted_tail", "driver.tail", "len"}
    ),
    "_github_get": frozenset({"httpx.get", "response.json"}),
    "_pane_id": frozenset({"dict", "result.stdout.strip", "subprocess.run"}),
    "_pane_pid": frozenset({"dict", "int", "result.stdout.strip", "subprocess.run"}),
    "_redacted_tail": frozenset({"len", "set", "sorted", "text.replace"}),
    "_run": frozenset({"dict", "subprocess.run"}),
    "_wait_for_client_session": frozenset({"_client_sessions", "_wait_until"}),
    "_wait_for_pane_row_texts": frozenset(
        {"_capture_pane", "_wait_until", "all", "any", "pane.splitlines", "str"}
    ),
    "_wait_for_pane_text": frozenset({"_capture_pane", "_wait_until", "str"}),
    "_wait_until": frozenset({"accept", "probe", "pytest.fail", "time.monotonic", "time.sleep"}),
}
_HELPER_SUBPROCESS_ARGUMENTS = {
    "_assert_no_panopticon_tmux_server": "['tmux', '-L', 'panopticon', 'has-session']",
    "_capture_pane": (
        "['tmux', '-L', 'panopticon', 'capture-pane', '-p', '-J', *history, '-t', session]"
    ),
    "_client_sessions": "['tmux', '-L', 'panopticon', 'list-clients', '-F', '#{client_session}']",
    "_pane_id": (
        "['tmux', '-L', 'panopticon', 'display-message', '-p', '-t', session, '#{pane_id}']"
    ),
    "_pane_pid": (
        "['tmux', '-L', 'panopticon', 'display-message', '-p', '-t', session, '#{pane_pid}']"
    ),
    "_run": "argv",
}
_PROTECTED_POST_SETUP_NAMES = frozenset(
    {
        "Path",
        "_advance_command",
        "_api_get",
        "_assert_no_panopticon_tmux_server",
        "_assert_task_remains_user_gated",
        "_capture_pane",
        "_client_sessions",
        "_failure_diagnostics",
        "_github_get",
        "_pane_id",
        "_pane_pid",
        "_redacted_tail",
        "_run",
        "_send_user_approval_while_gated",
        "_send_session_switch_while_attached",
        "_send_opener_key_while_unopened",
        "_wait_for_client_session",
        "_wait_for_pane_row_texts",
        "_wait_for_pane_text",
        "_wait_until",
        "client",
        "config",
        "driver",
        "httpx",
        "mcp",
        "seed_task",
        "subprocess",
        "task_fixture",
        "transport",
        "walkthrough",
    }
)
_PROTECTED_HELPER_NAMES = frozenset(
    name for name in _PROTECTED_POST_SETUP_NAMES if name.startswith("_")
)
_PROTECTED_PRE_SETUP_NAMES = _PROTECTED_HELPER_NAMES | frozenset(
    name for name in _ALLOWED_POST_SETUP_CALLS if "." not in name
)


def _call_path(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_path(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _literal_argv(node: ast.Call) -> tuple[str, ...] | None:
    if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
        return None
    literal_values: list[str] = []
    for value in node.args[0].elts:
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return None
        literal_values.append(value.value)
    return tuple(literal_values)


def _allowed_observation_command(call_path: str, node: ast.Call) -> bool:
    argv = _literal_argv(node)
    if not argv:
        return False
    if call_path == "_run":
        return argv in {("panopticon", "tasks"), ("panopticon", "stop")}
    if call_path != "subprocess.run":
        return True
    return (
        argv[:2] == ("docker", "ps")
        or argv == ("tmux", "-L", "panopticon", "has-session")
        or argv == ("panopticon", "tasks")
        or argv == ("panopticon", "stop")
    )


def _allowed_pre_setup_command(call_path: str, node: ast.Call) -> bool:
    if not node.args:
        return False
    argument = ast.unparse(node.args[0])
    if call_path == "_run":
        return argument in {
            "['git', 'clone', '--branch', default_branch, '--single-branch', config.repo_url, str(worktree)]",
            "['git', 'rev-parse', 'HEAD']",
            "['panopticon', '--version']",
            "['panopticon', 'doctor']",
            "['pipx', 'ensurepath']",
            "['pipx', 'runpip', 'panopticon-next', 'show', 'panopticon-next']",
            "install_argv",
        }
    if call_path == "subprocess.run":
        return argument in {
            "['docker', 'info']",
            "['docker', 'ps', '--all', '--quiet']",
            "['tmux', '-L', 'panopticon', 'has-session', '-t', session]",
            "[login_shell, '-ic', 'command -v panopticon']",
        }
    return False


def _allowed_wait_probe(node: ast.Call) -> bool:
    if len(node.args) < 2:
        return False
    probe = node.args[1]
    return isinstance(probe, ast.Lambda) or (
        isinstance(probe, ast.Name) and probe.id == "observed_merging_history"
    )


def _allowed_user_approval_call(node: ast.Call) -> bool:
    if len(node.args) != 9 or node.keywords:
        return False
    if [ast.unparse(argument) for argument in node.args[:3]] != [
        "client",
        "write_token",
        "task_id",
    ]:
        return False
    if ast.unparse(node.args[3]) not in {"'PLANNING'", "'ITERATING'"}:
        return False
    return [ast.unparse(argument) for argument in node.args[4:]] == [
        "env",
        "worktree",
        "task_session",
        "driver",
        "walkthrough.advance_commands[config.harness].encode() + b'\\r'",
    ]


def _allowed_session_switch_call(node: ast.Call) -> bool:
    if len(node.args) not in {5, 6} or node.keywords:
        return False
    if [ast.unparse(argument) for argument in node.args[:3]] != ["env", "worktree", "driver"]:
        return False
    return [ast.unparse(argument) for argument in node.args[3:]] in (
        ["'dashboard'", "walkthrough.attach_key"],
        ["task_session", "walkthrough.detach_keys"],
        ["'dashboard'", "walkthrough.attach_key", "True"],
        ["task_session", "walkthrough.detach_keys", "True"],
    )


def _allowed_opener_key_call(node: ast.Call) -> bool:
    if len(node.args) != 3 or node.keywords:
        return False
    return [ast.unparse(argument) for argument in node.args] in (
        ["artifact_log", "driver", "walkthrough.artifact_key"],
        ["browser_log", "driver", "walkthrough.pull_request_key"],
    )


def _allowed_post_setup_consumer(call_path: str, node: ast.Call) -> bool:
    """Permit only iterator consumers whose executable body is visible at the call site."""
    if node.keywords or not node.args:
        return False
    argument = node.args[0]
    if call_path == "next":
        return isinstance(argument, ast.GeneratorExp) and (
            len(node.args) == 1
            or (
                len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value is None
            )
        )
    if call_path == "any":
        return len(node.args) == 1 and isinstance(argument, ast.GeneratorExp)
    if call_path == "dict":
        return len(node.args) == 1 and ast.unparse(argument) == "env"
    if call_path == "sorted":
        return len(node.args) == 1 and ast.unparse(argument) == "workflow_names"
    return True


def _allowed_post_setup_loop(node: ast.For | ast.AsyncFor) -> bool:
    """Allow the one fixed-name cleanup loop; arbitrary iterators remain forbidden."""
    return (
        isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "name"
        and isinstance(node.iter, ast.Tuple)
        and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in node.iter.elts
        )
        and [item.value for item in node.iter.elts if isinstance(item, ast.Constant)]
        == [
            "PANOPTICON_SERVICE_AUTH_FILE",
            "PANOPTICON_SERVICE_AUTH_MODE",
            "PANOPTICON_SERVICE_AUTH_TOKEN",
        ]
    )


def _direct_task_mutation_call(node: ast.Call) -> bool:
    """Recognize direct mutation channels even when code is placed before the setup boundary."""
    call_path = _call_path(node.func)
    argv = _literal_argv(node)
    if call_path in {"_run", "subprocess.run"} and argv and argv[0] == "curl":
        return True
    if call_path.rsplit(".", 1)[-1] in {"delete", "patch", "post", "put"}:
        return True
    if call_path.rsplit(".", 1)[-1] == "request":
        return not (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).upper() in {"GET", "HEAD"}
        )
    return (
        call_path.startswith(("mcp.", "task_fixture."))
        or call_path == "transport.call_tool"
        or call_path in {"change_state", "seed_task"}
    )


def _direct_task_mutation_reference(node: ast.expr) -> bool:
    """Recognize references that could hide a mutation behind an arbitrary local alias."""
    call_path = _call_path(node)
    return (
        call_path.rsplit(".", 1)[-1] in {"delete", "patch", "post", "put", "request"}
        or call_path.startswith(("mcp.", "task_fixture."))
        or call_path == "transport.call_tool"
        or call_path in {"change_state", "seed_task"}
    )


def _pre_setup_deferred_expression_is_immediate(
    node: ast.Lambda | ast.GeneratorExp, parents: Mapping[ast.AST, ast.AST]
) -> bool:
    """Allow only deferred expressions consumed synchronously at their definition site."""
    parent = parents.get(node)
    if not isinstance(parent, ast.Call) or node not in parent.args:
        return False
    call_path = _call_path(parent.func)
    return (isinstance(node, ast.Lambda) and call_path == "_wait_until") or (
        isinstance(node, ast.GeneratorExp) and call_path == "next"
    )


def _observer_helper_violations(tree: ast.Module) -> list[str]:
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    violations: list[str] = []
    for helper_name, allowed_calls in _OBSERVER_HELPER_CALLS.items():
        helper = functions.get(helper_name)
        if helper is None:
            continue
        for node in ast.walk(helper):
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Call):
                call_path = _call_path(node.func)
                if call_path not in allowed_calls:
                    violations.append(
                        f"line {line}: observer helper {helper_name} calls "
                        f"{call_path or '<dynamic>'}"
                    )
                if call_path == "subprocess.run" and (
                    not node.args
                    or ast.unparse(node.args[0]) != _HELPER_SUBPROCESS_ARGUMENTS.get(helper_name)
                ):
                    violations.append(
                        f"line {line}: observer helper {helper_name} runs an unreviewed command"
                    )
                if (
                    helper_name
                    in {
                        "_complete_setup_task",
                        "_send_opener_key_while_unopened",
                        "_send_session_switch_while_attached",
                        "_send_user_approval_while_gated",
                    }
                    and call_path == "driver.send"
                    and (
                        not node.args
                        or (
                            helper_name == "_complete_setup_task"
                            and ast.unparse(node.args[0]) not in _ALLOWED_SETUP_DRIVER_INPUTS
                        )
                        or (
                            helper_name == "_send_user_approval_while_gated"
                            and ast.unparse(node.args[0]) != "command"
                        )
                        or (
                            helper_name == "_send_session_switch_while_attached"
                            and ast.unparse(node.args[0])
                            not in {"keys", "_SESSION_SWITCH_PROBE_KEYS"}
                        )
                        or (
                            helper_name == "_send_opener_key_while_unopened"
                            and ast.unparse(node.args[0]) != "key"
                        )
                    )
                ):
                    violations.append(f"line {line}: helper sends an unreviewed input")
            if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                violations.append(
                    f"line {line}: observer helper {helper_name} writes external state"
                )
    return violations


def _post_setup_direct_mutations(source: str) -> list[str]:
    """Reject every post-setup call or state write outside the reviewed UI/read-only surface."""
    tree = ast.parse(source)
    normalized_body: list[ast.stmt] = []
    digest_assignment_count = 0
    digest_assignment_is_literal = False
    for module_node in tree.body:
        is_digest_assignment = isinstance(module_node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_ACCEPTANCE_SOURCE_AST_SHA256"
            for target in module_node.targets
        )
        if not is_digest_assignment:
            normalized_body.append(module_node)
            continue
        assert isinstance(module_node, ast.Assign)
        digest_assignment_count += 1
        digest_assignment_is_literal = isinstance(module_node.value, ast.Constant) and isinstance(
            module_node.value.value, str
        )
        if digest_assignment_is_literal:
            normalized_body.append(
                ast.Assign(
                    targets=module_node.targets,
                    value=ast.Constant(value="<normalized-digest>"),
                )
            )
        else:
            normalized_body.append(module_node)
    normalized_tree = ast.Module(body=normalized_body, type_ignores=[])
    source_digest = hashlib.sha256(
        ast.dump(normalized_tree, include_attributes=False).encode()
    ).hexdigest()
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_run_live_new_user_journey"
    ]
    if len(functions) != 1:
        return ["acceptance source must define exactly one _run_live_new_user_journey"]
    source_digest_violations = []
    if digest_assignment_count != 1 or not digest_assignment_is_literal:
        source_digest_violations.append(
            "acceptance source digest must be assigned exactly once as a string literal"
        )
    if source_digest != _ACCEPTANCE_SOURCE_AST_SHA256:
        source_digest_violations.append(
            "acceptance source changed; review the complete executable surface and update its pinned AST digest"
        )
    function = functions[0]
    setup_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _call_path(node.func) == "_complete_setup_task"
    ]
    if len(setup_calls) != 1:
        return ["acceptance driver must have exactly one _complete_setup_task phase boundary"]
    phase_end = (
        setup_calls[0].end_lineno or setup_calls[0].lineno,
        setup_calls[0].end_col_offset or setup_calls[0].col_offset,
    )
    violations = source_digest_violations + _observer_helper_violations(tree)
    parents = {
        child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(function):
        line = getattr(node, "lineno", 0)
        before_or_at_setup = (line, getattr(node, "col_offset", 0)) <= phase_end
        if isinstance(node, ast.Call) and _direct_task_mutation_call(node):
            violations.append(f"line {line}: direct mutation call {_call_path(node.func)}")
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            hidden_references = (
                [
                    candidate
                    for candidate in ast.walk(value)
                    if isinstance(candidate, (ast.Name, ast.Attribute))
                    and _direct_task_mutation_reference(candidate)
                ]
                if value is not None
                else []
            )
            if hidden_references:
                violations.append(
                    f"line {line}: mutation-capable reference cannot be assigned or aliased"
                )
        if before_or_at_setup and node is not function:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                violations.append(f"line {line}: pre-setup deferred executable scope")
            if isinstance(node, (ast.Lambda, ast.GeneratorExp)) and not (
                _pre_setup_deferred_expression_is_immediate(node, parents)
            ):
                violations.append(f"line {line}: pre-setup deferred expression can escape")
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and (
                node.id in _PROTECTED_PRE_SETUP_NAMES
                or (not before_or_at_setup and node.id in _PROTECTED_POST_SETUP_NAMES)
            )
        ):
            violations.append(f"line {line}: protected name rebind {node.id}")
        if before_or_at_setup:
            if isinstance(node, ast.Call):
                call_path = _call_path(node.func)
                if (
                    call_path not in _ALLOWED_PRE_SETUP_CALLS
                    or (
                        call_path in {"_run", "subprocess.run"}
                        and not _allowed_pre_setup_command(call_path, node)
                    )
                    or (
                        call_path == "driver.send"
                        and (not node.args or ast.unparse(node.args[0]) != "b'\\r'")
                    )
                    or (call_path == "next" and not _allowed_post_setup_consumer(call_path, node))
                ):
                    violations.append(
                        f"line {line}: unreviewed pre-setup call {call_path or '<dynamic>'}"
                    )
            continue
        if isinstance(node, (ast.For, ast.AsyncFor)) and not _allowed_post_setup_loop(node):
            violations.append(f"line {line}: post-setup loop can execute a deferred iterator")
        if isinstance(node, ast.Call):
            call_path = _call_path(node.func)
            if (
                call_path not in _ALLOWED_POST_SETUP_CALLS
                or (
                    call_path in {"any", "dict", "next", "sorted"}
                    and not _allowed_post_setup_consumer(call_path, node)
                )
                or (
                    call_path in {"_run", "subprocess.run"}
                    and not _allowed_observation_command(call_path, node)
                )
                or (
                    call_path == "driver.send"
                    and (not node.args or ast.unparse(node.args[0]) not in _ALLOWED_DRIVER_INPUTS)
                )
                or (
                    call_path == "_send_user_approval_while_gated"
                    and not _allowed_user_approval_call(node)
                )
                or (
                    call_path == "_send_session_switch_while_attached"
                    and not _allowed_session_switch_call(node)
                )
                or (
                    call_path == "_send_opener_key_while_unopened"
                    and not _allowed_opener_key_call(node)
                )
                or (call_path == "_wait_until" and not _allowed_wait_probe(node))
            ):
                violations.append(f"line {line}: call {call_path or '<dynamic>'}")
        if (
            isinstance(node, (ast.Attribute, ast.Subscript))
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and _call_path(node) != "request_audit.active"
        ):
            violations.append(f"line {line}: state write {_call_path(node) or '<subscript>'}")
    return sorted(violations)


class _PtyProcess:
    """One real terminal process with a continuously drained, bounded transcript tail."""

    def __init__(self, pid: int, master_fd: int) -> None:
        self.pid = pid
        self._master_fd = master_fd
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._status: int | None = None
        self._drainer = threading.Thread(
            target=self._drain, name="acceptance-pty-drain", daemon=True
        )
        self._drainer.start()

    @classmethod
    def start(cls, argv: list[str], *, env: Mapping[str, str], cwd: Path) -> _PtyProcess:
        """Fork ``argv`` onto a fixed-size POSIX PTY; ``argv[0]`` resolves through PATH."""
        winsize = struct.pack("HHHH", _PTY_ROWS, _PTY_COLUMNS, 0, 0)
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                os.chdir(cwd)
                os.execvpe(argv[0], argv, dict(env))
            except BaseException:
                os._exit(127)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        return cls(pid, master_fd)

    def _drain(self) -> None:
        while True:
            try:
                chunk = os.read(self._master_fd, 65_536)
            except OSError as exc:
                if exc.errno in {errno.EBADF, errno.EIO}:
                    return
                raise
            if not chunk:
                return
            with self._lock:
                self._buffer.extend(chunk)
                excess = len(self._buffer) - _PTY_BUFFER_LIMIT
                if excess > 0:
                    del self._buffer[:excess]

    def send(self, data: bytes) -> None:
        os.write(self._master_fd, data)

    def tail(self) -> str:
        with self._lock:
            data = bytes(self._buffer)
        return data.decode("utf-8", errors="replace")

    def wait_for_text(self, text: str, *, timeout: float = 60) -> None:
        _wait_until(
            repr(text),
            lambda: text if text in self.tail() else None,
            timeout=timeout,
            interval=0.1,
        )

    def poll(self) -> int | None:
        if self._status is not None:
            return self._status
        waited, status = os.waitpid(self.pid, os.WNOHANG)
        if waited:
            self._status = os.waitstatus_to_exitcode(status)
        return self._status

    def wait(self, *, timeout: float = 30) -> int:
        return int(
            _wait_until(
                "the PTY child to exit",
                self.poll,
                timeout=timeout,
                interval=0.1,
                accept=lambda result: result is not None,
            )
        )

    def terminate(self) -> None:
        if self.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGTERM)
            try:
                self.wait(timeout=5)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.pid, signal.SIGKILL)
                self.wait(timeout=5)
        with contextlib.suppress(OSError):
            os.close(self._master_fd)
        self._drainer.join(timeout=1)


def _local_wheel_matches(wheel_path: Path, expected_hash: str) -> bool:
    """Hash a regular, non-symlink wheel without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(wheel_path, flags)
        with os.fdopen(descriptor, "rb") as wheel_file:
            if not stat.S_ISREG(os.fstat(wheel_file.fileno()).st_mode):
                return False
            return hashlib.file_digest(wheel_file, "sha256").hexdigest() == expected_hash
    except (OSError, ValueError):
        return False


def _walkthrough_contract_errors(contents: str) -> list[str]:
    """Return missing, duplicated, or out-of-order steps that would desync the live driver."""
    errors: list[str] = []
    release_version = re.search(r"panopticon_next-([0-9]+(?:\.[0-9]+){2})-", contents)
    digest_input = (
        contents.replace(release_version.group(1), "RELEASE_VERSION")
        if release_version is not None
        else contents
    )
    if hashlib.sha256(digest_input.encode()).hexdigest() != _WALKTHROUGH_SHA256:
        errors.append("walkthrough text changed; review the driver and update its pinned digest")
    positions: list[int] = []
    for fragment in _WALKTHROUGH_SEQUENCE:
        count = contents.count(fragment)
        if count < 1:
            errors.append(f"walkthrough is missing required step {fragment!r}")
        positions.append(contents.find(fragment))
    if all(position >= 0 for position in positions) and positions != sorted(positions):
        errors.append("walkthrough journey steps are out of order")
    for placeholder in _WALKTHROUGH_PLACEHOLDERS:
        if placeholder not in contents:
            errors.append(f"walkthrough is missing placeholder {placeholder}")
    user_walkthrough = contents.split("## Maintainer:", 1)[0]
    allowed_commands = {
        "panopticon --version",
        f"panopticon {_RELEASE_VERSION}",
        "panopticon doctor",
        "panopticon quickstart",
        "panopticon stop",
        "panopticon tasks",
    }
    shell_blocks = re.findall(r"```sh\n(.*?)```", user_walkthrough, re.DOTALL)
    allowed_shell_lines = {
        "brew install pipx",
        "sudo apt-get update",
        "sudo apt-get install --yes pipx",
        "pipx ensurepath",
        f"pipx install ./{_RELEASE_WHEEL}",
        "panopticon --version",
        "panopticon doctor",
        "cd /path/to/disposable-repo",
        "git remote get-url origin",
        "panopticon quickstart",
        "unset PANOPTICON_SERVICE_AUTH_FILE PANOPTICON_SERVICE_AUTH_MODE PANOPTICON_SERVICE_AUTH_TOKEN",
        "panopticon tasks",
        "panopticon stop",
        'test -z "$(docker ps --all --quiet --filter label=panopticon.task)"',
        "! tmux -L panopticon has-session 2>/dev/null",
    }
    executable_lines = {
        line.strip()
        for block in shell_blocks
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    unknown_shell_lines = executable_lines - allowed_shell_lines
    if unknown_shell_lines:
        errors.append(
            f"walkthrough contains unexecuted shell commands: {sorted(unknown_shell_lines)}"
        )
    documented_commands = {
        match.group(1).strip()
        for block in shell_blocks
        for line in block.splitlines()
        if (match := re.match(r"^!?\s*(panopticon(?:\s+.*)?)$", line.strip()))
    }
    documented_commands.update(
        command.strip() for command in re.findall(r"`(panopticon(?:\s+[^`]+)?)`", user_walkthrough)
    )
    unknown_commands = documented_commands - allowed_commands
    if unknown_commands:
        errors.append(
            f"walkthrough contains unexecuted Panopticon commands: {sorted(unknown_commands)}"
        )
    documented_press_keys = set(re.findall(r"(?i)\bpress(?:es)?\s+`([^`]+)`", user_walkthrough))
    unknown_press_keys = documented_press_keys - {"n", "t", "a", "p", "d", "R"}
    if unknown_press_keys:
        errors.append(
            f"walkthrough contains unexecuted dashboard keys: {sorted(unknown_press_keys)}"
        )
    allowed_environment_names = set(_REQUIRED) | {
        "PANOPTICON_SERVICE_AUTH_FILE",
        "PANOPTICON_SERVICE_AUTH_MODE",
        "PANOPTICON_SERVICE_AUTH_TOKEN",
    }
    unknown_environment_names = set(re.findall(r"\bPANOPTICON_[A-Z0-9_]+\b", contents)) - (
        allowed_environment_names
    )
    if unknown_environment_names:
        errors.append(
            f"walkthrough contains unsupported placeholders: {sorted(unknown_environment_names)}"
        )
    unknown_angle_placeholders = set(re.findall(r"<[^>\n]+>", contents)) - {
        "<reviewed 40-character default-branch SHA>",
        "<reviewed-wheel-sha256>",
    }
    if unknown_angle_placeholders:
        errors.append(
            f"walkthrough contains unsupported placeholders: {sorted(unknown_angle_placeholders)}"
        )
    return errors


def _documented_walkthrough(contents: str) -> DocumentedWalkthrough:
    errors = _walkthrough_contract_errors(contents)
    if errors:
        raise ValueError("; ".join(errors))
    user_walkthrough = contents.split("## Maintainer:", 1)[0]
    install_lines = re.findall(
        r"(?m)^(pipx install \./panopticon_next-([0-9]+(?:\.[0-9]+){2})-py3-none-any\.whl)$",
        user_walkthrough,
    )
    if len(install_lines) != 1:
        raise ValueError("walkthrough must define one exact versioned install command")
    quickstart_lines = re.findall(r"(?m)^panopticon quickstart(?:[ \t].*)?$", user_walkthrough)
    if quickstart_lines != ["panopticon quickstart"]:
        raise ValueError("walkthrough must define one exact quickstart command")
    prompt_match = re.search(r"Enter `([^`]+)` and press Enter", user_walkthrough, re.DOTALL)
    if prompt_match is None:
        raise ValueError("walkthrough must define the task prompt")
    task_prompt = " ".join(prompt_match.group(1).split())
    advance_commands = dict(
        re.findall(r"^[ \t]*\| (Claude|Codex) \| `([^`]+)` \|$", user_walkthrough, re.MULTILINE)
    )
    if set(advance_commands) != {"Claude", "Codex"}:
        raise ValueError("walkthrough must define Claude and Codex approval commands")

    key_patterns = {
        "new_task_key": r"From the dashboard:\s+1\. Press `([^`]+)`\.",
        "attach_key": r"Highlight the task and press `([^`]+)` to attach",
        "artifact_key": r"highlight the task, press `([^`]+)`, select `plan\.md`",
        "pull_request_key": r"highlight it and press `([^`]+)` to open the pull request",
    }
    parsed_keys: dict[str, bytes] = {}
    for name, pattern in key_patterns.items():
        matches = re.findall(pattern, user_walkthrough)
        if len(matches) != 1 or len(matches[0]) != 1:
            raise ValueError(f"walkthrough must define one single-key {name}")
        parsed_keys[name] = matches[0].encode()
    detach_labels = set(re.findall(r"[Dd]etach with `([^`]+)`", user_walkthrough))
    if detach_labels != {"Ctrl-b d"}:
        raise ValueError("walkthrough must consistently define tmux detach as Ctrl-b d")
    return DocumentedWalkthrough(
        install_version=install_lines[0][1],
        install_argv=tuple(shlex.split(install_lines[0][0])),
        quickstart_argv=tuple(shlex.split(quickstart_lines[0])),
        task_prompt=task_prompt,
        advance_commands={name.lower(): command for name, command in advance_commands.items()},
        new_task_key=parsed_keys["new_task_key"],
        attach_key=parsed_keys["attach_key"],
        artifact_key=parsed_keys["artifact_key"],
        pull_request_key=parsed_keys["pull_request_key"],
        detach_keys=b"\x02d",
        submit_key=b"\r",
        next_choice_key=b"\x1b[B",
        previous_row_key=b"\x1b[A",
    )


def _configuration(environ: Mapping[str, str]) -> LiveConfiguration | None:
    """Return a live configuration only after every destructive/network gate is explicit."""
    if environ.get("PANOPTICON_NEW_USER_ACCEPTANCE") != _OPT_IN:
        return None
    if any(not environ.get(name) for name in _REQUIRED):
        return None

    install_spec = environ["PANOPTICON_ACCEPTANCE_INSTALL_SPEC"]
    local_wheel = _HASHED_LOCAL_WHEEL.fullmatch(install_spec)
    if local_wheel is None:
        return None
    wheel_url = urlsplit(install_spec.split(" @ ", 1)[1])
    wheel_path = Path(unquote(wheel_url.path))
    if (
        wheel_url.scheme != "file"
        or wheel_url.netloc
        or wheel_url.username is not None
        or wheel_url.password is not None
        or wheel_url.query
        or re.fullmatch(r"sha256=[0-9a-f]{64}", wheel_url.fragment, re.IGNORECASE) is None
        or not wheel_path.is_absolute()
        or wheel_path.is_symlink()
    ):
        return None
    expected_hash = wheel_url.fragment.partition("=")[2].lower()
    if not _local_wheel_matches(wheel_path, expected_hash):
        return None
    repo_url = environ["PANOPTICON_ACCEPTANCE_GITHUB_REPO"]
    parsed = urlsplit(repo_url)
    repo_parts = parsed.path.removesuffix(".git").strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or len(repo_parts) != 2
        or not all(repo_parts)
        or not repo_parts[1].startswith("panopticon-acceptance-")
    ):
        return None
    base_sha = environ["PANOPTICON_ACCEPTANCE_BASE_SHA"].lower()
    if _SHA.fullmatch(base_sha) is None:
        return None
    harness = environ["PANOPTICON_ACCEPTANCE_HARNESS"]
    auth_env = environ["PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV"]
    if auth_env not in _HARNESS_AUTH_ENV.get(harness, frozenset()):
        return None
    github_token = environ["PANOPTICON_ACCEPTANCE_GH_TOKEN"]
    harness_token = environ["PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN"]
    if any(any(char.isspace() for char in value) for value in (github_token, harness_token)):
        return None
    return LiveConfiguration(
        install_spec=install_spec,
        repo_url=repo_url,
        base_sha=base_sha,
        github_token=github_token,
        harness=harness,
        harness_auth_env=auth_env,
        harness_auth_token=harness_token,
    )


def _assert_installed_walkthrough_version(
    package_version: str, cli_version: str, walkthrough: DocumentedWalkthrough
) -> None:
    assert package_version == walkthrough.install_version, (
        "the installed acceptance artifact must match the version in the user walkthrough"
    )
    assert cli_version == f"panopticon {walkthrough.install_version}"


def _documented_local_wheel(
    install_spec: str, walkthrough: DocumentedWalkthrough
) -> tuple[Path, list[str]]:
    wheel_url = urlsplit(install_spec.split(" @ ", 1)[1])
    wheel_path = Path(unquote(wheel_url.path))
    documented_argv = list(walkthrough.install_argv)
    assert documented_argv[:2] == ["pipx", "install"]
    assert documented_argv[2] == f"./{wheel_path.name}", (
        "the supplied acceptance wheel must be the artifact named in the walkthrough"
    )
    return wheel_path, documented_argv


_LIVE_CONFIGURATION = _configuration(os.environ)


def _run(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )


def _assert_expected_origin(worktree: Path, env: Mapping[str, str], expected: str) -> None:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=worktree,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, "the clean acceptance worktree must have an origin remote"
    assert result.stdout.strip() == expected, (
        "the clean acceptance worktree must have the documented disposable repository as origin"
    )


def _assert_no_legacy_worktree_state(worktree: Path) -> None:
    present = [name for name in _LEGACY_WORKTREE_STATE if (worktree / name).exists()]
    assert present == [], f"disposable worktree contains migratable Panopticon state: {present}"


def _assert_no_panopticon_tmux_server(env: Mapping[str, str], cwd: Path) -> None:
    session_probe = subprocess.run(
        ["tmux", "-L", "panopticon", "has-session"],
        cwd=cwd,
        env=dict(env),
        capture_output=True,
    )
    socket_path = Path("/tmp") / f"tmux-{os.getuid()}" / "panopticon"
    assert session_probe.returncode != 0, "the dedicated Panopticon tmux server has a session"
    assert not socket_path.exists(), "the dedicated Panopticon tmux server is still running"


def _wait_until(
    description: str,
    probe: Any,
    *,
    timeout: float = 1_200,
    interval: float = 2,
    accept: Any = bool,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if accept(last):
            return last
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {description}; last observation: {last!r}")


def _github_get(config: LiveConfiguration, path: str) -> Any:
    """Read GitHub state; the live driver has no direct forge mutation primitive."""
    response = httpx.get(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        trust_env=False,
    )
    assert response.status_code == 200, (
        f"GitHub GET {path} returned {response.status_code}: {response.text[:500]}"
    )
    return response.json() if response.content else None


def _github_status(config: LiveConfiguration, path: str) -> int:
    """Read only the status for a GitHub resource whose absence is part of the precondition."""
    response = httpx.get(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        trust_env=False,
    )
    assert response.status_code in {200, 404}, (
        f"GitHub GET {path} returned {response.status_code}: {response.text[:500]}"
    )
    return response.status_code


def _api_get(client: httpx.Client, token: str, path: str) -> Any:
    """Read task-service state; all live mutations must enter through the shipped UI."""
    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    return response.json() if response.content else None


def _assert_task_remains_user_gated(
    client: httpx.Client,
    write_token: str,
    task_id: str,
    state: str,
    *,
    duration: float = 2.0,
) -> None:
    """Prove a user-owned state does not advance before the operator acts."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        observed = _api_get(client, write_token, f"/tasks/{task_id}")
        assert observed["state"] == state
        assert observed["turn"] == "user"
        time.sleep(0.1)


def _send_user_approval_while_gated(
    client: httpx.Client,
    write_token: str,
    task_id: str,
    state: str,
    env: Mapping[str, str],
    cwd: Path,
    task_session: str,
    driver: _PtyProcess,
    command: bytes,
    *,
    observation_window: float = 2.0,
) -> None:
    """Continuously reject an automatic transition until the attached input is delivered."""
    command_text = command.decode().strip()
    pane_before = _capture_pane(env, cwd, task_session, scrollback=True)
    command_count_before = pane_before.count(command_text)
    input_delivered = threading.Event()
    monitor_ready = threading.Event()
    failures: list[str] = []

    def monitor_gate() -> None:
        monitor_ready.set()
        while not input_delivered.is_set():
            try:
                observed = _api_get(client, write_token, f"/tasks/{task_id}")
            except BaseException as exc:
                failures.append(f"approval gate observation failed before input: {exc!r}")
                return
            if observed["state"] != state or observed["turn"] != "user":
                failures.append(
                    f"task advanced before attached approval input: "
                    f"{observed['state']}/{observed['turn']}"
                )
                return
            time.sleep(0.01)

    monitor_thread = threading.Thread(target=monitor_gate, daemon=True)
    monitor_thread.start()
    assert monitor_ready.wait(timeout=5)
    time.sleep(observation_window)
    if failures:
        pytest.fail(failures[0])
    driver.send(command)
    input_delivered.set()
    monitor_thread.join(timeout=5)
    if failures:
        pytest.fail(failures[0])
    _wait_until(
        "the approval command to appear in the attached agent pane",
        lambda: (
            pane
            if (pane := _capture_pane(env, cwd, task_session, scrollback=True)).count(command_text)
            > command_count_before
            else None
        ),
        timeout=30,
        interval=0.1,
    )


def _capture_pane(
    env: Mapping[str, str], cwd: Path, session: str, *, scrollback: bool = False
) -> str:
    history = ["-S", "-"] if scrollback else []
    result = subprocess.run(
        ["tmux", "-L", "panopticon", "capture-pane", "-p", "-J", *history, "-t", session],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _pane_id(env: Mapping[str, str], cwd: Path, session: str) -> str:
    result = subprocess.run(
        [
            "tmux",
            "-L",
            "panopticon",
            "display-message",
            "-p",
            "-t",
            session,
            "#{pane_id}",
        ],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _pane_pid(env: Mapping[str, str], cwd: Path, session: str) -> int:
    result = subprocess.run(
        [
            "tmux",
            "-L",
            "panopticon",
            "display-message",
            "-p",
            "-t",
            session,
            "#{pane_pid}",
        ],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    return int(result.stdout.strip()) if result.returncode == 0 else 0


def _client_sessions(env: Mapping[str, str], cwd: Path) -> list[str]:
    """Return the sessions of attached clients without changing tmux state."""
    result = subprocess.run(
        ["tmux", "-L", "panopticon", "list-clients", "-F", "#{client_session}"],
        cwd=cwd,
        env=dict(env),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _send_session_switch_while_attached(
    env: Mapping[str, str],
    cwd: Path,
    driver: _PtyProcess,
    expected_session: str,
    keys: bytes,
    challenge_wrong_key: bool = False,
    *,
    observation_window: float = 1.0,
) -> None:
    """Reject a session switch before or in response to a harmless wrong input."""
    input_delivered = threading.Event()
    monitor_ready = threading.Event()
    failures: list[str] = []

    initial_sessions = _client_sessions(env, cwd)
    if initial_sessions != [expected_session]:
        pytest.fail(
            f"terminal left {expected_session!r} before documented input: {initial_sessions!r}"
        )

    def monitor_attachment() -> None:
        monitor_ready.set()
        while not input_delivered.is_set():
            observed = _client_sessions(env, cwd)
            if observed != [expected_session]:
                failures.append(
                    f"terminal left {expected_session!r} before documented input: {observed!r}"
                )
                return
            time.sleep(0.01)

    monitor_thread = threading.Thread(target=monitor_attachment, daemon=True)
    monitor_thread.start()
    assert monitor_ready.wait(timeout=5)
    if challenge_wrong_key:
        driver.send(_SESSION_SWITCH_PROBE_KEYS)
    time.sleep(observation_window)
    if failures:
        pytest.fail(failures[0])
    driver.send(keys)
    input_delivered.set()
    monitor_thread.join(timeout=5)
    if failures:
        pytest.fail(failures[0])


def _send_opener_key_while_unopened(
    log_path: Path,
    driver: _PtyProcess,
    key: bytes,
    *,
    observation_window: float = 1.0,
) -> None:
    """Reject an artifact or browser handoff that occurs before its documented key."""
    input_delivered = threading.Event()
    monitor_ready = threading.Event()
    failures: list[str] = []

    def monitor_handoff() -> None:
        monitor_ready.set()
        while not input_delivered.is_set():
            if log_path.exists():
                failures.append(f"host opener ran before documented input: {log_path}")
                return
            time.sleep(0.01)

    monitor_thread = threading.Thread(target=monitor_handoff, daemon=True)
    monitor_thread.start()
    assert monitor_ready.wait(timeout=5)
    time.sleep(observation_window)
    if failures:
        pytest.fail(failures[0])
    driver.send(key)
    input_delivered.set()
    monitor_thread.join(timeout=5)
    if failures:
        pytest.fail(failures[0])


def _wait_for_client_session(
    expected: str, *, env: Mapping[str, str], cwd: Path, timeout: float = 120
) -> None:
    _wait_until(
        f"the sole tmux client to attach to {expected!r}",
        lambda: observed if (observed := _client_sessions(env, cwd)) == [expected] else None,
        timeout=timeout,
        interval=0.1,
    )


def _wait_for_pane_text(
    session: str,
    text: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 120,
) -> str:
    return str(
        _wait_until(
            f"{text!r} in tmux session {session!r}",
            lambda: pane if text in (pane := _capture_pane(env, cwd, session)) else None,
            timeout=timeout,
            interval=0.1,
        )
    )


def _wait_for_pane_texts(
    session: str,
    texts: tuple[str, ...],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 120,
) -> str:
    def probe() -> str | None:
        pane = _capture_pane(env, cwd, session)
        return pane if all(text in pane for text in texts) else None

    return str(
        _wait_until(
            f"{texts!r} in tmux session {session!r}",
            probe,
            timeout=timeout,
            interval=0.1,
        )
    )


def _wait_for_pane_row_texts(
    session: str,
    texts: tuple[str, ...],
    *,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 120,
) -> str:
    def probe() -> str | None:
        pane = _capture_pane(env, cwd, session)
        return (
            pane if any(all(text in line for text in texts) for line in pane.splitlines()) else None
        )

    return str(
        _wait_until(
            f"one row containing {texts!r} in tmux session {session!r}",
            probe,
            timeout=timeout,
            interval=0.1,
        )
    )


def _advance_command(harness: str) -> str:
    return _ADVANCE_COMMAND[harness]


def _redacted_tail(text: str, tokens: tuple[str, ...], *, limit: int = _DIAGNOSTIC_LIMIT) -> str:
    for token in sorted(set(tokens), key=len, reverse=True):
        if token:
            text = text.replace(token, "[redacted]")
            if len(token) > 4:
                text = text.replace(f"...{token[-4:]}", "...[redacted]")
    return text[-limit:]


def _failure_diagnostics(
    driver: _PtyProcess | None,
    *,
    env: Mapping[str, str],
    cwd: Path,
    tokens: tuple[str, ...],
) -> str:
    sessions = _client_sessions(env, cwd)
    pane = _capture_pane(env, cwd, sessions[0]) if len(sessions) == 1 else ""
    pty_tail = driver.tail() if driver is not None else "<PTY not started>"
    combined = (
        f"tmux client sessions: {sessions!r}\n\ncurrent pane:\n{pane}\n\nPTY tail:\n{pty_tail}"
    )
    return _redacted_tail(combined, tokens)


def _complete_setup_task(
    setup_id: str,
    *,
    config: LiveConfiguration,
    driver: _PtyProcess,
    env: Mapping[str, str],
    cwd: Path,
    client: httpx.Client,
    write_token: str,
) -> None:
    session = f"panopticon-{setup_id}"
    _wait_for_client_session(session, env=env, cwd=cwd)
    responded: set[str] = set()
    last_pane = ""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        task = _api_get(client, write_token, f"/tasks/{setup_id}")
        if task["state"] == "COMPLETE":
            return
        # Setup is a scrolling shell, so prompt detection needs its history. Dashboard checks use
        # only the current viewport so an old Textual frame cannot satisfy a later state wait.
        last_pane = _capture_pane(env, cwd, session, scrollback=True)
        if "A Claude credential is already set" in last_pane and "claude-keep" not in responded:
            driver.send(b"\r")
            responded.add("claude-keep")
        elif "A GH_TOKEN is already set" in last_pane and "gh-keep" not in responded:
            driver.send(b"\r")
            responded.add("gh-keep")
        elif (
            "A CLAUDE_CODE_OAUTH_TOKEN is set" in last_pane
            or "A ANTHROPIC_API_KEY is set" in last_pane
        ) and "claude-adopt" not in responded:
            driver.send(b"y\r")
            responded.add("claude-adopt")
        elif (
            f"A {config.harness_auth_env} is set in your environment" in last_pane
            and "codex-adopt" not in responded
        ):
            driver.send(b"y\r")
            responded.add("codex-adopt")
        elif "Paste a Claude token to store it" in last_pane and "claude-paste" not in responded:
            driver.send(config.harness_auth_token.encode() + b"\r")
            responded.add("claude-paste")
        elif "A GH_TOKEN is set in your environment" in last_pane and "gh-adopt" not in responded:
            driver.send(b"y\r")
            responded.add("gh-adopt")
        elif "Paste a GitHub token to store it" in last_pane and "gh-paste" not in responded:
            driver.send(config.github_token.encode() + b"\r")
            responded.add("gh-paste")
        elif (
            "After updating the repo credentials, press Enter to re-check" in last_pane
            and "recheck" not in responded
        ):
            driver.send(b"\r")
            responded.add("recheck")
        elif (
            "All required task-container credentials are configured." in last_pane
            and "complete" not in responded
        ):
            driver.send(b"\r")
            responded.add("complete")
        time.sleep(0.1)
    pytest.fail(f"setup-repo did not complete; final pane:\n{last_pane[-4000:]}")


def _run_live_new_user_journey(tmp_path: Path, config: LiveConfiguration) -> None:
    walkthrough = _documented_walkthrough(_WALKTHROUGH_PATH.read_text())
    for binary in ("docker", "git", "pipx", "tmux", config.harness):
        assert shutil.which(binary), f"{binary} must be installed on the disposable host"
    other_harnesses = _REGISTERED_HARNESSES - {config.harness}
    assert not [name for name in other_harnesses if shutil.which(name)], (
        "the disposable host must expose only the selected harness so quickstart's choice is deterministic"
    )
    home_root = tmp_path / "home"
    config_root = home_root / ".config" / "panopticon"
    data_root = home_root / ".local" / "share" / "panopticon"
    cache_root = home_root / ".cache" / "panopticon"
    pipx_home = tmp_path / "pipx-home"
    pipx_bin = tmp_path / "pipx-bin"
    worktree = tmp_path / "disposable-repo"
    recorder_bin = tmp_path / "recorders"
    artifact_log = tmp_path / "xdg-open.log"
    browser_log = tmp_path / "browser.log"
    for path in (
        config_root,
        data_root,
        cache_root,
        pipx_home,
        pipx_bin,
        worktree,
        recorder_bin,
        artifact_log,
        browser_log,
    ):
        assert not path.exists()

    recorder_bin.mkdir()
    opener_script = '#!/bin/sh\nprintf \'%s\\n\' "$1" >> "$PANOPTICON_ACCEPTANCE_XDG_LOG"\n'
    for opener_name in ("open", "xdg-open"):
        opener = recorder_bin / opener_name
        opener.write_text(opener_script)
        opener.chmod(0o700)
    browser = recorder_bin / "record-browser"
    browser.write_text('#!/bin/sh\nprintf \'%s\\n\' "$1" >> "$PANOPTICON_ACCEPTANCE_BROWSER_LOG"\n')
    browser.chmod(0o700)

    env = {
        **{name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ},
        "HOME": str(home_root),
        "PIPX_HOME": str(pipx_home),
        "PIPX_BIN_DIR": str(pipx_bin),
        config.harness_auth_env: config.harness_auth_token,
        "GH_TOKEN": config.github_token,
        "BROWSER": str(browser),
        "PANOPTICON_ACCEPTANCE_BROWSER_LOG": str(browser_log),
        "PANOPTICON_ACCEPTANCE_XDG_LOG": str(artifact_log),
        "TERM": "xterm-256color",
    }
    Path(env["HOME"]).mkdir(mode=0o700)
    env["PATH"] = os.pathsep.join((str(recorder_bin), env["PATH"]))

    # Prove emptiness in the exact HOME/PATH/Docker/tmux environment the journey uses. A dedicated
    # disposable host has no unrelated containers at all; this deliberately catches unlabeled and
    # legacy Panopticon containers instead of trusting only the current label convention.
    assert subprocess.run(["docker", "info"], env=env, capture_output=True).returncode == 0
    _assert_no_panopticon_tmux_server(env, tmp_path)
    existing_containers = subprocess.run(
        ["docker", "ps", "--all", "--quiet"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not existing_containers, "the dedicated disposable host must begin with no containers"

    owner, repo = urlsplit(config.repo_url).path.removesuffix(".git").strip("/").split("/")
    repository = _github_get(config, f"/repos/{owner}/{repo}")
    assert not repository["archived"]
    assert repository.get("permissions", {}).get("push") is True
    assert repo.startswith("panopticon-acceptance-")
    assert "panopticon-acceptance-disposable" in repository.get("topics", [])
    default_branch = str(repository["default_branch"])
    branch = _github_get(config, f"/repos/{owner}/{repo}/git/ref/heads/{quote(default_branch)}")
    assert branch["object"]["sha"] == config.base_sha, (
        "the disposable repo moved; supply its current base SHA only after reviewing the new state"
    )
    assert (
        _github_status(
            config,
            f"/repos/{owner}/{repo}/contents/hello-panopticon.txt?ref={quote(config.base_sha)}",
        )
        == 404
    ), "the disposable base must not already contain hello-panopticon.txt"

    askpass = tmp_path / "git-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' x-access-token ;; "
        "*) printf '%s\\n' \"$PANOPTICON_ACCEPTANCE_GH_TOKEN\" ;; esac\n"
    )
    askpass.chmod(0o700)
    clone_env = {
        **env,
        "GIT_ASKPASS": str(askpass),
        "GIT_ASKPASS_REQUIRE": "force",
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": config.github_token,
    }
    _run(
        [
            "git",
            "clone",
            "--branch",
            default_branch,
            "--single-branch",
            config.repo_url,
            str(worktree),
        ],
        env=clone_env,
        cwd=tmp_path,
    )
    assert (
        _run(["git", "rev-parse", "HEAD"], env=env, cwd=worktree).stdout.strip() == config.base_sha
    )
    _assert_expected_origin(worktree, env, config.repo_url)
    _assert_no_legacy_worktree_state(worktree)

    assert shutil.which("panopticon", path=env["PATH"]) is None, (
        "the disposable host PATH must not already expose panopticon"
    )
    wheel_path, install_argv = _documented_local_wheel(config.install_spec, walkthrough)
    _run(install_argv, env=env, cwd=wheel_path.parent, timeout=600)
    panopticon = pipx_bin / "panopticon"
    assert panopticon.is_file()
    assert shutil.which("panopticon", path=env["PATH"]) is None
    _run(["pipx", "ensurepath"], env=env, cwd=tmp_path)
    login_shell = env.get("SHELL")
    assert login_shell and Path(login_shell).is_file(), (
        "the clean host must identify its login shell"
    )
    fresh_path = (
        subprocess.run(
            [login_shell, "-ic", "command -v panopticon"],
            env=env,
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        .stdout.strip()
        .splitlines()
    )
    assert fresh_path and fresh_path[-1] == str(panopticon)
    env["PATH"] = os.pathsep.join((str(pipx_bin), env["PATH"]))
    assert shutil.which("panopticon", path=env["PATH"]) == str(panopticon)
    version = _run(["panopticon", "--version"], env=env, cwd=tmp_path).stdout.strip()
    package = _run(
        ["pipx", "runpip", "panopticon-next", "show", "panopticon-next"],
        env=env,
        cwd=tmp_path,
    ).stdout
    package_version = next(
        line.partition(":")[2].strip()
        for line in package.splitlines()
        if line.lower().startswith("version:")
    )
    _assert_installed_walkthrough_version(package_version, version, walkthrough)
    _run(["panopticon", "doctor"], env=env, cwd=tmp_path)

    driver: _PtyProcess | None = None
    teardown_complete = False
    try:
        # 2119: REQ-054.7.2, REQ-054.7.8
        driver = _PtyProcess.start(list(walkthrough.quickstart_argv), env=env, cwd=worktree)
        secrets_file = config_root / "secrets" / "panopticon.env"
        driver.wait_for_text(
            f"Use {config.harness} as this repo's default harness? Press Enter to continue.",
            timeout=60,
        )
        driver.send(b"\r")

        auth_file = config_root / "secrets" / "task-service-auth.json"
        _wait_until(
            "quickstart's service credential file",
            lambda: auth_file if auth_file.is_file() else None,
            timeout=60,
            interval=0.1,
        )
        for session in ("service", "runner"):
            subprocess.run(
                ["tmux", "-L", "panopticon", "has-session", "-t", session],
                env=env,
                cwd=worktree,
                check=True,
                capture_output=True,
            )

        auth = json.loads(auth_file.read_text())
        write_token = auth["write"][-1]
        request_audit = _PostSetupRequestAudit()
        with httpx.Client(
            base_url="http://127.0.0.1:8000",
            timeout=30,
            trust_env=False,
            event_hooks={"request": [request_audit]},
        ) as client:
            setup = _wait_until(
                "the setup-repo task",
                lambda: next(
                    (
                        task
                        for task in _api_get(client, write_token, "/tasks")
                        if task["workflow"] == "setup-repo"
                    ),
                    None,
                ),
                timeout=120,
                interval=0.2,
            )
            setup_id = str(setup["id"])
            request_audit.active = True
            _complete_setup_task(
                setup_id,
                config=config,
                driver=driver,
                env=env,
                cwd=worktree,
                client=client,
                write_token=write_token,
            )
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            assert secrets_file.is_file() and not secrets_file.is_symlink()
            assert secrets_file.stat().st_mode & 0o077 == 0
            secret_lines = secrets_file.read_text().splitlines()
            assert f"{config.harness_auth_env}={config.harness_auth_token}" in secret_lines
            assert f"GH_TOKEN={config.github_token}" in secret_lines

            fresh_shell = dict(env)
            for name in (
                "PANOPTICON_SERVICE_AUTH_FILE",
                "PANOPTICON_SERVICE_AUTH_MODE",
                "PANOPTICON_SERVICE_AUTH_TOKEN",
            ):
                fresh_shell.pop(name, None)
            unauthenticated = client.get("/tasks")
            assert unauthenticated.status_code == 401, (
                "the task service must reject the same client request without its stored token"
            )
            invalid_client_credential = config_root / "secrets" / "acceptance-invalid-client.json"
            invalid_client_credential.write_text(
                json.dumps({"read": [], "write": ["acceptance-invalid-token"]})
            )
            invalid_client_credential.chmod(0o600)
            rejected_shell = {
                **fresh_shell,
                "PANOPTICON_SERVICE_AUTH_FILE": str(invalid_client_credential),
                "PANOPTICON_SERVICE_AUTH_MODE": "enforced",
            }
            rejected = subprocess.run(
                ["panopticon", "tasks"],
                env=rejected_shell,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert rejected.returncode != 0
            assert "401" in rejected.stdout + rejected.stderr
            completed_setup = _api_get(client, write_token, f"/tasks/{setup_id}")
            assert completed_setup["workflow"] == "setup-repo"
            assert completed_setup["state"] == "COMPLETE"
            listed = _run(["panopticon", "tasks"], env=fresh_shell, cwd=tmp_path)
            listed_rows = [line.split(maxsplit=3) for line in listed.stdout.splitlines() if line]
            assert listed_rows == [
                [
                    setup_id,
                    "COMPLETE",
                    str(completed_setup["turn"]),
                    str(completed_setup["slug"] or "-"),
                ]
            ]

            post_setup_tasks = _api_get(client, write_token, "/tasks")
            assert [str(item["id"]) for item in post_setup_tasks] == [setup_id], (
                "no coding task may exist before setup-repo completes"
            )
            assert post_setup_tasks[0]["workflow"] == "setup-repo"

            marker = "hello-panopticon.txt"
            content = "hello from Panopticon\n"
            prompt = walkthrough.task_prompt
            repos = _api_get(client, write_token, "/repos")
            assert len(repos) == 1, "the fresh quickstart should register exactly its current repo"
            configured_repo = next(
                item for item in repos if item["git_url"].rstrip("/") == config.repo_url.rstrip("/")
            )
            before_ids = {str(task["id"]) for task in _api_get(client, write_token, "/tasks")}
            workflow_infos = _api_get(
                client, write_token, f"/repos/{configured_repo['id']}/workflows"
            )
            workflow_names = [str(item["name"]) for item in workflow_infos]
            assert workflow_names == sorted(workflow_names)
            workflow_index = workflow_names.index("github-self-reviewed")

            # 2119: REQ-054.7.6, REQ-054.7.7
            _wait_for_pane_text("dashboard", "New task", env=env, cwd=worktree)
            dashboard_before_picker = _capture_pane(env, worktree, "dashboard")
            driver.send(walkthrough.new_task_key)
            _wait_until(
                "the repository picker modal",
                lambda: (
                    pane
                    if (pane := _capture_pane(env, worktree, "dashboard"))
                    != dashboard_before_picker
                    and str(configured_repo["id"]) in pane
                    and re.search(r"(?m)^\s*[│┃]?\s*repo\s*[│┃]?\s*$", pane)
                    else None
                ),
                timeout=30,
                interval=0.1,
            )
            driver.send(walkthrough.submit_key)
            _wait_for_pane_text("dashboard", "github-self-reviewed", env=env, cwd=worktree)
            driver.send(walkthrough.next_choice_key * workflow_index + walkthrough.submit_key)
            _wait_for_pane_text("dashboard", "enter: submit", env=env, cwd=worktree)
            driver.send(prompt.encode() + walkthrough.submit_key)

            task = _wait_until(
                "the sole dashboard-created task",
                lambda: (
                    created[0]
                    if len(
                        created := [
                            item
                            for item in _api_get(client, write_token, "/tasks")
                            if str(item["id"]) not in before_ids
                        ]
                    )
                    == 1
                    else None
                ),
                timeout=120,
                interval=0.2,
            )
            task_id = str(task["id"])
            assert task["workflow"] == "github-self-reviewed"
            assert task["memo"] == prompt
            assert task["harness"] == config.harness

            live = _wait_until(
                "a live task container",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))[
                        "container_status"
                    ]
                    == "live"
                    else None
                ),
                timeout=600,
            )
            assert live["state"] == "PLANNING"
            _wait_for_pane_row_texts("dashboard", (marker, "live"), env=env, cwd=worktree)

            # The completed setup row remains selected when the new active row appears above it.
            # Move to the new task, attach through `t`, then issue tmux's raw detach chord.
            # 2119: REQ-054.7.8
            _wait_for_pane_text("dashboard", marker, env=env, cwd=worktree)
            dashboard_pane = _pane_id(env, worktree, "dashboard")
            dashboard_pid = _pane_pid(env, worktree, "dashboard")
            assert dashboard_pane
            assert dashboard_pid
            driver.send(walkthrough.previous_row_key)
            time.sleep(0.2)
            _send_session_switch_while_attached(
                env, worktree, driver, "dashboard", walkthrough.attach_key
            )
            task_session = f"panopticon-{task_id}"
            _wait_for_client_session(task_session, env=env, cwd=worktree)
            assert _pane_pid(env, worktree, "dashboard") == dashboard_pid
            assert _wait_until(
                "a rendered task pane after attachment",
                lambda: _capture_pane(env, worktree, task_session).strip() or None,
                timeout=30,
                interval=0.1,
            )
            _send_session_switch_while_attached(
                env, worktree, driver, task_session, walkthrough.detach_keys
            )
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            assert _pane_id(env, worktree, "dashboard") == dashboard_pane
            assert _pane_pid(env, worktree, "dashboard") == dashboard_pid

            # The first round above proves both documented inputs work without a hidden precursor.
            # Repeat the round with an inert NUL challenge and prove that challenge does not switch
            # either direction before the documented key is delivered.
            _send_session_switch_while_attached(
                env, worktree, driver, "dashboard", walkthrough.attach_key, True
            )
            _wait_for_client_session(task_session, env=env, cwd=worktree)
            _send_session_switch_while_attached(
                env, worktree, driver, task_session, walkthrough.detach_keys, True
            )
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            assert _pane_id(env, worktree, "dashboard") == dashboard_pane
            assert _pane_pid(env, worktree, "dashboard") == dashboard_pid

            planned = _wait_until(
                "the agent's plan and planning handoff",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["turn"]
                    == "user"
                    and "plan.md" in _api_get(client, write_token, f"/tasks/{task_id}/artifacts")
                    else None
                ),
            )
            plan_response = client.get(
                f"/tasks/{task_id}/artifacts/plan.md",
                headers={"Authorization": f"Bearer {write_token}"},
            )
            plan_response.raise_for_status()
            assert plan_response.text.strip()
            assert marker in plan_response.text
            assert content.strip() in plan_response.text
            assert planned["state"] == "PLANNING"
            _assert_task_remains_user_gated(client, write_token, task_id, "PLANNING")

            # 2119: REQ-054.7.9
            artifact_names = _api_get(client, write_token, f"/tasks/{task_id}/artifacts")
            visible_artifacts = [name for name in artifact_names if not str(name).startswith(".")]
            plan_index = visible_artifacts.index("plan.md")
            _send_opener_key_while_unopened(artifact_log, driver, walkthrough.artifact_key)
            _wait_for_pane_text("dashboard", "plan.md", env=env, cwd=worktree)
            driver.send(walkthrough.next_choice_key * plan_index + walkthrough.submit_key)
            opened_artifact = Path(
                _wait_until(
                    "the xdg-open artifact handoff",
                    lambda: (
                        lines[0]
                        if artifact_log.is_file()
                        and len(lines := artifact_log.read_text().splitlines()) == 1
                        else None
                    ),
                    timeout=30,
                    interval=0.1,
                )
            )
            assert opened_artifact.name == "plan.md"
            assert opened_artifact.parent.name == task_id
            assert opened_artifact == data_root / "artifacts" / task_id / "plan.md"
            assert opened_artifact.read_bytes() == plan_response.content

            # 2119: REQ-054.7.6, REQ-054.7.7
            driver.send(walkthrough.attach_key)
            _wait_for_client_session(task_session, env=env, cwd=worktree)
            planning_gate = _api_get(client, write_token, f"/tasks/{task_id}")
            assert planning_gate["state"] == "PLANNING" and planning_gate["turn"] == "user"
            _send_user_approval_while_gated(
                client,
                write_token,
                task_id,
                "PLANNING",
                env,
                worktree,
                task_session,
                driver,
                walkthrough.advance_commands[config.harness].encode() + b"\r",
            )
            advanced = _wait_until(
                "the harness command to advance the task to ITERATING",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["state"]
                    == "ITERATING"
                    else None
                ),
            )
            assert advanced["state"] == "ITERATING"
            iterating_entries = [
                entry for entry in advanced["history"] if entry["to_state"] == "ITERATING"
            ]
            assert len(iterating_entries) == 1
            assert iterating_entries[0]["trigger"] == "advance"
            if _client_sessions(env, worktree) == [task_session]:
                driver.send(walkthrough.detach_keys)
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            _wait_for_pane_row_texts("dashboard", (marker, "ITERATING"), env=env, cwd=worktree)

            reviewable = _wait_until(
                "a reviewable pull request and ITERATING handoff",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["state"]
                    == "ITERATING"
                    and observed["turn"] == "user"
                    and observed.get("url")
                    else None
                ),
            )
            pr_match = re.fullmatch(
                rf"https://github\.com/{re.escape(owner)}/{re.escape(repo)}/pull/([0-9]+)",
                str(reviewable["url"]),
                re.IGNORECASE,
            )
            assert pr_match is not None
            pr_number = int(pr_match.group(1))

            # 2119: REQ-054.7.9
            _send_opener_key_while_unopened(browser_log, driver, walkthrough.pull_request_key)
            opened_url = _wait_until(
                "the BROWSER pull-request handoff",
                lambda: (
                    lines[0]
                    if browser_log.is_file()
                    and len(lines := browser_log.read_text().splitlines()) == 1
                    else None
                ),
                timeout=30,
                interval=0.1,
            )
            assert opened_url == reviewable["url"]

            changed_files = _github_get(config, f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
            assert [item["filename"] for item in changed_files] == [marker]
            assert changed_files[0]["status"] == "added"
            pull = _github_get(config, f"/repos/{owner}/{repo}/pulls/{pr_number}")
            blob = _github_get(
                config,
                f"/repos/{owner}/{repo}/contents/{quote(marker)}?ref={pull['head']['sha']}",
            )
            assert base64.b64decode(blob["content"]).decode() == content

            _assert_task_remains_user_gated(client, write_token, task_id, "ITERATING")

            # 2119: REQ-054.7.6, REQ-054.7.7
            driver.send(walkthrough.attach_key)
            _wait_for_client_session(task_session, env=env, cwd=worktree)
            iterating_gate = _api_get(client, write_token, f"/tasks/{task_id}")
            assert iterating_gate["state"] == "ITERATING" and iterating_gate["turn"] == "user"
            _send_user_approval_while_gated(
                client,
                write_token,
                task_id,
                "ITERATING",
                env,
                worktree,
                task_session,
                driver,
                walkthrough.advance_commands[config.harness].encode() + b"\r",
            )

            def observed_merging_history() -> Any:
                observed = _api_get(client, write_token, f"/tasks/{task_id}")
                return (
                    observed
                    if any(entry["to_state"] == "MERGING" for entry in observed["history"])
                    else None
                )

            merging = _wait_until(
                "a read-side history observation of the MERGING transition",
                observed_merging_history,
            )
            merging_entry = next(
                entry for entry in merging["history"] if entry["to_state"] == "MERGING"
            )
            assert merging_entry["trigger"] == "advance"
            if _client_sessions(env, worktree) == [task_session]:
                driver.send(walkthrough.detach_keys)

            complete = _wait_until(
                "the merged pull request and COMPLETE task",
                lambda: (
                    observed
                    if (observed := _api_get(client, write_token, f"/tasks/{task_id}"))["state"]
                    == "COMPLETE"
                    else None
                ),
            )
            assert complete["container_status"] == "–"
            _wait_for_client_session("dashboard", env=env, cwd=worktree)
            assert _pane_id(env, worktree, "dashboard") == dashboard_pane
            _wait_for_pane_row_texts("dashboard", (marker, "COMPLETE"), env=env, cwd=worktree)
            merged_pull = _github_get(config, f"/repos/{owner}/{repo}/pulls/{pr_number}")
            assert merged_pull["merged_at"] is not None
            merged_blob = _github_get(
                config,
                f"/repos/{owner}/{repo}/contents/{quote(marker)}?ref={quote(default_branch)}",
            )
            assert base64.b64decode(merged_blob["content"]).decode() == content

            # Follow the documented teardown from a second shell while the dashboard is still
            # attached, then prove both the first stop and its idempotent repeat succeed.
            _run(["panopticon", "stop"], env=env, cwd=tmp_path)
            assert driver.wait(timeout=30) is not None
            assert not subprocess.run(
                ["docker", "ps", "--all", "--quiet"],
                env=env,
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert (
                subprocess.run(
                    ["tmux", "-L", "panopticon", "has-session"],
                    env=env,
                    cwd=tmp_path,
                    capture_output=True,
                ).returncode
                != 0
            )
            _run(["panopticon", "stop"], env=env, cwd=tmp_path)
            assert not subprocess.run(
                ["docker", "ps", "--all", "--quiet"],
                env=env,
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert (
                subprocess.run(
                    ["tmux", "-L", "panopticon", "has-session"],
                    env=env,
                    cwd=tmp_path,
                    capture_output=True,
                ).returncode
                != 0
            )
            teardown_complete = True
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        diagnostic = _failure_diagnostics(
            driver,
            env=env,
            cwd=worktree,
            tokens=(config.github_token, config.harness_auth_token),
        )
        message = _redacted_tail(
            f"live new-user journey failed: {exc}\n\n{diagnostic}",
            (config.github_token, config.harness_auth_token),
        )
        raise AssertionError(message) from exc
    finally:
        if not teardown_complete:
            subprocess.run(["panopticon", "stop"], env=env, cwd=tmp_path, capture_output=True)
        if driver is not None:
            driver.terminate()

    assert not subprocess.run(
        ["docker", "ps", "--all", "--quiet"],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _assert_no_panopticon_tmux_server(env, tmp_path)


def _valid_local_install_spec(tmp_path: Path) -> str:
    wheel = tmp_path / _RELEASE_WHEEL
    wheel.write_bytes(b"reviewed evaluator wheel")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return f"panopticon-next @ {wheel.as_uri()}#sha256={digest}"


def test_complete_configuration_enables_clean_host_acceptance(tmp_path: Path) -> None:
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": _valid_local_install_spec(tmp_path),
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }
    assert _configuration(values) is not None


def test_clean_worktree_precondition_rejects_missing_origin(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(AssertionError, match="must have an origin remote"):
        _assert_expected_origin(tmp_path, os.environ, "https://github.com/acme/repo.git")


@pytest.mark.parametrize("legacy_name", _LEGACY_WORKTREE_STATE)
def test_clean_worktree_precondition_rejects_legacy_state(tmp_path: Path, legacy_name: str) -> None:
    legacy_path = tmp_path / legacy_name
    if legacy_path.suffix:
        legacy_path.write_text("legacy state")
    else:
        legacy_path.mkdir()

    with pytest.raises(AssertionError, match="migratable Panopticon state"):
        _assert_no_legacy_worktree_state(tmp_path)


def test_live_driver_consumes_the_current_documented_walkthrough_contract() -> None:
    # 2119: REQ-054.7.2
    walkthrough = _documented_walkthrough(_WALKTHROUGH_PATH.read_text())

    assert walkthrough.install_version == _RELEASE_VERSION
    assert walkthrough.install_argv == (
        "pipx",
        "install",
        f"./{_RELEASE_WHEEL}",
    )
    assert walkthrough.quickstart_argv == ("panopticon", "quickstart")
    assert walkthrough.task_prompt == (
        "Add a hello-panopticon.txt file containing hello from Panopticon and do not change any "
        "other files."
    )
    assert walkthrough.advance_commands == _ADVANCE_COMMAND
    assert walkthrough.new_task_key == b"n"
    assert walkthrough.attach_key == b"t"
    assert walkthrough.artifact_key == b"a"
    assert walkthrough.pull_request_key == b"p"
    assert walkthrough.detach_keys == b"\x02d"
    assert walkthrough.submit_key == b"\r"
    assert walkthrough.next_choice_key == b"\x1b[B"
    assert walkthrough.previous_row_key == b"\x1b[A"


def test_live_install_version_is_bound_to_the_user_walkthrough() -> None:
    walkthrough = _documented_walkthrough(_WALKTHROUGH_PATH.read_text())

    with pytest.raises(AssertionError, match="installed acceptance artifact"):
        _assert_installed_walkthrough_version("1.2.3", "panopticon 1.2.3", walkthrough)


def test_live_install_command_is_bound_to_the_documented_wheel(tmp_path: Path) -> None:
    walkthrough = _documented_walkthrough(_WALKTHROUGH_PATH.read_text())
    correct = tmp_path / _RELEASE_WHEEL
    wrong = tmp_path / "panopticon_next-1.2.3-py3-none-any.whl"

    wheel_path, argv = _documented_local_wheel(
        f"panopticon-next @ {correct.as_uri()}#sha256={'a' * 64}", walkthrough
    )
    assert wheel_path == correct
    assert tuple(argv) == walkthrough.install_argv
    with pytest.raises(AssertionError, match="artifact named in the walkthrough"):
        _documented_local_wheel(
            f"panopticon-next @ {wrong.as_uri()}#sha256={'a' * 64}", walkthrough
        )


@pytest.mark.parametrize("fragment", (*_WALKTHROUGH_SEQUENCE, *_WALKTHROUGH_PLACEHOLDERS))
def test_walkthrough_contract_rejects_missing_steps_and_placeholders(fragment: str) -> None:
    contents = _WALKTHROUGH_PATH.read_text()
    assert fragment in contents

    assert _walkthrough_contract_errors(contents.replace(fragment, "<corrupted>"))


def test_walkthrough_contract_rejects_out_of_order_steps() -> None:
    contents = _WALKTHROUGH_PATH.read_text()
    first = _WALKTHROUGH_SEQUENCE[0]
    second = _WALKTHROUGH_SEQUENCE[1]
    reordered = contents.replace(first, "<second>", 1).replace(second, first, 1)
    reordered = reordered.replace("<second>", second, 1)

    assert _walkthrough_contract_errors(reordered)


@pytest.mark.parametrize(
    "replacement",
    (
        "panopticon quickstart --disable-auth\n",
        "panopticon secret-bootstrap\npanopticon quickstart\n",
    ),
)
def test_walkthrough_contract_rejects_changed_or_extra_mandatory_commands(
    replacement: str,
) -> None:
    contents = _WALKTHROUGH_PATH.read_text()
    assert contents.count("panopticon quickstart\n") == 1
    corrupted = contents.replace("panopticon quickstart\n", replacement, 1)

    assert _walkthrough_contract_errors(corrupted)
    with pytest.raises(ValueError):
        _documented_walkthrough(corrupted)


def test_walkthrough_contract_rejects_an_unexecuted_dashboard_instruction() -> None:
    contents = _WALKTHROUGH_PATH.read_text()
    corrupted = contents.replace("Press `n`.", "Press `n`. Then press `q` to quit.", 1)

    assert "unexecuted dashboard keys" in " ".join(_walkthrough_contract_errors(corrupted))
    with pytest.raises(ValueError, match="unexecuted dashboard keys"):
        _documented_walkthrough(corrupted)


@pytest.mark.parametrize(
    "addition",
    (
        "\nprintf 'required but unexecuted\\n'\n",
        "\nexport PANOPTICON_ACCEPTANCE_UNDOCUMENTED='<required>'\n",
    ),
)
def test_walkthrough_contract_rejects_unexecuted_commands_and_placeholders(
    addition: str,
) -> None:
    contents = _WALKTHROUGH_PATH.read_text()
    corrupted = contents.replace("panopticon doctor\n", f"panopticon doctor{addition}", 1)

    assert _walkthrough_contract_errors(corrupted)
    with pytest.raises(ValueError):
        _documented_walkthrough(corrupted)


def test_hash_pinned_local_wheel_enables_offline_bundle_acceptance(tmp_path: Path) -> None:
    wheel = tmp_path / "panopticon_next-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"reviewed evaluator wheel")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": (
            f"panopticon-next @ {wheel.as_uri()}#sha256={digest}"
        ),
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }

    assert _configuration(values) is not None

    wheel.write_bytes(b"changed after review")
    assert _configuration(values) is None


def test_post_setup_acceptance_driver_has_no_direct_mutation_channels() -> None:
    # 2119: REQ-054.7.7
    assert _post_setup_direct_mutations(Path(__file__).read_text()) == []


def test_acceptance_source_digest_rejects_unreviewed_executable_changes() -> None:
    source = Path(__file__).read_text()
    changed = source.replace(
        "def _run_live_new_user_journey(tmp_path: Path, config: LiveConfiguration) -> None:\n",
        "def _run_live_new_user_journey(tmp_path: Path, config: LiveConfiguration) -> None:\n    pass\n",
    )
    assert changed != source

    assert "acceptance source changed" in " ".join(_post_setup_direct_mutations(changed))


def test_acceptance_source_digest_rejects_an_executable_digest_assignment() -> None:
    source = Path(__file__).read_text()
    changed = source.replace(
        f'_ACCEPTANCE_SOURCE_AST_SHA256 = "{_ACCEPTANCE_SOURCE_AST_SHA256}"',
        "_ACCEPTANCE_SOURCE_AST_SHA256 = "
        f'(unreviewed_executable_change(), "{_ACCEPTANCE_SOURCE_AST_SHA256}")[1]',
    )
    assert changed != source

    assert "assigned exactly once as a string literal" in " ".join(
        _post_setup_direct_mutations(changed)
    )


@pytest.mark.parametrize(
    "shortcut",
    (
        'client.post("/tasks", json={})',
        'client.request("POST", "/tasks")',
        "mcp.transition_task(task_id)",
        "seed_task()",
        "task_fixture.mutate()",
        "change_state(task_id)",
        'subprocess.run(["curl", "-X", "POST", "http://127.0.0.1:8000/tasks"])',
        'transport.call_tool("advance", task_id)',
        'task_fixture.state = "COMPLETE"',
        'for task_fixture.state in ["COMPLETE"]:\n        pass',
        "del task_fixture.state",
        'driver.send(b"curl -X POST http://127.0.0.1:8000/tasks\\r")',
        "_wait_until('mutated task', change_state)",
        '_api_get = client.post\n    _api_get("/tasks", json={})',
        "_api_get = mcp.transition_task\n    _api_get(task_id)",
        "_api_get = seed_task\n    _api_get()",
    ),
)
def test_post_setup_mutation_guard_rejects_shortcuts(shortcut: str) -> None:
    source = (
        "def _complete_setup_task(*args):\n"
        "    _wait_for_client_session('service')\n"
        "    responded = set()\n"
        "    time.monotonic()\n"
        "    _api_get(client, token, '/tasks/id')\n"
        "    _capture_pane(env, cwd, 'task')\n"
        "    driver.send(b'\\r')\n"
        "    responded.add('complete')\n"
        "    time.sleep(0.1)\n"
        "def _api_get(client, token, path):\n"
        "    response = client.get(path)\n"
        "    response.raise_for_status()\n"
        "    return response.json()\n"
        "def _assert_task_remains_user_gated(client, token, task_id, state):\n"
        "    deadline = time.monotonic()\n"
        "    _api_get(client, token, task_id)\n"
        "    time.sleep(0.1)\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        "    _complete_setup_task()\n"
        f"    {shortcut}\n"
    )

    assert _post_setup_direct_mutations(source)


def test_post_setup_mutation_guard_rejects_same_line_shortcut() -> None:
    source = (
        "def _complete_setup_task():\n"
        "    pass\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        "    _complete_setup_task(); client.post('/tasks', json={})\n"
    )

    assert any("client.post" in finding for finding in _post_setup_direct_mutations(source))


@pytest.mark.parametrize(
    "deferred_mutation",
    (
        'client.post("/tasks", json={})',
        "mcp.transition_task(task_id)",
        "seed_task()",
        "task_fixture.mutate()",
    ),
)
def test_post_setup_mutation_guard_rejects_precreated_deferred_generators(
    deferred_mutation: str,
) -> None:
    source = (
        "def _complete_setup_task():\n"
        "    pass\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        f"    deferred = ({deferred_mutation} for _ in [None])\n"
        "    _complete_setup_task()\n"
        "    next(deferred)\n"
    )

    assert any("call next" in finding for finding in _post_setup_direct_mutations(source))


@pytest.mark.parametrize(
    "consumer",
    (
        "any(item for item in deferred)",
        "next(item for item in deferred)",
        "[item for item in deferred]",
        "[*deferred]",
        "first, *rest = deferred",
    ),
)
def test_post_setup_mutation_guard_rejects_indirect_deferred_generator_consumers(
    consumer: str,
) -> None:
    source = (
        "def _complete_setup_task():\n"
        "    pass\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        '    deferred = (client.post("/tasks", json={}) for _ in [None])\n'
        "    _complete_setup_task()\n"
        f"    {consumer}\n"
    )

    assert any(
        "pre-setup deferred expression can escape" in finding
        for finding in _post_setup_direct_mutations(source)
    )


@pytest.mark.parametrize(
    "shortcut",
    (
        'client.post("/tasks", json={})',
        'mutate = client.post\n    mutate("/tasks", json={})',
        'mutate = client.request\n    mutate("POST", "/tasks", json={})',
        'subprocess.run(["curl", "-X", "POST", "http://127.0.0.1:8000/tasks"])',
        'subprocess.run(["sh", "-c", "curl -X POST http://127.0.0.1:8000/tasks"])',
        'subprocess.run(["bash", "-lc", "curl -X POST http://127.0.0.1:8000/tasks"])',
        'subprocess.run(["env", "curl", "-X", "POST", "http://127.0.0.1:8000/tasks"])',
        'os.system("curl -X POST http://127.0.0.1:8000/tasks")',
        'mutate = getattr(httpx, "post")\n    mutate("http://127.0.0.1:8000/tasks")',
    ),
)
def test_mutation_guard_rejects_undocumented_pre_setup_rest_shortcuts(
    shortcut: str,
) -> None:
    source = (
        "def _complete_setup_task():\n"
        "    pass\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        f"    {shortcut}\n"
        "    _complete_setup_task()\n"
    )

    assert _post_setup_direct_mutations(source)


def test_walkthrough_contract_rejects_an_unreviewed_natural_language_step() -> None:
    contents = _WALKTHROUGH_PATH.read_text() + "\nCall the HTTP API to create another task.\n"

    assert "walkthrough text changed" in " ".join(_walkthrough_contract_errors(contents))


@pytest.mark.parametrize(
    ("trusted_name", "mutator"),
    (("_api_get", "seed_task"), ("str", "client.post"), ("str", "mcp.transition_task")),
)
def test_post_setup_mutation_guard_rejects_prebound_mutator(
    trusted_name: str, mutator: str
) -> None:
    source = (
        "def _complete_setup_task():\n"
        "    pass\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        f"    {trusted_name} = {mutator}\n"
        "    _complete_setup_task()\n"
        f"    {trusted_name}()\n"
    )

    assert any(
        f"protected name rebind {trusted_name}" in finding
        for finding in _post_setup_direct_mutations(source)
    )


@pytest.mark.parametrize(
    "hidden_mutation",
    (
        'response = client.post("/tasks", json={})',
        'subprocess.run(["curl", "-X", "POST", "http://127.0.0.1:8000/tasks"])',
        'mcp.transition_task("task-id")',
        'task_fixture.state = "COMPLETE"',
    ),
)
def test_observer_helper_guard_rejects_hidden_mutations(hidden_mutation: str) -> None:
    source = (
        "def _complete_setup_task(*args):\n"
        "    _wait_for_client_session('service')\n"
        "    responded = set()\n"
        "    time.monotonic()\n"
        "    _api_get(client, token, '/tasks/id')\n"
        "    _capture_pane(env, cwd, 'task')\n"
        "    driver.send(b'\\r')\n"
        "    responded.add('complete')\n"
        "    time.sleep(0.1)\n"
        "def _api_get(client, token, path):\n"
        f"    {hidden_mutation}\n"
        "def _assert_task_remains_user_gated(client, token, task_id, state):\n"
        "    deadline = time.monotonic()\n"
        "    _api_get(client, token, task_id)\n"
        "    time.sleep(0.1)\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        "    _complete_setup_task()\n"
        "    _api_get(client, token, '/tasks')\n"
    )

    assert _post_setup_direct_mutations(source)


def test_setup_completion_boundary_rejects_a_hidden_mutation_before_helper_return() -> None:
    source = (
        "def _complete_setup_task(*args):\n"
        "    _api_get(client, token, '/tasks/id')\n"
        "    client.post('/tasks', json={})\n"
        "def _api_get(client, token, path):\n"
        "    response = client.get(path)\n"
        "    response.raise_for_status()\n"
        "    return response.json()\n"
        "def _assert_task_remains_user_gated(client, token, task_id, state):\n"
        "    deadline = time.monotonic()\n"
        "    _api_get(client, token, task_id)\n"
        "    time.sleep(0.1)\n"
        "def _run_live_new_user_journey(tmp_path, config):\n"
        "    _complete_setup_task()\n"
    )

    assert _post_setup_direct_mutations(source)


def test_guard_recursively_rejects_mutation_inside_an_approved_helper() -> None:
    source = Path(__file__).read_text()
    needle = (
        "    result = subprocess.run(\n"
        '        ["tmux", "-L", "panopticon", "capture-pane", "-p", "-J", *history, "-t", session],\n'
    )
    assert source.count(needle) == 1
    source = source.replace(
        needle,
        '    httpx.post("http://127.0.0.1:8000/tasks/task-id/state", '
        'json={"state": "COMPLETE"})\n' + needle,
    )

    violations = _post_setup_direct_mutations(source)

    assert any("observer helper _capture_pane calls httpx.post" in item for item in violations)


def test_post_setup_request_audit_rejects_mutating_http_methods() -> None:
    audit = _PostSetupRequestAudit()
    audit.active = True

    audit(httpx.Request("GET", "http://127.0.0.1:8000/tasks"))
    with pytest.raises(AssertionError, match="observation-only"):
        audit(httpx.Request("POST", "http://127.0.0.1:8000/tasks"))


def test_local_wheel_acceptance_rejects_missing_or_symlinked_artifacts(tmp_path: Path) -> None:
    target = tmp_path / "panopticon_next-1.2.3-py3-none-any.whl"
    target.write_bytes(b"reviewed evaluator wheel")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    link = tmp_path / "linked-panopticon_next-1.2.3-py3-none-any.whl"
    link.symlink_to(target)
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": (
            f"panopticon-next @ {link.as_uri()}#sha256={digest}"
        ),
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }

    assert _configuration(values) is None
    values["PANOPTICON_ACCEPTANCE_INSTALL_SPEC"] = (
        f"panopticon-next @ {(tmp_path / 'missing.whl').as_uri()}#sha256={digest}"
    )
    assert _configuration(values) is None


@pytest.mark.parametrize(("harness", "command"), [("claude", "/advance"), ("codex", "$advance")])
def test_live_approval_command_matches_the_selected_harness(harness: str, command: str) -> None:
    assert _advance_command(harness) == command


def test_user_approval_rejects_an_automatic_transition_before_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[bytes] = []

    class RecordingDriver:
        def send(self, data: bytes) -> None:
            sent.append(data)

    def already_advanced(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "ITERATING", "turn": "agent"})

    monkeypatch.setattr(sys.modules[__name__], "_capture_pane", lambda *_args, **_kwargs: "")
    with (
        httpx.Client(
            transport=httpx.MockTransport(already_advanced), base_url="http://panopticon.test"
        ) as client,
        pytest.raises(pytest.fail.Exception, match="advanced before attached approval input"),
    ):
        _send_user_approval_while_gated(
            client,
            "token",
            "task-id",
            "PLANNING",
            {},
            Path("."),
            "panopticon-task-id",
            RecordingDriver(),  # type: ignore[arg-type]
            b"$advance\r",
            observation_window=0.05,
        )

    assert sent == []


def test_terminal_switch_rejects_an_automatic_move_before_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[bytes] = []

    class RecordingDriver:
        def send(self, data: bytes) -> None:
            sent.append(data)

    monkeypatch.setattr(
        sys.modules[__name__], "_client_sessions", lambda _env, _cwd: ["panopticon-task-id"]
    )
    with pytest.raises(pytest.fail.Exception, match="left 'dashboard' before documented input"):
        _send_session_switch_while_attached(
            {},
            Path("."),
            RecordingDriver(),  # type: ignore[arg-type]
            "dashboard",
            b"t",
            observation_window=0.05,
        )

    assert sent == []


def test_terminal_switch_rejects_a_move_caused_by_the_wrong_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[bytes] = []
    switched = threading.Event()

    class RecordingDriver:
        def send(self, data: bytes) -> None:
            sent.append(data)
            if data == _SESSION_SWITCH_PROBE_KEYS:
                switched.set()

    monkeypatch.setattr(
        sys.modules[__name__],
        "_client_sessions",
        lambda _env, _cwd: ["panopticon-task-id"] if switched.is_set() else ["dashboard"],
    )
    with pytest.raises(pytest.fail.Exception, match="left 'dashboard' before documented input"):
        _send_session_switch_while_attached(
            {},
            Path("."),
            RecordingDriver(),  # type: ignore[arg-type]
            "dashboard",
            b"t",
            True,
            observation_window=0.05,
        )

    assert sent == [_SESSION_SWITCH_PROBE_KEYS]


def test_opener_key_rejects_an_automatic_handoff_before_input(tmp_path: Path) -> None:
    sent: list[bytes] = []
    opener_log = tmp_path / "artifact-opened"
    opener_log.write_text("/wrong/automatic/path\n")

    class RecordingDriver:
        def send(self, data: bytes) -> None:
            sent.append(data)

    with pytest.raises(pytest.fail.Exception, match="opener ran before documented input"):
        _send_opener_key_while_unopened(
            opener_log,
            RecordingDriver(),  # type: ignore[arg-type]
            b"a",
            observation_window=0.05,
        )

    assert sent == []


def test_live_diagnostics_are_bounded_and_redact_both_credentials() -> None:
    github_token = "github-secret"
    harness_token = "harness-secret"
    diagnostic = _redacted_tail(
        ("x" * 200)
        + f" before {github_token} middle {harness_token} "
        + f"masked ...{github_token[-4:]} and ...{harness_token[-4:]} after",
        (github_token, harness_token),
        limit=80,
    )
    assert len(diagnostic) == 80
    assert github_token not in diagnostic
    assert harness_token not in diagnostic
    assert f"...{github_token[-4:]}" not in diagnostic
    assert f"...{harness_token[-4:]}" not in diagnostic


def test_live_configuration_repr_omits_both_credentials() -> None:
    config = LiveConfiguration(
        install_spec="panopticon-next==1.2.3",
        repo_url="https://github.com/acme/panopticon-acceptance-disposable.git",
        base_sha="a" * 40,
        github_token="github-secret",
        harness="codex",
        harness_auth_env="OPENAI_API_KEY",
        harness_auth_token="harness-secret",
    )

    rendered = repr(config)
    assert "github-secret" not in rendered
    assert "harness-secret" not in rendered
    assert "github_token" not in rendered
    assert "harness_auth_token" not in rendered


def test_pty_process_has_the_fixed_terminal_size_and_drains_output(tmp_path: Path) -> None:
    process = _PtyProcess.start(
        ["sh", "-c", "stty size; printf 'drained-output\\n'"],
        env={**os.environ, "TERM": "xterm-256color"},
        cwd=tmp_path,
    )
    try:
        process.wait_for_text("45 180", timeout=5)
        process.wait_for_text("drained-output", timeout=5)
        assert process.wait(timeout=5) == 0
    finally:
        process.terminate()


@pytest.mark.parametrize("missing", _REQUIRED)
def test_clean_host_acceptance_requires_every_explicit_input(tmp_path: Path, missing: str) -> None:
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": _valid_local_install_spec(tmp_path),
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }
    values.pop(missing)
    assert _configuration(values) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PANOPTICON_NEW_USER_ACCEPTANCE", "1"),
        ("PANOPTICON_ACCEPTANCE_INSTALL_SPEC", "panopticon-next"),
        ("PANOPTICON_ACCEPTANCE_INSTALL_SPEC", "panopticon-next @ git+https://github.com/a/b@main"),
        (
            "PANOPTICON_ACCEPTANCE_INSTALL_SPEC",
            "panopticon-next @ https://token@example.com/panopticon_next-1.2.3-py3-none-any.whl#sha256="
            + "a" * 64,
        ),
        (
            "PANOPTICON_ACCEPTANCE_INSTALL_SPEC",
            "panopticon-next @ git+https://token@example.com/a/b@" + "a" * 40,
        ),
        ("PANOPTICON_ACCEPTANCE_GITHUB_REPO", "https://token@github.com/acme/repo"),
        ("PANOPTICON_ACCEPTANCE_GITHUB_REPO", "https://gitlab.com/acme/repo"),
        ("PANOPTICON_ACCEPTANCE_GITHUB_REPO", "https://github.com/acme/ordinary-repo"),
        ("PANOPTICON_ACCEPTANCE_BASE_SHA", "main"),
        ("PANOPTICON_ACCEPTANCE_HARNESS", "unknown"),
        # Pi has no MCP-backed GitHub workflow approval path yet; keep this complete journey honest.
        ("PANOPTICON_ACCEPTANCE_HARNESS", "pi"),
        ("PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV", "GH_TOKEN"),
        ("PANOPTICON_ACCEPTANCE_GH_TOKEN", "line one\nline two"),
    ],
)
def test_clean_host_acceptance_rejects_ambiguous_or_unsafe_configuration(
    tmp_path: Path, key: str, value: str
) -> None:
    values = {
        "PANOPTICON_NEW_USER_ACCEPTANCE": _OPT_IN,
        "PANOPTICON_ACCEPTANCE_INSTALL_SPEC": _valid_local_install_spec(tmp_path),
        "PANOPTICON_ACCEPTANCE_GITHUB_REPO": (
            "https://github.com/acme/panopticon-acceptance-disposable.git"
        ),
        "PANOPTICON_ACCEPTANCE_BASE_SHA": "a" * 40,
        "PANOPTICON_ACCEPTANCE_GH_TOKEN": "github-token",
        "PANOPTICON_ACCEPTANCE_HARNESS": "codex",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV": "OPENAI_API_KEY",
        "PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN": "model-token",
    }
    values[key] = value
    assert _configuration(values) is None


# 2119: REQ-054.7.1, REQ-054.7.2, REQ-054.7.6, REQ-054.7.7, REQ-054.7.8, REQ-054.7.9
def test_new_user_completes_one_real_github_self_reviewed_task(tmp_path: Path) -> None:
    if _LIVE_CONFIGURATION is None:
        pytest.skip("set every documented clean-host acceptance input and disposable-host opt-in")
    _run_live_new_user_journey(tmp_path, _LIVE_CONFIGURATION)


def test_complete_live_configuration_enters_the_real_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LiveConfiguration(
        install_spec="panopticon-next==1.2.3",
        repo_url="https://github.com/acme/panopticon-acceptance-disposable.git",
        base_sha="a" * 40,
        github_token="github-token",
        harness="codex",
        harness_auth_env="OPENAI_API_KEY",
        harness_auth_token="model-token",
    )
    entered: list[tuple[Path, LiveConfiguration]] = []
    monkeypatch.setitem(globals(), "_LIVE_CONFIGURATION", config)
    monkeypatch.setitem(
        globals(),
        "_run_live_new_user_journey",
        lambda path, selected: entered.append((path, selected)),
    )

    test_new_user_completes_one_real_github_self_reviewed_task(tmp_path)

    assert entered == [(tmp_path, config)]
