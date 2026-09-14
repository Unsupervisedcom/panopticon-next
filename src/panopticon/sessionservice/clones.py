"""Host-side repository clone caches, keyed by repository ID and stored source.

Each new task clones its source's cache. ``ensure`` clones on first use, then fetches and
fast-forwards that source's cache on reuse. Source edits select a separate cache without
changing old caches or task workspaces. Legacy caches are retained; disk accounting and GC
remain deferred (docs/design/BACKLOG.md). Git runs behind an injectable executor, with no LLM.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from panopticon.core.git import CommandRunner, _subprocess_run
from panopticon.sessionservice.git_credentials import RepoGitTransport


class CloneCache:
    """Maintains a separate local clone for each repo/source pair under ``root``.

    ``run`` (the ``git`` executor) and ``exists`` (the on-disk check) are injectable so the
    emitted commands and the clone-vs-fetch decision are unit-testable without a real repo.
    """

    def __init__(
        self,
        root: str,
        *,
        run: CommandRunner = _subprocess_run,
        exists: Callable[[str], bool] = os.path.isdir,
        makedirs: Callable[[str], None] = lambda p: Path(p).mkdir(parents=True, exist_ok=True),
    ) -> None:
        self._root = root.rstrip("/")
        self._run = run
        self._exists = exists
        self._makedirs = makedirs

    def path(self, repo_id: str, git_url: str) -> str:
        """Key by stored source, preserving old caches when a repo source is edited."""
        source_key = hashlib.sha256(json.dumps([repo_id, git_url]).encode()).hexdigest()
        return f"{self._root}/{source_key}"

    def _run_source_command(self, command: Sequence[str]) -> None:
        try:
            self._run(command)
        except subprocess.CalledProcessError:
            raise RuntimeError(
                "Repository clone/fetch failed. Check that the source exists and is readable "
                "on the runner, and that the cache directory is writable. For remote repositories, "
                "check the URL, network access, and repository credentials. "
                "Press g, then e to edit the source."
            ) from None

    def ensure(
        self,
        repo_id: str,
        git_url: str,
        *,
        transport: RepoGitTransport | None = None,
    ) -> str:
        """Ensure the repo's clone exists and is current, returning its path. Idempotent.

        Clones from ``git_url`` on first use; on later calls fetches (``--all --prune``) **and
        fast-forwards the checked-out base branch** to its upstream, so the branch a per-task clone
        is cut from is actually current. (``fetch`` alone only moves ``origin/<base>``; the local
        base branch — which ``git clone`` copies into the per-task clone — would stay at the
        commit it was first cloned at, so every task would start behind.)
        """
        path = self.path(repo_id, git_url)
        transport = transport or RepoGitTransport(git_url, git_url)
        if self._exists(path):
            fetch = ["git", "-C", path, "fetch", "--all", "--prune"]
            if transport.credentialed:
                # Override the remote only for this network operation. The cache keeps the
                # registered source spelling in persistent configuration.
                fetch[1:1] = [
                    "-c",
                    f"url.{transport.operation_url}.insteadOf={git_url}",
                ]
            with transport.git_command(fetch) as command:
                self._run_source_command(command)
            self._run(
                ["git", "-C", path, "merge", "--ff-only"]
            )  # advance the base branch to upstream
        else:
            self._makedirs(self._root)
            with transport.git_command(["git", "clone", transport.operation_url, path]) as command:
                self._run_source_command(command)
            if transport.credentialed and transport.operation_url != git_url:
                self._run(["git", "-C", path, "remote", "set-url", "origin", git_url])
        return path
