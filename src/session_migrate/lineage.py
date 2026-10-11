"""Private, content-free lineage ledger.

SQLite only sees an in-memory image. POSIX file descriptors, persistent flock
locks, and atomic replacement own the on-disk namespace. Native roots are
validated metadata supplied by adapters: this module never opens them.

Recovery is inspection, never a recovered request to publish. Locks coordinate
instances sharing this state directory; they do not lock external clients.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import posixpath
import re
import sqlite3
import stat
import sys
import threading
import types
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, EventKind
from session_migrate.witnesses import (
    BoundaryKind,
    BoundaryWitness,
    PrefixWitness,
    SequenceWitness,
    WitnessValidationError,
)

MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 32 * 1024
MAX_UNITS = 1024
MAX_OPERATIONS = 4096
MAX_REFERENCES = 16
INITIAL_CLOSE_BUDGET = 512 * 1024
MIN_CLOSE_BUDGET = 384 * 1024
APPLICATION_ID = 0x534D4C31
SCHEMA_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}\Z")
_UUID = re.compile(r"[0-9a-f]{32}\Z")
_LOCK_NAME = re.compile(r"[0-9a-f]{64}\.lock\Z")
_TEMP_NAME = re.compile(r"\.ledger-[0-9a-f]{32}\.tmp\Z")
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _FILE_FLAGS
_ERROR_CODES = frozenset(
    (
        "invalid_metadata",
        "metadata_limit",
        "noncanonical_metadata",
        "invalid_image",
        "unknown_schema",
        "image_budget",
        "image_limit",
        "image_changed",
        "image_read_failed",
        "image_write_failed",
        "persistence_uncertain",
        "record_limit",
        "invalid_budget",
        "unsafe_file",
        "unsafe_directory",
        "unsafe_ancestor",
        "unsafe_namespace",
        "namespace_changed",
        "unexpected_auxiliary",
        "lock_binding",
        "store_exists",
        "open_failed",
        "closed_store",
        "readonly_store",
        "invalid_guard",
        "lock_collision",
        "lock_failed",
        "ledger_io_failed",
        "lock_cleanup_failed",
        "close_failed",
        "operation_missing",
        "invalid_transition",
        "proof_mismatch",
        "reservation_conflict",
        "sqlite_image_unavailable",
        "ledger_failure",
    )
)


class LineageError(SessionMigrateError):
    """Fixed, content-safe error code; operating-system details stay private."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _ERROR_CODES else "ledger_failure"
        super().__init__(f"Lineage ledger: {self.code}.")


class Coverage(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class NativeState(StrEnum):
    NATIVE_NOT_STARTED = "native_not_started"
    ATTEMPTED_UNKNOWN = "attempted_unknown"
    ACKNOWLEDGED = "acknowledged"


class OperationState(StrEnum):
    READY = "ready"
    PUBLISHING = "publishing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    NEEDS_RECONCILE = "needs_reconcile"
    CANCELLED = "cancelled"
    FAILED = "failed"


def _require(condition: bool, code: str = "invalid_metadata") -> None:
    if not condition:
        raise LineageError(code)


def _hash(value: object, optional: bool = False) -> None:
    _require((optional and value is None) or (type(value) is str and bool(_HASH.fullmatch(value))))


def _identifier(value: object) -> None:
    _require(type(value) is str and bool(_ID.fullmatch(value)))


def _path(value: object) -> None:
    _require(type(value) is str)
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError:
        raise LineageError("invalid_metadata") from None
    _require(
        value.startswith("/")
        and value != "/"
        and posixpath.normpath(value) == value
        and not value.startswith("//")
        and encoded_length <= 4096
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _count(value: object, maximum: int = 2**31 - 1) -> None:
    _require(type(value) is int and 0 <= value <= maximum)


def _hashes(value: object) -> None:
    _require(type(value) is tuple and len(value) <= MAX_UNITS)
    for item in value:
        _hash(item)


def _references(value: object) -> None:
    _require(type(value) is tuple and len(value) <= MAX_REFERENCES)
    _require(all(type(item) is EvidenceReference for item in value))


@dataclass(frozen=True, slots=True, repr=False)
class RootWitness:
    device: int
    inode: int

    def __post_init__(self) -> None:
        _count(self.device, 2**63 - 1)
        _count(self.inode, 2**63 - 1)
        _require(self.inode > 0)


@dataclass(frozen=True, slots=True, repr=False)
class NativeNamespace:
    format: str
    canonical_root: str
    native_id: str

    def __post_init__(self) -> None:
        _require(type(self.format) is str and self.format in {item.value for item in AgentFormat})
        _path(self.canonical_root)
        _identifier(self.native_id)

    @property
    def lock_key(self) -> str:
        return hashlib.sha256(b"session-migrate/namespace/v1\0" + _encode(self)).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class QualifiedIdentity:
    namespace: NativeNamespace
    profile_fingerprint: str
    root_witness: RootWitness

    def __post_init__(self) -> None:
        _require(type(self.namespace) is NativeNamespace and type(self.root_witness) is RootWitness)
        _hash(self.profile_fingerprint)


@dataclass(frozen=True, slots=True, repr=False)
class EvidenceReference:
    kind: str
    stage: str
    availability: str
    path: str | None
    sha256: str | None

    def __post_init__(self) -> None:
        _require(self.kind in ("artifact", "manifest", "native", "receipt"))
        _require(self.stage in ("planned", "observed", "verified"))
        _require(self.availability in ("available", "missing", "unknown"))
        if self.path is not None:
            _path(self.path)
        _hash(self.sha256, optional=True)
        _require(
            self.stage != "verified"
            or (self.availability == "available" and self.sha256 is not None)
        )


@dataclass(frozen=True, slots=True, repr=False)
class PolicyVersions:
    canonical_algorithm: str
    projection_policy: str
    source_adapter: str
    target_adapter: str
    source_client: str | None
    target_client: str | None

    def __post_init__(self) -> None:
        for value in (
            self.canonical_algorithm,
            self.projection_policy,
            self.source_adapter,
            self.target_adapter,
        ):
            _require(type(value) is str and bool(_VERSION.fullmatch(value)))
        for value in (self.source_client, self.target_client):
            _require(value is None or (type(value) is str and bool(_VERSION.fullmatch(value))))


@dataclass(frozen=True, slots=True, repr=False)
class DerivedInputIdentityV1:
    kind: str
    document_sha256: str
    event_sha256: str

    def __post_init__(self):
        _require(type(self.kind) is str and self.kind == "user_handoff")
        _hash(self.document_sha256)
        _hash(self.event_sha256)


@dataclass(frozen=True, slots=True, repr=False)
class SelectionRangeV1:
    first_event: int
    last_event: int
    block_index: int | None

    def __post_init__(self):
        _count(self.first_event)
        _count(self.last_event)
        _require(self.first_event <= self.last_event)
        if self.block_index is not None:
            _count(self.block_index)


@dataclass(frozen=True, slots=True, repr=False)
class OmissionV1:
    kind: str
    count: int

    def __post_init__(self):
        _require(type(self.kind) is str and self.kind in {kind.value for kind in EventKind})
        _count(self.count)


@dataclass(frozen=True, slots=True, repr=False)
class TransformationV1:
    schema_version: int
    kind: str
    policy: str
    input: DerivedInputIdentityV1
    selection: tuple[SelectionRangeV1, ...]
    omissions: tuple[OmissionV1, ...]
    provenance: str

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(type(self.kind) is str and self.kind == "user_handoff")
        _require(type(self.policy) is str and self.policy == "user-handoff-v1")
        _require(type(self.input) is DerivedInputIdentityV1)
        self.input.__post_init__()
        _require(type(self.selection) is tuple and len(self.selection) <= MAX_UNITS)
        for item in self.selection:
            _require(type(item) is SelectionRangeV1)
            item.__post_init__()
        _require(type(self.omissions) is tuple and len(self.omissions) <= len(EventKind))
        for item in self.omissions:
            _require(type(item) is OmissionV1)
            item.__post_init__()
        _require(len({item.kind for item in self.omissions}) == len(self.omissions))
        _require(
            type(self.provenance) is str
            and self.provenance
            in (
                "user_supplied",
                "fake_model_reviewed",
            )
        )


@dataclass(frozen=True, slots=True, repr=False)
class ReturnLinkV1:
    schema_version: int
    parent_operation_id: str
    parent_receipt_sha256: str
    parent_target_identity: QualifiedIdentity

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _identifier(self.parent_operation_id)
        _hash(self.parent_receipt_sha256)
        _require(type(self.parent_target_identity) is QualifiedIdentity)
        self.parent_target_identity.__post_init__()


@dataclass(frozen=True, slots=True, repr=False)
class DerivationBindingV1:
    schema_version: int
    source_identity: QualifiedIdentity
    input: DerivedInputIdentityV1
    transformation_sha256: str

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(type(self.source_identity) is QualifiedIdentity)
        self.source_identity.__post_init__()
        _require(type(self.input) is DerivedInputIdentityV1)
        self.input.__post_init__()
        _hash(self.transformation_sha256)


@dataclass(frozen=True, slots=True, repr=False)
class DerivedVerificationV1:
    schema_version: int
    transformation_sha256: str
    input_event_sha256: str
    expected_target_snapshot_sha256: str
    observed_target_authority_sha256: str
    policy: str

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        for value in (
            self.transformation_sha256,
            self.input_event_sha256,
            self.expected_target_snapshot_sha256,
            self.observed_target_authority_sha256,
        ):
            _hash(value)
        _require(type(self.policy) is str and self.policy == "user-handoff-v1")


@dataclass(frozen=True, slots=True, repr=False)
class SourceMetadata:
    """Prepared source authority; identity remains absent for portable inputs."""

    identity: QualifiedIdentity | None
    artifact_sha256: str
    witness_sha256: str
    snapshot_sha256: str | None
    prefix_witness: PrefixWitness | None = None

    def __post_init__(self) -> None:
        _require(self.identity is None or type(self.identity) is QualifiedIdentity)
        _hash(self.artifact_sha256)
        _hash(self.witness_sha256)
        _hash(self.snapshot_sha256, optional=True)
        _require(self.prefix_witness is None or type(self.prefix_witness) is PrefixWitness)
        _require(self.prefix_witness is None or self.snapshot_sha256 is not None)


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionExpectation:
    snapshot_sha256: str | None
    unit_hashes: tuple[str, ...]
    coverage: Coverage
    prefix_witness: PrefixWitness | None = None
    relation_sha256: str | None = None

    def __post_init__(self) -> None:
        _hash(self.snapshot_sha256, optional=True)
        _hashes(self.unit_hashes)
        _require(type(self.coverage) is Coverage)
        _require(self.prefix_witness is None or type(self.prefix_witness) is PrefixWitness)
        _hash(self.relation_sha256, optional=True)
        _require(self.prefix_witness is None or not self.unit_hashes)
        _require(
            self.coverage is not Coverage.UNKNOWN
            or (
                self.snapshot_sha256 is None
                and not self.unit_hashes
                and self.prefix_witness is None
                and self.relation_sha256 is None
            )
        )
        _require(self.coverage is Coverage.UNKNOWN or self.snapshot_sha256 is not None)


@dataclass(frozen=True, slots=True, repr=False)
class OperationRecord:
    """Ready metadata; target_input_sha256 hashes the prepared native artifact."""

    operation_id: str
    source: SourceMetadata
    target_namespace: NativeNamespace
    target_profile_fingerprint: str
    options_sha256: str
    target_input_sha256: str
    versions: PolicyVersions
    expected_projection: ProjectionExpectation
    references: tuple[EvidenceReference, ...]
    transformation: TransformationV1 | None = None
    return_link: ReturnLinkV1 | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id)
        _require(
            type(self.source) is SourceMetadata and type(self.target_namespace) is NativeNamespace
        )
        _require(
            type(self.versions) is PolicyVersions
            and type(self.expected_projection) is ProjectionExpectation
        )
        _hash(self.target_profile_fingerprint)
        _hash(self.options_sha256)
        _hash(self.target_input_sha256)
        _references(self.references)
        _require(self.transformation is None or type(self.transformation) is TransformationV1)
        if self.transformation is not None:
            self.transformation.__post_init__()
            _require(self.source.identity is not None)
        _require(self.return_link is None or type(self.return_link) is ReturnLinkV1)
        if self.return_link is not None:
            self.return_link.__post_init__()
            _require(
                self.return_link.parent_operation_id != self.operation_id
                and self.return_link.parent_target_identity == self.source.identity
            )
        _encode(self)


@dataclass(frozen=True, slots=True, repr=False)
class CheckpointRecord:
    """Independent observation; artifact_sha256 hashes that side's export."""

    identity: QualifiedIdentity | None
    artifact_sha256: str
    snapshot_sha256: str | None
    unit_hashes: tuple[str, ...]
    turn_count: int
    tool_link_hashes: tuple[str, ...]
    boundary_closed: bool
    coverage: Coverage
    branch_sha256: str | None
    versions: PolicyVersions
    prefix_witness: PrefixWitness | None = None

    def __post_init__(self) -> None:
        _require(self.identity is None or type(self.identity) is QualifiedIdentity)
        _hash(self.artifact_sha256)
        _hash(self.snapshot_sha256, optional=True)
        _hash(self.branch_sha256, optional=True)
        _hashes(self.unit_hashes)
        _hashes(self.tool_link_hashes)
        _count(self.turn_count)
        _require(type(self.boundary_closed) is bool and type(self.coverage) is Coverage)
        _require(type(self.versions) is PolicyVersions)
        _require(self.prefix_witness is None or type(self.prefix_witness) is PrefixWitness)
        if self.prefix_witness is not None:
            prefix = self.prefix_witness
            _require(not self.unit_hashes and not self.tool_link_hashes)
            _require(prefix.closed_turn_count <= self.turn_count)
            _require(
                self.boundary_closed
                is (
                    prefix.boundary.kind is not BoundaryKind.UNPROVEN
                    and prefix.boundary.unit_offset == prefix.units.count
                    and prefix.boundary.pending_call_count == 0
                )
            )
        _require(
            self.coverage is not Coverage.UNKNOWN
            or (
                self.snapshot_sha256 is None
                and not self.unit_hashes
                and not self.tool_link_hashes
                and not self.boundary_closed
                and self.prefix_witness is None
            )
        )
        _require(self.coverage is Coverage.UNKNOWN or self.snapshot_sha256 is not None)
        _encode(self)


@dataclass(frozen=True, slots=True, repr=False)
class CompletionRecord:
    """Bind installed input and separate observations supplied by the caller."""

    target_identity: QualifiedIdentity
    input_artifact_sha256: str
    source_checkpoint: CheckpointRecord | None
    target_checkpoint: CheckpointRecord | None
    references: tuple[EvidenceReference, ...]
    derived_verification: DerivedVerificationV1 | None = None

    def __post_init__(self) -> None:
        _require(type(self.target_identity) is QualifiedIdentity)
        _hash(self.input_artifact_sha256)
        _require(self.source_checkpoint is None or type(self.source_checkpoint) is CheckpointRecord)
        _require(self.target_checkpoint is None or type(self.target_checkpoint) is CheckpointRecord)
        _references(self.references)
        _require(
            self.derived_verification is None
            or type(self.derived_verification) is DerivedVerificationV1
        )
        if self.derived_verification is not None:
            self.derived_verification.__post_init__()
        _encode(self, 2 * MAX_METADATA_BYTES + MAX_RECEIPT_BYTES)


@dataclass(frozen=True, slots=True, repr=False)
class CompletionReceipt:
    schema_version: int
    operation_id: str
    target_identity: QualifiedIdentity
    input_artifact_sha256: str
    source_checkpoint_sha256: str | None
    target_checkpoint_sha256: str | None
    continuity_reason: str
    references: tuple[EvidenceReference, ...]
    derivation: DerivationBindingV1 | None = None
    return_link: ReturnLinkV1 | None = None

    def __post_init__(self) -> None:
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _identifier(self.operation_id)
        _require(type(self.target_identity) is QualifiedIdentity)
        _hash(self.input_artifact_sha256)
        _hash(self.source_checkpoint_sha256, optional=True)
        _hash(self.target_checkpoint_sha256, optional=True)
        _require(
            self.continuity_reason
            in (
                "tracked",
                "identity_untracked",
                "coverage_unverified",
                "derived_handoff",
            )
        )
        _require(self.derivation is None or type(self.derivation) is DerivationBindingV1)
        _require((self.continuity_reason == "derived_handoff") == (self.derivation is not None))
        if self.derivation is not None:
            self.derivation.__post_init__()
            _require(
                self.source_checkpoint_sha256 is not None
                and self.target_checkpoint_sha256 is not None
            )
        _require(self.return_link is None or type(self.return_link) is ReturnLinkV1)
        if self.return_link is not None:
            self.return_link.__post_init__()
            _require(self.return_link.parent_operation_id != self.operation_id)
            if self.derivation is not None:
                _require(self.return_link.parent_target_identity == self.derivation.source_identity)
        _references(self.references)
        _encode(self, MAX_RECEIPT_BYTES)


@dataclass(frozen=True, slots=True, repr=False)
class ReconcileReceipt:
    schema_version: int
    operation_id: str
    target_id: str
    phase: str
    reason: str
    native_state: NativeState
    references: tuple[EvidenceReference, ...]
    ledger_recorded: bool

    def __post_init__(self) -> None:
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _identifier(self.operation_id)
        _identifier(self.target_id)
        _require(
            self.phase in ("recording_intent", "installing", "observing", "finalizing", "recovery")
        )
        _require(
            self.reason
            in (
                "intent_persistence_failed",
                "guards_changed",
                "native_result_unknown",
                "observation_failed",
                "finalization_failed",
                "interrupted",
                "publication_unconfirmed",
                "ledger_unavailable",
            )
        )
        _require(type(self.native_state) is NativeState and type(self.ledger_recorded) is bool)
        _references(self.references)
        _encode(self, MAX_RECEIPT_BYTES)


@dataclass(frozen=True, slots=True, repr=False)
class EdgeRecord:
    operation_id: str
    source_identity: QualifiedIdentity
    target_identity: QualifiedIdentity
    source_checkpoint_sha256: str
    target_checkpoint_sha256: str
    derivation: DerivationBindingV1 | None = None
    return_link: ReturnLinkV1 | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id)
        _require(
            type(self.source_identity) is QualifiedIdentity
            and type(self.target_identity) is QualifiedIdentity
        )
        _hash(self.source_checkpoint_sha256)
        _hash(self.target_checkpoint_sha256)
        _require(self.derivation is None or type(self.derivation) is DerivationBindingV1)
        if self.derivation is not None:
            self.derivation.__post_init__()
            _require(self.derivation.source_identity == self.source_identity)
        _require(self.return_link is None or type(self.return_link) is ReturnLinkV1)
        if self.return_link is not None:
            self.return_link.__post_init__()
            _require(
                self.return_link.parent_operation_id != self.operation_id
                and self.return_link.parent_target_identity == self.source_identity
            )

    @property
    def kind(self) -> str:
        return "DERIVED" if self.derivation is not None else "FULL"


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryOperation:
    record: OperationRecord
    state: OperationState
    durable_state: OperationState
    native_state: NativeState
    durable_native_state: NativeState
    reserved_bytes: int
    receipt: CompletionReceipt | ReconcileReceipt | None
    source_checkpoint: CheckpointRecord | None
    target_checkpoint: CheckpointRecord | None


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryView:
    store_uuid: str
    operations: tuple[RecoveryOperation, ...]
    edges: tuple[EdgeRecord, ...]


_RECORD_TYPES = (
    DerivedInputIdentityV1,
    SelectionRangeV1,
    OmissionV1,
    TransformationV1,
    ReturnLinkV1,
    DerivationBindingV1,
    DerivedVerificationV1,
    RootWitness,
    NativeNamespace,
    QualifiedIdentity,
    EvidenceReference,
    PolicyVersions,
    SourceMetadata,
    ProjectionExpectation,
    OperationRecord,
    CheckpointRecord,
    CompletionRecord,
    CompletionReceipt,
    ReconcileReceipt,
    EdgeRecord,
    SequenceWitness,
    BoundaryWitness,
    PrefixWitness,
)
_OPTIONAL_RECORD_FIELDS = {
    OperationRecord: ("transformation", "return_link"),
    CompletionRecord: ("derived_verification",),
    CompletionReceipt: ("derivation", "return_link"),
    EdgeRecord: ("derivation", "return_link"),
    SourceMetadata: ("prefix_witness",),
    ProjectionExpectation: ("prefix_witness", "relation_sha256"),
    CheckpointRecord: ("prefix_witness",),
}


def _json_value(value: object, depth: int = 0) -> object:
    _require(depth <= 12)
    if type(value) in _RECORD_TYPES:
        extensions = _OPTIONAL_RECORD_FIELDS.get(type(value), ())
        version_two = any(getattr(value, name) is not None for name in extensions)
        result = {
            field.name: _json_value(getattr(value, field.name), depth + 1)
            for field in dataclasses.fields(value)
            if field.name not in extensions or version_two
        }
        if version_two:
            result["record_schema_version"] = 2
        return result
    if type(value) in (Coverage, NativeState, OperationState, BoundaryKind):
        return value.value
    if type(value) is tuple:
        return [_json_value(item, depth + 1) for item in value]
    _require(value is None or type(value) in (str, int, bool))
    return value


def _encode(value: object, maximum: int = MAX_METADATA_BYTES) -> bytes:
    try:
        result = json.dumps(
            _json_value(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        _require(len(result) <= maximum, "metadata_limit")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise LineageError("invalid_metadata") from None


def _typed(kind: object, value: object, depth: int = 0) -> object:
    _require(depth <= 12)
    origin = get_origin(kind)
    if origin in (types.UnionType, Union):
        if value is None and type(None) in get_args(kind):
            return None
        for member in get_args(kind):
            if member is not type(None):
                try:
                    return _typed(member, value, depth + 1)
                except LineageError:
                    pass
        raise LineageError("invalid_metadata")
    if origin is tuple:
        _require(type(value) is list and len(value) <= MAX_UNITS)
        return tuple(_typed(get_args(kind)[0], item, depth + 1) for item in value)
    if kind in _RECORD_TYPES:
        _require(type(value) is dict)
        fields = {field.name for field in dataclasses.fields(kind)}
        extensions = _OPTIONAL_RECORD_FIELDS.get(kind, ())
        if extensions and "record_schema_version" in value:
            _require(
                type(value["record_schema_version"]) is int
                and value["record_schema_version"] == 2
                and set(value) == fields | {"record_schema_version"}
                and any(value[name] is not None for name in extensions)
            )
            value = {name: item for name, item in value.items() if name != "record_schema_version"}
        else:
            _require(set(value) == fields - set(extensions))
        hints = get_type_hints(kind)
        return kind(**{name: _typed(hints[name], item, depth + 1) for name, item in value.items()})
    if kind in (Coverage, NativeState, OperationState, BoundaryKind):
        _require(type(value) is str)
        try:
            return kind(value)
        except ValueError:
            raise LineageError("invalid_metadata") from None
    _require(type(value) is kind)
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    _require(len(pairs) == len({key for key, _ in pairs}))
    return dict(pairs)


def _decode(kind: object, payload: object, maximum: int = MAX_METADATA_BYTES):
    try:
        _require(type(payload) is bytes and 0 < len(payload) <= maximum, "metadata_limit")
        value = json.loads(
            payload, object_pairs_hook=_pairs, parse_constant=lambda _: _require(False)
        )
        result = _typed(kind, value)
        _require(_encode(result, maximum) == payload, "noncanonical_metadata")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, WitnessValidationError):
        raise LineageError("invalid_metadata") from None


def _validated(kind: object, value: object, maximum: int = MAX_METADATA_BYTES):
    _require(type(value) is kind)
    return _decode(kind, _encode(value, maximum), maximum)


def _stored_derived_verification(record, target):
    """Reconstruct durable metadata for image validation, never runtime proof."""
    if record.transformation is None:
        return None
    _require(type(target) is CheckpointRecord, "proof_mismatch")
    return DerivedVerificationV1(
        1,
        hashlib.sha256(_encode(record.transformation)).hexdigest(),
        record.transformation.input.event_sha256,
        target.snapshot_sha256,
        target.artifact_sha256,
        record.transformation.policy,
    )


_SCHEMA = (
    "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "schema_version INTEGER NOT NULL CHECK(schema_version=1), store_uuid TEXT NOT NULL, "
    "main_lock_device INTEGER NOT NULL, main_lock_inode INTEGER NOT NULL)",
    "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, target_key TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('ready','publishing','verifying','completed',"
    "'needs_reconcile','cancelled','failed')), native_state TEXT NOT NULL "
    "CHECK(native_state IN ('native_not_started','attempted_unknown','acknowledged')), "
    "reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes>=0), record_json BLOB NOT NULL, "
    "reconcile_json BLOB)",
    "CREATE UNIQUE INDEX operations_live_target ON operations(target_key) "
    "WHERE state NOT IN ('cancelled','failed')",
    "CREATE TABLE checkpoints (operation_id TEXT NOT NULL REFERENCES operations(operation_id), "
    "side TEXT NOT NULL CHECK(side IN ('source','target')), record_json BLOB NOT NULL, "
    "PRIMARY KEY(operation_id,side))",
    "CREATE TABLE edges (operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id), "
    "record_json BLOB NOT NULL)",
    "CREATE TABLE receipts (operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id), "
    "record_json BLOB NOT NULL)",
)
_EXPECTED_SCHEMA: tuple | None = None


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(
        connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name")
    )


def _expected_schema() -> tuple:
    global _EXPECTED_SCHEMA
    if _EXPECTED_SCHEMA is None:
        connection = sqlite3.connect(":memory:")
        try:
            for statement in _SCHEMA:
                connection.execute(statement)
            _EXPECTED_SCHEMA = _schema_rows(connection)
        finally:
            connection.close()
    return _EXPECTED_SCHEMA


def _authorizer(action, argument1, argument2, database, trigger):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (argument2 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _memory(image: bytes | None = None) -> sqlite3.Connection:
    _require(
        hasattr(sqlite3.Connection, "serialize") and hasattr(sqlite3.Connection, "deserialize"),
        "sqlite_image_unavailable",
    )
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA foreign_keys=ON")
        # An operations row can contain both its ready metadata and a receipt.
        connection.setlimit(
            sqlite3.SQLITE_LIMIT_LENGTH, MAX_METADATA_BYTES + MAX_RECEIPT_BYTES + 4096
        )
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 16384)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 32)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 32)
        if image is not None:
            connection.deserialize(image)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA journal_mode=MEMORY")
            connection.execute("PRAGMA foreign_keys=ON")
        connection.set_authorizer(_authorizer)
        return connection
    except (sqlite3.Error, OSError, AttributeError):
        connection.close()
        raise LineageError("invalid_image") from None


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _close_descriptors(descriptors) -> bool:
    failed = False
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            failed = True
    return failed


def _file(info: os.stat_result) -> None:
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600,
        "unsafe_file",
    )


def _directory(info: os.stat_result, private: bool = False) -> None:
    _require(stat.S_ISDIR(info.st_mode), "unsafe_directory")
    if private:
        _require(
            info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_directory"
        )
    else:
        _require(info.st_uid in (0, os.getuid()), "unsafe_ancestor")
        _require(not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX), "unsafe_ancestor")


class _PinnedNamespace:
    def __init__(self, chain: list[tuple[int, str | None, tuple[int, int]]]) -> None:
        self.chain = chain
        self.root = chain[-1][0]

    @classmethod
    def open(cls, path: Path, *, create: bool = False, missing_ok: bool = False):
        _path(str(path))
        chain = []
        try:
            descriptor = os.open("/", _DIR_FLAGS)
            info = os.fstat(descriptor)
            _directory(info)
            chain.append((descriptor, None, _identity(info)))
            parts = path.parts[1:]
            for index, name in enumerate(parts):
                parent = chain[-1][0]
                last = index == len(parts) - 1
                if last and create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                        os.fsync(parent)
                    except FileExistsError:
                        pass
                try:
                    descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    if missing_ok:
                        for opened, _, _ in reversed(chain):
                            os.close(opened)
                        return None
                    raise
                info = os.fstat(descriptor)
                chain.append((descriptor, name, _identity(info)))
                _directory(info, private=last)
            result = cls(chain)
            result.check()
            return result
        except (OSError, LineageError):
            for opened, _, _ in reversed(chain):
                os.close(opened)
            raise LineageError("unsafe_namespace") from None

    def check(self) -> None:
        try:
            for index, (descriptor, name, witness) in enumerate(self.chain):
                info = os.fstat(descriptor)
                _directory(info, private=index == len(self.chain) - 1)
                _require(_identity(info) == witness, "namespace_changed")
                if name is not None:
                    named = os.stat(name, dir_fd=self.chain[index - 1][0], follow_symlinks=False)
                    _require(
                        _identity(named) == witness and stat.S_ISDIR(named.st_mode),
                        "namespace_changed",
                    )
        except OSError:
            raise LineageError("namespace_changed") from None

    def close(self) -> None:
        failed = _close_descriptors(descriptor for descriptor, _, _ in reversed(self.chain))
        self.chain.clear()
        _require(not failed, "close_failed")


class OperationGuard:
    """Private live handle for one store, operation, and target namespace."""

    def __init__(self, store, operation_id, namespace, locks, namespaces=None):
        self._store = store
        self._operation_id = operation_id
        self._namespace = namespace
        self._store_uuid = store._store_uuid
        self._locks = locks
        self._namespaces = (namespace,) if namespaces is None else namespaces
        self._active = True
        self._ready_claim = None
        self._publication_claim = False

    def check(self) -> None:
        _require(self._active and not self._store._closed, "invalid_guard")
        _require(self._store_uuid == self._store._store_uuid, "invalid_guard")
        self._store._check()
        for name, descriptor, witness in self._locks:
            self._store._check_file(name, descriptor, witness, self._store._locks_fd)


class LineageStore:
    """Explicit installation/opening; no repair, migrations, or native I/O."""

    def __init__(self, namespace, lock_fd, locks_fd, *, readonly, max_image_bytes):
        self._namespace = namespace
        self._root_fd = namespace.root
        self._main_fd = lock_fd
        self._main_witness = _identity(os.fstat(lock_fd))
        self._locks_fd = locks_fd
        self._locks_witness = _identity(os.fstat(locks_fd))
        self._readonly = readonly
        self._max_image_bytes = max_image_bytes
        self._mutex = threading.RLock()
        self._closed = False
        self._fault_hook = None
        self._store_uuid = ""
        self._installed = False
        self._guarded_persist_renamed = False

    @classmethod
    def _open(
        cls,
        path,
        *,
        create,
        readonly=False,
        missing_ok=False,
        max_image_bytes=MAX_IMAGE_BYTES,
        creation_context=None,
        creation_guard=None,
    ):
        _count(max_image_bytes, MAX_IMAGE_BYTES)
        _require(max_image_bytes >= 32768, "image_budget")
        namespace = _PinnedNamespace.open(Path(path), create=create, missing_ok=missing_ok)
        if namespace is None:
            return None
        lock_fd = locks_fd = None
        store = None
        try:
            if create:
                _require(not os.listdir(namespace.root), "store_exists")
                lock_fd = os.open(
                    "main.lock",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                    0o600,
                    dir_fd=namespace.root,
                )
                os.mkdir("locks", 0o700, dir_fd=namespace.root)
            else:
                lock_fd = os.open(
                    "main.lock",
                    (os.O_RDONLY if readonly else os.O_RDWR) | _FILE_FLAGS,
                    dir_fd=namespace.root,
                )
            _file(os.fstat(lock_fd))
            locks_fd = os.open("locks", _DIR_FLAGS, dir_fd=namespace.root)
            _directory(os.fstat(locks_fd), private=True)
            store = cls(
                namespace, lock_fd, locks_fd, readonly=readonly, max_image_bytes=max_image_bytes
            )
            with store._locked(exclusive=create):
                if create:
                    store._store_uuid = uuid.uuid4().hex
                    os.write(lock_fd, (store._store_uuid + "\n").encode("ascii"))
                    os.fsync(lock_fd)
                    connection = store._memory_image()
                    try:
                        store._create_database(connection, creation_context)
                        if creation_guard is None:
                            store._persist(connection.serialize(), None)
                        else:
                            store._persist(
                                connection.serialize(), None, external_guard=creation_guard
                            )
                    finally:
                        connection.close()
                else:
                    store._installed = True
                    connection, _ = store._load()
                    connection.close()
            return store
        except (OSError, sqlite3.Error, ValueError, LineageError) as error:
            uncertain_creation = (
                create
                and creation_guard is not None
                and store is not None
                and (store._installed or store._guarded_persist_renamed)
            )
            try:
                if store is not None:
                    store.close()
                else:
                    if locks_fd is not None:
                        os.close(locks_fd)
                    if lock_fd is not None:
                        os.close(lock_fd)
                    namespace.close()
            finally:
                if uncertain_creation:
                    raise LineageError("persistence_uncertain") from None
            if isinstance(error, LineageError):
                raise error from None
            raise LineageError("open_failed") from None

    _memory_image = staticmethod(_memory)

    def _create_database(self, connection, creation_context=None):
        _require(creation_context is None)
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(
            "INSERT INTO metadata VALUES (1,1,?,?,?)",
            (self._store_uuid, *self._main_witness),
        )

    @classmethod
    def create_new(cls, path, *, max_image_bytes=MAX_IMAGE_BYTES):
        return cls._open(path, create=True, max_image_bytes=max_image_bytes)

    @classmethod
    def open_existing(cls, path, *, readonly=False, max_image_bytes=MAX_IMAGE_BYTES):
        return cls._open(path, create=False, readonly=readonly, max_image_bytes=max_image_bytes)

    @classmethod
    def inspect_existing(cls, path, *, max_image_bytes=MAX_IMAGE_BYTES):
        store = cls._open(
            path, create=False, readonly=True, missing_ok=True, max_image_bytes=max_image_bytes
        )
        if store is None:
            return None
        with store:
            return store.recovery_view()

    def __enter__(self):
        _require(not self._closed, "closed_store")
        return self

    def __exit__(self, *arguments):
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            descriptors = [self._main_fd, self._locks_fd]
            descriptors.extend(descriptor for descriptor, _, _ in reversed(self._namespace.chain))
            self._namespace.chain.clear()
            _require(not _close_descriptors(descriptors), "close_failed")

    def _check_file(self, name, descriptor, witness, parent=None):
        try:
            parent = self._root_fd if parent is None else parent
            opened = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _file(opened)
            _file(named)
            _require(_identity(opened) == witness == _identity(named), "namespace_changed")
        except OSError:
            raise LineageError("namespace_changed") from None

    def _check(self) -> None:
        _require(not self._closed, "closed_store")
        self._namespace.check()
        self._check_file("main.lock", self._main_fd, self._main_witness)
        try:
            locks = os.stat("locks", dir_fd=self._root_fd, follow_symlinks=False)
            _directory(locks, private=True)
            _directory(os.fstat(self._locks_fd), private=True)
            _require(
                _identity(locks) == self._locks_witness == _identity(os.fstat(self._locks_fd)),
                "namespace_changed",
            )
            for name in os.listdir(self._root_fd):
                if name in ("main.lock", "ledger.sqlite3", "locks"):
                    continue
                _require(bool(_TEMP_NAME.fullmatch(name)), "unexpected_auxiliary")
                _file(os.stat(name, dir_fd=self._root_fd, follow_symlinks=False))
            if self._installed:
                _file(os.stat("ledger.sqlite3", dir_fd=self._root_fd, follow_symlinks=False))
            for name in os.listdir(self._locks_fd):
                _require(bool(_LOCK_NAME.fullmatch(name)), "unexpected_auxiliary")
                _file(os.stat(name, dir_fd=self._locks_fd, follow_symlinks=False))
        except OSError:
            raise LineageError("namespace_changed") from None

    @contextmanager
    def _locked(self, *, exclusive):
        with self._mutex:
            self._check()
            try:
                fcntl.flock(self._main_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                self._check()
                yield
                self._check()
            except (OSError, sqlite3.Error):
                raise LineageError("ledger_io_failed") from None
            finally:
                try:
                    fcntl.flock(self._main_fd, fcntl.LOCK_UN)
                except OSError:
                    self.close()
                    raise LineageError("lock_cleanup_failed") from None

    @contextmanager
    def lock_operation(
        self,
        operation_id: str,
        target_namespace: NativeNamespace,
        additional_namespaces: tuple[NativeNamespace, ...] = (),
    ):
        _require(not self._readonly, "readonly_store")
        _identifier(operation_id)
        namespace = _validated(NativeNamespace, target_namespace)
        _require(type(additional_namespaces) is tuple)
        namespaces = (namespace,) + tuple(
            _validated(NativeNamespace, item) for item in additional_namespaces
        )
        operation_key = hashlib.sha256(
            b"session-migrate/operation/v1\0" + operation_id.encode("ascii")
        ).hexdigest()
        namespace_keys = {item.lock_key for item in namespaces}
        _require(operation_key not in namespace_keys, "lock_collision")
        names = sorted(key + ".lock" for key in namespace_keys | {operation_key})
        locks = []
        owned = []
        guard = None
        try:
            for name in names:
                self._check()
                created = False
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                        0o600,
                        dir_fd=self._locks_fd,
                    )
                    created = True
                except FileExistsError:
                    descriptor = os.open(name, os.O_RDWR | _FILE_FLAGS, dir_fd=self._locks_fd)
                owned.append(descriptor)
                info = os.fstat(descriptor)
                locks.append((name, descriptor, _identity(info)))
                self._check_file(name, descriptor, _identity(info), self._locks_fd)
                if created:
                    os.fsync(descriptor)
                    os.fsync(self._locks_fd)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._check_file(name, descriptor, _identity(info), self._locks_fd)
            guard = OperationGuard(self, operation_id, namespace, locks, namespaces)
            guard.check()
            yield guard
        except OSError:
            raise LineageError("lock_failed") from None
        finally:
            if guard is not None:
                guard._active = False
            cleanup_failed = False
            for descriptor in reversed(owned):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    cleanup_failed = True
            cleanup_failed = _close_descriptors(reversed(owned)) or cleanup_failed
            if cleanup_failed and sys.exc_info()[0] is None:
                raise LineageError("lock_cleanup_failed") from None

    def _guard(self, operation_id, guard, record=None):
        _require(
            type(guard) is OperationGuard
            and guard._store is self
            and guard._operation_id == operation_id,
            "invalid_guard",
        )
        guard.check()
        if record is not None:
            _require(record.target_namespace == guard._namespace, "invalid_guard")

    def _read_image(self):
        descriptor = None
        try:
            descriptor = os.open("ledger.sqlite3", os.O_RDONLY | _FILE_FLAGS, dir_fd=self._root_fd)
            before = os.fstat(descriptor)
            _file(before)
            _require(0 < before.st_size <= self._max_image_bytes, "image_limit")
            witness = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            self._check_file("ledger.sqlite3", descriptor, _identity(before))
            parts = []
            remaining = before.st_size
            while remaining:
                part = os.read(descriptor, min(65536, remaining))
                _require(bool(part), "image_changed")
                parts.append(part)
                remaining -= len(part)
            _require(not os.read(descriptor, 1), "image_changed")
            after = os.fstat(descriptor)
            _require(
                witness
                == (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ),
                "image_changed",
            )
            self._check_file("ledger.sqlite3", descriptor, _identity(before))
            self._check()
            return b"".join(parts), witness
        except OSError:
            raise LineageError("image_read_failed") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _load(self):
        image, witness = self._read_image()
        connection = self._memory_image(image)
        try:
            self._validate_database(connection, len(image))
            return connection, witness
        except LineageError:
            connection.close()
            raise
        except (sqlite3.Error, ValueError, TypeError, OverflowError):
            connection.close()
            raise LineageError("invalid_image") from None

    def _validate_image_header(self, connection, *, application_id, schema_version, schema):
        """Shared SQLite safety checks; each store supplies its exact own schema."""
        _require(connection.execute("PRAGMA page_size").fetchone() == (4096,), "unknown_schema")
        _require(
            connection.execute("PRAGMA user_version").fetchone() == (schema_version,),
            "unknown_schema",
        )
        _require(
            connection.execute("PRAGMA application_id").fetchone() == (application_id,),
            "unknown_schema",
        )
        _require(_schema_rows(connection) == schema, "unknown_schema")
        _require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "invalid_image")
        _require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "invalid_image")

    def _validate_database(self, connection, image_size):
        self._validate_image_header(
            connection,
            application_id=APPLICATION_ID,
            schema_version=SCHEMA_VERSION,
            schema=_expected_schema(),
        )
        metadata = connection.execute("SELECT * FROM metadata").fetchall()
        _require(len(metadata) == 1 and metadata[0][:2] == (1, 1), "unknown_schema")
        _, _, store_uuid, device, inode = metadata[0]
        self._bind_store_identity(store_uuid, device, inode)
        operations = connection.execute(
            "SELECT operation_id,target_key,state,native_state,reserved_bytes,"
            "record_json,reconcile_json FROM operations ORDER BY operation_id"
        ).fetchall()
        _require(len(operations) <= MAX_OPERATIONS, "record_limit")
        budget = 0
        for (
            operation_id,
            target_key,
            state,
            native_state,
            reserved,
            payload,
            reconcile,
        ) in operations:
            record = _decode(OperationRecord, payload)
            _require(
                operation_id == record.operation_id
                and target_key == record.target_namespace.lock_key,
                "invalid_image",
            )
            state = OperationState(state)
            native_state = NativeState(native_state)
            _count(reserved, INITIAL_CLOSE_BUDGET)
            pending = state in (
                OperationState.READY,
                OperationState.PUBLISHING,
                OperationState.VERIFYING,
                OperationState.NEEDS_RECONCILE,
            )
            _require(reserved >= MIN_CLOSE_BUDGET if pending else reserved == 0, "invalid_budget")
            budget += reserved
            if state in (OperationState.READY, OperationState.CANCELLED, OperationState.FAILED):
                _require(
                    native_state is NativeState.NATIVE_NOT_STARTED and reconcile is None,
                    "invalid_image",
                )
            if state in (OperationState.VERIFYING, OperationState.COMPLETED):
                _require(native_state is NativeState.ACKNOWLEDGED, "invalid_image")
            if reconcile is not None:
                receipt = _decode(ReconcileReceipt, reconcile, MAX_RECEIPT_BYTES)
                _require(
                    state is OperationState.NEEDS_RECONCILE
                    and receipt.operation_id == operation_id
                    and receipt.target_id == record.target_namespace.native_id
                    and receipt.native_state is native_state
                    and receipt.ledger_recorded,
                    "invalid_image",
                )
            _require(
                state is not OperationState.NEEDS_RECONCILE or reconcile is not None,
                "invalid_image",
            )
            checkpoints = {}
            for side, checkpoint in connection.execute(
                "SELECT side,record_json FROM checkpoints WHERE operation_id=?", (operation_id,)
            ):
                checkpoints[side] = _decode(CheckpointRecord, checkpoint)
            receipts = connection.execute(
                "SELECT record_json FROM receipts WHERE operation_id=?", (operation_id,)
            ).fetchall()
            edges = connection.execute(
                "SELECT record_json FROM edges WHERE operation_id=?", (operation_id,)
            ).fetchall()
            if state is OperationState.COMPLETED:
                _require(len(receipts) == 1, "invalid_image")
                receipt = _decode(CompletionReceipt, receipts[0][0], MAX_RECEIPT_BYTES)
                done = CompletionRecord(
                    receipt.target_identity,
                    receipt.input_artifact_sha256,
                    checkpoints.get("source"),
                    checkpoints.get("target"),
                    receipt.references,
                    _stored_derived_verification(record, checkpoints.get("target")),
                )
                expected, edge = self._completion(record, done)
                _require(receipt == expected and len(edges) == (1 if edge else 0), "invalid_image")
                if edge:
                    _require(_decode(EdgeRecord, edges[0][0]) == edge, "invalid_image")
            else:
                _require(not checkpoints and not receipts and not edges, "invalid_image")
        _require(image_size + budget <= self._max_image_bytes, "image_budget")
        self._check()

    def _bind_store_identity(self, store_uuid, device, inode):
        """Common immutable FD/UUID binding for schema-specific memory images."""
        _require(type(store_uuid) is str and bool(_UUID.fullmatch(store_uuid)), "lock_binding")
        _require((device, inode) == self._main_witness, "lock_binding")
        _require(
            os.pread(self._main_fd, 34, 0) == (store_uuid + "\n").encode("ascii")
            and os.fstat(self._main_fd).st_size == 33,
            "lock_binding",
        )
        _require(not self._store_uuid or self._store_uuid == store_uuid, "lock_binding")
        self._store_uuid = store_uuid

    def _event(self, event):
        if self._fault_hook is not None:
            self._fault_hook(event)

    def _image_witness(self, expected):
        if expected is None:
            try:
                os.stat("ledger.sqlite3", dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise LineageError("store_exists")
        try:
            actual = os.stat("ledger.sqlite3", dir_fd=self._root_fd, follow_symlinks=False)
            _file(actual)
            _require(
                expected
                == (
                    actual.st_dev,
                    actual.st_ino,
                    actual.st_size,
                    actual.st_mtime_ns,
                    actual.st_ctime_ns,
                ),
                "image_changed",
            )
        except OSError:
            raise LineageError("image_changed") from None

    def _persist(self, image, expected, *, external_guard=None):
        _require(len(image) <= self._max_image_bytes, "image_budget")
        _require(external_guard is None or callable(external_guard), "invalid_guard")
        if external_guard is not None:
            # Cleanup may itself fail after a caller closes a pinned FD. Retain
            # the actual rename outcome independently of any replaced error.
            self._guarded_persist_renamed = False
        name = ".ledger-" + uuid.uuid4().hex + ".tmp"
        descriptor = None
        renamed = False

        def check_external():
            if external_guard is not None:
                external_guard()

        try:
            self._check()
            self._image_witness(expected)
            check_external()
            self._event("before_temp_write")
            check_external()
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                0o600,
                dir_fd=self._root_fd,
            )
            witness = _identity(os.fstat(descriptor))
            self._check_file(name, descriptor, witness)
            check_external()
            offset = 0
            while offset < len(image):
                written = os.write(descriptor, image[offset : offset + 65536])
                _require(written > 0, "image_write_failed")
                offset += written
                check_external()
            self._event("after_temp_write")
            self._check()
            self._check_file(name, descriptor, witness)
            check_external()
            os.fsync(descriptor)
            self._event("after_temp_fsync")
            self._check()
            self._check_file(name, descriptor, witness)
            self._image_witness(expected)
            check_external()
            self._event("before_rename")
            self._check()
            self._check_file(name, descriptor, witness)
            self._image_witness(expected)
            check_external()
            os.replace(name, "ledger.sqlite3", src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            renamed = True
            if external_guard is not None:
                self._guarded_persist_renamed = True
            check_external()
            self._event("after_rename")
            self._check()
            self._check_file("ledger.sqlite3", descriptor, witness)
            self._installed = True
            check_external()
            os.fsync(self._root_fd)
            check_external()
            self._event("after_directory_fsync")
            self._check()
            self._check_file("ledger.sqlite3", descriptor, witness)
            check_external()
        except LineageError:
            if external_guard is not None and renamed:
                raise LineageError("persistence_uncertain") from None
            raise
        except (OSError, sqlite3.Error):
            raise LineageError(
                "persistence_uncertain" if renamed else "image_write_failed"
            ) from None
        finally:
            if descriptor is not None:
                if not renamed:
                    try:
                        self._check_file(name, descriptor, _identity(os.fstat(descriptor)))
                        os.unlink(name, dir_fd=self._root_fd)
                        os.fsync(self._root_fd)
                    except (OSError, LineageError):
                        pass
                try:
                    os.close(descriptor)
                except OSError:
                    if external_guard is None:
                        raise
                    raise LineageError(
                        "persistence_uncertain" if renamed else "image_write_failed"
                    ) from None

    def _row(self, connection, operation_id):
        row = connection.execute(
            "SELECT state,native_state,reserved_bytes,record_json "
            "FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        _require(row is not None, "operation_missing")
        return OperationState(row[0]), NativeState(row[1]), row[2], _decode(OperationRecord, row[3])

    def _mutate(
        self,
        operation_id,
        guard,
        mutate,
        *,
        inserting=False,
        finalizing=False,
        skip_unchanged=False,
        external_guard=None,
    ):
        _require(not self._readonly, "readonly_store")
        _require(external_guard is None or callable(external_guard), "invalid_guard")
        self._guard(operation_id, guard)
        persisted = False

        def check_external():
            self._guard(operation_id, guard)
            self._bind_store_identity(guard._store_uuid, *self._main_witness)
            external_guard()
            self._guard(operation_id, guard)
            self._bind_store_identity(guard._store_uuid, *self._main_witness)

        with self._locked(exclusive=True):
            connection, witness = self._load()
            try:
                if not inserting:
                    self._guard(operation_id, guard, self._row(connection, operation_id)[3])
                connection.execute("BEGIN IMMEDIATE")
                changes = connection.total_changes
                result = mutate(connection)
                connection.execute("COMMIT")
                if skip_unchanged and connection.total_changes == changes:
                    self._guard(operation_id, guard)
                    if external_guard is not None:
                        check_external()
                    return result
                image = connection.serialize()
                if not inserting and not finalizing:
                    _, _, reserved, _ = self._row(connection, operation_id)
                    growth = max(0, len(image) - witness[2])
                    _require(reserved - growth >= MIN_CLOSE_BUDGET, "image_budget")
                    connection.execute(
                        "UPDATE operations SET reserved_bytes=? WHERE operation_id=?",
                        (reserved - growth, operation_id),
                    )
                    image = connection.serialize()
                self._validate_database(connection, len(image))
                self._guard(operation_id, guard)
                if external_guard is None:
                    self._persist(image, witness)
                else:
                    self._persist(image, witness, external_guard=check_external)
                persisted = True
                self._guard(operation_id, guard)
                if external_guard is not None:
                    check_external()
                return result
            except LineageError:
                if persisted and external_guard is not None:
                    raise LineageError("persistence_uncertain") from None
                raise
            except sqlite3.IntegrityError:
                raise LineageError("reservation_conflict") from None
            except sqlite3.Error:
                raise LineageError("invalid_image") from None
            finally:
                connection.close()

    def ready(self, record: OperationRecord, guard: OperationGuard) -> None:
        record = _validated(OperationRecord, record)
        self.preflight_metadata(record)
        self._guard(record.operation_id, guard, record)

        def insert(connection):
            _require(
                connection.execute("SELECT count(*) FROM operations").fetchone()[0]
                < MAX_OPERATIONS,
                "record_limit",
            )
            connection.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,NULL)",
                (
                    record.operation_id,
                    record.target_namespace.lock_key,
                    OperationState.READY.value,
                    NativeState.NATIVE_NOT_STARTED.value,
                    INITIAL_CLOSE_BUDGET,
                    _encode(record),
                ),
            )

        self._mutate(record.operation_id, guard, insert, inserting=True)
        guard._ready_claim = hashlib.sha256(_encode(record)).hexdigest()

    @staticmethod
    def preflight_metadata(record: OperationRecord) -> None:
        """Check known receipt bounds before intent; no native or ledger I/O."""
        record = _validated(OperationRecord, record)
        # Observed witnesses may use the full signed 64-bit integer range.
        future = QualifiedIdentity(
            record.target_namespace,
            record.target_profile_fingerprint,
            RootWitness(2**63 - 1, 2**63 - 1),
        )
        _encode(
            CompletionReceipt(
                1,
                record.operation_id,
                future,
                record.target_input_sha256,
                "f" * 64,
                "f" * 64,
                "coverage_unverified",
                record.references,
            ),
            MAX_RECEIPT_BYTES,
        )
        _encode(
            ReconcileReceipt(
                1,
                record.operation_id,
                record.target_namespace.native_id,
                "recording_intent",
                "intent_persistence_failed",
                NativeState.ACKNOWLEDGED,
                record.references,
                True,
            ),
            MAX_RECEIPT_BYTES,
        )

    def check_guard(
        self,
        operation_id: str,
        guard: OperationGuard,
        *,
        expected_state: OperationState | None = None,
    ) -> None:
        """Re-read durable readiness under SH for the caller's post-I/O guards."""
        self._guard(operation_id, guard)
        _require(expected_state is None or type(expected_state) is OperationState)
        if expected_state is OperationState.PUBLISHING:
            _require(guard._publication_claim, "invalid_guard")
        with self._locked(exclusive=False):
            connection, _ = self._load()
            try:
                state, _, _, record = self._row(connection, operation_id)
                self._guard(operation_id, guard, record)
                _require(expected_state is None or state is expected_state, "invalid_transition")
            finally:
                connection.close()

    def begin_publication(self, operation_id: str, guard: OperationGuard) -> None:
        """Durable intent only. The caller closes its short token gate first."""
        self._guard(operation_id, guard)
        claim = guard._ready_claim
        _require(claim is not None, "invalid_transition")
        # A recovered READY row is not a publication capability. Consuming the
        # live claim also prevents retries when this persistence result is lost.
        guard._ready_claim = None

        def transition(connection):
            state, _, _, record = self._row(connection, operation_id)
            _require(state is OperationState.READY, "invalid_transition")
            _require(hashlib.sha256(_encode(record)).hexdigest() == claim, "invalid_guard")
            connection.execute(
                "UPDATE operations SET state='publishing' WHERE operation_id=? AND state='ready'",
                (operation_id,),
            )

        self._mutate(operation_id, guard, transition)
        guard._publication_claim = True

    def mark_attempted(self, operation_id: str, guard: OperationGuard) -> None:
        """Record conservative uncertainty; repeat caller guards after this I/O.

        This optional step cannot prove an invocation occurred. Recovery also
        treats an unacknowledged publishing image conservatively without it.
        """
        self._guard(operation_id, guard)
        _require(guard._publication_claim, "invalid_guard")

        def transition(connection):
            state, native, _, _ = self._row(connection, operation_id)
            _require(
                state is OperationState.PUBLISHING and native is NativeState.NATIVE_NOT_STARTED,
                "invalid_transition",
            )
            connection.execute(
                "UPDATE operations SET native_state='attempted_unknown' WHERE operation_id=?",
                (operation_id,),
            )

        self._mutate(operation_id, guard, transition)

    def mark_verifying(self, operation_id: str, guard: OperationGuard) -> None:
        """Caller attests acknowledgement; this store does not prove native I/O."""

        def transition(connection):
            state, _, _, _ = self._row(connection, operation_id)
            _require(
                state in (OperationState.PUBLISHING, OperationState.NEEDS_RECONCILE),
                "invalid_transition",
            )
            connection.execute(
                "UPDATE operations SET state='verifying',native_state='acknowledged',"
                "reconcile_json=NULL WHERE operation_id=?",
                (operation_id,),
            )

        self._mutate(operation_id, guard, transition)

    def needs_reconcile(self, receipt: ReconcileReceipt, guard: OperationGuard) -> ReconcileReceipt:
        receipt = _validated(ReconcileReceipt, receipt, MAX_RECEIPT_BYTES)
        saved = dataclasses.replace(receipt, ledger_recorded=True)

        def transition(connection):
            state, native, _, record = self._row(connection, receipt.operation_id)
            _require(
                state
                in (
                    OperationState.READY,
                    OperationState.PUBLISHING,
                    OperationState.VERIFYING,
                    OperationState.NEEDS_RECONCILE,
                ),
                "invalid_transition",
            )
            _require(receipt.target_id == record.target_namespace.native_id, "proof_mismatch")
            ranks = list(NativeState)
            _require(ranks.index(receipt.native_state) >= ranks.index(native), "invalid_transition")
            connection.execute(
                "UPDATE operations SET state='needs_reconcile',native_state=?,"
                "reconcile_json=? WHERE operation_id=?",
                (saved.native_state.value, _encode(saved, MAX_RECEIPT_BYTES), saved.operation_id),
            )
            return saved

        return self._mutate(receipt.operation_id, guard, transition)

    def cancel_ready(self, operation_id: str, guard: OperationGuard) -> None:
        self._finish_unpublished(operation_id, guard, OperationState.CANCELLED)

    def fail_ready(self, operation_id: str, guard: OperationGuard) -> None:
        self._finish_unpublished(operation_id, guard, OperationState.FAILED)

    def _finish_unpublished(self, operation_id, guard, target_state):
        def transition(connection):
            state, native, _, _ = self._row(connection, operation_id)
            _require(
                state is OperationState.READY and native is NativeState.NATIVE_NOT_STARTED,
                "invalid_transition",
            )
            connection.execute(
                "UPDATE operations SET state=?,reserved_bytes=0 "
                "WHERE operation_id=? AND state='ready'",
                (target_state.value, operation_id),
            )

        self._mutate(operation_id, guard, transition, finalizing=True)

    def _completion(self, record, done):
        _require(done.input_artifact_sha256 == record.target_input_sha256, "proof_mismatch")
        _require(
            done.target_identity.namespace == record.target_namespace
            and done.target_identity.profile_fingerprint == record.target_profile_fingerprint,
            "proof_mismatch",
        )
        source, target = done.source_checkpoint, done.target_checkpoint
        if source is not None:
            _require(
                source.identity == record.source.identity
                and source.artifact_sha256 == record.source.artifact_sha256
                and source.snapshot_sha256 == record.source.snapshot_sha256
                and source.prefix_witness == record.source.prefix_witness
                and source.versions == record.versions,
                "proof_mismatch",
            )
        if target is not None:
            _require(
                target.identity == done.target_identity
                and target.versions == record.versions
                and target.snapshot_sha256 == record.expected_projection.snapshot_sha256
                and target.unit_hashes == record.expected_projection.unit_hashes
                and target.prefix_witness == record.expected_projection.prefix_witness
                and target.coverage is record.expected_projection.coverage,
                "proof_mismatch",
            )
        source_hash = hashlib.sha256(_encode(source)).hexdigest() if source is not None else None
        target_hash = hashlib.sha256(_encode(target)).hexdigest() if target is not None else None
        reason = "identity_untracked" if record.source.identity is None else "coverage_unverified"
        edge = None
        derivation = None
        if record.transformation is not None:
            proof = done.derived_verification
            _require(
                type(proof) is DerivedVerificationV1
                and source is not None
                and source.identity is not None
                and target is not None
                and target.identity is not None,
                "proof_mismatch",
            )
            proof.__post_init__()
            transformation_sha = hashlib.sha256(_encode(record.transformation)).hexdigest()
            _require(
                proof.transformation_sha256 == transformation_sha
                and proof.input_event_sha256 == record.transformation.input.event_sha256
                and proof.policy == record.transformation.policy
                and proof.expected_target_snapshot_sha256
                == record.expected_projection.snapshot_sha256
                and proof.expected_target_snapshot_sha256 == target.snapshot_sha256
                and proof.observed_target_authority_sha256 == target.artifact_sha256,
                "proof_mismatch",
            )
            derivation = DerivationBindingV1(
                1, source.identity, record.transformation.input, transformation_sha
            )
            reason = "derived_handoff"
            edge = EdgeRecord(
                record.operation_id,
                source.identity,
                done.target_identity,
                source_hash,
                target_hash,
                derivation,
                record.return_link,
            )
        else:
            _require(done.derived_verification is None, "proof_mismatch")
        if record.transformation is None and (
            record.source.identity is not None
            and source is not None
            and target is not None
            and source.coverage is Coverage.VERIFIED
            and target.coverage is Coverage.VERIFIED
        ):
            reason = "tracked"
            edge = EdgeRecord(
                record.operation_id,
                record.source.identity,
                done.target_identity,
                source_hash,
                target_hash,
                None,
                record.return_link,
            )
        receipt = CompletionReceipt(
            1,
            record.operation_id,
            done.target_identity,
            done.input_artifact_sha256,
            source_hash,
            target_hash,
            reason,
            done.references,
            derivation,
            record.return_link,
        )
        _encode(receipt, MAX_RECEIPT_BYTES)
        return receipt, edge

    def complete(
        self, operation_id: str, completion: CompletionRecord, guard: OperationGuard
    ) -> CompletionReceipt:
        done = _validated(CompletionRecord, completion, 2 * MAX_METADATA_BYTES + MAX_RECEIPT_BYTES)
        for checkpoint in (done.source_checkpoint, done.target_checkpoint):
            if checkpoint is not None:
                _encode(checkpoint)

        def transition(connection):
            state, native, _, record = self._row(connection, operation_id)
            receipt, edge = self._completion(record, done)
            if state is OperationState.COMPLETED:
                existing = _decode(
                    CompletionReceipt,
                    connection.execute(
                        "SELECT record_json FROM receipts WHERE operation_id=?", (operation_id,)
                    ).fetchone()[0],
                    MAX_RECEIPT_BYTES,
                )
                _require(existing == receipt, "proof_mismatch")
                return existing
            _require(
                state is OperationState.VERIFYING and native is NativeState.ACKNOWLEDGED,
                "invalid_transition",
            )
            for side, checkpoint in (
                ("source", done.source_checkpoint),
                ("target", done.target_checkpoint),
            ):
                if checkpoint is not None:
                    connection.execute(
                        "INSERT INTO checkpoints VALUES (?,?,?)",
                        (operation_id, side, _encode(checkpoint)),
                    )
            if edge is not None:
                connection.execute("INSERT INTO edges VALUES (?,?)", (operation_id, _encode(edge)))
            connection.execute(
                "INSERT INTO receipts VALUES (?,?)",
                (operation_id, _encode(receipt, MAX_RECEIPT_BYTES)),
            )
            connection.execute(
                "UPDATE operations SET state='completed',native_state='acknowledged',"
                "reserved_bytes=0,reconcile_json=NULL WHERE operation_id=? AND state='verifying'",
                (operation_id,),
            )
            return receipt

        return self._mutate(operation_id, guard, transition, finalizing=True)

    def complete_reconciled(
        self,
        operation_id: str,
        expected_record_sha: str,
        expected_state: OperationState,
        completion: CompletionRecord,
        guard: OperationGuard,
        *,
        external_guard=None,
    ) -> CompletionReceipt:
        """Finalize an already acknowledged row with ledger-only compare-and-swap.

        The caller owns reconstructed native evidence and local finalization.
        This transition never manufactures acknowledgement or publication rights.
        An identical completed image is the sole state-CAS retry exception.
        """
        _hash(expected_record_sha)
        _require(
            type(expected_state) is OperationState
            and expected_state
            in (
                OperationState.VERIFYING,
                OperationState.NEEDS_RECONCILE,
                OperationState.COMPLETED,
            )
        )
        done = _validated(CompletionRecord, completion, 2 * MAX_METADATA_BYTES + MAX_RECEIPT_BYTES)
        _require(done.target_checkpoint is not None)
        for checkpoint in (done.source_checkpoint, done.target_checkpoint):
            if checkpoint is not None:
                _encode(checkpoint)

        def transition(connection):
            state, native, _, record = self._row(connection, operation_id)
            _require(
                hashlib.sha256(_encode(record)).hexdigest() == expected_record_sha,
                "proof_mismatch",
            )
            _require(native is NativeState.ACKNOWLEDGED, "invalid_transition")
            _require(record.source.identity is None or done.source_checkpoint is not None)
            receipt, edge = self._completion(record, done)
            if state is OperationState.COMPLETED:
                existing = _decode(
                    CompletionReceipt,
                    connection.execute(
                        "SELECT record_json FROM receipts WHERE operation_id=?", (operation_id,)
                    ).fetchone()[0],
                    MAX_RECEIPT_BYTES,
                )
                checkpoints = {
                    side: _decode(CheckpointRecord, payload)
                    for side, payload in connection.execute(
                        "SELECT side,record_json FROM checkpoints WHERE operation_id=?",
                        (operation_id,),
                    )
                }
                stored = CompletionRecord(
                    existing.target_identity,
                    existing.input_artifact_sha256,
                    checkpoints.get("source"),
                    checkpoints.get("target"),
                    existing.references,
                    _stored_derived_verification(record, checkpoints.get("target")),
                )
                _require(stored == done and existing == receipt, "proof_mismatch")
                return existing
            _require(
                state is expected_state
                and state in (OperationState.VERIFYING, OperationState.NEEDS_RECONCILE),
                "invalid_transition",
            )
            for side, checkpoint in (
                ("source", done.source_checkpoint),
                ("target", done.target_checkpoint),
            ):
                if checkpoint is not None:
                    connection.execute(
                        "INSERT INTO checkpoints VALUES (?,?,?)",
                        (operation_id, side, _encode(checkpoint)),
                    )
            if edge is not None:
                connection.execute("INSERT INTO edges VALUES (?,?)", (operation_id, _encode(edge)))
            connection.execute(
                "INSERT INTO receipts VALUES (?,?)",
                (operation_id, _encode(receipt, MAX_RECEIPT_BYTES)),
            )
            changed = connection.execute(
                "UPDATE operations SET state='completed',native_state='acknowledged',"
                "reserved_bytes=0,reconcile_json=NULL WHERE operation_id=? AND state=? "
                "AND native_state='acknowledged'",
                (operation_id, expected_state.value),
            ).rowcount
            _require(changed == 1, "invalid_transition")
            return receipt

        return self._mutate(
            operation_id,
            guard,
            transition,
            finalizing=True,
            skip_unchanged=True,
            external_guard=external_guard,
        )

    def recovery_view(self) -> RecoveryView:
        """Historical data only; publishing/verifying need conservative recovery."""
        with self._locked(exclusive=False):
            connection, _ = self._load()
            try:
                operations = []
                for operation_id, state, native, reserved, payload, reconcile in connection.execute(
                    "SELECT operation_id,state,native_state,reserved_bytes,record_json,"
                    "reconcile_json FROM operations ORDER BY operation_id"
                ):
                    record = _decode(OperationRecord, payload)
                    durable = OperationState(state)
                    effective = (
                        OperationState.NEEDS_RECONCILE
                        if durable in (OperationState.PUBLISHING, OperationState.VERIFYING)
                        else durable
                    )
                    durable_native = NativeState(native)
                    # A publishing image predates the native call. Its last
                    # recorded not-started value cannot prove that the call did
                    # not happen since then, nor that another worker is dead.
                    native = (
                        NativeState.ATTEMPTED_UNKNOWN
                        if durable is OperationState.PUBLISHING
                        and durable_native is NativeState.NATIVE_NOT_STARTED
                        else durable_native
                    )
                    receipt = (
                        _decode(ReconcileReceipt, reconcile, MAX_RECEIPT_BYTES)
                        if reconcile is not None
                        else None
                    )
                    if effective is OperationState.NEEDS_RECONCILE and receipt is None:
                        receipt = ReconcileReceipt(
                            1,
                            operation_id,
                            record.target_namespace.native_id,
                            "recovery",
                            "publication_unconfirmed",
                            native,
                            record.references,
                            False,
                        )
                    if durable is OperationState.COMPLETED:
                        receipt = _decode(
                            CompletionReceipt,
                            connection.execute(
                                "SELECT record_json FROM receipts WHERE operation_id=?",
                                (operation_id,),
                            ).fetchone()[0],
                            MAX_RECEIPT_BYTES,
                        )
                    checkpoints = {
                        side: _decode(CheckpointRecord, item)
                        for side, item in connection.execute(
                            "SELECT side,record_json FROM checkpoints WHERE operation_id=?",
                            (operation_id,),
                        )
                    }
                    operations.append(
                        RecoveryOperation(
                            record,
                            effective,
                            durable,
                            native,
                            durable_native,
                            reserved,
                            receipt,
                            checkpoints.get("source"),
                            checkpoints.get("target"),
                        )
                    )
                edges = tuple(
                    _decode(EdgeRecord, row[0])
                    for row in connection.execute(
                        "SELECT record_json FROM edges ORDER BY operation_id"
                    )
                )
                return RecoveryView(self._store_uuid, tuple(operations), edges)
            finally:
                connection.close()
