"""Independent session roots retain private organization without native subagent parents."""

import json
from copy import deepcopy

import pytest
import test_tree_operations as publisher_fixtures
from test_tree_journal import prepared as journal_prepared

from session_migrate import operations, tree_operations
from session_migrate.formats import opencode
from session_migrate.operations import CancellationToken
from session_migrate.tree_journal import TreeJournal, TreeJournalError, inspect_tree, journal_path
from session_migrate.tree_operations import (
    TreeOperationError,
    commit_tree,
    prepare_tree,
    prepare_tree_resume,
    tree_proposal,
)

tree_setup = publisher_fixtures.setup
tree_native = publisher_fixtures.tree_native


@pytest.mark.parametrize("mode", [None, "independent", "native-subagent"])
def test_publication_parent_is_factual_and_organization_is_durable(tree_setup, mode):
    inventory, selection, options, native, imports = tree_setup
    prepared = prepare_tree(inventory, selection, publication_mode=mode, **options)
    proposal = tree_proposal(prepared)
    expected_mode = mode or "independent"
    assert proposal["publication_mode"] == expected_mode
    assert not proposal["fork_boundary_proven"]
    assert proposal["hierarchy_semantics"] == (
        "external_organizational_map"
        if expected_mode == "independent"
        else "native_subagent_parent"
    )
    assert bool(proposal["warnings"]) == (expected_mode == "native-subagent")
    # Every node keeps the reviewed-tree loss proof, even without a native parent.
    assert all(
        handle._state.built.request.reviewed_tree_projection
        for _, handle in prepared._state.handles
    )
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed"
    assert result["publication_mode"] == expected_mode
    view = inspect_tree(options["state_home"], result["operation_id"])
    assert view["publication_mode"] == expected_mode
    for index, node in enumerate(proposal["nodes"]):
        organizational_parent = imports[index - 1] if index else None
        native_parent = organizational_parent if expected_mode == "native-subagent" else None
        assert node["organizational_parent_target_id"] == organizational_parent
        assert node["parent_target_id"] == native_parent
        assert result["nodes"][index]["organizational_parent_target_id"] == organizational_parent
        assert view["nodes"][index]["organizational_parent_target_id"] == organizational_parent
        document = native[imports[index]]
        parsed = opencode.parse_import_bytes(json.dumps(document).encode())
        assert document["info"].get("parentID") == native_parent
        assert parsed.title == node["title"] == ["parent", "child", "grandchild"][index]
        assert parsed.model == "test" and parsed.provider == "fixture"
        assert json.dumps(document).count("PRIVATE_BODY_SENTINEL " + node["key"]) == 1
    before = deepcopy(native)
    resumed = prepare_tree_resume(
        inventory, selection, operation_id=result["operation_id"], **options
    )
    assert tree_proposal(resumed)["publication_mode"] == expected_mode
    assert resumed._state.handles == ()
    resumed.discard()
    assert native == before and len(imports) == 3


@pytest.mark.parametrize("mode", ["independent", "native-subagent"])
def test_partial_resume_recovers_mode_and_only_publishes_unattempted(tree_setup, monkeypatch, mode):
    inventory, selection, options, native, imports = tree_setup
    token = CancellationToken()
    original_commit = operations.commit_operation

    def stop_after_one(*args, **kwargs):
        receipt = original_commit(*args, **kwargs)
        token.request_cancel()
        return receipt

    monkeypatch.setattr(operations, "commit_operation", stop_after_one)
    first = commit_tree(
        prepare_tree(inventory, selection, publication_mode=mode, **options), cancellation=token
    ).to_dict()
    assert first["status"] == "partial" and len(imports) == 1
    completed = deepcopy(native[imports[0]])
    monkeypatch.setattr(operations, "commit_operation", original_commit)
    resumed = prepare_tree_resume(
        inventory, selection, operation_id=first["operation_id"], **options
    )
    assert tree_proposal(resumed)["publication_mode"] == mode
    assert [key for key, _ in resumed._state.handles] == ["child", "grandchild"]
    assert commit_tree(resumed).status == "completed"
    assert len(set(imports)) == len(imports) == 3
    assert native[imports[0]] == completed


@pytest.mark.parametrize("mode", ["independent", "native-subagent"])
def test_explicit_resume_mode_switch_is_rejected_before_source_or_target_probes(
    tree_setup, monkeypatch, mode
):
    inventory, selection, options, _, imports = tree_setup
    result = commit_tree(
        prepare_tree(inventory, selection, publication_mode=mode, **options)
    ).to_dict()

    def forbidden(*args, **kwargs):
        pytest.fail("mode mismatch must fail before a source capture or native probe")

    monkeypatch.setattr(tree_operations, "_capture_plan", forbidden)
    monkeypatch.setattr(tree_operations, "_observe", forbidden)
    monkeypatch.setattr(operations, "prepare_operation", forbidden)
    opposite = "native-subagent" if mode == "independent" else "independent"
    with pytest.raises(TreeOperationError, match="publication mode differs"):
        prepare_tree_resume(
            inventory,
            selection,
            operation_id=result["operation_id"],
            publication_mode=opposite,
            **options,
        )
    assert len(imports) == 3


@pytest.mark.parametrize("mode", ["unknown", "", False, {}, ["independent"]])
def test_unknown_mode_fails_before_capture(tree_setup, monkeypatch, mode):
    inventory, selection, options, _, imports = tree_setup
    monkeypatch.setattr(
        tree_operations, "_capture_plan", lambda *_: pytest.fail("unexpected capture")
    )
    with pytest.raises(TreeOperationError, match="Unknown tree publication mode"):
        prepare_tree(inventory, selection, publication_mode=mode, **options)
    assert imports == []


@pytest.mark.parametrize("mode", ["independent", "native-subagent"])
@pytest.mark.parametrize("damage", ["mode", "organization", "native", "missing", "extra"])
def test_mode_schema_rejects_wrong_native_or_organizational_parent(tmp_path, mode, damage):
    import uuid

    operation = str(uuid.uuid4())
    event = journal_prepared(operation, tmp_path)
    event["publication_mode"] = mode
    for node in event["nodes"]:
        node["organizational_parent_target_id"] = node["parent_target_id"]
        if mode == "independent":
            node["parent_target_id"] = None
    if damage == "mode":
        event["publication_mode"] = "unknown"
    elif damage == "organization":
        event["nodes"][1]["organizational_parent_target_id"] = "ses_" + "f" * 32
    elif damage == "native":
        event["nodes"][1]["parent_target_id"] = (
            event["nodes"][0]["target_id"] if mode == "independent" else None
        )
    elif damage == "missing":
        event["nodes"][1].pop("organizational_parent_target_id")
    else:
        event["nodes"][1]["native_metadata"] = {}
    with (
        TreeJournal(tmp_path / "state", operation, create=True) as journal,
        pytest.raises(TreeJournalError),
    ):
        journal.append(event)


def test_legacy_missing_mode_inspection_preserves_native_parent_hash_chain(tmp_path):
    import uuid

    operation = str(uuid.uuid4())
    home = tmp_path / "state"
    event = journal_prepared(operation, tmp_path)
    with TreeJournal(home, operation, create=True) as journal:
        journal.append(event)
    path = journal_path(home, operation)
    before = path.read_bytes()
    view = inspect_tree(home, operation)
    assert view["publication_mode"] == "native-subagent"
    assert (
        view["nodes"][1]["organizational_parent_target_id"] == event["nodes"][1]["parent_target_id"]
    )
    assert before == path.read_bytes()


@pytest.fixture
def frozen_legacy_publisher(monkeypatch):
    """Optional exact ca834fe coordinator, never reconstructed with current defaults."""
    import hashlib
    import importlib.util
    import os
    import sys
    from pathlib import Path

    directory = os.environ.get("SESSION_MIGRATE_TEST_LEGACY_TREE_BASELINE")
    if not directory:
        pytest.skip("set SESSION_MIGRATE_TEST_LEGACY_TREE_BASELINE to frozen ca834fe snapshots")
    modules = {}
    hashes = {
        "tree_journal": "62c4ed245cb9cac5cf37a2a132d77a8a2432853aa1053c37a290184dbf757d26",
        "tree_operations": "a4cdcfea8324b5b5f6d7aec0cea3fd89c87186a32500b2216ec7e28dac2f3d8c",
    }
    for name, expected in hashes.items():
        path = Path(directory) / (name + ".py")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        spec = importlib.util.spec_from_file_location(
            "session_migrate._frozen_legacy_" + name, path
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    publisher = modules["tree_operations"]
    publisher.TreeJournal = modules["tree_journal"].TreeJournal
    publisher.inspect_tree = modules["tree_journal"].inspect_tree
    return publisher


@pytest.mark.parametrize("completed_count", [1, 3])
def test_frozen_legacy_journal_resumes_without_reinterpretation_or_reimports(
    tree_setup, monkeypatch, frozen_legacy_publisher, completed_count
):
    inventory, selection, options, native, imports = tree_setup
    legacy = frozen_legacy_publisher
    token = CancellationToken()
    original_commit = operations.commit_operation

    def stop_after_expected(*args, **kwargs):
        receipt = original_commit(*args, **kwargs)
        if len(imports) == completed_count:
            token.request_cancel()
        return receipt

    if completed_count == 1:
        monkeypatch.setattr(operations, "commit_operation", stop_after_expected)
    old_prepared = legacy.prepare_tree(inventory, selection, **options)
    old_proposal = legacy.tree_proposal(old_prepared)
    old = legacy.commit_tree(old_prepared, cancellation=token).to_dict()
    assert len(imports) == completed_count
    path = journal_path(options["state_home"], old["operation_id"])
    before_journal = path.read_bytes()
    before_native = deepcopy(native)
    first_event = json.loads(before_journal.splitlines()[0])["event"]
    assert "publication_mode" not in first_event
    assert all("organizational_parent_target_id" not in node for node in first_event["nodes"])
    monkeypatch.setattr(operations, "commit_operation", original_commit)
    resumed = prepare_tree_resume(inventory, selection, operation_id=old["operation_id"], **options)
    proposal = tree_proposal(resumed)
    assert proposal["publication_mode"] == "native-subagent"
    assert resumed._state.plan_sha256 == first_event["plan_sha256"]
    assert [node["target_id"] for node in proposal["nodes"]] == [
        node["target_id"] for node in old_proposal["nodes"]
    ]
    assert path.read_bytes() == before_journal
    assert native == before_native
    assert len(resumed._state.handles) == 3 - completed_count
    if completed_count == 3:
        resumed.discard()
        assert path.read_bytes() == before_journal
    else:
        assert commit_tree(resumed).status == "completed"
        assert len(set(imports)) == len(imports) == 3
        assert native[imports[0]] == before_native[imports[0]]
        assert native[imports[1]]["info"]["parentID"] == imports[0]
        assert native[imports[2]]["info"]["parentID"] == imports[1]


def test_stock_225_default_independent_publishes_three_visible_roots(tree_native):
    _, run = tree_native
    result = json.loads(
        run(r"""
import json, os, subprocess, sys
from pathlib import Path
from dataclasses import replace
import pytest
from test_tree_operations import setup
from session_migrate.tree_operations import prepare_tree, commit_tree, tree_proposal
from session_migrate.tree_journal import inspect_tree
from session_migrate.conversion import _opencode_session_ids
Path('/tmp').chmod(0o1777)
for ancestor in Path(sys.argv[1]).parents:
    if ancestor == Path('/tmp'):
        break
    ancestor.chmod(0o700)
root=Path(sys.argv[1])/'independent-tree-runtime'
root.mkdir(mode=0o700)
patch=pytest.MonkeyPatch()
inventory,selection,options,_,_=setup.__wrapped__(root,patch)
patch.undo()
options['target']=replace(options['target'],cli=Path('/tmp/opencode-v22'))
options['base_environ']={**os.environ,**options['base_environ']}
prepared=prepare_tree(inventory,selection,**options)
proposal=tree_proposal(prepared)
assert proposal['publication_mode']=='independent'
assert all(node['parent_target_id'] is None for node in proposal['nodes'])
result=commit_tree(prepared).to_dict()
assert result['status']=='completed',result
os.chdir(options['cwd'])
environment=options['target'].environment(options['base_environ'])
environment['XDG_DATA_HOME']=str(options['target'].root.parent)
ids=[node['target_id'] for node in proposal['nodes']]
assert _opencode_session_ids(options['target'].cli,environment)==set(ids)
view=inspect_tree(options['state_home'],result['operation_id'])
assert view['publication_mode']=='independent' and view['status']=='completed'
assert [node['organizational_parent_target_id'] for node in view['nodes']]==[None,ids[0],ids[1]]
for index,native_id in enumerate(ids):
    document=json.loads(subprocess.run(['/tmp/opencode-v22','session','export',native_id,
        '--standalone'],env=environment,capture_output=True,text=True,check=True,timeout=60).stdout)
    assert document['info'].get('parentID') is None
    assert document['info']['title']==proposal['nodes'][index]['title']
    body=json.dumps(document)
    assert body.count('PRIVATE_BODY_SENTINEL '+proposal['nodes'][index]['key'])==1
print(json.dumps({'status':'completed','visible_roots':len(ids),'organizational_map':True}))
""")
    )
    assert result == {"status": "completed", "visible_roots": 3, "organizational_map": True}


@pytest.mark.parametrize("damage", ["organization", "title", "mode"])
def test_resume_rejects_schema_valid_journal_changes_before_import(tree_setup, monkeypatch, damage):
    inventory, selection, options, _, imports = tree_setup
    prepared = prepare_tree(inventory, selection, **options)
    proposal = tree_proposal(prepared)
    fingerprint = prepared._state.plan_sha256
    prepared.discard()
    fields = (
        "key",
        "title",
        "target_id",
        "parent_target_id",
        "organizational_parent_target_id",
        "source_sha256",
        "target_sha256",
        "operation_id",
    )
    event = {
        "type": "prepared",
        "operation_id": proposal["operation_id"],
        "plan_sha256": fingerprint,
        "target_root": str(options["target"].root),
        "publication_mode": "independent",
        "nodes": [{key: node[key] for key in fields} for node in proposal["nodes"]],
    }
    if damage == "organization":
        # A legal earlier target ID is still the wrong organizational parent.
        event["nodes"][2]["organizational_parent_target_id"] = event["nodes"][0]["target_id"]
    elif damage == "title":
        event["nodes"][2]["title"] = "Changed journal title"
    else:
        event["publication_mode"] = "native-subagent"
        for node in event["nodes"]:
            node["parent_target_id"] = node["organizational_parent_target_id"]
    with TreeJournal(options["state_home"], proposal["operation_id"], create=True) as journal:
        journal.append(event)
    monkeypatch.setattr(
        operations, "prepare_operation", lambda *_args, **_kwargs: pytest.fail("unexpected prepare")
    )
    with pytest.raises(TreeOperationError, match="differs|differ"):
        prepare_tree_resume(inventory, selection, operation_id=proposal["operation_id"], **options)
    assert imports == []
