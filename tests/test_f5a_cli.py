"""CLI F5A over real private B3 captures and file publication in synthetic homes."""

import hashlib
import json
from pathlib import Path

import pytest
from test_f5a_return import return_fixture
from test_f5a_runtime import BODY, fixture

from session_migrate.cli import main
from session_migrate.continuity_bindings import PUBLIC_ENVIRONMENT
from session_migrate.lineage import LineageStore
from session_migrate.profiles import ProfileStore


def setup_cli(tmp_path, monkeypatch, *, returning=False, mode="full"):
    if returning:
        catalog, observer, request, state, env, first, _, ref = return_fixture(tmp_path, mode)
        if request.reviewed:
            request.reviewed.discard()
        source = request.current_source_ref.profile_id
        # Current explicit profiles from the actual observer, never historical fingerprints.
        profiles = observer._profiles
        target = request.current_target_profile.id
        parent = first.lineage_receipt.operation_id
    else:
        catalog, observer, a, b, ref, path, reviewed, _, env = fixture(tmp_path)
        reviewed.discard()
        profiles, source, target = (a, b), a.id, b.id
        state, parent = tmp_path / ".local/state/session-migrate", None
    catalog_path = catalog.path
    catalog.close()
    profile_file = tmp_path / "profiles/current.json"
    ProfileStore(profile_file).create(profiles)
    document_dir = tmp_path / "documents"
    document_dir.mkdir(mode=0o700)
    for key in PUBLIC_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    prefix = ["--catalog", str(catalog_path), "--state-home", str(state)]
    flags = [
        "--profiles",
        str(profile_file),
        "--source-profile",
        source,
        "--source-native-id",
        ref.native_id,
        "--json",
    ]
    destination = [
        "--target-profile",
        target,
        "--cwd",
        str(tmp_path / "project"),
        "--target-cli-version",
        "2.1.209" if returning else "0.144.4",
        "--model-provider",
        "openai",
        "--model",
        "synthetic-model",
    ]
    return prefix, flags, destination, state, document_dir / "handoff.md", parent


def editor(monkeypatch, document, *, body=BODY, mutate_source=None, digest=None):
    def accept():
        assert document.is_file()
        document.write_bytes(body)  # Preserve the issued private export's inode.
        if mutate_source:
            mutate_source()
        return digest or hashlib.sha256(body).hexdigest()

    monkeypatch.setattr("builtins.input", accept)


def output(capsys):
    return json.loads(capsys.readouterr().out)


def test_review_metadata_and_export_do_not_create_target_or_ledger(tmp_path, monkeypatch, capsys):
    prefix, flags, _, state, document, _ = setup_cli(tmp_path, monkeypatch)
    assert main(prefix + ["handoff", "review"] + flags + ["--range", "0:1"]) == 0
    result = output(capsys)
    assert result["status"] == "reviewed_metadata"
    assert "native original request" not in json.dumps(result)
    assert not state.exists() and not (tmp_path / "target").exists()
    assert (
        main(
            prefix + ["handoff", "export"] + flags + ["--range", "0:1", "--document", str(document)]
        )
        == 0
    )
    result = output(capsys)
    assert result["status"] == "exported"
    assert "Objective: unknown" in document.read_text()
    assert "native original answer" in document.read_text()
    assert document.stat().st_mode & 0o777 == 0o600
    assert not state.exists() and not (tmp_path / "target").exists()


@pytest.mark.parametrize("confirm", [False, True])
def test_summary_same_process_export_review_exact_body_and_default_zero_publication(
    tmp_path, monkeypatch, capsys, confirm
):
    prefix, flags, destination, state, document, _ = setup_cli(tmp_path, monkeypatch)
    editor(monkeypatch, document)
    assert (
        main(
            prefix
            + ["handoff", "summary"]
            + flags
            + destination
            + ["--range", "0:1", "--edit-export", str(document)]
            + (["--confirm"] if confirm else [])
        )
        == 0
    )
    result = output(capsys)
    assert BODY.decode() not in json.dumps(result)
    assert result["status"] == ("completed" if confirm else "prepared")
    if confirm:
        view = LineageStore.inspect_existing(state / "lineage-v1")
        assert len(view.operations) == 1
        assert view.operations[0].receipt.continuity_reason == "derived_handoff"
        native = Path(result["result"]["legacy_result"]["output"]).read_text()
        assert "native original answer" not in native
        assert "Next step: inspect synthetic fixture" in native
    else:
        assert not state.exists() and not (tmp_path / "target").exists()


@pytest.mark.parametrize("mode", ["full", "summary"])
def test_return_new_actual_parent_link_and_new_identity(tmp_path, monkeypatch, capsys, mode):
    prefix, flags, destination, state, document, parent = setup_cli(
        tmp_path, monkeypatch, returning=True, mode=mode
    )
    extra = []
    if mode == "summary":
        editor(monkeypatch, document)
        extra = ["--range", "0:1", "--edit-export", str(document)]
    assert (
        main(
            prefix
            + ["return-new"]
            + flags
            + destination
            + extra
            + ["--content-mode", mode, "--parent-operation-id", parent, "--confirm"]
        )
        == 0
    )
    result = output(capsys)
    assert result["status"] == "completed"
    view = LineageStore.inspect_existing(state / "lineage-v1")
    op = next(op for op in view.operations if op.record.return_link)
    assert op.record.return_link.parent_operation_id == parent
    assert (
        op.receipt.target_identity.namespace.native_id
        != op.record.source.identity.namespace.native_id
    )
    assert op.receipt.continuity_reason == ("derived_handoff" if mode == "summary" else "tracked")


@pytest.mark.parametrize("failure", ["digest", "replacement", "oversize", "source"])
def test_summary_input_failure_is_bodyfree_and_never_publishes(
    tmp_path, monkeypatch, capsys, failure
):
    prefix, flags, destination, state, document, _ = setup_cli(tmp_path, monkeypatch)

    def accept():
        if failure == "replacement":
            document.rename(document.with_suffix(".saved"))
            document.write_bytes(BODY)
            document.chmod(0o600)
        elif failure == "oversize":
            document.write_bytes(b"x" * (1024 * 1024 + 1))
        else:
            document.write_bytes(BODY)
        if failure == "source":
            path = next((tmp_path / "source").rglob("*.jsonl"))
            path.write_bytes(
                path.read_bytes().replace(b"native original answer", b"native changed answer")
            )
        return (
            "0" * 64 if failure == "digest" else hashlib.sha256(document.read_bytes()).hexdigest()
        )

    monkeypatch.setattr("builtins.input", accept)
    assert (
        main(
            prefix
            + ["handoff", "summary"]
            + flags
            + destination
            + ["--range", "0:1", "--edit-export", str(document), "--confirm"]
        )
        == 1
    )
    result = output(capsys)
    assert result["status"] == "blocked" and BODY.decode() not in json.dumps(result)
    assert not (tmp_path / "target").exists() and not state.exists()


@pytest.mark.parametrize(
    "flags", [[], ["--range", "1:0"], ["--range", "-1:1"], ["--range", "0:1", "--confirm"]]
)
def test_invalid_review_arguments_exit_two_without_binding(flags):
    with pytest.raises(SystemExit) as caught:
        main(["handoff", "review"] + flags)
    assert caught.value.code == 2


def test_missing_catalog_does_not_create_any_tree(tmp_path, capsys):
    assert (
        main(
            [
                "--catalog",
                str(tmp_path / "absent/catalog.sqlite"),
                "handoff",
                "review",
                "--source-profile",
                "s",
                "--source-native-id",
                "native",
                "--range",
                "0:1",
                "--json",
            ]
        )
        == 1
    )
    assert output(capsys)["status"] == "blocked"
    assert not (tmp_path / "absent").exists()


def test_derived_assessment_preserves_v2_baseline_and_human_relation(tmp_path, monkeypatch, capsys):
    from session_migrate.catalog import Catalog
    from session_migrate.profiles import ProfileStore

    prefix, flags, destination, state, document, _ = setup_cli(tmp_path, monkeypatch)
    editor(monkeypatch, document)
    assert (
        main(
            prefix
            + ["handoff", "summary"]
            + flags
            + destination
            + ["--range", "0:1", "--edit-export", str(document), "--confirm"]
        )
        == 0
    )
    output(capsys)
    view = LineageStore.inspect_existing(state / "lineage-v1")
    operation_id = view.operations[0].record.operation_id
    with Catalog(Path(prefix[1])) as catalog:
        target = ProfileStore(Path(flags[1])).load()[1]
        catalog.add_root(target.format, target.root)
        catalog.refresh(include_auto=False)
    args = prefix + ["assess", "--operation-id", operation_id, "--profiles", flags[1]]
    assert main(args + ["--json"]) == 0
    result = output(capsys)
    assert result["schema_version"] == 2 and result["baseline"]["schema_version"] == 1
    assert result["baseline"]["reason"] == "derived_handoff_not_full_history"
    assert result["baseline"]["status"] == "untracked"
    assert result["baseline"]["relation_matches"] is None
    assert result["derivation"]["input"]["document_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert result["update_available"] is False
    assert main(args) == 0
    text = capsys.readouterr().out
    assert "assessment_schema: 2" in text and "derived_handoff_not_full_history" in text


@pytest.mark.parametrize("mode", ["bad_digest", "source_version", "missing_parent"])
def test_no_fresh_proof_failure_cannot_replay_publication(tmp_path, monkeypatch, capsys, mode):
    prefix, flags, destination, state, document, _ = setup_cli(tmp_path, monkeypatch)
    editor(monkeypatch, document, digest="not-a-sha" if mode == "bad_digest" else None)
    extra = ["--range", "0:1", "--edit-export", str(document), "--confirm"]
    if mode == "source_version":
        extra += ["--source-client-version", "future-unproved"]
    command = (
        ["handoff", "summary"]
        if mode != "missing_parent"
        else ["return-new", "--content-mode", "summary", "--parent-operation-id", "absent-parent"]
    )
    assert main(prefix + command + flags + destination + extra) == 1
    assert output(capsys)["status"] == "blocked"
    assert not state.exists() and not (tmp_path / "target").exists()


def test_actual_publication_needs_reconcile_keeps_operation_id_and_does_not_echo_error(
    tmp_path, monkeypatch, capsys
):
    from session_migrate import operations
    from session_migrate.errors import SessionMigrateError

    prefix, flags, destination, state, document, _ = setup_cli(tmp_path, monkeypatch)
    editor(monkeypatch, document)

    def fail(*args):
        raise SessionMigrateError("synthetic-private-document-must-not-echo")

    monkeypatch.setattr(operations, "_completion_record", fail)
    assert (
        main(
            prefix
            + ["handoff", "summary"]
            + flags
            + destination
            + ["--range", "0:1", "--edit-export", str(document), "--confirm"]
        )
        == 1
    )
    result = output(capsys)
    assert result["status"] == "needs_reconcile"
    view = LineageStore.inspect_existing(state / "lineage-v1")
    assert len(view.operations) == 1
    assert view.operations[0].record.operation_id == result["operation_id"]
    assert view.operations[0].record.target_namespace.native_id == result["target_id"]
    assert result["ledger_recorded"] and result["references"]
    assert (tmp_path / "target").exists()
    assert "synthetic-private-document-must-not-echo" not in json.dumps(result)


@pytest.mark.parametrize("error_type", [TypeError, OSError, RuntimeError])
def test_after_real_commit_serialization_failure_is_uncertain_same_id(
    tmp_path, monkeypatch, capsys, error_type
):
    from session_migrate.operations import TransferReceipt

    prefix, flags, destination, state, document, _ = setup_cli(tmp_path, monkeypatch)
    editor(monkeypatch, document)

    def fail(receipt):
        raise error_type("private-body-must-not-echo")

    monkeypatch.setattr(TransferReceipt, "to_dict", fail)
    assert (
        main(
            prefix
            + ["handoff", "summary"]
            + flags
            + destination
            + ["--range", "0:1", "--edit-export", str(document), "--confirm"]
        )
        == 1
    )
    result = output(capsys)
    assert result["status"] == "persistence_uncertain"
    view = LineageStore.inspect_existing(state / "lineage-v1")
    assert len(view.operations) == 1
    assert view.operations[0].record.operation_id == result["operation_id"]
    assert view.operations[0].record.target_namespace.native_id == result["target_id"]
    assert "private-body-must-not-echo" not in json.dumps(result)
