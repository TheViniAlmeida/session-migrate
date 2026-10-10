"""Synthetic original files only; no native clients, publication or ledger."""

import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.lineage import (
    Coverage,
    OperationRecord,
    PolicyVersions,
    ProjectionExpectation,
    RootWitness,
    SourceMetadata,
)
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_namespaces import (
    _bind_execution_context,
    qualify_namespace,
    reserve_namespace,
)
from session_migrate.native_snapshots import (
    NativeSnapshot,
    SourceCaptureContext,
    capture_source,
)
from session_migrate.operations import (
    OperationKind,
    OperationRequest,
    _canonical_snapshot_sha,
    _digest,
    _public_options,
    _source_witness,
)
from session_migrate.profiles import Profile
from session_migrate.projection_policies import ProjectionPolicy
from session_migrate.projections import ProjectionContext, _observe, prepare_projection
from session_migrate.recovered_projections import (
    ReconstructedProjection,
    ReconstructedProjectionCertificate,
    RecoveredProjectionContext,
    RecoveryProjectionError,
    _prepare_recovery_context,
    prepare_recovered_projection,
    validate_recovered_projection,
    verify_recovered_projection,
)

ID = "50000000-0000-4000-8000-000000000001"
SOURCE_ID = "40000000-0000-4000-8000-000000000001"
STAMP = "2026-01-01T00:00:00Z"


def body(records):
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def source_records(fmt):
    if fmt is AgentFormat.CLAUDE:
        return [
            {
                "type": role,
                "uuid": f"fixture-{i}",
                "parentUuid": "fixture-0" if i else None,
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
    return [
        {
            "type": "session_meta",
            "timestamp": STAMP,
            "payload": {
                "id": SOURCE_ID,
                "session_id": SOURCE_ID,
                "cwd": "/synthetic/work",
                "cli_version": "0.144.4",
                "timestamp": STAMP,
                "model_provider": "openai",
                "history_mode": "legacy",
            },
        },
        *[
            {
                "type": "response_item",
                "timestamp": STAMP,
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": kind, "text": text}],
                },
            }
            for role, kind, text in (
                ("user", "input_text", "synthetic request"),
                ("assistant", "output_text", "synthetic answer"),
            )
        ],
    ]


def fixture(
    tmp_path,
    source_format=AgentFormat.CLAUDE,
    target=TargetFormat.CODEX,
    change_source=None,
    change_input=None,
):
    environment = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    target_root = tmp_path / "target"
    target_root.mkdir()
    path = tmp_path / "original-source.jsonl"
    records = source_records(source_format)
    if change_source is not None:
        change_source(records)
    path.write_bytes(body(records))
    source = capture_source(SourceCaptureContext(source_format, path))
    release = "2.1.209" if target is TargetFormat.CLAUDE else "0.144.4"
    options = ConversionOptions(target, session_id=ID, cwd=tmp_path, target_cli_version=release)
    artifact = convert_session(source.parse(), options)
    input_body = artifact.native_bytes
    if change_input is not None:
        records = [json.loads(line) for line in input_body.splitlines()]
        change_input(records)
        input_body = body(records)
        artifact = replace(artifact, native_bytes=input_body)
    input_path = tmp_path / "original-input.jsonl"
    input_path.write_bytes(input_body)
    input_capture = capture_source(SourceCaptureContext(AgentFormat(target.value), input_path))
    policy = ProjectionPolicy()
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        source.parse().cli_version,
        release,
    )
    projection = prepare_projection(
        source,
        artifact,
        options=options,
        versions=versions,
        context=ProjectionContext(target, release),
    )
    request = OperationRequest(
        OperationKind.IMPORT,
        path=path,
        source_format=source_format,
        target_format=target,
        home=target_root,
        cwd=tmp_path,
        session_id=ID,
        target_cli_version=release,
        dry_run=True,
    )
    source_obs = projection.source_observation
    input_obs = _observe(input_capture, release, policy)
    record = OperationRecord(
        "synthetic-recovered-operation",
        SourceMetadata(
            None,
            source.authority_sha256,
            _source_witness(source.parse(), path.parent),
            _canonical_snapshot_sha(source_obs, versions),
            source_obs.source_prefix,
        ),
        reserve_namespace(AgentFormat(target.value), target_root, ID),
        _digest(
            {
                "format": target.value,
                "environment": environment,
                "root": str(target_root),
            }
        ),
        _digest(_public_options(request, environment, environment, {})),
        input_capture.authority_sha256,
        versions,
        ProjectionExpectation(
            _canonical_snapshot_sha(input_obs, versions)
            if projection.expectation_coverage is not Coverage.UNKNOWN
            else None,
            (),
            projection.expectation_coverage,
            input_obs.source_prefix
            if projection.expectation_coverage is not Coverage.UNKNOWN
            else None,
            projection.expected.relation.chain_sha256
            if projection.expectation_coverage is not Coverage.UNKNOWN
            else None,
        ),
        (),
    )
    return source, input_capture, record, request, environment


def context_for(parts, **overrides):
    source, input_capture, record, request, environment = parts
    kwargs = {"source_environment": environment, "target_environment": environment}
    kwargs.update(overrides)
    return _prepare_recovery_context(source, input_capture, record, request, **kwargs)


def prepare(parts):
    return prepare_recovered_projection(*parts[:3], context_for(parts))


@pytest.mark.parametrize("source_format", [AgentFormat.CLAUDE, AgentFormat.CODEX])
@pytest.mark.parametrize("target", [TargetFormat.CLAUDE, TargetFormat.CODEX])
def test_existing_original_ids_bytes_and_new_provenance(tmp_path, source_format, target):
    parts = fixture(tmp_path, source_format, target)
    recovered = prepare(parts)
    assert recovered.artifact_bytes is parts[1].raw_bytes
    assert recovered.artifact_sha256 == parts[2].target_input_sha256
    assert recovered.artifact_observation.session.session_id == ID
    assert recovered.source_observation.session.session_id == SOURCE_ID
    assert recovered.provenance == recovered.certificate.provenance == "reconstructed_now"
    assert not recovered.certificate.identity_proven
    assert not recovered.certificate.boundary_proven
    assert not recovered.certificate.native_cas_proven
    assert not recovered.certificate.delta_eligible
    observed = NativeSnapshot.from_bytes(
        parts[1].raw_bytes,
        context=SourceCaptureContext(AgentFormat(target.value), Path("/synthetic/observed")),
    )
    result = verify_recovered_projection(
        recovered, observed, context=recovered._context.projection_context
    )
    assert result.preserves_content
    assert result.reason == "projection_matches"
    assert not result.identity_proven and not result.boundary_proven and not result.delta_eligible
    assert parts[2].source.identity is None


def test_producer_cannot_serialize_install_export_resume_or_create_artifact(tmp_path, monkeypatch):
    parts = fixture(tmp_path)
    from session_migrate import conversion, operations
    from session_migrate.formats import claude, codex

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected writer, installer or native client")

    for module, names in (
        (
            conversion,
            (
                "convert_session",
                "write_artifact",
                "load_opencode_session",
                "install_opencode_artifact",
                "install_kilo_artifact",
            ),
        ),
        (
            operations,
            ("convert_session", "_install", "_build_operation", "prepare_operation"),
        ),
        (claude, ("serialize",)),
        (codex, ("serialize",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    recovered = prepare(parts)
    validate_recovered_projection(recovered)
    assert recovered.artifact_bytes == parts[1].raw_bytes


@pytest.mark.parametrize(
    "field", ["target_input_sha256", "options_sha256", "target_profile_fingerprint"]
)
def test_record_hash_context_mismatch_is_blocked(tmp_path, field):
    parts = list(fixture(tmp_path))
    parts[2] = replace(parts[2], **{field: "0" * 64})
    with pytest.raises(RecoveryProjectionError):
        prepare(parts)


@pytest.mark.parametrize("field", ["artifact_sha256", "witness_sha256", "snapshot_sha256"])
def test_original_source_binding_mismatch_is_blocked(tmp_path, field):
    parts = list(fixture(tmp_path))
    parts[2] = replace(parts[2], source=replace(parts[2].source, **{field: "0" * 64}))
    with pytest.raises(RecoveryProjectionError, match="source_mismatch"):
        prepare(parts)


@pytest.mark.parametrize("field", ["snapshot_sha256", "relation_sha256"])
def test_durable_expected_roots_and_relation_must_match(tmp_path, field):
    parts = list(fixture(tmp_path))
    parts[2] = replace(
        parts[2],
        expected_projection=replace(parts[2].expected_projection, **{field: "0" * 64}),
    )
    with pytest.raises(RecoveryProjectionError, match="input_mismatch"):
        prepare(parts)


def test_missing_durable_proof_is_explicit(tmp_path):
    parts = list(fixture(tmp_path))
    parts[2] = replace(parts[2], source=replace(parts[2].source, prefix_witness=None))
    with pytest.raises(RecoveryProjectionError, match="proof_unavailable"):
        prepare(parts)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "changed"},
        {"session_id": "different"},
        {"cwd": Path("/different")},
        {"target_cli_version": "different"},
        {"target_title": "local synthetic title", "title_policy": "local_alias"},
    ],
)
def test_original_normalized_request_cannot_be_replaced(tmp_path, change):
    parts = list(fixture(tmp_path))
    parts[3] = replace(parts[3], **change)
    with pytest.raises(RecoveryProjectionError):
        prepare(parts)


def test_environment_root_and_profile_context_are_not_caller_hashes(tmp_path):
    parts = fixture(tmp_path)
    with pytest.raises(RecoveryProjectionError, match="options_mismatch"):
        context_for(parts, target_environment={**parts[-1], "HOME": "/different"})
    with pytest.raises(RecoveryProjectionError, match="context_unavailable"):
        context_for(parts, source_profile="0" * 64, target_profile="0" * 64)
    with pytest.raises(RecoveryProjectionError, match="context_unavailable"):
        context_for(parts, source_environment={})


@pytest.mark.parametrize("side", [0, 1])
def test_rewrite_with_restored_mtime_cannot_pass_file_capture(tmp_path, side):
    parts = fixture(tmp_path)
    captured = parts[side]
    before = captured.source_path.stat()
    captured.source_path.write_bytes(captured.raw_bytes.replace(b"synthetic", b"tampered!"))
    os.utime(captured.source_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(RecoveryProjectionError, match="binding_changed"):
        context_for(parts)


def test_exported_observed_body_cannot_replace_existing_original_input(tmp_path):
    parts = list(fixture(tmp_path))
    parts[1] = NativeSnapshot.from_bytes(
        parts[1].raw_bytes,
        context=SourceCaptureContext(AgentFormat.CODEX, parts[1].source_path),
    )
    with pytest.raises(RecoveryProjectionError, match="proof_unavailable"):
        context_for(parts)


def test_fabricated_capture_stat_cannot_authorize_original_input(tmp_path):
    parts = list(fixture(tmp_path))
    snapshot = parts[1]
    parts[1] = replace(
        snapshot, identity=replace(snapshot.identity, inode=snapshot.identity.inode + 1)
    )
    with pytest.raises(RecoveryProjectionError, match="binding_changed"):
        context_for(parts)


@pytest.mark.parametrize(
    "cls",
    [
        ReconstructedProjection,
        ReconstructedProjectionCertificate,
        RecoveredProjectionContext,
    ],
)
def test_private_constructors_are_not_dtos(cls):
    with pytest.raises(RecoveryProjectionError):
        cls()


def test_spoof_clone_and_certificate_removal_cannot_validate(tmp_path):
    recovered = prepare(fixture(tmp_path))
    with pytest.raises(RecoveryProjectionError, match="invalid_certificate"):
        validate_recovered_projection(copy.copy(recovered))
    object.__setattr__(recovered, "certificate", None)
    with pytest.raises(RecoveryProjectionError, match="binding_changed"):
        validate_recovered_projection(recovered)


def test_certificate_clone_and_tampering_cannot_validate(tmp_path):
    recovered = prepare(fixture(tmp_path))
    object.__setattr__(recovered.certificate, "relation_sha256", "0" * 64)
    with pytest.raises(RecoveryProjectionError, match="invalid_certificate"):
        validate_recovered_projection(recovered)


@pytest.mark.parametrize("attribute", ["_body", "binding_sha256", "artifact_sha256"])
def test_private_guard_binds_body_and_public_metadata(tmp_path, attribute):
    recovered = prepare(fixture(tmp_path))
    object.__setattr__(recovered, attribute, b"changed" if attribute == "_body" else "0" * 64)
    with pytest.raises(RecoveryProjectionError, match="binding_changed"):
        validate_recovered_projection(recovered)


def test_request_mutation_after_issuance_is_rejected(tmp_path):
    parts = fixture(tmp_path)
    context = context_for(parts)
    object.__setattr__(parts[3], "model", "changed")
    with pytest.raises(RecoveryProjectionError, match="binding_changed"):
        prepare_recovered_projection(*parts[:3], context)


def test_source_expected_payload_guard_ignores_dataclass_equality(tmp_path):
    def change(records):
        records[1]["message"]["content"] = [
            {"type": "tool_use", "id": "call", "name": "tool", "input": {"count": 1}}
        ]
        records.append(
            {
                **records[0],
                "uuid": "result",
                "parentUuid": "fixture-1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call",
                            "content": "result",
                            "is_error": False,
                        }
                    ],
                },
            }
        )

    recovered = prepare(fixture(tmp_path, change_source=change))
    event = next(e for e in recovered.expected._events if e.payload)
    prior = replace(event, payload=copy.deepcopy(event.payload))
    event.payload["tampered"] = True
    assert event == prior
    with pytest.raises(RecoveryProjectionError, match="binding_changed"):
        validate_recovered_projection(recovered)


def test_unknown_native_input_fields_block_content_preservation(tmp_path):
    parts = fixture(
        tmp_path,
        change_input=lambda records: records[0]["payload"].update({"future_field": "synthetic"}),
    )
    assert parts[2].expected_projection.coverage is Coverage.UNKNOWN
    with pytest.raises(RecoveryProjectionError, match="proof_unavailable"):
        prepare(parts)


def test_observation_loss_detail_cannot_preserve_content(tmp_path):
    parts = fixture(tmp_path)
    recovered = prepare(parts)
    records = [json.loads(line) for line in parts[1].raw_bytes.splitlines()]
    next(record for record in records if record["type"] == "response_item")["payload"]["phase"] = (
        "analysis"
    )
    observed = NativeSnapshot.from_bytes(
        body(records),
        context=SourceCaptureContext(AgentFormat.CODEX, Path("/synthetic/observed")),
    )
    result = verify_recovered_projection(
        recovered, observed, context=recovered._context.projection_context
    )
    assert not result.preserves_content


def test_native_id_alias_conflict_is_not_hidden_by_parser(tmp_path):
    parts = fixture(tmp_path)
    records = [json.loads(line) for line in parts[1].raw_bytes.splitlines()]
    records[0]["payload"]["session_id"] = "different"
    observed = NativeSnapshot.from_bytes(
        body(records),
        context=SourceCaptureContext(AgentFormat.CODEX, Path("/synthetic/observed")),
    )
    recovered = prepare(parts)
    with pytest.raises(RecoveryProjectionError, match="metadata_mismatch"):
        verify_recovered_projection(
            recovered, observed, context=recovered._context.projection_context
        )


def test_unknown_opencode_bridge_is_not_inferred_from_bytes(tmp_path):
    parts = list(fixture(tmp_path))
    parts[0] = replace(parts[0], source_format=AgentFormat.OPENCODE)
    with pytest.raises(RecoveryProjectionError, match="proof_unavailable"):
        context_for(parts)


def transfer_fixture(tmp_path, with_profiles=False):
    parts = list(fixture(tmp_path))
    source, input_capture, record, request, environment = parts
    source_root = tmp_path / "source-native"
    source_path = source_root / "projects" / "-synthetic-work" / f"{SOURCE_ID}.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source.raw_bytes)
    source = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, source_path))
    request = replace(
        request,
        command=OperationKind.TRANSFER,
        path=None,
        source_id=SOURCE_ID,
        source_home=source_root,
    )
    namespace = reserve_namespace(AgentFormat.CLAUDE, source_root, SOURCE_ID)
    source_profile = target_profile = None
    profiles = {}
    source_fingerprint = _digest(
        {"format": "claude", "environment": environment, "root": str(source_root)}
    )
    target_fingerprint = record.target_profile_fingerprint
    if with_profiles:
        source_profile = Profile(
            "fixture-source",
            "Synthetic source",
            AgentFormat.CLAUDE,
            source_root,
            environ={"HOME": str(tmp_path)},
        )
        target_profile = Profile(
            "fixture-target",
            "Synthetic target",
            AgentFormat.CODEX,
            Path(record.target_namespace.canonical_root),
            environ={"HOME": str(tmp_path)},
        )
        source_fingerprint, target_fingerprint = (
            source_profile.fingerprint,
            target_profile.fingerprint,
        )
        for side, profile in (("source", source_profile), ("target", target_profile)):
            profiles.update(
                {
                    f"{side}_profile_id": profile.id,
                    f"{side}_profile_name": profile.name,
                    f"{side}_profile_fingerprint": profile.fingerprint,
                }
            )
    session = source.parse()
    execution = _bind_execution_context(
        namespace,
        session,
        native_path=source_path,
        environ=environment,
        snapshot=source,
    )
    identity = qualify_namespace(
        session, namespace, source_fingerprint, execution_context=execution
    )
    assert identity is not None
    record = replace(
        record,
        source=replace(
            record.source,
            identity=identity,
            witness_sha256=_source_witness(session, source_root),
        ),
        options_sha256=_digest(_public_options(request, environment, environment, profiles)),
        target_profile_fingerprint=target_fingerprint,
    )
    return (
        (source, input_capture, record, request, environment),
        source_profile,
        target_profile,
    )


@pytest.mark.parametrize("with_profiles", [False, True])
def test_source_identity_requires_actual_layout_root_and_profiles(tmp_path, with_profiles):
    parts, source_profile, target_profile = transfer_fixture(tmp_path, with_profiles)
    context = context_for(parts, source_profile=source_profile, target_profile=target_profile)
    recovered = prepare_recovered_projection(*parts[:3], context)
    assert recovered.certificate is not None
    assert not recovered.certificate.identity_proven
    if with_profiles:
        assert context.projection_context.profile_fingerprint == target_profile.fingerprint


def test_qualified_source_dto_cannot_supply_fake_root_witness(tmp_path):
    parts, _, _ = transfer_fixture(tmp_path)
    parts = list(parts)
    identity = parts[2].source.identity
    identity = replace(
        identity,
        root_witness=RootWitness(identity.root_witness.device, identity.root_witness.inode + 1),
    )
    parts[2] = replace(parts[2], source=replace(parts[2].source, identity=identity))
    with pytest.raises(RecoveryProjectionError, match="context_mismatch"):
        context_for(parts)


def test_profile_fingerprint_does_not_replace_actual_environment(tmp_path):
    parts, source_profile, target_profile = transfer_fixture(tmp_path, True)
    mutated = replace(source_profile, environ={"HOME": "/different"})
    with pytest.raises(RecoveryProjectionError, match="options_mismatch"):
        context_for(parts, source_profile=mutated, target_profile=target_profile)


def test_context_certificate_and_source_capture_clones_cannot_be_swapped(tmp_path):
    parts = fixture(tmp_path)
    context = context_for(parts)
    with pytest.raises(RecoveryProjectionError, match="context_unavailable"):
        prepare_recovered_projection(*parts[:3], copy.copy(context))
    with pytest.raises(RecoveryProjectionError, match="context_mismatch"):
        prepare_recovered_projection(copy.copy(parts[0]), parts[1], parts[2], context)
    recovered = prepare_recovered_projection(*parts[:3], context)
    object.__setattr__(recovered, "certificate", copy.copy(recovered.certificate))
    with pytest.raises(RecoveryProjectionError):
        validate_recovered_projection(recovered)


def test_target_namespace_swaps_cannot_pass_public_options(tmp_path):
    parts = list(fixture(tmp_path))
    parts[2] = replace(
        parts[2],
        target_namespace=replace(parts[2].target_namespace, canonical_root="/different"),
    )
    with pytest.raises(RecoveryProjectionError, match="metadata_mismatch"):
        context_for(parts)


def test_unknown_policy_and_clients_are_explicit_blockers(tmp_path):
    parts = list(fixture(tmp_path))
    parts[2] = replace(
        parts[2],
        versions=replace(parts[2].versions, projection_policy="unsupported-v1"),
    )
    with pytest.raises(RecoveryProjectionError, match="unsupported_policy"):
        context_for(parts)


def test_public_environment_does_not_retain_secret_variables(tmp_path):
    parts = fixture(tmp_path)
    context = context_for(parts, source_environment={**parts[-1], "SYNTHETIC_SECRET": "marker"})
    assert "SYNTHETIC_SECRET" not in dict(context._source_environment)
    assert "marker" not in repr(context)


def test_alias_title_context_is_bound_but_has_no_finalization_authority(tmp_path):
    parts = list(fixture(tmp_path))
    parts[3] = replace(parts[3], target_title="Synthetic local alias", title_policy="local_alias")
    parts[2] = replace(
        parts[2],
        options_sha256=_digest(_public_options(parts[3], parts[-1], parts[-1], {})),
    )
    recovered = prepare(parts)
    assert recovered._context._options.target_title == "Synthetic local alias"
    assert not hasattr(recovered, "finalized") and not hasattr(recovered, "acknowledged")


def test_malformed_existing_raw_input_never_issues_context(tmp_path):
    parts = list(fixture(tmp_path))
    parts[1].source_path.write_bytes(b"{malformed}\n")
    parts[1] = capture_source(SourceCaptureContext(AgentFormat.CODEX, parts[1].source_path))
    parts[2] = replace(parts[2], target_input_sha256=parts[1].authority_sha256)
    from session_migrate.errors import SessionMigrateError

    with pytest.raises(SessionMigrateError):
        context_for(parts)


@pytest.mark.parametrize("links", ["hard", "symbolic"])
def test_existing_input_aliases_rejected_by_fd_capture(tmp_path, links):
    parts = fixture(tmp_path)
    alias = tmp_path / "alias.jsonl"
    if links == "hard":
        alias.hardlink_to(parts[1].source_path)
    else:
        alias.symlink_to(parts[1].source_path)
    from session_migrate.errors import SessionMigrateError

    with pytest.raises(SessionMigrateError):
        capture_source(SourceCaptureContext(AgentFormat.CODEX, alias))


def test_input_file_capture_limit_is_enforced_before_issuer(tmp_path):
    parts = fixture(tmp_path)
    from session_migrate.errors import SessionMigrateError

    with pytest.raises(SessionMigrateError):
        capture_source(SourceCaptureContext(AgentFormat.CODEX, parts[1].source_path), byte_limit=8)


@pytest.mark.parametrize("side", [0, 1])
def test_captured_raw_body_and_metadata_mutation_after_guard_is_rejected(tmp_path, side):
    parts = fixture(tmp_path)
    recovered = prepare(parts)
    object.__setattr__(
        parts[side],
        "_raw_bytes",
        parts[side].raw_bytes.replace(b"synthetic", b"tampered!"),
    )
    with pytest.raises(RecoveryProjectionError, match="binding_changed"):
        validate_recovered_projection(recovered)


def test_known_raw_loss_keeps_comparison_but_cannot_issue_certificate(tmp_path):
    parts = fixture(
        tmp_path,
        change_input=lambda records: next(
            record for record in records if record["type"] == "response_item"
        )["payload"].update({"phase": "analysis"}),
    )
    recovered = prepare(parts)
    assert recovered.expectation_coverage is Coverage.PARTIAL
    assert recovered.certificate is None
    result = verify_recovered_projection(
        recovered, parts[1], context=recovered._context.projection_context
    )
    assert result.semantic_matches and not result.preserves_content


def test_client_metadata_mismatch_cannot_use_caller_versions(tmp_path):
    parts = list(fixture(tmp_path))
    parts[2] = replace(parts[2], versions=replace(parts[2].versions, source_client="0.144.4"))
    with pytest.raises(RecoveryProjectionError, match="metadata_mismatch"):
        context_for(parts)


def test_verify_context_hashes_are_compared_with_issued_context(tmp_path):
    recovered = prepare(fixture(tmp_path))
    context = replace(recovered._context.projection_context, root_fingerprint="0" * 64)
    with pytest.raises(RecoveryProjectionError, match="context_mismatch"):
        verify_recovered_projection(recovered, recovered._context._input, context=context)
