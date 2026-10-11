"""Content-free, read-only CLI consumers of the continuity core."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from enum import Enum
from pathlib import Path

from session_migrate.catalog import Catalog, default_catalog_path
from session_migrate.continuity import (
    ContinuityAssessmentV1,
    ContinuityAssessmentV2,
    ContinuitySelector,
    ContinuityStatus,
    ExplicitPair,
    LineageInspectionV1,
    assess_continuity,
    inspect_lineage,
)
from session_migrate.continuity_bindings import (
    EXECUTION_ENVIRONMENT,
    PUBLIC_ENVIRONMENT,
    LegacyRuntimeContext,
)
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import (
    MAX_OPERATIONS,
    SCHEMA_VERSION,
    LineageStore,
    RecoveryOperation,
)
from session_migrate.model import AgentFormat
from session_migrate.profiles import ENVIRONMENT_KEYS, Profile, ProfileStore, SessionRef

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a bounded identifier")
    return value


def _absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("expected an absolute path without parent traversal")
    return path


def _limit(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer limit") from None
    if not 1 <= result <= MAX_OPERATIONS:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_OPERATIONS}")
    return result


def _offset(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer offset") from None
    if not 0 <= result <= MAX_OPERATIONS:
        raise argparse.ArgumentTypeError(f"offset must be between 0 and {MAX_OPERATIONS}")
    return result


def _environment_path(value: str) -> tuple[str, str]:
    key, separator, path = value.partition("=")
    if not separator or key not in ENVIRONMENT_KEYS:
        raise argparse.ArgumentTypeError("expected an allowed HOME/XDG key and absolute path")
    return key, str(_absolute_path(path))


def add_readonly_commands(subparsers) -> None:
    lineage = subparsers.add_parser("lineage", help="inspect existing content-free lineage")
    commands = lineage.add_subparsers(dest="lineage_command", required=True)
    from session_migrate.recovery_cli import add_lineage_action_commands

    add_lineage_action_commands(commands)
    inspect = commands.add_parser("inspect", help="inspect one existing operation")
    inspect.add_argument("--operation-id", required=True, type=_identifier)
    inspect.add_argument("--json", action="store_true", help="print metadata JSON")
    listing = commands.add_parser("list", help="list existing operations without native reads")
    listing.add_argument("--limit", type=_limit, default=50)
    listing.add_argument("--offset", type=_offset, default=0)
    listing.add_argument("--json", action="store_true", help="print metadata JSON")
    assess = subparsers.add_parser("assess", help="assess continuity; never update or resume")
    assess.add_argument("--operation-id", type=_identifier)
    assess.add_argument("--profiles", type=_absolute_path, help="read private saved profiles JSON")
    assess.add_argument(
        "--legacy-runtime-context",
        action="store_true",
        help="explicitly bind the exact current sparse public runtime environment",
    )
    for side in ("source", "target"):
        assess.add_argument(f"--{side}-format", choices=tuple(AgentFormat))
        assess.add_argument(f"--{side}-root", type=_absolute_path)
        assess.add_argument(f"--{side}-id", type=_identifier, help="exact native session ID")
        assess.add_argument(f"--{side}-profile", type=_identifier, help="current profile ID")
        assess.add_argument(f"--{side}-profile-name", help="name of an explicit current profile")
        assess.add_argument(
            f"--{side}-cli", type=_absolute_path, help="explicit native exporter CLI"
        )
        assess.add_argument(
            f"--{side}-env",
            type=_environment_path,
            action="append",
            default=[],
            help="current profile HOME/XDG override KEY=PATH (repeatable)",
        )
    assess.add_argument("--json", action="store_true", help="print metadata JSON")


def _metadata(value):
    """Serialize only the metadata DTO graphs, never sessions or native payloads."""
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, Enum):
        return _metadata(value.value)
    if isinstance(value, Path):
        return str(value)
    if type(value) in (tuple, list):
        return [_metadata(item) for item in value]
    if dataclasses.is_dataclass(value) and type(value).__module__ in {
        "session_migrate.lineage",
        "session_migrate.witnesses",
        "session_migrate.continuity",
        "session_migrate.canonical",
    }:
        return {
            field.name: _metadata(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    raise SessionMigrateError("readonly_metadata_invalid")


def metadata_dict(value: LineageInspectionV1 | ContinuityAssessmentV1 | RecoveryOperation) -> dict:
    if type(value) not in (
        LineageInspectionV1,
        ContinuityAssessmentV1,
        ContinuityAssessmentV2,
        RecoveryOperation,
    ):
        raise SessionMigrateError("readonly_metadata_invalid")
    result = _metadata(value)
    if type(value) is ContinuityAssessmentV1:
        # The core may describe future operations. The CLI advertises implemented actions only.
        result["actions"] = [action for action in result["actions"] if action == "inspect"]
    if type(value) is ContinuityAssessmentV2:
        result["baseline"]["actions"] = [
            action for action in result["baseline"]["actions"] if action == "inspect"
        ]
        result["baseline"]["update_available"] = False
    result["update_available"] = False
    return result


def _list_metadata(path: Path, limit: int, offset: int) -> dict:
    view = LineageStore.inspect_existing(path)
    operations = view.operations if view else ()
    return {
        "schema_version": 1,
        "ledger_schema_version": SCHEMA_VERSION,
        "reason": "listed" if view else "ledger_missing",
        "store_uuid": view.store_uuid if view else None,
        "total": len(operations),
        "limit": limit,
        "offset": offset,
        "operation_limit": MAX_OPERATIONS,
        "operations": [metadata_dict(op) for op in operations[offset : offset + limit]],
        "update_available": False,
    }


def _validate_selection(args, parser) -> None:
    ids = (args.source_id, args.target_id)
    if args.operation_id and any(ids):
        parser.error("operation ID and an explicit native session pair are mutually exclusive")
    if not args.operation_id and any(
        getattr(args, f"{side}_{key}") is None
        for side in ("source", "target")
        for key in ("format", "root", "id", "profile")
    ):
        parser.error("assess requires an operation ID or complete source and target references")
    for side in ("source", "target"):
        values = [getattr(args, f"{side}_{key}") for key in ("format", "root", "profile")]
        extras = (
            getattr(args, f"{side}_profile_name"),
            getattr(args, f"{side}_cli"),
            getattr(args, f"{side}_env"),
        )
        if (any(value is not None for value in values) or any(extras)) and not all(values):
            parser.error("explicit current profiles require format, root and profile ID")
        keys = [key for key, _ in getattr(args, f"{side}_env")]
        if len(set(keys)) != len(keys):
            parser.error("duplicate current profile environment key")


def _profiles(args, catalog: Catalog) -> tuple[Profile, ...]:
    if args.profiles:
        profiles = list(ProfileStore(args.profiles).load())
    else:
        # Match the existing public catalog-root profile convention used by Controller.
        profiles = [
            Profile(
                f"root-{root.id}",
                f"{root.format}: {Path(root.path).name}",
                root.format,
                Path(root.path),
            )
            for root in catalog.roots(enabled_only=True)
        ]
    selected = []
    for side in ("source", "target"):
        profile_id = getattr(args, f"{side}_profile")
        if profile_id is None:
            continue
        matches = [p for p in profiles if p.id == profile_id]
        format = AgentFormat(getattr(args, f"{side}_format"))
        root = getattr(args, f"{side}_root").resolve()
        name, cli, environ = (
            getattr(args, f"{side}_profile_name"),
            getattr(args, f"{side}_cli"),
            dict(getattr(args, f"{side}_env")),
        )
        if args.profiles:
            if len(matches) != 1:
                raise SessionMigrateError("current_profile_unavailable")
            profile = matches[0]
            if (
                profile.format != format
                or profile.root != root
                or (name is not None and profile.name != name)
                or (cli is not None and profile.cli != cli)
                or any(profile.environ.get(k) != v for k, v in environ.items())
            ):
                raise SessionMigrateError("current_profile_mismatch")
        else:
            if matches and len(matches) != 1:
                raise SessionMigrateError("current_profile_ambiguous")
            previous = matches[0] if matches else None
            profile = Profile(
                profile_id,
                name or (previous.name if previous else profile_id),
                format,
                root,
                cli,
                environ,
            )
            profiles = [
                p
                for p in profiles
                if p.id != profile_id and not (p.format == format and p.root == root)
            ]
            profiles.append(profile)
        selected.append(profile)
    if (
        len(selected) == 2
        and selected[0].id == selected[1].id
        and selected[0].fingerprint != selected[1].fingerprint
    ):
        raise SessionMigrateError("current_profile_ambiguous")
    return tuple(profiles)


def _pair(args, profiles: tuple[Profile, ...]) -> ContinuitySelector:
    if args.operation_id:
        return ContinuitySelector(operation_id=args.operation_id)
    refs = []
    for side in ("source", "target"):
        matches = [p for p in profiles if p.id == getattr(args, f"{side}_profile")]
        if len(matches) != 1:
            raise SessionMigrateError("current_profile_unavailable")
        profile = matches[0]
        refs.append(
            SessionRef(
                profile.format,
                profile.root,
                getattr(args, f"{side}_id"),
                profile.id,
                profile_fingerprint=profile.fingerprint,
            )
        )
    return ContinuitySelector(pair=ExplicitPair(*refs))


def _unavailable(reason: str, operation_id: str | None) -> dict:
    values = {field.name: None for field in dataclasses.fields(ContinuityAssessmentV1)}
    values.update(
        schema_version=1,
        status=ContinuityStatus.UNTRACKED,
        reason=reason,
        operation_id=operation_id,
        blockers=(reason,),
        actions=("inspect",),
        native_read_effects=(),
    )
    return metadata_dict(ContinuityAssessmentV1(**values))


def _assessment(args, environment, path: Path) -> dict:
    catalog_path = args.catalog or default_catalog_path(environ=environment)
    if not catalog_path.is_file():
        return _unavailable("catalog_missing", args.operation_id)
    with Catalog.open_readonly_existing(catalog_path) as catalog:
        profiles = _profiles(args, catalog)
        if not profiles:
            return _unavailable("profile_context_required", args.operation_id)
        selector = _pair(args, profiles)
        contexts = (
            tuple(
                LegacyRuntimeContext(
                    p.id, {k: v for k, v in environment.items() if k in PUBLIC_ENVIRONMENT}
                )
                for p in profiles
            )
            if args.legacy_runtime_context
            else ()
        )
        observer = EndpointObserver(
            catalog, profiles, base_environ=environment, legacy_runtime_contexts=contexts
        )
        result = metadata_dict(assess_continuity(selector, observer, path))
        baseline = result.get("baseline", result)
        for side in ("source", "target"):
            matches = [
                p
                for p in profiles
                if p.fingerprint == baseline[f"{side}_current_profile_fingerprint"]
            ]
            baseline[f"{side}_profile_id"] = matches[0].id if len(matches) == 1 else None
            baseline[f"{side}_profile_name"] = matches[0].name if len(matches) == 1 else None
        return result


def _print_human(result: dict) -> None:
    if result.get("schema_version") == 2 and "baseline" in result:
        print("assessment_schema: 2 (derived USER handoff; full history omitted)")
        print("derivation: " + json.dumps(result["derivation"], sort_keys=True))
        print("return_link: " + json.dumps(result["return_link"], sort_keys=True))
        _print_human(result["baseline"])
        return
    for key in ("status", "reason", "operation_id", "total", "limit", "offset"):
        if key in result:
            print(f"{key}: {result[key] if result[key] is not None else '-'}")
    operations = result.get("operations", ())
    if result.get("operation"):
        operations = (result["operation"],)
    for op in operations:
        print(f"operation: {op['record']['operation_id']} ({op['state']})")
        source = op["record"]["source"]["identity"]
        target = op["record"]["target_namespace"]
        for side, namespace in (
            ("source", source["namespace"] if source else None),
            ("target", target),
        ):
            if namespace:
                print(
                    f"{side}: {namespace['format']} {namespace['canonical_root']} "
                    f"{namespace['native_id']}"
                )
    for side in ("source", "target"):
        name = result.get(f"{side}_profile_name")
        if name is not None:
            print(f"{side}_profile: {result[f'{side}_profile_id']} ({name})")
        identity = result.get(f"{side}_identity")
        if identity:
            namespace = identity["namespace"]
            print(
                f"{side}: {namespace['format']} {namespace['canonical_root']} "
                f"{namespace['native_id']}"
            )
    for key in ("blockers", "actions", "native_read_effects"):
        if key in result:
            print(f"{key}: {', '.join(result[key]) or '-'}")
    print("update_available: false")


def run_readonly_command(args, parser: argparse.ArgumentParser) -> int:
    if args.command == "assess":
        _validate_selection(args, parser)
    # Capture the migrator namespace before binding any selected profile HOME/XDG.
    environment = {
        k: v for k, v in os.environ.items() if k in PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT
    }
    try:
        state_home = args.state_home or default_migration_state_home(environ=environment)
        path = state_home / "lineage-v1"
        if args.command == "assess":
            result = _assessment(args, environment, path)
        elif args.lineage_command == "inspect":
            result = metadata_dict(
                inspect_lineage(ContinuitySelector(operation_id=args.operation_id), path)
            )
        else:
            result = _list_metadata(path, args.limit, args.offset)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        else:
            _print_human(result)
        return 0
    except (SessionMigrateError, OSError, ValueError, TypeError):
        print("session-migrate: error: readonly_inspection_failed", file=sys.stderr)
        return 1
