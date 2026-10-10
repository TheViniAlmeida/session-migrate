"""Closed native grammars, used only by the private raw auditor."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.model import Session
from session_migrate.witnesses import SequenceWitness

SEMANTIC = "semantic"
STRUCTURE = "structure"
COORDINATE = "generated-coordinate"
ACCOUNTING = "proved-accounting"
EXCLUDED = "selected-scope-exclusion"
LOSS = "known-loss"
UNKNOWN = "unknown"
CATEGORIES = (SEMANTIC, STRUCTURE, COORDINATE, ACCOUNTING, EXCLUDED, LOSS, UNKNOWN)
MAX_DIAGNOSTICS = 1024
KNOWN_LOSS_ISSUES = frozenset(
    {
        "selected_scope_exclusion",
        "compaction_unproven",
        "provider_body_known_loss",
        "incomplete_tool",
        "parser_semantic_enrichment",
        "image_detail_loss",
        "semantic_alias_shadow",
        "custom_input_json_shape_loss",
        "phase_context_loss",
        "empty_text_carrier_loss",
        "opaque_result_carrier_loss",
    }
)


def strict_decode(data: bytes | str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("constant")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        pending = [(value, 0)]
        while pending:
            current, depth = pending.pop()
            if depth > 128 or (type(current) is float and not math.isfinite(current)):
                raise ValueError("limit")
            if type(current) is str:
                current.encode("utf-8")
            elif type(current) is dict:
                for key, item in current.items():
                    key.encode("utf-8")
                    pending.append((item, depth + 1))
            elif type(current) is list:
                pending.extend((item, depth + 1) for item in current)
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise SessionMigrateError("Invalid native audit JSON.") from None


def _encoded(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class AuditAtom:
    index: int
    record_index: int
    path: tuple[str | int, ...]
    category: str
    sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class AuditIssue:
    code: str
    record_index: int


@dataclass(frozen=True, slots=True, repr=False)
class Rule:
    category: str
    types: tuple[type, ...] = ()
    fields: dict[str, Rule] | None = None
    item: Rule | None = None
    required: tuple[str, ...] = ()
    whole: bool = False
    factory: Callable[[Any], Rule] | None = None


def scalar(category: str, *types: type) -> Rule:
    return Rule(category, types)


def subtree(category: str) -> Rule:
    return Rule(category, whole=True)


def dynamic(factory: Callable[[Any], Rule]) -> Rule:
    return Rule(STRUCTURE, factory=factory)


def obj(fields: dict[str, Rule], *, category: str = STRUCTURE, required=()) -> Rule:
    return Rule(category, (dict,), fields=fields, required=tuple(required))


def array(item: Rule, *, category: str = STRUCTURE) -> Rule:
    return Rule(category, (list,), item=item)


S = scalar(SEMANTIC, str)
ID = scalar(COORDINATE, str)
TIME = scalar(COORDINATE, str, int)
BOOL = scalar(STRUCTURE, bool)
NUMBER = scalar(ACCOUNTING, int, float)
DISCRIMINATOR = scalar(STRUCTURE, str)


class Chain:
    def __init__(self, domain: str) -> None:
        self.domain = ("session-migrate/native-audit/v1/" + domain + "\0").encode()
        self.root = hashlib.sha256(self.domain).digest()
        self.count = 0

    def add(self, value: object) -> None:
        self.root = hashlib.sha256(
            self.domain + self.count.to_bytes(8, "big") + self.root + _encoded(value)
        ).digest()
        self.count += 1

    def witness(self) -> SequenceWitness:
        return SequenceWitness(self.count, self.root.hex())


@dataclass(slots=True, repr=False)
class Taxonomy:
    atoms: list[AuditAtom] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    issues: list[AuditIssue] = field(default_factory=list)
    issue_counts: Counter[str] = field(default_factory=Counter)
    structure: Chain = field(default_factory=lambda: Chain("structure"))
    classification: Chain = field(default_factory=lambda: Chain("classification"))
    excluded_records: set[int] = field(default_factory=set)
    loss_records: set[int] = field(default_factory=set)

    def issue(self, code: str, record_index: int) -> None:
        self.issue_counts[code] += 1
        if len(self.issues) < MAX_DIAGNOSTICS:
            self.issues.append(AuditIssue(code, record_index))

    def reclassify_records(self, records: set[int], category: str) -> None:
        self.atoms = [
            AuditAtom(a.index, a.record_index, a.path, category, a.sha256)
            if a.record_index in records and a.category in {SEMANTIC, LOSS}
            else a
            for a in self.atoms
        ]
        self.counts = Counter(a.category for a in self.atoms)
        self.classification = Chain("classification")
        for atom in self.atoms:
            self.classification.add(
                [atom.index, atom.record_index, list(atom.path), atom.category, atom.sha256]
            )

    def walk(
        self,
        value: Any,
        rule: Rule,
        record_index: int,
        path: tuple[str | int, ...] = (),
        *,
        override: str | None = None,
    ) -> None:
        if rule.factory is not None:
            rule = rule.factory(value)
        category = override or rule.category
        valid = not rule.types or type(value) in rule.types
        if not valid:
            category = UNKNOWN
            self.issue("invalid_field_type", record_index)
        atom = AuditAtom(len(self.atoms), record_index, path, category, digest(value))
        self.atoms.append(atom)
        self.counts[category] += 1
        self.classification.add([atom.index, record_index, list(path), category, atom.sha256])
        if category == UNKNOWN:
            self.issue("unknown_atom", record_index)
        if category == LOSS:
            self.loss_records.add(record_index)
        if rule.whole or not valid or override is not None:
            children = (
                value.items()
                if type(value) is dict
                else enumerate(value)
                if type(value) is list
                else ()
            )
            for key, child in children:
                self.walk(child, subtree(category), record_index, (*path, key), override=category)
            return
        if type(value) is dict:
            fields = rule.fields or {}
            for key in rule.required:
                if key not in value:
                    self.issue("missing_required_field", record_index)
            for key, child in value.items():
                child_rule = fields.get(key)
                if child_rule is None:
                    self.issue("unknown_field", record_index)
                    child_rule = subtree(UNKNOWN)
                self.walk(child, child_rule, record_index, (*path, key))
        elif type(value) is list:
            for index, child in enumerate(value):
                self.walk(child, rule.item or subtree(UNKNOWN), record_index, (*path, index))


@dataclass(slots=True, repr=False)
class AdapterResult:
    taxonomy: Taxonomy
    selected: tuple[int, ...]
    schema_id: str
    client_id: str | None
    session: Session | None
