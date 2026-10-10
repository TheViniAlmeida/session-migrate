"""Explicit CLI actions over actual private B3 records; no clients/providers."""

import json
import os
import weakref
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_c2_services as fixtures
from test_lineage_cli import tree_state

from session_migrate import adoption, operations, reconciliation
from session_migrate.cli import main
from session_migrate.continuity_bindings import PUBLIC_ENVIRONMENT
from session_migrate.conversion import default_migration_state_home
from session_migrate.lineage import LineageStore, NativeState, OperationState
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import (
    OperationKind,
    OperationRequest,
    commit_operation,
    prepare_operation,
)
from session_migrate.profiles import Profile, ProfileStore, SessionRef, prepare_profile_operation
from session_migrate.titles import title_receipt_path


def profile_recovery(tmp_path, monkeypatch, *, reverse=False, title=False, separate_home=False):
    pair, observer, _, catalog, paths = fixtures.adoption_fixture(tmp_path, TargetFormat.CODEX)
    environment = fixtures.environment(tmp_path)
    source = observer._profiles[1 if reverse else 0]
    source_path = paths[1 if reverse else 0]
    target = Profile(
        "installation",
        "Installation",
        AgentFormat.CLAUDE if reverse else AgentFormat.CODEX,
        tmp_path / "installation",
        environ={"HOME": str(tmp_path / "selected-profile-home")} if separate_home else {},
    )
    source_native_id = pair.target.native_id if reverse else pair.source.native_id
    ref = SessionRef(
        source.format,
        source.root,
        source_native_id,
        source.id,
        profile_fingerprint=source.fingerprint,
    )
    request = OperationRequest(
        OperationKind.TRANSFER,
        target_format=TargetFormat(target.format),
        session_id=fixtures.TARGET_ID,
        cwd=Path("/synthetic/work"),
        target_cli_version="2.1.209" if reverse else "0.144.4",
        target_title="synthetic alias" if title else None,
        title_policy=fixtures.TitlePolicy.LOCAL_ALIAS if title else None,
    )
    prepared = prepare_profile_operation(
        catalog, source, ref, target, request, base_environ=environment
    )
    manifest, input_path = prepared._state.built.manifest, prepared._state.built.output
    from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError

    def interrupt(*args, **kwargs):
        raise SessionMigrateError("synthetic B3 checkpoint interruption")

    with monkeypatch.context() as patch:
        patch.setattr(operations, "_completion_record", interrupt)
        with pytest.raises(OperationNeedsReconcileError) as caught:
            commit_operation(prepared)
    catalog.refresh(
        claude_roots=(target.root,) if reverse else (),
        codex_roots=(target.root,) if not reverse else (),
        include_auto=False,
    )
    catalog_path = catalog.path
    catalog.close()
    storage = profiles_file(tmp_path, (source, target))
    exact_environment(monkeypatch, environment)
    home = default_migration_state_home(environ=environment)
    operation_id = caught.value.operation_id
    args = [
        "--state-home",
        str(home),
        "reconcile",
        "transfer",
        "--profile-operation",
        "--operation-id",
        operation_id,
        "--source-file",
        str(source_path),
        "--input-file",
        str(input_path),
        "--manifest-file",
        str(manifest),
        "--current-catalog",
        str(catalog_path),
        "--profiles",
        str(storage),
        "--source-profile",
        source.id,
        "--target-profile",
        target.id,
        "--source-native-id",
        source_native_id,
        "--session-id",
        fixtures.TARGET_ID,
        "--cwd",
        str(request.cwd),
        "--target-cli-version",
        request.target_cli_version,
        "--json",
    ]
    if title:
        args += ["--target-title", request.target_title, "--local-title-alias"]
    return args, operation_id, home, (source_path, input_path), manifest


def exact_environment(monkeypatch, environment):
    for key in PUBLIC_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)


def profiles_file(tmp_path, profiles):
    directory = tmp_path / "private-profiles"
    directory.mkdir(mode=0o700)
    path = directory / "profiles.json"
    ProfileStore(path).create(profiles)
    return path


def recovery(tmp_path, monkeypatch, *, native=False, title=False, fail_after_title=False):
    operation_id, ledger, context, observer, catalog = fixtures.recovery_fixture(
        tmp_path, monkeypatch, native_source=native, title=title, fail_after_title=fail_after_title
    )
    storage = profiles_file(tmp_path, observer._profiles)
    catalog.close()
    exact_environment(monkeypatch, context.source_environment)
    return operation_id, ledger, context, storage


def recovery_args(fixture):
    operation_id, ledger, context, storage = fixture
    request = context.request
    args = ["--state-home", str(ledger.parent), "reconcile", request.command.value]
    if request.command is OperationKind.IMPORT:
        args += [str(request.path), "--format", request.source_format.value]
    else:
        args += [
            request.source_id,
            "--from",
            request.source_format.value,
            "--source-home",
            str(request.source_home),
            "--source-profile",
            "source",
        ]
    args += [
        "--to",
        request.target_format.value,
        "--home",
        str(request.home),
        "--session-id",
        request.session_id,
        "--cwd",
        str(request.cwd),
        "--target-cli-version",
        request.target_cli_version,
        "--operation-id",
        operation_id,
        "--source-file",
        str(context.source_path),
        "--input-file",
        str(context.input_path),
        "--manifest-file",
        str(context.manifest_path),
        "--current-catalog",
        str(storage.parent.parent / "catalog/state.sqlite"),
        "--profiles",
        str(storage),
        "--target-profile",
        "target",
        "--legacy-runtime-context",
        "--json",
    ]
    if request.target_title:
        args += ["--target-title", request.target_title, "--local-title-alias"]
    return args


def action_json(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "synthetic request" not in captured.out and "synthetic answer" not in captured.out
    assert '"handle"' not in captured.out
    return json.loads(captured.out)


@contextmanager
def cli_handle_scope(monkeypatch):
    """Own CLI capabilities must be released without disturbing another caller."""
    from session_migrate import recovery_cli

    modules = (reconciliation, adoption)
    baseline = {module: tuple(module._HANDLES) for module in modules}
    issued = []
    held = []  # Retain actual issued handles so public reuse can be tested.
    with monkeypatch.context() as patch:
        for name, module in (
            ("prepare_reconciliation", reconciliation),
            ("prepare_adoption", adoption),
        ):
            original = getattr(recovery_cli, name)

            def observed(*args, _original=original, _module=module, **kwargs):
                proposal = _original(*args, **kwargs)
                if proposal.handle is not None:
                    held.append(proposal.handle)
                    issued.append((_module, weakref.ref(proposal.handle), proposal.to_dict()))
                return proposal

            patch.setattr(recovery_cli, name, observed)
        yield issued
        for module in modules:
            assert {id(handle) for handle in module._HANDLES} == {
                id(handle) for handle in baseline[module]
            }
        for module, reference, metadata in issued:
            handle = reference()
            assert handle is not None and handle not in module._HANDLES
            assert "handle" not in metadata
            commit, error = (
                (reconciliation.commit_reconciliation, reconciliation.ReconciliationError)
                if module is reconciliation
                else (adoption.commit_adoption, adoption.AdoptionError)
            )
            with pytest.raises(error, match="invalid_handle"):
                commit(handle)


@pytest.mark.parametrize("native", [False, True])
def test_actual_ack_reconcile_prepares_without_writes_then_confirms_same_id(
    tmp_path, monkeypatch, capsys, native
):
    fixture = recovery(tmp_path, monkeypatch, native=native)
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    with cli_handle_scope(monkeypatch):
        assert main(recovery_args(fixture)) == 0
    document = action_json(capsys)
    assert document["action"] == "reconcile" and document["status"] == "ready", document["reason"]
    assert document["proposal"]["operation_id"] == fixture[0]
    assert document["update_available"] is False and document["native_cas_proven"] is False
    assert tree_state(tmp_path) == before
    with cli_handle_scope(monkeypatch):
        assert main(recovery_args(fixture) + ["--confirm"]) == 0
    document = action_json(capsys)
    assert document["confirmed"] and document["result"]["operation_id"] == fixture[0]
    operation = LineageStore.inspect_existing(fixture[1]).operations[0]
    assert operation.durable_state is OperationState.COMPLETED
    assert operation.durable_native_state is NativeState.ACKNOWLEDGED
    if not native:
        assert operation.source_checkpoint.identity is None
    native_paths = (fixture[2].source_path, fixture[2].input_path)
    for path in native_paths:
        assert (
            tree_state(tmp_path)[str(path.relative_to(tmp_path))]
            == before[str(path.relative_to(tmp_path))]
        )
    after = tree_state(tmp_path)
    with cli_handle_scope(monkeypatch):
        assert main(recovery_args(fixture)) == 0
    assert action_json(capsys)["status"] == "completed"
    assert tree_state(tmp_path) == after


@pytest.mark.parametrize(
    "change", ["source", "input", "options", "version", "environment", "profile"]
)
def test_wrong_recovery_context_blocks_confirm_without_writes(
    tmp_path, monkeypatch, capsys, change
):
    fixture = recovery(tmp_path, monkeypatch, native=True)
    args = recovery_args(fixture)
    fixtures.no_native(monkeypatch)
    if change in {"source", "input"}:
        path = fixture[2].source_path if change == "source" else fixture[2].input_path
        previous = path.stat()
        path.write_bytes(path.read_bytes().replace(b"synthetic answer", b"synthetic broken"))
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "options":
        args += ["--model-provider", "changed"]
    elif change == "version":
        args[args.index("--target-cli-version") + 1] = "0.153.4"
    elif change == "environment":
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "wrong-runtime-data"))
    else:
        args += ["--source-profile-name", "Contradictory current profile"]
    before = tree_state(tmp_path)
    with cli_handle_scope(monkeypatch):
        assert main(args + ["--confirm"]) == 1
    result = action_json(capsys)
    assert result["status"] == "blocked" and not result["confirmed"]
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("already_finalized", [False, True])
def test_alias_finalization_reuses_same_operation(tmp_path, monkeypatch, capsys, already_finalized):
    fixture = recovery(tmp_path, monkeypatch, title=True, fail_after_title=already_finalized)
    fixtures.no_native(monkeypatch)
    if already_finalized:
        receipt_before = title_receipt_path(fixture[2].manifest_path).read_bytes()
    assert main(recovery_args(fixture) + ["--confirm"]) == 0
    result = action_json(capsys)
    assert result["result"]["operation_id"] == fixture[0]
    path = title_receipt_path(fixture[2].manifest_path)
    document = json.loads(path.read_bytes())
    assert document["binding"]["proposal_id"] == fixture[0]
    if already_finalized:
        assert path.read_bytes() == receipt_before


def adoption_args(tmp_path, pair, catalog_path, storage, state_home):
    args = [
        "--state-home",
        str(state_home),
        "adopt",
        "--current-catalog",
        str(catalog_path),
        "--profiles",
        str(storage),
        "--source-profile",
        pair.source.profile_id,
        "--target-profile",
        pair.target.profile_id,
        "--source-native-id",
        pair.source.native_id,
        "--target-native-id",
        pair.target.native_id,
        "--source-client-version",
        "2.1.209",
        "--target-client-version",
        "2.1.209" if pair.target.format is AgentFormat.CLAUDE else "0.144.4",
        "--json",
    ]
    return args


@pytest.mark.parametrize("format", [TargetFormat.CLAUDE, TargetFormat.CODEX])
def test_adoption_default_is_readonly_confirm_writes_baseline_only(
    tmp_path, monkeypatch, capsys, format
):
    pair, observer, home, catalog, paths = fixtures.adoption_fixture(tmp_path, target_format=format)
    storage = profiles_file(tmp_path, observer._profiles)
    catalog_path = catalog.path
    catalog.close()
    fixtures.no_native(monkeypatch)
    args = adoption_args(tmp_path, pair, catalog_path, storage, home)
    before = tree_state(tmp_path)
    with cli_handle_scope(monkeypatch):
        assert main(args) == 0
    document = action_json(capsys)
    assert document["status"] == "ready", document["reason"]
    assert document["proposal"]["provenance"] == "adopted_now"
    assert document["proposal"]["boundary"] == "unproven"
    assert tree_state(tmp_path) == before
    with cli_handle_scope(monkeypatch):
        assert main(args + ["--confirm"]) == 0
    result = action_json(capsys)
    baseline_id = result["result"]["baseline_id"]
    assert result["proposal"]["baseline_id"] == baseline_id
    assert baseline_id != document["proposal"]["baseline_id"]
    assert result["result"]["kind"] == "baseline"
    assert not LineageStore.inspect_existing(home / "lineage-v1").operations
    after = tree_state(tmp_path)
    with cli_handle_scope(monkeypatch):
        assert (
            main(
                [
                    "--state-home",
                    str(home),
                    "lineage",
                    "baseline",
                    "--baseline-id",
                    baseline_id,
                    "--json",
                ]
            )
            == 0
        )
    inspected = action_json(capsys)
    assert inspected["result"]["record"]["provenance"] == "adopted_now"
    assert inspected["result"]["record"]["baseline_id"] == baseline_id
    assert tree_state(tmp_path) == after
    for path in paths:
        assert after[str(path.relative_to(tmp_path))] == before[str(path.relative_to(tmp_path))]


def test_unknown_baseline_and_cancel_ready_default_never_create_state(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "absent-state"
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    assert (
        main(
            ["--state-home", str(home), "lineage", "baseline", "--baseline-id", "missing", "--json"]
        )
        == 0
    )
    assert action_json(capsys)["status"] == "baseline_missing"
    args = [
        "--state-home",
        str(home),
        "lineage",
        "cancel-ready",
        "--operation-id",
        "missing",
        "--json",
    ]
    assert main(args) == 0
    assert action_json(capsys)["reason"] == "ledger_missing"
    assert main(args + ["--confirm"]) == 1
    assert action_json(capsys)["status"] == "blocked"
    assert tree_state(tmp_path) == before


def test_actual_b3_ready_interruption_cancels_without_native_publication(
    tmp_path, monkeypatch, capsys
):
    env = fixtures.environment(tmp_path)
    path = tmp_path / "source.jsonl"
    fixtures.write(path, fixtures.records())
    prepared = prepare_operation(
        OperationRequest(
            OperationKind.IMPORT,
            path=path,
            source_format=AgentFormat.CLAUDE,
            target_format=TargetFormat.CODEX,
            home=tmp_path / "target",
            session_id=fixtures.TARGET_ID,
            cwd=Path("/synthetic/work"),
            target_cli_version="0.144.4",
        ),
        environ=env,
    )

    class SyntheticCrash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise SyntheticCrash

    with monkeypatch.context() as patch:
        patch.setattr(LineageStore, "begin_publication", crash)
        with pytest.raises(SyntheticCrash):
            commit_operation(prepared)
    state_home = default_migration_state_home(environ=env)
    operation = LineageStore.inspect_existing(state_home / "lineage-v1").operations[0]
    assert operation.durable_state is OperationState.READY
    assert operation.durable_native_state is NativeState.NATIVE_NOT_STARTED
    fixtures.no_native(monkeypatch)
    args = [
        "--state-home",
        str(state_home),
        "lineage",
        "cancel-ready",
        "--operation-id",
        operation.record.operation_id,
        "--json",
    ]
    before = tree_state(tmp_path)
    assert main(args) == 0
    assert action_json(capsys)["reason"] == "recovered_ready"
    assert tree_state(tmp_path) == before
    assert main(args + ["--confirm"]) == 0
    result = action_json(capsys)
    assert result["status"] == "cancelled"
    assert result["result"]["record"]["operation_id"] == operation.record.operation_id
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["reconcile", "transfer"],
        ["reconcile", "import", "/synthetic/file"],
        ["adopt", "--confirm"],
        ["lineage", "cancel-ready"],
        ["lineage", "baseline"],
        ["lineage", "baseline", "--baseline-id", "one", "--confirm"],
        ["reconcile", "transfer", "--proposal-file", "/synthetic/proposal.json"],
    ],
)
def test_invalid_action_arguments_never_touch_state(tmp_path, monkeypatch, capsys, args):
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code == 2
    capsys.readouterr()
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("title", [False, True])
def test_profile_pipeline_reconcile_preserves_actual_catalog_binding_both_directions(
    tmp_path, monkeypatch, capsys, reverse, title
):
    args, operation_id, home, paths, manifest = profile_recovery(
        tmp_path, monkeypatch, reverse=reverse, title=title
    )
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    assert main(args) == 0
    document = action_json(capsys)
    assert document["status"] == "ready", document["reason"]
    assert document["proposal"]["operation_id"] == operation_id
    assert tree_state(tmp_path) == before
    assert main(args + ["--confirm"]) == 0
    result = action_json(capsys)
    assert result["result"]["operation_id"] == operation_id
    assert result["result"]["continuity_reason"] == "tracked"
    operation = LineageStore.inspect_existing(home / "lineage-v1").operations[0]
    assert operation.durable_state is OperationState.COMPLETED
    assert operation.record.source.identity.profile_fingerprint
    if title:
        assert (
            json.loads(title_receipt_path(manifest).read_bytes())["binding"]["proposal_id"]
            == operation_id
        )
    for path in paths:
        assert path.read_bytes()
        assert (
            tree_state(tmp_path)[str(path.relative_to(tmp_path))]
            == before[str(path.relative_to(tmp_path))]
        )


@pytest.mark.parametrize("change", ["alias_operation", "alias_title", "receipt", "manifest"])
def test_alias_conflicts_block_without_overwriting(tmp_path, monkeypatch, capsys, change):
    fixture = recovery(tmp_path, monkeypatch, title=True, fail_after_title=True)
    context = fixture[2]
    if change.startswith("alias"):
        path = next(
            (
                default_migration_state_home(environ=context.target_environment)
                / "title-aliases/v1"
            ).glob("*.json")
        )
        document = json.loads(path.read_bytes())
        document["proposal_id" if change == "alias_operation" else "alias"] = "conflict"
    elif change == "receipt":
        path = title_receipt_path(context.manifest_path)
        document = json.loads(path.read_bytes())
        document["binding"]["target_sha256"] = "f" * 64
    else:
        path = context.manifest_path
        document = json.loads(path.read_bytes())
        document["target"]["timestamp"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(document))
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    assert main(recovery_args(fixture) + ["--confirm"]) == 1
    assert action_json(capsys)["status"] == "blocked"
    assert tree_state(tmp_path) == before


def test_reconcile_persistence_fault_reports_and_inspects_the_same_id(
    tmp_path, monkeypatch, capsys
):
    fixture = recovery(tmp_path, monkeypatch, native=True)
    fixtures.no_native(monkeypatch)
    original = LineageStore.complete_reconciled

    def interrupted(store, *args, **kwargs):
        def fault(event):
            if event == "after_rename":
                raise OSError("synthetic persistence interruption")

        store._fault_hook = fault
        return original(store, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(LineageStore, "complete_reconciled", interrupted)
        assert main(recovery_args(fixture) + ["--confirm"]) == 1
    result = action_json(capsys)
    assert result["proposal"]["operation_id"] == fixture[0]
    assert result["status"] == "persistence_uncertain"
    assert (
        main(
            [
                "--state-home",
                str(fixture[1].parent),
                "lineage",
                "inspect",
                "--operation-id",
                fixture[0],
                "--json",
            ]
        )
        == 0
    )
    inspected = action_json(capsys)
    assert inspected["operation"]["durable_state"] == "completed"
    assert inspected["operation"]["record"]["operation_id"] == fixture[0]
    assert len(LineageStore.inspect_existing(fixture[1]).operations) == 1


def test_adoption_persistence_fault_inspects_its_original_baseline_id(
    tmp_path, monkeypatch, capsys
):
    from session_migrate.baselines import BaselineStore

    pair, observer, home, catalog, _ = fixtures.adoption_fixture(tmp_path)
    storage = profiles_file(tmp_path, observer._profiles)
    catalog_path = catalog.path
    catalog.close()
    args = adoption_args(tmp_path, pair, catalog_path, storage, home)
    fixtures.no_native(monkeypatch)
    original = BaselineStore.append

    def interrupted(store, *args, **kwargs):
        def fault(event):
            if event == "after_rename":
                raise OSError("synthetic baseline persistence interruption")

        store._fault_hook = fault
        return original(store, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(BaselineStore, "append", interrupted)
        assert main(args + ["--confirm"]) == 1
    document = action_json(capsys)
    assert document["status"] == "persistence_uncertain"
    baseline_id = document["proposal"]["baseline_id"]
    assert (
        main(
            [
                "--state-home",
                str(home),
                "lineage",
                "baseline",
                "--baseline-id",
                baseline_id,
                "--json",
            ]
        )
        == 0
    )
    result = action_json(capsys)
    assert result["result"]["record"]["baseline_id"] == baseline_id
    assert len(BaselineStore.inspect_existing(home / "baselines-v1").baselines) == 1


def test_unknown_native_attempt_never_recovers_as_acknowledged(tmp_path, monkeypatch, capsys):
    from session_migrate.catalog import Catalog
    from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError

    env = fixtures.environment(tmp_path)
    source = tmp_path / "original-source.jsonl"
    fixtures.write(source, fixtures.records())
    target = Profile("target", "Target", AgentFormat.CODEX, tmp_path / "target")
    request = OperationRequest(
        OperationKind.IMPORT,
        path=source,
        source_format=AgentFormat.CLAUDE,
        target_format=TargetFormat.CODEX,
        home=target.root,
        session_id=fixtures.TARGET_ID,
        cwd=Path("/synthetic/work"),
        target_cli_version="0.144.4",
    )
    prepared = prepare_operation(request, environ=env)
    input_file, manifest = prepared._state.built.output, prepared._state.built.manifest
    install = operations._install

    def interrupted(*args, **kwargs):
        if args[-1]:
            return install(*args, **kwargs)
        raise SessionMigrateError("synthetic unknown native attempt")

    with monkeypatch.context() as patch:
        patch.setattr(operations, "_install", interrupted)
        with pytest.raises(OperationNeedsReconcileError) as caught:
            commit_operation(prepared)
    home = default_migration_state_home(environ=env)
    operation = LineageStore.inspect_existing(home / "lineage-v1").operations[0]
    assert operation.durable_native_state is NativeState.ATTEMPTED_UNKNOWN
    catalog_path = tmp_path / "catalog/state.sqlite"
    with Catalog(catalog_path) as catalog:
        catalog.add_root(AgentFormat.CODEX, target.root)
    storage = profiles_file(tmp_path, (target,))
    exact_environment(monkeypatch, env)
    fixtures.no_native(monkeypatch)
    args = [
        "--state-home",
        str(home),
        "reconcile",
        "import",
        str(source),
        "--format",
        "claude",
        "--to",
        "codex",
        "--home",
        str(target.root),
        "--session-id",
        request.session_id,
        "--cwd",
        str(request.cwd),
        "--target-cli-version",
        request.target_cli_version,
        "--operation-id",
        caught.value.operation_id,
        "--source-file",
        str(source),
        "--input-file",
        str(input_file),
        "--manifest-file",
        str(manifest),
        "--profiles",
        str(storage),
        "--target-profile",
        "target",
        "--current-catalog",
        str(catalog_path),
        "--legacy-runtime-context",
        "--json",
    ]
    before = tree_state(tmp_path)
    assert main(args) == 0
    proposal = action_json(capsys)
    assert proposal["reason"] == "native_ack_required" and proposal["status"] == "blocked"
    assert main(args + ["--confirm"]) == 1
    assert action_json(capsys)["reason"] == "native_ack_required"
    assert tree_state(tmp_path) == before and not target.root.exists()


@pytest.mark.parametrize("selector", ["catalog_id", "title"])
def test_legacy_catalog_selectors_preserve_the_original_request(
    tmp_path, monkeypatch, capsys, selector
):
    from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError

    pair, observer, _, catalog, paths = fixtures.adoption_fixture(tmp_path, TargetFormat.CODEX)
    environment = fixtures.environment(tmp_path)
    source = observer._profiles[0]
    target = Profile("installation", "Installation", AgentFormat.CODEX, tmp_path / "installation")
    with paths[0].open("a") as file:
        file.write(
            json.dumps(
                {
                    "type": "custom-title",
                    "sessionId": pair.source.native_id,
                    "customTitle": "synthetic source selector title",
                }
            )
            + "\n"
        )
    catalog.refresh(claude_roots=(source.root,), include_auto=False)
    entry = next(row for row in catalog.list_sessions() if row.session_id == pair.source.native_id)
    request = OperationRequest(
        OperationKind.TRANSFER,
        catalog=catalog.path,
        catalog_id=entry.catalog_id if selector == "catalog_id" else None,
        title="synthetic source selector title" if selector == "title" else None,
        target_format=TargetFormat.CODEX,
        home=target.root,
        session_id=fixtures.TARGET_ID,
        cwd=Path("/synthetic/work"),
        target_cli_version="0.144.4",
    )
    prepared = prepare_operation(request, environ=environment)
    request = prepared._state.built.request
    manifest, input_file = prepared._state.built.manifest, prepared._state.built.output

    def interrupt(*args, **kwargs):
        raise SessionMigrateError("synthetic checkpoint interruption")

    with monkeypatch.context() as patch:
        patch.setattr(operations, "_completion_record", interrupt)
        with pytest.raises(OperationNeedsReconcileError) as caught:
            commit_operation(prepared)
    catalog.refresh(codex_roots=(target.root,), include_auto=False)
    catalog_path = catalog.path
    catalog.close()
    storage = profiles_file(tmp_path, (source, target))
    exact_environment(monkeypatch, environment)
    home = default_migration_state_home(environ=environment)
    args = [
        "--state-home",
        str(home),
        "--catalog",
        str(catalog_path),
        "reconcile",
        "transfer",
        "--catalog-id" if selector == "catalog_id" else "--title",
        entry.catalog_id if selector == "catalog_id" else request.title,
        "--from",
        request.source_format.value,
        "--to",
        "codex",
        "--home",
        str(target.root),
        "--session-id",
        request.session_id,
        "--cwd",
        str(request.cwd),
        "--target-cli-version",
        request.target_cli_version,
        "--operation-id",
        caught.value.operation_id,
        "--source-file",
        str(paths[0]),
        "--input-file",
        str(input_file),
        "--manifest-file",
        str(manifest),
        "--profiles",
        str(storage),
        "--source-profile",
        source.id,
        "--target-profile",
        target.id,
        "--legacy-runtime-context",
        "--json",
    ]
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    assert main(args) == 0
    result = action_json(capsys)
    assert result["status"] == "ready", result["reason"]
    assert "synthetic source selector title" not in json.dumps(result)
    assert tree_state(tmp_path) == before
    assert main(args + ["--confirm"]) == 0
    assert action_json(capsys)["result"]["operation_id"] == caught.value.operation_id


@pytest.mark.parametrize("format", [AgentFormat.OPENCODE, AgentFormat.KILO])
def test_adoption_unsupported_capacity_never_attempts_native_export(
    tmp_path, monkeypatch, capsys, format
):
    pair, observer, home, catalog, _ = fixtures.adoption_fixture(tmp_path)
    source = observer._profiles[0]
    root = tmp_path / format.value
    root.mkdir(mode=0o700)
    target = Profile("unsupported", "Unsupported", format, root)
    catalog.add_root(format, root)
    catalog_path = catalog.path
    catalog.close()
    storage = profiles_file(tmp_path, (source, target))
    fixtures.no_native(monkeypatch)
    args = [
        "--state-home",
        str(home),
        "adopt",
        "--current-catalog",
        str(catalog_path),
        "--profiles",
        str(storage),
        "--source-profile",
        source.id,
        "--target-profile",
        target.id,
        "--source-native-id",
        pair.source.native_id,
        "--target-native-id",
        "ses_private_synthetic",
        "--source-client-version",
        "2.1.209",
        "--target-client-version",
        "2.0.23",
        "--json",
    ]
    before = tree_state(tmp_path)
    assert main(args) == 0
    proposal = action_json(capsys)
    assert proposal["status"] == "blocked"
    assert proposal["native_read_effects"] == []
    assert main(args + ["--confirm"]) == 1
    assert action_json(capsys)["status"] == "blocked"
    assert tree_state(tmp_path) == before and not home.exists()


def test_default_state_namespace_precedes_current_profile_home(tmp_path, monkeypatch, capsys):
    args, operation_id, home, _, _ = profile_recovery(tmp_path, monkeypatch, separate_home=True)
    assert args[:2] == ["--state-home", str(home)]
    args = args[2:]
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    assert main(args) == 0
    result = action_json(capsys)
    assert result["status"] == "ready", result["reason"]
    assert result["proposal"]["operation_id"] == operation_id
    assert tree_state(tmp_path) == before
    assert main(args + ["--confirm"]) == 0
    assert action_json(capsys)["result"]["operation_id"] == operation_id
    assert (
        LineageStore.inspect_existing(home / "lineage-v1").operations[0].durable_state
        is OperationState.COMPLETED
    )


def test_current_catalog_environment_does_not_change_original_catalog_none(
    tmp_path, monkeypatch, capsys
):
    fixture = recovery(tmp_path, monkeypatch)
    args = recovery_args(fixture)
    index = args.index("--current-catalog")
    current = args[index + 1]
    del args[index : index + 2]
    assert fixture[2].request.catalog is None
    monkeypatch.setenv("SESSION_MIGRATE_CATALOG", current)
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    assert main(args) == 0
    result = action_json(capsys)
    assert result["status"] == "ready", result["reason"]
    assert tree_state(tmp_path) == before
    assert main(args + ["--confirm"]) == 0
    assert action_json(capsys)["result"]["operation_id"] == fixture[0]


def test_profile_original_catalog_cannot_contradict_current_catalog(tmp_path, monkeypatch, capsys):
    args, _, _, _, _ = profile_recovery(tmp_path, monkeypatch)
    args = ["--catalog", str(tmp_path / "different-original-catalog.sqlite"), *args]
    fixtures.no_native(monkeypatch)
    before = tree_state(tmp_path)
    with cli_handle_scope(monkeypatch):
        assert main(args) == 1
    result = action_json(capsys)
    assert result["status"] == "blocked" and result["proposal"] is None
    assert tree_state(tmp_path) == before
    with cli_handle_scope(monkeypatch):
        assert main(args + ["--confirm"]) == 1
    assert action_json(capsys)["proposal"] is None
    assert tree_state(tmp_path) == before


def cancel_ready_fixture(tmp_path, monkeypatch):
    env = fixtures.environment(tmp_path)
    path = tmp_path / "source.jsonl"
    fixtures.write(path, fixtures.records())
    prepared = prepare_operation(
        OperationRequest(
            OperationKind.IMPORT,
            path=path,
            source_format=AgentFormat.CLAUDE,
            target_format=TargetFormat.CODEX,
            home=tmp_path / "target",
            session_id=fixtures.TARGET_ID,
            cwd=Path("/synthetic/work"),
            target_cli_version="0.144.4",
        ),
        environ=env,
    )

    class SyntheticCrash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise SyntheticCrash

    with monkeypatch.context() as patch:
        patch.setattr(LineageStore, "begin_publication", crash)
        with pytest.raises(SyntheticCrash):
            commit_operation(prepared)
    home = default_migration_state_home(environ=env)
    operation = LineageStore.inspect_existing(home / "lineage-v1").operations[0]
    assert operation.durable_state is OperationState.READY
    assert operation.durable_native_state is NativeState.NATIVE_NOT_STARTED
    fixtures.no_native(monkeypatch)
    args = [
        "--state-home",
        str(home),
        "lineage",
        "cancel-ready",
        "--operation-id",
        operation.record.operation_id,
        "--json",
        "--confirm",
    ]
    return args, home, operation.record.operation_id, path


@pytest.mark.parametrize(
    "boundary",
    [
        "before_temp_write",
        "after_temp_write",
        "after_temp_fsync",
        "before_rename",
        "after_rename",
        "after_directory_fsync",
    ],
)
def test_cancel_ready_write_faults_preserve_actual_truth_and_original_id(
    tmp_path, monkeypatch, capsys, boundary
):
    import errno

    args, home, operation_id, source = cancel_ready_fixture(tmp_path, monkeypatch)
    source_state = tree_state(tmp_path)[str(source.relative_to(tmp_path))]
    ledger = home / "lineage-v1/ledger.sqlite3"
    before = ledger.read_bytes()
    opened = []
    real_open, cancel = os.open, LineageStore.cancel_ready

    def tracked_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def faulted(store, *args, **kwargs):
        def fault(event):
            if event == boundary:
                raise OSError("PRIVATE_CANCEL_ERROR_BODY_MUST_NOT_APPEAR")

        store._fault_hook = fault
        return cancel(store, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", tracked_open)
        patch.setattr(LineageStore, "cancel_ready", faulted)
        assert main(args) == 1
    document = action_json(capsys)
    assert document["action"] == "cancel_ready"
    assert document["proposal"]["operation_id"] == operation_id
    assert document["proposal"]["native_state"] == "native_not_started"
    assert document["confirmed"] is False and document["result"] is None
    assert "PRIVATE_CANCEL_ERROR_BODY_MUST_NOT_APPEAR" not in json.dumps(document)
    operation = LineageStore.inspect_existing(home / "lineage-v1").operations[0]
    assert operation.durable_native_state is NativeState.NATIVE_NOT_STARTED
    assert operation.receipt is None
    if boundary in {"after_rename", "after_directory_fsync"}:
        assert document["status"] == document["reason"] == "persistence_uncertain"
        assert operation.durable_state is OperationState.CANCELLED
        assert ledger.read_bytes() != before
    else:
        assert document["status"] == "blocked"
        assert document["reason"] == "image_write_failed"
        assert operation.durable_state is OperationState.READY
        assert ledger.read_bytes() == before
    assert tree_state(tmp_path)[str(source.relative_to(tmp_path))] == source_state
    assert not (tmp_path / "target").exists()
    for fd in opened:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize(
    "failure",
    [
        "oserror",
        "typed",
        "value",
        "type",
        "attribute",
        "missing",
        "ready_snapshot",
        "changed_record",
    ],
)
def test_cancel_success_without_matching_durable_result_is_uncertain(
    tmp_path, monkeypatch, capsys, failure
):
    from dataclasses import replace

    from session_migrate.errors import SessionMigrateError

    args, home, operation_id, _ = cancel_ready_fixture(tmp_path, monkeypatch)
    inspect = reconciliation.inspect_recovered_operation
    original = inspect(operation_id, store_path=home / "lineage-v1")

    def unavailable(*args, **kwargs):
        if failure == "oserror":
            raise OSError("PRIVATE_INSPECTION_BODY_MUST_NOT_APPEAR")
        if failure == "typed":
            raise SessionMigrateError("PRIVATE_TYPED_INSPECTION_BODY_MUST_NOT_APPEAR")
        if failure in {"value", "type", "attribute"}:
            raise {"value": ValueError, "type": TypeError, "attribute": AttributeError}[failure](
                "PRIVATE_INSPECTION_BODY_MUST_NOT_APPEAR"
            )
        if failure == "missing":
            return None
        if failure == "ready_snapshot":
            return original
        actual = inspect(*args, **kwargs)
        return replace(actual, record=replace(actual.record, options_sha256="f" * 64))

    with monkeypatch.context() as patch:
        patch.setattr("session_migrate.recovery_cli.inspect_recovered_operation", unavailable)
        assert main(args) == 1
    document = action_json(capsys)
    assert document["action"] == "cancel_ready"
    assert document["status"] == document["reason"] == "persistence_uncertain"
    assert document["proposal"]["operation_id"] == operation_id
    assert not document["confirmed"] and document["result"] is None
    assert "PRIVATE_" not in json.dumps(document)
    operation = inspect(operation_id, store_path=home / "lineage-v1")
    assert operation.durable_state is OperationState.CANCELLED
    assert operation.durable_native_state is NativeState.NATIVE_NOT_STARTED
    assert operation.record == original.record and operation.receipt is None


@pytest.mark.parametrize("error_type", [None, ValueError, TypeError, AttributeError])
def test_cancel_generic_failure_has_the_same_public_action_name(
    tmp_path, monkeypatch, capsys, error_type
):
    args, home, operation_id, _ = cancel_ready_fixture(tmp_path, monkeypatch)
    from session_migrate.errors import SessionMigrateError

    def failed(*args, **kwargs):
        raise (error_type or SessionMigrateError)("PRIVATE_NONCOMMIT_ERROR_BODY")

    before = tree_state(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr("session_migrate.recovery_cli.cancel_recovered_ready", failed)
        assert main(args) == 1
    document = action_json(capsys)
    assert document["action"] == "cancel_ready"
    assert document["status"] == "blocked" and document["reason"] == "continuity_action_failed"
    assert document["operation_id"] == operation_id
    assert "PRIVATE_NONCOMMIT_ERROR_BODY" not in json.dumps(document)
    assert tree_state(tmp_path) == before
    assert (
        LineageStore.inspect_existing(home / "lineage-v1").operations[0].durable_state
        is OperationState.READY
    )


def test_cli_releases_own_recovery_handle_while_another_caller_keeps_a_live_one(
    tmp_path, monkeypatch, capsys
):
    independent = tmp_path / "independent-recovery"
    own = tmp_path / "cli-recovery"
    independent.mkdir(mode=0o700)
    own.mkdir(mode=0o700)
    operation_id, ledger, context, observer, catalog = fixtures.recovery_fixture(
        independent, monkeypatch, native_source=True
    )
    proposal = reconciliation.prepare_reconciliation(
        operation_id, store_path=ledger, context=context, observer=observer
    )
    assert proposal.status == "ready" and proposal.handle is not None
    live = proposal.handle
    try:
        cli_fixture = recovery(own, monkeypatch, native=True)
        fixtures.no_native(monkeypatch)
        before = tree_state(tmp_path)
        with cli_handle_scope(monkeypatch) as issued:
            assert main(recovery_args(cli_fixture)) == 0
        assert action_json(capsys)["status"] == "ready"
        assert len(issued) == 1 and issued[0][0] is reconciliation
        assert live in reconciliation._HANDLES
        assert tree_state(tmp_path) == before
        receipt = reconciliation.commit_reconciliation(live)
        assert receipt.operation_id == operation_id
        assert live not in reconciliation._HANDLES
        with pytest.raises(reconciliation.ReconciliationError, match="invalid_handle"):
            reconciliation.commit_reconciliation(live)
    finally:
        live.discard()
        catalog.close()


def test_cli_releases_own_adoption_handle_while_another_caller_keeps_a_live_one(
    tmp_path, monkeypatch, capsys
):
    independent = tmp_path / "independent-adoption"
    own = tmp_path / "cli-adoption"
    independent.mkdir(mode=0o700)
    own.mkdir(mode=0o700)
    pair, observer, home, catalog, _ = fixtures.adoption_fixture(independent)
    proposal = fixtures.plan_adoption(pair, observer, home)
    assert proposal.status == "ready" and proposal.handle is not None
    live = proposal.handle
    try:
        cli_pair, cli_observer, cli_home, cli_catalog, _ = fixtures.adoption_fixture(own)
        storage = profiles_file(own, cli_observer._profiles)
        current_catalog = cli_catalog.path
        cli_catalog.close()
        args = adoption_args(own, cli_pair, current_catalog, storage, cli_home)
        fixtures.no_native(monkeypatch)
        before = tree_state(tmp_path)
        with cli_handle_scope(monkeypatch) as issued:
            assert main(args) == 0
        assert action_json(capsys)["status"] == "ready"
        assert len(issued) == 1 and issued[0][0] is adoption
        assert live in adoption._HANDLES
        assert tree_state(tmp_path) == before
        receipt = adoption.commit_adoption(live)
        assert receipt.baseline_id == proposal.baseline_id
        assert live not in adoption._HANDLES
        with pytest.raises(adoption.AdoptionError, match="invalid_handle"):
            adoption.commit_adoption(live)
    finally:
        live.discard()
        catalog.close()
