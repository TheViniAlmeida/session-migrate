"""C2 storage gates using private synthetic roots and RAM-only SQLite images."""

import hashlib
import json
import os
import select
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest
from test_lineage import (
    HASH,
    OTHER_HASH,
    VERSIONS,
    checkpoint,
    completion,
    identity,
    operation,
    rewrite_image,
)
from test_lineage_recovery import receipt, subprocess_environment

from session_migrate.baselines import (
    BASELINE_APPLICATION_ID,
    MAX_BASELINE_RECORD_BYTES,
    BaselineReceipt,
    BaselineRecordV1,
    BaselineStore,
    _decode_baseline,
    _encode_baseline,
    open_baseline_coordinator_for_commit,
)
from session_migrate.lineage import (
    MAX_METADATA_BYTES,
    MAX_RECEIPT_BYTES,
    Coverage,
    EvidenceReference,
    LineageError,
    LineageStore,
    NativeState,
    OperationState,
    RootWitness,
    _encode,
)


def baseline(root, coordinator, baseline_id="baseline-1"):
    return BaselineRecordV1(
        schema_version=1,
        kind="baseline",
        provenance="adopted_now",
        baseline_id=baseline_id,
        created_at_utc="2026-10-10T12:00:00Z",
        coordinator_store_uuid=coordinator._store_uuid,
        source_profile_binding_kind="profile_v1",
        target_profile_binding_kind="legacy_runtime_v1",
        source_checkpoint=replace(
            checkpoint(identity(root / "source", "source")), boundary_closed=False
        ),
        target_checkpoint=replace(
            checkpoint(identity(root / "target", "target")), boundary_closed=False
        ),
        versions=VERSIONS,
        canonical_policy_sha=HASH,
        projection_policy_sha=HASH,
        relation_sha=OTHER_HASH,
        comparison_sha=OTHER_HASH,
        references=(),
    )


def ack_ready(store, root, record, guard, durable):
    store.ready(record, guard)
    store.begin_publication(record.operation_id, guard)
    store.mark_verifying(record.operation_id, guard)
    if durable is OperationState.NEEDS_RECONCILE:
        store.needs_reconcile(receipt(record, NativeState.ACKNOWLEDGED), guard)


def record_sha(record):
    return hashlib.sha256(_encode(record)).hexdigest()


@pytest.mark.parametrize("durable", [OperationState.VERIFYING, OperationState.NEEDS_RECONCILE])
def test_reconciled_ack_closes_reopens_and_retries_without_publication_claim(tmp_path, durable):
    record = operation(tmp_path)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        ack_ready(store, tmp_path, record, guard, durable)
    with LineageStore.open_existing(tmp_path / "state") as store:
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            assert guard._ready_claim is None and not guard._publication_claim
            saved = store.complete_reconciled(
                record.operation_id,
                record_sha(record),
                durable,
                completion(tmp_path, record),
                guard,
            )
            image = tmp_path / "state" / "ledger.sqlite3"
            previous = image.read_bytes(), image.stat().st_ino
            assert (
                store.complete_reconciled(
                    record.operation_id,
                    record_sha(record),
                    durable,
                    completion(tmp_path, record),
                    guard,
                )
                == saved
            )
            assert (image.read_bytes(), image.stat().st_ino) == previous
        view = store.recovery_view()
        entry = view.operations[0]
        assert entry.durable_state is OperationState.COMPLETED
        assert entry.durable_native_state is NativeState.ACKNOWLEDGED
        assert entry.reserved_bytes == 0
        assert entry.source_checkpoint and entry.target_checkpoint
        assert len(view.edges) == 1
    assert LineageStore.inspect_existing(tmp_path / "state") == view


@pytest.mark.parametrize("mismatch", ["record_sha", "state", "completion"])
def test_reconciled_cas_rejects_without_writes_or_budget_release(tmp_path, mismatch):
    record = operation(tmp_path)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        ack_ready(store, tmp_path, record, guard, OperationState.NEEDS_RECONCILE)
        image = tmp_path / "state" / "ledger.sqlite3"
        previous = image.read_bytes(), image.stat().st_ino
        before = store.recovery_view()
        done = completion(tmp_path, record)
        if mismatch == "completion":
            done = replace(done, input_artifact_sha256=OTHER_HASH)
        with pytest.raises(LineageError):
            store.complete_reconciled(
                record.operation_id,
                OTHER_HASH if mismatch == "record_sha" else record_sha(record),
                OperationState.VERIFYING if mismatch == "state" else OperationState.NEEDS_RECONCILE,
                done,
                guard,
            )
        assert (image.read_bytes(), image.stat().st_ino) == previous
        assert store.recovery_view() == before


@pytest.mark.parametrize("native", list(NativeState)[:2])
def test_reconciled_never_promotes_unknown_or_not_started_ack(tmp_path, native):
    record = operation(tmp_path)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        if native is NativeState.ATTEMPTED_UNKNOWN:
            store.begin_publication(record.operation_id, guard)
            store.mark_attempted(record.operation_id, guard)
        store.needs_reconcile(receipt(record, native), guard)
        previous = store.recovery_view()
        with pytest.raises(LineageError, match="invalid_transition"):
            store.complete_reconciled(
                record.operation_id,
                record_sha(record),
                OperationState.NEEDS_RECONCILE,
                completion(tmp_path, record),
                guard,
            )
        assert store.recovery_view() == previous


def test_reconciled_completion_requires_both_checkpoints_and_exact_completed_record(tmp_path):
    record = operation(tmp_path)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        ack_ready(store, tmp_path, record, guard, OperationState.VERIFYING)
        done = completion(tmp_path, record)
        with pytest.raises(LineageError):
            store.complete_reconciled(
                record.operation_id,
                record_sha(record),
                OperationState.VERIFYING,
                replace(done, source_checkpoint=None),
                guard,
            )
        store.complete_reconciled(
            record.operation_id, record_sha(record), OperationState.VERIFYING, done, guard
        )
        changed = replace(done, target_checkpoint=replace(done.target_checkpoint, turn_count=2))
        with pytest.raises(LineageError, match="proof_mismatch"):
            store.complete_reconciled(
                record.operation_id,
                record_sha(record),
                OperationState.VERIFYING,
                changed,
                guard,
            )


def test_reconciled_releases_only_its_own_reserved_budget(tmp_path):
    first = operation(tmp_path)
    other = operation(tmp_path, "other", "other-target")
    with LineageStore.create_new(tmp_path / "state") as store:
        with store.lock_operation(other.operation_id, other.target_namespace) as guard:
            store.ready(other, guard)
        reserved = store.recovery_view().operations[0].reserved_bytes
        with store.lock_operation(first.operation_id, first.target_namespace) as guard:
            ack_ready(store, tmp_path, first, guard, OperationState.NEEDS_RECONCILE)
            store.complete_reconciled(
                first.operation_id,
                record_sha(first),
                OperationState.NEEDS_RECONCILE,
                completion(tmp_path, first),
                guard,
            )
        entries = {item.record.operation_id: item for item in store.recovery_view().operations}
        assert entries[first.operation_id].reserved_bytes == 0
        assert entries[other.operation_id].reserved_bytes == reserved


@pytest.mark.parametrize(
    "boundary",
    [
        "before_temp_write",
        "after_temp_write",
        "after_temp_fsync",
        "before_rename",
        "after_rename",
        "after_directory_fsync",
    ],
)
def test_reconciled_image_failures_have_atomic_truth_and_idempotent_uncertain_retry(
    tmp_path, boundary
):
    record = operation(tmp_path)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        ack_ready(store, tmp_path, record, guard, OperationState.NEEDS_RECONCILE)
        done = completion(tmp_path, record)

        def fail(event):
            if event == boundary:
                raise OSError("PRIVATE_SYNTHETIC_FAILURE")

        store._fault_hook = fail
        with pytest.raises(LineageError) as caught:
            store.complete_reconciled(
                record.operation_id,
                record_sha(record),
                OperationState.NEEDS_RECONCILE,
                done,
                guard,
            )
        assert "PRIVATE_SYNTHETIC_FAILURE" not in str(caught.value)
        store._fault_hook = None
        entry = store.recovery_view().operations[0]
        persisted = boundary in ("after_rename", "after_directory_fsync")
        assert entry.durable_state is (
            OperationState.COMPLETED if persisted else OperationState.NEEDS_RECONCILE
        )
        assert (entry.reserved_bytes == 0) is persisted
        assert len(store.recovery_view().edges) == int(persisted)
        store.complete_reconciled(
            record.operation_id,
            record_sha(record),
            OperationState.NEEDS_RECONCILE,
            done,
            guard,
        )
    assert LineageStore.inspect_existing(tmp_path / "state").operations[0].reserved_bytes == 0


def test_baseline_inspect_missing_is_readonly_and_bootstrap_has_no_history(tmp_path):
    assert BaselineStore.inspect_existing(tmp_path / "baselines-v1") is None
    assert not list(tmp_path.iterdir())
    with open_baseline_coordinator_for_commit(tmp_path / "lineage-v1") as coordinator:
        view = coordinator.recovery_view()
        assert view.operations == view.edges == ()
    assert LineageStore.inspect_existing(tmp_path / "lineage-v1") == view
    (tmp_path / "orphan").mkdir(mode=0o700)
    with pytest.raises(LineageError):
        open_baseline_coordinator_for_commit(tmp_path / "orphan")
    assert not list((tmp_path / "orphan").iterdir())


def test_baseline_append_reopen_same_id_collision_multiple_anchors_and_no_history(tmp_path):
    with open_baseline_coordinator_for_commit(tmp_path / "lineage-v1") as coordinator:
        record = baseline(tmp_path, coordinator)
        ledger = tmp_path / "lineage-v1" / "ledger.sqlite3"
        original_ledger = ledger.read_bytes(), ledger.stat().st_ino
        with coordinator.lock_operation(
            record.baseline_id,
            record.target_checkpoint.identity.namespace,
            additional_namespaces=(record.source_checkpoint.identity.namespace,),
        ) as guard:
            with BaselineStore.create_new(
                tmp_path / "baselines-v1", coordinator=coordinator, guard=guard
            ) as store:
                saved = store.append(record, coordinator=coordinator, guard=guard)
                image = tmp_path / "baselines-v1" / "ledger.sqlite3"
                original = image.read_bytes(), image.stat().st_ino
                assert store.append(record, coordinator=coordinator, guard=guard) == saved
                assert (image.read_bytes(), image.stat().st_ino) == original
                with pytest.raises(LineageError, match="proof_mismatch"):
                    store.append(
                        replace(record, comparison_sha=HASH), coordinator=coordinator, guard=guard
                    )
                assert (image.read_bytes(), image.stat().st_ino) == original
            with BaselineStore.open_existing(
                tmp_path / "baselines-v1", coordinator=coordinator, guard=guard
            ) as reopened:
                assert reopened.baseline_view().baselines == (record,)
                assert reopened.append(record, coordinator=coordinator, guard=guard) == saved
        second = replace(record, baseline_id="baseline-2")
        with (
            coordinator.lock_operation(
                second.baseline_id,
                second.target_checkpoint.identity.namespace,
                additional_namespaces=(second.source_checkpoint.identity.namespace,),
            ) as guard,
            BaselineStore.open_existing(
                tmp_path / "baselines-v1", coordinator=coordinator, guard=guard
            ) as store,
        ):
            store.append(second, coordinator=coordinator, guard=guard)
            assert len(store.baseline_view().baselines) == 2
        assert (ledger.read_bytes(), ledger.stat().st_ino) == original_ledger
        assert coordinator.recovery_view().operations == coordinator.recovery_view().edges == ()
    view = BaselineStore.inspect_existing(
        tmp_path / "baselines-v1", coordinator_store_uuid=record.coordinator_store_uuid
    )
    assert view.baselines == (record, second)
    assert saved.kind == "baseline" and saved.provenance == "adopted_now"
    assert not hasattr(saved, "operation_id")
    connection = sqlite3.connect(":memory:")
    connection.deserialize(image.read_bytes())
    assert connection.execute("PRAGMA application_id").fetchone() == (BASELINE_APPLICATION_ID,)
    assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    assert {
        name for (name,) in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    } == {"metadata", "baselines"}
    connection.close()


@pytest.mark.parametrize(
    "mutator",
    [
        "ready",
        "check_guard",
        "begin_publication",
        "mark_attempted",
        "mark_verifying",
        "needs_reconcile",
        "cancel_ready",
        "fail_ready",
        "complete",
        "complete_reconciled",
        "recovery_view",
    ],
)
def test_baseline_has_no_historical_operation_mutator_before_io(tmp_path, mutator, monkeypatch):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with (
            coordinator.lock_operation(
                record.baseline_id, record.target_checkpoint.identity.namespace
            ) as guard,
            BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ) as store,
        ):

            def forbidden(*args, **kwargs):
                pytest.fail("historical storage I/O was reached")

            monkeypatch.setattr(store, "_load", forbidden)
            monkeypatch.setattr(store, "_persist", forbidden)
            with pytest.raises(LineageError, match="invalid_transition"):
                getattr(store, mutator)()


@pytest.mark.parametrize(
    "boundary",
    [
        "before_temp_write",
        "after_temp_write",
        "after_temp_fsync",
        "before_rename",
        "after_rename",
        "after_directory_fsync",
    ],
)
def test_baseline_atomic_append_faults_and_same_id_recovery(tmp_path, boundary):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        ledger_before = (tmp_path / "coordinator" / "ledger.sqlite3").read_bytes()
        with coordinator.lock_operation(
            record.baseline_id,
            record.target_checkpoint.identity.namespace,
            additional_namespaces=(record.source_checkpoint.identity.namespace,),
        ) as guard:
            with BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ) as store:

                def fail(event):
                    if event == boundary:
                        raise OSError("PRIVATE_BASELINE_FAILURE")

                store._fault_hook = fail
                with pytest.raises(LineageError) as caught:
                    store.append(record, coordinator=coordinator, guard=guard)
                assert "PRIVATE_BASELINE_FAILURE" not in str(caught.value)
                assert caught.value.code == (
                    "persistence_uncertain"
                    if boundary in ("after_rename", "after_directory_fsync")
                    else "image_write_failed"
                )
                store._fault_hook = None
                persisted = boundary in ("after_rename", "after_directory_fsync")
                assert store.baseline_view().baselines == ((record,) if persisted else ())
            with BaselineStore.open_existing(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ) as reopened:
                reopened.append(record, coordinator=coordinator, guard=guard)
                assert reopened.baseline_view().baselines == (record,)
        assert (tmp_path / "coordinator" / "ledger.sqlite3").read_bytes() == ledger_before


def test_baseline_guards_bind_coordinator_both_namespaces_and_operation_id(tmp_path):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with (
            coordinator.lock_operation(
                record.baseline_id, record.target_checkpoint.identity.namespace
            ) as guard,
            BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ) as store,
            pytest.raises(LineageError, match="invalid_guard"),
        ):
            store.append(record, coordinator=coordinator, guard=guard)
        with (
            coordinator.lock_operation(
                "other-id",
                record.target_checkpoint.identity.namespace,
                additional_namespaces=(record.source_checkpoint.identity.namespace,),
            ) as guard,
            BaselineStore.open_existing(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ) as store,
            pytest.raises(LineageError, match="invalid_guard"),
        ):
            store.append(record, coordinator=coordinator, guard=guard)
        with (
            LineageStore.create_new(tmp_path / "another-coordinator") as wrong,
            wrong.lock_operation(
                record.baseline_id, record.target_checkpoint.identity.namespace
            ) as guard,
            pytest.raises(LineageError, match="lock_binding"),
        ):
            BaselineStore.open_existing(tmp_path / "baseline", coordinator=wrong, guard=guard)
        with pytest.raises(LineageError, match="lock_binding"):
            BaselineStore.inspect_existing(tmp_path / "baseline", coordinator_store_uuid="f" * 32)
        with pytest.raises(LineageError, match="invalid_guard"):
            BaselineStore.open_existing(tmp_path / "baseline", coordinator=coordinator, guard=guard)


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "operation"},
        {"provenance": "imported"},
        {"schema_version": True},
        {"created_at_utc": "2026-10-10T12:00:00-03:00"},
        {"created_at_utc": "2026-02-30T12:00:00Z"},
        {"coordinator_store_uuid": "invalid"},
        {"source_profile_binding_kind": "implicit"},
        {"target_profile_binding_kind": "legacy"},
        {"comparison_sha": "invalid"},
    ],
)
def test_baseline_metadata_is_exact_and_profile_kinds_never_inferred(tmp_path, change):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with pytest.raises(LineageError):
            replace(record, **change)


@pytest.mark.parametrize("change", ["unqualified", "partial", "closed", "versions"])
def test_baseline_requires_qualified_verified_unproven_checkpoints(tmp_path, change):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        source = record.source_checkpoint
        if change == "unqualified":
            source = replace(source, identity=None)
        elif change == "partial":
            source = replace(source, coverage=Coverage.PARTIAL)
        elif change == "closed":
            source = replace(source, boundary_closed=True)
        else:
            source = replace(source, versions=replace(VERSIONS, source_adapter="other-v1"))
        with pytest.raises(LineageError):
            replace(record, source_checkpoint=source)


def test_baseline_pair_index_uses_profiles_namespaces_binding_kinds_but_not_witnesses(tmp_path):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        source = record.source_checkpoint
        witness = replace(source.identity, root_witness=RootWitness(1, 99))
        assert (
            replace(record, source_checkpoint=replace(source, identity=witness)).pair_key
            == record.pair_key
        )
        profile = replace(source.identity, profile_fingerprint=OTHER_HASH)
        assert (
            replace(record, source_checkpoint=replace(source, identity=profile)).pair_key
            != record.pair_key
        )
        assert (
            replace(record, source_profile_binding_kind="legacy_runtime_v1").pair_key
            != record.pair_key
        )
        assert (
            replace(
                record, source_checkpoint=record.target_checkpoint, target_checkpoint=source
            ).pair_key
            != record.pair_key
        )


def test_baseline_codec_duplicate_unknown_missing_keys_and_noncanonical_json_fail_closed(tmp_path):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        encoded = _encode_baseline(record)
        assert _decode_baseline(BaselineRecordV1, encoded) == record
        wire = json.loads(encoded)
        for malformed in (
            dict(wire, operation_id="fake-history"),
            {key: item for key, item in wire.items() if key != "source_profile_binding_kind"},
            dict(wire, schema_version=2),
            dict(wire, body="PRIVATE_BODY_SENTINEL"),
        ):
            with pytest.raises(LineageError) as caught:
                _decode_baseline(BaselineRecordV1, json.dumps(malformed).encode())
            assert "PRIVATE_BODY_SENTINEL" not in str(caught.value)
        with pytest.raises(LineageError):
            _decode_baseline(
                BaselineRecordV1,
                encoded.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1),
            )
        with pytest.raises(LineageError, match="noncanonical_metadata"):
            _decode_baseline(BaselineRecordV1, json.dumps(wire, indent=2).encode())
        assert str(tmp_path) not in repr(record)


def test_baseline_bounds_never_truncate_checkpoints_references_record_or_receipt(tmp_path):
    assert MAX_BASELINE_RECORD_BYTES == 384 * 1024
    assert MAX_METADATA_BYTES == 128 * 1024 and MAX_RECEIPT_BYTES == 32 * 1024
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        refs = (EvidenceReference("native", "verified", "available", "/synthetic", HASH),) * 17
        with pytest.raises(LineageError):
            replace(record, references=refs)
        with pytest.raises(LineageError, match="metadata_limit"):
            replace(
                record.source_checkpoint,
                unit_hashes=(HASH,) * 1024,
                tool_link_hashes=(HASH,) * 1024,
            )
        long_refs = tuple(
            EvidenceReference("native", "verified", "available", "/" + "r" * 4000 + str(i), HASH)
            for i in range(9)
        )
        with pytest.raises(LineageError, match="metadata_limit"):
            replace(record, references=long_refs)
        with pytest.raises(LineageError, match="metadata_limit"):
            _decode_baseline(BaselineRecordV1, b"x" * (MAX_BASELINE_RECORD_BYTES + 1))
        maximum = replace(
            record.source_checkpoint, unit_hashes=(HASH,) * 1024, tool_link_hashes=(HASH,) * 800
        )
        large = replace(record, source_checkpoint=maximum, target_checkpoint=maximum)
        assert _decode_baseline(BaselineRecordV1, _encode_baseline(large)) == large
        assert len(_encode_baseline(large)) > MAX_METADATA_BYTES
        with (
            coordinator.lock_operation(
                large.baseline_id,
                large.target_checkpoint.identity.namespace,
                additional_namespaces=(large.source_checkpoint.identity.namespace,),
            ) as guard,
            BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ) as store,
        ):
            saved = store.append(large, coordinator=coordinator, guard=guard)
            assert (
                _decode_baseline(BaselineReceipt, _encode_baseline(saved), MAX_RECEIPT_BYTES)
                == saved
            )
            assert store.baseline_view().baselines == (large,)


def test_baseline_image_budget_fails_before_publish_and_preserves_empty_image(tmp_path):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        huge = replace(
            record.source_checkpoint, unit_hashes=(HASH,) * 1024, tool_link_hashes=(HASH,) * 800
        )
        record = replace(record, source_checkpoint=huge)
        with (
            coordinator.lock_operation(
                record.baseline_id,
                record.target_checkpoint.identity.namespace,
                additional_namespaces=(record.source_checkpoint.identity.namespace,),
            ) as guard,
            BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard, max_image_bytes=32768
            ) as store,
        ):
            image = tmp_path / "baseline" / "ledger.sqlite3"
            before = image.read_bytes(), image.stat().st_ino
            with pytest.raises(LineageError, match="image_budget"):
                store.append(record, coordinator=coordinator, guard=guard)
            assert (image.read_bytes(), image.stat().st_ino) == before
            assert store.baseline_view().baselines == ()


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA user_version=2",
        "PRAGMA application_id=0",
        "CREATE TABLE surprise(value TEXT)",
        "CREATE INDEX surprise ON baselines(pair_key)",
        "CREATE VIEW surprise AS SELECT 1",
        "CREATE TRIGGER surprise AFTER INSERT ON baselines BEGIN SELECT 1; END",
    ],
)
def test_baseline_exact_schema_rejects_auxiliary_objects(tmp_path, statement):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with coordinator.lock_operation(
            record.baseline_id, record.target_checkpoint.identity.namespace
        ) as guard:
            with BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ):
                pass
            rewrite_image(
                tmp_path / "baseline" / "ledger.sqlite3",
                lambda connection: connection.execute(statement),
            )
            with pytest.raises(LineageError):
                BaselineStore.open_existing(
                    tmp_path / "baseline", coordinator=coordinator, guard=guard
                )


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink"])
def test_baseline_reuses_unsafe_file_guards_without_outside_writes(tmp_path, kind):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with coordinator.lock_operation(
            record.baseline_id, record.target_checkpoint.identity.namespace
        ) as guard:
            with BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ):
                pass
            image = tmp_path / "baseline" / "ledger.sqlite3"
            outside = tmp_path / "outside"
            outside.write_bytes(b"synthetic-outside-untouched")
            outside.chmod(0o600)
            image.unlink()
            if kind == "symlink":
                image.symlink_to(outside)
            elif kind == "fifo":
                os.mkfifo(image, 0o600)
            else:
                os.link(outside, image)
            with pytest.raises(LineageError):
                BaselineStore.open_existing(
                    tmp_path / "baseline", coordinator=coordinator, guard=guard
                )
            assert outside.read_bytes() == b"synthetic-outside-untouched"


def test_additional_namespace_locks_deduplicate_preserve_filenames_and_sort_before_main(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        source, target = record.source.identity.namespace, record.target_namespace
        with store.lock_operation(record.operation_id, target) as guard:
            original = tuple(name for name, _, _ in guard._locks)
        with store.lock_operation(
            record.operation_id, target, additional_namespaces=(source, target, source)
        ) as guard:
            names = tuple(name for name, _, _ in guard._locks)
            assert set(names) == set(original) | {source.lock_key + ".lock"}
            assert names == tuple(sorted(names)) and len(names) == 3
            assert guard._ready_claim is None and not guard._publication_claim


def test_baseline_main_lock_order_is_coordinator_shared_then_baseline_exclusive(
    tmp_path, monkeypatch
):
    import session_migrate.lineage as lineage

    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with (
            coordinator.lock_operation(
                record.baseline_id,
                record.target_checkpoint.identity.namespace,
                additional_namespaces=(record.source_checkpoint.identity.namespace,),
            ) as guard,
            BaselineStore.create_new(
                tmp_path / "baseline", coordinator=coordinator, guard=guard
            ) as store,
        ):
            events = []
            original = lineage.fcntl.flock

            def track(fd, flags):
                events.append((fd, flags))
                return original(fd, flags)

            monkeypatch.setattr(lineage.fcntl, "flock", track)
            store.append(record, coordinator=coordinator, guard=guard)
            assert events == [
                (coordinator._main_fd, lineage.fcntl.LOCK_SH),
                (store._main_fd, lineage.fcntl.LOCK_EX),
                (store._main_fd, lineage.fcntl.LOCK_UN),
                (coordinator._main_fd, lineage.fcntl.LOCK_UN),
            ]


@pytest.mark.parametrize("component", ["reconciled", "baseline"])
@pytest.mark.parametrize("sync_kind", ["file", "directory"])
def test_real_fsync_failure_keeps_pre_or_post_rename_truth(
    tmp_path, component, sync_kind, monkeypatch
):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        if component == "reconciled":
            record = operation(tmp_path)
            operation_id, target, additional = record.operation_id, record.target_namespace, ()
        else:
            record = baseline(tmp_path, coordinator)
            operation_id = record.baseline_id
            target = record.target_checkpoint.identity.namespace
            additional = (record.source_checkpoint.identity.namespace,)
        with coordinator.lock_operation(
            operation_id, target, additional_namespaces=additional
        ) as guard:
            if component == "reconciled":
                ack_ready(coordinator, tmp_path, record, guard, OperationState.NEEDS_RECONCILE)
                store = coordinator
            else:
                store = BaselineStore.create_new(
                    tmp_path / "baseline", coordinator=coordinator, guard=guard
                )
            try:
                original = os.fsync

                def failed_fsync(descriptor):
                    if (sync_kind == "directory" and descriptor == store._root_fd) or (
                        sync_kind == "file" and descriptor != store._root_fd
                    ):
                        raise OSError("PRIVATE_FSYNC_FAILURE")
                    return original(descriptor)

                with monkeypatch.context() as patch:
                    patch.setattr(os, "fsync", failed_fsync)
                    with pytest.raises(LineageError) as caught:
                        if component == "reconciled":
                            store.complete_reconciled(
                                record.operation_id,
                                record_sha(record),
                                OperationState.NEEDS_RECONCILE,
                                completion(tmp_path, record),
                                guard,
                            )
                        else:
                            store.append(record, coordinator=coordinator, guard=guard)
                assert caught.value.code == (
                    "persistence_uncertain" if sync_kind == "directory" else "image_write_failed"
                )
                if component == "reconciled":
                    assert store.recovery_view().operations[0].durable_state is (
                        OperationState.COMPLETED
                        if sync_kind == "directory"
                        else OperationState.NEEDS_RECONCILE
                    )
                else:
                    assert store.baseline_view().baselines == (
                        (record,) if sync_kind == "directory" else ()
                    )
            finally:
                if store is not coordinator:
                    store.close()


def test_recovered_ready_remains_cancel_only_and_cannot_complete(tmp_path):
    record = operation(tmp_path)
    with (
        LineageStore.create_new(tmp_path / "coordinator") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
    with LineageStore.open_existing(tmp_path / "coordinator") as store:
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            before = store.recovery_view()
            with pytest.raises(LineageError, match="invalid_transition"):
                store.complete_reconciled(
                    record.operation_id,
                    record_sha(record),
                    OperationState.VERIFYING,
                    completion(tmp_path, record),
                    guard,
                )
            assert store.recovery_view() == before
            with pytest.raises(LineageError, match="invalid_transition"):
                store.begin_publication(record.operation_id, guard)
            store.cancel_ready(record.operation_id, guard)
        assert store.recovery_view().operations[0].durable_state is OperationState.CANCELLED


def test_reconciled_portable_source_allows_absent_source_checkpoint_like_current_completion(
    tmp_path,
):
    from test_lineage import EXPORT_HASH, INPUT_HASH

    from session_migrate.lineage import CheckpointRecord, CompletionRecord

    record = operation(tmp_path, tracked=False)
    target = identity(tmp_path / "target")
    target_cp = CheckpointRecord(
        target, EXPORT_HASH, None, (), 0, (), False, Coverage.UNKNOWN, None, VERSIONS
    )
    done = CompletionRecord(target, INPUT_HASH, None, target_cp, ())
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        ack_ready(store, tmp_path, record, guard, OperationState.NEEDS_RECONCILE)
        expected, edge = store._completion(record, done)
        assert edge is None and expected.continuity_reason == "identity_untracked"
        saved = store.complete_reconciled(
            record.operation_id, record_sha(record), OperationState.NEEDS_RECONCILE, done, guard
        )
        assert saved == expected
        assert saved.source_checkpoint_sha256 is None
        assert saved.target_checkpoint_sha256 is not None
    view = LineageStore.inspect_existing(tmp_path / "state")
    assert view.operations[0].source_checkpoint is None
    assert view.operations[0].target_checkpoint == target_cp
    assert view.edges == ()


def test_current_b3_file_import_ack_with_unqualified_source_reconciles_without_reimport(
    tmp_path, monkeypatch
):
    from session_migrate import operations
    from session_migrate.errors import OperationNeedsReconcileError
    from session_migrate.model import AgentFormat, TargetFormat
    from session_migrate.operations import OperationKind, OperationRequest, run_operation

    source_id = "10000000-0000-4000-8000-000000000000"
    user_id = "10000000-0000-4000-8000-000000000001"
    assistant_id = "10000000-0000-4000-8000-000000000002"
    common = {
        "sessionId": source_id,
        "cwd": str(tmp_path),
        "version": "2.1.209",
        "isSidechain": False,
        "userType": "external",
    }
    rows = [
        dict(
            common,
            type="user",
            uuid=user_id,
            parentUuid=None,
            timestamp="2026-10-10T12:00:00Z",
            message={"role": "user", "content": "Synthetic portable source message."},
        ),
        dict(
            common,
            type="assistant",
            uuid=assistant_id,
            parentUuid=user_id,
            timestamp="2026-10-10T12:00:01Z",
            message={
                "id": "msg_synthetic",
                "type": "message",
                "role": "assistant",
                "model": "fixture-model",
                "content": [{"type": "text", "text": "Synthetic response."}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    ]
    input_path = tmp_path / "portable-claude.jsonl"
    input_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    input_path.chmod(0o600)
    env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
    ):
        path = tmp_path / key.lower()
        path.mkdir(mode=0o700)
        env[key] = str(path)
    request = OperationRequest(
        command=OperationKind.IMPORT,
        path=input_path,
        source_format=AgentFormat.CLAUDE,
        target_format=TargetFormat.CODEX,
        home=tmp_path / "target",
        cwd=tmp_path,
        target_cli_version="0.144.4",
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    captured = {}

    def finalization_failed(store, operation_id, done, guard):
        captured["done"] = done
        raise LineageError("image_write_failed")

    with monkeypatch.context() as patch:
        patch.setattr(LineageStore, "complete", finalization_failed)
        with pytest.raises(OperationNeedsReconcileError) as failure:
            run_operation(request, environ=env, migration_state_home=tmp_path / "migration-state")
    assert failure.value.native_state is NativeState.ACKNOWLEDGED
    path = tmp_path / "migration-state" / "lineage-v1"
    entry = LineageStore.inspect_existing(path).operations[0]
    assert entry.record.source.identity is None
    assert entry.durable_state is OperationState.NEEDS_RECONCILE
    assert entry.durable_native_state is NativeState.ACKNOWLEDGED
    done = captured["done"]
    assert done.target_checkpoint is not None
    assert done.source_checkpoint is None or done.source_checkpoint.identity is None
    installed = tuple((tmp_path / "target").rglob("*.jsonl"))
    assert len(installed) == 1
    original_input = installed[0].read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("recovery attempted another native install")

    monkeypatch.setattr(operations, "_install", forbidden)
    with (
        LineageStore.open_existing(path) as store,
        store.lock_operation(entry.record.operation_id, entry.record.target_namespace) as guard,
    ):
        expected, edge = store._completion(entry.record, done)
        assert edge is None and expected.continuity_reason == "identity_untracked"
        assert (
            store.complete_reconciled(
                entry.record.operation_id,
                record_sha(entry.record),
                entry.durable_state,
                done,
                guard,
            )
            == expected
        )
    reopened = LineageStore.inspect_existing(path)
    assert reopened.operations[0].durable_state is OperationState.COMPLETED
    assert reopened.edges == ()
    assert installed[0].read_bytes() == original_input


def test_reversed_source_target_lock_processes_serialize_without_deadlock(tmp_path):
    with LineageStore.create_new(tmp_path / "state"):
        pass
    script = """
import sys
from pathlib import Path
from session_migrate.lineage import LineageStore, NativeNamespace
root = Path(sys.argv[1])
source = NativeNamespace('codex', str(root / 'native-a'), 'a')
target = NativeNamespace('codex', str(root / 'native-b'), 'b')
if sys.argv[2] == 'reverse':
    source, target = target, source
with LineageStore.open_existing(root / 'state') as store:
    print('waiting', flush=True)
    with store.lock_operation(sys.argv[2], target, additional_namespaces=(source,)):
        print('locked', flush=True)
        sys.stdin.readline()
        print('released', flush=True)
"""
    processes = []

    def line(process):
        assert select.select([process.stdout], [], [], 5)[0]
        return process.stdout.readline()

    try:
        first = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), "forward"],
            env=subprocess_environment(tmp_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        processes.append(first)
        assert line(first) == b"waiting\n" and line(first) == b"locked\n"
        second = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), "reverse"],
            env=subprocess_environment(tmp_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        processes.append(second)
        assert line(second) == b"waiting\n"
        assert not select.select([second.stdout], [], [], 0.2)[0]
        first.stdin.write(b"release\n")
        first.stdin.flush()
        assert line(first) == b"released\n"
        assert line(second) == b"locked\n"
        second.stdin.write(b"release\n")
        second.stdin.flush()
        assert line(second) == b"released\n"
        for process in processes:
            output, error = process.communicate(timeout=5)
            assert process.returncode == 0 and output == error == b""
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
