"""Actual selected-profile backend with a synthetic B3 catalog and file targets."""

import asyncio
import json
import time

import pytest
import test_continuity as fixtures

from session_migrate.catalog import Catalog
from session_migrate.lineage import LineageStore
from session_migrate.web.auth import RunRegistry
from session_migrate.web.backend import BackendError, WorkspaceBackend


@pytest.fixture
def launched(tmp_path, request):
    catalog, _, ledger, operation, env, profiles, source = fixtures.actual_b3_fixture(
        tmp_path, "profiles"
    )
    path = catalog.path
    catalog.close()
    registry = RunRegistry()
    credential = registry.create("http://127.0.0.1:8021", frozenset({"readonly", "publish"}))
    payload = json.loads(registry._bootstrap_payload(credential))
    backend = WorkspaceBackend(
        registry,
        path,
        profiles,
        projects={"Private fixture": tmp_path / "work"},
        base_environ=env,
        service_actions=getattr(request, "param", frozenset()),
    )

    def context(method="GET"):
        return registry.authenticate(
            "Bearer " + payload["access_token"],
            host="127.0.0.1:8021",
            origin="http://127.0.0.1:8021" if method == "POST" else None,
            method=method,
            csrf_nonce=payload["csrf_nonce"] if method == "POST" else None,
        )

    try:
        yield backend, context, path, profiles, source, ledger, operation, env
    finally:
        backend.close()
        registry.close()


def dispatch(backend, context, action, parameters=None, identifiers=None, method="GET"):
    return asyncio.run(
        backend.dispatch(action, context(method), parameters or {}, identifiers or {})
    )


def selection(backend, context):
    values = dispatch(backend, context, "profiles")
    source_id = next(
        profile["id"] for profile in values["profiles"] if profile["format"] == "claude"
    )
    target_id = next(
        profile["id"] for profile in values["profiles"] if profile["format"] == "codex"
    )
    sessions = dispatch(backend, context, "sessions", {"profile_id": source_id})
    return values, source_id, target_id, sessions["sessions"][0]["id"]


def test_selected_profiles_sessions_preview_are_readonly_and_literal(launched):
    backend, context, catalog, profiles, source, _, _, _ = launched
    before_catalog, before_source = catalog.read_bytes(), source.read_bytes()
    values, _, _, source_id = selection(backend, context)
    assert all(
        profile["id"] not in {item.id for item in profiles} for profile in values["profiles"]
    )
    assert all(set(profile) == {"id", "name", "format"} for profile in values["profiles"])
    preview = dispatch(backend, context, "preview", identifiers={"session_id": source_id})
    assert type(preview["text"]) is str and len(preview["text"].encode()) < 65536
    assert catalog.read_bytes() == before_catalog and source.read_bytes() == before_source
    with pytest.raises(BackendError):
        dispatch(backend, context, "preview", identifiers={"session_id": str(source)})


@pytest.mark.parametrize("action", ["prepare_adopt", "prepare_reconcile", "prepare_cancel_ready"])
def test_publish_scope_does_not_enable_unselected_service_actions(launched, action):
    backend, context, _, _, _, ledger, _, _ = launched
    before = (ledger / "ledger.sqlite3").read_bytes()
    capabilities = dispatch(backend, context, "capabilities")
    assert capabilities["publish_enabled"] is True
    assert not any(
        capabilities[name]
        for name in ("adoption_available", "reconcile_available", "cancel_ready_available")
    )
    with pytest.raises(BackendError, match="capability_unavailable"):
        dispatch(backend, context, action, {}, method="POST")
    assert not backend.jobs._jobs and (ledger / "ledger.sqlite3").read_bytes() == before


def test_real_profile_transfer_private_job_and_receipt(launched):
    backend, context, _, _, _, ledger, _, _ = launched
    values, _, target_id, source_id = selection(backend, context)
    request = {
        "source_session_id": source_id,
        "target_profile_id": target_id,
        "project_id": values["projects"][0]["id"],
    }
    item = dispatch(backend, context, "prepare", request, method="POST")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        item = dispatch(backend, context, "job", identifiers={"job_id": item["job_id"]})
        if item["state"] in ("reviewable", "failed"):
            break
        time.sleep(0.01)
    assert item["state"] == "reviewable"
    proposal = dispatch(
        backend, context, "review", identifiers={"proposal_id": item["proposal_id"]}
    )
    confirmed = dispatch(
        backend,
        context,
        "confirm",
        {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "backend-once"},
        {"proposal_id": proposal["proposal_id"]},
        method="POST",
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = dispatch(backend, context, "job", identifiers={"job_id": confirmed["job_id"]})
        if result["state"] in ("completed", "failed", "uncertain"):
            break
        time.sleep(0.01)
    assert result["state"] == "completed"
    records = LineageStore.inspect_existing(ledger)
    assert len(records.operations) == 2
    replay = dispatch(
        backend,
        context,
        "confirm",
        {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "backend-once"},
        {"proposal_id": proposal["proposal_id"]},
        method="POST",
    )
    assert replay["job_id"] == confirmed["job_id"]
    assert len(LineageStore.inspect_existing(ledger).operations) == 2


def test_usage_uses_actual_issued_capture_and_same_reader_thread(launched):
    backend, context, _, _, _, _, _, _ = launched
    _, _, _, source_id = selection(backend, context)
    first = dispatch(backend, context, "usage", {"limit": "1"}, {"session_id": source_id})
    assert first["schema_version"] == 1 and first["capture_revision"]["source_identity"]
    assert all(
        value is None
        for group in first["aggregate"]["groups"]
        for _, value in group["dimension_totals"]
    )
    if first["next_cursor"]:
        second = dispatch(
            backend,
            context,
            "usage",
            {"limit": "1", "cursor": first["next_cursor"]},
            {"session_id": source_id},
        )
        assert second["capture_revision"] == first["capture_revision"]
        assert second["aggregate"] == first["aggregate"]


def test_catalog_revision_invalidates_session_cursor(launched):
    backend, context, catalog_path, profiles, source, _, _, _ = launched
    _, source_profile_id, _, _ = selection(backend, context)
    another = source.with_name("55555555-5555-4555-8555-555555555555.jsonl")
    rows = [json.loads(line) for line in source.read_bytes().splitlines()]
    for row in rows:
        row["sessionId"] = another.stem
    fixtures.write_records(another, rows)
    with Catalog(catalog_path) as catalog:
        catalog.refresh(claude_roots=(profiles[0].root,), include_auto=False)
    page = dispatch(backend, context, "sessions", {"profile_id": source_profile_id, "limit": "1"})
    assert page["next_cursor"]
    with Catalog(catalog_path) as catalog:
        catalog.refresh(claude_roots=(profiles[0].root,), include_auto=False)
    with pytest.raises(BackendError, match="refresh_required"):
        dispatch(
            backend,
            context,
            "sessions",
            {"profile_id": source_profile_id, "limit": "1", "cursor": page["next_cursor"]},
        )


def test_lineage_is_scoped_and_inspection_does_not_create_state(launched):
    backend, context, _, _, _, _, operation, _ = launched
    page = dispatch(backend, context, "lineage")
    assert len(page["operations"]) == 1
    assert page["operations"][0]["operation_id"] == operation.record.operation_id
    token = page["operations"][0]["id"]
    details = dispatch(backend, context, "inspect_lineage", identifiers={"operation_id": token})
    assert details["record"]["operation_id"] == operation.record.operation_id
    with pytest.raises(BackendError, match="operation_unavailable"):
        dispatch(
            backend,
            context,
            "inspect_lineage",
            identifiers={"operation_id": operation.record.operation_id},
        )


def test_http_to_real_backend_transfer_keeps_private_job_scope(launched):
    pytest.importorskip("starlette")
    httpx = pytest.importorskip("httpx")
    from session_migrate.web.app import create_app

    backend, _, _, _, _, ledger, _, _ = launched
    credential = backend.registry.create(
        "http://127.0.0.1:8021", frozenset({"readonly", "publish"})
    )
    payload = json.loads(backend.registry._bootstrap_payload(credential))
    app = create_app(backend.registry, "http://127.0.0.1:8021", backend)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8021"
        ) as client:
            values = {"Authorization": "Bearer " + payload["access_token"]}
            profiles = (await client.get("/api/profiles", headers=values)).json()["profiles"]
            source_profile = next(p["id"] for p in profiles if p["format"] == "claude")
            target_profile = next(p["id"] for p in profiles if p["format"] == "codex")
            page = (
                await client.get(
                    "/api/sessions", params={"profile_id": source_profile}, headers=values
                )
            ).json()
            values.update(
                {"Origin": "http://127.0.0.1:8021", "X-CSRF-Token": payload["csrf_nonce"]}
            )
            response = await client.post(
                "/api/proposals",
                headers=values,
                json={
                    "source_session_id": page["sessions"][0]["id"],
                    "target_profile_id": target_profile,
                },
            )
            assert response.status_code == 200
            job = response.json()
            for _ in range(300):
                job = (await client.get("/api/jobs/" + job["job_id"], headers=values)).json()
                if job["state"] in ("reviewable", "failed"):
                    break
                await asyncio.sleep(0.01)
            assert job["state"] == "reviewable"
            proposal = (
                await client.get("/api/proposals/" + job["proposal_id"], headers=values)
            ).json()
            response = await client.post(
                "/api/proposals/" + proposal["proposal_id"] + "/confirm",
                headers=values,
                json={
                    "body_fingerprint": proposal["fingerprint"],
                    "idempotency_key": "http-actual-once",
                },
            )
            assert response.status_code == 200
            job = response.json()
            for _ in range(300):
                job = (await client.get("/api/jobs/" + job["job_id"], headers=values)).json()
                if job["state"] in ("completed", "failed", "uncertain"):
                    break
                await asyncio.sleep(0.01)
            assert job["state"] == "completed"
            return job

    result = asyncio.run(exercise())
    assert result["result"]["status"] == "completed"
    assert len(LineageStore.inspect_existing(ledger).operations) == 2


def test_usage_observer_filters_unrelated_runtime_environment(launched):
    backend, _, catalog, profiles, _, _, _, env = launched
    values = {
        **env,
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": "/synthetic/source-code",
        "SYNTHETIC_API_SECRET": "NEVER_SERIALIZE_SYNTHETIC_ENV",
    }
    registry = RunRegistry()
    credential = registry.create("http://127.0.0.1:8021")
    payload = json.loads(registry._bootstrap_payload(credential))

    def context(method="GET"):
        return registry.authenticate(
            "Bearer " + payload["access_token"], host="127.0.0.1:8021", origin=None, method=method
        )

    isolated = WorkspaceBackend(
        registry, catalog, profiles, base_environ=values, state_home=backend.state_home
    )
    try:
        _, _, _, source_id = selection(isolated, context)
        report = dispatch(isolated, context, "usage", {"limit": "100"}, {"session_id": source_id})
        assert len(report["samples_page"]) > 0
        assert "SYNTHETIC_API_SECRET" not in isolated.observer_environ
        assert "PYTHONPATH" not in isolated.observer_environ
        assert "NEVER_SERIALIZE_SYNTHETIC_ENV" not in json.dumps(report)
    finally:
        isolated.close()
        registry.close()
