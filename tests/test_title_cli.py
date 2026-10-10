"""Title/proposal CLI contracts; synthetic sources, no providers or account stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from session_migrate import cli, operations
from session_migrate.cli import build_parser, main

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
TITLE = "  Título · ação 🧪 日本語 [bold]literal[/bold]  "


def _environment(tmp_path, monkeypatch):
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.setenv("SYNTHETIC_SECRET", "DO_NOT_SERIALIZE_FIXTURE_SECRET")


def test_proposal_parsers_reuse_selectors_and_title_is_independent():
    parser = build_parser()
    direct = parser.parse_args(
        [
            "transfer",
            "--title",
            "source name",
            "--to",
            "codex",
            "--target-title",
            TITLE,
            "--local-title-alias",
        ]
    )
    proposal = parser.parse_args(
        [
            "proposal",
            "transfer",
            "--title",
            "source name",
            "--to",
            "codex",
            "--target-title",
            TITLE,
            "--local-title-alias",
        ]
    )
    assert direct.title == proposal.title == "source name"
    assert direct.target_title == proposal.target_title == TITLE
    assert cli._operation_request(direct, "transfer") == cli._operation_request(
        proposal, "transfer"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "proposal",
                "import",
                "source",
                "--to",
                "codex",
                "--target-title",
                TITLE,
                "--local-title-alias",
                "--require-native-title",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["proposal", "convert", "source", "--to", "codex", "--output", "out", "--apply"]
        )


@pytest.mark.parametrize("command", ["convert", "import", "transfer"])
def test_cli_proposal_no_publication_and_handle_discarded(
    tmp_path,
    monkeypatch,
    capsys,
    command,
):
    _environment(tmp_path, monkeypatch)
    source = tmp_path / "source.jsonl"
    source.write_bytes(FIXTURE.read_bytes())
    original = source.read_bytes()
    cwd = tmp_path / "project"
    cwd.mkdir()
    target = tmp_path / "target"
    args = ["proposal", command]
    if command == "transfer":
        home = tmp_path / "source-home"
        native = home / "projects/-synthetic/10000000-0000-4000-8000-000000000000.jsonl"
        native.parent.mkdir(parents=True)
        native.write_bytes(original)
        args += [
            "10000000-0000-4000-8000-000000000000",
            "--from",
            "claude",
            "--source-home",
            str(home),
        ]
    else:
        args += [str(source)]
    args += ["--to", "codex", "--cwd", str(cwd), "--target-title", TITLE, "--local-title-alias"]
    args += (
        ["--output", str(target / "out.jsonl")] if command == "convert" else ["--home", str(target)]
    )
    prepared = []
    original_prepare = cli.prepare_operation

    def prepare(*a, **kw):
        handle = original_prepare(*a, **kw)
        prepared.append(handle)
        return handle

    monkeypatch.setattr(cli, "prepare_operation", prepare)
    monkeypatch.setattr(cli, "run_operation", lambda *a, **kw: pytest.fail("proposal applied"))
    monkeypatch.setattr(
        operations, "commit_operation", lambda *a, **kw: pytest.fail("commit called")
    )
    # stdin is deliberately unusable: proposal is JSON automation, not a prompt.
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("unexpected prompt"))
    assert main(args) == 0
    raw = capsys.readouterr().out
    value = json.loads(raw)
    assert value["schema_version"] == 1 and value["operation"] == command
    assert value["title"]["requested"] == TITLE
    assert value["title"]["local_alias"] == TITLE
    assert value["legacy_result"]["dry_run"]
    assert "DO_NOT_SERIALIZE_FIXTURE_SECRET" not in raw
    assert "Remember synthetic" not in raw and "iVBOR" not in raw
    assert source.read_bytes() == original
    assert not target.exists()
    assert not (tmp_path / "xdg_state_home").exists()
    assert len(prepared) == 1
    with pytest.raises(Exception, match="already consumed"):
        prepared[0]._consume()


@pytest.mark.parametrize(
    "title,extra",
    [
        ("", []),
        ("   ", []),
        ("bad\x1b[31m", []),
        ("bad\u202e", []),
        ("valid", []),
        ("valid", ["--require-native-title"]),
    ],
)
def test_title_cli_refusal_has_no_writes(tmp_path, monkeypatch, capsys, title, extra):
    _environment(tmp_path, monkeypatch)
    output = tmp_path / "missing/output.jsonl"
    monkeypatch.setattr(
        operations, "_resolve_source", lambda *a, **kw: pytest.fail("source loaded")
    )
    assert (
        main(
            [
                "convert",
                str(FIXTURE),
                "--to",
                "codex",
                "--output",
                str(output),
                "--target-title",
                title,
                *extra,
            ]
        )
        == 2
    )
    assert capsys.readouterr().err.startswith("session-migrate: error:")
    assert not output.parent.exists()


def test_alias_convert_receipt_is_pending_and_legacy_shape_stays_unchanged(
    tmp_path,
    monkeypatch,
    capsys,
):
    _environment(tmp_path, monkeypatch)
    cwd = tmp_path / "project"
    cwd.mkdir()
    plain = tmp_path / "plain.jsonl"
    assert (
        main(["convert", str(FIXTURE), "--to", "codex", "--output", str(plain), "--cwd", str(cwd)])
        == 0
    )
    legacy = json.loads(capsys.readouterr().out)
    assert "schema_version" not in legacy and "title" not in legacy
    titled = tmp_path / "titled.jsonl"
    assert (
        main(
            [
                "convert",
                str(FIXTURE),
                "--to",
                "codex",
                "--output",
                str(titled),
                "--cwd",
                str(cwd),
                "--target-title",
                TITLE,
                "--local-title-alias",
            ]
        )
        == 0
    )
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["schema_version"] == 1
    assert set(envelope["legacy_result"]) == set(legacy)
    assert envelope["title"]["persistence"] == "alias_pending"
    assert not envelope["title"]["alias_saved"]
    assert envelope["title"]["title_plan"]["requested"] == TITLE
    assert Path(envelope["title_receipt"]).is_file()
    manifest = json.loads(Path(envelope["legacy_result"]["manifest"]).read_text())
    assert manifest["schema_version"] == 2
    assert not (tmp_path / "xdg_state_home/session-migrate/title-aliases").exists()


@pytest.mark.parametrize("kind", ["native", "file"])
def test_title_cli_partial_publication_is_typed_metadata_without_exception_body(
    tmp_path,
    monkeypatch,
    capsys,
    kind,
):
    from session_migrate.errors import NativeImportPartialError, PublishedArtifactPartialError
    from session_migrate.model import TargetFormat

    def partial(*args, **kwargs):
        if kind == "native":
            raise NativeImportPartialError(
                "DO_NOT_ECHO_EXCEPTION_BODY",
                target_format=TargetFormat.CODEX,
                session_id="synthetic-id",
                manifest="synthetic-manifest",
                root="synthetic-root",
                stage="alias_publication",
            )
        raise PublishedArtifactPartialError(
            target_format=TargetFormat.CODEX,
            session_id="synthetic-id",
            output="synthetic-file",
            manifest="synthetic-manifest",
            stage="title_receipt_publication",
        )

    monkeypatch.setattr(cli, "run_operation", partial)
    assert (
        main(
            [
                "convert",
                str(FIXTURE),
                "--to",
                "codex",
                "--output",
                str(tmp_path / "out"),
                "--target-title",
                TITLE,
                "--local-title-alias",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    metadata = json.loads(output.err)
    assert metadata["status"] == "needs_reconcile" and metadata["target_id"] == "synthetic-id"
    assert metadata["manifest"] == "synthetic-manifest"
    assert metadata["publication_kind"] == (
        "native_import" if kind == "native" else "converted_file"
    )
    assert "DO_NOT_ECHO" not in output.err
    assert "do not repeat" in metadata["action"]


@pytest.mark.parametrize("version", ["2.0.24", "2.0.23-custom"])
def test_unverified_native_title_version_proposal_rejects_before_source_load(
    tmp_path,
    monkeypatch,
    capsys,
    version,
):
    _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        operations, "_resolve_source", lambda *a, **kw: pytest.fail("source loaded")
    )
    assert (
        main(
            [
                "proposal",
                "convert",
                str(FIXTURE),
                "--to",
                "opencode",
                "--output",
                str(tmp_path / "out.json"),
                "--target-title",
                TITLE,
                "--target-cli-version",
                version,
            ]
        )
        == 2
    )
    assert "exact-client proof" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()
