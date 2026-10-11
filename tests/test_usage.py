"""Issued RAW observations over actual B3 private file publication."""

import json
from dataclasses import replace
from itertools import permutations

import pytest
import test_continuity as fixtures

from session_migrate.continuity_observation import EndpointObserver
from session_migrate.profiles import SessionRef
from session_migrate.usage import (
    CounterObservationV1,
    ImportedPrefixBinding,
    UsageCapture,
    UsageError,
    UsageLimits,
    aggregate_usage,
    capture_usage,
    classify_origin,
    extract_usage,
    issue_imported_prefix_binding,
)

BODY = "PRIVATE_USAGE_TRANSCRIPT_BODY"


@pytest.fixture
def selected(tmp_path):
    catalog, _, ledger, operation, env, profiles, source = fixtures.actual_b3_fixture(
        tmp_path, "profiles"
    )
    rows = [json.loads(line) for line in source.read_bytes().splitlines()]
    rows[-1]["requestId"] = "req_observed"
    rows[-1]["message"].update(
        {
            "id": "msg_observed",
            "model": "fixture-model",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 3,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
    )
    rows[0]["message"]["content"][0]["text"] = BODY
    fixtures.write_records(source, rows)
    observer = EndpointObserver(catalog, profiles, base_environ=env, byte_limit=1024 * 1024)
    profile = profiles[0]
    ref = SessionRef(
        profile.format,
        profile.root,
        fixtures.SOURCE_ID,
        profile.id,
        profile_fingerprint=profile.fingerprint,
    )
    try:
        yield observer, profile, ref, source, operation
    finally:
        catalog.close()


def captured(selected, **kwargs):
    observer, profile, ref, *_ = selected
    return capture_usage(ref, profile, observer, UsageLimits(), client_version="2.1.209", **kwargs)


def test_conflicting_observation_multiset_has_no_order_preference(selected):
    first = captured(selected)
    sample_a = extract_usage(first).samples_page[0]
    first.close()
    source = selected[3]
    rows = [json.loads(line) for line in source.read_bytes().splitlines()]
    rows[-1]["message"]["usage"]["input_tokens"] = 13
    fixtures.write_records(source, rows)
    second = captured(selected)
    try:
        sample_b = extract_usage(second).samples_page[0]
        assert sample_a.sample_key == sample_b.sample_key
        results = [aggregate_usage(order) for order in permutations((sample_a, sample_a, sample_b))]
        assert all(result == results[0] for result in results)
        reasons = dict(results[0].excluded_counts_by_reason)
        assert reasons["identity_ambiguous"] == 3
        assert "duplicate_snapshot" not in reasons
        assert all(
            value is None for group in results[0].groups for _, value in group.dimension_totals
        )
        identical = aggregate_usage((sample_a, sample_a, sample_a))
        assert dict(identical.excluded_counts_by_reason)["duplicate_snapshot"] == 2
    finally:
        second.close()


def test_actual_capture_metadata_has_no_body_or_native_consumption(selected):
    capture = captured(selected)
    try:
        report = extract_usage(capture)
        assert report.sample_count == 1
        sample = report.samples_page[0]
        assert dict(sample.counters)["input"].value == 12
        assert dict(sample.counters)["output"].quality == "recorded"
        assert sample.origin == "unknown" and sample.epoch is None
        assert dict(report.aggregate.groups[0].dimension_totals)["input"] is None
        assert BODY not in json.dumps(report.to_dict()) + repr(capture) + repr(report)
        assert not hasattr(capture, "snapshot")
        assert classify_origin(capture, report.samples_page) == report.samples_page
    finally:
        capture.close()


@pytest.mark.parametrize("bad", [True, 1.0, -1, 2**63, "PRIVATE_COUNTER_BODY"])
def test_invalid_dimension_does_not_remove_valid_counter(selected, bad):
    source = selected[3]
    rows = [json.loads(line) for line in source.read_bytes().splitlines()]
    rows[-1]["message"]["usage"]["input_tokens"] = bad
    fixtures.write_records(source, rows)
    capture = captured(selected)
    try:
        sample = extract_usage(capture).samples_page[0]
        assert dict(sample.counters)["input"].value is None
        assert dict(sample.counters)["input"].reason == "invalid_counter"
        assert dict(sample.counters)["output"].value == 3
    finally:
        capture.close()


def test_append_keeps_logical_key_not_capture_hash(selected):
    first = captured(selected)
    previous = extract_usage(first).samples_page
    source = selected[3]
    rows = [json.loads(line) for line in source.read_bytes().splitlines()]
    addition = dict(rows[0])
    addition["uuid"] = "synthetic-appended"
    addition["parentUuid"] = rows[-1]["uuid"]
    fixtures.write_records(source, [*rows, addition])
    second = captured(selected)
    try:
        current = extract_usage(second).samples_page
        assert first.revision.authority_sha256 != second.revision.authority_sha256
        assert previous[0].sample_key == current[0].sample_key
        aggregate = aggregate_usage((*previous, *current))
        assert dict(aggregate.excluded_counts_by_reason)["duplicate_snapshot"] == 1
        assert all(
            value is None for group in aggregate.groups for _, value in group.dimension_totals
        )
        with pytest.raises(UsageError):
            extract_usage(first)
    finally:
        first.close()
        second.close()


def test_fake_capture_and_sample_and_import_history_never_gain_authority(selected):
    with pytest.raises(UsageError):
        UsageCapture()
    with pytest.raises(UsageError):
        extract_usage(object.__new__(UsageCapture))
    capture = captured(selected)
    try:
        sample = extract_usage(capture).samples_page[0]
        forged = replace(sample, origin="native")
        with pytest.raises(UsageError):
            aggregate_usage((forged,))
        with pytest.raises(UsageError):
            ImportedPrefixBinding()
        operation = selected[4]
        with pytest.raises(UsageError, match="imported_accounting_unproven"):
            issue_imported_prefix_binding(
                operation.receipt,
                (operation.source_checkpoint, operation.target_checkpoint),
                capture,
                (selected[1],),
            )
        with pytest.raises(UsageError, match="imported_accounting_unproven"):
            classify_origin(capture, (sample,), binding={"history": "imported"})
    finally:
        capture.close()


def test_wrong_client_profile_and_ttl_fail_fixed(selected, monkeypatch):
    observer, profile, ref, *_ = selected
    with pytest.raises(UsageError):
        capture_usage(ref, profile, observer, client_version="2.1.210")
    with pytest.raises(UsageError):
        capture_usage(ref, replace(profile, name="Changed"), observer, client_version="2.1.209")
    capture = captured(selected)
    with monkeypatch.context() as patch:
        patch.setattr("session_migrate.usage.monotonic_ns", lambda: 2**63)
        with pytest.raises(UsageError, match="usage_capture_expired"):
            extract_usage(capture)
    with pytest.raises(UsageError):
        extract_usage(capture)


def test_closed_capture_and_page_cursor_do_not_reconstruct_authority(selected):
    capture = captured(selected)
    report = extract_usage(capture, limit=1)
    with pytest.raises(UsageError, match="refresh_required"):
        extract_usage(capture, cursor="forged")
    assert report.aggregate == extract_usage(capture, offset=1).aggregate
    capture.close()
    with pytest.raises(UsageError):
        extract_usage(capture)


@pytest.mark.parametrize("bad", [True, -1, 2**63])
def test_counter_dto_strict_and_limits(bad):
    with pytest.raises(UsageError):
        CounterObservationV1(bad, "recorded", "usage-raw-fields-v1", None)
    with pytest.raises(UsageError):
        UsageLimits(samples=bad)


def test_physical_catalog_replacement_never_uses_cached_selection(selected, tmp_path, monkeypatch):
    import sqlite3

    observer, _, _, _, _ = selected
    path = observer._catalog.path
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    with sqlite3.connect(replacement) as connection:
        connection.execute("UPDATE sessions SET canonical_path='/outside/private/source'")
    replacement.replace(path)

    def forbidden(*args, **kwargs):
        raise AssertionError("stale physical selection reached native reading")

    monkeypatch.setattr("session_migrate.usage.observe_endpoint", forbidden)
    with pytest.raises(UsageError):
        captured(selected)


def test_changed_catalog_invalidates_live_capture(selected):
    import os

    capture = captured(selected)
    path = selected[0]._catalog.path
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    try:
        with pytest.raises(UsageError):
            extract_usage(capture)
    finally:
        capture.close()


def test_same_actual_capture_borrowed_once_and_no_body_mutation(selected, monkeypatch):
    import session_migrate.usage as module

    calls = []
    real_observe = module.observe_endpoint

    def counted(*args, **kwargs):
        endpoint = real_observe(*args, **kwargs)
        calls.append(endpoint)
        return endpoint

    monkeypatch.setattr(module, "observe_endpoint", counted)
    source = selected[3]
    before = source.read_bytes(), source.stat().st_mode, source.stat().st_mtime_ns
    capture = captured(selected)
    try:
        extract_usage(capture)
        extract_usage(capture)
        assert len(calls) == 1
        assert (source.read_bytes(), source.stat().st_mode, source.stat().st_mtime_ns) == before
    finally:
        capture.close()


def test_source_change_inside_decoder_rejected_without_body_echo(selected, monkeypatch):
    import session_migrate.usage as module

    capture = captured(selected)
    original = module.decode_usage

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        source = selected[3]
        source.write_bytes(source.read_bytes().replace(BODY.encode(), b"Changed private body"))
        return result

    monkeypatch.setattr(module, "decode_usage", changed)
    try:
        with pytest.raises(UsageError) as caught:
            extract_usage(capture)
        assert BODY not in str(caught.value)
    finally:
        capture.close()


def test_body_and_cursor_metadata_caps_fail_without_truncation(selected):
    observer, profile, ref, *_ = selected
    with pytest.raises(UsageError):
        capture_usage(
            ref, profile, observer, UsageLimits(capture_bytes=1), client_version="2.1.209"
        )
    capture = capture_usage(
        ref, profile, observer, UsageLimits(metadata_bytes=1), client_version="2.1.209"
    )
    try:
        with pytest.raises(UsageError, match="usage_limit"):
            extract_usage(capture)
    finally:
        capture.close()


def test_counter_and_capture_mutation_cannot_gain_native_authority(selected):
    capture = captured(selected)
    try:
        sample = extract_usage(capture).samples_page[0]
        object.__setattr__(sample, "origin", "native")
        with pytest.raises(UsageError):
            aggregate_usage((sample,))
        object.__setattr__(capture.revision, "authority_sha256", "0" * 64)
        with pytest.raises(UsageError):
            extract_usage(capture)
    finally:
        capture.close()


def test_failed_capture_releases_reserved_capacity_and_declares_attempt(selected):
    import session_migrate.usage as module

    observer, profile, ref, *_ = selected
    before = module._RESERVED_BYTES, module._RESERVED_CAPTURES
    with pytest.raises(UsageError) as caught:
        capture_usage(
            ref, profile, observer, UsageLimits(capture_bytes=1), client_version="2.1.209"
        )
    assert before == (module._RESERVED_BYTES, module._RESERVED_CAPTURES)
    assert "file_capture_read_attempted" in caught.value.native_read_effects
    capture = captured(selected)
    try:
        assert before == (module._RESERVED_BYTES, module._RESERVED_CAPTURES)
        assert extract_usage(capture).sample_count == 1
    finally:
        capture.close()


def test_samples_cap_fails_whole_capture_not_partial_rows(selected):
    observer, profile, ref, source, _ = selected
    rows = [json.loads(line) for line in source.read_bytes().splitlines()]
    addition = json.loads(json.dumps(rows[-1]))
    addition["uuid"] = "second-assistant"
    addition["parentUuid"] = rows[-1]["uuid"]
    addition["requestId"] = "second-request"
    addition["message"]["id"] = "second-message"
    fixtures.write_records(source, [*rows, addition])
    capture = capture_usage(
        ref, profile, observer, UsageLimits(samples=1), client_version="2.1.209"
    )
    try:
        with pytest.raises(UsageError):
            extract_usage(capture)
    finally:
        capture.close()
