"""Synthetic, private namespaces only: no native provider data is read."""

import json
import os
import sqlite3
from dataclasses import replace

import pytest

from session_migrate.lineage import (
    CheckpointRecord,
    CompletionRecord,
    Coverage,
    EvidenceReference,
    LineageError,
    LineageStore,
    NativeNamespace,
    NativeState,
    OperationRecord,
    OperationState,
    PolicyVersions,
    ProjectionExpectation,
    QualifiedIdentity,
    RootWitness,
    SourceMetadata,
)

HASH = "a" * 64
OTHER_HASH = "b" * 64
INPUT_HASH = "c" * 64
EXPORT_HASH = "d" * 64
VERSIONS = PolicyVersions("canonical-v1", "projection-v1", "fake-v1", "fake-v1", "1", "1")


def identity(root, native_id="session-1", profile=HASH):
    return QualifiedIdentity(
        NativeNamespace("codex", str(root), native_id), profile, RootWitness(1, 2)
    )


def operation(root, operation_id="operation-1", native_id="session-1", tracked=True):
    source = identity(root / "source", "source-1") if tracked else None
    return OperationRecord(
        operation_id,
        SourceMetadata(source, HASH, HASH, OTHER_HASH if tracked else None),
        NativeNamespace("codex", str(root / "target"), native_id),
        HASH,
        HASH,
        INPUT_HASH,
        VERSIONS,
        ProjectionExpectation(OTHER_HASH, (OTHER_HASH,), Coverage.VERIFIED)
        if tracked
        else ProjectionExpectation(None, (), Coverage.UNKNOWN),
        (),
    )


def checkpoint(qualified):
    return CheckpointRecord(
        qualified, HASH, OTHER_HASH, (OTHER_HASH,), 1, (), True, Coverage.VERIFIED, None, VERSIONS
    )


def completion(root, record):
    target = identity(root / "target", record.target_namespace.native_id)
    return CompletionRecord(
        target,
        INPUT_HASH,
        checkpoint(record.source.identity),
        replace(checkpoint(target), artifact_sha256=EXPORT_HASH),
        (),
    )


def rewrite_image(path, mutate):
    connection = sqlite3.connect(":memory:")
    connection.deserialize(path.read_bytes())
    mutate(connection)
    connection.commit()
    path.write_bytes(connection.serialize())
    connection.close()


def test_lifecycle_qualified_checkpoints_and_append_only_graph(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            store.begin_publication(record.operation_id, guard)
            store.mark_verifying(record.operation_id, guard)
            receipt = store.complete(record.operation_id, completion(tmp_path, record), guard)
            assert receipt.continuity_reason == "tracked"
            assert (
                store.complete(record.operation_id, completion(tmp_path, record), guard) == receipt
            )
            with pytest.raises(LineageError, match="invalid_transition"):
                store.begin_publication(record.operation_id, guard)
        view = store.recovery_view()
        assert view.operations[0].state is OperationState.COMPLETED
        assert len(view.edges) == 1
        assert view.operations[0].source_checkpoint != view.operations[0].target_checkpoint
    with LineageStore.open_existing(tmp_path / "state") as reopened:
        assert reopened.recovery_view() == view


def test_unqualified_source_keeps_receipt_without_inventing_graph(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path, tracked=False)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            store.begin_publication(record.operation_id, guard)
            store.mark_verifying(record.operation_id, guard)
            done = CompletionRecord(identity(tmp_path / "target"), INPUT_HASH, None, None, ())
            receipt = store.complete(record.operation_id, done, guard)
        assert receipt.continuity_reason == "identity_untracked"
        assert store.recovery_view().edges == ()


def test_readonly_missing_does_not_create_namespace(tmp_path):
    assert LineageStore.inspect_existing(tmp_path / "missing") is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("replacement", [b"", b"not a database"])
def test_existing_empty_or_corrupt_image_fails_closed(tmp_path, replacement):
    with LineageStore.create_new(tmp_path / "state"):
        pass
    (tmp_path / "state" / "ledger.sqlite3").write_bytes(replacement)
    with pytest.raises(LineageError):
        LineageStore.open_existing(tmp_path / "state")


@pytest.mark.parametrize(
    "statement",
    [
        "PRAGMA user_version=2",
        "PRAGMA application_id=0",
        "CREATE TABLE surprise(value TEXT)",
        "CREATE VIEW surprise AS SELECT 1",
        "CREATE TRIGGER surprise AFTER INSERT ON operations BEGIN SELECT 1; END",
        "CREATE VIRTUAL TABLE surprise USING fts5(value)",
    ],
)
def test_unknown_schema_and_objects_rejected(tmp_path, statement):
    with LineageStore.create_new(tmp_path / "state"):
        pass
    rewrite_image(
        tmp_path / "state" / "ledger.sqlite3", lambda connection: connection.execute(statement)
    )
    with pytest.raises(LineageError):
        LineageStore.open_existing(tmp_path / "state")


def test_unknown_record_keys_and_secret_sentinel_are_rejected(tmp_path):
    sentinel = "BODY_SENTINEL_NOT_METADATA"
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
    path = tmp_path / "state" / "ledger.sqlite3"

    def inject(connection):
        payload = json.loads(connection.execute("SELECT record_json FROM operations").fetchone()[0])
        payload["body"] = sentinel
        connection.execute("UPDATE operations SET record_json=?", (json.dumps(payload).encode(),))

    rewrite_image(path, inject)
    with pytest.raises(LineageError) as caught:
        LineageStore.open_existing(tmp_path / "state")
    assert sentinel not in str(caught.value)
    assert caught.value.__cause__ is None
    assert sentinel not in repr(record)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink"])
def test_unsafe_main_files_fail_without_writing_outside(tmp_path, kind):
    with LineageStore.create_new(tmp_path / "state"):
        pass
    image = tmp_path / "state" / "ledger.sqlite3"
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    outside.chmod(0o600)
    image.unlink()
    if kind == "symlink":
        image.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(image, 0o600)
    else:
        os.link(outside, image)
    with pytest.raises(LineageError):
        LineageStore.open_existing(tmp_path / "state")
    assert outside.read_bytes() == b"untouched"


def test_directory_and_main_lock_replacement_detected(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        root = tmp_path / "state"
        root.rename(tmp_path / "old")
        root.mkdir(mode=0o700)
        with pytest.raises(LineageError, match="namespace_changed"):
            store.recovery_view()
    (tmp_path / "state").rmdir()
    (tmp_path / "old").rename(tmp_path / "state")
    lock = tmp_path / "state" / "main.lock"
    lock.rename(tmp_path / "old-lock")
    lock.write_bytes((tmp_path / "old-lock").read_bytes())
    lock.chmod(0o600)
    with pytest.raises(LineageError, match="lock_binding"):
        LineageStore.open_existing(tmp_path / "state")


def test_profile_does_not_change_physical_lock_namespace(tmp_path):
    first = identity(tmp_path / "native", profile=HASH)
    second = replace(first, profile_fingerprint=OTHER_HASH)
    assert first != second
    assert first.namespace.lock_key == second.namespace.lock_key
    assert first.namespace.lock_key != identity(tmp_path / "other").namespace.lock_key


def test_checkpoint_projection_mismatch_is_not_completed(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            store.begin_publication(record.operation_id, guard)
            store.mark_verifying(record.operation_id, guard)
            done = completion(tmp_path, record)
            invalid = replace(
                done, target_checkpoint=replace(done.target_checkpoint, snapshot_sha256=HASH)
            )
            with pytest.raises(LineageError, match="proof_mismatch"):
                store.complete(record.operation_id, invalid, guard)
        assert store.recovery_view().operations[0].state is OperationState.NEEDS_RECONCILE


def test_budget_reserved_before_ready_and_kept_when_uncertain(tmp_path):
    with LineageStore.create_new(tmp_path / "state", max_image_bytes=700_000) as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            before = store.recovery_view().operations[0].reserved_bytes
            store.begin_publication(record.operation_id, guard)
            store.mark_verifying(record.operation_id, guard)
            assert store.recovery_view().operations[0].reserved_bytes <= before
        other = operation(tmp_path, "operation-2", "session-2")
        with (
            store.lock_operation(other.operation_id, other.target_namespace) as guard,
            pytest.raises(LineageError, match="image_budget"),
        ):
            store.ready(other, guard)
        assert len(store.recovery_view().operations) == 1


def test_owner_modes_and_no_sqlite_sidecars(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
    root = tmp_path / "state"
    assert root.stat().st_mode & 0o777 == 0o700
    assert {item.name for item in root.iterdir()} == {"ledger.sqlite3", "main.lock", "locks"}
    assert (root / "locks").stat().st_mode & 0o777 == 0o700
    for item in (root / "main.lock", root / "ledger.sqlite3", *(root / "locks").iterdir()):
        assert item.stat().st_mode & 0o777 == 0o600


def test_references_are_strict_and_reprs_do_not_disclose_paths(tmp_path):
    reference = EvidenceReference("manifest", "planned", "unknown", str(tmp_path / "planned"), HASH)
    assert str(tmp_path) not in repr(reference)
    with pytest.raises(LineageError):
        EvidenceReference("manifest", "verified", "unknown", str(tmp_path / "planned"), HASH)
    with pytest.raises(LineageError):
        NativeNamespace("codex", "relative", "session-1")
    with pytest.raises(LineageError):
        replace(operation(tmp_path), options_sha256="BODY_SENTINEL")


def test_native_state_is_acknowledged_before_complete(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            store.begin_publication(record.operation_id, guard)
            assert (
                store.recovery_view().operations[0].durable_native_state
                is NativeState.NATIVE_NOT_STARTED
            )
            with pytest.raises(LineageError, match="invalid_transition"):
                store.complete(record.operation_id, completion(tmp_path, record), guard)
