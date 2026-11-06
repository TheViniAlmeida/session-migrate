"""Nested memory remains quoted data, independently bound to its raw capture."""

import copy
import json

import pytest
from test_claude_reviewed_reader import attachment
from test_incomplete_tool_projection import changed_artifact
from test_projections import observed
from test_reviewed_portability_projection import parts, prepare, rows

from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage
from session_migrate.model import EventKind, Role
from session_migrate.projections import verify_projection


def nested_rows():
    records = rows()
    carrier = attachment(
        "nested-memory",
        "final",
        "nested_memory",
        rendered=[{"content": "  RENDERED_MEMORY\n"}],
        recorded_role="system",
        path="/NEVER_READ/MEMORY.md",
        displayPath="MEMORY.md",
        content={
            "path": "/NEVER_READ/MEMORY.md",
            "type": "Project",
            "content": " CACHED_MEMORY ação\n",
            "rawContent": " RAW_MEMORY\u2028 ",
            "contentDiffersFromDisk": True,
        },
    )
    carrier["renderedInHumanTurn"] = [{"content": "OTHER_RENDERED_MEMORY"}]
    records.append(carrier)
    return records


def test_nested_memory_projection_preserves_each_variant_without_runtime_authority(tmp_path):
    records = nested_rows()
    original = copy.deepcopy(records)
    values = parts(tmp_path, records)
    prepared = prepare(values)
    verified = verify_projection(prepared, observed(values[1]), context=values[-1])
    assert verified.semantic_matches and verified.structure_matches
    assert prepared.certificate is None
    assert prepared.source_coverage is Coverage.UNKNOWN
    assert not verified.preserves_content and not verified.delta_eligible
    quoted = [
        event
        for event in prepared.expected.events
        if event.kind is EventKind.CONTEXT and '"source_class":"nested_memory"' in event.text
    ]
    assert len(quoted) == 3
    assert all(event.role is Role.USER for event in quoted)
    data = {json.loads(event.text)["variant"]: json.loads(event.text)["data"] for event in quoted}
    assert data == {
        "rendered": records[-1]["rendered"][0]["content"],
        "renderedInHumanTurn": records[-1]["renderedInHumanTurn"][0]["content"],
        "captured_payload": records[-1]["attachment"],
    }
    assert records == original
    native = json.loads(values[1].native_bytes)
    assert not any(message["type"] == "system" for message in native["messages"])
    assert "permissions" not in native["info"]


@pytest.mark.parametrize(
    "tamper",
    ["rendered", "cached", "raw", "path", "drop_payload", "authority"],
)
def test_nested_memory_prepared_and_cold_tampering_are_rejected(tmp_path, tamper):
    values = parts(tmp_path, nested_rows())
    prepared = prepare(values)

    def change(bundle):
        messages = bundle["messages"]
        candidates = [
            message
            for message in messages
            if '"source_class":"nested_memory"' in message.get("text", "")
        ]
        assert len(candidates) == 3
        payload = next(message for message in candidates if "captured_payload" in message["text"])
        if tamper == "drop_payload":
            messages.remove(payload)
        elif tamper == "authority":
            payload["type"] = "system"
        else:
            marker = {
                "rendered": "  RENDERED_MEMORY",
                "cached": "CACHED_MEMORY",
                "raw": "RAW_MEMORY",
                "path": "/NEVER_READ/MEMORY.md",
            }[tamper]
            target = next(message for message in candidates if marker in message["text"])
            target["text"] = target["text"].replace(marker, "FORGED", 1)

    changed = changed_artifact(values[1], change)
    with pytest.raises(SessionMigrateError):
        prepare((values[0], changed, *values[2:]))
    try:
        verified = verify_projection(prepared, observed(changed), context=values[-1])
    except SessionMigrateError:
        pass
    else:
        assert not verified.semantic_matches
