"""Optional headless keyboard flows; synthetic stores, no external services."""

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("textual", reason="optional tui extra is not installed")

from textual.widgets import Button, Input, OptionList, Select

from session_migrate import conversion, operations, profiles
from session_migrate.catalog import Catalog
from session_migrate.conversion import load_session
from session_migrate.errors import NativeImportPartialError, SourceReadLimitError
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import CancellationToken, OperationRequest
from session_migrate.profiles import Profile, list_profile_sessions
from session_migrate.tui.app import MigrationApp
from session_migrate.tui.controller import Controller, Worker
from session_migrate.tui.widgets import Literal

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
CODEX_FIXTURE = Path(__file__).parent / "fixtures/codex-0.144.4/basic.jsonl"


def setup(tmp_path, format=AgentFormat.CLAUDE):
    root = tmp_path / "source" / format.value
    if format == AgentFormat.CLAUDE:
        session = load_session(FIXTURE, format)
        path = root / "projects/-synthetic" / f"{session.session_id}.jsonl"
        data = FIXTURE.read_bytes()
    elif format == AgentFormat.CODEX:
        session = load_session(CODEX_FIXTURE, format)
        path = root / "sessions/2026/08/18" / f"rollout-synthetic-{session.session_id}.jsonl"
        data = CODEX_FIXTURE.read_bytes()
    else:
        session = replace(
            load_session(FIXTURE, AgentFormat.CLAUDE),
            source_format=format,
            session_id="ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaaa",
        )
        path = root / "opencode.db"
        data = b""
    path.parent.mkdir(parents=True)
    if format == AgentFormat.OPENCODE:
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT,
                  slug TEXT, directory TEXT, title TEXT, version TEXT, time_created INTEGER,
                  time_updated INTEGER, time_archived INTEGER);
                CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
            """)
            db.execute(
                "INSERT INTO session VALUES (?, 'global', NULL, 'synthetic', ?, "
                "'Synthetic OpenCode', '2.0.23', 1787054400000, 1787054404000, NULL)",
                (session.session_id, str(tmp_path)),
            )
    else:
        path.write_bytes(data)
    catalog_path = tmp_path / "catalog.sqlite3"
    with Catalog(catalog_path) as catalog:
        registered = catalog.add_root(format, root)
        catalog.refresh(include_auto=False)
    source = Profile(
        f"root-{registered.id}",
        "Synthetic source",
        format,
        root,
        environ={"HOME": str(tmp_path / "source-home")},
    )
    controller = Controller(
        catalog_path, environ={"HOME": str(tmp_path / "base-home"), "PATH": "/usr/bin:/bin"}
    )
    return controller, source, path


async def idle(app, pilot):
    for _ in range(150):
        await pilot.pause(0.02)
        if app.pending is None and app.preview_request is None:
            return
    raise AssertionError("UI task did not finish")


async def activate(app, pilot, id):
    widget = app.query_one(f"#{id}", Button)
    widget.focus()
    await pilot.press("enter")
    await idle(app, pilot)


def fill(app, id, value):
    app.query_one(f"#{id}", Input).value = str(value)


def test_keyboard_claude_to_codex_preview_review_confirm_resize(tmp_path):
    controller, source, source_path = setup(tmp_path)
    before = source_path.read_bytes()

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 55)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            await pilot.pause()
            await activate(app, pilot, "list")
            assert app.stage == "sessions"
            assert str(source.root) in str(app.query_one("#source-summary", Literal).render())
            assert len(app.entries) == 1
            app.query_one("#session-list", OptionList).highlighted = 0
            await activate(app, pilot, "preview")
            assert app.snapshot is not None
            assert app.stage == "preview-step"
            await activate(app, pilot, "target")
            fill(app, "target-root", tmp_path / "codex-target")
            fill(app, "target-home", tmp_path / "target-home")
            fill(app, "target-name", "Target")
            fill(app, "cwd", tmp_path / "project")
            await pilot.pause()
            await activate(app, pilot, "prepare")
            assert app.draft is not None
            reserved = app.draft.result.session_id
            assert not app.query_one("#confirm", Button).disabled
            await pilot.resize_terminal(75, 22)
            await pilot.pause()
            await activate(app, pilot, "confirm")
            assert app.stage == "result"
            receipt_text = str(app.query_one("#receipt", Literal).render())
            assert reserved in receipt_text
            assert "Resume manually" in receipt_text
            assert app.completed

    asyncio.run(flow())
    assert source_path.read_bytes() == before
    paths = list((tmp_path / "codex-target").glob("sessions/**/*.jsonl"))
    assert len(paths) == 1
    assert load_session(paths[0], AgentFormat.CODEX).cwd == tmp_path / "project"


def test_literal_unicode_markup_osc_and_tools_folded(tmp_path):
    controller, source, path = setup(tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row.get("type") == "user" and isinstance(row.get("message"), dict):
            row["message"]["content"] = "[bold]é λ[/bold]\x1b]52;c;SYNTHETIC\x07"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with Catalog(controller.catalog) as catalog:
        catalog.refresh(include_auto=False)
        ref = list_profile_sessions(catalog, source)[0].ref
    request = controller.start_preview(source, ref)
    result = request.wait(3)
    texts = "\n".join(event.text for event in result.snapshot.events)
    assert "[bold]é λ[/bold]" in texts
    assert "SYNTHETIC" not in texts
    assert "\x1b" not in texts
    assert any(event.collapsed for event in result.snapshot.events)


def test_slow_preview_keeps_ui_responsive_and_cancelled_selection_stays_absent(
    tmp_path, monkeypatch
):
    controller, source, _ = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = profiles.read_session

    def slow(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr("session_migrate.tui.controller.read_session", slow)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(100, 45)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            await pilot.pause()
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            app.query_one("#preview", Button).focus()
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.01)
                if entered.is_set():
                    break
            assert entered.is_set()
            await pilot.resize_terminal(70, 24)
            await pilot.press("escape")
            assert app.stage == "sessions"
            release.set()
            await pilot.pause(0.1)
            assert app.snapshot is None

    try:
        asyncio.run(flow())
    finally:
        release.set()


def test_source_byte_limit_is_checked_before_loader(tmp_path, monkeypatch):
    controller, source, path = setup(tmp_path)
    controller.source_bytes = 10
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
    monkeypatch.setattr(
        "session_migrate.tui.controller.read_session",
        lambda *a, **kw: pytest.fail("parse before budget"),
    )
    result = controller.start_preview(source, ref).wait(3)
    assert result.snapshot is None
    assert result.status.value == "error"


def test_cache_revision_changes_after_append_and_profile_options_change(tmp_path):
    controller, source, path = setup(tmp_path)
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
    first = controller.start_preview(source, ref).wait(3).snapshot
    path.write_bytes(path.read_bytes() + b"\n")
    second = controller.start_preview(source, ref).wait(3).snapshot
    assert first.source_revision != second.source_revision
    assert controller.preview.cache.entries == 1
    other = replace(source, environ={"HOME": str(tmp_path / "different-home")})
    assert controller.revision(source, ref) != controller.revision(
        other, replace(ref, profile_fingerprint=None)
    )


def test_draft_reserved_identity_changed_source_and_double_commit_no_retry(tmp_path):
    controller, source, path = setup(tmp_path)
    target = Profile("target", "Target", "codex", tmp_path / "target")
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
    draft = controller.prepare(
        source, ref, target, OperationRequest("transfer", cwd=tmp_path), CancellationToken()
    )
    result = controller.commit(draft, CancellationToken(), lambda event: None)
    assert result.session_id == draft.result.session_id
    with pytest.raises(Exception, match="already consumed"):
        controller.commit(draft, CancellationToken(), lambda event: None)
    draft = controller.prepare(
        source, ref, target, OperationRequest("transfer", cwd=tmp_path), CancellationToken()
    )
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(Exception, match="Source changed"):
        controller.commit(draft, CancellationToken(), lambda event: None)


def test_worker_bounds_jobs_and_never_exposes_native_exception(tmp_path):
    worker = Worker()
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(2)
        raise RuntimeError("BODY MUST NOT APPEAR")

    assert worker.submit(1, "read", slow)
    assert entered.wait(1)
    assert not worker.submit(2, "read", lambda: None)
    release.set()
    for _ in range(100):
        result = worker.poll()
        if result is not None:
            break
        time.sleep(0.01)
    assert "BODY" not in result.error

    def partial():
        raise NativeImportPartialError(
            "BODY MUST NOT APPEAR", target_format=TargetFormat.CODEX, session_id="synthetic-target"
        )

    assert worker.submit(3, "commit", partial)
    for _ in range(100):
        result = worker.poll()
        if result is not None:
            break
        time.sleep(0.01)
    assert result.reconcile_id == "synthetic-target"
    assert "Do not repeat" in result.error


@pytest.mark.parametrize(
    "source_format,target_format", [("codex", "opencode"), ("opencode", "claude")]
)
def test_keyboard_opencode_routes_use_profile_environment_and_reserved_id(
    tmp_path, monkeypatch, source_format, target_format
):
    controller, source, _ = setup(tmp_path, AgentFormat(source_format))
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    calls = []
    if source_format == "opencode":
        loaded = replace(
            load_session(FIXTURE, AgentFormat.CLAUDE),
            source_format=AgentFormat.OPENCODE,
            session_id="ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaaa",
        )

        def export(native_id, *, source_cli=None, environ=None, **kwargs):
            assert native_id == loaded.session_id
            assert source_cli == cli
            assert Path(environ["XDG_DATA_HOME"]) / "opencode" == source.root
            calls.append("export")
            return loaded

        monkeypatch.setattr(operations, "load_opencode_session", export)
        monkeypatch.setattr(profiles, "load_opencode_session", export)
    else:
        monkeypatch.setattr(operations, "detect_opencode_version", lambda *args, **kwargs: "2.0.23")

        def install(artifact, *, manifest_path, target_cli, environ, dry_run):
            assert target_cli == cli
            assert environ["HOME"] == str(tmp_path / "target-home")
            assert environ["XDG_CONFIG_HOME"] == str(tmp_path / "target-home/.config")
            assert Path(environ["XDG_DATA_HOME"]) / "opencode" == tmp_path / "target-data/opencode"
            calls.append((artifact.session_id, dry_run))
            return manifest_path

        monkeypatch.setattr(operations, "install_opencode_artifact", install)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 55)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            await pilot.pause()
            if source_format == "opencode":
                fill(app, "source-cli", cli)
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            await activate(app, pilot, "preview")
            assert app.snapshot
            await activate(app, pilot, "target")
            app.query_one("#target-format", Select).value = target_format
            fill(
                app,
                "target-root",
                tmp_path
                / ("target-data/opencode" if target_format == "opencode" else "target-claude"),
            )
            fill(app, "target-home", tmp_path / "target-home")
            fill(app, "cwd", tmp_path / "project")
            if target_format == "opencode":
                fill(app, "target-cli", cli)
                fill(app, "schema", "2.0.23")
                fill(app, "provider", "synthetic")
                fill(app, "model", "offline")
            await pilot.pause()
            await activate(app, pilot, "prepare")
            assert app.draft
            reserved = app.draft.result.session_id
            await activate(app, pilot, "confirm")
            assert app.completed
            assert reserved in str(app.query_one("#receipt", Literal).render())
            if target_format == "opencode":
                assert len(calls) == 3
                assert all(id == reserved for id, dry_run in calls)
                assert sum(not dry_run for id, dry_run in calls) == 1
            else:
                assert len(calls) == 7  # selected export witnesses plus core prepare/commit

    asyncio.run(flow())


def test_native_export_byte_budget_rejects_before_parse(tmp_path, monkeypatch):
    cli = tmp_path / "synthetic-cli"
    cli.write_text("synthetic")
    cli.chmod(0o700)
    monkeypatch.setattr(conversion, "_resolve_opencode_cli", lambda *args: cli)
    monkeypatch.setattr(conversion, "_opencode_version", lambda *args: "2.0.23")
    monkeypatch.setattr(
        conversion,
        "_invoke_opencode_export",
        lambda cli, id, path, env: path.write_bytes(b"x" * 20),
    )
    monkeypatch.setattr(
        conversion, "load_session", lambda *args: pytest.fail("must not parse oversized export")
    )
    with pytest.raises(SourceReadLimitError):
        conversion.load_opencode_session(
            "ses_synthetic",
            source_cli=cli,
            environ={"HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
            max_source_bytes=10,
        )


def test_ten_thousand_metadata_rows_paginate_without_loading_bodies(tmp_path, monkeypatch):
    controller, source, _ = setup(tmp_path)
    with Catalog(controller.catalog) as catalog:
        root = catalog.roots()[0]
        catalog._connection.executemany(
            "INSERT INTO sessions(catalog_id,root_id,relative_path,canonical_path,format,"
            "session_id,kind,lifecycle,status,device,inode,bytes,modified_ns,indexed_at) "
            "VALUES (?,?,?,?,'claude',?,'main','active','candidate',0,0,0,0,'synthetic')",
            [
                (
                    f"synthetic-{index}",
                    root.id,
                    f"synthetic-{index}.jsonl",
                    str(source.root / f"synthetic-{index}.jsonl"),
                    f"synthetic-{index}",
                )
                for index in range(10_000)
            ],
        )
        catalog._connection.commit()
    monkeypatch.setattr(
        "session_migrate.tui.controller.read_session",
        lambda *args, **kwargs: pytest.fail("metadata listing loaded a body"),
    )
    start = time.perf_counter()
    first = controller.list(source, "", 0)
    second = controller.list(source, "", 50)
    elapsed = time.perf_counter() - start
    assert len(first) == len(second) == 50
    assert not {item.entry.catalog_id for item in first} & {
        item.entry.catalog_id for item in second
    }
    print(f"metadata benchmark: 10,000 synthetic rows, two pages of 50: {elapsed:.6f}s")


def test_edit_invalidates_prepared_draft_and_keeps_form_values(tmp_path):
    controller, source, _ = setup(tmp_path)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 60)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            await pilot.pause()
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            await activate(app, pilot, "preview")
            await activate(app, pilot, "target")
            fill(app, "target-root", tmp_path / "target")
            fill(app, "cwd", tmp_path / "project")
            await pilot.pause()
            await activate(app, pilot, "prepare")
            assert app.draft is not None
            await pilot.press("escape")
            assert app.stage == "target-step"
            assert app.value("cwd") == str(tmp_path / "project")
            fill(app, "cwd", tmp_path / "other-project")
            await pilot.pause()
            assert app.draft is None
            assert app.query_one("#confirm", Button).disabled

    asyncio.run(flow())


@pytest.mark.parametrize("publication_started", [False, True])
def test_ui_escape_respects_publication_boundary_and_double_confirm_never_retries(
    tmp_path, monkeypatch, publication_started
):
    controller, source, _ = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def commit(draft, token, progress):
        token.claim()
        if publication_started:
            token.begin_publication()
        calls.append(draft.request.session_id)
        entered.set()
        release.wait(3)
        token.check()
        return replace(draft.result, dry_run=False)

    monkeypatch.setattr(controller, "commit", commit)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 60)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            await pilot.pause()
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            await activate(app, pilot, "preview")
            await activate(app, pilot, "target")
            fill(app, "target-root", tmp_path / "target")
            fill(app, "cwd", tmp_path / "project")
            await pilot.pause()
            await activate(app, pilot, "prepare")
            app.query_one("#confirm", Button).focus()
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.01)
                if entered.is_set():
                    break
            assert entered.is_set()
            token = app.token
            app.on_button_pressed(Button.Pressed(app.query_one("#confirm", Button)))
            assert app.token is token
            assert len(calls) == 1
            await pilot.press("escape")
            assert app.stage == ("review" if publication_started else "target-step")
            assert app.pending == "commit"
            if publication_started:
                await pilot.press("ctrl+q")
                assert app.is_running
                assert app.stage == "review"
            release.set()
            await idle(app, pilot)
            assert app.completed == publication_started
            assert len(calls) == 1

    try:
        asyncio.run(flow())
    finally:
        release.set()


def test_partial_import_result_shows_identity_and_blocks_retry_navigation(tmp_path, monkeypatch):
    controller, source, _ = setup(tmp_path)

    def partial(draft, token, progress):
        raise NativeImportPartialError(
            "SYNTHETIC PRIVATE BODY",
            target_format=TargetFormat.CODEX,
            session_id=draft.result.session_id,
        )

    monkeypatch.setattr(controller, "commit", partial)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 60)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            await pilot.pause()
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            await activate(app, pilot, "preview")
            await activate(app, pilot, "target")
            fill(app, "target-root", tmp_path / "target")
            fill(app, "cwd", tmp_path / "project")
            await pilot.pause()
            await activate(app, pilot, "prepare")
            expected = app.draft.result.session_id
            await activate(app, pilot, "confirm")
            assert app.stage == "result"
            assert app.needs_reconcile
            text = str(app.query_one("#receipt", Literal).render())
            assert expected in text
            assert "SYNTHETIC PRIVATE BODY" not in text
            assert app.query_one("#another", Button).disabled
            await pilot.press("escape")
            assert app.stage == "result"

    asyncio.run(flow())


def test_profile_byte_budget_is_enforced_before_physical_parse(tmp_path, monkeypatch):
    controller, source, _ = setup(tmp_path)
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
        monkeypatch.setattr(
            profiles, "load_session", lambda *args: pytest.fail("parsed before budget")
        )
        with pytest.raises(SourceReadLimitError):
            profiles.read_session(catalog, source, ref, max_source_bytes=10)


def test_manual_resume_quotes_project_and_exact_cli():
    from session_migrate.tui.app import resume_instruction

    value = resume_instruction(
        "opencode",
        "ses_synthetic",
        "/synthetic/project with spaces",
        cli=Path("/synthetic/custom cli"),
    )
    assert (
        value
        == "cd -- '/synthetic/project with spaces'\n'/synthetic/custom cli' --session ses_synthetic"
    )


def test_resume_instruction_uses_exact_target_root_and_own_home():
    from session_migrate.tui.app import resume_instruction

    selected = Profile(
        "custom",
        "Custom",
        "codex",
        Path("/synthetic/custom codex"),
        environ={"HOME": "/synthetic/target home", "XDG_CONFIG_HOME": "/synthetic/target config"},
    )
    text = resume_instruction("codex", "synthetic-id", "/synthetic/project", profile=selected)
    assert "'CODEX_HOME=/synthetic/custom codex'" in text
    assert "'HOME=/synthetic/target home'" in text
    assert "'XDG_CONFIG_HOME=/synthetic/target config'" in text
    assert "env -u XDG_RUNTIME_DIR" in text


def test_opencode_global_database_size_does_not_block_selected_export(tmp_path, monkeypatch):
    controller, source, path = setup(tmp_path, AgentFormat.OPENCODE)
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    source = replace(source, cli=cli)
    controller.source_bytes = 10  # DB is much larger; only the selected export is budgeted.
    selected = replace(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        source_format=AgentFormat.OPENCODE,
        session_id="ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaaa",
    )

    def export(id, *, source_cli, environ, max_source_bytes):
        assert max_source_bytes == 10
        return selected

    monkeypatch.setattr(profiles, "load_opencode_session", export)
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
    assert path.stat().st_size > controller.source_bytes
    preview = controller.start_preview(source, ref).wait(3)
    assert preview.snapshot is not None


@pytest.mark.parametrize("drift_at", ["before_confirm", "after_preflight"])
def test_native_authority_witness_rejects_changed_export_before_publication(
    tmp_path, monkeypatch, drift_at
):
    controller, source, _ = setup(tmp_path, AgentFormat.OPENCODE)
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    source = replace(source, cli=cli)
    state = {
        "session": replace(
            load_session(FIXTURE, AgentFormat.CLAUDE),
            source_format=AgentFormat.OPENCODE,
            cli_version="2.0.23",
            session_id="ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaaa",
        )
    }

    def export(*args, **kwargs):
        return state["session"]

    monkeypatch.setattr(profiles, "load_opencode_session", export)
    monkeypatch.setattr(operations, "load_opencode_session", export)
    target = Profile("target", "Target", "claude", tmp_path / "target")
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
    draft = controller.prepare(
        source, ref, target, OperationRequest("transfer", cwd=tmp_path), CancellationToken()
    )

    def drift():
        state["session"] = replace(state["session"], source_sha256="a" * 64)

    if drift_at == "before_confirm":
        drift()
    else:
        original = operations.ensure_target_paths_available

        def preflight(*paths):
            original(*paths)
            drift()

        monkeypatch.setattr(operations, "ensure_target_paths_available", preflight)
    token = CancellationToken()
    with pytest.raises(Exception, match="Source changed"):
        controller.commit(draft, token, lambda event: None)
    assert not token.publishing
    assert not target.root.exists()


def test_prepared_project_cwd_is_canonical_and_option_drift_is_rejected(tmp_path):
    controller, source, _ = setup(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    target = Profile("target", "Target", "codex", tmp_path / "target")
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
    draft = controller.prepare(
        source, ref, target, OperationRequest("transfer", cwd=alias), CancellationToken()
    )
    assert draft.request.cwd == project
    changed = replace(draft, request=replace(draft.request, cwd=tmp_path / "other"))
    with pytest.raises(Exception, match="Prepared options changed"):
        controller.commit(changed, CancellationToken(), lambda event: None)
    assert not target.root.exists()


def test_edit_during_profile_binding_discards_old_worker_selection(tmp_path, monkeypatch):
    controller, source, _ = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = controller.bind_source

    def slow(selected):
        entered.set()
        release.wait(3)
        return original(selected)

    monkeypatch.setattr(controller, "bind_source", slow)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 55)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            await pilot.pause()
            app.query_one("#list", Button).focus()
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.01)
                if entered.is_set():
                    break
            assert entered.is_set()
            fill(app, "source-root", tmp_path / "different-root")
            await pilot.pause()
            release.set()
            await idle(app, pilot)
            assert app.source is None
            assert app.stage == "source"

    try:
        asyncio.run(flow())
    finally:
        release.set()


@pytest.mark.parametrize("format", [AgentFormat.GROK, AgentFormat.OPENHANDS])
def test_directory_source_preview_budget_membership_and_provenance(tmp_path, monkeypatch, format):
    from session_migrate.conversion import (
        ConversionOptions,
        convert_session,
        install_grok_artifact,
        install_openhands_artifact,
    )

    root = tmp_path / "source-root"
    source_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    project = tmp_path / "project"
    project.mkdir()
    artifact = convert_session(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        ConversionOptions(target_format=TargetFormat(format), session_id=source_id, cwd=project),
    )
    installer = install_grok_artifact if format == AgentFormat.GROK else install_openhands_artifact
    native_path, _ = installer(artifact, target_home=root)
    selected = Profile("directory", "Directory", format, root)
    catalog_path = tmp_path / "catalog.sqlite3"
    with Catalog(catalog_path) as catalog:
        catalog.add_root(format, root)
        assert catalog.refresh(include_auto=False).root_errors == 0
        ref = list_profile_sessions(catalog, selected)[0].ref
        original = profiles.read_session(catalog, selected, ref)
        limited = profiles.read_session(catalog, selected, ref, max_source_bytes=8 * 1024 * 1024)
        assert limited.source_path == original.source_path
        assert limited.source_sha256 == original.source_sha256
        assert [event.provenance for event in limited.events] == [
            event.provenance for event in original.events
        ]
    controller = Controller(catalog_path, environ={"HOME": str(tmp_path / "home")})
    preview = controller.start_preview(selected, ref).wait(3)
    assert preview.snapshot is not None
    before = controller.revision(selected, ref)
    if format == AgentFormat.GROK:
        updates = original.source_path / "updates.jsonl"
        updates.write_bytes(updates.read_bytes() + b"\n")
    else:
        events = original.source_path / "events"
        existing = next(events.glob("event-*.json"))
        added = events / "event-99999-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.json"
        added.write_bytes(existing.read_bytes())
    assert controller.revision(selected, ref) != before
    monkeypatch.setattr(
        profiles, "load_session", lambda *args: pytest.fail("oversized directory reached parser")
    )
    with Catalog(catalog_path) as catalog, pytest.raises(SourceReadLimitError):
        profiles.read_session(catalog, selected, ref, max_source_bytes=10)


def test_bounded_copy_growth_after_witness_is_rejected_before_parser(tmp_path, monkeypatch):
    controller, selected, path = setup(tmp_path)
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, selected)[0].ref
    budget = path.stat().st_size + 10
    original_paths = profiles.source_read_paths
    calls = []

    def enumerate_then_grow(*args, **kwargs):
        result = original_paths(*args, **kwargs)
        calls.append(1)
        # read_session preflight enumerates once, then immutable-copy acquisition enumerates.
        if len(calls) == 2:
            path.write_bytes(path.read_bytes() + b" " * 100)
        return result

    monkeypatch.setattr(profiles, "source_read_paths", enumerate_then_grow)
    monkeypatch.setattr(
        profiles, "load_session", lambda *args: pytest.fail("growth reached parser")
    )
    with Catalog(controller.catalog) as catalog, pytest.raises(SourceReadLimitError):
        profiles.read_session(catalog, selected, ref, max_source_bytes=budget)


@pytest.mark.parametrize("phase", ["prepare", "commit"])
def test_core_operation_native_loader_receives_interactive_budget(tmp_path, monkeypatch, phase):
    controller, selected, _ = setup(tmp_path, AgentFormat.OPENCODE)
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    selected = replace(selected, cli=cli)
    native = replace(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        source_format=AgentFormat.OPENCODE,
        session_id="ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaaa",
    )
    limits = []

    def export(*args, max_source_bytes=None, **kwargs):
        limits.append(max_source_bytes)
        return native

    monkeypatch.setattr(profiles, "load_opencode_session", export)
    monkeypatch.setattr(operations, "load_opencode_session", export)
    target = Profile("target", "Target", "claude", tmp_path / "target")
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, selected)[0].ref
    draft = controller.prepare(
        selected, ref, target, OperationRequest("transfer", cwd=tmp_path), CancellationToken()
    )
    if phase == "commit":
        limits.clear()
        controller.commit(draft, CancellationToken(), lambda event: None)
    assert limits
    assert all(limit == controller.source_bytes for limit in limits)


def test_selected_directory_symlinks_and_enumeration_budget_are_rejected(tmp_path):
    from session_migrate.profiles import source_read_paths

    root = tmp_path / "root"
    events = root / "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa" / "events"
    events.mkdir(parents=True)
    for index in range(3):
        (events / f"event-{index:05d}-synthetic.json").write_text("{}")
    with pytest.raises(SourceReadLimitError, match="file-count"):
        source_read_paths(AgentFormat.OPENHANDS, events.parent, root, max_entries=2)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (events / "event-99999-synthetic.json").symlink_to(outside)
    with pytest.raises(Exception, match="symlinks|outside"):
        source_read_paths(AgentFormat.OPENHANDS, events.parent, root)


def test_initial_native_preview_timeout_cancel_has_handle_and_does_not_cache(tmp_path, monkeypatch):
    controller, source, _ = setup(tmp_path, AgentFormat.OPENCODE)
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    source = replace(source, cli=cli)
    with Catalog(controller.catalog) as catalog:
        ref = list_profile_sessions(catalog, source)[0].ref
    entered, release = threading.Event(), threading.Event()
    native = replace(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        source_format=AgentFormat.OPENCODE,
        session_id=ref.native_id,
    )

    def slow_export(*args, **kwargs):
        entered.set()
        release.wait(3)
        return native

    monkeypatch.setattr(profiles, "load_opencode_session", slow_export)
    try:
        request = controller.start_preview(source, ref)
        assert entered.wait(1)
        assert request.poll().status.value == "loading"
        request.cancel()
        release.set()
        time.sleep(0.05)
        assert request.poll().status.value == "cancelled"
        assert controller.preview.cache.entries == 0
    finally:
        release.set()


def test_native_initial_authority_handle_exists_before_escape_and_late_cache_is_discarded(
    tmp_path, monkeypatch
):
    controller, selected, _ = setup(tmp_path, AgentFormat.OPENCODE)
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o700)
    entered, release = threading.Event(), threading.Event()
    native = replace(
        load_session(FIXTURE, AgentFormat.CLAUDE),
        source_format=AgentFormat.OPENCODE,
        session_id="ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaaa",
    )

    def slow(*args, **kwargs):
        entered.set()
        release.wait(3)
        return native

    monkeypatch.setattr(profiles, "load_opencode_session", slow)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 55)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = selected.id
            await pilot.pause()
            fill(app, "source-cli", cli)
            await pilot.pause()
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            app.query_one("#preview", Button).focus()
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.01)
                if entered.is_set():
                    break
            assert entered.is_set()
            assert app.preview_request is not None
            assert app.pending is None  # initial native export belongs to the preview service slot
            await pilot.press("escape")
            assert app.stage == "sessions"
            assert app.preview_request is None
            release.set()
            await pilot.pause(0.1)
            assert app.snapshot is None
            assert controller.preview.cache.entries == 0

    try:
        asyncio.run(flow())
    finally:
        release.set()


@pytest.mark.parametrize("entrypoint", ["wire", "state", "directory"])
@pytest.mark.parametrize("homedir", ["relative", "absolute", "invalid"])
def test_kimi_bounded_layout_original_identity_and_state_witness(tmp_path, entrypoint, homedir):
    import shutil

    from session_migrate.errors import SessionMigrateError
    from session_migrate.profiles import load_bounded_source, source_read_paths

    corpus = Path(__file__).parent / "native_corpus/v1/sources/kimi/0.38.0/portable-rich/native"
    original_dir = next(corpus.iterdir())
    root = tmp_path / "kimi"
    directory = root / "sessions/project" / original_dir.name
    shutil.copytree(original_dir, directory)
    state_path = directory / "state.json"
    wire_path = directory / "agents/main/wire.jsonl"
    state = json.loads(state_path.read_text())
    if homedir == "absolute":
        state["agents"]["main"]["homedir"] = str(directory / "agents/main")
    elif homedir == "invalid":
        state["agents"]["main"]["homedir"] = str(tmp_path / "outside")
    state_path.write_text(json.dumps(state))
    entry = {"wire": wire_path, "state": state_path, "directory": directory}[entrypoint]
    snapshots = {path: path.read_bytes() for path in (state_path, wire_path)}
    paths = source_read_paths(AgentFormat.KIMI, entry, root)
    assert state_path in paths and wire_path in paths
    if homedir == "invalid":
        with pytest.raises(SessionMigrateError, match="main-agent path"):
            load_session(entry, AgentFormat.KIMI)
        with pytest.raises(SessionMigrateError, match="main-agent path"):
            load_bounded_source(
                entry, AgentFormat.KIMI, root=root, max_source_bytes=8 * 1024 * 1024
            )
    else:
        expected = load_session(entry, AgentFormat.KIMI)
        actual = load_bounded_source(
            entry, AgentFormat.KIMI, root=root, max_source_bytes=8 * 1024 * 1024
        )
        assert actual.source_path == expected.source_path == wire_path
        assert actual.source_sha256 == expected.source_sha256
        assert len(actual.events) == len(expected.events)
        assert [event.provenance for event in actual.events] == [
            event.provenance for event in expected.events
        ]
        catalog_path = tmp_path / "catalog.sqlite3"
        selected = Profile("kimi", "Kimi", "kimi", root)
        with Catalog(catalog_path) as catalog:
            catalog.add_root(AgentFormat.KIMI, root)
            assert catalog.refresh(include_auto=False).root_errors == 0
            ref = list_profile_sessions(catalog, selected)[0].ref
        controller = Controller(catalog_path, environ={"HOME": str(tmp_path / "home")})
        before = controller.revision(selected, ref)
        state_path.write_bytes(state_path.read_bytes() + b" ")
        assert controller.revision(selected, ref) != before
        state_path.write_bytes(snapshots[state_path])
    assert all(path.read_bytes() == value for path, value in snapshots.items())


@pytest.mark.parametrize("entrypoint", ["file", "directory"])
def test_vibe_bounded_layout_is_neutral(tmp_path, entrypoint):
    import shutil

    from session_migrate.profiles import load_bounded_source, source_read_paths

    root = tmp_path / "vibe"
    directory = root / "session"
    shutil.copytree(Path(__file__).parent / "fixtures/vibe-2.24.3/basic", directory)
    path = directory if entrypoint == "directory" else directory / "messages.jsonl"
    original = {child: child.read_bytes() for child in directory.iterdir()}
    expected = load_session(path, AgentFormat.VIBE)
    actual = load_bounded_source(
        path, AgentFormat.VIBE, root=root, max_source_bytes=8 * 1024 * 1024
    )
    assert actual.source_path == expected.source_path
    assert actual.source_sha256 == expected.source_sha256
    assert len(actual.events) == len(expected.events)
    assert [event.provenance for event in actual.events] == [
        event.provenance for event in expected.events
    ]
    assert directory / "meta.json" in source_read_paths(AgentFormat.VIBE, path, root)
    assert all(child.read_bytes() == value for child, value in original.items())


@pytest.mark.parametrize("traversal", ["inside", "outside"])
def test_bounded_snapshot_parent_traversal_is_rejected_without_residue(tmp_path, traversal):
    from session_migrate.errors import SessionMigrateError
    from session_migrate.profiles import load_bounded_source, source_read_paths

    root = tmp_path / "source-root"
    root.mkdir()
    path = root / "source.jsonl"
    path.write_bytes(FIXTURE.read_bytes())
    private_parent = tmp_path / "snapshots"
    private_parent.mkdir()
    selected = root / ".." / (root.name if traversal == "inside" else "other") / path.name
    with pytest.raises(SessionMigrateError, match="traversal"):
        source_read_paths(AgentFormat.CLAUDE, selected, root)
    with pytest.raises(SessionMigrateError, match="traversal"):
        load_bounded_source(
            selected,
            AgentFormat.CLAUDE,
            root=root,
            max_source_bytes=8 * 1024 * 1024,
            environ={"TMPDIR": str(private_parent)},
        )
    assert list(private_parent.iterdir()) == []
    assert path.read_bytes() == FIXTURE.read_bytes()
    assert not (tmp_path / "other").exists()


@pytest.mark.parametrize(
    "format,relative",
    [
        (AgentFormat.CLAUDE, "fixtures/claude-2.1.209/basic.jsonl"),
        (AgentFormat.CODEX, "fixtures/codex-0.144.4/basic.jsonl"),
        (AgentFormat.CODEX, "fixtures/codex-0.153.4/paginated.jsonl"),
        (AgentFormat.PI, "fixtures/pi-0.80.6/basic.jsonl"),
        (AgentFormat.OMP, "fixtures/omp-18.0.5/basic.jsonl"),
        (AgentFormat.COPILOT, "fixtures/copilot-source-1.0.70/copilot-source-native-events.jsonl"),
        (
            AgentFormat.MUSE,
            "native_corpus/v1/sources/muse/0.2.1/portable-rich/native/session.jsonl",
        ),
        (
            AgentFormat.QWEN,
            "native_corpus/v1/sources/qwen/0.22.1/portable-rich/native/44c47f3e-ccfe-4fb7-aa06-0217dc950456.jsonl",
        ),
        (AgentFormat.OPENCODE, "fixtures/opencode-1.17.20/basic.json"),
    ],
)
def test_bounded_single_file_layout_matrix_is_neutral(tmp_path, format, relative):
    from session_migrate.profiles import load_bounded_source

    original = Path(__file__).parent / relative
    root = tmp_path / "source-root"
    root.mkdir()
    source = root / original.name
    source.write_bytes(original.read_bytes())
    before = source.read_bytes()
    expected = load_session(source, format)
    actual = load_bounded_source(source, format, root=root, max_source_bytes=8 * 1024 * 1024)
    assert actual.source_path == expected.source_path
    assert actual.source_sha256 == expected.source_sha256
    assert len(actual.events) == len(expected.events)
    assert [event.provenance for event in actual.events] == [
        event.provenance for event in expected.events
    ]
    assert source.read_bytes() == before


def test_bounded_regular_file_swapped_to_fifo_does_not_block_before_fstat(tmp_path, monkeypatch):
    import os

    from session_migrate.profiles import load_bounded_source

    root = tmp_path / "source-root"
    root.mkdir()
    source = root / "source.jsonl"
    source.write_bytes(FIXTURE.read_bytes())
    original = profiles._open_source_file

    def swap_then_open(path):
        path.unlink()
        os.mkfifo(path, mode=0o600)
        return original(path)

    monkeypatch.setattr(profiles, "_open_source_file", swap_then_open)
    outcomes = []

    def run():
        try:
            load_bounded_source(
                source, AgentFormat.CLAUDE, root=root, max_source_bytes=8 * 1024 * 1024
            )
        except Exception as error:
            outcomes.append(type(error))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive(), "FIFO open blocked before regular-file validation"
    assert outcomes == [SourceReadLimitError]
