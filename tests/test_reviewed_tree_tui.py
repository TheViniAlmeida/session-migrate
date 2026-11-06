"""Tree reviewed mode is opt-in; resume inherits it without manufacturing consent."""

import asyncio
import json
from pathlib import Path

import pytest
import test_tree_tui as tree_helpers
from test_reviewed_portability_core import source_rows
from test_tree_tui import activate, app_for, fill, idle

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, Input

from session_migrate.tree_cli import TreeWorkflow
from session_migrate.tui.widgets import Literal

tree_setup = tree_helpers.tree_setup
workspace = tree_helpers.workspace


def compacted(workspace):
    inventory = workspace[0]
    root = Path(inventory.canonical_root)
    for node in inventory.nodes:
        rows = source_rows()
        for row in rows:
            row.update(sessionId=node.source_native_id, cwd=str(workspace[1]["cwd"]))
        (root / node.relative_path).write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_tree_mode_does_not_acknowledge_and_confirm_remains_fresh(workspace, tmp_path):
    compacted(workspace)
    app = app_for(workspace, tmp_path)

    async def flow():
        async with app.run_test(size=(120, 55)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+t")
            await fill(app, pilot, workspace)
            screen = app.screen
            screen.query_one("#tree-version", Input).value = "2.1.309"
            assert not screen.query_one("#tree-reviewed-portability", Checkbox).value
            screen.query_one("#tree-partial", Checkbox).value = False
            screen.query_one("#tree-reviewed-portability", Checkbox).value = True
            await pilot.pause()
            assert not screen.query_one("#tree-partial", Checkbox).value
            await activate(screen, pilot, "prepare")
            await idle(app, pilot)
            assert screen.query_one("#tree-confirm", Button).disabled
            assert workspace[3] == []
            text = str(screen.query_one("#tree-metadata", Literal).render())
            assert '"reviewed_portability": true' in text
            assert "SYNTHETIC_PRIVATE_BODY" not in text
            screen.query_one("#tree-partial", Checkbox).value = True
            await pilot.pause()
            assert screen.query_one("#tree-confirm", Button).disabled
            await activate(screen, pilot, "prepare")
            await idle(app, pilot)
            assert not screen.query_one("#tree-confirm", Button).disabled
            assert screen.workflow.limits["max_source_bytes"] == 64 * 1024 * 1024
            assert workspace[3] == []
            await activate(screen, pilot, "confirm")
            await idle(app, pilot)
            assert screen.finished and len(workspace[3]) == 3
            screen.query_one("#tree-confirm", Button).press()
            await pilot.pause()
            assert len(workspace[3]) == 3

    asyncio.run(flow())


def test_tree_resume_omits_form_mode_and_updates_effective_mode_without_invalidation(
    workspace, tmp_path, monkeypatch
):
    compacted(workspace)
    app = app_for(workspace, tmp_path)
    options = workspace[1]
    workflow = app.controller.open_tree(profiles_file=workspace[5], reviewed_portability=True)
    try:
        workflow.load(source_profile_id="source", client_version="2.1.309", manifest=workspace[6])
        workflow.select(mode="subtree")
        workflow.prepare(
            "target",
            cwd=options["cwd"],
            model_provider="fixture",
            model="test",
            acknowledge_partial=True,
            reviewed_portability=True,
        )
        operation_id = workflow.commit()["operation_id"]
    finally:
        workflow.close()
    calls = []
    original = TreeWorkflow.prepare

    def prepare(workflow, *args, **kwargs):
        calls.append(kwargs)
        return original(workflow, *args, **kwargs)

    monkeypatch.setattr(TreeWorkflow, "prepare", prepare)

    async def flow():
        async with app.run_test(size=(120, 55)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+t")
            await fill(app, pilot, workspace)
            screen = app.screen
            screen.query_one("#tree-version", Input).value = "2.1.309"
            screen.query_one("#tree-operation", Input).value = operation_id
            assert not screen.query_one("#tree-reviewed-portability", Checkbox).value
            await activate(screen, pilot, "resume")
            await idle(app, pilot)
            await pilot.pause()
            assert calls and "reviewed_portability" not in calls[-1]
            assert screen.query_one("#tree-reviewed-portability", Checkbox).value
            assert screen.workflow is not None and screen.workflow.prepared is not None
            assert screen.workflow.limits["max_source_bytes"] == 64 * 1024 * 1024
            assert not screen.query_one("#tree-confirm", Button).disabled
            assert len(workspace[3]) == 3

    asyncio.run(flow())
