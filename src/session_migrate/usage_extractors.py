"""Bounded RAW counter decoding; candidates confer no native authority."""

from __future__ import annotations

from dataclasses import dataclass

from session_migrate.audit_adapters import strict_decode
from session_migrate.formats import claude, codex
from session_migrate.model import AgentFormat

DIMENSIONS = ("input", "output", "cached_input", "cache_read", "cache_write", "reasoning", "total")


@dataclass(frozen=True, slots=True, repr=False)
class RawUsageCandidate:
    record_index: int
    scope: str
    scope_id: str | None
    request_id: str | None
    message_id: str | None
    turn_id: str | None
    model: str | None
    provider: str | None
    counters: tuple[tuple[str, object], ...]
    identity_parts: tuple[str, ...]


def _label(value, maximum=128):
    if type(value) is not str or not value or any(ord(c) < 32 for c in value):
        return None
    try:
        return value if len(value.encode("utf-8")) <= maximum else None
    except UnicodeError:
        return None


def _candidate(
    index,
    scope,
    counts,
    *,
    ids=(),
    request=None,
    message=None,
    turn=None,
    model=None,
    provider=None,
):
    return RawUsageCandidate(
        index,
        scope,
        _label(message or turn or request),
        _label(request),
        _label(message),
        _label(turn),
        _label(model, 256),
        _label(provider, 256),
        tuple((name, counts.get(name)) for name in DIMENSIONS),
        tuple(value for value in ids if _label(value) is not None)
        if ids and all(_label(value) is not None for value in ids)
        else (),
    )


def decode_usage(raw: bytes, source_format: AgentFormat, client: str, maximum: int):
    """Decode explicit fields, without totals, fallback counters or epoch guesses."""
    candidates = []
    supported = {
        AgentFormat.CLAUDE: claude.VALIDATED_CLAUDE_VERSIONS,
        AgentFormat.CODEX: codex.VALIDATED_LEGACY_VERSIONS | codex.VALIDATED_PAGINATED_VERSIONS,
        AgentFormat.OPENCODE: {"2.0.22", "2.0.23"},
    }
    if client not in supported.get(source_format, ()):
        return ()
    if source_format is AgentFormat.OPENCODE:
        value = strict_decode(raw)
        if type(value) is not dict or type(value.get("messages")) is not list:
            raise ValueError
        rows = value["messages"]
        info = value.get("info", {})
        if type(info) is not dict:
            raise ValueError
        session_id = _label(info.get("id"))
        for index, row in enumerate(rows):
            if type(row) is not dict:
                raise ValueError
            if row.get("type") == "assistant":
                tokens = row.get("tokens")
                tokens = tokens if type(tokens) is dict else {}
                cache = tokens.get("cache")
                cache = cache if type(cache) is dict else {}
                model = row.get("model")
                model = model if type(model) is dict else {}
                candidates.append(
                    _candidate(
                        index,
                        "message",
                        {
                            "input": tokens.get("input"),
                            "output": tokens.get("output"),
                            "reasoning": tokens.get("reasoning"),
                            "cache_read": cache.get("read"),
                            "cache_write": cache.get("write"),
                        },
                        ids=(session_id, row.get("id")),
                        message=row.get("id"),
                        model=model.get("id"),
                        provider=model.get("providerID"),
                    )
                )
        tokens = info.get("tokens")
        tokens = tokens if type(tokens) is dict else {}
        cache = tokens.get("cache")
        cache = cache if type(cache) is dict else {}
        model = info.get("model")
        model = model if type(model) is dict else {}
        candidates.append(
            _candidate(
                len(rows),
                "session_cumulative",
                {
                    "input": tokens.get("input"),
                    "output": tokens.get("output"),
                    "reasoning": tokens.get("reasoning"),
                    "cache_read": cache.get("read"),
                    "cache_write": cache.get("write"),
                },
                model=model.get("id"),
                provider=model.get("providerID"),
            )
        )
    else:
        provider = None
        for index, line in enumerate(line for line in raw.splitlines() if line.strip()):
            if index >= 100_000 or len(line) > 64 * 1024 * 1024:
                raise ValueError
            row = strict_decode(line)
            if type(row) is not dict:
                raise ValueError
            payload = row.get("payload")
            payload = payload if type(payload) is dict else {}
            if source_format is AgentFormat.CLAUDE and row.get("type") == "assistant":
                message = row.get("message")
                message = message if type(message) is dict else {}
                counts = message.get("usage")
                counts = counts if type(counts) is dict else {}
                candidates.append(
                    _candidate(
                        index,
                        "message",
                        {
                            "input": counts.get("input_tokens"),
                            "output": counts.get("output_tokens"),
                            "cache_read": counts.get("cache_read_input_tokens"),
                            "cache_write": counts.get("cache_creation_input_tokens"),
                        },
                        ids=(row.get("requestId"), message.get("id")),
                        request=row.get("requestId"),
                        message=message.get("id"),
                        model=message.get("model"),
                    )
                )
            elif source_format is AgentFormat.CODEX:
                if row.get("type") == "session_meta":
                    provider = _label(payload.get("model_provider"), 256)
                if (
                    client == "0.144.4"
                    and row.get("type") == "event_msg"
                    and payload.get("type") == "token_count"
                ):
                    info = payload.get("info")
                    info = info if type(info) is dict else {}
                    for field, scope in (
                        ("total_token_usage", "session_cumulative"),
                        ("last_token_usage", "request"),
                    ):
                        counts = info.get(field)
                        if type(counts) is dict:
                            candidates.append(
                                _candidate(
                                    index,
                                    scope,
                                    {
                                        "input": counts.get("input_tokens"),
                                        "output": counts.get("output_tokens"),
                                        "cached_input": counts.get("cached_input_tokens"),
                                        "reasoning": counts.get("reasoning_output_tokens"),
                                        "total": counts.get("total_tokens"),
                                    },
                                    provider=provider,
                                )
                            )
                elif (
                    client in codex.VALIDATED_PAGINATED_VERSIONS
                    and row.get("type") == "token_usage_record"
                ):
                    # usage is an alias with unproven scope: never add it to turn/thread.
                    for field, scope in (
                        ("turn_token_usage", "turn"),
                        ("thread_token_usage", "thread_cumulative"),
                        ("usage", "request"),
                    ):
                        counts = payload.get(field)
                        if type(counts) is dict:
                            candidates.append(
                                _candidate(
                                    index,
                                    scope,
                                    {
                                        "input": counts.get("input_tokens"),
                                        "output": counts.get("output_tokens"),
                                        "cached_input": counts.get("cached_input_tokens"),
                                        "cache_write": counts.get("cache_write_input_tokens"),
                                        "reasoning": counts.get("reasoning_output_tokens"),
                                        "total": counts.get("total_tokens"),
                                    },
                                    ids=(payload.get("response_id"), payload.get("turn_id"))
                                    if scope == "turn"
                                    else (),
                                    turn=payload.get("turn_id"),
                                    request=payload.get("response_id"),
                                    provider=provider,
                                )
                            )
            if len(candidates) > maximum:
                raise ValueError
    if len(candidates) > maximum:
        raise ValueError
    return tuple(candidates)
