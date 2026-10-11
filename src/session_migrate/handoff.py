"""One-shot offline handoff editing and explicit local fake submission only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic_ns
from typing import Protocol
from weakref import WeakKeyDictionary

from session_migrate.canonical import CanonicalPolicy
from session_migrate.continuity_observation import (
    EndpointObserver,
    _original_authority,
    observe_endpoint,
    resolve_profile,
    validate_endpoint,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.handoff_types import (
    HANDOFF_POLICY,
    MAX_DOCUMENT_BYTES,
    DerivedUserHandoffInput,
    HandoffError,
    HandoffLimits,
    OriginalNativeAuthority,
    _issue_user_input,
    _require,
    validate_derived_input,
)
from session_migrate.lineage import (
    OmissionV1,
    SelectionRangeV1,
    TransformationV1,
    _encode,
    _PinnedNamespace,
)
from session_migrate.model import EventKind
from session_migrate.profiles import resolve_source

_MAX_HANDLES = 64
_SEAL = object()
_LOCK = threading.RLock()
_HANDLES: WeakKeyDictionary = WeakKeyDictionary()
_FAULT_HOOK = None
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class _Handle:
    __slots__ = ("_kind", "_binding", "_seal", "__weakref__")

    def __init__(self):
        raise HandoffError("invalid_handle")

    def __repr__(self):
        return f"<{type(self).__name__}>"

    def discard(self):
        with _LOCK:
            _HANDLES.pop(self, None)


class HandoffDraft(_Handle):
    __slots__ = ()


class ExportedDraft(_Handle):
    __slots__ = ()


class ReviewedHandoff(_Handle):
    __slots__ = ()


class HandoffSubmission(_Handle):
    __slots__ = ()


@dataclass(frozen=True, slots=True, repr=False)
class ProviderRequest:
    model_id: str
    input_bytes: bytes
    input_sha256: str
    max_input_bytes: int
    max_output_bytes: int
    deadline_ns: int
    budget_units: int
    credential_env_ref: str | None

    def __post_init__(self):
        _require(type(self.model_id) is str and 0 < len(self.model_id) <= 128)
        _require(type(self.input_bytes) is bytes)
        _require(self.input_sha256 == hashlib.sha256(self.input_bytes).hexdigest())
        for value, cap in (
            (self.max_input_bytes, MAX_DOCUMENT_BYTES),
            (self.max_output_bytes, MAX_DOCUMENT_BYTES),
            (self.budget_units, 2**31 - 1),
            (self.deadline_ns, 2**63 - 1),
        ):
            _require(type(value) is int and 0 < value <= cap)
        _require(len(self.input_bytes) <= self.max_input_bytes, "limit_exceeded")
        _require(
            self.credential_env_ref is None
            or (
                type(self.credential_env_ref) is str
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.credential_env_ref)
            )
        )


@dataclass(frozen=True, slots=True, repr=False)
class SubmissionConsent:
    document_sha256: str
    model_id: str
    max_output_bytes: int
    budget_units: int
    approved: bool


class HandoffProvider(Protocol):
    def generate(self, request: ProviderRequest) -> bytes: ...


@dataclass(frozen=True, slots=True, repr=False)
class FakeHandoffProvider:
    response: bytes
    model_id: str = "fake-local-v1"
    latency_ns: int = 0
    fail: bool = False

    def generate(self, request):
        _require(type(self.response) is bytes and type(self.fail) is bool)
        _require(type(self.latency_ns) is int and self.latency_ns >= 0)
        _require(request.model_id == self.model_id, "provider_unavailable")
        if monotonic_ns() + self.latency_ns >= request.deadline_ns:
            raise HandoffError("provider_timeout")
        if self.fail:
            raise HandoffError("provider_failed")
        return self.response


@dataclass(frozen=True, slots=True, repr=False)
class _State:
    kind: str
    authority: OriginalNativeAuthority
    selection: tuple[SelectionRangeV1, ...]
    omissions: tuple[OmissionV1, ...]
    limits: HandoffLimits
    document: bytes
    provenance: str = "user_supplied"
    path: Path | None = None
    ancestry: tuple = ()
    leaf_identity: tuple = ()
    derived_input: DerivedUserHandoffInput | None = None
    request: ProviderRequest | None = None


def _binding(state):
    request = state.request
    return hashlib.sha256(
        _encode(
            (
                state.kind,
                id(state.authority),
                state.authority.identity,
                state.authority.authority_sha256,
                state.authority.versions,
                state.selection,
                state.omissions,
                (
                    state.limits.max_source_bytes,
                    state.limits.max_events,
                    state.limits.max_ranges,
                    state.limits.max_excerpt_bytes,
                    state.limits.max_document_bytes,
                    state.limits.max_edit_bytes,
                ),
                hashlib.sha256(state.document).hexdigest(),
                state.provenance,
                str(state.path) if state.path else None,
                state.ancestry,
                state.leaf_identity,
                state.derived_input.identity if state.derived_input else None,
                None
                if request is None
                else (
                    request.model_id,
                    request.input_sha256,
                    request.max_input_bytes,
                    request.max_output_bytes,
                    request.deadline_ns,
                    request.budget_units,
                    request.credential_env_ref,
                ),
            )
        )
    ).hexdigest()


def _issue(cls, state):
    with _LOCK:
        _require(len(_HANDLES) < _MAX_HANDLES, "handle_capacity")
        handle = object.__new__(cls)
        handle._kind, handle._binding, handle._seal = state.kind, _binding(state), _SEAL
        _HANDLES[handle] = state
        return handle


def _state(handle, expected, *, consume=False):
    with _LOCK:
        _require(
            type(handle) is expected and handle in _HANDLES,
            "invalid_handle",
        )
        state = _HANDLES.pop(handle) if consume else _HANDLES[handle]
        _require(_matches(handle, state), "invalid_handle")
        return state


def _matches(handle, state):
    return (
        getattr(handle, "_seal", None) is _SEAL
        and type(getattr(handle, "_kind", None)) is str
        and handle._kind == state.kind
        and type(getattr(handle, "_binding", None)) is str
        and handle._binding == _binding(state)
    )


def _check_consumed(handle, state):
    _require(_matches(handle, state), "invalid_handle")
    state.authority.check()
    if state.derived_input is not None:
        validate_derived_input(state.derived_input)
    _require(_matches(handle, state), "invalid_handle")


def _event_text(event, block):
    if block is None:
        if event.text is not None:
            return event.text
        return json.dumps(event.payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    blocks = event.payload.get("content_blocks")
    if blocks is None and event.provenance.block_index == block:
        return _event_text(event, None)
    _require(type(blocks) is list and block < len(blocks), "selection_invalid")
    value = blocks[block]
    return (
        value.get("text")
        if type(value) is dict and type(value.get("text")) is str
        else json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


def prepare_handoff(
    source_ref, selection, limits, observer, *, client_version=None, canonical_policy=None
):
    _require(type(limits) is HandoffLimits)
    limits.__post_init__()
    _require(
        type(selection) is tuple and 0 < len(selection) <= limits.max_ranges, "selection_invalid"
    )
    for item in selection:
        _require(type(item) is SelectionRangeV1, "selection_invalid")
        item.__post_init__()
    profile = resolve_profile(observer, source_ref)
    observer = EndpointObserver(
        observer._catalog,
        observer._profiles,
        base_environ=observer._environment,
        byte_limit=min(observer._byte_limit, limits.max_source_bytes),
        legacy_runtime_contexts=observer._legacy_runtime_contexts,
    )
    if client_version is None:
        resolved = resolve_source(observer._catalog, profile, source_ref)
        entry = (
            observer._catalog.get_session(source_ref.catalog_id, include_paths=True)
            if source_ref.catalog_id
            else None
        )
        if entry is not None:
            client_version = entry.cli_version
        elif resolved.path is not None:
            from session_migrate.native_snapshots import SourceCaptureContext, capture_source

            captured = capture_source(
                SourceCaptureContext(source_ref.format, resolved.path),
                byte_limit=limits.max_source_bytes,
            )
            client_version = captured.parse().cli_version
    endpoint = observe_endpoint(
        observer,
        source_ref,
        client_version=client_version,
        canonical_policy=canonical_policy or CanonicalPolicy(),
    )
    validate_endpoint(endpoint, observer=observer, ref=source_ref)
    authority = _original_authority(endpoint, observer, source_ref)
    _require(len(authority.snapshot.raw_bytes) <= limits.max_source_bytes, "limit_exceeded")
    events = authority.session.events
    seen, selected_events, excerpts = set(), set(), []
    for item in selection:
        _require(item.last_event < len(events), "selection_invalid")
        for index in range(item.first_event, item.last_event + 1):
            _require(
                (index, item.block_index) not in seen
                and (index, None) not in seen
                and not (item.block_index is None and any(i == index for i, b in seen)),
                "selection_invalid",
            )
            seen.add((index, item.block_index))
            selected_events.add(index)
            _require(len(selected_events) <= limits.max_events, "limit_exceeded")
            event = events[index]
            excerpts.append((index, event, item.block_index, _event_text(event, item.block_index)))
    excerpt_size = sum(len(text.encode("utf-8")) for _, _, _, text in excerpts)
    _require(excerpt_size <= limits.max_excerpt_bytes, "limit_exceeded")
    counts = {kind.value: 0 for kind in EventKind}
    for index, event in enumerate(events):
        if index not in selected_events:
            counts[event.kind.value] += 1
    omissions = tuple(OmissionV1(kind, count) for kind, count in counts.items() if count)
    lines = [
        "# User handoff draft",
        "",
        "Objective: unknown",
        "Decisions: unknown",
        "State: unknown",
        "Verifications: unknown",
        "Pending work: unknown",
        "Next step: unknown",
        "",
        "Selected source excerpts are literal data:",
        "",
    ]
    for index, event, block, text in excerpts:
        lines.extend(
            [
                f"[event={index}; kind={event.kind.value}; "
                f"role={event.role.value if event.role else 'none'}; "
                f"block={block if block is not None else 'all'}]",
                text,
                "",
            ]
        )
    document = "\n".join(lines).encode("utf-8")
    _require(len(document) <= limits.max_document_bytes, "limit_exceeded")
    authority.check()
    return _issue(HandoffDraft, _State("draft", authority, selection, omissions, limits, document))


def render_handoff(draft):
    state = _state(draft, HandoffDraft)
    _check_consumed(draft, state)
    return state.document.decode("utf-8")


def handoff_metadata(handle):
    _require(
        type(handle) in (HandoffDraft, ExportedDraft, ReviewedHandoff, HandoffSubmission),
        "invalid_handle",
    )
    state = _state(handle, type(handle))
    _check_consumed(handle, state)
    return {
        "schema_version": 1,
        "kind": state.kind,
        "policy": HANDOFF_POLICY,
        "source_identity": json.loads(_encode(state.authority.identity)),
        "source_authority_sha256": state.authority.authority_sha256,
        "document_sha256": hashlib.sha256(state.document).hexdigest(),
        "selection": json.loads(_encode(state.selection)),
        "omissions": json.loads(_encode(state.omissions)),
        "provenance": state.provenance,
        "native_read_effects": state.authority.native_read_effects,
    }


def _path(path):
    _require(
        isinstance(path, Path) and path.is_absolute() and ".." not in path.parts, "unsafe_file"
    )
    return path


def _info(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _private_file(info):
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1,
        "unsafe_file",
    )


def _ancestry(namespace):
    return tuple((name, *identity) for _, name, identity in namespace.chain)


def _event(name):
    if _FAULT_HOOK is not None:
        _FAULT_HOOK(name)


def _file_current(namespace, path, descriptor, expected):
    namespace.check()
    opened = os.fstat(descriptor)
    named = os.stat(path.name, dir_fd=namespace.root, follow_symlinks=False)
    _private_file(opened)
    _private_file(named)
    _require(_info(opened) == _info(named) == expected, "namespace_changed")
    namespace.check()


def _close_all(descriptor, namespace):
    failed = False
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            failed = True
    if namespace is not None:
        try:
            namespace.close()
        except (OSError, SessionMigrateError):
            failed = True
    if failed:
        raise HandoffError("unsafe_file") from None


def export_handoff(draft, path):
    state = _state(draft, HandoffDraft, consume=True)
    descriptor = None
    namespace = None
    owned = None
    try:
        _check_consumed(draft, state)
        _path(path)
        namespace = _PinnedNamespace.open(path.parent)
        namespace.check()
        descriptor = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
            0o600,
            dir_fd=namespace.root,
        )
        _event("after_export_open")
        initial = _info(os.fstat(descriptor))
        _private_file(os.fstat(descriptor))
        _require(initial[5] == 0, "namespace_changed")
        _file_current(namespace, path, descriptor, initial)
        owned = initial
        _event("before_export_write")
        _check_consumed(draft, state)
        offset = 0
        while offset < len(state.document):
            _file_current(namespace, path, descriptor, owned)
            written = os.write(descriptor, state.document[offset : offset + 65536])
            _require(written > 0, "export_failed")
            offset += written
            after = _info(os.fstat(descriptor))
            _private_file(os.fstat(descriptor))
            _require(
                after[5] == offset and os.pread(descriptor, offset, 0) == state.document[:offset],
                "export_failed",
            )
            owned = after
            _file_current(namespace, path, descriptor, owned)
            _check_consumed(draft, state)
        _event("after_export_write")
        _file_current(namespace, path, descriptor, owned)
        _check_consumed(draft, state)
        os.fsync(descriptor)
        _event("after_export_file_fsync")
        _file_current(namespace, path, descriptor, owned)
        _check_consumed(draft, state)
        os.fsync(namespace.root)
        _event("after_export_directory_fsync")
        _file_current(namespace, path, descriptor, owned)
        _check_consumed(draft, state)
        exported_state = replace(
            state,
            kind="exported",
            path=path,
            ancestry=_ancestry(namespace),
            leaf_identity=owned[:2],
        )
    except (OSError, SessionMigrateError, ValueError, TypeError, AttributeError):
        if namespace is not None and descriptor is not None and owned is not None:
            try:
                _file_current(namespace, path, descriptor, owned)
                os.unlink(path.name, dir_fd=namespace.root)
                os.fsync(namespace.root)
            except (OSError, SessionMigrateError):
                pass
        raise HandoffError("export_failed") from None
    finally:
        _close_all(descriptor, namespace)
    _check_consumed(draft, state)
    return _issue(ExportedDraft, exported_state)


def _review(handle, state, document, confirmed_sha256):
    _require(
        type(document) is bytes and 0 < len(document) <= state.limits.max_edit_bytes,
        "limit_exceeded",
    )
    _require(
        type(confirmed_sha256) is str and hashlib.sha256(document).hexdigest() == confirmed_sha256,
        "digest_mismatch",
    )
    _check_consumed(handle, state)
    derived = _issue_user_input(document)
    _check_consumed(handle, state)
    return _issue(
        ReviewedHandoff, replace(state, kind="reviewed", document=document, derived_input=derived)
    )


def accept_user_text(draft, text, *, confirmed_sha256):
    state = _state(draft, HandoffDraft, consume=True)
    try:
        document = text.encode("utf-8") if type(text) is str else text
    except UnicodeError:
        raise HandoffError("invalid_document") from None
    return _review(draft, state, document, confirmed_sha256)


def accept_edit(exported, *, confirmed_sha256):
    state = _state(exported, ExportedDraft, consume=True)
    descriptor = None
    namespace = None
    try:
        _check_consumed(exported, state)
        namespace = _PinnedNamespace.open(state.path.parent)
        _require(_ancestry(namespace) == state.ancestry, "namespace_changed")
        descriptor = os.open(state.path.name, os.O_RDONLY | _FILE_FLAGS, dir_fd=namespace.root)
        before = _info(os.fstat(descriptor))
        _private_file(os.fstat(descriptor))
        _require(before[:2] == state.leaf_identity, "namespace_changed")
        _require(before[5] <= state.limits.max_edit_bytes, "limit_exceeded")
        _file_current(namespace, state.path, descriptor, before)
        chunks, total = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, state.limits.max_edit_bytes + 1 - total))
            _event("after_edit_read")
            _file_current(namespace, state.path, descriptor, before)
            _check_consumed(exported, state)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            _require(total <= state.limits.max_edit_bytes, "limit_exceeded")
        _file_current(namespace, state.path, descriptor, before)
        _check_consumed(exported, state)
        document = b"".join(chunks)
    except HandoffError:
        raise
    except (OSError, SessionMigrateError, ValueError, TypeError, AttributeError):
        raise HandoffError("unsafe_file") from None
    finally:
        _close_all(descriptor, namespace)
    return _review(exported, state, document, confirmed_sha256)


def _consume_reviewed(reviewed):
    state = _state(reviewed, ReviewedHandoff, consume=True)
    _check_consumed(reviewed, state)
    transformation = TransformationV1(
        1,
        "user_handoff",
        HANDOFF_POLICY,
        state.derived_input.identity,
        state.selection,
        state.omissions,
        state.provenance,
    )
    return state.authority, state.derived_input, transformation


def prepare_submission(
    reviewed,
    *,
    model_id,
    max_input_bytes,
    max_output_bytes,
    timeout_ns,
    budget_units,
    credential_env_ref=None,
):
    state = _state(reviewed, ReviewedHandoff, consume=True)
    _check_consumed(reviewed, state)
    _require(type(timeout_ns) is int and 0 < timeout_ns <= 60_000_000_000)
    request = ProviderRequest(
        model_id,
        state.derived_input.document_bytes,
        state.derived_input.identity.document_sha256,
        max_input_bytes,
        max_output_bytes,
        monotonic_ns() + timeout_ns,
        budget_units,
        credential_env_ref,
    )
    return _issue(HandoffSubmission, replace(state, kind="submission", request=request))


def submit(submission, provider, consent, *, cancelled=False):
    state = _state(submission, HandoffSubmission, consume=True)
    _check_consumed(submission, state)
    request = state.request
    _require(type(cancelled) is bool)
    if cancelled:
        raise HandoffError("cancelled")
    _require(
        type(consent) is SubmissionConsent
        and consent.approved is True
        and type(consent.document_sha256) is str
        and type(consent.model_id) is str
        and type(consent.max_output_bytes) is int
        and type(consent.budget_units) is int
        and consent.document_sha256 == request.input_sha256
        and consent.model_id == request.model_id
        and consent.max_output_bytes == request.max_output_bytes
        and consent.budget_units == request.budget_units,
        "consent_required",
    )
    _require(type(provider) is FakeHandoffProvider, "provider_unavailable")
    request.__post_init__()
    _require(monotonic_ns() < request.deadline_ns, "provider_timeout")
    try:
        response = provider.generate(request)
    except HandoffError:
        raise
    except Exception:
        raise HandoffError("provider_failed") from None
    _check_consumed(submission, state)
    _require(monotonic_ns() < request.deadline_ns, "provider_timeout")
    _require(
        type(response) is bytes
        and 0 < len(response) <= request.max_output_bytes
        and len(response) <= state.limits.max_document_bytes,
        "limit_exceeded",
    )
    try:
        response.decode("utf-8")
    except UnicodeError:
        raise HandoffError("invalid_document") from None
    return _issue(
        HandoffDraft,
        replace(
            state,
            kind="draft",
            document=response,
            provenance="fake_model_reviewed",
            derived_input=None,
            request=None,
        ),
    )
