"""Shared operation contracts using only synthetic transcripts and disposable stores."""

import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate import conversion, operations
from session_migrate.catalog import Catalog
from session_migrate.cli import main
from session_migrate.errors import (
    JsonlError,
    OperationNeedsReconcileError,
    SessionMigrateError,
)
from session_migrate.formats import antigravity, claude, cursor, hermes, mastracode
from session_migrate.lineage import EvidenceReference, NativeState, ReconcileReceipt
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import (
    OperationKind,
    OperationRequest,
    ProgressEvent,
    ProgressStage,
    run_operation,
)

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
TARGET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.parametrize("native_state", list(NativeState))
def test_cli_generic_reconciliation_keeps_typed_state_and_reference_availability(
    tmp_path, monkeypatch, capsys, native_state
):
    from session_migrate import cli
    from session_migrate.native_namespaces import reserve_namespace

    namespace = reserve_namespace(AgentFormat.CODEX, tmp_path / "native", TARGET_ID)
    receipt = ReconcileReceipt(
        1,
        "synthetic-operation",
        TARGET_ID,
        "installing",
        "native_result_unknown",
        native_state,
        (EvidenceReference("manifest", "planned", "unknown", str(tmp_path / "manifest"), None),),
        False,
    )

    def fail(operation):
        raise OperationNeedsReconcileError(receipt, target_namespace=namespace)

    monkeypatch.setattr(cli, "run_operation", fail)
    assert main(["import", str(FIXTURE), "--to", "codex"]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.err)
    assert captured.out == ""
    assert result["operation_id"] == "synthetic-operation"
    assert result["installation_state"] == native_state.value
    assert result["root"] == str(tmp_path / "native")
    assert result["target_id"] == TARGET_ID and result["target_format"] == "codex"
    assert result["references"][0]["availability"] == "unknown"
    assert result["references"][0]["stage"] == "planned"
    assert "import_succeeded" not in result and "published" not in result["action"].lower()


def request(tmp_path: Path, **options: object) -> OperationRequest:
    return replace(
        OperationRequest(
            command=OperationKind.IMPORT,
            path=FIXTURE,
            target_format=TargetFormat.CODEX,
            home=tmp_path / "target",
            session_id=TARGET_ID,
            cwd=tmp_path,
        ),
        **options,
    )


def environment(tmp_path: Path) -> dict[str, str]:
    result = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    for key in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR"):
        path = tmp_path / key.lower()
        conversion._mkdir_private_tree(path)
        result[key] = str(path)
    return result


def catalog_virtual_database(root: Path, source_format: AgentFormat, marker: str) -> Path:
    root.mkdir(parents=True)
    database = root / f"{source_format.value}.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
                slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
                version TEXT NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL, time_archived INTEGER
            );
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
            """
        )
        native_id = "ses_33333333333343338333333333333333"
        version = "1.17.20" if source_format == AgentFormat.OPENCODE else "7.5.0"
        connection.execute(
            "INSERT INTO session VALUES (?, 'global', NULL, 'synthetic', ?, ?, ?, "
            "1787054400000, 1787054404000, NULL)",
            (native_id, str(root.parent), marker, version),
        )
        connection.execute(
            "INSERT INTO message VALUES ('synthetic', ?, ?)",
            (native_id, json.dumps({"text": marker})),
        )
    return database


def catalog_virtual_request(
    tmp_path: Path, root: Path, source_format: AgentFormat
) -> OperationRequest:
    database = tmp_path / "catalog.sqlite3"
    with Catalog(database) as catalog:
        catalog.add_root(source_format, root)
        summary = catalog.refresh(include_auto=False)
        assert summary.root_errors == 0
        entries = catalog.list_sessions(agent_format=source_format)
        assert len(entries) == 1
    return request(
        tmp_path,
        command=OperationKind.TRANSFER,
        path=None,
        catalog=database,
        catalog_id=entries[0].catalog_id,
        dry_run=True,
    )


@pytest.mark.parametrize("source_format", [AgentFormat.OPENCODE, AgentFormat.KILO])
def test_catalog_noncanonical_store_rejects_same_id_in_different_native_store(
    source_format, tmp_path, monkeypatch
):
    env = environment(tmp_path / "environment")
    parent = tmp_path / "data"
    selected = parent / "selected-other"
    native = parent / source_format.value
    catalog_virtual_database(selected, source_format, "SYNTHETIC_SELECTED_MARKER")
    catalog_virtual_database(native, source_format, "SYNTHETIC_WRONG_STORE_MARKER")
    operation = catalog_virtual_request(tmp_path, selected, source_format)
    exported = []

    def wrong_store_export(session_id, *, source_cli=None, environ):
        database = (
            Path(environ["XDG_DATA_HOME"]) / source_format.value / f"{source_format.value}.db"
        )
        with sqlite3.connect(database) as connection:
            marker = json.loads(
                connection.execute(
                    "SELECT data FROM message WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
            )["text"]
        exported.append(marker)
        pytest.fail("noncanonical catalog selection exported a different native store")

    monkeypatch.setattr(operations, f"load_{source_format.value}_session", wrong_store_export)
    # The old root.parent projection resolves the other store with exactly the same ID.
    with sqlite3.connect(native / f"{source_format.value}.db") as connection:
        row = connection.execute("SELECT id, title FROM session").fetchone()
    assert row == ("ses_33333333333343338333333333333333", "SYNTHETIC_WRONG_STORE_MARKER")
    original = dict(env)
    events = []
    with pytest.raises(SessionMigrateError, match="cannot be mapped to its native XDG data store"):
        run_operation(operation, environ=env, progress=events.append)
    assert exported == []
    assert env == original
    assert events[-1].stage == ProgressStage.FAILED
    assert not operation.home.exists()


@pytest.mark.parametrize("source_format", [AgentFormat.OPENCODE, AgentFormat.KILO])
@pytest.mark.parametrize("alias", ["none", "catalog", "native"])
def test_catalog_canonical_store_and_directory_alias_use_exact_native_root(
    source_format, alias, tmp_path, monkeypatch
):
    env = environment(tmp_path / "environment")
    native = tmp_path / "selected-data" / source_format.value
    selected = native if alias != "native" else native.parent / "selected-other"
    catalog_virtual_database(selected, source_format, "SYNTHETIC_SELECTED_MARKER")
    if alias == "native":
        native.symlink_to(selected, target_is_directory=True)
    elif alias == "catalog":
        selected = tmp_path / "catalog-alias"
        selected.symlink_to(native, target_is_directory=True)
    operation = catalog_virtual_request(tmp_path, selected, source_format)
    observed = []
    source = conversion.load_session(
        Path(__file__).parent / "fixtures/opencode-source-1.17.20/comprehensive.json",
        AgentFormat.OPENCODE,
    )
    source = replace(source, source_format=source_format)

    def export(session_id, *, source_cli=None, environ):
        cli_root = Path(environ["XDG_DATA_HOME"]) / source_format.value
        assert cli_root.samefile(native)
        assert session_id == source.session_id
        observed.append(environ)
        return source

    monkeypatch.setattr(operations, f"load_{source_format.value}_session", export)
    original = dict(env)
    result = run_operation(operation, environ=env)
    assert result.source_format == source_format.value
    assert len(observed) == 1
    assert observed[0]["XDG_DATA_HOME"] == str(native.parent)
    assert env == original


@pytest.mark.parametrize("source_format", [AgentFormat.OPENCODE, AgentFormat.KILO])
def test_catalog_hardlinked_database_is_not_same_native_store(source_format, tmp_path, monkeypatch):
    env = environment(tmp_path / "environment")
    selected = tmp_path / "data" / "selected-other"
    selected_database = catalog_virtual_database(
        selected, source_format, "SYNTHETIC_SELECTED_MARKER"
    )
    native = selected.parent / source_format.value
    native.mkdir()
    (native / selected_database.name).hardlink_to(selected_database)
    assert (native / selected_database.name).samefile(selected_database)
    assert not native.samefile(selected)
    operation = catalog_virtual_request(tmp_path, selected, source_format)

    def forbidden(*args, **kwargs):
        pytest.fail("hardlinked database was mistaken for directory store identity")

    monkeypatch.setattr(operations, f"load_{source_format.value}_session", forbidden)
    with pytest.raises(SessionMigrateError, match="cannot be mapped"):
        run_operation(operation, environ=env)


@pytest.mark.parametrize("command", list(OperationKind))
def test_cli_and_core_results_match(command, tmp_path, capsys):
    source = FIXTURE
    source_id = claude.parse(source).session_id
    core = request(tmp_path, command=command, dry_run=True)
    arguments = [command.value]
    if command == OperationKind.TRANSFER:
        source_home = tmp_path / "source"
        transcript = source_home / "projects/project" / f"{source_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(source.read_bytes())
        core = replace(
            core,
            path=None,
            source_id=source_id,
            source_format=AgentFormat.CLAUDE,
            source_home=source_home,
        )
        arguments += [source_id, "--from", "claude", "--source-home", str(source_home)]
    else:
        arguments += [str(source)]
    arguments += ["--to", "codex", "--session-id", TARGET_ID, "--cwd", str(tmp_path)]
    if command == OperationKind.CONVERT:
        core = replace(core, output=tmp_path / "core.jsonl")
        arguments += ["--output", str(tmp_path / "cli.jsonl")]
    else:
        arguments += ["--home", str(core.home), "--dry-run"]
    expected = run_operation(core).to_dict()
    assert capsys.readouterr().out == ""
    assert main(arguments) == 0
    actual = json.loads(capsys.readouterr().out)
    if command == OperationKind.CONVERT:
        expected["output"] = str(tmp_path / "cli.jsonl")
        expected["manifest"] = str(tmp_path / "cli.jsonl.session-migrate.json")
        assert (tmp_path / "core.jsonl").read_bytes() == (tmp_path / "cli.jsonl").read_bytes()
    assert actual == expected


@pytest.mark.parametrize("target", list(TargetFormat))
def test_convert_routes_without_any_native_target_cli(target, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("convert invoked a target CLI or installer")

    for name in vars(operations):
        if name.startswith("install_") or name == "detect_opencode_version":
            monkeypatch.setattr(operations, name, forbidden)
    result = run_operation(
        request(
            tmp_path,
            command=OperationKind.CONVERT,
            target_format=target,
            home=None,
            output=tmp_path / "native",
        ),
        environ=environment(tmp_path),
    )
    assert result.target_format == target.value
    assert Path(result.output).is_file()
    assert Path(result.manifest).is_file()
    assert "SYNTHETIC" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("target", list(TargetFormat))
def test_import_routes_reuse_existing_installers(target, tmp_path, monkeypatch):
    env = environment(tmp_path)
    observed = []
    installer = f"install_{target.value}_artifact"
    if hasattr(operations, installer):

        def install(artifact, **kwargs):
            assert artifact.target_format == target
            assert kwargs["dry_run"] is True
            if target in {
                TargetFormat.OPENCODE,
                TargetFormat.KILO,
                TargetFormat.ANTIGRAVITY,
                TargetFormat.CURSOR,
                TargetFormat.HERMES,
            }:
                assert kwargs["environ"] == env
            observed.append(target)

        monkeypatch.setattr(operations, installer, install)
    options = {"target_format": target, "dry_run": True}
    if target in {TargetFormat.OPENCODE, TargetFormat.KILO}:
        options["home"] = None
    if target == TargetFormat.OPENCODE:
        options["target_cli_version"] = "1.17.20"
    result = run_operation(request(tmp_path, **options), environ=env)
    assert result.target_format == target.value
    assert observed == ([target] if hasattr(operations, installer) else [])
    assert not Path(result.manifest).exists()


def test_two_environments_resolve_defaults_without_global_mutation(tmp_path, monkeypatch):
    before = dict(os.environ)
    selected = [environment(tmp_path / name) for name in ("one", "two")]
    for env in selected:
        original = dict(env)
        result = run_operation(request(tmp_path, home=None), environ=env)
        assert Path(result.output).is_relative_to(Path(env["HOME"]) / ".codex")
        assert env == original
    assert dict(os.environ) == before
    explicit = tmp_path / "explicit"
    selected[0]["CODEX_HOME"] = "~/configured"
    configured = run_operation(request(tmp_path, home=None), environ=selected[0])
    assert Path(configured.output).is_relative_to(Path(selected[0]["HOME"]) / "configured")
    result = run_operation(request(tmp_path, home=explicit), environ=selected[0])
    assert Path(result.output).is_relative_to(explicit)


def test_explicit_environment_without_home_fails_only_when_required(tmp_path):
    with pytest.raises(SessionMigrateError, match="explicit HOME"):
        run_operation(request(tmp_path, home=None), environ={})
    result = run_operation(
        request(tmp_path), environ={}, migration_state_home=tmp_path / "migrator-state"
    )
    assert Path(result.output).is_relative_to(tmp_path / "target")
    with pytest.raises(SessionMigrateError, match="explicit HOME"):
        run_operation(request(tmp_path, home=Path("~/target")), environ={})


def test_ambient_environment_is_snapshotted_before_observers_run(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    monkeypatch.setenv("HOME", str(selected))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    def observer(event):
        monkeypatch.setenv("HOME", str(tmp_path / "changed"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "changed-target"))

    result = run_operation(request(tmp_path, home=None), progress=observer)
    assert Path(result.output).is_relative_to(selected / ".codex")
    assert not (tmp_path / "changed-target").exists()


def test_progress_is_content_free_and_observer_failure_does_not_undo_success(tmp_path):
    events = []

    def observer(event: ProgressEvent):
        events.append(event)
        raise RuntimeError("synthetic observer failure")

    result = run_operation(request(tmp_path), progress=observer)
    assert [event.stage for event in events] == [
        ProgressStage.LOADING,
        ProgressStage.CONVERTING,
        ProgressStage.PREFLIGHT,
        ProgressStage.PREFLIGHT,
        ProgressStage.RECORDING_INTENT,
        ProgressStage.INSTALLING,
        ProgressStage.VERIFYING,
        ProgressStage.COMPLETED,
    ]
    assert all(not hasattr(event, "session") for event in events)
    assert Path(result.output).is_file()
    assert Path(result.manifest).is_file()
    assert "SYNTHETIC" not in repr(result)
    assert "private title" not in repr(request(tmp_path, title="private title"))
    dry_events = []
    run_operation(
        request(tmp_path, home=tmp_path / "dry", dry_run=True), progress=dry_events.append
    )
    assert ProgressStage.PREFLIGHT in [event.stage for event in dry_events]
    assert ProgressStage.INSTALLING not in [event.stage for event in dry_events]


def test_core_preserves_source_mutation_detection(tmp_path, monkeypatch):
    source = tmp_path / "source.jsonl"
    source.write_bytes(FIXTURE.read_bytes())
    original = operations.capture_source

    def mutate(context, **options):
        snapshot = original(context, **options)
        with context.source_path.open("a") as stream:
            stream.write("{}\n")
        return snapshot

    monkeypatch.setattr(operations, "capture_source", mutate)
    events = []
    with pytest.raises(SessionMigrateError, match="prepared source changed"):
        run_operation(request(tmp_path, path=source), progress=events.append)
    assert events[-1].stage == ProgressStage.FAILED
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_late_collision_never_overwrites_target(tmp_path, dry_run):
    operation = request(tmp_path, dry_run=dry_run)
    planned = run_operation(replace(operation, dry_run=True))
    target = Path(planned.output)

    def observer(event):
        if event.stage in {ProgressStage.INSTALLING, ProgressStage.PREFLIGHT}:
            target.parent.mkdir(parents=True)
            target.write_bytes(b"synthetic existing target")

    with pytest.raises(JsonlError, match="refusing to overwrite"):
        run_operation(operation, progress=observer)
    assert target.read_bytes() == b"synthetic existing target"
    assert not Path(planned.manifest).exists()


def test_omp_source_and_target_buckets_use_selected_home_and_temp_root(tmp_path, monkeypatch):
    env = environment(tmp_path / "selected")
    work = Path(env["HOME"]) / "work"
    work.mkdir()

    def forbidden_home():
        pytest.fail("operation consulted process HOME")

    monkeypatch.setattr(Path, "home", forbidden_home)
    installed = run_operation(
        request(tmp_path, target_format=TargetFormat.OMP, home=None, cwd=work), environ=env
    )
    assert Path(installed.output).parent.name == "-work"
    transferred = run_operation(
        request(
            tmp_path,
            command=OperationKind.TRANSFER,
            path=None,
            source_format=AgentFormat.OMP,
            source_id=TARGET_ID,
            source_cwd=work,
            home=None,
        ),
        environ=env,
    )
    assert transferred.source_format == "omp"
    temp_work = Path(env["TMPDIR"]) / "work"
    temp_work.mkdir()
    planned = run_operation(
        request(
            tmp_path,
            target_format=TargetFormat.OMP,
            home=None,
            cwd=temp_work,
            dry_run=True,
            session_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ),
        environ=env,
    )
    assert Path(planned.output).parent.name == "-tmp-work"


def test_mastracode_git_probe_uses_selected_environment(tmp_path, monkeypatch):
    env = environment(tmp_path)
    observed = []

    def subprocess_probe(command, **kwargs):
        assert kwargs["env"] == env
        observed.append(command)
        return type("Completed", (), {"stdout": "", "returncode": 1})()

    monkeypatch.setattr(mastracode.subprocess, "run", subprocess_probe)
    run_operation(
        request(
            tmp_path,
            target_format=TargetFormat.MASTRACODE,
            command=OperationKind.CONVERT,
            output=tmp_path / "native",
        ),
        environ=env,
    )
    assert observed


def test_shared_native_success_then_manifest_failure_is_typed(tmp_path, monkeypatch):
    env = environment(tmp_path)

    def fail(*args):
        raise OSError("synthetic finalization failure")

    monkeypatch.setattr(conversion, "_write_reserved_file", fail)
    events = []
    operation = request(tmp_path, target_format=TargetFormat.MASTRACODE)
    with pytest.raises(OperationNeedsReconcileError) as failure:
        run_operation(operation, environ=env, progress=events.append)
    assert failure.value.native_state is NativeState.ACKNOWLEDGED and failure.value.needs_reconcile
    assert failure.value.target_format == TargetFormat.MASTRACODE
    assert failure.value.session_id == TARGET_ID
    assert events[-1].stage == ProgressStage.NEEDS_RECONCILE
    assert (
        conversion.load_mastracode_session(TARGET_ID, source_home=operation.home).session_id
        == TARGET_ID
    )
    assert not list(operation.home.glob("session-migrate/manifests/*.json"))


def test_hermes_native_confirmation_then_verification_failure_is_typed(tmp_path, monkeypatch):
    env = environment(tmp_path)
    target_home = tmp_path / "target"
    target_home.mkdir()
    (target_home / "state.db").write_bytes(b"synthetic database placeholder")
    monkeypatch.setattr(hermes, "verify_pinned_cli", lambda *args, **kwargs: Path(sys.executable))
    monkeypatch.setattr(hermes, "_script_interpreter", lambda *args: Path(sys.executable))
    monkeypatch.setattr(hermes, "_assert_no_collision", lambda *args: None)

    def imported(command, **options):
        bundle = json.loads(options["input"])
        session_id = bundle["session"]["id"]
        stdout = json.dumps({"ok": True, "imported_ids": [session_id], "skipped": 0})
        return type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": 0})()

    def fail(*args):
        raise JsonlError("synthetic verification failure")

    monkeypatch.setattr(hermes.subprocess, "run", imported)
    monkeypatch.setattr(hermes, "parse_session", fail)
    events = []
    with pytest.raises(OperationNeedsReconcileError) as failure:
        run_operation(
            request(tmp_path, target_format=TargetFormat.HERMES),
            environ=env,
            progress=events.append,
        )
    assert failure.value.target_format == TargetFormat.HERMES
    assert failure.value.native_state is NativeState.ACKNOWLEDGED
    assert events[-1].stage == ProgressStage.NEEDS_RECONCILE
    assert not list(target_home.glob("session-migrate/manifests/*.json"))


def test_hermes_refusal_never_exposes_native_diagnostics(tmp_path, monkeypatch, capsys):
    env = environment(tmp_path)
    target_home = tmp_path / "target"
    target_home.mkdir()
    (target_home / "state.db").write_bytes(b"synthetic database placeholder")
    monkeypatch.setattr(hermes, "verify_pinned_cli", lambda *args, **kwargs: Path(sys.executable))
    monkeypatch.setattr(hermes, "_script_interpreter", lambda *args: Path(sys.executable))
    monkeypatch.setattr(hermes, "_assert_no_collision", lambda *args: None)
    private = "SYNTHETIC_PRIVATE_NATIVE_DETAIL"

    def refused(*args, **kwargs):
        stdout = json.dumps({"ok": False, "errors": [private]})
        return type("Completed", (), {"stdout": stdout, "stderr": private, "returncode": 1})()

    monkeypatch.setattr(hermes.subprocess, "run", refused)
    events = []
    with pytest.raises(SessionMigrateError) as failure:
        run_operation(
            request(tmp_path, target_format=TargetFormat.HERMES),
            environ=env,
            progress=events.append,
        )
    assert isinstance(failure.value, OperationNeedsReconcileError)
    assert failure.value.native_state is NativeState.ATTEMPTED_UNKNOWN
    assert events[-1].stage == ProgressStage.NEEDS_RECONCILE
    status = main(
        [
            "import",
            str(FIXTURE),
            "--to",
            "hermes",
            "--home",
            str(target_home),
            "--cwd",
            str(tmp_path),
            "--session-id",
            TARGET_ID,
        ]
    )
    assert status == 2
    error = capsys.readouterr().err
    receipt = json.loads(error)
    assert receipt["status"] == "needs_reconcile"
    assert receipt["installation_state"] == "attempted_unknown"
    assert private not in error


@pytest.mark.parametrize("existing", [False, True])
def test_mastracode_custom_database_filename_fails_before_sibling_io(existing, tmp_path):
    env = environment(tmp_path)
    custom = tmp_path / "custom.db"
    if existing:
        custom.write_bytes(b"synthetic custom database")
    env["MASTRA_DB_PATH"] = str(custom)
    with pytest.raises(SessionMigrateError, match="canonical mastra.db"):
        run_operation(
            request(tmp_path, target_format=TargetFormat.MASTRACODE, home=None), environ=env
        )
    assert not (tmp_path / "mastra.db").exists()
    if existing:
        assert custom.read_bytes() == b"synthetic custom database"


def test_mastracode_canonical_database_environment_selects_existing_and_new_store(tmp_path):
    env = environment(tmp_path)
    database = tmp_path / "canonical/mastra.db"
    env["MASTRA_DB_PATH"] = str(database)
    imported = run_operation(
        request(tmp_path, target_format=TargetFormat.MASTRACODE, home=None), environ=env
    )
    assert database.is_file()
    transferred = run_operation(
        request(
            tmp_path,
            command=OperationKind.TRANSFER,
            path=None,
            source_format=AgentFormat.MASTRACODE,
            source_id=imported.session_id,
            source_home=None,
            home=tmp_path / "codex",
        ),
        environ=env,
    )
    assert transferred.source_format == "mastracode"
    assert not (Path(env["HOME"]) / ".local/share/mastracode/mastra.db").exists()


@pytest.mark.parametrize("target", [TargetFormat.ANTIGRAVITY, TargetFormat.CURSOR])
def test_pinned_database_installer_success_then_manifest_failure_is_typed(
    target, tmp_path, monkeypatch
):
    module = antigravity if target == TargetFormat.ANTIGRAVITY else cursor
    observed = []

    def install(native_bytes, **options):
        # Model the native installer success boundary; no proprietary binary needed.
        session_id = options["session_id"]
        relative = (
            module.session_relative_path(session_id)
            if target == TargetFormat.ANTIGRAVITY
            else module.session_relative_path(session_id, options["cwd"])
        )
        path = options["target_home"] / relative
        if options.get("dry_run"):
            assert not path.exists()
            return type("Installed", (), {"conversation_path": path})()
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(native_bytes)
        observed.append(path)
        return type("Installed", (), {"conversation_path": path})()

    def fail(*args):
        raise OSError("synthetic finalization failure")

    monkeypatch.setattr(module, "install_database", install)
    monkeypatch.setattr(conversion, "_write_reserved_file", fail)
    events = []
    with pytest.raises(OperationNeedsReconcileError) as failure:
        run_operation(request(tmp_path, target_format=target), progress=events.append)
    assert failure.value.target_format == target
    assert failure.value.native_state is NativeState.ACKNOWLEDGED
    assert observed[0].exists()
    assert events[-1].stage == ProgressStage.NEEDS_RECONCILE


def fake_native_cli(directory: Path, target: TargetFormat) -> Path:
    """Model public legacy native commands with an isolated HOME/XDG store."""

    binary = directory / target.value
    binary.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
from pathlib import Path
kind = Path(sys.argv[0]).name
arguments = sys.argv[1:]
root = Path(os.environ['XDG_DATA_HOME']) / kind
store = root / 'synthetic-store.json'
data = json.loads(store.read_text()) if store.exists() else {}
audit = Path(os.environ['HOME']) / 'audit.jsonl'
with audit.open('a') as stream:
    stream.write(json.dumps({'arguments': arguments, 'home': os.environ['HOME'],
                            'data': os.environ['XDG_DATA_HOME'], 'cwd': os.getcwd()}) + '\\n')
if arguments == ['--version']:
    print('1.17.20' if kind == 'opencode' else '7.5.0')
elif arguments[:2] == ['session', 'list']:
    print(json.dumps([{'id': key} for key in data]))
elif arguments[0] == 'import':
    bundle = json.loads(Path(arguments[1]).read_text())
    data[bundle['info']['id']] = bundle
    root.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(data))
elif arguments[0] == 'export':
    if arguments[1] not in data:
        print('Session not found: ' + arguments[1], file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data[arguments[1]]))
else:
    sys.exit(2)
"""
    )
    binary.chmod(0o700)
    return binary


@pytest.mark.parametrize("target", [TargetFormat.OPENCODE, TargetFormat.KILO])
def test_native_subprocesses_and_virtual_sources_are_isolated(target, tmp_path, monkeypatch):
    selected = [environment(tmp_path / name) for name in ("one", "two")]
    monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ambient-data"))
    monkeypatch.setenv("PATH", "/synthetic/ambient-path")
    before = dict(os.environ)
    for env in selected:
        binary = fake_native_cli(Path(env["HOME"]), target)
        env[f"{target.value.upper()}_BIN"] = str(binary)
        original = dict(env)
        imported = run_operation(request(tmp_path, target_format=target, home=None), environ=env)
        assert Path(imported.manifest).is_relative_to(Path(env["XDG_STATE_HOME"]))
        source = run_operation(
            request(
                tmp_path,
                command=OperationKind.TRANSFER,
                path=None,
                home=None,
                source_format=AgentFormat(target.value),
                source_id=imported.session_id,
                session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            ),
            environ=env,
        )
        assert source.source_format == target.value
        assert Path(source.output).is_relative_to(Path(env["HOME"]))
        records = [
            json.loads(line)
            for line in (Path(env["HOME"]) / "audit.jsonl").read_text().splitlines()
        ]
        assert all(
            record["home"] == env["HOME"] and record["data"] == env["XDG_DATA_HOME"]
            for record in records
        )
        assert any(record["arguments"][0] == "export" for record in records)
        assert env == original
    assert dict(os.environ) == before
    assert not (tmp_path / "ambient-home").exists()
    assert not (tmp_path / "ambient-data").exists()


@pytest.mark.parametrize("target", [TargetFormat.OPENCODE, TargetFormat.KILO])
def test_native_subprocess_success_then_finalization_failure_is_typed(
    target, tmp_path, monkeypatch
):
    env = environment(tmp_path)
    binary = fake_native_cli(Path(env["HOME"]), target)
    env[f"{target.value.upper()}_BIN"] = str(binary)

    def fail(*args):
        raise OSError("synthetic manifest finalization failure")

    monkeypatch.setattr(conversion, "_write_reserved_file", fail)
    events = []
    with pytest.raises(OperationNeedsReconcileError) as failure:
        run_operation(
            request(tmp_path, target_format=target, home=None), environ=env, progress=events.append
        )
    assert failure.value.native_state is NativeState.ACKNOWLEDGED and failure.value.needs_reconcile
    assert failure.value.target_format == target
    assert events[-1].stage == ProgressStage.NEEDS_RECONCILE
    store = Path(env["XDG_DATA_HOME"]) / target.value / "synthetic-store.json"
    assert failure.value.session_id in json.loads(store.read_text())
    assert not list(Path(env["XDG_STATE_HOME"]).glob("session-migrate/manifests/*/*.json"))
