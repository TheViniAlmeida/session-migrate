"""Physical storage authorities stay bound through parsing, audit and budgets."""

import hashlib

import pytest
from test_codex_storage import (
    CHILD_ID,
    ROOT_ID,
    header,
    history_base,
    message,
    pair,
    texts,
    write_rollout,
)

from session_migrate.canonical import CanonicalPolicy
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import codex
from session_migrate.jsonl import encode_jsonl
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat
from session_migrate.native_audit import (
    NativeAuditError,
    audit_snapshot,
    canonicalize_snapshot,
    validate_audit,
)
from session_migrate.native_snapshots import (
    CaptureProvenance,
    SourceCaptureContext,
    capture_readable_source,
    check_source_identity,
)
from session_migrate.profiles import load_bounded_source


def capture(path, root, **kwargs):
    return capture_readable_source(
        SourceCaptureContext(AgentFormat.CODEX, path), root=root, **kwargs
    )


@pytest.mark.parametrize("compressed", [False, True])
def test_single_capture_keeps_physical_hash_and_audits_the_same_decoded_bytes(tmp_path, compressed):
    path = write_rollout(
        tmp_path, ROOT_ID, [header(), message(1, "shared parent")], compressed=compressed
    )
    snapshot = capture(path, tmp_path)
    assert snapshot.integrity_valid()
    assert snapshot.authority_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert snapshot.parse().source_sha256 == codex.parse(path).source_sha256
    assert texts(snapshot.parse()) == ["shared parent"]
    audit = audit_snapshot(snapshot)
    observed = canonicalize_snapshot(snapshot, audit, CanonicalPolicy())
    assert texts(observed.session) == ["shared parent"]
    assert audit.coverage is Coverage.VERIFIED
    validate_audit(snapshot, audit)
    if compressed:
        assert snapshot.provenance is CaptureProvenance.RESOLVED_STORAGE
        assert snapshot.raw_bytes != path.read_bytes()
        assert snapshot.decoded_sha256 == hashlib.sha256(snapshot.raw_bytes).hexdigest()
        assert snapshot.source_storage_bytes == path.read_bytes()
        assert snapshot.captured_byte_count == len(path.read_bytes()) + len(snapshot.raw_bytes)
    else:
        assert snapshot.provenance is CaptureProvenance.FILE
        assert not snapshot.storage_components


def test_linked_capture_binds_every_component_without_claiming_full_raw_context(tmp_path):
    parent, child = pair(tmp_path, parent_compressed=True)
    snapshot = capture(child, tmp_path)
    assert [c.source_path for c in snapshot.storage_components] == [child, parent]
    assert snapshot.authority_sha256 == codex.parse(child, root=tmp_path).source_sha256
    assert snapshot.physical_source_sha256 == hashlib.sha256(child.read_bytes()).hexdigest()
    assert snapshot.authority_sha256 != snapshot.physical_source_sha256
    assert texts(snapshot.parse()) == ["shared parent", "own child"]
    audit = audit_snapshot(snapshot)
    observed = canonicalize_snapshot(snapshot, audit, CanonicalPolicy())
    assert texts(observed.session) == ["shared parent", "own child"]
    assert "codex_materialized_history_context_unproven" in dict(audit.issue_counts)
    assert audit.coverage is observed.coverage is Coverage.UNKNOWN
    validate_audit(snapshot, audit)


def test_ancestor_change_outside_selected_prefix_changes_composite_authority(tmp_path):
    parent, child = pair(tmp_path)
    first = capture(child, tmp_path)
    parent.write_bytes(parent.read_bytes() + encode_jsonl([message(2, "outside fork cutoff")]))
    with pytest.raises(SessionMigrateError, match="changed"):
        check_source_identity(first)
    second = capture(child, tmp_path)
    assert first.raw_bytes == second.raw_bytes
    assert first.authority_sha256 != second.authority_sha256
    assert texts(second.parse()) == ["shared parent", "own child"]


@pytest.mark.parametrize("repair_cached_digest", [False, True])
def test_issued_audit_rejects_changed_decoded_bytes_even_if_raw_storage_hash_is_unchanged(
    tmp_path, repair_cached_digest
):
    path = write_rollout(
        tmp_path, ROOT_ID, [header(), message(1, "shared parent")], compressed=True
    )
    snapshot = capture(path, tmp_path)
    audit = audit_snapshot(snapshot)
    before = snapshot.authority_sha256
    altered = snapshot.raw_bytes.replace(b"shared parent", b"forged parent")
    object.__setattr__(snapshot, "_raw_bytes", altered)
    if repair_cached_digest:
        object.__setattr__(snapshot, "decoded_sha256", hashlib.sha256(altered).hexdigest())
    assert snapshot.authority_sha256 == before
    with pytest.raises(NativeAuditError):
        validate_audit(snapshot, audit)


@pytest.mark.parametrize(
    "location", ["ancestor_header", "ancestor_content", "leaf_header", "leaf_content"]
)
def test_duplicate_keys_are_rejected_before_materialization_can_hide_them(tmp_path, location):
    parent = write_rollout(tmp_path, ROOT_ID, [header(), message(1, "shared parent")])
    if location.startswith("ancestor"):
        original = parent.read_bytes()
        needle = (
            b'"cwd":"/synthetic/work"' if location.endswith("header") else b'"text":"shared parent"'
        )
        field = b'"cwd"' if location.endswith("header") else b'"text"'
        assert needle in original
        parent.write_bytes(original.replace(needle, field + b':"HIDDEN_DUPLICATE",' + needle, 1))
    base = history_base(parent, 2)
    child = write_rollout(
        tmp_path, CHILD_ID, [header(CHILD_ID, base=base), message(3, "own child")]
    )
    if location.startswith("leaf"):
        original = child.read_bytes()
        needle = (
            b'"cwd":"/synthetic/work"' if location.endswith("header") else b'"text":"own child"'
        )
        field = b'"cwd"' if location.endswith("header") else b'"text"'
        assert needle in original
        child.write_bytes(original.replace(needle, field + b':"HIDDEN_DUPLICATE",' + needle, 1))
    with pytest.raises(SessionMigrateError, match="Invalid native audit JSON"):
        capture(child, tmp_path)


@pytest.mark.parametrize("invalid", [b'"unknown":1e309,', b'"unknown":"\\ud800",'])
def test_invalid_ancestor_header_never_disappears_into_a_valid_projection(tmp_path, invalid):
    parent = write_rollout(tmp_path, ROOT_ID, [header(), message(1, "shared parent")])
    parent.write_bytes(parent.read_bytes().replace(b'"payload":{', b'"payload":{' + invalid, 1))
    child = write_rollout(tmp_path, CHILD_ID, [header(CHILD_ID, base=history_base(parent, 2))])
    with pytest.raises(SessionMigrateError, match="Invalid native audit JSON"):
        capture(child, tmp_path)


@pytest.mark.parametrize("through_profile", [False, True])
def test_explicit_root_is_checked_before_opening_an_uncompressed_outside_source(
    tmp_path, monkeypatch, through_profile
):
    import session_migrate.native_snapshots as snapshots

    authorized = tmp_path / "authorized"
    authorized.mkdir()
    outside = write_rollout(tmp_path / "outside", ROOT_ID, [header(), message(1, "outside")])
    opened = []
    original = snapshots.capture_source

    def track(*args, **kwargs):
        opened.append(args[0].source_path)
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "capture_source", track)
    with pytest.raises(SessionMigrateError, match="outside"):
        if through_profile:
            load_bounded_source(outside, AgentFormat.CODEX, root=authorized, max_source_bytes=8192)
        else:
            capture(outside, authorized)
    assert opened == []


def test_aggregate_budget_counts_compressed_and_decoded_storage(tmp_path):
    parent, child = pair(tmp_path, parent_compressed=True)
    snapshot = capture(child, tmp_path)
    with pytest.raises(SessionMigrateError, match="limit|budget"):
        capture(child, tmp_path, byte_limit=snapshot.captured_byte_count - 1)
    assert parent.is_file() and child.is_file()
