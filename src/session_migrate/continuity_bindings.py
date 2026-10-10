"""Explicit runtime profile bindings, with the exact legacy B3 hash convention.

The context pins a currently selected Profile, not a historical profile ID.
Sparse legacy environments are used as supplied; no absent XDG key is guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from session_migrate.canonical import _json_bytes
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat
from session_migrate.operations import _PUBLIC_ENVIRONMENT, _digest
from session_migrate.profiles import Profile

PUBLIC_ENVIRONMENT = frozenset(_PUBLIC_ENVIRONMENT)
EXECUTION_ENVIRONMENT = frozenset(("PATH", "LANG", "LC_ALL"))


class ProfileBindingKind(StrEnum):
    PROFILE_V1 = "profile_v1"
    LEGACY_RUNTIME_V1 = "legacy_runtime_v1"


class RuntimeBindingError(SessionMigrateError):
    def __init__(self) -> None:
        super().__init__("Invalid current runtime profile binding.")


def _require(condition: bool) -> None:
    if not condition:
        raise RuntimeBindingError()


@dataclass(frozen=True, slots=True, repr=False)
class LegacyRuntimeContext:
    profile_id: str
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        _require(type(self.profile_id) is str and bool(self.profile_id))
        _require(isinstance(self.environment, Mapping))
        values = dict(self.environment)
        _require(
            all(
                type(k) is str and k in PUBLIC_ENVIRONMENT and type(v) is str
                for k, v in values.items()
            )
        )
        _json_bytes(values)
        object.__setattr__(self, "environment", MappingProxyType(values))


def _legacy_runtime_environment(
    profile: Profile,
    context: LegacyRuntimeContext,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    _require(type(profile) is Profile and type(context) is LegacyRuntimeContext)
    _require(profile.id == context.profile_id)
    context.__post_init__()
    values = {k: v for k, v in base_environment.items() if k in EXECUTION_ENVIRONMENT}
    values.update(context.environment)
    for key, value in profile.environ.items():
        _require(key in values and values[key] == value)
    if profile.format in (AgentFormat.OPENCODE, AgentFormat.KILO):
        _require(profile.cli is not None and profile.cli.is_file())
        data = values.get("XDG_DATA_HOME")
        if data is None:
            _require(bool(values.get("HOME")))
            data = str(Path(values["HOME"]) / ".local/share")
        _require((Path(data) / profile.format.value).resolve(strict=True) == profile.root)
        bin_key = "OPENCODE_BIN" if profile.format is AgentFormat.OPENCODE else "KILO_BIN"
        if bin_key in values:
            _require(Path(values[bin_key]).resolve(strict=True) == profile.cli.resolve(strict=True))
    return values


def _legacy_runtime_fingerprint(profile: Profile, environment: Mapping[str, str]) -> str:
    _require(type(profile) is Profile and profile.root.is_dir())
    # Reuse the pure codec and documented keys; do not construct PreparedState.
    return _digest(
        {
            "format": profile.format.value,
            "environment": {
                key: value for key, value in environment.items() if key in _PUBLIC_ENVIRONMENT
            },
            "root": str(profile.root),
        }
    )
