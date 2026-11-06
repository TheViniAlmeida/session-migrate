"""Compare source occurrence order with stock 2.2.5 raw target lowering."""

import copy
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_claude_cross_chain import assistant, batch_rows
from test_claude_desktop309 import audit_rows
from test_claude_latest import _parse
from test_incomplete_tool_projection import bundle_tools, changed_artifact
from test_native_context_projection import ARCHIVE, STRICT, context_records, parts, prepare
from test_projections import observed
from test_recovered_projections import fixture as legacy_recovery_fixture

from session_migrate import recovered_projections
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.formats.claude_carriers import plan_carriers
from session_migrate.formats.claude_compaction import select_compaction
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.projection_policies import (
    INCOMPLETE_TOOL_MESSAGE,
    ProjectionError,
    expected_from_source,
    native_context_order_matches,
)
from session_migrate.projections import (
    _observe,
    _same_semantics,
    _target_conversation_witness,
    verify_projection,
)


def serial_rows(*, text=False, error=False):
    rows = batch_rows(tools=True)
    rows[4]["parentUuid"] = "R1"
    rows[4]["message"]["id"] = "second-batch"
    if text:
        rows.insert(6, assistant("T", "R1", batch="text-batch"))
        rows[4]["parentUuid"] = "T"
    if error:
        rows[5]["message"]["content"][0].update(is_error=True, content=INCOMPLETE_TOOL_MESSAGE)
    return rows


def grouped_rows():
    rows = batch_rows(tools=True)
    rows[3]["message"]["content"].extend(rows[4]["message"]["content"])
    rows[5]["message"]["content"].extend(rows[6]["message"]["content"])
    rows[-1]["parentUuid"] = "R1"
    return [value for value in rows if value["uuid"] not in {"A2", "R2"}]


def merge_assistants(bundle):
    messages = [message for message in bundle["messages"] if message["type"] == "assistant"]
    first, second = messages[:2]
    first["content"].extend(second["content"])
    bundle["messages"].remove(second)


def independent(values, *, policy=STRICT):
    source = _observe(values[0], values[3].source_client, policy)
    expected = expected_from_source(source, target_format=values[4].target_format, policy=policy)
    return source, expected


@pytest.mark.parametrize("policy", [STRICT, ARCHIVE])
def test_parallel_results_are_rejected_before_native_io(tmp_path, monkeypatch, policy):
    values = parts(tmp_path, batch_rows(tools=True), policy=policy)
    source, expected = independent(values, policy=policy)
    target = observed(values[1])
    portable = _observe(target, "2.2.5", policy)
    # This is the original false PASS: both the writer and expected projection
    # associate results with calls, erasing the independent source ordering.
    assert _same_semantics(
        expected.witness.prefix_witness,
        _target_conversation_witness(portable, target, values[2], policy),
    )
    assert not native_context_order_matches(source, expected, target)
    effects = []

    def forbidden(*args, **kwargs):
        effects.append(True)
        raise AssertionError("native process invoked")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare(values, policy=policy)
    assert not effects


@pytest.mark.parametrize("policy", [STRICT, ARCHIVE])
@pytest.mark.parametrize("rows", [serial_rows(), grouped_rows(), batch_rows()])
def test_serial_grouped_tools_and_text_batch_preserve_order(tmp_path, policy, rows):
    values = parts(tmp_path, copy.deepcopy(rows), policy=policy)
    before = values[0].raw_bytes
    prepared = prepare(values, policy=policy)
    assert native_context_order_matches(
        prepared.source_observation, prepared.expected, observed(values[1])
    )
    assert verify_projection(prepared, observed(values[1]), context=values[-1]).semantic_matches
    assert values[0].raw_bytes == before


def test_correctly_grouped_parallel_calls_do_not_require_source_row_grouping(tmp_path):
    rows = grouped_rows()
    second = assistant("A2", "A1", call="t2", batch="second-batch")
    rows[3]["message"]["content"].pop()
    rows[4]["parentUuid"] = "A2"
    rows.insert(4, second)
    values = parts(tmp_path, rows)
    # Separate target messages emit C1,R1,C2,R2, which is wrong for this source.
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare(values)
    changed = changed_artifact(values[1], merge_assistants)
    prepared = prepare((values[0], changed, *values[2:]))
    assert verify_projection(prepared, observed(changed), context=values[-1]).semantic_matches


def test_same_assistant_cannot_reverse_only_its_result_order(tmp_path):
    rows = grouped_rows()
    rows[4]["message"]["content"].reverse()
    values = parts(tmp_path, rows)
    source, expected = independent(values)
    target = observed(values[1])
    assert _same_semantics(
        expected.witness.prefix_witness,
        _target_conversation_witness(_observe(target, "2.2.5", STRICT), target, values[2], STRICT),
    )
    assert not native_context_order_matches(source, expected, target)
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare(values)


@pytest.mark.parametrize("text,error", [(False, False), (True, False), (False, True)])
def test_cold_target_grouping_cannot_reorder_results_or_text(tmp_path, text, error):
    values = parts(tmp_path, serial_rows(text=text, error=error))
    prepared = prepare(values)
    changed = changed_artifact(values[1], merge_assistants)
    snapshot = observed(changed)
    portable = _observe(snapshot, "2.2.5", STRICT)
    assert _same_semantics(
        prepared.expected.witness.prefix_witness,
        _target_conversation_witness(portable, snapshot, values[2], STRICT),
    )
    result = verify_projection(prepared, snapshot, context=values[-1])
    assert not result.semantic_matches
    assert result.reason == "semantic_mismatch"
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], changed, *values[2:]))


def test_text_before_tools_inside_assistant_keeps_native_order(tmp_path):
    rows = grouped_rows()
    rows[3]["message"]["content"].insert(0, {"type": "text", "text": "before calls"})
    values = parts(tmp_path, rows)
    prepared = prepare(values)
    assert native_context_order_matches(
        prepared.source_observation, prepared.expected, observed(values[1])
    )


def test_only_proven_generated_archive_results_are_excluded(tmp_path):
    values = parts(tmp_path, context_records(open_call=True), policy=ARCHIVE)
    prepared = prepare(values, policy=ARCHIVE)
    assert prepared.expected.generated_members
    assert native_context_order_matches(
        prepared.source_observation, prepared.expected, observed(values[1])
    )

    def ordinary_error(bundle):
        bundle_tools(bundle)[0]["state"]["error"]["type"] = "ToolError"

    changed = changed_artifact(values[1], ordinary_error)
    assert not native_context_order_matches(
        prepared.source_observation, prepared.expected, observed(changed)
    )
    assert not verify_projection(prepared, observed(changed), context=values[-1]).semantic_matches


def test_real_same_text_error_remains_a_source_result(tmp_path):
    values = parts(tmp_path, serial_rows(error=True))
    prepared = prepare(values)
    assert not prepared.expected.generated_members
    assert native_context_order_matches(
        prepared.source_observation, prepared.expected, observed(values[1])
    )

    def forged_archive(bundle):
        bundle_tools(bundle)[0]["state"]["error"]["type"] = "ImportedIncompleteTool"

    changed = changed_artifact(values[1], forged_archive)
    assert not native_context_order_matches(
        prepared.source_observation, prepared.expected, observed(changed)
    )


@pytest.mark.parametrize("placement", ["executed", "native", "reasoning", "unsettled", "files"])
def test_unproven_native_placements_fail_closed(tmp_path, placement):
    values = parts(tmp_path, serial_rows())
    source, expected = independent(values)

    def alter(bundle):
        tool = bundle_tools(bundle)[0]
        if placement == "executed":
            tool["executed"] = True
        elif placement == "native":
            tool["native"] = {"opaque": "synthetic"}
        elif placement == "reasoning":
            next(m for m in bundle["messages"] if m["type"] == "assistant")["content"].append(
                {"type": "reasoning", "text": "synthetic reasoning"}
            )
        elif placement == "unsettled":
            tool["state"]["status"] = "running"
        else:
            next(m for m in bundle["messages"] if m["type"] == "user")["files"] = [{}]

    changed = changed_artifact(values[1], alter)
    assert not native_context_order_matches(source, expected, observed(changed))


def test_legacy_portable_policy_does_not_gain_native_order_constraints(tmp_path):
    values = parts(tmp_path, batch_rows(tools=True))
    source, expected = independent(values)
    assert native_context_order_matches(
        source, replace(expected, native_context_only=False), observed(values[1])
    )


@pytest.mark.parametrize(
    "block", [{"type": "thinking", "thinking": "synthetic reasoning"}, {"type": "unproven-part"}]
)
def test_unproven_source_material_cannot_disappear_from_order_proof(tmp_path, block):
    rows = serial_rows()
    rows[3]["message"]["content"].append(block)
    with pytest.raises(SessionMigrateError, match="native capture parsing failed"):
        parts(tmp_path, rows)


def test_public_cold_recovery_still_refuses_opencode225(tmp_path):
    legacy = legacy_recovery_fixture(tmp_path)
    values = parts(tmp_path, serial_rows())
    with pytest.raises(recovered_projections.RecoveryProjectionError, match="proof_unavailable"):
        recovered_projections._prepare_recovery_context(
            legacy[0],
            observed(values[1]),
            legacy[2],
            legacy[3],
            source_environment=legacy[4],
            target_environment=legacy[4],
        )


def test_recovery_prepare_order_guard_precedes_unavailable_cold_proof(tmp_path, monkeypatch):
    values = parts(tmp_path, batch_rows(tools=True))
    target = observed(values[1])
    record = SimpleNamespace(versions=values[3])
    context = SimpleNamespace(
        _source=values[0],
        _input=target,
        _record=record,
        _policy=STRICT,
        projection_context=values[-1],
    )
    # Public recovery issuance still refuses OpenCode (_FORMATS is unchanged).
    # Exercise only the existing prepare seam's defense, without claiming a
    # valid OP2 recovery context or constructing any recovery certificate.
    monkeypatch.setattr(recovered_projections, "_validate_context", lambda context: None)
    with pytest.raises(recovered_projections.RecoveryProjectionError, match="semantic_mismatch"):
        recovered_projections.prepare_recovered_projection(values[0], target, record, context)


_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1S8AAAAASUVORK5CYII="
)


@pytest.mark.parametrize("mixed", [False, True])
def test_nested_source_result_media_is_outside_textual_order_contract(tmp_path, mixed):
    rows = serial_rows()
    result = next(value for value in rows if value["uuid"] == "R1")["message"]["content"][0]
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG}}
    result["content"] = ([{"type": "text", "text": "synthetic result t1"}] if mixed else []) + [
        image
    ]
    values = parts(tmp_path, rows)
    source, expected = independent(values)
    assert not native_context_order_matches(source, expected, observed(values[1]))
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare(values)


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("error", [False, True])
def test_nested_target_result_media_is_outside_textual_order_contract(tmp_path, mixed, error):
    values = parts(tmp_path, serial_rows(error=error))
    prepared = prepare(values)

    def media(bundle):
        state = bundle_tools(bundle)[0]["state"]
        text = state.get("content", []) if mixed else []
        state["content"] = [
            *text,
            {"type": "file", "uri": "data:image/png;base64," + _PNG, "mime": "image/png"},
        ]

    changed = changed_artifact(values[1], media)
    assert not native_context_order_matches(
        prepared.source_observation, prepared.expected, observed(changed)
    )
    assert not verify_projection(prepared, observed(changed), context=values[-1]).semantic_matches
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], changed, *values[2:]))


def truncated_context(*, path="/synthetic/never-open-this-file", flag=True):
    return {"file": {"truncatedByTokenCap": flag, "filePath": path, "numLines": 1, "totalLines": 2}}


def test_material_truncation_context_is_rejected_without_reading_path(tmp_path, monkeypatch):
    rows = serial_rows()
    rows[5]["toolUseResult"] = truncated_context()
    before = copy.deepcopy(rows)
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    selection = select_compaction(records)
    effects = []

    def forbidden(*args, **kwargs):
        effects.append(True)
        raise AssertionError("file path evaluated")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(SessionMigrateError, match="truncation context"):
        plan_carriers(records, selection)
    with pytest.raises(SessionMigrateError, match="truncation context"):
        _parse(rows)
    assert rows == before
    assert not effects
    audit = audit_rows(rows, tmp_path)
    assert "invalid_graph" in dict(audit.issue_counts)


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_cannot_hide_changed_material_truncation_context(reverse):
    rows = serial_rows()
    harmless = copy.deepcopy(rows[5])
    truncated = copy.deepcopy(harmless)
    truncated["toolUseResult"] = truncated_context()
    rows[5] = truncated if reverse else harmless
    rows.append(harmless if reverse else truncated)
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        _parse(rows)


def test_identical_truncated_duplicate_retains_override_and_is_rejected():
    rows = serial_rows()
    rows[5]["toolUseResult"] = truncated_context()
    rows.append(copy.deepcopy(rows[5]))
    logical, duplicates, _ = claude._collapse_desktop_envelopes(
        tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    )
    assert duplicates == {len(rows) - 1}
    assert (
        next(value for value in logical if value.value["uuid"] == "R1").value["toolUseResult"]
        == truncated_context()
    )
    with pytest.raises(SessionMigrateError, match="truncation context"):
        _parse(rows)


@pytest.mark.parametrize(
    "metadata",
    [
        {"text": "inert synthetic metadata"},
        truncated_context(flag=False),
        truncated_context(flag="true"),
        truncated_context(path=None),
    ],
)
def test_unproven_arbitrary_tool_metadata_is_not_blanket_banned(tmp_path, metadata):
    rows = serial_rows()
    rows[5]["toolUseResult"] = copy.deepcopy(metadata)
    prepare(parts(tmp_path, rows))


def test_truncation_shaped_metadata_without_tool_result_is_not_material(tmp_path):
    rows = serial_rows()
    rows[0]["toolUseResult"] = truncated_context()
    prepare(parts(tmp_path, rows))


def test_identical_retired_truncation_overrides_remain_accounting_only(tmp_path):
    rows = serial_rows()
    old = copy.deepcopy(rows[5])
    old.update(uuid="old-result", parentUuid=None, toolUseResult=truncated_context())
    rows.insert(0, old)
    rows.append(copy.deepcopy(old))
    values = parts(tmp_path, rows)
    prepared = prepare(values)
    assert prepared.expected.native_context_only
    assert verify_projection(prepared, observed(values[1]), context=values[-1]).semantic_matches


@pytest.mark.parametrize("following", ["text", "thinking", "image", "unknown"])
def test_assistant_tool_followed_by_non_tool_block_has_no_flag_independent_order(following):
    rows = grouped_rows()
    rows[3]["message"]["content"].insert(
        1, {"type": following, "text": "synthetic following block"}
    )
    before = copy.deepcopy(rows)
    with pytest.raises(SessionMigrateError, match="native assistant block order"):
        _parse(rows)
    assert rows == before


@pytest.mark.parametrize("interleaved_result", [False, True])
def test_same_batch_sibling_text_cannot_follow_an_earlier_tool(interleaved_result):
    rows = grouped_rows()
    rows[3]["message"]["content"] = rows[3]["message"]["content"][:1]
    rows[4]["message"]["content"] = rows[4]["message"]["content"][:1]
    text = assistant("T", "R1" if interleaved_result else "A1", batch="batch")
    rows.insert(4 if not interleaved_result else 5, text)
    rows[-1]["parentUuid"] = "T" if interleaved_result else "R1"
    if not interleaved_result:
        next(value for value in rows if value["uuid"] == "R1")["parentUuid"] = "T"
    with pytest.raises(SessionMigrateError, match="native assistant block order"):
        _parse(rows)
