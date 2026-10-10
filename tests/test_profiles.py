"""Profiles operate only on synthetic transcripts and disposable stores."""

import json
import os
import shutil
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate import operations, profiles
from session_migrate.catalog import Catalog
from session_migrate.conversion import ConversionOptions, convert_session, load_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import OperationKind, OperationRequest, run_operation
from session_migrate.profiles import (
    Profile,
    ProfileStore,
    SessionRef,
    list_profile_sessions,
    profile_from_catalog_root,
    read_session,
    resolve_source,
    run_profile_operation,
)

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
SOURCE_ID = load_session(FIXTURE, AgentFormat.CLAUDE).session_id
TARGET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NATIVE_ID = "ses_33333333333343338333333333333333"


def claude_root(root, title="Synthetic title", session_id=SOURCE_ID):
    path = root / "projects" / "-synthetic" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    for row in rows:
        if "sessionId" in row:
            row["sessionId"] = session_id
    rows.append({"type": "custom-title", "sessionId": session_id, "customTitle": title})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def profile(tmp_path, format=AgentFormat.CLAUDE, id="synthetic", **kwargs):
    return Profile(
        id,
        id,
        format,
        tmp_path / id / format.value,
        environ={"HOME": str(tmp_path / id / "home")},
        **kwargs,
    )


def environment(tmp_path):
    return {
        "HOME": str(tmp_path / "base-home"),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "XDG_CONFIG_HOME": str(tmp_path / "base-config"),
    }


def test_profiles_are_ephemeral_and_immutable_and_home_is_not_cwd(tmp_path):
    before = dict(os.environ)
    overrides = {"HOME": str(tmp_path / "home")}
    selected = Profile("example", "Example", "claude", tmp_path / "root", environ=overrides)
    overrides["HOME"] = str(tmp_path / "changed")
    runtime = selected.environment({"HOME": "/synthetic/base", "XDG_CONFIG_HOME": "/wrong/config"})
    assert runtime["HOME"] == str(tmp_path / "home")
    assert runtime["XDG_CONFIG_HOME"] == str(tmp_path / "home/.config")
    assert Path.cwd() != Path(runtime["HOME"])
    runtime["HOME"] = "/synthetic/runtime-only"
    assert selected.environ["HOME"] == str(tmp_path / "home")
    with pytest.raises(TypeError):
        selected.environ["HOME"] = "/synthetic/forbidden"
    assert os.environ == before
    assert not selected.root.exists()
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"API_KEY": "SYNTHETIC"},
        {"HOME": "relative"},
        {"XDG_DATA_HOME": "~/data"},
        {"HOME": 1},
        {"HOME": "/synthetic/\ninvalid"},
    ],
)
def test_rejects_non_path_environment_and_secret_names(tmp_path, overrides):
    with pytest.raises(SessionMigrateError, match="environment|HOME/XDG"):
        Profile("test", "Test", "claude", tmp_path / "root", environ=overrides)


@pytest.mark.parametrize(
    "options",
    [
        {"id": "../escape"},
        {"name": ""},
        {"format": "unknown"},
        {"root": "relative"},
        {"cli": "relative"},
    ],
)
def test_invalid_profiles_fail_explicitly(tmp_path, options):
    values = {"id": "test", "name": "Test", "format": "claude", "root": tmp_path / "root"}
    with pytest.raises(SessionMigrateError):
        Profile(**(values | options))


def test_profile_store_private_roundtrip_no_overwrite_or_implicit_directory(tmp_path):
    selected = profile(tmp_path)
    store = ProfileStore(tmp_path / "private" / "profiles.json")
    with pytest.raises(SessionMigrateError, match="directory"):
        store.load()
    assert not store.path.parent.exists()
    store.create([selected])
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.get(selected.id).to_dict() == selected.to_dict()
    snapshot = store.path.read_bytes()
    with pytest.raises(SessionMigrateError, match="no overwrite"):
        store.create([replace(selected, name="Changed")])
    assert store.path.read_bytes() == snapshot
    with pytest.raises(SessionMigrateError, match="not found"):
        store.get("absent")


@pytest.mark.parametrize("duplicate", ["id", "name"])
def test_store_rejects_duplicate_profiles_before_writing(tmp_path, duplicate):
    first = profile(tmp_path, id="one")
    second = replace(first, id="two", name="two")
    second = replace(second, **{duplicate: getattr(first, duplicate)})
    store = ProfileStore(tmp_path / "private/profiles.json")
    with pytest.raises(SessionMigrateError, match="duplicate"):
        store.create([first, second])
    assert not store.path.parent.exists()


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("directory_mode", "0700"),
        ("file_mode", "0600"),
        ("directory_symlink", "symlink"),
        ("file_symlink", "symlink"),
        ("hardlink", "0600"),
        ("owner", "owned"),
    ],
)
def test_profile_storage_security(tmp_path, monkeypatch, mutation, match):
    store = ProfileStore(tmp_path / "private/profiles.json")
    store.create([profile(tmp_path)])
    if mutation == "directory_mode":
        store.path.parent.chmod(0o755)
    elif mutation == "file_mode":
        store.path.chmod(0o644)
    elif mutation == "directory_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(store.path.parent, target_is_directory=True)
        store = ProfileStore(alias / store.path.name)
    elif mutation == "file_symlink":
        alias = store.path.parent / "alias.json"
        alias.symlink_to(store.path)
        store = ProfileStore(alias)
    elif mutation == "hardlink":
        os.link(store.path, store.path.parent / "linked.json")
    else:
        monkeypatch.setattr(profiles.os, "getuid", lambda: store.path.stat().st_uid + 1)
    with pytest.raises(SessionMigrateError, match=match):
        store.load()


@pytest.mark.parametrize(
    "document,match",
    [
        ({"schema_version": 2, "profiles": []}, "version"),
        ({"schema_version": True, "profiles": []}, "version"),
        ({"schema_version": 1, "profiles": "bad"}, "list"),
        ({"schema_version": 1, "profiles": [{}]}, "row"),
        ({"schema_version": 1, "profiles": [], "extra": "bad"}, "schema"),
    ],
)
def test_profile_store_rejects_unknown_schema(tmp_path, document, match):
    store = ProfileStore(tmp_path / "private/profiles.json")
    store.create([])
    store.path.write_text(json.dumps(document))
    with pytest.raises(SessionMigrateError, match=match):
        store.load()


def test_profile_store_rejects_duplicate_json_keys_and_oversized_file(tmp_path):
    store = ProfileStore(tmp_path / "private/profiles.json")
    store.create([])
    store.path.write_text('{"schema_version":1,"schema_version":1,"profiles":[]}')
    with pytest.raises(SessionMigrateError, match="JSON"):
        store.load()
    store.path.write_bytes(b" " * 1_048_577)
    with pytest.raises(SessionMigrateError, match="size"):
        store.load()


def test_same_native_id_two_roots_titles_filters_pagination_and_identity(tmp_path):
    first, second = profile(tmp_path, id="one"), profile(tmp_path, id="two")
    claude_root(first.root, "First root title")
    second_path = claude_root(second.root, "Second root title")
    extra_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    extra_path = claude_root(second.root, "Older session", extra_id)
    os.utime(extra_path, ns=(1, 1))
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(first.format, first.root)
        catalog.add_root(second.format, second.root)
        catalog.refresh(include_auto=False)
        page = list_profile_sessions(catalog, second, limit=1)
        assert page[0].entry.title == "Second root title"
        assert page[0].entry.duplicate
        assert page[0].ordering_source == "filesystem_mtime"
        assert page[0].started_at_source == "native_metadata"
        assert (
            list_profile_sessions(catalog, second, limit=1, offset=1)[0].entry.title
            == "Older session"
        )
        assert list_profile_sessions(catalog, first, query="Second") == []
        assert (
            list_profile_sessions(
                catalog, second, statuses=("candidate",), since="2099-01-01T00:00:00Z"
            )
            == []
        )
        ref = page[0].ref
        direct = SessionRef(second.format, second.root, SOURCE_ID, second.id)
        assert resolve_source(catalog, second, direct).session_id == SOURCE_ID
        assert read_session(catalog, second, ref).title == "Second root title"
        with pytest.raises(SessionMigrateError, match="profile/root/format"):
            read_session(catalog, first, ref)
        forged = replace(
            ref, root=first.root, profile_id=first.id, profile_fingerprint=first.fingerprint
        )
        with pytest.raises(SessionMigrateError, match="another root"):
            resolve_source(catalog, first, forged)
        changed = replace(second, environ={"HOME": str(tmp_path / "changed-home")})
        with pytest.raises(SessionMigrateError, match="CLI/environment changed"):
            read_session(catalog, changed, ref)
        second_path.unlink()
        with pytest.raises(SessionMigrateError, match="missing"):
            read_session(catalog, second, ref)
        catalog.refresh(include_auto=False)
        missing = list_profile_sessions(catalog, second, statuses=("missing",))
        assert len(missing) == 1
        with pytest.raises(SessionMigrateError, match="missing"):
            read_session(catalog, second, missing[0].ref)


def test_source_is_reread_and_identity_revalidated(tmp_path):
    selected = profile(tmp_path)
    path = claude_root(selected.root)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(selected.format, selected.root)
        catalog.refresh(include_auto=False)
        ref = list_profile_sessions(catalog, selected)[0].ref
        text = path.read_text()
        path.write_text(text.replace(SOURCE_ID, TARGET_ID))
        with pytest.raises(SessionMigrateError, match="identity"):
            read_session(catalog, selected, ref)


def test_root_import_requires_exact_format_and_root_exists(tmp_path):
    selected = profile(tmp_path)
    claude_root(selected.root)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        root = catalog.add_root(selected.format, selected.root)
        imported = profile_from_catalog_root(
            root, profile_id="known", name="Known", agent_format="claude", environ={}
        )
        assert imported.root == selected.root
        with pytest.raises(SessionMigrateError, match="format"):
            profile_from_catalog_root(
                root, profile_id="wrong", name="Wrong", agent_format="codex", environ={}
            )
        missing = catalog.add_root(AgentFormat.CLAUDE, tmp_path / "absent")
        with pytest.raises(SessionMigrateError, match="missing"):
            profile_from_catalog_root(
                missing, profile_id="absent", name="Absent", agent_format="claude", environ={}
            )


def test_profile_transfer_uses_target_root_and_cwd_without_copying_source_config(tmp_path):
    source = profile(tmp_path, id="source")
    target = profile(tmp_path, format=AgentFormat.CODEX, id="target")
    source_path = claude_root(source.root)
    snapshot = source_path.read_bytes()
    config = Path(source.environ["HOME"]) / ".config/synthetic.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"synthetic": true}')
    project = tmp_path / "project"
    request = OperationRequest(OperationKind.TRANSFER, session_id=TARGET_ID, cwd=project)
    before = dict(os.environ)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(source.format, source.root)
        catalog.refresh(include_auto=False)
        ref = list_profile_sessions(catalog, source)[0].ref
        result = run_profile_operation(
            catalog, source, ref, target, request, base_environ=environment(tmp_path)
        )
        parsed = load_session(Path(result.output), AgentFormat.CODEX)
        assert parsed.cwd == project
        assert Path(result.output).is_relative_to(target.root)
        assert source_path.read_bytes() == snapshot
        assert not (Path(target.environ["HOME"]) / ".config/synthetic.json").exists()
        with pytest.raises(SessionMigrateError, match="selectors"):
            run_profile_operation(
                catalog, source, ref, target, replace(request, source_id=SOURCE_ID)
            )
    assert dict(os.environ) == before


def virtual_root(root, format, title):
    root.mkdir(parents=True)
    with sqlite3.connect(root / f"{format.value}.db") as connection:
        connection.executescript("""
            CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
              slug TEXT, directory TEXT, title TEXT, version TEXT, time_created INTEGER,
              time_updated INTEGER, time_archived INTEGER);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
        """)
        pin = "1.17.20" if format == AgentFormat.OPENCODE else "7.5.0"
        connection.execute(
            "INSERT INTO session VALUES (?, 'global', NULL, 'synthetic', ?, ?, ?, "
            "1787054400000, 1787054404000, NULL)",
            (NATIVE_ID, str(root.parent), title, pin),
        )


@pytest.mark.parametrize("format", [AgentFormat.OPENCODE, AgentFormat.KILO])
def test_two_virtual_stores_use_exact_source_cli_config_and_target_environment(
    tmp_path, monkeypatch, format
):
    executable = tmp_path / "cli"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    source = profile(tmp_path, format=format, id="source", cli=executable)
    other = profile(tmp_path, format=format, id="other", cli=executable)
    target = profile(tmp_path, format=AgentFormat.CODEX, id="target")
    virtual_root(source.root, format, "Chosen native title")
    virtual_root(other.root, format, "Different native title")
    loaded = replace(
        load_session(FIXTURE, AgentFormat.CLAUDE), source_format=format, session_id=NATIVE_ID
    )
    observations = []

    def export(native_id, *, source_cli, environ):
        assert native_id == NATIVE_ID
        assert source_cli == executable
        assert Path(environ["XDG_DATA_HOME"]) / format.value == source.root
        assert environ["HOME"] == source.environ["HOME"]
        assert environ["XDG_CONFIG_HOME"] == str(Path(source.environ["HOME"]) / ".config")
        observations.append(dict(environ))
        return loaded

    monkeypatch.setattr(operations, f"load_{format.value}_session", export)
    monkeypatch.setattr(profiles, f"load_{format.value}_session", export)
    base = environment(tmp_path)
    snapshot = dict(base)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(format, source.root)
        catalog.add_root(format, other.root)
        catalog.refresh(include_auto=False)
        item = list_profile_sessions(catalog, source)[0]
        assert item.entry.title == "Chosen native title"
        assert item.ordering_source == "native_time_updated_or_database_mtime"
        assert read_session(catalog, source, item.ref, base_environ=base).session_id == NATIVE_ID
        result = run_profile_operation(
            catalog,
            source,
            item.ref,
            target,
            OperationRequest("transfer", session_id=TARGET_ID),
            base_environ=base,
        )
        assert Path(result.output).is_relative_to(target.root)
        assert len(observations) == 2
        bad = replace(
            source,
            environ={"HOME": source.environ["HOME"], "XDG_DATA_HOME": str(other.root.parent)},
        )
        with pytest.raises(SessionMigrateError, match="does not match"):
            read_session(
                catalog, bad, replace(item.ref, profile_fingerprint=None), base_environ=base
            )
    assert base == snapshot


def test_source_environ_expands_source_paths_and_target_paths_independently(tmp_path):
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "source.jsonl").write_bytes(FIXTURE.read_bytes())
    target_home = tmp_path / "target-home"
    target_home.mkdir()
    source_env, target_env = {"HOME": str(source_home)}, {"HOME": str(target_home)}
    result = run_operation(
        OperationRequest(
            "convert",
            path=Path("~/source.jsonl"),
            source_format="claude",
            target_format="codex",
            output=Path("~/result.jsonl"),
        ),
        environ=target_env,
        source_environ=source_env,
    )
    assert Path(result.output) == target_home / "result.jsonl"
    assert source_env == {"HOME": str(source_home)}
    assert target_env == {"HOME": str(target_home)}


def test_profile_stock_opencode_two_stores_exact_native_id(tmp_path):
    candidate = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V2") or shutil.which("opencode")
    if not candidate:
        pytest.skip("native profile oracle requires $OPENCODE_BIN (stock OpenCode)")
    executable = Path(candidate)
    base = environment(tmp_path)
    from session_migrate.conversion import detect_opencode_version, install_opencode_artifact

    release = detect_opencode_version(executable, environ=base)
    if release not in {"2.0.22", "2.0.23"}:
        pytest.skip("native profile oracle requires stock OpenCode 2.0.22/2.0.23")
    native_id = opencode.session_id_from_uuid(TARGET_ID)
    first = profile(tmp_path, format=AgentFormat.OPENCODE, id="first", cli=executable)
    second = profile(tmp_path, format=AgentFormat.OPENCODE, id="second", cli=executable)
    original = load_session(FIXTURE, AgentFormat.CLAUDE)
    for selected, title in [
        (first, "First isolated native title"),
        (second, "Second isolated native title"),
    ]:
        values = selected.environment(base)
        values["XDG_DATA_HOME"] = str(selected.root.parent)
        artifact = convert_session(
            replace(original, title=title),
            ConversionOptions(
                target_format=TargetFormat.OPENCODE,
                session_id=TARGET_ID,
                cwd=tmp_path,
                target_cli_version=release,
                model_provider="synthetic",
                model="offline",
            ),
        )
        install_opencode_artifact(
            artifact,
            target_cli=executable,
            environ=values,
            manifest_path=Path(values["XDG_STATE_HOME"]) / "receipt.json",
        )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        for selected in (first, second):
            catalog.add_root(selected.format, selected.root)
        catalog.refresh(include_auto=False)
        for selected, title in [
            (first, "First isolated native title"),
            (second, "Second isolated native title"),
        ]:
            item = list_profile_sessions(catalog, selected)[0]
            assert item.ref.native_id == native_id
            assert item.entry.title == title
            assert read_session(catalog, selected, item.ref, base_environ=base).title == title


def test_archived_and_unsupported_subagent_statuses_remain_visible(tmp_path):
    codex = profile(tmp_path, format=AgentFormat.CODEX, id="archive")
    source = Path(__file__).parent / "fixtures/codex-0.144.4/basic.jsonl"
    parsed = load_session(source, AgentFormat.CODEX)
    path = codex.root / "archived_sessions" / f"rollout-synthetic-{parsed.session_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(source.read_bytes())
    claude = profile(tmp_path, id="sidechain")
    side_path = claude_root(claude.root)
    rows = [json.loads(line) for line in side_path.read_text().splitlines()]
    for row in rows:
        if row.get("type") in {"user", "assistant"}:
            row["isSidechain"] = True
    side_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(codex.format, codex.root)
        catalog.add_root(claude.format, claude.root)
        catalog.refresh(include_auto=False)
        archived = list_profile_sessions(catalog, codex, lifecycles=("archived",))
        assert len(archived) == 1
        assert read_session(catalog, codex, archived[0].ref).session_id == parsed.session_id
        unsupported = list_profile_sessions(catalog, claude, statuses=("unsupported",))
        assert len(unsupported) == 1
        assert unsupported[0].entry.reason == "claude_sidechain"
        with pytest.raises(
            SessionMigrateError, match="not transferable.*unsupported.*claude_sidechain"
        ):
            read_session(catalog, claude, unsupported[0].ref)


def test_source_environ_supplies_shared_source_home_and_source_catalog_default(
    tmp_path, monkeypatch
):
    source_env = {"HOME": str(tmp_path / "source-home"), "PATH": os.defpath}
    target_env = {"HOME": str(tmp_path / "target-home"), "PATH": os.defpath}
    loaded = replace(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        source_format=AgentFormat.HERMES,
        session_id="20261010_120000_abcdef",
    )
    observed = []

    def load(format, session_id, source_home):
        assert format == AgentFormat.HERMES
        assert session_id == "20261010_120000_abcdef"
        observed.append(source_home)
        return loaded

    monkeypatch.setattr(operations, "_load_shared_database_source", load)
    result = run_operation(
        OperationRequest(
            "transfer",
            source_format="hermes",
            source_id="20261010_120000_abcdef",
            target_format="claude",
            dry_run=True,
        ),
        environ=target_env,
        source_environ=source_env,
    )
    assert observed == [Path(source_env["HOME"]) / ".hermes"]
    assert Path(result.output).is_relative_to(Path(target_env["HOME"]) / ".claude")
    source_profile = profile(tmp_path, id="source")
    claude_root(source_profile.root)
    source_catalog = Path(source_env["HOME"]) / ".local/state/session-migrate/catalog.sqlite3"
    with Catalog(source_catalog) as catalog:
        catalog.add_root(source_profile.format, source_profile.root)
        catalog.refresh(include_auto=False)
        catalog_id = catalog.list_sessions()[0].catalog_id
    result = run_operation(
        OperationRequest("transfer", catalog_id=catalog_id, target_format="codex", dry_run=True),
        environ=target_env,
        source_environ=source_env,
    )
    assert Path(result.output).is_relative_to(Path(target_env["HOME"]) / ".codex")
    assert not (Path(target_env["HOME"]) / ".local/state/session-migrate/catalog.sqlite3").exists()


@pytest.mark.parametrize("explicit_cli", [False, True])
def test_source_environ_controls_native_cli_path_and_lookup(tmp_path, monkeypatch, explicit_cli):
    source_home = tmp_path / "source-home"
    source_env = {
        "HOME": str(source_home),
        "OPENCODE_BIN": "~/native-cli",
        "XDG_CONFIG_HOME": str(tmp_path / "source-config"),
    }
    target_env = {
        "HOME": str(tmp_path / "target-home"),
        "OPENCODE_BIN": "/wrong/target-cli",
        "XDG_CONFIG_HOME": str(tmp_path / "target-config"),
    }
    loaded = replace(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        source_format=AgentFormat.OPENCODE,
        session_id=NATIVE_ID,
    )

    def export(native_id, *, source_cli, environ):
        assert native_id == NATIVE_ID
        assert source_cli == (source_home / "explicit-cli" if explicit_cli else None)
        assert environ["OPENCODE_BIN"] == str(source_home / "native-cli")
        assert environ["XDG_CONFIG_HOME"] == source_env["XDG_CONFIG_HOME"]
        return loaded

    monkeypatch.setattr(operations, "load_opencode_session", export)
    result = run_operation(
        OperationRequest(
            "transfer",
            source_format="opencode",
            source_id=NATIVE_ID,
            target_format="claude",
            source_cli=Path("~/explicit-cli") if explicit_cli else None,
            dry_run=True,
        ),
        environ=target_env,
        source_environ=source_env,
    )
    assert Path(result.output).is_relative_to(Path(target_env["HOME"]) / ".claude")


def test_source_environ_controls_discovery_cwd_home_and_temporary_root(tmp_path, monkeypatch):
    source_temp, target_temp = tmp_path / "source-temp", tmp_path / "target-temp"
    source_temp.mkdir()
    target_temp.mkdir()
    source_home = tmp_path / "source-home"
    target_home = tmp_path / "target-home"
    observed = []

    def locate(format, native_id, home, *, cwd, user_home, temp_root):
        assert format == AgentFormat.CLAUDE
        assert native_id == SOURCE_ID
        observed.append((home, cwd, user_home, temp_root))
        return FIXTURE

    monkeypatch.setattr(operations, "locate_session", locate)
    result = run_operation(
        OperationRequest(
            "transfer",
            source_format="claude",
            source_id=SOURCE_ID,
            source_home=Path("~/root"),
            source_cwd=Path("~/project"),
            target_format="codex",
            cwd=Path("~/target-project"),
            dry_run=True,
        ),
        source_environ={"HOME": str(source_home), "TMPDIR": str(source_temp)},
        environ={"HOME": str(target_home), "TMPDIR": str(target_temp)},
    )
    assert observed == [(source_home / "root", source_home / "project", source_home, source_temp)]
    assert result.cwd == str(target_home / "target-project")


@pytest.mark.parametrize("target_existing", [False, True])
def test_mastracode_database_file_profile_import_read_and_transfer_exact_store(
    tmp_path, target_existing
):
    from session_migrate.conversion import install_mastracode_artifact
    from session_migrate.formats import mastracode

    base = environment(tmp_path)
    database = tmp_path / "source-db" / "mastra.db"
    selected = Profile(
        "mastra-file",
        "Mastra file",
        AgentFormat.MASTRACODE,
        database,
        environ={"HOME": str(tmp_path / "source-home")},
    )
    artifact = convert_session(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        ConversionOptions(
            target_format=TargetFormat.MASTRACODE, session_id=TARGET_ID, cwd=tmp_path
        ),
        environ=base,
    )
    install_mastracode_artifact(artifact, target_home=database)
    snapshot = database.read_bytes()
    target = profile(tmp_path, format=AgentFormat.CODEX, id="codex-target")
    target_database = tmp_path / "target-db" / "mastra.db"
    mastra_target = Profile(
        "mastra-target",
        "Mastra target",
        AgentFormat.MASTRACODE,
        target_database,
        environ={"HOME": str(tmp_path / "target-home")},
    )
    if target_existing:
        install_mastracode_artifact(artifact, target_home=target_database)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        root = catalog.add_root(selected.format, database)
        assert catalog.refresh(include_auto=False).root_errors == 0
        imported = profile_from_catalog_root(
            root,
            profile_id=selected.id,
            name=selected.name,
            agent_format=selected.format,
            environ=selected.environ,
        )
        item = list_profile_sessions(catalog, imported)[0]
        assert item.ref.root == database
        assert item.ref.native_id == TARGET_ID
        assert read_session(catalog, imported, item.ref, base_environ=base).session_id == TARGET_ID
        result = run_profile_operation(
            catalog,
            imported,
            item.ref,
            target,
            OperationRequest("transfer", session_id=SOURCE_ID),
            base_environ=base,
        )
        assert result.source_format == "mastracode"
        assert load_session(Path(result.output), AgentFormat.CODEX).session_id == SOURCE_ID
        transferred = run_profile_operation(
            catalog,
            imported,
            item.ref,
            mastra_target,
            OperationRequest("transfer", session_id=SOURCE_ID),
            base_environ=base,
        )
        assert transferred.output == f"mastracode:{SOURCE_ID}"
        assert Path(transferred.manifest).is_relative_to(target_database.parent)
        assert target_database.is_file()
        assert mastracode.parse_session(target_database, SOURCE_ID).session_id == SOURCE_ID
        assert database.read_bytes() == snapshot
    assert not (database / "mastra.db").exists()
    assert not (target_database / "mastra.db").exists()
    assert not (tmp_path / "mastra.db").exists()


def test_profile_noncanonical_database_files_fail_before_sibling_io(tmp_path):
    selected = profile(tmp_path, id="source")
    claude_root(selected.root)
    invalid = tmp_path / "noncanonical.db"
    invalid.write_bytes(b"synthetic non-database file")
    target = Profile("bad-mastra", "Bad Mastra", AgentFormat.MASTRACODE, invalid)
    snapshot = invalid.read_bytes()
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        bad_root = catalog.add_root(AgentFormat.MASTRACODE, invalid)
        with pytest.raises(SessionMigrateError, match="canonical MastraCode mastra.db"):
            profile_from_catalog_root(
                bad_root,
                profile_id="bad-source",
                name="Bad source",
                agent_format=AgentFormat.MASTRACODE,
                environ={},
            )
        with pytest.raises(SessionMigrateError, match="canonical MastraCode mastra.db"):
            list_profile_sessions(catalog, target)
        catalog.add_root(selected.format, selected.root)
        catalog.refresh(include_auto=False)
        ref = list_profile_sessions(catalog, selected)[0].ref
        with pytest.raises(SessionMigrateError, match="canonical MastraCode mastra.db"):
            run_profile_operation(
                catalog, selected, ref, target, OperationRequest("transfer", session_id=TARGET_ID)
            )
    assert invalid.read_bytes() == snapshot
    assert not (tmp_path / "mastra.db").exists()
    assert not (invalid / "mastra.db").exists()
