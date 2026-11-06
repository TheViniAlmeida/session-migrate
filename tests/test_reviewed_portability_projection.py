"""Reviewed conversation proofs never claim native runtime equivalence."""

import json
from dataclasses import replace

import pytest
from test_claude_desktop309 import desktop
from test_incomplete_tool_projection import changed_artifact
from test_native_context_projection import context_records
from test_projections import ID, observed, snapshot

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage, PolicyVersions
from session_migrate.model import AgentFormat, EventKind, TargetFormat
from session_migrate.native_audit import AuditPolicy, AuditSelection, audit_snapshot
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.projection_policies import (
    REVIEWED_ARCHIVE_POLICY_ID,
    REVIEWED_POLICY_ID,
    ProjectionError,
    ProjectionPolicy,
    expected_from_source,
    reviewed_tool_reference_text,
)
from session_migrate.projections import (
    ProjectionContext,
    _observe,
    _options_dict,
    _session_sha,
    prepare_projection,
    reviewed_portability_report,
    validate_prepared,
    verify_projection,
)

POLICY = ProjectionPolicy(REVIEWED_POLICY_ID)


def rows(*, interposed=True, thinking=False):
    assistant = [{"type": "text", "text": " assistant ação \n"}]
    if thinking:
        assistant.append({"type": "thinking", "thinking": "quoted thought", "signature": "data"})
    assistant.append({"type": "tool_use", "id": "reviewed-call", "name": "Read", "input": {"n": 1}})
    result = context_records()
    result[1]["message"]["content"] = "user original\u2028"
    result[6]["message"]["content"] = assistant
    result[-2]["message"]["content"] = [
        {"type": "tool_result", "tool_use_id": "reviewed-call", "content": "result\n "},
    ]
    if interposed:
        result[-2]["parentUuid"] = "middle"
        result.insert(7, desktop("assistant", "middle", "after", "intervening answer"))
    return result


def parts(tmp_path, records=None, *, policy=POLICY):
    source = snapshot(rows() if records is None else records)
    session = source.parse(reviewed_portability=True)
    options = ConversionOptions(
        TargetFormat.OPENCODE,
        session_id=ID,
        cwd=tmp_path,
        target_cli_version="2.2.5",
        reviewed_portability=True,
    )
    artifact = convert_session(session, options)
    versions = PolicyVersions(
        "canonical-v2",
        policy.policy_id,
        "native-audit-v1",
        "native-audit-v1",
        "2.1.309",
        "2.2.5",
    )
    return source, artifact, options, versions, ProjectionContext(TargetFormat.OPENCODE, "2.2.5")


def prepare(values, *, policy=POLICY):
    return prepare_projection(
        values[0],
        values[1],
        options=values[2],
        versions=values[3],
        context=values[4],
        policy=policy,
    )


def test_reviewed_order_is_declared_and_never_certifies_full(tmp_path):
    values = parts(tmp_path)
    prepared = prepare(values)
    result = verify_projection(prepared, observed(values[1]), context=values[4])
    assert result.semantic_matches and result.structure_matches
    assert result.expectation_coverage is not Coverage.VERIFIED
    assert prepared.certificate is None
    assert not result.preserves_content and not result.delta_eligible
    assert not result.identity_proven and not result.boundary_proven
    assert dict(result.loss_counts)["reviewed_runtime_not_preserved"] == 1
    assert dict(result.loss_counts)["authority_as_data"] >= 1
    report = reviewed_portability_report(prepared)
    assert report["order"]["changed"]
    assert report["order"]["source_displayed_sha256"] != report["order"]["target_effective_sha256"]
    encoded = json.dumps(report)
    assert "reviewed-call" not in encoded and "intervening answer" not in encoded
    assert (
        prepared.source_observation.audit.selection_policy_id
        == "claude309-reviewed-conversation-v1"
    )
    assert prepared.artifact_observation.audit.selection_policy_id == "native-active-v1"


def test_source_audit_recalculates_raw_and_expected_does_not_call_writer(tmp_path, monkeypatch):
    values = parts(tmp_path)
    source = _observe(values[0], "2.1.309", POLICY, reviewed_portability=True)

    def forbidden(*args, **kwargs):
        pytest.fail("independent audit expectation called writer")

    monkeypatch.setattr("session_migrate.formats.opencode_v2.from_legacy", forbidden)
    expected = expected_from_source(source, target_format=TargetFormat.OPENCODE, policy=POLICY)
    assert any(
        e.kind is EventKind.TOOL_CALL and e.tool_call_id == "reviewed-call" for e in expected.events
    )
    changed = replace(
        values[1].source,
        events=tuple(
            replace(e, text="forged conversation") if e.kind is EventKind.MESSAGE else e
            for e in values[1].source.events
        ),
    )
    with pytest.raises(ProjectionError, match="source_mismatch"):
        prepare((values[0], replace(values[1], source=changed), *values[2:]))


@pytest.mark.parametrize(
    "tamper",
    [
        "text",
        "input",
        "name",
        "id",
        "output",
        "order",
        "native",
        "system",
        "executed",
        "providerState",
        "permissions",
        "contextmarker",
        "errorflag",
    ],
)
def test_tampered_prepared_and_cold_content_or_order_fails(tmp_path, tamper):
    values = parts(tmp_path)
    prepared = prepare(values)

    def change(bundle):
        tool = next(
            p for m in bundle["messages"] for p in m.get("content", []) if p["type"] == "tool"
        )
        if tamper == "text":
            bundle["messages"][0]["text"] += "changed"
        elif tamper == "input":
            tool["state"]["input"] = {"n": 2}
        elif tamper == "name":
            tool["name"] = "Other"
        elif tamper == "id":
            tool["id"] = "different-call"
        elif tamper == "output":
            tool["state"]["content"][0]["text"] += "changed"
        elif tamper == "order":
            messages = bundle["messages"]
            messages[1], messages[2] = messages[2], messages[1]
        elif tamper == "native":
            tool["native"] = {"hosted": True}
        elif tamper == "executed":
            tool["executed"] = True
        elif tamper == "providerState":
            tool["providerState"] = {"historical": True}
        elif tamper == "permissions":
            bundle["info"]["permissions"] = {"*": "allow"}
        elif tamper == "contextmarker":
            next(m for m in bundle["messages"] if m["type"] == "synthetic")["metadata"][
                "session_migrate_context"
            ]["version"] = 2
        elif tamper == "errorflag":
            tool["state"]["status"] = "error"
            tool["state"]["error"] = {"type": "ToolError", "message": "result\n "}
        else:
            bundle["messages"][0]["type"] = "system"

    changed = changed_artifact(values[1], change)
    with pytest.raises(SessionMigrateError):
        prepare((values[0], changed, *values[2:]))
    try:
        verified = verify_projection(prepared, observed(changed), context=values[4])
    except SessionMigrateError:
        pass  # A malformed native envelope may be refused before comparison.
    else:
        assert not verified.semantic_matches


def test_source_unknown_stays_unknown_and_policy_mode_swaps_are_rejected(tmp_path):
    records = rows()
    records[0]["unrecognized_payload"] = {"content": "material"}
    values = parts(tmp_path, records)
    prepared = prepare(values)
    assert prepared.source_coverage is Coverage.UNKNOWN
    assert prepared.expectation_coverage is Coverage.UNKNOWN
    assert not prepared.expected.relation_complete
    audit = audit_snapshot(
        values[0],
        AuditPolicy(client_version="2.1.309"),
        AuditSelection("claude309-reviewed-conversation-v1"),
    )
    assert dict(audit.classification_counts)["unknown"] > 0
    with pytest.raises(ProjectionError, match="options_mismatch"):
        prepare((*values[:2], replace(values[2], reviewed_portability=False), *values[3:]))
    with pytest.raises(ProjectionError, match="unsupported_policy"):
        expected_from_source(prepared.source_observation, target_format=TargetFormat.OPENCODE)


def test_report_mutation_is_bound_and_legacy_false_omitted(tmp_path):
    values = parts(tmp_path)
    prepared = prepare(values)
    report = reviewed_portability_report(prepared)
    report["order"]["changed"] = False
    validate_prepared(prepared)
    assert reviewed_portability_report(prepared)["order"]["changed"]
    assert "reviewed_portability" not in _options_dict(
        replace(values[2], reviewed_portability=False)
    )
    assert _options_dict(values[2])["reviewed_portability"] is True
    original = _session_sha(values[1].source)
    values[1].source.reviewed_report["tampered"] = True
    assert _session_sha(values[1].source) != original
    with pytest.raises(ProjectionError, match="binding_changed"):
        validate_prepared(prepared)


def test_reviewed_thinking_is_exact_user_data_with_reserved_markers(tmp_path):
    values = parts(tmp_path, rows(thinking=True))
    prepared = prepare(values)
    contexts = [
        m for m in json.loads(values[1].native_bytes)["messages"] if m["type"] == "synthetic"
    ]
    assert any("quoted thought" in m["text"] for m in contexts)
    assert all(m["metadata"]["session_migrate_context"]["version"] == 1 for m in contexts)
    changed = changed_artifact(
        values[1],
        lambda bundle: next(m for m in bundle["messages"] if m["type"] == "synthetic")["metadata"][
            "session_migrate_context"
        ].update(version=2),
    )
    with pytest.raises(SessionMigrateError):
        prepare((values[0], changed, *values[2:]))
    assert not verify_projection(prepared, observed(changed), context=values[4]).semantic_matches


def test_reviewed_archive_requires_explicit_policy_and_exact_generated_rule(tmp_path):
    records = rows(interposed=False)
    records[-2]["message"]["content"] = "not a tool completion"
    values = parts(tmp_path, records)
    with pytest.raises(ProjectionError, match="source_tools_unproven"):
        prepare(values)
    policy = ProjectionPolicy(REVIEWED_ARCHIVE_POLICY_ID)
    archived_values = (
        *values[:3],
        replace(values[3], projection_policy=policy.policy_id),
        values[4],
    )
    prepared = prepare(archived_values, policy=policy)
    assert len(prepared.expected.generated_members) == 1
    assert prepared.certificate is None
    result = verify_projection(prepared, observed(values[1]), context=values[4])
    assert result.semantic_matches and not result.preserves_content


def test_reviewed_selection_is_rejected_on_other_source_formats(tmp_path):
    values = parts(tmp_path)
    source = NativeSnapshot.from_bytes(
        values[1].native_bytes,
        context=SourceCaptureContext(AgentFormat.OPENCODE, tmp_path / "target"),
    )
    with pytest.raises(SessionMigrateError):
        audit_snapshot(
            source,
            AuditPolicy(client_version="2.2.5"),
            AuditSelection("claude309-reviewed-conversation-v1"),
        )


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"type": "url", "url": "https://example.invalid/image.png"}},
        {"type": "unknown_material", "body": "must never disappear"},
    ],
)
def test_reviewed_mode_does_not_license_omitting_material_content(tmp_path, block):
    records = rows()
    records[1]["message"]["content"] = [block]
    with pytest.raises(SessionMigrateError):
        prepare(parts(tmp_path, records))


def test_reviewed_multi_call_result_permutation_is_concrete_and_content_exact(tmp_path):
    records = rows(interposed=False)
    records[6]["message"]["content"].append(
        {
            "type": "tool_use",
            "id": "second-call",
            "name": "Other",
            "input": {"x": "value"},
        }
    )
    records[-2]["message"]["content"].insert(
        0,
        {
            "type": "tool_result",
            "tool_use_id": "second-call",
            "content": "second result",
        },
    )
    values = parts(tmp_path, records)
    prepared = prepare(values)
    result = verify_projection(prepared, observed(values[1]), context=values[4])
    assert result.semantic_matches and result.structure_matches
    report = reviewed_portability_report(prepared)
    assert report["order"]["changed"]
    assert report["order"]["source_units"] == report["order"]["target_units"]
    mapping = report["order"]["source_to_target"]
    assert sorted(mapping) == list(range(len(mapping)))
    assert mapping != list(range(len(mapping)))
    assert dict(result.loss_counts)["reviewed_context_order_changed"] == 1


def test_reviewed_tool_reference_is_quoted_in_place_and_independently_verified(tmp_path):
    records = rows(interposed=False)
    blocks = [
        {"type": "text", "text": "before\n "},
        {"type": "tool_reference", "tool_name": 'historical"\nReference'},
        {"type": "text", "text": " after\u2028"},
    ]
    records[-2]["message"]["content"][0]["content"] = blocks
    values = parts(tmp_path, records)
    prepared = prepare(values)
    original = next(e for e in values[1].source.events if e.kind is EventKind.TOOL_RESULT)
    assert original.payload["content_blocks"] == blocks
    expected_blocks = [
        blocks[0],
        {
            "type": "text",
            "text": reviewed_tool_reference_text(
                blocks[1]["tool_name"],
            ),
        },
        blocks[2],
    ]
    target = json.loads(values[1].native_bytes)
    tool = next(p for m in target["messages"] for p in m.get("content", []) if p["type"] == "tool")
    assert tool["state"]["content"] == expected_blocks
    result = verify_projection(prepared, observed(values[1]), context=values[4])
    assert result.semantic_matches and not result.preserves_content
    assert dict(result.loss_counts)["reviewed_tool_reference_as_data"] == 1
    report = reviewed_portability_report(prepared)
    assert any(
        d["code"] == "reviewed_tool_reference_as_data" and d["count"] == 1
        for d in report["differences"]
    )
    assert blocks[1]["tool_name"] not in json.dumps(report)
    forged = changed_artifact(
        values[1],
        lambda bundle: next(
            p for m in bundle["messages"] for p in m.get("content", []) if p["type"] == "tool"
        )["state"]["content"][1].update(text="different reference"),
    )
    assert not verify_projection(prepared, observed(forged), context=values[4]).semantic_matches


def test_tool_error_envelope_is_explicit_counted_and_bound_before_ack(tmp_path):
    from session_migrate.projections import _expectation_sha

    records = rows(interposed=False)
    source_result = records[-2]["message"]["content"][0]
    source_result["is_error"] = True
    source_result["content"] = "exact error ação\n  "
    values = parts(tmp_path, records)
    prepared = prepare(values)
    report = reviewed_portability_report(prepared)
    difference = next(
        d for d in report["differences"] if d["code"] == "reviewed_tool_error_envelope"
    )
    assert difference["count"] == 1
    assert difference["record_indices"] == [len(records) - 2]
    assert (
        "ToolError" in difference["description"] and "bare error text" in difference["description"]
    )
    assert "exact error" not in json.dumps(report)
    assert dict(prepared.expected.loss_counts)["reviewed_tool_error_envelope"] == 1
    bundle = json.loads(values[1].native_bytes)
    tool = next(p for m in bundle["messages"] for p in m.get("content", []) if p["type"] == "tool")
    assert tool["state"]["status"] == "error"
    assert tool["state"]["error"] == {"type": "ToolError", "message": source_result["content"]}
    verified = verify_projection(prepared, observed(values[1]), context=values[4])
    assert verified.semantic_matches and not verified.preserves_content
    assert prepared.certificate is None and not verified.delta_eligible
    assert _expectation_sha(
        replace(
            prepared.expected,
            loss_counts=tuple(
                (code, count + 1 if code == "reviewed_tool_error_envelope" else count)
                for code, count in prepared.expected.loss_counts
            ),
        )
    ) != _expectation_sha(prepared.expected)
    actual_result = next(e for e in values[1].source.events if e.kind is EventKind.TOOL_RESULT)
    actual_result.payload["is_error"] = False
    with pytest.raises(ProjectionError, match="binding_changed"):
        validate_prepared(prepared)


def test_no_tool_error_envelope_difference_is_invented_for_successes(tmp_path):
    prepared = prepare(parts(tmp_path))
    assert "reviewed_tool_error_envelope" not in dict(prepared.expected.loss_counts)
    assert not any(
        d["code"] == "reviewed_tool_error_envelope"
        for d in reviewed_portability_report(prepared)["differences"]
    )
