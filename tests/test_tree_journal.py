"""Private synthetic coordinator metadata: durable intent, no implicit recovery."""

import json
import os
import uuid

import pytest

from session_migrate.tree_journal import TreeJournal, TreeJournalError, inspect_tree, journal_path


@pytest.fixture
def journal(tmp_path):
    home = tmp_path / "state"
    operation = str(uuid.uuid4())
    return home, operation


def prepared(operation, tmp_path):
    return {
        "type": "prepared",
        "operation_id": operation,
        "plan_sha256": "a" * 64,
        "target_root": str(tmp_path / "opencode"),
        "nodes": [
            {
                "key": "parent",
                "target_id": "ses_" + "b" * 32,
                "parent_target_id": None,
                "source_sha256": "c" * 64,
                "target_sha256": "d" * 64,
                "operation_id": "parent-operation",
            },
            {
                "key": "child",
                "target_id": "ses_" + "e" * 32,
                "parent_target_id": "ses_" + "b" * 32,
                "source_sha256": "f" * 64,
                "target_sha256": "0" * 64,
                "operation_id": "child-operation",
            },
        ],
    }


def test_crash_after_intent_is_uncertain_and_inspection_is_readonly(journal, tmp_path):
    home, operation = journal
    with TreeJournal(home, operation, create=True) as store:
        store.append(prepared(operation, tmp_path))
        store.append({"type": "attempting", "node_key": "parent", "node_operation_id": "node-op"})
    path = journal_path(home, operation)
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = inspect_tree(home, operation)
    assert result["status"] == "needs_reconcile"
    assert result["nodes"][0]["status"] == "attempting"
    assert result["nodes"][1]["status"] == "unattempted"
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_completed_node_keeps_observed_hash_and_inner_operation(journal, tmp_path):
    home, operation = journal
    with TreeJournal(home, operation, create=True) as store:
        store.append(prepared(operation, tmp_path))
        store.append({"type": "attempting", "node_key": "parent", "node_operation_id": "node-op"})
        store.append(
            {
                "type": "completed",
                "node_key": "parent",
                "node_operation_id": "node-op",
                "observed_sha256": "e" * 64,
            }
        )
        assert store.inspect()["status"] == "partial"
    result = inspect_tree(home, operation)
    assert result["nodes"][0]["observed_sha256"] == "e" * 64
    assert result["nodes"][0]["operation_id"] == "node-op"


@pytest.mark.parametrize(
    "damage", ["hash", "truncated", "duplicate", "mode", "symlink", "hardlink"]
)
def test_reject_changed_and_unsafe_journals(journal, tmp_path, damage):
    home, operation = journal
    with TreeJournal(home, operation, create=True) as store:
        store.append(prepared(operation, tmp_path))
    path = journal_path(home, operation)
    data = path.read_bytes()
    if damage == "hash":
        path.write_bytes(data.replace(b'"parent"', b'"forged"'))
    elif damage == "truncated":
        path.write_bytes(data[:-2])
    elif damage == "duplicate":
        path.write_bytes(data.replace(b'"sequence":0', b'"sequence":0,"sequence":0'))
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "symlink":
        original = path.with_suffix(".old")
        path.rename(original)
        path.symlink_to(original)
    else:
        os.link(path, path.with_suffix(".link"))
    with pytest.raises(TreeJournalError):
        inspect_tree(home, operation)


def test_single_writer_and_no_content_fields(journal, tmp_path):
    home, operation = journal
    with TreeJournal(home, operation, create=True) as store:
        store.append(prepared(operation, tmp_path))
        with pytest.raises(TreeJournalError):
            TreeJournal(home, operation)
        with pytest.raises(TreeJournalError):
            store.append(
                {
                    "type": "attempting",
                    "node_key": "parent",
                    "node_operation_id": "node-op",
                    "transcript": "PRIVATE_BODY_SENTINEL",
                }
            )
    assert "PRIVATE_BODY_SENTINEL" not in journal_path(home, operation).read_text()


def test_replacing_open_file_prevents_append(journal, tmp_path):
    home, operation = journal
    with TreeJournal(home, operation, create=True) as store:
        store.append(prepared(operation, tmp_path))
        path = journal_path(home, operation)
        path.rename(path.with_suffix(".moved"))
        path.write_text("")
        path.chmod(0o600)
        with pytest.raises(TreeJournalError):
            store.append({"type": "cancelled"})


def test_invalid_transition_never_authorizes_resume(journal, tmp_path):
    home, operation = journal
    with TreeJournal(home, operation, create=True) as store:
        store.append(prepared(operation, tmp_path))
        store.append(
            {
                "type": "completed",
                "node_key": "parent",
                "node_operation_id": "node-op",
                "observed_sha256": "a" * 64,
            }
        )
    with pytest.raises(TreeJournalError):
        inspect_tree(home, operation)


def test_json_payload_is_a_hash_chain(journal, tmp_path):
    home, operation = journal
    with TreeJournal(home, operation, create=True) as store:
        store.append(prepared(operation, tmp_path))
        store.append({"type": "cancelled"})
    records = [json.loads(line) for line in journal_path(home, operation).read_text().splitlines()]
    assert records[1]["previous"] == records[0]["sha256"]
