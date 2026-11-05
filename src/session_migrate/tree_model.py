"""Pure, bounded metadata model for persistent session trees.

A parsed declaration is never a capture, a verification receipt, or permission to
publish. All filesystem and native-client verification belongs to the caller.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

MAX_PROPOSAL_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 128
MAX_NODES = 1024
MAX_RELATIONSHIPS = 2048
MAX_TREE_DEPTH = 64
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,95}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class TreeValidationError(ValueError):
    """Untrusted tree metadata is invalid; messages never echo its values."""


def _error(message: str) -> None:
    raise TreeValidationError(message)


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _error(f"Invalid {label}")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _error(f"Invalid UTF-8 in {label}")
    if any(unicodedata.category(char) == "Cc" for char in value):
        _error(f"Control character in {label}")
    return value


def _key(value: Any) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        _error("Invalid metadata key")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _error("Invalid declared SHA256")
    return value


def validate_relative_path(value: Any) -> str:
    """Validate lexical containment; callers must also verify physical containment."""
    path = _text(value, "relative path")
    if (
        path.startswith("/")
        or "\\" in path
        or re.match(r"[A-Za-z]:", path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or str(PurePosixPath(path)) != path
    ):
        _error("Noncanonical relative path")
    return path


def _root(value: Any) -> str:
    root = _text(value, "canonical root")
    if root.startswith("/"):
        if "\\" in root or (
            root != "/"
            and (root.endswith("/") or any(part in {"", ".", ".."} for part in root[1:].split("/")))
        ):
            _error("Noncanonical root")
    elif re.match(r"[A-Za-z]:[\\\\/]", root):
        tail = root[3:]
        if tail and any(part in {"", ".", ".."} for part in re.split(r"[\\\\/]", tail)):
            _error("Noncanonical root")
        if "\\" in root and "/" in root:
            _error("Mixed Windows root separators")
    elif root.startswith("\\\\"):
        parts = root[2:].split("\\")
        if "/" in root or len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
            _error("Noncanonical UNC root")
    else:
        _error("Canonical root must be absolute")
    return root


def validate_tree_title(value: Any) -> str:
    title = _text(value, "title", 1024)
    if len(title.encode("utf-8")) > 1024:
        _error("Title exceeds UTF-8 byte limit")
    return title


def _enum(value: Any, choices: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        _error(f"Invalid {label}")
    return value


def _object(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys():
        _error("Missing metadata fields")
    if value.keys() - required - optional:
        _error("Unknown metadata fields")
    return value


def _list(value: Any, maximum: int, minimum: int = 0) -> list:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _error("Invalid metadata list size")
    return value


def _keys(value: Any, maximum: int = 2048, minimum: int = 0) -> tuple[str, ...]:
    result = tuple(_key(item) for item in _list(value, maximum, minimum))
    if len(result) != len(set(result)):
        _error("Duplicate metadata reference")
    return result


def _integer(value: Any, maximum: int = 9007199254740991) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _error("Invalid metadata integer")
    return value


def _timestamp(value: Any) -> str:
    value = _text(value, "timestamp", 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _error("Invalid timestamp")
    if parsed.tzinfo is None:
        _error("Timestamp requires timezone")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _unique(items: tuple, attribute: str, label: str) -> dict:
    result = {}
    for item in items:
        key = getattr(item, attribute)
        if key in result:
            _error(f"Duplicate {label}")
        result[key] = item
    return result


@dataclass(frozen=True)
class TreeEvidence:
    id: str
    kind: str
    declaration_id: str | None = None
    declared_at: str | None = None
    statement: str | None = None
    relative_path: str | None = None
    component_sha256: str | None = None
    field_pointer: str | None = None
    client_version: str | None = None
    uri: str | None = None
    claim: str | None = None


@dataclass(frozen=True)
class TreeNode:
    key: str
    source_native_id: str
    relative_path: str | None
    title: str
    persistent: bool = True
    persistence_evidence_ids: tuple[str, ...] = ()
    source_parent_status: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "persistence_evidence_ids", tuple(self.persistence_evidence_ids))

    def qualified_identity(self, inventory: TreeInventory) -> tuple[str, str, str]:
        return inventory.source_format, inventory.canonical_root, self.source_native_id


@dataclass(frozen=True)
class TreeRelationship:
    key: str
    parent_node_key: str
    child_node_key: str
    kind: str = "fork_origin"
    provenance: str = "manual"
    evidence_ids: tuple[str, ...] = ()
    shared_prefix: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if self.shared_prefix is not None:
            object.__setattr__(self, "shared_prefix", _freeze(dict(self.shared_prefix)))


@dataclass(frozen=True)
class TreeInventory:
    source_format: str
    canonical_root: str
    root_node_key: str
    nodes: tuple[TreeNode, ...]
    relationships: tuple[TreeRelationship, ...]
    evidence: tuple[TreeEvidence, ...]
    scope: str = "explicit_manifest"
    completeness: str = "partial"
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("nodes", "relationships", "evidence", "evidence_ids"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class TreeSelection:
    mode: str
    anchor_node_key: str
    included_node_keys: tuple[str, ...] = ()
    hierarchy_relationship_keys: tuple[str, ...] = ()
    boundary_policy: str = "require_closed"

    def __post_init__(self) -> None:
        for name in ("included_node_keys", "hierarchy_relationship_keys"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class TreePlan:
    operation_id: str
    inventory: TreeInventory
    selection: TreeSelection
    ordered_nodes: tuple[TreeNode, ...]
    id_map: Mapping[str, str]
    parent_map: Mapping[str, str | None]
    selected_relationships: tuple[TreeRelationship, ...]
    detached_node_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("ordered_nodes", "selected_relationships", "detached_node_keys"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("id_map", "parent_map"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def selected_node_keys(self) -> tuple[str, ...]:
        return tuple(node.key for node in self.ordered_nodes)


@dataclass(frozen=True)
class TreeManifest:
    inventory: TreeInventory
    selection: TreeSelection
    operation_id: str
    synthetic: bool
    # Immutable original metadata, including manual statements verbatim.
    declared_metadata: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "declared_metadata", _freeze(self.declared_metadata))

    @property
    def publication_blocker_codes(self) -> tuple[str, ...]:
        # No saved document can establish a current live verification capability.
        return ("SYNTHETIC_EVIDENCE_ONLY",) if self.synthetic else ("LIVE_PREFLIGHT_REQUIRED",)


def _evidence(value: dict) -> TreeEvidence:
    if not isinstance(value, dict):
        _error("Invalid evidence")
    kind = value.get("kind")
    fields = {
        "manual_declaration": {"id", "kind", "declaration_id", "declared_at", "statement"},
        "native_metadata": {
            "id",
            "kind",
            "relative_path",
            "component_sha256",
            "field_pointer",
            "client_version",
        },
        "external_report": {"id", "kind", "uri", "claim"},
    }
    if not isinstance(kind, str) or kind not in fields:
        _error("Invalid evidence kind")
    _object(value, fields[kind])
    _key(value["id"])
    if kind == "manual_declaration":
        _key(value["declaration_id"])
        _timestamp(value["declared_at"])
        _text(value["statement"], "manual statement", 1024)
    elif kind == "native_metadata":
        validate_relative_path(value["relative_path"])
        _sha(value["component_sha256"])
        if not _text(value["field_pointer"], "field pointer", 1024).startswith("/"):
            _error("Invalid field pointer")
        _text(value["client_version"], "client version", 64)
    else:
        _text(value["uri"], "evidence URI", 2048)
        _text(value["claim"], "external claim", 1024)
    return TreeEvidence(**value)


def _prefix(value: Any) -> None:
    if value is None:
        return
    _object(
        value,
        {
            "policy",
            "parent_capture_sha256",
            "child_capture_sha256",
            "logical_unit_count",
            "unit_chain_sha256",
            "tool_link_count",
            "tool_link_chain_sha256",
            "boundary",
        },
    )
    _text(value["policy"], "prefix policy", 96)
    _enum(value["boundary"], {"proven_closed", "unproven"}, "prefix boundary")
    for key in ("logical_unit_count", "tool_link_count"):
        _integer(value[key])
    for key in (
        "parent_capture_sha256",
        "child_capture_sha256",
        "unit_chain_sha256",
        "tool_link_chain_sha256",
    ):
        _sha(value[key])


def _relationship(value: dict) -> TreeRelationship:
    _object(
        value,
        {
            "key",
            "parent_node_key",
            "child_node_key",
            "kind",
            "provenance",
            "evidence_ids",
            "shared_prefix",
        },
    )
    for key in ("key", "parent_node_key", "child_node_key"):
        _key(value[key])
    _enum(value["kind"], {"organizational_parent", "fork_origin"}, "relationship kind")
    _enum(value["provenance"], {"native", "manual"}, "relationship provenance")
    refs = _keys(value["evidence_ids"], 16, 1)
    _prefix(value["shared_prefix"])
    return TreeRelationship(**{**value, "evidence_ids": refs})


def _selection(value: dict) -> TreeSelection:
    _object(
        value,
        {
            "mode",
            "anchor_node_key",
            "included_node_keys",
            "hierarchy_relationship_keys",
            "boundary_policy",
        },
    )
    _enum(value["mode"], {"single", "subtree", "full_tree"}, "selection mode")
    _enum(value["boundary_policy"], {"require_closed", "detach_explicit"}, "boundary policy")
    _key(value["anchor_node_key"])
    return TreeSelection(
        value["mode"],
        value["anchor_node_key"],
        _keys(value["included_node_keys"], MAX_NODES, 1),
        _keys(value["hierarchy_relationship_keys"]),
        value["boundary_policy"],
    )


def _node(value: dict) -> TreeNode:
    _object(
        value,
        {
            "key",
            "source_native_id",
            "relative_path",
            "title",
            "persistent",
            "persistence_evidence_ids",
            "source_parent_status",
        },
    )
    validate_relative_path(value["relative_path"])
    validate_tree_title(value["title"])
    return TreeNode(
        **{**value, "persistence_evidence_ids": _keys(value["persistence_evidence_ids"], 16, 1)}
    )


def validate_tree_inventory(inventory: TreeInventory) -> None:
    """Validate declarations and the complete fact DAG, without trusting declared hashes."""
    _enum(inventory.source_format, {"claude", "codex", "opencode"}, "source format")
    _root(inventory.canonical_root)
    _key(inventory.root_node_key)
    _enum(inventory.scope, {"explicit_manifest", "native_query"}, "inventory scope")
    _enum(
        inventory.completeness,
        {"partial", "declared_complete", "verified_complete"},
        "inventory completeness",
    )
    if not 1 <= len(inventory.nodes) <= MAX_NODES:
        _error("Tree node limit exceeded or inventory empty")
    if len(inventory.relationships) > MAX_RELATIONSHIPS:
        _error("Tree relationship limit exceeded")
    if len(inventory.evidence) > 4096:
        _error("Tree evidence limit exceeded")
    nodes = _unique(inventory.nodes, "key", "node key")
    evidence = _unique(inventory.evidence, "id", "evidence key")
    _unique(inventory.relationships, "key", "relationship key")
    for item in inventory.evidence:
        _evidence({key: value for key, value in vars(item).items() if value is not None})
    if inventory.root_node_key not in nodes:
        _error("Inventory root is absent")
    identities, paths = set(), set()
    for node in inventory.nodes:
        _key(node.key)
        _text(node.source_native_id, "native source ID", 256)
        if node.relative_path is not None:
            validate_relative_path(node.relative_path)
        validate_tree_title(node.title)
        _enum(
            node.source_parent_status,
            {"known_root", "known_parent", "unknown"},
            "source parent status",
        )
        if node.persistent is not True:
            _error("Nonpersistent node")
        if node.source_native_id in identities:
            _error("Duplicate qualified native identity")
        if node.relative_path is not None and node.relative_path in paths:
            _error("Duplicate source component path")
        identities.add(node.source_native_id)
        if node.relative_path is not None:
            paths.add(node.relative_path)
        refs = _keys(list(node.persistence_evidence_ids), 16, 1)
        if any(ref not in evidence or evidence[ref].kind == "external_report" for ref in refs):
            _error("Persistence lacks native or manual evidence")
    inventory_refs = _keys(list(inventory.evidence_ids), 32, 1)
    if any(ref not in evidence for ref in inventory_refs):
        _error("Absent inventory evidence reference")
    if inventory.completeness == "declared_complete" and not any(
        evidence[ref].kind == "manual_declaration" for ref in inventory_refs
    ):
        _error("Declared completeness lacks manual declaration")
    if inventory.completeness == "verified_complete" and (
        inventory.scope != "native_query"
        or not any(evidence[ref].kind == "native_metadata" for ref in inventory_refs)
    ):
        _error("Verified completeness lacks native enumeration declaration")
    children = {key: set() for key in nodes}
    indegree = {key: 0 for key in nodes}
    observed_parents = {key: set() for key in nodes}
    for edge in inventory.relationships:
        _key(edge.key)
        _enum(edge.kind, {"organizational_parent", "fork_origin"}, "relationship kind")
        _enum(edge.provenance, {"native", "manual"}, "relationship provenance")
        _prefix(dict(edge.shared_prefix) if edge.shared_prefix is not None else None)
        if edge.parent_node_key not in nodes or edge.child_node_key not in nodes:
            _error("Relationship references absent node")
        if edge.parent_node_key == edge.child_node_key:
            _error("Self relationship")
        refs = _keys(list(edge.evidence_ids), 16, 1)
        expected = "native_metadata" if edge.provenance == "native" else "manual_declaration"
        if any(ref not in evidence for ref in refs) or not any(
            evidence[ref].kind == expected for ref in refs
        ):
            _error("Relationship lacks evidence matching provenance")
        parent, child = edge.parent_node_key, edge.child_node_key
        observed_parents[child].add(parent)
        if child not in children[parent]:
            children[parent].add(child)
            indegree[child] += 1
    for node in inventory.nodes:
        if node.source_parent_status == "known_root" and observed_parents[node.key]:
            _error("Known root has observed parent")
        if node.source_parent_status == "known_parent" and not observed_parents[node.key]:
            _error("Known parent is absent from inventory")
    pending = deque(sorted(key for key, degree in indegree.items() if degree == 0))
    visited = 0
    while pending:
        parent = pending.popleft()
        visited += 1
        for child in sorted(children[parent]):
            indegree[child] -= 1
            if indegree[child] == 0:
                pending.append(child)
    if visited != len(nodes):
        _error("Cycle in relationship facts")


def deterministic_tree_id(
    operation_id: str, source_format: str, canonical_root: str, source_native_id: str
) -> str:
    try:
        namespace = uuid.UUID(operation_id)
    except (ValueError, AttributeError, TypeError):
        _error("Invalid operation UUID")
    name = json.dumps(
        [source_format, canonical_root, source_native_id], ensure_ascii=False, separators=(",", ":")
    )
    return "ses_" + uuid.uuid5(namespace, name).hex


def build_tree_plan(
    inventory: TreeInventory, selection: TreeSelection, operation_id: str
) -> TreePlan:
    """Produce a deterministic parent-first plan, never a publication capability."""
    validate_tree_inventory(inventory)
    _enum(selection.mode, {"single", "subtree", "full_tree"}, "selection mode")
    _enum(selection.boundary_policy, {"require_closed", "detach_explicit"}, "boundary policy")
    _key(selection.anchor_node_key)
    nodes = {node.key: node for node in inventory.nodes}
    relationships = {edge.key: edge for edge in inventory.relationships}
    hierarchy_keys = _keys(list(selection.hierarchy_relationship_keys))
    if any(key not in relationships for key in hierarchy_keys):
        _error("Absent hierarchy relationship")
    children = {key: set() for key in nodes}
    parents: dict[str, str | None] = dict.fromkeys(nodes)
    for key in hierarchy_keys:
        edge = relationships[key]
        old = parents[edge.child_node_key]
        if old is not None and old != edge.parent_node_key:
            _error("Multiple hierarchy parents")
        parents[edge.child_node_key] = edge.parent_node_key
        children[edge.parent_node_key].add(edge.child_node_key)
    if parents[inventory.root_node_key] is not None:
        _error("Declared inventory root has hierarchy parent")
    if nodes[inventory.root_node_key].source_parent_status != "known_root":
        _error("Unknown parent cannot establish inventory root")
    pending = deque([(inventory.root_node_key, 0)])
    ordered = []
    while pending:
        key, depth = pending.popleft()
        if depth > MAX_TREE_DEPTH:
            _error("Tree depth limit exceeded")
        ordered.append(key)
        pending.extend((child, depth + 1) for child in sorted(children[key]))
    if len(ordered) != len(nodes):
        _error("Hierarchy contains disconnected or orphan nodes")
    anchor = selection.anchor_node_key
    if anchor not in nodes:
        _error("Selection anchor is absent")
    if selection.mode == "full_tree":
        if anchor != inventory.root_node_key or inventory.completeness == "partial":
            _error("Full tree requires complete inventory and root anchor")
        included = set(nodes)
    else:
        included = {anchor}
        if selection.mode == "subtree":
            pending = deque([anchor])
            while pending:
                for child in children[pending.popleft()]:
                    included.add(child)
                    pending.append(child)
    declared = _keys(list(selection.included_node_keys), MAX_NODES)
    if declared and set(declared) != included:
        _error("Selection differs from exact mode closure")
    detached = ()
    if parents[anchor] is not None and parents[anchor] not in included:
        if selection.boundary_policy != "detach_explicit":
            _error("Selection boundary requires explicit detachment")
        detached = (anchor,)
    selected = tuple(nodes[key] for key in ordered if key in included)
    ids = {
        node.key: deterministic_tree_id(operation_id, *node.qualified_identity(inventory))
        for node in selected
    }
    if len(set(ids.values())) != len(ids):
        _error("Deterministic target ID collision")
    selected_parents = {
        key: (parent if parent in included else None)
        for key, parent in parents.items()
        if key in included
    }
    selected_edges = tuple(
        relationships[key]
        for key in hierarchy_keys
        if relationships[key].parent_node_key in included
        and relationships[key].child_node_key in included
    )
    return TreePlan(
        str(uuid.UUID(operation_id)),
        inventory,
        selection,
        selected,
        MappingProxyType(ids),
        MappingProxyType(selected_parents),
        selected_edges,
        detached,
    )


def _load_json(payload: bytes | str) -> dict:
    if isinstance(payload, str):
        if len(payload) > MAX_PROPOSAL_BYTES:
            _error("Proposal byte limit exceeded")
        try:
            raw = payload.encode("utf-8")
        except UnicodeError:
            _error("Invalid JSON UTF-8")
    elif isinstance(payload, bytes):
        raw = payload
    else:
        _error("JSON input must be bytes or text")
    if len(raw) > MAX_PROPOSAL_BYTES:
        _error("Proposal byte limit exceeded")
    # Bound lexical nesting before the decoder can recurse.
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                _error("JSON depth limit exceeded")
        elif byte in (93, 125):
            depth -= 1

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                _error("Duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        _error("Nonfinite JSON number")

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        _error("Invalid bounded tree JSON")
    if not isinstance(value, dict):
        _error("Tree JSON must be an object")
    return value


def parse_tree_manifest(payload: bytes | str) -> TreeManifest:
    """Parse an explicit private inventory; no paths are opened or commands run."""
    value = _load_json(payload)
    if value.get("kind") == "session-tree-proposal":
        result = _parse_proposal(value)
        _declared_input_limits(value, payload)
        return result
    _object(
        value,
        {
            "schema_version",
            "kind",
            "operation_id",
            "synthetic",
            "source_format",
            "canonical_root",
            "root_node_key",
            "nodes",
            "relationships",
            "evidence",
            "scope",
            "completeness",
            "evidence_ids",
            "selection",
        },
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _error("Unsupported tree schema version")
    if value["kind"] != "session-tree-manifest" or type(value["synthetic"]) is not bool:
        _error("Invalid tree manifest kind or synthetic marker")
    inventory = TreeInventory(
        value["source_format"],
        value["canonical_root"],
        value["root_node_key"],
        tuple(_node(item) for item in _list(value["nodes"], MAX_NODES, 1)),
        tuple(_relationship(item) for item in _list(value["relationships"], MAX_RELATIONSHIPS)),
        tuple(_evidence(item) for item in _list(value["evidence"], 4096)),
        value["scope"],
        value["completeness"],
        _keys(value["evidence_ids"], 32, 1),
    )
    selection = _selection(value["selection"])
    build_tree_plan(inventory, selection, value["operation_id"])
    return TreeManifest(
        inventory, selection, value["operation_id"], value["synthetic"], _freeze(value)
    )


def _namespace(value: Any) -> None:
    _object(
        value,
        {
            "format",
            "profile_id",
            "canonical_root",
            "root_witness_sha256",
            "profile_fingerprint_sha256",
            "client_version",
        },
    )
    _enum(value["format"], {"claude", "codex", "opencode"}, "namespace format")
    _key(value["profile_id"])
    _root(value["canonical_root"])
    _sha(value["root_witness_sha256"])
    _sha(value["profile_fingerprint_sha256"])
    _text(value["client_version"], "client version", 64)


def _capture(value: Any) -> None:
    _object(
        value,
        {
            "node_key",
            "snapshot_sha256",
            "source_revision_sha256",
            "components",
            "history_mode",
            "canonical_policy",
            "logical_unit_count",
            "unit_chain_sha256",
            "tool_link_count",
            "tool_link_chain_sha256",
            "snapshot_policy",
        },
    )
    _key(value["node_key"])
    _enum(value["history_mode"], {"complete_local", "verified_materialized_prefix"}, "history mode")
    _text(value["canonical_policy"], "canonical policy", 96)
    if value["snapshot_policy"] != "tree-source-components-v1":
        _error("Unsupported snapshot policy")
    for key in (
        "snapshot_sha256",
        "source_revision_sha256",
        "unit_chain_sha256",
        "tool_link_chain_sha256",
    ):
        _sha(value[key])
    for key in ("logical_unit_count", "tool_link_count"):
        _integer(value[key])
    paths, total = set(), 0
    for component in _list(value["components"], 1024, 1):
        _object(component, {"relative_path", "sha256", "byte_count"})
        path = validate_relative_path(component["relative_path"])
        if path in paths:
            _error("Duplicate capture component path")
        paths.add(path)
        _sha(component["sha256"])
        total += _integer(component["byte_count"], 256 * 1024 * 1024)
    if total > 256 * 1024 * 1024:
        _error("Declared capture size limit exceeded")


def _target(value: Any) -> None:
    _object(
        value,
        {
            "node_key",
            "native_id",
            "target_parent_node_key",
            "relationship_disposition",
            "source_relationship_keys",
            "title",
            "source_capture_sha256",
            "expected_logical_unit_count",
            "expected_unit_chain_sha256",
            "expected_tool_link_count",
            "expected_tool_link_chain_sha256",
            "history_policy",
            "loss_codes",
        },
    )
    _key(value["node_key"])
    if not isinstance(value["native_id"], str) or not re.fullmatch(
        r"ses_[0-9a-f]{32}", value["native_id"]
    ):
        _error("Invalid target ID")
    if value["target_parent_node_key"] is not None:
        _key(value["target_parent_node_key"])
    _enum(
        value["relationship_disposition"],
        {"native_parent", "root", "detached_selection_boundary", "retained_only", "unsupported"},
        "relationship disposition",
    )
    refs = _keys(value["source_relationship_keys"], 16)
    if value["relationship_disposition"] == "native_parent":
        if value["target_parent_node_key"] is None or not refs:
            _error("Native target parent lacks relationship")
    elif value["target_parent_node_key"] is not None:
        _error("Target disposition cannot have native parent")
    _object(value["title"], {"requested", "mode"})
    validate_tree_title(value["title"]["requested"])
    _enum(value["title"]["mode"], {"native", "local_alias"}, "title mode")
    for key in (
        "source_capture_sha256",
        "expected_unit_chain_sha256",
        "expected_tool_link_chain_sha256",
    ):
        _sha(value[key])
    for key in ("expected_logical_unit_count", "expected_tool_link_count"):
        _integer(value[key])
    if value["history_policy"] != "independent_materialized_history":
        _error("Unsupported target history policy")
    loss_codes = [_text(item, "loss code", 96) for item in _list(value["loss_codes"], 128)]
    if len(loss_codes) != len(set(loss_codes)):
        _error("Duplicate loss code")


def _parse_proposal(value: dict) -> TreeManifest:
    _object(
        value,
        {
            "schema_version",
            "kind",
            "operation_id",
            "created_at",
            "synthetic",
            "source_namespace",
            "target_namespace",
            "inventory",
            "selection",
            "nodes",
            "relationships",
            "evidence",
            "captures",
            "targets",
            "id_strategy",
            "limits",
            "preflight",
            "journal_contract",
        },
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _error("Unsupported tree schema version")
    if type(value["synthetic"]) is not bool:
        _error("Invalid synthetic marker")
    _timestamp(value["created_at"])
    _namespace(value["source_namespace"])
    _namespace(value["target_namespace"])
    if value["target_namespace"]["format"] != "opencode":
        _error("Unsupported tree destination")
    if value["id_strategy"] != "uuid5_operation_qualified_source_v1":
        _error("Unsupported tree ID strategy")
    if value["journal_contract"] != "durable-per-node-v1-inspect-only-recovery":
        _error("Unsupported tree journal contract")
    limits = _object(
        value["limits"],
        {
            "max_nodes",
            "max_tree_depth",
            "max_capture_bytes",
            "max_total_capture_bytes",
            "max_proposal_bytes",
            "max_json_depth",
        },
    )
    ceilings = {
        "max_nodes": MAX_NODES,
        "max_tree_depth": MAX_TREE_DEPTH,
        "max_capture_bytes": 256 * 1024 * 1024,
        "max_total_capture_bytes": 1024 * 1024 * 1024,
        "max_proposal_bytes": MAX_PROPOSAL_BYTES,
        "max_json_depth": MAX_JSON_DEPTH,
    }
    for key, ceiling in ceilings.items():
        if _integer(limits[key], ceiling) == 0:
            _error("Tree limits must be positive")
    summary = _object(
        value["inventory"], {"scope", "completeness", "root_node_key", "evidence_ids"}
    )
    captures, capture_bytes = {}, 0
    for item in _list(value["captures"], MAX_NODES, 1):
        _capture(item)
        if item["node_key"] in captures:
            _error("Duplicate node capture")
        captures[item["node_key"]] = item
        size = sum(component["byte_count"] for component in item["components"])
        if size > limits["max_capture_bytes"]:
            _error("Declared capture exceeds requested limit")
        capture_bytes += size
    if capture_bytes > limits["max_total_capture_bytes"]:
        _error("Declared total capture size limit exceeded")
    targets = {}
    for item in _list(value["targets"], MAX_NODES, 1):
        _target(item)
        if item["node_key"] in targets:
            _error("Duplicate node target")
        targets[item["node_key"]] = item
    selection = _selection(value["selection"])
    selected = set(selection.included_node_keys)
    if set(captures) != selected or set(targets) != selected:
        _error("Captures and targets must cover exactly the selected nodes")
    nodes = []
    for item in _list(value["nodes"], limits["max_nodes"], 1):
        _object(
            item,
            {
                "key",
                "source_native_id",
                "persistent",
                "persistence_evidence_ids",
                "source_parent_status",
            },
        )
        _key(item["key"])
        capture = captures.get(item["key"])
        target = targets.get(item["key"])
        # Unselected public metadata deliberately has no source component path.
        nodes.append(
            TreeNode(
                **{
                    **item,
                    "persistence_evidence_ids": _keys(item["persistence_evidence_ids"], 16, 1),
                },
                relative_path=capture["components"][0]["relative_path"] if capture else None,
                title=target["title"]["requested"] if target else item["source_native_id"],
            )
        )
    inventory = TreeInventory(
        value["source_namespace"]["format"],
        value["source_namespace"]["canonical_root"],
        summary["root_node_key"],
        tuple(nodes),
        tuple(_relationship(item) for item in _list(value["relationships"], MAX_RELATIONSHIPS)),
        tuple(_evidence(item) for item in _list(value["evidence"], 4096)),
        summary["scope"],
        summary["completeness"],
        _keys(summary["evidence_ids"], 32, 1),
    )
    plan = build_tree_plan(inventory, selection, value["operation_id"])
    depths = {}
    for node in plan.ordered_nodes:
        parent = plan.parent_map[node.key]
        depths[node.key] = depths[parent] + 1 if parent else 0
    if max(depths.values()) > limits["max_tree_depth"]:
        _error("Tree exceeds requested depth limit")
    edge_keys = set(selection.hierarchy_relationship_keys)
    for key, target in targets.items():
        if target["native_id"] != plan.id_map[key]:
            _error("Declared target ID differs from deterministic plan")
        if target["target_parent_node_key"] != plan.parent_map[key]:
            _error("Declared target parent differs from selection")
        disposition = target["relationship_disposition"]
        if key in plan.detached_node_keys:
            if disposition != "detached_selection_boundary":
                _error("Detached boundary disposition is missing")
        elif plan.parent_map[key] is None and disposition not in {"root", "unsupported"}:
            _error("Invalid root target disposition")
        elif plan.parent_map[key] is not None and disposition != "native_parent":
            _error("Selected parent requires native parent disposition")
        expected_edges = {
            edge.key for edge in plan.selected_relationships if edge.child_node_key == key
        }
        if set(target["source_relationship_keys"]) != expected_edges:
            _error("Target relationship references differ from selected hierarchy")
        if not set(target["source_relationship_keys"]) <= edge_keys:
            _error("Target references unselected relationship")
        capture = captures[key]
        if target["source_capture_sha256"] != capture["snapshot_sha256"]:
            _error("Target capture reference mismatch")
        for suffix in (
            "logical_unit_count",
            "unit_chain_sha256",
            "tool_link_count",
            "tool_link_chain_sha256",
        ):
            if target["expected_" + suffix] != capture[suffix]:
                _error("Target projection declaration mismatch")
        if any(
            edge.kind == "fork_origin"
            for edge in plan.selected_relationships
            if edge.child_node_key == key
        ) and ("FORK_ORIGIN_RETAINED_IN_MIGRATION_METADATA_ONLY" not in target["loss_codes"]):
            _error("Fork origin projection requires explicit loss code")
    preflight = _object(value["preflight"], {"status", "blocker_codes", "check_receipt_sha256"})
    _enum(preflight["status"], {"blocked", "ready_for_live_review"}, "preflight status")
    blockers = [_text(item, "blocker code", 96) for item in _list(preflight["blocker_codes"], 256)]
    if len(set(blockers)) != len(blockers):
        _error("Duplicate preflight blocker")
    if preflight["check_receipt_sha256"] is not None:
        _sha(preflight["check_receipt_sha256"])
    if value["synthetic"] and preflight["status"] != "blocked":
        _error("Synthetic proposal cannot declare ready preflight")
    if preflight["status"] == "blocked" and not blockers:
        _error("Blocked preflight lacks blocker")
    if preflight["status"] == "ready_for_live_review" and (
        blockers
        or preflight["check_receipt_sha256"] is None
        or any(target["relationship_disposition"] == "unsupported" for target in targets.values())
    ):
        _error("Incoherent ready preflight declaration")
    return TreeManifest(
        inventory, selection, value["operation_id"], value["synthetic"], _freeze(value)
    )


def parse_tree_proposal(payload: bytes | str) -> TreeManifest:
    """Parse descriptive public metadata, retaining no live verification authority."""
    value = _load_json(payload)
    if value.get("kind") != "session-tree-proposal":
        _error("Expected session tree proposal")
    result = _parse_proposal(value)
    _declared_input_limits(value, payload)
    return result


def _declared_input_limits(value: dict, payload: bytes | str) -> None:
    limits = value["limits"]
    size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
    if size > limits["max_proposal_bytes"]:
        _error("Proposal exceeds requested byte limit")
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > limits["max_json_depth"]:
            _error("JSON exceeds requested depth limit")
        children = item.values() if isinstance(item, dict) else item
        for child in children:
            if isinstance(child, (dict, list)):
                pending.append((child, depth + 1))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {name: _plain(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def inventory_to_dict(inventory: TreeInventory) -> dict[str, Any]:
    """Return metadata only; statements and provenance retain the declared content."""
    return _plain(inventory)


def plan_to_dict(plan: TreePlan) -> dict[str, Any]:
    """Return the reviewable selected mapping without captures or executable content."""
    return {
        "operation_id": plan.operation_id,
        "source_format": plan.inventory.source_format,
        "canonical_root": plan.inventory.canonical_root,
        "selection": _plain(plan.selection),
        "ordered_node_keys": list(plan.selected_node_keys),
        "id_map": dict(plan.id_map),
        "parent_map": dict(plan.parent_map),
        "selected_relationships": _plain(plan.selected_relationships),
        "detached_node_keys": list(plan.detached_node_keys),
        "verification": "not_performed",
    }
