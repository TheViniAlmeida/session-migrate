"""F5A B1: pre-extraction byte oracles and strict USER-only native emission."""

import json
import uuid
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from itertools import count
from pathlib import Path

import pytest

from session_migrate import conversion
from session_migrate.conversion import (
    MAX_PORTABLE_USER_BYTES,
    MAX_PORTABLE_USER_EVENTS,
    EmitContext,
    emit_portable,
    load_session,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, codex, opencode, opencode_v2
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, TargetFormat
from session_migrate.native_snapshots import NativeSnapshot

FIXTURES = Path(__file__).parent / "fixtures"
TIMESTAMP = "2026-10-10T12:00:00Z"
SESSION_ID = "12345678-1234-4234-8234-123456789abc"
OPENCODE_ID = "ses_12345678123442348234123456789abc"
CWD = Path("/synthetic/f5a/project")
VERSIONS = (
    (TargetFormat.CLAUDE, "2.1.209"),
    (TargetFormat.CODEX, "0.144.4"),
    (TargetFormat.OPENCODE, "2.0.22"),
    (TargetFormat.OPENCODE, "2.0.23"),
)
GOLDEN_FULL = {
    "claude-2.1.209/basic.jsonl|claude|2.1.209": {
        "dropped": {"compaction": 1},
        "sha256": "7d3e7ac6b5944718845eca780e2ed94c7fbc7102c53810ee1895ff047e68cbbb",
    },
    "claude-2.1.209/basic.jsonl|codex|0.144.4": {
        "dropped": {"session:title": 1},
        "sha256": "36ab66297449e455e9bed6e7b965682fac53919b5ae20b46e3d84581f365b7bf",
    },
    "claude-2.1.209/basic.jsonl|opencode|2.0.22": {
        "dropped": {},
        "sha256": "d1909b468a9b9975109d16ea1496e0380d1ee9c6dddc44d00bdf52c824c3159e",
    },
    "claude-2.1.209/basic.jsonl|opencode|2.0.23": {
        "dropped": {},
        "sha256": "d1909b468a9b9975109d16ea1496e0380d1ee9c6dddc44d00bdf52c824c3159e",
    },
    "codex-0.144.4/basic.jsonl|claude|2.1.209": {
        "dropped": {"compaction": 1},
        "sha256": "ea553ac33119fa8970eb7d23185bf2c3501666c3c08c8a4f1aae49fc132c3952",
    },
    "codex-0.144.4/basic.jsonl|codex|0.144.4": {
        "dropped": {"session:title": 1},
        "sha256": "6dcd136032e4d3f3fc9d8ba380946bd36be2a1842d9e6595cc5f28c6cdd8fef6",
    },
    "codex-0.144.4/basic.jsonl|opencode|2.0.22": {
        "dropped": {},
        "sha256": "0d939d6b42989a7c607976a7412902bbf345f4f1bca266eeeb526142aa6d19c2",
    },
    "codex-0.144.4/basic.jsonl|opencode|2.0.23": {
        "dropped": {},
        "sha256": "0d939d6b42989a7c607976a7412902bbf345f4f1bca266eeeb526142aa6d19c2",
    },
    "opencode-source-1.17.20/comprehensive.json|claude|2.1.209": {
        "dropped": {
            "compaction": 1,
            "message:privileged_role": 1,
            "opaque:opencode_file_source_metadata": 1,
            "opaque:opencode_ignored_text": 1,
            "opaque:opencode_nonportable_file": 1,
            "opaque:opencode_parent_session": 1,
            "opaque:opencode_patch_part": 1,
            "opaque:opencode_session_metadata": 1,
            "opaque:opencode_session_summary": 1,
            "opaque:opencode_snapshot_part": 1,
            "opaque:opencode_step-finish_part": 1,
            "opaque:opencode_step-start_part": 1,
            "opaque:opencode_tool_metadata": 1,
            "opaque:opencode_tool_result_compacted": 1,
            "opaque:opencode_user_output_format": 1,
            "opaque:opencode_user_summary_metadata": 1,
            "opaque:opencode_user_tool_policy": 1,
            "thinking": 1,
        },
        "sha256": "f6353b01ce1a5d2a6849eb37aa984fb9a45610373e907335a60ecf5c63a75b7f",
    },
    "opencode-source-1.17.20/comprehensive.json|codex|0.144.4": {
        "dropped": {
            "compaction:boundary_metadata": 1,
            "message:privileged_role": 1,
            "opaque:opencode_file_source_metadata": 1,
            "opaque:opencode_ignored_text": 1,
            "opaque:opencode_nonportable_file": 1,
            "opaque:opencode_parent_session": 1,
            "opaque:opencode_patch_part": 1,
            "opaque:opencode_session_metadata": 1,
            "opaque:opencode_session_summary": 1,
            "opaque:opencode_snapshot_part": 1,
            "opaque:opencode_step-finish_part": 1,
            "opaque:opencode_step-start_part": 1,
            "opaque:opencode_tool_metadata": 1,
            "opaque:opencode_tool_result_compacted": 1,
            "opaque:opencode_user_output_format": 1,
            "opaque:opencode_user_summary_metadata": 1,
            "opaque:opencode_user_tool_policy": 1,
            "session:title": 1,
            "thinking": 1,
        },
        "sha256": "afff3765c6ca31269723569343b5b1b9391a096f7ab79794c42ddb62c32e0522",
    },
    "opencode-source-1.17.20/comprehensive.json|opencode|2.0.22": {
        "dropped": {
            "compaction:boundary_metadata": 1,
            "message:privileged_role": 1,
            "opaque:opencode_file_source_metadata": 1,
            "opaque:opencode_ignored_text": 1,
            "opaque:opencode_nonportable_file": 1,
            "opaque:opencode_parent_session": 1,
            "opaque:opencode_patch_part": 1,
            "opaque:opencode_session_metadata": 1,
            "opaque:opencode_session_summary": 1,
            "opaque:opencode_snapshot_part": 1,
            "opaque:opencode_step-finish_part": 1,
            "opaque:opencode_step-start_part": 1,
            "opaque:opencode_tool_metadata": 1,
            "opaque:opencode_tool_result_compacted": 1,
            "opaque:opencode_user_output_format": 1,
            "opaque:opencode_user_summary_metadata": 1,
            "opaque:opencode_user_tool_policy": 1,
            "thinking": 1,
        },
        "sha256": "4c1afd5f83a7558b8246aa6b9d11b1bad077ae7084b451e56847c6640cdfd146",
    },
    "opencode-source-1.17.20/comprehensive.json|opencode|2.0.23": {
        "dropped": {
            "compaction:boundary_metadata": 1,
            "message:privileged_role": 1,
            "opaque:opencode_file_source_metadata": 1,
            "opaque:opencode_ignored_text": 1,
            "opaque:opencode_nonportable_file": 1,
            "opaque:opencode_parent_session": 1,
            "opaque:opencode_patch_part": 1,
            "opaque:opencode_session_metadata": 1,
            "opaque:opencode_session_summary": 1,
            "opaque:opencode_snapshot_part": 1,
            "opaque:opencode_step-finish_part": 1,
            "opaque:opencode_step-start_part": 1,
            "opaque:opencode_tool_metadata": 1,
            "opaque:opencode_tool_result_compacted": 1,
            "opaque:opencode_user_output_format": 1,
            "opaque:opencode_user_summary_metadata": 1,
            "opaque:opencode_user_tool_policy": 1,
            "thinking": 1,
        },
        "sha256": "4c1afd5f83a7558b8246aa6b9d11b1bad077ae7084b451e56847c6640cdfd146",
    },
}


def user_event(text="User-provided handoff; historical transcript omitted\n\nSynthetic data"):
    return Event(
        EventKind.MESSAGE,
        Provenance(0, "user_handoff"),
        role=Role.USER,
        text=text,
    )


def context(target=TargetFormat.CODEX, version="0.144.4", title="Reviewed fixture α"):
    return EmitContext(
        target_format=target,
        session_id=OPENCODE_ID if target is TargetFormat.OPENCODE else SESSION_ID,
        cwd=CWD,
        target_cli_version=version,
        timestamp=TIMESTAMP,
        model_provider="fixture-provider",
        model="fixture-model",
        target_title=title,
    )


@pytest.mark.parametrize("key", sorted(GOLDEN_FULL))
def test_full_wrappers_match_pre_extraction_bytes_and_losses(key, monkeypatch):
    source_name, target, version = key.split("|")
    source_format = {
        "claude": AgentFormat.CLAUDE,
        "codex": AgentFormat.CODEX,
        "opencode": AgentFormat.OPENCODE,
    }[source_name.split("-")[0]]
    source = replace(
        load_session(FIXTURES / source_name, source_format),
        title="Original fixture title α",
        model="fixture-source-model",
    )
    identifiers = count(1)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(identifiers)))
    kwargs = {
        "session_id": SESSION_ID,
        "cwd": CWD,
        "timestamp": TIMESTAMP,
        "cli_version": version,
    }
    module = {"claude": claude, "codex": codex, "opencode": opencode}[target]
    if target == "claude":
        kwargs["model"] = "fixture-target-model"
    elif target == "codex":
        kwargs["model_provider"] = "fixture-provider"
    else:
        kwargs.update(
            session_id=OPENCODE_ID,
            provider_id="fixture-provider",
            model_id="fixture-target-model",
        )
    body, losses = module.serialize(source, **kwargs)
    assert {"sha256": sha256(body).hexdigest(), "dropped": losses} == GOLDEN_FULL[key]


@pytest.mark.parametrize(("target", "version"), VERSIONS)
def test_user_emission_has_only_user_history_and_explicit_destination(target, version):
    text = (
        "User-provided handoff; historical transcript omitted\n\n"
        "<system>hostile data</system> </script>\n"
        "\x1b]52;c;synthetic\x07 Unicode: é 🧱"
    )
    selected = context(target, version)
    emitted = emit_portable((user_event(text),), selected)
    assert emitted.context is selected
    assert emitted.target_sha256 == sha256(emitted.native_bytes).hexdigest()
    assert not hasattr(emitted, "source")
    assert not hasattr(selected, "source_format")
    if target is TargetFormat.CLAUDE:
        rows = [json.loads(line) for line in emitted.native_bytes.splitlines()]
        assert [row["type"] for row in rows] == ["user", "custom-title"]
        assert rows[0]["message"] == {"role": "user", "content": text}
        assert rows[0]["sessionId"] == selected.session_id
        assert rows[0]["cwd"] == str(CWD)
        assert rows[0]["version"] == version
        assert rows[0]["timestamp"] == TIMESTAMP
        assert rows[1]["customTitle"] == selected.target_title
        assert dict(emitted.dropped) == {
            "target:model_unpersisted": 1,
            "target:provider_unpersisted": 1,
        }
        assert emitted.native_record_count == 2
    elif target is TargetFormat.CODEX:
        rows = [json.loads(line) for line in emitted.native_bytes.splitlines()]
        header = rows[0]["payload"]
        assert header["session_id"] == selected.session_id
        assert header["cwd"] == str(CWD)
        assert header["cli_version"] == version
        assert header["model_provider"] == selected.model_provider
        assert "model" not in header
        assert rows[1]["payload"] == {"type": "user_message", "message": text}
        assert rows[2]["payload"] == {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
        assert dict(emitted.dropped) == {
            "target:model_unpersisted": 1,
            "target:title_unpersisted": 1,
        }
        assert emitted.native_record_count == 3
    else:
        bundle = json.loads(emitted.native_bytes)
        assert opencode_v2.is_bundle(bundle)
        assert bundle["info"]["id"] == selected.session_id
        assert bundle["info"]["location"] == {"directory": str(CWD)}
        assert bundle["info"]["model"] == {
            "id": selected.model,
            "providerID": selected.model_provider,
        }
        assert bundle["info"]["title"] == selected.target_title
        assert "version" not in bundle["info"]
        assert [message["type"] for message in bundle["messages"]] == ["user"]
        assert bundle["messages"][0]["text"] == text
        assert all("info" not in m and "parts" not in m for m in bundle["messages"])
        assert dict(emitted.dropped) == {"target:cli_version_unpersisted": 1}
        assert emitted.native_record_count == 2


@pytest.mark.parametrize(("target", "version"), VERSIONS)
def test_emission_never_calls_source_session_or_native_snapshot_wrappers(
    target, version, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Source construction/loading/full wrapper must not be called")

    monkeypatch.setattr(conversion, "Session", forbidden)
    monkeypatch.setattr(conversion, "load_session", forbidden)
    monkeypatch.setattr(NativeSnapshot, "from_bytes", classmethod(forbidden))
    monkeypatch.setattr(claude, "serialize", forbidden)
    monkeypatch.setattr(codex, "serialize", forbidden)
    monkeypatch.setattr(opencode, "serialize", forbidden)
    emitted = emit_portable((user_event(),), context(target, version))
    assert emitted.native_bytes


def test_event_input_payload_is_not_retained_or_mutated():
    event = user_event()
    payload = {"content_blocks": [{"type": "text", "text": event.text}]}
    selected = replace(event, payload=payload)
    emitted = emit_portable((selected,), context())
    payload["content_blocks"][0]["text"] = "Changed after emission"
    rows = [json.loads(line) for line in emitted.native_bytes.splitlines()]
    assert rows[1]["payload"]["message"] == event.text
    assert b"Changed after emission" not in emitted.native_bytes


def test_emission_output_context_and_losses_are_frozen_and_body_hidden():
    emitted = emit_portable((user_event(),), context())
    with pytest.raises(FrozenInstanceError):
        emitted.context.model = "Changed"
    with pytest.raises(FrozenInstanceError):
        emitted.native_record_count = 1
    with pytest.raises(TypeError):
        emitted.dropped["arbitrary"] = 1
    assert type(emitted.losses) is tuple
    assert "Synthetic data" not in repr(emitted)
    assert "Reviewed fixture" not in repr(emitted.context)


@pytest.mark.parametrize(
    "changes",
    [
        {"role": Role.ASSISTANT},
        {"role": Role.SYSTEM},
        {"role": Role.TOOL},
        {"role": "user"},
        {"role": None},
        {"kind": EventKind.TOOL_CALL},
        {"kind": EventKind.TOOL_RESULT},
        {"kind": EventKind.THINKING},
        {"kind": EventKind.COMPACTION},
        {"kind": EventKind.CONTEXT},
        {"kind": EventKind.OPAQUE},
        {"kind": "message"},
        {"timestamp": TIMESTAMP},
        {"tool_name": "Read"},
        {"tool_call_id": "call_fixture"},
        {"text": ""},
        {"text": "   "},
        {"text": None},
        {"text": "\ud800"},
        {"payload": {"ui_only_projection": True}},
        {"payload": {"input": {"command": "synthetic"}}},
        {"payload": {"content_blocks": [{"type": "tool_use", "id": "fixture"}]}},
        {"payload": {"content_blocks": [{"type": "text", "text": "different"}]}},
        {"payload": []},
        {"provenance": Provenance(0, "source-native", source_id="source-id")},
        {"provenance": Provenance(0, "user_handoff", block_index=0)},
        {"provenance": Provenance(True, "user_handoff")},
        {"provenance": Provenance(1, "user_handoff")},
    ],
)
def test_user_emitter_rejects_unsupported_timeline_without_echoing_text(changes):
    with pytest.raises(SessionMigrateError) as error:
        emit_portable((replace(user_event(), **changes),), context())
    assert "Synthetic data" not in str(error.value)
    assert "call_fixture" not in str(error.value)


@pytest.mark.parametrize("events", [None, [], (), [user_event()], (user_event(),) * 1025])
def test_user_emitter_requires_bounded_immutable_input(events):
    with pytest.raises(SessionMigrateError):
        emit_portable(events, context())


@pytest.mark.parametrize(
    "changes",
    [
        {"target_format": TargetFormat.PI},
        {"target_format": "codex"},
        {"target_cli_version": "0.153.4"},
        {"target_cli_version": True},
        {"session_id": "invalid-sensitive-id"},
        {"session_id": True},
        {"cwd": Path("relative")},
        {"cwd": Path("/synthetic/../outside")},
        {"cwd": "/synthetic/path"},
        {"timestamp": "2026-10-10"},
        {"timestamp": "2026-10-10T12:00:00"},
        {"model": ""},
        {"model": True},
        {"model": "model\ncontrol"},
        {"model": "\ud800"},
        {"model_provider": ""},
        {"model_provider": "provider\x7f"},
        {"model_provider": "x" * 257},
        {"target_title": ""},
        {"target_title": "bad\n"},
        {"target_title": "é" * 513},
    ],
)
def test_explicit_context_validation_does_not_echo_invalid_identifiers(changes):
    with pytest.raises(SessionMigrateError) as error:
        replace(context(), **changes)
    assert "invalid-sensitive-id" not in str(error.value)


def test_target_title_rejects_mutable_str_subclass_with_custom_encoding():
    class MutableTitle(str):
        def encode(self, *args, **kwargs):
            return b"x"

    title = MutableTitle("x" * 1025)
    title.custom_state = "Mutable title state"
    with pytest.raises(SessionMigrateError):
        context(TargetFormat.CLAUDE, "2.1.209", title=title)


def test_opencode_target_accepts_only_exact_pins_and_canonical_native_id():
    with pytest.raises(SessionMigrateError):
        context(TargetFormat.OPENCODE, "2.0.24")
    with pytest.raises(SessionMigrateError):
        replace(context(TargetFormat.OPENCODE, "2.0.23"), session_id="ses_fixture")
    with pytest.raises(SessionMigrateError):
        replace(context(TargetFormat.OPENCODE, "2.0.23"), session_id=SESSION_ID)


def test_context_is_revalidated_before_emission():
    selected = context()
    object.__setattr__(selected, "target_cli_version", "0.999.0")
    with pytest.raises(SessionMigrateError):
        emit_portable((user_event(),), selected)
    with pytest.raises(SessionMigrateError):
        emit_portable((user_event(),), {"target_format": "codex"})


def test_utf8_limit_is_exact_and_never_silently_truncated():
    event = user_event("é" * (MAX_PORTABLE_USER_BYTES // 2))
    emitted = emit_portable((event,), context())
    assert event.text in emitted.native_bytes.decode()
    with pytest.raises(SessionMigrateError):
        emit_portable((user_event("x" * (MAX_PORTABLE_USER_BYTES + 1)),), context())


def test_maximum_accepted_document_plus_handoff_label_is_supported():
    text = "User-provided handoff; historical transcript omitted\n\n" + "x" * (1024 * 1024)
    emitted = emit_portable((user_event(text),), context())
    rows = [json.loads(line) for line in emitted.native_bytes.splitlines()]
    assert rows[2]["payload"]["content"][0]["text"] == text


def test_multiple_user_records_keep_order_and_byte_limit_is_aggregate():
    events = (
        user_event("first"),
        replace(user_event("second"), provenance=Provenance(1, "user_handoff")),
    )
    emitted = emit_portable(events, context())
    items = [
        json.loads(line)["payload"]
        for line in emitted.native_bytes.splitlines()
        if json.loads(line)["type"] == "response_item"
    ]
    assert [item["content"][0]["text"] for item in items] == ["first", "second"]
    half = "x" * (MAX_PORTABLE_USER_BYTES // 2 + 1)
    events = (user_event(half), replace(user_event(half), provenance=Provenance(1, "user_handoff")))
    with pytest.raises(SessionMigrateError):
        emit_portable(events, context())
    assert MAX_PORTABLE_USER_EVENTS == 1024


@pytest.mark.parametrize(("target", "version"), VERSIONS)
def test_unrequested_title_has_no_false_native_title_claim(target, version):
    emitted = emit_portable((user_event(),), context(target, version, title=None))
    if target is TargetFormat.OPENCODE:
        assert json.loads(emitted.native_bytes)["info"]["title"] == "Imported session"
        assert emitted.dropped["target:title_synthesized"] == 1
    else:
        assert "target:title_unpersisted" not in emitted.dropped
        if target is TargetFormat.CLAUDE:
            assert emitted.native_record_count == 1
