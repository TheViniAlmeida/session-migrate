"""Explicit offline review and return-new; independent of the main transfer draft."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Select, TextArea

from session_migrate.handoff_cli import failure_document, selection_range
from session_migrate.operations import CancellationToken, OperationRequest
from session_migrate.tui.widgets import Literal


class HandoffScreen(Screen):
    """One bounded private actor; no automatic editor, provider or resume."""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("ctrl+q", "quit_safe", "Quit", priority=True),
    ]

    def __init__(self, controller, *, profiles_file=None):
        super().__init__()
        self.controller, self.profiles_file = controller, profiles_file
        self.actor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="handoff-ui")
        self.workflow = None
        self.future = None
        self.pending = None
        self.generation = 0
        self.token = None
        self.uncertain = False
        self.finished = False
        self._loading = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Literal(
            "Offline USER handoff / return-new; update and native CAS unavailable.",
            id="handoff-status",
        )
        with VerticalScroll(id="handoff-form"):
            yield Input(
                str(self.profiles_file) if self.profiles_file else "",
                placeholder="Current private profiles file (optional, absolute)",
                id="handoff-profiles",
            )
            yield Input(placeholder="Current source profile ID", id="handoff-source")
            yield Input(placeholder="Exact current source native ID", id="handoff-native-id")
            yield Input(
                placeholder="Explicit FIRST:LAST[:BLOCK] ranges, comma separated",
                id="handoff-ranges",
            )
            yield Input(placeholder="Exact source client version", id="handoff-source-version")
            yield Literal(
                "Capture uses actual source events, never the clipped preview. "
                "Selected text is literal data."
            )
            with Horizontal():
                yield Button("Capture selected ranges", id="handoff-capture")
                yield Button("Back / cancel", id="handoff-back")
            yield TextArea(id="handoff-document")
            yield Input(
                placeholder="New absolute file in an existing owned 0700 directory",
                id="handoff-export-path",
            )
            yield Input(
                placeholder="SHA256 of the complete accepted UTF-8 document", id="handoff-digest"
            )
            with Horizontal():
                yield Button("Export for manual editing", id="handoff-export", disabled=True)
                yield Button(
                    "Accept edited export + SHA", id="handoff-accept-export", disabled=True
                )
                yield Button("Accept complete text + SHA", id="handoff-accept-text", disabled=True)
            yield Input(
                placeholder="Explicit parent operation ID for return-new (otherwise summary)",
                id="handoff-parent",
            )
            yield Select(
                [("Summary USER input", "summary"), ("Full return-new", "full")],
                value="summary",
                allow_blank=False,
                id="handoff-mode",
            )
            yield Input(placeholder="Current target profile ID", id="handoff-target")
            yield Input(placeholder="Absolute project CWD", id="handoff-cwd")
            yield Input(placeholder="Exact target client version", id="handoff-target-version")
            yield Input(placeholder="Explicit model provider", id="handoff-provider")
            yield Input(placeholder="Explicit model", id="handoff-model")
            yield Input(placeholder="Optional new target ID", id="handoff-target-id")
            yield Input(placeholder="Optional destination title (literal)", id="handoff-title")
            yield Button("Prepare new session", id="handoff-prepare")
            yield Literal(
                "No proposal yet. Export/read may use private scratch or native cache/WAL.",
                id="handoff-metadata",
            )
            yield Button(
                "Confirm this prepared new session once", id="handoff-confirm", disabled=True
            )
        yield Footer()

    def on_mount(self):
        self._loading = False
        self.set_interval(0.05, self.poll_job)

    def value(self, identifier):
        return self.query_one(f"#handoff-{identifier}", Input).value

    def status(self, text):
        self.query_one("#handoff-status", Literal).set_text(text)

    def _close_workflow(self):
        if self.workflow is not None:
            self.workflow.close()
            self.workflow = None

    def _source_selection(self):
        path = self.value("profiles").strip()
        if path and (not Path(path).is_absolute() or ".." in Path(path).parts):
            raise ValueError("invalid profile path")
        return (
            self.value("source").strip(),
            self.value("native-id").strip(),
            Path(path) if path else None,
        )

    def _open(self, selection):
        if self.workflow is None:
            self.workflow = self.controller.open_handoff(
                selection[0],
                selection[1],
                profiles_file=selection[2],
            )
        return self.workflow

    def launch(self, kind, work):
        if self.future is not None or self.uncertain or self.finished:
            self.status(
                "Wait for the active task or inspect the completed/uncertain operation. No retry."
            )
            return
        generation = self.generation

        def execute():
            try:
                return generation, kind, work(), None
            except Exception as error:
                return generation, kind, None, failure_document(error)

        self.pending = kind
        self.future = self.actor.submit(execute)
        self.query_one("#handoff-confirm", Button).disabled = True
        self.status("Working; no editor/provider/resume is executed.")

    def _invalidate(self, *, source=False):
        # This check is independent of progress text and happens before generation changes.
        if self.pending == "commit" and self.token and not self.token.request_cancel():
            self.status(
                "Publication has started. Keep observing the same operation; "
                "cancellation is closed."
            )
            return False
        if self.token and self.pending == "prepare":
            self.token.request_cancel()
        self.generation += 1
        self.query_one("#handoff-confirm", Button).disabled = True
        if self.future is None:
            # Actor-local cleanup keeps SQLite access in its owning thread.
            self.launch(
                "clear",
                self._close_workflow
                if source
                else lambda: self.workflow.clear() if self.workflow else None,
            )
        return True

    def on_input_changed(self, event: Input.Changed):
        event.stop()
        if self._loading or not self.is_mounted:
            return
        identifier = event.input.id
        if identifier in {"handoff-digest", "handoff-export-path"}:
            return
        # Source edits invalidate all authority; destination edits preserve USER review.
        source = identifier in {
            "handoff-profiles",
            "handoff-source",
            "handoff-native-id",
            "handoff-ranges",
            "handoff-source-version",
        }
        if source:
            self._invalidate(source=True)
        elif self.pending in {"prepare", "commit"} or (
            self.workflow is not None and self.workflow.prepared is not None
        ):
            self._invalidate()

    def on_select_changed(self, event: Select.Changed):
        event.stop()
        if (
            not self._loading
            and self.is_mounted
            and (
                self.pending in {"prepare", "commit"}
                or (self.workflow is not None and self.workflow.prepared is not None)
            )
        ):
            self._invalidate()

    def on_text_area_changed(self, event: TextArea.Changed):
        event.stop()
        if (
            not self._loading
            and self.is_mounted
            and (
                self.pending in {"accept", "prepare", "commit"}
                or (
                    self.workflow is not None
                    and (self.workflow.prepared is not None or self.workflow.reviewed is not None)
                )
            )
        ):
            self._invalidate()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        identifier = event.button.id
        if identifier == "handoff-back":
            self.action_back()
            return
        if self.future is not None or self.uncertain or self.finished:
            return
        if identifier == "handoff-capture":
            try:
                ranges = tuple(
                    selection_range(value.strip()) for value in self.value("ranges").split(",")
                )
            except Exception:
                self.status("Enter explicit valid inclusive ranges.")
                return
            version = self.value("source-version").strip() or None
            try:
                source_selection = self._source_selection()
            except ValueError:
                self.status("Choose an absolute current profiles path.")
                return

            def capture():
                workflow = self._open(source_selection)
                metadata = workflow.capture(
                    ranges, version, source_bytes=self.controller.source_bytes
                )
                return metadata, workflow.document()

            self.launch("capture", capture)
        elif identifier == "handoff-export":
            path = Path(self.value("export-path"))
            self.launch("export", lambda: self.workflow.export(path))
        elif identifier in {"handoff-accept-text", "handoff-accept-export"}:
            digest = self.value("digest")
            text = (
                self.query_one("#handoff-document", TextArea).text
                if identifier.endswith("text")
                else None
            )
            self.launch("accept", lambda: self.workflow.accept(digest, text=text))
        elif identifier == "handoff-prepare":
            mode = self.query_one("#handoff-mode", Select).value
            parent = self.value("parent").strip() or None
            target_id = self.value("target").strip()
            cwd = Path(self.value("cwd"))
            version, provider, model = (
                self.value(key).strip() for key in ("target-version", "provider", "model")
            )
            if (
                not cwd.is_absolute()
                or ".." in cwd.parts
                or not all((version, provider, model))
                or (mode == "full" and parent is None)
            ):
                self.status(
                    "Choose explicit target/CWD/version/model/provider "
                    "and a parent for full return."
                )
                return
            options = OperationRequest(
                "transfer",
                cwd=cwd,
                target_cli_version=version,
                model_provider=provider,
                model=model,
                session_id=self.value("target-id").strip() or None,
                target_title=self.value("title") or None,
            )
            self.token = CancellationToken()
            try:
                source_selection = self._source_selection()
            except ValueError:
                self.status("Choose an absolute current profiles path.")
                return

            def prepare():
                workflow = self._open(source_selection)
                matches = [p for p in workflow.observer._profiles if p.id == target_id]
                if len(matches) != 1:
                    raise ValueError("unavailable target")
                return workflow.prepare(
                    matches[0], options, mode=mode, parent=parent, cancellation=self.token
                )

            self.launch("prepare", prepare)
        elif identifier == "handoff-confirm":
            if self.workflow is None or self.workflow.prepared is None:
                return
            # Preparation closes its token; commit claims a fresh publication token.
            self.token = CancellationToken()
            self.launch("commit", lambda: self.workflow.commit(cancellation=self.token))

    def poll_job(self):
        if self.future is None or not self.future.done():
            return
        generation, kind, result, error = self.future.result()
        self.future, self.pending = None, None
        if generation != self.generation:
            self.launch("clear", self._close_workflow)
            return
        if error is not None:
            self.uncertain = error["status"] in {"needs_reconcile", "persistence_uncertain"}
            self.query_one("#handoff-metadata", Literal).set_text(json.dumps(error, sort_keys=True))
            self.status(
                "Inspect the same operation; do not retry."
                if self.uncertain
                else "Action blocked. Recapture and review; errors contain no document body."
            )
            return
        if kind == "capture":
            metadata, document = result
            self._loading = True
            self.query_one("#handoff-document", TextArea).load_text(document)
            self._loading = False
            for name in ("export", "accept-text"):
                self.query_one(f"#handoff-{name}", Button).disabled = False
            result = metadata
        elif kind == "export":
            self.query_one("#handoff-accept-export", Button).disabled = False
            for name in ("export", "accept-text"):
                self.query_one(f"#handoff-{name}", Button).disabled = True
        elif kind == "accept":
            for name in ("export", "accept-text", "accept-export"):
                self.query_one(f"#handoff-{name}", Button).disabled = True
        elif kind == "prepare":
            self.query_one("#handoff-confirm", Button).disabled = False
        elif kind == "commit":
            self.finished = True
        if result is not None:
            self.query_one("#handoff-metadata", Literal).set_text(
                json.dumps(result, sort_keys=True, ensure_ascii=True)
            )
        self.status(
            "Completed. Resume manually; no native closure/update proof is claimed."
            if kind == "commit"
            else "Ready. Confirm only this process's current handles."
        )

    def action_back(self):
        was_reading = self.pending in {"capture", "export", "accept"}
        if not self._invalidate(source=True):
            return
        if was_reading:
            self.app.status(
                "Handoff read abandoned. An active native export may finish in the background."
            )
        self.dismiss()

    def action_quit_safe(self):
        if not self._invalidate(source=True):
            return
        self.app.exit()

    def on_unmount(self):
        if self.token:
            self.token.request_cancel()
        # Queued cleanup follows the sole running task and cannot stop a native job.
        self.actor.submit(self._close_workflow)
        self.actor.shutdown(wait=False)
