"""Private, runner-local credential configuration for foreground setup.

Connections contain harness credentials only. Repository files are immutable snapshots when
configured; rotating subscription credentials deliberately retain a shared directory reference.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from panopticon.core.dirs import _secrets_dir
from panopticon.harnesses.pi import API_KEY_ENV_VARS
from panopticon.terminal.log_tee import open_private_directory

AUTH_KEYS: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"),
    "pi": tuple(API_KEY_ENV_VARS),
    "outfitter": tuple(API_KEY_ENV_VARS),
}


@dataclass(frozen=True)
class Connection:
    harness: str
    env_file: str
    credential_dir: str | None = None


def _private_root(secrets_root: Path | None = None) -> Path:
    """Resolve configured parent aliases while refusing a symlink at the secrets directory."""
    root = secrets_root if secrets_root is not None else _secrets_dir()
    return root.parent.resolve() / root.name


def _directory(secrets_root: Path | None = None) -> int:
    root = _private_root(secrets_root)
    try:
        fd = open_private_directory(root, create=True)
    except OSError as exc:
        raise ValueError(f"Cannot safely open secrets directory: {root}") from exc
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError(f"Setup needs an owner-only secrets directory (0700): {root}")
    return fd


def _validate_file(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError(f"Setup requires an owner-only regular credential file (0600): {path}")


def read_private(reference: str) -> str:
    """Read a credential reference without following directory or leaf symlinks."""
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or not relative.name:
        raise ValueError("Invalid private credential reference.")
    directory = open_private_directory(_private_root() / relative.parent, create=False)
    try:
        try:
            fd = os.open(
                relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(
                f"Cannot safely read credential file: {_private_root() / relative}"
            ) from exc
        try:
            _validate_file(fd, _secrets_dir() / relative)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, encoding="utf-8") as handle:
            return handle.read()
    finally:
        os.close(directory)


def write_private(reference: str, content: str) -> None:
    """Atomically replace a private leaf; reject unsafe existing files without repairing them."""
    if Path(reference).name != reference or reference in {"", ".", ".."}:
        raise ValueError("Invalid private credential filename.")
    directory = _directory()
    temporary = f".setup-{uuid.uuid4().hex}"
    try:
        try:
            old = os.open(reference, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            pass
        else:
            try:
                _validate_file(old, _secrets_dir() / reference)
            finally:
                os.close(old)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, reference, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


@contextlib.contextmanager
def setup_lock(*, secrets_root: Path | None = None) -> Iterator[None]:
    """A process-lifetime writer lock, released automatically even after interrupted login."""
    directory = _directory(secrets_root)
    try:
        fd = os.open(
            "setup.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        _validate_file(
            fd, (secrets_root if secrets_root is not None else _secrets_dir()) / "setup.lock"
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another setup is active on this host. Finish or exit it first."
            ) from exc
        yield
    finally:
        os.close(fd)


def env_values(content: str) -> dict[str, str]:
    """Docker env-file values are literal; shell evaluation is deliberately unsupported."""
    values = {}
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


def merge_env(content: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or any(c in value for c in "\r\n\0"):
            raise ValueError("Credential must be a single line.")
    lines = [line for line in content.splitlines() if line.split("=", 1)[0].strip() not in values]
    return "\n".join([*lines, *(f"{key}={value}" for key, value in values.items())]) + "\n"


def load_connections() -> dict[str, Connection]:
    try:
        data = json.loads(read_private("connections.json"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Saved connection configuration is invalid; run setup to repair it.")
    result = {}
    for harness, fields in data.items():
        if harness not in AUTH_KEYS or not isinstance(fields, dict):
            raise ValueError("Saved connection configuration is invalid.")
        env_file = fields.get("env_file")
        credential_dir = fields.get("credential_dir")
        if not isinstance(env_file, str) or (
            credential_dir is not None and not isinstance(credential_dir, str)
        ):
            raise ValueError("Saved connection configuration is invalid.")
        result[harness] = Connection(harness, env_file, credential_dir)
    return result


def save_connection(connection: Connection) -> None:
    connections = load_connections()
    connections[connection.harness] = connection
    write_private(
        "connections.json",
        json.dumps(
            {
                name: {"env_file": value.env_file, "credential_dir": value.credential_dir}
                for name, value in connections.items()
            }
        )
        + "\n",
    )


def connection_values(connection: Connection) -> dict[str, str]:
    values = env_values(read_private(connection.env_file))
    allowed = AUTH_KEYS[connection.harness]
    if any(key not in allowed for key in values):
        raise ValueError(
            f"Reusable connection contains non-harness values: {_private_root() / connection.env_file}; it cannot be applied."
        )
    return values


def configured(connection: Connection) -> bool:
    try:
        values = connection_values(connection)
    except FileNotFoundError:
        return False
    if any(values.get(key, "").strip() for key in AUTH_KEYS[connection.harness]):
        return True
    if connection.harness in {"codex", "pi", "outfitter"} and connection.credential_dir:
        try:
            data = json.loads(read_private(f"{connection.credential_dir}/auth.json"))
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and bool(data)
    return False


def connect(
    harness: str,
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
    run: Callable[..., Any] = subprocess.run,
) -> Connection:
    """Configure a reusable connection. Caller holds setup_lock throughout the operation."""
    if harness not in AUTH_KEYS:
        raise ValueError(f"No foreground setup is available for {harness}.")
    saved = load_connections().get(harness)
    if (
        saved
        and configured(saved)
        and input_fn(f"Use saved {harness} connection on this host? [Y/n] ").strip().lower() != "n"
    ):
        return saved
    print(f"Connect {harness}. Credentials are saved privately on this host for task containers.")
    values: dict[str, str] = {}
    credential_dir = None
    if harness == "claude":
        token = secret_fn("Paste a Claude token or API key (Enter for browser login): ").strip()
        if not token:
            if not shutil.which("claude"):
                raise RuntimeError("Install Claude Code, then run `panopticon setup` again.")
            result = run(["claude", "setup-token"], check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    "Claude login was cancelled or failed; previous credentials were kept."
                )
            token = secret_fn("Paste the token shown by Claude (input hidden): ").strip()
        if not token:
            raise RuntimeError("No credential saved. Run `panopticon setup` to resume.")
        key = "ANTHROPIC_API_KEY" if token.startswith("sk-ant-api") else "CLAUDE_CODE_OAUTH_TOKEN"
        values[key] = token
    elif harness == "codex":
        token = secret_fn("Paste an OpenAI API key (Enter for Codex browser login): ").strip()
        if token:
            values["OPENAI_API_KEY"] = token
        else:
            if not shutil.which("codex"):
                raise RuntimeError("Install Codex, then run `panopticon setup` again.")
            credential_dir = f"codex-{uuid.uuid4().hex}.d"
            path = _private_root() / credential_dir
            fd = open_private_directory(path, create=True)
            os.close(fd)
            child_env = {
                key: value
                for key, value in os.environ.items()
                if key not in {key for keys in AUTH_KEYS.values() for key in keys}
            }
            child_env["CODEX_HOME"] = str(path)
            result = run(
                ["codex", "-c", 'cli_auth_credentials_store="file"', "login"],
                env=child_env,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Codex login was cancelled or failed; previous credentials were kept."
                )
            auth = json.loads(read_private(f"{credential_dir}/auth.json"))
            if not isinstance(auth, dict) or not auth:
                raise ValueError("Codex did not save usable credential data. Run setup again.")
    else:
        print("Supported provider variables: " + ", ".join(AUTH_KEYS[harness]))
        key = input_fn("Provider variable [ANTHROPIC_API_KEY]: ").strip() or "ANTHROPIC_API_KEY"
        if key not in AUTH_KEYS[harness]:
            raise ValueError("Unsupported provider variable.")
        token = secret_fn("Provider API key (input hidden): ").strip()
        if not token:
            raise RuntimeError("No credential saved. Run `panopticon setup` to resume.")
        values[key] = token
    connection = Connection(harness, f"connection-{uuid.uuid4().hex}.env", credential_dir)
    write_private(connection.env_file, merge_env("", values))
    save_connection(connection)
    print(f"{harness} credentials configured locally. A task will verify provider access.")
    return connection
