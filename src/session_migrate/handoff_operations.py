"""Reviewed USER handoffs and explicit return-new through the genuine B3 runtime."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
from pathlib import Path
from weakref import WeakKeyDictionary

from session_migrate.canonical import CanonicalPolicy
from session_migrate.continuity import ContinuitySelector
from session_migrate.continuity_observation import (
    _original_authority,
    observe_endpoint,
    resolve_profile,
    validate_endpoint,
    validate_observer,
)
from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError
from session_migrate.handoff import _consume_reviewed
from session_migrate.handoff_types import OriginalNativeAuthority, validate_derived_input
from session_migrate.lineage import (
    MAX_RECEIPT_BYTES,
    CheckpointRecord,
    CompletionReceipt,
    EdgeRecord,
    LineageStore,
    NativeState,
    OperationRecord,
    OperationState,
    ReturnLinkV1,
    _decode,
    _encode,
)
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_source
from session_migrate.operations import OperationKind, OperationRequest
from session_migrate.profiles import Profile, SessionRef, _prepared_profile_binding, resolve_source


class HandoffOperationError(SessionMigrateError):
    def __init__(self, code="invalid_request"):
        self.code = (
            code
            if code
            in {
                "invalid_request",
                "invalid_context",
                "source_changed",
                "parent_unavailable",
                "parent_changed",
                "return_parent_unqualified",
                "parent_source_mismatch",
                "target_identity_reused",
                "target_full_history_unproven",
                "input_unavailable",
                "handle_capacity",
            }
            else "invalid_request"
        )
        super().__init__(f"Handoff operation: {self.code}.")


class RuntimePersistenceUncertainError(OperationNeedsReconcileError):
    """The same native publication needs inspection after ledger rename."""

    code = "persistence_uncertain"


def _require(condition, code="invalid_request"):
    if not condition:
        raise HandoffOperationError(code)


def _sha(value):
    return hashlib.sha256(_encode(value)).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class ReturnNewRequest:
    current_source_ref: SessionRef
    selected_parent_operation_id: str
    current_target_profile: Profile
    content_mode: str
    target_options: OperationRequest
    reviewed: object = None

    def __post_init__(self):
        _require(
            type(self.current_source_ref) is SessionRef
            and type(self.current_target_profile) is Profile
            and type(self.target_options) is OperationRequest
            and self.content_mode in {"full", "summary"}
        )
        ContinuitySelector(operation_id=self.selected_parent_operation_id)
        _require((self.content_mode == "summary") == (self.reviewed is not None))


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class _RuntimeContext:
    authority: object
    derived_input: object
    transformation: object
    return_link: ReturnLinkV1 | None
    parent_record_sha: str | None
    store_path: Path
    parent_store_uuid: str | None
    forbidden_native_ids: tuple[str, ...]

    def __init__(self):
        raise HandoffOperationError("invalid_context")


_CONTEXTS: WeakKeyDictionary = WeakKeyDictionary()
_HELD_COORDINATOR: ContextVar = ContextVar("handoff_held_coordinator", default=None)


def _context_binding(context):
    return _sha(
        (
            id(context.authority),
            context.authority.identity,
            context.authority.authority_sha256,
            context.derived_input.identity if context.derived_input else None,
            context.transformation,
            context.return_link,
            context.parent_record_sha,
            str(context.store_path),
            context.parent_store_uuid,
            context.forbidden_native_ids,
        )
    )


def _issue_context(
    authority,
    derived_input,
    transformation,
    store_path,
    *,
    return_link=None,
    parent_record_sha=None,
    parent_store_uuid=None,
    forbidden_native_ids=(),
):
    _require(type(authority) is OriginalNativeAuthority, "invalid_context")
    authority.check()
    if derived_input is not None:
        validate_derived_input(derived_input)
        _require(transformation.input == derived_input.identity, "invalid_context")
    _require(len(_CONTEXTS) < 64, "handle_capacity")
    context = object.__new__(_RuntimeContext)
    for name, value in locals().copy().items():
        if name in {field.name for field in fields(_RuntimeContext)}:
            object.__setattr__(context, name, value)
    _CONTEXTS[context] = _context_binding(context)
    validate_runtime_context(context)
    return context


def _parent(view, operation_id):
    return next((op for op in view.operations if op.record.operation_id == operation_id), None)


def _verify_parent_records(record, receipt, source, target, edge):
    _require(record.source.identity is not None, "return_parent_unqualified")
    _require(
        source is not None
        and target is not None
        and receipt.input_artifact_sha256 == record.target_input_sha256
        and receipt.target_identity == target.identity
        and target.identity.namespace == record.target_namespace
        and target.identity.profile_fingerprint == record.target_profile_fingerprint
        and receipt.source_checkpoint_sha256 == _sha(source)
        and receipt.target_checkpoint_sha256 == _sha(target)
        and source.identity == record.source.identity
        and source.artifact_sha256 == record.source.artifact_sha256
        and source.snapshot_sha256 == record.source.snapshot_sha256
        and source.prefix_witness == record.source.prefix_witness
        and target.snapshot_sha256 == record.expected_projection.snapshot_sha256
        and target.prefix_witness == record.expected_projection.prefix_witness
        and source.versions == target.versions == record.versions,
        "parent_unavailable",
    )
    _require(
        edge is not None
        and edge.source_identity == source.identity
        and edge.target_identity == target.identity
        and edge.source_checkpoint_sha256 == _sha(source)
        and edge.target_checkpoint_sha256 == _sha(target)
        and edge.derivation == receipt.derivation
        and edge.return_link == receipt.return_link == record.return_link,
        "parent_unavailable",
    )


def _verified_parent(store_path, operation_id):
    view = LineageStore.inspect_existing(store_path)
    _require(view is not None, "parent_unavailable")
    op = _parent(view, operation_id)
    _require(
        op is not None
        and op.durable_state is OperationState.COMPLETED
        and type(op.receipt) is CompletionReceipt,
        "parent_unavailable",
    )
    _require(op.record.source.identity is not None, "return_parent_unqualified")
    edges = [edge for edge in view.edges if edge.operation_id == operation_id]
    _require(len(edges) == 1, "parent_unavailable")
    _verify_parent_records(
        op.record, op.receipt, op.source_checkpoint, op.target_checkpoint, edges[0]
    )
    return view, op


def _coordinator_current(binding):
    store, guard, record, context = binding
    _require(
        type(store) is LineageStore and not store._readonly and store._mutex._is_owned(),
        "invalid_context",
    )
    store._check()
    store._guard(record.operation_id, guard, record)
    store._bind_store_identity(guard._store_uuid, *store._main_witness)
    if context is not None:
        _require(
            context in _CONTEXTS and _context_binding(context) == _CONTEXTS[context],
            "invalid_context",
        )
        _require(context.store_path == _store_path(store), "invalid_context")
    return store, guard, record


def _store_path(store):
    return Path("/", *(name for _, name, _ in store._namespace.chain if name is not None))


@contextmanager
def _held_coordinator(store, guard, record, context=None):
    """Private scope only inside complete_reconciled's already-held EX callback.

    This neither acquires nor releases a flock. The genuine store API owns EX;
    current RAM issuer, operation guard and physical image are checked on reads.
    """
    _require(type(record) is OperationRecord and _HELD_COORDINATOR.get() is None, "invalid_context")
    binding = (store, guard, record, context)
    _coordinator_current(binding)
    token = _HELD_COORDINATOR.set(binding)
    try:
        yield
        _coordinator_current(binding)
    finally:
        _HELD_COORDINATOR.reset(token)


def _validate_parent_binding(
    store_path,
    link,
    *,
    expected_record_sha=None,
    expected_store_uuid=None,
    source_identity=None,
    operation_record=None,
):
    binding = _HELD_COORDINATOR.get()
    if binding is None:
        view, parent = _verified_parent(store_path, link.parent_operation_id)
        store_uuid, record, receipt = view.store_uuid, parent.record, parent.receipt
        if operation_record is not None:
            actual = _parent(view, operation_record.operation_id)
            _require(
                type(operation_record) is OperationRecord
                and actual is not None
                and actual.record == operation_record
                and actual.durable_native_state is NativeState.ACKNOWLEDGED
                and actual.durable_state
                in {
                    OperationState.VERIFYING,
                    OperationState.NEEDS_RECONCILE,
                    OperationState.COMPLETED,
                },
                "parent_changed",
            )
    else:
        store, guard, current = _coordinator_current(binding)
        _require(
            _store_path(store) == store_path and current.return_link == link, "invalid_context"
        )
        _require(operation_record is None or operation_record == current, "parent_changed")
        connection, _ = store._load()
        try:
            state, native, _, physical_current = store._row(connection, current.operation_id)
            _require(
                native is NativeState.ACKNOWLEDGED
                and state
                in {
                    OperationState.VERIFYING,
                    OperationState.NEEDS_RECONCILE,
                    OperationState.COMPLETED,
                }
                and physical_current == current,
                "parent_changed",
            )
            store._guard(current.operation_id, guard, physical_current)
            parent_state, parent_native, _, record = store._row(
                connection, link.parent_operation_id
            )
            _require(
                parent_state is OperationState.COMPLETED
                and parent_native is NativeState.ACKNOWLEDGED,
                "parent_changed",
            )
            row = connection.execute(
                "SELECT record_json FROM receipts WHERE operation_id=?", (link.parent_operation_id,)
            ).fetchone()
            _require(row is not None, "parent_changed")
            receipt = _decode(CompletionReceipt, row[0], MAX_RECEIPT_BYTES)
            checkpoints = {
                side: _decode(CheckpointRecord, payload)
                for side, payload in connection.execute(
                    "SELECT side,record_json FROM checkpoints WHERE operation_id=?",
                    (link.parent_operation_id,),
                )
            }
            row = connection.execute(
                "SELECT record_json FROM edges WHERE operation_id=?", (link.parent_operation_id,)
            ).fetchone()
            _require(row is not None, "parent_changed")
            _verify_parent_records(
                record,
                receipt,
                checkpoints.get("source"),
                checkpoints.get("target"),
                _decode(EdgeRecord, row[0]),
            )
            store_uuid = store._store_uuid
        finally:
            connection.close()
        _coordinator_current(binding)
    _require(
        _sha(receipt) == link.parent_receipt_sha256
        and receipt.target_identity == link.parent_target_identity
        and (source_identity is None or receipt.target_identity == source_identity)
        and (expected_record_sha is None or _sha(record) == expected_record_sha)
        and (expected_store_uuid is None or store_uuid == expected_store_uuid),
        "parent_changed",
    )
    return store_uuid, _sha(record)


def validate_runtime_context(context, *, fresh=False):
    _require(type(context) is _RuntimeContext and context in _CONTEXTS, "invalid_context")
    _require(_context_binding(context) == _CONTEXTS[context], "invalid_context")
    if context.derived_input is not None:
        validate_derived_input(context.derived_input)
    if context.return_link is not None:
        _validate_parent_binding(
            context.store_path,
            context.return_link,
            expected_store_uuid=context.parent_store_uuid,
            expected_record_sha=context.parent_record_sha,
            source_identity=context.authority.identity,
        )
    if fresh:
        context.authority.check()


def _base_environment(observer, values):
    return dict(observer._environment if values is None else values)


def _opencode_catalog_selection(observer, request, authority):
    """Bind a real virtual source without treating its export as a native file."""
    from session_migrate.catalog import Catalog
    from session_migrate.recovered_projections import _catalog_witness, _selection_metadata

    validate_observer(observer)
    _require(type(authority) is OriginalNativeAuthority, "invalid_context")
    profile, ref = authority.profile, authority.source_ref
    _require(
        profile.format is AgentFormat.OPENCODE
        and request.command is OperationKind.TRANSFER
        and request.catalog == observer._catalog.path
        and request.source_format is profile.format
        and bool(request.catalog_id)
        and request.title is None
        and request.source_id is None
        and request.source_home is None
        and request.source_cwd is None,
        "invalid_context",
    )
    connection = observer._catalog._connection
    _require(isinstance(connection, sqlite3.Connection), "source_changed")
    connection.execute("SELECT 1").fetchone()
    witness = _catalog_witness(request.catalog)

    def selection(catalog, *, native):
        entry = catalog.get_session(request.catalog_id, include_paths=True)
        _require(
            entry.format == profile.format.value
            and entry.root == str(profile.root)
            and entry.session_id == ref.native_id,
            "source_changed",
        )
        locator = (
            resolve_source(catalog, profile, ref)
            if native
            else catalog.session_source_for_transfer(entry.catalog_id)
        )
        _require(
            locator.format is profile.format
            and locator.root == profile.root
            and locator.session_id == ref.native_id,
            "source_changed",
        )
        return entry, locator, profile, ref

    with Catalog.open_readonly_existing(request.catalog) as catalog:
        selected = selection(catalog, native=True)
    with Catalog.open_readonly_existing(request.catalog) as catalog:
        current = selection(catalog, native=False)
    cached = selection(observer._catalog, native=False)
    _require(
        _selection_metadata(selected) == _selection_metadata(current)
        and _selection_metadata(current) == _selection_metadata(cached)
        and _catalog_witness(request.catalog) == witness
        and observer._catalog._connection is connection,
        "source_changed",
    )
    validate_observer(observer)
    return *current, witness


def _prepare_context_operation(
    context,
    target_profile,
    options,
    observer,
    *,
    base_environ=None,
    state_home=None,
    cancellation=None,
):
    from session_migrate.conversion import default_migration_state_home
    from session_migrate.operations import CancellationToken, _prepare_operation

    _require(type(target_profile) is Profile and type(options) is OperationRequest)
    _require(options.command is OperationKind.TRANSFER)
    validate_observer(observer)
    captured_observer = context.authority.observer
    current_source = resolve_profile(observer, context.authority.source_ref)
    _require(
        current_source.fingerprint == context.authority.profile.fingerprint
        and observer._catalog.path == captured_observer._catalog.path
        and dict(observer._environment) == dict(captured_observer._environment)
        and observer._legacy_runtime_contexts == captured_observer._legacy_runtime_contexts
        and len(context.authority.snapshot.raw_bytes) <= observer._byte_limit,
        "invalid_context",
    )
    base = _base_environment(observer, base_environ)
    state_home = state_home or default_migration_state_home(environ=base)
    _require(state_home / "lineage-v1" == context.store_path, "invalid_context")
    source = context.authority.profile
    physical_selection = []

    def source_guard():
        from session_migrate.recovered_projections import _catalog_selection, _selection_metadata

        def selection():
            if source.format is AgentFormat.OPENCODE:
                return _opencode_catalog_selection(observer, bound, context.authority)
            return _catalog_selection(observer, bound)

        current = selection()
        binding = _selection_metadata(current[:4])
        witness = current[4]
        if physical_selection:
            _require((binding, witness) == physical_selection[0], "source_changed")
        else:
            physical_selection.append((binding, witness))
        validate_runtime_context(context, fresh=True)
        _require(selection()[4] == witness, "source_changed")

    bound, target_env, source_env, profile_guard, profiles = _prepared_profile_binding(
        observer._catalog,
        source,
        context.authority.source_ref,
        target_profile,
        options,
        base,
        source_guard,
        readonly_catalog=True,
    )
    token = cancellation or CancellationToken()
    token.claim()
    try:
        return _prepare_operation(
            bound,
            token,
            environ=target_env,
            source_environ=source_env,
            prepublication_check=profile_guard,
            profile_context=profiles,
            migration_state_home=state_home,
            runtime_context=context,
        )
    finally:
        token.close()


def prepare_summary(
    reviewed,
    target_profile,
    options,
    observer,
    *,
    base_environ=None,
    state_home=None,
    cancellation=None,
):
    # Reviewed is consumed before target preflight, environment resolution or I/O.
    authority, derived_input, transformation = _consume_reviewed(reviewed)
    validate_observer(observer)
    from session_migrate.conversion import default_migration_state_home

    base = _base_environment(observer, base_environ)
    state_home = state_home or default_migration_state_home(environ=base)
    _require(
        isinstance(state_home, Path) and state_home.is_absolute() and ".." not in state_home.parts
    )
    context = _issue_context(authority, derived_input, transformation, state_home / "lineage-v1")
    return _prepare_context_operation(
        context,
        target_profile,
        options,
        observer,
        base_environ=base,
        state_home=state_home,
        cancellation=cancellation,
    )


def _current_source_client(observer, profile, ref):
    try:
        if profile.format in {AgentFormat.CLAUDE, AgentFormat.CODEX}:
            selected = resolve_source(observer._catalog, profile, ref)
            _require(selected.path is not None, "invalid_context")
            capture = capture_source(
                SourceCaptureContext(profile.format, selected.path), byte_limit=observer._byte_limit
            )
            return capture.parse().cli_version, capture
        if profile.format is AgentFormat.OPENCODE:
            from session_migrate.conversion import detect_opencode_version

            return detect_opencode_version(
                profile.cli, environ=profile.environment(observer._environment)
            ), None
        raise HandoffOperationError("invalid_context")
    except (SessionMigrateError, sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        raise HandoffOperationError("source_changed") from None


def prepare_return_new(request, observer, state_home, *, base_environ=None, cancellation=None):
    # Summary review is consumed before even reading the parent ledger.
    _require(type(request) is ReturnNewRequest)
    consumed = _consume_reviewed(request.reviewed) if request.reviewed is not None else None
    request.__post_init__()
    _require(
        not (
            request.content_mode == "full"
            and request.current_target_profile.format is AgentFormat.OPENCODE
        ),
        "target_full_history_unproven",
    )
    _require(
        isinstance(state_home, Path) and state_home.is_absolute() and ".." not in state_home.parts
    )
    validate_observer(observer)
    source_profile = resolve_profile(observer, request.current_source_ref)
    view, parent = _verified_parent(state_home / "lineage-v1", request.selected_parent_operation_id)
    if consumed is None:
        current_client, version_capture = _current_source_client(
            observer, source_profile, request.current_source_ref
        )
        _require(type(current_client) is str, "invalid_context")
        try:
            endpoint = observe_endpoint(
                observer,
                request.current_source_ref,
                client_version=current_client,
                canonical_policy=CanonicalPolicy(),
            )
        except (SessionMigrateError, sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
            raise HandoffOperationError("source_changed") from None
        validate_endpoint(endpoint, observer=observer, ref=request.current_source_ref)
        _require(
            version_capture is None
            or endpoint.authority_sha256 == version_capture.authority_sha256,
            "source_changed",
        )
        authority = _original_authority(endpoint, observer, request.current_source_ref)
        derived_input = transformation = None
    else:
        authority, derived_input, transformation = consumed
    _require(
        authority.source_ref == request.current_source_ref
        and authority.profile.fingerprint == source_profile.fingerprint
        and authority.identity == parent.receipt.target_identity,
        "parent_source_mismatch",
    )
    link = ReturnLinkV1(
        1, parent.record.operation_id, _sha(parent.receipt), parent.receipt.target_identity
    )
    forbidden = (
        parent.record.source.identity.namespace.native_id,
        parent.receipt.target_identity.namespace.native_id,
    )
    context = _issue_context(
        authority,
        derived_input,
        transformation,
        state_home / "lineage-v1",
        return_link=link,
        parent_record_sha=_sha(parent.record),
        parent_store_uuid=view.store_uuid,
        forbidden_native_ids=forbidden,
    )
    prepared = _prepare_context_operation(
        context,
        request.current_target_profile,
        request.target_options,
        observer,
        base_environ=base_environ,
        state_home=state_home,
        cancellation=cancellation,
    )
    if prepared._state.built.artifact.session_id in forbidden:
        prepared.discard()
        raise HandoffOperationError("target_identity_reused")
    return prepared
