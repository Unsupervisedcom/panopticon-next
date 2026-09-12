"""The selected environment applies to every process in an integrated shell pipeline."""

# 2119-spec: runtime-readiness
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from panopticon.terminal.session_environment import session_environment_command


# 2119: 1.3, 1.4, 1.5
def test_pipeline_producer_and_consumer_both_receive_selected_environment(tmp_path: Path) -> None:
    selected = {"HOME": str(tmp_path), "PATH": os.defpath, "PANOPTICON_DATA": "/selected/data"}
    observed = []
    commands = []
    for role in ("producer", "consumer"):
        path = tmp_path / (role + ".json")
        observed.append(path)
        script = (
            "import json,os,pathlib; pathlib.Path("
            + repr(str(path))
            + ").write_text(json.dumps(dict(os.environ)))"
        )
        commands.append(shlex.join([sys.executable, "-c", script]))
    pipeline = session_environment_command(" | ".join(commands), environment=selected)
    stale = {
        **os.environ,
        "PANOPTICON_DATA": "/stale/data",
        "PYTHONPATH": str(tmp_path / "stale-python"),
        "ANTHROPIC_API_KEY": "synthetic-stale-provider-token",
    }
    subprocess.run(["/bin/sh", "-c", pipeline], env=stale, check=True, timeout=5)
    for path in observed:
        child = json.loads(path.read_text())
        assert child["PANOPTICON_DATA"] == "/selected/data"
        assert "PYTHONPATH" not in child
        assert "ANTHROPIC_API_KEY" not in child
    assert "synthetic-stale-provider-token" not in pipeline


# 2119: 1.3, 1.4
def test_real_log_sink_cannot_import_a_stale_pythonpath_package(tmp_path: Path) -> None:
    stale_package = tmp_path / "stale-python" / "panopticon"
    stale_package.mkdir(parents=True)
    (stale_package / "__init__.py").write_text("raise RuntimeError('stale package imported')\n")
    log = tmp_path.resolve() / "service.log"
    producer = shlex.join([sys.executable, "-c", "print('expected service output')"])
    sink = shlex.join([sys.executable, "-m", "panopticon.terminal.log_tee", str(log)])
    command = session_environment_command(
        producer + " | " + sink, environment={"HOME": str(tmp_path), "PATH": os.defpath}
    )
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env={**os.environ, "PYTHONPATH": str(stale_package.parent)},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "expected service output\n"
    assert result.stdout == "expected service output\n"
