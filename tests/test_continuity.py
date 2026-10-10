"""Synthetic native-layout files and private ledger images; no live clients."""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.canonical import CanonicalPolicy
from session_migrate.catalog import Catalog
from session_migrate.continuity import (
    ContinuityError,
    ContinuitySelector,
    ContinuityStatus,
    ExplicitPair,
    _snapshot_sha,
    assess_continuity,
    inspect_lineage,
)
from session_migrate.continuity_bindings import (
    PUBLIC_ENVIRONMENT,
    LegacyRuntimeContext,
    ProfileBindingKind,
    RuntimeBindingError,
    _legacy_runtime_environment,
    _legacy_runtime_fingerprint,
)
from session_migrate.continuity_observation import (
    ContinuityObservationError,
    EndpointObservation,
    EndpointObserver,
    observe_endpoint,
    ref_for_identity,
    validate_endpoint,
)
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import claude
from session_migrate.lineage import (
    CheckpointRecord,
    CompletionRecord,
    Coverage,
    LineageStore,
    OperationRecord,
    PolicyVersions,
    ProjectionExpectation,
    SourceMetadata,
)
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_source
from session_migrate.profiles import Profile, SessionRef
from session_migrate.projection_policies import expected_from_source

SOURCE_ID = "50000000-0000-4000-8000-000000000001"
TARGET_ID = "50000000-0000-4000-8000-000000000002"
CLIENT = "2.1.209"
POLICY = CanonicalPolicy()
VERSIONS = PolicyVersions(
    "canonical-v2",
    "portable-projection-v1",
    "native-audit-v1",
    "native-audit-v1",
    CLIENT,
    CLIENT,
)


def records(count=2):
    return [
        {
            "type": "user" if i % 2 == 0 else "assistant",
            "uuid": f"synthetic-{i}",
            "parentUuid": f"synthetic-{i - 1}" if i else None,
            "sessionId": SOURCE_ID,
            "cwd": "/synthetic/work",
            "version": CLIENT,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": [{"type": "text", "text": f"synthetic-{i}"}],
            },
        }
        for i in range(count)
    ]


def write_records(path, values):
    path.write_text("".join(json.dumps(v) + "\n" for v in values))


def checkpoint(endpoint):
    obs = endpoint.observation
    return CheckpointRecord(
        endpoint.identity,
        endpoint.authority_sha256,
        _snapshot_sha(obs.source_prefix, VERSIONS, POLICY),
        (),
        0,
        (),
        False,
        Coverage.VERIFIED,
        None,
        VERSIONS,
        obs.source_prefix,
    )


def setup(tmp_path, count=2, complete=True, source_records=None, target_id=TARGET_ID):
    tmp_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path.chmod(0o700)
    roots = (tmp_path / "source", tmp_path / "target")
    source_path = (
        roots[0]
        / "projects"
        / claude.project_directory_name(Path("/synthetic/work"))
        / f"{SOURCE_ID}.jsonl"
    )
    source_path.parent.mkdir(parents=True)
    write_records(source_path, source_records if source_records is not None else records(count))
    snap = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, source_path))
    artifact = convert_session(
        snap.parse(),
        ConversionOptions(TargetFormat.CLAUDE, session_id=target_id, cwd=Path("/synthetic/work")),
    )
    target_path = (
        roots[1] / "projects" / claude.project_directory_name(artifact.cwd) / f"{target_id}.jsonl"
    )
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(artifact.native_bytes)
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(claude_roots=roots, include_auto=False)
    profiles = tuple(
        Profile(name, name, AgentFormat.CLAUDE, root)
        for name, root in zip(("source-profile", "target-profile"), roots, strict=True)
    )
    observer = EndpointObserver(catalog, profiles)
    refs = tuple(
        SessionRef(p.format, p.root, native_id, p.id, profile_fingerprint=p.fingerprint)
        for p, native_id in zip(profiles, (SOURCE_ID, target_id), strict=True)
    )
    endpoints = tuple(
        observe_endpoint(observer, ref, client_version=CLIENT, canonical_policy=POLICY)
        for ref in refs
    )
    cps = tuple(checkpoint(ep) for ep in endpoints)
    expected = expected_from_source(endpoints[0].observation, target_format=TargetFormat.CLAUDE)
    record = OperationRecord(
        "synthetic-operation",
        SourceMetadata(
            endpoints[0].identity,
            snap.authority_sha256,
            "a" * 64,
            cps[0].snapshot_sha256,
            cps[0].prefix_witness,
        ),
        endpoints[1].identity.namespace,
        profiles[1].fingerprint,
        "b" * 64,
        artifact.target_sha256,
        VERSIONS,
        ProjectionExpectation(
            cps[1].snapshot_sha256,
            (),
            Coverage.VERIFIED,
            cps[1].prefix_witness,
            expected.relation.chain_sha256,
        ),
        (),
    )
    store_path = tmp_path / "ledger"
    with (
        LineageStore.create_new(store_path) as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        if complete:
            store.begin_publication(record.operation_id, guard)
            if complete is True:
                store.mark_verifying(record.operation_id, guard)
                store.complete(
                    record.operation_id,
                    CompletionRecord(
                        endpoints[1].identity,
                        artifact.target_sha256,
                        cps[0],
                        cps[1],
                        (),
                    ),
                    guard,
                )
    return catalog, observer, refs, source_path, target_path, store_path


def test_publishing_recovery_is_not_a_failed_retry_or_republication(tmp_path):
    fixture = setup(tmp_path, complete="publishing")
    try:
        before = tree_state(tmp_path)
        result = assessment(fixture)
        assert result.status is ContinuityStatus.NEEDS_RECONCILE
        assert result.actions == ("inspect", "plan_reconcile")
        assert tree_state(tmp_path) == before
    finally:
        fixture[0].close()


def test_ledger_recheck_rejects_concurrent_state_change(tmp_path, monkeypatch):
    fixture = setup(tmp_path)
    try:
        import session_migrate.continuity as module

        inspect = module.LineageStore.inspect_existing
        calls = 0

        def changing(path):
            nonlocal calls
            calls += 1
            value = inspect(path)
            return replace(value, store_uuid="b" * 32) if calls == 2 else value

        monkeypatch.setattr(module.LineageStore, "inspect_existing", changing)
        result = assessment(fixture)
        assert (
            result.status is ContinuityStatus.NEEDS_RECONCILE and result.reason == "ledger_changed"
        )
    finally:
        fixture[0].close()


def test_namespace_root_profile_change_is_not_uuid_deduplication(tmp_path):
    fixture = setup(tmp_path)
    try:
        wrong_profiles = tuple(
            Profile(
                p.id,
                p.name,
                p.format,
                p.root,
                environ={"HOME": str(tmp_path / "different-home")},
            )
            for p in fixture[1]._profiles
        )
        observer = EndpointObserver(fixture[0], wrong_profiles)
        result = assess_continuity(
            ContinuitySelector(operation_id="synthetic-operation"),
            observer,
            fixture[-1],
        )
        assert result.status is ContinuityStatus.UNTRACKED and result.reason == "identity_unproven"
    finally:
        fixture[0].close()


def test_identical_native_id_in_two_roots_still_has_distinct_qualified_identities(
    tmp_path,
):
    fixture = setup(tmp_path, target_id=SOURCE_ID)
    try:
        inspection = inspect_lineage(
            ContinuitySelector(operation_id="synthetic-operation"), fixture[-1]
        )
        source = inspection.operation.source_checkpoint.identity
        target = inspection.operation.target_checkpoint.identity
        assert source.namespace.native_id == target.namespace.native_id
        assert source.namespace.canonical_root != target.namespace.canonical_root
        assert source != target
        assert assessment(fixture).status is ContinuityStatus.NO_OP
    finally:
        fixture[0].close()


def test_same_semantic_units_with_changed_raw_graph_do_not_fake_no_op(tmp_path):
    fixture = setup(tmp_path)
    try:
        values = records()
        values.append({"type": "last-prompt", "leafUuid": "synthetic-1", "sessionId": SOURCE_ID})
        write_records(fixture[3], values)
        result = assessment(fixture)
        assert result.source_comparison.matches
        assert not result.source_structure_matches
        assert (
            result.status is ContinuityStatus.LINEAGE_BROKEN
            and result.reason == "structure_changed"
        )
    finally:
        fixture[0].close()


def test_tool_input_changed_with_equal_event_dataclass_breaks_prefix(tmp_path):
    values = records()
    values[0]["message"]["content"] = [
        {
            "type": "tool_use",
            "name": "synthetic_tool",
            "id": "synthetic-call",
            "input": {"count": 1},
        }
    ]
    values[1]["message"]["content"] = [
        {
            "type": "tool_result",
            "tool_use_id": "synthetic-call",
            "content": "synthetic result",
            "is_error": False,
        }
    ]
    values[0]["type"] = values[0]["message"]["role"] = "assistant"
    values[1]["type"] = values[1]["message"]["role"] = "user"
    fixture = setup(tmp_path, source_records=values)
    try:
        assert assessment(fixture).status is ContinuityStatus.NO_OP
        values[0]["message"]["content"] = [
            {
                "type": "tool_use",
                "name": "synthetic_tool",
                "id": "synthetic-call",
                "input": {"count": 2},
            }
        ]
        values[1]["message"]["content"] = [
            {
                "type": "tool_result",
                "tool_use_id": "synthetic-call",
                "content": "synthetic result",
                "is_error": False,
            }
        ]
        values[0]["type"] = values[0]["message"]["role"] = "assistant"
        values[1]["type"] = values[1]["message"]["role"] = "user"
        write_records(fixture[3], values)
        snapshot = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, fixture[3]))
        e = snapshot.parse().events[0]
        other = replace(e, payload={"input": {"count": 3}})
        assert e == other
        assert assessment(fixture).status is ContinuityStatus.LINEAGE_BROKEN
    finally:
        fixture[0].close()


def test_multiple_exact_pair_edges_are_ambiguous_not_latest(tmp_path, monkeypatch):
    fixture = setup(tmp_path)
    try:
        first = inspect_lineage(
            ContinuitySelector(operation_id="synthetic-operation"), fixture[-1]
        ).operation
        import session_migrate.continuity as module

        # Schema1 reserves each target ID once. A mocked ambiguous view tests
        # only rejection, without manufacturing any positive authority.
        view = LineageStore.inspect_existing(fixture[-1])
        second = replace(first, record=replace(first.record, operation_id="synthetic-newer"))
        monkeypatch.setattr(
            module.LineageStore,
            "inspect_existing",
            lambda _: replace(view, operations=(first, second)),
        )
        selector = ContinuitySelector(pair=ExplicitPair(*fixture[2]))
        inspection = inspect_lineage(selector, fixture[-1], observer=fixture[1])
        assert inspection.candidate_count == 2 and inspection.reason == "ambiguous_baseline"
        assert (
            assess_continuity(selector, fixture[1], fixture[-1]).status
            is ContinuityStatus.UNTRACKED
        )
    finally:
        fixture[0].close()


def test_policy_and_receipt_fields_are_checked_before_observation(tmp_path, monkeypatch):
    fixture = setup(tmp_path)
    try:
        import session_migrate.continuity as module

        actual = module.LineageStore.inspect_existing
        original = actual(fixture[-1])
        item = original.operations[0]
        changed = replace(
            item,
            record=replace(item.record, versions=replace(VERSIONS, projection_policy="other-v1")),
        )
        monkeypatch.setattr(
            module.LineageStore,
            "inspect_existing",
            lambda _: replace(original, operations=(changed,)),
        )
        result = assessment(fixture)
        assert (
            result.status is ContinuityStatus.LINEAGE_BROKEN and result.reason == "policy_changed"
        )
        changed = replace(item, receipt=replace(item.receipt, input_artifact_sha256="b" * 64))
        result = assessment(fixture)
        assert (
            result.status is ContinuityStatus.LINEAGE_BROKEN
            and result.reason == "receipt_binding_changed"
        )
    finally:
        fixture[0].close()


@pytest.mark.parametrize("fail_export", [False, True])
@pytest.mark.parametrize("legacy_binding", [False, True])
def test_opencode_observer_uses_issued_export_proof_and_declares_read_effects(
    tmp_path, monkeypatch, fail_export, legacy_binding
):
    fixture = setup(tmp_path / "seed")
    try:
        import session_migrate.continuity_observation as module
        from session_migrate import conversion
        from session_migrate.catalog import CatalogTransferSource

        root = tmp_path / (".local/share/opencode" if legacy_binding else "data/opencode")
        root.mkdir(parents=True)
        (root / "opencode.db").write_bytes(b"synthetic layout marker; mocked export never opens it")
        client = tmp_path / "synthetic-client"
        client.write_text("synthetic executable fixture")
        client.chmod(0o700)
        source = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, fixture[3])).parse()
        artifact = convert_session(
            source,
            ConversionOptions(
                TargetFormat.OPENCODE,
                session_id=SOURCE_ID,
                cwd=Path("/synthetic/work"),
                target_cli_version="2.0.23",
            ),
        )
        selected_environment = {"HOME": str(tmp_path)}
        if not legacy_binding:
            selected_environment["XDG_DATA_HOME"] = str(tmp_path / "data")
        profile = Profile(
            "opencode-profile",
            "opencode-profile",
            AgentFormat.OPENCODE,
            root,
            client,
            selected_environment,
        )
        sparse = {
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "OPENCODE_BIN": str(client),
        }
        observer = EndpointObserver(
            fixture[0],
            (profile,),
            base_environ={"TMPDIR": str(tmp_path)},
            legacy_runtime_contexts=(LegacyRuntimeContext(profile.id, sparse),)
            if legacy_binding
            else (),
        )
        ref = SessionRef(
            AgentFormat.OPENCODE,
            root,
            artifact.session_id,
            profile.id,
            profile_fingerprint=profile.fingerprint,
        )
        monkeypatch.setattr(
            module,
            "resolve_source",
            lambda *args: CatalogTransferSource(
                AgentFormat.OPENCODE, artifact.session_id, root, None
            ),
        )
        monkeypatch.setattr(conversion, "_opencode_version", lambda *args: "2.0.23")

        def export(cli, session_id, path, environ):
            assert session_id == artifact.session_id
            if legacy_binding:
                assert {k: v for k, v in environ.items() if k in PUBLIC_ENVIRONMENT} == sparse
                assert "XDG_DATA_HOME" not in environ
            if fail_export:
                raise module.SessionMigrateError("Synthetic exporter failure")
            path.write_bytes(artifact.native_bytes)

        monkeypatch.setattr(conversion, "_invoke_opencode_export", export)
        attempted = []
        if fail_export:
            with pytest.raises(ContinuityObservationError) as caught:
                observe_endpoint(
                    observer,
                    ref,
                    client_version="2.0.23",
                    canonical_policy=POLICY,
                    on_read_effect=attempted.append,
                )
            assert "native_export_read_attempted" in attempted
            assert "native_cache_or_wal_possible" in caught.value.native_read_effects
            assert "native_export_read" not in caught.value.native_read_effects
            return
        endpoint = observe_endpoint(
            observer,
            ref,
            client_version="2.0.23",
            canonical_policy=POLICY,
            on_read_effect=attempted.append,
        )
        validate_endpoint(endpoint, observer=observer, ref=ref)
        assert endpoint.identity.namespace.native_id == artifact.session_id
        if legacy_binding:
            assert endpoint.profile_binding_kind is ProfileBindingKind.LEGACY_RUNTIME_V1
            assert endpoint.identity.profile_fingerprint == _legacy_runtime_fingerprint(
                profile, sparse
            )
            assert endpoint.current_profile_fingerprint == profile.fingerprint
        assert endpoint.native_read_effects == (
            "native_export_read_attempted",
            "private_scratch_possible",
            "native_cache_or_wal_possible",
            "native_export_read",
            "private_scratch",
        )
        assert not list(tmp_path.glob("session-migrate-opencode-source-*"))
    finally:
        fixture[0].close()


def append_message(path, text="synthetic append"):
    values = [json.loads(line) for line in path.read_text().splitlines()]
    last = next(v for v in reversed(values) if v.get("uuid"))
    values.append(
        {
            "type": "user",
            "uuid": "synthetic-appended",
            "parentUuid": last["uuid"],
            "sessionId": last["sessionId"],
            "cwd": last["cwd"],
            "version": CLIENT,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "user", "content": text},
        }
    )
    write_records(path, values)


def assessment(fixture):
    return assess_continuity(
        ContinuitySelector(operation_id="synthetic-operation"), fixture[1], fixture[-1]
    )


def tree_state(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_no_op_is_issued_from_real_capture_audit_and_layout_without_writes(tmp_path):
    fixture = setup(tmp_path)
    before = tree_state(tmp_path)
    try:
        result = assessment(fixture)
        assert result.status is ContinuityStatus.NO_OP and result.reason == "unchanged"
        assert result.source_comparison.matches and result.target_comparison.matches
        assert (
            result.source_structure_matches
            and result.target_structure_matches
            and result.relation_matches
        )
        assert result.blockers == ("native_boundary_unproven",)
        assert not result.update_available
        assert result.native_read_effects == (
            "file_capture_read",
            "file_capture_read_attempted",
        )
        assert tree_state(tmp_path) == before
    finally:
        fixture[0].close()


@pytest.mark.parametrize("side,reason", [("target", "target_advanced"), ("both", "both_advanced")])
def test_advancement_conflicts_use_each_own_checkpoint(tmp_path, side, reason):
    fixture = setup(tmp_path)
    try:
        append_message(fixture[4])
        if side == "both":
            append_message(fixture[3])
        result = assessment(fixture)
        assert result.status is ContinuityStatus.CONFLICT and result.reason == reason
    finally:
        fixture[0].close()


def test_source_extension_is_not_fake_delta_without_native_closure(tmp_path):
    fixture = setup(tmp_path)
    try:
        append_message(fixture[3])
        result = assessment(fixture)
        assert result.source_comparison.sequence_extends
        assert result.status is ContinuityStatus.UNTRACKED
        assert result.reason == "native_boundary_unproven"
        assert "structure_alignment_unproven" in result.blockers
        assert set(ContinuityStatus) == {
            ContinuityStatus.NO_OP,
            ContinuityStatus.DELTA_CANDIDATE,
            ContinuityStatus.CONFLICT,
            ContinuityStatus.LINEAGE_BROKEN,
            ContinuityStatus.UNTRACKED,
            ContinuityStatus.NEEDS_RECONCILE,
        }
    finally:
        fixture[0].close()


def test_changed_prefix_or_truncated_history_is_lineage_broken(tmp_path):
    fixture = setup(tmp_path)
    try:
        values = records()
        values[0]["message"]["content"][0]["text"] = "synthetic changed"
        write_records(fixture[3], values)
        assert assessment(fixture).status is ContinuityStatus.LINEAGE_BROKEN
        write_records(fixture[3], records(1))
        assert assessment(fixture).status is ContinuityStatus.LINEAGE_BROKEN
    finally:
        fixture[0].close()


def test_recovered_ready_inspects_and_offers_cancel_without_publication(tmp_path):
    fixture = setup(tmp_path, complete=False)
    try:
        before = tree_state(tmp_path)
        result = assessment(fixture)
        assert (
            result.status is ContinuityStatus.NEEDS_RECONCILE and result.reason == "recovered_ready"
        )
        assert result.actions == ("inspect", "cancel_ready")
        assert tree_state(tmp_path) == before
    finally:
        fixture[0].close()


def test_missing_ledger_and_fake_observer_never_create_or_authenticate(tmp_path):
    missing = tmp_path / "missing"
    selector = ContinuitySelector(operation_id="synthetic-operation")
    assert inspect_lineage(selector, missing).reason == "ledger_missing"
    assert not missing.exists()
    fixture = setup(tmp_path / "fixture")
    try:
        result = assess_continuity(selector, lambda _: True, fixture[-1])
        assert (
            result.status is ContinuityStatus.UNTRACKED and result.reason == "observation_unproven"
        )
        with pytest.raises(ContinuityObservationError):
            EndpointObservation()
        ep = observe_endpoint(
            fixture[1], fixture[2][0], client_version=CLIENT, canonical_policy=POLICY
        )
        with pytest.raises(ContinuityObservationError):
            validate_endpoint(copy.copy(ep), observer=fixture[1], ref=fixture[2][0])
        with pytest.raises(ContinuityObservationError):
            validate_endpoint(
                object.__new__(EndpointObservation),
                observer=fixture[1],
                ref=fixture[2][0],
            )
    finally:
        fixture[0].close()


def test_explicit_pair_never_selects_latest_or_unqualified_uuid(tmp_path):
    fixture = setup(tmp_path)
    try:
        selector = ContinuitySelector(pair=ExplicitPair(*fixture[2]))
        assert inspect_lineage(selector, fixture[-1], observer=fixture[1]).candidate_ids == (
            "synthetic-operation",
        )
        assert assess_continuity(selector, fixture[1], fixture[-1]).status is ContinuityStatus.NO_OP
        wrong = replace(fixture[2][1], root=fixture[2][0].root)
        assert (
            inspect_lineage(
                ContinuitySelector(pair=ExplicitPair(fixture[2][0], wrong)),
                fixture[-1],
                observer=fixture[1],
            ).reason
            == "profile_binding_unproven"
        )
        with pytest.raises(ContinuityError):
            ContinuitySelector(operation_id="synthetic-operation", pair=ExplicitPair(*fixture[2]))
        with pytest.raises(ContinuityError):
            ContinuitySelector()
    finally:
        fixture[0].close()


@pytest.mark.parametrize("missing", ["profile_id", "profile_fingerprint"])
def test_explicit_pair_requires_complete_pinned_profile_references(tmp_path, missing):
    fixture = setup(tmp_path)
    try:
        ref = replace(fixture[2][0], **{missing: None})
        with pytest.raises(ContinuityError):
            ExplicitPair(ref, fixture[2][1])
        wrong = replace(fixture[2][0], profile_id="unselected-profile")
        selector = ContinuitySelector(pair=ExplicitPair(wrong, fixture[2][1]))
        assert inspect_lineage(selector, fixture[-1]).reason == "profile_context_required"
        assert inspect_lineage(selector, fixture[-1], observer=fixture[1]).operation is None
        assert (
            assess_continuity(selector, fixture[1], fixture[-1]).status
            is ContinuityStatus.UNTRACKED
        )
    finally:
        fixture[0].close()


def test_assessment_provenance_is_typed_actual_and_content_free(tmp_path):
    fixture = setup(tmp_path)
    try:
        result = assessment(fixture)
        inspected = inspect_lineage(
            ContinuitySelector(operation_id="synthetic-operation"), fixture[-1]
        )
        assert result.source_identity == inspected.operation.source_checkpoint.identity
        assert result.target_identity == inspected.operation.target_checkpoint.identity
        assert (
            result.source_checkpoint_sha256
            and result.target_checkpoint_sha256
            and result.receipt_sha256
        )
        assert result.versions == VERSIONS and result.canonical_policy_sha256 == POLICY.sha256
        assert result.source_coverage is result.target_coverage is Coverage.VERIFIED
        assert result.source_fidelity is result.target_fidelity is Coverage.VERIFIED
        with pytest.raises(ContinuityError):
            replace(result, source_checkpoint_sha256="not-a-hash")
        with pytest.raises(ContinuityError):
            replace(result, source_coverage="verified")
    finally:
        fixture[0].close()


def test_first_completed_capture_effects_are_retained_when_target_fails(tmp_path, monkeypatch):
    fixture = setup(tmp_path)
    try:
        import session_migrate.continuity as module

        actual = module.observe_endpoint
        calls = 0

        def capture(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ContinuityObservationError("endpoint_unavailable")
            return actual(*args, **kwargs)

        monkeypatch.setattr(module, "observe_endpoint", capture)
        result = assessment(fixture)
        assert result.status is ContinuityStatus.UNTRACKED and calls == 2
        assert "file_capture_read" in result.native_read_effects
        assert result.source_identity is not None and result.target_identity is None
        assert result.source_coverage is Coverage.VERIFIED and result.target_coverage is None
        assert result.observed_end_ns >= result.observed_start_ns
    finally:
        fixture[0].close()


def test_observation_change_during_double_capture_is_detected(tmp_path, monkeypatch):
    fixture = setup(tmp_path)
    try:
        import session_migrate.continuity as module

        actual = module.observe_endpoint
        count = 0

        def capture(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                append_message(fixture[3])
            return actual(*args, **kwargs)

        monkeypatch.setattr(module, "observe_endpoint", capture)
        result = assessment(fixture)
        assert (
            result.status is ContinuityStatus.UNTRACKED and result.reason == "observation_changed"
        )
        assert count == 4
    finally:
        fixture[0].close()


def test_unknown_raw_field_prevents_no_op_even_with_same_ir_text(tmp_path):
    fixture = setup(tmp_path)
    try:
        values = records()
        values[0]["unknown"] = {"synthetic": True}
        write_records(fixture[3], values)
        result = assessment(fixture)
        assert (
            result.status is ContinuityStatus.UNTRACKED and result.reason == "coverage_unverified"
        )
    finally:
        fixture[0].close()


def test_ten_thousand_events_compare_complete_chains_not_diagnostics(tmp_path):
    fixture = setup(tmp_path, count=10_000)
    try:
        result = assessment(fixture)
        assert result.status is ContinuityStatus.NO_OP
        assert (
            result.source_comparison.current_count
            == result.target_comparison.current_count
            == 10_000
        )
        values = records(10_000)
        values[-1]["message"]["content"][0]["text"] = "synthetic last changed"
        write_records(fixture[3], values)
        assert assessment(fixture).status is ContinuityStatus.LINEAGE_BROKEN
    finally:
        fixture[0].close()


def actual_b3_fixture(tmp_path, entrypoint):
    """Run the installed B3 file pipeline only inside private synthetic roots."""
    from session_migrate.operations import (
        OperationRequest,
        commit_operation,
        default_migration_state_home,
        run_operation,
    )
    from session_migrate.profiles import prepare_profile_operation

    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    for key in (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
    ):
        directory = tmp_path / key.lower()
        directory.mkdir(mode=0o700)
        environment[key] = str(directory)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    source_root, target_root = tmp_path / "source-native", tmp_path / "target-native"
    source_path = (
        source_root / "projects" / claude.project_directory_name(work) / f"{SOURCE_ID}.jsonl"
    )
    source_path.parent.mkdir(parents=True, mode=0o700)
    values = records()
    for value in values:
        value["cwd"] = str(work)
    write_records(source_path, values)
    source_raw = source_path.read_bytes()
    profiles = (
        Profile("source-native", "Source native", AgentFormat.CLAUDE, source_root),
        Profile("target-native", "Target native", AgentFormat.CODEX, target_root),
    )
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(claude_roots=(source_root,), include_auto=False)
    common = {
        "target_format": TargetFormat.CODEX,
        "session_id": TARGET_ID,
        "cwd": work,
        "target_cli_version": "0.144.4",
    }
    if entrypoint == "legacy":
        result = run_operation(
            OperationRequest(
                "transfer",
                source_id=SOURCE_ID,
                source_format=AgentFormat.CLAUDE,
                source_home=source_root,
                home=target_root,
                **common,
            ),
            environ=environment,
        )
    else:
        ref = SessionRef(
            profiles[0].format,
            source_root,
            SOURCE_ID,
            profiles[0].id,
            profile_fingerprint=profiles[0].fingerprint,
        )
        prepared = prepare_profile_operation(
            catalog,
            profiles[0],
            ref,
            profiles[1],
            OperationRequest("transfer", **common),
            base_environ=environment,
        )
        result = commit_operation(prepared)
    receipt = result.lineage_receipt
    assert receipt is not None and receipt.continuity_reason == "tracked"
    assert source_path.read_bytes() == source_raw
    ledger = default_migration_state_home(environ=environment) / "lineage-v1"
    catalog.refresh(claude_roots=(source_root,), codex_roots=(target_root,), include_auto=False)
    contexts = (
        tuple(
            LegacyRuntimeContext(
                p.id, {k: v for k, v in environment.items() if k in PUBLIC_ENVIRONMENT}
            )
            for p in profiles
        )
        if entrypoint == "legacy"
        else ()
    )
    observer = EndpointObserver(
        catalog, profiles, base_environ=environment, legacy_runtime_contexts=contexts
    )
    operation = LineageStore.inspect_existing(ledger).operations[0]
    return catalog, observer, ledger, operation, environment, profiles, source_path


@pytest.mark.parametrize("entrypoint", ["legacy", "profiles"])
def test_actual_b3_receipt_resolves_current_profiles_and_preserves_digest_axes(
    tmp_path, entrypoint
):
    catalog, observer, ledger, operation, env, profiles, _ = actual_b3_fixture(tmp_path, entrypoint)
    try:
        before = tree_state(ledger)
        selector = ContinuitySelector(operation_id=operation.record.operation_id)
        result = assess_continuity(selector, observer, ledger)
        assert result.status is ContinuityStatus.NO_OP
        expected_kind = (
            ProfileBindingKind.LEGACY_RUNTIME_V1
            if entrypoint == "legacy"
            else ProfileBindingKind.PROFILE_V1
        )
        assert (
            result.source_profile_binding_kind
            is result.target_profile_binding_kind
            is expected_kind
        )
        assert result.source_current_profile_fingerprint == profiles[0].fingerprint
        assert result.target_current_profile_fingerprint == profiles[1].fingerprint
        refs = tuple(
            ref_for_identity(observer, cp.identity)
            for cp in (operation.source_checkpoint, operation.target_checkpoint)
        )
        assert [ref.profile_id for ref in refs] == [p.id for p in profiles]
        pair = ContinuitySelector(pair=ExplicitPair(*refs))
        assert inspect_lineage(pair, ledger, observer=observer).operation is not None
        assert assess_continuity(pair, observer, ledger).status is ContinuityStatus.NO_OP
        if entrypoint == "legacy":
            assert (
                result.source_identity.profile_fingerprint
                != result.source_current_profile_fingerprint
            )
            assert result.source_identity.profile_fingerprint == _legacy_runtime_fingerprint(
                profiles[0], env
            )
            no_context = EndpointObserver(catalog, profiles, base_environ=env)
            assert (
                assess_continuity(selector, no_context, ledger).status is ContinuityStatus.UNTRACKED
            )
        assert tree_state(ledger) == before
    finally:
        catalog.close()


@pytest.mark.parametrize("changed_side", ["source", "target", "both"])
def test_actual_legacy_receipt_preserves_advance_classification(tmp_path, changed_side):
    catalog, observer, ledger, operation, _, profiles, source = actual_b3_fixture(
        tmp_path, "legacy"
    )
    try:
        target = next(profiles[1].root.rglob(f"*{TARGET_ID}*.jsonl"))
        if changed_side in ("source", "both"):
            append_message(source)
        if changed_side in ("target", "both"):
            # Codex append uses its actual response_item grammar, not Claude records.
            with target.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:01Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "synthetic append",
                            },
                        }
                    )
                    + "\n"
                )
                stream.write(
                    json.dumps(
                        {
                            "timestamp": "2026-01-01T00:00:01Z",
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "synthetic append"}],
                            },
                        }
                    )
                    + "\n"
                )
        result = assess_continuity(
            ContinuitySelector(operation_id=operation.record.operation_id),
            observer,
            ledger,
        )
        if changed_side == "source":
            assert result.status is ContinuityStatus.UNTRACKED
            assert result.reason == "native_boundary_unproven"
        else:
            assert result.status is ContinuityStatus.CONFLICT, result.reason
            assert result.reason == (
                "both_advanced" if changed_side == "both" else "target_advanced"
            )
    finally:
        catalog.close()


@pytest.mark.parametrize("change", ["environment", "profile_mutation", "body", "root", "ambiguous"])
def test_actual_legacy_receipt_rejects_stale_or_ambiguous_binding(tmp_path, change):
    catalog, observer, ledger, operation, env, profiles, source = actual_b3_fixture(
        tmp_path, "legacy"
    )
    try:
        if change == "environment":
            sparse = {k: v for k, v in env.items() if k in PUBLIC_ENVIRONMENT}
            sparse["XDG_RUNTIME_DIR"] = str(tmp_path / "different-runtime")
            observer = EndpointObserver(
                catalog,
                profiles,
                base_environ=env,
                legacy_runtime_contexts=tuple(LegacyRuntimeContext(p.id, sparse) for p in profiles),
            )
        elif change == "profile_mutation":
            object.__setattr__(profiles[0], "name", "Changed current profile")
        elif change == "body":
            values = [json.loads(line) for line in source.read_text().splitlines()]
            values[0]["message"]["content"][0]["text"] = "changed private synthetic body"
            write_records(source, values)
        elif change == "root":
            root = tmp_path / "different-root"
            root.mkdir()
            changed = replace(profiles[0], root=root)
            observer = EndpointObserver(
                catalog,
                (changed, profiles[1]),
                base_environ=env,
                legacy_runtime_contexts=observer._legacy_runtime_contexts,
            )
        else:
            alias = replace(
                profiles[0],
                id="different-current-profile",
                name="Another current profile",
            )
            observer = EndpointObserver(
                catalog,
                profiles + (alias,),
                base_environ=env,
                legacy_runtime_contexts=observer._legacy_runtime_contexts
                + (
                    LegacyRuntimeContext(
                        alias.id, observer._legacy_runtime_contexts[0].environment
                    ),
                ),
            )
        result = assess_continuity(
            ContinuitySelector(operation_id=operation.record.operation_id),
            observer,
            ledger,
        )
        assert result.status in (
            ContinuityStatus.UNTRACKED,
            ContinuityStatus.LINEAGE_BROKEN,
        )
        assert result.status is not ContinuityStatus.NO_OP
    finally:
        catalog.close()


def test_sparse_runtime_environment_exact_codec_does_not_inject_profile_defaults(
    tmp_path,
):
    from session_migrate.operations import _digest

    root = tmp_path / "source"
    root.mkdir()
    profile = Profile("source", "Source", AgentFormat.CLAUDE, root, environ={"HOME": str(tmp_path)})
    sparse = {
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "OPENCODE_BIN": str(tmp_path / "synthetic-op"),
        "KILO_BIN": str(tmp_path / "synthetic-kilo"),
    }
    ctx = LegacyRuntimeContext(profile.id, sparse)
    values = _legacy_runtime_environment(
        profile,
        ctx,
        {
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(tmp_path / "ignored-base"),
        },
    )
    assert "XDG_DATA_HOME" not in values
    assert set(values) == set(sparse) | {"PATH", "LANG"}
    assert _legacy_runtime_fingerprint(profile, values) == _digest(
        {"format": "claude", "root": str(root), "environment": sparse}
    )
    assert _legacy_runtime_fingerprint(
        profile, {**values, "PATH": "/other"}
    ) == _legacy_runtime_fingerprint(profile, values)
    for key in ("XDG_RUNTIME_DIR", "OPENCODE_BIN", "KILO_BIN"):
        altered = {**values, key: values[key] + "-changed"}
        assert _legacy_runtime_fingerprint(profile, altered) != _legacy_runtime_fingerprint(
            profile, values
        )
    with pytest.raises(RuntimeBindingError):
        _legacy_runtime_environment(profile, LegacyRuntimeContext(profile.id, {}), {})
    with pytest.raises(RuntimeBindingError):
        LegacyRuntimeContext(profile.id, {"UNDECLARED_TOKEN": "synthetic-value"})


def test_legacy_opencode_sparse_runtime_validates_root_and_selected_cli(tmp_path):
    home = tmp_path / "home"
    root = home / ".local/share/opencode"
    root.mkdir(parents=True)
    cli = tmp_path / "synthetic-cli"
    cli.write_text("synthetic executable fixture")
    cli.chmod(0o700)
    profile = Profile("op", "OpenCode", AgentFormat.OPENCODE, root, cli, {"HOME": str(home)})
    sparse = {"HOME": str(home), "OPENCODE_BIN": str(cli)}
    values = _legacy_runtime_environment(profile, LegacyRuntimeContext(profile.id, sparse), {})
    assert values == sparse and "XDG_DATA_HOME" not in values
    other = tmp_path / "other-cli"
    other.write_text("synthetic unrelated executable fixture")
    for changes in (
        {"OPENCODE_BIN": str(other)},
        {"HOME": str(tmp_path)},
        {"XDG_DATA_HOME": str(tmp_path)},
    ):
        with pytest.raises((RuntimeBindingError, FileNotFoundError)):
            _legacy_runtime_environment(
                profile, LegacyRuntimeContext(profile.id, {**sparse, **changes}), {}
            )
