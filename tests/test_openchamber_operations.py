"""Connected destinations keep authority and source transport separate."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate import conversion, openchamber, operations
from session_migrate.errors import SessionMigrateError
from session_migrate.model import TargetFormat
from session_migrate.operations import OperationRequest, commit_operation, prepare_operation

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
SERVER = "http://127.0.0.1:4096"


def test_target_transport_does_not_redirect_source_or_survive_inheritance(tmp_path):
    request = OperationRequest(
        "import",
        path=FIXTURE,
        target_format=TargetFormat.OPENCODE,
        target_client="openchamber",
        target_server=SERVER,
    )
    inherited = {
        "HOME": str(tmp_path),
        "SESSION_MIGRATE_OPENCODE_SERVER": "http://127.0.0.1:9999",
        "SESSION_MIGRATE_OPENCODE_BINDING": "untrusted",
    }
    _, target, source = operations._operation_context(request, inherited, None)
    assert target["SESSION_MIGRATE_OPENCODE_SERVER"] == SERVER
    assert "SESSION_MIGRATE_OPENCODE_SERVER" not in source
    assert "SESSION_MIGRATE_OPENCODE_BINDING" not in target
    assert "SESSION_MIGRATE_OPENCODE_BINDING" not in source
    _, target, source = operations._operation_context(
        replace(request, target_client=None, target_server=None), inherited, None
    )
    assert "SESSION_MIGRATE_OPENCODE_SERVER" not in target
    assert inherited["SESSION_MIGRATE_OPENCODE_BINDING"] == "untrusted"


def test_server_cannot_be_silently_ignored_by_another_destination():
    with pytest.raises(SessionMigrateError, match="OpenChamber destination"):
        OperationRequest("import", target_format=TargetFormat.OPENCODE, target_server=SERVER)
    with pytest.raises(SessionMigrateError, match="OpenCode format"):
        OperationRequest("import", target_format=TargetFormat.CODEX, target_client="openchamber")


def prepared_backend(tmp_path, monkeypatch):
    cli = tmp_path / "opencode"
    cli.write_text("synthetic client; never executed\n")
    cli.chmod(0o700)
    root = tmp_path / "home/.local/share/opencode"
    root.mkdir(parents=True)
    binding = {
        "version": "2.0.27",
        "pid": 123,
        "epoch": "1",
        "root": str(root),
        "database": str(root / "opencode.db"),
    }
    monkeypatch.setattr(operations, "detect_opencode_version", lambda *a, **kw: "2.0.27")
    monkeypatch.setattr(conversion, "_opencode_version", lambda *a, **kw: "2.0.27")
    monkeypatch.setattr(openchamber, "assert_backend_matches", lambda *a: dict(binding))
    installs = []
    monkeypatch.setattr(operations, "_install", lambda *a: installs.append(a[-1]))
    request = OperationRequest(
        "import",
        path=FIXTURE,
        target_format=TargetFormat.OPENCODE,
        target_client="openchamber",
        target_server=SERVER,
        target_cli=cli,
        cwd=tmp_path,
        model_provider="openai",
    )
    prepared = prepare_operation(
        request,
        environ={
            "HOME": str(tmp_path / "home"),
            "OPENCODE_SERVER_PASSWORD": "YOUR_API_KEY_HERE",
        },
    )
    return prepared, binding, installs


def test_preparation_records_backend_but_never_credentials(tmp_path, monkeypatch):
    prepared, binding, installs = prepared_backend(tmp_path, monkeypatch)
    doc = operations.proposal_metadata(prepared).to_dict()
    assert doc["target"]["client"] == "openchamber"
    assert doc["target"]["format"] == "opencode"
    assert doc["target"]["backend"] == binding
    assert doc["target"]["root"] == binding["root"]
    assert "YOUR_API_KEY_HERE" not in json.dumps(doc)
    assert installs == [True]
    assert (
        json.loads(prepared._state.target_environment["SESSION_MIGRATE_OPENCODE_BINDING"])
        == binding
    )
    prepared.discard()


def test_changed_backend_is_rejected_before_any_native_publication(tmp_path, monkeypatch):
    prepared, binding, installs = prepared_backend(tmp_path, monkeypatch)
    binding["pid"] += 1
    with pytest.raises(SessionMigrateError, match="backend changed"):
        commit_operation(prepared)
    assert installs == [True]


def test_old_result_contract_remains_exact():
    result = {"target_format": "codex"}
    assert operations._client_result(result, OperationRequest("convert")) is result
    request = OperationRequest(
        "convert", target_format=TargetFormat.OPENCODE, target_client="openchamber"
    )
    assert operations._client_result(result, request, completed=True) == {
        "target_format": "codex",
        "target_client": "openchamber",
    }
