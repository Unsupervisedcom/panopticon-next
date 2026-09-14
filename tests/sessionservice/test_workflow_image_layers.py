"""Exact audit gate for the complete built-in workflow image-layer set."""

from __future__ import annotations

from pathlib import Path

from panopticon.workflows.discovery import discover_workflows


# 2119: REQ-022.2
def test_every_shipped_workflow_layer_matches_audited_content(tmp_path: Path) -> None:
    workflows = discover_workflows(_home_workflows=tmp_path / "no-home-workflows")
    empty_layer = ""
    codex_only_layer = r"""RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
      x86_64) triple="x86_64-unknown-linux-musl" ;; \
      aarch64) triple="aarch64-unknown-linux-musl" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl --fail --silent --show-error --location \
      "https://github.com/openai/codex/releases/download/rust-v0.144.4/codex-$triple.tar.gz" \
      | tar --extract --gzip --directory /usr/local/bin; \
    if [ -e "/usr/local/bin/codex-$triple" ]; then mv "/usr/local/bin/codex-$triple" /usr/local/bin/codex; fi; \
    chmod 0755 /usr/local/bin/codex"""
    # Exact audited layer bytes: an altered or newly shipped layer cannot evade this gate by
    # spelling, downloading, or renaming the gh executable differently.
    expected = {
        "github-peer-reviewed": empty_layer,
        "github-self-reviewed": empty_layer,
        "local-git-self-reviewed": empty_layer,
        "orchestrator": empty_layer,
        "review": empty_layer,
        "setup-repo": empty_layer,
        "2119-auto-spec": codex_only_layer,
        "2119-auto-sol": codex_only_layer,
        "2119-human-spec": codex_only_layer,
        "spike": empty_layer,
    }
    actual = {name: workflow.image_layer() for name, workflow in workflows.items()}
    assert actual == expected
