"""Private synthetic authorities; no native clients, stores or providers."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.canonical import CanonicalPolicy
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage, PolicyVersions
from session_migrate.model import AgentFormat, EventKind, TargetFormat
from session_migrate.native_audit import AuditPolicy, audit_snapshot, canonicalize_snapshot
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.projection_policies import (
    ProjectionError,
    ProjectionPolicy,
    expected_from_source,
)
from session_migrate.projections import (
    PreparedProjection,
    ProjectionCertificate,
    ProjectionContext,
    SourceVersionContext,
    prepare_projection,
    validate_prepared,
    verify_projection,
)

ID = "50000000-0000-4000-8000-000000000001"
STAMP = "2026-01-01T00:00:00Z"


def snapshot(records, fmt=AgentFormat.CLAUDE):
    data = ("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)).encode()
    return NativeSnapshot.from_bytes(
        data, context=SourceCaptureContext(fmt, Path("/synthetic/source"))
    )


def claude_records(blocks=None, roles=None, cwd="/synthetic/work"):
    blocks = blocks or [
        [{"type": "text", "text": "synthetic user"}],
        [{"type": "text", "text": "synthetic assistant"}],
    ]
    roles = roles or ["user", "assistant"]
    return [
        {
            "type": role,
            "uuid": f"synthetic-record-{i}",
            "parentUuid": f"synthetic-record-{i - 1}" if i else None,
            "sessionId": "synthetic-source",
            "cwd": cwd,
            "version": "2.1.209",
            "timestamp": STAMP,
            "message": {"role": role, "content": content},
        }
        for i, (role, content) in enumerate(zip(roles, blocks, strict=True))
    ]


def tool_records(*, error=False, input_value=None):
    return claude_records(
        [
            [{"type": "text", "text": "synthetic user"}],
            [
                {
                    "type": "tool_use",
                    "id": "synthetic-call",
                    "name": "synthetic_tool",
                    "input": {"count": 1} if input_value is None else input_value,
                }
            ],
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "synthetic-call",
                    "content": "synthetic result",
                    "is_error": error,
                }
            ],
            [{"type": "text", "text": "synthetic assistant"}],
        ],
        ["user", "assistant", "user", "assistant"],
    )


def codex_records(namespace="synthetic-namespace"):
    payloads = [
        (
            "session_meta",
            {
                "id": "synthetic-source",
                "session_id": "synthetic-source",
                "timestamp": STAMP,
                "cwd": "/synthetic/work",
                "cli_version": "0.144.4",
                "model_provider": "openai",
                "history_mode": "legacy",
            },
        ),
        (
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "synthetic user"}],
            },
        ),
        (
            "response_item",
            {
                "type": "function_call",
                "call_id": "synthetic-call",
                "name": "synthetic_tool",
                "arguments": '{"count":1}',
                "namespace": namespace,
            },
        ),
        (
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "synthetic-call",
                "output": "synthetic result",
            },
        ),
    ]
    return [{"type": kind, "timestamp": STAMP, "payload": payload} for kind, payload in payloads]


def observed(artifact):
    return NativeSnapshot.from_bytes(
        artifact.native_bytes,
        context=SourceCaptureContext(
            AgentFormat(artifact.target_format.value), Path("/synthetic/observed")
        ),
    )


def components(tmp_path, records=None, target=TargetFormat.CLAUDE, fmt=AgentFormat.CLAUDE):
    source = snapshot(records or claude_records(), fmt)
    release = {
        TargetFormat.CLAUDE: "2.1.209",
        TargetFormat.CODEX: "0.144.4",
        TargetFormat.OPENCODE: "2.0.23",
    }[target]
    source_release = {
        AgentFormat.CLAUDE: "2.1.209",
        AgentFormat.CODEX: "0.144.4",
        AgentFormat.OPENCODE: "2.0.23",
    }[fmt]
    options = ConversionOptions(target, session_id=ID, cwd=tmp_path, target_cli_version=release)
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        source_release,
        release,
    )
    context = ProjectionContext(target, release)
    return source, artifact, options, versions, context


def prepare(parts):
    source, artifact, options, versions, context = parts
    return prepare_projection(source, artifact, options=options, versions=versions, context=context)


@pytest.mark.parametrize("target", [TargetFormat.CLAUDE, TargetFormat.CODEX, TargetFormat.OPENCODE])
def test_existing_artifact_is_retained_and_observed_without_native_claim(tmp_path, target):
    parts = components(tmp_path, target=target)
    prepared = prepare(parts)
    assert prepared.artifact_bytes == parts[1].native_bytes
    assert prepared.artifact_sha256 == parts[1].target_sha256
    assert prepared.source_observation.audit.authority_sha256 == parts[0].authority_sha256
    assert prepared.artifact_observation.audit.authority_sha256 == prepared.artifact_sha256
    validate_prepared(prepared)
    result = verify_projection(prepared, observed(parts[1]), context=parts[-1])
    assert result.semantic_matches and result.structure_matches and result.relation_complete
    assert not result.identity_proven and not result.boundary_proven and not result.delta_eligible
    assert prepared.expected.witness.prefix_witness.closed_turn_count == 0


def test_preparation_never_reserializes_and_does_not_use_dropped(tmp_path, monkeypatch):
    parts = components(tmp_path)
    from session_migrate.formats import claude

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected serialization")

    monkeypatch.setattr(claude, "serialize", forbidden)
    parts[1].dropped["synthetic-untrusted-count"] = 999
    prepared = prepare(parts)
    assert prepared.artifact_bytes == parts[1].native_bytes


def test_deliberately_buggy_writer_is_detected_by_independent_expectation(tmp_path, monkeypatch):
    from session_migrate.formats import claude

    writer = claude.serialize

    def buggy(*args, **kwargs):
        data, dropped = writer(*args, **kwargs)
        return data.replace(b"synthetic user", b"synthetic corrupt"), dropped

    monkeypatch.setattr(claude, "serialize", buggy)
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare(components(tmp_path))


def test_mutated_source_payload_is_bound_even_when_event_equality_is_true(tmp_path):
    parts = components(tmp_path, tool_records())
    prepared = prepare(parts)
    call = next(e for e in parts[1].source.events if e.kind is EventKind.TOOL_CALL)
    prior = replace(call, payload={"input": {"count": 1}})
    call.payload["input"]["count"] = 2
    assert prior == call
    with pytest.raises(ProjectionError, match="binding_changed"):
        validate_prepared(prepared)
    with pytest.raises(ProjectionError, match="source_mismatch"):
        prepare(parts)


@pytest.mark.parametrize("target", [TargetFormat.CLAUDE, TargetFormat.CODEX, TargetFormat.OPENCODE])
def test_complete_tools_have_explicit_occurrence_memberships(tmp_path, target):
    parts = components(tmp_path, tool_records(), target)
    prepared = prepare(parts)
    assert prepared.expected.witness.prefix_witness.tool_links.count == 1
    assert prepared.expected.relation.count == 4
    assert [m.source_index for m in prepared.expected.members] == [0, 1, 2, 3]
    result = verify_projection(prepared, observed(parts[1]), context=parts[-1])
    assert result.semantic_matches


def test_known_error_loss_does_not_erase_complete_raw_source_audit(tmp_path):
    parts = components(tmp_path, tool_records(error=True), TargetFormat.CODEX)
    prepared = prepare(parts)
    assert prepared.source_coverage is Coverage.VERIFIED
    assert prepared.expectation_coverage is Coverage.PARTIAL
    assert dict(prepared.expected.loss_counts)["tool_error_omitted"] == 1
    assert prepared.certificate is None and not prepared.expected.preserves_content
    result = verify_projection(prepared, observed(parts[1]), context=parts[-1])
    assert result.semantic_matches and result.reason == "losses_declared"
    assert not result.preserves_content


def test_nonobject_input_wrapper_is_an_explicit_known_transform(tmp_path):
    parts = components(tmp_path, tool_records(input_value=[True, 1]), TargetFormat.CLAUDE)
    prepared = prepare(parts)
    assert dict(prepared.expected.loss_counts)["non_object_input_wrapped"] == 1
    assert prepared.certificate is None
    assert prepare(parts).expected.relation == prepared.expected.relation


def test_repeated_user_blocks_group_by_spans_not_text_search(tmp_path):
    records = claude_records(
        [
            [{"type": "text", "text": "repeat"}, {"type": "text", "text": "repeat"}],
            [{"type": "text", "text": "done"}],
        ]
    )
    prepared = prepare(components(tmp_path, records, TargetFormat.OPENCODE))
    first, second = prepared.expected.members[:2]
    assert first.target_index == second.target_index == 0
    assert (first.target_text_start, first.target_text_length) == (0, 6)
    assert (second.target_text_start, second.target_text_length) == (7, 6)
    assert prepared.expected.relation.count == 3
    assert prepared.expected.events[0].text == "repeat\nrepeat"
    assert dict(prepared.expected.loss_counts)["user_text_grouped"] == 2
    assert prepared.certificate is None


@pytest.mark.parametrize("kind", ["duplicate", "orphan", "open"])
def test_tool_ambiguity_cannot_acquire_a_certificate(tmp_path, kind):
    records = tool_records()
    if kind == "duplicate":
        records[1]["message"]["content"].append(records[1]["message"]["content"][0].copy())
    elif kind == "orphan":
        records[2]["message"]["content"][0]["tool_use_id"] = "synthetic-orphan"
    else:
        records[2]["message"]["content"] = []
    with pytest.raises(SessionMigrateError):
        prepare(components(tmp_path, records))


def test_unknown_source_field_prevents_certificate_but_keeps_observed_comparison(tmp_path):
    records = claude_records()
    records[0]["future_field"] = {"synthetic": "unmapped"}
    prepared = prepare(components(tmp_path, records))
    assert prepared.source_coverage is Coverage.UNKNOWN
    assert prepared.expectation_coverage is Coverage.UNKNOWN and prepared.certificate is None


@pytest.mark.parametrize(
    "field", ["artifact", "options", "context", "versions", "body", "expected"]
)
def test_mutation_of_bound_inputs_is_rejected(tmp_path, field):
    parts = components(tmp_path)
    prepared = prepare(parts)
    if field == "artifact":
        object.__setattr__(parts[1], "native_bytes", parts[1].native_bytes + b" ")
    elif field == "options":
        object.__setattr__(parts[2], "model", "synthetic changed model")
    elif field == "context":
        object.__setattr__(parts[4], "root_fingerprint", "a" * 64)
    elif field == "versions":
        object.__setattr__(parts[3], "source_adapter", "synthetic-other-adapter")
    elif field == "body":
        object.__setattr__(prepared, "_body", prepared.artifact_bytes + b" ")
    else:
        prepared.expected._events[0].payload["changed"] = True
    with pytest.raises(SessionMigrateError):
        validate_prepared(prepared)


def test_fake_and_foreign_certificates_are_rejected(tmp_path):
    with pytest.raises(ProjectionError):
        ProjectionCertificate()
    with pytest.raises(ProjectionError):
        PreparedProjection()
    with pytest.raises(ProjectionError):
        validate_prepared(object.__new__(PreparedProjection))
    first = prepare(components(tmp_path))
    with pytest.raises(ProjectionError, match="invalid_certificate"):
        validate_prepared(copy.copy(first))
    original_certificate = first.certificate
    object.__setattr__(first, "certificate", copy.copy(original_certificate))
    with pytest.raises(ProjectionError, match="invalid_certificate"):
        validate_prepared(first)
    object.__setattr__(first, "certificate", original_certificate)
    changed = claude_records(
        [[{"type": "text", "text": "different"}], [{"type": "text", "text": "done"}]]
    )
    second = prepare(components(tmp_path, changed))
    assert second.certificate is not None
    object.__setattr__(first, "certificate", second.certificate)
    with pytest.raises(ProjectionError, match="invalid_certificate"):
        validate_prepared(first)


@pytest.mark.parametrize("namespace", ["", "synthetic-namespace"])
def test_codex_namespace_presence_is_an_explicit_known_loss(tmp_path, namespace):
    parts = components(tmp_path, codex_records(namespace), TargetFormat.CLAUDE, AgentFormat.CODEX)
    prepared = prepare(parts)
    assert prepared.source_coverage is Coverage.VERIFIED
    assert dict(prepared.expected.loss_counts)["tool_namespace_omitted"] == 1
    assert prepared.expectation_coverage is Coverage.PARTIAL and prepared.certificate is None


@pytest.mark.parametrize("namespace", [True, 1, 1.0, {"nested": [True]}])
def test_namespace_unknown_types_cannot_certify_lost_raw_semantics(tmp_path, namespace):
    parts = components(tmp_path, codex_records(namespace), TargetFormat.CLAUDE, AgentFormat.CODEX)
    prepared = prepare(parts)
    assert prepared.source_coverage is Coverage.UNKNOWN
    assert prepared.certificate is None and not prepared.expected.preserves_content


@pytest.mark.parametrize("target", [TargetFormat.CLAUDE, TargetFormat.CODEX, TargetFormat.OPENCODE])
def test_opencode_two_source_tools_are_mapped_independently(tmp_path, target):
    seed = components(tmp_path, tool_records(), TargetFormat.OPENCODE)
    source = NativeSnapshot.from_bytes(
        seed[1].native_bytes,
        context=SourceCaptureContext(AgentFormat.OPENCODE, Path("/synthetic/opencode-source")),
    )
    release = {
        TargetFormat.CLAUDE: "2.1.209",
        TargetFormat.CODEX: "0.144.4",
        TargetFormat.OPENCODE: "2.0.23",
    }[target]
    options = ConversionOptions(target, session_id=ID, cwd=tmp_path, target_cli_version=release)
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        "2.0.23",
        release,
    )
    context = ProjectionContext(target, release)
    prepared = prepare_projection(
        source, artifact, options=options, versions=versions, context=context
    )
    assert prepared.expected.witness.prefix_witness.tool_links.count == 1
    assert verify_projection(prepared, observed(artifact), context=context).semantic_matches


def test_wrong_options_context_policy_and_observed_payload_fail_closed(tmp_path):
    parts = components(tmp_path)
    with pytest.raises(ProjectionError, match="options_mismatch"):
        prepare_projection(
            parts[0],
            parts[1],
            options=replace(parts[2], session_id="50000000-0000-4000-8000-000000000002"),
            versions=parts[3],
            context=parts[4],
        )
    prepared = prepare(parts)
    with pytest.raises(ProjectionError, match="context_mismatch"):
        verify_projection(
            prepared, observed(parts[1]), context=replace(parts[4], profile_fingerprint="a" * 64)
        )
    with pytest.raises(ProjectionError, match="unsupported_policy"):
        ProjectionPolicy("future-policy")
    corrupt = replace(
        parts[1],
        native_bytes=parts[1].native_bytes.replace(b"synthetic assistant", b"synthetic changed"),
    )
    result = verify_projection(prepared, observed(corrupt), context=parts[4])
    assert not result.semantic_matches and not result.preserves_content


def test_opencode_export_wrapper_client_override_requires_explicit_bound_bridge(tmp_path):
    seed = components(tmp_path, tool_records(), TargetFormat.OPENCODE)
    virtual_path = Path(f"opencode:{seed[1].session_id}")
    source = NativeSnapshot.from_bytes(
        seed[1].native_bytes, context=SourceCaptureContext(AgentFormat.OPENCODE, virtual_path)
    )
    original = source.parse()
    assert original.cli_version is None
    wrapper_source = replace(original, source_path=virtual_path, cli_version="2.0.23")
    options = ConversionOptions(
        TargetFormat.CLAUDE, session_id=ID, cwd=tmp_path, target_cli_version="2.1.209"
    )
    artifact = convert_session(wrapper_source, options)
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        "2.0.23",
        "2.1.209",
    )
    context = ProjectionContext(TargetFormat.CLAUDE, "2.1.209")
    with pytest.raises(ProjectionError, match="source_mismatch"):
        prepare_projection(source, artifact, options=options, versions=versions, context=context)
    bridge = SourceVersionContext(
        AgentFormat.OPENCODE, "2.0.23", virtual_path, source.authority_sha256
    )
    prepared = prepare_projection(
        source, artifact, options=options, versions=versions, context=context, source_context=bridge
    )
    validate_prepared(prepared)
    assert prepared.source_context is bridge
    assert verify_projection(prepared, observed(artifact), context=context).semantic_matches
    wrong_version = replace(bridge, source_client="2.0.22")
    with pytest.raises(ProjectionError, match="context_mismatch"):
        prepare_projection(
            source,
            artifact,
            options=options,
            versions=versions,
            context=context,
            source_context=wrong_version,
        )
    wrong_model = replace(artifact, source=replace(wrapper_source, model="synthetic changed"))
    with pytest.raises(ProjectionError, match="source_mismatch"):
        prepare_projection(
            source,
            wrong_model,
            options=options,
            versions=versions,
            context=context,
            source_context=bridge,
        )
    object.__setattr__(bridge, "authority_sha256", "a" * 64)
    with pytest.raises(ProjectionError, match="binding_changed"):
        validate_prepared(prepared)


def test_inline_media_and_url_reference_have_distinct_authority(tmp_path):
    records = claude_records(
        [
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "eA=="},
                }
            ],
            [{"type": "text", "text": "done"}],
        ]
    )
    prepared = prepare(components(tmp_path, records, TargetFormat.CODEX))
    assert not prepared.expected.witness.blocking
    records[0]["message"]["content"][0]["source"] = {
        "type": "url",
        "url": "https://synthetic.invalid/media",
    }
    external = prepare(components(tmp_path, records, TargetFormat.CODEX))
    assert external.certificate is None
    assert external.expected.witness.blocking
    assert (
        external.expected.witness.prefix_witness.units
        != prepared.expected.witness.prefix_witness.units
    )


@pytest.mark.parametrize("target", [TargetFormat.CLAUDE, TargetFormat.CODEX, TargetFormat.OPENCODE])
def test_ordered_tool_result_text_and_inline_image_are_modeled(tmp_path, target):
    records = tool_records()
    records[2]["message"]["content"][0]["content"] = [
        {"type": "text", "text": "synthetic result"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "eA=="}},
    ]
    parts = components(tmp_path, records, target)
    prepared = prepare(parts)
    assert verify_projection(prepared, observed(parts[1]), context=parts[-1]).semantic_matches


def test_opencode_user_image_text_regrouping_is_explicit_known_order_loss(tmp_path):
    records = claude_records(
        [
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "eA=="},
                },
                {"type": "text", "text": "synthetic after image"},
            ],
            [{"type": "text", "text": "done"}],
        ]
    )
    prepared = prepare(components(tmp_path, records, TargetFormat.OPENCODE))
    assert prepared.expected.events[0].kind is EventKind.MESSAGE
    assert prepared.expected.events[1].kind is EventKind.CONTEXT
    assert dict(prepared.expected.loss_counts)["user_content_order_grouped"] == 2
    assert prepared.certificate is None


def test_raw_known_loss_is_accounted_independently_of_fidelity(tmp_path):
    records = claude_records(
        [
            [{"type": "text", "text": "synthetic user"}],
            [
                {"type": "thinking", "thinking": "synthetic private state"},
                {"type": "text", "text": "synthetic assistant"},
            ],
        ]
    )
    prepared = prepare(components(tmp_path, records))
    assert prepared.source_coverage is Coverage.VERIFIED
    assert dict(prepared.expected.loss_counts)["source_raw_known_loss"] > 0
    assert prepared.expectation_coverage is Coverage.PARTIAL and prepared.certificate is None


def test_omitted_title_is_metadata_loss_without_claiming_conversation_content_loss(tmp_path):
    records = claude_records()
    records.append(
        {"type": "custom-title", "customTitle": "synthetic name", "sessionId": "synthetic-source"}
    )
    parts = components(tmp_path, records, TargetFormat.CODEX)
    prepared = prepare(parts)
    assert dict(prepared.expected.metadata_loss_counts) == {"session_title_omitted": 1}
    assert not prepared.expected.loss_counts and prepared.expected.preserves_content
    assert prepared.certificate is not None
    result = verify_projection(prepared, observed(parts[1]), context=parts[-1])
    assert result.preserves_content and result.reason == "projection_matches"
    assert dict(result.metadata_loss_counts) == {"session_title_omitted": 1}


def test_paginated_codex_items_never_prove_native_turn_closure(tmp_path):
    path = Path(__file__).parent / "fixtures/codex-0.153.4/paginated.jsonl"
    source = NativeSnapshot.from_bytes(
        path.read_bytes(),
        context=SourceCaptureContext(AgentFormat.CODEX, Path("/synthetic/paginated")),
    )
    options = ConversionOptions(
        TargetFormat.CODEX, session_id=ID, cwd=tmp_path, target_cli_version="0.144.4"
    )
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        "0.153.4",
        "0.144.4",
    )
    prepared = prepare_projection(
        source,
        artifact,
        options=options,
        versions=versions,
        context=ProjectionContext(TargetFormat.CODEX, "0.144.4"),
    )
    assert prepared.expected.witness.prefix_witness.closed_turn_count == 0
    assert prepared.expected.witness.prefix_witness.boundary.kind.value == "unproven"


def test_relation_roots_cover_ten_thousand_events_and_bounded_diagnostics():
    records = claude_records(
        [[{"type": "text", "text": str(i)}] for i in range(10_000)],
        ["user" if i % 2 == 0 else "assistant" for i in range(10_000)],
    )
    source = snapshot(records)
    observation = canonicalize_snapshot(
        source, audit_snapshot(source, AuditPolicy()), CanonicalPolicy()
    )
    expected = expected_from_source(observation, target_format=TargetFormat.CODEX)
    assert expected.relation.count == 10_000
    assert expected.witness.prefix_witness.units.count == 10_000
    assert len(expected.members) == len(expected.witness.units) == 1024
    assert expected.relation_complete
    changed = claude_records(
        [[{"type": "text", "text": "changed"}], [{"type": "text", "text": "done"}]]
    )
    other = snapshot(changed)
    other_obs = canonicalize_snapshot(other, audit_snapshot(other), CanonicalPolicy())
    assert (
        expected.relation
        != expected_from_source(other_obs, target_format=TargetFormat.CODEX).relation
    )
