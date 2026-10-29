# The lifecycle of a task container

Every container-backed task in panopticon runs in its own **container** — a throwaway Docker
container running its selected agent harness (Claude Code, Codex, Pi, or Outfitter), working
on a private clone of the repo. Shell workflows such as `setup-repo` run on the host instead.
This doc explains the container's life from the operator's chair: how one comes up, the statuses you watch on the
dashboard, what each means, and how a container recovers or is torn down. It's about *observable
behaviour* from the operator's side.

For the token a container authenticates with, see [auth](auth.md); for the image it's built from,
[layers](layers.md); for a task's branch/clone and provisioning, [tasks](tasks.md).

## One task, one container, one session

A container-backed task's agent work happens in its container. Nothing else in panopticon runs an
LLM — the task service (control plane) and the session service (runner) are deterministic host
processes that never call an LLM. That's the **determinism invariant**: the container is the
*only* LLM-bearing component, so everything the agent does is scoped to it.

Concretely, each running task has:

- a **container** named `panopticon-<task-id>`, spawned by the session service on the host's
  Docker daemon;
- a **tmux session** (`panopticon-<task-id>`) whose pane runs the agent — this is what you attach
  to with `t` from the dashboard;
- a **per-task clone** of the repo, mounted read-write at `/workspace`, on the task's own branch.

The **session service** (the per-host runner) owns the container's whole lifecycle — it claims
the task, builds the image, starts the container, and later heals or cleans it up. The **task
service** only ever *records* and *displays* what the runner reports; it spawns nothing itself.

## The status you see on the dashboard

The dashboard shows one **container status** per task. The task service combines the task's
terminal state, launch pause, dependencies, and runner claim with the reported lifecycle
*phase*, the container's open **registration** (its live connection), and runner liveness.
The table explains the statuses; the selection rules below determine which one appears.

| Status | What it means | What you do |
|---|---|---|
| `–` | Terminal task (COMPLETE / DROPPED) — no container concept. | Nothing. |
| `paused` | Launch is paused and no container registration is open; no failed phase takes precedence. | Resume launch when ready. |
| `gated` | The task is unclaimed and dependencies block launch. | Wait for the dependencies to clear, or inspect the blocking tasks. |
| `queued` | The task is unclaimed and eligible for a runner to pick up. | Wait; if it sticks, check a runner is actually running (`make start`). |
| `live` | The claimed task has an open container registration and no reported failed phase. The registration alone does not prove the agent is healthy. | Attach with `t` to watch or steer. |
| `disconnected` | The task is claimed, has no open registration, and its runner is no longer connected. | Bring that runner's host back, or the task stays stranded until its claim is released. |
| `claiming` | A runner just claimed it; the spawn is about to start. | Nothing — transient. |
| `preparing` | Readying the per-task clone / workspace. | Nothing — transient. |
| `building` | Composing and building the image. Uncached steps can take time. | Wait; a first image build can take minutes. |
| `starting` | `docker run` and the tmux session are coming up. | Nothing — transient. |
| `awaiting` | Container and tmux are up; waiting for the container's `/live` registration. | Nothing — transient; if it lingers, see *When it goes wrong*. |
| `healing` | The runner is recovering the task's missing session. | Wait for recovery; inspect repeated failures. |
| `failed` | A launcher or runner failure has been reported. It can remain visible even while the container's liveness process is registered. | Read the detail; fix the cause (bad image layer, missing secret) and respawn. |
| `down` | The task is claimed, its runner is live, and neither a registration nor a lifecycle phase remains. | Respawn from the dashboard with `R` (the runner also self-heals — see below). |

The selection rules in `compose_container_status` (`core/models.py`) run in this order:

1. A terminal task shows `–`.
2. A launch-paused task without a registration shows `paused`, or `failed` if that phase
   was reported.
3. An unclaimed task shows `gated` when dependencies block it, otherwise `queued`.
4. A claimed task with an open registration shows `live`, except that a reported `failed`
   phase wins. Other stale phases and runner disconnection do not override that registration.
5. Without a registration, a claimed task whose runner is disconnected shows `disconnected`,
   even if a lifecycle phase remains.
6. Otherwise the reported phase shows through; with no phase, the task shows `down`.

The dashboard only displays this result. The runner reports `claiming`, `preparing`,
`building`, `starting`, and `awaiting` during spawn, plus `healing` during recovery and
`failed` on failure. Those phases are inputs to the rules, not a separate priority order.

## Coming up: the spawn sequence

When a new task appears, the per-host session service brings its container up in five steps,
reporting each as a status above.

1. **Claim** (`claiming`). A runner **claims** the unclaimed task first — a compare-and-set that
   409s if another host got there first. This is the spawn gate: exactly one host ever runs a
   given task, even with several runners watching the same task service.

2. **Prepare** (`preparing`). The runner makes the task's **workspace**: a
   local `git clone` of a per-repo cache into a task-private directory, mounted read-write at
   `/workspace`. It hardlinks objects when possible and copies them across filesystems,
   and its `origin` is pointed at the real forge rather than the local cache.

3. **Build** (`building`). The runner composes and `docker build`s the task's image. It's tagged
   per `(harness, workflow, repo)`, with an optional runtime suffix. Docker reuses unchanged
   layers, which is why the first build can be slow and later builds are usually faster.
   See [layers](layers.md) for how the image is composed.

4. **Start** (`starting`). The runner does `docker run --detach` (injecting the repo's secrets
   via `--env-file`, mounting `/workspace`, and a small config volume that persists the agent's
   history across respawns), then creates the tmux session whose pane execs the agent inside the
   container.

5. **Await** (`awaiting`). The container's entrypoint starts up. It first **remaps** its baked
   `panopticon` user to the invoking host's uid/gid (`PANOPTICON_PUID`/`PGID`) and drops
   privileges via `gosu` — so files the agent writes under `/workspace` are owned by *you* on the
   host, not root. Then it **connects to the task service and holds the connection open**,
   registering that this container is working on this task.

Once that registration is open, the status flips to **`live`** and the agent is off.

## Provisioning: naming the task gives it a branch

A fresh task starts on whatever its clone checked out; once the agent sets a **slug**, the branch
appears. The mechanics happen on the runner, where the container actually is (so they stay correct
even when the runner is remote): the session service sees the slug land and runs
`git checkout -b panopticon/<slug>` on the per-task clone, with `origin` already pointed at the
forge. The task service only *records* the result — it touches no filesystem.

For what a slug, branch, clone, and `provisioned` mean as task concepts, see [tasks](tasks.md).

## While it runs

- **Liveness is connection-based, not a heartbeat.** The container holds one long-lived `/live`
  connection to the task service. While it's open the task reads `live`; if it drops (crash,
  kill, network blip) the registration clears and the task stops being `live`. The container
  reconnects with backoff across transient blips.
- **The agent.** In the tmux pane, the launcher bootstraps and starts the task's selected
  harness. It renders workflow **skills** and **operations** in that harness's format and
  supplies the workflow's state-machine overview. Claude Code and Codex connect their **MCP**
  clients to the task service; Pi and Outfitter use rendered **REST** instructions for task
  operations and artifacts because they have no MCP client.
- **The task memo is pre-filled.** On a task's first spawn the runner pastes the task's
  description into the agent's input box (unsent), so you see the ask waiting when you attach.
- **Turn and blocked.** As the agent and user hand the work back and forth, the task's **turn**
  flips (`agent` ↔ `user`) via in-container hooks; a separate **blocked** marker is a deliberate
  "waiting on something" flag the agent sets. Both are shown on the dashboard.
- **Auth.** Credentials depend on the selected harness: Claude Code uses an OAuth token or
  API key from the repo's env-file; Codex uses its API credentials or a mounted `auth.json`;
  Pi and Outfitter use Pi-compatible provider credentials or a mounted credential directory.
  These model-provider credentials are separate from the task-service capability token used
  by MCP and REST calls. See [auth](auth.md) for setup and credential layouts.

## When it goes wrong

A container can disappear out from under a live task — an OOM kill, a host reboot, a `docker rm`.
The system distinguishes a few cases:

- **Lingering `awaiting`** — the container may be unable to reach the task service even when
  the host dashboard works. Inspect Docker logs for the current or surviving container and
  the task pane if it is still available. A failed launcher can close its pane; recovery may
  restart the container and replace its logs. Check the container's `PANOPTICON_SERVICE_URL`,
  the service listener, the Docker bridge route, and host firewall rules. On Linux, allow only
  the required container network
  to reach the task-service port; do not disable the firewall or expose the service broadly.
  An unauthenticated request from inside the container that receives HTTP **401** proves
  network reachability, **not successful authentication**. Connection timeouts or refusals
  need network/listener repair; 401/403 responses need credential repair instead. Liveness
  retries automatically and logs recovery on the next keepalive. After fixing connectivity,
  use **`R`** to retry a task whose agent launcher already failed.
- **`down`** — the container is gone but its runner is alive. The runner's **reconcile** pass
  notices the container has vanished and clears the stale spawn phase, so the task composes to
  `down` rather than lying at `awaiting`.
- **Self-heal.** The runner also **heals** orphans automatically: a task it still owns whose tmux
  session is gone gets respawned through the same idempotent spawn path (the agent resumes from
  its persisted history). A **crash-loop cap** (a handful of respawns within a short window)
  stops a hopelessly failing task from respawning forever — past the cap it's left `down` for you
  to look at.
- **Manual respawn.** You can always respawn a `down` task yourself from the dashboard with
  **`R`**. This is also how you pick up a changed secret or env-file — respawn to restart the
  container with the new values.
- **`disconnected`** — the *runner* is gone, not just the container. The task is stuck claimed by
  an absent host. Bring that host back (its runner reclaims and heals), or release the claim so
  another runner can take over.
- **`failed`** — a spawn step raised. The status carries a detail string (e.g. a broken image
  layer or a missing secret). Fix the underlying cause, then respawn.

Repository caches are keyed by repository ID and source. Editing a source creates a fresh cache;
existing caches and task checkouts are preserved. Upgrading creates one fresh cache per source,
so retained caches use additional disk space. Existing tasks refuse to resume against a different
source: restore the original source with `g` → `e`, or create a new task for the edited repository.
Equivalent GitHub SSH/HTTPS credential repairs remain supported.

## Teardown

When a task reaches a terminal state (COMPLETE or DROPPED), its container is no longer needed. The
runner's **cleanup** pass stops the container, releases the claim, and removes the per-task
`/workspace` clone. If a delete is blocked (e.g. by a file left root-owned by a nested build), it
escalates — an as-root sweep, then quarantining the directory aside — so a stuck workspace never
wedges the runner. After cleanup the task shows `–`: no container, nothing to attach to.

## See also

- [auth](auth.md) — setting up model-provider and task-service credentials.
- [layers](layers.md) — how a task's image is composed (base → harness → workflow → repo).
- [tasks](tasks.md) — slug, branch, clone, and provisioning as task concepts.
