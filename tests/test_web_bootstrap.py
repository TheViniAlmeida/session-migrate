"""Private launch files never overwrite or clean up changed caller files."""

import errno
import json
import os
import stat

import pytest

from session_migrate.web import bootstrap
from session_migrate.web.auth import RunRegistry, WebAuthError


def setup_launch(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    registry = RunRegistry()
    credential = registry.create("http://127.0.0.1:8021")
    return registry, credential, parent / "launch.json"


def test_exclusive_file_permissions_contents_and_owned_cleanup(tmp_path):
    registry, credential, path = setup_launch(tmp_path)
    owner = bootstrap.write_bootstrap(registry, credential, path)
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    data = json.loads(path.read_bytes())
    assert set(data) == {
        "schema_version",
        "run_id",
        "origin",
        "expires_at",
        "access_token",
        "csrf_nonce",
    }
    assert data["schema_version"] == 1
    assert owner.path == path
    descriptor = owner._fd
    directories = [node[2] for node in owner._directory.nodes]
    assert owner.close() == "removed"
    assert not path.exists()
    assert owner.close() == "already_closed"
    for fd in (descriptor, *directories):
        with pytest.raises(OSError) as failure:
            os.fstat(fd)
        assert failure.value.errno == errno.EBADF


@pytest.mark.parametrize("kind", ["regular", "symlink", "hardlink"])
def test_existing_files_never_overwritten_or_removed(tmp_path, kind):
    registry, credential, path = setup_launch(tmp_path)
    original = path.parent / "original"
    original.write_bytes(b"operator-owned synthetic fixture")
    if kind == "regular":
        path.write_bytes(b"operator-owned synthetic fixture")
    elif kind == "symlink":
        path.symlink_to(original)
    else:
        os.link(original, path)
    before = path.lstat()
    with pytest.raises(WebAuthError, match="bootstrap_write_failed"):
        bootstrap.write_bootstrap(registry, credential, path)
    assert path.read_bytes() == b"operator-owned synthetic fixture"
    assert (path.lstat().st_dev, path.lstat().st_ino) == (before.st_dev, before.st_ino)
    assert original.read_bytes() == b"operator-owned synthetic fixture"


@pytest.mark.parametrize("mutation", ["edited", "replaced", "hardlinked", "chmod"])
def test_cleanup_preserves_changed_file(tmp_path, mutation):
    registry, credential, path = setup_launch(tmp_path)
    owner = bootstrap.write_bootstrap(registry, credential, path)
    if mutation == "edited":
        path.write_bytes(b"edited synthetic fixture")
    elif mutation == "replaced":
        path.rename(path.with_suffix(".old"))
        path.write_bytes(b"replacement synthetic fixture")
    elif mutation == "hardlinked":
        os.link(path, path.with_suffix(".linked"))
    else:
        path.chmod(0o400)
    before = path.read_bytes()
    assert owner.close() == "preserved"
    assert path.exists() and path.read_bytes() == before
    with pytest.raises(OSError):
        os.fstat(owner._fd)


def test_parent_rename_aba_preserves_file(tmp_path):
    registry, credential, path = setup_launch(tmp_path)
    owner = bootstrap.write_bootstrap(registry, credential, path)
    moved = path.parent.with_name("moved")
    path.parent.rename(moved)
    moved.rename(path.parent)
    assert owner.close() == "preserved"
    assert path.exists()


def test_nonprivate_parent_is_not_chmoded(tmp_path):
    registry, credential, path = setup_launch(tmp_path)
    path.parent.chmod(0o755)
    with pytest.raises(WebAuthError, match="bootstrap_directory_unavailable"):
        bootstrap.write_bootstrap(registry, credential, path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert not path.exists()


def test_missing_parent_is_not_created(tmp_path):
    registry, credential, path = setup_launch(tmp_path)
    missing = path.parent / "absent" / "launch.json"
    with pytest.raises(WebAuthError, match="bootstrap_directory_unavailable"):
        bootstrap.write_bootstrap(registry, credential, missing)
    assert not missing.parent.exists()


def test_ancestor_symlink_is_rejected(tmp_path):
    registry, credential, path = setup_launch(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(WebAuthError, match="bootstrap_directory_unavailable"):
        bootstrap.write_bootstrap(registry, credential, linked / path.name)
    assert not path.exists()


def test_failed_write_removes_only_unchanged_own_prefix_and_closes_fds(tmp_path, monkeypatch):
    registry, credential, path = setup_launch(tmp_path)
    opened = []
    real_open, real_write = os.open, os.write

    def tracked_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    writes = []

    def fail_write(fd, data):
        writes.append(fd)
        if len(writes) == 1:
            return real_write(fd, data[:10])
        raise OSError("synthetic write fault")

    monkeypatch.setattr(bootstrap.os, "open", tracked_open)
    monkeypatch.setattr(bootstrap.os, "write", fail_write)
    with pytest.raises(WebAuthError, match="bootstrap_write_failed"):
        bootstrap.write_bootstrap(registry, credential, path)
    assert not path.exists()
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_postwrite_mutation_is_preserved_and_never_returned_as_success(tmp_path, monkeypatch):
    registry, credential, path = setup_launch(tmp_path)
    real_sync = os.fsync
    mutated = []

    def mutate_after_sync(fd):
        real_sync(fd)
        if not mutated and stat.S_ISREG(os.fstat(fd).st_mode):
            mutated.append(True)
            path.write_bytes(b"operator edit after synthetic fsync")

    monkeypatch.setattr(bootstrap.os, "fsync", mutate_after_sync)
    with pytest.raises(WebAuthError, match="bootstrap_write_failed"):
        bootstrap.write_bootstrap(registry, credential, path)
    assert path.read_bytes() == b"operator edit after synthetic fsync"


def test_expiry_after_io_rejects_launch_and_cleans_owned_file(tmp_path, monkeypatch):
    _, _, path = setup_launch(tmp_path)
    tick = [0.0]
    registry = RunRegistry(clock=lambda: tick[0])
    credential = registry.create("http://127.0.0.1:8021", lifetime=1)
    real_sync = os.fsync

    def expire(fd):
        real_sync(fd)
        tick[0] = 2.0

    monkeypatch.setattr(bootstrap.os, "fsync", expire)
    with pytest.raises(WebAuthError, match="bootstrap_write_failed"):
        bootstrap.write_bootstrap(registry, credential, path)
    assert not path.exists()


def test_cleanup_fsync_failure_reports_uncertainty(tmp_path, monkeypatch):
    registry, credential, path = setup_launch(tmp_path)
    owner = bootstrap.write_bootstrap(registry, credential, path)

    def fail(_):
        raise OSError("synthetic directory sync fault")

    monkeypatch.setattr(bootstrap.os, "fsync", fail)
    assert owner.close() == "uncertain"
    assert not path.exists()


@pytest.mark.parametrize("path", ["relative.json", "/tmp/../escape.json", "\x00", True])
def test_invalid_paths_fail_without_filesystem_effects(path):
    registry = RunRegistry()
    credential = registry.create("http://127.0.0.1:8021")
    with pytest.raises(WebAuthError, match="bootstrap_path_invalid"):
        bootstrap.write_bootstrap(registry, credential, path)
