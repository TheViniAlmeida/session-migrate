"""Actual B3/C1 readonly operations flows over private synthetic native files."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

pytest.importorskip("textual", reason="optional continuity TUI extra is not installed")

from textual.widgets import Button, Input, OptionList

from session_migrate import operations
from session_migrate.catalog import Catalog
from session_migrate.continuity import ContinuityStatus
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError
from session_migrate.formats import claude
from session_migrate.lineage import LineageStore, OperationState
from session_migrate.model import AgentFormat
from session_migrate.native_namespaces import ensure_migration_state_home
from session_migrate.operations import CancellationToken, OperationRequest
from session_migrate.profiles import Profile, ProfileStore, list_profile_sessions
from session_migrate.tui.app import MigrationApp
from session_migrate.tui.continuity_screen import _STATUS_LABELS, ContinuityScreen, detail_text
from session_migrate.tui.controller import ContinuityReadError, Controller
from session_migrate.tui.widgets import Literal

SOURCE_ID = "40000000-0000-4000-8000-000000000001"
BODY = "SYNTHETIC_CONVERSATION_PRIVATE_CONTENT"
STAMP = "2026-01-01T00:00:00Z"


def native_records(identifier, cwd):
    return [
        {
            "type": role,
            "uuid": f"synthetic-{i}",
            "parentUuid": "synthetic-0" if i else None,
            "sessionId": identifier,
            "cwd": str(cwd),
            "version": "2.1.209",
            "timestamp": STAMP,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        for i, (role, text) in enumerate((("user", BODY), ("assistant", "synthetic answer")))
    ]


def write_records(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


def setup_case(tmp_path, count=1, pending=None):
    tmp_path.chmod(0o700)
    roots = (tmp_path / "source[bold]λ[/bold]", tmp_path / "target")
    for root in roots:
        root.mkdir(mode=0o700, parents=True)
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    source_path = (
        roots[0] / "projects" / claude.project_directory_name(project) / f"{SOURCE_ID}.jsonl"
    )
    write_records(source_path, native_records(SOURCE_ID, project))
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)}
    for key in ("HOME", "XDG_STATE_HOME"):
        home = tmp_path / key.lower()
        home.mkdir(mode=0o700)
        env[key] = str(home)
    source = Profile(
        "source-current",
        "[bold]Source λ[/bold]",
        AgentFormat.CLAUDE,
        roots[0],
        environ={"HOME": str(tmp_path / "source-home")},
    )
    target = Profile(
        "target-current",
        "Target",
        AgentFormat.CLAUDE,
        roots[1],
        environ={
            "HOME": str(tmp_path / "target-home"),
            "XDG_STATE_HOME": str(tmp_path / "other-state"),
        },
    )
    catalog_path = tmp_path / "catalog/state.sqlite"
    with Catalog(catalog_path) as catalog:
        for profile in (source, target):
            catalog.add_root(profile.format, profile.root)
        catalog.refresh(include_auto=False)
        ref = list_profile_sessions(catalog, source)[0].ref
    controller = Controller(catalog_path, environ=env)
    receipts = []
    drafts = []
    for _ in range(count):
        draft = controller.prepare(
            source,
            ref,
            target,
            OperationRequest("transfer", cwd=project, target_cli_version="2.1.209"),
            CancellationToken(),
        )
        drafts.append(draft)
        if pending == "ready":
            original = LineageStore.ready

            def interrupt(store, *args, _original=original):
                _original(store, *args)
                raise KeyboardInterrupt("synthetic process interruption")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(LineageStore, "ready", interrupt)
                with pytest.raises(KeyboardInterrupt):
                    controller.commit(draft, CancellationToken(), lambda event: None)
        elif pending == "reconcile":
            with pytest.MonkeyPatch.context() as patch:

                def interrupt(*args):
                    raise SessionMigrateError("synthetic finalization interruption")

                patch.setattr(operations, "_completion_record", interrupt)
                with pytest.raises(OperationNeedsReconcileError):
                    controller.commit(draft, CancellationToken(), lambda event: None)
        else:
            receipts.append(controller.commit(draft, CancellationToken(), lambda event: None))
    with Catalog(catalog_path) as catalog:
        catalog.refresh(include_auto=False)
    profiles_file = tmp_path / "profiles/current.json"
    ProfileStore(profiles_file).create([source, target])
    return controller, source, target, ref, source_path, profiles_file, receipts, drafts


def snapshot(root):
    return {
        str(path.relative_to(root)): (path.is_dir(), path.read_bytes() if path.is_file() else None)
        for path in root.rglob("*")
    }


def forbid_writes(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("readonly operations invoked a mutation")

    monkeypatch.setattr(Catalog, "__init__", denied)
    for name in (
        "create_new",
        "ready",
        "begin_publication",
        "mark_verifying",
        "complete",
        "complete_reconciled",
        "cancel_ready",
    ):
        monkeypatch.setattr(LineageStore, name, denied)
    for name in ("_install", "run_operation", "prepare_operation", "commit_operation"):
        monkeypatch.setattr(operations, name, denied)


async def idle(app, pilot):
    for _ in range(200):
        await pilot.pause(0.015)
        screen = app.screen
        if isinstance(screen, ContinuityScreen):
            if screen.pending is None:
                return
        elif app.pending is None and app.preview_request is None:
            return
    raise AssertionError("readonly UI task did not finish")


async def activate(app, pilot, identifier):
    app.screen.query_one(f"#{identifier}", Button).focus()
    await pilot.press("enter")
    await idle(app, pilot)


def test_readonly_controller_actual_receipt_profiles_and_base_state_home(tmp_path, monkeypatch):
    controller, source, target, _, _, profiles_file, receipts, _ = setup_case(tmp_path)
    operation_id = receipts[0].lineage_receipt.operation_id
    assert receipts[0].lineage_receipt.continuity_reason == "tracked"
    assert (
        controller.continuity_store_path
        == default_migration_state_home(environ=controller.environ) / "lineage-v1"
    )
    assert (
        controller.continuity_store_path
        != default_migration_state_home(environ=target.environment(controller.environ))
        / "lineage-v1"
    )
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)
    page = controller.list_operations()
    assert len(page.operations) == 1 and page.operations[0].record.operation_id == operation_id
    detail = controller.assess_operation(operation_id)
    assert detail.profile_context == "completed_transfer"
    assert detail.assessment.status is ContinuityStatus.NO_OP
    assert detail.assessment.source_current_profile_fingerprint == source.fingerprint
    assert detail.assessment.target_current_profile_fingerprint == target.fingerprint
    assert not detail.update_available and not detail.assessment.update_available
    explicit = controller.assess_operation(operation_id, profiles_file=profiles_file)
    assert explicit.assessment.status is ContinuityStatus.NO_OP
    assert BODY not in detail_text(detail) and BODY not in repr(page) and BODY not in repr(detail)
    assert snapshot(tmp_path) == before


def test_keyboard_operations_inspect_assess_back_and_literal_metadata(tmp_path, monkeypatch):
    controller, _, _, _, _, profiles_file, receipts, _ = setup_case(tmp_path)
    operation_id = receipts[0].lineage_receipt.operation_id
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)

    async def flow():
        app = MigrationApp(controller, profiles_file=profiles_file)
        async with app.run_test(size=(120, 45)) as pilot:
            await idle(app, pilot)
            await activate(app, pilot, "operations")
            assert isinstance(app.screen, ContinuityScreen)
            screen = app.screen
            assert screen.page.total_count == 1
            screen.query_one("#operations-list", OptionList).highlighted = 0
            await pilot.pause()
            assert screen.query_one("#operation-id", Input).value == operation_id
            await activate(app, pilot, "operations-inspect")
            rendered = str(screen.query_one("#operation-detail", Literal).render())
            assert "completed" in rendered and operation_id in rendered
            assert "source[bold]λ[/bold]" in rendered and BODY not in rendered
            await activate(app, pilot, "operations-assess")
            assert screen.detail.assessment.status is ContinuityStatus.NO_OP
            rendered = str(screen.query_one("#operation-detail", Literal).render())
            assert "No change (no_op)" in rendered and "Update available: false" in rendered
            assert "file_capture_read" in rendered
            assert "cache/WAL" in str(
                screen.query_one("#operations-read-effects", Literal).render()
            )
            assert screen.query_one("#operation-detail", Literal).render().spans == []
            await pilot.resize_terminal(78, 23)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ContinuityScreen) and app.stage == "source"

    asyncio.run(flow())
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "change,status",
    [
        ("target_advanced", ContinuityStatus.CONFLICT),
        ("source_advanced", ContinuityStatus.UNTRACKED),
        ("source_changed", ContinuityStatus.LINEAGE_BROKEN),
        ("profile_changed", ContinuityStatus.UNTRACKED),
    ],
)
def test_actual_c1_statuses_keep_updates_unavailable(tmp_path, monkeypatch, change, status):
    controller, source, target, _, source_path, profiles_file, receipts, _ = setup_case(tmp_path)
    operation_id = receipts[0].lineage_receipt.operation_id
    native_path = (
        source_path
        if change.startswith("source")
        else next(target.root.glob("projects/**/*.jsonl"))
    )
    if change == "profile_changed":
        changed = replace(source, name="Changed current profile")
        profiles_file = tmp_path / "profiles/changed.json"
        ProfileStore(profiles_file).create([changed, target])
    else:
        rows = [json.loads(row) for row in native_path.read_bytes().splitlines()]
        if change == "source_changed":
            rows[0]["message"]["content"][0]["text"] = "synthetic source changed"
        else:
            addition = dict(rows[0])
            addition["uuid"] = "synthetic-new-unit"
            addition["parentUuid"] = rows[-1]["uuid"]
            addition["message"] = {
                "role": "user",
                "content": [{"type": "text", "text": "new unit"}],
            }
            rows.append(addition)
        write_records(native_path, rows)
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)
    detail = controller.assess_operation(operation_id, profiles_file=profiles_file)
    assert detail.assessment.status is status, detail.assessment.reason
    assert not detail.update_available and "Update available: false" in detail_text(detail)
    if change == "source_advanced":
        assert detail.assessment.reason == "native_boundary_unproven"
        assert "delta_candidate" not in detail_text(detail)
    if change == "profile_changed":
        assert detail.assessment.reason == "identity_unproven"
        assert "Current profiles" in detail_text(detail)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("pending", ["ready", "reconcile"])
def test_recovered_pending_operations_are_readonly_needs_reconcile(tmp_path, monkeypatch, pending):
    controller, _, _, _, _, profiles_file, _, drafts = setup_case(tmp_path, pending=pending)
    operation_id = drafts[0].proposal.proposal_id
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)
    detail = controller.assess_operation(operation_id, profiles_file=profiles_file)
    assert detail.assessment.status is ContinuityStatus.NEEDS_RECONCILE
    assert detail.inspection.operation.durable_state is (
        OperationState.READY if pending == "ready" else OperationState.NEEDS_RECONCILE
    )
    assert not detail.update_available and snapshot(tmp_path) == before


def test_missing_current_profiles_are_explicit_and_not_fabricated(tmp_path, monkeypatch):
    controller, _, _, _, _, _, receipts, _ = setup_case(tmp_path)
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)
    detail = controller.assess_operation(
        receipts[0].lineage_receipt.operation_id, current_profiles=()
    )
    assert detail.assessment is None and detail.context_reason == "profile_context_required"
    assert "Choose current profiles" in detail_text(detail)
    assert "Continuity: unavailable" in detail_text(detail)
    assert "Continuity: Untracked" not in detail_text(detail)
    assert snapshot(tmp_path) == before


def test_missing_ledger_and_missing_home_do_not_create_state(tmp_path, monkeypatch):
    controller = Controller(tmp_path / "missing-catalog.sqlite", environ={"HOME": str(tmp_path)})
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)
    assert controller.list_operations().reason == "ledger_missing"
    assert controller.inspect_operation("missing-operation").inspection.reason == "ledger_missing"
    assert snapshot(tmp_path) == before
    unrelated = Controller(tmp_path / "catalog.sqlite", environ={})
    with pytest.raises(ContinuityReadError, match="state_home_unavailable"):
        unrelated.list_operations()


def test_bounded_page_uses_explicit_operations_and_no_bodies(tmp_path, monkeypatch):
    controller, _, _, _, _, _, _, _ = setup_case(tmp_path, count=3)
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)
    first, second = (
        controller.list_operations(limit=2),
        controller.list_operations(offset=2, limit=2),
    )
    assert first.total_count == second.total_count == 3 and first.has_next and not second.has_next
    assert len(first.operations) == 2 and len(second.operations) == 1
    ids = [operation.record.operation_id for operation in first.operations + second.operations]
    assert len(set(ids)) == 3
    assert BODY not in repr(first) and snapshot(tmp_path) == before
    with pytest.raises(ContinuityReadError, match="invalid_page"):
        controller.list_operations(limit=51)


def test_read_error_never_renders_external_exception_body(tmp_path, monkeypatch):
    controller, _, _, _, _, profiles_file, _, _ = setup_case(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("[bold]" + BODY + "[/bold]\x1b]52;c;unsafe\x07")

    monkeypatch.setattr(controller, "list_operations", fail)

    async def flow():
        app = MigrationApp(controller, profiles_file=profiles_file)
        async with app.run_test(size=(110, 40)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+o")
            await idle(app, pilot)
            rendered = str(app.screen.query_one("#operation-detail", Literal).render())
            assert (
                "Read unavailable" in rendered and BODY not in rendered and "\x1b" not in rendered
            )

    asyncio.run(flow())


def test_reader_cancel_and_selectors_preserve_actual_transfer_draft(tmp_path, monkeypatch):
    controller, source, target, ref, _, profiles_file, receipts, _ = setup_case(tmp_path)
    draft = controller.prepare(
        source,
        ref,
        target,
        OperationRequest("transfer", cwd=tmp_path / "project", target_cli_version="2.1.209"),
        CancellationToken(),
    )
    entered, release = threading.Event(), threading.Event()
    original = controller.assess_operation
    loop_thread = threading.get_ident()

    def slow(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        release.wait(4)
        return original(*args, **kwargs)

    monkeypatch.setattr(controller, "assess_operation", slow)
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)

    async def flow():
        app = MigrationApp(controller, profiles_file=profiles_file)
        async with app.run_test(size=(120, 45)) as pilot:
            await idle(app, pilot)
            app.draft = draft
            app.show("review")
            app.query_one("#confirm", Button).disabled = False
            await pilot.press("ctrl+o")
            await idle(app, pilot)
            screen = app.screen
            screen.query_one("#operation-id", Input).value = receipts[
                0
            ].lineage_receipt.operation_id
            screen.query_one("#operations-assess", Button).focus()
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.01)
                if entered.is_set():
                    break
            assert entered.is_set() and screen.pending == "assess"
            assert app.draft is draft and controller._active_prepared is draft.prepared
            await activate(app, pilot, "operations-cancel")
            assert screen.pending is None and "Read cancelled" in str(
                screen.query_one("#operations-status", Literal).render()
            )
            release.set()
            await pilot.pause(0.15)
            assert screen.detail is None
            await pilot.press("escape")
            await pilot.pause()
            assert app.stage == "review" and app.draft is draft
            assert controller._active_prepared is draft.prepared

    try:
        asyncio.run(flow())
    finally:
        release.set()
        controller.discard(draft)
    assert snapshot(tmp_path) == before


def test_restart_requires_actual_saved_profiles_and_never_reconstructs_history(
    tmp_path, monkeypatch
):
    controller, _, _, _, _, profiles_file, receipts, _ = setup_case(tmp_path)
    fresh = Controller(controller.catalog, environ=controller.environ)
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)
    operation_id = receipts[0].lineage_receipt.operation_id
    inferred = fresh.assess_operation(operation_id)
    assert inferred.assessment.status is ContinuityStatus.UNTRACKED
    assert inferred.assessment.reason == "identity_unproven"
    current = fresh.assess_operation(operation_id, profiles_file=profiles_file)
    assert current.assessment.status is ContinuityStatus.NO_OP
    assert current.profile_context == "saved_profiles"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("empty", [False, True])
def test_keyboard_missing_or_empty_ledger_preserves_directory_tree(tmp_path, monkeypatch, empty):
    controller, _, _, _, _, profiles_file, _, _ = setup_case(tmp_path, count=0)
    if empty:
        ensure_migration_state_home(controller.continuity_store_path.parent)
        with LineageStore.create_new(controller.continuity_store_path):
            pass
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)

    async def flow():
        app = MigrationApp(controller, profiles_file=profiles_file)
        async with app.run_test(size=(105, 36)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+o")
            await idle(app, pilot)
            screen = app.screen
            assert screen.page.total_count == 0
            assert screen.page.reason == ("empty" if empty else "ledger_missing")
            assert screen.query_one("#operations-next", Button).disabled
            assert "No durable operations" in str(
                screen.query_one("#operations-status", Literal).render()
            )
            screen.query_one("#operation-id", Input).value = "missing-operation"
            await activate(app, pilot, "operations-inspect")
            assert screen.detail.inspection.operation is None
            await pilot.press("escape")
            await pilot.pause()
            assert app.stage == "source"

    asyncio.run(flow())
    assert snapshot(tmp_path) == before


def test_selector_change_and_refresh_discard_old_assessment_without_stacking_readers(
    tmp_path, monkeypatch
):
    controller, _, _, _, _, profiles_file, receipts, _ = setup_case(tmp_path)
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)

    async def flow():
        app = MigrationApp(controller, profiles_file=profiles_file)
        async with app.run_test(size=(120, 45)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+o")
            await idle(app, pilot)
            screen = app.screen
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert app.screen is screen and len(app.screen_stack) == 2
            screen.query_one("#operation-id", Input).value = receipts[
                0
            ].lineage_receipt.operation_id
            await activate(app, pilot, "operations-assess")
            assert screen.detail.assessment.status is ContinuityStatus.NO_OP
            screen.query_one("#operation-id", Input).value = "different-operation"
            await pilot.pause()
            assert screen.detail is None
            assert "Selection changed" in str(
                screen.query_one("#operation-detail", Literal).render()
            )
            await activate(app, pilot, "operations-refresh")
            assert screen.detail is None

    asyncio.run(flow())
    assert snapshot(tmp_path) == before


def test_keyboard_real_operation_pagination_is_bounded_and_readonly(tmp_path, monkeypatch):
    controller, _, _, _, _, profiles_file, _, _ = setup_case(tmp_path, count=51)
    before = snapshot(tmp_path)
    forbid_writes(monkeypatch)

    async def flow():
        app = MigrationApp(controller, profiles_file=profiles_file)
        async with app.run_test(size=(115, 42)) as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+o")
            await idle(app, pilot)
            screen = app.screen
            assert screen.page.total_count == 51 and len(screen.page.operations) == 50
            assert screen.query_one("#operations-list", OptionList).option_count == 50
            await activate(app, pilot, "operations-next")
            assert screen.page_offset == 50 and len(screen.page.operations) == 1
            assert screen.query_one("#operations-next", Button).disabled
            await activate(app, pilot, "operations-previous")
            assert screen.page_offset == 0 and len(screen.page.operations) == 50

    asyncio.run(flow())
    assert snapshot(tmp_path) == before


def test_all_c1_statuses_have_literal_labels_without_inventing_delta_authority():
    assert set(_STATUS_LABELS) == {status.value for status in ContinuityStatus}
