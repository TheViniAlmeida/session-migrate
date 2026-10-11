"""Cancel bindings come from an actual prepared B3 READY record."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
import test_c2_services as fixtures

from session_migrate import operations, reconciliation
from session_migrate.lineage import LineageError, LineageStore, NativeState, OperationState, _encode
from session_migrate.operations import OperationRequest, prepare_operation


def ready_fixture(tmp_path):
    env = fixtures.environment(tmp_path)
    source = tmp_path / "source.jsonl"
    fixtures.write(source, fixtures.records())
    prepared = prepare_operation(
        OperationRequest(
            "import",
            path=source,
            source_format="claude",
            target_format="codex",
            home=tmp_path / "target",
            session_id=fixtures.TARGET_ID,
            cwd=Path("/synthetic/work"),
            target_cli_version="0.144.4",
        ),
        environ=env,
    )
    try:
        record = operations._ready_record(prepared._state, prepared._proposal.proposal_id)
        path = tmp_path / "ledger"
        with LineageStore.create_new(path) as store:
            with store.lock_operation(record.operation_id, record.target_namespace) as guard:
                store.ready(record, guard)
            store_uuid = store._store_uuid
        return record, path, hashlib.sha256(_encode(record)).hexdigest(), store_uuid
    finally:
        prepared.discard()


def test_actual_cancel_expected_binding_matches_legacy_bytes(tmp_path):
    record, path, digest, store_uuid = ready_fixture(tmp_path)
    reconciliation.cancel_recovered_ready(
        record.operation_id,
        store_path=path,
        expected_record_sha256=digest,
        expected_store_uuid=store_uuid,
    )
    operation = reconciliation.inspect_recovered_operation(record.operation_id, store_path=path)
    assert operation.durable_state is OperationState.CANCELLED
    assert operation.durable_native_state is NativeState.NATIVE_NOT_STARTED
    assert hashlib.sha256(_encode(operation.record)).hexdigest() == digest


@pytest.mark.parametrize(
    "digest,store_uuid",
    [
        (None, "0" * 32),
        ("0" * 64, None),
        (True, "0" * 32),
        ("A" * 64, "0" * 32),
        ("0" * 64, "invalid"),
    ],
)
def test_cancel_rejects_invalid_expected_pair_before_mutation(tmp_path, digest, store_uuid):
    record, path, _, _ = ready_fixture(tmp_path)
    before = (path / "ledger.sqlite3").read_bytes()
    with pytest.raises(LineageError):
        reconciliation.cancel_recovered_ready(
            record.operation_id,
            store_path=path,
            expected_record_sha256=digest,
            expected_store_uuid=store_uuid,
        )
    assert (path / "ledger.sqlite3").read_bytes() == before


@pytest.mark.parametrize("changed", ("record", "store", "state"))
def test_cancel_checks_expected_inside_real_mutation(tmp_path, monkeypatch, changed):
    record, path, digest, store_uuid = ready_fixture(tmp_path)
    original = LineageStore._row

    def row(store, connection, operation_id):
        state, native, receipt, current = original(store, connection, operation_id)
        # Inspection passes. Only the actual EX mutation sees the changed proof.
        if store._mutating:
            if changed == "record":
                current = replace(current, options_sha256="0" * 64)
            elif changed == "store":
                store._store_uuid = "0" * 32
            else:
                native = NativeState.ATTEMPTED_UNKNOWN
        return state, native, receipt, current

    # _mutate calls transition on its EX-owned private connection. Inject at that
    # boundary instead of faking an operation, checkpoint or acknowledgement.
    mutate = LineageStore._mutate

    def guarded_mutate(store, operation_id, guard, transition, **kwargs):
        def guarded_transition(connection):
            store._mutating = True
            try:
                return transition(connection)
            finally:
                store._mutating = False

        return mutate(store, operation_id, guard, guarded_transition, **kwargs)

    monkeypatch.setattr(LineageStore, "_row", row)
    monkeypatch.setattr(LineageStore, "_mutate", guarded_mutate)
    before = (path / "ledger.sqlite3").read_bytes()
    with pytest.raises(LineageError), LineageStore.open_existing(path) as store:
        store._mutating = False
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.cancel_ready(
                record.operation_id,
                guard,
                expected_record_sha256=digest,
                expected_store_uuid=store_uuid,
            )
    assert (path / "ledger.sqlite3").read_bytes() == before
