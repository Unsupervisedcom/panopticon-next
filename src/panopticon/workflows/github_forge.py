"""Shared base for the GitHub-forge workflows (ADR 0004, ADR 0005).

`GithubForgeWorkflow` carries everything common to workflows whose code reaches GitHub and
whose lifecycle is shepherded through a PR: the base-installed `gh` tool the agent reaches for
and the forge skills (`open-pr`, `babysit-ci`, `babysit-merge`) the agent drives against `gh`/CI.
The concrete lifecycles differ only in their **states** — a
peer gates the merge (`GithubPeerReviewed`) or the user self-reviews and approves it
(`GithubSelfReviewed`) — so each subclass supplies its own `name` + states and inherits the
forge plumbing from here.

The plan convention (artifact name, shared responsibilities, URI resolver, briefing hook)
lives on :class:`~panopticon.workflows.planned_workflow.PlannedWorkflow`; this class extends
it with the GitHub-specific surface (``gh`` tool and forge skills).

This base is **abstract**: it declares no `name` value and no states, so workflow discovery
(`workflows.discovery`) never registers or instantiates it — it keeps only classes with a
string `name` defined in the scanned module.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from panopticon.core.models import Responsibility, Skill, Tool
from panopticon.workflows.planned_workflow import PlannedWorkflow


class GithubForgeWorkflow(PlannedWorkflow):
    """Abstract base for GitHub-forge workflows: shared `gh` tool and forge skills.
    Concrete subclasses add a ``name`` and their states; they inherit the plumbing below.
    Not a registrable workflow on its own (no ``name``, no states).

    The plan convention (``PLAN_ARTIFACT_NAME``, ``PLAN_WRITTEN``, ``TOKEN_ESTIMATED``,
    :meth:`plan_uri`, :meth:`_briefing_extras`) is inherited from
    :class:`~panopticon.workflows.planned_workflow.PlannedWorkflow`."""

    merge_recovery_state: ClassVar[str] = "ITERATING"

    URL_RECORDED: ClassVar[Responsibility] = Responsibility(
        key="url-recorded",
        description="The PR URL is recorded on the task with the `set_url` MCP tool.",
    )

    def tools(self) -> Sequence[Tool]:
        """Name the base-installed `gh` executable so the agent reaches for it."""
        return (
            Tool(
                "gh",
                "the GitHub CLI — authenticated to the forge. Use it for all remote VCS: open and "
                "update the PR (`gh pr ...`), watch CI (`gh pr checks`), and merge. The forge skills "
                "drive it.",
            ),
        )

    def skills(self) -> Sequence[Skill]:
        """The forge skills (ADR 0004 — remote VCS is workflow-specific). The agent runs these
        in the container against `gh`/CI, calling back over MCP/REST."""
        return (
            Skill(
                "open-pr",
                "Open a draft PR for this task's branch.",
                f"1. Push the task's branch.\n"
                f"2. Open a **draft** PR against the repo's base branch with "
                f"`gh pr create --draft`. Title it for the change and reference the plan artifact "
                f"(`{self.PLAN_ARTIFACT_NAME}`).\n"
                f"3. Call the `set_url` MCP tool with the PR URL returned by `gh pr create`, "
                f"so the dashboard's `p` hotkey opens it and the `url-recorded` responsibility "
                f"can be resolved.",
            ),
            Skill(
                "babysit-ci",
                "Watch the PR's CI and fix failures (and base conflicts) until green.",
                "**Overview — push-driven watch with cross-turn state artifact.**\n"
                "Use `run_in_background` to arm a non-blocking CI watcher, then surrender the "
                "turn. On re-invocation (when the watcher finishes) pick up from the state "
                "artifact. This avoids occupying a turn for the full duration of CI.\n\n"
                "**State artifact: `.babysit-ci-state.json`** (task artifact store)\n"
                "Read with `ReadMcpResourceTool` (URI "
                "`panopticon://tasks/<task_id>/artifacts/.babysit-ci-state.json`); write/update "
                'with the `put_artifact` MCP tool (`name=".babysit-ci-state.json"`).\n'
                "Fields: `started_at` (ISO timestamp — budget anchor), `head_sha` (PR HEAD SHA "
                "at start), `watch_bash_id` (background task ID, `null` when none), `retries` "
                "(`{check_name: count}` map), `completed` (`false` while active; `true` when "
                "done or bailed — the cleanup signal, since artifacts cannot be deleted).\n\n"
                "**First invocation** (artifact absent OR `completed == true`):\n"
                "1. Run `gh pr view <pr> --json state,headRefOid,mergeable,mergeStateStatus` "
                "and branch on the result:\n"
                "   - `state=MERGED` → report and stop.\n"
                "   - `state=CLOSED` → report as unexpected and stop.\n"
                "   - `mergeStateStatus=DIRTY` / `mergeable=CONFLICTING` → conflicts present. "
                "**Do not watch CI** — a conflicting PR has no mergeable commit and `--watch` "
                "blocks forever. Fetch base, merge/rebase onto the branch, fix trivial conflicts "
                "and push. Bail to the user on a non-trivial conflict. After pushing, restart "
                "from Step 1.\n"
                "   - `mergeStateStatus=BEHIND` → branch is behind base but not conflicting. "
                "Run `gh pr update-branch` or merge base locally, then restart from Step 1.\n"
                "   - `mergeStateStatus=BLOCKED` / `UNSTABLE` → a required check is failing or "
                "a review is blocking; surface the details and don't spin.\n"
                "   - `mergeStateStatus=UNKNOWN` → GitHub is still computing mergeability; wait "
                "briefly and retry Step 1 — don't treat as ready.\n"
                "   - `mergeStateStatus=CLEAN` / `HAS_HOOKS` → proceed.\n"
                "2. Write the state artifact: "
                '`{"started_at": "<now ISO>", "head_sha": "<headRefOid>", '
                '"watch_bash_id": null, "retries": {}, "completed": false}`.\n'
                "3. Arm the background watcher with `run_in_background`:\n"
                "   `gh pr checks <pr> --watch 2>&1 | tee /tmp/babysit-ci-watch.log; "
                "echo EXIT:$? >> /tmp/babysit-ci-watch.log`\n"
                "4. Update the state artifact with the returned bash task ID (`watch_bash_id`).\n"
                "5. End the turn. The stop hook keeps `turn=agent` while the background task is "
                "running and fires the agent back when the watcher completes.\n\n"
                "**Re-invocation** (artifact present AND `completed == false`):\n"
                "1. Read the state artifact. If `started_at` is more than 2 h ago → write "
                '`{..., "completed": true}` and bail to the user with a timeout message.\n'
                "2. If `watch_bash_id` is set and the background task is still running → end "
                "the turn immediately (spurious re-invocation; the watcher fires us again when "
                "done).\n"
                "3. Read `/tmp/babysit-ci-watch.log`. Parse the `EXIT:N` trailer at the end.\n"
                "4. If `EXIT:0` → all checks passed. Run "
                "`gh pr view <pr> --json state,autoMergeRequest` to check whether the PR is in "
                "the merge queue:\n"
                '   - If `autoMergeRequest` is non-null: write `{..., "completed": true}` to '
                "the state artifact. Do NOT stop — the turn must stay on agent. Immediately run "
                "`/babysit-merge` to shepherd the PR through the merge queue.\n"
                '   - Otherwise: write `{..., "completed": true}` to the state artifact, '
                "report success, and stop (the stop hook flips turn to user — "
                "do not auto-advance).\n"
                "5. If `EXIT:` non-zero → extract failing check names from the log; increment "
                "their `retries` counters. If any counter exceeds 3 → write "
                '`{..., "completed": true}` and bail to the user. Otherwise: diagnose the '
                "failure, fix it in the worktree, commit and push. Overwrite the state artifact "
                "with fresh state (`watch_bash_id: null`, `completed: false`, updated `retries`, "
                "same `started_at`) to force a fresh watcher, then restart from First Invocation "
                "Step 1.\n\n"
                "**Anti-patterns to avoid:**\n"
                "- Never run `gh pr checks --watch` synchronously (blocking) — always wrap it "
                "in `run_in_background` as above. The **exit code** captured in the log is the "
                "pass/fail signal; never read the `conclusion` field to determine check status.\n"
                "- Never poll manually with a shell loop — use `run_in_background` + state "
                "artifact instead, or `gh run watch <run_id> --exit-status` for a single run.\n"
                "- Never use fixed-length SHA slices (`[:7]`, `[:8]`) for run matching — use "
                '`headSha.startswith("<short-sha>")` and gate on `status == "completed"` '
                "before reading any result field.\n"
                "- Never grep `displayTitle` — GitHub rewrites it on PR rename and the pattern "
                "will match the wrong run.",
            ),
            Skill(
                "babysit-merge",
                "Shepherd the PR through the merge queue.",
                "## State artifact\n\n"
                "Persist cross-turn state in the task artifact `.babysit-merge-state.json` "
                "(read via its MCP resource URI; write with `put_artifact`). "
                "This keeps task metadata with the task — no repo file, no gitignore entry — "
                "and survives container respawns.\n"
                "```json\n"
                '{ "started_at": "<ISO-8601>", "watch_bash_id": null, "requeue_count": 0 }\n'
                "```\n"
                "- `started_at`: set on first invocation; anchor for the 2h budget.\n"
                "- `watch_bash_id`: Bash task ID of the active background watcher (`null` when "
                "none is running).\n"
                "- `requeue_count`: how many times `gh pr merge --auto` has been called "
                "(cap at 5).\n\n"
                "## Decision tree (run on every entry and re-invocation)\n\n"
                "**1. Fetch PR state** (always fresh):\n"
                "`gh pr view <pr> --json "
                "state,headRefOid,mergeable,mergeStateStatus,reviewDecision,autoMergeRequest`\n"
                "If this read fails or the PR state is unknown, retain MERGING and the state "
                "artifact, report the unavailable evidence to the user, and stop.\n\n"
                "**2. Branch on `state`:**\n\n"
                "- **`MERGED`** → call the `advance` MCP operation (moves task to COMPLETE); "
                "delete the state artifact; stop.\n"
                "- **`CLOSED`** (not merged) → the PR was closed without merging. Do NOT advance. "
                "Delete the state artifact, leave a clear message for the user, and stop "
                "(the stop hook flips the turn to the user).\n"
                "- **`OPEN`** → continue to step 3.\n\n"
                "**3. Fetch required-check and queue evidence** (always fresh):\n"
                "`gh pr checks <pr> --required --json name,bucket,state,link`\n"
                "`gh pr checks` queries the current PR head. After the check query, re-read "
                "it with `gh pr view <pr> --json headRefOid`. If it differs from step 1, discard "
                "the evidence and restart the decision tree at step 1. If the re-read fails, "
                "mark check evidence unavailable.\n"
                "Use only the latest outcome of each required check for the current "
                "`headRefOid`. Ignore older-head and superseded failed/cancelled runs; if "
                "current-head/latest scope cannot be established, mark check evidence "
                "unavailable. Do not use optional-check failures as repair evidence.\n"
                "Read the returned buckets: `fail` and `cancel` are repair evidence; `pending` "
                "means wait; `pass` and `skipping` are not failures. Classify JSON buckets, "
                "not exit status; a pending or nonzero exit alone is not repair evidence. The recognized "
                "`no required checks reported` response means an empty required-check set: "
                "it establishes neither repair evidence, unavailable evidence, nor a "
                "pending-check wait; let the other PR evidence decide. Other query errors or "
                "missing/invalid results mean evidence is unavailable, not failed checks.\n"
                "Read actual queue membership separately (`autoMergeRequest` alone is not "
                "queue membership):\n"
                "`gh api graphql --field owner=<owner> --field name=<repo> --field number=<PR> --raw-field "
                "query='query($owner:String!,$name:String!,$number:Int!){repository("
                "owner:$owner,name:$name){pullRequest(number:$number){isInMergeQueue}}}'`\n"
                "Use `data.repository.pullRequest.isInMergeQueue`; a query error or missing "
                "value means queue evidence is unavailable. Do not infer false from a failed "
                "read.\n\n"
                "**4. Take the first matching branch** (repair before waits):\n\n"
                "Without repair evidence, never move to an implementation state. Every "
                "non-repair branch retains MERGING and the state artifact. A bare `BLOCKED` "
                "or `UNKNOWN` value is not repair evidence.\n\n"
                "- Repair evidence: `mergeable=CONFLICTING` or `mergeStateStatus=DIRTY`, "
                "a latest current-head required-check bucket of `fail` or `cancel`, or "
                "`reviewDecision=CHANGES_REQUESTED` → call "
                f"`set_state {self.merge_recovery_state}` with an explanation; "
                "delete the state artifact; stop.\n"
                "- If check or queue evidence is unavailable → report the unavailable evidence "
                "to the user and stop. Independent known repair evidence still takes precedence "
                "above.\n"
                "- If `mergeStateStatus=BEHIND` → tell the user to synchronize the branch with "
                "`babysit-ci` before attempting to queue; stop. Do not queue a behind branch.\n"
                "- If `reviewDecision=REVIEW_REQUIRED` → tell the user approval is required and "
                "stop, returning control to the user. Do not arm a check watcher for an "
                "approval wait.\n"
                "- If `autoMergeRequest` is non-null or `isInMergeQueue` is true → skip "
                "`gh pr merge --auto` (do not double-queue). If required checks are pending, "
                "go to step 5; otherwise tell the user to re-invoke `babysit-merge` after queue "
                "completion to observe `MERGED` and advance the task, then stop. A "
                "completed-check watcher does not wait for a merge event.\n"
                "- If required checks are pending → go to step 5 without queueing yet.\n"
                "- If `mergeStateStatus=BLOCKED`, mergeability is unknown, or readiness is "
                "otherwise unestablished → report the unresolved blocker to the user and stop. "
                "Do not enqueue or loop on unknown evidence.\n"
                "- Otherwise, only when mergeability is established and `mergeStateStatus` "
                "is `CLEAN` or `HAS_HOOKS` → run `gh pr merge --squash --auto`; increment "
                "`requeue_count` in the state artifact. If `requeue_count` > 5, bail to the "
                "user and stop. After enqueueing, tell the user to re-invoke `babysit-merge` "
                "after queue completion to observe `MERGED` and advance the task, then stop "
                "with the state artifact retained.\n\n"
                "**5. Arm background watcher** (only for known pending required checks):\n"
                "```\n"
                "run_in_background: gh pr checks <PR> --required --watch 2>&1 "
                "| tee /tmp/babysit-merge-watch.log; "
                "echo EXIT:$? >> /tmp/babysit-merge-watch.log\n"
                "```\n"
                "Record the returned Bash task ID as `watch_bash_id` in the state artifact. "
                "End the turn (the watcher runs in the background).\n\n"
                "## Re-invocation (state artifact exists from a prior turn)\n\n"
                "1. Read the state artifact. If `started_at` is more than 2h ago → bail to the "
                "user, delete the state artifact, and stop.\n"
                "2. If `watch_bash_id` is set and that background Bash task is **still running** → "
                "end the turn immediately (nothing to do yet).\n"
                "3. If a watcher completed, read `/tmp/babysit-merge-watch.log` and find the "
                "last `EXIT:N` line. A prior wait may have no watcher or log.\n"
                "4. Clear `watch_bash_id` in the state artifact (`null`).\n"
                "5. Re-enter the decision tree from step 1 above. Repeat until merged, blocked, "
                "or budget exceeded.",
            ),
        )
