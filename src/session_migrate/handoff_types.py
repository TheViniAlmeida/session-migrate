"""Private handoff inputs; original native authority and USER bytes stay distinct."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from session_migrate.errors import SessionMigrateError
from session_migrate.jsonl import DEFAULT_MAX_TOTAL_BYTES
from session_migrate.lineage import DerivedInputIdentityV1, PolicyVersions, QualifiedIdentity
from session_migrate.model import Event, EventKind, Provenance, Role, Session
from session_migrate.native_snapshots import NativeSnapshot

if TYPE_CHECKING:
    from session_migrate.continuity_observation import EndpointObservation, EndpointObserver
    from session_migrate.profiles import Profile, SessionRef

HANDOFF_POLICY = "user-handoff-v1"
HANDOFF_PREFIX = "User-provided handoff; historical transcript omitted"
MAX_DOCUMENT_BYTES = 1024 * 1024
_SEAL = object()


class HandoffError(SessionMigrateError):
    def __init__(self, code="invalid_handle"):
        self.code = (
            code
            if code
            in {
                "invalid_handle",
                "handle_capacity",
                "invalid_request",
                "selection_invalid",
                "limit_exceeded",
                "source_changed",
                "invalid_document",
                "digest_mismatch",
                "unsafe_file",
                "namespace_changed",
                "export_failed",
                "consent_required",
                "provider_unavailable",
                "provider_failed",
                "provider_timeout",
                "cancelled",
            }
            else "invalid_request"
        )
        super().__init__(f"Handoff: {self.code}.")


def _require(condition, code="invalid_request"):
    if not condition:
        raise HandoffError(code)


@dataclass(frozen=True, slots=True, repr=False)
class HandoffLimits:
    max_source_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_events: int = 1024
    max_ranges: int = 1024
    max_excerpt_bytes: int = 512 * 1024
    max_document_bytes: int = MAX_DOCUMENT_BYTES
    max_edit_bytes: int = MAX_DOCUMENT_BYTES

    def __post_init__(self):
        for value, cap in (
            (self.max_source_bytes, DEFAULT_MAX_TOTAL_BYTES),
            (self.max_events, 1024),
            (self.max_ranges, 1024),
            (self.max_excerpt_bytes, MAX_DOCUMENT_BYTES),
            (self.max_document_bytes, MAX_DOCUMENT_BYTES),
            (self.max_edit_bytes, MAX_DOCUMENT_BYTES),
        ):
            _require(type(value) is int and 0 < value <= cap)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class OriginalNativeAuthority:
    identity: QualifiedIdentity
    authority_sha256: str
    versions: PolicyVersions
    _seal: object

    def __init__(self):
        raise HandoffError("invalid_handle")

    @property
    def snapshot(self) -> NativeSnapshot:
        return _authority_state(self).snapshot

    @property
    def session(self) -> Session:
        # The native audit's original Session is bound to this exact capture;
        # its getter validates and deep-copies event payloads before exposing it.
        return _authority_state(self).endpoint.observation.session

    @property
    def observation(self):
        return _authority_state(self).endpoint.observation

    @property
    def profile(self) -> Profile:
        return _authority_state(self).profile

    @property
    def source_ref(self) -> SessionRef:
        return _authority_state(self).ref

    @property
    def observer(self) -> EndpointObserver:
        return _authority_state(self).observer

    @property
    def native_read_effects(self) -> tuple[str, ...]:
        return _authority_state(self).endpoint.native_read_effects

    def check(self):
        state = _authority_state(self)
        from session_migrate.continuity_observation import observe_endpoint, validate_endpoint

        try:
            fresh = observe_endpoint(
                state.observer,
                state.ref,
                client_version=self.versions.source_client,
                canonical_policy=state.endpoint.observation.policy,
            )
            validate_endpoint(fresh, observer=state.observer, ref=state.ref)
            _require(
                fresh.identity == self.identity
                and fresh.authority_sha256 == self.authority_sha256
                and fresh.current_profile_fingerprint == state.endpoint.current_profile_fingerprint
                and fresh.current_profile_id == state.endpoint.current_profile_id
                and fresh.observation.source_prefix == state.endpoint.observation.source_prefix,
                "source_changed",
            )
            _authority_state(self)
        except (SessionMigrateError, sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
            raise HandoffError("source_changed") from None


@dataclass(frozen=True, slots=True, repr=False)
class _AuthorityState:
    observer: EndpointObserver
    ref: SessionRef
    endpoint: EndpointObservation
    snapshot: NativeSnapshot
    session: Session
    profile: Profile
    binding: str


_AUTHORITIES: WeakKeyDictionary = WeakKeyDictionary()


def _authority_binding(authority, state):
    from session_migrate.lineage import _encode

    return hashlib.sha256(
        _encode(
            (
                authority.identity,
                authority.authority_sha256,
                authority.versions,
                id(state.observer),
                id(state.endpoint),
                id(state.snapshot),
                state.profile.fingerprint,
                str(state.ref.root),
                state.ref.native_id,
                state.ref.catalog_id,
                state.ref.profile_fingerprint,
            )
        )
    ).hexdigest()


def _authority_state(authority):
    _require(
        type(authority) is OriginalNativeAuthority
        and getattr(authority, "_seal", None) is _SEAL
        and authority in _AUTHORITIES,
        "invalid_handle",
    )
    state = _AUTHORITIES[authority]
    from session_migrate.continuity_observation import _OBSERVATIONS, validate_endpoint

    try:
        validate_endpoint(state.endpoint, observer=state.observer, ref=state.ref)
        issued = _OBSERVATIONS[state.endpoint]
        _require(
            issued.snapshot is state.snapshot
            and issued.session is state.session
            and issued.profile is state.profile
            and authority.identity == state.endpoint.identity
            and authority.authority_sha256 == state.snapshot.authority_sha256
            and hashlib.sha256(state.snapshot.raw_bytes).hexdigest() == authority.authority_sha256
            and _authority_binding(authority, state) == state.binding,
            "source_changed",
        )
    except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError):
        raise HandoffError("source_changed") from None
    return state


def _issue_original_authority(endpoint, observer, ref):
    # The private C1 registry owns the actual capture/session. No arbitrary
    # snapshot, source bytes, Session, hash or caller guard is accepted here.
    from session_migrate.continuity_observation import _OBSERVATIONS, validate_endpoint

    validate_endpoint(endpoint, observer=observer, ref=ref)
    issued = _OBSERVATIONS[endpoint]
    authority = object.__new__(OriginalNativeAuthority)
    for name, value in {
        "identity": endpoint.identity,
        "authority_sha256": endpoint.authority_sha256,
        "versions": PolicyVersions(
            endpoint.observation.policy.algorithm,
            HANDOFF_POLICY,
            "native-audit-v1",
            "native-audit-v1",
            endpoint.observation.audit.client_id,
            None,
        ),
        "_seal": _SEAL,
    }.items():
        object.__setattr__(authority, name, value)
    state = _AuthorityState(
        observer, ref, endpoint, issued.snapshot, issued.session, issued.profile, ""
    )
    state = _AuthorityState(
        observer,
        ref,
        endpoint,
        issued.snapshot,
        issued.session,
        issued.profile,
        _authority_binding(authority, state),
    )
    _AUTHORITIES[authority] = state
    _authority_state(authority)
    return authority


def _event_digest(event):
    _require(
        type(event) is Event
        and event.kind is EventKind.MESSAGE
        and event.role is Role.USER
        and event.timestamp is None
        and event.tool_name is None
        and event.tool_call_id is None
        and event.provenance == Provenance(0, "user_handoff", None)
        and type(event.text) is str
        and event.payload == {},
        "invalid_document",
    )
    payload = json.dumps(
        {
            "kind": event.kind.value,
            "role": event.role.value,
            "text": event.text,
            "content_blocks": [{"type": "text", "text": event.text}],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(b"user-handoff-v1\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class DerivedUserHandoffInput:
    identity: DerivedInputIdentityV1
    event: Event
    _document: bytes
    _seal: object

    def __init__(self):
        raise HandoffError("invalid_handle")

    @property
    def document_bytes(self):
        validate_derived_input(self)
        return self._document

    @property
    def events(self):
        validate_derived_input(self)
        return (copy.deepcopy(self.event),)


_INPUTS: WeakKeyDictionary = WeakKeyDictionary()


def validate_derived_input(value):
    _require(
        type(value) is DerivedUserHandoffInput
        and getattr(value, "_seal", None) is _SEAL
        and value in _INPUTS,
        "invalid_handle",
    )
    _require(
        type(value._document) is bytes
        and 0 < len(value._document) <= MAX_DOCUMENT_BYTES
        and value.identity
        == DerivedInputIdentityV1(
            "user_handoff", hashlib.sha256(value._document).hexdigest(), _event_digest(value.event)
        )
        and _INPUTS[value] == (value.identity.document_sha256, value.identity.event_sha256)
        and value.event.text == HANDOFF_PREFIX + "\n\n" + value._document.decode("utf-8"),
        "invalid_document",
    )


def _issue_user_input(document):
    _require(type(document) is bytes and 0 < len(document) <= MAX_DOCUMENT_BYTES, "limit_exceeded")
    try:
        text = document.decode("utf-8")
    except UnicodeError:
        raise HandoffError("invalid_document") from None
    event = Event(
        EventKind.MESSAGE,
        Provenance(0, "user_handoff", None),
        Role.USER,
        text=HANDOFF_PREFIX + "\n\n" + text,
    )
    identity = DerivedInputIdentityV1(
        "user_handoff", hashlib.sha256(document).hexdigest(), _event_digest(event)
    )
    result = object.__new__(DerivedUserHandoffInput)
    for name, value in {
        "identity": identity,
        "event": event,
        "_document": document,
        "_seal": _SEAL,
    }.items():
        object.__setattr__(result, name, value)
    _INPUTS[result] = (identity.document_sha256, identity.event_sha256)
    validate_derived_input(result)
    return result
