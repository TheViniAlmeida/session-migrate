"""Readonly catalog captures never pass a filesystem path to SQLite."""

import errno
import os
import sqlite3

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
