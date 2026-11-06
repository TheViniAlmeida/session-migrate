"""PNG portability binds source bytes, native ownership and ordered subblocks."""

import copy
import json
from dataclasses import replace

import pytest
import test_tree_operations as tree_fixtures
from test_claude_reviewed_media_reader import image, media_rows, png, uri
from test_incomplete_tool_projection import changed_artifact
from test_projections import ID, observed, snapshot
from test_reviewed_portability_projection import parts as text_parts

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage, PolicyVersions
from session_migrate.model import EventKind, TargetFormat
from session_migrate.native_audit import AuditPolicy, AuditSelection, audit_snapshot
from session_migrate.projection_policies import (
    REVIEWED_MEDIA_POLICY_ID,
    REVIEWED_POLICY_ID,
    ProjectionError,
    ProjectionPolicy,
    _native_source_order,
    _native_target_order,
    expected_from_source,
)
from session_migrate.projections import (
    ProjectionContext,
    _expectation_sha,
    _observe,
    prepare_projection,
    reviewed_portability_report,
    validate_prepared,
    verify_projection,
)

tree_setup = tree_fixtures.setup

POLICY = ProjectionPolicy(REVIEWED_MEDIA_POLICY_ID)


def parts(tmp_path, blocks=None, records=None):
    source = snapshot(media_rows(blocks) if records is None else records)
    options = ConversionOptions(
        TargetFormat.OPENCODE,
        session_id=ID,
        cwd=tmp_path,
        target_cli_version="2.2.5",
        reviewed_portability=True,
    )
    artifact = convert_session(source.parse(reviewed_portability=True), options)
    versions = PolicyVersions(
        "canonical-v2", POLICY.policy_id, "native-audit-v1", "native-audit-v1", "2.1.309", "2.2.5"
    )
    return source, artifact, options, versions, ProjectionContext(TargetFormat.OPENCODE, "2.2.5")


def prepare(values, *, policy=POLICY):
    return prepare_projection(
        values[0],
        values[1],
        options=values[2],
        versions=values[3],
        context=values[4],
        policy=policy,
    )


def tools(bundle):
    return [p for m in bundle["messages"] for p in m.get("content", []) if p["type"] == "tool"]


@pytest.mark.parametrize(
    "blocks",
    [
        [image(png())],
        [
            {"type": "text", "text": " before ação\n  "},
            image(png()),
            {"type": "text", "text": "\n after\u2028"},
        ],
        [image(png()), image(png(raster=b"\0\x01\x02\x03"))],
    ],
)
def test_completed_png_is_real_native_media_with_exact_order_link_and_declared_limits(
    tmp_path, blocks
):
    values = parts(tmp_path, blocks)
    prepared = prepare(values)
    result = verify_projection(prepared, observed(values[1]), context=values[4])
    assert result.semantic_matches and result.structure_matches
    assert result.source_coverage is result.expectation_coverage is Coverage.UNKNOWN
    assert (
        not result.preserves_content and not result.delta_eligible and prepared.certificate is None
    )
    assert (
        prepared.source_observation.audit.selection_policy_id == "claude309-reviewed-inline-png-v1"
    )
    assert prepared.artifact_observation.audit.selection_policy_id == "native-active-v1"
    assert "reviewed_media_runtime_unproven" in dict(prepared.source_observation.audit.issue_counts)
    native = json.loads(values[1].native_bytes)
    tool = tools(native)[0]
    assert tool["id"] == "SYNTHETIC_IMAGE_CALL" and tool["name"] == "Read"
    assert tool["state"]["input"] == {"file_path": "/NEVER/READ/IMAGE"}
    assert [b["type"] for b in tool["state"]["content"]] == [
        "file" if b["type"] == "image" else "text" for b in blocks
    ]
    for source_block, target_block in zip(blocks, tool["state"]["content"], strict=True):
        if source_block["type"] == "image":
            assert target_block == {
                "type": "file",
                "mime": "image/png",
                "uri": "data:image/png;base64," + source_block["source"]["data"],
            }
        else:
            assert target_block == source_block
    report = reviewed_portability_report(prepared)
    assert report["policy_id"] == "claude-2.1.309-reviewed-inline-png-v1"
    assert report["projection_policy"] == REVIEWED_MEDIA_POLICY_ID
    assert report["inline_tool_result_images"]["count"] == sum(b["type"] == "image" for b in blocks)
    for code in [
        "tool_result_inline_png_transport",
        "chat_tool_images_relocated_to_user",
        "chat_tool_images_have_positional_association",
    ]:
        assert dict(result.loss_counts)[code] == report["inline_tool_result_images"]["count"]
        assert next(d for d in report["differences"] if d["code"] == code)["count"] > 0
    encoded = json.dumps(report)
    assert "data:image" not in encoded and "SYNTHETIC_IMAGE_CALL" not in encoded
    assert report["native_image_result_blocks"] and report["image_transport_binding_sha256"]
    with pytest.raises(ValueError):
        _native_source_order(prepared.source_observation.events, reviewed=True)
    with pytest.raises(ValueError):
        _native_target_order(native, set())


@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "mime",
        "missing",
        "extra",
        "order",
        "call_id",
        "input",
        "name",
        "error",
        "uri_url",
        "uri_file",
        "base64",
        "authority",
        "move_owner",
    ],
)
def test_changed_png_content_subblock_order_or_tool_ownership_fails_prepare_and_cold(
    tmp_path, mutation
):
    blocks = [
        {"type": "text", "text": "first"},
        image(png()),
        image(png(raster=b"\0\x01\x02\x03")),
        {"type": "text", "text": "last"},
    ]
    values = parts(tmp_path, blocks)
    prepared = prepare(values)

    def change(bundle):
        tool = tools(bundle)[0]
        content = tool["state"]["content"]
        if mutation == "bytes":
            content[1]["uri"] = uri(png(raster=b"\0\x05\x06\x07"))
        elif mutation == "mime":
            content[1]["mime"] = "image/jpeg"
        elif mutation == "missing":
            del content[1]
        elif mutation == "extra":
            content.append(copy.deepcopy(content[1]))
        elif mutation == "order":
            content[1], content[2] = content[2], content[1]
        elif mutation == "call_id":
            tool["id"] = "other-call"
        elif mutation == "input":
            tool["state"]["input"] = {"file_path": "changed"}
        elif mutation == "name":
            tool["name"] = "Other"
        elif mutation == "error":
            tool["state"]["status"] = "error"
            tool["state"]["error"] = {"type": "ToolError", "message": "fail"}
        elif mutation == "uri_url":
            content[1]["uri"] = "https://example.invalid/image.png"
        elif mutation == "uri_file":
            content[1]["uri"] = "file:///NEVER/READ/IMAGE"
        elif mutation == "base64":
            content[1]["uri"] = "data:image/png;base64,aW52YWxpZA=="
        elif mutation == "authority":
            bundle["info"]["permissions"] = [{"permission": "*", "pattern": "*", "action": "allow"}]
        elif mutation == "move_owner":
            second = copy.deepcopy(tool)
            second["id"] = "other-call"
            second["state"]["content"] = [content.pop(1)]
            next(m for m in bundle["messages"] if m["type"] == "assistant")["content"].append(
                second
            )

    changed = changed_artifact(values[1], change)
    with pytest.raises(SessionMigrateError):
        prepare((values[0], changed, *values[2:]))
    try:
        result = verify_projection(prepared, observed(changed), context=values[4])
    except SessionMigrateError:
        pass
    else:
        assert not result.semantic_matches


def test_media_policy_and_audit_selection_cannot_be_swapped_with_text_v19(tmp_path):
    values = parts(tmp_path)
    prepared = prepare(values)
    with pytest.raises(SessionMigrateError):
        audit_snapshot(
            values[0],
            AuditPolicy(client_version="2.1.309"),
            AuditSelection("claude309-reviewed-conversation-v1"),
        )
    with pytest.raises(SessionMigrateError):
        _observe(
            values[0], "2.1.309", ProjectionPolicy(REVIEWED_POLICY_ID), reviewed_portability=True
        )
    text = text_parts(tmp_path)
    with pytest.raises(SessionMigrateError):
        audit_snapshot(
            text[0],
            AuditPolicy(client_version="2.1.309"),
            AuditSelection("claude309-reviewed-inline-png-v1"),
        )
    with pytest.raises(ProjectionError):
        expected_from_source(
            prepared.source_observation,
            target_format=TargetFormat.OPENCODE,
            policy=ProjectionPolicy(REVIEWED_POLICY_ID),
        )
    with pytest.raises(ProjectionError):
        prepare((values[0], values[1], replace(values[2], reviewed_portability=False), *values[3:]))


def test_expectation_is_raw_source_derived_and_image_bindings_cannot_be_mutated(
    tmp_path, monkeypatch
):
    values = parts(tmp_path)
    source = _observe(values[0], "2.1.309", POLICY, reviewed_portability=True)

    def forbidden(*args, **kwargs):
        pytest.fail("expectation invoked writer")

    monkeypatch.setattr("session_migrate.formats.opencode_v2.from_legacy", forbidden)
    expected = expected_from_source(source, target_format=TargetFormat.OPENCODE, policy=POLICY)
    assert expected.reviewed_media_results
    changed = replace(expected, reviewed_media_results=())
    assert _expectation_sha(changed) != _expectation_sha(expected)
    prepared = prepare(values)
    actual_result = next(e for e in values[1].source.events if e.kind is EventKind.TOOL_RESULT)
    actual_result.payload["content_blocks"][0]["image_url"] = uri(png(raster=b"\0\x01\x02\x03"))
    with pytest.raises(ProjectionError, match="binding_changed"):
        validate_prepared(prepared)


def test_image_cannot_move_between_two_existing_completed_calls(tmp_path):
    records = media_rows()
    records[1]["message"]["content"].append(
        {
            "type": "tool_use",
            "id": "SECOND_IMAGE_CALL",
            "name": "Read",
            "input": {"file_path": "/NEVER/READ/SECOND"},
        }
    )
    records[2]["message"]["content"].append(
        {
            "type": "tool_result",
            "tool_use_id": "SECOND_IMAGE_CALL",
            "content": [image(png(raster=b"\0\x01\x02\x03"))],
        }
    )
    values = parts(tmp_path, records=records)
    prepared = prepare(values)
    assert len(prepared.expected.reviewed_media_results) == 2

    def swap_ownership(bundle):
        first, second = tools(bundle)
        first["state"]["content"], second["state"]["content"] = (
            second["state"]["content"],
            first["state"]["content"],
        )

    changed = changed_artifact(values[1], swap_ownership)
    with pytest.raises(ProjectionError, match="semantic_mismatch"):
        prepare((values[0], changed, *values[2:]))
    assert not verify_projection(prepared, observed(changed), context=values[4]).semantic_matches


def test_media_archive_is_explicit_and_generates_only_the_existing_validated_rule(tmp_path):
    from session_migrate.projection_policies import REVIEWED_MEDIA_ARCHIVE_POLICY_ID

    records = media_rows()
    records[-1]["message"]["content"] = [
        {
            "type": "tool_use",
            "id": "OPEN_CALL",
            "name": "Other",
            "input": {"n": 1},
        }
    ]
    values = parts(tmp_path, records=records)
    with pytest.raises(ProjectionError, match="source_tools_unproven"):
        prepare(values)
    policy = ProjectionPolicy(REVIEWED_MEDIA_ARCHIVE_POLICY_ID)
    archived = (*values[:3], replace(values[3], projection_policy=policy.policy_id), values[4])
    prepared = prepare(archived, policy=policy)
    assert len(prepared.expected.generated_members) == 1
    assert verify_projection(prepared, observed(values[1]), context=values[4]).semantic_matches
    assert prepared.certificate is None


@pytest.mark.parametrize("archive_open", [False, True])
@pytest.mark.parametrize("corrupt_archive", [False, True])
def test_completed_media_tree_resume_verifies_archive_without_reimport(
    tree_setup, archive_open, corrupt_archive
):
    from pathlib import Path

    from test_tree_cli import declaration

    from session_migrate.projection_policies import REVIEWED_MEDIA_ARCHIVE_POLICY_ID
    from session_migrate.tree_model import TreeSelection
    from session_migrate.tree_operations import (
        TreeOperationError,
        commit_tree,
        prepare_tree,
        prepare_tree_resume,
        tree_proposal,
    )
    from session_migrate.tree_sources import inventory_from_explicit_manifest

    original, _, options, native, imports = tree_setup
    root = Path(original.canonical_root)
    for node in original.nodes:
        records = media_rows()
        for row in records:
            row["sessionId"] = node.source_native_id
        if archive_open:
            records[-1]["message"]["content"] = [
                {
                    "type": "tool_use",
                    "id": "OPEN_TREE_CALL",
                    "name": "Other",
                    "input": {"n": 1},
                }
            ]
        (root / node.relative_path).write_text("\n".join(json.dumps(r) for r in records) + "\n")
    declared = declaration(original)
    declared["nodes"] = [node for node in declared["nodes"] if node["key"] == "parent"]
    declared["relationships"] = []
    inventory = inventory_from_explicit_manifest(
        declared, root=root, source_format="claude", client_version="2.1.309"
    )
    selection = TreeSelection("single", "parent", ("parent",), ())
    prepared = prepare_tree(inventory, selection, reviewed_portability=True, **options)
    proposal = tree_proposal(prepared)
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed" and len(imports) == 1
    archive = Path(proposal["nodes"][0]["reviewed_archive"]["path"])
    before = copy.deepcopy(native)
    if corrupt_archive:
        archive.write_bytes(b"X" * archive.stat().st_size)
        with pytest.raises(TreeOperationError, match="completed tree node"):
            prepare_tree_resume(
                inventory, selection, operation_id=result["operation_id"], **options
            )
    else:
        resumed = prepare_tree_resume(
            inventory, selection, operation_id=result["operation_id"], **options
        )
        assert resumed._state.reviewed_portability and not resumed._state.handles
        resumed.discard()
    assert len(imports) == 1 and native == before
    from session_migrate.lineage import LineageStore

    record = (
        LineageStore.inspect_existing(options["state_home"] / "lineage-v1").operations[0].record
    )
    assert record.versions.projection_policy == (
        REVIEWED_MEDIA_ARCHIVE_POLICY_ID if archive_open else REVIEWED_MEDIA_POLICY_ID
    )
