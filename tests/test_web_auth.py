"""Authentication uses real private contexts and synthetic launch credentials."""

import gc
import json
import threading

import pytest

from session_migrate.web.auth import (
    BootstrapCredential,
    RunContext,
    RunRegistry,
    WebAuthError,
    validate_origin,
)

ORIGIN = "http://127.0.0.1:8021"


def launch(*, publish=False, lifetime=1800, **options):
    registry = RunRegistry(**options)
    scopes = frozenset({"readonly", "publish"} if publish else {"readonly"})
    credential = registry.create(ORIGIN, scopes, lifetime=lifetime)
    payload = json.loads(registry._bootstrap_payload(credential))
    return registry, credential, payload


def context(registry, payload, *, method="GET", **options):
    values = {
        "host": "127.0.0.1:8021",
        "origin": ORIGIN if method == "POST" else None,
        "method": method,
        "csrf_nonce": payload["csrf_nonce"] if method == "POST" else None,
    }
    values.update(options)
    return registry.authenticate("Bearer " + payload["access_token"], **values)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8021",
        "http://0.0.0.0:8021",
        "http://127.0.0.1:8021/",
        "http://127.0.0.1:8021?x=1",
        "http://127.0.0.1:8021#x",
        "https://127.0.0.1:8021",
        "http://user@127.0.0.1:8021",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1",
        "http://[::1]:8021/",
        "null",
        None,
        True,
    ],
)
def test_origin_is_exact_loopback_with_port(value):
    with pytest.raises(WebAuthError, match="origin_invalid"):
        validate_origin(value)


def test_ipv6_origin_and_host_are_exact():
    registry = RunRegistry()
    credential = registry.create("http://[::1]:8021")
    payload = json.loads(registry._bootstrap_payload(credential))
    issued = registry.authenticate(
        "Bearer " + payload["access_token"], host="[::1]:8021", origin=None, method="GET"
    )
    assert registry.authorize(issued, "readonly").origin == "http://[::1]:8021"
    with pytest.raises(WebAuthError, match="host_denied"):
        registry.authenticate(
            "Bearer " + payload["access_token"], host="::1:8021", origin=None, method="GET"
        )


def test_private_context_not_dto_or_run_id():
    registry, _, payload = launch()
    issued = context(registry, payload)
    for fake in (payload["run_id"], {"run_id": payload["run_id"]}, object.__new__(RunContext)):
        with pytest.raises(WebAuthError, match="context_invalid"):
            registry.authorize(fake, "readonly")
    with pytest.raises(WebAuthError, match="context_invalid"):
        RunContext()
    other, _, _ = launch()
    with pytest.raises(WebAuthError, match="context_invalid"):
        other.authorize(issued, "readonly")


@pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "post", True, None])
def test_methods_are_closed(method):
    registry, _, payload = launch()
    with pytest.raises(WebAuthError, match="method_denied"):
        context(registry, payload, method=method)


@pytest.mark.parametrize("host", ["localhost:8021", "127.0.0.1:8022", "evil.invalid", None])
def test_host_rebinding_rejected(host):
    registry, _, payload = launch()
    with pytest.raises(WebAuthError, match="host_denied"):
        context(registry, payload, host=host)


@pytest.mark.parametrize("origin", ["null", "http://localhost:8021", "http://127.0.0.1:8022", None])
def test_post_requires_exact_origin(origin):
    registry, _, payload = launch(publish=True)
    with pytest.raises(WebAuthError, match="origin_denied"):
        context(registry, payload, method="POST", origin=origin)


def test_get_present_origin_is_validated():
    registry, _, payload = launch()
    with pytest.raises(WebAuthError, match="origin_denied"):
        context(registry, payload, origin="null")


def test_token_nonce_scopes_and_post_context_are_independent():
    registry, _, payload = launch(publish=True)
    read = context(registry, payload)
    with pytest.raises(WebAuthError, match="mutation_context_required"):
        registry.authorize(read, "publish")
    mutation = context(registry, payload, method="POST")
    assert registry.authorize(mutation, "publish").run_id == payload["run_id"]
    for bad in (None, "A" * 43):
        with pytest.raises(WebAuthError):
            context(registry, payload, method="POST", csrf_nonce=bad)
    with pytest.raises(WebAuthError, match="authentication_required"):
        registry.authenticate(
            "Bearer " + "A" * 43, host="127.0.0.1:8021", origin=None, method="GET"
        )
    readonly, _, read_payload = launch()
    with pytest.raises(WebAuthError, match="scope_denied"):
        readonly.authorize(context(readonly, read_payload, method="POST"), "publish")


def test_expiry_revalidates_existing_context_without_wall_clock_authority():
    ticks = [10.0]
    registry, credential, payload = launch(lifetime=2, clock=lambda: ticks[0], wall_clock=lambda: 0)
    issued = context(registry, payload)
    assert registry.authorize(issued, "readonly").run_id == payload["run_id"]
    ticks[0] = 12.0
    for call in (
        lambda: registry.authorize(issued, "readonly"),
        lambda: context(registry, payload),
        lambda: registry._bootstrap_payload(credential),
    ):
        with pytest.raises(WebAuthError):
            call()


def test_revoke_is_effective_under_shared_lock_and_hook_runs_once():
    shared_lock = threading.RLock()
    registry, _, payload = launch(lock=shared_lock, publish=True)
    assert registry.lock is shared_lock
    observed = []
    registry._bind_revocation_hook(lambda run_id: observed.append(run_id))
    read, mutation = context(registry, payload), context(registry, payload, method="POST")
    assert registry.revoke(mutation) == payload["run_id"]
    assert observed == [payload["run_id"]]
    for call in (
        lambda: registry.authorize(read, "readonly"),
        lambda: context(registry, payload),
        lambda: registry.revoke(mutation),
    ):
        with pytest.raises(WebAuthError):
            call()
    assert len(observed) == 1


def test_failed_hook_does_not_restore_authorization():
    registry, _, payload = launch()

    def fail(_):
        raise RuntimeError("synthetic cleanup fault")

    registry._bind_revocation_hook(fail)
    with pytest.raises(WebAuthError, match="revocation_cleanup_failed"):
        registry.revoke(context(registry, payload, method="POST"))
    with pytest.raises(WebAuthError, match="authentication_required"):
        context(registry, payload)


def test_context_lifetime_does_not_retain_abandoned_keys():
    registry, _, payload = launch(max_contexts=2)
    first, second = context(registry, payload), context(registry, payload)
    with pytest.raises(WebAuthError, match="context_capacity"):
        context(registry, payload)
    del first
    gc.collect()
    replacement = context(registry, payload)
    assert (
        registry.authorize(second, "readonly").run_id
        == registry.authorize(replacement, "readonly").run_id
    )
    for _ in range(10):
        del replacement
        replacement = context(registry, payload)
    assert registry.authorize(replacement, "readonly").run_id == payload["run_id"]


def test_public_metadata_tampering_cannot_change_private_run_scope():
    registry, credential, payload = launch()
    issued = context(registry, payload, method="POST")
    metadata = registry.authorize(issued, "readonly")
    object.__setattr__(metadata, "scopes", frozenset({"readonly", "publish"}))
    object.__setattr__(credential._metadata, "scopes", frozenset({"readonly", "publish"}))
    with pytest.raises(WebAuthError, match="scope_denied"):
        registry.authorize(issued, "publish")
    with pytest.raises(WebAuthError, match="bootstrap_invalid"):
        registry._bootstrap_payload(credential)


def test_bootstrap_requires_registry_issued_credential_and_exact_secret():
    registry, credential, _ = launch()
    with pytest.raises(WebAuthError, match="bootstrap_invalid"):
        registry._bootstrap_payload(object.__new__(BootstrapCredential))

    class MutableText(str):
        pass

    object.__setattr__(credential, "_access_token", MutableText("A" * 43))
    with pytest.raises(WebAuthError, match="credential_invalid"):
        registry._bootstrap_payload(credential)


def test_secret_canaries_never_enter_repr_or_fixed_errors():
    registry, credential, payload = launch()
    issued = context(registry, payload)
    rendered = repr(credential) + repr(issued) + repr(registry)
    assert not any(value in rendered for value in (payload["access_token"], payload["csrf_nonce"]))
    with pytest.raises(WebAuthError) as failure:
        context(registry, payload, host="secret-canary.invalid")
    assert str(failure.value) == "host_denied"
    assert "secret-canary" not in repr(failure.value)


def test_close_revokes_every_run_even_when_one_hook_fails():
    registry = RunRegistry()
    credentials = [registry.create(ORIGIN) for _ in range(2)]
    payloads = [json.loads(registry._bootstrap_payload(item)) for item in credentials]
    observed = []

    def hook(run_id):
        observed.append(run_id)
        if len(observed) == 1:
            raise RuntimeError("synthetic cleanup fault")

    registry._bind_revocation_hook(hook)
    with pytest.raises(WebAuthError, match="revocation_cleanup_failed"):
        registry.close()
    assert len(observed) == 2
    for payload in payloads:
        with pytest.raises(WebAuthError):
            context(registry, payload)


@pytest.mark.parametrize(
    "options", [{"max_runs": True}, {"max_contexts": 0}, {"lock": threading.Lock()}]
)
def test_registry_bounds_and_reentrant_lock_required(options):
    with pytest.raises(WebAuthError):
        RunRegistry(**options)
