"""Synthetic-only preview tests; no provider, native store, or account access."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session
from session_migrate.preview import (
    PageLimits,
    PreviewCache,
    PreviewKey,
    PreviewLimits,
    PreviewRequest,
    PreviewService,
    PreviewStatus,
    literal_text,
    project_session,
)


def event(
    text: str = "hello",
    *,
    kind: EventKind = EventKind.MESSAGE,
    role: Role = Role.USER,
    index: int = 0,
    **kwargs: object,
) -> Event:
    return Event(kind, Provenance(index), role=role, text=text, **kwargs)


def session(*events: Event, revision: str = "revision-1") -> Session:
    return Session(
        AgentFormat.CODEX,
        Path("/synthetic/session.jsonl"),
        revision,
        "native-session",
        None,
        None,
        None,
        None,
        "synthetic",
        tuple(events),
        len(events),
    )


def key(
    *,
    root: str = "/synthetic/root",
    profile: str = "profile-a",
    source_format: AgentFormat = AgentFormat.CODEX,
) -> PreviewKey:
    return PreviewKey(source_format, profile, root, "native-session")


def test_pagination_preserves_source_order_even_when_timestamps_disagree() -> None:
    source = session(
        *(event(f"message {i}", index=i, timestamp=f"2026-10-{20 - i:02d}") for i in range(9))
    )
    snapshot = project_session(source)
    limits = PageLimits(max_events=3)
    first = snapshot.page(limits=limits)
    second = snapshot.page(first.next_offset, limits=limits)
    third = snapshot.page(second.next_offset, limits=limits)
    assert [row.source_index for page in (first, second, third) for row in page.events] == list(
        range(9)
    )
    assert third.next_offset is None
    assert snapshot.page(9).events == ()
    assert first.total_source_events == 9
    with pytest.raises(ValueError, match="offset"):
        snapshot.page(-1)


def test_empty_session_is_explicit() -> None:
    snapshot = project_session(session())
    assert snapshot.page().events == ()
    assert snapshot.page().next_offset is None
    assert snapshot.notices == ("Empty session.",)


@pytest.mark.parametrize(
    "escape",
    [
        "\x1b[31m",
        "\x1b[0m",
        "\x1b]8;;https://invalid.test/token\x1b\\",
        "\x1b]52;c;c2VjcmV0\x07",
        "\x1bPpayload\x1b\\",
        "\x9b31m",
        "\x9dsecret\x9c",
        "\x90secret\x9c",
        "\x00\x01\x07\x08\x0b\x0c\x0d\x7f\x85",
        "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069",
    ],
)
def test_dangerous_controls_are_visual_only(escape: str) -> None:
    body = f"Olá {escape}世界 👩‍💻\n\t[bold]literal[/bold] <script>literal</script>"
    source = session(event(body))
    row = project_session(source).events[0]
    assert row.text == "Olá 世界 👩‍💻\n\t[bold]literal[/bold] <script>literal</script>"
    assert source.events[0].text == body


def test_incomplete_terminal_strings_are_removed() -> None:
    assert literal_text("safe\x1b]52;private") == "safe"
    assert literal_text("safe\x1bPprivate") == "safe"
    assert literal_text("safe\x9dprivate") == "safe"
    assert literal_text("safe\x1b[31") == "safe"


@pytest.mark.parametrize(
    "block",
    ["image", "input_image", "audio", "input_audio", "video", "document", "attachment", "file"],
)
def test_media_is_metadata_without_uri_path_or_body(block: str) -> None:
    payload = {
        "block_type": block,
        "image_url": "data:image/png;base64,SECRET_BYTES",
        "url": "https://invalid.test/SECRET_TOKEN",
        "path": "/SECRET_PATH",
        "name": "SECRET_NAME",
    }
    row = project_session(session(event("SECRET_BODY", payload=payload)), expand_tools=True).events[
        0
    ]
    assert row.text == f"[Media: {block}; content not loaded]"
    assert "SECRET" not in row.text


def test_tool_bodies_are_collapsed_by_default_and_expansion_is_explicit() -> None:
    source = session(
        event(
            "private tool result",
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            tool_name="exec\x1b[31m",
        ),
        event(
            "",
            kind=EventKind.TOOL_CALL,
            tool_name="read",
            payload={"arguments": '{"path":"synthetic"}', "token": "DO_NOT_PRINT"},
        ),
    )
    folded = project_session(source)
    assert all(row.collapsed for row in folded.events)
    assert "private tool" not in folded.events[0].text
    expanded = project_session(source, expand_tools=True)
    assert all(not row.collapsed for row in expanded.events)
    assert "private tool result" in expanded.events[0].text
    assert json.loads(expanded.events[1].text.split("\n", 1)[1]) == {"path": "synthetic"}
    assert "DO_NOT_PRINT" not in expanded.events[1].text
    assert "\x1b" not in folded.events[0].text


def test_compaction_and_opaque_events_are_explicit() -> None:
    snapshot = project_session(
        session(event("summary", kind=EventKind.COMPACTION), event("", kind=EventKind.OPAQUE))
    )
    assert "Compaction" in snapshot.events[0].text
    assert "summary" in snapshot.events[0].text
    assert "no text" in snapshot.events[1].text
    assert any("cannot reconstruct" in notice for notice in snapshot.notices)


@pytest.mark.parametrize(
    "limits",
    [
        PreviewLimits(max_events=2),
        PreviewLimits(max_characters=7),
        PreviewLimits(max_bytes=7),
        PreviewLimits(event_characters=2),
        PreviewLimits(input_characters=2),
    ],
)
def test_snapshot_budgets_are_effective_and_labelled(limits: PreviewLimits) -> None:
    source = session(*(event("é🌍" * 20, index=i) for i in range(5)))
    snapshot = project_session(source, limits=limits)
    assert len(snapshot.events) <= limits.max_events
    assert sum(len(row.text) for row in snapshot.events) <= limits.max_characters
    assert snapshot.text_bytes <= limits.max_bytes
    assert all(len(row.text) <= limits.event_characters for row in snapshot.events)
    assert any(
        "Preview limited" in notice and "history is unchanged" in notice
        for notice in snapshot.notices
    )
    assert snapshot.total_source_events == 5
    assert len(source.events) == 5


def test_page_character_byte_and_event_budgets_make_progress() -> None:
    snapshot = project_session(session(*(event("🌍" * 20) for _ in range(4))))
    limits = PageLimits(max_events=2, max_characters=5, max_bytes=3)
    offset = 0
    seen = []
    while True:
        page = snapshot.page(offset, limits=limits)
        assert sum(len(row.text) for row in page.events) <= 5
        assert sum(len(row.text.encode()) for row in page.events) <= 3
        assert len(page.events) <= 2
        seen.extend(row.source_index for row in page.events)
        assert all(row.truncated for row in page.events)
        if page.next_offset is None:
            break
        assert page.next_offset > offset
        offset = page.next_offset
    assert seen == list(range(4))


@pytest.mark.parametrize("factory", [PreviewLimits, PageLimits, PreviewCache])
def test_limits_reject_invalid_values(factory: object) -> None:
    name = "max_entries" if factory is PreviewCache else "max_events"
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="positive integers"):
            factory(**{name: value})


def test_cache_identity_includes_format_profile_root_and_id() -> None:
    cache = PreviewCache(max_entries=8)
    identities = [
        key(),
        key(root="/synthetic/other"),
        key(profile="profile-b"),
        key(source_format=AgentFormat.CLAUDE),
    ]
    for i, identity in enumerate(identities):
        cache.put(identity, project_session(session(event(f"body {i}"))))
    for i, identity in enumerate(identities):
        assert cache.get(identity, "revision-1").events[0].text == f"body {i}"
    assert cache.entries == 4


def test_cache_invalidates_revision_and_separates_expansion_and_limits() -> None:
    cache = PreviewCache()
    source = session(event("tool body", kind=EventKind.TOOL_RESULT))
    folded = project_session(source)
    expanded = project_session(source, expand_tools=True)
    tiny = project_session(source, limits=PreviewLimits(event_characters=2))
    for snapshot in (folded, expanded, tiny):
        assert cache.put(key(), snapshot)
    assert cache.get(key(), "revision-1") is folded
    assert cache.get(key(), "revision-1", expand_tools=True) is expanded
    assert cache.get(key(), "revision-1", limits=tiny.limits) is tiny
    assert cache.get(key(), "revision-2") is None
    assert cache.entries == 0
    new = project_session(replace(source, source_sha256="revision-2"))
    assert cache.put(key(), new)
    assert cache.get(key(), "revision-2") is new


def test_cache_entry_byte_bounds_and_lru() -> None:
    cache = PreviewCache(max_entries=2, max_bytes=8000)
    snapshot = project_session(session(event("x" * 100)))
    first, second, third = key(root="a"), key(root="b"), key(root="c")
    assert cache.put(first, snapshot)
    assert cache.put(second, snapshot)
    assert cache.get(first, "revision-1") is snapshot
    assert cache.put(third, snapshot)
    assert cache.get(second, "revision-1") is None
    assert cache.entries == 2
    assert 0 < cache.retained_bytes <= 8000
    small = PreviewCache(max_bytes=1)
    assert not small.put(first, snapshot)
    assert small.retained_bytes == 0 and small.entries == 0
    byte_bound = PreviewCache(max_entries=100, max_bytes=cache.retained_bytes // 2 + 1)
    for identity in (first, second, third):
        byte_bound.put(identity, snapshot)
    assert byte_bound.entries == 1
    assert byte_bound.retained_bytes <= byte_bound.max_bytes
    cache.clear()
    assert cache.entries == 0 and cache.retained_bytes == 0


def test_projection_does_not_change_source_bytes_or_payloads(tmp_path: Path) -> None:
    original = b'{"text":"synthetic","control":"\\u001b[31m"}\n'
    source_path = tmp_path / "source.jsonl"
    source_path.write_bytes(original)
    payload = {"arguments": "[bold]literal[/bold]", "nested": {"data": ["original"]}}
    source = replace(
        session(event("\x1b[31m🌍", kind=EventKind.TOOL_CALL, payload=payload)),
        source_path=source_path,
    )
    before = (
        source.events[0].text,
        repr(payload),
        hashlib.sha256(source_path.read_bytes()).hexdigest(),
    )
    for expand in (True, False):
        project_session(source, expand_tools=expand, limits=PreviewLimits(max_bytes=1)).page()
    after = (
        source.events[0].text,
        repr(payload),
        hashlib.sha256(source_path.read_bytes()).hexdigest(),
    )
    assert after == before
    assert source_path.read_bytes() == original


def test_service_reads_off_caller_thread_and_uses_projected_cache() -> None:
    main_thread = threading.get_ident()
    seen = []

    def loader() -> Session:
        seen.append(threading.get_ident())
        return session(event("loaded"))

    service = PreviewService()
    request = service.request(key(), "revision-1", loader)
    result = request.wait(1)
    assert result.status == PreviewStatus.READY
    assert result.snapshot.events[0].text == "loaded"
    assert seen and seen[0] != main_thread
    cached = service.request(key(), "revision-1", lambda: pytest.fail("cache missed"))
    assert cached.poll().snapshot is result.snapshot


@pytest.mark.parametrize("terminal", [PreviewStatus.CANCELLED, PreviewStatus.TIMED_OUT])
def test_slow_loader_slots_stay_bounded_and_late_results_are_discarded(
    terminal: PreviewStatus,
) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def loader() -> Session:
        started.set()
        try:
            assert release.wait(2)
            return session(event("late body"))
        finally:
            finished.set()

    service = PreviewService(max_workers=1)
    request = service.request(key(), "revision-1", loader, timeout=0.05)
    try:
        assert started.wait(1)
        assert request.poll().status == PreviewStatus.LOADING
        if terminal == PreviewStatus.CANCELLED:
            request.cancel()
        assert request.wait(0.2).status == terminal
        for _ in range(20):
            busy = service.request(key(root="other"), "revision-1", lambda: pytest.fail("queued"))
            assert busy.poll().status == PreviewStatus.BUSY
        assert service.cache.entries == 0
    finally:
        release.set()
        assert finished.wait(1)
    assert request.wait().status == terminal
    assert request.poll().snapshot is None
    # A cancelled/deadline-expired reader cannot put a late body in the cache.
    assert service.cache.get(key(), "revision-1") is None


def test_service_errors_and_repr_do_not_leak_body_or_identity() -> None:
    def loader() -> Session:
        raise ValueError("PRIVATE_BODY /PRIVATE_ACCOUNT token=PRIVATE_TOKEN")

    service = PreviewService()
    request = service.request(key(), "revision-1", loader)
    result = request.wait(1)
    assert result.status == PreviewStatus.ERROR
    assert result.error == "Preview read failed."
    assert "PRIVATE" not in repr(result) + repr(request) + str(result.error)
    snapshot = project_session(session(event("PRIVATE_BODY")))
    assert "PRIVATE_BODY" not in repr(snapshot) + repr(snapshot.events[0])
    assert "/synthetic/root" not in repr(key())


def test_service_rejects_loader_identity_change_and_invalid_deadline() -> None:
    service = PreviewService()
    request = service.request(key(), "revision-1", lambda: replace(session(), session_id="other"))
    assert request.wait(1).status == PreviewStatus.ERROR
    assert service.cache.entries == 0
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite and positive"):
            service.request(key(), "revision-1", session, timeout=timeout)


def test_expanded_flat_arguments_are_bounded_with_explicit_nested_omission() -> None:
    arguments = {
        "command": "[bold]echo Olá\x1b[31m[/bold]",
        "count": 2,
        "active": True,
        "nested": {"private": "DO_NOT_STRINGIFY"},
        "image_url": "https://invalid.test/PRIVATE_MEDIA_TOKEN",
        "blob": "data:image/png;base64,PRIVATE_BYTES",
    }
    source = session(
        event(
            "",
            kind=EventKind.TOOL_CALL,
            tool_name="shell",
            payload={"arguments": arguments, "private": "DO_NOT_STRINGIFY"},
        )
    )
    snapshot = project_session(source, expand_tools=True)
    text = snapshot.events[0].text
    assert "echo Olá" in text
    assert "[bold]" in text and "\x1b" not in text
    assert "Nested or binary argument omitted" in text
    assert "Media content not loaded" in text
    assert "DO_NOT_STRINGIFY" not in text and "PRIVATE" not in text
    assert snapshot.events[0].truncated
    assert snapshot.notices
    bounded = project_session(
        source, expand_tools=True, limits=PreviewLimits(input_characters=8, max_bytes=5)
    )
    assert bounded.text_bytes <= 5


def test_parallel_requests_have_no_unbounded_queue_or_reader_threads() -> None:
    service = PreviewService(max_workers=2)
    barrier = threading.Barrier(9)
    release = threading.Event()
    lock = threading.Lock()
    requests = []
    entered = []

    def submit(index: int) -> None:
        def loader() -> Session:
            with lock:
                entered.append(index)
            assert release.wait(2)
            return session(event(f"selected {index}"))

        barrier.wait()
        request = service.request(key(root=f"root-{index}"), "revision-1", loader, timeout=2)
        with lock:
            requests.append(request)

    callers = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    try:
        for caller in callers:
            caller.start()
        barrier.wait()
        for caller in callers:
            caller.join(1)
            assert not caller.is_alive()
        states = [request.poll().status for request in requests]
        assert states.count(PreviewStatus.LOADING) == 2
        assert states.count(PreviewStatus.BUSY) == 6
    finally:
        release.set()
        for caller in callers:
            caller.join(1)
    assert sum(request.wait(1).status == PreviewStatus.READY for request in requests) == 2
    assert len(entered) == 2
    assert service.cache.entries == 2


@pytest.mark.parametrize(
    "carrier,encoding",
    [
        (carrier, encoding)
        for carrier in ("arguments", "input", "event_text")
        for encoding in ("dict", "json", "escaped_json")
        if (carrier, encoding) != ("event_text", "dict")
    ],
)
def test_tool_media_policy_is_equivalent_across_argument_representations(
    carrier: str,
    encoding: str,
) -> None:
    arguments = {
        "command": "echo [bold]Olá[/bold]",
        "image_url": "https://invalid.test/MEDIA_TOKEN",
        "blob": "data:image/png;base64,MEDIA_BYTES",
        "image_path": "/MEDIA_PATH",
    }
    if encoding == "dict":
        value = arguments
    else:
        value = json.dumps(arguments, ensure_ascii=False)
        if encoding == "escaped_json":
            value = (
                value.replace("image_url", r"image\u005furl")
                .replace("data:", r"d\u0061ta\u003a")
                .replace("https://", r"https:\/\/")
            )
    source = session(
        event(
            value if carrier == "event_text" else "",
            kind=EventKind.TOOL_CALL,
            payload={} if carrier == "event_text" else {carrier: value},
        )
    )
    before = (source.events[0].text, repr(source.events[0].payload))
    actual = project_session(source, expand_tools=True)
    expected = project_session(
        session(event("", kind=EventKind.TOOL_CALL, payload={"arguments": arguments})),
        expand_tools=True,
    )
    assert actual.events[0].text == expected.events[0].text
    assert "Media content not loaded" in actual.events[0].text
    assert "MEDIA_TOKEN" not in actual.events[0].text
    assert "MEDIA_BYTES" not in actual.events[0].text
    assert "MEDIA_PATH" not in actual.events[0].text
    assert (source.events[0].text, repr(source.events[0].payload)) == before


@pytest.mark.parametrize(
    "value",
    [
        '{"image_url":"https://invalid.test/MEDIA_TOKEN",',
        '{"blob":"data:image/png;base64,MEDIA_BYTES',
        "https://invalid.test/MEDIA_TOKEN /MEDIA_PATH data:image/png;base64,MEDIA_BYTES",
        '[{"image_url":"https://invalid.test/MEDIA_TOKEN"}]',
        '"data:image/png;base64,MEDIA_BYTES"',
        "42",
        "null",
    ],
)
def test_partial_streamed_and_unknown_tool_inputs_are_omitted_without_body(value: str) -> None:
    snapshot = project_session(
        session(event("", kind=EventKind.TOOL_CALL, payload={"input": value})), expand_tools=True
    )
    text = snapshot.events[0].text
    assert "Tool arguments omitted from preview" in text
    assert "MEDIA_" not in text and "invalid.test" not in text
    assert snapshot.events[0].truncated
    assert any("Preview limited" in notice for notice in snapshot.notices)


@pytest.mark.parametrize(
    "value",
    [
        '{"nested":' + "[" * 20 + '"MEDIA_BYTES"' + "]" * 20 + "}",
        '{"items":[' + ",".join('"MEDIA_TOKEN"' for _ in range(300)) + "]}",
        '{"blob":"' + "data:image/png;base64,MEDIA_BYTES" * 2000 + '"}',
    ],
)
def test_tool_json_limits_apply_before_decoder(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "session_migrate.preview.json.loads", lambda _: pytest.fail("unbounded parse")
    )
    snapshot = project_session(
        session(event("", kind=EventKind.TOOL_CALL, payload={"arguments": value})),
        expand_tools=True,
    )
    assert "Tool arguments omitted from preview" in snapshot.events[0].text
    assert "MEDIA_" not in snapshot.events[0].text
    assert snapshot.events[0].truncated


def test_nested_tool_json_is_bounded_and_matches_dict_omission_policy() -> None:
    arguments = {
        "command": 'echo "quoted" [literal] {text}',
        "nested": {"media": [{"image_url": "https://invalid.test/MEDIA_TOKEN"}]},
    }
    snapshots = [
        project_session(
            session(event("", kind=EventKind.TOOL_CALL, payload={"arguments": value})),
            expand_tools=True,
        )
        for value in (arguments, json.dumps(arguments))
    ]
    assert snapshots[0].events[0].text == snapshots[1].events[0].text
    assert "MEDIA_TOKEN" not in snapshots[1].events[0].text
    assert "Nested or binary argument omitted" in snapshots[1].events[0].text
    assert snapshots[1].events[0].truncated
    assert snapshots[1].notices


def test_media_object_json_and_dict_omit_url_file_path_and_binary() -> None:
    arguments = {
        "type": "image",
        "url": "https://invalid.test/MEDIA_TOKEN",
        "path": "/MEDIA_PATH",
        "data": "MEDIA_BYTES",
    }
    for value in (arguments, json.dumps(arguments)):
        snapshot = project_session(
            session(event("", kind=EventKind.TOOL_CALL, payload={"input": value})),
            expand_tools=True,
        )
        assert snapshot.events[0].text.endswith("[Media content not loaded]")
        assert "MEDIA_" not in snapshot.events[0].text


def test_regular_message_and_expanded_tool_result_text_remain_literal() -> None:
    text = "[bold]https://invalid.test/CONTEXT_TOKEN[/bold] data:image/png;base64,CONTEXT_BYTES"
    snapshot = project_session(
        session(event(text), event(text, kind=EventKind.TOOL_RESULT)), expand_tools=True
    )
    assert snapshot.events[0].text == text
    assert snapshot.events[1].text.endswith(text)


def _wait_reader_finished(service: PreviewService) -> None:
    """Observe release of the single reader slot, including post-resolver work."""
    assert service._slots.acquire(timeout=1)
    service._slots.release()


def test_authoritative_factory_returns_handle_while_authority_is_blocked() -> None:
    started, release = threading.Event(), threading.Event()
    caller_thread = threading.get_ident()
    resolver_threads = []

    def resolver() -> tuple[str, Session]:
        resolver_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return "actual-revision", session(event("authoritatively read"))

    service = PreviewService()
    request = service.request_authoritative(key(), resolver)
    try:
        assert started.wait(1)
        assert resolver_threads == [resolver_threads[0]]
        assert resolver_threads[0] != caller_thread
        assert request.poll().status == PreviewStatus.LOADING
        assert service.cache.entries == 0
    finally:
        release.set()
    result = request.wait(1)
    assert result.status == PreviewStatus.READY
    assert result.snapshot.source_revision == "actual-revision"
    assert result.snapshot.events[0].text == "authoritatively read"
    _wait_reader_finished(service)


@pytest.mark.parametrize("terminal", [PreviewStatus.CANCELLED, PreviewStatus.TIMED_OUT])
def test_authority_cancel_and_deadline_cover_resolver_and_never_cache_late_results(
    terminal: PreviewStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    now = [100.0]
    monkeypatch.setattr("session_migrate.preview.time.monotonic", lambda: now[0])

    def resolver() -> tuple[str, Session]:
        started.set()
        assert release.wait(2)
        return "actual-revision", session(event("late authority body"))

    service = PreviewService()
    request = service.request_authoritative(key(), resolver, timeout=10)
    try:
        assert started.wait(1)
        if terminal == PreviewStatus.CANCELLED:
            request.cancel()
        else:
            now[0] = 111.0
        assert request.poll().status == terminal
        for _ in range(10):
            busy = service.request_authoritative(
                key(root="other"), lambda: pytest.fail("resolver queued after cancellation")
            )
            assert busy.poll().status == PreviewStatus.BUSY
        assert service.cache.entries == 0
    finally:
        release.set()
        _wait_reader_finished(service)
    assert request.poll().status == terminal
    assert request.poll().snapshot is None
    assert service.cache.entries == 0


def test_authority_expiry_without_polling_or_cancel_discards_completed_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    now = [100.0]
    monkeypatch.setattr("session_migrate.preview.time.monotonic", lambda: now[0])
    service = PreviewService()

    def resolver() -> tuple[str, Session]:
        started.set()
        assert release.wait(2)
        return "late-revision", session(event("late body"))

    request = service.request_authoritative(key(), resolver, timeout=10)
    try:
        assert started.wait(1)
        now[0] = 111.0
    finally:
        release.set()
        _wait_reader_finished(service)
    assert request.poll().status == PreviewStatus.TIMED_OUT
    assert service.cache.entries == 0


def test_authoritative_cache_hit_requires_current_witness_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 64
    source = session(event("cacheable body"), revision=revision)
    snapshot = project_session(source)
    cache = PreviewCache()
    assert cache.put(key(), snapshot)
    calls = []
    original_get = cache.get

    def witnessed_get(*args: object, **kwargs: object):
        calls.append("cache")
        return original_get(*args, **kwargs)

    monkeypatch.setattr(cache, "get", witnessed_get)
    monkeypatch.setattr(
        "session_migrate.preview.project_session",
        lambda *args, **kwargs: pytest.fail("cached projection regenerated"),
    )

    def resolver() -> tuple[str, Session]:
        calls.append("authority")
        return revision, source

    service = PreviewService(cache=cache)
    result = service.request_authoritative(key(), resolver).wait(1)
    assert result.status == PreviewStatus.READY
    assert result.snapshot is snapshot
    assert calls == ["authority", "cache"]
    _wait_reader_finished(service)


def test_authoritative_witness_revision_change_invalidates_old_snapshot() -> None:
    cache = PreviewCache()
    assert cache.put(key(), project_session(session(event("old body"))))
    service = PreviewService(cache=cache)
    result = service.request_authoritative(
        key(), lambda: ("revision-2", session(event("new body"), revision="revision-2"))
    ).wait(1)
    assert result.status == PreviewStatus.READY
    assert result.snapshot.source_revision == "revision-2"
    assert result.snapshot.events[0].text == "new body"
    _wait_reader_finished(service)
    assert cache.entries == 1
    assert cache.get(key(), "revision-2") is result.snapshot


@pytest.mark.parametrize("change", ["id", "format"])
def test_authoritative_identity_is_rechecked_even_with_cached_revision(change: str) -> None:
    cache = PreviewCache()
    original = project_session(session(event("original cache")))
    assert cache.put(key(), original)
    changed = replace(
        session(),
        **({"session_id": "other"} if change == "id" else {"source_format": AgentFormat.CLAUDE}),
    )
    service = PreviewService(cache=cache)
    result = service.request_authoritative(key(), lambda: ("revision-1", changed)).wait(1)
    assert result.status == PreviewStatus.ERROR
    assert result.error == "Preview source identity changed."
    assert result.snapshot is None
    _wait_reader_finished(service)
    assert cache.get(key(), "revision-1") is original


@pytest.mark.parametrize("revision", [None, 42, "", "   ", "x" * 257])
def test_authoritative_revision_is_bounded_nonempty_before_cache_lookup(
    revision: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PreviewService()
    monkeypatch.setattr(
        service.cache, "get", lambda *args, **kwargs: pytest.fail("invalid revision")
    )
    request = service.request_authoritative(key(), lambda: (revision, session()))
    result = request.wait(1)
    assert result.status == PreviewStatus.ERROR
    assert result.error == "Preview source revision invalid."
    _wait_reader_finished(service)
    assert service.cache.entries == 0


def test_authoritative_revision_maximum_is_accepted() -> None:
    service = PreviewService()
    revision = "x" * 256
    result = service.request_authoritative(key(), lambda: (revision, session())).wait(1)
    assert result.status == PreviewStatus.READY
    assert result.snapshot.source_revision == revision
    _wait_reader_finished(service)


def test_authoritative_resolver_errors_are_content_free_even_with_warm_cache() -> None:
    cache = PreviewCache()
    assert cache.put(key(), project_session(session(event("cached body"))))
    service = PreviewService(cache=cache)

    def resolver() -> tuple[str, Session]:
        raise ValueError("PRIVATE_AUTH_BODY /PRIVATE_PATH token=PRIVATE_TOKEN")

    result = service.request_authoritative(key(), resolver).wait(1)
    assert result.status == PreviewStatus.ERROR
    assert result.error == "Preview read failed."
    assert result.snapshot is None
    assert "PRIVATE" not in repr(result) + str(result.error)
    _wait_reader_finished(service)


def test_authoritative_options_preserve_projection_and_cache_fingerprint() -> None:
    source = session(event("tool output", kind=EventKind.TOOL_RESULT))
    service = PreviewService(limits=PreviewLimits(event_characters=30))
    folded = service.request_authoritative(key(), lambda: ("revision-1", source)).wait(1)
    _wait_reader_finished(service)
    expanded = service.request_authoritative(
        key(), lambda: ("revision-1", source), expand_tools=True
    ).wait(1)
    _wait_reader_finished(service)
    assert folded.snapshot.events[0].collapsed
    assert not expanded.snapshot.events[0].collapsed
    assert expanded.snapshot.events[0].truncated
    assert expanded.snapshot.notices
    assert service.cache.entries == 2


def test_authoritative_timeout_rejects_invalid_values_before_resolver() -> None:
    service = PreviewService()
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite and positive"):
            service.request_authoritative(
                key(), lambda: pytest.fail("invalid timeout"), timeout=timeout
            )


@pytest.mark.parametrize("terminal", [PreviewStatus.CANCELLED, PreviewStatus.TIMED_OUT])
def test_cancelled_or_expired_authority_never_looks_up_or_invalidates_warm_cache(
    terminal: PreviewStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    now = [100.0]
    monkeypatch.setattr("session_migrate.preview.time.monotonic", lambda: now[0])
    cache = PreviewCache()
    original = project_session(session(event("warm cache body")))
    assert cache.put(key(), original)
    original_get = cache.get
    monkeypatch.setattr(cache, "get", lambda *args, **kwargs: pytest.fail("late lookup"))
    service = PreviewService(cache=cache)

    def resolver() -> tuple[str, Session]:
        started.set()
        assert release.wait(2)
        return "revision-2", session(event("late fresh body"), revision="revision-2")

    request = service.request_authoritative(key(), resolver, timeout=10)
    try:
        assert started.wait(1)
        if terminal == PreviewStatus.CANCELLED:
            request.cancel()
        else:
            now[0] = 111.0
    finally:
        release.set()
        _wait_reader_finished(service)
    assert request.poll().status == terminal
    assert request.poll().snapshot is None
    assert cache.entries == 1
    assert original_get(key(), "revision-1") is original


def test_cancel_between_authority_check_and_cache_lookup_cannot_invalidate_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked, release = threading.Event(), threading.Event()
    caller_thread = threading.get_ident()
    original_poll = PreviewRequest.poll
    worker_checks = []

    def paused_post_authority_poll(request: PreviewRequest):
        result = original_poll(request)
        if threading.get_ident() != caller_thread and result.status == PreviewStatus.LOADING:
            worker_checks.append(result.status)
            if len(worker_checks) == 2:
                checked.set()
                assert release.wait(2)
        return result

    monkeypatch.setattr(PreviewRequest, "poll", paused_post_authority_poll)
    cache = PreviewCache()
    original = project_session(session(event("unchanged warm cache")))
    assert cache.put(key(), original)
    original_get = cache.get
    lookups = []

    def observed_lookup(*args: object, **kwargs: object):
        lookups.append("cache queried")
        return original_get(*args, **kwargs)

    monkeypatch.setattr(cache, "get", observed_lookup)
    service = PreviewService(cache=cache)
    request = service.request_authoritative(
        key(), lambda: ("revision-2", session(event("new body"), revision="revision-2"))
    )
    try:
        assert checked.wait(1)
        request.cancel()
        assert original_poll(request).status == PreviewStatus.CANCELLED
    finally:
        release.set()
        _wait_reader_finished(service)
    assert original_poll(request).status == PreviewStatus.CANCELLED
    assert lookups == []
    assert cache.entries == 1
    assert original_get(key(), "revision-1") is original
