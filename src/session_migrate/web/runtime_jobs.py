"""Private native handoff capabilities owned by the single jobs worker."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakKeyDictionary

from session_migrate.catalog import Catalog
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.handoff import (
    HandoffDraft,
    accept_user_text,
    handoff_metadata,
    prepare_handoff,
    render_handoff,
)
from session_migrate.handoff_operations import ReturnNewRequest, prepare_return_new, prepare_summary
from session_migrate.handoff_types import HandoffLimits
from session_migrate.lineage import SelectionRangeV1
from session_migrate.operations import (
    OperationKind,
    OperationRequest,
    PreparedOperation,
    TransferReceipt,
    commit_operation,
    proposal_metadata,
)
from session_migrate.profiles import Profile, SessionRef, resolve_source

NATIVE_ACTIONS = frozenset({"summary", "return_new_summary", "return_new_full", "tree"})
RUNTIME_BUFFER_BYTES = 64 * 1024 * 1024
SOURCE_BYTES = 4 * 1024 * 1024
DOCUMENT_BYTES = 60000
_ISSUED = WeakKeyDictionary()


class RuntimeJobError(Exception):
    def __init__(self, reason="private_handle_required"):
        self.reason = reason
        super().__init__(reason)


def _require(condition, reason="private_handle_required"):
    if not condition:
        raise RuntimeJobError(reason)


def workflow_json(value):
    """Bound the actual UTF-8 envelope, including escaped JSON overhead."""
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        _require(len(encoded.encode("utf-8")) <= 64 * 1024, "request_invalid")
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RuntimeJobError("request_invalid") from None


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeRequest:
    """Trusted launch resolution; constructed by the backend inside the worker."""

    catalog_path: Path
    source_profile: Profile
    source_ref: SessionRef
    target_profile: Profile
    options: OperationRequest
    state_home: Path
    base_environ: dict
    observer_environ: dict
    source_client_version: str | None = None
    selection: tuple[SelectionRangeV1, ...] = ()
    parent_operation_id: str | None = None
    binding_check: object = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class TreeRuntimeRequest:
    """Launch-resolved tree declaration; HTTP never supplies paths or profiles."""

    source_profile: Profile
    target_profile: Profile
    cwd: Path
    state_home: Path
    base_environ: dict
    source_client_version: str
    manifest: dict
    mode: str
    anchor: str | None
    detach: bool
    acknowledge_partial: bool
    model_provider: str
    model: str
    hierarchy_keys: tuple[str, ...] | None = None
    binding_check: object = field(default=None, repr=False)
    project_name: str = ""


def _tree_binding(request):
    _require(type(request) is TreeRuntimeRequest, "request_invalid")
    _require(type(request.source_profile) is Profile and type(request.target_profile) is Profile)
    for path in (request.cwd, request.state_home):
        _require(isinstance(path, Path) and path.is_absolute() and ".." not in path.parts)
    _require(request.cwd.is_dir(), "request_invalid")
    _require(type(request.manifest) is dict, "request_invalid")
    _require(request.mode in {"single", "subtree", "full_tree"}, "request_invalid")
    _require(type(request.detach) is bool and type(request.acknowledge_partial) is bool)
    value = {
        "source": request.source_profile.fingerprint,
        "target": request.target_profile.fingerprint,
        "cwd": str(request.cwd),
        "state_home": str(request.state_home),
        "version": request.source_client_version,
        "manifest": request.manifest,
        "mode": request.mode,
        "anchor": request.anchor,
        "detach": request.detach,
        "partial": request.acknowledge_partial,
        "provider": request.model_provider,
        "model": request.model,
        "edges": request.hierarchy_keys,
        "project_name": request.project_name,
        "environment_sha256": hashlib.sha256(
            repr(sorted(request.base_environ.items())).encode()
        ).hexdigest(),
        "binding_check_identity": id(request.binding_check),
    }
    return hashlib.sha256(workflow_json(value).encode("utf-8")).hexdigest()


def _tree_metadata(document, request):
    """Project structure/losses/IDs; exclude paths, transcript bodies and native args."""
    source, target = document["source"], document["target"]
    return {
        "schema_version": 1,
        "native_action": "tree",
        "coverage": document.get("coverage", "unknown"),
        "projection_scope": document.get("projection_scope", "portable_conversation"),
        "hierarchy_semantics": document.get("hierarchy_semantics", "organizational_parent"),
        "fork_boundary_proven": False,
        "operation_id": document["operation_id"],
        "status": document["status"],
        "source": {
            **{key: source[key] for key in ("format", "completeness") if key in source},
            "profile_name": request.source_profile.name,
        },
        "target": {
            **{key: target[key] for key in ("format", "cli_version") if key in target},
            "profile_name": request.target_profile.name,
            "project_name": request.project_name,
        },
        "selection": document["selection"],
        "structure": document["structure"],
        "nodes": [
            {
                key: node[key]
                for key in (
                    "key",
                    "source_native_id",
                    "title",
                    "coverage",
                    "loss_details_available",
                    "target_id",
                    "parent_target_id",
                    "operation_id",
                    "source_sha256",
                    "target_sha256",
                    "losses",
                    "warnings",
                )
                if key in node
            }
            for node in document["nodes"]
        ],
        "detached_node_keys": document.get("detached_node_keys", []),
        "requires_partial_acknowledgement": document.get("requires_partial_acknowledgement", False),
        "partial_acknowledged": document.get("partial_acknowledged", False),
        "effects": ["publish_selected_persistent_tree"],
    }


def _validate(request, action):
    _require(type(request) is RuntimeRequest and action in NATIVE_ACTIONS, "request_invalid")
    _require(type(request.source_profile) is Profile and type(request.target_profile) is Profile)
    _require(type(request.source_ref) is SessionRef and type(request.options) is OperationRequest)
    for path in (request.catalog_path, request.state_home, request.options.cwd):
        _require(isinstance(path, Path) and path.is_absolute() and ".." not in path.parts)
    _require(request.options.cwd.is_dir(), "request_invalid")
    _require(request.options.command is OperationKind.TRANSFER)
    # Only closed launch-selected destination options, never a second source or path.
    for name in (
        "path",
        "source_format",
        "output",
        "home",
        "source_home",
        "source_cwd",
        "source_cli",
        "source_id",
        "catalog",
        "catalog_id",
        "title",
        "target_cli",
        "session_id",
    ):
        _require(getattr(request.options, name) is None, "request_invalid")
    _require(not request.options.dry_run, "request_invalid")
    for value in (
        request.options.model,
        request.options.model_provider,
        request.options.target_cli_version,
    ):
        _require(
            type(value) is str
            and 0 < len(value.encode("utf-8")) <= 256
            and all(ord(c) >= 32 and ord(c) != 127 for c in value),
            "request_invalid",
        )
    _require(type(request.base_environ) is dict and type(request.observer_environ) is dict)
    _require(request.binding_check is None or callable(request.binding_check))
    _require(
        request.source_ref.profile_id == request.source_profile.id
        and request.source_ref.profile_fingerprint == request.source_profile.fingerprint
        and request.source_ref.root == request.source_profile.root
        and request.source_ref.format is request.source_profile.format,
        "source_changed",
    )
    _require(type(request.selection) is tuple and len(request.selection) <= 128)
    for item in request.selection:
        _require(type(item) is SelectionRangeV1, "request_invalid")
        item.__post_init__()
    _require(bool(request.selection) == (action != "return_new_full"), "request_invalid")
    _require((request.parent_operation_id is None) == (action == "summary"), "request_invalid")
    if request.parent_operation_id is not None:
        _require(type(request.parent_operation_id) is str and bool(request.parent_operation_id))


def _binding(request):
    # Identity/options/environment stay private; seal the exact captured launch resolution.
    return hashlib.sha256(
        repr(
            (
                request.catalog_path,
                request.source_profile.fingerprint,
                request.source_ref,
                request.target_profile.fingerprint,
                tuple(
                    getattr(request.options, name) for name in request.options.__dataclass_fields__
                ),
                request.state_home,
                sorted(request.base_environ.items()),
                sorted(request.observer_environ.items()),
                request.selection,
                request.source_client_version,
                request.parent_operation_id,
                id(request.binding_check),
            )
        ).encode("utf-8")
    ).hexdigest()


@dataclass(slots=True, repr=False)
class _RuntimeState:
    run_id: str
    action: str
    request: RuntimeRequest
    binding: str
    owner_thread: int
    catalog: Catalog | None = None
    observer: EndpointObserver | None = None
    draft: HandoffDraft | None = None
    core: PreparedOperation | None = None
    stage: str = "capture"
    document: str | None = None
    metadata: dict | None = None
    core_fingerprint: str | None = None


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class PrivateRuntimeHandle:
    def __init__(self):
        raise RuntimeJobError()

    def discard(self):
        if self not in _ISSUED:
            return
        state = _state(self)
        # Clear editable copies even if a subsequent Catalog.close needs retry.
        state.document = state.metadata = None
        for name in ("draft", "core"):
            handle = getattr(state, name)
            if handle is not None:
                handle.discard()
                setattr(state, name, None)
        if state.catalog is not None:
            state.catalog.close()
            state.catalog = None
        state.document = state.metadata = state.observer = None
        _ISSUED.pop(self, None)


def _state(handle, run_id=None):
    _require(type(handle) is PrivateRuntimeHandle and handle in _ISSUED)
    state = _ISSUED[handle]
    _require(state.owner_thread == threading.get_ident(), "worker_ownership_required")
    _require(run_id is None or state.run_id == run_id, "private_handle_required")
    return state


def _guard(state, external):
    if state.action == "tree":
        _require(_tree_binding(state.request) == state.binding, "source_changed")
    else:
        _validate(state.request, state.action)
        _require(_binding(state.request) == state.binding, "source_changed")
    if state.request.binding_check is not None:
        state.request.binding_check()
    if external is not None:
        external()


def new_runtime(request, action, run_id):
    if action == "tree":
        binding = _tree_binding(request)
    else:
        _validate(request, action)
        binding = _binding(request)
    handle = object.__new__(PrivateRuntimeHandle)
    _ISSUED[handle] = _RuntimeState(run_id, action, request, binding, threading.get_ident())
    return handle


def capture_runtime(handle, *, cancellation, prepublication_check):
    state = _state(handle)
    _require(state.stage == "capture")
    if state.action == "tree":
        cancellation.check()
        _guard(state, prepublication_check)
        state.stage = "accepted"
        return None
    request = state.request
    cancellation.check()
    _guard(state, prepublication_check)
    _require(request.catalog_path.stat().st_size <= 8 * 1024 * 1024, "buffer_capacity")
    state.catalog = Catalog.open_readonly_existing(request.catalog_path)
    resolve_source(state.catalog, request.source_profile, request.source_ref)
    profiles = tuple({p.id: p for p in (request.source_profile, request.target_profile)}.values())
    state.observer = EndpointObserver(
        state.catalog, profiles, base_environ=request.observer_environ, byte_limit=SOURCE_BYTES
    )
    if state.action == "return_new_full":
        state.stage = "accepted"
        return
    state.draft = prepare_handoff(
        request.source_ref,
        request.selection,
        HandoffLimits(
            max_source_bytes=SOURCE_BYTES,
            max_ranges=128,
            max_document_bytes=DOCUMENT_BYTES,
            max_edit_bytes=DOCUMENT_BYTES,
        ),
        state.observer,
        client_version=request.source_client_version,
    )
    document = render_handoff(state.draft)
    metadata = handoff_metadata(state.draft)
    state.document = document
    state.metadata = {k: v for k, v in metadata.items() if k != "source_identity"}
    cancellation.check()
    _guard(state, prepublication_check)
    state.stage = "editable"
    return document, json.loads(json.dumps(state.metadata))


def prepare_runtime(
    handle,
    *,
    document=None,
    document_sha256=None,
    cancellation,
    prepublication_check,
    progress=None,
):
    state = _state(handle)
    _require(state.stage in {"editable", "accepted"})
    request = state.request
    cancellation.check()
    _guard(state, prepublication_check)
    if state.action == "tree":
        from session_migrate.tree_cli import TreeWorkflow

        state.core = TreeWorkflow(
            (request.source_profile, request.target_profile)
            if request.source_profile.id != request.target_profile.id
            else (request.source_profile,),
            state_home=request.state_home,
            base_environ=request.base_environ,
            max_source_bytes=SOURCE_BYTES,
            max_total_source_bytes=2 * SOURCE_BYTES,
        )
        state.core.load(
            source_profile_id=request.source_profile.id,
            client_version=request.source_client_version,
            manifest=request.manifest,
        )
        state.core.select(
            mode=request.mode,
            anchor=request.anchor,
            detach=request.detach,
            hierarchy_keys=request.hierarchy_keys,
        )
        raw = state.core.prepare(
            request.target_profile.id,
            cwd=request.cwd,
            model_provider=request.model_provider,
            model=request.model,
            acknowledge_partial=request.acknowledge_partial,
            prepublication_check=lambda: _guard(state, prepublication_check),
        )
        state.metadata = _tree_metadata(raw, request)
        state.core_fingerprint = hashlib.sha256(workflow_json(state.metadata).encode()).hexdigest()
        cancellation.check()
        _guard(state, prepublication_check)
        state.stage = "prepared"
        return
    reviewed = None
    try:
        if state.stage == "editable":
            draft, state.draft = state.draft, None
            state.document = None
            state.stage = "accepted"
            reviewed = accept_user_text(draft, document, confirmed_sha256=document_sha256)
            state.metadata = {
                k: v for k, v in handoff_metadata(reviewed).items() if k != "source_identity"
            }

        def guard():
            _guard(state, prepublication_check)

        callbacks = dict(
            base_environ=request.base_environ,
            cancellation=cancellation,
            prepublication_check=guard,
            progress=progress,
        )
        if state.action == "summary":
            state.core = prepare_summary(
                reviewed,
                request.target_profile,
                request.options,
                state.observer,
                state_home=request.state_home,
                **callbacks,
            )
        else:
            state.core = prepare_return_new(
                ReturnNewRequest(
                    request.source_ref,
                    request.parent_operation_id,
                    request.target_profile,
                    "full" if state.action == "return_new_full" else "summary",
                    request.options,
                    reviewed,
                ),
                state.observer,
                request.state_home,
                **callbacks,
            )
        _require(type(state.core) is PreparedOperation)
        cancellation.check()
        guard()
        raw = proposal_metadata(state.core).to_dict()
        state.core_fingerprint = hashlib.sha256(
            json.dumps(raw, sort_keys=True, ensure_ascii=True).encode("ascii")
        ).hexdigest()
        state.stage = "prepared"
    finally:
        if reviewed is not None:
            reviewed.discard()


def runtime_metadata(handle):
    state = _state(handle)
    if state.action == "tree":
        _require(state.stage == "prepared" and state.core is not None)
        document = workflow_json(state.metadata)
        return (
            state.metadata["operation_id"],
            document,
            hashlib.sha256(document.encode()).hexdigest(),
            tuple(state.metadata["effects"]),
            RUNTIME_BUFFER_BYTES,
        )
    _require(state.stage == "prepared" and type(state.core) is PreparedOperation)
    core = proposal_metadata(state.core)
    raw = core.to_dict()
    safe = {
        "schema_version": 1,
        "native_action": state.action,
        "core_metadata_sha256": state.core_fingerprint,
        "binding_sha256": state.binding,
        "handoff": state.metadata,
        "target_cli_version": state.request.options.target_cli_version,
        "model_provider": state.request.options.model_provider,
        "model": state.request.options.model,
        "effects": raw.get("effects", []),
    }
    encoded = json.dumps(safe, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    _require(len(encoded) <= 1024 * 1024, "metadata_limit")
    return (
        core.proposal_id,
        encoded,
        hashlib.sha256(encoded.encode("ascii")).hexdigest(),
        tuple(safe["effects"]),
        RUNTIME_BUFFER_BYTES,
    )


def commit_runtime(handle, run_id, *, cancellation, progress=None):
    state = _state(handle, run_id)
    if state.action == "tree":
        _require(state.stage == "prepared" and state.core is not None)
        _guard(state, None)
        current = _tree_metadata(state.core.proposal(), state.request)
        _require(
            hashlib.sha256(workflow_json(current).encode()).hexdigest() == state.core_fingerprint,
            "source_changed",
        )
        state.stage = "committing"
        result = state.core.commit(cancellation=cancellation, progress=progress)
        state.stage = "completed"
        return {
            "schema_version": 1,
            "native_action": "tree",
            "operation_id": result["operation_id"],
            "status": result["status"],
            "nodes": [
                {
                    key: node[key]
                    for key in (
                        "key",
                        "target_id",
                        "parent_target_id",
                        "operation_id",
                        "status",
                    )
                    if key in node
                }
                for node in result["nodes"]
            ],
        }
    _require(state.stage == "prepared" and type(state.core) is PreparedOperation)
    raw = proposal_metadata(state.core).to_dict()
    _require(
        hashlib.sha256(
            json.dumps(raw, sort_keys=True, ensure_ascii=True).encode("ascii")
        ).hexdigest()
        == state.core_fingerprint
    )
    state.stage = "committing"
    receipt = commit_operation(state.core, cancellation=cancellation, progress=progress)
    _require(type(receipt) is TransferReceipt, "receipt_unavailable")
    state.stage = "completed"
    # Public DTOs retain real result IDs and hashes, without native paths or private identities.
    legacy = receipt.legacy_result
    return {
        "schema_version": 1,
        "proposal_id": receipt.proposal.proposal_id,
        "status": receipt.status,
        "native_action": state.action,
        "effects": receipt.proposal.to_dict()["effects"],
        "legacy_result": {
            k: legacy[k]
            for k in (
                "dry_run",
                "source_format",
                "target_format",
                "session_id",
                "records",
                "sha256",
                "dropped_events",
            )
            if k in legacy
        },
    }
