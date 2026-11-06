"""HTTP tree requests resolve only launch-selected profiles and one-shot runtime handles."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import pytest
import test_tree_cli as cli_fixtures
import test_tree_operations as publisher_fixtures

from session_migrate import conversion, operations
from session_migrate.catalog import Catalog
from session_migrate.tree_cli import TreeWorkflow
from session_migrate.web.app import create_app
from session_migrate.web.auth import RunRegistry
from session_migrate.web.backend import WorkspaceBackend

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")
ORIGIN = "http://127.0.0.1:8021"
workspace = cli_fixtures.workspace
tree_setup = publisher_fixtures.setup


@pytest.fixture
def web(workspace, tmp_path, request):
    settings = getattr(request, "param", {})
    _, options, _, _, profiles, _, _, _ = workspace
    catalog = tmp_path / "catalog.sqlite3"
    with Catalog(catalog):
        pass
    registry = RunRegistry()
    scopes = frozenset({"readonly", "publish"} if settings.get("publish", True) else {"readonly"})
    credential = registry.create(ORIGIN, scopes)
    payload = json.loads(registry._bootstrap_payload(credential))
    backend = WorkspaceBackend(
        registry,
        catalog,
        profiles,
        projects={"Synthetic": options["cwd"]},
        state_home=options["state_home"],
        base_environ=options["base_environ"],
        runtime_actions=frozenset({"tree"}) if settings.get("tree", True) else frozenset(),
    )
    app = create_app(registry, ORIGIN, backend)

    def call(method, path, *, remove=None, **kwargs):
        headers = {
            "Authorization": "Bearer " + payload["access_token"],
            "Origin": ORIGIN,
            "X-CSRF-Token": payload["csrf_nonce"],
        }
        if remove:
            headers.pop(remove)

        async def perform():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN
            ) as client:
                return await client.request(method, path, headers=headers, **kwargs)

        return asyncio.run(perform())

    values = call("GET", "/api/profiles").json()
    parameters = {
        "source_profile_id": next(p["id"] for p in values["profiles"] if p["format"] == "claude"),
        "target_profile_id": next(p["id"] for p in values["profiles"] if p["format"] == "opencode"),
        "project_id": values["projects"][0]["id"],
        "source_client_version": "2.1.318",
        "manifest": cli_fixtures.declaration(workspace[0]),
        "mode": "full_tree",
        "detach": False,
        "acknowledge_partial": True,
        "model_provider": "fixture",
        "model": "test",
    }
    try:
        yield call, parameters, workspace, backend, registry, payload
    finally:
        registry.close()
        backend.close()


def wait(call, job, states):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = call("GET", "/api/jobs/" + job["job_id"])
        assert result.status_code == 200, result.text
        value = result.json()
        if value["state"] in states:
            return value
        time.sleep(0.01)
    raise AssertionError("Tree HTTP job did not finish")


def review(web):
    call, parameters, *_ = web
    response = call("POST", "/api/trees/prepare", json=parameters)
    assert response.status_code == 200, response.text
    prepared = wait(call, response.json(), {"reviewable", "failed", "cancelled"})
    assert prepared["state"] == "reviewable", prepared
    response = call("GET", "/api/proposals/" + prepared["proposal_id"])
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("web", [{"publish": False}, {"tree": False}], indirect=True)
def test_http_tree_defaults_require_publish_and_explicit_tree_launch(web):
    call, parameters, workspace, *_ = web
    response = call("POST", "/api/trees/prepare", json=parameters)
    assert response.status_code in {403, 500}
    assert workspace[3] == []


@pytest.mark.parametrize("remove", ["Authorization", "Origin", "X-CSRF-Token"])
def test_http_tree_requires_current_auth_and_nonce(web, remove):
    call, parameters, workspace, *_ = web
    assert call("POST", "/api/trees/prepare", json=parameters, remove=remove).status_code == 403
    assert workspace[3] == []


def test_http_tree_review_metadata_then_exact_replay_is_same_publication(web):
    call, parameters, workspace, *_ = web
    parameters["manifest"]["nodes"][0]["title"] = "<img src=x onerror=alert(1)>"
    proposal = review(web)
    encoded = json.dumps(proposal)
    assert workspace[3] == [] and proposal["native_action"] == "tree"
    assert "PRIVATE_BODY_SENTINEL" not in encoded and workspace[0].canonical_root not in encoded
    assert str(workspace[1]["target"].root) not in encoded
    assert (
        proposal["metadata"]["structure"]["nodes"][0]["title"]
        == parameters["manifest"]["nodes"][0]["title"]
    )
    body = {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "tree-once"}
    path = "/api/trees/" + proposal["proposal_id"] + "/confirm"
    first = call("POST", path, json=body)
    assert first.status_code == 200, first.text
    result = wait(call, first.json(), {"completed", "failed", "uncertain"})
    assert result["state"] == "completed", result
    assert len(workspace[3]) == 3
    assert call("POST", path, json=body).json()["job_id"] == first.json()["job_id"]
    assert call("POST", path, json={**body, "idempotency_key": "second"}).status_code == 500
    assert len(workspace[3]) == 3 and "journal_path" not in result["result"]


@pytest.mark.parametrize("change", ["root", "absolute", "traversal", "claimed_native"])
def test_http_tree_never_accepts_outside_paths_or_serialized_native_authority(web, change):
    call, parameters, workspace, *_ = web
    if change == "root":
        parameters["manifest"]["canonical_root"] = "/outside"
    elif change in {"absolute", "traversal"}:
        parameters["manifest"]["nodes"][0]["relative_path"] = (
            "/outside" if change == "absolute" else "../outside"
        )
    else:
        parameters["manifest"]["kind"] = "session-tree-proposal"
        parameters["manifest"]["verified"] = True
    result = call("POST", "/api/trees/prepare", json=parameters)
    assert result.status_code == 200, result.text
    assert wait(call, result.json(), {"failed"})["state"] == "failed"
    assert workspace[3] == []


def test_http_tree_no_arbitrary_env_or_cli_options_and_action_bound_confirmation(web):
    call, parameters, workspace, *_ = web
    assert (
        call(
            "POST", "/api/trees/prepare", json={**parameters, "target_cli": "/outside"}
        ).status_code
        == 400
    )
    proposal = review(web)
    body = {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "bound-action"}
    assert (
        call(
            "POST", "/api/proposals/" + proposal["proposal_id"] + "/confirm", json=body
        ).status_code
        == 500
    )
    assert workspace[3] == []


def test_http_tree_cancellation_keeps_receipt_and_publishes_nothing_before_barrier(
    web, monkeypatch
):
    call, _, workspace, *_ = web
    proposal = review(web)
    entered, release = threading.Event(), threading.Event()
    original = TreeWorkflow.commit

    def delayed(workflow, **kwargs):
        entered.set()
        release.wait(10)
        return original(workflow, **kwargs)

    monkeypatch.setattr(TreeWorkflow, "commit", delayed)
    body = {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "cancel-tree"}
    response = call("POST", "/api/trees/" + proposal["proposal_id"] + "/confirm", json=body)
    assert response.status_code == 200, response.text
    try:
        assert entered.wait(5)
        cancel = call("POST", "/api/jobs/" + response.json()["job_id"] + "/cancel", json={})
        assert cancel.status_code == 200
    finally:
        release.set()
    result = wait(call, response.json(), {"cancelled", "failed", "uncertain"})
    assert result["state"] == "cancelled", result
    assert workspace[3] == []
    assert result["operation_id"] == proposal["metadata"]["operation_id"]


def test_http_tree_manifest_is_not_mutable_publication_authority(web):
    call, _, workspace, backend, *_ = web
    proposal = review(web)
    edited = copy.deepcopy(proposal)
    edited["metadata"]["selection"]["mode"] = "single"
    response = call("POST", "/api/trees/" + proposal["proposal_id"] + "/confirm", json=edited)
    assert response.status_code == 400 and workspace[3] == []
    assert backend.jobs._buffers > 0


def test_http_cancel_between_nodes_preserves_partial_receipts_without_automatic_retry(
    web, monkeypatch
):
    call, _, workspace, *_ = web
    proposal = review(web)
    original = operations.commit_operation

    def cancel_after_first(*args, **kwargs):
        receipt = original(*args, **kwargs)
        kwargs["cancellation"].parent.request_cancel()
        return receipt

    monkeypatch.setattr(operations, "commit_operation", cancel_after_first)
    response = call(
        "POST",
        "/api/trees/" + proposal["proposal_id"] + "/confirm",
        json={"body_fingerprint": proposal["fingerprint"], "idempotency_key": "partial-tree"},
    )
    assert response.status_code == 200, response.text
    result = wait(call, response.json(), {"uncertain", "completed", "failed"})
    assert result["state"] == "uncertain", result
    assert result["result"]["status"] == "partial"
    assert result["result"]["nodes"][0]["status"] == "completed"
    assert result["result"]["nodes"][0]["operation_id"]
    assert len(workspace[3]) == 1


def test_http_tree_capture_budget_blocks_large_source_before_target_probe(web, monkeypatch):
    call, parameters, workspace, *_ = web
    root = workspace[4][0].root
    path = root / "parent.jsonl"
    path.write_bytes(path.read_bytes() + b" " * (4 * 1024 * 1024))
    probes = []
    monkeypatch.setattr(
        conversion, "_opencode_version", lambda *args: probes.append(True) or "2.2.5"
    )
    response = call("POST", "/api/trees/prepare", json=parameters)
    assert response.status_code == 200, response.text
    assert wait(call, response.json(), {"failed"})["state"] == "failed"
    assert workspace[3] == [] and probes == []


def test_web_review_exposes_unknown_coverage_without_fork_proof(web):
    _, _, workspace, *_ = web
    for path in Path(workspace[0].canonical_root).glob("*.jsonl"):
        value = json.loads(path.read_text())
        value["future_field"] = {"unmapped": True}
        path.write_text(json.dumps(value) + "\n")
    value = review(web)["metadata"]
    assert value["coverage"] == "unknown"
    assert value["projection_scope"] == "portable_conversation"
    assert value["publication_mode"] == "independent"
    assert value["hierarchy_semantics"] == "external_organizational_map"
    assert value["fork_boundary_proven"] is False
    assert all(node["coverage"] == "unknown" and node["title"] for node in value["nodes"])
    assert workspace[3] == []


@pytest.mark.parametrize("mode", [None, "independent", "native-subagent"])
def test_http_publication_mode_default_and_native_opt_in(web, mode):
    call, parameters, workspace, *_ = web
    if mode is not None:
        parameters["publication_mode"] = mode
    proposal = review(web)
    value = proposal["metadata"]
    expected = mode or "independent"
    assert value["publication_mode"] == expected
    assert value["hierarchy_semantics"] == (
        "external_organizational_map" if expected == "independent" else "native_subagent_parent"
    )
    assert bool(value["warnings"]) == (expected == "native-subagent")
    assert all(node["parent_target_id"] is None for node in value["nodes"]) == (
        expected == "independent"
    )
    assert value["nodes"][1]["organizational_parent_target_id"] == value["nodes"][0]["target_id"]
    assert workspace[3] == []
    response = call(
        "POST",
        "/api/trees/" + proposal["proposal_id"] + "/confirm",
        json={"body_fingerprint": proposal["fingerprint"], "idempotency_key": "mode-bound"},
    )
    assert response.status_code == 200, response.text
    result = wait(call, response.json(), {"completed", "failed", "uncertain"})
    assert result["state"] == "completed", result
    assert result["result"]["publication_mode"] == expected
    assert len(workspace[3]) == 3
    for index, native_id in enumerate(workspace[3]):
        native_parent = workspace[3][index - 1] if index and expected == "native-subagent" else None
        assert workspace[2][native_id]["info"].get("parentID") == native_parent


@pytest.mark.parametrize("mode", ["", "native", "INDEPENDENT", None, True, [], {}])
def test_http_invalid_publication_mode_rejected_before_jobs_and_native_probes(
    web, monkeypatch, mode
):
    call, parameters, workspace, backend, *_ = web

    def forbidden(*args, **kwargs):
        pytest.fail("malformed mode must not enqueue a job or probe a native target")

    monkeypatch.setattr(backend.jobs, "prepare_runtime", forbidden)
    monkeypatch.setattr(conversion, "_opencode_version", forbidden)
    result = call("POST", "/api/trees/prepare", json={**parameters, "publication_mode": mode})
    assert result.status_code == 400
    assert result.json()["reason"] == "publication_mode_invalid"
    assert workspace[3] == []


def test_runtime_tree_mode_substitution_invalidates_issued_capability(workspace):
    from dataclasses import replace

    from session_migrate.operations import CancellationToken
    from session_migrate.web import runtime_jobs

    inventory, options, _, imports, profiles, *_ = workspace
    request = runtime_jobs.TreeRuntimeRequest(
        profiles[0],
        profiles[1],
        options["cwd"],
        options["state_home"],
        options["base_environ"],
        "2.1.318",
        cli_fixtures.declaration(inventory),
        "full_tree",
        None,
        False,
        True,
        "fixture",
        "test",
    )
    changed = replace(request, publication_mode="native-subagent")
    assert request.publication_mode == "independent"
    assert runtime_jobs._tree_binding(request) != runtime_jobs._tree_binding(changed)
    handle = runtime_jobs.new_runtime(request, "tree", "synthetic-run")
    try:
        runtime_jobs.capture_runtime(
            handle,
            cancellation=CancellationToken(),
            prepublication_check=lambda: None,
        )
        runtime_jobs.prepare_runtime(
            handle,
            cancellation=CancellationToken(),
            prepublication_check=lambda: None,
        )
        runtime_jobs._ISSUED[handle].request = changed
        with pytest.raises(runtime_jobs.RuntimeJobError, match="source_changed"):
            runtime_jobs.commit_runtime(handle, "synthetic-run", cancellation=CancellationToken())
        assert imports == []
    finally:
        handle.discard()
