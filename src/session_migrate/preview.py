"""Bounded, literal conversation previews; no adapters, persistence, or media I/O.

Callers must obtain/revalidate ``source_revision`` and identity authoritatively,
including before using a cached snapshot. Limits affect previews only. The loader
runs outside the UI thread and must apply the profile's source guards and its own
I/O/parsing limits: Python threads cannot forcibly stop a blocked native reader.
"""

from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import islice

from session_migrate.model import AgentFormat, Event, EventKind, Role, Session

# Strip complete/incomplete terminal control strings before stripping controls.
_TERMINAL = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\|$)|"
    r"[P_X^][^\x1b]*(?:\x1b\\|$)|\[[0-?]*[ -/]*[@-~]?|[ -/]*[@-~]?)"
    r"|\x9d[^\x07\x9c]*(?:\x07|\x9c|$)"
    r"|[\x90\x98\x9e\x9f][^\x9c]*(?:\x9c|$)|\x9b[0-?]*[ -/]*[@-~]?"
)
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_MEDIA = frozenset(
    {"image", "input_image", "audio", "input_audio", "video", "document", "attachment", "file"}
)
_TOOL = frozenset({EventKind.TOOL_CALL, EventKind.TOOL_RESULT})
_LIMIT_NOTICE = "Preview limited; transferred history is unchanged."


def literal_text(value: str) -> str:
    """Remove terminal/C0/C1/bidi controls; preserve Unicode and literal markup.

    Newlines and tabs are retained. Consumers must use a plain-text widget (for
    example Rich ``Text``), never a markup/HTML/Markdown parser.
    """
    value = _TERMINAL.sub("", value)
    return "".join(
        char
        for char in value
        if char not in _BIDI
        and (char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159))
    )


def _positive(**values: int) -> None:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values.values()
    ):
        raise ValueError("preview limits must be positive integers")


@dataclass(frozen=True, slots=True)
class PreviewLimits:
    max_events: int = 1000
    max_characters: int = 128_000
    max_bytes: int = 256_000
    event_characters: int = 4000
    input_characters: int = 32_000

    def __post_init__(self) -> None:
        _positive(
            max_events=self.max_events,
            max_characters=self.max_characters,
            max_bytes=self.max_bytes,
            event_characters=self.event_characters,
            input_characters=self.input_characters,
        )


@dataclass(frozen=True, slots=True)
class PageLimits:
    max_events: int = 40
    max_characters: int = 8000
    max_bytes: int = 16_000

    def __post_init__(self) -> None:
        _positive(
            max_events=self.max_events, max_characters=self.max_characters, max_bytes=self.max_bytes
        )


_DEFAULT_LIMITS = PreviewLimits()
_DEFAULT_PAGE_LIMITS = PageLimits()


@dataclass(frozen=True, slots=True, repr=False)
class PreviewKey:
    source_format: AgentFormat
    profile_id: str
    root: str
    native_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_format", AgentFormat(self.source_format))
        if not all(
            isinstance(value, str) and value
            for value in (self.profile_id, self.root, self.native_id)
        ):
            raise ValueError("preview identity requires profile, root, and native ID")


@dataclass(frozen=True, slots=True, repr=False)
class PreviewEvent:
    source_index: int
    kind: EventKind
    role: Role | None
    text: str
    collapsed: bool = False
    truncated: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class PreviewPage:
    events: tuple[PreviewEvent, ...]
    next_offset: int | None
    total_source_events: int
    notices: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class PreviewSnapshot:
    source_revision: str
    events: tuple[PreviewEvent, ...]
    total_source_events: int
    limits: PreviewLimits
    expand_tools: bool
    notices: tuple[str, ...]

    @property
    def text_bytes(self) -> int:
        return sum(len(event.text.encode("utf-8")) for event in self.events)

    def page(self, offset: int = 0, *, limits: PageLimits = _DEFAULT_PAGE_LIMITS) -> PreviewPage:
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= len(self.events)
        ):
            raise ValueError("preview offset is outside the snapshot")
        rows: list[PreviewEvent] = []
        characters, byte_count = limits.max_characters, limits.max_bytes
        notices = list(self.notices)
        for event in self.events[offset : offset + limits.max_events]:
            text, clipped = _clip(event.text, characters, byte_count)
            if not text and event.text and rows:
                break
            rows.append(replace(event, text=text, truncated=event.truncated or clipped))
            characters -= len(text)
            byte_count -= len(text.encode("utf-8"))
            if clipped and _LIMIT_NOTICE not in notices:
                notices.append(_LIMIT_NOTICE)
            if characters == 0 or byte_count == 0:
                break
        end = offset + len(rows)
        return PreviewPage(
            tuple(rows),
            end if end < len(self.events) else None,
            self.total_source_events,
            tuple(notices),
        )


def _clip(text: str, characters: int, byte_count: int) -> tuple[str, bool]:
    prefix = text[:characters]
    encoded = prefix.encode("utf-8", errors="replace")
    result = encoded[:byte_count].decode("utf-8", errors="ignore")
    return result, result != text


def _structured_arguments(arguments: dict[object, object], limit: int) -> tuple[str, bool]:
    """Bound flat tool arguments; never serialize an arbitrary source payload."""
    media_kind = arguments.get("block_type", arguments.get("type"))
    if isinstance(media_kind, str) and media_kind in _MEDIA:
        return "[Media content not loaded]", False
    rendered: dict[str, object] = {}
    clipped = len(arguments) > 16
    field_limit = max(1, limit // 16)
    for name, value in islice(arguments.items(), 16):
        if not isinstance(name, str):
            clipped = True
            continue
        safe_name = literal_text(name[:64])
        clipped |= len(name) > 64
        if (
            safe_name.casefold() in _MEDIA
            or safe_name.casefold()
            in {
                "image_url",
                "image_path",
                "audio_path",
                "video_path",
                "attachment_path",
                "media_path",
                "base64",
                "media",
            }
            or isinstance(value, str)
            and value[:field_limit].lstrip().casefold().startswith("data:")
        ):
            rendered[safe_name] = "[Media content not loaded]"
        elif isinstance(value, str):
            clipped |= len(value) > field_limit
            rendered[safe_name] = literal_text(value[:field_limit])
        elif (
            value is None
            or isinstance(value, bool)
            or isinstance(value, int)
            and value.bit_length() <= 128
            or isinstance(value, float)
            and math.isfinite(value)
        ):
            rendered[safe_name] = value
        else:
            rendered[safe_name] = "[Nested or binary argument omitted from preview]"
            clipped = True
    return json.dumps(rendered, ensure_ascii=False), clipped


def _tool_arguments(arguments: object, limit: int) -> tuple[str, bool]:
    """Apply the dict policy to bounded JSON strings; unknown inputs stay hidden.

    The lexical check bounds work and nesting before decoding. It does not try
    to repair partial streamed JSON. Escaped strings are decoded before applying
    the media policy, so an escaped key/URI cannot bypass that policy.
    """
    omitted = "[Tool arguments omitted from preview: incomplete, unsupported or limited input]"
    if isinstance(arguments, str):
        if len(arguments) > min(limit, 32_000):
            return omitted, True
        depth = punctuation = 0
        quoted = escaped = False
        for char in arguments:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                punctuation += 1
            elif char in "]}":
                depth -= 1
                punctuation += 1
            elif char in ",:":
                punctuation += 1
            if depth > 16 or punctuation > 256 or depth < 0:
                return omitted, True
        if quoted or depth:
            return omitted, True
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            return omitted, True
    if isinstance(arguments, dict):
        return _structured_arguments(arguments, limit)
    return omitted, True


def _event_text(event: Event, *, expand_tools: bool, input_limit: int) -> tuple[str, bool]:
    # Do not inspect or display media URLs, embedded bytes, filenames or tokens.
    block = event.payload.get("block_type")
    if isinstance(block, str) and block in _MEDIA:
        return f"[Media: {block}; content not loaded]", False
    if event.kind in _TOOL and not expand_tools:
        name = literal_text((event.tool_name or "unnamed")[:64])
        return f"[{event.kind.value}: {name}; collapsed]", False
    raw = event.text or ""
    truncated = False
    if event.kind == EventKind.TOOL_CALL and raw:
        # Some portable callers carry textual arguments in Event.text.
        raw, truncated = _tool_arguments(raw, input_limit)
    elif not raw and event.kind in _TOOL:
        # Show only selected portable arguments, never the complete payload.
        arguments = event.payload.get("arguments", event.payload.get("input"))
        if arguments is not None:
            raw, truncated = _tool_arguments(arguments, input_limit)
    truncated |= len(raw) > input_limit
    text = literal_text(raw[:input_limit])
    if not text:
        text = f"[{event.kind.value}; no text]"
    if event.kind == EventKind.COMPACTION:
        text = "[Compaction: earlier context may be summarized]\n" + text
    if event.kind in _TOOL and expand_tools:
        name = literal_text((event.tool_name or "unnamed")[:64])
        text = f"[{event.kind.value}: {name}; expanded]\n" + text
    return text, truncated


def project_session(
    session: Session,
    *,
    limits: PreviewLimits = _DEFAULT_LIMITS,
    expand_tools: bool = False,
    source_revision: str | None = None,
) -> PreviewSnapshot:
    """Project bounded visual text without changing the source ``Session``.

    Budgets count event text. Fixed notices and Python object overhead are not
    text budgets; the cache accounts separately for retained snapshot objects.
    ``source_revision`` is opaque, owned and authoritatively checked by caller.
    """
    rows: list[PreviewEvent] = []
    characters, byte_count = limits.max_characters, limits.max_bytes
    limited = len(session.events) > limits.max_events
    compaction = False
    for index, event in enumerate(session.events[: limits.max_events]):
        text, truncated = _event_text(
            event, expand_tools=expand_tools, input_limit=limits.input_characters
        )
        text, clipped = _clip(text, min(characters, limits.event_characters), byte_count)
        if not text and rows:
            limited = True
            break
        rows.append(
            PreviewEvent(
                index,
                event.kind,
                event.role,
                text,
                event.kind in _TOOL and not expand_tools,
                truncated or clipped,
            )
        )
        limited |= truncated or clipped
        compaction |= event.kind == EventKind.COMPACTION
        characters -= len(text)
        byte_count -= len(text.encode("utf-8"))
        if characters == 0 or byte_count == 0:
            limited |= index + 1 < len(session.events)
            break
    notices = (_LIMIT_NOTICE,) if limited else ()
    if compaction:
        notices += ("Compaction present; this preview cannot reconstruct earlier context.",)
    if not session.events:
        notices += ("Empty session.",)
    return PreviewSnapshot(
        source_revision if source_revision is not None else session.source_sha256,
        tuple(rows),
        len(session.events),
        limits,
        expand_tools,
        notices,
    )


_CacheIdentity = tuple[PreviewKey, PreviewLimits, bool]


def _retained_bytes(key: PreviewKey, snapshot: PreviewSnapshot) -> int:
    # Conservative accounting, including containers/row/string overhead. Shared
    # strings may be counted multiple times; bodies are never retained as Session.
    return (
        sys.getsizeof(key)
        + sum(
            sys.getsizeof(value)
            for value in (key.profile_id, key.root, key.native_id, key.source_format.value)
        )
        + sys.getsizeof(snapshot)
        + sys.getsizeof(snapshot.source_revision)
        + sys.getsizeof(snapshot.events)
        + sys.getsizeof(snapshot.notices)
        + sys.getsizeof(snapshot.limits)
        + sys.getsizeof(snapshot.total_source_events)
        + sum(
            sys.getsizeof(event) + sys.getsizeof(event.text) + sys.getsizeof(event.source_index)
            for event in snapshot.events
        )
        + sum(sys.getsizeof(notice) for notice in snapshot.notices)
        + 512
    )


class PreviewCache:
    """Thread-safe in-memory LRU of projected snapshots, bounded by bytes/entries."""

    def __init__(self, *, max_entries: int = 8, max_bytes: int = 2_000_000) -> None:
        _positive(max_entries=max_entries, max_bytes=max_bytes)
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries: OrderedDict[_CacheIdentity, tuple[PreviewSnapshot, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def entries(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(
        self,
        key: PreviewKey,
        source_revision: str,
        *,
        limits: PreviewLimits = _DEFAULT_LIMITS,
        expand_tools: bool = False,
    ) -> PreviewSnapshot | None:
        with self._lock:
            for identity, (snapshot, size) in list(self._entries.items()):
                if identity[0] == key and snapshot.source_revision != source_revision:
                    del self._entries[identity]
                    self._bytes -= size
            identity = (key, limits, expand_tools)
            entry = self._entries.get(identity)
            if entry is None:
                return None
            self._entries.move_to_end(identity)
            return entry[0]

    def put(self, key: PreviewKey, snapshot: PreviewSnapshot) -> bool:
        identity = (key, snapshot.limits, snapshot.expand_tools)
        size = _retained_bytes(key, snapshot)
        with self._lock:
            for cached_key, (previous, previous_size) in list(self._entries.items()):
                if cached_key[0] == key and (
                    cached_key == identity or previous.source_revision != snapshot.source_revision
                ):
                    del self._entries[cached_key]
                    self._bytes -= previous_size
            if size > self.max_bytes:
                return False
            while self._entries and (
                len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes
            ):
                _, (_, previous_size) = self._entries.popitem(last=False)
                self._bytes -= previous_size
            self._entries[identity] = (snapshot, size)
            self._bytes += size
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


class PreviewStatus(StrEnum):
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BUSY = "busy"


@dataclass(frozen=True, slots=True, repr=False)
class PreviewResult:
    status: PreviewStatus
    snapshot: PreviewSnapshot | None = None
    error: str | None = None


class PreviewRequest:
    """Nonblocking poll and bounded wait; timeout/cancel cannot kill the loader."""

    def __init__(self, timeout: float) -> None:
        self._deadline = time.monotonic() + timeout
        self._result = PreviewResult(PreviewStatus.LOADING)
        self._lock = threading.Lock()
        self._done = threading.Event()

    def _expire(self) -> None:
        if self._result.status == PreviewStatus.LOADING and time.monotonic() >= self._deadline:
            self._result = PreviewResult(PreviewStatus.TIMED_OUT, error="Preview read timed out.")
            self._done.set()

    def _publish(self, result: PreviewResult, publish: Callable[[], object] | None = None) -> None:
        with self._lock:
            self._expire()
            if self._result.status == PreviewStatus.LOADING:
                if publish is not None:
                    publish()
                self._result = result
                self._done.set()

    def _lookup(self, lookup: Callable[[], PreviewSnapshot | None]) -> PreviewSnapshot | None:
        # Only bounded memory cache work belongs here, never source I/O. The
        # request lock makes the lookup atomic with respect to cancel/expiry.
        with self._lock:
            self._expire()
            if self._result.status != PreviewStatus.LOADING:
                return None
            return lookup()

    def poll(self) -> PreviewResult:
        with self._lock:
            self._expire()
            return self._result

    def wait(self, timeout: float | None = None) -> PreviewResult:
        remaining = max(0.0, self._deadline - time.monotonic())
        self._done.wait(remaining if timeout is None else min(max(0.0, timeout), remaining))
        return self.poll()

    def cancel(self) -> None:
        self._publish(PreviewResult(PreviewStatus.CANCELLED))


class PreviewService:
    """Bounded daemon reader slots, no queued jobs and no persistent bodies.

    A timed-out/cancelled callback keeps its slot until it exits, and never
    publishes/cache-writes its result. This bounds threads, not the callback's
    source/parsing memory; injected adapters must enforce their own I/O budgets.
    """

    def __init__(
        self,
        *,
        cache: PreviewCache | None = None,
        max_workers: int = 1,
        limits: PreviewLimits = _DEFAULT_LIMITS,
    ) -> None:
        _positive(max_workers=max_workers)
        self.cache = cache if cache is not None else PreviewCache()
        self.limits = limits
        self._slots = threading.BoundedSemaphore(max_workers)

    @staticmethod
    def _new_request(timeout: float) -> PreviewRequest:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("preview timeout must be finite and positive")
        return PreviewRequest(timeout)

    def request(
        self,
        key: PreviewKey,
        source_revision: str,
        loader: Callable[[], Session],
        *,
        timeout: float = 5.0,
        expand_tools: bool = False,
    ) -> PreviewRequest:
        """Use an already-authoritative revision; the caller must revalidate it."""
        request = self._new_request(timeout)
        cached = self.cache.get(key, source_revision, limits=self.limits, expand_tools=expand_tools)
        if cached is not None:
            request._publish(PreviewResult(PreviewStatus.READY, cached))
            return request
        return self._submit(
            request,
            key,
            lambda: (source_revision, loader()),
            expand_tools=expand_tools,
            authoritative=False,
        )

    def request_authoritative(
        self,
        key: PreviewKey,
        resolver: Callable[[], tuple[str, Session]],
        *,
        timeout: float = 5.0,
        expand_tools: bool = False,
    ) -> PreviewRequest:
        """Return a cancellable handle without doing source I/O on the caller.

        The entire authority/read/parse operation belongs in ``resolver``. It
        must apply the profile guards and return the current opaque revision
        (1..256 characters) with the guarded Session. Even a cache hit requires
        that witness; it cannot skip authoritative I/O. The deadline starts at
        factory entry, and expiry/cancel cannot stop a blocked resolver thread.
        """
        request = self._new_request(timeout)
        return self._submit(
            request,
            key,
            resolver,
            expand_tools=expand_tools,
            authoritative=True,
        )

    def _submit(
        self,
        request: PreviewRequest,
        key: PreviewKey,
        resolver: Callable[[], tuple[str, Session]],
        *,
        expand_tools: bool,
        authoritative: bool,
    ) -> PreviewRequest:
        if not self._slots.acquire(blocking=False):
            request._publish(PreviewResult(PreviewStatus.BUSY, error="Preview reader is busy."))
            return request

        def read() -> None:
            try:
                if request.poll().status != PreviewStatus.LOADING:
                    return
                source_revision, session = resolver()
                if request.poll().status != PreviewStatus.LOADING:
                    return
                if authoritative and (
                    not isinstance(source_revision, str)
                    or not source_revision
                    or len(source_revision) > 256
                    or not source_revision.strip()
                ):
                    request._publish(
                        PreviewResult(PreviewStatus.ERROR, error="Preview source revision invalid.")
                    )
                    return
                if (
                    session.source_format != key.source_format
                    or session.session_id != key.native_id
                ):
                    request._publish(
                        PreviewResult(PreviewStatus.ERROR, error="Preview source identity changed.")
                    )
                    return
                if authoritative:
                    cached = request._lookup(
                        lambda: self.cache.get(
                            key,
                            source_revision,
                            limits=self.limits,
                            expand_tools=expand_tools,
                        )
                    )
                    if cached is not None:
                        request._publish(PreviewResult(PreviewStatus.READY, cached))
                        return
                if request.poll().status != PreviewStatus.LOADING:
                    return
                snapshot = project_session(
                    session,
                    limits=self.limits,
                    expand_tools=expand_tools,
                    source_revision=source_revision,
                )
                request._publish(
                    PreviewResult(PreviewStatus.READY, snapshot),
                    lambda: self.cache.put(key, snapshot),
                )
            except Exception:
                # Exception messages/tracebacks may contain session bodies or account paths.
                request._publish(PreviewResult(PreviewStatus.ERROR, error="Preview read failed."))
            finally:
                self._slots.release()

        try:
            threading.Thread(target=read, name="session-preview", daemon=True).start()
        except Exception:
            self._slots.release()
            request._publish(
                PreviewResult(PreviewStatus.ERROR, error="Preview reader unavailable.")
            )
        return request
