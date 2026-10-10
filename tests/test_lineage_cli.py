"""Read-only CLI over actual file publication in private synthetic agent roots."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from session_migrate.catalog import Catalog
from session_migrate.cli import main
from session_migrate.continuity_bindings import PUBLIC_ENVIRONMENT
from session_migrate.conversion import default_migration_state_home
from session_migrate.formats import claude
from session_migrate.lineage import LineageStore
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import OperationRequest, commit_operation, run_operation
from session_migrate.profiles import (
    Profile,
    ProfileStore,
    SessionRef,
    prepare_profile_operation,
)

SOURCE_ID = "60000000-0000-4000-8000-000000000001"
TARGET_ID = "60000000-0000-4000-8000-000000000002"
BODY = "PRIVATE_TRANSCRIPT_BODY_MUST_NEVER_APPEAR_IN_CLI"


@pytest.fixture
def private_env(tmp_path, monkeypatch):
    for key in PUBLIC_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    values = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for key in ("HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        directory = tmp_path / key.lower()
        directory.mkdir(mode=0o700)
        values[key] = str(directory)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    tmp_path.chmod(0o700)
    return values


def rows(count=2):
    return [
        {
            "type": "user" if i % 2 == 0 else "assistant",
            "uuid": f"test-{i}",
            "parentUuid": f"test-{i - 1}" if i else None,
            "sessionId": SOURCE_ID,
            "cwd": "/synthetic/work",
            "version": "2.1.209",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": [{"type": "text", "text": BODY}],
            },
        }
        for i in range(count)
    ]


def write_rows(path, values):
    path.write_text("".join(json.dumps(row) + "\n" for row in values))
    path.chmod(0o600)


def tree_state(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


def publish(tmp_path, environment, kind="profiles", target_id=TARGET_ID):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    source = source_root / "projects" / claude.project_directory_name(work) / f"{SOURCE_ID}.jsonl"
    source.parent.mkdir(parents=True, mode=0o700)
    values = rows()
    for row in values:
        row["cwd"] = str(work)
    write_rows(source, values)
    profiles = (
        Profile("source", "Synthetic source", AgentFormat.CLAUDE, source_root),
        Profile("target", "Synthetic target", AgentFormat.CODEX, target_root),
    )
    catalog_path = tmp_path / "catalog" / "catalog.sqlite"
    with Catalog(catalog_path) as catalog:
        catalog.refresh(claude_roots=(source_root,), include_auto=False)
        options = {
            "target_format": TargetFormat.CODEX,
            "session_id": target_id,
            "cwd": work,
            "target_cli_version": "0.144.4",
        }
        if kind == "profiles":
            ref = SessionRef(
                profiles[0].format,
                source_root,
                SOURCE_ID,
                profiles[0].id,
                profile_fingerprint=profiles[0].fingerprint,
            )
            prepared = prepare_profile_operation(
                catalog,
                profiles[0],
                ref,
                profiles[1],
                OperationRequest("transfer", **options),
                base_environ=environment,
            )
            result = commit_operation(prepared)
        else:
            result = run_operation(
                OperationRequest(
                    "transfer",
                    source_id=SOURCE_ID,
                    source_format=AgentFormat.CLAUDE,
                    source_home=source_root,
                    home=target_root,
                    **options,
                ),
                environ=environment,
            )
        catalog.refresh(claude_roots=(source_root,), codex_roots=(target_root,), include_auto=False)
    assert result.lineage_receipt.continuity_reason == "tracked"
    storage = tmp_path / "profiles"
    storage.mkdir(mode=0o700)
    profile_file = storage / "profiles.json"
    ProfileStore(profile_file).create(profiles)
    state_home = default_migration_state_home(environ=environment)
    view = LineageStore.inspect_existing(state_home / "lineage-v1")
    assert view.operations[0].record.operation_id == result.lineage_receipt.operation_id
    return {
        "state_home": state_home,
        "catalog": catalog_path,
        "profiles": profiles,
        "profiles_file": profile_file,
        "operation_id": result.lineage_receipt.operation_id,
        "source": source,
        "target": Path(
            (result.legacy_result if kind == "profiles" else result.to_dict())["output"]
        ),
        "view": view,
    }


def explicit_profile_arguments(fixture):
    args = arguments(fixture, "assess") + ["--operation-id", fixture["operation_id"], "--json"]
    for side, profile in zip(("source", "target"), fixture["profiles"], strict=True):
        args += [
            f"--{side}-format",
            profile.format.value,
            f"--{side}-root",
            str(profile.root),
            f"--{side}-profile",
            profile.id,
            f"--{side}-profile-name",
            profile.name,
        ]
    return args


def arguments(fixture, command):
    return [
        "--state-home",
        str(fixture["state_home"]),
        "--catalog",
        str(fixture["catalog"]),
        command,
    ]


def assess_arguments(fixture, pair=False):
    result = arguments(fixture, "assess") + ["--profiles", str(fixture["profiles_file"]), "--json"]
    if not pair:
        return result + ["--operation-id", fixture["operation_id"]]
    for side, profile, native in zip(
        ("source", "target"), fixture["profiles"], (SOURCE_ID, TARGET_ID), strict=True
    ):
        result += [
            f"--{side}-format",
            profile.format.value,
            f"--{side}-root",
            str(profile.root),
            f"--{side}-id",
            native,
            f"--{side}-profile",
            profile.id,
        ]
    return result


def output_json(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    assert BODY not in captured.out
    return json.loads(captured.out)


@pytest.mark.parametrize("command", ["inspect", "list"])
def test_missing_ledger_never_creates_namespace(tmp_path, private_env, capsys, command):
    home = tmp_path / "absent" / "state"
    args = ["--state-home", str(home), "lineage", command, "--json"]
    if command == "inspect":
        args += ["--operation-id", "unknown"]
    before = tree_state(tmp_path)
    assert main(args) == 0
    assert output_json(capsys)["reason"] == "ledger_missing"
    assert tree_state(tmp_path) == before
    assert not home.exists()


@pytest.mark.parametrize("kind", ["profiles", "legacy"])
def test_real_receipts_inspect_list_assess_and_pair_are_readonly(
    tmp_path, private_env, capsys, kind
):
    fixture = publish(tmp_path, private_env, kind)
    before = tree_state(tmp_path)
    inspect = arguments(fixture, "lineage") + [
        "inspect",
        "--operation-id",
        fixture["operation_id"],
        "--json",
    ]
    assert main(inspect) == 0
    inspected = output_json(capsys)
    op = inspected["operation"]
    assert op["record"]["operation_id"] == fixture["operation_id"]
    assert op["receipt"]["source_checkpoint_sha256"]
    assert op["receipt"]["target_checkpoint_sha256"]
    assert main(arguments(fixture, "lineage") + ["list", "--json"]) == 0
    listing = output_json(capsys)
    assert listing["total"] == 1 and listing["operations"][0]["state"] == "completed"
    for pair in (False, True):
        args = assess_arguments(fixture, pair)
        if kind == "legacy":
            args += ["--legacy-runtime-context"]
        assert main(args) == 0
        assessment = output_json(capsys)
        assert assessment["status"] == "no_op" and assessment["reason"] == "unchanged"
        assert assessment["update_available"] is False
        assert assessment["actions"] == ["inspect"]
        assert (
            assessment["source_current_profile_fingerprint"] == fixture["profiles"][0].fingerprint
        )
        assert assessment["source_profile_binding_kind"] == (
            "legacy_runtime_v1" if kind == "legacy" else "profile_v1"
        )
        if kind == "legacy":
            assert (
                assessment["source_identity"]["profile_fingerprint"]
                != assessment["source_current_profile_fingerprint"]
            )
    assert tree_state(tmp_path) == before


def test_legacy_is_opt_in_and_keeps_sparse_environment(tmp_path, private_env, capsys):
    assert "XDG_DATA_HOME" not in private_env and "XDG_CONFIG_HOME" not in private_env
    fixture = publish(tmp_path, private_env, "legacy")
    assert main(assess_arguments(fixture)) == 0
    result = output_json(capsys)
    assert result["status"] == "untracked" and result["reason"] == "identity_unproven"
    assert main(assess_arguments(fixture) + ["--legacy-runtime-context"]) == 0
    assert output_json(capsys)["status"] == "no_op"


def test_missing_catalog_does_not_create_it(tmp_path, private_env, capsys):
    path = tmp_path / "absent" / "catalog.sqlite"
    before = tree_state(tmp_path)
    assert main(["--catalog", str(path), "assess", "--operation-id", "unknown", "--json"]) == 0
    result = output_json(capsys)
    assert result["status"] == "untracked" and result["reason"] == "catalog_missing"
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize(
    "args",
    [
        ["assess"],
        ["assess", "--source-id", SOURCE_ID],
        ["assess", "--operation-id", "one", "--source-id", SOURCE_ID],
        ["lineage", "inspect", "--operation-id", "invalid/id"],
        ["lineage", "list", "--limit", "0"],
        ["lineage", "list", "--offset", "-1"],
        ["assess", "--operation-id", "one", "--source-env", "SECRET=/ignored"],
        ["assess", "--operation-id", "one", "--source-profile-fingerprint", "a" * 64],
    ],
)
def test_argument_errors_are_code_two_without_state(args, tmp_path, private_env, capsys):
    before = tree_state(tmp_path)
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code == 2
    capsys.readouterr()
    assert tree_state(tmp_path) == before


def test_invalid_ledger_error_is_code_one_and_content_free(tmp_path, private_env, capsys):
    home = tmp_path / "state"
    ledger = home / "lineage-v1"
    ledger.mkdir(parents=True, mode=0o700)
    (ledger / "invalid").write_text(BODY)
    before = tree_state(tmp_path)
    assert main(["--state-home", str(home), "lineage", "list", "--json"]) == 1
    captured = capsys.readouterr()
    assert "readonly_inspection_failed" in captured.err and BODY not in captured.err
    assert captured.out == "" and tree_state(tmp_path) == before


def test_unknown_operation_and_pagination_are_honest(tmp_path, private_env, capsys):
    fixture = publish(tmp_path, private_env)
    assert (
        main(arguments(fixture, "lineage") + ["inspect", "--operation-id", "unknown", "--json"])
        == 0
    )
    assert output_json(capsys)["reason"] == "operation_not_found"
    assert (
        main(arguments(fixture, "lineage") + ["list", "--offset", "1", "--limit", "1", "--json"])
        == 0
    )
    result = output_json(capsys)
    assert result["operations"] == [] and result["total"] == 1
    assert result["offset"] == 1 and result["operation_limit"] == 4096


def test_human_output_has_qualified_metadata_without_body(tmp_path, private_env, capsys):
    fixture = publish(tmp_path, private_env)
    assert (
        main(arguments(fixture, "lineage") + ["inspect", "--operation-id", fixture["operation_id"]])
        == 0
    )
    text = capsys.readouterr().out
    assert fixture["operation_id"] in text and SOURCE_ID in text and TARGET_ID in text
    assert "source: claude" in text and "target: codex" in text
    assert BODY not in text and "update_available: false" in text


@pytest.mark.parametrize("kind", ["profiles", "legacy"])
def test_explicit_current_profiles_need_no_saved_file(tmp_path, private_env, capsys, kind):
    fixture = publish(tmp_path, private_env, kind)
    args = explicit_profile_arguments(fixture)
    if kind == "legacy":
        args.append("--legacy-runtime-context")
    before = tree_state(tmp_path)
    assert main(args) == 0
    result = output_json(capsys)
    assert result["status"] == "no_op"
    assert result["source_profile_name"] == "Synthetic source"
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize(
    "source_advanced,target_advanced,reason,status",
    [
        (True, False, "native_boundary_unproven", "untracked"),
        (False, True, "target_advanced", "conflict"),
        (True, True, "both_advanced", "conflict"),
    ],
)
def test_advanced_axes_never_offer_update(
    tmp_path, private_env, capsys, source_advanced, target_advanced, reason, status
):
    fixture = publish(tmp_path, private_env)
    if source_advanced:
        values = rows(4)
        for row in values:
            row["cwd"] = str(tmp_path / "work")
        write_rows(fixture["source"], values)
    if target_advanced:
        with fixture["target"].open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": BODY},
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": BODY}],
                        },
                    }
                )
                + "\n"
            )
    before = tree_state(tmp_path)
    assert main(assess_arguments(fixture)) == 0
    result = output_json(capsys)
    assert result["status"] == status and result["reason"] == reason, result["reason"]
    assert result["update_available"] is False and result["actions"] == ["inspect"]
    assert tree_state(tmp_path) == before


def test_changed_prefix_and_missing_native_are_not_proof(tmp_path, private_env, capsys):
    fixture = publish(tmp_path, private_env)
    raw = fixture["source"].read_text()
    fixture["source"].write_text(raw.replace(BODY, "changed"))
    assert main(assess_arguments(fixture)) == 0
    result = output_json(capsys)
    assert result["status"] == "lineage_broken" and result["reason"] == "prefix_changed"
    fixture["source"].rename(fixture["source"].with_suffix(".hidden"))
    before = tree_state(tmp_path)
    assert main(assess_arguments(fixture)) == 0
    result = output_json(capsys)
    assert result["status"] == "untracked"
    assert result["reason"] == "endpoint_unavailable"
    assert result["native_read_effects"] == []
    assert tree_state(tmp_path) == before


def test_profile_changes_do_not_reuse_historical_fingerprint(tmp_path, private_env, capsys):
    fixture = publish(tmp_path, private_env)
    args = explicit_profile_arguments(fixture)
    index = args.index("--source-profile-name") + 1
    args[index] = "Changed current profile"
    assert main(args) == 0
    result = output_json(capsys)
    assert result["status"] == "untracked" and result["reason"] == "identity_unproven"
    saved = assess_arguments(fixture, pair=True) + ["--source-profile-name", "Contradiction"]
    before = tree_state(tmp_path)
    assert main(saved) == 1
    assert "readonly_inspection_failed" in capsys.readouterr().err
    assert tree_state(tmp_path) == before


def test_same_uuid_with_different_formats_keeps_qualified_identity(tmp_path, private_env, capsys):
    fixture = publish(tmp_path, private_env, target_id=SOURCE_ID)
    args = assess_arguments(fixture, pair=True)
    args[args.index("--target-id") + 1] = SOURCE_ID
    assert main(args) == 0
    result = output_json(capsys)
    assert result["status"] == "no_op"
    source, target = result["source_identity"]["namespace"], result["target_identity"]["namespace"]
    assert source["native_id"] == target["native_id"] == SOURCE_ID
    assert source["format"] == "claude" and target["format"] == "codex"
    assert source["canonical_root"] != target["canonical_root"]
    wrong = args.copy()
    wrong[wrong.index("--target-root") + 1] = str(fixture["profiles"][0].root)
    assert main(wrong) == 1
    capsys.readouterr()


def test_state_default_captured_before_profile_home_binding(tmp_path, private_env, capsys):
    fixture = publish(tmp_path, private_env)
    args = explicit_profile_arguments(fixture)
    del args[:2]
    args += ["--source-env", f"HOME={tmp_path / 'separate-profile-home'}"]
    before = tree_state(tmp_path)
    assert main(args) == 0
    result = output_json(capsys)
    assert result["reason"] == "identity_unproven"
    assert result["operation_id"] == fixture["operation_id"]
    assert tree_state(tmp_path) == before


def test_failed_native_read_effects_are_metadata_even_without_observation(
    tmp_path, private_env, monkeypatch, capsys
):
    from session_migrate import continuity
    from session_migrate.continuity_observation import ContinuityObservationError

    fixture = publish(tmp_path, private_env)

    def failed_read(*args, on_read_effect, **kwargs):
        effects = (
            "native_export_read_attempted",
            "private_scratch_possible",
            "native_cache_or_wal_possible",
        )
        for effect in effects:
            on_read_effect(effect)
        raise ContinuityObservationError("endpoint_unavailable", native_read_effects=effects)

    monkeypatch.setattr(continuity, "observe_endpoint", failed_read)
    before = tree_state(tmp_path)
    assert main(assess_arguments(fixture)) == 0
    result = output_json(capsys)
    assert result["status"] == "untracked" and result["reason"] == "endpoint_unavailable"
    assert "native_export_read_attempted" in result["native_read_effects"]
    assert "native_cache_or_wal_possible" in result["native_read_effects"]
    assert "native_export_read" not in result["native_read_effects"]
    assert tree_state(tmp_path) == before
