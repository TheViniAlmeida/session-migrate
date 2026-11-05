"""Compressed and linked Codex sources through user-facing, bounded entry points."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from test_codex_storage import (
    CHILD_ID,
    REVERT_ID,
    ROOT_ID,
    header,
    history_base,
    message,
    pair,
    texts,
    write_rollout,
)

from session_migrate import codex_storage, profiles
from session_migrate.catalog import Catalog, _filename_uuid
from session_migrate.cli import main
from session_migrate.discovery import locate_session
from session_migrate.errors import SessionMigrateError, SourceReadLimitError
from session_migrate.inspection import detect_path_format, inspect_session, inspect_snapshot
from session_migrate.jsonl import encode_jsonl
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import SourceCaptureContext, capture_readable_source
from session_migrate.profiles import (
    Profile,
    list_profile_sessions,
    load_bounded_source,
    read_session,
)


@pytest.mark.parametrize("compressed", [False, True])
def test_discovery_and_filename_identity_use_stable_reverted_thread_id(tmp_path, compressed):
    path = write_rollout(
        tmp_path,
        ROOT_ID,
        [header(), message(1, "synthetic")],
        physical=REVERT_ID,
        compressed=compressed,
    )
    assert locate_session(AgentFormat.CODEX, ROOT_ID, tmp_path) == path
    assert _filename_uuid(path) == ROOT_ID
    with pytest.raises(SessionMigrateError, match="no codex session"):
        locate_session(AgentFormat.CODEX, REVERT_ID, tmp_path)


def test_plain_and_compressed_or_two_physical_rollouts_remain_ambiguous(tmp_path):
    plain = write_rollout(tmp_path, ROOT_ID, [header(), message(1, "synthetic")])
    write_rollout(tmp_path, ROOT_ID, [header(), message(1, "message")], compressed=True)
    with pytest.raises(SessionMigrateError, match="multiple codex"):
        locate_session(AgentFormat.CODEX, ROOT_ID, tmp_path)
    plain.with_suffix(".jsonl.zst").unlink()
    write_rollout(tmp_path, ROOT_ID, [header()], physical=REVERT_ID)
    with pytest.raises(SessionMigrateError, match="multiple codex"):
        locate_session(AgentFormat.CODEX, ROOT_ID, tmp_path)


def test_compressed_cli_inspection_reports_storage_authority_without_body(tmp_path, capsys):
    path = write_rollout(tmp_path, ROOT_ID, [header(), message(1, "message")], compressed=True)
    assert detect_path_format(path) == AgentFormat.CODEX
    assert main(["inspect", str(path), "--json"]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["bytes"] == path.stat().st_size
    assert report["session_id"] == ROOT_ID
    assert "message" not in output.out


def test_linked_inspection_uses_exact_snapshot_and_composite_authority(tmp_path):
    parent, child = pair(tmp_path, parent_compressed=True, parent_archived=True)
    snapshot = capture_readable_source(
        SourceCaptureContext(AgentFormat.CODEX, child), root=tmp_path
    )
    actual = inspect_snapshot(snapshot)
    assert actual == inspect_session(child)
    assert actual.sha256 == snapshot.authority_sha256
    assert actual.sha256 != hashlib.sha256(snapshot.raw_bytes).hexdigest()
    assert actual.bytes == parent.stat().st_size + child.stat().st_size
    parent.write_bytes(b"changed after capture")
    assert inspect_snapshot(snapshot) == actual


def test_catalog_cli_lists_compressed_resolved_fork_and_retains_corrupt_diagnostic(
    tmp_path, capsys
):
    root = tmp_path / "store"
    _, child = pair(root, parent_compressed=True)
    broken = write_rollout(root, REVERT_ID, [header(REVERT_ID)], compressed=False)
    broken = broken.with_suffix(".jsonl.zst")
    broken.write_bytes(b"\xffnot-zstd\x00")
    broken.with_suffix("").unlink()
    database = tmp_path / "catalog.sqlite3"
    prefix = ["--catalog", str(database), "catalog"]
    assert main([*prefix, "refresh", "--codex-root", str(root), "--no-auto", "--json"]) == 0
    capsys.readouterr()
    with Catalog(database) as catalog:
        entries = catalog.list_sessions(include_paths=True, statuses=("candidate", "corrupt"))
        by_id = {entry.session_id: entry for entry in entries}
        assert by_id[CHILD_ID].status == "candidate"
        assert Path(by_id[CHILD_ID].path) == child
        assert by_id[REVERT_ID].status == "corrupt"
        assert by_id[REVERT_ID].reason == "codex_storage_invalid"


def test_profile_bounded_load_resolves_all_ancestors_and_preserves_authority(tmp_path):
    root = tmp_path / "store"
    _, child = pair(root, parent_compressed=True, parent_archived=True)
    selected = Profile("synthetic", "Synthetic", AgentFormat.CODEX, root)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(AgentFormat.CODEX, root)
        assert catalog.refresh(include_auto=False).root_errors == 0
        item = next(
            item
            for item in list_profile_sessions(catalog, selected)
            if item.ref.native_id == CHILD_ID
        )
        actual = read_session(catalog, selected, item.ref, max_source_bytes=64_000)
    expected = codex_storage.materialize_codex_history(root, child)
    assert texts(actual) == ["shared parent", "own child"]
    assert actual.source_path == child
    assert actual.source_sha256 == expected.authority_sha256


@pytest.mark.parametrize("fallback", [False, True])
def test_metadata_only_probe_tracks_ancestor_and_namespace_mutations(
    tmp_path, monkeypatch, fallback
):
    parent, child = pair(tmp_path, parent_compressed=True)
    if fallback:
        monkeypatch.setattr(codex_storage, "_stdlib_zstd", lambda: None)
    paths = profiles.source_read_paths(AgentFormat.CODEX, child, tmp_path, max_source_bytes=64_000)
    assert parent in paths and child in paths
    before = profiles._path_revision(paths)
    parent.write_bytes(parent.read_bytes() + b"suffix mutation")
    assert profiles._path_revision(paths) != before


def test_large_ancestor_is_not_fully_read_before_the_interactive_budget_guard(
    tmp_path, monkeypatch
):
    parent, child = pair(tmp_path)
    with parent.open("ab") as output:
        output.write(b" " * 2_000_000)
    count = 0
    original_read = os.read

    def read(descriptor, size):
        nonlocal count
        data = original_read(descriptor, size)
        count += len(data)
        return data

    monkeypatch.setattr(os, "read", read)
    paths = profiles.source_read_paths(AgentFormat.CODEX, child, tmp_path, max_source_bytes=64_000)
    assert parent in paths
    assert count < 64_000
    count = 0
    with pytest.raises(SourceReadLimitError, match="read budget"):
        load_bounded_source(child, AgentFormat.CODEX, root=tmp_path, max_source_bytes=64_000)
    assert count < 64_000


def test_catalog_refresh_reconsiders_unchanged_leaf_after_ancestor_mutation(tmp_path):
    parent, child = pair(tmp_path)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(AgentFormat.CODEX, tmp_path)
        assert catalog.refresh(include_auto=False).root_errors == 0
        first = next(
            entry
            for entry in catalog.list_sessions(include_paths=True)
            if entry.session_id == CHILD_ID
        )
        assert first.status == "candidate"
        parent.write_bytes(b'{"type":"not-session-meta"}\n')
        result = catalog.refresh(include_auto=False)
        assert result.root_errors == 0
        second = next(
            entry
            for entry in catalog.list_sessions(
                include_paths=True, statuses=("corrupt", "unsupported")
            )
            if entry.session_id == CHILD_ID
        )
        assert second.status != "candidate"
        assert child.exists()


def test_unresolved_lineage_stays_visible_as_a_diagnostic(tmp_path):
    parent, child = pair(tmp_path)
    parent.unlink()
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(AgentFormat.CODEX, tmp_path)
        assert catalog.refresh(include_auto=False).root_errors == 0
        entries = catalog.list_sessions(include_paths=True, statuses=("unsupported", "corrupt"))
        assert len(entries) == 1
        assert entries[0].session_id == CHILD_ID
        assert entries[0].reason == "codex_history_base"
        assert Path(entries[0].path) == child


def test_attached_empty_history_is_a_candidate_without_inherited_messages(tmp_path):
    parent, child = pair(tmp_path, attached=True)
    base = history_base(parent, 2)
    child.write_bytes(
        encode_jsonl(
            [
                header(
                    CHILD_ID,
                    base=base,
                    attached_thread=True,
                    forked_from_id=ROOT_ID,
                    forked_from_ordinal_exclusive=2,
                )
            ]
        )
    )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.add_root(AgentFormat.CODEX, tmp_path)
        assert catalog.refresh(include_auto=False).root_errors == 0
        entry = next(entry for entry in catalog.list_sessions() if entry.session_id == CHILD_ID)
        assert entry.status == "candidate"
    parsed = load_bounded_source(child, AgentFormat.CODEX, root=tmp_path, max_source_bytes=64_000)
    assert texts(parsed) == []
