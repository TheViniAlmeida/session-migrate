"""Reconstruct volatile projection authority from original, captured file bytes.

This is a private readonly seam for a future reconciliation service. It never
serializes, installs, acknowledges, finalizes titles or resumes an operation.
Its certificates mean reconstructed_now; native identity, boundary and CAS are
separate authorities. Caller metadata and hashes alone cannot issue a handle.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from weakref import WeakKeyDictionary

from session_migrate.catalog import Catalog, CatalogEntry, CatalogTransferSource
from session_migrate.continuity_observation import (
    _OBSERVATIONS,
    EndpointObservation,
    EndpointObserver,
    observe_endpoint,
    validate_endpoint,
    validate_observer,
)
from session_migrate.conversion import ConversionOptions, default_target_home
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage, OperationRecord
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_audit import validate_observation
from session_migrate.native_namespaces import (
    _bind_execution_context,
    qualify_namespace,
    reserve_namespace,
)
from session_migrate.native_snapshots import (
    CaptureProvenance,
    NativeSnapshot,
    SourceCaptureContext,
    capture_source,
    check_source_identity,
)
from session_migrate.operations import (
    _PUBLIC_ENVIRONMENT,
    OperationKind,
    OperationRequest,
    _canonical_snapshot_sha,
    _digest,
    _public_options,
    _source_witness,
    _validate_title_request,
)
from session_migrate.profiles import Profile, SessionRef, resolve_source
from session_migrate.projection_policies import (
    ExpectedProjection,
    ProjectionPolicy,
    expected_from_source,
)
from session_migrate.projections import (
    ProjectionContext,
    ProjectionVerification,
    _expectation_coverage,
    _expectation_sha,
    _observation_dict,
    _observe,
    _options_dict,
    _same_semantics,
    _sha,
)

_SEAL = object()
_FORMATS = frozenset({AgentFormat.CLAUDE, AgentFormat.CODEX})


class RecoveryProjectionError(SessionMigrateError):
    """Fixed, content-free errors, including explicit unavailable gates."""

    def __init__(self, code: str):
        allowed = {
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
        }
        self.code = code if code in allowed else "proof_unavailable"
        super().__init__(f"Recovered projection: {self.code}.")


def _require(condition: bool, code: str = "context_mismatch") -> None:
    if not condition:
        raise RecoveryProjectionError(code)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class RecoveredProjectionContext:
    """Issued from the normalized original request and actual public context."""

    projection_context: ProjectionContext
    _request: OperationRequest
    _source_environment: tuple[tuple[str, str], ...]
    _target_environment: tuple[tuple[str, str], ...]
    _source_profile: Profile | None
    _target_profile: Profile | None
    _policy: ProjectionPolicy
    _source: NativeSnapshot
    _input: NativeSnapshot
    _record: OperationRecord
    _witness_sha256: str
    _options: ConversionOptions
    _selection: _RecoverySelection | None
    _seal: object

    def __init__(self):
        raise RecoveryProjectionError("context_unavailable")


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class ReconstructedProjectionCertificate:
    binding_sha256: str
    relation_sha256: str
    artifact_sha256: str
    _seal: object

    def __init__(self):
        raise RecoveryProjectionError("invalid_certificate")

    @property
    def provenance(self) -> str:
        return "reconstructed_now"

    @property
    def delta_eligible(self) -> bool:
        return False

    @property
    def identity_proven(self) -> bool:
        return False

    @property
    def boundary_proven(self) -> bool:
        return False

    @property
    def native_cas_proven(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class ReconstructedProjection:
    artifact_sha256: str
    source_authority_sha256: str
    binding_sha256: str
    expected: ExpectedProjection
    source_coverage: Coverage
    expectation_coverage: Coverage
    certificate: ReconstructedProjectionCertificate | None
    _context: RecoveredProjectionContext
    _source_observation: object
    _artifact_observation: object
    _expected_sha256: str
    _body: bytes
    _seal: object

    def __init__(self):
        raise RecoveryProjectionError("invalid_certificate")

    @property
    def provenance(self) -> str:
        return "reconstructed_now"

    @property
    def artifact_bytes(self) -> bytes:
        validate_recovered_projection(self)
        return self._body

    @property
    def source_observation(self):
        validate_recovered_projection(self)
        return self._source_observation

    @property
    def artifact_observation(self):
        validate_recovered_projection(self)
        return self._artifact_observation

    @property
    def source_context(self):
        validate_recovered_projection(self)


_CONTEXTS: WeakKeyDictionary = WeakKeyDictionary()
_PROJECTIONS: WeakKeyDictionary = WeakKeyDictionary()
_CERTIFICATES: WeakKeyDictionary = WeakKeyDictionary()
_SELECTIONS: WeakKeyDictionary = WeakKeyDictionary()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class _RecoverySelection:
    _observer: EndpointObserver
    _request: OperationRequest
    _endpoint: EndpointObservation
    _profile: Profile
    _ref: SessionRef
    _entry: CatalogEntry
    _locator: CatalogTransferSource
    _connection: sqlite3.Connection
    _catalog_witness: tuple
    _seal: object

    def __init__(self):
        raise RecoveryProjectionError("context_unavailable")


def _new(cls, values):
    handle = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(handle, name, value)
    object.__setattr__(handle, "_seal", _SEAL)
    return handle


def _capture_binding(snapshot: NativeSnapshot) -> dict:
    _require(type(snapshot) is NativeSnapshot, "proof_unavailable")
    _require(snapshot.provenance is CaptureProvenance.FILE, "proof_unavailable")
    actual = hashlib.sha256(snapshot.raw_bytes).hexdigest()
    _require(actual == snapshot.authority_sha256, "binding_changed")
    _require(
        snapshot.identity is not None and snapshot.identity.links == 1,
        "proof_unavailable",
    )
    _require(len(snapshot.raw_bytes) == snapshot.identity.size, "binding_changed")
    return {
        "format": snapshot.source_format.value,
        "path": str(snapshot.source_path),
        "provenance": snapshot.provenance.value,
        "identity": asdict(snapshot.identity),
        "ancestors": [(str(a.path), a.device, a.inode) for a in snapshot.ancestors],
        "body": actual,
    }


def _require_existing_capture(snapshot):
    """Prove an existing FD capture rather than trusting a constructed DTO."""
    captured = capture_source(SourceCaptureContext(snapshot.source_format, snapshot.source_path))
    _require(_capture_binding(snapshot) == _capture_binding(captured), "binding_changed")


def _environment(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    _require(isinstance(values, Mapping), "context_unavailable")
    # Only the public allowlist is retained. Full process environments never bind
    # this handle or enter metadata, errors, repr or serialization.
    result = []
    for key in _PUBLIC_ENVIRONMENT:
        if key in values:
            value = values[key]
            _require(type(value) is str and bool(value), "context_unavailable")
            _require(
                Path(value).is_absolute() and ".." not in Path(value).parts,
                "context_unavailable",
            )
            result.append((key, value))
    _require(any(key == "HOME" for key, _ in result), "context_unavailable")
    return tuple(result)


def _profiles(source, target) -> dict:
    _require((source is None) == (target is None), "context_unavailable")
    result = {}
    for side, profile in (("source", source), ("target", target)):
        if profile is not None:
            _require(type(profile) is Profile, "context_unavailable")
            result.update(
                {
                    f"{side}_profile_id": profile.id,
                    f"{side}_profile_name": profile.name,
                    f"{side}_profile_fingerprint": profile.fingerprint,
                }
            )
    return result


def _catalog_witness(path):
    """Revision metadata correlated with C3's guarded physical image reads.

    The stat tuple is not catalog authority: only open_readonly_existing reads
    and validates its bytes. Retaining this revision also detects replacement
    of an issued selection's file or namespace by an otherwise identical image.
    """
    try:
        info = path.stat(follow_symlinks=False)
        ancestors = []
        for parent in path.parents:
            current = parent.stat(follow_symlinks=False)
            ancestors.append((str(parent), current.st_dev, current.st_ino, current.st_mode))
    except OSError:
        raise RecoveryProjectionError("context_unavailable") from None
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        tuple(ancestors),
    )


def _selection_from_catalog(catalog, observer, request, *, validate_native):
    """Resolve a validated physical image, without using the observer's cache."""
    catalog_id = request.catalog_id
    if request.title:
        title = request.title.strip()
        _require(bool(title), "context_unavailable")
        matches = catalog.list_sessions(
            query=title,
            include_paths=True,
            agent_format=request.source_format,
            limit=10_000,
        )
        _require(len(matches) < 10_000, "context_unavailable")
        exact = [e for e in matches if e.title and e.title.casefold() == title.casefold()]
        candidates = exact or matches
        _require(len(candidates) == 1, "context_unavailable")
        catalog_id = candidates[0].catalog_id
    entry = catalog.get_session(catalog_id, include_paths=True)
    _require(entry.format in {f.value for f in _FORMATS}, "proof_unavailable")
    _require(
        entry.session_id is not None and entry.root is not None and entry.path is not None,
        "context_unavailable",
    )
    matches = [
        p
        for p in observer._profiles
        if p.format.value == entry.format and str(p.root) == entry.root
    ]
    _require(len(matches) == 1, "context_unavailable")
    profile = matches[0]
    ref = SessionRef(
        profile.format,
        profile.root,
        entry.session_id,
        profile.id,
        catalog_id=entry.catalog_id,
        profile_fingerprint=profile.fingerprint,
    )
    if validate_native:
        locator = resolve_source(catalog, profile, ref)
    else:
        # No further native filesystem I/O after the final physical catalog
        # capture. The first pass already used the real profile resolver.
        roots = [
            r
            for r in catalog.roots(enabled_only=True)
            if r.format == profile.format.value and Path(r.path) == profile.root
        ]
        _require(len(roots) == 1, "context_unavailable")
        locator = catalog.session_source_for_transfer(entry.catalog_id)
    _require(
        locator.format is profile.format
        and locator.root == profile.root
        and locator.session_id == ref.native_id
        and locator.path == Path(entry.path),
        "context_mismatch",
    )
    return entry, locator, profile, ref


def _selection_metadata(selected):
    entry, locator, profile, ref = selected
    return (
        asdict(entry),
        locator,
        profile.to_dict(),
        ref.format,
        ref.root,
        ref.native_id,
        ref.profile_id,
        ref.catalog_id,
        ref.profile_fingerprint,
    )


def _catalog_selection(observer, request):
    """Correlate the selector with two fresh guarded physical catalog images."""
    _require(type(observer) is EndpointObserver, "context_unavailable")
    _require(
        type(observer._catalog) is Catalog
        and type(observer._profiles) is tuple
        and all(type(p) is Profile for p in observer._profiles),
        "context_unavailable",
    )
    validate_observer(observer)
    _require(
        request.command is OperationKind.TRANSFER
        and request.catalog == observer._catalog.path
        and bool(request.catalog_id) != bool(request.title)
        and request.source_id is None
        and request.source_home is None
        and request.source_cwd is None,
        "context_unavailable",
    )
    connection = observer._catalog._connection
    _require(isinstance(connection, sqlite3.Connection), "context_unavailable")
    try:
        connection.execute("SELECT 1").fetchone()  # Liveness, never selector authority.
    except sqlite3.Error:
        raise RecoveryProjectionError("context_unavailable") from None
    witness = _catalog_witness(request.catalog)
    with Catalog.open_readonly_existing(request.catalog) as catalog:
        selected = _selection_from_catalog(catalog, observer, request, validate_native=True)
    with Catalog.open_readonly_existing(request.catalog) as catalog:
        current = _selection_from_catalog(catalog, observer, request, validate_native=False)
    try:
        cached = _selection_from_catalog(
            observer._catalog, observer, request, validate_native=False
        )
    except sqlite3.Error:
        raise RecoveryProjectionError("context_unavailable") from None
    _require(
        _selection_metadata(current) == _selection_metadata(selected)
        and _selection_metadata(cached) == _selection_metadata(current)
        and _catalog_witness(request.catalog) == witness
        and observer._catalog._connection is connection,
        "binding_changed",
    )
    validate_observer(observer)
    return *current, witness


def _selection_binding(selection):
    return _sha(
        {
            "observer": id(selection._observer),
            "catalog": id(selection._observer._catalog),
            "connection": id(selection._observer._catalog._connection),
            "issued_connection": id(selection._connection),
            "catalog_witness": selection._catalog_witness,
            "request": {
                f.name: str(v) if isinstance(v, Path) else v
                for f in fields(selection._request)
                if (v := getattr(selection._request, f.name)) is not None
            },
            "entry": asdict(selection._entry),
            "locator": {
                "format": selection._locator.format.value,
                "root": str(selection._locator.root),
                "path": str(selection._locator.path),
                "native_id": selection._locator.session_id,
            },
            "profile": selection._profile.to_dict(),
            "ref": {
                "format": selection._ref.format.value,
                "root": str(selection._ref.root),
                "native_id": selection._ref.native_id,
                "profile": selection._ref.profile_id,
                "fingerprint": selection._ref.profile_fingerprint,
                "catalog_id": selection._ref.catalog_id,
            },
            "endpoint": id(selection._endpoint),
        }
    )


def _validate_selection(selection):
    _require(
        type(selection) is _RecoverySelection
        and getattr(selection, "_seal", None) is _SEAL
        and selection in _SELECTIONS,
        "context_unavailable",
    )
    _require(_selection_binding(selection) == _SELECTIONS[selection], "binding_changed")
    _require(
        selection._observer._catalog._connection is selection._connection,
        "binding_changed",
    )
    validate_endpoint(selection._endpoint, observer=selection._observer, ref=selection._ref)
    entry, locator, profile, ref, witness = _catalog_selection(
        selection._observer, selection._request
    )
    _require(
        entry == selection._entry
        and locator == selection._locator
        and profile.to_dict() == selection._profile.to_dict()
        and ref.catalog_id == selection._ref.catalog_id
        and ref.profile_fingerprint == selection._ref.profile_fingerprint,
        "binding_changed",
    )
    _require(witness == selection._catalog_witness, "binding_changed")
    # Catalog capture/query work is I/O too. Prove full native bytes after it,
    # then ensure the physical image/connection survived those native reads.
    _require_existing_capture(_OBSERVATIONS[selection._endpoint].snapshot)
    validate_endpoint(selection._endpoint, observer=selection._observer, ref=selection._ref)
    _require(
        _selection_binding(selection) == _SELECTIONS[selection]
        and _catalog_witness(selection._request.catalog) == witness,
        "binding_changed",
    )


def _issue_selection(observer, request, source, record, source_profile, policy, on_read_effect):
    connection = observer._catalog._connection if type(observer) is EndpointObserver else None
    entry, locator, profile, ref, witness = _catalog_selection(observer, request)
    _require(
        source_profile is None or source_profile.to_dict() == profile.to_dict(),
        "context_mismatch",
    )
    _require(locator.path == source.source_path, "context_mismatch")
    endpoint = observe_endpoint(
        observer,
        ref,
        client_version=record.versions.source_client,
        canonical_policy=policy.canonical_policy,
        on_read_effect=on_read_effect,
    )
    validate_endpoint(endpoint, observer=observer, ref=ref)
    captured = _OBSERVATIONS[endpoint].snapshot
    _require(_capture_binding(captured) == _capture_binding(source), "binding_changed")
    _require(
        record.source.identity is None or endpoint.identity == record.source.identity,
        "context_mismatch",
    )
    selection = _new(
        _RecoverySelection,
        {
            "_observer": observer,
            "_request": request,
            "_endpoint": endpoint,
            "_profile": profile,
            "_ref": ref,
            "_entry": entry,
            "_locator": locator,
            "_connection": connection,
            "_catalog_witness": witness,
        },
    )
    _SELECTIONS[selection] = _selection_binding(selection)
    _validate_selection(selection)
    return selection


def _fingerprint(fmt, root, environment, profile):
    return (
        profile.fingerprint
        if profile is not None
        else _digest(
            {
                "format": fmt.value,
                "environment": dict(environment),
                "root": str(root),
            }
        )
    )


def _context_binding(context):
    profiles = _profiles(context._source_profile, context._target_profile)
    return _sha(
        {
            "source": _capture_binding(context._source),
            "input": _capture_binding(context._input),
            "record": asdict(context._record),
            "request": _public_options(
                context._request,
                dict(context._target_environment),
                dict(context._source_environment),
                profiles,
            ),
            "request_dry_run": context._request.dry_run,
            "source_profile": context._source_profile.to_dict()
            if context._source_profile is not None
            else None,
            "target_profile": context._target_profile.to_dict()
            if context._target_profile is not None
            else None,
            "policy": context._policy.sha256,
            "projection_context": asdict(context.projection_context),
            "options": _options_dict(context._options),
            "witness": context._witness_sha256,
            "selection": _selection_binding(context._selection)
            if context._selection is not None
            else None,
        }
    )


def _validate_context(context):
    _require(
        type(context) is RecoveredProjectionContext and getattr(context, "_seal", None) is _SEAL,
        "context_unavailable",
    )
    _require(context in _CONTEXTS, "context_unavailable")
    _require(_CONTEXTS[context] == _context_binding(context), "binding_changed")
    context._policy.__post_init__()
    context.projection_context.__post_init__()
    context._record.__post_init__()
    if context._selection is not None:
        _require(context._selection._request is context._request, "binding_changed")
        _validate_selection(context._selection)
        _require_existing_capture(context._source)
        _require_existing_capture(context._input)
        check_source_identity(context._source)
        check_source_identity(context._input)
        _require(
            _catalog_witness(context._request.catalog) == context._selection._catalog_witness
            and _CONTEXTS[context] == _context_binding(context),
            "binding_changed",
        )


def _native_metadata(snapshot, session, client):
    _require(session.session_id is not None and session.cwd is not None, "metadata_mismatch")
    _require(session.cli_version == client, "metadata_mismatch")
    for record in snapshot.decoded_records():
        value = record.value
        if snapshot.source_format is AgentFormat.CLAUDE:
            for key, wanted in (
                ("sessionId", session.session_id),
                ("cwd", str(session.cwd)),
                ("version", client),
            ):
                _require(key not in value or value[key] == wanted, "metadata_mismatch")
        elif value.get("type") == "session_meta":
            payload = value.get("payload")
            _require(type(payload) is dict, "metadata_mismatch")
            for key, wanted in (
                ("id", session.session_id),
                ("session_id", session.session_id),
                ("cwd", str(session.cwd)),
                ("cli_version", client),
            ):
                _require(key not in payload or payload[key] == wanted, "metadata_mismatch")


def _issue_recovery_context(
    source_capture: NativeSnapshot,
    input_file_capture: NativeSnapshot,
    record: OperationRecord,
    request: OperationRequest,
    *,
    source_environment: Mapping[str, str],
    target_environment: Mapping[str, str],
    source_profile: Profile | None = None,
    target_profile: Profile | None = None,
    policy: ProjectionPolicy | None = None,
    observer: EndpointObserver | None = None,
    on_read_effect=None,
) -> RecoveredProjectionContext:
    """Reconciliation-driver seam; no caller hash, native export or write effects.

    The request must be the exact normalized original request, including the UUID
    generated before preparation. Catalog/title selectors without that selected
    source context remain unavailable. No client executable is invoked.
    """
    _capture_binding(source_capture)
    _capture_binding(input_file_capture)
    _require(
        type(record) is OperationRecord and type(request) is OperationRequest,
        "context_unavailable",
    )
    record.__post_init__()
    _require(
        source_capture.source_format in _FORMATS and input_file_capture.source_format in _FORMATS,
        "proof_unavailable",
    )
    _require_existing_capture(source_capture)
    _require_existing_capture(input_file_capture)
    _require(
        request.command in {OperationKind.IMPORT, OperationKind.TRANSFER},
        "context_unavailable",
    )
    for item in fields(request):
        value = getattr(request, item.name)
        if isinstance(value, Path):
            _require(value.is_absolute() and ".." not in value.parts, "context_unavailable")
    _require(request.session_id == record.target_namespace.native_id, "options_mismatch")
    _require(
        request.target_format is None
        or request.target_format.value == input_file_capture.source_format.value,
        "context_mismatch",
    )
    if request.target_format is None:
        _require(
            request.command is OperationKind.TRANSFER
            and source_capture.source_format is not input_file_capture.source_format,
            "context_unavailable",
        )
    _require(
        request.source_format is None or request.source_format is source_capture.source_format,
        "context_mismatch",
    )
    _require(request.source_cli is None and request.target_cli is None, "context_unavailable")
    _validate_title_request(request)
    policy = policy if policy is not None else ProjectionPolicy()
    _require(type(policy) is ProjectionPolicy, "unsupported_policy")
    policy.__post_init__()
    profiles = _profiles(source_profile, target_profile)
    selection = None
    if request.catalog_id or request.title:
        selection = _issue_selection(
            observer, request, source_capture, record, source_profile, policy, on_read_effect
        )
    versions = record.versions
    _require(
        versions.canonical_algorithm == policy.canonical_policy.algorithm
        and versions.projection_policy == policy.policy_id
        and versions.source_adapter == versions.target_adapter == "native-audit-v1",
        "unsupported_policy",
    )
    source_env, target_env = (
        _environment(source_environment),
        _environment(target_environment),
    )
    _require(
        _digest(_public_options(request, dict(target_env), dict(source_env), profiles))
        == record.options_sha256,
        "options_mismatch",
    )
    _require(
        input_file_capture.authority_sha256 == record.target_input_sha256,
        "input_mismatch",
    )
    _require(
        source_capture.authority_sha256 == record.source.artifact_sha256,
        "source_mismatch",
    )
    source_obs = _observe(source_capture, versions.source_client, policy)
    input_obs = _observe(input_file_capture, versions.target_client, policy)
    source_session, input_session = source_obs.session, input_obs.session
    _native_metadata(source_capture, source_session, versions.source_client)
    _native_metadata(input_file_capture, input_session, versions.target_client)
    _require(
        request.target_cli_version is None or request.target_cli_version == versions.target_client,
        "options_mismatch",
    )
    target_root = (
        request.home
        or default_target_home(input_file_capture.source_format, environ=dict(target_env))
    ).absolute()
    source_root = source_capture.source_path.parent
    if request.command is OperationKind.IMPORT:
        _require(request.path == source_capture.source_path, "context_mismatch")
        _require(record.source.identity is None, "context_mismatch")
    else:
        if selection is not None:
            source_root = selection._locator.root
            _require(
                selection._ref.native_id == source_session.session_id,
                "context_mismatch",
            )
        else:
            source_root = (
                request.source_home
                or default_target_home(source_capture.source_format, environ=dict(source_env))
            ).absolute()
            _require(request.source_id == source_session.session_id, "context_mismatch")
    for profile, fmt, root, environment in (
        (source_profile, source_capture.source_format, source_root, dict(source_env)),
        (
            target_profile,
            input_file_capture.source_format,
            target_root,
            dict(target_env),
        ),
    ):
        if profile is not None:
            _require(
                profile.format is fmt and profile.root == root and profile.cli is None,
                "context_mismatch",
            )
            _require(
                all(environment.get(k) == v for k, v in profile.environ.items()),
                "context_mismatch",
            )
    namespace = reserve_namespace(
        input_file_capture.source_format, target_root, input_session.session_id
    )
    _require(namespace == record.target_namespace, "metadata_mismatch")
    target_fingerprint = _fingerprint(
        input_file_capture.source_format, target_root, target_env, target_profile
    )
    _require(target_fingerprint == record.target_profile_fingerprint, "context_mismatch")
    if record.source.identity is not None:
        source_namespace = reserve_namespace(
            source_capture.source_format, source_root, source_session.session_id
        )
        execution = _bind_execution_context(
            source_namespace,
            source_session,
            native_path=source_capture.source_path,
            environ=dict(source_env),
            snapshot=source_capture,
        )
        qualified = qualify_namespace(
            source_session,
            source_namespace,
            _fingerprint(source_capture.source_format, source_root, source_env, source_profile),
            execution_context=execution,
        )
        _require(
            qualified is not None and qualified == record.source.identity,
            "context_mismatch",
        )
    witness = _source_witness(source_session, source_root)
    _require(witness == record.source.witness_sha256, "source_mismatch")
    _require_existing_capture(source_capture)
    _require_existing_capture(input_file_capture)
    if selection is not None:
        _validate_selection(selection)
    cwd = request.cwd or source_session.cwd
    _require(cwd is not None and cwd.resolve() == input_session.cwd, "options_mismatch")
    options = ConversionOptions(
        TargetFormat(input_file_capture.source_format.value),
        session_id=request.session_id,
        cwd=input_session.cwd,
        target_cli_version=versions.target_client,
        model_provider=request.model_provider,
        model=request.model,
        target_title=request.target_title,
        title_policy=request.title_policy,
    )
    if input_file_capture.source_format is AgentFormat.CODEX:
        _require(
            input_session.model_provider == (request.model_provider or "openai"),
            "options_mismatch",
        )
        _require(input_session.title is None, "metadata_mismatch")
    else:
        _require(input_session.title == source_session.title, "metadata_mismatch")
        if request.model is not None and any(
            e.role is not None and e.role.value == "assistant" for e in source_obs.events
        ):
            _require(input_session.model == request.model, "options_mismatch")
    context = _new(
        RecoveredProjectionContext,
        {
            "projection_context": ProjectionContext(
                options.target_format,
                versions.target_client,
                target_fingerprint,
                _digest(asdict(namespace)),
            ),
            "_request": request,
            "_source_environment": source_env,
            "_target_environment": target_env,
            "_source_profile": source_profile,
            "_target_profile": target_profile,
            "_policy": policy,
            "_source": source_capture,
            "_input": input_file_capture,
            "_record": record,
            "_witness_sha256": witness,
            "_options": options,
            "_selection": selection,
        },
    )
    _CONTEXTS[context] = _context_binding(context)
    _validate_context(context)
    return context


def _prepare_recovery_context(
    source_capture: NativeSnapshot,
    input_file_capture: NativeSnapshot,
    record: OperationRecord,
    request: OperationRequest,
    *,
    source_environment: Mapping[str, str],
    target_environment: Mapping[str, str],
    source_profile: Profile | None = None,
    target_profile: Profile | None = None,
    policy: ProjectionPolicy | None = None,
    observer: EndpointObserver | None = None,
    on_read_effect=None,
) -> RecoveredProjectionContext:
    """Issue only validated context; loader/filesystem details stay private."""
    try:
        return _issue_recovery_context(
            source_capture,
            input_file_capture,
            record,
            request,
            source_environment=source_environment,
            target_environment=target_environment,
            source_profile=source_profile,
            target_profile=target_profile,
            policy=policy,
            observer=observer,
            on_read_effect=on_read_effect,
        )
    except RecoveryProjectionError:
        raise
    except (
        SessionMigrateError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        AttributeError,
        RecursionError,
    ):
        raise RecoveryProjectionError("proof_unavailable") from None


def _binding(prepared):
    return _sha(
        {
            "context": _context_binding(prepared._context),
            "source_observation": _observation_dict(prepared._source_observation),
            "input_observation": _observation_dict(prepared._artifact_observation),
            "source_fidelity": prepared._source_observation.fidelity.value,
            "input_fidelity": prepared._artifact_observation.fidelity.value,
            "source_prefix": asdict(prepared._source_observation.source_prefix),
            "input_prefix": asdict(prepared._artifact_observation.source_prefix),
            "expected": _expectation_sha(prepared.expected),
            "artifact": prepared.artifact_sha256,
            "source": prepared.source_authority_sha256,
            "source_coverage": prepared.source_coverage.value,
            "expectation_coverage": prepared.expectation_coverage.value,
            "body": hashlib.sha256(prepared._body).hexdigest(),
            "provenance": prepared.provenance,
        }
    )


def prepare_recovered_projection(source_capture, input_file_capture, record, context):
    """Audit existing input and derive its expectation independently from source."""
    _validate_context(context)
    _require(
        source_capture is context._source
        and input_file_capture is context._input
        and record is context._record,
        "context_mismatch",
    )
    policy, versions = context._policy, record.versions
    source_obs = _observe(source_capture, versions.source_client, policy)
    expected = expected_from_source(
        source_obs,
        target_format=context.projection_context.target_format,
        policy=policy,
    )
    input_obs = _observe(input_file_capture, versions.target_client, policy)
    _require(
        _same_semantics(expected.witness.prefix_witness, input_obs.witness.prefix_witness),
        "semantic_mismatch",
    )
    _require(
        record.source.prefix_witness is not None
        and record.source.snapshot_sha256 is not None
        and record.expected_projection.prefix_witness is not None
        and record.expected_projection.snapshot_sha256 is not None
        and record.expected_projection.relation_sha256 is not None,
        "proof_unavailable",
    )
    _require(
        record.source.prefix_witness == source_obs.source_prefix
        and record.source.snapshot_sha256 == _canonical_snapshot_sha(source_obs, versions),
        "source_mismatch",
    )
    coverage = _expectation_coverage(source_obs, input_obs, expected)
    _require(
        record.expected_projection.prefix_witness == input_obs.source_prefix
        and not record.expected_projection.unit_hashes
        and record.expected_projection.snapshot_sha256
        == _canonical_snapshot_sha(input_obs, versions)
        and record.expected_projection.relation_sha256 == expected.relation.chain_sha256
        and record.expected_projection.coverage is coverage,
        "input_mismatch",
    )
    prepared = _new(
        ReconstructedProjection,
        {
            "artifact_sha256": input_file_capture.authority_sha256,
            "source_authority_sha256": source_capture.authority_sha256,
            "binding_sha256": "",
            "expected": expected,
            "source_coverage": source_obs.coverage,
            "expectation_coverage": coverage,
            "certificate": None,
            "_context": context,
            "_source_observation": source_obs,
            "_artifact_observation": input_obs,
            "_expected_sha256": _expectation_sha(expected),
            "_body": input_file_capture.raw_bytes,
        },
    )
    binding = _binding(prepared)
    object.__setattr__(prepared, "binding_sha256", binding)
    if coverage is Coverage.VERIFIED and expected.relation_complete and expected.preserves_content:
        certificate = _new(
            ReconstructedProjectionCertificate,
            {
                "binding_sha256": binding,
                "relation_sha256": expected.relation.chain_sha256,
                "artifact_sha256": prepared.artifact_sha256,
            },
        )
        _CERTIFICATES[certificate] = (
            binding,
            certificate.relation_sha256,
            certificate.artifact_sha256,
        )
        object.__setattr__(prepared, "certificate", certificate)
    _PROJECTIONS[prepared] = (binding, prepared.certificate)
    return prepared


def validate_recovered_projection(prepared):
    """Revalidate private issuance and, when selected, its live catalog/capture."""
    _require(
        type(prepared) is ReconstructedProjection and getattr(prepared, "_seal", None) is _SEAL,
        "invalid_certificate",
    )
    _require(prepared in _PROJECTIONS, "invalid_certificate")
    _validate_context(prepared._context)
    validate_observation(prepared._source_observation)
    validate_observation(prepared._artifact_observation)
    _require(
        _expectation_sha(prepared.expected) == prepared._expected_sha256,
        "binding_changed",
    )
    binding = _binding(prepared)
    _require(
        binding == prepared.binding_sha256
        and _PROJECTIONS[prepared] == (binding, prepared.certificate),
        "binding_changed",
    )
    if prepared.certificate is not None:
        certificate = prepared.certificate
        _require(
            type(certificate) is ReconstructedProjectionCertificate
            and getattr(certificate, "_seal", None) is _SEAL
            and certificate in _CERTIFICATES,
            "invalid_certificate",
        )
        wanted = (
            binding,
            prepared.expected.relation.chain_sha256,
            prepared.artifact_sha256,
        )
        _require(
            _CERTIFICATES[certificate]
            == wanted
            == (
                certificate.binding_sha256,
                certificate.relation_sha256,
                certificate.artifact_sha256,
            ),
            "invalid_certificate",
        )


def verify_recovered_projection(prepared, observed_snapshot, *, context):
    """Compare separate observed target bytes without conferring effect authority."""
    validate_recovered_projection(prepared)
    _require(
        type(context) is ProjectionContext and context == prepared._context.projection_context,
        "context_mismatch",
    )
    _require(
        type(observed_snapshot) is NativeSnapshot
        and observed_snapshot.source_format.value == context.target_format.value,
        "context_mismatch",
    )
    observed = _observe(observed_snapshot, context.target_client, prepared._context._policy)
    actual, original = observed.session, prepared._artifact_observation.session
    _native_metadata(observed_snapshot, actual, context.target_client)
    metadata = actual.session_id == original.session_id and actual.cwd == original.cwd
    semantic = _same_semantics(
        prepared.expected.witness.prefix_witness, observed.witness.prefix_witness
    )
    structure = observed.audit.structure == prepared._artifact_observation.audit.structure
    fidelity = (
        observed.coverage is Coverage.VERIFIED
        and observed.fidelity is Coverage.VERIFIED
        and prepared._source_observation.fidelity is Coverage.VERIFIED
        and prepared.expectation_coverage is Coverage.VERIFIED
    )
    reason = "projection_matches"
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
    elif not fidelity:
        reason = "audit_partial"
    return ProjectionVerification(
        semantic and metadata,
        structure and metadata,
        prepared.expected.relation_complete,
        False,
        False,
        prepared.source_coverage,
        prepared.expectation_coverage,
        observed.coverage,
        semantic and metadata and structure and fidelity and prepared.expected.preserves_content,
        prepared.expected.loss_counts,
        prepared.expected.metadata_loss_counts,
        reason,
    )
