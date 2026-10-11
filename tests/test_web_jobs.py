"""Job ownership uses actual prepared file operations and synthetic private roots."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from session_migrate.operations import OperationRequest, prepare_operation
from session_migrate.web import jobs
from session_migrate.web.auth import RunRegistry
from session_migrate.web.jobs import JobError, JobManager

ORIGIN = "http://127.0.0.1:8021"
FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"


@pytest.fixture
def launched(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_bytes(FIXTURE.read_bytes())
    env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "TMPDIR"):
        directory = tmp_path / name.lower()
        directory.mkdir(mode=0o700)
        env[name] = str(directory)
    request = OperationRequest(
        "import", path=source, target_format="codex", home=tmp_path / "target", cwd=tmp_path
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

    issued = []

    def preparer(_selected, **kwargs):
        handle = prepare_operation(request, environ=env, **kwargs)
        issued.append(handle)
        return handle

    return registry, context, preparer, issued, request


def wait_state(manager, job_id, context, wanted, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dto = manager.get(job_id, context).to_dict()
        if dto["state"] in wanted:
            return dto
        time.sleep(0.01)
    pytest.fail("bounded job did not reach the required state")


def review(manager, context):
    prepared = manager.prepare(
        {"source_session_id": "selected", "target_profile_id": "target"}, context("POST")
    ).to_dict()
    result = wait_state(manager, prepared["job_id"], context(), {"reviewable", "failed"})
    assert result["state"] == "reviewable"
    return manager.review(result["proposal_id"], context()).to_dict()


def test_real_core_prepare_confirm_once_and_metadata_only(launched):
    registry, context, preparer, issued, request = launched
    manager = JobManager(registry, preparer)
    try:
        proposal = review(manager, context)
        assert not request.home.exists()
        first = manager.confirm(
            proposal["proposal_id"], proposal["fingerprint"], "confirm-key", context("POST")
        ).to_dict()
        result = wait_state(
            manager, first["job_id"], context(), {"completed", "failed", "uncertain"}
        )
        assert result["state"] == "completed"
        assert Path(result["result"]["legacy_result"]["output"]).is_file()
        repeated = manager.confirm(
            proposal["proposal_id"], proposal["fingerprint"], "confirm-key", context("POST")
        ).to_dict()
        assert repeated["job_id"] == first["job_id"] and repeated["state"] == "completed"
        assert len(issued) == 1 and manager._buffers == 0
    finally:
        manager.close()


def test_same_key_after_ttl_and_conflicting_key_denied(launched):
    registry, context, preparer, _, _ = launched
    ticks = [0.0]
    manager = JobManager(registry, preparer, clock=lambda: ticks[0])
    try:
        proposal = review(manager, context)
        result = manager.confirm(
            proposal["proposal_id"], proposal["fingerprint"], "stable", context("POST")
        ).to_dict()
        wait_state(manager, result["job_id"], context(), {"completed", "failed", "uncertain"})
        ticks[0] = 301.0
        replay = manager.confirm(
            proposal["proposal_id"], proposal["fingerprint"], "stable", context("POST")
        ).to_dict()
        assert replay["job_id"] == result["job_id"]
        with pytest.raises(JobError, match="idempotency_conflict"):
            manager.confirm(proposal["proposal_id"], "0" * 64, "stable", context("POST"))
    finally:
        manager.close()


def test_two_keys_cannot_take_same_handle(launched):
    registry, context, preparer, issued, _ = launched
    manager = JobManager(registry, preparer)
    try:
        proposal = review(manager, context)
        gate = threading.Barrier(3)
        outcomes = []

        def confirm(key):
            current = context("POST")
            gate.wait()
            try:
                outcomes.append(
                    manager.confirm(
                        proposal["proposal_id"], proposal["fingerprint"], key, current
                    ).to_dict()
                )
            except JobError as error:
                outcomes.append(error.reason)

        workers = [threading.Thread(target=confirm, args=(key,)) for key in ("a", "b")]
        for worker in workers:
            worker.start()
        gate.wait()
        for worker in workers:
            worker.join(timeout=10)
        assert sum(type(value) is dict for value in outcomes) == 1
        assert "proposal_unavailable" in outcomes and len(issued) == 1
        job_id = next(value["job_id"] for value in outcomes if type(value) is dict)
        wait_state(manager, job_id, context(), {"completed", "failed", "uncertain"})
    finally:
        manager.close()


def test_cancel_review_discards_only_its_handle(launched):
    registry, context, preparer, issued, _ = launched
    manager = JobManager(registry, preparer)
    try:
        proposal = review(manager, context)
        cancelled = manager.cancel(proposal["preparation_job_id"], context("POST")).to_dict()
        assert cancelled["state"] == "cancelled" and issued[0]._consumed
        with pytest.raises(JobError, match="proposal_unavailable"):
            manager.review(proposal["proposal_id"], context())
        assert manager._buffers == 0
    finally:
        manager.close()


def test_enqueue_failure_preserves_review(launched, monkeypatch):
    registry, context, preparer, issued, _ = launched
    manager = JobManager(registry, preparer)
    try:
        proposal = review(manager, context)
        original = manager._condition.notify

        def fail(*_):
            raise RuntimeError("synthetic notify failure")

        monkeypatch.setattr(manager._condition, "notify", fail)
        with pytest.raises(JobError, match="enqueue_failed"):
            manager.confirm(
                proposal["proposal_id"], proposal["fingerprint"], "retry-key", context("POST")
            )
        assert manager.review(proposal["proposal_id"], context()).to_dict()["state"] == "reviewable"
        assert not issued[0]._consumed and manager._keys == {}
        monkeypatch.setattr(manager._condition, "notify", original)
        job = manager.confirm(
            proposal["proposal_id"], proposal["fingerprint"], "retry-key", context("POST")
        ).to_dict()
        wait_state(manager, job["job_id"], context(), {"completed", "failed", "uncertain"})
    finally:
        manager.close()


def test_revocation_denies_context_and_discards_review(launched):
    registry, context, preparer, issued, _ = launched
    manager = JobManager(registry, preparer)
    try:
        proposal = review(manager, context)
        current = context("POST")
        registry.revoke(current)
        assert issued[0]._consumed and manager._buffers == 0
        with pytest.raises(JobError, match="run_unavailable"):
            manager.confirm(proposal["proposal_id"], proposal["fingerprint"], "old", current)
    finally:
        manager.close()


def test_real_barrier_prevents_cancel_and_keeps_worker_handle(launched, monkeypatch):
    registry, context, preparer, issued, _ = launched
    manager = JobManager(registry, preparer)
    entered, release = threading.Event(), threading.Event()
    original = jobs.commit_operation

    def paused(handle, *, cancellation, progress):
        def at_progress(event):
            progress(event)
            if cancellation.publishing and not entered.is_set():
                entered.set()
                assert release.wait(timeout=10)

        return original(handle, cancellation=cancellation, progress=at_progress)

    monkeypatch.setattr(jobs, "commit_operation", paused)
    try:
        proposal = review(manager, context)
        job = manager.confirm(
            proposal["proposal_id"], proposal["fingerprint"], "barrier", context("POST")
        ).to_dict()
        assert entered.wait(timeout=10)
        cancel = manager.cancel(job["job_id"], context("POST")).to_dict()
        assert cancel["state"] == "barrier_pubclosed"
        assert manager._jobs[job["job_id"]].handle is issued[0]
        release.set()
        assert (
            wait_state(manager, job["job_id"], context(), {"completed", "failed", "uncertain"})[
                "state"
            ]
            == "completed"
        )
    finally:
        release.set()
        manager.close()


def test_queued_cancel_and_queue_capacity_keep_private_ownership(launched):
    registry, context, real_preparer, _, request = launched
    entered, release = threading.Event(), threading.Event()

    def held(selected, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return real_preparer(selected, **kwargs)

    manager = JobManager(registry, held)
    try:
        first = manager.prepare({"index": 0}, context("POST")).to_dict()
        assert entered.wait(timeout=10)
        waiting = [manager.prepare({"index": i}, context("POST")).to_dict() for i in range(1, 9)]
        with pytest.raises(JobError, match="queue_capacity"):
            manager.prepare({"index": 9}, context("POST"))
        assert (
            manager.cancel(waiting[0]["job_id"], context("POST")).to_dict()["state"] == "cancelled"
        )
        for item in waiting[1:]:
            manager.cancel(item["job_id"], context("POST"))
        manager.cancel(first["job_id"], context("POST"))
        release.set()
        assert (
            wait_state(manager, first["job_id"], context(), {"cancelled", "failed"})["state"]
            == "cancelled"
        )
        assert manager._buffers == 0 and not request.home.exists()
    finally:
        release.set()
        manager.close()


def test_expired_review_drops_handle_without_revival(launched):
    registry, context, preparer, issued, _ = launched
    ticks = [0.0]
    manager = JobManager(registry, preparer, clock=lambda: ticks[0])
    try:
        proposal = review(manager, context)
        ticks[0] = 300.0
        with pytest.raises(JobError, match="proposal_unavailable"):
            manager.confirm(
                proposal["proposal_id"], proposal["fingerprint"], "expired", context("POST")
            )
        assert issued[0]._consumed and manager._buffers == 0
    finally:
        manager.close()


def test_close_waits_for_publication_without_discarding_active_handle(launched, monkeypatch):
    registry, context, preparer, issued, _ = launched
    manager = JobManager(registry, preparer)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = jobs.commit_operation

    def paused(handle, *, cancellation, progress):
        def observed(event):
            progress(event)
            if cancellation.publishing and not entered.is_set():
                entered.set()
                assert release.wait(timeout=10)

        return original(handle, cancellation=cancellation, progress=observed)

    monkeypatch.setattr(jobs, "commit_operation", paused)
    closer = None
    try:
        proposal = review(manager, context)
        item = manager.confirm(
            proposal["proposal_id"], proposal["fingerprint"], "close-barrier", context("POST")
        ).to_dict()
        assert entered.wait(timeout=10)

        def close():
            manager.close()
            closed.set()

        closer = threading.Thread(target=close)
        closer.start()
        assert not closed.wait(timeout=0.05)
        assert manager._jobs[item["job_id"]].handle is issued[0]
        release.set()
        assert closed.wait(timeout=10)
        assert manager._jobs[item["job_id"]].state == "completed" and manager._buffers == 0
    finally:
        release.set()
        if closer is not None:
            closer.join(timeout=10)
        manager.close()


def test_dto_preparer_cannot_issue_a_private_proposal(launched):
    registry, context, _, _, _ = launched

    def fake(*_, **__):
        return {"proposal_id": "fake-dto"}

    manager = JobManager(registry, fake)
    try:
        item = manager.prepare({"source_session_id": "fixture"}, context("POST")).to_dict()
        result = wait_state(manager, item["job_id"], context(), {"failed"})
        assert result["result"] == {"reason": "preparation_failed"} and manager._buffers == 0
    finally:
        manager.close()
