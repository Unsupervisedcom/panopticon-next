# Task Authentication Readiness and Repair

## Overview

Credential repair preserves requested work while preventing a queued backlog from starting as a
side effect. An execution hold is independent of the agent's conversational blocked marker.
Credential checks inspect only the transport available to the selected task on its runner;
presence is not proof of successful provider authentication.

## Requirements

### 1: Repair admission

1. Beginning repair for a repository MUST prevent subsequent task claims in that repository
   until repair is explicitly finished, with claim admission and repair serialized at the service.
2. Beginning repair MUST pause each unclaimed nonterminal task in the selected repository.
3. Beginning repair MUST reject the operation before changing holds when the repository has a
   claimed nonterminal task with neither a live registration nor a failed launch or launch hold.
4. Beginning repair MUST preserve existing live tasks' claims, registrations, and execution.
5. A task created while its repository is under repair MUST remain paused after repair finishes.
6. Finishing repair MUST leave individually paused tasks paused until explicitly retried.
7. Repair admission and task pause state MUST survive task-service restart.
8. Changing a task's conversational blocked marker MUST NOT release its execution hold.

### 2: Retry

1. An explicit retry through the task retry operation MUST release only the selected nonterminal
   task's execution hold and claim when its repository is not under repair, it has no live
   registration, and it is unclaimed, paused, or has a failed launch.
2. An explicit retry MUST be rejected while the selected repository is under repair.
3. Pausing or retrying a task MUST preserve its repository, prompt, workflow state, and history.
4. An existing task or repository upgraded from a schema without execution holds MUST begin with
   its execution hold disabled.
5. The task retry operation MUST reject a claimed task whose launch is neither paused nor failed.

### 3: Runner checks

1. Before cloning or building for a container task without a previously recorded clone, the runner
   MUST check credential presence using only that repository's configured environment file and
   credential directory.
2. The credential-presence check MUST NOT use ambient host credentials, contact a provider, or
   invoke an agent.
3. Missing configured credentials MUST leave the claimed task paused with a failed launch reason
   naming the selected harness and foreground setup as the remedy.
4. Repairing credentials alone MUST NOT cause an authentication-failed task to start or heal.
5. A shell workflow MUST remain exempt from container credential-presence requirements.
6. Credential-presence checks MUST recognize the supported Claude gateway configuration, Codex
   API-key/access-token/credential-directory transports, and Pi credential transports.
7. A rejected claim that is not an ownership race MUST retain its rejection reason in runner
   diagnostics.
8. The runner MUST exclude paused tasks from fresh spawning, automatic healing, and startup
   claim release, including after service or runner restart.
9. A previously provisioned task MUST retain the existing in-container credential validation path
   when resuming, so credentials in its persistent configuration volume remain usable.

The runner check above is presence-only. The existing in-container harness authentication check
remains responsible for provider validation; this does not move that network probe into the host.
The dashboard's `R` action uses the explicit task retry operation for paused tasks.

### 4: Authority

1. Repair admission and explicit retry MUST require fleet-write authority rather than a task
   capability.
