"""Private, durable metadata journal for a multi-operation coordinator.

No native payload or replay authority is stored here. An interrupted ``attempting``
event always needs explicit read-only reconciliation; opening a journal never imports.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.native_namespaces import ensure_migration_state_home

MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 8192
_ZERO = "0" * 64
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,95}\Z")
_ID = re.compile(r"ses_[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class TreeJournalError(SessionMigrateError):
    """A bounded journal or its physical namespace cannot be trusted."""


def _fail() -> None:
    raise TreeJournalError("Tree coordinator journal is invalid, changed or unavailable")


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _uuid(value: str) -> str:
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail()
    except (ValueError, TypeError, AttributeError):
        _fail()
    return value


def journal_path(state_home: Path, operation_id: str) -> Path:
    _uuid(operation_id)
    if not isinstance(state_home, Path) or not state_home.is_absolute() or ".." in state_home.parts:
        _fail()
    return state_home / "trees-v1" / (operation_id + ".jsonl")


def _safe_event(event: dict) -> None:
    """Reject accidental content persistence instead of trusting a caller's dict."""
    if type(event) is not dict or event.get("type") not in {
        "prepared",
        "attempting",
        "completed",
        "failed",
        "cancelled",
        "needs_reconcile",
        "finished",
    }:
        _fail()
    fields = {
        "prepared": {"type", "operation_id", "plan_sha256", "target_root", "nodes"},
        "attempting": {"type", "node_key", "node_operation_id"},
        "completed": {"type", "node_key", "node_operation_id", "observed_sha256"},
        "failed": {"type", "node_key", "node_operation_id"},
        "cancelled": {"type"},
        "needs_reconcile": {"type", "node_key", "node_operation_id"},
        "finished": {"type"},
    }
    optional = {"target_sha256"} if event["type"] == "attempting" else set()
    if not fields[event["type"]] <= event.keys() or event.keys() - fields[event["type"]] - optional:
        _fail()
    if event["type"] == "prepared":
        _uuid(event["operation_id"])
        if type(event["nodes"]) is not list or not 1 <= len(event["nodes"]) <= 1024:
            _fail()
        if type(event["target_root"]) is not str or not Path(event["target_root"]).is_absolute():
            _fail()
        keys, ids = set(), set()
        for node in event["nodes"]:
            required = {
                "key",
                "target_id",
                "parent_target_id",
                "source_sha256",
                "target_sha256",
                "operation_id",
            }
            if (
                type(node) is not dict
                or not required <= node.keys()
                or node.keys() - required - {"title"}
            ):
                _fail()
            if "title" in node:
                from session_migrate.tree_model import validate_tree_title

                try:
                    validate_tree_title(node["title"])
                except ValueError:
                    _fail()
            for key, pattern in (
                ("key", _KEY),
                ("target_id", _ID),
                ("source_sha256", _SHA),
                ("target_sha256", _SHA),
            ):
                if type(node[key]) is not str or not pattern.fullmatch(node[key]):
                    _fail()
            if (
                node["key"] in keys
                or node["target_id"] in ids
                or node["parent_target_id"] is not None
                and (
                    type(node["parent_target_id"]) is not str or node["parent_target_id"] not in ids
                )
            ):
                _fail()
            if (
                type(node["operation_id"]) is not str
                or not node["operation_id"]
                or len(node["operation_id"]) > 128
                or not re.fullmatch(r"[A-Za-z0-9_-]+", node["operation_id"])
            ):
                _fail()
            keys.add(node["key"])
            ids.add(node["target_id"])
    for key, value in event.items():
        if key == "nodes":
            continue
        if type(value) is not str or not value or len(value) > 4096:
            _fail()
        if any(ord(char) < 32 for char in value):
            _fail()
        if key.endswith("sha256") and (
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        ):
            _fail()


class TreeJournal:
    """Exclusive FD-pinned writer; inspection acquires only a shared read lock."""

    def __init__(
        self, state_home: Path, operation_id: str, *, create: bool = False, readonly=False
    ):
        self.path = journal_path(state_home, operation_id)
        self._fds = []
        self._file = None
        self._readonly = readonly
        self._operation_id = operation_id
        self.events = []
        self._hash = _ZERO
        self._size = 0
        try:
            if create:
                ensure_migration_state_home(state_home)
            descriptor = os.open(self.path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._fds.append((descriptor, None, os.fstat(descriptor)))
            for part in self.path.parent.parts[1:]:
                parent = descriptor
                try:
                    descriptor = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o700, dir_fd=parent)
                    os.fsync(parent)
                    descriptor = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                self._fds.append((descriptor, part, os.fstat(descriptor)))
            directory = os.fstat(descriptor)
            if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
                _fail()
            flags = (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW | os.O_NONBLOCK
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            self._file = os.open(self.path.name, flags, 0o600, dir_fd=descriptor)
            info = os.fstat(self._file)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                _fail()
            self._identity = (info.st_dev, info.st_ino)
            self._revision = (info.st_mtime_ns, info.st_ctime_ns)
            fcntl.flock(self._file, (fcntl.LOCK_SH if readonly else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            self._load()
            self._check()
            if create:
                os.fsync(self._file)
                os.fsync(descriptor)
        except (OSError, ValueError, RecursionError):
            self.close()
            _fail()
        except BaseException:
            self.close()
            raise

    def _check(self):
        for index, (descriptor, name, expected) in enumerate(self._fds):
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                _fail()
            if (current.st_mode, current.st_uid) != (expected.st_mode, expected.st_uid):
                _fail()
            if index:
                named = os.stat(name, dir_fd=self._fds[index - 1][0], follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (expected.st_dev, expected.st_ino):
                    _fail()
        current = os.stat(self.path.name, dir_fd=self._fds[-1][0], follow_symlinks=False)
        if (
            (current.st_dev, current.st_ino) != self._identity
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_size != self._size
            or (current.st_mtime_ns, current.st_ctime_ns) != self._revision
        ):
            _fail()

    def _load(self):
        info = os.fstat(self._file)
        if info.st_size > MAX_JOURNAL_BYTES:
            _fail()
        data = os.pread(self._file, MAX_JOURNAL_BYTES + 1, 0)
        if len(data) != info.st_size or (data and not data.endswith(b"\n")):
            _fail()
        previous = _ZERO
        for index, line in enumerate(data.splitlines()):
            if index >= MAX_EVENTS:
                _fail()
            try:
                record = json.loads(line, object_pairs_hook=_pairs)
            except (ValueError, UnicodeError, RecursionError):
                _fail()
            if type(record) is not dict or set(record) != {
                "sequence",
                "previous",
                "event",
                "sha256",
            }:
                _fail()
            _safe_event(record["event"])
            digest = hashlib.sha256(
                _json({key: record[key] for key in ("sequence", "previous", "event")})
            ).hexdigest()
            if (
                type(record["sequence"]) is not int
                or record["sequence"] != index
                or record["previous"] != previous
                or record["sha256"] != digest
            ):
                _fail()
            if index == 0 and (
                record["event"]["type"] != "prepared"
                or record["event"]["operation_id"] != self._operation_id
            ):
                _fail()
            if index and record["event"]["type"] == "prepared":
                _fail()
            self.events.append(record["event"])
            previous = digest
        self._hash, self._size = previous, len(data)

    def append(self, event: dict) -> None:
        if self._readonly:
            _fail()
        _safe_event(event)
        self._check()
        if (not self.events) != (event["type"] == "prepared"):
            _fail()
        if not self.events and event["operation_id"] != self._operation_id:
            _fail()
        record = {"sequence": len(self.events), "previous": self._hash, "event": event}
        digest = hashlib.sha256(_json(record)).hexdigest()
        line = _json({**record, "sha256": digest}) + b"\n"
        if len(self.events) >= MAX_EVENTS or self._size + len(line) > MAX_JOURNAL_BYTES:
            _fail()
        try:
            offset = 0
            while offset < len(line):
                written = os.pwrite(self._file, line[offset:], self._size + offset)
                if written <= 0:
                    _fail()
                offset += written
            os.fsync(self._file)
        except OSError:
            _fail()
        self._size += len(line)
        info = os.fstat(self._file)
        self._revision = (info.st_mtime_ns, info.st_ctime_ns)
        self._hash = digest
        self.events.append(json.loads(_json(event)))
        self._check()

    def close(self):
        if self._file is not None:
            os.close(self._file)
            self._file = None
        for descriptor, _, _ in reversed(self._fds):
            os.close(descriptor)
        self._fds = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def inspect(self) -> dict:
        self._check()
        return _inspect(self, self._operation_id)


def inspect_tree(state_home: Path, operation_id: str) -> dict:
    """Read metadata and classify uncertainty; never observes/imports native data."""
    with TreeJournal(state_home, operation_id, readonly=True) as journal:
        return _inspect(journal, operation_id)


def _inspect(journal: TreeJournal, operation_id: str) -> dict:
    if not journal.events:
        _fail()
    prepared = journal.events[0]
    nodes = {node["key"]: {**node, "status": "unattempted"} for node in prepared["nodes"]}
    for event in journal.events[1:]:
        key = event.get("node_key")
        if key is None:
            continue
        if key not in nodes:
            _fail()
        previous = nodes[key]["status"]
        next_status = event["type"]
        if next_status == "attempting":
            if previous != "unattempted":
                _fail()
        elif (
            previous != "attempting"
            and not (previous == "completed" and next_status == "needs_reconcile")
        ) or nodes[key]["operation_id"] != event["node_operation_id"]:
            _fail()
        nodes[key].update(status=next_status, operation_id=event["node_operation_id"])
        if "target_sha256" in event:
            nodes[key]["target_sha256"] = event["target_sha256"]
        if "observed_sha256" in event:
            nodes[key]["observed_sha256"] = event["observed_sha256"]
    statuses = {node["status"] for node in nodes.values()}
    status = (
        "needs_reconcile"
        if statuses & {"attempting", "needs_reconcile"}
        else "completed"
        if statuses == {"completed"}
        else "cancelled"
        if journal.events[-1]["type"] == "cancelled"
        else "partial"
        if "completed" in statuses
        else "ready"
    )
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "status": status,
        "plan_sha256": prepared["plan_sha256"],
        "target_root": prepared["target_root"],
        "nodes": list(nodes.values()),
        "journal_path": str(journal.path),
    }
