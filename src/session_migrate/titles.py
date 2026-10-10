"""Explicit target-title plans and private, root-qualified local aliases.

Plans describe intent, not an installed native title. Publication of aliases and
receipts is explicit; this module never imports a native session or edits one.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
import uuid
from contextlib import suppress
from dataclasses import InitVar, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, TargetFormat

MAX_TITLE_BYTES = 1024
TITLE_SCHEMA_VERSION = 1
MAX_TITLE_RECORD_BYTES = 16 * 1024
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


class TitlePolicy(StrEnum):
    NATIVE = "native"
    LOCAL_ALIAS = "local_alias"


def validate_title(value: str) -> str:
    """Preserve valid Unicode exactly; enforce the app's explicit UTF-8 limit."""
    if not isinstance(value, str) or not value.strip():
        raise SessionMigrateError("target title must be a nonempty string")
    if any(
        unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} or char in _BIDI_CONTROLS
        for char in value
    ):
        raise SessionMigrateError("target title contains unsupported controls or Unicode")
    if len(value.encode("utf-8")) > MAX_TITLE_BYTES:
        raise SessionMigrateError("target title exceeds the SessionMigrate 1024-byte UTF-8 limit")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class TitlePlan:
    requested: str
    policy: TitlePolicy
    native_effective: str | None
    local_alias: str | None
    capability_status: str
    proof_version: str | None
    proof_id: str | None
    intended_persistence: str
    source_title_disposition: str

    def __post_init__(self) -> None:
        validate_title(self.requested)
        try:
            object.__setattr__(self, "policy", TitlePolicy(self.policy))
        except (TypeError, ValueError) as exc:
            raise SessionMigrateError("title policy is unsupported") from exc
        if self.source_title_disposition not in {
            "preserved",
            "replaced_by_request",
            "omitted",
            "unknown",
        }:
            raise SessionMigrateError("source title disposition is invalid")
        if self.policy == TitlePolicy.NATIVE:
            valid = (
                self.native_effective == self.requested
                and self.local_alias is None
                and self.capability_status == "native_verified"
                and isinstance(self.proof_version, str)
                and bool(self.proof_version)
                and isinstance(self.proof_id, str)
                and bool(self.proof_id)
                and self.intended_persistence == "pending_native_import"
            )
        else:
            valid = (
                self.local_alias == self.requested
                and self.capability_status == "local_alias"
                and self.proof_version is None
                and self.proof_id is None
                and self.intended_persistence == "alias_pending"
            )
        if not valid:
            raise SessionMigrateError("title plan does not match its persistence contract")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "policy": self.policy.value,
            "native_effective": self.native_effective,
            "local_alias": self.local_alias,
            "capability_status": self.capability_status,
            "proof_version": self.proof_version,
            "proof_id": self.proof_id,
            "intended_persistence": self.intended_persistence,
            "source_title_disposition": self.source_title_disposition,
            "app_max_utf8_bytes": MAX_TITLE_BYTES,
        }


def plan_target_title(
    target_format: TargetFormat | AgentFormat | str,
    *,
    requested: str,
    cli_version: str | None,
    policy: TitlePolicy | str | None = None,
    source_title: str | None = None,
) -> TitlePlan:
    """Resolve an explicit policy without a native probe or filesystem write."""
    from session_migrate.capabilities import title_capability_for

    validate_title(requested)
    try:
        selected = TitlePolicy.NATIVE if policy is None else TitlePolicy(policy)
    except (TypeError, ValueError) as exc:
        raise SessionMigrateError("title policy is unsupported") from exc
    capability = title_capability_for(target_format, cli_version=cli_version, policy=selected)
    if capability.status == "unsupported":
        raise SessionMigrateError(
            "native target title is not verified for this format/schema/client; "
            "choose local_alias explicitly"
        )
    native = selected == TitlePolicy.NATIVE
    return TitlePlan(
        requested=requested,
        policy=selected,
        native_effective=requested if native else None,
        local_alias=None if native else requested,
        capability_status=capability.status,
        proof_version=capability.proof_version,
        proof_id=capability.proof_id,
        intended_persistence="pending_native_import" if native else "alias_pending",
        source_title_disposition=(
            "preserved" if source_title == requested else "replaced_by_request"
        )
        if native
        else "unknown",
    )


@dataclass(frozen=True, slots=True, repr=False)
class TitleReceipt:
    """Title-only metadata; an intent remains pending until explicit observation."""

    plan: TitlePlan
    persistence: str
    verification_state: str = "pending"
    native_observed: str | None = None
    observed_cli_version: str | None = None
    alias_saved: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plan, TitlePlan) or type(self.alias_saved) is not bool:
            raise SessionMigrateError("title receipt schema is invalid")
        if self.persistence == self.plan.intended_persistence:
            valid = (
                self.verification_state == "pending"
                and self.native_observed is None
                and self.observed_cli_version is None
                and not self.alias_saved
            )
        elif self.persistence == "native_verified":
            valid = (
                self.plan.policy == TitlePolicy.NATIVE
                and self.verification_state == "verified"
                and self.native_observed == self.plan.requested
                and self.observed_cli_version == self.plan.proof_version
                and not self.alias_saved
            )
        elif self.persistence == "local_alias":
            valid = (
                self.plan.policy == TitlePolicy.LOCAL_ALIAS
                and self.verification_state == "published"
                and self.native_observed is None
                and self.observed_cli_version is None
                and self.alias_saved
            )
        else:
            valid = False
        if not valid:
            raise SessionMigrateError("title receipt does not match observed persistence")

    @classmethod
    def pending(cls, plan: TitlePlan) -> TitleReceipt:
        return cls(plan, plan.intended_persistence)

    @classmethod
    def verified_native(cls, plan: TitlePlan, *, observed: str, cli_version: str) -> TitleReceipt:
        return cls(plan, "native_verified", "verified", observed, cli_version)

    @classmethod
    def published_alias(cls, plan: TitlePlan) -> TitleReceipt:
        return cls(plan, "local_alias", "published", alias_saved=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TITLE_SCHEMA_VERSION,
            "title_plan": self.plan.to_dict(),
            "persistence": self.persistence,
            "verification_state": self.verification_state,
            "native_observed": self.native_observed,
            "observed_cli_version": self.observed_cli_version,
            "alias_saved": self.alias_saved,
        }


def _path(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SessionMigrateError("title storage/root path must be absolute without traversal")
    # On Linux, // and / reach the same namespace. Keep one canonical key
    # without resolving through a symlink; the FD walk checks every component.
    return Path("/", *path.parts[1:]) if os.name == "posix" else path


def _directory(path: Path, *, create: bool, missing_ok: bool = False) -> int | None:
    if os.name != "posix":
        raise SessionMigrateError("private title storage requires POSIX no-follow support")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=fd)
            try:
                next_fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except FileNotFoundError:
                if missing_ok:
                    os.close(fd)
                    return None
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise SessionMigrateError(
            "title storage/root directory is unavailable or a symlink"
        ) from exc


def _private_directory(path: Path, *, create: bool, missing_ok: bool = False) -> int | None:
    fd = _directory(path, create=create, missing_ok=missing_ok)
    if fd is not None:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise SessionMigrateError("title storage directory must be owned with mode 0700")
    return fd


def _root_witness(path: Path, *, missing_ok: bool = False) -> tuple[int, int] | None:
    parent = _directory(path.parent, create=False, missing_ok=missing_ok)
    if parent is None:
        return None
    try:
        fd = os.open(path.name or ".", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise SessionMigrateError(
                    "native root must be a directory or regular database file"
                )
            return info.st_dev, info.st_ino
        finally:
            os.close(fd)
    except FileNotFoundError as exc:
        if missing_ok:
            return None
        raise SessionMigrateError("native root is missing") from exc
    except OSError as exc:
        raise SessionMigrateError("native root is missing, unreadable, or a symlink") from exc
    finally:
        os.close(parent)


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 1024
        or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
    ):
        raise SessionMigrateError("title identity/proposal identifier is invalid")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AliasIdentity:
    format: AgentFormat
    root: Path
    native_id: str
    root_device: int = -1
    root_inode: int = -1
    allow_missing_root: InitVar[bool] = False

    def __post_init__(self, allow_missing_root: bool) -> None:
        if type(allow_missing_root) is not bool:
            raise SessionMigrateError("planned native root flag must be boolean")
        try:
            object.__setattr__(self, "format", AgentFormat(self.format))
        except (TypeError, ValueError) as exc:
            raise SessionMigrateError("title identity format is unsupported") from exc
        root = _path(self.root)
        object.__setattr__(self, "root", root)
        _identifier(self.native_id)
        witness = _root_witness(root, missing_ok=allow_missing_root)
        if witness is None:
            if (self.root_device, self.root_inode) != (-1, -1):
                raise SessionMigrateError("native root identity is unavailable")
            return
        if (self.root_device, self.root_inode) == (-1, -1):
            object.__setattr__(self, "root_device", witness[0])
            object.__setattr__(self, "root_inode", witness[1])
        elif (
            type(self.root_device) is not int
            or type(self.root_inode) is not int
            or witness != (self.root_device, self.root_inode)
        ):
            raise SessionMigrateError("native root identity changed; local alias is not applicable")

    @classmethod
    def planned(cls, format: AgentFormat | str, root: Path, native_id: str) -> AliasIdentity:
        """Read-only preflight identity; a future root has no invented inode."""
        return cls(format, root, native_id, allow_missing_root=True)

    @property
    def bound(self) -> bool:
        return self.root_device >= 0 and self.root_inode >= 0

    def bind_root(self) -> AliasIdentity:
        """Capture a newly installed root; existing witnesses cannot be replaced."""
        if self.bound:
            self.validate_root()
            return self
        return replace(self, allow_missing_root=False)

    def validate_root(self) -> None:
        if _root_witness(self.root) != (self.root_device, self.root_inode):
            raise SessionMigrateError("native root identity changed; local alias is not applicable")

    @property
    def key(self) -> str:
        # Profile names/IDs and root inode are deliberately not the identity key.
        return hashlib.sha256(
            _json_bytes(
                {"format": self.format.value, "root": str(self.root), "native_id": self.native_id}
            )
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "root": str(self.root),
            "native_id": self.native_id,
            "root_device": self.root_device if self.bound else None,
            "root_inode": self.root_inode if self.bound else None,
        }


@dataclass(frozen=True, slots=True, repr=False)
class TitleAlias:
    identity: AliasIdentity
    alias: str
    created_at: str
    proposal_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TITLE_SCHEMA_VERSION,
            "identity": self.identity.to_dict(),
            "alias": self.alias,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "native_id_verified": True,
        }


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _directory_current(path: Path, fd: int) -> None:
    current = _private_directory(path, create=False)
    try:
        expected, actual = os.fstat(fd), os.fstat(current)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise SessionMigrateError("title storage directory changed during access")
    finally:
        os.close(current)


def _unlink_owned(fd: int, name: str, identity: tuple[int, int]) -> None:
    with suppress(FileNotFoundError):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            os.unlink(name, dir_fd=fd)


def _preflight(path: Path) -> None:
    directory = _private_directory(path.parent, create=False, missing_ok=True)
    if directory is None:
        return
    try:
        try:
            os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise SessionMigrateError("title metadata already exists; no overwrite")
    finally:
        os.close(directory)


def _publish(path: Path, data: bytes, identity: AliasIdentity | None = None) -> None:
    if len(data) > MAX_TITLE_RECORD_BYTES:
        raise SessionMigrateError("title metadata exceeds its size limit")
    _preflight(path)
    directory = _private_directory(path.parent, create=True)
    temporary = f".title-{uuid.uuid4().hex}.tmp"
    own_identity = None
    published = False
    completed = False
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        info = os.fstat(fd)
        own_identity = (info.st_dev, info.st_ino)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if identity is not None:
                identity.validate_root()
            _directory_current(path.parent, directory)
            staged = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
            if (staged.st_dev, staged.st_ino) != own_identity:
                raise SessionMigrateError("title metadata changed during publication")
            os.link(
                temporary,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            published = True
            _unlink_owned(directory, temporary, own_identity)
            final = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (final.st_dev, final.st_ino) != own_identity or final.st_nlink != 1:
                raise SessionMigrateError("title metadata changed during publication")
            if identity is not None:
                identity.validate_root()
            _directory_current(path.parent, directory)
            os.fsync(directory)
            completed = True
    except OSError as exc:
        raise SessionMigrateError("title metadata publication failed; no overwrite") from exc
    finally:
        if own_identity is not None:
            _unlink_owned(directory, temporary, own_identity)
            # Failed publication removes only this writer's record, never a competitor's.
            if published and not completed:
                _unlink_owned(directory, path.name, own_identity)
        os.close(directory)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class TitleAliasStore:
    """Exclusive creation and exact lookup; no editing, removal, or body index."""

    def __init__(self, directory: Path):
        self.directory = _path(directory)

    def path_for(self, identity: AliasIdentity) -> Path:
        return self.directory / f"{identity.key}.json"

    def preflight(self, identity: AliasIdentity) -> Path:
        if identity.bound:
            identity.validate_root()
        else:
            _root_witness(identity.root, missing_ok=True)
        path = self.path_for(identity)
        _preflight(path)
        return path

    def create(
        self, identity: AliasIdentity, alias: str, *, proposal_id: str, target_verified: bool
    ) -> TitleAlias:
        validate_title(alias)
        _identifier(proposal_id)
        if target_verified is not True:
            raise SessionMigrateError("local alias requires a verified installed target identity")
        identity = identity.bind_root()
        record = TitleAlias(identity, alias, datetime.now(UTC).isoformat(), proposal_id)
        _publish(self.path_for(identity), _json_bytes(record.to_dict()), identity)
        return record

    def load(self, identity: AliasIdentity) -> TitleAlias:
        identity = identity.bind_root()
        directory = _private_directory(self.directory, create=False)
        try:
            fd = os.open(
                self.path_for(identity).name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1
                ):
                    raise SessionMigrateError("title alias must be a private owned regular file")
                if before.st_size > MAX_TITLE_RECORD_BYTES:
                    raise SessionMigrateError("title metadata exceeds its size limit")
                data = stream.read(MAX_TITLE_RECORD_BYTES + 1)
                after = os.fstat(stream.fileno())
                named = os.stat(
                    self.path_for(identity).name, dir_fd=directory, follow_symlinks=False
                )
                if (
                    len(data) > MAX_TITLE_RECORD_BYTES
                    or (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    )
                    != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    )
                    or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(named.st_mode)
                    or named.st_uid != os.getuid()
                    or stat.S_IMODE(named.st_mode) != 0o600
                    or named.st_nlink != 1
                ):
                    raise SessionMigrateError("title metadata changed or exceeded its size limit")
                _directory_current(self.directory, directory)
        except OSError as exc:
            raise SessionMigrateError("title alias is missing, unreadable, or a symlink") from exc
        finally:
            os.close(directory)
        try:
            row = json.loads(data, object_pairs_hook=_unique_pairs)
            if not isinstance(row, dict) or set(row) != {
                "schema_version",
                "identity",
                "alias",
                "created_at",
                "proposal_id",
                "native_id_verified",
            }:
                raise ValueError
            if (
                type(row["schema_version"]) is not int
                or row["schema_version"] != TITLE_SCHEMA_VERSION
            ):
                raise ValueError
            stored_identity = row["identity"]
            if (
                not isinstance(stored_identity, dict)
                or type(stored_identity.get("root_device")) is not int
                or type(stored_identity.get("root_inode")) is not int
                or row["native_id_verified"] is not True
                or stored_identity != identity.to_dict()
            ):
                raise ValueError
            if not isinstance(row["created_at"], str):
                raise ValueError
            date = datetime.fromisoformat(row["created_at"])
            if date.tzinfo is None:
                raise ValueError
            alias = validate_title(row["alias"])
            proposal_id = _identifier(row["proposal_id"])
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise SessionMigrateError("title alias JSON/schema/identity is invalid") from exc
        identity.validate_root()
        return TitleAlias(identity, alias, row["created_at"], proposal_id)


def title_receipt_path(manifest: Path) -> Path:
    return _path(manifest).with_name(f"{manifest.name}.title-receipt.json")


def preflight_title_receipt(path: Path) -> None:
    _preflight(_path(path))


@dataclass(frozen=True, slots=True, repr=False)
class TitleReceiptBinding:
    """An installed root-qualified identity or a physical convert-only artifact."""

    target_format: AgentFormat
    target_sha256: str
    identity: AliasIdentity | None = None
    output_path: Path | None = None
    session_id: str | None = None
    manifest_path: Path | None = None
    manifest_sha256: str | None = None
    proposal_id: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "target_format", AgentFormat(self.target_format))
        except (TypeError, ValueError) as exc:
            raise SessionMigrateError("title receipt target format is unsupported") from exc
        _sha256(self.target_sha256)
        if self.identity is not None:
            if (
                not isinstance(self.identity, AliasIdentity)
                or not self.identity.bound
                or self.identity.format != self.target_format
                or self.output_path is not None
            ):
                raise SessionMigrateError("title receipt requires an installed root identity")
            if self.session_id is not None and self.session_id != self.identity.native_id:
                raise SessionMigrateError("title receipt target identity is inconsistent")
            object.__setattr__(self, "session_id", self.identity.native_id)
        elif self.output_path is not None:
            object.__setattr__(self, "output_path", _path(self.output_path))
        else:
            raise SessionMigrateError("title receipt requires target identity or physical output")
        if self.session_id is not None:
            _identifier(self.session_id)
        if self.manifest_path is not None:
            object.__setattr__(self, "manifest_path", _path(self.manifest_path))
        if self.manifest_sha256 is not None:
            if self.manifest_path is None:
                raise SessionMigrateError("title receipt manifest hash requires its path")
            _sha256(self.manifest_sha256)
        if self.proposal_id is not None:
            _identifier(self.proposal_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_format": self.target_format.value,
            "target_sha256": self.target_sha256,
            "identity": self.identity.to_dict() if self.identity is not None else None,
            "output_path": str(self.output_path) if self.output_path is not None else None,
            "session_id": self.session_id,
            "manifest_path": str(self.manifest_path) if self.manifest_path is not None else None,
            "manifest_sha256": self.manifest_sha256,
            "proposal_id": self.proposal_id,
        }


def _sha256(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise SessionMigrateError("title receipt hash must be a SHA256 hex digest")


def _verify_physical_output(path: Path, expected_sha256: str) -> None:
    directory = _directory(path.parent, create=False)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SessionMigrateError("title receipt output must be a regular single-link file")
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining and (block := stream.read(min(64 * 1024, remaining))):
                digest.update(block)
                remaining -= len(block)
            grew = bool(stream.read(1))
            after = os.fstat(stream.fileno())
            named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (
                remaining
                or grew
                or digest.hexdigest() != expected_sha256
                or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
            ):
                raise SessionMigrateError("title receipt output changed or has a different hash")
    except OSError as exc:
        raise SessionMigrateError(
            "title receipt output is missing, unreadable, or a symlink"
        ) from exc
    finally:
        os.close(directory)


def write_title_receipt(
    path: Path,
    receipt: TitleReceipt,
    *,
    binding: TitleReceiptBinding,
    alias_store: TitleAliasStore | None = None,
) -> None:
    """Publish associated evidence only; the orchestration owns partial-import state."""
    if not isinstance(receipt, TitleReceipt):
        raise SessionMigrateError("title receipt schema is invalid")
    if not isinstance(binding, TitleReceiptBinding):
        raise SessionMigrateError("title receipt requires an explicit target binding")
    if binding.identity is not None:
        binding.identity.validate_root()
    else:
        if receipt.verification_state != "pending":
            raise SessionMigrateError(
                "convert-only title receipt cannot claim installed persistence"
            )
        _verify_physical_output(binding.output_path, binding.target_sha256)
    if receipt.persistence == "native_verified":
        from session_migrate.capabilities import title_capability_for

        capability = title_capability_for(
            binding.target_format, cli_version=receipt.observed_cli_version
        )
        if (
            capability.status != "native_verified"
            or capability.proof_id != receipt.plan.proof_id
            or capability.proof_version != receipt.plan.proof_version
        ):
            raise SessionMigrateError(
                "title receipt native verification has no matching client proof"
            )
    elif receipt.persistence == "local_alias":
        if alias_store is None or binding.identity is None:
            raise SessionMigrateError("published title receipt requires its private alias evidence")
        record = alias_store.load(binding.identity)
        if record.alias != receipt.plan.requested or (
            binding.proposal_id is not None and record.proposal_id != binding.proposal_id
        ):
            raise SessionMigrateError("title receipt alias evidence is inconsistent")
    if binding.manifest_sha256 is not None:
        _verify_physical_output(binding.manifest_path, binding.manifest_sha256)
    document = receipt.to_dict()
    document["binding"] = binding.to_dict()
    _publish(_path(path), _json_bytes(document), binding.identity)
