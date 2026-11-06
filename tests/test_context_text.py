"""Typed historical text stays synthetic and cannot become a user instruction."""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.audit_adapters import SEMANTIC, STRUCTURE, UNKNOWN
from session_migrate.canonical import CanonicalPolicy, hash_events
from session_migrate.formats import opencode
from session_migrate.formats.context_text import (
    SYNTHETIC_TEXT_BLOCK_TYPE,
    is_marked_synthetic_text,
    is_synthetic_text_context,
    synthetic_text_metadata,
)
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role
from session_migrate.native_audit import AuditPolicy, audit_snapshot, canonicalize_snapshot
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext

STAMP = "2026-11-05T12:00:00Z"


def context(text="historical context", index=1):
    return Event(
        kind=EventKind.CONTEXT,
        role=Role.USER,
        text=text,
        timestamp=STAMP,
        payload={"block_type": SYNTHETIC_TEXT_BLOCK_TYPE},
        provenance=Provenance(index, "user", source_id=f"context-{index}"),
    )


def message(text="request", role=Role.USER, index=0):
    return Event(
        kind=EventKind.MESSAGE,
        role=role,
        text=text,
        timestamp=STAMP,
        provenance=Provenance(index, role.value),
    )


def serialize(events, release="2.2.5", *, legacy=False):
    return opencode.serialize_events(
        events,
        session_id="ses_" + "1" * 32,
        cwd=Path("/synthetic/context-test"),
        cli_version=release,
        provider_id="fixture",
        model_id="fixture",
        timestamp=STAMP,
        legacy_schema=legacy,
    )


def observe(value, release="2.2.5"):
    data = json.dumps(value, ensure_ascii=False).encode()
    snapshot = NativeSnapshot.from_bytes(
        data,
        context=SourceCaptureContext(AgentFormat.OPENCODE, Path("opencode:synthetic")),
    )
    audit = audit_snapshot(snapshot, AuditPolicy(client_version=release))
    observation = canonicalize_snapshot(snapshot, audit, CanonicalPolicy())
    return snapshot, audit, observation


@pytest.mark.parametrize("text", ["exact body", "  before\n\nUnicode é λ 🦊\r\nafter  ", ""])
def test_exact_225_roundtrip_preserves_body_kind_and_native_marker(text):
    original = context(text)
    data, dropped = serialize([original])
    assert not dropped
    native = json.loads(data)
    record = native["messages"][0]
    assert set(record) == {"id", "time", "type", "text", "metadata"}
    assert record["type"] == "synthetic"
    assert record["text"] == text
    assert record["metadata"] == {
        "session_migrate_context": {"kind": "synthetic_text", "version": 1}
    }
    opencode.validate_native_bytes(data, native["info"]["id"])
    source = opencode.parse_session_bytes(data, Path("opencode:synthetic"))
    assert source.source_sha256 == hashlib.sha256(data).hexdigest()
    assert source.raw_record_count == 2
    assert len(source.events) == 1
    recovered = source.events[0]
    assert recovered.kind is EventKind.CONTEXT
    assert recovered.role is Role.USER
    assert recovered.text == text
    assert recovered.payload == {"block_type": SYNTHETIC_TEXT_BLOCK_TYPE}
    assert recovered.provenance == Provenance(0, "synthetic", source_id=record["id"])
    assert (
        hash_events([original], CanonicalPolicy()).prefix_witness
        == hash_events([recovered], CanonicalPolicy()).prefix_witness
    )
    _, audit, observation = observe(native)
    assert audit.coverage is Coverage.VERIFIED
    assert not audit.issue_counts
    atoms = {atom.path: atom.category for atom in observation.atoms}
    assert atoms[("messages", 0, "text")] == SEMANTIC
    assert atoms[("messages", 0, "metadata", "session_migrate_context", "kind")] == STRUCTURE
    assert atoms[("messages", 0, "metadata", "session_migrate_context", "version")] == STRUCTURE


def test_context_has_own_native_message_between_user_assistant_and_tools():
    events = [
        message(index=0),
        context("first", index=0),
        context("second", index=0),
        message("answer", Role.ASSISTANT, index=1),
        context("before tool", index=2),
        Event(
            EventKind.TOOL_CALL,
            Provenance(3, "assistant"),
            Role.ASSISTANT,
            timestamp=STAMP,
            tool_name="fixture",
            tool_call_id="fixture-call",
            payload={"input": {}},
        ),
        Event(
            EventKind.TOOL_RESULT,
            Provenance(4, "user"),
            Role.TOOL,
            timestamp=STAMP,
            text="tool output",
            tool_call_id="fixture-call",
        ),
        context("after tool", index=5),
        message("tail", Role.ASSISTANT, index=6),
    ]
    data, dropped = serialize(events)
    assert not dropped
    native = json.loads(data)
    assert [item["type"] for item in native["messages"]] == [
        "user",
        "synthetic",
        "synthetic",
        "assistant",
        "synthetic",
        "assistant",
        "synthetic",
        "assistant",
    ]
    parsed = opencode.parse_import_bytes(data)
    assert [event.kind for event in parsed.events] == [event.kind for event in events]
    assert [event.text for event in parsed.events] == [event.text for event in events]
    assert [event.text for event in parsed.events if event.kind is EventKind.MESSAGE] == [
        "request",
        "answer",
        "tail",
    ]


def test_native_context_preserves_original_index_after_omitted_control_record():
    data, _ = serialize([message(), context()])
    native = json.loads(data)
    native["messages"].insert(
        1,
        {
            "id": "msg_fixture-control",
            "type": "idle",
            "outcome": "succeeded",
            "time": copy.deepcopy(native["messages"][0]["time"]),
        },
    )
    parsed = opencode.parse_import_bytes(json.dumps(native).encode())
    recovered = next(event for event in parsed.events if is_synthetic_text_context(event))
    assert recovered.provenance.record_index == 2
    assert recovered.provenance.record_type == "synthetic"


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"session_migrate_context": {"kind": "synthetic_text", "version": True}},
        {"session_migrate_context": {"kind": "synthetic_text", "version": 1.0}},
        {"session_migrate_context": {"kind": "synthetic_text", "version": "1"}},
        {"session_migrate_context": {"kind": "synthetic_text", "version": 2}},
        {"session_migrate_context": {"kind": "system", "version": 1}},
        {"session_migrate_context": {"kind": "synthetic_text", "version": 1, "role": "system"}},
        {"session_migrate_context": {"kind": "synthetic_text"}},
        {"session_migrate_context": {"version": 1}},
        {"session_migrate_context": []},
        {"session_migrate_context": {"kind": "synthetic_text", "version": 1}, "future": {}},
    ],
)
def test_unmarked_and_forged_synthetic_records_remain_opaque(metadata):
    data, _ = serialize([message(), context()])
    native = json.loads(data)
    candidate = native["messages"][1]
    if metadata is None:
        candidate.pop("metadata")
    else:
        candidate["metadata"] = metadata
    assert not is_marked_synthetic_text(candidate)
    parsed = opencode.parse_import_bytes(json.dumps(native).encode())
    assert not any(event.kind is EventKind.CONTEXT for event in parsed.events)
    assert any(event.kind is EventKind.OPAQUE for event in parsed.events)
    _, audit, observed = observe(native)
    assert audit.coverage is Coverage.UNKNOWN
    assert any(
        atom.category == UNKNOWN and atom.path[:2] == ("messages", 1) for atom in observed.atoms
    )


@pytest.mark.parametrize("kind", ["user", "system"])
def test_marker_on_other_native_kind_never_grants_context_type(kind):
    data, _ = serialize([message(), context()])
    native = json.loads(data)
    native["messages"][1]["type"] = kind
    parsed = opencode.parse_import_bytes(json.dumps(native).encode())
    assert not any(event.kind is EventKind.CONTEXT for event in parsed.events)
    if kind == "user":
        genuine = next(event for event in parsed.events if event.text == "historical context")
        expected = context()
        assert (
            hash_events([genuine], CanonicalPolicy()).prefix_witness
            != hash_events([expected], CanonicalPolicy()).prefix_witness
        )


def test_extra_native_fields_are_unknown_without_copying_operational_data():
    data, _ = serialize([message(), context()])
    native = json.loads(data)
    native["messages"][1]["future"] = {"permissions": ["fixture-policy"]}
    parsed = opencode.parse_import_bytes(json.dumps(native).encode())
    retained = next(event for event in parsed.events if is_synthetic_text_context(event))
    assert retained.payload == {"block_type": SYNTHETIC_TEXT_BLOCK_TYPE}
    assert any(event.kind is EventKind.OPAQUE for event in parsed.events)
    _, audit, observation = observe(native)
    assert audit.coverage is Coverage.UNKNOWN
    assert any(atom.category == UNKNOWN and "future" in atom.path for atom in observation.atoms)
    reserialized, _ = serialize(parsed.events)
    result = json.loads(reserialized)
    assert "future" not in result["messages"][1]
    assert "permissions" not in result["info"]


@pytest.mark.parametrize("release", ["1.17.20", "2.0.23", "2.1.1", "2.2.6", "opencode v2.2.5"])
def test_other_writer_versions_report_loss_without_context_injection(release):
    data, dropped = serialize([message(), context("never injected")], release)
    assert dropped == {"context:synthetic_text_unsupported_target": 1}
    assert "never injected" not in data.decode()
    parsed = opencode.parse_import_bytes(data)
    assert [event.text for event in parsed.events] == ["request"]


def test_legacy_schema_and_forged_legacy_carrier_cannot_promote_context():
    data, dropped = serialize([message(), context("never injected")], legacy=True)
    assert dropped == {"context:synthetic_text_unsupported_target": 1}
    assert "never injected" not in data.decode()
    legacy = json.loads(data)
    legacy["messages"][0]["info"].update(synthetic_text_metadata())
    legacy["messages"][0]["parts"][0].update(
        {"metadata": synthetic_text_metadata(), "synthetic": True}
    )
    parsed = opencode.parse_import_bytes(json.dumps(legacy).encode())
    assert not any(event.kind is EventKind.CONTEXT for event in parsed.events)
    assert parsed.events[0].kind is EventKind.MESSAGE


@pytest.mark.parametrize("release", ["2.0.23", "2.1.1"])
def test_old_audit_clients_do_not_acquire_synthetic_text_grammar(release):
    data, _ = serialize([message(), context()])
    _, audit, observation = observe(json.loads(data), release)
    assert audit.coverage is Coverage.UNKNOWN
    assert any(
        atom.category == UNKNOWN and atom.path[:2] == ("messages", 1) for atom in observation.atoms
    )


def test_marker_and_canonical_recognizers_require_text_context_with_user_role():
    marker = synthetic_text_metadata()
    marker["session_migrate_context"]["kind"] = "system"
    assert synthetic_text_metadata()["session_migrate_context"]["kind"] == "synthetic_text"
    original = context()
    assert is_synthetic_text_context(original)
    assert not is_synthetic_text_context(replace(original, role=Role.SYSTEM))
    assert not is_synthetic_text_context(replace(original, kind=EventKind.MESSAGE))
    assert not is_synthetic_text_context(replace(original, text=None))
    assert not is_synthetic_text_context(replace(original, payload={"block_type": "image"}))


def test_context_payload_does_not_transport_policy_fields():
    original = replace(
        context(),
        payload={
            "block_type": "synthetic_text",
            "permissions": ["fixture"],
            "hooks": {"fixture": True},
        },
    )
    data, _ = serialize([original])
    record = json.loads(data)["messages"][0]
    assert set(record) == {"id", "time", "type", "text", "metadata"}
    assert record["metadata"] == synthetic_text_metadata()
