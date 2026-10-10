"""Exact OpenCode v2 title oracle using only public, offline session commands.

A list plus a separate cold export verifies native title persistence, not model
continuation. The source is the synthetic Claude 2.1.209 fixture, never a user
store. The exact-client CI selector rejects skips from this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
from native_corpus.route_oracle import expected_semantic_signature, normalize_source_session

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import claude, opencode
from session_migrate.model import Session, TargetFormat
from session_migrate.titles import TitlePolicy

_FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
_STOCK_SHA256 = {
    "2.0.22": "32cf5aa0a69a650e36277e3315d189835ddc79fb9aa1d0aef5025be5af5ad122",
    "2.0.23": "a233de331f4438336e604d8e006fdab18e5911c4ece16de8e502eb61394c3586",
}
_TITLE = "Título nativo · Ação 🧪 日本語 — Ελληνικά"
_MAX_COMMAND_BYTES = 4 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, repr=False)
class _NativeClient:
    version: str
    sha256: str
    run: Callable[[list[str]], str]


@pytest.fixture
def native_client(tmp_path: Path) -> _NativeClient:
    selected = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V2")
    if not selected:
        pytest.skip("set SESSION_MIGRATE_TEST_OPENCODE_V2 to the exact stock 2.0.22 or 2.0.23 ELF")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is required for the offline exact-title oracle")
    binary = Path(selected).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail("native title binary is unavailable", pytrace=False)
    if binary.stat().st_size > 256 * 1024 * 1024:
        pytest.fail("native title binary exceeds the safety limit", pytrace=False)
    digest = _sha256(binary)
    if digest not in _STOCK_SHA256.values():
        pytest.fail("native title binary does not match an exact stock checksum", pytrace=False)
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
    }
    for name in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        folder = tmp_path / name.lower()
        folder.mkdir(mode=0o700)
        environment[name] = str(folder)
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    command = [
        bwrap,
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/storage",
        "--tmpfs",
        "/root",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(tmp_path),
        str(tmp_path),
        "--ro-bind",
        str(binary),
        "/tmp/opencode-native-title",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        str(project),
        "--",
        "/tmp/opencode-native-title",
    ]

    def run(arguments: list[str]) -> str:
        # Each call creates a new process; output never goes to logs or provider code.
        try:
            completed = subprocess.run(
                command + arguments,
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired):
            pytest.fail("native title command failed or timed out", pytrace=False)
        if completed.returncode != 0:
            pytest.fail("native title command returned a failure", pytrace=False)
        if max(len(completed.stdout.encode()), len(completed.stderr.encode())) > _MAX_COMMAND_BYTES:
            pytest.fail("native title command output exceeds the safety limit", pytrace=False)
        return completed.stdout

    version = run(["--version"]).strip().removeprefix("opencode v")
    if version not in _STOCK_SHA256 or _STOCK_SHA256[version] != digest:
        pytest.fail("native title version/checksum mismatch", pytrace=False)
    return _NativeClient(version, digest, run)


def _portable(session: Session):
    # Reuse the reviewed route contract: include tool payloads/media and ordered
    # semantics, rather than Event equality (payload has compare=False).
    return expected_semantic_signature(normalize_source_session(session), "opencode")


def _assert_public_title(
    tmp_path: Path,
    client: _NativeClient,
    source: Session,
    native_bytes: bytes,
    session_id: str,
    title: str,
) -> None:
    generated = json.loads(native_bytes)
    assert generated["info"]["id"] == session_id
    assert generated["info"]["title"] == title
    source_path = source.source_path
    source_bytes = source_path.read_bytes()
    source_hash = _sha256(source_path)
    source_state = copy.deepcopy(asdict(source))
    fixture_hash = _sha256(_FIXTURE)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(native_bytes)
    generated_session = opencode.parse_session(bundle_path)
    assert _portable(generated_session) == _portable(source)
    initial = json.loads(client.run(["session", "list", "--format", "json", "--standalone"]))
    assert initial == []
    confirmation = client.run(
        [
            "session",
            "import",
            str(bundle_path),
            "--directory",
            str(tmp_path / "project"),
            "--standalone",
        ]
    )
    assert confirmation.startswith("Imported session: ") and session_id in confirmation
    listed = json.loads(client.run(["session", "list", "--format", "json", "--standalone"]))
    selected = [entry for entry in listed if entry.get("id") == session_id]
    assert len(selected) == 1
    assert selected[0]["title"] == title
    # This standalone export is another process and reopens the persisted native store.
    cold_bytes = client.run(["session", "export", session_id, "--standalone"]).encode("utf-8")
    cold = json.loads(cold_bytes)
    assert cold["info"]["id"] == session_id
    assert cold["info"]["title"] == title
    cold_path = tmp_path / "cold-export.json"
    cold_path.write_bytes(cold_bytes)
    replay = opencode.parse_session(cold_path)
    assert replay.session_id == session_id
    assert replay.title == title
    assert _portable(replay) == _portable(source)
    assert _portable(replay) == _portable(generated_session)
    assert asdict(source) == source_state
    assert source.source_path == source_path
    assert source_path.read_bytes() == source_bytes
    assert _sha256(source_path) == source_hash == source.source_sha256
    assert _sha256(_FIXTURE) == fixture_hash
    assert bundle_path.read_bytes() == native_bytes


def test_stock_opencode_v2_title_public_picker_and_cold_export(
    tmp_path: Path,
    native_client: _NativeClient,
) -> None:
    source_path = tmp_path / "source.jsonl"
    source_path.write_bytes(_FIXTURE.read_bytes())
    source = claude.parse(source_path)
    source_state = copy.deepcopy(asdict(source))
    target_uuid = uuid.uuid4()
    session_id = opencode.session_id_from_uuid(str(target_uuid))
    assert session_id == f"ses_{target_uuid.hex}" and str(target_uuid) != source.session_id
    native_bytes, _ = opencode.serialize(
        source,
        session_id=session_id,
        cwd=tmp_path / "project",
        cli_version=native_client.version,
        provider_id="synthetic-unused-provider",
        model_id="synthetic-unused-model",
        title=_TITLE,
    )
    assert asdict(source) == source_state
    _assert_public_title(tmp_path, native_client, source, native_bytes, session_id, _TITLE)


def test_stock_opencode_v2_conversion_title_public_picker_and_cold_export(
    tmp_path: Path,
    native_client: _NativeClient,
) -> None:
    source_path = tmp_path / "source.jsonl"
    source_path.write_bytes(_FIXTURE.read_bytes())
    source = claude.parse(source_path)
    source_state = copy.deepcopy(asdict(source))
    target_uuid = uuid.uuid4()
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.OPENCODE,
            session_id=str(target_uuid),
            cwd=tmp_path / "project",
            target_cli_version=native_client.version,
            model_provider="synthetic-unused-provider",
            model="synthetic-unused-model",
            target_title=_TITLE,
            title_policy=TitlePolicy.NATIVE,
        ),
        environ={},
    )
    assert artifact.source is source
    assert asdict(source) == source_state
    assert artifact.session_id == f"ses_{target_uuid.hex}"
    assert str(target_uuid) != source.session_id
    assert artifact.effective_target_title == _TITLE
    assert artifact.title_plan is not None
    assert artifact.title_plan.requested == _TITLE
    assert artifact.title_plan.native_effective == _TITLE
    assert artifact.title_plan.policy == TitlePolicy.NATIVE
    assert artifact.title_plan.capability_status == "native_verified"
    assert artifact.title_plan.proof_version == native_client.version
    assert artifact.title_plan.intended_persistence == "pending_native_import"
    _assert_public_title(
        tmp_path,
        native_client,
        source,
        artifact.native_bytes,
        artifact.session_id,
        _TITLE,
    )
