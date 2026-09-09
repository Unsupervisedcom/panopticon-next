"""Human temporary-home launchers isolate state and run installed artifacts."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "bin" / "dev-tmp-home"
PROD = ROOT / "bin" / "prod-tmp-home"
COMMON = ROOT / "bin" / "_tmp-home-common"


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -eu\n{body}")
    path.chmod(0o755)


def _fake_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    trace = tmp_path / "trace"
    target = tmp_path / "target"
    target.mkdir()
    (target / ".git").mkdir()
    original_home = tmp_path / "original-home"
    auth_dir = original_home / ".codex"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text('{"auth_mode":"chatgpt"}')
    fake_panopticon = tmp_path / "fake-panopticon"
    _executable(
        fake_panopticon,
        """
auth=no
[ ! -f "$HOME/.codex/auth.json" ] || auth=yes
printf 'panopticon:%s|home=%s|config=%s|codex_key=%s|cwd=%s|auth=%s\\n' \\
  "$*" "$HOME" "${PANOPTICON_CONFIG-unset}" "${CODEX_API_KEY-unset}" "$PWD" "$auth" \\
  >> "$PANOPTICON_TMP_HOME_TRACE"
printf 'runtime:%s|port=%s|service=%s|container_service=%s|tmux=%s|docker_host=%s|docker_context=%s\\n' \\
  "${PANOPTICON_RUNTIME_ID-unset}" "${PANOPTICON_PORT-unset}" \\
  "${PANOPTICON_SERVICE_URL-unset}" "${PANOPTICON_CONTAINER_SERVICE_URL-unset}" \\
  "${TMUX_TMPDIR-unset}" "${DOCKER_HOST-unset}" "${DOCKER_CONTEXT-unset}" \\
  >> "$PANOPTICON_TMP_HOME_TRACE"
[ "${1-}" != --version ] || printf 'panopticon test\\n'
""",
    )
    _executable(
        fake_bin / "docker",
        """
printf 'docker:%s\\n' "$*" >> "$PANOPTICON_TMP_HOME_TRACE"
[ "${FAKE_DOCKER_FAIL-0}" != 1 ]
if [ "${1-}" = context ]; then printf 'unix:///tmp/fake-docker.sock\\n'; exit 0; fi
[ "${FAKE_DOCKER_CONTAINERS-0}" != 1 ] || printf 'container-id\\n'
""",
    )
    _executable(
        fake_bin / "tmux",
        'printf \'tmux:%s\\n\' "$*" >> "$PANOPTICON_TMP_HOME_TRACE"\n'
        'exit "${FAKE_TMUX_STATUS-1}"\n',
    )
    _executable(fake_bin / "python3", "printf '41873\\n'\n")
    _executable(
        fake_bin / "uv",
        """
printf 'uv:%s\\n' "$*" >> "$PANOPTICON_TMP_HOME_TRACE"
out=
while [ "$#" -gt 0 ]; do
  if [ "$1" = --out-dir ]; then shift; out=$1; fi
  shift
done
mkdir -p "$out"
: > "$out/panopticon_next-0.2.8-py3-none-any.whl"
""",
    )
    _executable(
        fake_bin / "pipx",
        """
printf 'pipx:%s|home=%s\\n' "$*" "$HOME" >> "$PANOPTICON_TMP_HOME_TRACE"
mkdir -p "$PIPX_BIN_DIR"
cp "$FAKE_PANOPTICON_SOURCE" "$PIPX_BIN_DIR/panopticon"
chmod 755 "$PIPX_BIN_DIR/panopticon"
""",
    )
    env = {
        **os.environ,
        "HOME": str(original_home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "PANOPTICON_TMP_HOME_KEEP": "1",
        "PANOPTICON_TMP_HOME_TRACE": str(trace),
        "FAKE_PANOPTICON_SOURCE": str(fake_panopticon),
        "PANOPTICON_CONFIG": "/should/not/leak",
        "CODEX_API_KEY": "should-not-leak",
        "DOCKER_CONTEXT": "orbstack-should-not-leak",
    }
    return env, target, trace


def test_launchers_are_executable_bash_scripts() -> None:
    for script in (COMMON, DEV, PROD):
        assert script.read_text().startswith("#!/usr/bin/env bash\n")
        assert stat.S_IMODE(script.stat().st_mode) & 0o111
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_dev_tmp_home_builds_the_checkout_and_copies_codex_auth(tmp_path: Path) -> None:
    env, _, trace = _fake_environment(tmp_path)
    completed = subprocess.run(
        [str(DEV)],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    output = completed.stdout
    home = Path(output.partition("Panopticon dev HOME: ")[2].splitlines()[0])
    assert home.is_dir()
    assert home == home.resolve()
    assert (home / ".codex" / "auth.json").read_text() == '{"auth_mode":"chatgpt"}'
    assert stat.S_IMODE((home / ".codex" / "auth.json").stat().st_mode) == 0o600
    observed = trace.read_text()
    assert "uv:build --wheel --out-dir" in observed
    assert "pipx:install --force" in observed
    assert (
        f"panopticon:quickstart|home={home}|config=unset|codex_key=unset|cwd={ROOT}|auth=yes"
        in observed
    )
    assert "runtime:tmp-dev-" in observed
    assert "|port=41873|service=http://127.0.0.1:41873" in observed
    assert "|container_service=http://host.docker.internal:41873" in observed
    tmux_dir = Path(observed.partition("|tmux=")[2].partition("|")[0])
    assert tmux_dir.parent == Path("/tmp").resolve()
    assert len(str(tmux_dir / "tmux-501" / "panopticon")) < 104
    assert not tmux_dir.exists()
    assert "|docker_host=unix:///tmp/fake-docker.sock|docker_context=unset" in observed
    assert "panopticon:stop" in observed


def test_dev_tmp_home_rejects_a_target_repository_argument(tmp_path: Path) -> None:
    env, target, trace = _fake_environment(tmp_path)
    completed = subprocess.run(
        [str(DEV), str(target)],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "usage: bin/dev-tmp-home" in completed.stderr
    assert not trace.exists()


def test_prod_tmp_home_installs_latest_without_copying_codex_auth(tmp_path: Path) -> None:
    env, target, trace = _fake_environment(tmp_path)
    completed = subprocess.run(
        [str(PROD), "latest", str(target)],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    home = Path(completed.stdout.partition("Panopticon prod HOME: ")[2].splitlines()[0])
    assert home.is_dir()
    assert not (home / ".codex" / "auth.json").exists()
    observed = trace.read_text()
    assert "pipx:install panopticon-next" in observed
    assert (
        f"panopticon:quickstart|home={home}|config=unset|codex_key=unset|cwd={target}|auth=no"
        in observed
    )


def test_tmp_home_coexists_with_existing_panopticon_runtime(tmp_path: Path) -> None:
    env, _, trace = _fake_environment(tmp_path)
    env["FAKE_TMUX_STATUS"] = "0"
    env["FAKE_DOCKER_CONTAINERS"] = "1"
    completed = subprocess.run(
        [str(DEV)],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Isolated service URL: http://127.0.0.1:41873" in completed.stdout
    assert "panopticon:quickstart" in trace.read_text()


def test_prod_tmp_home_retains_guard_for_an_older_release(tmp_path: Path) -> None:
    env, target, trace = _fake_environment(tmp_path)
    env["FAKE_TMUX_STATUS"] = "0"
    completed = subprocess.run(
        [str(PROD), "latest", str(target)],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "shared tmux -L panopticon server is already active" in completed.stderr
    assert "pipx:" not in trace.read_text()
