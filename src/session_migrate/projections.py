"""Private projection preparation and mandatory observation, without native I/O.

An existing F3 artifact is retained verbatim. Certificates prove only this
projection comparison; native identity, turn closure and delta eligibility are
separate authorities and remain unproven in this increment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from session_migrate.canonical import _json_bytes
from session_migrate.conversion import ConversionArtifact, ConversionOptions
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, codex, opencode_v2
from session_migrate.lineage import Coverage, PolicyVersions
from session_migrate.model import AgentFormat, EventKind, Session, TargetFormat
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.projection_policies import (
    _REVIEWED_MEDIA_POLICIES,
    _REVIEWED_POLICIES,
    ExpectedProjection,
    ProjectionError,
    ProjectionPolicy,
    _native_target_order,
    archived_tools_match,
    expected_from_source,
    native_context_carriers_match,
    native_context_order_matches,
    require,
    reviewed_context_order_matches,
    validate_policy_target,
)

if TYPE_CHECKING:
    from session_migrate.native_audit import CanonicalObservation

_SEAL = object()
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CLIENTS = {
    TargetFormat.CLAUDE: claude.VALIDATED_CLAUDE_VERSIONS,
    TargetFormat.CODEX: codex.VALIDATED_TARGET_VERSIONS,
    TargetFormat.OPENCODE: opencode_v2.VALIDATED_VERSIONS,
}


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionContext:
    target_format: TargetFormat
    target_client: str
    profile_fingerprint: str | None = None
    root_fingerprint: str | None = None

    def __post_init__(self) -> None:
        require(
            type(self.target_format) is TargetFormat and self.target_format in _CLIENTS,
            "unsupported_target",
        )
        require(
            type(self.target_client) is str and self.target_client in _CLIENTS[self.target_format],
            "unsupported_target",
        )
        for value in (self.profile_fingerprint, self.root_fingerprint):
            require(value is None or (type(value) is str and bool(_HASH.fullmatch(value))))


@dataclass(frozen=True, slots=True, repr=False)
class SourceVersionContext:
    """Explicit wrapper coordinates; never native identity or export proof."""

    source_format: AgentFormat
    source_client: str
    virtual_source_path: Path
    authority_sha256: str

    def __post_init__(self) -> None:
        require(self.source_format is AgentFormat.OPENCODE, "unsupported_policy")
        require(
            type(self.source_client) is str
            and self.source_client in opencode_v2.VALIDATED_VERSIONS,
            "unsupported_policy",
        )
        require(isinstance(self.virtual_source_path, Path))
        require(type(self.authority_sha256) is str and bool(_HASH.fullmatch(self.authority_sha256)))


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False, init=False, eq=False)
class ProjectionCertificate:
    binding_sha256: str
    relation_sha256: str
    artifact_sha256: str
    _seal: object

    def __init__(self) -> None:
        raise ProjectionError("invalid_certificate")

    @property
    def delta_eligible(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False, init=False, eq=False)
class PreparedProjection:
    artifact_sha256: str
    source_authority_sha256: str
    binding_sha256: str
    expected: ExpectedProjection
    source_coverage: Coverage
    expectation_coverage: Coverage
    certificate: ProjectionCertificate | None
    _seal: object
    _source: NativeSnapshot
    _artifact: ConversionArtifact
    _options: ConversionOptions
    _versions: PolicyVersions
    _context: ProjectionContext
    _policy: ProjectionPolicy
    _source_observation: object
    _artifact_observation: object
    _body: bytes
    _expected_sha256: str
    _source_context: SourceVersionContext | None

    def __init__(self) -> None:
        raise ProjectionError("invalid_certificate")

    @property
    def artifact_bytes(self) -> bytes:
        validate_prepared(self)
        return self._body

    @property
    def source_observation(self) -> CanonicalObservation:
        validate_prepared(self)
        return self._source_observation

    @property
    def artifact_observation(self) -> CanonicalObservation:
        validate_prepared(self)
        return self._artifact_observation

    @property
    def source_context(self) -> SourceVersionContext | None:
        validate_prepared(self)
        return self._source_context


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionVerification:
    semantic_matches: bool
    structure_matches: bool
    relation_complete: bool
    identity_proven: bool
    boundary_proven: bool
    source_coverage: Coverage
    expectation_coverage: Coverage
    observed_coverage: Coverage
    preserves_content: bool
    loss_counts: tuple[tuple[str, int], ...]
    metadata_loss_counts: tuple[tuple[str, int], ...]
    reason: str

    @property
    def delta_eligible(self) -> bool:
        return False


_PREPARED: WeakKeyDictionary[PreparedProjection, str] = WeakKeyDictionary()
_CERTIFICATES: WeakKeyDictionary[ProjectionCertificate, tuple[str, str, str]] = WeakKeyDictionary()


def _sha(value: object) -> str:
    return hashlib.sha256(_json_bytes(_metadata_json(value))).hexdigest()


def _metadata_json(value: object) -> object:
    """Encode typed metadata only; event business payloads are validated first."""
    if isinstance(value, StrEnum):
        return value.value
    if type(value) in (list, tuple):
        return [_metadata_json(item) for item in value]
    if type(value) is dict:
        return {key: _metadata_json(item) for key, item in value.items()}
    return value


def _session_sha(session: Session) -> str:
    require(type(session) is Session)
    for event in session.events:
        _json_bytes(event.payload)
    return _sha(
        {
            "format": session.source_format.value,
            "path": str(session.source_path),
            "authority": session.source_sha256,
            "session_id": session.session_id,
            "cwd": str(session.cwd) if session.cwd is not None else None,
            "started_at": session.started_at,
            "client": session.cli_version,
            "model": session.model,
            "provider": session.model_provider,
            "title": session.title,
            "records": session.raw_record_count,
            **(
                {"reviewed_report": session.reviewed_report}
                if session.reviewed_report is not None
                else {}
            ),
            "events": [
                {
                    "kind": e.kind.value,
                    "role": e.role.value if e.role else None,
                    "timestamp": e.timestamp,
                    "text": e.text,
                    "tool_name": e.tool_name,
                    "tool_call_id": e.tool_call_id,
                    "payload": e.payload,
                    "provenance": asdict(e.provenance),
                }
                for e in session.events
            ],
        }
    )


def _options_dict(options: ConversionOptions) -> dict:
    require(type(options) is ConversionOptions)
    return {
        "target": options.target_format.value,
        "session_id": options.session_id,
        "cwd": str(options.cwd) if options.cwd is not None else None,
        "client": options.target_cli_version,
        "provider": options.model_provider,
        "model": options.model,
        "title": options.target_title,
        "title_policy": options.title_policy.value if options.title_policy else None,
        "target_parent_id": options.target_parent_id,
        **({"reviewed_portability": True} if options.reviewed_portability else {}),
    }


def _artifact_dict(artifact: ConversionArtifact) -> dict:
    require(type(artifact) is ConversionArtifact and type(artifact.native_bytes) is bytes)
    return {
        "source_ir": _session_sha(artifact.source),
        "target": artifact.target_format.value,
        "session_id": artifact.session_id,
        "cwd": str(artifact.cwd),
        "client": artifact.target_cli_version,
        "timestamp": artifact.timestamp,
        "body": hashlib.sha256(artifact.native_bytes).hexdigest(),
        "records": artifact.native_record_count,
        "title_plan": asdict(artifact.title_plan) if artifact.title_plan else None,
        **(
            {"reviewed_archive": artifact.reviewed_archive}
            if artifact.reviewed_archive is not None
            else {}
        ),
    }


def _observation_dict(observation) -> dict:
    return {
        "authority": observation.audit.authority_sha256,
        "selection": observation.audit.selection_sha256,
        "structure": asdict(observation.audit.structure),
        "classification": asdict(observation.audit.classification),
        "audit_policy": observation.audit.audit_policy_sha256,
        "policy": observation.policy.sha256,
        "semantic": asdict(observation.witness.prefix_witness),
        "coverage": observation.coverage.value,
    }


def _expectation_sha(expected: ExpectedProjection) -> str:
    for event in expected.events:
        _json_bytes(event.payload)
    return _sha(
        {
            "policy": expected.policy.sha256,
            "prefix": asdict(expected.witness.prefix_witness),
            "issues": list(expected.witness.issue_counts),
            "relation": asdict(expected.relation),
            "members": [asdict(m) for m in expected.members],
            **(
                {"generated_members": [asdict(m) for m in expected.generated_members]}
                if expected.generated_members
                else {}
            ),
            **(
                {
                    "native_context_hashes": expected.native_context_hashes,
                    "native_context_only": expected.native_context_only,
                }
                if expected.native_context_hashes
                else {}
            ),
            **(
                {
                    "reviewed_source_order": expected.reviewed_source_order,
                    "reviewed_target_order": expected.reviewed_target_order,
                }
                if expected.policy.policy_id in _REVIEWED_POLICIES
                else {}
            ),
            **(
                {"reviewed_media_results": expected.reviewed_media_results}
                if expected.policy.policy_id in _REVIEWED_MEDIA_POLICIES
                else {}
            ),
            "losses": list(expected.loss_counts),
            "metadata_losses": list(expected.metadata_loss_counts),
            "relation_complete": expected.relation_complete,
            "preserves_content": expected.preserves_content,
            "events": _sha(
                [
                    {
                        "kind": e.kind.value,
                        "role": e.role.value if e.role else None,
                        "text": e.text,
                        "tool_name": e.tool_name,
                        "tool_id": e.tool_call_id,
                        "payload": e.payload,
                        "provenance": asdict(e.provenance),
                    }
                    for e in expected.events
                ]
            ),
        }
    )


def _binding(
    source,
    artifact,
    options,
    versions,
    context,
    policy,
    source_obs,
    artifact_obs,
    expected_sha,
    source_context,
) -> str:
    return _sha(
        {
            "source_bytes": hashlib.sha256(source.raw_bytes).hexdigest(),
            "source_storage": source.storage_binding,
            "source_path": str(source.source_path),
            "source_format": source.source_format.value,
            "source_provenance": source.provenance.value,
            "source_identity": asdict(source.identity) if source.identity else None,
            "source_ancestry": [
                {"path": str(a.path), "device": a.device, "inode": a.inode}
                for a in source.ancestors
            ],
            "artifact": _artifact_dict(artifact),
            "options": _options_dict(options),
            "versions": asdict(versions),
            "context": asdict(context),
            "policy": policy.sha256,
            "source_observation": _observation_dict(source_obs),
            "artifact_observation": _observation_dict(artifact_obs),
            "expectation": expected_sha,
            "source_runtime_coordinates": {
                "format": source_context.source_format.value,
                "client": source_context.source_client,
                "virtual_path": str(source_context.virtual_source_path),
                "authority": source_context.authority_sha256,
            }
            if source_context is not None
            else None,
        }
    )


def _observe(
    snapshot: NativeSnapshot,
    client: str | None,
    policy: ProjectionPolicy,
    *,
    reviewed_portability: bool = False,
):
    from session_migrate.native_audit import (
        AuditPolicy,
        AuditSelection,
        audit_snapshot,
        canonicalize_snapshot,
    )

    require(type(reviewed_portability) is bool, "unsupported_policy")
    if reviewed_portability:
        require(policy.policy_id in _REVIEWED_POLICIES, "unsupported_policy")
    audit = audit_snapshot(
        snapshot,
        audit_policy=AuditPolicy(client_version=client),
        selection=AuditSelection(
            "claude309-reviewed-inline-png-v1"
            if policy.policy_id in _REVIEWED_MEDIA_POLICIES
            else "claude309-reviewed-conversation-v1"
        )
        if reviewed_portability
        else AuditSelection(),
    )
    return canonicalize_snapshot(snapshot, audit, policy.canonical_policy)


def _same_semantics(first, second) -> bool:
    return first.units == second.units and first.tool_links == second.tool_links


def _target_conversation_witness(observation, snapshot, options, policy):
    """Separate an explicitly bound organizational parent from conversation units.

    The original audit/observation remains intact, including its loss coverage.
    Only the exact target parent option permits this one metadata unit to be
    excluded from conversation comparison; it cannot establish a fork boundary.
    """
    if options.target_parent_id is None:
        return observation.witness.prefix_witness
    from session_migrate.canonical import hash_events
    from session_migrate.formats import opencode
    from session_migrate.model import EventKind

    require(
        options.target_format is TargetFormat.OPENCODE
        and options.target_cli_version == "2.2.5"
        and snapshot.source_format is AgentFormat.OPENCODE,
        "options_mismatch",
    )
    require(
        opencode.parse_import_bytes(snapshot.raw_bytes).parent_session == options.target_parent_id,
        "target_parent_mismatch",
    )
    # Use the audit's normalized projection, retaining its independent raw
    # accounting for known losses such as the native runtime busy clock.
    events = observation.events
    conversation = tuple(
        event
        for event in events
        if not (
            event.kind is EventKind.OPAQUE
            and event.payload == {"reason": "opencode_parent_session"}
            and event.provenance.record_type == "session"
        )
    )
    original_parent_count = sum(
        event.kind is EventKind.OPAQUE
        and event.payload == {"reason": "opencode_parent_session"}
        and event.provenance.record_type == "session"
        for event in observation.session.events
    )
    require(
        original_parent_count == 1 and len(events) - len(conversation) <= 1,
        "target_parent_mismatch",
    )
    return hash_events(conversation, policy.canonical_policy).prefix_witness


def _expectation_coverage(source_obs, artifact_obs, expected) -> Coverage:
    if source_obs.coverage is Coverage.UNKNOWN or artifact_obs.coverage is Coverage.UNKNOWN:
        return Coverage.UNKNOWN
    if (
        source_obs.coverage is Coverage.PARTIAL
        or artifact_obs.coverage is Coverage.PARTIAL
        or source_obs.fidelity is not Coverage.VERIFIED
        or artifact_obs.fidelity is not Coverage.VERIFIED
        or expected.loss_counts
        or expected.witness.blocking
    ):
        return Coverage.PARTIAL
    return Coverage.VERIFIED


def prepare_projection(
    source_snapshot: NativeSnapshot,
    artifact: ConversionArtifact,
    *,
    options: ConversionOptions,
    versions: PolicyVersions,
    context: ProjectionContext,
    policy: ProjectionPolicy | None = None,
    source_context: SourceVersionContext | None = None,
) -> PreparedProjection:
    """Bind an existing artifact, computing the independent expectation first."""
    require(type(source_snapshot) is NativeSnapshot)
    policy = policy if policy is not None else ProjectionPolicy()
    require(type(policy) is ProjectionPolicy, "unsupported_policy")
    require(type(context) is ProjectionContext and type(versions) is PolicyVersions)
    context.__post_init__()
    versions.__post_init__()
    policy.__post_init__()
    validate_policy_target(policy, context.target_format, context.target_client)
    require(type(artifact) is ConversionArtifact)
    require(artifact.target_format is context.target_format, "context_mismatch")
    require(artifact.target_cli_version == context.target_client, "context_mismatch")
    require(options.target_format is artifact.target_format, "options_mismatch")
    if options.target_title is not None:
        require(
            artifact.title_plan is not None
            and artifact.title_plan.requested == options.target_title,
            "options_mismatch",
        )
        require(
            options.title_policy is None or options.title_policy is artifact.title_plan.policy,
            "options_mismatch",
        )
    require(
        options.target_cli_version is None or options.target_cli_version == context.target_client,
        "options_mismatch",
    )
    if options.cwd is not None:
        require(Path(options.cwd).absolute() == artifact.cwd, "options_mismatch")
    if options.session_id is not None:
        wanted_id = options.session_id
        if artifact.target_format is TargetFormat.OPENCODE:
            import uuid

            wanted_id = "ses_" + uuid.UUID(wanted_id).hex
        require(wanted_id == artifact.session_id, "options_mismatch")
    require(
        versions.canonical_algorithm == policy.canonical_policy.algorithm
        and versions.projection_policy == policy.policy_id,
        "unsupported_policy",
    )
    require(versions.target_client == context.target_client, "unsupported_policy")
    reviewed = policy.policy_id in _REVIEWED_POLICIES
    require(
        type(options.reviewed_portability) is bool and options.reviewed_portability is reviewed,
        "options_mismatch",
    )
    if reviewed:
        require(
            source_snapshot.source_format is AgentFormat.CLAUDE
            and versions.source_client == "2.1.309"
            and context.target_format is TargetFormat.OPENCODE
            and context.target_client == "2.2.5",
            "unsupported_policy",
        )
    source_obs = _observe(
        source_snapshot,
        versions.source_client,
        policy,
        reviewed_portability=reviewed,
    )
    require(versions.source_adapter == source_obs.audit.audit_policy_id, "unsupported_policy")
    parsed_source = source_obs.session
    if source_context is not None:
        require(type(source_context) is SourceVersionContext, "context_mismatch")
        source_context.__post_init__()
        require(
            source_snapshot.source_format is AgentFormat.OPENCODE
            and source_obs.audit.schema_id == "opencode-session-transfer-v2"
            and source_context.source_client
            == versions.source_client
            == source_obs.audit.client_id,
            "context_mismatch",
        )
        require(
            source_context.authority_sha256 == source_snapshot.authority_sha256
            and source_context.virtual_source_path
            == source_snapshot.source_path
            == Path(f"opencode:{parsed_source.session_id}"),
            "context_mismatch",
        )
        require(
            parsed_source.cli_version is None
            or parsed_source.cli_version in opencode_v2.VALIDATED_VERSIONS,
            "context_mismatch",
        )
        parsed_source = replace(
            parsed_source,
            source_path=source_context.virtual_source_path,
            cli_version=source_context.source_client,
        )
    require(_session_sha(parsed_source) == _session_sha(artifact.source), "source_mismatch")
    require(artifact.source.source_sha256 == source_snapshot.authority_sha256, "source_mismatch")
    expected = expected_from_source(source_obs, target_format=artifact.target_format, policy=policy)
    artifact_snapshot = NativeSnapshot.from_bytes(
        artifact.native_bytes,
        context=SourceCaptureContext(
            AgentFormat(artifact.target_format.value), Path("/projection/prepared-artifact")
        ),
    )
    artifact_obs = _observe(artifact_snapshot, context.target_client, policy)
    require(archived_tools_match(expected, artifact_snapshot), "semantic_mismatch")
    require(native_context_carriers_match(expected, artifact_snapshot), "semantic_mismatch")
    require(reviewed_context_order_matches(expected, artifact_snapshot), "semantic_mismatch")
    require(
        native_context_order_matches(source_obs, expected, artifact_snapshot), "semantic_mismatch"
    )
    require(versions.target_adapter == artifact_obs.audit.audit_policy_id, "unsupported_policy")
    require(
        _same_semantics(
            expected.witness.prefix_witness,
            _target_conversation_witness(artifact_obs, artifact_snapshot, options, policy),
        ),
        "semantic_mismatch",
    )
    require(
        artifact_obs.session.session_id == artifact.session_id
        and artifact_obs.session.cwd == artifact.cwd,
        "artifact_mismatch",
    )
    if (
        options.model is not None
        and artifact.target_format
        in {
            TargetFormat.CLAUDE,
            TargetFormat.OPENCODE,
        }
        and any(e.role is not None and e.role.value == "assistant" for e in expected.events)
    ):
        require(artifact_obs.session.model == options.model, "options_mismatch")
    native_title = source_obs.session.title
    if artifact.target_format is TargetFormat.CODEX:
        native_title = None
    elif artifact.target_format is TargetFormat.OPENCODE:
        native_title = artifact.effective_target_title or "Imported session"
    require(artifact_obs.session.title == native_title, "artifact_mismatch")
    if artifact.target_format in {TargetFormat.CODEX, TargetFormat.OPENCODE}:
        provider = options.model_provider or (
            "openai"
            if artifact.target_format is TargetFormat.CODEX
            else source_obs.session.model_provider or "openai"
        )
        require(artifact_obs.session.model_provider == provider, "options_mismatch")
    expected_sha = _expectation_sha(expected)
    binding = _binding(
        source_snapshot,
        artifact,
        options,
        versions,
        context,
        policy,
        source_obs,
        artifact_obs,
        expected_sha,
        source_context,
    )
    expectation_coverage = _expectation_coverage(source_obs, artifact_obs, expected)
    certificate = None
    if expectation_coverage is Coverage.VERIFIED and expected.relation_complete:
        certificate = object.__new__(ProjectionCertificate)
        for name, value in {
            "binding_sha256": binding,
            "relation_sha256": expected.relation.chain_sha256,
            "artifact_sha256": artifact_snapshot.authority_sha256,
            "_seal": _SEAL,
        }.items():
            object.__setattr__(certificate, name, value)
        _CERTIFICATES[certificate] = (
            binding,
            expected.relation.chain_sha256,
            artifact_snapshot.authority_sha256,
        )
    prepared = object.__new__(PreparedProjection)
    for name, value in {
        "artifact_sha256": artifact_snapshot.authority_sha256,
        "source_authority_sha256": source_snapshot.authority_sha256,
        "binding_sha256": binding,
        "expected": expected,
        "source_coverage": source_obs.coverage,
        "expectation_coverage": expectation_coverage,
        "certificate": certificate,
        "_seal": _SEAL,
        "_source": source_snapshot,
        "_artifact": artifact,
        "_options": options,
        "_versions": versions,
        "_context": context,
        "_policy": policy,
        "_source_observation": source_obs,
        "_artifact_observation": artifact_obs,
        "_body": bytes(artifact.native_bytes),
        "_expected_sha256": expected_sha,
        "_source_context": source_context,
    }.items():
        object.__setattr__(prepared, name, value)
    _PREPARED[prepared] = binding
    return prepared


def validate_prepared(prepared: PreparedProjection) -> None:
    """Recheck payloads and all live inputs; no native or filesystem I/O occurs."""
    from session_migrate.native_audit import validate_observation

    require(
        type(prepared) is PreparedProjection and getattr(prepared, "_seal", None) is _SEAL,
        "invalid_certificate",
    )
    require(
        prepared in _PREPARED and _PREPARED[prepared] == prepared.binding_sha256,
        "invalid_certificate",
    )
    validate_observation(prepared._source_observation)
    validate_observation(prepared._artifact_observation)
    prepared._context.__post_init__()
    prepared._policy.__post_init__()
    prepared._versions.__post_init__()
    if prepared._source_context is not None:
        prepared._source_context.__post_init__()
    expected_sha = _expectation_sha(prepared.expected)
    require(expected_sha == prepared._expected_sha256, "binding_changed")
    actual = _binding(
        prepared._source,
        prepared._artifact,
        prepared._options,
        prepared._versions,
        prepared._context,
        prepared._policy,
        prepared._source_observation,
        prepared._artifact_observation,
        expected_sha,
        prepared._source_context,
    )
    require(actual == prepared.binding_sha256, "binding_changed")
    require(
        hashlib.sha256(prepared._body).hexdigest() == prepared.artifact_sha256
        and prepared._source.integrity_valid()
        and prepared._source.authority_sha256 == prepared.source_authority_sha256,
        "binding_changed",
    )
    require(prepared.source_coverage is prepared._source_observation.coverage, "binding_changed")
    require(
        prepared.expectation_coverage
        is _expectation_coverage(
            prepared._source_observation, prepared._artifact_observation, prepared.expected
        ),
        "binding_changed",
    )
    certificate = prepared.certificate
    if certificate is not None:
        require(
            type(certificate) is ProjectionCertificate
            and getattr(certificate, "_seal", None) is _SEAL,
            "invalid_certificate",
        )
        require(
            certificate in _CERTIFICATES
            and _CERTIFICATES[certificate]
            == (
                certificate.binding_sha256,
                certificate.relation_sha256,
                certificate.artifact_sha256,
            ),
            "invalid_certificate",
        )
        require(
            certificate.binding_sha256 == actual
            and certificate.artifact_sha256 == prepared.artifact_sha256
            and certificate.relation_sha256 == prepared.expected.relation.chain_sha256,
            "invalid_certificate",
        )


def verify_projection(
    prepared: PreparedProjection,
    observed_snapshot: NativeSnapshot,
    *,
    context: ProjectionContext,
) -> ProjectionVerification:
    """Observe and compare after publication; never import or repair a target."""
    validate_prepared(prepared)
    require(type(context) is ProjectionContext)
    context.__post_init__()
    require(_sha(asdict(context)) == _sha(asdict(prepared._context)), "context_mismatch")
    require(
        type(observed_snapshot) is NativeSnapshot
        and observed_snapshot.source_format.value == context.target_format.value,
        "context_mismatch",
    )
    observed = _observe(observed_snapshot, context.target_client, prepared._policy)
    validate_policy_target(prepared._policy, context.target_format, context.target_client)
    semantic = (
        archived_tools_match(prepared.expected, observed_snapshot)
        and reviewed_context_order_matches(prepared.expected, observed_snapshot)
        and native_context_carriers_match(prepared.expected, observed_snapshot)
        and native_context_order_matches(
            prepared._source_observation, prepared.expected, observed_snapshot
        )
        and _same_semantics(
            prepared.expected.witness.prefix_witness,
            _target_conversation_witness(
                observed, observed_snapshot, prepared._options, prepared._policy
            ),
        )
    )
    if prepared._policy.policy_id in _REVIEWED_POLICIES:
        from session_migrate.audit_adapters import strict_decode

        archived = {
            prepared.expected._events[m.call_target_index].tool_call_id
            for m in prepared.expected.generated_members
        }
        try:
            semantic = semantic and (
                _native_target_order(
                    strict_decode(observed_snapshot.raw_bytes),
                    archived,
                    inline_png=prepared._policy.policy_id in _REVIEWED_MEDIA_POLICIES,
                )
                == _native_target_order(
                    strict_decode(prepared._body),
                    archived,
                    inline_png=prepared._policy.policy_id in _REVIEWED_MEDIA_POLICIES,
                )
            )
        except (SessionMigrateError, ValueError, TypeError, KeyError, AttributeError):
            semantic = False
    metadata = observed.session.session_id == prepared._artifact.session_id and (
        observed.session.cwd == prepared._artifact.cwd
    )
    structure = observed.audit.structure == prepared._artifact_observation.audit.structure
    if not metadata:
        reason = "target_identity_mismatch"
    elif not semantic:
        reason = "semantic_mismatch"
    elif not structure:
        reason = "structure_mismatch"
    elif observed.coverage is Coverage.UNKNOWN or prepared.expectation_coverage is Coverage.UNKNOWN:
        reason = "audit_unproven"
    elif prepared.expected.loss_counts:
        reason = "losses_declared"
    elif observed.coverage is Coverage.PARTIAL or prepared.expectation_coverage is Coverage.PARTIAL:
        reason = "audit_partial"
    else:
        reason = "projection_matches"
    return ProjectionVerification(
        semantic and metadata,
        structure and metadata,
        prepared.expected.relation_complete,
        False,
        False,
        prepared.source_coverage,
        prepared.expectation_coverage,
        observed.coverage,
        semantic
        and metadata
        and structure
        and prepared.expected.preserves_content
        and observed.coverage is Coverage.VERIFIED
        and observed.fidelity is Coverage.VERIFIED
        and prepared._source_observation.fidelity is Coverage.VERIFIED
        and prepared.expectation_coverage is Coverage.VERIFIED,
        prepared.expected.loss_counts,
        prepared.expected.metadata_loss_counts,
        reason,
    )


def reviewed_portability_report(prepared: PreparedProjection) -> dict | None:
    """Return the bound, content-free review of these exact prepared bytes."""
    import copy
    from collections import defaultdict, deque

    validate_prepared(prepared)
    if prepared._policy.policy_id not in _REVIEWED_POLICIES:
        return None
    report = copy.deepcopy(prepared._source_observation.session.reviewed_report)
    require(type(report) is dict, "source_mismatch")
    source = prepared.expected.reviewed_source_order
    target = prepared.expected.reviewed_target_order
    positions = defaultdict(deque)
    for index, unit in enumerate(target):
        positions[unit].append(index)
    mapping = [positions[unit].popleft() if positions[unit] else None for unit in source]
    report["order"] = {
        "policy": "reviewed-stock-225-effective-order-v1",
        "source_displayed_sha256": _sha(source),
        "target_effective_sha256": _sha(target),
        "changed": source != target,
        "source_units": len(source),
        "target_units": len(target),
        "source_to_target": mapping,
        "grouped_source_units": sum(index is None for index in mapping),
    }
    losses = dict(prepared.expected.loss_counts)
    for code in (
        "reviewed_tool_reference_as_data",
        "reviewed_context_order_changed",
        "reviewed_tool_error_envelope",
    ):
        if losses.get(code):
            indices = sorted(
                {
                    event.provenance.record_index
                    for event in prepared._source_observation.events
                    if (
                        code == "reviewed_tool_reference_as_data"
                        and any(
                            type(block) is dict and block.get("type") == "tool_reference"
                            for block in event.payload.get("content_blocks", [])
                        )
                    )
                    or (
                        code == "reviewed_tool_error_envelope"
                        and event.kind is EventKind.TOOL_RESULT
                        and event.payload.get("is_error") is True
                    )
                }
            )
            report.setdefault("differences", []).append(
                {
                    "code": code,
                    "count": losses[code],
                    "record_indices": indices,
                    **(
                        {
                            "description": (
                                "Stock OpenCode 2.2.5 serializes a failed tool result as JSON DATA "
                                "containing its ToolError and content fields. The original error "
                                "message and failure status remain preserved inside that envelope; "
                                "the provider receives this JSON instead of bare error text."
                            )
                        }
                        if code == "reviewed_tool_error_envelope"
                        else {}
                    ),
                }
            )
    if prepared._policy.policy_id in _REVIEWED_MEDIA_POLICIES:
        report["native_image_result_blocks"] = [
            {
                "call_reference_sha256": _sha(call_id),
                "ordered_blocks": [{"kind": kind, "sha256": digest} for kind, digest in blocks],
            }
            for call_id, blocks in prepared.expected.reviewed_media_results
        ]
        report["image_transport_binding_sha256"] = _sha(prepared.expected.reviewed_media_results)
    report["projection_policy"] = prepared._policy.policy_id
    return report
