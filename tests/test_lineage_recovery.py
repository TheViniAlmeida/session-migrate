"""Process death, bounded finalization, and private POSIX namespace fixtures."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_lineage import (
    EXPORT_HASH,
    HASH,
    INPUT_HASH,
    OTHER_HASH,
    checkpoint,
    completion,
    identity,
    operation,
    rewrite_image,
)

from session_migrate.errors import OperationNeedsReconcileError
from session_migrate.lineage import (
    INITIAL_CLOSE_BUDGET,
    MAX_METADATA_BYTES,
    MAX_RECEIPT_BYTES,
    MIN_CLOSE_BUDGET,
    EvidenceReference,
    LineageError,
    LineageStore,
    NativeState,
    OperationState,
    ReconcileReceipt,
    _decode,
    _encode,
    _memory,
)


def subprocess_environment(private_root):
    repository = Path(__file__).resolve().parents[1]
    return {
        "HOME": str(private_root),
        "XDG_CONFIG_HOME": str(private_root),
        "XDG_DATA_HOME": str(private_root),
        "XDG_CACHE_HOME": str(private_root),
        "TMPDIR": str(private_root),
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join((str(repository / "src"), str(repository / "tests"))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def receipt(record, native=NativeState.NATIVE_NOT_STARTED, reason="interrupted"):
    return ReconcileReceipt(
        1,
        record.operation_id,
        record.target_namespace.native_id,
        "recovery",
        reason,
        native,
        (),
        False,
    )


@pytest.mark.parametrize(
    ("boundary", "durable"),
    [
        ("before_temp_write", OperationState.READY),
        ("after_temp_write", OperationState.READY),
        ("after_temp_fsync", OperationState.READY),
        ("before_rename", OperationState.READY),
        ("after_rename", OperationState.PUBLISHING),
        ("after_directory_fsync", OperationState.PUBLISHING),
    ],
)
def test_write_failure_preserves_truth_and_cleans_only_own_staging(tmp_path, boundary, durable):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        competitor = tmp_path / "state" / (".ledger-" + "f" * 32 + ".tmp")
        competitor.write_bytes(b"competitor-private-staging")
        competitor.chmod(0o600)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)

            def fail(event):
                if event == boundary:
                    raise OSError("SYNTHETIC_ERROR_SENTINEL")

            store._fault_hook = fail
            with pytest.raises(LineageError) as caught:
                store.begin_publication(record.operation_id, guard)
            store._fault_hook = None
            assert "SYNTHETIC_ERROR_SENTINEL" not in str(caught.value)
            assert caught.value.__cause__ is None
            observed = store.recovery_view().operations[0]
            assert observed.durable_state is durable
            assert observed.durable_native_state is NativeState.NATIVE_NOT_STARTED
            if durable is OperationState.PUBLISHING:
                assert observed.native_state is NativeState.ATTEMPTED_UNKNOWN
            assert competitor.read_bytes() == b"competitor-private-staging"
            assert list((tmp_path / "state").glob(".ledger-*.tmp")) == [competitor]
            with pytest.raises(LineageError, match="invalid_guard"):
                store.check_guard(
                    record.operation_id, guard, expected_state=OperationState.PUBLISHING
                )
            with pytest.raises(LineageError, match="invalid_transition"):
                store.begin_publication(record.operation_id, guard)


@pytest.mark.parametrize("boundary", ["after_temp_fsync", "after_rename", "after_directory_fsync"])
def test_subprocess_crash_never_reconstructs_executable_request(tmp_path, boundary):
    with LineageStore.create_new(tmp_path / "state"):
        pass
    script = """
import os, sys
from pathlib import Path
from session_migrate.lineage import LineageStore
from test_lineage import operation
root = Path(sys.argv[1])
record = operation(root)
with LineageStore.open_existing(root / 'state') as store:
    with store.lock_operation(record.operation_id, record.target_namespace) as guard:
        store.ready(record, guard)
        store._fault_hook = lambda event: os._exit(87) if event == sys.argv[2] else None
        store.begin_publication(record.operation_id, guard)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), boundary],
        env=subprocess_environment(tmp_path),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 87
    assert result.stdout == result.stderr == b""
    view = LineageStore.inspect_existing(tmp_path / "state")
    entry = view.operations[0]
    assert entry.durable_state is (
        OperationState.READY if boundary == "after_temp_fsync" else OperationState.PUBLISHING
    )
    assert not hasattr(entry, "request")
    assert not hasattr(entry, "publish")
    assert entry.durable_native_state is NativeState.NATIVE_NOT_STARTED
    if entry.durable_state is OperationState.PUBLISHING:
        assert entry.native_state is NativeState.ATTEMPTED_UNKNOWN
    assert len(view.operations) == 1


def test_uncertain_target_absence_cannot_cancel_release_or_retry(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            store.begin_publication(record.operation_id, guard)
            store.mark_attempted(record.operation_id, guard)
            saved = store.needs_reconcile(receipt(record, NativeState.ATTEMPTED_UNKNOWN), guard)
            assert saved.ledger_recorded
            assert not (tmp_path / "target").exists()
            for action in (store.begin_publication, store.cancel_ready, store.fail_ready):
                with pytest.raises(LineageError, match="invalid_transition"):
                    action(record.operation_id, guard)
            with pytest.raises(LineageError, match="invalid_transition"):
                store.needs_reconcile(receipt(record), guard)
        entry = store.recovery_view().operations[0]
        assert entry.reserved_bytes >= MIN_CLOSE_BUDGET
        assert entry.state is OperationState.NEEDS_RECONCILE
        other = operation(tmp_path, "other-operation")
        with (
            store.lock_operation(other.operation_id, other.target_namespace) as guard,
            pytest.raises(LineageError, match="reservation_conflict"),
        ):
            store.ready(other, guard)


@pytest.mark.parametrize("finish", ["cancel_ready", "fail_ready"])
def test_cancel_before_intent_releases_budget_and_target_reservation(tmp_path, finish):
    with LineageStore.create_new(tmp_path / "state", max_image_bytes=700_000) as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            getattr(store, finish)(record.operation_id, guard)
        assert store.recovery_view().operations[0].reserved_bytes == 0
        second = operation(tmp_path, "operation-2")
        with store.lock_operation(second.operation_id, second.target_namespace) as guard:
            store.ready(second, guard)
        assert len(store.recovery_view().operations) == 2


def test_operation_namespace_locks_are_global_ordered_and_persistent(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            names = [name for name, _, _ in guard._locks]
            assert names == sorted(names)
            script = """
import fcntl, os, sys
descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('blocked', flush=True)
else:
    raise SystemExit(2)
"""
            path = tmp_path / "state" / "locks" / (record.target_namespace.lock_key + ".lock")
            result = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                env=subprocess_environment(tmp_path),
                capture_output=True,
                timeout=10,
            )
            assert result.returncode == 0
            assert result.stdout == b"blocked\n"
        assert sorted(item.name for item in (tmp_path / "state" / "locks").iterdir()) == names


def test_two_processes_same_reservation_do_not_both_reach_ready(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        script = """
import sys
from pathlib import Path
from session_migrate.lineage import LineageError, LineageStore
from test_lineage import operation
root = Path(sys.argv[1])
record = operation(root)
print('waiting', flush=True)
with LineageStore.open_existing(root / 'state') as store:
    with store.lock_operation(record.operation_id, record.target_namespace) as guard:
        try:
            store.ready(record, guard)
        except LineageError as error:
            print(error.code, flush=True)
        else:
            print('ready', flush=True)
"""
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path)],
                env=subprocess_environment(tmp_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert child.stdout.readline() == b"waiting\n"
                store.ready(record, guard)
                with pytest.raises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.1)
            except BaseException:
                child.kill()
                child.wait()
                raise
        output, errors = child.communicate(timeout=10)
        assert child.returncode == 0
        assert output == b"reservation_conflict\n"
        assert errors == b""
        assert len(store.recovery_view().operations) == 1


def test_parent_swap_during_fsync_does_not_touch_outside_namespace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    marker = outside / "marker"
    marker.write_bytes(b"unchanged")
    marker.chmod(0o600)
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)

            def swap(event):
                if event == "after_temp_fsync":
                    (tmp_path / "state").rename(tmp_path / "original")
                    (tmp_path / "state").symlink_to(outside, target_is_directory=True)

            store._fault_hook = swap
            with pytest.raises(LineageError, match="namespace_changed"):
                store.begin_publication(record.operation_id, guard)
    assert {entry.name for entry in outside.iterdir()} == {"marker"}
    assert marker.read_bytes() == b"unchanged"


def test_lock_swap_foreign_guard_closed_guard_and_readonly_refuse_writes(tmp_path):
    with (
        LineageStore.create_new(tmp_path / "state") as first,
        LineageStore.open_existing(tmp_path / "state") as second,
    ):
        record = operation(tmp_path)
        with first.lock_operation(record.operation_id, record.target_namespace) as guard:
            with pytest.raises(LineageError, match="invalid_guard"):
                second.ready(record, guard)
            name, _, _ = guard._locks[0]
            path = tmp_path / "state" / "locks" / name
            path.rename(tmp_path / "old-operation-lock")
            path.write_bytes(b"")
            path.chmod(0o600)
            with pytest.raises(LineageError, match="namespace_changed"):
                guard.check()
        with pytest.raises(LineageError, match="invalid_guard"):
            guard.check()
    with (
        LineageStore.open_existing(tmp_path / "state", readonly=True) as store,
        pytest.raises(LineageError, match="readonly_store"),
        store.lock_operation(record.operation_id, record.target_namespace),
    ):
        pass


def test_sqlite_never_opens_a_path_and_denies_attach_or_extensions(tmp_path, monkeypatch):
    original = sqlite3.connect
    observed = []

    def connect(path, *args, **kwargs):
        observed.append(path)
        assert path == ":memory:"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with LineageStore.create_new(tmp_path / "state"):
        pass
    with LineageStore.open_existing(tmp_path / "state"):
        pass
    connection = _memory()
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("ATTACH DATABASE ':memory:' AS extra")
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("SELECT load_extension('untrusted')")
    connection.close()
    assert observed and set(observed) == {":memory:"}


@pytest.mark.parametrize(
    "payload",
    [
        b'{"device":1,"device":2,"inode":1}',
        b'{"device":true,"inode":1}',
        b'{"device":NaN,"inode":1}',
        b'{"device":Infinity,"inode":1}',
        b"[" * 1000 + b"]" * 1000,
    ],
)
def test_duplicate_keys_nan_bool_and_depth_rejected(payload):
    from session_migrate.lineage import RootWitness

    with pytest.raises(LineageError):
        _decode(RootWitness, payload)


def test_oversize_image_and_metadata_fail_before_ready(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with pytest.raises(LineageError, match="invalid_metadata"):
            replace(record.expected_projection, unit_hashes=(HASH,) * 1025)
        assert store.recovery_view().operations == ()
    path = tmp_path / "state" / "ledger.sqlite3"
    with path.open("ab") as output:
        output.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(LineageError, match="image_limit"):
        LineageStore.open_existing(tmp_path / "state")


def test_existing_orphan_empty_and_unsafe_ancestors_never_repaired(tmp_path):
    orphan = tmp_path / "orphan"
    orphan.mkdir(mode=0o700)
    lock = orphan / "main.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    before = (lock.stat(), lock.read_bytes())
    with pytest.raises(LineageError):
        LineageStore.create_new(orphan)
    with pytest.raises(LineageError):
        LineageStore.inspect_existing(orphan)
    assert (lock.stat(), lock.read_bytes()) == before
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(LineageError):
        LineageStore.create_new(unsafe / "state")
    assert list(unsafe.iterdir()) == []
    assert unsafe.stat().st_mode & 0o777 == 0o777


def test_reconcile_receipt_truth_is_separate_from_legacy_import_success(tmp_path):
    record = operation(tmp_path)
    for state in NativeState:
        error = OperationNeedsReconcileError(receipt(record, state))
        assert error.receipt.native_state is state
        assert error.receipt.ledger_recorded is False
        assert not hasattr(error, "import_succeeded")
        assert str(tmp_path) not in str(error)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        saved = store.needs_reconcile(receipt(record), guard)
        assert saved.ledger_recorded
        assert store.recovery_view().operations[0].receipt == saved


def test_replay_different_completion_cannot_rewrite_graph(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            store.begin_publication(record.operation_id, guard)
            store.mark_verifying(record.operation_id, guard)
            done = completion(tmp_path, record)
            store.complete(record.operation_id, done, guard)
            before = store.recovery_view()
            for changed in (
                replace(done, input_artifact_sha256=HASH),
                replace(
                    done,
                    target_identity=replace(
                        done.target_identity,
                        root_witness=replace(done.target_identity.root_witness, inode=99),
                    ),
                ),
            ):
                with pytest.raises(LineageError, match="proof_mismatch"):
                    store.complete(record.operation_id, changed, guard)
            assert store.recovery_view() == before


def test_largest_checkpoints_and_receipt_fit_reserved_budget_near_cap(tmp_path):
    record = operation(tmp_path, "o" * 128, "n" * 128)
    large_hashes = (HASH,) * 900
    record = replace(
        record, expected_projection=replace(record.expected_projection, unit_hashes=large_hashes)
    )
    source = replace(
        checkpoint(record.source.identity),
        unit_hashes=large_hashes,
        tool_link_hashes=(OTHER_HASH,) * 1000,
    )
    target_identity = identity(tmp_path / "target", "n" * 128)
    target = replace(source, identity=target_identity, artifact_sha256=EXPORT_HASH)
    # Maximum path reference content is counted, not trimmed. Keep the receipt
    # just below its independent 32KiB cap; bodies are never used as padding.
    refs = tuple(
        EvidenceReference("manifest", "verified", "available", "/" + "p" * 1750 + str(index), HASH)
        for index in range(16)
    )
    done = replace(
        completion(tmp_path, record),
        source_checkpoint=source,
        target_checkpoint=target,
        references=refs,
    )
    assert 120_000 < len(_encode(source)) <= MAX_METADATA_BYTES
    assert 120_000 < len(_encode(target)) <= MAX_METADATA_BYTES
    cap = 650_000
    with LineageStore.create_new(tmp_path / "state", max_image_bytes=cap) as store:
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            before_size = (tmp_path / "state" / "ledger.sqlite3").stat().st_size
            assert cap - before_size - INITIAL_CLOSE_BUDGET < 20_000
            store.begin_publication(record.operation_id, guard)
            store.mark_verifying(record.operation_id, guard)
            final = store.complete(record.operation_id, done, guard)
            assert 30_000 < len(_encode(final, MAX_RECEIPT_BYTES)) <= MAX_RECEIPT_BYTES
        image_size = (tmp_path / "state" / "ledger.sqlite3").stat().st_size
        assert image_size <= cap
        assert image_size - before_size < MIN_CLOSE_BUDGET
        assert store.recovery_view().operations[0].reserved_bytes == 0


def test_foreign_key_and_noncanonical_record_corruption_fail_content_safe(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
    path = tmp_path / "state" / "ledger.sqlite3"

    def corrupt(connection):
        original = connection.execute("SELECT record_json FROM operations").fetchone()[0]
        connection.execute(
            "UPDATE operations SET record_json=?",
            (json.dumps(json.loads(original), indent=2).encode(),),
        )

    rewrite_image(path, corrupt)
    with pytest.raises(LineageError) as caught:
        LineageStore.open_existing(tmp_path / "state")
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("failure", ["file_fsync", "directory_fsync", "unlock"])
def test_failed_lock_creation_or_unlock_closes_all_owned_descriptors(
    tmp_path, monkeypatch, failure
):
    import fcntl

    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        original_open = os.open
        original_fsync = os.fsync
        original_flock = fcntl.flock
        owned = []

        def opened(name, *args, **kwargs):
            descriptor = original_open(name, *args, **kwargs)
            if type(name) is str and name.endswith(".lock") and name != "main.lock":
                owned.append(descriptor)
            return descriptor

        def sync(descriptor):
            if (failure == "file_fsync" and descriptor in owned) or (
                failure == "directory_fsync" and descriptor == store._locks_fd
            ):
                raise OSError("PRIVATE_OS_ERROR_SENTINEL")
            return original_fsync(descriptor)

        def flock(descriptor, mode):
            if failure == "unlock" and mode == fcntl.LOCK_UN and descriptor in owned:
                raise OSError("PRIVATE_UNLOCK_ERROR_SENTINEL")
            return original_flock(descriptor, mode)

        monkeypatch.setattr(os, "open", opened)
        monkeypatch.setattr(os, "fsync", sync)
        monkeypatch.setattr(fcntl, "flock", flock)
        with (
            pytest.raises(LineageError) as caught,
            store.lock_operation(record.operation_id, record.target_namespace),
        ):
            pass
        assert "SENTINEL" not in str(caught.value)
        assert owned
        for descriptor in owned:
            with pytest.raises(OSError):
                os.fstat(descriptor)


def test_post_io_guard_reloads_store_and_never_accepts_unknown_state(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            store.check_guard(record.operation_id, guard, expected_state=OperationState.READY)
            with pytest.raises(LineageError, match="invalid_guard"):
                store.check_guard(
                    record.operation_id, guard, expected_state=OperationState.PUBLISHING
                )
            store.begin_publication(record.operation_id, guard)
            store.check_guard(record.operation_id, guard, expected_state=OperationState.PUBLISHING)


def test_ready_rejects_receipts_that_could_not_be_persisted_on_failure(tmp_path):
    refs = tuple(
        EvidenceReference("manifest", "planned", "unknown", "/" + "p" * 4000 + str(index), HASH)
        for index in range(16)
    )
    with LineageStore.create_new(tmp_path / "state") as store:
        record = replace(operation(tmp_path), references=refs)
        with (
            store.lock_operation(record.operation_id, record.target_namespace) as guard,
            pytest.raises(LineageError, match="metadata_limit"),
        ):
            store.ready(record, guard)
        assert store.recovery_view().operations == ()


def test_nonstandard_sqlite_page_size_cannot_expand_finalize_budget(tmp_path):
    with LineageStore.create_new(tmp_path / "state"):
        pass

    from session_migrate.lineage import _SCHEMA, APPLICATION_ID

    path = tmp_path / "state" / "ledger.sqlite3"
    old = sqlite3.connect(":memory:")
    old.deserialize(path.read_bytes())
    metadata = old.execute("SELECT * FROM metadata").fetchone()
    old.close()
    alternate = sqlite3.connect(":memory:")
    alternate.execute("PRAGMA page_size=65536")
    for statement in _SCHEMA:
        alternate.execute(statement)
    alternate.execute(f"PRAGMA application_id={APPLICATION_ID}")
    alternate.execute("PRAGMA user_version=1")
    alternate.execute("INSERT INTO metadata VALUES (?,?,?,?,?)", metadata)
    alternate.commit()
    assert alternate.execute("PRAGMA page_size").fetchone() == (65536,)
    path.write_bytes(alternate.serialize())
    alternate.close()
    with pytest.raises(LineageError, match="unknown_schema"):
        LineageStore.open_existing(tmp_path / "state")


def test_main_file_swap_before_rename_and_lock_directory_swap_fail_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-unchanged")
    outside.chmod(0o600)
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)

            def swap(event):
                if event == "before_rename":
                    image = tmp_path / "state" / "ledger.sqlite3"
                    image.rename(tmp_path / "original-image")
                    image.symlink_to(outside)

            store._fault_hook = swap
            with pytest.raises(LineageError):
                store.begin_publication(record.operation_id, guard)
    assert outside.read_bytes() == b"outside-unchanged"
    with LineageStore.create_new(tmp_path / "second") as store:
        (tmp_path / "second" / "locks").rename(tmp_path / "old-locks")
        (tmp_path / "second" / "locks").mkdir(mode=0o700)
        with pytest.raises(LineageError, match="namespace_changed"):
            store.recovery_view()


def test_temporary_replacement_preserves_competitor_file(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
            replaced = []

            def swap(event):
                if event == "after_temp_fsync":
                    temporary = next((tmp_path / "state").glob(".ledger-*.tmp"))
                    temporary.rename(tmp_path / "owned-original-temp")
                    temporary.write_bytes(b"competing-writer")
                    temporary.chmod(0o600)
                    replaced.append(temporary)

            store._fault_hook = swap
            with pytest.raises(LineageError, match="namespace_changed"):
                store.begin_publication(record.operation_id, guard)
            assert replaced[0].read_bytes() == b"competing-writer"


def test_symlink_ancestor_is_rejected_without_external_writes(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(LineageError):
        LineageStore.create_new(alias / "state")
    assert list(outside.iterdir()) == []


def test_sqlite_foreign_key_corruption_is_rejected(tmp_path):
    with LineageStore.create_new(tmp_path / "state"):
        pass

    def inject(connection):
        connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?)",
            ("absent-operation", "source", _encode(checkpoint(identity(tmp_path / "source")))),
        )

    rewrite_image(tmp_path / "state" / "ledger.sqlite3", inject)
    with pytest.raises(LineageError):
        LineageStore.open_existing(tmp_path / "state")


def test_checkpoint_size_is_rejected_while_constructing_metadata(tmp_path):
    with pytest.raises(LineageError, match="metadata_limit"):
        replace(
            checkpoint(identity(tmp_path / "source")),
            unit_hashes=(HASH,) * 1024,
            tool_link_hashes=(HASH,) * 1024,
        )


def test_error_codes_never_echo_caller_text():
    error = LineageError("UNTRUSTED_BODY_ERROR_SENTINEL")
    assert error.code == "ledger_failure"
    assert "SENTINEL" not in str(error)


def test_recovered_ready_or_failed_intent_never_recreates_publication_claim(tmp_path):
    record = operation(tmp_path)
    with LineageStore.create_new(tmp_path / "state") as store:
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            store.ready(record, guard)
        with (
            store.lock_operation(record.operation_id, record.target_namespace) as new_guard,
            pytest.raises(LineageError, match="invalid_transition"),
        ):
            store.begin_publication(record.operation_id, new_guard)
    with LineageStore.open_existing(tmp_path / "state") as store:
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            with pytest.raises(LineageError, match="invalid_transition"):
                store.begin_publication(record.operation_id, guard)
            store.cancel_ready(record.operation_id, guard)
        fresh = operation(tmp_path, "fresh-operation")
        with store.lock_operation(fresh.operation_id, fresh.target_namespace) as guard:
            store.ready(fresh, guard)

            def fail(event):
                if event == "after_temp_fsync":
                    raise OSError("bounded-local-intent-failure")

            store._fault_hook = fail
            with pytest.raises(LineageError):
                store.begin_publication(fresh.operation_id, guard)
            store._fault_hook = None
            entry = next(
                entry
                for entry in store.recovery_view().operations
                if entry.record.operation_id == fresh.operation_id
            )
            assert entry.durable_state is OperationState.READY
            with pytest.raises(LineageError, match="invalid_transition"):
                store.begin_publication(fresh.operation_id, guard)
            with pytest.raises(LineageError, match="invalid_guard"):
                store.mark_attempted(fresh.operation_id, guard)


@pytest.mark.parametrize("boundary", ["after_temp_fsync", "after_rename"])
def test_failed_ready_does_not_create_a_live_publication_claim(tmp_path, boundary):
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:

            def fail(event):
                if event == boundary:
                    raise OSError("synthetic-ready-failure")

            store._fault_hook = fail
            with pytest.raises(LineageError):
                store.ready(record, guard)
            store._fault_hook = None
            with pytest.raises(LineageError, match="invalid_transition"):
                store.begin_publication(record.operation_id, guard)
            if boundary == "after_rename":
                assert store.recovery_view().operations[0].durable_state is OperationState.READY
            else:
                assert store.recovery_view().operations == ()


def test_body_request_is_never_persisted_or_echoed(tmp_path, capsys):
    sentinel = "BODY_REQUEST_SENTINEL"
    with LineageStore.create_new(tmp_path / "state") as store:
        record = operation(tmp_path)
        with store.lock_operation(record.operation_id, record.target_namespace) as guard:
            with pytest.raises(LineageError) as caught:
                store.ready({"operation_id": record.operation_id, "request": sentinel}, guard)
            assert sentinel not in str(caught.value)
        assert store.recovery_view().operations == ()
        assert sentinel.encode() not in (tmp_path / "state" / "ledger.sqlite3").read_bytes()
    assert capsys.readouterr() == ("", "")


def test_multiple_return_edges_keep_independent_observations(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        source_identity = identity(tmp_path / "a", "initial-session")
        source_artifact = HASH
        source_snapshot = OTHER_HASH
        for index in range(6):
            from session_migrate.lineage import CompletionRecord, SourceMetadata

            target_root = tmp_path / ("b" if index % 2 == 0 else "a")
            target_identity = identity(target_root, f"return-{index}")
            target_snapshot = ("e" if index % 2 == 0 else "f") * 64
            record = replace(
                operation(tmp_path, f"operation-{index}", f"return-{index}"),
                source=SourceMetadata(source_identity, source_artifact, HASH, source_snapshot),
                target_namespace=target_identity.namespace,
                expected_projection=replace(
                    operation(tmp_path).expected_projection,
                    snapshot_sha256=target_snapshot,
                    unit_hashes=(target_snapshot,),
                ),
            )
            source = replace(
                checkpoint(source_identity),
                artifact_sha256=source_artifact,
                snapshot_sha256=source_snapshot,
                unit_hashes=(source_snapshot,),
            )
            target = replace(
                checkpoint(target_identity),
                artifact_sha256=EXPORT_HASH,
                snapshot_sha256=target_snapshot,
                unit_hashes=(target_snapshot,),
            )
            done = CompletionRecord(target_identity, INPUT_HASH, source, target, ())
            with store.lock_operation(record.operation_id, record.target_namespace) as guard:
                store.ready(record, guard)
                store.begin_publication(record.operation_id, guard)
                store.mark_verifying(record.operation_id, guard)
                store.complete(record.operation_id, done, guard)
            source_identity, source_artifact, source_snapshot = (
                target_identity,
                EXPORT_HASH,
                target_snapshot,
            )
        view = store.recovery_view()
        assert len(view.edges) == len(view.operations) == 6
        for index, entry in enumerate(view.operations):
            assert entry.receipt.continuity_reason == "tracked"
            assert (
                entry.source_checkpoint.snapshot_sha256 != entry.target_checkpoint.snapshot_sha256
            )
            if index:
                assert (
                    entry.record.source.identity
                    == view.operations[index - 1].receipt.target_identity
                )
