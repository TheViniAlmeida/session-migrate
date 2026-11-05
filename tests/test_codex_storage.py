"""Private synthetic Codex storage/lineage checks; no native user stores."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from zstd_fixture import compress_fixture

from session_migrate import codex_storage as storage
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import codex
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.model import AgentFormat, EventKind
from session_migrate.native_snapshots import SourceCaptureContext, capture_source

ROOT_ID = "10000000-0000-4000-8000-000000000001"
CHILD_ID = "20000000-0000-4000-8000-000000000002"
GRANDCHILD_ID = "30000000-0000-4000-8000-000000000003"
REVERT_ID = "40000000-0000-4000-8000-000000000004"
FIRST = base64.b64decode("KLUv/SAGMQAAZmlyc3QK")
SECOND = base64.b64decode("KLUv/SAHOQAAc2Vjb25kCg==")
BOMB = base64.b64decode("KLUv/aBADQMAVAAAEHh4AQD7/znAAgNqCHg=")
COMPRESSED_FIXTURES = {
    "c1100bb1e0c34cf169434849616ac4b00e68fcbd90cc9306aa006c6344a00597": (
        "KLUv/WDFAIUHAGIOLCFQaasDSDaBhuDb3ZtuRz5VGZsZorocsEHQYlg8IzMhqhqGOSfClCse2Mq18LUXtg/0/"
        "bYfTIdcVjhfZTHvgFwuWCY6mCi33ZLCTvXbxRrmfIKEjETSepd+DScYyc8RAkESAl0vd6Eak/ycba/XlBWvXwa"
        "daiRsbst5QgIAB0rXs/aGni3INCv+ID2zXmMS/txAESIIQEdKB6JAiuujW83rWv989Tr1YGNevn5j028EGABKRao"
        "CaEwgUuu4LBQza/zQDI9lQGPB5oc7rSzguPgtngFb4cWiamkC6qqxeNCD2wPGgAlKu8LhE1sHBQ=="
    ),
    "b029e7cb0db4f278ae3a59017b4aa412ce35fa0d0503a07aa1ab8cb77ac6c4b1": (
        "KLUv/WC/AFUHANKNKiFQaasDSDaBhqBbaW2bhXxuDT5hiDFywAYRCYbFR1W1qh6GGCNCbNde2D7Qt9t2Dx1wWeF8"
        "FcW8A3C5YBemvSS1bYYUbqndLtYQ4xPgYyAS1qto1zCCkfw8DwiCDnk93EViS/Lzta0cUxRz/a7XUgNfM1OcoxQA"
        "OBJdv9gb+qXe0ijmJ/WLcmzB93MDzSQIyAOhA0lk4nqmqeZtrH+ueltyjzEvX79M028EGABc5agMaIwlUuu4LBQza"
        "/zQDI9lQGPB5oc7rSzguPgtngFb4cWiamkC6qqxeNCD2wPGgAlKu8LhE1sHBQ=="
    ),
}


def header(identifier=ROOT_ID, *, base=None, **metadata):
    return {
        "type": "session_meta",
        "timestamp": "2026-11-05T00:00:00Z",
        "ordinal": base["end_ordinal_exclusive"] if base else 0,
        "payload": {
            "id": identifier,
            "session_id": identifier,
            "history_mode": "paginated",
            "cli_version": "0.166.0",
            "cwd": "/synthetic/work",
            **({"history_base": base} if base else {}),
            **metadata,
        },
    }


def message(ordinal, text):
    return {
        "type": "event_msg",
        "timestamp": "2026-11-05T00:00:00Z",
        "ordinal": ordinal,
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "UserMessage",
                "id": f"user-{ordinal}",
                "content": [{"type": "text", "text": text}],
            },
        },
    }


def write_rollout(root, identifier, records, *, physical=None, archived=False, compressed=False):
    directory = root / ("archived_sessions" if archived else "sessions") / "2026/11/05"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = f"_{physical}" if physical else ""
    path = directory / f"rollout-2026-11-05T00-00-00-{identifier}{suffix}.jsonl"
    data = encode_jsonl(records)
    if compressed:
        module = storage._stdlib_zstd()
        path = path.with_suffix(".jsonl.zst")
        # Precompressed synthetic fixtures keep the 3.11–3.13 decoder gate executable.
        data = (
            module.compress(data)
            if module is not None
            else (
                base64.b64decode(COMPRESSED_FIXTURES[hashlib.sha256(data).hexdigest()])
                if hashlib.sha256(data).hexdigest() in COMPRESSED_FIXTURES
                else compress_fixture(data)
            )
        )
    path.write_bytes(data)
    return path


def history_base(path, ordinal, *, identifier=ROOT_ID, offset=None):
    data = storage.decode_codex_storage(path.read_bytes(), path)
    return {
        "thread_id": identifier,
        "end_ordinal_exclusive": ordinal,
        "end_byte_offset": len(data) if offset is None else offset,
    }


def pair(tmp_path, *, parent_compressed=False, parent_archived=False, attached=False):
    parent = write_rollout(
        tmp_path,
        ROOT_ID,
        [header(), message(1, "shared parent")],
        compressed=parent_compressed,
        archived=parent_archived,
    )
    base = history_base(parent, 2)
    child = write_rollout(
        tmp_path,
        CHILD_ID,
        [
            header(
                CHILD_ID,
                base=base,
                forked_from_id=ROOT_ID,
                forked_from_ordinal_exclusive=2,
                attached_thread=attached,
            ),
            message(3, "own child"),
        ],
    )
    return parent, child


def texts(session):
    return [event.text for event in session.events if event.kind == EventKind.MESSAGE]


@pytest.mark.parametrize("fallback", [False, True])
def test_plain_concat_and_bounded_stream_decoding(monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(storage, "_stdlib_zstd", lambda: None)
    assert storage.decode_codex_storage(b"plain", Path("x.jsonl")) == b"plain"
    assert storage.decode_codex_storage(FIRST + SECOND, Path("x.jsonl.zst")) == b"first\nsecond\n"
    assert storage.decode_codex_storage(BOMB, Path("x.jsonl.zst")) == b"x" * 200_000
    with pytest.raises(SessionMigrateError, match="byte safety limit|window safety limit"):
        storage.decode_codex_storage(BOMB, Path("x.jsonl.zst"), max_decoded_bytes=100_000)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("data", [b"", FIRST[:-1], FIRST + b"garbage", FIRST + SECOND[:-2]])
def test_truncated_and_invalid_storage_never_returns_a_prefix(monkeypatch, fallback, data):
    if fallback:
        monkeypatch.setattr(storage, "_stdlib_zstd", lambda: None)
    with pytest.raises(SessionMigrateError, match="incomplete|invalid"):
        storage.decode_codex_storage(data, Path("x.jsonl.zst"))


@pytest.mark.parametrize("fallback", [False, True])
def test_frame_count_is_bounded(monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(storage, "_stdlib_zstd", lambda: None)
    monkeypatch.setattr(storage, "MAX_ZSTD_FRAMES", 2)
    with pytest.raises(SessionMigrateError, match="frame safety limit"):
        storage.decode_codex_storage(FIRST * 3, Path("x.jsonl.zst"))


@pytest.mark.parametrize("fallback", [False, True])
def test_window_size_is_bounded_even_when_the_output_is_tiny(monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(storage, "_stdlib_zstd", lambda: None)
    # A valid streaming frame with a 2 KiB window and just seven output bytes.
    frame = base64.b64decode("KLUv/QAIOQAAYm91bmRlZA==")
    assert storage.decode_codex_storage(frame, Path("x.jsonl.zst")) == b"bounded"
    monkeypatch.setattr(storage, "MAX_ZSTD_WINDOW_LOG", 10)
    with pytest.raises(SessionMigrateError, match="invalid"):
        storage.decode_codex_storage(frame, Path("x.jsonl.zst"))


def test_missing_decoder_reports_an_explicit_capability_limit(monkeypatch):
    monkeypatch.setattr(storage, "_stdlib_zstd", lambda: None)
    monkeypatch.setattr(storage.ctypes.util, "find_library", lambda name: None)
    storage._load_libzstd.cache_clear()
    try:
        with pytest.raises(SessionMigrateError, match="requires Python 3.14.*system libzstd"):
            storage.decode_codex_storage(FIRST, Path("x.jsonl.zst"))
    finally:
        storage._load_libzstd.cache_clear()


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_fork_resolves_archived_prefix_once_and_binds_raw_components(
    tmp_path, compressed, fallback, monkeypatch
):
    if fallback:
        monkeypatch.setattr(storage, "_stdlib_zstd", lambda: None)
    parent, child = pair(tmp_path, parent_compressed=compressed, parent_archived=True)
    reads = []

    def capture(context, **options):
        reads.append(context.source_path)
        return capture_source(context, **options)

    result = storage.materialize_codex_history(tmp_path, child, capture=capture)
    assert reads == [child, parent]
    assert result.source_sha256 == hashlib.sha256(child.read_bytes()).hexdigest()
    assert [component.source_path for component in result.components] == reads
    assert result.authority_sha256 != result.source_sha256
    assert result.materialized_sha256 == hashlib.sha256(result.materialized_bytes).hexdigest()
    assert b"history_base" in result.components[0].raw_bytes
    assert b"history_base" not in result.materialized_bytes
    assert result.session_id == CHILD_ID and result.rollout_id == CHILD_ID
    assert texts(codex.parse(child)) == ["shared parent", "own child"]
    assert codex.parse(child).source_sha256 == result.authority_sha256
    with pytest.raises(FrozenInstanceError):
        result.materialized_bytes = b"mutated"


def test_compressed_parse_keeps_physical_hash_separate_from_decoded_hash(tmp_path):
    path = write_rollout(tmp_path, ROOT_ID, [header(), message(1, "message")], compressed=True)
    result = storage.materialize_codex_history(tmp_path, path)
    raw = path.read_bytes()
    assert result.source_sha256 != result.decoded_sha256
    assert result.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert codex.parse_bytes(raw, path).source_sha256 == result.source_sha256
    assert texts(codex.parse(path)) == ["message"]


def test_nested_fork_cutoffs_use_physical_bytes_and_never_duplicate_prefix(tmp_path):
    parent, child = pair(tmp_path)
    # The parent's later suffix is outside the immutable byte/ordinal bookmark.
    with parent.open("ab") as output:
        output.write(encode_jsonl([message(2, "excluded later parent")]))
    base = history_base(child, 4, identifier=CHILD_ID)
    grandchild = write_rollout(
        tmp_path,
        GRANDCHILD_ID,
        [header(GRANDCHILD_ID, base=base), message(5, "own grandchild")],
    )
    assert texts(codex.parse(grandchild)) == ["shared parent", "own child", "own grandchild"]
    records = list(
        iter_jsonl_bytes(storage.materialize_codex_history(tmp_path, grandchild).materialized_bytes)
    )
    assert [record.value["ordinal"] for record in records] == list(range(len(records)))
    assert records[0].value["payload"]["id"] == GRANDCHILD_ID


@pytest.mark.parametrize("attached", [False, True])
def test_revert_uses_physical_rollout_id_and_retains_logical_fork_cutoff(tmp_path, attached):
    _, original = pair(tmp_path, attached=attached)
    base = history_base(original, 4, identifier=CHILD_ID)
    reverted = write_rollout(
        tmp_path,
        CHILD_ID,
        [
            header(
                CHILD_ID,
                base=base,
                forked_from_id=ROOT_ID,
                forked_from_ordinal_exclusive=2,
                attached_thread=attached,
            ),
            message(5, "after revert"),
        ],
        physical=REVERT_ID,
    )
    result = storage.materialize_codex_history(tmp_path, reverted)
    assert result.session_id == CHILD_ID and result.rollout_id == REVERT_ID
    expected = ["own child", "after revert"]
    assert texts(codex.parse(reverted)) == (expected if attached else ["shared parent", *expected])


def test_attached_empty_history_captures_parent_without_promoting_it(tmp_path):
    parent, child = pair(tmp_path, attached=True)
    records = list(iter_jsonl_bytes(child.read_bytes()))
    child.write_bytes(encode_jsonl([records[0].value]))
    result = storage.materialize_codex_history(tmp_path, child)
    assert len(result.components) == 2
    assert result.components[1].source_path == parent
    assert texts(codex.parse(child)) == []


def test_fork_of_attached_history_keeps_the_ancestor_projection_boundary(tmp_path):
    _, attached = pair(tmp_path, attached=True)
    base = history_base(attached, 4, identifier=CHILD_ID)
    child = write_rollout(
        tmp_path,
        GRANDCHILD_ID,
        [header(GRANDCHILD_ID, base=base), message(5, "ordinary fork")],
    )
    assert texts(codex.parse(child)) == ["own child", "ordinary fork"]


def test_attached_revert_before_original_fork_boundary_preserves_new_child_turns(tmp_path):
    parent, _ = pair(tmp_path, attached=True)
    header_end = len(parent.read_bytes().splitlines(keepends=True)[0])
    base = history_base(parent, 1, offset=header_end)
    reverted = write_rollout(
        tmp_path,
        CHILD_ID,
        [
            header(
                CHILD_ID,
                base=base,
                forked_from_id=ROOT_ID,
                forked_from_ordinal_exclusive=2,
                attached_thread=True,
            ),
            message(2, "new child turn below its original fork ordinal"),
        ],
        physical=REVERT_ID,
    )
    assert texts(codex.parse(reverted)) == ["new child turn below its original fork ordinal"]


@pytest.mark.parametrize("mutation", ["offset", "ordinal", "gap", "unresolved"])
def test_invalid_lineage_cannot_be_reclassified_as_a_standalone_root(tmp_path, mutation):
    _, child = pair(tmp_path)
    records = [dict(record.value) for record in iter_jsonl_bytes(child.read_bytes())]
    base = records[0]["payload"]["history_base"]
    if mutation == "offset":
        base["end_byte_offset"] -= 1
    elif mutation == "ordinal":
        base["end_ordinal_exclusive"] = 1
        records[0]["ordinal"], records[1]["ordinal"] = 1, 2
    elif mutation == "gap":
        records[1]["ordinal"] += 1
    else:
        base["thread_id"] = GRANDCHILD_ID
    child.write_bytes(encode_jsonl(records))
    with pytest.raises(SessionMigrateError, match="history_base|ordinals"):
        storage.materialize_codex_history(tmp_path, child)


def test_duplicate_physical_candidate_ids_are_ambiguous_even_across_stores(tmp_path):
    parent, child = pair(tmp_path)
    duplicate = tmp_path / "archived_sessions" / parent.name
    duplicate.parent.mkdir()
    duplicate.write_bytes(parent.read_bytes())
    with pytest.raises(SessionMigrateError, match="duplicate physical"):
        storage.materialize_codex_history(tmp_path, child)


def test_parent_mutation_after_capture_invalidates_entire_materialization(tmp_path):
    parent, child = pair(tmp_path)

    def capture(context, **options):
        snapshot = capture_source(context, **options)
        if context.source_path == parent:
            parent.write_bytes(parent.read_bytes() + b"\n")
        return snapshot

    with pytest.raises(SessionMigrateError, match="file changed"):
        storage.materialize_codex_history(tmp_path, child, capture=capture)


def test_new_duplicate_candidate_after_capture_is_detected(tmp_path):
    parent, child = pair(tmp_path)

    def capture(context, **options):
        snapshot = capture_source(context, **options)
        if context.source_path == parent:
            duplicate = tmp_path / "archived_sessions" / parent.name
            duplicate.parent.mkdir()
            duplicate.write_bytes(parent.read_bytes())
        return snapshot

    with pytest.raises(SessionMigrateError, match="duplicate physical"):
        storage.materialize_codex_history(tmp_path, child, capture=capture)


@pytest.mark.parametrize("limit", ["max_depth", "max_components", "max_total_bytes"])
def test_materialization_limits_are_enforced(tmp_path, limit):
    _, child = pair(tmp_path)
    with pytest.raises(SessionMigrateError, match="safety limit"):
        storage.materialize_codex_history(tmp_path, child, **{limit: 1})


def test_cycle_is_refused_before_recapturing_a_component(tmp_path):
    _, child = pair(tmp_path)
    records = [dict(record.value) for record in iter_jsonl_bytes(child.read_bytes())]
    records[0]["payload"]["history_base"]["thread_id"] = CHILD_ID
    child.write_bytes(encode_jsonl(records))
    reads = []

    def capture(context, **options):
        reads.append(context.source_path)
        return capture_source(context, **options)

    with pytest.raises(SessionMigrateError, match="cycle"):
        storage.materialize_codex_history(tmp_path, child, capture=capture)
    assert reads == [child]


def test_traversal_cross_root_and_symlink_components_are_refused(tmp_path):
    parent, child = pair(tmp_path)
    with pytest.raises(SessionMigrateError, match="outside.*root"):
        storage.materialize_codex_history(child.parent, parent.parent.parent / parent.name)
    with pytest.raises(SessionMigrateError, match="traversal"):
        storage.materialize_codex_history(tmp_path, child.parent / ".." / child.name)
    parent.rename(parent.with_name("source.jsonl"))
    parent.symlink_to(parent.with_name("source.jsonl"))
    with pytest.raises(SessionMigrateError, match="symlink"):
        storage.materialize_codex_history(tmp_path, child)


def test_preexisting_leaf_capture_is_reused_and_unresolved_bytes_fail_closed(tmp_path):
    _, child = pair(tmp_path)
    snapshot = capture_source(SourceCaptureContext(AgentFormat.CODEX, child))
    with pytest.raises(SessionMigrateError, match="history_base"):
        codex.parse_bytes(snapshot.raw_bytes, child)
    result = storage.materialize_codex_history(tmp_path, child, source_snapshot=snapshot)
    assert result.components[0] is snapshot
    # A caller cannot mutate freshly decoded records to change retained bytes.
    values = [dict(record.value) for record in iter_jsonl_bytes(result.materialized_bytes)]
    values[0]["payload"]["id"] = ROOT_ID
    assert json.loads(result.materialized_bytes.splitlines()[0])["payload"]["id"] == CHILD_ID


def test_projection_size_and_record_limits_apply_across_components(tmp_path, monkeypatch):
    _, child = pair(tmp_path)
    monkeypatch.setattr(storage, "DEFAULT_MAX_RECORDS", 2)
    with pytest.raises(SessionMigrateError, match="record safety limit"):
        storage.materialize_codex_history(tmp_path, child)
    with pytest.raises(SessionMigrateError, match="byte safety limit|window safety limit"):
        storage._encode_projection([message(1, "larger than the limit")], 1)


def test_deep_json_component_is_rejected_before_projection(tmp_path):
    value = "synthetic"
    for _ in range(130):
        value = [value]
    path = write_rollout(tmp_path, ROOT_ID, [header(context=value)])
    with pytest.raises(SessionMigrateError, match="Invalid native audit JSON"):
        storage.materialize_codex_history(tmp_path, path)


def test_directory_swap_during_index_never_follows_the_replacement_link(tmp_path, monkeypatch):
    root = tmp_path / "store"
    parent, child = pair(root)
    moved = root / "captured-directory"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_directory = parent.parent
    inode = original_directory.stat().st_ino
    original_scandir = os.scandir
    swapped = False

    def scandir(descriptor):
        nonlocal swapped
        if isinstance(descriptor, int) and os.fstat(descriptor).st_ino == inode and not swapped:
            swapped = True
            original_directory.rename(moved)
            original_directory.symlink_to(outside, target_is_directory=True)
        return original_scandir(descriptor)

    monkeypatch.setattr(storage.os, "scandir", scandir)
    with pytest.raises(SessionMigrateError, match="directory changed"):
        storage.materialize_codex_history(root, child)
    assert swapped


@pytest.mark.parametrize("fallback", [False, True])
def test_window_cannot_exceed_a_smaller_capture_budget(monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(storage, "_stdlib_zstd", lambda: None)
    # Valid 2 KiB streaming window, but only seven output bytes: output-only
    # accounting would miss the decoder allocation before the first output.
    frame = base64.b64decode("KLUv/QAIOQAAYm91bmRlZA==")
    assert (
        storage.decode_codex_storage(frame, Path("x.jsonl.zst"), max_decoded_bytes=2048)
        == b"bounded"
    )
    with pytest.raises(SessionMigrateError, match="window safety limit"):
        storage.decode_codex_storage(frame, Path("x.jsonl.zst"), max_decoded_bytes=1024)
