# `2119-human-spec`

Defines a change as an RFC 2119 contract and reviewed tests before implementation. You
approve that contract, the agent builds and proves it, two independent reviewers challenge
the final diff, and you approve the reviewed PR before it can merge.

```
SPECIFYING → BUILDING → TESTING → REVIEWING → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any nonterminal state.)

**When to use:** consequential GitHub changes where you want to inspect the specification
before any implementation and inspect the reviewed result before merge.

This workflow is **opt-in**: enable it for a repo before it appears in the task-creation
picker. It sets no default interactive harness or model; explicit task settings, repository
defaults, and the application's fallback determine those separately from reviewer settings.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **SPECIFYING** | The agent writes individually addressable requirements under `specs/`, annotates genuine tests for every MUST-level requirement, obtains fresh-context test-honesty verdicts, and publishes the specification as a task artifact. | **You**, after reviewing the specification. |
| **BUILDING** | The agent implements the specification, makes reviewable commits, opens a draft PR, and records its URL on the task. | **The agent**, once every building responsibility is resolved. |
| **TESTING** | The agent runs the full test suite, the 2119 check, the repo's own local gate, and PR CI. | **The agent**, once every gate is green. |
| **REVIEWING** | Two fresh-context reviewers inspect the final diff, attempt targeted mutations, and publish evidence-bearing PR comments. The agent triages every finding, applies accepted fixes, reruns the testing gates, and publishes the review outputs and triage summary as task artifacts. | **You**: advancing to MERGING is your approval of the reviewed PR. |
| **MERGING** | The agent considers the triage summary's suggested follow-up issues, files those you endorsed or did not reject, and shepherds the PR through the merge queue. | **The agent**, which advances itself once the PR is merged. |
| **COMPLETE** | Terminal. The change has landed. | n/a |

Before leaving SPECIFYING, spec lint and `npx rfc2119 check` must pass, including annotated
coverage and recorded test-honesty verdicts. The tests themselves may still fail until
BUILDING implements the requirements. The 2119 check does **not** execute the test suite:
TESTING separately requires the full suite, the 2119 check, the repo's own gate (when one
exists, run under `pipefail`), and PR CI to pass. Use the repository's pinned CLI version
when its instructions specify one.

If the agent accepts a must-fix, it runs one fresh review round after retesting. Review is
capped at two rounds. A failed or unverified reviewer dispatch does not count as a clean
review. Every finding needs an explicit accept/reject decision and a reason.

These are the workflow's agent responsibilities and skill instructions. The control plane
checks responsibility status when advancing; it does not execute the suite or independently
audit every agent claim. The final-review helper additionally checks the supplied review
evidence before its triage and responsibility callbacks run.

## Reviewer defaults

The test-honesty reviewer defaults to `codex:gpt-5.6-sol`. The two final-review slots
default to `claude:claude-fable-5` and `codex:gpt-5.6-sol`, providing cross-provider review.
Repository reviewer settings can replace any of those defaults without changing the task's
interactive harness or model.

Final-review evidence includes the requested and observed model, verification source,
reviewed commit, review round, and review body. The helper requires the two final comments
in configured slot order for the final reviewed commit. Claude identity comes from JSON
`modelUsage`; Codex identity comes from the matching persisted rollout, a weaker source
than a documented stdout identity field. A mismatch, missing evidence, or dispatch failure
blocks acceptance.

Each reviewer chooses at least one targeted mutation of a specific claimed property and
applies it only in a throwaway copy, verifies tests actually run the mutated code, and
reports whether it was killed or survived. A survivor is an evidence defect; a kill alone
does not prove that the right assertion failed. The task checkout must remain unchanged.

## Repository overrides and artifacts

The repository fields `honesty_reviewer`, `reviewer_1`, and `reviewer_2` independently replace
the test-honesty reviewer and the two final-review slots. Values are atomic
`<harness>:<model>` pairs; supported reviewer harnesses are `claude` and `codex`, and a blank
value falls back to the workflow default. The model suffix remains opaque, including any
further colons. The runner transports these as `PANOPTICON_2119_HONESTY_REVIEWER`,
`PANOPTICON_2119_REVIEWER_1`, and `PANOPTICON_2119_REVIEWER_2`; resolution and reviewer calls
happen inside the task container. Ensure that container has credentials for the chosen
reviewers. The workflow layer installs Codex, and the base image supplies Claude Code.

The specification is a required task artifact. The review skill also directs the agent to
publish final review outputs and its triage summary; the universal `artifacts` skill asks
it to publish other reviewable documents, such as gate reports, as they are produced.
Read these durable documents with the dashboard's `a` hotkey. Merely writing a file in
`/workspace` does not publish it: the agent uses `put_artifact` over MCP, or the artifact
REST endpoint on a harness without MCP. These task artifacts complement the specification
and tests in the repository and review comments on the PR. Record the PR URL in the task's
external URL field for the `p` hotkey, rather than using an artifact as the link.

## Your part and the agent's part

- **You**: approve the specification, react to suggested follow-up issues in the triage PR
  comment, and approve the reviewed PR by advancing from REVIEWING.
- **The agent**: writes and publishes the contract, creates and gets the tests judged,
  implements only the specified change, proves it locally and in CI, runs and triages the
  final reviews, and merges after your approval.

## Skills

- **`spec-2119`** writes the specification and annotated tests, dispatches test-honesty
  reviews, records their verdicts, and publishes the specification artifact.
- **`open-pr`** opens a draft PR that references the published specification and records
  its URL on the task.
- **`babysit-ci`** watches CI and repairs failures or base conflicts.
- **`dual-review`** dispatches the configured final reviewers, verifies their evidence,
  requires targeted mutations, and drives triage and any re-review.
- **`babysit-merge`** files approved or unopposed deferred-work issues, then shepherds the
  PR through the merge queue.

## Related

- [`2119-auto-spec`](2119-auto-spec.md): the same reviewer defaults, without the human
  specification gate.
- [`2119-auto-sol`](2119-auto-sol.md): automatic specification with two Sol review slots
  by default.
- [Workflow catalog](README.md).
