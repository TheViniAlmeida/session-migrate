"""Genuine C2 services run on one persistent worker over private B3 fixtures."""

import copy
import json
import threading

import pytest
import test_c2_services as fixtures
from test_web_jobs import ORIGIN, wait_state
from test_web_service_cancel import ready_fixture

from session_migrate import adoption, reconciliation
from session_migrate.baselines import BaselineStore
from session_migrate.catalog import Catalog
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.lineage import LineageStore, NativeState, OperationState
from session_migrate.web import jobs, service_jobs
from session_migrate.web.auth import RunRegistry
from session_migrate.web.jobs import JobError, JobManager
from session_migrate.web.service_jobs import SERVICE_CAPTURE_BYTES, prepare_service


def authorization():
    registry = RunRegistry()
    credential = registry.create(ORIGIN, frozenset({"readonly", "publish"}))
    payload = json.loads(registry._bootstrap_payload(credential))

    def context(method="GET"):
        return registry.authenticate(
            "Bearer " + payload["access_token"],
            host="127.0.0.1:8021",
            origin=ORIGIN if method == "POST" else None,
            method=method,
            csrf_nonce=payload["csrf_nonce"] if method == "POST" else None,
        )

    return registry, context


def service_fixture(tmp_path, monkeypatch, kind):
    if kind == "adopt":
        pair, observer, home, catalog, _ = fixtures.adoption_fixture(tmp_path)
        parameters = {
            "pair": pair,
            "state_home": home,
            "source_client_version": "2.1.209",
            "target_client_version": "2.1.209",
        }
        subject = home
    elif kind == "reconcile":
        operation_id, path, context, observer, catalog = fixtures.profile_recovery_fixture(
            tmp_path, monkeypatch
        )
        parameters = {"operation_id": operation_id, "store_path": path, "context": context}
        subject = path
    else:
        record, path, digest, store_uuid = ready_fixture(tmp_path)
        return (
            {
                "operation_id": record.operation_id,
                "store_path": path,
                "expected_record_sha256": digest,
                "expected_store_uuid": store_uuid,
            },
            None,
            path,
        )
    selected = (
        catalog.path,
        observer._profiles,
        dict(observer._environment),
        observer._legacy_runtime_contexts,
    )
    catalog.close()
    return parameters, selected, subject


def preparer_for(kind, parameters, selected, *, opened=None):
    def preparer(request, *, service_action, cancellation, prepublication_check, progress):
        assert service_action == kind
        cancellation.check()
        prepublication_check()
        observer = None
        if selected is not None:
            path, profiles, environment, legacy = selected
            catalog = Catalog.open_readonly_existing(path)
            if opened is not None:
                opened.append((catalog, threading.get_ident()))
            observer = EndpointObserver(
                catalog,
                profiles,
                base_environ=environment,
                byte_limit=SERVICE_CAPTURE_BYTES,
                legacy_runtime_contexts=legacy,
            )
        return prepare_service(kind, observer=observer, binding=request, **parameters)

    return preparer


def review(manager, context, kind):
    job = manager.prepare(
        {"source_session_id": "selected"}, context("POST"), service_action=kind
    ).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"reviewable", "failed", "recovered"})
    assert result["state"] == "reviewable", result
    return manager.review(result["proposal_id"], context()).to_dict()


def confirm(manager, context, proposal, kind, key="once"):
    return manager.confirm(
        proposal["proposal_id"], proposal["fingerprint"], key, context("POST"), service_action=kind
    ).to_dict()


@pytest.mark.parametrize("kind", ("adopt", "reconcile", "cancel_ready"))
def test_genuine_service_once_bound_subject_and_no_native(tmp_path, monkeypatch, kind):
    parameters, selected, path = service_fixture(tmp_path, monkeypatch, kind)
    fixtures.no_native(monkeypatch)
    registry, context = authorization()
    manager = JobManager(registry, preparer_for(kind, parameters, selected))
    try:
        proposal = review(manager, context, kind)
        assert proposal["service_action"] == kind
        assert "canonical_root" not in json.dumps(proposal)
        assert str(tmp_path) not in json.dumps(proposal)
        if kind == "adopt":
            assert proposal["operation_id"] is None and proposal["subject"]["kind"] == "baseline"
            assert not path.exists()
        job = confirm(manager, context, proposal, kind)
        result = wait_state(manager, job["job_id"], context(), {"completed", "failed", "uncertain"})
        assert result["state"] == "completed", result
        assert result["subject"] == proposal["subject"]
        assert str(tmp_path) not in json.dumps(result)
        assert confirm(manager, context, proposal, kind)["job_id"] == job["job_id"]
        with pytest.raises(JobError, match="idempotency_conflict"):
            confirm(manager, context, proposal, "transfer")
        if kind == "adopt":
            view = LineageStore.inspect_existing(path / "lineage-v1")
            assert view.operations == ()
            baseline = BaselineStore.inspect_existing(
                path / "baselines-v1", coordinator_store_uuid=view.store_uuid
            )
            assert len(baseline.baselines) == 1
            assert result["result"]["receipt_projection"]["provenance"] == "adopted_now"
        else:
            operation = reconciliation.inspect_recovered_operation(
                parameters["operation_id"], store_path=path
            )
            assert operation.durable_state is (
                OperationState.COMPLETED if kind == "reconcile" else OperationState.CANCELLED
            )
            assert operation.durable_native_state is (
                NativeState.ACKNOWLEDGED if kind == "reconcile" else NativeState.NATIVE_NOT_STARTED
            )
    finally:
        manager.close()
    assert manager._buffers == 0 and not manager._worker.is_alive()


@pytest.mark.parametrize("close_action", ("cancel", "revoke", "expire", "close"))
def test_cleanup_runs_on_owner_and_charge_survives_until_close(tmp_path, monkeypatch, close_action):
    parameters, selected, path = service_fixture(tmp_path, monkeypatch, "adopt")
    opened, closed = [], []
    closing, release = threading.Event(), threading.Event()
    original = Catalog.close

    def held_close(catalog):
        closing.set()
        assert release.wait(10)
        closed.append(threading.get_ident())
        return original(catalog)

    monkeypatch.setattr(Catalog, "close", held_close)
    registry, context = authorization()
    ticks = [0.0]
    manager = JobManager(
        registry, preparer_for("adopt", parameters, selected, opened=opened), clock=lambda: ticks[0]
    )
    closer = None
    try:
        proposal = review(manager, context, "adopt")
        if close_action == "cancel":
            manager.cancel(proposal["preparation_job_id"], context("POST"))
        elif close_action == "revoke":
            registry.revoke(context("POST"))
        elif close_action == "expire":
            ticks[0] = 300
            with pytest.raises(JobError, match="proposal_unavailable"):
                manager.review(proposal["proposal_id"], context())
        else:
            closer = threading.Thread(target=manager.close)
            closer.start()
        assert closing.wait(10)
        assert manager._buffers == service_jobs.SERVICE_BUFFER_BYTES
        release.set()
    finally:
        release.set()
        if closer is not None:
            closer.join(10)
        manager.close()
    assert closed == [opened[0][1]] and manager._buffers == 0 and not path.exists()


@pytest.mark.parametrize("kind", ("adopt", "reconcile", "cancel_ready"))
@pytest.mark.parametrize("after_entry", (False, True))
def test_cancel_and_revoke_arbitrate_entry_and_keep_active_owner(
    tmp_path, monkeypatch, kind, after_entry
):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, kind)
    registry, context = authorization()
    manager = JobManager(registry, preparer_for(kind, parameters, selected))
    held, release = threading.Event(), threading.Event()
    original = jobs._AuthLease.enter_service

    def paused(lease, bundle):
        if after_entry:
            original(lease, bundle)
        held.set()
        assert release.wait(10)
        if not after_entry:
            original(lease, bundle)

    monkeypatch.setattr(jobs._AuthLease, "enter_service", paused)
    try:
        proposal = review(manager, context, kind)
        job = confirm(manager, context, proposal, kind)
        assert held.wait(10)
        cancelled = manager.cancel(job["job_id"], context("POST")).to_dict()
        assert cancelled["state"] == ("service_commit_entered" if after_entry else "committing")
        registry.revoke(context("POST"))
        assert manager._jobs[job["job_id"]].handle is not None
        release.set()
    finally:
        release.set()
        manager.close()
    assert manager._jobs[job["job_id"]].state == ("completed" if after_entry else "cancelled")
    assert manager._buffers == 0


@pytest.mark.parametrize("kind", ("adopt", "reconcile", "cancel_ready"))
def test_unknown_return_after_entry_is_uncertain_same_subject(tmp_path, monkeypatch, kind):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, kind)
    registry, context = authorization()
    manager = JobManager(registry, preparer_for(kind, parameters, selected))
    if kind == "adopt":
        monkeypatch.setattr(adoption, "commit_adoption", lambda _: {"fake": "receipt"})
    elif kind == "reconcile":
        monkeypatch.setattr(reconciliation, "commit_reconciliation", lambda _: None)
    else:
        monkeypatch.setattr(reconciliation, "cancel_recovered_ready", lambda *args, **kwargs: None)
    try:
        proposal = review(manager, context, kind)
        job = confirm(manager, context, proposal, kind)
        result = wait_state(manager, job["job_id"], context(), {"completed", "failed", "uncertain"})
        assert result["state"] == "uncertain" and result["subject"] == proposal["subject"]
    finally:
        manager.close()


def test_private_bundle_cannot_be_forged_or_copied(tmp_path, monkeypatch):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, "adopt")
    with pytest.raises(service_jobs.ServicePreparationError):
        service_jobs.PrivateTypedServiceHandle()
    registry, context = authorization()
    real = preparer_for("adopt", parameters, selected)

    def copied(request, **kwargs):
        bundle = real(request, **kwargs)
        try:
            return copy.copy(bundle)
        finally:
            bundle.discard()

    manager = JobManager(registry, copied)
    try:
        job = manager.prepare({}, context("POST"), service_action="adopt").to_dict()
        assert wait_state(manager, job["job_id"], context(), {"failed"})["state"] == "failed"
    finally:
        manager.close()


@pytest.mark.parametrize("kind", ("adopt", "reconcile"))
def test_serializer_fault_after_actual_commit_keeps_uncertain_subject(tmp_path, monkeypatch, kind):
    parameters, selected, path = service_fixture(tmp_path, monkeypatch, kind)
    registry, context = authorization()
    manager = JobManager(registry, preparer_for(kind, parameters, selected))
    serialized, core_errors = [], []
    try:
        proposal = review(manager, context, kind)

        if kind == "reconcile":
            original = reconciliation.commit_reconciliation

            def traced_commit(handle):
                try:
                    return original(handle)
                except reconciliation.ReconciliationError as error:
                    core_errors.append(error.code)
                    raise

            monkeypatch.setattr(reconciliation, "commit_reconciliation", traced_commit)

        def fail_serializer(_receipt):
            serialized.append(type(_receipt).__name__)
            raise ValueError("synthetic serializer fault")

        monkeypatch.setattr(service_jobs, "_receipt", fail_serializer)
        job = confirm(manager, context, proposal, kind)
        result = wait_state(manager, job["job_id"], context(), {"completed", "failed", "uncertain"})
        assert result["state"] == "uncertain" and result["subject"] == proposal["subject"]
        assert serialized, {"core_errors": core_errors, "state": result["state"]}
        if kind == "adopt":
            assert adoption.inspect_adoption(proposal["subject"]["id"], state_home=path) is not None
        else:
            assert (
                reconciliation.inspect_recovered_operation(
                    proposal["subject"]["id"], store_path=path
                ).durable_state
                is OperationState.COMPLETED
            )
    finally:
        manager.close()


@pytest.mark.parametrize("after_entry", (False, True))
def test_deadline_and_close_preserve_admitted_core_worker(tmp_path, monkeypatch, after_entry):
    parameters, selected, path = service_fixture(tmp_path, monkeypatch, "adopt")
    registry, context = authorization()
    ticks = [0.0]
    manager = JobManager(
        registry, preparer_for("adopt", parameters, selected), clock=lambda: ticks[0]
    )
    held, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = jobs._AuthLease.enter_service
    closer = None

    def paused(lease, bundle):
        if after_entry:
            original(lease, bundle)
        held.set()
        assert release.wait(10)
        if not after_entry:
            original(lease, bundle)

    monkeypatch.setattr(jobs._AuthLease, "enter_service", paused)
    try:
        proposal = review(manager, context, "adopt")
        job = confirm(manager, context, proposal, "adopt")
        assert held.wait(10)
        ticks[0] = 31.0
        if after_entry:

            def close():
                manager.close()
                closed.set()

            closer = threading.Thread(target=close)
            closer.start()
            assert not closed.wait(0.05)
        release.set()
        if after_entry:
            assert closed.wait(10)
            assert manager._jobs[job["job_id"]].state == "completed"
        else:
            assert wait_state(manager, job["job_id"], context(), {"failed"})["state"] == "failed"
            assert not path.exists()
    finally:
        release.set()
        if closer is not None:
            closer.join(10)
        manager.close()
    assert manager._buffers == 0


def test_service_idempotency_precedes_ttl_and_enqueue_rollback(tmp_path, monkeypatch):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, "adopt")
    registry, context = authorization()
    ticks = [0.0]
    manager = JobManager(
        registry, preparer_for("adopt", parameters, selected), clock=lambda: ticks[0]
    )
    try:
        proposal = review(manager, context, "adopt")
        notify = manager._condition.notify
        monkeypatch.setattr(
            manager._condition,
            "notify",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic enqueue")),
        )
        with pytest.raises(JobError, match="enqueue_failed"):
            confirm(manager, context, proposal, "adopt")
        assert manager.review(proposal["proposal_id"], context()).to_dict()["state"] == "reviewable"
        assert manager._keys == {}
        monkeypatch.setattr(manager._condition, "notify", notify)
        job = confirm(manager, context, proposal, "adopt")
        assert (
            wait_state(manager, job["job_id"], context(), {"completed", "uncertain"})["state"]
            == "completed"
        )
        ticks[0] = 301
        assert confirm(manager, context, proposal, "adopt")["job_id"] == job["job_id"]
    finally:
        manager.close()


def test_queued_service_cancel_keeps_catalog_charge_until_worker_cleanup(tmp_path, monkeypatch):
    parameters, selected, path = service_fixture(tmp_path, monkeypatch, "adopt")
    registry, context = authorization()
    held, release = threading.Event(), threading.Event()
    real = preparer_for("adopt", parameters, selected)

    def preparer(request, **callbacks):
        if request.get("hold"):
            held.set()
            assert release.wait(10)
            return None  # Only a blocked prep; never a fabricated capability.
        return real(request, **callbacks)

    manager = JobManager(registry, preparer)
    try:
        proposal = review(manager, context, "adopt")
        manager.prepare({"hold": "queue"}, context("POST"))
        assert held.wait(10)
        job = confirm(manager, context, proposal, "adopt")
        assert manager.cancel(job["job_id"], context("POST")).to_dict()["state"] == "cancelled"
        assert manager._buffers >= service_jobs.SERVICE_BUFFER_BYTES
        assert len(manager._cleanup) == 1 and not path.exists()
    finally:
        release.set()
        manager.close()
    assert manager._buffers == 0 and not path.exists()


def test_missing_original_context_never_issues_reviewable_reconcile(tmp_path, monkeypatch):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, "reconcile")
    parameters["context"] = None
    registry, context = authorization()
    manager = JobManager(registry, preparer_for("reconcile", parameters, selected))
    try:
        job = manager.prepare({}, context("POST"), service_action="reconcile").to_dict()
        result = wait_state(manager, job["job_id"], context(), {"failed"})
        assert result["result"]["reason"] == "context_unavailable"
        assert manager._proposals == {}
    finally:
        manager.close()


def test_reconcile_completed_is_inspection_and_has_no_confirmable_handle(tmp_path, monkeypatch):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, "reconcile")
    registry, context = authorization()
    manager = JobManager(registry, preparer_for("reconcile", parameters, selected))
    try:
        proposal = review(manager, context, "reconcile")
        job = confirm(manager, context, proposal, "reconcile")
        assert (
            wait_state(manager, job["job_id"], context(), {"completed", "uncertain"})["state"]
            == "completed"
        )
        inspected = manager.prepare({}, context("POST"), service_action="reconcile").to_dict()
        result = wait_state(manager, inspected["job_id"], context(), {"recovered", "failed"})
        assert result["state"] == "recovered" and result["proposal_id"] is None
        assert (
            result["result"]["inspection"]["receipt_projection"]["operation_id"]
            == parameters["operation_id"]
        )
    finally:
        manager.close()


def test_source_changed_after_review_cannot_substitute_proof(tmp_path, monkeypatch):
    parameters, selected, path = service_fixture(tmp_path, monkeypatch, "adopt")
    registry, context = authorization()
    manager = JobManager(registry, preparer_for("adopt", parameters, selected))
    try:
        proposal = review(manager, context, "adopt")
        source = next(parameters["pair"].source.root.rglob("*.jsonl"))
        source.write_bytes(source.read_bytes().replace(b"synthetic answer", b"synthetic changed"))
        job = confirm(manager, context, proposal, "adopt")
        result = wait_state(manager, job["job_id"], context(), {"completed", "uncertain", "failed"})
        assert result["state"] == "uncertain" and result["subject"] == proposal["subject"]
        assert not path.exists()
    finally:
        manager.close()


def test_cleanup_exception_retains_owner_and_charge_until_real_close(tmp_path, monkeypatch):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, "adopt")
    registry, context = authorization()
    manager = JobManager(registry, preparer_for("adopt", parameters, selected))
    attempted, release = threading.Event(), threading.Event()
    original = Catalog.close
    attempts = []

    def close(catalog):
        attempts.append(threading.get_ident())
        if len(attempts) == 1:
            attempted.set()
            raise RuntimeError("synthetic first close fault")
        assert release.wait(10)
        return original(catalog)

    try:
        proposal = review(manager, context, "adopt")
        monkeypatch.setattr(Catalog, "close", close)
        manager.cancel(proposal["preparation_job_id"], context("POST"))
        assert attempted.wait(10)
        assert manager._buffers == service_jobs.SERVICE_BUFFER_BYTES
        release.set()
    finally:
        release.set()
        manager.close()
    assert len(attempts) == 2 and set(attempts) == {manager._worker.ident}
    assert manager._buffers == 0 and not manager._worker.is_alive()


def test_service_admission_reserves_buffers_before_read_or_handle(tmp_path, monkeypatch):
    parameters, selected, _ = service_fixture(tmp_path, monkeypatch, "adopt")
    registry, context = authorization()
    held, release = threading.Event(), threading.Event()
    calls = []
    real = preparer_for("adopt", parameters, selected)

    def paused(request, **callbacks):
        calls.append(threading.get_ident())
        held.set()
        assert release.wait(10)
        return real(request, **callbacks)

    manager = JobManager(registry, paused)
    try:
        first = manager.prepare({}, context("POST"), service_action="adopt").to_dict()
        assert held.wait(10)
        second = manager.prepare({}, context("POST"), service_action="adopt").to_dict()
        assert manager._buffers == jobs.MAX_BUFFER_BYTES
        with pytest.raises(JobError, match="buffer_capacity"):
            manager.prepare({}, context("POST"), service_action="adopt")
        assert len(calls) == 1
        manager.cancel(second["job_id"], context("POST"))
        manager.cancel(first["job_id"], context("POST"))
        release.set()
        assert (
            wait_state(manager, first["job_id"], context(), {"cancelled"})["state"] == "cancelled"
        )
    finally:
        release.set()
        manager.close()
    assert manager._buffers == 0
