"""Selected local resources; HTTP values resolve to opaque launch-owned IDs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from session_migrate.catalog import Catalog
from session_migrate.continuity import ContinuitySelector, ExplicitPair, assess_continuity
from session_migrate.continuity_bindings import EXECUTION_ENVIRONMENT, PUBLIC_ENVIRONMENT
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.conversion import default_migration_state_home
from session_migrate.lineage import LineageStore, SelectionRangeV1, _encode
from session_migrate.lineage_cli import metadata_dict
from session_migrate.operations import OperationRequest, proposal_metadata
from session_migrate.preview import PreviewLimits, project_session
from session_migrate.profiles import (
    Profile,
    list_profile_sessions,
    prepare_profile_operation,
    read_session,
    resolve_source,
)
from session_migrate.reconciliation import RecoveryContext
from session_migrate.usage import UsageLimits, capture_usage, extract_usage
from session_migrate.web.auth import RunRegistry
from session_migrate.web.jobs import JobManager


class BackendError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _witness(path):
    info = os.stat(path, follow_symlinks=False)
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _opaque():
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True, repr=False)
class _Selected:
    profile_id: str
    ref: object
    client_version: str | None


@dataclass(frozen=True, slots=True, repr=False)
class _Cursor:
    run_id: str
    action: str
    owner_id: str
    witness: tuple
    offset: int
    expires: float


@dataclass(frozen=True, slots=True, repr=False)
class _UsageCursor:
    run_id: str
    session_id: str
    source_cursor: str
    offset: int
    limit: int
    revision: object
    expires: float


@dataclass(frozen=True, slots=True, repr=False)
class _OperationSelection:
    run_id: str
    operation_id: str
    store_uuid: str
    record_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class _RecoveryBinding:
    run_id: str
    operation_id: str
    context: RecoveryContext


class WorkspaceBackend:
    """Launch configuration is process-only; no route accepts paths or environment."""

    def __init__(
        self,
        registry: RunRegistry,
        catalog_path: Path,
        profiles: tuple[Profile, ...],
        *,
        projects: Mapping[str, Path] | None = None,
        base_environ: Mapping[str, str] | None = None,
        state_home: Path | None = None,
        service_actions: frozenset[str] = frozenset(),
        runtime_actions: frozenset[str] = frozenset(),
    ):
        if type(registry) is not RunRegistry or type(profiles) is not tuple or not profiles:
            raise BackendError("launch_invalid")
        if len(profiles) > 32 or any(type(profile) is not Profile for profile in profiles):
            raise BackendError("launch_invalid")
        if len({profile.id for profile in profiles}) != len(profiles):
            raise BackendError("launch_invalid")
        if type(service_actions) is not frozenset or not service_actions <= {
            "adopt",
            "reconcile",
            "cancel_ready",
        }:
            raise BackendError("launch_invalid")
        self._service_actions = service_actions
        if type(runtime_actions) is not frozenset or not runtime_actions <= {
            "summary",
            "return_new_summary",
            "return_new_full",
        }:
            raise BackendError("launch_invalid")
        self._runtime_actions = runtime_actions
        self.registry, self.lock = registry, registry.lock
        self.catalog_path = Path(catalog_path)
        self.base_environ = dict(os.environ if base_environ is None else base_environ)
        self.observer_environ = {
            key: value
            for key, value in self.base_environ.items()
            if key in PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT
        }
        self.state_home = state_home or default_migration_state_home(environ=self.base_environ)
        self.profiles = {_opaque(): profile for profile in profiles}
        self.projects = {}
        for name, path in (projects or {}).items():
            if type(name) is not str or not name or len(name.encode("utf-8")) > 256:
                raise BackendError("launch_invalid")
            path = Path(path)
            if not path.is_absolute() or not path.is_dir():
                raise BackendError("launch_invalid")
            self.projects[_opaque()] = (name, path)
        if len(self.projects) > 32:
            raise BackendError("launch_invalid")
        with Catalog.open_readonly_existing(self.catalog_path):
            pass
        self._sessions, self._session_keys, self._cursors, self._operations = {}, {}, {}, {}
        self._recovery_bindings = {}
        self._usage = {}
        self._usage_cursors = {}
        self._reader = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="session-migrate-web-read"
        )
        self._closed = False
        self.jobs = JobManager(registry, self._prepare, runtime_resolver=self._resolve_runtime)

    def _resolve_runtime(self, parameters, *, native_action):
        from session_migrate.web.runtime_jobs import RuntimeRequest

        if native_action not in self._runtime_actions:
            raise BackendError("capability_unavailable")
        run_id = parameters.get("_run_id")
        self.registry.check_run(run_id, scope="publish")
        selected, source = self._selected(parameters.get("source_session_id"))
        target = self._profile(parameters.get("target_profile_id"))
        project_id = parameters.get("project_id")
        if type(project_id) is not str or project_id not in self.projects:
            raise BackendError("project_unavailable")
        fields = ("model", "model_provider", "target_cli_version")
        for name in fields:
            value = parameters.get(name)
            if (
                type(value) is not str
                or not 0 < len(value.encode("utf-8")) <= 256
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise BackendError("field_invalid")
        options = OperationRequest(
            "transfer",
            target_format=target.format.value,
            cwd=self.projects[project_id][1],
            model=parameters["model"],
            model_provider=parameters["model_provider"],
            target_cli_version=parameters["target_cli_version"],
            target_title=parameters.get("target_title"),
            title_policy=parameters.get("title_policy"),
        )
        ranges = parameters.get("selection", [])
        if type(ranges) is not list or len(ranges) > 128:
            raise BackendError("selection_invalid")
        selection = []
        for item in ranges:
            if type(item) is not dict or set(item) != {"first_event", "last_event", "block_index"}:
                raise BackendError("selection_invalid")
            selection.append(SelectionRangeV1(**item))
        if bool(selection) != (native_action != "return_new_full"):
            raise BackendError("selection_invalid")
        before = _witness(self.catalog_path)
        parent_token = parameters.get("operation_id")
        parent = None if native_action == "summary" else self._operation(parent_token, run_id)
        parent_selection = None if parent is None else self._operations[parent_token]
        parent_link = None
        if parent is not None:
            from session_migrate.handoff_operations import _sha
            from session_migrate.lineage import ReturnLinkV1

            if _sha(parent.record) != parent_selection.record_sha256:
                raise BackendError("operation_unavailable")
            parent_link = ReturnLinkV1(
                1, parent.record.operation_id, _sha(parent.receipt), parent.receipt.target_identity
            )
        if native_action == "summary" and parent_token is not None:
            raise BackendError("fields_invalid")
        with Catalog.open_readonly_existing(self.catalog_path) as catalog:
            entry = catalog.get_session(selected.ref.catalog_id, include_paths=True)
            if entry is None or entry.cli_version != selected.client_version:
                raise BackendError("source_changed")
            resolve_source(catalog, source, selected.ref)
            version = entry.cli_version

        def binding_check():
            if _witness(self.catalog_path) != before:
                raise BackendError("catalog_changed")
            current, current_profile = self._selected(parameters["source_session_id"])
            if (
                current != selected
                or current_profile != source
                or self._profile(parameters["target_profile_id"]) != target
            ):
                raise BackendError("profile_changed")
            with Catalog.open_readonly_existing(self.catalog_path) as catalog:
                resolve_source(catalog, source, selected.ref)
            if parent is not None:
                from session_migrate.handoff_operations import _validate_parent_binding

                if self._operations.get(parent_token) != parent_selection:
                    raise BackendError("operation_unavailable")
                # The core may already hold EX during finalization. Its private
                # coordinator validates physical parent rows without taking a
                # second flock, while retaining the same receipt/checkpoint proof.
                _validate_parent_binding(
                    self.state_home / "lineage-v1",
                    parent_link,
                    expected_record_sha=parent_selection.record_sha256,
                    expected_store_uuid=parent_selection.store_uuid,
                )
            if _witness(self.catalog_path) != before:
                raise BackendError("catalog_changed")

        binding_check()
        return RuntimeRequest(
            self.catalog_path,
            source,
            selected.ref,
            target,
            options,
            self.state_home,
            dict(self.base_environ),
            dict(self.observer_environ),
            version,
            tuple(selection),
            parent.record.operation_id if parent else None,
            binding_check,
        )

    def _auth(self, context, *, publish=False):
        if self._closed:
            raise BackendError("workspace_closed")
        return self.registry.authorize(context, "publish" if publish else "readonly")

    def _profile(self, opaque_id):
        if type(opaque_id) is not str or opaque_id not in self.profiles:
            raise BackendError("profile_unavailable")
        return self.profiles[opaque_id]

    def _selected(self, opaque_id):
        if type(opaque_id) is not str or opaque_id not in self._sessions:
            raise BackendError("session_unavailable")
        selected = self._sessions[opaque_id]
        profile = self._profile(selected.profile_id)
        if selected.ref.profile_fingerprint != profile.fingerprint:
            raise BackendError("profile_changed")
        return selected, profile

    def _limit(self, value, maximum):
        if value is None:
            return min(50, maximum)
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise BackendError("limit_invalid")
        limit = int(value)
        if not 1 <= limit <= maximum:
            raise BackendError("limit_invalid")
        return limit

    def _page(self, context, action, owner_id, revision, cursor, offset, more):
        run = self._auth(context)
        if cursor is not None:
            item = self._cursors.get(cursor)
            if (
                item is None
                or item.run_id != run.run_id
                or item.action != action
                or item.owner_id != owner_id
                or item.witness != revision
                or time.monotonic() >= item.expires
            ):
                raise BackendError("refresh_required")
            return item.offset
        if not more:
            return None
        now = time.monotonic()
        self._cursors = {key: item for key, item in self._cursors.items() if item.expires > now}
        if len(self._cursors) >= 128:
            raise BackendError("cursor_capacity")
        token = _opaque()
        self._cursors[token] = _Cursor(run.run_id, action, owner_id, revision, offset, now + 30)
        return token

    def _list_sessions(self, context, parameters):
        profile_id = parameters.get("profile_id")
        profile = self._profile(profile_id)
        limit = self._limit(parameters.get("limit"), 100)
        before = _witness(self.catalog_path)
        offset = (
            self._page(context, "sessions", profile_id, before, parameters.get("cursor"), 0, False)
            if parameters.get("cursor")
            else 0
        )
        with Catalog.open_readonly_existing(self.catalog_path) as catalog:
            entries = list_profile_sessions(catalog, profile, limit=limit + 1, offset=offset)
        if _witness(self.catalog_path) != before:
            raise BackendError("refresh_required")
        result = []
        with self.lock:
            for item in entries[:limit]:
                if item.ref is None:
                    continue
                key = (profile_id, item.ref.native_id, item.ref.catalog_id, profile.fingerprint)
                opaque_id = self._session_keys.get(key)
                if opaque_id is None:
                    if len(self._sessions) >= 10000:
                        raise BackendError("selection_capacity")
                    opaque_id = _opaque()
                    self._session_keys[key] = opaque_id
                    self._sessions[opaque_id] = _Selected(
                        profile_id, item.ref, item.entry.cli_version
                    )
                result.append(
                    {
                        "id": opaque_id,
                        "title": item.entry.title,
                        "format": item.entry.format,
                        "updated_at": item.entry.started_at,
                    }
                )
            cursor = self._page(
                context, "sessions", profile_id, before, None, offset + limit, len(entries) > limit
            )
        return {"schema_version": 1, "sessions": result, "next_cursor": cursor}

    def _preview(self, opaque_id):
        selected, profile = self._selected(opaque_id)
        before = _witness(self.catalog_path)
        with Catalog.open_readonly_existing(self.catalog_path) as catalog:
            session = read_session(
                catalog,
                profile,
                selected.ref,
                base_environ=self.base_environ,
                max_source_bytes=8 * 1024 * 1024,
            )
            preview = project_session(
                session,
                limits=PreviewLimits(max_bytes=60000, max_characters=60000, event_characters=4000),
            )
        if _witness(self.catalog_path) != before:
            raise BackendError("refresh_required")
        return {
            "schema_version": 1,
            "text": "\n\n".join(
                f"{event.role.value if event.role else event.kind.value}: {event.text}"
                for event in preview.events
            ),
            "truncated": bool(preview.notices),
        }

    def _prepare(self, parameters, *, service_action="transfer", **callbacks):
        if service_action != "transfer":
            return self._prepare_service(service_action, parameters, **callbacks)
        if "target_server" in parameters or "target_client" in parameters:
            raise BackendError("field_invalid")
        selected, source_profile = self._selected(parameters.get("source_session_id"))
        target_profile = self._profile(parameters.get("target_profile_id"))
        project_id = parameters.get("project_id")
        cwd = None
        if project_id is not None:
            if type(project_id) is not str or project_id not in self.projects:
                raise BackendError("project_unavailable")
            cwd = self.projects[project_id][1]
        title, policy = parameters.get("target_title"), parameters.get("title_policy")
        if title is not None and type(title) is not str:
            raise BackendError("title_invalid")
        if policy is not None and (
            type(policy) is not str or policy not in ("native", "local_alias")
        ):
            raise BackendError("title_policy_invalid")
        if title is None and policy is not None:
            raise BackendError("title_policy_invalid")
        request = OperationRequest(
            "transfer",
            target_format=target_profile.format.value,
            cwd=cwd,
            target_title=title,
            title_policy=policy,
        )
        before = _witness(self.catalog_path)
        external = callbacks.pop("prepublication_check", None)

        def guard():
            if _witness(self.catalog_path) != before:
                raise BackendError("catalog_changed")
            with Catalog.open_readonly_existing(self.catalog_path) as current:
                resolve_source(current, source_profile, selected.ref)
            if _witness(self.catalog_path) != before:
                raise BackendError("catalog_changed")
            if external is not None:
                external()

        with Catalog.open_readonly_existing(self.catalog_path) as catalog:
            resolve_source(catalog, source_profile, selected.ref)
            guard()
            prepared = prepare_profile_operation(
                catalog,
                source_profile,
                selected.ref,
                target_profile,
                request,
                base_environ=self.base_environ,
                max_source_bytes=8 * 1024 * 1024,
                catalog_opener=Catalog.open_readonly_existing,
                migration_state_home=self.state_home,
                prepublication_check=guard,
                **callbacks,
            )
        try:
            run_id = parameters.get("_run_id")
            state = prepared._state
            if (
                run_id is not None
                and state.built.source_snapshot is not None
                and isinstance(state.built.output, Path)
            ):
                recovery = RecoveryContext(
                    state.built.request,
                    state.built.source_snapshot.source_path,
                    state.built.output,
                    {k: v for k, v in state.source_environment.items() if k in PUBLIC_ENVIRONMENT},
                    {k: v for k, v in state.target_environment.items() if k in PUBLIC_ENVIRONMENT},
                    source_profile,
                    target_profile,
                    state.built.manifest,
                )
                with self.lock:
                    self.registry.check_run(run_id, scope="publish")
                    if len(self._recovery_bindings) >= 32:
                        raise BackendError("context_capacity")
                    self._recovery_bindings[_opaque()] = _RecoveryBinding(
                        run_id, proposal_metadata(prepared).proposal_id, recovery
                    )
            return prepared
        except Exception:
            prepared.discard()
            raise

    def _prepare_service(self, action, parameters, **callbacks):
        from session_migrate.web.service_jobs import SERVICE_CAPTURE_BYTES, prepare_service

        if action not in self._service_actions:
            raise BackendError("capability_unavailable")
        check = callbacks.get("prepublication_check")
        if check is not None:
            check()
        run_id = parameters.get("_run_id")
        self.registry.check_run(run_id, scope="publish")
        binding = {"run_id": run_id}
        keywords = {}
        if action == "adopt":
            source, _ = self._selected(parameters.get("source_session_id"))
            target, _ = self._selected(parameters.get("target_session_id"))
            keywords.update(pair=ExplicitPair(source.ref, target.ref), state_home=self.state_home)
            binding.update(
                source_session_id=parameters["source_session_id"],
                target_session_id=parameters["target_session_id"],
            )
        else:
            token = parameters.get("operation_id")
            operation = self._operation(token, run_id)
            selected = self._operations[token]
            binding.update(operation_id=token, record_sha256=selected.record_sha256)
            keywords.update(
                operation_id=operation.record.operation_id,
                store_path=self.state_home / "lineage-v1",
            )
            if action == "cancel_ready":
                keywords.update(
                    expected_record_sha256=selected.record_sha256,
                    expected_store_uuid=selected.store_uuid,
                )
                if check is not None:
                    check()
                return prepare_service(action, binding=binding, **keywords)
            if action != "reconcile":
                raise BackendError("capability_unavailable")
            context_id = parameters.get("recovery_context_id")
            original = self._recovery_bindings.get(context_id)
            if (
                original is None
                or original.run_id != run_id
                or original.operation_id != operation.record.operation_id
                or original.context.source_profile not in self.profiles.values()
                or original.context.target_profile not in self.profiles.values()
            ):
                raise BackendError("context_unavailable")
            binding["recovery_context_id"] = context_id
            keywords["context"] = original.context
        catalog = Catalog.open_readonly_existing(self.catalog_path)
        try:
            if action == "adopt":
                keywords.update(
                    source_client_version=catalog.get_session(
                        source.ref.catalog_id, include_paths=True
                    ).cli_version,
                    target_client_version=catalog.get_session(
                        target.ref.catalog_id, include_paths=True
                    ).cli_version,
                )
            observer = EndpointObserver(
                catalog,
                tuple(self.profiles.values()),
                base_environ=self.observer_environ,
                byte_limit=SERVICE_CAPTURE_BYTES,
            )
            if check is not None:
                check()
        except Exception:
            catalog.close()
            raise
        return prepare_service(action, observer=observer, binding=binding, **keywords)

    def _usage_report(self, context, opaque_id, parameters):
        selected, profile = self._selected(opaque_id)
        run = self._auth(context)
        limit = self._limit(parameters.get("limit"), 100)
        key = (run.run_id, opaque_id)
        with self.lock:
            cached = self._usage.get(key)
            if parameters.get("cursor"):
                if cached is None or time.monotonic() >= cached[2]:
                    raise BackendError("refresh_required")
                page = self._usage_cursors.get(parameters["cursor"])
                if (
                    page is None
                    or page.run_id != run.run_id
                    or page.session_id != opaque_id
                    or page.limit != limit
                    or page.revision != cached[3]
                    or time.monotonic() >= page.expires
                ):
                    raise BackendError("refresh_required")
                capture, _, _, _ = cached
                report = extract_usage(
                    capture, offset=page.offset, limit=limit, cursor=page.source_cursor
                )
                return self._usage_page(run.run_id, opaque_id, report, page.offset, limit)
            if cached is not None:
                cached[0].close()
                cached[1].close()
                self._usage.pop(key)
            now = time.monotonic()
            for old_key, item in tuple(self._usage.items()):
                if now >= item[2]:
                    item[0].close()
                    item[1].close()
                    self._usage.pop(old_key)
            if len(self._usage) >= 16:
                raise BackendError("capture_capacity")
        catalog = Catalog.open_readonly_existing(self.catalog_path)
        capture = None
        try:
            entry = catalog.get_session(selected.ref.catalog_id, include_paths=True)
            version = entry.cli_version
            if type(version) is not str or not version:
                raise BackendError("schema_unproven")
            observer = EndpointObserver(
                catalog,
                tuple(self.profiles.values()),
                base_environ=self.observer_environ,
                byte_limit=8 * 1024 * 1024,
            )
            capture = capture_usage(
                selected.ref, profile, observer, UsageLimits(), client_version=version
            )
            report = extract_usage(capture, limit=limit)
            with self.lock:
                self._auth(context)
                self._usage[key] = (
                    capture,
                    catalog,
                    time.monotonic() + 30,
                    report.capture_revision,
                )
            return self._usage_page(run.run_id, opaque_id, report, 0, limit)
        except Exception:
            if capture is not None:
                capture.close()
            catalog.close()
            raise

    def _usage_page(self, run_id, session_id, report, offset, limit):
        document = report.to_dict()
        if report.next_cursor is not None:
            now = time.monotonic()
            self._usage_cursors = {
                key: item for key, item in self._usage_cursors.items() if item.expires > now
            }
            if len(self._usage_cursors) >= 128:
                raise BackendError("cursor_capacity")
            token = _opaque()
            self._usage_cursors[token] = _UsageCursor(
                run_id,
                session_id,
                report.next_cursor,
                offset + limit,
                limit,
                report.capture_revision,
                now + 30,
            )
            document["next_cursor"] = token
        return document

    async def _read(self, context, function, *arguments):
        def guarded():
            self._auth(context)
            result = function(*arguments)
            self._auth(context)
            return result

        return await asyncio.get_running_loop().run_in_executor(self._reader, guarded)

    def _allowed_operation(self, operation):
        profiles = tuple(self.profiles.values())
        record = operation.record
        target = record.target_namespace
        if not any(
            profile.format.value == target.format
            and str(profile.root) == target.canonical_root
            and profile.fingerprint == record.target_profile_fingerprint
            for profile in profiles
        ):
            return False
        source = record.source.identity
        return source is not None and any(
            profile.format.value == source.namespace.format
            and str(profile.root) == source.namespace.canonical_root
            and profile.fingerprint == source.profile_fingerprint
            for profile in profiles
        )

    def _lineage_page(self, context, parameters):
        run_id = self._auth(context).run_id
        limit = self._limit(parameters.get("limit"), 100)
        view = LineageStore.inspect_existing(self.state_home / "lineage-v1")
        operations = (
            tuple(operation for operation in view.operations if self._allowed_operation(operation))
            if view
            else ()
        )
        revision = (
            view.store_uuid if view else None,
            json.dumps([metadata_dict(operation) for operation in operations], sort_keys=True),
        )
        cursor = parameters.get("cursor")
        offset = (
            self._page(context, "lineage", "state", revision, cursor, 0, False) if cursor else 0
        )
        result = []
        with self.lock:
            for operation in operations[offset : offset + limit]:
                operation_id = operation.record.operation_id
                selected = _OperationSelection(
                    run_id,
                    operation_id,
                    view.store_uuid,
                    hashlib.sha256(_encode(operation.record)).hexdigest(),
                )
                token = next(
                    (key for key, value in self._operations.items() if value == selected), None
                )
                if token is None:
                    if len(self._operations) >= 1024:
                        raise BackendError("operation_capacity")
                    token = _opaque()
                    self._operations[token] = selected
                original_id = next(
                    (
                        key
                        for key, value in self._recovery_bindings.items()
                        if value.run_id == run_id and value.operation_id == operation_id
                    ),
                    None,
                )
                result.append(
                    {
                        "id": token,
                        "operation_id": operation_id,
                        "state": operation.state.value,
                        "recovery_context_id": original_id,
                    }
                )
            next_cursor = self._page(
                context,
                "lineage",
                "state",
                revision,
                None,
                offset + limit,
                offset + limit < len(operations),
            )
        return {
            "schema_version": 1,
            "operations": result,
            "next_cursor": next_cursor,
            "reason": "listed" if view else "ledger_missing",
            "update_available": False,
        }

    def _operation(self, token, run_id):
        if type(token) is not str or token not in self._operations:
            raise BackendError("operation_unavailable")
        view = LineageStore.inspect_existing(self.state_home / "lineage-v1")
        selected = self._operations[token]
        if selected.run_id != run_id:
            raise BackendError("operation_unavailable")
        if view is not None and view.store_uuid == selected.store_uuid:
            for operation in view.operations:
                if (
                    operation.record.operation_id == selected.operation_id
                    and hashlib.sha256(_encode(operation.record)).hexdigest()
                    == selected.record_sha256
                    and self._allowed_operation(operation)
                ):
                    return operation
        raise BackendError("operation_unavailable")

    def _assessment(self, token, run_id):
        operation = self._operation(token, run_id)
        with Catalog.open_readonly_existing(self.catalog_path) as catalog:
            observer = EndpointObserver(
                catalog,
                tuple(self.profiles.values()),
                base_environ=self.observer_environ,
                byte_limit=8 * 1024 * 1024,
            )
            result = assess_continuity(
                ContinuitySelector(operation_id=operation.record.operation_id),
                observer,
                self.state_home / "lineage-v1",
            )
            return metadata_dict(result)

    async def dispatch(self, action, context, parameters, identifiers):
        service_prepare = {
            "prepare_adopt": "adopt",
            "prepare_reconcile": "reconcile",
            "prepare_cancel_ready": "cancel_ready",
        }
        service_confirm = {
            "confirm_adopt": "adopt",
            "confirm_reconcile": "reconcile",
            "confirm_cancel_ready": "cancel_ready",
        }
        runtime_prepare = {
            "capture_handoff": "summary",
            "capture_return_new": "return_new_summary",
            "prepare_return_new": "return_new_full",
        }
        runtime_confirm = {
            "confirm_handoff": "summary",
            "confirm_return_summary": "return_new_summary",
            "confirm_return_full": "return_new_full",
        }
        runtime_mutations = (
            {"accept_handoff", "discard_handoff"} | runtime_prepare.keys() | runtime_confirm.keys()
        )
        run = self._auth(
            context,
            publish=action
            in {"prepare", "confirm", "cancel"}
            | service_prepare.keys()
            | service_confirm.keys()
            | runtime_mutations,
        )
        if (action in service_prepare and service_prepare[action] not in self._service_actions) or (
            action in service_confirm and service_confirm[action] not in self._service_actions
        ):
            raise BackendError("capability_unavailable")
        if (
            (action in runtime_prepare and runtime_prepare[action] not in self._runtime_actions)
            or (action in runtime_confirm and runtime_confirm[action] not in self._runtime_actions)
            or (
                action in {"accept_handoff", "discard_handoff", "handoff_document"}
                and not self._runtime_actions
            )
        ):
            raise BackendError("capability_unavailable")
        if action == "profiles":
            return {
                "schema_version": 1,
                "profiles": [
                    {
                        "id": key,
                        "name": profile.name,
                        "format": profile.format.value,
                        **({"client": profile.client} if profile.client is not None else {}),
                    }
                    for key, profile in self.profiles.items()
                ],
                "projects": [{"id": key, "name": value[0]} for key, value in self.projects.items()],
            }
        if action == "capabilities":
            return {
                "schema_version": 1,
                "publish_enabled": "publish" in run.scopes,
                "full_transfer": True,
                "update_available": False,
                "summary_available": "summary" in self._runtime_actions,
                "return_new_available": "return_new_full" in self._runtime_actions,
                "return_summary_available": "return_new_summary" in self._runtime_actions,
                "adoption_available": "adopt" in self._service_actions,
                "cancel_ready_available": "cancel_ready" in self._service_actions,
                "reconcile_available": "reconcile" in self._service_actions
                and any(
                    binding.run_id == run.run_id for binding in self._recovery_bindings.values()
                ),
            }
        if action == "sessions":
            return await self._read(context, self._list_sessions, context, parameters)
        if action == "preview":
            return await self._read(context, self._preview, identifiers["session_id"])
        if action == "usage":
            return await self._read(
                context, self._usage_report, context, identifiers["session_id"], parameters
            )
        if action in ("lineage", "recovery"):
            return await self._read(context, self._lineage_page, context, parameters)
        if action == "inspect_lineage":
            return metadata_dict(
                await self._read(context, self._operation, identifiers["operation_id"], run.run_id)
            )
        if action == "assessment":
            return await self._read(
                context, self._assessment, parameters.get("operation_id"), run.run_id
            )
        if action == "prepare":
            return self.jobs.prepare({**parameters, "_run_id": run.run_id}, context).to_dict()
        if action in runtime_prepare:
            native_action = runtime_prepare[action]
            request = {**parameters, "_run_id": run.run_id}
            method = (
                self.jobs.prepare_runtime
                if native_action == "return_new_full"
                else self.jobs.capture
            )
            return method(request, context, native_action=native_action).to_dict()
        if action == "handoff_document":
            return self.jobs.handoff_document(identifiers["draft_id"], context).to_dict()
        if action == "accept_handoff":
            return self.jobs.accept_prepare(
                identifiers["draft_id"],
                parameters["document"],
                parameters["document_sha256"],
                context,
            ).to_dict()
        if action == "discard_handoff":
            return self.jobs.discard_draft(identifiers["draft_id"], context).to_dict()
        if action in runtime_confirm:
            return self.jobs.confirm(
                identifiers["proposal_id"],
                parameters["body_fingerprint"],
                parameters["idempotency_key"],
                context,
                native_action=runtime_confirm[action],
            ).to_dict()
        if action in service_prepare:
            return self.jobs.prepare(
                {**parameters, "_run_id": run.run_id},
                context,
                service_action=service_prepare[action],
            ).to_dict()
        if action == "review":
            return self.jobs.review(identifiers["proposal_id"], context).to_dict()
        if action == "confirm" or action in service_confirm:
            return self.jobs.confirm(
                identifiers["proposal_id"],
                parameters.get("body_fingerprint"),
                parameters.get("idempotency_key"),
                context,
                service_action=service_confirm.get(action, "transfer"),
            ).to_dict()
        if action == "cancel":
            return self.jobs.cancel(identifiers["job_id"], context).to_dict()
        if action == "job":
            return self.jobs.get(identifiers["job_id"], context).to_dict()
        raise BackendError("capability_unavailable")

    def close(self):
        if not self._closed:
            self.jobs.close()

            def close_reads():
                for capture, catalog, _, _ in self._usage.values():
                    capture.close()
                    catalog.close()
                self._usage.clear()
                self._usage_cursors.clear()
                self._recovery_bindings.clear()

            self._reader.submit(close_reads).result()
            self._reader.shutdown(wait=True)
            self._closed = True
