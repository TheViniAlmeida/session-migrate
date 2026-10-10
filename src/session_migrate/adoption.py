"""Explicit adoption of a freshly proved pair; append-only current baselines.

No historical operation, native acknowledgement, import receipt, closure or
update permission is synthesized by adoption.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic_ns
from weakref import ReferenceType, WeakKeyDictionary
from weakref import ref as weak_ref

from session_migrate.audit_adapters import EXCLUDED
from session_migrate.baselines import (
    BaselineRecordV1,
    BaselineStore,
    _encode_baseline,
    _receipt,
    open_baseline_coordinator_for_commit,
)
from session_migrate.continuity import ExplicitPair
from session_migrate.continuity_observation import (
    _OBSERVATIONS,
    EndpointObserver,
    validate_endpoint,
    validate_observer,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import (
    Coverage,
    EvidenceReference,
    LineageError,
    LineageStore,
    NativeState,
    PolicyVersions,
    QualifiedIdentity,
    _encode,
    _identifier,
)
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_namespaces import ensure_migration_state_home
from session_migrate.operations import _checkpoint, _effective_source_coverage
from session_migrate.projection_policies import ProjectionPolicy, expected_from_source
from session_migrate.projections import _same_semantics
from session_migrate.reconciliation import (
    _READ_EFFECTS,
    _endpoint_fingerprint,
    _observe_pair,
    _path,
    _sha,
    _stable,
)
from session_migrate.witnesses import BoundaryKind

_MAX_HANDLES = 64
_REASONS = {
    "ready",
    "invalid_request",
    "invalid_handle",
    "handle_capacity",
    "proof_unavailable",
    "observer_changed",
    "identity_unproven",
    "endpoint_unavailable",
    "unsupported_endpoint",
    "policy_changed",
    "observation_unproven",
    "observation_changed",
    "coverage_unverified",
    "relation_unproven",
    "structure_unproven",
    "source_tools_unproven",
    "semantic_mismatch",
    "coordinator_changed",
    "baseline_changed",
    "baseline_conflict",
    "persistence_uncertain",
}


class AdoptionError(SessionMigrateError):
    def __init__(self, code, *, baseline_id=None):
        self.code = code if code in _REASONS else "proof_unavailable"
        self.baseline_id = baseline_id
        super().__init__(f"Adoption: {self.code}.")


def _require(condition, code="proof_unavailable"):
    if not condition:
        raise AdoptionError(code)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class AdoptionHandle:
    _binding: str

    def __init__(self):
        raise AdoptionError("invalid_handle")

    def discard(self):
        _HANDLES.pop(self, None)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class AdoptionProposalV1:
    schema_version: int
    baseline_id: str
    provenance: str
    status: str
    reason: str
    source_identity: QualifiedIdentity | None
    target_identity: QualifiedIdentity | None
    source_root_sha256: str
    target_root_sha256: str
    source_profile_binding_kind: str | None
    target_profile_binding_kind: str | None
    versions: PolicyVersions
    canonical_policy_sha256: str
    projection_policy_sha256: str
    relation_sha256: str | None
    comparison_sha256: str | None
    source_coverage: Coverage | None
    target_coverage: Coverage | None
    source_fidelity: Coverage | None
    target_fidelity: Coverage | None
    boundary: BoundaryKind
    native_state: NativeState
    metadata_loss_counts: tuple[tuple[str, int], ...]
    native_read_effects: tuple[str, ...]
    available_effects: tuple[str, ...]
    blockers: tuple[str, ...]
    handle: AdoptionHandle | None = field(default=None, compare=False)

    def __post_init__(self):
        _require(
            type(self.schema_version) is int and self.schema_version == 1,
            "invalid_request",
        )
        _identifier(self.baseline_id)
        _require(
            self.provenance == "adopted_now"
            and self.status in {"ready", "blocked"}
            and self.reason in _REASONS,
            "invalid_request",
        )
        for value in (self.source_identity, self.target_identity):
            _require(value is None or type(value) is QualifiedIdentity, "invalid_request")
        for value in (
            self.source_root_sha256,
            self.target_root_sha256,
            self.canonical_policy_sha256,
            self.projection_policy_sha256,
        ):
            _require(
                type(value) is str
                and len(value) == 64
                and all(c in "0123456789abcdef" for c in value),
                "invalid_request",
            )
        for value in (self.relation_sha256, self.comparison_sha256):
            _require(
                value is None
                or (
                    type(value) is str
                    and len(value) == 64
                    and all(c in "0123456789abcdef" for c in value)
                ),
                "invalid_request",
            )
        for value in (
            self.source_profile_binding_kind,
            self.target_profile_binding_kind,
        ):
            _require(
                value is None or value in {"profile_v1", "legacy_runtime_v1"},
                "invalid_request",
            )
        for value in (
            self.source_coverage,
            self.target_coverage,
            self.source_fidelity,
            self.target_fidelity,
        ):
            _require(value is None or type(value) is Coverage, "invalid_request")
        _require(
            type(self.versions) is PolicyVersions
            and self.boundary is BoundaryKind.UNPROVEN
            and self.native_state is NativeState.NATIVE_NOT_STARTED,
            "invalid_request",
        )
        for values in (self.native_read_effects, self.available_effects, self.blockers):
            _require(
                type(values) is tuple and all(type(v) is str for v in values),
                "invalid_request",
            )
        _require(
            set(self.native_read_effects) <= _READ_EFFECTS and set(self.blockers) <= _REASONS,
            "invalid_request",
        )
        _require(
            set(self.available_effects)
            <= {"append_baseline", "bootstrap_empty_coordinator", "inspect"},
            "invalid_request",
        )
        _require(
            type(self.metadata_loss_counts) is tuple
            and all(
                type(row) is tuple
                and len(row) == 2
                and type(row[0]) is str
                and type(row[1]) is int
                and row[1] >= 0
                for row in self.metadata_loss_counts
            ),
            "invalid_request",
        )
        _require(
            self.handle is None or type(self.handle) is AdoptionHandle,
            "invalid_request",
        )

    def to_dict(self):
        return {
            f.name: json.loads(_encode(getattr(self, f.name)))
            for f in fields(self)
            if f.name != "handle"
        }


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class _State:
    pair: ExplicitPair
    observer: EndpointObserver
    state_home: Path
    policy: ProjectionPolicy
    proposal: AdoptionProposalV1
    coordinator_uuid: str | None
    baseline_view_sha: str
    created_at_utc: str
    source_checkpoint: object
    target_checkpoint: object
    references: tuple[EvidenceReference, ...]
    endpoint_fingerprints: tuple[str, str]
    public_proposal: ReferenceType[AdoptionProposalV1] | None = None


_HANDLES: WeakKeyDictionary[AdoptionHandle, _State] = WeakKeyDictionary()


def _binding(state):
    _require(state.proposal.handle is None, "invalid_handle")
    refs = [
        (
            _sha(
                (
                    ref.format.value,
                    str(ref.root),
                    ref.native_id,
                    ref.profile_id,
                    ref.profile_fingerprint,
                    ref.catalog_id,
                )
            )
        )
        for ref in (state.pair.source, state.pair.target)
    ]
    return _sha(
        (
            refs,
            id(state.observer),
            str(state.state_home),
            state.policy.sha256,
            state.proposal.to_dict(),
            state.coordinator_uuid,
            state.baseline_view_sha,
            state.created_at_utc,
            state.source_checkpoint,
            state.target_checkpoint,
            state.references,
            state.endpoint_fingerprints,
            id(state.public_proposal),
        )
    )


def _issue(state):
    _require(len(_HANDLES) < _MAX_HANDLES, "handle_capacity")
    public = state.proposal
    state = replace(state, proposal=replace(public, handle=None), public_proposal=weak_ref(public))
    handle = object.__new__(AdoptionHandle)
    object.__setattr__(handle, "_binding", _binding(state))
    _HANDLES[handle] = state
    return handle


def _check_handle_state(handle, state):
    """Keep capability lifetime independent from its public metadata DTO."""
    try:
        _require(type(state.public_proposal) is ReferenceType, "invalid_handle")
        _require(handle._binding == _binding(state), "invalid_handle")
        public = state.public_proposal()
        if public is not None:
            _require(type(public) is AdoptionProposalV1, "invalid_handle")
            public.__post_init__()
            _require(
                public.handle is handle and public.to_dict() == state.proposal.to_dict(),
                "invalid_handle",
            )
    except (SessionMigrateError, ValueError, TypeError, AttributeError):
        raise AdoptionError("invalid_handle") from None


def _proof(pair, observer, versions, policy, effects):
    started = monotonic_ns()
    validate_observer(observer)
    pair.__post_init__()
    refs = (pair.source, pair.target)
    _require(
        all(ref.format in {AgentFormat.CLAUDE, AgentFormat.CODEX} for ref in refs),
        "structure_unproven",
    )
    clients = (versions.source_client, versions.target_client)
    first = _observe_pair(observer, refs, clients, policy.canonical_policy, effects)
    second = _observe_pair(observer, refs, clients, policy.canonical_policy, effects)
    _stable(first, second)
    source, target = second
    for endpoint in second:
        obs = endpoint.observation
        _require(
            obs.coverage is Coverage.VERIFIED
            and obs.fidelity is Coverage.VERIFIED
            and not obs.witness.blocking,
            "coverage_unverified",
        )
        _require(obs.source_prefix.structure is not None, "structure_unproven")
        _require(
            not dict(obs.audit.issue_counts).get("selected_scope_exclusion", 0)
            and (
                endpoint.identity.namespace.format != AgentFormat.CLAUDE.value
                or not dict(obs.audit.classification_counts).get(EXCLUDED, 0)
            ),
            "structure_unproven",
        )
        _require(obs.boundary.kind is BoundaryKind.UNPROVEN, "proof_unavailable")
    expected = expected_from_source(
        source.observation,
        target_format=TargetFormat(pair.target.format.value),
        policy=policy,
    )
    _require(
        expected.relation_complete
        and expected.preserves_content
        and not expected.loss_counts
        and not expected.witness.blocking,
        "relation_unproven",
    )
    _require(
        _same_semantics(expected.witness.prefix_witness, target.observation.source_prefix),
        "semantic_mismatch",
    )
    if pair.source.format is pair.target.format:
        _require(
            source.observation.audit.structure == target.observation.audit.structure,
            "structure_unproven",
        )
    for ref, endpoint in zip(refs, second, strict=True):
        if pair.source.format is not pair.target.format or ref.format is AgentFormat.CODEX:
            validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=started)
            snapshot = _OBSERVATIONS[endpoint].snapshot
            validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=started)
            _require(_linear_native_graph(snapshot), "structure_unproven")
    # Raw adapters proved each complete graph independently. The projection
    # relation includes every source event; units and tools bind its full target.
    _require(
        expected.relation.count > 0 or source.observation.source_prefix.units.count == 0,
        "relation_unproven",
    )
    comparison = _sha(
        (
            "adoption-comparison-v1",
            source.authority_sha256,
            target.authority_sha256,
            expected.relation.chain_sha256,
            expected.witness.prefix_witness.units,
            expected.witness.prefix_witness.tool_links,
            source.observation.source_prefix.structure,
            target.observation.source_prefix.structure,
            policy.sha256,
            versions,
        )
    )
    source_cp = _checkpoint(
        source.observation,
        source.identity,
        source.authority_sha256,
        versions,
        _effective_source_coverage(source.observation),
    )
    target_cp = _checkpoint(
        target.observation,
        target.identity,
        target.authority_sha256,
        versions,
        _effective_source_coverage(target.observation),
    )
    references = []
    for ref, endpoint in zip(refs, second, strict=True):
        validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=started)
        snapshot_path = _OBSERVATIONS[endpoint].snapshot.source_path
        validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=started)
        references.append(
            EvidenceReference(
                "native",
                "verified",
                "available",
                str(snapshot_path),
                endpoint.authority_sha256,
            )
        )
    return second, expected, comparison, source_cp, target_cp, tuple(references)


def _linear_native_graph(snapshot):
    """Prove the bounded MVP graph from the already audited immutable capture.

    No target serializer or caller graph is used. More complex native graphs
    require an independent structural relation issuer before they can be adopted.
    """
    if snapshot.source_format not in {AgentFormat.CLAUDE, AgentFormat.CODEX}:
        return False
    rows = tuple(record.value for record in snapshot.decoded_records())
    if snapshot.source_format is AgentFormat.CODEX:
        metadata = [row for row in rows if row.get("type") == "session_meta"]
        return (
            len(metadata) == 1
            and rows[0] is metadata[0]
            and metadata[0].get("payload", {}).get("history_mode") == "legacy"
            and all(
                row.get("type") in {"session_meta", "response_item", "event_msg", "turn_context"}
                for row in rows
            )
            and _codex_duplicate_pairing(rows)
        )
    messages = [row for row in rows if row.get("type") in {"user", "assistant"}]
    previous = None
    for row in messages:
        if row.get("parentUuid") != previous or row.get("logicalParentUuid") is not None:
            return False
        previous = row.get("uuid")
    return bool(messages) and all(
        row.get("type") in {"user", "assistant", "custom-title", "ai-title", "last-prompt"}
        for row in rows
    )


def _codex_duplicate_pairing(rows):
    """Independently bind duplicate event carriers in physical order, not counts."""
    events, responses = [], []
    for row in rows:
        payload = row.get("payload", {})
        kind = payload.get("type")
        if row.get("type") == "event_msg" and kind in {"user_message", "agent_message"}:
            events.append(
                (
                    "user" if kind == "user_message" else "assistant",
                    payload.get("message"),
                )
            )
        elif row.get("type") == "response_item" and kind == "message":
            blocks = payload.get("content", ())
            for block in blocks:
                if block.get("type") in {"input_text", "output_text", "text"}:
                    responses.append((payload.get("role"), block.get("text")))
                elif events:
                    return False
    return not events or events == responses


def prepare_adoption(
    pair,
    observer,
    *,
    policy=None,
    state_home,
    source_client_version=None,
    target_client_version=None,
):
    _require(
        type(pair) is ExplicitPair and type(observer) is EndpointObserver,
        "invalid_request",
    )
    _path(state_home)
    policy = policy if policy is not None else ProjectionPolicy()
    _require(type(policy) is ProjectionPolicy, "invalid_request")
    policy.__post_init__()
    versions = PolicyVersions(
        policy.canonical_policy.algorithm,
        policy.policy_id,
        "native-audit-v1",
        "native-audit-v1",
        source_client_version,
        target_client_version,
    )
    baseline_id = uuid.uuid4().hex
    created = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    effects = set()
    proof = None
    coordinator = baseline = None

    def result(status, reason):
        endpoints = proof[0] if proof else (None, None)
        expected = proof[1] if proof else None
        proposal = AdoptionProposalV1(
            1,
            baseline_id,
            "adopted_now",
            status,
            reason,
            *(e.identity if e else None for e in endpoints),
            _sha(str(pair.source.root)),
            _sha(str(pair.target.root)),
            *(e.profile_binding_kind.value if e else None for e in endpoints),
            versions,
            policy.canonical_policy.sha256,
            policy.sha256,
            expected.relation.chain_sha256 if expected else None,
            proof[2] if proof else None,
            *(e.observation.coverage if e else None for e in endpoints),
            *(e.observation.fidelity if e else None for e in endpoints),
            BoundaryKind.UNPROVEN,
            NativeState.NATIVE_NOT_STARTED,
            expected.metadata_loss_counts if expected else (),
            tuple(sorted(effects)),
            (
                ("append_baseline",)
                + (("bootstrap_empty_coordinator",) if coordinator is None else ())
            )
            if status == "ready"
            else ("inspect",),
            () if status == "ready" else (reason,),
        )
        if status == "ready":
            state = _State(
                pair,
                observer,
                state_home,
                policy,
                proposal,
                coordinator.store_uuid if coordinator else None,
                _sha(baseline),
                created,
                proof[3],
                proof[4],
                proof[5],
                tuple(_endpoint_fingerprint(e) for e in endpoints),
            )
            object.__setattr__(proposal, "handle", _issue(state))
        return proposal

    try:
        coordinator = LineageStore.inspect_existing(state_home / "lineage-v1")
        baseline = BaselineStore.inspect_existing(
            state_home / "baselines-v1",
            coordinator_store_uuid=coordinator.store_uuid if coordinator else None,
        )
        _require(coordinator is not None or baseline is None, "coordinator_changed")
        proof = _proof(pair, observer, versions, policy, effects)
        latest = LineageStore.inspect_existing(state_home / "lineage-v1")
        _require(
            (latest.store_uuid if latest else None)
            == (coordinator.store_uuid if coordinator else None),
            "coordinator_changed",
        )
        latest_baseline = BaselineStore.inspect_existing(
            state_home / "baselines-v1",
            coordinator_store_uuid=latest.store_uuid if latest else None,
        )
        _require(_sha(latest_baseline) == _sha(baseline), "baseline_changed")
        return result("ready", "ready")
    except (
        SessionMigrateError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
    ) as error:
        effects.update(getattr(error, "native_read_effects", ()))
        reason = getattr(error, "code", "proof_unavailable")
        return result("blocked", reason if reason in _REASONS else "proof_unavailable")


def _record(state, coordinator_uuid):
    return BaselineRecordV1(
        1,
        "baseline",
        "adopted_now",
        state.proposal.baseline_id,
        state.created_at_utc,
        coordinator_uuid,
        state.proposal.source_profile_binding_kind,
        state.proposal.target_profile_binding_kind,
        state.source_checkpoint,
        state.target_checkpoint,
        state.proposal.versions,
        state.proposal.canonical_policy_sha256,
        state.proposal.projection_policy_sha256,
        state.proposal.relation_sha256,
        state.proposal.comparison_sha256,
        state.references,
    )


def _recheck(state):
    proof = _proof(state.pair, state.observer, state.proposal.versions, state.policy, set())
    _require(
        tuple(_endpoint_fingerprint(e) for e in proof[0]) == state.endpoint_fingerprints
        and proof[2] == state.proposal.comparison_sha256
        and proof[3] == state.source_checkpoint
        and proof[4] == state.target_checkpoint
        and proof[5] == state.references,
        "observation_changed",
    )


def commit_adoption(handle):
    _require(type(handle) is AdoptionHandle and handle in _HANDLES, "invalid_handle")
    state = _HANDLES.pop(handle)
    _check_handle_state(handle, state)
    published = False
    coordinator = baseline = None

    def fresh_guard():
        try:
            _check_handle_state(handle, state)
            _recheck(state)
            _check_handle_state(handle, state)
        except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError):
            raise LineageError("proof_mismatch") from None

    try:
        _recheck(state)
        before = LineageStore.inspect_existing(state.state_home / "lineage-v1")
        _require(
            (before.store_uuid if before else None) == state.coordinator_uuid,
            "coordinator_changed",
        )
        # The coordinator foundation creates only its leaf. Create missing
        # state-home ancestors through the existing nofollow/private FD walker,
        # exclusively after the explicit commit and fresh native proof.
        home_existed = state.state_home.exists()
        ensure_migration_state_home(state.state_home)
        published = not home_existed
        _recheck(state)
        with open_baseline_coordinator_for_commit(
            state.state_home / "lineage-v1", external_guard=fresh_guard
        ) as coordinator:
            published = before is None  # Bootstrap itself is a durable commit effect.
            if before is not None:
                _require(
                    coordinator._store_uuid == state.coordinator_uuid,
                    "coordinator_changed",
                )
            else:
                _require(not coordinator.recovery_view().operations, "coordinator_changed")
            source, target = (
                state.source_checkpoint.identity,
                state.target_checkpoint.identity,
            )
            with coordinator.lock_operation(
                state.proposal.baseline_id,
                target.namespace,
                additional_namespaces=(source.namespace,),
            ) as guard:
                _recheck(state)
                current = BaselineStore.inspect_existing(
                    state.state_home / "baselines-v1",
                    coordinator_store_uuid=coordinator._store_uuid,
                )
                _require(_sha(current) == state.baseline_view_sha, "baseline_changed")
                _recheck(state)
                record = _record(state, coordinator._store_uuid)
                _encode_baseline(record)  # Bound/preflight everything before sidecar creation.
                if current is None:
                    baseline = BaselineStore.create_new(
                        state.state_home / "baselines-v1",
                        coordinator=coordinator,
                        guard=guard,
                        external_guard=fresh_guard,
                    )
                    published = True
                else:
                    baseline = BaselineStore.open_existing(
                        state.state_home / "baselines-v1",
                        coordinator=coordinator,
                        guard=guard,
                        external_guard=fresh_guard,
                    )
                with baseline:
                    _recheck(state)
                    receipt = baseline.append(
                        record, coordinator=coordinator, guard=guard, external_guard=fresh_guard
                    )
                    published = True
                    _recheck(state)
                    guard.check()
                    _check_handle_state(handle, state)
                    return receipt
    except (
        SessionMigrateError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
    ) as error:
        code = (
            "persistence_uncertain"
            if published
            or (baseline is not None and baseline._guarded_persist_renamed)
            or (coordinator is not None and coordinator._guarded_persist_renamed)
            else getattr(error, "code", "proof_unavailable")
        )
        raise AdoptionError(code, baseline_id=state.proposal.baseline_id) from None


def inspect_adoption(baseline_id, *, state_home):
    """Resolve an uncertain commit using its original ID, without another import."""
    _identifier(baseline_id)
    _path(state_home)
    coordinator = LineageStore.inspect_existing(state_home / "lineage-v1")
    view = BaselineStore.inspect_existing(
        state_home / "baselines-v1",
        coordinator_store_uuid=coordinator.store_uuid if coordinator else None,
    )
    if view is None:
        return None
    _require(coordinator is not None, "coordinator_changed")
    records = [r for r in view.baselines if r.baseline_id == baseline_id]
    return _receipt(records[0]) if records else None
