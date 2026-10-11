"""Private derived authority compares genuine B1 output to independent USER input."""

import copy
import json
from dataclasses import replace

import pytest
from test_f5a_runtime import fixture

from session_migrate.conversion import EmitContext, emit_portable
from session_migrate.derived_projection import (
    DerivedProjectionError,
    PreparedDerivedProjection,
    prepare_derived_projection,
    validate_derived_projection,
    verify_derived_projection,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.handoff import _consume_reviewed
from session_migrate.lineage import Coverage
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext


@pytest.mark.parametrize(
    "target,version",
    [
        (TargetFormat.CLAUDE, "2.1.209"),
        (TargetFormat.CODEX, "0.144.4"),
        (TargetFormat.OPENCODE, "2.0.22"),
        (TargetFormat.OPENCODE, "2.0.23"),
    ],
)
def test_private_projection_source_and_user_are_distinct_and_no_full_certificate(
    tmp_path, target, version
):
    catalog, _, _, _, _, _, reviewed, options, _ = fixture(tmp_path)
    authority, user_input, transformation = _consume_reviewed(reviewed)
    context = EmitContext(
        target,
        "ses_72222222222242228222222222222222"
        if target is TargetFormat.OPENCODE
        else "72222222-2222-4222-8222-222222222222",
        options.cwd,
        version,
        "2026-10-10T12:00:00.123456Z",
        "openai",
        "synthetic-model",
    )
    emission = emit_portable(user_input.events, context)
    prepared = prepare_derived_projection(
        authority, user_input, transformation, emission, artifact_path=tmp_path / "target.jsonl"
    )
    assert prepared.source_authority_sha256 == authority.authority_sha256
    assert not hasattr(prepared, "certificate")
    actual_bytes = emission.native_bytes
    if target is TargetFormat.OPENCODE:
        raw = json.loads(actual_bytes)
        raw["info"]["model"]["variant"] = "observed-importer-variant"
        actual_bytes = json.dumps(raw).encode()
    assert prepared.expectation.coverage is (
        Coverage.PARTIAL if target is TargetFormat.OPENCODE else Coverage.VERIFIED
    )
    actual = NativeSnapshot.from_bytes(
        actual_bytes,
        context=SourceCaptureContext(AgentFormat(target.value), tmp_path / "native-target.jsonl"),
    )
    proof, observed = verify_derived_projection(prepared, actual)
    assert proof.input_event_sha256 == user_input.identity.event_sha256
    assert observed.source_prefix == prepared.expectation.prefix_witness
    with pytest.raises(DerivedProjectionError):
        PreparedDerivedProjection()
    with pytest.raises(DerivedProjectionError):
        validate_derived_projection(copy.copy(prepared))
    catalog.close()


def test_private_projection_source_changed_is_not_wire_proof(tmp_path):
    catalog, _, _, _, _, source_path, reviewed, options, _ = fixture(tmp_path)
    authority, user_input, transformation = _consume_reviewed(reviewed)
    context = EmitContext(
        TargetFormat.CODEX,
        "72222222-2222-4222-8222-222222222222",
        options.cwd,
        "0.144.4",
        "2026-10-10T12:00:00Z",
        "openai",
        "synthetic-model",
    )
    emission = emit_portable(user_input.events, context)
    prepared = prepare_derived_projection(
        authority, user_input, transformation, emission, artifact_path=tmp_path / "target.jsonl"
    )
    actual = NativeSnapshot.from_bytes(
        emission.native_bytes,
        context=SourceCaptureContext(AgentFormat.CODEX, tmp_path / "native.jsonl"),
    )
    source_path.write_bytes(source_path.read_bytes().replace(b"original", b"modified"))
    with pytest.raises(SessionMigrateError):
        verify_derived_projection(prepared, actual)
    catalog.close()


@pytest.mark.parametrize("change", ["content", "role", "unknown", "context"])
def test_native_artifact_cannot_be_its_own_content_oracle(tmp_path, change):
    catalog, _, _, _, _, _, reviewed, options, _ = fixture(tmp_path)
    authority, user_input, transformation = _consume_reviewed(reviewed)
    context = EmitContext(
        TargetFormat.CODEX,
        "72222222-2222-4222-8222-222222222222",
        options.cwd,
        "0.144.4",
        "2026-10-10T12:00:00Z",
        "openai",
        "synthetic-model",
    )
    emission = emit_portable(user_input.events, context)
    if change == "content":
        emission = replace(
            emission, native_bytes=emission.native_bytes.replace(b"unknown", b"changed")
        )
    elif change == "role":
        emission = replace(
            emission,
            native_bytes=emission.native_bytes.replace(b'"role":"user"', b'"role":"assistant"'),
        )
    elif change == "unknown":
        emission = replace(
            emission, native_bytes=emission.native_bytes + b'{"type":"future_unknown"}\n'
        )
    else:
        emission = replace(emission, context=replace(context, model_provider="changed"))
    with pytest.raises(SessionMigrateError):
        prepare_derived_projection(
            authority, user_input, transformation, emission, artifact_path=tmp_path / "target.jsonl"
        )
    catalog.close()
