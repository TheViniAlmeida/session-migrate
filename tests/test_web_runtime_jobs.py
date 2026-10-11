"""Native worker ownership exercised over genuine synthetic B3/C1 fixtures."""

import copy
import hashlib
import json
import threading
import time
from dataclasses import replace

import pytest
from test_f5a_runtime import fixture
from test_web_jobs import ORIGIN, wait_state

from session_migrate.catalog import Catalog
from session_migrate.lineage import LineageStore, SelectionRangeV1
from session_migrate.web import runtime_jobs
from session_migrate.web.auth import RunRegistry
from session_migrate.web.jobs import JobError, JobManager
from session_migrate.web.runtime_jobs import (
    RUNTIME_BUFFER_BYTES,
    PrivateRuntimeHandle,
    RuntimeJobError,
    RuntimeRequest,
    new_runtime,
    workflow_json,
)


@pytest.fixture
def runtime(tmp_path):
    catalog, _, source, target, ref, path, reviewed, options, env = fixture(tmp_path)
    reviewed.discard()
    catalog.close()
    request = RuntimeRequest(
        catalog.path,
        source,
        ref,
        target,
        options,
        tmp_path / "state",
        dict(env),
        dict(env),
        "2.1.209",
        (SelectionRangeV1(0, 1, None),),
    )
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

    resolved_threads = []

    def resolver(_parameters, *, native_action):
        resolved_threads.append(threading.get_ident())
        return request

    manager = JobManager(registry, lambda *args, **kwargs: None, runtime_resolver=resolver)
    yield manager, registry, context, request, path, resolved_threads
    manager.close()


def captured(manager, context):
    job = manager.capture({"source_session_id": "opaque-source"}, context("POST")).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"editable", "failed", "cancelled"})
    assert result["state"] == "editable", result
    return manager.handoff_document(result["result"]["draft_id"], context()).to_dict()


def accepted(manager, context, draft, body="Literal USER 👋\r\n<html>data</html>"):
    digest = hashlib.sha256(body.encode()).hexdigest()
    job = manager.accept_prepare(draft["draft_id"], body, digest, context("POST")).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"reviewable", "failed", "cancelled"})
    assert result["state"] == "reviewable", result
    return manager.review(result["proposal_id"], context()).to_dict(), job, body, digest


def confirmed(manager, context, proposal, *, key="native-once"):
    job = manager.confirm(
        proposal["proposal_id"],
        proposal["fingerprint"],
        key,
        context("POST"),
        native_action=proposal["native_action"],
    ).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"completed", "failed", "uncertain"})
    assert result["state"] == "completed", result
    return result


def test_genuine_native_summary_whole_document_and_exact_retry(runtime):
    manager, _, context, request, source_path, threads = runtime
    original = source_path.read_bytes()
    draft = captured(manager, context)
    assert "native original answer" in draft["document"]
    assert draft["document_sha256"] == hashlib.sha256(draft["document"].encode()).hexdigest()
    assert manager._buffers == RUNTIME_BUFFER_BYTES
    proposal, job, body, digest = accepted(manager, context, draft)
    retry = manager.accept_prepare(draft["draft_id"], body, digest, context("POST")).to_dict()
    assert retry["job_id"] == job["job_id"]
    with pytest.raises(JobError, match="idempotency_conflict"):
        manager.accept_prepare(draft["draft_id"], body + "different", digest, context("POST"))
    with pytest.raises(JobError, match="draft_unavailable"):
        manager.handoff_document(draft["draft_id"], context())
    # No native paths, RAW/body or private source authority are projected into normal DTOs.
    assert body not in json.dumps(proposal)
    assert str(source_path.parent) not in json.dumps(proposal)
    assert "source_identity" not in proposal["metadata"]["handoff"]
    result = confirmed(manager, context, proposal)
    assert result["native_action"] == "summary" and "service_action" not in result
    receipt_id = result["result"]["legacy_result"]["session_id"]
    targets = list(request.target_profile.root.rglob("*.jsonl"))
    assert len(targets) == 1 and receipt_id in targets[0].read_text()
    assert body in targets[0].read_text().replace("\\r", "\r").replace("\\n", "\n")
    view = LineageStore.inspect_existing(request.state_home / "lineage-v1")
    assert view.operations[0].receipt.derivation.input.document_sha256 == digest
    assert source_path.read_bytes() == original
    assert threads == [manager._worker.ident] and manager._buffers == 0
    replay = manager.confirm(
        proposal["proposal_id"],
        proposal["fingerprint"],
        "native-once",
        context("POST"),
        native_action="summary",
    ).to_dict()
    assert replay["job_id"] == result["job_id"]
    assert len(LineageStore.inspect_existing(request.state_home / "lineage-v1").operations) == 1


@pytest.mark.parametrize("phase", ["draft", "proposal"])
def test_source_changed_blocks_real_fresh_check_without_target(runtime, phase):
    manager, _, context, request, source_path, _ = runtime
    draft = captured(manager, context)
    proposal = accepted(manager, context, draft)[0] if phase == "proposal" else None
    source_path.write_bytes(source_path.read_bytes().replace(b"original", b"modified"))
    if proposal is None:
        job = manager.accept_prepare(
            draft["draft_id"],
            "USER unchanged",
            hashlib.sha256(b"USER unchanged").hexdigest(),
            context("POST"),
        ).to_dict()
    else:
        job = manager.confirm(
            proposal["proposal_id"],
            proposal["fingerprint"],
            "stale",
            context("POST"),
            native_action="summary",
        ).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"failed", "uncertain", "cancelled"})
    assert result["state"] == "failed"
    assert not request.target_profile.root.exists()
    assert not (request.state_home / "lineage-v1").exists()


@pytest.mark.parametrize("change", ["source_profile", "options", "parent"])
def test_closed_selection_and_options_cannot_change_after_capture(runtime, change):
    manager, _, context, request, _, _ = runtime
    draft = captured(manager, context)
    if change == "source_profile":
        object.__setattr__(request.source_profile, "name", "mutated")
    elif change == "options":
        object.__setattr__(request.options, "model", "mutated")
    else:
        object.__setattr__(request, "parent_operation_id", "unissued-parent")
    job = manager.accept_prepare(
        draft["draft_id"], "USER", hashlib.sha256(b"USER").hexdigest(), context("POST")
    ).to_dict()
    assert wait_state(manager, job["job_id"], context(), {"failed"})["state"] == "failed"
    assert not request.target_profile.root.exists()


def test_native_confirm_requires_bound_action_and_fresh_one_shot_token(runtime):
    manager, _, context, _, _, _ = runtime
    proposal = accepted(manager, context, captured(manager, context))[0]
    with pytest.raises(JobError, match="proposal_unavailable"):
        manager.confirm(proposal["proposal_id"], proposal["fingerprint"], "wrong", context("POST"))
    with pytest.raises(JobError, match="proposal_unavailable"):
        manager.confirm(
            proposal["proposal_id"],
            proposal["fingerprint"],
            "wrong",
            context("POST"),
            native_action="return_new_summary",
        )
    result = confirmed(manager, context, proposal)
    assert result["state"] == "completed"


def test_digest_failure_consumes_same_draft_and_exact_retry_is_same_failure(runtime):
    manager, _, context, request, _, _ = runtime
    draft = captured(manager, context)
    job = manager.accept_prepare(draft["draft_id"], "USER", "0" * 64, context("POST")).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"failed"})
    assert (
        manager.accept_prepare(draft["draft_id"], "USER", "0" * 64, context("POST")).to_dict()[
            "job_id"
        ]
        == result["job_id"]
    )
    assert not request.target_profile.root.exists() and manager._buffers == 0


def test_utf8_budget_bounds_actual_envelope_without_normalization(runtime):
    manager, _, context, _, _, _ = runtime
    # NonASCII document fits the actual UTF-8 wire; ensure_ascii would exceed 64KiB.
    draft = captured(manager, context)
    body = "é" * 25000
    proposal, _, _, digest = accepted(manager, context, draft, body)
    assert proposal["metadata"]["handoff"]["document_sha256"] == digest
    assert len(workflow_json({"document": body}).encode()) < 65536
    assert len(json.dumps({"document": body}).encode()) > 65536
    with pytest.raises(RuntimeJobError, match="request_invalid"):
        workflow_json({"document": '"' * 59000})
    with pytest.raises(JobError, match="request_invalid"):
        manager.accept_prepare(draft["draft_id"], '"' * 59000, "0" * 64, context("POST"))
    with pytest.raises(JobError, match="request_invalid"):
        manager.accept_prepare(draft["draft_id"], "é" * 30001, "0" * 64, context("POST"))


def test_expiry_discard_revoke_all_close_catalog_on_owner_thread(runtime, monkeypatch):
    manager, registry, context, _, _, _ = runtime
    closed = []
    original = Catalog.close

    def close(catalog):
        closed.append(threading.get_ident())
        return original(catalog)

    monkeypatch.setattr(Catalog, "close", close)
    ticks = [0.0]
    manager._clock = lambda: ticks[0]
    draft = captured(manager, context)
    ticks[0] = 301.0
    with pytest.raises(JobError, match="draft_unavailable"):
        manager.handoff_document(draft["draft_id"], context())
    deadline = time.monotonic() + 10
    while manager._buffers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager._buffers == 0
    draft = captured(manager, context)
    manager.discard_draft(draft["draft_id"], context("POST"))
    while manager._buffers and time.monotonic() < deadline:
        time.sleep(0.01)
    draft = captured(manager, context)
    registry.revoke(context("POST"))
    manager.close()
    assert manager._buffers == 0 and closed and set(closed) == {manager._worker.ident}
    assert all(d.document is None and d.handle is None for d in manager._drafts.values())


def test_closed_private_constructor_copy_and_wrong_thread(runtime):
    manager, _, context, request, _, _ = runtime
    with pytest.raises(RuntimeJobError):
        PrivateRuntimeHandle()
    draft = captured(manager, context)
    handle = manager._drafts[draft["draft_id"]].handle
    with pytest.raises(RuntimeJobError, match="worker_ownership_required"):
        handle.discard()
    with pytest.raises(RuntimeJobError):
        runtime_jobs.runtime_metadata(copy.copy(handle))
    with pytest.raises(RuntimeJobError, match="request_invalid"):
        new_runtime(replace(request, selection=({"first_event": 0},)), "summary", "run")
    with pytest.raises(RuntimeJobError, match="request_invalid"):
        new_runtime(
            replace(request, options=replace(request.options, path=request.catalog_path)),
            "summary",
            "run",
        )


def test_accept_enqueue_rollback_precedes_core_only(runtime, monkeypatch):
    manager, _, context, _, _, _ = runtime
    draft = captured(manager, context)
    original = manager._condition.notify

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic enqueue failure")

    monkeypatch.setattr(manager._condition, "notify", failed)
    with pytest.raises(JobError, match="enqueue_failed"):
        manager.accept_prepare(
            draft["draft_id"], "USER", hashlib.sha256(b"USER").hexdigest(), context("POST")
        )
    monkeypatch.setattr(manager._condition, "notify", original)
    assert manager.handoff_document(draft["draft_id"], context()).to_dict() == draft
    proposal = accepted(manager, context, draft)[0]
    confirmed(manager, context, proposal)


def test_draft_slots_run_binding_and_untyped_resolver_fail_closed(runtime):
    manager, registry, context, request, _, _ = runtime
    first = captured(manager, context)
    second = captured(manager, context)
    assert manager._buffers == 2 * RUNTIME_BUFFER_BYTES
    with pytest.raises(JobError, match="draft_capacity"):
        manager.capture({"source_session_id": "third"}, context("POST"))
    other_credential = registry.create(ORIGIN, frozenset({"readonly", "publish"}))
    other_payload = json.loads(registry._bootstrap_payload(other_credential))
    other = registry.authenticate(
        "Bearer " + other_payload["access_token"], host="127.0.0.1:8021", origin=None, method="GET"
    )
    with pytest.raises(JobError, match="draft_unavailable"):
        manager.handoff_document(first["draft_id"], other)
    manager.discard_draft(first["draft_id"], context("POST"))
    manager.discard_draft(second["draft_id"], context("POST"))
    manager._runtime_resolver = lambda parameters, *, native_action: {"options": request.options}
    # Cleanup has priority over this next capture, including releasing its charge.
    deadline = time.monotonic() + 10
    while manager._buffers and time.monotonic() < deadline:
        time.sleep(0.01)
    job = manager.capture({"source_session_id": "untyped"}, context("POST")).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"failed"})
    assert result["result"]["reason"] == "request_invalid"
    assert not request.target_profile.root.exists()


def test_closed_launch_parent_binding_checked_again_before_proposal(runtime):
    manager, _, context, request, _, _ = runtime
    binding = ["original"]

    def check():
        if binding[0] != "original":
            raise RuntimeJobError("source_changed")

    request = replace(request, binding_check=check)
    manager._runtime_resolver = lambda parameters, *, native_action: request
    draft = captured(manager, context)
    binding[0] = "current-launch-parent-changed"
    job = manager.accept_prepare(
        draft["draft_id"], "USER", hashlib.sha256(b"USER").hexdigest(), context("POST")
    ).to_dict()
    result = wait_state(manager, job["job_id"], context(), {"failed"})
    assert result["result"]["reason"] == "source_changed"
    assert not request.target_profile.root.exists()


@pytest.mark.parametrize("pause_at", ["core_return", "proposal_admission"])
def test_cancel_late_native_preparation_discards_result_with_closed_core_token(
    runtime, monkeypatch, pause_at
):
    manager, _, context, request, _, _ = runtime
    draft = captured(manager, context)
    returned, release = threading.Event(), threading.Event()
    issued = []
    original_core = runtime_jobs.prepare_summary

    def core(*args, **kwargs):
        handle = original_core(*args, **kwargs)
        issued.append(handle)
        if pause_at == "core_return":
            returned.set()
            assert release.wait(10)
        return handle

    original_admission = manager._runtime_prepared

    def admission(*args, **kwargs):
        if pause_at == "proposal_admission":
            returned.set()
            assert release.wait(10)
        return original_admission(*args, **kwargs)

    monkeypatch.setattr(runtime_jobs, "prepare_summary", core)
    monkeypatch.setattr(manager, "_runtime_prepared", admission)
    body = "Whole genuine USER cancelled before review"
    digest = hashlib.sha256(body.encode()).hexdigest()
    job = manager.accept_prepare(draft["draft_id"], body, digest, context("POST")).to_dict()
    try:
        assert returned.wait(10)
        assert issued and not issued[0]._consumed
        assert manager._jobs[job["job_id"]].token.request_cancel() is False
        assert not manager._jobs[job["job_id"]].token.publishing
        requested = manager.cancel(job["job_id"], context("POST")).to_dict()
        assert requested["state"] == "preparing"
        assert manager._buffers == RUNTIME_BUFFER_BYTES
    finally:
        release.set()
    final = wait_state(manager, job["job_id"], context(), {"cancelled", "failed", "reviewable"})
    assert final["state"] == "cancelled" and final["proposal_id"] is None
    assert not any(not proposal.consumed for proposal in manager._proposals.values())
    replay = manager.accept_prepare(draft["draft_id"], body, digest, context("POST")).to_dict()
    assert replay["job_id"] == job["job_id"] and replay["state"] == "cancelled"
    manager.close()
    assert issued[0]._consumed and manager._buffers == 0
    assert not request.target_profile.root.exists()
    assert not (request.state_home / "lineage-v1").exists()
