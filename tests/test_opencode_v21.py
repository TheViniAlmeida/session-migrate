"""OpenCode 2.1 public transfers, pinned to the v2.1.1 schema contract."""

import json
from pathlib import Path

import pytest

from session_migrate.conversion import load_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode, opencode_v2
from session_migrate.model import AgentFormat, EventKind

FIXTURE = Path(__file__).parent / "fixtures/codex-0.144.4/basic.jsonl"


def bundle(tmp_path, release="2.1.1"):
    session = load_session(FIXTURE, AgentFormat.CODEX)
    data, _ = opencode.serialize(
        session, session_id="ses_fixture", cwd=tmp_path, cli_version=release
    )
    return json.loads(data)


def parse(tmp_path, value):
    path = tmp_path / "opencode-export.json"
    path.write_text(json.dumps(value))
    opencode.validate_native_bytes(path.read_bytes(), value["info"]["id"])
    return opencode.parse_import(path)


def compaction(value):
    return next(m for m in value["messages"] if m["type"] == "compaction")


@pytest.mark.parametrize("release", ["2.1.1", "opencode v2.1.1", "2.1.2-custom"])
def test_v21_schema_selected_and_version_pin(release, tmp_path):
    value = bundle(tmp_path, release)
    assert "location" in value["info"]
    assert "recent" not in compaction(value)
    assert "providerContext" not in compaction(value)
    assert opencode_v2.validated_version(release) == "2.1.1"
    parse(tmp_path, value)


def test_v20_compaction_still_writes_required_recent(tmp_path):
    value = bundle(tmp_path, "2.0.23")
    assert compaction(value)["recent"] == ""
    assert opencode_v2.validated_version("2.0.24") == "2.0.23"
    parse(tmp_path, value)


@pytest.mark.parametrize("location", ["assistant", "text", "reasoning", "tool", "compaction"])
@pytest.mark.parametrize("malformed", [None, [], "invalid"])
def test_native_must_be_object_before_portable_projection(tmp_path, location, malformed):
    value = bundle(tmp_path)
    assistant = next(m for m in value["messages"] if m["type"] == "assistant")
    if location in {"text", "reasoning"}:
        target = {"type": location, "text": "Readable fixture"}
        assistant["content"].append(target)
    elif location == "tool":
        target = next(
            c
            for m in value["messages"]
            if m["type"] == "assistant"
            for c in m["content"]
            if c["type"] == "tool"
        )
    else:
        target = compaction(value) if location == "compaction" else assistant
    target["native"] = malformed
    with pytest.raises(SessionMigrateError, match="native object"):
        parse(tmp_path, value)


def test_native_is_reported_without_copying_private_values(tmp_path):
    value = bundle(tmp_path)
    assistant = next(m for m in value["messages"] if m["type"] == "assistant")
    assistant["native"] = {"provider_fixture": "PRIVATE_REPLAY_MARKER"}
    assistant["content"][0]["native"] = {"signature": "PRIVATE_REPLAY_MARKER"}
    parsed = parse(tmp_path, value)
    assert ("opencode_v2_native", 1) in parsed.losses
    assert ("opencode_v2_content_native", 1) in parsed.losses
    assert not any("unknown" in key for key, _ in parsed.losses)
    assert "PRIVATE_REPLAY_MARKER" not in repr(parsed.events)


@pytest.mark.parametrize("legacy", [False, True])
def test_stored_recent_text_survives_portable_compaction(tmp_path, legacy):
    value = bundle(tmp_path)
    checkpoint = compaction(value)
    if legacy:
        checkpoint["recent"] = "LEGACY_TAIL_MARKER"
    else:
        checkpoint["metadata"] = {"recent": "METADATA_TAIL_MARKER"}
    parsed = parse(tmp_path, value)
    summary = next(e for e in parsed.events if e.kind == EventKind.COMPACTION)
    assert ("LEGACY_TAIL_MARKER" if legacy else "METADATA_TAIL_MARKER") in summary.text


def test_keep_id_rebuilds_only_kept_tail_including_tools(tmp_path):
    value = bundle(tmp_path)
    checkpoint = compaction(value)
    messages = value["messages"]
    tool_message = next(
        m
        for m in messages
        if m["type"] == "assistant" and any(c["type"] == "tool" for c in m["content"])
    )
    tool = next(c for c in tool_message["content"] if c["type"] == "tool")
    tool["native"] = {"opaque": "DO_NOT_RENDER_NATIVE"}
    tool["state"]["content"] = [{"type": "text", "text": "LONG_TOOL_TAIL" * 150}]
    checkpoint["metadata"] = {"keepID": tool_message["id"], "recent": "STALE_RECENT"}
    parsed = parse(tmp_path, value)
    summary = next(e for e in parsed.events if e.kind == EventKind.COMPACTION)
    assert "[Assistant tool call]: shell(" in summary.text
    assert "LONG_TOOL_TAIL" * 150 in summary.text
    assert "STALE_RECENT" not in summary.text
    assert "DO_NOT_RENDER_NATIVE" not in summary.text
    assert "Continue after the synthetic compaction" not in summary.text
    assert "Remember synthetic migrator nonce" not in summary.text


def test_keep_id_user_tail_survives_a_second_native_transfer(tmp_path):
    value = bundle(tmp_path)
    user = next(m for m in value["messages"] if m["type"] == "user")
    user["text"] = "KEPT_USER_TAIL"
    compaction(value)["metadata"] = {"keepID": user["id"]}
    parse(tmp_path, value)
    session = opencode.parse_session(tmp_path / "opencode-export.json")
    data, _ = opencode.serialize(
        session, session_id="ses_roundtrip", cwd=tmp_path, cli_version="2.1.1"
    )
    written = json.loads(data)
    checkpoint = compaction(written)
    assert "[User]: KEPT_USER_TAIL" in checkpoint["summary"]
    assert "[Attached image/png: inline attachment]" in checkpoint["summary"]
    assert "recent" not in checkpoint
    parsed = parse(tmp_path, written)
    summary = next(e for e in parsed.events if e.kind == EventKind.COMPACTION)
    assert "KEPT_USER_TAIL" in summary.text


@pytest.mark.parametrize("keep_id", ["msg_missing", "msg_future", 42, None])
def test_unresolved_keep_id_preserves_history_and_readable_summary(tmp_path, keep_id):
    value = bundle(tmp_path)
    checkpoint = compaction(value)
    checkpoint["metadata"] = {"keepID": keep_id}
    value["messages"][-1]["id"] = "msg_future"
    parsed = parse(tmp_path, value)
    assert not any(e.kind == EventKind.COMPACTION for e in parsed.events)
    assert any(checkpoint["summary"] in (e.text or "") for e in parsed.events)
    assert any("Remember synthetic migrator nonce" in (e.text or "") for e in parsed.events)
    assert ("opencode_v2_compaction_unresolved_keep_id", 1) in parsed.losses


@pytest.mark.parametrize("window", [[], None, {"opaque": "PRIVATE_WINDOW"}])
@pytest.mark.parametrize("summary", ["", "Unused provider summary"])
def test_native_window_never_retires_portable_original_history(tmp_path, window, summary):
    value = bundle(tmp_path)
    checkpoint = compaction(value)
    checkpoint.update(summary=summary, native={"messages": window})
    parsed = parse(tmp_path, value)
    assert not any(e.kind == EventKind.COMPACTION for e in parsed.events)
    assert any("Remember synthetic migrator nonce" in (e.text or "") for e in parsed.events)
    assert ("opencode_v2_compaction_native_window", 1) in parsed.losses
    assert "PRIVATE_WINDOW" not in repr(parsed.events)


def test_native_without_window_does_not_disable_summary(tmp_path):
    value = bundle(tmp_path)
    compaction(value)["native"] = {"provider_fixture": True}
    parsed = parse(tmp_path, value)
    assert any(e.kind == EventKind.COMPACTION for e in parsed.events)


@pytest.mark.parametrize("recent", [None, [], 42])
def test_optional_legacy_recent_must_still_be_string(tmp_path, recent):
    value = bundle(tmp_path)
    compaction(value)["recent"] = recent
    with pytest.raises(SessionMigrateError):
        parse(tmp_path, value)
