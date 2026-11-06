"""Synthetic, flag-invariant Claude 2.1.309 batch recovery contracts.

The expected orders match the extracted native helpers with both flag values;
they do not claim stock wire or transport verification.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType, SimpleNamespace

import pytest
from test_claude_desktop309 import audit_rows
from test_claude_latest import _parse
from test_native_context_projection import parts, prepare
from test_projections import ID, observed

from session_migrate import operations
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.formats.claude_carriers import plan_carriers
from session_migrate.formats.claude_compaction import (
    POLICY_ID,
    RECOVERY_POLICY_ID,
    is_native_context_policy,
    select_compaction,
)
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_namespaces import reserve_namespace
from session_migrate.projection_policies import NATIVE_CONTEXT_POLICY_ID
from session_migrate.projections import verify_projection


def row(kind, uid, parent, content=None, **fields):
    value = {
        "type": kind,
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": "synthetic-session",
        "version": "2.1.309",
        "isSidechain": False,
        "timestamp": "2026-11-06T12:00:00.000Z",
    }
    if kind in {"user", "assistant"}:
        value["message"] = {
            "role": kind,
            "content": [{"type": "text", "text": uid}] if content is None else content,
        }
    elif content is not None:
        value["content"] = content
    value.update(fields)
    return value


def assistant(uid, parent="U", call=None, batch="batch"):
    content = (
        None
        if call is None
        else [{"type": "tool_use", "id": call, "name": "synthetic_tool", "input": {}}]
    )
    value = row("assistant", uid, parent, content)
    value["message"]["id"] = batch
    return value


def result(uid, parent, call):
    return row(
        "user",
        uid,
        parent,
        [{"type": "tool_result", "tool_use_id": call, "content": "synthetic result " + call}],
    )


def batch_rows(*, tools=False, continuation=True, linear=False):
    rows = [
        row("user", "U", None),
        row(
            "system",
            "B",
            None,
            subtype="compact_boundary",
            compactMetadata={"preservedMessages": {"anchorUuid": "S", "uuids": ["U"]}},
        ),
        row("user", "S", "B", isCompactSummary=True),
        assistant("A1", call="t1" if tools else None),
        assistant("A2", "A1" if linear else "U", "t2" if tools else None),
    ]
    if tools:
        rows.extend([result("R1", "A1", "t1"), result("R2", "A2", "t2")])
    if continuation:
        rows.append(assistant("Z", "R2" if tools else "A1", batch="next"))
    return rows


def select(rows):
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    return records, select_compaction(records)


def ids(records):
    return [record.value["uuid"] for record in records]


@pytest.mark.parametrize(
    "tools,continuation,expected,chain,leaf",
    [
        (False, True, ["B", "S", "U", "A1", "A2", "Z"], ["B", "S", "U", "A1", "Z"], "Z"),
        (False, False, ["B", "S", "U", "A2", "A1"], ["B", "S", "U", "A2"], "A2"),
        (
            True,
            True,
            ["B", "S", "U", "A2", "A1", "R1", "R2", "Z"],
            ["B", "S", "U", "A2", "R2", "Z"],
            "Z",
        ),
    ],
)
def test_recovery_keeps_native_anchor_order_and_original_envelopes(
    tools, continuation, expected, chain, leaf
):
    rows = batch_rows(tools=tools, continuation=continuation)
    before = copy.deepcopy(rows)
    records, selection = select(rows)
    assert ids(selection.selected_records) == expected
    assert selection.policy_id == RECOVERY_POLICY_ID
    assert ids([records[index] for index in selection.parent_chain_indices]) == chain
    assert records[selection.original_leaf_index].value["uuid"] == leaf
    assert ids([records[index] for index in selection.recovered_indices]) == [
        uid for uid in expected if uid not in chain
    ]
    assert rows == before
    assert all(record is records[record.index] for record in selection.selected_records)
    assert selection.effective_parents[4] == (3 if rows[4]["parentUuid"] == "A1" else 0)
    assert set(selection.recovery_provenance) == set(selection.recovered_indices)
    with pytest.raises(TypeError):
        selection.recovery_provenance[4] = ("wrong", 0)
    with pytest.raises(FrozenInstanceError):
        selection.original_leaf_index = 0


def test_timestamp_order_does_not_replace_native_batch_anchor_order():
    rows = batch_rows(tools=True)
    for offset, value in enumerate(rows):
        value["timestamp"] = f"2026-11-06T12:00:{59 - offset:02d}.000Z"
    _, selection = select(rows)
    assert ids(selection.selected_records) == ["B", "S", "U", "A2", "A1", "R1", "R2", "Z"]


def test_kpo_restores_unique_metadata_tail_on_recovered_result():
    rows = batch_rows(tools=True)
    rows.insert(6, row("system", "M1", "R1", subtype="local_command", content="synthetic meta"))
    records, selection = select(rows)
    assert ids(selection.selected_records) == ["B", "S", "U", "A2", "A1", "R1", "M1", "R2", "Z"]
    assert ids(plan_carriers(records, selection).replay_records) == ids(selection.selected_records)
    assert selection.recovery_provenance[6] == ("batch_metadata_tail", 5)


def test_fpo_uses_original_leaf_instead_of_recovered_last_assistant():
    rows = batch_rows(continuation=False)
    rows.append(
        row(
            "system",
            "M1",
            "A2",
            content="duration",
            subtype="turn_duration",
            durationMs=1,
            messageCount=1,
        )
    )
    records, selection = select(rows)
    assert ids(selection.selected_records) == ["B", "S", "U", "A2", "A1"]
    assert selection.original_leaf_index == 4
    assert ids(plan_carriers(records, selection).replay_records) == [
        "B",
        "S",
        "U",
        "A2",
        "A1",
        "M1",
    ]


@pytest.mark.parametrize("ever_explicit", [False, True])
def test_native_checkpoint_batch_pruning_precedes_recovery(ever_explicit):
    rows = batch_rows(tools=True, linear=True)
    rows.insert(6, {"type": "last-prompt", "leafUuid": "R1", "explicit": ever_explicit})
    records, selection = select(rows)
    assert records[selection.original_leaf_index].value["uuid"] == ("R1" if ever_explicit else "Z")
    expected = ["B", "S", "U", "A1", "A2", "R1", "R2"]
    assert ids(selection.selected_records) == (expected if ever_explicit else [*expected, "Z"])


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda rows: rows[4].update(uuid="A1"), "duplicate"),
        (lambda rows: rows[4].pop("uuid"), "missing graph"),
        (lambda rows: rows[4].update(sessionId="other"), "mixed session"),
        (lambda rows: rows[4]["message"].update(id={}), "batch identity"),
        (lambda rows: rows[3].update(parentUuid="B"), "batch identity"),
        (lambda rows: rows[5].update(sourceToolAssistantUUID="A2"), "assistant identity"),
        (
            lambda rows: rows[5]["message"]["content"][0].update(tool_use_id="absent"),
            "causal ordering",
        ),
        (lambda rows: rows[4]["message"]["content"][0].update(id="t1"), "ambiguous"),
        (
            lambda rows: rows[5]["message"]["content"].append(
                copy.deepcopy(rows[5]["message"]["content"][0])
            ),
            "duplicate tool",
        ),
        (lambda rows: rows[3]["message"]["content"][0].pop("id"), "tool identity"),
    ],
)
def test_invalid_recovery_identity_and_causality_fail_closed(mutation, reason):
    rows = batch_rows(tools=True)
    mutation(rows)
    before = copy.deepcopy(rows)
    with pytest.raises(SessionMigrateError, match=reason):
        select(rows)
    assert rows == before


@pytest.mark.parametrize("source", [False, True])
def test_flag_dependent_off_parent_recovery_is_refused(source):
    rows = batch_rows(tools=True)
    rows[5]["parentUuid"] = "U"
    if source:
        rows[5]["sourceToolAssistantUUID"] = "A1"
    with pytest.raises(SessionMigrateError, match="cross-chain tool recovery"):
        select(rows)


def test_duplicate_results_on_another_branch_remain_unsupported():
    rows = batch_rows(tools=True)
    rows.insert(6, result("duplicate", "A1", "t1"))
    with pytest.raises(SessionMigrateError, match="causal ordering"):
        select(rows)


def test_physical_result_before_its_recovered_call_is_refused():
    rows = batch_rows(tools=True)
    rows[3], rows[5] = rows[5], rows[3]
    with pytest.raises(SessionMigrateError, match="causal ordering"):
        select(rows)


def test_parser_preserves_source_bytes_usage_version_and_unknown_authority(tmp_path):
    rows = batch_rows(tools=True)
    rows[3]["message"]["usage"] = {"input_tokens": 123, "output_tokens": 456}
    original = copy.deepcopy(rows)
    session = _parse(rows)
    assert session.source_sha256 == hashlib.sha256(encode_jsonl(rows)).hexdigest()
    assert session.raw_record_count == len(rows)
    audit = audit_rows(rows, tmp_path)
    assert audit.coverage is Coverage.UNKNOWN
    assert audit.client_id is None
    assert "invalid_graph" not in dict(audit.issue_counts)
    assert "unsupported_client_version" in dict(audit.issue_counts)
    assert rows == original


@pytest.mark.parametrize(
    "field", ["original_leaf_index", "parent_chain_indices", "recovery_provenance"]
)
def test_raw_audit_binds_recovered_chain_provenance(field, monkeypatch, tmp_path):
    rows = batch_rows(tools=True)
    baseline = audit_rows(rows, tmp_path)
    original = claude.select_compaction

    def altered(records):
        selection = original(records)
        changes = {
            "original_leaf_index": selection.parent_chain_indices[-2],
            "parent_chain_indices": selection.parent_chain_indices[:-1],
            "recovery_provenance": MappingProxyType(
                {
                    index: ("different_proof", owner)
                    for index, (_, owner) in selection.recovery_provenance.items()
                }
            ),
        }
        if field == "original_leaf_index":
            # Keep the carrier's leaf consistency guard satisfied while proving
            # that the audit binds which Ko leaf and parentwalk were used.
            return replace(
                selection,
                **{field: changes[field]},
                parent_chain_indices=selection.parent_chain_indices[:-1],
            )
        if field == "parent_chain_indices":
            changes[field] = selection.parent_chain_indices[1:]
        return replace(selection, **{field: changes[field]})

    monkeypatch.setattr(claude, "select_compaction", altered)
    changed = audit_rows(rows, tmp_path)
    assert "invalid_graph" not in dict(changed.issue_counts)
    assert changed.structure != baseline.structure


def test_recovery_policy_is_exact_and_legacy_selection_keeps_v1():
    rows = batch_rows()
    rows[4]["message"]["id"] = "other-batch"
    assert select(rows)[1].policy_id == POLICY_ID
    assert is_native_context_policy(POLICY_ID)
    assert is_native_context_policy(RECOVERY_POLICY_ID)
    for unknown in (
        None,
        {},
        [],
        "claude-2.1.309-native-latest-preserved-v3",
        RECOVERY_POLICY_ID + "x",
    ):
        assert not is_native_context_policy(unknown)


def test_checkpoint_through_batch_without_added_records_requires_v2():
    rows = batch_rows(tools=True, continuation=False, linear=True)
    rows = [value for value in rows if value["uuid"] not in {"R1", "R2"}]
    rows[4]["parentUuid"] = "M"
    rows.insert(4, row("system", "M", "A1", subtype="turn_duration"))
    sibling = row("system", "X", "A1", subtype="turn_duration")
    sibling.pop("isSidechain")  # Native Pco's strict scope check excludes this tail.
    rows.insert(5, sibling)
    rows.append({"type": "last-prompt", "leafUuid": "A1"})
    rows.append(assistant("Z", "A2", batch="next"))
    records, selection = select(rows)
    assert records[selection.original_leaf_index].value["uuid"] == "Z"
    assert not selection.recovered_indices
    assert "native_checkpoint_through_batch" in selection.diagnostics
    assert selection.policy_id == RECOVERY_POLICY_ID


@pytest.mark.parametrize(
    "control", ["prompt_snapshot", "prompt_render_point", "deferred_tools_record"]
)
def test_proven_retired_control_requires_v2(control):
    rows = batch_rows()
    rows[4]["message"]["id"] = "other-batch"
    rows.insert(0, row("attachment", "old", None, attachment={"type": control}))
    records, selection = select(rows)
    assert not selection.recovered_indices
    assert selection.retired_control_indices == (0,)
    assert selection.policy_id == RECOVERY_POLICY_ID
    assert 0 not in plan_carriers(records, selection).replay_indices


@pytest.mark.parametrize(
    "target,version",
    [
        (TargetFormat.CODEX, "0.144.4"),
        (TargetFormat.CLAUDE, "2.1.209"),
        (TargetFormat.OPENCODE, "2.1.1"),
    ],
)
def test_recovery_policy_keeps_exact_opencode225_target_gate(tmp_path, target, version):
    session = _parse(batch_rows())
    assert any(
        event.payload.get("native_context_policy") == RECOVERY_POLICY_ID for event in session.events
    )
    with pytest.raises(SessionMigrateError, match="exact OpenCode 2.2.5 target"):
        convert_session(
            session, ConversionOptions(target, cwd=tmp_path, target_cli_version=version)
        )


def test_recovered_text_projection_and_operation_policy_keep_unknown_authority(
    tmp_path, monkeypatch
):
    values = parts(tmp_path, batch_rows())
    prepared = prepare(values)
    assert prepared.expected.native_context_only
    assert verify_projection(prepared, observed(values[1]), context=values[-1]).semantic_matches
    assert audit_rows(batch_rows(), tmp_path).coverage is Coverage.UNKNOWN
    request = operations.OperationRequest(
        command=operations.OperationKind.IMPORT,
        target_format=TargetFormat.OPENCODE,
        target_cli_version="2.2.5",
        session_id=ID,
    )
    state = SimpleNamespace(
        built=SimpleNamespace(
            artifact=values[1],
            source_snapshot=values[0],
            request=request,
        )
    )
    monkeypatch.setattr(operations, "_profile_fingerprint", lambda *args: "a" * 64)
    monkeypatch.setattr(
        operations,
        "_native_namespace",
        lambda state: reserve_namespace(
            AgentFormat.OPENCODE,
            tmp_path / "target",
            values[1].session_id,
        ),
    )
    result, versions, context = operations._prepare_lineage_projection(state)
    assert (
        versions.projection_policy == result.expected.policy.policy_id == NATIVE_CONTEXT_POLICY_ID
    )
    assert verify_projection(result, observed(values[1]), context=context).semantic_matches
