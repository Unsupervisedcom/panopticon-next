"""Private transport for explicitly configured operator migration authorization."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from panopticon.taskservice.auth import _read_regular_file, credential_path

OPERATOR_AUTH_FILE = "operator-auth.json"
OPERATOR_AUTH_ENV = "PANOPTICON_OPERATOR_TOKEN_FILE"


def _load(path: Path) -> str:
    try:
        data = json.loads(_read_regular_file(path))
        token = data["token"]
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n\0"):
            raise ValueError
        return token
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Operator credential file is invalid or unavailable.") from exc


def operator_token(*, secrets_dir: str | Path | None = None) -> str | None:
    """Read an explicit environment token or its private operator-only file."""
    if token := os.environ.get("PANOPTICON_OPERATOR_TOKEN"):
        return token
    reference = os.environ.get(OPERATOR_AUTH_ENV)
    path = credential_path(reference or OPERATOR_AUTH_FILE, secrets_dir=secrets_dir)
    if reference or os.path.lexists(path):
        return _load(path)
    return None


def persist_operator_token(*, allow_create: bool) -> None:
    """Save an explicitly supplied token once; never replace existing authorization."""
    token = os.environ.get("PANOPTICON_OPERATOR_TOKEN")
    reference = os.environ.get(OPERATOR_AUTH_ENV)
    if not token:
        if reference:
            _load(credential_path(reference))
        return
    if any(c in token for c in "\r\n\0"):
        raise ValueError("Operator token must be one line.")
    path = credential_path(reference or OPERATOR_AUTH_FILE)
    if os.path.lexists(path):
        if _load(path) != token:
            raise ValueError(
                "Operator credential differs from the saved file. Stop the runtime, "
                "replace its private operator credential file, then restart."
            )
        return
    if not allow_create:
        raise ValueError("Stop the running service before configuring operator authorization.")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Operator credentials require an owner-only secrets directory.")
    fd, temporary = tempfile.mkstemp(prefix=".operator-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"token": token}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _load(path) != token:
                raise ValueError(
                    "Another startup selected different operator credentials."
                ) from None
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
