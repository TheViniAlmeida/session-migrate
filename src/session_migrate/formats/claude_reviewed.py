"""Explicit, read-only conversation portability for compacted Claude 2.1.309.

Captured runtime data never becomes a destination configuration or authority.
This plan selects historical conversation, not the native request normalizer.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import struct
import zlib
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, NoReturn

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude_compaction import CompactionSelection, select_compaction
from session_migrate.formats.common import content_text
from session_migrate.jsonl import JsonlRecord
from session_migrate.model import Event, EventKind, Provenance, Role

SOURCE_REVIEWED_POLICY_ID = "claude-2.1.309-reviewed-conversation-v1"
REVIEWED_AUDIT_SELECTION_ID = "claude309-reviewed-conversation-v1"
SOURCE_REVIEWED_MEDIA_POLICY_ID = "claude-2.1.309-reviewed-inline-png-v1"
REVIEWED_MEDIA_AUDIT_SELECTION_ID = "claude309-reviewed-inline-png-v1"
MAX_REVIEWED_PNG_BYTES = 16 * 1024 * 1024
MAX_REVIEWED_PNG_DIMENSION = 8192
MAX_REVIEWED_PNG_PIXELS = 16 * 1024 * 1024
MAX_REVIEWED_PNG_INFLATED_BYTES = 64 * 1024 * 1024
_RENDERED_CLASSES = frozenset(
    {
        "instructions",
        "session_context",
        "context_sections",
        "date",
        "environment",
        "model",
        "deferred_tools_delta",
        "agent_listing_delta",
        "mcp_instructions_delta",
        "skill_listing",
        "invoked_skills",
        "hook_additional_context",
        "silent_turn_reminder",
        "remote_session_change",
        "edited_text_file",
        "queued_command",
        "compact_file_reference",
        "file",
        "auto_mode",
    }
)
_ARCHIVE_CLASSES = frozenset(
    {
        "prompt_snapshot",
        "prompt_render_point",
        "deferred_tools_record",
        "hook_success",
        "hook_system_message",
        "credential_org",
        "thinking_drop",
        "command_permissions",
        "session_cron_carry",
    }
)


@dataclass(frozen=True, slots=True)
class ReviewedCarrierPlan:
    replay_records: tuple[JsonlRecord, ...] = field(repr=False)
    replay_indices: tuple[int, ...]
    context_text: Mapping[int, tuple[str, ...]] = field(repr=False)
    events_by_record: Mapping[int, tuple[Event, ...]] = field(repr=False)
    context_provenance: Mapping[int, tuple[dict, ...]] = field(repr=False)
    report: dict = field(repr=False)
    inert_indices: tuple[int, ...] = ()
    diagnostics: tuple[str, ...] = (
        "reviewed_runtime_not_preserved",
        "source_order_not_reconstructed",
    )
    policy_id: str = SOURCE_REVIEWED_POLICY_ID


def _fail(reason: str) -> NoReturn:
    raise SessionMigrateError(f"Claude reviewed portability: {reason}")


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError, RecursionError):
        _fail("unsupported historical JSON value")


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def validate_inline_png_data_uri(uri: object) -> dict:
    """Validate a bounded static PNG without rendering or reading external data.

    The closed, non-indexed subset admits critical chunks and sRGB/gAMA/pHYs/tRNS.
    Unsupported chunks, animation and trailing/compressed surplus fail closed.
    The capture caller separately enforces its complete source byte budget.
    """
    prefix = "data:image/png;base64,"
    if type(uri) is not str or not uri.startswith(prefix):
        _fail("unsupported inline PNG data URI")
    encoded = uri[len(prefix) :]
    if not encoded or len(encoded) > ((MAX_REVIEWED_PNG_BYTES + 2) // 3) * 4:
        _fail("inline PNG exceeds the image byte limit")
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error):
        _fail("invalid inline PNG base64")
    if base64.b64encode(data).decode("ascii") != encoded:
        _fail("noncanonical inline PNG base64")
    if len(data) > MAX_REVIEWED_PNG_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        _fail("invalid or oversized inline PNG")
    offset, chunk_count = 8, 0
    header = None
    palette = None
    compressed = bytearray()
    saw_data = False
    ended = False
    ancillary: set[bytes] = set()
    while offset < len(data):
        chunk_count += 1
        if chunk_count > 4096 or len(data) - offset < 12:
            _fail("invalid inline PNG chunk framing")
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            _fail("invalid inline PNG chunk length")
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + body) != crc:
            _fail("invalid inline PNG chunk checksum")
        if header is None:
            if kind != b"IHDR" or length != 13:
                _fail("invalid inline PNG header")
            header = struct.unpack(">IIBBBBB", body)
            width, height, depth, color, compression, filtering, interlace = header
            if color == 3:
                _fail("indexed inline PNG is unsupported")
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 4: {8, 16}, 6: {8, 16}}
            if (
                not 0 < width <= MAX_REVIEWED_PNG_DIMENSION
                or not 0 < height <= MAX_REVIEWED_PNG_DIMENSION
                or width * height > MAX_REVIEWED_PNG_PIXELS
                or depth not in depths.get(color, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                _fail("unsupported inline PNG dimensions or encoding")
        elif kind == b"PLTE":
            if (
                palette is not None
                or saw_data
                or b"tRNS" in ancillary
                or color in {0, 4}
                or not length
                or length % 3
                or length > 768
            ):
                _fail("invalid inline PNG palette")
            if color == 3 and length // 3 > 2**depth:
                _fail("invalid inline PNG palette depth")
            palette = length // 3
        elif kind == b"IDAT":
            if color == 3 and palette is None:
                _fail("invalid inline PNG image data order")
            compressed.extend(body)
            saw_data = True
        elif kind == b"IEND":
            if length or not compressed or end != len(data):
                _fail("invalid inline PNG end")
            ended = True
            break
        elif kind in {b"sRGB", b"gAMA", b"pHYs", b"tRNS"}:
            if kind in ancillary or saw_data:
                _fail("invalid inline PNG ancillary order")
            ancillary.add(kind)
            if (
                (kind == b"sRGB" and (length != 1 or body[0] > 3 or palette is not None))
                or (
                    kind == b"gAMA"
                    and (length != 4 or int.from_bytes(body) == 0 or palette is not None)
                )
                or (kind == b"pHYs" and (length != 9 or body[8] > 1))
                or (
                    kind == b"tRNS"
                    and not (
                        (color == 0 and length == 2 and int.from_bytes(body) < 2**depth)
                        or (
                            color == 2
                            and length == 6
                            and all(int.from_bytes(body[i : i + 2]) < 2**depth for i in (0, 2, 4))
                        )
                        or (color == 3 and palette is not None and 0 < length <= palette)
                    )
                )
            ):
                _fail("invalid inline PNG ancillary data")
        else:
            _fail("unsupported inline PNG chunk")
        offset = end
    if header is None or not ended:
        _fail("incomplete inline PNG")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
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
    spans = [((w * channels * depth + 7) // 8, h) for w, h in passes if w and h]
    expected = sum((row + 1) * count for row, count in spans)
    if expected > MAX_REVIEWED_PNG_INFLATED_BYTES:
        _fail("inline PNG exceeds the raster safety limit")
    rows = (row for row, count in spans for _ in range(count))
    remaining = 0
    total = 0
    decoder = zlib.decompressobj()
    tail = compressed
    try:
        while tail:
            output = decoder.decompress(tail, 64 * 1024)
            tail = decoder.unconsumed_tail
            total += len(output)
            if total > expected or decoder.unused_data:
                _fail("invalid inline PNG raster length")
            position = 0
            while position < len(output):
                if not remaining:
                    remaining = next(rows, None)
                    if remaining is None or output[position] > 4:
                        _fail("invalid inline PNG scanline")
                    position += 1
                consumed = min(remaining, len(output) - position)
                remaining -= consumed
                position += consumed
        if not decoder.eof or total != expected or remaining or next(rows, None) is not None:
            _fail("incomplete inline PNG raster")
    except zlib.error:
        _fail("invalid inline PNG compressed data")
    return {
        "mime": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "decoded_bytes": len(data),
        "width": width,
        "height": height,
    }


def reviewed_context_payload(**metadata: Any) -> dict:
    return {
        "block_type": "synthetic_text",
        "reviewed_context_policy": SOURCE_REVIEWED_POLICY_ID,
        **metadata,
    }


def _provenance(record: JsonlRecord, block_index: int | None = None) -> Provenance:
    return Provenance(record.index, record.value.get("type"), record.value.get("uuid"), block_index)


def _data_event(
    record: JsonlRecord,
    value: Any,
    source_class: str,
    variant: str,
    *,
    recorded_role: str | None = None,
    block_index: int | None = None,
    pointer: str = "",
) -> Event:
    return Event(
        EventKind.CONTEXT,
        _provenance(record, block_index),
        role=Role.USER,
        timestamp=record.value.get("timestamp"),
        text=_json(
            {
                "kind": "reviewed_historical_data",
                "policy_id": SOURCE_REVIEWED_POLICY_ID,
                "source_class": source_class,
                "recorded_role": recorded_role,
                "variant": variant,
                "data": value,
            }
        ),
        payload=reviewed_context_payload(
            reviewed_data_class=source_class,
            reviewed_variant=variant,
            recorded_role=recorded_role,
            reviewed_pointer=pointer,
        ),
    )


def _result_blocks(value: Any, images: list[dict]) -> list[dict]:
    if type(value) is str:
        return [{"type": "text", "text": value}]
    if type(value) is not list:
        _fail("unsupported displayed tool result content")
    blocks = []
    for index, block in enumerate(value):
        if type(block) is str:
            blocks.append({"type": "text", "text": block})
        elif (
            type(block) is dict
            and set(block) == {"type", "text"}
            and (block["type"] == "text" and type(block["text"]) is str)
        ) or (
            type(block) is dict
            and set(block) == {"type", "tool_name"}
            and block["type"] == "tool_reference"
            and type(block["tool_name"]) is str
            and block["tool_name"]
        ):
            blocks.append(dict(block))
        elif type(block) is dict and set(block) == {"type", "source"} and block["type"] == "image":
            source = block["source"]
            if (
                type(source) is not dict
                or set(source) != {"type", "media_type", "data"}
                or source["type"] != "base64"
                or source["media_type"] != "image/png"
                or type(source["data"]) is not str
            ):
                _fail("unsupported displayed inline PNG source")
            uri = f"data:image/png;base64,{source['data']}"
            metadata = validate_inline_png_data_uri(uri)
            images.append({"content_block_index": index, **metadata, "source_sha256": _sha(block)})
            blocks.append({"type": "image", "image_url": uri})
        else:
            _fail("unsupported displayed tool result block or media")
    return blocks


def _conversation_events(
    record: JsonlRecord, difference, thinking: list[dict], images: list[dict]
) -> list[Event]:
    value = record.value
    message = value["message"]
    content = message.get("content")
    role = Role(value["type"])
    if value.get("isCompactSummary") is True or value.get("isMeta") is True:
        if role is not Role.USER:
            _fail("invalid reviewed historical context role")
        texts = (
            [content]
            if type(content) is str
            else [
                block["text"]
                for block in content
                if type(block) is dict
                and set(block) == {"type", "text"}
                and block["type"] == "text"
                and type(block["text"]) is str
            ]
            if type(content) is list
            else []
        )
        if not texts or (type(content) is list and len(texts) != len(content)):
            _fail("unsupported compact summary or metadata content")
        summary = value.get("isCompactSummary") is True
        difference(
            "summary_as_reviewed_context" if summary else "metadata_conversation_as_data",
            record.index,
        )
        return [
            Event(
                EventKind.CONTEXT,
                _provenance(record, i if type(content) is list else None),
                role=Role.USER,
                text=text
                if summary
                else _data_event(
                    record, text, "metadata_conversation", "message", recorded_role="user"
                ).text,
                timestamp=value.get("timestamp"),
                payload=reviewed_context_payload(
                    reviewed_data_class="summary" if summary else "metadata_conversation"
                ),
            )
            for i, text in enumerate(texts)
        ]
    if type(content) is str:
        return [
            Event(
                EventKind.MESSAGE,
                _provenance(record),
                role=role,
                text=content,
                timestamp=value.get("timestamp"),
            )
        ]
    if type(content) is not list:
        _fail("unsupported conversation content")
    events: list[Event] = []
    for index, block in enumerate(content):
        if type(block) is not dict:
            _fail("unsupported conversation block")
        kind = block.get("type")
        if type(kind) is not str:
            _fail("malformed displayed conversation block type")
        common = {"provenance": _provenance(record, index), "timestamp": value.get("timestamp")}
        if kind == "text" and type(block.get("text")) is str:
            if set(block) != {"type", "text"}:
                _fail("unsupported displayed text fields")
            events.append(Event(EventKind.MESSAGE, role=role, text=block["text"], **common))
        elif kind == "tool_use" and role is Role.ASSISTANT:
            if (
                any(type(block.get(key)) is not str or not block[key] for key in ("id", "name"))
                or "input" not in block
            ):
                _fail("malformed displayed tool call")
            _json(block["input"])
            events.append(
                Event(
                    EventKind.TOOL_CALL,
                    role=role,
                    tool_call_id=block["id"],
                    tool_name=block["name"],
                    payload={"input": copy.deepcopy(block["input"])},
                    **common,
                )
            )
            if set(block) - {"type", "id", "name", "input"}:
                difference("tool_block_metadata_archived", record.index)
        elif kind == "tool_result" and role is Role.USER:
            if (
                type(block.get("tool_use_id")) is not str
                or not block["tool_use_id"]
                or "content" not in block
            ):
                _fail("malformed displayed tool result")
            if "is_error" in block and type(block["is_error"]) is not bool:
                _fail("malformed displayed tool error flag")
            result_images: list[dict] = []
            blocks = _result_blocks(block["content"], result_images)
            if result_images and block.get("is_error") is True:
                _fail("inline PNG in an error tool result is unsupported")
            images.extend(
                {"record_index": record.index, "block_index": index, **item}
                for item in result_images
            )
            for item in blocks:
                if item["type"] == "tool_reference":
                    difference("tool_registry_reference_not_restored", record.index)
            events.append(
                Event(
                    EventKind.TOOL_RESULT,
                    role=Role.TOOL,
                    tool_call_id=block["tool_use_id"],
                    text=content_text(block["content"]),
                    payload={"content_blocks": blocks, "is_error": block.get("is_error") is True},
                    **common,
                )
            )
            if set(block) - {"type", "tool_use_id", "content", "is_error"}:
                difference("tool_result_metadata_archived", record.index)
        elif kind in {"thinking", "redacted_thinking"} and role is Role.ASSISTANT:
            key = "thinking" if kind == "thinking" else "data"
            if type(block.get(key)) is not str:
                _fail("malformed captured thinking block")
            events.append(
                _data_event(
                    record,
                    block,
                    "thinking",
                    kind,
                    recorded_role="assistant",
                    block_index=index,
                    pointer=f"/message/content/{index}",
                )
            )
            thinking.append(
                {"record_index": record.index, "block_index": index, "sha256": _sha(block)}
            )
            difference("thinking_as_quoted_data", record.index)
        else:
            _fail("unsupported displayed conversation block or media")
    return events


def _validate_nested_memory(attachment: dict) -> None:
    """Accept only the captured project-memory shape; paths remain inert data."""
    if (
        set(attachment) != {"type", "path", "displayPath", "content"}
        or type(attachment["path"]) is not str
        or not attachment["path"]
        or type(attachment["displayPath"]) is not str
        or type(attachment["content"]) is not dict
    ):
        _fail("malformed captured nested memory")
    content = attachment["content"]
    if (
        not {"path", "type", "content"} <= set(content)
        or set(content) - {"path", "type", "content", "contentDiffersFromDisk", "rawContent"}
        or type(content["path"]) is not str
        or content["path"] != attachment["path"]
        or type(content["type"]) is not str
        or content["type"] != "Project"
        or type(content["content"]) is not str
        or (
            "contentDiffersFromDisk" in content
            and type(content["contentDiffersFromDisk"]) is not bool
        )
        or ("rawContent" in content and type(content["rawContent"]) is not str)
    ):
        _fail("malformed captured nested memory")


def _rendered_events(record: JsonlRecord, source_class: str, difference) -> list[Event]:
    value = record.value
    result: list[Event] = []
    for variant in ("rendered", "renderedInHumanTurn"):
        if variant not in value:
            continue
        if (
            type(value.get("renderedRole")) is not str
            or value["renderedRole"] not in {"user", "system"}
            or type(value[variant]) is not list
        ):
            _fail("malformed recorded rendering role or variant")
        if variant == "renderedInHumanTurn":
            difference("native_render_variant_not_selected", record.index)
        for entry_index, entry in enumerate(value[variant]):
            if type(entry) is not dict or set(entry) != {"content"}:
                _fail("unsupported recorded rendering entry")
            content = entry["content"]
            texts = (
                [content]
                if type(content) is str
                else [
                    block["text"]
                    for block in content
                    if type(block) is dict
                    and set(block) == {"type", "text"}
                    and block["type"] == "text"
                    and type(block["text"]) is str
                ]
                if type(content) is list
                else []
            )
            if type(content) not in {str, list} or (
                type(content) is list and len(texts) != len(content)
            ):
                _fail("unsupported recorded rendering block")
            for block_index, text in enumerate(texts):
                pointer = f"/{variant}/{entry_index}/content"
                if type(content) is list:
                    pointer += f"/{block_index}"
                result.append(
                    _data_event(
                        record,
                        text,
                        source_class,
                        variant,
                        recorded_role=value["renderedRole"],
                        block_index=len(result),
                        pointer=pointer,
                    )
                )
    return result


def plan_reviewed_carriers(
    records: Sequence[JsonlRecord], selection: CompactionSelection
) -> ReviewedCarrierPlan:
    """Plan preserved historical content; never apply native runtime controls."""
    if selection is None:
        _fail("requires a proven preserved compaction selection")
    expected = select_compaction(records)
    if expected is None or selection != expected:
        _fail("inconsistent reviewed graph selection")
    by_index = {record.index: record for record in records}
    if len(by_index) != len(records) or any(
        by_index.get(r.index) is not r for r in selection.selected_records
    ):
        _fail("inconsistent reviewed source records")
    children: dict[int, list[int]] = defaultdict(list)
    for index, parent in selection.effective_parents.items():
        if index not in by_index or (parent is not None and parent not in by_index):
            _fail("dangling reviewed metadata parent")
        if parent is not None and by_index[index].value["type"] not in {"user", "assistant"}:
            children[parent].append(index)
    for values in children.values():
        values.sort()
    seen = set(selection.selected_indices)
    pending = deque([selection.original_leaf_index])
    closure: list[int] = []
    while pending:
        for index in children.get(pending.popleft(), ()):
            if index in seen:
                continue
            seen.add(index)
            closure.append(index)
            pending.append(index)
    replay = (*selection.selected_records, *(by_index[index] for index in closure))
    differences: Counter[str] = Counter()
    difference_indices: dict[str, set[int]] = defaultdict(set)

    def difference(code: str, index: int) -> None:
        differences[code] += 1
        difference_indices[code].add(index)

    archived: list[dict] = []
    thinking: list[dict] = []
    system: list[dict] = []
    references: list[dict] = []
    wire: list[dict] = []
    tool_references: list[dict] = []
    images: list[dict] = []
    events_by_record: dict[int, tuple[Event, ...]] = {}
    calls: dict[str, Event] = {}
    results: set[str] = set()
    for record in replay:
        value = record.value
        kind = value["type"]
        events: list[Event] = []
        if kind in {"user", "assistant"}:
            events = _conversation_events(record, difference, thinking, images)
            for event in events:
                if event.kind is EventKind.TOOL_CALL:
                    if event.tool_call_id in calls:
                        _fail("duplicate displayed tool call identity")
                    calls[event.tool_call_id] = event
                    copies = value.get("wireToolInputs")
                    if copies is not None:
                        if type(copies) is not dict or event.tool_call_id not in copies:
                            _fail("unresolved displayed and wire tool input binding")
                        different = _json(event.payload["input"]) != _json(
                            copies[event.tool_call_id]
                        )
                        wire.append(
                            {
                                "record_index": record.index,
                                "block_index": event.provenance.block_index,
                                "displayed_sha256": _sha(event.payload["input"]),
                                "wire_sha256": _sha(copies[event.tool_call_id]),
                                "different": different,
                            }
                        )
                        difference("wire_input_selection_not_restored", record.index)
                        if different:
                            difference("wire_display_difference", record.index)
                elif event.kind is EventKind.TOOL_RESULT:
                    if event.tool_call_id not in calls or event.tool_call_id in results:
                        _fail("unpaired or ambiguous displayed tool result")
                    results.add(event.tool_call_id)
                    for block_index, block in enumerate(event.payload["content_blocks"]):
                        if block["type"] == "tool_reference":
                            tool_references.append(
                                {
                                    "record_index": record.index,
                                    "block_index": event.provenance.block_index,
                                    "content_block_index": block_index,
                                    "sha256": _sha(block),
                                }
                            )
            if "wireIngestContext" in value:
                difference("wire_ingest_context_not_restored", record.index)
            if value.get("toolUseResult") is not None:
                difference("tool_result_runtime_metadata_archived", record.index)
            extra_rendering = _rendered_events(record, "conversation_rendering", difference)
            events.extend(extra_rendering)
            if extra_rendering and value.get("renderedRole") == "system":
                system.append({"record_index": record.index, "sha256": _sha(value.get("rendered"))})
                difference("recorded_system_authority_not_replayed", record.index)
        elif kind == "attachment":
            attachment = value.get("attachment")
            if type(attachment) is not dict or type(attachment.get("type")) is not str:
                _fail("malformed historical attachment")
            source_class = attachment["type"]
            if source_class == "nested_memory":
                _validate_nested_memory(attachment)
                events = _rendered_events(record, source_class, difference)
                if not events:
                    _fail("missing captured historical rendering")
                if value.get("renderedRole") == "system":
                    system.append(
                        {"record_index": record.index, "sha256": _sha(value.get("rendered"))}
                    )
                    difference("recorded_system_authority_not_replayed", record.index)
                events.append(
                    _data_event(
                        record,
                        attachment,
                        source_class,
                        "captured_payload",
                        block_index=len(events),
                        pointer="/attachment",
                    )
                )
                difference("nested_memory_payload_as_quoted_data", record.index)
                difference("captured_memory_variants_not_reconciled", record.index)
                difference("recorded_carrier_as_quoted_data", record.index)
            elif source_class in _RENDERED_CLASSES:
                events = _rendered_events(record, source_class, difference)
                if not events:
                    _fail("missing captured historical rendering")
                if value.get("renderedRole") == "system":
                    system.append(
                        {"record_index": record.index, "sha256": _sha(value.get("rendered"))}
                    )
                    difference("recorded_system_authority_not_replayed", record.index)
                if source_class == "file" and "content" in attachment:
                    events.append(
                        _data_event(
                            record,
                            attachment["content"],
                            "cached_file",
                            "cached",
                            pointer="/attachment/content",
                        )
                    )
                    difference("cached_and_rendered_retained_as_data", record.index)
                if source_class == "edited_text_file":
                    if type(attachment.get("snippet")) is not str:
                        _fail("malformed captured file snippet")
                    events.append(
                        _data_event(
                            record,
                            attachment["snippet"],
                            "file_snippet",
                            "snippet",
                            pointer="/attachment/snippet",
                        )
                    )
                    difference("snippet_retained_as_partial_data", record.index)
                if source_class == "compact_file_reference":
                    references.append({"record_index": record.index, "sha256": _sha(attachment)})
                    events.append(_data_event(record, attachment, "reference_only", "reference"))
                    difference("referenced_content_not_present", record.index)
                if source_class == "queued_command":
                    events.append(
                        _data_event(record, attachment, "queued_command", "captured_payload")
                    )
                    difference("queued_command_retained_as_data", record.index)
                difference("recorded_carrier_as_quoted_data", record.index)
            elif source_class in _ARCHIVE_CLASSES:
                archived.append(
                    {
                        "record_index": record.index,
                        "record_class": source_class,
                        "sha256": _sha(value),
                    }
                )
                code = {
                    "prompt_snapshot": "source_system_baseline_not_restored",
                    "deferred_tools_record": "source_tool_registry_not_restored",
                    "prompt_render_point": "source_render_controls_not_restored",
                    "credential_org": "account_identity_not_transferred",
                }.get(source_class, "source_runtime_record_archived")
                difference(code, record.index)
                if source_class in {"hook_success", "hook_system_message"}:
                    difference("source_runtime_hook_state_unavailable", record.index)
                if source_class == "prompt_snapshot":
                    difference("source_render_controls_not_restored", record.index)
            else:
                _fail("unsupported historical attachment class")
        elif kind == "system":
            subtype = value.get("subtype")
            if type(subtype) is not str:
                _fail("malformed historical system record")
            if subtype in {"local_command", "informational"} and type(value.get("content")) is str:
                events.append(
                    _data_event(
                        record,
                        value["content"],
                        subtype,
                        "content",
                        recorded_role="system",
                        pointer="/content" if subtype == "informational" else "",
                    )
                )
                difference("recorded_system_authority_not_replayed", record.index)
                if subtype == "informational":
                    difference("system_informational_as_quoted_data", record.index)
                system.append({"record_index": record.index, "sha256": _sha(value["content"])})
            elif subtype in {"compact_boundary", "stop_hook_summary", "turn_duration"}:
                archived.append(
                    {"record_index": record.index, "record_class": subtype, "sha256": _sha(value)}
                )
                difference("source_runtime_record_archived", record.index)
                if subtype == "stop_hook_summary":
                    difference("source_runtime_hook_state_unavailable", record.index)
            else:
                _fail("unsupported historical system record")
        else:
            _fail("unsupported historical record")
        events_by_record[record.index] = tuple(events)
    open_calls = [
        {
            "record_index": call.provenance.record_index,
            "block_index": call.provenance.block_index,
            "sha256": _sha(call.payload["input"]),
        }
        for call_id, call in calls.items()
        if call_id not in results
    ]
    for call in open_calls:
        difference("open_tool_call_requires_explicit_archive", call["record_index"])
    difference("reviewed_runtime_not_preserved", selection.boundary_index)
    difference("source_order_not_reconstructed", selection.boundary_index)
    policy_id = SOURCE_REVIEWED_POLICY_ID
    if images:
        policy_id = SOURCE_REVIEWED_MEDIA_POLICY_ID
        for item in images:
            for code in (
                "tool_result_inline_png_transport",
                "chat_tool_images_relocated_to_user",
                "chat_tool_images_have_positional_association",
                "chat_tool_result_text_before_images",
            ):
                difference(code, item["record_index"])
        for index, events in events_by_record.items():
            rewritten = []
            for event in events:
                if event.kind is EventKind.CONTEXT:
                    text = event.text
                    if event.payload.get("reviewed_data_class") != "summary":
                        quoted = json.loads(text)
                        quoted["policy_id"] = policy_id
                        text = _json(quoted)
                    event = replace(
                        event,
                        text=text,
                        payload={**event.payload, "reviewed_context_policy": policy_id},
                    )
                rewritten.append(event)
            events_by_record[index] = tuple(rewritten)
    context = {
        index: tuple(event.text for event in events if event.kind is EventKind.CONTEXT)
        for index, events in events_by_record.items()
        if any(event.kind is EventKind.CONTEXT for event in events)
    }
    provenance = {
        index: tuple(
            {
                "block_index": event.provenance.block_index,
                "pointer": event.payload.get("reviewed_pointer", ""),
                "sha256": _sha(event.text),
            }
            for event in events
            if event.kind is EventKind.CONTEXT
        )
        for index, events in events_by_record.items()
        if index in context
    }
    replay_indices = tuple(record.index for record in replay)
    excluded = tuple(record.index for record in records if record.index not in seen)
    report = {
        "policy_id": policy_id,
        "source_record_count": len(records),
        "logical_record_count": len(records),
        "duplicate_record_count": 0,
        "selected_count": len(replay),
        "graph_selected_count": len(selection.selected_indices),
        "recovered_count": len(selection.recovered_indices),
        "excluded_count": len(excluded),
        "retired_count": len(selection.retired_indices),
        "model_visible_context_count": sum(map(len, context.values())),
        "archived_runtime_records": archived,
        "system_authority_as_data": {"count": len(system), "provenance": system},
        "thinking_as_data": {"count": len(thinking), "provenance": thinking},
        "displayed_vs_wire_inputs": {
            "count": sum(item["different"] for item in wire),
            "displayed_count": len(calls),
            "wire_count": len(wire),
            "difference_count": sum(item["different"] for item in wire),
            "provenance": wire,
        },
        "unresolved_file_references": {"count": len(references), "provenance": references},
        "open_tool_calls": {"count": len(open_calls), "provenance": open_calls},
        "tool_references": {"count": len(tool_references), "provenance": tool_references},
        "differences": [
            {"code": code, "count": count, "record_indices": sorted(difference_indices[code])}
            for code, count in sorted(differences.items())
        ],
        "provenance": {
            "selected_indices": list(selection.selected_indices),
            "parent_chain_indices": list(selection.parent_chain_indices),
            "recovered_indices": list(selection.recovered_indices),
            "closure_indices": closure,
            "replay_indices": list(replay_indices),
            "retired_indices": list(selection.retired_indices),
            "excluded_indices": list(excluded),
            "archived_indices": [item["record_index"] for item in archived],
        },
    }
    if images:
        report["inline_tool_result_images"] = {"count": len(images), "provenance": images}
        report["image_transport"] = {
            "media_type": "image/png",
            "native_store": "Image bytes and their tool call/result association are preserved.",
            "chat": (
                "Chat emits tool text first and moves images to a following USER message; original "
                "text/image interleaving is not preserved on wire. Their wire association "
                "is positional; the image has no explicit tool call ID."
            ),
            "responses": (
                "Responses keeps each image inside function_call_output with its tool call ID."
            ),
            "limits": {
                "max_decoded_png_bytes": MAX_REVIEWED_PNG_BYTES,
                "max_dimension": MAX_REVIEWED_PNG_DIMENSION,
                "max_pixels": MAX_REVIEWED_PNG_PIXELS,
                "max_inflated_bytes": MAX_REVIEWED_PNG_INFLATED_BYTES,
                "indexed_color_supported": False,
            },
        }
    return ReviewedCarrierPlan(
        tuple(replay),
        replay_indices,
        MappingProxyType(context),
        MappingProxyType(events_by_record),
        MappingProxyType(provenance),
        report,
        policy_id=policy_id,
    )
