"""Content-free witness carriers, independent of canonicalization and native I/O.

A chain root describes the entire declared sequence. These types validate only
structure: they neither compute hashes nor establish coverage, native boundary
authority, prefix equivalence, or permission to update a session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from session_migrate.errors import SessionMigrateError

MAX_WITNESS_COUNT = 2**31 - 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class WitnessValidationError(SessionMigrateError):
    """Fixed error; invalid witness values are never included in its message."""

    def __init__(self) -> None:
        super().__init__("Invalid witness metadata.")


def _require(condition: bool) -> None:
    if not condition:
        raise WitnessValidationError()


def _count(value: object) -> None:
    _require(type(value) is int and 0 <= value <= MAX_WITNESS_COUNT)


@dataclass(frozen=True, slots=True, repr=False)
class SequenceWitness:
    """Exact count and chain root for one complete ordered collection."""

    count: int
    chain_sha256: str

    def __post_init__(self) -> None:
        _count(self.count)
        _require(type(self.chain_sha256) is str and bool(_SHA256.fullmatch(self.chain_sha256)))


class BoundaryKind(StrEnum):
    UNPROVEN = "unproven"
    NATIVE_CLOSED = "native_closed"
    IMPORTED_CLOSED = "imported_closed"


@dataclass(frozen=True, slots=True, repr=False)
class BoundaryWitness:
    """An explicit boundary declaration; zero pending calls does not prove it."""

    kind: BoundaryKind
    unit_offset: int
    pending_call_count: int

    def __post_init__(self) -> None:
        _require(type(self.kind) is BoundaryKind)
        _count(self.unit_offset)
        _count(self.pending_call_count)
        _require(self.kind is BoundaryKind.UNPROVEN or self.pending_call_count == 0)


@dataclass(frozen=True, slots=True, repr=False)
class PrefixWitness:
    """Version 2 sequence carriers; policy/algorithm authority lives elsewhere."""

    schema_version: int
    units: SequenceWitness
    tool_links: SequenceWitness
    structure: SequenceWitness | None
    closed_turn_count: int
    boundary: BoundaryWitness

    def __post_init__(self) -> None:
        _require(type(self.schema_version) is int and self.schema_version == 2)
        _require(type(self.units) is SequenceWitness and type(self.tool_links) is SequenceWitness)
        _require(self.structure is None or type(self.structure) is SequenceWitness)
        _count(self.closed_turn_count)
        _require(type(self.boundary) is BoundaryWitness)
        _require(self.boundary.unit_offset <= self.units.count)
