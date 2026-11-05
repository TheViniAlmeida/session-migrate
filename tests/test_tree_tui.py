"""Keyboard tree review keeps work off the UI thread and observes cancelled commits."""

import asyncio
import json
import threading

import pytest
import test_tree_cli as cli_fixtures
import test_tree_operations as publisher_fixtures

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, Input, Tree

from session_migrate.catalog import Catalog
from session_migrate.tree_cli import TreeWorkflow
from session_migrate.tui.app import MigrationApp
from session_migrate.tui.controller import Controller
from session_migrate.tui.tree_screen import TreeScreen
from session_migrate.tui.widgets import Literal

workspace = cli_fixtures.workspace
tree_setup = publisher_fixtures.setup


def app_for(workspace, tmp_path):
    _, options, _, _, _, storage, _, _ = workspace
    catalog = tmp_path / "catalog.sqlite3"
    with Catalog(catalog):
        pass
    controller = Controller(catalog, environ=options["base_environ"])
    controller.continuity_store_path = options["state_home"] / "lineage-v1"
    return MigrationApp(controller, profiles_file=storage)


async def idle(app, pilot):
    for _ in range(250):
        await pilot.pause(0.01)
        if (app.screen.future if isinstance(app.screen, TreeScreen) else app.pending) is None:
            return
    raise AssertionError("Tree worker did not finish")


async def fill(app, pilot, workspace):
    _, options, _, _, _, _, manifest, _ = workspace
    for name, value in {
        "source": "source",
        "version": "2.1.318",
        "manifest": str(manifest),
        "target": "target",
        "cwd": str(options["cwd"]),
        "provider": "fixture",
        "model": "test",
    }.items():
        app.screen.query_one(f"#tree-{name}", Input).value = value
    app.screen.query_one("#tree-partial", Checkbox).value = True
    await pilot.pause(0.05)


async def activate(screen, pilot, name):
    screen.query_one(f"#tree-{name}", Button).focus()
    await pilot.press("enter")
    await pilot.pause(0.02)


def test_keyboard_opens_tree_review_and_confirm_publishes_once(workspace, tmp_path):
    app = app_for(workspace, tmp_path)

    async def flow():
        async with app.run_test(size=(120, 48)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TreeScreen)
            await fill(app, pilot, workspace)
            await activate(app.screen, pilot, "prepare")
            await idle(app, pilot)
            assert not app.screen.query_one("#tree-confirm", Button).disabled
            assert workspace[3] == []
            await activate(app.screen, pilot, "confirm")
            await idle(app, pilot)
            assert app.screen.finished and len(workspace[3]) == 3
            assert app.screen.query_one("#tree-confirm", Button).disabled
            assert "PRIVATE_BODY_SENTINEL" not in str(
                app.screen.query_one("#tree-metadata", Literal).render()
            )
            await pilot.press("escape")
            assert not isinstance(app.screen, TreeScreen)

    asyncio.run(flow())


def test_escape_during_deferred_read_keeps_ui_responsive_and_discards_result(
    workspace, tmp_path, monkeypatch
):
    app = app_for(workspace, tmp_path)
    entered, release, discarded = threading.Event(), threading.Event(), threading.Event()
    original_open = app.controller.open_tree
    main_thread = threading.get_ident()

    def delayed(**kwargs):
        assert threading.get_ident() != main_thread
        entered.set()
        release.wait(10)
        result = original_open(**kwargs)
        original_close = result.close

        def close():
            discarded.set()
            original_close()

        result.close = close
        return result

    monkeypatch.setattr(app.controller, "open_tree", delayed)

    async def flow():
        async with app.run_test(size=(100, 35)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+t")
            await fill(app, pilot, workspace)
            await activate(app.screen, pilot, "inventory")
            assert entered.is_set()
            await pilot.resize_terminal(70, 24)
            await pilot.press("escape")
            assert not isinstance(app.screen, TreeScreen)
            release.set()
            for _ in range(100):
                await pilot.pause(0.01)
                if discarded.is_set():
                    break
            assert discarded.is_set() and workspace[3] == []

    try:
        asyncio.run(flow())
    finally:
        release.set()


def test_escape_during_deferred_commit_waits_for_cancelled_receipt(
    workspace, tmp_path, monkeypatch
):
    app = app_for(workspace, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = TreeWorkflow.commit

    def delayed(workflow, **kwargs):
        entered.set()
        release.wait(10)
        return original(workflow, **kwargs)

    monkeypatch.setattr(TreeWorkflow, "commit", delayed)

    async def flow():
        async with app.run_test(size=(120, 48)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+t")
            await fill(app, pilot, workspace)
            await activate(app.screen, pilot, "prepare")
            await idle(app, pilot)
            await activate(app.screen, pilot, "confirm")
            assert entered.is_set()
            await pilot.press("escape")
            assert isinstance(app.screen, TreeScreen) and app.screen.pending == "commit"
            release.set()
            await idle(app, pilot)
            assert app.screen.finished and workspace[3] == []
            assert "cancelled" in str(app.screen.query_one("#tree-metadata", Literal).render())

    try:
        asyncio.run(flow())
    finally:
        release.set()


def test_visual_tree_keeps_markup_literal_and_reselecting_anchor_invalidates(workspace, tmp_path):
    manifest = workspace[6]
    value = json.loads(manifest.read_text())
    label = "[bold]Literal[/bold] <script>literal</script>"
    value["nodes"][0]["title"] = label
    manifest.write_text(json.dumps(value))
    app = app_for(workspace, tmp_path)

    async def flow():
        async with app.run_test(size=(120, 48)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+t")
            await fill(app, pilot, workspace)
            await activate(app.screen, pilot, "prepare")
            await idle(app, pilot)
            assert not app.screen.query_one("#tree-confirm", Button).disabled
            tree = app.screen.query_one("#tree-browser", Tree)
            parent = tree.root.children[0]
            assert label in parent.label.plain
            assert not parent.label.spans
            child = parent.children[0]
            assert child.data == "child"
            tree.select_node(child)
            await pilot.pause(0.1)
            assert app.screen.value("anchor") == "child"
            assert app.screen.query_one("#tree-confirm", Button).disabled
            assert workspace[3] == []

    asyncio.run(flow())
