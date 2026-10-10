"""Typed orchestration shared by command-line and interactive clients."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

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
    SessionMigrateError,
)
from session_migrate.model import AgentFormat, Session, TargetFormat

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
            if self._claimed:
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", OperationKind(self.command))
        if self.source_format is not None:
            object.__setattr__(self, "source_format", AgentFormat(self.source_format))
        if self.target_format is not None:
            object.__setattr__(self, "target_format", TargetFormat(self.target_format))


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
        from copy import deepcopy

        return {field.name: deepcopy(getattr(self, field.name)) for field in fields(self)}


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
) -> OperationResult:
    """Snapshot target/source environments independently and use validated adapters.

    Omitted source_environ preserves the legacy single-environment contract.
    """

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


def _execute(
    request: OperationRequest,
    environ: Mapping[str, str],
    progress: ProgressCallback | None,
    source_environ: Mapping[str, str],
    cancellation: CancellationToken | None,
    prepublication_check: Callable[[], None] | None,
    max_source_bytes: int | None,
) -> OperationResult:
    if request.command != OperationKind.TRANSFER and (
        request.path is None or request.target_format is None
    ):
        raise SessionMigrateError("convert/import require a source path and target format")
    if request.command == OperationKind.CONVERT and request.output is None:
        raise SessionMigrateError("convert requires an output path")
    source_root = None
    source_options = {"max_source_bytes": max_source_bytes} if max_source_bytes is not None else {}
    target_cli_version = request.target_cli_version
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
        and not request.target_cli_version
    ):
        target_cli_version = detect_opencode_version(request.target_cli, environ=environ)
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
    from session_migrate.profiles import load_bounded_source

    return load_bounded_source(
        path,
        source_format,
        root=root or path.resolve().parent,
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
