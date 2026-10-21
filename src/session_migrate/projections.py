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
from session_migrate.formats import claude, codex
from session_migrate.lineage import Coverage, PolicyVersions
from session_migrate.model import AgentFormat, Session, TargetFormat
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.projection_policies import (
    ExpectedProjection,
    ProjectionError,
    ProjectionPolicy,
    expected_from_source,
    require,
)

if TYPE_CHECKING:
    from session_migrate.native_audit import CanonicalObservation

_SEAL = object()
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CLIENTS = {
    TargetFormat.CLAUDE: claude.VALIDATED_CLAUDE_VERSIONS,
    TargetFormat.CODEX: codex.VALIDATED_TARGET_VERSIONS,
    TargetFormat.OPENCODE: {"2.0.22", "2.0.23", "2.1.1"},
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
            type(self.source_client) is str and self.source_client in {"2.0.22", "2.0.23", "2.1.1"},
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


def _observe(snapshot: NativeSnapshot, client: str | None, policy: ProjectionPolicy):
    from session_migrate.native_audit import AuditPolicy, audit_snapshot, canonicalize_snapshot

    audit = audit_snapshot(snapshot, audit_policy=AuditPolicy(client_version=client))
    return canonicalize_snapshot(snapshot, audit, policy.canonical_policy)


def _same_semantics(first, second) -> bool:
    return first.units == second.units and first.tool_links == second.tool_links


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
    source_obs = _observe(source_snapshot, versions.source_client, policy)
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
            parsed_source.cli_version in (None, "2.0.22", "2.0.23", "2.1.1"), "context_mismatch"
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
    require(versions.target_adapter == artifact_obs.audit.audit_policy_id, "unsupported_policy")
    require(
        _same_semantics(expected.witness.prefix_witness, artifact_obs.witness.prefix_witness),
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
        and hashlib.sha256(prepared._source.raw_bytes).hexdigest()
        == prepared.source_authority_sha256,
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
    semantic = _same_semantics(
        prepared.expected.witness.prefix_witness, observed.witness.prefix_witness
    )
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
