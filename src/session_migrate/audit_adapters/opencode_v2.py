"""Closed OP2 transfer grammar, before its legacy projection can erase fields."""

from session_migrate.audit_adapters import (
    ACCOUNTING,
    COORDINATE,
    DISCRIMINATOR,
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
    dynamic,
    obj,
    scalar,
    subtree,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode as parser

_CLIENTS = frozenset({"2.0.22", "2.0.23", "2.1.1"})

MODEL = obj(
    {
        "id": scalar(ACCOUNTING, str),
        "providerID": scalar(ACCOUNTING, str),
        "variant": scalar(LOSS, str),
    },
    required=("id", "providerID"),
)
CLOCK = obj(
    {
        "created": scalar(COORDINATE, int),
        "updated": scalar(COORDINATE, int),
        "completed": scalar(COORDINATE, int),
        "streamed": scalar(COORDINATE, int),
    },
    required=("created",),
)
TOKENS = obj(
    {
        "input": NUMBER,
        "output": NUMBER,
        "reasoning": NUMBER,
        "cache": obj(
            {"read": NUMBER, "write": NUMBER}, category=ACCOUNTING, required=("read", "write")
        ),
    },
    category=ACCOUNTING,
    required=("input", "output", "reasoning", "cache"),
)


def attachment(value: object) -> Rule:
    source = value.get("source") if type(value) is dict else None
    source_rule = obj({"type": DISCRIMINATOR}, category=SEMANTIC, required=("type",))
    if type(source) is dict and source.get("type") != "inline":
        source_rule = subtree(UNKNOWN)
    return obj(
        {
            "data": S,
            "mime": S,
            "source": source_rule,
            "name": scalar(LOSS, str),
            "description": scalar(LOSS, str),
            "mention": subtree(UNKNOWN),
        },
        category=SEMANTIC,
        required=("data", "mime", "source"),
    )


def result_block(value: object) -> Rule:
    if type(value) is not dict:
        return subtree(UNKNOWN)
    if value.get("type") == "text":
        return obj({"type": DISCRIMINATOR, "text": S}, category=SEMANTIC, required=("type", "text"))
    if value.get("type") == "file":
        return obj(
            {"type": DISCRIMINATOR, "uri": S, "mime": S, "name": scalar(LOSS, str)},
            category=SEMANTIC,
            required=("type", "uri", "mime"),
        )
    return subtree(UNKNOWN)


def content(value: object) -> Rule:
    if type(value) is not dict:
        return subtree(UNKNOWN)
    kind = value.get("type")
    if kind == "text":
        return obj(
            {
                "type": DISCRIMINATOR,
                "text": scalar(LOSS, str) if value.get("text") == "" else S,
                "state": subtree(UNKNOWN),
                "time": CLOCK,
            },
            required=("type", "text"),
        )
    if kind == "reasoning":
        return obj(
            {
                "type": DISCRIMINATOR,
                "text": scalar(LOSS, str),
                "state": subtree(UNKNOWN),
                "time": CLOCK,
            },
            category=LOSS,
            required=("type", "text"),
        )
    if kind != "tool":
        return subtree(UNKNOWN)
    state = value.get("state")
    state = state if type(state) is dict else {}
    status = state.get("status")
    fields = {"status": DISCRIMINATOR, "input": subtree(SEMANTIC)}
    if status == "completed":
        fields["content"] = array(dynamic(result_block), category=SEMANTIC)
    elif status == "error":
        error = state.get("error")
        error_type = error.get("type") if type(error) is dict else None
        fields["error"] = obj(
            {
                "type": scalar(STRUCTURE if error_type == "ToolError" else LOSS, str),
                "message": S,
                "status": scalar(LOSS, int),
                "response": subtree(UNKNOWN),
            },
            category=SEMANTIC,
            required=("type", "message"),
        )
        fields["content"] = array(dynamic(result_block), category=LOSS)
    else:
        return subtree(UNKNOWN)
    return obj(
        {
            "type": DISCRIMINATOR,
            "id": ID,
            "name": S,
            "state": obj(
                fields,
                category=SEMANTIC,
                required=("status", "input", "content" if status == "completed" else "error"),
            ),
            "time": CLOCK,
            "executed": scalar(STRUCTURE, bool),
            "providerState": subtree(UNKNOWN),
            "providerResultState": subtree(UNKNOWN),
        },
        required=("id", "name", "state"),
    )


def message(value: object, *, schema_21: bool = False) -> Rule:
    if type(value) is not dict:
        return subtree(UNKNOWN)
    kind = value.get("type")
    common = {"type": DISCRIMINATOR, "id": ID, "time": CLOCK, "metadata": subtree(UNKNOWN)}
    if schema_21:
        # Provider-owned replay data is observed as unknown RAW, never portable
        # message content or evidence of a resumable native checkpoint.
        common["native"] = subtree(UNKNOWN)
        if kind == "compaction":
            common["metadata"] = obj(
                {"keepID": scalar(STRUCTURE, str), "recent": scalar(LOSS, str)}
            )
    if kind == "user":
        # OP2 requires this empty carrier for an inline-file-only user message.
        # It contributes no text atom; the user role and file atoms remain observed.
        text_rule = S
        if value.get("text") == "":
            text_rule = scalar(
                COORDINATE if type(value.get("files")) is list and value["files"] else LOSS, str
            )
        return obj(
            {
                **common,
                "text": text_rule,
                "files": array(dynamic(attachment)),
                "agents": subtree(UNKNOWN),
                "skills": subtree(UNKNOWN),
            },
            required=("type", "id", "time", "text"),
        )
    if kind == "assistant":
        return obj(
            {
                **common,
                "agent": scalar(ACCOUNTING, str),
                "model": MODEL,
                "content": array(dynamic(content)),
                "finish": scalar(STRUCTURE, str),
                "rawFinish": scalar(LOSS, str),
                "cost": NUMBER,
                "tokens": TOKENS,
                "providerState": subtree(UNKNOWN),
                "providerContext": subtree(UNKNOWN),
                "snapshot": subtree(UNKNOWN),
                "retry": subtree(UNKNOWN),
                "error": subtree(UNKNOWN),
            },
            required=("type", "id", "time", "agent", "model", "content"),
        )
    if kind == "compaction":
        return obj(
            {
                **common,
                "status": scalar(LOSS, str),
                "reason": scalar(LOSS, str),
                "model": MODEL,
                "summary": scalar(LOSS, str),
                "recent": scalar(LOSS, str),
            },
            category=LOSS,
        )
    return subtree(UNKNOWN)


def audit(value, snapshot, policy) -> AdapterResult:
    taxonomy = Taxonomy()
    client = policy.client_version
    if client not in _CLIENTS:
        taxonomy.issue("unsupported_client_version", 0)
    schema_21 = client == "2.1.1"
    info = value.get("info") if type(value) is dict else None
    info = info if type(info) is dict else {}
    grammar = obj(
        {
            "info": obj(
                {
                    "id": ID,
                    "projectID": ID,
                    "title": scalar(ACCOUNTING, str),
                    "agent": scalar(ACCOUNTING, str),
                    "model": MODEL,
                    "location": obj(
                        {"directory": scalar(STRUCTURE, str), "workspaceID": ID},
                        required=("directory",),
                    ),
                    "cost": NUMBER,
                    "tokens": TOKENS,
                    "time": CLOCK,
                    "metadata": subtree(UNKNOWN),
                    "permissions": subtree(UNKNOWN),
                    "revert": subtree(UNKNOWN),
                    "fork": subtree(UNKNOWN),
                },
                required=("id", "projectID", "title", "location", "time", "cost", "tokens"),
            ),
            "messages": array(dynamic(lambda item: message(item, schema_21=schema_21))),
        },
        required=("info", "messages"),
    )
    taxonomy.walk(value, grammar, 0)
    messages = value.get("messages") if type(value) is dict else None
    messages = messages if type(messages) is list else []
    seen = set()
    for index, item in enumerate(messages):
        if type(item) is not dict:
            continue
        mid = item.get("id")
        if type(mid) is not str or mid in seen:
            taxonomy.issue("invalid_graph", index)
        if item.get("type") == "compaction":
            taxonomy.issue("compaction_unproven", index)
            metadata = item.get("metadata")
            if schema_21 and type(metadata) is dict and "keepID" in metadata:
                keep_id = metadata["keepID"]
                # Even a valid backward reference does not establish closure.
                # A missing/forward/self reference cannot retire earlier history.
                if type(keep_id) is not str or keep_id not in seen:
                    taxonomy.issue("invalid_graph", index)
        if type(mid) is str:
            seen.add(mid)
        for part in item.get("content", []) if type(item.get("content")) is list else []:
            if type(part) is dict and part.get("type") == "tool":
                state = part.get("state")
                if (
                    type(state) is dict
                    and type(state.get("error")) is dict
                    and state["error"].get("type") == "ImportedIncompleteTool"
                ):
                    taxonomy.issue("incomplete_tool", index)
        taxonomy.structure.add(
            [
                index,
                item.get("type"),
                len(item.get("content", [])) if type(item.get("content")) is list else 0,
            ]
        )
    taxonomy.structure.add(["session", bool(info.get("id")), len(messages)])
    try:
        session = parser.parse_session_bytes(snapshot.raw_bytes, snapshot.source_path)
    except (SessionMigrateError, TypeError, ValueError, RecursionError):
        taxonomy.issue("parser_rejected_source", 0)
        session = None
    return AdapterResult(
        taxonomy,
        tuple(range(len(messages))),
        "opencode-session-transfer-v2",
        client if client in _CLIENTS else None,
        session,
    )
