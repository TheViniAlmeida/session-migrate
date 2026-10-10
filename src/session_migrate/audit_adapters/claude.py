"""Claude 2.1.209 closed raw grammar and independent graph membership guards."""

from __future__ import annotations

from session_migrate.audit_adapters import (
    ACCOUNTING,
    BOOL,
    COORDINATE,
    DISCRIMINATOR,
    EXCLUDED,
    ID,
    LOSS,
    NUMBER,
    SEMANTIC,
    STRUCTURE,
    UNKNOWN,
    AdapterResult,
    Rule,
    S,
    Taxonomy,
    array,
    digest,
    dynamic,
    obj,
    scalar,
    subtree,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude as parser


def content(value: object) -> Rule:
    if type(value) is str:
        return scalar(LOSS, str) if value == "" else S
    if type(value) is list:
        return array(dynamic(block))
    return subtree(UNKNOWN)


def block(value: object) -> Rule:
    if type(value) is not dict:
        return subtree(UNKNOWN)
    kind = value.get("type")
    common = {"type": DISCRIMINATOR}
    if kind == "text":
        return obj(
            {**common, "text": scalar(LOSS, str) if value.get("text") == "" else S},
            required=("type", "text"),
        )
    if kind == "tool_use":
        return obj(
            {**common, "id": ID, "name": S, "input": subtree(SEMANTIC)},
            required=("id", "name", "input"),
        )
    if kind == "tool_result":
        return obj(
            {
                **common,
                "tool_use_id": ID,
                "content": dynamic(content),
                "is_error": scalar(SEMANTIC, bool),
            },
            required=("tool_use_id", "content"),
        )
    if kind == "image":
        source = value.get("source")
        if type(source) is dict and source.get("type") == "base64":
            source_rule = obj(
                {"type": DISCRIMINATOR, "media_type": S, "data": S},
                category=SEMANTIC,
                required=("type", "media_type", "data"),
            )
        elif type(source) is dict and source.get("type") == "url":
            source_rule = obj(
                {"type": DISCRIMINATOR, "url": S}, category=SEMANTIC, required=("type", "url")
            )
        else:
            source_rule = subtree(UNKNOWN)
        return obj({**common, "source": source_rule}, required=("source",))
    if kind in {"thinking", "redacted_thinking"}:
        return obj(
            {
                **common,
                "thinking": scalar(LOSS, str),
                "signature": scalar(LOSS, str),
                "data": scalar(LOSS, str),
            },
            category=LOSS,
        )
    return subtree(UNKNOWN)


def grammar(value: dict) -> Rule:
    fields = {
        "type": DISCRIMINATOR,
        "uuid": ID,
        "parentUuid": scalar(STRUCTURE, str, type(None)),
        "logicalParentUuid": ID,
        "leafUuid": ID,
        "sessionId": ID,
        "cwd": scalar(STRUCTURE, str),
        "version": scalar(STRUCTURE, str),
        "timestamp": scalar(COORDINATE, str),
        "isSidechain": BOOL,
        "isMeta": BOOL,
        "isCompactSummary": BOOL,
        "isVisibleInTranscriptOnly": BOOL,
        "userType": scalar(ACCOUNTING, str),
        "gitBranch": scalar(COORDINATE, str),
        "requestId": ID,
        "subtype": DISCRIMINATOR,
        "content": scalar(LOSS, str),
        "customTitle": scalar(ACCOUNTING, str),
        "aiTitle": scalar(ACCOUNTING, str),
        "compactMetadata": obj(
            {
                "trigger": scalar(LOSS, str),
                "preTokens": scalar(LOSS, int),
                "preservedSegment": obj(
                    {"anchorUuid": ID, "headUuid": ID, "tailUuid": ID},
                    required=("anchorUuid", "headUuid", "tailUuid"),
                ),
                "preservedMessages": obj({"allUuids": array(ID), "uuids": array(ID)}),
            }
        ),
        "message": obj(
            {
                "role": DISCRIMINATOR,
                "content": dynamic(content),
                "id": ID,
                "type": DISCRIMINATOR,
                "model": scalar(ACCOUNTING, str),
                "stop_reason": scalar(STRUCTURE, str, type(None)),
                "stop_sequence": scalar(STRUCTURE, str, type(None)),
                "usage": obj(
                    {
                        key: NUMBER
                        for key in (
                            "input_tokens",
                            "output_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    },
                    category=ACCOUNTING,
                ),
            },
            required=("role", "content"),
        ),
    }
    if value.get("type") not in {
        "user",
        "assistant",
        "last-prompt",
        "custom-title",
        "ai-title",
        "system",
    }:
        return subtree(UNKNOWN)
    required = ("type",)
    if value.get("type") in {"user", "assistant"}:
        required += ("uuid", "parentUuid", "sessionId", "cwd", "version", "message")
    return obj(fields, required=required)


def audit(records, snapshot, policy) -> AdapterResult:
    taxonomy = Taxonomy()
    client_ids = {r.value.get("version") for r in records if type(r.value.get("version")) is str}
    client = next(iter(client_ids)) if len(client_ids) == 1 else None
    if client != "2.1.209" or (
        policy.client_version is not None and policy.client_version != client
    ):
        taxonomy.issue("unsupported_client_version", 0)
    by_uuid = {}
    for record in records:
        value = record.value
        uid = value.get("uuid")
        if uid is not None:
            if type(uid) is not str or not uid or uid in by_uuid:
                taxonomy.issue("invalid_graph", record.index)
            else:
                by_uuid[uid] = record
    # Check all physical nodes, including inactive branches, independently of selection.
    finished = set()
    for uid, record in by_uuid.items():
        parent = record.value.get("parentUuid")
        logical = record.value.get("logicalParentUuid")
        if any(
            p is not None and (type(p) is not str or p not in by_uuid) for p in (parent, logical)
        ):
            taxonomy.issue("invalid_graph", record.index)
        seen = set()
        cursor = uid
        while type(cursor) is str and cursor in by_uuid and cursor not in finished:
            if cursor in seen:
                declared_loop = any(
                    node.value.get("subtype") == "compact_boundary"
                    and type(node.value.get("logicalParentUuid")) is str
                    and parser._valid_preserved_compaction_back_edge(
                        node, node.value["logicalParentUuid"], seen, by_uuid
                    )
                    for node in (by_uuid[item] for item in seen)
                )
                if not declared_loop:
                    taxonomy.issue("invalid_graph", record.index)
                break
            seen.add(cursor)
            current = by_uuid[cursor].value
            cursor = current.get("parentUuid") or (
                current.get("logicalParentUuid")
                if current.get("type") == "system" and current.get("subtype") == "compact_boundary"
                else None
            )
            if type(cursor) is not str:
                break
        finished.update(seen)
    try:
        active = parser._active_conversation_records(list(records))
        selected = tuple(record.index for record in active)
    except (SessionMigrateError, TypeError):
        taxonomy.issue("invalid_graph", 0)
        selected = ()
    selected_set = set(selected)
    selected_ordinals = {index: ordinal for ordinal, index in enumerate(selected)}
    selected_session_ids = {
        r.value.get("sessionId")
        for r in records
        if r.index in selected_set and type(r.value.get("sessionId")) is str
    }
    if len(selected_session_ids) != 1:
        taxonomy.issue("ambiguous_source_identity", 0)
    for record in records:
        value = record.value
        taxonomy.walk(value, grammar(value), record.index)
        if value.get("type") in {"user", "assistant"} and record.index not in selected_set:
            taxonomy.excluded_records.add(record.index)
            taxonomy.issue("selected_scope_exclusion", record.index)
        if value.get("isCompactSummary") is True or value.get("subtype") == "compact_boundary":
            taxonomy.issue("compaction_unproven", record.index)
            taxonomy.loss_records.add(record.index)
        metadata = value.get("compactMetadata")
        if type(metadata) is dict and "preservedSegment" in metadata:
            segment = metadata.get("preservedSegment")
            messages = metadata.get("preservedMessages")
            declared = (
                messages.get("allUuids", messages.get("uuids")) if type(messages) is dict else None
            )
            valid = (
                type(segment) is dict
                and type(declared) is list
                and all(type(uid) is str for uid in declared)
            )
            if valid:
                valid = len(declared) == len(set(declared)) and all(
                    type(segment.get(key)) is str for key in ("anchorUuid", "headUuid", "tailUuid")
                )
            if valid:
                anchor, head, tail = (
                    segment[key] for key in ("anchorUuid", "headUuid", "tailUuid")
                )
                seen_path = set()
                cursor = tail
                while (
                    type(cursor) is str
                    and cursor in by_uuid
                    and cursor != anchor
                    and cursor not in seen_path
                ):
                    seen_path.add(cursor)
                    cursor = by_uuid[cursor].value.get("parentUuid")
                valid = cursor == anchor and head in seen_path and set(declared) == seen_path
            if not valid:
                taxonomy.issue("invalid_graph", record.index)
        message_value = value.get("message")
        if (
            type(message_value) is dict
            and "type" in message_value
            and message_value["type"] != "message"
        ):
            taxonomy.issue("unsupported_discriminator", record.index)
        role = message_value.get("role") if type(message_value) is dict else None
        if type(role) not in {str, type(None)} or role not in {None, "user", "assistant"}:
            taxonomy.issue("unsupported_role", record.index)
        if value.get("type") in {"user", "assistant"} and role != value["type"]:
            taxonomy.issue("role_mismatch", record.index)
        parent = value.get("parentUuid")
        taxonomy.structure.add(
            {
                "physical": record.index,
                "parent": by_uuid[parent].index
                if type(parent) is str and parent in by_uuid
                else None,
                "selected_ordinal": selected_ordinals.get(record.index),
                "kind": value.get("type"),
                "compacted": value.get("isCompactSummary") is True,
                "excluded_sha256": digest(value)
                if record.index in taxonomy.excluded_records
                else None,
            }
        )
    taxonomy.reclassify_records(taxonomy.excluded_records, EXCLUDED)
    try:
        session = parser.parse_records(
            records, source_path=snapshot.source_path, source_sha256=snapshot.authority_sha256
        )
    except (SessionMigrateError, TypeError, ValueError, RecursionError):
        taxonomy.issue("parser_rejected_source", 0)
        session = None
    return AdapterResult(
        taxonomy, selected, "claude-jsonl-v1", client if client == "2.1.209" else None, session
    )
