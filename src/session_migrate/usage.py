"""Private issued usage captures and conservative, content-free RAW observations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from time import monotonic_ns
from weakref import WeakKeyDictionary

from session_migrate.canonical import CanonicalPolicy
from session_migrate.catalog import Catalog
from session_migrate.continuity_observation import (
    EndpointObserver,
    _borrow_validated_capture,
    observe_endpoint,
    resolve_profile,
    validate_observer,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import QualifiedIdentity
from session_migrate.native_snapshots import NativeSnapshot
from session_migrate.profiles import Profile, SessionRef, resolve_source
from session_migrate.usage_extractors import DIMENSIONS, decode_usage

SEMANTICS_VERSION = "usage-raw-fields-v1"
MAX_COUNTER = 2**63 - 1
_SCHEMAS = {
    ("claude", "2.1.209"): "claude-jsonl-v1",
    ("codex", "0.144.4"): "codex-legacy",
    ("codex", "0.153.4"): "codex-paginated",
    ("opencode", "2.0.22"): "opencode-session-transfer-v2",
    ("opencode", "2.0.23"): "opencode-session-transfer-v2",
}
_REASONS = {
    "schema_unproven",
    "invalid_counter",
    "identity_ambiguous",
    "terminal_unproven",
    "epoch_unproven",
    "imported",
    "origin_unknown",
    "duplicate_snapshot",
    "aggregate_overflow",
}


class UsageError(SessionMigrateError):
    def __init__(self, code="usage_capture_unavailable", *, native_read_effects=()):
        self.code = (
            code
            if code
            in {
                "usage_capture_unavailable",
                "usage_capture_expired",
                "invalid_usage",
                "usage_limit",
                "refresh_required",
                "imported_accounting_unproven",
            }
            else "usage_capture_unavailable"
        )
        allowed = {
            "file_capture_read_attempted",
            "file_capture_read",
            "native_export_read_attempted",
            "native_export_read",
            "private_scratch_possible",
            "private_scratch",
            "native_cache_or_wal_possible",
        }
        self.native_read_effects = tuple(
            effect for effect in native_read_effects if type(effect) is str and effect in allowed
        )
        super().__init__(self.code)


def _require(condition, code="invalid_usage"):
    if not condition:
        raise UsageError(code)


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _hash_string(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _sha(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class UsageLimits:
    capture_bytes: int = 8 * 1024 * 1024
    samples: int = 100_000
    metadata_bytes: int = 1024 * 1024
    ttl_seconds: int = 30

    def __post_init__(self):
        for value, maximum in (
            (self.capture_bytes, 128 * 1024 * 1024),
            (self.samples, 100_000),
            (self.metadata_bytes, 1024 * 1024),
            (self.ttl_seconds, 30),
        ):
            _require(_integer(value, maximum) and value > 0)


@dataclass(frozen=True, slots=True)
class UsageSchemaKey:
    format: str
    exact_client: str
    native_schema: str
    extractor_version: str = SEMANTICS_VERSION

    def __post_init__(self):
        _require(_SCHEMAS.get((self.format, self.exact_client)) == self.native_schema)
        _require(self.extractor_version == SEMANTICS_VERSION)


@dataclass(frozen=True, slots=True)
class CounterObservationV1:
    value: int | None
    quality: str
    semantics_id: str
    reason: str | None
    schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        _require(self.value is None or _integer(self.value, MAX_COUNTER))
        _require(self.quality in {"recorded", "synthetic", "heuristic", "unproven"})
        _require(self.semantics_id == SEMANTICS_VERSION)
        _require(self.reason is None or self.reason in _REASONS)
        _require(self.value is not None or self.quality == "unproven")


@dataclass(frozen=True, slots=True, repr=False)
class CaptureRevisionV1:
    capture_id: str
    authority_sha256: str
    source_identity: QualifiedIdentity
    profile_fingerprint: str
    schema_key: UsageSchemaKey
    read_generation: int
    schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        _require(type(self.capture_id) is str and len(self.capture_id) == 32)
        _require(all(c in "0123456789abcdef" for c in self.capture_id))
        _require(_hash_string(self.authority_sha256) and _hash_string(self.profile_fingerprint))
        _require(type(self.source_identity) is QualifiedIdentity)
        self.source_identity.__post_init__()
        _require(type(self.schema_key) is UsageSchemaKey)
        self.schema_key.__post_init__()
        _require(_integer(self.read_generation, MAX_COUNTER))


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class UsageSampleV1:
    sample_key: str | None
    source_identity: QualifiedIdentity
    capture_revision: CaptureRevisionV1
    record_index: int
    schema_key: UsageSchemaKey
    scope: str
    scope_id: str | None
    epoch: str | None
    request_id: str | None
    message_id: str | None
    turn_id: str | None
    model: str | None
    provider: str | None
    counters: tuple[tuple[str, CounterObservationV1], ...]
    origin: str = "unknown"
    coverage: str = "partial"
    reason: str = "origin_unknown"
    unit: str = "tokens"
    schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        _require(type(self.source_identity) is QualifiedIdentity)
        _require(type(self.capture_revision) is CaptureRevisionV1)
        _require(type(self.schema_key) is UsageSchemaKey)
        _require(
            self.capture_revision.source_identity == self.source_identity
            and self.capture_revision.schema_key == self.schema_key
        )
        _require(_integer(self.record_index, 100_000))
        _require(
            self.scope in {"request", "message", "turn", "session_cumulative", "thread_cumulative"}
        )
        _require(self.origin in {"native", "imported", "estimated", "unknown"})
        _require(self.coverage in {"complete", "partial", "unknown"})
        _require(self.reason in _REASONS and self.unit == "tokens")
        _require(type(self.counters) is tuple and tuple(k for k, _ in self.counters) == DIMENSIONS)
        _require(all(type(v) is CounterObservationV1 for _, v in self.counters))
        for value, maximum in (
            (self.sample_key, 64),
            (self.scope_id, 128),
            (self.epoch, 128),
            (self.request_id, 128),
            (self.message_id, 128),
            (self.turn_id, 128),
            (self.model, 256),
            (self.provider, 256),
        ):
            _require(
                value is None
                or (
                    type(value) is str
                    and len(value.encode()) <= maximum
                    and not any(ord(c) < 32 for c in value)
                )
            )


@dataclass(frozen=True, slots=True, repr=False)
class UsageGroupV1:
    model: str | None
    provider: str | None
    scope: str
    epoch: str | None
    origin: str
    dimension_totals: tuple[tuple[str, int | None], ...]
    dimension_quality: tuple[tuple[str, str], ...]
    dimension_excluded_counts: tuple[tuple[str, int], ...]
    schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        _require(self.origin == "unknown" and self.epoch is None)
        for pairs in (
            self.dimension_totals,
            self.dimension_quality,
            self.dimension_excluded_counts,
        ):
            _require(type(pairs) is tuple and tuple(k for k, _ in pairs) == DIMENSIONS)
        _require(all(value is None for _, value in self.dimension_totals))
        _require(all(value == "unproven" for _, value in self.dimension_quality))
        _require(all(_integer(value, 100_000) for _, value in self.dimension_excluded_counts))


@dataclass(frozen=True, slots=True, repr=False)
class UsageAggregateV1:
    groups: tuple[UsageGroupV1, ...]
    excluded_counts_by_reason: tuple[tuple[str, int], ...]
    considered_count: int
    coverage: str = "unknown"
    schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        _require(type(self.groups) is tuple and all(type(g) is UsageGroupV1 for g in self.groups))
        _require(_integer(self.considered_count, 100_000) and self.coverage == "unknown")
        _require(type(self.excluded_counts_by_reason) is tuple)
        _require(
            all(
                reason in _REASONS and _integer(count, 100_000)
                for reason, count in self.excluded_counts_by_reason
            )
        )


@dataclass(frozen=True, slots=True, repr=False)
class UsageReportV1:
    capture_revision: CaptureRevisionV1
    source_identity: QualifiedIdentity
    schema_key: UsageSchemaKey
    semantics_version: str
    samples_page: tuple[UsageSampleV1, ...]
    next_cursor: str | None
    sample_count: int
    aggregate: UsageAggregateV1
    coverage: str
    exclusions: tuple[tuple[str, int], ...]
    native_read_effects: tuple[str, ...]
    schema_version: int = field(default=1, init=False)

    def __post_init__(self):
        _require(type(self.capture_revision) is CaptureRevisionV1)
        _require(
            self.source_identity == self.capture_revision.source_identity
            and self.schema_key == self.capture_revision.schema_key
        )
        _require(self.semantics_version == SEMANTICS_VERSION)
        _require(
            type(self.samples_page) is tuple
            and len(self.samples_page) <= 100
            and all(type(sample) is UsageSampleV1 for sample in self.samples_page)
        )
        _require(
            all(sample.capture_revision == self.capture_revision for sample in self.samples_page)
        )
        _require(_integer(self.sample_count, 100_000))
        _require(self.next_cursor is None or _hash_string(self.next_cursor))
        _require(type(self.aggregate) is UsageAggregateV1)
        _require(self.coverage in {"partial", "unknown"})
        _require(
            type(self.native_read_effects) is tuple
            and all(type(effect) is str for effect in self.native_read_effects)
        )

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class UsageCapture:
    revision: CaptureRevisionV1
    native_read_effects: tuple[str, ...]

    def __init__(self):
        raise UsageError()

    def close(self):
        with _LOCK:
            _CAPTURES.pop(self, None)


@dataclass(slots=True, repr=False)
class _CaptureState:
    snapshot: NativeSnapshot
    observer: EndpointObserver
    ref: SessionRef
    endpoint: object
    limits: UsageLimits
    expires_ns: int
    binding: str
    catalog_witness: tuple
    samples: tuple[UsageSampleV1, ...] | None = None
    owned_bytes: int = 0


_LOCK = threading.RLock()
_RESERVED_BYTES = 0
_RESERVED_CAPTURES = 0
_CAPTURES: WeakKeyDictionary[UsageCapture, _CaptureState] = WeakKeyDictionary()
_SAMPLES: WeakKeyDictionary[UsageSampleV1, str] = WeakKeyDictionary()


def _catalog_witness(path):
    info = os.stat(path, follow_symlinks=False)
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


def capture_usage(ref, current_profile, observer, limits=None, *, client_version):
    """Issue one RAW capture from the actual selected driver and exact observed client."""
    global _RESERVED_BYTES, _RESERVED_CAPTURES
    captured = endpoint = None
    reserved = False
    limits = UsageLimits() if limits is None else limits
    try:
        _require(type(observer) is EndpointObserver and type(ref) is SessionRef)
        _require(type(current_profile) is Profile and type(limits) is UsageLimits)
        limits.__post_init__()
        validate_observer(observer)
        _require(resolve_profile(observer, ref) is current_profile)
        _require(
            ref.profile_id == current_profile.id
            and ref.profile_fingerprint == current_profile.fingerprint
        )
        schema = _SCHEMAS.get((ref.format.value, client_version))
        _require(schema is not None)
        witness = _catalog_witness(observer._catalog.path)
        cached = resolve_source(observer._catalog, current_profile, ref)
        with Catalog.open_readonly_existing(observer._catalog.path) as physical:
            _require(resolve_source(physical, current_profile, ref) == cached)
        with _LOCK:
            for old, state in tuple(_CAPTURES.items()):
                if monotonic_ns() >= state.expires_ns:
                    _CAPTURES.pop(old, None)
            _require(len(_CAPTURES) + _RESERVED_CAPTURES < 32, "usage_limit")
            _require(
                sum(s.owned_bytes for s in _CAPTURES.values())
                + _RESERVED_BYTES
                + limits.capture_bytes
                <= 128 * 1024 * 1024,
                "usage_limit",
            )
            _RESERVED_BYTES += limits.capture_bytes
            _RESERVED_CAPTURES += 1
            reserved = True
        # The driver owns its own fresh physical index and uses the smaller read budget.
        with Catalog.open_readonly_existing(observer._catalog.path) as current_catalog:
            driver = EndpointObserver(
                current_catalog,
                observer._profiles,
                base_environ=observer._environment,
                byte_limit=min(observer._byte_limit, limits.capture_bytes),
                legacy_runtime_contexts=observer._legacy_runtime_contexts,
            )
            endpoint = observe_endpoint(
                driver, ref, client_version=client_version, canonical_policy=CanonicalPolicy()
            )

        def issue(snapshot, actual_client):
            global _RESERVED_BYTES, _RESERVED_CAPTURES
            nonlocal captured, reserved
            _require(
                actual_client == client_version
                and endpoint.observation.audit.client_id == actual_client
            )
            _require(endpoint.observation.audit.schema_id == schema)
            _require(
                snapshot.source_format is ref.format
                and snapshot.authority_sha256 == endpoint.authority_sha256
                and hashlib.sha256(snapshot.raw_bytes).hexdigest() == endpoint.authority_sha256
            )
            _require(len(snapshot.raw_bytes) <= limits.capture_bytes, "usage_limit")
            revision = CaptureRevisionV1(
                uuid.uuid4().hex,
                snapshot.authority_sha256,
                endpoint.identity,
                current_profile.fingerprint,
                UsageSchemaKey(ref.format.value, actual_client, schema),
                monotonic_ns(),
            )
            captured = object.__new__(UsageCapture)
            object.__setattr__(captured, "revision", revision)
            object.__setattr__(captured, "native_read_effects", endpoint.native_read_effects)
            with _LOCK:
                _require(len(_CAPTURES) < 32, "usage_limit")
                _require(
                    sum(s.owned_bytes for s in _CAPTURES.values())
                    + _RESERVED_BYTES
                    - limits.capture_bytes
                    + len(snapshot.raw_bytes)
                    <= 128 * 1024 * 1024,
                    "usage_limit",
                )
                _RESERVED_BYTES -= limits.capture_bytes
                _RESERVED_CAPTURES -= 1
                reserved = False
                _CAPTURES[captured] = _CaptureState(
                    snapshot,
                    driver,
                    ref,
                    endpoint,
                    limits,
                    monotonic_ns() + limits.ttl_seconds * 1_000_000_000,
                    _sha([asdict(revision), list(captured.native_read_effects)]),
                    witness,
                    owned_bytes=len(snapshot.raw_bytes),
                )
            return captured

        _borrow_validated_capture(endpoint, observer=driver, ref=ref, consumer=issue)
        with Catalog.open_readonly_existing(observer._catalog.path) as physical:
            _require(resolve_source(physical, current_profile, ref) == cached)
        _require(_catalog_witness(observer._catalog.path) == witness)
        return captured
    except UsageError as error:
        if endpoint is not None:
            error.native_read_effects = endpoint.native_read_effects
        if captured is not None:
            captured.close()
        raise
    except (
        SessionMigrateError,
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        AttributeError,
    ) as error:
        if captured is not None:
            captured.close()
        raise UsageError(
            native_read_effects=endpoint.native_read_effects
            if endpoint is not None
            else getattr(error, "native_read_effects", ())
        ) from None
    finally:
        if reserved:
            with _LOCK:
                _RESERVED_BYTES -= limits.capture_bytes
                _RESERVED_CAPTURES -= 1


def _validate_capture(capture):
    _require(type(capture) is UsageCapture and capture in _CAPTURES)
    state = _CAPTURES[capture]
    if monotonic_ns() >= state.expires_ns:
        capture.close()
        raise UsageError("usage_capture_expired")
    _require(_sha([asdict(capture.revision), list(capture.native_read_effects)]) == state.binding)
    _require(_catalog_witness(state.observer._catalog.path) == state.catalog_witness)
    _borrow_validated_capture(
        state.endpoint,
        observer=state.observer,
        ref=state.ref,
        consumer=lambda snapshot, client: _require(
            snapshot is state.snapshot and client == capture.revision.schema_key.exact_client
        ),
    )
    return state


def _counter(value):
    valid = _integer(value, MAX_COUNTER)
    return CounterObservationV1(
        value if valid else None,
        "recorded" if valid else "unproven",
        SEMANTICS_VERSION,
        None if valid else "invalid_counter",
    )


def aggregate_usage(samples, policy=SEMANTICS_VERSION):
    """Aggregate only privately issued observations; no native additivity proof exists yet."""
    _require(policy == SEMANTICS_VERSION and type(samples) is tuple and len(samples) <= 100_000)
    groups, exclusions, observations = {}, Counter(), {}
    with _LOCK:
        for sample in samples:
            _require(type(sample) is UsageSampleV1 and sample in _SAMPLES)
            _require(_sha(asdict(sample)) == _SAMPLES[sample])
            if sample.sample_key:
                counter_binding = _sha(asdict(sample)["counters"])
                bindings = observations.setdefault(sample.sample_key, Counter())
                bindings[counter_binding] += 1
            key = (sample.model, sample.provider, sample.scope, sample.epoch, sample.origin)
            groups[key] = groups.get(key, 0) + 1
            exclusions["origin_unknown"] += 1
        for bindings in observations.values():
            count = sum(bindings.values())
            if len(bindings) > 1:
                exclusions["identity_ambiguous"] += count
            elif count > 1:
                exclusions["duplicate_snapshot"] += count - 1
    # RAW numeric observations are not authority for native consumption. No zero total.
    return UsageAggregateV1(
        tuple(
            UsageGroupV1(
                *key,
                tuple((d, None) for d in DIMENSIONS),
                tuple((d, "unproven") for d in DIMENSIONS),
                tuple((d, count) for d in DIMENSIONS),
            )
            for key, count in sorted(groups.items(), key=lambda item: json.dumps(item[0]))
        ),
        tuple(sorted(exclusions.items())),
        len(samples),
    )


def extract_usage(capture, *, offset=0, limit=100, cursor=None):
    """Decode the issued bytes once; paginate without changing the global aggregate."""
    try:
        with _LOCK:
            state = _validate_capture(capture)
            _require(_integer(offset, 100_000) and _integer(limit, 100) and limit > 0)
            if cursor is not None:
                _require(
                    type(cursor) is str
                    and cursor
                    == _sha(
                        [
                            state.binding,
                            offset,
                            limit,
                            SEMANTICS_VERSION,
                        ]
                    ),
                    "refresh_required",
                )
            if state.samples is None:
                candidates = decode_usage(
                    state.snapshot.raw_bytes,
                    state.snapshot.source_format,
                    capture.revision.schema_key.exact_client,
                    state.limits.samples,
                )
                issued = []
                for candidate in candidates:
                    key = (
                        _sha(
                            [
                                "usage-key-v1",
                                asdict(capture.revision.source_identity),
                                candidate.scope,
                                candidate.identity_parts,
                            ]
                        )
                        if candidate.identity_parts
                        else None
                    )
                    sample = UsageSampleV1(
                        key,
                        capture.revision.source_identity,
                        capture.revision,
                        candidate.record_index,
                        capture.revision.schema_key,
                        candidate.scope,
                        candidate.scope_id,
                        None,
                        candidate.request_id,
                        candidate.message_id,
                        candidate.turn_id,
                        candidate.model,
                        candidate.provider,
                        tuple((name, _counter(value)) for name, value in candidate.counters),
                        reason="origin_unknown" if key else "identity_ambiguous",
                    )
                    size = len(json.dumps(asdict(sample), ensure_ascii=True).encode())
                    _require(
                        sum(s.owned_bytes for s in _CAPTURES.values()) + _RESERVED_BYTES + size
                        <= 128 * 1024 * 1024,
                        "usage_limit",
                    )
                    state.owned_bytes += size
                    _SAMPLES[sample] = _sha(asdict(sample))
                    issued.append(sample)
                state.samples = tuple(issued)
            aggregate = aggregate_usage(state.samples)
            end = offset + limit
            report = UsageReportV1(
                capture.revision,
                capture.revision.source_identity,
                capture.revision.schema_key,
                SEMANTICS_VERSION,
                state.samples[offset:end],
                _sha([state.binding, end, limit, SEMANTICS_VERSION])
                if end < len(state.samples)
                else None,
                len(state.samples),
                aggregate,
                "partial" if state.samples else "unknown",
                aggregate.excluded_counts_by_reason,
                capture.native_read_effects,
            )
            _require(
                len(json.dumps(report.to_dict(), ensure_ascii=True).encode())
                <= state.limits.metadata_bytes,
                "usage_limit",
            )
            _validate_capture(capture)
            return report
    except UsageError:
        raise
    except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError):
        raise UsageError() from None


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ImportedPrefixBinding:
    def __init__(self):
        raise UsageError("imported_accounting_unproven")


def issue_imported_prefix_binding(receipt, checkpoints, current_target_capture, current_profiles):
    """F4 semantic checkpoints/adopted_now do not seal historical accounting fields."""
    with _LOCK:
        _validate_capture(current_target_capture)
    raise UsageError("imported_accounting_unproven")


def classify_origin(capture, samples, binding=None):
    with _LOCK:
        _validate_capture(capture)
        _require(binding is None, "imported_accounting_unproven")
        _require(type(samples) is tuple)
        for sample in samples:
            _require(sample in _SAMPLES and sample.capture_revision == capture.revision)
            _require(sample.origin == "unknown" and _sha(asdict(sample)) == _SAMPLES[sample])
    return samples
