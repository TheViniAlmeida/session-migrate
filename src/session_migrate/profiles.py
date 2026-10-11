"""Explicit, content-free profiles and root-qualified session selection.

Persistence is opt-in; creating a Profile never touches disk or process state.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any

from session_migrate.catalog import Catalog, CatalogEntry, CatalogRoot, CatalogTransferSource
from session_migrate.conversion import (
    default_migration_state_home,
    load_kilo_session,
    load_opencode_session,
    load_session,
)
from session_migrate.discovery import normalized_source_id
from session_migrate.errors import SessionMigrateError, SourceReadLimitError
from session_migrate.model import AgentFormat, Session, TargetFormat
from session_migrate.operations import (
    CancellationToken,
    OperationKind,
    OperationRequest,
    OperationResult,
    PreparedOperation,
    ProgressCallback,
    TransferReceipt,
    _catalog_virtual_source_environment,
    _load_shared_database_source,
    _temporary_root,
    prepare_operation,
    run_operation,
)

PROFILE_SCHEMA_VERSION = 1
ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    }
)
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_VIRTUAL_CLI = frozenset({AgentFormat.OPENCODE, AgentFormat.KILO})
_SHARED = frozenset({AgentFormat.HERMES, AgentFormat.MASTRACODE, AgentFormat.DEVIN})
_SOURCE_CLI = _VIRTUAL_CLI
_TARGET_CLI = _VIRTUAL_CLI | {AgentFormat.ANTIGRAVITY, AgentFormat.CURSOR, AgentFormat.HERMES}
_MAX_PROFILE_BYTES = 1_048_576


def _label(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise SessionMigrateError(f"profile {name} must be a nonempty bounded string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SessionMigrateError(f"profile {name} contains controls")
    return value


def _path(value: Path | str, name: str) -> Path:
    if not isinstance(value, (Path, str)):
        raise SessionMigrateError(f"profile {name} must be an absolute path")
    result = Path(_label(str(value), name))
    if not result.is_absolute() or ".." in result.parts:
        raise SessionMigrateError(
            f"profile {name} must be an absolute path without parent traversal"
        )
    return result


def _format(value: AgentFormat | str) -> AgentFormat:
    try:
        return AgentFormat(value)
    except (TypeError, ValueError) as exc:
        raise SessionMigrateError("profile format is unsupported") from exc


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    format: AgentFormat
    root: Path
    cli: Path | None = None
    environ: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _PROFILE_ID.fullmatch(self.id):
            raise SessionMigrateError("profile ID must be a bounded filename-safe identifier")
        _label(self.name, "name")
        object.__setattr__(self, "format", _format(self.format))
        object.__setattr__(self, "root", _path(self.root, "root").resolve())
        if self.cli is not None:
            object.__setattr__(self, "cli", _path(self.cli, "CLI"))
        if not isinstance(self.environ, Mapping):
            raise SessionMigrateError("profile environment must be a mapping")
        values = {}
        for key, value in self.environ.items():
            if key not in ENVIRONMENT_KEYS:
                raise SessionMigrateError(
                    "profile environment accepts only HOME/XDG path overrides"
                )
            if not isinstance(value, str):
                raise SessionMigrateError("profile environment paths must be strings")
            values[key] = str(_path(value, "environment path"))
        object.__setattr__(self, "environ", MappingProxyType(values))

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return a fresh runtime environment, never another profile's environment."""
        values = dict(os.environ if base is None else base)
        values.update(self.environ)
        # A changed HOME must not retain inherited XDG paths from another home.
        if "HOME" in self.environ:
            home = Path(self.environ["HOME"])
            defaults = {
                "XDG_DATA_HOME": ".local/share",
                "XDG_CONFIG_HOME": ".config",
                "XDG_CACHE_HOME": ".cache",
                "XDG_STATE_HOME": ".local/state",
            }
            for key, relative in defaults.items():
                if key not in self.environ:
                    values[key] = str(home / relative)
            if "XDG_RUNTIME_DIR" not in self.environ:
                values.pop("XDG_RUNTIME_DIR", None)
        return values

    @property
    def fingerprint(self) -> str:
        return sha256(json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "format": self.format.value,
            "root": str(self.root),
            "cli": str(self.cli) if self.cli else None,
            "environ": dict(self.environ),
        }


@dataclass(frozen=True, slots=True)
class SessionRef:
    format: AgentFormat
    root: Path
    native_id: str
    profile_id: str | None = None
    catalog_id: str | None = field(default=None, compare=False)
    profile_fingerprint: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", _format(self.format))
        object.__setattr__(self, "root", _path(self.root, "root"))
        _label(self.native_id, "native ID")


@dataclass(frozen=True, slots=True)
class ProfileSession:
    profile_id: str
    entry: CatalogEntry
    ref: SessionRef | None
    # Catalog ordering is filesystem mtime (native time_updated for shared stores).
    ordering_source: str
    started_at_source: str = "native_metadata"


def _validate_source_root(profile: Profile) -> None:
    if profile.root.is_dir():
        return
    if (
        profile.format == AgentFormat.MASTRACODE
        and profile.root.name == "mastra.db"
        and profile.root.is_file()
    ):
        return
    raise SessionMigrateError(
        "profile root is missing or is not a directory/canonical MastraCode mastra.db file"
    )


def _root(catalog: Catalog, profile: Profile) -> CatalogRoot:
    _validate_source_root(profile)
    matches = [
        root
        for root in catalog.roots(enabled_only=True)
        if root.format == profile.format.value and Path(root.path) == profile.root.resolve()
    ]
    if len(matches) != 1:
        raise SessionMigrateError(
            "profile root is not registered with its exact format in the catalog"
        )
    return matches[0]


def _cli(profile: Profile, *, source: bool) -> Path | None:
    supported = _SOURCE_CLI if source else _TARGET_CLI
    if profile.cli is not None and profile.format not in supported:
        raise SessionMigrateError("profile CLI is not supported for this operation direction")
    if profile.format in _VIRTUAL_CLI and profile.cli is None:
        raise SessionMigrateError("OpenCode/Kilo profiles require an explicit CLI")
    if profile.cli is not None and (
        not profile.cli.is_file() or not os.access(profile.cli, os.X_OK)
    ):
        raise SessionMigrateError("profile CLI is missing or is not executable")
    return profile.cli


def _source_environment(profile: Profile, base: Mapping[str, str] | None) -> dict[str, str]:
    values = profile.environment(base)
    if profile.format in _VIRTUAL_CLI:
        _cli(profile, source=True)
        values = _catalog_virtual_source_environment(profile.format, profile.root, values)
        # An explicit override must agree with the selected store, not be silently ignored.
        if "XDG_DATA_HOME" in profile.environ:
            selected = Path(profile.environ["XDG_DATA_HOME"]) / profile.format.value
            if not selected.is_dir() or not selected.samefile(profile.root):
                raise SessionMigrateError(
                    "profile XDG_DATA_HOME does not match its native store root"
                )
    return values


def profile_from_catalog_root(
    root: CatalogRoot,
    *,
    profile_id: str,
    name: str,
    agent_format: AgentFormat,
    cli: Path | None = None,
    environ: Mapping[str, str],
) -> Profile:
    """Import a known root only with explicit format and runtime path choices."""
    if root.format != _format(agent_format).value:
        raise SessionMigrateError("profile format does not match the catalog root")
    if not root.enabled:
        raise SessionMigrateError("catalog root is disabled")
    profile = Profile(profile_id, name, agent_format, Path(root.path), cli, environ)
    _validate_source_root(profile)
    _source_environment(profile, {})
    return profile


def list_profile_sessions(
    catalog: Catalog,
    profile: Profile,
    *,
    query: str | None = None,
    include_missing: bool = True,
    statuses: Sequence[str] = (),
    kinds: Sequence[str] = (),
    lifecycles: Sequence[str] = (),
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ProfileSession]:
    root = _root(catalog, profile)
    entries = catalog.list_sessions(
        root_id=root.id,
        agent_format=profile.format,
        query=query,
        include_paths=True,
        include_missing=include_missing,
        statuses=statuses,
        kinds=kinds,
        lifecycles=lifecycles,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    ordering = "filesystem_mtime"
    if profile.format in _VIRTUAL_CLI:
        ordering = "native_time_updated_or_database_mtime"
    elif profile.format in _SHARED:
        ordering = "native_updated_at_or_database_mtime"
    return [
        ProfileSession(
            profile.id,
            entry,
            SessionRef(
                profile.format,
                profile.root,
                entry.session_id,
                profile.id,
                entry.catalog_id,
                profile.fingerprint,
            )
            if entry.session_id
            else None,
            ordering,
        )
        for entry in entries
    ]


def _resolve_entry(catalog: Catalog, profile: Profile, ref: SessionRef) -> CatalogEntry:
    root = _root(catalog, profile)
    if (ref.profile_id is not None and ref.profile_id != profile.id) or (
        ref.format != profile.format or ref.root != profile.root
    ):
        raise SessionMigrateError(
            "session reference does not match the selected profile/root/format"
        )
    if ref.profile_fingerprint is not None and ref.profile_fingerprint != profile.fingerprint:
        raise SessionMigrateError(
            "session reference profile CLI/environment changed; select it again"
        )
    if ref.catalog_id:
        entries = [catalog.get_session(ref.catalog_id, include_paths=True)]
    else:
        entries = []
        offset = 0
        while True:
            page = catalog.list_sessions(
                root_id=root.id,
                query=ref.native_id,
                include_paths=True,
                include_missing=True,
                limit=10_000,
                offset=offset,
            )
            entries.extend(entry for entry in page if entry.session_id == ref.native_id)
            if len(page) < 10_000:
                break
            offset += len(page)
    matches = [
        entry
        for entry in entries
        if entry.session_id == ref.native_id
        and entry.format == ref.format.value
        and Path(entry.root or "") == profile.root
    ]
    if len(matches) != 1:
        raise SessionMigrateError(
            "session reference is absent, ambiguous, or belongs to another root"
        )
    return matches[0]


def resolve_source(catalog: Catalog, profile: Profile, ref: SessionRef) -> CatalogTransferSource:
    entry = _resolve_entry(catalog, profile, ref)
    source = catalog.session_source_for_transfer(entry.catalog_id)
    if source.path is not None:
        try:
            source.path.resolve(strict=True).relative_to(profile.root.resolve(strict=True))
        except (OSError, ValueError, RuntimeError) as exc:
            raise SessionMigrateError(
                "selected session source is missing or outside its profile root"
            ) from exc
    return source


def source_read_paths(
    source_format: AgentFormat | None,
    path: Path,
    root: Path,
    *,
    max_entries: int = 100_000,
) -> tuple[Path, ...]:
    """Enumerate only authoritative selected-session files, with bounded directory work.

    Directory paths are membership/identity witnesses, never a byte estimate.
    Native parsing and filename/sequence validation remain in the adapters.
    """
    root = root.resolve(strict=True)
    boundary = root.parent if root.is_file() else root

    def validate(candidate: Path) -> None:
        if ".." in candidate.parts:
            raise SessionMigrateError("Source component cannot contain parent traversal.")
        try:
            candidate.relative_to(boundary)
            for ancestor in (candidate, *candidate.parents):
                if ancestor.is_symlink():
                    raise SessionMigrateError("Source files/directories cannot be symlinks.")
                if ancestor == boundary:
                    break
            candidate.resolve(strict=False).relative_to(boundary)
        except (ValueError, RuntimeError, OSError) as exc:
            raise SessionMigrateError(
                "Source component is outside the selected profile root."
            ) from exc

    paths: list[Path]
    validate(path)
    if source_format == AgentFormat.KIMI:
        from session_migrate.formats import kimi

        directory, wire, state = kimi._source_paths(path)
        paths = [directory, directory / "agents", directory / "agents/main", wire, state]
    elif source_format == AgentFormat.VIBE:
        from session_migrate.formats import vibe

        directory, messages, meta = vibe._source_paths(path)
        paths = [directory, messages, meta]
    elif source_format == AgentFormat.GROK:
        directory = path if path.is_dir() else path.parent
        paths = [directory, directory / "summary.json", directory / "updates.jsonl"]
    elif source_format == AgentFormat.OPENHANDS:
        if path.name == "events" and path.is_dir():
            conversation, events = path.parent, path
        elif path.is_dir():
            conversation, events = path, path / "events"
        else:
            events, conversation = path.parent, path.parent.parent
        validate(conversation)
        validate(events)
        paths = [conversation, events, conversation / "base_state.json"]
        try:
            with os.scandir(events) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > max_entries:
                        raise SourceReadLimitError(
                            "Source directory exceeds the file-count budget."
                        )
                    if entry.name.startswith("event-") and entry.name.endswith(".json"):
                        paths.append(Path(entry.path))
        except OSError as exc:
            raise SessionMigrateError("Source event directory is unavailable.") from exc
    else:
        paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
    for candidate in paths:
        validate(candidate)
        if candidate.exists() and not (candidate.is_file() or candidate.is_dir()):
            raise SessionMigrateError("Source component is not a regular file/directory.")
    return tuple(sorted(set(paths)))


def _path_revision(paths: tuple[Path, ...]) -> tuple:
    values = []
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            values.append((path, None))
            continue
        values.append(
            (
                path,
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                stat.S_IFMT(info.st_mode),
            )
        )
    return tuple(values)


def _open_source_file(path: Path) -> int:
    """Open every path component without following symlinks, including parents."""
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def load_bounded_source(
    path: Path,
    source_format: AgentFormat | None,
    *,
    root: Path,
    max_source_bytes: int,
    environ: Mapping[str, str] | None = None,
) -> Session:
    """Parse a bounded immutable private copy; retain original path/hash/provenance."""
    if (
        isinstance(max_source_bytes, bool)
        or not isinstance(max_source_bytes, int)
        or max_source_bytes < 1
    ):
        raise SessionMigrateError("Source byte budget must be a positive integer.")
    root = root.resolve(strict=True)
    if root.is_file():
        root = root.parent
    paths = source_read_paths(source_format, path, root)
    before = _path_revision(paths)
    temp_root = _temporary_root(os.environ if environ is None else environ)
    with tempfile.TemporaryDirectory(prefix="session-migrate-bounded-", dir=temp_root) as folder:
        snapshot_root = Path(folder)
        snapshot_root.chmod(0o700)
        total = 0
        for original in paths:
            if not original.exists():
                continue
            target = snapshot_root / original.relative_to(root)
            try:
                target.resolve(strict=False).relative_to(snapshot_root)
            except (ValueError, RuntimeError, OSError) as exc:
                raise SessionMigrateError(
                    "Snapshot target is outside its private directory."
                ) from exc
            if original.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = _open_source_file(original)
            with os.fdopen(descriptor, "rb") as reader:
                info = os.fstat(reader.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_source_bytes - total:
                    raise SourceReadLimitError("Source exceeds the interactive read budget.")
                output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(output, "wb") as writer:
                    while chunk := reader.read(min(64 * 1024, max_source_bytes - total + 1)):
                        total += len(chunk)
                        if total > max_source_bytes:
                            raise SourceReadLimitError(
                                "Source grew beyond the interactive read budget."
                            )
                        writer.write(chunk)
        after_paths = source_read_paths(source_format, path, root)
        if paths != after_paths or before != _path_revision(after_paths):
            raise SessionMigrateError("Source changed during bounded snapshot.")
        selected = snapshot_root / path.relative_to(root)
        try:
            selected.resolve(strict=False).relative_to(snapshot_root)
        except (ValueError, RuntimeError, OSError) as exc:
            raise SessionMigrateError(
                "Snapshot selection is outside its private directory."
            ) from exc
        if source_format == AgentFormat.KIMI:
            from session_migrate.formats import kimi

            session = kimi.parse_session(selected, original_session_dir=kimi._source_paths(path)[0])
        else:
            session = load_session(selected, source_format)
        if before != _path_revision(source_read_paths(source_format, path, root)):
            raise SessionMigrateError("Source changed during bounded parsing.")
        try:
            original_path = root / session.source_path.relative_to(snapshot_root)
        except ValueError:
            original_path = path
        return replace(session, source_path=original_path)


def read_session(
    catalog: Catalog,
    profile: Profile,
    ref: SessionRef,
    *,
    base_environ: Mapping[str, str] | None = None,
    max_source_bytes: int | None = None,
) -> Session:
    """Load authoritative native content only for the selected identity."""
    source = resolve_source(catalog, profile, ref)
    if max_source_bytes is not None:
        if (
            isinstance(max_source_bytes, bool)
            or not isinstance(max_source_bytes, int)
            or max_source_bytes < 1
        ):
            raise SessionMigrateError("Source byte budget must be a positive integer.")
        if source.path is not None:
            paths = source_read_paths(profile.format, source.path, profile.root)
            total = sum(candidate.stat().st_size for candidate in paths if candidate.is_file())
            if total > max_source_bytes:
                raise SourceReadLimitError("Source exceeds the interactive read budget.")
        elif profile.format not in _VIRTUAL_CLI:
            # Shared databases lack a selected-export budget API. This conservative stat
            # preflight is not an atomic cap; their existing adapter safety limits remain.
            names = {
                AgentFormat.HERMES: "state.db",
                AgentFormat.MASTRACODE: "mastra.db",
                AgentFormat.DEVIN: "sessions.db",
            }
            database = (
                profile.root if profile.root.is_file() else profile.root / names[profile.format]
            )
            paths = source_read_paths(profile.format, database, profile.root)
            if (
                sum(candidate.stat().st_size for candidate in paths if candidate.is_file())
                > max_source_bytes
            ):
                raise SourceReadLimitError(
                    "Source exceeds the conservative shared-store preflight budget."
                )
    values = _source_environment(profile, base_environ)
    if profile.format in _VIRTUAL_CLI:
        loader = (
            load_opencode_session if profile.format == AgentFormat.OPENCODE else load_kilo_session
        )
        options = {"max_source_bytes": max_source_bytes} if max_source_bytes is not None else {}
        session = loader(
            ref.native_id, source_cli=_cli(profile, source=True), environ=values, **options
        )
    elif profile.format in _SHARED:
        session = _load_shared_database_source(profile.format, ref.native_id, profile.root)
    else:
        assert source.path is not None
        session = (
            load_session(source.path, profile.format)
            if max_source_bytes is None
            else load_bounded_source(
                source.path,
                profile.format,
                root=profile.root,
                max_source_bytes=max_source_bytes,
                environ=values,
            )
        )
    if (
        session.source_format != ref.format
        or not session.session_id
        or (
            normalized_source_id(ref.format, session.session_id)
            != normalized_source_id(ref.format, ref.native_id)
        )
    ):
        raise SessionMigrateError(
            "authoritative session identity does not match its profile reference"
        )
    return session


def run_profile_operation(
    catalog: Catalog,
    source_profile: Profile,
    ref: SessionRef,
    target_profile: Profile,
    request: OperationRequest,
    *,
    base_environ: Mapping[str, str] | None = None,
    progress: ProgressCallback | None = None,
    cancellation: CancellationToken | None = None,
    prepublication_check: Callable[[], None] | None = None,
    max_source_bytes: int | None = None,
) -> OperationResult | TransferReceipt:
    """Bind a transfer to two explicit profiles; adapters re-read native content."""
    environment = dict(os.environ if base_environ is None else base_environ)
    state_home = default_migration_state_home(environ=environment)
    bound, target_values, source_values, guard, context = _prepared_profile_binding(
        catalog, source_profile, ref, target_profile, request, environment, prepublication_check
    )
    return run_operation(
        bound,
        environ=target_values,
        source_environ=source_values,
        progress=progress,
        cancellation=cancellation,
        prepublication_check=guard,
        max_source_bytes=max_source_bytes,
        migration_state_home=state_home,
        profile_context=context,
    )


def _bind_profile_operation(
    catalog: Catalog,
    source_profile: Profile,
    ref: SessionRef,
    target_profile: Profile,
    request: OperationRequest,
    base_environ: Mapping[str, str] | None,
) -> tuple[OperationRequest, dict[str, str], dict[str, str]]:
    if request.command != OperationKind.TRANSFER:
        raise SessionMigrateError("profile operation currently supports transfer only")
    if any(
        getattr(request, key) is not None
        for key in (
            "path",
            "source_id",
            "catalog",
            "catalog_id",
            "title",
            "source_home",
            "source_cwd",
            "source_cli",
            "home",
            "target_cli",
        )
    ):
        raise SessionMigrateError(
            "profile operation selectors must come from the selected profiles/ref"
        )
    if request.source_format is not None and request.source_format != source_profile.format:
        raise SessionMigrateError("request source format does not match the selected profile")
    if (
        request.target_format is not None
        and request.target_format.value != target_profile.format.value
    ):
        raise SessionMigrateError("request target format does not match the selected profile")
    entry = _resolve_entry(catalog, source_profile, ref)
    source = resolve_source(catalog, source_profile, ref)
    source_values = _source_environment(source_profile, base_environ)
    target_values = target_profile.environment(base_environ)
    home = target_profile.root
    if home.is_file() and not (
        target_profile.format == AgentFormat.MASTRACODE and home.name == "mastra.db"
    ):
        raise SessionMigrateError(
            "target profile file root requires the canonical MastraCode mastra.db filename"
        )
    if target_profile.format in _VIRTUAL_CLI:
        selected = target_profile.root
        if selected.name != target_profile.format.value:
            raise SessionMigrateError(
                "target profile root cannot be mapped to its native XDG data store"
            )
        if "XDG_DATA_HOME" in target_profile.environ and (
            Path(target_profile.environ["XDG_DATA_HOME"]) / target_profile.format.value != selected
        ):
            raise SessionMigrateError(
                "target profile XDG_DATA_HOME does not match its native store root"
            )
        if selected.exists():
            # Preserve the F2A directory/DB identity guard for populated stores.
            database = selected / f"{target_profile.format.value}.db"
            if database.exists() or database.is_symlink():
                _catalog_virtual_source_environment(target_profile.format, selected, target_values)
            elif selected.is_symlink() or not selected.is_dir():
                raise SessionMigrateError("target profile root must be a native store directory")
        target_values["XDG_DATA_HOME"] = str(selected.parent)
        home = None
    bound = replace(
        request,
        source_format=source.format,
        target_format=TargetFormat(target_profile.format),
        catalog=catalog.path,
        catalog_id=entry.catalog_id,
        source_cli=_cli(source_profile, source=True),
        target_cli=_cli(target_profile, source=False),
        home=home,
    )
    return bound, target_values, source_values


def prepare_profile_operation(
    catalog: Catalog,
    source_profile: Profile,
    ref: SessionRef,
    target_profile: Profile,
    request: OperationRequest,
    *,
    base_environ: Mapping[str, str] | None = None,
    progress: ProgressCallback | None = None,
    cancellation: CancellationToken | None = None,
    prepublication_check: Callable[[], None] | None = None,
    max_source_bytes: int | None = None,
    catalog_opener: Callable[[Path], Catalog] | None = None,
    migration_state_home: Path | None = None,
) -> PreparedOperation:
    """Prepare one exact profile pair; every guard opens its own worker connection."""
    environment = dict(os.environ if base_environ is None else base_environ)
    state_home = migration_state_home or default_migration_state_home(environ=environment)
    bound, target_values, source_values, guard, context = _prepared_profile_binding(
        catalog,
        source_profile,
        ref,
        target_profile,
        request,
        environment,
        prepublication_check,
        catalog_opener=catalog_opener,
    )
    return prepare_operation(
        bound,
        environ=target_values,
        source_environ=source_values,
        progress=progress,
        cancellation=cancellation,
        prepublication_check=guard,
        max_source_bytes=max_source_bytes,
        migration_state_home=state_home,
        profile_context=context,
        catalog_opener=catalog_opener,
    )


def _prepared_profile_binding(
    catalog,
    source_profile,
    ref,
    target_profile,
    request,
    environment,
    prepublication_check,
    *,
    catalog_opener=None,
    readonly_catalog=False,
):
    bound, target_values, source_values = _bind_profile_operation(
        catalog, source_profile, ref, target_profile, request, environment
    )
    catalog_path = catalog.path
    expected_source, expected_target = source_profile.fingerprint, target_profile.fingerprint

    def guard() -> None:
        if (
            source_profile.fingerprint != expected_source
            or target_profile.fingerprint != expected_target
        ):
            raise SessionMigrateError("prepared profile options changed; prepare again")
        opener = Catalog.open_readonly_existing if readonly_catalog else (catalog_opener or Catalog)
        with opener(catalog_path) as current:
            rebound, target_current, source_current = _bind_profile_operation(
                current, source_profile, ref, target_profile, request, environment
            )
        if rebound != bound or target_current != target_values or source_current != source_values:
            raise SessionMigrateError("prepared profile binding changed; prepare again")
        if prepublication_check is not None:
            prepublication_check()

    return (
        bound,
        target_values,
        source_values,
        guard,
        {
            "source_profile_id": source_profile.id,
            "source_profile_name": source_profile.name,
            "source_profile_fingerprint": expected_source,
            "target_profile_id": target_profile.id,
            "target_profile_name": target_profile.name,
            "target_profile_fingerprint": expected_target,
        },
    )


def local_alias_for_session(
    catalog: Catalog,
    profile: Profile,
    ref: SessionRef,
    *,
    base_environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return a separate label for an exact validated ref; never replace catalog title."""
    from session_migrate.conversion import default_migration_state_home
    from session_migrate.titles import AliasIdentity, TitleAliasStore

    resolve_source(catalog, profile, ref)
    identity = AliasIdentity(profile.format, profile.root, ref.native_id)
    store = TitleAliasStore(
        default_migration_state_home(environ=profile.environment(base_environ)) / "title-aliases/v1"
    )
    if not os.path.lexists(store.path_for(identity)):
        store.preflight(identity)
        return None
    return store.load(identity).alias


class ProfileStore:
    """Private versioned JSON snapshots; explicit exclusive creation, no overwrite."""

    def __init__(self, path: Path):
        self.path = _path(path, "storage path")

    def _directory(self, *, create: bool) -> int:
        if os.name != "posix":
            raise SessionMigrateError(
                "private profile storage requires POSIX ownership and no-follow support"
            )
        parent = self.path.parent
        fd = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in parent.parts[1:]:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = next_fd
        except OSError as exc:
            os.close(fd)
            raise SessionMigrateError(
                "profile storage directory is unavailable or a symlink"
            ) from exc
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise SessionMigrateError(
                "profile storage directory must be owned by this user with mode 0700"
            )
        return fd

    def create(self, profiles: Sequence[Profile]) -> None:
        ids = [profile.id for profile in profiles]
        names = [profile.name.casefold() for profile in profiles]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise SessionMigrateError("duplicate profile ID or name")
        data = (
            json.dumps(
                {
                    "schema_version": PROFILE_SCHEMA_VERSION,
                    "profiles": [profile.to_dict() for profile in profiles],
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            + b"\n"
        )
        if len(data) > _MAX_PROFILE_BYTES:
            raise SessionMigrateError("profile storage exceeds its size limit")
        directory = self._directory(create=True)
        try:
            try:
                fd = os.open(
                    self.path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
            except OSError as exc:
                raise SessionMigrateError(
                    "profile storage already exists or cannot be created; no overwrite"
                ) from exc
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)

    def load(self) -> tuple[Profile, ...]:
        directory = self._directory(create=False)
        try:
            try:
                fd = os.open(
                    self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            except OSError as exc:
                raise SessionMigrateError(
                    "profile storage is missing, unreadable, or a symlink"
                ) from exc
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                ):
                    raise SessionMigrateError(
                        "profile storage must be a private owned regular file with mode 0600"
                    )
                data = handle.read(_MAX_PROFILE_BYTES + 1)
        finally:
            os.close(directory)
        if len(data) > _MAX_PROFILE_BYTES:
            raise SessionMigrateError("profile storage exceeds its size limit")
        try:
            document = json.loads(data, object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeError) as exc:
            raise SessionMigrateError("profile storage contains invalid JSON") from exc
        if not isinstance(document, dict) or set(document) != {"schema_version", "profiles"}:
            raise SessionMigrateError("profile storage schema is invalid")
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != PROFILE_SCHEMA_VERSION
        ):
            raise SessionMigrateError("profile storage schema version is unsupported")
        rows = document["profiles"]
        if not isinstance(rows, list):
            raise SessionMigrateError("profile storage profiles must be a list")
        profiles = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "id",
                "name",
                "format",
                "root",
                "cli",
                "environ",
            }:
                raise SessionMigrateError("profile storage row schema is invalid")
            profiles.append(Profile(**row))
        if len({profile.id for profile in profiles}) != len(profiles) or len(
            {profile.name.casefold() for profile in profiles}
        ) != len(profiles):
            raise SessionMigrateError("duplicate profile ID or name")
        return tuple(profiles)

    def get(self, profile_id: str) -> Profile:
        for profile in self.load():
            if profile.id == profile_id:
                return profile
        raise SessionMigrateError("profile ID was not found")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
