"""Synthetic proof of the exact OpenCode 2.2.5 incomplete-call archive rule."""

import copy
import json
from dataclasses import replace

import pytest
from test_projections import ID, observed, snapshot, tool_records
from test_recovered_projections import context_for, fixture

from session_migrate.canonical import iter_canonical_units
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, EventKind, Role, TargetFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_source
from session_migrate.projection_policies import (
    INCOMPLETE_TOOL_MESSAGE,
    INCOMPLETE_TOOL_RULE,
    OPENCODE_ARCHIVE_POLICY_ID,
    ProjectionError,
    ProjectionPolicy,
    expected_from_source,
    policy_from_id,
)
from session_migrate.projections import (
    ProjectionContext,
    _expectation_sha,
    _observe,
    prepare_projection,
    validate_prepared,
    verify_projection,
)
from session_migrate.recovered_projections import RecoveryProjectionError

POLICY = ProjectionPolicy(OPENCODE_ARCHIVE_POLICY_ID)


def open_records(*, unknown=False):
    records = tool_records()
    records[2]["message"]["content"] = []
    if unknown:
        records[0]["future_field"] = {"synthetic": "unknown"}
    return records


def archive_parts(
    tmp_path,
    records=None,
    *,
    policy=POLICY,
    target=TargetFormat.OPENCODE,
    client="2.2.5",
    parent=None,
):
    from session_migrate.lineage import PolicyVersions

    source = snapshot(records if records is not None else open_records())
    options = ConversionOptions(
        target, session_id=ID, cwd=tmp_path, target_cli_version=client, target_parent_id=parent
    )
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        policy.policy_id,
        "native-audit-v1",
        "native-audit-v1",
        source.parse().cli_version,
        client,
    )
    context = ProjectionContext(target, client)
    return source, artifact, options, versions, context


def prepare(parts, *, policy=POLICY):
    source, artifact, options, versions, context = parts
    return prepare_projection(
        source, artifact, options=options, versions=versions, context=context, policy=policy
    )


def tools(artifact):
    return [
        content
        for message in json.loads(artifact.native_bytes)["messages"]
        if message["type"] == "assistant"
        for content in message["content"]
        if content["type"] == "tool"
    ]


def changed_artifact(artifact, change):
    bundle = json.loads(artifact.native_bytes)
    change(bundle)
    return replace(artifact, native_bytes=json.dumps(bundle).encode())


def bundle_tools(bundle):
    return [
        content
        for message in bundle["messages"]
        if message["type"] == "assistant"
        for content in message["content"]
        if content["type"] == "tool"
    ]


def test_default_policy_still_refuses_open_call(tmp_path):
    parts = archive_parts(tmp_path, policy=ProjectionPolicy())
    with pytest.raises(ProjectionError, match="source_tools_unproven"):
        prepare(parts, policy=ProjectionPolicy())


@pytest.mark.parametrize("unknown", [False, True])
def test_exact_archive_is_bound_loss_without_source_promotion(tmp_path, unknown):
    parts = archive_parts(tmp_path, open_records(unknown=unknown))
    original = bytes(parts[0].raw_bytes)
    prepared = prepare(parts)
    result = verify_projection(prepared, observed(parts[1]), context=parts[-1])
    assert result.semantic_matches and result.structure_matches
    assert not result.preserves_content and not result.delta_eligible
    assert not result.identity_proven and not result.boundary_proven
    assert prepared.certificate is None
    assert prepared.expectation_coverage is (Coverage.UNKNOWN if unknown else Coverage.PARTIAL)
    assert prepared.source_coverage is (Coverage.UNKNOWN if unknown else Coverage.VERIFIED)
    assert dict(prepared.expected.loss_counts)["incomplete_tool_archived"] == 1
    assert not any(e.kind is EventKind.TOOL_RESULT for e in prepared.source_observation.events)
    assert parts[0].raw_bytes == original
    native = tools(parts[1])[0]
    assert native["id"] == "synthetic-call" and native["name"] == "synthetic_tool"
    assert native["state"] == {
        "status": "error",
        "input": {"count": 1},
        "error": {"type": "ImportedIncompleteTool", "message": INCOMPLETE_TOOL_MESSAGE},
    }
    expected = prepared.expected
    generated = expected.generated_members[0]
    assert generated.rule_id == INCOMPLETE_TOOL_RULE
    assert generated.transform == "archive_incomplete_tool"
    assert expected.events[generated.call_target_index].kind is EventKind.TOOL_CALL
    result_event = expected.events[generated.target_index]
    assert result_event.kind is EventKind.TOOL_RESULT and result_event.role is Role.TOOL
    assert result_event.tool_call_id == native["id"] and result_event.tool_name == native["name"]
    assert result_event.text == INCOMPLETE_TOOL_MESSAGE
    assert result_event.payload == {
        "content_blocks": [{"type": "text", "text": INCOMPLETE_TOOL_MESSAGE}],
        "is_error": True,
    }
    original_units = tuple(
        iter_canonical_units(prepared.source_observation.events, POLICY.canonical_policy)
    )
    target_units = tuple(iter_canonical_units(expected.events, POLICY.canonical_policy))
    assert generated.source_sha256 == original_units[generated.source_index].sha256
    assert generated.target_sha256 == target_units[generated.target_index].sha256
    normal = {m.target_index for m in expected.members if m.target_index is not None}
    generated_targets = {m.target_index for m in expected.generated_members}
    assert normal.isdisjoint(generated_targets)
    assert normal | generated_targets == set(range(len(target_units)))
    assert expected.relation.count == len(original_units)
    validate_prepared(prepared)


def test_desktop309_archive_stays_unknown_with_unproven_relation(tmp_path):
    records = open_records()
    for record in records:
        record["version"] = "2.1.309"
    parts = archive_parts(tmp_path, records)
    prepared = prepare(parts)
    assert prepared.source_observation.session.cli_version == "2.1.309"
    assert prepared.source_coverage is prepared.expectation_coverage is Coverage.UNKNOWN
    assert not prepared.expected.relation_complete
    assert prepared.certificate is None
    result = verify_projection(prepared, observed(parts[1]), context=parts[-1])
    assert result.semantic_matches and not result.preserves_content and not result.delta_eligible
    assert result.reason == "audit_unproven"


def test_each_missing_call_has_its_own_generated_binding_and_loss(tmp_path):
    records = open_records()
    records[1]["message"]["content"].append(
        {"type": "tool_use", "id": "second-open", "name": "second_tool", "input": {"n": 2}}
    )
    parts = archive_parts(tmp_path, records)
    prepared = prepare(parts)
    assert dict(prepared.expected.loss_counts)["incomplete_tool_archived"] == 2
    assert len(prepared.expected.generated_members) == 2
    assert {t["id"] for t in tools(parts[1])} == {"synthetic-call", "second-open"}
    assert verify_projection(prepared, observed(parts[1]), context=parts[-1]).semantic_matches


@pytest.mark.parametrize("value", [[True, 1], "synthetic", None])
def test_archive_preserves_existing_non_object_input_wrapping(tmp_path, value):
    records = open_records()
    records[1]["message"]["content"][0]["input"] = value
    parts = archive_parts(tmp_path, records)
    prepared = prepare(parts)
    call = next(e for e in prepared.expected.events if e.kind is EventKind.TOOL_CALL)
    assert call.payload == {"input": {"input": value}}
    assert tools(parts[1])[0]["state"]["input"] == {"input": value}
    assert dict(prepared.expected.loss_counts)["non_object_input_wrapped"] == 1
    assert verify_projection(prepared, observed(parts[1]), context=parts[-1]).semantic_matches


def test_expected_archive_is_independent_of_any_writer(tmp_path, monkeypatch):
    parts = archive_parts(tmp_path)
    source_obs = _observe(parts[0], "2.1.209", POLICY)

    def forbidden(*args, **kwargs):
        pytest.fail("the expectation invoked a writer")

    monkeypatch.setattr("session_migrate.formats.opencode_v2.from_legacy", forbidden)
    expected = expected_from_source(source_obs, target_format=TargetFormat.OPENCODE, policy=POLICY)
    assert len(expected.generated_members) == 1


@pytest.mark.parametrize("tamper", ["type", "message", "success", "input", "name", "id", "missing"])
def test_raw_archive_tampering_refused_in_preparation_and_verification(tmp_path, tamper):
    parts = archive_parts(tmp_path)
    prepared = prepare(parts)

    def change(bundle):
        tool = bundle_tools(bundle)[0]
        if tamper == "type":
            tool["state"]["error"]["type"] = "ToolError"
        elif tamper == "message":
            tool["state"]["error"]["message"] = "changed"
        elif tamper == "success":
            tool["state"] = {
                "status": "completed",
                "input": {"count": 1},
                "content": [{"type": "text", "text": INCOMPLETE_TOOL_MESSAGE}],
            }
        elif tamper in {"name", "id"}:
            tool[tamper] = "changed"
        elif tamper == "input":
            tool["state"]["input"] = {"count": 2}
        else:
            for message in bundle["messages"]:
                if message["type"] == "assistant":
                    message["content"] = [c for c in message["content"] if c is not tool]

    changed = changed_artifact(parts[1], change)
    with pytest.raises(SessionMigrateError):
        prepare((parts[0], changed, *parts[2:]))
    result = verify_projection(prepared, observed(changed), context=parts[-1])
    assert not result.semantic_matches and not result.preserves_content


def test_marker_moved_to_same_text_closed_call_is_rejected(tmp_path):
    records = open_records()
    records[1]["message"]["content"].append(
        {"type": "tool_use", "id": "closed-call", "name": "synthetic_tool", "input": {"count": 1}}
    )
    records[2]["message"]["content"] = [
        {
            "type": "tool_result",
            "tool_use_id": "closed-call",
            "content": INCOMPLETE_TOOL_MESSAGE,
            "is_error": True,
        }
    ]
    parts = archive_parts(tmp_path, records)
    prepared = prepare(parts)

    def change(bundle):
        first, second = bundle_tools(bundle)
        first["state"]["error"]["type"], second["state"]["error"]["type"] = (
            second["state"]["error"]["type"],
            first["state"]["error"]["type"],
        )

    changed = changed_artifact(parts[1], change)
    # The normalized errors are exactly equal; only the native marker identity changed.
    assert _observe(observed(changed), "2.2.5", POLICY).witness.prefix_witness == (
        _observe(observed(parts[1]), "2.2.5", POLICY).witness.prefix_witness
    )
    assert not verify_projection(prepared, observed(changed), context=parts[-1]).semantic_matches
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((parts[0], changed, *parts[2:]))


@pytest.mark.parametrize("content", [[], [{"type": "tool_reference", "tool_name": "lookup"}]])
def test_closed_zero_text_results_are_not_archived(tmp_path, content):
    records = tool_records()
    records[2]["message"]["content"][0]["content"] = content
    parts = archive_parts(tmp_path, records)
    prepared = prepare(parts)
    assert not prepared.expected.generated_members
    assert "incomplete_tool_archived" not in dict(prepared.expected.loss_counts)
    assert tools(parts[1])[0]["state"]["status"] == "completed"
    assert verify_projection(prepared, observed(parts[1]), context=parts[-1]).semantic_matches


@pytest.mark.parametrize(
    "issue", ["missing_name", "missing_id", "duplicate_id", "orphan", "duplicate_result"]
)
def test_archive_policy_does_not_tolerate_other_tool_issues(tmp_path, issue):
    records = tool_records()
    call = records[1]["message"]["content"][0]
    if issue == "missing_name":
        call.pop("name")
    elif issue == "missing_id":
        call.pop("id")
    elif issue == "duplicate_id":
        records[1]["message"]["content"].append(copy.deepcopy(call))
    elif issue == "orphan":
        records[2]["message"]["content"][0]["tool_use_id"] = "orphan"
    else:
        records[2]["message"]["content"] *= 2
    source_obs = _observe(snapshot(records), "2.1.209", POLICY)
    with pytest.raises(ProjectionError, match="source_tools_unproven"):
        expected_from_source(source_obs, target_format=TargetFormat.OPENCODE, policy=POLICY)


@pytest.mark.parametrize(
    "target,client",
    [
        (TargetFormat.OPENCODE, "2.0.23"),
        (TargetFormat.CODEX, "0.144.4"),
        (TargetFormat.CLAUDE, "2.1.209"),
    ],
)
def test_archive_policy_is_bound_to_exact_target_client(tmp_path, target, client):
    parts = archive_parts(tmp_path, tool_records(), target=target, client=client)
    with pytest.raises(ProjectionError, match="unsupported_policy"):
        prepare(parts)


def test_registry_and_recovery_do_not_open_unsupported_formats(tmp_path):
    assert policy_from_id(OPENCODE_ARCHIVE_POLICY_ID) == POLICY
    with pytest.raises(ProjectionError, match="unsupported_policy"):
        policy_from_id("portable-opencode-future")
    parts = fixture(tmp_path)
    record = replace(
        parts[2], versions=replace(parts[2].versions, projection_policy=OPENCODE_ARCHIVE_POLICY_ID)
    )
    with pytest.raises(RecoveryProjectionError, match="unsupported_policy"):
        context_for((*parts[:2], record, *parts[3:]))
    archived = archive_parts(tmp_path)
    path = tmp_path / "synthetic-opencode-input.json"
    path.write_bytes(archived[1].native_bytes)
    captured = capture_source(SourceCaptureContext(AgentFormat.OPENCODE, path))
    with pytest.raises(RecoveryProjectionError, match="proof_unavailable"):
        context_for((parts[0], captured, record, *parts[3:]))


def test_archive_parent_metadata_uses_existing_conversation_witness(tmp_path):
    parts = archive_parts(tmp_path, parent="ses_" + "a" * 32)
    prepared = prepare(parts)
    assert verify_projection(prepared, observed(parts[1]), context=parts[-1]).semantic_matches


def test_generated_members_are_in_prepared_binding(tmp_path):
    prepared = prepare(archive_parts(tmp_path))
    before = _expectation_sha(prepared.expected)
    member = prepared.expected.generated_members[0]
    object.__setattr__(prepared.expected, "generated_members", (replace(member, rule_id="other"),))
    assert _expectation_sha(prepared.expected) != before
    with pytest.raises(ProjectionError, match="binding_changed"):
        validate_prepared(prepared)
