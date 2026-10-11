"""Bounded one-worker ownership of genuine prepared core operations."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from session_migrate.errors import OperationCancelledError, OperationNeedsReconcileError
from session_migrate.operations import (
    CancellationToken,
    PreparedOperation,
    TransferReceipt,
    commit_operation,
    proposal_metadata,
)
from session_migrate.web.auth import RunRegistry, WebAuthError
from session_migrate.web.runtime_jobs import (
    DOCUMENT_BYTES,
    NATIVE_ACTIONS,
    RUNTIME_BUFFER_BYTES,
    PrivateRuntimeHandle,
    RuntimeJobError,
    capture_runtime,
    commit_runtime,
    new_runtime,
    prepare_runtime,
    runtime_metadata,
    workflow_json,
)
from session_migrate.web.service_jobs import (
    SERVICE_ACTIONS,
    SERVICE_BUFFER_BYTES,
    PrivateTypedServiceHandle,
    ServicePreparationError,
    commit_service,
    service_action,
    service_metadata,
)

_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TERMINAL = frozenset({"completed", "failed", "cancelled", "uncertain", "recovered"})
MAX_BUFFER_BYTES = 128 * 1024 * 1024


class JobError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _document(value):
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded) > 1024 * 1024:
            raise JobError("metadata_limit")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise JobError("metadata_invalid") from None


@dataclass(frozen=True, slots=True, repr=False)
class JobDTO:
    _json: str

    def to_dict(self):
        return json.loads(self._json)


@dataclass(slots=True, repr=False)
class _Job:
    run_id: str
    job_id: str
    action: str
    fingerprint: str
    token: CancellationToken
    state: str = "queued"
    proposal_id: str | None = None
    operation_id: str | None = None
    request_json: str | None = None
    handle: PreparedOperation | PrivateTypedServiceHandle | PrivateRuntimeHandle | None = None
    result_json: str | None = None
    effects: tuple[str, ...] = ()
    buffer_bytes: int = 0
    auth_lease: object = None
    service_action: str = "transfer"
    subject: dict | None = None
    deadline: float | None = None
    native_action: str | None = None
    cancel_requested: bool = False


@dataclass(slots=True, repr=False)
class _Proposal:
    run_id: str
    proposal_id: str
    preparation_job_id: str
    operation_id: str | None
    fingerprint: str
    metadata_json: str
    effects: tuple[str, ...]
    expires: float
    expires_at: str
    handle: PreparedOperation | PrivateTypedServiceHandle | PrivateRuntimeHandle | None
    buffer_bytes: int
    consumed: bool = False
    auth_lease: object = None
    service_action: str = "transfer"
    subject: dict | None = None
    native_action: str | None = None


@dataclass(slots=True, repr=False)
class _Draft:
    run_id: str
    draft_id: str
    capture_job_id: str
    native_action: str
    handle: PrivateRuntimeHandle | None
    document: str | None
    metadata_json: str
    expires: float
    expires_at: str
    buffer_bytes: int
    auth_lease: object
    consumed: bool = False
    accepted_fingerprint: str | None = None
    acceptance_job_id: str | None = None


class _AuthLease:
    """Retain authorized core ownership once its real publication barrier closes."""

    def __init__(self, registry, run_id):
        self.registry, self.run_id = registry, run_id
        self.commit_token = None
        self.service_entered = False
        self.service_bundle = None
        self.service_deadline = None
        self.clock = time.monotonic

    def check(self):
        with self.registry.lock:
            if self.service_entered:
                return
            if self.commit_token is not None and self.commit_token.publishing:
                return
            self.registry.check_run(self.run_id, scope="publish")
            if self.service_deadline is not None:
                self.commit_token.check()
                if self.clock() >= self.service_deadline:
                    raise JobError("service_deadline")

    def enter_service(self, bundle):
        with self.registry.lock:
            if self.service_entered or self.service_bundle is not bundle:
                raise JobError("private_handle_required")
            self.registry.check_run(self.run_id, scope="publish")
            self.commit_token.check()
            if self.clock() >= self.service_deadline:
                raise JobError("service_deadline")
            service_metadata(bundle)  # A live genuine issuer bundle is still required.
            self.service_entered = True


class JobManager:
    """Core execution stays outside the authentication/queue lock."""

    def __init__(
        self,
        registry: RunRegistry,
        preparer,
        *,
        runtime_resolver=None,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        if type(registry) is not RunRegistry or not callable(preparer):
            raise JobError("manager_invalid")
        if runtime_resolver is not None and not callable(runtime_resolver):
            raise JobError("manager_invalid")
        self._runtime_resolver = runtime_resolver
        self.registry, self.lock = registry, registry.lock
        self._condition = threading.Condition(self.lock)
        self._preparer, self._clock, self._wall_clock = preparer, clock, wall_clock
        self._queue = deque()
        self._cleanup = deque()
        self._jobs, self._proposals, self._keys = {}, {}, {}
        self._drafts = {}
        self._buffers = 0
        self._closed = False
        registry._bind_revocation_hook(self._revoke_run)
        self._worker = threading.Thread(target=self._work, name="session-migrate-jobs", daemon=True)
        self._worker.start()

    def _auth(self, context, *, publish=False):
        if self._closed:
            raise JobError("manager_closed")
        try:
            return self.registry.authorize(context, "publish" if publish else "readonly")
        except WebAuthError as error:
            raise JobError(error.reason) from None

    def _dto(self, job):
        state = job.state
        if (
            state == "committing"
            and job.service_action != "transfer"
            and job.auth_lease.service_entered
        ):
            state = "service_commit_entered"
        elif state == "committing" and job.token.publishing:
            state = "barrier_pubclosed"
        document = {
            "schema_version": 1,
            "run_id": job.run_id,
            "job_id": job.job_id,
            "proposal_id": job.proposal_id,
            "operation_id": job.operation_id,
            "fingerprint": job.fingerprint,
            "action": job.action,
            "state": state,
            "effects": list(job.effects),
            "result": json.loads(job.result_json) if job.result_json else None,
        }
        if job.service_action != "transfer":
            document.update(service_action=job.service_action, subject=job.subject)
        if job.native_action is not None:
            document["native_action"] = job.native_action
        return JobDTO(_document(document))

    def _reserve_job(self, run_id):
        if len(self._queue) >= 8:
            raise JobError("queue_capacity")
        if sum(job.run_id == run_id for job in self._jobs.values()) >= 64:
            raise JobError("job_capacity")

    def _drop_proposal(self, proposal):
        if proposal.handle is not None:
            if proposal.service_action == "transfer" and proposal.native_action is None:
                proposal.handle.discard()
                self._buffers -= proposal.buffer_bytes
            else:
                self._cleanup.append((proposal.handle, proposal.buffer_bytes))
                self._condition.notify_all()
            proposal.handle = None
            proposal.buffer_bytes = 0
        proposal.consumed = True

    def _expire_proposals(self):
        now = self._clock()
        for draft in self._drafts.values():
            if not draft.consumed and now >= draft.expires:
                self._drop_draft(draft, "draft_expired")
        for proposal in self._proposals.values():
            if not proposal.consumed and now >= proposal.expires:
                self._drop_proposal(proposal)
                job = self._jobs[proposal.preparation_job_id]
                job.state = "cancelled"
                job.result_json = _document({"reason": "proposal_expired"})

    def _runtime_enqueue(self, request, context, action, native_action):
        if type(native_action) is not str or native_action not in NATIVE_ACTIONS:
            raise JobError("action_invalid")
        if (action == "capture") != (native_action != "return_new_full"):
            raise JobError("action_invalid")
        if type(request) is not dict:
            raise JobError("request_invalid")
        try:
            encoded = workflow_json(request)
        except RuntimeJobError as error:
            raise JobError(error.reason) from None
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._condition:
            run = self._auth(context, publish=True)
            self._expire_proposals()
            self._reserve_job(run.run_id)
            if self._runtime_resolver is None:
                raise JobError("runtime_unavailable")
            if action == "capture":
                active = [draft for draft in self._drafts.values() if not draft.consumed]
                queued = [
                    j
                    for j in self._jobs.values()
                    if j.action == "capture" and j.state in {"queued", "preparing"}
                ]
                if len(active) + len(queued) >= 8 or (
                    sum(d.run_id == run.run_id for d in active)
                    + sum(j.run_id == run.run_id for j in queued)
                    >= 2
                ):
                    raise JobError("draft_capacity")
            if self._buffers + RUNTIME_BUFFER_BYTES > MAX_BUFFER_BYTES:
                raise JobError("buffer_capacity")
            job = _Job(
                run.run_id,
                str(uuid.uuid4()),
                action,
                fingerprint,
                CancellationToken(),
                request_json=encoded,
                buffer_bytes=RUNTIME_BUFFER_BYTES,
                native_action=native_action,
                deadline=self._clock() + 30,
            )
            self._buffers += job.buffer_bytes
            try:
                self._jobs[job.job_id] = job
                self._queue.append(job.job_id)
                self._condition.notify()
            except Exception:
                self._jobs.pop(job.job_id, None)
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                self._buffers -= job.buffer_bytes
                raise JobError("enqueue_failed") from None
            return self._dto(job)

    def capture(self, request, context, *, native_action="summary"):
        return self._runtime_enqueue(request, context, "capture", native_action)

    def prepare_runtime(self, request, context, *, native_action="return_new_full"):
        return self._runtime_enqueue(request, context, "prepare_runtime", native_action)

    def handoff_document(self, draft_id, context):
        # Only copies captured in the worker are read here. No SQLite/core authority IO.
        with self.lock:
            run = self._auth(context)
            self._expire_proposals()
            draft = self._drafts.get(draft_id)
            if draft is None or draft.run_id != run.run_id or draft.consumed:
                raise JobError("draft_unavailable")
            return JobDTO(
                _document(
                    {
                        "schema_version": 1,
                        "run_id": draft.run_id,
                        "draft_id": draft.draft_id,
                        "native_action": draft.native_action,
                        "document": draft.document,
                        "document_sha256": hashlib.sha256(
                            draft.document.encode("utf-8")
                        ).hexdigest(),
                        "expires_at": draft.expires_at,
                        "metadata": json.loads(draft.metadata_json),
                    }
                )
            )

    def accept_prepare(self, draft_id, document, document_sha256, context):
        if type(document) is not str or type(document_sha256) is not str:
            raise JobError("request_invalid")
        try:
            if not 0 < len(document.encode("utf-8")) <= DOCUMENT_BYTES or "\x00" in document:
                raise JobError("request_invalid")
            if _HASH.fullmatch(document_sha256) is None:
                raise JobError("fingerprint_invalid")
            encoded = workflow_json({"document": document, "document_sha256": document_sha256})
        except (UnicodeError, RuntimeJobError):
            raise JobError("request_invalid") from None
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._condition:
            run = self._auth(context, publish=True)
            self._expire_proposals()
            draft = self._drafts.get(draft_id)
            if draft is None or draft.run_id != run.run_id:
                raise JobError("draft_unavailable")
            if draft.acceptance_job_id is not None:
                if draft.accepted_fingerprint != fingerprint:
                    raise JobError("idempotency_conflict")
                return self._dto(self._jobs[draft.acceptance_job_id])
            if draft.consumed or draft.handle is None:
                raise JobError("draft_unavailable")
            self._reserve_job(run.run_id)
            job = _Job(
                run.run_id,
                str(uuid.uuid4()),
                "accept_prepare",
                fingerprint,
                CancellationToken(),
                request_json=encoded,
                handle=draft.handle,
                buffer_bytes=draft.buffer_bytes,
                native_action=draft.native_action,
                auth_lease=draft.auth_lease,
                deadline=self._clock() + 30,
            )
            previous_document = draft.document
            # Move before queueing; restore only an enqueue failure before any core IO.
            draft.consumed, draft.handle, draft.document, draft.buffer_bytes = True, None, None, 0
            draft.accepted_fingerprint, draft.acceptance_job_id = fingerprint, job.job_id
            try:
                self._jobs[job.job_id] = job
                self._queue.append(job.job_id)
                self._condition.notify()
            except Exception:
                self._jobs.pop(job.job_id, None)
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                draft.consumed, draft.handle, draft.document, draft.buffer_bytes = (
                    False,
                    job.handle,
                    previous_document,
                    job.buffer_bytes,
                )
                draft.accepted_fingerprint = draft.acceptance_job_id = None
                job.handle = None
                raise JobError("enqueue_failed") from None
            self._jobs[draft.capture_job_id].state = "completed"
            return self._dto(job)

    def _drop_draft(self, draft, reason):
        if draft.handle is not None:
            self._cleanup.append((draft.handle, draft.buffer_bytes))
            self._condition.notify_all()
        draft.handle, draft.document, draft.buffer_bytes, draft.consumed = None, None, 0, True
        job = self._jobs[draft.capture_job_id]
        job.state, job.result_json = "cancelled", _document({"reason": reason})

    def discard_draft(self, draft_id, context):
        with self._condition:
            run = self._auth(context, publish=True)
            self._expire_proposals()
            draft = self._drafts.get(draft_id)
            if draft is None or draft.run_id != run.run_id or draft.consumed:
                raise JobError("draft_unavailable")
            self._drop_draft(draft, "draft_discarded")
            return self._dto(self._jobs[draft.capture_job_id])

    def _runtime_captured(self, job, handle, captured, auth_lease):
        document, metadata = captured
        metadata_json = _document(metadata)
        with self._condition:
            self.registry.check_run(job.run_id, scope="publish")
            job.token.check()
            if self._clock() >= job.deadline:
                raise JobError("runtime_deadline")
            self._expire_proposals()
            draft_id = str(uuid.uuid4())
            draft = _Draft(
                job.run_id,
                draft_id,
                job.job_id,
                job.native_action,
                handle,
                document,
                metadata_json,
                self._clock() + 300,
                datetime.fromtimestamp(self._wall_clock() + 300, UTC).isoformat(),
                job.buffer_bytes,
                auth_lease,
            )
            self._drafts[draft_id] = draft
            job.buffer_bytes, job.request_json = 0, None
            job.state, job.effects = "editable", tuple(metadata.get("native_read_effects", ()))
            job.result_json = _document(
                {"draft_id": draft_id, "metadata": metadata, "expires_at": draft.expires_at}
            )

    def _runtime_prepared(self, job, handle, auth_lease):
        operation_id, metadata_json, fingerprint, effects, charge = runtime_metadata(handle)
        with self._condition:
            # Core closes its preparation token on return. The job still owns
            # cancellation admission until it exposes a confirmable proposal.
            if job.cancel_requested:
                raise OperationCancelledError("Native preparation cancelled before review.")
            self.registry.check_run(job.run_id, scope="publish")
            job.token.check()
            if self._clock() >= job.deadline:
                raise JobError("runtime_deadline")
            self._expire_proposals()
            if sum(not p.consumed for p in self._proposals.values()) >= 32:
                raise JobError("proposal_capacity")
            if self._buffers - job.buffer_bytes + charge > MAX_BUFFER_BYTES:
                raise JobError("buffer_capacity")
            proposal_id = str(uuid.uuid4())
            proposal = _Proposal(
                job.run_id,
                proposal_id,
                job.job_id,
                operation_id,
                fingerprint,
                metadata_json,
                effects,
                self._clock() + 300,
                datetime.fromtimestamp(self._wall_clock() + 300, UTC).isoformat(),
                handle,
                charge,
                auth_lease=auth_lease,
                native_action=job.native_action,
            )
            self._buffers += charge - job.buffer_bytes
            self._proposals[proposal_id] = proposal
            job.buffer_bytes, job.request_json = 0, None
            job.state, job.proposal_id, job.operation_id = "reviewable", proposal_id, operation_id
            job.effects, job.result_json = effects, _document({"proposal_id": proposal_id})

    def prepare(self, request, context, *, service_action="transfer"):
        if type(service_action) is not str or service_action not in SERVICE_ACTIONS | {"transfer"}:
            raise JobError("action_invalid")
        encoded = _document(request)
        if type(request) is not dict or len(encoded) > 64 * 1024:
            raise JobError("request_invalid")
        fingerprint = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        with self._condition:
            run = self._auth(context, publish=True)
            self._expire_proposals()
            self._reserve_job(run.run_id)
            charge = SERVICE_BUFFER_BYTES if service_action != "transfer" else len(encoded)
            if self._buffers + charge > MAX_BUFFER_BYTES:
                raise JobError("buffer_capacity")
            job = _Job(
                run.run_id,
                str(uuid.uuid4()),
                "prepare",
                fingerprint,
                CancellationToken(),
                request_json=encoded,
                buffer_bytes=charge,
                service_action=service_action,
                deadline=self._clock() + 30 if service_action != "transfer" else None,
            )
            self._buffers += job.buffer_bytes
            try:
                self._jobs[job.job_id] = job
                self._queue.append(job.job_id)
                self._condition.notify()
            except Exception:
                self._jobs.pop(job.job_id, None)
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                self._buffers -= job.buffer_bytes
                raise JobError("enqueue_failed") from None
            return self._dto(job)

    def review(self, proposal_id, context):
        with self.lock:
            run = self._auth(context)
            self._expire_proposals()
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.run_id != run.run_id or proposal.consumed:
                raise JobError("proposal_unavailable")
            document = {
                "schema_version": 1,
                "run_id": proposal.run_id,
                "proposal_id": proposal.proposal_id,
                "preparation_job_id": proposal.preparation_job_id,
                "operation_id": proposal.operation_id,
                "fingerprint": proposal.fingerprint,
                "state": "reviewable",
                "expires_at": proposal.expires_at,
                "effects": list(proposal.effects),
                "metadata": json.loads(proposal.metadata_json),
            }
            if proposal.service_action != "transfer":
                document.update(service_action=proposal.service_action, subject=proposal.subject)
            if proposal.native_action is not None:
                document["native_action"] = proposal.native_action
            return JobDTO(_document(document))

    def confirm(
        self,
        proposal_id,
        body_fingerprint,
        idempotency_key,
        context,
        *,
        service_action="transfer",
        native_action=None,
    ):
        if type(service_action) is not str or service_action not in SERVICE_ACTIONS | {"transfer"}:
            raise JobError("action_invalid")
        if native_action is not None and (
            type(native_action) is not str or native_action not in NATIVE_ACTIONS
        ):
            raise JobError("action_invalid")
        if native_action is not None and service_action != "transfer":
            raise JobError("action_invalid")
        if type(idempotency_key) is not str or _KEY.fullmatch(idempotency_key) is None:
            raise JobError("idempotency_key_invalid")
        if type(body_fingerprint) is not str or _HASH.fullmatch(body_fingerprint) is None:
            raise JobError("fingerprint_invalid")
        with self._condition:
            run = self._auth(context, publish=True)
            key = (run.run_id, idempotency_key)
            payload = (proposal_id, body_fingerprint, service_action)
            if native_action is not None:
                payload += (native_action,)
            if key in self._keys:
                previous, job_id = self._keys[key]
                if previous != payload:
                    raise JobError("idempotency_conflict")
                return self._dto(self._jobs[job_id])
            self._expire_proposals()
            proposal = self._proposals.get(proposal_id)
            if (
                proposal is None
                or proposal.run_id != run.run_id
                or proposal.consumed
                or proposal.fingerprint != body_fingerprint
                or proposal.handle is None
                or proposal.service_action != service_action
                or proposal.native_action != native_action
            ):
                raise JobError("proposal_unavailable")
            self._reserve_job(run.run_id)
            if sum(existing[0] == run.run_id for existing in self._keys) >= 64:
                raise JobError("idempotency_capacity")
            job = _Job(
                run.run_id,
                str(uuid.uuid4()),
                "confirm",
                body_fingerprint,
                CancellationToken(),
                proposal_id=proposal_id,
                operation_id=proposal.operation_id,
                handle=proposal.handle,
                effects=proposal.effects,
                buffer_bytes=proposal.buffer_bytes,
                auth_lease=proposal.auth_lease,
                service_action=service_action,
                subject=proposal.subject,
                native_action=native_action,
                deadline=self._clock() + 30 if service_action != "transfer" else None,
            )
            previous_handle, previous_charge = proposal.handle, proposal.buffer_bytes
            previous_token = proposal.auth_lease.commit_token
            previous_bundle, previous_deadline = (
                proposal.auth_lease.service_bundle,
                proposal.auth_lease.service_deadline,
            )
            try:
                proposal.auth_lease.commit_token = job.token
                if service_action != "transfer":
                    proposal.auth_lease.service_bundle = job.handle
                    proposal.auth_lease.service_deadline = job.deadline
                self._keys[key] = (payload, job.job_id)
                self._jobs[job.job_id] = job
                proposal.handle, proposal.buffer_bytes, proposal.consumed = None, 0, True
                self._queue.append(job.job_id)
                self._condition.notify()
            except Exception:
                self._keys.pop(key, None)
                self._jobs.pop(job.job_id, None)
                if job.job_id in self._queue:
                    self._queue.remove(job.job_id)
                proposal.handle, proposal.buffer_bytes, proposal.consumed = (
                    previous_handle,
                    previous_charge,
                    False,
                )
                proposal.auth_lease.commit_token = previous_token
                proposal.auth_lease.service_bundle = previous_bundle
                proposal.auth_lease.service_deadline = previous_deadline
                job.handle = None
                raise JobError("enqueue_failed") from None
            return self._dto(job)

    def get(self, job_id, context):
        with self.lock:
            run = self._auth(context)
            self._expire_proposals()
            job = self._jobs.get(job_id)
            if job is None or job.run_id != run.run_id:
                raise JobError("job_unavailable")
            return self._dto(job)

    def cancel(self, job_id, context):
        with self.lock:
            run = self._auth(context, publish=True)
            job = self._jobs.get(job_id)
            if job is None or job.run_id != run.run_id:
                raise JobError("job_unavailable")
            self._cancel(job)
            return self._dto(job)

    def _cancel(self, job):
        if job.state in _TERMINAL:
            return
        if job.state == "preparing" and job.native_action is not None:
            job.cancel_requested = True
        if job.state == "editable":
            for draft in self._drafts.values():
                if draft.capture_job_id == job.job_id and not draft.consumed:
                    self._drop_draft(draft, "draft_discarded")
                    break
        elif job.state == "reviewable":
            proposal = self._proposals.get(job.proposal_id)
            if proposal is not None:
                self._drop_proposal(proposal)
            job.state = "cancelled"
        elif job.state == "queued":
            job.token.request_cancel()
            job.state = "cancelled"
            if job.handle is not None:
                if job.service_action == "transfer" and job.native_action is None:
                    job.handle.discard()
                else:
                    self._cleanup.append((job.handle, job.buffer_bytes))
                    job.buffer_bytes = 0  # The cleanup queue retains its charge.
                    self._condition.notify_all()
                job.handle = None
            self._buffers -= job.buffer_bytes
            job.buffer_bytes = 0
            job.request_json = None
            self._queue.remove(job.job_id)
        else:
            job.token.request_cancel()

    def _revoke_run(self, run_id):
        for job in self._jobs.values():
            if job.run_id == run_id:
                self._cancel(job)
        self._condition.notify_all()

    def _progress(self, job_id, _event):
        # UI progress never interrupts core. The publication state is read from its token.
        try:
            with self.lock:
                job = self._jobs.get(job_id)
                if job is not None and job.state == "committing" and job.token.publishing:
                    self._condition.notify_all()
        except Exception:
            return

    def _service_prepared(self, job, prepared, auth_lease):
        if service_action(prepared) != job.service_action:
            raise JobError("action_invalid")
        subject, metadata_json, fingerprint, effects, charge = service_metadata(prepared)
        with self._condition:
            self.registry.check_run(job.run_id, scope="publish")
            job.token.check()
            if self._clock() >= job.deadline:
                raise JobError("service_deadline")
            self._expire_proposals()
            if sum(not item.consumed for item in self._proposals.values()) >= 32:
                raise JobError("proposal_capacity")
            if self._buffers - job.buffer_bytes + charge > MAX_BUFFER_BYTES:
                raise JobError("buffer_capacity")
            proposal_id = str(uuid.uuid4())
            operation_id = subject["id"] if subject["kind"] == "operation" else None
            proposal = _Proposal(
                job.run_id,
                proposal_id,
                job.job_id,
                operation_id,
                fingerprint,
                metadata_json,
                effects,
                self._clock() + 300,
                datetime.fromtimestamp(self._wall_clock() + 300, UTC).isoformat(),
                prepared,
                charge,
                auth_lease=auth_lease,
                service_action=job.service_action,
                subject=subject,
            )
            self._buffers += charge - job.buffer_bytes
            job.buffer_bytes, job.request_json = 0, None
            self._proposals[proposal_id] = proposal
            job.state, job.proposal_id, job.operation_id = "reviewable", proposal_id, operation_id
            job.subject, job.effects = subject, effects
            job.result_json = _document({"proposal_id": proposal_id})

    def _service_cleanup(self, handle, charge, job=None):
        # Never discard a service or close SQLite on the HTTP/revocation thread.
        try:
            handle.discard()
        except Exception:
            with self._condition:
                if job is not None:
                    entered = job.token.publishing or (
                        job.auth_lease is not None and job.auth_lease.service_entered
                    )
                    job.state = "uncertain" if entered else "failed"
                    job.result_json = _document({"reason": "resource_cleanup_unconfirmed"})
                # Retain the capability and charge until its owning worker really
                # closes it. Close consequently has no invented wall-clock bound.
                self._cleanup.append((handle, charge, job))
                self._condition.wait(timeout=0.05)
            return
        with self._condition:
            self._buffers -= charge
            self._condition.notify_all()

    def _work(self):
        while True:
            with self._condition:
                while not self._queue and not self._cleanup and not self._closed:
                    self._condition.wait(timeout=1)
                    self._expire_proposals()
                if not self._queue and not self._cleanup and self._closed:
                    return
                cleanup = self._cleanup.popleft() if self._cleanup else None
                job = None if cleanup is not None else self._jobs[self._queue.popleft()]
                if job is not None:
                    if job.state == "cancelled":
                        continue
                    job.state = "committing" if job.action == "confirm" else "preparing"
            if cleanup is not None:
                self._service_cleanup(*cleanup)
                continue
            prepared = request = captured = resolved = None
            try:
                self.registry.check_run(job.run_id, scope="publish")
                job.token.check()
                if job.deadline is not None and self._clock() >= job.deadline:
                    raise JobError("service_deadline")
                if job.native_action is not None and job.action != "confirm":
                    auth_lease = job.auth_lease or _AuthLease(self.registry, job.run_id)
                    auth_lease.clock = self._clock
                    job.auth_lease = auth_lease
                    if job.action == "accept_prepare":
                        prepared = job.handle
                        request = json.loads(job.request_json)
                    else:
                        if self._runtime_resolver is None:
                            raise JobError("runtime_unavailable")
                        request = json.loads(job.request_json)
                        resolved = self._runtime_resolver(request, native_action=job.native_action)
                        prepared = new_runtime(resolved, job.native_action, job.run_id)
                        captured = capture_runtime(
                            prepared,
                            cancellation=job.token,
                            prepublication_check=auth_lease.check,
                        )
                        if job.action == "capture":
                            self._runtime_captured(job, prepared, captured, auth_lease)
                            prepared = None
                            continue
                    prepare_runtime(
                        prepared,
                        cancellation=job.token,
                        document=request.get("document"),
                        document_sha256=request.get("document_sha256"),
                        prepublication_check=auth_lease.check,
                        progress=lambda event, current_id=job.job_id: self._progress(
                            current_id, event
                        ),
                    )
                    self._runtime_prepared(job, prepared, auth_lease)
                    prepared = job.handle = None
                    continue
                if job.action == "prepare":
                    request = json.loads(job.request_json)
                    auth_lease = _AuthLease(self.registry, job.run_id)
                    auth_lease.clock = self._clock
                    if job.service_action != "transfer":
                        auth_lease.commit_token = job.token
                        auth_lease.service_deadline = job.deadline
                    extra = (
                        {}
                        if job.service_action == "transfer"
                        else {
                            "service_action": job.service_action,
                        }
                    )
                    prepared = self._preparer(
                        request,
                        cancellation=job.token,
                        progress=lambda event, current_id=job.job_id: self._progress(
                            current_id, event
                        ),
                        prepublication_check=auth_lease.check,
                        **extra,
                    )
                    if job.service_action != "transfer":
                        if type(prepared) is not PrivateTypedServiceHandle:
                            raise JobError("private_handle_required")
                        self._service_prepared(job, prepared, auth_lease)
                        prepared = None
                        continue
                    if type(prepared) is not PreparedOperation:
                        raise JobError("private_handle_required")
                    core = proposal_metadata(prepared)
                    metadata_json = _document(core.to_dict())
                    fingerprint = hashlib.sha256(metadata_json.encode("ascii")).hexdigest()
                    state = prepared._state
                    if state is None:
                        raise JobError("private_handle_required")
                    charge = len(state.built.artifact.native_bytes) + len(metadata_json)
                    snapshots = [*state.observed_captures, *state.last_source_capture]
                    charge += sum(
                        len(snapshot.raw_bytes)
                        for snapshot in {id(s): s for s in snapshots}.values()
                    )
                    with self._condition:
                        self.registry.check_run(job.run_id, scope="publish")
                        job.token.check()
                        self._expire_proposals()
                        if sum(not item.consumed for item in self._proposals.values()) >= 32:
                            raise JobError("proposal_capacity")
                        if self._buffers - job.buffer_bytes + charge > MAX_BUFFER_BYTES:
                            raise JobError("buffer_capacity")
                        proposal_id = str(uuid.uuid4())
                        effects = tuple(core.to_dict().get("effects", ()))
                        proposal = _Proposal(
                            job.run_id,
                            proposal_id,
                            job.job_id,
                            core.proposal_id,
                            fingerprint,
                            metadata_json,
                            effects,
                            self._clock() + 300,
                            datetime.fromtimestamp(self._wall_clock() + 300, UTC).isoformat(),
                            prepared,
                            charge,
                            auth_lease=auth_lease,
                        )
                        self._buffers += charge - job.buffer_bytes
                        job.buffer_bytes, job.request_json = 0, None
                        self._proposals[proposal_id] = proposal
                        prepared = None
                        job.state, job.proposal_id, job.operation_id = (
                            "reviewable",
                            proposal_id,
                            core.proposal_id,
                        )
                        job.effects = effects
                        job.result_json = _document({"proposal_id": proposal_id})
                else:
                    handle = job.handle
                    if job.native_action is not None:
                        result = commit_runtime(
                            handle,
                            job.run_id,
                            cancellation=job.token,
                            progress=lambda event, current_id=job.job_id: self._progress(
                                current_id, event
                            ),
                        )
                        with self._condition:
                            job.state, job.result_json = "completed", _document(result)
                        continue
                    if job.service_action != "transfer":
                        result = commit_service(handle, job.auth_lease)
                        result_json = _document(result)
                        with self._condition:
                            job.state, job.result_json = "completed", result_json
                        continue
                    if type(handle) is not PreparedOperation:
                        raise JobError("private_handle_required")
                    receipt = commit_operation(
                        handle,
                        cancellation=job.token,
                        progress=lambda event, current_id=job.job_id: self._progress(
                            current_id, event
                        ),
                    )
                    if type(receipt) is not TransferReceipt:
                        raise JobError("receipt_unavailable")
                    with self._condition:
                        job.state = "completed"
                        job.result_json = _document(receipt.to_dict())
            except OperationCancelledError:
                with self.lock:
                    entered = job.token.publishing or (
                        job.auth_lease is not None and job.auth_lease.service_entered
                    )
                    job.state = "uncertain" if entered else "cancelled"
                    job.result_json = _document(
                        {
                            "reason": "publication_unconfirmed"
                            if entered
                            else "cancelled_before_publication"
                        }
                    )
            except OperationNeedsReconcileError as error:
                with self.lock:
                    job.state = "uncertain"
                    if job.service_action != "transfer":
                        job.result_json = _document({"reason": "service_commit_unconfirmed"})
                        continue
                    job.operation_id = error.operation_id
                    job.result_json = _document(
                        {"reason": "needs_reconcile", "operation_id": error.operation_id}
                        if job.native_action is not None
                        else {"reason": "needs_reconcile", "receipt": asdict(error.receipt)}
                    )
            except RuntimeJobError as error:
                with self.lock:
                    job.state = "uncertain" if job.token.publishing else "failed"
                    job.result_json = _document(
                        {
                            "reason": "publication_unconfirmed"
                            if job.token.publishing
                            else error.reason
                        }
                    )
            except ServicePreparationError as error:
                with self.lock:
                    entered = job.auth_lease is not None and job.auth_lease.service_entered
                    if error.recovered:
                        job.subject = error.subject
                        job.operation_id = error.subject["id"]
                    job.state = (
                        "uncertain" if entered else "recovered" if error.recovered else "failed"
                    )
                    job.result_json = _document(
                        {
                            "reason": "service_commit_unconfirmed" if entered else error.reason,
                            "inspection": error.metadata,
                        }
                    )
            except Exception:
                with self.lock:
                    entered = job.token.publishing or (
                        job.auth_lease is not None and job.auth_lease.service_entered
                    )
                    cancelled = False
                    if job.service_action != "transfer" and not entered:
                        try:
                            job.token.check()
                        except OperationCancelledError:
                            cancelled = True
                    job.state = "uncertain" if entered else "cancelled" if cancelled else "failed"
                    job.result_json = _document(
                        {"reason": "publication_unconfirmed" if entered else "preparation_failed"}
                    )
            finally:
                # Do not retain a literal accepted/captured document in worker loop locals.
                request = captured = resolved = None
                if type(prepared) is PreparedOperation:
                    prepared.discard()
                service_handle = (
                    prepared
                    if type(prepared) in (PrivateTypedServiceHandle, PrivateRuntimeHandle)
                    else None
                )
                if job.state not in {"reviewable", "editable"} and (
                    job.service_action != "transfer" or job.native_action is not None
                ):
                    service_handle = service_handle or job.handle
                if service_handle is not None:
                    with self._condition:
                        charge, job.buffer_bytes = job.buffer_bytes, 0
                        job.handle = None
                    self._service_cleanup(service_handle, charge, job)
                with self._condition:
                    if job.state not in {"reviewable", "editable"}:
                        if job.handle is not None:
                            job.handle.discard()
                            job.handle = None
                        self._buffers -= job.buffer_bytes
                        job.buffer_bytes = 0
                        job.request_json = None
                    self._condition.notify_all()

    def close(self):
        with self._condition:
            if not self._closed:
                self._closed = True
                for job in self._jobs.values():
                    self._cancel(job)
                self._condition.notify_all()
        # A publication that crossed the token barrier retains worker ownership.
        self._worker.join()
