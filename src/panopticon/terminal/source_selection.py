"""Repository-source selection, validation, equivalence, and stable identity."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

SourceKind = Literal["remote", "checkout", "bundle"]

_REMOTE_SCHEMES = frozenset({"ftp", "ftps", "git", "http", "https", "ssh"})
_GITHUB_SCHEMES = frozenset({"https", "ssh"})
_SCP_REMOTE = re.compile(r"^(?P<user>[^@/:]+)@(?P<host>[^/:]+):(?P<path>.+)$")
_ID_COMPONENT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class RepositorySource:
    """A validated source ready to show to the operator and persist on a repo."""

    git_url: str
    name: str
    kind: SourceKind

    @property
    def description(self) -> str:
        if self.kind == "bundle":
            return "Git bundle (fixed snapshot)"
        if self.kind == "checkout":
            return "local Git checkout"
        if _github_identity(self.git_url) is not None:
            return "GitHub remote"
        return "Git remote"


def _runner(run: Callable[..., Any] | None) -> Callable[..., Any]:
    return run or subprocess.run


def _run_git(
    args: list[str], *, run: Callable[..., Any] | None = None
) -> subprocess.CompletedProcess[str]:
    return cast(
        "subprocess.CompletedProcess[str]",
        _runner(run)(["git", *args], capture_output=True, text=True, check=True),
    )


def _remote_parts(source: str) -> tuple[str, str, str] | None:
    """Return ``(scheme, host, path)`` for a supported remote spelling."""
    if match := _SCP_REMOTE.fullmatch(source):
        return "scp", match.group("host"), match.group("path")
    parsed = urlsplit(source)
    if parsed.scheme.lower() not in _REMOTE_SCHEMES or not parsed.hostname or not parsed.path:
        return None
    return parsed.scheme.lower(), parsed.hostname, parsed.path


def _looks_like_remote(source: str) -> bool:
    return _SCP_REMOTE.fullmatch(source) is not None or urlsplit(source).scheme.lower() in (
        _REMOTE_SCHEMES
    )


def _remote_is_safe(source: str) -> bool:
    if _SCP_REMOTE.fullmatch(source) is not None:
        return "?" not in source and "#" not in source
    parsed = urlsplit(source)
    if parsed.scheme.lower() not in _REMOTE_SCHEMES:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.hostname is not None
        and bool(parsed.path)
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (parsed.scheme.lower() == "ssh" or parsed.username is None)
    )


def _github_identity(source: str) -> tuple[str, ...] | None:
    """Canonical GitHub owner/repository path for supported GitHub transports."""
    source = source.strip()
    if not _remote_is_safe(source):
        return None
    if match := _SCP_REMOTE.fullmatch(source):
        scheme = "scp"
        host = match.group("host")
        path = match.group("path")
    else:
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
            parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (scheme == "https" and parsed.username is not None)
        ):
            return None
        host = parsed.hostname
        path = parsed.path
    if host.lower() != "github.com":
        return None
    normalized_path = path.replace("\\", "/").rstrip("/")
    if normalized_path.lower().endswith(".git"):
        normalized_path = normalized_path[:-4]
    pieces = tuple(piece.lower() for piece in normalized_path.strip("/").split("/") if piece)
    return pieces if len(pieces) >= 2 else None


def _file_url_path(source: str) -> Path | None:
    parsed = urlsplit(source)
    if parsed.scheme.lower() != "file":
        return None
    if parsed.netloc not in {"", "localhost"}:
        raise RuntimeError(f"unsupported local Git source {source!r}: file URL has a remote host")
    return Path(unquote(parsed.path))


def _local_path(source: str, *, cwd: Path | None = None) -> Path | None:
    if _remote_parts(source) is not None:
        return None
    path = _file_url_path(source)
    if path is None:
        path = Path(source).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    return path.resolve()


def _checkout_root(path: Path, *, run: Callable[..., Any] | None = None) -> Path | None:
    try:
        result = _run_git(["-C", str(path), "rev-parse", "--show-toplevel"], run=run)
    except (FileNotFoundError, subprocess.CalledProcessError):
        result = None
    if result is not None and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    try:
        bare = _run_git(["-C", str(path), "rev-parse", "--is-bare-repository"], run=run)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return path.resolve() if bare.stdout.strip() == "true" else None


def _bundle_name(path: Path) -> str:
    return path.name[:-7] if path.name.lower().endswith(".bundle") else path.stem


def _remote_name(source: str) -> str:
    parts = _remote_parts(source)
    if parts is None:
        return "repo"
    tail = parts[2].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if tail.lower().endswith(".git"):
        tail = tail[:-4]
    return tail or "repo"


def resolve_source(
    value: str,
    *,
    cwd: Path | None = None,
    run: Callable[..., Any] | None = None,
) -> RepositorySource:
    """Validate and describe an explicit Git remote, checkout, or bundle."""
    source = value.strip()
    if not source:
        raise RuntimeError("repository source cannot be empty")
    if _looks_like_remote(source):
        if not _remote_is_safe(source):
            raise RuntimeError("invalid Git remote URL: embedded credentials are not supported")
        return RepositorySource(source, _remote_name(source), "remote")

    path = _local_path(source, cwd=cwd)
    assert path is not None
    if path.is_file():
        try:
            with tempfile.TemporaryDirectory(prefix="panopticon-bundle-") as temporary:
                _run_git(["init", "--bare", temporary], run=run)
                _run_git(["-C", temporary, "bundle", "verify", str(path)], run=run)
        except FileNotFoundError as exc:
            raise RuntimeError(f"cannot validate Git bundle {path}: git is not installed") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"invalid Git bundle: {path}") from exc
        return RepositorySource(str(path), _bundle_name(path), "bundle")
    if path.is_dir() and (root := _checkout_root(path, run=run)) is not None:
        return RepositorySource(str(root), root.name, "checkout")
    kind = "Git bundle" if path.name.lower().endswith(".bundle") else "local Git checkout"
    raise RuntimeError(f"invalid {kind}: {path}")


def detect_current_source(
    *, cwd: Path | None = None, run: Callable[..., Any] | None = None
) -> RepositorySource | None:
    """Describe the checkout containing ``cwd``, preferring its origin when present."""
    location = (cwd or Path.cwd()).resolve()
    root = _checkout_root(location, run=run)
    if root is None:
        return None
    try:
        result = _run_git(
            ["-C", str(root), "config", "--get", "remote.origin.url"],
            run=run,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        result = None
    if result is not None and result.stdout.strip():
        try:
            return resolve_source(result.stdout.strip(), cwd=root, run=run)
        except RuntimeError:
            # A legacy checkout may contain an unsafe credential-bearing origin. Keep setup usable
            # without displaying that value by offering the checkout's canonical local path.
            return RepositorySource(str(root), root.name, "checkout")
    return RepositorySource(str(root), root.name, "checkout")


def detect_git_url() -> str:
    """Return the current checkout's origin, or its canonical path when it has no origin."""
    source = detect_current_source()
    if source is None:
        raise RuntimeError(
            "quickstart could not identify a Git repository in the current directory; "
            "choose a repository source explicitly"
        )
    return source.git_url


def select_source(
    *,
    cwd: Path | None = None,
    input_fn: Callable[[str], str] = input,
    run: Callable[..., Any] | None = None,
) -> RepositorySource:
    """Suggest the current checkout and let the operator accept or replace it."""
    location = (cwd or Path.cwd()).resolve()
    suggested = detect_current_source(cwd=location, run=run)
    if suggested is not None:
        print(f"Suggested repository: {suggested.name}")
        print(f"  Source: {suggested.git_url}")
        prompt = f"Repository source [{suggested.git_url}]: "
    else:
        prompt = "Repository source (Git URL, local checkout, or Git bundle): "

    while True:
        answer = input_fn(prompt).strip()
        if not answer and suggested is not None:
            selected = suggested
        elif answer:
            try:
                selected = resolve_source(answer, cwd=location, run=run)
            except RuntimeError as exc:
                print(f"panopticon: {exc}")
                prompt = "Repository source: "
                continue
        else:
            print("panopticon: repository source cannot be empty")
            continue
        print(f"Selected repository: {selected.name}")
        print(f"  Source: {selected.git_url}")
        print(f"  Type: {selected.description}")
        return selected


def source_name(source: str) -> str:
    """Friendly name derived without validating an already-selected source."""
    path = _local_path(source)
    if path is None:
        return _remote_name(source)
    if path.is_file() or path.name.lower().endswith(".bundle"):
        return _bundle_name(path)
    return path.name or "repo"


def source_identity(source: str) -> str:
    """Conservative stable identity used for equality and new repository IDs."""
    stripped = source.strip()
    if github := _github_identity(stripped):
        return "github:" + "/".join(github)
    if _looks_like_remote(stripped):
        return "remote:" + stripped
    path = _local_path(stripped)
    assert path is not None
    kind = "bundle" if path.is_file() or path.name.lower().endswith(".bundle") else "checkout"
    return f"{kind}:{path}"


def sources_equivalent(left: str, right: str) -> bool:
    """Whether two stored source strings identify the same bounded source."""
    if not left.strip() or not right.strip():
        return False
    try:
        left_identity = source_identity(left)
        right_identity = source_identity(right)
    except (OSError, RuntimeError, ValueError):
        # Repository rows can predate the current source-validation contract. An
        # unsupported stored spelling must not prevent setup from examining the
        # remaining rows, and it is never safe to infer that spelling is equivalent.
        return False
    return left_identity == right_identity


def _id_stem(source: str) -> str:
    stem = _ID_COMPONENT.sub("-", source_name(source).lower()).strip("-")
    return (stem or "repo")[:48].rstrip("-")


def repo_id_candidates(source: str) -> tuple[str, ...]:
    """Bounded deterministic candidates, widening the identity digest after a collision."""
    digest = hashlib.sha256(source_identity(source).encode()).hexdigest()
    stem = _id_stem(source)
    return tuple(f"{stem}-{digest[:length]}" for length in (8, 12, 16, 24, 32, 64))


def repo_id_from_url(source: str) -> str:
    """Derive a stable source-qualified repository ID."""
    return repo_id_candidates(source)[0]


def is_github_source(source: str) -> bool:
    """Whether quickstart may enable workflows that require GitHub."""
    return _github_identity(source) is not None
