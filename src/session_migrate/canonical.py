"""Pure, exact semantic hashing; native coverage and turn closure require separate audits.

Only hashes, counts and fixed issue codes leave this module. Diagnostic arrays are
bounded samples, never the authority for a prefix comparison. Use the unit stream
to compare long histories, and explicitly supply the previously observed policy.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from session_migrate.errors import SessionMigrateError
from session_migrate.model import Event, EventKind, Role
from session_migrate.witnesses import (
    BoundaryKind,
    BoundaryWitness,
    PrefixWitness,
    SequenceWitness,
)

MAX_DIAGNOSTICS = 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")
_MEDIA_FIELDS = frozenset(("image_url", "audio_url", "video_url"))
_MEDIA_TYPES = frozenset(("image", "image_url", "audio", "video", "input_image"))
_ISSUE_CODES = frozenset(
    (
        "external_media_reference",
        "opaque_event",
        "missing_tool_name",
        "missing_call_id",
        "duplicate_call_id",
        "orphan_tool_result",
        "ambiguous_tool_result",
        "duplicate_tool_result",
        "tool_identity_mismatch",
        "open_tool_call",
        "unsupported_media",
    )
)


class CanonicalError(SessionMigrateError):
    """A fixed error message that never includes a rejected value."""

    def __init__(self) -> None:
        super().__init__("Invalid canonical input.")


def _require(condition: bool) -> None:
    if not condition:
        raise CanonicalError()


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalPolicy:
    algorithm: str = "canonical-v2"
    policy_version: str = "exact-events-v1"

    def __post_init__(self) -> None:
        _require(type(self.algorithm) is str and bool(_VERSION.fullmatch(self.algorithm)))
        _require(type(self.policy_version) is str and bool(_VERSION.fullmatch(self.policy_version)))
        _require(self.algorithm == "canonical-v2" and self.policy_version == "exact-events-v1")

    @property
    def sha256(self) -> str:
        return _digest(
            b"session-migrate/canonical-policy/v2\0",
            _json_bytes(
                {
                    "algorithm": self.algorithm,
                    "policy_version": self.policy_version,
                }
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalIssue:
    code: str
    unit_index: int

    def __post_init__(self) -> None:
        _require(type(self.code) is str and self.code in _ISSUE_CODES)
        _require(type(self.unit_index) is int and 0 <= self.unit_index <= 2**31 - 1)


class _Issues:
    def __init__(self) -> None:
        self.diagnostics: list[CanonicalIssue] = []
        self.counts: Counter[str] = Counter()

    def append(self, issue: CanonicalIssue) -> None:
        self.counts[issue.code] += 1
        if len(self.diagnostics) < MAX_DIAGNOSTICS:
            self.diagnostics.append(issue)


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalUnit:
    index: int
    policy: CanonicalPolicy
    sha256: str
    tool_link_sha256: str | None = None
    tool_action: str | None = None
    tool_ordinal: int | None = None
    issues: tuple[CanonicalIssue, ...] = ()
    issue_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        _require(type(self.index) is int and 0 <= self.index < 2**31 - 1)
        _require(type(self.policy) is CanonicalPolicy)
        self.policy.__post_init__()
        _require(type(self.sha256) is str and bool(_HASH.fullmatch(self.sha256)))
        _require(
            self.tool_link_sha256 is None
            or (type(self.tool_link_sha256) is str and bool(_HASH.fullmatch(self.tool_link_sha256)))
        )
        _require(self.tool_action in (None, "call", "result"))
        _require(
            self.tool_ordinal is None
            or (type(self.tool_ordinal) is int and 0 <= self.tool_ordinal < 2**31 - 1)
        )
        _require(
            type(self.issues) is tuple
            and len(self.issues) <= MAX_DIAGNOSTICS
            and all(type(i) is CanonicalIssue for i in self.issues)
        )
        _require(type(self.issue_counts) is tuple)
        for item in self.issue_counts:
            _require(type(item) is tuple and len(item) == 2)
            code, count = item
            _require(type(code) is str and code in _ISSUE_CODES)
            _require(type(count) is int and 0 < count <= 2**31 - 1)


@dataclass(frozen=True, slots=True, repr=False)
class SemanticWitness:
    policy: CanonicalPolicy
    prefix_witness: PrefixWitness
    units: tuple[CanonicalUnit, ...]
    issues: tuple[CanonicalIssue, ...]
    issue_counts: tuple[tuple[str, int], ...]
    links_proven: bool

    @property
    def blocking(self) -> bool:
        return bool(self.issue_counts)


@dataclass(frozen=True, slots=True, repr=False)
class PrefixComparison:
    matches: bool
    sequence_extends: bool
    reason: str
    current_count: int


def _digest(domain: bytes, data: bytes) -> str:
    return hashlib.sha256(domain + data).hexdigest()


def _json_value(value: object, ancestors: set[int], depth: int = 0) -> object:
    _require(depth <= 128)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        _require(math.isfinite(value))
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise CanonicalError() from None
        return value
    _require(type(value) in (dict, list))
    _require(id(value) not in ancestors)
    ancestors.add(id(value))
    try:
        if type(value) is list:
            return [_json_value(item, ancestors, depth + 1) for item in value]
        _require(all(type(key) is str for key in value))
        return {
            _json_value(key, ancestors, depth + 1): _json_value(item, ancestors, depth + 1)
            for key, item in value.items()
        }
    finally:
        ancestors.remove(id(value))


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            _json_value(value, set()),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, OverflowError, RecursionError, UnicodeError):
        raise CanonicalError() from None


def _media_reference(value: object, issues: _Issues, index: int) -> object:
    _require(type(value) is str)
    if not value.startswith("data:"):
        issues.append(CanonicalIssue("external_media_reference", index))
        return {"reference_sha256": _digest(b"media-reference\0", value.encode("utf-8"))}
    header, separator, encoded = value.partition(",")
    _require(bool(separator) and header.endswith(";base64") and len(header) > 12)
    mime = header[5:-7]
    _require(bool(_MIME.fullmatch(mime)))
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error):
        raise CanonicalError() from None
    return {"mime": mime, "bytes_sha256": hashlib.sha256(decoded).hexdigest()}


def _media_block(value: dict, issues: _Issues, index: int) -> dict:
    """Interpret a known media block, preserving its carrier and other metadata."""
    result = dict(value)
    recognized = False
    for key in (*_MEDIA_FIELDS, "url"):
        if key not in value:
            continue
        recognized = True
        item = value[key]
        if type(item) is dict and "url" in item:
            result[key] = {**item, "url": _media_reference(item["url"], issues, index)}
        else:
            result[key] = _media_reference(item, issues, index)
    source = value.get("source")
    if type(source) is dict:
        if source.get("type") == "base64":
            recognized = True
            mime, data = source.get("media_type", source.get("mime_type")), source.get("data")
            _require(type(mime) is str and type(data) is str)
            result["source"] = {
                **source,
                "data": _media_reference(
                    f"data:{mime};base64,{data}",
                    issues,
                    index,
                ),
            }
        elif source.get("type") == "url" and "url" in source:
            recognized = True
            result["source"] = {**source, "url": _media_reference(source["url"], issues, index)}
    for key in ("image_path", "audio_path", "video_path", "media_path"):
        if key in value:
            recognized = True
            issues.append(CanonicalIssue("external_media_reference", index))
    if not recognized:
        issues.append(CanonicalIssue("unsupported_media", index))
    return result


def _canonical_payload(payload: dict, kind: EventKind, issues: _Issues, index: int) -> dict:
    # Tool input and arbitrary JSON remain exact, even when keys resemble media.
    media_type = payload.get("block_type", payload.get("type"))
    if kind is EventKind.CONTEXT and type(media_type) is str and media_type in _MEDIA_TYPES:
        return _media_block(payload, issues, index)
    if kind is not EventKind.TOOL_RESULT or type(payload.get("content_blocks")) is not list:
        return payload
    blocks = []
    for block in payload["content_blocks"]:
        block_type = block.get("type") if type(block) is dict else None
        blocks.append(
            _media_block(block, issues, index)
            if (type(block_type) is str and block_type in _MEDIA_TYPES)
            else block
        )
    return {**payload, "content_blocks": blocks}


def iter_canonical_units(
    events: Iterable[Event], policy: CanonicalPolicy
) -> Iterator[CanonicalUnit]:
    """Hash every ordered event, keeping native call identifiers out of the hash.

    Tool identifiers are used only to resolve logical ordinals. A missing,
    duplicate or unresolved identifier produces an explicit blocking issue.
    Provenance indices and timestamps are source coordinates, not semantics.
    """
    _require(type(policy) is CanonicalPolicy)
    policy.__post_init__()
    calls: dict[str, tuple[int, str | None, bytes | None]] = {}
    duplicate_calls: set[str] = set()
    results: set[str] = set()
    call_count = 0
    for index, event in enumerate(events):
        _require(type(event) is Event and type(event.kind) is EventKind)
        _require(event.role is None or type(event.role) is Role)
        _require(type(event.payload) is dict)
        for value in (event.text, event.tool_name, event.tool_call_id):
            _require(value is None or type(value) is str)
            if value is not None:
                _json_bytes(value)
        payload = _json_value(event.payload, set())
        issues = _Issues()
        ordinal = None
        action = None
        link = None
        # None marks absence; explicit JSON null is b"null", preserving both
        # presence and JSON types that Python equality otherwise conflates.
        namespace = _json_bytes(payload["namespace"]) if "namespace" in payload else None
        if event.kind is EventKind.OPAQUE:
            issues.append(CanonicalIssue("opaque_event", index))
        if event.kind is EventKind.TOOL_CALL:
            action, ordinal = "call", call_count
            call_count += 1
            if not event.tool_name:
                issues.append(CanonicalIssue("missing_tool_name", index))
            if not event.tool_call_id:
                issues.append(CanonicalIssue("missing_call_id", index))
            elif event.tool_call_id in calls:
                duplicate_calls.add(event.tool_call_id)
                issues.append(CanonicalIssue("duplicate_call_id", index))
            else:
                calls[event.tool_call_id] = (ordinal, event.tool_name, namespace)
        elif event.kind is EventKind.TOOL_RESULT:
            action = "result"
            call = calls.get(event.tool_call_id) if event.tool_call_id else None
            if call is None:
                issues.append(CanonicalIssue("orphan_tool_result", index))
            elif event.tool_call_id in duplicate_calls:
                issues.append(CanonicalIssue("ambiguous_tool_result", index))
            elif event.tool_call_id in results:
                issues.append(CanonicalIssue("duplicate_tool_result", index))
            else:
                ordinal, name, call_namespace = call
                results.add(event.tool_call_id)
                if (event.tool_name is not None and event.tool_name != name) or (
                    namespace is not None and namespace != call_namespace
                ):
                    issues.append(CanonicalIssue("tool_identity_mismatch", index))
                else:
                    link = _digest(
                        b"session-migrate/tool-link/v2\0",
                        _json_bytes(
                            {
                                "policy": policy.sha256,
                                "call_ordinal": ordinal,
                                "result_index": index,
                            }
                        ),
                    )
        semantic = {
            "kind": event.kind.value,
            "role": event.role.value if event.role else None,
            "text": event.text,
            "tool_name": event.tool_name,
            "tool_ordinal": ordinal,
            "payload": _canonical_payload(payload, event.kind, issues, index),
        }
        yield CanonicalUnit(
            index,
            policy,
            _digest(b"session-migrate/semantic-unit/v2\0", _json_bytes(semantic)),
            link,
            action,
            ordinal,
            tuple(issues.diagnostics),
            tuple(sorted(issues.counts.items())),
        )


class _Chain:
    def __init__(self, domain: str, policy: CanonicalPolicy) -> None:
        self.count = 0
        self._domain = f"session-migrate/{domain}/v2\0".encode("ascii")
        self._policy = bytes.fromhex(policy.sha256)
        self.root = hashlib.sha256(self._domain + self._policy).digest()

    def add(self, sha256: str) -> None:
        _require(self.count < 2**31 - 1)
        self.root = hashlib.sha256(
            self._domain
            + self._policy
            + self.count.to_bytes(8, "big")
            + self.root
            + bytes.fromhex(sha256)
        ).digest()
        self.count += 1

    def witness(self) -> SequenceWitness:
        return SequenceWitness(self.count, self.root.hex())


def hash_events(events: Iterable[Event], policy: CanonicalPolicy) -> SemanticWitness:
    """Produce complete roots and bounded diagnostics, without declaring native proof."""
    _require(type(policy) is CanonicalPolicy)
    policy.__post_init__()
    units, links = _Chain("unit-chain", policy), _Chain("tool-link-chain", policy)
    diagnostic_units: list[CanonicalUnit] = []
    diagnostic_issues: list[CanonicalIssue] = []
    issue_counts: Counter[str] = Counter()
    pending: set[int] = set()
    for unit in iter_canonical_units(events, policy):
        units.add(unit.sha256)
        if unit.tool_link_sha256 is not None:
            links.add(unit.tool_link_sha256)
        if len(diagnostic_units) < MAX_DIAGNOSTICS:
            diagnostic_units.append(unit)
        issue_counts.update(dict(unit.issue_counts))
        for issue in unit.issues:
            if len(diagnostic_issues) < MAX_DIAGNOSTICS:
                diagnostic_issues.append(issue)
        if unit.tool_action == "call":
            pending.add(unit.tool_ordinal)
        elif unit.tool_link_sha256 is not None:
            pending.discard(unit.tool_ordinal)
    if pending:
        issue_counts["open_tool_call"] += len(pending)
        for _ in range(min(len(pending), MAX_DIAGNOSTICS - len(diagnostic_issues))):
            diagnostic_issues.append(CanonicalIssue("open_tool_call", units.count))
    prefix = PrefixWitness(
        2,
        units.witness(),
        links.witness(),
        None,
        0,
        BoundaryWitness(BoundaryKind.UNPROVEN, units.count, len(pending)),
    )
    return SemanticWitness(
        policy,
        prefix,
        tuple(diagnostic_units),
        tuple(diagnostic_issues),
        tuple(sorted(issue_counts.items())),
        not issue_counts,
    )


def compare_prefix(
    current_units: Iterable[CanonicalUnit],
    previous: PrefixWitness,
    *,
    previous_policy: CanonicalPolicy | None = None,
    current_policy: CanonicalPolicy | None = None,
) -> PrefixComparison:
    """Compare complete count/chain witnesses, without searching text or subsequences.

    Policy compatibility is explicit. `sequence_extends` describes only ordered
    units and tool links; it supplies no coverage, structure or boundary proof.
    For an empty stream the caller must provide its observed current policy.
    """
    _require(type(previous) is PrefixWitness)
    if previous_policy is None:
        return PrefixComparison(False, False, "policy_unproven", 0)
    _require(type(previous_policy) is CanonicalPolicy)
    if (previous_policy.algorithm, previous_policy.policy_version) != (
        "canonical-v2",
        "exact-events-v1",
    ):
        return PrefixComparison(False, False, "policy_mismatch", 0)
    if current_policy is not None:
        _require(type(current_policy) is CanonicalPolicy)
        if current_policy != previous_policy:
            return PrefixComparison(False, False, "policy_mismatch", 0)
    units = links = None
    prefix_units = prefix_links = None
    issue_found = False
    count = 0
    for unit in current_units:
        _require(type(unit) is CanonicalUnit and unit.index == count)
        if unit.policy != previous_policy or (
            current_policy is not None and unit.policy != current_policy
        ):
            return PrefixComparison(False, False, "policy_mismatch", count)
        if units is None:
            current_policy = unit.policy
            units, links = _Chain("unit-chain", unit.policy), _Chain("tool-link-chain", unit.policy)
        units.add(unit.sha256)
        if unit.tool_link_sha256 is not None:
            links.add(unit.tool_link_sha256)
        issue_found |= bool(unit.issues or unit.issue_counts)
        count += 1
        if count == previous.units.count:
            prefix_units, prefix_links = units.witness(), links.witness()
    if current_policy is None:
        return PrefixComparison(False, False, "policy_unproven", count)
    if previous.units.count == 0:
        prefix_units = _Chain("unit-chain", current_policy).witness()
        prefix_links = _Chain("tool-link-chain", current_policy).witness()
    if count < previous.units.count:
        return PrefixComparison(False, False, "shortened", count)
    if issue_found:
        return PrefixComparison(False, False, "semantic_issues", count)
    if prefix_units != previous.units or prefix_links != previous.tool_links:
        return PrefixComparison(False, False, "hash_mismatch", count)
    return PrefixComparison(True, count > previous.units.count, "match", count)
