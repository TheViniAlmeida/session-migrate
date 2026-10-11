"""Exclusive private launch files with descriptor-bound cleanup."""

from __future__ import annotations

import os
import stat
import threading
from contextlib import suppress
from pathlib import Path

from session_migrate.web.auth import BootstrapCredential, RunRegistry, WebAuthError


def _witness(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _directory_key(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_ctime_ns)


def _absolute(path):
    if type(path) not in (str, type(Path())):
        raise WebAuthError("bootstrap_path_invalid")
    if type(path) is str and ("\x00" in path or len(os.fsencode(path)) > 4096):
        raise WebAuthError("bootstrap_path_invalid")
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts or not value.name or len(value.parts) > 64:
        raise WebAuthError("bootstrap_path_invalid")
    return value


class _Directory:
    def __init__(self, parent):
        self.nodes = []
        self.closed = False
        try:
            root = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.nodes.append((None, None, root, _directory_key(os.fstat(root))))
            current = root
            for name in parent.parts[1:]:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
                self.nodes.append((current, name, descriptor, _directory_key(os.fstat(descriptor))))
                current = descriptor
            self.fd = current
            info = os.fstat(current)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise WebAuthError("bootstrap_parent_not_private")
            self.check()
        except (OSError, WebAuthError):
            self.close()
            raise WebAuthError("bootstrap_directory_unavailable") from None

    def check(self, *, own_creation=False):
        if self.closed:
            raise WebAuthError("bootstrap_directory_changed")
        for index, (parent, name, descriptor, expected) in enumerate(self.nodes):
            current = _directory_key(os.fstat(descriptor))
            final = index == len(self.nodes) - 1
            if own_creation and final:
                if current[:4] != expected[:4]:
                    raise WebAuthError("bootstrap_directory_changed")
            elif current != expected:
                raise WebAuthError("bootstrap_directory_changed")
            if parent is not None:
                named = _directory_key(os.stat(name, dir_fd=parent, follow_symlinks=False))
                if named != current:
                    raise WebAuthError("bootstrap_directory_changed")
        if own_creation:
            parent, name, descriptor, _ = self.nodes[-1]
            self.nodes[-1] = (parent, name, descriptor, _directory_key(os.fstat(descriptor)))

    def close(self):
        if self.closed:
            return
        self.closed = True
        for _, _, descriptor, _ in reversed(self.nodes):
            with suppress(OSError):
                os.close(descriptor)


def _file_current(directory, path, descriptor, expected, payload):
    directory.check()
    opened = os.fstat(descriptor)
    named = os.stat(path.name, dir_fd=directory.fd, follow_symlinks=False)
    if _witness(opened) != expected or _witness(named) != expected:
        raise WebAuthError("bootstrap_file_changed")
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        raise WebAuthError("bootstrap_file_changed")
    if os.pread(descriptor, len(payload) + 1, 0) != payload:
        raise WebAuthError("bootstrap_file_changed")
    if _witness(os.fstat(descriptor)) != expected:
        raise WebAuthError("bootstrap_file_changed")
    directory.check()
    if _witness(os.stat(path.name, dir_fd=directory.fd, follow_symlinks=False)) != expected:
        raise WebAuthError("bootstrap_file_changed")


def _cleanup(directory, path, descriptor, expected, payload):
    try:
        _file_current(directory, path, descriptor, expected, payload)
        os.unlink(path.name, dir_fd=directory.fd)
    except (OSError, WebAuthError):
        return "preserved"
    try:
        os.fsync(directory.fd)
    except OSError:
        return "uncertain"
    return "removed"


class OwnedBootstrap:
    """One owned file; edited, linked, moved or replaced files are preserved."""

    def __init__(self):
        raise WebAuthError("bootstrap_invalid")

    def __repr__(self):
        return "<OwnedBootstrap>"

    @property
    def path(self):
        return self._path

    def close(self):
        with self._lock:
            if self._closed:
                return "already_closed"
            self._closed = True
            try:
                return _cleanup(
                    self._directory, self._path, self._fd, self._expected, self._payload
                )
            finally:
                try:
                    os.close(self._fd)
                finally:
                    self._directory.close()
                    self._payload = b""


def write_bootstrap(registry: RunRegistry, credential: BootstrapCredential, path: Path | str):
    """Write only to an explicitly selected existing private directory."""
    if type(registry) is not RunRegistry:
        raise WebAuthError("bootstrap_invalid")
    path = _absolute(path)
    payload = registry._bootstrap_payload(credential)
    if len(payload) > 4096:
        raise WebAuthError("bootstrap_limit")
    directory = _Directory(path.parent)
    descriptor = None
    expected = None
    written = b""
    try:
        directory.check()
        descriptor = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory.fd,
        )
        expected = _witness(os.fstat(descriptor))
        directory.check(own_creation=True)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise WebAuthError("bootstrap_file_not_private")
        while len(written) < len(payload):
            _file_current(directory, path, descriptor, expected, written)
            count = os.write(descriptor, payload[len(written) :])
            if count <= 0:
                raise WebAuthError("bootstrap_write_failed")
            written += payload[len(written) : len(written) + count]
            expected = _witness(os.fstat(descriptor))
        _file_current(directory, path, descriptor, expected, payload)
        os.fsync(descriptor)
        _file_current(directory, path, descriptor, expected, payload)
        os.fsync(directory.fd)
        if registry._bootstrap_payload(credential) != payload:
            raise WebAuthError("bootstrap_invalid")
        _file_current(directory, path, descriptor, expected, payload)
        if registry._bootstrap_payload(credential) != payload:
            raise WebAuthError("bootstrap_invalid")
        owner = object.__new__(OwnedBootstrap)
        owner._path, owner._directory, owner._fd = path, directory, descriptor
        owner._expected, owner._payload = expected, payload
        owner._lock, owner._closed = threading.Lock(), False
        return owner
    except (OSError, WebAuthError):
        if descriptor is not None:
            if expected is not None:
                _cleanup(directory, path, descriptor, expected, written)
            try:
                os.close(descriptor)
            finally:
                directory.close()
        else:
            directory.close()
        raise WebAuthError("bootstrap_write_failed") from None
