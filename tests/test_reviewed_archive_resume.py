"""Completed reviewed trees retain a fresh durable proof of their original source bytes."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path

import pytest
from test_reviewed_portability_interfaces import reviewed_tree as reviewed_tree
from test_reviewed_portability_interfaces import tree_setup as tree_setup

from session_migrate import operations
from session_migrate.errors import SessionMigrateError
from session_migrate.tree_operations import (
    TreeOperationError,
    commit_tree,
    prepare_tree,
    prepare_tree_resume,
    tree_proposal,
)


def completed(reviewed_tree):
    inventory, selection, options, native, imports = reviewed_tree
    prepared = prepare_tree(inventory, selection, reviewed_portability=True, **options)
    proposal = tree_proposal(prepared)
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed" and len(imports) == 3
    return inventory, selection, options, native, imports, proposal, result


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "corrupt",
        "symlink",
        "mode",
        "directory_mode",
        "namespace_mode",
        "directory_symlink",
        "hardlink",
    ],
)
def test_completed_reviewed_resume_rejects_unavailable_or_unsafe_original_archive(
    reviewed_tree, mutation
):
    inventory, selection, options, native, imports, proposal, result = completed(reviewed_tree)
    path = Path(proposal["nodes"][0]["reviewed_archive"]["path"])
    original = path.read_bytes()
    native_before = copy.deepcopy(native)
    if mutation in {"missing", "corrupt", "symlink"}:
        preserved = path.with_suffix(".kept")
        path.rename(preserved)
        if mutation == "corrupt":
            path.write_bytes(b"X" * len(original))
            path.chmod(0o600)
        elif mutation == "symlink":
            path.symlink_to(preserved.name)
    elif mutation == "mode":
        path.chmod(0o644)
    elif mutation == "directory_mode":
        path.parent.chmod(0o755)
    elif mutation == "namespace_mode":
        path.parent.parent.chmod(0o755)
    elif mutation == "directory_symlink":
        preserved = path.parent.with_name(path.parent.name + "-kept")
        path.parent.rename(preserved)
        path.parent.symlink_to(preserved.name, target_is_directory=True)
    else:
        os.link(path, path.with_suffix(".alias"))
    with pytest.raises(TreeOperationError, match="completed tree node"):
        prepare_tree_resume(inventory, selection, operation_id=result["operation_id"], **options)
    assert len(imports) == 3 and native == native_before
    if mutation == "missing":
        assert not path.exists()
    elif mutation == "corrupt":
        assert path.read_bytes() == b"X" * len(original)
    elif mutation == "symlink":
        assert path.is_symlink()


def test_completed_reviewed_resume_accepts_fresh_original_without_reimport(reviewed_tree):
    inventory, selection, options, native, imports, proposal, result = completed(reviewed_tree)
    before = {
        node["key"]: Path(node["reviewed_archive"]["path"]).read_bytes()
        for node in proposal["nodes"]
    }
    native_before = copy.deepcopy(native)
    resumed = prepare_tree_resume(
        inventory, selection, operation_id=result["operation_id"], **options
    )
    assert resumed._state.reviewed_portability is True and not resumed._state.handles
    resumed.discard()
    assert len(imports) == 3 and native == native_before
    for node in proposal["nodes"]:
        assert Path(node["reviewed_archive"]["path"]).read_bytes() == before[node["key"]]


def test_guard_checks_first_completed_archive_before_publishing_child(reviewed_tree, monkeypatch):
    inventory, selection, options, native, imports = reviewed_tree
    original_commit = operations.commit_operation
    changed = {}

    def corrupt_after_completion(handle, **kwargs):
        receipt = original_commit(handle, **kwargs)
        if len(imports) == 1:
            path = Path(receipt.legacy_result["reviewed_archive"]["path"])
            body = path.read_bytes()
            path.with_suffix(".kept").write_bytes(body)
            path.write_bytes(b"X" * len(body))
            changed.update(path=path, body=b"X" * len(body), native=copy.deepcopy(native))
        return receipt

    monkeypatch.setattr(operations, "commit_operation", corrupt_after_completion)
    prepared = prepare_tree(inventory, selection, reviewed_portability=True, **options)
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "needs_reconcile"
    assert len(imports) == 1 and native == changed["native"]
    assert changed["path"].read_bytes() == changed["body"]
    assert all(node["status"] == "unattempted" for node in result["nodes"][1:])


def test_archive_verifier_rejects_foreign_operation_namespace_before_reading(reviewed_tree):
    _, _, options, _, _, proposal, _ = completed(reviewed_tree)
    node = proposal["nodes"][0]
    archive = node["reviewed_archive"]
    path = Path(archive["path"])
    with pytest.raises(SessionMigrateError, match="namespace or binding"):
        operations.verify_reviewed_source_archive(
            path,
            state_home=options["state_home"],
            operation_id="0" * 32 if path.parent.name != "0" * 32 else "1" * 32,
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


def test_strict_completed_resume_does_not_require_reviewed_archive(tree_setup, monkeypatch):
    inventory, selection, options, native, imports = tree_setup
    prepared = prepare_tree(inventory, selection, **options)
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed"
    before = copy.deepcopy(native)

    def forbidden(*args, **kwargs):
        pytest.fail("strict legacy resume checked reviewed archive authority")

    monkeypatch.setattr(operations, "verify_reviewed_source_archive", forbidden)
    resumed = prepare_tree_resume(
        inventory, selection, operation_id=result["operation_id"], **options
    )
    assert not resumed._state.reviewed_portability and not resumed._state.handles
    resumed.discard()
    assert len(imports) == 3 and native == before
