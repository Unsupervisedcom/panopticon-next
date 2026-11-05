# Merge Recovery State

## Overview

The shared `babysit-merge` skill sends a pull request that needs repair back to implementation.
The GitHub peer-reviewed and self-reviewed workflows call that state `ITERATING`;
the three RFC 2119 workflows call it `BUILDING`. Recovery instructions need to name
the implementation state that exists in the active workflow. Waiting for approval,
pending checks, or the merge queue does not establish that implementation needs repair.

For this contract, repair evidence means a merge conflict (`mergeable=CONFLICTING`
or `mergeStateStatus=DIRTY`), a failed or cancelled latest required-check outcome
for the current PR head, or
`reviewDecision=CHANGES_REQUESTED`. A `BLOCKED` merge state alone is not repair
evidence. Failed or cancelled outcomes for older heads or superseded check runs
are not repair evidence. The contract concerns the rendered agent instructions; it does not add
a runtime PR classifier or a new workflow state.

## Requirements

### 1: Workflow-specific recovery

1. For `2119-human-spec`, `2119-auto-spec`, and `2119-auto-sol`, the rendered
   `babysit-merge` instruction for an open PR with repair evidence MUST direct
   `set_state BUILDING`.
2. For `github-peer-reviewed` and `github-self-reviewed`, the rendered
   `babysit-merge` instruction for an open PR with repair evidence MUST direct
   `set_state ITERATING`.
3. The rendered `babysit-merge` instructions MUST NOT direct recovery to an
   implementation state without repair evidence.

Recovery uses the existing free-move contract in `REQ-009.5`: it bypasses the
normal transition graph and unresolved responsibilities, enters the destination,
and seeds fresh pending destination responsibilities.

### 2: Evidence before recovery

1. On each decision-tree invocation, the rendered `babysit-merge` instructions MUST
   obtain fresh PR state, mergeability, merge state, review decision, queue membership,
   and the latest required-check outcomes for the current PR head before deciding
   whether an open PR needs repair.
2. The rendered `babysit-merge` instructions MUST NOT treat pending required checks,
   failed optional checks, missing check results, or a check-query error as proof of
   a failed required check.
3. For an open PR, the rendered `babysit-merge` instructions MUST evaluate repair
   evidence before waiting for approval, checks, queue completion, or branch updates.
4. The rendered `babysit-merge` instructions MUST treat the recognized
   `no required checks reported` response as an empty required-check set that by
   itself establishes neither repair evidence, unavailable evidence, nor a
   pending-check wait, leaving other PR evidence to determine the next action.

### 3: Waits preserve the merge stage

1. Without repair evidence, the rendered `babysit-merge` instructions MUST keep the
   task in `MERGING` while approval is required, required checks are pending, the PR
   is queued, the branch is behind, check evidence is unavailable, or a `BLOCKED`
   merge state has no established cause.
2. For a `BLOCKED` open PR without repair evidence or an identified approval, check,
   or queue wait, the rendered `babysit-merge` instructions MUST direct the agent to
   report the unresolved blocker to the user and stop the current invocation.
3. For an approval wait without repair evidence, the rendered `babysit-merge`
   instructions MUST return control to the user while retaining the state artifact.
4. For a behind branch without repair evidence, the rendered `babysit-merge`
   instructions MUST require branch synchronization before attempting to queue the PR.
