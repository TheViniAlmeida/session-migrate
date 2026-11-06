"""Only controls deleted by native upo may bypass the active control guard."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from types import MappingProxyType

import pytest
from test_claude_carriers import envelope, prepare, topology
from test_claude_latest import _observe, _parse

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude_carriers import plan_carriers
from session_migrate.jsonl import encode_jsonl
from session_migrate.model import EventKind

CONTROLS = (
    {"type": "prompt_snapshot", "systemPrompt": ["SYNTHETIC_RETIRED_CONTROL"]},
    {"type": "prompt_render_point", "cause": "set_model_system_prompt"},
    {
        "type": "deferred_tools_record",
        "entries": [{"name": "SyntheticTool", "description": "SYNTHETIC_RETIRED_CONTROL"}],
        "nameOnlyAnnouncements": ["SyntheticTool"],
        "strippedReferences": [],
        "toolInputCopies": [{"id": "synthetic-call", "copy": "wire"}],
    },
)


def retired_rows(control, *, descendant=False):
    rows = topology()
    rows[0] = envelope(
        "attachment", "old", "answer" if descendant else None, attachment=copy.deepcopy(control)
    )
    return rows


@pytest.mark.parametrize("control", CONTROLS, ids=lambda control: control["type"])
@pytest.mark.parametrize("descendant", [False, True])
def test_deleted_controls_never_reach_replay_even_with_final_descendant_parent(control, descendant):
    rows = retired_rows(control, descendant=descendant)
    before = encode_jsonl(rows)
    records, selection = prepare(rows)
    snapshots = copy.deepcopy(records)
    result = plan_carriers(records, selection)

    assert selection.retired_indices == (0,)
    assert 0 not in selection.effective_parents
    assert 0 not in result.replay_indices
    assert 0 not in result.context_text
    assert all(record is records[record.index] for record in result.replay_records)
    assert records == snapshots
    assert encode_jsonl(rows) == before


@pytest.mark.parametrize("control", CONTROLS, ids=lambda control: control["type"])
def test_parser_and_raw_audit_agree_and_keep_physical_accounting(control):
    rows = retired_rows(control)
    source = encode_jsonl(rows)
    session = _parse(rows)
    audit, observation = _observe(rows)

    assert observation.session.events == session.events
    assert observation.session.raw_record_count == session.raw_record_count == len(rows)
    assert session.source_sha256 == audit.authority_sha256 == hashlib.sha256(source).hexdigest()
    assert {atom.record_index for atom in audit.atoms} == set(range(len(rows)))
    assert not {"invalid_graph", "parser_rejected_source"} & dict(audit.issue_counts).keys()
    visible = [event.text for event in session.events if event.kind is not EventKind.OPAQUE]
    assert visible == [
        "historical summary",
        "preserved",
        "persisted carrier text",
        "ordinary human request",
        "answer",
    ]
    assert encode_jsonl(rows) == source


@pytest.mark.parametrize("control", CONTROLS, ids=lambda control: control["type"])
@pytest.mark.parametrize("position", ["preserved_preboundary", "active", "fpo", "excluded_live"])
def test_controls_which_remain_live_are_rejected(control, position):
    rows = topology()
    if position == "preserved_preboundary":
        rows = retired_rows(control)
        rows[2]["compactMetadata"]["preservedMessages"]["uuids"] = ["old", "kept"]
    elif position == "active":
        rows[4]["attachment"] = copy.deepcopy(control)
    else:
        parent = "answer" if position == "fpo" else "summary"
        rows.append(envelope("attachment", "control", parent, attachment=copy.deepcopy(control)))
    records, selection = prepare(rows)
    control_index = 0 if position == "preserved_preboundary" else 4 if position == "active" else 7
    assert control_index in selection.effective_parents
    assert control_index not in selection.retired_indices
    if position in {"fpo", "excluded_live"}:
        assert control_index in selection.excluded_indices
    with pytest.raises(SessionMigrateError, match="persisted prompt or tool control"):
        plan_carriers(records, selection)


@pytest.mark.parametrize(
    "forgery", ["retired", "live", "preserved", "boundary", "selected", "leaf"]
)
def test_forged_selection_cannot_hide_a_live_control(forgery):
    rows = topology()
    rows.append(envelope("attachment", "control", "summary", attachment=CONTROLS[0]))
    records, selection = prepare(rows)
    changes = {
        "retired": {"retired_indices": (*selection.retired_indices, 7)},
        "live": {
            "retired_indices": (*selection.retired_indices, 7),
            "effective_parents": MappingProxyType(
                {
                    index: parent
                    for index, parent in selection.effective_parents.items()
                    if index != 7
                }
            ),
        },
        "preserved": {"preserved_indices": (7, *selection.preserved_indices)},
        "boundary": {"boundary_index": 8, "retired_indices": (0, 7)},
        "selected": {"selected_indices": (*selection.selected_indices, 7)},
        "leaf": {"original_leaf_index": 7},
    }[forgery]
    with pytest.raises(SessionMigrateError, match="inconsistent compaction retirement selection"):
        plan_carriers(records, replace(selection, **changes))


def test_forged_preservation_cannot_retire_a_preboundary_live_control():
    rows = retired_rows(CONTROLS[0])
    rows[2]["compactMetadata"]["preservedMessages"]["uuids"] = ["old", "kept"]
    records, selection = prepare(rows)
    forged = replace(
        selection,
        preserved_indices=(1,),
        retired_indices=(0,),
        selected_records=tuple(
            record for record in selection.selected_records if record.index != 0
        ),
        selected_indices=tuple(index for index in selection.selected_indices if index != 0),
        effective_parents=MappingProxyType(
            {
                index: 3 if parent == 0 else parent
                for index, parent in selection.effective_parents.items()
                if index != 0
            }
        ),
    )
    with pytest.raises(SessionMigrateError, match="inconsistent compaction retirement selection"):
        plan_carriers(records, forged)
