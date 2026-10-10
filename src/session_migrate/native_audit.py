"""Private same-capture raw audits and canonical observations.

Coverage here is raw accounting completeness, never native identity, closure or
loss-free replay. Handles are volatile module capabilities, not signatures or
protection from hostile Python code in this process. No bodies are serialized.
"""

from __future__ import annotations

import copy
import hashlib
import io
from dataclasses import asdict, dataclass, fields, replace
from weakref import WeakKeyDictionary

from session_migrate.audit_adapters import (
    KNOWN_LOSS_ISSUES,
    LOSS,
    UNKNOWN,
    AdapterResult,
    AuditAtom,
    AuditIssue,
    digest,
    strict_decode,
)
from session_migrate.canonical import CanonicalPolicy, SemanticWitness, hash_events
from session_migrate.errors import SessionMigrateError
from session_migrate.jsonl import (
    DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_TOTAL_BYTES,
    JsonlRecord,
)
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, Event, EventKind, Session
from session_migrate.native_snapshots import CaptureProvenance, NativeSnapshot, SourceCaptureContext
from session_migrate.witnesses import BoundaryWitness, PrefixWitness, SequenceWitness


class NativeAuditError(SessionMigrateError):
    def __init__(self) -> None:
        super().__init__("Invalid native audit authority or source.")


@dataclass(frozen=True, slots=True, repr=False)
class AuditPolicy:
    policy_id: str = "native-audit-v1"
    client_version: str | None = None

    def __post_init__(self) -> None:
        if self.policy_id != "native-audit-v1" or (
            self.client_version is not None
            and (type(self.client_version) is not str or len(self.client_version) > 128)
        ):
            raise NativeAuditError()

    @property
    def sha256(self) -> str:
        return digest([self.policy_id, self.client_version])


@dataclass(frozen=True, slots=True, repr=False)
class AuditSelection:
    policy_id: str = "native-active-v1"

    def __post_init__(self) -> None:
        if self.policy_id != "native-active-v1":
            raise NativeAuditError()


@dataclass(frozen=True, slots=True, repr=False, init=False, eq=False, weakref_slot=True)
class NativeAudit:
    authority_sha256: str
    audit_policy_id: str
    audit_policy_sha256: str
    selection_policy_id: str
    schema_id: str
    client_id: str | None
    selection_sha256: str
    structure: SequenceWitness
    classification: SequenceWitness
    classification_counts: tuple[tuple[str, int], ...]
    raw_atom_count: int
    semantic_atom_count: int
    issues: tuple[AuditIssue, ...]
    issue_counts: tuple[tuple[str, int], ...]
    coverage: Coverage

    @property
    def atoms(self) -> tuple[AuditAtom, ...]:
        state = _audit_state(self)
        validate_audit(state.snapshot, self)
        return tuple(replace(atom) for atom in state.atoms)


@dataclass(frozen=True, slots=True, repr=False, init=False, eq=False, weakref_slot=True)
class CanonicalObservation:
    audit: NativeAudit
    policy: CanonicalPolicy
    witness: SemanticWitness
    source_prefix: PrefixWitness
    coverage: Coverage
    fidelity: Coverage
    boundary: BoundaryWitness
    identity: None

    @property
    def session(self) -> Session:
        validate_observation(self)
        return copy.deepcopy(_audit_state(self.audit).original)

    @property
    def events(self) -> tuple[Event, ...]:
        validate_observation(self)
        return copy.deepcopy(_audit_state(self.audit).normalized.events)

    @property
    def atoms(self) -> tuple[AuditAtom, ...]:
        validate_observation(self)
        return self.audit.atoms


@dataclass(frozen=True, slots=True, repr=False)
class _AuditState:
    snapshot: NativeSnapshot
    snapshot_binding: str
    policy: AuditPolicy
    selection: AuditSelection
    metadata_binding: str
    original: Session | None
    normalized: Session | None
    session_binding: str
    normalized_binding: str
    atoms: tuple[AuditAtom, ...]
    atoms_binding: str


@dataclass(frozen=True, slots=True, repr=False)
class _ObservationState:
    metadata_binding: str


_AUDITS: WeakKeyDictionary[NativeAudit, _AuditState] = WeakKeyDictionary()
_OBSERVATIONS: WeakKeyDictionary[CanonicalObservation, _ObservationState] = WeakKeyDictionary()


def _snapshot_binding(snapshot: NativeSnapshot) -> str:
    if type(snapshot) is not NativeSnapshot:
        raise NativeAuditError()
    SourceCaptureContext(snapshot.source_format, snapshot.source_path)
    if (
        type(snapshot.raw_bytes) is not bytes
        or len(snapshot.raw_bytes) > DEFAULT_MAX_TOTAL_BYTES
        or type(snapshot.provenance) is not CaptureProvenance
    ):
        raise NativeAuditError()
    return digest(
        [
            snapshot.source_format.value,
            str(snapshot.source_path),
            snapshot.provenance.value,
            snapshot.authority_sha256,
            hashlib.sha256(snapshot.raw_bytes).hexdigest(),
            asdict(snapshot.identity) if snapshot.identity is not None else None,
            [asdict(item) | {"path": str(item.path)} for item in snapshot.ancestors],
        ]
    )


def _session_binding(session: Session | None) -> str:
    if session is None:
        return digest(None)
    return digest(
        [
            session.source_format.value,
            str(session.source_path),
            session.source_sha256,
            session.session_id,
            str(session.cwd) if session.cwd is not None else None,
            session.started_at,
            session.cli_version,
            session.model,
            session.title,
            session.raw_record_count,
            session.model_provider,
            [asdict(event) for event in session.events],
        ]
    )


def _audit_metadata(audit: NativeAudit) -> str:
    return digest(
        {
            field.name: asdict(value)
            if hasattr(value, "__dataclass_fields__")
            else [asdict(item) if hasattr(item, "__dataclass_fields__") else item for item in value]
            if type(value) is tuple
            else value
            for field in fields(NativeAudit)
            for value in [getattr(audit, field.name)]
        }
    )


def _atoms_binding(atoms: tuple[AuditAtom, ...]) -> str:
    return digest([asdict(atom) for atom in atoms])


def _observation_metadata(observation: CanonicalObservation) -> str:
    return digest(
        [
            id(observation.audit),
            observation.policy.sha256,
            asdict(observation.witness),
            asdict(observation.source_prefix),
            observation.coverage.value,
            observation.fidelity.value,
            asdict(observation.boundary),
            observation.identity,
        ]
    )


def _audit_state(audit: NativeAudit) -> _AuditState:
    if type(audit) is not NativeAudit or audit not in _AUDITS:
        raise NativeAuditError()
    return _AUDITS[audit]


def validate_audit(snapshot: NativeSnapshot, audit: NativeAudit) -> None:
    try:
        state = _audit_state(audit)
        state.policy.__post_init__()
        state.selection.__post_init__()
        if (
            snapshot is not state.snapshot
            or state.policy.sha256 != audit.audit_policy_sha256
            or state.selection.policy_id != audit.selection_policy_id
            or _snapshot_binding(snapshot) != state.snapshot_binding
            or hashlib.sha256(snapshot.raw_bytes).hexdigest() != audit.authority_sha256
            or _audit_metadata(audit) != state.metadata_binding
            or _session_binding(state.original) != state.session_binding
            or _session_binding(state.normalized) != state.normalized_binding
            or _atoms_binding(state.atoms) != state.atoms_binding
        ):
            raise NativeAuditError()
    except (SessionMigrateError, TypeError, ValueError, AttributeError, RecursionError):
        raise NativeAuditError() from None


def validate_observation(observation: CanonicalObservation) -> None:
    try:
        if type(observation) is not CanonicalObservation or observation not in _OBSERVATIONS:
            raise NativeAuditError()
        state = _audit_state(observation.audit)
        validate_audit(state.snapshot, observation.audit)
        if _observation_metadata(observation) != _OBSERVATIONS[observation].metadata_binding:
            raise NativeAuditError()
    except (SessionMigrateError, TypeError, ValueError, AttributeError, RecursionError):
        raise NativeAuditError() from None


def _decode_records(snapshot: NativeSnapshot) -> tuple[JsonlRecord, ...]:
    stream = io.BytesIO(snapshot.raw_bytes)
    records = []
    line_number = 0
    while line := stream.readline(DEFAULT_MAX_RECORD_BYTES + 1):
        line_number += 1
        if len(line) > DEFAULT_MAX_RECORD_BYTES or len(records) > DEFAULT_MAX_RECORDS:
            raise NativeAuditError()
        if not line.strip():
            continue
        value = strict_decode(line)
        if type(value) is not dict or len(records) >= DEFAULT_MAX_RECORDS:
            raise NativeAuditError()
        records.append(JsonlRecord(len(records), line_number, value))
    return tuple(records)


def _normalize(result: AdapterResult, snapshot: NativeSnapshot) -> Session | None:
    if result.session is None:
        return None
    events = []
    taxonomy = result.taxonomy
    records = _decode_records(snapshot) if snapshot.source_format == AgentFormat.CODEX else ()
    raw_opencode = (
        strict_decode(snapshot.raw_bytes)
        if snapshot.source_format == AgentFormat.OPENCODE
        else None
    )
    for event in copy.deepcopy(result.session.events):
        if event.kind is EventKind.TOOL_RESULT:
            blocks = event.payload.get("content_blocks")
            if type(blocks) is list and any(
                type(block) is dict and block.get("type") == "opaque" for block in blocks
            ):
                taxonomy.issue("opaque_result_carrier_loss", event.provenance.record_index)
        if snapshot.source_format == AgentFormat.CODEX and event.kind in {
            EventKind.TOOL_CALL,
            EventKind.TOOL_RESULT,
        }:
            payload = records[event.provenance.record_index].value.get("payload")
            if type(payload) is dict:
                field = "namespace" if event.kind == EventKind.TOOL_CALL else "is_error"
                if field in payload:
                    if field not in event.payload or digest(event.payload[field]) != digest(
                        payload[field]
                    ):
                        taxonomy.issue("parser_semantic_enrichment", event.provenance.record_index)
                    event = replace(event, payload={**event.payload, field: payload[field]})
        if event.kind == EventKind.OPAQUE:
            record_index = event.provenance.record_index
            # Only raw-covered known losses/scope exclusions can remove an opaque carrier.
            if snapshot.source_format != AgentFormat.OPENCODE and (
                record_index in taxonomy.excluded_records or record_index in taxonomy.loss_records
            ):
                continue
            if snapshot.source_format == AgentFormat.OPENCODE and _known_opencode_opaque(
                raw_opencode, event
            ):
                continue
            if (
                snapshot.source_format == AgentFormat.OPENCODE
                and event.payload.get("reason") == "opencode_empty_text"
            ):
                messages = raw_opencode.get("messages", [])
                if record_index < len(messages):
                    value = messages[record_index]
                    if (
                        value.get("type") == "user"
                        and value.get("text") == ""
                        and value.get("files")
                    ):
                        continue
            taxonomy.issue("unrecoverable_opaque", record_index)
        events.append(event)
    return replace(result.session, events=tuple(events))


def _known_opencode_opaque(raw: dict, event: Event) -> bool:
    """Explicit raw-span recovery rules, never removal by a reason prefix."""

    reason = event.payload.get("reason")
    info = raw.get("info", {})
    if reason == "opencode_v2_session_model_variant":
        return type(info.get("model")) is dict and type(info["model"].get("variant")) is str
    messages = raw.get("messages", [])
    index = event.provenance.record_index
    if not 0 <= index < len(messages) or type(messages[index]) is not dict:
        return False
    message = messages[index]
    if reason == "opencode_empty_text":
        if message.get("type") == "user" and message.get("text") == "":
            return True
        if message.get("type") == "assistant":
            parts = message.get("content", [])
            return any(
                type(part) is dict and part.get("type") == "text" and part.get("text") == ""
                for part in parts
                if type(parts) is list
            )
    if reason == "opencode_v2_message_model_variant":
        return type(message.get("model")) is dict and type(message["model"].get("variant")) is str
    if reason == "opencode_v2_file_metadata":
        return any(
            type(item) is dict
            and (type(item.get("name")) is str or type(item.get("description")) is str)
            for item in message.get("files", [])
        )
    parts = message.get("content", [])
    for part in parts if type(parts) is list else []:
        if (
            type(part) is not dict
            or part.get("type") != "tool"
            or type(part.get("state")) is not dict
        ):
            continue
        state = part["state"]
        error = state.get("error")
        if type(error) is dict:
            if (
                reason == "opencode_v2_tool_error_type"
                and type(error.get("type")) is str
                and error["type"] != "ToolError"
            ):
                return True
            if reason == "opencode_v2_tool_error_status" and type(error.get("status")) is int:
                return True
        if (
            reason == "opencode_v2_error_tool_content"
            and state.get("status") == "error"
            and type(state.get("content")) is list
        ):
            return True
        if reason == "opencode_v2_tool_file_metadata":
            return any(
                type(item) is dict and item.get("type") == "file" and type(item.get("name")) is str
                for item in state.get("content", [])
            )
    return False


def audit_snapshot(
    snapshot: NativeSnapshot,
    audit_policy: AuditPolicy | None = None,
    selection: AuditSelection | None = None,
) -> NativeAudit:
    """Decode/classify every raw value, binding selection and parser IR to one capture."""

    audit_policy = AuditPolicy() if audit_policy is None else audit_policy
    selection = AuditSelection() if selection is None else selection
    if type(audit_policy) is not AuditPolicy or type(selection) is not AuditSelection:
        raise NativeAuditError()
    audit_policy.__post_init__()
    selection.__post_init__()
    try:
        binding = _snapshot_binding(snapshot)
        if hashlib.sha256(snapshot.raw_bytes).hexdigest() != snapshot.authority_sha256:
            raise NativeAuditError()
        from session_migrate.audit_adapters import claude, codex, opencode_v2

        if snapshot.source_format == AgentFormat.CLAUDE:
            result = claude.audit(_decode_records(snapshot), snapshot, audit_policy)
        elif snapshot.source_format == AgentFormat.CODEX:
            result = codex.audit(_decode_records(snapshot), snapshot, audit_policy)
        elif snapshot.source_format == AgentFormat.OPENCODE:
            raw = strict_decode(snapshot.raw_bytes)
            if type(raw) is not dict:
                raise NativeAuditError()
            result = opencode_v2.audit(raw, snapshot, audit_policy)
        else:
            raise NativeAuditError()
        normalized = _normalize(result, snapshot)
        taxonomy = result.taxonomy
        blocking_codes = set(taxonomy.issue_counts) - KNOWN_LOSS_ISSUES
        coverage = (
            Coverage.UNKNOWN if taxonomy.counts[UNKNOWN] or blocking_codes else Coverage.VERIFIED
        )
        audit = object.__new__(NativeAudit)
        values = {
            "authority_sha256": snapshot.authority_sha256,
            "audit_policy_id": audit_policy.policy_id,
            "audit_policy_sha256": audit_policy.sha256,
            "selection_policy_id": selection.policy_id,
            "schema_id": result.schema_id,
            "client_id": result.client_id,
            "selection_sha256": digest([selection.policy_id, list(result.selected)]),
            "structure": taxonomy.structure.witness(),
            "classification": taxonomy.classification.witness(),
            "classification_counts": tuple(sorted(taxonomy.counts.items())),
            "raw_atom_count": len(taxonomy.atoms),
            "semantic_atom_count": taxonomy.counts["semantic"],
            "issues": tuple(taxonomy.issues),
            "issue_counts": tuple(sorted(taxonomy.issue_counts.items())),
            "coverage": coverage,
        }
        for name, value in values.items():
            object.__setattr__(audit, name, value)
        atoms = tuple(taxonomy.atoms)
        _AUDITS[audit] = _AuditState(
            snapshot,
            binding,
            audit_policy,
            selection,
            _audit_metadata(audit),
            result.session,
            normalized,
            _session_binding(result.session),
            _session_binding(normalized),
            atoms,
            _atoms_binding(atoms),
        )
        return audit
    except (TypeError, ValueError, AttributeError, RecursionError, UnicodeError):
        raise NativeAuditError() from None
    except SessionMigrateError:
        raise NativeAuditError() from None


def canonicalize_snapshot(
    snapshot: NativeSnapshot,
    audit: NativeAudit,
    canonical_policy: CanonicalPolicy,
) -> CanonicalObservation:
    """Canonicalize only the privately bound parser IR, never caller-supplied events."""

    validate_audit(snapshot, audit)
    state = _audit_state(audit)
    if (
        state.original is None
        or state.normalized is None
        or type(canonical_policy) is not CanonicalPolicy
    ):
        raise NativeAuditError()
    try:
        witness = hash_events(copy.deepcopy(state.normalized.events), canonical_policy)
        prefix = replace(witness.prefix_witness, structure=audit.structure)
        witness = replace(witness, prefix_witness=prefix)
        observation = object.__new__(CanonicalObservation)
        fidelity = (
            Coverage.PARTIAL
            if (
                dict(audit.classification_counts).get(LOSS, 0)
                or set(dict(audit.issue_counts)) & KNOWN_LOSS_ISSUES
                or witness.blocking
            )
            else Coverage.VERIFIED
        )
        for name, value in {
            "audit": audit,
            "policy": canonical_policy,
            "witness": witness,
            "source_prefix": prefix,
            "coverage": audit.coverage,
            "fidelity": fidelity,
            "boundary": prefix.boundary,
            "identity": None,
        }.items():
            object.__setattr__(observation, name, value)
        _OBSERVATIONS[observation] = _ObservationState(_observation_metadata(observation))
        return observation
    except (SessionMigrateError, ValueError, TypeError, RecursionError, AttributeError):
        raise NativeAuditError() from None
