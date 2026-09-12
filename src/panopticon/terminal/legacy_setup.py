"""Run the explicit legacy authentication workflow under the foreground writer lock."""

from __future__ import annotations

import importlib.resources
import os
import subprocess

from panopticon.harnesses.pi import API_KEY_ENV_VARS
from panopticon.terminal.setup_credentials import setup_lock


def main() -> int:
    from panopticon.workflows.setup_repo import SetupRepo

    task_lib = (importlib.resources.files("panopticon.sessionservice") / "task_lib.sh").read_text()
    environment = dict(os.environ, PANOPTICON_PI_API_KEY_ENV_VARS=" ".join(API_KEY_ENV_VARS))
    try:
        with setup_lock():
            return subprocess.run(
                ["sh", "-c", f"{task_lib}\n{SetupRepo().legacy_script()}"],
                env=environment,
                check=False,
            ).returncode
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Setup could not start: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
