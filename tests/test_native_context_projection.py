"""Versioned, independent projection of bounded native historical context."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_claude_desktop309 import desktop
from test_claude_desktop309_compaction import topology
from test_incomplete_tool_projection import bundle_tools, changed_artifact
from test_projections import ID, claude_records, observed, snapshot, tool_records

from session_migrate import operations
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats.context_text import is_synthetic_text_context
from session_migrate.lineage import Coverage, PolicyVersions
from session_migrate.model import AgentFormat, EventKind, Role, TargetFormat
from session_migrate.native_namespaces import reserve_namespace
from session_migrate.projection_policies import (
    INCOMPLETE_TOOL_MESSAGE,
    NATIVE_CONTEXT_ARCHIVE_POLICY_ID,
    NATIVE_CONTEXT_POLICY_ID,
    OPENCODE_ARCHIVE_POLICY_ID,
    PORTABLE_POLICY_ID,
    ProjectionError,
    ProjectionPolicy,
    archived_tools_match,
    expected_from_source,
    policy_from_id,
    validate_policy_target,
)
from session_migrate.projections import (
    ProjectionContext,
    _observe,
    prepare_projection,
    validate_prepared,
    verify_projection,
)

STRICT = ProjectionPolicy(NATIVE_CONTEXT_POLICY_ID)
ARCHIVE = ProjectionPolicy(NATIVE_CONTEXT_ARCHIVE_POLICY_ID)


def context_records(text="  histórico\nUnicode: ação \u2028  ", *, open_call=False):
    rows = topology()
    rows[1]["type"] = rows[1]["message"]["role"] = "user"
    rows[1]["message"]["content"] = "real user before context"
    rows[2]["isMeta"] = True
    rows[2]["message"]["content"] = text
    rows[6]["message"]["content"] = [
        {"type": "tool_use", "id": "native-call", "name": "Read", "input": {"n": 1}}
    ]
    parent = "after"
    if not open_call:
        rows.append(
            desktop(
                "user",
                "result",
                parent,
                [{"type": "tool_result", "tool_use_id": "native-call", "content": "result"}],
            )
        )
        parent = "result"
    rows.append(desktop("assistant", "final", parent, "final answer"))
    return rows


def parts(tmp_path, rows=None, *, policy=STRICT):
    source = snapshot(context_records() if rows is None else rows)
    options = ConversionOptions(
        TargetFormat.OPENCODE, session_id=ID, cwd=tmp_path, target_cli_version="2.2.5"
    )
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        policy.policy_id,
        "native-audit-v1",
        "native-audit-v1",
        artifact.source.cli_version,
        "2.2.5",
    )
    return source, artifact, options, versions, ProjectionContext(TargetFormat.OPENCODE, "2.2.5")


def prepare(values, *, policy=STRICT):
    source, artifact, options, versions, context = values
    return prepare_projection(
        source, artifact, options=options, versions=versions, context=context, policy=policy
    )


@pytest.mark.parametrize("text", ["", "  histórico\nUnicode: ação \u2028  "])
def test_context_summary_and_tools_preserve_exact_order_without_authority(tmp_path, text):
    values = parts(tmp_path, context_records(text))
    before = values[0].raw_bytes
    prepared = prepare(values)
    expected = prepared.expected
    assert [(e.kind, e.text) for e in expected.events] == [
        (EventKind.CONTEXT, "source summary"),
        (EventKind.MESSAGE, "real user before context"),
        (EventKind.CONTEXT, text),
        (EventKind.TOOL_CALL, None),
        (EventKind.TOOL_RESULT, "result"),
        (EventKind.MESSAGE, "final answer"),
    ]
    context = expected.events[2]
    assert context.role is Role.USER and context.payload == {"block_type": "synthetic_text"}
    native = json.loads(values[1].native_bytes)["messages"]
    assert [message["type"] for message in native] == [
        "synthetic",
        "user",
        "synthetic",
        "assistant",
        "assistant",
    ]
    assert native[2]["text"] == text
    result = verify_projection(prepared, observed(values[1]), context=values[-1])
    assert result.semantic_matches and result.structure_matches
    assert result.source_coverage is result.expectation_coverage is Coverage.UNKNOWN
    assert not result.relation_complete and not result.preserves_content
    assert not result.delta_eligible and not result.identity_proven and not result.boundary_proven
    assert prepared.certificate is None and not expected.generated_members
    assert values[0].raw_bytes == before
    validate_prepared(prepared)


def test_expectation_is_independent_and_old_policy_explicitly_omits_context(tmp_path, monkeypatch):
    values = parts(tmp_path)
    source = _observe(values[0], "2.1.309", STRICT)

    def forbidden(*args, **kwargs):
        pytest.fail("independent expectation called the writer")

    monkeypatch.setattr("session_migrate.formats.opencode_v2.from_legacy", forbidden)
    expected = expected_from_source(source, target_format=TargetFormat.OPENCODE, policy=STRICT)
    assert any(is_synthetic_text_context(event) for event in expected.events)
    old = expected_from_source(source, target_format=TargetFormat.OPENCODE)
    assert not any(is_synthetic_text_context(event) for event in old.events)
    source_index = next(
        i for i, event in enumerate(source.events) if is_synthetic_text_context(event)
    )
    assert old.members[source_index].transform == "omit"
    assert "source_event_omitted" in old.members[source_index].losses


@pytest.mark.parametrize("tamper", ["user", "missing_marker", "revision", "extra_marker", "text"])
def test_changed_context_or_marker_fails_preparation_and_verification(tmp_path, tamper):
    values = parts(tmp_path)
    prepared = prepare(values)

    def change(bundle):
        context = next(message for message in bundle["messages"] if message["type"] == "synthetic")
        if tamper == "user":
            context["type"] = "user"
            del context["metadata"]
        elif tamper == "missing_marker":
            del context["metadata"]
        elif tamper == "revision":
            context["metadata"]["session_migrate_context"]["version"] = 2
        elif tamper == "extra_marker":
            context["metadata"]["extra"] = True
        else:
            context["text"] += "altered"

    changed = changed_artifact(values[1], change)
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], changed, *values[2:]))
    assert not verify_projection(prepared, observed(changed), context=values[-1]).semantic_matches


def test_source_and_options_are_bound(tmp_path):
    values = parts(tmp_path)
    changed_rows = context_records("different source context")
    with pytest.raises(ProjectionError, match="source_mismatch"):
        prepare((snapshot(changed_rows), *values[1:]))
    changed_options = replace(values[2], session_id="60000000-0000-4000-8000-000000000001")
    with pytest.raises(ProjectionError, match="options_mismatch"):
        prepare((*values[:2], changed_options, *values[3:]))


def test_strict_refuses_open_call_and_archive_binds_only_generated_errors(tmp_path):
    values = parts(tmp_path, context_records(open_call=True))
    with pytest.raises(ProjectionError, match="source_tools_unproven"):
        prepare(values)
    values = parts(tmp_path, context_records(open_call=True), policy=ARCHIVE)
    prepared = prepare(values, policy=ARCHIVE)
    expected = prepared.expected
    assert len(expected.generated_members) == 1
    assert dict(expected.loss_counts)["incomplete_tool_archived"] == 1
    assert any(is_synthetic_text_context(event) for event in expected.events)
    normal = {member.target_index for member in expected.members if member.target_index is not None}
    generated = {member.target_index for member in expected.generated_members}
    assert normal.isdisjoint(generated)
    assert normal | generated == set(range(len(expected.events)))
    assert expected.relation.count == len(prepared.source_observation.events)
    result = verify_projection(prepared, observed(values[1]), context=values[-1])
    assert result.semantic_matches and result.structure_matches
    assert result.expectation_coverage is Coverage.UNKNOWN and prepared.certificate is None
    assert not result.preserves_content and not result.delta_eligible

    def change(bundle):
        bundle_tools(bundle)[0]["state"]["error"]["type"] = "ToolError"

    changed = changed_artifact(values[1], change)
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], changed, *values[2:]), policy=ARCHIVE)
    assert not verify_projection(prepared, observed(changed), context=values[-1]).semantic_matches


@pytest.mark.parametrize("policy", [STRICT, ARCHIVE])
@pytest.mark.parametrize("defect", ["missing_name", "duplicate_call", "orphan_result"])
def test_native_context_policies_keep_tool_ambiguity_guards(tmp_path, policy, defect):
    rows = context_records()
    if defect == "missing_name":
        del rows[6]["message"]["content"][0]["name"]
    elif defect == "duplicate_call":
        rows[6]["message"]["content"].append(copy.deepcopy(rows[6]["message"]["content"][0]))
    else:
        rows[7]["message"]["content"][0]["tool_use_id"] = "unknown-call"
        with pytest.raises(SessionMigrateError, match="native capture parsing failed"):
            parts(tmp_path, rows, policy=policy)
        return
    with pytest.raises(ProjectionError, match="source_tools_unproven"):
        prepare(parts(tmp_path, rows, policy=policy), policy=policy)


@pytest.mark.parametrize("policy", [STRICT, ARCHIVE])
def test_completed_same_text_error_cannot_forge_generated_archive_marker(tmp_path, policy):
    rows = tool_records(error=True)
    rows[2]["message"]["content"][0]["content"] = INCOMPLETE_TOOL_MESSAGE
    values = parts(tmp_path, rows, policy=policy)
    prepared = prepare(values, policy=policy)
    assert not prepared.expected.generated_members

    def change(bundle):
        bundle_tools(bundle)[0]["state"]["error"]["type"] = "ImportedIncompleteTool"

    changed = changed_artifact(values[1], change)
    assert not archived_tools_match(prepared.expected, observed(changed))
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], changed, *values[2:]), policy=policy)


@pytest.mark.parametrize("policy", [STRICT, ARCHIVE])
@pytest.mark.parametrize(
    "target,client",
    [
        (TargetFormat.OPENCODE, "2.1.1"),
        (TargetFormat.OPENCODE, "2.0.23"),
        (TargetFormat.CODEX, "0.144.4"),
        (TargetFormat.CLAUDE, "2.1.209"),
    ],
)
def test_new_registry_policies_require_exact_target(policy, target, client):
    assert policy_from_id(policy.policy_id) == policy
    with pytest.raises(ProjectionError, match="unsupported_policy"):
        validate_policy_target(policy, target, client)


@pytest.mark.parametrize(
    "reviewed,open_call,context,expected_id",
    [
        (False, False, True, NATIVE_CONTEXT_POLICY_ID),
        (False, False, "summary", NATIVE_CONTEXT_POLICY_ID),
        (False, True, True, None),
        (True, False, True, NATIVE_CONTEXT_POLICY_ID),
        (True, True, True, NATIVE_CONTEXT_ARCHIVE_POLICY_ID),
        (False, False, False, PORTABLE_POLICY_ID),
        (True, True, False, OPENCODE_ARCHIVE_POLICY_ID),
    ],
)
def test_operations_select_policy_from_raw_source_and_reviewed_opt_in(
    tmp_path, monkeypatch, reviewed, open_call, context, expected_id
):
    rows = context_records(open_call=open_call) if context else tool_records()
    if context == "summary":
        rows[2].pop("isMeta")
    if open_call and not context:
        rows[2]["message"]["content"] = []
    values = parts(tmp_path, rows)
    request = operations.OperationRequest(
        command=operations.OperationKind.IMPORT,
        target_format=TargetFormat.OPENCODE,
        target_cli_version="2.2.5",
        session_id=ID,
        reviewed_tree_projection=reviewed,
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
    if expected_id is None:
        with pytest.raises(ProjectionError, match="source_tools_unproven"):
            operations._prepare_lineage_projection(state)
        return
    prepared, versions, context = operations._prepare_lineage_projection(state)
    assert versions.projection_policy == prepared.expected.policy.policy_id == expected_id
    assert verify_projection(prepared, observed(values[1]), context=context).semantic_matches


@pytest.mark.parametrize(
    "name,expected_hash",
    [
        ("messages", "f3f82b8ab1d8e9f7cc30a92d42a88acc5f3746f4869f88a571039976426c8928"),
        ("tools", "629d9e1ac864bafdc39836e498ee45b9aee98457c8967029c14357616f9848df"),
        ("archive", "6c28dd1e91f696bc4003aa73873f49f64d643a4b74465c0df14a7a9a5106d601"),
    ],
)
def test_existing_policy_relation_hashes_remain_unchanged(name, expected_hash):
    rows = copy.deepcopy(claude_records() if name == "messages" else tool_records())
    policy = ProjectionPolicy(
        OPENCODE_ARCHIVE_POLICY_ID if name == "archive" else PORTABLE_POLICY_ID
    )
    if name == "archive":
        rows[2]["message"]["content"] = []
    source = _observe(snapshot(rows), "2.1.209", policy)
    expected = expected_from_source(source, target_format=TargetFormat.OPENCODE, policy=policy)
    assert expected.relation.chain_sha256 == expected_hash


def test_raw_native_summary_is_exact_even_when_legacy_projection_trims(tmp_path):
    rows = context_records()
    rows[5]["message"]["content"] = "  source summary\n"
    values = parts(tmp_path, rows)
    prepared = prepare(values)
    assert prepared.expected.native_context_hashes
    assert verify_projection(prepared, observed(values[1]), context=values[-1]).semantic_matches

    def trim_summary(bundle):
        next(m for m in bundle["messages"] if m["type"] == "synthetic")["text"] = "source summary"

    changed = changed_artifact(values[1], trim_summary)
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], changed, *values[2:]))
    assert not verify_projection(prepared, observed(changed), context=values[-1]).semantic_matches


@pytest.mark.parametrize("metadata", [{"keepID": "msg_missing"}, {"recent": ""}])
def test_new_replay_window_is_not_authorized_by_portable_summary_match(tmp_path, metadata):
    values = parts(tmp_path)
    prepared = prepare(values)

    def change(bundle):
        next(m for m in bundle["messages"] if m["type"] == "synthetic")["metadata"] = metadata

    changed = changed_artifact(values[1], change)
    assert not verify_projection(prepared, observed(changed), context=values[-1]).semantic_matches
