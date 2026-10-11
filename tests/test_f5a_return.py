"""Explicit parent/current-profile return-new; actual private B3 roundtrip."""

import hashlib
import sqlite3
from dataclasses import replace

import pytest
from test_f5a_runtime import BODY, fixture

from session_migrate import continuity, handoff_operations, operations
from session_migrate.continuity import (
    ContinuityAssessmentV2,
    ContinuitySelector,
    ContinuityStatus,
    assess_continuity,
)
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError
from session_migrate.handoff import accept_user_text, prepare_handoff
from session_migrate.handoff_operations import (
    HandoffOperationError,
    ReturnNewRequest,
    _verified_parent,
    prepare_return_new,
)
from session_migrate.handoff_types import HandoffLimits
from session_migrate.lineage import (
    MAX_RECEIPT_BYTES,
    CompletionReceipt,
    LineageStore,
    OperationState,
    SelectionRangeV1,
    _decode,
    _encode,
)
from session_migrate.model import TargetFormat
from session_migrate.operations import OperationKind, commit_operation, prepare_operation
from session_migrate.profiles import SessionRef, prepare_profile_operation
from session_migrate.reconciliation import (
    ReconciliationError,
    RecoveryContext,
    commit_reconciliation,
    prepare_reconciliation,
)


def return_fixture(tmp_path, mode):
    catalog, _, a_profile, b_profile, a_ref, _, reviewed_unused, options, env = fixture(tmp_path)
    reviewed_unused.discard()
    first = commit_operation(
        prepare_profile_operation(catalog, a_profile, a_ref, b_profile, options, base_environ=env)
    )
    catalog.add_root(b_profile.format, b_profile.root)
    catalog.refresh(include_auto=False)
    observer = EndpointObserver(catalog, (a_profile, b_profile), base_environ=env)
    b_ref = SessionRef(
        b_profile.format,
        b_profile.root,
        first.lineage_receipt.target_identity.namespace.native_id,
        b_profile.id,
        profile_fingerprint=b_profile.fingerprint,
    )
    reviewed = None
    if mode == "summary":
        draft = prepare_handoff(
            b_ref,
            (SelectionRangeV1(0, 1, None),),
            HandoffLimits(),
            observer,
            client_version="0.144.4",
        )
        reviewed = accept_user_text(draft, BODY, confirmed_sha256=hashlib.sha256(BODY).hexdigest())
    return_options = replace(options, target_cli_version="2.1.209")
    state_home = tmp_path / ".local/state/session-migrate"
    request = ReturnNewRequest(
        b_ref, first.lineage_receipt.operation_id, a_profile, mode, return_options, reviewed
    )
    return catalog, observer, request, state_home, env, first, a_ref, b_ref


@pytest.mark.parametrize("mode", ["full", "summary"])
def test_actual_a_to_b_to_a2_has_parent_link_and_new_identity(tmp_path, mode):
    catalog, observer, request, state_home, env, first, a_ref, b_ref = return_fixture(
        tmp_path, mode
    )
    second_prepared = prepare_return_new(request, observer, state_home, base_environ=env)
    second = commit_operation(second_prepared)
    assert second.lineage_receipt.target_identity.namespace.native_id not in {
        a_ref.native_id,
        b_ref.native_id,
    }
    assert (
        second.lineage_receipt.return_link.parent_operation_id == first.lineage_receipt.operation_id
    )
    view = LineageStore.inspect_existing(state_home / "lineage-v1")
    op = next(
        op
        for op in view.operations
        if op.record.operation_id == second.lineage_receipt.operation_id
    )
    assert op.record.source.identity == first.lineage_receipt.target_identity
    assert op.record.return_link == second.lineage_receipt.return_link
    catalog.refresh(include_auto=False)
    if mode == "summary":
        assert second.lineage_receipt.continuity_reason == "derived_handoff"
        result = assess_continuity(
            ContinuitySelector(operation_id=op.record.operation_id),
            observer,
            state_home / "lineage-v1",
        )
        assert type(result) is ContinuityAssessmentV2
        assert result.baseline.status is ContinuityStatus.UNTRACKED
        assert result.baseline.reason == "derived_handoff_not_full_history"
        assert not result.update_available and result.baseline.source_comparison is None
    catalog.close()


def _change_actual_parent_receipt(store_path, operation_id):
    ledger = store_path / "ledger.sqlite3"
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(ledger.read_bytes())
        payload = connection.execute(
            "SELECT record_json FROM receipts WHERE operation_id=?", (operation_id,)
        ).fetchone()[0]
        receipt = _decode(CompletionReceipt, payload, MAX_RECEIPT_BYTES)
        references = tuple(
            replace(ref, sha256="0" * 64) if ref.sha256 is not None else ref
            for ref in receipt.references
        )
        changed = replace(receipt, references=references)
        connection.execute(
            "UPDATE receipts SET record_json=? WHERE operation_id=?",
            (_encode(changed), operation_id),
        )
        connection.commit()
        ledger.write_bytes(connection.serialize())
    finally:
        connection.close()


@pytest.mark.parametrize("mode", ["full", "summary"])
@pytest.mark.parametrize("when", ["before_rename", "after_rename"])
def test_actual_parent_rechecked_on_held_coordinator_without_reflock(
    tmp_path, monkeypatch, mode, when
):
    catalog, observer, request, state_home, env, first, _, _ = return_fixture(tmp_path, mode)
    prepared = prepare_return_new(request, observer, state_home, base_environ=env)
    store_path = state_home / "lineage-v1"
    original = LineageStore.complete_reconciled
    observed = []

    def complete(store, *args, **kwargs):
        def fault(event):
            if event == when and not observed:
                _change_actual_parent_receipt(store_path, first.lineage_receipt.operation_id)
                observed.append(event)

        store._fault_hook = fault
        try:
            return original(store, *args, **kwargs)
        finally:
            store._fault_hook = None

    monkeypatch.setattr(LineageStore, "complete_reconciled", complete)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert observed == [when]
    view, parent = _verified_parent(store_path, first.lineage_receipt.operation_id)
    assert parent.receipt != first.lineage_receipt
    op = next(op for op in view.operations if op.record.operation_id == caught.value.operation_id)
    assert op.record.return_link.parent_operation_id == first.lineage_receipt.operation_id
    assert (
        op.record.return_link.parent_receipt_sha256
        == hashlib.sha256(_encode(first.lineage_receipt)).hexdigest()
    )
    if when == "after_rename":
        assert caught.value.code == "persistence_uncertain"
        assert (
            op.durable_state is OperationState.COMPLETED
            and not caught.value.receipt.ledger_recorded
        )
    else:
        assert op.durable_state is OperationState.NEEDS_RECONCILE
    catalog.close()


def test_return_parent_change_before_ready_does_not_publish_new_identity(tmp_path):
    catalog, observer, request, state_home, env, first, _, _ = return_fixture(tmp_path, "full")
    prepared = prepare_return_new(request, observer, state_home, base_environ=env)
    _change_actual_parent_receipt(state_home / "lineage-v1", first.lineage_receipt.operation_id)
    with pytest.raises(SessionMigrateError):
        commit_operation(prepared)
    view = LineageStore.inspect_existing(state_home / "lineage-v1")
    assert len(view.operations) == 1
    catalog.close()


def test_return_invalid_request_is_explicit(tmp_path):
    with pytest.raises(HandoffOperationError) as caught:
        prepare_return_new(object(), object(), tmp_path)
    assert caught.value.code == "invalid_request"


def test_actual_unqualified_import_parent_never_infers_old_a_identity(tmp_path):
    catalog, _, a, b, _, path, unused, options, env = fixture(tmp_path)
    unused.discard()
    imported = commit_operation(
        prepare_operation(
            replace(
                options,
                command=OperationKind.IMPORT,
                path=path,
                source_format=a.format,
                target_format=TargetFormat(b.format.value),
                home=b.root,
            ),
            environ=env,
        )
    )
    catalog.add_root(b.format, b.root)
    catalog.refresh(include_auto=False)
    observer = EndpointObserver(catalog, (a, b), base_environ=env)
    ref = SessionRef(
        b.format,
        b.root,
        imported.lineage_receipt.target_identity.namespace.native_id,
        b.id,
        profile_fingerprint=b.fingerprint,
    )
    state_home = tmp_path / ".local/state/session-migrate"
    assert (
        LineageStore.inspect_existing(state_home / "lineage-v1")
        .operations[0]
        .record.source.identity
        is None
    )
    with pytest.raises(HandoffOperationError) as caught:
        prepare_return_new(
            ReturnNewRequest(
                ref,
                imported.lineage_receipt.operation_id,
                a,
                "full",
                replace(options, target_cli_version="2.1.209"),
            ),
            observer,
            state_home,
            base_environ=env,
        )
    assert caught.value.code == "return_parent_unqualified"
    assert len(LineageStore.inspect_existing(state_home / "lineage-v1").operations) == 1
    catalog.close()


def test_derived_c1_never_invokes_full_baseline_or_delta(tmp_path, monkeypatch):
    catalog, observer, request, state_home, env, _, _, _ = return_fixture(tmp_path, "summary")
    prepared = prepare_return_new(request, observer, state_home, base_environ=env)
    state = prepared._state
    receipt = commit_operation(prepared)
    catalog.refresh(include_auto=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Full baseline must not become summary authority")

    monkeypatch.setattr(continuity, "_baseline_reason", forbidden)
    selector = ContinuitySelector(operation_id=receipt.lineage_receipt.operation_id)
    result = assess_continuity(selector, observer, state_home / "lineage-v1")
    assert type(result) is ContinuityAssessmentV2
    assert result.baseline.reason == "derived_handoff_not_full_history"
    state.built.output.write_bytes(state.built.output.read_bytes().replace(b"unknown", b"changed"))
    catalog.refresh(include_auto=False)
    changed = assess_continuity(selector, observer, state_home / "lineage-v1")
    assert type(changed) is ContinuityAssessmentV2
    assert changed.baseline.reason == "derived_target_changed"
    assert not changed.update_available and changed.baseline.target_comparison is None
    catalog.close()


def test_full_return_recovery_binds_optional_link_and_preserves_native_publication(
    tmp_path, monkeypatch
):
    catalog, observer, request, state_home, env, _, _, _ = return_fixture(tmp_path, "full")
    prepared = prepare_return_new(request, observer, state_home, base_environ=env)
    state = prepared._state
    original = operations._completion_record

    def interrupted(*args):
        raise SessionMigrateError("synthetic interruption after native acknowledgement")

    monkeypatch.setattr(operations, "_completion_record", interrupted)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    monkeypatch.setattr(operations, "_completion_record", original)
    catalog.refresh(include_auto=False)
    context = RecoveryContext(
        state.built.request,
        state.built.source_snapshot.source_path,
        state.built.output,
        {k: v for k, v in state.source_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        {k: v for k, v in state.target_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        state.built.runtime_context.authority.profile,
        request.current_target_profile,
        state.built.manifest,
    )
    result = prepare_reconciliation(
        caught.value.operation_id,
        store_path=state_home / "lineage-v1",
        context=context,
        observer=observer,
    )
    assert result.status == "ready", result.reason
    before = LineageStore.inspect_existing(state_home / "lineage-v1")
    native_before = state.built.output.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Recovery must not republish native history")

    monkeypatch.setattr(operations, "_install", forbidden)
    receipt = commit_reconciliation(result.handle)
    after = LineageStore.inspect_existing(state_home / "lineage-v1")
    assert before.store_uuid == after.store_uuid and len(after.operations) == 2
    current = next(
        op for op in after.operations if op.record.operation_id == caught.value.operation_id
    )
    previous = next(
        op for op in before.operations if op.record.operation_id == caught.value.operation_id
    )
    assert current.record == previous.record
    assert receipt.operation_id == caught.value.operation_id
    assert receipt.target_identity.namespace.native_id == caught.value.session_id
    assert receipt.return_link == current.record.return_link
    assert current.source_checkpoint.identity == current.record.source.identity
    assert state.built.output.read_bytes() == native_before
    catalog.close()


@pytest.mark.parametrize("mode", ["full", "summary"])
@pytest.mark.parametrize("when", ["before_rename", "after_rename"])
def test_return_recovery_parent_mutation_rechecks_unchanged_native_inputs(
    tmp_path, monkeypatch, mode, when
):
    catalog, observer, request, state_home, env, first, _, _ = return_fixture(tmp_path, mode)
    prepared = prepare_return_new(request, observer, state_home, base_environ=env)
    state = prepared._state
    original_completion = operations._completion_record

    def interrupted(*args):
        raise SessionMigrateError("synthetic observation interruption after ACK")

    monkeypatch.setattr(operations, "_completion_record", interrupted)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    monkeypatch.setattr(operations, "_completion_record", original_completion)
    catalog.refresh(include_auto=False)
    document = None
    if mode == "summary":
        directory = tmp_path / "documents"
        directory.mkdir(mode=0o700)
        document = directory / "accepted.md"
        document.write_bytes(BODY)
        document.chmod(0o600)
    context = RecoveryContext(
        state.built.request,
        state.built.source_snapshot.source_path,
        state.built.output,
        {k: v for k, v in state.source_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        {k: v for k, v in state.target_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        state.built.runtime_context.authority.profile,
        request.current_target_profile,
        state.built.manifest,
        handoff_document=document,
    )
    store_path = state_home / "lineage-v1"
    plan = prepare_reconciliation(
        caught.value.operation_id, store_path=store_path, context=context, observer=observer
    )
    assert plan.status == "ready", plan.reason
    before = LineageStore.inspect_existing(store_path)
    native_before = state.built.output.read_bytes()
    source_before = state.built.source_snapshot.source_path.read_bytes()
    original = LineageStore.complete_reconciled
    changed = []

    def complete(store, *args, **kwargs):
        def fault(event):
            if event == when and not changed:
                _change_actual_parent_receipt(store_path, first.lineage_receipt.operation_id)
                changed.append(event)

        store._fault_hook = fault
        try:
            return original(store, *args, **kwargs)
        finally:
            store._fault_hook = None

    def forbidden(*args, **kwargs):
        raise AssertionError("No native publisher or importer during recovery")

    monkeypatch.setattr(operations, "_install", forbidden)
    monkeypatch.setattr(LineageStore, "complete_reconciled", complete)
    with pytest.raises(ReconciliationError) as failed:
        commit_reconciliation(plan.handle)
    assert changed == [when]
    assert failed.value.operation_id == caught.value.operation_id
    after = LineageStore.inspect_existing(store_path)
    assert after.store_uuid == before.store_uuid and len(after.operations) == 2
    current = next(
        op for op in after.operations if op.record.operation_id == caught.value.operation_id
    )
    previous = next(
        op for op in before.operations if op.record.operation_id == caught.value.operation_id
    )
    assert current.record == previous.record
    assert current.durable_state is (
        OperationState.COMPLETED if when == "after_rename" else OperationState.NEEDS_RECONCILE
    )
    if when == "after_rename":
        assert failed.value.code == "persistence_uncertain"
    assert state.built.output.read_bytes() == native_before
    assert state.built.source_snapshot.source_path.read_bytes() == source_before
    catalog.close()


def test_full_return_unknown_current_b_version_blocks_without_parent_fallback(
    tmp_path, monkeypatch
):
    catalog, observer, request, state_home, env, first, _, _ = return_fixture(tmp_path, "full")
    b_path = next(request.current_source_ref.root.rglob("*.jsonl"))
    b_path.write_bytes(b_path.read_bytes().replace(b"0.144.4", b"0.153.4"))
    catalog.refresh(include_auto=False)
    actual = handoff_operations.observe_endpoint
    selected = []

    def observe(*args, **kwargs):
        selected.append(kwargs["client_version"])
        return actual(*args, **kwargs)

    monkeypatch.setattr(handoff_operations, "observe_endpoint", observe)
    before = (state_home / "lineage-v1/ledger.sqlite3").read_bytes()
    with pytest.raises(HandoffOperationError) as caught:
        prepare_return_new(request, observer, state_home, base_environ=env)
    _, parent = _verified_parent(state_home / "lineage-v1", first.lineage_receipt.operation_id)
    assert parent.record.versions.target_client == "0.144.4"
    assert selected == ["0.153.4"] and caught.value.code == "source_changed"
    assert (state_home / "lineage-v1/ledger.sqlite3").read_bytes() == before
    catalog.close()
