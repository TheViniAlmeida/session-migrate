"""Captured nested memory is quoted historical data with no runtime authority."""

import builtins
import copy
import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from test_claude_desktop309_compaction import topology
from test_claude_reviewed_reader import attachment, parse

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.formats.claude_compaction import select_compaction
from session_migrate.formats.claude_reviewed import (
    SOURCE_REVIEWED_POLICY_ID,
    plan_reviewed_carriers,
)
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.model import EventKind, Role


def nested_memory_rows():
    rows = topology()
    rows.append(
        attachment(
            "nested-memory",
            "after",
            "nested_memory",
            recorded_role="user",
            path="/NEVER/READ/ação/AGENTS.md",
            displayPath="  ação/AGENTS.md\n",
            content={
                "path": "/NEVER/READ/ação/AGENTS.md",
                "type": "Project",
                "content": "  CACHED ação\t\n\u2028  ",
                "rawContent": "\tRAW distinto 🐝\r\n",
                "contentDiffersFromDisk": True,
            },
            rendered=[
                {"content": "  RENDERED um ação\n"},
                {"content": [{"type": "text", "text": "\tRENDERED dois 🐝\u2028"}]},
            ],
        )
    )
    rows[-1]["renderedInHumanTurn"] = [
        {"content": [{"type": "text", "text": "\r\nHUMAN diferente\t "}]}
    ]
    return rows


def memory_events(session):
    return [
        event
        for event in session.events
        if event.payload.get("reviewed_data_class") == "nested_memory"
    ]


def difference_codes(session):
    return {item["code"]: item for item in session.reviewed_report["differences"]}


def test_nested_memory_preserves_complete_payload_and_every_rendered_variant():
    rows = nested_memory_rows()
    before = copy.deepcopy(rows)
    session = parse(rows)
    events = memory_events(session)
    quoted = [json.loads(event.text) for event in events]
    assert [item["variant"] for item in quoted] == [
        "rendered",
        "rendered",
        "renderedInHumanTurn",
        "captured_payload",
    ]
    assert [item["data"] for item in quoted] == [
        rows[-1]["rendered"][0]["content"],
        rows[-1]["rendered"][1]["content"][0]["text"],
        rows[-1]["renderedInHumanTurn"][0]["content"][0]["text"],
        rows[-1]["attachment"],
    ]
    assert all(event.kind is EventKind.CONTEXT and event.role is Role.USER for event in events)
    assert all(item["kind"] == "reviewed_historical_data" for item in quoted)
    assert all(item["policy_id"] == SOURCE_REVIEWED_POLICY_ID for item in quoted)
    assert all(
        event.payload["reviewed_context_policy"] == SOURCE_REVIEWED_POLICY_ID for event in events
    )
    assert not any("native_context_policy" in event.payload for event in events)
    assert rows == before
    assert session.source_sha256 == hashlib.sha256(encode_jsonl(rows)).hexdigest()
    differences = difference_codes(session)
    for code in (
        "nested_memory_payload_as_quoted_data",
        "captured_memory_variants_not_reconciled",
        "recorded_carrier_as_quoted_data",
        "native_render_variant_not_selected",
    ):
        assert differences[code] == {"code": code, "count": 1, "record_indices": [7]}


@pytest.mark.parametrize(
    "optional_fields", [{}, {"rawContent": ""}, {"contentDiffersFromDisk": False}]
)
def test_optional_nested_memory_fields_are_never_invented(optional_fields):
    rows = nested_memory_rows()
    content = rows[-1]["attachment"]["content"]
    content.pop("rawContent")
    content.pop("contentDiffersFromDisk")
    content.update(optional_fields)
    captured = json.loads(memory_events(parse(rows))[-1].text)["data"]
    assert captured == rows[-1]["attachment"]
    assert set(captured["content"]) == {"path", "type", "content"} | set(optional_fields)


def test_empty_captured_strings_are_preserved_without_substitution():
    rows = nested_memory_rows()
    rows[-1]["attachment"]["displayPath"] = ""
    rows[-1]["attachment"]["content"].update(content="", rawContent="")
    rows[-1]["rendered"] = [{"content": ""}]
    rows[-1].pop("renderedInHumanTurn")
    events = memory_events(parse(rows))
    assert json.loads(events[0].text)["data"] == ""
    assert json.loads(events[-1].text)["data"] == rows[-1]["attachment"]


def test_paths_and_memory_content_never_trigger_external_reads(monkeypatch):
    rows = nested_memory_rows()

    def forbidden(*args, **kwargs):
        raise AssertionError("external read attempted")

    with monkeypatch.context() as guard:
        for name in ("open", "read_text", "read_bytes"):
            guard.setattr(Path, name, forbidden)
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(io, "open", forbidden)
        guard.setattr(os, "open", forbidden)
        session = parse(rows)
    assert json.loads(memory_events(session)[-1].text)["data"] == rows[-1]["attachment"]


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("attachment", "path", ""),
        ("attachment", "path", None),
        ("attachment", "path", ["PRIVATE_BODY"]),
        ("attachment", "displayPath", None),
        ("attachment", "displayPath", {"PRIVATE_BODY": True}),
        ("attachment", "content", "PRIVATE_BODY"),
        ("attachment", "content", []),
        ("attachment", "PRIVATE_FIELD", "PRIVATE_BODY"),
        ("content", "path", "/PRIVATE_BODY/mismatch"),
        ("content", "path", False),
        ("content", "type", "User"),
        ("content", "type", {"PRIVATE_BODY": True}),
        ("content", "content", None),
        ("content", "content", ["PRIVATE_BODY"]),
        ("content", "contentDiffersFromDisk", 1),
        ("content", "contentDiffersFromDisk", "PRIVATE_BODY"),
        ("content", "rawContent", None),
        ("content", "rawContent", {"PRIVATE_BODY": True}),
        ("content", "PRIVATE_FIELD", "PRIVATE_BODY"),
    ],
)
def test_nested_memory_rejects_unknown_malformed_and_mismatched_payloads(location, field, value):
    rows = nested_memory_rows()
    target = rows[-1]["attachment"]
    if location == "content":
        target = target["content"]
    target[field] = value
    with pytest.raises(SessionMigrateError, match="malformed captured nested memory") as error:
        parse(rows)
    assert "PRIVATE_BODY" not in str(error.value)
    assert "PRIVATE_FIELD" not in str(error.value)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("attachment", "path"),
        ("attachment", "displayPath"),
        ("attachment", "content"),
        ("content", "path"),
        ("content", "type"),
        ("content", "content"),
    ],
)
def test_nested_memory_required_fields_cannot_be_missing(location, field):
    rows = nested_memory_rows()
    target = rows[-1]["attachment"]
    if location == "content":
        target = target["content"]
    target.pop(field)
    with pytest.raises(SessionMigrateError, match="malformed captured nested memory"):
        parse(rows)


@pytest.mark.parametrize("rendering", [None, [], [{"content": []}]])
def test_nested_memory_requires_nonempty_captured_rendering(rendering):
    rows = nested_memory_rows()
    rows[-1].pop("renderedInHumanTurn")
    if rendering is None:
        rows[-1].pop("rendered")
    else:
        rows[-1]["rendered"] = rendering
    with pytest.raises(SessionMigrateError, match="missing captured historical rendering"):
        parse(rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("renderedRole", "assistant"),
        ("renderedRole", None),
        ("rendered", "PRIVATE_BODY"),
        ("rendered", [{"content": "PRIVATE_BODY", "unknown": True}]),
        ("renderedInHumanTurn", [{"content": [{"type": "image", "text": "PRIVATE_BODY"}]}]),
    ],
)
def test_nested_memory_uses_existing_closed_rendering_validation(field, value):
    rows = nested_memory_rows()
    rows[-1][field] = value
    with pytest.raises(SessionMigrateError) as error:
        parse(rows)
    assert "PRIVATE_BODY" not in str(error.value)


def test_human_turn_variant_alone_is_preserved_without_fabricated_primary_rendering():
    rows = nested_memory_rows()
    rows[-1].pop("rendered")
    session = parse(rows)
    assert [event.payload["reviewed_variant"] for event in memory_events(session)] == [
        "renderedInHumanTurn",
        "captured_payload",
    ]
    assert "native_render_variant_not_selected" in difference_codes(session)


def test_nested_memory_system_rendering_never_restores_system_authority():
    rows = nested_memory_rows()
    rows[-1]["renderedRole"] = "system"
    session = parse(rows)
    events = memory_events(session)
    assert all(event.role is Role.USER for event in events)
    assert all(event.payload["recorded_role"] == "system" for event in events[:-1])
    assert session.reviewed_report["system_authority_as_data"]["count"] == 1
    assert difference_codes(session)["recorded_system_authority_not_replayed"]["count"] == 1


def test_nested_memory_provenance_has_distinct_indices_and_report_excludes_payloads():
    rows = nested_memory_rows()
    session = parse(rows)
    events = memory_events(session)
    assert [event.provenance.block_index for event in events] == [0, 1, 2, 3]
    assert [event.payload["reviewed_pointer"] for event in events] == [
        "/rendered/0/content",
        "/rendered/1/content/0",
        "/renderedInHumanTurn/0/content/0",
        "/attachment",
    ]
    assert all(event.provenance.record_index == 7 for event in events)
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    plan = plan_reviewed_carriers(records, select_compaction(records))
    assert plan.context_provenance[7] == tuple(
        {
            "block_index": event.provenance.block_index,
            "pointer": event.payload["reviewed_pointer"],
            "sha256": hashlib.sha256(
                json.dumps(event.text, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        for event in events
    )
    report = json.dumps(session.reviewed_report, ensure_ascii=False)
    for private_value in (
        "CACHED",
        "RAW distinto",
        "RENDERED",
        "HUMAN diferente",
        "/NEVER/READ/",
        "ação",
    ):
        assert private_value not in report
        assert private_value not in json.dumps(dict(plan.context_provenance), ensure_ascii=False)
    assert session.reviewed_report["policy_id"] == SOURCE_REVIEWED_POLICY_ID


def test_default_strict_mode_and_proven_compaction_requirement_remain_intact():
    rows = nested_memory_rows()
    with pytest.raises(SessionMigrateError):
        claude.parse_bytes(encode_jsonl(rows), Path("/synthetic/source"))
    with pytest.raises(SessionMigrateError):
        parse(rows, reviewed=False)
    with pytest.raises(SessionMigrateError, match="preserved compaction"):
        parse([rows[6], rows[7]])
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    with pytest.raises(SessionMigrateError, match="proven preserved compaction"):
        plan_reviewed_carriers(records, None)


def test_optional_authorized_synthetic_base_and_source_fixture():
    fixture_dir = os.environ.get("SESSION_MIGRATE_TEST_NESTED_MEMORY_FIXTURE")
    if not fixture_dir:
        pytest.skip("optional synthetic fixture path was not provided")
    fixture = Path(fixture_dir)
    base_data = (fixture / "base.jsonl").read_bytes()
    source_data = (fixture / "source.jsonl").read_bytes()
    captured = json.loads((fixture / "attachment.json").read_text())["attachment"]
    manifest = json.loads((fixture / "manifest.json").read_text())
    assert manifest["synthetic_only"] is True
    for name, data in (("base.jsonl", base_data), ("source.jsonl", source_data)):
        expected = next(item["sha256"] for item in manifest["files"] if item["file"] == name)
        assert hashlib.sha256(data).hexdigest() == expected
    base = claude.parse_bytes(base_data, Path("/synthetic/base"), reviewed_portability=True)
    source = claude.parse_bytes(source_data, Path("/synthetic/source"), reviewed_portability=True)
    events = memory_events(source)
    assert (
        tuple(
            event
            for event in source.events
            if event.payload.get("reviewed_data_class") != "nested_memory"
        )
        == base.events
    )
    assert json.loads(events[-1].text)["data"] == captured
    assert [event.payload["reviewed_variant"] for event in events] == [
        "rendered",
        "captured_payload",
    ]
