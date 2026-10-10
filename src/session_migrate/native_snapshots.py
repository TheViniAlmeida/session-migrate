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
    authority_sha256: str = field(init=False)

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
        if self.provenance == CaptureProvenance.EXPORTED_BYTES:
            if self.identity is not None or self.ancestors:
                raise SessionMigrateError("exported capture cannot carry filesystem identity")
        elif not isinstance(self.identity, FileIdentity) or not self.ancestors:
            raise SessionMigrateError("file capture requires filesystem identity")
        object.__setattr__(self, "authority_sha256", hashlib.sha256(self._raw_bytes).hexdigest())

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

    def parse(self) -> Session:
        from session_migrate.formats import claude, codex, opencode

        try:
            if self.source_format == AgentFormat.CLAUDE:
                return claude.parse_records(
                    self.decoded_records(),
                    source_path=self.source_path,
                    source_sha256=self.authority_sha256,
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
