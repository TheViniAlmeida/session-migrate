"""ASGI security tests use synthetic backend results and never open a listener."""

import asyncio
import json

import pytest

from session_migrate.web.app import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    create_app,
    decode_request,
)
from session_migrate.web.auth import RunRegistry

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")

ORIGIN = "http://127.0.0.1:8021"


class Backend:
    def __init__(self):
        self.calls = []
        self.document = {"schema_version": 1, "observed": True}

    async def dispatch(self, action, context, parameters, identifiers):
        self.calls.append((action, context, parameters, identifiers))
        return self.document


def setup_app(*, publish=False, clock=None):
    registry = RunRegistry(**({"clock": clock} if clock else {}))
    credential = registry.create(
        ORIGIN, frozenset({"readonly", "publish"} if publish else {"readonly"})
    )
    payload = json.loads(registry._bootstrap_payload(credential))
    backend = Backend()
    return registry, payload, backend, create_app(registry, ORIGIN, backend)


def headers(payload, *, mutation=False):
    values = {"Authorization": "Bearer " + payload["access_token"]}
    if mutation:
        values.update(
            {
                "Origin": ORIGIN,
                "X-CSRF-Token": payload["csrf_nonce"],
                "Content-Type": "application/json",
            }
        )
    return values


def request(app, method, path, **options):
    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            return await client.request(method, path, **options)

    return asyncio.run(call())


def test_public_assets_have_csp_and_never_embed_launch_credentials():
    _, payload, backend, app = setup_app()
    for path in ("/", "/static/dashboard.css", "/static/dashboard.js"):
        response = request(app, "GET", path)
        assert response.status_code == 200
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert not any(
            value in response.text for value in (payload["access_token"], payload["csrf_nonce"])
        )
    assert backend.calls == []
    assert request(app, "GET", "/static/auth.py").status_code == 404
    assert request(app, "GET", "/launch.json").status_code == 404


def test_api_needs_real_token_and_issued_context():
    registry, payload, backend, app = setup_app()
    assert request(app, "GET", "/api/profiles").status_code == 403
    response = request(app, "GET", "/api/profiles", headers=headers(payload))
    assert response.status_code == 200 and len(backend.calls) == 1
    assert registry.authorize(backend.calls[0][1], "readonly").run_id == payload["run_id"]


@pytest.mark.parametrize(
    "path", ["/api/proposals", "/api/proposals/example/confirm", "/api/jobs/example/cancel"]
)
def test_readonly_scope_cannot_publish(path):
    _, payload, backend, app = setup_app()
    assert (
        request(
            app, "POST", path, headers=headers(payload, mutation=True), content=b"{}"
        ).status_code
        == 403
    )
    assert backend.calls == []


@pytest.mark.parametrize("remove", ["Origin", "X-CSRF-Token", "Authorization"])
def test_post_requires_origin_nonce_and_token(remove):
    _, payload, backend, app = setup_app(publish=True)
    values = headers(payload, mutation=True)
    del values[remove]
    response = request(app, "POST", "/api/proposals", headers=values, content=b"{}")
    assert response.status_code == 403
    assert backend.calls == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("Host", "evil.invalid"),
        ("Host", "localhost:8021"),
        ("Origin", "null"),
        ("Origin", "http://127.0.0.1:8022"),
    ],
)
def test_host_and_present_origin_are_exact_for_public_and_private_routes(key, value):
    _, payload, backend, app = setup_app()
    for path in ("/", "/api/profiles"):
        values = headers(payload)
        values[key] = value
        response = request(app, "GET", path, headers=values)
        assert response.status_code == 403
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert backend.calls == []


@pytest.mark.parametrize(
    "name", ["Host", "Origin", "Authorization", "X-CSRF-Token", "Content-Type"]
)
def test_duplicate_security_headers_rejected(name):
    _, payload, backend, app = setup_app(publish=True)
    values = list(headers(payload, mutation=True).items())
    if name == "Host":
        values.extend([(name, "127.0.0.1:8021"), (name, "127.0.0.1:8021")])
    else:
        values.append((name, dict(values)[name]))
    response = request(app, "POST", "/api/proposals", headers=values, content=b"{}")
    assert response.status_code == 403
    assert backend.calls == []


@pytest.mark.parametrize(
    "content",
    [
        b'{"source_session_id":"a","source_session_id":"b"}',
        b'{"source_session_id":NaN}',
        b"[]",
        b'"body-canary"',
        b'{"\xff":1}',
        b"{" + b'"x":[' + b"[" * 20 + b"0" + b"]" * 20 + b"]}",
    ],
)
def test_closed_json_rejects_duplicate_unknown_shapes_and_deep_values(content):
    _, payload, backend, app = setup_app(publish=True)
    response = request(
        app, "POST", "/api/proposals", headers=headers(payload, mutation=True), content=content
    )
    assert response.status_code == 400
    assert backend.calls == []
    assert "body-canary" not in response.text


def test_request_and_response_bounds_are_enforced():
    _, payload, backend, app = setup_app(publish=True)
    response = request(
        app,
        "POST",
        "/api/proposals",
        headers=headers(payload, mutation=True),
        content=b" " * (MAX_REQUEST_BYTES + 1),
    )
    assert response.status_code == 413 and backend.calls == []
    backend.document = {"bounded": "x" * MAX_RESPONSE_BYTES}
    response = request(app, "GET", "/api/profiles", headers=headers(payload))
    assert response.status_code == 413


@pytest.mark.parametrize(
    "query", ["profile_id=a&profile_id=b", "root=/private", "cli=/bin/false", "token=canary"]
)
def test_query_duplicates_and_arbitrary_paths_denied(query):
    _, payload, backend, app = setup_app()
    response = request(app, "GET", "/api/sessions?" + query, headers=headers(payload))
    assert response.status_code == 400 and backend.calls == []
    assert "canary" not in response.text


def test_allowed_post_dispatch_uses_real_mutation_context_only():
    registry, payload, backend, app = setup_app(publish=True)
    response = request(
        app,
        "POST",
        "/api/proposals",
        headers=headers(payload, mutation=True),
        json={"source_session_id": "selected-source", "target_profile_id": "selected-target"},
    )
    assert response.status_code == 200 and backend.calls[0][0] == "prepare"
    assert registry.authorize(backend.calls[0][1], "publish").run_id == payload["run_id"]


def test_logout_invalidates_server_run_before_acknowledgement():
    _, payload, backend, app = setup_app()
    response = request(
        app, "POST", "/api/auth/logout", headers=headers(payload, mutation=True), content=b"{}"
    )
    assert response.status_code == 200 and response.json()["revoked"] is True
    assert request(app, "GET", "/api/profiles", headers=headers(payload)).status_code == 403
    assert backend.calls == []


def test_expiry_rejects_existing_token_without_backend_dispatch():
    ticks = [0.0]
    _, payload, backend, app = setup_app(clock=lambda: ticks[0])
    ticks[0] = 1800.0
    assert request(app, "GET", "/api/profiles", headers=headers(payload)).status_code == 403
    assert backend.calls == []


def test_backend_errors_are_fixed_and_never_echo_body_or_headers():
    _, payload, backend, app = setup_app()

    async def fail(*_):
        raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE_BODY")

    backend.dispatch = fail
    response = request(app, "GET", "/api/profiles", headers=headers(payload))
    assert response.status_code == 500 and response.json() == {"reason": "operation_unavailable"}
    assert "SYNTHETIC_PRIVATE" not in response.text


def test_request_decoder_limits_tree_without_echoing_input():
    with pytest.raises(Exception, match="invalid_json"):
        decode_request(b'{"' + b"x" * 129 + b'":1}')
