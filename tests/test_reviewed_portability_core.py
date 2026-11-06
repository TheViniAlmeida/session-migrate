"""Synthetic reviewed preparation, consent and private source archive contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_tree_operations import setup as setup

from session_migrate import conversion, operations
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import (
    OperationKind,
    OperationRequest,
    commit_operation,
    prepare_operation,
    proposal_metadata,
    run_operation,
)

ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def source_rows():
    from test_claude_desktop309_compaction import topology

    rows = topology()
    rows.pop(4)
    rows[4]["parentUuid"] = "boundary"
    rows[1]["message"]["content"] = "SYNTHETIC_PRIVATE_BODY ação\n  "
    rows[-1]["message"]["content"] = "synthetic answer"
    return rows


def context(tmp_path, *, dry_run=False):
    source = tmp_path / "source.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in source_rows()) + "\n")
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    request = OperationRequest(
        OperationKind.CONVERT,
        path=source,
        source_format=AgentFormat.CLAUDE,
        target_format=TargetFormat.OPENCODE,
        target_cli_version="2.2.5",
        output=tmp_path / "target.json",
        cwd=tmp_path,
        session_id=ID,
        reviewed_portability=True,
        dry_run=dry_run,
    )
    return request, env


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_reviewed_flags_require_real_bool(value):
    with pytest.raises(SessionMigrateError, match="boolean"):
        OperationRequest(OperationKind.CONVERT, reviewed_portability=value)
    with pytest.raises(SessionMigrateError, match="boolean"):
        ConversionOptions(TargetFormat.OPENCODE, reviewed_portability=value)


def test_false_omitted_from_legacy_option_fingerprint(tmp_path):
    request, _ = context(tmp_path)
    request = replace(request, reviewed_portability=False)
    document = operations._public_options(request, {}, {}, {})
    assert "reviewed_portability" not in document["request"]
    assert (
        operations._public_options(replace(request, reviewed_portability=True), {}, {}, {})[
            "request"
        ]["reviewed_portability"]
        is True
    )


def test_reviewed_conversion_reader_and_options_must_agree(tmp_path):
    request, _ = context(tmp_path)
    reviewed = conversion.load_session(request.path, reviewed_portability=True)
    strict = conversion.load_session(request.path)
    reviewed_options = ConversionOptions(
        TargetFormat.OPENCODE, target_cli_version="2.2.5", reviewed_portability=True
    )
    with pytest.raises(SessionMigrateError, match="must agree"):
        convert_session(reviewed, replace(reviewed_options, reviewed_portability=False))
    with pytest.raises(SessionMigrateError, match="must agree"):
        convert_session(strict, reviewed_options)
    with pytest.raises(SessionMigrateError, match="exact OpenCode"):
        replace(reviewed_options, target_cli_version="2.2.4")


def test_reviewed_prepare_requires_separate_ack_and_retains_exact_original(tmp_path):
    request, env = context(tmp_path)
    original = request.path.read_bytes()
    prepared = prepare_operation(request, environ=env)
    proposal = proposal_metadata(prepared).to_dict()
    archive = Path(proposal["reviewed_archive"]["path"])
    assert proposal["mode"] == "reviewed_portability"
    assert proposal["requires_reviewed_portability_acknowledgement"] is True
    assert proposal["reviewed_archive"]["status"] == "planned"
    assert "SYNTHETIC_PRIVATE_BODY" not in json.dumps(proposal)
    assert not archive.exists() and not request.output.exists()
    with pytest.raises(SessionMigrateError, match="acknowledgement"):
        commit_operation(prepared)
    assert not archive.exists() and not request.output.exists()
    receipt = commit_operation(prepared, acknowledge_reviewed_portability=True)
    assert archive.read_bytes() == original == request.path.read_bytes()
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700
    retained = receipt.legacy_result["reviewed_archive"]
    assert retained["status"] == "retained"
    assert retained["sha256"] == hashlib.sha256(original).hexdigest()
    manifest = json.loads(Path(receipt.legacy_result["manifest"]).read_bytes())
    assert manifest["mode"] == "reviewed_portability"
    assert manifest["source"]["reviewed_report"]
    assert manifest["reviewed_archive"]["sha256"] == retained["sha256"]
    assert prepared._state is None


def test_reviewed_run_never_auto_acknowledges_including_convert_dry_run(tmp_path):
    request, env = context(tmp_path)
    with pytest.raises(SessionMigrateError, match="prepare_operation"):
        run_operation(request, environ=env)
    assert not request.output.exists()
    receipt = run_operation(replace(request, dry_run=True), environ=env)
    assert receipt.status == "ready"
    archive = Path(receipt.proposal.to_dict()["reviewed_archive"]["path"])
    assert not archive.exists() and not request.output.exists()


@pytest.mark.parametrize("mutation", ["source", "report", "options", "archive"])
def test_reviewed_mutation_invalidates_consent_before_any_publication(tmp_path, mutation):
    request, env = context(tmp_path)
    prepared = prepare_operation(request, environ=env)
    archive = Path(proposal_metadata(prepared).to_dict()["reviewed_archive"]["path"])
    if mutation == "source":
        request.path.write_bytes(
            request.path.read_bytes().replace(b"synthetic answer", b"changed answer")
        )
    elif mutation == "report":
        prepared._state.built.artifact.source.reviewed_report["selected_count"] += 1
    elif mutation == "options":
        object.__setattr__(prepared._state.built.request, "reviewed_portability", False)
    else:
        prepared._state.built.artifact.reviewed_archive["sha256"] = "0" * 64
    with pytest.raises(SessionMigrateError):
        commit_operation(prepared, acknowledge_reviewed_portability=True)
    assert not request.output.exists() and not archive.exists()


def test_archive_readback_failure_blocks_target_and_keeps_archived_bytes(tmp_path, monkeypatch):
    request, env = context(tmp_path)
    prepared = prepare_operation(request, environ=env)
    archive = Path(proposal_metadata(prepared).to_dict()["reviewed_archive"]["path"])

    def mismatch(state):
        raise SessionMigrateError("synthetic archive digest mismatch")

    monkeypatch.setattr(operations, "_verify_reviewed_archive", mismatch)
    with pytest.raises(SessionMigrateError, match="digest mismatch"):
        commit_operation(prepared, acknowledge_reviewed_portability=True)
    assert not request.output.exists()
    assert archive.read_bytes() == request.path.read_bytes()


def test_archive_collision_and_symlink_block_target_without_overwrite(tmp_path):
    request, env = context(tmp_path)
    prepared = prepare_operation(request, environ=env)
    archive = Path(proposal_metadata(prepared).to_dict()["reviewed_archive"]["path"])
    from session_migrate.native_namespaces import ensure_migration_state_home

    ensure_migration_state_home(archive.parent)
    other = tmp_path / "other"
    other.write_bytes(b"SYNTHETIC_OTHER")
    archive.symlink_to(other)
    with pytest.raises(SessionMigrateError, match="archive publication failed"):
        commit_operation(prepared, acknowledge_reviewed_portability=True)
    assert not request.output.exists()
    assert other.read_bytes() == b"SYNTHETIC_OTHER" and archive.is_symlink()


def native_context(tmp_path, setup):
    _, _, options, native, imports = setup
    request, _ = context(tmp_path)
    request = replace(
        request,
        command=OperationKind.IMPORT,
        output=None,
        target_cli=Path("/usr/bin/true"),
        cwd=options["cwd"],
        model_provider="fixture",
        model="test",
    )
    return request, options, native, imports


def test_reviewed_native_publication_retains_archive_without_full_edge(tmp_path, setup):
    from session_migrate.lineage import Coverage, LineageStore

    request, options, _, imports = native_context(tmp_path, setup)
    prepared = prepare_operation(
        request, environ=options["base_environ"], migration_state_home=options["state_home"]
    )
    assert prepared._state.projection.certificate is None
    assert prepared._state.projection.expectation_coverage is not Coverage.VERIFIED
    receipt = commit_operation(prepared, acknowledge_reviewed_portability=True)
    assert len(imports) == 1
    archive = receipt.legacy_result["reviewed_archive"]
    assert archive["status"] == "retained"
    assert Path(archive["path"]).read_bytes() == request.path.read_bytes()
    assert receipt.lineage_receipt.continuity_reason in {
        "identity_untracked",
        "coverage_unverified",
    }
    assert not LineageStore.inspect_existing(options["state_home"] / "lineage-v1").edges
    references = receipt.lineage_receipt.references
    assert any(r.path == archive["path"] and r.sha256 == archive["sha256"] for r in references)


def test_failure_after_native_import_keeps_private_archive_and_evidence(
    tmp_path, setup, monkeypatch
):
    from session_migrate.errors import OperationNeedsReconcileError

    request, options, _, imports = native_context(tmp_path, setup)
    original_import = conversion._invoke_opencode_import

    def fail_after_import(*args, **kwargs):
        original_import(*args, **kwargs)
        raise SessionMigrateError("Synthetic failure after import")

    monkeypatch.setattr(conversion, "_invoke_opencode_import", fail_after_import)
    prepared = prepare_operation(
        request, environ=options["base_environ"], migration_state_home=options["state_home"]
    )
    archive = proposal_metadata(prepared).to_dict()["reviewed_archive"]
    with pytest.raises(OperationNeedsReconcileError) as failure:
        commit_operation(prepared, acknowledge_reviewed_portability=True)
    assert len(imports) == 1
    assert Path(archive["path"]).read_bytes() == request.path.read_bytes()
    assert any(
        r.path == archive["path"]
        and r.sha256 == archive["sha256"]
        and r.availability == "available"
        for r in failure.value.receipt.references
    )


def test_reviewed_tool_reference_is_quoted_in_place_without_mutating_source(tmp_path):
    from test_claude_desktop309 import desktop

    from session_migrate.model import EventKind
    from session_migrate.projection_policies import reviewed_tool_reference_text

    request, env = context(tmp_path)
    rows = source_rows()
    rows[-1]["message"]["content"] = [
        {"type": "tool_use", "id": "synthetic-call", "name": "Read", "input": {"n": 1}}
    ]
    blocks = [
        {"type": "text", "text": "  synthetic-before\n"},
        {"type": "tool_reference", "tool_name": "SyntheticTool"},
        {"type": "text", "text": "\nsynthetic-after  "},
    ]
    rows.extend(
        [
            desktop(
                "user",
                "result",
                "after",
                [{"type": "tool_result", "tool_use_id": "synthetic-call", "content": blocks}],
            ),
            desktop("assistant", "final", "result", "synthetic final"),
        ]
    )
    request.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    prepared = prepare_operation(request, environ=env)
    artifact = prepared._state.built.artifact
    source_result = next(e for e in artifact.source.events if e.kind is EventKind.TOOL_RESULT)
    assert source_result.payload["content_blocks"] == blocks
    assert "tool_result:tool_reference" not in artifact.dropped
    document = json.loads(artifact.native_bytes)
    result_text = [
        block["text"]
        for message in document["messages"]
        if message["type"] == "assistant"
        for part in message["content"]
        if part["type"] == "tool"
        for block in part["state"]["content"]
        if block["type"] == "text"
    ]
    assert result_text == [
        blocks[0]["text"],
        reviewed_tool_reference_text("SyntheticTool"),
        blocks[2]["text"],
    ]
    commit_operation(prepared, acknowledge_reviewed_portability=True)
    assert source_result.payload["content_blocks"] == blocks
