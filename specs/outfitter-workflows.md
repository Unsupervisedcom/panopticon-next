# Outfitter catalog workflows

## Overview

Outfitter's community catalog publishes organization workflows as typed graphs
(`workflows/<id>/workflow.yaml`): actors, environments, integrations, and a DAG of nodes that each
perform an action or delegate to a nested workflow. Outfitter validates and distributes these
packages but never executes them. Panopticon runs lifecycles of exactly this shape, so every package the
operator's Outfitter `.agents` root provides is read and projected onto a Panopticon workflow —
no per-package code. Nothing is vendored: whatever catalog the root's `workflows/` directory
carries is what runs; the canonical implementation-plan packages (`founder`, `engineer`,
`software-factory`) are the motivating examples.

The projection keeps ADR 0004's rule that a workflow is code: the package supplies the node chain
and its descriptions, while Python owns the gate policy, the skills, and the tools. Each node
becomes one state in dependency order. Action nodes are agent work. A node that delegates to a
nested workflow is the operator's sign-off gate. Its label in all three packages is `REVIEW`, so a
declared reviewer launch pair engages the governed review task of `REQ-013` on entry.

## Requirements

### 1: Catalog resolution

1. A package MUST be resolved through the Outfitter `.agents` root — `$PANOPTICON_AGENTS`,
   defaulting to `~/.agents` — never from files shipped inside this repository.
2. Resolution MUST take the first `workflows/<id>/workflow.yaml` provided by the root itself and
   then by each `settings.yml` source in listed order.
3. A remote source MUST resolve at the root's `cache/repos/` checkout keyed by the unpadded
   URL-safe base64 of `<uri>#<ref>`, a `github` shorthand normalizing to
   `git+https://github.com/<owner>/<repo>.git`.
4. A `sources` list in `settings.local.yml` MUST replace the `settings.yml` list wholesale.
5. Loading a package that no layer provides MUST fail with an error naming the package and the
   root.
6. Loading a package whose node references a nested workflow that no layer provides MUST fail.

### 2: Contract validation

1. A package whose `version` is not `1` MUST be rejected.
2. A node that declares both `action` and `workflow`, or neither, MUST be rejected.
3. A node that references an actor, environment, integration, or needed node the package does
   not declare MUST be rejected.
4. An actor of kind `agent` without a `profile` MUST be rejected.
5. A package whose nodes do not form one chain — more than one root, a node needing more than
   one node, a node with more than one dependent, or a node unreachable from the root — MUST be
   rejected.

### 3: Projection

1. A loaded package MUST project to states in node dependency order, each labelled with the node
   id upper-cased with `.`, `-`, and `_` replaced by `_`, followed by `COMPLETE`.
2. A node that performs an action MUST project to an agent-advanced state whose only
   responsibility has the action name as its key and the node description as its description.
3. A node that delegates to a nested workflow MUST project to a user-advanced state.
4. A node whose actor is of kind `human` MUST project to a state that is entered on the user's
   turn, is user-advanced, and carries no responsibilities.
5. The description of a state projected from a node with an agent actor MUST name that actor's
   Outfitter profile.

### 4: Registration

1. Workflow discovery MUST register `outfitter-<id>` as an opt-in workflow for every package the
   `.agents` root provides that loads, validates, and projects — with no per-package code.
2. Each registered Outfitter workflow MUST leave `review_harness` and `review_model` unset.
3. When the `.agents` root provides no packages, discovery MUST complete without failing and
   without registering any Outfitter workflow.
4. A provided package that fails validation or projection MUST be skipped with a diagnostic,
   leaving every other package and workflow registered.

### 5: Skills and tools

1. An Outfitter workflow whose package opens a draft pull request MUST offer the `open-pr` and
   `babysit-ci` skills.
2. An Outfitter workflow whose package merges a pull request MUST offer the `babysit-merge`
   skill.
3. An Outfitter workflow whose package pushes as the human MUST offer the `push-branch` skill.
4. An Outfitter workflow whose package declares no GitHub integration MUST NOT declare the `gh`
   tool.

## Non-goals

- Panopticon never fetches or syncs a catalog itself: it reads only what Outfitter has already
  checked out under the `.agents` root, and upgrading the catalog is a change to that root's
  pinned sources, not to Panopticon.
- Outfitter's agent profiles, skills, prompt fragments, and MCP declarations are not composed by
  Panopticon; a task's harness and model remain the operator's choice.
- Packages whose nodes fan in or fan out are outside this change.
