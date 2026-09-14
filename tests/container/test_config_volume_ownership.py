"""Real entrypoint ownership checks with isolated Docker volumes and no agent/model calls."""

from __future__ import annotations

import importlib.resources
import shutil
import subprocess
import uuid
from collections.abc import Iterator

import pytest

import panopticon.docker as docker_package


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=300
    ).stdout


def _docker_running() -> bool:
    return bool(
        shutil.which("docker")
        and subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    )


pytestmark = pytest.mark.skipif(not _docker_running(), reason="needs a working Docker daemon")


@pytest.fixture(scope="module")
def ownership_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    context = tmp_path_factory.mktemp("config-ownership-image")
    entrypoint = importlib.resources.files(docker_package) / "entrypoint.sh"
    (context / "entrypoint.sh").write_bytes(entrypoint.read_bytes())
    (context / "Dockerfile").write_text(
        "FROM python:3.13-slim\n"
        "RUN apt-get update && apt-get install --yes --no-install-recommends gosu passwd\n"
        "RUN groupadd --gid 1000 panopticon && useradd --uid 1000 --gid 1000 "
        "--create-home --home-dir /home/panopticon panopticon\n"
        "ENV HOME=/home/panopticon\n"
        "COPY --chmod=0755 entrypoint.sh /entrypoint.sh\n"
        'ENTRYPOINT ["/entrypoint.sh"]\n'
    )
    image = f"panopticon-config-ownership-{uuid.uuid4().hex}"
    try:
        _docker("build", "--tag", image, str(context))
        yield image
    finally:
        subprocess.run(["docker", "image", "rm", image], capture_output=True)


@pytest.mark.parametrize("config_dir", [".claude", ".codex", ".pi", ".outfitter"])
@pytest.mark.parametrize("identity", ["1000:1000", "1234:5678"])
def test_fresh_and_resumed_config_volume_is_writable_without_changing_external_targets(
    ownership_image: str, config_dir: str, identity: str
) -> None:
    prefix = f"panopticon-config-ownership-{uuid.uuid4().hex}"
    volumes = [f"{prefix}-{suffix}" for suffix in ("config", "credentials", "unrelated")]
    uid, gid = identity.split(":")
    config_path = f"/home/panopticon/{config_dir}"
    mounts = [
        "--mount",
        f"type=volume,src={volumes[0]},dst={config_path},volume-nocopy",
        "--mount",
        f"type=volume,src={volumes[1]},dst=/panopticon/credentials,volume-nocopy",
        "--mount",
        f"type=volume,src={volumes[2]},dst=/unrelated,volume-nocopy",
    ]
    run = ["run", "--rm", "--network", "none", *mounts]

    def as_root(script: str) -> None:
        _docker(*run, "--entrypoint", "bash", ownership_image, "-euc", script)

    def as_task(script: str) -> None:
        _docker(
            *run,
            "--env",
            f"PANOPTICON_PUID={uid}",
            "--env",
            f"PANOPTICON_PGID={gid}",
            "--env",
            f"CONFIG_DIR={config_path}",
            "--env",
            f"EXPECTED_ID={identity}",
            ownership_image,
            "bash",
            "-euc",
            script,
        )

    try:
        for volume in volumes:
            _docker("volume", "create", volume)
        as_root(
            "printf credential-fixture > /panopticon/credentials/auth.json; "
            "printf unrelated-fixture > /unrelated/data; "
            "chown --recursive 4321:5432 /panopticon/credentials /unrelated"
        )
        as_task(
            'test "$(id --user):$(id --group)" = "$EXPECTED_ID"; '
            'test "$(stat --format=%u:%g "$CONFIG_DIR")" = "$EXPECTED_ID"; '
            'mkdir --parents "$CONFIG_DIR/agent"; '
            'printf saved-session > "$CONFIG_DIR/agent/history"; '
            'chmod 0600 "$CONFIG_DIR/agent/history"; '
            'ln --symbolic /panopticon/credentials/auth.json "$CONFIG_DIR/agent/auth.json"'
        )
        # Simulate a volume last used by another host, preserving its saved state and links.
        as_root(f"chown --recursive --no-dereference 2222:3333 {config_path}")
        as_task(
            'test "$(id --user):$(id --group)" = "$EXPECTED_ID"; '
            'test "$(cat "$CONFIG_DIR/agent/history")" = saved-session; '
            'test "$(stat --format=%u:%g "$CONFIG_DIR/agent/history")" = "$EXPECTED_ID"; '
            'test "$(stat --format=%a "$CONFIG_DIR/agent/history")" = 600; '
            'printf resumed >> "$CONFIG_DIR/agent/history"; '
            'test "$(cat "$CONFIG_DIR/agent/history")" = saved-sessionresumed; '
            'test -L "$CONFIG_DIR/agent/auth.json"; '
            'test "$(cat "$CONFIG_DIR/agent/auth.json")" = credential-fixture; '
            'test "$(stat --format=%u:%g /panopticon/credentials/auth.json)" = 4321:5432; '
            'test "$(cat /unrelated/data)" = unrelated-fixture; '
            'test "$(stat --format=%u:%g /unrelated/data)" = 4321:5432'
        )
    finally:
        for volume in volumes:
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
