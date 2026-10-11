"""Explicit, acknowledged recovery; no native import or publication authority.

Proposals contain metadata only. Their volatile handles are capabilities issued
by this module, never a serialized request that can be replayed or applied.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from time import monotonic_ns
from types import MappingProxyType
from weakref import ReferenceType, WeakKeyDictionary
from weakref import ref as weak_ref

from session_migrate.continuity_observation import (
    _OBSERVATIONS,
    EndpointObserver,
    observe_endpoint,
    ref_for_identity,
    validate_endpoint,
    validate_observer,
)
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import (
    CompletionReceipt,
    CompletionRecord,
    EvidenceReference,
    LineageError,
    LineageStore,
    NativeState,
    OperationState,
    PolicyVersions,
    QualifiedIdentity,
    RecoveryOperation,
    _encode,
    _identifier,
    _validate_cancel_expectations,
)
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_source
from session_migrate.operations import (
    _PUBLIC_ENVIRONMENT,
    OperationRequest,
    _checkpoint,
    _effective_source_coverage,
)
from session_migrate.profiles import Profile
from session_migrate.projection_policies import ProjectionPolicy
from session_migrate.recovered_projections import (
    _prepare_recovery_context,
    prepare_recovered_projection,
    validate_recovered_projection,
    verify_recovered_projection,
)
from session_migrate.titles import (
    MAX_TITLE_RECORD_BYTES,
    AliasIdentity,
    TitleAliasStore,
    TitlePolicy,
    TitleReceipt,
    TitleReceiptBinding,
    _directory,
    _directory_current,
    _private_directory,
    _unique_pairs,
    plan_target_title,
    preflight_title_receipt,
    title_receipt_path,
    write_title_receipt,
)

_MAX_HANDLES = 64
_READ_EFFECTS = {
    "file_capture_read_attempted",
    "file_capture_read",
    "native_export_read_attempted",
    "native_export_read",
    "private_scratch_possible",
    "private_scratch",
    "native_cache_or_wal_possible",
}


class ReconciliationError(SessionMigrateError):
    def __init__(self, code: str, *, operation_id: str | None = None):
        self.code = (
            code
            if code
            in {
                "invalid_request",
                "invalid_handle",
                "handle_capacity",
                "ledger_changed",
                "observation_changed",
                "proof_unavailable",
                "finalization_unavailable",
                "finalization_conflict",
                "persistence_uncertain",
                "not_cancelable",
            }
            else "proof_unavailable"
        )
        self.operation_id = operation_id
        super().__init__(f"Reconciliation: {self.code}.")


def _require(condition, code="proof_unavailable"):
    if not condition:
        raise ReconciliationError(code)


def _sha(value):
    # Durable records retain A's exact codec. Internal composite bindings also
    # include DTOs that deliberately cannot enter the durable record codec.
    try:
        payload = _encode(value)
    except LineageError:
        payload = json.dumps(
            _metadata(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _metadata(value):
    try:
        return json.loads(_encode(value))
    except LineageError:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Mapping):
            return {k: _metadata(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [_metadata(v) for v in value]
        if is_dataclass(value):
            return {f.name: _metadata(getattr(value, f.name)) for f in fields(value)}
        raise ReconciliationError("invalid_request") from None


def _path(value):
    _require(
        isinstance(value, Path) and value.is_absolute() and ".." not in value.parts,
        "invalid_request",
    )
    return value


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryContext:
    request: OperationRequest
    source_path: Path
    input_path: Path
    source_environment: Mapping[str, str]
    target_environment: Mapping[str, str]
    source_profile: Profile | None = None
    target_profile: Profile | None = None
    manifest_path: Path | None = None
    policy: ProjectionPolicy = field(default_factory=ProjectionPolicy)
    handoff_document: Path | None = None

    def __post_init__(self):
        _require(type(self.request) is OperationRequest, "invalid_request")
        _path(self.source_path)
        _path(self.input_path)
        if self.manifest_path is not None:
            _path(self.manifest_path)
        if self.handoff_document is not None:
            _path(self.handoff_document)
        for name in ("source_environment", "target_environment"):
            values = dict(getattr(self, name))
            _require(
                all(
                    type(k) is str and k in _PUBLIC_ENVIRONMENT and type(v) is str
                    for k, v in values.items()
                ),
                "invalid_request",
            )
            object.__setattr__(self, name, MappingProxyType(values))
        _require(type(self.policy) is ProjectionPolicy, "invalid_request")
        self.policy.__post_init__()
        for profile in (self.source_profile, self.target_profile):
            _require(profile is None or type(profile) is Profile, "invalid_request")


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class ReconciliationHandle:
    _binding: str

    def __init__(self):
        raise ReconciliationError("invalid_handle")

    def discard(self):
        _HANDLES.pop(self, None)


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class ReconciliationProposalV1:
    schema_version: int
    operation_id: str
    status: str
    reason: str
    durable_state: OperationState | None
    native_state: NativeState | None
    record_sha256: str | None
    versions: PolicyVersions | None
    source_identity: QualifiedIdentity | None
    target_identity: QualifiedIdentity | None
    native_read_effects: tuple[str, ...]
    available_effects: tuple[str, ...]
    blockers: tuple[str, ...]
    receipt: CompletionReceipt | None = None
    handle: ReconciliationHandle | None = field(default=None, compare=False)

    def __post_init__(self):
        _require(
            type(self.schema_version) is int and self.schema_version == 1,
            "invalid_request",
        )
        _identifier(self.operation_id)
        _require(
            self.status in {"ready", "blocked", "inspect_only", "completed"},
            "invalid_request",
        )
        _require(type(self.reason) is str and self.reason in _REASONS, "invalid_request")
        _require(
            self.durable_state is None or type(self.durable_state) is OperationState,
            "invalid_request",
        )
        _require(
            self.native_state is None or type(self.native_state) is NativeState,
            "invalid_request",
        )
        _require(
            self.versions is None or type(self.versions) is PolicyVersions,
            "invalid_request",
        )
        for identity in (self.source_identity, self.target_identity):
            _require(
                identity is None or type(identity) is QualifiedIdentity,
                "invalid_request",
            )
        _require(
            self.record_sha256 is None
            or (
                type(self.record_sha256) is str
                and len(self.record_sha256) == 64
                and all(c in "0123456789abcdef" for c in self.record_sha256)
            ),
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
            <= {
                "complete_ledger",
                "local_alias",
                "title_receipt",
                "cancel_ready",
                "inspect",
            },
            "invalid_request",
        )
        _require(
            self.receipt is None or type(self.receipt) is CompletionReceipt,
            "invalid_request",
        )
        _require(
            self.handle is None or type(self.handle) is ReconciliationHandle,
            "invalid_request",
        )

    def to_dict(self):
        document = {
            f.name: json.loads(_encode(getattr(self, f.name)))
            for f in fields(self)
            if f.name != "handle"
        }
        for side in ("source", "target"):
            identity = getattr(self, side + "_identity")
            document[side + "_root_sha256"] = (
                _sha(identity.namespace.canonical_root) if identity else None
            )
        return document


_REASONS = {
    "ready",
    "completed",
    "operation_not_found",
    "ledger_missing",
    "ledger_invalid",
    "recovered_ready",
    "native_ack_required",
    "operation_not_recoverable",
    "context_unavailable",
    "context_mismatch",
    "proof_unavailable",
    "source_mismatch",
    "input_mismatch",
    "options_mismatch",
    "unsupported_policy",
    "semantic_mismatch",
    "metadata_mismatch",
    "binding_changed",
    "invalid_certificate",
    "observer_changed",
    "identity_unproven",
    "endpoint_unavailable",
    "unsupported_endpoint",
    "policy_changed",
    "observation_unproven",
    "observation_changed",
    "finalization_unavailable",
    "finalization_conflict",
    "ledger_changed",
    "handle_capacity",
}


@dataclass(frozen=True, slots=True, weakref_slot=True, repr=False)
class _State:
    store_path: Path
    store_uuid: str
    operation: RecoveryOperation
    context: RecoveryContext | None
    observer: EndpointObserver | None
    proposal: ReconciliationProposalV1
    proof: object = None
    public_proposal: ReferenceType[ReconciliationProposalV1] | None = None


_HANDLES: WeakKeyDictionary[ReconciliationHandle, _State] = WeakKeyDictionary()


def _context_binding(context):
    if context is None:
        return None
    binding = {
        "request": {
            f.name: str(v) if isinstance(v, Path) else v
            for f in fields(context.request)
            if (v := getattr(context.request, f.name)) is not None
        },
        "source": str(context.source_path),
        "input": str(context.input_path),
        "manifest": str(context.manifest_path) if context.manifest_path else None,
        "source_environment": dict(context.source_environment),
        "target_environment": dict(context.target_environment),
        "profiles": [
            p.fingerprint if p else None for p in (context.source_profile, context.target_profile)
        ],
        "policy": context.policy.sha256,
    }
    if context.handoff_document is not None:
        binding["handoff_document"] = str(context.handoff_document)
    return binding


def _state_binding(state):
    _require(state.proposal.handle is None, "invalid_handle")
    # Profile contains Path/MappingProxy values; use its established fingerprint.
    context = _context_binding(state.context)
    if context is not None:
        context["profiles"] = [
            p.fingerprint if p else None
            for p in (state.context.source_profile, state.context.target_profile)
        ]
    return _sha(
        {
            "path": str(state.store_path),
            "uuid": state.store_uuid,
            "operation": state.operation,
            "context": context,
            "observer": id(state.observer),
            "proposal": state.proposal.to_dict(),
            "proof": id(state.proof),
            "public_proposal": id(state.public_proposal),
        }
    )


def _issue(state):
    _require(len(_HANDLES) < _MAX_HANDLES, "handle_capacity")
    # A weak-key value must never retain its handle through the public DTO.
    # Private metadata survives when callers keep only proposal.handle.
    public = state.proposal
    state = replace(state, proposal=replace(public, handle=None), public_proposal=weak_ref(public))
    handle = object.__new__(ReconciliationHandle)
    object.__setattr__(handle, "_binding", _state_binding(state))
    _HANDLES[handle] = state
    return handle


def _check_handle_state(handle, state):
    """Bind private metadata and any still-live original public proposal."""
    try:
        _require(type(state.public_proposal) is ReferenceType, "invalid_handle")
        _require(handle._binding == _state_binding(state), "invalid_handle")
        public = state.public_proposal()
        if public is not None:
            _require(type(public) is ReconciliationProposalV1, "invalid_handle")
            public.__post_init__()
            _require(
                public.handle is handle and public.to_dict() == state.proposal.to_dict(),
                "invalid_handle",
            )
    except (SessionMigrateError, ValueError, TypeError, AttributeError):
        raise ReconciliationError("invalid_handle") from None


def _operation(view, operation_id):
    if view is None:
        return None
    return next((op for op in view.operations if op.record.operation_id == operation_id), None)


def inspect_recovered_operation(operation_id, *, store_path):
    _identifier(operation_id)
    _path(store_path)
    view = LineageStore.inspect_existing(store_path)
    return _operation(view, operation_id)


def _observe_pair(observer, refs, clients, policy, effects):
    started = monotonic_ns()
    result = []
    for ref, client in zip(refs, clients, strict=True):
        if ref is None:
            result.append(None)
            continue
        endpoint = observe_endpoint(
            observer,
            ref,
            client_version=client,
            canonical_policy=policy,
            on_read_effect=effects.add,
        )
        validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=started)
        result.append(endpoint)
    # The last capture can itself perform native I/O. Recheck every first-side
    # authority after the second-side read, in addition to fresh double capture.
    for ref, endpoint in zip(refs, result, strict=True):
        if endpoint is not None:
            validate_endpoint(endpoint, observer=observer, ref=ref, issued_after_ns=started)
    return tuple(result)


def _endpoint_fingerprint(endpoint):
    if endpoint is None:
        return None
    return _sha(
        (
            endpoint.identity,
            endpoint.authority_sha256,
            endpoint.observation.source_prefix,
            endpoint.observation.audit.structure,
            endpoint.observation.coverage,
            endpoint.observation.fidelity,
            endpoint.profile_binding_kind.value,
            endpoint.current_profile_fingerprint,
            endpoint.current_profile_id,
        )
    )


def _stable(first, second):
    _require(
        all(
            _endpoint_fingerprint(a) == _endpoint_fingerprint(b)
            for a, b in zip(first, second, strict=True)
        ),
        "observation_changed",
    )


def _read_document(path, *, private=True, maximum=MAX_TITLE_RECORD_BYTES):
    directory = (_private_directory if private else _directory)(path.parent, create=False)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            _require(
                stat.S_ISREG(before.st_mode)
                and before.st_nlink == 1
                and (
                    not private
                    or (before.st_uid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600)
                ),
                "finalization_unavailable",
            )
            _require(before.st_size <= maximum, "finalization_unavailable")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)

            def witness(info):
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )

            _require(
                len(data) <= maximum and witness(before) == witness(after) == witness(named),
                "finalization_conflict",
            )
            _directory_current(path.parent, directory)
    finally:
        os.close(directory)
    document = json.loads(
        data,
        object_pairs_hook=_unique_pairs,
        parse_constant=lambda _: _require(False, "finalization_conflict"),
    )
    _require(type(document) is dict, "finalization_conflict")
    return document, hashlib.sha256(data).hexdigest()


def _title_state(context, record, projection, target, *, observer, ref, issued_after_ns):
    request = context.request
    if request.target_title is None:
        return None
    try:
        plan = plan_target_title(
            record.target_namespace.format,
            requested=request.target_title,
            cli_version=record.versions.target_client,
            policy=request.title_policy,
            source_title=(
                None
                if record.transformation is not None
                else projection.source_observation.session.title
            ),
        )
    except SessionMigrateError:
        raise ReconciliationError("finalization_unavailable") from None
    manifest = context.manifest_path
    _require(
        manifest is not None
        and any(ref.kind == "manifest" and ref.path == str(manifest) for ref in record.references),
        "finalization_unavailable",
    )
    document, manifest_sha = _read_document(
        manifest,
        private=False,
        maximum=128 * 1024 if record.transformation is not None else MAX_TITLE_RECORD_BYTES,
    )
    if record.transformation is not None:
        from session_migrate.conversion import decode_derived_manifest

        document = decode_derived_manifest(json.dumps(document).encode())
        _require(
            document["transformation"] == json.loads(_encode(record.transformation))
            and document["conversion_input"] == json.loads(_encode(record.transformation.input))
            and document["return_link"] == json.loads(_encode(record.return_link)),
            "finalization_conflict",
        )
        document = {**document, "schema_version": 2, "source": document["authority_source"]}
    _require(
        document.get("schema_version") == 2
        and type(document.get("source")) is dict
        and type(document.get("target")) is dict
        and document["source"].get("sha256") == record.source.artifact_sha256
        and document["target"].get("sha256") == record.target_input_sha256
        and document["target"].get("session_id") == record.target_namespace.native_id
        and document["target"].get("format") == record.target_namespace.format,
        "finalization_conflict",
    )
    source_session, input_session = (
        projection.source_observation.session,
        projection.artifact_observation.session,
    )
    validate_endpoint(target, observer=observer, ref=ref, issued_after_ns=issued_after_ns)
    target_path = _OBSERVATIONS[target].snapshot.source_path
    validate_endpoint(target, observer=observer, ref=ref, issued_after_ns=issued_after_ns)
    _require(
        document["source"].get("format") == source_session.source_format.value
        and document["source"].get("path") == str(context.source_path)
        and document["source"].get("session_id") == source_session.session_id
        and document["source"].get("cli_version") == record.versions.source_client
        and document["target"].get("path") == str(target_path)
        and document["target"].get("cli_version") == record.versions.target_client
        and document["target"].get("cwd") == str(input_session.cwd)
        and document["target"].get("timestamp") == input_session.started_at,
        "finalization_conflict",
    )
    identity = target.identity
    alias_identity = AliasIdentity(
        AgentFormat(identity.namespace.format),
        Path(identity.namespace.canonical_root),
        identity.namespace.native_id,
        identity.root_witness.device,
        identity.root_witness.inode,
    )
    store = TitleAliasStore(
        default_migration_state_home(environ=context.target_environment) / "title-aliases/v1"
    )
    binding = TitleReceiptBinding(
        AgentFormat(record.target_namespace.format),
        record.target_input_sha256,
        identity=alias_identity,
        manifest_path=manifest,
        proposal_id=record.operation_id,
    )
    if plan.policy is TitlePolicy.NATIVE:
        observed = target.observation.session.title
        _require(observed == plan.requested, "finalization_conflict")
        receipt = TitleReceipt.verified_native(
            plan, observed=observed, cli_version=record.versions.target_client
        )
    else:
        receipt = TitleReceipt.published_alias(plan)
    expected = receipt.to_dict()
    expected["binding"] = binding.to_dict()
    receipt_path = title_receipt_path(manifest)
    alias_path = store.path_for(alias_identity) if plan.policy is TitlePolicy.LOCAL_ALIAS else None
    alias_sha = None
    if alias_path is not None:
        if alias_path.exists() or alias_path.is_symlink():
            existing = store.load(alias_identity)
            _require(
                existing.alias == plan.requested and existing.proposal_id == record.operation_id,
                "finalization_conflict",
            )
            _, alias_sha = _read_document(alias_path)
        else:
            store.preflight(alias_identity)
    receipt_sha = None
    if receipt_path.exists() or receipt_path.is_symlink():
        existing, receipt_sha = _read_document(receipt_path)
        _require(existing == expected, "finalization_conflict")
    else:
        preflight_title_receipt(receipt_path)
    return (
        plan,
        store,
        alias_identity,
        receipt,
        binding,
        receipt_path,
        manifest_sha,
        alias_sha,
        receipt_sha,
    )


def _proof(operation, context, observer, effects, *, store_path=None):
    if operation.record.transformation is not None:
        return _derived_recovery_proof(operation, context, observer, effects, store_path=store_path)
    started = monotonic_ns()
    record = operation.record
    validate_observer(observer)
    fmt = context.request.source_format
    if fmt is None:
        from session_migrate.inspection import detect_path_format

        fmt = detect_path_format(context.source_path)
    _require(fmt in {AgentFormat.CLAUDE, AgentFormat.CODEX}, "proof_unavailable")
    effects.add("file_capture_read_attempted")
    source = capture_source(SourceCaptureContext(fmt, context.source_path))
    effects.add("file_capture_read")
    artifact = capture_source(
        SourceCaptureContext(AgentFormat(record.target_namespace.format), context.input_path)
    )
    effects.update(("file_capture_read_attempted", "file_capture_read"))
    recovered_context = _prepare_recovery_context(
        source,
        artifact,
        record,
        context.request,
        source_environment=context.source_environment,
        target_environment=context.target_environment,
        source_profile=context.source_profile,
        target_profile=context.target_profile,
        policy=context.policy,
        observer=observer,
        on_read_effect=effects.add,
        parent_store_path=store_path if record.return_link is not None else None,
    )
    projection = prepare_recovered_projection(source, artifact, record, recovered_context)
    source_ref = (
        ref_for_identity(observer, record.source.identity) if record.source.identity else None
    )
    from session_migrate.continuity_observation import effective_profile_fingerprint
    from session_migrate.profiles import SessionRef

    matches = [
        p
        for p in observer._profiles
        if p.format.value == record.target_namespace.format
        and str(p.root) == record.target_namespace.canonical_root
    ]
    _require(len(matches) == 1, "proof_unavailable")
    profile = matches[0]
    target_ref = SessionRef(
        profile.format,
        profile.root,
        record.target_namespace.native_id,
        profile.id,
        profile_fingerprint=profile.fingerprint,
    )
    _require(
        effective_profile_fingerprint(observer, target_ref) == record.target_profile_fingerprint,
        "proof_unavailable",
    )
    refs = (source_ref, target_ref)
    clients = (record.versions.source_client, record.versions.target_client)
    first = _observe_pair(observer, refs, clients, context.policy.canonical_policy, effects)
    second = _observe_pair(observer, refs, clients, context.policy.canonical_policy, effects)
    _stable(first, second)
    if second[0] is not None:
        _require(
            second[0].identity == record.source.identity
            and second[0].authority_sha256 == source.authority_sha256,
            "observation_changed",
        )
    target = second[1]
    _require(target.authority_sha256 == artifact.authority_sha256, "observation_changed")
    validate_endpoint(target, observer=observer, ref=target_ref, issued_after_ns=started)
    target_snapshot = _OBSERVATIONS[target].snapshot
    validate_endpoint(target, observer=observer, ref=target_ref, issued_after_ns=started)
    verification = verify_recovered_projection(
        projection, target_snapshot, context=recovered_context.projection_context
    )
    _require(
        verification.semantic_matches and verification.structure_matches,
        "proof_unavailable",
    )
    actual = _effective_source_coverage(target.observation)
    _require(actual is record.expected_projection.coverage, "proof_unavailable")
    if actual.value == "verified":
        _require(
            verification.relation_complete
            and verification.preserves_content
            and projection.certificate is not None,
            "proof_unavailable",
        )
    title = _title_state(
        context,
        record,
        projection,
        target,
        observer=observer,
        ref=target_ref,
        issued_after_ns=started,
    )
    # Title/manifest reads can overlap native mutation. Reobserve after those I/Os
    # and recapture portable source/input bytes even when identity is absent.
    third = _observe_pair(observer, refs, clients, context.policy.canonical_policy, effects)
    _stable(second, third)
    final_source = capture_source(SourceCaptureContext(fmt, context.source_path))
    final_input = capture_source(
        SourceCaptureContext(AgentFormat(record.target_namespace.format), context.input_path)
    )
    _require(
        final_source.authority_sha256 == source.authority_sha256
        and final_input.authority_sha256 == artifact.authority_sha256
        and final_source.identity == source.identity
        and final_input.identity == artifact.identity,
        "observation_changed",
    )
    validate_recovered_projection(projection)
    for ref, endpoint in zip(refs, third, strict=True):
        if endpoint is not None:
            validate_endpoint(endpoint, observer=observer, ref=ref)
    source_cp = _checkpoint(
        projection.source_observation,
        record.source.identity,
        source.authority_sha256,
        record.versions,
        _effective_source_coverage(projection.source_observation),
    )
    target_cp = _checkpoint(
        third[1].observation,
        third[1].identity,
        third[1].authority_sha256,
        record.versions,
        _effective_source_coverage(third[1].observation),
    )
    validate_endpoint(third[1], observer=observer, ref=target_ref, issued_after_ns=started)
    target_path = _OBSERVATIONS[third[1]].snapshot.source_path
    validate_endpoint(third[1], observer=observer, ref=target_ref, issued_after_ns=started)
    references = [
        EvidenceReference(
            "artifact",
            "verified",
            "available",
            str(context.input_path),
            artifact.authority_sha256,
        ),
        EvidenceReference(
            "native",
            "verified",
            "available",
            str(target_path),
            third[1].authority_sha256,
        ),
    ]
    if title is not None:
        references.append(
            EvidenceReference(
                "manifest",
                "verified",
                "available",
                str(context.manifest_path),
                title[6],
            )
        )
        if title[8] is not None:
            references.append(
                EvidenceReference("receipt", "verified", "available", str(title[5]), title[8])
            )
    completion = CompletionRecord(
        third[1].identity,
        artifact.authority_sha256,
        source_cp,
        target_cp,
        tuple(references),
    )
    return completion, tuple(_endpoint_fingerprint(e) for e in third), title


def _private_handoff_bytes(path):
    """Existing private accepted document, integral FD read with A's safeguards."""
    from session_migrate.handoff import _ancestry, _file_current, _private_file
    from session_migrate.handoff_types import MAX_DOCUMENT_BYTES
    from session_migrate.lineage import _PinnedNamespace

    _require(path is not None, "context_unavailable")
    namespace = _PinnedNamespace.open(path.parent)
    descriptor = None
    try:
        namespace.check()
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=namespace.root
        )
        info = os.fstat(descriptor)
        _private_file(info)
        _require(0 < info.st_size <= MAX_DOCUMENT_BYTES, "proof_unavailable")
        chunks = []
        remaining = info.st_size
        from session_migrate.handoff import _info

        witness = _info(info)
        while remaining:
            _file_current(namespace, path, descriptor, witness)
            block = os.read(descriptor, min(remaining, 64 * 1024))
            _require(bool(block), "proof_unavailable")
            chunks.append(block)
            remaining -= len(block)
        _require(not os.read(descriptor, 1), "proof_unavailable")
        _file_current(namespace, path, descriptor, witness)
        return b"".join(chunks), (witness, _ancestry(namespace))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        namespace.close()


def _derived_recovery_proof(operation, context, observer, effects, *, store_path):
    started = monotonic_ns()
    from session_migrate.continuity_observation import (
        _original_authority,
        effective_profile_fingerprint,
    )
    from session_migrate.conversion import EmitContext, PortableEmission, decode_derived_manifest
    from session_migrate.derived_projection import (
        _schema_time,
        prepare_derived_projection,
        verify_derived_projection,
    )
    from session_migrate.handoff_operations import _validate_parent_binding
    from session_migrate.handoff_types import _issue_user_input
    from session_migrate.model import TargetFormat
    from session_migrate.operations import _digest, _public_options, _source_witness
    from session_migrate.profiles import SessionRef
    from session_migrate.recovered_projections import _catalog_selection, _profiles

    record = operation.record
    _require(
        context.handoff_document is not None and context.manifest_path is not None,
        "context_unavailable",
    )
    validate_observer(observer)
    source_ref = ref_for_identity(observer, record.source.identity)
    matches = [
        p
        for p in observer._profiles
        if p.format.value == record.target_namespace.format
        and str(p.root) == record.target_namespace.canonical_root
    ]
    _require(len(matches) == 1, "proof_unavailable")
    p = matches[0]
    target_ref = SessionRef(
        p.format, p.root, record.target_namespace.native_id, p.id, profile_fingerprint=p.fingerprint
    )
    _require(
        effective_profile_fingerprint(observer, target_ref) == record.target_profile_fingerprint,
        "proof_unavailable",
    )
    profiles = _profiles(context.source_profile, context.target_profile)
    _require(
        _digest(
            _public_options(
                context.request,
                context.target_environment,
                context.source_environment,
                profiles,
                transformation=record.transformation,
                return_link=record.return_link,
            )
        )
        == record.options_sha256,
        "proof_unavailable",
    )
    _require(
        context.request.session_id == record.target_namespace.native_id
        or (
            record.target_namespace.format == "opencode"
            and record.target_namespace.native_id
            == "ses_" + context.request.session_id.replace("-", "")
        ),
        "proof_unavailable",
    )
    selection = _catalog_selection(observer, context.request)
    data, document_witness = _private_handoff_bytes(context.handoff_document)
    derived_input = _issue_user_input(data)
    _require(derived_input.identity == record.transformation.input, "proof_unavailable")
    input_capture = capture_source(
        SourceCaptureContext(AgentFormat(record.target_namespace.format), context.input_path)
    )
    _require(input_capture.authority_sha256 == record.target_input_sha256, "proof_unavailable")
    manifest, manifest_sha = _read_document(
        context.manifest_path, private=False, maximum=128 * 1024
    )
    manifest = decode_derived_manifest(json.dumps(manifest).encode())
    _require(
        any(
            ref.kind == "manifest" and ref.path == str(context.manifest_path)
            for ref in record.references
        )
        and manifest["transformation"] == json.loads(_encode(record.transformation))
        and manifest["conversion_input"] == json.loads(_encode(record.transformation.input))
        and manifest["return_link"] == json.loads(_encode(record.return_link))
        and manifest["target"]["sha256"] == record.target_input_sha256
        and manifest["target"]["format"] == record.target_namespace.format
        and manifest["target"]["session_id"] == record.target_namespace.native_id
        and manifest["target"]["cli_version"] == record.versions.target_client
        and manifest["target"]["cwd"] == str(input_capture.parse().cwd)
        and _schema_time(
            manifest["target"]["timestamp"], TargetFormat(record.target_namespace.format)
        )
        == _schema_time(
            input_capture.parse().started_at, TargetFormat(record.target_namespace.format)
        )
        and manifest["target"]["records"] == input_capture.parse().raw_record_count
        and manifest["authority_source"]["sha256"] == record.source.artifact_sha256
        and manifest["authority_source"]["identity"] == json.loads(_encode(record.source.identity))
        and manifest["authority_source"]["path"] == str(context.source_path)
        and manifest["authority_source"]["cli_version"] == record.versions.source_client,
        "proof_unavailable",
    )
    refs = (source_ref, target_ref)
    clients = (record.versions.source_client, record.versions.target_client)
    first = _observe_pair(observer, refs, clients, context.policy.canonical_policy, effects)
    second = _observe_pair(observer, refs, clients, context.policy.canonical_policy, effects)
    _stable(first, second)
    source, target = second
    _require(
        source.identity == record.source.identity
        and source.authority_sha256 == record.source.artifact_sha256
        and target.authority_sha256 == record.target_input_sha256,
        "observation_changed",
    )
    authority = _original_authority(source, observer, source_ref)
    _require(authority.snapshot.source_path == context.source_path, "proof_unavailable")
    _require(
        manifest["authority_source"]["records"] == authority.session.raw_record_count
        and manifest["authority_source"]["events"] == authority.session.event_counts(),
        "proof_unavailable",
    )
    _require(
        _source_witness(authority.session, Path(source.identity.namespace.canonical_root))
        == record.source.witness_sha256,
        "proof_unavailable",
    )
    if record.return_link is not None:
        _require(store_path is not None, "context_unavailable")
        _validate_parent_binding(
            store_path,
            record.return_link,
            source_identity=authority.identity,
            operation_record=record,
        )
    options = context.request
    emit_context = EmitContext(
        TargetFormat(record.target_namespace.format),
        record.target_namespace.native_id,
        input_capture.parse().cwd,
        record.versions.target_client,
        manifest["target"]["timestamp"],
        options.model_provider,
        options.model,
        options.target_title if options.title_policy is TitlePolicy.NATIVE else None,
    )
    emission = PortableEmission(
        emit_context, input_capture.raw_bytes, input_capture.parse().raw_record_count, ()
    )
    projection = prepare_derived_projection(
        authority, derived_input, record.transformation, emission, artifact_path=context.input_path
    )
    _require(
        projection.expectation == record.expected_projection
        and projection.versions == record.versions,
        "proof_unavailable",
    )
    validate_endpoint(target, observer=observer, ref=target_ref, issued_after_ns=started)
    actual_snapshot = _OBSERVATIONS[target].snapshot
    validate_endpoint(target, observer=observer, ref=target_ref, issued_after_ns=started)
    proof, target_observed = verify_derived_projection(projection, actual_snapshot)
    title = _title_state(
        context,
        record,
        projection,
        target,
        observer=observer,
        ref=target_ref,
        issued_after_ns=started,
    )
    third = _observe_pair(observer, refs, clients, context.policy.canonical_policy, effects)
    _stable(second, third)
    fresh_data, fresh_witness = _private_handoff_bytes(context.handoff_document)
    _require(
        fresh_data == data
        and fresh_witness == document_witness
        and _catalog_selection(observer, context.request)[4] == selection[4],
        "observation_changed",
    )
    input_after = capture_source(
        SourceCaptureContext(AgentFormat(record.target_namespace.format), context.input_path)
    )
    _, manifest_after_sha = _read_document(context.manifest_path, private=False, maximum=128 * 1024)
    _require(
        input_after.authority_sha256 == input_capture.authority_sha256
        and manifest_after_sha == manifest_sha,
        "observation_changed",
    )
    fourth = _observe_pair(observer, refs, clients, context.policy.canonical_policy, effects)
    _stable(third, fourth)
    authority.check()
    source_cp = _checkpoint(
        source.observation,
        record.source.identity,
        source.authority_sha256,
        record.versions,
        _effective_source_coverage(source.observation),
    )
    target_cp = _checkpoint(
        target_observed,
        target.identity,
        target.authority_sha256,
        record.versions,
        _effective_source_coverage(target_observed),
    )
    references = (
        EvidenceReference(
            "artifact",
            "verified",
            "available",
            str(context.input_path),
            input_capture.authority_sha256,
        ),
        EvidenceReference(
            "manifest", "verified", "available", str(context.manifest_path), manifest_sha
        ),
        EvidenceReference(
            "artifact",
            "verified",
            "available",
            str(context.handoff_document),
            record.transformation.input.document_sha256,
        ),
    )
    if title is not None and title[8] is not None:
        references += (
            EvidenceReference("receipt", "verified", "available", str(title[5]), title[8]),
        )
    return (
        CompletionRecord(
            target.identity, record.target_input_sha256, source_cp, target_cp, references, proof
        ),
        tuple(_endpoint_fingerprint(e) for e in fourth),
        title,
    )


def prepare_reconciliation(operation_id, *, store_path, context=None, observer=None):
    _identifier(operation_id)
    _path(store_path)
    effects = set()
    operation = view = None

    def result(status, reason, *, proof=None, receipt=None):
        record = operation.record if operation else None
        available = (
            ("complete_ledger",)
            if status == "ready"
            else (("cancel_ready", "inspect") if reason == "recovered_ready" else ("inspect",))
        )
        if proof is not None and proof[2] is not None:
            available += ("local_alias",) if proof[2][0].policy is TitlePolicy.LOCAL_ALIAS else ()
            available += ("title_receipt",)
        proposal = ReconciliationProposalV1(
            1,
            operation_id,
            status,
            reason,
            operation.durable_state if operation else None,
            operation.durable_native_state if operation else None,
            _sha(record) if record else None,
            record.versions if record else None,
            record.source.identity if record else None,
            proof[0].target_identity if proof else (receipt.target_identity if receipt else None),
            tuple(sorted(effects)),
            available,
            () if status in {"ready", "completed"} else (reason,),
            receipt,
        )
        if status in {"ready", "completed"}:
            state = _State(
                store_path,
                view.store_uuid,
                operation,
                context,
                observer,
                proposal,
                proof,
            )
            object.__setattr__(proposal, "handle", _issue(state))
        return proposal

    try:
        view = LineageStore.inspect_existing(store_path)
        if view is None:
            return result("blocked", "ledger_missing")
        operation = _operation(view, operation_id)
        if operation is None:
            return result("blocked", "operation_not_found")
        if operation.durable_state is OperationState.COMPLETED:
            return result("completed", "completed", receipt=operation.receipt)
        if operation.durable_state is OperationState.READY:
            return result("inspect_only", "recovered_ready")
        if operation.durable_native_state is not NativeState.ACKNOWLEDGED:
            return result("blocked", "native_ack_required")
        if operation.durable_state not in {
            OperationState.VERIFYING,
            OperationState.NEEDS_RECONCILE,
        }:
            return result("blocked", "operation_not_recoverable")
        if type(context) is not RecoveryContext or type(observer) is not EndpointObserver:
            return result("blocked", "context_unavailable")
        proof = _proof(operation, context, observer, effects, store_path=store_path)
        latest = LineageStore.inspect_existing(store_path)
        _require(
            latest is not None
            and latest.store_uuid == view.store_uuid
            and _sha(_operation(latest, operation_id)) == _sha(operation),
            "ledger_changed",
        )
        return result("ready", "ready", proof=proof)
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


def commit_reconciliation(handle):
    _require(type(handle) is ReconciliationHandle and handle in _HANDLES, "invalid_handle")
    state = _HANDLES.pop(handle)
    _check_handle_state(handle, state)
    record = state.operation.record
    published = False
    store = None
    try:
        with LineageStore.open_existing(state.store_path) as store:
            additional = (record.source.identity.namespace,) if record.source.identity else ()
            with store.lock_operation(
                record.operation_id,
                record.target_namespace,
                additional_namespaces=additional,
            ) as guard:
                current = _operation(store.recovery_view(), record.operation_id)
                _require(
                    store._store_uuid == state.store_uuid
                    and current is not None
                    and _sha(current) == _sha(state.operation),
                    "ledger_changed",
                )
                if current.durable_state is OperationState.COMPLETED:
                    return current.receipt
                proof = _proof(
                    current, state.context, state.observer, set(), store_path=state.store_path
                )
                _require(
                    proof[0] == state.proof[0]
                    and proof[1] == state.proof[1]
                    and _title_binding(proof[2]) == _title_binding(state.proof[2]),
                    "observation_changed",
                )
                title = proof[2]
                if title is not None:
                    (
                        plan,
                        alias_store,
                        identity,
                        receipt,
                        binding,
                        receipt_path,
                        _,
                        alias_sha,
                        receipt_sha,
                    ) = title
                    if plan.policy is TitlePolicy.LOCAL_ALIAS and alias_sha is None:
                        alias_store.create(
                            identity,
                            plan.requested,
                            proposal_id=record.operation_id,
                            target_verified=True,
                        )
                        after_alias = _proof(
                            current,
                            state.context,
                            state.observer,
                            set(),
                            store_path=state.store_path,
                        )
                        _require(
                            after_alias[1] == proof[1]
                            and _title_binding(after_alias[2])[:6] == _title_binding(title)[:6],
                            "observation_changed",
                        )
                    if receipt_sha is None:
                        write_title_receipt(
                            receipt_path,
                            receipt,
                            binding=binding,
                            alias_store=alias_store,
                        )
                    proof = _proof(
                        current, state.context, state.observer, set(), store_path=state.store_path
                    )
                    _require(
                        proof[1] == state.proof[1]
                        and _title_binding(proof[2])[:6] == _title_binding(title)[:6],
                        "observation_changed",
                    )
                guard.check()
                _require(
                    _sha(_operation(store.recovery_view(), record.operation_id)) == _sha(current),
                    "ledger_changed",
                )
                expected_title = _title_binding(proof[2])

                def fresh_guard():
                    # Issued state and actual fresh loaders are the authority;
                    # caller hashes never become a persistence capability.
                    try:
                        _check_handle_state(handle, state)
                        if record.return_link is not None:
                            from session_migrate.handoff_operations import _held_coordinator

                            with _held_coordinator(store, guard, record):
                                fresh = _proof(
                                    current,
                                    state.context,
                                    state.observer,
                                    set(),
                                    store_path=state.store_path,
                                )
                        else:
                            fresh = _proof(
                                current,
                                state.context,
                                state.observer,
                                set(),
                                store_path=state.store_path,
                            )
                        _require(
                            fresh[0] == proof[0]
                            and fresh[1] == proof[1]
                            and _title_binding(fresh[2]) == expected_title,
                            "observation_changed",
                        )
                        _check_handle_state(handle, state)
                    except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError):
                        raise LineageError("proof_mismatch") from None

                receipt = store.complete_reconciled(
                    record.operation_id,
                    state.proposal.record_sha256,
                    current.durable_state,
                    proof[0],
                    guard,
                    external_guard=fresh_guard,
                )
                published = True
                after = _proof(
                    current, state.context, state.observer, set(), store_path=state.store_path
                )
                _require(
                    after[0] == proof[0]
                    and after[1] == proof[1]
                    and _title_binding(after[2]) == _title_binding(proof[2]),
                    "persistence_uncertain",
                )
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
            if published or (store is not None and store._guarded_persist_renamed)
            else getattr(error, "code", "proof_unavailable")
        )
        raise ReconciliationError(code, operation_id=record.operation_id) from None


def _title_binding(title):
    if title is None:
        return None
    return (
        title[0].to_dict(),
        title[2].to_dict(),
        title[3].to_dict(),
        title[4].to_dict(),
        str(title[5]),
        title[6],
        title[7],
        title[8],
    )


def cancel_recovered_ready(
    operation_id,
    *,
    store_path,
    expected_record_sha256=None,
    expected_store_uuid=None,
):
    _identifier(operation_id)
    _path(store_path)
    _validate_cancel_expectations(expected_record_sha256, expected_store_uuid)
    view = LineageStore.inspect_existing(store_path)
    operation = _operation(view, operation_id)
    _require(
        operation is not None
        and operation.durable_state is OperationState.READY
        and operation.durable_native_state is NativeState.NATIVE_NOT_STARTED,
        "not_cancelable",
    )
    record = operation.record
    if expected_record_sha256 is not None:
        _require(
            view.store_uuid == expected_store_uuid and _sha(record) == expected_record_sha256,
            "ledger_changed",
        )
    with (
        LineageStore.open_existing(store_path) as store,
        store.lock_operation(operation_id, record.target_namespace) as guard,
    ):
        _require(
            store._store_uuid == view.store_uuid
            and _sha(_operation(store.recovery_view(), operation_id)) == _sha(operation),
            "ledger_changed",
        )
        store.cancel_ready(
            operation_id,
            guard,
            expected_record_sha256=expected_record_sha256,
            expected_store_uuid=expected_store_uuid,
        )
