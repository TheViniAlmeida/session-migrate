"""Readonly catalog captures never pass a filesystem path to SQLite."""

import errno
import hashlib
import os
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_lineage_cli as fixtures
from test_lineage_cli import publish, tree_state

from session_migrate.catalog import Catalog
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.errors import SessionMigrateError

private_env = fixtures.private_env


def test_readonly_exact_catalog_type_and_queries_never_touch_disk(
    tmp_path, private_env, monkeypatch
):
    fixture = publish(tmp_path, private_env)
    before = tree_state(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("readonly capture called writable initialization")

    monkeypatch.setattr(Catalog, "__init__", forbidden)
    monkeypatch.setattr(Catalog, "_initialize", forbidden)
    with Catalog.open_readonly_existing(fixture["catalog"]) as catalog:
        assert type(catalog) is Catalog
        assert len(catalog.roots()) == 2
        assert len(catalog.list_sessions()) == 2
        assert type(EndpointObserver(catalog, fixture["profiles"])) is EndpointObserver
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            catalog.add_root(fixture["profiles"][0].format, fixture["profiles"][0].root)
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("phase", ["open", "read", "connect", "deserialize", "execute", "fetch"])
@pytest.mark.parametrize("kind", ["parent", "ancestor", "aba"])
def test_complete_directory_binding_survives_no_observable_swaps(
    tmp_path, private_env, monkeypatch, phase, kind
):
    fixture = publish(tmp_path, private_env)
    path = fixture["catalog"]
    original = path.parent if kind != "ancestor" else tmp_path
    detached = original.with_name(original.name + "-detached")
    raw = path.read_bytes()
    marker, opened = [], []
    real_open, real_read, real_connect = os.open, os.read, sqlite3.connect

    def swap():
        if marker:
            return
        marker.append(True)
        original.rename(detached)
        if kind == "aba":
            detached.rename(original)
        else:
            original.mkdir(mode=0o700)

    def tracked_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        opened.append(fd)
        if phase == "open" and name == "catalog":
            swap()
        return fd

    def tracked_read(fd, size):
        data = real_read(fd, size)
        if phase == "read" and data.startswith(b"SQLite format 3\0"):
            swap()
        return data

    def tracked_connect(*args, **kwargs):
        factory = kwargs["factory"]

        class TrackedCursor(sqlite3.Cursor):
            def fetchall(self):
                result = super().fetchall()
                if phase == "fetch":
                    swap()
                return result

        class TrackedConnection(factory):
            def deserialize(self, *args, **kwargs):
                result = super().deserialize(*args, **kwargs)
                if phase == "deserialize":
                    swap()
                return result

            def cursor(self, factory=TrackedCursor):
                return super().cursor(factory)

            def execute(self, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                if phase == "execute":
                    swap()
                return result

        kwargs["factory"] = TrackedConnection
        result = real_connect(*args, **kwargs)
        if phase == "connect":
            swap()
        return result

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "read", tracked_read)
    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    with pytest.raises(SessionMigrateError, match="readonly_catalog_unavailable"):
        Catalog.open_readonly_existing(path)
    assert marker
    actual = (
        path
        if kind == "aba"
        else detached / path.name
        if kind == "parent"
        else detached / "catalog" / path.name
    )
    assert actual.read_bytes() == raw
    for fd in opened:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("kind", ["view", "generated", "virtual"])
def test_schema_objects_rejected_before_evaluation(tmp_path, private_env, monkeypatch, kind):
    fixture = publish(tmp_path, private_env)
    path = fixture["catalog"]
    with sqlite3.connect(path) as connection:
        connection.create_function("session_casefold", 1, str.casefold, deterministic=True)
        connection.execute("DROP TABLE catalog_meta")
        if kind == "view":
            connection.execute(
                "CREATE VIEW catalog_meta AS SELECT 'schema_version' AS key, "
                "session_casefold('4') AS value"
            )
        elif kind == "generated":
            connection.execute(
                "CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, "
                "value TEXT GENERATED ALWAYS AS(session_casefold('4')) VIRTUAL)"
            )
            connection.execute("INSERT INTO catalog_meta(key) VALUES('schema_version')")
        else:
            connection.execute("CREATE VIRTUAL TABLE catalog_meta USING fts5(key, value)")
    invoked, queries, settings = [], [], []
    real_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        factory = kwargs["factory"]

        class TrackedConnection(factory):
            def create_function(self, name, arity, function, **options):
                def recorded(value):
                    invoked.append(True)
                    return function(value)

                return super().create_function(name, arity, recorded, **options)

            def execute(self, sql, *args, **kwargs):
                queries.append(sql)
                return super().execute(sql, *args, **kwargs)

            def deserialize(self, *args, **kwargs):
                settings.extend(
                    [
                        self.execute("PRAGMA trusted_schema").fetchone()[0],
                        self.execute("PRAGMA query_only").fetchone()[0],
                    ]
                )
                return super().deserialize(*args, **kwargs)

        kwargs["factory"] = TrackedConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    before = tree_state(tmp_path)
    with pytest.raises(SessionMigrateError, match="readonly_catalog_unavailable"):
        Catalog.open_readonly_existing(path)
    assert settings == [0, 1] and invoked == []
    assert "SELECT value FROM catalog_meta WHERE key='schema_version'" not in queries
    assert tree_state(tmp_path) == before


def test_schema_budget_exhaustion_is_fixed_and_closes_all_fds(tmp_path, private_env, monkeypatch):
    fixture = publish(tmp_path, private_env)
    real_open = os.open
    opened = []

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", tracked_open)
    before = tree_state(tmp_path)
    with pytest.raises(SessionMigrateError) as caught:
        Catalog.open_readonly_existing(fixture["catalog"], max_query_steps=100)
    assert str(caught.value) == "readonly_catalog_unavailable"
    for fd in opened:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("method", ["execute", "fetchone", "fetchall", "fetchmany", "iteration"])
def test_snapshot_query_budget_exhaustion_is_fixed(tmp_path, private_env, method):
    fixture = publish(tmp_path, private_env)
    before = tree_state(tmp_path)
    with Catalog.open_readonly_existing(fixture["catalog"], max_query_steps=20_000) as catalog:
        assert isinstance(catalog._connection, sqlite3.Connection)
        assert catalog._connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            catalog._connection.execute("CREATE TABLE forbidden(id INTEGER)")
        sql = (
            "WITH RECURSIVE count(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM count "
            "WHERE x<1000000) SELECT " + ("sum(x)" if method == "execute" else "x") + " FROM count"
        )
        with pytest.raises(SessionMigrateError) as caught:
            cursor = catalog._connection.execute(sql)
            if method == "fetchone":
                while cursor.fetchone() is not None:
                    pass
            elif method == "fetchall":
                cursor.fetchall()
            elif method == "fetchmany":
                cursor.fetchmany(1000000)
            elif method == "iteration":
                list(cursor)
        assert str(caught.value) == "readonly_catalog_unavailable"
    assert tree_state(tmp_path) == before


def test_missing_catalog_does_not_create_any_directory(tmp_path, private_env):
    before = tree_state(tmp_path)
    with pytest.raises(SessionMigrateError, match="readonly_catalog_unavailable"):
        Catalog.open_readonly_existing(tmp_path / "absent" / "catalog.sqlite")
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "parent_symlink",
        "hardlink",
        "mode",
        "schema",
        "missing_column",
        "wal_header",
        "journal",
        "wal",
        "shm",
    ],
)
def test_unsafe_or_unsupported_catalog_rejected_without_mutation(tmp_path, private_env, kind):
    fixture = publish(tmp_path, private_env)
    path = fixture["catalog"]
    if kind == "symlink":
        link = path.with_name("link.sqlite")
        link.symlink_to(path)
        path = link
    elif kind == "parent_symlink":
        link = tmp_path / "linked-parent"
        link.symlink_to(path.parent, target_is_directory=True)
        path = link / path.name
    elif kind == "hardlink":
        os.link(path, path.with_name("hardlink.sqlite"))
    elif kind == "mode":
        path.chmod(0o644)
    elif kind == "schema":
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE catalog_meta SET value='3' WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=3")
    elif kind == "missing_column":
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE sessions RENAME COLUMN source_fingerprint TO removed")
    elif kind == "wal_header":
        image = bytearray(path.read_bytes())
        image[18:20] = b"\x02\x02"
        path.write_bytes(image)
    else:
        path.with_name(path.name + "-" + kind).write_bytes(b"private synthetic sidecar")
    before = tree_state(tmp_path)
    with pytest.raises(SessionMigrateError, match="readonly_catalog_unavailable"):
        Catalog.open_readonly_existing(path)
    assert tree_state(tmp_path) == before


def test_readonly_closes_memory_connection_and_all_file_descriptors_on_failure(
    tmp_path, private_env, monkeypatch
):
    import session_migrate.catalog as module

    fixture = publish(tmp_path, private_env)
    real_open = os.open
    descriptors, closed = [], []

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        descriptors.append(fd)
        return fd

    class BrokenConnection:
        def deserialize(self, image):
            raise sqlite3.DatabaseError("PRIVATE_DB_FAILURE")

        def close(self):
            closed.append(True)

    def memory_only(path, **kwargs):
        assert path == ":memory:"
        assert issubclass(kwargs["factory"], sqlite3.Connection)
        return BrokenConnection()

    monkeypatch.setattr(module.os, "open", tracked_open)
    monkeypatch.setattr(module.sqlite3, "connect", memory_only)
    before = tree_state(tmp_path)
    with pytest.raises(SessionMigrateError) as caught:
        Catalog.open_readonly_existing(fixture["catalog"])
    assert str(caught.value) == "readonly_catalog_unavailable"
    assert closed == [True] and descriptors
    for fd in descriptors:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF
    assert tree_state(tmp_path) == before


@contextmanager
def system_ancestor_child_churn(monkeypatch, catalog_path, *, after_publication=False):
    """Create only an owned empty /tmp sibling during this catalog's physical read."""
    from session_migrate import operations
    from session_migrate.catalog import _system_ancestor_allows_child_churn

    if Path("/tmp") not in catalog_path.absolute().parents:
        pytest.skip("catalog must be under the system /tmp ancestor")
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not _system_ancestor_allows_child_churn(Path("/tmp").stat(), root):
            pytest.skip("requires a non-renamable root-owned /tmp system ancestor")
    finally:
        os.close(root)
    info = catalog_path.stat()
    identity = (info.st_dev, info.st_ino)
    sibling = Path("/tmp") / ("session-migrate-owned-churn-" + uuid.uuid4().hex)
    state = {"published": not after_publication, "created": False}
    real_read, real_install = os.read, operations._install

    def observed_install(*args, **kwargs):
        result = real_install(*args, **kwargs)
        if args[-1] is False:
            state["published"] = True
        return result

    def observed_read(fd, size):
        data = real_read(fd, size)
        opened = os.fstat(fd)
        if (
            state["published"]
            and not state["created"]
            and (opened.st_dev, opened.st_ino) == identity
            and data.startswith(b"SQLite format 3\0")
        ):
            sibling.mkdir(mode=0o700)
            state["created"] = True
        return data

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "read", observed_read)
            if after_publication:
                patch.setattr(operations, "_install", observed_install)
            yield state
    finally:
        if state["created"]:
            sibling.rmdir()


def test_root_owned_system_ancestor_accepts_irrelevant_child_churn(private_env, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="session-migrate-catalog-churn-", dir="/tmp") as name:
        root = Path(name)
        fixture = publish(root, private_env)
        path = fixture["catalog"]
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with (
            system_ancestor_child_churn(monkeypatch, path) as state,
            Catalog.open_readonly_existing(path) as catalog,
        ):
            assert len(catalog.list_sessions()) == 2
        assert state["created"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "case,expected",
    [
        ("protected", True),
        ("euid_root", False),
        ("directory_user", False),
        ("parent_user", False),
        ("parent_group_write", False),
        ("parent_other_write", False),
        ("effective_write", False),
        ("access_ambiguous", False),
        ("access_error", False),
        ("parent_gid_changed", False),
        ("no_parent", False),
        ("not_directory", False),
        ("access_unsupported", False),
    ],
)
def test_system_ancestor_classification_fails_closed(monkeypatch, case, expected):
    from session_migrate import catalog as catalog_module
    from session_migrate.catalog import _system_ancestor_allows_child_churn

    monkeypatch.setattr(catalog_module, "_unprivileged_linux_caller", lambda: True)
    info = SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFDIR | 0o1777, st_uid=0, st_gid=0)
    parent = SimpleNamespace(st_dev=1, st_ino=1, st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0)
    if case == "directory_user":
        info.st_uid = 1000
    elif case == "parent_user":
        parent.st_uid = 1000
    elif case == "parent_group_write":
        parent.st_mode |= stat.S_IWGRP
    elif case == "parent_other_write":
        parent.st_mode |= stat.S_IWOTH
    elif case == "not_directory":
        info.st_mode = stat.S_IFREG | 0o600
    reads = []

    def access(path, mode, *, dir_fd, effective_ids):
        assert path == "." and mode == os.W_OK and dir_fd == 17 and effective_ids is True
        if case == "access_error":
            raise NotImplementedError
        return None if case == "access_ambiguous" else case == "effective_write"

    def fstat(fd):
        assert fd == 17
        reads.append(True)
        return (
            SimpleNamespace(**{**vars(parent), "st_gid": 1})
            if (case == "parent_gid_changed" and len(reads) > 1)
            else parent
        )

    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: 0 if case == "euid_root" else 1000)
        patch.setattr(os, "fstat", fstat)
        patch.setattr(os, "access", access)
        patch.setattr(os, "supports_dir_fd", {access})
        patch.setattr(
            os, "supports_effective_ids", set() if case == "access_unsupported" else {access}
        )
        result = _system_ancestor_allows_child_churn(info, None if case == "no_parent" else 17)
    assert result is expected


@pytest.mark.parametrize("kind", ["directory", "database"])
@pytest.mark.parametrize("field", ["st_uid", "st_gid", "st_mode"])
def test_readonly_rejects_identity_owner_group_or_permission_change_during_capture(
    tmp_path, private_env, monkeypatch, kind, field
):
    fixture = publish(tmp_path, private_env)
    path = fixture["catalog"]
    before = path.read_bytes()
    selected = path.parent if kind == "directory" else path
    info = selected.stat()
    identity = (info.st_dev, info.st_ino)
    real_read, real_fstat = os.read, os.fstat
    changed = []

    def read(fd, size):
        data = real_read(fd, size)
        if data.startswith(b"SQLite format 3\0"):
            changed.append(True)
        return data

    def fstat(fd):
        value = real_fstat(fd)
        if changed and (value.st_dev, value.st_ino) == identity:
            values = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
            values[field] = (
                values[field] ^ stat.S_IWOTH if field == "st_mode" else values[field] + 1
            )
            return SimpleNamespace(**values)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", read)
        patch.setattr(os, "fstat", fstat)
        with pytest.raises(SessionMigrateError, match="readonly_catalog_unavailable"):
            Catalog.open_readonly_existing(path)
    assert changed and path.read_bytes() == before


def test_readonly_rejects_database_row_mutation_during_capture(tmp_path, private_env, monkeypatch):
    fixture = publish(tmp_path, private_env)
    path = fixture["catalog"]
    real_read = os.read
    changed = []

    def read(fd, size):
        data = real_read(fd, size)
        if not changed and data.startswith(b"SQLite format 3\0"):
            changed.append(True)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE sessions SET display_title='synthetic mutation'")
        return data

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", read)
        with pytest.raises(SessionMigrateError, match="readonly_catalog_unavailable"):
            Catalog.open_readonly_existing(path)
    assert changed


@pytest.mark.parametrize(
    "case,expected",
    [
        ("unprivileged", True),
        ("effective_capability", False),
        ("permitted_capability", False),
        ("inherited_capability", False),
        ("ambient_capability", False),
        ("saved_root", False),
        ("real_root", False),
        ("euid_root", False),
        ("uid_flip", False),
        ("duplicate", False),
        ("missing", False),
        ("malformed", False),
        ("oversized", False),
        ("unavailable", False),
        ("not_linux", False),
        ("fsuid_root", False),
        ("fsuid_other", False),
        ("uid_missing", False),
        ("uid_duplicate", False),
        ("uid_malformed", False),
        ("uid_leading_zero", False),
        ("uid_oversized", False),
    ],
)
def test_system_ancestor_requires_unprivileged_linux_caller(monkeypatch, case, expected):
    from session_migrate import catalog as catalog_module

    fields = {b"CapInh": b"0", b"CapPrm": b"0", b"CapEff": b"0", b"CapAmb": b"0"}
    selected = {
        "effective_capability": b"CapEff",
        "permitted_capability": b"CapPrm",
        "inherited_capability": b"CapInh",
        "ambient_capability": b"CapAmb",
    }.get(case)
    if selected is not None:
        fields[selected] = b"1"
    if case == "missing":
        fields.pop(b"CapAmb")
    if case == "malformed":
        fields[b"CapEff"] = b"not-hex"
    metadata = b"Name:\tsynthetic\n" + b"\n".join(k + b":\t" + v for k, v in fields.items())
    metadata += b"\nCapBnd:\tffffffffffffffff\n"
    uid_line = {
        "fsuid_root": b"Uid:\t1000 1000 1000 0\n",
        "fsuid_other": b"Uid:\t1000 1000 1000 2000\n",
        "uid_malformed": b"Uid:\t1000 1000 1000 not-decimal\n",
        "uid_leading_zero": b"Uid:\t1000 1000 1000 01000\n",
        "uid_oversized": b"Uid:\t1000 1000 1000 4294967296\n",
    }.get(case, b"Uid:\t1000 1000 1000 1000\n")
    if case != "uid_missing":
        metadata += uid_line
    if case == "uid_duplicate":
        metadata += uid_line
    if case == "duplicate":
        metadata += b"CapEff:\t0\n"
    if case == "oversized":
        metadata += b"X" * (64 * 1024)
    reads = []

    def uid():
        reads.append(True)
        if case == "saved_root":
            return (1000, 1000, 0)
        if case == "real_root":
            return (0, 1000, 1000)
        if case == "uid_flip" and len(reads) > 1:
            return (1001, 1001, 1001)
        return (1000, 1000, 1000)

    def read_metadata():
        if case == "unavailable":
            raise PermissionError
        return metadata

    monkeypatch.setattr(catalog_module.sys, "platform", "linux" if case != "not_linux" else "other")
    monkeypatch.setattr(os, "getresuid", uid)
    monkeypatch.setattr(os, "geteuid", lambda: 0 if case == "euid_root" else 1000)
    monkeypatch.setattr(catalog_module, "_linux_caller_privilege_metadata", read_metadata)
    assert catalog_module._unprivileged_linux_caller() is expected


@pytest.mark.parametrize("flip", ["capability", "fsuid"])
def test_privilege_flip_during_capture_keeps_system_ancestor_strict(private_env, monkeypatch, flip):
    from session_migrate import catalog as catalog_module

    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not catalog_module._system_ancestor_allows_child_churn(Path("/tmp").stat(), parent):
            pytest.skip("requires a non-renamable root-owned /tmp system ancestor")
    finally:
        os.close(parent)
    with tempfile.TemporaryDirectory(prefix="session-migrate-cap-flip-", dir="/tmp") as name:
        fixture = publish(Path(name), private_env)
        path = fixture["catalog"]
        original = path.read_bytes()
        privileged = []
        real_read = os.read
        uid = os.geteuid()

        def read(fd, size):
            data = real_read(fd, size)
            if data.startswith(b"SQLite format 3\0"):
                privileged.append(True)
            return data

        def metadata():
            capability = int(bool(privileged) and flip == "capability")
            fsuid = 0 if privileged and flip == "fsuid" else uid
            return (
                f"Uid:\t{uid} {uid} {uid} {fsuid}\n"
                f"CapInh:\t0\nCapPrm:\t{capability:x}\nCapEff:\t{capability:x}\nCapAmb:\t0\n"
            ).encode()

        with monkeypatch.context() as patch:
            patch.setattr(os, "read", read)
            patch.setattr(catalog_module, "_linux_caller_privilege_metadata", metadata)
            with pytest.raises(SessionMigrateError, match="readonly_catalog_unavailable"):
                Catalog.open_readonly_existing(path)
        assert privileged and path.read_bytes() == original
