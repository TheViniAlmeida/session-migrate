"""Bounded, non-authoritative historical text carried by native synthetic records."""

from __future__ import annotations

from typing import Any

from session_migrate.model import Event, EventKind, Role

SYNTHETIC_TEXT_BLOCK_TYPE = "synthetic_text"
_MARKER_KEY = "session_migrate_context"


def is_synthetic_text_context(event: Event) -> bool:
    """Recognize the canonical plain-text context type, including empty text."""
    return (
        event.kind == EventKind.CONTEXT
        and event.role == Role.USER
        and isinstance(event.text, str)
        and event.payload.get("block_type") == SYNTHETIC_TEXT_BLOCK_TYPE
    )


def synthetic_text_metadata() -> dict[str, Any]:
    """Return a fresh reserved marker; no source operational metadata is copied."""
    return {_MARKER_KEY: {"kind": SYNTHETIC_TEXT_BLOCK_TYPE, "version": 1}}


def is_marked_synthetic_text(message: object) -> bool:
    """Accept only the exact reserved marker on a native synthetic text envelope.

    This marker identifies historical content, never a user instruction or system
    authority. Unknown metadata, marker revisions, and lookalike types stay opaque.
    """
    if (
        type(message) is not dict
        or message.get("type") != "synthetic"
        or type(message.get("text")) is not str
    ):
        return False
    metadata = message.get("metadata")
    if type(metadata) is not dict or set(metadata) != {_MARKER_KEY}:
        return False
    marker = metadata[_MARKER_KEY]
    return (
        type(marker) is dict
        and set(marker) == {"kind", "version"}
        and marker["kind"] == SYNTHETIC_TEXT_BLOCK_TYPE
        and type(marker["version"]) is int
        and marker["version"] == 1
    )
