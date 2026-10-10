"""Issued, fresh readonly endpoint observations; no ledger or native publication.

File capture performs reads. OpenCode's selected export may use private scratch
and touch its cache/WAL; these effects are declared, not hidden as zero FS I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from time import monotonic_ns
from types import MappingProxyType
from weakref import WeakKeyDictionary

from session_migrate.canonical import CanonicalPolicy, _json_bytes
from session_migrate.catalog import Catalog
from session_migrate.continuity_bindings import (
    EXECUTION_ENVIRONMENT,
    PUBLIC_ENVIRONMENT,
    LegacyRuntimeContext,
    ProfileBindingKind,
    RuntimeBindingError,
    _legacy_runtime_environment,
    _legacy_runtime_fingerprint,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.jsonl import DEFAULT_MAX_TOTAL_BYTES
from session_migrate.lineage import QualifiedIdentity
from session_migrate.model import AgentFormat
from session_migrate.native_audit import (
    AuditPolicy,
    CanonicalObservation,
    audit_snapshot,
    canonicalize_snapshot,
    validate_observation,
)
from session_migrate.native_namespaces import (
    _bind_execution_context,
    qualify_namespace,
    reserve_namespace,
)
from session_migrate.native_snapshots import (
    NativeSnapshot,
    SourceCaptureContext,
    capture_source,
    check_source_identity,
)
from session_migrate.profiles import (
    Profile,
    SessionRef,
    _source_environment,
    resolve_source,
)

_ENVIRONMENT_KEYS = PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT


class ContinuityObservationError(SessionMigrateError):
    def __init__(
        self,
        code: str = "observation_unproven",
        *,
        native_read_effects: tuple[str, ...] = (),
    ) -> None:
        self.code = (
            code
            if code
            in {
                "observation_unproven",
                "observer_changed",
                "endpoint_unavailable",
                "identity_unproven",
                "unsupported_endpoint",
                "policy_changed",
            }
            else "observation_unproven"
        )
        super().__init__(f"Continuity observation: {self.code}.")
        self.native_read_effects = native_read_effects


def _require(condition: bool, code: str = "observation_unproven") -> None:
    if not condition:
        raise ContinuityObservationError(code)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class EndpointObserver:
    """A selected catalog/profile driver, never an arbitrary observer callback."""

    _catalog: Catalog
    _profiles: tuple[Profile, ...]
    _environment: Mapping[str, str]
    _byte_limit: int
    _legacy_runtime_contexts: tuple[LegacyRuntimeContext, ...]

    def __init__(
        self,
        catalog: Catalog,
        profiles: tuple[Profile, ...],
        *,
        base_environ: Mapping[str, str] | None = None,
        byte_limit: int = DEFAULT_MAX_TOTAL_BYTES,
        legacy_runtime_contexts: tuple[LegacyRuntimeContext, ...] = (),
    ) -> None:
        _require(type(catalog) is Catalog and catalog.path.is_file(), "endpoint_unavailable")
        _require(
            type(profiles) is tuple and bool(profiles) and all(type(p) is Profile for p in profiles)
        )
        _require(len({p.id for p in profiles}) == len(profiles))
        _require(type(byte_limit) is int and 0 < byte_limit <= DEFAULT_MAX_TOTAL_BYTES)
        environment = dict(base_environ or {})
        _require(
            type(legacy_runtime_contexts) is tuple
            and all(type(c) is LegacyRuntimeContext for c in legacy_runtime_contexts)
        )
        _require(
            len({c.profile_id for c in legacy_runtime_contexts}) == len(legacy_runtime_contexts)
        )
        for context in legacy_runtime_contexts:
            matches = [p for p in profiles if p.id == context.profile_id]
            _require(len(matches) == 1)
            _legacy_runtime_environment(matches[0], context, environment)
        _require(
            all(
                type(k) is str and k in _ENVIRONMENT_KEYS and type(v) is str
                for k, v in environment.items()
            )
        )
        for name, value in {
            "_catalog": catalog,
            "_profiles": profiles,
            "_environment": MappingProxyType(environment),
            "_byte_limit": byte_limit,
            "_legacy_runtime_contexts": legacy_runtime_contexts,
        }.items():
            object.__setattr__(self, name, value)
        _OBSERVERS[self] = _observer_binding(self)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class EndpointObservation:
    identity: QualifiedIdentity
    observation: CanonicalObservation
    authority_sha256: str
    native_read_effects: tuple[str, ...]
    profile_binding_kind: ProfileBindingKind
    current_profile_fingerprint: str
    current_profile_id: str

    def __init__(self) -> None:
        raise ContinuityObservationError()


@dataclass(frozen=True, slots=True, repr=False)
class _IssuedObservation:
    observer: EndpointObserver
    ref_binding: str
    snapshot: NativeSnapshot
    execution_context: object
    session: object
    profile: Profile
    identity_fingerprint: str
    issued_ns: int
    binding: str


_OBSERVERS: WeakKeyDictionary[EndpointObserver, str] = WeakKeyDictionary()
_OBSERVATIONS: WeakKeyDictionary[EndpointObservation, _IssuedObservation] = WeakKeyDictionary()


def _observer_binding(observer: EndpointObserver) -> str:
    return hashlib.sha256(
        _json_bytes(
            {
                "catalog": str(observer._catalog.path),
                "profiles": [p.fingerprint for p in observer._profiles],
                "environment": dict(observer._environment),
                "limit": observer._byte_limit,
                "legacy_runtime_contexts": [
                    {
                        "profile_id": context.profile_id,
                        "environment": dict(context.environment),
                    }
                    for context in observer._legacy_runtime_contexts
                ],
            }
        )
    ).hexdigest()


def _ref_binding(ref: SessionRef) -> str:
    _require(type(ref) is SessionRef)
    return hashlib.sha256(
        _json_bytes(
            {
                "format": ref.format.value,
                "root": str(ref.root),
                "native_id": ref.native_id,
                "profile": ref.profile_id,
                "fingerprint": ref.profile_fingerprint,
                "catalog_id": ref.catalog_id,
            }
        )
    ).hexdigest()


def validate_observer(observer: EndpointObserver) -> None:
    _require(type(observer) is EndpointObserver and observer in _OBSERVERS)
    _require(_observer_binding(observer) == _OBSERVERS[observer], "observer_changed")


def resolve_profile(observer: EndpointObserver, ref: SessionRef) -> Profile:
    validate_observer(observer)
    _ref_binding(ref)
    matches = [
        p
        for p in observer._profiles
        if (
            p.format is ref.format
            and p.root == ref.root
            and (ref.profile_id is None or p.id == ref.profile_id)
        )
    ]
    _require(len(matches) == 1, "endpoint_unavailable")
    profile = matches[0]
    _require(
        ref.profile_fingerprint is None or ref.profile_fingerprint == profile.fingerprint,
        "identity_unproven",
    )
    return profile


def _profile_binding(observer: EndpointObserver, profile: Profile):
    validate_observer(observer)
    contexts = [c for c in observer._legacy_runtime_contexts if c.profile_id == profile.id]
    if contexts:
        try:
            environment = _legacy_runtime_environment(profile, contexts[0], observer._environment)
            fingerprint = _legacy_runtime_fingerprint(profile, environment)
        except (RuntimeBindingError, OSError, ValueError):
            raise ContinuityObservationError("identity_unproven") from None
        return (
            ProfileBindingKind.LEGACY_RUNTIME_V1,
            fingerprint,
            environment,
        )
    return (
        ProfileBindingKind.PROFILE_V1,
        profile.fingerprint,
        _source_environment(profile, observer._environment),
    )


def effective_profile_fingerprint(observer: EndpointObserver, ref: SessionRef) -> str:
    profile = resolve_profile(observer, ref)
    return _profile_binding(observer, profile)[1]


def ref_for_identity(observer: EndpointObserver, identity: QualifiedIdentity) -> SessionRef:
    validate_observer(observer)
    _require(type(identity) is QualifiedIdentity)
    matches = [
        p
        for p in observer._profiles
        if (
            p.format.value == identity.namespace.format
            and str(p.root) == identity.namespace.canonical_root
            and _profile_binding(observer, p)[1] == identity.profile_fingerprint
        )
    ]
    _require(len(matches) == 1, "identity_unproven")
    p = matches[0]
    return SessionRef(
        p.format,
        p.root,
        identity.namespace.native_id,
        p.id,
        profile_fingerprint=p.fingerprint,
    )


def _observation_binding(value: EndpointObservation) -> str:
    return hashlib.sha256(
        _json_bytes(
            {
                "identity": {
                    "namespace": asdict(value.identity.namespace),
                    "profile": value.identity.profile_fingerprint,
                    "root": asdict(value.identity.root_witness),
                },
                "authority": value.authority_sha256,
                "observation": id(value.observation),
                "effects": list(value.native_read_effects),
                "profile_binding_kind": value.profile_binding_kind.value,
                "current_profile_fingerprint": value.current_profile_fingerprint,
                "current_profile_id": value.current_profile_id,
            }
        )
    ).hexdigest()


def observe_endpoint(
    observer: EndpointObserver,
    ref: SessionRef,
    *,
    client_version: str | None,
    canonical_policy: CanonicalPolicy,
    on_read_effect: Callable[[str], None] | None = None,
) -> EndpointObservation:
    """Capture through the actual selected loader, then issue private proof."""
    profile = resolve_profile(observer, ref)
    effects: list[str] = []

    def declare(effect: str) -> None:
        if effect not in effects:
            effects.append(effect)
        if on_read_effect is not None:
            on_read_effect(effect)

    try:
        source = resolve_source(observer._catalog, profile, ref)
        namespace = reserve_namespace(ref.format, ref.root, ref.native_id)
        binding_kind, identity_fingerprint, values = _profile_binding(observer, profile)
        if ref.format in {AgentFormat.CLAUDE, AgentFormat.CODEX}:
            _require(source.path is not None, "endpoint_unavailable")
            declare("file_capture_read_attempted")
            snapshot = capture_source(
                SourceCaptureContext(ref.format, source.path),
                byte_limit=observer._byte_limit,
            )
            session = snapshot.parse()
            export_proof = None
            declare("file_capture_read")
        elif ref.format is AgentFormat.OPENCODE:
            from session_migrate.conversion import load_opencode_session

            captures, proofs = [], []
            declare("native_export_read_attempted")
            declare("private_scratch_possible")
            declare("native_cache_or_wal_possible")
            session = load_opencode_session(
                ref.native_id,
                source_cli=profile.cli,
                environ=values,
                max_source_bytes=observer._byte_limit,
                on_capture=captures.append,
                on_native_observation=proofs.append,
            )
            _require(len(captures) == len(proofs) == 1)
            snapshot, export_proof = captures[0], proofs[0]
            _require(session.cli_version == client_version, "policy_changed")
            declare("native_export_read")
            declare("private_scratch")
        else:
            raise ContinuityObservationError("unsupported_endpoint")
        audit = audit_snapshot(snapshot, AuditPolicy(client_version=client_version))
        observation = canonicalize_snapshot(snapshot, audit, canonical_policy)
        _require(audit.client_id == client_version, "policy_changed")
        context = _bind_execution_context(
            namespace,
            session,
            native_path=session.source_path,
            environ=values,
            client=profile.cli,
            snapshot=snapshot,
            export_proof=export_proof,
        )
        identity = qualify_namespace(
            session,
            namespace,
            identity_fingerprint,
            native_path=session.source_path,
            execution_context=context,
        )
        _require(identity is not None, "identity_unproven")
        result = object.__new__(EndpointObservation)
        for name, value in {
            "identity": identity,
            "observation": observation,
            "authority_sha256": snapshot.authority_sha256,
            "native_read_effects": tuple(effects),
            "profile_binding_kind": binding_kind,
            "current_profile_fingerprint": profile.fingerprint,
            "current_profile_id": profile.id,
        }.items():
            object.__setattr__(result, name, value)
        _OBSERVATIONS[result] = _IssuedObservation(
            observer,
            _ref_binding(ref),
            snapshot,
            context,
            session,
            profile,
            identity_fingerprint,
            monotonic_ns(),
            _observation_binding(result),
        )
        validate_endpoint(result, observer=observer, ref=ref)
        return result
    except ContinuityObservationError as error:
        error.native_read_effects = tuple(effects)
        raise
    except (SessionMigrateError, OSError, ValueError, TypeError):
        raise ContinuityObservationError(
            "endpoint_unavailable", native_read_effects=tuple(effects)
        ) from None


def validate_endpoint(
    endpoint: EndpointObservation,
    *,
    observer: EndpointObserver,
    ref: SessionRef,
    issued_after_ns: int = 0,
) -> None:
    validate_observer(observer)
    _require(type(endpoint) is EndpointObservation and endpoint in _OBSERVATIONS)
    state = _OBSERVATIONS[endpoint]
    _require(
        state.observer is observer
        and state.ref_binding == _ref_binding(ref)
        and state.issued_ns >= issued_after_ns
    )
    validate_observation(endpoint.observation)
    _require(_observation_binding(endpoint) == state.binding)
    _require(hashlib.sha256(state.snapshot.raw_bytes).hexdigest() == endpoint.authority_sha256)
    if "file_capture_read" in endpoint.native_read_effects:
        check_source_identity(state.snapshot)
    identity = qualify_namespace(
        state.session,
        endpoint.identity.namespace,
        state.identity_fingerprint,
        native_path=state.session.source_path,
        execution_context=state.execution_context,
    )
    _require(identity == endpoint.identity, "identity_unproven")
