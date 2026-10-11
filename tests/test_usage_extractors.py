"""Untrusted decoder candidates from actual private file captures, not issued proofs."""

import json
from pathlib import Path

import pytest

from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_source
from session_migrate.usage_extractors import decode_usage

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(tmp_path, path, format):
    private = tmp_path / "source"
    private.write_bytes(path.read_bytes())
    private.chmod(0o600)
    return capture_source(SourceCaptureContext(format, private))


def test_claude_missing_request_has_no_synthetic_identity(tmp_path):
    snapshot = read_fixture(tmp_path, FIXTURES / "claude-2.1.209/basic.jsonl", AgentFormat.CLAUDE)
    samples = decode_usage(snapshot.raw_bytes, AgentFormat.CLAUDE, "2.1.209", 100)
    assert len(samples) == 3
    assert all(not s.identity_parts and s.request_id is None for s in samples)
    assert dict(samples[0].counters)["input"] == 1


def test_legacy_missing_usage_never_becomes_zero(tmp_path):
    snapshot = read_fixture(tmp_path, FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    assert decode_usage(snapshot.raw_bytes, AgentFormat.CODEX, "0.144.4", 100) == ()


def test_paginated_aliases_remain_separate_unproven_scopes(tmp_path):
    snapshot = read_fixture(tmp_path, FIXTURES / "codex-0.153.4/paginated.jsonl", AgentFormat.CODEX)
    samples = decode_usage(snapshot.raw_bytes, AgentFormat.CODEX, "0.153.4", 100)
    assert [s.scope for s in samples] == ["turn", "thread_cumulative", "request"]
    assert dict(samples[0].counters)["input"] == 10
    assert not samples[1].identity_parts and not samples[2].identity_parts


def test_opencode_no_per_message_fallback_or_cost_inference(tmp_path):
    value = {
        "info": {
            "id": "ses_fixture",
            "tokens": {"input": 99, "output": 88},
            "model": {"id": "session-model", "providerID": "fixture"},
            "cost": 0,
        },
        "messages": [{"type": "assistant", "id": "msg_fixture"}],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    snapshot = capture_source(SourceCaptureContext(AgentFormat.OPENCODE, path))
    samples = decode_usage(snapshot.raw_bytes, AgentFormat.OPENCODE, "2.0.23", 100)
    assert dict(samples[0].counters)["input"] is None
    assert samples[0].model is None and samples[0].provider is None
    assert dict(samples[1].counters)["input"] == 99
    assert not hasattr(samples[0], "cost")


def test_unknown_release_and_caps(tmp_path):
    snapshot = read_fixture(tmp_path, FIXTURES / "claude-2.1.209/basic.jsonl", AgentFormat.CLAUDE)
    assert decode_usage(snapshot.raw_bytes, AgentFormat.CLAUDE, "future", 100) == ()
    with pytest.raises(ValueError):
        decode_usage(snapshot.raw_bytes, AgentFormat.CLAUDE, "2.1.209", 1)


def test_version_registry_does_not_remap_paginated_counter_to_legacy(tmp_path):
    snapshot = read_fixture(tmp_path, FIXTURES / "codex-0.153.4/paginated.jsonl", AgentFormat.CODEX)
    assert decode_usage(snapshot.raw_bytes, AgentFormat.CODEX, "0.144.4", 100) == ()
