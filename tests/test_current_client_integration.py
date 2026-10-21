"""Exact current-client contracts shared by migration interfaces and usage."""

import json
from pathlib import Path

import pytest

from session_migrate.capabilities import handoff_emission_supported
from session_migrate.conversion import ConversionOptions, EmitContext, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import OperationRequest, prepare_operation
from session_migrate.projections import ProjectionContext
from session_migrate.usage import UsageError, UsageSchemaKey
from session_migrate.usage_extractors import decode_usage


@pytest.mark.parametrize(
    "target,version",
    [
        (TargetFormat.CLAUDE, "2.1.303"),
        (TargetFormat.CLAUDE, "2.1.306"),
        (TargetFormat.CODEX, "0.163.0"),
    ],
)
def test_current_explicit_target_is_consistent_across_interfaces(tmp_path, target, version):
    source = claude.parse(Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl")
    artifact = convert_session(
        source, ConversionOptions(target, target_cli_version=version, cwd=tmp_path)
    )
    assert artifact.target_cli_version == version
    assert not any(w["code"] == "unvalidated_target_version" for w in artifact.warnings)
    ProjectionContext(target, version)
    assert handoff_emission_supported(AgentFormat(target), cli_version=version)
    EmitContext(
        target,
        artifact.session_id,
        tmp_path,
        version,
        "2026-10-21T00:00:00Z",
        "fixture",
        "fake-fixture-model",
    )


@pytest.mark.parametrize(
    "target,version", [(TargetFormat.CLAUDE, "2.1.307"), (TargetFormat.CODEX, "0.164.0")]
)
def test_unvalidated_future_client_does_not_gain_user_emission(tmp_path, target, version):
    assert not handoff_emission_supported(AgentFormat(target), cli_version=version)
    with pytest.raises(SessionMigrateError):
        EmitContext(
            target,
            "83838383-8383-4383-8383-838383838383",
            tmp_path,
            version,
            "2026-10-21T00:00:00Z",
            "fixture",
            "fake-fixture-model",
        )


def test_custom_claude_namespace_refused_before_preparation_side_effects(tmp_path):
    destination = tmp_path / "new-home"
    request = OperationRequest(
        "import",
        path=tmp_path / "not-read.jsonl",
        target_format=TargetFormat.CLAUDE,
        home=destination,
    )
    with pytest.raises(SessionMigrateError, match="custom project namespace is unsupported"):
        prepare_operation(
            request,
            environ={"HOME": str(destination), "CLAUDE_CODE_PROJECT_DIR_NAME": "synthetic-custom"},
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("schema", ["codex-legacy", "codex-paginated"])
def test_latest_codex_usage_accepts_only_proven_native_schema(schema):
    UsageSchemaKey("codex", "0.163.0", schema)
    with pytest.raises(UsageError):
        UsageSchemaKey("codex", "0.163.0", "unknown-schema")


def test_latest_counter_fields_keep_turn_request_and_cumulative_separate():
    counts = {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "cache_write_input_tokens": 3,
        "output_tokens": 4,
        "reasoning_output_tokens": 1,
        "total_tokens": 14,
    }
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "thread-fixture",
                "cli_version": "0.163.0",
                "model_provider": "fixture",
                "history_mode": "paginated",
            },
        },
        {
            "type": "token_usage_record",
            "payload": {
                "turn_id": "turn-fixture",
                "response_id": "response-fixture",
                "turn_token_usage": counts,
                "thread_token_usage": counts,
                "usage": counts,
            },
        },
    ]
    samples = decode_usage(
        b"\n".join(json.dumps(x).encode() for x in rows), AgentFormat.CODEX, "0.163.0", 100
    )
    assert [s.scope for s in samples] == ["turn", "thread_cumulative", "request"]
    assert [dict(s.counters)["cache_write"] for s in samples] == [3, 3, 3]
    assert [dict(s.counters)["total"] for s in samples] == [14, 14, 14]
    assert not samples[1].identity_parts and not samples[2].identity_parts


@pytest.mark.parametrize("version", ["2.1.303", "2.1.306"])
def test_current_claude_raw_counter_fields_remain_observations(version):
    row = {
        "type": "assistant",
        "uuid": "message-fixture",
        "message": {
            "model": "fake-haiku",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
            },
        },
    }
    samples = decode_usage(json.dumps(row).encode(), AgentFormat.CLAUDE, version, 100)
    assert len(samples) == 1
    assert dict(samples[0].counters)["input"] == 10
    assert dict(samples[0].counters)["cache_write"] == 1
    assert not samples[0].identity_parts
