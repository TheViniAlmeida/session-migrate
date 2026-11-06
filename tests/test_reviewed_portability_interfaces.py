"""Fresh review consent and durable portability mode across CLI and tree resume."""

import json
from pathlib import Path

import pytest
import test_tree_operations as tree_fixtures
from test_reviewed_portability_core import context, source_rows
from test_tree_cli import declaration, documents
from test_tree_journal import prepared as journal_prepared

from session_migrate.cli import build_parser, main
from session_migrate.tree_journal import TreeJournal, TreeJournalError, inspect_tree
from session_migrate.tree_operations import (
    TreeOperationError,
    commit_tree,
    prepare_tree,
    prepare_tree_resume,
    tree_proposal,
)
from session_migrate.tree_sources import inventory_from_explicit_manifest

tree_setup = tree_fixtures.setup


def conversion_args(tmp_path):
    request, _ = context(tmp_path)
    return request, [
        "--state-home",
        str(tmp_path / "state"),
        "convert",
        str(request.path),
        "--format",
        "claude",
        "--to",
        "opencode",
        "--target-cli-version",
        "2.2.5",
        "--output",
        str(request.output),
        "--cwd",
        str(tmp_path),
        "--reviewed-portability",
    ]


def test_cli_reports_before_refusing_missing_ack(tmp_path, monkeypatch, capsys):
    request, args = conversion_args(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(args) == 2
    output = capsys.readouterr()
    preview = documents(output.out)[0]
    assert preview["mode"] == "reviewed_portability"
    assert preview["reviewed_portability"]["policy_id"]
    assert not request.output.exists()
    assert not Path(preview["reviewed_archive"]["path"]).exists()
    assert "acknowledge" in output.err


def test_cli_explicit_ack_retains_archive_and_publishes_after_preview(tmp_path, capsys):
    request, args = conversion_args(tmp_path)
    original = request.path.read_bytes()
    assert main(args + ["--acknowledge-reviewed-portability"]) == 0
    output = documents(capsys.readouterr().out)
    assert len(output) == 2
    preview = output[0]
    assert preview["mode"] == "reviewed_portability"
    assert request.output.exists()
    assert Path(preview["reviewed_archive"]["path"]).read_bytes() == original
    assert Path(preview["reviewed_archive"]["path"]).is_relative_to(tmp_path / "state")
    assert request.path.read_bytes() == original


def test_cli_ack_does_not_select_reviewed_mode(tmp_path, capsys):
    request, args = conversion_args(tmp_path)
    args.remove("--reviewed-portability")
    assert main(args + ["--acknowledge-reviewed-portability"]) == 2
    assert not request.output.exists()
    assert "requires --reviewed-portability" in capsys.readouterr().err


def test_cli_proposal_never_publishes_even_with_ack_flag(tmp_path, capsys):
    request, args = conversion_args(tmp_path)
    args.insert(2, "proposal")
    assert main(args + ["--acknowledge-reviewed-portability"]) == 0
    preview = documents(capsys.readouterr().out)[0]
    assert preview["reviewed_archive"]["status"] == "planned"
    assert not request.output.exists()
    assert not Path(preview["reviewed_archive"]["path"]).exists()


@pytest.fixture
def reviewed_tree(tree_setup):
    original, selection, options, native, imports = tree_setup
    root = Path(original.canonical_root)
    for node in original.nodes:
        rows = source_rows()
        for row in rows:
            row["sessionId"] = node.source_native_id
        (root / node.relative_path).write_text("".join(json.dumps(row) + "\n" for row in rows))
    inventory = inventory_from_explicit_manifest(
        declaration(original), root=root, source_format="claude", client_version="2.1.309"
    )
    return inventory, selection, options, native, imports


def test_tree_mode_is_not_partial_acknowledgement(reviewed_tree):
    inventory, selection, options, _, imports = reviewed_tree
    options = {**options, "acknowledge_partial": False}
    prepared = prepare_tree(inventory, selection, reviewed_portability=True, **options)
    proposal = tree_proposal(prepared)
    assert proposal["reviewed_portability"] is True
    assert proposal["requires_partial_acknowledgement"] is True
    assert not proposal["partial_acknowledged"]
    assert all(node["reviewed_report"] and node["reviewed_archive"] for node in proposal["nodes"])
    with pytest.raises(TreeOperationError, match="acknowledge"):
        commit_tree(prepared)
    assert not imports
    assert all(not Path(node["reviewed_archive"]["path"]).exists() for node in proposal["nodes"])


def test_reviewed_tree_resume_keeps_mode_and_completed_nodes(reviewed_tree):
    inventory, selection, options, native, imports = reviewed_tree
    prepared = prepare_tree(inventory, selection, reviewed_portability=True, **options)
    proposal = tree_proposal(prepared)
    result = commit_tree(prepared).to_dict()
    assert result["status"] == "completed" and len(imports) == 3
    operation_id = result["operation_id"]
    assert inspect_tree(options["state_home"], operation_id)["reviewed_portability"] is True
    for node in proposal["nodes"]:
        assert Path(node["reviewed_archive"]["path"]).is_file()
    resumed = prepare_tree_resume(inventory, selection, operation_id=operation_id, **options)
    assert tree_proposal(resumed)["reviewed_portability"] is True
    assert resumed._state.handles == ()
    resumed.discard()
    before = json.dumps(native, sort_keys=True)
    with pytest.raises(TreeOperationError, match="portability mode"):
        prepare_tree_resume(
            inventory, selection, operation_id=operation_id, reviewed_portability=False, **options
        )
    assert len(imports) == 3 and json.dumps(native, sort_keys=True) == before


@pytest.mark.parametrize("value", [0, 1, "true", [], {}])
def test_tree_reviewed_mode_rejects_non_boolean(reviewed_tree, value):
    inventory, selection, options, _, imports = reviewed_tree
    with pytest.raises(TreeOperationError, match="boolean"):
        prepare_tree(inventory, selection, reviewed_portability=value, **options)
    assert not imports


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_journal_reviewed_mode_requires_real_bool(tmp_path, value):
    event = journal_prepared("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", tmp_path)
    event["reviewed_portability"] = value
    with (
        TreeJournal(tmp_path, event["operation_id"], create=True) as journal,
        pytest.raises(TreeJournalError),
    ):
        journal.append(event)


def test_tree_resume_cli_mode_is_unspecified_by_default():
    parser = build_parser()
    args = [
        "tree",
        "resume",
        "--profiles",
        "/tmp/synthetic-profiles.json",
        "--source-client-version",
        "2.1.309",
        "--target-profile",
        "target",
        "--cwd",
        "/tmp",
        "--model-provider",
        "synthetic",
        "--model",
        "test",
        "--operation-id",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    ]
    assert parser.parse_args(args).reviewed_portability is None
    assert parser.parse_args(args + ["--reviewed-portability"]).reviewed_portability is True
    assert parser.parse_args(args + ["--no-reviewed-portability"]).reviewed_portability is False
