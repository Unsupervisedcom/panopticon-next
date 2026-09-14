"""The provisioning skill + nudge (ADR 0010/0011) — agnostic; every task provisions.

A task starts with no slug and so no branch: it works in a writable per-task clone at ``/workspace``
(on the base branch). Once the agent understands the task well enough to **name** it, it runs the
``provision`` skill — choose a slug, set it (`set_slug`), and the session service branches the clone
``panopticon/<slug>``. The slug-check hook nudges the agent toward this skill on each user turn while
the task is still unslugged (ARCHITECTURE §8.3 — the slug is decided in the container).

This is data — a skill spec + a prompt string — so it lives in ``core`` (LLM-free): the task service
exposes the skill on every task (`TaskService.skills`), and the container's hook emits the nudge.
"""

from __future__ import annotations

from panopticon.core.models import Skill

#: The skill's name — shared by the skill spec and the nudge so they can't drift.
PROVISION_SKILL_NAME = "provision"

PROVISION_SKILL = Skill(
    PROVISION_SKILL_NAME,
    "Name the task (set its slug) so the session service creates your branch.",
    "You work in `/workspace` — a writable checkout of the repo, on its base branch. Once you "
    "understand the task well enough to name it, choose a short kebab-case **slug** (e.g. "
    "`fix-login-redirect`) and set it with the `set_slug` tool. If your harness has no MCP "
    "client, replace `<slug>` below with your chosen slug and run this Python command. It uses "
    "Panopticon's installed REST client and the task's existing runtime credential, without "
    "putting that credential in command arguments:\n\n"
    "```sh\n"
    "python - <<'PY'\n"
    "import os\n"
    "import httpx\n"
    "from panopticon.client import TaskServiceClient\n"
    'with httpx.Client(base_url=os.environ["PANOPTICON_SERVICE_URL"], trust_env=False) as http:\n'
    '    TaskServiceClient(http).set_slug(os.environ["PANOPTICON_TASK_ID"], "<slug>")\n'
    "PY\n"
    "```\n\n"
    "The session service then creates "
    "your feature branch `panopticon/<slug>` in `/workspace` and points `origin` at the forge. "
    "**Don't commit until your branch exists** — confirm `git -C /workspace branch --show-current` "
    "reads `panopticon/<slug>` (give the session service a moment if it doesn't yet). Then do the "
    "task's work on that branch.",
)

#: Emitted by the slug-check hook on each user turn while the task is unslugged (ADR 0011 §3).
PROVISION_NUDGE = (
    "This task has no slug yet, so it has no branch. Once the user has given you enough to name "
    f"the task, read and follow the `{PROVISION_SKILL_NAME}` skill's instructions to set a slug and "
    "create your branch — don't commit before then."
)
