"""Selected opaque HTTP resources drive genuine services over synthetic B3 files."""

import asyncio
import json

import pytest

from session_migrate import operations
from session_migrate.catalog import Catalog
from session_migrate.lineage import LineageStore, NativeState, OperationState
from session_migrate.operations import proposal_metadata
from session_migrate.web.app import create_app

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")

pytest_plugins = ["test_web_backend"]
pytestmark = pytest.mark.parametrize(
    "launched", [frozenset({"adopt", "reconcile", "cancel_ready"})], indirect=True
)
ORIGIN = "http://127.0.0.1:8021"


def client_for(backend, *, publish=True):
    scopes = {"readonly", "publish"} if publish else {"readonly"}
    credential = backend.registry.create(ORIGIN, frozenset(scopes))
    payload = json.loads(backend.registry._bootstrap_payload(credential))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend.registry, ORIGIN, backend)),
        base_url=ORIGIN,
        headers={
            "Authorization": "Bearer " + payload["access_token"],
            "Origin": ORIGIN,
            "X-CSRF-Token": payload["csrf_nonce"],
        },
    )


async def settled(client, item, states):
    for _ in range(1500):
        response = await client.get("/api/jobs/" + item["job_id"])
        response.raise_for_status()
        current = response.json()
        if current["state"] in states | {"failed", "uncertain", "cancelled"}:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError("bounded synthetic service job did not finish")


async def selected(client):
    profiles = (await client.get("/api/profiles")).json()
    source = next(p["id"] for p in profiles["profiles"] if p["format"] == "claude")
    target = next(p["id"] for p in profiles["profiles"] if p["format"] == "codex")
    source_session = (await client.get("/api/sessions", params={"profile_id": source})).json()[
        "sessions"
    ][0]["id"]
    target_session = (await client.get("/api/sessions", params={"profile_id": target})).json()[
        "sessions"
    ][0]["id"]
    return profiles, source_session, target, target_session


async def confirmed(client, action, proposal, key):
    response = await client.post(
        f"/api/{action}/{proposal['proposal_id']}/confirm",
        json={"body_fingerprint": proposal["fingerprint"], "idempotency_key": key},
    )
    response.raise_for_status()
    return await settled(client, response.json(), {"completed", "recovered"})


def test_actual_asgi_adoption_keeps_baseline_distinct_and_confirmation_idempotent(launched):
    backend, _, catalog, _, _, ledger, _, _ = launched
    before_catalog = catalog.read_bytes()
    before_count = len(LineageStore.inspect_existing(ledger).operations)

    async def run():
        async with client_for(backend) as client:
            _, source, _, target = await selected(client)
            response = await client.post(
                "/api/adopt/prepare",
                json={"source_session_id": source, "target_session_id": target},
            )
            response.raise_for_status()
            prepared = await settled(client, response.json(), {"reviewable"})
            assert prepared["state"] == "reviewable", prepared
            proposal = (await client.get("/api/proposals/" + prepared["proposal_id"])).json()
            result = await confirmed(client, "adopt", proposal, "adopt-once")
            assert result["state"] == "completed", result
            assert result["operation_id"] is None and result["subject"]["kind"] == "baseline"
            assert result["result"]["receipt_projection"]["provenance"] == "adopted_now"
            replay = await confirmed(client, "adopt", proposal, "adopt-once")
            assert replay["job_id"] == result["job_id"]

    asyncio.run(run())
    assert len(LineageStore.inspect_existing(ledger).operations) == before_count
    assert catalog.read_bytes() == before_catalog


def test_actual_asgi_reconciliation_uses_original_prepared_context_and_never_reimports(
    launched, monkeypatch
):
    backend, _, _, _, _, ledger, _, _ = launched
    original = operations._completion_record

    def interrupted(*args, **kwargs):
        from session_migrate.errors import SessionMigrateError

        raise SessionMigrateError("synthetic acknowledged interruption")

    async def run():
        async with client_for(backend) as client:
            profiles, source, target, _ = await selected(client)
            prepared_response = await client.post(
                "/api/proposals",
                json={
                    "source_session_id": source,
                    "target_profile_id": target,
                    "project_id": profiles["projects"][0]["id"],
                },
            )
            prepared = await settled(client, prepared_response.json(), {"reviewable"})
            assert prepared["state"] == "reviewable", prepared
            proposal = (await client.get("/api/proposals/" + prepared["proposal_id"])).json()
            monkeypatch.setattr(operations, "_completion_record", interrupted)
            response = await client.post(
                f"/api/proposals/{proposal['proposal_id']}/confirm",
                json={
                    "body_fingerprint": proposal["fingerprint"],
                    "idempotency_key": "interrupted-transfer",
                },
            )
            transfer = await settled(client, response.json(), {"uncertain"})
            assert transfer["state"] == "uncertain", transfer
            monkeypatch.setattr(operations, "_completion_record", original)
            # Refresh is the existing explicit CLI prerequisite, outside readonly HTTP.
            # C1 must resolve the newly published target from a real physical catalog.
            with Catalog(backend.catalog_path) as current:
                current.refresh(
                    claude_roots=tuple(
                        p.root for p in backend.profiles.values() if p.format.value == "claude"
                    ),
                    codex_roots=tuple(
                        p.root for p in backend.profiles.values() if p.format.value == "codex"
                    ),
                    include_auto=False,
                )
            view = LineageStore.inspect_existing(ledger)
            operation = next(
                o for o in view.operations if o.record.operation_id == transfer["operation_id"]
            )
            assert operation.durable_native_state is NativeState.ACKNOWLEDGED
            input_path = next(
                ref.path for ref in operation.record.references if ref.kind == "artifact"
            )
            from pathlib import Path

            original_native = Path(input_path).read_bytes()
            resources = (await client.get("/api/lineage")).json()["operations"]
            resource = next(
                row for row in resources if row["operation_id"] == transfer["operation_id"]
            )
            assert resource["recovery_context_id"] is not None
            response = await client.post(
                "/api/reconcile/prepare",
                json={
                    "operation_id": resource["id"],
                    "recovery_context_id": resource["recovery_context_id"],
                },
            )
            recovery = await settled(client, response.json(), {"reviewable"})
            assert recovery["state"] == "reviewable", recovery.get("result")
            review = (await client.get("/api/proposals/" + recovery["proposal_id"])).json()
            result = await confirmed(client, "reconcile", review, "reconcile-once")
            assert result["state"] == "completed", result
            assert result["subject"] == {"kind": "operation", "id": transfer["operation_id"]}
            assert Path(input_path).read_bytes() == original_native
            final = LineageStore.inspect_existing(ledger)
            assert len(final.operations) == len(view.operations)
            recovered = next(
                o for o in final.operations if o.record.operation_id == transfer["operation_id"]
            )
            assert recovered.durable_state is OperationState.COMPLETED

    asyncio.run(run())


def test_operation_tokens_cannot_cross_authenticated_runs(launched):
    from test_web_backend import dispatch

    backend, context, _, _, _, ledger, _, _ = launched
    token = dispatch(backend, context, "lineage")["operations"][0]["id"]
    before = (ledger / "ledger.sqlite3").read_bytes()

    async def run():
        async with client_for(backend) as client:
            response = await client.get("/api/lineage/" + token)
            assert response.status_code >= 400
            assert "operations" not in response.json()
            item = await client.post("/api/cancel-ready/prepare", json={"operation_id": token})
            item.raise_for_status()
            result = await settled(client, item.json(), {"reviewable"})
            assert result["state"] == "failed" and result.get("proposal_id") is None

    asyncio.run(run())
    assert (ledger / "ledger.sqlite3").read_bytes() == before


def test_actual_ready_service_http_cancel_binds_review_and_has_no_native_write(launched):
    from test_web_backend import selection

    backend, context, _, _, _, ledger, _, _ = launched
    profiles, _, target, source = selection(backend, context)
    run = backend.registry.authorize(context("POST"), "publish")
    prepared = backend._prepare(
        {
            "source_session_id": source,
            "target_profile_id": target,
            "project_id": profiles["projects"][0]["id"],
            "_run_id": run.run_id,
        }
    )
    try:
        record = operations._ready_record(prepared._state, proposal_metadata(prepared).proposal_id)
        with (
            LineageStore.open_existing(ledger) as store,
            store.lock_operation(record.operation_id, record.target_namespace) as guard,
        ):
            store.ready(record, guard)
    finally:
        prepared.discard()

    # This new run obtains its own token for the genuine qualified READY record.
    async def run_http():
        async with client_for(backend) as client:
            resource = next(
                row
                for row in (await client.get("/api/lineage")).json()["operations"]
                if row["operation_id"] == record.operation_id
            )
            response = await client.post(
                "/api/cancel-ready/prepare", json={"operation_id": resource["id"]}
            )
            reviewable = await settled(client, response.json(), {"reviewable"})
            assert reviewable["state"] == "reviewable", reviewable
            review = (await client.get("/api/proposals/" + reviewable["proposal_id"])).json()
            result = await confirmed(client, "cancel-ready", review, "cancel-ready-once")
            assert result["state"] == "completed", result
            assert result["result"]["durable_state"] == "cancelled"
            assert result["result"]["native_state"] == "native_not_started"

    asyncio.run(run_http())
    cancelled = next(
        o
        for o in LineageStore.inspect_existing(ledger).operations
        if o.record.operation_id == record.operation_id
    )
    assert cancelled.record == record and cancelled.durable_state is OperationState.CANCELLED
    assert cancelled.durable_native_state is NativeState.NATIVE_NOT_STARTED


@pytest.mark.parametrize("route", ["adopt", "reconcile", "cancel-ready"])
def test_new_service_routes_enforce_readonly_scope_and_reject_arbitrary_paths(launched, route):
    backend = launched[0]

    async def run():
        async with client_for(backend, publish=False) as client:
            response = await client.post(f"/api/{route}/prepare", json={"path": "/arbitrary"})
            assert response.status_code == 403
        async with client_for(backend) as client:
            response = await client.post(f"/api/{route}/prepare", json={"path": "/arbitrary"})
            assert response.status_code == 400
        assert not backend.jobs._jobs

    asyncio.run(run())
