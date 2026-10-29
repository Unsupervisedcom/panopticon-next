# Repository hook resolution

## Overview

This spec covers the host-side repository hook on the Docker spawn path described in
`docs/hooks.md`. A stored `hook_file` is a name relative to the runner's configured hooks
directory, including names in nested directories. The hooks directory follows the existing
config-directory override rules. Containment follows the existing path resolver, including
symlink resolution. Shell workflows and dashboard input behavior are unchanged.

## Requirements

### 1: Hook selection and execution

1. A Docker spawn with an executable stored hook name, including a nested relative name, MUST
   execute the script from the runner's configured hooks directory before image building,
   regardless of the runner's working directory.
2. The hook process MUST use the prepared per-task workspace as its working directory.
3. The hook process MUST receive the runner's environment with `PANOPTICON_TASK_ID`,
   `PANOPTICON_REPO_NAME`, and `PANOPTICON_WORKSPACE` set to the spawning task's ID, repository
   name, and absolute workspace path respectively.
4. A hook name that resolves outside the configured hooks directory MUST abort the spawn before
   executing that script or starting a container.
5. Each spawn MUST select its hook from the hooks directory configured at the time of that spawn.
6. A rejected hook name that resolves outside the configured hooks directory MUST leave the
   task's lifecycle as `failed` with the rejected hook name in the failure detail.

### 2: Optional hooks and failures

1. An unset or empty hook name MUST allow the spawn to continue without running a hook.
2. A hook name whose resolved target is within the configured hooks directory but missing or
   non-executable MUST allow the spawn to continue without running a hook.
3. A hook process that exits nonzero MUST abort the spawn before image building or container start.
4. A hook process that exits nonzero MUST leave the task's lifecycle as `failed` with its exit
   status in the failure detail.
