"""Exact semantic witnesses over synthetic data; no native stores or providers."""

import base64
import json
from dataclasses import asdict, replace

import pytest

from session_migrate.canonical import (
    MAX_DIAGNOSTICS,
    CanonicalError,
    CanonicalPolicy,
    compare_prefix,
    hash_events,
    iter_canonical_units,
)
from session_migrate.model import Event, EventKind, Provenance, Role
from session_migrate.witnesses import BoundaryKind

POLICY = CanonicalPolicy()


def event(text="synthetic", *, kind=EventKind.MESSAGE, role=Role.USER, payload=None, **kwargs):
    return Event(kind, Provenance(0), role, text=text, payload=payload or {}, **kwargs)


def root(events, policy=POLICY):
    return hash_events(events, policy).prefix_witness.units


def media_event(payload):
    return event(kind=EventKind.CONTEXT, payload={"block_type": "image", **payload})


def tool_pair(call_id="synthetic-call", **call_payload):
    return (
        event(
            None,
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="synthetic_tool",
            tool_call_id=call_id,
            payload={"input": {"count": 1}, **call_payload},
        ),
        event(
            "synthetic output",
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            tool_call_id=call_id,
            payload={"is_error": False},
        ),
    )


def compare(events, old, *, policy=POLICY, previous_policy=POLICY):
    return compare_prefix(
        iter_canonical_units(events, policy),
        old,
        previous_policy=previous_policy,
        current_policy=policy,
    )


def test_event_equality_does_not_hide_changed_payload():
    first = event(payload={"input": {"count": 1}})
    changed = replace(first, payload={"input": {"count": 2}})
    assert first == changed
    assert root([first]) != root([changed])


@pytest.mark.parametrize(
    "changed",
    [
        {"text": "synthetic "},
        {"text": "Synthetic"},
        {"role": Role.ASSISTANT},
        {"kind": EventKind.THINKING},
        {"tool_name": "different_tool"},
        {"payload": {"extra": "synthetic"}},
    ],
)
def test_full_event_semantics_change_root(changed):
    first = event()
    assert root([first]) != root([replace(first, **changed)])


def test_coordinates_are_not_semantics_but_unicode_is_exact():
    first = event("\u00e9")
    assert root([first]) == root(
        [replace(first, timestamp="synthetic-time", provenance=Provenance(50, block_index=9))]
    )
    assert root([first]) != root([replace(first, text="e\u0301")])


def test_object_key_order_is_neutral_but_array_order_and_types_are_exact():
    assert root([event(payload={"a": 1, "b": 2})]) == root([event(payload={"b": 2, "a": 1})])
    assert root([event(payload={"blocks": [1, 2]})]) != root([event(payload={"blocks": [2, 1]})])
    assert root([event(payload={"a": True})]) != root([event(payload={"a": 1})])
    assert root([event(payload={"a": 1.0})]) != root([event(payload={"a": 1})])


def test_logical_links_ignore_remapped_native_ids_and_detect_output_change():
    first = hash_events(tool_pair(), POLICY)
    remapped = hash_events(tool_pair("different-synthetic-id"), POLICY)
    assert first.prefix_witness == remapped.prefix_witness
    assert first.links_proven and remapped.links_proven
    assert first.prefix_witness.tool_links.count == 1
    changed = list(tool_pair())
    changed[1] = replace(changed[1], text="changed synthetic output")
    assert root(changed) != first.prefix_witness.units


def test_tool_namespace_error_and_input_order_are_semantic():
    pair = tool_pair(namespace="alpha")
    assert root(pair) != root(tool_pair(namespace="beta"))
    changed = [pair[0], replace(pair[1], payload={"is_error": True})]
    assert root(pair) != root(changed)
    assert root(tool_pair(input={"items": [1, 2]})) != root(tool_pair(input={"items": [2, 1]}))


@pytest.mark.parametrize(
    "call_namespace,result_namespace",
    [
        (True, 1),
        (1, 1.0),
        ([True], [1]),
        ({"nested": [True]}, {"nested": [1]}),
        ({"nested": {"value": 1}}, {"nested": {"value": 1.0}}),
        ("synthetic", None),
    ],
)
def test_explicit_tool_result_namespace_claim_preserves_json_types_and_null(
    call_namespace, result_namespace
):
    call, result = tool_pair(namespace=call_namespace)
    result = replace(result, payload={**result.payload, "namespace": result_namespace})
    witness = hash_events([call, result], POLICY)
    assert witness.blocking and not witness.links_proven
    assert dict(witness.issue_counts)["tool_identity_mismatch"] == 1
    assert witness.prefix_witness.tool_links.count == 0


def test_absent_tool_result_namespace_can_bind_but_explicit_null_is_a_claim():
    for namespace in ("synthetic", None, True, {"nested": [1.0]}):
        call, result = tool_pair(namespace=namespace)
        assert hash_events([call, result], POLICY).links_proven
        claimed = replace(result, payload={**result.payload, "namespace": namespace})
        assert hash_events([call, claimed], POLICY).links_proven
    call, result = tool_pair()
    assert hash_events([call, result], POLICY).links_proven
    explicit_null = replace(result, payload={**result.payload, "namespace": None})
    witness = hash_events([call, explicit_null], POLICY)
    assert witness.blocking and not witness.links_proven
    assert witness.prefix_witness.tool_links.count == 0


def test_business_ids_in_payload_are_not_silently_dropped():
    assert root(tool_pair(input={"id": "synthetic-a"})) != root(
        tool_pair(input={"id": "synthetic-b"})
    )


def test_tool_arguments_that_resemble_media_remain_exact_json():
    first = tool_pair(
        input={
            "image_url": "data:image/png;base64,eA==",
            "nested": {"type": "image", "url": "https://synthetic.invalid/a"},
        }
    )
    second = tool_pair(
        input={
            "image_url": "data:image/png;base64,eB==",
            "nested": {"type": "image", "url": "https://synthetic.invalid/a"},
        }
    )
    assert base64.b64decode("eA==") == base64.b64decode("eB==")
    assert root(first) != root(second)
    assert not hash_events(first, POLICY).blocking
    assert not hash_events(second, POLICY).blocking
    assert root([event(payload={"image_url": "data:image/png;base64,eA=="})]) != root(
        [event(payload={"image_url": "data:image/png;base64,eB=="})]
    )


def test_multiple_calls_and_swapped_result_links_change_roots():
    a, ar = tool_pair("synthetic-a")
    b, br = tool_pair("synthetic-b")
    first = hash_events([a, b, ar, br], POLICY)
    second = hash_events([a, b, br, ar], POLICY)
    assert first.links_proven and second.links_proven
    assert first.prefix_witness.units != second.prefix_witness.units
    assert first.prefix_witness.tool_links != second.prefix_witness.tool_links


@pytest.mark.parametrize(
    "events,expected",
    [
        ([tool_pair()[0]], "open_tool_call"),
        ([tool_pair()[1]], "orphan_tool_result"),
        ([*tool_pair(), tool_pair()[1]], "duplicate_tool_result"),
        ([tool_pair()[0], tool_pair()[0], tool_pair()[1]], "ambiguous_tool_result"),
        ([replace(tool_pair()[0], tool_call_id=None)], "missing_call_id"),
        ([replace(tool_pair()[0], tool_name=None), tool_pair()[1]], "missing_tool_name"),
        ([tool_pair()[0], replace(tool_pair()[1], tool_name="other")], "tool_identity_mismatch"),
        ([event(kind=EventKind.OPAQUE)], "opaque_event"),
    ],
)
def test_incomplete_or_ambiguous_semantics_are_explicit(events, expected):
    witness = hash_events(events, POLICY)
    assert expected in dict(witness.issue_counts)
    assert witness.blocking and not witness.links_proven
    assert witness.prefix_witness.boundary.kind is BoundaryKind.UNPROVEN


def test_duplicate_after_a_result_invalidates_link_proof():
    witness = hash_events([*tool_pair(), tool_pair()[0]], POLICY)
    assert dict(witness.issue_counts)["duplicate_call_id"] == 1
    assert not witness.links_proven


def test_hashing_ir_never_proves_turn_closure_or_native_coverage():
    witness = hash_events([event(role=Role.ASSISTANT)], POLICY)
    assert witness.prefix_witness.boundary.kind is BoundaryKind.UNPROVEN
    assert witness.prefix_witness.closed_turn_count == 0
    assert witness.prefix_witness.structure is None
    assert not hasattr(witness, "coverage")


def test_inline_media_hashes_decoded_bytes_and_mime_and_keeps_no_body():
    encoded = base64.b64encode(b"synthetic bytes").decode()
    image = media_event({"image_url": f"data:image/png;base64,{encoded}"})
    witness = hash_events([image], POLICY)
    assert not witness.blocking
    assert root([image]) != root(
        [
            replace(
                image,
                payload={
                    "block_type": "image",
                    "image_url": f"data:image/jpeg;base64,{encoded}",
                },
            )
        ]
    )
    assert root([image]) != root(
        [
            replace(
                image,
                payload={
                    "block_type": "image",
                    "image_url": "data:image/png;base64,eA==",
                },
            )
        ]
    )
    assert "synthetic bytes" not in repr(witness)
    assert encoded not in json.dumps(asdict(witness))
    assert root([media_event({"image_url": "data:image/png;base64,eA=="})]) == root(
        [media_event({"image_url": "data:image/png;base64,eB=="})]
    )


@pytest.mark.parametrize("mime", ["image/", "/png", "image/png\n", "image/ p", "é/png"])
def test_media_mime_rejects_controls_and_invalid_tokens(mime):
    with pytest.raises(CanonicalError):
        hash_events([media_event({"image_url": f"data:{mime};base64,eA=="})], POLICY)


def test_nested_base64_media_is_validated_without_dropping_metadata():
    first = event(
        kind=EventKind.TOOL_RESULT,
        role=Role.TOOL,
        tool_call_id="synthetic-call",
        payload={
            "content_blocks": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "eA==",
                    },
                }
            ]
        },
    )
    assert not hash_events([tool_pair()[0], first], POLICY).blocking
    changed = event(
        kind=EventKind.TOOL_RESULT,
        role=Role.TOOL,
        tool_call_id="synthetic-call",
        payload={
            "content_blocks": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "eQ==",
                    },
                }
            ]
        },
    )
    assert root([tool_pair()[0], first]) != root([tool_pair()[0], changed])


def test_media_urls_are_reference_only_and_block_native_proof():
    first = media_event({"image_url": "https://synthetic.invalid/a"})
    second = media_event({"image_url": "https://synthetic.invalid/b"})
    witness = hash_events([first], POLICY)
    assert witness.blocking and not witness.links_proven
    assert dict(witness.issue_counts) == {"external_media_reference": 1}
    assert root([first]) != root([second])
    assert "synthetic.invalid" not in json.dumps(asdict(witness))


def test_unsupported_media_carriers_keep_payload_but_block_proof():
    first = media_event({"source": {"type": "unknown", "data": "synthetic-a"}})
    second = media_event({"source": {"type": "unknown", "data": "synthetic-b"}})
    assert dict(hash_events([first], POLICY).issue_counts) == {"unsupported_media": 1}
    assert root([first]) != root([second])


def test_nested_media_metadata_is_not_invented_or_discarded():
    plain = media_event({"image_url": {"url": "data:image/png;base64,eA=="}})
    typed = media_event({"image_url": {"type": "image", "url": "data:image/png;base64,eA=="}})
    assert root([plain]) != root([typed])


@pytest.mark.parametrize(
    "payload",
    [
        {1: "invalid"},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": float("-inf")},
        {"value": {1, 2}},
        {"value": (1, 2)},
        {"value": b"synthetic"},
        {"value": object()},
        {"value": "\ud800"},
        {"\udfff": "value"},
        {"image_url": "data:image/png;base64,not valid"},
    ],
)
def test_invalid_json_and_media_fail_with_content_safe_error(payload):
    with pytest.raises(CanonicalError, match="^Invalid canonical input\\.$"):
        hash_events(
            [media_event(payload) if "image_url" in payload else event(payload=payload)], POLICY
        )


def test_cycles_and_invalid_text_are_rejected_safely():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(CanonicalError):
        hash_events([event(payload={"value": cycle})], POLICY)
    with pytest.raises(CanonicalError):
        hash_events([event("\ud800")], POLICY)


def test_long_payload_is_hashed_completely():
    first = event("x" * 100_000 + "a", payload={"value": "y" * 100_000 + "a"})
    assert root([first]) != root([replace(first, text="x" * 100_000 + "b")])
    assert root([first]) != root([replace(first, payload={"value": "y" * 100_000 + "b"})])


def test_order_and_repetitions_are_not_subsequence_matches():
    a, b = event("a"), event("b")
    assert root([a, a, b]) != root([a, b, b])
    old = hash_events([a, b], POLICY).prefix_witness
    assert compare([a, b], old).matches
    assert not compare([b, a, b], old).matches
    assert not compare([a, a, b], old).matches


def test_prefix_exact_extension_first_last_change_and_shortening():
    events = [event(str(index)) for index in range(4)]
    old = hash_events(events, POLICY).prefix_witness
    exact = compare(events, old)
    assert exact.matches and not exact.sequence_extends
    longer = compare(events + [event("extra")], old)
    assert longer.matches and longer.sequence_extends and longer.current_count == 5
    assert compare(events[:-1], old).reason == "shortened"
    for index in (0, 3):
        changed = events.copy()
        changed[index] = event("changed")
        assert compare(changed + [event("extra")], old).reason == "hash_mismatch"


def test_prefix_policy_compatibility_is_explicit_and_domain_separated():
    events = [event()]
    old = hash_events(events, POLICY).prefix_witness
    different = object.__new__(CanonicalPolicy)
    object.__setattr__(different, "algorithm", "canonical-v2")
    object.__setattr__(different, "policy_version", "exact-events-v2")
    assert different.sha256 != POLICY.sha256
    assert compare(events, old, previous_policy=None).reason == "policy_unproven"
    assert compare(events, old, previous_policy=different).reason == "policy_mismatch"
    with pytest.raises(CanonicalError):
        CanonicalPolicy(policy_version="exact-events-v2")
    with pytest.raises(CanonicalError):
        CanonicalPolicy(algorithm="canonical-v999")
    with pytest.raises(CanonicalError):
        hash_events(events, different)


def test_empty_prefix_requires_observed_current_policy():
    old = hash_events([], POLICY).prefix_witness
    assert compare_prefix([], old, previous_policy=POLICY).reason == "policy_unproven"
    assert compare([], old).matches
    assert compare([event()], old).sequence_extends


def test_tool_link_root_mismatch_and_ambiguous_units_cannot_match():
    pair = tool_pair()
    old = hash_events(pair, POLICY).prefix_witness
    fake_links = replace(old.tool_links, chain_sha256="a" * 64)
    assert compare(pair, replace(old, tool_links=fake_links)).reason == "hash_mismatch"
    ambiguous = [*pair, pair[1]]
    assert compare(ambiguous, old).reason == "semantic_issues"


def test_ten_thousand_units_have_complete_constant_size_roots_and_bounded_diagnostics():
    small = hash_events((event(str(i)) for i in range(1025)), POLICY)
    large = hash_events((event(str(i)) for i in range(10_000)), POLICY)
    assert small.prefix_witness.units.count == 1025
    assert large.prefix_witness.units.count == 10_000
    assert len(small.units) == len(large.units) == MAX_DIAGNOSTICS
    assert len(json.dumps(asdict(large.prefix_witness))) < 1024
    assert (
        abs(
            len(json.dumps(asdict(large.prefix_witness)))
            - len(json.dumps(asdict(small.prefix_witness)))
        )
        <= 4
    )
    extended = compare((event(str(i)) for i in range(10_000)), small.prefix_witness)
    assert extended.matches and extended.sequence_extends and extended.current_count == 10_000
    changed_last = (event("changed" if i == 9999 else str(i)) for i in range(10_000))
    assert root(changed_last) != large.prefix_witness.units
    assert (
        compare_prefix(
            large.units, small.prefix_witness, previous_policy=POLICY, current_policy=POLICY
        ).reason
        == "shortened"
    )


def test_issue_diagnostics_are_bounded_but_issue_counts_are_complete():
    witness = hash_events((event(kind=EventKind.OPAQUE) for _ in range(10_000)), POLICY)
    assert len(witness.issues) == MAX_DIAGNOSTICS
    assert dict(witness.issue_counts) == {"opaque_event": 10_000}
    assert witness.blocking and not witness.links_proven


def test_ten_thousand_tool_events_have_complete_link_roots():
    def history():
        for index in range(5000):
            yield from tool_pair(f"synthetic-{index}")

    witness = hash_events(history(), POLICY)
    assert witness.links_proven and not witness.blocking
    assert witness.prefix_witness.units.count == 10_000
    assert witness.prefix_witness.tool_links.count == 5000
    assert witness.prefix_witness.boundary.pending_call_count == 0
    assert len(witness.units) == MAX_DIAGNOSTICS
    assert len(json.dumps(asdict(witness.prefix_witness))) < 1024
    assert compare(history(), witness.prefix_witness).matches


def test_many_media_issues_in_one_unit_remain_bounded_and_counted_completely():
    witness = hash_events(
        [
            event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                tool_call_id="synthetic-call",
                payload={
                    "content_blocks": [
                        {"type": "image", "image_url": "https://synthetic.invalid/media"}
                        for _ in range(10_000)
                    ]
                },
            )
        ],
        POLICY,
    )
    assert len(witness.issues) == len(witness.units[0].issues) == MAX_DIAGNOSTICS
    assert dict(witness.issue_counts) == {
        "external_media_reference": 10_000,
        "orphan_tool_result": 1,
    }
    assert dict(witness.units[0].issue_counts) == {
        "external_media_reference": 10_000,
        "orphan_tool_result": 1,
    }
