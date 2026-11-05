"""Read-only Desktop 2.1.309 compatibility never grants completeness authority."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
from test_claude_latest import _observe, _parse, _record

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, EventKind
from session_migrate.native_audit import audit_snapshot
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.tree_sources import TreeSourceError, inventory_from_native_metadata
from session_migrate.witnesses import BoundaryKind


def desktop(role="user", uid="u", parent=None, content="request", **fields):
    return _record(
        role,
        uid,
        parent,
        content,
        version="2.1.309",
        timestamp="2026-11-05T20:00:53-03:00",
        **fields,
    )


def messages(session):
    return [event.text for event in session.events if event.kind is EventKind.MESSAGE]


def audit_rows(rows, tmp_path):
    data = encode_jsonl(rows)
    return audit_snapshot(
        NativeSnapshot.from_bytes(
            data,
            context=SourceCaptureContext(AgentFormat.CLAUDE, tmp_path / "source.jsonl"),
        )
    )


def test_desktop_read_contract_is_separate_from_validated_writer_contract(tmp_path):
    assert "2.1.309" in claude.READABLE_CLAUDE_VERSIONS
    assert "2.1.309" not in claude.VALIDATED_CLAUDE_VERSIONS
    assert claude.PINNED_CLAUDE_VERSION == "2.1.209"
    rows = [desktop(), desktop("assistant", "a", "u", "answer")]
    audit, observation = _observe(rows)
    assert audit.client_id is None
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert observation.boundary.kind is BoundaryKind.UNPROVEN
    assert "unsupported_client_version" in dict(audit.issue_counts)
    assert messages(_parse(rows)) == ["request", "answer"]
    source = tmp_path / "source.jsonl"
    source.write_bytes(encode_jsonl(rows))
    inventory = inventory_from_native_metadata(
        root=tmp_path, source_format="claude", client_version="2.1.309"
    )
    assert len(inventory.nodes) == 1
    assert inventory.nodes[0].source_parent_status == "unknown"
    assert not inventory.relationships
    assert inventory.evidence[0].client_version == "2.1.309"
    assert inventory.evidence[0].component_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


def test_duplicate_wrappers_keep_physical_hash_count_provenance_and_cwd_conflict(tmp_path):
    first = desktop(slug="first", promptId="one")
    repeated = {**copy.deepcopy(first), "cwd": "/fixture/other", "slug": "second"}
    repeated["promptId"] = "two"
    repeated["toolUseResult"] = {"text": "WRAPPER_ONLY_SENTINEL"}
    rows = [first, repeated, desktop("assistant", "a", "u", "answer")]
    before = copy.deepcopy(rows)
    raw = encode_jsonl(rows)
    source_path = tmp_path / "source.jsonl"
    source_path.write_bytes(raw)
    session = claude.parse(source_path)
    assert messages(session) == ["request", "answer"]
    assert session.cwd is None
    assert session.cli_version == "2.1.309"
    assert session.raw_record_count == 3
    assert session.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert source_path.read_bytes() == raw
    assert rows == before
    opaque = [event for event in session.events if event.kind is EventKind.OPAQUE]
    assert len(opaque) == 1
    assert opaque[0].payload == {"reason": "duplicate_envelope_cwd_conflict"}
    assert opaque[0].provenance.record_index == 1
    assert opaque[0].provenance.source_id == first["uuid"]
    assert "WRAPPER_ONLY_SENTINEL" not in repr(session.events)
    records = list(iter_jsonl_bytes(raw))
    captured = copy.deepcopy(records)
    claude.parse_records(records, source_path=source_path, source_sha256=session.source_sha256)
    assert records == captured
    audit, observation = _observe(rows)
    issues = dict(audit.issue_counts)
    assert issues["duplicate_envelope_unproven"] == 1
    assert "invalid_graph" not in issues
    assert "parser_rejected_source" not in issues
    assert audit.client_id is None
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert observation.fidelity is not Coverage.VERIFIED
    assert {atom.record_index for atom in audit.atoms} == {0, 1, 2}


@pytest.mark.parametrize("last_prompt", [False, True])
def test_late_duplicates_preserve_first_unique_leaf_and_first_representative(last_prompt):
    first = desktop()
    rows = [first, desktop("assistant", "a", "u", "answer"), copy.deepcopy(first)]
    rows.append(copy.deepcopy(first))
    if last_prompt:
        rows.append({"type": "last-prompt", "leafUuid": "a", "sessionId": "fixture-session"})
    session = _parse(rows)
    assert messages(session) == ["request", "answer"]
    assert [event.provenance.record_index for event in session.events] == [0, 1, 2, 3]
    assert session.cwd == Path("/fixture/work")
    assert all(
        event.payload["reason"] == "duplicate_desktop_message_envelope"
        for event in session.events
        if event.kind is EventKind.OPAQUE
    )


def test_explicit_leaf_still_overrides_first_unique_order():
    first = desktop()
    rows = [
        first,
        desktop("assistant", "a", "u", "chosen"),
        desktop("assistant", "branch", "u", "inactive"),
        copy.deepcopy(first),
        {"type": "last-prompt", "leafUuid": "a"},
    ]
    assert messages(_parse(rows)) == ["request", "chosen"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("parentUuid", "another-parent"),
        ("sessionId", "another-session"),
        ("timestamp", "2026-11-05T21:00:53-03:00"),
        ("version", "2.1.318"),
        ("isSidechain", True),
        ("isMeta", True),
        ("isCompactSummary", True),
        ("logicalParentUuid", "another-parent"),
        ("attachment", {"type": "changed"}),
        ("unreviewed", {"value": "changed"}),
        ("message", {"role": "user", "content": "changed"}),
    ],
)
def test_duplicate_semantic_or_unknown_field_conflicts_stay_rejected(field, replacement, tmp_path):
    first = desktop()
    repeated = {**copy.deepcopy(first), field: replacement}
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, repeated])
    issues = dict(audit_rows([first, repeated], tmp_path).issue_counts)
    assert "invalid_graph" in issues
    assert "parser_rejected_source" in issues
    assert "duplicate_envelope_unproven" not in issues


@pytest.mark.parametrize("left,right", [(1, True), (0, False), (1, 1.0)])
@pytest.mark.parametrize("location", ["envelope", "message", "input"])
def test_duplicate_comparison_is_type_exact_at_every_depth(left, right, location):
    first = desktop()
    if location == "envelope":
        first["unreviewed"] = {"values": [left]}
    elif location == "message":
        first["message"]["usage"] = {"input_tokens": left}
    else:
        first["message"]["content"] = [
            {"type": "tool_use", "id": "call", "name": "Read", "input": {"values": [left]}}
        ]
    repeated = copy.deepcopy(first)
    if location == "envelope":
        repeated["unreviewed"]["values"][0] = right
    elif location == "message":
        repeated["message"]["usage"]["input_tokens"] = right
    else:
        repeated["message"]["content"][0]["input"]["values"][0] = right
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, repeated])


@pytest.mark.parametrize(
    "field", ["uuid", "parentUuid", "sessionId", "timestamp", "version", "type", "message"]
)
def test_duplicates_require_explicit_identity_and_message_fields(field):
    first = desktop()
    first.pop(field)
    # A missing UUID cannot identify a duplicate group in the first place.
    if field == "uuid":
        assert len(messages(_parse([first, copy.deepcopy(first)]))) == 2
        return
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, copy.deepcopy(first)])


@pytest.mark.parametrize(
    "message",
    [
        None,
        {},
        {"role": "assistant", "content": "wrong role"},
        {"role": "user"},
        {"role": "user", "content": 42},
        {"role": "user", "content": [None]},
    ],
)
def test_duplicate_message_shape_must_be_valid(message):
    first = desktop()
    first["message"] = message
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, copy.deepcopy(first)])


@pytest.mark.parametrize("version", ["2.1.209", "2.1.306", "2.1.308", "2.1.310", "2.1.318"])
def test_other_versions_do_not_inherit_desktop_duplicate_handling(version):
    first = desktop()
    first["version"] = version
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, copy.deepcopy(first)])


@pytest.mark.parametrize("field", ["isMeta", "isCompactSummary", "isSidechain", "unreviewed"])
def test_absent_and_explicit_false_or_null_are_distinct(field):
    first = desktop()
    first.pop(field, None)
    repeated = {**copy.deepcopy(first), field: None if field == "unreviewed" else False}
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, repeated])


def test_identical_unknown_metadata_can_be_deduplicated_but_has_no_authority():
    first = desktop(attachment={"type": "unreviewed", "value": 1})
    rows = [first, copy.deepcopy(first)]
    assert messages(_parse(rows)) == ["request"]
    audit, observation = _observe(rows)
    assert "unknown_field" in dict(audit.issue_counts)
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN


@pytest.mark.parametrize("kind", ["attachment", "system"])
def test_duplicate_record_type_changes_remain_rejected(kind):
    first = desktop()
    other = {**copy.deepcopy(first), "type": kind}
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, other])


def attachment():
    row = desktop(uid="attachment", parent="u")
    row.pop("message")
    row["type"] = "attachment"
    row["attachment"] = {"type": "file", "name": "fixture.txt", "content": "OPAQUE_SENTINEL"}
    return row


def test_identical_attachment_duplicates_are_each_opaque_and_never_replayed():
    attached = attachment()
    rows = [
        desktop(),
        attached,
        desktop("assistant", "a", "attachment", "answer"),
        copy.deepcopy(attached),
    ]
    source = _parse(rows)
    assert messages(source) == ["request", "answer"]
    opaque = [event for event in source.events if event.kind is EventKind.OPAQUE]
    assert [event.provenance.record_index for event in opaque] == [1, 3]
    assert [event.payload["reason"] for event in opaque] == [
        "active_graph_metadata_record",
        "duplicate_desktop_message_envelope",
    ]
    assert "OPAQUE_SENTINEL" not in repr(source.events)
    audit, observation = _observe(rows)
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "duplicate_envelope_unproven" in dict(audit.issue_counts)
    assert "invalid_graph" not in dict(audit.issue_counts)
    assert {atom.record_index for atom in audit.atoms} == {0, 1, 2, 3}
    assert source.raw_record_count == 4


@pytest.mark.parametrize("change", ["content", "missing", "message"])
def test_attachment_conflicts_and_malformed_duplicates_remain_rejected(change):
    attached = attachment()
    repeated = copy.deepcopy(attached)
    if change == "content":
        repeated["attachment"]["content"] = "changed"
    elif change == "missing":
        attached.pop("attachment")
        repeated.pop("attachment")
    else:
        attached["message"] = repeated["message"] = {"role": "user", "content": "ambiguous"}
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([desktop(), attached, repeated])


def test_compact_boundary_duplicate_still_rejected():
    boundary = {**desktop(uid="boundary"), "type": "system", "subtype": "compact_boundary"}
    rows = [boundary, copy.deepcopy(boundary), desktop("assistant", "a", "boundary", "answer")]
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse(rows)


@pytest.mark.parametrize("value", [None, "", "invalid-timestamp", 1, True])
def test_duplicate_timestamp_must_be_explicit_valid_text(value):
    first = desktop()
    first["timestamp"] = value
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse([first, copy.deepcopy(first)])


def test_repeated_sidechain_still_cannot_be_promoted_to_parent():
    first = desktop(isSidechain=True)
    with pytest.raises(SessionMigrateError, match="sidechain/subagent"):
        _parse([first, copy.deepcopy(first)])


def test_missing_cwd_on_repeated_envelope_is_a_cwd_conflict():
    first = desktop()
    repeated = copy.deepcopy(first)
    repeated.pop("cwd")
    source = _parse([first, repeated])
    assert source.cwd is None
    assert source.events[-1].payload["reason"] == "duplicate_envelope_cwd_conflict"


@pytest.mark.parametrize("cycle", [False, True])
def test_safe_duplicate_does_not_repair_missing_parent_or_cycle(cycle, tmp_path):
    first = desktop(parent="a" if cycle else "missing")
    rows = [first, copy.deepcopy(first), desktop("assistant", "a", "u", "answer")]
    with pytest.raises(SessionMigrateError, match="ancestry cycle" if cycle else "missing parent"):
        _parse(rows)
    audit = audit_rows(rows, tmp_path)
    assert audit.coverage is Coverage.UNKNOWN
    assert "invalid_graph" in dict(audit.issue_counts)
    assert "parser_rejected_source" in dict(audit.issue_counts)


def test_child_before_parent_tool_order_and_wrapper_only_result_loss():
    calls = [
        {"type": "tool_use", "id": "one", "name": "Read", "input": {}},
        {"type": "tool_use", "id": "two", "name": "Read", "input": {}},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": "two", "content": "second", "is_error": True},
        {"type": "tool_result", "tool_use_id": "one", "content": "first"},
    ]
    call = desktop("assistant", "a", "u", calls)
    repeated = {**copy.deepcopy(call), "toolUseResult": "WRAPPER_ONLY_SENTINEL"}
    rows = [
        desktop(),
        desktop("user", "r", "a", results),
        call,
        desktop("assistant", "z", "r", "finished"),
        repeated,
    ]
    source = _parse(rows)
    tools = [event for event in source.events if event.tool_call_id]
    assert [event.tool_call_id for event in tools] == ["one", "two", "two", "one"]
    assert [event.kind for event in tools] == (
        [EventKind.TOOL_CALL] * 2 + [EventKind.TOOL_RESULT] * 2
    )
    assert tools[2].payload["is_error"] is True
    assert "WRAPPER_ONLY_SENTINEL" not in repr(source.events)
    _emitted, dropped = claude.serialize(source, session_id="target", cwd=Path("/fixture/work"))
    assert dropped == {"opaque:duplicate_desktop_message_envelope": 1}
    audit, observation = _observe(rows)
    assert "invalid_graph" not in dict(audit.issue_counts)
    assert observation.fidelity is not Coverage.VERIFIED


def test_compaction_semantics_and_omission_accounting_survive_duplicate_envelope():
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "boundary",
        "parentUuid": None,
        "sessionId": "fixture-session",
        "version": "2.1.309",
        "compactMetadata": {"trigger": "manual", "preTokens": 1000},
    }
    summary = desktop("user", "summary", "boundary", "compacted history", isCompactSummary=True)
    rows = [
        boundary,
        summary,
        desktop("assistant", "a", "summary", "continued"),
        copy.deepcopy(summary),
    ]
    source = _parse(rows)
    assert [event.kind for event in source.events] == [
        EventKind.COMPACTION,
        EventKind.MESSAGE,
        EventKind.OPAQUE,
    ]
    assert source.events[0].text == "compacted history"
    assert source.events[0].payload["has_boundary_metadata"] is True
    audit, observation = _observe(rows)
    assert "compaction_unproven" in dict(audit.issue_counts)
    assert "invalid_graph" not in dict(audit.issue_counts)
    assert observation.fidelity is not Coverage.VERIFIED
    _emitted, dropped = claude.serialize(source, session_id="target", cwd=Path("/fixture/work"))
    assert dropped == {"compaction": 1, "opaque:duplicate_desktop_message_envelope": 1}


@pytest.mark.parametrize("version", ["2.1.308", "2.1.310", "2.2.0"])
def test_unknown_tree_version_stays_closed(tmp_path, version):
    row = desktop()
    row["version"] = version
    (tmp_path / "source.jsonl").write_bytes(encode_jsonl([row]))
    with pytest.raises(TreeSourceError, match="no approved metadata adapter"):
        inventory_from_native_metadata(
            root=tmp_path, source_format="claude", client_version=version
        )


def test_mixed_unapproved_tree_metadata_stays_closed(tmp_path):
    row = desktop("assistant", "a", "u", "answer")
    row["version"] = "2.1.310"
    (tmp_path / "source.jsonl").write_bytes(encode_jsonl([desktop(), row]))
    with pytest.raises(TreeSourceError, match="identity or client version is ambiguous"):
        inventory_from_native_metadata(
            root=tmp_path, source_format="claude", client_version="2.1.309"
        )
