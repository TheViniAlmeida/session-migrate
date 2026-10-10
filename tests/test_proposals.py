"""Prepared publication contracts, with synthetic files and a private fake CLI."""

import hashlib
import json
import os
import shutil
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType

import pytest

from session_migrate import operations
from session_migrate.catalog import Catalog
from session_migrate.errors import (
    OperationCancelledError,
    OperationNeedsReconcileError,
    PublishedArtifactPartialError,
    SessionMigrateError,
)
from session_migrate.lineage import LineageStore, NativeState, OperationState
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import (
    CancellationToken,
    OperationRequest,
    ProgressStage,
    commit_operation,
    prepare_operation,
    proposal_metadata,
    run_operation,
)
from session_migrate.profiles import (
    Profile,
    list_profile_sessions,
    local_alias_for_session,
    prepare_profile_operation,
)
from session_migrate.titles import TitleAliasStore, TitlePolicy

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
TITLE = "Título 🧪 日本語 [bold]"


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_bytes(FIXTURE.read_bytes())
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "SYNTHETIC_SECRET": "do-not-serialize-this"}
    for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "TMPDIR"):
        path = tmp_path / key.lower()
        path.mkdir(mode=0o700)
        env[key] = str(path)
    request = OperationRequest(
        "import", path=source, target_format="codex", home=tmp_path / "target", cwd=tmp_path
    )
    return request, env


@pytest.fixture
def fake_cli(tmp_path):
    cli = tmp_path / "fake-opencode"
    cli.write_text("""#!/usr/bin/python3
import json, os, pathlib, sqlite3, sys
root = pathlib.Path(os.environ['XDG_DATA_HOME']) / 'opencode'
args = sys.argv[1:]
if args == ['--version']:
    print('2.0.23'); sys.exit(0)
root.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(root / 'opencode.db')
db.execute('CREATE TABLE IF NOT EXISTS session ('
           'id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT, '
           'title TEXT, version TEXT, time_created INTEGER, time_updated INTEGER, '
           'time_archived INTEGER)')
if args[:2] == ['session', 'list']:
    rows = db.execute('SELECT id,title FROM session')
    print(json.dumps([{'id': row[0], 'title': row[1]} for row in rows]))
elif args[:2] == ['session', 'import']:
    bundle = json.loads(pathlib.Path(args[2]).read_text()); info = bundle['info']
    db.execute('INSERT INTO session VALUES (?, ?, NULL, ?, ?, ?, ?, 1, 1, NULL)',
        (info['id'], 'synthetic', info.get('slug','synthetic'),
         info['location']['directory'], info['title'], '2.0.23'))
    db.commit()
    directory = root / 'bundles'; directory.mkdir(exist_ok=True)
    (directory / (info['id'] + '.json')).write_text(json.dumps(bundle))
    print('Imported session: ' + info['id'])
elif args[:2] == ['session', 'export']:
    print((root / 'bundles' / (args[2] + '.json')).read_text())
else:
    sys.exit(2)
""")
    cli.chmod(0o700)
    return cli


def test_prepare_is_readonly_and_commit_reuses_exact_artifact_id_timestamp(setup, monkeypatch):
    request, env = setup
    count = []
    original = operations.convert_session

    def convert(*args, **kwargs):
        count.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(operations, "convert_session", convert)
    prepared = prepare_operation(request, environ=env)
    proposal = proposal_metadata(prepared).to_dict()
    assert not request.home.exists()
    assert proposal["legacy_result"]["dry_run"]
    assert "do-not-serialize-this" not in json.dumps(proposal)
    assert "SYNTHETIC_SECRET" not in json.dumps(proposal)
    receipt = commit_operation(prepared)
    result = receipt.legacy_result
    assert (
        hashlib.sha256(Path(result["output"]).read_bytes()).hexdigest()
        == proposal["target"]["sha256"]
    )
    assert result["session_id"] == proposal["target"]["native_id"]
    assert (
        json.loads(Path(result["manifest"]).read_bytes())["target"]["timestamp"]
        == proposal["target"]["timestamp"]
    )
    assert count == [1]
    assert receipt.to_dict()["schema_version"] == 1


def test_proposal_frozen_nested_output_copies_and_discard(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    metadata = proposal_metadata(prepared)
    with pytest.raises(FrozenInstanceError):
        metadata.proposal_id = "changed"
    document = metadata.to_dict()
    document["target"]["native_id"] = "changed"
    document["options"]["target_environment"]["HOME"] = "changed"
    assert metadata.to_dict()["target"]["native_id"] != "changed"
    prepared.discard()
    with pytest.raises(SessionMigrateError, match="consumed"):
        commit_operation(prepared)
    assert not request.home.exists()


def test_source_append_rejects_before_install_and_consumes_handle(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    request.path.write_bytes(request.path.read_bytes() + b"\n")
    with pytest.raises(SessionMigrateError, match="source changed"):
        commit_operation(prepared)
    assert not request.home.exists()
    with pytest.raises(SessionMigrateError, match="consumed"):
        commit_operation(prepared)


def test_late_target_collision_no_overwrite(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    output = Path(proposal_metadata(prepared).to_dict()["legacy_result"]["output"])
    output.parent.mkdir(parents=True)
    output.write_bytes(b"competing synthetic target")
    with pytest.raises(SessionMigrateError, match="destination|overwrite"):
        commit_operation(prepared)
    assert output.read_bytes() == b"competing synthetic target"


def test_unrelated_session_can_create_previously_absent_target_root(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    request.home.mkdir()
    (request.home / "unrelated-synthetic").write_bytes(b"unrelated metadata")
    assert commit_operation(prepared).status == "completed"


def test_new_target_symlink_cannot_redirect_prepared_destination(setup, tmp_path):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    request.home.symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(SessionMigrateError, match="symlink"):
        commit_operation(prepared)
    assert list(unrelated.iterdir()) == []


def test_bounded_directory_entrypoint_preserves_provenance_and_scope(setup, tmp_path):
    request, env = setup
    fixture = Path(__file__).parent / "fixtures/vibe-2.24.3/basic"
    directory = tmp_path / "vibe"
    shutil.copytree(fixture, directory)
    prepared = prepare_operation(
        replace(request, path=directory, source_format="vibe"),
        environ=env,
        max_source_bytes=8 * 1024 * 1024,
    )
    source = proposal_metadata(prepared).to_dict()["source"]
    assert source["path"] == str(directory / "messages.jsonl")
    assert source["read_limit_scope"] == "private_bounded_snapshot"
    assert commit_operation(prepared).status == "completed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("cwd", Path("/")),
        ("model", "changed"),
        ("target_cli_version", "changed"),
        ("target_title", "changed"),
        ("title_policy", TitlePolicy.LOCAL_ALIAS),
    ],
)
def test_private_request_tampering_is_refused(setup, field, value):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    object.__setattr__(prepared._state.built.request, field, value)
    with pytest.raises(SessionMigrateError, match="options or artifact"):
        commit_operation(prepared)
    assert not request.home.exists()


def test_target_root_replacement_and_cwd_replacement_rejected(setup, tmp_path):
    request, env = setup
    request.home.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    for replace_path in (request.home, cwd):
        prepared = prepare_operation(replace(request, cwd=cwd), environ=env)
        replace_path.rename(replace_path.with_name(replace_path.name + "-old"))
        replace_path.mkdir()
        with pytest.raises(SessionMigrateError, match="destination or CWD"):
            commit_operation(prepared)


def test_double_confirmation_concurrent_only_one_writer(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    barrier = threading.Barrier(2)
    results = []

    def commit():
        barrier.wait()
        try:
            results.append(commit_operation(prepared).status)
        except SessionMigrateError:
            results.append("refused")

    workers = [threading.Thread(target=commit) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sorted(results) == ["completed", "refused"]


def test_alias_after_verified_target_and_separate_receipt_manifest_v2(setup):
    request, env = setup
    request = replace(request, target_title=TITLE, title_policy=TitlePolicy.LOCAL_ALIAS)
    prepared = prepare_operation(request, environ=env)
    assert not request.home.exists()
    assert not (Path(env["XDG_STATE_HOME"]) / "session-migrate").exists()
    receipt = commit_operation(prepared)
    row = receipt.to_dict()
    assert row["title"]["persistence"] == "local_alias"
    assert row["title"]["native_observed"] is None
    assert json.loads(Path(row["legacy_result"]["manifest"]).read_bytes())["schema_version"] == 2
    title_row = json.loads(Path(row["title_receipt"]).read_bytes())
    assert title_row["binding"]["identity"]["root_inode"] == request.home.stat().st_ino


@pytest.mark.parametrize(
    "failure,stage",
    [
        ("alias", "finalizing"),
        ("receipt", "finalizing"),
        ("verify", "observing"),
    ],
)
def test_failure_after_native_publication_is_reconcile_with_manifest_preserved(
    setup, monkeypatch, failure, stage
):
    request, env = setup
    request = replace(request, target_title=TITLE, title_policy=TitlePolicy.LOCAL_ALIAS)
    prepared = prepare_operation(request, environ=env)

    def fail(*args, **kwargs):
        raise OSError("synthetic private diagnostic not echoed")

    if failure == "alias":
        monkeypatch.setattr(TitleAliasStore, "create", fail)
    elif failure == "receipt":
        monkeypatch.setattr(operations, "write_title_receipt", fail)
    else:
        monkeypatch.setattr(operations, "_verify_installed_identity", fail)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    error = caught.value
    assert error.needs_reconcile and error.native_state is NativeState.ACKNOWLEDGED
    assert not hasattr(error, "import_succeeded")
    assert error.receipt.ledger_recorded
    assert error.stage == stage
    assert Path(error.manifest).is_file()
    assert any(
        reference.kind == "manifest" and reference.availability == "available"
        for reference in error.receipt.references
    )
    assert "private diagnostic" not in str(error)
    view = LineageStore.inspect_existing(
        operations.default_migration_state_home(environ=env) / "lineage-v1"
    )
    assert view.operations[0].durable_state is OperationState.NEEDS_RECONCILE
    with pytest.raises(SessionMigrateError, match="consumed"):
        commit_operation(prepared)


def test_convert_prepare_no_files_commit_pending_and_metadata_failure_is_not_native_import(
    setup, tmp_path, monkeypatch
):
    request, env = setup
    request = replace(
        request,
        command="convert",
        output=tmp_path / "converted.jsonl",
        target_title=TITLE,
        title_policy=TitlePolicy.LOCAL_ALIAS,
    )
    prepared = prepare_operation(request, environ=env)
    assert not request.output.exists()
    receipt = commit_operation(prepared)
    assert receipt.to_dict()["title"]["persistence"] == "alias_pending"
    assert receipt.to_dict()["target_identity"] is None
    assert not (Path(env["XDG_STATE_HOME"]) / "session-migrate").exists()
    second = prepare_operation(replace(request, output=tmp_path / "second.jsonl"), environ=env)

    def fail(*args, **kwargs):
        raise OSError("synthetic finalization failure")

    monkeypatch.setattr(operations, "write_title_receipt", fail)
    with pytest.raises(PublishedArtifactPartialError) as caught:
        commit_operation(second)
    assert caught.value.publication_succeeded and not caught.value.import_succeeded
    assert Path(caught.value.output).is_file() and Path(caught.value.manifest).is_file()


@pytest.mark.parametrize(
    "options",
    [
        {"require_native_title": True},
        {"target_title": TITLE, "require_native_title": True},
        {"target_title": TITLE},
        {
            "target_title": TITLE,
            "title_policy": TitlePolicy.LOCAL_ALIAS,
            "require_native_title": True,
        },
    ],
)
def test_static_title_refusal_before_load_or_write(setup, monkeypatch, options):
    request, env = setup
    request = replace(request, **options)
    if options.get("require_native_title") and options.get("title_policy") is None:
        request = replace(request, command="convert", output=request.home / "output.jsonl")
    monkeypatch.setattr(
        operations, "_resolve_source", lambda *a, **kw: pytest.fail("source loaded")
    )
    token = CancellationToken()
    with pytest.raises(SessionMigrateError):
        prepare_operation(request, environ=env, cancellation=token)
    assert not token.request_cancel()
    assert not request.home.exists()


@pytest.mark.parametrize("cancel_at", [ProgressStage.LOADING, ProgressStage.PREFLIGHT])
def test_prepare_cancellation_no_writes_token_closed(setup, cancel_at):
    request, env = setup
    token = CancellationToken()

    def progress(event):
        if event.stage == cancel_at:
            token.request_cancel()

    with pytest.raises(OperationCancelledError):
        prepare_operation(request, environ=env, cancellation=token, progress=progress)
    assert not request.home.exists()
    assert not token.request_cancel()


def test_commit_cancel_boundary_and_progress_observational(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    token = CancellationToken()

    def cancel(event):
        if event.stage == ProgressStage.PREFLIGHT:
            token.request_cancel()

    with pytest.raises(OperationCancelledError):
        commit_operation(prepared, cancellation=token, progress=cancel)
    assert not request.home.exists()
    prepared = prepare_operation(request, environ=env)
    token = CancellationToken()

    def observer(event):
        if event.stage == ProgressStage.INSTALLING:
            assert not token.request_cancel()
        raise RuntimeError("observer cannot control publication")

    assert commit_operation(prepared, cancellation=token, progress=observer).status == "completed"


def test_direct_override_cancel_before_load_and_failed_setup_closes_token(setup, monkeypatch):
    request, env = setup
    request = replace(request, target_title=TITLE, title_policy=TitlePolicy.LOCAL_ALIAS)
    token = CancellationToken()
    token.request_cancel()
    monkeypatch.setattr(
        operations, "_resolve_source", lambda *a, **kw: pytest.fail("source loaded")
    )
    with pytest.raises(OperationCancelledError):
        run_operation(request, environ=env, cancellation=token)
    assert not token.request_cancel()
    token = CancellationToken()
    with pytest.raises(SessionMigrateError):
        run_operation(replace(request, path=Path("~/missing")), environ={}, cancellation=token)
    assert not token.request_cancel()


def test_closed_unused_token_cannot_start_another_request(setup):
    request, env = setup
    token = CancellationToken()
    token.close()
    with pytest.raises(SessionMigrateError, match="already belongs"):
        prepare_operation(request, environ=env, cancellation=token)
    assert not request.home.exists()


def test_opencode_runtime_version_and_title_verified_by_public_api(setup, fake_cli):
    request, env = setup
    request = replace(
        request,
        target_format="opencode",
        home=None,
        target_cli=fake_cli,
        target_title=TITLE,
        require_native_title=True,
    )
    prepared = prepare_operation(request, environ=env)
    proposal = proposal_metadata(prepared).to_dict()
    assert proposal["target"]["cli_version"] == "2.0.23"
    assert proposal["effects"]
    receipt = commit_operation(prepared)
    assert receipt.title.persistence == "native_verified"
    assert receipt.title.observed_cli_version == "2.0.23"
    assert receipt.title.native_observed == TITLE


def test_cli_identity_changed_before_confirm_refused(setup, fake_cli):
    request, env = setup
    prepared = prepare_operation(
        replace(request, target_format="opencode", home=None, target_cli=fake_cli), environ=env
    )
    fake_cli.write_text(fake_cli.read_text() + "\n# replacement\n")
    with pytest.raises(SessionMigrateError, match="client identity"):
        commit_operation(prepared)


def test_declared_client_label_cannot_bypass_runtime_version(setup, fake_cli):
    request, env = setup
    request = replace(
        request,
        target_format="opencode",
        home=None,
        target_cli=fake_cli,
        target_cli_version="2.0.22",
        target_title=TITLE,
    )
    with pytest.raises(SessionMigrateError, match="client version"):
        prepare_operation(request, environ=env)
    assert not (Path(env["XDG_DATA_HOME"]) / "opencode").exists()


def test_profile_wrappers_revalidate_exact_ref_and_alias_remains_separate(setup, tmp_path):
    request, env = setup
    source_root = tmp_path / "source-profile"
    source_root.mkdir()
    source_id = operations.load_session(FIXTURE, AgentFormat.CLAUDE).session_id
    source_path = source_root / "projects" / "-synthetic" / f"{source_id}.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(FIXTURE.read_bytes())
    database = tmp_path / "catalog.sqlite3"
    with Catalog(database) as catalog:
        catalog.add_root(AgentFormat.CLAUDE, source_root)
        catalog.refresh(include_auto=False)
        source = Profile("source", "Source profile", AgentFormat.CLAUDE, source_root)
        target = Profile(
            "target",
            "Target profile",
            AgentFormat.CODEX,
            request.home,
            environ={key: value for key, value in env.items() if key in {"HOME", "XDG_STATE_HOME"}},
        )
        ref = list_profile_sessions(catalog, source)[0].ref
        prepared = prepare_profile_operation(
            catalog,
            source,
            ref,
            target,
            OperationRequest(
                "transfer", cwd=tmp_path, target_title=TITLE, title_policy=TitlePolicy.LOCAL_ALIAS
            ),
            base_environ=env,
        )
        assert (
            proposal_metadata(prepared).to_dict()["options"]["profiles"]["target_profile_id"]
            == "target"
        )
        receipt = commit_operation(prepared)
        catalog.add_root(AgentFormat.CODEX, target.root)
        catalog.refresh(include_auto=False)
        target_row = list_profile_sessions(catalog, target)[0]
        native_title = target_row.entry.title
        assert local_alias_for_session(catalog, target, target_row.ref, base_environ=env) == TITLE
        assert list_profile_sessions(catalog, target)[0].entry.title == native_title
        assert receipt.title.alias_saved


def test_missing_source_timestamp_uses_exact_prepared_bytes_without_reconversion(
    setup, monkeypatch
):
    request, env = setup
    rows = [json.loads(line) for line in request.path.read_text().splitlines()]
    for row in rows:
        row.pop("timestamp", None)
    request.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    prepared = prepare_operation(request, environ=env)
    proposal = proposal_metadata(prepared).to_dict()
    monkeypatch.setattr(operations, "convert_session", lambda *a, **kw: pytest.fail("reconverted"))
    receipt = commit_operation(prepared)
    actual = hashlib.sha256(Path(receipt.legacy_result["output"]).read_bytes()).hexdigest()
    assert actual == proposal["target"]["sha256"]


def test_companion_metadata_change_invalidates_authoritative_source(setup, tmp_path):
    request, env = setup
    fixture = Path(__file__).parent / "fixtures/vibe-2.24.3/basic"
    directory = tmp_path / "vibe"
    shutil.copytree(fixture, directory)
    prepared = prepare_operation(
        replace(request, path=directory, source_format="vibe"), environ=env
    )
    metadata = directory / "meta.json"
    row = json.loads(metadata.read_bytes())
    row["title"] = "changed synthetic metadata"
    metadata.write_text(json.dumps(row))
    with pytest.raises(SessionMigrateError, match="source changed"):
        commit_operation(prepared)
    assert not request.home.exists()


def test_last_authority_check_after_preflight_rejects_source_append(setup):
    request, env = setup
    calls = []

    def guard():
        calls.append(1)
        if len(calls) == 4:
            request.path.write_bytes(request.path.read_bytes() + b"\n")

    prepared = prepare_operation(request, environ=env, prepublication_check=guard)
    with pytest.raises(SessionMigrateError, match="source changed"):
        commit_operation(prepared)
    assert not request.home.exists()


def test_native_title_verification_failure_preserves_installed_manifest(
    setup, fake_cli, monkeypatch
):
    request, env = setup
    prepared = prepare_operation(
        replace(
            request, target_format="opencode", home=None, target_cli=fake_cli, target_title=TITLE
        ),
        environ=env,
    )

    def fail(*args, **kwargs):
        raise SessionMigrateError("synthetic native body diagnostic")

    monkeypatch.setattr(operations, "_observe_native_title", fail)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert caught.value.stage == "finalizing"
    assert caught.value.native_state is NativeState.ACKNOWLEDGED
    assert caught.value.receipt.ledger_recorded
    assert Path(caught.value.manifest).is_file()
    assert "body diagnostic" not in str(caught.value)


def test_version_probe_free_form_output_not_exposed(setup, fake_cli):
    request, env = setup
    fake_cli.write_text(
        fake_cli.read_text().replace("print('2.0.23')", "print('PRIVATE_NATIVE_DIAGNOSTIC')")
    )
    with pytest.raises(SessionMigrateError) as caught:
        prepare_operation(
            replace(
                request,
                target_format="opencode",
                home=None,
                target_cli=fake_cli,
                target_title=TITLE,
            ),
            environ=env,
        )
    assert "PRIVATE_NATIVE_DIAGNOSTIC" not in str(caught.value)


@pytest.mark.parametrize("changed", ["target_symlink", "cwd", "client"])
def test_final_slow_authority_guard_cannot_redirect_publication(setup, tmp_path, fake_cli, changed):
    request, env = setup
    request.home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    request = replace(request, cwd=project)
    if changed == "client":
        request = replace(request, home=None, target_format="opencode", target_cli=fake_cli)
    calls = []

    def guard():
        calls.append(1)
        if len(calls) != 4:
            return
        if changed == "target_symlink":
            request.home.rename(tmp_path / "old-target")
            request.home.symlink_to(redirected, target_is_directory=True)
        elif changed == "cwd":
            project.rename(tmp_path / "old-project")
            project.mkdir()
        else:
            fake_cli.write_text(fake_cli.read_text() + "\n# changed after authority\n")

    prepared = prepare_operation(request, environ=env, prepublication_check=guard)
    with pytest.raises(SessionMigrateError, match="symlink|CWD|client identity"):
        commit_operation(prepared)
    assert list(redirected.iterdir()) == []
    if changed == "client":
        import sqlite3

        with sqlite3.connect(Path(env["XDG_DATA_HOME"]) / "opencode/opencode.db") as db:
            assert db.execute("SELECT count(*) FROM session").fetchone()[0] == 0


def test_namespace_check_is_after_client_hash_io(setup, tmp_path, fake_cli, monkeypatch):
    request, env = setup
    request = replace(request, home=None, target_format="opencode", target_cli=fake_cli)
    prepared = prepare_operation(request, environ=env)
    root = Path(env["XDG_DATA_HOME"]) / "opencode"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    original = operations._client_revision
    moved = []

    def slow_hash(path):
        result = original(path)
        if path is not None and not moved:
            root.rename(tmp_path / "old-native")
            root.symlink_to(redirected, target_is_directory=True)
            moved.append(1)
        return result

    monkeypatch.setattr(operations, "_client_revision", slow_hash)
    with pytest.raises(SessionMigrateError, match="symlink"):
        commit_operation(prepared)
    assert list(redirected.iterdir()) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ("timestamp", "2026-10-11T00:00:00Z"),
        ("target_format", TargetFormat.CLAUDE),
        ("native_record_count", 999),
        ("target_cli_version", "different"),
        ("dropped", {"session:title": 999}),
        ("warnings", ({"code": "changed"},)),
    ],
)
def test_artifact_metadata_tampering_refused_even_when_bytes_unchanged(setup, field, value):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    artifact = prepared._state.built.artifact
    byte_hash = artifact.target_sha256
    object.__setattr__(artifact, field, value)
    assert artifact.target_sha256 == byte_hash
    with pytest.raises(SessionMigrateError, match="options or artifact"):
        commit_operation(prepared)
    assert not request.home.exists()


def test_runtime_environments_are_private_immutable_snapshots_without_secret_hash(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    for environment in (prepared._state.target_environment, prepared._state.source_environment):
        assert isinstance(environment, MappingProxyType)
        with pytest.raises(TypeError):
            environment["RUNTIME_ONLY_OPTION"] = "changed"
    encoded = json.dumps(proposal_metadata(prepared).to_dict())
    assert env["SYNTHETIC_SECRET"] not in encoded
    assert hashlib.sha256(env["SYNTHETIC_SECRET"].encode()).hexdigest() not in encoded
    env["SYNTHETIC_SECRET"] = "external dict changed"
    assert prepared._state.target_environment["SYNTHETIC_SECRET"] == "do-not-serialize-this"
    prepared.discard()


@pytest.mark.parametrize("replacement", ["target", "manifest_parent"])
def test_postinstall_namespace_replacement_never_binds_alias_to_empty_root(
    setup, tmp_path, monkeypatch, replacement
):
    request, env = setup
    request = replace(request, target_title=TITLE, title_policy=TitlePolicy.LOCAL_ALIAS)
    prepared = prepare_operation(request, environ=env)
    proposal = proposal_metadata(prepared).to_dict()
    original = operations._verify_installed_identity
    moved = []

    def verify(state):
        session = original(state)
        root = state.target_root if replacement == "target" else state.built.manifest.parent
        old = root.with_name(root.name + "-old")
        root.rename(old)
        root.mkdir()
        moved.append((root, old))
        return session

    monkeypatch.setattr(operations, "_verify_installed_identity", verify)
    with pytest.raises(OperationNeedsReconcileError) as caught:
        commit_operation(prepared)
    assert caught.value.stage == "observing"
    assert caught.value.native_state is NativeState.ACKNOWLEDGED
    assert caught.value.receipt.ledger_recorded
    assert any(
        reference.kind == "manifest" and reference.availability == "missing"
        for reference in caught.value.receipt.references
    )
    root, old = moved[0]
    manifest = Path(proposal["legacy_result"]["manifest"])
    assert (old / manifest.relative_to(root)).is_file()
    assert not (Path(env["XDG_STATE_HOME"]) / "session-migrate/title-aliases/v1").exists()
    assert not Path(proposal["title_receipt"]).exists()


def test_mastra_future_file_allows_unrelated_creation_and_rejects_collision_replacement(
    setup, tmp_path
):
    request, env = setup
    database = tmp_path / "mastra.db"
    request = replace(request, target_format="mastracode", home=database)
    prepared = prepare_operation(request, environ=env)
    assert not database.exists()
    unrelated = run_operation(
        replace(request, session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), environ=env
    )
    assert database.is_file()
    receipt = commit_operation(prepared)
    assert (
        receipt.status == "completed"
        and receipt.legacy_result["session_id"] != unrelated.session_id
    )
    prepared = prepare_operation(
        replace(request, session_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"), environ=env
    )
    run_operation(replace(request, session_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"), environ=env)
    with pytest.raises(SessionMigrateError, match="exists|overwrite"):
        commit_operation(prepared)
    prepared = prepare_operation(request, environ=env)
    database.rename(tmp_path / "old-mastra.db")
    run_operation(replace(request, session_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"), environ=env)
    with pytest.raises(SessionMigrateError, match="destination"):
        commit_operation(prepared)


def test_regular_file_never_becomes_future_directory_target_root(setup):
    request, env = setup
    prepared = prepare_operation(request, environ=env)
    request.home.write_bytes(b"unexpected regular file")
    with pytest.raises(SessionMigrateError, match="destination"):
        commit_operation(prepared)


def test_native_title_list_uses_prepared_project_cwd(setup, fake_cli, tmp_path, monkeypatch):
    request, env = setup
    project = tmp_path / "project"
    project.mkdir()
    from session_migrate import conversion

    original = conversion._run_opencode
    calls = []

    def run(command, environment, *, cwd=None):
        if cwd is not None:
            calls.append(cwd)
        return original(command, environment, cwd=cwd)

    monkeypatch.setattr(conversion, "_run_opencode", run)
    receipt = commit_operation(
        prepare_operation(
            replace(
                request,
                home=None,
                target_format="opencode",
                target_cli=fake_cli,
                cwd=project,
                target_title=TITLE,
            ),
            environ=env,
        )
    )
    assert receipt.title.persistence == "native_verified"
    assert calls == [project]
