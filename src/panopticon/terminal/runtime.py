"""Truthful, bounded readiness checks for the integrated local runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import httpx

import panopticon
from panopticon.taskservice.auth import environment_token
from panopticon.taskservice.operator_auth import OPERATOR_AUTH_ENV, OPERATOR_AUTH_FILE
from panopticon.terminal.session_environment import selected_session_environment

SERVICE_KIND = "panopticon-task-service"
RUNTIME_API_REVISION = 1
DEFAULT_CONTAINER_SERVICE_URL = "http://host.docker.internal:8000"
DEFAULT_RUNNER_ID = "local"
DEFAULT_READINESS_TIMEOUT = 30.0
DEFAULT_REQUEST_TIMEOUT = 1.0


class MigrationDecision(str, Enum):
    """Whether automatic migration is safe before integrated startup."""

    SERVICE_ABSENT = "service_absent"
    COMPATIBLE_SERVICE_LIVE = "compatible_service_live"


class RuntimeReadinessError(RuntimeError):
    """A live or expected runtime could not be verified safely."""


@dataclass(frozen=True)
class RuntimeConfiguration:
    """The non-secret launch contract shared by foreground, service, and runner."""

    service_url: str
    container_service_url: str
    runner_id: str
    instance_id: str
    environment: Mapping[str, str]


GetResponse = Callable[[str, float], httpx.Response]
SessionExists = Callable[[], bool]


def _resolved_home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("HOME") or Path.home()).expanduser().resolve()


def _resolved_directory(
    environ: Mapping[str, str], *, panopticon_name: str, xdg_name: str, fallback: Path
) -> Path:
    if value := environ.get(panopticon_name):
        return Path(value).expanduser().resolve()
    if value := environ.get(xdg_name):
        return (Path(value).expanduser() / "panopticon").resolve()
    return fallback.resolve()


def _identity_material(
    *,
    environment: Mapping[str, str],
    executable: str,
    service_url: str,
    data_dir: Path,
    config_dir: Path,
    cache_dir: Path,
    state_dir: Path,
    database: str,
) -> dict[str, Any]:
    stable_runtime_settings = {
        name: environment[name]
        for name in (
            "TMUX_TMPDIR",
            "DOCKER_API_VERSION",
            "DOCKER_CERT_PATH",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_DEFAULT_PLATFORM",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "PANOPTICON_CONTAINER_SERVICE_URL",
            OPERATOR_AUTH_ENV,
            "PANOPTICON_SERVICE_AUTH_MODE",
            "PANOPTICON_SERVICE_AUTH_FILE",
        )
        if name in environment
    }
    return {
        "executable": str(Path(executable).expanduser().resolve()),
        "install": str(Path(panopticon.__file__).resolve().parent),
        "service_url": service_url.rstrip("/"),
        "data": str(data_dir),
        "config": str(config_dir),
        "cache": str(cache_dir),
        "state": str(state_dir),
        "database": database,
        "runtime_settings": stable_runtime_settings,
    }


def _service_port(service_url: str) -> int:
    try:
        parsed = urlsplit(service_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("PANOPTICON_SERVICE_URL is not a valid URL") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "PANOPTICON_SERVICE_URL must be an HTTP origin without credentials, path, query, or "
            "fragment"
        )
    return port if port is not None else 80


def resolve_runtime(
    service_url: str,
    *,
    environ: Mapping[str, str] = os.environ,
    executable: str = sys.executable,
) -> RuntimeConfiguration:
    """Resolve and fingerprint the local integrated runtime once.

    The runner id is intentionally excluded from the fingerprint: multiple selected runners may
    connect to one service instance, and runner readiness is proven through the live registry.
    """

    selected = selected_session_environment(environ)
    home = _resolved_home(environ)
    data_dir = _resolved_directory(
        environ,
        panopticon_name="PANOPTICON_DATA",
        xdg_name="XDG_DATA_HOME",
        fallback=home / ".local" / "share" / "panopticon",
    )
    config_dir = _resolved_directory(
        environ,
        panopticon_name="PANOPTICON_CONFIG",
        xdg_name="XDG_CONFIG_HOME",
        fallback=home / ".config" / "panopticon",
    )
    cache_dir = _resolved_directory(
        environ,
        panopticon_name="PANOPTICON_CACHE",
        xdg_name="XDG_CACHE_HOME",
        fallback=home / ".cache" / "panopticon",
    )
    state_dir = _resolved_directory(
        environ,
        panopticon_name="PANOPTICON_STATE",
        xdg_name="XDG_STATE_HOME",
        fallback=home / ".local" / "state" / "panopticon",
    )
    normalized_service_url = service_url.rstrip("/")
    port = _service_port(normalized_service_url)
    default_container_url = DEFAULT_CONTAINER_SERVICE_URL.rsplit(":", 1)[0] + f":{port}"
    container_service_url = environ.get("PANOPTICON_CONTAINER_SERVICE_URL", default_container_url)
    runner_id = environ.get("PANOPTICON_RUNNER_ID", DEFAULT_RUNNER_ID)
    database = environ.get("PANOPTICON_DB", f"sqlite:///{data_dir / 'panopticon.db'}")
    operator_token_file = environ.get(OPERATOR_AUTH_ENV)
    if (
        operator_token_file
        or environ.get("PANOPTICON_OPERATOR_TOKEN")
        or (config_dir / "secrets" / OPERATOR_AUTH_FILE).exists()
    ):
        selected[OPERATOR_AUTH_ENV] = operator_token_file or OPERATOR_AUTH_FILE
    selected.update(
        {
            "HOME": str(home),
            "PATH": environ.get("PATH", os.defpath),
            "PANOPTICON_CACHE": str(cache_dir),
            "PANOPTICON_CONFIG": str(config_dir),
            "PANOPTICON_CONTAINER_SERVICE_URL": container_service_url,
            "PANOPTICON_DATA": str(data_dir),
            "PANOPTICON_DB": database,
            "PANOPTICON_PORT": str(port),
            "PANOPTICON_RUNNER_ID": runner_id,
            "PANOPTICON_SERVICE_URL": normalized_service_url,
            "PANOPTICON_STATE": str(state_dir),
        }
    )
    selected.pop("PANOPTICON_INSTANCE_ID", None)
    material = _identity_material(
        environment=selected,
        executable=executable,
        service_url=normalized_service_url,
        data_dir=data_dir,
        config_dir=config_dir,
        cache_dir=cache_dir,
        state_dir=state_dir,
        database=database,
    )
    instance_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    selected["PANOPTICON_INSTANCE_ID"] = instance_id
    return RuntimeConfiguration(
        normalized_service_url,
        container_service_url,
        runner_id,
        instance_id,
        MappingProxyType(selected),
    )


def _default_get(runtime: RuntimeConfiguration) -> GetResponse:
    token = environment_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    def get(path: str, timeout: float) -> httpx.Response:
        total_timeout = max(timeout, 0.001)

        async def request() -> httpx.Response:
            try:
                async with asyncio.timeout(total_timeout):
                    async with httpx.AsyncClient(
                        base_url=runtime.service_url,
                        headers=headers,
                        trust_env=False,
                        timeout=None,
                    ) as client:
                        return await client.get(path)
            except TimeoutError as exc:
                request = httpx.Request("GET", f"{runtime.service_url}{path}")
                raise httpx.ReadTimeout(
                    f"request exceeded its {total_timeout:g}s total deadline",
                    request=request,
                ) from exc

        return asyncio.run(request())

    return get


def integrated_service_session_exists(
    *, run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
) -> bool:
    """Whether the dedicated tmux server has a named integrated service session."""

    from panopticon.sessionservice.tmux_defaults import defaults_argv

    try:
        result = run(
            [
                "tmux",
                "-L",
                "panopticon",
                *defaults_argv("panopticon"),
                "has-session",
                "-t",
                "service",
            ],
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _verified_identity(response: httpx.Response, runtime: RuntimeConfiguration) -> dict[str, Any]:
    if response.status_code in {401, 403}:
        raise RuntimeReadinessError(
            "task service rejected the configured credential; reconnect Panopticon authentication"
        )
    if response.status_code == 404:
        raise RuntimeReadinessError(
            "an unverified legacy task service is already running; leave it running and use "
            "`panopticon stop` only when you are ready to replace or upgrade that fleet"
        )
    if response.status_code >= 400:
        response.raise_for_status()
    try:
        identity = response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeReadinessError(
            "the service identity response is malformed; check that the configured address points "
            "to Panopticon"
        ) from exc
    if not isinstance(identity, dict):
        raise RuntimeReadinessError(
            "the service identity response is malformed; check that the configured address points "
            "to Panopticon"
        )
    if identity.get("service") != SERVICE_KIND:
        raise RuntimeReadinessError(
            "the configured address belongs to a different service; check PANOPTICON_SERVICE_URL"
        )
    revision = identity.get("api_revision")
    if type(revision) is not int or revision != RUNTIME_API_REVISION:
        raise RuntimeReadinessError(
            f"task-service runtime API revision {revision!r} is unsupported; this client supports "
            f"revision {RUNTIME_API_REVISION}. Preserve the current fleet and use matching "
            "client and service versions"
        )
    if identity.get("instance_id") != runtime.instance_id:
        raise RuntimeReadinessError(
            "a different Panopticon runtime is listening at the configured address; preserve it "
            "and use the matching installation or stop it deliberately"
        )
    if not isinstance(identity.get("version"), str):
        raise RuntimeReadinessError(
            "the service identity response is malformed because it has no valid package version; "
            "check that PANOPTICON_SERVICE_URL points to a compatible Panopticon task service"
        )
    return identity


def guard_before_migration(
    runtime: RuntimeConfiguration,
    *,
    get: GetResponse | None = None,
    service_session_exists: SessionExists = integrated_service_session_exists,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> MigrationDecision:
    """Verify that migration cannot race a live, starting, or inert service process."""

    request = get or _default_get(runtime)
    try:
        response = request("/identity", request_timeout)
    except httpx.ConnectError as exc:
        if service_session_exists():
            raise RuntimeReadinessError(
                "the service tmux session exists but its authenticated API is unavailable; "
                "migration was not run because that process may still hold the database. Inspect "
                "the service log and stop the fleet deliberately before retrying"
            ) from exc
        return MigrationDecision.SERVICE_ABSENT
    except httpx.TimeoutException as exc:
        raise RuntimeReadinessError(
            "the task service did not answer the migration safety probe; migration was not run"
        ) from exc
    except httpx.TransportError as exc:
        raise RuntimeReadinessError(
            "the task service connection failed during the migration safety probe; migration "
            "was not run. Inspect the service log before retrying"
        ) from exc
    try:
        _verified_identity(response, runtime)
    except httpx.HTTPStatusError as exc:
        raise RuntimeReadinessError(
            f"task service returned HTTP {exc.response.status_code} during the migration safety "
            "probe; migration was not run"
        ) from exc
    return MigrationDecision.COMPATIBLE_SERVICE_LIVE


def _runner_registrations(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code in {401, 403}:
        raise RuntimeReadinessError(
            "task service rejected runner inventory access; reconnect Panopticon authentication"
        )
    response.raise_for_status()
    try:
        body = response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeReadinessError(
            "the task service returned a malformed runner inventory"
        ) from exc
    if not isinstance(body, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in body
    ):
        raise RuntimeReadinessError("the task service returned a malformed runner inventory")
    return body


def wait_until_ready(
    runtime: RuntimeConfiguration,
    *,
    timeout: float = DEFAULT_READINESS_TIMEOUT,
    interval: float = 0.2,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    get: GetResponse | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Return verified identity once the intended service and runner are live, or fail boundedly."""

    if timeout < 0 or interval <= 0 or request_timeout <= 0:
        raise ValueError(
            "readiness timeout must be nonnegative; interval and request timeout positive"
        )
    request = get or _default_get(runtime)
    deadline = monotonic() + timeout
    service_verified = False
    identity: dict[str, Any] | None = None
    runners: list[dict[str, Any]] = []
    runner_log = Path(runtime.environment["PANOPTICON_STATE"]) / "runner.log"
    attempted = False
    while True:
        if attempted and monotonic() >= deadline:
            break
        attempted = True
        service_verified = False
        remaining = max(0.001, deadline - monotonic())
        try:
            response = request("/identity", min(request_timeout, remaining))
            if response.status_code >= 500:
                response.raise_for_status()
            identity = _verified_identity(response, runtime)
            service_verified = True
            if monotonic() >= deadline:
                break
            remaining = max(0.001, deadline - monotonic())
            runners = _runner_registrations(request("/runners", min(request_timeout, remaining)))
            if monotonic() > deadline:
                break
            selected = [runner for runner in runners if runner["id"] == runtime.runner_id]
            if any(runner.get("instance_id") == runtime.instance_id for runner in selected):
                return identity
            if selected:
                raise RuntimeReadinessError(
                    f"runner {runtime.runner_id!r} is connected from a different runtime; "
                    f"preserve it and inspect {runner_log} before stopping or replacing it "
                    "deliberately"
                )
        except (httpx.TransportError, httpx.HTTPStatusError):
            # Connection startup and transient server errors may settle before the bounded deadline.
            pass
        if monotonic() >= deadline:
            break
        sleep(min(interval, max(0.0, deadline - monotonic())))

    if not service_verified:
        raise RuntimeReadinessError(
            f"task service at {runtime.service_url} did not become ready within {timeout:g}s; "
            "inspect the service log"
        )
    runner_ids = [str(runner["id"]) for runner in runners]
    others = ", ".join(sorted(runner_ids)) if runner_ids else "none"
    raise RuntimeReadinessError(
        f"runner {runtime.runner_id!r} did not connect within {timeout:g}s (other live runners: "
        f"{others}); inspect {runner_log}"
    )
