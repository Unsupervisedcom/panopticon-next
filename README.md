# panopticon

**Agents write the code, you own what ships.**

Panopticon is a terminal-native control plane for running multiple coding agents. It drives Claude
Code, Codex, or pi for each task and gives you one place to supervise the fleet.

## Quick install

```sh
pipx install panopticon-next
panopticon quickstart
```

Run quickstart from any directory. Connect a coding agent, select a repository, and open the
dashboard. Missing prerequisites have a corrective action; your saved connection remains available
when you resume. See [Requirements](#requirements) and [Install](#install) for details.

- **A live dashboard** of all your tasks, showing which agents are working and which are blocked
  waiting on you, so you stop cycling through terminals to find the one that's stuck.
- **[Configurable workflows](docs/workflows/README.md)** that make approval points explicit and
  prevent Panopticon from advancing a task until the current stage is resolved.
- **Isolated by default:** each agent works in its own container on its own branch
  so concurrent tasks do not share workspaces.

Self-hosted: your infrastructure, your secrets, your repos. Workflows control Panopticon's state
transitions; they do not restrict the commands an agent can execute. Use least-privilege
credentials and forge branch protection as hard controls. Containers separate workspaces but are
not a security boundary against a malicious or compromised agent. Quickstart writes a separate environment file for each repository. Reusable agent connections
contain no GitHub token; repositories using the same Codex subscription share its rotating
authentication directory.

New here? Follow the [new-user walkthrough](docs/getting-started.md). For the mental model behind
the dashboard, read [`docs/overview.md`](docs/overview.md).

## The dashboard

The whole fleet in one terminal view — every task's `state`, whose `turn` it is (agent or you),
its `container` status, and its repo and slug:

```text
══════════════════════════════════════════════════════════════════════════
  panopticon                                                6 tasks
──────────────────────────────────────────────────────────────────────────
  state          turn       container   repo       slug[memo]
  ITERATING      agent      live        web-api    add-oauth[Add OAuth login]
  PLANNING       user       live        web-api    fix-upload[Flaky S3 upload]
  MERGING        agent      starting    dashboard  dark-mode[Dark-mode theme]
  ITERATING      user ⚠     down        web-api    migrate-db[Move to Postgres]
  ORCHESTRATING  agent      live        infra      q3-cleanup[Q3 tech-debt]
  PLANNING       agent      live        infra      └─ drop-py38[Drop Python 3.8]
  COMPLETE       agent      –           web-api    ship-readme[README refresh]
──────────────────────────────────────────────────────────────────────────
  t attach   n new task   x drop   / search   d detail   ? help   q quit
══════════════════════════════════════════════════════════════════════════
```

The `turn` column is color-coded live — green when the agent is working, yellow when it's your
move, red (`⚠`) when a task is blocked waiting on you — so you can tell at a glance which agents
need you. The `container` column tracks each agent's sandbox as it spawns (`queued → … → live`,
or `down` when one needs a respawn), and governed sub-tasks nest under their governor (`└─`).
Press `t` to drop into any task's session, `?` for the full key list.

## Requirements

Panopticon runs the control plane on your host and each agent in its own container, so it
shells out to a few host tools. You need:

- **Python 3.11+**
- **Docker**, with the daemon running
- **tmux:** the dashboard, console supervisor, and task sessions run on a dedicated
  `tmux -L panopticon` server
- **git:** the session service clones a per-task workspace for each agent

Foreground setup offers Claude, Codex, or Pi. Pasting a token or API key requires no agent CLI
on the host; install Claude Code or Codex locally only if you choose its native browser login.
The task container supplies the agent CLI. Use Claude or Codex for the first GitHub walkthrough.

`panopticon quickstart` checks these first; run `panopticon doctor` to re-check any time.

## Install

Panopticon is a command-line app, so [pipx](https://pipx.pypa.io) is the recommended way to install
it: it puts the `panopticon` command on your `PATH` in its own isolated environment. If
`command -v pipx` prints nothing, install pipx first:

```sh
# macOS
brew install pipx

# Ubuntu or Debian
sudo apt-get update
sudo apt-get install --yes pipx
```

Make commands installed by pipx available in future shells, then **close this terminal and open a
new one** so the PATH change takes effect:

```sh
pipx ensurepath
```

In that new terminal, install Panopticon, then start setup:

```sh
pipx install panopticon-next
panopticon quickstart
```

[//]: # (x-release-please-start-version)
You can verify the installed release with `panopticon --version`; it should print `panopticon 0.2.11`
without requiring a checkout or `uv`.
[//]: # (x-release-please-end)

The distribution is **`panopticon-next`**, but the command you run and the package you import are
both **`panopticon`**. To work from a checkout, run `uv sync` and then `uv run panopticon doctor`.

## Quickstart

Run `panopticon quickstart` from any directory:

```sh
panopticon quickstart
```

Connect Claude, Codex, or Pi, then confirm a repository URL or local path. The current checkout is
suggested when available; a checkout without an origin works locally, and a Git bundle is labeled
a fixed snapshot. GitHub sources enable GitHub workflows; local and other sources use the local
Git workflow.

Setup runs in the foreground. Claude can open its browser login and accept a hidden token paste;
Codex browser login writes directly to a new private directory. GitHub workflow credentials are
entered separately for the selected repository. A saved connection means credentials are
configured; the first real task verifies provider access.

Use `panopticon setup` to connect an agent even before Docker or tmux is installed. To repair a
registered repository, open the repository screen and press `s`, or run
`panopticon setup --repo <repo-id>`. Repository repair requires the local task service. Pending
tasks remain paused after repair: select the one you want to start and press `R`. Running tasks
remain available. Existing explicit credentials are preserved unless you select replacement.

A second repository can use the saved agent connection. Credentials are configured on the current
host; a remote runner needs its own setup. Bare `panopticon` opens first-use setup when no stack
configuration exists and otherwise opens your fleet.

Quickstart enables task-service authentication automatically. On Linux, the task service binds to
`0.0.0.0` so bridge containers can reach it; restrict reachable interfaces with a firewall or
encrypted, access-controlled transport before running on an untrusted network. On macOS it binds
to loopback. See [`docs/auth.md`](docs/auth.md).

## Your first task

This GitHub walkthrough currently requires a Claude or Codex task. On the dashboard:

1. **Create it.** Press `n`, pick the repo, and choose `github-self-reviewed` for the shortest
   walkthrough: you perform its approval. `github-peer-reviewed` instead requires another person
   to approve the PR. For a predictable test, enter `Add a hello-panopticon.txt file containing
   hello from Panopticon and do not change any other files.` A new task row appears under the
   selected repo.
2. **Watch it start.** The task's `container` column moves `queued → … → live` as the runner
   spawns its container; once it's `live` the agent starts on its own branch and begins planning
   automatically.
3. **Respond when it needs you.** The `turn` column shows whether the agent is working or waiting
   on you. Press `t` to attach to its session, then detach with `Ctrl-b d` (or your own `tmux`
   prefix + `d`) to return to the same dashboard and highlighted task.
4. **Open and approve the plan.** When `plan.md` is ready, press `a`, select `plan.md`, and press
   Enter. Press `t` to attach again, give any correction, and invoke the **`advance`** operation:
   `/advance` in Claude or `$advance` in Codex. The dashboard advances to `ITERATING` while the
   agent implements and tests the change. The [workflow guide](docs/workflows/README.md) documents
   command syntax for the other harnesses and workflows they support.
5. **Review what ships.** The agent opens a PR; press `p` on the dashboard to open it in your
   browser. For the example request, the PR should add only `hello-panopticon.txt`, containing one
   line: `hello from Panopticon`. Attach and invoke `advance` with the same harness-specific syntax
   to approve the self-reviewed gate. The task moves through `MERGING`; success is a merged PR and
   a `COMPLETE` dashboard task with no remaining task container. Configure GitHub branch protection
   as the hard merge control.

When you finish evaluating Panopticon, run `panopticon stop`. It stops Panopticon task containers
and its dedicated tmux server; stored configuration, credentials, task records, and artifacts
remain on disk. Verify teardown with:

```sh
docker ps --filter label=panopticon.task
tmux -L panopticon has-session 2>/dev/null; test $? -ne 0
```

Both checks exit successfully when teardown is complete: Docker lists no Panopticon task
containers and the dedicated tmux server is gone. Running `panopticon stop` again also succeeds.

## Configuration

Panopticon stores its data under standard XDG locations, each overridable by an environment
variable (resolution is `$PANOPTICON_*` → `$XDG_*_HOME/panopticon` → the default below):

| What | Default location | Override |
|---|---|---|
| Database | `~/.local/share/panopticon/panopticon.db` | `PANOPTICON_DB` (or `PANOPTICON_DATA`) |
| Artifacts + per-task clones | `~/.local/share/panopticon/` | `PANOPTICON_DATA` |
| Layers, secrets, workflows | `~/.config/panopticon/` | `PANOPTICON_CONFIG` (workflows also via the `--workflows-path` flag) |
| Per-repo clone cache | `~/.cache/panopticon/repos/` | `PANOPTICON_CACHE` |
