# Outfitter catalog workflows: `outfitter-founder`, `outfitter-engineer`, `outfitter-software-factory`

The [Outfitter community catalog](https://github.com/ai-outfitter/community-profiles) publishes
organization workflows as typed graphs (`workflows/<id>/workflow.yaml`): who acts (human, agent,
tool, or system actors), where (environments), with what (integrations), and a DAG of nodes that
each perform an action or delegate to a nested workflow. Outfitter validates and distributes these
packages and deliberately never executes them. Panopticon runs lifecycles of exactly this shape,
so **every package your `.agents` root provides registers as a workflow named `outfitter-<id>`** —
no per-package code. The canonical implementation-plan packages look like:

| Workflow | Package | Lifecycle |
|---|---|---|
| `outfitter-founder` | `founder` | `WORK → COMMIT → REVIEW → PUSH → COMPLETE` |
| `outfitter-engineer` | `engineer` | `RESEARCH → DEVELOP → DRAFT → REVIEW → MERGE → COMPLETE` |
| `outfitter-software-factory` | `software-factory` | `PREPARE → IMPLEMENT → DRAFT → CI → REVIEW → MERGE → COMPLETE` |

(plus `DROPPED`, reachable from any state.)

The packages are resolved through the operator's Outfitter **`.agents` root**
(`$PANOPTICON_AGENTS`, default `~/.agents`) when the service builds its workflow registry — the
same layered graph Outfitter resolves: the root's own `workflows/` directory first, then each
`sources` entry of `settings.yml` (`settings.local.yml` replaces the list wholesale) in listed
order, a remote source at the checkout Outfitter caches under `cache/repos/`. Nothing is
vendored: the pin is the source ref in the `.agents` settings, so upgrading the catalog is an
`.agents` change, not a Panopticon release. On a host
whose root does not provide the packages, discovery skips these workflows with a log line — never
a startup failure. The `adversarial-review` package must be present too, because all three nest
it.

All three are **opt-in**: enable them for a repo before they appear in the task-creation picker.

## How a package becomes a lifecycle

Each node becomes one state, in dependency order, labelled by its node id. The gate policy is
Panopticon's, not the catalog's:

| Node kind | Projected state |
|---|---|
| Performs an `action` | Agent work. The agent enters on its own turn, has one responsibility (the node's description, keyed by the action name), and advances itself once it is met. |
| Delegates to a nested `workflow` (`adversarial-review`) | **Your sign-off gate.** User-advanced. Its label is `REVIEW`, so a repo that declares a reviewer launch pair gets the governed cross-model review task (ADR 0014, REQ-013) on entry; otherwise you review and advance. |
| Actor is a `human` | Your work: entered on your turn, user-advanced, no agent responsibilities. |
| Actor is a `system` (the platform merging after every gate) | Observed by the agent, which advances once the platform has acted. |

Packages whose nodes fan in or out are skipped with a log line (a Panopticon happy path is a
line), as is any package that fails Outfitter's contract — one broken package never blocks the
rest of the catalog or service startup.

## Lifecycles

### `outfitter-founder`

Ship a verified local change as a human-authenticated founder agent with independent review.

| State | What happens | Who advances |
|---|---|---|
| **WORK** | Implement and verify the approved change locally. | The agent. |
| **COMMIT** | Record the verified change locally. | The agent. |
| **REVIEW** | Get an independent review before pushing. | **You**, once the verdict is addressed. |
| **PUSH** | Publish the approved commit as the human (`push-branch` skill: clean tree, push the task branch, record its URL). | The agent. |

### `outfitter-engineer`

Research, implement, review, and merge changes through a human-authenticated engineering agent.

| State | What happens | Who advances |
|---|---|---|
| **RESEARCH** | Investigate the problem and solution space. | The agent. |
| **DEVELOP** | Implement and refine the change locally. | The agent. |
| **DRAFT** | Open a draft pull request as the human (`open-pr`), then keep CI green (`babysit-ci`). | The agent. |
| **REVIEW** | Request an independent review after CI passes. | **You**, once the verdict is addressed. |
| **MERGE** | Merge the approved pull request as the human (`babysit-merge`). | The agent. |

### `outfitter-software-factory`

Delegate typed issues to a resident engineer for CI-gated implementation and independent review.

| State | What happens | Who advances |
|---|---|---|
| **PREPARE** | Accept one typed issue. | The agent. |
| **IMPLEMENT** | Implement and verify the requested change on a semantic branch. | The agent. |
| **DRAFT** | Publish the verified implementation as a draft pull request (`open-pr`). | The agent. |
| **CI** | Wait for required checks to pass (`babysit-ci`). | The agent. |
| **REVIEW** | Request an independent review. | **You**, once the verdict is addressed. |
| **MERGE** | Let the platform merge after every required gate (`babysit-merge` watches the queue). | The agent. |

## Skills and tools

The skills are the existing GitHub-forge procedures, selected by the actions a package performs:
`open-draft-pr` → `open-pr` + `babysit-ci`, `wait-for-required-ci` → `babysit-ci`, the merge
actions → `babysit-merge`, and `push-as-human` → `push-branch`. The `gh` tool is declared whenever
the package reaches GitHub, as a CLI or through the GitHub MCP server.

## Harness and profile

A task's harness is your choice, as for any workflow. Under the `outfitter` harness the task's
starting model is the Outfitter **profile id**; each state's description names the actor profile
the package expects (`founder`, `engineer`, `resident-engineer`), so pick that profile when you
create the task. Under `claude`, `codex`, or `pi`, the state descriptions and skills carry the same
lifecycle without Outfitter's composed profile.

## What is not projected

Panopticon never fetches or syncs a catalog itself — it reads only what Outfitter has already
checked out under the `.agents` root — and it does not compose the
packages' agent profiles, skills, prompt fragments, or MCP declarations. Environments and
integrations are surfaced in the state descriptions for the agent, not provisioned.

## Related

- [`github-self-reviewed`](github-self-reviewed.md): the forge plumbing these workflows reuse.
- [ADR 0004](../design/decisions/0004-workflow-abstraction.md): why the gate policy and skills
  live in code while the graph comes from the package.
