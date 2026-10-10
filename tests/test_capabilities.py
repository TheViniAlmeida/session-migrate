import pytest

from session_migrate.capabilities import capabilities_for, list_capabilities, title_capability_for
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


@pytest.mark.parametrize("version", ["2.0.22", "2.0.23"])
def test_title_proof_is_scoped_to_exact_opencode_client_and_schema(version):
    proved = title_capability_for("opencode", cli_version=version)
    assert proved.status == "native_verified"
    assert proved.schema == "v2"
    assert proved.proof_version == version
    assert proved.proof_id == f"opencode-{version}-public-list-cold-export"
    assert (
        title_capability_for("opencode", schema="legacy", cli_version=version).status
        == "unsupported"
    )
    assert title_capability_for("opencode", cli_version="2.0.24").status == "unsupported"
    assert title_capability_for("opencode", cli_version="2.0.23-beta").status == "unsupported"
    assert title_capability_for("opencode", cli_version=None).status == "unsupported"
    assert title_capability_for("opencode", cli_version="2.0.22-beta").status == "unsupported"


@pytest.mark.parametrize("format", list(AgentFormat))
def test_local_alias_is_explicit_and_does_not_enable_native_title(format):
    native = title_capability_for(format)
    assert native.status == "unsupported"
    alias = title_capability_for(format, policy="local_alias")
    assert alias.status == "local_alias"
    assert alias.proof_version is None
    assert alias.proof_id is None
    assert "picker title is unchanged" in alias.reason


def test_title_policy_never_enables_unsupported_schema_direction():
    assert (
        title_capability_for("codex", schema="paginated", policy="local_alias").status
        == "unsupported"
    )
    with pytest.raises(SessionMigrateError, match="policy"):
        title_capability_for("claude", policy="auto")
    with pytest.raises(SessionMigrateError, match="version"):
        title_capability_for("claude", cli_version=1)
