"""Content-free adopted-now anchors, separate from the historical lineage ledger.

These DTOs validate metadata, never native comparison authority. The adoption
service must supply its independently audited evidence under coordinator locks.
SQLite sees only RAM images; LineageStore supplies the existing pinned POSIX I/O.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from session_migrate.lineage import (
    _UUID,
    MAX_IMAGE_BYTES,
    MAX_RECEIPT_BYTES,
    CheckpointRecord,
    Coverage,
    EvidenceReference,
    LineageError,
    LineageStore,
    OperationGuard,
    PolicyVersions,
    _decode,
    _encode,
    _hash,
    _identifier,
    _json_value,
    _memory,
    _pairs,
    _references,
    _require,
    _schema_rows,
    _typed,
)
from session_migrate.witnesses import BoundaryKind, WitnessValidationError

BASELINE_APPLICATION_ID = 0x534D4231
BASELINE_SCHEMA_VERSION = 1
MAX_BASELINE_RECORD_BYTES = 384 * 1024
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SCHEMA = (
    "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "schema_version INTEGER NOT NULL CHECK(schema_version=1), store_uuid TEXT NOT NULL, "
    "main_lock_device INTEGER NOT NULL, main_lock_inode INTEGER NOT NULL, "
    "coordinator_store_uuid TEXT NOT NULL)",
    "CREATE TABLE baselines (id TEXT PRIMARY KEY, pair_key TEXT NOT NULL, "
    "record_json BLOB NOT NULL, record_sha TEXT NOT NULL)",
)
_EXPECTED_SCHEMA: tuple | None = None


def _uuid(value):
    _require(type(value) is str and bool(_UUID.fullmatch(value)))


def _timestamp(value):
    _require(type(value) is str and bool(_UTC_TIMESTAMP.fullmatch(value)))
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise LineageError("invalid_metadata") from None


@dataclass(frozen=True, slots=True, repr=False)
class BaselineRecordV1:
    """An observed current pair; it does not represent a past import or closure."""

    schema_version: int
    kind: str
    provenance: str
    baseline_id: str
    created_at_utc: str
    coordinator_store_uuid: str
    source_profile_binding_kind: str
    target_profile_binding_kind: str
    source_checkpoint: CheckpointRecord
    target_checkpoint: CheckpointRecord
    versions: PolicyVersions
    canonical_policy_sha: str
    projection_policy_sha: str
    relation_sha: str
    comparison_sha: str
    references: tuple[EvidenceReference, ...]

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(type(self.kind) is str and self.kind == "baseline")
        _require(type(self.provenance) is str and self.provenance == "adopted_now")
        _identifier(self.baseline_id)
        _timestamp(self.created_at_utc)
        _uuid(self.coordinator_store_uuid)
        for kind in (self.source_profile_binding_kind, self.target_profile_binding_kind):
            _require(type(kind) is str and kind in ("profile_v1", "legacy_runtime_v1"))
        _require(type(self.versions) is PolicyVersions)
        for checkpoint in (self.source_checkpoint, self.target_checkpoint):
            _require(type(checkpoint) is CheckpointRecord)
            checkpoint = _decode(CheckpointRecord, _encode(checkpoint))
            _require(
                checkpoint.identity is not None
                and checkpoint.coverage is Coverage.VERIFIED
                and checkpoint.versions == self.versions
                and not checkpoint.boundary_closed
                and (
                    checkpoint.prefix_witness is None
                    or checkpoint.prefix_witness.boundary.kind is BoundaryKind.UNPROVEN
                )
            )
        for value in (
            self.canonical_policy_sha,
            self.projection_policy_sha,
            self.relation_sha,
            self.comparison_sha,
        ):
            _hash(value)
        _references(self.references)
        _encode_baseline(self)
        _receipt(self)

    @property
    def pair_key(self) -> str:
        source, target = self.source_checkpoint.identity, self.target_checkpoint.identity
        # Root witnesses deliberately remain in checkpoints, outside the pair index.
        payload = _encode(
            (
                source.namespace,
                source.profile_fingerprint,
                self.source_profile_binding_kind,
                target.namespace,
                target.profile_fingerprint,
                self.target_profile_binding_kind,
            )
        )
        return hashlib.sha256(b"session-migrate/baseline-pair/v1\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class BaselineReceipt:
    schema_version: int
    kind: str
    provenance: str
    baseline_id: str
    created_at_utc: str
    coordinator_store_uuid: str
    record_sha: str
    pair_key: str
    references: tuple[EvidenceReference, ...]

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == 1)
        _require(type(self.kind) is str and self.kind == "baseline")
        _require(type(self.provenance) is str and self.provenance == "adopted_now")
        _identifier(self.baseline_id)
        _timestamp(self.created_at_utc)
        _uuid(self.coordinator_store_uuid)
        _hash(self.record_sha)
        _hash(self.pair_key)
        _references(self.references)
        _encode_baseline(self, MAX_RECEIPT_BYTES)


@dataclass(frozen=True, slots=True, repr=False)
class BaselineView:
    store_uuid: str
    coordinator_store_uuid: str
    baselines: tuple[BaselineRecordV1, ...]


_TYPES = (BaselineRecordV1, BaselineReceipt)


def _encode_baseline(value, maximum=MAX_BASELINE_RECORD_BYTES):
    _require(type(value) in _TYPES)
    try:
        payload = json.dumps(
            {
                field.name: _json_value(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        _require(0 < len(payload) <= maximum, "metadata_limit")
        return payload
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise LineageError("invalid_metadata") from None


def _decode_baseline(kind, payload, maximum=MAX_BASELINE_RECORD_BYTES):
    _require(kind in _TYPES)
    try:
        _require(type(payload) is bytes and 0 < len(payload) <= maximum, "metadata_limit")
        value = json.loads(
            payload, object_pairs_hook=_pairs, parse_constant=lambda _: _require(False)
        )
        hints = {
            "schema_version": int,
            "kind": str,
            "provenance": str,
            "baseline_id": str,
            "created_at_utc": str,
            "coordinator_store_uuid": str,
            "source_profile_binding_kind": str,
            "target_profile_binding_kind": str,
            "source_checkpoint": CheckpointRecord,
            "target_checkpoint": CheckpointRecord,
            "versions": PolicyVersions,
            "canonical_policy_sha": str,
            "projection_policy_sha": str,
            "relation_sha": str,
            "comparison_sha": str,
            "references": tuple[EvidenceReference, ...],
            "record_sha": str,
            "pair_key": str,
        }
        fields = {field.name for field in dataclasses.fields(kind)}
        _require(type(value) is dict and set(value) == fields)
        result = kind(**{name: _typed(hints[name], item) for name, item in value.items()})
        _require(_encode_baseline(result, maximum) == payload, "noncanonical_metadata")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError, WitnessValidationError):
        raise LineageError("invalid_metadata") from None


def _receipt(record):
    return BaselineReceipt(
        1,
        "baseline",
        "adopted_now",
        record.baseline_id,
        record.created_at_utc,
        record.coordinator_store_uuid,
        hashlib.sha256(_encode_baseline(record)).hexdigest(),
        record.pair_key,
        record.references,
    )


def _expected_schema():
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


def _baseline_memory(image=None):
    connection = _memory(image)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_BASELINE_RECORD_BYTES + 4096)
    return connection


def _coordinator_guard(coordinator, guard):
    _require(type(coordinator) is LineageStore and type(guard) is OperationGuard, "invalid_guard")
    coordinator._guard(guard._operation_id, guard)
    coordinator._bind_store_identity(guard._store_uuid, *coordinator._main_witness)


def open_baseline_coordinator_for_commit(
    path, *, max_image_bytes=MAX_IMAGE_BYTES, external_guard=None
):
    """Explicit commit bootstrap only; never repairs or creates historical rows."""
    _require(external_guard is None or callable(external_guard), "invalid_guard")
    if external_guard is not None:
        external_guard()
    coordinator = LineageStore._open(
        path,
        create=False,
        missing_ok=True,
        max_image_bytes=max_image_bytes,
    )
    if coordinator is not None:
        if external_guard is not None:
            try:
                external_guard()
            except BaseException:
                coordinator.close()
                raise
        return coordinator
    if external_guard is not None:
        return LineageStore._open(
            path,
            create=True,
            max_image_bytes=max_image_bytes,
            creation_guard=external_guard,
        )
    return LineageStore.create_new(path, max_image_bytes=max_image_bytes)


class BaselineStore(LineageStore):
    """Append-only schema, sharing A's exact namespace/locking/atomic I/O guards."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._coordinator_store_uuid = ""

    _memory_image = staticmethod(_baseline_memory)

    def _create_database(self, connection, creation_context=None):
        _uuid(creation_context)
        self._coordinator_store_uuid = creation_context
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id={BASELINE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={BASELINE_SCHEMA_VERSION}")
        connection.execute(
            "INSERT INTO metadata VALUES (1,1,?,?,?,?)",
            (self._store_uuid, *self._main_witness, creation_context),
        )

    @classmethod
    def _open_coordinated(
        cls, path, *, create, coordinator, guard, max_image_bytes, external_guard=None
    ):
        store = None

        def check_external():
            _coordinator_guard(coordinator, guard)
            if external_guard is not None:
                external_guard()
                _coordinator_guard(coordinator, guard)

        try:
            _require(external_guard is None or callable(external_guard), "invalid_guard")
            check_external()
            with coordinator._locked(exclusive=False):
                connection, _ = coordinator._load()
                connection.close()
                check_external()
                store = cls._open(
                    path,
                    create=create,
                    max_image_bytes=max_image_bytes,
                    creation_context=coordinator._store_uuid if create else None,
                    creation_guard=check_external if create else None,
                )
                _require(
                    store._coordinator_store_uuid == coordinator._store_uuid
                    and store._main_witness != coordinator._main_witness,
                    "lock_binding",
                )
                check_external()
            return store
        except BaseException:
            uncertain = (
                create
                and store is not None
                and (store._installed or store._guarded_persist_renamed)
            )
            try:
                if store is not None:
                    store.close()
            finally:
                if uncertain:
                    raise LineageError("persistence_uncertain") from None
            raise

    @classmethod
    def create_new(
        cls, path, *, coordinator, guard, max_image_bytes=MAX_IMAGE_BYTES, external_guard=None
    ):
        return cls._open_coordinated(
            path,
            create=True,
            coordinator=coordinator,
            guard=guard,
            max_image_bytes=max_image_bytes,
            external_guard=external_guard,
        )

    @classmethod
    def open_existing(
        cls, path, *, coordinator, guard, max_image_bytes=MAX_IMAGE_BYTES, external_guard=None
    ):
        return cls._open_coordinated(
            path,
            create=False,
            coordinator=coordinator,
            guard=guard,
            max_image_bytes=max_image_bytes,
            external_guard=external_guard,
        )

    @classmethod
    def inspect_existing(
        cls,
        path,
        *,
        coordinator_store_uuid=None,
        max_image_bytes=MAX_IMAGE_BYTES,
    ):
        if coordinator_store_uuid is not None:
            _uuid(coordinator_store_uuid)
        store = cls._open(
            path,
            create=False,
            readonly=True,
            missing_ok=True,
            max_image_bytes=max_image_bytes,
        )
        if store is None:
            return None
        with store:
            view = store.baseline_view()
            _require(
                coordinator_store_uuid is None
                or view.coordinator_store_uuid == coordinator_store_uuid,
                "lock_binding",
            )
            return view

    def _validate_database(self, connection, image_size):
        self._validate_image_header(
            connection,
            application_id=BASELINE_APPLICATION_ID,
            schema_version=BASELINE_SCHEMA_VERSION,
            schema=_expected_schema(),
        )
        metadata = connection.execute("SELECT * FROM metadata").fetchall()
        _require(len(metadata) == 1 and metadata[0][:2] == (1, 1), "unknown_schema")
        _, _, store_uuid, device, inode, coordinator_uuid = metadata[0]
        self._bind_store_identity(store_uuid, device, inode)
        _uuid(coordinator_uuid)
        _require(
            coordinator_uuid != store_uuid
            and (
                not self._coordinator_store_uuid or self._coordinator_store_uuid == coordinator_uuid
            ),
            "lock_binding",
        )
        self._coordinator_store_uuid = coordinator_uuid
        for baseline_id, pair_key, payload, record_sha in connection.execute(
            "SELECT id,pair_key,record_json,record_sha FROM baselines ORDER BY id"
        ):
            record = _decode_baseline(BaselineRecordV1, payload)
            _require(
                record.baseline_id == baseline_id
                and record.coordinator_store_uuid == coordinator_uuid
                and record.pair_key == pair_key
                and hashlib.sha256(payload).hexdigest() == record_sha,
                "invalid_image",
            )
            _receipt(record)
        _require(image_size <= self._max_image_bytes, "image_budget")
        self._check()

    def _append_guard(self, record, coordinator, guard):
        _coordinator_guard(coordinator, guard)
        _require(
            record.baseline_id == guard._operation_id
            and record.coordinator_store_uuid == coordinator._store_uuid
            and self._coordinator_store_uuid == coordinator._store_uuid,
            "invalid_guard",
        )
        source, target = record.source_checkpoint.identity, record.target_checkpoint.identity
        _require(target.namespace == guard._namespace, "invalid_guard")
        required = {source.namespace.lock_key + ".lock", target.namespace.lock_key + ".lock"}
        _require(required <= {name for name, _, _ in guard._locks}, "invalid_guard")
        _require(
            source.namespace in guard._namespaces and target.namespace in guard._namespaces,
            "invalid_guard",
        )

    def append(
        self, record: BaselineRecordV1, *, coordinator, guard, external_guard=None
    ) -> BaselineReceipt:
        """One append image; a repeated ID is accepted only for identical metadata."""
        _require(not self._readonly, "readonly_store")
        _require(external_guard is None or callable(external_guard), "invalid_guard")
        record = _decode_baseline(BaselineRecordV1, _encode_baseline(record))
        receipt = _receipt(record)
        payload = _encode_baseline(record)

        def check_external():
            self._append_guard(record, coordinator, guard)
            if external_guard is not None:
                external_guard()
                self._append_guard(record, coordinator, guard)

        check_external()
        persisted = False
        try:
            with coordinator._locked(exclusive=False):
                coordinator_connection, _ = coordinator._load()
                coordinator_connection.close()
                check_external()
                with self._locked(exclusive=True):
                    connection, witness = self._load()
                    try:
                        row = connection.execute(
                            "SELECT record_json FROM baselines WHERE id=?", (record.baseline_id,)
                        ).fetchone()
                        if row is not None:
                            _require(row[0] == payload, "proof_mismatch")
                            check_external()
                            return receipt
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute(
                            "INSERT INTO baselines VALUES (?,?,?,?)",
                            (record.baseline_id, record.pair_key, payload, receipt.record_sha),
                        )
                        connection.execute("COMMIT")
                        image = connection.serialize()
                        self._validate_database(connection, len(image))
                        check_external()
                        self._persist(
                            image,
                            witness,
                            external_guard=check_external,
                        )
                        persisted = True
                        check_external()
                        return receipt
                    except sqlite3.Error:
                        raise LineageError("invalid_image") from None
                    finally:
                        connection.close()
        except LineageError:
            if persisted:
                raise LineageError("persistence_uncertain") from None
            raise

    def baseline_view(self) -> BaselineView:
        with self._locked(exclusive=False):
            connection, _ = self._load()
            try:
                records = tuple(
                    _decode_baseline(BaselineRecordV1, payload)
                    for (payload,) in connection.execute(
                        "SELECT record_json FROM baselines ORDER BY id"
                    )
                )
                return BaselineView(self._store_uuid, self._coordinator_store_uuid, records)
            finally:
                connection.close()

    def _history_unavailable(self, *args, **kwargs):
        raise LineageError("invalid_transition")

    # Sharing A's storage mechanisms must not grant any historical operation API.
    ready = _history_unavailable
    check_guard = _history_unavailable
    begin_publication = _history_unavailable
    mark_attempted = _history_unavailable
    mark_verifying = _history_unavailable
    needs_reconcile = _history_unavailable
    cancel_ready = _history_unavailable
    fail_ready = _history_unavailable
    complete = _history_unavailable
    complete_reconciled = _history_unavailable
    recovery_view = _history_unavailable
