"""Parent-first session-tree publication using the existing per-node importer.

Saved manifests and journals contain metadata, never executable publication
authority. Every publication requires freshly captured sources and sealed native
artifacts; an uncertain node stops the coordinator without rollback or reimport.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

from session_migrate import operations
from session_migrate.conversion import default_migration_state_home, load_opencode_session
from session_migrate.errors import (
    OperationCancelledError,
    OperationNeedsReconcileError,
    SessionMigrateError,
)
from session_migrate.formats import opencode
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import CancellationToken, OperationKind, OperationRequest
from session_migrate.profiles import Profile
from session_migrate.tree_journal import TreeJournal, inspect_tree, journal_path
from session_migrate.tree_model import (
    TreeInventory,
    TreePlan,
    TreeSelection,
    build_tree_plan,
    inventory_to_dict,
    plan_to_dict,
)
from session_migrate.tree_sources import (
    MAX_SOURCE_BYTES,
    MAX_TOTAL_SOURCE_BYTES,
    _capture_metadata,
    _ReadBudget,
    _root,
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class TreeOperationError(SessionMigrateError):
    """A tree cannot safely be prepared, published or resumed."""


class _CompletedTreeChanged(TreeOperationError):
    def __init__(self, node_keys):
        self.node_keys = tuple(node_keys)
        super().__init__("A completed tree node or its durable source binding changed")


@dataclass(frozen=True, slots=True, repr=False)
class TreeResult:
    _document: str

    @property
    def status(self) -> str:
        return json.loads(self._document)["status"]

    @property
    def operation_id(self) -> str:
        return json.loads(self._document)["operation_id"]

    def to_dict(self) -> dict:
        return json.loads(self._document)


@dataclass(frozen=True, slots=True, repr=False)
class _TreeState:
    plan: TreePlan
    publication_mode: str
    target: Profile
    source_profile: Profile | None
    cwd: Path
    model_provider: str
    model: str
    state_home: Path
    base_environ: Mapping[str, str]
    target_environment: Mapping[str, str]
    plan_sha256: str
    source_sha256: Mapping[str, str]
    handles: tuple
    acknowledged: bool
    requires_acknowledgement: bool
    guard: Callable[[], None]
    resume: bool
    completed: list


_SEAL = object()


class PreparedTree:
    """One-shot live capability. Serialized proposals cannot reconstruct it."""

    __slots__ = ("_state", "_proposal", "_consumed", "_lock")

    def __init__(self, state, proposal, *, _seal=None):
        if _seal is not _SEAL:
            raise TreeOperationError("A live prepared tree capability is required")
        self._state, self._proposal = state, _json(proposal)
        self._consumed, self._lock = False, threading.Lock()

    def discard(self) -> None:
        with self._lock:
            if self._state is not None:
                for _, handle in self._state.handles:
                    handle.discard()
            self._state, self._consumed = None, True

    def _consume(self):
        with self._lock:
            if self._consumed or self._state is None:
                raise TreeOperationError("Prepared tree already consumed; prepare again")
            self._consumed = True
            state, self._state = self._state, None
            return state


def tree_proposal(prepared: PreparedTree) -> dict:
    if type(prepared) is not PreparedTree:
        raise TreeOperationError("A live prepared tree capability is required")
    return json.loads(prepared._proposal)


def _capture_plan(
    plan: TreePlan, max_source_bytes=MAX_SOURCE_BYTES, max_total_source_bytes=MAX_TOTAL_SOURCE_BYTES
) -> dict[str, str]:
    """Recheck physical containment, exact native identities and declared captures."""
    root = _root(Path(plan.inventory.canonical_root))
    budget = _ReadBudget.for_root(root)
    budget.remaining = max_total_source_bytes
    evidence = {item.id: item for item in plan.inventory.evidence}
    result = {}
    for node in plan.ordered_nodes:
        if node.relative_path is None:
            raise TreeOperationError("Tree publication requires an explicit local source component")
        references = [
            evidence[key]
            for key in node.persistence_evidence_ids
            if evidence[key].kind == "native_metadata"
            and evidence[key].relative_path == node.relative_path
        ]
        if not references or len({item.client_version for item in references}) != 1:
            raise TreeOperationError("Tree node lacks root-bound native capture evidence")
        node_budget = _ReadBudget(
            min(max_source_bytes, budget.remaining), budget.root_device, budget.root_inode
        )
        allowance = node_budget.remaining
        metadata, digest = _capture_metadata(
            root,
            node.relative_path,
            plan.inventory.source_format,
            references[0].client_version,
            node_budget,
        )
        budget.remaining -= allowance - node_budget.remaining
        if (
            metadata.native_id != node.source_native_id
            or metadata.subagent
            or any(item.component_sha256 != digest for item in references)
        ):
            raise TreeOperationError("Tree source identity or capture changed; rebuild inventory")
        result[node.key] = digest
    return result


def _target_environment(target: Profile, base: Mapping[str, str]) -> dict[str, str]:
    if type(target) is not Profile or target.format is not AgentFormat.OPENCODE:
        raise TreeOperationError("Tree publication requires an explicit OpenCode target profile")
    if target.root.name != "opencode" or target.root.is_symlink():
        raise TreeOperationError("Tree destination must be its native OpenCode store root")
    if target.client == "openchamber" and target.server is None:
        raise TreeOperationError("OpenChamber tree publication requires its connected server")
    if target.root.exists() and not target.root.is_dir():
        raise TreeOperationError("Tree target root must be a directory")
    if (
        "XDG_DATA_HOME" in target.environ
        and Path(target.environ["XDG_DATA_HOME"]) / "opencode" != target.root
    ):
        raise TreeOperationError("Tree target profile does not match its native data namespace")
    values = target.environment(base)
    values["XDG_DATA_HOME"] = str(target.root.parent)
    if target.server:
        values["SESSION_MIGRATE_OPENCODE_SERVER"] = target.server
    else:
        values.pop("SESSION_MIGRATE_OPENCODE_SERVER", None)
        values.pop("SESSION_MIGRATE_OPENCODE_BINDING", None)
    return values


def _hierarchy_semantics(publication_mode):
    return (
        "external_organizational_map"
        if publication_mode == "independent"
        else "native_subagent_parent"
    )


def _publication_warnings(publication_mode):
    if publication_mode == "independent":
        return []
    return [
        {
            "code": "native_subagent_composer_disabled",
            "message": (
                "OpenCode 2.2.5 treats parentID as a native subagent relationship and disables "
                "the child composer. This does not preserve an editable persistent fork."
            ),
        }
    ]


def _plan_fingerprint(
    plan,
    target,
    source_profile,
    cwd,
    provider,
    model,
    max_source_bytes,
    max_total_bytes,
    publication_mode="native-subagent",
) -> str:
    return _digest(
        {
            "inventory": inventory_to_dict(plan.inventory),
            "plan": plan_to_dict(plan),
            "target_profile": target.fingerprint,
            "source_profile": source_profile.fingerprint if source_profile else None,
            "cwd": str(cwd),
            "model_provider": provider,
            "model": model,
            "max_source_bytes": max_source_bytes,
            "max_total_bytes": max_total_bytes,
            # Preserve the historical native-parent fingerprint byte for byte.
            **({"publication_mode": publication_mode} if publication_mode == "independent" else {}),
        }
    )


def _observe(target: Profile, environment: Mapping[str, str], node: dict, *, cwd=None) -> str:
    captures = []
    observed = load_opencode_session(
        node["target_id"], source_cli=target.cli, environ=environment, on_capture=captures.append
    )
    if len(captures) != 1:
        raise TreeOperationError("Tree target has no authoritative native export")
    parsed = opencode.parse_import_bytes(captures[0].raw_bytes)
    if (
        observed.session_id != node["target_id"]
        or observed.cli_version != "2.2.5"
        or parsed.parent_session != node["parent_target_id"]
        or (cwd is not None and parsed.cwd != cwd)
    ):
        raise TreeOperationError("Tree target identity or parent changed")
    return observed.source_sha256


def _node_request(plan, node, target, cwd, model_provider, model, publication_mode):
    parent_key = plan.parent_map[node.key]
    return OperationRequest(
        command=OperationKind.IMPORT,
        path=Path(plan.inventory.canonical_root) / node.relative_path,
        source_format=AgentFormat(plan.inventory.source_format),
        source_home=Path(plan.inventory.canonical_root),
        target_format=TargetFormat.OPENCODE,
        session_id=str(uuid.UUID(hex=plan.id_map[node.key][4:])),
        cwd=cwd,
        target_cli=target.cli,
        target_cli_version="2.2.5",
        model_provider=model_provider,
        model=model,
        target_title=node.title,
        target_parent_id=(
            plan.id_map[parent_key]
            if parent_key and publication_mode == "native-subagent"
            else None
        ),
        reviewed_tree_projection=True,
        target_client=target.client,
        target_server=target.server,
    )


def _live_source_binding(request, target_values, source_values, profile_context, byte_limit):
    """Rebuild the existing operation's source/options binding without importing.

    A durable IMPORT intentionally has no qualified source identity. Its guarded
    source witness binds the format, root epoch, exact path, native ID, physical
    layout and captured authority instead. Codex authority can be composite and
    must stay separate from the leaf's physical SHA256.
    """
    from session_migrate.conversion import _resolve_opencode_cli
    from session_migrate.native_snapshots import check_source_identity

    request, target_values, source_values = operations._operation_context(
        request, target_values, source_values
    )
    for environment in (target_values, source_values):
        for key in operations._PUBLIC_ENVIRONMENT:
            if key in environment and key not in {"OPENCODE_BIN", "KILO_BIN"}:
                environment[key] = str(Path(environment[key]).resolve())
    request = replace(
        request,
        path=request.path.resolve(strict=True),
        source_home=request.source_home.resolve(strict=True),
        cwd=request.cwd.resolve(),
        target_cli=_resolve_opencode_cli(request.target_cli, target_values),
        dry_run=True,
    )
    captures = []
    session = operations._load_path_source(
        request.path,
        request.source_format,
        request.source_home,
        source_values,
        byte_limit,
        on_capture=captures.append,
    )
    if len(captures) != 1 or not captures[0].integrity_valid():
        raise TreeOperationError("Completed tree source lacks a guarded native capture")
    snapshot = captures[0]
    witness = operations._source_witness(session, request.source_home)
    check_source_identity(snapshot)
    if request.source_format is AgentFormat.OPENCODE:
        source_values = operations._catalog_virtual_source_environment(
            request.source_format, request.source_home, source_values
        )
    return {
        "source_format": session.source_format.value,
        "source_native_id": session.session_id,
        "physical_sha256": snapshot.physical_source_sha256,
        "authority_sha256": snapshot.authority_sha256,
        "witness_sha256": witness,
        "options_fingerprint": operations._digest(
            operations._public_options(request, target_values, source_values, profile_context)
        ),
    }


def _validate_journal_nodes(plan, nodes, source_hashes, publication_mode):
    if {node["key"] for node in nodes} != set(plan.selected_node_keys):
        raise TreeOperationError("Tree journal node selection differs from the live plan")
    titles = {node.key: node.title for node in plan.ordered_nodes}
    for node in nodes:
        key = node["key"]
        parent_key = plan.parent_map[key]
        organizational_parent = plan.id_map[parent_key] if parent_key else None
        if (
            node["target_id"] != plan.id_map[key]
            or node["parent_target_id"]
            != (organizational_parent if publication_mode == "native-subagent" else None)
            or node["organizational_parent_target_id"] != organizational_parent
            or ("title" in node and node["title"] != titles[key])
            or node["source_sha256"] != source_hashes[key]
        ):
            raise TreeOperationError("Tree journal identity differs from the live source plan")


def _verify_completed_ledgers(state_home, target, nodes, source_binding):
    """A journal declaration alone cannot certify that a node completed."""
    if not nodes:
        return
    from session_migrate.lineage import CompletionReceipt, LineageStore, OperationState

    view = LineageStore.inspect_existing(state_home / "lineage-v1")
    if view is None:
        raise _CompletedTreeChanged(node["key"] for node in nodes)
    operations_by_id = {item.record.operation_id: item for item in view.operations}
    root = target.root.stat()
    for node in nodes:
        try:
            expected = source_binding(node["key"])
        except SessionMigrateError:
            raise _CompletedTreeChanged((node["key"],)) from None
        item = operations_by_id.get(node["operation_id"])
        if (
            item is None
            or item.state is not OperationState.COMPLETED
            or not isinstance(item.receipt, CompletionReceipt)
            or item.record.target_namespace.native_id != node["target_id"]
            or item.record.target_namespace.canonical_root != str(target.root)
            or item.record.target_profile_fingerprint != target.fingerprint
            or item.record.source.identity is not None
            or item.record.source.artifact_sha256 != expected["authority_sha256"]
            or item.record.source.witness_sha256 != expected["witness_sha256"]
            or item.record.options_sha256 != expected["options_fingerprint"]
            or item.record.target_input_sha256 != node["target_sha256"]
            or item.receipt.input_artifact_sha256 != node["target_sha256"]
            or (
                item.source_checkpoint is not None
                and (
                    item.source_checkpoint.artifact_sha256 != expected["authority_sha256"]
                    or item.source_checkpoint.identity is not None
                )
            )
            or item.target_checkpoint is None
            or item.target_checkpoint.artifact_sha256 != node["observed_sha256"]
            or (
                item.receipt.target_identity.root_witness.device,
                item.receipt.target_identity.root_witness.inode,
            )
            != (root.st_dev, root.st_ino)
        ):
            raise _CompletedTreeChanged((node["key"],))


def prepare_tree(
    inventory: TreeInventory,
    selection: TreeSelection,
    *,
    target: Profile,
    cwd: Path,
    model_provider: str,
    model: str,
    operation_id: str | None = None,
    state_home: Path | None = None,
    base_environ: Mapping[str, str] | None = None,
    source_profile: Profile | None = None,
    acknowledge_partial: bool = False,
    publication_mode: str | None = None,
    prepublication_check: Callable[[], None] | None = None,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_total_source_bytes: int = MAX_TOTAL_SOURCE_BYTES,
    max_total_bytes: int | None = None,
    _resume: bool = False,
) -> PreparedTree:
    """Compile all selected nodes before publication; probe only explicit profiles."""
    from types import MappingProxyType

    operation_id = operation_id or str(uuid.uuid4())
    plan = build_tree_plan(inventory, selection, operation_id)
    if max_total_bytes is not None:
        if max_total_source_bytes != MAX_TOTAL_SOURCE_BYTES:
            raise TreeOperationError("Pass only one total tree capture budget")
        max_total_source_bytes = max_total_bytes
    if any(
        type(value) is not int or not 1 <= value <= maximum
        for value, maximum in (
            (max_source_bytes, MAX_SOURCE_BYTES),
            (max_total_source_bytes, MAX_TOTAL_SOURCE_BYTES),
        )
    ):
        raise TreeOperationError("Tree capture budgets must be positive bounded byte counts")
    if (
        not isinstance(cwd, Path)
        or not cwd.is_absolute()
        or ".." in cwd.parts
        or not cwd.is_dir()
        or any(path.is_symlink() for path in (cwd, *cwd.parents))
    ):
        raise TreeOperationError(
            "Tree publication requires an existing canonical project directory"
        )
    if any(
        type(value) is not str
        or not value.strip()
        or len(value) > 256
        or any(ord(char) < 32 for char in value)
        for value in (model_provider, model)
    ):
        raise TreeOperationError("Tree publication requires an explicit model and provider")
    if type(acknowledge_partial) is not bool:
        raise TreeOperationError("Partial projection acknowledgement must be explicit")
    if source_profile is not None and (
        type(source_profile) is not Profile
        or source_profile.format.value != inventory.source_format
        or str(source_profile.root) != inventory.canonical_root
        or source_profile.client is not None
    ):
        raise TreeOperationError("Tree inventory belongs to another source profile namespace")
    base = dict(os.environ if base_environ is None else base_environ)
    values = _target_environment(target, base)
    source_values = source_profile.environment(base) if source_profile else dict(base)
    state_home = state_home or default_migration_state_home(environ=base)
    path = journal_path(state_home, operation_id)
    if not _resume and os.path.lexists(path):
        raise TreeOperationError(
            "Tree operation already has a journal; inspect and explicitly resume"
        )
    if publication_mode is not None and (
        type(publication_mode) is not str
        or publication_mode not in {"independent", "native-subagent"}
    ):
        raise TreeOperationError("Unknown tree publication mode")
    prior = inspect_tree(state_home, operation_id) if _resume else None
    persisted_mode = prior["publication_mode"] if prior is not None else None
    if prior is not None and publication_mode is not None and publication_mode != persisted_mode:
        raise TreeOperationError("Tree resume publication mode differs from the original operation")
    publication_mode = persisted_mode or publication_mode or "independent"
    source_hashes = _capture_plan(plan, max_source_bytes, max_total_source_bytes)
    fingerprint = _plan_fingerprint(
        plan,
        target,
        source_profile,
        cwd,
        model_provider,
        model,
        max_source_bytes,
        max_total_source_bytes,
        publication_mode,
    )
    original_target, original_source = (
        target.fingerprint,
        source_profile.fingerprint if source_profile else None,
    )
    completed = []
    planned_nodes = {node.key: node for node in plan.ordered_nodes}
    profile_context = {
        "target_profile_id": target.id,
        "target_profile_name": target.name,
        "target_profile_fingerprint": target.fingerprint,
    }

    def source_binding(key):
        node = planned_nodes[key]
        binding = _live_source_binding(
            _node_request(plan, node, target, cwd, model_provider, model, publication_mode),
            values,
            source_values,
            profile_context,
            max_source_bytes,
        )
        if (
            binding["source_format"] != inventory.source_format
            or binding["source_native_id"] != node.source_native_id
            or binding["physical_sha256"] != source_hashes[key]
        ):
            raise TreeOperationError("Completed tree source differs from its live inventory")
        return binding

    def guard():
        if prepublication_check is not None:
            prepublication_check()
        if (
            target.fingerprint != original_target
            or (source_profile.fingerprint if source_profile else None) != original_source
            or _plan_fingerprint(
                build_tree_plan(inventory, selection, operation_id),
                target,
                source_profile,
                cwd,
                model_provider,
                model,
                max_source_bytes,
                max_total_source_bytes,
                publication_mode,
            )
            != fingerprint
            or _target_environment(target, base) != values
            or _capture_plan(plan, max_source_bytes, max_total_source_bytes) != source_hashes
        ):
            raise TreeOperationError("Tree sources, profile or options changed; prepare again")
        _verify_completed_ledgers(state_home, target, completed, source_binding)
        for node in completed:
            node["title"] = planned_nodes[node["key"]].title
        for completed_node in completed:
            try:
                observed = _observe(target, values, completed_node, cwd=cwd)
            except SessionMigrateError:
                raise _CompletedTreeChanged((completed_node["key"],)) from None
            if observed != completed_node["observed_sha256"]:
                raise _CompletedTreeChanged((completed_node["key"],))

    guard()
    if _resume:
        if prior["plan_sha256"] != fingerprint or prior["target_root"] != str(target.root):
            raise TreeOperationError("Tree resume options differ from the original operation")
        _validate_journal_nodes(plan, prior["nodes"], source_hashes, publication_mode)
        if any(node["status"] not in {"unattempted", "completed"} for node in prior["nodes"]):
            raise TreeOperationError(
                "Tree has an attempted node; explicit reconciliation is required"
            )
        for node in prior["nodes"]:
            if node["status"] == "completed":
                if _observe(target, values, node, cwd=cwd) != node.get("observed_sha256"):
                    raise TreeOperationError("A completed tree target changed; resume is blocked")
                # The journal has no certified loss/coverage details. Absence
                # cannot upgrade an earlier UNKNOWN audit to PARTIAL or FULL.
                node.update(
                    coverage="unknown",
                    losses=None,
                    loss_details_available=False,
                    warnings=[
                        {
                            "code": "resumed_loss_details_unavailable",
                            "message": (
                                "Historical loss details are not certified by the tree journal."
                            ),
                        }
                    ],
                )
                completed.append(node)
        _verify_completed_ledgers(state_home, target, completed, source_binding)
    completed_keys = {node["key"] for node in completed}
    handles, nodes = [], []
    captured_bytes = 0
    try:
        for node in plan.ordered_nodes:
            parent_key = plan.parent_map[node.key]
            organizational_parent_id = plan.id_map[parent_key] if parent_key else None
            parent_id = organizational_parent_id if publication_mode == "native-subagent" else None
            if node.key in completed_keys:
                nodes.append(next(item for item in completed if item["key"] == node.key))
                continue
            available_bytes = max_total_source_bytes - captured_bytes
            if available_bytes <= 0:
                raise TreeOperationError(
                    "Tree retained source and target artifacts exceed the byte limit"
                )
            request = _node_request(
                plan, node, target, cwd, model_provider, model, publication_mode
            )
            handle = operations.prepare_operation(
                request,
                environ=values,
                source_environ=source_values,
                prepublication_check=guard,
                max_source_bytes=min(max_source_bytes, available_bytes),
                migration_state_home=state_home,
                profile_context=profile_context,
            )
            handles.append((node.key, handle))
            proposal = operations.proposal_metadata(handle).to_dict()
            snapshot = handle._state.built.source_snapshot
            physical_sha = (
                snapshot.storage_components[0].authority_sha256
                if snapshot is not None and snapshot.storage_components
                else proposal["source"]["sha256"]
            )
            if snapshot is not None:
                captured_bytes += snapshot.captured_byte_count + len(
                    handle._state.built.artifact.native_bytes
                )
                if (
                    snapshot.captured_byte_count > max_source_bytes
                    or captured_bytes > max_total_source_bytes
                ):
                    raise TreeOperationError("Tree source aggregate capture exceeds its byte limit")
            if (
                proposal["source"]["native_id"] != node.source_native_id
                or physical_sha != source_hashes[node.key]
                or proposal["target"]["native_id"] != plan.id_map[node.key]
                or proposal["target"]["cli_version"] != "2.2.5"
            ):
                raise TreeOperationError(
                    "Compiled tree node differs from its source or target identity"
                )
            nodes.append(
                {
                    "key": node.key,
                    "source_native_id": node.source_native_id,
                    "title": opencode.parse_import_bytes(
                        handle._state.built.artifact.native_bytes
                    ).title,
                    "target_id": plan.id_map[node.key],
                    "parent_target_id": parent_id,
                    "organizational_parent_target_id": organizational_parent_id,
                    "operation_id": proposal["proposal_id"],
                    "status": "unattempted",
                    "source_sha256": source_hashes[node.key],
                    "source_authority_sha256": proposal["source"]["sha256"],
                    "target_sha256": proposal["target"]["sha256"],
                    "losses": proposal["losses"],
                    "warnings": proposal["warnings"],
                    "coverage": "partial"
                    if handle._state.projection is not None
                    and handle._state.projection.expectation_coverage.value != "unknown"
                    else "unknown",
                }
            )
        guard()
        needs_ack = inventory.completeness == "partial" or any(
            item.get("losses") or item.get("coverage", "partial") != "verified" for item in nodes
        )
        proposal = {
            "schema_version": 1,
            "kind": "session-tree-proposal",
            "operation_id": operation_id,
            "status": "ready",
            "projection_scope": "portable_conversation",
            "coverage": "partial"
            if all(node.get("coverage", "partial") != "unknown" for node in nodes)
            else "unknown",
            "publication_mode": publication_mode,
            "hierarchy_semantics": _hierarchy_semantics(publication_mode),
            "warnings": _publication_warnings(publication_mode),
            "fork_boundary_proven": False,
            "source": {
                "format": inventory.source_format,
                "root": inventory.canonical_root,
                "completeness": inventory.completeness,
                "max_source_bytes": max_source_bytes,
                "max_total_bytes": max_total_source_bytes,
            },
            "target": {
                "format": "opencode",
                "root": str(target.root),
                "cli_version": "2.2.5",
                "cwd": str(cwd),
                "profile_fingerprint": target.fingerprint,
            },
            "selection": {
                "mode": selection.mode,
                "anchor_node_key": selection.anchor_node_key,
                "boundary_policy": selection.boundary_policy,
            },
            "nodes": nodes,
            "detached_node_keys": list(plan.detached_node_keys),
            "requires_partial_acknowledgement": needs_ack,
            "partial_acknowledged": acknowledge_partial,
        }
        state = _TreeState(
            plan,
            publication_mode,
            target,
            source_profile,
            cwd,
            model_provider,
            model,
            state_home,
            MappingProxyType(base),
            MappingProxyType(values),
            fingerprint,
            MappingProxyType(source_hashes),
            tuple(handles),
            acknowledge_partial,
            needs_ack,
            guard,
            _resume,
            completed,
        )
        return PreparedTree(state, proposal, _seal=_SEAL)
    except BaseException:
        for _, handle in handles:
            handle.discard()
        raise


class _NodeCancellation(CancellationToken):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent

    def check(self):
        if not self.publishing:
            self.parent.check()
        super().check()

    def begin_publication(self):
        self.parent.check()
        super().begin_publication()


def _result(state, nodes, status) -> TreeResult:
    return TreeResult(
        _json(
            {
                "schema_version": 1,
                "operation_id": state.plan.operation_id,
                "status": status,
                "coverage": "unknown"
                if any(node.get("coverage", "unknown") == "unknown" for node in nodes)
                else "partial",
                "projection_scope": "portable_conversation",
                "publication_mode": state.publication_mode,
                "hierarchy_semantics": _hierarchy_semantics(state.publication_mode),
                "fork_boundary_proven": False,
                "warnings": _publication_warnings(state.publication_mode),
                "nodes": nodes,
                "journal_path": str(journal_path(state.state_home, state.plan.operation_id)),
            }
        )
    )


def _completed_changed_result(state, nodes, journal, error):
    for node in nodes:
        if node["key"] not in error.node_keys or node["status"] != "completed":
            continue
        node["status"] = "needs_reconcile"
        if journal is not None:
            with suppress(SessionMigrateError, OSError):
                journal.append(
                    {
                        "type": "needs_reconcile",
                        "node_key": node["key"],
                        "node_operation_id": node["operation_id"],
                    }
                )
    return _result(state, nodes, "needs_reconcile")


def commit_tree(prepared: PreparedTree, *, cancellation=None, progress=None) -> TreeResult:
    """Consume all artifacts once; stop on uncertainty and leave published nodes intact."""
    if type(prepared) is not PreparedTree:
        raise TreeOperationError("A live prepared tree capability is required")
    proposal = tree_proposal(prepared)
    state = prepared._consume()
    token = cancellation or CancellationToken()
    token.claim()
    nodes = [
        {
            key: node.get(key)
            for key in (
                "key",
                "title",
                "coverage",
                "target_id",
                "parent_target_id",
                "organizational_parent_target_id",
                "operation_id",
                "status",
            )
        }
        for node in proposal["nodes"]
    ]
    journal = None
    try:
        token.check()
        if state.requires_acknowledgement and not state.acknowledged:
            raise TreeOperationError(
                "Review portable omissions and explicitly acknowledge the partial tree projection"
            )
        state.guard()
        # Every sealed artifact is rechecked before the first coordinator write.
        for _, handle in state.handles:
            operations._prepublication_guards(handle._state)
        journal = TreeJournal(state.state_home, state.plan.operation_id, create=not state.resume)
        if state.resume:
            current = journal.inspect()
            _validate_journal_nodes(
                state.plan, current["nodes"], state.source_sha256, state.publication_mode
            )
            completed_fields = (
                "operation_id",
                "target_id",
                "parent_target_id",
                "organizational_parent_target_id",
                "source_sha256",
                "target_sha256",
                "observed_sha256",
            )
            expected_completed = {
                item["key"]: tuple(item.get(field) for field in completed_fields)
                for item in state.completed
            }
            current_completed = {
                item["key"]: tuple(item.get(field) for field in completed_fields)
                for item in current["nodes"]
                if item["status"] == "completed"
            }
            if (
                current["publication_mode"] != state.publication_mode
                or current["plan_sha256"] != state.plan_sha256
                or current_completed != expected_completed
                or any(
                    node["status"] not in {"completed", "unattempted"} for node in current["nodes"]
                )
            ):
                raise TreeOperationError("Tree journal changed since resume preparation")
        else:
            journal.append(
                {
                    "type": "prepared",
                    "operation_id": state.plan.operation_id,
                    "plan_sha256": state.plan_sha256,
                    "target_root": str(state.target.root),
                    "publication_mode": state.publication_mode,
                    "nodes": [
                        {
                            key: node[key]
                            for key in (
                                "key",
                                "title",
                                "target_id",
                                "parent_target_id",
                                "organizational_parent_target_id",
                                "source_sha256",
                                "target_sha256",
                                "operation_id",
                            )
                        }
                        for node in proposal["nodes"]
                    ],
                }
            )
        for key, handle in state.handles:
            node = next(item for item in nodes if item["key"] == key)
            token.check()
            state.guard()
            operations._prepublication_guards(handle._state)
            current_artifact_sha = operations.proposal_metadata(handle).to_dict()["target"][
                "sha256"
            ]
            journal.append(
                {
                    "type": "attempting",
                    "node_key": key,
                    "node_operation_id": node["operation_id"],
                    "target_sha256": current_artifact_sha,
                }
            )
            node["status"] = "attempting"
            try:
                receipt = operations.commit_operation(
                    handle, cancellation=_NodeCancellation(token), progress=progress
                )
                # Inner completion is an existing durable ledger record. Export again
                # to bind explicit resume to exact bytes and the organizational parent.
                observed_sha = _observe(state.target, state.target_environment, node, cwd=state.cwd)
                if receipt.lineage_receipt is None:
                    raise TreeOperationError("Tree node lacks its per-operation completion ledger")
                completed_node = next(item for item in proposal["nodes"] if item["key"] == key)
                state.completed.append(
                    {**completed_node, "observed_sha256": observed_sha, "status": "completed"}
                )
                node.update(status="completed", result=receipt.to_dict())
                journal.append(
                    {
                        "type": "completed",
                        "node_key": key,
                        "node_operation_id": node["operation_id"],
                        "observed_sha256": observed_sha,
                    }
                )
                state.guard()
            except _CompletedTreeChanged:
                raise
            except OperationCancelledError:
                journal.append(
                    {"type": "failed", "node_key": key, "node_operation_id": node["operation_id"]}
                )
                node["status"] = "failed"
                raise
            except BaseException as error:
                # Once handed to the native operation, unexpected failures are
                # conservatively uncertain. The ledger retains the precise state.
                node["status"] = "needs_reconcile"
                with suppress(SessionMigrateError, OSError):
                    journal.append(
                        {
                            "type": "needs_reconcile",
                            "node_key": key,
                            "node_operation_id": node["operation_id"],
                        }
                    )
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(error, OperationNeedsReconcileError):
                    node["reconciliation"] = {
                        "operation_id": error.operation_id,
                        "native_state": error.native_state.value,
                    }
                try:
                    state.guard()
                except _CompletedTreeChanged as changed:
                    return _completed_changed_result(state, nodes, journal, changed)
                except (SessionMigrateError, OSError):
                    pass
                return _result(state, nodes, "needs_reconcile")
        state.guard()
        journal.append({"type": "finished"})
        state.guard()
        return _result(state, nodes, "completed")
    except _CompletedTreeChanged as error:
        return _completed_changed_result(state, nodes, journal, error)
    except OperationCancelledError:
        if journal is not None:
            journal.append({"type": "cancelled"})
        status = "partial" if any(node["status"] == "completed" for node in nodes) else "cancelled"
        return _result(state, nodes, status)
    except Exception:
        if any(node["status"] in {"attempting", "needs_reconcile"} for node in nodes):
            return _result(state, nodes, "needs_reconcile")
        if any(node["status"] == "completed" for node in nodes):
            return _result(state, nodes, "partial")
        raise
    finally:
        for _, handle in state.handles:
            handle.discard()
        if journal is not None:
            journal.close()
        token.close()


def prepare_tree_resume(inventory, selection, *, operation_id: str, **kwargs) -> PreparedTree:
    """Explicit fresh preflight of only nodes the journal proves unattempted."""
    return prepare_tree(inventory, selection, operation_id=operation_id, _resume=True, **kwargs)


def reconcile_tree(
    state_home: Path,
    operation_id: str,
    *,
    target: Profile | None = None,
    base_environ: Mapping[str, str] | None = None,
) -> dict:
    """Read/export uncertain IDs for human assessment; never retry or rewrite a node."""
    view = inspect_tree(state_home, operation_id)
    view["reconciliation_required"] = view["status"] == "needs_reconcile"
    if target is None:
        return view
    values = _target_environment(target, dict(os.environ if base_environ is None else base_environ))
    if view["target_root"] != str(target.root):
        raise TreeOperationError("Tree reconciliation belongs to another target namespace")
    from session_migrate.conversion import _opencode_session_exists, _resolve_opencode_cli

    cli = _resolve_opencode_cli(target.cli, values)
    for node in view["nodes"]:
        if node["status"] not in {"attempting", "needs_reconcile"}:
            continue
        if _opencode_session_exists(cli, node["target_id"], values):
            node["observation"] = {"status": "present", "sha256": _observe(target, values, node)}
        else:
            node["observation"] = {"status": "absent"}
    return view
