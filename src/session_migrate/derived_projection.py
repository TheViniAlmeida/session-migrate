"""Private USER-handoff projection authority, distinct from full-history proof.

The expectation comes from accepted USER input. Native snapshots below audit
genuine emitted target artifacts and actual observations, never a USER document
masquerading as native source history. Wire verification DTOs are metadata only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from weakref import WeakKeyDictionary

from session_migrate.audit_adapters import LOSS, UNKNOWN, digest, strict_decode
from session_migrate.canonical import CanonicalPolicy, _json_bytes, hash_events
from session_migrate.conversion import EmitContext, PortableEmission
from session_migrate.errors import SessionMigrateError
from session_migrate.handoff_types import (
    HANDOFF_POLICY,
    DerivedUserHandoffInput,
    OriginalNativeAuthority,
    validate_derived_input,
)
from session_migrate.lineage import (
    Coverage,
    DerivedVerificationV1,
    PolicyVersions,
    ProjectionExpectation,
    TransformationV1,
    _encode,
)
from session_migrate.model import AgentFormat, EventKind, Role, TargetFormat
from session_migrate.native_audit import (
    AuditPolicy,
    CanonicalObservation,
    audit_snapshot,
    canonicalize_snapshot,
    validate_observation,
)
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext

_SEAL = object()
_MAX_HANDLES = 64


class DerivedProjectionError(SessionMigrateError):
    def __init__(self, code="invalid_certificate"):
        self.code = (
            code
            if code
            in {
                "invalid_certificate",
                "binding_changed",
                "source_changed",
                "input_changed",
                "context_mismatch",
                "semantic_mismatch",
                "structure_mismatch",
                "audit_unproven",
                "metadata_mismatch",
                "handle_capacity",
            }
            else "invalid_certificate"
        )
        super().__init__(f"Derived projection: {self.code}.")


def _require(condition, code="invalid_certificate"):
    if not condition:
        raise DerivedProjectionError(code)


def _digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _transformation_sha(transformation):
    _require(type(transformation) is TransformationV1)
    transformation.__post_init__()
    return hashlib.sha256(_encode(transformation)).hexdigest()


def checkpoint_snapshot_sha(observation, versions):
    validate_observation(observation)
    return hashlib.sha256(
        b"session-migrate/checkpoint-prefix/v2\0"
        + _json_bytes(
            {
                "prefix": json.loads(_encode(observation.source_prefix)),
                "versions": asdict(versions),
                "policy_sha256": observation.policy.sha256,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class PreparedDerivedProjection:
    artifact_sha256: str
    source_authority_sha256: str
    expectation: ProjectionExpectation
    versions: PolicyVersions
    _authority: OriginalNativeAuthority
    _input: DerivedUserHandoffInput
    _transformation: TransformationV1
    _context: EmitContext
    _source_observation: CanonicalObservation
    _artifact_observation: CanonicalObservation
    _artifact_snapshot: NativeSnapshot
    _user_units: object
    _user_tools: object
    _seal: object

    def __init__(self):
        raise DerivedProjectionError()

    @property
    def source_observation(self):
        validate_derived_projection(self)
        return self._source_observation

    @property
    def artifact_observation(self):
        validate_derived_projection(self)
        return self._artifact_observation


_PROJECTIONS: WeakKeyDictionary = WeakKeyDictionary()


def _binding(prepared):
    return _digest(
        {
            "source_identity": json.loads(_encode(prepared._authority.identity)),
            "source_sha": prepared.source_authority_sha256,
            "source_capture": id(prepared._authority.snapshot),
            "source_observation": id(prepared._source_observation),
            "input": json.loads(_encode(prepared._input.identity)),
            "transformation": _transformation_sha(prepared._transformation),
            "context": {
                **asdict(prepared._context),
                "cwd": str(prepared._context.cwd),
                "target_format": prepared._context.target_format.value,
            },
            "artifact_sha": prepared.artifact_sha256,
            "artifact_capture": id(prepared._artifact_snapshot),
            "artifact_observation": id(prepared._artifact_observation),
            "expectation": json.loads(_encode(prepared.expectation)),
            "versions": asdict(prepared.versions),
            "user_units": json.loads(_encode(prepared._user_units)),
            "user_tools": json.loads(_encode(prepared._user_tools)),
        }
    )


def _schema_time(timestamp, target_format):
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    return (
        parsed.replace(microsecond=parsed.microsecond // 1000 * 1000)
        if target_format is TargetFormat.OPENCODE
        else parsed
    )


def _op_user_scope(context):
    return context.target_format is TargetFormat.OPENCODE and context.target_cli_version in {
        "2.0.22",
        "2.0.23",
    }


def _op_user_metadata(snapshot, context, observed, *, imported):
    # Decode the same captured RAW that the private audit observed. Session's
    # legacy projection alone cannot prove these session-level metadata fields.
    raw = strict_decode(snapshot.raw_bytes)
    _require(
        observed.audit.schema_id == "opencode-session-transfer-v2"
        and observed.audit.client_id == context.target_cli_version
        and type(raw) is dict,
        "audit_unproven",
    )
    info = raw.get("info")
    _require(type(info) is dict, "metadata_mismatch")
    model = info.get("model")
    location = info.get("location")
    _require(
        type(model) is dict
        and type(model.get("id")) is str
        and type(model.get("providerID")) is str
        and model["id"] == context.model
        and model["providerID"] == context.model_provider
        and type(location) is dict
        and type(location.get("directory")) is str
        and Path(location["directory"]).resolve() == context.cwd.resolve()
        and info.get("id") == context.session_id,
        "metadata_mismatch",
    )
    messages = raw.get("messages")
    _require(
        type(messages) is list
        and all(type(message) is dict and message.get("type") == "user" for message in messages),
        "semantic_mismatch",
    )
    if imported:
        losses = tuple(atom for atom in observed.atoms if atom.category == LOSS)
        _require(
            observed.fidelity is Coverage.PARTIAL
            and type(model.get("variant")) is str
            and len(losses) == 1
            and losses[0].record_index == 0
            and losses[0].path == ("info", "model", "variant")
            and losses[0].sha256 == digest(model["variant"]),
            "audit_unproven",
        )


def _observe(snapshot, context, *, imported=False):
    _require(type(snapshot) is NativeSnapshot, "context_mismatch")
    _require(snapshot.source_format.value == context.target_format.value, "context_mismatch")
    audit = audit_snapshot(snapshot, AuditPolicy(client_version=context.target_cli_version))
    observed = canonicalize_snapshot(snapshot, audit, CanonicalPolicy())
    _require(
        observed.coverage is Coverage.VERIFIED
        and not observed.witness.blocking
        and not observed.audit.issue_counts
        and not any(atom.category == UNKNOWN for atom in observed.atoms),
        "audit_unproven",
    )
    if _op_user_scope(context):
        _op_user_metadata(snapshot, context, observed, imported=imported)
    if not (imported and _op_user_scope(context)):
        _require(observed.fidelity is Coverage.VERIFIED, "audit_unproven")
    session = observed.session
    expected_time = _schema_time(context.timestamp, context.target_format)
    _require(type(session.started_at) is str, "metadata_mismatch")
    actual_time = _schema_time(session.started_at, context.target_format)
    _require(
        session.session_id == context.session_id
        and session.cwd == context.cwd.resolve()
        and actual_time == expected_time,
        "metadata_mismatch",
    )
    if context.target_format is not TargetFormat.OPENCODE:
        _require(session.cli_version == context.target_cli_version, "metadata_mismatch")
    if context.target_format is TargetFormat.CODEX:
        _require(
            session.model_provider == context.model_provider
            and session.model is None
            and session.title is None,
            "metadata_mismatch",
        )
    elif context.target_format is TargetFormat.CLAUDE:
        _require(
            session.title == context.target_title
            and session.model is None
            and session.model_provider == "anthropic",
            "metadata_mismatch",
        )
    else:
        _require(
            session.model_provider == context.model_provider and session.model == context.model,
            "metadata_mismatch",
        )
        _require(session.title == (context.target_title or "Imported session"), "metadata_mismatch")
    _require(
        all(
            event.kind is EventKind.MESSAGE
            and event.role is Role.USER
            and event.tool_name is None
            and event.tool_call_id is None
            and event.payload
            == ({"synthetic": False} if context.target_format is TargetFormat.OPENCODE else {})
            for event in observed.events
        ),
        "semantic_mismatch",
    )
    return observed


def _expected_user_events(derived_input, context):
    # OpenCode's closed USER schema carries the explicit default synthetic flag.
    # This rule depends only on accepted USER input and the target schema; no
    # native bytes, parser result or emitter output can choose its semantics.
    return (
        tuple(replace(event, payload={"synthetic": False}) for event in derived_input.events)
        if context.target_format is TargetFormat.OPENCODE
        else derived_input.events
    )


def prepare_derived_projection(
    authority, derived_input, transformation, emission, *, artifact_path
):
    """Issue a private expectation after independently auditing genuine B1 output."""
    _require(type(authority) is OriginalNativeAuthority)
    authority.check()
    validate_derived_input(derived_input)
    _require(
        type(transformation) is TransformationV1 and transformation.input == derived_input.identity,
        "input_changed",
    )
    transformation.__post_init__()
    _require(
        type(emission) is PortableEmission and type(emission.context) is EmitContext,
        "context_mismatch",
    )
    context = emission.context
    context.__post_init__()
    _require(
        isinstance(artifact_path, Path)
        and artifact_path.is_absolute()
        and ".." not in artifact_path.parts,
        "context_mismatch",
    )
    _require(
        type(emission.native_bytes) is bytes
        and emission.native_bytes
        and hashlib.sha256(emission.native_bytes).hexdigest() == emission.target_sha256,
        "binding_changed",
    )
    # This snapshot is the emitted native TARGET artifact. It confers no native
    # source identity and is never supplied as OriginalNativeAuthority.
    artifact = NativeSnapshot.from_bytes(
        emission.native_bytes,
        context=SourceCaptureContext(AgentFormat(context.target_format.value), artifact_path),
    )
    observed = _observe(artifact, context)
    _require(emission.native_record_count == observed.session.raw_record_count, "metadata_mismatch")
    expected = hash_events(_expected_user_events(derived_input, context), CanonicalPolicy())
    _require(
        not expected.blocking
        and expected.prefix_witness.units == observed.source_prefix.units
        and expected.prefix_witness.tool_links == observed.source_prefix.tool_links,
        "semantic_mismatch",
    )
    versions = PolicyVersions(
        CanonicalPolicy().algorithm,
        HANDOFF_POLICY,
        "native-audit-v1",
        "native-audit-v1",
        authority.versions.source_client,
        context.target_cli_version,
    )
    # Structure is borrowed from genuine native output only after USER semantics
    # and raw accounting/metadata have independently passed. It is not an oracle
    # for the content expectation.
    expectation = ProjectionExpectation(
        checkpoint_snapshot_sha(observed, versions),
        (),
        # OP's imported variant is unproved session metadata, not USER semantic
        # fidelity. Choose the conservative checkpoint scope before import;
        # completion must still match the actual effective coverage exactly.
        Coverage.PARTIAL if _op_user_scope(context) else Coverage.VERIFIED,
        observed.source_prefix,
        _digest(
            {
                "policy": HANDOFF_POLICY,
                "input_event": derived_input.identity.event_sha256,
                "units": json.loads(_encode(expected.prefix_witness.units)),
                "tools": json.loads(_encode(expected.prefix_witness.tool_links)),
                "target_format": context.target_format.value,
            }
        ),
    )
    _require(len(_PROJECTIONS) < _MAX_HANDLES, "handle_capacity")
    prepared = object.__new__(PreparedDerivedProjection)
    for name, value in {
        "artifact_sha256": artifact.authority_sha256,
        "source_authority_sha256": authority.authority_sha256,
        "expectation": expectation,
        "versions": versions,
        "_authority": authority,
        "_input": derived_input,
        "_transformation": transformation,
        "_context": context,
        "_source_observation": authority.observation,
        "_artifact_observation": observed,
        "_artifact_snapshot": artifact,
        "_user_units": expected.prefix_witness.units,
        "_user_tools": expected.prefix_witness.tool_links,
        "_seal": _SEAL,
    }.items():
        object.__setattr__(prepared, name, value)
    _PROJECTIONS[prepared] = _binding(prepared)
    validate_derived_projection(prepared)
    prepared._authority.check()
    return prepared


def validate_derived_projection(prepared):
    _require(
        type(prepared) is PreparedDerivedProjection
        and getattr(prepared, "_seal", None) is _SEAL
        and prepared in _PROJECTIONS
    )
    validate_derived_input(prepared._input)
    _require(prepared._transformation.input == prepared._input.identity, "input_changed")
    validate_observation(prepared._source_observation)
    validate_observation(prepared._artifact_observation)
    _require(
        prepared._authority.observation is prepared._source_observation
        and prepared._authority.authority_sha256 == prepared.source_authority_sha256,
        "source_changed",
    )
    _require(
        hashlib.sha256(prepared._artifact_snapshot.raw_bytes).hexdigest()
        == prepared.artifact_sha256
        and _binding(prepared) == _PROJECTIONS[prepared],
        "binding_changed",
    )


def verify_derived_projection(prepared, observed_snapshot):
    """Return wire metadata only after private issuance and current RAW proof."""
    validate_derived_projection(prepared)
    prepared._authority.check()
    observed = _observe(observed_snapshot, prepared._context, imported=True)
    _require(
        observed.source_prefix.units == prepared._user_units
        and observed.source_prefix.tool_links == prepared._user_tools,
        "semantic_mismatch",
    )
    _require(
        observed.audit.structure == prepared._artifact_observation.audit.structure,
        "structure_mismatch",
    )
    _require(
        observed.source_prefix == prepared.expectation.prefix_witness
        and checkpoint_snapshot_sha(observed, prepared.versions)
        == prepared.expectation.snapshot_sha256,
        "structure_mismatch",
    )
    proof = DerivedVerificationV1(
        1,
        _transformation_sha(prepared._transformation),
        prepared._input.identity.event_sha256,
        prepared.expectation.snapshot_sha256,
        observed_snapshot.authority_sha256,
        HANDOFF_POLICY,
    )
    return proof, observed
