"""Authorization races and return flows use the real native publication barrier."""

import json
import threading
import time
from dataclasses import replace

import pytest
from test_web_runtime_jobs import accepted, captured, confirmed
from test_web_runtime_jobs import runtime as runtime_fixture

from session_migrate import handoff_operations
from session_migrate.catalog import Catalog
from session_migrate.lineage import LineageStore, SelectionRangeV1
from session_migrate.operations import OperationRequest
from session_migrate.profiles import SessionRef
from session_migrate.web import jobs, runtime_jobs


@pytest.fixture
def runtime(tmp_path):
    yield from runtime_fixture.__wrapped__(tmp_path)


def private_terminal(manager, job_id):
    deadline = time.monotonic() + 15
    with manager._condition:
        while time.monotonic() < deadline:
            job = manager._jobs[job_id]
            if job.state in {"completed", "failed", "uncertain", "cancelled"}:
                return job
            manager._condition.wait(timeout=0.05)
    pytest.fail("native worker did not finish")


@pytest.mark.parametrize("when", ["before", "after"])
def test_revoke_native_token_race_retains_real_core_after_publication(runtime, monkeypatch, when):
    manager, registry, context, request, _, _ = runtime
    original = runtime_jobs.commit_operation
    auth_original = jobs._AuthLease.check
    after_checks = []
    revoked = threading.Event()
    current = context("POST")

    def check(lease):
        if lease.commit_token is not None and lease.commit_token.publishing:
            after_checks.append(True)
        return auth_original(lease)

    def commit(handle, *, cancellation, progress):
        assert not cancellation.publishing
        if when == "before":
            registry.revoke(current)
            revoked.set()

        def at_progress(event):
            progress(event)
            if cancellation.publishing and not revoked.is_set():
                assert manager._jobs[confirm_job[0]].auth_lease.service_entered is False
                registry.revoke(current)
                revoked.set()

        return original(handle, cancellation=cancellation, progress=at_progress)

    # Obtain the new job id under the same lock used by its worker.
    confirm_job = []
    monkeypatch.setattr(jobs._AuthLease, "check", check)
    proposal = accepted(manager, context, captured(manager, context))[0]
    monkeypatch.setattr(runtime_jobs, "commit_operation", commit)
    with manager.lock:
        job = manager.confirm(
            proposal["proposal_id"],
            proposal["fingerprint"],
            "revoke-race",
            current,
            native_action="summary",
        ).to_dict()
        confirm_job.append(job["job_id"])
    result = private_terminal(manager, job["job_id"])
    assert revoked.is_set()
    if when == "before":
        assert result.state == "cancelled"
        assert not request.target_profile.root.exists()
        assert not (request.state_home / "lineage-v1").exists()
    else:
        assert result.state == "completed" and result.token.publishing and after_checks
        assert not result.auth_lease.service_entered
        assert len(LineageStore.inspect_existing(request.state_home / "lineage-v1").operations) == 1
    manager.close()
    assert manager._buffers == 0


def test_postpublication_native_guard_still_detects_changed_source(runtime, monkeypatch):
    manager, _, context, request, source_path, _ = runtime
    proposal = accepted(manager, context, captured(manager, context))[0]
    original = runtime_jobs.commit_operation
    changed = []

    def commit(handle, *, cancellation, progress):
        def at_progress(event):
            progress(event)
            if cancellation.publishing and not changed:
                changed.append(True)
                source_path.write_bytes(source_path.read_bytes().replace(b"original", b"modified"))

        return original(handle, cancellation=cancellation, progress=at_progress)

    monkeypatch.setattr(runtime_jobs, "commit_operation", commit)
    job = manager.confirm(
        proposal["proposal_id"],
        proposal["fingerprint"],
        "source-race",
        context("POST"),
        native_action="summary",
    ).to_dict()
    result = private_terminal(manager, job["job_id"])
    assert changed and result.state == "uncertain" and result.token.publishing
    assert json.loads(result.result_json)["reason"] == "needs_reconcile"
    assert not request.target_profile.root.exists()
    assert LineageStore.inspect_existing(request.state_home / "lineage-v1").operations


@pytest.mark.parametrize("mode", ["summary", "full"])
def test_return_new_uses_current_source_genuine_parent_and_new_target(runtime, mode):
    manager, _, context, request, source_path, _ = runtime
    first = confirmed(manager, context, accepted(manager, context, captured(manager, context))[0])
    parent_id = first["operation_id"]
    target_id = first["result"]["legacy_result"]["session_id"]
    current = SessionRef(
        request.target_profile.format,
        request.target_profile.root,
        target_id,
        request.target_profile.id,
        profile_fingerprint=request.target_profile.fingerprint,
    )
    with Catalog(request.catalog_path) as catalog:
        catalog.refresh(
            claude_roots=(request.source_profile.root,),
            codex_roots=(request.target_profile.root,),
            include_auto=False,
        )
    returning = replace(
        request,
        source_profile=request.target_profile,
        source_ref=current,
        target_profile=request.source_profile,
        options=OperationRequest(
            "transfer",
            cwd=request.options.cwd,
            target_cli_version="2.1.209",
            model_provider="anthropic",
            model="synthetic-return-model",
        ),
        source_client_version="0.144.4",
        selection=(SelectionRangeV1(0, 0, None),) if mode == "summary" else (),
        parent_operation_id=parent_id,
    )
    manager._runtime_resolver = lambda parameters, *, native_action: returning
    action = "return_new_" + mode
    if mode == "summary":
        capture = manager.capture(
            {"source_session_id": "current-parent-target"}, context("POST"), native_action=action
        ).to_dict()
        result = private_wait(manager, capture["job_id"], context, {"editable", "failed"})
        assert result["state"] == "editable", result
        draft = manager.handoff_document(result["result"]["draft_id"], context()).to_dict()
        proposal = accepted(manager, context, draft)[0]
    else:
        job = manager.prepare_runtime(
            {"source_session_id": "current-parent-target"}, context("POST"), native_action=action
        ).to_dict()
        result = private_wait(manager, job["job_id"], context, {"reviewable", "failed"})
        assert result["state"] == "reviewable", result
        proposal = manager.review(result["proposal_id"], context()).to_dict()
    second = confirmed(manager, context, proposal, key="native-return")
    new_id = second["result"]["legacy_result"]["session_id"]
    assert new_id not in (current.native_id, request.source_ref.native_id)
    view = LineageStore.inspect_existing(request.state_home / "lineage-v1")
    operation = next(
        op for op in view.operations if op.record.operation_id == second["operation_id"]
    )
    assert operation.receipt.return_link.parent_operation_id == parent_id
    assert source_path.exists() and len(list(request.source_profile.root.rglob("*.jsonl"))) == 2
    assert manager._buffers == 0


def private_wait(manager, job_id, context, states):
    from test_web_jobs import wait_state

    return wait_state(manager, job_id, context(), states)


def test_failed_close_retains_owned_capability_and_charge_until_retry(runtime, monkeypatch):
    manager, _, context, _, _, _ = runtime
    draft = captured(manager, context)
    handle = manager._drafts[draft["draft_id"]].handle
    original = Catalog.close
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def close(catalog):
        attempts.append(threading.get_ident())
        if len(attempts) == 1:
            entered.set()
            assert release.wait(10)
            raise OSError("synthetic failed close")
        return original(catalog)

    monkeypatch.setattr(Catalog, "close", close)
    try:
        manager.discard_draft(draft["draft_id"], context("POST"))
        assert entered.wait(10)
        assert manager._buffers == runtime_jobs.RUNTIME_BUFFER_BYTES
        assert handle in runtime_jobs._ISSUED
        assert manager._drafts[draft["draft_id"]].document is None
    finally:
        release.set()
    manager.close()
    assert manager._buffers == 0 and handle not in runtime_jobs._ISSUED
    assert len(attempts) == 2 and set(attempts) == {manager._worker.ident}


def test_prepare_callback_runs_original_profile_guard_before_external(runtime, monkeypatch):
    manager, _, context, request, source_path, _ = runtime
    original = handoff_operations._prepared_profile_binding
    calls = []

    def binding(*args, **kwargs):
        bound, target, source, guard, profiles = original(*args, **kwargs)

        def recorded():
            calls.append("original")
            guard()

        return bound, target, source, recorded, profiles

    def external():
        # Record that external authorization runs only after actual native profile/source checks.
        calls.append("external")
        assert calls[-2] == "original"

    monkeypatch.setattr(handoff_operations, "_prepared_profile_binding", binding)
    request = replace(request, binding_check=external)
    manager._runtime_resolver = lambda parameters, *, native_action: request
    # Capture has no profile operation yet. Start observing composition after capture.
    object.__setattr__(request, "binding_check", None)
    draft = captured(manager, context)
    # External _AuthLease is composed inside the core; spy rather than altering captured options.
    original_check = jobs._AuthLease.check

    def check(lease):
        if calls and calls[-1] == "original":
            external()
        return original_check(lease)

    monkeypatch.setattr(jobs._AuthLease, "check", check)
    proposal = accepted(manager, context, draft)[0]
    confirmed(manager, context, proposal)
    assert calls.count("original") >= 5 and calls.count("external") >= 5
    assert source_path.exists()
