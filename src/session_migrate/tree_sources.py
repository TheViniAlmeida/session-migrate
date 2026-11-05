"""Read-only, bounded inventories of explicitly rooted persistent sessions.

The returned model contains metadata only. Native metadata is read from guarded
captures, never from picker labels, message-parent UUIDs or text similarity.
Claude CLI does not record a session parent; an operator declaration is the
functional route for those relationships. Undocumented Desktop rows are not an
approved native adapter.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.jsonl import DEFAULT_MAX_RECORD_BYTES, DEFAULT_MAX_RECORDS
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_source
from session_migrate.tree_model import (
    MAX_NODES,
    TreeEvidence,
    TreeInventory,
    TreeNode,
    TreeRelationship,
    TreeValidationError,
    validate_relative_path,
    validate_tree_inventory,
)

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 16384
MAX_DIRECTORY_DEPTH = 32
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,95}\Z")
_APPROVED = {
    "claude": claude.READABLE_CLAUDE_VERSIONS,
    "codex": {"0.166.0"},
    "opencode": {"2.2.5"},
}


class TreeSourceError(SessionMigrateError):
    """Content-safe source failure; never includes a native path or row value."""


def _fail(message: str) -> None:
    raise TreeSourceError(message)


def _text(value: object, *, maximum: int = 1024) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        _fail("Invalid tree source metadata text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail("Invalid tree source metadata UTF-8")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("Control character in tree source metadata")
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail("Invalid native source ID")
    return value


def _key(value: object) -> str:
    if type(value) is not str or not _KEY.fullmatch(value):
        _fail("Invalid source metadata key")
    return value


def _object(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys():
        _fail("Missing tree source metadata fields")
    if value.keys() - required - optional:
        _fail("Unknown tree source metadata fields")
    return value


def _list(value: object, maximum: int = MAX_NODES) -> list:
    if type(value) is not list or len(value) > maximum:
        _fail("Invalid tree source metadata list")
    return value


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            _fail("Duplicate JSON metadata key")
        result[key] = value
    return result


def _json(data: bytes, *, limit: int) -> dict:
    if len(data) > limit:
        _fail("Tree source JSON exceeds byte limit")
    try:
        value = json.loads(
            data, object_pairs_hook=_pairs, parse_constant=lambda _: _fail("Invalid JSON constant")
        )
    except (ValueError, UnicodeError, RecursionError):
        _fail("Invalid tree source JSON")
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 128:
            _fail("Tree source JSON exceeds depth limit")
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
    if not isinstance(value, dict):
        _fail("Tree source JSON must be an object")
    return value


def _format(value: AgentFormat | str) -> str:
    name = value.value if isinstance(value, AgentFormat) else value
    if type(name) is not str or name not in _APPROVED:
        _fail("Tree source format is unsupported")
    return name


def _root(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or value == Path("/"):
        _fail("Tree source requires an explicit absolute root")
    if ".." in value.parts or "\\" in str(value):
        _fail("Tree source root is noncanonical")
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(value.anchor, flags)
        for component in value.parts[1:]:
            following = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        opened = os.fstat(descriptor)
        named = os.stat(value, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode) or (opened.st_dev, opened.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            _fail("Tree source root changed")
    except OSError:
        _fail("Tree source root filesystem guard failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return value


def _relative(value: object) -> str:
    try:
        return validate_relative_path(value)
    except TreeValidationError:
        _fail("Tree source path must remain canonical and inside its root")


@dataclass(frozen=True, slots=True, repr=False)
class _Metadata:
    native_id: str
    parent_id: str | None
    parent_status: str
    relationship_kind: str
    field_pointer: str
    client_version: str
    title: str | None = None
    subagent: bool = False


@dataclass(slots=True)
class _ReadBudget:
    remaining: int
    root_device: int
    root_inode: int
    source_limit: int = MAX_SOURCE_BYTES

    @classmethod
    def for_root(
        cls,
        root: Path,
        *,
        max_source_bytes=MAX_SOURCE_BYTES,
        max_total_source_bytes=MAX_TOTAL_SOURCE_BYTES,
    ) -> _ReadBudget:
        if (
            type(max_source_bytes) is not int
            or not 0 < max_source_bytes <= MAX_SOURCE_BYTES
            or type(max_total_source_bytes) is not int
            or not 0 < max_total_source_bytes <= MAX_TOTAL_SOURCE_BYTES
        ):
            _fail("Invalid tree source capture budget")
        try:
            value = os.stat(root, follow_symlinks=False)
        except OSError:
            _fail("Tree source root changed before capture")
        if not stat.S_ISDIR(value.st_mode):
            _fail("Tree source root changed before capture")
        return cls(max_total_source_bytes, value.st_dev, value.st_ino, max_source_bytes)


def _records(data: bytes):
    """Strict JSONL decoding, including duplicate keys and per-row depth limits."""
    stream = io.BytesIO(data)
    index = 0
    while raw := stream.readline(DEFAULT_MAX_RECORD_BYTES + 1):
        if len(raw) > DEFAULT_MAX_RECORD_BYTES:
            _fail("Tree source record byte limit exceeded")
        if not raw.strip():
            continue
        if index >= DEFAULT_MAX_RECORDS:
            _fail("Tree source record count limit exceeded")
        yield index, _json(raw, limit=DEFAULT_MAX_RECORD_BYTES)
        index += 1


def _metadata(data: bytes, source_path: Path, source_format: str, client_version: str) -> _Metadata:
    if source_format == "opencode":
        document = _json(data, limit=MAX_SOURCE_BYTES)
        info = document.get("info")
        if not isinstance(info, dict) or not isinstance(document.get("messages"), list):
            _fail("OpenCode tree source requires its public export shape")
        native_id = _identifier(info.get("id"))
        parent = info.get("parentID")
        if parent is not None:
            parent = _identifier(parent)
        title = info.get("title")
        if title is not None:
            title = _text(title)
        return _Metadata(
            native_id,
            parent,
            "known_parent" if parent else "known_root",
            "organizational_parent",
            "/info/parentID",
            client_version,
            title,
        )
    # Iteration is bounded. JSON rows are transient and never leave this adapter.
    identities, versions = set(), set()
    first_metadata = None
    sidechain = False
    try:
        for index, row in _records(data):
            if source_format == "claude":
                if row.get("sessionId") is not None:
                    identities.add(_identifier(row["sessionId"]))
                if row.get("version") is not None:
                    versions.add(_text(row["version"], maximum=64))
                if row.get("isSidechain") is not None:
                    if type(row["isSidechain"]) is not bool:
                        _fail("Claude sidechain marker is invalid")
                    sidechain |= row["isSidechain"]
            elif index == 0:
                if row.get("type") != "session_meta" or not isinstance(row.get("payload"), dict):
                    _fail("Codex tree source must begin with session metadata")
                first_metadata = row["payload"]
            elif row.get("type") == "session_meta":
                _fail("Codex tree source has repeated identity metadata")
    except (SessionMigrateError, RecursionError):
        _fail("Invalid tree source transcript metadata")
    if source_format == "claude":
        if (
            len(identities) != 1
            or client_version not in versions
            or not versions <= _APPROVED["claude"]
        ):
            _fail("Claude transcript identity or client version is ambiguous")
        # parentUuid is a message pointer; isSidechain=false does not prove a root.
        return _Metadata(
            next(iter(identities)),
            None,
            "unknown",
            "fork_origin",
            "/sessionId",
            client_version,
            subagent=sidechain,
        )
    if first_metadata is None:
        _fail("Codex tree source has no identity metadata")
    payload = first_metadata
    native_id = _identifier(payload.get("id"))
    if payload.get("cli_version") != client_version:
        _fail("Codex tree source client version differs from the selected adapter")
    parent = payload.get("forked_from_id")
    if parent is not None:
        parent = _identifier(parent)
    subagent_parent = payload.get("parent_thread_id")
    if subagent_parent is not None:
        _identifier(subagent_parent)
    source = payload.get("source")
    subagent = subagent_parent is not None or (
        isinstance(source, dict) and ("subagent" in source or "subAgent" in source)
    )
    if not subagent and source not in ("cli", "exec", "vscode", "appServer"):
        _fail("Codex source is not an approved persistent session source")
    if payload.get("attached_thread") is not None and type(payload["attached_thread"]) is not bool:
        _fail("Codex attached-thread marker is invalid")
    # session_id and history_base describe grouping/storage, never logical parentage.
    return _Metadata(
        native_id,
        parent,
        "known_parent" if parent else "known_root",
        "fork_origin",
        "/0/payload/forked_from_id",
        client_version,
        subagent=subagent,
    )


def _capture_metadata(
    root: Path, relative_path: str, source_format: str, client_version: str, budget: _ReadBudget
):
    relative_path = _relative(relative_path)
    allowance = min(budget.source_limit, budget.remaining)
    if allowance <= 0:
        _fail("Tree source aggregate byte limit exceeded")
    snapshot = capture_source(
        SourceCaptureContext(AgentFormat(source_format), root / relative_path),
        byte_limit=allowance,
    )
    root_witness = next((item for item in snapshot.ancestors if item.path == root), None)
    if root_witness is None or (root_witness.device, root_witness.inode) != (
        budget.root_device,
        budget.root_inode,
    ):
        _fail("Tree source root changed during inventory capture")
    data = snapshot.raw_bytes
    compressed = source_format == "codex" and relative_path.endswith(".jsonl.zst")
    if source_format == "codex":
        from session_migrate.codex_storage import decode_codex_storage

        decoded_limit = allowance - len(data) if compressed else allowance
        if decoded_limit <= 0:
            _fail("Tree source aggregate byte limit exceeded")
        data = decode_codex_storage(data, root / relative_path, max_decoded_bytes=decoded_limit)
    budget.remaining -= len(snapshot.raw_bytes) + (len(data) if compressed else 0)
    if budget.remaining < 0:
        _fail("Tree source aggregate byte limit exceeded")
    metadata = _metadata(data, root / relative_path, source_format, client_version)
    return metadata, snapshot.authority_sha256


def _validate(inventory: TreeInventory) -> TreeInventory:
    try:
        validate_tree_inventory(inventory)
    except TreeValidationError as exc:
        raise TreeSourceError(str(exc)) from None
    return inventory


def inventory_from_explicit_manifest(
    manifest: Mapping | Path,
    *,
    root: Path,
    source_format: AgentFormat | str,
    client_version: str,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_total_source_bytes: int = MAX_TOTAL_SOURCE_BYTES,
) -> TreeInventory:
    """Validate declared relationships against exact, persistent transcript IDs.

    This small input format is ``kind=session-tree-inventory, schema_version=1``.
    It requires ``root_node_key``, ``nodes``, ``relationships`` and ``declaration``.
    Each node names ``key, source_native_id, relative_path, source_parent_status``
    and optionally ``title``. Declaration requires ``declaration_id, declared_at,
    operator, statement, confirmed=true``. Completeness defaults to ``partial``;
    ``declared_complete`` is an operator assertion, never native enumeration.
    """
    root, source_format = _root(root), _format(source_format)
    client_version = _text(client_version, maximum=64)
    if client_version not in _APPROVED[source_format]:
        _fail("Tree source client version has no approved metadata adapter")
    if isinstance(manifest, Path):
        snapshot = capture_source(
            SourceCaptureContext(AgentFormat(source_format), manifest),
            byte_limit=MAX_MANIFEST_BYTES,
        )
        document = _json(snapshot.raw_bytes, limit=MAX_MANIFEST_BYTES)
    elif isinstance(manifest, Mapping):
        try:
            document = _json(
                json.dumps(dict(manifest), allow_nan=False).encode("utf-8"),
                limit=MAX_MANIFEST_BYTES,
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail("Invalid explicit tree manifest")
    else:
        _fail("Explicit tree manifest must be a mapping or file path")
    _object(
        document,
        {"kind", "schema_version", "root_node_key", "nodes", "relationships", "declaration"},
        {"completeness", "canonical_root", "source_format"},
    )
    if (
        document["kind"] != "session-tree-inventory"
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
    ):
        _fail("Unsupported explicit tree manifest schema")
    if (
        document.get("canonical_root", str(root)) != str(root)
        or document.get("source_format", source_format) != source_format
    ):
        _fail("Explicit tree manifest belongs to another source root or format")
    declaration = _object(
        document["declaration"],
        {"declaration_id", "declared_at", "operator", "statement", "confirmed"},
    )
    if declaration["confirmed"] is not True:
        _fail("Manual tree declaration requires explicit confirmation")
    declaration_id = _key(declaration["declaration_id"])
    operator = _key(declaration["operator"])
    declared_at = _text(declaration["declared_at"], maximum=64)
    try:
        timestamp = datetime.fromisoformat(declared_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            _fail("Manual declaration timestamp requires timezone")
    except ValueError:
        _fail("Manual declaration timestamp is invalid")
    statement = _text(f"Operator {operator}: {_text(declaration['statement'])}")
    manual_id = "manual_declaration"
    evidence = [
        TreeEvidence(
            manual_id,
            "manual_declaration",
            declaration_id=declaration_id,
            declared_at=declared_at,
            statement=statement,
        )
    ]
    nodes, metadata_by_key = [], {}
    budget = _ReadBudget.for_root(
        root, max_source_bytes=max_source_bytes, max_total_source_bytes=max_total_source_bytes
    )
    for index, item in enumerate(_list(document["nodes"])):
        _object(
            item, {"key", "source_native_id", "relative_path", "source_parent_status"}, {"title"}
        )
        key = _key(item["key"])
        native_id = _identifier(item["source_native_id"])
        relative_path = _relative(item["relative_path"])
        metadata, digest = _capture_metadata(
            root, relative_path, source_format, client_version, budget
        )
        if metadata.native_id != native_id:
            _fail("Declared native ID differs from the transcript identity")
        if (
            metadata.subagent
            or "subagents" in Path(relative_path).parts
            or Path(relative_path).name.startswith("agent-")
        ):
            _fail("A subagent transcript cannot become a persistent tree session")
        if key in metadata_by_key:
            _fail("Duplicate tree node key")
        metadata_by_key[key] = metadata
        evidence_id = f"source_{index}"
        evidence.append(
            TreeEvidence(
                evidence_id,
                "native_metadata",
                relative_path=relative_path,
                component_sha256=digest,
                field_pointer="/sessionId"
                if source_format == "claude"
                else "/0/payload/id"
                if source_format == "codex"
                else "/info/id",
                client_version=client_version,
            )
        )
        nodes.append(
            TreeNode(
                key,
                native_id,
                relative_path,
                item.get("title", metadata.title or native_id),
                persistence_evidence_ids=(evidence_id,),
                source_parent_status=item["source_parent_status"],
            )
        )
    relationships = []
    for item in _list(document["relationships"], maximum=2 * MAX_NODES):
        _object(item, {"key", "parent_node_key", "child_node_key", "kind"})
        relationships.append(
            TreeRelationship(
                _key(item["key"]),
                _key(item["parent_node_key"]),
                _key(item["child_node_key"]),
                item["kind"],
                "manual",
                (manual_id,),
            )
        )
    by_native_id = {node.source_native_id: node.key for node in nodes}
    # Native facts cannot be erased or contradicted by an operator declaration.
    for node in nodes:
        metadata = metadata_by_key[node.key]
        if metadata.parent_id is None:
            continue
        parent_key = by_native_id.get(metadata.parent_id)
        if parent_key is None:
            _fail("Observed native parent is outside the explicit inventory root scope")
        matching = [
            edge
            for edge in relationships
            if edge.child_node_key == node.key and edge.kind == metadata.relationship_kind
        ]
        if not matching or any(edge.parent_node_key != parent_key for edge in matching):
            _fail("Manual declaration conflicts with an observed native parent")
        if node.source_parent_status != "known_parent":
            _fail("Manual parent status conflicts with observed native metadata")
    completeness = document.get("completeness", "partial")
    if completeness not in {"partial", "declared_complete"}:
        _fail("Explicit manifest cannot claim verified native completeness")
    return _validate(
        TreeInventory(
            source_format,
            str(root),
            _key(document["root_node_key"]),
            tuple(nodes),
            tuple(relationships),
            tuple(evidence),
            completeness=completeness,
            evidence_ids=(manual_id,),
        )
    )


def _enumerate(root: Path, source_format: str) -> tuple[str, ...]:
    paths = []
    entries = 0
    try:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            relative = Path(directory).relative_to(root)
            if len(relative.parts) > MAX_DIRECTORY_DEPTH:
                _fail("Tree source directory depth limit exceeded")
            entries += len(subdirs) + len(files)
            if entries > MAX_DIRECTORY_ENTRIES:
                _fail("Tree source directory entry limit exceeded")
            for name in subdirs + files:
                if (Path(directory) / name).is_symlink():
                    _fail("Tree source inventory refuses symbolic links")
            if "subagents" in subdirs:
                subdirs.remove("subagents")
            for name in sorted(files):
                supported = (
                    name.endswith(".json")
                    if source_format == "opencode"
                    else name.endswith(".jsonl")
                    or (source_format == "codex" and name.endswith(".jsonl.zst"))
                )
                if supported and not name.startswith("agent-"):
                    paths.append((relative / name).as_posix())
                    if len(paths) > MAX_NODES:
                        _fail("Tree source node limit exceeded")
    except OSError:
        _fail("Tree source enumeration failed")
    return tuple(sorted(paths))


def inventory_from_native_metadata(
    *,
    root: Path,
    source_format: AgentFormat | str,
    client_version: str,
    relative_paths: Sequence[str] | None = None,
    root_native_id: str | None = None,
    adapter: str | None = None,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_total_source_bytes: int = MAX_TOTAL_SOURCE_BYTES,
) -> TreeInventory:
    """Enumerate only a named root, or inspect an exact list of relative paths.

    Offline enumeration is partial relative to the client: it never claims API
    pagination completeness. Every observed logical parent must be present in
    the same root inventory. Claude CLI parents remain unknown. Desktop metadata
    is deliberately unsupported until a primary shape and transcript binding
    are verified; use the explicit manifest for operator-declared lineage.
    """
    if adapter is not None and (
        type(adapter) is not str
        or adapter not in {"claude_cli", "codex_rollout", "opencode_export"}
    ):
        _fail("Unverified native metadata adapter; use an explicit manual tree manifest")
    root, source_format = _root(root), _format(source_format)
    client_version = _text(client_version, maximum=64)
    if (
        adapter is not None
        and adapter
        != {"claude": "claude_cli", "codex": "codex_rollout", "opencode": "opencode_export"}[
            source_format
        ]
    ):
        _fail("Native metadata adapter does not match source format")
    if client_version not in _APPROVED[source_format]:
        _fail("Tree source client version has no approved metadata adapter")
    if relative_paths is None:
        paths = _enumerate(root, source_format)
    else:
        if (
            not isinstance(relative_paths, Sequence)
            or isinstance(relative_paths, (str, bytes))
            or len(relative_paths) > MAX_NODES
        ):
            _fail("Invalid explicit native metadata path list")
        paths = tuple(_relative(path) for path in relative_paths)
    if len(paths) != len(set(paths)):
        _fail("Duplicate native metadata source path")
    nodes, evidence, observations = [], [], {}
    budget = _ReadBudget.for_root(
        root, max_source_bytes=max_source_bytes, max_total_source_bytes=max_total_source_bytes
    )
    for path in paths:
        metadata, digest = _capture_metadata(root, path, source_format, client_version, budget)
        if (
            metadata.subagent
            or "subagents" in Path(path).parts
            or Path(path).name.startswith("agent-")
        ):
            if relative_paths is not None:
                _fail("A subagent transcript cannot become a persistent tree session")
            continue
        if metadata.native_id in observations:
            _fail("Duplicate native session ID inside the named root")
        index = len(nodes)
        key, evidence_id = f"node_{index}", f"source_{index}"
        observations[metadata.native_id] = (key, metadata, evidence_id)
        identity_pointer = {
            "claude": "/sessionId",
            "codex": "/0/payload/id",
            "opencode": "/info/id",
        }[source_format]
        evidence.append(
            TreeEvidence(
                evidence_id,
                "native_metadata",
                relative_path=path,
                component_sha256=digest,
                field_pointer=identity_pointer,
                client_version=client_version,
            )
        )
        if metadata.parent_id is not None:
            evidence.append(
                TreeEvidence(
                    f"relationship_{index}",
                    "native_metadata",
                    relative_path=path,
                    component_sha256=digest,
                    field_pointer=metadata.field_pointer,
                    client_version=client_version,
                )
            )
        nodes.append(
            TreeNode(
                key,
                metadata.native_id,
                path,
                metadata.title or metadata.native_id,
                persistence_evidence_ids=(evidence_id,),
                source_parent_status=metadata.parent_status,
            )
        )
    relationships = []
    for key, metadata, _evidence_id in observations.values():
        if metadata.parent_id is None:
            continue
        if metadata.parent_id not in observations:
            _fail("Observed native parent is absent from the named source root inventory")
        parent_key = observations[metadata.parent_id][0]
        relation_evidence_id = f"relationship_{key.removeprefix('node_')}"
        relationships.append(
            TreeRelationship(
                f"edge_{len(relationships)}",
                parent_key,
                key,
                metadata.relationship_kind,
                "native",
                (relation_evidence_id,),
            )
        )
    if not nodes:
        _fail("No persistent native sessions were found in the explicit source scope")
    if root_native_id is not None:
        root_native_id = _identifier(root_native_id)
        if root_native_id not in observations:
            _fail("Selected root ID is absent from the native source inventory")
        root_key = observations[root_native_id][0]
    else:
        roots = [node.key for node in nodes if node.source_parent_status == "known_root"]
        if len(roots) > 1:
            _fail("Native source scope contains several roots; select one explicitly")
        root_key = roots[0] if roots else nodes[0].key
    return _validate(
        TreeInventory(
            source_format,
            str(root),
            root_key,
            tuple(nodes),
            tuple(relationships),
            tuple(evidence),
            scope="native_query",
            completeness="partial",
            evidence_ids=(evidence[0].id,),
        )
    )
