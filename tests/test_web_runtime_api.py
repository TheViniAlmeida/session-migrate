"""HTTP runtime consumers use genuine private F5 files, authorities and ledger."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import test_continuity as fixtures

from session_migrate.cli import build_parser
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, codex
from session_migrate.handoff_types import HANDOFF_PREFIX
from session_migrate.lineage import LineageStore
from session_migrate.model import Role
from session_migrate.web.app import create_app
from session_migrate.web.auth import RunRegistry
from session_migrate.web.backend import BackendError, WorkspaceBackend
from session_migrate.web.launch import selected_runtime_actions

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")
ORIGIN = "http://127.0.0.1:8021"
ACTIONS = frozenset({"summary", "return_new_summary", "return_new_full"})
BODY = "Literal USER 👋\r\n<html>data & é</html>\nNext step: inspect current B"
WORKED = "Fresh work on actual current B"


@dataclass(repr=False)
class Workspace:
    backend: object
    registry: object
    payload: dict
    app: object
    catalog: object
    profiles: tuple
    source_path: object
    target_path: object
    ledger: object
    parent: object

    def headers(self, payload=None):
        payload = self.payload if payload is None else payload
        return {
            "Authorization": "Bearer " + payload["access_token"],
            "Origin": ORIGIN,
            "X-CSRF-Token": payload["csrf_nonce"],
        }

    def request(self, method, path, *, payload=None, **options):
        async def call():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN
            ) as client:
                return await client.request(method, path, headers=self.headers(payload), **options)

        return asyncio.run(call())


@pytest.fixture
def workspace(tmp_path, request):
    settings = getattr(request, "param", {})
    catalog, _, ledger, parent, env, profiles, source_path = fixtures.actual_b3_fixture(
        tmp_path, "profiles"
    )
    target_path = next(profiles[1].root.rglob("*.jsonl"))
    if settings.get("worked"):
        with target_path.open("ab") as output:
            for role, text in (("user", WORKED), ("assistant", "Completed fresh synthetic work")):
                row = {
                    "timestamp": "2026-10-10T12:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [
                            {
                                "type": "input_text" if role == "user" else "output_text",
                                "text": text,
                            }
                        ],
                    },
                }
                output.write(json.dumps(row).encode() + b"\n")
                mirror = {
                    "timestamp": row["timestamp"],
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message" if role == "user" else "agent_message",
                        "message": text,
                    },
                }
                output.write(json.dumps(mirror).encode() + b"\n")
        catalog.refresh(
            claude_roots=(profiles[0].root,), codex_roots=(profiles[1].root,), include_auto=False
        )
    catalog.close()
    registry = RunRegistry()
    scopes = (
        frozenset({"readonly", "publish"})
        if settings.get("publish", True)
        else frozenset({"readonly"})
    )
    credential = registry.create(ORIGIN, scopes)
    payload = json.loads(registry._bootstrap_payload(credential))
    keywords = (
        {}
        if settings.get("default_actions")
        else {"runtime_actions": settings.get("actions", ACTIONS)}
    )
    backend = WorkspaceBackend(
        registry,
        catalog.path,
        profiles,
        projects={"Private fixture": tmp_path / "work"},
        base_environ=env,
        **keywords,
    )
    result = Workspace(
        backend,
        registry,
        payload,
        create_app(registry, ORIGIN, backend),
        catalog.path,
        profiles,
        source_path,
        target_path,
        ledger,
        parent,
    )
    try:
        yield result
    finally:
        registry.close()
        backend.close()


def response_json(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def selected_request(workspace, mode="summary"):
    values = response_json(workspace.request("GET", "/api/profiles"))
    source_format, target_format = ("claude", "codex") if mode == "summary" else ("codex", "claude")
    source = next(p["id"] for p in values["profiles"] if p["format"] == source_format)
    target = next(p["id"] for p in values["profiles"] if p["format"] == target_format)
    page = response_json(workspace.request("GET", "/api/sessions", params={"profile_id": source}))
    parameters = {
        "source_session_id": page["sessions"][0]["id"],
        "target_profile_id": target,
        "project_id": values["projects"][0]["id"],
        "model": "synthetic-model",
        "model_provider": "openai" if mode == "summary" else "anthropic",
        "target_cli_version": "0.144.4" if mode == "summary" else "2.1.209",
    }
    if mode != "return_new_full":
        parameters["selection"] = [
            {"first_event": 0, "last_event": 1 if mode == "summary" else 3, "block_index": None}
        ]
    if mode != "summary":
        lineage = response_json(workspace.request("GET", "/api/lineage"))
        parameters["operation_id"] = next(
            op["id"]
            for op in lineage["operations"]
            if op["operation_id"] == workspace.parent.record.operation_id
        )
    return parameters


def wait_job(workspace, job, states):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = response_json(workspace.request("GET", "/api/jobs/" + job["job_id"]))
        if job["state"] in states:
            return job
        time.sleep(0.01)
    pytest.fail("genuine runtime HTTP job did not reach its expected state")


def capture(workspace, parameters=None, mode="summary"):
    path = "/api/handoffs/capture" if mode == "summary" else "/api/return-new/capture"
    job = response_json(
        workspace.request("POST", path, json=parameters or selected_request(workspace, mode))
    )
    job = wait_job(workspace, job, {"editable", "failed", "cancelled"})
    assert job["state"] == "editable", job
    assert "document" not in job["result"]
    draft = response_json(workspace.request("GET", "/api/handoffs/" + job["result"]["draft_id"]))
    assert draft["document_sha256"] == hashlib.sha256(draft["document"].encode()).hexdigest()
    return job, draft


def accept(workspace, draft, *, body=BODY, digest=None):
    digest = hashlib.sha256(body.encode()).hexdigest() if digest is None else digest
    job = response_json(
        workspace.request(
            "POST",
            "/api/handoffs/" + draft["draft_id"] + "/accept",
            json={"document": body, "document_sha256": digest},
        )
    )
    job = wait_job(workspace, job, {"reviewable", "failed", "cancelled"})
    return job


def review(workspace, job):
    assert job["state"] == "reviewable", job
    proposal = response_json(workspace.request("GET", "/api/proposals/" + job["proposal_id"]))
    assert BODY not in json.dumps(proposal)
    assert str(workspace.source_path.parent) not in json.dumps(proposal)
    return proposal


def confirm(workspace, proposal, *, key="http-native-once"):
    resource = {
        "summary": "handoffs",
        "return_new_summary": "return-new-summary",
        "return_new_full": "return-new-full",
    }[proposal["native_action"]]
    path = "/api/" + resource + "/" + proposal["proposal_id"] + "/confirm"
    data = {"body_fingerprint": proposal["fingerprint"], "idempotency_key": key}
    job = response_json(workspace.request("POST", path, json=data))
    job = wait_job(workspace, job, {"completed", "failed", "uncertain", "cancelled"})
    assert job["state"] == "completed", job
    replay = response_json(workspace.request("POST", path, json=data))
    assert replay["job_id"] == job["job_id"]
    return job


def test_http_genuine_summary_literal_utf8_sha_accept_proposal_and_publication(workspace):
    source_before = workspace.source_path.read_bytes()
    ledger_before = (workspace.ledger / "ledger.sqlite3").read_bytes()
    job, draft = capture(workspace)
    assert "synthetic-1" in draft["document"]
    assert (workspace.ledger / "ledger.sqlite3").read_bytes() == ledger_before
    assert len(list(workspace.profiles[1].root.rglob("*.jsonl"))) == 1
    accepted = accept(workspace, draft)
    proposal = review(workspace, accepted)
    assert proposal["native_action"] == "summary"
    assert (
        proposal["metadata"]["handoff"]["document_sha256"]
        == hashlib.sha256(BODY.encode()).hexdigest()
    )
    assert (workspace.ledger / "ledger.sqlite3").read_bytes() == ledger_before
    result = confirm(workspace, proposal)
    view = LineageStore.inspect_existing(workspace.ledger)
    assert len(view.operations) == 2
    operation = next(
        op for op in view.operations if op.record.operation_id == result["operation_id"]
    )
    assert operation.receipt.continuity_reason == "derived_handoff"
    assert (
        operation.receipt.derivation.input.document_sha256
        == hashlib.sha256(BODY.encode()).hexdigest()
    )
    native_id = result["result"]["legacy_result"]["session_id"]
    path = next(
        p
        for p in workspace.profiles[1].root.rglob("*.jsonl")
        if p.name.endswith(native_id + ".jsonl")
    )
    session = codex.parse(path)
    assert session.events[0].role is Role.USER
    assert session.events[0].text == HANDOFF_PREFIX + "\n\n" + BODY
    assert path.read_bytes() != workspace.target_path.read_bytes()
    assert workspace.source_path.read_bytes() == source_before
    assert job["native_action"] == result["native_action"] == "summary"


@pytest.mark.parametrize("workspace", [{"worked": True}], indirect=True)
@pytest.mark.parametrize("mode", ["return_new_full", "return_new_summary"])
def test_http_return_new_real_parent_current_worked_b_and_distinct_target(workspace, mode):
    parameters = selected_request(workspace, mode)
    b_before = workspace.target_path.read_bytes()
    assert WORKED.encode() in b_before
    if mode == "return_new_summary":
        _, draft = capture(workspace, parameters, mode)
        assert WORKED in draft["document"]
        proposal = review(workspace, accept(workspace, draft))
    else:
        job = response_json(workspace.request("POST", "/api/return-new/prepare", json=parameters))
        proposal = review(
            workspace, wait_job(workspace, job, {"reviewable", "failed", "cancelled"})
        )
    assert proposal["native_action"] == mode
    result = confirm(workspace, proposal)
    view = LineageStore.inspect_existing(workspace.ledger)
    operation = next(
        op for op in view.operations if op.record.operation_id == result["operation_id"]
    )
    assert operation.record.source.identity == workspace.parent.receipt.target_identity
    assert operation.record.source.artifact_sha256 == hashlib.sha256(b_before).hexdigest()
    assert operation.receipt.return_link.parent_operation_id == workspace.parent.record.operation_id
    assert operation.receipt.target_identity.namespace.native_id not in {
        fixtures.SOURCE_ID,
        fixtures.TARGET_ID,
    }
    assert operation.receipt.continuity_reason == (
        "derived_handoff" if mode.endswith("summary") else "tracked"
    )
    target_id = operation.receipt.target_identity.namespace.native_id
    target_path = next(
        p for p in workspace.profiles[0].root.rglob("*.jsonl") if p.stem == target_id
    )
    current = claude.parse(target_path)
    assert any(
        (BODY if mode.endswith("summary") else WORKED) in (event.text or "")
        for event in current.events
    )
    assert len(list(workspace.profiles[0].root.rglob("*.jsonl"))) == 2
    assert workspace.target_path.read_bytes() == b_before


@pytest.mark.parametrize("workspace", [{"default_actions": True}], indirect=True)
@pytest.mark.parametrize(
    "path,mode",
    [
        ("/api/handoffs/capture", "summary"),
        ("/api/return-new/capture", "return_new_summary"),
        ("/api/return-new/prepare", "return_new_full"),
    ],
)
def test_default_runtime_capabilities_are_off_and_deny_before_enqueue(workspace, path, mode):
    assert workspace.backend._runtime_actions == frozenset()
    capabilities = response_json(workspace.request("GET", "/api/capabilities"))
    assert capabilities["publish_enabled"] is True
    assert not any(
        capabilities[k]
        for k in ("summary_available", "return_new_available", "return_summary_available")
    )
    before = (workspace.ledger / "ledger.sqlite3").read_bytes()
    response_json(workspace.request("POST", path, json=selected_request(workspace, mode)), 500)
    assert not workspace.backend.jobs._jobs
    assert (workspace.ledger / "ledger.sqlite3").read_bytes() == before


@pytest.mark.parametrize("workspace", [{"publish": False}], indirect=True)
def test_runtime_http_requires_publish_before_enqueue(workspace):
    parameters = selected_request(workspace)
    response_json(workspace.request("POST", "/api/handoffs/capture", json=parameters), 403)
    assert not workspace.backend.jobs._jobs


@pytest.mark.parametrize(
    "action,expected",
    [("handoff", {"summary"}), ("return_new", {"return_new_full", "return_new_summary"})],
)
def test_launch_runtime_flags_require_publish_and_select_exact_actions(action, expected):
    args = SimpleNamespace(allow_publish=False)
    assert selected_runtime_actions(args) == frozenset()
    setattr(args, "allow_" + action, True)
    with pytest.raises(SessionMigrateError, match="web_runtime_requires_publish"):
        selected_runtime_actions(args)
    args.allow_publish = True
    assert selected_runtime_actions(args) == frozenset(expected)
    setattr(args, "allow_" + action, 1)
    with pytest.raises(SessionMigrateError, match="web_runtime_selection_invalid"):
        selected_runtime_actions(args)


def test_cli_runtime_flags_are_explicit_and_default_off():
    common = [
        "--catalog",
        "/fixture/catalog",
        "web",
        "--profiles",
        "/fixture/profiles",
        "--profile",
        "selected",
        "--bootstrap",
        "/fixture/launch",
    ]
    args = build_parser().parse_args(common)
    assert args.allow_handoff is False and args.allow_return_new is False
    assert selected_runtime_actions(args) == frozenset()
    enabled = build_parser().parse_args(
        [*common, "--allow-publish", "--allow-handoff", "--allow-return-new"]
    )
    assert selected_runtime_actions(enabled) == ACTIONS


@pytest.mark.parametrize(
    "change",
    [
        "bool_first",
        "bool_block",
        "reverse",
        "empty",
        "unknown_range",
        "unknown_top",
        "body_on_capture",
        "missing_model",
        "unsupported_title_policy",
    ],
)
def test_http_runtime_closed_fields_and_ranges_deny_before_job(workspace, change):
    parameters = selected_request(workspace)
    if change == "bool_first":
        parameters["selection"][0]["first_event"] = True
    elif change == "bool_block":
        parameters["selection"][0]["block_index"] = False
    elif change == "reverse":
        parameters["selection"][0]["first_event"] = 2
    elif change == "empty":
        parameters["selection"] = []
    elif change == "unknown_range":
        parameters["selection"][0]["caller_proof"] = "forged"
    elif change == "unknown_top":
        parameters["cwd"] = "/caller/path"
    elif change == "body_on_capture":
        parameters["document"] = "not a capture"
    elif change == "missing_model":
        parameters.pop("model")
    else:
        parameters["title_policy"] = "future-policy"
    before = (workspace.ledger / "ledger.sqlite3").read_bytes()
    response_json(workspace.request("POST", "/api/handoffs/capture", json=parameters), 400)
    assert not workspace.backend.jobs._jobs
    assert (workspace.ledger / "ledger.sqlite3").read_bytes() == before


@pytest.mark.parametrize("actions", [["summary"], frozenset({"future"}), True])
def test_constructor_rejects_untyped_and_unknown_runtime_actions(workspace, actions):
    with pytest.raises(BackendError, match="launch_invalid"):
        WorkspaceBackend(
            workspace.registry, workspace.catalog, workspace.profiles, runtime_actions=actions
        )


def test_http_accept_digest_is_exact_and_wrong_digest_does_not_publish(workspace):
    _, draft = capture(workspace)
    before = (workspace.ledger / "ledger.sqlite3").read_bytes()
    accepted = accept(workspace, draft, digest="0" * 64)
    assert accepted["state"] == "failed"
    assert (workspace.ledger / "ledger.sqlite3").read_bytes() == before
    assert len(list(workspace.profiles[1].root.rglob("*.jsonl"))) == 1
    replay = response_json(
        workspace.request(
            "POST",
            "/api/handoffs/" + draft["draft_id"] + "/accept",
            json={"document": BODY, "document_sha256": "0" * 64},
        )
    )
    assert replay["job_id"] == accepted["job_id"]


def test_http_proposal_confirm_action_mismatch_is_not_authority(workspace):
    _, draft = capture(workspace)
    proposal = review(workspace, accept(workspace, draft))
    before = (workspace.ledger / "ledger.sqlite3").read_bytes()
    body = {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "wrong-action"}
    for resource in ("proposals", "return-new-summary", "return-new-full"):
        response_json(
            workspace.request(
                "POST", "/api/" + resource + "/" + proposal["proposal_id"] + "/confirm", json=body
            ),
            500,
        )
    assert (workspace.ledger / "ledger.sqlite3").read_bytes() == before
    assert len(list(workspace.profiles[1].root.rglob("*.jsonl"))) == 1
    confirm(workspace, proposal)


def test_http_run_scope_and_logout_protect_draft_job_proposal_and_confirmation(workspace):
    job, draft = capture(workspace)
    credential = workspace.registry.create(ORIGIN, frozenset({"readonly", "publish"}))
    other = json.loads(workspace.registry._bootstrap_payload(credential))
    response_json(workspace.request("GET", "/api/jobs/" + job["job_id"], payload=other), 500)
    response_json(
        workspace.request("GET", "/api/handoffs/" + draft["draft_id"], payload=other), 500
    )
    response_json(
        workspace.request(
            "POST",
            "/api/handoffs/" + draft["draft_id"] + "/accept",
            payload=other,
            json={"document": BODY, "document_sha256": hashlib.sha256(BODY.encode()).hexdigest()},
        ),
        500,
    )
    proposal = review(workspace, accept(workspace, draft))
    response_json(
        workspace.request("GET", "/api/proposals/" + proposal["proposal_id"], payload=other), 500
    )
    body = {"body_fingerprint": proposal["fingerprint"], "idempotency_key": "revoked"}
    response_json(
        workspace.request(
            "POST",
            "/api/handoffs/" + proposal["proposal_id"] + "/confirm",
            payload=other,
            json=body,
        ),
        500,
    )
    before = (workspace.ledger / "ledger.sqlite3").read_bytes()
    assert response_json(workspace.request("POST", "/api/auth/logout", json={})) == {
        "schema_version": 1,
        "revoked": True,
    }
    response_json(
        workspace.request(
            "POST", "/api/handoffs/" + proposal["proposal_id"] + "/confirm", json=body
        ),
        403,
    )
    assert (workspace.ledger / "ledger.sqlite3").read_bytes() == before
    assert len(list(workspace.profiles[1].root.rglob("*.jsonl"))) == 1
