"""Synthetic HTTP review and acknowledgement never imply runtime equivalence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import test_tree_web as tree_fixtures
from test_claude_reviewed_reader import attachment
from test_reviewed_portability_core import source_rows
from test_web_app import headers, request, setup_app

from session_migrate.catalog import Catalog
from session_migrate.web.jobs import JobError
from session_migrate.web.runtime_jobs import _tree_binding

workspace = tree_fixtures.workspace
tree_setup = tree_fixtures.tree_setup
web = tree_fixtures.web


def reviewed_sources(web, *, padding_bytes=0):
    _, parameters, workspace, *_ = web
    inventory = workspace[0]
    for node in inventory.nodes:
        path = Path(inventory.canonical_root) / node.relative_path
        rows = source_rows()
        rows.append(
            attachment("runtime-history", "after", "prompt_snapshot", baseline=["DATA_ONLY"])
        )
        parent = "runtime-history"
        while (
            padding_bytes
            and sum(len(row.get("attachment", {}).get("padding", "")) for row in rows)
            < padding_bytes
        ):
            uid = "padding-" + str(len(rows))
            rows.append(attachment(uid, parent, "prompt_snapshot", padding="X" * (512 * 1024)))
            parent = uid
        for row in rows:
            row["sessionId"] = node.source_native_id
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    parameters.update(source_client_version="2.1.309", reviewed_portability=True)
    return parameters


def normal_selection(web, *, padding_bytes=0):
    call, parameters, workspace, backend, *_ = web
    reviewed_sources(web, padding_bytes=padding_bytes)
    root = Path(workspace[0].canonical_root)
    project = root / "projects" / "synthetic-project"
    project.mkdir(parents=True, mode=0o700)
    for node in workspace[0].nodes:
        (project / (node.source_native_id + ".jsonl")).write_bytes(
            (root / node.relative_path).read_bytes()
        )
    with Catalog(backend.catalog_path) as catalog:
        catalog.refresh(claude_roots=(Path(workspace[0].canonical_root),), include_auto=False)
    sessions = call("GET", "/api/sessions", params={"profile_id": parameters["source_profile_id"]})
    assert sessions.status_code == 200
    selected = sessions.json()["sessions"][0]["id"]
    return {
        "source_session_id": selected,
        "target_profile_id": parameters["target_profile_id"],
        "project_id": parameters["project_id"],
        "reviewed_portability": True,
    }


def normal_review(web):
    call, *_ = web
    selected = normal_selection(web)
    created = call("POST", "/api/proposals", json=selected)
    assert created.status_code == 200
    job = tree_fixtures.wait(call, created.json(), {"reviewable", "failed"})
    assert job["state"] == "reviewable", job
    proposal = call("GET", "/api/proposals/" + job["proposal_id"]).json()
    assert proposal["metadata"]["mode"] == "reviewed_portability"
    return selected, proposal


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
@pytest.mark.parametrize("confirmation", [False, True])
def test_http_reviewed_fields_require_real_booleans(value, confirmation):
    _, payload, backend, app = setup_app(publish=True)
    body = (
        {
            "body_fingerprint": "a" * 64,
            "idempotency_key": "once",
            "acknowledge_reviewed_portability": value,
        }
        if confirmation
        else {
            "source_session_id": "source",
            "target_profile_id": "target",
            "reviewed_portability": value,
        }
    )
    path = "/api/proposals/proposal/confirm" if confirmation else "/api/proposals"
    result = request(app, "POST", path, headers=headers(payload, mutation=True), json=body)
    assert result.status_code == 400 and backend.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/adopt/prepare",
        "/api/reconcile/prepare",
        "/api/cancel-ready/prepare",
        "/api/handoffs/capture",
        "/api/return-new/capture",
        "/api/return-new/prepare",
    ],
)
def test_reviewed_option_is_not_accepted_for_other_actions(path):
    _, payload, backend, app = setup_app(publish=True)
    result = request(
        app,
        "POST",
        path,
        headers=headers(payload, mutation=True),
        json={"reviewed_portability": True},
    )
    assert result.status_code == 400 and backend.calls == []


def test_http_preview_explicit_reviewed_is_readonly_and_strict_stays_rejected(web):
    call, _, workspace, backend, *_ = web
    selected = normal_selection(web)
    paths = [Path(workspace[0].canonical_root) / node.relative_path for node in workspace[0].nodes]
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    catalog_before = backend.catalog_path.read_bytes()
    route = "/api/sessions/" + selected["source_session_id"] + "/preview"
    assert call("GET", route).status_code == 500
    result = call("GET", route, params={"reviewed_portability": "true"})
    assert result.status_code == 200, result.text
    document = result.json()
    assert document["mode"] == "reviewed_portability" and document["reviewed_report"]
    assert "SYNTHETIC_PRIVATE_BODY" in document["text"]
    assert "DATA_ONLY" not in document["text"]
    assert call("GET", route, params={"reviewed_portability": "1"}).status_code == 400
    assert call("GET", route).status_code == 500
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    assert backend.catalog_path.read_bytes() == catalog_before
    assert workspace[3] == []


def test_http_ack_is_separate_fresh_and_bound_into_idempotency(web):
    call, _, workspace, backend, *_ = web
    _, proposal = normal_review(web)
    assert backend.jobs._jobs[proposal["preparation_job_id"]].deadline is None
    archive = Path(proposal["metadata"]["reviewed_archive"]["path"])
    assert not archive.exists() and not workspace[3]
    body = {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "reviewed-once"}
    route = "/api/proposals/" + proposal["proposal_id"] + "/confirm"
    for extra in ({}, {"acknowledge_reviewed_portability": False}):
        assert call("POST", route, json={**body, **extra}).status_code == 500
        private = backend.jobs._proposals[proposal["proposal_id"]]
        assert not private.consumed and private.handle is not None
        assert not workspace[3] and not archive.exists()
    approved = {**body, "acknowledge_reviewed_portability": True}
    first = call("POST", route, json=approved)
    assert first.status_code == 200
    result = tree_fixtures.wait(call, first.json(), {"completed", "failed", "uncertain"})
    assert result["state"] == "completed", result
    assert result["result"]["legacy_result"]["reviewed_archive"]["status"] == "retained"
    assert archive.is_file() and len(workspace[3]) == 1
    assert call("POST", route, json=approved).json()["job_id"] == first.json()["job_id"]
    assert call("POST", route, json=body).status_code == 500
    assert len(workspace[3]) == 1


@pytest.mark.parametrize("mutation", ["mode", "metadata", "fingerprint", "unknown"])
def test_injected_review_cannot_enqueue_or_consume_proposal(web, mutation):
    call, _, workspace, backend, *_ = web
    _, proposal = normal_review(web)
    private = backend.jobs._proposals[proposal["proposal_id"]]
    body = {
        "body_fingerprint": proposal["fingerprint"],
        "idempotency_key": "injected",
        "acknowledge_reviewed_portability": True,
    }
    if mutation == "mode":
        object.__setattr__(private.handle._state.built.request, "reviewed_portability", False)
    elif mutation == "metadata":
        metadata = json.loads(private.metadata_json)
        metadata["reviewed_portability"]["source_record_count"] = 1
        private.metadata_json = json.dumps(metadata)
    elif mutation == "fingerprint":
        body["body_fingerprint"] = "0" * 64
    else:
        body["source"] = {"reviewed": True}
    route = "/api/proposals/" + proposal["proposal_id"] + "/confirm"
    assert call("POST", route, json=body).status_code in {400, 500}
    assert not private.consumed and private.handle is not None
    assert workspace[3] == []


@pytest.mark.parametrize("mutation", ["source", "report"])
def test_confirmed_source_or_report_mutation_fails_before_import(web, mutation):
    call, _, workspace, backend, *_ = web
    _, proposal = normal_review(web)
    private = backend.jobs._proposals[proposal["proposal_id"]]
    state = private.handle._state
    if mutation == "source":
        path = state.built.artifact.source.source_path
        path.write_bytes(path.read_bytes().replace(b"synthetic answer", b"mutated answer"))
    else:
        state.built.artifact.source.reviewed_report["selected_count"] += 1
    response = call(
        "POST",
        "/api/proposals/" + proposal["proposal_id"] + "/confirm",
        json={
            "body_fingerprint": proposal["fingerprint"],
            "idempotency_key": "changed-source",
            "acknowledge_reviewed_portability": True,
        },
    )
    assert response.status_code == 200
    result = tree_fixtures.wait(call, response.json(), {"failed", "uncertain", "completed"})
    assert result["state"] == "failed" and workspace[3] == []
    assert not Path(proposal["metadata"]["reviewed_archive"]["path"]).exists()


def test_reviewed_tree_needs_partial_ack_and_separate_fresh_confirmation(web):
    call, parameters, workspace, *_ = web
    reviewed_sources(web)
    parameters["acknowledge_partial"] = False
    denied = call("POST", "/api/trees/prepare", json=parameters)
    job = tree_fixtures.wait(call, denied.json(), {"reviewable", "failed"})
    assert job["state"] == "reviewable" and workspace[3] == []
    denied_proposal = call("GET", "/api/proposals/" + job["proposal_id"]).json()
    assert denied_proposal["metadata"]["requires_partial_acknowledgement"] is True
    assert denied_proposal["metadata"]["partial_acknowledged"] is False
    denied_confirm = call(
        "POST",
        "/api/trees/" + job["proposal_id"] + "/confirm",
        json={"body_fingerprint": denied_proposal["fingerprint"], "idempotency_key": "no-partial"},
    )
    failed = tree_fixtures.wait(call, denied_confirm.json(), {"failed", "uncertain", "completed"})
    assert failed["state"] == "failed" and workspace[3] == []
    parameters["acknowledge_partial"] = True
    proposal = tree_fixtures.review(web)
    assert proposal["metadata"]["reviewed_portability"] is True
    assert proposal["metadata"]["mode"] == "reviewed_portability"
    assert all(
        n["reviewed_report"] and n["reviewed_archive"] for n in proposal["metadata"]["nodes"]
    )
    assert workspace[3] == []
    approved = call(
        "POST",
        "/api/trees/" + proposal["proposal_id"] + "/confirm",
        json={"body_fingerprint": proposal["fingerprint"], "idempotency_key": "tree-reviewed-once"},
    )
    result = tree_fixtures.wait(call, approved.json(), {"completed", "failed", "uncertain"})
    assert result["state"] == "completed" and len(workspace[3]) == 3


def test_direct_manager_does_not_extend_ack_to_services_or_trees(web):
    _, _, _, backend, registry, payload = web
    context = registry.authenticate(
        "Bearer " + payload["access_token"],
        host="127.0.0.1:8021",
        origin="http://127.0.0.1:8021",
        method="POST",
        csrf_nonce=payload["csrf_nonce"],
    )
    for kwargs in ({"native_action": "tree"}, {"service_action": "adopt"}):
        with pytest.raises(JobError, match="action_invalid"):
            backend.jobs.confirm(
                "proposal",
                "a" * 64,
                "once",
                context,
                acknowledge_reviewed_portability=True,
                **kwargs,
            )


def test_tree_mode_is_strict_boolean_and_only_true_changes_runtime_binding(web):
    from session_migrate.web.runtime_jobs import RuntimeJobError

    _, parameters, _, backend, _, payload = web
    tree = backend._resolve_tree({**parameters, "_run_id": payload["run_id"]})
    assert tree.reviewed_portability is False
    assert _tree_binding(tree) == _tree_binding(replace(tree, reviewed_portability=False))
    assert _tree_binding(tree) != _tree_binding(replace(tree, reviewed_portability=True))
    for value in (None, 0, 1, "true"):
        with pytest.raises(RuntimeJobError):
            _tree_binding(replace(tree, reviewed_portability=value))


def test_explicit_false_keeps_legacy_prepare_fingerprint_and_never_auto_publishes(web):
    call, _, workspace, *_ = web
    selected = normal_selection(web)
    legacy = {key: value for key, value in selected.items() if key != "reviewed_portability"}
    omitted = call("POST", "/api/proposals", json=legacy).json()
    explicit = call("POST", "/api/proposals", json={**legacy, "reviewed_portability": False}).json()
    opted_in = call("POST", "/api/proposals", json=selected).json()
    assert omitted["fingerprint"] == explicit["fingerprint"] != opted_in["fingerprint"]
    for job in (omitted, explicit):
        assert tree_fixtures.wait(call, job, {"failed", "reviewable"})["state"] == "failed"
    assert tree_fixtures.wait(call, opted_in, {"failed", "reviewable"})["state"] == "reviewable"
    assert workspace[3] == []


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_tree_reviewed_option_is_boolean_at_http_boundary(web, value):
    call, parameters, workspace, *_ = web
    result = call("POST", "/api/trees/prepare", json={**parameters, "reviewed_portability": value})
    assert result.status_code == 400 and workspace[3] == []


def test_reviewed_source_over_eight_mib_previews_prepares_and_archives_with_explicit_ack(web):
    call, _, workspace, backend, *_ = web
    selected = normal_selection(web, padding_bytes=9 * 1024 * 1024)
    opaque, profile = backend._selected(selected["source_session_id"])
    with Catalog.open_readonly_existing(backend.catalog_path) as catalog:
        from session_migrate.profiles import resolve_source

        source = resolve_source(catalog, profile, opaque.ref).path
    original = source.read_bytes()
    assert len(original) > 8 * 1024 * 1024
    route = "/api/sessions/" + selected["source_session_id"] + "/preview"
    assert call("GET", route).status_code == 500
    preview = call("GET", route, params={"reviewed_portability": "true"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["source_read_limit_bytes"] == 64 * 1024 * 1024
    prepared = call("POST", "/api/proposals", json=selected)
    job = tree_fixtures.wait(call, prepared.json(), {"reviewable", "failed"})
    assert job["state"] == "reviewable", job
    proposal = call("GET", "/api/proposals/" + job["proposal_id"]).json()
    assert proposal["metadata"]["source"]["read_limit_bytes"] == 64 * 1024 * 1024
    assert backend.jobs._buffers >= 2 * len(original)
    archive = Path(proposal["metadata"]["reviewed_archive"]["path"])
    assert not archive.exists() and workspace[3] == []
    confirmed = call(
        "POST",
        "/api/proposals/" + job["proposal_id"] + "/confirm",
        json={
            "body_fingerprint": proposal["fingerprint"],
            "idempotency_key": "large-source-once",
            "acknowledge_reviewed_portability": True,
        },
    )
    result = tree_fixtures.wait(call, confirmed.json(), {"completed", "failed", "uncertain"})
    assert result["state"] == "completed", result
    assert archive.read_bytes() == original == source.read_bytes()
    assert len(workspace[3]) == 1


def test_explicit_launch_budget_overrides_reviewed_default_without_truncation(web):
    call, _, workspace, backend, *_ = web
    selected = normal_selection(web)
    backend.max_source_bytes = 16
    assert backend._source_budget(True) == backend._source_budget(False) == 16
    response = call(
        "GET",
        "/api/sessions/" + selected["source_session_id"] + "/preview",
        params={"reviewed_portability": "true"},
    )
    assert response.status_code == 413
    assert response.json() == {"reason": "source_read_limit_exceeded", "read_limit_bytes": 16}
    prepared = call("POST", "/api/proposals", json=selected)
    failed = tree_fixtures.wait(call, prepared.json(), {"failed", "reviewable"})
    assert failed["state"] == "failed"
    assert failed["result"] == {"reason": "source_read_limit_exceeded", "read_limit_bytes": 16}
    assert workspace[3] == []


def test_reviewed_tree_byte_capacity_failure_does_not_expose_or_publish_nodes(web, monkeypatch):
    from session_migrate.web import jobs, runtime_jobs

    call, parameters, workspace, backend, *_ = web
    reviewed_sources(web)
    before = {
        node.key: (Path(workspace[0].canonical_root) / node.relative_path).read_bytes()
        for node in workspace[0].nodes
    }
    monkeypatch.setattr(jobs, "MAX_BUFFER_BYTES", 4096)
    monkeypatch.setattr(jobs, "RUNTIME_BUFFER_BYTES", 1)
    monkeypatch.setattr(runtime_jobs, "RUNTIME_BUFFER_BYTES", 1)
    prepared = call("POST", "/api/trees/prepare", json=parameters)
    assert prepared.status_code == 200
    result = tree_fixtures.wait(call, prepared.json(), {"failed", "reviewable"})
    assert result["state"] == "failed" and result["result"] == {"reason": "buffer_capacity"}
    assert workspace[3] == [] and not backend.jobs._proposals
    for node in workspace[0].nodes:
        assert (Path(workspace[0].canonical_root) / node.relative_path).read_bytes() == before[
            node.key
        ]


def test_large_reviewed_report_has_own_bounded_response_envelope_without_truncation(web):
    from session_migrate.web.runtime_jobs import RuntimeJobError, _tree_metadata_json

    _, parameters, _, backend, _, payload = web
    request = backend._resolve_tree({**parameters, "_run_id": payload["run_id"]})
    document = {"reviewed_report": {"provenance": {"retired_indices": list(range(20000))}}}
    with pytest.raises(RuntimeJobError):
        _tree_metadata_json(document, request)
    encoded = _tree_metadata_json(document, replace(request, reviewed_portability=True))
    assert len(encoded) > 64 * 1024 and json.loads(encoded) == document
    with pytest.raises(RuntimeJobError, match="metadata_limit"):
        _tree_metadata_json(
            {"oversized": "X" * (1024 * 1024)}, replace(request, reviewed_portability=True)
        )


@pytest.mark.parametrize(
    "elapsed,cancel,expected",
    [(80, False, "reviewable"), (301, False, "failed"), (80, True, "cancelled")],
)
def test_reviewed_preparation_timeout_uses_fake_clock_and_preserves_cancellation(
    web, monkeypatch, elapsed, cancel, expected
):
    from session_migrate.web import jobs
    from session_migrate.web.runtime_jobs import preparation_timeout

    call, parameters, workspace, backend, *_ = web
    reviewed_sources(web)
    clock = [0.0]
    monkeypatch.setattr(backend.jobs, "_clock", lambda: clock[0])
    original = jobs.prepare_runtime

    def simulate_preparation_time(handle, **kwargs):
        original(handle, **kwargs)
        clock[0] = float(elapsed)
        if cancel:
            assert kwargs["cancellation"].request_cancel()

    monkeypatch.setattr(jobs, "prepare_runtime", simulate_preparation_time)
    prepared = call("POST", "/api/trees/prepare", json=parameters).json()
    job_id = prepared["job_id"]
    with backend.jobs._condition:
        assert backend.jobs._condition.wait_for(
            lambda: backend.jobs._jobs[job_id].state in {"reviewable", "failed", "cancelled"},
            timeout=15,
        )
        assert backend.jobs._jobs[job_id].deadline == 300
    result = call("GET", "/api/jobs/" + job_id).json()
    assert result["state"] == expected and workspace[3] == []
    if expected == "failed":
        assert result["result"] == {
            "reason": "runtime_deadline",
            "preparation_timeout_seconds": 300,
        }
    if expected == "reviewable":
        proposal = call("GET", "/api/proposals/" + result["proposal_id"]).json()
        assert proposal["metadata"]["budget_policy"]["preparation_timeout_seconds"] == 300
        assert proposal["metadata"]["budget_policy"]["policy_id"] == "reviewed-web-budgets-v1"
    assert preparation_timeout("tree") == preparation_timeout("summary") == 30
