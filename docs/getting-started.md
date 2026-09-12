# New-user walkthrough

This walkthrough proves the evaluator path on a macOS or Linux machine: install Panopticon, check
the host, configure one disposable GitHub repository, supervise one self-reviewed task, open its
plan and pull request, and stop the local runtime. Use a repository where creating and merging a
small test pull request is safe.

## 1. Install and verify the host

Panopticon uses pipx so its command is isolated from system Python packages. If `command -v pipx`
prints nothing, install pipx for your host:

```sh
# macOS
brew install pipx

# Ubuntu or Debian
sudo apt-get update
sudo apt-get install --yes pipx
```

Add pipx's application directory to PATH for future shells:

```sh
pipx ensurepath
```

**Close this terminal and open a new one** so the PATH change takes effect. The current evaluator
build is supplied as a wheel beside this walkthrough. Open the extracted evaluator-bundle
directory in the new terminal and install that wheel in an isolated environment:

```sh
PANOPTICON_RELEASE_VERSION=0.2.11 # x-release-please-version
pipx install "./panopticon_next-${PANOPTICON_RELEASE_VERSION}-py3-none-any.whl"
panopticon --version
panopticon doctor
```

For the published release, run `pipx install panopticon-next`, then `panopticon quickstart`.
The local wheel path above also supports an independently checksummed evaluation.

[//]: # (x-release-please-start-version)
Expected result: `panopticon --version` prints `panopticon 0.2.11` without requiring a source
checkout, and `doctor` ends with `All prerequisites satisfied.` Do not continue until both checks
pass. `doctor` checks Python 3.11+, Git, the Docker CLI and daemon, tmux, and at least one agent
harness. Follow the corrective action printed beneath any failed check, then rerun it.
[//]: # (x-release-please-end)

The complete GitHub walkthrough below currently requires Claude or Codex. Pi and Outfitter can run
compatible workflows, but their current adapters cannot execute workflow skills that require
Panopticon's MCP tools.

On macOS, use OrbStack or Docker Desktop and see [macOS setup](macos-setup.md). On Linux, the
authenticated task service binds to `0.0.0.0` so bridge containers can reach it. Restrict inbound
access with a host firewall or encrypted, access-controlled transport before using Panopticon on
an untrusted network; see [authentication](auth.md).

## 2. Connect an agent and select the repository

Run setup from any directory:

```sh
panopticon quickstart
```

Choose Claude or Codex and follow the foreground prompts. Claude accepts a token or API key with
hidden input; pressing Enter runs `claude setup-token`, followed by a hidden paste of the displayed
token. Codex accepts an API key or runs browser login in a new private credential directory.
No authentication task or attach/detach navigation is needed.

Enter the disposable repository's URL or local checkout path when asked for its source. Confirm
the displayed name and exact source. For this GitHub walkthrough, select a GitHub source whose
repository does not already contain `hello-panopticon.txt`. Quickstart also accepts local Git
checkouts without an origin and Git bundles, labeled fixed snapshots; those use a forge-free
workflow rather than this walkthrough's GitHub lifecycle.

After prerequisite and runtime checks, enter a GitHub token scoped to the disposable repository,
able to read it, push a branch, and open and merge a pull request. That token is stored only for
this repository. The reusable agent connection contains no GitHub token.

Success check: setup reports credentials configured on this host and opens the dashboard.
Configured credentials do not prove provider access; the real task below checks that. If setup
is cancelled or a prerequisite fails, completed credential steps remain saved. Rerun quickstart
to resume. For an existing repository, use the repository screen's `s` action or
`panopticon setup --repo <repo-id>`. Repair holds pending tasks until individually retried with
`R`; it does not stop running tasks or start the backlog.

## 3. Prove client access from a fresh shell

Open a new shell with no Panopticon authentication variables and run:

```sh
unset PANOPTICON_SERVICE_AUTH_FILE PANOPTICON_SERVICE_AUTH_MODE PANOPTICON_SERVICE_AUTH_TOKEN
panopticon tasks
```

The command automatically reuses the private default credential created on first startup.

Success check: the client works from the fresh shell: `panopticon tasks` exits successfully without prompting for a credential or returning
`401 Unauthorized`. An empty list before the first coding task is expected.

## 4. Create and supervise the first task

From the dashboard:

1. Press `n`. Select the registered repository with the arrow keys and Enter.
2. Select `github-self-reviewed` and press Enter. This is the short walkthrough: the initiating
   operator approves the work. `github-peer-reviewed` is also available, but requires another
   person to approve the pull request.
3. Enter `Add a hello-panopticon.txt file containing hello from Panopticon and do not change any
   other files.` and press Enter to submit it.

   Success check: a new task row appears under the selected repository. Its container status starts
   at `queued`, although a fast runner may move past `queued` before you can read it.
4. Watch the task's container status move from `queued` to `live`. If it becomes `down`, press `d`
   for details, fix the reported Docker, harness, or credential problem, and press `R` to respawn.

   Success check: the selected task's `container` column reaches `live`.
5. Highlight the task and press `t` to attach to its agent session. Do not enter a prompt yet;
   detach with `Ctrl-b d`.

   Success check: attach shows the selected task's agent session; `Ctrl-b d` returns you to the
   same dashboard with that task highlighted.
6. When `plan.md` appears, highlight the task, press `a`, select `plan.md`, and press Enter.

   Success check: `plan.md` is open from the selected task's artifact list and describes the
   requested one-file change.
7. Press `t` to attach, give any correction, then invoke the `advance` operation using the syntax
   for the selected harness:

   | Harness | Enter in the attached agent session |
   | --- | --- |
   | Claude | `/advance` |
   | Codex | `$advance` |
   Detach with `Ctrl-b d`.

   Success check: after plan approval, the dashboard reports `ITERATING` while the agent
   implements, tests, pushes its branch, and opens a draft pull request.
8. When the task has a URL, highlight it and press `p` to open the pull request.

   Success check: your browser opens the pull request for the disposable repository, and its diff
   adds only `hello-panopticon.txt` with one line: `hello from Panopticon`.
9. If the change needs correction, press `t`, explain the correction, and detach again. When the
   change is ready, attach and invoke `advance` with the same harness-specific syntax to approve
   the self-reviewed gate. Detach to watch the resulting state changes. GitHub branch protection
   remains the hard merge control.

   Success check: the task passes through `MERGING` (it may be too brief to read in the dashboard);
   after the agent merges the pull request, GitHub reports it merged and the dashboard reports
   `COMPLETE` with no container for that task.

On the headed machine used for the release rehearsal, also confirm that step 6 shows `plan.md` in
the configured document application and step 8 shows the exact task pull request in the browser.

## 5. Stop the evaluated instance

Stop the evaluated instance and verify that its task containers and dedicated tmux server are gone:

```sh
panopticon stop
test -z "$(docker ps --all --quiet --filter label=panopticon.task)"
! tmux -L panopticon has-session 2>/dev/null
panopticon stop
```

The second stop is intentionally safe. Configuration, repository settings, credentials, task
records, and retained artifacts remain on disk; `stop` removes neither the installation nor stored
state.

Success check: both Docker and tmux verification commands exit successfully and print no
Panopticon container or session IDs; the second `panopticon stop` also succeeds. The runtime is
gone, while your configuration, credentials, task records, and retained artifacts remain on disk.

## Maintainer: run the clean-host acceptance gate

The repository includes an opt-in release gate for the journey above. Run it only on a disposable
host with a dedicated Docker daemon and tmux namespace: it installs the selected artifact, invokes
an agent model, pushes a branch, opens a pull request, and merges that pull request into the supplied
repository. The host must expose exactly one registered Panopticon harness, that harness must be
Claude or Codex, and the host must begin with no Docker containers. The GitHub repository name
must start with `panopticon-acceptance-` and it must carry
the `panopticon-acceptance-disposable` topic; the base SHA makes an unreviewed repository change
fail closed.

The gate first proves the documented attach and detach inputs without any precursor. It then
repeats that round with an inert NUL challenge before each documented key and requires the client
to remain in its original session after the challenge. The challenge is a maintainer-only
causality check; evaluators do not enter it during the walkthrough.

Use new, purpose-limited tokens supplied explicitly for this run. The gate never imports a native
harness login or reads a personal credential file. The install spec must identify the supplied
local evaluator wheel with an absolute `file:///` URL and its SHA-256 fragment. The gate rejects a
missing, changed, or symlinked artifact, then runs the exact relative `pipx install` command shown
above from the wheel's directory.

```sh
export PANOPTICON_NEW_USER_ACCEPTANCE=I_AM_RUNNING_ON_A_DISPOSABLE_HOST
export PANOPTICON_ACCEPTANCE_INSTALL_SPEC="panopticon-next @ file:///absolute/path/to/panopticon_next-${PANOPTICON_RELEASE_VERSION}-py3-none-any.whl#sha256=<reviewed-wheel-sha256>"
export PANOPTICON_ACCEPTANCE_GITHUB_REPO='https://github.com/example/panopticon-acceptance-demo.git'
export PANOPTICON_ACCEPTANCE_BASE_SHA='<reviewed 40-character default-branch SHA>'
export PANOPTICON_ACCEPTANCE_HARNESS='codex'
export PANOPTICON_ACCEPTANCE_HARNESS_AUTH_ENV='OPENAI_API_KEY'

read -r -s -p 'Disposable GitHub token: ' PANOPTICON_ACCEPTANCE_GH_TOKEN
export PANOPTICON_ACCEPTANCE_GH_TOKEN
printf '\n'
read -r -s -p 'Disposable harness token: ' PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN
export PANOPTICON_ACCEPTANCE_HARNESS_AUTH_TOKEN
printf '\n'

uv run pytest -q tests/acceptance/test_new_user_onboarding.py \
  -k new_user_completes_one_real_github_self_reviewed_task
```

For Claude, use `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` as the harness auth environment
name. For Codex, use `OPENAI_API_KEY`, `CODEX_API_KEY`, or `CODEX_ACCESS_TOKEN`. If the opt-in
phrase or any input is absent or ambiguous, the live test skips before package-index, GitHub,
Docker, tmux, or model work begins.
