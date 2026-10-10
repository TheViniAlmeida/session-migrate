"""Typed orchestration shared by command-line and interactive clients."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from session_migrate import __version__
from session_migrate.capabilities import capabilities_for, title_capability_for
from session_migrate.catalog import Catalog, default_catalog_path
from session_migrate.conversion import (
    KILO_HOME_UNSUPPORTED,
    OPENCODE_HOME_UNSUPPORTED,
    ConversionArtifact,
    ConversionOptions,
    content_free_result,
    convert_session,
    default_migration_state_home,
    default_target_home,
    detect_opencode_version,
    ensure_target_paths_available,
    environment_home,
    expand_environment_path,
    install_antigravity_artifact,
    install_copilot_artifact,
    install_cursor_artifact,
    install_devin_artifact,
    install_grok_artifact,
    install_hermes_artifact,
    install_kilo_artifact,
    install_kimi_artifact,
    install_mastracode_artifact,
    install_opencode_artifact,
    install_openhands_artifact,
    install_vibe_artifact,
    kilo_manifest_path,
    load_devin_session,
    load_hermes_session,
    load_kilo_session,
    load_mastracode_session,
    load_opencode_session,
    load_session,
    opencode_manifest_path,
    shared_database_manifest_path,
    target_import_paths,
    write_artifact,
)
from session_migrate.discovery import locate_session, normalized_source_id
from session_migrate.errors import (
    NativeImportPartialError,
    OperationCancelledError,
    PublishedArtifactPartialError,
    SessionMigrateError,
    SourceReadLimitError,
)
from session_migrate.formats import opencode_v2
from session_migrate.model import AgentFormat, Session, TargetFormat
from session_migrate.titles import (
    AliasIdentity,
    TitleAliasStore,
    TitlePolicy,
    TitleReceipt,
    TitleReceiptBinding,
    preflight_title_receipt,
    title_receipt_path,
    validate_title,
    write_title_receipt,
)

_SHARED_DATABASE_SOURCES = {
    AgentFormat.HERMES,
    AgentFormat.MASTRACODE,
    AgentFormat.DEVIN,
}
_SHARED_DATABASE_TARGETS = {
    TargetFormat.HERMES,
    TargetFormat.MASTRACODE,
    TargetFormat.DEVIN,
}


class OperationKind(StrEnum):
    CONVERT = "convert"
    IMPORT = "import"
    TRANSFER = "transfer"


class ProgressStage(StrEnum):
    LOADING = "loading"
    CONVERTING = "converting"
    PREFLIGHT = "preflight"
    INSTALLING = "installing"
    COMPLETED = "completed"
    NEEDS_RECONCILE = "needs_reconcile"
    FAILED = "failed"
    CANCELLED = "cancelled"
    VERIFYING = "verifying"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: ProgressStage


ProgressCallback = Callable[[ProgressEvent], None]


class CancellationToken:
    """One operation's atomic cancel/publication boundary; observers cannot cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = self._publishing = self._claimed = self._closed = False

    def request_cancel(self) -> bool:
        with self._lock:
            if self._publishing or self._closed:
                return False
            self._cancelled = True
            return True

    @property
    def publishing(self) -> bool:
        with self._lock:
            return self._publishing

    def claim(self) -> None:
        with self._lock:
            if self._claimed or self._closed:
                raise SessionMigrateError("cancellation token already belongs to an operation")
            self._claimed = True

    def check(self) -> None:
        with self._lock:
            if self._cancelled:
                raise OperationCancelledError("Operation cancelled before publication.")

    def begin_publication(self) -> None:
        with self._lock:
            if self._cancelled:
                raise OperationCancelledError("Operation cancelled before publication.")
            if self._publishing or self._closed:
                raise SessionMigrateError("operation publication already consumed")
            self._publishing = True

    def close(self) -> None:
        with self._lock:
            self._closed = True


@dataclass(frozen=True, slots=True, repr=False)
class OperationRequest:
    """Normalized selectors and options without CLI or persisted profile state."""

    command: OperationKind
    path: Path | None = None
    source_format: AgentFormat | None = None
    target_format: TargetFormat | None = None
    output: Path | None = None
    home: Path | None = None
    source_home: Path | None = None
    source_cwd: Path | None = None
    source_cli: Path | None = None
    source_id: str | None = None
    catalog: Path | None = None
    catalog_id: str | None = None
    title: str | None = None
    session_id: str | None = None
    cwd: Path | None = None
    target_cli: Path | None = None
    target_cli_version: str | None = None
    model_provider: str | None = None
    model: str | None = None
    dry_run: bool = False
    target_title: str | None = None
    title_policy: TitlePolicy | None = None
    require_native_title: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", OperationKind(self.command))
        if self.source_format is not None:
            object.__setattr__(self, "source_format", AgentFormat(self.source_format))
        if self.target_format is not None:
            object.__setattr__(self, "target_format", TargetFormat(self.target_format))
        if self.target_title is not None:
            validate_title(self.target_title)
        if self.title_policy is not None:
            try:
                object.__setattr__(self, "title_policy", TitlePolicy(self.title_policy))
            except (TypeError, ValueError) as exc:
                raise SessionMigrateError("title policy is unsupported") from exc
            if self.target_title is None:
                raise SessionMigrateError("title policy requires an explicit target title")
        if type(self.require_native_title) is not bool:
            raise SessionMigrateError("native title requirement must be boolean")


@dataclass(frozen=True, slots=True, repr=False)
class OperationResult:
    """CLI-compatible metadata; no retained session or serialized conversation."""

    dry_run: bool
    source_format: str
    target_format: str
    session_id: str
    cwd: str
    output: str
    manifest: str
    records: int
    sha256: str
    dropped_events: dict[str, int]
    warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:

        return {field.name: deepcopy(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True, repr=False)
class TransferProposal:
    """Immutable metadata only; JSON projections are independent deep copies."""

    proposal_id: str
    _document: str
    schema_version: int = field(default=1, init=False)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._document)


@dataclass(frozen=True, slots=True, repr=False, init=False)
class TransferReceipt:
    proposal: TransferProposal
    status: str
    _legacy_json: str
    title: TitleReceipt | None
    title_receipt: str | None
    _identity_json: str

    def __init__(
        self,
        proposal: TransferProposal,
        status: str,
        legacy_result: Mapping[str, Any],
        *,
        title: TitleReceipt | None = None,
        title_receipt: Path | None = None,
        target_identity: Mapping[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "proposal", proposal)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "_legacy_json", _canonical_json(dict(legacy_result)))
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "title_receipt", str(title_receipt) if title_receipt else None)
        object.__setattr__(self, "_identity_json", _canonical_json(target_identity))

    @property
    def legacy_result(self) -> dict[str, Any]:
        return json.loads(self._legacy_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal.proposal_id,
            "status": self.status,
            "legacy_result": self.legacy_result,
            "title": self.title.to_dict() if self.title is not None else None,
            "title_receipt": self.title_receipt,
            "target_identity": json.loads(self._identity_json),
            "effects": self.proposal.to_dict()["effects"],
        }


class PreparedOperation:
    """Private, single-use artifact handle; no global registry or serialized replay."""

    __slots__ = ("_lock", "_state", "_proposal", "_consumed")

    def __init__(self, state: _PreparedState, proposal: TransferProposal):
        self._lock = threading.Lock()
        self._state = state
        self._proposal = proposal
        self._consumed = False

    def discard(self) -> None:
        with self._lock:
            self._consumed = True
            self._state = None

    def _consume(self) -> tuple[_PreparedState, TransferProposal]:
        with self._lock:
            if self._consumed or self._state is None:
                raise SessionMigrateError("prepared operation already consumed; prepare again")
            self._consumed = True
            state, self._state = self._state, None
            return state, self._proposal


def proposal_metadata(prepared: PreparedOperation) -> TransferProposal:
    if not isinstance(prepared, PreparedOperation):
        raise SessionMigrateError("a private prepared operation handle is required")
    return prepared._proposal


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


_PUBLIC_ENVIRONMENT = (
    "HOME",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
    "OPENCODE_BIN",
    "KILO_BIN",
)
_PROFILE_CONTEXT = {
    "source_profile_id",
    "source_profile_name",
    "source_profile_fingerprint",
    "target_profile_id",
    "target_profile_name",
    "target_profile_fingerprint",
}


def _public_options(
    request: OperationRequest,
    target: Mapping[str, str],
    source: Mapping[str, str],
    profiles: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "request": {
            item.name: str(value) if isinstance(value, Path) else value
            for item in fields(request)
            if item.name != "dry_run" and (value := getattr(request, item.name)) is not None
        },
        "target_environment": {key: target[key] for key in _PUBLIC_ENVIRONMENT if key in target},
        "source_environment": {key: source[key] for key in _PUBLIC_ENVIRONMENT if key in source},
        "profiles": dict(profiles),
    }


def _validate_title_request(request: OperationRequest) -> None:
    if request.require_native_title:
        if request.command == OperationKind.CONVERT:
            raise SessionMigrateError(
                "convert-only cannot require installed native title persistence"
            )
        if request.target_title is None or request.title_policy == TitlePolicy.LOCAL_ALIAS:
            raise SessionMigrateError(
                "native title requirement needs an explicit native target title"
            )
    if request.target_title is not None and request.title_policy != TitlePolicy.LOCAL_ALIAS:
        if request.target_format is not None and request.target_format != TargetFormat.OPENCODE:
            raise SessionMigrateError(
                "native target title is unsupported; choose local_alias explicitly"
            )
        if request.target_cli_version is not None:
            capability = title_capability_for(
                request.target_format or TargetFormat.OPENCODE,
                cli_version=request.target_cli_version,
            )
            if capability.status != "native_verified":
                raise SessionMigrateError("native target title has no exact-client proof")


def _root_epoch(path: Path) -> tuple:
    if any(ancestor.is_symlink() for ancestor in (path, *path.parents)):
        raise SessionMigrateError("prepared root is a symlink")
    candidate = path
    while not candidate.exists():
        if candidate.is_symlink():
            raise SessionMigrateError("prepared root is a symlink")
        candidate = candidate.parent
    if path.is_symlink():
        raise SessionMigrateError("prepared root is a symlink")
    info = candidate.stat()
    return (
        str(path),
        str(candidate),
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        stat.S_IMODE(info.st_mode),
        info.st_uid,
    )


def _target_epoch_matches(path: Path, expected: tuple, *, database_file: bool = False) -> bool:
    current = _root_epoch(path)
    if current == expected:
        return True
    if expected[0] == expected[1]:
        return False
    # An absent destination has no prior root inode. An unrelated native session
    # may safely create it, provided the existing ancestor was not replaced and
    # the exact target ID still passes the installer's collision preflight.
    shape = path.is_dir() or (database_file and path.name == "mastra.db" and path.is_file())
    return _root_epoch(Path(expected[1]))[1:] == expected[1:] and shape


def _target_root(built: _OperationBuild, environ: Mapping[str, str]) -> Path | None:
    if built.request.command == OperationKind.CONVERT:
        return None
    if built.artifact.target_format in {TargetFormat.OPENCODE, TargetFormat.KILO}:
        data = environ.get("XDG_DATA_HOME")
        parent = Path(data) if data else environment_home(environ) / ".local/share"
        return (parent / built.artifact.target_format.value).absolute()
    return built.home.absolute()


def _source_root(built: _OperationBuild, environ: Mapping[str, str]) -> Path:
    if built.source_root is not None:
        return built.source_root.resolve()
    if built.artifact.source.source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
        data = environ.get("XDG_DATA_HOME")
        parent = Path(data) if data else environment_home(environ) / ".local/share"
        return (parent / built.artifact.source.source_format.value).resolve()
    return _physical_source_root(
        built.artifact.source.source_path, built.artifact.source.source_format
    )


def _physical_source_root(path: Path, format: AgentFormat | None) -> Path:
    path = path.resolve()
    if format == AgentFormat.KIMI:
        from session_migrate.formats import kimi

        return kimi._source_paths(path)[0]
    if format == AgentFormat.VIBE:
        from session_migrate.formats import vibe

        return vibe._source_paths(path)[0]
    if format == AgentFormat.GROK:
        return path if path.is_dir() else path.parent
    if format == AgentFormat.OPENHANDS:
        return (
            (path.parent if path.name == "events" else path)
            if path.is_dir()
            else path.parent.parent
        )
    return path.parent


def _source_witness(session: Session, root: Path) -> str:
    # Selected native exports may change their own WAL/SHM. Their export digest,
    # not global DB activity, is the selected conversation's authority witness.
    layout = None
    if session.source_format not in _SHARED_DATABASE_SOURCES | {
        AgentFormat.OPENCODE,
        AgentFormat.KILO,
    }:
        from session_migrate.profiles import _path_revision, source_read_paths

        layout = str(
            _path_revision(source_read_paths(session.source_format, session.source_path, root))
        )
    return _digest(
        {
            "format": session.source_format.value,
            "root": str(root),
            "root_epoch": _root_epoch(root),
            "path": str(session.source_path),
            "native_id": session.session_id,
            "sha256": session.source_sha256,
            "cli_version": session.cli_version,
            "started_at": session.started_at,
            "cwd": str(session.cwd) if session.cwd is not None else None,
            "counts": session.event_counts(),
            "records": session.raw_record_count,
            "layout": layout,
        }
    )


def _client_revision(path: Path | None) -> tuple | None:
    if path is None:
        return None
    try:
        link = path.lstat()
        selected = path.resolve(strict=True)
        info = selected.stat()
        if not selected.is_file() or info.st_size > 256 * 1024 * 1024:
            raise SessionMigrateError("prepared client is not a bounded regular file")
        digest = hashlib.sha256()
        descriptor = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise SessionMigrateError("prepared client changed during verification")
            remaining = info.st_size
            while remaining and (block := stream.read(min(64 * 1024, remaining))):
                digest.update(block)
                remaining -= len(block)
            after = os.fstat(stream.fileno())
            named = selected.lstat()
            if (
                remaining
                or stream.read(1)
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise SessionMigrateError("prepared client changed during verification")
        return (
            str(path),
            str(selected),
            link.st_dev,
            link.st_ino,
            link.st_mtime_ns,
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            digest.hexdigest(),
        )
    except OSError as exc:
        raise SessionMigrateError("prepared client is unavailable") from exc


def _pin_clients(
    built: _OperationBuild,
    target: Mapping[str, str],
    source: Mapping[str, str],
) -> _OperationBuild:
    from session_migrate import conversion

    request = built.request
    target_cli, source_cli = request.target_cli, request.source_cli
    if request.command != OperationKind.CONVERT:
        if built.artifact.target_format == TargetFormat.OPENCODE:
            target_cli = conversion._resolve_opencode_cli(target_cli, target)
        elif built.artifact.target_format == TargetFormat.KILO:
            target_cli = conversion._resolve_kilo_cli(target_cli, target)
    if request.command == OperationKind.TRANSFER:
        if built.artifact.source.source_format == AgentFormat.OPENCODE:
            source_cli = conversion._resolve_opencode_cli(source_cli, source)
        elif built.artifact.source.source_format == AgentFormat.KILO:
            source_cli = conversion._resolve_kilo_cli(source_cli, source)
    home = built.home.resolve() if built.home is not None else None
    return replace(
        built,
        home=home,
        request=replace(
            request,
            target_cli=target_cli,
            source_cli=source_cli,
            home=home,
            source_format=built.artifact.source.source_format,
            target_format=built.artifact.target_format,
            cwd=built.artifact.cwd,
            target_cli_version=built.artifact.target_cli_version,
        ),
    )


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedState:
    built: _OperationBuild
    target_environment: Mapping[str, str]
    source_environment: Mapping[str, str]
    source_root: Path
    source_witness: str
    source_epoch: tuple
    target_root: Path | None
    target_epoch: tuple
    cwd_epoch: tuple
    client_revisions: tuple
    options_fingerprint: str
    profiles: Mapping[str, str]
    artifact_sha256: str
    artifact_fingerprint: str
    check: Callable[[], None] | None
    max_source_bytes: int | None


def _title_preflight(state: _PreparedState) -> tuple[TitleAliasStore | None, AliasIdentity | None]:
    built = state.built
    plan = built.artifact.title_plan
    if plan is None:
        return None, None
    preflight_title_receipt(title_receipt_path(built.manifest))
    if built.request.command == OperationKind.CONVERT:
        return None, None
    identity = AliasIdentity.planned(
        built.artifact.target_format, state.target_root, built.artifact.session_id
    )
    if plan.policy != TitlePolicy.LOCAL_ALIAS:
        return None, identity
    store = TitleAliasStore(
        default_migration_state_home(environ=state.target_environment) / "title-aliases/v1"
    )
    store.preflight(identity)
    return store, identity


def _read_prepared_source(state: _PreparedState) -> None:
    if state.check is not None:
        state.check()
    if _root_epoch(state.source_root) != state.source_epoch:
        raise SessionMigrateError("prepared source root changed; prepare again")
    session, _target, root = _resolve_source(
        state.built.request,
        state.source_environment,
        state.max_source_bytes,
        expected_root=state.source_root,
    )
    if root is not None and root.resolve() != state.source_root:
        raise SessionMigrateError("prepared source root changed; prepare again")
    if _source_witness(session, state.source_root) != state.source_witness:
        raise SessionMigrateError("prepared source changed; prepare again")


def _artifact_fingerprint(built: _OperationBuild) -> str:
    artifact, source = built.artifact, built.artifact.source
    return _digest(
        {
            "source": {
                "format": source.source_format.value,
                "path": str(source.source_path),
                "sha256": source.source_sha256,
                "session_id": source.session_id,
                "cli_version": source.cli_version,
                "cwd": str(source.cwd),
                "started_at": source.started_at,
                "title": source.title,
                "model": source.model,
                "model_provider": source.model_provider,
                "records": source.raw_record_count,
                "events": source.event_counts(),
            },
            "target": {
                "format": artifact.target_format.value,
                "session_id": artifact.session_id,
                "cli_version": artifact.target_cli_version,
                "cwd": str(artifact.cwd),
                "timestamp": artifact.timestamp,
                "records": artifact.native_record_count,
                "sha256": artifact.target_sha256,
                "dropped": artifact.dropped,
                "warnings": artifact.warnings,
                "title_plan": artifact.title_plan.to_dict() if artifact.title_plan else None,
            },
            "paths": {
                "output": str(built.output),
                "manifest": str(built.manifest),
                "home": str(built.home),
                "source_root": str(built.source_root),
            },
        }
    )


def _client_epoch(path: Path | None) -> tuple | None:
    if path is None:
        return None
    try:
        link = path.lstat()
        selected = path.resolve(strict=True)
        info = selected.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SessionMigrateError("prepared client identity changed; prepare again")
        return (
            str(path),
            str(selected),
            link.st_dev,
            link.st_ino,
            link.st_mtime_ns,
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        )
    except OSError as exc:
        raise SessionMigrateError("prepared client is unavailable") from exc


def _verify_prepared_options(state: _PreparedState) -> None:
    if not all(
        isinstance(values, MappingProxyType)
        for values in (
            state.target_environment,
            state.source_environment,
            state.profiles,
        )
    ):
        raise SessionMigrateError("prepared runtime mappings must remain immutable")
    current = (
        _client_revision(state.built.request.source_cli),
        _client_revision(state.built.request.target_cli),
    )
    if current != state.client_revisions:
        raise SessionMigrateError("prepared client identity changed; prepare again")
    if (
        state.built.artifact.target_sha256 != state.artifact_sha256
        or _artifact_fingerprint(state.built) != state.artifact_fingerprint
        or _digest(
            _public_options(
                state.built.request,
                state.target_environment,
                state.source_environment,
                state.profiles,
            )
        )
        != state.options_fingerprint
    ):
        raise SessionMigrateError("prepared options or artifact changed; prepare again")
    if (
        _client_epoch(state.built.request.source_cli),
        _client_epoch(state.built.request.target_cli),
    ) != tuple(
        revision[:-1] if revision is not None else None for revision in state.client_revisions
    ):
        raise SessionMigrateError("prepared client identity changed; prepare again")
    # Namespace checks are last: hashing binaries/artifacts and authoritative
    # exports can be slow. Never publish using an epoch checked before that I/O.
    if _root_epoch(state.source_root) != state.source_epoch:
        raise SessionMigrateError("prepared source root changed; prepare again")
    if _root_epoch(state.built.artifact.cwd) != state.cwd_epoch or not _target_epoch_matches(
        state.target_root or state.built.output.parent,
        state.target_epoch,
        database_file=(
            state.built.request.command != OperationKind.CONVERT
            and state.built.artifact.target_format == TargetFormat.MASTRACODE
        ),
    ):
        raise SessionMigrateError("prepared destination or CWD changed; prepare again")


def prepare_operation(
    request: OperationRequest,
    *,
    environ: Mapping[str, str] | None = None,
    source_environ: Mapping[str, str] | None = None,
    progress: ProgressCallback | None = None,
    cancellation: CancellationToken | None = None,
    prepublication_check: Callable[[], None] | None = None,
    max_source_bytes: int | None = None,
    profile_context: Mapping[str, str] | None = None,
) -> PreparedOperation:
    """Resolve and serialize once, then preflight without installing output/metadata."""
    token = cancellation or CancellationToken()
    token.claim()
    try:
        return _prepare_operation(
            request,
            token,
            environ=environ,
            source_environ=source_environ,
            progress=progress,
            prepublication_check=prepublication_check,
            max_source_bytes=max_source_bytes,
            profile_context=profile_context,
        )
    finally:
        token.close()


def _prepare_operation(
    request: OperationRequest,
    token: CancellationToken,
    *,
    environ: Mapping[str, str] | None = None,
    source_environ: Mapping[str, str] | None = None,
    progress: ProgressCallback | None = None,
    prepublication_check: Callable[[], None] | None = None,
    max_source_bytes: int | None = None,
    profile_context: Mapping[str, str] | None = None,
) -> PreparedOperation:
    """Resolve and serialize once, then preflight without installing output/metadata."""
    try:
        _validate_title_request(request)
        context = dict(profile_context or {})
        if set(context) - _PROFILE_CONTEXT or any(
            not isinstance(value, str) for value in context.values()
        ):
            raise SessionMigrateError("proposal profile metadata is invalid")
        token.check()
        request, target, source = _operation_context(request, environ, source_environ)
        for environment in (target, source):
            for key in _PUBLIC_ENVIRONMENT:
                if key in environment and key not in {"OPENCODE_BIN", "KILO_BIN"}:
                    environment[key] = str(Path(environment[key]).resolve())
        if request.path is not None:
            request = replace(request, path=request.path.resolve(strict=True))
        if request.source_home is not None:
            request = replace(request, source_home=request.source_home.resolve(strict=True))
        if prepublication_check is not None:
            prepublication_check()
        _notify(progress, ProgressStage.LOADING)
        request = replace(request, session_id=request.session_id or str(uuid.uuid4()), dry_run=True)
        built = _build_operation(
            request, target, source, progress, token, max_source_bytes, observe_target=True
        )
        if built.source_root is not None and built.artifact.source.source_format in {
            AgentFormat.OPENCODE,
            AgentFormat.KILO,
        }:
            source = _catalog_virtual_source_environment(
                built.artifact.source.source_format, built.source_root, source
            )
        built = _pin_clients(built, target, source)
        root = _source_root(built, source)
        witness = _source_witness(built.artifact.source, root)
        options = _public_options(built.request, target, source, context)
        destination_before = _target_root(built, target)
        root_existed_before_probe = (
            destination_before.exists() if destination_before is not None else None
        )
        token.check()
        _notify(progress, ProgressStage.PREFLIGHT)
        _install(
            built.request,
            built.artifact,
            built.artifact.target_format,
            built.output,
            built.manifest,
            built.home,
            target,
            True,
        )
        destination = _target_root(built, target)
        state = _PreparedState(
            built,
            MappingProxyType(dict(target)),
            MappingProxyType(dict(source)),
            root,
            witness,
            _root_epoch(root),
            destination,
            _root_epoch(destination or built.output.parent),
            _root_epoch(built.artifact.cwd),
            (
                _client_revision(built.request.source_cli),
                _client_revision(built.request.target_cli),
            ),
            _digest(options),
            MappingProxyType(dict(context)),
            built.artifact.target_sha256,
            _artifact_fingerprint(built),
            prepublication_check,
            max_source_bytes,
        )
        _title_preflight(state)
        _read_prepared_source(state)
        _verify_prepared_options(state)
        token.check()
        target_format = built.artifact.target_format
        schema = (
            "v2"
            if target_format == TargetFormat.OPENCODE
            and opencode_v2.version(built.artifact.target_cli_version)
            else "legacy"
            if target_format in {TargetFormat.OPENCODE, TargetFormat.CODEX}
            else "v3"
            if target_format in {TargetFormat.PI, TargetFormat.OMP}
            else capabilities_for(target_format).schema
        )
        proposal_id = uuid.uuid4().hex
        metadata = {
            "schema_version": 1,
            "migration_version": __version__,
            "proposal_id": proposal_id,
            "mode": "full",
            "operation": request.command.value,
            "source": {
                "format": built.artifact.source.source_format.value,
                "root": str(root),
                "native_id": built.artifact.source.session_id,
                "path": str(built.artifact.source.source_path),
                "witness": witness,
                "sha256": built.artifact.source.source_sha256,
                "records": built.artifact.source.raw_record_count,
                "title": built.artifact.source.title,
                "title_source": "native_metadata",
                "cli_version": built.artifact.source.cli_version,
                "cli_version_source": "native_version_probe"
                if built.artifact.source.source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}
                else "adapter_projection",
                "read_limit_bytes": max_source_bytes,
                "read_limit_scope": (
                    "adapter_limits"
                    if max_source_bytes is None
                    else "conservative_shared_store_stat_preflight"
                    if built.artifact.source.source_format in _SHARED_DATABASE_SOURCES
                    else "selected_export_before_parse"
                    if built.artifact.source.source_format
                    in {AgentFormat.OPENCODE, AgentFormat.KILO}
                    else "private_bounded_snapshot"
                ),
                "events": built.artifact.source.event_counts(),
            },
            "target": {
                "format": target_format.value,
                "root": str(destination) if destination else None,
                "native_id": built.artifact.session_id,
                "reserved_uuid": built.request.session_id,
                "cli": str(built.request.target_cli) if built.request.target_cli else None,
                "cli_version": built.artifact.target_cli_version,
                "cli_version_source": "native_version_probe"
                if target_format == TargetFormat.OPENCODE
                and request.command != OperationKind.CONVERT
                else "writer_metadata",
                "observed_cli_version": built.artifact.target_cli_version
                if target_format == TargetFormat.OPENCODE
                and request.command != OperationKind.CONVERT
                else None,
                "schema": schema,
                "cwd": str(built.artifact.cwd),
                "model": built.request.model,
                "model_provider": built.request.model_provider,
                "timestamp": built.artifact.timestamp,
                "sha256": state.artifact_sha256,
            },
            "title": built.artifact.title_plan.to_dict() if built.artifact.title_plan else None,
            "losses": built.artifact.dropped,
            "warnings": built.artifact.warnings,
            "effects": (
                ["native_source_export_may_update_runtime_state"]
                if built.artifact.source.source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}
                and request.command == OperationKind.TRANSFER
                else []
            )
            + (
                ["native_target_probe_may_initialize_state"]
                if target_format
                in {
                    TargetFormat.OPENCODE,
                    TargetFormat.KILO,
                    TargetFormat.HERMES,
                    TargetFormat.ANTIGRAVITY,
                    TargetFormat.CURSOR,
                    TargetFormat.MASTRACODE,
                    TargetFormat.DEVIN,
                }
                and request.command != OperationKind.CONVERT
                else []
            ),
            "options": options,
            "options_fingerprint": state.options_fingerprint,
            "legacy_result": content_free_result(
                built.artifact, output_path=built.output, manifest_path=built.manifest, dry_run=True
            ),
            "title_receipt": str(title_receipt_path(built.manifest))
            if built.artifact.title_plan
            else None,
            "preflight": {
                "target_root_existed_before_probe": root_existed_before_probe,
                "target_root_exists_after_probe": destination.exists()
                if destination is not None
                else None,
                "target_epoch": state.target_epoch,
                "cwd_epoch": state.cwd_epoch,
                "client_revisions": state.client_revisions,
            },
        }
        return PreparedOperation(state, TransferProposal(proposal_id, _canonical_json(metadata)))
    except OperationCancelledError:
        _notify(progress, ProgressStage.CANCELLED)
        raise
    except Exception:
        _notify(progress, ProgressStage.FAILED)
        raise


def _verify_installed_identity(state: _PreparedState) -> Session:
    built = state.built
    target = built.artifact.target_format
    if target == TargetFormat.OPENCODE:
        session = load_opencode_session(
            built.artifact.session_id,
            source_cli=built.request.target_cli,
            environ=state.target_environment,
        )
    elif target == TargetFormat.KILO:
        session = load_kilo_session(
            built.artifact.session_id,
            source_cli=built.request.target_cli,
            environ=state.target_environment,
        )
    elif target in _SHARED_DATABASE_TARGETS:
        session = _load_shared_database_source(
            AgentFormat(target), built.artifact.session_id, built.home
        )
    else:
        session = load_session(built.output, AgentFormat(target))
    if (
        session.source_format.value != target.value
        or not session.session_id
        or normalized_source_id(AgentFormat(target), session.session_id)
        != normalized_source_id(AgentFormat(target), built.artifact.session_id)
    ):
        raise SessionMigrateError("installed target identity could not be verified")
    return session


def _observe_native_title(state: _PreparedState, session: Session) -> TitleReceipt:
    from session_migrate import conversion

    built = state.built
    observed_version = detect_opencode_version(
        built.request.target_cli, environ=state.target_environment
    )
    completed = conversion._run_opencode(
        [
            str(built.request.target_cli),
            "session",
            "list",
            "--format",
            "json",
            "--standalone",
            "--max-count",
            "1000000",
        ],
        state.target_environment,
        cwd=built.artifact.cwd,
    )
    if len(completed.stdout.encode()) > 64 * 1024 * 1024:
        raise SessionMigrateError("native title metadata exceeded its safety limit")
    try:
        rows = json.loads(completed.stdout)
        selected = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("id") == built.artifact.session_id
        ]
    except (ValueError, TypeError) as exc:
        raise SessionMigrateError("native title metadata could not be verified") from exc
    if (
        not isinstance(rows, list)
        or len(selected) != 1
        or selected[0].get("title") != built.artifact.title_plan.requested
        or session.title != built.artifact.title_plan.requested
    ):
        raise SessionMigrateError("native title did not match the prepared title")
    return TitleReceipt.verified_native(
        built.artifact.title_plan, observed=session.title, cli_version=observed_version
    )


def _materialized_path(path: Path) -> tuple:
    _root_epoch(path)
    info = path.lstat()
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise SessionMigrateError("published target/manifest namespace is unavailable")
    if stat.S_ISDIR(info.st_mode):
        return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _published_namespace(state: _PreparedState) -> tuple:
    paths = (state.built.manifest,)
    if isinstance(state.built.output, Path):
        paths += (state.built.output,)
    target_epoch = _root_epoch(state.target_root) if state.target_root is not None else None
    return target_epoch, tuple((path, _materialized_path(path)) for path in paths)


def _verify_published_namespace(
    state: _PreparedState,
    identity: AliasIdentity | None,
    namespace: tuple,
) -> None:
    if _artifact_fingerprint(state.built) != state.artifact_fingerprint:
        raise SessionMigrateError("published artifact metadata changed")
    if identity is not None:
        identity.validate_root()
    if _published_namespace(state) != namespace:
        raise SessionMigrateError("published target/manifest namespace changed")


def commit_operation(
    prepared: PreparedOperation,
    *,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> TransferReceipt:
    """Consume once; revalidate authority/preflight and publish the SAME artifact."""
    token = cancellation or CancellationToken()
    token.claim()
    return _commit_operation(prepared, token, progress)


def _commit_operation(
    prepared: PreparedOperation,
    token: CancellationToken,
    progress: ProgressCallback | None,
) -> TransferReceipt:
    state = None
    published = False
    stage = "preflight"
    try:
        if not isinstance(prepared, PreparedOperation):
            raise SessionMigrateError("a private prepared operation handle is required")
        state, proposal = prepared._consume()
        token.check()
        _verify_prepared_options(state)
        _read_prepared_source(state)
        _verify_prepared_options(state)
        built = state.built
        _notify(progress, ProgressStage.PREFLIGHT)
        _install(
            built.request,
            built.artifact,
            built.artifact.target_format,
            built.output,
            built.manifest,
            built.home,
            state.target_environment,
            True,
        )
        store, planned_identity = _title_preflight(state)
        token.check()
        _verify_prepared_options(state)
        _read_prepared_source(state)
        _verify_prepared_options(state)
        token.check()
        token.begin_publication()
        _notify(progress, ProgressStage.INSTALLING)
        stage = "native_install"
        _install(
            built.request,
            built.artifact,
            built.artifact.target_format,
            built.output,
            built.manifest,
            built.home,
            state.target_environment,
            False,
        )
        published = True
        stage = "target_verification"
        identity = planned_identity.bind_root() if planned_identity is not None else None
        namespace = _published_namespace(state)
        title = None
        receipt_path = None
        if built.artifact.title_plan is not None:
            title = TitleReceipt.pending(built.artifact.title_plan)
            if built.request.command != OperationKind.CONVERT:
                stage = "target_verification"
                _notify(progress, ProgressStage.VERIFYING)
                session = _verify_installed_identity(state)
                _verify_published_namespace(state, identity, namespace)
                if built.artifact.title_plan.policy == TitlePolicy.NATIVE:
                    stage = "title_verification"
                    title = _observe_native_title(state, session)
                    _verify_published_namespace(state, identity, namespace)
                else:
                    stage = "alias_publication"
                    store.create(
                        identity,
                        built.artifact.title_plan.requested,
                        proposal_id=proposal.proposal_id,
                        target_verified=True,
                    )
                    title = TitleReceipt.published_alias(built.artifact.title_plan)
                    _verify_published_namespace(state, identity, namespace)
            stage = "title_receipt_publication"
            _verify_published_namespace(state, identity, namespace)
            receipt_path = title_receipt_path(built.manifest)
            binding = TitleReceiptBinding(
                built.artifact.target_format,
                built.artifact.target_sha256,
                identity=identity,
                output_path=built.output if identity is None else None,
                session_id=built.artifact.session_id,
                manifest_path=built.manifest,
                proposal_id=proposal.proposal_id,
            )
            write_title_receipt(receipt_path, title, binding=binding, alias_store=store)
        _verify_published_namespace(state, identity, namespace)
        result = content_free_result(
            built.artifact, output_path=built.output, manifest_path=built.manifest, dry_run=False
        )
        _notify(progress, ProgressStage.COMPLETED)
        return TransferReceipt(
            proposal,
            "completed",
            result,
            title=title,
            title_receipt=receipt_path,
            target_identity=identity.to_dict() if identity else None,
        )
    except OperationCancelledError:
        _notify(progress, ProgressStage.CANCELLED)
        raise
    except NativeImportPartialError as exc:
        if state is not None:
            exc.manifest, exc.root, exc.stage = (
                str(state.built.manifest),
                str(state.target_root),
                stage,
            )
        _notify(progress, ProgressStage.NEEDS_RECONCILE)
        raise
    except Exception:
        if published and state is not None:
            _notify(progress, ProgressStage.NEEDS_RECONCILE)
            if state.built.request.command == OperationKind.CONVERT:
                raise PublishedArtifactPartialError(
                    target_format=state.built.artifact.target_format,
                    session_id=state.built.artifact.session_id,
                    output=str(state.built.output),
                    manifest=str(state.built.manifest),
                    stage=stage,
                ) from None
            raise NativeImportPartialError(
                "Native session exists; title finalization needs reconciliation. Do not reimport.",
                target_format=state.built.artifact.target_format,
                session_id=state.built.artifact.session_id,
                manifest=str(state.built.manifest),
                root=str(state.target_root),
                stage=stage,
            ) from None
        _notify(progress, ProgressStage.FAILED)
        raise
    finally:
        token.close()


def _notify(callback: ProgressCallback | None, stage: ProgressStage) -> None:
    if callback is not None:
        # Observers cannot turn a completed native import into a retry.
        with suppress(Exception):
            callback(ProgressEvent(stage))


def _optional_home(environ: Mapping[str, str]) -> Path | None:
    return environment_home(environ) if environ.get("HOME") else None


def _temporary_root(environ: Mapping[str, str]) -> Path:
    configured = [environ[key] for key in ("TMPDIR", "TEMP", "TMP") if environ.get(key)]
    defaults = (
        [r"c:\temp", r"c:\tmp", r"\temp", r"\tmp"]
        if os.name == "nt"
        else ["/tmp", "/var/tmp", "/usr/tmp"]
    )
    for value in [*configured, *defaults, str(Path.cwd())]:
        candidate = expand_environment_path(Path(value), environ).absolute()
        if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    raise SessionMigrateError("selected environment has no usable temporary directory")


def _catalog_default(environ: Mapping[str, str]) -> Path:
    home = _optional_home(environ)
    if not home and not (environ.get("SESSION_MIGRATE_CATALOG") or environ.get("XDG_STATE_HOME")):
        environment_home(environ)
    return default_catalog_path(environ=environ, home=home)


def _catalog_virtual_source_environment(
    source_format: AgentFormat, root: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    """Map a catalog store to the directory that the official CLI will open."""

    message = (
        f"cataloged {source_format.value} source root cannot be mapped to its native "
        f"XDG data store; use a root reachable as XDG_DATA_HOME/{source_format.value}"
    )
    try:
        selected = root.resolve(strict=True)
        native = selected.parent / source_format.value
        # Directory identity matters: hardlinked DB files can have different WAL/SHM stores.
        if not selected.is_dir() or not selected.samefile(native):
            raise SessionMigrateError(message)
        database = selected / f"{source_format.value}.db"
        native_database = native / database.name
        if (
            database.is_symlink()
            or not database.is_file()
            or not database.samefile(native_database)
        ):
            raise SessionMigrateError(message)
    except (OSError, RuntimeError) as exc:
        raise SessionMigrateError(message) from exc
    values = dict(environ)
    values["XDG_DATA_HOME"] = str(selected.parent)
    return values


def run_operation(
    request: OperationRequest,
    *,
    environ: Mapping[str, str] | None = None,
    progress: ProgressCallback | None = None,
    source_environ: Mapping[str, str] | None = None,
    cancellation: CancellationToken | None = None,
    prepublication_check: Callable[[], None] | None = None,
    max_source_bytes: int | None = None,
) -> OperationResult | TransferReceipt:
    """Snapshot target/source environments independently and use validated adapters.

    Omitted source_environ preserves the legacy single-environment contract.
    """

    if request.target_title is not None or request.require_native_title:
        token = cancellation or CancellationToken()
        token.claim()
        try:
            prepared = _prepare_operation(
                request,
                token,
                environ=environ,
                source_environ=source_environ,
                progress=progress,
                max_source_bytes=max_source_bytes,
                prepublication_check=prepublication_check,
            )
            if request.dry_run and request.command != OperationKind.CONVERT:
                token.check()
                proposal = proposal_metadata(prepared)
                prepared.discard()
                return TransferReceipt(proposal, "ready", proposal.to_dict()["legacy_result"])
            return _commit_operation(prepared, token, progress)
        finally:
            token.close()
    if cancellation is not None:
        cancellation.claim()
    try:
        return _run_operation(
            request,
            environ=environ,
            source_environ=source_environ,
            progress=progress,
            cancellation=cancellation,
            prepublication_check=prepublication_check,
            max_source_bytes=max_source_bytes,
        )
    finally:
        if cancellation is not None:
            cancellation.close()


def _run_operation(
    request: OperationRequest,
    *,
    environ: Mapping[str, str] | None,
    source_environ: Mapping[str, str] | None,
    progress: ProgressCallback | None,
    cancellation: CancellationToken | None,
    prepublication_check: Callable[[], None] | None,
    max_source_bytes: int | None,
) -> OperationResult:
    request, values, source_values = _operation_context(request, environ, source_environ)
    _notify(progress, ProgressStage.LOADING)
    try:
        if cancellation is not None:
            cancellation.check()
        result = _execute(
            request,
            values,
            progress,
            source_values,
            cancellation,
            prepublication_check,
            max_source_bytes,
        )
    except OperationCancelledError:
        _notify(progress, ProgressStage.CANCELLED)
        raise
    except NativeImportPartialError:
        _notify(progress, ProgressStage.NEEDS_RECONCILE)
        raise
    except Exception:
        _notify(progress, ProgressStage.FAILED)
        raise
    _notify(progress, ProgressStage.COMPLETED)
    return result


def _operation_context(
    request: OperationRequest,
    environ: Mapping[str, str] | None,
    source_environ: Mapping[str, str] | None,
) -> tuple[OperationRequest, dict[str, str], dict[str, str]]:
    values = dict(os.environ if environ is None else environ)
    if environ is None and not values.get("HOME"):
        values["HOME"] = str(Path.home())
    source_values = dict(values if source_environ is None else source_environ)
    source_fields = {"path", "source_home", "source_cwd", "source_cli", "catalog"}
    paths = {
        field.name: expand_environment_path(
            value, source_values if field.name in source_fields else values
        )
        for field in fields(request)
        if isinstance(value := getattr(request, field.name), Path)
    }
    request = replace(request, **paths)
    for selected in (values, source_values):
        for key in (
            "OPENCODE_BIN",
            "KILO_BIN",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "SESSION_MIGRATE_CATALOG",
        ):
            if selected.get(key):
                selected[key] = str(expand_environment_path(Path(selected[key]), selected))
        # Explicit temp roots avoid tempfile's process-wide, cached environment.
        selected["TMPDIR"] = str(_temporary_root(selected))
    return request, values, source_values


def _execute(
    request: OperationRequest,
    environ: Mapping[str, str],
    progress: ProgressCallback | None,
    source_environ: Mapping[str, str],
    cancellation: CancellationToken | None,
    prepublication_check: Callable[[], None] | None,
    max_source_bytes: int | None,
) -> OperationResult:
    built = _build_operation(
        request, environ, source_environ, progress, cancellation, max_source_bytes
    )
    artifact, target_format = built.artifact, built.artifact.target_format
    output_path, manifest_path, home, dry_run = (
        built.output,
        built.manifest,
        built.home,
        built.dry_run,
    )
    result = content_free_result(
        artifact,
        output_path=output_path,
        manifest_path=manifest_path,
        dry_run=dry_run,
    )
    if cancellation is not None:
        cancellation.check()
        _notify(progress, ProgressStage.PREFLIGHT)
        if not dry_run:
            _install(
                request,
                artifact,
                target_format,
                output_path,
                manifest_path,
                home,
                environ,
                True,
            )
        cancellation.check()
        if not dry_run:
            if prepublication_check is not None:
                prepublication_check()
            cancellation.check()
            cancellation.begin_publication()
    if cancellation is None and not dry_run and prepublication_check is not None:
        prepublication_check()
    _notify(progress, ProgressStage.PREFLIGHT if dry_run else ProgressStage.INSTALLING)
    _install(
        request,
        artifact,
        target_format,
        output_path,
        manifest_path,
        home,
        environ,
        dry_run,
    )
    return OperationResult(**result)


@dataclass(frozen=True, slots=True, repr=False)
class _OperationBuild:
    request: OperationRequest
    artifact: ConversionArtifact
    output: Path | str
    manifest: Path
    home: Path | None
    source_root: Path | None
    dry_run: bool


def _resolve_source(
    request: OperationRequest,
    source_environ: Mapping[str, str],
    max_source_bytes: int | None,
    expected_root: Path | None = None,
) -> tuple[Session, TargetFormat, Path | None]:
    if request.command != OperationKind.TRANSFER and (
        request.path is None or request.target_format is None
    ):
        raise SessionMigrateError("convert/import require a source path and target format")
    if request.command == OperationKind.CONVERT and request.output is None:
        raise SessionMigrateError("convert requires an output path")
    source_root = None
    source_options = {"max_source_bytes": max_source_bytes} if max_source_bytes is not None else {}
    if request.command == "transfer":
        session = None
        virtual_source_environ = None
        if request.catalog_id or request.title:
            if request.source_id:
                raise SessionMigrateError("pass only one of SOURCE_ID, --catalog-id, or --title")
            if request.catalog_id and request.title:
                raise SessionMigrateError("pass either --catalog-id or --title, not both")
            if request.source_home or request.source_cwd:
                raise SessionMigrateError(
                    "--source-home/--source-cwd do not apply with catalog selection"
                )
            with Catalog(request.catalog or _catalog_default(source_environ)) as catalog:
                catalog_id = request.catalog_id
                if request.title:
                    title = request.title.strip()
                    if not title:
                        raise SessionMigrateError("--title cannot be empty")
                    source_filter = (
                        AgentFormat(request.source_format) if request.source_format else None
                    )
                    matches = catalog.list_sessions(
                        query=title,
                        include_paths=True,
                        agent_format=source_filter,
                        limit=10_000,
                    )
                    exact = [
                        match
                        for match in matches
                        if match.title and match.title.casefold() == title.casefold()
                    ]
                    candidates = exact or matches
                    if not candidates:
                        raise SessionMigrateError(
                            "catalog title was not found; refresh the catalog or search first"
                        )
                    if len(candidates) != 1:
                        raise SessionMigrateError(
                            "catalog title is ambiguous; use catalog search and --catalog-id"
                        )
                    catalog_id = candidates[0].catalog_id
                assert catalog_id is not None
                entry = catalog.get_session(catalog_id, include_paths=True)
                source_reference = catalog.session_source_for_transfer(catalog_id)
            source_format = source_reference.format
            source_path = source_reference.path
            source_root = source_reference.root
            if expected_root is not None and source_root.resolve() != expected_root:
                raise SessionMigrateError("prepared source root changed; prepare again")
            if request.source_format and AgentFormat(request.source_format) != source_format:
                raise SessionMigrateError("--from does not match the catalog session format")
            requested_source_id = entry.session_id
            if source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                virtual_source_environ = _catalog_virtual_source_environment(
                    source_format, source_reference.root, source_environ
                )
        else:
            if not request.source_id or not request.source_format:
                raise SessionMigrateError(
                    "transfer requires SOURCE_ID with --from, --catalog-id, or --title"
                )
            source_format = AgentFormat(request.source_format)
            requested_source_id = normalized_source_id(source_format, request.source_id)
            if source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                if request.source_home or request.source_cwd:
                    raise SessionMigrateError(
                        f"{source_format.value} source transfer uses its normal "
                        "HOME/XDG environment; "
                        "--source-home/--source-cwd do not apply"
                    )
                loader = (
                    load_opencode_session
                    if source_format == AgentFormat.OPENCODE
                    else load_kilo_session
                )
                session = loader(
                    requested_source_id,
                    source_cli=request.source_cli,
                    environ=source_environ,
                    **source_options,
                )
            elif source_format in _SHARED_DATABASE_SOURCES:
                source_home = request.source_home or default_target_home(
                    source_format, environ=source_environ
                )
                source_root = source_home
                _shared_source_budget(source_format, source_home, max_source_bytes)
                session = _load_shared_database_source(
                    source_format,
                    requested_source_id,
                    source_home,
                )
            else:
                source_home = request.source_home or default_target_home(
                    source_format, environ=source_environ
                )
                source_root = source_home
                source_path = locate_session(
                    source_format,
                    requested_source_id,
                    source_home,
                    cwd=request.source_cwd,
                    user_home=(
                        environment_home(source_environ)
                        if source_format == AgentFormat.OMP and request.source_cwd is not None
                        else _optional_home(source_environ)
                    ),
                    temp_root=_temporary_root(source_environ),
                )
        if request.source_cli and source_format not in {
            AgentFormat.OPENCODE,
            AgentFormat.KILO,
        }:
            raise SessionMigrateError("--source-cli applies only to OpenCode/Kilo transfer")
        if session is None:
            if source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                if not requested_source_id:
                    raise SessionMigrateError(
                        f"cataloged {source_format.value} session is missing its native session ID"
                    )
                loader = (
                    load_opencode_session
                    if source_format == AgentFormat.OPENCODE
                    else load_kilo_session
                )
                session = loader(
                    requested_source_id,
                    source_cli=request.source_cli,
                    environ=virtual_source_environ or source_environ,
                    **source_options,
                )
            elif source_format in _SHARED_DATABASE_SOURCES:
                if not requested_source_id:
                    raise SessionMigrateError(
                        f"cataloged {source_format.value} session is missing its native session ID"
                    )
                _shared_source_budget(source_format, source_reference.root, max_source_bytes)
                session = _load_shared_database_source(
                    source_format,
                    requested_source_id,
                    source_reference.root,
                )
            else:
                assert source_path is not None
                session = _load_path_source(
                    source_path, source_format, source_root, source_environ, max_source_bytes
                )
        if not session.session_id:
            raise SessionMigrateError("discovered transcript has no native session ID metadata")
        if requested_source_id and (
            normalized_source_id(source_format, session.session_id) != requested_source_id
        ):
            raise SessionMigrateError(
                "discovered transcript metadata does not match the source UUID"
            )
        if request.target_format:
            target_format = TargetFormat(request.target_format)
        elif source_format == AgentFormat.CLAUDE:
            target_format = TargetFormat.CODEX
        elif source_format == AgentFormat.CODEX:
            target_format = TargetFormat.CLAUDE
        else:
            raise SessionMigrateError(
                f"{source_format.value} source transfer requires an explicit --to target"
            )
    else:
        source_format = AgentFormat(request.source_format) if request.source_format else None
        session = _load_path_source(
            request.path, source_format, None, source_environ, max_source_bytes
        )
        target_format = TargetFormat(request.target_format)
    return session, target_format, source_root


def _build_operation(
    request: OperationRequest,
    environ: Mapping[str, str],
    source_environ: Mapping[str, str],
    progress: ProgressCallback | None,
    cancellation: CancellationToken | None,
    max_source_bytes: int | None,
    observe_target: bool = False,
) -> _OperationBuild:
    session, target_format, source_root = _resolve_source(request, source_environ, max_source_bytes)
    target_cli_version = request.target_cli_version
    if cancellation is not None:
        cancellation.check()
    if target_format == TargetFormat.OPENCODE and request.home:
        raise SessionMigrateError(OPENCODE_HOME_UNSUPPORTED)
    if target_format == TargetFormat.KILO and request.home:
        raise SessionMigrateError(KILO_HOME_UNSUPPORTED)
    if request.target_cli and (
        target_format
        not in {
            TargetFormat.OPENCODE,
            TargetFormat.KILO,
            TargetFormat.ANTIGRAVITY,
            TargetFormat.CURSOR,
            TargetFormat.HERMES,
        }
        or request.command == "convert"
    ):
        raise SessionMigrateError(
            "--target-cli only applies to OpenCode/Kilo/Antigravity/Cursor import "
            "and transfer, or Hermes import and transfer"
        )
    if (
        target_format == TargetFormat.OPENCODE
        and request.command != "convert"
        and (observe_target or request.target_title is not None or not request.target_cli_version)
    ):
        try:
            target_cli_version = detect_opencode_version(request.target_cli, environ=environ)
        except SessionMigrateError:
            if observe_target or request.target_title is not None:
                raise SessionMigrateError(
                    "target client version probe failed or is unsupported"
                ) from None
            raise
        if (observe_target or request.target_title is not None) and request.target_cli_version:
            expected = opencode_v2.version(request.target_cli_version) or request.target_cli_version
            if target_cli_version != expected:
                raise SessionMigrateError("target client version differs from prepared options")
    _notify(progress, ProgressStage.CONVERTING)
    artifact = convert_session(
        session,
        ConversionOptions(
            target_format=target_format,
            session_id=request.session_id,
            cwd=request.cwd,
            target_cli_version=target_cli_version,
            model_provider=request.model_provider,
            model=request.model,
            target_title=request.target_title,
            title_policy=request.title_policy,
        ),
        environ=environ,
    )
    home: Path | None = None
    if request.command == "convert":
        output_path = request.output
        manifest_path = output_path.with_name(f"{output_path.name}.session-migrate.json")
        dry_run = False
    elif target_format == TargetFormat.OPENCODE:
        output_path = f"opencode:{artifact.session_id}"
        manifest_path = opencode_manifest_path(
            artifact, state_home=default_migration_state_home(environ=environ)
        )
        dry_run = request.dry_run
    elif target_format == TargetFormat.KILO:
        output_path = f"kilo:{artifact.session_id}"
        manifest_path = kilo_manifest_path(
            artifact, state_home=default_migration_state_home(environ=environ)
        )
        dry_run = request.dry_run
    elif target_format in _SHARED_DATABASE_TARGETS:
        home = request.home or default_target_home(target_format, environ=environ)
        output_path = f"{target_format.value}:{artifact.session_id}"
        manifest_path = shared_database_manifest_path(artifact, target_home=home)
        dry_run = request.dry_run
    else:
        home = request.home or default_target_home(target_format, environ=environ)
        output_path, manifest_path = target_import_paths(
            artifact,
            home,
            user_home=(
                environment_home(environ)
                if target_format == TargetFormat.OMP
                else _optional_home(environ)
            ),
            temp_root=_temporary_root(environ),
        )
        dry_run = request.dry_run
    return _OperationBuild(
        request, artifact, output_path, manifest_path, home, source_root, dry_run
    )


def _install(
    request: OperationRequest,
    artifact: ConversionArtifact,
    target_format: TargetFormat,
    output_path: Path | str,
    manifest_path: Path,
    home: Path | None,
    environ: Mapping[str, str],
    dry_run: bool,
) -> None:
    if request.command == "convert" and dry_run:
        ensure_target_paths_available(output_path, manifest_path)
    elif target_format == TargetFormat.OPENCODE and request.command != "convert":
        install_opencode_artifact(
            artifact,
            manifest_path=manifest_path,
            target_cli=request.target_cli,
            environ=environ,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.KILO and request.command != "convert":
        install_kilo_artifact(
            artifact,
            manifest_path=manifest_path,
            target_cli=request.target_cli,
            environ=environ,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.COPILOT and request.command != "convert":
        install_copilot_artifact(
            artifact,
            target_home=home,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.ANTIGRAVITY and request.command != "convert":
        install_antigravity_artifact(
            artifact,
            target_home=home,
            target_cli=request.target_cli,
            environ=environ,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.CURSOR and request.command != "convert":
        install_cursor_artifact(
            artifact,
            target_home=home,
            target_cli=request.target_cli,
            environ=environ,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.VIBE and request.command != "convert":
        install_vibe_artifact(
            artifact,
            target_home=home,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.KIMI and request.command != "convert":
        install_kimi_artifact(
            artifact,
            target_home=home,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.GROK and request.command != "convert":
        install_grok_artifact(artifact, target_home=home, dry_run=dry_run)
    elif target_format == TargetFormat.OPENHANDS and request.command != "convert":
        install_openhands_artifact(artifact, target_home=home, dry_run=dry_run)
    elif target_format == TargetFormat.HERMES and request.command != "convert":
        install_hermes_artifact(
            artifact,
            target_home=home,
            target_cli=request.target_cli,
            environ=environ,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.MASTRACODE and request.command != "convert":
        install_mastracode_artifact(
            artifact,
            target_home=home,
            dry_run=dry_run,
        )
    elif target_format == TargetFormat.DEVIN and request.command != "convert":
        install_devin_artifact(
            artifact,
            target_home=home,
            dry_run=dry_run,
        )
    elif not dry_run:
        write_artifact(
            artifact,
            output_path=output_path,
            manifest_path=manifest_path,
        )
    else:
        ensure_target_paths_available(output_path, manifest_path)


def _load_path_source(
    path: Path,
    source_format: AgentFormat | None,
    root: Path | None,
    environ: Mapping[str, str],
    max_source_bytes: int | None,
) -> Session:
    if max_source_bytes is None:
        return load_session(path, source_format)
    from session_migrate.inspection import detect_path_format
    from session_migrate.profiles import load_bounded_source

    source_format = source_format or detect_path_format(path)

    return load_bounded_source(
        path,
        source_format,
        root=root or _physical_source_root(path, source_format),
        max_source_bytes=max_source_bytes,
        environ=environ,
    )


def _load_shared_database_source(
    source_format: AgentFormat,
    session_id: str,
    source_home: Path,
) -> Session:
    if source_format == AgentFormat.HERMES:
        return load_hermes_session(session_id, source_home=source_home)
    if source_format == AgentFormat.MASTRACODE:
        return load_mastracode_session(session_id, source_home=source_home)
    if source_format == AgentFormat.DEVIN:
        return load_devin_session(session_id, source_home=source_home)
    raise SessionMigrateError("source is not a supported shared-database format")


def _shared_source_budget(source_format: AgentFormat, root: Path, maximum: int | None) -> None:
    if maximum is None:
        return
    if type(maximum) is not int or maximum < 1:
        raise SessionMigrateError("Source byte budget must be a positive integer.")
    from session_migrate.profiles import source_read_paths

    names = {
        AgentFormat.HERMES: "state.db",
        AgentFormat.MASTRACODE: "mastra.db",
        AgentFormat.DEVIN: "sessions.db",
    }
    path = root if root.is_file() else root / names[source_format]
    paths = source_read_paths(source_format, path, root)
    if sum(candidate.stat().st_size for candidate in paths if candidate.is_file()) > maximum:
        raise SourceReadLimitError("Source exceeds the conservative shared-store preflight budget.")
