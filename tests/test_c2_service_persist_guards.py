"""Fresh service authority through actual catalog selection and staging I/O."""

import copy
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_c2_services as fixtures

from session_migrate import adoption, reconciliation, recovered_projections
from session_migrate.baselines import BaselineStore
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
    _digest,
    _public_options,
    commit_operation,
    prepare_operation,
)
from session_migrate.profiles import Profile

PRE = ("before_temp_write", "after_temp_write", "after_temp_fsync", "before_rename")
POST = ("after_rename", "after_directory_fsync")


def change_bytes(path):
    before = path.stat()
    body = path.read_bytes()
    changed = body.replace(b"synthetic answer", b"synthetic broken")
    assert changed != body and len(changed) == len(body)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def ready_recovery(data):
    op, store, context, observer, _ = data
    proposal = reconciliation.prepare_reconciliation(
        op, store_path=store, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    return proposal


@pytest.mark.parametrize("selection", ("valid", "unknown", "ambiguous"))
def test_actual_b3_title_selector_requires_a_current_unique_catalog_selection(
    tmp_path, monkeypatch, selection
):
    env = fixtures.environment(tmp_path)
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    source = (
        source_root
        / "projects"
        / claude.project_directory_name(Path("/synthetic/work"))
        / (fixtures.SOURCE_ID + ".jsonl")
    )
    title = "synthetic catalog selected title"
    values = fixtures.records() + [
        {
            "type": "custom-title",
            "sessionId": fixtures.SOURCE_ID,
            "customTitle": title,
        }
    ]
    fixtures.write(source, values)
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(claude_roots=(source_root,), include_auto=False)
    prepared = prepare_operation(
        OperationRequest(
            OperationKind.TRANSFER,
            catalog=catalog.path,
            title=title,
            source_format=AgentFormat.CLAUDE,
            target_format=TargetFormat.CODEX,
            home=target_root,
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
    path = default_migration_state_home(environ=env) / "lineage-v1"
    op = interrupted.value.receipt.operation_id
    profiles = (
        Profile("source", "Source", AgentFormat.CLAUDE, source_root),
        Profile("target", "Target", AgentFormat.CODEX, target_root),
    )
    if selection == "unknown":
        changed = copy.deepcopy(values)
        changed[-1]["customTitle"] = "changed title"
        fixtures.write(source, changed)
    elif selection == "ambiguous":
        other_id = "40000000-0000-4000-8000-000000000002"
        changed = copy.deepcopy(values)
        for row in changed:
            row["sessionId"] = other_id
        fixtures.write(source.with_name(other_id + ".jsonl"), changed)
    catalog.refresh(claude_roots=(source_root,), codex_roots=(target_root,), include_auto=False)
    observer = EndpointObserver(
        catalog,
        profiles,
        base_environ=env,
        legacy_runtime_contexts=tuple(LegacyRuntimeContext(p.id, env) for p in profiles),
    )
    before = (path / "ledger.sqlite3").read_bytes()
    fixtures.no_native(monkeypatch)
    proposal = reconciliation.prepare_reconciliation(
        op, store_path=path, context=context, observer=observer
    )
    assert (path / "ledger.sqlite3").read_bytes() == before
    if selection == "valid":
        assert proposal.status == "ready", proposal.reason
        receipt = reconciliation.commit_reconciliation(proposal.handle)
        assert receipt.operation_id == op
        assert context.request.title == title and context.request.catalog_id is None
        assert context.request.source_id is None
    else:
        assert proposal.reason == "context_unavailable" and proposal.handle is None
    catalog.close()


@pytest.mark.parametrize("source_format", (AgentFormat.CLAUDE, AgentFormat.CODEX))
@pytest.mark.parametrize("title", (False, True))
def test_actual_profile_api_keeps_original_catalog_selector_and_options(
    tmp_path, monkeypatch, source_format, title
):
    data = fixtures.profile_recovery_fixture(
        tmp_path, monkeypatch, source_format=source_format, title=title
    )
    op, path, context, observer, catalog = data
    request = context.request
    record = LineageStore.inspect_existing(path).operations[0].record
    assert request.catalog_id and request.catalog == catalog.path
    assert request.source_id is None and request.source_home is None
    profiles = recovered_projections._profiles(context.source_profile, context.target_profile)
    assert (
        _digest(
            _public_options(
                request, context.target_environment, context.source_environment, profiles
            )
        )
        == record.options_sha256
    )
    fixtures.no_native(monkeypatch)
    proposal = ready_recovery(data)
    receipt = reconciliation.commit_reconciliation(proposal.handle)
    assert receipt.operation_id == op
    reopened = LineageStore.inspect_existing(path).operations[0]
    assert reopened.durable_state is OperationState.COMPLETED
    assert reopened.durable_native_state is NativeState.ACKNOWLEDGED
    assert reopened.record == record and context.request is request
    assert request.source_id is None and request.catalog_id
    if title:
        assert any(ref.kind == "receipt" and ref.sha256 for ref in receipt.references)
    catalog.close()


@pytest.mark.parametrize(
    "change",
    (
        "catalog_id",
        "catalog",
        "profile",
        "closed_catalog",
        "missing_observer",
        "catalog_kind",
        "current_profile_kind",
    ),
)
def test_catalog_request_has_no_authority_without_verified_current_selection(
    tmp_path, monkeypatch, change
):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    op, path, context, observer, catalog = data
    before = (path / "ledger.sqlite3").read_bytes()
    if change == "catalog_id":
        context = replace(context, request=replace(context.request, catalog_id="unknown"))
    elif change == "catalog":
        context = replace(
            context, request=replace(context.request, catalog=tmp_path / "other.sqlite")
        )
    elif change == "profile":
        context = replace(context, source_profile=replace(context.source_profile, name="changed"))
    elif change == "closed_catalog":
        catalog.close()
    elif change == "catalog_kind":
        object.__setattr__(observer, "_catalog", SimpleNamespace(path=catalog.path))
    elif change == "current_profile_kind":
        profile = observer._profiles[0]
        fake = SimpleNamespace(fingerprint=profile.fingerprint)
        object.__setattr__(observer, "_profiles", (fake, observer._profiles[1]))
    else:
        observer = None
    result = reconciliation.prepare_reconciliation(
        op, store_path=path, context=context, observer=observer
    )
    assert result.status == "blocked" and result.handle is None
    assert (path / "ledger.sqlite3").read_bytes() == before
    catalog.close()


@pytest.mark.parametrize("change", ("clone", "row", "profile", "source", "root"))
def test_selected_raw_context_is_revalidated_on_every_factory_use(tmp_path, monkeypatch, change):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    _, path, context, observer, catalog = data
    record = LineageStore.inspect_existing(path).operations[0].record
    source = capture_source(
        SourceCaptureContext(context.source_profile.format, context.source_path)
    )
    artifact = capture_source(
        SourceCaptureContext(context.target_profile.format, context.input_path)
    )
    raw = recovered_projections._prepare_recovery_context(
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
    assert raw._selection is not None
    projected = recovered_projections.prepare_recovered_projection(source, artifact, record, raw)
    if change == "clone":
        object.__setattr__(raw, "_selection", copy.copy(raw._selection))
    elif change == "row":
        # An actual catalog row swap remains unavailable even with unchanged
        # source bytes; the captured path is not a replacement selector proof.
        catalog._connection.execute(
            "UPDATE sessions SET canonical_path=? WHERE catalog_id=?",
            (str(tmp_path / "different-source.jsonl"), context.request.catalog_id),
        )
        catalog._connection.commit()
    elif change == "profile":
        object.__setattr__(context.source_profile, "name", "changed")
    elif change == "source":
        change_bytes(context.source_path)
    else:
        context.source_profile.root.rename(tmp_path / "relocated-source")
        context.source_profile.root.mkdir(mode=0o700)
    with pytest.raises(SessionMigrateError):
        recovered_projections.prepare_recovered_projection(source, artifact, record, raw)
    with pytest.raises(SessionMigrateError):
        recovered_projections.validate_recovered_projection(projected)
    catalog.close()


@pytest.mark.parametrize("boundary", PRE + POST)
@pytest.mark.parametrize("what", ("source", "target", "input"))
def test_recovery_native_and_original_input_mutation_at_each_publish_boundary(
    tmp_path, monkeypatch, boundary, what
):
    data = fixtures.recovery_fixture(tmp_path, monkeypatch, native_source=True)
    op, path, context, _, catalog = data
    proposal = ready_recovery(data)
    before = (path / "ledger.sqlite3").read_bytes()
    prior = LineageStore.inspect_existing(path).operations[0]
    native_path = Path(
        next(
            ref.path
            for ref in reconciliation._HANDLES[proposal.handle].proof[0].references
            if ref.kind == "native"
        )
    )
    changed_path = {
        "source": context.source_path,
        "target": native_path,
        "input": context.input_path,
    }[what]
    called = False
    original = LineageStore.complete_reconciled

    def complete(store, *args, **kwargs):
        def fault(event):
            nonlocal called
            if event == boundary and not called:
                called = True
                change_bytes(changed_path)

        store._fault_hook = fault
        return original(store, *args, **kwargs)

    monkeypatch.setattr(LineageStore, "complete_reconciled", complete)
    with pytest.raises(reconciliation.ReconciliationError) as caught:
        reconciliation.commit_reconciliation(proposal.handle)
    assert called and caught.value.operation_id == op
    reopened = LineageStore.inspect_existing(path).operations[0]
    if boundary in PRE:
        assert (path / "ledger.sqlite3").read_bytes() == before
        assert reopened.durable_state is prior.durable_state
        assert reopened.reserved_bytes == prior.reserved_bytes
    else:
        assert caught.value.code == "persistence_uncertain"
        assert reopened.durable_state is OperationState.COMPLETED
        assert reconciliation.prepare_reconciliation(op, store_path=path).status == "completed"
    assert reopened.durable_native_state is NativeState.ACKNOWLEDGED
    assert not list(path.glob(".ledger-*.tmp"))
    catalog.close()


@pytest.mark.parametrize("boundary", ("before_rename", "after_rename"))
@pytest.mark.parametrize("what", ("source", "target", "root", "profile", "policy", "version"))
def test_adoption_freshness_during_append_preserves_prior_records(
    tmp_path, monkeypatch, boundary, what
):
    pair, observer, home, catalog, paths = fixtures.adoption_fixture(tmp_path)
    first = adoption.commit_adoption(fixtures.plan_adoption(pair, observer, home).handle)
    proposal = fixtures.plan_adoption(pair, observer, home)
    state = adoption._HANDLES[proposal.handle]
    before = (home / "baselines-v1/ledger.sqlite3").read_bytes()
    coordinator_before = (home / "lineage-v1/ledger.sqlite3").read_bytes()
    called = False
    original = BaselineStore.append
    mutated = None

    def append(store, *args, **kwargs):
        def fault(event):
            nonlocal called, mutated
            if event == boundary and not called:
                called = True
                if what in {"source", "target"}:
                    change_bytes(paths[what == "target"])
                elif what == "root":
                    pair.source.root.rename(tmp_path / "relocated-source")
                    pair.source.root.mkdir(mode=0o700)
                else:
                    obj, key, value = {
                        "profile": (observer._profiles[0], "name", "changed"),
                        "policy": (state.policy, "policy_id", "changed"),
                        "version": (state.proposal.versions, "source_client", "changed"),
                    }[what]
                    mutated = obj, key, getattr(obj, key)
                    object.__setattr__(obj, key, value)

        store._fault_hook = fault
        return original(store, *args, **kwargs)

    monkeypatch.setattr(BaselineStore, "append", append)
    try:
        with pytest.raises(adoption.AdoptionError) as caught:
            adoption.commit_adoption(proposal.handle)
    finally:
        if mutated:
            object.__setattr__(*mutated)
    assert called and caught.value.baseline_id == proposal.baseline_id
    assert (home / "lineage-v1/ledger.sqlite3").read_bytes() == coordinator_before
    assert adoption.inspect_adoption(first.baseline_id, state_home=home) == first
    latest = adoption.inspect_adoption(proposal.baseline_id, state_home=home)
    if boundary == "before_rename":
        assert latest is None
        assert (home / "baselines-v1/ledger.sqlite3").read_bytes() == before
    else:
        assert latest is not None and caught.value.code == "persistence_uncertain"
    catalog.close()


@pytest.mark.parametrize("service", ("recovery", "adoption"))
@pytest.mark.parametrize("boundary", ("before_rename", "after_rename"))
def test_actual_closed_coordinator_fd_cannot_silently_complete(
    tmp_path, monkeypatch, service, boundary
):
    if service == "recovery":
        data = fixtures.recovery_fixture(tmp_path, monkeypatch, native_source=True)
        proposal = ready_recovery(data)
        method = LineageStore.complete_reconciled
        commit = reconciliation.commit_reconciliation
        error = reconciliation.ReconciliationError
        path = data[1]
        catalog = data[-1]
    else:
        pair, observer, home, catalog, _ = fixtures.adoption_fixture(tmp_path)
        adoption.commit_adoption(fixtures.plan_adoption(pair, observer, home).handle)
        proposal = fixtures.plan_adoption(pair, observer, home)
        method = BaselineStore.append
        commit = adoption.commit_adoption
        error = adoption.AdoptionError
        path = home / "baselines-v1"
    before = (path / "ledger.sqlite3").read_bytes()
    attacked = False

    def wrapped(store, *args, **kwargs):
        def fault(event):
            nonlocal attacked
            if event == boundary and not attacked:
                attacked = True
                descriptor = (
                    store._main_fd if service == "recovery" else kwargs["coordinator"]._main_fd
                )
                os.close(descriptor)
                with pytest.raises(OSError):
                    os.fstat(descriptor)

        store._fault_hook = fault
        return method(store, *args, **kwargs)

    monkeypatch.setattr(
        LineageStore if service == "recovery" else BaselineStore,
        "complete_reconciled" if service == "recovery" else "append",
        wrapped,
    )
    with pytest.raises(error) as caught:
        commit(proposal.handle)
    assert attacked
    if boundary == "before_rename":
        assert (path / "ledger.sqlite3").read_bytes() == before
    else:
        assert caught.value.code == "persistence_uncertain"
    catalog.close()


@pytest.mark.parametrize("store_kind", ("bootstrap", "baseline_create"))
@pytest.mark.parametrize("boundary", ("before_rename", "after_rename"))
def test_initial_store_publication_also_uses_fresh_native_evidence(
    tmp_path, monkeypatch, store_kind, boundary
):
    pair, observer, home, catalog, paths = fixtures.adoption_fixture(tmp_path)
    proposal = fixtures.plan_adoption(pair, observer, home)
    original = LineageStore._event
    attacked = False

    def event(store, current):
        nonlocal attacked
        kind = "baseline_create" if type(store) is BaselineStore else "bootstrap"
        if current == boundary and kind == store_kind and not attacked:
            attacked = True
            change_bytes(paths[0])
        original(store, current)

    monkeypatch.setattr(LineageStore, "_event", event)
    with pytest.raises(adoption.AdoptionError) as caught:
        adoption.commit_adoption(proposal.handle)
    assert attacked and caught.value.baseline_id == proposal.baseline_id
    store_path = home / ("lineage-v1" if store_kind == "bootstrap" else "baselines-v1")
    if boundary == "before_rename":
        assert not (store_path / "ledger.sqlite3").exists()
    else:
        assert caught.value.code == "persistence_uncertain"
        view = (LineageStore if store_kind == "bootstrap" else BaselineStore).inspect_existing(
            store_path
        )
        assert not (view.operations if store_kind == "bootstrap" else view.baselines)
    if store_kind == "baseline_create":
        assert not LineageStore.inspect_existing(home / "lineage-v1").operations
    catalog.close()


@pytest.mark.parametrize("what", ("root", "profile", "options", "version", "coordinator_uuid"))
@pytest.mark.parametrize("boundary", ("before_rename", "after_rename"))
def test_recovery_rechecks_issued_context_and_current_profiles_during_persist(
    tmp_path, monkeypatch, what, boundary
):
    data = fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
    op, path, context, observer, catalog = data
    proposal = ready_recovery(data)
    state = reconciliation._HANDLES[proposal.handle]
    before = (path / "ledger.sqlite3").read_bytes()
    original = LineageStore.complete_reconciled
    attacked = False
    mutated = None
    changed_store = None

    def complete(store, *args, **kwargs):
        def fault(event):
            nonlocal attacked, mutated, changed_store
            if event == boundary and not attacked:
                attacked = True
                if what == "coordinator_uuid":
                    changed_store = store
                    os.pwrite(store._main_fd, ("f" * 32 + "\n").encode("ascii"), 0)
                    os.fsync(store._main_fd)
                elif what == "root":
                    observer._profiles[0].root.rename(tmp_path / "relocated-source")
                    observer._profiles[0].root.mkdir(mode=0o700)
                else:
                    obj, key, value = {
                        "profile": (context.source_profile, "name", "changed"),
                        "options": (context.request, "model_provider", "changed"),
                        "version": (state.operation.record.versions, "source_client", "changed"),
                    }[what]
                    mutated = obj, key, getattr(obj, key)
                    object.__setattr__(obj, key, value)

        store._fault_hook = fault
        return original(store, *args, **kwargs)

    monkeypatch.setattr(LineageStore, "complete_reconciled", complete)
    try:
        with pytest.raises(reconciliation.ReconciliationError) as caught:
            reconciliation.commit_reconciliation(proposal.handle)
    finally:
        if mutated:
            object.__setattr__(*mutated)
        if changed_store is not None:
            lock = path / "main.lock"
            lock.write_bytes((changed_store._store_uuid + "\n").encode("ascii"))
    assert attacked and caught.value.operation_id == op
    if boundary == "before_rename":
        assert (path / "ledger.sqlite3").read_bytes() == before
        assert (
            LineageStore.inspect_existing(path).operations[0].durable_state
            is OperationState.NEEDS_RECONCILE
        )
    else:
        assert caught.value.code == "persistence_uncertain"
        assert reconciliation.prepare_reconciliation(op, store_path=path).status == "completed"
    catalog.close()


@pytest.mark.parametrize("service", ("recovery", "adoption"))
@pytest.mark.parametrize("boundary", ("open", "write", "file_fsync", "rename", "directory_fsync"))
def test_fresh_native_guard_runs_after_actual_staging_io(tmp_path, monkeypatch, service, boundary):
    if service == "recovery":
        data = fixtures.recovery_fixture(tmp_path, monkeypatch, native_source=True)
        proposal = ready_recovery(data)
        path, source_path, catalog = data[1], data[2].source_path, data[-1]
        owner, name = LineageStore, "complete_reconciled"
        commit, error = reconciliation.commit_reconciliation, reconciliation.ReconciliationError
    else:
        pair, observer, home, catalog, paths = fixtures.adoption_fixture(tmp_path)
        adoption.commit_adoption(fixtures.plan_adoption(pair, observer, home).handle)
        proposal = fixtures.plan_adoption(pair, observer, home)
        path, source_path = home / "baselines-v1", paths[0]
        owner, name = BaselineStore, "append"
        commit, error = adoption.commit_adoption, adoption.AdoptionError
    before = (path / "ledger.sqlite3").read_bytes()
    original = getattr(owner, name)
    attacked = False

    def wrapped(store, *args, **kwargs):
        temp_fd = None
        open_fd, write_fd, fsync_fd, replace_file = os.open, os.write, os.fsync, os.replace

        def attack(current):
            nonlocal attacked
            if current == boundary and not attacked:
                attacked = True
                change_bytes(source_path)

        def opened(current, *rest, **options):
            nonlocal temp_fd
            descriptor = open_fd(current, *rest, **options)
            if str(current).startswith(".ledger-") and options.get("dir_fd") == store._root_fd:
                temp_fd = descriptor
                attack("open")
            return descriptor

        def written(descriptor, body):
            result = write_fd(descriptor, body)
            if descriptor == temp_fd:
                attack("write")
            return result

        def synced(descriptor):
            result = fsync_fd(descriptor)
            if descriptor == temp_fd:
                attack("file_fsync")
            elif descriptor == store._root_fd:
                attack("directory_fsync")
            return result

        def replaced(*values, **options):
            result = replace_file(*values, **options)
            if options.get("dst_dir_fd") == store._root_fd:
                attack("rename")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", opened)
            patch.setattr(os, "write", written)
            patch.setattr(os, "fsync", synced)
            patch.setattr(os, "replace", replaced)
            return original(store, *args, **kwargs)

    monkeypatch.setattr(owner, name, wrapped)
    with pytest.raises(error) as caught:
        commit(proposal.handle)
    assert attacked
    if boundary in {"rename", "directory_fsync"}:
        assert caught.value.code == "persistence_uncertain"
    else:
        assert (path / "ledger.sqlite3").read_bytes() == before
    catalog.close()
