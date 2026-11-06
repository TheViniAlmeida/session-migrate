"""Native selection is shared by parser/audit without inventing source content."""

from __future__ import annotations

import copy
import hashlib

import pytest
from test_claude_desktop309_compaction import topology
from test_claude_latest import _observe, _parse

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude_compaction import POLICY_ID
from session_migrate.jsonl import encode_jsonl
from session_migrate.lineage import Coverage
from session_migrate.model import EventKind, Role


def test_parser_and_audit_share_native_selection_and_indirect_summary():
    rows = topology()
    rows[2]["isMeta"] = True
    rows[2]["message"]["content"] = "  native metadata context\n"
    original = copy.deepcopy(rows)
    session = _parse(rows)
    audit, observation = _observe(rows)
    assert audit.schema_id == "claude-309-native-context-v1"
    assert observation.coverage is Coverage.UNKNOWN
    assert "invalid_graph" not in dict(audit.issue_counts)
    assert "compaction_unproven" in dict(audit.issue_counts)
    visible = [e for e in session.events if e.kind is not EventKind.OPAQUE]
    assert [e.text for e in visible] == [
        "source summary",
        "preserved first",
        "  native metadata context\n",
        "after compaction",
    ]
    assert [e.kind for e in visible] == [
        EventKind.CONTEXT,
        EventKind.MESSAGE,
        EventKind.CONTEXT,
        EventKind.MESSAGE,
    ]
    assert visible[0].payload["has_boundary_metadata"] is True
    assert visible[0].payload["native_context_policy"] == POLICY_ID
    assert visible[0].payload["block_type"] == "synthetic_text"
    assert visible[2].role is Role.USER
    assert visible[2].payload == {"block_type": "synthetic_text"}
    assert visible[2].provenance.record_index == 2
    assert session.raw_record_count == len(rows)
    assert session.source_sha256 == hashlib.sha256(encode_jsonl(rows)).hexdigest()
    assert rows == original


@pytest.mark.parametrize("flag", ["isMeta", "isCompactSummary"])
@pytest.mark.parametrize("value", ["true", 1, None, {}])
def test_native_context_flags_do_not_accept_truthy_or_malformed_values(flag, value):
    rows = topology()
    rows[2][flag] = value
    with pytest.raises(SessionMigrateError, match="malformed context flag"):
        _parse(rows)


def test_additional_selected_summary_cannot_be_silently_associated():
    rows = topology()
    rows[-1]["isCompactSummary"] = True
    with pytest.raises(SessionMigrateError, match="multiple selected compact summaries"):
        _parse(rows)


def test_compact_anchor_cannot_impersonate_assistant():
    rows = topology()
    rows[5]["type"] = rows[5]["message"]["role"] = "assistant"
    with pytest.raises(SessionMigrateError, match="invalid compact summary identity"):
        _parse(rows)


@pytest.mark.parametrize(
    "content",
    [
        None,
        [],
        [{"type": "image", "source": {}}],
        [{"type": "tool_use", "id": "unexpected", "name": "Read", "input": {}}],
    ],
)
def test_metadata_nontext_carriers_need_their_own_contract(content):
    rows = topology()
    rows[2]["isMeta"] = True
    rows[2]["message"]["content"] = content
    with pytest.raises(SessionMigrateError, match="unsupported non-text content"):
        _parse(rows)


@pytest.mark.parametrize("content", ["", "   ", [{"type": "text", "text": "\n"}]])
def test_empty_native_summary_does_not_authorize_context_retirement(content):
    rows = topology()
    rows[5]["message"]["content"] = content
    with pytest.raises(SessionMigrateError, match="empty compact summary"):
        _parse(rows)
