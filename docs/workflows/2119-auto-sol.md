# `2119-auto-sol`

Runs the RFC 2119 specification, implementation, and proof stages autonomously. By default,
it sends the final diff through two independent Sol review contexts before waiting for your
approval.
It trades cross-model diversity for a predictable same-model review path.

```
SPECIFYING → BUILDING → TESTING → REVIEWING → MERGING → COMPLETE
```

(plus `DROPPED`, reachable from any nonterminal state.)

**When to use:** GitHub changes where the agent may proceed from specification into
implementation without waiting and two independently dispatched Sol reviews are the intended
default.

This workflow is **opt-in**: enable it for a repo before it appears in the task-creation
picker. It sets no default interactive harness or model; explicit task settings, repository
defaults, and the application's fallback determine those separately from reviewer settings.

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **SPECIFYING** | The agent writes individually addressable requirements under `specs/`, annotates genuine tests for every MUST-level requirement, obtains fresh-context test-honesty verdicts, and publishes the specification as a task artifact. | **The agent**, which proceeds directly to implementation once the specification responsibilities are resolved. |
| **BUILDING** | The agent implements the specification, makes reviewable commits, opens a draft PR, and records its URL on the task. | **The agent**, once every building responsibility is resolved. |
| **TESTING** | The agent runs the full test suite, the 2119 check, the repo's own local gate, and PR CI. | **The agent**, once every gate is green. |
| **REVIEWING** | Two independently dispatched, fresh-context reviewers inspect the final diff, attempt targeted mutations, and publish evidence-bearing PR comments. The agent triages every finding, applies accepted fixes, reruns the testing gates, and publishes the review outputs and triage summary as task artifacts. | **You**: advancing to MERGING is your approval of the reviewed PR. |
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

## Same-model independence

The test-honesty reviewer and both final-review slots default to
`codex:gpt-5.6-sol`. The default final reviews run in separate fresh contexts and each reviewer
chooses its own targeted mutation, preserving independence from the author and from the other
review context. They do **not** provide cross-model diversity.

Repository reviewer settings can replace any default, so “Sol” describes this workflow's
shipped configuration rather than a lock.

Final-review evidence carries the requested and observed model, verification source,
reviewed commit, review round, and review body. The helper checks the two final comments in
configured slot order against the final reviewed commit; failed dispatches, missing or
ambiguous evidence, and identity mismatches block acceptance. See the
[shared verification rules](2119-human-spec.md#reviewer-defaults).

Each reviewer attempts at least one targeted mutation in a throwaway copy, verifies tests
actually run the mutated code, and reports killed or survived. A survivor is an evidence
defect; a kill alone does not establish why the test failed. The task checkout must remain
unchanged.

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

- **You**: react to suggested follow-up issues in the triage PR comment and approve the
  reviewed PR by advancing from REVIEWING.
- **The agent**: writes and publishes the contract, creates and gets the tests judged,
  implements only the specified change, proves it locally and in CI, runs and triages two
  configured final reviews, and merges after your approval.

## Skills

- **`spec-2119`** writes the specification and annotated tests, dispatches test-honesty
  reviews, records their verdicts, and publishes the specification artifact.
- **`open-pr`** opens a draft PR that references the published specification and records
  its URL on the task.
- **`babysit-ci`** watches CI and repairs failures or base conflicts.
- **`dual-review-sol`** dispatches the configured final reviewers, verifies their evidence,
  requires targeted mutations, and drives triage and any re-review.
- **`babysit-merge`** files approved or unopposed deferred-work issues, then shepherds the
  PR through the merge queue.

## Related

- [`2119-auto-spec`](2119-auto-spec.md): the same automatic lifecycle with Fable-plus-Sol
  final-review defaults.
- [`2119-human-spec`](2119-human-spec.md): adds a human specification gate and defaults to
  cross-provider final review.
- [Workflow catalog](README.md).
