"""Synthetic native-shape inventories and adversarial source bindings."""

from __future__ import annotations

import hashlib
import json

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.tree_model import TreeSelection, TreeValidationError, build_tree_plan
from session_migrate.tree_sources import (
    TreeSourceError,
    inventory_from_explicit_manifest,
    inventory_from_native_metadata,
)


def claude(root, native_id, **fields):
    path = root / f"project/{native_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "type": "user",
        "uuid": f"message-{native_id}",
        "parentUuid": None,
        "sessionId": native_id,
        "version": "2.1.318",
        "isSidechain": False,
        "message": {"role": "user", "content": "PRIVATE_BODY_SENTINEL"},
        **fields,
    }
    path.write_text(json.dumps(row) + "\n")
    return path


def codex(root, native_id, **fields):
    path = root / f"sessions/{native_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "type": "session_meta",
        "payload": {"id": native_id, "cli_version": "0.166.0", "source": "cli", **fields},
    }
    path.write_text(json.dumps(row) + "\n")
    return path


def opencode(root, native_id, parent=None):
    path = root / f"{native_id}.json"
    path.write_text(
        json.dumps(
            {
                "info": {"id": native_id, "parentID": parent, "title": f"Title {native_id}"},
                "messages": [],
            }
        )
    )
    return path


def manifest(root):
    for value in ("parent", "child", "grandchild"):
        claude(root, value)
    return {
        "schema_version": 1,
        "kind": "session-tree-inventory",
        "root_node_key": "parent",
        "completeness": "declared_complete",
        "nodes": [
            {
                "key": key,
                "source_native_id": key,
                "relative_path": f"project/{key}.jsonl",
                "source_parent_status": "known_root" if key == "parent" else "known_parent",
            }
            for key in ("parent", "child", "grandchild")
        ],
        "relationships": [
            {
                "key": "edge_child",
                "parent_node_key": "parent",
                "child_node_key": "child",
                "kind": "fork_origin",
            },
            {
                "key": "edge_grandchild",
                "parent_node_key": "child",
                "child_node_key": "grandchild",
                "kind": "fork_origin",
            },
        ],
        "declaration": {
            "declaration_id": "operator_tree",
            "operator": "operator",
            "declared_at": "2026-11-05T12:00:00-03:00",
            "confirmed": True,
            "statement": "Explicitly declared persistent three-level fork tree.",
        },
    }


def load_manifest(value, root):
    return inventory_from_explicit_manifest(
        value, root=root, source_format="claude", client_version="2.1.318"
    )


def load_native(root, source_format="claude", **kwargs):
    version = {"claude": "2.1.318", "codex": "0.166.0", "opencode": "2.2.5"}[source_format]
    return inventory_from_native_metadata(
        root=root, source_format=source_format, client_version=version, **kwargs
    )


def test_manual_three_levels_are_functional_and_preserve_manual_provenance(tmp_path):
    inventory = load_manifest(manifest(tmp_path), tmp_path)
    plan = build_tree_plan(
        inventory,
        TreeSelection(
            "full_tree", "parent", hierarchy_relationship_keys=("edge_child", "edge_grandchild")
        ),
        "ed248491-d0a0-4b52-915a-3ca25d15a27a",
    )
    assert plan.selected_node_keys == ("parent", "child", "grandchild")
    assert plan.parent_map == {"parent": None, "child": "parent", "grandchild": "child"}
    assert all(edge.provenance == "manual" for edge in inventory.relationships)
    assert inventory.completeness == "declared_complete"
    assert "PRIVATE_BODY_SENTINEL" not in repr(inventory)
    assert "Operator operator:" in inventory.evidence[0].statement
    evidence = inventory.evidence[1]
    assert (
        evidence.component_sha256
        == hashlib.sha256((tmp_path / evidence.relative_path).read_bytes()).hexdigest()
    )


def test_manual_manifest_path_is_read_only_and_guarded(tmp_path):
    value = manifest(tmp_path)
    path = tmp_path / "manual-tree.json"
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    assert len(load_manifest(path, tmp_path).nodes) == 3
    assert path.read_bytes() == before


def test_native_claude_does_not_infer_a_parent_from_message_or_labels(tmp_path):
    claude(tmp_path, "parent")
    claude(tmp_path, "child", parentUuid="parent", customTitle="parent", session_id="parent")
    inventory = load_native(tmp_path)
    assert len(inventory.nodes) == 2
    assert inventory.relationships == ()
    assert {node.source_parent_status for node in inventory.nodes} == {"unknown"}
    assert inventory.completeness == "partial"
    with pytest.raises(TreeValidationError, match="Unknown parent"):
        build_tree_plan(
            inventory,
            TreeSelection("single", inventory.root_node_key),
            "ed248491-d0a0-4b52-915a-3ca25d15a27a",
        )


def test_codex_keeps_fork_origin_attached_fork_and_physical_prefix_distinct(tmp_path):
    codex(tmp_path, "parent")
    codex(
        tmp_path,
        "child",
        forked_from_id="parent",
        attached_thread=True,
        session_id="child",
        history_base={"thread_id": "unrelated-physical-prefix"},
    )
    inventory = load_native(tmp_path, "codex")
    nodes = {node.key: node.source_native_id for node in inventory.nodes}
    assert len(inventory.relationships) == 1
    edge = inventory.relationships[0]
    assert (nodes[edge.parent_node_key], nodes[edge.child_node_key]) == ("parent", "child")
    assert edge.kind == "fork_origin"
    assert edge.provenance == "native"
    assert edge.shared_prefix is None
    evidence = next(item for item in inventory.evidence if item.id in edge.evidence_ids)
    assert evidence.field_pointer == "/0/payload/forked_from_id"
    assert all(node.source_native_id != "unrelated-physical-prefix" for node in inventory.nodes)


def test_codex_session_id_grouping_never_becomes_parentage(tmp_path):
    codex(tmp_path, "parent", session_id="shared-group")
    codex(tmp_path, "child", session_id="shared-group")
    inventory = load_native(tmp_path, "codex", root_native_id="parent")
    assert inventory.relationships == ()
    assert len(inventory.nodes) == 2


def test_native_opencode_export_parent_and_title(tmp_path):
    opencode(tmp_path, "ses_parent")
    opencode(tmp_path, "ses_child", "ses_parent")
    inventory = load_native(tmp_path, "opencode")
    assert inventory.relationships[0].kind == "organizational_parent"
    assert inventory.relationships[0].provenance == "native"
    assert {node.title for node in inventory.nodes} == {"Title ses_parent", "Title ses_child"}
    assert inventory.completeness == "partial"


@pytest.mark.parametrize("source_format", ("claude", "codex"))
def test_native_enumeration_excludes_subagent_artifacts(tmp_path, source_format):
    if source_format == "claude":
        claude(tmp_path, "parent")
        claude(tmp_path, "subagent", isSidechain=True)
    else:
        codex(tmp_path, "parent")
        codex(tmp_path, "subagent", parent_thread_id="parent")
    inventory = load_native(tmp_path, source_format)
    assert [node.source_native_id for node in inventory.nodes] == ["parent"]
    assert inventory.relationships == ()


@pytest.mark.parametrize(
    "marker",
    ({"parent_thread_id": "parent"}, {"source": {"subagent": {"parent_thread_id": "parent"}}}),
)
def test_explicit_codex_subagent_cannot_be_promoted(tmp_path, marker):
    path = codex(tmp_path, "child", **marker)
    with pytest.raises(TreeSourceError, match="subagent"):
        load_native(tmp_path, "codex", relative_paths=[path.relative_to(tmp_path).as_posix()])


def test_manual_manifest_cannot_promote_sidechain(tmp_path):
    value = manifest(tmp_path)
    claude(tmp_path, "child", isSidechain=True)
    with pytest.raises(TreeSourceError, match="subagent"):
        load_manifest(value, tmp_path)


@pytest.mark.parametrize(
    "bad_path",
    (
        "../outside.jsonl",
        "/outside.jsonl",
        "a/../file.jsonl",
        "a//file.jsonl",
        "a/./file.jsonl",
        "a\\file.jsonl",
    ),
)
def test_manifest_refuses_noncanonical_or_cross_root_paths(tmp_path, bad_path):
    value = manifest(tmp_path)
    value["nodes"][0]["relative_path"] = bad_path
    with pytest.raises(TreeSourceError, match="inside its root"):
        load_manifest(value, tmp_path)


@pytest.mark.parametrize("ancestor", (False, True))
def test_manifest_refuses_symlink_file_or_ancestor(tmp_path, ancestor):
    root = tmp_path / "root"
    root.mkdir()
    value = manifest(root)
    if ancestor:
        link = root / "linked"
        link.symlink_to(root / "project", target_is_directory=True)
        value["nodes"][0]["relative_path"] = "linked/parent.jsonl"
    else:
        link = root / "linked.jsonl"
        link.symlink_to(root / "project/parent.jsonl")
        value["nodes"][0]["relative_path"] = "linked.jsonl"
    with pytest.raises(SessionMigrateError, match="guard failed|regular file"):
        load_manifest(value, root)


def test_named_root_refuses_symlink_even_when_files_are_regular(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    claude(root, "parent")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(TreeSourceError, match="root filesystem guard"):
        load_native(alias)


def test_native_refuses_duplicate_ids_and_never_fuses_roots(tmp_path):
    first = claude(tmp_path, "parent")
    duplicate = first.with_name("copy.jsonl")
    duplicate.write_bytes(first.read_bytes())
    with pytest.raises(TreeSourceError, match="Duplicate native session ID"):
        load_native(tmp_path)


def test_manual_declared_identity_must_equal_exact_transcript(tmp_path):
    value = manifest(tmp_path)
    value["nodes"][1]["source_native_id"] = "parent"
    with pytest.raises(TreeSourceError, match="differs from the transcript"):
        load_manifest(value, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    (
        "unconfirmed",
        "no_timezone",
        "verified_complete",
        "wrong_root",
        "unknown_field",
        "duplicate_node",
    ),
)
def test_manual_metadata_refusals(tmp_path, mutation):
    value = manifest(tmp_path)
    if mutation == "unconfirmed":
        value["declaration"]["confirmed"] = False
    elif mutation == "no_timezone":
        value["declaration"]["declared_at"] = "2026-11-05T12:00:00"
    elif mutation == "verified_complete":
        value["completeness"] = "verified_complete"
    elif mutation == "wrong_root":
        value["canonical_root"] = "/another/root"
    elif mutation == "unknown_field":
        value["autoApprove"] = True
    elif mutation == "duplicate_node":
        value["nodes"].append(value["nodes"][0].copy())
    with pytest.raises(TreeSourceError):
        load_manifest(value, tmp_path)


def test_manual_refuses_duplicate_json_keys(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"kind":"session-tree-inventory","kind":"other"}')
    with pytest.raises(TreeSourceError, match="Duplicate JSON"):
        load_manifest(path, tmp_path)


def test_native_orphan_does_not_search_another_root(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    codex(first, "parent")
    codex(second, "child", forked_from_id="parent")
    with pytest.raises(TreeSourceError, match="absent from the named source root"):
        load_native(second, "codex")


def test_manual_conflicting_native_relationship_is_refused(tmp_path):
    codex(tmp_path, "parent")
    codex(tmp_path, "other")
    codex(tmp_path, "child", forked_from_id="parent")
    value = manifest(tmp_path)
    value["nodes"] = [
        {
            "key": key,
            "source_native_id": key,
            "relative_path": f"sessions/{key}.jsonl",
            "source_parent_status": "known_parent" if key == "child" else "known_root",
        }
        for key in ("parent", "other", "child")
    ]
    value["relationships"] = [
        {"key": "bad", "parent_node_key": "other", "child_node_key": "child", "kind": "fork_origin"}
    ]
    with pytest.raises(TreeSourceError, match="conflicts with an observed native parent"):
        inventory_from_explicit_manifest(
            value, root=tmp_path, source_format="codex", client_version="0.166.0"
        )


def test_undocumented_desktop_adapter_has_explicit_manual_fallback(tmp_path):
    with pytest.raises(TreeSourceError, match="explicit manual tree manifest"):
        load_native(tmp_path, adapter="claude_desktop")
    assert load_manifest(manifest(tmp_path), tmp_path).relationships


def test_unapproved_native_version_is_not_silently_trusted(tmp_path):
    claude(tmp_path, "parent")
    with pytest.raises(TreeSourceError, match="approved metadata adapter"):
        inventory_from_native_metadata(
            root=tmp_path, source_format="claude", client_version="2.1.319"
        )


def test_compressed_codex_evidence_hashes_physical_file(tmp_path):
    import ctypes
    import ctypes.util

    library = ctypes.util.find_library("zstd")
    if not library:
        pytest.skip("libzstd is unavailable")
    lib = ctypes.CDLL(library)
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
    ]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    plain = (
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "parent", "cli_version": "0.166.0", "source": "cli"},
            }
        )
        + "\n"
    ).encode()
    output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(plain)))
    count = lib.ZSTD_compress(output, len(output), plain, len(plain), 3)
    path = tmp_path / "parent.jsonl.zst"
    path.write_bytes(output.raw[:count])
    inventory = load_native(tmp_path, "codex")
    assert inventory.nodes[0].source_native_id == "parent"
    assert inventory.evidence[0].component_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_native_duplicate_metadata_keys_are_refused(tmp_path):
    path = tmp_path / "duplicate.jsonl"
    path.write_text('{"sessionId":"first","sessionId":"second","version":"2.1.318"}\n')
    with pytest.raises(TreeSourceError, match="transcript metadata"):
        load_native(tmp_path)


def test_native_record_depth_is_bounded_before_returning_inventory(tmp_path):
    path = tmp_path / "nested.jsonl"
    path.write_text(
        '{"sessionId":"parent","version":"2.1.318","message":' + "[" * 130 + "0" + "]" * 130 + "}\n"
    )
    with pytest.raises(TreeSourceError, match="transcript metadata"):
        load_native(tmp_path)


def test_native_directory_budget_is_enforced(tmp_path, monkeypatch):
    import session_migrate.tree_sources as sources

    claude(tmp_path, "parent")
    monkeypatch.setattr(sources, "MAX_DIRECTORY_ENTRIES", 1)
    with pytest.raises(TreeSourceError, match="directory entry limit"):
        load_native(tmp_path)


def test_native_source_byte_budget_is_enforced(tmp_path):
    claude(tmp_path, "parent")
    with pytest.raises(SessionMigrateError, match="byte safety limit"):
        load_native(tmp_path, max_total_source_bytes=32)


def test_native_explicit_paths_refuse_duplicates(tmp_path):
    claude(tmp_path, "parent")
    with pytest.raises(TreeSourceError, match="Duplicate native metadata source path"):
        load_native(tmp_path, relative_paths=["project/parent.jsonl", "project/parent.jsonl"])


def test_manifest_hardlink_is_refused(tmp_path):
    value = manifest(tmp_path)
    (tmp_path / "copy").hardlink_to(tmp_path / "project/parent.jsonl")
    with pytest.raises(SessionMigrateError, match="one link"):
        load_manifest(value, tmp_path)


def test_known_mixed_claude_versions_keep_identity_without_claiming_one_client(tmp_path):
    value = manifest(tmp_path)
    for node in value["nodes"]:
        path = tmp_path / node["relative_path"]
        row = json.loads(path.read_text())
        old = {**row, "uuid": "older-" + row["uuid"], "version": "2.1.209"}
        row["parentUuid"] = old["uuid"]
        path.write_text(json.dumps(old) + "\n" + json.dumps(row) + "\n")
    inventory = load_manifest(value, tmp_path)
    assert len(inventory.nodes) == 3
    assert all(edge.provenance == "manual" for edge in inventory.relationships)
    from session_migrate.lineage import Coverage
    from session_migrate.model import AgentFormat
    from session_migrate.native_audit import audit_snapshot
    from session_migrate.native_snapshots import SourceCaptureContext, capture_source

    captured = capture_source(
        SourceCaptureContext(AgentFormat.CLAUDE, tmp_path / "project/parent.jsonl")
    )
    assert audit_snapshot(captured).coverage is Coverage.UNKNOWN


def test_unknown_mixed_claude_version_still_fails_before_inventory(tmp_path):
    value = manifest(tmp_path)
    path = tmp_path / value["nodes"][0]["relative_path"]
    row = json.loads(path.read_text())
    unknown = {**row, "uuid": "unknown", "version": "2.1.999"}
    path.write_text(json.dumps(unknown) + "\n" + json.dumps(row) + "\n")
    with pytest.raises(TreeSourceError, match="ambiguous"):
        load_manifest(value, tmp_path)
