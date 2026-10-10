"""Typed, content-free carriers and explicit backward-compatible record codecs."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from test_lineage import HASH, OTHER_HASH, checkpoint, completion, operation

from session_migrate.lineage import (
    INITIAL_CLOSE_BUDGET,
    MAX_METADATA_BYTES,
    MIN_CLOSE_BUDGET,
    CheckpointRecord,
    Coverage,
    LineageError,
    LineageStore,
    OperationState,
    ProjectionExpectation,
    SourceMetadata,
    _decode,
    _encode,
)
from session_migrate.witnesses import (
    BoundaryKind,
    BoundaryWitness,
    PrefixWitness,
    SequenceWitness,
    WitnessValidationError,
)


def prefix(count=10_000, kind=BoundaryKind.NATIVE_CLOSED, *, structure=True):
    return PrefixWitness(
        2,
        SequenceWitness(count, HASH),
        SequenceWitness(7, OTHER_HASH),
        SequenceWitness(31, "e" * 64) if structure else None,
        17,
        BoundaryWitness(kind, count, 0),
    )


def v2_operation(root, operation_id="v2-operation", native_id="v2-native"):
    record = operation(root, operation_id, native_id)
    return replace(
        record,
        source=replace(record.source, prefix_witness=prefix()),
        expected_projection=replace(
            record.expected_projection,
            unit_hashes=(),
            prefix_witness=replace(
                prefix(),
                units=SequenceWitness(20_000, OTHER_HASH),
                boundary=BoundaryWitness(BoundaryKind.IMPORTED_CLOSED, 20_000, 0),
            ),
            relation_sha256="f" * 64,
        ),
    )


def v2_completion(root, record):
    done = completion(root, record)
    source = replace(
        done.source_checkpoint,
        unit_hashes=(),
        tool_link_hashes=(),
        turn_count=17,
        prefix_witness=record.source.prefix_witness,
    )
    target = replace(
        done.target_checkpoint,
        unit_hashes=(),
        tool_link_hashes=(),
        turn_count=17,
        prefix_witness=record.expected_projection.prefix_witness,
    )
    return replace(done, source_checkpoint=source, target_checkpoint=target)


def strict_wire(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def test_v1_golden_bytes_captured_before_optional_fields_were_added():
    root = Path("/synthetic")
    record = operation(root)
    done = completion(root, record)
    receipt, _ = LineageStore._completion(None, record, done)
    baseline = {
        "source": (
            record.source,
            486,
            "d35115f82d50b5f86e331e7823c99cf9657301deda57390a70d874977ae38a36",
        ),
        "projection": (
            record.expected_projection,
            191,
            "f3d1cf49ad20a2f3d9757a3ba574594363992ee763a958f302a4c7ed5bfe4dbe",
        ),
        "checkpoint": (
            checkpoint(record.source.identity),
            768,
            "7fa039f71492f08b959e7b95aed8711624730e571bb6d6afc6f90048fbf980d0",
        ),
        "operation": (
            record,
            1304,
            "65c5cd68b3cbafccd61577371c796b16665c15f7e68f0f300d1c4d1fe90fdb7b",
        ),
        "completion": (
            done,
            1926,
            "c6872f3d1ef32442c009c1d6257825fca2a3bbb8085cd90faf2c18929aa82d44",
        ),
        "receipt": (
            receipt,
            613,
            "4ad21fda5b30ca0aa790ef2be3d9efec3965cfcd327a7907b601b42c3acb8cef",
        ),
    }
    for value, length, digest in baseline.values():
        encoded = _encode(value)
        assert len(encoded) == length
        assert hashlib.sha256(encoded).hexdigest() == digest
        assert _encode(_decode(type(value), encoded)) == encoded
        assert b"prefix_witness" not in encoded
        assert b"relation_sha256" not in encoded
        assert b"record_schema_version" not in encoded


def test_prefix_roundtrip_has_constant_storage_for_large_complete_collections():
    small = prefix(1025)
    large = prefix(2**31 - 1)
    assert _decode(PrefixWitness, _encode(small)) == small
    assert _decode(PrefixWitness, _encode(large)) == large
    assert len(_encode(large)) < 1024
    assert abs(len(_encode(large)) - len(_encode(small))) < 32
    assert large.units.count > 1024
    assert large.closed_turn_count == 17
    assert large.closed_turn_count != large.units.count
    assert "chain_sha256" not in repr(large)
    with pytest.raises(FrozenInstanceError):
        large.closed_turn_count = 1


@pytest.mark.parametrize("count", [True, False, -1, 2**31, 1.0, "1", None])
def test_counts_are_exact_bounded_integers(count):
    with pytest.raises(WitnessValidationError):
        SequenceWitness(count, HASH)
    with pytest.raises(WitnessValidationError):
        BoundaryWitness(BoundaryKind.UNPROVEN, count, 0)
    with pytest.raises(WitnessValidationError):
        replace(prefix(), closed_turn_count=count)


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "a" * 65, "z" * 64, "BODY_SENTINEL", None])
def test_chain_roots_are_strict_sha256(digest):
    with pytest.raises(WitnessValidationError) as caught:
        SequenceWitness(10_000, digest)
    assert "BODY_SENTINEL" not in str(caught.value)


def test_boundary_is_explicit_and_not_inferred_from_count():
    unproven = prefix(kind=BoundaryKind.UNPROVEN, structure=False)
    assert unproven.boundary.kind is BoundaryKind.UNPROVEN
    assert unproven.boundary.pending_call_count == 0
    assert unproven.structure is None
    earlier = replace(prefix(), boundary=BoundaryWitness(BoundaryKind.NATIVE_CLOSED, 100, 0))
    assert earlier.boundary.unit_offset < earlier.units.count
    with pytest.raises(WitnessValidationError):
        replace(prefix(), boundary=BoundaryWitness(BoundaryKind.NATIVE_CLOSED, 10_001, 0))
    with pytest.raises(WitnessValidationError):
        BoundaryWitness(BoundaryKind.NATIVE_CLOSED, 100, 1)
    with pytest.raises(WitnessValidationError):
        BoundaryWitness("native_closed", 100, 0)


@pytest.mark.parametrize("schema", [True, 1, 3, 2.0, "2", None])
def test_unknown_prefix_versions_fail_closed(schema):
    with pytest.raises(WitnessValidationError):
        replace(prefix(), schema_version=schema)
    payload = json.loads(_encode(prefix()))
    payload["schema_version"] = schema
    with pytest.raises(LineageError):
        _decode(PrefixWitness, strict_wire(payload))


@pytest.mark.parametrize("record_type", [SourceMetadata, ProjectionExpectation, CheckpointRecord])
def test_v2_exact_keysets_and_version_markers(record_type):
    record = v2_operation(Path("/synthetic"))
    done = v2_completion(Path("/synthetic"), record)
    value = {
        SourceMetadata: record.source,
        ProjectionExpectation: record.expected_projection,
        CheckpointRecord: done.target_checkpoint,
    }[record_type]
    encoded = _encode(value)
    wire = json.loads(encoded)
    assert wire["record_schema_version"] == 2
    assert _decode(record_type, encoded) == value
    for invalid in (
        dict(wire, record_schema_version=3),
        dict(wire, record_schema_version=True),
        dict(wire, body="SYNTHETIC_BODY_SENTINEL"),
        {key: item for key, item in wire.items() if key != "record_schema_version"},
        {key: item for key, item in wire.items() if key != "prefix_witness"},
    ):
        with pytest.raises(LineageError) as caught:
            _decode(record_type, strict_wire(invalid))
        assert "SYNTHETIC_BODY_SENTINEL" not in str(caught.value)


def test_empty_optional_extensions_cannot_disguise_v1_as_v2():
    wire = json.loads(_encode(operation(Path("/synthetic")).expected_projection))
    wire.update(record_schema_version=2, prefix_witness=None, relation_sha256=None)
    with pytest.raises(LineageError):
        _decode(ProjectionExpectation, strict_wire(wire))


def test_unknown_semantics_do_not_gain_coverage_or_boundary_proof():
    unknown = ProjectionExpectation(None, (), Coverage.UNKNOWN)
    assert unknown.prefix_witness is None
    assert unknown.relation_sha256 is None
    for extra in ({"prefix_witness": prefix()}, {"relation_sha256": HASH}):
        with pytest.raises(LineageError):
            replace(unknown, **extra)
    with pytest.raises(LineageError):
        SourceMetadata(None, HASH, HASH, None, prefix())
    cp = replace(
        checkpoint(None),
        snapshot_sha256=None,
        unit_hashes=(),
        tool_link_hashes=(),
        boundary_closed=False,
        coverage=Coverage.UNKNOWN,
    )
    with pytest.raises(LineageError):
        replace(cp, prefix_witness=prefix())


def test_v2_never_silently_retains_sampled_v1_arrays():
    with pytest.raises(LineageError):
        replace(operation(Path("/synthetic")).expected_projection, prefix_witness=prefix())
    with pytest.raises(LineageError):
        replace(checkpoint(None), prefix_witness=prefix())
    cp = replace(
        checkpoint(None),
        unit_hashes=(),
        tool_link_hashes=(),
        turn_count=17,
        prefix_witness=prefix(),
    )
    with pytest.raises(LineageError):
        replace(cp, boundary_closed=False)
    cp = replace(cp, prefix_witness=prefix(kind=BoundaryKind.UNPROVEN), boundary_closed=False)
    assert cp.coverage is Coverage.VERIFIED  # An explicit caller declaration, not hashing.
    assert cp.prefix_witness.boundary.kind is BoundaryKind.UNPROVEN


def test_mixed_v1_and_v2_records_recover_without_sql_schema_migration(tmp_path):
    with LineageStore.create_new(tmp_path / "state") as store:
        records = [operation(tmp_path, "v1-operation", "v1-native"), v2_operation(tmp_path)]
        for record in records:
            done = (
                completion(tmp_path, record)
                if record.source.prefix_witness is None
                else v2_completion(tmp_path, record)
            )
            with store.lock_operation(record.operation_id, record.target_namespace) as guard:
                store.ready(record, guard)
                store.begin_publication(record.operation_id, guard)
                store.mark_verifying(record.operation_id, guard)
                store.complete(record.operation_id, done, guard)
        before = store.recovery_view()
        assert len(before.edges) == 2
        assert before.operations[0].source_checkpoint.prefix_witness is None
        assert before.operations[1].source_checkpoint.prefix_witness.units.count == 10_000
    with LineageStore.open_existing(tmp_path / "state") as store:
        assert store.recovery_view() == before
    assert all(entry.state is OperationState.COMPLETED for entry in before.operations)


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize(
    "changed_field", ["units", "links", "structure", "closed_turns", "boundary"]
)
def test_completion_compares_the_entire_prepared_prefix_per_side(tmp_path, side, changed_field):
    record = v2_operation(tmp_path)
    done = v2_completion(tmp_path, record)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        store.begin_publication(record.operation_id, guard)
        store.mark_verifying(record.operation_id, guard)
        cp = getattr(done, side + "_checkpoint")
        witness = cp.prefix_witness
        changed = {
            "units": replace(witness, units=replace(witness.units, chain_sha256="f" * 64)),
            "links": replace(witness, tool_links=replace(witness.tool_links, count=8)),
            "structure": replace(
                witness, structure=replace(witness.structure, chain_sha256="f" * 64)
            ),
            "closed_turns": replace(witness, closed_turn_count=16),
            "boundary": replace(
                witness, boundary=BoundaryWitness(BoundaryKind.UNPROVEN, witness.units.count, 0)
            ),
        }[changed_field]
        wrong = replace(
            cp,
            prefix_witness=changed,
            boundary_closed=changed.boundary.kind is not BoundaryKind.UNPROVEN,
        )
        bad = replace(done, **{side + "_checkpoint": wrong})
        with pytest.raises(LineageError, match="proof_mismatch"):
            store.complete(record.operation_id, bad, guard)
        assert store.recovery_view().operations[0].durable_state is OperationState.VERIFYING
        store.complete(record.operation_id, done, guard)


@pytest.mark.parametrize("side", ["source", "target"])
def test_policy_versions_are_still_compared_for_prefix_records(tmp_path, side):
    record = v2_operation(tmp_path)
    done = v2_completion(tmp_path, record)
    cp = getattr(done, side + "_checkpoint")
    mismatch = replace(cp, versions=replace(cp.versions, canonical_algorithm="different-v2"))
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        store.begin_publication(record.operation_id, guard)
        store.mark_verifying(record.operation_id, guard)
        with pytest.raises(LineageError, match="proof_mismatch"):
            store.complete(
                record.operation_id, replace(done, **{side + "_checkpoint": mismatch}), guard
            )


def test_long_prefix_is_bounded_and_preserves_reserved_finalization_budget(tmp_path):
    record = v2_operation(tmp_path)
    done = v2_completion(tmp_path, record)
    assert len(_encode(record)) < 10_000
    assert len(_encode(done.source_checkpoint)) < MAX_METADATA_BYTES
    assert len(_encode(done.target_checkpoint)) < MAX_METADATA_BYTES
    with (
        LineageStore.create_new(tmp_path / "state", max_image_bytes=570_000) as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        assert store.recovery_view().operations[0].reserved_bytes == INITIAL_CLOSE_BUDGET
        store.begin_publication(record.operation_id, guard)
        store.mark_verifying(record.operation_id, guard)
        assert store.recovery_view().operations[0].reserved_bytes >= MIN_CLOSE_BUDGET
        store.complete(record.operation_id, done, guard)
        assert store.recovery_view().operations[0].reserved_bytes == 0


def test_duplicate_keys_nonfinite_values_and_unknown_prefix_fields_rejected():
    wire = json.loads(_encode(prefix()))
    for payload in (
        strict_wire(dict(wire, request="BODY_SENTINEL")),
        _encode(prefix()).replace(b'"schema_version":2', b'"schema_version":2,"schema_version":2'),
        _encode(prefix()).replace(b'"count":10000', b'"count":NaN'),
    ):
        with pytest.raises(LineageError) as caught:
            _decode(PrefixWitness, payload)
        assert "BODY_SENTINEL" not in str(caught.value)


@pytest.mark.parametrize(
    "source_v2,target_v2", [(False, False), (True, False), (False, True), (True, True)]
)
def test_checkpoint_sides_can_use_different_codec_versions(tmp_path, source_v2, target_v2):
    legacy = operation(tmp_path)
    modern = v2_operation(tmp_path, legacy.operation_id, legacy.target_namespace.native_id)
    record = replace(
        legacy,
        source=modern.source if source_v2 else legacy.source,
        expected_projection=modern.expected_projection if target_v2 else legacy.expected_projection,
    )
    legacy_done = completion(tmp_path, legacy)
    modern_done = v2_completion(tmp_path, modern)
    done = replace(
        legacy_done,
        source_checkpoint=modern_done.source_checkpoint
        if source_v2
        else legacy_done.source_checkpoint,
        target_checkpoint=modern_done.target_checkpoint
        if target_v2
        else legacy_done.target_checkpoint,
    )
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        store.begin_publication(record.operation_id, guard)
        store.mark_verifying(record.operation_id, guard)
        store.complete(record.operation_id, done, guard)
        before = store.recovery_view()
    with LineageStore.open_existing(tmp_path / "state") as store:
        assert store.recovery_view() == before
    assert (before.operations[0].source_checkpoint.prefix_witness is not None) is source_v2
    assert (before.operations[0].target_checkpoint.prefix_witness is not None) is target_v2


def test_relation_only_metadata_has_an_explicit_version_without_promoting_coverage():
    projected = replace(
        operation(Path("/synthetic")).expected_projection,
        relation_sha256=HASH,
        coverage=Coverage.PARTIAL,
    )
    wire = json.loads(_encode(projected))
    assert wire["record_schema_version"] == 2
    assert wire["prefix_witness"] is None
    assert projected.coverage is Coverage.PARTIAL
    assert _decode(ProjectionExpectation, _encode(projected)) == projected
    with pytest.raises(LineageError):
        replace(projected, relation_sha256="BODY_SENTINEL")


def test_closed_turns_cannot_exceed_total_or_implicitly_close_a_partial_prefix():
    record = v2_operation(Path("/synthetic"))
    cp = v2_completion(Path("/synthetic"), record).source_checkpoint
    with pytest.raises(LineageError):
        replace(cp, turn_count=16)
    earlier = replace(
        cp.prefix_witness, boundary=BoundaryWitness(BoundaryKind.NATIVE_CLOSED, 100, 0)
    )
    with pytest.raises(LineageError):
        replace(cp, prefix_witness=earlier)
    partial = replace(cp, prefix_witness=earlier, boundary_closed=False)
    assert partial.prefix_witness.boundary.kind is BoundaryKind.NATIVE_CLOSED
    assert partial.boundary_closed is False


@pytest.mark.parametrize("component", ["units", "tool_links", "structure", "boundary"])
def test_unknown_components_are_rejected_without_payloads(component):
    with pytest.raises(WitnessValidationError) as caught:
        replace(prefix(), **{component: {"body": "SYNTHETIC_BODY_SENTINEL"}})
    assert "SYNTHETIC_BODY_SENTINEL" not in str(caught.value)


def test_pending_count_is_not_a_boolean_and_missing_structure_stays_absent():
    with pytest.raises(WitnessValidationError):
        BoundaryWitness(BoundaryKind.UNPROVEN, 100, True)
    carrier = prefix(structure=False)
    assert carrier.structure is None
    assert _decode(PrefixWitness, _encode(carrier)).structure is None


def test_v2_checkpoint_cannot_drop_prepared_source_or_target_witness(tmp_path):
    record = v2_operation(tmp_path)
    done = v2_completion(tmp_path, record)
    with (
        LineageStore.create_new(tmp_path / "state") as store,
        store.lock_operation(record.operation_id, record.target_namespace) as guard,
    ):
        store.ready(record, guard)
        store.begin_publication(record.operation_id, guard)
        store.mark_verifying(record.operation_id, guard)
        for side in ("source", "target"):
            removed = replace(getattr(done, side + "_checkpoint"), prefix_witness=None)
            with pytest.raises(LineageError, match="proof_mismatch"):
                store.complete(
                    record.operation_id, replace(done, **{side + "_checkpoint": removed}), guard
                )
