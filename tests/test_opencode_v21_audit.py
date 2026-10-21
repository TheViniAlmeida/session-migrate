"""Synthetic 2.1 RAW audits preserve uncertainty about native checkpoints."""

import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from test_native_audit import opencode_bundle

from session_migrate.audit_adapters import LOSS, STRUCTURE, UNKNOWN
from session_migrate.canonical import CanonicalPolicy
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat
from session_migrate.native_audit import AuditPolicy, audit_snapshot, canonicalize_snapshot
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.witnesses import BoundaryKind


def observe(value, client="2.1.1"):
    snapshot = NativeSnapshot.from_bytes(
        json.dumps(value).encode(),
        context=SourceCaptureContext(AgentFormat.OPENCODE, Path("opencode:ses_fixture")),
    )
    audit = audit_snapshot(snapshot, AuditPolicy(client_version=client))
    observed = canonicalize_snapshot(snapshot, audit, CanonicalPolicy())
    return snapshot, audit, observed


def compacted(metadata):
    value = opencode_bundle()
    value["messages"].append(
        {
            "id": "msg_compaction",
            "type": "compaction",
            "time": {"created": 4, "completed": 5},
            "status": "completed",
            "reason": "auto",
            "model": copy.deepcopy(value["info"]["model"]),
            "summary": "portable summary",
            "metadata": metadata,
        }
    )
    return value


@pytest.mark.parametrize("client", ["2.0.22", "2.0.23", "2.1.1"])
def test_exact_supported_client_does_not_create_raw_version_or_native_identity(client):
    snapshot, audit, observed = observe(opencode_bundle(), client)
    assert audit.client_id == client
    assert audit.coverage is Coverage.VERIFIED
    assert not audit.issue_counts
    assert snapshot.parse().cli_version is None
    assert observed.session.cli_version is None
    assert observed.identity is None
    assert observed.boundary.kind is BoundaryKind.UNPROVEN


@pytest.mark.parametrize("client", [None, "2.1.2", "2.1.1-custom", "opencode v2.1.1"])
def test_raw_shape_never_proves_an_unknown_client(client):
    _, audit, _ = observe(opencode_bundle(), client)
    assert audit.client_id is None
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["unsupported_client_version"] == 1


@pytest.mark.parametrize("metadata", [{"keepID": "msg_user"}, {"recent": "kept context"}])
def test_compaction_metadata_is_audited_without_proving_a_boundary(metadata):
    _, audit, observed = observe(compacted(metadata))
    # The RAW fields have closed rules, but the parser's metadata-loss carrier
    # has no independent recovery rule. Do not turn that gap into a certificate.
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["compaction_unproven"] == 1
    assert dict(audit.issue_counts)["unrecoverable_opaque"] == 1
    assert not dict(audit.classification_counts).get(UNKNOWN)
    assert observed.fidelity is Coverage.PARTIAL
    assert observed.boundary.kind is BoundaryKind.UNPROVEN
    assert observed.identity is None
    atoms = {atom.path: atom for atom in observed.atoms}
    field = next(iter(metadata))
    assert atoms[("messages", 2, "metadata", field)].category == (
        STRUCTURE if field == "keepID" else LOSS
    )


@pytest.mark.parametrize("keep_id", ["msg_missing", "msg_compaction", "msg_later", None, 1])
def test_missing_self_forward_or_malformed_keep_reference_is_unproven(keep_id):
    value = compacted({"keepID": keep_id})
    value["messages"].append(
        {"id": "msg_later", "type": "user", "time": {"created": 6}, "text": "later"}
    )
    _, audit, observed = observe(value)
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["invalid_graph"]
    assert observed.boundary.kind is BoundaryKind.UNPROVEN
    assert observed.identity is None


@pytest.mark.parametrize("client", ["2.0.22", "2.0.23"])
def test_old_clients_do_not_gain_new_compaction_metadata_rules(client):
    _, audit, _ = observe(compacted({"keepID": "msg_user"}), client)
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.classification_counts)[UNKNOWN]


@pytest.mark.parametrize("kind", ["assistant", "compaction"])
def test_provider_private_native_is_unknown_and_never_portable_text(kind):
    value = compacted({"keepID": "msg_user"}) if kind == "compaction" else opencode_bundle()
    value["messages"][-1]["native"] = {"messages": [{"text": "SYNTHETIC_PRIVATE_BODY"}]}
    _, audit, observed = observe(value)
    assert audit.coverage is Coverage.UNKNOWN
    assert any("native" in atom.path and atom.category == UNKNOWN for atom in observed.atoms)
    assert "SYNTHETIC_PRIVATE_BODY" not in json.dumps([asdict(event) for event in observed.events])
    assert "SYNTHETIC_PRIVATE_BODY" not in repr(audit)
    assert observed.boundary.kind is BoundaryKind.UNPROVEN


def test_unknown_metadata_stays_unknown_and_raw_variations_change_audit_binding():
    value = compacted({"keepID": "msg_user", "future": "SYNTHETIC_UNKNOWN"})
    before, audit, observed = observe(value)
    changed = copy.deepcopy(value)
    changed["messages"][-1]["metadata"]["future"] = "SYNTHETIC_CHANGED"
    after, other, other_observed = observe(changed)
    assert audit.coverage is other.coverage is Coverage.UNKNOWN
    assert before.authority_sha256 != after.authority_sha256
    assert audit.classification != other.classification
    assert observed.boundary.kind is other_observed.boundary.kind is BoundaryKind.UNPROVEN


def test_keep_reference_variation_is_bound_even_when_both_references_exist():
    value = compacted({"keepID": "msg_user"})
    _, first, _ = observe(value)
    value["messages"][-1]["metadata"]["keepID"] = "msg_assistant"
    _, second, observed = observe(value)
    assert first.coverage is second.coverage is Coverage.UNKNOWN
    assert first.classification != second.classification
    assert observed.boundary.kind is BoundaryKind.UNPROVEN


@pytest.mark.parametrize("recent", [None, 1, {"text": "not a string"}])
def test_recent_metadata_requires_a_string(recent):
    _, audit, observed = observe(compacted({"recent": recent}))
    assert audit.coverage is Coverage.UNKNOWN
    assert dict(audit.issue_counts)["invalid_field_type"]
    assert observed.boundary.kind is BoundaryKind.UNPROVEN
