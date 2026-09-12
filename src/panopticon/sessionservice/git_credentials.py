"""Repository-scoped GitHub credentials for host Git and task-clone origins.

The runner reads only the selected repository's host-local env-file. Host network commands receive
the token through a short-lived Git credential helper, while task clones retain only a reference
to ``gh``'s credential helper and receive ``GH_TOKEN`` through their existing env-file transport.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from panopticon.core.dirs import _secrets_dir
from panopticon.core.git import CommandRunner, _subprocess_run

_SCP_GITHUB: Final = re.compile(r"^(?P<user>[^@/:]+)@(?P<host>[^/:]+):(?P<path>[^?#]+)$")
_GITHUB_SCHEMES: Final = frozenset({"https", "ssh"})
_CREDENTIAL_PREFIX: Final = "panopticon-git-auth-"


class GitCredentialError(RuntimeError):
    """The selected repository's runner-local Git credential cannot be used safely."""


@dataclass(frozen=True)
class RepoGitTransport:
    """One repository's stored source, operational URL, and optional scoped token."""

    source_url: str
    operation_url: str
    token: str | None = field(default=None, repr=False)

    @property
    def credentialed(self) -> bool:
        return self.token is not None

    @contextmanager
    def git_command(self, args: Sequence[str]) -> Iterator[list[str]]:
        """Yield one network Git command with a private, temporary credential helper."""
        command = list(args)
        if self.token is None:
            yield command
            return
        if not command or command[0] != "git":
            raise ValueError("repository credentials may be applied only to git commands")
        with tempfile.TemporaryDirectory(prefix=_CREDENTIAL_PREFIX) as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            token_file = directory / "token"
            helper = directory / "credential-helper"
            operation = urlsplit(self.operation_url)
            credential_scope = f"{operation.scheme}://{operation.netloc}"
            expected_path = operation.path.lstrip("/")
            _write_private(token_file, self.token + "\n", 0o600)
            helper_source = (
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  get)\n"
                "    protocol= host= path=\n"
                "    while IFS='=' read -r key value; do\n"
                '      case "$key" in\n'
                "        protocol) protocol=$value ;;\n"
                "        host) host=$value ;;\n"
                "        path) path=$value ;;\n"
                "      esac\n"
                "    done\n"
                f'    test "$protocol" = {shlex.quote(operation.scheme)} || exit 0\n'
                f'    test "$host" = {shlex.quote(operation.netloc)} || exit 0\n'
                f'    test "$path" = {shlex.quote(expected_path)} || exit 0\n'
                "    printf '%s\\n' 'username=x-access-token'\n"
                "    printf '%s' 'password='\n"
                f"    cat -- {shlex.quote(str(token_file))}\n"
                "    printf '\\n'\n"
                "    ;;\n"
                "  store|erase) exit 0 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
            )
            _write_private(helper, helper_source, 0o700)
            yield [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper=!{helper}",
                "-c",
                f"credential.{credential_scope}.useHttpPath=true",
                "-c",
                "http.extraHeader=",
                *command[1:],
            ]


def _write_private(path: Path, content: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        # ``open(..., mode)`` is filtered through the process umask.  Reassert the exact mode so a
        # restrictive umask cannot accidentally make the credential helper non-executable.
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _github_https_url(source: str) -> str | None:
    """Return the credential-capable URL for one supported exact-host GitHub source."""
    source = source.strip()
    if match := _SCP_GITHUB.fullmatch(source):
        if match.group("host").lower() != "github.com":
            return None
        path = match.group("path").lstrip("/")
        return f"https://github.com/{path}" if len(path.split("/")) >= 2 else None

    parsed = urlsplit(source)
    scheme = parsed.scheme.lower()
    if scheme not in _GITHUB_SCHEMES or parsed.hostname is None:
        return None
    try:
        if parsed.port is not None:
            return None
    except ValueError:
        return None
    if (
        parsed.hostname.lower() != "github.com"
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (scheme == "https" and parsed.username is not None)
    ):
        return None
    path = parsed.path.lstrip("/")
    if len(path.rstrip("/").split("/")) < 2:
        return None
    return f"https://github.com/{path}"


def _read_private_env(reference: str, *, secrets_dir: str | Path | None = None) -> str:
    """Read a relative owner-only env-file without following path-component symlinks."""
    relative = Path(reference)
    if relative.is_absolute() or not relative.name or ".." in relative.parts:
        raise GitCredentialError("repository Git credential reference is invalid")
    root = Path(secrets_dir) if secrets_dir is not None else _secrets_dir()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory = os.open(root, directory_flags)
        try:
            for part in relative.parts[:-1]:
                child = os.open(part, directory_flags, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
    except OSError as exc:
        raise GitCredentialError(
            "repository Git credential file is unavailable on this runner"
        ) from exc
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise GitCredentialError(
            "repository Git credential file must be an owner-only regular file"
        )
    try:
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeError) as exc:
        raise GitCredentialError(
            "repository Git credential file is unreadable on this runner"
        ) from exc


def _env_values(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


def repo_git_transport(
    repo: Mapping[str, object], *, secrets_dir: str | Path | None = None
) -> RepoGitTransport:
    """Resolve only this repository's runner-local ``GH_TOKEN`` and operational URL."""
    source = str(repo["git_url"])
    https_url = _github_https_url(source)
    reference = repo.get("env_file")
    if https_url is None or not reference:
        return RepoGitTransport(source, source)
    content = _read_private_env(str(reference), secrets_dir=secrets_dir)
    token = _env_values(content).get("GH_TOKEN", "").strip()
    if not token:
        return RepoGitTransport(source, source)
    if any(character in token for character in "\r\n\0"):
        raise GitCredentialError("repository Git credential must be one line")
    return RepoGitTransport(source, https_url, token)


def configure_task_git_credentials(
    repo_path: str,
    transport: RepoGitTransport,
    *,
    run: CommandRunner = _subprocess_run,
) -> None:
    """Use the container's existing ``gh`` helper for the task's credential-free HTTPS origin."""
    if not transport.credentialed:
        return
    run(["git", "-C", repo_path, "config", "--local", "credential.helper", ""])
    run(
        [
            "git",
            "-C",
            repo_path,
            "config",
            "--local",
            "credential.https://github.com.helper",
            "!gh auth git-credential",
        ]
    )
    run(
        [
            "git",
            "-C",
            repo_path,
            "config",
            "--local",
            "credential.https://github.com.useHttpPath",
            "true",
        ]
    )
