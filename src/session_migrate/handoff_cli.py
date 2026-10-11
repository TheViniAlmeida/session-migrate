"""Explicit F5A consumers; private handles live only in the current process."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict

from session_migrate.continuity_bindings import EXECUTION_ENVIRONMENT, PUBLIC_ENVIRONMENT
from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import OperationNeedsReconcileError, SessionMigrateError
from session_migrate.handoff import (
    accept_edit,
    accept_user_text,
    export_handoff,
    handoff_metadata,
    prepare_handoff,
    render_handoff,
)
from session_migrate.handoff_operations import (
    ReturnNewRequest,
    RuntimePersistenceUncertainError,
    prepare_return_new,
    prepare_summary,
)
from session_migrate.handoff_types import HandoffError, HandoffLimits
from session_migrate.lineage import SelectionRangeV1
from session_migrate.lineage_cli import _absolute_path, _identifier
from session_migrate.operations import OperationRequest, commit_operation, proposal_metadata
from session_migrate.recovery_cli import _add_profiles, _driver, _ref


class HandoffResultUnavailable(SessionMigrateError):
    """Commit returned, but its response cannot be rendered; inspect the same ID."""

    def __init__(self, operation_id, target_id):
        super().__init__("handoff_result_unavailable")
        self.operation_id, self.target_id = operation_id, target_id


def selection_range(value):
    """Inclusive event indices, optionally one actual provenance block index."""
    match = re.fullmatch(r"(0|[1-9][0-9]{0,9}):(0|[1-9][0-9]{0,9})(?::(0|[1-9][0-9]{0,9}))?", value)
    if match is None:
        raise argparse.ArgumentTypeError("expected FIRST:LAST[:BLOCK] inclusive range")
    first, last, block = match.groups()
    try:
        return SelectionRangeV1(int(first), int(last), int(block) if block is not None else None)
    except SessionMigrateError:
        raise argparse.ArgumentTypeError("invalid selection range") from None


def _selection_arguments(parser):
    parser.add_argument("--range", dest="ranges", action="append", type=selection_range, default=[])
    parser.add_argument("--source-client-version", help="exact captured source client release")


def _target_arguments(parser):
    parser.add_argument("--cwd", type=_absolute_path, required=True)
    parser.add_argument("--target-cli-version", required=True)
    parser.add_argument("--model-provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--session-id", type=_identifier)
    parser.add_argument("--target-title")
    parser.add_argument(
        "--edit-export",
        type=_absolute_path,
        help="exclusive private document; manually edit, "
        "then enter its full SHA256 in this process",
    )


def add_handoff_commands(subparsers):
    handoff = subparsers.add_parser("handoff", help="explicit literal offline USER handoff")
    commands = handoff.add_subparsers(dest="handoff_command", required=True)
    for command in ("review", "export", "summary"):
        parser = commands.add_parser(command)
        _add_profiles(parser)
        _selection_arguments(parser)
        if command == "export":
            parser.add_argument("--document", type=_absolute_path, required=True)
        if command == "summary":
            _target_arguments(parser)
    parser = subparsers.add_parser("return-new", help="explicit qualified parent B to new A2")
    _add_profiles(parser)
    _selection_arguments(parser)
    _target_arguments(parser)
    parser.add_argument("--parent-operation-id", type=_identifier, required=True)
    parser.add_argument("--content-mode", choices=("full", "summary"), required=True)


class HandoffWorkflow:
    """One thread owns the readonly driver and its one-shot handles until close."""

    def __init__(self, catalog, observer, source, ref, state_home, environment):
        self.catalog, self.observer, self.source, self.ref = catalog, observer, source, ref
        self.state_home, self.environment = state_home, dict(environment)
        self.draft = self.exported = self.reviewed = self.prepared = None
        self.proposal = None

    def capture(self, ranges, client_version=None, *, source_bytes=8 * 1024 * 1024):
        self.clear()
        self.draft = prepare_handoff(
            self.ref,
            tuple(ranges),
            HandoffLimits(max_source_bytes=source_bytes),
            self.observer,
            client_version=client_version,
        )
        return handoff_metadata(self.draft)

    def document(self):
        return render_handoff(self.draft)

    def export(self, path):
        self.exported = export_handoff(self.draft, path)
        self.draft = None
        return handoff_metadata(self.exported)

    def accept(self, digest, *, text=None):
        if text is None:
            handle, self.exported = self.exported, None
            self.reviewed = accept_edit(handle, confirmed_sha256=digest)
        else:
            handle, self.draft = self.draft, None
            self.reviewed = accept_user_text(handle, text, confirmed_sha256=digest)
        return handoff_metadata(self.reviewed)

    def prepare(self, target, options, *, mode="summary", parent=None, cancellation=None):
        if self.prepared is not None:
            self.prepared.discard()
            self.prepared = None
        # Ownership moves before calling a consuming backend, including failure.
        reviewed, self.reviewed = self.reviewed, None
        try:
            if parent is None:
                if mode != "summary":
                    raise HandoffError("invalid_request")
                self.prepared = prepare_summary(
                    reviewed,
                    target,
                    options,
                    self.observer,
                    base_environ=self.environment,
                    state_home=self.state_home,
                    cancellation=cancellation,
                )
            else:
                self.prepared = prepare_return_new(
                    ReturnNewRequest(self.ref, parent, target, mode, options, reviewed),
                    self.observer,
                    self.state_home,
                    base_environ=self.environment,
                    cancellation=cancellation,
                )
        finally:
            if reviewed is not None:
                reviewed.discard()
        self.proposal = proposal_metadata(self.prepared).to_dict()
        return self.proposal

    def commit(self, *, cancellation=None):
        prepared, self.prepared = self.prepared, None
        try:
            metadata = proposal_metadata(prepared).to_dict()
            receipt = commit_operation(prepared, cancellation=cancellation)
            try:
                result = receipt.to_dict()
                json.dumps(result, allow_nan=False)
                return result
            except Exception:
                raise HandoffResultUnavailable(
                    metadata["proposal_id"], metadata["target"]["native_id"]
                ) from None
        finally:
            if prepared is not None:
                prepared.discard()

    def clear(self):
        for name in ("draft", "exported", "reviewed", "prepared"):
            handle = getattr(self, name)
            setattr(self, name, None)
            if handle is not None:
                handle.discard()
        self.proposal = None

    def close(self):
        self.clear()
        if self.catalog is not None:
            self.catalog.close()
            self.catalog = None


def action_document(status, *, proposal=None, result=None, reason=None):
    return dict(
        schema_version=1,
        status=status,
        proposal=proposal,
        result=result,
        reason=reason,
        update_available=False,
        native_cas_proven=False,
        boundary="unproven",
    )


def failure_document(error):
    if isinstance(error, HandoffResultUnavailable):
        return dict(
            action_document(
                "persistence_uncertain", reason="result_unavailable_inspect_same_operation"
            ),
            operation_id=error.operation_id,
            target_id=error.target_id,
        )
    if isinstance(error, OperationNeedsReconcileError):
        return dict(
            action_document(
                "persistence_uncertain"
                if isinstance(error, RuntimePersistenceUncertainError)
                else "needs_reconcile",
                reason="inspect_same_operation_no_retry",
            ),
            operation_id=error.operation_id,
            target_id=error.session_id,
            native_state=error.native_state.value,
            stage=error.stage,
            manifest=error.manifest,
            root=error.root,
            output=error.output,
            ledger_recorded=error.receipt.ledger_recorded,
            references=[asdict(reference) for reference in error.receipt.references],
        )
    code = error.code if isinstance(error, HandoffError) else "handoff_unavailable"
    from session_migrate.handoff_operations import HandoffOperationError

    if isinstance(error, HandoffOperationError):
        code = error.code
    return action_document("blocked", reason=code)


def _validate(args, parser):
    returning = args.command == "return-new"
    summary = (
        returning
        and args.content_mode == "summary"
        or getattr(args, "handoff_command", None) == "summary"
    )
    if args.source_profile is None or args.source_native_id is None:
        parser.error("choose an explicit current source profile and native ID")
    if (summary or returning) and args.target_profile is None:
        parser.error("choose an explicit current target profile")
    if not returning or args.content_mode == "summary":
        if not args.ranges or len(args.ranges) > 1024:
            parser.error("select between 1 and 1024 explicit event/block ranges")
    elif args.ranges or args.edit_export or args.source_client_version:
        parser.error("full return uses the current qualified parent capture, without summary input")
    if summary and args.edit_export is None:
        parser.error("summary requires --edit-export and same-process SHA256 review")
    if not summary and not returning and args.confirm:
        parser.error("review/export do not publish a target")
    for side in ("source", "target"):
        fields = [
            getattr(args, f"{side}_profile_{key}")
            for key in ("format", "root", "name", "cli", "env")
        ]
        if any(fields) and getattr(args, f"{side}_profile") is None:
            parser.error("current profile fields require their profile ID")
        if not args.profiles and any(fields) and not all(fields[:2]):
            parser.error("explicit current profiles require format and root")
        keys = [k for k, _ in getattr(args, f"{side}_profile_env")]
        if len(keys) != len(set(keys)):
            parser.error("duplicate current profile environment key")
    if args.target_native_id:
        parser.error("target is new; use --session-id for an explicit new identity")


def run_handoff_command(args, parser):
    _validate(args, parser)
    environment = {
        k: v for k, v in os.environ.items() if k in PUBLIC_ENVIRONMENT | EXECUTION_ENVIRONMENT
    }
    workflow = None
    try:
        state_home = args.state_home or default_migration_state_home(environ=environment)
        with _driver(args, environment) as (catalog, selected, observer):
            workflow = HandoffWorkflow(
                catalog,
                observer,
                selected["source"],
                _ref(selected["source"], args.source_native_id),
                state_home,
                environment,
            )
            returning = args.command == "return-new"
            mode = args.content_mode if returning else "summary"
            if not returning or mode == "summary":
                metadata = workflow.capture(args.ranges, args.source_client_version)
                if args.command == "handoff" and args.handoff_command == "review":
                    return _print(action_document("reviewed_metadata", result=metadata), args.json)
                if args.command == "handoff" and args.handoff_command == "export":
                    return _print(
                        action_document("exported", result=workflow.export(args.document)),
                        args.json,
                    )
                workflow.export(args.edit_export)
                # No editor, subprocess, network or receipt JSON is executed/replayed.
                print(
                    "Edit the entire private document, then enter its SHA256. "
                    "No target is published yet.",
                    file=sys.stderr,
                )
                workflow.accept(input().strip())
            options = OperationRequest(
                "transfer",
                cwd=args.cwd,
                session_id=args.session_id,
                target_cli_version=args.target_cli_version,
                model_provider=args.model_provider,
                model=args.model,
                target_title=args.target_title,
            )
            proposal = workflow.prepare(
                selected["target"],
                options,
                mode=mode,
                parent=args.parent_operation_id if returning else None,
            )
            if not args.confirm:
                return _print(action_document("prepared", proposal=proposal), args.json)
            return _print(
                action_document("completed", proposal=proposal, result=workflow.commit()), args.json
            )
    except (
        SessionMigrateError,
        OSError,
        ValueError,
        TypeError,
        AssertionError,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        return _print(failure_document(error), args.json, 1)
    finally:
        if workflow is not None:
            # _driver owns the catalog; discard handles before its reference goes away.
            workflow.clear()


def _print(document, as_json, code=0):
    if as_json:
        print(json.dumps(document, sort_keys=True, ensure_ascii=True))
    else:
        print(f"status: {document['status']}")
        print(f"reason: {document['reason'] or '-'}")
        print(json.dumps(document, sort_keys=True, ensure_ascii=True))
    return code
