"""Namespace qualification uses disposable native layouts and metadata only."""

from dataclasses import replace

import pytest
from test_operations import FIXTURE, TARGET_ID

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    load_session,
    target_import_paths,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.native_namespaces import (
    _bind_execution_context,
    qualify_namespace,
    reserve_namespace,
)
from session_migrate.native_snapshots import SourceCaptureContext, capture_source

HASH = "a" * 64


def test_target_root_and_source_root_use_the_same_qualified_namespace(tmp_path):
    source = load_session(FIXTURE, AgentFormat.CLAUDE)
    artifact = convert_session(
        source, ConversionOptions(TargetFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path)
    )
    root = tmp_path / "native"
    path, _ = target_import_paths(artifact, root)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes(artifact.native_bytes)
    observed = load_session(path, AgentFormat.CLAUDE)
    reserved = reserve_namespace(AgentFormat.CLAUDE, root, observed.session_id)
    snapshot = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, path))
    context = _bind_execution_context(
        reserved, observed, native_path=path, environ={}, snapshot=snapshot
    )
    first = qualify_namespace(observed, reserved, HASH, native_path=path, execution_context=context)
    second = qualify_namespace(observed, reserved, HASH, execution_context=context)
    assert first == second
    assert first.namespace.canonical_root == str(root)
    assert first.namespace.canonical_root != str(path.parent)


def test_portable_path_and_same_id_in_other_root_do_not_qualify(tmp_path):
    source = load_session(FIXTURE, AgentFormat.CLAUDE)
    root = tmp_path / "native"
    root.mkdir(mode=0o700)
    reserved = reserve_namespace(AgentFormat.CLAUDE, root, source.session_id)
    assert qualify_namespace(source, reserved, HASH) is None
    portable = tmp_path / "portable.jsonl"
    portable.write_bytes(FIXTURE.read_bytes())
    assert qualify_namespace(load_session(portable, AgentFormat.CLAUDE), reserved, HASH) is None
    assert (
        reserved.lock_key
        != reserve_namespace(AgentFormat.CLAUDE, tmp_path / "different", source.session_id).lock_key
    )


def test_missing_future_root_reserves_without_fabricating_witness(tmp_path):
    source = load_session(FIXTURE, AgentFormat.CLAUDE)
    namespace = reserve_namespace(AgentFormat.CLAUDE, tmp_path / "future", source.session_id)
    assert not (tmp_path / "future").exists()
    assert qualify_namespace(source, namespace, HASH) is None


def test_symlink_root_or_id_disagreement_never_qualifies(tmp_path):
    source = load_session(FIXTURE, AgentFormat.CLAUDE)
    root = tmp_path / "native"
    root.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(SessionMigrateError):
        reserve_namespace(AgentFormat.CLAUDE, alias, source.session_id)
    namespace = reserve_namespace(AgentFormat.CLAUDE, root, TARGET_ID)
    assert qualify_namespace(source, namespace, HASH) is None


def test_opencode_export_context_requires_reserved_virtual_id_and_existing_root(tmp_path):
    source = load_session(FIXTURE, AgentFormat.CLAUDE)
    root = tmp_path / "data" / "opencode"
    root.mkdir(parents=True, mode=0o700)
    native_id = "ses_" + TARGET_ID.replace("-", "")
    session = replace(
        source,
        source_format=AgentFormat.OPENCODE,
        session_id=native_id,
        source_path=type(root)(f"opencode:{native_id}"),
    )
    namespace = reserve_namespace(AgentFormat.OPENCODE, root, native_id)
    assert qualify_namespace(session, namespace, HASH, native_path=session.source_path) is None
    client = tmp_path / "client"
    client.write_bytes(b"synthetic-client")
    with pytest.raises(SessionMigrateError):
        _bind_execution_context(
            namespace,
            session,
            native_path=session.source_path,
            environ={"XDG_DATA_HOME": str(root.parent)},
            client=client,
        )
    assert qualify_namespace(replace(session, source_path=FIXTURE), namespace, HASH) is None


def test_issued_context_mutation_and_file_without_loader_authority_rejected(tmp_path):
    source = load_session(FIXTURE, AgentFormat.CLAUDE)
    artifact = convert_session(
        source, ConversionOptions(TargetFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path)
    )
    root = tmp_path / "native"
    path, _ = target_import_paths(artifact, root)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes(artifact.native_bytes)
    snapshot = capture_source(SourceCaptureContext(AgentFormat.CLAUDE, path))
    observed = snapshot.parse()
    namespace = reserve_namespace(AgentFormat.CLAUDE, root, observed.session_id)
    context = _bind_execution_context(
        namespace, observed, native_path=path, environ={}, snapshot=snapshot
    )
    assert qualify_namespace(observed, namespace, HASH, execution_context=context)
    object.__setattr__(context, "authority_sha256", "b" * 64)
    assert (
        qualify_namespace(
            replace(observed, source_sha256="b" * 64), namespace, HASH, execution_context=context
        )
        is None
    )
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"synthetic-not-a-session")
    fake = replace(
        source, source_format=AgentFormat.PI, source_path=unrelated, source_sha256="c" * 64
    )
    namespace = reserve_namespace(AgentFormat.PI, tmp_path, fake.session_id)
    with pytest.raises(SessionMigrateError, match="issued loader authority"):
        _bind_execution_context(namespace, fake, native_path=unrelated, environ={})


def test_original_parent_traversal_is_rejected_before_normalization(tmp_path):
    with pytest.raises(SessionMigrateError, match="parent traversal"):
        reserve_namespace(AgentFormat.CLAUDE, tmp_path / "native/../other", TARGET_ID)
