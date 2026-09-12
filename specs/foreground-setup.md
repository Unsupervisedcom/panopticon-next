# Foreground setup

## Overview

A local setup operation connects a selected coding agent and configures a repository before
opening the fleet. Credential presence is not a successful provider request. This operation
reuses runner-local private storage; it does not synchronize credentials between machines.

## Requirements

### 1: Entry and recovery

1. `panopticon setup` MUST offer agent credential configuration without requiring Docker, tmux,
   or a running task service.
2. `panopticon quickstart` MUST configure credentials in the foreground without creating an
   authentication task.
3. The repository screen's setup action MUST invoke the same foreground credential operation
   with the selected repository.
4. Reopening setup MUST derive missing steps from saved credential configuration rather than
   treating the control-plane bootstrap credential as completed agent setup.
5. Cancelling setup MUST retain credential steps that the operator already saved.
6. A missing infrastructure prerequisite MUST produce an actionable diagnostic before opening
   the fleet.

### 2: Credential transport and scope

1. A saved reusable agent connection MUST exclude forge tokens and unrelated environment values.
2. Applying a reusable connection to a repository MUST require an explicit selection of that
   connection for that repository.
3. Setup MUST preserve an existing repository's explicit credential binding unless the operator
   selects replacement.
4. A missing or invalid explicit repository credential MUST NOT silently use a reusable connection.
5. Configuring repository credentials MUST preserve unrelated values by copying them into a new
   uniquely named repository environment file while retaining the previous file unchanged.
6. Replacing one repository's credentials MUST NOT modify another repository's environment file.
7. A GitHub token entered during setup MUST be stored only in the selected repository's
   environment file.
8. A saved Codex subscription connection MUST retain one shared writable authentication directory
   across repositories that explicitly select that connection.
9. Setup readiness MUST inspect the credential transport available to task containers on the
   local runner, excluding inherited host login or environment credentials.
10. Setup MUST describe saved credentials as configured rather than claim provider authentication
    was verified without a successful provider request.
11. Applying a reusable connection MUST copy its harness environment values into the selected
    repository's own environment file, sharing only its credential-directory reference.
12. Replacing a reusable Codex connection MUST leave previously bound authentication directories
    unchanged.
13. Foreground setup MUST NOT adopt credentials from the host environment or native personal login.
14. Reusable connection records MUST remain local private files without being stored in the task service.

### 3: Private writes and interactive login

1. A credential write MUST replace its destination atomically with an owner-only regular file.
2. Setup MUST reject credential destinations that are symlinks or accessible to other users.
3. Cancelling or failing native login MUST leave the previous working connection unchanged.
4. Claude setup MUST accept a hidden paste after invoking `claude setup-token` directly when
   the operator chooses native login.
5. Foreground setup MUST NOT capture or scrape native login terminal output for tokens.
6. Codex native login MUST use a new private authentication directory without reading or writing
   the operator's personal Codex credentials.
7. A concurrent credential writer MUST be refused while another setup operation holds the
   same runner-local setup lock; exiting the owning process releases that lock.
8. The legacy setup workflow MUST acquire the same lock before offering credential mutation.

### 4: Existing work

1. Before changing a registered repository's credentials, foreground setup MUST pause its pending
   task launches through the task service's launch-hold operation.
2. Credential repair MUST NOT clear pending tasks' launch holds.
3. Foreground setup MUST refuse credential mutation while an existing legacy authentication
   session for that repository is running.
4. Legacy authentication task records MUST remain available after foreground setup.
5. A failed credential operation MUST retain the repository's previous credential references.
6. Registered-repository setup MUST refuse credential mutation when the task service cannot
   establish its launch hold.

## Compatibility

Explicit start, host, console, and existing setup-repo workflow APIs remain available. New setup
uses operator authorization; it does not grant ordinary task credentials repository-wide access.
The existing repository model stores env-file and credential-directory references. Reusable
connections are private runner-local configuration, explicitly copied or bound to each selected
repository; no implicit fallback applies to existing repositories.

## Operation order

Standalone setup saves a runner-local harness connection without fleet infrastructure. Quickstart
then selects a source, checks prerequisites, guards migration, starts and verifies services,
registers or reuses the source, and binds credentials under the repair hold. Registered-repository
repair requires the verified running service; offline setup never mutates existing repo bindings.
New repository files use unique opaque names. Codex re-login creates a new directory and only
success replaces the saved connection reference. Existing bound repos retain their prior account.
