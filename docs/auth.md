# Authentication

## Task-service authentication

The task service accepts bearer tokens from one host-local JSON file. Store it under
`~/.config/panopticon/secrets/` (or `$PANOPTICON_CONFIG/secrets`) and refer to it by filename; do
not put its contents in a repo env-file, task, database field, or artifact. Use distinct tokens for
an off-host read-only client and for clients that mutate control-plane state. The shipped terminal
dashboard has mutating actions and therefore uses a write token; a phoneopticon board uses its read
token only for ordinary GET requests:

```json
{
  "read": ["generate-a-long-random-dashboard-token"],
  "write": ["generate-a-different-long-random-fleet-token"]
}
```

The `write` array is required and nonempty. The `read` array is optional (and may be empty) when no
read-only client is deployed. Arrays may contain multiple tokens for rotation, but may not contain
duplicates or overlap. Tokens use the transport-safe ASCII bearer grammar: letters, digits,
`-._~+/`, followed by optional `=` padding, with a minimum length of twelve characters. Generate
long random values using that alphabet; short values, spaces, control characters, non-ASCII text,
quotes, and backslashes are rejected at startup. The file must be owned by the Panopticon process
user with no group or other permissions (normally mode `0600`); insecure files are rejected.
Configure every task-service and runner host with the same filename reference.
Integrated startup (`panopticon start`, `panopticon host`, and `panopticon quickstart`) creates a
private `task-service-auth.json` credential on first use and selects enforced mode automatically.
It reuses that credential on later starts. To select a different credential explicitly:

The required steady-state configuration is enforced mode:

```sh
export PANOPTICON_SERVICE_AUTH_FILE=task-service-auth.json
export PANOPTICON_SERVICE_AUTH_MODE=enforced
```

The standalone task-service launcher defaults to `127.0.0.1`. With enforced authentication, the
integrated `panopticon start` and `panopticon host` commands default to `127.0.0.1` on Darwin and
`0.0.0.0` on Linux and Windows so native containers can reach the service. Disabled integrated
startup defaults to loopback on every platform. On native Linux the authenticated compatibility
default intentionally listens on every host interface because bridge containers cannot reach host
loopback; safe operation therefore depends on enforced task-service authentication plus
independently encrypted and access-controlled transport. `PANOPTICON_HOST` overrides these launch
defaults when the operator selects another container-reachable intended interface. Bearer tokens
travel over HTTP, so a broad bind is appropriate only where every reachable interface has those
protections.

On macOS, both OrbStack and Docker Desktop provide the `host.docker.internal` route that lets task
containers reach the loopback-bound service. Panopticon does not probe which runtime is active;
the conservative Darwin default is the same for both.

Authentication mode is reported at startup; disabled mode produces a warning. Enforced mode is
the steady state. Disabled mode is the operator's explicit break-glass recovery path: clear an
invalid `PANOPTICON_SERVICE_AUTH_FILE` reference and restart the service with
`PANOPTICON_SERVICE_AUTH_MODE=disabled`. Integrated startup binds loopback in this mode unless you
also set `PANOPTICON_HOST`; restore or replace the host-local credential file, then restart the
fleet directly in enforced mode.

Integrated startup creates missing tmux sessions with the invoking process's current authentication
environment, but deliberately leaves existing service, runner, dashboard, and task sessions alive.
It does not restart them to converge changed credentials: doing so would interrupt the live fleet.
During migration or rotation, explicitly restart each component at the corresponding rollout step
below; do not treat a second `panopticon start` invocation as proof that existing sessions changed.

Host clients (runner, dashboard, and CLI) resolve the file against their own secrets directory.
For each new task, the runner validates it and creates a private regular-file snapshot: Docker tasks
receive that snapshot as a read-only mount—even when the repo has no `env_file`—and shell tasks use
the snapshot for their session lifetime. This prevents a rotation-time file replacement from
changing the object being launched. Tokens are sent in the `Authorization` header, never in URLs or
command arguments. `GET /healthz` stays open; every other route is protected.
Read tokens may call ordinary GET endpoints. Write tokens may call every endpoint, including the
task and runner liveness streams and MCP.

Docker task containers do not receive either fleet token. The runner derives a deterministic,
opaque capability for the task from the active write token and snapshots only that capability.
It permits the container to read and mutate its own task, publish its own artifacts, hold its own
registration and liveness stream, and perform its own workflow operations. An orchestrator task
may additionally list, create, and pre-plan only its transitively governed descendants. It cannot
mutate or drop a sibling or unrelated task. Shell workflows run directly on the trusted host and
retain the fleet write-token snapshot needed for their host-side operation.

Existing Docker containers retain their derived capability until respawn. Removing its source
write-token generation from the service invalidates that generation's derived capabilities; keep
the old generation active until trusted callers and task containers have converged during a normal
rotation. To lock out a suspected container, remove that generation after trusted callers have
respawned onto the next one.

## Browser read-only transport

Configure an exact-origin allowlist for the phoneopticon board as a comma-separated environment
variable on the task service:

```sh
export PANOPTICON_BROWSER_ORIGINS=https://phone.example,https://phone-alt.example:8443
```

With no allowlist, cross-origin task-service access is disabled. Entries must be complete
`http://` or `https://` scheme-host-port origins, without wildcards, paths, queries, fragments, or
embedded credentials. The browser sends its fleet read token only as
`Authorization: Bearer <token>` and uses `GET /tasks`; cookies, URL credentials, alternate auth
headers, and cross-origin mutations are rejected. The CORS response does not enable credentials.

Deploy authentication by creating the credential file, configuring enforced mode, and restarting
the full stack so every task container is respawned with its per-task capability. To rotate,
append the next read/write tokens after the old tokens in their arrays; the last token is the
active token selected by clients. Restart the service, then restart all hosts and respawn
containers so callers select the new last token while both generations work. Remove the old
tokens only after the fleet has converged, then restart the service again.

An enforced service refuses to start when the reference is absent or invalid. Authentication
failures always return `401`, `WWW-Authenticate: Bearer`, and
`{"detail":"authentication required"}` without revealing whether a resource exists. Loopback is
not exempt: once the process binds beyond localhost, a loopback bypass would also bypass local
proxies and port forwards.

## Container authentication — giving tasks their agent credentials

Each **harness** (the agent CLI a task runs) authenticates its own way. `panopticon setup` connects
Claude, Codex, or Pi in the foreground and saves a reusable, host-local connection containing only
that harness's credentials. It does not require Docker, tmux, or a running task service. A reusable
connection has no repository or forge token and is never an implicit fallback for an existing
repository.

`panopticon quickstart` connects or reuses an agent, selects a repository source, verifies the
local runtime, and registers or reuses the repository before opening the dashboard. A new
repository explicitly receives the selected connection; an existing explicit binding is preserved
unless you choose replacement. Repository-specific credentials such as `GH_TOKEN` are collected
separately. The repo's `env_file` carries environment credentials;
`credential_dir` carries shared rotating auth files. Saved credentials establish that the task can
receive them; the first real task verifies provider access.

The Claude manual setup is below; [Codex / OpenAI](#codex--openai-gpt-56) and
[Pi](#pi-earendil-workspi) follow. Claude authenticates from `CLAUDE_CODE_OAUTH_TOKEN` in the
repo's env-file (or `ANTHROPIC_API_KEY`). The OAuth token is long-lived and non-rotating, so it
survives concurrent tasks and respawns. There is no Claude `login` command; use `setup-token`.

## Claude one-time setup per account

1. **Mint a long-lived token** on a machine where you can complete the browser OAuth (it needs a
   Claude subscription or Console login):

   ```sh
   claude setup-token
   ```

   Complete the browser flow; the command prints a token (`sk-ant-oat01-…`). It's long-lived
   (~1 year), non-rotating, and inference-only — exactly what an unattended container needs. The
   same token works for every repo; minting another does not invalidate it, so you can roll out a
   renewal gradually.

2. **Add it to the repo's env-file.** Each repo has an `env_file` — a **name relative to the secrets
   dir** (`~/.config/panopticon/secrets/`, or `$PANOPTICON_CONFIG/secrets`) naming a file of
   `KEY=value` lines that the runner injects into the task container (`--env-file`). Add (or update)
   one line:

   ```sh
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…
   ```

   Keep the file `0600` and out of version control. If the repo has no `env_file` yet, create one
   under the secrets dir (e.g. `~/.config/panopticon/secrets/<repo>.env`) and set the repo's
   `env_file` to its **name** (`<repo>.env`) in the dashboard's repo form, which accepts an
   absolute or relative path and normalizes it to a name.

That's it — new task containers for that repo now authenticate from the token.

## Foreground repository setup and recovery

To repair a registered repository, press `g` in the dashboard, highlight the repository, and press
`s`, or run:

```sh
panopticon setup --repo <repo-id>
```

Registered-repository setup is local-only: it first proves that the selected task service and
runner are the intended local runtime. It then places the repository under an execution hold before
changing credentials. Running tasks continue undisturbed. Pending tasks remain held after repair;
retry only the task you want to start with `R` so a credential change cannot release the whole
backlog.

An existing repository's explicit harness, env-file, and credential-directory binding remains in
effect unless you choose replacement. Replacement copies retained repository values into a new
private env-file and changes the repository reference only after the private write succeeds. An
incomplete explicit binding is reported as incomplete; setup does not silently substitute a saved
connection. Credentials are runner-local, so repeat setup on each host that will launch tasks.

The legacy `setup-repo` workflow remains available for explicit API and integration compatibility,
and its existing task records remain visible. Quickstart and the repository screen no longer create
that task. Foreground setup refuses to race a running legacy authentication session for the same
repository; finish or exit that session, then run foreground setup again.

## Notes

- **The env-file lives on the host that spawns the container.** Because `env_file` is stored as a
  bare name resolved against each runner's own `~/.config/panopticon/secrets/`, the same repo record
  works across hosts: with a single host (M1) that's the machine you minted on; with remote runners
  (M5), place a same-named env-file under each runner host's secrets dir.
- **`ANTHROPIC_API_KEY` overrides `CLAUDE_CODE_OAUTH_TOKEN`.** If a repo needs to burst past the
  subscription rate limit, put an `ANTHROPIC_API_KEY` in the same env-file — but don't set both
  unintentionally, since the API key wins.
- **Already-running tasks** keep their credential snapshot. New and deliberately respawned tasks
  use the replacement; foreground setup does not interrupt live work.
- **Rotating/revoking.** To replace a Claude credential, run
  `panopticon setup --repo <repo-id>`, select replacement, decline reuse of the saved connection,
  and enter the new token. Foreground replacement creates a new private repository env-file and
  leaves the prior file unchanged. Retry affected pending tasks individually after setup.
  Per-token revocation isn't available upstream (account-level "revoke all" can take time to
  propagate), so treat a leak as "mint a replacement + monitor usage in the Console," and keep the
  env-file tightly held.
- **A malformed credential fails the spawn, not the container.** Before launching `claude`, the
  harness checks the *shape* of whichever var is set — the right prefix (`CLAUDE_CODE_OAUTH_TOKEN`
  must start `sk-ant-oat01-`, `ANTHROPIC_API_KEY` must start `sk-ant-`) plus a plausible minimum
  length — and, on a mismatch, fails the spawn with a lifecycle detail naming the bad variable and
  pointing at the env-file — the same UX as a missing credential. This is deliberately a cheap
  check, not full validation of Anthropic's token grammar and not a live API probe (either would
  add a network round trip, and its own flakiness, to every spawn); it catches a wrong prefix or an
  obviously truncated/placeholder value, and rules out **in-container `/login`** as a recovery path
  (no browser in the container, the pasted URL gets tmux linebreaks, and a per-task config volume
  means a login there fixes exactly one session) — repair the repository in foreground setup and
  retry the affected task instead.

## Codex / OpenAI (GPT-5.6)

A task created with `harness: "codex"` (or in a repo whose `default_harness` is codex) runs
OpenAI's Codex CLI in its container. Three credential tiers, in order of setup effort:

1. **API key** (pay-per-token): add one line to the repo's env-file —

   ```sh
   CODEX_API_KEY=sk-...
   ```

   The harness renders it into codex's `auth.json` at container start (the same shape
   `codex login --with-api-key` writes). `OPENAI_API_KEY` works too.

2. **ChatGPT Business/Enterprise access token** (non-rotating — the exact analog of
   `claude setup-token`): mint at `chatgpt.com/admin/access-tokens`, then

   ```sh
   CODEX_ACCESS_TOKEN=...
   ```

   in the env-file. Codex reads it straight from the environment.

3. **ChatGPT Plus/Pro subscription** (rotating tokens — needs the shared credential dir):

   Run `panopticon setup`, choose Codex, and leave the API-key prompt empty to complete Codex
   browser login in a new private directory. Quickstart explicitly binds that saved connection to
   its selected repository. For an existing repository, run `panopticon setup --repo <repo-id>` and
   choose replacement. A later Codex login creates another directory; repositories already bound
   to the former directory keep using it until explicitly replaced.

   For an advanced manually managed repository binding, create an isolated directory and make
   Codex write file-based credentials directly into it:

   ```sh
   mkdir -p ~/.config/panopticon/secrets/codex-manual.d
   chmod 0700 ~/.config/panopticon/secrets/codex-manual.d
   CODEX_HOME=~/.config/panopticon/secrets/codex-manual.d \
     codex -c 'cli_auth_credentials_store="file"' login
   chmod 0600 ~/.config/panopticon/secrets/codex-manual.d/auth.json
   # then set credential_dir to codex-manual.d in the dashboard's repo form
   ```

   The runner mounts the dir **read-write and shared** into that repo's task containers; the
   harness symlinks `auth.json` into each task's `CODEX_HOME`. Sharing is deliberate: ChatGPT
   refresh tokens **rotate with reuse detection**, so every session must converge on one copy —
   codex reloads the file from disk before refreshing (and on 401) and writes refreshed tokens
   back through the symlink, so concurrent sessions on one host stay consistent. Do **not**
   copy the same auth.json to a second host; log in per host, or use an access token. If the chain
   is invalidated, run foreground setup on that host and explicitly replace each affected
   repository binding.

Pick the model per task via `starting_model` (e.g. `gpt-5.6-sol`, `gpt-5.6-terra`,
`gpt-5.6-luna`), with an optional reasoning-effort suffix (`gpt-5.6-sol:high`); unset, codex
picks its own default. Note the fleet-level constraint: plan
rate limits (not auth) cap concurrent Codex throughput on Plus/Pro.

## Pi (earendil-works/pi)

A task created with `harness: "pi"` (or in a repo whose `default_harness` is pi) runs the `pi`
coding-agent CLI (https://github.com/earendil-works/pi) in its container. Its native configuration
directory is `~/.pi/agent`; Panopticon keeps the persistent volume rooted at `~/.pi` and sets
`PI_CODING_AGENT_DIR` to that native `agent` subdirectory. Pi resolves provider environment
variables directly, while OAuth and stored API-key credentials live in `auth.json`.

1. **API key** (any of pi's many providers): add one line to the repo's env-file —

   ```sh
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   `OPENAI_API_KEY`, `GEMINI_API_KEY`, and Pi's other documented provider variables work too.
   Pi reads the variable directly at launch; the harness writes no file for this path.

2. **Subscription or OAuth-backed provider** (ChatGPT Plus/Pro, GitHub Copilot, xAI, OpenRouter,
   or Radius — rotating tokens or an OAuth-minted key, needs the shared credential dir):

   ```sh
   # on the host, once per account:
   pi
   /login   # then select a provider
   # /login writes ~/.pi/agent/auth.json — share it with task containers:
   mkdir -p ~/.config/panopticon/secrets/pi.d/pi/agent
   cp ~/.pi/agent/auth.json ~/.config/panopticon/secrets/pi.d/pi/agent/
   chmod 0600 ~/.config/panopticon/secrets/pi.d/pi/agent/auth.json
   # then set credential_dir to pi.d in the dashboard's repo form
   ```

   The runner mounts the directory **read-write and shared** into that repo's task containers; the
   harness imports `pi/agent/auth.json` into each task's native directory. Pi's provider-generic
   OAuth and stored API-key entries are supported, including the `openai-codex` entry produced by
   ChatGPT login. A codex CLI `auth.json` with top-level `OPENAI_API_KEY`, `tokens`, and
   `last_refresh` fields is a different format and is rejected with an actionable lifecycle
   failure.

3. **Personal pi config** (custom providers, local models, and other host-managed config): put
   the pi files in a `pi/agent/` subdirectory of the repo's existing credential directory. For example,
   if the repo uses `credential_dir: "openai.d"`:

   ```sh
   mkdir -p ~/.config/panopticon/secrets/openai.d/pi/agent
   cp ~/.pi/agent/models.json ~/.config/panopticon/secrets/openai.d/pi/agent/
   ```

   The harness imports each entry under `openai.d/pi/agent/` without changing the mounted source.
   For `models.json`, only HTTP(S) provider hosts exactly equal to `localhost`, `127.0.0.1`, or
   `::1` are adapted to `host.docker.internal`, so a model server on the runner host is reachable
   from the container. LAN and public URLs remain unchanged. Existing files in the persistent Pi
   volume are never overwritten.

Anthropic API keys are supported and first-class. Anthropic OAuth in pi is not recommended or supported by Panopticon and may risk your Anthropic account. Panopticon does not suggest or
generate that credential path, but it does not block an operator who explicitly supplies
`ANTHROPIC_OAUTH_TOKEN` after making an informed choice.

Pick the model per task via `starting_model` (pi's own `--model` syntax, e.g. `sonnet`,
`sonnet:high`, `openai/gpt-4o`); unset, pi picks its own default.

**Known gap:** pi has no MCP client, so workflow skills that name an MCP tool directly (outside
the two operations this harness itself renders) won't work unmodified under pi. That includes the
built-in planned GitHub workflows; use Claude or Codex for the documented first-task walkthrough.
See the `panopticon.harnesses.pi` module docstring for exactly which skills are affected.
