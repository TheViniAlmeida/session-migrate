"""Exact Claude readers retain closed authority gates across client upgrades."""

from __future__ import annotations

from pathlib import Path

import pytest

from session_migrate.canonical import CanonicalPolicy
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, EventKind
from session_migrate.native_audit import AuditPolicy, audit_snapshot, canonicalize_snapshot
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.witnesses import BoundaryKind


def _record(role, uid, parent, content, *, version="2.1.306", **fields):
    return {
        "type": role,
        "uuid": uid,
        "parentUuid": parent,
        "isSidechain": False,
        "sessionId": "fixture-session",
        "cwd": "/fixture/work",
        "version": version,
        "message": {"role": role, "content": content},
        **fields,
    }


def _parse(records):
    return claude.parse_bytes(encode_jsonl(records), Path("/fixture/transcript.jsonl"))


def _observe(records, *, client_version=None):
    snapshot = NativeSnapshot.from_bytes(
        encode_jsonl(records),
        context=SourceCaptureContext(AgentFormat.CLAUDE, Path("/fixture/transcript.jsonl")),
    )
    audit = audit_snapshot(snapshot, AuditPolicy(client_version=client_version))
    return audit, canonicalize_snapshot(snapshot, audit, CanonicalPolicy())


@pytest.mark.parametrize("version", sorted(claude.VALIDATED_CLAUDE_VERSIONS))
def test_exact_reader_acceptance_does_not_prove_turn_closure(version):
    rows = [
        _record("user", "u", None, "request\nUnicode: ação \u2028 preserved", version=version),
        _record("assistant", "a", "u", "answer", version=version),
    ]
    audit, observation = _observe(rows)

    assert audit.client_id == version
    assert audit.coverage is Coverage.VERIFIED
    assert observation.boundary.kind is BoundaryKind.UNPROVEN
    assert observation.identity is None
    assert [event.text for event in _parse(rows).events] == [
        "request\nUnicode: ação \u2028 preserved",
        "answer",
    ]


@pytest.mark.parametrize("version", ["2.1.302", "2.1.305", "2.1.307", "2.2.0"])
def test_unvalidated_client_does_not_inherit_adjacent_release_authority(version):
    audit, observation = _observe([_record("user", "u", None, "request", version=version)])

    assert audit.client_id is None
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "unsupported_client_version" in dict(audit.issue_counts)


@pytest.mark.parametrize("version", ["2.1.303", "2.1.306"])
def test_mixed_validated_clients_remain_readable_without_selecting_an_authority_version(version):
    rows = [
        _record("user", "u", None, "before upgrade", version="2.1.209"),
        _record("assistant", "a", "u", "after upgrade", version=version),
    ]
    audit, observation = _observe(rows)

    assert [event.text for event in _parse(rows).events] == ["before upgrade", "after upgrade"]
    assert audit.client_id is None
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "unsupported_client_version" in dict(audit.issue_counts)


def test_explicit_client_policy_must_match_the_capture():
    audit, _ = _observe([_record("user", "u", None, "request")], client_version="2.1.209")
    assert audit.coverage is Coverage.UNKNOWN
    assert "unsupported_client_version" in dict(audit.issue_counts)


@pytest.mark.parametrize("level", ["envelope", "message", "block"])
@pytest.mark.parametrize("version", sorted(claude.VALIDATED_CLAUDE_VERSIONS))
def test_latest_unknown_metadata_remains_closed_and_absent_from_replayed_text(level, version):
    row = _record("user", "u", None, [{"type": "text", "text": "request"}], version=version)
    carrier = {
        "envelope": row,
        "message": row["message"],
        "block": row["message"]["content"][0],
    }[level]
    carrier["unreviewed_metadata"] = {"text": "UNTRUSTED_METADATA_SENTINEL"}
    audit, observation = _observe([row])

    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "unknown_field" in dict(audit.issue_counts)
    assert [event.text for event in _parse([row]).events] == ["request"]


@pytest.mark.parametrize("version", sorted(claude.VALIDATED_CLAUDE_VERSIONS))
def test_latest_parallel_tool_ids_survive_child_before_parent_and_round_trip(version):
    calls = [
        {"type": "tool_use", "id": "call-one", "name": "Read", "input": {"file": "one"}},
        {"type": "tool_use", "id": "call-two", "name": "Read", "input": {"file": "two"}},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": "call-two", "content": "failure", "is_error": True},
        {
            "type": "tool_result",
            "tool_use_id": "call-one",
            "content": [{"type": "text", "text": "success\nsecond line"}],
        },
    ]
    rows = [
        _record("user", "u", None, "request", version=version),
        _record("user", "r", "a", results, version=version),
        _record("assistant", "a", "u", calls, version=version),
        _record("assistant", "z", "r", "finished", version=version),
    ]
    source = _parse(rows)
    audit, observation = _observe(rows)

    assert audit.coverage is Coverage.VERIFIED
    assert observation.boundary.kind is BoundaryKind.UNPROVEN
    tool_events = [event for event in source.events if event.tool_call_id]
    assert [event.tool_call_id for event in tool_events] == [
        "call-one",
        "call-two",
        "call-two",
        "call-one",
    ]
    assert tool_events[2].payload["is_error"] is True
    assert tool_events[3].text == "success\nsecond line"
    emitted, dropped = claude.serialize(
        source, session_id="target", cwd=Path("/fixture/work"), cli_version=version
    )
    assert not dropped
    reparsed = claude.parse_bytes(emitted, Path("/fixture/target.jsonl"))
    assert [(event.kind, event.tool_call_id, event.text) for event in reparsed.events] == [
        (event.kind, event.tool_call_id, event.text) for event in source.events
    ]


@pytest.mark.parametrize("version", sorted(claude.VALIDATED_CLAUDE_VERSIONS))
def test_latest_compaction_still_requires_independent_continuity_proof(version):
    rows = [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "sessionId": "fixture-session",
            "cwd": "/fixture/work",
            "version": version,
            "compactMetadata": {"trigger": "manual", "preTokens": 1000},
        },
        _record("user", "summary", "boundary", "summary", isCompactSummary=True, version=version),
        _record("assistant", "a", "summary", "continued", version=version),
    ]
    audit, observation = _observe(rows)

    assert "compaction_unproven" in dict(audit.issue_counts)
    assert observation.fidelity is not Coverage.VERIFIED
    assert observation.boundary.kind is BoundaryKind.UNPROVEN
    assert [event.kind for event in _parse(rows).events] == [
        EventKind.COMPACTION,
        EventKind.MESSAGE,
    ]


@pytest.mark.parametrize("version", sorted(claude.VALIDATED_CLAUDE_VERSIONS))
def test_latest_sidechain_cannot_be_promoted_to_a_parent_session(version):
    rows = [_record("user", "u", None, "fork prompt", isSidechain=True, version=version)]
    with pytest.raises(SessionMigrateError, match="sidechain/subagent"):
        _parse(rows)
    snapshot = NativeSnapshot.from_bytes(
        encode_jsonl(rows),
        context=SourceCaptureContext(AgentFormat.CLAUDE, Path("/fixture/transcript.jsonl")),
    )
    audit = audit_snapshot(snapshot)
    assert audit.coverage is Coverage.UNKNOWN
    assert "parser_rejected_source" in dict(audit.issue_counts)


@pytest.mark.parametrize("selected", ["side", "crossed"])
@pytest.mark.parametrize("version", sorted(claude.VALIDATED_CLAUDE_VERSIONS))
def test_mainline_leaf_cannot_select_or_cross_a_sidechain(selected, version):
    rows = [
        _record("user", "u", None, "parent request", version=version),
        _record("assistant", "a", "u", "parent answer", version=version),
        _record("assistant", "side", "u", "child answer", isSidechain=True, version=version),
        _record("assistant", "crossed", "side", "invalid parent continuation", version=version),
        {"type": "last-prompt", "leafUuid": selected},
    ]
    with pytest.raises(SessionMigrateError, match="sidechain/subagent"):
        _parse(rows)
    snapshot = NativeSnapshot.from_bytes(
        encode_jsonl(rows),
        context=SourceCaptureContext(AgentFormat.CLAUDE, Path("/fixture/transcript.jsonl")),
    )
    audit = audit_snapshot(snapshot)
    assert audit.coverage is Coverage.UNKNOWN
    assert {"invalid_graph", "parser_rejected_source"} <= dict(audit.issue_counts).keys()


def test_latest_emission_keeps_the_existing_writer_default():
    source = _parse([_record("user", "u", None, "request")])
    emitted, _ = claude.serialize(source, session_id="target", cwd=Path("/fixture/work"))

    assert claude.PINNED_CLAUDE_VERSION == "2.1.209"
    assert frozenset({"2.1.209", "2.1.303", "2.1.306"}) == claude.VALIDATED_CLAUDE_VERSIONS
    assert {record.value.get("version") for record in iter_jsonl_bytes(emitted)} == {"2.1.209"}


@pytest.mark.parametrize("record_type", ["api-request-shape", "api-request-blob", "api-request"])
@pytest.mark.parametrize("version", ["2.1.303", "2.1.306"])
def test_stock_request_metadata_is_unknown_and_cannot_inject_conversation(record_type, version):
    # Envelope shapes observed in the stock 2.1.303/306 cold-resume loopback captures.
    # Request payloads remain unprojected, even when they contain a user message.
    message = {"role": "user", "content": "UNPROJECTED_REQUEST_SENTINEL"}
    metadata = {
        "api-request-shape": {
            "shapeHash": "shape",
            "timestamp": "2026-10-21T12:00:00Z",
            "version": version,
            "shape": {"system": [message], "tools": []},
        },
        "api-request-blob": {"hash": "blob", "message": message},
        "api-request": {
            "id": "request",
            "timestamp": "2026-10-21T12:00:00Z",
            "version": version,
            "querySource": "sdk",
            "shapeHash": "shape",
            "params": {"system": [message]},
            "keep": 0,
            "tail": [message],
            "cacheControl": [],
            "requestDigest": "request-digest",
        },
    }[record_type]
    rows = [
        _record("user", "u", None, "request", version=version),
        {"type": record_type, "sessionId": "fixture-session", **metadata},
        _record("assistant", "a", "u", "answer", version=version),
    ]
    audit, observation = _observe(rows)

    assert audit.client_id == version
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "unknown_atom" in dict(audit.issue_counts)
    assert [event.text for event in _parse(rows).events if event.kind is EventKind.MESSAGE] == [
        "request",
        "answer",
    ]
    assert all(
        "UNPROJECTED_REQUEST_SENTINEL" not in (event.text or "") for event in observation.events
    )


@pytest.mark.parametrize("version", ["2.1.303", "2.1.306"])
def test_stock_assistant_request_references_do_not_gain_fidelity_from_version_acceptance(version):
    row = _record("assistant", "a", None, [{"type": "text", "text": "answer"}], version=version)
    row.update(
        {
            "apiBlockIndex": 0,
            "perTurnEffort": None,
            "requestRef": "request",
            "requestedModel": "fixture-model",
        }
    )
    audit, observation = _observe([row])
    assert audit.client_id == version
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "unknown_field" in dict(audit.issue_counts)
    assert observation.boundary.kind is BoundaryKind.UNPROVEN
