"""Private synthetic captures only; no native stores or provider processes."""

import hashlib
import os
import traceback
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import (
    CaptureProvenance,
    NativeSnapshot,
    SourceCaptureContext,
    capture_source,
    check_source_identity,
)

FIXTURE = Path(__file__).parent / "fixtures/codex-0.144.4/basic.jsonl"


def source(tmp_path: Path) -> tuple[Path, SourceCaptureContext]:
    path = tmp_path / "session.jsonl"
    path.write_bytes(FIXTURE.read_bytes())
    path.chmod(0o600)
    return path, SourceCaptureContext(AgentFormat.CODEX, path)


def test_capture_reads_once_and_binds_the_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, context = source(tmp_path)
    data = path.read_bytes()
    reads = []
    original_read = os.read

    def tracked_read(descriptor: int, count: int) -> bytes:
        reads.append(count)
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "read", tracked_read)
    snapshot = capture_source(context, byte_limit=len(data))
    assert reads == [len(data) + 1, 1]
    assert snapshot.raw_bytes == data
    assert snapshot.authority_sha256 == hashlib.sha256(data).hexdigest()
    assert snapshot.source_path == path
    assert snapshot.provenance == CaptureProvenance.FILE
    assert snapshot.identity is not None
    assert snapshot.identity.size == len(data)
    assert snapshot.ancestors[-1].path == tmp_path
    assert snapshot.parse().source_sha256 == snapshot.authority_sha256
    check_source_identity(snapshot)


def test_changed_file_does_not_change_parser_or_auditor_authority(tmp_path: Path) -> None:
    path, context = source(tmp_path)
    snapshot = capture_source(context)
    expected = asdict(snapshot.parse())
    expected_records = snapshot.decoded_records()
    path.write_bytes(b'{"type":"new-source"}\n')
    assert asdict(snapshot.parse()) == expected
    assert snapshot.decoded_records() == expected_records
    assert hashlib.sha256(snapshot.raw_bytes).hexdigest() == expected["source_sha256"]
    with pytest.raises(SessionMigrateError, match="file changed"):
        check_source_identity(snapshot)


def test_nested_decoding_and_event_mutation_cannot_change_snapshot(tmp_path: Path) -> None:
    _, context = source(tmp_path)
    snapshot = capture_source(context)
    digest = snapshot.authority_sha256
    expected = asdict(snapshot.parse())
    records = snapshot.decoded_records()
    records[0].value["payload"]["id"] = "mutated"
    assert snapshot.decoded_records()[0].value["payload"]["id"] != "mutated"
    event = next(event for event in snapshot.parse().events if event.payload)
    event.payload["synthetic_mutation"] = {"nested": ["mutation"]}
    assert asdict(snapshot.parse()) == expected
    assert hashlib.sha256(snapshot.raw_bytes).hexdigest() == digest
    with pytest.raises(FrozenInstanceError):
        snapshot._raw_bytes = b"replacement"


def test_export_capture_preserves_virtual_path_without_filesystem_identity() -> None:
    virtual_path = Path("captured-export/session.json")
    context = SourceCaptureContext(AgentFormat.CODEX, virtual_path)
    snapshot = NativeSnapshot.from_bytes(FIXTURE.read_bytes(), context=context)
    assert snapshot.source_path == virtual_path
    assert snapshot.parse().source_path == virtual_path
    assert snapshot.provenance == CaptureProvenance.EXPORTED_BYTES
    assert snapshot.identity is None and snapshot.ancestors == ()
    with pytest.raises(SessionMigrateError, match="no filesystem identity"):
        check_source_identity(snapshot)


@pytest.mark.parametrize("limit", [True, 0, -1, 256 * 1024 * 1024 + 1, 1.5])
def test_invalid_byte_limits_are_rejected(tmp_path: Path, limit: object) -> None:
    _, context = source(tmp_path)
    with pytest.raises(SessionMigrateError, match="byte limit is invalid"):
        capture_source(context, byte_limit=limit)
    with pytest.raises(SessionMigrateError, match="byte limit is invalid"):
        NativeSnapshot.from_bytes(b"{}", context=context, byte_limit=limit)


def test_limit_rejects_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, context = source(tmp_path)

    def forbidden_read(*args: object) -> bytes:
        pytest.fail("oversized source was read")

    monkeypatch.setattr(os, "read", forbidden_read)
    with pytest.raises(SessionMigrateError, match="byte safety limit"):
        capture_source(context, byte_limit=path.stat().st_size - 1)
    with pytest.raises(SessionMigrateError, match="byte safety limit"):
        NativeSnapshot.from_bytes(b"{}", context=context, byte_limit=1)
    with pytest.raises(SessionMigrateError, match="immutable bytes"):
        NativeSnapshot.from_bytes(bytearray(b"{}"), context=context)


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "hardlink", "ancestor_symlink"])
def test_special_and_aliased_files_are_rejected(tmp_path: Path, kind: str) -> None:
    path, _ = source(tmp_path)
    target = tmp_path / "unsafe"
    if kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(path)
    elif kind == "hardlink":
        os.link(path, target)
    else:
        target.symlink_to(tmp_path, target_is_directory=True)
        target /= path.name
    with pytest.raises(SessionMigrateError):
        capture_source(SourceCaptureContext(AgentFormat.CODEX, target))


@pytest.mark.parametrize("mutation", ["body", "replacement", "ancestor"])
def test_changes_during_read_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    folder = tmp_path / "capture"
    folder.mkdir(mode=0o700)
    path, context = source(folder)
    original_read = os.read
    changed = False

    def changed_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        data = original_read(descriptor, count)
        if changed:
            return data
        changed = True
        if mutation == "body":
            path.write_bytes(b"different-body")
        elif mutation == "replacement":
            path.rename(folder / "old")
            path.write_bytes(data)
        else:
            folder.rename(tmp_path / "renamed")
            folder.mkdir(mode=0o700)
            path.write_bytes(data)
        return data

    monkeypatch.setattr(os, "read", changed_read)
    with pytest.raises(SessionMigrateError, match="changed|guard failed"):
        capture_source(context)


def test_named_file_replacement_during_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, context = source(tmp_path)
    original_open = os.open

    def racing_open(name: object, flags: int, *args: object, **kwargs: object) -> int:
        if name == path.name:
            data = path.read_bytes()
            path.rename(tmp_path / "displaced")
            path.write_bytes(data)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(SessionMigrateError, match="file changed"):
        capture_source(context)


def test_short_regular_file_reads_complete_one_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, context = source(tmp_path)
    expected = path.read_bytes()
    original_read = os.read

    def short_read(descriptor: int, count: int) -> bytes:
        return original_read(descriptor, min(count, 31))

    monkeypatch.setattr(os, "read", short_read)
    snapshot = capture_source(context)
    assert snapshot.raw_bytes == expected
    assert snapshot.parse().source_sha256 == hashlib.sha256(expected).hexdigest()


def test_symlink_replacement_after_capture_does_not_reinterpret_source(tmp_path: Path) -> None:
    path, context = source(tmp_path)
    snapshot = capture_source(context)
    path.rename(tmp_path / "original")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"{}\n")
    path.symlink_to(replacement)
    assert snapshot.parse().source_path == path
    assert snapshot.parse().source_sha256 == snapshot.authority_sha256
    with pytest.raises(SessionMigrateError, match="regular file"):
        check_source_identity(snapshot)


def test_ancestor_rename_after_capture_rejects_identity_guard_and_freezes_path(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "capture"
    folder.mkdir(mode=0o700)
    path, context = source(folder)
    snapshot = capture_source(context)
    folder.rename(tmp_path / "renamed")
    assert snapshot.parse().source_path == path
    with pytest.raises(SessionMigrateError, match="changed|guard failed"):
        check_source_identity(snapshot)


def test_identity_guard_is_metadata_only_and_never_rereads_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, context = source(tmp_path)
    snapshot = capture_source(context)

    def forbidden_read(*args: object) -> bytes:
        pytest.fail("identity guard attempted to read a body")

    monkeypatch.setattr(os, "read", forbidden_read)
    check_source_identity(snapshot)


@pytest.mark.parametrize(
    "data",
    [b'{"SECRET_BODY":', b'{"payload":{"history_mode":"SECRET_BODY"},"type":"session_meta"}\n'],
)
def test_repr_and_parser_failures_do_not_expose_body(data: bytes) -> None:
    snapshot = NativeSnapshot.from_bytes(
        data, context=SourceCaptureContext(AgentFormat.CODEX, Path("synthetic"))
    )
    assert "SECRET_BODY" not in repr(snapshot)
    with pytest.raises(SessionMigrateError, match="native capture parsing failed") as caught:
        snapshot.parse()
    assert "SECRET_BODY" not in "".join(traceback.format_exception(caught.value))


def test_unknown_format_rejected_without_echoing_input() -> None:
    with pytest.raises(SessionMigrateError, match="format is unsupported") as caught:
        SourceCaptureContext("SECRET_BODY", Path("synthetic"))
    assert "SECRET_BODY" not in str(caught.value)


def test_snapshot_and_context_repr_exclude_all_metadata() -> None:
    context = SourceCaptureContext(AgentFormat.CODEX, Path("SECRET_PATH"))
    snapshot = NativeSnapshot.from_bytes(b"{}\n", context=context)
    for value in (context, snapshot):
        assert "SECRET_PATH" not in repr(value)
        assert "source_format" not in repr(value)


def test_deep_json_failure_is_normalized_without_body_leak() -> None:
    data = b'{"SECRET_BODY":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n"
    snapshot = NativeSnapshot.from_bytes(
        data, context=SourceCaptureContext(AgentFormat.CODEX, Path("synthetic"))
    )
    for decode in (snapshot.parse, snapshot.decoded_records):
        with pytest.raises(SessionMigrateError, match="native capture") as caught:
            decode()
        assert "SECRET_BODY" not in "".join(traceback.format_exception(caught.value))
