"""Conservative adapter capabilities, independent of interface or process state.

Schema names describe adapter contracts, not automatic acceptance of arbitrary
native client versions. Native parsers/installers remain the final validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode_v2
from session_migrate.model import AgentFormat


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    format: AgentFormat
    schema: str
    source_supported: bool
    target_supported: bool
    pinned_cli_version: str
    media_source: bool | None = None
    media_target: bool | None = None
    native_title_supported: bool = False
    update_supported: bool = False
    proof_version: str | None = None
    title_reason: str = "Native title override has not passed the F3 native picker/resume proof."
    update_reason: str = (
        "In-place update has not passed the F5B concurrency/recovery/native resume proof."
    )
    media_scope: str = "Unknown; the native adapter validates and reports omissions."


@dataclass(frozen=True, slots=True)
class TitleCapability:
    format: AgentFormat
    schema: str
    status: str
    reason: str
    proof_version: str | None = None
    proof_id: str | None = None


# Schema compatibility does not prove picker title persistence for other releases.
# Each release has an independent public-list/cold-export title oracle.
_NATIVE_TITLE_PROOFS = {
    (AgentFormat.OPENCODE, "v2", "2.0.22"): "opencode-2.0.22-public-list-cold-export",
    (AgentFormat.OPENCODE, "v2", "2.0.23"): "opencode-2.0.23-public-list-cold-export",
}


def title_capability_for(
    agent_format: AgentFormat | str,
    *,
    schema: str | None = None,
    cli_version: str | None = None,
    policy: str = "native",
) -> TitleCapability:
    """Resolve title policy for an exact proved client without any native I/O."""
    if cli_version is not None and not isinstance(cli_version, str):
        raise SessionMigrateError("title client version must be a string")
    if not isinstance(policy, str) or policy not in {"native", "local_alias"}:
        raise SessionMigrateError("title policy is unsupported")
    selected = capabilities_for(agent_format)
    observed = (
        opencode_v2.version(cli_version)
        if selected.format == AgentFormat.OPENCODE and cli_version is not None
        else cli_version
    )
    if schema is None and selected.format == AgentFormat.OPENCODE and observed:
        schema = "v2"
    capability = capabilities_for(selected.format, schema=schema)
    if not capability.target_supported:
        return TitleCapability(
            selected.format, capability.schema, "unsupported", "This schema has no target writer."
        )
    if policy == "local_alias":
        return TitleCapability(
            selected.format,
            capability.schema,
            "local_alias",
            "Explicit private local alias; the native picker title is unchanged.",
        )
    proof = _NATIVE_TITLE_PROOFS.get((selected.format, capability.schema, observed))
    if proof is None:
        return TitleCapability(
            selected.format,
            capability.schema,
            "unsupported",
            "Native title persistence has no proof for this exact schema/client; "
            "local_alias requires an explicit choice.",
        )
    return TitleCapability(
        selected.format,
        capability.schema,
        "native_verified",
        "Exact-client public native list and cold export verified the requested title.",
        observed,
        proof,
    )


def _registry() -> dict[tuple[AgentFormat, str], AdapterCapabilities]:
    result = {}
    for agent_format in AgentFormat:
        module = import_module(f"session_migrate.formats.{agent_format.value}")
        pin = getattr(module, f"PINNED_{agent_format.value.upper()}_VERSION")
        schemas = ("native",)
        if agent_format == AgentFormat.CODEX:
            schemas = ("legacy", "paginated")
        elif agent_format == AgentFormat.OPENCODE:
            schemas = ("legacy", "v2")
        elif agent_format in {AgentFormat.PI, AgentFormat.OMP}:
            schemas = ("v3",)
        elif agent_format == AgentFormat.HERMES:
            schemas = ("sqlite-v26", "export-v26")
        elif agent_format == AgentFormat.DEVIN:
            schemas = ("sqlite-v16", "export-v16")
        for schema in schemas:
            media = None
            scope = "Unknown; the native adapter validates and reports omissions."
            if agent_format in {AgentFormat.CLAUDE, AgentFormat.CODEX, AgentFormat.OPENCODE}:
                media = True
                scope = (
                    "Portable user/tool-result image blocks; "
                    "role, MIME, size and encoding restrictions apply."
                )
            elif agent_format == AgentFormat.CURSOR:
                media = False
                scope = (
                    "Experimental ordered user/assistant text only; omitted classes are counted."
                )
            result[agent_format, schema] = AdapterCapabilities(
                agent_format,
                schema,
                True,
                not (agent_format == AgentFormat.CODEX and schema == "paginated"),
                opencode_v2.VALIDATED_VERSION
                if agent_format == AgentFormat.OPENCODE and schema == "v2"
                else pin,
                media,
                media,
                media_scope=scope,
            )
    return result


_CAPABILITIES = _registry()


def capabilities_for(
    agent_format: AgentFormat | str,
    *,
    schema: str | None = None,
) -> AdapterCapabilities:
    """Return a declared schema or fail explicitly, without probing a CLI/store."""
    try:
        selected = AgentFormat(agent_format)
    except (ValueError, TypeError) as exc:
        raise SessionMigrateError("adapter format is unsupported") from exc
    if schema is not None and not isinstance(schema, str):
        raise SessionMigrateError("adapter schema must be a known string identifier")
    if schema is None:
        return next(value for (format_, _), value in _CAPABILITIES.items() if format_ == selected)
    try:
        return _CAPABILITIES[selected, schema]
    except KeyError as exc:
        raise SessionMigrateError(
            "adapter schema is unknown; native validation is required"
        ) from exc


def list_capabilities(
    agent_format: AgentFormat | str | None = None,
) -> tuple[AdapterCapabilities, ...]:
    if agent_format is None:
        return tuple(_CAPABILITIES.values())
    selected = capabilities_for(agent_format).format
    return tuple(value for (format_, _), value in _CAPABILITIES.items() if format_ == selected)


def handoff_emission_supported(agent_format: AgentFormat | str, *, cli_version: str) -> bool:
    """Schema-level USER emission only; no native resume, closure or update claim."""
    selected = capabilities_for(agent_format).format
    versions = {
        AgentFormat.CLAUDE: {"2.1.209"},
        AgentFormat.CODEX: {"0.144.4"},
        AgentFormat.OPENCODE: set(opencode_v2.VALIDATED_VERSIONS),
    }
    return type(cli_version) is str and cli_version in versions.get(selected, ())
