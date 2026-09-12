# Skip Terminal Provisioner

## Overview

The host daemon revisits every task in each snapshot. A terminal task's disposable workspace can be
removed by the cleanup step, but a later pass still invokes the provisioner for that task. The
provisioner then runs Git against the deleted directory; the host's per-task exception boundary
swallows the error and logs a traceback on every subsequent pass.

The correction is deliberately narrower than skipping terminal tasks altogether. Cleanup still
needs to run after a task becomes terminal to release runtime resources. The host pass therefore
continues processing terminal tasks while bypassing only their provisioning step.

## Requirements

### 1: Terminal task handling

1. The host daemon MUST NOT invoke the provisioner for a terminal task.
2. The host daemon MUST invoke workspace cleanup for a terminal task.

### 2: Completed local results

1. Automatic cleanup of a completed `local-git-self-reviewed` task MUST preserve its recorded
   clone directory with the same HEAD and committed content as its completed merge result.
2. Automatic cleanup of a retention-enabled completed local task MUST leave its selected source
   unchanged, including checkout refs and contents or bundle bytes.
3. Retaining a completed local checkout MUST preserve runtime cleanup:
   a. A still-running task backend is stopped.
   b. The task's runtime authentication snapshots are removed.
   c. This runner's lingering task claim is released.
4. Dropped local tasks MUST retain the existing automatic checkout-deletion behavior.
5. Workflows that do not opt into completed-checkout retention MUST retain their existing
   automatic checkout-deletion behavior.
6. Subsequent cleanup passes MUST NOT recreate a missing retained checkout.

The workflow execution metadata declares the completed-checkout retention policy. The default is
ordinary deletion; `local-git-self-reviewed` opts into retention. This policy applies only to
completed checkout disposal, independently of runtime resource cleanup.
