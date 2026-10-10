"""Durable operations use synthetic transcripts and private disposable roots."""

import json
import os
import threading
import time
from dataclasses import replace

import pytest
from test_operations import FIXTURE, environment, request

from session_migrate import operations
from session_migrate.errors import (
    NativeImportPartialError,
    OperationNeedsReconcileError,
    SessionMigrateError,
)
from session_migrate.lineage import LineageStore, NativeState, OperationState
from session_migrate.model import AgentFormat, EventKind
from session_migrate.operations import (
    CancellationToken,
    ProgressStage,
    commit_operation,
    prepare_operation,
    run_operation,
)


def private_request(tmp_path, **options):
    path = tmp_path / "source.jsonl"
    path.write_bytes(FIXTURE.read_bytes())
    path.chmod(0o600)
    return request(tmp_path, path=path, **options)


def test_legacy_no_title_always_observes_and_has_separate_lineage_receipt(tmp_path, monkeypatch):
    env = environment(tmp_path)
    observed = []
    original = operations._verify_installed_identity

    def inspect(state):
        observed.append(state.built.artifact.session_id)
        return original(state)

    monkeypatch.setattr(operations, "_verify_installed_identity", inspect)
    result = run_operation(private_request(tmp_path), environ=env)
    assert observed == [result.session_id, result.session_id]
    assert result.lineage_receipt.operation_id
    assert "lineage_receipt" not in result.to_dict()
    assert result.lineage_receipt.continuity_reason == "identity_untracked"
    view = LineageStore.inspect_existing(
        operations.default_migration_state_home(environ=env) / "lineage-v1"
    )
    assert view.operations[0].state is OperationState.COMPLETED


@pytest.mark.parametrize("boundary", ["after_temp_fsync", "after_rename", "after_directory_fsync"])
def test_intent_failure_never_calls_native_and_returns_honest_receipt(
    tmp_path, monkeypatch, boundary
):
    env = environment(tmp_path)
    prepared = prepare_operation(private_request(tmp_path), environ=env)
    original = LineageStore.begin_publication
    native_calls = []
    installer = operations._install

    def install(*args):
        if not args[-1]:
            native_calls.append(True)
        return installer(*args)

    def begin(store, operation_id, guard):
        def fail(event):
            if event == boundary:
                raise OSError("SYNTHETIC_INTENT_FAILURE")

        store._fault_hook = fail
        try:
            return original(store, operation_id, guard)
        finally:
            store._fault_hook = None

    monkeypatch.setattr(LineageStore, "begin_publication", begin)
    monkeypatch.setattr(operations, "_install", install)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert native_calls == []
    assert caught.value.native_state is NativeState.NATIVE_NOT_STARTED
    assert caught.value.receipt.ledger_recorded
    assert "SYNTHETIC_INTENT_FAILURE" not in str(caught.value)
    assert not hasattr(caught.value, "import_succeeded")


def test_cancel_during_intent_fsync_is_fast_and_ui_stage_is_neutral(tmp_path, monkeypatch):
    env = environment(tmp_path)
    prepared = prepare_operation(private_request(tmp_path), environ=env)
    token = CancellationToken()
    entered, release = threading.Event(), threading.Event()
    original = LineageStore.begin_publication
    events, errors = [], []

    def begin(store, operation_id, guard):
        def pause(event):
            if event == "after_temp_fsync":
                entered.set()
                assert release.wait(5)

        store._fault_hook = pause
        try:
            return original(store, operation_id, guard)
        finally:
            store._fault_hook = None

    def commit():
        try:
            commit_operation(prepared, cancellation=token, progress=events.append)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(LineageStore, "begin_publication", begin)
    worker = threading.Thread(target=commit)
    worker.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        assert token.request_cancel() is False
        assert time.monotonic() - started < 0.2
        assert events[-1].stage is ProgressStage.RECORDING_INTENT
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []


def test_source_mutation_in_intent_fsync_prevents_native_publication(tmp_path, monkeypatch):
    env = environment(tmp_path)
    operation = private_request(tmp_path)
    prepared = prepare_operation(operation, environ=env)
    original = LineageStore.begin_publication

    def begin(store, operation_id, guard):
        def change(event):
            if event == "after_temp_fsync":
                before = operation.path.stat()
                raw = operation.path.read_bytes().replace(b"hello", b"HELLO")
                if raw == operation.path.read_bytes():
                    raw += b"\n"
                operation.path.write_bytes(raw)
                os.utime(operation.path, ns=(before.st_atime_ns, before.st_mtime_ns))

        store._fault_hook = change
        try:
            return original(store, operation_id, guard)
        finally:
            store._fault_hook = None

    monkeypatch.setattr(LineageStore, "begin_publication", begin)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert caught.value.native_state is NativeState.NATIVE_NOT_STARTED
    assert not operation.home.exists()


def test_private_source_payload_mutation_rejected_even_when_event_equality_matches(tmp_path):
    prepared = prepare_operation(private_request(tmp_path), environ=environment(tmp_path))
    source = prepared._state.built.artifact.source
    event = next(
        (event for event in source.events if event.kind is EventKind.TOOL_CALL), source.events[0]
    )
    changed = replace(event, payload={**event.payload, "synthetic_changed": True})
    assert changed == event
    event.payload["synthetic_changed"] = True
    with pytest.raises(SessionMigrateError, match="prepared options or artifact changed"):
        commit_operation(prepared)


def test_unknown_native_result_is_not_reported_as_import_acknowledged(tmp_path, monkeypatch):
    prepared = prepare_operation(private_request(tmp_path), environ=environment(tmp_path))
    original = operations._install

    def install(*args):
        if not args[-1]:
            raise RuntimeError("PRIVATE_NATIVE_ERROR_SENTINEL")
        return original(*args)

    monkeypatch.setattr(operations, "_install", install)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert caught.value.native_state is NativeState.ATTEMPTED_UNKNOWN
    assert caught.value.receipt.ledger_recorded
    assert "PRIVATE_NATIVE_ERROR_SENTINEL" not in str(caught.value)
    assert not hasattr(caught.value, "import_succeeded")


@pytest.mark.parametrize("side", ["source", "target"])
def test_whole_capture_guard_after_reference_io_rejects_restored_stat_mutation(
    tmp_path, monkeypatch, side
):
    env = environment(tmp_path)
    operation = private_request(tmp_path)
    prepared = prepare_operation(operation, environ=env)
    original = operations._observed_references
    changed = []

    def references(state, session=None):
        result = original(state, session)
        if session is not None and not changed:
            path = operation.path if side == "source" else state.built.output
            before = path.stat()
            raw = path.read_bytes()
            edited = raw.replace(b"synthetic", b"SYNTHETIC", 1)
            assert edited != raw and len(edited) == len(raw)
            path.write_bytes(edited)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            changed.append(True)
        return result

    monkeypatch.setattr(operations, "_observed_references", references)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert changed
    assert caught.value.native_state is NativeState.ACKNOWLEDGED
    assert caught.value.receipt.ledger_recorded
    view = LineageStore.inspect_existing(
        operations.default_migration_state_home(environ=env) / "lineage-v1"
    )
    assert view.operations[0].durable_state is OperationState.NEEDS_RECONCILE


def test_ack_partial_always_attempts_observation_and_never_completes(tmp_path, monkeypatch):
    env = environment(tmp_path)
    prepared = prepare_operation(private_request(tmp_path), environ=env)
    install, observe = operations._install, operations._verify_installed_identity
    calls = []

    def partial(*values):
        result = install(*values)
        if not values[-1]:
            raise NativeImportPartialError(
                "PRIVATE_ERROR_SENTINEL",
                target_format=values[1].target_format,
                session_id=values[1].session_id,
            )
        return result

    def observed(state):
        calls.append(True)
        return observe(state)

    monkeypatch.setattr(operations, "_install", partial)
    monkeypatch.setattr(operations, "_verify_installed_identity", observed)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert calls == [True]
    assert caught.value.native_state is NativeState.ACKNOWLEDGED
    assert any(
        ref.kind == "native" and ref.stage == "observed" and ref.availability == "available"
        for ref in caught.value.receipt.references
    )
    view = LineageStore.inspect_existing(
        operations.default_migration_state_home(environ=env) / "lineage-v1"
    )
    assert view.operations[0].durable_state is OperationState.NEEDS_RECONCILE
    assert not view.edges


def test_observed_unknown_raw_field_cannot_inherit_expected_verified_coverage(
    tmp_path, monkeypatch
):
    from test_projections import claude_records

    from session_migrate.formats import claude
    from session_migrate.lineage import Coverage

    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    source_id = "30000000-0000-4000-8000-000000000001"
    rows = claude_records(cwd=str(work))
    for row in rows:
        row["sessionId"] = source_id
    root = tmp_path / "native-source"
    path = root / "projects" / claude.project_directory_name(work) / f"{source_id}.jsonl"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    env = environment(tmp_path)
    operation = request(
        tmp_path,
        command="transfer",
        path=None,
        source_id=source_id,
        source_format=AgentFormat.CLAUDE,
        source_home=root,
        cwd=work,
        target_cli_version="0.144.4",
    )
    original = operations._install

    def install(*values):
        result = original(*values)
        if not values[-1]:
            output = values[3]
            content = [json.loads(line) for line in output.read_text().splitlines() if line]
            content[0]["payload"]["unknown_semantic_field"] = "synthetic-unaccounted"
            output.write_text("".join(json.dumps(row) + "\n" for row in content))
        return result

    monkeypatch.setattr(operations, "_install", install)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        run_operation(operation, environ=env)
    assert caught.value.native_state is NativeState.ACKNOWLEDGED
    view = LineageStore.inspect_existing(
        operations.default_migration_state_home(environ=env) / "lineage-v1"
    )
    entry = view.operations[0]
    assert entry.record.source.identity is not None
    assert entry.record.expected_projection.coverage is Coverage.VERIFIED
    assert entry.durable_state is OperationState.NEEDS_RECONCILE
    assert entry.target_checkpoint is None and not view.edges


def test_readonly_proposal_without_home_does_not_require_or_create_ledger(tmp_path):
    operation = private_request(tmp_path)
    prepared = prepare_operation(operation, environ={})
    assert prepared._state.migration_state_home is None
    assert not operation.home.exists()
    prepared.discard()
