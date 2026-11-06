"""A missing logical parent is not permission to invent a compaction splice."""

from __future__ import annotations

import copy

import pytest
from test_claude_desktop309 import audit_rows, desktop
from test_claude_latest import _parse

from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage


def topology():
    rows = [
        desktop("user", "before", None, "before compaction"),
        desktop("assistant", "head", "before", "preserved first"),
        desktop("user", "tail", "head", "preserved last"),
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "logicalParentUuid": "unpersisted",
            "sessionId": "fixture-session",
            "cwd": "/fixture/work",
            "version": "2.1.309",
            "timestamp": "2026-11-05T20:00:53-03:00",
            "compactMetadata": {
                "preservedSegment": {
                    "headUuid": "head",
                    "tailUuid": "tail",
                    "anchorUuid": "summary",
                },
                "preservedMessages": {
                    "anchorUuid": "summary",
                    "uuids": ["head", "tail"],
                    "allUuids": ["head", "tail", "unpersisted"],
                },
                "runningTurnSends": {"openerUuid": "before", "uuids": ["before"]},
            },
        },
        {
            "type": "attachment",
            "uuid": "interposed",
            "parentUuid": "boundary",
            "sessionId": "fixture-session",
            "version": "2.1.309",
            "attachment": {
                "type": "hook_system_message",
                "content": "UI note",
                "hookName": "Fixture",
                "hookEvent": "SessionStart",
            },
        },
        desktop("user", "summary", "interposed", "source summary", isCompactSummary=True),
        desktop("assistant", "after", "summary", "after compaction"),
    ]
    return rows


@pytest.mark.parametrize("checkpoint_present", [False, True])
def test_all_uuids_membership_cannot_supply_a_missing_persisted_node(checkpoint_present, tmp_path):
    rows = topology()
    rows[3]["compactMetadata"]["preservedMessages"]["uuids"] = ["head", "unpersisted"]
    if checkpoint_present:
        rows.append({"type": "last-prompt", "leafUuid": "after"})
    original = copy.deepcopy(rows)
    with pytest.raises(SessionMigrateError, match="missing preservation UUID"):
        _parse(rows)
    audit = audit_rows(rows, tmp_path)
    assert audit.coverage is Coverage.UNKNOWN
    assert {"invalid_graph", "parser_rejected_source"} <= dict(audit.issue_counts).keys()
    assert rows == original
