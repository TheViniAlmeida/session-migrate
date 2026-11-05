"""Bounded Codex storage decoding and immutable paginated-history captures.

The storage hash identifies physical bytes. A resolved history additionally binds
every physical component and its projection; decoded bytes are never substituted
for a compressed file's source hash.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import importlib
import io
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from session_migrate.audit_adapters import strict_decode
from session_migrate.errors import SessionMigrateError, SourceReadLimitError
from session_migrate.jsonl import (
    DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_TOTAL_BYTES,
    JsonlRecord,
)
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import (
    NativeSnapshot,
    SourceCaptureContext,
    _check_ancestors,
    _check_file,
    _check_json_depth,
    _flags,
    _open_ancestors,
    capture_source,
    check_source_identity,
)

MAX_HISTORY_BYTES = 1024 * 1024 * 1024
MAX_HISTORY_DEPTH = 64
MAX_HISTORY_COMPONENTS = 64
MAX_INDEX_ENTRIES = 100_000
MAX_ZSTD_FRAMES = 1024
MAX_ZSTD_WINDOW_LOG = 28
_CHUNK_BYTES = 64 * 1024
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_ROLLOUT_NAME = re.compile(
    rf"rollout-(?P<date>\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}})-"
    rf"(?P<thread>{_UUID})(?:_(?P<rollout>{_UUID}))?\.jsonl(?:\.zst)?\Z"
)


def rollout_ids(path: Path) -> tuple[str, str] | None:
    """Return stable thread and physical rollout IDs from a canonical basename."""

    match = _ROLLOUT_NAME.fullmatch(path.name)
    if match is None:
        return None
    try:
        datetime.strptime(match["date"], "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None
    stable = str(uuid.UUID(match["thread"]))
    return stable, str(uuid.UUID(match["rollout"] or match["thread"]))


def _limit(value: int, ceiling: int, name: str) -> None:
    if type(value) is not int or not 0 < value <= ceiling:
        raise SessionMigrateError(f"Codex {name} safety limit is invalid")


def _window_log(limit: int) -> int:
    """Bound decoder working windows by the requested output budget as well."""
    return min(MAX_ZSTD_WINDOW_LOG, max(10, (max(1, limit) - 1).bit_length()))


def _stdlib_zstd() -> Any:
    try:
        return importlib.import_module("compression.zstd")
    except ImportError:
        return None


def _decode_stdlib(data: bytes, limit: int, module: Any) -> bytes:
    chunks: list[bytes] = []
    total = 0
    frames = 0
    pending = data
    try:
        while pending:
            frames += 1
            if frames > MAX_ZSTD_FRAMES:
                raise SessionMigrateError("Codex compressed storage exceeds the frame safety limit")
            decoder = module.ZstdDecompressor(
                options={module.DecompressionParameter.window_log_max: _window_log(limit)}
            )
            while True:
                chunk = decoder.decompress(pending, min(_CHUNK_BYTES, limit + 1 - total))
                pending = b""
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise SessionMigrateError("Codex decoded storage exceeds the byte safety limit")
                if decoder.eof:
                    pending = decoder.unused_data
                    break
                if decoder.needs_input:
                    raise SessionMigrateError("Codex compressed storage is incomplete")
    except SessionMigrateError:
        raise
    except (ValueError, EOFError, module.ZstdError):
        raise SessionMigrateError(
            "Codex compressed storage is invalid or exceeds its window safety limit"
        ) from None
    return b"".join(chunks)


class _InputBuffer(ctypes.Structure):
    _fields_ = [("src", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


class _OutputBuffer(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


@lru_cache(maxsize=1)
def _load_libzstd() -> Any:
    """Load the platform decoder, never a subprocess or downloaded implementation."""

    try:
        name = ctypes.util.find_library("zstd")
        if not name:
            raise OSError
        library = ctypes.CDLL(name)
        library.ZSTD_createDStream.argtypes = []
        library.ZSTD_createDStream.restype = ctypes.c_void_p
        library.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
        library.ZSTD_freeDStream.restype = ctypes.c_size_t
        library.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
        library.ZSTD_initDStream.restype = ctypes.c_size_t
        library.ZSTD_DCtx_setParameter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        library.ZSTD_DCtx_setParameter.restype = ctypes.c_size_t
        library.ZSTD_decompressStream.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_OutputBuffer),
            ctypes.POINTER(_InputBuffer),
        ]
        library.ZSTD_decompressStream.restype = ctypes.c_size_t
        library.ZSTD_isError.argtypes = [ctypes.c_size_t]
        library.ZSTD_isError.restype = ctypes.c_uint
        return library
    except (OSError, AttributeError):
        raise SessionMigrateError(
            "Codex compressed storage requires Python 3.14 compression.zstd "
            "or a system libzstd with bounded streaming support"
        ) from None


def _decode_libzstd(data: bytes, limit: int) -> bytes:
    library = _load_libzstd()
    stream = library.ZSTD_createDStream()
    if not stream:
        raise SessionMigrateError("Codex compressed decoder could not be initialized")
    try:
        initialized = library.ZSTD_initDStream(stream)
        # ZSTD_d_windowLogMax = 100 in the stable libzstd public API.
        window = library.ZSTD_DCtx_setParameter(stream, 100, _window_log(limit))
        if library.ZSTD_isError(initialized) or library.ZSTD_isError(window):
            raise SessionMigrateError("Codex compressed decoder cannot enforce its window limit")
        source = ctypes.c_char_p(data)
        input_buffer = _InputBuffer(ctypes.cast(source, ctypes.c_void_p), len(data), 0)
        output_memory = ctypes.create_string_buffer(_CHUNK_BYTES)
        chunks: list[bytes] = []
        total = 0
        frames = 0
        while True:
            before = input_buffer.pos
            output_buffer = _OutputBuffer(
                ctypes.cast(output_memory, ctypes.c_void_p), min(_CHUNK_BYTES, limit + 1 - total), 0
            )
            remaining = library.ZSTD_decompressStream(
                stream, ctypes.byref(output_buffer), ctypes.byref(input_buffer)
            )
            if library.ZSTD_isError(remaining):
                raise SessionMigrateError(
                    "Codex compressed storage is invalid or exceeds its window safety limit"
                )
            chunk = output_memory.raw[: output_buffer.pos]
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise SessionMigrateError("Codex decoded storage exceeds the byte safety limit")
            if remaining == 0:
                frames += 1
                if frames > MAX_ZSTD_FRAMES:
                    raise SessionMigrateError(
                        "Codex compressed storage exceeds the frame safety limit"
                    )
                if input_buffer.pos == input_buffer.size:
                    return b"".join(chunks)
            elif input_buffer.pos == before and not chunk:
                raise SessionMigrateError("Codex compressed storage is incomplete")
    finally:
        library.ZSTD_freeDStream(stream)


def decode_codex_storage(
    data: bytes,
    source_path: Path,
    *,
    max_decoded_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> bytes:
    """Decode plain/concatenated-zstd captured bytes with strict expansion bounds.

    A `.jsonl.zst` suffix selects Zstandard, matching native rollout storage.
    Invalid/truncated frames and trailing garbage fail instead of returning a
    plausible prefix. Python 3.11–3.13 use a guarded system libzstd fallback.
    """

    _limit(max_decoded_bytes, DEFAULT_MAX_TOTAL_BYTES, "decoded byte")
    if type(data) is not bytes or len(data) > DEFAULT_MAX_TOTAL_BYTES:
        raise SessionMigrateError("Codex captured storage bytes are invalid or oversized")
    if not source_path.name.endswith(".jsonl.zst"):
        if len(data) > max_decoded_bytes:
            raise SessionMigrateError("Codex decoded storage exceeds the byte safety limit")
        return data
    if not data:
        raise SessionMigrateError("Codex compressed storage is incomplete")
    module = _stdlib_zstd()
    return (
        _decode_stdlib(data, max_decoded_bytes, module)
        if module is not None
        else _decode_libzstd(data, max_decoded_bytes)
    )


@dataclass(frozen=True, slots=True, repr=False)
class CodexMaterialization:
    """Immutable projection plus all raw authorities, in leaf-to-root order."""

    source_path: Path
    components: tuple[NativeSnapshot, ...]
    materialized_bytes: bytes = field(repr=False)
    source_sha256: str
    decoded_sha256: str
    materialized_sha256: str
    authority_sha256: str
    session_id: str
    rollout_id: str


def materialization_authority(
    root: Path, components: tuple[NativeSnapshot, ...], materialized_bytes: bytes
) -> str:
    """Bind physical authorities and their projection with one shared digest policy."""

    if not components or type(materialized_bytes) is not bytes:
        raise SessionMigrateError("Codex materialization authority is invalid")
    if len(components) == 1:
        return components[0].authority_sha256
    try:
        manifest = {
            "schema": "codex-materialization-v1",
            "components": [
                {
                    "path": str(item.source_path.relative_to(root)),
                    "sha256": item.authority_sha256,
                }
                for item in components
            ],
            "projection_sha256": hashlib.sha256(materialized_bytes).hexdigest(),
        }
    except ValueError:
        raise SessionMigrateError("Codex materialization component is outside its root") from None
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class _HistoryBase:
    rollout_id: str
    ordinal: int
    offset: int


@dataclass(slots=True, repr=False)
class _DecodedComponent:
    snapshot: NativeSnapshot
    decoded: bytes
    records: list[dict[str, Any]]
    ends: list[int]
    metadata: dict[str, Any]
    base: _HistoryBase | None
    rollout_id: str


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise SessionMigrateError("Codex history path contains parent traversal")
    return Path(os.path.abspath(path.expanduser()))


def _inside(path: Path, root: Path) -> None:
    if path == root or not path.is_relative_to(root):
        raise SessionMigrateError("Codex history component is outside its authorized root")


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value >= 2**64:
        raise SessionMigrateError(f"Codex history {name} must be an unsigned integer")
    return value


def _identifier(value: object, name: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(uuid.UUID(value))
    except ValueError:
        raise SessionMigrateError(f"Codex history {name} is invalid") from None


def strict_codex_records(data: bytes):
    """Reject ambiguous physical JSON before any projection can discard spans."""
    stream = io.BytesIO(data)
    index, line_number = 0, 0
    while line := stream.readline(DEFAULT_MAX_RECORD_BYTES + 1):
        line_number += 1
        if len(line) > DEFAULT_MAX_RECORD_BYTES:
            raise SessionMigrateError("Codex history record exceeds the byte safety limit")
        if not line.strip():
            continue
        if index >= DEFAULT_MAX_RECORDS:
            raise SessionMigrateError("Codex history exceeds the record safety limit")
        value = strict_decode(line)
        if type(value) is not dict:
            raise SessionMigrateError("Codex history JSONL record must be an object")
        yield JsonlRecord(index, line_number, value)
        index += 1


def _decode_component(snapshot: NativeSnapshot, byte_limit: int) -> _DecodedComponent:
    decoded = decode_codex_storage(
        snapshot.raw_bytes, snapshot.source_path, max_decoded_bytes=byte_limit
    )
    try:
        records = [dict(record.value) for record in strict_codex_records(decoded)]
        for record in records:
            _check_json_depth(record)
    except (RecursionError, ValueError):
        raise SessionMigrateError("Codex history JSONL is invalid") from None
    if not records or records[0].get("type") != "session_meta":
        raise SessionMigrateError("Codex history must start with session metadata")
    if any(record.get("type") == "session_meta" for record in records[1:]):
        raise SessionMigrateError("Codex history contains repeated session metadata")
    metadata = records[0].get("payload")
    if not isinstance(metadata, dict):
        raise SessionMigrateError("Codex history session metadata is invalid")
    if metadata.get("subagent_history_start_ordinal") is not None:
        raise SessionMigrateError("Codex paginated subagent history projection is not supported")
    stable_id = _identifier(metadata.get("id") or metadata.get("session_id"), "thread ID")
    ids = rollout_ids(snapshot.source_path)
    if ids is not None and ids[0] != stable_id:
        raise SessionMigrateError("Codex history filename belongs to another stable thread")
    base_value = metadata.get("history_base")
    base = None
    if base_value is not None:
        if not isinstance(base_value, dict):
            raise SessionMigrateError("Codex history_base must be an object")
        base = _HistoryBase(
            _identifier(base_value.get("thread_id"), "history_base rollout ID"),
            _integer(
                base_value.get("end_ordinal_exclusive"), "history_base ordinal", positive=True
            ),
            _integer(base_value.get("end_byte_offset"), "history_base offset", positive=True),
        )
    mode = metadata.get("history_mode", "legacy")
    if not isinstance(mode, str) or mode not in {"legacy", "paginated"}:
        raise SessionMigrateError("Codex history mode is unsupported")
    if base is not None and mode != "paginated":
        raise SessionMigrateError("Codex history_base requires paginated history")
    attached = metadata.get("attached_thread", False)
    if type(attached) is not bool:
        raise SessionMigrateError("Codex attached-thread flag is invalid")
    if attached and mode != "paginated":
        raise SessionMigrateError("Codex attached-thread history requires paginated history")
    if mode == "paginated":
        start = base.ordinal if base else 0
        for index, record in enumerate(records):
            ordinal = _integer(record.get("ordinal"), "record ordinal")
            if ordinal != start + index:
                raise SessionMigrateError("Codex history_base/root ordinals must be contiguous")
    ends: list[int] = []
    offset = 0
    for line in io.BytesIO(decoded):
        offset += len(line)
        if line.strip():
            ends.append(offset)
    return _DecodedComponent(
        snapshot, decoded, records, ends, metadata, base, ids[1] if ids else stable_id
    )


def _index_rollouts(
    root: Path,
    *,
    max_entries: int = MAX_INDEX_ENTRIES,
    directory_witnesses: set[Path] | None = None,
) -> dict[str, Path]:
    """Index names only; every contributing body is later captured with guards."""

    index: dict[str, Path] = {}
    seen = 0

    def visit(directory: Path, descriptor: int, depth: int) -> None:
        nonlocal seen
        if directory_witnesses is not None:
            directory_witnesses.add(directory)
        if depth > MAX_HISTORY_DEPTH:
            raise SessionMigrateError(
                "Codex history index exceeds the directory depth safety limit"
            )
        with os.scandir(descriptor) as entries:
            for entry in entries:
                seen += 1
                if seen > max_entries:
                    raise SessionMigrateError("Codex history index exceeds the entry safety limit")
                if entry.is_symlink():
                    raise SessionMigrateError("Codex history index contains a symlink")
                path = directory / entry.name
                if entry.is_dir(follow_symlinks=False):
                    visit_child(path, descriptor, depth + 1)
                elif (ids := rollout_ids(path)) is not None:
                    if not entry.is_file(follow_symlinks=False):
                        raise SessionMigrateError("Codex history component is not a regular file")
                    if ids[1] in index:
                        raise SessionMigrateError(
                            "Codex history has duplicate physical rollout IDs"
                        )
                    index[ids[1]] = path

    def visit_child(path: Path, parent_descriptor: int, depth: int) -> None:
        descriptor = os.open(path.name, _flags(directory=True), dir_fd=parent_descriptor)
        try:
            before = os.fstat(descriptor)
            visit(path, descriptor, depth)
            after = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(after.st_mode) or (before.st_dev, before.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise SessionMigrateError("Codex history index directory changed")
        finally:
            os.close(descriptor)

    try:
        with ExitStack() as stack:
            ancestors = _open_ancestors(root / ".codex-history-index", stack)
            _check_ancestors(ancestors)
            root_descriptor = ancestors[-1][1]
            managed = []
            for name in ("sessions", "archived_sessions"):
                try:
                    metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SessionMigrateError("Codex history directory is not a guarded directory")
                managed.append(root / name)
            if managed:
                for path in managed:
                    visit_child(path, root_descriptor, 0)
            else:
                visit(root, root_descriptor, 0)
            _check_ancestors(ancestors)
    except OSError:
        raise SessionMigrateError("Codex history index filesystem guard failed") from None
    return index


class _ProbeReader(io.RawIOBase):
    def __init__(self, descriptor: int, budget: list[int]) -> None:
        self.descriptor, self.budget = descriptor, budget

    def readable(self) -> bool:
        return True

    def readinto(self, output: Any) -> int:
        if self.budget[0] < 1:
            raise SourceReadLimitError("Codex dependency metadata exceeds the read budget")
        count = min(len(output), 8 * 1024, self.budget[0])
        data = os.read(self.descriptor, count)
        if len(data) > self.budget[0]:
            raise SourceReadLimitError("Codex dependency metadata exceeds the read budget")
        self.budget[0] -= len(data)
        output[: len(data)] = data
        return len(data)


def _libzstd_metadata(reader: _ProbeReader, output_limit: int) -> bytes:
    library = _load_libzstd()
    stream = library.ZSTD_createDStream()
    if not stream:
        raise SessionMigrateError("Codex metadata decoder could not be initialized")
    output = bytearray()
    try:
        initialized = library.ZSTD_initDStream(stream)
        window = library.ZSTD_DCtx_setParameter(stream, 100, _window_log(output_limit))
        if library.ZSTD_isError(initialized) or library.ZSTD_isError(window):
            raise SessionMigrateError("Codex metadata decoder cannot enforce its window limit")
        memory = ctypes.create_string_buffer(_CHUNK_BYTES)
        frames = 0
        while raw := reader.read(_CHUNK_BYTES):
            source = ctypes.c_char_p(raw)
            input_buffer = _InputBuffer(ctypes.cast(source, ctypes.c_void_p), len(raw), 0)
            while True:
                before = input_buffer.pos
                target = _OutputBuffer(
                    ctypes.cast(memory, ctypes.c_void_p),
                    min(_CHUNK_BYTES, output_limit + 1 - len(output)),
                    0,
                )
                remaining = library.ZSTD_decompressStream(
                    stream, ctypes.byref(target), ctypes.byref(input_buffer)
                )
                if library.ZSTD_isError(remaining):
                    raise SessionMigrateError("Codex dependency metadata compression is invalid")
                output.extend(memory.raw[: target.pos])
                if b"\n" in output:
                    line = bytes(output).split(b"\n", 1)[0] + b"\n"
                    if len(line) <= output_limit:
                        return line
                if len(output) > output_limit:
                    raise SourceReadLimitError("Codex dependency metadata exceeds the read budget")
                if remaining == 0:
                    frames += 1
                    if frames > MAX_ZSTD_FRAMES:
                        raise SessionMigrateError("Codex metadata exceeds the frame safety limit")
                if input_buffer.pos == len(raw) and (not target.pos or target.pos < target.size):
                    break
                if input_buffer.pos == before and not target.pos:
                    raise SessionMigrateError("Codex dependency metadata compression is incomplete")
        raise SessionMigrateError("Codex dependency metadata is incomplete")
    finally:
        library.ZSTD_freeDStream(stream)


def _probe_metadata(
    path: Path, input_budget: list[int], output_budget: list[int]
) -> dict[str, Any]:
    """Read an unverified dependency hint; full capture must validate all frames later."""
    try:
        with ExitStack() as stack:
            ancestors = _open_ancestors(path, stack)
            _check_ancestors(ancestors)
            parent = ancestors[-1][1]
            before = _check_file(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
            descriptor = os.open(path.name, _flags(), dir_fd=parent)
            stack.callback(os.close, descriptor)
            if _check_file(os.fstat(descriptor)) != before:
                raise SessionMigrateError("Codex dependency metadata changed")
            reader = _ProbeReader(descriptor, input_budget)
            if path.name.endswith(".jsonl.zst"):
                module = _stdlib_zstd()
                if module is None:
                    line = _libzstd_metadata(reader, output_budget[0])
                else:
                    try:
                        with module.ZstdFile(
                            reader,
                            options={
                                module.DecompressionParameter.window_log_max: _window_log(
                                    output_budget[0]
                                )
                            },
                        ) as decoded:
                            line = decoded.readline(output_budget[0] + 1)
                    except (module.ZstdError, EOFError, ValueError):
                        raise SessionMigrateError("Codex dependency metadata is invalid") from None
            else:
                line = io.BufferedReader(reader).readline(output_budget[0] + 1)
            if len(line) > output_budget[0]:
                raise SourceReadLimitError("Codex dependency metadata exceeds the read budget")
            output_budget[0] -= len(line)
            if (
                _check_file(os.fstat(descriptor)) != before
                or _check_file(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != before
            ):
                raise SessionMigrateError("Codex dependency metadata changed")
            _check_ancestors(ancestors)
            records = list(strict_codex_records(line))
            if len(records) != 1 or records[0].value.get("type") != "session_meta":
                raise SessionMigrateError("Codex dependency metadata must start with session_meta")
            payload = records[0].value.get("payload")
            if not isinstance(payload, dict):
                raise SessionMigrateError("Codex dependency metadata is invalid")
            _check_json_depth(payload)
            return payload
    except OSError:
        raise SessionMigrateError("Codex dependency metadata filesystem guard failed") from None


def codex_history_read_paths(
    root: Path,
    source_path: Path,
    *,
    max_probe_bytes: int = 8 * 1024 * 1024,
    max_entries: int = MAX_INDEX_ENTRIES,
) -> tuple[Path, ...]:
    """Return dependency/file and namespace witnesses using bounded metadata only."""
    _limit(max_probe_bytes, DEFAULT_MAX_TOTAL_BYTES, "metadata probe byte")
    root, source_path = _absolute(root), _absolute(source_path)
    paths: set[Path] = set()
    input_budget, output_budget = [max_probe_bytes], [max_probe_bytes]
    index = None
    seen: set[str] = set()
    path = source_path
    for _ in range(MAX_HISTORY_DEPTH):
        _inside(path, root)
        paths.add(path)
        metadata = _probe_metadata(path, input_budget, output_budget)
        base = metadata.get("history_base")
        if base is None:
            return tuple(sorted(paths))
        if not isinstance(base, dict):
            raise SessionMigrateError("Codex history_base is invalid")
        identifier = _identifier(base.get("thread_id"), "history_base rollout ID")
        if identifier in seen:
            raise SessionMigrateError("Codex history dependency cycle")
        seen.add(identifier)
        if index is None:
            index = _index_rollouts(root, max_entries=max_entries, directory_witnesses=paths)
            paths.update((root, root / "sessions", root / "archived_sessions"))
        path = index.get(identifier)
        if path is None:
            raise SessionMigrateError("Codex history_base source rollout is unresolved")
    raise SessionMigrateError("Codex history dependency depth exceeds the safety limit")


def _cutoff(component: _DecodedComponent, base: _HistoryBase) -> list[dict[str, Any]]:
    if base.offset > len(component.decoded) or base.offset not in component.ends:
        raise SessionMigrateError(
            "Codex history_base byte offset is not a complete record boundary"
        )
    if component.decoded[base.offset - 1 : base.offset] != b"\n":
        raise SessionMigrateError("Codex history_base byte offset is not newline terminated")
    count = component.ends.index(base.offset) + 1
    prefix = component.records[:count]
    if prefix[-1].get("ordinal") != base.ordinal - 1:
        raise SessionMigrateError("Codex history_base byte and ordinal cutoffs disagree")
    if component.records[0]["ordinal"] >= base.ordinal:
        raise SessionMigrateError("Codex history_base cutoff does not include its source metadata")
    return prefix


def _attached_start(leaf: _DecodedComponent) -> int | None:
    metadata = leaf.metadata
    attached = metadata.get("attached_thread", False)
    if not attached:
        return None
    parent = _identifier(metadata.get("forked_from_id"), "attached fork parent")
    cutoff = metadata.get("forked_from_ordinal_exclusive")
    if (
        cutoff is None
        and leaf.base
        and (leaf.base.rollout_id == parent or leaf.rollout_id == metadata.get("id"))
    ):
        cutoff = leaf.base.ordinal
    if cutoff is None:
        raise SessionMigrateError("Codex attached-thread logical fork cutoff is unresolved")
    cutoff = _integer(cutoff, "attached fork ordinal", positive=True)
    if leaf.base is None:
        raise SessionMigrateError("Codex attached-thread physical history boundary is unresolved")
    return leaf.base.ordinal + 1 if leaf.base.ordinal <= cutoff else None


def _encode_projection(records: list[dict[str, Any]], byte_limit: int) -> bytes:
    output = io.BytesIO()
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if output.tell() + len(encoded) > byte_limit:
            raise SessionMigrateError("Codex materialized history exceeds the byte safety limit")
        output.write(encoded)
    return output.getvalue()


def materialize_codex_history(
    root: Path,
    source_path: Path,
    *,
    source_snapshot: NativeSnapshot | None = None,
    capture: Callable[..., NativeSnapshot] | None = None,
    max_decoded_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_total_bytes: int = MAX_HISTORY_BYTES,
    max_depth: int = MAX_HISTORY_DEPTH,
    max_components: int = MAX_HISTORY_COMPONENTS,
) -> CodexMaterialization:
    """Capture and validate a full physical lineage before projecting any record.

    Root is the explicitly authorized Codex home (active and archived stores).
    Captures are reused for parsing/auditing; none of the source bodies is reopened.
    Candidate-name and file-identity guards are rechecked after resolution.
    """

    _limit(max_decoded_bytes, DEFAULT_MAX_TOTAL_BYTES, "decoded byte")
    _limit(max_total_bytes, MAX_HISTORY_BYTES, "total byte")
    _limit(max_depth, MAX_HISTORY_DEPTH, "depth")
    _limit(max_components, MAX_HISTORY_COMPONENTS, "component")
    root, source_path = _absolute(root), _absolute(source_path)
    _inside(source_path, root)
    capture = capture or capture_source
    index: dict[str, Path] | None = None
    components: list[_DecodedComponent] = []
    seen: set[str] = set()
    total = 0
    path = source_path
    while True:
        if len(components) >= min(max_depth, max_components):
            raise SessionMigrateError("Codex history exceeds the depth or component safety limit")
        _inside(path, root)
        remaining = max_total_bytes - total
        if remaining < 1:
            raise SessionMigrateError("Codex history exceeds the total byte safety limit")
        raw_limit = min(max_decoded_bytes, remaining)
        snapshot = (
            source_snapshot
            if not components and source_snapshot is not None
            else capture(SourceCaptureContext(AgentFormat.CODEX, path), byte_limit=raw_limit)
        )
        if (
            not isinstance(snapshot, NativeSnapshot)
            or snapshot.source_path != path
            or snapshot.source_format != AgentFormat.CODEX
        ):
            raise SessionMigrateError("Codex history capture context does not match its component")
        decoded_limit = min(max_decoded_bytes, remaining - len(snapshot.raw_bytes))
        if decoded_limit < 1:
            raise SessionMigrateError("Codex history exceeds the total byte safety limit")
        component = _decode_component(snapshot, decoded_limit)
        if (components or component.base is not None) and (
            component.metadata.get("history_mode", "legacy") != "paginated"
        ):
            raise SessionMigrateError("Codex history_base source rollout is not paginated")
        if component.rollout_id in seen:
            raise SessionMigrateError("Codex history contains a physical rollout cycle")
        seen.add(component.rollout_id)
        total += len(snapshot.raw_bytes) + len(component.decoded)
        if total > max_total_bytes:
            raise SessionMigrateError("Codex history exceeds the total byte safety limit")
        components.append(component)
        if component.base is None:
            break
        if component.base.rollout_id in seen:
            raise SessionMigrateError("Codex history contains a physical rollout cycle")
        if index is None:
            index = _index_rollouts(root)
        path = index.get(component.base.rollout_id)
        if path is None:
            raise SessionMigrateError("Codex history_base source rollout is unresolved")
    leaf = components[0]
    start = 0
    visible_components = len(components)
    attached_boundary_found = False
    for position, component in enumerate(components):
        attached_start = _attached_start(component)
        if attached_start is not None:
            visible_components, start = position + 1, attached_start
            attached_boundary_found = True
            break
    if any(item.metadata.get("attached_thread") for item in components) and (
        not attached_boundary_found
    ):
        raise SessionMigrateError("Codex attached-thread inherited history boundary is unresolved")
    projected: list[dict[str, Any]] = []
    for position in range(len(components) - 1, -1, -1):
        component = components[position]
        records = (
            _cutoff(component, components[position - 1].base) if position > 0 else component.records
        )
        if position < visible_components:
            projected.extend(records[1:])
        if len(projected) >= DEFAULT_MAX_RECORDS:
            raise SessionMigrateError("Codex materialized history exceeds the record safety limit")
    mode = leaf.metadata.get("history_mode", "legacy")
    if mode == "paginated":
        projected = [record for record in projected if record["ordinal"] >= start]
        metadata = dict(leaf.metadata)
        metadata.pop("history_base", None)
        metadata.pop("subagent_history_start_ordinal", None)
        header = {**leaf.records[0], "payload": metadata}
        projected = [header, *projected]
        projected = [{**record, "ordinal": ordinal} for ordinal, record in enumerate(projected)]
        materialized = _encode_projection(
            projected, min(max_decoded_bytes, max_total_bytes - total)
        )
    else:
        if leaf.metadata.get("subagent_history_start_ordinal") is not None:
            raise SessionMigrateError("Codex legacy subagent projection is unsupported")
        materialized = leaf.decoded
    if len(materialized) > max_decoded_bytes or total + len(materialized) > max_total_bytes:
        raise SessionMigrateError("Codex materialized history exceeds the byte safety limit")
    if index is not None and _index_rollouts(root) != index:
        raise SessionMigrateError("Codex history candidate index changed during capture")
    for component in components:
        check_source_identity(component.snapshot)
    materialized_hash = hashlib.sha256(materialized).hexdigest()
    snapshots = tuple(item.snapshot for item in components)
    authority = materialization_authority(root, snapshots, materialized)
    return CodexMaterialization(
        source_path,
        snapshots,
        materialized,
        leaf.snapshot.authority_sha256,
        hashlib.sha256(leaf.decoded).hexdigest(),
        materialized_hash,
        authority,
        str(leaf.metadata.get("id") or leaf.metadata["session_id"]),
        leaf.rollout_id,
    )
