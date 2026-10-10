"""Real B3/C1 authority compared with fresh physical catalog snapshots."""

import os
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
import test_c2_services as fixtures

from session_migrate import reconciliation
from session_migrate import recovered_projections as raw
from session_migrate.catalog import Catalog
from session_migrate.continuity_bindings import LegacyRuntimeContext
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError
from session_migrate.formats import claude
from session_migrate.lineage import LineageStore, NativeState, OperationState
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_source
from session_migrate.operations import (
    OperationKind,
    OperationRequest,
    commit_operation,
    prepare_operation,
)
from session_migrate.profiles import Profile


def memory_copy(connection):
    copied = sqlite3.connect(":memory:")
    copied.row_factory = sqlite3.Row
    copied.create_function("session_casefold", 1, str.casefold, deterministic=True)
    connection.backup(copied)
    return copied


def projection(data, *, observer=None):
    _, path, context, actual_observer, _ = data
    observer = observer if observer is not None else actual_observer
    record = LineageStore.inspect_existing(path).operations[0].record
    source = capture_source(
        SourceCaptureContext(context.source_profile.format, context.source_path)
    )
    artifact = capture_source(
        SourceCaptureContext(context.target_profile.format, context.input_path)
    )
    issued = raw._prepare_recovery_context(
        source,
        artifact,
        record,
        context.request,
        source_environment=context.source_environment,
        target_environment=context.target_environment,
        source_profile=context.source_profile,
        target_profile=context.target_profile,
        policy=context.policy,
        observer=observer,
    )
    return raw.prepare_recovered_projection(source, artifact, record, issued)


def ready(data):
    op, path, context, observer, _ = data
    result = reconciliation.prepare_reconciliation(
        op, store_path=path, context=context, observer=observer
    )
    assert result.status == "ready", result.reason
    return result


def change_physical_row(connection, context, tmp_path):
    connection.execute(
        "UPDATE sessions SET canonical_path=? WHERE catalog_id=?",
        (str(tmp_path / "changed-current-source.jsonl"), context.request.catalog_id),
    )
    connection.commit()


@pytest.mark.parametrize("change", ("row", "missing", "replacement"))
def test_current_physical_catalog_blocks_before_commit_with_a_stale_real_connection(
    tmp_path, monkeypatch, change
):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    _, path, context, _, catalog = data
    proposal = ready(data)
    previous = (path / "ledger.sqlite3").read_bytes()
    prior = LineageStore.inspect_existing(path).operations[0]
    original = catalog._connection
    stale = memory_copy(original)
    try:
        if change == "row":
            change_physical_row(original, context, tmp_path)
        elif change == "missing":
            catalog.path.rename(catalog.path.with_suffix(".retired"))
        else:
            replacement = catalog.path.with_suffix(".replacement")
            copied = sqlite3.connect(replacement)
            try:
                original.backup(copied)
                change_physical_row(copied, context, tmp_path)
            finally:
                copied.close()
            replacement.chmod(0o600)
            os.replace(replacement, catalog.path)
        catalog._connection = stale
        fixtures.no_native(monkeypatch)
        with pytest.raises(reconciliation.ReconciliationError):
            reconciliation.commit_reconciliation(proposal.handle)
        reopened = LineageStore.inspect_existing(path).operations[0]
        assert (path / "ledger.sqlite3").read_bytes() == previous
        assert reopened.durable_state is OperationState.NEEDS_RECONCILE
        assert reopened.durable_native_state is NativeState.ACKNOWLEDGED
        assert reopened.reserved_bytes == prior.reserved_bytes
    finally:
        catalog._connection = original
        stale.close()
        catalog.close()


@pytest.mark.parametrize("boundary", ("before_temp_write", "before_rename", "after_rename"))
def test_physical_catalog_change_during_persistence_cannot_use_a_cached_old_row(
    tmp_path, monkeypatch, boundary
):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    op, path, context, _, catalog = data
    proposal = ready(data)
    previous = (path / "ledger.sqlite3").read_bytes()
    original = catalog._connection
    stale = memory_copy(original)
    complete = LineageStore.complete_reconciled
    attacked = False

    def wrapped(store, *args, **kwargs):
        def fault(current):
            nonlocal attacked
            if current == boundary and not attacked:
                attacked = True
                change_physical_row(original, context, tmp_path)
                catalog._connection = stale

        store._fault_hook = fault
        return complete(store, *args, **kwargs)

    monkeypatch.setattr(LineageStore, "complete_reconciled", wrapped)
    fixtures.no_native(monkeypatch)
    try:
        with pytest.raises(reconciliation.ReconciliationError) as caught:
            reconciliation.commit_reconciliation(proposal.handle)
        assert attacked and caught.value.operation_id == op
        reopened = LineageStore.inspect_existing(path).operations[0]
        if boundary == "after_rename":
            assert caught.value.code == "persistence_uncertain"
            assert reopened.durable_state is OperationState.COMPLETED
        else:
            assert (path / "ledger.sqlite3").read_bytes() == previous
            assert reopened.durable_state is OperationState.NEEDS_RECONCILE
        assert reopened.durable_native_state is NativeState.ACKNOWLEDGED
    finally:
        catalog._connection = original
        stale.close()
        catalog.close()


@pytest.mark.parametrize("change", ("connection", "row", "missing", "replacement_identical"))
def test_issued_context_retains_its_original_connection_and_physical_revision(
    tmp_path, monkeypatch, change
):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    _, _, context, _, catalog = data
    prepared = projection(data)
    original = catalog._connection
    stale = memory_copy(original)
    try:
        if change == "connection":
            catalog._connection = stale
        elif change == "row":
            change_physical_row(original, context, tmp_path)
        elif change == "missing":
            catalog.path.rename(catalog.path.with_suffix(".retired"))
        else:
            replacement = catalog.path.with_suffix(".replacement")
            replacement.write_bytes(catalog.path.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, catalog.path)
        with pytest.raises(SessionMigrateError):
            raw.validate_recovered_projection(prepared)
        with pytest.raises(SessionMigrateError):
            raw.prepare_recovered_projection(
                prepared._context._source,
                prepared._context._input,
                prepared._context._record,
                prepared._context,
            )
    finally:
        catalog._connection = original
        stale.close()
        catalog.close()


def test_legitimate_readonly_observer_snapshot_has_no_sqlite_disk_filename_requirement(
    tmp_path, monkeypatch
):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    _, _, context, observer, catalog = data
    with Catalog.open_readonly_existing(catalog.path) as snapshot:
        assert snapshot._connection.execute("PRAGMA database_list").fetchone()[2] == ""
        current = EndpointObserver(
            snapshot,
            observer._profiles,
            base_environ=observer._environment,
            legacy_runtime_contexts=observer._legacy_runtime_contexts,
        )
        prepared = projection(data, observer=current)
        raw.validate_recovered_projection(prepared)
        assert prepared._context._request is context.request
        assert prepared._context._request.catalog_id == context.request.catalog_id
        assert prepared._context._request.source_id is None
    catalog.close()


@pytest.mark.parametrize("side", ("source", "input"))
def test_full_native_bytes_are_rechecked_after_new_catalog_io(tmp_path, monkeypatch, side):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    context = data[2]
    prepared = projection(data)
    changed = context.source_path if side == "source" else context.input_path
    original = Catalog.open_readonly_existing
    attacked = False

    def opened(cls, *args, **kwargs):
        nonlocal attacked
        snapshot = original(*args, **kwargs)
        if not attacked:
            attacked = True
            before = changed.stat()
            body = changed.read_bytes()
            mutated = body.replace(b"synthetic answer", b"synthetic broken")
            assert mutated != body and len(mutated) == len(body)
            changed.write_bytes(mutated)
            os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns))
        return snapshot

    monkeypatch.setattr(Catalog, "open_readonly_existing", classmethod(opened))
    with pytest.raises(SessionMigrateError):
        raw.validate_recovered_projection(prepared)
    assert attacked
    data[-1].close()


@pytest.mark.parametrize("native_source", (False, True))
def test_legacy_request_without_catalog_selection_adds_no_physical_catalog_queries(
    tmp_path, monkeypatch, native_source
):
    data = fixtures.recovery_fixture(tmp_path, monkeypatch, native_source=native_source)

    def unavailable(*args, **kwargs):
        pytest.fail("legacy request without catalog selection opened a physical snapshot")

    monkeypatch.setattr(Catalog, "open_readonly_existing", unavailable)
    proposal = ready(data)
    proposal.handle.discard()
    data[-1].close()


@pytest.mark.parametrize("selection", ("valid", "unknown", "ambiguous"))
def test_title_selector_uses_current_physical_resolution_with_a_cached_old_index(
    tmp_path, monkeypatch, selection
):
    env = fixtures.environment(tmp_path)
    roots = (tmp_path / "source", tmp_path / "target")
    source = (
        roots[0]
        / "projects"
        / claude.project_directory_name(Path("/synthetic/work"))
        / (fixtures.SOURCE_ID + ".jsonl")
    )
    title = "synthetic selected catalog title"
    values = fixtures.records() + [
        {
            "type": "custom-title",
            "sessionId": fixtures.SOURCE_ID,
            "customTitle": title,
        }
    ]
    fixtures.write(source, values)
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(claude_roots=roots[:1], include_auto=False)
    prepared = prepare_operation(
        OperationRequest(
            OperationKind.TRANSFER,
            catalog=catalog.path,
            title=title,
            source_format=AgentFormat.CLAUDE,
            target_format=TargetFormat.CODEX,
            home=roots[1],
            session_id=fixtures.TARGET_ID,
            target_cli_version="0.144.4",
            cwd=Path("/synthetic/work"),
        ),
        environ=env,
    )
    state = prepared._state
    context = reconciliation.RecoveryContext(
        state.built.request,
        source,
        state.built.output,
        state.source_environment,
        state.target_environment,
        manifest_path=state.built.manifest,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            fixtures.operations,
            "_completion_record",
            lambda *args: (_ for _ in ()).throw(SessionMigrateError("synthetic interruption")),
        )
        with pytest.raises(OperationNeedsReconcileError) as interrupted:
            commit_operation(prepared)
    catalog.refresh(claude_roots=roots[:1], codex_roots=roots[1:], include_auto=False)
    profiles = (
        Profile("source", "Source", AgentFormat.CLAUDE, roots[0]),
        Profile("target", "Target", AgentFormat.CODEX, roots[1]),
    )
    observer = EndpointObserver(
        catalog,
        profiles,
        base_environ=env,
        legacy_runtime_contexts=tuple(LegacyRuntimeContext(p.id, env) for p in profiles),
    )
    data = (
        interrupted.value.receipt.operation_id,
        default_migration_state_home(environ=env) / "lineage-v1",
        context,
        observer,
        catalog,
    )
    proposal = ready(data)
    op, path, _, _, _ = data
    original = catalog._connection
    stale = memory_copy(original)
    previous = (path / "ledger.sqlite3").read_bytes()
    record = LineageStore.inspect_existing(path).operations[0].record
    try:
        if selection == "unknown":
            original.execute(
                "UPDATE sessions SET display_title='changed title' WHERE format='claude'"
            )
            original.execute("UPDATE session_labels SET value='changed label'")
            original.commit()
        elif selection == "ambiguous":
            other_id = "40000000-0000-4000-8000-000000000002"
            changed = deepcopy(values)
            for row in changed:
                row["sessionId"] = other_id
            fixtures.write(source.with_name(other_id + ".jsonl"), changed)
            catalog.refresh(claude_roots=roots[:1], codex_roots=roots[1:], include_auto=False)
        catalog._connection = stale
        fixtures.no_native(monkeypatch)
        if selection == "valid":
            receipt = reconciliation.commit_reconciliation(proposal.handle)
            assert receipt.operation_id == op
            assert context.request.title == title
            assert context.request.catalog_id is context.request.source_id is None
            assert LineageStore.inspect_existing(path).operations[0].record == record
        else:
            with pytest.raises(reconciliation.ReconciliationError):
                reconciliation.commit_reconciliation(proposal.handle)
            blocked = reconciliation.prepare_reconciliation(
                op, store_path=path, context=context, observer=observer
            )
            assert blocked.reason == "context_unavailable" and blocked.handle is None
            assert (path / "ledger.sqlite3").read_bytes() == previous
    finally:
        catalog._connection = original
        stale.close()
        catalog.close()
