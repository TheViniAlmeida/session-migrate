"""Closed USER proof over target RAW; no authority from fabricated source bytes."""

import copy
import hashlib
import json
from dataclasses import replace

import pytest
from test_f5a_runtime import BODY, fixture

from session_migrate.audit_adapters import LOSS
from session_migrate.conversion import EmitContext, emit_portable
from session_migrate.derived_projection import (
    DerivedProjectionError,
    PreparedDerivedProjection,
    checkpoint_snapshot_sha,
    prepare_derived_projection,
    validate_derived_projection,
    verify_derived_projection,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.handoff import _consume_reviewed
from session_migrate.handoff_types import HANDOFF_PREFIX
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_audit import NativeAuditError
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.operations import _effective_source_coverage


@pytest.fixture(params=["2.0.22", "2.0.23"])
def op_user(tmp_path, request):
    catalog, _, _, _, _, _, reviewed, options, _ = fixture(tmp_path)
    authority, user_input, transformation = _consume_reviewed(reviewed)
    context = EmitContext(
        TargetFormat.OPENCODE,
        "ses_72222222222242228222222222222222",
        options.cwd,
        request.param,
        "2026-10-10T12:00:00.123456Z",
        "openai",
        "synthetic-model",
    )
    emission = emit_portable(user_input.events, context)
    prepared = prepare_derived_projection(
        authority, user_input, transformation, emission, artifact_path=tmp_path / "artifact.json"
    )
    raw = json.loads(emission.native_bytes)
    raw["info"]["model"]["variant"] = "observed-importer-variant"
    yield authority, user_input, transformation, emission, prepared, raw, tmp_path
    catalog.close()


def target_snapshot(raw, tmp_path):
    # This is a target audit fixture, never OriginalNativeAuthority input.
    data = json.dumps(raw).encode() if type(raw) is dict else raw
    return NativeSnapshot.from_bytes(
        data,
        context=SourceCaptureContext(AgentFormat.OPENCODE, tmp_path / "observed-target.json"),
    )


def test_op_user_proof_keeps_actual_loss_and_conservative_checkpoint_scope(op_user):
    authority, user_input, _, emission, prepared, raw, tmp_path = op_user
    snapshot = target_snapshot(raw, tmp_path)
    captured_bytes = snapshot.raw_bytes
    proof, observed = verify_derived_projection(prepared, snapshot)
    assert snapshot.raw_bytes is captured_bytes
    assert snapshot.raw_bytes != emission.native_bytes
    assert authority.snapshot.raw_bytes != snapshot.raw_bytes
    assert prepared.artifact_observation.coverage is Coverage.VERIFIED
    assert prepared.artifact_observation.fidelity is Coverage.VERIFIED
    assert prepared.expectation.coverage is Coverage.PARTIAL
    assert observed.coverage is Coverage.VERIFIED
    assert observed.fidelity is Coverage.PARTIAL
    assert _effective_source_coverage(observed) is Coverage.PARTIAL
    assert observed.audit.issue_counts == ()
    assert [atom.path for atom in observed.atoms if atom.category == LOSS] == [
        ("info", "model", "variant")
    ]
    assert observed.events[0].text == HANDOFF_PREFIX + "\n\n" + BODY.decode()
    assert observed.source_prefix == prepared.expectation.prefix_witness
    assert observed.audit.structure == prepared.artifact_observation.audit.structure
    assert (
        checkpoint_snapshot_sha(observed, prepared.versions)
        == proof.expected_target_snapshot_sha256
    )
    assert proof.input_event_sha256 == user_input.identity.event_sha256
    assert proof.observed_target_authority_sha256 == hashlib.sha256(captured_bytes).hexdigest()
    assert not hasattr(prepared, "certificate")
    # The variant is bound by whole-RAW SHA, but never promised as USER semantics.
    raw["info"]["model"]["variant"] = "another-observed-variant"
    other_proof, other_observed = verify_derived_projection(
        prepared, target_snapshot(raw, tmp_path)
    )
    assert other_proof.observed_target_authority_sha256 != proof.observed_target_authority_sha256
    assert other_observed.source_prefix == observed.source_prefix
    assert other_observed.fidelity is Coverage.PARTIAL


@pytest.mark.parametrize(
    "change,code",
    [
        ("no_variant", "audit_unproven"),
        ("variant_bool", "native_parser_rejected"),
        ("variant_object", "native_parser_rejected"),
        ("variant_null", "native_parser_rejected"),
        ("model_missing", "metadata_mismatch"),
        ("provider_missing", "native_parser_rejected"),
        ("id_missing", "native_parser_rejected"),
        ("provider_type", "native_parser_rejected"),
        ("model_extra", "audit_unproven"),
        ("model_changed", "metadata_mismatch"),
        ("provider_changed", "metadata_mismatch"),
        ("cwd_changed", "metadata_mismatch"),
        ("cwd_missing", "native_parser_rejected"),
        ("session_id", "metadata_mismatch"),
        ("truncated", "semantic_mismatch"),
        ("body_only", "semantic_mismatch"),
        ("substring", "semantic_mismatch"),
        ("empty_body_loss", "audit_unproven"),
        ("duplicate_user", "semantic_mismatch"),
        ("assistant", "semantic_mismatch"),
        ("tool_record", "native_parser_rejected"),
        ("call_tool", "audit_unproven"),
        ("file_loss", "audit_unproven"),
        ("unknown_metadata", "audit_unproven"),
        ("unknown_schema", "audit_unproven"),
    ],
)
def test_op_user_scope_rejects_every_other_loss_role_and_raw_metadata_change(op_user, change, code):
    _, _, _, _, prepared, raw, tmp_path = op_user
    model = raw["info"]["model"]
    message = raw["messages"][0]
    if change == "no_variant":
        model.pop("variant")
    elif change.startswith("variant_"):
        model["variant"] = {"variant_bool": True, "variant_object": {}, "variant_null": None}[
            change
        ]
    elif change == "model_missing":
        raw["info"].pop("model")
    elif change == "provider_missing":
        model.pop("providerID")
    elif change == "id_missing":
        model.pop("id")
    elif change == "provider_type":
        model["providerID"] = 7
    elif change == "model_extra":
        model["future"] = "unknown"
    elif change == "model_changed":
        model["id"] = "wrong-model"
    elif change == "provider_changed":
        model["providerID"] = "wrong-provider"
    elif change == "cwd_changed":
        raw["info"]["location"]["directory"] = str(tmp_path)
    elif change == "cwd_missing":
        raw["info"]["location"].pop("directory")
    elif change == "session_id":
        raw["info"]["id"] = "ses_73333333333343338333333333333333"
    elif change == "truncated":
        message["text"] = message["text"][:-1]
    elif change == "body_only":
        message["text"] = BODY.decode()
    elif change == "substring":
        message["text"] = "prefix" + message["text"] + "suffix"
    elif change == "empty_body_loss":
        message["text"] = ""
    elif change == "duplicate_user":
        other = copy.deepcopy(message)
        other["id"] = "msg_different"
        raw["messages"].append(other)
    elif change == "assistant":
        raw["messages"] = [
            {
                "type": "assistant",
                "id": message["id"],
                "time": message["time"],
                "agent": "build",
                "model": {"id": model["id"], "providerID": model["providerID"]},
                "content": [{"type": "text", "text": message["text"]}],
            }
        ]
    elif change == "tool_record":
        message["type"] = "tool"
    elif change == "call_tool":
        message["content"] = [{"type": "tool", "id": "call", "name": "exec"}]
    elif change == "file_loss":
        message["files"] = [
            {
                "data": "Zml4dHVyZQ==",
                "mime": "text/plain",
                "source": {"type": "inline"},
                "name": "unproved.txt",
            }
        ]
    elif change == "unknown_metadata":
        raw["info"]["metadata"] = {"future": True}
    else:
        raw["schema_version"] = 3
    error_type = NativeAuditError if code == "native_parser_rejected" else DerivedProjectionError
    with pytest.raises(error_type) as error:
        verify_derived_projection(prepared, target_snapshot(raw, tmp_path))
    if error_type is DerivedProjectionError:
        assert error.value.code == code


def test_same_raw_duplicate_keys_are_rejected_before_proof(op_user):
    _, _, _, _, prepared, raw, tmp_path = op_user
    data = json.dumps(raw).encode()
    duplicate = b'{"info":{},' + data[1:]
    with pytest.raises(SessionMigrateError):
        verify_derived_projection(prepared, target_snapshot(duplicate, tmp_path))


@pytest.mark.parametrize("change", ["variant", "model_missing", "body", "future_version"])
def test_preparation_remains_strict_and_cannot_self_certify_artifact(op_user, change):
    authority, user_input, transformation, emission, _, _, tmp_path = op_user
    raw = json.loads(emission.native_bytes)
    if change == "variant":
        raw["info"]["model"]["variant"] = "unproved"
    elif change == "model_missing":
        raw["info"].pop("model")
    elif change == "body":
        raw["messages"][0]["text"] = "self-authorized wrong USER"
    else:
        with pytest.raises(SessionMigrateError, match="invalid or unsupported"):
            replace(emission.context, target_cli_version="2.0.24")
        return
    data = json.dumps(raw).encode()
    emission = replace(emission, native_bytes=data)
    with pytest.raises(SessionMigrateError):
        prepare_derived_projection(
            authority, user_input, transformation, emission, artifact_path=tmp_path / "other.json"
        )


@pytest.mark.parametrize("change", ["copy", "constructor", "expectation", "context", "snapshot"])
def test_private_issuer_and_binding_are_not_replaced_by_wire_or_copies(op_user, change):
    _, _, _, _, prepared, raw, tmp_path = op_user
    if change == "constructor":
        with pytest.raises(DerivedProjectionError):
            PreparedDerivedProjection()
        return
    if change == "copy":
        prepared = copy.copy(prepared)
    elif change == "expectation":
        object.__setattr__(
            prepared, "expectation", replace(prepared.expectation, coverage=Coverage.VERIFIED)
        )
    elif change == "context":
        object.__setattr__(prepared, "_context", replace(prepared._context, model="changed"))
    else:
        object.__setattr__(prepared, "_artifact_snapshot", target_snapshot(raw, tmp_path))
    with pytest.raises(SessionMigrateError):
        validate_derived_projection(prepared)
    with pytest.raises(SessionMigrateError):
        verify_derived_projection(prepared, target_snapshot(raw, tmp_path))
