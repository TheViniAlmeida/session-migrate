import pytest

from session_migrate.capabilities import capabilities_for, list_capabilities
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat


def test_every_format_exposes_conservative_source_target_and_proof_states():
    assert {entry.format for entry in list_capabilities()} == set(AgentFormat)
    for format in AgentFormat:
        capability = capabilities_for(format)
        assert capability.source_supported
        assert capability.target_supported
        assert capability.pinned_cli_version
        assert not capability.native_title_supported
        assert not capability.update_supported
        assert capability.proof_version is None
        assert "proof" in capability.title_reason
        assert "proof" in capability.update_reason


def test_schema_directions_do_not_claim_codex_paginated_target():
    assert capabilities_for("codex", schema="paginated").source_supported
    assert not capabilities_for("codex", schema="paginated").target_supported
    assert capabilities_for("codex", schema="legacy").target_supported
    assert {entry.schema for entry in list_capabilities("opencode")} == {"legacy", "v2"}
    assert capabilities_for("opencode", schema="v2").target_supported


def test_media_is_scoped_or_unknown_and_cursor_is_text_only():
    assert capabilities_for("claude").media_source
    assert capabilities_for("opencode", schema="v2").media_target
    assert "restrictions" in capabilities_for("opencode", schema="v2").media_scope
    assert not capabilities_for("cursor").media_target
    assert capabilities_for("hermes").media_target is None


@pytest.mark.parametrize(
    "format,schema", [("unknown", None), ("opencode", "v99"), ("codex", "unknown"), ("pi", "v4")]
)
def test_unknown_format_or_schema_fails_explicitly(format, schema):
    with pytest.raises(SessionMigrateError, match="unsupported|unknown"):
        capabilities_for(format, schema=schema)
