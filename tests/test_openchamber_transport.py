"""Local-only transport checks; all HTTP and process identity are synthetic."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest

from session_migrate import conversion, openchamber
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.model import TargetFormat


def test_connected_cli_cannot_be_redirected_by_proxy_environment():
    values = {
        "SESSION_MIGRATE_OPENCODE_SERVER": "http://127.0.0.1:4096",
        "SESSION_MIGRATE_OPENCODE_BINDING": "private metadata",
        "HTTP_PROXY": "http://untrusted.invalid:8080",
        "https_proxy": "http://untrusted.invalid:8080",
        "ALL_PROXY": "socks5://untrusted.invalid:1080",
        "OPENCODE_SERVER_PASSWORD": "YOUR_API_KEY_HERE",
    }
    result = conversion._opencode_process_environment(values)
    assert not any(key.lower().endswith("_proxy") and key.lower() != "no_proxy" for key in result)
    assert result["NO_PROXY"] == result["no_proxy"] == "127.0.0.1,localhost,::1"
    assert "SESSION_MIGRATE_OPENCODE_BINDING" not in result
    assert result["OPENCODE_SERVER_PASSWORD"] == values["OPENCODE_SERVER_PASSWORD"]
    assert "HTTP_PROXY" in values
    assert conversion._opencode_process_environment({"HTTP_PROXY": "legacy"}) == {
        "HTTP_PROXY": "legacy",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:4096",
        "http://0.0.0.0:4096",
        "http://example.com:4096",
        "http://127.0.0.1",
        "http://127.0.0.1:4096/",
        "http://127.0.0.1:4096/?token=YOUR_API_KEY_HERE",
        "http://user:YOUR_API_KEY_HERE@127.0.0.1:4096",
        "http://127.0.0.1:4096/#fragment",
    ],
)
def test_server_rejects_non_origin_urls_without_reflecting_input(url: str) -> None:
    with pytest.raises(SessionMigrateError) as error:
        openchamber.validated_server(url)
    assert "YOUR_API_KEY_HERE" not in str(error.value)


@pytest.mark.parametrize(
    "url", ["http://localhost:4096", "http://127.0.0.1:4096", "http://[::1]:4096"]
)
def test_server_accepts_loopback_origins(url: str) -> None:
    expected = url.replace("localhost", "127.0.0.1")
    assert openchamber.validated_server(url) == expected


class _Response:
    status = 200

    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_probe_binds_version_pid_epoch_and_expected_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def fetch(server, route, environ, **kwargs):
        observed["route"] = route
        return {"version": "2.0.27", "pid": 4321, "paths": {"tmp": "/tmp"}}

    monkeypatch.setattr(openchamber, "_get_json", fetch)
    monkeypatch.setattr(openchamber, "_process_epoch", lambda pid: "12345")
    monkeypatch.setattr(
        openchamber,
        "_listener_identity",
        lambda server, env: (4321, "12345", tmp_path / ".local/share/opencode/opencode.db", "777"),
    )
    monkeypatch.setattr(openchamber, "_has_listening_socket", lambda pid, server: True)

    def has_fd(pid, path):
        observed["pid"] = pid
        observed["database"] = path
        return True

    monkeypatch.setattr(openchamber, "_has_database_fd", has_fd)
    environment = {"HOME": str(tmp_path)}
    binding = openchamber.assert_backend_matches("http://127.0.0.1:4096", environment, "2.0.27")
    assert observed == {
        "route": "/api/info",
        "pid": 4321,
        "database": tmp_path / ".local/share/opencode/opencode.db",
    }
    assert binding["root"] == str(tmp_path / ".local/share/opencode")
    assert binding["epoch"] == "12345"
    assert binding["socket"] == "777"
    with pytest.raises(SessionMigrateError, match="versions differ"):
        openchamber.assert_backend_matches("http://127.0.0.1:4096", environment, "2.0.26")


def test_probe_rejects_missing_database_fd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        openchamber, "_get_json", lambda *args, **kwargs: {"version": "2.0.27", "pid": 23}
    )
    monkeypatch.setattr(openchamber, "_process_epoch", lambda pid: "123")
    monkeypatch.setattr(
        openchamber,
        "_listener_identity",
        lambda server, env: (23, "123", tmp_path / ".local/share/opencode/opencode.db", "777"),
    )
    monkeypatch.setattr(openchamber, "_has_listening_socket", lambda pid, server: True)
    monkeypatch.setattr(openchamber, "_has_database_fd", lambda pid, path: False)
    with pytest.raises(SessionMigrateError, match="database does not match"):
        openchamber.probe_backend("http://127.0.0.1:4096", {"HOME": str(tmp_path)})


def test_process_identity_reads_only_proc_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if str(path) == "/proc/42/status":
            return f"Name:\topencode\nUid:\t{os.getuid()}\t{os.getuid()}\n"
        if str(path) == "/proc/42/stat":
            return "42 (opencode server) " + " ".join(["0"] * 19 + ["123456"])
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert openchamber._process_epoch(42) == "123456"


def test_database_fd_requires_exact_open_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = tmp_path / "opencode.db"
    monkeypatch.setattr(Path, "iterdir", lambda path: iter([path / "3"]))
    monkeypatch.setattr(os, "readlink", lambda path: str(expected))
    monkeypatch.setattr(os.path, "samefile", lambda descriptor, path: True)
    assert openchamber._has_database_fd(42, expected)
    assert not openchamber._has_database_fd(42, tmp_path / "other.db")


def test_custom_database_override_cannot_change_root_or_filename(tmp_path: Path) -> None:
    environment = {"HOME": str(tmp_path)}
    default = tmp_path / ".local/share/opencode/opencode.db"
    assert openchamber._expected_database({**environment, "OPENCODE_DB": "opencode.db"}) == default
    for override in ("custom.db", str(tmp_path / "elsewhere/opencode.db")):
        with pytest.raises(
            SessionMigrateError, match="Custom OpenCode database path is unsupported"
        ):
            openchamber._expected_database({**environment, "OPENCODE_DB": override})


def test_proc_listener_scan_detects_ambiguous_exact_and_wildcard_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if str(path) == "/proc/net/tcp":
            return (
                "sl local_address rem_address st tx_queue tr tm uid timeout inode\n"
                "0: 0100007F:1000 00000000:0000 0A 0:0 00:0 0 0 0 777\n"
                "1: 00000000:1000 00000000:0000 0A 0:0 00:0 0 0 0 888\n"
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert openchamber._listening_socket_inodes("http://localhost:4096") == {"777", "888"}


def test_sole_wildcard_listener_serves_the_same_literal_loopback_origin(monkeypatch):
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if str(path) == "/proc/net/tcp":
            return (
                "sl local_address rem_address st tx_queue tr tm uid timeout inode\n"
                "0: 00000000:1000 00000000:0000 0A 0:0 00:0 0 0 0 888\n"
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert openchamber._listening_socket_inodes("http://localhost:4096") == {"888"}


def test_unverified_listener_blocks_http_before_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openchamber,
        "_listener_identity",
        lambda server, env: (_ for _ in ()).throw(SessionMigrateError("listener unverified")),
    )
    monkeypatch.setattr(
        openchamber.urllib.request,
        "build_opener",
        lambda *handlers: pytest.fail("HTTP opener constructed for unverified listener"),
    )
    with pytest.raises(SessionMigrateError, match="listener unverified"):
        openchamber._get_json(
            "http://127.0.0.1:4096",
            "/api/info",
            {"OPENCODE_SERVER_PASSWORD": "YOUR_API_KEY_HERE"},
        )


def test_socket_owner_without_expected_database_blocks_api_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = Path.iterdir
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda path: (
            iter([Path("/proc/41"), Path("/proc/42")]) if path == Path("/proc") else original(path)
        ),
    )
    monkeypatch.setattr(openchamber, "_listening_socket_inodes", lambda server: {"777"})
    monkeypatch.setattr(openchamber, "_has_socket_inode", lambda pid, inodes: pid == 41)
    monkeypatch.setattr(openchamber, "_process_epoch", lambda pid: "12345")
    monkeypatch.setattr(openchamber, "_has_database_fd", lambda pid, path: pid == 42)
    monkeypatch.setattr(
        openchamber,
        "_get_json",
        lambda *args, **kwargs: pytest.fail("API queried before socket and DB belonged to one PID"),
    )
    with pytest.raises(SessionMigrateError, match="listener is unverified"):
        openchamber.probe_backend("http://127.0.0.1:4096", {"HOME": str(tmp_path)})


def test_reused_listener_pid_blocks_api_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = Path.iterdir
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda path: iter([Path("/proc/42")]) if path == Path("/proc") else original(path),
    )
    monkeypatch.setattr(openchamber, "_listening_socket_inodes", lambda server: {"777"})
    monkeypatch.setattr(openchamber, "_has_socket_inode", lambda pid, inodes: True)
    epochs = iter(["12345", "12346"])
    monkeypatch.setattr(openchamber, "_process_epoch", lambda pid: next(epochs))
    monkeypatch.setattr(openchamber, "_has_database_fd", lambda pid, path: True)
    monkeypatch.setattr(
        openchamber,
        "_get_json",
        lambda *args, **kwargs: pytest.fail("API queried after process epoch changed"),
    )
    with pytest.raises(SessionMigrateError, match="listener changed"):
        openchamber.probe_backend("http://127.0.0.1:4096", {"HOME": str(tmp_path)})


def test_reused_port_multiple_listeners_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(openchamber, "_listening_socket_inodes", lambda server: {"777", "888"})
    with pytest.raises(SessionMigrateError, match="listener is unverified"):
        openchamber._listener_identity("http://127.0.0.1:4096", {"HOME": str(tmp_path)})


def test_http_transport_disables_redirects_and_sanitizes_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class Opener:
        def open(self, request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            raise HTTPError(request.full_url, 302, "redirect", {}, None)

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(openchamber.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(
        openchamber, "_listener_identity", lambda server, env: (42, "123", Path("/tmp/db"), "777")
    )
    with pytest.raises(SessionMigrateError) as error:
        openchamber._get_json(
            "http://localhost:4096",
            "/api/info",
            {"OPENCODE_SERVER_PASSWORD": "YOUR_API_KEY_HERE"},
        )
    assert "YOUR_API_KEY_HERE" not in str(error.value)
    assert seen["request"].full_url == "http://127.0.0.1:4096/api/info"
    assert seen["request"].get_header("Authorization", "").startswith("Basic ")
    assert any(isinstance(handler, openchamber._NoRedirect) for handler in seen["handlers"])
    assert seen["timeout"] == openchamber.HTTP_TIMEOUT_SECONDS


@pytest.mark.parametrize("body", [b'{"pid":1,"pid":2}', b'{"pid":NaN}'])
def test_http_transport_rejects_ambiguous_json(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    response = _Response({})
    response.body = body

    class Opener:
        def open(self, request, timeout):
            return response

    monkeypatch.setattr(openchamber.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(
        openchamber, "_listener_identity", lambda server, env: (42, "123", Path("/tmp/db"), "777")
    )
    with pytest.raises(SessionMigrateError, match="invalid JSON"):
        openchamber._get_json("http://127.0.0.1:4096", "/api/info", {})


def test_collision_uses_exact_detail_and_fails_on_other_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
    routes: list[str] = []

    def fetch(server, route, environ, **kwargs):
        routes.append(route)
        return None

    monkeypatch.setattr(openchamber, "_get_json", fetch)
    assert not openchamber.session_exists("http://localhost:4096", sid, {})
    assert routes == [f"/api/session/{sid}"]

    monkeypatch.setattr(
        openchamber,
        "_get_json",
        lambda *args, **kwargs: {"data": {"id": sid, "title": "Handoff"}},
    )
    assert openchamber.session_exists("http://localhost:4096", sid, {})

    def unavailable(server, route, environ, **kwargs):
        raise SessionMigrateError("OpenCode server request failed")

    monkeypatch.setattr(openchamber, "_get_json", unavailable)
    with pytest.raises(SessionMigrateError, match="request failed"):
        openchamber.session_exists("http://localhost:4096", sid, {})


def test_discovery_requires_exact_session_in_paginated_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
    routes: list[str] = []

    def fetch(server, route, environ, **kwargs):
        routes.append(route)
        if route.startswith("/api/session/"):
            return {"data": {"id": sid, "title": "Handoff", "location": {"directory": "/repo"}}}
        if "cursor=" not in route:
            return {"data": [{"id": "other"}], "cursor": {"next": "more"}}
        return {
            "data": [{"id": sid, "title": "Handoff", "location": {"directory": "/repo"}}],
            "cursor": {},
        }

    monkeypatch.setattr(openchamber, "_get_json", fetch)
    proof = openchamber.verify_session_discovery("http://localhost:4096", sid, Path("/repo"), {})
    assert proof == {"id": sid, "directory": "/repo", "title": "Handoff"}
    assert "limit=10" in routes[1]
    assert "search=Handoff" in routes[1]
    assert "cursor=more" in routes[2]


def _artifact(tmp_path: Path) -> conversion.ConversionArtifact:
    fixture = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
    return conversion.convert_session(
        claude.parse(fixture),
        conversion.ConversionOptions(
            target_format=TargetFormat.OPENCODE,
            session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            cwd=tmp_path,
            target_cli_version="2.0.27",
        ),
    )


def test_server_drift_before_import_stops_before_native_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = _artifact(tmp_path)
    monkeypatch.setattr(
        conversion, "_resolve_opencode_cli", lambda path, env: Path("/bin/opencode")
    )
    monkeypatch.setattr(conversion, "_opencode_version", lambda cli, env: "2.0.27")
    bindings = iter([{"pid": 1}, {"pid": 2}])
    monkeypatch.setattr(openchamber, "assert_backend_matches", lambda *args: next(bindings))
    monkeypatch.setattr(openchamber, "session_exists", lambda *args: False)
    monkeypatch.setattr(
        conversion,
        "_invoke_opencode_import",
        lambda *args, **kwargs: pytest.fail("native import ran after PID drift"),
    )
    manifest = tmp_path / "manifest.json"
    with pytest.raises(SessionMigrateError, match="identity changed before import"):
        conversion.install_opencode_artifact(
            artifact,
            manifest_path=manifest,
            environ={"SESSION_MIGRATE_OPENCODE_SERVER": "http://127.0.0.1:4096"},
        )
    assert not manifest.exists()


def test_prepared_binding_drift_stops_before_manifest_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = _artifact(tmp_path)
    prepared = {
        "version": "2.0.27",
        "pid": 111,
        "epoch": "12345",
        "root": str(tmp_path / "opencode"),
        "database": str(tmp_path / "opencode/opencode.db"),
        "socket": "777",
    }
    observed = {**prepared, "pid": 222}
    monkeypatch.setattr(
        conversion, "_resolve_opencode_cli", lambda path, env: Path("/bin/opencode")
    )
    monkeypatch.setattr(conversion, "_opencode_version", lambda cli, env: "2.0.27")
    monkeypatch.setattr(openchamber, "assert_backend_matches", lambda *args: observed)
    monkeypatch.setattr(
        openchamber,
        "session_exists",
        lambda *args: pytest.fail("collision lookup ran after prepared binding drift"),
    )
    monkeypatch.setattr(
        conversion,
        "_invoke_opencode_import",
        lambda *args, **kwargs: pytest.fail("native import ran after prepared binding drift"),
    )
    manifest = tmp_path / "manifest.json"
    with pytest.raises(SessionMigrateError, match="identity changed since preparation"):
        conversion.install_opencode_artifact(
            artifact,
            manifest_path=manifest,
            environ={
                "SESSION_MIGRATE_OPENCODE_SERVER": "http://127.0.0.1:4096",
                "SESSION_MIGRATE_OPENCODE_BINDING": json.dumps(prepared, sort_keys=True),
            },
        )
    assert not manifest.exists()


def test_prepared_binding_marker_rejects_invalid_json_without_echo() -> None:
    with pytest.raises(SessionMigrateError, match="prepared backend binding is invalid") as error:
        openchamber.parse_backend_binding('{"pid":NaN,"database":"YOUR_API_KEY_HERE"}')
    assert "YOUR_API_KEY_HERE" not in str(error.value)


def test_cli_import_uses_server_flag_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(conversion, "_opencode_version", lambda cli, env: "2.0.27")

    def run(command, environ):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, "Imported session: ses_test", "")

    monkeypatch.setattr(conversion, "_run_opencode", run)
    conversion._invoke_opencode_import(
        Path("/bin/opencode"),
        Path("/tmp/import.json"),
        {
            "SESSION_MIGRATE_OPENCODE_SERVER": "http://127.0.0.1:4096",
            "OPENCODE_SERVER_PASSWORD": "YOUR_API_KEY_HERE",
        },
        cwd=Path("/repo"),
    )
    assert captured[0][-2:] == ["--server", "http://127.0.0.1:4096"]
    assert "--standalone" not in captured[0]
    assert "YOUR_API_KEY_HERE" not in " ".join(captured[0])
