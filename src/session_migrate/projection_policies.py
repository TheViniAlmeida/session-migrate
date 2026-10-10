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

from session_migrate.canonical import (
    MAX_DIAGNOSTICS,
    CanonicalPolicy,
    SemanticWitness,
    _json_bytes,
    hash_events,
    iter_canonical_units,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import Coverage
from session_migrate.model import Event, EventKind, Role, TargetFormat
from session_migrate.witnesses import SequenceWitness

if TYPE_CHECKING:
    from session_migrate.native_audit import CanonicalObservation


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
        require(self.policy_id == "portable-projection-v1", "unsupported_policy")
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
class ExpectedProjection:
    policy: ProjectionPolicy
    witness: SemanticWitness
    relation: SequenceWitness
    members: tuple[RelationMember, ...]
    loss_counts: tuple[tuple[str, int], ...]
    metadata_loss_counts: tuple[tuple[str, int], ...]
    relation_complete: bool
    preserves_content: bool
    _events: tuple[Event, ...]

    @property
    def events(self) -> tuple[Event, ...]:
        return copy.deepcopy(self._events)


def _content(event: Event, target: TargetFormat, losses: set[str]) -> tuple[str | None, list]:
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
    events = source.events
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
        not any(code in tool_issues for code, _ in raw_witness.issue_counts),
        "source_tools_unproven",
    )
    projected: list[Event] = []
    origins: list[list[tuple[int, int | None, int | None]]] = []
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
        text, blocks = _content(e, target_format, losses)
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
                result_index, result = deferred_results[e.tool_call_id]
                emit(result_event(result, result_index, e), result_index, "associate_tool_result")
                if result_index != index + 1:
                    losses_by_source[result_index].add("tool_result_order_associated")
        elif e.kind is EventKind.TOOL_RESULT:
            if target_format is not TargetFormat.OPENCODE:
                emit(result_event(e, index, calls[e.tool_call_id][1]), index)
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
        else:
            losses.add("source_event_omitted")
            transforms[index] = "omit"
    if target_format is TargetFormat.OPENCODE:
        grouped, grouped_origins = [], []
        cursor = 0
        while cursor < len(projected):
            first = projected[cursor]
            end = cursor + 1
            if first.role is Role.USER and first.kind in (EventKind.MESSAGE, EventKind.CONTEXT):
                while (
                    end < len(projected)
                    and projected[end].role is Role.USER
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
    relation_root = hashlib.sha256(
        b"session-migrate/projection-relation/v1\0"
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
    relation_root = hashlib.sha256(
        relation_root
        + _json_bytes(
            {
                "target_format": target_format.value,
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
        tuple(sorted(loss_counts.items())),
        tuple(sorted(metadata_loss_counts.items())),
        source.coverage is Coverage.VERIFIED,
        not loss_counts and source.fidelity is Coverage.VERIFIED,
        tuple(copy.deepcopy(projected)),
    )
