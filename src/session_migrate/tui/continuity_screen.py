"""Independent readonly operations browser; literal metadata and C1 decisions."""

from __future__ import annotations

import threading
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, OptionList
from textual.widgets.option_list import Option

from session_migrate.continuity import ContinuityAssessmentV2, ContinuitySelector
from session_migrate.errors import SessionMigrateError
from session_migrate.lineage import CompletionReceipt, ReconcileReceipt
from session_migrate.tui.controller import ContinuityDetail, Controller, OperationPage, Worker
from session_migrate.tui.widgets import Literal, plain

_STATUS_LABELS = {
    "no_op": "No change",
    "delta_candidate": "Delta candidate",
    "conflict": "Conflict",
    "lineage_broken": "Lineage broken",
    "untracked": "Untracked",
    "needs_reconcile": "Needs reconciliation",
}

_REASON_LABELS = {
    "profile_context_required": "Choose current profiles before assessing this operation.",
    "identity_unproven": "Current profiles or native identities do not match this operation.",
    "profile_binding_unproven": "Current profile bindings could not be proved.",
    "identity_changed": "A native namespace or root witness changed.",
    "endpoint_unavailable": "A current endpoint is unavailable in the selected catalog/profile.",
    "native_boundary_unproven": "Source advanced; a safe native boundary is unproven.",
    "coverage_unverified": "Raw coverage or content fidelity is unverified.",
    "ledger_missing": "No durable ledger is available at the application's state location.",
    "operation_not_found": "This operation ID is absent from the durable ledger.",
}


def detail_text(detail: ContinuityDetail) -> str:
    """Render only durable metadata and actual C1 assessment fields."""
    inspection = detail.inspection
    operation = inspection.operation
    if operation is None:
        return _REASON_LABELS.get(inspection.reason, inspection.reason)
    record = operation.record
    source = record.source.identity
    lines = [
        f"Operation ID: {record.operation_id}",
        f"Durable state: {operation.durable_state.value}",
        f"Recovered state: {operation.state.value}",
        f"Last recorded native state: {operation.durable_native_state.value}",
        f"Recovered native state: {operation.native_state.value}",
        f"Reserved bytes: {operation.reserved_bytes}",
        f"Source: {source.namespace.format if source else 'portable / identity untracked'}",
        f"Source root: {source.namespace.canonical_root if source else '-'}",
        f"Source native ID: {source.namespace.native_id if source else '-'}",
        f"Target: {record.target_namespace.format}",
        f"Target root: {record.target_namespace.canonical_root}",
        f"Target native ID: {record.target_namespace.native_id}",
        f"Input SHA256: {record.target_input_sha256}",
        f"Source SHA256: {record.source.artifact_sha256}",
        f"Canonical / projection policy: {record.versions.canonical_algorithm} / "
        f"{record.versions.projection_policy}",
        f"Source / target client: {record.versions.source_client or '-'} / "
        f"{record.versions.target_client or '-'}",
    ]
    if type(operation.receipt) is CompletionReceipt:
        lines.append(f"Historical receipt: completed / {operation.receipt.continuity_reason}")
    elif type(operation.receipt) is ReconcileReceipt:
        lines.append(f"Recovery receipt: {operation.receipt.phase} / {operation.receipt.reason}")
    references = operation.receipt.references if operation.receipt else record.references
    for reference in references:
        lines.append(
            f"Evidence {reference.kind}: {reference.stage}/{reference.availability} | "
            f"{reference.path or '-'} | {reference.sha256 or '-'}"
        )
    if detail.profile_context:
        lines.append(f"Current profile context: {detail.profile_context}")
    if detail.context_reason:
        lines.append("Continuity: unavailable (current profile context required)")
        lines.append(_REASON_LABELS.get(detail.context_reason, detail.context_reason))
    assessment = detail.assessment
    if type(assessment) is ContinuityAssessmentV2:
        lines.extend(
            [
                "Assessment schema: 2 — derived USER handoff, historical transcript omitted.",
                f"Document SHA256: {assessment.derivation.input.document_sha256}",
                f"Derived event SHA256: {assessment.derivation.input.event_sha256}",
                "Return parent: "
                + (
                    assessment.return_link.parent_operation_id if assessment.return_link else "none"
                ),
            ]
        )
        assessment = assessment.baseline
    if assessment is not None:
        status = assessment.status.value
        lines.extend(
            [
                f"Continuity: {_STATUS_LABELS[status]} ({status})",
                f"Reason: {assessment.reason}",
                _REASON_LABELS.get(assessment.reason, ""),
                f"Source / target coverage: {_value(assessment.source_coverage)} / "
                f"{_value(assessment.target_coverage)}",
                f"Source / target fidelity: {_value(assessment.source_fidelity)} / "
                f"{_value(assessment.target_fidelity)}",
                f"Source / target structure matches: {assessment.source_structure_matches} / "
                f"{assessment.target_structure_matches}",
                f"Independent relation matches: {assessment.relation_matches}",
                f"Source / target current profile binding: "
                f"{_value(assessment.source_profile_binding_kind)} / "
                f"{_value(assessment.target_profile_binding_kind)}",
                "Source current profile SHA256: "
                f"{assessment.source_current_profile_fingerprint or '-'}",
                "Target current profile SHA256: "
                f"{assessment.target_current_profile_fingerprint or '-'}",
                f"Blockers: {', '.join(assessment.blockers) or '-'}",
                "Native read effects: "
                f"{', '.join(assessment.native_read_effects) or 'none reported'}",
            ]
        )
    lines.extend(
        [
            "Assessment applies to its observed read window.",
            "Native boundary proof: unproven.",
            "Update available: false.",
        ]
    )
    return "\n".join(line for line in lines if line)


def _value(value):
    return value.value if value is not None else "unobserved"


class ContinuityScreen(Screen):
    """A single cancellable reader, independent of the transfer publication job."""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        ("ctrl+r", "refresh", "Refresh"),
    ]

    def __init__(self, controller: Controller, *, profiles_file: Path | None = None):
        super().__init__()
        self.controller = controller
        self.profiles_file = profiles_file
        self.reader = Worker()
        self.pending: str | None = None
        self.generation = 0
        self.page_offset = 0
        self.page: OperationPage | None = None
        self.detail: ContinuityDetail | None = None
        self.cancel_event: threading.Event | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Literal("Operations — readonly", id="operations-status")
        yield Literal(
            "Inspect reads durable metadata. Assess reads current endpoints. "
            "Native exports may use private scratch and affect cache/WAL.",
            id="operations-read-effects",
        )
        with Horizontal(id="operations-navigation"):
            yield Button("Refresh", id="operations-refresh")
            yield Button("Previous", id="operations-previous", disabled=True)
            yield Button("Next", id="operations-next", disabled=True)
            yield Button("Back", id="operations-back")
        yield Input(
            str(self.profiles_file) if self.profiles_file else "",
            placeholder="Current private profiles file (optional, absolute)",
            id="operations-profiles",
        )
        with Horizontal(id="operations-actions"):
            yield Input(placeholder="Explicit operation ID", id="operation-id")
            yield Button("Inspect", id="operations-inspect")
            yield Button("Assess", id="operations-assess")
            yield Button("Cancel read", id="operations-cancel", disabled=True)
        with VerticalScroll(id="operations-content"):
            yield OptionList(id="operations-list")
            yield Literal("Choose an operation ID and inspect its metadata.", id="operation-detail")
        yield Footer()

    def on_mount(self):
        self.set_interval(0.05, self.poll_reads)
        self.action_refresh()

    def status(self, value):
        self.query_one("#operations-status", Literal).set_text(value)

    def launch(self, kind, work):
        if self.pending is not None:
            self.status("A read is running. Wait or cancel the read.")
            return
        cancel = threading.Event()
        if not self.reader.submit(self.generation, kind, lambda: work(cancel)):
            self.status("The previous read is still finishing.")
            return
        self.pending, self.cancel_event = kind, cancel
        self.query_one("#operations-cancel", Button).disabled = False
        self.status(
            "Reading durable metadata…"
            if kind == "list"
            else "Inspecting durable metadata…"
            if kind == "inspect"
            else "Assessing current endpoints…"
        )

    def action_refresh(self):
        offset = self.page_offset
        self.launch(
            "list",
            lambda cancel: self.controller.list_operations(offset=offset, cancel_event=cancel),
        )

    def selected_operation_id(self):
        value = self.query_one("#operation-id", Input).value.strip()
        try:
            ContinuitySelector(operation_id=value)
        except SessionMigrateError:
            self.status("Enter a valid operation ID from the durable ledger.")
            return None
        return value

    def action_inspect(self):
        operation_id = self.selected_operation_id()
        if operation_id is not None:
            self.launch(
                "inspect",
                lambda cancel: self.controller.inspect_operation(operation_id, cancel_event=cancel),
            )

    def action_assess(self):
        operation_id = self.selected_operation_id()
        if operation_id is None:
            return
        selected = self.query_one("#operations-profiles", Input).value.strip()
        profiles_file = Path(selected) if selected else None
        if profiles_file is not None and not profiles_file.is_absolute():
            self.status("Use an absolute current profiles file path.")
            return
        self.launch(
            "assess",
            lambda cancel: self.controller.assess_operation(
                operation_id, profiles_file=profiles_file, cancel_event=cancel
            ),
        )

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        identifier = event.button.id
        if identifier == "operations-back":
            self.action_back()
        elif identifier == "operations-cancel":
            self.cancel_read()
        elif identifier == "operations-refresh":
            self.action_refresh()
        elif identifier == "operations-inspect":
            self.action_inspect()
        elif identifier == "operations-assess":
            self.action_assess()
        elif identifier in {"operations-previous", "operations-next"} and not self.pending:
            self.page_offset = max(
                0, self.page_offset + (50 if identifier == "operations-next" else -50)
            )
            self.action_refresh()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        event.stop()
        if event.option_list.id == "operations-list" and event.option.id:
            self.query_one("#operation-id", Input).value = event.option.id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        event.stop()
        if event.option_list.id == "operations-list":
            self.action_inspect()

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        if event.input.id == "operation-id":
            self.action_inspect()

    def on_input_changed(self, event: Input.Changed):
        # Read selectors belong to this screen. They must never invalidate the
        # transfer draft through MigrationApp's option-edit event handler.
        event.stop()
        if event.input.id in {"operation-id", "operations-profiles"}:
            if self.pending in {"inspect", "assess"}:
                self.cancel_read()
            if self.detail is not None:
                self.detail = None
                self.query_one("#operation-detail", Literal).set_text(
                    "Selection changed. Inspect or assess the chosen operation again."
                )

    def poll_reads(self):
        result = self.reader.poll()
        if result is None or result.generation != self.generation:
            return
        self.pending = None
        self.cancel_event = None
        self.query_one("#operations-cancel", Button).disabled = True
        if result.error:
            self.detail = None
            self.query_one("#operation-detail", Literal).set_text(
                "Read unavailable. Check the durable ledger, current profiles and catalog."
            )
            self.status("Read failed. No operation was changed.")
        elif result.kind == "list":
            self.detail = None
            self.page = result.value
            options = self.query_one("#operations-list", OptionList)
            options.clear_options()
            for operation in self.page.operations:
                record = operation.record
                options.add_option(
                    Option(
                        plain(
                            f"{record.operation_id} | {operation.durable_state.value} | "
                            f"{operation.native_state.value} | {record.target_namespace.format}: "
                            f"{record.target_namespace.native_id}"
                        ),
                        id=record.operation_id,
                    )
                )
            self.query_one("#operations-previous", Button).disabled = self.page_offset == 0
            self.query_one("#operations-next", Button).disabled = not self.page.has_next
            self.status(
                f"{self.page.total_count} durable operations; page {self.page_offset // 50 + 1}."
                if self.page.operations
                else "No durable operations found."
            )
            if not self.page.operations:
                self.query_one("#operation-detail", Literal).set_text(
                    _REASON_LABELS.get(self.page.reason, "The durable ledger has no operations.")
                )
            else:
                self.query_one("#operation-detail", Literal).set_text(
                    "Choose an operation ID and inspect its metadata."
                )
        else:
            self.detail = result.value
            self.query_one("#operation-detail", Literal).set_text(detail_text(self.detail))
            self.status(
                "Assessment ready."
                if self.detail.assessment
                else "Assessment blocked. Current profile context is unavailable."
                if self.detail.context_reason
                else "Metadata ready."
            )

    def cancel_read(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.generation += 1
        self.pending = None
        self.cancel_event = None
        self.query_one("#operations-cancel", Button).disabled = True
        self.status("Read cancelled. An active native read may finish in the background.")

    def action_back(self):
        self.cancel_read()
        self.dismiss()

    def on_unmount(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.generation += 1
