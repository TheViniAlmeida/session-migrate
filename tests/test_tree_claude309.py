"""Read-only Desktop 309 support must not acquire full-history authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_tree_cli import declaration
from test_tree_operations import setup as setup

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode
from session_migrate.model import EventKind
from session_migrate.tree_operations import commit_tree, prepare_tree, tree_proposal
from session_migrate.tree_sources import inventory_from_explicit_manifest


def desktop_inventory(inventory, *, failure=None):
    root = Path(inventory.canonical_root)
    for node in inventory.nodes:
        path = root / node.relative_path
        row = json.loads(path.read_text())
        row["version"] = "2.1.309"
        duplicate = {
            **row,
            "cwd": "/fixture/changed",
            "slug": "copy",
            "promptId": "new-envelope",
            "toolUseResult": {"opaque": "DO_NOT_REPLAY"},
        }
        if node.key == "grandchild":
            if failure == "missing_parent":
                row["parentUuid"] = duplicate["parentUuid"] = "missing"
            elif failure == "conflicting_copy":
                duplicate["parentUuid"] = "different"
        path.write_text(json.dumps(row) + "\n" + json.dumps(duplicate) + "\n")
    return inventory_from_explicit_manifest(
        declaration(inventory), root=root, source_format="claude", client_version="2.1.309"
    )


def test_desktop_tree_publishes_once_with_unknown_coverage_and_physical_losses(setup):
    inventory, selection, options, native, imports = setup
    inventory = desktop_inventory(inventory)
    before = {
        node.key: (Path(inventory.canonical_root) / node.relative_path).read_bytes()
        for node in inventory.nodes
    }
    prepared = prepare_tree(inventory, selection, **options)
    proposal = tree_proposal(prepared)
    assert proposal["coverage"] == "unknown"
    assert proposal["fork_boundary_proven"] is False
    assert all(node["losses"] for node in proposal["nodes"])
    assert all(node["coverage"] == "unknown" for node in proposal["nodes"])
    assert all(
        any(w["code"] == "unvalidated_source_version" for w in node["warnings"])
        for node in proposal["nodes"]
    )
    for node in proposal["nodes"]:
        assert node["source_sha256"] == hashlib.sha256(before[node["key"]]).hexdigest()
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed"
    assert len(imports) == 3
    for node in proposal["nodes"]:
        artifact = native[node["target_id"]]
        assert artifact["info"].get("parentID") == node["parent_target_id"]
        projected = opencode.parse_import_bytes(json.dumps(artifact).encode())
        assert [e.text for e in projected.events if e.kind is EventKind.MESSAGE] == [
            f"PRIVATE_BODY_SENTINEL {node['key']}"
        ]
        assert "DO_NOT_REPLAY" not in json.dumps(artifact)
    for node in inventory.nodes:
        assert (Path(inventory.canonical_root) / node.relative_path).read_bytes() == before[
            node.key
        ]


def test_desktop_tree_requires_explicit_partial_acknowledgement(setup):
    inventory, selection, options, _, imports = setup
    inventory = desktop_inventory(inventory)
    options["acknowledge_partial"] = False
    prepared = prepare_tree(inventory, selection, **options)
    with pytest.raises(SessionMigrateError, match="acknowledg"):
        commit_tree(prepared)
    assert not imports


@pytest.mark.parametrize("failure", ["missing_parent", "conflicting_copy"])
def test_desktop_tree_rejects_unproven_graph_before_any_node_import(setup, failure):
    inventory, selection, options, _, imports = setup
    inventory = desktop_inventory(inventory, failure=failure)
    with pytest.raises(SessionMigrateError):
        prepare_tree(inventory, selection, **options)
    assert not imports


def test_individual_conversion_requires_destination_for_conflicting_envelope_cwd(tmp_path):
    from test_claude_latest import _parse, _record

    from session_migrate.conversion import ConversionOptions, convert_session
    from session_migrate.model import TargetFormat

    row = _record("user", "u", None, "request", version="2.1.309", timestamp="2026-11-05T12:00:00Z")
    session = _parse([row, {**row, "cwd": "/fixture/different"}])
    assert session.cwd is None
    with pytest.raises(SessionMigrateError, match="explicit destination --cwd"):
        convert_session(session, ConversionOptions(TargetFormat.OPENCODE))
    artifact = convert_session(
        session,
        ConversionOptions(
            TargetFormat.OPENCODE,
            cwd=tmp_path,
            target_cli_version="2.2.5",
            model_provider="fixture",
            model="test",
        ),
    )
    assert artifact.cwd == tmp_path
    assert artifact.dropped
    assert any(w["code"] == "unvalidated_source_version" for w in artifact.warnings)


def test_309_tool_evidence_survives_opencode_projection(tmp_path):
    from test_claude_desktop309 import desktop
    from test_claude_latest import _parse

    from session_migrate.conversion import ConversionOptions, convert_session
    from session_migrate.model import TargetFormat

    call = desktop(
        "assistant",
        "a",
        "u",
        [
            {
                "type": "tool_use",
                "id": "call",
                "name": "Read",
                "input": {"file": "ação.txt", "options": {"lines": [1, 3]}},
            }
        ],
    )
    result = desktop(
        "user",
        "r",
        "a",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call",
                "content": "synthetic failure\nnext line",
                "is_error": True,
            }
        ],
    )
    source = _parse(
        [
            desktop(),
            result,
            call,
            desktop("assistant", "z", "r", "done"),
            {**result, "toolUseResult": {"extra": "WRAPPER_ONLY_SENTINEL"}},
        ]
    )
    artifact = convert_session(
        source,
        ConversionOptions(
            TargetFormat.OPENCODE,
            cwd=tmp_path,
            target_cli_version="2.2.5",
            model_provider="fixture",
            model="test",
        ),
    )
    target = opencode.parse_import_bytes(artifact.native_bytes)
    tools = [e for e in target.events if e.tool_call_id]
    assert [e.kind for e in tools] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert [e.tool_call_id for e in tools] == ["call", "call"]
    assert tools[0].payload["input"] == call["message"]["content"][0]["input"]
    assert tools[1].text == "synthetic failure\nnext line"
    assert tools[1].payload["is_error"] is True
    assert "WRAPPER_ONLY_SENTINEL" not in artifact.native_bytes.decode()
    assert artifact.dropped["opaque:duplicate_desktop_message_envelope"] == 1
