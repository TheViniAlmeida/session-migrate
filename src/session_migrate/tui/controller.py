"""UI-independent worker coordination; all catalog connections are worker-local."""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from session_migrate.catalog import Catalog
from session_migrate.errors import (
    NativeImportPartialError,
    OperationCancelledError,
    SessionMigrateError,
    SourceReadLimitError,
)
from session_migrate.model import AgentFormat
from session_migrate.operations import (
    CancellationToken,
    OperationRequest,
    OperationResult,
    ProgressEvent,
)
from session_migrate.preview import PreviewKey, PreviewRequest, PreviewService
from session_migrate.profiles import (
    Profile,
    ProfileSession,
    ProfileStore,
    SessionRef,
    list_profile_sessions,
    profile_from_catalog_root,
    read_session,
    resolve_source,
    run_profile_operation,
    source_read_paths,
)


class SourceChangedError(SessionMigrateError):
    """Selected authoritative content no longer matches the reviewed source."""


@dataclass(frozen=True, slots=True)
class JobResult:
    generation: int
    kind: str
    value: Any = None
    error: str | None = None
    reconcile_id: str | None = None


class Worker:
    """One daemon job, no queued work and no exception/body logging."""

    def __init__(self):
        self._slot = threading.BoundedSemaphore(1)
        self.results: queue.Queue[JobResult] = queue.Queue(maxsize=1)

    def submit(self, generation: int, kind: str, work: Callable[[], Any]) -> bool:
        if not self._slot.acquire(blocking=False):
            return False

        def run():
            try:
                result = JobResult(generation, kind, work())
            except NativeImportPartialError as exc:
                result = JobResult(
                    generation,
                    kind,
                    error=(
                        "Import completed; verification needs attention. Do not repeat the import."
                    ),
                    reconcile_id=exc.session_id,
                )
            except SourceChangedError:
                result = JobResult(
                    generation,
                    kind,
                    error="Source changed. Return to the destination step and prepare again.",
                )
            except SourceReadLimitError:
                result = JobResult(
                    generation,
                    kind,
                    error=(
                        "Source exceeds the interactive 8 MiB read budget. "
                        "Use the CLI for larger sessions."
                    ),
                )
            except OperationCancelledError:
                result = JobResult(generation, kind, error="Cancelled before installation.")
            except Exception:
                result = JobResult(
                    generation,
                    kind,
                    error=(
                        "Action failed. Check the selected profile, "
                        "session availability and options."
                    ),
                )
            self.results.put(result)

        threading.Thread(target=run, name="session-tui-job", daemon=True).start()
        return True

    def poll(self) -> JobResult | None:
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            return None
        self._slot.release()
        return result


@dataclass(frozen=True, slots=True)
class Draft:
    nonce: str
    source: Profile
    ref: SessionRef
    target: Profile
    request: OperationRequest
    revision: str
    result: OperationResult
    options: str


def fingerprint(
    source: Profile, ref: SessionRef, target: Profile, request: OperationRequest
) -> str:
    return sha256(
        repr(
            (
                source.fingerprint,
                ref,
                target.fingerprint,
                request.cwd,
                request.target_cli_version,
                request.model_provider,
                request.model,
                request.session_id,
            )
        ).encode()
    ).hexdigest()


class Controller:
    """Bounded profile/catalog/preview/transfer services for a single local user."""

    def __init__(
        self,
        catalog: Path,
        *,
        environ: Mapping[str, str] | None = None,
        source_bytes: int = 8 * 1024 * 1024,
    ):
        self.catalog = catalog
        self.environ = dict(os.environ if environ is None else environ)
        self.source_bytes = source_bytes
        self.preview = PreviewService(max_workers=1)
        self._draft_lock = threading.Lock()
        self._active_draft: tuple[str, str] | None = None

    def profiles(self, storage: Path | None = None) -> list[Profile]:
        if storage is not None:
            return list(ProfileStore(storage).load())
        with Catalog(self.catalog) as catalog:
            roots = catalog.roots(enabled_only=True)
        # Native CLI/env choices are supplied explicitly in the form before reading.
        return [
            Profile(
                f"root-{root.id}",
                f"{root.format}: {Path(root.path).name}",
                root.format,
                Path(root.path),
            )
            for root in roots
        ]

    def bind_source(self, selected: Profile) -> Profile:
        with Catalog(self.catalog) as catalog:
            roots = [
                root
                for root in catalog.roots(enabled_only=True)
                if root.format == selected.format.value and Path(root.path) == selected.root
            ]
        if len(roots) != 1:
            raise SessionMigrateError("Source profile root is not registered.")
        return profile_from_catalog_root(
            roots[0],
            profile_id=selected.id,
            name=selected.name,
            agent_format=selected.format,
            cli=selected.cli,
            environ=selected.environ,
        )

    def list(
        self,
        selected: Profile,
        query: str,
        offset: int,
        lifecycle: str = "",
        status: str = "",
        since: str = "",
        until: str = "",
    ) -> list[ProfileSession]:
        with Catalog(self.catalog) as catalog:
            return list_profile_sessions(
                catalog,
                selected,
                query=query or None,
                limit=50,
                offset=offset,
                lifecycles=(lifecycle,) if lifecycle else (),
                statuses=(status,) if status else (),
                since=since or None,
                until=until or None,
            )

    def _metadata_revision(self, selected: Profile, ref: SessionRef) -> str:
        with Catalog(self.catalog) as catalog:
            source = resolve_source(catalog, selected, ref)
        if source.path is None:
            names = {
                AgentFormat.OPENCODE: "opencode.db",
                AgentFormat.KILO: "kilo.db",
                AgentFormat.HERMES: "state.db",
                AgentFormat.MASTRACODE: "mastra.db",
                AgentFormat.DEVIN: "sessions.db",
            }
            path = (
                selected.root if selected.root.is_file() else selected.root / names[selected.format]
            )
        else:
            path = source.path
        paths = source_read_paths(selected.format, path, selected.root)
        witnesses = [("profile", selected.fingerprint)]
        total = 0
        for candidate in dict.fromkeys(paths):
            if not candidate.exists():
                witnesses.append((str(candidate), None))
                continue
            if candidate.is_symlink() or not (candidate.is_file() or candidate.is_dir()):
                raise SessionMigrateError("Source path is unavailable.")
            info = candidate.stat()
            total += info.st_size if candidate.is_file() else 0
            if total > self.source_bytes and selected.format not in {
                AgentFormat.OPENCODE,
                AgentFormat.KILO,
            }:
                raise SourceReadLimitError("Source exceeds the interactive read budget.")
            witnesses.append(
                (
                    str(candidate),
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
        return sha256(json.dumps(witnesses).encode()).hexdigest()

    def _native_snapshot(self, source: Profile, ref: SessionRef):
        # Official exports may modify WAL/SHM themselves. Their selected-session
        # export digest is the authority witness; store stats remain byte/identity guards.
        self._metadata_revision(source, ref)
        with Catalog(self.catalog) as catalog:
            session = read_session(
                catalog, source, ref, base_environ=self.environ, max_source_bytes=self.source_bytes
            )
        self._metadata_revision(source, ref)
        revision = sha256(
            json.dumps(
                [
                    source.fingerprint,
                    str(source.root),
                    ref.native_id,
                    session.source_sha256,
                    session.cli_version,
                ]
            ).encode()
        ).hexdigest()
        return revision, session

    def revision(self, source: Profile, ref: SessionRef) -> str:
        if source.format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
            return self._native_snapshot(source, ref)[0]
        return self._metadata_revision(source, ref)

    def _read(self, source: Profile, ref: SessionRef, revision: str):
        if self.revision(source, ref) != revision:
            raise SourceChangedError("Source changed.")
        with Catalog(self.catalog) as catalog:
            session = read_session(
                catalog, source, ref, base_environ=self.environ, max_source_bytes=self.source_bytes
            )
        if self.revision(source, ref) != revision:
            raise SourceChangedError("Source changed during read.")
        return session

    def start_preview(
        self, source: Profile, ref: SessionRef, *, expand_tools: bool = False
    ) -> PreviewRequest:
        def resolve():
            if source.format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                return self._native_snapshot(source, ref)
            revision = self.revision(source, ref)
            return revision, self._read(source, ref, revision)

        return self.preview.request_authoritative(
            PreviewKey(source.format, source.id, str(source.root), ref.native_id),
            resolve,
            timeout=10.0,
            expand_tools=expand_tools,
        )

    def prepare(
        self,
        source: Profile,
        ref: SessionRef,
        target: Profile,
        request: OperationRequest,
        cancellation: CancellationToken,
    ) -> Draft:
        revision = self.revision(source, ref)
        reserved = replace(
            request,
            session_id=request.session_id or str(uuid.uuid4()),
            dry_run=True,
            cwd=request.cwd.resolve() if request.cwd is not None else None,
        )
        with Catalog(self.catalog) as catalog:
            result = run_profile_operation(
                catalog,
                source,
                ref,
                target,
                reserved,
                base_environ=self.environ,
                cancellation=cancellation,
                max_source_bytes=self.source_bytes,
            )
        if revision != self.revision(source, ref):
            raise SourceChangedError("Source changed during preparation.")
        nonce = uuid.uuid4().hex
        options = fingerprint(source, ref, target, reserved)
        with self._draft_lock:
            self._active_draft = (nonce, options)
        return Draft(
            nonce,
            source,
            ref,
            target,
            reserved,
            revision,
            result,
            options,
        )

    def commit(
        self,
        draft: Draft,
        cancellation: CancellationToken,
        progress: Callable[[ProgressEvent], None],
    ) -> OperationResult:
        with self._draft_lock:
            if (draft.nonce, draft.options) != self._active_draft:
                raise SessionMigrateError("Confirmation already consumed.")
            self._active_draft = None
        if fingerprint(draft.source, draft.ref, draft.target, draft.request) != draft.options:
            raise SessionMigrateError("Prepared options changed; prepare again.")
        if self.revision(draft.source, draft.ref) != draft.revision:
            raise SourceChangedError("Source changed; prepare again.")
        with Catalog(self.catalog) as catalog:
            return run_profile_operation(
                catalog,
                draft.source,
                draft.ref,
                draft.target,
                replace(draft.request, dry_run=False),
                base_environ=self.environ,
                cancellation=cancellation,
                progress=progress,
                prepublication_check=lambda: self._validate_revision(draft),
                max_source_bytes=self.source_bytes,
            )

    def _validate_revision(self, draft: Draft) -> None:
        if self.revision(draft.source, draft.ref) != draft.revision:
            raise SourceChangedError("Source changed; prepare again.")
