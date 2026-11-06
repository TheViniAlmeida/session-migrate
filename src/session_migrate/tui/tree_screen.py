"""Literal tree review with worker-owned capabilities and observable cancellation."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Select, Tree

from session_migrate.operations import CancellationToken
from session_migrate.tree_cli import tree_failure
from session_migrate.tui.widgets import Literal


class TreeScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back / cancel", priority=True),
        Binding("ctrl+q", "quit_safe", "Quit", priority=True),
    ]

    def __init__(self, controller, *, profiles_file=None):
        super().__init__()
        self.controller, self.profiles_file = controller, profiles_file
        self.actor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tree-ui")
        self.workflow = self.future = self.pending = self.token = None
        self.generation = 0
        self.finished = False
        self._loading = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Literal(
            "Persistent tree: inventory → select → review → confirm once.", id="tree-status"
        )
        with VerticalScroll(id="tree-form"):
            yield Input(
                str(self.profiles_file or ""),
                placeholder="Existing private profiles file",
                id="tree-profiles",
            )
            yield Input(placeholder="Existing source profile ID", id="tree-source")
            yield Input(placeholder="Exact source client version", id="tree-version")
            yield Input(
                placeholder="Explicit manual inventory file (empty: native metadata)",
                id="tree-manifest",
            )
            yield Button("Read inventory", id="tree-inventory")
            yield Tree("Sessions — select a node to choose the anchor", id="tree-browser")
            yield Select(
                [
                    ("Single persistent node", "single"),
                    ("Selected subtree", "subtree"),
                    ("Complete tree", "full_tree"),
                ],
                value="subtree",
                allow_blank=False,
                id="tree-mode",
            )
            yield Input(placeholder="Anchor node key (empty: inventory root)", id="tree-anchor")
            yield Input(
                placeholder="Hierarchy edge keys, comma separated (empty: all declared)",
                id="tree-edges",
            )
            yield Checkbox("Explicitly detach anchor from omitted parent", id="tree-detach")
            yield Checkbox("Acknowledge partial inventory or omitted nodes", id="tree-partial")
            yield Checkbox(
                "Reviewed conversation portability: compacted Claude 2.1.309 → OpenCode 2.2.5",
                id="tree-reviewed-portability",
            )
            yield Select(
                [
                    ("Independent sessions — editable; external organizational map", "independent"),
                    ("Native subagents — child composers disabled in stock UI", "native-subagent"),
                ],
                value="independent",
                allow_blank=False,
                id="tree-publication-mode",
            )
            yield Literal(
                "No native fork boundary or peer communication is provisioned. "
                "Resume retains the operation's recorded publication mode."
            )
            yield Input(placeholder="Existing destination profile ID", id="tree-target")
            yield Input(placeholder="Absolute project CWD", id="tree-cwd")
            yield Input(placeholder="Model provider", id="tree-provider")
            yield Input(placeholder="Model", id="tree-model")
            yield Input(
                placeholder="Operation ID to inspect/reconcile/resume (optional)",
                id="tree-operation",
            )
            with Horizontal():
                yield Button("Review selection", id="tree-prepare")
                yield Button("Review resume", id="tree-resume")
                yield Button("Inspect", id="tree-inspect")
                yield Button("Reconcile (read only)", id="tree-reconcile")
            yield Literal(
                "No live proposal. Inventory declarations are recaptured before publication.",
                id="tree-metadata",
            )
            with Horizontal():
                yield Button("Confirm reviewed tree once", id="tree-confirm", disabled=True)
                yield Button("Cancel / back", id="tree-back")
        yield Footer()

    def on_mount(self):
        self._loading = False
        self.set_interval(0.05, self.poll_job)

    def value(self, name):
        return self.query_one(f"#tree-{name}", Input).value.strip()

    def status(self, value):
        self.query_one("#tree-status", Literal).set_text(value)

    def show_tree(self, document):
        if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
            return
        widget = self.query_one("#tree-browser", Tree)
        widget.clear()
        rows = {
            row["key"]: row for row in document["nodes"] if isinstance(row, dict) and "key" in row
        }
        targets = {row.get("target_id"): key for key, row in rows.items() if row.get("target_id")}
        parents = {
            key: targets.get(
                row.get("organizational_parent_target_id", row.get("parent_target_id"))
            )
            for key, row in rows.items()
        }
        for edge in document.get("relationships", []):
            if isinstance(edge, dict) and edge.get("child_node_key") in rows:
                parents[edge["child_node_key"]] = edge.get("parent_node_key")
        structure = document.get("structure", {})
        if isinstance(structure, dict) and isinstance(structure.get("parent_map"), dict):
            parents.update(structure["parent_map"])
        rendered, remaining = {}, dict(rows)
        while remaining:
            ready = [key for key in remaining if parents.get(key) not in remaining]
            if not ready:
                break  # Invalid metadata is not rendered as a misleading hierarchy.
            for key in ready:
                row = remaining.pop(key)
                label = row.get("title") or row.get("source_native_id") or key
                parent = rendered.get(parents.get(key), widget.root)
                rendered[key] = parent.add(Text(f"{label}  [{key}]"), data=key, expand=True)
        widget.root.expand()

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        event.stop()
        if self.pending is None and not self.finished and isinstance(event.node.data, str):
            self.query_one("#tree-anchor", Input).value = event.node.data
            self.status("Anchor selected. Review the selection before confirming.")

    def _close(self):
        if self.workflow is not None:
            self.workflow.close()
            self.workflow = None

    def _open(self, selection, *, reviewed_portability=False):
        self._close()
        self.workflow = self.controller.open_tree(
            profiles_file=selection["profiles_file"],
            **({"reviewed_portability": True} if reviewed_portability else {}),
        )
        self.workflow.load(
            source_profile_id=selection["source"],
            client_version=selection["version"],
            manifest=selection["manifest"],
        )
        return self.workflow

    def _selection(self):
        profiles, manifest = self.value("profiles"), self.value("manifest")
        for value in (profiles, manifest):
            if value and (not Path(value).is_absolute() or ".." in Path(value).parts):
                raise ValueError("absolute_path_required")
        return {
            "profiles_file": Path(profiles) if profiles else None,
            "manifest": Path(manifest) if manifest else None,
            "source": self.value("source"),
            "version": self.value("version"),
        }

    def launch(self, kind, work):
        if self.future is not None:
            return
        generation = self.generation

        def execute():
            try:
                return generation, kind, work(), None
            except Exception as error:
                return generation, kind, None, tree_failure(error)

        self.pending = kind
        self.future = self.actor.submit(execute)
        self.query_one("#tree-confirm", Button).disabled = True
        for widget in self.query("Input, Select, Checkbox"):
            widget.disabled = True
        self.status("Working. Cancellation keeps the same operation and any published receipts.")

    def _invalidate(self):
        self.generation += 1
        self.query_one("#tree-confirm", Button).disabled = True
        self.actor.submit(self._close)

    def on_input_changed(self, event: Input.Changed):
        event.stop()
        if not self._loading and self.is_mounted and self.pending is None:
            self._invalidate()

    def on_select_changed(self, event: Select.Changed):
        event.stop()
        if not self._loading and self.is_mounted and self.pending is None:
            self._invalidate()

    def on_checkbox_changed(self, event: Checkbox.Changed):
        event.stop()
        if not self._loading and self.is_mounted and self.pending is None:
            if event.checkbox.id == "tree-reviewed-portability":
                self.query_one("#tree-partial", Checkbox).label = (
                    "Acknowledge partial inventory, omissions and displayed portability differences"
                    if event.value
                    else "Acknowledge partial inventory or omitted nodes"
                )
            self._invalidate()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        kind = (event.button.id or "").removeprefix("tree-")
        if kind == "back":
            self.action_back()
            return
        if self.future is not None:
            return
        if kind in {"inspect", "reconcile"}:
            operation_id = self.value("operation")
            if not operation_id:
                self.status("Enter the exact existing tree operation ID.")
                return

            def inspect():
                from session_migrate import tree_operations

                workflow = self.controller.open_tree(profiles_file=None)
                try:
                    return getattr(tree_operations, kind + "_tree")(
                        workflow.state_home, operation_id
                    )
                finally:
                    workflow.close()

            self.launch(kind, inspect)
            return
        if self.finished:
            self.status("Inspect this operation or return to open a fresh review.")
            return
        if kind == "confirm":
            self.token = CancellationToken()
            self.launch("commit", lambda: self.workflow.commit(cancellation=self.token))
            return
        try:
            selection = self._selection()
            mode = self.query_one("#tree-mode", Select).value
            publication_mode = self.query_one("#tree-publication-mode", Select).value
            anchor = self.value("anchor") or None
            edges = [
                value.strip() for value in self.value("edges").split(",") if value.strip()
            ] or None
            detach = self.query_one("#tree-detach", Checkbox).value
            partial = self.query_one("#tree-partial", Checkbox).value
            reviewed = self.query_one("#tree-reviewed-portability", Checkbox).value
            target, cwd = self.value("target"), Path(self.value("cwd"))
            provider, model = self.value("provider"), self.value("model")
            operation_id = self.value("operation") or None
            if kind != "inventory" and (
                not cwd.is_absolute()
                or ".." in cwd.parts
                or not all((target, provider, model))
                or (kind == "resume" and not operation_id)
            ):
                raise ValueError("explicit_destination_required")
        except ValueError:
            self.status("Choose current profiles, absolute paths and explicit destination options.")
            return

        def work():
            effective_reviewed = reviewed
            if kind == "resume":
                from session_migrate.tree_operations import inspect_tree

                recorded = inspect_tree(self.controller._continuity_path().parent, operation_id)
                if (
                    type(recorded) is not dict
                    or type(recorded.get("reviewed_portability", False)) is not bool
                ):
                    raise ValueError("tree_reviewed_mode_unavailable")
                effective_reviewed = recorded.get("reviewed_portability", False)
            workflow = self._open(selection, reviewed_portability=effective_reviewed)
            if kind == "inventory":
                return workflow.inventory_metadata()
            workflow.select(mode=mode, anchor=anchor, detach=detach, hierarchy_keys=edges)
            return workflow.prepare(
                target,
                cwd=cwd,
                model_provider=provider,
                model=model,
                acknowledge_partial=partial,
                operation_id=operation_id,
                resume=kind == "resume",
                **({"publication_mode": publication_mode} if kind != "resume" else {}),
                **({"reviewed_portability": True} if kind != "resume" and reviewed else {}),
            )

        self.launch("prepare" if kind in {"prepare", "resume"} else kind, work)

    def poll_job(self):
        if self.future is None or not self.future.done():
            return
        generation, kind, result, error = self.future.result()
        self.future = self.pending = None
        for widget in self.query("Input, Select, Checkbox"):
            widget.disabled = False
        if generation != self.generation and kind != "commit":
            self.actor.submit(self._close)
            return
        document = error if error is not None else result
        if error is None:
            self.show_tree(document)
        self.query_one("#tree-metadata", Literal).set_text(
            json.dumps(document, sort_keys=True, ensure_ascii=True, indent=2)
        )
        if kind == "commit":
            self.finished = True
            self.status(
                "Publication finished. Inspect partial or uncertain nodes "
                "before any explicit resume."
            )
        elif error is not None:
            self.status("Action blocked. Check current inputs and review again.")
        elif kind == "prepare":
            reviewed = result.get("reviewed_portability", False) is True
            self._loading = True
            try:
                with self.prevent(Checkbox.Changed):
                    self.query_one("#tree-reviewed-portability", Checkbox).value = reviewed
            finally:
                self._loading = False
            self.query_one("#tree-confirm", Button).label = (
                "Accept displayed differences and publish tree once"
                if reviewed
                else "Confirm reviewed tree once"
            )
            needs_ack = result.get("requires_partial_acknowledgement", False) is True
            self.query_one("#tree-confirm", Button).disabled = (
                needs_ack and not self.query_one("#tree-partial", Checkbox).value
            )
            self.query_one("#tree-partial", Checkbox).label = (
                "Acknowledge partial inventory, omissions and displayed portability differences"
                if reviewed
                else "Acknowledge partial inventory or omitted nodes"
            )
            publication_mode = result.get("publication_mode", "native-subagent")
            self.status(
                f"Publication mode: {publication_mode}. Reviewed portability: {reviewed}. "
                "Review titles, exact selected nodes, relationships, IDs and losses; "
                "then confirm once."
            )
        else:
            self.status("Read complete. Saved JSON never authorizes publication.")

    def action_back(self):
        if self.pending == "commit":
            cancelled = self.token.request_cancel()
            self.status(
                "Cancellation requested. Waiting for the receipt of this operation."
                if cancelled
                else "Publication is active. Waiting for the same operation's receipt."
            )
            return
        self.generation += 1
        self.dismiss()

    def action_quit_safe(self):
        if self.pending == "commit":
            self.action_back()
            return
        self.app.exit()

    def on_unmount(self):
        if self.token:
            self.token.request_cancel()
        self.actor.submit(self._close)
        self.actor.shutdown(wait=False)
