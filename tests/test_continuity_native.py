"""Cold public exports composed with real migration receipts; no model calls."""

import json

import pytest

pytest.importorskip("textual", reason="native composition fixtures require tui extra")
import test_tui_native
from test_tui_native import setup_case

from session_migrate.canonical import CanonicalPolicy
from session_migrate.catalog import Catalog
from session_migrate.continuity import ContinuitySelector, ContinuityStatus, assess_continuity
from session_migrate.continuity_bindings import EXECUTION_ENVIRONMENT, PUBLIC_ENVIRONMENT
from session_migrate.continuity_observation import EndpointObserver, observe_endpoint
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import OperationNeedsReconcileError
from session_migrate.lineage import (
    Coverage,
    LineageStore,
    NativeState,
    OperationState,
    ReconcileReceipt,
)
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import OperationKind, OperationRequest, commit_operation
from session_migrate.profiles import Profile, SessionRef, prepare_profile_operation

title_stock = test_tui_native.title_stock


@pytest.mark.parametrize(
    "source,target", [("claude", "opencode"), ("codex", "opencode"), ("opencode", "claude")]
)
def test_real_receipt_continuity_reopens_exact_native_store(tmp_path, title_stock, source, target):
    case = setup_case(tmp_path, title_stock, source, target)
    if source == "claude":
        values = [
            {
                "type": role,
                "uuid": f"synthetic-{index}",
                "parentUuid": "synthetic-0" if index else None,
                "sessionId": case.native_id,
                "cwd": str(case.project),
                "version": "2.1.209",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
            }
            for index, (role, text) in enumerate(
                (("user", "Synthetic request"), ("assistant", "Synthetic response"))
            )
        ]
        case.source_path.write_bytes(
            b"".join(json.dumps(value).encode() + b"\n" for value in values)
        )
        with Catalog(case.controller.catalog) as catalog:
            assert catalog.refresh(include_auto=False).root_errors == 0
    public_keys = {"HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"}
    source_profile = Profile(
        case.source_id,
        "Synthetic source",
        AgentFormat(source),
        case.source_root,
        cli=title_stock.cli if source == "opencode" else None,
        environ={key: value for key, value in case.source_env.items() if key in public_keys},
    )
    target_profile = Profile(
        "native-target",
        "Synthetic target",
        AgentFormat(target),
        case.target_root,
        cli=title_stock.cli if target == "opencode" else None,
        environ={key: value for key, value in case.target_env.items() if key in public_keys},
    )
    ref = SessionRef(
        source_profile.format,
        source_profile.root,
        case.native_id,
        source_profile.id,
        profile_fingerprint=source_profile.fingerprint,
    )
    request = OperationRequest(
        OperationKind.TRANSFER,
        target_format=TargetFormat(target),
        cwd=case.project,
        target_cli_version=title_stock.release if target == "opencode" else None,
        model_provider="synthetic" if target == "opencode" else None,
        model="offline" if target == "opencode" else None,
    )
    with Catalog(case.controller.catalog) as catalog:
        prepared = prepare_profile_operation(
            catalog,
            source_profile,
            ref,
            target_profile,
            request,
            base_environ=case.source_env,
        )
        if source == "claude":
            # The stock importer adds a non-empty session model variant. The
            # parser declares that context loss, so a formerly verified source
            # cannot silently become a verified destination checkpoint.
            with pytest.raises(OperationNeedsReconcileError) as caught:
                commit_operation(prepared)
            assert caught.value.native_state is NativeState.ACKNOWLEDGED
            operation_id = caught.value.operation_id
            receipt = None
        else:
            receipt = commit_operation(prepared)
            assert receipt.lineage_receipt is not None
            operation_id = receipt.lineage_receipt.operation_id
        catalog.add_root(target_profile.format, target_profile.root)
        assert catalog.refresh(include_auto=False).root_errors == 0
    store_path = default_migration_state_home(environ=case.source_env) / "lineage-v1"
    before = LineageStore.inspect_existing(store_path)
    with Catalog(case.controller.catalog) as catalog:
        observer = EndpointObserver(
            catalog,
            (source_profile, target_profile),
            base_environ={
                key: value
                for key, value in case.source_env.items()
                if key in PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT
            },
        )
        selector = ContinuitySelector(operation_id=operation_id)
        first = assess_continuity(selector, observer, store_path=store_path)
        second = assess_continuity(selector, observer, store_path=store_path)
    # These real native fixture routes retain known projection losses. A
    # completed import is not evidence that continuity coverage was verified.
    if source == "claude":
        assert first.status is second.status is ContinuityStatus.NEEDS_RECONCILE
        operation = before.operations[0]
        assert operation.durable_state is OperationState.NEEDS_RECONCILE
        assert operation.durable_native_state is NativeState.ACKNOWLEDGED
        assert operation.record.expected_projection.coverage is Coverage.VERIFIED
        assert type(operation.receipt) is ReconcileReceipt
        assert operation.source_checkpoint is operation.target_checkpoint is None
        assert not before.edges
    else:
        assert receipt.lineage_receipt.continuity_reason == "coverage_unverified"
        assert first.status is ContinuityStatus.UNTRACKED
        assert second.status is ContinuityStatus.UNTRACKED
        assert first.reason == second.reason == "coverage_unverified"
    assert "update" not in first.actions
    assert LineageStore.inspect_existing(store_path) == before
    with Catalog(case.controller.catalog) as catalog:
        observer = EndpointObserver(
            catalog,
            (source_profile, target_profile),
            base_environ={
                key: value
                for key, value in case.source_env.items()
                if key in PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT
            },
        )
        profile = source_profile if source == "opencode" else target_profile
        namespace = (
            before.operations[0].record.target_namespace
            if target == "opencode"
            else before.operations[0].record.source.identity.namespace
        )
        op_ref = SessionRef(
            profile.format,
            profile.root,
            namespace.native_id,
            profile.id,
            profile_fingerprint=profile.fingerprint,
        )
        first_export = observe_endpoint(
            observer, op_ref, client_version=title_stock.release, canonical_policy=CanonicalPolicy()
        )
        second_export = observe_endpoint(
            observer, op_ref, client_version=title_stock.release, canonical_policy=CanonicalPolicy()
        )
    if source == "claude":
        assert first_export.observation.coverage is Coverage.VERIFIED
        assert first_export.observation.fidelity is Coverage.PARTIAL
        assert any(
            atom.category == "known-loss" and atom.path == ("info", "model", "variant")
            for atom in first_export.observation.audit.atoms
        )
    assert first_export.identity == second_export.identity
    assert first_export.authority_sha256 == second_export.authority_sha256
    assert "native_export_read" in first_export.native_read_effects
    assert "native_cache_or_wal_possible" in first_export.native_read_effects
    assert LineageStore.inspect_existing(store_path) == before
