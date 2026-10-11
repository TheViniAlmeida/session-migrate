"""Process-private authentication for explicitly launched loopback workspaces."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from session_migrate.errors import SessionMigrateError

_SCOPES = frozenset({"readonly", "publish"})
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)


class WebAuthError(SessionMigrateError):
    """Fixed reasons only; request headers and secrets never enter errors."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_origin(origin: str) -> str:
    if type(origin) is not str or len(origin) > 128:
        raise WebAuthError("origin_invalid")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise WebAuthError("origin_invalid") from None
    if (
        parsed.scheme != "http"
        or host not in ("127.0.0.1", "::1")
        or parsed.username is not None
        or parsed.password is not None
        or not port
        or not 1 <= port <= 65535
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin != f"http://{'[::1]' if host == '::1' else host}:{port}"
    ):
        raise WebAuthError("origin_invalid")
    return origin


def _digest(value: str) -> bytes:
    if type(value) is not str or _SECRET.fullmatch(value) is None:
        raise WebAuthError("credential_invalid")
    return hashlib.sha256(value.encode("ascii")).digest()


@dataclass(frozen=True, slots=True, repr=False)
class RunMetadata:
    run_id: str
    origin: str
    expires_at: str
    scopes: frozenset[str]


class RunContext:
    """Only a registry can issue a context; JSON and run IDs are not authority."""

    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise WebAuthError("context_invalid")

    def __repr__(self):
        return "<RunContext>"


@dataclass(frozen=True, slots=True, eq=False, repr=False, weakref_slot=True, init=False)
class BootstrapCredential:
    _metadata: RunMetadata
    _access_token: str
    _csrf_nonce: str

    def __new__(cls):
        raise WebAuthError("bootstrap_invalid")

    def __repr__(self):
        return "<BootstrapCredential>"


@dataclass(slots=True, repr=False)
class _Run:
    metadata: RunMetadata
    expires_monotonic: float
    token_digest: bytes
    nonce_digest: bytes
    generation: str
    revoked: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class _ContextBinding:
    run_id: str
    generation: str
    mutation: bool


class RunRegistry:
    """Authentication and jobs share this lock for confirmation and revocation."""

    def __init__(
        self,
        lock=None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        max_runs: int = 8,
        max_contexts: int = 256,
    ):
        if type(max_runs) is not int or not 1 <= max_runs <= 8:
            raise WebAuthError("capacity_invalid")
        if type(max_contexts) is not int or not 1 <= max_contexts <= 256:
            raise WebAuthError("capacity_invalid")
        if lock is not None and type(lock) is not type(threading.RLock()):
            raise WebAuthError("lock_invalid")
        self.lock = threading.RLock() if lock is None else lock
        self._clock, self._wall_clock = clock, wall_clock
        self._max_runs, self._max_contexts = max_runs, max_contexts
        self._runs: dict[str, _Run] = {}
        self._contexts = weakref.WeakKeyDictionary()
        self._bootstraps = weakref.WeakKeyDictionary()
        self._on_revoke = None
        self._closed = False

    def _bind_revocation_hook(self, callback: Callable[[str], None]) -> None:
        with self.lock:
            if self._on_revoke is not None or not callable(callback):
                raise WebAuthError("revocation_hook_invalid")
            self._on_revoke = callback

    def _revoke(self, run: _Run) -> None:
        if run.revoked:
            return
        run.revoked = True
        if self._on_revoke is not None:
            try:
                self._on_revoke(run.metadata.run_id)
            except Exception:
                raise WebAuthError("revocation_cleanup_failed") from None

    def _expire(self) -> None:
        now = self._clock()
        for run in tuple(self._runs.values()):
            if not run.revoked and now >= run.expires_monotonic:
                self._revoke(run)

    def create(
        self, origin: str, scopes: frozenset[str] = frozenset({"readonly"}), *, lifetime: int = 1800
    ) -> BootstrapCredential:
        origin = validate_origin(origin)
        if (
            type(scopes) is not frozenset
            or any(type(item) is not str for item in scopes)
            or not scopes <= _SCOPES
            or "readonly" not in scopes
        ):
            raise WebAuthError("scope_invalid")
        if type(lifetime) is not int or not 1 <= lifetime <= 1800:
            raise WebAuthError("lifetime_invalid")
        with self.lock:
            self._expire()
            if self._closed or len(self._runs) >= self._max_runs:
                raise WebAuthError("run_capacity")
            run_id = str(uuid.uuid4())
            expires_at = datetime.fromtimestamp(self._wall_clock() + lifetime, UTC).isoformat()
            metadata = RunMetadata(run_id, origin, expires_at, scopes)
            token, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            private_metadata = RunMetadata(run_id, origin, expires_at, scopes)
            run = _Run(
                private_metadata,
                self._clock() + lifetime,
                _digest(token),
                _digest(nonce),
                str(uuid.uuid4()),
            )
            credential = object.__new__(BootstrapCredential)
            for name, value in (
                ("_metadata", metadata),
                ("_access_token", token),
                ("_csrf_nonce", nonce),
            ):
                object.__setattr__(credential, name, value)
            self._runs[run_id] = run
            self._bootstraps[credential] = (run_id, run.generation)
            return credential

    def _bootstrap_payload(self, credential: BootstrapCredential) -> bytes:
        with self.lock:
            self._expire()
            binding = (
                self._bootstraps.get(credential)
                if type(credential) is BootstrapCredential
                else None
            )
            if binding is None:
                raise WebAuthError("bootstrap_invalid")
            run = self._runs.get(binding[0])
            if (
                run is None
                or run.revoked
                or self._closed
                or run.generation != binding[1]
                or credential._metadata != run.metadata
                or not hmac.compare_digest(_digest(credential._access_token), run.token_digest)
                or not hmac.compare_digest(_digest(credential._csrf_nonce), run.nonce_digest)
            ):
                raise WebAuthError("bootstrap_invalid")
            return json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run.metadata.run_id,
                    "origin": run.metadata.origin,
                    "expires_at": run.metadata.expires_at,
                    "access_token": credential._access_token,
                    "csrf_nonce": credential._csrf_nonce,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")

    def authenticate(
        self,
        authorization: str,
        *,
        host: str,
        origin: str | None,
        method: str,
        csrf_nonce: str | None = None,
    ) -> RunContext:
        if type(method) is not str or method not in ("GET", "POST"):
            raise WebAuthError("method_denied")
        if type(authorization) is not str or not authorization.startswith("Bearer "):
            raise WebAuthError("authentication_required")
        token_digest = _digest(authorization[7:])
        mutation = method == "POST"
        with self.lock:
            self._expire()
            if self._closed:
                raise WebAuthError("run_unavailable")
            match = None
            for candidate in self._runs.values():
                if hmac.compare_digest(token_digest, candidate.token_digest):
                    match = candidate
            if match is None or match.revoked:
                raise WebAuthError("authentication_required")
            if type(host) is not str or host != urlsplit(match.metadata.origin).netloc:
                raise WebAuthError("host_denied")
            if (
                origin is not None and (type(origin) is not str or origin != match.metadata.origin)
            ) or (mutation and origin is None):
                raise WebAuthError("origin_denied")
            if mutation and not hmac.compare_digest(_digest(csrf_nonce), match.nonce_digest):
                raise WebAuthError("csrf_denied")
            if len(self._contexts) >= self._max_contexts:
                raise WebAuthError("context_capacity")
            context = object.__new__(RunContext)
            self._contexts[context] = _ContextBinding(
                match.metadata.run_id, match.generation, mutation
            )
            return context

    def _current(self, context: RunContext, *, mutation: bool = False) -> _Run:
        self._expire()
        binding = self._contexts.get(context) if type(context) is RunContext else None
        if binding is None:
            raise WebAuthError("context_invalid")
        run = self._runs.get(binding.run_id)
        if self._closed or run is None or run.revoked or run.generation != binding.generation:
            raise WebAuthError("run_unavailable")
        if mutation and not binding.mutation:
            raise WebAuthError("mutation_context_required")
        return run

    def authorize(self, context: RunContext, scope: str, *, mutation: bool = False) -> RunMetadata:
        if type(scope) is not str or scope not in _SCOPES or type(mutation) is not bool:
            raise WebAuthError("scope_invalid")
        with self.lock:
            run = self._current(context, mutation=mutation or scope == "publish")
            if scope not in run.metadata.scopes:
                raise WebAuthError("scope_denied")
            return RunMetadata(
                run.metadata.run_id,
                run.metadata.origin,
                run.metadata.expires_at,
                run.metadata.scopes,
            )

    def revoke(self, context: RunContext) -> str:
        with self.lock:
            run = self._current(context, mutation=True)
            self._revoke(run)
            return run.metadata.run_id

    def check_run(self, run_id: str, *, scope: str = "readonly") -> RunMetadata:
        """Trusted worker guard; HTTP run IDs must first pass authenticate/authorize."""
        if type(run_id) is not str or type(scope) is not str or scope not in _SCOPES:
            raise WebAuthError("run_unavailable")
        with self.lock:
            self._expire()
            run = self._runs.get(run_id)
            if self._closed or run is None or run.revoked or scope not in run.metadata.scopes:
                raise WebAuthError("run_unavailable")
            return RunMetadata(
                run.metadata.run_id,
                run.metadata.origin,
                run.metadata.expires_at,
                run.metadata.scopes,
            )

    def close(self) -> None:
        with self.lock:
            self._closed = True
            failed = False
            for run in tuple(self._runs.values()):
                try:
                    self._revoke(run)
                except WebAuthError:
                    failed = True
            if failed:
                raise WebAuthError("revocation_cleanup_failed")
