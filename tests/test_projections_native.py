"""Public offline import/cold-export checks, separate from boundary/identity proof."""

from pathlib import Path

import pytest
import test_title_native
from test_projections import claude_records, snapshot, tool_records

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.jsonl import write_private_atomic
from session_migrate.lineage import PolicyVersions
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext
from session_migrate.projections import ProjectionContext, prepare_projection, verify_projection

native_client = test_title_native.native_client


@pytest.mark.parametrize(
    ("source_format", "release", "folder"),
    [
        (AgentFormat.CLAUDE, "2.1.209", "claude-2.1.209"),
        (AgentFormat.CODEX, "0.144.4", "codex-0.144.4"),
    ],
)
def test_captured_source_projection_matches_public_cold_export(
    tmp_path: Path, native_client, source_format, release, folder
):
    fixture = Path(__file__).parent / "fixtures" / folder / "basic.jsonl"
    source = NativeSnapshot.from_bytes(
        fixture.read_bytes(),
        context=SourceCaptureContext(source_format, Path(f"{source_format.value}:fixture")),
    )
    options = ConversionOptions(
        TargetFormat.OPENCODE,
        cwd=tmp_path / "project",
        target_cli_version=native_client.version,
    )
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        release,
        native_client.version,
    )
    context = ProjectionContext(TargetFormat.OPENCODE, native_client.version)
    prepared = prepare_projection(
        source, artifact, options=options, versions=versions, context=context
    )
    bundle = tmp_path / "projection-import.json"
    write_private_atomic(bundle, prepared.artifact_bytes)
    acknowledgement = native_client.run(
        ["session", "import", str(bundle), "--directory", str(tmp_path / "project"), "--standalone"]
    )
    assert acknowledgement.startswith("Imported session: ")
    assert artifact.session_id in acknowledgement
    # A separate client process reopens the persisted native session.
    exported = native_client.run(["session", "export", artifact.session_id, "--standalone"]).encode(
        "utf-8"
    )
    observed = NativeSnapshot.from_bytes(
        exported,
        context=SourceCaptureContext(AgentFormat.OPENCODE, Path(f"opencode:{artifact.session_id}")),
    )
    result = verify_projection(prepared, observed, context=context)
    assert result.semantic_matches
    assert result.relation_complete
    assert not result.identity_proven
    assert not result.boundary_proven
    assert not result.delta_eligible


@pytest.mark.parametrize("case", ["tool", "tool_error", "inline_image"])
def test_tools_errors_and_inline_media_match_public_cold_export(
    tmp_path: Path, native_client, case: str
):
    if case == "inline_image":
        records = claude_records(
            [
                [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "eA==",
                        },
                    }
                ],
                [{"type": "text", "text": "synthetic image answer"}],
            ]
        )
    else:
        records = tool_records(error=case == "tool_error")
    source = snapshot(records)
    options = ConversionOptions(
        TargetFormat.OPENCODE,
        cwd=tmp_path / "project",
        target_cli_version=native_client.version,
    )
    artifact = convert_session(source.parse(), options)
    versions = PolicyVersions(
        "canonical-v2",
        "portable-projection-v1",
        "native-audit-v1",
        "native-audit-v1",
        "2.1.209",
        native_client.version,
    )
    context = ProjectionContext(TargetFormat.OPENCODE, native_client.version)
    prepared = prepare_projection(
        source, artifact, options=options, versions=versions, context=context
    )
    bundle = tmp_path / "content-import.json"
    write_private_atomic(bundle, prepared.artifact_bytes)
    acknowledgement = native_client.run(
        ["session", "import", str(bundle), "--directory", str(tmp_path / "project"), "--standalone"]
    )
    assert artifact.session_id in acknowledgement
    observed = NativeSnapshot.from_bytes(
        native_client.run(["session", "export", artifact.session_id, "--standalone"]).encode(),
        context=SourceCaptureContext(AgentFormat.OPENCODE, Path(f"opencode:{artifact.session_id}")),
    )
    result = verify_projection(prepared, observed, context=context)
    assert result.semantic_matches
    assert result.relation_complete
    assert not result.identity_proven
    assert not result.boundary_proven
    assert not result.delta_eligible
