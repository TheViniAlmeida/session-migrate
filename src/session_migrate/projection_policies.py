"""Independent portable projection expectations, without invoking a writer.

Membership covers every source event by occurrence. Known losses describe
fidelity, independently of the completeness of the raw source audit.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from session_migrate.audit_adapters import strict_decode
from session_migrate.canonical import (
    MAX_DIAGNOSTICS,
    CanonicalPolicy,
    SemanticWitness,
    _json_bytes,
    hash_events,
    iter_canonical_units,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats.context_text import (
    SYNTHETIC_TEXT_BLOCK_TYPE,
    is_synthetic_text_context,
)
from session_migrate.lineage import Coverage
from session_migrate.model import Event, EventKind, Role, TargetFormat
from session_migrate.witnesses import SequenceWitness

if TYPE_CHECKING:
    from session_migrate.native_audit import CanonicalObservation

PORTABLE_POLICY_ID = "portable-projection-v1"
OPENCODE_ARCHIVE_POLICY_ID = "portable-opencode-225-archive-v1"
NATIVE_CONTEXT_POLICY_ID = "portable-native-context-225-v1"
NATIVE_CONTEXT_ARCHIVE_POLICY_ID = "portable-native-context-225-archive-v1"
_NATIVE_CONTEXT_POLICIES = {NATIVE_CONTEXT_POLICY_ID, NATIVE_CONTEXT_ARCHIVE_POLICY_ID}
REVIEWED_POLICY_ID = "portable-claude309-reviewed-225-v1"
REVIEWED_ARCHIVE_POLICY_ID = "portable-claude309-reviewed-225-archive-v1"
_REVIEWED_POLICIES = {REVIEWED_POLICY_ID, REVIEWED_ARCHIVE_POLICY_ID}
_ARCHIVE_POLICIES = {
    OPENCODE_ARCHIVE_POLICY_ID,
    NATIVE_CONTEXT_ARCHIVE_POLICY_ID,
    REVIEWED_ARCHIVE_POLICY_ID,
}
INCOMPLETE_TOOL_MESSAGE = "Imported call had no completed result"
INCOMPLETE_TOOL_RULE = "opencode-225-incomplete-tool-error-v1"


class ProjectionError(SessionMigrateError):
    """Content-safe, fixed projection failure codes."""

    def __init__(self, code: str) -> None:
        allowed = {
            "unsupported_policy",
            "unsupported_target",
            "invalid_projection",
            "source_mismatch",
            "artifact_mismatch",
            "options_mismatch",
            "context_mismatch",
            "invalid_certificate",
            "binding_changed",
            "semantic_mismatch",
            "source_tools_unproven",
        }
        self.code = code if type(code) is str and code in allowed else "invalid_projection"
        super().__init__(f"Projection: {self.code}.")


def require(condition: bool, code: str = "invalid_projection") -> None:
    if not condition:
        raise ProjectionError(code)


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionPolicy:
    policy_id: str = "portable-projection-v1"
    canonical_policy: CanonicalPolicy = CanonicalPolicy()

    def __post_init__(self) -> None:
        require(
            self.policy_id
            in {
                PORTABLE_POLICY_ID,
                OPENCODE_ARCHIVE_POLICY_ID,
                *_NATIVE_CONTEXT_POLICIES,
                *_REVIEWED_POLICIES,
            },
            "unsupported_policy",
        )
        require(type(self.canonical_policy) is CanonicalPolicy, "unsupported_policy")
        self.canonical_policy.__post_init__()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            _json_bytes(
                {
                    "policy": self.policy_id,
                    "canonical": self.canonical_policy.sha256,
                }
            )
        ).hexdigest()


def policy_from_id(policy_id: str) -> ProjectionPolicy:
    """Resolve persisted identifiers only through the supported policy registry."""
    require(type(policy_id) is str, "unsupported_policy")
    return ProjectionPolicy(policy_id)


def validate_policy_target(policy: ProjectionPolicy, target: TargetFormat, client: str) -> None:
    policy.__post_init__()
    if policy.policy_id in {
        OPENCODE_ARCHIVE_POLICY_ID,
        *_NATIVE_CONTEXT_POLICIES,
        *_REVIEWED_POLICIES,
    }:
        require(target is TargetFormat.OPENCODE and client == "2.2.5", "unsupported_policy")


@dataclass(frozen=True, slots=True, repr=False)
class RelationMember:
    source_index: int
    target_index: int | None
    source_sha256: str
    target_sha256: str | None
    transform: str
    target_text_start: int | None
    target_text_length: int | None
    losses: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class GeneratedRelationMember:
    source_index: int
    call_target_index: int
    target_index: int
    source_sha256: str
    target_sha256: str
    transform: str
    rule_id: str


@dataclass(frozen=True, slots=True, repr=False)
class ExpectedProjection:
    policy: ProjectionPolicy
    witness: SemanticWitness
    relation: SequenceWitness
    members: tuple[RelationMember, ...]
    generated_members: tuple[GeneratedRelationMember, ...]
    loss_counts: tuple[tuple[str, int], ...]
    metadata_loss_counts: tuple[tuple[str, int], ...]
    relation_complete: bool
    preserves_content: bool
    _events: tuple[Event, ...]
    native_context_hashes: tuple[str, ...] = ()
    native_context_only: bool = False
    reviewed_source_order: tuple[tuple[str, str], ...] = ()
    reviewed_target_order: tuple[tuple[str, str], ...] = ()

    @property
    def events(self) -> tuple[Event, ...]:
        return copy.deepcopy(self._events)


def native_context_carriers_match(expected: ExpectedProjection, snapshot) -> bool:
    """Bind typed context verbatim and prohibit a new replay cut in active-only output."""
    if not expected.native_context_hashes:
        return True
    from session_migrate.formats.context_text import is_marked_synthetic_text

    try:
        bundle = strict_decode(snapshot.raw_bytes)
        messages = bundle["messages"]
        if expected.native_context_only and any(m.get("type") == "compaction" for m in messages):
            return False
        contexts = [m for m in messages if is_marked_synthetic_text(m)]
        return (
            tuple(hashlib.sha256(_json_bytes(m["text"])).hexdigest() for m in contexts)
            == expected.native_context_hashes
        )
    except (SessionMigrateError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _native_source_order(
    events: tuple[Event, ...], *, reviewed: bool = False
) -> tuple[tuple[str, str], ...]:
    """Read original source occurrences; never use the paired expected events."""
    trace: list[tuple[str, str]] = []
    inert = {
        "active_graph_metadata_record",
        "top_level_tool_result_metadata",
        "inactive_or_metadata_conversation_record",
        "non_conversation_record",
        "duplicate_desktop_message_envelope",
        "duplicate_envelope_cwd_conflict",
    }
    for event in events:
        if event.kind is EventKind.MESSAGE and event.role in {
            Role.USER,
            Role.ASSISTANT,
            Role.SYSTEM,
        }:
            if event.text:
                trace.append(
                    (event.role.value, hashlib.sha256(_json_bytes(event.text)).hexdigest())
                )
        elif is_synthetic_text_context(event):
            if event.role is not Role.USER or type(event.text) is not str:
                raise ValueError("unsupported source text placement")
            trace.append(("user", hashlib.sha256(_json_bytes(event.text)).hexdigest()))
        elif event.kind in {EventKind.TOOL_CALL, EventKind.TOOL_RESULT}:
            if type(event.tool_call_id) is not str or not event.tool_call_id:
                raise ValueError("unsupported source tool identity")
            if event.kind is EventKind.TOOL_RESULT:
                blocks = event.payload.get("content_blocks")
                if type(blocks) is not list or any(
                    type(block) is not dict
                    or not (
                        (block.get("type") == "text" and type(block.get("text")) is str)
                        or (
                            reviewed
                            and block.get("type") == "tool_reference"
                            and set(block) == {"type", "tool_name"}
                            and type(block.get("tool_name")) is str
                            and bool(block["tool_name"])
                        )
                    )
                    for block in blocks
                ):
                    raise ValueError("unsupported source tool result content")
            trace.append(
                ("call" if event.kind is EventKind.TOOL_CALL else "result", event.tool_call_id)
            )
        elif (
            event.kind is EventKind.OPAQUE
            and event.role is None
            and event.payload.get("reason") in inert
        ):
            continue
        else:
            # Images, reasoning, native windows and unknown material need their
            # own lowering contract before they can receive this order proof.
            raise ValueError("unsupported source context placement")
    return tuple(trace)


def _native_target_order(bundle: dict, archived: set[str]) -> tuple[tuple[str, str], ...]:
    """Bounded stock 2.2.5 to-llm-message lowering, before provider encoding.

    Each assistant emits its text/calls in content order, then its ordinary
    tool results in that same content order. Timestamps do not participate.
    """
    trace: list[tuple[str, str]] = []

    def text(role: str, value: object, *, keep_empty: bool = False) -> None:
        if type(value) is not str:
            raise ValueError("unsupported target text")
        if value or keep_empty:
            trace.append((role, hashlib.sha256(_json_bytes(value)).hexdigest()))

    for message in bundle["messages"]:
        kind = message["type"]
        if kind in {"user", "synthetic", "system"}:
            if kind == "user" and (message.get("files") or message.get("skills")):
                raise ValueError("unsupported target attachment placement")
            text(
                "system" if kind == "system" else "user",
                message["text"],
                keep_empty=kind == "synthetic",
            )
        elif kind == "assistant":
            results: list[tuple[str, str]] = []
            for part in message["content"]:
                if part["type"] == "text":
                    if part.get("native") is not None:
                        raise ValueError("unsupported provider text state")
                    text("assistant", part["text"])
                elif part["type"] == "tool":
                    if part.get("executed", False) is not False or part.get("native") is not None:
                        raise ValueError("unsupported provider tool placement")
                    call_id = part["id"]
                    if type(call_id) is not str or not call_id:
                        raise ValueError("unsupported target tool identity")
                    if part["state"]["status"] not in {"completed", "error"}:
                        raise ValueError("unsupported unsettled tool lowering")
                    result_content = part["state"].get("content", [])
                    if type(result_content) is not list or any(
                        type(block) is not dict
                        or block.get("type") != "text"
                        or type(block.get("text")) is not str
                        for block in result_content
                    ):
                        raise ValueError("unsupported target tool result content")
                    trace.append(("call", call_id))
                    if call_id not in archived:
                        results.append(("result", call_id))
                else:
                    raise ValueError("unsupported target assistant placement")
            trace.extend(results)
        elif kind not in {"agent-switched", "idle"}:
            raise ValueError("unsupported target context placement")
    return tuple(trace)


def native_context_order_matches(
    source: CanonicalObservation, expected: ExpectedProjection, snapshot
) -> bool:
    """Compare source order against raw target lowering independently of projection.

    Existing projection rules intentionally pair results with calls. They must
    not serve as the source oracle for native context replay order.
    """
    if not expected.native_context_only:
        return True
    from session_migrate.native_audit import validate_observation

    try:
        validate_observation(source)
        if expected.policy.policy_id not in _NATIVE_CONTEXT_POLICIES:
            return False
        if not archived_tools_match(expected, snapshot):
            return False
        events = source.events
        archived: set[str] = set()
        for member in expected.generated_members:
            call = events[member.source_index]
            if (
                call.kind is not EventKind.TOOL_CALL
                or member.transform != "archive_incomplete_tool"
                or member.rule_id != INCOMPLETE_TOOL_RULE
                or call.tool_call_id != expected._events[member.call_target_index].tool_call_id
            ):
                return False
            archived.add(call.tool_call_id)
        return _native_source_order(events) == _native_target_order(
            strict_decode(snapshot.raw_bytes), archived
        )
    except (SessionMigrateError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return False


def _reviewed_projected_order(
    events: tuple[Event, ...], generated: tuple[GeneratedRelationMember, ...]
) -> tuple[tuple[str, str], ...]:
    """Derive stock target effective ordering from source occurrences, not bytes.

    Tool results complete their historical call; they are emitted after all
    assistant parts in that call's source message. Historical DATA always ends
    the current message. This is a closed description of the approved lowering.
    """
    archived = {events[member.source_index].tool_call_id for member in generated}
    results = {e.tool_call_id for e in events if e.kind is EventKind.TOOL_RESULT}
    trace: list[tuple[str, str]] = []
    pending: list[tuple[str, str]] = []
    pending_results: list[tuple[str, str]] = []
    pending_user: list[str] = []
    coordinate = None

    def flush() -> None:
        nonlocal coordinate
        if pending_user:
            trace.append(("user", hashlib.sha256(_json_bytes("\n".join(pending_user))).hexdigest()))
        trace.extend(pending)
        trace.extend(pending_results)
        pending.clear()
        pending_user.clear()
        pending_results.clear()
        coordinate = None

    for event in events:
        if is_synthetic_text_context(event):
            flush()
            trace.append(("user", hashlib.sha256(_json_bytes(event.text)).hexdigest()))
        elif event.kind is EventKind.MESSAGE and event.text:
            wanted = (event.role, event.provenance.record_index)
            if coordinate is not None and coordinate != wanted:
                flush()
            coordinate = wanted
            if event.role is Role.USER:
                pending_user.append(event.text)
            else:
                require(event.role is Role.ASSISTANT, "source_mismatch")
                pending.append(("assistant", hashlib.sha256(_json_bytes(event.text)).hexdigest()))
        elif event.kind is EventKind.TOOL_CALL:
            wanted = (Role.ASSISTANT, event.provenance.record_index)
            if coordinate is not None and coordinate != wanted:
                flush()
            coordinate = wanted
            pending.append(("call", event.tool_call_id))
            require(
                event.tool_call_id in results or event.tool_call_id in archived,
                "source_tools_unproven",
            )
            if event.tool_call_id not in archived:
                pending_results.append(("result", event.tool_call_id))
        elif event.kind is EventKind.TOOL_RESULT:
            flush()
        # _native_source_order already rejected all other material except
        # explicitly inert metadata. Such records do not create target messages.
    flush()
    return tuple(trace)


def reviewed_context_order_matches(expected: ExpectedProjection, snapshot) -> bool:
    """Verify the declared reviewed lowering, including authority constraints."""
    if expected.policy.policy_id not in _REVIEWED_POLICIES:
        return True
    try:
        bundle = strict_decode(snapshot.raw_bytes)
        # No source authority, native provider payload, compaction or windows may
        # be smuggled through a reviewed conversation projection.
        if any(key in bundle["info"] for key in {"permissions", "revert", "fork", "metadata"}):
            return False
        for message in bundle["messages"]:
            if message.get("type") not in {"user", "synthetic", "assistant"}:
                return False
            if any(
                key in message
                for key in {
                    "native",
                    "providerState",
                    "providerContext",
                    "snapshot",
                    "retry",
                    "error",
                    "window",
                    "producer",
                    "hosted",
                }
            ):
                return False
            if "metadata" in message and message.get("type") != "synthetic":
                return False
            if message.get("type") == "user" and message.get("agents"):
                return False
            for part in message.get("content", []):
                if any(
                    key in part
                    for key in {
                        "native",
                        "providerState",
                        "providerResultState",
                        "hosted",
                        "window",
                        "producer",
                    }
                ):
                    return False
                if part.get("type") == "tool" and set(part["state"]) - {
                    "status",
                    "input",
                    "content",
                    "error",
                }:
                    return False
        return (
            archived_tools_match(expected, snapshot)
            and native_context_carriers_match(expected, snapshot)
            and _native_target_order(
                bundle,
                {
                    expected._events[m.call_target_index].tool_call_id
                    for m in expected.generated_members
                },
            )
            == expected.reviewed_target_order
        )
    except (SessionMigrateError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return False


def archived_tools_match(expected: ExpectedProjection, snapshot) -> bool:
    """Check native archive markers that canonical tool errors intentionally erase.

    The expected calls come only from independently derived generated bindings.
    A same-text ordinary error, or a marker moved to another call, is not proof.
    """
    if expected.policy.policy_id not in {
        OPENCODE_ARCHIVE_POLICY_ID,
        *_NATIVE_CONTEXT_POLICIES,
        *_REVIEWED_POLICIES,
    }:
        return True
    try:
        bundle = strict_decode(snapshot.raw_bytes)
        tools = [
            content
            for message in bundle["messages"]
            if message.get("type") == "assistant"
            for content in message["content"]
            if content.get("type") == "tool"
        ]
        marked = [
            tool
            for tool in tools
            if tool.get("state", {}).get("error", {}).get("type") == "ImportedIncompleteTool"
        ]
        wanted = {
            expected._events[member.call_target_index].tool_call_id: expected._events[
                member.call_target_index
            ]
            for member in expected.generated_members
        }
        if len(marked) != len(wanted) or {tool["id"] for tool in marked} != set(wanted):
            return False
        for tool in marked:
            call = wanted[tool["id"]]
            state = tool["state"]
            if not (
                tool["name"] == call.tool_name
                and state["status"] == "error"
                and state["error"]
                == {
                    "type": "ImportedIncompleteTool",
                    "message": INCOMPLETE_TOOL_MESSAGE,
                }
                and _json_bytes(state["input"]) == _json_bytes(call.payload["input"])
            ):
                return False
        return True
    except (SessionMigrateError, ValueError, TypeError, KeyError, AttributeError):
        return False


def reviewed_tool_reference_text(tool_name: str) -> str:
    """Quote a captured registry reference as DATA, never as a live definition."""
    require(type(tool_name) is str and bool(tool_name), "source_mismatch")
    return "[session-migrate historical tool reference data]\n" + json.dumps(
        {"type": "tool_reference", "tool_name": tool_name},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _content(
    event: Event,
    target: TargetFormat,
    losses: set[str],
    *,
    reviewed: bool = False,
) -> tuple[str | None, list]:
    raw = event.payload.get("content_blocks")
    kept = []
    if type(raw) is list:
        for block in raw:
            if type(block) is not dict:
                losses.add("result_block_omitted")
                continue
            kind = block.get("type")
            if kind == "text" and type(block.get("text")) is str:
                kept.append({"type": "text", "text": block["text"]})
            elif kind == "image" and type(block.get("image_url")) is str:
                uri = block["image_url"]
                if target is TargetFormat.CODEX or uri.startswith("data:image/"):
                    kept.append({"type": "image", "image_url": uri})
                else:
                    losses.add("result_image_omitted")
            elif reviewed and kind == "tool_reference" and set(block) == {"type", "tool_name"}:
                kept.append(
                    {"type": "text", "text": reviewed_tool_reference_text(block["tool_name"])}
                )
                losses.add("reviewed_tool_reference_as_data")
            elif (
                target is TargetFormat.CLAUDE
                and kind == "tool_reference"
                and (type(block.get("tool_name")) is str)
            ):
                kept.append({"type": "tool_reference", "tool_name": block["tool_name"]})
            else:
                losses.add("result_block_omitted")
    if not kept:
        kept = [{"type": "text", "text": event.text or ""}]
    texts = [b["text"] for b in kept if b["type"] == "text"]
    if target is TargetFormat.OPENCODE and not texts and event.text:
        kept.insert(0, {"type": "text", "text": event.text})
        texts.append(event.text)
    text = "\n".join(texts if target is TargetFormat.OPENCODE else (t for t in texts if t))
    return (text or None) if target is TargetFormat.OPENCODE else text, kept


def expected_from_source(
    source: CanonicalObservation,
    *,
    target_format: TargetFormat,
    policy: ProjectionPolicy | None = None,
) -> ExpectedProjection:
    """Compute expected semantics before reading any serialized artifact."""
    from session_migrate.native_audit import validate_observation

    validate_observation(source)
    policy = policy if policy is not None else ProjectionPolicy()
    require(type(policy) is ProjectionPolicy, "unsupported_policy")
    policy.__post_init__()
    archive = policy.policy_id in _ARCHIVE_POLICIES
    reviewed = policy.policy_id in _REVIEWED_POLICIES
    native_context = policy.policy_id in _NATIVE_CONTEXT_POLICIES or reviewed
    require(
        not (archive or native_context) or target_format is TargetFormat.OPENCODE,
        "unsupported_policy",
    )
    require(
        type(target_format) is TargetFormat
        and target_format
        in {
            TargetFormat.CLAUDE,
            TargetFormat.CODEX,
            TargetFormat.OPENCODE,
        },
        "unsupported_target",
    )
    if reviewed:
        require(
            source.audit.selection_policy_id == "claude309-reviewed-conversation-v1"
            and source.audit.client_id == "2.1.309"
            and source.session.reviewed_report is not None,
            "unsupported_policy",
        )
    else:
        require(source.audit.selection_policy_id == "native-active-v1", "unsupported_policy")
    events = source.events
    native_context_hashes = ()
    native_context_only = False
    if native_context:
        from session_migrate.formats.claude_compaction import is_native_context_policy

        native_context_hashes = tuple(
            hashlib.sha256(_json_bytes(event.text)).hexdigest()
            for event in events
            if is_synthetic_text_context(event)
        )
        native_context_only = not reviewed and any(
            is_native_context_policy(event.payload.get("native_context_policy")) for event in events
        )
    if reviewed:
        for event in events:
            if is_synthetic_text_context(event):
                require(
                    event.payload.get("reviewed_context_policy")
                    == "claude-2.1.309-reviewed-conversation-v1"
                    and "native_context_policy" not in event.payload,
                    "source_mismatch",
                )
        # These policies license runtime/ordering differences only. Refuse content
        # the portable grammar would otherwise silently omit or change.
        try:
            _native_source_order(events, reviewed=True)
        except ValueError:
            raise ProjectionError("source_mismatch") from None
        require(
            all(
                (event.kind is not EventKind.TOOL_CALL or type(event.payload.get("input")) is dict)
                and (event.kind is not EventKind.MESSAGE or bool(event.text))
                for event in events
            ),
            "source_mismatch",
        )
    original = tuple(iter_canonical_units(events, policy.canonical_policy))
    tool_issues = {
        "missing_tool_name",
        "missing_call_id",
        "duplicate_call_id",
        "orphan_tool_result",
        "ambiguous_tool_result",
        "duplicate_tool_result",
        "tool_identity_mismatch",
        "open_tool_call",
    }
    raw_witness = hash_events(events, policy.canonical_policy)
    require(
        not any(
            code in tool_issues and not (archive and code == "open_tool_call")
            for code, _ in raw_witness.issue_counts
        ),
        "source_tools_unproven",
    )
    projected: list[Event] = []
    origins: list[list[tuple[int, int | None, int | None]]] = []
    generated_origins: dict[int, int] = {}
    losses_by_source: list[set[str]] = [set() for _ in events]
    transforms = ["preserve" for _ in events]
    calls: dict[str, tuple[int, Event]] = {}
    deferred_results: dict[str, tuple[int, Event]] = {}
    if target_format is TargetFormat.OPENCODE:
        deferred_results = {
            e.tool_call_id: (i, e) for i, e in enumerate(events) if e.kind is EventKind.TOOL_RESULT
        }

    def emit(event: Event, index: int, transform: str = "preserve") -> None:
        projected.append(event)
        origins.append([(index, None, None)])
        transforms[index] = transform

    def result_event(e: Event, index: int, call: Event | None) -> Event:
        losses = losses_by_source[index]
        if set(e.payload) - {"content_blocks", "is_error", "namespace"}:
            losses.add("result_payload_omitted")
        text, blocks = _content(e, target_format, losses, reviewed=reviewed)
        is_error = e.payload.get("is_error") is True
        payload = {"content_blocks": blocks}
        if target_format is not TargetFormat.CODEX:
            payload["is_error"] = is_error
        elif is_error:
            losses.add("tool_error_omitted")
        if "namespace" in e.payload:
            losses.add("tool_namespace_omitted")
        if target_format is TargetFormat.OPENCODE and is_error:
            if any(b["type"] != "text" for b in blocks):
                losses.add("error_result_media_omitted")
            blocks = [{"type": "text", "text": text}] if text else []
            payload["content_blocks"] = blocks
        return replace(
            e,
            text=text,
            tool_name=call.tool_name
            if (target_format is TargetFormat.OPENCODE and call is not None)
            else None,
            payload=payload,
        )

    for index, e in enumerate(events):
        losses = losses_by_source[index]
        if e.kind is EventKind.MESSAGE and e.role in (Role.USER, Role.ASSISTANT) and e.text:
            inert = {"synthetic": False}
            if e.payload and _json_bytes(e.payload) != _json_bytes(inert):
                losses.add("message_payload_omitted")
            payload = {"synthetic": False} if target_format is TargetFormat.OPENCODE else {}
            emit(replace(e, tool_name=None, tool_call_id=None, payload=payload), index)
        elif e.kind is EventKind.TOOL_CALL:
            if set(e.payload) - {"input", "namespace"} or e.text is not None:
                losses.add("call_payload_omitted")
            value = copy.deepcopy(e.payload.get("input", {}))
            if target_format is TargetFormat.CODEX and type(value) is str:
                try:
                    decoded = json.loads(
                        value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                    )
                    _json_bytes(decoded)
                except (ValueError, TypeError):
                    pass
                else:
                    value = decoded
                    losses.add("input_string_decoded")
            elif target_format is not TargetFormat.CODEX and type(value) is not dict:
                value = {"input": value}
                losses.add("non_object_input_wrapped")
            if "namespace" in e.payload:
                losses.add("tool_namespace_omitted")
            projected_call = replace(e, text=None, payload={"input": value})
            calls[e.tool_call_id] = (index, e)
            emit(projected_call, index)
            if target_format is TargetFormat.OPENCODE:
                if e.tool_call_id in deferred_results:
                    result_index, result = deferred_results[e.tool_call_id]
                    emit(
                        result_event(result, result_index, e), result_index, "associate_tool_result"
                    )
                    if result_index != index + 1:
                        losses_by_source[result_index].add("tool_result_order_associated")
                else:
                    require(archive, "source_tools_unproven")
                    generated = replace(
                        projected_call,
                        kind=EventKind.TOOL_RESULT,
                        role=Role.TOOL,
                        text=INCOMPLETE_TOOL_MESSAGE,
                        payload={
                            "content_blocks": [{"type": "text", "text": INCOMPLETE_TOOL_MESSAGE}],
                            "is_error": True,
                        },
                    )
                    projected.append(generated)
                    origins.append([])
                    generated_origins[id(generated)] = index
                    losses.add("incomplete_tool_archived")
        elif e.kind is EventKind.TOOL_RESULT:
            if target_format is not TargetFormat.OPENCODE:
                emit(result_event(e, index, calls[e.tool_call_id][1]), index)
        elif native_context and is_synthetic_text_context(e):
            if (
                set(e.payload) - {"block_type"}
                or e.tool_name is not None
                or e.tool_call_id is not None
            ):
                losses.add("context_payload_omitted")
            emit(
                replace(
                    e,
                    tool_name=None,
                    tool_call_id=None,
                    payload={"block_type": SYNTHETIC_TEXT_BLOCK_TYPE},
                ),
                index,
            )
        elif (
            e.kind is EventKind.CONTEXT
            and e.role is Role.USER
            and (e.payload.get("block_type") == "image" and type(e.payload.get("image_url")) is str)
        ):
            uri = e.payload["image_url"]
            if target_format is not TargetFormat.CODEX and not uri.startswith("data:image/"):
                losses.add("image_reference_omitted")
                transforms[index] = "omit"
                continue
            payload = {"block_type": "image", "image_url": uri}
            if target_format is TargetFormat.OPENCODE:
                payload["mime_type"] = uri[5:].split(";", 1)[0]
            emit(replace(e, text=None, tool_name=None, tool_call_id=None, payload=payload), index)
        elif e.kind is EventKind.COMPACTION and e.text and target_format is not TargetFormat.CLAUDE:
            payload = (
                {}
                if target_format is TargetFormat.CODEX
                else {
                    "source_subtype": "opencode_compaction_summary",
                    "has_boundary_metadata": False,
                }
            )
            losses.add("compaction_structure_omitted")
            emit(
                replace(
                    e,
                    role=Role.SYSTEM,
                    text=e.text.strip() if (target_format is TargetFormat.OPENCODE) else e.text,
                    tool_name=None,
                    tool_call_id=None,
                    payload=payload,
                ),
                index,
                "portable_compaction",
            )
        elif reviewed and e.kind is EventKind.OPAQUE:
            # The reviewed source-order guard admitted only explicit inert
            # metadata reasons. Its original bytes remain in the bound capture;
            # UNKNOWN raw classification is deliberately left unchanged.
            losses.add("reviewed_metadata_archive_only")
            transforms[index] = "archive_source_metadata"
        else:
            losses.add("source_event_omitted")
            transforms[index] = "omit"
    if target_format is TargetFormat.OPENCODE:
        grouped, grouped_origins = [], []
        cursor = 0
        while cursor < len(projected):
            first = projected[cursor]
            end = cursor + 1
            if (
                first.role is Role.USER
                and first.kind in (EventKind.MESSAGE, EventKind.CONTEXT)
                and not (native_context and is_synthetic_text_context(first))
            ):
                while (
                    end < len(projected)
                    and projected[end].role is Role.USER
                    and not (native_context and is_synthetic_text_context(projected[end]))
                    and (
                        projected[end].kind in (EventKind.MESSAGE, EventKind.CONTEXT)
                        and projected[end].provenance.record_index == first.provenance.record_index
                    )
                ):
                    end += 1
                texts = [i for i in range(cursor, end) if projected[i].kind is EventKind.MESSAGE]
                images = [i for i in range(cursor, end) if projected[i].kind is EventKind.CONTEXT]
                if texts:
                    merged = replace(
                        projected[texts[0]], text="\n".join(projected[i].text for i in texts)
                    )
                    merged_origins, offset = [], 0
                    for i in texts:
                        source_index = origins[i][0][0]
                        length = len(projected[i].text)
                        merged_origins.append(
                            (
                                source_index,
                                offset if len(texts) > 1 else None,
                                length if len(texts) > 1 else None,
                            )
                        )
                        if len(texts) > 1:
                            transforms[source_index] = "group_user_text"
                            losses_by_source[source_index].add("user_text_grouped")
                        offset += length + 1
                    grouped.append(merged)
                    grouped_origins.append(merged_origins)
                for i in images:
                    grouped.append(projected[i])
                    grouped_origins.append(origins[i])
                if texts + images != list(range(cursor, end)):
                    for i in range(cursor, end):
                        losses_by_source[origins[i][0][0]].add("user_content_order_grouped")
            else:
                grouped.append(first)
                grouped_origins.append(origins[cursor])
            cursor = end
        projected, origins = grouped, grouped_origins
    projected_units = tuple(iter_canonical_units(projected, policy.canonical_policy))
    target_members: dict[int, tuple[int, int | None, int | None]] = {}
    for target_index, sources in enumerate(origins):
        for source_index, start, length in sources:
            require(source_index not in target_members)
            target_members[source_index] = (target_index, start, length)
    generated_members = tuple(
        GeneratedRelationMember(
            generated_origins[id(event)],
            target_members[generated_origins[id(event)]][0],
            target_index,
            original[generated_origins[id(event)]].sha256,
            projected_units[target_index].sha256,
            "archive_incomplete_tool",
            INCOMPLETE_TOOL_RULE,
        )
        for target_index, event in enumerate(projected)
        if id(event) in generated_origins
    )
    normal_targets = {target[0] for target in target_members.values()}
    generated_targets = {member.target_index for member in generated_members}
    require(not normal_targets & generated_targets)
    require(normal_targets | generated_targets == set(range(len(projected_units))))
    relation_root = hashlib.sha256(
        (
            b"session-migrate/projection-relation/v2\0"
            if archive
            else b"session-migrate/projection-relation/v1\0"
        )
        + bytes.fromhex(policy.sha256)
        + bytes.fromhex(source.audit.authority_sha256)
        + bytes.fromhex(source.audit.selection_sha256)
        + bytes.fromhex(source.audit.classification.chain_sha256)
    ).digest()
    members = []
    loss_counts: Counter[str] = Counter()
    metadata_loss_counts: Counter[str] = Counter()
    raw_known_losses = dict(source.audit.classification_counts).get("known-loss", 0)
    if raw_known_losses:
        loss_counts["source_raw_known_loss"] = raw_known_losses
    reviewed_source_order = _native_source_order(events, reviewed=True) if reviewed else ()
    reviewed_target_order = _reviewed_projected_order(events, generated_members) if reviewed else ()
    if reviewed:
        error_count = sum(
            event.kind is EventKind.TOOL_RESULT and event.payload.get("is_error") is True
            for event in events
        )
        if error_count:
            loss_counts["reviewed_tool_error_envelope"] = error_count
        if reviewed_source_order != reviewed_target_order:
            loss_counts["reviewed_context_order_changed"] = 1
        loss_counts["reviewed_runtime_not_preserved"] = 1
        loss_counts["authority_as_data"] = sum(is_synthetic_text_context(e) for e in events) or 1
    if source.session.title and target_format is TargetFormat.CODEX:
        metadata_loss_counts["session_title_omitted"] = 1
    for index, unit in enumerate(original):
        target = target_members.get(index)
        member = RelationMember(
            index,
            target[0] if target else None,
            unit.sha256,
            projected_units[target[0]].sha256 if target else None,
            "portable_field_projection"
            if target
            and transforms[index] == "preserve"
            and (unit.sha256 != projected_units[target[0]].sha256)
            else transforms[index],
            target[1] if target else None,
            target[2] if target else None,
            tuple(sorted(losses_by_source[index])),
        )
        loss_counts.update(member.losses)
        body = _json_bytes(
            {
                "source": member.source_index,
                "target": member.target_index,
                "source_sha256": member.source_sha256,
                "target_sha256": member.target_sha256,
                "transform": member.transform,
                "text_start": member.target_text_start,
                "text_length": member.target_text_length,
                "losses": list(member.losses),
            }
        )
        relation_root = hashlib.sha256(
            relation_root + index.to_bytes(8, "big") + hashlib.sha256(body).digest()
        ).digest()
        if len(members) < MAX_DIAGNOSTICS:
            members.append(member)
    for member in generated_members:
        body = _json_bytes(
            {
                "source": member.source_index,
                "call_target": member.call_target_index,
                "target": member.target_index,
                "source_sha256": member.source_sha256,
                "target_sha256": member.target_sha256,
                "transform": member.transform,
                "rule_id": member.rule_id,
            }
        )
        relation_root = hashlib.sha256(
            relation_root + b"generated\0" + hashlib.sha256(body).digest()
        ).digest()
    relation_root = hashlib.sha256(
        relation_root
        + _json_bytes(
            {
                "target_format": target_format.value,
                **(
                    {
                        "reviewed_source_order": [list(unit) for unit in reviewed_source_order],
                        "reviewed_target_order": [list(unit) for unit in reviewed_target_order],
                    }
                    if reviewed
                    else {}
                ),
                "loss_counts": dict(sorted(loss_counts.items())),
                "metadata_loss_counts": dict(sorted(metadata_loss_counts.items())),
            }
        )
    ).digest()
    return ExpectedProjection(
        policy,
        hash_events(projected, policy.canonical_policy),
        SequenceWitness(len(original), relation_root.hex()),
        tuple(members),
        generated_members,
        tuple(sorted(loss_counts.items())),
        tuple(sorted(metadata_loss_counts.items())),
        source.coverage is Coverage.VERIFIED,
        not loss_counts and source.fidelity is Coverage.VERIFIED,
        tuple(copy.deepcopy(projected)),
        native_context_hashes=native_context_hashes,
        native_context_only=native_context_only,
        reviewed_source_order=reviewed_source_order,
        reviewed_target_order=reviewed_target_order,
    )
