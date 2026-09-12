# REQ-054: New-user onboarding

## Overview

The release candidate must work as an evaluator experiences it: install a named build on a clean
machine, configure the intended repository without hidden state, supervise one human-controlled
GitHub task, and tear the local runtime down cleanly.

## Requirements

### REQ-054.1: Install identity

1. The new-user documentation MUST provide one canonical installation command that installs the
   latest published `panopticon-next` release, followed by a separate launch command.
2. Following the documented installation steps in a new isolated package environment MUST expose
   a `panopticon` executable that runs without importing code from a source checkout.
3. `panopticon --version` MUST report the installed distribution version and exit successfully.

### REQ-054.2: Side-effect-free repository selection and validation

1. Before repository registration, runtime startup, or task creation, `panopticon quickstart` MUST
   select and validate a repository source under `source-selection.1` and `source-selection.2`.
2. If the current working directory identifies a supported Git checkout, the command MUST: (a)
   offer it as the suggested source; and (b) allow the operator to enter a different supported
   source.
3. A Git checkout without `remote.origin.url` MUST remain selectable by its canonical local path.
4. Source-validation failure MUST leave repository registrations, task records, tmux servers and
   sessions, containers, and images unchanged from their pre-selection state. Harness setup that the
   operator explicitly saved before source selection may remain.

### REQ-054.3: Fresh-shell authentication

1. A valid default bootstrap credential MUST have a final pathname that is not a symlink, be a
   regular file owned by the effective user, grant no permissions to group or other users, and
   parse as a usable credential under REQ-035.
2. Given a valid default bootstrap credential and with `PANOPTICON_SERVICE_AUTH_FILE` and
   `PANOPTICON_SERVICE_AUTH_MODE` unset, `panopticon start`, `panopticon host`, and `panopticon
   quickstart` MUST authenticate using that credential, including when an integrated tmux session
   already exists.
3. Under the same conditions, `panopticon console`, `panopticon dashboard`, and `panopticon tasks`
   MUST select that credential before constructing their task-service client.
4. A client-only command MUST NOT create a missing default bootstrap credential.
5. Reading an existing credential MUST NOT create, replace, modify, chmod, or chown it.
6. If the default credential is invalid, the command MUST fail before constructing an authenticated
   client.
7. Failure under clause 6 MUST NOT print credential contents.
8. Failure under clause 6 MUST leave the pathname and any symlink target unchanged.

### REQ-054.4: First GitHub workflow choice

1. For a repository whose selected source is a supported GitHub HTTPS or SSH URL, successful
   quickstart MUST persist the union of the previously enabled workflows, `github-self-reviewed`,
   and `github-peer-reviewed`.
2. The new-user walkthrough MUST use `github-self-reviewed` for its first task and state that the
   initiating operator performs its approval.
3. The walkthrough MUST state separately that `github-peer-reviewed` requires another person to
   approve the pull request.

### REQ-054.5: Private repository environment template

1. If the documented secrets directory does not exist, quickstart MUST create it as a directory
   owned by the effective user with mode `0700`.
2. If the documented repository environment file does not exist, quickstart MUST create it as a
   non-symlinked regular file owned by the effective user with mode `0600`.
3. A newly created empty repository environment file MUST contain no live secret until the
   operator explicitly configures credentials through foreground setup.
4. If a valid repository environment file already exists, quickstart MUST retain that file
   unchanged, using a new private file when the operator selects credential replacement.
5. If the secrets directory or environment pathname is unsafe, quickstart MUST fail and identify
   the unsafe path.
6. Failure under clause 5 MUST NOT repair, replace, or modify the unsafe path.

### REQ-054.6: Evaluation teardown

1. After `panopticon stop` succeeds, every Panopticon task container started by that instance MUST
   be stopped or removed.
2. After `panopticon stop` succeeds, its dedicated tmux server MUST no longer exist.
3. `panopticon stop` MUST NOT delete Panopticon configuration, repository configuration,
   credentials, task records, or retained task output.
4. Repeating `panopticon stop` after successful teardown MUST succeed without recreating Panopticon
   resources.
5. The new-user walkthrough MUST state what is retained and provide commands that verify container
   and tmux teardown.

### REQ-054.7: Executable first-task walkthrough

1. The acceptance fixture MUST begin with no Panopticon configuration or data, no Panopticon tmux
   server, no Panopticon task container, and a supported disposable GitHub repository source.
2. Following only the documented walkthrough, with its documented placeholders supplied, MUST
   install Panopticon, complete quickstart, start the integrated services, authenticate a client
   command from a new shell, and complete one `github-self-reviewed` task.
3. The walkthrough MUST give an observable success check after quickstart, client authentication,
   task submission, container launch, session attach and detach, plan-artifact opening, approval,
   pull-request opening, task completion, and teardown.
4. No successful step MAY depend on an environment variable, credential transfer, command-line
   option, preexisting session, or seeded task that the walkthrough does not document.
5. Foreground setup MUST NOT report repository setup complete while the selected harness
   credentials or required GitHub token is absent from its task-container credential sources.
6. After foreground setup completes, the acceptance run MUST create the coding task and initiate every
   human approval through the documented dashboard and attached-agent inputs.
7. After foreground setup completes, the acceptance driver MUST NOT directly mutate task state through
   the REST API, MCP, a fixture, or a seeded task. Task mutations initiated by the real user inputs
   are permitted when carried out internally by Panopticon or the attached task agent.
8. The acceptance run MUST operate `panopticon quickstart` through a real terminal, use `t` to move
   that terminal from the dashboard to the selected task session, use the documented tmux detach
   keys to return, and prove that it returns to the same still-running dashboard.
9. The acceptance run MUST trigger artifact and pull-request opening with the documented dashboard
   keys and prove the exact artifact path and pull-request URL were handed to the host opener.
10. A headed-machine release check SHOULD confirm that the configured applications show the opened
    artifact and pull request to the operator.
