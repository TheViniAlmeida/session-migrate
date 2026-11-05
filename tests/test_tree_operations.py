"""Trees compile sealed per-node operations and never replay uncertain imports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from test_opencode_v22_native import STOCK_SHA256

from session_migrate import conversion, operations, tree_operations
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import CancellationToken, OperationKind, OperationRequest
from session_migrate.profiles import Profile
from session_migrate.tree_journal import TreeJournal, inspect_tree, journal_path
from session_migrate.tree_model import TreeSelection
from session_migrate.tree_operations import (
    commit_tree,
    prepare_tree,
    prepare_tree_resume,
    reconcile_tree,
    tree_proposal,
)
from session_migrate.tree_sources import inventory_from_explicit_manifest


@pytest.fixture
def tree_native(tmp_path):
    selected = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V22")
    if not selected:
        pytest.skip("set SESSION_MIGRATE_TEST_OPENCODE_V22 to the exact stock 2.2.5 ELF")
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap is required for private tree native publication")
    binary = Path(selected).resolve()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == STOCK_SHA256
    repo = Path(__file__).resolve().parents[1]
    test_dependencies = Path(pytest.__file__).resolve().parent.parent
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_SERVER_PASSWORD": "SYNTHETIC_LOCAL_TEST_PASSWORD",
        "OPENCODE_CONFIG_CONTENT": '{"warming":false,"disabled_providers":["opencode"]}',
    }
    for key in (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        path = tmp_path / key.lower()
        path.mkdir(mode=0o700)
        environment[key] = str(path)
    prefix = [
        "bwrap",
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--tmpfs",
        "/",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--dir",
        "/etc",
        "--ro-bind",
        "/etc/ssl",
        "/etc/ssl",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(tmp_path),
        str(tmp_path),
        "--ro-bind",
        str(binary),
        "/tmp/opencode-v22",
        "--ro-bind",
        str(repo / "src"),
        "/tmp/session-migrate-src",
        "--ro-bind",
        str(repo / "tests"),
        "/tmp/session-migrate-tests",
        "--ro-bind",
        str(test_dependencies),
        "/tmp/session-migrate-test-deps",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        str(tmp_path),
        "--",
    ]

    def run(script, *, worker=True):
        result = subprocess.run(
            prefix
            + [
                "/usr/bin/python3",
                "-I",
                "-B",
                "-c",
                "import sys;sys.path[:0]=['/tmp/session-migrate-src',"
                "'/tmp/session-migrate-tests','/tmp/session-migrate-test-deps'];" + script,
                str(tmp_path),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    return tmp_path, run


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    keys = ["parent", "child", "grandchild"]
    ids = {key: f"00000000-0000-4000-8000-{index:012d}" for index, key in enumerate(keys)}
    for key in keys:
        (root / (key + ".jsonl")).write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"00000000-0000-4000-9000-{keys.index(key):012d}",
                    "parentUuid": None,
                    "sessionId": ids[key],
                    "version": "2.1.318",
                    "isSidechain": False,
                    "cwd": str(project),
                    "timestamp": "2026-11-05T12:00:00Z",
                    "message": {"role": "user", "content": f"PRIVATE_BODY_SENTINEL {key}"},
                }
            )
            + "\n"
        )
    declaration = {
        "kind": "session-tree-inventory",
        "schema_version": 1,
        "root_node_key": "parent",
        "completeness": "declared_complete",
        "nodes": [
            {
                "key": key,
                "source_native_id": ids[key],
                "relative_path": key + ".jsonl",
                "title": key,
                "source_parent_status": "known_root" if key == "parent" else "known_parent",
            }
            for key in keys
        ],
        "relationships": [
            {
                "key": "edge_child",
                "parent_node_key": "parent",
                "child_node_key": "child",
                "kind": "fork_origin",
            },
            {
                "key": "edge_grandchild",
                "parent_node_key": "child",
                "child_node_key": "grandchild",
                "kind": "fork_origin",
            },
        ],
        "declaration": {
            "declaration_id": "synthetic",
            "declared_at": "2026-11-05T12:00:00Z",
            "operator": "test",
            "statement": "synthetic declared persistent forks",
            "confirmed": True,
        },
    }
    inventory = inventory_from_explicit_manifest(
        declaration, root=root, source_format="claude", client_version="2.1.318"
    )
    selection = TreeSelection("full_tree", "parent", tuple(keys), ("edge_child", "edge_grandchild"))
    environ = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for name in (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        folder = tmp_path / name.lower()
        folder.mkdir(mode=0o700)
        environ[name] = str(folder)
    target = Profile(
        "target",
        "Synthetic target",
        AgentFormat.OPENCODE,
        Path(environ["XDG_DATA_HOME"]) / "opencode",
        cli=Path("/usr/bin/true"),
    )
    native, imports = {}, []

    def import_bundle(cli, path, values, *, cwd=None):
        document = json.loads(path.read_bytes())
        info = document["info"]
        assert info["id"] not in native
        if info.get("parentID"):
            assert info["parentID"] in native
        native[info["id"]] = document
        imports.append(info["id"])
        target.root.mkdir(parents=True, exist_ok=True)

    def export_bundle(cli, session_id, path, values):
        if session_id not in native:
            raise SessionMigrateError("Synthetic exact target absent")
        path.write_text(json.dumps(native[session_id]))
        path.chmod(0o600)

    monkeypatch.setattr(conversion, "_opencode_version", lambda *_: "2.2.5")
    monkeypatch.setattr(
        conversion,
        "_opencode_session_exists",
        lambda _cli, native_id, _env, **_: native_id in native,
    )
    monkeypatch.setattr(conversion, "_invoke_opencode_import", import_bundle)
    monkeypatch.setattr(conversion, "_invoke_opencode_export", export_bundle)
    options = {
        "target": target,
        "cwd": project,
        "model_provider": "fixture",
        "model": "test",
        "state_home": tmp_path / "migration",
        "base_environ": environ,
        "acknowledge_partial": True,
    }
    return inventory, selection, options, native, imports


def test_compile_all_nodes_then_publish_parent_first_with_ledgers(setup):
    inventory, selection, options, native, imports = setup
    prepared = prepare_tree(inventory, selection, **options)
    proposal = tree_proposal(prepared)
    assert imports == []
    assert [node["key"] for node in proposal["nodes"]] == ["parent", "child", "grandchild"]
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed"
    assert imports == [node["target_id"] for node in proposal["nodes"]]
    assert native[imports[1]]["info"]["parentID"] == imports[0]
    assert native[imports[2]]["info"]["parentID"] == imports[1]
    view = inspect_tree(options["state_home"], proposal["operation_id"])
    assert view["status"] == "completed"
    assert all(node["operation_id"] for node in view["nodes"])
    journal = journal_path(options["state_home"], proposal["operation_id"]).read_text()
    assert "PRIVATE_BODY_SENTINEL" not in journal
    with pytest.raises(SessionMigrateError, match="consumed"):
        commit_tree(prepared)


def test_changed_last_source_blocks_every_native_write(setup):
    inventory, selection, options, _, imports = setup
    prepared = prepare_tree(inventory, selection, **options)
    path = Path(inventory.canonical_root) / "grandchild.jsonl"
    path.write_text(path.read_text().replace("PRIVATE_BODY_SENTINEL", "CHANGED"))
    with pytest.raises(SessionMigrateError, match="changed"):
        commit_tree(prepared)
    assert imports == []
    assert not journal_path(options["state_home"], tree_proposal(prepared)["operation_id"]).exists()


def test_collision_in_child_after_prepare_blocks_parent(setup):
    inventory, selection, options, native, imports = setup
    prepared = prepare_tree(inventory, selection, **options)
    child = tree_proposal(prepared)["nodes"][1]["target_id"]
    native[child] = {"info": {"id": child}}
    with pytest.raises(SessionMigrateError, match="already exists"):
        commit_tree(prepared)
    assert imports == []


def test_uncertain_parent_stops_descendants_and_resume_never_reimports(setup, monkeypatch):
    inventory, selection, options, _, imports = setup
    real_import = conversion._invoke_opencode_import

    def crash(*args, **kwargs):
        real_import(*args, **kwargs)
        raise SessionMigrateError("Synthetic failure after native write")

    monkeypatch.setattr(conversion, "_invoke_opencode_import", crash)
    prepared = prepare_tree(inventory, selection, **options)
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "needs_reconcile" and len(imports) == 1
    view = reconcile_tree(
        options["state_home"],
        result["operation_id"],
        target=options["target"],
        base_environ=options["base_environ"],
    )
    assert view["nodes"][0]["observation"]["status"] == "present"
    assert len(imports) == 1
    with pytest.raises(SessionMigrateError, match="reconciliation"):
        prepare_tree_resume(inventory, selection, operation_id=result["operation_id"], **options)
    assert len(imports) == 1


def test_cancel_between_nodes_then_explicit_resume_remaining(setup, monkeypatch):
    inventory, selection, options, _, imports = setup
    token = CancellationToken()
    real_commit = operations.commit_operation

    def commit(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        token.request_cancel()
        return result

    monkeypatch.setattr(operations, "commit_operation", commit)
    prepared = prepare_tree(inventory, selection, **options)
    result = commit_tree(prepared, cancellation=token).to_dict()
    assert result["status"] == "partial" and len(imports) == 1
    monkeypatch.setattr(operations, "commit_operation", real_commit)
    resumed = prepare_tree_resume(
        inventory, selection, operation_id=result["operation_id"], **options
    )
    assert len(tree_proposal(resumed)["nodes"]) == 3
    result = commit_tree(resumed).to_dict()
    assert result["status"] == "completed" and len(imports) == 3


def test_partial_projection_requires_explicit_acknowledgement(setup):
    inventory, selection, options, _, imports = setup
    prepared = prepare_tree(inventory, selection, **{**options, "acknowledge_partial": False})
    proposal = tree_proposal(prepared)
    if proposal["requires_partial_acknowledgement"]:
        with pytest.raises(SessionMigrateError, match="acknowledge"):
            commit_tree(prepared)
        assert imports == []


def test_authority_guard_revocation_before_first_write(setup):
    inventory, selection, options, _, imports = setup
    revoked = False

    def guard():
        if revoked:
            raise SessionMigrateError("Synthetic revoked review")

    prepared = prepare_tree(inventory, selection, prepublication_check=guard, **options)
    revoked = True
    with pytest.raises(SessionMigrateError, match="revoked"):
        commit_tree(prepared)
    assert imports == []


def test_discarded_and_forged_handles_do_not_publish(setup):
    inventory, selection, options, _, imports = setup
    prepared = prepare_tree(inventory, selection, **options)
    document = tree_proposal(prepared)
    document["partial_acknowledged"] = True
    prepared.discard()
    with pytest.raises(SessionMigrateError, match="consumed"):
        commit_tree(prepared)
    with pytest.raises(SessionMigrateError, match="capability"):
        commit_tree(document)
    assert imports == []


def test_per_node_budget_stops_before_target_probe(setup, monkeypatch):
    inventory, selection, options, _, imports = setup

    def unexpected_probe(*_):
        pytest.fail("target probe ran after an exhausted source byte budget")

    monkeypatch.setattr(conversion, "_opencode_version", unexpected_probe)
    with pytest.raises(SessionMigrateError, match="byte|limit|budget"):
        prepare_tree(inventory, selection, max_source_bytes=64, **options)
    assert imports == []


def test_total_budget_includes_retained_native_artifacts(setup):
    inventory, selection, options, _, imports = setup
    physical_bytes = sum(
        (Path(inventory.canonical_root) / node.relative_path).stat().st_size
        for node in inventory.nodes
    )
    with pytest.raises(SessionMigrateError, match="byte|limit|budget"):
        prepare_tree(inventory, selection, max_total_bytes=physical_bytes + 32, **options)
    assert imports == []


@pytest.mark.parametrize("value", [0, -1, True, 2**40])
def test_invalid_capture_budgets_fail_before_publication(setup, value):
    inventory, selection, options, _, imports = setup
    with pytest.raises(SessionMigrateError, match="budget"):
        prepare_tree(inventory, selection, max_source_bytes=value, **options)
    assert imports == []


def test_changed_completed_node_blocks_explicit_resume(setup, monkeypatch):
    inventory, selection, options, native, imports = setup
    token = CancellationToken()
    real_commit = operations.commit_operation

    def stop_after_parent(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        token.request_cancel()
        return result

    monkeypatch.setattr(operations, "commit_operation", stop_after_parent)
    result = commit_tree(
        prepare_tree(inventory, selection, **options), cancellation=token
    ).to_dict()
    monkeypatch.setattr(operations, "commit_operation", real_commit)
    native[imports[0]]["info"]["title"] = "changed after publication"
    with pytest.raises(SessionMigrateError, match="changed"):
        prepare_tree_resume(inventory, selection, operation_id=result["operation_id"], **options)
    assert len(imports) == 1


def test_parent_removed_after_completion_never_imports_child(setup, monkeypatch):
    inventory, selection, options, native, imports = setup
    real_commit = operations.commit_operation

    def remove_parent(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        native.clear()
        return result

    monkeypatch.setattr(operations, "commit_operation", remove_parent)
    result = commit_tree(prepare_tree(inventory, selection, **options)).to_dict()
    assert result["status"] == "needs_reconcile"
    assert len(imports) == 1


def test_journal_failure_after_inner_completion_preserves_uncertainty(setup, monkeypatch):
    inventory, selection, options, _, imports = setup
    real_append = tree_operations.TreeJournal.append

    def fail_completion(store, event):
        if event["type"] in {"completed", "needs_reconcile"}:
            raise SessionMigrateError("Synthetic journal persistence failure")
        return real_append(store, event)

    monkeypatch.setattr(tree_operations.TreeJournal, "append", fail_completion)
    result = commit_tree(prepare_tree(inventory, selection, **options)).to_dict()
    assert result["status"] == "needs_reconcile"
    assert len(imports) == 1
    assert (
        inspect_tree(options["state_home"], result["operation_id"])["status"] == "needs_reconcile"
    )


def test_changed_parent_relation_after_native_ack_stops_grandchild(setup, monkeypatch):
    inventory, selection, options, native, imports = setup
    real_import = conversion._invoke_opencode_import

    def change_parent(*args, **kwargs):
        real_import(*args, **kwargs)
        if len(imports) == 2:
            native[imports[-1]]["info"]["parentID"] = "ses_" + "f" * 32

    monkeypatch.setattr(conversion, "_invoke_opencode_import", change_parent)
    result = commit_tree(prepare_tree(inventory, selection, **options)).to_dict()
    assert result["status"] == "needs_reconcile"
    assert [node["status"] for node in result["nodes"]] == [
        "completed",
        "needs_reconcile",
        "unattempted",
    ]
    assert len(imports) == 2


@pytest.mark.parametrize("changed_key", ["parent", "grandchild"])
def test_new_completed_target_is_guarded_before_descendants_and_final(
    setup, monkeypatch, changed_key
):
    inventory, selection, options, native, imports = setup
    original_append = TreeJournal.append

    def mutate_after_completion(store, event):
        result = original_append(store, event)
        if event["type"] == "completed" and event["node_key"] == changed_key:
            native[imports[-1]]["info"]["title"] = "Changed after completion receipt"
        return result

    monkeypatch.setattr(TreeJournal, "append", mutate_after_completion)
    result = commit_tree(prepare_tree(inventory, selection, **options)).to_dict()
    assert result["status"] == "needs_reconcile"
    assert len(imports) == (1 if changed_key == "parent" else 3)
    assert (
        next(node for node in result["nodes"] if node["key"] == changed_key)["status"]
        == "needs_reconcile"
    )
    assert (
        inspect_tree(options["state_home"], result["operation_id"])["status"] == "needs_reconcile"
    )


@pytest.mark.parametrize("wrong_binding", ["source", "source_path", "title", "model"])
def test_resume_rejects_legitimate_completion_for_wrong_source_or_options(setup, wrong_binding):
    inventory, selection, options, _, imports = setup
    prepared = prepare_tree(inventory, selection, **options)
    proposal = tree_proposal(prepared)
    fingerprint = prepared._state.plan_sha256
    prepared.discard()
    parent = proposal["nodes"][0]
    target = options["target"]
    source = Path(inventory.canonical_root) / "parent.jsonl"
    if wrong_binding == "source":
        source = source.with_name("child.jsonl")
    elif wrong_binding == "source_path":
        clone = source.with_name("identical-parent-copy.jsonl")
        clone.write_bytes(source.read_bytes())
        source = clone
    request = OperationRequest(
        command=OperationKind.IMPORT,
        path=source,
        source_format=AgentFormat.CLAUDE,
        source_home=Path(inventory.canonical_root),
        target_format=TargetFormat.OPENCODE,
        session_id=str(uuid.UUID(hex=parent["target_id"][4:])),
        cwd=options["cwd"],
        target_cli=target.cli,
        target_cli_version="2.2.5",
        model_provider="fixture",
        model="WRONG_MODEL" if wrong_binding == "model" else "test",
        target_title="WRONG_TITLE" if wrong_binding == "title" else "parent",
        reviewed_tree_projection=True,
    )
    wrong_handle = operations.prepare_operation(
        request,
        environ=options["base_environ"],
        source_environ=options["base_environ"],
        migration_state_home=options["state_home"],
        profile_context={
            "target_profile_id": target.id,
            "target_profile_name": target.name,
            "target_profile_fingerprint": target.fingerprint,
        },
    )
    wrong_proposal = operations.proposal_metadata(wrong_handle).to_dict()
    operations.commit_operation(wrong_handle)
    fields = (
        "key",
        "target_id",
        "parent_target_id",
        "source_sha256",
        "target_sha256",
        "operation_id",
    )
    nodes = [{key: node[key] for key in fields} for node in proposal["nodes"]]
    nodes[0].update(
        operation_id=wrong_proposal["proposal_id"], target_sha256=wrong_proposal["target"]["sha256"]
    )
    observed = tree_operations._observe(
        target, options["base_environ"], nodes[0], cwd=options["cwd"]
    )
    with TreeJournal(options["state_home"], proposal["operation_id"], create=True) as journal:
        journal.append(
            {
                "type": "prepared",
                "operation_id": proposal["operation_id"],
                "plan_sha256": fingerprint,
                "target_root": str(target.root),
                "nodes": nodes,
            }
        )
        journal.append(
            {
                "type": "attempting",
                "node_key": "parent",
                "node_operation_id": nodes[0]["operation_id"],
            }
        )
        journal.append(
            {
                "type": "completed",
                "node_key": "parent",
                "node_operation_id": nodes[0]["operation_id"],
                "observed_sha256": observed,
            }
        )
        journal.append({"type": "cancelled"})
    with pytest.raises(tree_operations.TreeOperationError, match="binding changed"):
        prepare_tree_resume(inventory, selection, operation_id=proposal["operation_id"], **options)
    assert len(imports) == 1


def test_resume_preserves_unknown_coverage_when_journal_has_no_audit_details(setup):
    from test_tree_cli import declaration

    inventory, selection, options, _, imports = setup
    for node in inventory.nodes:
        path = Path(inventory.canonical_root) / node.relative_path
        row = json.loads(path.read_bytes())
        row["future_field"] = {"synthetic": "unmapped"}
        path.write_text(json.dumps(row) + "\n")
    inventory = inventory_from_explicit_manifest(
        declaration(inventory),
        root=Path(inventory.canonical_root),
        source_format="claude",
        client_version="2.1.318",
    )
    prepared = prepare_tree(inventory, selection, **options)
    proposal = tree_proposal(prepared)
    assert proposal["coverage"] == "unknown"
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed"
    assert result["coverage"] == "unknown"
    resumed = prepare_tree_resume(
        inventory, selection, operation_id=proposal["operation_id"], **options
    )
    after = tree_proposal(resumed)
    resumed.discard()
    assert after["coverage"] == "unknown"
    assert all(node["coverage"] == "unknown" and node["losses"] is None for node in after["nodes"])
    assert len(imports) == 3


def test_resume_rejects_replaced_source_with_identical_bytes(setup, monkeypatch):
    inventory, selection, options, _, imports = setup
    token = CancellationToken()
    real_commit = operations.commit_operation

    def stop_after_parent(*args, **kwargs):
        receipt = real_commit(*args, **kwargs)
        token.request_cancel()
        return receipt

    monkeypatch.setattr(operations, "commit_operation", stop_after_parent)
    result = commit_tree(
        prepare_tree(inventory, selection, **options), cancellation=token
    ).to_dict()
    monkeypatch.setattr(operations, "commit_operation", real_commit)
    source = Path(inventory.canonical_root) / "parent.jsonl"
    source.rename(source.with_suffix(".original"))
    source.write_bytes(source.with_suffix(".original").read_bytes())
    with pytest.raises(tree_operations.TreeOperationError, match="binding changed"):
        prepare_tree_resume(inventory, selection, operation_id=result["operation_id"], **options)
    assert len(imports) == 1


@pytest.mark.parametrize("compressed", [False, True])
def test_completed_codex_resume_binds_composite_authority_and_physical_leaf(setup, compressed):
    from test_codex_storage import CHILD_ID, ROOT_ID, header, history_base, message, write_rollout

    from session_migrate import codex_storage
    from session_migrate.tree_sources import inventory_from_native_metadata

    if compressed and codex_storage._stdlib_zstd() is None:
        pytest.skip("this mutation regression needs the stdlib synthetic fixture encoder")
    old_inventory, _, options, _, imports = setup
    root = Path(old_inventory.canonical_root).parent / "codex-store"
    root.mkdir()
    parent = write_rollout(
        root,
        ROOT_ID,
        [header(source="cli"), message(1, "shared parent")],
        compressed=compressed,
        archived=True,
    )
    child = write_rollout(
        root,
        CHILD_ID,
        [
            header(
                CHILD_ID,
                base=history_base(parent, 2),
                source="cli",
                forked_from_id=ROOT_ID,
                forked_from_ordinal_exclusive=2,
                attached_thread=False,
            ),
            message(3, "own child"),
        ],
    )
    inventory = inventory_from_native_metadata(
        root=root, source_format="codex", client_version="0.166.0"
    )
    child_key = next(node.key for node in inventory.nodes if node.source_native_id == CHILD_ID)
    selection = TreeSelection(
        "single",
        child_key,
        (child_key,),
        tuple(edge.key for edge in inventory.relationships),
        boundary_policy="detach_explicit",
    )
    prepared = prepare_tree(inventory, selection, **options)
    proposal = tree_proposal(prepared)
    assert proposal["nodes"][0]["source_sha256"] == hashlib.sha256(child.read_bytes()).hexdigest()
    assert proposal["nodes"][0]["source_authority_sha256"] != proposal["nodes"][0]["source_sha256"]
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed"
    resumed = prepare_tree_resume(
        inventory, selection, operation_id=result["operation_id"], **options
    )
    resumed.discard()
    assert len(imports) == 1
    # A base component is outside the selected node set. Its physical identity
    # still belongs to the completed operation's guarded composite witness.
    parent.write_bytes(parent.read_bytes())
    with pytest.raises(tree_operations.TreeOperationError, match="binding changed"):
        prepare_tree_resume(inventory, selection, operation_id=result["operation_id"], **options)


def test_two_clean_cancellations_can_resume_with_each_fresh_artifact_hash(setup, monkeypatch):
    inventory, selection, options, _, imports = setup
    original_commit = operations.commit_operation
    token = CancellationToken()

    def stop_after_one(*args, **kwargs):
        receipt = original_commit(*args, **kwargs)
        token.request_cancel()
        return receipt

    monkeypatch.setattr(operations, "commit_operation", stop_after_one)
    first = commit_tree(prepare_tree(inventory, selection, **options), cancellation=token).to_dict()
    assert first["status"] == "partial" and len(imports) == 1
    token = CancellationToken()
    second_prepared = prepare_tree_resume(
        inventory, selection, operation_id=first["operation_id"], **options
    )
    second_proposal = tree_proposal(second_prepared)
    second = commit_tree(second_prepared, cancellation=token).to_dict()
    assert second["status"] == "partial" and len(imports) == 2
    child_journal = next(
        node
        for node in inspect_tree(options["state_home"], first["operation_id"])["nodes"]
        if node["key"] == "child"
    )
    child_proposal = next(node for node in second_proposal["nodes"] if node["key"] == "child")
    assert child_journal["target_sha256"] == child_proposal["target_sha256"]
    monkeypatch.setattr(operations, "commit_operation", original_commit)
    third = prepare_tree_resume(inventory, selection, operation_id=first["operation_id"], **options)
    assert commit_tree(third).status == "completed" and len(imports) == 3


@pytest.mark.parametrize("connected_server", [False, True])
@pytest.mark.parametrize("source_version", ["2.1.318", "2.1.309"])
def test_stock_225_three_levels_cold_export_and_child_collision(
    tree_native, connected_server, source_version
):
    _, run = tree_native
    script = r"""
import json, os, subprocess, sys
from pathlib import Path
from dataclasses import replace
import pytest
from test_tree_operations import setup
from session_migrate.tree_operations import prepare_tree,commit_tree,tree_proposal
from session_migrate.conversion import _opencode_session_exists,_opencode_session_ids
from session_migrate.tree_journal import inspect_tree
# The fixture masks /tmp with a fresh namespace-local tmpfs. Restore its
# standard sticky bit so the production ancestry guard can run unchanged.
Path('/tmp').chmod(0o1777)
for ancestor in Path(sys.argv[1]).parents:
    if ancestor == Path('/tmp'):
        break
    ancestor.chmod(0o700)
root=Path(sys.argv[1])/'tree-runtime'
root.mkdir(mode=0o700)
patch=pytest.MonkeyPatch()
inventory,selection,options,_,_=setup.__wrapped__(root,patch)
patch.undo()
from session_migrate.tree_sources import inventory_from_explicit_manifest
from session_migrate.tree_model import TreeSelection
# A real fork keeps its copied prefix in the child's independent transcript.
# The publisher must preserve it once, without injecting the parent's history.
source_root=Path(inventory.canonical_root)
parent_row=json.loads((source_root/'parent.jsonl').read_text())
child_row=json.loads((source_root/'child.jsonl').read_text())
copied_parent={**parent_row,'sessionId':child_row['sessionId']}
child_row['parentUuid']=copied_parent['uuid']
(source_root/'child.jsonl').write_text(json.dumps(copied_parent)+'\n'+json.dumps(child_row)+'\n')
sibling={**parent_row,'sessionId':'00000000-0000-4000-8000-000000000003',
    'uuid':'00000000-0000-4000-9000-000000000003',
    'message':{'role':'user','content':'PRIVATE_BODY_SENTINEL sibling'}}
(source_root/'sibling.jsonl').write_text(json.dumps(sibling)+'\n')
declaration={'kind':'session-tree-inventory','schema_version':1,'root_node_key':'parent',
    'completeness':'declared_complete',
    'nodes':[{'key':node.key,'source_native_id':node.source_native_id,
        'relative_path':node.relative_path,'title':node.title,
        'source_parent_status':node.source_parent_status} for node in inventory.nodes]+
        [{'key':'sibling','source_native_id':sibling['sessionId'],'relative_path':'sibling.jsonl',
          'title':'sibling','source_parent_status':'known_parent'}],
    'relationships':[{'key':edge.key,'parent_node_key':edge.parent_node_key,
        'child_node_key':edge.child_node_key,'kind':edge.kind} for edge in inventory.relationships]+
        [{'key':'edge_sibling','parent_node_key':'parent','child_node_key':'sibling','kind':'fork_origin'}],
    'declaration':{'declaration_id':'four_nodes','declared_at':'2026-11-05T12:00:00Z',
        'operator':'test','statement':'synthetic persistent forks with a copied child prefix',
        'confirmed':True}}
# Exercise Desktop envelope copies on the real importer without reading a user store.
for path in source_root.glob('*.jsonl'):
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row['version']=SOURCE_VERSION
    if SOURCE_VERSION=='2.1.309':
        # A late copy of the oldest message must not become the fallback leaf.
        rows.append({**rows[0],'cwd':'/fixture/other','slug':'copied-envelope',
            'promptId':'copied-prompt','toolUseResult':{'unprojected':'WRAPPER_ONLY_SENTINEL'}})
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
inventory=inventory_from_explicit_manifest(declaration,root=source_root,source_format='claude',
    client_version=SOURCE_VERSION)
selection=TreeSelection('full_tree','parent',('parent','child','grandchild','sibling'),
    ('edge_child','edge_grandchild','edge_sibling'))
options['target']=replace(options['target'],cli=Path('/tmp/opencode-v22'))
options['base_environ']={**os.environ,**options['base_environ']}
def mark(value):
    with (root/'phases.log').open('a') as output:
        output.write(value+'\n')
from offline_provider import offline_provider
provider_context=offline_provider('TREE_CHILD_RESUMED')
provider=provider_context.__enter__()
(options['cwd']/'opencode.json').write_text(json.dumps({
    'warming':False,'disabled_providers':['opencode'],'providers':{'fixture':{
        'package':'@opencode/ai/providers/openai-compatible',
        'settings':{'apiKey':'fake-only','baseURL':f'http://127.0.0.1:{provider.server_port}/v1'},
        'models':{'test':{'limit':{'context':64000,'output':4096}}}
    }}}))
server = None
if CONNECTED_SERVER:
    import socket,time,urllib.request,base64
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    environment=options['target'].environment(options['base_environ'])
    environment['XDG_DATA_HOME']=str(options['target'].root.parent)
    server=subprocess.Popen(['/tmp/opencode-v22','serve','--hostname','127.0.0.1','--port',str(port)],
        cwd=options['cwd'],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+15
    for _ in range(200):
        if server.poll() is not None: raise AssertionError('synthetic server exited')
        try:
            auth=('opencode:'+environment['OPENCODE_SERVER_PASSWORD']).encode()
            token=base64.b64encode(auth).decode()
            request=urllib.request.Request(origin+'/api/info',
                headers={'Authorization':'Basic '+token})
            with urllib.request.urlopen(request,timeout=1) as response:
                assert json.load(response)['version']=='2.2.5'
            break
        except OSError:
            if time.monotonic()>deadline: raise AssertionError('synthetic server failed readiness')
            time.sleep(.05)
    else: raise AssertionError('synthetic server failed readiness')
    options['target']=replace(options['target'],client='openchamber',server=origin)
mark('preparing')
prepared=prepare_tree(inventory,selection,**options)
proposal=tree_proposal(prepared)
if SOURCE_VERSION=='2.1.309':
    assert proposal['coverage']=='unknown'
    assert all(node['coverage']=='unknown' and node['losses'] for node in proposal['nodes'])
mark('committing')
result=commit_tree(prepared,progress=lambda event: mark(event.stage.value)).to_dict()
assert result['status']=='completed',result
mark('committed')
os.chdir(options['cwd'])
environment=options['target'].environment(options['base_environ'])
environment['XDG_DATA_HOME']=str(options['target'].root.parent)
transport=['--standalone']
if CONNECTED_SERVER:
    environment['SESSION_MIGRATE_OPENCODE_SERVER']=origin
    transport=['--server',origin]
ids=[node['target_id'] for node in proposal['nodes']]
if not CONNECTED_SERVER:
    assert _opencode_session_ids(options['target'].cli,environment)=={ids[0]}
else:
    assert _opencode_session_ids(options['target'].cli,environment)==set(ids)
assert all(_opencode_session_exists(options['target'].cli,value,environment) for value in ids)
assert not _opencode_session_exists(options['target'].cli,'ses_'+'e'*32,environment)
assert inspect_tree(options['state_home'],result['operation_id'])['status']=='completed'
for index,native_id in enumerate(ids):
    export=subprocess.run(['/tmp/opencode-v22','session','export',native_id,*transport],
        env=environment,capture_output=True,text=True,check=True,timeout=60)
    document=json.loads(export.stdout)
    assert document['info'].get('parentID')==proposal['nodes'][index]['parent_target_id']
    assert document['messages']
    if index==0: parent_before=document
    if index==1: child_before=document
assert json.dumps(child_before).count('PRIVATE_BODY_SENTINEL parent')==1
assert json.dumps(child_before).count('PRIVATE_BODY_SENTINEL child')==1
if server is not None:
    # Reopen the same native store in a fresh server process before continuation.
    server.terminate()
    server.wait(timeout=10)
    server=subprocess.Popen(['/tmp/opencode-v22','serve','--hostname','127.0.0.1','--port',str(port)],
        cwd=options['cwd'],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if server.poll() is not None: raise AssertionError('cold synthetic server exited')
        try:
            with urllib.request.urlopen(request,timeout=1) as response:
                assert json.load(response)['version']=='2.2.5'
            break
        except OSError: time.sleep(.05)
    else: raise AssertionError('cold synthetic server failed readiness')
    assert all(_opencode_session_exists(options['target'].cli,value,environment) for value in ids)
mark('resuming')
resumed=subprocess.run(['/tmp/opencode-v22','run','--session',ids[1],'--model','fixture/test',
    '--format','json','Continue this persistent child independently',*transport],
    cwd=options['cwd'],env=environment,capture_output=True,text=True,timeout=60)
assert resumed.returncode==0,resumed.stderr
mark('resumed')
child_after=json.loads(subprocess.run(['/tmp/opencode-v22','session','export',ids[1],*transport],
    env=environment,capture_output=True,text=True,check=True,timeout=60).stdout)
parent_after=json.loads(subprocess.run(['/tmp/opencode-v22','session','export',ids[0],*transport],
    env=environment,capture_output=True,text=True,check=True,timeout=60).stdout)
assert parent_after==parent_before
assert child_after['info']['parentID']==ids[0]
assert len(child_after['messages'])>len(child_before['messages'])
assert provider.requests
replay=json.dumps(provider.requests)
assert 'PRIVATE_BODY_SENTINEL child' in replay
assert replay.count('PRIVATE_BODY_SENTINEL parent')==1
assert 'PRIVATE_BODY_SENTINEL grandchild' not in replay
assert 'PRIVATE_BODY_SENTINEL sibling' not in replay
assert 'WRAPPER_ONLY_SENTINEL' not in replay
if server is not None:
    server.terminate()
    server.wait(timeout=10)
provider_context.__exit__(None,None,None)
print(json.dumps({'status':'completed','nodes':len(ids),'cold_export_parent_chain':True,
    'exact_child_collision_probe':True,'private_journal':True,'independent_child_resumable':True}))
"""
    result = json.loads(
        run(
            f"CONNECTED_SERVER={connected_server!r}\nSOURCE_VERSION={source_version!r}\n" + script,
            worker=True,
        )
    )
    assert result["nodes"] == 4 and result["exact_child_collision_probe"]


def test_reviewed_tree_publishes_known_mixed_claude_versions_without_promoting_coverage(setup):
    from test_tree_cli import declaration

    inventory, selection, options, _, imports = setup
    for node in inventory.nodes:
        path = Path(inventory.canonical_root) / node.relative_path
        row = json.loads(path.read_text())
        old = {**row, "uuid": "older-" + row["uuid"], "version": "2.1.209"}
        row["parentUuid"] = old["uuid"]
        path.write_text(json.dumps(old) + "\n" + json.dumps(row) + "\n")
    inventory = inventory_from_explicit_manifest(
        declaration(inventory),
        root=Path(inventory.canonical_root),
        source_format="claude",
        client_version="2.1.318",
    )
    prepared = prepare_tree(inventory, selection, **options)
    assert tree_proposal(prepared)["coverage"] == "unknown"
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed"
    assert len(imports) == 3
