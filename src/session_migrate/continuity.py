"""Readonly continuity assessment on separately observed source/target axes.

No ledger creation, baseline, publication, update or recovery transition occurs.
The delta status is reserved for real adapter closure/structure proofs; current
native observations leave turn closure unproven and cannot authorize a delta.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic_ns

from session_migrate.canonical import (
    CanonicalPolicy,
    PrefixComparison,
    _json_bytes,
    compare_prefix,
    iter_canonical_units,
)
from session_migrate.continuity_bindings import ProfileBindingKind
from session_migrate.continuity_observation import (
    ContinuityObservationError,
    EndpointObserver,
    effective_profile_fingerprint,
    observe_endpoint,
    ref_for_identity,
    resolve_profile,
    validate_endpoint,
    validate_observer,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import (
    CompletionReceipt,
    Coverage,
    DerivationBindingV1,
    LineageStore,
    OperationState,
    PolicyVersions,
    QualifiedIdentity,
    RecoveryOperation,
    RecoveryView,
    ReturnLinkV1,
    _encode,
)
from session_migrate.model import TargetFormat
from session_migrate.profiles import SessionRef
from session_migrate.projection_policies import expected_from_source
from session_migrate.witnesses import BoundaryKind, PrefixWitness

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ContinuityError(SessionMigrateError):
    def __init__(self) -> None:
        super().__init__("Invalid continuity selection or metadata.")


class ContinuityStatus(StrEnum):
    NO_OP = "no_op"
    DELTA_CANDIDATE = "delta_candidate"
    CONFLICT = "conflict"
    LINEAGE_BROKEN = "lineage_broken"
    UNTRACKED = "untracked"
    NEEDS_RECONCILE = "needs_reconcile"


@dataclass(frozen=True, slots=True, repr=False)
class ExplicitPair:
    source: SessionRef
    target: SessionRef

    def __post_init__(self) -> None:
        if type(self.source) is not SessionRef or type(self.target) is not SessionRef:
            raise ContinuityError()
        for ref in (self.source, self.target):
            if (
                type(ref.profile_id) is not str
                or not _ID.fullmatch(ref.profile_id)
                or (
                    type(ref.profile_fingerprint) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", ref.profile_fingerprint)
                )
            ):
                raise ContinuityError()


@dataclass(frozen=True, slots=True, repr=False)
class ContinuitySelector:
    operation_id: str | None = None
    pair: ExplicitPair | None = None

    def __post_init__(self) -> None:
        if (self.operation_id is None) == (self.pair is None):
            raise ContinuityError()
        if self.operation_id is not None and (
            type(self.operation_id) is not str or not _ID.fullmatch(self.operation_id)
        ):
            raise ContinuityError()
        if self.pair is not None and type(self.pair) is not ExplicitPair:
            raise ContinuityError()
        if self.pair is not None:
            self.pair.__post_init__()


@dataclass(frozen=True, slots=True, repr=False)
class LineageInspectionV1:
    schema_version: int
    operation: RecoveryOperation | None
    candidate_ids: tuple[str, ...]
    candidate_count: int
    store_fingerprint: str | None
    reason: str


@dataclass(frozen=True, slots=True, repr=False)
class ContinuityAssessmentV1:
    schema_version: int
    status: ContinuityStatus
    reason: str
    operation_id: str | None
    source_comparison: PrefixComparison | None
    target_comparison: PrefixComparison | None
    source_structure_matches: bool | None
    target_structure_matches: bool | None
    relation_matches: bool | None
    source_authority_sha256: str | None
    target_authority_sha256: str | None
    blockers: tuple[str, ...]
    actions: tuple[str, ...]
    native_read_effects: tuple[str, ...]
    observed_start_ns: int | None
    observed_end_ns: int | None
    source_identity: QualifiedIdentity | None
    target_identity: QualifiedIdentity | None
    source_checkpoint_sha256: str | None
    target_checkpoint_sha256: str | None
    receipt_sha256: str | None
    versions: PolicyVersions | None
    canonical_policy_sha256: str | None
    source_coverage: Coverage | None
    target_coverage: Coverage | None
    source_fidelity: Coverage | None
    target_fidelity: Coverage | None
    source_profile_binding_kind: ProfileBindingKind | None
    target_profile_binding_kind: ProfileBindingKind | None
    source_current_profile_fingerprint: str | None
    target_current_profile_fingerprint: str | None

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.status) is not ContinuityStatus
        ):
            raise ContinuityError()
        for value in (self.source_identity, self.target_identity):
            if value is not None and type(value) is not QualifiedIdentity:
                raise ContinuityError()
        for value in (
            self.source_authority_sha256,
            self.target_authority_sha256,
            self.source_checkpoint_sha256,
            self.target_checkpoint_sha256,
            self.receipt_sha256,
            self.canonical_policy_sha256,
            self.source_current_profile_fingerprint,
            self.target_current_profile_fingerprint,
        ):
            if value is not None and (
                type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise ContinuityError()
        for value in (
            self.source_profile_binding_kind,
            self.target_profile_binding_kind,
        ):
            if value is not None and type(value) is not ProfileBindingKind:
                raise ContinuityError()
        if self.versions is not None and type(self.versions) is not PolicyVersions:
            raise ContinuityError()
        if self.versions is not None:
            self.versions.__post_init__()
        for value in (
            self.source_coverage,
            self.target_coverage,
            self.source_fidelity,
            self.target_fidelity,
        ):
            if value is not None and type(value) is not Coverage:
                raise ContinuityError()
        for values in (self.blockers, self.actions, self.native_read_effects):
            if (
                type(values) is not tuple
                or len(values) > 1024
                or any(type(v) is not str for v in values)
            ):
                raise ContinuityError()
            if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) for value in values):
                raise ContinuityError()
        if type(self.reason) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.reason):
            raise ContinuityError()
        if self.operation_id is not None and (
            type(self.operation_id) is not str or not _ID.fullmatch(self.operation_id)
        ):
            raise ContinuityError()
        for value in (self.observed_start_ns, self.observed_end_ns):
            if value is not None and (type(value) is not int or value < 0):
                raise ContinuityError()
        for value in (self.source_comparison, self.target_comparison):
            if value is not None and type(value) is not PrefixComparison:
                raise ContinuityError()
        for value in (
            self.source_structure_matches,
            self.target_structure_matches,
            self.relation_matches,
        ):
            if value is not None and type(value) is not bool:
                raise ContinuityError()

    @property
    def update_available(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, repr=False)
class ContinuityAssessmentV2:
    schema_version: int
    baseline: ContinuityAssessmentV1
    derivation: DerivationBindingV1
    return_link: ReturnLinkV1 | None

    def __post_init__(self):
        if (
            type(self.schema_version) is not int
            or self.schema_version != 2
            or type(self.baseline) is not ContinuityAssessmentV1
            or type(self.derivation) is not DerivationBindingV1
            or (self.return_link is not None and type(self.return_link) is not ReturnLinkV1)
        ):
            raise ContinuityError()
        self.baseline.__post_init__()
        self.derivation.__post_init__()
        if self.return_link is not None:
            self.return_link.__post_init__()

    @property
    def update_available(self):
        return False


def _sha(record) -> str | None:
    return hashlib.sha256(_encode(record)).hexdigest() if record is not None else None


def _view_fingerprint(view: RecoveryView) -> str:
    body = {
        "store": view.store_uuid,
        "operations": [
            {
                "record": _sha(item.record),
                "state": item.state.value,
                "durable": item.durable_state.value,
                "native": item.native_state.value,
                "durable_native": item.durable_native_state.value,
                "reserved": item.reserved_bytes,
                "receipt": _sha(item.receipt),
                "source": _sha(item.source_checkpoint),
                "target": _sha(item.target_checkpoint),
            }
            for item in view.operations
        ],
        "edges": [_sha(edge) for edge in view.edges],
    }
    return hashlib.sha256(_json_bytes(body)).hexdigest()


def _matches_ref(ref: SessionRef, identity, observer: EndpointObserver | None = None) -> bool:
    return identity is not None and (
        ref.format.value == identity.namespace.format
        and str(ref.root) == identity.namespace.canonical_root
        and ref.native_id == identity.namespace.native_id
        and (
            effective_profile_fingerprint(observer, ref)
            if observer is not None
            else ref.profile_fingerprint
        )
        == identity.profile_fingerprint
    )


def _select(
    selector: ContinuitySelector,
    view: RecoveryView | None,
    observer: EndpointObserver | None = None,
) -> LineageInspectionV1:
    if view is None:
        return LineageInspectionV1(1, None, (), 0, None, "ledger_missing")
    if selector.operation_id is not None:
        candidates = [
            op for op in view.operations if op.record.operation_id == selector.operation_id
        ]
    else:
        candidates = [
            op
            for op in view.operations
            if _matches_ref(selector.pair.source, op.record.source.identity, observer)
            and (
                _matches_ref(selector.pair.target, op.target_checkpoint.identity, observer)
                if op.target_checkpoint
                else (
                    selector.pair.target.format.value == op.record.target_namespace.format
                    and str(selector.pair.target.root) == op.record.target_namespace.canonical_root
                    and selector.pair.target.native_id == op.record.target_namespace.native_id
                    and (
                        (
                            effective_profile_fingerprint(observer, selector.pair.target)
                            if observer is not None
                            else selector.pair.target.profile_fingerprint
                        )
                        == op.record.target_profile_fingerprint
                    )
                )
            )
        ]
    ids = tuple(op.record.operation_id for op in candidates)
    return LineageInspectionV1(
        1,
        candidates[0] if len(candidates) == 1 else None,
        ids[:1024],
        len(ids),
        _view_fingerprint(view),
        "selected" if len(ids) == 1 else "ambiguous_baseline" if ids else "operation_not_found",
    )


def inspect_lineage(
    selector: ContinuitySelector,
    store_path: Path,
    *,
    observer: EndpointObserver | None = None,
) -> LineageInspectionV1:
    if type(selector) is not ContinuitySelector or not isinstance(store_path, Path):
        raise ContinuityError()
    selector.__post_init__()
    failure = _pair_profile_failure(selector, observer)
    if failure:
        return LineageInspectionV1(1, None, (), 0, None, failure)
    return _select(selector, LineageStore.inspect_existing(store_path), observer)


def _pair_profile_failure(
    selector: ContinuitySelector, observer: EndpointObserver | None
) -> str | None:
    if selector.pair is None:
        return None
    if observer is None:
        return "profile_context_required"
    try:
        for ref in (selector.pair.source, selector.pair.target):
            profile = resolve_profile(observer, ref)
            if profile.id != ref.profile_id or profile.fingerprint != ref.profile_fingerprint:
                return "profile_binding_unproven"
            effective_profile_fingerprint(observer, ref)
    except ContinuityObservationError:
        return "profile_binding_unproven"
    return None


def _snapshot_sha(prefix: PrefixWitness, versions: PolicyVersions, policy: CanonicalPolicy) -> str:
    value = {
        "prefix": json.loads(_encode(prefix)),
        "versions": asdict(versions),
        "policy_sha256": policy.sha256,
    }
    return hashlib.sha256(
        b"session-migrate/checkpoint-prefix/v2\0" + _json_bytes(value)
    ).hexdigest()


def _baseline_reason(
    op: RecoveryOperation, view: RecoveryView, policy: CanonicalPolicy
) -> str | None:
    receipt, source, target, record = (
        op.receipt,
        op.source_checkpoint,
        op.target_checkpoint,
        op.record,
    )
    if type(receipt) is not CompletionReceipt or source is None or target is None:
        return "checkpoint_missing"
    if (
        receipt.operation_id != record.operation_id
        or receipt.input_artifact_sha256 != record.target_input_sha256
        or receipt.source_checkpoint_sha256 != _sha(source)
        or receipt.target_checkpoint_sha256 != _sha(target)
        or source.identity != record.source.identity
        or source.artifact_sha256 != record.source.artifact_sha256
        or source.snapshot_sha256 != record.source.snapshot_sha256
        or target.snapshot_sha256 != record.expected_projection.snapshot_sha256
        or target.identity != receipt.target_identity
        or target.identity.namespace != record.target_namespace
        or target.identity.profile_fingerprint != record.target_profile_fingerprint
    ):
        return "receipt_binding_changed"
    if source.versions != record.versions or target.versions != record.versions:
        return "policy_changed"
    if record.versions.canonical_algorithm != policy.algorithm:
        return "policy_unproven"
    if record.versions.projection_policy != "portable-projection-v1" or (
        record.versions.source_adapter != "native-audit-v1"
        or record.versions.target_adapter != "native-audit-v1"
    ):
        return "policy_unproven"
    if (
        source.coverage is not Coverage.VERIFIED
        or target.coverage is not Coverage.VERIFIED
        or (
            record.expected_projection.coverage is not Coverage.VERIFIED
            or receipt.continuity_reason != "tracked"
        )
    ):
        return "coverage_unverified"
    if source.prefix_witness is None or target.prefix_witness is None:
        return "legacy_checkpoint_unproven"
    if source.prefix_witness.structure is None or target.prefix_witness.structure is None:
        return "structure_unproven"
    if source.snapshot_sha256 != _snapshot_sha(source.prefix_witness, record.versions, policy) or (
        target.snapshot_sha256 != _snapshot_sha(target.prefix_witness, record.versions, policy)
    ):
        return "checkpoint_binding_changed"
    if (
        source.prefix_witness != record.source.prefix_witness
        or target.prefix_witness != record.expected_projection.prefix_witness
    ):
        return "checkpoint_binding_changed"
    if record.expected_projection.relation_sha256 is None:
        return "relation_unproven"
    edges = [e for e in view.edges if e.operation_id == record.operation_id]
    if len(edges) != 1 or (
        edges[0].source_identity != source.identity
        or edges[0].target_identity != target.identity
        or edges[0].source_checkpoint_sha256 != _sha(source)
        or edges[0].target_checkpoint_sha256 != _sha(target)
    ):
        return "edge_binding_changed"
    return None


def assess_continuity(
    selector: ContinuitySelector,
    observer: EndpointObserver,
    store_path: Path,
) -> ContinuityAssessmentV1 | ContinuityAssessmentV2:
    """Observe twice per endpoint and recheck the ledger; never mutate either."""
    if type(selector) is not ContinuitySelector or not isinstance(store_path, Path):
        raise ContinuityError()
    selector.__post_init__()
    source_cmp = target_cmp = None
    source_struct = target_struct = relation = None
    source_sha = target_sha = None
    effects = ()
    started = finished = None
    operation_id = selector.operation_id
    source_identity = target_identity = None
    source_cp_sha = target_cp_sha = receipt_sha = None
    versions = canonical_sha = None
    source_cov = target_cov = source_fidelity = target_fidelity = None
    source_binding_kind = target_binding_kind = None
    source_current_fp = target_current_fp = None

    def result(status, reason, blockers=(), actions=("inspect",)):
        return ContinuityAssessmentV1(
            1,
            status,
            reason,
            operation_id,
            source_cmp,
            target_cmp,
            source_struct,
            target_struct,
            relation,
            source_sha,
            target_sha,
            tuple(blockers),
            tuple(actions),
            effects,
            started,
            finished,
            source_identity,
            target_identity,
            source_cp_sha,
            target_cp_sha,
            receipt_sha,
            versions,
            canonical_sha,
            source_cov,
            target_cov,
            source_fidelity,
            target_fidelity,
            source_binding_kind,
            target_binding_kind,
            source_current_fp,
            target_current_fp,
        )

    def read_effect(effect: str) -> None:
        nonlocal effects
        effects = tuple(sorted(set(effects) | {effect}))

    failure = _pair_profile_failure(selector, observer)
    if failure:
        return result(ContinuityStatus.UNTRACKED, failure)

    try:
        view = LineageStore.inspect_existing(store_path)
        inspection = _select(selector, view, observer)
    except SessionMigrateError:
        return result(ContinuityStatus.LINEAGE_BROKEN, "ledger_invalid")
    op = inspection.operation
    if op is None:
        return result(ContinuityStatus.UNTRACKED, inspection.reason)
    operation_id = op.record.operation_id
    if op.record.transformation is not None:
        return _assess_derived(selector, observer, store_path, view, op)
    source_cp_sha, target_cp_sha, receipt_sha = (
        _sha(op.source_checkpoint),
        _sha(op.target_checkpoint),
        _sha(op.receipt),
    )
    versions = op.record.versions
    if op.state is not OperationState.COMPLETED:
        actions = (
            ("inspect", "cancel_ready")
            if op.durable_state is OperationState.READY
            else ("inspect", "plan_reconcile")
            if op.state is OperationState.NEEDS_RECONCILE
            else ("inspect",)
        )
        return result(
            ContinuityStatus.NEEDS_RECONCILE
            if op.state in (OperationState.READY, OperationState.NEEDS_RECONCILE)
            else ContinuityStatus.UNTRACKED,
            "recovered_ready"
            if op.state is OperationState.READY
            else "operation_pending"
            if op.state is OperationState.NEEDS_RECONCILE
            else "operation_not_completed",
            actions=actions,
        )
    policy = CanonicalPolicy()
    canonical_sha = policy.sha256
    failure = _baseline_reason(op, view, policy)
    if failure is not None:
        return result(
            ContinuityStatus.LINEAGE_BROKEN
            if failure.endswith("changed")
            else ContinuityStatus.UNTRACKED,
            failure,
        )
    try:
        validate_observer(observer)
        refs = (
            (selector.pair.source, selector.pair.target)
            if selector.pair
            else (
                ref_for_identity(observer, op.source_checkpoint.identity),
                ref_for_identity(observer, op.target_checkpoint.identity),
            )
        )
        clients = (op.record.versions.source_client, op.record.versions.target_client)
        started = monotonic_ns()
        first = []
        for index, (ref, client) in enumerate(zip(refs, clients, strict=True)):
            endpoint = observe_endpoint(
                observer,
                ref,
                client_version=client,
                canonical_policy=policy,
                on_read_effect=read_effect,
            )
            for effect in endpoint.native_read_effects:
                read_effect(effect)
            validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=started)
            if index == 0:
                source_binding_kind, source_current_fp = (
                    endpoint.profile_binding_kind,
                    endpoint.current_profile_fingerprint,
                )
                source_identity, source_sha = (
                    endpoint.identity,
                    endpoint.authority_sha256,
                )
                source_cov, source_fidelity = (
                    endpoint.observation.coverage,
                    endpoint.observation.fidelity,
                )
            else:
                target_binding_kind, target_current_fp = (
                    endpoint.profile_binding_kind,
                    endpoint.current_profile_fingerprint,
                )
                target_identity, target_sha = (
                    endpoint.identity,
                    endpoint.authority_sha256,
                )
                target_cov, target_fidelity = (
                    endpoint.observation.coverage,
                    endpoint.observation.fidelity,
                )
            first.append(endpoint)
        second_start = monotonic_ns()
        second = []
        for index, (ref, client) in enumerate(zip(refs, clients, strict=True)):
            endpoint = observe_endpoint(
                observer,
                ref,
                client_version=client,
                canonical_policy=policy,
                on_read_effect=read_effect,
            )
            for effect in endpoint.native_read_effects:
                read_effect(effect)
            validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=second_start)
            if index == 0:
                source_binding_kind, source_current_fp = (
                    endpoint.profile_binding_kind,
                    endpoint.current_profile_fingerprint,
                )
                source_identity, source_sha = (
                    endpoint.identity,
                    endpoint.authority_sha256,
                )
                source_cov, source_fidelity = (
                    endpoint.observation.coverage,
                    endpoint.observation.fidelity,
                )
            else:
                target_binding_kind, target_current_fp = (
                    endpoint.profile_binding_kind,
                    endpoint.current_profile_fingerprint,
                )
                target_identity, target_sha = (
                    endpoint.identity,
                    endpoint.authority_sha256,
                )
                target_cov, target_fidelity = (
                    endpoint.observation.coverage,
                    endpoint.observation.fidelity,
                )
            second.append(endpoint)
        finished = monotonic_ns()
        source, target = second
        source_sha, target_sha = source.authority_sha256, target.authority_sha256
        latest = LineageStore.inspect_existing(store_path)
        if latest is None or _view_fingerprint(latest) != inspection.store_fingerprint:
            return result(ContinuityStatus.NEEDS_RECONCILE, "ledger_changed", ("ledger_changed",))
        if any(
            a.authority_sha256 != b.authority_sha256
            or a.identity != b.identity
            or a.observation.source_prefix != b.observation.source_prefix
            for a, b in zip(first, second, strict=True)
        ):
            return result(
                ContinuityStatus.UNTRACKED,
                "observation_changed",
                ("observation_changed",),
            )
        if (
            source.identity != op.source_checkpoint.identity
            or target.identity != op.target_checkpoint.identity
        ):
            return result(ContinuityStatus.LINEAGE_BROKEN, "identity_changed")
        observations = (source.observation, target.observation)
        if any(
            o.coverage is not Coverage.VERIFIED
            or o.fidelity is not Coverage.VERIFIED
            or o.witness.blocking
            for o in observations
        ):
            return result(
                ContinuityStatus.UNTRACKED,
                "coverage_unverified",
                ("coverage_unverified",),
            )
        source_cmp = compare_prefix(
            iter_canonical_units(observations[0].events, policy),
            op.source_checkpoint.prefix_witness,
            previous_policy=policy,
            current_policy=policy,
        )
        target_cmp = compare_prefix(
            iter_canonical_units(observations[1].events, policy),
            op.target_checkpoint.prefix_witness,
            previous_policy=policy,
            current_policy=policy,
        )
        source_struct = (
            observations[0].source_prefix.structure == op.source_checkpoint.prefix_witness.structure
        )
        target_struct = (
            observations[1].source_prefix.structure == op.target_checkpoint.prefix_witness.structure
        )
        if not source_cmp.matches or not target_cmp.matches:
            return result(ContinuityStatus.LINEAGE_BROKEN, "prefix_changed")
        if target_cmp.sequence_extends:
            return result(
                ContinuityStatus.CONFLICT,
                "both_advanced" if source_cmp.sequence_extends else "target_advanced",
                actions=("inspect", "new_session"),
            )
        if source_cmp.sequence_extends:
            blockers = ["native_boundary_unproven"]
            if not source_struct:
                blockers.append("structure_alignment_unproven")
            # No native closure issuer currently exists. Do not expose a flag that
            # manufactures DELTA_CANDIDATE from an otherwise plausible prefix.
            return result(
                ContinuityStatus.UNTRACKED,
                "native_boundary_unproven",
                blockers,
                actions=("inspect", "new_session"),
            )
        if not source_struct or not target_struct:
            return result(ContinuityStatus.LINEAGE_BROKEN, "structure_changed")
        if source.authority_sha256 != op.record.source.artifact_sha256:
            return result(ContinuityStatus.UNTRACKED, "relation_source_authority_changed")
        expected = expected_from_source(
            observations[0],
            target_format=TargetFormat(op.record.target_namespace.format),
        )
        relation = (
            expected.relation_complete
            and expected.preserves_content
            and (
                expected.relation.chain_sha256 == op.record.expected_projection.relation_sha256
                and expected.witness.prefix_witness.units == observations[1].source_prefix.units
                and expected.witness.prefix_witness.tool_links
                == observations[1].source_prefix.tool_links
            )
        )
        if not relation:
            return result(ContinuityStatus.UNTRACKED, "relation_unproven")
        boundary_blockers = (
            ("native_boundary_unproven",)
            if any(o.boundary.kind is BoundaryKind.UNPROVEN for o in observations)
            else ()
        )
        return result(ContinuityStatus.NO_OP, "unchanged", boundary_blockers, actions=("inspect",))
    except ContinuityObservationError as error:
        finished = monotonic_ns() if started is not None else None
        for effect in error.native_read_effects:
            read_effect(effect)
        return result(ContinuityStatus.UNTRACKED, error.code, (error.code,))
    except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError):
        finished = monotonic_ns() if started is not None else None
        return result(
            ContinuityStatus.UNTRACKED,
            "observation_unproven",
            ("observation_unproven",),
        )


def _assess_derived(selector, observer, store_path, view, op):
    """Observe derived endpoints without invoking the full-history baseline oracle."""
    from session_migrate.reconciliation import _observe_pair, _stable

    record, receipt = op.record, op.receipt
    derivation = DerivationBindingV1(
        1, record.source.identity, record.transformation.input, _sha(record.transformation)
    )
    effects = set()
    observations = None
    started = finished = None

    def result(reason, status=ContinuityStatus.UNTRACKED):
        source, target = observations if observations is not None else (None, None)
        base = ContinuityAssessmentV1(
            1,
            status,
            reason,
            record.operation_id,
            None,
            None,
            None,
            None,
            None,
            source.authority_sha256 if source else None,
            target.authority_sha256 if target else None,
            ("native_boundary_unproven",),
            ("inspect",),
            tuple(sorted(effects)),
            started,
            finished,
            source.identity if source else None,
            target.identity if target else None,
            _sha(op.source_checkpoint),
            _sha(op.target_checkpoint),
            _sha(receipt),
            record.versions,
            CanonicalPolicy().sha256,
            source.observation.coverage if source else None,
            target.observation.coverage if target else None,
            source.observation.fidelity if source else None,
            target.observation.fidelity if target else None,
            source.profile_binding_kind if source else None,
            target.profile_binding_kind if target else None,
            source.current_profile_fingerprint if source else None,
            target.current_profile_fingerprint if target else None,
        )
        return ContinuityAssessmentV2(2, base, derivation, record.return_link)

    if op.durable_state is not OperationState.COMPLETED:
        return result("derived_operation_pending", ContinuityStatus.NEEDS_RECONCILE)
    try:
        if (
            type(receipt) is not CompletionReceipt
            or receipt.derivation != derivation
            or receipt.return_link != record.return_link
            or receipt.continuity_reason != "derived_handoff"
            or op.source_checkpoint is None
            or op.target_checkpoint is None
            or receipt.source_checkpoint_sha256 != _sha(op.source_checkpoint)
            or receipt.target_checkpoint_sha256 != _sha(op.target_checkpoint)
            or receipt.target_identity != op.target_checkpoint.identity
            or receipt.input_artifact_sha256 != record.target_input_sha256
            or op.source_checkpoint.identity != record.source.identity
            or op.source_checkpoint.artifact_sha256 != record.source.artifact_sha256
            or op.target_checkpoint.snapshot_sha256 != record.expected_projection.snapshot_sha256
            or op.target_checkpoint.prefix_witness != record.expected_projection.prefix_witness
        ):
            return result("derived_binding_changed", ContinuityStatus.LINEAGE_BROKEN)
        edges = [edge for edge in view.edges if edge.operation_id == record.operation_id]
        if (
            len(edges) != 1
            or edges[0].derivation != derivation
            or edges[0].return_link != record.return_link
            or edges[0].source_identity != record.source.identity
            or edges[0].target_identity != receipt.target_identity
            or edges[0].source_checkpoint_sha256 != _sha(op.source_checkpoint)
            or edges[0].target_checkpoint_sha256 != _sha(op.target_checkpoint)
        ):
            return result("derived_binding_changed", ContinuityStatus.LINEAGE_BROKEN)
        if record.return_link is not None:
            from session_migrate.handoff_operations import _verified_parent

            latest_parent_view, parent = _verified_parent(
                store_path, record.return_link.parent_operation_id
            )
            if (
                latest_parent_view.store_uuid != view.store_uuid
                or _sha(parent.receipt) != record.return_link.parent_receipt_sha256
                or parent.receipt.target_identity != record.source.identity
            ):
                return result("derived_parent_changed", ContinuityStatus.LINEAGE_BROKEN)
        validate_observer(observer)
        refs = (
            (selector.pair.source, selector.pair.target)
            if selector.pair
            else (
                ref_for_identity(observer, record.source.identity),
                ref_for_identity(observer, receipt.target_identity),
            )
        )
        clients = (record.versions.source_client, record.versions.target_client)
        started = monotonic_ns()
        first = _observe_pair(observer, refs, clients, CanonicalPolicy(), effects)
        second = _observe_pair(observer, refs, clients, CanonicalPolicy(), effects)
        _stable(first, second)
        observations = second
        finished = monotonic_ns()
        latest = LineageStore.inspect_existing(store_path)
        if latest is None or _view_fingerprint(latest) != _view_fingerprint(view):
            return result("derived_ledger_changed", ContinuityStatus.NEEDS_RECONCILE)
        if (
            second[0].identity != record.source.identity
            or second[1].identity != receipt.target_identity
        ):
            return result("derived_identity_changed", ContinuityStatus.LINEAGE_BROKEN)
        if second[0].authority_sha256 != record.source.artifact_sha256:
            return result("derived_source_changed")
        if second[1].authority_sha256 != op.target_checkpoint.artifact_sha256:
            return result("derived_target_changed")
        return result("derived_handoff_not_full_history")
    except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError) as error:
        effects.update(getattr(error, "native_read_effects", ()))
        finished = monotonic_ns() if started is not None else None
        return result("derived_observation_unproven")
