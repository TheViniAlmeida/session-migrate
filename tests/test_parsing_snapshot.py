"""Bytes seams retain exact parser payloads and provenance without filesystem reads."""

import hashlib
from dataclasses import asdict
from pathlib import Path

import pytest

from session_migrate.errors import JsonlError
from session_migrate.formats import claude, codex, opencode
from session_migrate.jsonl import iter_jsonl, iter_jsonl_bytes
from session_migrate.model import AgentFormat
from session_migrate.native_snapshots import NativeSnapshot, SourceCaptureContext, capture_source

FIXTURES = Path(__file__).parent / "fixtures"
CASES = [
    (AgentFormat.CLAUDE, "claude-2.1.209/basic.jsonl", claude.parse, claude.parse_bytes),
    (AgentFormat.CODEX, "codex-0.144.4/basic.jsonl", codex.parse, codex.parse_bytes),
    (AgentFormat.CODEX, "codex-0.153.4/paginated.jsonl", codex.parse, codex.parse_bytes),
    (
        AgentFormat.OPENCODE,
        "opencode-1.17.20/basic.json",
        opencode.parse_session,
        opencode.parse_session_bytes,
    ),
    (
        AgentFormat.OPENCODE,
        "opencode-source-1.17.20/comprehensive.json",
        opencode.parse_session,
        opencode.parse_session_bytes,
    ),
]


@pytest.mark.parametrize("source_format,fixture,path_parser,bytes_parser", CASES)
def test_path_bytes_and_captured_source_parity(
    tmp_path: Path, source_format: AgentFormat, fixture: str, path_parser, bytes_parser
) -> None:
    data = (FIXTURES / fixture).read_bytes()
    path = tmp_path / "private-source"
    path.write_bytes(data)
    path.chmod(0o600)
    expected = asdict(path_parser(path))
    assert asdict(bytes_parser(data, path)) == expected
    snapshot = capture_source(SourceCaptureContext(source_format, path))
    assert asdict(snapshot.parse()) == expected
    assert expected["source_sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("source_format,fixture,path_parser,bytes_parser", CASES)
def test_bytes_parsers_do_not_resolve_or_reopen_source(
    monkeypatch: pytest.MonkeyPatch,
    source_format: AgentFormat,
    fixture: str,
    path_parser,
    bytes_parser,
) -> None:
    data = (FIXTURES / fixture).read_bytes()
    frozen_path = Path("virtual/source")

    def forbidden(*args: object, **kwargs: object):
        pytest.fail("pure bytes parser touched the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    parsed = bytes_parser(data, frozen_path)
    assert parsed.source_path == frozen_path
    assert parsed.source_sha256 == hashlib.sha256(data).hexdigest()
    snapshot = NativeSnapshot.from_bytes(
        data, context=SourceCaptureContext(source_format, frozen_path)
    )
    assert asdict(snapshot.parse()) == asdict(parsed)


def test_jsonl_physical_blank_lines_and_duplicate_keys_preserve_legacy_parity(
    tmp_path: Path,
) -> None:
    data = b' \r\n{"value":1,"value":2}\r\n\n{"nested":[{"unicode":"\xc3\xa9"}]}\n'
    path = tmp_path / "records.jsonl"
    path.write_bytes(data)
    records = tuple(iter_jsonl_bytes(data))
    assert records == tuple(iter_jsonl(path))
    assert [(record.index, record.line_number) for record in records] == [(0, 2), (1, 4)]
    assert records[0].value == {"value": 2}


@pytest.mark.parametrize(
    "data,limits",
    [
        (b"{}\n{}\n", {"max_total_bytes": 5}),
        (b'{"text":"' + b"x" * 32, {"max_record_bytes": 16}),
        (b"{}\n{}\n", {"max_records": 1}),
        (b'\n{"value":NaN}\n', {}),
        (b"\n[]\n", {}),
        (b'{"value":"\xff"}\n', {}),
    ],
)
def test_jsonl_failure_parity(tmp_path: Path, data: bytes, limits: dict) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(data)
    with pytest.raises(JsonlError) as path_error:
        tuple(iter_jsonl(path, **limits))
    with pytest.raises(JsonlError) as bytes_error:
        tuple(iter_jsonl_bytes(data, **limits))
    assert str(bytes_error.value) == str(path_error.value)


def test_opencode_import_bytes_reuses_existing_v1_and_v2_projection(tmp_path: Path) -> None:
    original = claude.parse(FIXTURES / "claude-2.1.209/basic.jsonl")
    for version in (opencode.PINNED_OPENCODE_VERSION, "2.0.23"):
        data, _ = opencode.serialize(
            original,
            session_id="ses_synthetic_snapshot",
            cwd=tmp_path,
            cli_version=version,
            provider_id="fixture",
            model_id="fixture-model",
        )
        path = tmp_path / f"export-{version}.json"
        path.write_bytes(data)
        assert asdict(opencode.parse_import_bytes(data)) == asdict(opencode.parse_import(path))
        assert asdict(opencode.parse_session_bytes(data, path)) == asdict(
            opencode.parse_session(path)
        )


@pytest.mark.parametrize("source_format,fixture,path_parser,bytes_parser", CASES)
def test_legacy_path_wrappers_continue_to_follow_symlinks(
    tmp_path: Path, source_format: AgentFormat, fixture: str, path_parser, bytes_parser
) -> None:
    original = FIXTURES / fixture
    alias = tmp_path / "alias"
    alias.symlink_to(original.resolve())
    assert asdict(path_parser(alias)) == asdict(path_parser(original))
