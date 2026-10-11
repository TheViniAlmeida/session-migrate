"""Headless offline review/return-new with actual private handles; no native clients."""

import asyncio
import hashlib
import threading
from pathlib import Path

import pytest

pytest.importorskip("textual")

from test_f5a_cli import setup_cli
from test_f5a_runtime import BODY
from textual.widgets import Button, Input, Select, TextArea

from session_migrate import handoff_cli
from session_migrate.lineage import LineageStore
from session_migrate.operations import CancellationToken
from session_migrate.tui.app import MigrationApp
from session_migrate.tui.controller import Controller
from session_migrate.tui.handoff_screen import HandoffScreen


async def idle(screen, pilot):
    for _ in range(400):
        await pilot.pause(0.015)
        if screen.future is None:
            return
    raise AssertionError("bounded handoff task did not finish")


async def open_handoff(app, pilot):
    # Pilot's idle message queue does not drain the app's profile-reader result.
    for _ in range(400):
        await pilot.pause(0.015)
        if app.pending is None and app.preview_request is None and app.screen.is_mounted:
            break
    else:
        raise AssertionError("bounded main task did not finish before handoff")
    await pilot.press("ctrl+h")
    for _ in range(400):
        if isinstance(app.screen, HandoffScreen) and app.screen.is_mounted:
            return app.screen
        await pilot.pause(0.015)
    raise AssertionError("keyboard handoff screen did not mount")


def configure(screen, tmp_path, profile_file, source_id, *, parent=None):
    values = dict(
        profiles=str(profile_file),
        source="target" if parent else "source",
        **{
            "native-id": source_id,
            "ranges": "0:1",
            "source-version": "0.144.4" if parent else "2.1.209",
            "target": "source" if parent else "target",
            "cwd": str(tmp_path / "project"),
            "target-version": "2.1.209" if parent else "0.144.4",
            "provider": "openai",
            "model": "synthetic-model",
        },
    )
    if parent:
        values["parent"] = parent
    for key, value in values.items():
        screen.query_one(f"#handoff-{key}", Input).value = value


async def press(screen, pilot, identifier):
    screen.query_one(f"#handoff-{identifier}", Button).focus()
    await pilot.press("enter")
    await idle(screen, pilot)


def controller_fixture(tmp_path, monkeypatch, *, returning=False):
    prefix, flags, _, state, document, parent = setup_cli(
        tmp_path, monkeypatch, returning=returning
    )
    import os

    controller = Controller(
        Path(prefix[1]),
        environ={"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": os.environ["PATH"]},
    )
    profiles = Path(flags[1])
    return controller, profiles, flags[5], state, document, parent


@pytest.mark.parametrize("returning", [False, True])
def test_keyboard_handoff_waits_for_actual_initial_profile_read(tmp_path, monkeypatch, returning):
    controller, profiles, _, _, _, _ = controller_fixture(
        tmp_path, monkeypatch, returning=returning
    )
    entered, release = threading.Event(), threading.Event()
    actual_profiles, actual_status = controller.profiles, MigrationApp.status
    notices = []

    def held_profiles(storage=None):
        result = actual_profiles(storage)
        entered.set()
        assert release.wait(10)
        return result

    def status(app, value):
        notices.append(value)
        return actual_status(app, value)

    monkeypatch.setattr(controller, "profiles", held_profiles)
    monkeypatch.setattr(MigrationApp, "status", status)

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(110, 45)) as pilot:
            for _ in range(400):
                await pilot.pause(0.015)
                if entered.is_set():
                    break
            assert entered.is_set() and app.screen.is_mounted
            assert app.pending == "profiles"
            await pilot.press("ctrl+h")
            assert not isinstance(app.screen, HandoffScreen)
            assert app.pending == "profiles"
            assert "Wait for the current task before opening handoff review." in notices
            release.set()
            screen = await open_handoff(app, pilot)
            assert app.pending is None and app.preview_request is None
            assert screen.query_one("#handoff-profiles", Input).value == str(profiles)

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("returning", [False, True])
def test_actual_summary_and_return_ui_review_confirm_exact_private_handles(
    tmp_path, monkeypatch, returning
):
    controller, profiles, source_id, state, _, parent = controller_fixture(
        tmp_path, monkeypatch, returning=returning
    )

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(110, 45)) as pilot:
            screen = await open_handoff(app, pilot)
            assert isinstance(screen, HandoffScreen)
            configure(screen, tmp_path, profiles, source_id, parent=parent)
            await idle(screen, pilot)
            await press(screen, pilot, "capture")
            assert "Objective: unknown" in screen.query_one("#handoff-document", TextArea).text
            screen.query_one("#handoff-document", TextArea).load_text(BODY.decode())
            screen.query_one("#handoff-digest", Input).value = hashlib.sha256(BODY).hexdigest()
            await pilot.pause()
            await press(screen, pilot, "accept-text")
            await press(screen, pilot, "prepare")
            assert not screen.query_one("#handoff-confirm", Button).disabled
            proposal = screen.workflow.proposal
            await pilot.resize_terminal(90, 32)
            assert screen.workflow.proposal is proposal
            await press(screen, pilot, "confirm")
            assert screen.finished and not screen.uncertain
            view = LineageStore.inspect_existing(state / "lineage-v1")
            op = next(op for op in view.operations if op.record.transformation)
            assert op.receipt.derivation.input.document_sha256 == hashlib.sha256(BODY).hexdigest()
            if returning:
                assert op.record.return_link.parent_operation_id == parent
            assert app.draft is None and app.pending is None

    asyncio.run(run())


def test_same_process_export_edit_sha_ui_preserves_entire_document(tmp_path, monkeypatch):
    controller, profiles, source_id, state, document, _ = controller_fixture(tmp_path, monkeypatch)
    body = "a" * 12000 + "\nLast byte remains part of the USER document."

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(100, 40)) as pilot:
            screen = await open_handoff(app, pilot)
            configure(screen, tmp_path, profiles, source_id)
            await idle(screen, pilot)
            await press(screen, pilot, "capture")
            screen.query_one("#handoff-export-path", Input).value = str(document)
            await press(screen, pilot, "export")
            document.write_text(body)
            screen.query_one("#handoff-digest", Input).value = hashlib.sha256(
                body.encode()
            ).hexdigest()
            await press(screen, pilot, "accept-export")
            await press(screen, pilot, "prepare")
            assert not screen.query_one("#handoff-confirm", Button).disabled
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HandoffScreen)
            assert document.read_text() == body
            assert not state.exists() and not (tmp_path / "target").exists()

    asyncio.run(run())


def test_postpublication_back_quit_input_keep_same_receipt_and_no_kill(tmp_path, monkeypatch):
    controller, profiles, source_id, state, _, _ = controller_fixture(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    actual_commit = handoff_cli.commit_operation

    def held_commit(prepared, *, cancellation=None):
        # Hold after the actual commit has published and closed its token.
        result = actual_commit(prepared, cancellation=cancellation)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(handoff_cli, "commit_operation", held_commit)

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(100, 40)) as pilot:
            screen = await open_handoff(app, pilot)
            configure(screen, tmp_path, profiles, source_id)
            await idle(screen, pilot)
            await press(screen, pilot, "capture")
            screen.query_one("#handoff-document", TextArea).load_text(BODY.decode())
            screen.query_one("#handoff-digest", Input).value = hashlib.sha256(BODY).hexdigest()
            await pilot.pause()
            await press(screen, pilot, "accept-text")
            await press(screen, pilot, "prepare")
            screen.query_one("#handoff-confirm", Button).focus()
            await pilot.press("enter")
            for _ in range(200):
                await pilot.pause(0.015)
                if entered.is_set():
                    break
            assert entered.is_set()
            generation = screen.generation
            await pilot.press("escape", "ctrl+q")
            screen.query_one("#handoff-model", Input).value = "changed-visible-form"
            await pilot.pause()
            assert app.screen is screen and screen.generation == generation
            assert screen.pending == "commit"
            release.set()
            await idle(screen, pilot)
            assert screen.finished
            assert len(LineageStore.inspect_existing(state / "lineage-v1").operations) == 1

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_handoff_screen_does_not_invalidate_main_prepared_draft(tmp_path, monkeypatch):
    controller, profiles, source_id, _, _, _ = controller_fixture(tmp_path, monkeypatch)
    # A real original full prepared transfer, not an invented public proposal.
    from session_migrate.operations import OperationRequest
    from session_migrate.profiles import ProfileStore, SessionRef

    source, target = ProfileStore(profiles).load()
    ref = SessionRef(
        source.format, source.root, source_id, source.id, profile_fingerprint=source.fingerprint
    )
    draft = controller.prepare(
        source,
        ref,
        target,
        OperationRequest("transfer", cwd=tmp_path / "project", target_cli_version="0.144.4"),
        CancellationToken(),
    )

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.draft = draft
            screen = await open_handoff(app, pilot)
            configure(screen, tmp_path, profiles, source_id)
            await idle(screen, pilot)
            await press(screen, pilot, "capture")
            await pilot.press("escape")
            assert app.draft is draft
            receipt = controller.commit(draft, CancellationToken(), lambda event: None)
            assert receipt.lineage_receipt is not None

    asyncio.run(run())


def test_full_return_ui_prepares_without_manufactured_handoff(tmp_path, monkeypatch):
    controller, profiles, source_id, state, _, parent = controller_fixture(
        tmp_path, monkeypatch, returning=True
    )

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(100, 40)) as pilot:
            screen = await open_handoff(app, pilot)
            configure(screen, tmp_path, profiles, source_id, parent=parent)
            screen.query_one("#handoff-mode", Select).value = "full"
            await idle(screen, pilot)
            await press(screen, pilot, "prepare")
            assert not screen.query_one("#handoff-confirm", Button).disabled
            assert screen.workflow.draft is None and screen.workflow.reviewed is None
            await press(screen, pilot, "confirm")
            assert screen.finished
            op = next(
                op
                for op in LineageStore.inspect_existing(state / "lineage-v1").operations
                if op.record.return_link
            )
            assert (
                op.record.transformation is None
                and op.record.return_link.parent_operation_id == parent
            )

    asyncio.run(run())


def test_back_during_actual_capture_abandons_result_and_closes_own_driver(tmp_path, monkeypatch):
    controller, profiles, source_id, state, _, _ = controller_fixture(tmp_path, monkeypatch)
    captured, release, closed = threading.Event(), threading.Event(), threading.Event()
    actual_capture, actual_close = (
        handoff_cli.HandoffWorkflow.capture,
        handoff_cli.HandoffWorkflow.close,
    )

    def held_capture(workflow, *args, **kwargs):
        result = actual_capture(workflow, *args, **kwargs)
        captured.set()
        assert release.wait(5)
        return result

    def observe_close(workflow):
        actual_close(workflow)
        closed.set()

    monkeypatch.setattr(handoff_cli.HandoffWorkflow, "capture", held_capture)
    monkeypatch.setattr(handoff_cli.HandoffWorkflow, "close", observe_close)

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(100, 40)) as pilot:
            screen = await open_handoff(app, pilot)
            configure(screen, tmp_path, profiles, source_id)
            await idle(screen, pilot)
            screen.query_one("#handoff-capture", Button).focus()
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.015)
                if captured.is_set():
                    break
            assert captured.is_set()
            await pilot.press("escape")
            assert not isinstance(app.screen, HandoffScreen)
            release.set()
            for _ in range(100):
                await pilot.pause(0.015)
                if closed.is_set():
                    break
            assert closed.is_set() and screen.workflow is None
            assert not state.exists() and not (tmp_path / "target").exists()

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_actual_text_edited_while_accepting_cannot_review_old_document(tmp_path, monkeypatch):
    controller, profiles, source_id, state, _, _ = controller_fixture(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    actual = handoff_cli.HandoffWorkflow.accept

    def held_accept(workflow, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return actual(workflow, *args, **kwargs)

    monkeypatch.setattr(handoff_cli.HandoffWorkflow, "accept", held_accept)

    async def run():
        app = MigrationApp(controller, profiles_file=profiles)
        async with app.run_test(size=(100, 40)) as pilot:
            screen = await open_handoff(app, pilot)
            configure(screen, tmp_path, profiles, source_id)
            await idle(screen, pilot)
            await press(screen, pilot, "capture")
            screen.query_one("#handoff-document", TextArea).load_text(BODY.decode())
            screen.query_one("#handoff-digest", Input).value = hashlib.sha256(BODY).hexdigest()
            await pilot.pause()
            screen.query_one("#handoff-accept-text", Button).focus()
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.01)
                if entered.is_set():
                    break
            assert entered.is_set() and screen.pending == "accept"
            screen.query_one("#handoff-document", TextArea).load_text(
                "Changed visible USER document"
            )
            await pilot.pause()
            release.set()
            await idle(screen, pilot)
            assert screen.workflow is None or screen.workflow.reviewed is None, (
                "old accepted input remained authoritative after visible text changed during accept"
            )
            assert not state.exists() and not (tmp_path / "target").exists()

    try:
        asyncio.run(run())
    finally:
        release.set()
