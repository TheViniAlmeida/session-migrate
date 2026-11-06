"""UI-independent worker coordination; all catalog connections are worker-local."""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from session_migrate.catalog import Catalog
from session_migrate.continuity import (
    ContinuityAssessmentV1,
    ContinuityAssessmentV2,
    ContinuitySelector,
    LineageInspectionV1,
    assess_continuity,
    inspect_lineage,
)
from session_migrate.continuity_bindings import EXECUTION_ENVIRONMENT, PUBLIC_ENVIRONMENT
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import (
    NativeImportPartialError,
    OperationCancelledError,
    OperationNeedsReconcileError,
    PublishedArtifactPartialError,
    SessionMigrateError,
    SourceReadLimitError,
)
from session_migrate.lineage import CompletionReceipt, LineageStore, RecoveryOperation
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import (
    SourceCaptureContext,
    capture_readable_source,
    check_source_identity,
)
from session_migrate.operations import (
    CancellationToken,
    OperationRequest,
    OperationResult,
    PreparedOperation,
    ProgressEvent,
    TransferProposal,
    TransferReceipt,
    commit_operation,
    proposal_metadata,
)
from session_migrate.preview import PreviewKey, PreviewRequest, PreviewService
from session_migrate.profiles import (
    Profile,
    ProfileSession,
    ProfileStore,
    SessionRef,
    list_profile_sessions,
    local_alias_for_session,
    prepare_profile_operation,
    profile_from_catalog_root,
    read_session,
    resolve_source,
    source_read_paths,
)


class SourceChangedError(SessionMigrateError):
    """Selected authoritative content no longer matches the reviewed source."""


class ReviewedReadLimitError(SourceReadLimitError):
    def __init__(self, byte_limit: int):
        super().__init__(
            f"Reviewed source exceeds the selected {byte_limit}-byte read budget. "
            "Use the CLI for larger sessions."
        )


class ContinuityReadError(SessionMigrateError):
    def __init__(self, code: str):
        self.code = (
            code
            if code in {"state_home_unavailable", "invalid_page", "read_cancelled"}
            else "read_unavailable"
        )
        super().__init__(f"Continuity read: {self.code}.")


@dataclass(frozen=True, slots=True, repr=False)
class OperationPage:
    operations: tuple[RecoveryOperation, ...]
    offset: int
    total_count: int
    reason: str

    @property
    def has_next(self):
        return self.offset + len(self.operations) < self.total_count


@dataclass(frozen=True, slots=True, repr=False)
class ContinuityDetail:
    inspection: LineageInspectionV1
    assessment: ContinuityAssessmentV1 | ContinuityAssessmentV2 | None = None
    profile_context: str | None = None
    context_reason: str | None = None

    @property
    def update_available(self):
        return False


@dataclass(frozen=True, slots=True)
class JobResult:
    generation: int
    kind: str
    value: Any = None
    error: str | None = None
    reconcile_id: str | None = None
    reconcile_manifest: str | None = None
    reconcile_root: str | None = None
    reconcile_output: str | None = None
    reconcile_stage: str | None = None
    publication_kind: str | None = None
    reconcile_native_state: str | None = None
    reconcile_references: tuple = ()


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
            except OperationNeedsReconcileError as exc:
                result = JobResult(
                    generation,
                    kind,
                    error=(
                        "Operation requires reconciliation. Inspect its reserved target "
                        "and evidence; do not repeat the action."
                    ),
                    reconcile_id=exc.session_id,
                    reconcile_manifest=exc.manifest,
                    reconcile_root=exc.root,
                    reconcile_output=exc.output,
                    reconcile_stage=exc.stage,
                    publication_kind="native operation",
                    reconcile_native_state=exc.native_state.value,
                    reconcile_references=exc.receipt.references,
                )
            except (NativeImportPartialError, PublishedArtifactPartialError) as exc:
                result = JobResult(
                    generation,
                    kind,
                    error=(
                        "Publication completed; verification needs attention. "
                        "Do not repeat the action."
                    ),
                    reconcile_id=exc.session_id,
                    reconcile_manifest=exc.manifest,
                    reconcile_root=getattr(exc, "root", None),
                    reconcile_output=getattr(exc, "output", None),
                    reconcile_stage=exc.stage,
                    publication_kind="native import" if exc.import_succeeded else "converted file",
                )
            except SourceChangedError:
                result = JobResult(
                    generation,
                    kind,
                    error="Source changed. Return to the destination step and prepare again.",
                )
            except ReviewedReadLimitError as exc:
                result = JobResult(generation, kind, error=str(exc))
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
class SessionRow(ProfileSession):
    local_alias: str | None = None
    alias_status: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class Draft:
    nonce: str
    source: Profile
    ref: SessionRef
    target: Profile
    request: OperationRequest
    revision: str
    result: OperationResult
    options: str
    prepared: PreparedOperation = field(repr=False)
    proposal: TransferProposal = field(repr=False)


def fingerprint(
    source: Profile,
    ref: SessionRef,
    target: Profile,
    request: OperationRequest,
    *,
    source_bytes: int | None = None,
) -> str:
    values = (
        source.fingerprint,
        ref,
        target.fingerprint,
        request.cwd,
        request.target_cli_version,
        request.model_provider,
        request.model,
        request.session_id,
        request.target_title,
        request.title_policy,
        request.require_native_title,
    )
    if request.reviewed_portability:
        values += ("reviewed_portability", True, "source_bytes", source_bytes)
    return sha256(repr(values).encode()).hexdigest()


class Controller:
    """Bounded profile/catalog/preview/transfer services for a single local user."""

    def __init__(
        self,
        catalog: Path,
        *,
        environ: Mapping[str, str] | None = None,
        source_bytes: int | None = None,
    ):
        self.catalog = catalog
        self.environ = dict(os.environ if environ is None else environ)
        if source_bytes is not None and (type(source_bytes) is not int or source_bytes <= 0):
            raise SessionMigrateError("Source read budget must be a positive integer")
        self._explicit_source_bytes = source_bytes is not None
        self.source_bytes = 8 * 1024 * 1024 if source_bytes is None else source_bytes
        self.preview = PreviewService(max_workers=1)
        self._draft_lock = threading.Lock()
        self._active_draft: tuple[str, str] | None = None
        self._active_prepared: PreparedOperation | None = None
        # This is the application's BASE runtime, captured before a destination
        # profile overrides HOME/XDG. Resolving it has no filesystem write effect.
        self._continuity_environment = {
            key: value
            for key, value in self.environ.items()
            if key in PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT
        }
        try:
            self.continuity_store_path = (
                default_migration_state_home(environ=self._continuity_environment) / "lineage-v1"
            )
        except (SessionMigrateError, OSError, ValueError):
            self.continuity_store_path = None
        self._completed_profiles: dict[str, tuple[Profile, ...]] = {}

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
        if selected.client == "openchamber":
            raise SessionMigrateError("OpenChamber profiles are destinations only")
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
            page = list_profile_sessions(
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
            rows = []
            for item in page:
                alias, alias_status = None, None
                if item.ref and item.entry.status in {"candidate", "validated"}:
                    try:
                        alias = local_alias_for_session(
                            catalog,
                            selected,
                            item.ref,
                            base_environ=self.environ,
                        )
                    except (OSError, SessionMigrateError):
                        alias_status = "Local alias unavailable."
                rows.append(
                    SessionRow(
                        item.profile_id,
                        item.entry,
                        item.ref,
                        item.ordering_source,
                        item.started_at_source,
                        alias,
                        alias_status,
                    )
                )
            return rows

    def source_limit(self, *, reviewed_portability: bool = False) -> int:
        if type(reviewed_portability) is not bool:
            raise SessionMigrateError("Reviewed portability must be boolean")
        if (
            reviewed_portability
            and not self._explicit_source_bytes
            and self.source_bytes == 8 * 1024 * 1024
        ):
            return 64 * 1024 * 1024
        return self.source_bytes

    def _metadata_revision(
        self, selected: Profile, ref: SessionRef, *, reviewed_portability: bool = False
    ) -> str:
        limit = self.source_limit(reviewed_portability=reviewed_portability)
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
        try:
            paths = source_read_paths(selected.format, path, selected.root, max_source_bytes=limit)
        except SourceReadLimitError:
            if reviewed_portability:
                raise ReviewedReadLimitError(limit) from None
            raise
        witnesses = [("profile", selected.fingerprint)]
        if reviewed_portability:
            witnesses.append(("reviewed_source_read_limit", limit))
        total = 0
        for candidate in dict.fromkeys(paths):
            if not candidate.exists():
                witnesses.append((str(candidate), None))
                continue
            if candidate.is_symlink() or not (candidate.is_file() or candidate.is_dir()):
                raise SessionMigrateError("Source path is unavailable.")
            info = candidate.stat()
            total += info.st_size if candidate.is_file() else 0
            if total > limit and selected.format not in {
                AgentFormat.OPENCODE,
                AgentFormat.KILO,
            }:
                if reviewed_portability:
                    raise ReviewedReadLimitError(limit)
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

    def revision(
        self, source: Profile, ref: SessionRef, *, reviewed_portability: bool = False
    ) -> str:
        if type(reviewed_portability) is not bool:
            raise SessionMigrateError("Reviewed portability must be boolean")
        if reviewed_portability:
            if source.format is not AgentFormat.CLAUDE:
                raise SessionMigrateError("Reviewed preview requires compacted Claude 2.1.309")
            return self._metadata_revision(source, ref, reviewed_portability=True)
        if source.format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
            return self._native_snapshot(source, ref)[0]
        return self._metadata_revision(source, ref)

    def _read(
        self, source: Profile, ref: SessionRef, revision: str, *, reviewed_portability: bool = False
    ):
        if type(reviewed_portability) is not bool:
            raise SessionMigrateError("Reviewed portability must be boolean")
        if reviewed_portability and source.format is not AgentFormat.CLAUDE:
            raise SessionMigrateError("Reviewed preview requires compacted Claude 2.1.309")
        mode = {"reviewed_portability": True} if reviewed_portability else {}
        if self.revision(source, ref, **mode) != revision:
            raise SourceChangedError("Source changed.")
        if reviewed_portability:
            with Catalog(self.catalog) as catalog:
                selected = resolve_source(catalog, source, ref)
            if selected.path is None or selected.format is not AgentFormat.CLAUDE:
                raise SessionMigrateError("Reviewed source path is unavailable")
            captured = capture_readable_source(
                SourceCaptureContext(source.format, selected.path),
                root=source.root,
                byte_limit=self.source_limit(reviewed_portability=True),
            )
            session = captured.parse(reviewed_portability=True)
            check_source_identity(captured)
            if session.source_format is not source.format or session.session_id != ref.native_id:
                raise SourceChangedError("Source identity changed during reviewed read.")
            if session.cli_version != "2.1.309" or type(session.reviewed_report) is not dict:
                raise SessionMigrateError("Reviewed source contract is unavailable")
        else:
            with Catalog(self.catalog) as catalog:
                session = read_session(
                    catalog,
                    source,
                    ref,
                    base_environ=self.environ,
                    max_source_bytes=self.source_bytes,
                )
        if self.revision(source, ref, **mode) != revision:
            raise SourceChangedError("Source changed during read.")
        return session

    def start_preview(
        self,
        source: Profile,
        ref: SessionRef,
        *,
        expand_tools: bool = False,
        reviewed_portability: bool = False,
    ) -> PreviewRequest:
        if type(reviewed_portability) is not bool:
            raise SessionMigrateError("Reviewed portability must be boolean")
        if reviewed_portability and source.format is not AgentFormat.CLAUDE:
            raise SessionMigrateError("Reviewed preview requires compacted Claude 2.1.309")

        def resolve():
            if source.format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                return self._native_snapshot(source, ref)
            revision = self.revision(
                source, ref, **({"reviewed_portability": True} if reviewed_portability else {})
            )
            return revision, self._read(
                source,
                ref,
                revision,
                **({"reviewed_portability": True} if reviewed_portability else {}),
            )

        return self.preview.request_authoritative(
            PreviewKey(
                source.format,
                source.id,
                str(source.root),
                ref.native_id,
                **({"reviewed_portability": True} if reviewed_portability else {}),
            ),
            resolve,
            timeout=10.0,
            expand_tools=expand_tools,
        )

    def discard(self, draft: Draft | None = None) -> None:
        with self._draft_lock:
            if draft is not None and self._active_draft != (draft.nonce, draft.options):
                draft.prepared.discard()
                return
            prepared = self._active_prepared
            self._active_draft = self._active_prepared = None
        if prepared is not None:
            prepared.discard()

    def prepare(
        self,
        source: Profile,
        ref: SessionRef,
        target: Profile,
        request: OperationRequest,
        cancellation: CancellationToken,
    ) -> Draft:
        self.discard()
        if request.reviewed_portability and (
            source.format is not AgentFormat.CLAUDE
            or target.format is not AgentFormat.OPENCODE
            or request.target_cli_version != "2.2.5"
        ):
            raise SessionMigrateError(
                "Reviewed portability requires Claude 2.1.309 to OpenCode 2.2.5"
            )
        mode = {"reviewed_portability": True} if request.reviewed_portability else {}
        limit = self.source_limit(**mode)
        revision = self.revision(source, ref, **mode)
        reserved = replace(request, cwd=request.cwd.resolve() if request.cwd is not None else None)
        with Catalog(self.catalog) as catalog:
            prepared = prepare_profile_operation(
                catalog,
                source,
                ref,
                target,
                reserved,
                base_environ=self.environ,
                cancellation=cancellation,
                max_source_bytes=limit,
            )
        try:
            if revision != self.revision(source, ref, **mode):
                raise SourceChangedError("Source changed during preparation.")
            proposal = proposal_metadata(prepared)
            metadata = proposal.to_dict()
            if request.reviewed_portability and (
                metadata.get("requires_reviewed_portability_acknowledgement") is not True
                or type(metadata.get("source", {}).get("reviewed_report")) is not dict
                or type(metadata.get("reviewed_archive")) is not dict
            ):
                raise SessionMigrateError(
                    "Reviewed proposal differences and archive are unavailable"
                )
            result = OperationResult(**metadata["legacy_result"])
            reserved = replace(
                reserved,
                session_id=metadata["target"]["reserved_uuid"],
                dry_run=True,
                cwd=Path(result.cwd),
            )
            nonce = proposal.proposal_id
            options = fingerprint(
                source,
                ref,
                target,
                reserved,
                **({"source_bytes": limit} if request.reviewed_portability else {}),
            )
            with self._draft_lock:
                self._active_draft, self._active_prepared = (nonce, options), prepared
            return Draft(
                nonce, source, ref, target, reserved, revision, result, options, prepared, proposal
            )
        except Exception:
            prepared.discard()
            raise

    def commit(
        self,
        draft: Draft,
        cancellation: CancellationToken,
        progress: Callable[[ProgressEvent], None],
        *,
        acknowledge_reviewed_portability: bool = False,
    ) -> TransferReceipt:
        if type(acknowledge_reviewed_portability) is not bool:
            raise SessionMigrateError("Reviewed portability acknowledgement must be boolean")
        if draft.request.reviewed_portability and not acknowledge_reviewed_portability:
            raise SessionMigrateError("Accept the displayed reviewed portability differences first")
        with self._draft_lock:
            if (
                draft.nonce,
                draft.options,
            ) != self._active_draft or draft.prepared is not self._active_prepared:
                raise SessionMigrateError("Confirmation already consumed or handle changed.")
            self._active_draft = self._active_prepared = None
        try:
            if (
                fingerprint(
                    draft.source,
                    draft.ref,
                    draft.target,
                    draft.request,
                    **(
                        {"source_bytes": self.source_limit(reviewed_portability=True)}
                        if draft.request.reviewed_portability
                        else {}
                    ),
                )
                != draft.options
            ):
                raise SessionMigrateError("Prepared options changed; prepare again.")
            if (
                self.revision(
                    draft.source,
                    draft.ref,
                    **(
                        {"reviewed_portability": True} if draft.request.reviewed_portability else {}
                    ),
                )
                != draft.revision
            ):
                raise SourceChangedError("Source changed; prepare again.")
            receipt = commit_operation(
                draft.prepared,
                cancellation=cancellation,
                progress=progress,
                **(
                    {"acknowledge_reviewed_portability": True}
                    if draft.request.reviewed_portability
                    else {}
                ),
            )
            if (
                type(receipt) is TransferReceipt
                and type(receipt.lineage_receipt) is CompletionReceipt
                and receipt.lineage_receipt.operation_id == draft.proposal.proposal_id
            ):
                with self._draft_lock:
                    self._completed_profiles[receipt.lineage_receipt.operation_id] = (
                        draft.source,
                        draft.target,
                    )
                    while len(self._completed_profiles) > 64:
                        self._completed_profiles.pop(next(iter(self._completed_profiles)))
            return receipt
        except Exception:
            draft.prepared.discard()
            raise

    def _continuity_path(self):
        if self.continuity_store_path is None:
            raise ContinuityReadError("state_home_unavailable")
        return self.continuity_store_path

    @staticmethod
    def _check_read_cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise ContinuityReadError("read_cancelled")

    def continuity_profiles(self, storage: Path | None = None) -> tuple[Profile, ...]:
        """Current explicit profiles; this entrypoint never initializes a catalog."""
        if storage is not None:
            return tuple(ProfileStore(storage).load())
        with Catalog.open_readonly_existing(self.catalog) as catalog:
            roots = catalog.roots(enabled_only=True)
        return tuple(
            Profile(
                f"root-{root.id}",
                f"{root.format}: {Path(root.path).name}",
                root.format,
                Path(root.path),
            )
            for root in roots
        )

    def open_handoff(self, source_profile_id, native_id, *, profiles_file=None):
        """Called on the dedicated handoff thread, which owns its driver until close."""
        from session_migrate.handoff_cli import HandoffWorkflow

        profiles = self.continuity_profiles(profiles_file)
        matches = [profile for profile in profiles if profile.id == source_profile_id]
        if len(matches) != 1:
            raise SessionMigrateError("current_profile_unavailable")
        source = matches[0]
        ref = SessionRef(
            source.format, source.root, native_id, source.id, profile_fingerprint=source.fingerprint
        )
        catalog = Catalog.open_readonly_existing(self.catalog)
        try:
            observer = EndpointObserver(
                catalog,
                profiles,
                base_environ=self._continuity_environment,
                byte_limit=self.source_bytes,
            )
            return HandoffWorkflow(
                catalog,
                observer,
                source,
                ref,
                self._continuity_path().parent,
                self._continuity_environment,
            )
        except Exception:
            catalog.close()
            raise

    def open_tree(self, *, profiles_file=None, reviewed_portability: bool = False):
        """Resolve only current existing profiles, on the dedicated tree worker."""
        from session_migrate.tree_cli import TreeWorkflow

        limit = self.source_limit(reviewed_portability=reviewed_portability)
        return TreeWorkflow(
            self.continuity_profiles(profiles_file),
            state_home=self._continuity_path().parent,
            base_environ=self.environ,
            max_source_bytes=limit,
            max_total_source_bytes=limit,
        )

    def list_operations(self, *, offset=0, limit=50, cancel_event=None) -> OperationPage:
        if (
            type(offset) is not int
            or not 0 <= offset <= 2**31 - 1
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ContinuityReadError("invalid_page")
        self._check_read_cancel(cancel_event)
        view = LineageStore.inspect_existing(self._continuity_path())
        self._check_read_cancel(cancel_event)
        if view is None:
            return OperationPage((), offset, 0, "ledger_missing")
        return OperationPage(
            view.operations[offset : offset + limit],
            offset,
            len(view.operations),
            "operations" if view.operations else "empty",
        )

    def inspect_operation(self, operation_id, *, cancel_event=None) -> ContinuityDetail:
        self._check_read_cancel(cancel_event)
        selector = ContinuitySelector(operation_id=operation_id)
        inspection = inspect_lineage(selector, self._continuity_path())
        self._check_read_cancel(cancel_event)
        return ContinuityDetail(inspection)

    def assess_operation(
        self, operation_id, *, profiles_file=None, current_profiles=None, cancel_event=None
    ) -> ContinuityDetail:
        """C1 authority from current profiles and a worker-local readonly index.

        Same-session context is retained from the actual committed Draft. Neither
        a historical profile hash nor a receipt root can construct a Profile.
        Cancellation abandons the result; an already running export can finish.
        """
        detail = self.inspect_operation(operation_id, cancel_event=cancel_event)
        if detail.inspection.operation is None:
            return detail
        if profiles_file is not None:
            profiles = self.continuity_profiles(profiles_file)
            context = "saved_profiles"
        elif current_profiles is not None:
            profiles = tuple(current_profiles)
            context = "explicit_current_profiles"
        else:
            with self._draft_lock:
                profiles = self._completed_profiles.get(operation_id)
            if profiles is not None:
                context = "completed_transfer"
            else:
                profiles = self.continuity_profiles()
                context = "registered_roots"
        if (
            not profiles
            or any(type(p) is not Profile for p in profiles)
            or len({p.id for p in profiles}) != len(profiles)
        ):
            return ContinuityDetail(
                detail.inspection,
                profile_context=context,
                context_reason="profile_context_required",
            )
        self._check_read_cancel(cancel_event)
        with Catalog.open_readonly_existing(self.catalog) as catalog:
            observer = EndpointObserver(
                catalog,
                profiles,
                base_environ=self._continuity_environment,
                byte_limit=self.source_bytes,
            )
            self._check_read_cancel(cancel_event)
            assessment = assess_continuity(
                ContinuitySelector(operation_id=operation_id), observer, self._continuity_path()
            )
            self._check_read_cancel(cancel_event)
            return ContinuityDetail(detail.inspection, assessment, context)
