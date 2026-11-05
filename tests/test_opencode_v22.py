"""OpenCode 2.2.5 retains the 2.1 transfer contract and conservative RAW gates."""

import json

import pytest
from test_opencode_v21 import bundle, compaction, parse
from test_opencode_v21_audit import compacted, observe

from session_migrate.audit_adapters import LOSS, UNKNOWN
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode, opencode_v2
from session_migrate.lineage import Coverage
from session_migrate.model import EventKind
from session_migrate.witnesses import BoundaryKind


@pytest.mark.parametrize("release", ["2.2.5", "opencode v2.2.5", "2.2.6-custom"])
def test_v22_selects_flat_transfer_and_optional_recent(release, tmp_path):
    value = bundle(tmp_path, release)
    assert opencode.is_v2(release)
    assert "location" in value["info"]
    assert "recent" not in compaction(value)
    assert "native" not in compaction(value)
    assert opencode_v2.validated_version(release) == "2.2.5"
    assert any(event.kind == EventKind.COMPACTION for event in parse(tmp_path, value).events)


@pytest.mark.parametrize("release", ["2.3.0", "2.20.0", "1.19.5", "2.2", "2.2.5 garbage"])
def test_unexamined_series_stays_unsupported(release):
    assert not opencode.is_v2(release)


@pytest.mark.parametrize("busy", [None, True, -1, "1", [], 1.5])
def test_busy_time_is_validated_before_projection(tmp_path, busy):
    value = bundle(tmp_path, "2.2.5")
    value["info"]["time"]["busy"] = busy
    with pytest.raises(SessionMigrateError, match="native time"):
        parse(tmp_path, value)


def test_busy_runtime_coordinate_is_explicit_loss(tmp_path):
    value = bundle(tmp_path, "2.2.5")
    value["info"]["time"]["busy"] = value["info"]["time"]["created"]
    parsed = parse(tmp_path, value)
    assert ("opencode_v2_session_busy", 1) in parsed.losses
    _, audit, observed = observe(value, "2.2.5")
    assert observed.boundary.kind is BoundaryKind.UNPROVEN
    assert audit.coverage is Coverage.UNKNOWN
    atom = next(atom for atom in observed.atoms if atom.path == ("info", "time", "busy"))
    assert atom.category == LOSS


def test_exact_225_client_does_not_prove_native_identity():
    value = bundle_value()
    _, audit, observed = observe(value, "2.2.5")
    assert audit.client_id == "2.2.5"
    assert audit.coverage is Coverage.VERIFIED
    assert not audit.issue_counts
    assert observed.identity is None
    assert observed.boundary.kind is BoundaryKind.UNPROVEN
    assert observed.session.cli_version is None


def bundle_value():
    # No compaction or provider metadata: isolates the closed public grammar.
    from test_native_audit import opencode_bundle

    return opencode_bundle()


@pytest.mark.parametrize("client", ["2.2.4", "2.2.6", "2.2.5-custom", "opencode v2.2.5"])
def test_audit_requires_exact_validated_client(client):
    _, audit, observed = observe(bundle_value(), client)
    assert audit.client_id is None
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["unsupported_client_version"] == 1
    assert observed.identity is None


@pytest.mark.parametrize("kind", ["assistant", "compaction"])
def test_225_provider_checkpoint_stays_unknown_and_private(kind):
    value = compacted({"keepID": "msg_user"}) if kind == "compaction" else bundle_value()
    value["messages"][-1]["native"] = {"messages": [{"text": "PRIVATE_NATIVE_225"}]}
    _, audit, observed = observe(value, "2.2.5")
    assert audit.coverage is Coverage.UNKNOWN
    assert any("native" in atom.path and atom.category == UNKNOWN for atom in observed.atoms)
    assert "PRIVATE_NATIVE_225" not in repr(observed.events)
    assert observed.boundary.kind is BoundaryKind.UNPROVEN


def test_parent_reference_remains_unproved_raw_metadata(tmp_path):
    value = bundle_value()
    value["info"]["parentID"] = "ses_parent"
    parsed = parse(tmp_path, value)
    assert parsed.parent_session == "ses_parent"
    _, audit, observed = observe(value, "2.2.5")
    assert audit.coverage is Coverage.UNKNOWN
    assert (
        next(atom for atom in observed.atoms if atom.path == ("info", "parentID")).category
        == UNKNOWN
    )
    assert observed.boundary.kind is BoundaryKind.UNPROVEN
    assert observed.identity is None


def test_225_compaction_keeps_readable_tail_without_native_copy(tmp_path):
    value = bundle(tmp_path, "2.2.5")
    user = next(message for message in value["messages"] if message["type"] == "user")
    checkpoint = compaction(value)
    checkpoint["metadata"] = {"keepID": user["id"]}
    parsed = parse(tmp_path, value)
    summary = next(event for event in parsed.events if event.kind == EventKind.COMPACTION)
    assert user["text"] in summary.text
    assert checkpoint["summary"] in summary.text
    assert "native" not in json.dumps(checkpoint)
