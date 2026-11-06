"""Explicit tree review shared by the CLI and interactive frontends.

JSON is always an input declaration or a metadata view. Only the current
process's sealed publisher handle can authorize the reviewed publication.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from session_migrate.conversion import default_migration_state_home
from session_migrate.errors import SessionMigrateError
from session_migrate.operations import CancellationToken
from session_migrate.profiles import Profile, ProfileStore
from session_migrate.tree_model import (
    TreeSelection,
    TreeValidationError,
    build_tree_plan,
    inventory_to_dict,
)
from session_migrate.tree_sources import (
    TreeSourceError,
    inventory_from_explicit_manifest,
    inventory_from_native_metadata,
)


class TreeWorkflow:
    """One explicit source inventory and one consumable prepared capability."""

    def __init__(
        self,
        profiles=(),
        *,
        state_home=None,
        base_environ=None,
        max_source_bytes=None,
        max_total_source_bytes=None,
    ):
        self.profiles = tuple(profiles)
        if any(type(profile) is not Profile for profile in self.profiles) or len(
            {profile.id for profile in self.profiles}
        ) != len(self.profiles):
            raise SessionMigrateError("tree_profiles_invalid")
        self.environ = dict(os.environ if base_environ is None else base_environ)
        self.state_home = state_home or default_migration_state_home(environ=self.environ)
        self.inventory = self.source = self.plan = self.prepared = None
        self._guard = None
        self._committed = False
        self.limits = {}
        if max_source_bytes is not None:
            self.limits["max_source_bytes"] = max_source_bytes
        if max_total_source_bytes is not None:
            self.limits["max_total_source_bytes"] = max_total_source_bytes

    def profile(self, identifier):
        matches = [profile for profile in self.profiles if profile.id == identifier]
        if len(matches) != 1:
            raise SessionMigrateError("tree_profile_unavailable")
        return matches[0]

    def load(
        self,
        *,
        source_profile_id=None,
        root=None,
        source_format=None,
        client_version,
        manifest=None,
        relative_paths=None,
        root_native_id=None,
    ):
        self.clear()
        self.source = self.profile(source_profile_id) if source_profile_id else None
        if self.source is not None:
            if root is not None and Path(root) != self.source.root:
                raise SessionMigrateError("tree_source_root_mismatch")
            if source_format is not None and source_format != self.source.format.value:
                raise SessionMigrateError("tree_source_format_mismatch")
            root, source_format = self.source.root, self.source.format.value
        if root is None or source_format is None:
            raise SessionMigrateError("tree_explicit_source_required")
        if manifest is not None:
            if relative_paths or root_native_id:
                raise SessionMigrateError("tree_manifest_options_conflict")
            self.inventory = inventory_from_explicit_manifest(
                manifest,
                root=Path(root),
                source_format=source_format,
                client_version=client_version,
                **self.limits,
            )
        else:
            self.inventory = inventory_from_native_metadata(
                root=Path(root),
                source_format=source_format,
                client_version=client_version,
                relative_paths=relative_paths,
                root_native_id=root_native_id,
                **self.limits,
            )
        return self.inventory_metadata()

    def inventory_metadata(self):
        if self.inventory is None:
            raise SessionMigrateError("tree_inventory_required")
        return {
            "schema_version": 1,
            "kind": "session-tree-inventory-view",
            "authority": "metadata_only_recapture_required",
            **inventory_to_dict(self.inventory),
        }

    def select(self, *, mode="subtree", anchor=None, detach=False, hierarchy_keys=None):
        if self.inventory is None:
            raise SessionMigrateError("tree_inventory_required")
        self.clear_prepared()
        selection = TreeSelection(
            mode,
            anchor or self.inventory.root_node_key,
            hierarchy_relationship_keys=tuple(edge.key for edge in self.inventory.relationships)
            if hierarchy_keys is None
            else tuple(hierarchy_keys),
            boundary_policy="detach_explicit" if detach else "require_closed",
        )
        # Pure closure and boundary checks happen before any target probe.
        self.plan = build_tree_plan(self.inventory, selection, str(uuid.uuid4()))
        return selection

    def prepare(
        self,
        target_profile_id,
        *,
        cwd,
        model_provider,
        model,
        acknowledge_partial=False,
        publication_mode=None,
        reviewed_portability=None,
        operation_id=None,
        resume=False,
        prepublication_check=None,
    ):
        from session_migrate import tree_operations

        if self.plan is None or self.inventory is None or self._committed:
            raise SessionMigrateError("tree_selection_required")
        self.clear_prepared()
        target = self.profile(target_profile_id)
        if prepublication_check is not None:
            prepublication_check()
        self._guard = prepublication_check
        options = dict(
            target=target,
            cwd=Path(cwd),
            model_provider=model_provider,
            model=model,
            operation_id=operation_id,
            state_home=self.state_home,
            base_environ=self.environ,
            source_profile=self.source,
            acknowledge_partial=acknowledge_partial,
            publication_mode=publication_mode,
            reviewed_portability=reviewed_portability,
            prepublication_check=prepublication_check,
            **self.limits,
        )
        prepare = tree_operations.prepare_tree_resume if resume else tree_operations.prepare_tree
        self.prepared = prepare(self.inventory, self.plan.selection, **options)
        try:
            metadata = tree_operations.tree_proposal(self.prepared)
            self.plan = build_tree_plan(
                self.inventory, self.plan.selection, metadata["operation_id"]
            )
            return self.proposal()
        except Exception:
            self.clear_prepared()
            raise

    def proposal(self):
        from session_migrate.tree_operations import tree_proposal

        if self.prepared is None:
            raise SessionMigrateError("tree_live_proposal_required")
        metadata = tree_proposal(self.prepared)
        # Include the exact selected structure, not unrelated inventory nodes.
        metadata["structure"] = {
            "nodes": [
                {"key": node.key, "title": node.title, "source_native_id": node.source_native_id}
                for node in self.plan.ordered_nodes
            ],
            "relationships": [
                {
                    "key": edge.key,
                    "parent_node_key": edge.parent_node_key,
                    "child_node_key": edge.child_node_key,
                    "kind": edge.kind,
                    "provenance": edge.provenance,
                }
                for edge in self.plan.selected_relationships
            ],
        }
        return metadata

    def commit(self, *, cancellation=None, progress=None):
        from session_migrate.tree_operations import commit_tree

        if self.prepared is None or self._committed:
            raise SessionMigrateError("tree_confirmation_consumed")
        if self._guard is not None:
            self._guard()
        prepared, self.prepared = self.prepared, None
        self._committed = True
        try:
            return commit_tree(
                prepared, cancellation=cancellation or CancellationToken(), progress=progress
            ).to_dict()
        finally:
            prepared.discard()

    def clear_prepared(self):
        if self.prepared is not None:
            self.prepared.discard()
            self.prepared = None

    def clear(self):
        self.clear_prepared()
        self.inventory = self.source = self.plan = None
        self._committed = False
        self._guard = None

    close = clear
    discard = clear


def add_tree_commands(subparsers):
    parser = subparsers.add_parser("tree", help="inventory and explicitly migrate persistent trees")
    commands = parser.add_subparsers(dest="tree_command", required=True)
    for name in ("inventory", "review", "migrate", "resume"):
        command = commands.add_parser(name)
        command.add_argument(
            "--profiles",
            type=Path,
            required=name != "inventory",
            help="existing private profiles file",
        )
        command.add_argument("--source-profile", help="exact saved source profile ID")
        command.add_argument("--root", type=Path, help="explicit native source root")
        command.add_argument("--format", choices=("claude", "codex", "opencode"))
        command.add_argument("--source-client-version", required=True)
        command.add_argument("--manifest", type=Path, help="explicit manual inventory declaration")
        command.add_argument("--relative-path", action="append", help="exact native relative path")
        command.add_argument("--root-native-id", help="explicit native inventory root ID")
        if name == "inventory":
            continue
        command.add_argument("--target-profile", required=True)
        command.add_argument("--cwd", type=Path, required=True)
        command.add_argument("--model-provider", required=True)
        command.add_argument("--model", required=True)
        command.add_argument(
            "--mode", choices=("single", "subtree", "full_tree"), default="subtree"
        )
        command.add_argument("--anchor", help="exact inventory node key (defaults to root)")
        command.add_argument(
            "--hierarchy-edge", action="append", help="reviewed hierarchy edge key"
        )
        command.add_argument(
            "--detach", action="store_true", help="explicitly detach the selected anchor"
        )
        command.add_argument(
            "--publication-mode",
            choices=("independent", "native-subagent"),
            help=(
                "independent roots by default; resume recovers the saved mode. "
                "native-subagent sets parentID and disables the child composer in OpenCode 2.2.5"
            ),
        )
        command.add_argument("--acknowledge-partial", action="store_true")
        command.add_argument(
            "--reviewed-portability",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=(
                "review runtime/order differences for preserved Claude 2.1.309 context; "
                "resume preserves its mode"
            ),
        )
        command.add_argument("--operation-id", required=name == "resume")
        if name != "review":
            command.add_argument(
                "--yes", action="store_true", help="explicitly confirm this fresh review"
            )
    for name in ("inspect", "reconcile"):
        command = commands.add_parser(name, help="inspect the same journal; never reimport")
        command.add_argument("operation_id")


def tree_failure(error):
    """Return content-free diagnostics; partial journal evidence remains discoverable."""
    value = getattr(error, "result", None)
    if value is not None and callable(getattr(value, "to_dict", None)):
        return value.to_dict()
    result = {"schema_version": 1, "status": "blocked", "reason": "tree_action_failed"}
    if isinstance(error, (TreeSourceError, TreeValidationError)):
        # These types explicitly guarantee content-safe adapter diagnostics.
        result["detail"] = str(error)
    return result


def run_tree_command(args, parser):
    workflow = None
    try:
        state_home = args.state_home or default_migration_state_home()
        if args.tree_command in {"inspect", "reconcile"}:
            from session_migrate import tree_operations

            operation = getattr(tree_operations, args.tree_command + "_tree")
            print(json.dumps(operation(state_home, args.operation_id), indent=2, sort_keys=True))
            return 0
        profiles = ProfileStore(args.profiles.expanduser()).load() if args.profiles else ()
        workflow = TreeWorkflow(profiles, state_home=state_home)
        result = workflow.load(
            source_profile_id=args.source_profile,
            root=args.root.expanduser() if args.root else None,
            source_format=args.format,
            client_version=args.source_client_version,
            manifest=args.manifest.expanduser() if args.manifest else None,
            relative_paths=args.relative_path,
            root_native_id=args.root_native_id,
        )
        if args.tree_command == "inventory":
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        workflow.select(
            mode=args.mode,
            anchor=args.anchor,
            detach=args.detach,
            hierarchy_keys=args.hierarchy_edge,
        )
        result = workflow.prepare(
            args.target_profile,
            cwd=args.cwd.expanduser(),
            model_provider=args.model_provider,
            model=args.model,
            operation_id=args.operation_id,
            acknowledge_partial=args.acknowledge_partial,
            publication_mode=args.publication_mode,
            reviewed_portability=args.reviewed_portability,
            resume=args.tree_command == "resume",
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.tree_command == "review":
            return 0
        confirmed = args.yes
        if not confirmed and sys.stdin.isatty():
            print(
                "Publish exactly this reviewed tree? Type yes: ",
                file=sys.stderr,
                end="",
                flush=True,
            )
            confirmed = sys.stdin.readline().strip().lower() == "yes"
        if not confirmed:
            print(
                "session-migrate: tree confirmation required (type yes or pass --yes).",
                file=sys.stderr,
            )
            return 2
        result = workflow.commit()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "completed" else 2
    except (SessionMigrateError, TreeValidationError, OSError, ValueError) as error:
        print(json.dumps(tree_failure(error), sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if workflow is not None:
            workflow.close()
