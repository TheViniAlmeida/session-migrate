"""Explicit metadata-only recovery/adoption actions; never replay serialized handles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import replace

from session_migrate.adoption import (
    AdoptionError,
    commit_adoption,
    inspect_adoption,
    prepare_adoption,
)
from session_migrate.baselines import (
    BaselineReceipt,
    BaselineRecordV1,
    BaselineStore,
    _encode_baseline,
)
from session_migrate.catalog import Catalog, default_catalog_path
from session_migrate.continuity import ExplicitPair
from session_migrate.continuity_bindings import (
    EXECUTION_ENVIRONMENT,
    PUBLIC_ENVIRONMENT,
    LegacyRuntimeContext,
)
from session_migrate.continuity_observation import EndpointObserver
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import (
    CompletionReceipt,
    LineageError,
    NativeState,
    OperationState,
    RecoveryOperation,
    _encode,
)
from session_migrate.lineage_cli import (
    _absolute_path,
    _environment_path,
    _identifier,
    _profiles,
    metadata_dict,
)
from session_migrate.model import AgentFormat
from session_migrate.profiles import ProfileStore, SessionRef, _bind_profile_operation
from session_migrate.reconciliation import (
    ReconciliationError,
    RecoveryContext,
    cancel_recovered_ready,
    commit_reconciliation,
    inspect_recovered_operation,
    prepare_reconciliation,
)


def _add_profiles(parser, *, adoption=False):
    parser.add_argument(
        "--current-catalog",
        type=_absolute_path,
        help="existing current catalog; --catalog still reproduces original options",
    )
    parser.add_argument(
        "--profiles", type=_absolute_path, help="read private saved current profiles"
    )
    parser.add_argument(
        "--legacy-runtime-context",
        action="store_true",
        help="bind the exact current sparse public runtime; do not guess HOME/XDG",
    )
    for side in ("source", "target"):
        parser.add_argument(f"--{side}-profile", type=_identifier, help="current profile ID")
        parser.add_argument(f"--{side}-profile-format", choices=tuple(AgentFormat))
        parser.add_argument(f"--{side}-profile-root", type=_absolute_path)
        parser.add_argument(f"--{side}-profile-name")
        parser.add_argument(f"--{side}-profile-cli", type=_absolute_path)
        parser.add_argument(
            f"--{side}-profile-env",
            action="append",
            default=[],
            type=_environment_path,
            help="current HOME/XDG KEY=PATH (repeatable)",
        )
        parser.add_argument(
            f"--{side}-native-id",
            type=_identifier,
            help="exact selected native session ID",
            required=adoption,
        )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="prepare fresh proof and confirm that live handle in this process",
    )
    parser.add_argument("--json", action="store_true", help="print content-free action metadata")


def add_action_commands(subparsers, operation_arguments):
    reconcile = subparsers.add_parser(
        "reconcile", help="reconcile an acknowledged operation; no import"
    )
    commands = reconcile.add_subparsers(dest="reconcile_command", required=True)
    for command in ("import", "transfer"):
        parser = commands.add_parser(
            command, help="reproduce the complete original operation options"
        )
        operation_arguments(parser, command)
        parser.add_argument("--operation-id", type=_identifier, required=True)
        parser.add_argument(
            "--source-file",
            type=_absolute_path,
            required=True,
            help="original source capture file; never a proposal JSON",
        )
        parser.add_argument(
            "--input-file",
            type=_absolute_path,
            required=True,
            help="original prepared native input file",
        )
        parser.add_argument(
            "--manifest-file",
            type=_absolute_path,
            help="original manifest for same-operation title finalization",
        )
        parser.add_argument(
            "--handoff-document",
            type=_absolute_path,
            help="private accepted USER document for genuine derived reconciliation; no replay",
        )
        parser.add_argument(
            "--profile-operation",
            action="store_true",
            help="reproduce the original Profile API transfer binding",
        )
        _add_profiles(parser)
    adopt = subparsers.add_parser(
        "adopt", help="prove a qualified current pair; append a baseline only"
    )
    _add_profiles(adopt, adoption=True)
    adopt.add_argument("--source-client-version", required=True, help="exact source client release")
    adopt.add_argument("--target-client-version", required=True, help="exact target client release")


def add_lineage_action_commands(commands):
    cancel = commands.add_parser(
        "cancel-ready", help="inspect or explicitly cancel recovered READY"
    )
    cancel.add_argument("--operation-id", type=_identifier, required=True)
    cancel.add_argument(
        "--confirm", action="store_true", help="confirm cancellation of this READY ID"
    )
    cancel.add_argument("--json", action="store_true", help="print content-free action metadata")
    baseline = commands.add_parser(
        "baseline", help="inspect an existing baseline by its explicit ID"
    )
    baseline.add_argument("--baseline-id", type=_identifier, required=True)
    baseline.add_argument(
        "--json", action="store_true", help="print content-free baseline metadata"
    )


def _validate(args, parser):
    if args.command == "lineage":
        return
    if args.target_profile is None:
        parser.error("the current target profile ID is required")
    if (
        args.command == "adopt" or args.reconcile_command == "transfer"
    ) and args.source_profile is None:
        parser.error("the current source profile ID is required")
    for side in ("source", "target"):
        profile_id = getattr(args, f"{side}_profile")
        fields = [
            getattr(args, f"{side}_profile_{key}")
            for key in ("format", "root", "name", "cli", "env")
        ]
        if profile_id is None and any(fields):
            parser.error("current profile flags require their profile ID")
        keys = [key for key, _ in getattr(args, f"{side}_profile_env")]
        if len(keys) != len(set(keys)):
            parser.error("duplicate current profile environment key")
        if not args.profiles and profile_id and any(fields) and not all(fields[:2]):
            parser.error("explicit current profiles require format and root")
    if args.command == "adopt":
        return
    if args.session_id is None:
        parser.error("the original normalized target --session-id is required")
    if args.target_native_id and args.target_native_id != args.session_id:
        parser.error("the target native ID must agree with the original --session-id")
    if args.profile_operation:
        if args.reconcile_command != "transfer":
            parser.error("the Profile API supports transfer only")
        if args.source_native_id is None:
            parser.error("Profile API transfer requires the exact --source-native-id")
        if any(
            getattr(args, key, None) is not None
            for key in (
                "source_id",
                "catalog_id",
                "title",
                "source_home",
                "source_cwd",
                "source_cli",
                "home",
                "target_cli",
            )
        ):
            parser.error("Profile API original selectors must come from current profiles/ref")
        if args.legacy_runtime_context:
            parser.error("Profile API and legacy runtime binding are mutually exclusive")
    elif not args.legacy_runtime_context:
        parser.error("legacy recovery requires the explicit --legacy-runtime-context")
    if args.reconcile_command == "import" and args.path != args.source_file:
        parser.error("the original import path must agree with --source-file")
    if (
        args.reconcile_command == "transfer"
        and args.source_id
        and args.source_native_id
        and args.source_id != args.source_native_id
    ):
        parser.error("source native ID contradicts the original source selector")


def _selected_profiles(args, catalog):
    saved = ProfileStore(args.profiles).load() if args.profiles else ()
    defaults = (
        ()
        if saved
        else tuple((root, f"root-{root.id}") for root in catalog.roots(enabled_only=True))
    )
    values = {"profiles": args.profiles}
    for side in ("source", "target"):
        profile_id = getattr(args, f"{side}_profile")
        values[f"{side}_profile"] = profile_id
        current = next((p for p in saved if p.id == profile_id), None)
        root = next((r for r, identifier in defaults if identifier == profile_id), None)
        for key in ("format", "root", "name", "cli", "env"):
            value = getattr(args, f"{side}_profile_{key}")
            mapped = "profile_name" if key == "name" else key
            if key == "format" and value is None:
                value = current.format.value if current else root.format if root else None
            if key == "root" and value is None:
                value = current.root if current else _absolute_path(root.path) if root else None
            values[f"{side}_{mapped}"] = value
        if profile_id and (values[f"{side}_format"] is None or values[f"{side}_root"] is None):
            raise SessionMigrateError("current_profile_unavailable")
    profiles = _profiles(argparse.Namespace(**values), catalog)
    selected = {}
    for side in ("source", "target"):
        profile_id = getattr(args, f"{side}_profile")
        if profile_id:
            matches = [p for p in profiles if p.id == profile_id]
            if len(matches) != 1:
                raise SessionMigrateError("current_profile_unavailable")
            selected[side] = matches[0]
    return selected


def _public(environment):
    return {key: value for key, value in environment.items() if key in PUBLIC_ENVIRONMENT}


def _ref(profile, native_id):
    return SessionRef(
        profile.format, profile.root, native_id, profile.id, profile_fingerprint=profile.fingerprint
    )


@contextmanager
def _driver(args, environment):
    # Resolve the current default before profile binding, honoring SESSION_MIGRATE_CATALOG.
    path = args.current_catalog or args.catalog or default_catalog_path()
    with Catalog.open_readonly_existing(path) as catalog:
        selected = _selected_profiles(args, catalog)
        profiles = tuple({p.id: p for p in selected.values()}.values())
        contexts = (
            tuple(LegacyRuntimeContext(p.id, _public(environment)) for p in profiles)
            if args.legacy_runtime_context
            else ()
        )
        observer = EndpointObserver(
            catalog, profiles, base_environ=environment, legacy_runtime_contexts=contexts
        )
        yield catalog, selected, observer


def _result_metadata(value):
    if value is None:
        return None
    if type(value) in (BaselineReceipt, BaselineRecordV1):
        return json.loads(_encode_baseline(value))
    if type(value) is RecoveryOperation:
        return metadata_dict(value)
    if type(value) is CompletionReceipt:
        return json.loads(_encode(value))
    raise SessionMigrateError("action_metadata_invalid")


def _document(action, status, *, proposal=None, result=None, confirmed=False, reason=None):
    return {
        "schema_version": 1,
        "action": action,
        "status": status,
        "proposal": proposal,
        "result": result,
        "confirmed": confirmed,
        "reason": reason if reason is not None else (proposal.get("reason") if proposal else None),
        "effects": proposal.get("available_effects", []) if proposal else [],
        "native_read_effects": proposal.get("native_read_effects", []) if proposal else [],
        "update_available": False,
        "native_cas_proven": False,
        "boundary": "unproven",
    }


def _apply(proposal, args, action, commit):
    metadata = proposal.to_dict()
    handle = proposal.handle
    try:
        if not args.confirm:
            return _document(action, proposal.status, proposal=metadata), 0
        if handle is None:
            return _document(action, "blocked", proposal=metadata), 1
        try:
            receipt = commit(handle)
        except (ReconciliationError, AdoptionError) as error:
            status = "persistence_uncertain" if error.code == "persistence_uncertain" else "blocked"
            return _document(action, status, proposal=metadata, reason=error.code), 1
        return _document(
            action, "completed", proposal=metadata, result=_result_metadata(receipt), confirmed=True
        ), 0
    finally:
        if handle is not None:
            handle.discard()


def _reconcile(args, environment, state_home, operation_request):
    with _driver(args, environment) as (catalog, selected, observer):
        request = operation_request(args, args.reconcile_command)
        source_profile = target_profile = None
        if args.profile_operation:
            if (
                request.catalog is not None
                and request.catalog.expanduser().absolute() != catalog.path
            ):
                raise SessionMigrateError("original_catalog_mismatch")
            request = replace(request, catalog=None)
            source_profile, target_profile = selected["source"], selected["target"]
            request, target_env, source_env = _bind_profile_operation(
                catalog,
                source_profile,
                _ref(source_profile, args.source_native_id),
                target_profile,
                request,
                environment,
            )
        else:
            source_env = target_env = environment
        context = RecoveryContext(
            request,
            args.source_file,
            args.input_file,
            _public(source_env),
            _public(target_env),
            source_profile,
            target_profile,
            args.manifest_file,
            handoff_document=args.handoff_document,
        )
        proposal = prepare_reconciliation(
            args.operation_id,
            store_path=state_home / "lineage-v1",
            context=context,
            observer=observer,
        )
        return _apply(proposal, args, "reconcile", commit_reconciliation)


def _adopt(args, environment, state_home):
    with _driver(args, environment) as (_, selected, observer):
        pair = ExplicitPair(
            _ref(selected["source"], args.source_native_id),
            _ref(selected["target"], args.target_native_id),
        )
        proposal = prepare_adoption(
            pair,
            observer,
            state_home=state_home,
            source_client_version=args.source_client_version,
            target_client_version=args.target_client_version,
        )
        return _apply(proposal, args, "adopt", commit_adoption)


def _cancel(args, state_home):
    path = state_home / "lineage-v1"
    proposal = prepare_reconciliation(args.operation_id, store_path=path)
    metadata = proposal.to_dict()
    if proposal.handle is not None:
        proposal.handle.discard()
    if not args.confirm:
        return _document("cancel_ready", proposal.status, proposal=metadata), 0
    if proposal.reason != "recovered_ready":
        return _document("cancel_ready", "blocked", proposal=metadata), 1
    cancel_returned = False
    try:
        cancel_recovered_ready(args.operation_id, store_path=path)
        cancel_returned = True
        result = inspect_recovered_operation(args.operation_id, store_path=path)
    except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError) as error:
        if cancel_returned or (
            isinstance(error, LineageError) and error.code == "persistence_uncertain"
        ):
            return _document(
                "cancel_ready",
                "persistence_uncertain",
                proposal=metadata,
                reason="persistence_uncertain",
            ), 1
        if isinstance(error, LineageError):
            return _document("cancel_ready", "blocked", proposal=metadata, reason=error.code), 1
        raise
    if (
        type(result) is not RecoveryOperation
        or result.record.operation_id != args.operation_id
        or result.durable_state is not OperationState.CANCELLED
        or result.durable_native_state is not NativeState.NATIVE_NOT_STARTED
        or hashlib.sha256(_encode(result.record)).hexdigest() != proposal.record_sha256
    ):
        return _document(
            "cancel_ready",
            "persistence_uncertain",
            proposal=metadata,
            reason="persistence_uncertain",
        ), 1
    return _document(
        "cancel_ready",
        "cancelled",
        proposal=metadata,
        result=_result_metadata(result),
        confirmed=True,
    ), 0


def _baseline(args, state_home):
    receipt = inspect_adoption(args.baseline_id, state_home=state_home)
    record = None
    if receipt is not None:
        view = BaselineStore.inspect_existing(
            state_home / "baselines-v1", coordinator_store_uuid=receipt.coordinator_store_uuid
        )
        record = next((r for r in view.baselines if r.baseline_id == args.baseline_id), None)
    result = {
        "baseline_id": args.baseline_id,
        "receipt": _result_metadata(receipt),
        "record": _result_metadata(record),
    }
    return _document(
        "inspect_baseline", "selected" if receipt else "baseline_missing", result=result
    ), 0


def _print_action(document, as_json):
    if as_json:
        print(json.dumps(document, indent=2, sort_keys=True, allow_nan=False))
        return
    for key in ("action", "status", "reason"):
        print(f"{key}: {document[key] or '-'}")
    proposal = document["proposal"] or {}
    result = document["result"] or {}
    for key in ("operation_id", "baseline_id"):
        value = proposal.get(key) or result.get(key) or document.get(key)
        if value:
            print(f"{key}: {value}")
    for side in ("source", "target"):
        identity = proposal.get(f"{side}_identity")
        if identity:
            namespace = identity["namespace"]
            print(
                f"{side}: {namespace['format']} {namespace['canonical_root']} "
                f"{namespace['native_id']}"
            )
    if proposal.get("blockers"):
        print(f"blockers: {', '.join(proposal['blockers'])}")
    print(f"effects: {', '.join(document['effects']) or '-'}")
    print(f"native_read_effects: {', '.join(document['native_read_effects']) or '-'}")
    print("update_available: false")


def run_action_command(args, parser, operation_request):
    _validate(args, parser)
    environment = {
        k: v for k, v in os.environ.items() if k in PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT
    }
    try:
        state_home = args.state_home or default_migration_state_home(environ=environment)
        if args.command == "adopt":
            document, code = _adopt(args, environment, state_home)
        elif args.command == "reconcile":
            document, code = _reconcile(args, environment, state_home, operation_request)
        elif args.lineage_command == "cancel-ready":
            document, code = _cancel(args, state_home)
        else:
            document, code = _baseline(args, state_home)
        _print_action(document, args.json)
        return code
    except (SessionMigrateError, OSError, ValueError, TypeError, AttributeError) as error:
        reason = "continuity_action_failed"
        if args.json:
            action = args.command if args.command != "lineage" else args.lineage_command
            if action == "cancel-ready":
                action = "cancel_ready"
            document = _document(action, "blocked", reason=reason)
            for key in ("operation_id", "baseline_id"):
                value = getattr(error, key, None) or getattr(args, key, None)
                if value:
                    document[key] = value
            _print_action(document, True)
        else:
            print(f"session-migrate: error: {reason}", file=sys.stderr)
        return 1
