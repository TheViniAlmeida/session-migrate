"""Synthetic latest-boundary selection without rewriting captured envelopes."""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError

import pytest
from test_claude_desktop309 import desktop
from test_claude_desktop309_compaction import topology

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude_compaction import POLICY_ID, select_compaction
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes


def select(rows):
    return select_compaction(tuple(iter_jsonl_bytes(encode_jsonl(rows))))


def uuids(selection):
    return [record.value["uuid"] for record in selection.selected_records]


def test_indirect_anchor_native_edges_preserve_all_envelopes_and_indices():
    rows = topology()
    rows[1]["message"]["usage"] = {"input_tokens": 123, "output_tokens": 456}
    original = copy.deepcopy(rows)
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    before = copy.deepcopy(records)
    selection = select_compaction(records)
    assert uuids(selection) == ["boundary", "interposed", "summary", "head", "tail", "after"]
    assert selection.policy_id == POLICY_ID
    assert selection.selected_indices == (3, 4, 5, 1, 2, 6)
    assert dict(selection.effective_parents) == {1: 5, 2: 1, 3: None, 4: 3, 5: 4, 6: 2}
    assert selection.historical_indices == tuple(range(7))
    assert selection.retired_indices == selection.excluded_indices == (0,)
    assert selection.preserved_indices == (1, 2)
    assert selection.summary_index == 5
    assert selection.summary_boundary_index == selection.boundary_index == 3
    assert "running_turn_sends_not_transportable" in selection.diagnostics
    assert "preserved_usage_accounting_unproven" in selection.diagnostics
    assert rows == original and records == before
    assert selection.selected_records[3] is records[1]
    assert records[1].value["message"]["usage"] == {"input_tokens": 123, "output_tokens": 456}
    assert "source summary" not in repr(selection)
    with pytest.raises(TypeError):
        selection.effective_parents[6] = 5
    with pytest.raises(FrozenInstanceError):
        selection.boundary_index = 0


def test_preserved_list_is_authoritative_even_in_nonphysical_nonparent_order():
    rows = topology()
    rows[3]["compactMetadata"]["preservedMessages"]["uuids"] = ["tail", "head"]
    assert uuids(select(rows)) == ["boundary", "interposed", "summary", "tail", "head", "after"]


def test_list_authority_does_not_validate_or_follow_stale_segment():
    rows = topology()
    rows[3]["compactMetadata"]["preservedSegment"] = {"headUuid": "missing"}
    assert select(rows) is not None


@pytest.mark.parametrize("all_uuids", [None, ["head", "tail"], ["head", "tail", "not-persisted"]])
def test_optional_memory_only_all_uuids_never_becomes_a_parent(all_uuids):
    rows = topology()
    metadata = rows[3]["compactMetadata"]["preservedMessages"]
    if all_uuids is None:
        del metadata["allUuids"]
    else:
        metadata["allUuids"] = all_uuids
    assert uuids(select(rows))[-1] == "after"


def test_preserved_segment_walk_only():
    rows = topology()
    del rows[3]["compactMetadata"]["preservedMessages"]
    selection = select(rows)
    assert selection.preserved_indices == (1, 2)
    assert "preservation_walk" in selection.diagnostics


def test_latest_of_two_compactions_replaces_first_preservation():
    rows = topology()
    rows.extend(
        [
            desktop("user", "second-kept", "after", "kept by second"),
            {
                **copy.deepcopy(rows[3]),
                "uuid": "boundary2",
                "compactMetadata": {
                    "preservedMessages": {"anchorUuid": "summary2", "uuids": ["second-kept"]}
                },
            },
            desktop("user", "summary2", "boundary2", "second summary", isCompactSummary=True),
            desktop("assistant", "final", "summary2", "final"),
        ]
    )
    selection = select(rows)
    assert uuids(selection) == ["boundary2", "summary2", "second-kept", "final"]
    assert selection.boundary_index == 8
    assert set(selection.retired_indices) == set(range(7))


def test_later_boundary_without_preservation_fails_closed():
    rows = topology()
    rows.append({**copy.deepcopy(rows[3]), "uuid": "boundary2", "compactMetadata": {}})
    with pytest.raises(SessionMigrateError, match="later boundary"):
        select(rows)


def test_implicit_checkpoint_before_final_reply_cannot_trim_reply():
    rows = topology()
    rows.extend(
        [
            desktop("user", "request", "after", "request"),
            {"type": "last-prompt", "leafUuid": "request"},
            desktop("assistant", "final", "request", "final reply"),
        ]
    )
    assert uuids(select(rows))[-1] == "final"


@pytest.mark.parametrize("flag", ["explicit", "rewound"])
def test_effective_explicit_checkpoint_and_rewind_are_rejected(flag):
    rows = topology()
    rows.append({"type": "last-prompt", "leafUuid": "summary", flag: True})
    with pytest.raises(SessionMigrateError, match="explicit or rewind"):
        select(rows)


def test_explicit_clear_is_rejected():
    rows = topology() + [{"type": "last-prompt", "leafUuid": None, "explicit": True}]
    with pytest.raises(SessionMigrateError, match="explicit clear"):
        select(rows)


def test_stale_explicit_checkpoint_is_cleared_by_subsequent_native_row():
    rows = topology()
    rows.extend(
        [
            {"type": "last-prompt", "leafUuid": "after", "explicit": True},
            desktop("user", "request", "after", "new request"),
        ]
    )
    assert uuids(select(rows))[-1] == "request"


def test_fork_briefing_metadata_does_not_clear_effective_explicit_checkpoint():
    rows = topology() + [{"type": "last-prompt", "leafUuid": "after", "explicit": True}]
    rows.append(
        {
            **copy.deepcopy(rows[4]),
            "uuid": "briefing",
            "parentUuid": "after",
            "attachment": {"type": "fork_briefing"},
        }
    )
    with pytest.raises(SessionMigrateError, match="effective explicit"):
        select(rows)


def test_native_implicit_checkpoint_prunes_other_branch_without_leaking_it():
    rows = topology()
    rows.extend(
        [
            desktop("assistant", "branch", "summary", "other branch"),
            {"type": "last-prompt", "leafUuid": "after"},
        ]
    )
    selection = select(rows)
    assert uuids(selection)[-1] == "after"
    assert 7 in selection.excluded_indices
    assert 7 not in selection.retired_indices


def test_native_last_row_prunes_other_branch_with_stale_implicit_checkpoint():
    rows = topology()
    rows.extend(
        [
            desktop("assistant", "branch", "summary", "other branch"),
            {"type": "last-prompt", "leafUuid": "summary"},
            desktop("user", "final", "after", "latest branch"),
        ]
    )
    assert uuids(select(rows))[-2:] == ["after", "final"]
    assert "branch" not in uuids(select(rows))


def test_native_checkpoint_candidate_remains_preferred_with_metadata_sibling_leaf():
    # Ko calls rPt with direct=False here: a plain descendant through metadata
    # does not displace a checkpoint that remains in its native leaf set.
    rows = topology()
    rows[6] = desktop("user", "A", "tail", "checkpoint request")
    rows.extend(
        [
            {**copy.deepcopy(rows[4]), "uuid": "X", "parentUuid": "A"},
            {"type": "last-prompt", "leafUuid": "A"},
            {**copy.deepcopy(rows[4]), "uuid": "Y", "parentUuid": "A"},
            desktop("assistant", "B", "Y", "descendant response"),
        ]
    )
    selection = select(rows)
    assert uuids(selection)[-1] == "A"
    assert "B" not in uuids(selection)
    assert 10 in selection.excluded_indices
    assert "native_leaf_pruned" in selection.diagnostics


def test_native_batch_sensitive_checkpoint_pruning_is_refused():
    rows = topology()
    rows[1]["message"]["id"] = "batch"
    rows[1]["message"]["content"] = [
        {"type": "tool_use", "id": call, "name": "Read", "input": {}}
        for call in ("first", "second")
    ]
    rows[2]["message"]["content"] = [
        {"type": "tool_result", "tool_use_id": "first", "content": "first result"}
    ]
    rows[6] = {**copy.deepcopy(rows[4]), "uuid": "X", "parentUuid": "tail"}
    rows.extend(
        [
            {"type": "last-prompt", "leafUuid": "tail"},
            desktop(
                "user",
                "B",
                "head",
                [{"type": "tool_result", "tool_use_id": "second", "content": "second result"}],
            ),
        ]
    )
    with pytest.raises(SessionMigrateError, match="cross-chain tool recovery"):
        select(rows)


def test_native_post_boundary_message_with_retired_parent_uses_kept_tail():
    rows = topology()
    rows[6]["parentUuid"] = "before"
    selection = select(rows)
    assert selection.effective_parents[6] == 2
    assert rows[6]["parentUuid"] == "before"


def test_child_before_parent_permutation_keeps_effective_order():
    rows = topology()
    rows.extend(
        [
            desktop("assistant", "final", "request", "answer"),
            desktop("user", "request", "after", "request"),
        ]
    )
    assert uuids(select(rows))[-3:] == ["after", "request", "final"]


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda r: r[3]["compactMetadata"]["preservedMessages"].update(uuids=[]), "malformed"),
        (
            lambda r: r[3]["compactMetadata"]["preservedMessages"].update(uuids=["head", "head"]),
            "duplicate",
        ),
        (
            lambda r: r[3]["compactMetadata"]["preservedMessages"].update(uuids=["missing"]),
            "allUuids",
        ),
        (lambda r: r[3]["compactMetadata"]["preservedMessages"].pop("uuids"), "malformed"),
        (
            lambda r: r[3]["compactMetadata"]["preservedMessages"].update(allUuids=["head"]),
            "allUuids",
        ),
        (
            lambda r: r[3]["compactMetadata"]["preservedMessages"].update(allUuids="head"),
            "malformed",
        ),
        (
            lambda r: r[3]["compactMetadata"]["preservedMessages"].update(anchorUuid="after"),
            "summary",
        ),
        (lambda r: r[4].update(parentUuid="after"), "cycle"),
        (lambda r: r[4].update(parentUuid="interposed"), "cycle"),
        (lambda r: r[6].update(parentUuid="after"), "cycle"),
        (lambda r: r[6].update(parentUuid="unpersisted"), "missing parent UUID"),
        (lambda r: r[2].update(sessionId="different"), "mixed session"),
        (lambda r: r[2].update(isSidechain=True), "sidechain"),
        (lambda r: r[2].update(agentId="agent"), "subagent"),
        (lambda r: r[2].update(version="2.1.318"), "identity"),
    ],
)
def test_recognized_invalid_topology_is_rejected_without_content_leak(mutate, reason):
    rows = topology()
    mutate(rows)
    original = copy.deepcopy(rows)
    with pytest.raises(SessionMigrateError, match=reason) as error:
        select(rows)
    assert "preserved first" not in str(error.value)
    assert rows == original


def test_material_bridge_to_summary_is_not_guessed():
    rows = topology()
    rows[4] = desktop("user", "interposed", "boundary", "material bridge")
    with pytest.raises(SessionMigrateError, match="material bridge"):
        select(rows)


def test_wrong_boundary_anchor_is_rejected():
    rows = topology()
    rows[4]["parentUuid"] = "wrong-boundary"
    rows.insert(3, {**copy.deepcopy(rows[3]), "uuid": "wrong-boundary", "compactMetadata": {}})
    with pytest.raises(SessionMigrateError, match="material bridge"):
        select(rows)


def test_segment_cycle_and_missing_head_are_rejected():
    rows = topology()
    del rows[3]["compactMetadata"]["preservedMessages"]
    rows[1]["parentUuid"] = "tail"
    with pytest.raises(SessionMigrateError, match="cycle"):
        select(rows)
    rows[1]["parentUuid"] = "missing"
    rows[3]["compactMetadata"]["preservedSegment"]["headUuid"] = "before"
    with pytest.raises(SessionMigrateError, match="missing preservation UUID"):
        select(rows)


def test_missing_kept_uuid_is_not_recovered_from_memory_metadata():
    rows = topology()
    rows[3]["compactMetadata"]["preservedMessages"]["uuids"] = ["unpersisted"]
    with pytest.raises(SessionMigrateError, match="missing preservation UUID"):
        select(rows)


def test_in_chain_complete_and_open_tools_keep_source_order():
    rows = topology()
    rows[1]["message"]["content"] = [
        {"type": "tool_use", "id": "call", "name": "Read", "input": {}}
    ]
    rows[2]["message"]["content"] = [
        {"type": "tool_result", "tool_use_id": "call", "content": "ok"}
    ]
    rows[6]["message"]["content"] = [
        {"type": "tool_use", "id": "open", "name": "Read", "input": {}}
    ]
    assert uuids(select(rows))[-3:] == ["head", "tail", "after"]
    assert rows[6]["message"]["content"][0]["type"] == "tool_use"


def test_unrelated_branch_tool_result_is_never_resurrected_by_id():
    rows = topology()
    rows[1]["message"]["content"] = [
        {"type": "tool_use", "id": "call", "name": "Read", "input": {}}
    ]
    rows.append(
        desktop(
            "user",
            "unrelated-result",
            "summary",
            [{"type": "tool_result", "tool_use_id": "call", "content": "other branch"}],
        )
    )
    rows.append({"type": "last-prompt", "leafUuid": "after"})
    with pytest.raises(SessionMigrateError, match="cross-chain tool recovery"):
        select(rows)


def test_cross_chain_assistant_batch_is_explicitly_unsupported():
    rows = topology()
    rows[6]["message"]["id"] = "batch"
    rows.append(desktop("assistant", "sibling", "summary", "sibling"))
    rows[-1]["message"]["id"] = "batch"
    rows.append({"type": "last-prompt", "leafUuid": "after"})
    with pytest.raises(SessionMigrateError, match="cross-chain assistant batch"):
        select(rows)


def test_other_version_and_no_preservation_decline_without_changing_reader():
    assert select([desktop()]) is None
    rows = topology()
    for row in rows:
        row["version"] = "2.1.318"
    assert select(rows) is None


def test_retired_implicit_checkpoint_is_ignored_but_unknown_checkpoint_is_refused():
    rows = topology() + [{"type": "last-prompt", "leafUuid": "before"}]
    assert uuids(select(rows))[-1] == "after"
    rows[-1]["leafUuid"] = "never-persisted"
    with pytest.raises(SessionMigrateError, match="missing leaf UUID"):
        select(rows)


def test_meta_context_keeps_provenance_in_selected_chain():
    rows = topology()
    rows[2]["isMeta"] = True
    selection = select(rows)
    assert selection.selected_records[-2].index == 2
    assert selection.selected_records[-2].value["isMeta"] is True


@pytest.mark.parametrize("field", ["uuid", "type", "version", "sessionId", "parentUuid"])
def test_malformed_graph_fields_fail_with_safe_domain_error(field):
    rows = topology()
    rows[2][field] = {"SYNTHETIC_PRIVATE_SENTINEL": []}
    with pytest.raises(SessionMigrateError) as error:
        select(rows)
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("field", ["leafUuid", "sessionId", "version", "explicit", "rewound"])
def test_malformed_checkpoint_fields_fail_with_safe_domain_error(field):
    rows = topology() + [{"type": "last-prompt", "leafUuid": "after"}]
    rows[-1][field] = {"SYNTHETIC_PRIVATE_SENTINEL": []}
    with pytest.raises(SessionMigrateError) as error:
        select(rows)
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in str(error.value)
