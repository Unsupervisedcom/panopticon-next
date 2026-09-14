"""The claude JSON-config helper: a read-merge-write that never clobbers keys it didn't set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panopticon.harnesses.config import update_json_config


def test_update_json_config_starts_empty_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    with update_json_config(path) as data:  # parent dir created on demand
        data["a"] = 1
    assert json.loads(path.read_text()) == {"a": 1}


def test_update_json_config_merges_into_existing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"keep": "me", "override": "old"}')

    with update_json_config(path) as data:
        data["override"] = "new"
        data["added"] = True

    assert json.loads(path.read_text()) == {"keep": "me", "override": "new", "added": True}


@pytest.mark.parametrize("contents", [" []\n", "[1]", "null", "42", "true", '"private-value"'])
def test_update_json_config_rejects_non_objects_without_changing_file(
    tmp_path: Path, contents: str
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(contents)
    original = path.read_bytes()

    with pytest.raises(ValueError) as error, update_json_config(path):
        pytest.fail("invalid configuration must be rejected before the caller can mutate it")

    assert str(error.value) == (
        f"Config file {path} must contain a JSON object. "
        "Restore or repair the file, then retry the task."
    )
    assert path.read_bytes() == original


def test_update_json_config_leaves_file_untouched_on_error(tmp_path: Path) -> None:
    # A raise inside the block aborts the write — no half-applied config lands on disk.
    path = tmp_path / "config.json"
    path.write_text('{"keep": "me"}')

    with pytest.raises(RuntimeError), update_json_config(path) as data:
        data["added"] = True
        raise RuntimeError("boom")

    assert json.loads(path.read_text()) == {"keep": "me"}  # unchanged
