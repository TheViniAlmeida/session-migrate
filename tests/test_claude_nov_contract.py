"""November reader support remains separate from completeness and session ancestry."""

from __future__ import annotations

import pytest
from test_claude_latest import _observe, _parse, _record

from session_migrate.formats import claude
from session_migrate.lineage import Coverage
from session_migrate.model import EventKind
from session_migrate.witnesses import BoundaryKind


@pytest.mark.parametrize("version", ["2.1.317", "2.1.318"])
def test_nov_exact_reader_keeps_historical_versions_and_writer_default(version):
    assert {
        "2.1.209",
        "2.1.303",
        "2.1.306",
        "2.1.317",
        "2.1.318",
    } <= claude.VALIDATED_CLAUDE_VERSIONS
    assert claude.PINNED_CLAUDE_VERSION == "2.1.209"
    rows = [
        _record("user", "u", None, "persistent user", version=version),
        _record("assistant", "a", "u", "persistent reply", version=version),
    ]
    audit, observation = _observe(rows)
    assert audit.client_id == version
    assert audit.coverage is Coverage.VERIFIED
    assert observation.boundary.kind is BoundaryKind.UNPROVEN
    assert observation.identity is None
    assert [event.text for event in _parse(rows).events] == ["persistent user", "persistent reply"]


@pytest.mark.parametrize("version", ["2.1.316", "2.1.319", "2.2.0"])
def test_nov_nearby_releases_do_not_inherit_exact_reader_authority(version):
    audit, observation = _observe([_record("user", "u", None, "request", version=version)])
    assert audit.client_id is None
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN


@pytest.mark.parametrize("previous", ["2.1.209", "2.1.303", "2.1.306"])
def test_nov_mixed_versions_remain_readable_with_unknown_authority(previous):
    rows = [
        _record("user", "u", None, "before", version=previous),
        _record("assistant", "a", "u", "after", version="2.1.318"),
    ]
    audit, observation = _observe(rows)
    assert [event.text for event in _parse(rows).events] == ["before", "after"]
    assert audit.client_id is None
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN


@pytest.mark.parametrize(
    "field", ["parentSessionId", "spawnedFrom", "forkedFromSessionId", "lineageDetached"]
)
def test_nov_unproven_session_metadata_does_not_become_message_ancestry(field):
    # These are deliberately unreviewed envelope fields. A message parent UUID
    # describes the conversation graph, never a persistent session parent.
    row = _record("user", "u", None, "request", version="2.1.318")
    row[field] = "UNREVIEWED_SESSION_RELATIONSHIP"
    audit, observation = _observe([row])
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "unknown_field" in dict(audit.issue_counts)
    assert [event.text for event in _parse([row]).events if event.kind is EventKind.MESSAGE] == [
        "request"
    ]
    assert observation.boundary.kind is BoundaryKind.UNPROVEN


@pytest.mark.parametrize("version", ["2.1.317", "2.1.318"])
@pytest.mark.parametrize("field", ["turnOrigin", "promptCacheRequested", "requestRef"])
def test_nov_stock_metadata_does_not_gain_fidelity_from_version_acceptance(version, field):
    row = _record("user", "u", None, "request", version=version)
    row[field] = "UNPROJECTED_METADATA_SENTINEL"
    audit, observation = _observe([row])
    assert audit.client_id == version
    assert audit.coverage is observation.coverage is Coverage.UNKNOWN
    assert "unknown_field" in dict(audit.issue_counts)
    assert [event.text for event in _parse([row]).events] == ["request"]
