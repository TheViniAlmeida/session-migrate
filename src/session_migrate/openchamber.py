"""Bounded, local-only transport and identity checks for an OpenCode server."""

from __future__ import annotations

import base64
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode

MAX_RESPONSE_BYTES = 64 * 1024
HTTP_TIMEOUT_SECONDS = 5
MAX_BINDING_BYTES = 4096


def validated_server(url: str) -> str:
    """Require a plain HTTP origin on a literal loopback address."""
    if type(url) is not str:
        raise SessionMigrateError("OpenCode server URL must be a local HTTP origin")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise SessionMigrateError("OpenCode server URL must be a local HTTP origin") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or url != f"http://{parsed.netloc}"
        or parsed.netloc
        != (
            f"[{parsed.hostname}]:{port}"
            if parsed.hostname == "::1"
            else f"{parsed.hostname}:{port}"
        )
    ):
        raise SessionMigrateError("OpenCode server URL must be a local HTTP origin")
    return f"http://127.0.0.1:{port}" if parsed.hostname == "localhost" else url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _get_json(
    server: str,
    route: str,
    environ: Mapping[str, str],
    *,
    missing_ok: bool = False,
    expected_listener: tuple[int, str, Path, str] | None = None,
) -> Any:
    origin = validated_server(server)
    listener = _listener_identity(origin, environ)
    if expected_listener is not None and listener != expected_listener:
        raise SessionMigrateError("OpenCode server listener changed before request")
    headers = {"Accept": "application/json"}
    password = environ.get("OPENCODE_SERVER_PASSWORD") or environ.get("OPENCODE_PASSWORD")
    if password:
        credential = base64.b64encode(("opencode:" + password).encode()).decode("ascii")
        headers["Authorization"] = "Basic " + credential
    request = urllib.request.Request(origin + route, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise SessionMigrateError("OpenCode server returned an unexpected HTTP status")
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            if _listener_identity(origin, environ) != listener:
                raise SessionMigrateError(
                    "OpenCode server listener changed during request"
                ) from None
            return None
        raise SessionMigrateError("OpenCode server request failed") from None
    except (OSError, ValueError, TimeoutError):
        raise SessionMigrateError("OpenCode server request failed") from None
    if _listener_identity(origin, environ) != listener:
        raise SessionMigrateError("OpenCode server listener changed during request")
    if len(data) > MAX_RESPONSE_BYTES:
        raise SessionMigrateError("OpenCode server response exceeded the safety limit")
    try:
        return json.loads(data, parse_constant=_invalid_constant, object_pairs_hook=_unique_keys)
    except (UnicodeError, ValueError):
        raise SessionMigrateError("OpenCode server returned invalid JSON") from None


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _expected_database(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    xdg = environ.get("XDG_DATA_HOME")
    if xdg:
        if not Path(xdg).is_absolute():
            raise SessionMigrateError("OpenCode data home must be absolute")
        root = Path(xdg) / "opencode"
    elif home and Path(home).is_absolute():
        root = Path(home) / ".local/share/opencode"
    else:
        raise SessionMigrateError("OpenCode data home is unavailable")
    database = (root / "opencode.db").resolve(strict=False)
    override = environ.get("OPENCODE_DB")
    if override is not None:
        configured = Path(override)
        configured = configured if configured.is_absolute() else root / configured
        if configured.resolve(strict=False) != database:
            raise SessionMigrateError("Custom OpenCode database path is unsupported")
    return database


def _listening_socket_inodes(server: str) -> set[str]:
    parsed = urllib.parse.urlsplit(validated_server(server))
    host = parsed.hostname
    assert host in {"127.0.0.1", "::1"} and parsed.port is not None
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    packed = socket.inet_pton(family, host)
    address = "".join(packed[index : index + 4][::-1].hex() for index in range(0, len(packed), 4))
    table = Path("/proc/net/tcp6" if family == socket.AF_INET6 else "/proc/net/tcp")
    try:
        rows = table.read_text().splitlines()[1:]
    except OSError:
        raise SessionMigrateError("OpenCode server listener metadata is unavailable") from None
    inodes: set[str] = set()
    for row in rows:
        columns = row.split()
        if len(columns) < 10 or columns[3] != "0A":
            continue
        local = columns[1].split(":")
        if len(local) != 2:
            continue
        try:
            port = int(local[1], 16)
        except ValueError:
            continue
        if (
            local[0].lower() in {address, "0" * len(address)}
            and port == parsed.port
            and columns[9].isdecimal()
        ):
            inodes.add(columns[9])
    return inodes


def _has_socket_inode(pid: int, inodes: set[str]) -> bool:
    try:
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]") and target[8:-1] in inodes:
                return True
    except OSError:
        pass
    return False


def _has_listening_socket(pid: int, server: str) -> bool:
    return _has_socket_inode(pid, _listening_socket_inodes(server))


def _listener_identity(server: str, environ: Mapping[str, str]) -> tuple[int, str, Path, str]:
    """Prove one same-UID process owns the local origin listener and selected DB."""
    database = _expected_database(environ)
    inodes = _listening_socket_inodes(server)
    if len(inodes) != 1:
        raise SessionMigrateError("OpenCode server listener is unverified")
    socket_inode = next(iter(inodes))
    candidates: list[tuple[int, str, Path, str]] = []
    holders: set[int] = set()
    try:
        processes = Path("/proc").iterdir()
        for process in processes:
            if not process.name.isdecimal():
                continue
            pid = int(process.name)
            if not _has_socket_inode(pid, inodes):
                continue
            holders.add(pid)
            try:
                epoch = _process_epoch(pid)
            except SessionMigrateError:
                raise SessionMigrateError("OpenCode server listener is unverified") from None
            if _has_database_fd(pid, database):
                candidates.append((pid, epoch, database, socket_inode))
    except OSError:
        raise SessionMigrateError("OpenCode server listener is unverified") from None
    if len(candidates) != 1 or holders != {candidates[0][0]}:
        raise SessionMigrateError("OpenCode server listener is unverified")
    pid, epoch, database, socket_inode = candidates[0]
    if (
        _process_epoch(pid) != epoch
        or not _has_socket_inode(pid, inodes)
        or not _has_database_fd(pid, database)
    ):
        raise SessionMigrateError("OpenCode server listener changed during identity check")
    return candidates[0]


def _process_epoch(pid: int) -> str:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        owner = next(line for line in status.splitlines() if line.startswith("Uid:"))
        if int(owner.split()[1]) != os.getuid():
            raise SessionMigrateError("OpenCode server process has a different owner")
        stat_line = Path(f"/proc/{pid}/stat").read_text()
        # The process name is parenthesized and may contain spaces.
        epoch = stat_line.rsplit(") ", 1)[1].split()[19]
        if not epoch.isdecimal():
            raise ValueError
        return epoch
    except (OSError, StopIteration, ValueError, IndexError):
        raise SessionMigrateError("OpenCode server process identity is unavailable") from None


def _has_database_fd(pid: int, expected: Path) -> bool:
    try:
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.endswith(" (deleted)"):
                continue
            if Path(target).is_absolute() and Path(target).resolve(strict=False) == expected:
                try:
                    if os.path.samefile(descriptor, expected):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def probe_backend(server: str, environ: Mapping[str, str]) -> dict[str, str | int]:
    """Bind public version and PID to the local listener holding the expected DB."""
    origin = validated_server(server)
    listener_pid, listener_epoch, database, socket_inode = _listener_identity(origin, environ)
    info = _get_json(
        origin,
        "/api/info",
        environ,
        expected_listener=(listener_pid, listener_epoch, database, socket_inode),
    )
    if not isinstance(info, dict):
        raise SessionMigrateError("OpenCode server identity is invalid")
    version, pid = info.get("version"), info.get("pid")
    if (
        type(version) is not str
        or not opencode.supported_version(version)
        or not opencode.is_v2(version)
    ):
        raise SessionMigrateError("OpenCode server version is unsupported")
    if type(pid) is not int or pid != listener_pid:
        raise SessionMigrateError("OpenCode server PID is invalid")
    epoch = _process_epoch(pid)
    if epoch != listener_epoch or not _has_listening_socket(pid, origin):
        raise SessionMigrateError("OpenCode server listener changed during identity check")
    if not _has_database_fd(pid, database):
        raise SessionMigrateError("OpenCode server database does not match the selected data home")
    if _process_epoch(pid) != epoch:
        raise SessionMigrateError("OpenCode server process changed during identity check")
    if _listener_identity(origin, environ) != (pid, epoch, database, socket_inode):
        raise SessionMigrateError("OpenCode server listener changed during identity check")
    return {
        "version": version,
        "pid": pid,
        "epoch": epoch,
        "root": str(database.parent),
        "database": str(database),
        "socket": socket_inode,
    }


def assert_backend_matches(
    server: str, environ: Mapping[str, str], cli_version: str
) -> dict[str, str | int]:
    binding = probe_backend(server, environ)
    if binding["version"] != cli_version:
        raise SessionMigrateError("OpenCode CLI and server versions differ")
    return binding


def parse_backend_binding(marker: str) -> dict[str, str | int]:
    """Validate the private prepared-state marker without reflecting its value."""
    invalid = SessionMigrateError("OpenCode prepared backend binding is invalid")
    if type(marker) is not str:
        raise invalid
    try:
        if len(marker.encode("utf-8")) > MAX_BINDING_BYTES:
            raise ValueError
        binding = json.loads(
            marker, parse_constant=_invalid_constant, object_pairs_hook=_unique_keys
        )
    except (UnicodeError, ValueError):
        raise invalid from None
    if not isinstance(binding, dict) or set(binding) != {
        "version",
        "pid",
        "epoch",
        "root",
        "database",
        "socket",
    }:
        raise invalid
    version, pid, epoch = binding["version"], binding["pid"], binding["epoch"]
    root, database = binding["root"], binding["database"]
    socket_inode = binding["socket"]
    if (
        type(version) is not str
        or not opencode.is_v2(version)
        or type(pid) is not int
        or pid < 1
        or type(epoch) is not str
        or not epoch.isdecimal()
        or type(root) is not str
        or type(database) is not str
        or not Path(root).is_absolute()
        or not Path(database).is_absolute()
        or Path(database).parent != Path(root)
        or type(socket_inode) is not str
        or not socket_inode.isdecimal()
    ):
        raise invalid
    return binding


def session_exists(server: str, session_id: str, environ: Mapping[str, str]) -> bool:
    if not session_id.startswith("ses_") or not session_id[4:].isalnum():
        raise SessionMigrateError("OpenCode session ID is invalid")
    response = _get_json(
        server, "/api/session/" + urllib.parse.quote(session_id), environ, missing_ok=True
    )
    if response is None:
        return False
    value = _session_detail(response)
    if not isinstance(value, dict) or value.get("id") != session_id:
        raise SessionMigrateError("OpenCode server returned mismatched session metadata")
    return True


def _session_detail(response: Any) -> Any:
    if isinstance(response, dict) and isinstance(response.get("data"), dict):
        return response["data"]
    return response


def list_session_ids(server: str, environ: Mapping[str, str]) -> set[str]:
    """Read all server pages, failing closed on an unbounded or malformed list."""
    result: set[str] = set()
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(1000):
        route = "/api/session?limit=10&order=desc"
        if cursor is not None:
            route += "&cursor=" + urllib.parse.quote(cursor, safe="")
        page = _get_json(server, route, environ)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise SessionMigrateError("OpenCode session list returned invalid metadata")
        for item in page["data"]:
            if not isinstance(item, dict) or type(item.get("id")) is not str:
                raise SessionMigrateError("OpenCode session list returned invalid metadata")
            result.add(item["id"])
        next_cursor = (
            page.get("cursor", {}).get("next") if isinstance(page.get("cursor"), dict) else None
        )
        if next_cursor is None:
            return result
        if type(next_cursor) is not str or not next_cursor or next_cursor in seen:
            raise SessionMigrateError("OpenCode session list cursor is invalid")
        seen.add(next_cursor)
        cursor = next_cursor
    raise SessionMigrateError("OpenCode session list exceeded the page safety limit")


def verify_session_discovery(
    server: str, session_id: str, cwd: Path, environ: Mapping[str, str]
) -> dict[str, str]:
    """Require detail and a bounded directory/title search to expose the imported ID."""
    detail = _session_detail(
        _get_json(server, "/api/session/" + urllib.parse.quote(session_id), environ)
    )
    if not isinstance(detail, dict) or detail.get("id") != session_id:
        raise SessionMigrateError("OpenCode imported session detail is missing")
    location = detail.get("location")
    directory = location.get("directory") if isinstance(location, dict) else detail.get("directory")
    title = detail.get("title")
    if directory != str(cwd) or type(title) is not str or not title:
        raise SessionMigrateError("OpenCode imported session metadata differs from the target")
    detail_time = detail.get("time")
    if isinstance(detail_time, dict) and detail_time.get("archived"):
        raise SessionMigrateError("OpenCode imported session is archived")
    query = urllib.parse.urlencode({"limit": 10, "search": title, "directory": str(cwd)})
    cursor: str | None = None
    for _ in range(50):
        route = "/api/session?" + query
        if cursor is not None:
            route += "&cursor=" + urllib.parse.quote(cursor, safe="")
        page = _get_json(server, route, environ)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise SessionMigrateError("OpenCode session discovery returned invalid metadata")
        for item in page["data"]:
            if isinstance(item, dict) and item.get("id") == session_id:
                item_location = item.get("location")
                item_directory = (
                    item_location.get("directory")
                    if isinstance(item_location, dict)
                    else item.get("directory")
                )
                if item_directory != str(cwd) or item.get("title") != title:
                    raise SessionMigrateError("OpenCode session discovery metadata differs")
                item_time = item.get("time")
                if isinstance(item_time, dict) and item_time.get("archived"):
                    raise SessionMigrateError("OpenCode imported session is archived")
                return {"id": session_id, "directory": str(cwd), "title": title}
        next_cursor = (
            page.get("cursor", {}).get("next") if isinstance(page.get("cursor"), dict) else None
        )
        if next_cursor is None:
            break
        if type(next_cursor) is not str or not next_cursor or next_cursor == cursor:
            raise SessionMigrateError("OpenCode session discovery cursor is invalid")
        cursor = next_cursor
    raise SessionMigrateError("OpenCode import returned success but session is not discoverable")
