"""OpenChamber destination selection stays bound to an actual OpenCode store."""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from session_migrate.cli import _operation_request, build_parser, main
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import OperationRequest
from session_migrate.profiles import Profile, ProfileStore, _bind_profile_operation

SERVER = "http://127.0.0.1:4096"


def test_cli_alias_preserves_native_target_and_server():
    parser = build_parser()
    args = parser.parse_args(
        ["import", "/tmp/source.jsonl", "--to", "openchamber", "--target-server", SERVER]
    )
    request = _operation_request(args, "import")
    assert request.target_format is TargetFormat.OPENCODE
    assert request.target_client == "openchamber"
    assert request.target_server == SERVER
    assert request.source_format is None

    converted = _operation_request(
        parser.parse_args(
            ["convert", "/tmp/source.jsonl", "--to", "openchamber", "--output", "/tmp/out.json"]
        ),
        "convert",
    )
    assert converted.target_client == "openchamber" and converted.target_server is None

    with pytest.raises(SessionMigrateError, match="target-server"):
        _operation_request(
            parser.parse_args(["import", "/tmp/source.jsonl", "--to", "openchamber"]),
            "import",
        )


def test_cli_import_reports_missing_backend_before_source_read(capsys):
    assert main(["import", "/tmp/source.jsonl", "--to", "openchamber"]) == 2
    assert "--target-server" in capsys.readouterr().err


def test_legacy_profile_shape_and_fingerprint_are_exact(tmp_path):
    profile = Profile("legacy", "Legacy", "opencode", tmp_path / "opencode")
    expected = {
        "id": "legacy",
        "name": "Legacy",
        "format": "opencode",
        "root": str(tmp_path / "opencode"),
        "cli": None,
        "environ": {},
    }
    assert profile.to_dict() == expected
    assert (
        profile.fingerprint
        == hashlib.sha256(json.dumps(expected, sort_keys=True).encode("utf-8")).hexdigest()
    )

    store = ProfileStore(tmp_path / "private" / "profiles.json")
    store.create([profile])
    assert store.load()[0].to_dict() == expected


def test_openchamber_profile_roundtrip_and_rejects_untrusted_identity(tmp_path):
    target = Profile(
        "target",
        "Destination",
        AgentFormat.OPENCODE,
        tmp_path / "opencode",
        client="openchamber",
        server=SERVER,
    )
    assert target.to_dict()["client"] == "openchamber"
    assert target.to_dict()["server"] == SERVER
    store = ProfileStore(tmp_path / "private" / "profiles.json")
    store.create([target])
    assert store.load() == (target,)
    with pytest.raises(SessionMigrateError, match="OpenCode format"):
        replace(target, format=AgentFormat.CODEX)
    with pytest.raises(SessionMigrateError, match="local HTTP origin"):
        replace(target, server="https://example.test")


def test_profile_store_rejects_extra_client_metadata_keys(tmp_path):
    storage = tmp_path / "private"
    storage.mkdir(mode=0o700)
    path = storage / "profiles.json"
    row = Profile("target", "Destination", "opencode", tmp_path / "opencode").to_dict()
    row["server_url_override"] = SERVER
    path.write_text(json.dumps({"schema_version": 1, "profiles": [row]}))
    path.chmod(0o600)
    with pytest.raises(SessionMigrateError, match="row schema"):
        ProfileStore(path).load()


def test_profile_binding_refuses_client_or_server_conflict(monkeypatch, tmp_path):
    source = Profile("source", "Source", "claude", tmp_path / "claude")
    target = Profile(
        "target",
        "Destination",
        "opencode",
        tmp_path / "opencode",
        client="openchamber",
        server=SERVER,
    )
    catalog = SimpleNamespace(path=tmp_path / "catalog.sqlite3")
    request = OperationRequest("transfer", target_format="opencode")
    with pytest.raises(SessionMigrateError, match="target client"):
        _bind_profile_operation(
            catalog,
            source,
            object(),
            replace(target, client=None, server=None),
            replace(request, target_client="openchamber", target_server=SERVER),
            {},
        )
    with pytest.raises(SessionMigrateError, match="target server"):
        _bind_profile_operation(
            catalog,
            source,
            object(),
            target,
            replace(
                request,
                target_client="openchamber",
                target_server="http://localhost:4097",
            ),
            {},
        )

    monkeypatch.setattr(
        "session_migrate.profiles._resolve_entry", lambda *a: SimpleNamespace(catalog_id="c")
    )
    monkeypatch.setattr(
        "session_migrate.profiles.resolve_source",
        lambda *a: SimpleNamespace(format=AgentFormat.CLAUDE),
    )
    monkeypatch.setattr("session_migrate.profiles._source_environment", lambda *a: {})
    monkeypatch.setattr("session_migrate.profiles._cli", lambda *a, **kw: None)
    bound, target_env, _ = _bind_profile_operation(
        catalog,
        source,
        object(),
        target,
        request,
        {},
    )
    assert (bound.target_format, bound.target_client, bound.target_server) == (
        TargetFormat.OPENCODE,
        "openchamber",
        SERVER,
    )
    assert target_env["XDG_DATA_HOME"] == str(tmp_path)


def test_web_profile_metadata_is_opaque_and_server_is_private(tmp_path):
    from session_migrate.catalog import Catalog
    from session_migrate.web.auth import RunRegistry
    from session_migrate.web.backend import BackendError, WorkspaceBackend

    catalog_path = tmp_path / "catalog.sqlite3"
    with Catalog(catalog_path):
        pass
    profile = Profile(
        "target",
        "Destination",
        "opencode",
        tmp_path / "opencode",
        client="openchamber",
        server=SERVER,
    )
    registry = RunRegistry()
    credential = registry.create("http://127.0.0.1:8021", frozenset({"readonly", "publish"}))
    payload = json.loads(registry._bootstrap_payload(credential))
    backend = WorkspaceBackend(
        registry,
        catalog_path,
        (profile,),
        base_environ={"HOME": str(tmp_path / "home")},
        state_home=tmp_path / "state",
    )
    try:
        context = registry.authenticate(
            "Bearer " + payload["access_token"],
            host="127.0.0.1:8021",
            origin=None,
            method="GET",
            csrf_nonce=None,
        )
        values = asyncio.run(backend.dispatch("profiles", context, {}, {}))
        assert values["profiles"] == [
            {
                "id": next(iter(backend.profiles)),
                "name": "Destination",
                "format": "opencode",
                "client": "openchamber",
            }
        ]
        assert SERVER not in json.dumps(values)
        with pytest.raises(BackendError, match="field_invalid"):
            backend._prepare({"target_server": SERVER})
    finally:
        backend.close()
        registry.close()


def test_tui_destination_form_keeps_client_and_server(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from session_migrate.catalog import Catalog
    from session_migrate.tui.app import MigrationApp
    from session_migrate.tui.controller import Controller

    catalog_path = tmp_path / "catalog.sqlite3"
    with Catalog(catalog_path):
        pass
    controller = Controller(catalog_path, environ={"HOME": str(tmp_path / "home")})
    app = MigrationApp(controller)

    async def check():
        async with app.run_test(size=(120, 55)):
            target = Profile(
                "target",
                "Destination",
                "opencode",
                tmp_path / "opencode",
                client="openchamber",
                server=SERVER,
            )
            app.fill_profile("target", target)
            assert app.query_one("#target-format", Select).value == "openchamber"
            assert app.query_one("#target-server", Input).value == SERVER
            values = app.profile_form("target")
            assert values["format"] == "opencode"
            assert values["client"] == "openchamber"
            assert values["server"] == SERVER

    asyncio.run(check())
