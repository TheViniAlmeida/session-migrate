"""Private synthetic raw grammar/authority tests; no stores, CLIs or providers."""

import copy
import hashlib
import json
import os
import traceback
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from session_migrate.canonical import CanonicalPolicy
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, EventKind
from session_migrate.native_audit import (
    AuditPolicy,
    CanonicalObservation,
    NativeAudit,
    NativeAuditError,
    audit_snapshot,
    canonicalize_snapshot,
    validate_audit,
    validate_observation,
)
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext, capture_source
from session_migrate.witnesses import BoundaryKind


def claude_records():
    base = {"sessionId": "fixture-session", "cwd": "/fixture", "version": "2.1.209"}
    return [
        {
            **base,
            "type": "user",
            "uuid": "u",
            "parentUuid": None,
            "message": {"role": "user", "content": "request"},
        },
        {
            **base,
            "type": "assistant",
            "uuid": "a",
            "parentUuid": "u",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "tool",
                        "input": {
                            "arbitrary": {
                                "business_id": "abc",
                                "bool": True,
                                "float": 1.0,
                                "nil": None,
                            }
                        },
                    }
                ],
            },
        },
        {
            **base,
            "type": "user",
            "uuid": "r",
            "parentUuid": "a",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call",
                        "content": "done",
                        "is_error": False,
                    }
                ],
            },
        },
        {
            **base,
            "type": "assistant",
            "uuid": "z",
            "parentUuid": "r",
            "message": {"role": "assistant", "content": "answer", "stop_reason": "end_turn"},
        },
        {"type": "last-prompt", "leafUuid": "z"},
    ]


def codex_records(paginated=False):
    version = "0.153.4" if paginated else "0.144.4"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": "fixture",
                "session_id": "fixture",
                "cwd": "/fixture",
                "cli_version": version,
                "history_mode": "paginated" if paginated else "legacy",
            },
        }
    ]
    if paginated:
        records += [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "id": "u",
                        "content": [{"type": "text", "text": "request", "text_elements": []}],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "id": "a",
                        "content": [{"type": "Text", "text": "answer"}],
                        "phase": "final_answer",
                    },
                },
            },
        ]
        for index, record in enumerate(records):
            record["ordinal"] = index
    else:
        records += [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "request"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call",
                    "name": "tool",
                    "arguments": '{"x":[1,true,null,{"key":2}]}',
                    "namespace": "namespace",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call",
                    "output": "done",
                    "is_error": True,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            },
        ]
    return records


def opencode_bundle():
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    model = {"id": "fixture", "providerID": "fixture"}
    return {
        "info": {
            "id": "ses_fixture",
            "projectID": "project",
            "title": "fixture",
            "agent": "build",
            "model": model,
            "location": {"directory": "/fixture"},
            "cost": 0,
            "tokens": tokens,
            "time": {"created": 1, "updated": 1},
        },
        "messages": [
            {"id": "msg_user", "type": "user", "time": {"created": 1}, "text": "request"},
            {
                "id": "msg_assistant",
                "type": "assistant",
                "time": {"created": 2, "completed": 3},
                "agent": "build",
                "model": model,
                "finish": "stop",
                "cost": 0,
                "tokens": tokens,
                "content": [
                    {
                        "type": "tool",
                        "id": "call",
                        "name": "tool",
                        "time": {"created": 2, "completed": 3},
                        "state": {
                            "status": "completed",
                            "input": {"free": [True, 1, 1.0]},
                            "content": [{"type": "text", "text": "done"}],
                        },
                    },
                    {"type": "text", "text": "answer"},
                ],
            },
        ],
    }


def snapshot(value, source_format=AgentFormat.CLAUDE, path=Path("virtual")):
    data = (
        json.dumps(value).encode()
        if source_format == AgentFormat.OPENCODE
        else b"\n".join(json.dumps(r).encode() for r in value) + b"\n"
    )
    return NativeSnapshot.from_bytes(data, context=SourceCaptureContext(source_format, path))


def observation(value, source_format=AgentFormat.CLAUDE):
    snap = snapshot(value, source_format)
    policy = (
        AuditPolicy(client_version="2.0.23")
        if source_format == AgentFormat.OPENCODE
        else AuditPolicy()
    )
    audit = audit_snapshot(snap, policy)
    return snap, audit, canonicalize_snapshot(snap, audit, CanonicalPolicy())


@pytest.mark.parametrize(
    "source_format,value",
    [
        (AgentFormat.CLAUDE, claude_records()),
        (AgentFormat.CODEX, codex_records()),
        (AgentFormat.CODEX, codex_records(True)),
        (AgentFormat.OPENCODE, opencode_bundle()),
    ],
)
def test_complete_priority_grammars_are_accounted_without_native_proof(source_format, value):
    snap, audit, obs = observation(copy.deepcopy(value), source_format)
    assert audit.coverage is Coverage.VERIFIED
    assert audit.raw_atom_count == audit.classification.count == len(audit.atoms)
    assert sum(dict(audit.classification_counts).values()) == audit.raw_atom_count
    assert obs.source_prefix.structure == audit.structure
    assert obs.boundary.kind is BoundaryKind.UNPROVEN
    assert obs.source_prefix.closed_turn_count == 0 and obs.identity is None
    assert obs.session.source_sha256 == snap.authority_sha256
    validate_audit(snap, audit)
    validate_observation(obs)


@pytest.mark.parametrize("path", [(), (0, "message"), (1, "message", "content", 0)])
def test_unknown_claude_fields_at_each_level_block_authority(path):
    value = claude_records()
    target = value[0] if not path else value
    for key in path:
        target = target[key]
    target["PRIVATE_KEY_DO_NOT_ECHO"] = {"secret": "PRIVATE_VALUE_DO_NOT_ECHO"}
    _, audit, obs = observation(value)
    assert audit.coverage is Coverage.UNKNOWN and obs.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["unknown_field"]
    assert "PRIVATE" not in repr(audit) + repr(obs) + repr(audit.issues)


@pytest.mark.parametrize(
    "path",
    [("info",), ("info", "location"), ("messages", 1), ("messages", 1, "content", 0, "state")],
)
def test_unknown_opencode_nested_fields_block_authority(path):
    value = opencode_bundle()
    target = value
    for key in path:
        target = target[key]
    target["unknown"] = {"future": True}
    _, audit, _ = observation(value, AgentFormat.OPENCODE)
    assert audit.coverage is Coverage.UNKNOWN


@pytest.mark.parametrize(
    "data,source_format",
    [
        (b'{"type":"user","type":"assistant"}\n', AgentFormat.CLAUDE),
        (b'{"info":{"nested":{"x":1,"x":2}},"messages":[]}', AgentFormat.OPENCODE),
        (b'{"type":"response_item","payload":{"x":NaN}}\n', AgentFormat.CODEX),
        (b'{"type":"response_item","payload":{"x":1e999}}\n', AgentFormat.CODEX),
        (b'{"x":"\\ud800"}\n', AgentFormat.CLAUDE),
        (b'{"x":"\xff"}\n', AgentFormat.CLAUDE),
        (b'{"PRIVATE_VALUE":' + b"[" * 129 + b"0" + b"]" * 129 + b"}\n", AgentFormat.CLAUDE),
    ],
)
def test_strict_decoder_rejects_duplicate_finite_unicode_and_depth(data, source_format):
    snap = NativeSnapshot.from_bytes(
        data, context=SourceCaptureContext(source_format, Path("virtual"))
    )
    with pytest.raises(NativeAuditError) as caught:
        audit_snapshot(snap)
    assert "PRIVATE_VALUE" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("arguments", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}'])
def test_duplicate_or_nonfinite_embedded_tool_json_is_rejected(arguments):
    records = codex_records()
    records[2]["payload"]["arguments"] = arguments
    with pytest.raises(NativeAuditError):
        audit_snapshot(snapshot(records, AgentFormat.CODEX))


def test_tool_input_subtree_keys_and_json_types_are_semantic_not_allowlisted():
    base = claude_records()
    _, audit, first = observation(base)
    value = copy.deepcopy(base)
    value[1]["message"]["content"][0]["input"]["arbitrary"]["bool"] = 1
    _, changed_audit, changed = observation(value)
    assert changed_audit.coverage is Coverage.VERIFIED
    assert audit.classification != changed_audit.classification
    assert first.source_prefix.units != changed.source_prefix.units
    value[1]["message"]["content"][0]["input"]["unregistered_business_key"] = ["keep", 1.0]
    _, extended, _ = observation(value)
    assert extended.coverage is Coverage.VERIFIED


def test_codex_namespace_and_error_are_preserved_in_normalized_semantics():
    _, audit, obs = observation(codex_records(), AgentFormat.CODEX)
    call = next(e for e in obs.events if e.kind is EventKind.TOOL_CALL)
    result = next(e for e in obs.events if e.kind is EventKind.TOOL_RESULT)
    assert call.payload["namespace"] == "namespace"
    assert result.payload["is_error"] is True
    original = next(e for e in obs.session.events if e.kind is EventKind.TOOL_RESULT)
    assert "is_error" not in original.payload
    assert dict(audit.issue_counts)["parser_semantic_enrichment"]
    assert audit.coverage is Coverage.VERIFIED and obs.fidelity is Coverage.PARTIAL


@pytest.mark.parametrize(
    "mutation", ["duplicate", "parent", "cycle", "leaf", "role", "missing_uuid"]
)
def test_claude_graph_and_scope_guards_fail_closed(mutation):
    records = claude_records()
    if mutation == "duplicate":
        records[1]["uuid"] = "u"
    elif mutation == "parent":
        records[1]["parentUuid"] = "missing"
    elif mutation == "cycle":
        records[0]["parentUuid"] = "z"
    elif mutation == "leaf":
        records[-1]["leafUuid"] = "missing"
    elif mutation == "role":
        records[0]["message"]["role"] = "assistant"
    else:
        del records[0]["uuid"]
    audit = audit_snapshot(snapshot(records))
    assert audit.coverage is Coverage.UNKNOWN


def test_inactive_branch_is_accounted_and_leaf_selection_changes_structure():
    records = claude_records()
    inactive = copy.deepcopy(records[0])
    inactive["uuid"] = "inactive"
    inactive["message"]["content"] = "alternate"
    records.insert(-1, inactive)
    _, audit, obs = observation(records)
    assert audit.coverage is Coverage.VERIFIED
    assert obs.fidelity is Coverage.PARTIAL
    assert dict(audit.classification_counts)["selected-scope-exclusion"] > 0
    changed = copy.deepcopy(records)
    changed[-1]["leafUuid"] = "inactive"
    _, changed_audit, _ = observation(changed)
    assert audit.selection_sha256 != changed_audit.selection_sha256
    assert audit.structure != changed_audit.structure


def test_unknown_inactive_body_cannot_hide_behind_scope_exclusion():
    records = claude_records()
    inactive = copy.deepcopy(records[0])
    inactive["uuid"] = "inactive"
    inactive["message"]["unknown"] = "opaque"
    records.insert(-1, inactive)
    audit = audit_snapshot(snapshot(records))
    assert audit.coverage is Coverage.UNKNOWN


def test_codex_duplicate_event_pairing_requires_full_occurrence_bijection():
    records = codex_records()
    records += [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "request"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "answer"}},
    ]
    _, audit, _ = observation(records, AgentFormat.CODEX)
    assert audit.coverage is Coverage.VERIFIED
    assert dict(audit.classification_counts)["selected-scope-exclusion"]
    records.append(copy.deepcopy(records[-1]))
    audit2 = audit_snapshot(snapshot(records, AgentFormat.CODEX))
    assert audit2.coverage is Coverage.UNKNOWN
    assert dict(audit2.issue_counts)["ambiguous_event_pairing"]


@pytest.mark.parametrize("ordinal", [True, -1, 100, "1"])
def test_paginated_ordinal_guards(ordinal):
    records = codex_records(True)
    records[1]["ordinal"] = ordinal
    audit = audit_snapshot(snapshot(records, AgentFormat.CODEX))
    assert audit.coverage is Coverage.UNKNOWN


def test_provider_message_body_is_known_loss_not_accounting():
    records = codex_records(True)
    records.append(
        {
            "ordinal": 3,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "provider-private body"}],
            },
        }
    )
    _, audit, obs = observation(records, AgentFormat.CODEX)
    assert audit.coverage is Coverage.VERIFIED
    assert obs.fidelity is Coverage.PARTIAL
    assert dict(audit.classification_counts)["known-loss"] > 0
    assert all(event.kind is not EventKind.OPAQUE for event in obs.events)


def test_unknown_provider_metadata_remains_unknown_even_with_known_loss():
    records = codex_records(True)
    records.append(
        {
            "ordinal": 3,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "provider body"}],
                "providerState": {"opaque": "body"},
            },
        }
    )
    audit = audit_snapshot(snapshot(records, AgentFormat.CODEX))
    assert audit.coverage is Coverage.UNKNOWN


def test_compaction_and_serializer_finish_do_not_establish_boundary():
    records = claude_records()
    records[0]["isCompactSummary"] = True
    _, audit, obs = observation(records)
    assert dict(audit.issue_counts)["compaction_unproven"]
    assert obs.boundary.kind is BoundaryKind.UNPROVEN and obs.identity is None
    _, audit2, target = observation(opencode_bundle(), AgentFormat.OPENCODE)
    assert audit2.coverage is Coverage.VERIFIED
    assert target.boundary.kind is BoundaryKind.UNPROVEN


def test_unrecoverable_opaque_is_unknown():
    records = claude_records()
    records[0]["message"]["content"] = [{"type": "new-provider-carrier", "opaque": "body"}]
    _, audit, _ = observation(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["unrecoverable_opaque"]


def test_inline_media_changes_semantics_and_remote_reference_is_partial():
    records = claude_records()
    records[0]["message"]["content"] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}
    ]
    _, audit, first = observation(records)
    assert audit.coverage is Coverage.VERIFIED
    changed = copy.deepcopy(records)
    changed[0]["message"]["content"][0]["source"]["data"] = "Yg=="
    _, _, second = observation(changed)
    assert first.source_prefix.units != second.source_prefix.units
    changed[0]["message"]["content"][0]["source"] = {
        "type": "url",
        "url": "https://fixture.invalid/image",
    }
    _, _, remote = observation(changed)
    assert remote.fidelity is Coverage.PARTIAL


def test_ten_thousand_atoms_and_bounded_diagnostics_cover_full_sequence():
    records = claude_records()
    records[1]["message"]["content"][0]["input"] = {f"business_{i}": i for i in range(10000)}
    _, audit, obs = observation(records)
    assert audit.raw_atom_count > 10000
    assert audit.classification.count == len(audit.atoms) == audit.raw_atom_count
    assert obs.coverage is Coverage.VERIFIED
    unknown = copy.deepcopy(records)
    unknown[0]["unknown"] = {f"hidden_{i}": i for i in range(10000)}
    audit2 = audit_snapshot(snapshot(unknown))
    assert len(audit2.issues) <= 1024
    assert dict(audit2.issue_counts)["unknown_atom"] > 10000
    assert audit2.classification.count > 20000


def test_same_bytes_in_different_handle_do_not_inherit_private_authority():
    snap, audit, _ = observation(claude_records())
    other = NativeSnapshot.from_bytes(
        snap.raw_bytes, context=SourceCaptureContext(AgentFormat.CLAUDE, snap.source_path)
    )
    with pytest.raises(NativeAuditError):
        validate_audit(other, audit)
    with pytest.raises(NativeAuditError):
        validate_audit(snap, NativeAudit())
    with pytest.raises(NativeAuditError):
        validate_observation(CanonicalObservation())


def test_views_are_fresh_and_metadata_forgery_rejected():
    snap, audit, obs = observation(claude_records())
    original = asdict(obs.session)
    exposed = obs.session
    next(e for e in exposed.events if e.kind is EventKind.TOOL_CALL).payload["input"]["extra"] = (
        "mutation"
    )
    next(e for e in obs.events if e.kind is EventKind.TOOL_CALL).payload["input"]["extra"] = (
        "another mutation"
    )
    assert asdict(obs.session) == original
    validate_observation(obs)
    with pytest.raises(FrozenInstanceError):
        audit.authority_sha256 = "0" * 64
    object.__setattr__(audit, "selection_sha256", "0" * 64)
    with pytest.raises(NativeAuditError):
        validate_audit(snap, audit)


def test_same_stat_semantic_edit_requires_fresh_authority_comparison(tmp_path):
    records = claude_records()
    path = tmp_path / "synthetic.jsonl"
    path.write_bytes(snapshot(records).raw_bytes)
    before_stat = path.stat()
    first = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, path))
    records[0]["message"]["content"] = "changed"
    second_bytes = snapshot(records).raw_bytes
    assert len(second_bytes) == first.identity.size
    path.write_bytes(second_bytes)
    os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    second = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, path))
    assert second.identity.size == first.identity.size
    assert second.identity.modified_ns == first.identity.modified_ns
    assert second.authority_sha256 != first.authority_sha256
    assert hashlib.sha256(first.raw_bytes).hexdigest() == first.authority_sha256


def test_snapshot_field_forgery_fails_binding():
    snap, audit, _ = observation(claude_records())
    object.__setattr__(snap, "source_path", Path("forged"))
    with pytest.raises(NativeAuditError):
        canonicalize_snapshot(snap, audit, CanonicalPolicy())


def test_unsupported_versions_are_unknown_and_portable_opencode_requires_version_context():
    records = claude_records()
    for record in records:
        if "version" in record:
            record["version"] = "2.1.future"
    assert audit_snapshot(snapshot(records)).coverage is Coverage.UNKNOWN
    snap = snapshot(opencode_bundle(), AgentFormat.OPENCODE)
    assert audit_snapshot(snap).coverage is Coverage.UNKNOWN


@pytest.mark.parametrize(
    "source_format,value",
    [
        (AgentFormat.CLAUDE, claude_records()),
        (AgentFormat.CODEX, codex_records()),
        (AgentFormat.OPENCODE, opencode_bundle()),
    ],
)
def test_audit_and_parser_use_captured_authority_without_path_access(
    monkeypatch, source_format, value
):
    snap = snapshot(value, source_format)

    def forbidden(*args, **kwargs):
        pytest.fail("audit or parser touched a source path")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    policy = (
        AuditPolicy(client_version="2.0.23")
        if source_format == AgentFormat.OPENCODE
        else AuditPolicy()
    )
    audit = audit_snapshot(snap, policy)
    obs = canonicalize_snapshot(snap, audit, CanonicalPolicy())
    assert obs.session.source_path == snap.source_path
    assert obs.session.source_sha256 == snap.authority_sha256


def test_inactive_logical_cycle_is_not_hidden_by_selection():
    records = claude_records()
    records.insert(
        -1,
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "cycle_a",
            "parentUuid": None,
            "logicalParentUuid": "cycle_b",
        },
    )
    records.insert(
        -1,
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "cycle_b",
            "parentUuid": None,
            "logicalParentUuid": "cycle_a",
        },
    )
    audit = audit_snapshot(snapshot(records))
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["invalid_graph"]


def test_preserved_segment_declaration_requires_full_unique_membership():
    records = claude_records()
    records.insert(
        -1,
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "compactMetadata": {
                "preservedSegment": {"anchorUuid": "u", "headUuid": "a", "tailUuid": "z"},
                "preservedMessages": {"allUuids": ["a", "r", "z", "z"]},
            },
        },
    )
    audit = audit_snapshot(snapshot(records))
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["invalid_graph"]


def test_known_reasoning_loss_body_remains_bound_even_when_parser_discards_it():
    records = codex_records()
    records.append(
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "encrypted_content": "known-body-one"},
        }
    )
    _, audit, obs = observation(records, AgentFormat.CODEX)
    changed = copy.deepcopy(records)
    changed[-1]["payload"]["encrypted_content"] = "known-body-two"
    _, other_audit, other_obs = observation(changed, AgentFormat.CODEX)
    assert audit.coverage is Coverage.VERIFIED
    assert obs.fidelity is Coverage.PARTIAL
    assert asdict(obs.session) != asdict(other_obs.session)  # Authority SHA differs.
    assert audit.classification != other_audit.classification
    assert obs.source_prefix.units == other_obs.source_prefix.units


def test_custom_tool_embedded_json_duplicates_cannot_escape_strict_decoder():
    records = codex_records()
    records[2]["payload"]["type"] = "custom_tool_call"
    records[2]["payload"]["arguments"] = '{"x":1,"x":2}'
    with pytest.raises(NativeAuditError):
        audit_snapshot(snapshot(records, AgentFormat.CODEX))


def test_opencode_known_structured_error_loss_keeps_raw_audit_complete():
    value = opencode_bundle()
    value["messages"][1]["content"][0]["state"] = {
        "status": "error",
        "input": {},
        "error": {"type": "ToolError", "message": "error result", "status": 500},
    }
    _, audit, obs = observation(value, AgentFormat.OPENCODE)
    assert audit.coverage is Coverage.VERIFIED
    assert obs.fidelity is Coverage.PARTIAL
    assert all(event.kind is not EventKind.OPAQUE for event in obs.events)


def test_opencode_unknown_metadata_cannot_be_recovered_by_a_known_loss_elsewhere():
    value = opencode_bundle()
    value["info"]["model"]["variant"] = "known-loss"
    value["messages"][0]["metadata"] = {"private": "unknown-body"}
    _, audit, _ = observation(value, AgentFormat.OPENCODE)
    assert audit.coverage is Coverage.UNKNOWN


def test_inactive_body_change_updates_structure_root():
    value = claude_records()
    branch = copy.deepcopy(value[0])
    branch["uuid"] = "inactive"
    value.insert(-1, branch)
    _, first, _ = observation(value)
    value[-2]["message"]["content"] = "different inactive body"
    _, second, _ = observation(value)
    assert first.structure != second.structure


def test_changed_external_audit_policy_reference_is_rejected():
    snap = snapshot(claude_records())
    policy = AuditPolicy(client_version="2.1.209")
    audit = audit_snapshot(snap, policy)
    object.__setattr__(policy, "client_version", "2.1.future")
    with pytest.raises(NativeAuditError):
        validate_audit(snap, audit)


def test_known_field_with_unknown_message_discriminator_is_unknown_schema():
    value = claude_records()
    value[1]["message"]["type"] = "future-client-body"
    audit = audit_snapshot(snapshot(value))
    assert audit.coverage is Coverage.UNKNOWN


def test_mutated_snapshot_with_invalid_typed_context_is_rejected_before_audit():
    snap = snapshot(claude_records())
    object.__setattr__(snap, "source_path", "PRIVATE_PATH_NOT_A_PATH")
    with pytest.raises(NativeAuditError):
        audit_snapshot(snap)


def test_unsupported_client_value_is_not_exposed_as_a_client_identifier():
    value = claude_records()
    for item in value:
        if "version" in item:
            item["version"] = "PRIVATE_BODY_NOT_A_CLIENT_VERSION"
    audit = audit_snapshot(snapshot(value))
    assert audit.coverage is Coverage.UNKNOWN and audit.client_id is None


def _codex_priority_source_without_other_losses():
    records = codex_records()
    del records[2]["payload"]["namespace"]
    del records[3]["payload"]["is_error"]
    return records


def _prepare_codex_priority(records, tmp_path):
    from session_migrate.conversion import ConversionOptions, convert_session
    from session_migrate.lineage import PolicyVersions
    from session_migrate.model import TargetFormat
    from session_migrate.projections import ProjectionContext, prepare_projection

    source = snapshot(records, AgentFormat.CODEX)
    options = ConversionOptions(
        TargetFormat.CLAUDE,
        session_id="50000000-0000-4000-8000-000000000001",
        cwd=tmp_path,
        target_cli_version="2.1.209",
    )
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        "0.144.4",
        "2.1.209",
    )
    return (
        source,
        artifact,
        prepare_projection(
            source,
            artifact,
            options=options,
            versions=versions,
            context=ProjectionContext(TargetFormat.CLAUDE, "2.1.209"),
        ),
    )


@pytest.mark.parametrize("detail", ["low", "high"])
def test_image_detail_loss_never_gets_a_full_preservation_certificate(tmp_path, detail):
    records = _codex_priority_source_without_other_losses()
    records[1]["payload"]["content"].append(
        {"type": "input_image", "image_url": "data:image/png;base64,YQ==", "detail": detail}
    )
    source, artifact, prepared = _prepare_codex_priority(records, tmp_path)
    audit = audit_snapshot(source)
    obs = canonicalize_snapshot(source, audit, CanonicalPolicy())
    assert audit.coverage is Coverage.VERIFIED and obs.fidelity is Coverage.PARTIAL
    assert dict(audit.issue_counts)["image_detail_loss"]
    assert any(atom.category == "known-loss" and atom.path[-1] == "detail" for atom in audit.atoms)
    assert prepared.certificate is None and not prepared.expected.preserves_content
    assert b'"detail"' not in artifact.native_bytes


def test_image_detail_edits_change_raw_roots_but_never_promote_equal_ir():
    records = _codex_priority_source_without_other_losses()
    records[1]["payload"]["content"].append(
        {"type": "input_image", "image_url": "data:image/png;base64,YQ==", "detail": "low"}
    )
    first, first_audit, first_obs = observation(records, AgentFormat.CODEX)
    records[1]["payload"]["content"][-1]["detail"] = "high"
    second, second_audit, second_obs = observation(records, AgentFormat.CODEX)
    assert first.authority_sha256 != second.authority_sha256
    assert first_audit.classification != second_audit.classification
    assert first_obs.source_prefix.units == second_obs.source_prefix.units
    assert first_obs.fidelity is second_obs.fidelity is Coverage.PARTIAL


def test_shadowed_input_alias_is_explicit_loss_without_full_certificate(tmp_path):
    records = _codex_priority_source_without_other_losses()
    records[2]["payload"]["input"] = {"shadowed": "source content"}
    source, _, prepared = _prepare_codex_priority(records, tmp_path)
    audit = audit_snapshot(source)
    obs = canonicalize_snapshot(source, audit, CanonicalPolicy())
    assert dict(audit.issue_counts)["semantic_alias_shadow"]
    assert audit.coverage is Coverage.VERIFIED and obs.fidelity is Coverage.PARTIAL
    assert any(
        atom.path == ("payload", "input") and atom.category == "known-loss" for atom in audit.atoms
    )
    assert prepared.certificate is None and not prepared.expected.preserves_content


@pytest.mark.parametrize("raw_input", ['{"literal":1}', "[1,true]", "null", '"literal"'])
def test_custom_tool_json_string_shape_loss_has_no_full_certificate(tmp_path, raw_input):
    records = _codex_priority_source_without_other_losses()
    records[2]["payload"]["type"] = "custom_tool_call"
    del records[2]["payload"]["arguments"]
    records[2]["payload"]["input"] = raw_input
    source, _, prepared = _prepare_codex_priority(records, tmp_path)
    audit = audit_snapshot(source)
    obs = canonicalize_snapshot(source, audit, CanonicalPolicy())
    assert dict(audit.issue_counts)["custom_input_json_shape_loss"]
    assert audit.coverage is Coverage.VERIFIED and obs.fidelity is Coverage.PARTIAL
    assert prepared.certificate is None and not prepared.expected.preserves_content


@pytest.mark.parametrize(
    "mutation,issue",
    [
        ("image_alias", "semantic_alias_shadow"),
        ("phase", "phase_context_loss"),
        ("result_output_text", "opaque_result_carrier_loss"),
        ("empty_text", "empty_text_carrier_loss"),
    ],
)
def test_semantic_slot_census_marks_other_reader_gaps_partial(mutation, issue):
    records = _codex_priority_source_without_other_losses()
    if mutation == "image_alias":
        records[1]["payload"]["content"].append(
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,YQ==",
                "url": "data:image/png;base64,Yg==",
            }
        )
    elif mutation == "phase":
        records[1]["payload"]["phase"] = "analysis"
    elif mutation == "result_output_text":
        records[3]["payload"]["output"] = [{"type": "output_text", "text": "reader drops this"}]
    else:
        records[1]["payload"]["content"].append({"type": "input_text", "text": ""})
    _, audit, obs = observation(records, AgentFormat.CODEX)
    assert dict(audit.issue_counts)[issue]
    assert audit.coverage is Coverage.VERIFIED and obs.fidelity is Coverage.PARTIAL


def test_claude_empty_message_semantic_carrier_is_known_loss():
    records = claude_records()
    records[0]["message"]["content"] = ""
    _, audit, obs = observation(records)
    assert audit.coverage is Coverage.VERIFIED and obs.fidelity is Coverage.PARTIAL
    assert dict(audit.classification_counts)["known-loss"]


def test_claude_invalid_tool_result_image_cannot_be_verified_fidelity():
    records = claude_records()
    records[2]["message"]["content"][0]["content"] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "invalid base64!"},
        }
    ]
    _, audit, obs = observation(records)
    assert obs.fidelity is Coverage.PARTIAL
    assert dict(audit.issue_counts)["opaque_result_carrier_loss"]
