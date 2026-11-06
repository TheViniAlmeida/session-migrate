"""Private, bounded native captures; no coverage, namespace or boundary proof.

Bodies live only in immutable memory, are excluded from repr, and have no
serialization API. Parsing and subsequent auditing must consume this authority,
never reopen its source. Filesystem witnesses are identity guards, not a proof
against an adversary rewriting bytes while restoring filesystem metadata.
"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.jsonl import DEFAULT_MAX_TOTAL_BYTES, JsonlRecord, iter_jsonl_bytes
from session_migrate.model import AgentFormat, Session

_SUPPORTED = frozenset({AgentFormat.CLAUDE, AgentFormat.CODEX, AgentFormat.OPENCODE})
MAX_CAPTURE_JSON_DEPTH = 128


class CaptureProvenance(StrEnum):
    FILE = "file"
    EXPORTED_BYTES = "exported_bytes"
    RESOLVED_STORAGE = "resolved_storage"


@dataclass(frozen=True, slots=True, repr=False)
class SourceCaptureContext:
    source_format: AgentFormat
    source_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.source_format, AgentFormat) or self.source_format not in _SUPPORTED:
            raise SessionMigrateError("native capture format is unsupported")
        if not isinstance(self.source_path, Path):
            raise SessionMigrateError("native capture path is invalid")


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileIdentity:
        return cls(
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class AncestorIdentity:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True, repr=False)
class NativeSnapshot:
    """One immutable source authority; decoded objects are fresh copies each time."""

    source_format: AgentFormat
    source_path: Path
    provenance: CaptureProvenance
    identity: FileIdentity | None
    ancestors: tuple[AncestorIdentity, ...]
    _raw_bytes: bytes = field(repr=False)
    storage_components: tuple[NativeSnapshot, ...] = field(default=(), repr=False)
    storage_root: Path | None = field(default=None, repr=False)
    authority_sha256: str = field(init=False)
    decoded_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        SourceCaptureContext(self.source_format, self.source_path)
        if type(self._raw_bytes) is not bytes or len(self._raw_bytes) > DEFAULT_MAX_TOTAL_BYTES:
            raise SessionMigrateError("native capture bytes are invalid or exceed the safety limit")
        if not isinstance(self.provenance, CaptureProvenance):
            raise SessionMigrateError("native capture provenance is invalid")
        if type(self.ancestors) is not tuple or any(
            not isinstance(item, AncestorIdentity) for item in self.ancestors
        ):
            raise SessionMigrateError("native capture ancestry is invalid")
        if self.provenance == CaptureProvenance.RESOLVED_STORAGE:
            self._validate_storage()
        elif self.storage_components or self.storage_root is not None:
            raise SessionMigrateError("ordinary capture cannot carry storage components")
        if self.provenance == CaptureProvenance.EXPORTED_BYTES:
            if self.identity is not None or self.ancestors:
                raise SessionMigrateError("exported capture cannot carry filesystem identity")
        elif not isinstance(self.identity, FileIdentity) or not self.ancestors:
            raise SessionMigrateError("file capture requires filesystem identity")
        object.__setattr__(self, "decoded_sha256", hashlib.sha256(self._raw_bytes).hexdigest())
        object.__setattr__(self, "authority_sha256", self._authority())

    def _validate_storage(self) -> None:
        if (
            self.source_format is not AgentFormat.CODEX
            or type(self.storage_components) is not tuple
            or not 1 <= len(self.storage_components) <= 64
            or not isinstance(self.storage_root, Path)
            or not self.storage_root.is_absolute()
            or ".." in self.storage_root.parts
        ):
            raise SessionMigrateError("resolved storage capture is invalid")
        seen = set()
        for component in self.storage_components:
            if (
                type(component) is not NativeSnapshot
                or component.source_format is not AgentFormat.CODEX
                or component.provenance is not CaptureProvenance.FILE
                or not component.source_path.is_relative_to(self.storage_root)
                or component.source_path == self.storage_root
                or component.source_path in seen
                or not component.integrity_valid()
            ):
                raise SessionMigrateError("resolved storage component is invalid")
            seen.add(component.source_path)
        leaf = self.storage_components[0]
        if (
            self.source_path != leaf.source_path
            or self.identity != leaf.identity
            or self.ancestors != leaf.ancestors
        ):
            raise SessionMigrateError("resolved storage leaf identity is invalid")

    def _authority(self) -> str:
        if self.provenance is CaptureProvenance.RESOLVED_STORAGE:
            from session_migrate.codex_storage import materialization_authority

            return materialization_authority(
                self.storage_root, self.storage_components, self._raw_bytes
            )
        return hashlib.sha256(self._raw_bytes).hexdigest()

    def integrity_valid(self) -> bool:
        """Bind physical component bytes and their separately decoded projection."""
        try:
            if self.provenance is CaptureProvenance.RESOLVED_STORAGE:
                self._validate_storage()
            elif self.storage_components or self.storage_root is not None:
                return False
            return (
                hashlib.sha256(self._raw_bytes).hexdigest() == self.decoded_sha256
                and self._authority() == self.authority_sha256
            )
        except (SessionMigrateError, TypeError, ValueError, AttributeError, RecursionError):
            return False

    @property
    def captured_byte_count(self) -> int:
        return len(self._raw_bytes) + sum(len(c.raw_bytes) for c in self.storage_components)

    @property
    def physical_source_sha256(self) -> str:
        return (
            self.storage_components[0].authority_sha256
            if self.storage_components
            else self.authority_sha256
        )

    @property
    def source_storage_bytes(self) -> bytes:
        return self.storage_components[0].raw_bytes if self.storage_components else self.raw_bytes

    @property
    def storage_binding(self) -> tuple:
        return (
            str(self.storage_root) if self.storage_root is not None else None,
            tuple(
                (
                    str(c.source_path),
                    c.authority_sha256,
                    (
                        c.identity.device,
                        c.identity.inode,
                        c.identity.mode,
                        c.identity.links,
                        c.identity.size,
                        c.identity.modified_ns,
                        c.identity.changed_ns,
                    ),
                    tuple((str(a.path), a.device, a.inode) for a in c.ancestors),
                )
                for c in self.storage_components
            ),
        )

    @property
    def raw_bytes(self) -> bytes:
        return self._raw_bytes

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        context: SourceCaptureContext,
        byte_limit: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> NativeSnapshot:
        """Bind a captured export in RAM; preserve its virtual path verbatim."""

        _check_limit(byte_limit)
        if type(data) is not bytes:
            raise SessionMigrateError("native capture requires immutable bytes")
        if len(data) > byte_limit:
            raise SessionMigrateError("native capture exceeds the byte safety limit")
        return cls(
            context.source_format,
            context.source_path,
            CaptureProvenance.EXPORTED_BYTES,
            None,
            (),
            data,
        )

    def decoded_records(self) -> tuple[JsonlRecord, ...]:
        if self.source_format == AgentFormat.OPENCODE:
            raise SessionMigrateError("native capture is not a JSONL source")
        try:
            records = tuple(iter_jsonl_bytes(self._raw_bytes))
            for record in records:
                _check_json_depth(record.value)
            return records
        except (SessionMigrateError, RecursionError):
            raise SessionMigrateError("native capture JSONL decoding failed") from None

    def parse(self, *, reviewed_portability: bool = False) -> Session:
        from session_migrate.formats import claude, codex, opencode

        if type(reviewed_portability) is not bool:
            raise SessionMigrateError("reviewed portability mode must be boolean")
        if reviewed_portability and self.source_format is not AgentFormat.CLAUDE:
            raise SessionMigrateError("reviewed portability requires Claude 2.1.309")
        try:
            if self.source_format == AgentFormat.CLAUDE:
                return claude.parse_records(
                    self.decoded_records(),
                    source_path=self.source_path,
                    source_sha256=self.authority_sha256,
                    reviewed_portability=reviewed_portability,
                )
            if self.source_format == AgentFormat.CODEX:
                return codex.parse_records(
                    self.decoded_records(),
                    source_path=self.source_path,
                    source_sha256=self.authority_sha256,
                )
            return opencode.parse_session_bytes(self._raw_bytes, self.source_path)
        except (SessionMigrateError, RecursionError):
            raise SessionMigrateError("native capture parsing failed") from None


def _check_limit(byte_limit: int) -> None:
    if type(byte_limit) is not int or not 0 < byte_limit <= DEFAULT_MAX_TOTAL_BYTES:
        raise SessionMigrateError("native capture byte limit is invalid")


def _check_json_depth(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_CAPTURE_JSON_DEPTH:
            raise SessionMigrateError("native capture JSON depth exceeds the safety limit")
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _flags(*, directory: bool = False) -> int:
    required = ("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY")
    if any(not hasattr(os, flag) for flag in required):
        raise SessionMigrateError("native capture filesystem guards are unavailable")
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0)


def _open_ancestors(path: Path, stack: ExitStack) -> list[tuple[AncestorIdentity, int]]:
    descriptor = os.open(path.anchor, _flags(directory=True))
    stack.callback(os.close, descriptor)
    current = Path(path.anchor)
    result = []
    for component in (None, *path.parts[1:-1]):
        if component is not None:
            descriptor = os.open(component, _flags(directory=True), dir_fd=descriptor)
            stack.callback(os.close, descriptor)
            current /= component
        identity = os.fstat(descriptor)
        result.append((AncestorIdentity(current, identity.st_dev, identity.st_ino), descriptor))
    return result


def _check_ancestors(ancestors: list[tuple[AncestorIdentity, int]]) -> None:
    for index, (expected, descriptor) in enumerate(ancestors):
        opened = os.fstat(descriptor)
        named = (
            os.stat(expected.path.anchor, follow_symlinks=False)
            if index == 0
            else os.stat(expected.path.name, dir_fd=ancestors[index - 1][1], follow_symlinks=False)
        )
        for value in (opened, named):
            if not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != (
                expected.device,
                expected.inode,
            ):
                raise SessionMigrateError("native capture ancestry changed")


def _check_file(value: os.stat_result) -> FileIdentity:
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise SessionMigrateError("native capture requires a regular file with one link")
    return FileIdentity.from_stat(value)


def _read_bounded(descriptor: int, byte_limit: int) -> bytes:
    """One logical capture from one FD; short reads do not imply mutation."""

    chunks: list[bytes] = []
    total_bytes = 0
    while total_bytes <= byte_limit:
        chunk = os.read(descriptor, min(1024 * 1024, byte_limit + 1 - total_bytes))
        if not chunk:
            break
        chunks.append(chunk)
        total_bytes += len(chunk)
    if total_bytes > byte_limit:
        raise SessionMigrateError("native capture exceeds the byte safety limit")
    return b"".join(chunks)


def capture_source(
    context: SourceCaptureContext,
    *,
    byte_limit: int = DEFAULT_MAX_TOTAL_BYTES,
) -> NativeSnapshot:
    """Read a regular source once through guarded descriptors, bounded before I/O."""

    _check_limit(byte_limit)
    if ".." in context.source_path.parts:
        raise SessionMigrateError("native capture path contains a parent traversal")
    path = Path(os.path.abspath(context.source_path.expanduser()))
    try:
        with ExitStack() as stack:
            ancestors = _open_ancestors(path, stack)
            parent_descriptor = ancestors[-1][1]
            _check_ancestors(ancestors)
            before = _check_file(
                os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            )
            if before.size > byte_limit:
                raise SessionMigrateError("native capture exceeds the byte safety limit")
            descriptor = os.open(path.name, _flags(), dir_fd=parent_descriptor)
            stack.callback(os.close, descriptor)
            if _check_file(os.fstat(descriptor)) != before:
                raise SessionMigrateError("native capture file changed")
            data = _read_bounded(descriptor, byte_limit)
            if len(data) != before.size or _check_file(os.fstat(descriptor)) != before:
                raise SessionMigrateError("native capture file changed")
            if (
                _check_file(os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False))
                != before
            ):
                raise SessionMigrateError("native capture file changed")
            _check_ancestors(ancestors)
            return NativeSnapshot(
                context.source_format,
                path,
                CaptureProvenance.FILE,
                before,
                tuple(item for item, _ in ancestors),
                data,
            )
    except OSError:
        raise SessionMigrateError("native capture filesystem guard failed") from None


def check_source_identity(snapshot: NativeSnapshot) -> None:
    """Check names/stat witnesses only; content freshness requires a new capture.

    Exported bytes have no filesystem identity and cannot undergo this guard.
    No source body is read here and no content or namespace proof is returned.
    """

    if snapshot.provenance is CaptureProvenance.RESOLVED_STORAGE:
        if not snapshot.integrity_valid():
            raise SessionMigrateError("resolved storage capture changed")
        for component in snapshot.storage_components:
            check_source_identity(component)
        return
    if snapshot.provenance != CaptureProvenance.FILE:
        raise SessionMigrateError("native capture has no filesystem identity")
    try:
        with ExitStack() as stack:
            ancestors = _open_ancestors(snapshot.source_path, stack)
            if tuple(item for item, _ in ancestors) != snapshot.ancestors:
                raise SessionMigrateError("native capture ancestry changed")
            _check_ancestors(ancestors)
            if (
                _check_file(
                    os.stat(
                        snapshot.source_path.name, dir_fd=ancestors[-1][1], follow_symlinks=False
                    )
                )
                != snapshot.identity
            ):
                raise SessionMigrateError("native capture file changed")
    except OSError:
        raise SessionMigrateError("native capture filesystem guard failed") from None


def capture_readable_source(
    context: SourceCaptureContext,
    *,
    root: Path | None = None,
    byte_limit: int = DEFAULT_MAX_TOTAL_BYTES,
) -> NativeSnapshot:
    """Capture physical authorities, then resolve Codex storage within one root.

    Ordinary captures keep their byte-for-byte authority. Compressed/forked
    inputs retain every raw component and bind the materialized parse/audit bytes
    separately; no caller receives a fresh read disguised as the original file.
    """
    # Check an explicitly selected namespace before opening the source, including
    # ordinary uncompressed files which otherwise return through the fast path.
    if root is not None:
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or ".." in root.parts
            or ".." in context.source_path.parts
        ):
            raise SessionMigrateError("native capture root or source path is noncanonical")
        source_path = Path(os.path.abspath(context.source_path.expanduser()))
        if source_path == root or not source_path.is_relative_to(root):
            raise SessionMigrateError("native capture source is outside the authorized root")
        if any(part.is_symlink() for part in (root, *root.parents)):
            raise SessionMigrateError("native capture root cannot use symlinks")
    snapshot = capture_source(context, byte_limit=byte_limit)
    if context.source_format is not AgentFormat.CODEX:
        return snapshot
    from session_migrate.codex_storage import (
        decode_codex_storage,
        materialize_codex_history,
        strict_codex_records,
    )

    decoded = decode_codex_storage(
        snapshot.raw_bytes,
        snapshot.source_path,
        max_decoded_bytes=(
            max(1, byte_limit - len(snapshot.raw_bytes))
            if snapshot.source_path.name.endswith(".jsonl.zst")
            else byte_limit
        ),
    )
    first = next(strict_codex_records(decoded), None)
    metadata = first.value.get("payload", {}) if first is not None else {}
    if not isinstance(metadata, dict):
        metadata = {}
    linked = (
        metadata.get("history_base") is not None
        or metadata.get("attached_thread") is True
        or metadata.get("subagent_history_start_ordinal") is not None
    )
    if not linked and decoded == snapshot.raw_bytes:
        return snapshot
    boundary = root or next(
        (
            parent.parent
            for parent in snapshot.source_path.parents
            if parent.name in {"sessions", "archived_sessions"}
        ),
        snapshot.source_path.parent,
    )
    if not boundary.is_absolute() or ".." in boundary.parts:
        raise SessionMigrateError("Codex storage root must be an absolute bounded root")
    if linked:
        history = materialize_codex_history(
            boundary,
            snapshot.source_path,
            source_snapshot=snapshot,
            max_decoded_bytes=byte_limit,
            max_total_bytes=byte_limit,
        )
        decoded, components = history.materialized_bytes, history.components
    else:
        components = (snapshot,)
    resolved = NativeSnapshot(
        snapshot.source_format,
        snapshot.source_path,
        CaptureProvenance.RESOLVED_STORAGE,
        snapshot.identity,
        snapshot.ancestors,
        decoded,
        tuple(components),
        boundary,
    )
    if resolved.captured_byte_count > byte_limit:
        raise SessionMigrateError("resolved storage exceeds the aggregate capture byte limit")
    check_source_identity(resolved)
    return resolved
