"""Closed carrier contracts use born-synthetic, known native shapes only."""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude_carriers import POLICY_ID, plan_carriers
from session_migrate.formats.claude_compaction import select_compaction
from session_migrate.jsonl import JsonlRecord, encode_jsonl, iter_jsonl_bytes


def envelope(kind, uid, parent, **fields):
    return {
        "type": kind,
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": "synthetic-carrier-session",
        "cwd": "/fixture/work",
        "version": "2.1.309",
        "isSidechain": False,
        "timestamp": "2026-11-05T12:00:00.000Z",
        **fields,
    }


def conversation(kind, uid, parent, content, **fields):
    return envelope(kind, uid, parent, message={"role": kind, "content": content}, **fields)


def carrier(uid="carrier", parent="summary", kind="instructions", **fields):
    attachment = {
        "instructions": {
            "type": "instructions",
            "files": [
                {"path": "/fixture/CLAUDE.md", "type": "Project", "content": "raw historical text"}
            ],
        },
        "context_sections": {
            "type": "context_sections",
            "sections": [{"name": "Synthetic", "text": "raw historical text"}],
        },
        "date": {"type": "date", "date": "synthetic historical date"},
    }[kind]
    return envelope(
        "attachment",
        uid,
        parent,
        attachment=attachment,
        rendered=[{"content": "persisted carrier text"}],
        renderedRole="user",
        **fields,
    )


def topology():
    return [
        conversation("user", "old", None, "retired"),
        conversation("assistant", "kept", "old", "preserved"),
        envelope(
            "system",
            "boundary",
            None,
            subtype="compact_boundary",
            compactMetadata={"preservedMessages": {"anchorUuid": "summary", "uuids": ["kept"]}},
        ),
        conversation("user", "summary", "boundary", "historical summary", isCompactSummary=True),
        carrier(),
        conversation("user", "request", "carrier", "ordinary human request"),
        conversation("assistant", "answer", "request", "answer"),
    ]


def prepare(rows):
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    selection = select_compaction(records)
    assert selection is not None
    return records, selection


def plan(rows):
    return plan_carriers(*prepare(rows))


def replay_ids(result):
    return [record.value["uuid"] for record in result.replay_records]


def test_bridge_text_and_hoist_use_original_records_without_mutation():
    rows = topology()
    rows[4]["parentUuid"] = "boundary"
    rows[3]["parentUuid"] = "carrier"
    rows[5]["parentUuid"] = "summary"
    before = copy.deepcopy(rows)
    records, selection = prepare(rows)
    snapshots = copy.deepcopy(records)
    result = plan_carriers(records, selection)
    # The summary is an ordinary user for OAs, so instructions hoist before it.
    assert replay_ids(result) == ["carrier", "boundary", "summary", "kept", "request", "answer"]
    assert dict(result.context_text) == {4: ("persisted carrier text",)}
    assert result.inert_indices == (2,)
    assert rows == before and records == snapshots
    assert all(record is records[record.index] for record in result.replay_records)
    assert result.policy_id == POLICY_ID
    assert "runtime_hook_transformations_unproven" in result.diagnostics
    assert "persisted carrier text" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.policy_id = "changed"
    with pytest.raises(TypeError):
        result.context_text[4] = ("changed",)


@pytest.mark.parametrize("kind", ["instructions", "context_sections", "date"])
@pytest.mark.parametrize("position", ["bridge", "finaldescendant"])
def test_supported_rendering_survives_bridge_and_final_descendant(kind, position):
    rows = topology()
    rows[4] = carrier(kind=kind)
    if position == "finaldescendant":
        rows[5]["parentUuid"] = "summary"
        rows[4]["parentUuid"] = "answer"
    result = plan(rows)
    assert result.context_text[4] == ("persisted carrier text",)
    assert replay_ids(result).count("carrier") == 1
    if position == "finaldescendant":
        assert replay_ids(result)[-2:] == ["answer", "carrier"]
        assert "native_metadata_descendants_restored" in result.diagnostics


def test_all_rendered_entries_and_blocks_keep_boundaries_and_order():
    rows = topology()
    rows[4]["rendered"] = [
        {"content": "first\n"},
        {"content": [{"type": "text", "text": "second"}, {"type": "text", "text": "third"}]},
        {"content": "fourth"},
    ]
    assert plan(rows).context_text[4] == ("first\n", "second", "third", "fourth")


@pytest.mark.parametrize("role", [None, "system", "assistant", "", 1, [], {}])
def test_missing_or_nonuser_recorded_authority_is_rejected(role):
    rows = topology()
    if role is None:
        del rows[4]["renderedRole"]
    else:
        rows[4]["renderedRole"] = role
    with pytest.raises(SessionMigrateError, match="explicit user authority"):
        plan(rows)


@pytest.mark.parametrize(
    "rendered",
    [
        None,
        [],
        "text",
        {},
        [{"content": ""}],
        [{"content": []}],
        [{"content": 1}],
        [{"text": "text"}],
        [{"content": "text", "role": "system"}],
        [{"content": [{"type": "image", "text": "text"}]}],
        [{"content": [{"type": "text", "text": ""}]}],
        [{"content": [{"type": "text", "text": "text", "cache_control": {}}]}],
        [{"content": [{"type": "text"}]}],
        [{"content": ["text"]}],
    ],
)
def test_bad_or_extended_rendering_never_falls_back_to_raw_content(rendered):
    rows = topology()
    rows[4]["rendered"] = rendered
    with pytest.raises(SessionMigrateError, match="rendering"):
        plan(rows)


@pytest.mark.parametrize(
    "kind",
    [
        "queued_command",
        "poll_events",
        "coordinator_context",
        "deferred_tools_delta",
        "deferred_tools_record",
        "session_context",
        "prompt_snapshot",
        "fork_briefing",
        "synthetic",
        "synthetic_metadata",
        "unknown",
        "content-replacement",
    ],
)
def test_unsupported_active_attachment_kinds_are_rejected_even_rendered(kind):
    rows = topology()
    rows[4]["attachment"] = {"type": kind, "prompt": "material must not disappear"}
    with pytest.raises(SessionMigrateError):
        plan(rows)


@pytest.mark.parametrize(
    "field",
    [
        "renderedInHumanTurn",
        "taskDelivery",
        "source",
        "origin",
        "renderedByBatchHead",
        "reminderId",
        "batch",
        "leftOut",
        "rewritten",
        "unknownField",
        "isVirtual",
    ],
)
def test_unsupported_envelope_controls_are_rejected(field):
    rows = topology()
    rows[4][field] = "source control sentinel"
    with pytest.raises(SessionMigrateError, match="field or override") as error:
        plan(rows)
    assert "source control sentinel" not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"files": None},
        {"files": [{}]},
        {"files": [{"path": "x", "type": "Unknown", "content": "x"}]},
        {"files": [{"path": "x", "type": [], "content": "x"}]},
        {"files": [{"path": "x", "type": "Project", "content": "x", "extra": 1}]},
        {"removed": "path"},
        {"removed": [False]},
        {"changed": 1},
        {"reason": "unknown"},
        {"reason": []},
        {"taskDelivery": {}},
    ],
)
def test_malformed_instructions_or_unknown_fields_are_rejected(mutation):
    rows = topology()
    rows[4]["attachment"].update(mutation)
    with pytest.raises(SessionMigrateError):
        plan(rows)


@pytest.mark.parametrize(
    "kind,fields",
    [
        ("context_sections", {"sections": None}),
        ("context_sections", {"sections": [{"name": "x", "text": "x", "extra": 1}]}),
        ("context_sections", {"sections": [{"name": 1, "text": "x"}]}),
        ("date", {"date": 1}),
        ("date", {"changed": "yes"}),
        ("date", {"extra": 1}),
    ],
)
def test_malformed_context_sections_and_date_fail_closed(kind, fields):
    rows = topology()
    rows[4] = carrier(kind=kind)
    rows[4]["attachment"].update(fields)
    with pytest.raises(SessionMigrateError):
        plan(rows)


def test_known_instructions_fields_and_file_types_preserve_persisted_text():
    rows = topology()
    rows[4]["attachment"].update(
        files=[
            {"path": "/unread/" + kind, "type": kind, "content": "", "unverified": True}
            for kind in ["User", "Project", "Local", "Managed", "AutoMem"]
        ],
        removed=["/never/read"],
        changed=True,
        reason="compaction",
    )
    assert plan(rows).context_text[4] == ("persisted carrier text",)


@pytest.mark.parametrize("position", ["bridge", "finaldescendant"])
def test_local_command_is_historical_user_context_never_executed(position):
    rows = topology()
    rows[4] = envelope(
        "system",
        "carrier",
        "summary",
        subtype="local_command",
        content="$(must-never-execute)\nretain historical text",
    )
    if position == "finaldescendant":
        rows[4]["parentUuid"] = "answer"
        rows[5]["parentUuid"] = "summary"
    result = plan(rows)
    assert result.context_text[4] == ("$(must-never-execute)\nretain historical text",)


@pytest.mark.parametrize("subtype", ["unknown", "file_snapshot", "synthetic_metadata"])
def test_unknown_system_is_not_dropped(subtype):
    rows = topology()
    rows[4] = envelope("system", "carrier", "summary", subtype=subtype, content="material")
    with pytest.raises(SessionMigrateError, match="system kind"):
        plan(rows)


def test_only_demonstrated_inert_shapes_are_omitted_with_diagnostics():
    rows = topology()
    rows[4] = envelope(
        "system",
        "carrier",
        "summary",
        subtype="turn_duration",
        content="display only",
        durationMs=1,
        messageCount=2,
    )
    rows.append(
        envelope(
            "attachment",
            "hook",
            "answer",
            attachment={
                "type": "hook_system_message",
                "content": "display only",
                "hookName": "Synthetic",
                "hookEvent": "SessionStart",
            },
        )
    )
    result = plan(rows)
    assert not result.context_text
    assert result.inert_indices == (2, 4, 7)
    assert "inert_turn_duration_omitted" in result.diagnostics
    assert "inert_hook_runtime_metadata_omitted" in result.diagnostics


@pytest.mark.parametrize("kind", ["hook", "duration"])
@pytest.mark.parametrize("override", ["rendered", "taskDelivery", "unknown", "hooks"])
def test_inert_metadata_with_any_unknown_override_is_rejected(kind, override):
    rows = topology()
    if kind == "hook":
        rows[4] = envelope(
            "attachment",
            "carrier",
            "summary",
            attachment={
                "type": "hook_system_message",
                "content": "x",
                "hookName": "x",
                "hookEvent": "x",
            },
        )
    else:
        rows[4] = envelope(
            "system",
            "carrier",
            "summary",
            subtype="turn_duration",
            content="x",
            durationMs=1,
            messageCount=2,
        )
    rows[4][override] = []
    with pytest.raises(SessionMigrateError):
        plan(rows)


@pytest.mark.parametrize(
    "field,value",
    [("durationMs", True), ("durationMs", -1), ("messageCount", 1.5), ("messageCount", -1)],
)
def test_bad_inert_duration_fields_fail_closed(field, value):
    rows = topology()
    rows[4] = envelope(
        "system",
        "carrier",
        "summary",
        subtype="turn_duration",
        content="x",
        durationMs=1,
        messageCount=2,
    )
    rows[4][field] = value
    with pytest.raises(SessionMigrateError, match="duration metadata"):
        plan(rows)


@pytest.mark.parametrize("field", ["isVirtual", "taskDelivery", "rendered"])
@pytest.mark.parametrize("position", [5, 6])
def test_selected_conversation_overrides_are_rejected(field, position):
    rows = topology()
    rows[position][field] = True
    with pytest.raises(SessionMigrateError, match="conversation delivery"):
        plan(rows)


@pytest.mark.parametrize("position", [0, 4, 6])
def test_content_replacement_anywhere_including_retired_data_is_rejected(position):
    rows = topology()
    rows.insert(
        position,
        {
            "type": "content-replacement",
            "replacements": [
                {"kind": "tool-result", "toolUseId": "tool", "replacement": "changed body"}
            ],
        },
    )
    with pytest.raises(SessionMigrateError, match="content replacement"):
        plan(rows)


def test_native_hoist_preserves_carrier_order_and_noncarrier_order():
    rows = topology()
    rows.extend(
        [
            conversation("user", "next", "answer", "ordinary"),
            envelope("system", "local", "next", subtype="local_command", content="historical"),
            carrier("c2", "local", kind="date"),
            conversation("user", "ordinary2", "c2", "ordinary2"),
            carrier("c3", "ordinary2", kind="context_sections"),
            conversation("assistant", "end", "c3", "end"),
        ]
    )
    ids = replay_ids(plan(rows))
    assert ids[ids.index("answer") : ids.index("end")] == [
        "answer",
        "c2",
        "c3",
        "next",
        "local",
        "ordinary2",
    ]


def test_first_tool_result_content_block_is_a_hoist_barrier():
    rows = topology()
    rows[6]["message"]["content"] = [
        {"type": "tool_use", "id": "call", "name": "Test", "input": {}}
    ]
    rows.extend(
        [
            conversation(
                "user",
                "result",
                "answer",
                [{"type": "tool_result", "tool_use_id": "call", "content": "result"}],
            ),
            conversation("user", "ordinary", "result", "ordinary"),
            carrier("c2", "ordinary"),
            conversation("assistant", "end", "c2", "end"),
        ]
    )
    ids = replay_ids(plan(rows))
    assert ids[ids.index("result") : ids.index("end")] == ["result", "c2", "ordinary"]


def test_nonfirst_tool_result_does_not_create_hoist_barrier():
    # Direct plan construction isolates OAs from separate tool-batch guards.
    records, selection = prepare(topology())
    changed = dict(records[5].value)
    changed["message"] = {
        "role": "user",
        "content": [
            {"type": "text", "text": "ordinary"},
            {"type": "tool_result", "tool_use_id": "x"},
        ],
    }
    replaced = JsonlRecord(5, records[5].line_number, changed)
    records = records[:5] + (replaced,) + records[6:]
    selection = replace(
        selection, selected_records=tuple(records[r.index] for r in selection.selected_records)
    )
    ids = replay_ids(plan_carriers(records, selection))
    assert ids.index("carrier") == ids.index("kept") + 1


def test_fpo_branch_with_divergent_dfs_and_timestamp_order_is_rejected():
    rows = topology()
    rows.extend([carrier("a", "answer"), carrier("b", "answer"), carrier("c", "a")])
    rows[7]["timestamp"] = "2026-11-05T12:00:01Z"
    rows[8]["timestamp"] = "2026-11-05T12:00:02Z"
    rows[9]["timestamp"] = "2026-11-05T12:00:03Z"
    with pytest.raises(SessionMigrateError, match="ambiguous native metadata material order"):
        plan(rows)


def test_fpo_unambiguous_chain_keeps_all_material_text():
    rows = topology()
    rows.extend([carrier("a", "answer"), carrier("b", "a")])
    rows[7]["rendered"] = [{"content": "first descendant"}]
    rows[8]["rendered"] = [{"content": "second descendant"}]
    result = plan(rows)
    assert replay_ids(result)[-3:] == ["answer", "a", "b"]
    assert result.context_text[7] == ("first descendant",)
    assert result.context_text[8] == ("second descendant",)


def test_fpo_same_timestamp_branch_divergence_is_rejected():
    rows = topology() + [carrier("a", "answer"), carrier("b", "answer"), carrier("c", "a")]
    with pytest.raises(SessionMigrateError, match="ambiguous native metadata material order"):
        plan(rows)


def test_fpo_missing_order_timestamp_is_rejected_for_multiple_material_rows():
    rows = topology() + [carrier("a", "answer"), carrier("b", "a")]
    del rows[8]["timestamp"]
    with pytest.raises(SessionMigrateError, match="unproven metadata descendant timestamp"):
        plan(rows)


def test_unsupported_fpo_descendant_is_not_silently_ignored():
    rows = topology() + [
        envelope(
            "attachment", "q", "answer", attachment={"type": "queued_command", "prompt": "material"}
        )
    ]
    with pytest.raises(SessionMigrateError, match="unsupported active attachment"):
        plan(rows)


def test_nonactive_unrecognized_display_metadata_can_remain_accounting_only():
    rows = topology()
    rows[0]["display"] = {"type": "unrecognized-display-only"}
    assert plan(rows).context_text[4] == ("persisted carrier text",)


def test_duplicate_record_indices_are_rejected():
    records, selection = prepare(topology())
    with pytest.raises(SessionMigrateError, match="duplicate physical"):
        plan_carriers((*records, records[4]), selection)


def test_duplicate_uuids_are_rejected():
    records, selection = prepare(topology())
    duplicate = JsonlRecord(7, 8, records[4].value)
    with pytest.raises(SessionMigrateError, match="duplicate record UUID"):
        plan_carriers((*records, duplicate), selection)


def test_inconsistent_or_foreign_selection_objects_are_rejected():
    records, selection = prepare(topology())
    foreign = tuple(copy.deepcopy(record) for record in selection.selected_records)
    with pytest.raises(SessionMigrateError, match="inconsistent compaction"):
        plan_carriers(records, replace(selection, selected_records=foreign))


def test_dangling_effective_parent_is_rejected():
    records, selection = prepare(topology())
    parents = dict(selection.effective_parents)
    parents[4] = 999
    with pytest.raises(SessionMigrateError, match="dangling effective"):
        plan_carriers(records, replace(selection, effective_parents=MappingProxyType(parents)))


@pytest.mark.parametrize(
    "kind", ["prompt_snapshot", "prompt_render_point", "deferred_tools_record"]
)
@pytest.mark.parametrize("position", ["retired", "descendant"])
def test_persisted_controls_are_rejected_even_before_cut(kind, position):
    rows = topology()
    if position == "retired":
        rows[0] = envelope("attachment", "old", None, attachment={"type": kind})
    else:
        rows.append(envelope("attachment", "control", "answer", attachment={"type": kind}))
    with pytest.raises(SessionMigrateError, match="persisted prompt or tool control"):
        plan(rows)


@pytest.mark.parametrize("following", [" ", "\t", "\n", "\x1c", "\x1f", "\x85"])
def test_rendered_reminder_text_that_needs_native_escaping_is_rejected(following):
    rows = topology()
    rows[4]["rendered"] = [{"content": "<system-reminder" + following + "id='x'>historical"}]
    with pytest.raises(SessionMigrateError, match="unsupported native normalization"):
        plan(rows)


def test_plain_system_reminder_envelope_is_preserved_verbatim():
    rows = topology()
    rows[4]["rendered"] = [{"content": "<system-reminder>historical\n</system-reminder>"}]
    assert plan(rows).context_text[4] == ("<system-reminder>historical\n</system-reminder>",)


def test_out_of_physical_order_source_is_rejected():
    records, selection = prepare(topology())
    with pytest.raises(SessionMigrateError, match="not in physical order"):
        plan_carriers(tuple(reversed(records)), selection)


@pytest.mark.parametrize("field", ["renderedBesideToolResult", "placement"])
def test_recently_proven_placement_controls_remain_outside_supported_envelope(field):
    rows = topology()
    rows[4][field] = None
    with pytest.raises(SessionMigrateError, match="field or override"):
        plan(rows)


@pytest.mark.parametrize("position", [4, 5, 6])
def test_task_delivery_presence_including_null_is_refused(position):
    rows = topology()
    rows[position]["taskDelivery"] = None
    with pytest.raises(SessionMigrateError):
        plan(rows)


def test_fpo_order_difference_only_in_inert_metadata_does_not_drop_material():
    rows = topology()
    rows.extend(
        [
            envelope(
                "system",
                "inert",
                "answer",
                subtype="turn_duration",
                content="display",
                durationMs=1,
                messageCount=2,
            ),
            carrier("material", "answer"),
            envelope(
                "system",
                "inert2",
                "inert",
                subtype="turn_duration",
                content="display",
                durationMs=2,
                messageCount=2,
            ),
        ]
    )
    rows[7]["timestamp"] = "2026-11-05T12:00:01Z"
    rows[8]["timestamp"] = "2026-11-05T12:00:02Z"
    rows[9]["timestamp"] = "2026-11-05T12:00:03Z"
    result = plan(rows)
    assert result.context_text[8] == ("persisted carrier text",)
    assert set(result.replay_indices) == {1, 2, 3, 4, 5, 6, 7, 8, 9}


def test_unknown_descendant_of_descendant_is_refused():
    rows = topology() + [carrier("first", "answer")]
    rows.append(envelope("system", "bad", "first", subtype="unknown", content="material"))
    with pytest.raises(SessionMigrateError, match="unsupported active system kind"):
        plan(rows)


def test_fpo_nonascii_timestamp_order_is_refused_instead_of_guessing_js_comparison():
    rows = topology() + [carrier("a", "answer"), carrier("b", "a")]
    rows[8]["timestamp"] = "\U00010000"
    with pytest.raises(SessionMigrateError, match="unproven metadata descendant timestamp"):
        plan(rows)


def test_control_discriminator_inside_literal_tool_input_is_not_a_control_record():
    rows = topology()
    rows[1]["message"]["content"] = [
        {
            "type": "tool_use",
            "id": "literal-data",
            "name": "Read",
            "input": {"type": "content-replacement", "replacement": "ordinary user data"},
        }
    ]
    assert plan(rows).replay_indices


@pytest.mark.parametrize(
    "field", ["isApiErrorMessage", "batchToolUses", "wireToolInputs", "wireIngestContext"]
)
@pytest.mark.parametrize("role", ["user", "assistant"])
def test_known_conversation_controls_are_not_treated_as_plain_history(field, role):
    rows = topology()
    position = 5 if role == "user" else 6
    rows[position][field] = True if field == "isApiErrorMessage" else None
    if role == "assistant":
        rows[position]["message"]["model"] = "<synthetic>"
    with pytest.raises(SessionMigrateError, match="conversation delivery"):
        plan(rows)


@pytest.mark.parametrize(
    "origin", [None, {"kind": "task-notification"}, {"kind": "human", "extra": True}, "human"]
)
def test_origins_that_change_native_wrapping_are_refused(origin):
    rows = topology()
    rows[5]["origin"] = origin
    with pytest.raises(SessionMigrateError, match="conversation origin"):
        plan(rows)


def test_explicit_human_origin_retains_plain_conversation():
    rows = topology()
    rows[5]["origin"] = {"kind": "human"}
    assert plan(rows).replay_indices


@pytest.mark.parametrize("position", [3, 5])
@pytest.mark.parametrize(
    "content",
    ['<system-reminder id="abcd">text', [{"type": "text", "text": "<system-reminder\x85text"}]],
)
def test_native_text_escape_is_guarded_for_summary_and_user(position, content):
    rows = topology()
    rows[position]["message"]["content"] = content
    with pytest.raises(SessionMigrateError, match="native normalization"):
        plan(rows)


def test_ecmascript_bom_whitespace_cannot_bypass_reminder_escape_guard():
    rows = topology()
    rows[4]["rendered"] = [{"content": '<system-reminder\ufeffid="deadbeef">text'}]
    with pytest.raises(SessionMigrateError, match="native normalization"):
        plan(rows)
    rows = topology()
    rows[5]["message"]["content"] = '<system-reminder\ufeffid="deadbeef">text'
    with pytest.raises(SessionMigrateError, match="native normalization"):
        plan(rows)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "  ",
        "\ufeff",
        [],
        [{"type": "text", "text": "\ufeff"}],
        [{"type": "thinking", "thinking": "private"}],
        [{"type": "redacted_thinking", "data": "opaque"}],
    ],
)
def test_assistant_barriers_requiring_native_normalization_are_refused(content):
    rows = topology()
    rows[6]["message"]["content"] = content
    with pytest.raises(SessionMigrateError, match="assistant normalization"):
        plan(rows)


def test_local_command_empty_fallback_is_not_silently_omitted():
    rows = topology()
    rows[4] = envelope("system", "carrier", "summary", subtype="local_command", content="")
    with pytest.raises(SessionMigrateError, match="local command content"):
        plan(rows)


@pytest.mark.parametrize(
    "content",
    ["(no content)", "  [Empty text removed]  ", [{"type": "text", "text": "(no content)"}]],
)
def test_native_empty_assistant_literals_do_not_create_false_barriers(content):
    rows = topology()
    rows[6]["message"]["content"] = content
    with pytest.raises(SessionMigrateError, match="assistant normalization"):
        plan(rows)
