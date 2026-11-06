"""Closed historical carrier replay for Claude Code 2.1.309.

Only persisted user-role text and two demonstrated inert shapes are supported.
This reconstructs transcript normalization, not unavailable runtime hook state.
Source envelopes are borrowed unchanged; no source path or command is evaluated.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any, NoReturn

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude_compaction import (
    PERSISTED_CONTROL_TYPES,
    CompactionSelection,
    select_compaction,
)
from session_migrate.jsonl import JsonlRecord

POLICY_ID = "claude-2.1.309-closed-historical-carriers-v1"
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_EMPTY_ASSISTANT_TEXT = {"", "(no content)", "[Empty text removed]"}
_ENVELOPE = frozenset(
    {"type", "uuid", "parentUuid", "sessionId", "cwd", "version", "timestamp", "isSidechain"}
)
_REASONS = frozenset(
    {
        "session_start",
        "compaction",
        "policy_refresh",
        "managed_folder_check",
        "directory_added",
        "settings_sync",
        "account_change",
        "hooks_invalidate",
        "policy_verdict",
        "project_reread",
        "memory_paused",
        "memory_resumed",
        "auto_memory_off",
        "auto_memory_back_on",
    }
)


@dataclass(frozen=True, slots=True)
class CarrierPlan:
    """Immutable replay/provenance plan; text never appears in its repr."""

    replay_records: tuple[JsonlRecord, ...] = field(repr=False)
    replay_indices: tuple[int, ...]
    context_text: Mapping[int, tuple[str, ...]] = field(repr=False)
    inert_indices: tuple[int, ...]
    diagnostics: tuple[str, ...]
    policy_id: str = POLICY_ID


def _fail(reason: str) -> NoReturn:
    # Fixed implementation literals only: source values may contain secrets.
    raise SessionMigrateError(f"Claude 2.1.309 historical carriers: {reason}")


def _text(value: Any) -> bool:
    return type(value) is str and bool(value)


def _keys(value: Mapping[str, Any], allowed: set[str] | frozenset[str]) -> None:
    if not value.keys() <= allowed:
        _fail("unsupported carrier field or override")


def _guard_native_text(text: str) -> None:
    prefix = "<system-reminder"
    if text.startswith(prefix) and len(text) > len(prefix):
        following = text[len(prefix)]
        if following in _JS_WHITESPACE + "\x1c\x1d\x1e\x1f\x85":
            _fail("text requires unsupported native normalization")


def _rendered(value: Mapping[str, Any]) -> tuple[str, ...]:
    if value.get("renderedRole") != "user":
        _fail("persisted carrier requires explicit user authority")
    entries = value.get("rendered")
    if not isinstance(entries, list) or not entries:
        _fail("missing or malformed persisted carrier rendering")
    texts: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"content"}:
            _fail("unsupported persisted rendering entry")
        content = entry["content"]
        if _text(content):
            texts.append(content)
        elif isinstance(content, list) and content:
            for block in content:
                if (
                    not isinstance(block, Mapping)
                    or set(block) != {"type", "text"}
                    or block["type"] != "text"
                    or not _text(block["text"])
                ):
                    _fail("unsupported persisted rendering block")
                texts.append(block["text"])
        else:
            _fail("missing or malformed persisted carrier rendering")
    for text in texts:
        _guard_native_text(text)
    return tuple(texts)


def _attachment_shape(attachment: Mapping[str, Any]) -> None:
    kind = attachment.get("type")
    if kind == "instructions":
        _keys(attachment, {"type", "files", "removed", "changed", "reason"})
        files = attachment.get("files")
        if not isinstance(files, list):
            _fail("malformed instructions files")
        for item in files:
            if not isinstance(item, Mapping):
                _fail("malformed instructions file")
            _keys(item, {"path", "type", "content", "unverified"})
            if (
                not _text(item.get("path"))
                or not _text(item.get("type"))
                or item["type"] not in {"User", "Project", "Local", "Managed", "AutoMem"}
                or type(item.get("content")) is not str
                or ("unverified" in item and type(item["unverified"]) is not bool)
            ):
                _fail("malformed instructions file")
        if "removed" in attachment and (
            not isinstance(attachment["removed"], list)
            or not all(_text(item) for item in attachment["removed"])
        ):
            _fail("malformed removed instructions")
        if "reason" in attachment and (
            not _text(attachment["reason"]) or attachment["reason"] not in _REASONS
        ):
            _fail("unsupported instructions reason")
    elif kind == "context_sections":
        _keys(attachment, {"type", "sections"})
        sections = attachment.get("sections")
        if not isinstance(sections, list) or any(
            not isinstance(item, Mapping)
            or set(item) != {"name", "text"}
            or type(item["name"]) is not str
            or type(item["text"]) is not str
            for item in sections
        ):
            _fail("malformed context sections")
    elif kind == "date":
        _keys(attachment, {"type", "date", "changed"})
        if type(attachment.get("date")) is not str:
            _fail("malformed date carrier")
    else:
        _fail("unsupported active attachment kind")
    if "changed" in attachment and type(attachment["changed"]) is not bool:
        _fail("malformed carrier changed flag")


def has_truncated_tool_result_context(record: JsonlRecord) -> bool:
    """Detect SNs' persisted input to an unmodeled file truncation reminder."""
    value = record.value
    result = value.get("toolUseResult")
    file = result.get("file") if isinstance(result, Mapping) else None
    message = value.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    return (
        isinstance(file, Mapping)
        and file.get("truncatedByTokenCap") is True
        and type(file.get("filePath")) is str
        and isinstance(content, list)
        and any(
            isinstance(block, Mapping) and block.get("type") == "tool_result" for block in content
        )
    )


def _validate_assistant_block_order(records: Sequence[JsonlRecord]) -> None:
    """Decline HZn-sensitive content without assuming echoApiBlockOrder.

    Native xC can concatenate same-ID assistants across tool-result users and
    metadata. Conservatively inspect every selected member of each batch; a
    later non-tool block may otherwise move before an earlier tool call.
    """
    tools_seen: set[str | int] = set()
    for record in records:
        if record.value.get("type") != "assistant":
            continue
        message = record.value["message"]
        batch = message.get("id")
        key = batch if type(batch) is str and batch else record.index
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                tools_seen.add(key)
            elif key in tools_seen:
                _fail("native assistant block order requires an unsupported runtime flag")


def _carrier(record: JsonlRecord) -> tuple[str, ...] | None:
    value = record.value
    if has_truncated_tool_result_context(record):
        _fail("persisted tool result truncation context is unsupported")
    kind = value.get("type")
    if type(kind) is not str:
        _fail("malformed active carrier identity")
    if kind in {"user", "assistant"}:
        if (
            value.get("isVirtual", False) is not False
            or "taskDelivery" in value
            or any(key.startswith("rendered") for key in value)
            or value.get("isApiErrorMessage", False) is not False
            or value.get("resumedFromIncompleteThinking", False) is not False
            or any(key in value for key in ("batchToolUses", "wireToolInputs", "wireIngestContext"))
        ):
            _fail("unsupported conversation delivery or rendering override")
        if "origin" in value and value["origin"] != {"kind": "human"}:
            _fail("unsupported conversation origin")
        message = value.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if kind == "assistant":
            visible = (
                type(content) is str and content.strip(_JS_WHITESPACE) not in _EMPTY_ASSISTANT_TEXT
            )
            if isinstance(content, list):
                visible = any(
                    isinstance(block, Mapping)
                    and (
                        block.get("type") == "tool_use"
                        or block.get("type") == "text"
                        and type(block.get("text")) is str
                        and block["text"].strip(_JS_WHITESPACE) not in _EMPTY_ASSISTANT_TEXT
                    )
                    for block in content
                )
            if not visible:
                _fail("assistant normalization needs an unsupported batch contract")
        if kind == "user":
            if type(content) is str:
                _guard_native_text(content)
            elif isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, Mapping)
                        and block.get("type") == "text"
                        and type(block.get("text")) is str
                    ):
                        _guard_native_text(block["text"])
        return None
    if kind == "system" and value.get("subtype") == "compact_boundary":
        _keys(value, _ENVELOPE | {"subtype", "content", "logicalParentUuid", "compactMetadata"})
        return ()
    if kind == "system":
        subtype = value.get("subtype")
        if subtype == "local_command":
            _keys(value, _ENVELOPE | {"subtype", "content"})
            if type(value.get("content")) is not str or not value["content"]:
                _fail("malformed local command content")
            return (value["content"],)
        if subtype == "turn_duration":
            _keys(value, _ENVELOPE | {"subtype", "content", "durationMs", "messageCount"})
            if (
                type(value.get("content")) is not str
                or type(value.get("durationMs")) not in {int, float}
                or (type(value["durationMs"]) is float and not isfinite(value["durationMs"]))
                or value["durationMs"] < 0
                or type(value.get("messageCount")) is not int
                or value["messageCount"] < 0
            ):
                _fail("malformed inert duration metadata")
            return ()
        _fail("unsupported active system kind")
    if kind != "attachment":
        _fail("unsupported active carrier kind")
    attachment = value.get("attachment")
    if not isinstance(attachment, Mapping):
        _fail("malformed active attachment")
    if attachment.get("type") == "hook_system_message":
        _keys(value, _ENVELOPE | {"attachment"})
        if set(attachment) != {"type", "content", "hookName", "hookEvent"} or not all(
            type(attachment.get(key)) is str for key in ("content", "hookName", "hookEvent")
        ):
            _fail("unsupported inert hook metadata shape")
        return ()
    _keys(value, _ENVELOPE | {"attachment", "rendered", "renderedRole"})
    _attachment_shape(attachment)
    return _rendered(value)


def _barrier(record: JsonlRecord) -> bool:
    if record.value.get("type") == "assistant":
        return True
    message = record.value.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    return (
        record.value.get("type") == "user"
        and isinstance(content, list)
        and bool(content)
        and isinstance(content[0], Mapping)
        and content[0].get("type") == "tool_result"
    )


def _hoist(records: Sequence[JsonlRecord]) -> tuple[JsonlRecord, ...]:
    # OAs stable round hoist. Restricting the active attachment predicate is
    # equivalent here because the only other supported attachment is inert.
    rounds: list[list[JsonlRecord]] = [[]]
    for record in records:
        if _barrier(record):
            rounds.append([record])
        else:
            rounds[-1].append(record)
    result: list[JsonlRecord] = []
    for group in rounds:
        barrier = bool(group) and _barrier(group[0])
        start = 1 if barrier else 0
        if barrier:
            result.append(group[0])
        movable = [record for record in group[start:] if record.value.get("type") == "attachment"]
        result.extend(movable)
        movable_indices = {record.index for record in movable}
        result.extend(record for record in group[start:] if record.index not in movable_indices)
    return tuple(result)


def plan_carriers(records: Sequence[JsonlRecord], selection: CompactionSelection) -> CarrierPlan:
    """Validate all active carriers, restore fpo metadata, and apply native OAs.

    The native fpo flag is unavailable: both algorithms must agree on material
    order. Inert rows can differ in order without changing model context.
    """
    if len({record.index for record in records}) != len(records):
        _fail("duplicate physical record index")
    if any(left.index >= right.index for left, right in zip(records, records[1:], strict=False)):
        _fail("source records are not in physical order")
    by_index = {record.index: record for record in records}
    uuids = [record.value.get("uuid") for record in records if "uuid" in record.value]
    if any(not _text(uid) for uid in uuids) or len(set(uuids)) != len(uuids):
        _fail("malformed or duplicate record UUID")
    # This is a transcript control record, not a keyword in arbitrary tool data.
    if any(record.value.get("type") == "content-replacement" for record in records):
        _fail("persisted content replacement is unsupported")
    controls = tuple(
        record.index
        for record in records
        if (
            isinstance(attachment := record.value.get("attachment"), Mapping)
            and type(attachment.get("type")) is str
            and attachment["type"] in PERSISTED_CONTROL_TYPES
        )
    )
    if controls:
        # Only upo's actual pre-boundary deletions are inert. Recompute the
        # source contract so forged retired/live/preserved fields cannot hide
        # controls which still reach snapshot, tool or fpo consumers.
        expected = select_compaction(records)
        if expected is None or selection != expected:
            _fail("inconsistent compaction retirement selection")
        if not set(controls) <= set(expected.retired_indices):
            _fail("persisted prompt or tool control is unsupported")
    if (
        not selection.selected_indices
        or len(set(selection.selected_indices)) != len(selection.selected_indices)
        or tuple(record.index for record in selection.selected_records)
        != selection.selected_indices
        or any(by_index.get(record.index) is not record for record in selection.selected_records)
    ):
        _fail("inconsistent compaction selection")
    children: dict[int, list[int]] = {}
    for index, parent in selection.effective_parents.items():
        if index not in by_index or (parent is not None and parent not in by_index):
            _fail("dangling effective metadata parent")
        if parent is not None and by_index[index].value.get("type") not in {"user", "assistant"}:
            children.setdefault(parent, []).append(index)
    for child_list in children.values():
        child_list.sort()  # Native Map insertion order is physical source order.
    selected = set(selection.selected_indices)
    leaf = selection.original_leaf_index
    if (
        leaf not in selected
        or not selection.parent_chain_indices
        or leaf != selection.parent_chain_indices[-1]
    ):
        _fail("inconsistent original compaction leaf")
    queue = deque([leaf])
    bfs: list[int] = []
    seen = set(selected)
    while queue:
        for child in children.get(queue.popleft(), []):
            if child in seen:
                _fail("cyclic or repeated metadata descendant")
            seen.add(child)
            bfs.append(child)
            queue.append(child)
    texts: dict[int, tuple[str, ...]] = {}
    inert: list[int] = []
    for index in (*selection.selected_indices, *bfs):
        result = _carrier(by_index[index])
        if result:
            texts[index] = result
        elif result == ():
            inert.append(index)
    if len(bfs) > 1 and any(index in texts for index in bfs):
        if any(
            not _text(by_index[index].value.get("timestamp"))
            or not by_index[index].value["timestamp"].isascii()
            for index in bfs
        ):
            _fail("unproven metadata descendant timestamp order")

        def key(index: int) -> str:
            return by_index[index].value["timestamp"]

        bfs.sort(key=key)
        dfs: list[int] = []
        stack = list(reversed(sorted(children.get(leaf, []), key=key)))
        while stack:
            index = stack.pop()
            dfs.append(index)
            stack.extend(reversed(sorted(children.get(index, []), key=key)))
        if [index for index in dfs if index in texts] != [index for index in bfs if index in texts]:
            _fail("ambiguous native metadata material order")
    replay = _hoist((*selection.selected_records, *(by_index[index] for index in bfs)))
    _validate_assistant_block_order(replay)
    diagnostics = ["runtime_hook_transformations_unproven"]
    if bfs:
        diagnostics.append("native_metadata_descendants_restored")
    if any(by_index[index].value.get("subtype") == "turn_duration" for index in inert):
        diagnostics.append("inert_turn_duration_omitted")
    if any(by_index[index].value.get("type") == "attachment" for index in inert):
        diagnostics.append("inert_hook_runtime_metadata_omitted")
    return CarrierPlan(
        replay_records=replay,
        replay_indices=tuple(record.index for record in replay),
        context_text=MappingProxyType(texts),
        inert_indices=tuple(inert),
        diagnostics=tuple(diagnostics),
    )
