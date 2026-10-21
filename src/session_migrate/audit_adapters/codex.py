"""Closed Codex legacy/paginated grammar; provider bodies are never accounting."""

import json
from collections import Counter

from session_migrate.audit_adapters import (
    ACCOUNTING,
    COORDINATE,
    DISCRIMINATOR,
    EXCLUDED,
    ID,
    LOSS,
    SEMANTIC,
    STRUCTURE,
    UNKNOWN,
    AdapterResult,
    Rule,
    S,
    Taxonomy,
    array,
    dynamic,
    obj,
    scalar,
    strict_decode,
    subtree,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import codex as parser


def block(value: object) -> Rule:
    if type(value) is not dict:
        return subtree(UNKNOWN)
    kind = value.get("type")
    if kind in {"input_text", "output_text", "text", "Text"}:
        return obj(
            {
                "type": DISCRIMINATOR,
                "text": scalar(LOSS, str) if value.get("text") == "" else S,
                "text_elements": array(subtree(UNKNOWN)),
                "annotations": subtree(LOSS),
                "logprobs": subtree(LOSS),
            },
            required=("type", "text"),
        )
    if kind in {"input_image", "image"}:
        image_rule, url_rule = S, S
        if "image_url" in value and "url" in value:
            if type(value["image_url"]) is str and value["image_url"]:
                url_rule = scalar(LOSS, str)
            else:
                image_rule = scalar(LOSS, str)
        return obj(
            {
                "type": DISCRIMINATOR,
                "image_url": image_rule,
                "url": url_rule,
                "file_id": scalar(LOSS, str),
                "detail": scalar(LOSS, str),
            },
            required=("type",),
        )
    return subtree(UNKNOWN)


def result_block(value: object) -> Rule:
    # The mature result reader accepts input_text/text, not output_text/Text.
    if type(value) is dict and value.get("type") in {"output_text", "Text"}:
        return obj(
            {"type": DISCRIMINATOR, "text": scalar(LOSS, str)},
            category=LOSS,
            required=("type", "text"),
        )
    return block(value)


def _decoded_custom_input(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        json.loads(value)
        return True
    except (ValueError, RecursionError):
        return False


def response(value: object) -> Rule:
    if type(value) is not dict:
        return subtree(UNKNOWN)
    kind = value.get("type")
    common = {
        "type": DISCRIMINATOR,
        "id": ID,
        "encrypted_content": scalar(LOSS, str),
    }
    if kind == "message":
        return obj(
            {
                **common,
                "role": DISCRIMINATOR,
                "content": array(dynamic(block)),
                "phase": scalar(LOSS, str),
                "status": scalar(LOSS, str),
                "internal_chat_message_metadata_passthrough": subtree(UNKNOWN),
            },
            required=("type", "role", "content"),
        )
    if kind in {"function_call", "custom_tool_call"}:
        arguments_rule = subtree(SEMANTIC)
        input_rule = subtree(LOSS if "arguments" in value else SEMANTIC)
        if kind == "custom_tool_call":
            if _decoded_custom_input(value.get("arguments")):
                arguments_rule = subtree(LOSS)
            if _decoded_custom_input(value.get("input")):
                input_rule = subtree(LOSS)
        return obj(
            {
                **common,
                "call_id": ID,
                "name": S,
                "namespace": scalar(SEMANTIC, str),
                "arguments": arguments_rule,
                "input": input_rule,
                "status": scalar(LOSS, str),
            },
            required=("type", "name", "arguments" if "arguments" in value else "input"),
        )
    if kind in {"function_call_output", "custom_tool_call_output"}:
        output = value.get("output")
        output_rule = S if type(output) is str else array(dynamic(result_block))
        return obj(
            {**common, "call_id": ID, "output": output_rule, "is_error": scalar(SEMANTIC, bool)},
            required=("type", "call_id", "output"),
        )
    if kind == "reasoning":
        return obj(
            {
                **common,
                "summary": array(
                    obj({"type": DISCRIMINATOR, "text": scalar(LOSS, str)}, category=LOSS)
                ),
                "encrypted_content": scalar(LOSS, str),
                "content": subtree(LOSS),
            },
            category=LOSS,
        )
    return subtree(UNKNOWN)


def completed(value: object) -> Rule:
    if type(value) is not dict or value.get("type") not in {"UserMessage", "AgentMessage"}:
        return subtree(UNKNOWN)
    return obj(
        {
            "type": DISCRIMINATOR,
            "id": ID,
            "client_id": ID,
            "content": array(dynamic(block)),
            "phase": scalar(LOSS, str),
        },
        required=("type", "id", "content"),
    )


def grammar(value: dict) -> Rule:
    kind = value.get("type")
    payload = value.get("payload")
    if kind == "session_meta":
        rule = obj(
            {
                "id": ID,
                "session_id": ID,
                "cwd": scalar(STRUCTURE, str),
                "timestamp": scalar(COORDINATE, str),
                "originator": scalar(ACCOUNTING, str),
                "cli_version": scalar(STRUCTURE, str),
                "source": scalar(STRUCTURE, str),
                "thread_source": scalar(STRUCTURE, str),
                "runtime_workspace_roots": array(scalar(STRUCTURE, str)),
                "context_window": obj({"window_id": ID}, required=("window_id",)),
                "base_instructions": subtree(LOSS),
                "model_provider": scalar(ACCOUNTING, str),
                "history_mode": DISCRIMINATOR,
                "history_base": subtree(UNKNOWN),
                "subagent_history_start_ordinal": subtree(UNKNOWN),
            },
            required=("id", "cwd", "cli_version"),
        )
    elif kind == "response_item":
        rule = response(payload)
    elif kind == "event_msg" and type(payload) is dict:
        event_type = payload.get("type")
        if event_type in {"user_message", "agent_message"}:
            rule = obj({"type": DISCRIMINATOR, "message": S}, required=("type", "message"))
        elif event_type == "item_completed":
            rule = obj(
                {
                    "type": DISCRIMINATOR,
                    "item": dynamic(completed),
                    "thread_id": ID,
                    "turn_id": ID,
                    "started_at_ms": scalar(COORDINATE, int),
                    "completed_at_ms": scalar(COORDINATE, int),
                },
                required=("type", "item"),
            )
        elif event_type == "thread_name_updated":
            rule = obj(
                {
                    "type": DISCRIMINATOR,
                    "thread_name": scalar(ACCOUNTING, str),
                    "name": scalar(ACCOUNTING, str),
                },
                required=("type",),
            )
        elif event_type == "task_started":
            rule = obj(
                {"type": DISCRIMINATOR, "model": scalar(ACCOUNTING, str)}, required=("type",)
            )
        else:
            rule = subtree(UNKNOWN)
    elif kind == "compacted":
        rule = obj(
            {"message": scalar(LOSS, str), "replacement_history": subtree(UNKNOWN)}, category=LOSS
        )
    else:
        return subtree(UNKNOWN)
    return obj(
        {
            "type": DISCRIMINATOR,
            "timestamp": scalar(COORDINATE, str),
            "ordinal": scalar(STRUCTURE, int),
            "payload": rule,
            "metadata": subtree(UNKNOWN),
        },
        required=("type", "payload"),
    )


def _semantic_loss_issues(value: dict, taxonomy: Taxonomy, index: int) -> None:
    """Correlate supported semantic slots with reader emissions, by raw span."""
    payload = value.get("payload")
    if type(payload) is not dict:
        return
    if "phase" in payload:
        taxonomy.issue("phase_context_loss", index)
    item = payload.get("item")
    if type(item) is dict and "phase" in item:
        taxonomy.issue("phase_context_loss", index)
    if payload.get("type") in {"function_call", "custom_tool_call"}:
        if "arguments" in payload and "input" in payload:
            taxonomy.issue("semantic_alias_shadow", index)
        selected_input = payload.get("arguments", payload.get("input"))
        if payload.get("type") == "custom_tool_call" and _decoded_custom_input(selected_input):
            taxonomy.issue("custom_input_json_shape_loss", index)
    groups = [payload.get("content"), payload.get("output")]
    if type(item) is dict:
        groups.append(item.get("content"))
    for group in groups:
        for candidate in group if type(group) is list else []:
            if type(candidate) is not dict:
                continue
            if candidate.get("type") in {"input_image", "image"}:
                if "file_id" in candidate:
                    taxonomy.issue("image_file_id_loss", index)
                if "detail" in candidate:
                    taxonomy.issue("image_detail_loss", index)
                if "image_url" in candidate and "url" in candidate:
                    taxonomy.issue("semantic_alias_shadow", index)
            if (
                candidate.get("type") in {"text", "Text", "input_text", "output_text"}
                and candidate.get("text") == ""
            ):
                taxonomy.issue("empty_text_carrier_loss", index)


def audit(records, snapshot, policy) -> AdapterResult:
    taxonomy = Taxonomy()
    metas = [r for r in records if r.value.get("type") == "session_meta"]
    meta = metas[0].value.get("payload") if metas else None
    meta = meta if type(meta) is dict else {}
    mode = meta.get("history_mode", "legacy")
    client = meta.get("cli_version")
    if len(metas) != 1 or mode not in {"legacy", "paginated"}:
        taxonomy.issue("ambiguous_source_identity", 0)
    supported = type(client) is str and (
        (mode == "legacy" and client in parser.VALIDATED_LEGACY_VERSIONS)
        or (mode == "paginated" and client in parser.VALIDATED_PAGINATED_VERSIONS)
    )
    if not supported or (policy.client_version is not None and policy.client_version != client):
        taxonomy.issue("unsupported_client_version", 0)
    if meta.get("session_id") not in (None, meta.get("id")):
        taxonomy.issue("ambiguous_source_identity", 0)
    response_messages = Counter()
    event_messages = Counter()
    for record in records:
        value = record.value
        payload = value.get("payload")
        payload = payload if type(payload) is dict else {}
        taxonomy.walk(value, grammar(value), record.index)
        _semantic_loss_issues(value, taxonomy, record.index)
        if "ordinal" in value and (
            type(value["ordinal"]) is not int or value["ordinal"] != record.index
        ):
            taxonomy.issue("invalid_ordinal", record.index)
        if mode == "paginated" and "ordinal" not in value:
            taxonomy.issue("invalid_ordinal", record.index)
        if value.get("type") == "response_item":
            item_type = payload.get("type")
            if item_type in {"function_call", "custom_tool_call"}:
                arguments = payload.get("arguments", payload.get("input"))
                if type(arguments) is str:
                    if item_type == "function_call":
                        strict_decode(arguments)
                    else:
                        try:
                            json.loads(arguments)
                        except json.JSONDecodeError:
                            pass  # Custom tool plaintext stays an exact semantic string.
                        else:
                            strict_decode(arguments)
            if item_type == "message":
                if payload.get("role") not in {"user", "assistant"}:
                    taxonomy.issue("unsupported_role", record.index)
                if mode == "paginated":
                    taxonomy.loss_records.add(record.index)
                    taxonomy.issue("provider_body_known_loss", record.index)
                else:
                    content = payload.get("content")
                    if type(content) is list:
                        content_kinds = parser._message_content_kinds(payload)
                        if content and content_kinds == ():
                            taxonomy.issue("invalid_content_item_kinds", record.index)
                            content = []
                        for block_index, block_value in enumerate(content):
                            if (
                                content_kinds is not None
                                and content_kinds[block_index]
                                in parser.PROVIDER_CONTEXT_CONTENT_KINDS
                            ):
                                taxonomy.loss_records.add(record.index)
                                taxonomy.issue("provider_body_known_loss", record.index)
                                continue
                            if (
                                type(block_value) is dict
                                and block_value.get("type") in {"input_text", "output_text", "text"}
                                and type(block_value.get("text")) is str
                            ):
                                response_messages[(payload.get("role"), block_value["text"])] += 1
        elif value.get("type") == "event_msg" and payload.get("type") in {
            "user_message",
            "agent_message",
        }:
            role = "user" if payload["type"] == "user_message" else "assistant"
            text = payload.get("message")
            if type(text) is str:
                event_messages[(role, text)] += 1
        if value.get("type") == "compacted":
            taxonomy.loss_records.add(record.index)
            taxonomy.issue("compaction_unproven", record.index)
        taxonomy.structure.add(
            [record.index, value.get("type"), value.get("ordinal"), payload.get("type")]
        )
    if mode == "legacy" and response_messages and event_messages:
        if event_messages != response_messages:
            taxonomy.issue("ambiguous_event_pairing", 0)
        else:
            for record in records:
                payload = record.value.get("payload")
                if (
                    record.value.get("type") == "event_msg"
                    and type(payload) is dict
                    and payload.get("type") in {"user_message", "agent_message"}
                ):
                    taxonomy.excluded_records.add(record.index)
    taxonomy.reclassify_records(taxonomy.loss_records, LOSS)
    taxonomy.reclassify_records(taxonomy.excluded_records, EXCLUDED)
    selected = tuple(r.index for r in records if r.index not in taxonomy.excluded_records)
    try:
        session = parser.parse_records(
            records, source_path=snapshot.source_path, source_sha256=snapshot.authority_sha256
        )
    except (SessionMigrateError, TypeError, ValueError, RecursionError):
        taxonomy.issue("parser_rejected_source", 0)
        session = None
    return AdapterResult(
        taxonomy,
        selected,
        "codex-" + (mode if type(mode) is str and mode in {"legacy", "paginated"} else "unknown"),
        client if supported else None,
        session,
    )
