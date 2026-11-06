"""Bounded, read-only replay selection for preserved Claude Code 2.1.309 cuts.

This is the native latest-boundary contract, not the SDK display-history fold.
Rows keep their original envelopes; relinks exist only in the returned index map.
Unsupported native checkpoint, gap and indirect tool recovery fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NoReturn

from session_migrate.errors import SessionMigrateError
from session_migrate.jsonl import JsonlRecord

POLICY_ID = "claude-2.1.309-native-latest-preserved-v1"
RECOVERY_POLICY_ID = "claude-2.1.309-native-latest-preserved-v2"
NATIVE_CONTEXT_POLICY_IDS = frozenset({POLICY_ID, RECOVERY_POLICY_ID})
PERSISTED_CONTROL_TYPES = frozenset(
    {"prompt_snapshot", "prompt_render_point", "deferred_tools_record"}
)
_NODE_TYPES = frozenset({"user", "assistant", "attachment", "system"})
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class CompactionSelection:
    """An immutable index selection; source records are borrowed, never rewritten."""

    selected_records: tuple[JsonlRecord, ...] = field(repr=False)
    selected_indices: tuple[int, ...]
    effective_parents: Mapping[int, int | None]
    boundary_index: int
    summary_index: int
    summary_boundary_index: int
    historical_indices: tuple[int, ...]
    retired_indices: tuple[int, ...]
    excluded_indices: tuple[int, ...]
    preserved_indices: tuple[int, ...]
    diagnostics: tuple[str, ...]
    original_leaf_index: int
    parent_chain_indices: tuple[int, ...]
    recovered_indices: tuple[int, ...]
    recovery_provenance: Mapping[int, tuple[str, int]]
    retired_control_indices: tuple[int, ...]
    policy_id: str = POLICY_ID


def _fail(reason: str) -> NoReturn:
    # Every reason is a fixed implementation literal, never source content.
    raise SessionMigrateError(f"Claude 2.1.309 preserved compaction: {reason}")


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value)


def is_native_context_policy(value: Any) -> bool:
    return type(value) is str and value in NATIVE_CONTEXT_POLICY_IDS


def _boundary(record: JsonlRecord) -> bool:
    return (
        record.value.get("type") == "system" and record.value.get("subtype") == "compact_boundary"
    )


def _preserves(record: JsonlRecord) -> bool:
    metadata = record.value.get("compactMetadata")
    return isinstance(metadata, Mapping) and (
        "preservedMessages" in metadata or "preservedSegment" in metadata
    )


def _identifiers(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(map(_identifier, value)):
        _fail("malformed preservation UUID list")
    if len(set(value)) != len(value):
        _fail("duplicate preservation UUID")
    return value


def _walk(start: str, parents: Mapping[str, str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    cursor: str | None = start
    while cursor is not None:
        if cursor in seen:
            _fail("ancestry cycle")
        if cursor not in parents:
            _fail("missing parent UUID")
        seen.add(cursor)
        result.append(cursor)
        cursor = parents[cursor]
    return result


def _validate_cycles(parents: Mapping[str, str | None]) -> None:
    done: set[str] = set()
    for uid in parents:
        visiting: set[str] = set()
        cursor: str | None = uid
        while cursor is not None and cursor in parents and cursor not in done:
            if cursor in visiting:
                _fail("ancestry cycle")
            visiting.add(cursor)
            cursor = parents[cursor]
        done.update(visiting)


def _preserved(
    metadata: Mapping[str, Any], nodes: Mapping[str, JsonlRecord], parents: Mapping[str, str | None]
) -> tuple[str, list[str], str]:
    if "preservedMessages" in metadata:
        kept = metadata["preservedMessages"]
        if not isinstance(kept, Mapping) or not _identifier(kept.get("anchorUuid")):
            _fail("malformed preservedMessages")
        uuids = _identifiers(kept.get("uuids"))
        if "allUuids" in kept:
            all_uuids = _identifiers(kept["allUuids"])
            if not set(uuids) <= set(all_uuids):
                _fail("allUuids omits persisted preservation UUIDs")
        return kept["anchorUuid"], uuids, "preservation_list"
    segment = metadata.get("preservedSegment")
    if not isinstance(segment, Mapping) or not all(
        _identifier(segment.get(key)) for key in ("headUuid", "tailUuid", "anchorUuid")
    ):
        _fail("malformed preservedSegment")
    head, cursor = segment["headUuid"], segment["tailUuid"]
    reverse: list[str] = []
    seen: set[str] = set()
    while cursor is not None:
        if cursor in seen:
            _fail("preservedSegment ancestry cycle")
        if cursor not in nodes:
            _fail("missing preservation UUID")
        seen.add(cursor)
        reverse.append(cursor)
        if cursor == head:
            return segment["anchorUuid"], list(reversed(reverse)), "preservation_walk"
        cursor = parents[cursor]
    _fail("preservedSegment does not reach head")


def select_compaction(records: Sequence[JsonlRecord]) -> CompactionSelection | None:
    """Select the proven native replay path, or decline non-applicable sources.

    The caller first collapses only proven duplicate Desktop envelopes. Physical
    indices remain authoritative for provenance and latest-boundary retirement.
    Recognized malformed preserved transcripts raise instead of falling back.
    """
    boundaries = [record for record in records if _boundary(record)]
    recognized = [
        record
        for record in boundaries
        if record.value.get("version") == "2.1.309" and _preserves(record)
    ]
    if not recognized:
        return None
    boundary = boundaries[-1]
    if boundary.value.get("version") != "2.1.309" or not _preserves(boundary):
        _fail("later boundary supersedes preservation without a proven contract")
    if len({record.index for record in records}) != len(records):
        _fail("duplicate physical record index")
    if any(left.index >= right.index for left, right in zip(records, records[1:], strict=False)):
        _fail("source records are not in physical order")
    positions = {record.index: offset for offset, record in enumerate(records)}
    cut = positions[boundary.index]
    nodes: dict[str, JsonlRecord] = {}
    parents: dict[str, str | None] = {}
    session_ids: set[str] = set()
    for record in records:
        value = record.value
        uid = value.get("uuid")
        if uid is None:
            if _identifier(value.get("type")) and value.get("type") in _NODE_TYPES:
                _fail("missing graph record UUID")
            continue
        if not _identifier(uid) or uid in nodes:
            _fail("malformed or duplicate record UUID")
        if (
            not _identifier(value.get("type"))
            or value.get("type") not in _NODE_TYPES
            or value.get("version") != "2.1.309"
        ):
            _fail("unsupported graph record identity")
        if (value.get("isSidechain") is not None and value.get("isSidechain") is not False) or (
            value.get("agentId") is not None
        ):
            _fail("sidechain or subagent scope is unsupported")
        if any(
            key in value and type(value[key]) is not bool for key in ("isMeta", "isCompactSummary")
        ):
            _fail("malformed context flag")
        sid = value.get("sessionId")
        if not _identifier(sid):
            _fail("missing session identity")
        session_ids.add(sid)
        parent = value.get("parentUuid")
        if "parentUuid" not in value or (parent is not None and not _identifier(parent)):
            _fail("malformed parent UUID")
        if value.get("type") in {"user", "assistant"}:
            message = value.get("message")
            if not isinstance(message, Mapping) or message.get("role") != value.get("type"):
                _fail("malformed conversation identity")
        nodes[uid], parents[uid] = record, parent
    if len(session_ids) != 1:
        _fail("mixed session identity")
    _validate_cycles(parents)
    boundary_uid = boundary.value.get("uuid")
    if boundary_uid not in nodes or parents[boundary_uid] is not None:
        _fail("boundary is not a native graph root")
    metadata = boundary.value["compactMetadata"]
    anchor, kept, source = _preserved(metadata, nodes, parents)
    if any(uid not in nodes for uid in kept):
        _fail("missing preservation UUID")
    if anchor not in nodes or nodes[anchor].value.get("isCompactSummary") is not True:
        _fail("missing compact summary anchor")
    if anchor in kept or boundary_uid in kept:
        _fail("preservation includes its own anchor or boundary")
    if nodes[anchor].value.get("type") != "user":
        _fail("invalid compact summary identity")
    anchor_path: list[str] = []
    anchor_seen: set[str] = set()
    cursor: str | None = anchor
    while cursor != boundary_uid:
        if cursor is None or cursor not in nodes or cursor in anchor_seen:
            _fail("compact summary does not reach selected boundary")
        if cursor != anchor and (
            nodes[cursor].value.get("type") in {"user", "assistant"} or _boundary(nodes[cursor])
        ):
            _fail("compact summary has an unproven material bridge")
        anchor_path.append(cursor)
        anchor_seen.add(cursor)
        cursor = parents[cursor]
    if any(positions[nodes[uid].index] < cut for uid in anchor_path):
        _fail("compact summary anchor precedes selected boundary")
    if any(
        nodes[uid].value.get("isCompactSummary") is True or _boundary(nodes[uid]) for uid in kept
    ):
        _fail("preservation contains a summary or boundary")

    # Native latest-boundary relink (upo), represented without altering envelopes.
    effective = dict(parents)
    previous = anchor
    for uid in kept:
        effective[uid] = previous
        previous = uid
    tail = kept[-1]
    for uid in effective:
        if effective[uid] == anchor and uid != kept[0]:
            effective[uid] = tail
    kept_set = set(kept)
    retired = {
        uid
        for uid, record in nodes.items()
        if positions[record.index] < cut and uid not in kept_set
    }
    live = {uid: record for uid, record in nodes.items() if uid not in retired}
    effective = {uid: parent for uid, parent in effective.items() if uid in live}
    for uid, record in live.items():
        if record.value.get("type") in {"user", "assistant"} and effective[uid] in retired:
            effective[uid] = tail
    _validate_cycles(effective)
    if any(parent is not None and parent not in live for parent in effective.values()):
        _fail("missing parent UUID after native relink")
    leaf, checkpoint_flags = _select_leaf(records, nodes, live, effective, boundary.index)
    chain = list(reversed(_walk(leaf, effective)))
    selected_ids = set(chain)
    if not {anchor, boundary_uid} <= selected_ids:
        _fail("selected branch does not include preservation anchor")
    ordered, provenance = _recover_chain(live, effective, chain)
    selected_ids = set(ordered)
    if not kept_set <= selected_ids:
        _fail("selected branch does not include preservation anchor")
    _validate_tool_scope(live, selected_ids, ordered, set(provenance))
    selected_records = tuple(live[uid] for uid in ordered)
    if sum(record.value.get("isCompactSummary") is True for record in selected_records) != 1:
        _fail("multiple selected compact summaries")
    selected_indices = tuple(record.index for record in selected_records)
    selected_set = set(selected_indices)
    retired_controls = tuple(
        record.index
        for uid, record in nodes.items()
        if uid in retired
        and isinstance(attachment := record.value.get("attachment"), Mapping)
        and _identifier(attachment.get("type"))
        and attachment["type"] in PERSISTED_CONTROL_TYPES
    )
    diagnostics = [source, "preserved_usage_accounting_unproven", *checkpoint_flags]
    if provenance:
        diagnostics.append("native_parent_linked_batch_recovery")
    if retired_controls:
        diagnostics.append("retired_persisted_controls_inert")
    if any(
        isinstance(record.value.get("compactMetadata"), Mapping)
        and "runningTurnSends" in record.value["compactMetadata"]
        for record in boundaries
    ):
        diagnostics.append("running_turn_sends_not_transportable")
    return CompactionSelection(
        selected_records=selected_records,
        selected_indices=selected_indices,
        effective_parents=MappingProxyType(
            {
                live[uid].index: live[parent].index if parent is not None else None
                for uid, parent in effective.items()
            }
        ),
        boundary_index=boundary.index,
        summary_index=nodes[anchor].index,
        summary_boundary_index=boundary.index,
        historical_indices=tuple(record.index for record in records),
        retired_indices=tuple(
            record.index for record in records if record.value.get("uuid") in retired
        ),
        excluded_indices=tuple(
            record.index for record in records if record.index not in selected_set
        ),
        preserved_indices=tuple(nodes[uid].index for uid in kept),
        diagnostics=tuple(diagnostics),
        original_leaf_index=live[leaf].index,
        parent_chain_indices=tuple(live[uid].index for uid in chain),
        recovered_indices=tuple(live[uid].index for uid in ordered if uid in provenance),
        recovery_provenance=MappingProxyType(
            {
                live[uid].index: (reason, live[owner].index)
                for uid, (reason, owner) in provenance.items()
            }
        ),
        retired_control_indices=retired_controls,
        policy_id=RECOVERY_POLICY_ID
        if provenance or retired_controls or "native_checkpoint_through_batch" in checkpoint_flags
        else POLICY_ID,
    )


def _select_leaf(
    records: Sequence[JsonlRecord],
    all_nodes: Mapping[str, JsonlRecord],
    live: Mapping[str, JsonlRecord],
    parents: Mapping[str, str | None],
    boundary_index: int,
) -> tuple[str, tuple[str, ...]]:
    checkpoint: str | None = None
    explicit = False
    ever_explicit = False
    rewound = False
    cleared = False
    last_row: str | None = None
    after_boundary = False
    session_ids = {row.value.get("sessionId") for row in live.values()}
    for record in records:
        value = record.value
        if record.index == boundary_index:
            after_boundary = True
            checkpoint, explicit, ever_explicit, rewound = None, False, False, False
        if not after_boundary:
            continue
        if value.get("type") == "last-prompt":
            sid = value.get("sessionId")
            if sid is not None and (not _identifier(sid) or sid not in session_ids):
                _fail("checkpoint has incompatible session scope")
            if value.get("version") is not None and value.get("version") != "2.1.309":
                _fail("checkpoint has incompatible version scope")
            for flag in ("explicit", "rewound"):
                if flag in value and type(value[flag]) is not bool:
                    _fail("malformed checkpoint flags")
            leaf = value.get("leafUuid")
            if leaf is None and value.get("explicit") is True:
                cleared = True
                checkpoint, explicit, ever_explicit, rewound = None, False, False, False
            if leaf is not None:
                if not _identifier(leaf) or leaf not in all_nodes:
                    _fail("checkpoint references a missing leaf UUID")
                explicit = value.get("explicit") is True or (explicit and leaf == checkpoint)
                ever_explicit = value.get("explicit") is True or (
                    ever_explicit and leaf == checkpoint
                )
                rewound = value.get("rewound") is True or (rewound and leaf == checkpoint)
                checkpoint = leaf
                cleared = False
        elif value.get("uuid") in live:
            attachment = value.get("attachment")
            if (
                value.get("type") == "attachment"
                and isinstance(attachment, Mapping)
                and attachment.get("type") == "fork_briefing"
            ):
                continue
            last_row = value["uuid"]
            explicit = rewound = cleared = False
    if cleared:
        _fail("explicit clear checkpoint is unsupported")
    if checkpoint in live and (explicit or rewound):
        _fail("effective explicit or rewind checkpoint is unsupported")
    referenced = {parent for parent in parents.values() if parent is not None}
    material_parents = {
        parents[uid]
        for uid, record in live.items()
        if record.value.get("type") in {"user", "assistant"}
    }
    candidates: set[str] = set()
    nearest_material: dict[str, str | None] = {}
    for uid in live.keys() - referenced:
        cursor: str | None = uid
        traversed: list[str] = []
        ancestor = None
        while cursor is not None:
            if cursor in nearest_material:
                ancestor = nearest_material[cursor]
                break
            traversed.append(cursor)
            if live[cursor].value.get("type") in {"user", "assistant"}:
                ancestor = cursor
                break
            cursor = parents[cursor]
        for visited in traversed:
            nearest_material[visited] = ancestor
        if ancestor is not None and ancestor not in material_parents:
            candidates.add(ancestor)
    if not candidates:
        _fail("no proven conversation leaf")
    if len(candidates) == 1:
        return candidates.pop(), ("native_unique_leaf",)
    # Ko calls rPt with direct=False: only a proven batch relationship can
    # displace a candidate checkpoint. Ordinary descendants do not do so.
    through_batch = (
        checkpoint in candidates
        and last_row is not None
        and _through_batch(live, parents, last_row, checkpoint, enabled=not ever_explicit)
    )
    if checkpoint in candidates and not through_batch:
        chosen = checkpoint
    elif last_row is not None:
        chosen = next(
            (
                uid
                for uid in _walk(last_row, parents)
                if live[uid].value.get("type") in {"user", "assistant"}
            ),
            None,
        )
    else:
        chosen = None
    if chosen not in candidates:
        _fail("ambiguous native conversation leaves")
    return chosen, (
        ("native_leaf_pruned", "native_checkpoint_through_batch")
        if through_batch
        else ("native_leaf_pruned",)
    )


def _blocks(record: JsonlRecord, kind: str, *, strict: bool = False) -> set[str]:
    message = record.value.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return set()
    key = "id" if kind == "tool_use" else "tool_use_id"
    identifiers: set[str] = set()
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != kind:
            continue
        identifier = block.get(key)
        if not _identifier(identifier) or identifier in identifiers:
            if strict:
                _fail("malformed or duplicate tool identity")
            continue
        identifiers.add(identifier)
    return identifiers


def _batch_content(record: JsonlRecord, batch: str) -> bool:
    value = record.value
    kind = value.get("type")
    if kind == "assistant":
        return value["message"].get("id") == batch
    if kind == "user":
        return value.get("isMeta") is True or bool(_blocks(record, "tool_result"))
    return kind == "attachment" or (kind == "system" and not _boundary(record))


def _metadata(record: JsonlRecord) -> bool:
    value = record.value
    return (
        value.get("type") == "attachment"
        or (value.get("type") == "system" and not _boundary(record))
        or (
            value.get("type") == "user"
            and value.get("isMeta") is True
            and not _blocks(record, "tool_result")
        )
    )


def _batch_ancestors(
    live: Mapping[str, JsonlRecord], parents: Mapping[str, str | None], uid: str, batch: str
) -> list[str]:
    result: list[str] = []
    cursor = parents[uid]
    while cursor is not None and _batch_content(live[cursor], batch):
        result.append(cursor)
        cursor = parents[cursor]
    return result


def _through_batch(
    live: Mapping[str, JsonlRecord],
    parents: Mapping[str, str | None],
    start: str,
    target: str,
    *,
    enabled: bool,
) -> bool:
    """The native rPt predicate with direct=False, used only by Ko pruning."""
    if not enabled:
        return False
    cursor: str | None = target
    metadata: JsonlRecord | None = None
    while cursor is not None and _metadata(live[cursor]):
        metadata = live[cursor]
        cursor = parents[cursor]
    base = live[cursor] if cursor is not None else None
    owner: str | None = None
    results: set[str] = set()
    tool_attachment = False
    if base is not None and base.value.get("type") == "user" and _blocks(base, "tool_result"):
        results = _blocks(base, "tool_result")
        owner = parents[cursor]
    elif metadata is not None:
        attachment = metadata.value.get("attachment")
        tool = attachment.get("toolUseID") if isinstance(attachment, Mapping) else None
        batch = (
            base.value["message"].get("id")
            if base is not None and (base.value.get("type") == "assistant")
            else None
        )
        if _identifier(batch) and type(tool) is str:
            owner = next(
                (
                    uid
                    for uid in [cursor, *_batch_ancestors(live, parents, cursor, batch)]
                    if tool in _blocks(live[uid], "tool_use")
                ),
                None,
            )
            if owner is not None:
                results = {tool}
                tool_attachment = True
    else:
        owner = cursor
    calls = _blocks(live[owner], "tool_use") if owner is not None else set()
    batch = (
        live[owner].value["message"].get("id")
        if owner is not None and (live[owner].value.get("type") == "assistant" and calls)
        else None
    )
    if not _identifier(batch):
        return False
    ancestors = set(_batch_ancestors(live, parents, owner, batch))
    previous: str | None = None
    through = False
    cursor = start
    while cursor is not None:
        record = live[cursor]
        if through and cursor == owner:
            return True
        if (
            record.value.get("type") == "assistant"
            and cursor != owner
            and record.value["message"].get("id") == batch
            and _blocks(record, "tool_use")
        ):
            if cursor in ancestors:
                return True
            through = True
        elif not _batch_content(record, batch):
            through = False
        if cursor == owner and previous is not None and results & calls:
            previous_results = _blocks(live[previous], "tool_result")
            if any(
                call in calls and (len(calls) > 1 if tool_attachment else call not in results)
                for call in previous_results
            ):
                return True
        previous, cursor = cursor, parents[cursor]
    return False


def _recover_chain(
    live: Mapping[str, JsonlRecord], parents: Mapping[str, str | None], chain: list[str]
) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Native kpo's flag-invariant parent-linked subset, preserving its anchors.

    Off-parent source/call-ID associations are deliberately left unselected and
    rejected by _validate_tool_scope. Thus neither flag can add such a result to
    an accepted replay. No source parent is replaced by the recovered order.
    """
    batches: dict[str, list[str]] = {}
    results: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    positions = {uid: offset for offset, uid in enumerate(live)}
    for uid, record in live.items():
        parent = parents[uid]
        if parent is not None:
            children.setdefault(parent, []).append(uid)
        if record.value.get("type") == "assistant":
            batch = record.value["message"].get("id")
            if batch is not None and not _identifier(batch):
                _fail("malformed assistant batch identity")
            if batch:
                batches.setdefault(batch, []).append(uid)
        elif record.value.get("type") == "user" and _blocks(record, "tool_result"):
            if parent is not None:
                results.setdefault(parent, []).append(uid)
    seen = set(chain)
    chain_positions = {uid: offset for offset, uid in enumerate(chain)}
    processed: set[str] = set()
    provenance: dict[str, tuple[str, str]] = {}
    intervals: list[tuple[int, int, list[str], dict[str, list[str]]]] = []

    def tail(start: str, owner: str) -> list[str]:
        pending: list[str] = []
        cursor = start
        while True:
            record = live[cursor]
            if (
                cursor in seen
                or not _metadata(record)
                or record.value.get("isSidechain", _ABSENT)
                != live[owner].value.get("isSidechain", _ABSENT)
            ):
                return []
            pending.append(cursor)
            descendants = [uid for uid in children.get(cursor, ()) if uid not in seen]
            if len(descendants) > 1:
                return []
            if not descendants:
                return pending
            cursor = descendants[0]

    def recover_tails(owner: str, destination: list[str]) -> None:
        for child in children.get(owner, ()):
            for uid in tail(child, owner):
                seen.add(uid)
                destination.append(uid)
                provenance[uid] = ("batch_metadata_tail", owner)

    for anchor in chain:
        record = live[anchor]
        if record.value.get("type") != "assistant":
            continue
        batch = record.value["message"].get("id")
        if not batch or batch in processed:
            continue
        processed.add(batch)
        members = batches[batch]
        roots = set()
        for uid in members:
            ancestors = _batch_ancestors(live, parents, uid, batch)
            roots.add(parents[ancestors[-1] if ancestors else uid])
        if len(roots) != 1:
            _fail("cross-chain assistant batch identity is unsupported")
        recovered = [uid for uid in members if uid not in seen]
        for uid in recovered:
            provenance[uid] = ("assistant_batch", anchor)
            seen.add(uid)
        for member in members:
            for uid in results.get(member, ()):
                if uid not in seen:
                    seen.add(uid)
                    recovered.append(uid)
                    provenance[uid] = ("parent_linked_tool_result", member)
        start = chain_positions[anchor]
        end = max(chain_positions.get(uid, start) + 1 for uid in members)
        while end < len(chain) and _batch_content(live[chain[end]], batch):
            end += 1
        anchored: dict[str, list[str]] = {}
        if any(_blocks(live[uid], "tool_use") for uid in members):
            owners = list(members)
            for member in members:
                owners.extend(uid for uid in results.get(member, ()) if uid in seen)
            for owner in owners:
                recover_tails(owner, recovered)
            for owner in chain[start + 1 : end]:
                if _metadata(live[owner]):
                    tails: list[str] = []
                    recover_tails(owner, tails)
                    if tails:
                        anchored[owner] = tails
        if recovered or anchored:
            intervals.append((start, end, sorted(recovered, key=positions.__getitem__), anchored))
    if not intervals:
        return chain, provenance
    merged: list[tuple[int, int, list[str], dict[str, list[str]]]] = []
    for start, end, recovered, anchored in intervals:
        if merged and start < merged[-1][1]:
            old_start, old_end, old_recovered, old_anchored = merged.pop()
            for uid, tails in anchored.items():
                old_anchored.setdefault(uid, []).extend(tails)
            merged.append(
                (
                    old_start,
                    max(old_end, end),
                    sorted([*old_recovered, *recovered], key=positions.__getitem__),
                    old_anchored,
                )
            )
        else:
            merged.append((start, end, recovered, anchored))
    ordered: list[str] = []
    cursor = 0
    for start, end, recovered, anchored in merged:
        ordered.extend(chain[cursor : start + 1])
        offset = 0
        for uid in chain[start + 1 : end]:
            while offset < len(recovered) and positions[recovered[offset]] < positions[uid]:
                ordered.append(recovered[offset])
                offset += 1
            ordered.append(uid)
            ordered.extend(anchored.get(uid, ()))
        ordered.extend(recovered[offset:])
        cursor = end
    ordered.extend(chain[cursor:])
    return ordered, provenance


def _validate_tool_scope(
    live: Mapping[str, JsonlRecord], selected: set[str], ordered: list[str], recovered: set[str]
) -> None:
    batches: dict[str, set[str]] = {}
    selected_calls: set[str] = set()
    calls_by_uid = {
        uid: _blocks(record, "tool_use", strict=bool(recovered)) for uid, record in live.items()
    }
    results_by_uid = {
        uid: _blocks(record, "tool_result", strict=bool(recovered)) for uid, record in live.items()
    }
    for uid, record in live.items():
        value = record.value
        source = value.get("sourceToolAssistantUUID")
        if source is not None and not _identifier(source):
            _fail("malformed tool assistant identity")
        if value.get("type") == "assistant":
            message = value["message"]
            batch = message.get("id")
            if _identifier(batch):
                batches.setdefault(batch, set()).add(uid)
            if uid in selected:
                selected_calls.update(calls_by_uid[uid])
    for members in batches.values():
        if members & selected and members - selected:
            _fail("cross-chain assistant batch recovery is unsupported")
    owners: dict[str, list[str]] = {}
    for uid, calls in calls_by_uid.items():
        for call in calls:
            owners.setdefault(call, []).append(uid)
    if recovered and any(len(owners[call]) != 1 for call in selected_calls):
        _fail("ambiguous tool call identity")
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    for uid in ordered:
        record = live[uid]
        calls, results = calls_by_uid[uid], results_by_uid[uid]
        if (
            calls & seen_calls
            or (recovered and results & seen_results)
            or not results <= seen_calls
        ):
            _fail("unproven tool causal ordering")
        if results and uid in recovered:
            parent = record.value.get("parentUuid")
            if (
                parent not in selected
                or live[parent].value.get("type") != "assistant"
                or not results <= calls_by_uid[parent]
            ):
                _fail("unproven parent-linked tool assistant identity")
        source = record.value.get("sourceToolAssistantUUID")
        if results and source is not None:
            if source not in selected:
                _fail("cross-chain tool recovery is unsupported")
            if live[source].value.get("type") != "assistant" or not results <= calls_by_uid[source]:
                _fail("unproven tool assistant identity")
        seen_calls.update(calls)
        seen_results.update(results)
    for uid, record in live.items():
        if uid in selected or not results_by_uid[uid]:
            continue
        if (
            results_by_uid[uid] & selected_calls
            or record.value.get("parentUuid") in selected
            or record.value.get("sourceToolAssistantUUID") in selected
        ):
            _fail("cross-chain tool recovery is unsupported")
