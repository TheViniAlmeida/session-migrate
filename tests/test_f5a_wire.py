"""F5A wire extensions preserve old bytes and never certify full preservation."""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_lineage import completion, operation
from test_witnesses import v2_completion, v2_operation

from session_migrate.lineage import (
    MAX_UNITS,
    CompletionReceipt,
    CompletionRecord,
    DerivationBindingV1,
    DerivedInputIdentityV1,
    DerivedVerificationV1,
    EdgeRecord,
    LineageError,
    LineageStore,
    OmissionV1,
    OperationRecord,
    ReturnLinkV1,
    SelectionRangeV1,
    TransformationV1,
    _decode,
    _encode,
)


def transformation():
    return TransformationV1(
        1,
        "user_handoff",
        "user-handoff-v1",
        DerivedInputIdentityV1("user_handoff", "1" * 64, "2" * 64),
        (SelectionRangeV1(0, 1, None),),
        (OmissionV1("tool_call", 3),),
        "user_supplied",
    )


def derived(root):
    record = replace(operation(root), transformation=transformation())
    done = completion(root, record)
    proof = DerivedVerificationV1(
        1,
        hashlib.sha256(_encode(record.transformation)).hexdigest(),
        "2" * 64,
        record.expected_projection.snapshot_sha256,
        done.target_checkpoint.artifact_sha256,
        "user-handoff-v1",
    )
    return record, replace(done, derived_verification=proof)


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@pytest.mark.parametrize("nested", (False, True))
def test_legacy_records_without_f5a_extensions_omit_them_even_with_nested_v2(nested):
    root = Path("/synthetic")
    record = v2_operation(root) if nested else operation(root)
    done = v2_completion(root, record) if nested else completion(root, record)
    receipt, edge = LineageStore._completion(None, record, done)
    for item in (record, done, receipt, edge):
        document = json.loads(_encode(item))
        assert "transformation" not in document and "return_link" not in document
        assert "derivation" not in document and "derived_verification" not in document
        assert "record_schema_version" not in document
        assert _decode(type(item), _encode(item)) == item
    assert receipt.continuity_reason == "tracked" and edge.kind == "FULL"


def test_derived_roundtrip_completes_as_derived_with_two_individual_checkpoints(tmp_path):
    record, done = derived(tmp_path)
    receipt, edge = LineageStore._completion(None, record, done)
    assert receipt.continuity_reason == "derived_handoff" and edge.kind == "DERIVED"
    assert (
        receipt.derivation
        == edge.derivation
        == DerivationBindingV1(
            1,
            record.source.identity,
            record.transformation.input,
            hashlib.sha256(_encode(record.transformation)).hexdigest(),
        )
    )
    for item in (record, done, receipt, edge):
        document = json.loads(_encode(item))
        assert document["record_schema_version"] == 2
        assert _decode(type(item), _encode(item)) == item
        assert "document_bytes" not in document
    path = tmp_path / "ledger"
    with (
        LineageStore.create_new(path) as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        store.begin_publication(record.operation_id, guard)
        store.mark_attempted(record.operation_id, guard)
        store.mark_verifying(record.operation_id, guard)
        assert store.complete(record.operation_id, done, guard) == receipt
    reopened = LineageStore.inspect_existing(path)
    assert reopened.operations[0].receipt == receipt and reopened.edges == (edge,)


@pytest.mark.parametrize(
    "name",
    (
        "transformation_sha256",
        "input_event_sha256",
        "expected_target_snapshot_sha256",
        "observed_target_authority_sha256",
    ),
)
def test_derived_completion_rejects_mismatched_verification_metadata(name):
    record, done = derived(Path("/synthetic"))
    bad = replace(done, derived_verification=replace(done.derived_verification, **{name: "f" * 64}))
    with pytest.raises(LineageError, match="proof_mismatch"):
        LineageStore._completion(None, record, bad)


@pytest.mark.parametrize(
    "change", ("missing_proof", "missing_source", "missing_target", "full_proof")
)
def test_derived_proof_cannot_create_full_tracked_or_missing_identity_completion(change):
    record, done = derived(Path("/synthetic"))
    if change == "missing_proof":
        done = replace(done, derived_verification=None)
    elif change == "missing_source":
        done = replace(done, source_checkpoint=None)
    elif change == "missing_target":
        done = replace(done, target_checkpoint=None)
    else:
        record = replace(record, transformation=None)
    with pytest.raises(LineageError, match="proof_mismatch"):
        LineageStore._completion(None, record, done)


@pytest.mark.parametrize("mode", ("full", "derived"))
def test_return_link_has_exact_source_identity_and_is_mirrored(mode):
    record, done = derived(Path("/synthetic"))
    if mode == "full":
        record = replace(record, transformation=None)
        done = replace(done, derived_verification=None)
    link = ReturnLinkV1(1, "parent-operation", "e" * 64, record.source.identity)
    record = replace(record, return_link=link)
    receipt, edge = LineageStore._completion(None, record, done)
    assert receipt.return_link == edge.return_link == link
    assert _decode(OperationRecord, _encode(record)) == record
    with pytest.raises(LineageError):
        replace(record, return_link=replace(link, parent_target_identity=done.target_identity))
    with pytest.raises(LineageError):
        replace(record, return_link=replace(link, parent_operation_id=record.operation_id))


@pytest.mark.parametrize("kind", (OperationRecord, CompletionRecord, CompletionReceipt, EdgeRecord))
@pytest.mark.parametrize(
    "change", ("no_marker", "unknown", "marker_bool", "empty_extensions", "missing_field")
)
def test_extension_codec_is_closed_canonical_and_marker_required(kind, change):
    record, done = derived(Path("/synthetic"))
    receipt, edge = LineageStore._completion(None, record, done)
    item = {
        OperationRecord: record,
        CompletionRecord: done,
        CompletionReceipt: receipt,
        EdgeRecord: edge,
    }[kind]
    document = json.loads(_encode(item))
    if change == "no_marker":
        document.pop("record_schema_version")
    elif change == "unknown":
        document["caller_authority"] = "not-proof"
    elif change == "marker_bool":
        document["record_schema_version"] = True
    elif change == "missing_field":
        document.pop("derived_verification" if kind is CompletionRecord else "return_link")
    else:
        for name in ("transformation", "derivation", "derived_verification", "return_link"):
            if name in document:
                document[name] = None
    with pytest.raises(LineageError):
        _decode(kind, wire(document))


@pytest.mark.parametrize(
    "change",
    (
        "bool_range",
        "reverse_range",
        "duplicate_omission",
        "unknown_kind",
        "over_ranges",
        "over_count",
        "schema",
        "input_kind",
    ),
)
def test_new_wire_types_have_exact_enums_counts_and_caps(change):
    value = json.loads(_encode(transformation()))
    if change == "bool_range":
        value["selection"][0]["first_event"] = True
    elif change == "reverse_range":
        value["selection"][0]["first_event"] = 2
    elif change == "duplicate_omission":
        value["omissions"] *= 2
    elif change == "unknown_kind":
        value["omissions"][0]["kind"] = "unknown"
    elif change == "over_ranges":
        value["selection"] *= MAX_UNITS + 1
    elif change == "over_count":
        value["omissions"][0]["count"] = 2**31
    elif change == "schema":
        value["schema_version"] = 2
    else:
        value["input"]["kind"] = "native"
    with pytest.raises(LineageError):
        _decode(TransformationV1, wire(value))


def test_duplicate_and_noncanonical_fields_do_not_issue_any_metadata():
    encoded = _encode(transformation())
    with pytest.raises(LineageError):
        _decode(TransformationV1, b'{"schema_version":1,' + encoded[1:])
    with pytest.raises(LineageError):
        _decode(TransformationV1, json.dumps(json.loads(encoded), indent=2).encode())
    value = copy.deepcopy(json.loads(encoded))
    value["input"]["schema_version"] = 1
    with pytest.raises(LineageError):
        _decode(TransformationV1, wire(value))
