"""Worker-owned, genuinely issued service capabilities; no HTTP replay authority."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

from session_migrate import adoption, reconciliation
from session_migrate.baselines import BaselineReceipt, _encode_baseline
from session_migrate.catalog import Catalog
from session_migrate.continuity import ExplicitPair
from session_migrate.continuity_observation import EndpointObserver, validate_observer
from session_migrate.lineage import (
    CompletionReceipt,
    LineageStore,
    NativeState,
    OperationState,
    RecoveryOperation,
    _encode,
    _validate_cancel_expectations,
)

SERVICE_ACTIONS = frozenset({"adopt", "reconcile", "cancel_ready"})
SERVICE_BUFFER_BYTES = 64 * 1024 * 1024
SERVICE_CAPTURE_BYTES = 4 * 1024 * 1024
_ISSUED = WeakKeyDictionary()


class ServicePreparationError(Exception):
    def __init__(self, reason, *, metadata=None, recovered=False, subject=None):
        self.reason, self.metadata, self.recovered = reason, metadata, recovered
        self.subject = subject
        super().__init__(reason)


def _require(condition, reason="private_handle_required"):
    if not condition:
        raise ServicePreparationError(reason)


def _json(value):
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    _require(len(encoded) <= 1024 * 1024, "metadata_limit")
    return encoded


def _binding(value):
    # Launch-owned opaque IDs and SHA bindings only, never paths or ENV.
    _require(type(value) is dict and len(value) <= 32, "binding_invalid")
    for key, item in value.items():
        _require(
            type(key) is str
            and key.isascii()
            and key.replace("_", "").isalnum()
            and len(key) <= 64
            and type(item) is str
            and 0 < len(item) <= 128
            and item.isascii()
            and all(c.isalnum() or c in "_.:-" for c in item),
            "binding_invalid",
        )
    return json.loads(_json(value))


def _metadata(proposal, binding):
    document = proposal.to_dict()
    safe = {
        name: value
        for name, value in document.items()
        if name
        in {
            "schema_version",
            "status",
            "reason",
            "versions",
            "provenance",
            "durable_state",
            "native_state",
            "native_read_effects",
            "available_effects",
            "blockers",
            "boundary",
            "metadata_loss_counts",
        }
        or name.endswith("sha256")
    }
    safe["binding"] = binding
    safe["core_metadata_sha256"] = hashlib.sha256(_json(document).encode("ascii")).hexdigest()
    return safe


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class PrivateTypedServiceHandle:
    def __init__(self):
        raise ServicePreparationError("private_handle_required")

    def discard(self):
        if self not in _ISSUED:
            return
        state = _state(self)
        # sqlite's thread affinity is preserved for TTL, revoke, queue and close.
        _require(state.owner_thread == threading.get_ident(), "worker_ownership_required")
        if state.handle is not None:
            state.handle.discard()
            state.handle = None
        if state.catalog is not None:
            state.catalog.close()
            state.catalog = None
        _ISSUED.pop(self, None)


@dataclass(slots=True, repr=False)
class _ServiceState:
    action: str
    subject: dict
    metadata_json: str
    fingerprint: str
    effects: tuple[str, ...]
    handle: object
    catalog: Catalog | None
    owner_thread: int
    store_path: Path | None
    record_sha256: str | None
    store_uuid: str | None
    committed: bool = False


def _state(handle):
    _require(type(handle) is PrivateTypedServiceHandle and handle in _ISSUED)
    return _ISSUED[handle]


def service_metadata(handle):
    state = _state(handle)
    return (
        state.subject.copy(),
        state.metadata_json,
        state.fingerprint,
        state.effects,
        SERVICE_BUFFER_BYTES,
    )


def service_action(handle):
    return _state(handle).action


def prepare_service(
    action,
    *,
    observer=None,
    pair=None,
    context=None,
    operation_id=None,
    store_path=None,
    state_home=None,
    source_client_version=None,
    target_client_version=None,
    binding=None,
    expected_record_sha256=None,
    expected_store_uuid=None,
):
    """Call on the persistent jobs worker; transfer ownership of its readonly catalog.

    The caller resolves genuine profiles/refs/context and checks capabilities and
    selection freshness. Its observer/catalog must be opened on this same worker.
    Even a blocked preparation closes the transferred catalog on this worker.
    """
    catalog = handle = None
    try:
        _require(type(action) is str and action in SERVICE_ACTIONS, "action_invalid")
        binding = _binding(binding)
        if action != "cancel_ready":
            _require(type(observer) is EndpointObserver, "observer_required")
            catalog = observer._catalog
            _require(
                type(catalog) is Catalog
                and catalog._connection.execute("PRAGMA query_only").fetchone()[0] == 1,
                "readonly_catalog_required",
            )
            _require(
                catalog.path.stat().st_size <= 8 * 1024 * 1024
                and observer._byte_limit <= SERVICE_CAPTURE_BYTES,
                "buffer_capacity",
            )
            validate_observer(observer)
        if action == "adopt":
            _require(type(pair) is ExplicitPair, "pair_required")
            _require(
                type(source_client_version) is str and type(target_client_version) is str,
                "client_version_required",
            )
            proposal = adoption.prepare_adoption(
                pair,
                observer,
                state_home=state_home,
                source_client_version=source_client_version,
                target_client_version=target_client_version,
            )
            handle = proposal.handle
            subject = {"kind": "baseline", "id": proposal.baseline_id}
            _require(
                type(handle) is adoption.AdoptionHandle and handle in adoption._HANDLES,
                proposal.reason,
            )
            metadata = _metadata(proposal, binding)
            record_sha = store_uuid = None
        elif action == "reconcile":
            _require(type(context) is reconciliation.RecoveryContext, "context_unavailable")
            paths = (
                context.source_path,
                context.input_path,
                context.manifest_path,
                context.handoff_document,
            )
            _require(
                sum(path.stat().st_size for path in paths if path is not None) <= 8 * 1024 * 1024,
                "buffer_capacity",
            )
            proposal = reconciliation.prepare_reconciliation(
                operation_id,
                store_path=store_path,
                context=context,
                observer=observer,
            )
            handle = proposal.handle
            if proposal.status == "completed":
                _require(type(proposal.receipt) is CompletionReceipt, "receipt_unavailable")
                raise ServicePreparationError(
                    "completed",
                    metadata=_receipt(proposal.receipt),
                    recovered=True,
                    subject={"kind": "operation", "id": proposal.operation_id},
                )
            _require(
                type(handle) is reconciliation.ReconciliationHandle
                and handle in reconciliation._HANDLES
                and proposal.status == "ready",
                proposal.reason,
            )
            subject = {"kind": "operation", "id": proposal.operation_id}
            metadata = _metadata(proposal, binding)
            record_sha = proposal.record_sha256
            store_uuid = reconciliation._HANDLES[handle].store_uuid
        else:
            _validate_cancel_expectations(expected_record_sha256, expected_store_uuid)
            _require(expected_record_sha256 is not None, "binding_required")
            operation = reconciliation.inspect_recovered_operation(
                operation_id, store_path=store_path
            )
            view = LineageStore.inspect_existing(store_path)
            _require(
                type(operation) is RecoveryOperation
                and view is not None
                and view.store_uuid == expected_store_uuid
                and hashlib.sha256(_encode(operation.record)).hexdigest() == expected_record_sha256,
                "ledger_changed",
            )
            _require(
                operation.durable_state is OperationState.READY
                and operation.durable_native_state is NativeState.NATIVE_NOT_STARTED,
                "not_cancelable",
            )
            subject = {"kind": "operation", "id": operation.record.operation_id}
            record_sha, store_uuid = expected_record_sha256, expected_store_uuid
            metadata = {
                "status": "ready",
                "reason": "recovered_ready",
                "record_sha256": record_sha,
                "store_uuid": store_uuid,
                "durable_state": "ready",
                "native_state": "not_started",
                "available_effects": ["cancel_ready"],
                "native_read_effects": [],
                "binding": binding,
            }
        metadata_json = _json(metadata)
        fingerprint = hashlib.sha256(
            _json({"service_action": action, "subject": subject, "metadata": metadata}).encode(
                "ascii"
            )
        ).hexdigest()
        bundle = object.__new__(PrivateTypedServiceHandle)
        effects = tuple(metadata.get("native_read_effects", ())) + tuple(
            metadata.get("available_effects", ())
        )
        _ISSUED[bundle] = _ServiceState(
            action,
            subject,
            metadata_json,
            fingerprint,
            effects,
            handle,
            catalog,
            threading.get_ident(),
            store_path,
            record_sha,
            store_uuid,
        )
        handle = catalog = None
        return bundle
    finally:
        if handle is not None:
            handle.discard()
        if catalog is not None:
            # A failed preparation still owns its readonly connection on this
            # worker. Its admitted charge remains held while close is retried.
            while True:
                try:
                    catalog.close()
                    break
                except Exception:
                    time.sleep(0.05)


def _receipt(receipt):
    if type(receipt) is BaselineReceipt:
        receipt.__post_init__()
        encoded = _encode_baseline(receipt)
        names = (
            "schema_version",
            "kind",
            "provenance",
            "baseline_id",
            "record_sha",
            "pair_key",
            "created_at_utc",
        )
    elif type(receipt) is CompletionReceipt:
        receipt.__post_init__()
        encoded = _encode(receipt)
        names = (
            "schema_version",
            "operation_id",
            "input_artifact_sha256",
            "continuity_reason",
            "source_checkpoint_sha256",
            "target_checkpoint_sha256",
        )
    else:
        raise ServicePreparationError("receipt_unavailable")
    decoded = json.loads(encoded)
    return {
        "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
        "receipt_projection": {name: decoded[name] for name in names},
    }


def commit_service(bundle, lease):
    state = _state(bundle)
    _require(
        state.owner_thread == threading.get_ident() and not state.committed,
        "worker_ownership_required",
    )
    # The same authentication lock arbitrates entry against revoke/cancel/expiry.
    # No core I/O, including lockfiles, occurs under it.
    lease.enter_service(bundle)
    state.committed = True
    if state.action == "adopt":
        receipt = adoption.commit_adoption(state.handle)
        _require(
            type(receipt) is BaselineReceipt and receipt.baseline_id == state.subject["id"],
            "receipt_unavailable",
        )
        return _receipt(receipt)
    if state.action == "reconcile":
        receipt = reconciliation.commit_reconciliation(state.handle)
        _require(
            type(receipt) is CompletionReceipt and receipt.operation_id == state.subject["id"],
            "receipt_unavailable",
        )
        return _receipt(receipt)
    reconciliation.cancel_recovered_ready(
        state.subject["id"],
        store_path=state.store_path,
        expected_record_sha256=state.record_sha256,
        expected_store_uuid=state.store_uuid,
    )
    operation = reconciliation.inspect_recovered_operation(
        state.subject["id"], store_path=state.store_path
    )
    view = LineageStore.inspect_existing(state.store_path)
    _require(
        type(operation) is RecoveryOperation
        and view is not None
        and view.store_uuid == state.store_uuid
        and operation.record.operation_id == state.subject["id"]
        and hashlib.sha256(_encode(operation.record)).hexdigest() == state.record_sha256
        and operation.durable_state is OperationState.CANCELLED
        and operation.durable_native_state is NativeState.NATIVE_NOT_STARTED,
        "cancellation_unconfirmed",
    )
    return {
        "operation_id": state.subject["id"],
        "record_sha256": state.record_sha256,
        "durable_state": operation.durable_state.value,
        "native_state": operation.durable_native_state.value,
    }
