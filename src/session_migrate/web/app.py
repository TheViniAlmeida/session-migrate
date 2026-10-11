"""Bounded HTTP boundary for an explicitly selected local workspace."""

from __future__ import annotations

import json
import re
from importlib.resources import files
from urllib.parse import urlsplit

from session_migrate.web.auth import RunRegistry, WebAuthError, validate_origin

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
_ASSETS = {"dashboard.css": "text/css", "dashboard.js": "text/javascript"}
_OPAQUE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class WebInputError(Exception):
    pass


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise WebInputError("invalid_json")
        result[key] = value
    return result


def _json_tree(value, depth=0, budget=None):
    if budget is None:
        budget = [4096]
    budget[0] -= 1
    if depth > 16 or budget[0] < 0:
        raise WebInputError("invalid_json")
    if type(value) is dict:
        if len(value) > 64:
            raise WebInputError("invalid_json")
        for key, item in value.items():
            if len(key.encode("utf-8")) > 128:
                raise WebInputError("invalid_json")
            _json_tree(item, depth + 1, budget)
    elif type(value) is list:
        for item in value:
            _json_tree(item, depth + 1, budget)
    elif type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise WebInputError("invalid_json") from None
    elif value is not None and type(value) not in (str, int, bool):
        raise WebInputError("invalid_json")


def decode_request(data):
    if type(data) is not bytes or len(data) > MAX_REQUEST_BYTES:
        raise WebInputError("request_too_large")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(WebInputError("invalid_json")),
        )
        if type(value) is not dict:
            raise WebInputError("invalid_json")
        _json_tree(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WebInputError("invalid_json") from None


def validate_parameters(action, parameters, identifiers):
    for value in identifiers.values():
        if type(value) is not str or _OPAQUE.fullmatch(value) is None:
            raise WebInputError("identifier_invalid")
    nullable = {"project_id", "target_title", "title_policy"}
    for name, value in parameters.items():
        if value is None and name in nullable:
            continue
        if name == "selection":
            if type(value) is not list or not 1 <= len(value) <= 128:
                raise WebInputError("selection_invalid")
            for item in value:
                if type(item) is not dict or set(item) != {
                    "first_event",
                    "last_event",
                    "block_index",
                }:
                    raise WebInputError("selection_invalid")
                if any(
                    type(item[key]) is not int or not 0 <= item[key] <= 100000
                    for key in ("first_event", "last_event")
                ):
                    raise WebInputError("selection_invalid")
                if item["first_event"] > item["last_event"] or (
                    item["block_index"] is not None
                    and (
                        type(item["block_index"]) is not int
                        or not 0 <= item["block_index"] <= 100000
                    )
                ):
                    raise WebInputError("selection_invalid")
            continue
        if type(value) is not str:
            raise WebInputError("field_type_invalid")
        if name == "document":
            if not value or len(value.encode("utf-8")) > 60000:
                raise WebInputError("document_invalid")
        elif name in {"model", "model_provider", "target_cli_version"}:
            if not 0 < len(value.encode("utf-8")) <= 256 or any(
                ord(c) < 32 or ord(c) == 127 for c in value
            ):
                raise WebInputError("field_invalid")
        elif name == "target_title":
            if len(value.encode("utf-8")) > 1024:
                raise WebInputError("title_invalid")
        elif name == "title_policy":
            if value not in ("native", "local_alias"):
                raise WebInputError("title_policy_invalid")
        elif name in {"body_fingerprint", "document_sha256"}:
            if _FINGERPRINT.fullmatch(value) is None:
                raise WebInputError("fingerprint_invalid")
        elif name == "limit":
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100:
                raise WebInputError("limit_invalid")
        elif _OPAQUE.fullmatch(value) is None:
            raise WebInputError("identifier_invalid")
    if action == "prepare" and not {"source_session_id", "target_profile_id"} <= parameters.keys():
        raise WebInputError("fields_missing")
    required = {
        "prepare_adopt": {"source_session_id", "target_session_id"},
        "prepare_reconcile": {"operation_id", "recovery_context_id"},
        "prepare_cancel_ready": {"operation_id"},
        "accept_handoff": {"document", "document_sha256"},
    }
    if action in required and set(parameters) != required[action]:
        raise WebInputError("fields_missing")
    runtime_required = {
        "source_session_id",
        "target_profile_id",
        "project_id",
        "model",
        "model_provider",
        "target_cli_version",
    }
    if action in {"capture_handoff", "capture_return_new", "prepare_return_new"}:
        if action != "capture_handoff":
            runtime_required.add("operation_id")
        if action != "prepare_return_new":
            runtime_required.add("selection")
        if not runtime_required <= parameters.keys():
            raise WebInputError("fields_missing")
    if action in {
        "confirm",
        "confirm_adopt",
        "confirm_reconcile",
        "confirm_cancel_ready",
        "confirm_handoff",
        "confirm_return_summary",
        "confirm_return_full",
    } and set(parameters) != {"body_fingerprint", "idempotency_key"}:
        raise WebInputError("fields_missing")


def create_app(registry: RunRegistry, origin: str, backend):
    """Create ASGI only. Binding a socket and issuing launch credentials are separate."""
    try:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route
    except ImportError:
        raise WebAuthError("web_extra_unavailable") from None

    if type(registry) is not RunRegistry:
        raise WebAuthError("registry_invalid")
    origin = validate_origin(origin)
    host = urlsplit(origin).netloc
    asset_root = files("session_migrate.web").joinpath("assets")
    assets = {"index.html": asset_root.joinpath("index.html").read_bytes()}
    assets.update({name: asset_root.joinpath(name).read_bytes() for name in _ASSETS})
    if any(len(data) > MAX_RESPONSE_BYTES for data in assets.values()):
        raise WebAuthError("asset_limit")

    def public_json(document, status=200):
        try:
            data = json.dumps(
                document, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ).encode("ascii")
            if len(data) > MAX_RESPONSE_BYTES:
                return JSONResponse({"reason": "response_limit"}, status_code=413)
            return Response(data, status_code=status, media_type="application/json")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return JSONResponse({"reason": "response_unavailable"}, status_code=500)

    async def static(request):
        name = request.path_params.get("asset", "index.html")
        if name not in assets:
            return public_json({"reason": "not_found"}, 404)
        return Response(
            assets[name], media_type="text/html" if name == "index.html" else _ASSETS[name]
        )

    async def endpoint(request):
        action, scope, allowed = request.scope["route_contract"]
        try:
            context = registry.authenticate(
                request.headers.get("authorization"),
                host=request.headers.get("host"),
                origin=request.headers.get("origin"),
                method=request.method,
                csrf_nonce=request.headers.get("x-csrf-token"),
            )
            registry.authorize(context, scope, mutation=request.method == "POST")
            if request.method == "POST":
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].strip()
                    != "application/json"
                ):
                    raise WebInputError("content_type_invalid")
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_REQUEST_BYTES:
                        raise WebInputError("request_too_large")
                    chunks.append(chunk)
                parameters = decode_request(b"".join(chunks))
            else:
                pairs = list(request.query_params.multi_items())
                if len(pairs) > 16 or len({key for key, _ in pairs}) != len(pairs):
                    raise WebInputError("query_invalid")
                parameters = dict(pairs)
            registry.authorize(context, scope, mutation=request.method == "POST")
            if set(parameters) - set(allowed):
                raise WebInputError("fields_invalid")
            validate_parameters(action, parameters, dict(request.path_params))
            registry.authorize(context, scope, mutation=request.method == "POST")
            if action == "logout":
                registry.revoke(context)
                return public_json({"schema_version": 1, "revoked": True})
            document = await backend.dispatch(
                action, context, parameters, dict(request.path_params)
            )
            return public_json(document)
        except WebAuthError as error:
            return public_json({"reason": error.reason}, 403)
        except WebInputError as error:
            return public_json(
                {"reason": str(error)}, 413 if str(error) == "request_too_large" else 400
            )
        except Exception:
            return public_json({"reason": "operation_unavailable"}, 500)

    def api_route(path, action, method="GET", scope="readonly", allowed=()):
        async def handler(request):
            request.scope["route_contract"] = (action, scope, allowed)
            return await endpoint(request)

        return Route(path, handler, methods=[method])

    routes = [
        Route("/", static, methods=["GET"]),
        Route("/static/{asset}", static, methods=["GET"]),
        api_route("/api/profiles", "profiles"),
        api_route("/api/capabilities", "capabilities"),
        api_route("/api/sessions", "sessions", allowed=("profile_id", "cursor", "limit")),
        api_route("/api/sessions/{session_id}/preview", "preview"),
        api_route("/api/sessions/{session_id}/usage", "usage", allowed=("cursor", "limit")),
        api_route("/api/lineage", "lineage", allowed=("cursor", "limit")),
        api_route("/api/lineage/{operation_id}", "inspect_lineage"),
        api_route("/api/assessment", "assessment", allowed=("operation_id",)),
        api_route("/api/recovery", "recovery"),
        api_route("/api/proposals/{proposal_id}", "review"),
        api_route("/api/jobs/{job_id}", "job"),
        api_route("/api/handoffs/{draft_id}", "handoff_document"),
        api_route(
            "/api/handoffs/{draft_id}/accept",
            "accept_handoff",
            "POST",
            "publish",
            ("document", "document_sha256"),
        ),
        api_route("/api/handoffs/{draft_id}/discard", "discard_handoff", "POST", "publish"),
        api_route(
            "/api/proposals",
            "prepare",
            "POST",
            "publish",
            (
                "source_session_id",
                "target_profile_id",
                "project_id",
                "target_title",
                "title_policy",
            ),
        ),
        api_route(
            "/api/proposals/{proposal_id}/confirm",
            "confirm",
            "POST",
            "publish",
            ("body_fingerprint", "idempotency_key"),
        ),
        api_route(
            "/api/adopt/prepare",
            "prepare_adopt",
            "POST",
            "publish",
            ("source_session_id", "target_session_id"),
        ),
        api_route(
            "/api/reconcile/prepare",
            "prepare_reconcile",
            "POST",
            "publish",
            ("operation_id", "recovery_context_id"),
        ),
        api_route(
            "/api/cancel-ready/prepare",
            "prepare_cancel_ready",
            "POST",
            "publish",
            ("operation_id",),
        ),
        api_route(
            "/api/adopt/{proposal_id}/confirm",
            "confirm_adopt",
            "POST",
            "publish",
            ("body_fingerprint", "idempotency_key"),
        ),
        api_route(
            "/api/reconcile/{proposal_id}/confirm",
            "confirm_reconcile",
            "POST",
            "publish",
            ("body_fingerprint", "idempotency_key"),
        ),
        api_route(
            "/api/cancel-ready/{proposal_id}/confirm",
            "confirm_cancel_ready",
            "POST",
            "publish",
            ("body_fingerprint", "idempotency_key"),
        ),
        api_route("/api/jobs/{job_id}/cancel", "cancel", "POST", "publish"),
        api_route("/api/auth/logout", "logout", "POST"),
        api_route("/api/auth/revoke", "logout", "POST"),
    ]

    runtime_fields = (
        "source_session_id",
        "target_profile_id",
        "project_id",
        "model",
        "model_provider",
        "target_cli_version",
        "target_title",
        "title_policy",
    )
    routes.extend(
        [
            api_route(
                "/api/handoffs/capture",
                "capture_handoff",
                "POST",
                "publish",
                (*runtime_fields, "selection"),
            ),
            api_route(
                "/api/return-new/capture",
                "capture_return_new",
                "POST",
                "publish",
                (*runtime_fields, "selection", "operation_id"),
            ),
            api_route(
                "/api/return-new/prepare",
                "prepare_return_new",
                "POST",
                "publish",
                (*runtime_fields, "operation_id"),
            ),
            api_route(
                "/api/handoffs/{proposal_id}/confirm",
                "confirm_handoff",
                "POST",
                "publish",
                ("body_fingerprint", "idempotency_key"),
            ),
            api_route(
                "/api/return-new-summary/{proposal_id}/confirm",
                "confirm_return_summary",
                "POST",
                "publish",
                ("body_fingerprint", "idempotency_key"),
            ),
            api_route(
                "/api/return-new-full/{proposal_id}/confirm",
                "confirm_return_full",
                "POST",
                "publish",
                ("body_fingerprint", "idempotency_key"),
            ),
        ]
    )

    application = Starlette(routes=routes)

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await application(scope, receive, send)
            return
        headers = scope.get("headers", [])
        counts = {}
        for name, _value in headers:
            key = name.lower()
            counts[key] = counts.get(key, 0) + 1
        safe = not any(
            counts.get(key, 0) > 1
            for key in (
                b"host",
                b"origin",
                b"authorization",
                b"x-csrf-token",
                b"content-type",
                b"content-length",
            )
        )
        try:
            values = {name.lower(): value.decode("ascii") for name, value in headers}
        except UnicodeError:
            safe = False
            values = {}
        safe = safe and values.get(b"host") == host and values.get(b"origin", origin) == origin
        safe = safe and len(headers) <= 64 and sum(len(n) + len(v) for n, v in headers) <= 16384

        async def secured_send(message):
            if message["type"] == "http.response.start":
                additions = [
                    (name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in _SECURITY_HEADERS.items()
                ]
                message = {**message, "headers": [*message.get("headers", []), *additions]}
            await send(message)

        if len(scope.get("query_string", b"")) > MAX_REQUEST_BYTES:
            await public_json({"reason": "request_too_large"}, 413)(scope, receive, secured_send)
            return

        if not safe:
            await public_json({"reason": "request_denied"}, 403)(scope, receive, secured_send)
            return
        await application(scope, receive, secured_send)

    return guarded
