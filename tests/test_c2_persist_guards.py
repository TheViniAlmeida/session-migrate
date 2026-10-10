"""Cross-store guard checks around atomic sidecar image publication."""

import errno
import hashlib
import os

import pytest
from test_c2_stores import baseline

from session_migrate.baselines import BaselineStore
from session_migrate.lineage import LineageError, LineageStore

PRE_RENAME = (
    "before_temp_write",
    "after_temp_write",
    "after_temp_fsync",
    "before_rename",
)
POST_RENAME = ("after_rename", "after_directory_fsync")
MUTATIONS = ("root", "uuid", "main", "operation", "source", "target")


def change_coordinator(tmp_path, coordinator, record, mutation):
    path = tmp_path / "coordinator"
    if mutation == "root":
        path.rename(tmp_path / "original-coordinator")
        with LineageStore.create_new(path):
            pass
        return
    if mutation == "uuid":
        os.pwrite(coordinator._main_fd, ("f" * 32 + "\n").encode("ascii"), 0)
        os.fsync(coordinator._main_fd)
        return
    if mutation == "main":
        name = path / "main.lock"
    else:
        key = (
            hashlib.sha256(
                b"session-migrate/operation/v1\0" + record.baseline_id.encode("ascii")
            ).hexdigest()
            if mutation == "operation"
            else record.source_checkpoint.identity.namespace.lock_key
            if mutation == "source"
            else record.target_checkpoint.identity.namespace.lock_key
        )
        name = path / "locks" / (key + ".lock")
    content = name.read_bytes()
    name.rename(tmp_path / ("original-" + mutation + "-lock"))
    name.write_bytes(content)
    name.chmod(0o600)


@pytest.mark.parametrize("boundary", PRE_RENAME + POST_RENAME)
@pytest.mark.parametrize("mutation", MUTATIONS)
def test_append_rechecks_live_coordinator_uuid_fd_names_and_both_namespace_locks(
    tmp_path, boundary, mutation
):
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
            image = tmp_path / "baseline" / "ledger.sqlite3"
            previous = image.read_bytes(), image.stat().st_ino
            changed = False

            def attack(event):
                nonlocal changed
                if event == boundary and not changed:
                    changed = True
                    change_coordinator(tmp_path, coordinator, record, mutation)

            store._fault_hook = attack
            with pytest.raises(LineageError) as caught:
                store.append(record, coordinator=coordinator, guard=guard)
            store._fault_hook = None
            assert changed
            persisted = boundary in POST_RENAME
            if persisted:
                assert caught.value.code == "persistence_uncertain"
                assert image.read_bytes() != previous[0]
            else:
                assert caught.value.code in ("namespace_changed", "lock_binding")
                assert (image.read_bytes(), image.stat().st_ino) == previous
            observed = BaselineStore.inspect_existing(
                tmp_path / "baseline", coordinator_store_uuid=record.coordinator_store_uuid
            )
            assert observed.baselines == ((record,) if persisted else ())
            assert not list((tmp_path / "baseline").glob(".ledger-*.tmp"))
            assert guard._ready_claim is None and not guard._publication_claim
        assert not guard._active


@pytest.mark.parametrize("boundary", PRE_RENAME + POST_RENAME)
@pytest.mark.parametrize("mutation", ("root", "uuid"))
def test_initial_sidecar_creation_has_the_same_coordinator_publish_guards(
    tmp_path, boundary, mutation, monkeypatch
):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        changed = False

        def attack(store, event):
            nonlocal changed
            if event == boundary and not changed:
                changed = True
                change_coordinator(tmp_path, coordinator, record, mutation)

        with coordinator.lock_operation(
            record.baseline_id,
            record.target_checkpoint.identity.namespace,
            additional_namespaces=(record.source_checkpoint.identity.namespace,),
        ) as guard:
            monkeypatch.setattr(BaselineStore, "_event", attack)
            with pytest.raises(LineageError) as caught:
                BaselineStore.create_new(
                    tmp_path / "baseline", coordinator=coordinator, guard=guard
                )
            assert changed
            image = tmp_path / "baseline" / "ledger.sqlite3"
            if boundary in POST_RENAME:
                assert caught.value.code == "persistence_uncertain" and image.exists()
                view = BaselineStore.inspect_existing(tmp_path / "baseline")
                assert view.baselines == ()
                assert view.coordinator_store_uuid == record.coordinator_store_uuid
            else:
                assert caught.value.code in ("namespace_changed", "lock_binding")
                assert not image.exists()
                with pytest.raises(LineageError):
                    BaselineStore.inspect_existing(tmp_path / "baseline")
            assert not list((tmp_path / "baseline").glob(".ledger-*.tmp"))
            assert guard._ready_claim is None and not guard._publication_claim
        assert not guard._active


@pytest.mark.parametrize(
    "io_boundary", ("open", "write", "file_fsync", "rename", "directory_fsync")
)
def test_external_guard_rechecks_immediately_after_actual_staging_io(
    tmp_path, io_boundary, monkeypatch
):
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
            image = tmp_path / "baseline" / "ledger.sqlite3"
            previous = image.read_bytes(), image.stat().st_ino
            temp_fd = None
            changed = False
            original_open, original_write = os.open, os.write
            original_fsync, original_replace = os.fsync, os.replace

            def attack(boundary):
                nonlocal changed
                if io_boundary == boundary and not changed:
                    changed = True
                    os.pwrite(coordinator._main_fd, ("f" * 32 + "\n").encode("ascii"), 0)
                    original_fsync(coordinator._main_fd)

            def opened(path, *args, **kwargs):
                nonlocal temp_fd
                descriptor = original_open(path, *args, **kwargs)
                if str(path).startswith(".ledger-") and kwargs.get("dir_fd") == store._root_fd:
                    temp_fd = descriptor
                    attack("open")
                return descriptor

            def written(descriptor, content):
                result = original_write(descriptor, content)
                if descriptor == temp_fd:
                    attack("write")
                return result

            def synced(descriptor):
                result = original_fsync(descriptor)
                if descriptor == temp_fd:
                    attack("file_fsync")
                elif descriptor == store._root_fd:
                    attack("directory_fsync")
                return result

            def replaced(*args, **kwargs):
                result = original_replace(*args, **kwargs)
                attack("rename")
                return result

            with monkeypatch.context() as patch:
                patch.setattr(os, "open", opened)
                patch.setattr(os, "write", written)
                patch.setattr(os, "fsync", synced)
                patch.setattr(os, "replace", replaced)
                with pytest.raises(LineageError) as caught:
                    store.append(record, coordinator=coordinator, guard=guard)
            assert changed
            persisted = io_boundary in ("rename", "directory_fsync")
            assert caught.value.code == ("persistence_uncertain" if persisted else "lock_binding")
            if not persisted:
                assert (image.read_bytes(), image.stat().st_ino) == previous
            assert BaselineStore.inspect_existing(tmp_path / "baseline").baselines == (
                (record,) if persisted else ()
            )


def test_guard_failure_after_persist_before_context_exit_reports_uncertainty(tmp_path, monkeypatch):
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
            original = store._persist

            def changed_after_persist(*args, **kwargs):
                original(*args, **kwargs)
                change_coordinator(tmp_path, coordinator, record, "uuid")

            monkeypatch.setattr(store, "_persist", changed_after_persist)
            with pytest.raises(LineageError, match="persistence_uncertain"):
                store.append(record, coordinator=coordinator, guard=guard)
            assert BaselineStore.inspect_existing(tmp_path / "baseline").baselines == (record,)


@pytest.mark.parametrize("mutation", ("uuid", "baseline_root"))
def test_initial_creation_failure_after_persist_reports_uncertainty(
    tmp_path, mutation, monkeypatch
):
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with coordinator.lock_operation(
            record.baseline_id,
            record.target_checkpoint.identity.namespace,
            additional_namespaces=(record.source_checkpoint.identity.namespace,),
        ) as guard:
            original = BaselineStore._persist

            def changed_after_persist(store, *args, **kwargs):
                original(store, *args, **kwargs)
                if mutation == "uuid":
                    change_coordinator(tmp_path, coordinator, record, "uuid")
                else:
                    (tmp_path / "baseline").rename(tmp_path / "relocated-baseline")

            monkeypatch.setattr(BaselineStore, "_persist", changed_after_persist)
            with pytest.raises(LineageError, match="persistence_uncertain"):
                BaselineStore.create_new(
                    tmp_path / "baseline", coordinator=coordinator, guard=guard
                )
            path = tmp_path / ("relocated-baseline" if mutation == "baseline_root" else "baseline")
            assert BaselineStore.inspect_existing(path).baselines == ()


def test_coordinator_context_exit_guard_failure_after_append_reports_uncertainty(
    tmp_path, monkeypatch
):
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
            persisted = False
            checks_after_persist = 0
            original_persist, original_check = store._persist, coordinator._check

            def finished(*args, **kwargs):
                nonlocal persisted
                original_persist(*args, **kwargs)
                persisted = True

            def checked():
                nonlocal checks_after_persist
                if persisted:
                    checks_after_persist += 1
                    if checks_after_persist == 2:
                        change_coordinator(tmp_path, coordinator, record, "root")
                original_check()

            monkeypatch.setattr(store, "_persist", finished)
            monkeypatch.setattr(coordinator, "_check", checked)
            with pytest.raises(LineageError, match="persistence_uncertain"):
                store.append(record, coordinator=coordinator, guard=guard)
            assert checks_after_persist == 2
            assert BaselineStore.inspect_existing(tmp_path / "baseline").baselines == (record,)


@pytest.mark.parametrize("create", (True, False))
@pytest.mark.parametrize("failure", ("namespace", "unexpected"))
def test_coordinated_open_closes_all_unreturned_fds_after_context_exit_error(
    tmp_path, create, failure, monkeypatch
):
    path = tmp_path / "baseline"
    with LineageStore.create_new(tmp_path / "coordinator") as coordinator:
        record = baseline(tmp_path, coordinator)
        with coordinator.lock_operation(
            record.baseline_id,
            record.target_checkpoint.identity.namespace,
            additional_namespaces=(record.source_checkpoint.identity.namespace,),
        ) as guard:
            previous = None
            if not create:
                with BaselineStore.create_new(path, coordinator=coordinator, guard=guard) as seed:
                    seed.append(record, coordinator=coordinator, guard=guard)
                image = path / "ledger.sqlite3"
                previous = image.read_bytes(), image.stat().st_ino
            opened_stores = []
            descriptors = []
            checks_after_open = 0
            original_open, original_check = BaselineStore._open, coordinator._check

            def opened(cls, *args, **kwargs):
                store = original_open(*args, **kwargs)
                opened_stores.append(store)
                descriptors.extend((store._main_fd, store._locks_fd))
                descriptors.extend(fd for fd, _, _ in store._namespace.chain)
                return store

            def checked():
                nonlocal checks_after_open
                if opened_stores:
                    checks_after_open += 1
                    if checks_after_open == 2:
                        if failure == "namespace":
                            change_coordinator(tmp_path, coordinator, record, "root")
                        else:
                            raise RuntimeError("SYNTHETIC_EXIT_FAILURE")
                original_check()

            with monkeypatch.context() as patch:
                patch.setattr(BaselineStore, "_open", classmethod(opened))
                patch.setattr(coordinator, "_check", checked)
                expected_error = LineageError if create or failure == "namespace" else RuntimeError
                with pytest.raises(expected_error) as caught:
                    method = BaselineStore.create_new if create else BaselineStore.open_existing
                    method(path, coordinator=coordinator, guard=guard)
            assert checks_after_open == 2
            assert len(opened_stores) == 1 and opened_stores[0]._closed
            assert not opened_stores[0]._namespace.chain
            for descriptor in descriptors:
                with pytest.raises(OSError) as closed:
                    os.fstat(descriptor)
                assert closed.value.errno == errno.EBADF
            if create:
                assert caught.value.code == "persistence_uncertain"
            elif failure == "namespace":
                assert caught.value.code == "namespace_changed"
            else:
                assert str(caught.value) == "SYNTHETIC_EXIT_FAILURE"
            image = path / "ledger.sqlite3"
            if previous is not None:
                assert (image.read_bytes(), image.stat().st_ino) == previous
            view = BaselineStore.inspect_existing(path)
            assert view.baselines == (() if create else (record,))
            assert view.coordinator_store_uuid == record.coordinator_store_uuid
