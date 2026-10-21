"""Typed orchestration shared by command-line and interactive clients."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
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
    OperationNeedsReconcileError,
    PublishedArtifactPartialError,
    SessionMigrateError,
    SourceReadLimitError,
)
from session_migrate.formats import claude, codex, opencode_v2
from session_migrate.lineage import (
    CheckpointRecord,
    CompletionReceipt,
    CompletionRecord,
    Coverage,
    EvidenceReference,
    LineageStore,
    NativeState,
    OperationRecord,
    OperationState,
    PolicyVersions,
    ProjectionExpectation,
    ReconcileReceipt,
    SourceMetadata,
)
from session_migrate.model import AgentFormat, Session, TargetFormat
from session_migrate.native_namespaces import (
    _bind_execution_context,
    qualify_namespace,
    reserve_namespace,
)
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext, capture_source
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
    RECORDING_INTENT = "recording_intent"
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
    target_client: str | None = None
    target_server: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", OperationKind(self.command))
        if self.source_format is not None:
            object.__setattr__(self, "source_format", AgentFormat(self.source_format))
        if self.target_format is not None:
            object.__setattr__(self, "target_format", TargetFormat(self.target_format))
        if self.target_client is not None and self.target_client != "openchamber":
            raise SessionMigrateError("target client is unsupported")
        if self.target_server is not None and self.target_client != "openchamber":
            raise SessionMigrateError("--target-server requires the OpenChamber destination")
        if (
            self.target_client is not None or self.target_server is not None
        ) and self.target_format is not TargetFormat.OPENCODE:
            raise SessionMigrateError("OpenChamber/server targets require OpenCode format")
        if self.target_server is not None:
            from session_migrate.openchamber import validated_server

            object.__setattr__(self, "target_server", validated_server(self.target_server))
        if (
            self.target_client == "openchamber"
            and self.command is not OperationKind.CONVERT
            and self.target_server is None
        ):
            raise SessionMigrateError("OpenChamber requires its local OpenCode --target-server")
        if (
            self.target_client == "openchamber"
            and self.command is OperationKind.CONVERT
            and self.target_cli_version is None
        ):
            object.__setattr__(self, "target_cli_version", opencode_v2.VALIDATED_VERSION)
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
    lineage_receipt: CompletionReceipt | None = field(default=None, repr=False, compare=False)
    target_client: str | None = None
    target_server: str | None = None
    backend_discovery_verified: bool | None = None

    def to_dict(self) -> dict[str, Any]:

        return {
            field.name: deepcopy(getattr(self, field.name))
            for field in fields(self)
            if field.name != "lineage_receipt"
            and not (
                field.name in {"target_client", "target_server", "backend_discovery_verified"}
                and getattr(self, field.name) is None
            )
        }


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
    lineage_receipt: CompletionReceipt | None

    def __init__(
        self,
        proposal: TransferProposal,
        status: str,
        legacy_result: Mapping[str, Any],
        *,
        title: TitleReceipt | None = None,
        title_receipt: Path | None = None,
        target_identity: Mapping[str, Any] | None = None,
        lineage_receipt: CompletionReceipt | None = None,
    ) -> None:
        object.__setattr__(self, "proposal", proposal)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "_legacy_json", _canonical_json(dict(legacy_result)))
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "title_receipt", str(title_receipt) if title_receipt else None)
        object.__setattr__(self, "_identity_json", _canonical_json(target_identity))
        object.__setattr__(self, "lineage_receipt", lineage_receipt)

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


def _client_result(result: dict, request: OperationRequest, *, completed: bool = False) -> dict:
    if request.target_client == "openchamber":
        result = dict(result)
        result["target_client"] = request.target_client
        if request.target_server:
            result["target_server"] = request.target_server
            if completed and request.command is not OperationKind.CONVERT:
                result["backend_discovery_verified"] = True
    return result


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
    *,
    transformation=None,
    return_link=None,
) -> dict[str, Any]:
    value = {
        "request": {
            item.name: str(value) if isinstance(value, Path) else value
            for item in fields(request)
            if item.name != "dry_run" and (value := getattr(request, item.name)) is not None
        },
        "target_environment": {key: target[key] for key in _PUBLIC_ENVIRONMENT if key in target},
        "source_environment": {key: source[key] for key in _PUBLIC_ENVIRONMENT if key in source},
        "profiles": dict(profiles),
    }
    if transformation is not None or return_link is not None:
        from session_migrate.lineage import _encode

        value["runtime_metadata"] = {
            "transformation": json.loads(_encode(transformation)) if transformation else None,
            "return_link": json.loads(_encode(return_link)) if return_link else None,
        }
    return value


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
    if not path.exists():
        # Creating the migrator's private state can materialize HOME/.local
        # while the native destination is still absent. The original existing
        # ancestor must remain identical and the new chain must be no-follow.
        return _root_epoch(Path(expected[1]))[1:] == expected[1:]
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
    if built.request.command != OperationKind.TRANSFER:
        return _physical_source_root(
            built.artifact.source.source_path, built.artifact.source.source_format
        )
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
    migration_state_home: Path | None
    projection: Any = None
    observed_captures: list[NativeSnapshot] = field(default_factory=list, repr=False, compare=False)
    projection_versions: PolicyVersions | None = None
    projection_context: Any = None
    observed_export_proofs: list[Any] = field(default_factory=list, repr=False, compare=False)
    last_source_capture: list[NativeSnapshot] = field(
        default_factory=list, repr=False, compare=False
    )
    catalog_opener: Callable[[Path], Catalog] | None = None
    backend_binding: str | None = None


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
    if state.built.runtime_context is not None:
        from session_migrate.handoff_operations import validate_runtime_context

        validate_runtime_context(state.built.runtime_context, fresh=True)
    if state.check is not None:
        state.check()
    if _root_epoch(state.source_root) != state.source_epoch:
        raise SessionMigrateError("prepared source root changed; prepare again")
    captures = []
    session, _target, root = _resolve_source(
        state.built.request,
        state.source_environment,
        state.max_source_bytes,
        expected_root=state.source_root,
        on_capture=captures.append if state.built.source_snapshot is not None else None,
        catalog_opener=state.catalog_opener,
        readonly_catalog=state.built.runtime_context is not None,
    )
    if root is not None and root.resolve() != state.source_root:
        raise SessionMigrateError("prepared source root changed; prepare again")
    if _source_witness(session, state.source_root) != state.source_witness:
        raise SessionMigrateError("prepared source changed; prepare again")
    if state.built.source_snapshot is not None and (
        len(captures) != 1
        or captures[0].authority_sha256 != state.built.source_snapshot.authority_sha256
    ):
        raise SessionMigrateError("prepared source changed; prepare again")
    state.last_source_capture.clear()
    state.last_source_capture.extend(captures)


def _artifact_fingerprint(built: _OperationBuild) -> str:
    artifact, source = built.artifact, built.artifact.source
    value = {
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
            "payload_fingerprint": _source_payload_fingerprint(source),
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
    if built.runtime_context is not None:
        from session_migrate.handoff_operations import validate_runtime_context

        validate_runtime_context(built.runtime_context)
        from session_migrate.lineage import _encode

        value["runtime_metadata"] = {
            "transformation": json.loads(_encode(built.artifact.transformation))
            if built.artifact.transformation
            else None,
            "input_identity": json.loads(_encode(built.runtime_context.derived_input.identity))
            if built.runtime_context.derived_input
            else None,
            "original_authority": built.runtime_context.authority.authority_sha256,
            "return_link": json.loads(_encode(built.runtime_context.return_link))
            if built.runtime_context.return_link
            else None,
        }
    return _digest(value)


def _source_payload_fingerprint(source: Session) -> str:
    from session_migrate.canonical import _json_bytes

    try:
        value = [
            {
                "kind": event.kind.value,
                "role": event.role.value if event.role is not None else None,
                "provenance": asdict(event.provenance),
                "timestamp": event.timestamp,
                "text": event.text,
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
                "payload": event.payload,
            }
            for event in source.events
        ]
        return hashlib.sha256(
            b"session-migrate/private-source-ir/v1\0" + _json_bytes(value)
        ).hexdigest()
    except (SessionMigrateError, TypeError, ValueError, UnicodeError, RecursionError):
        raise SessionMigrateError("Prepared source payload cannot be bound safely.") from None


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
    if state.backend_binding is not None:
        from session_migrate.openchamber import assert_backend_matches

        observed = assert_backend_matches(
            state.built.request.target_server,
            state.target_environment,
            state.built.artifact.target_cli_version,
        )
        if (
            _canonical_json(observed) != state.backend_binding
            or state.target_environment.get("SESSION_MIGRATE_OPENCODE_BINDING")
            != state.backend_binding
        ):
            raise SessionMigrateError("prepared OpenChamber backend changed; prepare again")
    if state.projection is not None:
        if state.built.artifact.transformation is not None:
            from session_migrate.derived_projection import (
                validate_derived_projection as validate_prepared,
            )
        else:
            from session_migrate.projections import validate_prepared

        try:
            validate_prepared(state.projection)
        except SessionMigrateError:
            raise SessionMigrateError(
                "prepared options or artifact changed; prepare again"
            ) from None
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
                transformation=state.built.artifact.transformation,
                return_link=state.built.artifact.return_link,
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
    migration_state_home: Path | None = None,
    catalog_opener: Callable[[Path], Catalog] | None = None,
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
            migration_state_home=migration_state_home,
            catalog_opener=catalog_opener,
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
    migration_state_home: Path | None = None,
    catalog_opener: Callable[[Path], Catalog] | None = None,
    runtime_context=None,
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
        base_environment = dict(os.environ if environ is None else environ)
        state_home = migration_state_home
        if state_home is None and (
            base_environment.get("HOME") or base_environment.get("XDG_STATE_HOME")
        ):
            state_home = default_migration_state_home(environ=base_environment)
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
            request,
            target,
            source,
            progress,
            token,
            max_source_bytes,
            observe_target=True,
            catalog_opener=catalog_opener,
            runtime_context=runtime_context,
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
        options = _public_options(
            built.request,
            target,
            source,
            context,
            transformation=built.artifact.transformation,
            return_link=built.artifact.return_link,
        )
        destination_before = _target_root(built, target)
        root_existed_before_probe = (
            destination_before.exists() if destination_before is not None else None
        )
        token.check()
        _notify(progress, ProgressStage.PREFLIGHT)
        backend_binding = None
        if built.request.target_server and request.command is not OperationKind.CONVERT:
            from session_migrate.openchamber import assert_backend_matches

            backend_binding = _canonical_json(
                assert_backend_matches(
                    built.request.target_server, target, built.artifact.target_cli_version
                )
            )
            target["SESSION_MIGRATE_OPENCODE_BINDING"] = backend_binding
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
            state_home,
        )
        state = replace(state, catalog_opener=catalog_opener, backend_binding=backend_binding)
        projection, projection_versions, projection_context = _prepare_lineage_projection(state)
        state = replace(
            state,
            projection=projection,
            projection_versions=projection_versions,
            projection_context=projection_context,
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
            "mode": "summary" if built.artifact.transformation is not None else "full",
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
        if built.artifact.transformation is not None or built.artifact.return_link is not None:
            from session_migrate.lineage import _encode

            metadata["transformation"] = json.loads(_encode(built.artifact.transformation))
            metadata["return_link"] = json.loads(_encode(built.artifact.return_link))
        if built.request.target_client or built.request.target_server:
            metadata["target"]["client"] = built.request.target_client or "opencode"
            metadata["target"]["server"] = built.request.target_server
            metadata["target"]["backend"] = (
                json.loads(backend_binding) if backend_binding is not None else None
            )
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
            on_capture=state.observed_captures.append,
            on_native_observation=state.observed_export_proofs.append,
        )
    elif target == TargetFormat.KILO:
        session = load_kilo_session(
            built.artifact.session_id,
            source_cli=built.request.target_cli,
            environ=state.target_environment,
            on_native_observation=state.observed_export_proofs.append,
        )
    elif target in _SHARED_DATABASE_TARGETS:
        session = _load_shared_database_source(
            AgentFormat(target),
            built.artifact.session_id,
            built.home,
            on_native_observation=state.observed_export_proofs.append,
        )
    else:
        session = _load_path_source(
            Path(built.output),
            AgentFormat(target),
            state.target_root,
            state.target_environment,
            None,
            state.observed_captures.append,
            state.observed_export_proofs.append,
        )
    if (
        session.source_format.value != target.value
        or not session.session_id
        or normalized_source_id(AgentFormat(target), session.session_id)
        != normalized_source_id(AgentFormat(target), built.artifact.session_id)
    ):
        raise SessionMigrateError("installed target identity could not be verified")
    if built.request.target_server:
        from session_migrate.openchamber import verify_session_discovery

        verify_session_discovery(
            built.request.target_server,
            built.artifact.session_id,
            built.artifact.cwd,
            state.target_environment,
        )
    return session


def _observe_native_title(state: _PreparedState, session: Session) -> TitleReceipt:
    from session_migrate import conversion

    built = state.built
    observed_version = detect_opencode_version(
        built.request.target_cli, environ=state.target_environment
    )
    if built.request.target_server:
        from session_migrate.openchamber import verify_session_discovery

        selected = verify_session_discovery(
            built.request.target_server,
            built.artifact.session_id,
            built.artifact.cwd,
            state.target_environment,
        )
        if selected.get("title") != built.artifact.title_plan.requested or (
            session.title != built.artifact.title_plan.requested
        ):
            raise SessionMigrateError("native title did not match the prepared title")
        return TitleReceipt.verified_native(
            built.artifact.title_plan, observed=session.title, cli_version=observed_version
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


def _native_namespace(state: _PreparedState):
    return reserve_namespace(
        AgentFormat(state.built.artifact.target_format),
        state.target_root,
        state.built.artifact.session_id,
    )


def _profile_fingerprint(state: _PreparedState, side: str) -> str:
    return state.profiles.get(f"{side}_profile_fingerprint") or _digest(
        {
            "format": (
                state.built.artifact.source.source_format.value
                if side == "source"
                else state.built.artifact.target_format.value
            ),
            "environment": {
                key: value
                for key, value in (
                    state.source_environment if side == "source" else state.target_environment
                ).items()
                if key in _PUBLIC_ENVIRONMENT
            },
            "root": str(state.source_root if side == "source" else state.target_root),
        }
    )


def _client_identifier(value: str | None) -> str | None:
    return (
        value
        if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}", value)
        else None
    )


def _prepare_lineage_projection(state: _PreparedState):
    if state.built.artifact.transformation is not None:
        from session_migrate.derived_projection import prepare_derived_projection

        runtime = state.built.runtime_context
        prepared = prepare_derived_projection(
            runtime.authority,
            runtime.derived_input,
            runtime.transformation,
            state.built.portable_emission,
            artifact_path=state.built.output
            if isinstance(state.built.output, Path)
            else Path(state.built.manifest),
        )
        return prepared, prepared.versions, None
    if state.built.request.command == OperationKind.CONVERT or state.built.source_snapshot is None:
        return None, None, None
    from session_migrate.projection_policies import ProjectionPolicy
    from session_migrate.projections import (
        ProjectionContext,
        SourceVersionContext,
        prepare_projection,
    )

    artifact = state.built.artifact
    known_sources = {
        AgentFormat.CLAUDE: claude.VALIDATED_CLAUDE_VERSIONS,
        AgentFormat.CODEX: codex.VALIDATED_LEGACY_VERSIONS | codex.VALIDATED_PAGINATED_VERSIONS,
        AgentFormat.OPENCODE: {"2.0.22", "2.0.23", "2.1.1"},
    }
    known_targets = {
        TargetFormat.CLAUDE: claude.VALIDATED_CLAUDE_VERSIONS,
        TargetFormat.CODEX: codex.VALIDATED_TARGET_VERSIONS,
        TargetFormat.OPENCODE: {"2.0.22", "2.0.23", "2.1.1"},
    }
    if artifact.source.cli_version not in known_sources.get(
        artifact.source.source_format, ()
    ) or artifact.target_cli_version not in known_targets.get(artifact.target_format, ()):
        return None, None, None
    policy = ProjectionPolicy()
    versions = PolicyVersions(
        policy.canonical_policy.algorithm,
        policy.policy_id,
        "native-audit-v1",
        "native-audit-v1",
        artifact.source.cli_version,
        artifact.target_cli_version,
    )
    context = ProjectionContext(
        artifact.target_format,
        artifact.target_cli_version,
        _profile_fingerprint(state, "target"),
        _digest(asdict(_native_namespace(state))),
    )
    request = state.built.request
    options = ConversionOptions(
        artifact.target_format,
        session_id=request.session_id,
        cwd=artifact.cwd,
        target_cli_version=artifact.target_cli_version,
        model_provider=request.model_provider,
        model=request.model,
        target_title=request.target_title,
        title_policy=request.title_policy,
    )
    source_context = None
    if (
        artifact.source.source_format is AgentFormat.OPENCODE
        and state.built.source_snapshot.source_path
        == Path(f"opencode:{artifact.source.session_id}")
    ):
        source_context = SourceVersionContext(
            AgentFormat.OPENCODE,
            artifact.source.cli_version,
            state.built.source_snapshot.source_path,
            state.built.source_snapshot.authority_sha256,
        )
    prepared = prepare_projection(
        state.built.source_snapshot,
        artifact,
        options=options,
        versions=versions,
        context=context,
        policy=policy,
        source_context=source_context,
    )
    return prepared, versions, context


def _lineage_versions(state):
    return state.projection_versions or PolicyVersions(
        "unverified-v1",
        "unverified-v1",
        "legacy-unverified-v1",
        "legacy-unverified-v1",
        _client_identifier(state.built.artifact.source.cli_version),
        _client_identifier(state.built.artifact.target_cli_version),
    )


def _canonical_snapshot_sha(observation, versions):
    from session_migrate.lineage import _encode

    value = {
        "prefix": json.loads(_encode(observation.source_prefix)),
        "versions": asdict(versions),
        "policy_sha256": observation.policy.sha256,
    }
    return hashlib.sha256(
        b"session-migrate/checkpoint-prefix/v2\0" + _canonical_json(value).encode()
    ).hexdigest()


def _source_identity(state):
    if state.built.runtime_context is not None:
        from session_migrate.handoff_operations import validate_runtime_context

        validate_runtime_context(state.built.runtime_context, fresh=True)
        return state.built.runtime_context.authority.identity
    if state.built.request.command != OperationKind.TRANSFER or state.built.source_snapshot is None:
        return None
    session = state.built.artifact.source
    root = state.built.source_root or state.built.request.source_home
    if root is None:
        root = (
            _source_root(state.built, state.source_environment)
            if session.source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}
            else default_target_home(session.source_format, environ=state.source_environment)
        )
    try:
        namespace = reserve_namespace(session.source_format, root, session.session_id)
        context = _bind_execution_context(
            namespace,
            session,
            native_path=session.source_path,
            environ=state.source_environment,
            client=state.built.request.source_cli,
            snapshot=state.built.source_snapshot,
            export_proof=state.built.source_export_proof,
        )
    except SessionMigrateError:
        return None
    return qualify_namespace(
        session, namespace, _profile_fingerprint(state, "source"), execution_context=context
    )


def _planned_references(state):
    path = str(state.built.output) if isinstance(state.built.output, Path) else None
    return (
        EvidenceReference("artifact", "planned", "unknown", path, state.artifact_sha256),
        EvidenceReference("manifest", "planned", "unknown", str(state.built.manifest), None),
    )


def _ready_record(state, operation_id):
    projection, versions = state.projection, _lineage_versions(state)
    source_observation = projection.source_observation if projection is not None else None
    target_observation = projection.artifact_observation if projection is not None else None
    source_known = (
        source_observation is not None
        and _effective_source_coverage(source_observation) is not Coverage.UNKNOWN
    )
    derived = state.built.artifact.transformation is not None
    target_known = (
        (target_observation is not None and projection.expectation_coverage is not Coverage.UNKNOWN)
        if not derived
        else True
    )
    source = SourceMetadata(
        _source_identity(state),
        state.built.artifact.source.source_sha256,
        state.source_witness,
        _canonical_snapshot_sha(source_observation, versions) if source_known else None,
        source_observation.source_prefix if source_known else None,
    )
    expected = (
        projection.expectation
        if derived
        else ProjectionExpectation(
            _canonical_snapshot_sha(target_observation, versions) if target_known else None,
            (),
            projection.expectation_coverage if projection is not None else Coverage.UNKNOWN,
            target_observation.source_prefix if target_known else None,
            projection.expected.relation.chain_sha256 if target_known else None,
        )
    )
    return OperationRecord(
        operation_id,
        source,
        _native_namespace(state),
        _profile_fingerprint(state, "target"),
        state.options_fingerprint,
        state.artifact_sha256,
        versions,
        expected,
        _planned_references(state),
        state.built.artifact.transformation,
        state.built.artifact.return_link,
    )


def _checkpoint(observation, identity, artifact_sha, versions, coverage):
    known = observation is not None and coverage is not Coverage.UNKNOWN
    prefix = observation.source_prefix if known else None
    return CheckpointRecord(
        identity,
        artifact_sha,
        _canonical_snapshot_sha(observation, versions) if known else None,
        (),
        prefix.closed_turn_count if prefix else 0,
        (),
        False,
        coverage,
        None,
        versions,
        prefix,
    )


def _effective_source_coverage(observation):
    if observation.coverage is Coverage.UNKNOWN or observation.fidelity is Coverage.UNKNOWN:
        return Coverage.UNKNOWN
    if (
        observation.coverage is not Coverage.VERIFIED
        or observation.fidelity is not Coverage.VERIFIED
    ):
        return Coverage.PARTIAL
    return Coverage.VERIFIED


def _completion_record(state, record, session):
    from session_migrate.canonical import CanonicalPolicy
    from session_migrate.native_audit import AuditPolicy, audit_snapshot, canonicalize_snapshot
    from session_migrate.projections import verify_projection

    snapshot = state.observed_captures[-1] if state.observed_captures else None
    native_path = (
        Path(state.built.output)
        if isinstance(state.built.output, Path)
        else Path(record.target_namespace.canonical_root)
        if session.source_format in _SHARED_DATABASE_SOURCES
        else session.source_path
    )
    context = _bind_execution_context(
        record.target_namespace,
        session,
        native_path=native_path,
        environ=state.target_environment,
        client=state.built.request.target_cli,
        snapshot=snapshot,
        export_proof=state.observed_export_proofs[-1] if state.observed_export_proofs else None,
    )
    identity = qualify_namespace(
        session,
        record.target_namespace,
        record.target_profile_fingerprint,
        native_path=native_path,
        execution_context=context,
    )
    if identity is None:
        raise SessionMigrateError("Observed native namespace cannot be qualified.")
    source_cp = target_cp = None
    if record.transformation is not None:
        from session_migrate.derived_projection import verify_derived_projection

        if snapshot is None or state.projection is None:
            raise SessionMigrateError("Derived target observation is unavailable.")
        proof, observed = verify_derived_projection(state.projection, snapshot)
        source_observed = state.projection.source_observation
        source_cp = _checkpoint(
            source_observed,
            record.source.identity,
            record.source.artifact_sha256,
            record.versions,
            _effective_source_coverage(source_observed),
        )
        target_cp = _checkpoint(
            observed,
            identity,
            snapshot.authority_sha256,
            record.versions,
            _effective_source_coverage(observed),
        )
        return CompletionRecord(
            identity, state.artifact_sha256, source_cp, target_cp, _planned_references(state), proof
        )
    if state.projection is not None:
        if snapshot is None:
            raise SessionMigrateError("Observed target capture is unavailable.")
        verification = verify_projection(
            state.projection, snapshot, context=state.projection_context
        )
        if not verification.semantic_matches or not verification.structure_matches:
            raise SessionMigrateError(
                "Observed target projection differs from its prepared expectation."
            )
        observed = canonicalize_snapshot(
            snapshot,
            audit_snapshot(snapshot, AuditPolicy(client_version=record.versions.target_client)),
            CanonicalPolicy(),
        )
        actual_coverage = _effective_source_coverage(observed)
        expected_coverage = record.expected_projection.coverage
        if expected_coverage is Coverage.VERIFIED and (
            actual_coverage is not Coverage.VERIFIED
            or not verification.relation_complete
            or not verification.preserves_content
            or state.projection.certificate is None
        ):
            raise SessionMigrateError(
                "Observed target does not sustain its prepared projection authority."
            )
        if expected_coverage is Coverage.PARTIAL and actual_coverage is Coverage.UNKNOWN:
            raise SessionMigrateError("Observed target projection authority became unknown.")
        source_cp = _checkpoint(
            state.projection.source_observation,
            record.source.identity,
            record.source.artifact_sha256,
            record.versions,
            _effective_source_coverage(state.projection.source_observation),
        )
        target_cp = _checkpoint(
            observed,
            identity,
            session.source_sha256,
            record.versions,
            record.expected_projection.coverage,
        )
    return CompletionRecord(
        identity, state.artifact_sha256, source_cp, target_cp, _planned_references(state)
    )


def _observed_references(state, session=None):
    references = []
    for reference in _planned_references(state):
        if reference.path is None:
            references.append(reference)
            continue
        path = Path(reference.path)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > 64 * 1024 * 1024
                ):
                    references.append(reference)
                    continue
                digest = hashlib.sha256()
                remaining = info.st_size
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    digest.update(block)
                    remaining -= len(block)
                after = os.fstat(stream.fileno())
                named = path.lstat()
                if (
                    remaining
                    or stream.read(1)
                    or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    )
                    or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                ):
                    references.append(reference)
                    continue
            references.append(
                EvidenceReference(
                    reference.kind, "observed", "available", reference.path, digest.hexdigest()
                )
            )
        except FileNotFoundError:
            references.append(
                EvidenceReference(reference.kind, "observed", "missing", reference.path, None)
            )
        except OSError:
            references.append(reference)
    if session is not None:
        references.append(
            EvidenceReference("native", "observed", "available", None, session.source_sha256)
        )
    return tuple(references)


def _open_lineage_store(state):
    from session_migrate.native_namespaces import ensure_migration_state_home

    if state.migration_state_home is None:
        raise SessionMigrateError("Native publication requires explicit migration state HOME.")
    ensure_migration_state_home(state.migration_state_home)
    directory = state.migration_state_home / "lineage-v1"
    return (
        LineageStore.open_existing(directory)
        if os.path.lexists(directory)
        else LineageStore.create_new(directory)
    )


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
    if (
        isinstance(prepared, PreparedOperation)
        and prepared._state is not None
        and prepared._state.built.request.command != OperationKind.CONVERT
    ):
        return _commit_native_operation(prepared, token, progress)
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
        result = _client_result(result, built.request, completed=True)
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


def _prepublication_guards(state, *, preflight=True):
    _verify_prepared_options(state)
    if preflight:
        built = state.built
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
    _verify_prepared_options(state)
    _read_prepared_source(state)
    _quick_publication_guards(state)


def _quick_publication_guards(state):
    from session_migrate.native_snapshots import CaptureProvenance, check_source_identity

    if (
        state.last_source_capture
        and state.last_source_capture[-1].provenance is CaptureProvenance.FILE
    ):
        check_source_identity(state.last_source_capture[-1])
    if (
        _client_epoch(state.built.request.source_cli),
        _client_epoch(state.built.request.target_cli),
    ) != tuple(
        revision[:-1] if revision is not None else None for revision in state.client_revisions
    ):
        raise SessionMigrateError("prepared client identity changed; prepare again")
    if (
        _root_epoch(state.source_root) != state.source_epoch
        or _root_epoch(state.built.artifact.cwd) != state.cwd_epoch
    ):
        raise SessionMigrateError("prepared source root or CWD changed; prepare again")
    if not _target_epoch_matches(
        state.target_root,
        state.target_epoch,
        database_file=state.built.artifact.target_format is TargetFormat.MASTRACODE,
    ):
        raise SessionMigrateError("prepared destination changed; prepare again")


def _raise_reconcile(
    state,
    operation_id,
    native_state,
    phase,
    reason,
    *,
    store=None,
    guard=None,
    session=None,
    runtime_uncertain=False,
):
    try:
        references = _observed_references(state, session)
    except Exception:
        references = _planned_references(state)
    receipt = ReconcileReceipt(
        1,
        operation_id,
        state.built.artifact.session_id,
        phase,
        reason,
        native_state,
        references,
        False,
    )
    if store is not None and guard is not None:
        with suppress(Exception):
            receipt = store.needs_reconcile(receipt, guard)
    if runtime_uncertain:
        from session_migrate.handoff_operations import RuntimePersistenceUncertainError

        raise RuntimePersistenceUncertainError(
            receipt, target_namespace=_native_namespace(state)
        ) from None
    raise OperationNeedsReconcileError(receipt, target_namespace=_native_namespace(state)) from None


def _runtime_title_guard(state, record, title_identity, title_store, title, receipt_path):
    if state.built.artifact.title_plan is None:
        return ()
    from session_migrate.reconciliation import _read_document

    plan = state.built.artifact.title_plan
    references = ()
    if plan.policy is TitlePolicy.LOCAL_ALIAS:
        alias = title_store.load(title_identity)
        if alias.alias != plan.requested or alias.proposal_id != record.operation_id:
            raise SessionMigrateError("Published local alias changed.")
        alias_path = title_store.path_for(title_identity)
        document, alias_sha = _read_document(alias_path)
        if document != alias.to_dict():
            raise SessionMigrateError("Published local alias changed while reading.")
        references = (
            EvidenceReference("receipt", "verified", "available", str(alias_path), alias_sha),
        )
    binding = TitleReceiptBinding(
        state.built.artifact.target_format,
        state.built.artifact.target_sha256,
        identity=title_identity,
        output_path=None,
        session_id=state.built.artifact.session_id,
        manifest_path=state.built.manifest,
        proposal_id=record.operation_id,
    )
    expected = title.to_dict()
    expected["binding"] = binding.to_dict()
    document, receipt_sha = _read_document(receipt_path)
    if document != expected:
        raise SessionMigrateError("Published title receipt changed.")
    return references + (
        EvidenceReference("receipt", "verified", "available", str(receipt_path), receipt_sha),
    )


def _complete_runtime_operation(
    state,
    record,
    completion,
    store,
    guard,
    namespace,
    title_identity,
    title_store,
    title,
    receipt_path,
):
    from session_migrate.handoff_operations import _held_coordinator
    from session_migrate.lineage import LineageError, _encode
    from session_migrate.native_snapshots import check_source_identity

    def fresh_guard():
        try:
            with _held_coordinator(store, guard, record, state.built.runtime_context):
                for _ in range(2):
                    _verify_prepared_options(state)
                    _read_prepared_source(state)
                    state.observed_captures.clear()
                    state.observed_export_proofs.clear()
                    current_session = _verify_installed_identity(state)
                    current = _completion_record(state, record, current_session)
                    references = _observed_references(
                        state, current_session
                    ) + _runtime_title_guard(
                        state, record, title_identity, title_store, title, receipt_path
                    )
                    if replace(current, references=references) != completion:
                        raise SessionMigrateError("Runtime completion evidence changed.")
                    _verify_published_namespace(state, title_identity, namespace)
                # Metadata reads above cannot replace either whole native capture.
                _read_prepared_source(state)
                state.observed_captures.clear()
                state.observed_export_proofs.clear()
                current_session = _verify_installed_identity(state)
                if (
                    replace(
                        _completion_record(state, record, current_session),
                        references=completion.references,
                    )
                    != completion
                ):
                    raise SessionMigrateError("Runtime native proof changed after metadata I/O.")
                _quick_publication_guards(state)
                if state.observed_captures and state.observed_captures[-1].identity is not None:
                    check_source_identity(state.observed_captures[-1])
        except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError):
            raise LineageError("proof_mismatch") from None

    return store.complete_reconciled(
        record.operation_id,
        hashlib.sha256(_encode(record)).hexdigest(),
        OperationState.VERIFYING,
        completion,
        guard,
        external_guard=fresh_guard,
    )


def _commit_native_operation(prepared, token, progress):
    state = None
    store = guard = session = None
    operation_id = None
    native_state = NativeState.NATIVE_NOT_STARTED
    phase, reason = "recording_intent", "intent_persistence_failed"
    gate_closed = ready = recording = False
    try:
        state, proposal = prepared._consume()
        operation_id = proposal.proposal_id
        token.check()
        _notify(progress, ProgressStage.PREFLIGHT)
        _prepublication_guards(state)
        title_store, planned_title_identity = _title_preflight(state)
        record = _ready_record(state, operation_id)
        LineageStore.preflight_metadata(record)
        recording = True
        store = _open_lineage_store(state)
        additional = (
            (record.source.identity.namespace,)
            if state.built.runtime_context is not None and record.source.identity is not None
            else ()
        )
        with store.lock_operation(
            operation_id, record.target_namespace, additional_namespaces=additional
        ) as guard:
            try:
                if state.built.runtime_context is not None:
                    _prepublication_guards(state, preflight=False)
                store.ready(record, guard)
                ready = True
                token.check()
                reason = "guards_changed"
                _prepublication_guards(state)
                store.check_guard(operation_id, guard, expected_state=OperationState.READY)
                _quick_publication_guards(state)
                token.check()
                token.begin_publication()
                gate_closed = True
                _notify(progress, ProgressStage.RECORDING_INTENT)
                reason = "intent_persistence_failed"
                store.begin_publication(operation_id, guard)
                reason = "guards_changed"
                _prepublication_guards(state)
                store.check_guard(operation_id, guard, expected_state=OperationState.PUBLISHING)
                _notify(progress, ProgressStage.INSTALLING)
                _quick_publication_guards(state)
                phase, reason = "installing", "native_result_unknown"
                native_state = NativeState.ATTEMPTED_UNKNOWN
                built = state.built
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
                native_state = NativeState.ACKNOWLEDGED
                phase, reason = "observing", "observation_failed"
                store.mark_verifying(operation_id, guard)
                _notify(progress, ProgressStage.VERIFYING)
                session = _verify_installed_identity(state)
                completion = _completion_record(state, record, session)
                namespace = _published_namespace(state)
                title_identity = (
                    planned_title_identity.bind_root()
                    if planned_title_identity is not None
                    else None
                )
                title = None
                receipt_path = None
                if built.artifact.title_plan is not None:
                    phase, reason = "finalizing", "finalization_failed"
                    title = TitleReceipt.pending(built.artifact.title_plan)
                    _verify_published_namespace(state, title_identity, namespace)
                    if built.artifact.title_plan.policy is TitlePolicy.NATIVE:
                        title = _observe_native_title(state, session)
                    else:
                        title_store.create(
                            title_identity,
                            built.artifact.title_plan.requested,
                            proposal_id=operation_id,
                            target_verified=True,
                        )
                        title = TitleReceipt.published_alias(built.artifact.title_plan)
                    _verify_published_namespace(state, title_identity, namespace)
                    receipt_path = title_receipt_path(built.manifest)
                    binding = TitleReceiptBinding(
                        built.artifact.target_format,
                        built.artifact.target_sha256,
                        identity=title_identity,
                        output_path=None,
                        session_id=built.artifact.session_id,
                        manifest_path=built.manifest,
                        proposal_id=operation_id,
                    )
                    write_title_receipt(
                        receipt_path, title, binding=binding, alias_store=title_store
                    )
                phase, reason = "finalizing", "finalization_failed"
                references = _observed_references(state, session)
                _verify_prepared_options(state)
                _read_prepared_source(state)
                previous_target_sha = session.source_sha256
                state.observed_captures.clear()
                state.observed_export_proofs.clear()
                session = _verify_installed_identity(state)
                if session.source_sha256 != previous_target_sha:
                    raise SessionMigrateError("Observed target changed during local finalization.")
                completion = _completion_record(state, record, session)
                _verify_published_namespace(state, title_identity, namespace)
                store.check_guard(operation_id, guard, expected_state=OperationState.VERIFYING)
                _quick_publication_guards(state)
                if state.observed_captures and state.observed_captures[-1].identity is not None:
                    from session_migrate.native_snapshots import check_source_identity

                    check_source_identity(state.observed_captures[-1])
                completion = replace(completion, references=references)
                if state.built.runtime_context is None:
                    lineage_receipt = store.complete(operation_id, completion, guard)
                else:
                    completion = replace(
                        completion,
                        references=completion.references
                        + _runtime_title_guard(
                            state, record, title_identity, title_store, title, receipt_path
                        ),
                    )
                    lineage_receipt = _complete_runtime_operation(
                        state,
                        record,
                        completion,
                        store,
                        guard,
                        namespace,
                        title_identity,
                        title_store,
                        title,
                        receipt_path,
                    )
                result = content_free_result(
                    built.artifact,
                    output_path=built.output,
                    manifest_path=built.manifest,
                    dry_run=False,
                )
                result = _client_result(result, built.request, completed=True)
                _notify(progress, ProgressStage.COMPLETED)
                return TransferReceipt(
                    proposal,
                    "completed",
                    result,
                    title=title,
                    title_receipt=receipt_path,
                    target_identity=title_identity.to_dict() if title_identity else None,
                    lineage_receipt=lineage_receipt,
                )
            except OperationCancelledError:
                if ready and not gate_closed:
                    store.cancel_ready(operation_id, guard)
                _notify(progress, ProgressStage.CANCELLED)
                raise
            except NativeImportPartialError:
                native_state = NativeState.ACKNOWLEDGED
                with suppress(Exception):
                    session = _verify_installed_identity(state)
                _notify(progress, ProgressStage.NEEDS_RECONCILE)
                _raise_reconcile(
                    state,
                    operation_id,
                    native_state,
                    "finalizing",
                    "finalization_failed",
                    store=store,
                    guard=guard,
                    session=session,
                )
            except Exception as error:
                if ready and not gate_closed:
                    with suppress(Exception):
                        store.fail_ready(operation_id, guard)
                    _notify(progress, ProgressStage.FAILED)
                    raise
                _notify(progress, ProgressStage.NEEDS_RECONCILE)
                _raise_reconcile(
                    state,
                    operation_id,
                    native_state,
                    phase,
                    reason,
                    store=store,
                    guard=guard,
                    session=session,
                    runtime_uncertain=(
                        state.built.runtime_context is not None
                        and (
                            store._guarded_persist_renamed
                            or getattr(error, "code", None) == "persistence_uncertain"
                        )
                    ),
                )
    except (OperationCancelledError, OperationNeedsReconcileError):
        raise
    except Exception:
        if (
            state is not None
            and operation_id is not None
            and recording
            and (store is None or not ready)
        ):
            _notify(progress, ProgressStage.NEEDS_RECONCILE)
            _raise_reconcile(state, operation_id, native_state, phase, reason)
        _notify(progress, ProgressStage.FAILED)
        raise
    finally:
        if store is not None:
            store.close()
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
    migration_state_home: Path | None = None,
    profile_context: Mapping[str, str] | None = None,
) -> OperationResult | TransferReceipt:
    """Snapshot target/source environments independently and use validated adapters.

    Omitted source_environ preserves the legacy single-environment contract.
    """

    if (
        request.target_title is not None
        or request.require_native_title
        or (
            request.command in {OperationKind.IMPORT, OperationKind.TRANSFER}
            and not request.dry_run
        )
    ):
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
                migration_state_home=migration_state_home,
                profile_context=profile_context,
            )
            if request.dry_run and request.command != OperationKind.CONVERT:
                token.check()
                proposal = proposal_metadata(prepared)
                prepared.discard()
                return TransferReceipt(proposal, "ready", proposal.to_dict()["legacy_result"])
            receipt = _commit_operation(prepared, token, progress)
            if request.target_title is None and not request.require_native_title:
                return OperationResult(
                    **receipt.legacy_result, lineage_receipt=receipt.lineage_receipt
                )
            return receipt
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
    if (
        request.target_format is TargetFormat.CLAUDE
        and request.command is not OperationKind.CONVERT
        and values.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    ):
        raise SessionMigrateError(
            "Claude target custom project namespace is unsupported: "
            "CLAUDE_CODE_PROJECT_DIR_NAME must be unset for verified publication."
        )
    # A connected destination never redirects source discovery or another
    # operation implicitly through inherited process state.
    source_values.pop("SESSION_MIGRATE_OPENCODE_SERVER", None)
    values.pop("SESSION_MIGRATE_OPENCODE_SERVER", None)
    source_values.pop("SESSION_MIGRATE_OPENCODE_BINDING", None)
    values.pop("SESSION_MIGRATE_OPENCODE_BINDING", None)
    if request.target_server and request.command is not OperationKind.CONVERT:
        values["SESSION_MIGRATE_OPENCODE_SERVER"] = request.target_server
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
    return OperationResult(**_client_result(result, request, completed=not dry_run))


@dataclass(frozen=True, slots=True, repr=False)
class _OperationBuild:
    request: OperationRequest
    artifact: ConversionArtifact
    output: Path | str
    manifest: Path
    home: Path | None
    source_root: Path | None
    dry_run: bool
    source_snapshot: NativeSnapshot | None = None
    source_export_proof: Any = None
    runtime_context: Any = None
    portable_emission: Any = None


def _resolve_source(
    request: OperationRequest,
    source_environ: Mapping[str, str],
    max_source_bytes: int | None,
    expected_root: Path | None = None,
    on_capture: Callable[[NativeSnapshot], None] | None = None,
    on_native_observation: Callable[[Any], None] | None = None,
    catalog_opener: Callable[[Path], Catalog] | None = None,
    readonly_catalog: bool = False,
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
            catalog_path = request.catalog or _catalog_default(source_environ)
            opener = (
                Catalog.open_readonly_existing if readonly_catalog else (catalog_opener or Catalog)
            )
            with opener(catalog_path) as catalog:
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
                    **(
                        {"on_native_observation": on_native_observation}
                        if on_native_observation is not None
                        else {}
                    ),
                    **(
                        {"on_capture": on_capture}
                        if source_format == AgentFormat.OPENCODE and on_capture is not None
                        else {}
                    ),
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
                    **(
                        {"on_native_observation": on_native_observation}
                        if on_native_observation is not None
                        else {}
                    ),
                    **(
                        {"on_capture": on_capture}
                        if source_format == AgentFormat.OPENCODE and on_capture is not None
                        else {}
                    ),
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
                    source_path,
                    source_format,
                    source_root,
                    source_environ,
                    max_source_bytes,
                    on_capture,
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
            request.path, source_format, None, source_environ, max_source_bytes, on_capture
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
    catalog_opener: Callable[[Path], Catalog] | None = None,
    runtime_context=None,
) -> _OperationBuild:
    if runtime_context is not None:
        from session_migrate.handoff_operations import validate_runtime_context

        validate_runtime_context(runtime_context, fresh=True)
    captures = []
    export_proofs = []
    session, target_format, source_root = _resolve_source(
        request,
        source_environ,
        max_source_bytes,
        on_capture=captures.append
        if observe_target and request.command != OperationKind.CONVERT
        else None,
        on_native_observation=export_proofs.append
        if observe_target and request.command != OperationKind.CONVERT
        else None,
        catalog_opener=catalog_opener,
        readonly_catalog=runtime_context is not None,
    )
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
    if runtime_context is not None and runtime_context.derived_input is not None:
        from session_migrate.conversion import EmitContext, _utc_now, emit_portable
        from session_migrate.handoff_operations import validate_runtime_context
        from session_migrate.titles import plan_target_title

        validate_runtime_context(runtime_context, fresh=True)
        if (
            request.cwd is None
            or request.target_cli_version is None
            or request.model_provider is None
            or request.model is None
        ):
            raise SessionMigrateError("Summary requires explicit CWD, version, model and provider.")
        if session.source_sha256 != runtime_context.authority.authority_sha256:
            raise SessionMigrateError("Summary native authority changed.")
        title_plan = (
            plan_target_title(
                target_format,
                requested=request.target_title,
                cli_version=target_cli_version,
                policy=request.title_policy,
                source_title=None,
            )
            if request.target_title is not None
            else None
        )
        target_id = request.session_id
        if target_format is TargetFormat.OPENCODE:
            from session_migrate.formats import opencode

            target_id = opencode.session_id_from_uuid(target_id)
        emit_context = EmitContext(
            target_format,
            target_id,
            request.cwd.resolve(),
            target_cli_version,
            _utc_now(),
            request.model_provider,
            request.model,
            title_plan.native_effective if title_plan is not None else None,
        )
        emission = emit_portable(runtime_context.derived_input.events, emit_context)
        artifact = ConversionArtifact(
            session,
            target_format,
            target_id,
            emit_context.cwd,
            target_cli_version,
            emit_context.timestamp,
            emission.native_bytes,
            emission.native_record_count,
            dict(emission.dropped),
            (
                {
                    "code": "derived_handoff",
                    "message": "Historical transcript omitted; reviewed USER handoff.",
                },
            ),
            title_plan,
            runtime_context.transformation,
            runtime_context.authority.identity,
            runtime_context.return_link,
        )
    else:
        emission = None
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
        if runtime_context is not None:
            artifact = replace(artifact, return_link=runtime_context.return_link)
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
        request,
        artifact,
        output_path,
        manifest_path,
        home,
        source_root,
        dry_run,
        captures[0] if captures else None,
        export_proofs[0] if export_proofs else None,
        runtime_context,
        emission,
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
    on_capture: Callable[[NativeSnapshot], None] | None = None,
    on_native_observation: Callable[[Any], None] | None = None,
) -> Session:
    if on_capture is not None:
        from session_migrate.inspection import detect_path_format
        from session_migrate.jsonl import DEFAULT_MAX_TOTAL_BYTES

        selected_format = source_format or detect_path_format(path)
        if selected_format in {AgentFormat.CLAUDE, AgentFormat.CODEX, AgentFormat.OPENCODE}:
            try:
                snapshot = capture_source(
                    SourceCaptureContext(selected_format, path),
                    byte_limit=max_source_bytes or DEFAULT_MAX_TOTAL_BYTES,
                )
            except SessionMigrateError as error:
                if (
                    max_source_bytes is not None
                    and str(error) == "native capture exceeds the byte safety limit"
                ):
                    raise SourceReadLimitError(
                        "Source exceeds the interactive read budget."
                    ) from None
                raise
            session = snapshot.parse()
            on_capture(snapshot)
            return session
    if max_source_bytes is None:
        session = load_session(path, source_format)
    else:
        from session_migrate.inspection import detect_path_format
        from session_migrate.profiles import load_bounded_source

        source_format = source_format or detect_path_format(path)
        session = load_bounded_source(
            path,
            source_format,
            root=root or _physical_source_root(path, source_format),
            max_source_bytes=max_source_bytes,
            environ=environ,
        )
    if on_native_observation is not None:
        from session_migrate.conversion import _publish_export_proof

        _publish_export_proof(session, path, None, {}, on_native_observation)
    return session


def _load_shared_database_source(
    source_format: AgentFormat,
    session_id: str,
    source_home: Path,
    on_native_observation: Callable[[Any], None] | None = None,
) -> Session:
    if source_format == AgentFormat.HERMES:
        return load_hermes_session(
            session_id,
            source_home=source_home,
            **(
                {"on_native_observation": on_native_observation}
                if on_native_observation is not None
                else {}
            ),
        )
    if source_format == AgentFormat.MASTRACODE:
        return load_mastracode_session(
            session_id,
            source_home=source_home,
            **(
                {"on_native_observation": on_native_observation}
                if on_native_observation is not None
                else {}
            ),
        )
    if source_format == AgentFormat.DEVIN:
        return load_devin_session(
            session_id,
            source_home=source_home,
            **(
                {"on_native_observation": on_native_observation}
                if on_native_observation is not None
                else {}
            ),
        )
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
