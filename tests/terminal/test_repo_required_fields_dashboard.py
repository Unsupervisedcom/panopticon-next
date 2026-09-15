"""Required-field errors preserve the repository editor and its unsaved input."""

from __future__ import annotations

from copy import deepcopy

import pytest
from test_dashboard import _FakeClient
from textual.widgets import Input, Static

from panopticon.terminal.dashboard import Dashboard, RepoFormScreen, ReposScreen


@pytest.mark.parametrize("field", ["name", "git_url"])
@pytest.mark.parametrize("blank", ["", " \t "])
async def test_repo_edit_rejects_blank_required_fields_and_keeps_the_draft(
    field: str, blank: str
) -> None:
    original = {
        "id": "r1",
        "name": "Project name",
        "git_url": "/work/project with spaces",
        "default_base": "main",
        "capabilities": {"preserved": True},
    }
    fake = _FakeClient([], repos=[deepcopy(original)])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "e")
        await pilot.pause()
        form = app.screen
        form.query_one("#field-default_base", Input).value = "unsaved-branch"
        form.query_one(f"#field-{field}", Input).value = blank
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen is form and isinstance(form, RepoFormScreen)
        assert "required" in str(form.query_one("#form-error", Static).render())
        assert fake.updated_repos == []
        assert fake.list_repos() == [original]
        assert form.query_one(f"#field-{field}", Input).value == blank
        assert form.query_one("#field-default_base", Input).value == "unsaved-branch"

        form.query_one(f"#field-{field}", Input).value = str(original[field])
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReposScreen)
        assert len(fake.updated_repos) == 1
        saved = fake.list_repos()[0]
        assert saved["name"] == original["name"]
        assert saved["git_url"] == original["git_url"]
        assert saved["default_base"] == "unsaved-branch"
        assert saved["capabilities"]["preserved"] is True


@pytest.mark.parametrize("blank", ["", " \t "])
async def test_repo_create_still_rejects_blank_source_inline(blank: str) -> None:
    fake = _FakeClient([], repos=[])
    app = Dashboard(fake)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g", "n")
        await pilot.pause()
        app.screen.query_one("#field-id", Input).value = "new"
        app.screen.query_one("#field-name", Input).value = "New project"
        app.screen.query_one("#field-git_url", Input).value = blank
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, RepoFormScreen)
        assert "required" in str(app.screen.query_one("#form-error", Static).render())
        assert app.screen.query_one("#field-name", Input).value == "New project"
        assert fake.created_repos == []
        assert fake.list_repos() == []
