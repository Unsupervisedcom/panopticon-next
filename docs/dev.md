# Developing panopticon

Working *on* panopticon rather than running it? This is the core development loop: set up a
venv, run the checks, and (optionally) bring the stack up locally. Just want to *use*
panopticon? Start with the [README](../README.md). Want the full picture — the module map,
conventions, and the tests worth knowing — read [`AGENTS.md`](../AGENTS.md) and the design
docs under [`docs/design/`](design/README.md).

## Prerequisites

- **Python 3.11+**
- **[`uv`](https://docs.astral.sh/uv/)** — the package/venv manager (`brew install uv`, or the
  astral installer).

That's all you need to edit code and run the checks. **Running the whole stack** additionally
needs Docker, tmux, git, and the `claude` CLI — see the
[README requirements](../README.md#requirements) (and [`docs/macos-setup.md`](macos-setup.md)
on macOS).

A [`Makefile`](../Makefile) wraps the `uv` commands; `make help` lists every target.

## Setup

```sh
make sync        # uv sync — create the venv and install deps (incl. the dev group)
```

## The check loop

`make check` is the inner loop — it's exactly what CI runs, so if it's green locally the PR
gate will be too:

```sh
make check       # lint-check + typecheck + test
```

Run the pieces individually while iterating:

| Command | What it does |
|---|---|
| `make lint` | Ruff **lint + auto-format** (`ruff check --fix` then `ruff format`) — fixes in place |
| `make format` | Format only (`ruff format`) |
| `make typecheck` | `mypy --package panopticon` (strict) |
| `make test` | `pytest` |
| `make lint-check` | Lint + format **check, read-only** — the CI-parity gate `make check` uses |

The distinction that matters: `make lint` **modifies** your files (auto-fix + format), while
`make lint-check` only **reports** (it's what CI runs, so it never rewrites code). Run
`make lint` before committing to fix findings; `make check` to confirm you match CI.

Ruff owns line width via `ruff format`, so there's no separate line-length nag. The ruleset
lives under `[tool.ruff]` in [`pyproject.toml`](../pyproject.toml).

## Running the stack locally

To exercise the real system (not just the tests) you need the container toolchain from the
prerequisites above. Build the base image once, then bring everything up:

```sh
make build       # docker-build the base task-container image (needed before spawning tasks)
make start       # task service + session-service runner + dashboard supervisor
make stop        # tear it all down (task containers + the -L panopticon tmux server)
```

Individual pieces, when you want just one:

- `make serve` — the task service (control plane) over HTTP
- `make dashboard` — the dashboard once, foreground (no tmux)
- `make host` — task service + session-service host in background tmux (headless/CI)

See [`docs/overview.md`](overview.md) for the mental model behind these pieces, and the
[README quickstart](../README.md#quickstart) for the end-to-end first run.

## Database migrations

Schema is managed by **Alembic**. After changing the ORM rows, generate and apply a migration:

```sh
make migrate-revision MSG="describe the change"   # autogenerate from ORM changes
make migrate                                       # apply up to head
```

Commit the generated file under `src/panopticon/migrations/versions/`.
`tests/test_migrations.py` guards the migrations against drift from the ORM schema. See the
Dev-commands section of [`AGENTS.md`](../AGENTS.md) for the details.

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push to `main` and
every PR (on a Python 3.13 runner). It is the same sequence `make check` wraps:

```
uv sync
uv run ruff check           # lint
uv run ruff format --check  # format check
uv run mypy --package panopticon
uv run pytest
```

So `make check` locally reproduces the PR gate — get it green before you push.

## Releases

Release Please reads Conventional Commit headers after each push to `main` and opens or updates a
release PR. Merging an approved release PR creates a `v<version>` GitHub release. That published
release builds both Python distributions, publishes them to PyPI with trusted publishing, and
publishes the same release wheel in a multi-platform base image at
`ghcr.io/unsupervisedcom/panopticon-next:<version>`. Non-prereleases also update the `latest` tag.

Complete this setup before merging the automation PR:

1. GitHub Actions MUST have access to a `RELEASE_PLEASE_TOKEN` repository or inherited
   organization secret. Its fine-grained PAT MUST grant `Unsupervisedcom/panopticon-next` write
   access to contents, issues, and pull requests. This follows the dedicated-PAT convention used
   by the ai-outfitter repositories because organization policy can prevent the default
   `GITHUB_TOKEN` from opening PRs.
2. GitHub MUST have an environment named `pypi`.
3. The `panopticon-next` project on PyPI MUST configure a trusted publisher with owner
   `Unsupervisedcom`, repository `panopticon-next`, workflow `release.yml`, and environment `pypi`.
   Trusted publishing uses GitHub OIDC, so no `PYPI_TOKEN` secret is needed.

Before merging each generated release PR, an independent reviewer MUST approve it. Inspect the
proposed version and stop for explicit user approval if it is a major version or carries a breaking
change. After merge, verify that the GitHub Release, PyPI version, immutable GHCR version tag, and
non-prerelease `latest` tag were published successfully.

The image job uses the release wheel rather than resolving an unpinned package from the index. It
MUST verify that the release tag, wheel metadata, image CLI version, and base-image fingerprint all
agree before pushing the immutable version tag. It publishes `linux/amd64` and `linux/arm64` under
the repository-scoped `GITHUB_TOKEN` with `packages: write` permission.

Every release PR MUST receive review before merge. A major version or any breaking-change marker
MUST NOT be authored or merged without explicit user approval; see [`AGENTS.md`](../AGENTS.md).

## Where to go deeper

- [`AGENTS.md`](../AGENTS.md) — the operating manual: the determinism invariant, module map,
  conventions, dev commands, and the tests worth knowing.
- [`docs/design/`](design/README.md) — goals, architecture, roadmap, and the ADRs.
- [`docs/overview.md`](overview.md) — how the running pieces fit together.
