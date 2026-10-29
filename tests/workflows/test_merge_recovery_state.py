"""The merge babysitter's rendered recovery command works in every forge lifecycle."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from panopticon.core.models import Status
from panopticon.workflows.discovery import discover_workflows


@pytest.mark.parametrize(
    ("workflow_name", "expected_state"),
    [
        ("2119-human-spec", "BUILDING"),
        ("2119-auto-spec", "BUILDING"),
        ("2119-auto-sol", "BUILDING"),
        ("github-peer-reviewed", "ITERATING"),
        ("github-self-reviewed", "ITERATING"),
    ],
)
def test_rendered_merge_recovery_enters_implementation_with_fresh_responsibilities(
    workflow_name: str, expected_state: str
) -> None:
    # 2119: merge-recovery-state.1.1
    # 2119: merge-recovery-state.1.2
    workflow = discover_workflows(_home_workflows=Path("/nonexistent"))[workflow_name]
    instructions = next(
        skill.instructions for skill in workflow.skills() if skill.name == "babysit-merge"
    )
    recovery = re.search(
        r"- Repair evidence: `mergeable=CONFLICTING` or `mergeStateStatus=DIRTY`, "
        r"a latest current-head required-check bucket of `fail` or `cancel`, or "
        r"`reviewDecision=CHANGES_REQUESTED` → call `set_state ([A-Z]+)` with an explanation; "
        r"delete the state artifact; stop\.",
        instructions,
    )
    assert recovery is not None
    rendered_target = recovery.group(1)

    task = workflow.start_task("task", "repo", at="t0")
    workflow.force_transition(task, expected_state, at="t1", trigger="set-state")
    for responsibility in task.outstanding_responsibilities:
        task.resolve_responsibility(key=responsibility.key, status=Status.MET)
    previous_implementation = task.current_entry
    workflow.force_transition(task, "MERGING", at="t2", trigger="set-state")
    merging = task.current_entry
    assert task.outstanding_responsibilities
    assert set(workflow.transitions("MERGING")) == {"COMPLETE", "DROPPED"}

    workflow.force_transition(task, rendered_target, at="t3", trigger="set-state")

    assert rendered_target == expected_state
    assert task.state == expected_state
    assert task.current_entry.from_state == "MERGING"
    assert task.current_entry.to_state == expected_state
    assert task.current_entry is not previous_implementation
    assert task.current_entry.responsibilities == list(workflow.responsibilities(expected_state))
    assert task.current_entry.responsibilities
    assert all(item.status is Status.PENDING for item in task.current_entry.responsibilities)
    assert all(item.status is Status.MET for item in previous_implementation.responsibilities)
    assert all(item.status is Status.PENDING for item in merging.responsibilities)


@pytest.mark.parametrize(
    ("workflow_name", "expected_state"),
    [
        ("2119-human-spec", "BUILDING"),
        ("2119-auto-spec", "BUILDING"),
        ("2119-auto-sol", "BUILDING"),
        ("github-peer-reviewed", "ITERATING"),
        ("github-self-reviewed", "ITERATING"),
    ],
)
def test_merge_instructions_distinguish_repair_evidence_from_waits(
    workflow_name: str, expected_state: str
) -> None:
    # 2119: merge-recovery-state.1.3
    # 2119: merge-recovery-state.2.1
    # 2119: merge-recovery-state.2.2
    # 2119: merge-recovery-state.2.3
    # 2119: merge-recovery-state.2.4
    # 2119: merge-recovery-state.3.1
    # 2119: merge-recovery-state.3.2
    # 2119: merge-recovery-state.3.3
    # 2119: merge-recovery-state.3.4
    workflow = discover_workflows(_home_workflows=Path("/nonexistent"))[workflow_name]
    instructions = next(
        skill.instructions for skill in workflow.skills() if skill.name == "babysit-merge"
    )
    # The artifact is the agent's decision procedure, not a Python classifier. Assert
    # the complete ordered policy so a conflicting branch or unconditional watcher
    # cannot satisfy this contract by merely mentioning the expected evidence words.
    expected_policy = """## Decision tree (run on every entry and re-invocation)

**1. Fetch PR state** (always fresh):
`gh pr view <pr> --json state,headRefOid,mergeable,mergeStateStatus,reviewDecision,autoMergeRequest`
If this read fails or the PR state is unknown, retain MERGING and the state artifact, report the unavailable evidence to the user, and stop.

**2. Branch on `state`:**

- **`MERGED`** → call the `advance` MCP operation (moves task to COMPLETE); delete the state artifact; stop.
- **`CLOSED`** (not merged) → the PR was closed without merging. Do NOT advance. Delete the state artifact, leave a clear message for the user, and stop (the stop hook flips the turn to the user).
- **`OPEN`** → continue to step 3.

**3. Fetch required-check and queue evidence** (always fresh):
`gh pr checks <pr> --required --json name,bucket,state,link`
`gh pr checks` queries the current PR head. After the check query, re-read it with `gh pr view <pr> --json headRefOid`. If it differs from step 1, discard the evidence and restart the decision tree at step 1. If the re-read fails, mark check evidence unavailable.
Use only the latest outcome of each required check for the current `headRefOid`. Ignore older-head and superseded failed/cancelled runs; if current-head/latest scope cannot be established, mark check evidence unavailable. Do not use optional-check failures as repair evidence.
Read the returned buckets: `fail` and `cancel` are repair evidence; `pending` means wait; `pass` and `skipping` are not failures. Classify JSON buckets, not exit status; a pending or nonzero exit alone is not repair evidence. The recognized `no required checks reported` response means an empty required-check set: it establishes neither repair evidence, unavailable evidence, nor a pending-check wait; let the other PR evidence decide. Other query errors or missing/invalid results mean evidence is unavailable, not failed checks.
Read actual queue membership separately (`autoMergeRequest` alone is not queue membership):
`gh api graphql --field owner=<owner> --field name=<repo> --field number=<PR> --raw-field query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){isInMergeQueue}}}'`
Use `data.repository.pullRequest.isInMergeQueue`; a query error or missing value means queue evidence is unavailable. Do not infer false from a failed read.

**4. Take the first matching branch** (repair before waits):

Without repair evidence, never move to an implementation state. Every non-repair branch retains MERGING and the state artifact. A bare `BLOCKED` or `UNKNOWN` value is not repair evidence.

- Repair evidence: `mergeable=CONFLICTING` or `mergeStateStatus=DIRTY`, a latest current-head required-check bucket of `fail` or `cancel`, or `reviewDecision=CHANGES_REQUESTED` → call `set_state <recovery-state>` with an explanation; delete the state artifact; stop.
- If check or queue evidence is unavailable → report the unavailable evidence to the user and stop. Independent known repair evidence still takes precedence above.
- If `mergeStateStatus=BEHIND` → tell the user to synchronize the branch with `babysit-ci` before attempting to queue; stop. Do not queue a behind branch.
- If `reviewDecision=REVIEW_REQUIRED` → tell the user approval is required and stop, returning control to the user. Do not arm a check watcher for an approval wait.
- If `autoMergeRequest` is non-null or `isInMergeQueue` is true → skip `gh pr merge --auto` (do not double-queue). If required checks are pending, go to step 5; otherwise tell the user to re-invoke `babysit-merge` after queue completion to observe `MERGED` and advance the task, then stop. A completed-check watcher does not wait for a merge event.
- If required checks are pending → go to step 5 without queueing yet.
- If `mergeStateStatus=BLOCKED`, mergeability is unknown, or readiness is otherwise unestablished → report the unresolved blocker to the user and stop. Do not enqueue or loop on unknown evidence.
- Otherwise, only when mergeability is established and `mergeStateStatus` is `CLEAN` or `HAS_HOOKS` → run `gh pr merge --squash --auto`; increment `requeue_count` in the state artifact. If `requeue_count` > 5, bail to the user and stop. After enqueueing, tell the user to re-invoke `babysit-merge` after queue completion to observe `MERGED` and advance the task, then stop with the state artifact retained.

**5. Arm background watcher** (only for known pending required checks):
```
run_in_background: gh pr checks <PR> --required --watch 2>&1 | tee /tmp/babysit-merge-watch.log; echo EXIT:$? >> /tmp/babysit-merge-watch.log
```
Record the returned Bash task ID as `watch_bash_id` in the state artifact. End the turn (the watcher runs in the background).

""".replace("<recovery-state>", expected_state)
    assert "## Pre-queue mergeability check" not in instructions
    start = instructions.index("## Decision tree")
    end = instructions.index("## Re-invocation")
    assert instructions[start:end] == expected_policy
    assert instructions.count("`set_state ") == 1
