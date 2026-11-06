"""Closed, bounded PNG portability preserves actual bytes, not textual markers."""

import base64
import copy
import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest
from test_claude_desktop309_compaction import topology

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, opencode
from session_migrate.formats.claude_compaction import select_compaction
from session_migrate.formats.claude_reviewed import (
    MAX_REVIEWED_PNG_BYTES,
    MAX_REVIEWED_PNG_DIMENSION,
    MAX_REVIEWED_PNG_INFLATED_BYTES,
    MAX_REVIEWED_PNG_PIXELS,
    SOURCE_REVIEWED_MEDIA_POLICY_ID,
    SOURCE_REVIEWED_POLICY_ID,
    plan_reviewed_carriers,
    validate_inline_png_data_uri,
)
from session_migrate.jsonl import encode_jsonl, iter_jsonl_bytes
from session_migrate.model import AgentFormat, EventKind, Role
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.projection_policies import _native_source_order

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(*, width=1, height=1, color=2, depth=8, interlace=0, raster=None, extra=()):
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    if raster is None:
        passes = [(width, height)]
        if interlace:
            passes = [
                (max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy))
                for x, y, dx, dy in (
                    (0, 0, 8, 8),
                    (4, 0, 8, 8),
                    (0, 4, 4, 8),
                    (2, 0, 4, 4),
                    (0, 2, 2, 4),
                    (1, 0, 2, 2),
                    (0, 1, 1, 2),
                )
            ]
        raster = b"".join(
            b"\0" + bytes((w * channels * depth + 7) // 8)
            for w, h in passes
            if w and h
            for _ in range(h)
        )
    header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)
    return (
        SIGNATURE
        + chunk(b"IHDR", header)
        + (chunk(b"PLTE", bytes(6)) if color == 3 else b"")
        + b"".join(chunk(kind, body) for kind, body in extra)
        + chunk(b"IDAT", zlib.compress(raster))
        + chunk(b"IEND", b"")
    )


def uri(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def image(data):
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": uri(data).split(",")[1]},
    }


def media_rows(blocks=None):
    rows = topology()
    rows[1]["message"]["content"] = [
        {
            "type": "tool_use",
            "id": "SYNTHETIC_IMAGE_CALL",
            "name": "Read",
            "input": {"file_path": "/NEVER/READ/IMAGE"},
        }
    ]
    rows[2]["message"]["content"] = [
        {
            "type": "tool_result",
            "tool_use_id": "SYNTHETIC_IMAGE_CALL",
            "content": blocks if blocks is not None else [image(png())],
        }
    ]
    return rows


def parse(rows):
    return claude.parse_bytes(
        encode_jsonl(rows), Path("/fixture/media.jsonl"), reviewed_portability=True
    )


@pytest.mark.parametrize("error_flag", [None, False])
def test_inline_png_preserves_bytes_tool_identity_and_source_provenance(error_flag):
    data = png()
    blocks = [
        {"type": "text", "text": "  SOURCE_MEDIA_BEFORE\n"},
        image(data),
        {"type": "text", "text": "\nSOURCE_MEDIA_AFTER  "},
    ]
    rows = media_rows(blocks)
    if error_flag is not None:
        rows[2]["message"]["content"][0]["is_error"] = error_flag
    before = copy.deepcopy(rows)
    session = parse(rows)
    call = next(e for e in session.events if e.kind is EventKind.TOOL_CALL)
    result = next(e for e in session.events if e.kind is EventKind.TOOL_RESULT)
    assert call.tool_call_id == result.tool_call_id == "SYNTHETIC_IMAGE_CALL"
    assert call.tool_name == "Read"
    assert call.payload == {"input": {"file_path": "/NEVER/READ/IMAGE"}}
    assert result.payload == {
        "is_error": False,
        "content_blocks": [blocks[0], {"type": "image", "image_url": uri(data)}, blocks[2]],
    }
    assert result.text == "  SOURCE_MEDIA_BEFORE\n\n\nSOURCE_MEDIA_AFTER  "
    assert (
        result.provenance.record_index,
        result.provenance.block_index,
        result.provenance.source_id,
    ) == (2, 0, "tail")
    assert session.source_sha256 == hashlib.sha256(encode_jsonl(rows)).hexdigest()
    assert rows == before
    report = session.reviewed_report
    assert report["policy_id"] == SOURCE_REVIEWED_MEDIA_POLICY_ID
    item = report["inline_tool_result_images"]["provenance"][0]
    assert item == {
        "record_index": 2,
        "block_index": 0,
        "content_block_index": 1,
        "mime": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "decoded_bytes": len(data),
        "width": 1,
        "height": 1,
        "source_sha256": hashlib.sha256(
            json.dumps(
                blocks[1], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }
    assert report["inline_tool_result_images"]["count"] == 1
    assert "positional" in report["image_transport"]["chat"]
    assert "no explicit tool call ID" in report["image_transport"]["chat"]
    assert "text/image interleaving is not preserved on wire" in report["image_transport"]["chat"]
    assert "function_call_output" in report["image_transport"]["responses"]
    assert report["image_transport"]["limits"] == {
        "max_decoded_png_bytes": MAX_REVIEWED_PNG_BYTES,
        "max_dimension": MAX_REVIEWED_PNG_DIMENSION,
        "max_pixels": MAX_REVIEWED_PNG_PIXELS,
        "max_inflated_bytes": MAX_REVIEWED_PNG_INFLATED_BYTES,
        "indexed_color_supported": False,
    }
    encoded_report = json.dumps(report)
    for private in (
        "SYNTHETIC_IMAGE_CALL",
        "/NEVER/",
        uri(data),
        "SOURCE_MEDIA_BEFORE",
        "SOURCE_MEDIA_AFTER",
    ):
        assert private not in encoded_report
    contexts = [e for e in session.events if e.kind is EventKind.CONTEXT]
    assert all(e.role is Role.USER for e in contexts)
    assert all(
        e.payload["reviewed_context_policy"] == SOURCE_REVIEWED_MEDIA_POLICY_ID for e in contexts
    )
    assert all("native_context_policy" not in e.payload for e in contexts)
    assert report == parse(rows).reviewed_report


def test_two_images_keep_content_order_in_writer_and_cold_parser(tmp_path):
    images = [png(), png(raster=b"\0\x01\x02\x03")]
    session = parse(media_rows([image(data) for data in images]))
    data, losses = opencode.serialize(
        session,
        session_id="ses_fixture_media",
        cwd=tmp_path,
        cli_version="2.2.5",
        timestamp="2026-11-06T00:00:00Z",
    )
    assert losses == {"opaque:inactive_or_metadata_conversation_record": 1}
    opencode.validate_native_bytes(data, "ses_fixture_media")
    cold = opencode.parse_import_bytes(data)
    result = next(e for e in cold.events if e.kind is EventKind.TOOL_RESULT)
    assert [b["image_url"] for b in result.payload["content_blocks"]] == [uri(p) for p in images]
    assert session.reviewed_report["inline_tool_result_images"]["count"] == 2


def test_image_report_and_body_are_bound_without_mutating_record_values():
    rows = media_rows()
    records = tuple(iter_jsonl_bytes(encode_jsonl(rows)))
    original = copy.deepcopy([r.value for r in records])
    plan = plan_reviewed_carriers(records, select_compaction(records))
    assert plan.policy_id == SOURCE_REVIEWED_MEDIA_POLICY_ID
    one = parse(rows)
    rows[2]["message"]["content"][0]["content"][0] = image(png(raster=b"\0\x01\x02\x03"))
    two = parse(rows)
    assert one.source_sha256 != two.source_sha256
    assert one.reviewed_report != two.reviewed_report
    assert [r.value for r in records] == original


def test_media_does_not_override_an_explicit_capture_byte_budget():
    data = encode_jsonl(media_rows())
    with pytest.raises(SessionMigrateError):
        NativeSnapshot.from_bytes(
            data,
            context=SourceCaptureContext(AgentFormat.CLAUDE, Path("/fixture/media.jsonl")),
            byte_limit=len(data) - 1,
        )


def test_no_image_uses_legacy_report_and_policy_and_strict_guard_remains_closed():
    session = parse(media_rows([{"type": "text", "text": "result"}]))
    assert session.reviewed_report["policy_id"] == SOURCE_REVIEWED_POLICY_ID
    assert "inline_tool_result_images" not in session.reviewed_report
    assert "image_transport" not in session.reviewed_report
    data = encode_jsonl(media_rows())
    strict = claude.parse_bytes(data, Path("/fixture/media.jsonl"))
    assert strict.reviewed_report is None
    with pytest.raises(ValueError):
        _native_source_order(strict.events)


@pytest.mark.parametrize("size", [(1, 1), (2, 3), (9, 11), (32, 17)])
@pytest.mark.parametrize("interlace", [0, 1])
@pytest.mark.parametrize("color,depth", [(0, 1), (0, 16), (2, 8), (4, 16), (6, 8)])
def test_closed_png_raster_lengths_and_adam7_passes(size, interlace, color, depth):
    data = png(width=size[0], height=size[1], interlace=interlace, color=color, depth=depth)
    assert validate_inline_png_data_uri(uri(data)) == {
        "mime": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "decoded_bytes": len(data),
        "width": size[0],
        "height": size[1],
    }


@pytest.mark.parametrize("depth", [1, 2, 4, 8])
@pytest.mark.parametrize("interlace", [0, 1])
def test_indexed_png_is_explicitly_unsupported_even_with_valid_palette(depth, interlace):
    data = png(color=3, depth=depth, interlace=interlace)
    with pytest.raises(SessionMigrateError, match="indexed inline PNG is unsupported"):
        validate_inline_png_data_uri(uri(data))
    with pytest.raises(SessionMigrateError, match="indexed inline PNG is unsupported"):
        parse(media_rows([image(data)]))


def test_adversarial_index_255_with_one_palette_entry_is_rejected():
    data = (
        SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
        + chunk(b"PLTE", bytes(3))
        + chunk(b"IDAT", zlib.compress(b"\0\xff"))
        + chunk(b"IEND", b"")
    )
    with pytest.raises(SessionMigrateError, match="indexed inline PNG is unsupported"):
        validate_inline_png_data_uri(uri(data))
    with pytest.raises(SessionMigrateError, match="indexed inline PNG is unsupported"):
        parse(media_rows([image(data)]))


def test_large_compressed_raster_is_validated_in_bounded_chunks():
    data = png(width=320, height=900)
    assert validate_inline_png_data_uri(uri(data))["height"] == 900


@pytest.mark.parametrize(
    "extras",
    [
        [(b"sRGB", b"\0")],
        [(b"gAMA", struct.pack(">I", 45455))],
        [(b"pHYs", struct.pack(">IIB", 100, 100, 1))],
        [(b"tRNS", bytes(6))],
    ],
)
def test_supported_ancillary_bytes_are_preserved_without_normalizing(extras):
    data = png(extra=extras)
    session = parse(media_rows([image(data)]))
    result = next(e for e in session.events if e.kind is EventKind.TOOL_RESULT)
    assert result.payload["content_blocks"] == [{"type": "image", "image_url": uri(data)}]


def test_consecutive_idat_chunks_and_split_zlib_stream_are_supported():
    data = png()
    payload = zlib.compress(b"\0\0\0\0")
    split = data[:33] + b"".join(chunk(b"IDAT", bytes([c])) for c in payload) + chunk(b"IEND", b"")
    assert validate_inline_png_data_uri(uri(split))["sha256"] == hashlib.sha256(split).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        "double_header",
        "double_palette",
        "palette_after_idat",
        "palette_after_transparency",
        "zero_idat_palette",
        "double_srgb",
        "bad_srgb",
        "bad_gamma",
        "bad_phys",
        "bad_trns",
        "zlib_trailer",
        "zlib_second_stream",
        "chunk_count",
    ],
)
def test_png_chunk_and_compression_order_is_closed(change):
    header = png()[:33]
    raster = chunk(b"IDAT", zlib.compress(bytes(4)))
    ending = chunk(b"IEND", b"")
    if change == "double_header":
        data = header + header[8:] + raster + ending
    elif change == "double_palette":
        data = header + chunk(b"PLTE", bytes(3)) * 2 + raster + ending
    elif change == "palette_after_idat":
        data = header + raster + chunk(b"PLTE", bytes(3)) + ending
    elif change == "palette_after_transparency":
        data = header + chunk(b"tRNS", bytes(6)) + chunk(b"PLTE", bytes(3)) + raster + ending
    elif change == "zero_idat_palette":
        data = header + chunk(b"IDAT", b"") + chunk(b"PLTE", bytes(3)) + raster + ending
    elif change == "double_srgb":
        data = png(extra=[(b"sRGB", b"\0"), (b"sRGB", b"\0")])
    elif change in {"bad_srgb", "bad_gamma", "bad_phys", "bad_trns"}:
        data = png(
            extra=[
                {
                    "bad_srgb": (b"sRGB", b"\x05"),
                    "bad_gamma": (b"gAMA", bytes(4)),
                    "bad_phys": (b"pHYs", bytes(8)),
                    "bad_trns": (b"tRNS", b"\0"),
                }[change]
            ]
        )
    elif change == "chunk_count":
        data = header + chunk(b"IDAT", b"") * 4096 + raster + ending
    else:
        data = (
            header
            + chunk(
                b"IDAT",
                zlib.compress(bytes(4))
                + (b"private" if change == "zlib_trailer" else zlib.compress(bytes(4))),
            )
            + ending
        )
    with pytest.raises(SessionMigrateError):
        validate_inline_png_data_uri(uri(data))


def test_media_validation_never_opens_captured_paths(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("captured path opened")

    monkeypatch.setattr(Path, "open", forbidden)
    parse(media_rows())


def test_media_policy_updates_only_framing_and_preserves_historical_text():
    rows = media_rows()
    literal = "  Histórico\n" + SOURCE_REVIEWED_POLICY_ID + "\n  "
    rows[1]["message"]["content"].insert(0, {"type": "thinking", "thinking": literal})
    rows[1]["message"]["content"].insert(1, {"type": "text", "text": literal})
    rows[5]["message"]["content"] = literal
    session = parse(rows)
    assert any(e.kind is EventKind.MESSAGE and e.text == literal for e in session.events)
    quoted = next(e for e in session.events if e.payload.get("reviewed_data_class") == "thinking")
    assert json.loads(quoted.text)["data"] == {"type": "thinking", "thinking": literal}
    assert json.loads(quoted.text)["policy_id"] == SOURCE_REVIEWED_MEDIA_POLICY_ID
    summary = next(e for e in session.events if e.payload.get("reviewed_data_class") == "summary")
    assert summary.text == literal


@pytest.mark.parametrize("filter_value", [5, 255])
def test_adam7_invalid_filter_and_missing_pass_bytes_are_rejected(filter_value):
    data = png(width=9, height=11, interlace=1)
    idat_size = int.from_bytes(data[33:37])
    raster = zlib.decompress(data[41 : 41 + idat_size])
    for invalid in (raster[:-1], bytes([filter_value]) + raster[1:]):
        corrupted = data[:33] + chunk(b"IDAT", zlib.compress(invalid)) + chunk(b"IEND", b"")
        with pytest.raises(SessionMigrateError):
            validate_inline_png_data_uri(uri(corrupted))


def test_image_byte_limit_is_checked_before_base64_decode(monkeypatch):
    import session_migrate.formats.claude_reviewed as reader

    monkeypatch.setattr(reader, "MAX_REVIEWED_PNG_BYTES", 8)

    def forbidden(*args, **kwargs):
        raise AssertionError("oversized base64 decoded")

    monkeypatch.setattr(reader.base64, "b64decode", forbidden)
    with pytest.raises(SessionMigrateError, match="byte limit"):
        validate_inline_png_data_uri(uri(png()))


def test_unselected_image_does_not_change_the_reviewed_policy():
    rows = media_rows([{"type": "text", "text": "result"}])
    rows[0]["message"]["content"] = [image(png())]
    assert parse(rows).reviewed_report["policy_id"] == SOURCE_REVIEWED_POLICY_ID


@pytest.mark.parametrize(
    "change",
    [
        "error",
        "source_type",
        "mime",
        "data_type",
        "source_extra",
        "block_extra",
        "url",
        "unknown",
        "base64",
        "empty",
        "nonascii",
    ],
)
def test_unsupported_media_shape_remains_fail_closed(change):
    rows = media_rows()
    result = rows[2]["message"]["content"][0]
    block = result["content"][0]
    if change == "error":
        result["is_error"] = True
    elif change == "source_type":
        block["source"]["type"] = "BASE64"
    elif change == "mime":
        block["source"]["media_type"] = "image/jpeg"
    elif change == "data_type":
        block["source"]["data"] = True
    elif change == "source_extra":
        block["source"]["filename"] = "/NEVER/READ/IMAGE"
    elif change == "block_extra":
        block["display"] = "SECRET_SENTINEL"
    elif change == "url":
        block["source"] = {"type": "url", "url": "file:///NEVER/READ/IMAGE"}
    elif change == "unknown":
        block["type"] = "unknown_media"
    else:
        block["source"]["data"] = {"base64": "!!!", "empty": "", "nonascii": "é"}[change]
    with pytest.raises(SessionMigrateError) as error:
        parse(rows)
    assert "SECRET_SENTINEL" not in str(error.value)
    assert "/NEVER/" not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        "signature",
        "truncated",
        "checksum",
        "raster_short",
        "raster_long",
        "filter",
        "trailer",
        "zlib",
        "noend",
        "emptydata",
        "width",
        "height",
        "pixels",
        "inflate",
        "chunklength",
        "ancillary",
        "animated",
        "header",
        "interlace",
    ],
)
def test_invalid_or_unsupported_png_is_rejected(change):
    data = png()
    if change == "signature":
        data = b"not PNG"
    elif change == "truncated":
        data = data[:-2]
    elif change == "checksum":
        data = data[:29] + bytes([data[29] ^ 1]) + data[30:]
    elif change == "raster_short":
        data = png(raster=b"\0\0")
    elif change == "raster_long":
        data = png(raster=bytes(8))
    elif change == "filter":
        data = png(raster=b"\x05\0\0\0")
    elif change == "trailer":
        data += b"secret trailer"
    elif change == "zlib":
        data = data[:33] + chunk(b"IDAT", b"not zlib") + chunk(b"IEND", b"")
    elif change == "noend":
        data = data[:-12]
    elif change == "emptydata":
        data = data[:33] + chunk(b"IDAT", b"") + chunk(b"IEND", b"")
    elif change in {"width", "height", "pixels", "inflate"}:
        width, height, depth, color = {
            "width": (8193, 1, 8, 2),
            "height": (1, 8193, 8, 2),
            "pixels": (8192, 8192, 8, 2),
            "inflate": (4096, 4096, 16, 6),
        }[change]
        data = (
            SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0"))
            + chunk(b"IEND", b"")
        )
    elif change == "chunklength":
        data = data[:33] + struct.pack(">I", 2**32 - 1) + data[37:]
    elif change in {"ancillary", "animated"}:
        data = png(extra=[(b"tEXt" if change == "ancillary" else b"acTL", b"private")])
    elif change == "header":
        data = SIGNATURE + chunk(b"IHDR", bytes(13)) + data[33:]
    else:
        data = png(interlace=2)
    with pytest.raises(SessionMigrateError):
        validate_inline_png_data_uri(uri(data))


@pytest.mark.parametrize(
    "mutate", ["orphan", "duplicate_call", "duplicate_result", "missing_input", "missing_name"]
)
def test_image_never_loosens_tool_identity_guards(mutate):
    rows = media_rows()
    if mutate == "orphan":
        rows[2]["message"]["content"][0]["tool_use_id"] = "unpaired"
    elif mutate == "duplicate_call":
        rows[1]["message"]["content"] *= 2
    elif mutate == "duplicate_result":
        rows[2]["message"]["content"] *= 2
    else:
        rows[1]["message"]["content"][0].pop("input" if mutate == "missing_input" else "name")
    with pytest.raises(SessionMigrateError):
        parse(rows)
