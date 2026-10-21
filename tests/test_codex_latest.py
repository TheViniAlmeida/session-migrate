"""Synthetic grammar and loss guards matching stock Codex 0.163.0 captures.

Native execution and replay proof live in test_claude_codex_latest_native.py.
These unit cases never contact a provider or load client state.
"""

import json
from pathlib import Path

import pytest

from session_migrate.canonical import CanonicalPolicy
from session_migrate.formats import codex
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role
from session_migrate.native_audit import audit_snapshot, canonicalize_snapshot
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext


def _records(mode="paginated", version="0.163.0"):
    records = [
        {
            "type": "session_meta",
            "payload": {
                "session_id": "fixture-thread",
                "id": "fixture-thread",
                "cwd": "/fixture/work",
                "cli_version": version,
                "history_mode": mode,
            },
        }
    ]
    if mode == "paginated":
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "fixture-thread",
                    "turn_id": "fixture-turn",
                    "item": {
                        "type": "UserMessage",
                        "id": "fixture-item",
                        "content": [{"type": "text", "text": "portable user", "text_elements": []}],
                    },
                    "started_at_ms": 1,
                    "completed_at_ms": 2,
                },
            }
        )
        for index, record in enumerate(records):
            record["ordinal"] = index
    else:
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "portable user"}],
                },
            }
        )
    return records


def _observe(records):
    snapshot = NativeSnapshot.from_bytes(
        b"\n".join(json.dumps(record).encode() for record in records) + b"\n",
        context=SourceCaptureContext(AgentFormat.CODEX, Path("synthetic-latest.jsonl")),
    )
    audit = audit_snapshot(snapshot)
    return audit, canonicalize_snapshot(snapshot, audit, CanonicalPolicy())


def _append(records, row):
    if records[0]["payload"]["history_mode"] == "paginated":
        row["ordinal"] = len(records)
    records.append(row)


@pytest.mark.parametrize("mode", ("legacy", "paginated"))
def test_latest_exact_modes_accept_portable_subset_without_native_identity(mode):
    audit, observed = _observe(_records(mode))
    assert audit.client_id == "0.163.0"
    assert audit.coverage is Coverage.VERIFIED
    assert observed.fidelity is Coverage.VERIFIED
    assert observed.identity is None
    assert [(event.role, event.text) for event in observed.events] == [(Role.USER, "portable user")]


@pytest.mark.parametrize("version", ("0.163.1", "0.163.0-alpha.1", "0.999.0", [], None))
def test_unproved_version_never_inherits_latest_audit_support(version):
    audit, _ = _observe(_records(version=version))
    assert audit.client_id is None
    assert audit.coverage is Coverage.UNKNOWN
    assert "unsupported_client_version" in dict(audit.issue_counts)


@pytest.mark.parametrize("mode,version", (("legacy", "0.153.4"), ("paginated", "0.144.4")))
def test_old_versions_do_not_gain_unproved_history_mode(mode, version):
    audit, _ = _observe(_records(mode, version))
    assert audit.coverage is Coverage.UNKNOWN
    assert "unsupported_client_version" in dict(audit.issue_counts)


def test_latest_writer_keeps_legacy_schema_and_compatible_default():
    assert codex.PINNED_CODEX_VERSION == "0.144.4"
    assert {"0.144.4", "0.163.0"} == codex.VALIDATED_LEGACY_VERSIONS
    assert {"0.153.4", "0.163.0"} == codex.VALIDATED_PAGINATED_VERSIONS
    assert {"0.144.4", "0.163.0"} == codex.VALIDATED_TARGET_VERSIONS
    data, dropped = codex.serialize_events(
        [
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text="portable user",
                provenance=Provenance(0, "synthetic"),
            )
        ],
        session_id="fixture-thread",
        cwd=Path("/fixture/work"),
        cli_version="0.163.0",
    )
    records = [json.loads(line) for line in data.splitlines()]
    assert records[0]["payload"]["history_mode"] == "legacy"
    assert all("ordinal" not in record for record in records)
    assert not dropped
    audit, observed = _observe(records)
    assert audit.client_id == "0.163.0"
    assert observed.fidelity is Coverage.VERIFIED


@pytest.mark.parametrize("mode", ("legacy", "paginated"))
def test_file_image_reference_is_explicit_loss_without_empty_image_context(mode):
    records = _records(mode)
    payload = records[1]["payload"]
    content = payload["item"]["content"] if mode == "paginated" else payload["content"]
    content.append(
        {"type": "image" if mode == "paginated" else "input_image", "file_id": "file_synthetic"}
    )
    audit, observed = _observe(records)
    assert "image_file_id_loss" in dict(audit.issue_counts)
    assert any(
        atom.path[-1:] == ("file_id",) and atom.category == "known-loss" for atom in audit.atoms
    )
    assert observed.fidelity is not Coverage.VERIFIED
    assert not any(event.kind is EventKind.CONTEXT for event in observed.session.events)
    assert any(event.payload.get("reason") == "image_file_id" for event in observed.session.events)


def test_provider_annotations_and_encrypted_content_cannot_become_user_text():
    records = _records()
    _append(
        records,
        {
            "type": "response_item",
            "metadata": {"private_state": "DO_NOT_PROMOTE_METADATA"},
            "payload": {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "encrypted_content": "DO_NOT_PROMOTE_ENCRYPTED",
                "content": [
                    {"type": "output_text", "text": "DO_NOT_PROMOTE_PROVIDER", "annotations": []}
                ],
            },
        },
    )
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert any(
        atom.path[-1:] == ("annotations",) and atom.category == "known-loss" for atom in audit.atoms
    )
    assert any(
        atom.path[-1:] == ("encrypted_content",) and atom.category == "known-loss"
        for atom in audit.atoms
    )
    assert [event.text for event in observed.session.events if event.kind is EventKind.MESSAGE] == [
        "portable user"
    ]


def test_partial_answer_phase_is_loss_even_with_canonical_assistant_text():
    records = _records()
    _append(
        records,
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "fixture-thread",
                "turn_id": "fixture-turn",
                "started_at_ms": 1,
                "completed_at_ms": 2,
                "item": {
                    "type": "AgentMessage",
                    "id": "assistant-item",
                    "phase": "partial_answer",
                    "content": [{"type": "Text", "text": "partial portable answer"}],
                },
            },
        },
    )
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.VERIFIED
    assert observed.fidelity is Coverage.PARTIAL
    assert "phase_context_loss" in dict(audit.issue_counts)
    assert observed.session.events[-1].text == "partial portable answer"


def test_retained_context_and_compaction_resume_state_remain_unproved():
    records = _records()
    _append(
        records,
        {
            "type": "retained_context",
            "payload": {"type": "delivered_assistant_message", "text": "DO_NOT_PROMOTE_RETAINED"},
        },
    )
    _append(
        records,
        {
            "type": "compacted",
            "payload": {
                "message": "synthetic summary",
                "resume_metadata": {"last_started_turn_id": "fixture-turn"},
            },
        },
    )
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert observed.fidelity is not Coverage.VERIFIED
    assert [event.text for event in observed.session.events if event.kind is EventKind.MESSAGE] == [
        "portable user"
    ]


def test_reviewed_creation_metadata_does_not_grant_context_fidelity():
    records = _records()
    records[0]["payload"].update(
        {
            "runtime_workspace_roots": ["/fixture/work"],
            "thread_source": "user",
            "context_window": {"window_id": "fixture-window"},
            "base_instructions": {"text": "DO_NOT_PROMOTE_BASE_INSTRUCTIONS"},
        }
    )
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.VERIFIED
    assert observed.fidelity is Coverage.PARTIAL
    assert [event.text for event in observed.session.events] == ["portable user"]


@pytest.mark.parametrize("version", ("0.144.4", "0.163.0"))
def test_legacy_resume_uses_harness_kind_and_preserves_literal_user_xml(version):
    records = _records("legacy", version)
    literal = "<environment_context>literal user quotation</environment_context>"
    provider = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "PRIVATE_PROVIDER_ENVIRONMENT"}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "fixture-turn",
                "content_item_kinds": ["environments.environment_context"],
            },
        },
    }
    _append(records, provider)
    _append(
        records,
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": literal}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "fixture-turn",
                    "content_item_kinds": ["user.text"],
                },
            },
        },
    )
    _append(records, {"type": "event_msg", "payload": {"type": "user_message", "message": literal}})
    audit, observed = _observe(records)
    messages = [event.text for event in observed.session.events if event.kind is EventKind.MESSAGE]
    assert messages == ["portable user", literal]
    assert observed.session.raw_record_count == len(records)
    opaque = next(
        event
        for event in observed.session.events
        if event.payload.get("reason") == "provider_context_message"
    )
    assert opaque.provenance.record_index == 2
    assert opaque.provenance.block_index == 0
    assert opaque.text is None and opaque.role is None
    assert "provider_body_known_loss" in dict(audit.issue_counts)
    assert audit.coverage is Coverage.UNKNOWN  # Private passthrough is still unproved.


def test_legacy_mixed_content_kinds_are_aligned_per_block():
    records = _records("legacy")
    literal = "<environment_context>legitimate user text</environment_context>"
    records[1]["payload"].update(
        {
            "content": [
                {"type": "input_text", "text": literal},
                {"type": "input_text", "text": "PRIVATE_PROVIDER_ENVIRONMENT"},
            ],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text", "environments.environment_context"],
            },
        }
    )
    _, observed = _observe(records)
    assert [event.text for event in observed.session.events if event.kind is EventKind.MESSAGE] == [
        literal
    ]
    assert observed.session.events[-1].provenance.block_index == 1
    assert observed.session.events[-1].payload["reason"] == "provider_context_message"


def test_literal_ui_user_message_is_not_consumed_by_identical_provider_context():
    records = _records("legacy")
    literal = "<environment_context>user quotation</environment_context>"
    records[1]["payload"].update(
        {
            "content": [{"type": "input_text", "text": literal}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["environments.environment_context"],
            },
        }
    )
    _append(records, {"type": "event_msg", "payload": {"type": "user_message", "message": literal}})
    _, observed = _observe(records)
    assert [event.text for event in observed.session.events if event.kind is EventKind.MESSAGE] == [
        literal
    ]
    assert observed.session.events[-1].provenance.record_type == "event_msg"


@pytest.mark.parametrize(
    "kinds", (["user.text"], ["unknown"], ["environments.environment_context_extra"], None)
)
def test_legacy_xml_literal_without_exact_context_kind_is_preserved(kinds):
    records = _records("legacy")
    literal = "<environment_context>literal</environment_context>"
    records[1]["payload"]["content"][0]["text"] = literal
    records[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
        "content_item_kinds": kinds
    }
    _, observed = _observe(records)
    assert observed.session.events[0].kind is EventKind.MESSAGE
    assert observed.session.events[0].text == literal


@pytest.mark.parametrize(
    "kinds", ([], ["user.text", "environments.environment_context"], "user.text", [1])
)
def test_malformed_content_kind_alignment_cannot_leak_provider_context(kinds):
    records = _records("legacy")
    records[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
        "content_item_kinds": kinds
    }
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert "invalid_content_item_kinds" in dict(audit.issue_counts)
    assert observed.session.raw_record_count == 2
    assert not any(event.kind is EventKind.MESSAGE for event in observed.session.events)
    assert observed.session.events[0].payload["reason"] == "invalid_content_item_kinds"
