"""Real B3 summary publication over private synthetic Claude/Codex files."""

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate import operations
from session_migrate.catalog import Catalog
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.conversion import decode_derived_manifest
from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError
from session_migrate.formats import claude
from session_migrate.handoff import accept_user_text, prepare_handoff
from session_migrate.handoff_operations import prepare_summary
from session_migrate.handoff_types import HandoffLimits
from session_migrate.lineage import LineageStore, OperationState, SelectionRangeV1
from session_migrate.model import AgentFormat
from session_migrate.operations import OperationRequest, commit_operation
from session_migrate.profiles import Profile, SessionRef
from session_migrate.reconciliation import (
    RecoveryContext,
    commit_reconciliation,
    prepare_reconciliation,
)
from session_migrate.titles import AliasIdentity, TitleAliasStore, TitlePolicy, title_receipt_path

ID = "61111111-1111-4111-8111-111111111111"
BODY = b"Objective: unknown\nNext step: inspect synthetic fixture\n<html>literal</html>"


def fixture(tmp_path, target=AgentFormat.CODEX):
    tmp_path.chmod(0o700)
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    env = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    root = tmp_path / "source"
    path = root / "projects" / claude.project_directory_name(project) / (ID + ".jsonl")
    path.parent.mkdir(parents=True)
    rows = [
        {
            "type": role,
            "uuid": str(i),
            "parentUuid": str(i - 1) if i else None,
            "sessionId": ID,
            "cwd": str(project),
            "version": "2.1.209",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        for i, (role, text) in enumerate(
            (("user", "native original request"), ("assistant", "native original answer"))
        )
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(claude_roots=(root,), include_auto=False)
    source = Profile("source", "Source", AgentFormat.CLAUDE, root)
    target_profile = Profile("target", "Target", target, tmp_path / "target")
    ref = SessionRef(
        source.format, source.root, ID, source.id, profile_fingerprint=source.fingerprint
    )
    observer = EndpointObserver(catalog, (source,), base_environ=env)
    draft = prepare_handoff(
        ref, (SelectionRangeV1(0, 1, None),), HandoffLimits(), observer, client_version="2.1.209"
    )
    reviewed = accept_user_text(draft, BODY, confirmed_sha256=hashlib.sha256(BODY).hexdigest())
    options = OperationRequest(
        "transfer",
        cwd=project,
        target_cli_version="2.1.209" if target is AgentFormat.CLAUDE else "0.144.4",
        model_provider="openai",
        model="synthetic-model",
    )
    return catalog, observer, source, target_profile, ref, path, reviewed, options, env


@pytest.mark.parametrize("target", [AgentFormat.CLAUDE, AgentFormat.CODEX])
def test_summary_real_b3_has_original_source_and_genuine_user_target(tmp_path, target):
    catalog, observer, source, destination, _, path, reviewed, options, env = fixture(
        tmp_path, target
    )
    original = path.read_bytes()
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    assert (
        prepared._state.built.artifact.source.source_sha256 == hashlib.sha256(original).hexdigest()
    )
    assert prepared._state.built.artifact.source.session_id == ID
    assert "native original answer" not in prepared._state.built.artifact.native_bytes.decode()
    assert BODY.decode() in prepared._state.built.runtime_context.derived_input.events[0].text
    receipt = commit_operation(prepared)
    assert receipt.lineage_receipt.continuity_reason == "derived_handoff"
    assert (
        receipt.lineage_receipt.derivation.source_identity.profile_fingerprint == source.fingerprint
    )
    manifest = decode_derived_manifest(
        Path(receipt.to_dict()["legacy_result"]["manifest"]).read_bytes()
    )
    assert manifest["schema_version"] == 3
    assert manifest["authority_source"]["session_id"] == ID
    assert manifest["conversion_input"]["document_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert BODY.decode() not in json.dumps(manifest)
    view = (
        LineageStore.inspect_existing(prepared._state.migration_state_home / "lineage-v1")
        if prepared._state
        else LineageStore.inspect_existing(tmp_path / ".local/state/session-migrate/lineage-v1")
    )
    op = view.operations[0]
    assert op.source_checkpoint.identity == op.record.source.identity
    assert op.source_checkpoint.artifact_sha256 == hashlib.sha256(original).hexdigest()
    assert op.target_checkpoint.identity == receipt.lineage_receipt.target_identity
    assert view.edges[0].derivation == receipt.lineage_receipt.derivation
    assert path.read_bytes() == original
    catalog.close()


@pytest.mark.parametrize(
    "change", ["source", "catalog", "options", "source_profile", "target_profile", "closed_catalog"]
)
def test_prepared_runtime_guards_block_before_ready_with_no_ledger(tmp_path, change):
    catalog, observer, source, destination, _, source_path, reviewed, options, env = fixture(
        tmp_path
    )
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    if change == "source":
        source_path.write_bytes(source_path.read_bytes().replace(b"original", b"modified"))
    elif change == "catalog":
        connection = sqlite3.connect(catalog.path)
        try:
            connection.execute("UPDATE sessions SET display_title='Changed physical registry'")
            connection.commit()
        finally:
            connection.close()
    elif change == "options":
        object.__setattr__(prepared._state.built.request, "model", "changed-option")
    elif change == "closed_catalog":
        catalog.close()
    else:
        object.__setattr__(
            source if change == "source_profile" else destination, "name", "Changed current profile"
        )
    with pytest.raises(SessionMigrateError):
        commit_operation(prepared)
    assert not (tmp_path / ".local/state/session-migrate/lineage-v1").exists()
    assert not destination.root.exists()
    catalog.close()


@pytest.mark.parametrize("change", [None, "alias", "receipt"])
@pytest.mark.parametrize("when", ["before_rename", "after_rename"])
def test_summary_alias_receipt_use_target_home_and_remain_guarded(
    tmp_path, monkeypatch, change, when
):
    catalog, observer, _, destination, _, _, reviewed, options, env = fixture(tmp_path)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    destination = replace(destination, environ={"HOME": str(target_home)})
    options = replace(
        options, target_title="Explicit local title", title_policy=TitlePolicy.LOCAL_ALIAS
    )
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    state = prepared._state
    original = LineageStore.complete_reconciled
    changed = []
    alias_paths = []

    def complete(store, *args, **kwargs):
        def fault(event):
            if change is None or event != when or changed:
                return
            aliases = list(
                (target_home / ".local/state/session-migrate/title-aliases/v1").glob("*.json")
            )
            assert len(aliases) == 1
            alias_paths.extend(aliases)
            path = aliases[0] if change == "alias" else title_receipt_path(state.built.manifest)
            document = json.loads(path.read_bytes())
            if change == "alias":
                document["alias"] = "Changed local alias"
            else:
                document["alias_saved"] = False
            path.write_text(json.dumps(document))
            changed.append(event)

        store._fault_hook = fault
        try:
            return original(store, *args, **kwargs)
        finally:
            store._fault_hook = None

    monkeypatch.setattr(LineageStore, "complete_reconciled", complete)
    if change is None:
        receipt = commit_operation(prepared)
        identity = receipt.lineage_receipt.target_identity
        alias_identity = AliasIdentity(
            destination.format, destination.root, identity.namespace.native_id
        ).bind_root()
        alias_store = TitleAliasStore(target_home / ".local/state/session-migrate/title-aliases/v1")
        assert alias_store.load(alias_identity).proposal_id == receipt.lineage_receipt.operation_id
        assert (
            len([ref for ref in receipt.lineage_receipt.references if ref.kind == "receipt"]) == 2
        )
    else:
        with pytest.raises(OperationNeedsReconcileError) as caught:
            commit_operation(prepared)
        assert changed == [when] and len(alias_paths) == 1
        view = LineageStore.inspect_existing(state.migration_state_home / "lineage-v1")
        op = next(
            op for op in view.operations if op.record.operation_id == caught.value.operation_id
        )
        assert op.durable_state is (
            OperationState.COMPLETED if when == "after_rename" else OperationState.NEEDS_RECONCILE
        )
        if when == "after_rename":
            assert caught.value.code == "persistence_uncertain"
    assert (tmp_path / ".local/state/session-migrate/lineage-v1/ledger.sqlite3").exists()
    assert not (target_home / ".local/state/session-migrate/lineage-v1").exists()
    catalog.close()


def test_summary_recovery_requires_existing_document_and_durable_ack(tmp_path, monkeypatch):
    catalog, observer, source, destination, _, path, reviewed, options, env = fixture(tmp_path)
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    state = prepared._state
    original = operations._completion_record

    def fail(*args):
        raise SessionMigrateError("synthetic observation failure")

    monkeypatch.setattr(operations, "_completion_record", fail)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    monkeypatch.setattr(operations, "_completion_record", original)
    catalog.add_root(destination.format, destination.root)
    catalog.refresh(include_auto=False)
    observer = EndpointObserver(catalog, (source, destination), base_environ=env)
    docdir = tmp_path / "documents"
    docdir.mkdir(mode=0o700)
    document = docdir / "accepted.md"
    document.write_bytes(BODY)
    document.chmod(0o600)
    context = RecoveryContext(
        state.built.request,
        path,
        state.built.output,
        {k: v for k, v in state.source_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        {k: v for k, v in state.target_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        source,
        destination,
        state.built.manifest,
        handoff_document=document,
    )
    store_path = state.migration_state_home / "lineage-v1"
    missing = prepare_reconciliation(
        caught.value.receipt.operation_id,
        store_path=store_path,
        context=replace(context, handoff_document=None),
        observer=observer,
    )
    assert missing.status == "blocked"
    proposal = prepare_reconciliation(
        caught.value.receipt.operation_id, store_path=store_path, context=context, observer=observer
    )
    assert proposal.status == "ready", proposal.reason
    receipt = commit_reconciliation(proposal.handle)
    assert receipt.continuity_reason == "derived_handoff"
    assert receipt.derivation.input.document_sha256 == hashlib.sha256(BODY).hexdigest()
    catalog.close()


def test_partial_alias_recovery_repairs_same_ids_with_private_original_document(
    tmp_path, monkeypatch
):
    catalog, observer, source, destination, _, path, reviewed, options, env = fixture(tmp_path)
    options = replace(
        options, target_title="Same local title", title_policy=TitlePolicy.LOCAL_ALIAS
    )
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    state = prepared._state
    writer = operations.write_title_receipt

    def interrupted(*args, **kwargs):
        raise SessionMigrateError("synthetic local receipt interruption")

    monkeypatch.setattr(operations, "write_title_receipt", interrupted)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    monkeypatch.setattr(operations, "write_title_receipt", writer)
    aliases = list((tmp_path / ".local/state/session-migrate/title-aliases/v1").glob("*.json"))
    assert len(aliases) == 1
    alias_bytes = aliases[0].read_bytes()
    assert not title_receipt_path(state.built.manifest).exists()
    catalog.add_root(destination.format, destination.root)
    catalog.refresh(include_auto=False)
    observer = EndpointObserver(catalog, (source, destination), base_environ=env)
    documents = tmp_path / "documents"
    documents.mkdir(mode=0o700)
    document = documents / "accepted.md"
    document.write_bytes(BODY)
    document.chmod(0o600)
    context = RecoveryContext(
        state.built.request,
        path,
        state.built.output,
        {k: v for k, v in state.source_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        {k: v for k, v in state.target_environment.items() if k in operations._PUBLIC_ENVIRONMENT},
        source,
        destination,
        state.built.manifest,
        handoff_document=document,
    )
    store_path = state.migration_state_home / "lineage-v1"
    before = (store_path / "ledger.sqlite3").read_bytes()
    missing = prepare_reconciliation(
        caught.value.operation_id,
        store_path=store_path,
        context=replace(context, handoff_document=None),
        observer=observer,
    )
    assert missing.status == "blocked"
    plan = prepare_reconciliation(
        caught.value.operation_id, store_path=store_path, context=context, observer=observer
    )
    assert plan.status == "ready", plan.reason
    assert (store_path / "ledger.sqlite3").read_bytes() == before
    receipt = commit_reconciliation(plan.handle)
    assert receipt.operation_id == caught.value.operation_id
    assert receipt.target_identity.namespace.native_id == caught.value.session_id
    assert aliases[0].read_bytes() == alias_bytes
    assert title_receipt_path(state.built.manifest).exists()
    completed_bytes = (store_path / "ledger.sqlite3").read_bytes()
    historical = prepare_reconciliation(receipt.operation_id, store_path=store_path)
    assert historical.status == "completed" and historical.receipt == receipt
    assert (store_path / "ledger.sqlite3").read_bytes() == completed_bytes
    assert len(list(aliases[0].parent.glob("*.json"))) == 1
    catalog.close()


@pytest.mark.parametrize(
    "change", ["extra", "false_return", "count", "warning", "duplicate", "body"]
)
def test_summary_manifest_v3_rejects_unknown_and_nonmetadata_wire(tmp_path, change):
    catalog, observer, _, destination, _, _, reviewed, options, env = fixture(tmp_path)
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    document = prepared._state.built.artifact.manifest(output_path=prepared._state.built.output)
    if change == "extra":
        document["unknown"] = True
    elif change == "false_return":
        document["return_link"] = False
    elif change == "count":
        document["authority_source"]["events"]["message"] = True
    elif change == "warning":
        document["warnings"][0]["body"] = "untrusted body"
    elif change == "body":
        document["conversion_input"]["text"] = "untrusted body"
    payload = json.dumps(document).encode()
    if change == "duplicate":
        payload = payload[:-1] + b',"schema_version":3}'
    with pytest.raises(SessionMigrateError):
        decode_derived_manifest(payload)
    assert not (tmp_path / ".local/state/session-migrate/lineage-v1").exists()
    prepared.discard()
    catalog.close()


@pytest.mark.parametrize(
    "source_format,locator,accepted",
    [
        ("opencode", "matching", True),
        ("opencode", "absolute", True),
        ("opencode", "different_id", False),
        ("opencode", "relative_file", False),
        ("claude", "matching", False),
        ("codex", "matching", False),
    ],
)
def test_summary_manifest_metadata_accepts_only_matching_virtual_source_locator(
    tmp_path, source_format, locator, accepted
):
    # These are decoder inputs only. Editing wire metadata issues no native authority.
    catalog, observer, _, destination, _, _, reviewed, options, env = fixture(tmp_path)
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    try:
        document = prepared._state.built.artifact.manifest(output_path=prepared._state.built.output)
        source = document["authority_source"]
        source["format"] = source_format
        source["identity"]["namespace"]["format"] = source_format
        source["path"] = {
            "matching": f"opencode:{source['session_id']}",
            "absolute": str(tmp_path / "original-export.json"),
            "different_id": "opencode:another-session",
            "relative_file": "original-export.json",
        }[locator]
        payload = json.dumps(document).encode()
        if accepted:
            assert decode_derived_manifest(payload) == document
        else:
            with pytest.raises(SessionMigrateError):
                decode_derived_manifest(payload)
        assert prepared._state.built.artifact.source.source_format is AgentFormat.CLAUDE
        assert not (tmp_path / ".local/state/session-migrate/lineage-v1").exists()
    finally:
        prepared.discard()
        catalog.close()


@pytest.mark.parametrize("endpoint", ["source", "target", "manifest"])
@pytest.mark.parametrize("when", ["before_rename", "after_rename"])
def test_ack_completion_rechecks_whole_native_and_metadata_after_io(
    tmp_path, monkeypatch, endpoint, when
):
    catalog, observer, _, destination, _, source_path, reviewed, options, env = fixture(tmp_path)
    prepared = prepare_summary(reviewed, destination, options, observer, base_environ=env)
    state = prepared._state
    calls = []
    original_install = operations._install
    original_complete = LineageStore.complete_reconciled
    mutated = []

    def install(*args):
        if not args[-1]:
            calls.append("published")
        return original_install(*args)

    def complete(store, *args, **kwargs):
        def fault(event):
            if event != when or mutated:
                return
            path = {
                "source": source_path,
                "target": state.built.output,
                "manifest": state.built.manifest,
            }[endpoint]
            before = path.stat()
            data = path.read_bytes()
            replacement = (
                data.replace(b"unknown", b"changed")
                if endpoint == "target"
                else data.replace(b"original", b"modified")
                if endpoint == "source"
                else data + b" "
            )
            path.write_bytes(replacement)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            mutated.append(event)

        store._fault_hook = fault
        try:
            return original_complete(store, *args, **kwargs)
        finally:
            store._fault_hook = None

    monkeypatch.setattr(operations, "_install", install)
    monkeypatch.setattr(LineageStore, "complete_reconciled", complete)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert mutated == [when] and calls == ["published"]
    view = LineageStore.inspect_existing(state.migration_state_home / "lineage-v1")
    op = next(op for op in view.operations if op.record.operation_id == caught.value.operation_id)
    assert op.record.target_namespace.native_id == caught.value.session_id
    if when == "after_rename":
        assert caught.value.code == "persistence_uncertain"
        assert op.durable_state is OperationState.COMPLETED and op.receipt is not None
        assert not caught.value.receipt.ledger_recorded
    else:
        assert op.durable_state is OperationState.NEEDS_RECONCILE and op.receipt is not None
        assert caught.value.receipt.ledger_recorded
    with pytest.raises(SessionMigrateError):
        commit_operation(prepared)
    assert calls == ["published"]
    catalog.close()
