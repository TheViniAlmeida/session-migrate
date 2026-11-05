"""Synthetic 0.166.0 attribution and conservative source-contract regressions."""

from copy import deepcopy

import pytest
from test_codex_latest import _observe, _records

from session_migrate.lineage import Coverage
from session_migrate.model import EventKind, Role


@pytest.mark.parametrize(
    "kind",
    (
        "agents_md.instructions",
        "hooks.additional_context",
        "multi_agent.subagent_notification",
        "multi_agent.inter_agent_message",
        "agent_message_board.notification",
        "current_time.reminder",
        "fixture_extension.internal_context",
        "additional_content.fixture_external_source",
        "compaction.summary",
        "recap.prompt",
        "guardian.sender_messages",
    ),
)
def test_nov_typed_harness_context_does_not_become_user_message(kind):
    records = _records("legacy", "0.166.0")
    records[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
        "content_item_kinds": [kind]
    }
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert "provider_body_known_loss" in dict(audit.issue_counts)
    assert not any(e.kind is EventKind.MESSAGE for e in observed.session.events)
    assert observed.session.events[0].payload["reason"] == "provider_context_message"


@pytest.mark.parametrize(
    "source", ("harness", "tool", "skills", "agents_md", "developer_instructions")
)
def test_nov_aligned_provenance_blocks_context_even_when_user_role_or_kind(source):
    records = _records("legacy", "0.166.0")
    records[1]["payload"]["content"] += [{"type": "input_text", "text": "PRIVATE_CONTEXT"}]
    records[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
        "content_item_kinds": ["user.text", "user.text"],
        "content_item_metadata": [
            {"provenance": {"type": "user"}},
            {"provenance": {"type": source}},
        ],
    }
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert [(e.role, e.text) for e in observed.session.events if e.kind is EventKind.MESSAGE] == [
        (Role.USER, "portable user")
    ]
    assert observed.session.events[-1].provenance.block_index == 1
    assert observed.session.events[-1].payload["source_content_kind"] == f"provenance:{source}"


@pytest.mark.parametrize(
    "source",
    ({"harness_injected": True}, {"source_tool_namespace": "functions"}),
)
def test_nov_legacy_attribution_aliases_do_not_promote_context(source):
    records = _records("legacy", "0.166.0")
    records[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
        "content_item_metadata": [source]
    }
    _, observed = _observe(records)
    assert not any(e.kind is EventKind.MESSAGE for e in observed.session.events)


@pytest.mark.parametrize(
    "source",
    (
        [],
        [{}, {}],
        "user",
        [None],
        [{"provenance": "user"}],
        [{"harness_injected": 1}],
        [{"source_tool_namespace": []}],
    ),
)
def test_nov_malformed_source_attribution_cannot_leak_text(source):
    records = _records("legacy", "0.166.0")
    records[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
        "content_item_metadata": source
    }
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert "invalid_content_item_metadata" in dict(audit.issue_counts)
    assert not any(e.kind is EventKind.MESSAGE for e in observed.session.events)


def test_nov_user_quotation_is_preserved_by_attribution_without_text_heuristic():
    records = _records("legacy", "0.166.0")
    text = '<codex_internal_context source="fixture">literal quotation</codex_internal_context>'
    records[1]["payload"]["content"][0]["text"] = text
    records[1]["payload"]["internal_chat_message_metadata_passthrough"] = {
        "content_item_metadata": [{"provenance": {"type": "user"}}]
    }
    _, observed = _observe(records)
    assert [(e.role, e.text) for e in observed.session.events if e.kind is EventKind.MESSAGE] == [
        (Role.USER, text)
    ]


@pytest.mark.parametrize("mode", ("legacy", "paginated"))
def test_nov_unknown_fields_and_usage_metadata_do_not_gain_accounting_or_full_fidelity(mode):
    records = _records(mode, "0.166.0")
    baseline = deepcopy(records)
    records[0]["payload"]["future_contract"] = {"looks_safe": True}
    audit, observed = _observe(records)
    assert audit.coverage is Coverage.UNKNOWN
    assert observed.identity is None
    assert any(atom.path[-2:] == ("future_contract", "looks_safe") for atom in audit.atoms)
    baseline.append(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_usage_includes_reasoning": True,
                    "total_token_usage": {"total_tokens": 42},
                },
            },
            **({"ordinal": len(baseline)} if mode == "paginated" else {}),
        }
    )
    usage_audit, _ = _observe(baseline)
    assert usage_audit.coverage is Coverage.UNKNOWN


@pytest.mark.parametrize("mode", ("legacy", "paginated"))
def test_nov_exact_version_audits_portable_subset_without_native_identity(mode):
    audit, observed = _observe(_records(mode, "0.166.0"))
    assert audit.client_id == "0.166.0"
    assert audit.coverage is Coverage.VERIFIED
    assert observed.identity is None


@pytest.mark.parametrize("version", ("0.164.0", "0.165.0", "0.166.1", "0.166.0-alpha.1"))
def test_nov_does_not_infer_support_from_version_range(version):
    audit, _ = _observe(_records("paginated", version))
    assert audit.client_id is None
    assert audit.coverage is Coverage.UNKNOWN
    assert "unsupported_client_version" in dict(audit.issue_counts)
