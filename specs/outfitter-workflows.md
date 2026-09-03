# Outfitter catalog workflows

## Overview

Outfitter's community catalog publishes organization workflows as typed graphs
(`workflows/<id>/workflow.yaml`): actors, environments, integrations, and a DAG of nodes that each
perform an action or delegate to a nested workflow. Outfitter validates and distributes these
packages but never executes them. Panopticon runs lifecycles of exactly this shape, so the three
canonical implementation-plan packages — `founder`, `engineer`, and `software-factory` — are
vendored from a pinned community-profiles release and projected onto Panopticon workflows.

The projection keeps ADR 0004's rule that a workflow is code: the package supplies the node chain
and its descriptions, while Python owns the gate policy, the skills, and the tools. Each node
becomes one state in dependency order. Action nodes are agent work. A node that delegates to a
nested workflow is the operator's sign-off gate. Its label in all three packages is `REVIEW`, so a
declared reviewer launch pair engages the governed review task of `REQ-013` on entry.

## Requirements

### 1: Pinned vendored packages

1. Loading a vendored package whose content digest differs from its pinned digest MUST fail with
   an error that names the package and the pinned catalog release.
2. The vendored `founder`, `engineer`, `software-factory`, and `adversarial-review` packages MUST
   each load with the node ids the pinned community-profiles release declares for them.
3. Loading a package whose node references a nested workflow that is not a loadable vendored
   package MUST fail.

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

1. Workflow discovery MUST register `outfitter-founder`, `outfitter-engineer`, and
   `outfitter-software-factory` as opt-in workflows.
2. Each registered Outfitter workflow MUST leave `review_harness` and `review_model` unset.

### 5: Skills and tools

1. An Outfitter workflow whose package opens a draft pull request MUST offer the `open-pr` and
   `babysit-ci` skills.
2. An Outfitter workflow whose package merges a pull request MUST offer the `babysit-merge`
   skill.
3. An Outfitter workflow whose package pushes as the human MUST offer the `push-branch` skill.
4. An Outfitter workflow whose package declares no GitHub integration MUST NOT declare the `gh`
   tool.

## Non-goals

- Panopticon does not sync, resolve, or execute an Outfitter catalog at runtime; the packages are
  vendored and upgraded by an explicit change to the file and its digest.
- Outfitter's agent profiles, skills, prompt fragments, and MCP declarations are not composed by
  Panopticon; a task's harness and model remain the operator's choice.
- Packages whose nodes fan in or fan out are outside this change.
