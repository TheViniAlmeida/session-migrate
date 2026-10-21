"""Exact 2.1.1 public exports of kept tails and private checkpoints."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from session_migrate.conversion import ConversionOptions, convert_session, load_opencode_session
from session_migrate.formats import codex
from session_migrate.model import EventKind, TargetFormat


@pytest.mark.parametrize("private_window", [False, True])
def test_stock_v21_compaction_export_preserves_portable_history(tmp_path, private_window):
    binary = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V2")
    if not binary:
        pytest.skip("set SESSION_MIGRATE_TEST_OPENCODE_V2 to stock 2.1.1")
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_CONFIG_CONTENT": '{"warming":false,"disabled_providers":["opencode"]}',
    }
    for name in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        folder = tmp_path / name.lower()
        folder.mkdir(mode=0o700)
        env[name] = str(folder)

    def run(*args):
        result = subprocess.run(
            [binary, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    release = run("--version").strip().removeprefix("opencode v")
    if release != "2.1.1":
        pytest.skip("2.1-specific compaction gate requires stock 2.1.1")
    source = codex.parse(Path(__file__).parent / "fixtures/codex-0.144.4/basic.jsonl")
    options = ConversionOptions(
        TargetFormat.OPENCODE,
        target_cli_version=release,
        cwd=tmp_path,
        model_provider="fixture",
        model="test",
    )
    artifact = convert_session(source, options)
    value = json.loads(artifact.native_bytes)
    checkpoint = next(m for m in value["messages"] if m["type"] == "compaction")
    user = next(m for m in value["messages"] if m["type"] == "user")
    user["text"] = "SYNTHETIC_KEPT_USER_TAIL"
    checkpoint["summary"] = "SYNTHETIC_SUMMARY_BOUNDARY"
    if private_window:
        checkpoint["native"] = {"messages": [], "opaque": "PRIVATE_REPLAY_MARKER"}
    else:
        checkpoint["metadata"] = {"keepID": user["id"]}
    path = tmp_path / "import.json"
    path.write_text(json.dumps(value))
    run("session", "import", str(path), "--directory", str(tmp_path), "--standalone")
    exported = json.loads(run("session", "export", artifact.session_id, "--standalone"))
    actual = next(m for m in exported["messages"] if m["type"] == "compaction")
    assert "recent" not in actual
    parsed = load_opencode_session(artifact.session_id, source_cli=Path(binary), environ=env)
    assert parsed.cli_version == release
    if private_window:
        assert not any(e.kind == EventKind.COMPACTION for e in parsed.events)
        assert any(e.text == user["text"] for e in parsed.events)
        assert "PRIVATE_REPLAY_MARKER" not in repr(parsed.events)
    else:
        assert actual["metadata"]["keepID"] == user["id"]
        summary = next(e for e in parsed.events if e.kind == EventKind.COMPACTION)
        assert user["text"] in summary.text
        assert checkpoint["summary"] in summary.text
    migrated = convert_session(parsed, options)
    assert b"PRIVATE_REPLAY_MARKER" not in migrated.native_bytes
    path.write_bytes(migrated.native_bytes)
    run("session", "import", str(path), "--directory", str(tmp_path), "--standalone")
    reopened = load_opencode_session(migrated.session_id, source_cli=Path(binary), environ=env)
    assert any("SYNTHETIC_KEPT_USER_TAIL" in (e.text or "") for e in reopened.events)
