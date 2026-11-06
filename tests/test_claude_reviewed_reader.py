"""Explicit reviewed reading preserves captured conversation as historical data."""

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
from test_claude_desktop309_compaction import topology

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.formats.claude_compaction import select_compaction
from session_migrate.formats.claude_reviewed import (
    SOURCE_REVIEWED_POLICY_ID,
    plan_reviewed_carriers,
)
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.model import AgentFormat, EventKind, Role
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext


def attachment(uid, parent, kind, *, rendered=None, recorded_role="system", **fields):
    row = {
        "type": "attachment",
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": "fixture-session",
        "version": "2.1.309",
        "isSidechain": False,
        "attachment": {"type": kind, **fields},
    }
    if rendered is not None:
        row.update(rendered=rendered, renderedRole=recorded_role)
    return row


def reviewed_rows():
    rows = topology()
    rows[1]["message"]["content"] = [
        {
            "type": "thinking",
            "thinking": "  CAPTURED_THINKING\n",
            "signature": "CAPTURED_SIGNATURE",
        },
        {"type": "text", "text": "  ASSISTANT_LITERAL\n"},
        {
            "type": "tool_use",
            "id": "DISPLAY_CALL",
            "name": "displayed_tool",
            "input": {"shown": "DISPLAY_INPUT"},
        },
    ]
    rows[1]["wireToolInputs"] = {"DISPLAY_CALL": {"shown": "DIFFERENT_WIRE_INPUT"}}
    rows[2]["message"]["content"] = [
        {"type": "text", "text": "USER_LITERAL\n  "},
        {
            "type": "tool_result",
            "tool_use_id": "DISPLAY_CALL",
            "is_error": True,
            "content": [{"type": "text", "text": "  DISPLAY_RESULT\n"}],
        },
    ]
    rows.extend(
        [
            attachment(
                "rendered",
                "after",
                "environment",
                rendered=[{"content": "SYSTEM_TEXT_AS_DATA"}],
                snapshot={"captured": True},
            ),
            attachment(
                "cached",
                "rendered",
                "file",
                rendered=[{"content": "CACHED_RENDERED"}],
                filename="/NEVER/READ/THIS",
                content={"anonymous_key": "CACHED_BODY"},
            ),
            attachment(
                "reference",
                "cached",
                "compact_file_reference",
                rendered=[{"content": "REFERENCE_RENDERED"}],
                filename="/NEVER/READ/REFERENCE",
                displayPath="REFERENCE_PATH",
            ),
            attachment(
                "queue",
                "reference",
                "queued_command",
                rendered=[{"content": "QUEUE_VARIANT_ONE"}],
                command="NEVER_EXECUTE",
            ),
            attachment(
                "snapshot", "queue", "prompt_snapshot", systemPrompt=["RUNTIME_BASELINE_ONLY"]
            ),
            attachment(
                "deferred",
                "snapshot",
                "deferred_tools_record",
                entries=[{"name": "NEVER_REGISTER"}],
                toolInputCopies={},
            ),
        ]
    )
    rows[-3]["renderedInHumanTurn"] = [{"content": [{"type": "text", "text": "QUEUE_VARIANT_TWO"}]}]
    return rows


def parse(rows, *, reviewed=True):
    return claude.parse_bytes(
        encode_jsonl(rows), Path("/synthetic/source"), reviewed_portability=reviewed
    )


def test_reviewed_preserves_displayed_conversation_and_provenance_without_mutation():
    rows = reviewed_rows()
    before = copy.deepcopy(rows)
    session = parse(rows)
    assert session.source_sha256 == hashlib.sha256(encode_jsonl(rows)).hexdigest()
    assert session.raw_record_count == len(rows)
    assert session.reviewed_report["policy_id"] == SOURCE_REVIEWED_POLICY_ID
    texts = [
        (event.role, event.text) for event in session.events if event.kind is EventKind.MESSAGE
    ]
    assert texts == [
        (Role.ASSISTANT, "  ASSISTANT_LITERAL\n"),
        (Role.USER, "USER_LITERAL\n  "),
        (Role.ASSISTANT, "after compaction"),
    ]
    call = next(event for event in session.events if event.kind is EventKind.TOOL_CALL)
    result = next(event for event in session.events if event.kind is EventKind.TOOL_RESULT)
    assert (call.tool_name, call.tool_call_id, call.payload) == (
        "displayed_tool",
        "DISPLAY_CALL",
        {"input": {"shown": "DISPLAY_INPUT"}},
    )
    assert (result.tool_call_id, result.text, result.payload) == (
        "DISPLAY_CALL",
        "  DISPLAY_RESULT\n",
        {"is_error": True, "content_blocks": [{"type": "text", "text": "  DISPLAY_RESULT\n"}]},
    )
    assert (
        call.provenance.record_index,
        call.provenance.block_index,
        call.provenance.source_id,
    ) == (1, 2, "head")
    assert rows == before


def test_rendering_variants_cached_payload_and_thinking_are_quoted_user_data():
    session = parse(reviewed_rows())
    contexts = [event for event in session.events if event.kind is EventKind.CONTEXT]
    assert all(event.role is Role.USER for event in contexts)
    assert all(
        event.payload["reviewed_context_policy"] == SOURCE_REVIEWED_POLICY_ID for event in contexts
    )
    assert all("native_context_policy" not in event.payload for event in contexts)
    quoted = [
        json.loads(event.text)
        for event in contexts
        if event.payload.get("reviewed_data_class") != "summary"
    ]
    assert any(
        item["data"]
        == {
            "type": "thinking",
            "thinking": "  CAPTURED_THINKING\n",
            "signature": "CAPTURED_SIGNATURE",
        }
        for item in quoted
    )
    assert any(item["data"] == {"anonymous_key": "CACHED_BODY"} for item in quoted)
    assert any(item["data"] == "QUEUE_VARIANT_ONE" for item in quoted)
    assert any(item["data"] == "QUEUE_VARIANT_TWO" for item in quoted)
    assert not any(
        event.kind in {EventKind.THINKING, EventKind.COMPACTION} for event in session.events
    )
    assert session.reviewed_report["thinking_as_data"]["count"] == 1
    assert session.reviewed_report["unresolved_file_references"]["count"] == 1
    assert session.reviewed_report["displayed_vs_wire_inputs"]["difference_count"] == 1


def test_runtime_controls_are_archive_only_and_report_has_no_contents_or_identifiers():
    rows = reviewed_rows()
    session = parse(rows)
    report = json.dumps(session.reviewed_report, sort_keys=True)
    for sentinel in (
        "RUNTIME_BASELINE_ONLY",
        "NEVER_REGISTER",
        "CAPTURED_THINKING",
        "SYSTEM_TEXT_AS_DATA",
        "DISPLAY_CALL",
        "DISPLAY_INPUT",
        "DIFFERENT_WIRE_INPUT",
        "/NEVER/",
        "fixture-session",
    ):
        assert sentinel not in report
    assert "RUNTIME_BASELINE_ONLY" not in json.dumps([event.text for event in session.events])
    assert "NEVER_REGISTER" not in json.dumps([event.text for event in session.events])
    classes = {row["record_class"] for row in session.reviewed_report["archived_runtime_records"]}
    assert {"prompt_snapshot", "deferred_tools_record"} <= classes
    assert session.reviewed_report == parse(rows).reviewed_report


def test_reviewed_never_reads_captured_file_paths(tmp_path, monkeypatch):
    rows = reviewed_rows()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("captured path read")

    monkeypatch.setattr(Path, "open", forbidden)
    parse(rows)
    assert not calls


@pytest.mark.parametrize("input_value", [{"x": [True, None]}, [1, "x"], " input\n", 3, False, None])
def test_displayed_input_keeps_json_type_and_value(input_value):
    rows = reviewed_rows()
    rows[1]["message"]["content"][2]["input"] = copy.deepcopy(input_value)
    rows[1]["wireToolInputs"]["DISPLAY_CALL"] = copy.deepcopy(input_value)
    session = parse(rows)
    call = next(event for event in session.events if event.kind is EventKind.TOOL_CALL)
    assert type(call.payload["input"]) is type(input_value)
    assert call.payload["input"] == input_value
    assert session.reviewed_report["displayed_vs_wire_inputs"]["difference_count"] == 0


def test_displayed_input_numeric_overflow_is_not_silently_normalized():
    rows = reviewed_rows()
    rows[1].pop("wireToolInputs")
    rows[1]["message"]["content"][2]["input"] = {"value": 123.5}
    data = encode_jsonl(rows).replace(b"123.5", b"1e999")
    with pytest.raises(SessionMigrateError, match="historical JSON"):
        claude.parse_bytes(data, Path("/synthetic/source"), reviewed_portability=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[1]["message"]["content"].append(
            {"type": "unknown_body", "body": "PRIVATE_BODY"}
        ),
        lambda rows: rows[2]["message"]["content"][1].update(
            content=[{"type": "image", "source": {}}]
        ),
        lambda rows: rows[1]["message"]["content"][2].pop("input"),
        lambda rows: rows[2]["message"]["content"][1].update(tool_use_id="ORPHAN"),
        lambda rows: rows[1]["message"]["content"].append(
            copy.deepcopy(rows[1]["message"]["content"][2])
        ),
        lambda rows: rows[7].update(renderedRole={"PRIVATE_BODY": []}),
        lambda rows: rows[7].update(rendered=[{"content": "PRIVATE_BODY", "unknown": 1}]),
        lambda rows: rows[7]["attachment"].update(type="unknown_history"),
        lambda rows: rows[7].update(parentUuid="missing"),
        lambda rows: rows[1].update(sessionId="different"),
    ],
)
def test_ambiguous_unknown_or_invalid_content_remains_blocked(mutate):
    rows = reviewed_rows()
    mutate(rows)
    with pytest.raises(SessionMigrateError) as error:
        parse(rows)
    assert "PRIVATE_BODY" not in str(error.value)


def test_tool_reference_remains_exact_data_in_source_ir():
    rows = reviewed_rows()
    content = [{"type": "tool_reference", "tool_name": "CAPTURED_NAME_ONLY"}]
    rows[2]["message"]["content"][1]["content"] = copy.deepcopy(content)
    session = parse(rows)
    result = next(event for event in session.events if event.kind is EventKind.TOOL_RESULT)
    assert result.payload["content_blocks"] == content
    assert session.reviewed_report["tool_references"]["count"] == 1
    assert "CAPTURED_NAME_ONLY" not in json.dumps(session.reviewed_report)


def test_open_call_is_reported_without_fabricated_result():
    rows = reviewed_rows()
    rows[2]["message"]["content"].pop()
    session = parse(rows)
    assert session.reviewed_report["open_tool_calls"]["count"] == 1
    assert not any(event.kind is EventKind.TOOL_RESULT for event in session.events)


def test_plan_uses_original_leaf_and_borrowed_envelopes_without_hoist():
    rows = reviewed_rows()
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    selection = select_compaction(records)
    plan = plan_reviewed_carriers(records, selection)
    assert plan.replay_indices == (*selection.selected_indices, *range(7, len(records)))
    assert plan.report["provenance"]["closure_indices"] == list(range(7, len(records)))
    assert all(record is records[record.index] for record in plan.replay_records)
    with pytest.raises(TypeError):
        plan.events_by_record[0] = ()
    with pytest.raises(SessionMigrateError, match="inconsistent"):
        plan_reviewed_carriers(records[:-1], selection)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_mode_flag_is_strict_boolean(value):
    data = encode_jsonl(topology())
    snapshot = NativeSnapshot.from_bytes(
        data, context=SourceCaptureContext(AgentFormat.CLAUDE, Path("/synthetic/source"))
    )
    for call in (
        lambda: claude.parse_bytes(data, Path("/synthetic/source"), reviewed_portability=value),
        lambda: snapshot.parse(reviewed_portability=value),
    ):
        with pytest.raises(SessionMigrateError, match="boolean"):
            call()


def test_explicit_mode_has_no_fallback_and_legacy_false_keeps_events_and_policy():
    rows = topology()
    data = encode_jsonl(rows)
    old = claude.parse_bytes(data, Path("/synthetic/source"))
    explicit = claude.parse_bytes(data, Path("/synthetic/source"), reviewed_portability=False)
    assert old == explicit and old.reviewed_report is explicit.reviewed_report is None
    assert any("native_context_policy" in event.payload for event in old.events)
    with pytest.raises(SessionMigrateError):
        parse(reviewed_rows(), reviewed=False)
    with pytest.raises(SessionMigrateError, match="preserved compaction"):
        parse([rows[-1]])
    for row in rows:
        row["version"] = "2.1.318"
    with pytest.raises(SessionMigrateError, match="2.1.309"):
        parse(rows)


def informational_rows(content="  Historical informational DATA ação\u2028\n  "):
    rows = topology()
    rows.append(
        {
            "type": "system",
            "subtype": "informational",
            "content": content,
            "uuid": "informational",
            "parentUuid": "after",
            "sessionId": "fixture-session",
            "version": "2.1.309",
            "isSidechain": False,
            "timestamp": "2026-11-06T10:00:00-03:00",
        }
    )
    return rows


@pytest.mark.parametrize("content", ["  Historical DATA ação\u2028\n  ", "", "\n\t "])
def test_system_informational_is_exact_quoted_user_data_with_source_provenance(content):
    rows = informational_rows(content)
    before = copy.deepcopy(rows)
    session = parse(rows)
    (event,) = [
        event
        for event in session.events
        if event.payload.get("reviewed_data_class") == "informational"
    ]
    captured = json.loads(event.text)
    assert captured["data"] == content and captured["recorded_role"] == "system"
    assert (event.kind, event.role) == (EventKind.CONTEXT, Role.USER)
    assert event.payload["reviewed_context_policy"] == SOURCE_REVIEWED_POLICY_ID
    assert event.payload["reviewed_pointer"] == "/content"
    assert "native_context_policy" not in event.payload
    assert (
        event.provenance.record_index,
        event.provenance.source_id,
        event.provenance.record_type,
        event.provenance.block_index,
    ) == (7, "informational", "system", None)
    report = session.reviewed_report
    assert report["system_authority_as_data"]["count"] == 1
    assert report["provenance"]["closure_indices"] == [7]
    assert (
        next(
            item
            for item in report["differences"]
            if item["code"] == "system_informational_as_quoted_data"
        )["count"]
        == 1
    )
    if content.strip():
        assert content not in json.dumps(report)
    assert session.source_sha256 == hashlib.sha256(encode_jsonl(rows)).hexdigest()
    assert rows == before
    with pytest.raises(SessionMigrateError, match="unsupported active system kind"):
        parse(rows, reviewed=False)


@pytest.mark.parametrize(
    "content", [None, 1, True, [], {}, [{"type": "text", "text": "unmodeled"}]]
)
def test_system_informational_requires_a_captured_string(content):
    with pytest.raises(SessionMigrateError, match="unsupported historical system record"):
        parse(informational_rows(content))


def test_unknown_system_subtype_does_not_inherit_informational_support():
    rows = informational_rows()
    rows[-1]["subtype"] = "unmodeled-system-kind"
    with pytest.raises(SessionMigrateError, match="unsupported historical system record"):
        parse(rows)


def test_informational_body_binding_and_independent_raw_audit_remain_unknown(tmp_path):
    from test_incomplete_tool_projection import changed_artifact
    from test_projections import observed
    from test_reviewed_portability_projection import parts, prepare

    from session_migrate.lineage import Coverage
    from session_migrate.projection_policies import ProjectionError
    from session_migrate.projections import verify_projection

    rows = informational_rows()
    values = parts(tmp_path, rows)
    prepared = prepare(values)
    assert prepared.source_coverage is Coverage.UNKNOWN
    assert prepared.expectation_coverage is Coverage.UNKNOWN
    assert prepared.source_observation.session.reviewed_report == parse(rows).reviewed_report
    assert verify_projection(prepared, observed(values[1]), context=values[-1]).semantic_matches
    changed_rows = copy.deepcopy(rows)
    changed_rows[-1]["content"] += "changed"
    changed = parse(changed_rows)
    assert changed.source_sha256 != prepared.source_authority_sha256
    assert (
        changed.reviewed_report["system_authority_as_data"]
        != parse(rows).reviewed_report["system_authority_as_data"]
    )

    def change_body(bundle):
        record = next(
            record
            for record in bundle["messages"]
            if record.get("type") == "synthetic"
            and '"source_class":"informational"' in record["text"]
        )
        quoted = json.loads(record["text"])
        quoted["data"] += "changed"
        record["text"] = json.dumps(
            quoted, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )

    altered = changed_artifact(values[1], change_body)
    assert not verify_projection(prepared, observed(altered), context=values[-1]).semantic_matches
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], altered, *values[2:]))


@pytest.mark.parametrize(
    "case,selected,thinking,system,wire,deferred",
    [
        ("case-01.jsonl", 172, 20, 23, 17, 13),
        ("case-02.jsonl", 427, 53, 48, 19, 7),
        ("case-03.jsonl", 374, 54, 43, 42, 26),
        ("case-04.jsonl", 101, 11, 20, 8, 7),
        ("case-05.jsonl", 45, 3, 16, 2, 1),
    ],
)
def test_authorized_synthetic_bundle(case, selected, thinking, system, wire, deferred):
    location = os.environ.get("SESSION_MIGRATE_REVIEWED_FIXTURE_ROOT")
    if location is None:
        pytest.skip("authorized synthetic fixture bundle not selected")
    root = Path(location)
    manifest = json.loads((root / "manifest.json").read_text())
    expected = next(item for item in manifest["cases"] if item["file"] == case)
    path = root / case
    data = path.read_bytes()
    before = hashlib.sha256(data).hexdigest()
    assert before == expected["sha256"]
    session = claude.parse_bytes(data, path, reviewed_portability=True)
    report = session.reviewed_report
    assert session.source_sha256 == before and session.raw_record_count == expected["records"]
    assert report["selected_count"] == selected
    assert report["thinking_as_data"]["count"] == thinking
    assert report["system_authority_as_data"]["count"] == system
    assert report["displayed_vs_wire_inputs"]["difference_count"] == wire
    classes = [item["record_class"] for item in report["archived_runtime_records"]]
    assert (
        classes.count("prompt_snapshot") == 2 and classes.count("deferred_tools_record") == deferred
    )
    assert (
        sum(event.kind is EventKind.TOOL_CALL for event in session.events)
        == report["displayed_vs_wire_inputs"]["displayed_count"]
    )
    assert all(
        event.role is Role.USER for event in session.events if event.kind is EventKind.CONTEXT
    )
    assert not any(
        event.kind in {EventKind.THINKING, EventKind.COMPACTION} for event in session.events
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(SessionMigrateError):
        claude.parse_bytes(data, path)
