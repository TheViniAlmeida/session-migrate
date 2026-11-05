"""Observable tree selection, bounded parsing, and qualified ID contracts."""

import json
import uuid
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from session_migrate.tree_model import (
    MAX_JSON_DEPTH,
    MAX_PROPOSAL_BYTES,
    TreeEvidence,
    TreeInventory,
    TreeNode,
    TreeRelationship,
    TreeSelection,
    TreeValidationError,
    build_tree_plan,
    deterministic_tree_id,
    inventory_to_dict,
    parse_tree_manifest,
    parse_tree_proposal,
    plan_to_dict,
    validate_relative_path,
    validate_tree_inventory,
    validate_tree_title,
)

OPERATION = "4f3f56bd-2fd8-4f12-9f9a-1e11ac48aa0a"


def inventory():
    evidence = TreeEvidence(
        "operator",
        "manual_declaration",
        "review",
        "2026-11-05T10:00:00-03:00",
        "Operator declares this synthetic tree.",
    )
    nodes = tuple(
        TreeNode(
            key,
            f"native-{key}",
            f"project/{key}.jsonl",
            f"Title {key}",
            True,
            ("operator",),
            "known_root" if key == "root" else "known_parent",
        )
        for key in ("root", "child", "sibling", "grandchild")
    )
    edges = tuple(
        TreeRelationship(f"edge_{child}", parent, child, evidence_ids=("operator",))
        for parent, child in (("root", "child"), ("root", "sibling"), ("child", "grandchild"))
    )
    return TreeInventory(
        "claude",
        "/synthetic/source",
        "root",
        nodes,
        edges,
        (evidence,),
        "explicit_manifest",
        "declared_complete",
        ("operator",),
    )


def selection(inv, mode="full_tree", anchor="root", boundary="require_closed", included=()):
    return TreeSelection(
        mode, anchor, included, tuple(edge.key for edge in inv.relationships), boundary
    )


def manifest(inv=None):
    inv = inv or inventory()
    value = inventory_to_dict(inv)
    value["evidence"] = [
        {key: val for key, val in item.items() if val is not None} for item in value["evidence"]
    ]
    value.update(
        schema_version=1,
        kind="session-tree-manifest",
        operation_id=OPERATION,
        synthetic=True,
        selection={
            "mode": "full_tree",
            "anchor_node_key": "root",
            "included_node_keys": [node.key for node in inv.nodes],
            "hierarchy_relationship_keys": [edge.key for edge in inv.relationships],
            "boundary_policy": "require_closed",
        },
    )
    return value


def test_plan_order_parents_and_identity_stability():
    inv = inventory()
    plan = build_tree_plan(inv, selection(inv), OPERATION)
    assert plan.selected_node_keys == ("root", "child", "sibling", "grandchild")
    assert plan.parent_map == {
        "root": None,
        "child": "root",
        "sibling": "root",
        "grandchild": "child",
    }
    reordered = replace(
        inv, nodes=tuple(reversed(inv.nodes)), relationships=tuple(reversed(inv.relationships))
    )
    assert build_tree_plan(reordered, selection(reordered), OPERATION).id_map == plan.id_map
    name = '["claude","/synthetic/source","native-root"]'
    assert plan.id_map["root"] == "ses_" + uuid.uuid5(uuid.UUID(OPERATION), name).hex
    assert (
        deterministic_tree_id(OPERATION, "claude", "/other", "native-root") != plan.id_map["root"]
    )
    assert (
        deterministic_tree_id(OPERATION, "codex", "/synthetic/source", "native-root")
        != plan.id_map["root"]
    )
    assert (
        deterministic_tree_id(str(uuid.uuid4()), *inv.nodes[0].qualified_identity(inv))
        != plan.id_map["root"]
    )


def test_selection_is_exact_and_boundary_is_explicit():
    inv = inventory()
    with pytest.raises(TreeValidationError, match="boundary"):
        build_tree_plan(inv, selection(inv, "subtree", "child"), OPERATION)
    sub = build_tree_plan(inv, selection(inv, "subtree", "child", "detach_explicit"), OPERATION)
    assert sub.selected_node_keys == ("child", "grandchild")
    assert sub.detached_node_keys == ("child",)
    assert sub.parent_map == {"child": None, "grandchild": "child"}
    assert set(sub.id_map) == {"child", "grandchild"}
    single = build_tree_plan(inv, selection(inv, "single", "child", "detach_explicit"), OPERATION)
    assert single.selected_node_keys == ("child",)
    assert single.selected_relationships == ()
    for included in (("child",), ("child", "grandchild", "sibling")):
        with pytest.raises(TreeValidationError, match="closure"):
            build_tree_plan(
                inv, selection(inv, "subtree", "child", "detach_explicit", included), OPERATION
            )
    with pytest.raises(TreeValidationError, match="complete"):
        build_tree_plan(replace(inv, completeness="partial"), selection(inv), OPERATION)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_key",
        "duplicate_native",
        "duplicate_path",
        "orphan",
        "self",
        "cycle",
        "missing_parent",
        "multiple_parent",
        "root_has_parent",
        "duplicate_evidence",
        "duplicate_relationship",
    ],
)
def test_invalid_graphs_are_rejected(case):
    inv = inventory()
    nodes, edges = list(inv.nodes), list(inv.relationships)
    if case == "duplicate_key":
        nodes[-1] = replace(nodes[-1], key="child")
    elif case == "duplicate_native":
        nodes[-1] = replace(nodes[-1], source_native_id=nodes[0].source_native_id)
    elif case == "duplicate_path":
        nodes[-1] = replace(nodes[-1], relative_path=nodes[0].relative_path)
    elif case == "orphan":
        edges.pop()
        nodes[-1] = replace(nodes[-1], source_parent_status="unknown")
    elif case == "self":
        edges[0] = replace(edges[0], parent_node_key="child")
    elif case == "cycle":
        edges.append(TreeRelationship("cycle", "grandchild", "child", evidence_ids=("operator",)))
    elif case == "missing_parent":
        edges[0] = replace(edges[0], parent_node_key="absent")
    elif case == "multiple_parent":
        edges.append(TreeRelationship("extra", "sibling", "grandchild", evidence_ids=("operator",)))
    elif case == "root_has_parent":
        edges.append(TreeRelationship("badroot", "child", "root", evidence_ids=("operator",)))
    elif case == "duplicate_evidence":
        inv = replace(inv, evidence=inv.evidence * 2)
    elif case == "duplicate_relationship":
        edges.append(edges[0])
    inv = replace(inv, nodes=nodes, relationships=edges)
    with pytest.raises(TreeValidationError):
        build_tree_plan(inv, selection(inv), OPERATION)


def test_distinct_facts_for_same_pair_are_one_hierarchy_parent():
    inv = inventory()
    extra = replace(inv.relationships[0], key="organizational", kind="organizational_parent")
    inv = replace(inv, relationships=(*inv.relationships, extra))
    plan = build_tree_plan(inv, selection(inv), OPERATION)
    assert plan.parent_map["child"] == "root"
    assert len(plan.ordered_nodes) == 4


def test_external_reports_and_unknown_roots_cannot_establish_tree():
    inv = inventory()
    with pytest.raises(TreeValidationError, match="root"):
        build_tree_plan(
            replace(
                inv, nodes=(replace(inv.nodes[0], source_parent_status="unknown"), *inv.nodes[1:])
            ),
            selection(inv),
            OPERATION,
        )
    report = TreeEvidence(
        "operator",
        "external_report",
        uri="https://example.invalid/report",
        claim="A report does not prove persistence.",
    )
    with pytest.raises(TreeValidationError, match="Persistence"):
        validate_tree_inventory(replace(inv, evidence=(report,)))
    with pytest.raises(TreeValidationError, match="provenance"):
        validate_tree_inventory(
            replace(
                inv,
                relationships=(
                    replace(inv.relationships[0], provenance="native"),
                    *inv.relationships[1:],
                ),
            )
        )


def test_maximum_nodes_and_depth_are_bounded():
    inv = inventory()
    too_many = tuple(
        replace(inv.nodes[0], key=f"n{i}", source_native_id=f"id{i}", relative_path=f"p/{i}.jsonl")
        for i in range(1025)
    )
    with pytest.raises(TreeValidationError, match="node limit"):
        validate_tree_inventory(replace(inv, nodes=too_many))
    nodes = tuple(
        TreeNode(
            f"n{i}",
            f"id{i}",
            f"p/{i}.jsonl",
            "Title",
            True,
            ("operator",),
            "known_root" if i == 0 else "known_parent",
        )
        for i in range(66)
    )
    edges = tuple(
        TreeRelationship(f"e{i}", f"n{i - 1}", f"n{i}", evidence_ids=("operator",))
        for i in range(1, 66)
    )
    chain = replace(inv, root_node_key="n0", nodes=nodes, relationships=edges)
    with pytest.raises(TreeValidationError, match="depth"):
        build_tree_plan(chain, selection(chain, anchor="n0"), OPERATION)
    boundary = replace(chain, nodes=nodes[:-1], relationships=edges[:-1])
    assert (
        len(build_tree_plan(boundary, selection(boundary, anchor="n0"), OPERATION).ordered_nodes)
        == 65
    )


def test_manual_json_keeps_provenance_and_is_deeply_immutable():
    value = manifest()
    value["evidence"][0]["statement"] = "Exact operator statement: café 🐈."
    parsed = parse_tree_manifest(json.dumps(value, ensure_ascii=False))
    assert parsed.inventory.evidence[0].statement == value["evidence"][0]["statement"]
    assert parsed.publication_blocker_codes == ("SYNTHETIC_EVIDENCE_ONLY",)
    with pytest.raises(TypeError):
        parsed.declared_metadata["selection"]["mode"] = "single"
    with pytest.raises(FrozenInstanceError):
        parsed.inventory.nodes[0].title = "changed"
    plan = build_tree_plan(parsed.inventory, parsed.selection, parsed.operation_id)
    with pytest.raises(TypeError):
        plan.id_map["root"] = "changed"
    assert plan_to_dict(plan)["verification"] == "not_performed"


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "C:/file",
        "a\\b",
        "a\x00b",
        "a\x7fb",
    ],
)
def test_noncanonical_relative_paths_are_rejected(path):
    with pytest.raises(TreeValidationError):
        validate_relative_path(path)


def test_titles_are_utf8_bytes_bounded():
    assert validate_tree_title("é" * 512) == "é" * 512
    for title in ("é" * 513, "bad\nline", "bad\x7f", "\ud800", ""):
        with pytest.raises(TreeValidationError):
            validate_tree_title(title)


def test_parser_fails_closed_and_never_executes_input(tmp_path):
    marker = tmp_path / "must-not-exist"
    value = manifest()
    value["evidence"][0]["statement"] = f"$(touch {marker})"
    assert (
        parse_tree_manifest(json.dumps(value)).inventory.evidence[0].statement
        == value["evidence"][0]["statement"]
    )
    assert not marker.exists()
    for extra in ("permissions", "mcp", "hooks", "conversation"):
        mutated = {**value, extra: {"anything": "untrusted"}}
        with pytest.raises(TreeValidationError, match="Unknown"):
            parse_tree_manifest(json.dumps(mutated))
    value["nodes"][0]["permissions"] = []
    with pytest.raises(TreeValidationError, match="Unknown"):
        parse_tree_manifest(json.dumps(value))
    for payload in (
        '{"kind":1,"kind":2}',
        '{"value":NaN}',
        b"\xff",
        "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1),
        " " * (MAX_PROPOSAL_BYTES + 1),
    ):
        with pytest.raises(TreeValidationError):
            parse_tree_manifest(payload)


def test_public_synthetic_proposal_parses_but_never_rehydrates_verification():
    payload = (Path(__file__).parents[1] / "docs/examples/session-tree-v1.json").read_bytes()
    parsed = parse_tree_proposal(payload)
    assert parsed.synthetic
    assert parsed.publication_blocker_codes == ("SYNTHETIC_EVIDENCE_ONLY",)
    assert parsed.inventory.relationships[0].provenance == "manual"
    declared = json.loads(payload)
    declared["synthetic"] = False
    declared["preflight"] = {
        "status": "ready_for_live_review",
        "blocker_codes": [],
        "check_receipt_sha256": "a" * 64,
    }
    parsed = parse_tree_proposal(json.dumps(declared))
    assert parsed.publication_blocker_codes == ("LIVE_PREFLIGHT_REQUIRED",)
    declared["synthetic"] = True
    with pytest.raises(TreeValidationError, match="Synthetic"):
        parse_tree_proposal(json.dumps(declared))


@pytest.mark.parametrize(
    "mutation",
    [
        "capture_extra",
        "target_parent",
        "target_id",
        "capture_hash",
        "unknown_node",
        "duplicate_capture",
        "fake_projection",
    ],
)
def test_public_proposal_inconsistent_metadata_rejected(mutation):
    value = json.loads(
        (Path(__file__).parents[1] / "docs/examples/session-tree-v1.json").read_text()
    )
    if mutation == "capture_extra":
        value["captures"][0]["permissions"] = []
    elif mutation == "target_parent":
        value["targets"][1]["target_parent_node_key"] = "grandchild"
    elif mutation == "target_id":
        value["targets"][0]["native_id"] = "ses_" + "a" * 32
    elif mutation == "capture_hash":
        value["captures"][0]["snapshot_sha256"] = "a" * 64
    elif mutation == "unknown_node":
        value["nodes"][0]["key"] = "absent"
    elif mutation == "duplicate_capture":
        value["captures"][1] = value["captures"][0]
    elif mutation == "fake_projection":
        value["targets"][0]["expected_logical_unit_count"] += 1
    with pytest.raises(TreeValidationError):
        parse_tree_proposal(json.dumps(value))


def test_public_partial_selection_does_not_require_unselected_captures():
    value = json.loads(
        (Path(__file__).parents[1] / "docs/examples/session-tree-v1.json").read_text()
    )
    value["selection"].update(
        mode="single",
        anchor_node_key="child",
        included_node_keys=["child"],
        boundary_policy="detach_explicit",
    )
    value["captures"] = [value["captures"][1]]
    value["targets"] = [value["targets"][1]]
    value["targets"][0].update(
        target_parent_node_key=None,
        relationship_disposition="detached_selection_boundary",
        source_relationship_keys=[],
    )
    parsed = parse_tree_proposal(json.dumps(value))
    plan = build_tree_plan(parsed.inventory, parsed.selection, parsed.operation_id)
    assert plan.selected_node_keys == ("child",)
    assert plan.detached_node_keys == ("child",)
    assert parsed.inventory.nodes[0].relative_path is None


@pytest.mark.parametrize(
    "limit,value",
    [
        ("max_proposal_bytes", 100),
        ("max_json_depth", 2),
        ("max_capture_bytes", 99),
        ("max_tree_depth", 1),
    ],
)
def test_declared_smaller_limits_are_enforced(limit, value):
    proposal = json.loads(
        (Path(__file__).parents[1] / "docs/examples/session-tree-v1.json").read_text()
    )
    proposal["limits"][limit] = value
    with pytest.raises(TreeValidationError):
        parse_tree_proposal(json.dumps(proposal))


@pytest.mark.parametrize("field", ["kind", "id", "statement"])
def test_invalid_evidence_types_have_controlled_failure(field):
    value = manifest()
    value["evidence"][0][field] = []
    with pytest.raises(TreeValidationError):
        parse_tree_manifest(json.dumps(value))


@pytest.mark.parametrize(
    "root",
    ["C:/Users/operator/sessions", "C:\\Users\\operator\\sessions", "\\\\server\\share\\sessions"],
)
def test_qualified_identity_preserves_canonical_windows_roots(root):
    inv = replace(inventory(), canonical_root=root)
    plan = build_tree_plan(inv, selection(inv), OPERATION)
    expected = uuid.uuid5(
        uuid.UUID(OPERATION),
        json.dumps(["claude", root, "native-root"], ensure_ascii=False, separators=(",", ":")),
    )
    assert plan.id_map["root"] == "ses_" + expected.hex


@pytest.mark.parametrize(
    "root",
    [
        "relative",
        "/a/../b",
        "/a//b",
        "C:/a/../b",
        "C:\\a\\.\\b",
        "\\\\server",
        "\\\\server\\share\\..\\escape",
    ],
)
def test_noncanonical_roots_are_rejected(root):
    inv = replace(inventory(), canonical_root=root)
    with pytest.raises(TreeValidationError):
        build_tree_plan(inv, selection(inv), OPERATION)


def test_opencode_inventory_can_preserve_observed_parent_relationships():
    inv = replace(inventory(), source_format="opencode")
    plan = build_tree_plan(inv, selection(inv), OPERATION)
    assert plan.parent_map["grandchild"] == "child"
    assert plan.inventory.source_format == "opencode"
