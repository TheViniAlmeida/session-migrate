"""Real tree compilation/publication behind explicit frontend confirmation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_tree_operations as publisher_fixtures

from session_migrate import operations
from session_migrate.cli import main
from session_migrate.errors import SessionMigrateError
from session_migrate.profiles import Profile, ProfileStore
from session_migrate.tree_cli import TreeWorkflow

tree_setup = publisher_fixtures.setup


def declaration(inventory):
    return {
        "kind": "session-tree-inventory",
        "schema_version": 1,
        "root_node_key": inventory.root_node_key,
        "completeness": "declared_complete",
        "nodes": [
            {
                key: getattr(node, key)
                for key in (
                    "key",
                    "source_native_id",
                    "relative_path",
                    "title",
                    "source_parent_status",
                )
            }
            for node in inventory.nodes
        ],
        "relationships": [
            {
                key: getattr(edge, key)
                for key in (
                    "key",
                    "parent_node_key",
                    "child_node_key",
                    "kind",
                )
            }
            for edge in inventory.relationships
        ],
        "declaration": {
            "declaration_id": "frontend_test",
            "operator": "test",
            "declared_at": "2026-11-05T12:00:00Z",
            "confirmed": True,
            "statement": "Explicitly reviewed synthetic persistent forks.",
        },
    }


@pytest.fixture
def workspace(tree_setup, tmp_path, monkeypatch):
    inventory, _, options, native, imports = tree_setup
    tmp_path.chmod(0o700)
    source = Profile("source", "Synthetic source", "claude", Path(inventory.canonical_root))
    profiles = (source, options["target"])
    storage = tmp_path / "profiles.json"
    ProfileStore(storage).create(profiles)
    manifest = tmp_path / "inventory.json"
    manifest.write_text(json.dumps(declaration(inventory)))
    for key, value in options["base_environ"].items():
        monkeypatch.setenv(key, value)
    args = [
        "--state-home",
        str(options["state_home"]),
        "tree",
        "migrate",
        "--profiles",
        str(storage),
        "--source-profile",
        "source",
        "--source-client-version",
        "2.1.318",
        "--manifest",
        str(manifest),
        "--target-profile",
        "target",
        "--cwd",
        str(options["cwd"]),
        "--model-provider",
        "fixture",
        "--model",
        "test",
        "--acknowledge-partial",
    ]
    return inventory, options, native, imports, profiles, storage, manifest, args


def documents(text):
    result, decoder = [], json.JSONDecoder()
    while text.strip():
        text = text.lstrip()
        value, end = decoder.raw_decode(text)
        result.append(value)
        text = text[end:]
    return result


def test_noninteractive_cli_requires_current_confirmation_and_never_publishes(
    workspace, capsys, monkeypatch
):
    _, options, _, imports, _, _, _, args = workspace
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(args) == 2
    output = capsys.readouterr()
    proposal = json.loads(output.out)
    assert "confirmation required" in output.err
    assert [item["key"] for item in proposal["structure"]["nodes"]] == [
        "parent",
        "child",
        "grandchild",
    ]
    assert all(edge["provenance"] == "manual" for edge in proposal["structure"]["relationships"])
    assert imports == [] and not (options["state_home"] / "tree-v1").exists()
    assert "PRIVATE_BODY_SENTINEL" not in output.out + output.err


def test_explicit_cli_yes_publishes_parent_first_and_inspection_never_reimports(workspace, capsys):
    _, options, native, imports, _, _, _, args = workspace
    assert main([*args, "--yes"]) == 0
    proposal, result = documents(capsys.readouterr().out)
    assert result["status"] == "completed" and len(imports) == 3
    assert native[imports[1]]["info"]["parentID"] == imports[0]
    for command in ("inspect", "reconcile"):
        assert (
            main(
                [
                    "--state-home",
                    str(options["state_home"]),
                    "tree",
                    command,
                    result["operation_id"],
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["operation_id"] == proposal["operation_id"]
    assert len(imports) == 3


def test_boundary_requires_detach_before_target_preflight(workspace, capsys):
    _, _, _, imports, _, _, _, args = workspace
    assert main([*args, "--mode", "single", "--anchor", "child", "--yes"]) == 2
    assert imports == []
    capsys.readouterr()
    assert main([*args, "--mode", "single", "--anchor", "child", "--detach", "--yes"]) == 0
    proposal, result = documents(capsys.readouterr().out)
    assert proposal["detached_node_keys"] == ["child"]
    assert [item["key"] for item in proposal["structure"]["nodes"]] == ["child"]
    assert result["nodes"][0]["parent_target_id"] is None and len(imports) == 1


def test_shared_workflow_rejects_stale_source_and_consumed_confirmation(workspace):
    inventory, options, _, imports, profiles, _, manifest, _ = workspace
    flow = TreeWorkflow(
        profiles, state_home=options["state_home"], base_environ=options["base_environ"]
    )
    flow.load(source_profile_id="source", client_version="2.1.318", manifest=manifest)
    flow.select(mode="full_tree")
    flow.prepare(
        "target",
        cwd=options["cwd"],
        model_provider="fixture",
        model="test",
        acknowledge_partial=True,
    )
    path = Path(inventory.canonical_root) / "grandchild.jsonl"
    path.write_text(path.read_text().replace("PRIVATE_BODY_SENTINEL", "CHANGED"))
    with pytest.raises(SessionMigrateError, match="changed"):
        flow.commit()
    with pytest.raises(SessionMigrateError, match="consumed"):
        flow.commit()
    assert imports == []
    flow.close()


def test_inventory_recaptures_under_explicit_root_and_rejects_saved_authority(workspace, capsys):
    inventory, _, _, imports, _, _, manifest, _ = workspace
    args = [
        "tree",
        "inventory",
        "--root",
        inventory.canonical_root,
        "--format",
        "claude",
        "--source-client-version",
        "2.1.318",
        "--manifest",
        str(manifest),
    ]
    assert main(args) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["authority"] == "metadata_only_recapture_required"
    assert "PRIVATE_BODY_SENTINEL" not in json.dumps(document)
    manifest.write_text(json.dumps(document))
    assert main(args) == 2
    assert imports == []


def test_cli_partial_receipt_requires_explicit_resume_and_only_remaining_nodes(
    workspace, capsys, monkeypatch
):
    _, _, _, imports, _, _, _, args = workspace
    original = operations.commit_operation

    def cancel_after_first(*values, **kwargs):
        receipt = original(*values, **kwargs)
        kwargs["cancellation"].parent.request_cancel()
        return receipt

    monkeypatch.setattr(operations, "commit_operation", cancel_after_first)
    assert main([*args, "--yes"]) == 2
    _, partial = documents(capsys.readouterr().out)
    assert partial["status"] == "partial" and len(imports) == 1
    assert partial["nodes"][0]["status"] == "completed"
    monkeypatch.setattr(operations, "commit_operation", original)
    resume_args = ["resume" if item == "migrate" else item for item in args]
    assert main([*resume_args, "--operation-id", partial["operation_id"], "--yes"]) == 0
    _, resumed = documents(capsys.readouterr().out)
    assert resumed["status"] == "completed" and len(imports) == 3
