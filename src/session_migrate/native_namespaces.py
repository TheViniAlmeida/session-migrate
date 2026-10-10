"""Native layout/ID qualification, separate from semantic projection authority."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

from session_migrate.discovery import normalized_source_id
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.lineage import NativeNamespace, QualifiedIdentity, RootWitness
from session_migrate.model import AgentFormat, Session
from session_migrate.native_snapshots import (
    CaptureProvenance,
    NativeSnapshot,
    check_source_identity,
)

_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DATABASE_NAMES = {
    AgentFormat.HERMES: "state.db",
    AgentFormat.MASTRACODE: "mastra.db",
    AgentFormat.DEVIN: "sessions.db",
}
_SEAL = object()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False, init=False)
class _ExecutionContext:
    namespace: NativeNamespace
    path: Path
    authority_sha256: str
    _seal: object

    def __init__(self):
        raise SessionMigrateError("Native execution context is private.")


_CONTEXTS = WeakKeyDictionary()


def _context_binding(context):
    return (
        context.namespace.format,
        context.namespace.canonical_root,
        context.namespace.native_id,
        str(context.path),
        context.authority_sha256,
        context._seal,
    )


def _bind_execution_context(
    namespace: NativeNamespace,
    session: Session,
    *,
    native_path: Path,
    environ: Mapping[str, str],
    client: Path | None = None,
    snapshot: NativeSnapshot | None = None,
    export_proof=None,
) -> _ExecutionContext:
    """Driver-only capability after its selected loader/export completed."""
    format = AgentFormat(namespace.format)
    if (
        not session.session_id
        or normalized_source_id(format, session.session_id) != namespace.native_id
    ):
        raise SessionMigrateError("Observed native ID differs from its reservation.")
    root = Path(namespace.canonical_root)
    if format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
        from session_migrate.conversion import _validate_export_proof

        _validate_export_proof(export_proof, session, environ, client)
        base = (
            Path(environ["XDG_DATA_HOME"])
            if environ.get("XDG_DATA_HOME")
            else Path(environ["HOME"]) / ".local/share"
        )
        if (
            client is None
            or _absolute(base / format.value) != root
            or native_path != Path(f"{format.value}:{namespace.native_id}")
        ):
            raise SessionMigrateError("Native export execution root cannot be bound.")
        if not Path(client).resolve(strict=True).is_file():
            raise SessionMigrateError("Native export client cannot be bound.")
        if snapshot is not None and (
            snapshot.provenance is not CaptureProvenance.EXPORTED_BYTES
            or snapshot.source_path != native_path
        ):
            raise SessionMigrateError("Portable capture cannot qualify native export context.")
    elif format in _DATABASE_NAMES:
        from session_migrate.conversion import _validate_export_proof

        _validate_export_proof(export_proof, session, {}, None, native_path=native_path)
        if _absolute(native_path) != root:
            raise SessionMigrateError("Observed database path differs from its native root.")
    elif snapshot is not None:
        if snapshot.provenance is not CaptureProvenance.FILE or snapshot.source_path != _absolute(
            native_path
        ):
            raise SessionMigrateError("Portable capture cannot qualify a native file.")
        check_source_identity(snapshot)
    else:
        from session_migrate.conversion import _validate_export_proof

        _validate_export_proof(export_proof, session, {}, None, native_path=native_path)
    if snapshot is not None and snapshot.authority_sha256 != session.source_sha256:
        raise SessionMigrateError("Native observation authority differs from its capture.")
    context = object.__new__(_ExecutionContext)
    for name, value in {
        "namespace": namespace,
        "path": native_path,
        "authority_sha256": session.source_sha256,
        "_seal": _SEAL,
    }.items():
        object.__setattr__(context, name, value)
    _CONTEXTS[context] = _context_binding(context)
    return context


def _absolute(path: Path) -> Path:
    if ".." in Path(path).parts:
        raise SessionMigrateError("Native namespace contains a parent traversal.")
    path = Path(os.path.abspath(path))
    if ".." in Path(path).parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise SessionMigrateError("Native namespace is unavailable or a symlink.")
    return path


def ensure_migration_state_home(path: Path) -> None:
    """Create missing private parents only through pinned, safe ancestors."""
    path = _absolute(path)
    chain = []

    def check():
        for parent, name, descriptor, device, inode in chain:
            opened, named = (
                os.fstat(descriptor),
                os.stat(name, dir_fd=parent, follow_symlinks=False),
            )
            if (opened.st_dev, opened.st_ino) != (device, inode) or (
                named.st_dev,
                named.st_ino,
            ) != (device, inode):
                raise SessionMigrateError("Migration state namespace changed.")

    try:
        with ExitStack() as stack:
            parent = os.open("/", _FLAGS | os.O_DIRECTORY)
            stack.callback(os.close, parent)
            for index, name in enumerate(path.parts[1:]):
                info = os.fstat(parent)
                if info.st_uid not in (0, os.getuid()) or (
                    info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
                ):
                    raise SessionMigrateError("Migration state ancestry is unsafe.")
                check()
                try:
                    descriptor = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=parent)
                except FileNotFoundError:
                    os.mkdir(name, 0o700, dir_fd=parent)
                    os.fsync(parent)
                    descriptor = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=parent)
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                if index == len(path.parts) - 2 and (
                    info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise SessionMigrateError("Migration state directory must be private.")
                chain.append((parent, name, descriptor, info.st_dev, info.st_ino))
                check()
                parent = descriptor
    except OSError:
        raise SessionMigrateError("Migration state directory is unavailable.") from None


def reserve_namespace(format: AgentFormat, root: Path, native_id: str) -> NativeNamespace:
    """Reserve a lexical native root/ID without creating a root or a witness."""
    format = AgentFormat(format)
    root = _absolute(root)
    if format in _DATABASE_NAMES and root.name != _DATABASE_NAMES[format]:
        root = root / _DATABASE_NAMES[format]
    return NativeNamespace(format.value, str(root), normalized_source_id(format, native_id))


def _witness(path: Path) -> RootWitness:
    """Pin ancestors and compare FD/name identities around a metadata read."""
    chain = []
    with ExitStack() as stack:
        parent = os.open("/", _FLAGS | os.O_DIRECTORY)
        stack.callback(os.close, parent)
        for index, name in enumerate(path.parts[1:]):
            last = index == len(path.parts) - 2
            descriptor = os.open(name, _FLAGS | (0 if last else os.O_DIRECTORY), dir_fd=parent)
            stack.callback(os.close, descriptor)
            opened = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise SessionMigrateError("Native namespace changed.")
            if not (
                stat.S_ISDIR(opened.st_mode)
                or (last and stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1)
            ):
                raise SessionMigrateError("Native namespace is not a regular file or directory.")
            chain.append((parent, name, descriptor, opened.st_dev, opened.st_ino))
            parent = descriptor
        for parent, name, descriptor, device, inode in chain:
            opened, named = (
                os.fstat(descriptor),
                os.stat(name, dir_fd=parent, follow_symlinks=False),
            )
            if (opened.st_dev, opened.st_ino) != (device, inode) or (
                named.st_dev,
                named.st_ino,
            ) != (device, inode):
                raise SessionMigrateError("Native namespace changed.")
        return RootWitness(chain[-1][3], chain[-1][4])


def qualify_namespace(
    session: Session,
    namespace: NativeNamespace,
    profile_fingerprint: str,
    *,
    native_path: Path | None = None,
    execution_context: _ExecutionContext | None = None,
) -> QualifiedIdentity | None:
    """Qualify observed native metadata/layout; portable ambiguity stays None.

    ``native_path`` is the exact installer/loader path selected by orchestration.
    It does not establish semantic coverage, native turn closure, or CAS.
    """
    try:
        format = AgentFormat(namespace.format)
        if (
            session.source_format is not format
            or not session.session_id
            or normalized_source_id(format, session.session_id) != namespace.native_id
        ):
            return None
        root = _absolute(Path(namespace.canonical_root))
        path = native_path if native_path is not None else session.source_path
        if execution_context is None:
            return None
        if execution_context not in _CONTEXTS or _CONTEXTS[execution_context] != _context_binding(
            execution_context
        ):
            return None
        if (
            type(execution_context) is not _ExecutionContext
            or execution_context._seal is not _SEAL
            or execution_context.namespace != namespace
            or execution_context.path != path
            or execution_context.authority_sha256 != session.source_sha256
        ):
            return None
        if format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
            if execution_context is None or path != Path(f"{format.value}:{namespace.native_id}"):
                return None
        elif format in _DATABASE_NAMES:
            if execution_context is None or _absolute(path) != root:
                return None
        else:
            path = _absolute(path)
            relative = path.relative_to(root)
            if format is AgentFormat.CLAUDE:
                if (
                    session.cwd is None
                    or relative
                    != Path("projects")
                    / claude.project_directory_name(session.cwd)
                    / f"{namespace.native_id}.jsonl"
                ):
                    return None
            elif format is AgentFormat.CODEX:
                if not (
                    (len(relative.parts) == 5 and relative.parts[0] == "sessions")
                    or (len(relative.parts) == 2 and relative.parts[0] == "archived_sessions")
                ) or not relative.name.endswith(f"-{namespace.native_id}.jsonl"):
                    return None
            elif native_path is None:
                # Existing adapters have different directory/file grammars.
                # Their exact installer-selected path is required for now.
                return None
            _witness(path)
        return QualifiedIdentity(namespace, profile_fingerprint, _witness(root))
    except (OSError, ValueError, SessionMigrateError):
        return None
