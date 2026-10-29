# Container image layers — how they work and how to add your own

A task container's image is **composed from four tiers** stacked in order: **base → harness → workflow → repo**. The base
provides shared tools; the harness adds its agent CLI; a workflow adds what its skills need;
a repo adds its own toolchain. This doc explains the model and gives copy-pasteable recipes
for adding a workflow layer and a repo layer.

## The four tiers

The base has a Dockerfile; the other tiers contribute optional Dockerfile fragments that the
composer appends in order after `FROM panopticon-base`:

- **Base** — minimal and general: `python`, `git`, `gh`, `bash`, `curl`, `vim`; the `claude` CLI
  available to the launcher; the `panopticon` package; and the entrypoint
  (uid/gid remap → privilege drop).
  Built from `src/panopticon/docker/Dockerfile` and tagged `panopticon-base` (`make build`). It is
  deliberately **not** opinionated about any single workflow or repo; `gh` is shared because
  credentials and GitHub-bound work are workflow-independent.
- **Harness** — the selected harness's local `image_layer()` installs its agent CLI and runtime
  dependencies. Claude's fragment is empty because the base already installs Claude Code;
  Codex, Pi, and Outfitter contribute their own installation steps. `tmux` runs on the host,
  not in the base task image.
- **Workflow** — a fragment a `Workflow` subclass contributes via its `image_layer()` method, adding
  workflow-specific additions its skills need. It is empty when the base already supplies every
  required tool, as for `github-peer-reviewed` and `github-self-reviewed`. The 2119 workflows
  add Codex for review even when the interactive harness is different.
- **Repo** — a fragment referenced by a repo's `image_layer_file`, adding repo-specific setup:
  build toolchain, dependencies, pre-launch configuration (e.g. `uv`, `make sync`).

The repo layer sits on top because it is the most specific and most frequently changing.

## How composition works

When the session service (the runner) spawns a task, it composes the image before `docker run`
(`sessionservice/spawner.py` `_compose_image`, `sessionservice/images.py`):

1. It gets the harness fragment from the runner's local harness adapter, then fetches the
   workflow and repo fragments over REST from the task service —
   `GET /workflows/{name}/image-layer` and `GET /repos/{id}/image-layer`. It keeps that order
   and drops empty fragments.
2. If none remain, the task runs on **`panopticon-base`** directly (no composed-image build;
   the base freshness check below still applies).
3. Otherwise it writes a Dockerfile that starts `FROM panopticon-base` and appends the fragments,
   then `docker build`s it, tagged **`panopticon-<harness>-<workflow>-<repo_id>`**. When
   `PANOPTICON_RUNTIME_ID` is set, a 12-character hash suffix separates that runtime's
   composed tags.

Every spawn first compares the base image's content/version fingerprint with the packaged
Dockerfile, entrypoint, Panopticon version, installed package source, and optional development
wheel, rebuilding the static `panopticon-base` tag when it is missing or stale. The packaged-source
digest is cached once per runner process: restart the runner after changing installed package
source so it computes a fresh digest. Docker layer-caches the base rebuild and composed image,
so unchanged inputs are cheap. Change a layer and the next spawn rebuilds the affected steps.

Each published Panopticon release also produces a multi-platform copy of that base image at
`ghcr.io/unsupervisedcom/panopticon-next:<version>` and updates `latest` for a non-prerelease. The
published image is built from the same release wheel and carries the same base fingerprint, so it
is an immutable distributable form of the locally built `panopticon-base`. The runner continues to
manage its local `panopticon-base` tag and does not implicitly pull from the registry.

> Workflows whose `runner_type` is `"shell"` (e.g. `setup-repo`) run on the host with no container,
> so they have no image and layers are ignored.

## Adding a workflow layer

Override `image_layer()` on your `Workflow` subclass to return a Dockerfile fragment string. The
default (`core/workflow.py`) returns `""` — no layer. Everything the string contains is baked into
the image, so use it for system-level installs your skills depend on. For example, a workflow that
renders diagrams could add Graphviz:

```python
def image_layer(self) -> str:
    return "RUN apt-get update && apt-get install --yes --no-install-recommends graphviz"
```

Notes:

- This is distinct from `tools()` and `skills()`. `image_layer()` puts a binary **in the image**;
  `tools()` just *names* an expected tool so the agent reaches for it, and `skills()` declares
  agent-driven procedures. A named tool may already be installed in the base image.
- Spell external-program flags in full (`apt-get install --yes`, `--no-install-recommends`) — they
  are self-documenting and grep-able.
- Keep it small. The base stays general on purpose; only add what the workflow's own skills need.

## Adding a repo layer

A repo layer is **operator-authored** and referenced by name, so you don't touch code:

1. **Write the fragment file** under the layers directory —
   `~/.config/panopticon/layers/` (`$PANOPTICON_CONFIG/layers`; `core/dirs.py` `LAYERS_DIR`). For
   example `~/.config/panopticon/layers/myrepo.dockerfile`:

   ```dockerfile
   # Layered on top of base → harness → workflow, so shared tools such as gh are already present.
   RUN curl --location --silent https://astral.sh/uv/install.sh | sh
   ```

   It's a plain Dockerfile fragment — `RUN`/`ENV`/`COPY` lines, no `FROM` (the composer supplies
   that). It inherits the preceding layer's build user and environment; set `USER` explicitly
   if the install needs a particular user. The entrypoint runs the task as the unprivileged
   `panopticon` user at container startup.

2. **Point the repo at it** in the dashboard's repo form: set `image_layer_file` to the file's
   **name** (`myrepo.dockerfile`), not a path. The form offers a picker over the files in your
   layers dir, with a custom-path entry that normalizes to a name.

The value is a **reference**, not inline content — see the `image_layer_file` field in
[`repos.md`](repos.md). The task service resolves it against its layers dir and serves the content
over REST; the runner composes it onto base → harness → workflow. Names that escape the layers
root (`..`, absolute paths) are rejected; nested names (`team/myrepo.dockerfile`) are allowed.
An empty or unset `image_layer_file` means no repo layer.

## Notes

- **The repo layer file lives with the task service.** `image_layer_file` resolves against
  the task service's layers directory; remote runners fetch its contents over REST. Unlike
  an `env_file` secret, it does not need a same-named file on every runner host.
- **Rebuilds & cleanup.** Editing a layer rebuilds the affected steps on the next spawn (Docker
  caches the rest). A packaged base-input or version change also refreshes a stale base
  automatically. `make clean` removes `panopticon-base` and every composed `panopticon-*` image;
  `make build` rebuilds the base immediately with the current development wheel. Changes to
  installed package source require a runner restart to refresh its cached source digest;
  `make build` remains the explicit development rebuild path.
- **Elevated privileges are a capability, not a layer.** Docker-in-Docker is opt-in via the repo's
  `capabilities` map (`docker_in_docker`), which makes the runner spawn `--privileged` and the
  entrypoint start a nested daemon — it is not something you add through a Dockerfile fragment.
- **Secrets never go in a layer.** Image layers are cached and shared; keep API keys and tokens in
  the repo's `env_file` (`docs/auth.md`), injected at launch, not baked into the image.
