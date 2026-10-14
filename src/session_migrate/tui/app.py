"""Keyboard-driven optional interface backed by the shared operation core."""

from __future__ import annotations

from pathlib import Path
from shlex import join, quote

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    OptionList,
    Select,
    TextArea,
)
from textual.widgets.option_list import Option

from session_migrate.capabilities import capabilities_for, list_capabilities, title_capability_for
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat
from session_migrate.operations import CancellationToken, OperationRequest
from session_migrate.preview import PreviewRequest, PreviewSnapshot, PreviewStatus, literal_text
from session_migrate.profiles import ENVIRONMENT_KEYS, Profile, ProfileSession, ProfileStore
from session_migrate.titles import TitlePolicy, validate_title
from session_migrate.tui.continuity_screen import ContinuityScreen
from session_migrate.tui.controller import Controller, Draft, Worker
from session_migrate.tui.widgets import Literal, plain


class MigrationApp(App):
    """One draft and one operation at a time; no automatic import retries."""

    TITLE = "Session Migrate"
    CSS_PATH = "terminal.tcss"
    BINDINGS = [
        ("escape", "back", "Back / cancel"),
        ("ctrl+q", "quit_safe", "Quit"),
        ("ctrl+o", "operations", "Operations"),
        ("ctrl+h", "handoff", "Handoff / return-new"),
    ]

    def __init__(self, controller: Controller, *, profiles_file: Path | None = None):
        super().__init__()
        self.controller = controller
        self.profiles_file = profiles_file
        self.worker = Worker()
        self.generation = 0
        self.pending: str | None = None
        self.saved_profiles: list[Profile] = []
        self.source: Profile | None = None
        self.entries: list[ProfileSession] = []
        self.selected: ProfileSession | None = None
        self.offset = 0
        self.draft: Draft | None = None
        self.token: CancellationToken | None = None
        self.preview_request: PreviewRequest | None = None
        self.snapshot: PreviewSnapshot | None = None
        self.preview_offset = 0
        self.preview_history = [0]
        self.expand_tools = False
        self._form_loading = True
        self.completed = False
        self.needs_reconcile = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="app-navigation"):
            yield Button("Operations", id="operations")
            yield Button("Handoff / return-new", id="handoff")
        yield Literal("Loading profiles…", id="status")
        with ContentSwitcher(initial="source", id="steps"):
            with VerticalScroll(id="source"):
                yield Literal("1. Source profile — choose a registered native root")
                yield Select([], prompt="Known profile/root", id="source-profile")
                yield Select(
                    [(format.value, format.value) for format in AgentFormat],
                    value="claude",
                    allow_blank=False,
                    id="source-format",
                )
                yield Literal("Native root")
                yield Input(placeholder="Native root (absolute)", id="source-root")
                yield Input(placeholder="Profile name", id="source-name")
                yield Literal("HOME for this profile")
                yield Input(placeholder="HOME (absolute; independent of project)", id="source-home")
                yield Literal("Native CLI executable")
                yield Input(placeholder="Native CLI (OpenCode/Kilo source)", id="source-cli")
                yield Input(placeholder="XDG_CONFIG_HOME (optional)", id="source-config")
                yield Input(placeholder="XDG_DATA_HOME (optional)", id="source-data")
                yield Input(placeholder="Private profiles JSON file (optional)", id="profile-file")
                with Horizontal():
                    yield Button("Read profiles", id="read-profiles")
                    yield Button("Save new private file", id="save-profiles")
                    yield Button("List sessions", id="list", variant="primary")
            with VerticalScroll(id="sessions"):
                yield Literal("2. Sessions — metadata search, no conversation bodies")
                yield Literal("", id="source-summary")
                yield Literal(
                    "Start-date filters use native session metadata. "
                    "Order reflects file/database updates, not observed activity."
                )
                yield Input(placeholder="Title / native ID / project", id="query")
                with Horizontal():
                    yield Select(
                        [
                            ("All availability", ""),
                            ("Candidate", "candidate"),
                            ("Validated", "validated"),
                            ("Missing", "missing"),
                            ("Unsupported", "unsupported"),
                            ("Corrupt", "corrupt"),
                        ],
                        value="",
                        allow_blank=False,
                        id="availability",
                    )
                    yield Select(
                        [("All lifecycle", ""), ("Active", "active"), ("Archived", "archived")],
                        value="",
                        allow_blank=False,
                        id="lifecycle",
                    )
                yield Input(placeholder="Since (RFC-3339, optional)", id="since")
                yield Input(placeholder="Until (RFC-3339, optional)", id="until")
                with Horizontal():
                    yield Button("Search", id="search")
                    yield Button("Previous", id="previous")
                    yield Button("Next", id="next")
                yield OptionList(id="session-list")
                yield Button("Open selected preview", id="preview", variant="primary")
            with VerticalScroll(id="preview-step"):
                yield Literal("3. Preview — literal text; media shown only as metadata")
                yield TextArea(read_only=True, show_cursor=False, id="preview-text")
                with Horizontal():
                    yield Button("Previous page", id="preview-previous")
                    yield Button("Next page", id="preview-next")
                    yield Button("Expand / fold tools", id="tools")
                    yield Button("Choose destination", id="target", variant="primary")
            with VerticalScroll(id="target-step"):
                yield Literal("4. Destination — creates a new session")
                yield Select([], prompt="Known target profile (optional)", id="target-profile")
                yield Select(
                    [(format.value, format.value) for format in AgentFormat]
                    + [("OpenChamber (OpenCode backend)", "openchamber")],
                    value="codex",
                    allow_blank=False,
                    id="target-format",
                )
                yield Literal("Native root")
                yield Input(placeholder="Native root (absolute)", id="target-root")
                yield Input(placeholder="Profile name", id="target-name")
                yield Literal("HOME for this profile")
                yield Input(placeholder="HOME (absolute; independent of project)", id="target-home")
                yield Literal("Native CLI executable")
                yield Input(placeholder="Native CLI when required", id="target-cli")
                yield Input(placeholder="XDG_CONFIG_HOME (optional)", id="target-config")
                yield Input(placeholder="XDG_DATA_HOME (optional)", id="target-data")
                yield Input(
                    placeholder="Local OpenCode backend URL (OpenChamber only; http://127.0.0.1:port)",
                    id="target-server",
                )
                yield Input(placeholder="Project CWD (absolute)", id="cwd")
                yield Input(
                    placeholder="Target CLI version (OpenCode selects transfer schema)", id="schema"
                )
                yield Input(placeholder="Model provider when required", id="provider")
                yield Input(placeholder="Model when required", id="model")
                yield Input(
                    placeholder="Destination title (optional, preserved exactly)", id="target-title"
                )
                yield Checkbox(
                    "Use an explicit local alias (does not rename native picker)",
                    id="local-title-alias",
                )
                yield Checkbox("Require verified installed native title", id="require-native-title")
                yield Literal("", id="capability")
                yield Button("Prepare transfer", id="prepare", variant="primary")
            with VerticalScroll(id="review"):
                yield Literal("5. Review — confirm the new session and destination")
                yield Literal("", id="proposal")
                yield Button("Confirm transfer", id="confirm", variant="success", disabled=True)
            with VerticalScroll(id="result"):
                yield Literal("", id="receipt")
                yield Button("Choose another session", id="another")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.05, self.poll_jobs)
        self._form_loading = False
        self.launch("profiles", lambda: self.controller.profiles(self.profiles_file))

    @property
    def stage(self) -> str:
        return self.query_one("#steps", ContentSwitcher).current or "source"

    def status(self, value: str) -> None:
        self.query_one("#status", Literal).set_text(value)

    def show_source_summary(self) -> None:
        if self.source is not None:
            self.query_one("#source-summary", Literal).set_text(
                f"Profile: {self.source.name} ({self.source.format})\n"
                f"Root: {self.source.root}\n"
                f"HOME: {self.source.environ.get('HOME', '(runtime default)')}\n"
                f"CLI: {self.source.cli or '(adapter default)'}"
            )

    def show(self, stage: str) -> None:
        self.query_one("#steps", ContentSwitcher).current = stage

    def value(self, id: str) -> str:
        return self.query_one(f"#{id}", Input).value.strip()

    def launch(self, kind, work):
        if self.pending:
            self.status("A task is still running. Wait or cancel before starting another.")
            return False
        if not self.worker.submit(self.generation, kind, work):
            self.status("A reader is still finishing. Please wait.")
            return False
        self.pending = kind
        self.status("Working…")
        self.query_one("#confirm", Button).disabled = True
        return True

    def invalidate(self) -> bool:
        cancelled = self.token.request_cancel() if self.token else True
        if self.pending == "commit" and not cancelled:
            self.status(
                "Cancellation is closed. Waiting for the operation result."
                if self.token.publishing
                else "Operation finishing. Waiting for its result."
            )
            return False
        self.controller.discard()
        self.generation += 1
        self.draft = None
        self.query_one("#confirm", Button).disabled = True
        return True

    def profile_form(self, direction: str) -> dict:
        choice = self.query_one(f"#{direction}-profile", Select).value
        previous = next((profile for profile in self.saved_profiles if profile.id == choice), None)
        overrides = dict(previous.environ) if previous else {}
        for suffix, key in (
            ("home", "HOME"),
            ("config", "XDG_CONFIG_HOME"),
            ("data", "XDG_DATA_HOME"),
        ):
            value = self.value(f"{direction}-{suffix}")
            if value:
                overrides[key] = value
            else:
                overrides.pop(key, None)
        selected_format = self.query_one(f"#{direction}-format", Select).value
        result = {
            "id": previous.id if previous else f"{direction}-ephemeral",
            "name": self.value(f"{direction}-name") or direction,
            "format": "opencode" if selected_format == "openchamber" else selected_format,
            "root": Path(self.value(f"{direction}-root")),
            "cli": Path(self.value(f"{direction}-cli")) if self.value(f"{direction}-cli") else None,
            "environ": overrides,
        }
        if direction == "target" and selected_format == "openchamber":
            result["client"] = "openchamber"
            result["server"] = self.value("target-server") or None
        return result

    def on_select_changed(self, event: Select.Changed):
        if self._form_loading:
            return
        if (
            event.select.id in {"source-profile", "target-profile"}
            and event.value is not Select.NULL
        ):
            profile = next((item for item in self.saved_profiles if item.id == event.value), None)
            if profile:
                self.fill_profile(event.select.id.split("-")[0], profile)
        if event.select.id == "target-format":
            self.show_capabilities()
        self.on_edit()

    def fill_profile(self, direction: str, profile: Profile):
        self._form_loading = True
        self.query_one(f"#{direction}-format", Select).value = (
            "openchamber"
            if direction == "target" and profile.client == "openchamber"
            else profile.format.value
        )
        if direction == "target":
            self.query_one("#target-server", Input).value = profile.server or ""
        for suffix, value in (
            ("root", profile.root),
            ("name", profile.name),
            ("cli", profile.cli or ""),
            ("home", profile.environ.get("HOME", self.controller.environ.get("HOME", ""))),
            ("config", profile.environ.get("XDG_CONFIG_HOME", "")),
            ("data", profile.environ.get("XDG_DATA_HOME", "")),
        ):
            self.query_one(f"#{direction}-{suffix}", Input).value = str(value)
        self._form_loading = False

    def on_input_changed(self, event: Input.Changed):
        if not self._form_loading:
            self.on_edit()
            if event.input.id in {"target-title", "schema", "target-server"}:
                self.show_capabilities()

    def on_checkbox_changed(self, event: Checkbox.Changed):
        if not self._form_loading:
            self.on_edit()
            self.show_capabilities()

    def target_title(self) -> str | None:
        raw = self.query_one("#target-title", Input).value
        return validate_title(raw) if raw != "" else None

    def on_edit(self):
        # Only option edits invalidate a draft; typing filters has no publication side effects.
        if self.draft is not None or self.pending in {
            "bind",
            "sessions",
            "preview",
            "prepare",
            "commit",
        }:
            return self.invalidate()

    def show_capabilities(self):
        format = self.query_one("#target-format", Select).value
        if format is Select.NULL:
            return
        openchamber = format == "openchamber"
        if openchamber:
            format = "opencode"
        info = capabilities_for(format)
        schemas = ", ".join(
            item.schema for item in list_capabilities(format) if item.target_supported
        )
        alias = self.query_one("#local-title-alias", Checkbox).value
        required = self.query_one("#require-native-title", Checkbox).value
        capability = title_capability_for(
            format,
            cli_version=self.value("schema") or None,
            policy="local_alias" if alias else "native",
        )
        problem = None
        try:
            title = self.target_title()
            if openchamber:
                from session_migrate.openchamber import validated_server

                validated_server(self.value("target-server"))
            if alias and required:
                problem = "Local alias and required native title are incompatible."
            elif (alias or required) and title is None:
                problem = "Choose a destination title for the selected title policy."
            elif (
                title is not None
                and not alias
                and capability.status == "unsupported"
                and (format != "opencode" or self.value("schema"))
            ):
                problem = "Native naming unsupported. Choose local alias explicitly."
        except SessionMigrateError as error:
            problem = str(error)
        self.query_one("#prepare", Button).disabled = problem is not None
        self.query_one("#capability", Literal).set_text(
            f"Supported output: {schemas}. Media: {info.media_scope}\n"
            f"Naming policy: {capability.status}. {capability.reason}\n"
            "Prepare checks the effective client/version. New session only.\n"
            f"{'OpenChamber uses the selected local OpenCode backend. ' if openchamber else ''}"
            f"{problem or ''}"
        )

    def search(self):
        if self.source is None:
            return
        source, query, offset = self.source, self.value("query"), self.offset
        lifecycle = self.query_one("#lifecycle", Select).value
        status = self.query_one("#availability", Select).value
        since, until = self.value("since"), self.value("until")
        self.launch(
            "sessions",
            lambda: self.controller.list(source, query, offset, lifecycle, status, since, until),
        )

    def open_preview(self):
        options = self.query_one("#session-list", OptionList)
        if options.highlighted is None or not self.entries:
            self.status("Choose an available session first.")
            return
        item = self.entries[options.highlighted]
        if not item.ref or item.entry.status in {
            "missing",
            "unsupported",
            "corrupt",
            "busy",
            "unreadable",
            "oversized",
        }:
            self.status(
                f"Session unavailable: {item.entry.status}; "
                f"{item.entry.reason or 'no native identity'}."
            )
            return
        self.selected = item
        self.preview_offset = 0
        self.preview_history = [0]
        self.snapshot = None
        self.show("preview-step")
        source, ref = self.source, item.ref
        self.preview_request = self.controller.start_preview(
            source, ref, expand_tools=self.expand_tools
        )
        self.status("Reading preview…")

    def render_preview(self):
        if self.snapshot is None:
            return
        page = self.snapshot.page(self.preview_offset)
        text = "\n\n".join(f"{item.role or item.kind}: {item.text}" for item in page.events)
        self.query_one("#preview-text", TextArea).load_text(text)
        self.query_one("#preview-next", Button).disabled = page.next_offset is None
        self.query_one("#preview-previous", Button).disabled = len(self.preview_history) == 1
        self.status(" ".join(page.notices) or "Preview ready.")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "handoff":
            self.action_handoff()
            return
        id = event.button.id
        if id == "operations":
            self.action_operations()
            return
        if self.pending:
            self.status("A task is running. Wait or use Escape before installation starts.")
            return
        try:
            if id == "list":
                selected = self.profile_form("source")
                self.launch("bind", lambda: self.controller.bind_source(Profile(**selected)))
            elif id == "read-profiles":
                path = Path(self.value("profile-file"))
                self.launch("profiles", lambda: self.controller.profiles(path))
            elif id == "save-profiles":
                path, selected = Path(self.value("profile-file")), self.profile_form("source")
                self.launch("save", lambda: ProfileStore(path).create([Profile(**selected)]))
            elif id in {"search", "previous", "next"}:
                if id == "search":
                    self.offset = 0
                else:
                    self.offset = max(0, self.offset + (50 if id == "next" else -50))
                self.search()
            elif id == "preview":
                self.open_preview()
            elif id == "preview-next" and self.snapshot:
                offset = self.snapshot.page(self.preview_offset).next_offset
                if offset is not None:
                    self.preview_offset = offset
                    self.preview_history.append(offset)
                    self.render_preview()
            elif id == "preview-previous" and len(self.preview_history) > 1:
                self.preview_history.pop()
                self.preview_offset = self.preview_history[-1]
                self.render_preview()
            elif id == "tools" and self.selected:
                self.expand_tools = not self.expand_tools
                self.open_preview()
            elif id == "target" and self.snapshot:
                self.show("target-step")
                self.show_capabilities()
            elif id == "prepare" and self.selected and self.source:
                if not self.value("cwd") or not Path(self.value("cwd")).is_absolute():
                    self.status("Choose an absolute project CWD before preparing.")
                    return
                if (
                    self.query_one("#local-title-alias", Checkbox).value
                    and self.query_one("#require-native-title", Checkbox).value
                ):
                    raise SessionMigrateError(
                        "Local alias and required native title are incompatible."
                    )
                target = self.profile_form("target")
                request = OperationRequest(
                    "transfer",
                    cwd=Path(self.value("cwd")),
                    target_cli_version=self.value("schema") or None,
                    model_provider=self.value("provider") or None,
                    model=self.value("model") or None,
                    target_title=self.target_title(),
                    title_policy=TitlePolicy.LOCAL_ALIAS
                    if self.query_one("#local-title-alias", Checkbox).value
                    else None,
                    require_native_title=self.query_one("#require-native-title", Checkbox).value,
                )
                self.token = CancellationToken()
                source, ref, token = self.source, self.selected.ref, self.token
                self.launch(
                    "prepare",
                    lambda: self.controller.prepare(source, ref, Profile(**target), request, token),
                )
            elif id == "confirm" and self.draft and not self.completed:
                draft = self.draft
                self.token = CancellationToken()
                token = self.token
                self.launch(
                    "commit",
                    lambda: self.controller.commit(
                        draft,
                        token,
                        lambda progress: self.call_from_thread(
                            self.status, progress_label(progress.stage.value)
                        ),
                    ),
                )
            elif id == "another" and not self.pending:
                self.draft = None
                self.completed = False
                self.show_source_summary()
                self.show("sessions")
                self.search()
        except SessionMigrateError as error:
            self.status(str(error))
        except Exception:
            self.status("Invalid profile or options. Use absolute paths and supported formats.")

    def poll_jobs(self):
        result = self.worker.poll()
        if result is not None:
            self.pending = None
            self.token = None
            if result.generation != self.generation:
                if result.kind == "prepare" and isinstance(result.value, Draft):
                    self.controller.discard(result.value)
                self.status("Selection changed; repeat the selected action.")
            elif result.error:
                failed_draft = self.draft
                self.draft = None
                self.status(result.error)
                if result.reconcile_id:
                    self.completed = True
                    self.needs_reconcile = True
                    self.query_one("#another", Button).disabled = True
                    self.show("result")
                    destination = failed_draft.target.root if failed_draft else "selected profile"
                    manifest = result.reconcile_manifest or (
                        failed_draft.result.manifest if failed_draft else "not available"
                    )
                    self.query_one("#receipt", Literal).set_text(
                        f"Needs attention. Target ID: {result.reconcile_id}\n"
                        f"Publication: {result.publication_kind or 'native import'}\n"
                        f"Stage: {result.reconcile_stage or 'verification'}\n"
                        f"Installation: {result.reconcile_native_state or 'acknowledged'}\n"
                        f"Destination: {result.reconcile_root or destination}\n"
                        f"Manifest: {manifest}\n"
                        f"Output: {result.reconcile_output or '-'}\n"
                        + "".join(
                            f"Evidence {reference.kind}: "
                            f"{reference.stage}/{reference.availability}\n"
                            for reference in result.reconcile_references
                        )
                        + "Do not repeat publication. Inspect the reserved target and evidence."
                    )
            elif result.kind == "profiles":
                self.saved_profiles = result.value
                for direction in ("source", "target"):
                    choices = []
                    for profile in result.value:
                        if direction == "source" and profile.client == "openchamber":
                            continue
                        label = (
                            "OpenChamber (OpenCode backend)"
                            if profile.client == "openchamber"
                            else profile.format.value
                        )
                        choices.append(
                            (plain(f"{profile.name} — {label} — {profile.root}"), profile.id)
                        )
                    self.query_one(f"#{direction}-profile", Select).set_options(choices)
                self.status(
                    "Choose a source profile/root."
                    if any(profile.client != "openchamber" for profile in result.value)
                    else "No registered roots. Register a root with the catalog CLI, "
                    "or read a private profiles file."
                )
            elif result.kind == "bind":
                self.source = result.value
                self.show_source_summary()
                self.selected = None
                self.snapshot = None
                self.draft = None
                self.offset = 0
                self.show("sessions")
                self.search()
            elif result.kind == "sessions":
                previous_id = self.selected.entry.catalog_id if self.selected else None
                self.entries = result.value
                options = self.query_one("#session-list", OptionList)
                options.clear_options()
                for item in self.entries:
                    options.add_option(
                        Option(
                            plain(
                                f"Native title: {item.entry.title or '(untitled)'} | "
                                f"Local alias: {getattr(item, 'local_alias', None) or '-'} | "
                                f"{getattr(item, 'alias_status', None) or ''}"
                                f"{item.entry.session_id or '(no ID)'} | "
                                f"{item.entry.lifecycle} | {item.entry.status}"
                            ),
                            id=item.entry.catalog_id,
                        )
                    )
                for index, item in enumerate(self.entries):
                    if item.entry.catalog_id == previous_id:
                        options.highlighted = index
                self.query_one("#previous", Button).disabled = self.offset == 0
                self.query_one("#next", Button).disabled = len(self.entries) < 50
                self.status(
                    f"{len(self.entries)} sessions; page {self.offset // 50 + 1}."
                    if self.entries
                    else "No matching sessions."
                )
            elif result.kind == "preview":
                self.preview_request = result.value
            elif result.kind == "prepare":
                self.draft = result.value
                self.show("review")
                draft = self.draft
                self.query_one("#proposal", Literal).set_text(review_text(draft))
                self.query_one("#confirm", Button).disabled = False
                self.status("Ready for confirmation. Back preserves your options.")
            elif result.kind == "commit":
                self.completed = True
                target_profile = self.draft.target if self.draft else None
                target_cli = target_profile.cli if target_profile else None
                self.draft = None
                self.show("result")
                envelope = result.value.to_dict()
                receipt = envelope.get("legacy_result", envelope)
                resume = resume_instruction(
                    receipt["target_format"],
                    receipt["session_id"],
                    receipt["cwd"],
                    cli=target_cli,
                    profile=target_profile,
                )
                self.query_one("#receipt", Literal).set_text(
                    f"Completed.\n"
                    f"Target ID: {receipt['session_id']}\n"
                    f"Output: {receipt['output']}\n"
                    f"Manifest: {receipt['manifest']}\n"
                    f"{title_text(envelope.get('title'), receipt=True)}\n"
                    f"Title receipt: {envelope.get('title_receipt') or '-'}\n"
                    f"Resume manually:\n"
                    f"{resume}"
                )
                self.status("Transfer completed.")
            elif result.kind == "save":
                self.status("New private profile file saved. Existing files are never overwritten.")
        if self.preview_request:
            preview = self.preview_request.poll()
            if preview.status != PreviewStatus.LOADING:
                self.preview_request = None
                if preview.status == PreviewStatus.READY:
                    self.snapshot = preview.snapshot
                    self.render_preview()
                else:
                    self.status(preview.error or "Preview cancelled.")

    def action_back(self):
        if self.needs_reconcile:
            self.status(
                "Verification needs attention. Inspect the published target manually; "
                "do not repeat publication."
            )
            return
        if self.pending and not self.invalidate():
            return
        if not self.pending and self.stage == "review" and self.draft:
            self.invalidate()
        if not self.pending and self.stage == "result":
            self.completed = False
        if self.preview_request:
            self.preview_request.cancel()
            self.preview_request = None
        previous = {
            "sessions": "source",
            "preview-step": "sessions",
            "target-step": "preview-step",
            "review": "target-step",
            "result": "sessions",
        }
        self.show(previous.get(self.stage, "source"))

    def action_operations(self):
        if isinstance(self.screen, ContinuityScreen):
            return
        if self.pending or self.preview_request:
            self.status("Wait for the current task before opening durable operations.")
            return
        self.push_screen(ContinuityScreen(self.controller, profiles_file=self.profiles_file))

    def action_handoff(self):
        from session_migrate.tui.handoff_screen import HandoffScreen

        if isinstance(self.screen, HandoffScreen):
            return
        if self.pending or self.preview_request:
            self.status("Wait for the current task before opening handoff review.")
            return
        self.push_screen(HandoffScreen(self.controller, profiles_file=self.profiles_file))

    def action_quit(self):
        # Also route inherited framework quit bindings through the publication guard.
        self.action_quit_safe()

    def action_quit_safe(self):
        from session_migrate.tui.handoff_screen import HandoffScreen

        if isinstance(self.screen, HandoffScreen):
            self.screen.action_quit_safe()
            return
        if self.pending == "commit":
            self.action_back()
            return
        if self.token:
            self.token.request_cancel()
        if self.preview_request:
            self.preview_request.cancel()
        self.controller.discard()
        self.exit()


def title_text(metadata: dict | None, *, receipt: bool = False) -> str:
    if metadata is None:
        return "Title override: none; native naming follows the adapter."
    plan = metadata["title_plan"] if receipt else metadata
    return (
        f"Requested title: {plan['requested']}\n"
        f"Native effective (planned): {plan.get('native_effective') or 'unchanged/unknown'}\n"
        f"Native observed: {metadata.get('native_observed') or 'not verified'}\n"
        f"Local alias (app only): {plan.get('local_alias') or '-'}\n"
        f"Persistence: {metadata.get('persistence', plan['intended_persistence'])}\n"
        f"Verification: {metadata.get('verification_state', 'pending')}\n"
        f"Native proof: {plan.get('proof_version') or '-'} / {plan.get('proof_id') or '-'}\n"
        f"Source title disposition: {plan['source_title_disposition']}"
    )


def review_text(draft: Draft) -> str:
    metadata = draft.proposal.to_dict()
    source, target, options = metadata["source"], metadata["target"], metadata["options"]
    loss = "\n".join(f"{reason}: {count}" for reason, count in sorted(metadata["losses"].items()))
    warnings = ", ".join(item.get("code", "warning") for item in metadata["warnings"])
    effects = ", ".join(metadata["effects"]) or "No native probe effects reported."
    return (
        f"Source: {draft.source.name} ({source['format']})\n"
        f"Source root: {source['root']}\nSource native ID: {source['native_id']}\n"
        f"Source title (native): {source.get('title') or '(untitled)'}\n"
        f"Source counts: {source['events']} / {source['records']} records\n"
        f"Source read limit: {source['read_limit_bytes']} / {source['read_limit_scope']}\n"
        f"Source environment: {options['source_environment']}\n"
        f"Destination: {draft.target.name} ({target['format']})\n"
        f"Root: {target['root']}\nCLI: {target['cli'] or '(adapter default)'}\n"
        f"Target environment: {options['target_environment']}\n"
        f"Project CWD: {target['cwd']}\nReserved target ID: {target['native_id']}\n"
        f"Target version: {target['cli_version']} ({target['cli_version_source']})\n"
        f"Schema: {target['schema']}\n"
        f"Model/provider: {target['model']}/{target['model_provider']}\n"
        f"Artifact SHA256: {target['sha256']}\n{title_text(metadata['title'])}\n"
        f"Reported losses:\n{loss or 'None reported.'}\nWarnings: {warnings or 'None reported.'}\n"
        f"Probe effects: {effects}\nNew session output: {metadata['legacy_result']['output']}\n"
        f"Manifest: {metadata['legacy_result']['manifest']}\n"
        f"Title receipt: {metadata['title_receipt'] or '-'}\n"
        "Confirm publishes this exact prepared artifact. Resume remains manual."
    )


def resume_instruction(
    format: str,
    session_id: str,
    cwd: str,
    *,
    cli: Path | None = None,
    profile: Profile | None = None,
) -> str:
    # Only non-secret profile path overrides/root selectors enter these instructions.
    # This text is never executed, and no source configuration/authentication is copied.
    values = {}
    if profile is not None:
        runtime = profile.environment({})
        values = {key: value for key, value in runtime.items() if key in ENVIRONMENT_KEYS}
        if format == "codex":
            values["CODEX_HOME"] = str(profile.root)
        elif format == "claude":
            values["CLAUDE_CONFIG_DIR"] = str(profile.root)
        elif format in {"opencode", "kilo"}:
            values["XDG_DATA_HOME"] = str(profile.root.parent)
    commands = {
        "claude": ["claude", "--resume", session_id],
        "codex": ["codex", "resume", session_id],
        "opencode": [str(cli or "opencode"), "--session", session_id],
    }
    command = commands.get(format)
    if command is None:
        root = f"Native root: {profile.root}\n" if profile else ""
        settings = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        return literal_text(
            f"Open project {cwd}.\n{root}{settings}\n"
            f"Select {session_id} in the {format} client session picker."
        )
    prefix = []
    if values:
        prefix = ["env"]
        if profile and "HOME" in profile.environ and "XDG_RUNTIME_DIR" not in profile.environ:
            prefix += ["-u", "XDG_RUNTIME_DIR"]
        prefix += [f"{key}={value}" for key, value in sorted(values.items())]
    return literal_text(f"cd -- {quote(cwd)}\n{join([*prefix, *command])}")


def progress_label(stage: str) -> str:
    return {
        "loading": "Reading session…",
        "converting": "Converting history…",
        "preflight": "Checking destination…",
        "recording_intent": "Recording operation intent…",
        "installing": "Installing new session…",
        "verifying": "Verifying native identity/title and local metadata…",
        "completed": "Finishing result…",
        "cancelled": "Cancelled before installation.",
        "needs_reconcile": "Verification needs attention.",
        "failed": "Action failed.",
    }.get(stage, "Working…")
