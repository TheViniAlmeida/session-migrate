"""Real B3/C1 paths over synthetic, private files; no external native clients."""

import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate import operations, reconciliation
from session_migrate.adoption import (
    AdoptionError,
    commit_adoption,
    inspect_adoption,
    prepare_adoption,
)
from session_migrate.baselines import BaselineStore
from session_migrate.catalog import Catalog
from session_migrate.continuity import ExplicitPair
from session_migrate.continuity_bindings import LegacyRuntimeContext
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    default_migration_state_home,
)
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
from session_migrate.profiles import Profile, SessionRef, prepare_profile_operation
from session_migrate.reconciliation import (
    ReconciliationError,
    RecoveryContext,
    cancel_recovered_ready,
    commit_reconciliation,
    prepare_reconciliation,
)
from session_migrate.titles import TitlePolicy, title_receipt_path

SOURCE_ID = "40000000-0000-4000-8000-000000000001"
TARGET_ID = "50000000-0000-4000-8000-000000000001"
STAMP = "2026-01-01T00:00:00Z"


def records():
    return [
        {
            "type": role,
            "uuid": f"synthetic-{i}",
            "parentUuid": "synthetic-0" if i else None,
            "sessionId": SOURCE_ID,
            "cwd": "/synthetic/work",
            "version": "2.1.209",
            "timestamp": STAMP,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        for i, (role, text) in enumerate(
            (("user", "synthetic request"), ("assistant", "synthetic answer"))
        )
    ]


def write(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(v).encode() + b"\n" for v in values))
    path.chmod(0o600)


def environment(tmp_path):
    tmp_path.chmod(0o700)
    result = {}
    for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        root = tmp_path / key.lower()
        root.mkdir(mode=0o700)
        result[key] = str(root)
    return result


def recovery_fixture(
    tmp_path,
    monkeypatch,
    *,
    native_source=False,
    title=False,
    fail_after_title=False,
    explicit_format=True,
):
    env = environment(tmp_path)
    source_root = tmp_path / "source"
    source_path = (
        (
            source_root
            / "projects"
            / claude.project_directory_name(Path("/synthetic/work"))
            / f"{SOURCE_ID}.jsonl"
        )
        if native_source
        else tmp_path / "original-source.jsonl"
    )
    write(source_path, records())
    req = OperationRequest(
        OperationKind.TRANSFER if native_source else OperationKind.IMPORT,
        path=None if native_source else source_path,
        source_format=AgentFormat.CLAUDE if explicit_format else None,
        source_home=source_root if native_source else None,
        source_id=SOURCE_ID if native_source else None,
        target_format=TargetFormat.CODEX,
        home=tmp_path / "target",
        session_id=TARGET_ID,
        cwd=Path("/synthetic/work"),
        target_cli_version="0.144.4",
        target_title="synthetic alias" if title else None,
        title_policy=TitlePolicy.LOCAL_ALIAS if title else None,
    )
    prepared = prepare_operation(req, environ=env)
    state = prepared._state
    req = state.built.request
    manifest = state.built.manifest
    input_path = state.built.output
    if fail_after_title:
        original = LineageStore.complete
        monkeypatch.setattr(
            LineageStore,
            "complete",
            lambda *args: (_ for _ in ()).throw(SessionMigrateError("synthetic interruption")),
        )
    else:
        original = operations._completion_record
        monkeypatch.setattr(
            operations,
            "_completion_record",
            lambda *args: (_ for _ in ()).throw(SessionMigrateError("synthetic interruption")),
        )
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    if fail_after_title:
        monkeypatch.setattr(LineageStore, "complete", original)
    else:
        monkeypatch.setattr(operations, "_completion_record", original)
    store_path = default_migration_state_home(environ=env) / "lineage-v1"
    operation_id = caught.value.receipt.operation_id
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(
        claude_roots=(source_root,) if native_source else (),
        codex_roots=(tmp_path / "target",),
        include_auto=False,
    )
    profiles = [Profile("target", "Target", AgentFormat.CODEX, tmp_path / "target")]
    if native_source:
        profiles.append(Profile("source", "Source", AgentFormat.CLAUDE, source_root))
    observer = EndpointObserver(
        catalog,
        tuple(profiles),
        base_environ=env,
        legacy_runtime_contexts=tuple(LegacyRuntimeContext(p.id, env) for p in profiles),
    )
    context = RecoveryContext(req, source_path, input_path, env, env, manifest_path=manifest)
    return operation_id, store_path, context, observer, catalog


def no_native(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("recovery/adoption called native publication")

    for name in ("_install", "_build_operation", "_execute", "_run_operation"):
        monkeypatch.setattr(operations, name, denied)
    monkeypatch.setattr(LineageStore, "begin_publication", denied)
    monkeypatch.setattr(LineageStore, "mark_verifying", denied)


def profile_recovery_fixture(
    tmp_path, monkeypatch, *, source_format=AgentFormat.CLAUDE, title=False
):
    """Actual profile API normalization and durable B3 ACK in both directions."""
    env = environment(tmp_path)
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    target_format = AgentFormat.CODEX if source_format is AgentFormat.CLAUDE else AgentFormat.CLAUDE
    releases = {AgentFormat.CLAUDE: "2.1.209", AgentFormat.CODEX: "0.144.4"}
    if source_format is AgentFormat.CLAUDE:
        source_path = (
            source_root
            / "projects"
            / claude.project_directory_name(Path("/synthetic/work"))
            / (SOURCE_ID + ".jsonl")
        )
        write(source_path, records())
    else:
        seed = tmp_path / "fixture-seed.jsonl"
        write(seed, records())
        captured = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, seed))
        artifact = convert_session(
            captured.parse(),
            ConversionOptions(
                TargetFormat.CODEX,
                session_id=SOURCE_ID,
                cwd=Path("/synthetic/work"),
                target_cli_version=releases[AgentFormat.CODEX],
            ),
        )
        source_path = source_root / "sessions/2026/01/01" / ("rollout-" + SOURCE_ID + ".jsonl")
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(artifact.native_bytes)
        source_path.chmod(0o600)
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(
        claude_roots=(source_root,) if source_format is AgentFormat.CLAUDE else (),
        codex_roots=(source_root,) if source_format is AgentFormat.CODEX else (),
        include_auto=False,
    )
    profiles = (
        Profile("source", "Source", source_format, source_root),
        Profile("target", "Target", target_format, target_root),
    )
    ref = SessionRef(
        profiles[0].format,
        profiles[0].root,
        SOURCE_ID,
        profiles[0].id,
        profile_fingerprint=profiles[0].fingerprint,
    )
    request = OperationRequest(
        OperationKind.TRANSFER,
        target_format=TargetFormat(target_format.value),
        session_id=TARGET_ID,
        cwd=Path("/synthetic/work"),
        target_cli_version=releases[target_format],
        target_title="synthetic profile alias" if title else None,
        title_policy=TitlePolicy.LOCAL_ALIAS if title else None,
    )
    prepared = prepare_profile_operation(
        catalog, profiles[0], ref, profiles[1], request, base_environ=env
    )
    state = prepared._state
    context = RecoveryContext(
        state.built.request,
        source_path,
        state.built.output,
        state.source_environment,
        state.target_environment,
        source_profile=profiles[0],
        target_profile=profiles[1],
        manifest_path=state.built.manifest,
    )
    original = operations._completion_record
    monkeypatch.setattr(
        operations,
        "_completion_record",
        lambda *args: (_ for _ in ()).throw(SessionMigrateError("synthetic interruption")),
    )
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    monkeypatch.setattr(operations, "_completion_record", original)
    catalog.refresh(
        claude_roots=(source_root if source_format is AgentFormat.CLAUDE else target_root,),
        codex_roots=(source_root if source_format is AgentFormat.CODEX else target_root,),
        include_auto=False,
    )
    observer = EndpointObserver(catalog, profiles, base_environ=env)
    return (
        caught.value.receipt.operation_id,
        default_migration_state_home(environ=env) / "lineage-v1",
        context,
        observer,
        catalog,
    )


@pytest.mark.parametrize("native_source", [False, True])
def test_real_ack_recovery_reopens_and_never_republishes(tmp_path, monkeypatch, native_source):
    operation_id, store_path, context, observer, catalog = recovery_fixture(
        tmp_path, monkeypatch, native_source=native_source
    )
    no_native(monkeypatch)
    before = (store_path / "ledger.sqlite3").read_bytes()
    proposal = prepare_reconciliation(
        operation_id, store_path=store_path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    assert (store_path / "ledger.sqlite3").read_bytes() == before
    assert "synthetic request" not in json.dumps(proposal.to_dict())
    receipt = commit_reconciliation(proposal.handle)
    assert receipt.continuity_reason == ("tracked" if native_source else "identity_untracked")
    view = LineageStore.inspect_existing(store_path)
    assert view.operations[0].durable_state is OperationState.COMPLETED
    assert view.operations[0].source_checkpoint.identity is None if not native_source else True
    with pytest.raises(ReconciliationError, match="invalid_handle"):
        commit_reconciliation(proposal.handle)
    historical = prepare_reconciliation(operation_id, store_path=store_path)
    assert historical.status == "completed"
    assert commit_reconciliation(historical.handle) == receipt
    catalog.close()


@pytest.mark.parametrize("change", ["input", "source", "options", "version", "context"])
def test_recovery_changed_proofs_block_without_ledger_write(tmp_path, monkeypatch, change):
    operation_id, path, context, observer, catalog = recovery_fixture(tmp_path, monkeypatch)
    before = (path / "ledger.sqlite3").read_bytes()
    if change in {"input", "source"}:
        changed = context.input_path if change == "input" else context.source_path
        data = changed.read_bytes().replace(b"synthetic answer", b"synthetic broken")
        old = changed.stat()
        changed.write_bytes(data)
        os.utime(changed, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif change == "options":
        context = replace(context, request=replace(context.request, model_provider="changed"))
    elif change == "version":
        context = replace(context, request=replace(context.request, target_cli_version="0.153.4"))
    else:
        context = None
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "blocked"
    assert proposal.handle is None
    assert (path / "ledger.sqlite3").read_bytes() == before
    catalog.close()


@pytest.mark.parametrize("tamper", ["clone", "binding", "discard", "source"])
def test_recovery_one_shot_and_freshness(tmp_path, monkeypatch, tamper):
    operation_id, path, context, observer, catalog = recovery_fixture(tmp_path, monkeypatch)
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    handle = proposal.handle
    before = (path / "ledger.sqlite3").read_bytes()
    if tamper == "clone":
        handle = copy.copy(handle)
    elif tamper == "binding":
        object.__setattr__(handle, "_binding", "0" * 64)
    elif tamper == "discard":
        handle.discard()
    else:
        context.source_path.write_bytes(
            context.source_path.read_bytes().replace(b"synthetic answer", b"synthetic broken")
        )
    with pytest.raises(ReconciliationError):
        commit_reconciliation(handle)
    assert (path / "ledger.sqlite3").read_bytes() == before
    catalog.close()


@pytest.mark.parametrize("existing", [False, True])
def test_f3_alias_and_receipt_are_repaired_or_reused_exactly(tmp_path, monkeypatch, existing):
    operation_id, path, context, observer, catalog = recovery_fixture(
        tmp_path, monkeypatch, title=True, fail_after_title=existing
    )
    no_native(monkeypatch)
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    assert "local_alias" in proposal.available_effects
    receipt = commit_reconciliation(proposal.handle)
    receipt_path = title_receipt_path(context.manifest_path)
    document = json.loads(receipt_path.read_bytes())
    assert document["alias_saved"] and document["binding"]["proposal_id"] == operation_id
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert any(ref.kind == "receipt" and ref.sha256 for ref in receipt.references)
    catalog.close()


def adoption_fixture(tmp_path, target_format=TargetFormat.CLAUDE):
    tmp_path.chmod(0o700)
    roots = (tmp_path / "source", tmp_path / "target")
    source_path = (
        roots[0]
        / "projects"
        / claude.project_directory_name(Path("/synthetic/work"))
        / f"{SOURCE_ID}.jsonl"
    )
    write(source_path, records())
    source = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, source_path))
    artifact = convert_session(
        source.parse(),
        ConversionOptions(
            target_format,
            session_id=TARGET_ID,
            cwd=Path("/synthetic/work"),
            target_cli_version="2.1.209" if target_format is TargetFormat.CLAUDE else "0.144.4",
        ),
    )
    target_path = (
        (roots[1] / "projects" / claude.project_directory_name(artifact.cwd) / f"{TARGET_ID}.jsonl")
        if target_format is TargetFormat.CLAUDE
        else roots[1] / "sessions/2026/01/01" / f"rollout-{TARGET_ID}.jsonl"
    )
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(artifact.native_bytes)
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(
        claude_roots=roots if target_format is TargetFormat.CLAUDE else roots[:1],
        codex_roots=roots[1:] if target_format is TargetFormat.CODEX else (),
        include_auto=False,
    )
    profiles = tuple(
        Profile(name, name, fmt, root)
        for name, root, fmt in zip(
            ("source", "target"),
            roots,
            (AgentFormat.CLAUDE, AgentFormat(target_format.value)),
            strict=True,
        )
    )
    observer = EndpointObserver(catalog, profiles)
    refs = tuple(
        SessionRef(p.format, p.root, identifier, p.id, profile_fingerprint=p.fingerprint)
        for p, identifier in zip(profiles, (SOURCE_ID, TARGET_ID), strict=True)
    )
    return (
        ExplicitPair(*refs),
        observer,
        tmp_path / "state",
        catalog,
        (source_path, target_path),
    )


def plan_adoption(pair, observer, home):
    return prepare_adoption(
        pair,
        observer,
        state_home=home,
        source_client_version="2.1.209",
        target_client_version="2.1.209" if pair.target.format is AgentFormat.CLAUDE else "0.144.4",
    )


def test_adoption_observes_real_pair_and_bootstraps_only_on_commit(tmp_path, monkeypatch):
    pair, observer, home, catalog, paths = adoption_fixture(tmp_path)
    no_native(monkeypatch)
    before = tuple(p.read_bytes() for p in paths)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "ready", proposal.reason
    assert not home.exists()
    assert "synthetic request" not in json.dumps(proposal.to_dict())
    receipt = commit_adoption(proposal.handle)
    assert receipt.baseline_id == proposal.baseline_id
    assert inspect_adoption(receipt.baseline_id, state_home=home) == receipt
    coordinator = LineageStore.inspect_existing(home / "lineage-v1")
    assert not coordinator.operations and not coordinator.edges
    view = BaselineStore.inspect_existing(home / "baselines-v1")
    record = view.baselines[0]
    assert record.provenance == "adopted_now" and record.source_profile_binding_kind == "profile_v1"
    assert not hasattr(record, "operation_id")
    assert tuple(p.read_bytes() for p in paths) == before
    with pytest.raises(AdoptionError, match="invalid_handle"):
        commit_adoption(proposal.handle)
    catalog.close()


@pytest.mark.parametrize("change", ["content", "unknown", "profile", "version"])
def test_adoption_changed_or_unproved_pair_has_no_state_effect(tmp_path, monkeypatch, change):
    pair, observer, home, catalog, paths = adoption_fixture(tmp_path)
    if change == "content":
        paths[1].write_bytes(
            paths[1].read_bytes().replace(b"synthetic answer", b"synthetic broken")
        )
    elif change == "unknown":
        paths[1].write_bytes(paths[1].read_bytes() + b'{"type":"future_unknown"}\n')
    elif change == "profile":
        pair = replace(pair, target=replace(pair.target, profile_fingerprint="f" * 64))
    else:
        paths[1].write_bytes(paths[1].read_bytes().replace(b"2.1.209", b"9.9.999"))
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "blocked"
    assert proposal.handle is None and not home.exists()
    catalog.close()


def test_adoption_rechecks_after_prepare_without_bootstrap(tmp_path, monkeypatch):
    pair, observer, home, catalog, paths = adoption_fixture(tmp_path)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "ready", proposal.reason
    paths[1].write_bytes(paths[1].read_bytes().replace(b"synthetic answer", b"synthetic broken"))
    with pytest.raises(AdoptionError):
        commit_adoption(proposal.handle)
    assert not home.exists()
    catalog.close()


@pytest.mark.parametrize("state", ["ready", "publishing", "attempted"])
def test_ready_cancel_only_and_unknown_never_promotes_ack(tmp_path, monkeypatch, state):
    env = environment(tmp_path)
    source = tmp_path / "source.jsonl"
    write(source, records())
    prepared = prepare_operation(
        OperationRequest(
            OperationKind.IMPORT,
            path=source,
            source_format=AgentFormat.CLAUDE,
            target_format=TargetFormat.CODEX,
            home=tmp_path / "target",
            session_id=TARGET_ID,
            cwd=Path("/synthetic/work"),
            target_cli_version="0.144.4",
        ),
        environ=env,
    )
    record = operations._ready_record(prepared._state, prepared._proposal.proposal_id)
    path = tmp_path / "ledger"
    with (
        LineageStore.create_new(path) as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        if state != "ready":
            store.begin_publication(record.operation_id, guard)
        if state == "attempted":
            store.mark_attempted(record.operation_id, guard)
    no_native(monkeypatch)
    before = (path / "ledger.sqlite3").read_bytes()
    proposal = prepare_reconciliation(record.operation_id, store_path=path)
    assert proposal.handle is None
    assert (path / "ledger.sqlite3").read_bytes() == before
    if state == "ready":
        assert proposal.status == "inspect_only" and "cancel_ready" in proposal.available_effects
        cancel_recovered_ready(record.operation_id, store_path=path)
        assert (
            LineageStore.inspect_existing(path).operations[0].durable_state
            is OperationState.CANCELLED
        )
    else:
        assert proposal.reason == "native_ack_required"
        with pytest.raises(ReconciliationError, match="not_cancelable"):
            cancel_recovered_ready(record.operation_id, store_path=path)
    prepared.discard()


def test_title_receipt_failure_preserves_ack_and_same_operation_repairs_partial(
    tmp_path, monkeypatch
):
    operation_id, path, context, observer, catalog = recovery_fixture(
        tmp_path, monkeypatch, title=True
    )
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    writer = reconciliation.write_title_receipt

    def fail(*args, **kwargs):
        raise OSError("synthetic receipt failure")

    monkeypatch.setattr(reconciliation, "write_title_receipt", fail)
    with pytest.raises(ReconciliationError):
        commit_reconciliation(proposal.handle)
    view = LineageStore.inspect_existing(path)
    assert view.operations[0].durable_state is OperationState.NEEDS_RECONCILE
    assert view.operations[0].durable_native_state is NativeState.ACKNOWLEDGED
    assert not title_receipt_path(context.manifest_path).exists()
    alias_dir = (
        default_migration_state_home(environ=context.target_environment) / "title-aliases/v1"
    )
    assert len(list(alias_dir.glob("*.json"))) == 1
    monkeypatch.setattr(reconciliation, "write_title_receipt", writer)
    monkeypatch.setattr(
        reconciliation.TitleAliasStore,
        "create",
        lambda *a, **kw: pytest.fail("alias recreated"),
    )
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    commit_reconciliation(proposal.handle)
    catalog.close()


@pytest.mark.parametrize(
    "change",
    ["alias_operation", "alias_title", "receipt", "manifest", "missing_manifest"],
)
def test_f3_conflicts_never_complete(tmp_path, monkeypatch, change):
    operation_id, path, context, observer, catalog = recovery_fixture(
        tmp_path, monkeypatch, title=True, fail_after_title=True
    )
    if change.startswith("alias"):
        alias_dir = (
            default_migration_state_home(environ=context.target_environment) / "title-aliases/v1"
        )
        document_path = next(alias_dir.glob("*.json"))
        document = json.loads(document_path.read_bytes())
        document["proposal_id" if change == "alias_operation" else "alias"] = "conflicting-value"
        document_path.write_text(json.dumps(document))
    elif change == "receipt":
        document_path = title_receipt_path(context.manifest_path)
        document = json.loads(document_path.read_bytes())
        document["binding"]["target_sha256"] = "f" * 64
        document_path.write_text(json.dumps(document))
    elif change == "manifest":
        document = json.loads(context.manifest_path.read_bytes())
        document["target"]["timestamp"] = "2000-01-01T00:00:00Z"
        context.manifest_path.write_text(json.dumps(document))
    else:
        context = replace(context, manifest_path=None)
    before = (path / "ledger.sqlite3").read_bytes()
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "blocked" and proposal.handle is None
    assert (path / "ledger.sqlite3").read_bytes() == before
    catalog.close()


def test_recovery_postpersist_change_is_uncertain_but_history_is_retained(tmp_path, monkeypatch):
    operation_id, path, context, observer, catalog = recovery_fixture(tmp_path, monkeypatch)
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    original = LineageStore.complete_reconciled

    def close_then_change(*args, **kwargs):
        receipt = original(*args, **kwargs)
        context.source_path.write_bytes(
            context.source_path.read_bytes().replace(b"synthetic answer", b"synthetic broken")
        )
        return receipt

    monkeypatch.setattr(LineageStore, "complete_reconciled", close_then_change)
    with pytest.raises(ReconciliationError, match="persistence_uncertain") as caught:
        commit_reconciliation(proposal.handle)
    assert caught.value.operation_id == operation_id
    historical = prepare_reconciliation(operation_id, store_path=path)
    assert historical.status == "completed"
    assert historical.receipt == commit_reconciliation(historical.handle)
    catalog.close()


def test_adoption_postpersist_change_inspects_original_baseline_id(tmp_path, monkeypatch):
    pair, observer, home, catalog, paths = adoption_fixture(tmp_path)
    proposal = plan_adoption(pair, observer, home)
    original = BaselineStore.append

    def append_then_change(*args, **kwargs):
        receipt = original(*args, **kwargs)
        paths[1].write_bytes(
            paths[1].read_bytes().replace(b"synthetic answer", b"synthetic broken")
        )
        return receipt

    monkeypatch.setattr(BaselineStore, "append", append_then_change)
    with pytest.raises(AdoptionError, match="persistence_uncertain") as caught:
        commit_adoption(proposal.handle)
    assert caught.value.baseline_id == proposal.baseline_id
    receipt = inspect_adoption(proposal.baseline_id, state_home=home)
    assert receipt.baseline_id == proposal.baseline_id
    assert len(BaselineStore.inspect_existing(home / "baselines-v1").baselines) == 1
    assert not LineageStore.inspect_existing(home / "lineage-v1").operations
    catalog.close()


@pytest.mark.parametrize("tamper", ["clone", "binding", "proposal", "discard"])
def test_adoption_private_one_shot_tamper_cannot_bootstrap(tmp_path, monkeypatch, tamper):
    pair, observer, home, catalog, _ = adoption_fixture(tmp_path)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "ready", proposal.reason
    handle = proposal.handle
    if tamper == "clone":
        handle = copy.copy(handle)
    elif tamper == "binding":
        object.__setattr__(handle, "_binding", "0" * 64)
    elif tamper == "proposal":
        object.__setattr__(proposal, "baseline_id", "changed-id")
    else:
        handle.discard()
    with pytest.raises(AdoptionError, match="invalid_handle"):
        commit_adoption(handle)
    assert not home.exists()
    catalog.close()


def test_second_adoption_preserves_existing_coordinator_byteexact(tmp_path, monkeypatch):
    pair, observer, home, catalog, _ = adoption_fixture(tmp_path)
    first = commit_adoption(plan_adoption(pair, observer, home).handle)
    before = (home / "lineage-v1/ledger.sqlite3").read_bytes()
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "ready", proposal.reason
    assert "bootstrap_empty_coordinator" not in proposal.available_effects
    second = commit_adoption(proposal.handle)
    assert second.baseline_id != first.baseline_id
    assert (home / "lineage-v1/ledger.sqlite3").read_bytes() == before
    assert len(BaselineStore.inspect_existing(home / "baselines-v1").baselines) == 2
    catalog.close()


def test_crossformat_adoption_has_independent_linear_graph_and_semantic_relation(
    tmp_path, monkeypatch
):
    pair, observer, home, catalog, _ = adoption_fixture(tmp_path, TargetFormat.CODEX)
    no_native(monkeypatch)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "ready", proposal.reason
    receipt = commit_adoption(proposal.handle)
    assert inspect_adoption(receipt.baseline_id, state_home=home) == receipt
    assert not LineageStore.inspect_existing(home / "lineage-v1").operations
    catalog.close()


def test_adoption_unselected_branch_is_blocked_despite_matching_semantics(tmp_path):
    pair, observer, home, catalog, paths = adoption_fixture(tmp_path)
    values = [json.loads(row) for row in paths[1].read_bytes().splitlines()]
    branch = copy.deepcopy(values[1])
    branch["uuid"] = "unselected-branch"
    values.insert(1, branch)
    write(paths[1], values)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "blocked" and not home.exists()
    assert proposal.reason in {"structure_unproven", "coverage_unverified"}
    catalog.close()


def test_same_uuid_collision_in_target_namespace_cannot_be_adopted(tmp_path):
    pair, observer, home, catalog, paths = adoption_fixture(tmp_path)
    collision = pair.target.root / "projects/another-work" / paths[1].name
    collision.parent.mkdir(parents=True)
    collision.write_bytes(paths[1].read_bytes())
    catalog.refresh(claude_roots=(pair.source.root, pair.target.root), include_auto=False)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "blocked" and not home.exists()
    catalog.close()


@pytest.mark.parametrize("service", ["recovery", "adoption"])
def test_native_namespace_root_swap_invalidates_prepared_handle(tmp_path, monkeypatch, service):
    if service == "recovery":
        operation_id, path, context, observer, catalog = recovery_fixture(tmp_path, monkeypatch)
        proposal = prepare_reconciliation(
            operation_id, store_path=path, context=context, observer=observer
        )
        root = tmp_path / "target"

        def commit():
            return commit_reconciliation(proposal.handle)

        error = ReconciliationError
    else:
        pair, observer, _, catalog, _ = adoption_fixture(tmp_path)
        proposal = plan_adoption(pair, observer, tmp_path / "state")
        root = pair.target.root

        def commit():
            return commit_adoption(proposal.handle)

        error = AdoptionError
    assert proposal.status == "ready", proposal.reason
    moved = tmp_path / "previous-root"
    root.rename(moved)
    import shutil

    shutil.copytree(moved, root)
    with pytest.raises(error):
        commit()
    catalog.close()


def test_changed_current_profile_invalidates_recovery_without_overwriting(tmp_path, monkeypatch):
    operation_id, path, context, observer, catalog = recovery_fixture(tmp_path, monkeypatch)
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    before = (path / "ledger.sqlite3").read_bytes()
    object.__setattr__(observer._profiles[0], "environ", {"HOME": str(tmp_path / "changed-home")})
    with pytest.raises(ReconciliationError):
        commit_reconciliation(proposal.handle)
    assert (path / "ledger.sqlite3").read_bytes() == before
    catalog.close()


def test_recovery_original_autodetected_format_stays_recoverable(tmp_path, monkeypatch):
    operation_id, path, context, observer, catalog = recovery_fixture(
        tmp_path, monkeypatch, explicit_format=False
    )
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    assert commit_reconciliation(proposal.handle).continuity_reason == "identity_untracked"
    catalog.close()


@pytest.mark.parametrize("change", ["header", "unknown", "payload"])
def test_original_input_copy_never_hides_altered_target_raw(tmp_path, monkeypatch, change):
    operation_id, path, context, observer, catalog = recovery_fixture(tmp_path, monkeypatch)
    original_input = tmp_path / "original-input-copy.jsonl"
    original_input.write_bytes(context.input_path.read_bytes())
    original_input.chmod(0o600)
    target_path = context.input_path
    context = replace(context, input_path=original_input)
    values = [json.loads(row) for row in target_path.read_bytes().splitlines()]
    if change == "header":
        values[0]["payload"]["model_provider"] = "changed-provider"
    elif change == "unknown":
        values[-1]["unknown_future_field"] = True
    else:
        values[-1]["payload"]["content"][0]["text"] = "synthetic changed payload"
    previous = target_path.stat()
    write(target_path, values)
    os.utime(target_path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    before = (path / "ledger.sqlite3").read_bytes()
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert proposal.status == "blocked" and proposal.handle is None
    assert (path / "ledger.sqlite3").read_bytes() == before
    catalog.close()


def test_reconciliation_after_rename_fault_reports_same_id_and_completed_history(
    tmp_path, monkeypatch
):
    operation_id, path, context, observer, catalog = recovery_fixture(tmp_path, monkeypatch)
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    original = LineageStore.complete_reconciled

    def fail_after_rename(store, *args, **kwargs):
        def fault(event):
            if event == "after_rename":
                raise OSError("synthetic durable-image interruption")

        store._fault_hook = fault
        try:
            return original(store, *args, **kwargs)
        finally:
            store._fault_hook = None

    monkeypatch.setattr(LineageStore, "complete_reconciled", fail_after_rename)
    with pytest.raises(ReconciliationError, match="persistence_uncertain") as caught:
        commit_reconciliation(proposal.handle)
    assert caught.value.operation_id == operation_id
    historical = prepare_reconciliation(operation_id, store_path=path)
    assert historical.status == "completed" and historical.receipt.operation_id == operation_id
    catalog.close()


@pytest.mark.parametrize("existing", [False, True])
def test_adoption_after_rename_fault_reports_same_baseline_id(tmp_path, monkeypatch, existing):
    pair, observer, home, catalog, _ = adoption_fixture(tmp_path)
    if existing:
        first = commit_adoption(plan_adoption(pair, observer, home).handle)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "ready", proposal.reason
    original = BaselineStore.append

    def fail_after_rename(store, *args, **kwargs):
        def fault(event):
            if event == "after_rename":
                raise OSError("synthetic durable baseline interruption")

        store._fault_hook = fault
        try:
            return original(store, *args, **kwargs)
        finally:
            store._fault_hook = None

    monkeypatch.setattr(BaselineStore, "append", fail_after_rename)
    with pytest.raises(AdoptionError, match="persistence_uncertain") as caught:
        commit_adoption(proposal.handle)
    assert caught.value.baseline_id == proposal.baseline_id
    assert (
        inspect_adoption(proposal.baseline_id, state_home=home).baseline_id == proposal.baseline_id
    )
    if existing:
        assert inspect_adoption(first.baseline_id, state_home=home) == first
    assert not LineageStore.inspect_existing(home / "lineage-v1").operations
    catalog.close()


def test_partial_local_alias_then_changed_native_never_rewrites_alias(tmp_path, monkeypatch):
    operation_id, path, context, observer, catalog = recovery_fixture(
        tmp_path, monkeypatch, title=True
    )
    proposal = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    writer = reconciliation.write_title_receipt
    monkeypatch.setattr(
        reconciliation,
        "write_title_receipt",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic receipt failure")),
    )
    with pytest.raises(ReconciliationError):
        commit_reconciliation(proposal.handle)
    monkeypatch.setattr(reconciliation, "write_title_receipt", writer)
    alias_dir = (
        default_migration_state_home(environ=context.target_environment) / "title-aliases/v1"
    )
    alias = next(alias_dir.glob("*.json"))
    before_alias = alias.read_bytes()
    original_input = tmp_path / "original-input.jsonl"
    original_input.write_bytes(context.input_path.read_bytes())
    native_path = context.input_path
    context = replace(context, input_path=original_input)
    native_path.write_bytes(
        native_path.read_bytes().replace(b"synthetic answer", b"synthetic broken")
    )
    second = prepare_reconciliation(
        operation_id, store_path=path, context=context, observer=observer
    )
    assert second.status == "blocked" and second.handle is None
    assert alias.read_bytes() == before_alias
    assert json.loads(before_alias)["proposal_id"] == operation_id
    assert (
        LineageStore.inspect_existing(path).operations[0].durable_state
        is OperationState.NEEDS_RECONCILE
    )
    catalog.close()


@pytest.mark.parametrize("change", ["mismatched", "duplicate", "reordered", "nonlinear"])
def test_crossformat_codex_duplicate_pairing_requires_complete_ordered_proof(tmp_path, change):
    pair, observer, home, catalog, paths = adoption_fixture(tmp_path, TargetFormat.CODEX)
    values = [json.loads(row) for row in paths[1].read_bytes().splitlines()]
    indices = [i for i, row in enumerate(values) if row["type"] == "event_msg"]
    if change == "mismatched":
        values[indices[0]]["payload"]["message"] = "synthetic mismatched carrier"
    elif change == "duplicate":
        values.insert(indices[0], copy.deepcopy(values[indices[0]]))
    elif change == "reordered":
        values[indices[0]]["payload"], values[indices[1]]["payload"] = (
            values[indices[1]]["payload"],
            values[indices[0]]["payload"],
        )
    else:
        values[1]["ordinal"] = 9999
    write(paths[1], values)
    proposal = plan_adoption(pair, observer, home)
    assert proposal.status == "blocked" and proposal.handle is None and not home.exists()
    assert proposal.reason in {
        "coverage_unverified",
        "structure_unproven",
        "semantic_mismatch",
    }
    catalog.close()
