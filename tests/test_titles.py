import copy
import hashlib
import json
import os
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_migrate import conversion, titles
from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity, claude, copilot, opencode
from session_migrate.model import (
    AgentFormat,
    TargetFormat,
)
from session_migrate.titles import (
    AliasIdentity,
    TitleAliasStore,
    TitlePolicy,
    TitleReceipt,
    TitleReceiptBinding,
    plan_target_title,
    preflight_title_receipt,
    title_receipt_path,
    validate_title,
    write_title_receipt,
)

TARGET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TITLE = "  Título e\u0301 · 👩‍💻 日本語 [bold]  "


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.jsonl"
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": TARGET_ID,
            "cwd": str(tmp_path),
            "version": claude.PINNED_CLAUDE_VERSION,
            "timestamp": "2026-10-10T10:00:00Z",
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": TARGET_ID,
            "cwd": str(tmp_path),
            "version": claude.PINNED_CLAUDE_VERSION,
            "timestamp": "2026-10-10T10:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "answer"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file": "synthetic"},
                    },
                ],
            },
        },
        {"type": "custom-title", "sessionId": TARGET_ID, "customTitle": "Original synthetic name"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    return conversion.load_session(path, AgentFormat.CLAUDE)


@pytest.fixture
def alias(tmp_path):
    root = tmp_path / "native"
    root.mkdir()
    identity = AliasIdentity(AgentFormat.CLAUDE, root, TARGET_ID)
    store = TitleAliasStore(tmp_path / "private" / "title-aliases" / "v1")
    return store, identity


@pytest.mark.parametrize("value", [TITLE, "a" * 1024, "🧪" * 256, "e\u0301", "a\u200db"])
def test_title_unicode_preserved_exactly(value):
    assert validate_title(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        " \t",
        None,
        1,
        "a\x00b",
        "a\x1bb",
        "a\x7fb",
        "a\x85b",
        "a\nb",
        "a\rb",
        "a\u2028b",
        "a\u2029b",
        "a\u202eb",
        "a\u2066b",
        "a\u200fb",
        "\ud800",
        "a" * 1025,
        "🧪" * 257,
    ],
)
def test_invalid_titles_fail_without_echoing_value(value):
    with pytest.raises(SessionMigrateError) as caught:
        validate_title(value)
    if isinstance(value, str) and value:
        assert value not in str(caught.value)


def test_options_explicit_policy_and_default_contract():
    options = ConversionOptions("opencode", target_title=TITLE, title_policy="local_alias")
    assert options.title_policy is TitlePolicy.LOCAL_ALIAS
    with pytest.raises(SessionMigrateError, match="explicit target title"):
        ConversionOptions("claude", title_policy=TitlePolicy.LOCAL_ALIAS)
    with pytest.raises(SessionMigrateError, match="policy"):
        ConversionOptions("claude", target_title="valid", title_policy="automatic")


@pytest.mark.parametrize("target", ["claude", "codex", "omp", "kilo", "copilot", "antigravity"])
def test_unproved_native_override_fails_before_writer(source, monkeypatch, target):
    module = getattr(conversion, target)
    monkeypatch.setattr(module, "serialize", lambda *a, **kw: pytest.fail("writer called"))
    with pytest.raises(SessionMigrateError, match="not verified"):
        convert_session(source, ConversionOptions(target, target_title=TITLE))


@pytest.mark.parametrize(
    "version", [None, "1.17.20", "2.0.21", "2.0.24", "2.0.99", "2.0.22-beta", "2.0.23-beta"]
)
def test_unknown_native_opencode_version_never_inherits_proof(source, monkeypatch, version):
    monkeypatch.setattr(opencode, "serialize", lambda *a, **kw: pytest.fail("writer called"))
    with pytest.raises(SessionMigrateError, match="not verified"):
        convert_session(
            source, ConversionOptions("opencode", target_title=TITLE, target_cli_version=version)
        )


@pytest.mark.parametrize("version", ["2.0.22", "2.0.23"])
def test_opencode_override_keeps_original_source_payload_hash_and_provenance(source, version):
    original = copy.deepcopy(asdict(source))
    original_bytes = source.source_path.read_bytes()
    artifact = convert_session(
        source,
        ConversionOptions(
            "opencode", session_id=TARGET_ID, target_cli_version=version, target_title=TITLE
        ),
    )
    data = json.loads(artifact.native_bytes)
    assert data["info"]["title"] == TITLE
    assert artifact.effective_target_title == TITLE
    assert artifact.source is source
    assert asdict(source) == original
    assert source.source_path.read_bytes() == original_bytes
    assert (
        artifact.manifest(output_path="opencode:synthetic")["source"]["sha256"]
        == source.source_sha256
    )
    assert artifact.manifest(output_path="opencode:synthetic")["schema_version"] == 2
    assert artifact.title_plan.source_title_disposition == "replaced_by_request"
    assert artifact.title_plan.proof_version == version
    assert artifact.title_plan.proof_id == f"opencode-{version}-public-list-cold-export"
    assert TitleReceipt.pending(artifact.title_plan).persistence == "pending_native_import"


def _convert_fixed(source, options, monkeypatch):
    count = iter(range(1, 1000))
    with monkeypatch.context() as patch:
        patch.setattr(
            uuid,
            "uuid4",
            lambda: uuid.UUID(
                bytes=hashlib.sha256(str(next(count)).encode()).digest()[:16], version=4
            ),
        )
        patch.setattr(conversion, "_utc_now", lambda: "2026-10-10T12:00:00Z")
        artifact = convert_session(source, options, environ={"HOME": str(source.cwd)})
        return artifact, artifact.manifest(output_path=source.cwd / "output")


@pytest.mark.parametrize("target", list(TargetFormat))
def test_no_override_conversion_and_manifest_parity_all_targets(source, monkeypatch, target):
    normal, manifest = _convert_fixed(
        source, ConversionOptions(target, session_id=TARGET_ID), monkeypatch
    )
    explicit, explicit_manifest = _convert_fixed(
        source,
        ConversionOptions(target, session_id=TARGET_ID, target_title=None, title_policy=None),
        monkeypatch,
    )
    assert normal.native_bytes == explicit.native_bytes
    assert normal.dropped == explicit.dropped
    assert normal.warnings == explicit.warnings
    assert manifest == explicit_manifest
    assert normal.title_plan is None
    assert normal.effective_target_title == source.title
    assert set(manifest["target"]) == {
        "format",
        "path",
        "sha256",
        "session_id",
        "cli_version",
        "cwd",
        "timestamp",
        "records",
    }


@pytest.mark.parametrize("target", [TargetFormat.CLAUDE, TargetFormat.CODEX, TargetFormat.OMP])
def test_alias_does_not_modify_transcript_or_hide_title_losses(source, monkeypatch, target):
    options = ConversionOptions(target, session_id=TARGET_ID)
    normal, manifest = _convert_fixed(source, options, monkeypatch)
    alias, alias_manifest = _convert_fixed(
        source,
        replace(options, target_title=TITLE, title_policy=TitlePolicy.LOCAL_ALIAS),
        monkeypatch,
    )
    assert alias.native_bytes == normal.native_bytes
    assert alias_manifest == manifest
    assert alias.dropped == normal.dropped
    assert alias.title_plan.local_alias == TITLE
    assert alias.effective_target_title == source.title
    assert TitleReceipt.pending(alias.title_plan).persistence == "alias_pending"
    if target == TargetFormat.CODEX:
        assert alias.dropped["session:title"] == 1
        assert alias.title_plan.source_title_disposition == "omitted"
    elif target == TargetFormat.CLAUDE:
        assert alias.title_plan.native_effective == source.title


def test_receipt_never_confuses_plan_with_observation():
    native = plan_target_title("opencode", requested=TITLE, cli_version="2.0.22")
    pending = TitleReceipt.pending(native)
    assert pending.to_dict()["verification_state"] == "pending"
    assert pending.to_dict()["native_observed"] is None
    verified = TitleReceipt.verified_native(native, observed=TITLE, cli_version="2.0.22")
    assert verified.to_dict()["persistence"] == "native_verified"
    for observed, version in [("different", "2.0.22"), (TITLE, "2.0.23")]:
        with pytest.raises(SessionMigrateError, match="observed persistence"):
            TitleReceipt.verified_native(native, observed=observed, cli_version=version)
    with pytest.raises(SessionMigrateError):
        TitleReceipt.published_alias(native)
    local = plan_target_title("codex", requested=TITLE, cli_version=None, policy="local_alias")
    saved = TitleReceipt.published_alias(local)
    assert saved.alias_saved
    assert saved.native_observed is None
    document = saved.to_dict()
    document["title_plan"]["requested"] = "changed output copy"
    assert saved.plan.requested == TITLE


def test_alias_preflight_is_readonly_and_publication_private(alias):
    store, identity = alias
    path = store.preflight(identity)
    assert not store.directory.exists()
    record = store.create(identity, TITLE, proposal_id="proposal-1", target_verified=True)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1
    assert store.load(identity).to_dict() == record.to_dict()
    assert path.name == f"{identity.key}.json"
    assert set(json.loads(path.read_bytes())) == {
        "schema_version",
        "identity",
        "alias",
        "created_at",
        "proposal_id",
        "native_id_verified",
    }
    assert not list(store.directory.glob("*.tmp"))
    original = path.read_bytes()
    with pytest.raises(SessionMigrateError, match="no overwrite"):
        store.create(identity, "replacement", proposal_id="proposal-2", target_verified=True)
    assert path.read_bytes() == original


def test_alias_requires_verified_target_without_creating_storage(alias):
    store, identity = alias
    with pytest.raises(SessionMigrateError, match="verified installed"):
        store.create(identity, TITLE, proposal_id="proposal", target_verified=False)
    assert not store.directory.exists()


def test_alias_same_id_two_roots_and_renamed_profile(alias, tmp_path):
    store, identity = alias
    second = tmp_path / "other-native"
    second.mkdir()
    other = AliasIdentity(identity.format, second, identity.native_id)
    assert identity.key != other.key
    store.create(identity, "first name", proposal_id="first", target_verified=True)
    store.create(other, "second name", proposal_id="second", target_verified=True)
    # Profile display name is intentionally not part of root-qualified identity.
    renamed_profile_identity = AliasIdentity(identity.format, identity.root, identity.native_id)
    assert store.load(renamed_profile_identity).alias == "first name"
    assert store.load(other).alias == "second name"


def test_alias_regular_database_root_supported(tmp_path):
    root = tmp_path / "mastra.db"
    root.write_bytes(b"synthetic root identity only")
    identity = AliasIdentity("mastracode", root, TARGET_ID)
    store = TitleAliasStore(tmp_path / "aliases")
    store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    assert store.load(identity).alias == TITLE


def test_replaced_root_never_reapplies_alias(alias):
    store, identity = alias
    store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    identity.root.rename(identity.root.with_name("old-native"))
    identity.root.mkdir()
    with pytest.raises(SessionMigrateError, match="root identity changed"):
        store.load(identity)
    replacement = AliasIdentity(identity.format, identity.root, identity.native_id)
    assert replacement.key == identity.key
    with pytest.raises(SessionMigrateError, match="identity"):
        store.load(replacement)


@pytest.mark.parametrize(
    "shape", ["symlink", "ancestor-symlink", "fifo", "mode", "hardlink", "owner"]
)
def test_alias_untrusted_file_shapes_rejected(alias, tmp_path, monkeypatch, shape):
    store, identity = alias
    store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    path = store.path_for(identity)
    if shape == "symlink":
        path.rename(tmp_path / "real-record")
        path.symlink_to(tmp_path / "real-record")
    elif shape == "ancestor-symlink":
        store.directory.rename(store.directory.with_name("real-directory"))
        store.directory.symlink_to(
            store.directory.with_name("real-directory"), target_is_directory=True
        )
    elif shape == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    elif shape == "mode":
        path.chmod(0o644)
    elif shape == "hardlink":
        os.link(path, tmp_path / "additional-link")
    else:
        real_getuid = os.getuid
        monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)
    with pytest.raises(SessionMigrateError):
        store.load(identity)


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "boolean-version",
        "identity",
        "unknown-key",
        "unverified",
        "duplicate-key",
        "too-large",
        "invalid-json",
    ],
)
def test_alias_invalid_schema_or_identity_rejected(alias, change):
    store, identity = alias
    store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    path = store.path_for(identity)
    row = json.loads(path.read_bytes())
    if change == "schema":
        row["schema_version"] = 99
    elif change == "boolean-version":
        row["schema_version"] = True
    elif change == "identity":
        row["identity"]["native_id"] = "different"
    elif change == "unknown-key":
        row["body"] = "unrecognized synthetic field"
    elif change == "unverified":
        row["native_id_verified"] = False
    data = json.dumps(row).encode()
    if change == "duplicate-key":
        data = b'{"schema_version":1,"schema_version":1}'
    elif change == "too-large":
        data = b" " * (titles.MAX_TITLE_RECORD_BYTES + 1)
    elif change == "invalid-json":
        data = b"{"
    path.write_bytes(data)
    with pytest.raises(SessionMigrateError):
        store.load(identity)


def test_alias_insecure_directory_and_traversal_no_residue(alias, tmp_path):
    store, identity = alias
    store.directory.mkdir(parents=True, mode=0o755)
    store.directory.chmod(0o755)
    with pytest.raises(SessionMigrateError, match="0700"):
        store.preflight(identity)
    with pytest.raises(SessionMigrateError, match="traversal"):
        TitleAliasStore(tmp_path / "private" / ".." / "escape")
    with pytest.raises(SessionMigrateError, match="traversal"):
        AliasIdentity(identity.format, identity.root / ".." / identity.root.name, TARGET_ID)
    assert not (tmp_path / "escape").exists()


def test_symlinked_root_rejected_before_storage(alias, tmp_path):
    store, identity = alias
    linked = tmp_path / "linked-root"
    linked.symlink_to(identity.root, target_is_directory=True)
    with pytest.raises(SessionMigrateError, match="symlink"):
        AliasIdentity(identity.format, linked, TARGET_ID)
    assert not store.directory.exists()


def test_alias_concurrent_no_clobber_one_winner(alias):
    store, identity = alias

    def create(name):
        try:
            store.create(identity, name, proposal_id=name, target_verified=True)
            return name
        except SessionMigrateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["first", "second"]))
    assert len([name for name in results if name]) == 1
    assert store.load(identity).alias == next(name for name in results if name)
    assert not list(store.directory.glob("*.tmp"))


def test_alias_publication_failure_cleans_owned_staging(alias, monkeypatch):
    store, identity = alias

    def fail(_fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(SessionMigrateError, match="publication failed"):
        store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    assert list(store.directory.iterdir()) == []


def test_alias_final_race_preserves_competing_record(alias, monkeypatch):
    store, identity = alias
    real_link = os.link
    path = store.path_for(identity)

    def competing_link(*args, **kwargs):
        path.write_bytes(b"competing synthetic record")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", competing_link)
    with pytest.raises(SessionMigrateError, match="no overwrite"):
        store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    assert path.read_bytes() == b"competing synthetic record"
    assert list(store.directory.iterdir()) == [path]


def test_root_replacement_during_publication_removes_own_alias(alias, monkeypatch):
    store, identity = alias
    real_link = os.link

    def replace_root(*args, **kwargs):
        result = real_link(*args, **kwargs)
        identity.root.rename(identity.root.with_name("old-root"))
        identity.root.mkdir()
        return result

    monkeypatch.setattr(os, "link", replace_root)
    with pytest.raises(SessionMigrateError, match="root identity changed"):
        store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    assert list(store.directory.iterdir()) == []


def test_receipt_preflight_no_write_and_explicit_exclusive_publication(tmp_path):
    manifest = tmp_path / "private" / "manifest.json"
    path = title_receipt_path(manifest)
    preflight_title_receipt(path)
    assert not path.parent.exists()
    plan = plan_target_title("codex", requested=TITLE, cli_version=None, policy="local_alias")
    pending = TitleReceipt.pending(plan)
    output = tmp_path / "output.jsonl"
    output.write_bytes(b"synthetic converted artifact")
    binding = TitleReceiptBinding(
        "codex", hashlib.sha256(output.read_bytes()).hexdigest(), output_path=output
    )
    write_title_receipt(path, pending, binding=binding)
    assert json.loads(path.read_bytes())["persistence"] == "alias_pending"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(SessionMigrateError, match="no overwrite"):
        write_title_receipt(path, pending, binding=binding)
    assert not manifest.exists()


def test_planned_alias_future_root_preflight_then_postinstall_binding(tmp_path):
    root = tmp_path / "future" / "native"
    identity = AliasIdentity.planned("codex", root, TARGET_ID)
    store = TitleAliasStore(tmp_path / "aliases")
    assert not identity.bound
    assert identity.to_dict()["root_inode"] is None
    store.preflight(identity)
    assert not root.parent.exists()
    assert not store.directory.exists()
    with pytest.raises(SessionMigrateError):
        store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    assert not store.directory.exists()
    root.mkdir(parents=True)
    record = store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    assert record.identity.bound
    assert record.identity.root_inode == root.stat().st_ino
    assert store.load(identity).alias == TITLE


def test_planned_future_root_does_not_accept_symlink_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SessionMigrateError, match="symlink"):
        AliasIdentity.planned("codex", link / "future", TARGET_ID)


def test_canonical_root_namespace_keeps_single_alias_key(alias):
    store, identity = alias
    equivalent = AliasIdentity(identity.format, Path("/" + str(identity.root)), identity.native_id)
    assert equivalent.root == identity.root
    assert equivalent.key == identity.key
    store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    assert store.load(equivalent).alias == TITLE
    with pytest.raises(SessionMigrateError, match="no overwrite"):
        store.create(equivalent, "other", proposal_id="other", target_verified=True)


def test_native_receipt_requires_explicit_bound_identity_and_matching_proof(tmp_path):
    root = tmp_path / "opencode"
    root.mkdir()
    identity = AliasIdentity("opencode", root, f"ses_{TARGET_ID.replace('-', '')}")
    binding = TitleReceiptBinding("opencode", "a" * 64, identity=identity, proposal_id="proposal")
    native = plan_target_title("opencode", requested=TITLE, cli_version="2.0.22")
    receipt = TitleReceipt.verified_native(native, observed=TITLE, cli_version="2.0.22")
    path = tmp_path / "state" / "receipt.json"
    with pytest.raises(SessionMigrateError, match="explicit target binding"):
        write_title_receipt(path, receipt, binding=None)
    assert not path.parent.exists()
    write_title_receipt(path, receipt, binding=binding)
    row = json.loads(path.read_bytes())
    assert row["binding"]["identity"] == identity.to_dict()
    assert row["binding"]["output_path"] is None
    assert row["persistence"] == "native_verified"
    fake = replace(native, proof_id="not the registered proof")
    with pytest.raises(SessionMigrateError, match="matching client proof"):
        write_title_receipt(
            tmp_path / "different" / "receipt.json",
            TitleReceipt.verified_native(fake, observed=TITLE, cli_version="2.0.22"),
            binding=binding,
        )
    assert not (tmp_path / "different").exists()


def test_convert_receipt_rejects_installed_claim_or_output_hash_drift(tmp_path):
    output = tmp_path / "converted.json"
    output.write_bytes(b"synthetic converted bundle")
    binding = TitleReceiptBinding(
        "opencode", hashlib.sha256(output.read_bytes()).hexdigest(), output_path=output
    )
    plan = plan_target_title("opencode", requested=TITLE, cli_version="2.0.22")
    path = tmp_path / "state" / "receipt.json"
    with pytest.raises(SessionMigrateError, match="cannot claim installed"):
        write_title_receipt(
            path,
            TitleReceipt.verified_native(plan, observed=TITLE, cli_version="2.0.22"),
            binding=binding,
        )
    output.write_bytes(b"changed synthetic bundle")
    with pytest.raises(SessionMigrateError, match="different hash"):
        write_title_receipt(path, TitleReceipt.pending(plan), binding=binding)
    assert not path.parent.exists()


def test_published_alias_receipt_requires_actual_private_record(alias, tmp_path):
    store, identity = alias
    binding = TitleReceiptBinding("claude", "a" * 64, identity=identity, proposal_id="proposal")
    plan = plan_target_title("claude", requested=TITLE, cli_version=None, policy="local_alias")
    receipt = TitleReceipt.published_alias(plan)
    path = tmp_path / "receipt" / "title.json"
    with pytest.raises(SessionMigrateError, match="alias evidence"):
        write_title_receipt(path, receipt, binding=binding)
    store.create(identity, TITLE, proposal_id="proposal", target_verified=True)
    write_title_receipt(path, receipt, binding=binding, alias_store=store)
    assert json.loads(path.read_bytes())["alias_saved"] is True
    changed = replace(binding, proposal_id="different-proposal")
    with pytest.raises(SessionMigrateError, match="inconsistent"):
        write_title_receipt(
            tmp_path / "missing" / "receipt.json", receipt, binding=changed, alias_store=store
        )
    assert not (tmp_path / "missing").exists()


def test_copilot_sidecar_uses_effective_target_title_legacy_fallback(source, tmp_path, monkeypatch):
    artifact = convert_session(source, ConversionOptions("copilot", session_id=TARGET_ID))
    seen = []
    original = copilot.workspace_bytes

    def workspace(**kwargs):
        seen.append(kwargs["title"])
        return original(**kwargs)

    monkeypatch.setattr(copilot, "workspace_bytes", workspace)
    events, _manifest = conversion.install_copilot_artifact(
        artifact, target_home=tmp_path / "copilot"
    )
    assert events.is_file()
    assert seen == [source.title]
    assert artifact.source is source


def test_antigravity_preflight_and_install_use_effective_title_fallback(
    source, tmp_path, monkeypatch
):
    artifact = convert_session(source, ConversionOptions("antigravity", session_id=TARGET_ID))
    native_path, _manifest = conversion.target_import_paths(artifact, tmp_path / "antigravity")
    seen = []

    def install(*args, **kwargs):
        seen.append((kwargs["title"], kwargs.get("dry_run", False)))
        return SimpleNamespace(conversation_path=native_path)

    monkeypatch.setattr(antigravity, "install_database", install)
    conversion.install_antigravity_artifact(
        artifact, target_home=tmp_path / "antigravity", dry_run=True
    )
    conversion.install_antigravity_artifact(artifact, target_home=tmp_path / "antigravity")
    assert seen == [(source.title, True), (source.title, False)]
    assert artifact.source is source
