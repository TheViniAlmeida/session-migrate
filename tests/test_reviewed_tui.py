"""Reviewed TUI opt-in precedes bounded previews and never implies consent."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_claude_reviewed_reader import reviewed_rows
from test_tui import activate, fill, idle, setup

pytest.importorskip("textual")

from textual.widgets import Button, Checkbox, OptionList, Select, TextArea

from session_migrate import operations
from session_migrate.catalog import Catalog
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, TargetFormat
from session_migrate.operations import CancellationToken, OperationKind, OperationRequest
from session_migrate.preview import PreviewKey
from session_migrate.profiles import Profile, list_profile_sessions
from session_migrate.tui.app import MigrationApp
from session_migrate.tui.controller import Controller, SourceChangedError, fingerprint
from session_migrate.tui.widgets import Literal


def reviewed_source(tmp_path):
    controller, source, path = setup(tmp_path)
    native_id = path.stem
    rows = reviewed_rows()
    for row in rows:
        row["sessionId"] = native_id
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with Catalog(controller.catalog) as catalog:
        catalog.refresh(include_auto=False)
        ref = list_profile_sessions(catalog, source)[0].ref
    return controller, source, path, ref


def reviewed_request(tmp_path):
    return OperationRequest(
        "transfer",
        source_format=AgentFormat.CLAUDE,
        target_format=TargetFormat.OPENCODE,
        target_cli_version="2.2.5",
        cwd=tmp_path,
        reviewed_portability=True,
    )


def prepare_as_private_conversion(monkeypatch, path, tmp_path, controller):
    requests = []

    def prepare(catalog, source, ref, target, request, **kwargs):
        requests.append((request, kwargs))
        return operations.prepare_operation(
            replace(
                request,
                command=OperationKind.CONVERT,
                path=path,
                output=tmp_path / "synthetic-target.json",
            ),
            environ=controller.environ,
        )

    monkeypatch.setattr("session_migrate.tui.controller.prepare_profile_operation", prepare)
    return requests


def test_mode_precedes_preview_and_never_uses_strict_loader(tmp_path, monkeypatch):
    controller, source, path, ref = reviewed_source(tmp_path)
    original = path.read_bytes()
    strict = controller.start_preview(source, ref).wait(5)
    assert strict.snapshot is None
    calls = []
    original_capture = __import__(
        "session_migrate.tui.controller", fromlist=["capture_readable_source"]
    ).capture_readable_source

    def capture(context, **kwargs):
        calls.append((context, kwargs))
        return original_capture(context, **kwargs)

    monkeypatch.setattr("session_migrate.tui.controller.capture_readable_source", capture)
    monkeypatch.setattr(
        "session_migrate.tui.controller.read_session",
        lambda *a, **k: pytest.fail("strict loader used"),
    )
    result = controller.start_preview(source, ref, reviewed_portability=True).wait(5)
    assert result.snapshot is not None
    assert calls[0][1] == {"root": source.root, "byte_limit": 64 * 1024 * 1024}
    assert calls[0][0].source_format is AgentFormat.CLAUDE
    assert path.read_bytes() == original


def test_preview_cache_and_revision_bind_mode_and_relevant_budget(tmp_path, monkeypatch):
    controller, source, path, ref = reviewed_source(tmp_path)
    keys = []
    request = controller.preview.request_authoritative

    def record(key, *args, **kwargs):
        keys.append(key)
        return request(key, *args, **kwargs)

    monkeypatch.setattr(controller.preview, "request_authoritative", record)
    controller.start_preview(source, ref).wait(5)
    first = controller.start_preview(source, ref, reviewed_portability=True).wait(5).snapshot
    assert keys[0] != keys[1] and keys[1].reviewed_portability
    controller.source_bytes = 32 * 1024 * 1024
    second = controller.start_preview(source, ref, reviewed_portability=True).wait(5).snapshot
    assert first.source_revision != second.source_revision
    assert (
        PreviewKey(source.format, source.id, str(source.root), ref.native_id).reviewed_portability
        is False
    )


def test_explicit_lower_budget_is_preserved_before_capture(tmp_path, monkeypatch):
    controller, source, path, ref = reviewed_source(tmp_path)
    bounded = Controller(controller.catalog, environ=controller.environ, source_bytes=10)
    assert bounded.source_limit() == bounded.source_limit(reviewed_portability=True) == 10
    monkeypatch.setattr(
        "session_migrate.tui.controller.capture_readable_source",
        lambda *a, **kw: pytest.fail("capture before budget guard"),
    )
    assert bounded.start_preview(source, ref, reviewed_portability=True).wait(5).snapshot is None


def test_reviewed_mode_outside_claude_or_non_bool_never_reads(tmp_path, monkeypatch):
    controller, source, path, ref = reviewed_source(tmp_path)
    monkeypatch.setattr(
        controller, "revision", lambda *a, **kw: pytest.fail("unsupported source read")
    )
    with pytest.raises(SessionMigrateError, match="Claude"):
        controller.start_preview(
            replace(source, format=AgentFormat.CODEX), ref, reviewed_portability=True
        )
    for value in (None, 0, 1, "true"):
        with pytest.raises(SessionMigrateError, match="boolean"):
            controller.start_preview(source, ref, reviewed_portability=value)


def test_reviewed_capture_checks_session_identity_and_source_revision(tmp_path, monkeypatch):
    controller, source, path, ref = reviewed_source(tmp_path)
    before = controller.revision(source, ref, reviewed_portability=True)
    path.write_bytes(
        path.read_bytes().replace(ref.native_id.encode(), b"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    )
    with pytest.raises(SourceChangedError):
        controller._read(source, ref, before, reviewed_portability=True)
    revision = controller.revision(source, ref, reviewed_portability=True)
    with pytest.raises(SourceChangedError, match="identity"):
        controller._read(source, ref, revision, reviewed_portability=True)


def test_controller_requires_separate_ack_and_consumes_confirmation_once(tmp_path, monkeypatch):
    controller, source, path, ref = reviewed_source(tmp_path)
    requests = prepare_as_private_conversion(monkeypatch, path, tmp_path, controller)
    target = Profile("target", "Target", "opencode", tmp_path / "target")
    draft = controller.prepare(source, ref, target, reviewed_request(tmp_path), CancellationToken())
    assert (
        requests[0][0].reviewed_portability
        and requests[0][1]["max_source_bytes"] == 64 * 1024 * 1024
    )
    assert "acknowledge_reviewed_portability" not in requests[0][1]
    archive = Path(draft.proposal.to_dict()["reviewed_archive"]["path"])
    with pytest.raises(SessionMigrateError, match="displayed"):
        controller.commit(draft, CancellationToken(), lambda event: None)
    assert not archive.exists()
    receipt = controller.commit(
        draft, CancellationToken(), lambda event: None, acknowledge_reviewed_portability=True
    )
    assert receipt.legacy_result["reviewed_archive"]["status"] == "retained"
    with pytest.raises(SessionMigrateError, match="already consumed"):
        controller.commit(
            draft, CancellationToken(), lambda event: None, acknowledge_reviewed_portability=True
        )


@pytest.mark.parametrize("changed", ["mode", "budget"])
def test_prepared_mode_or_budget_change_cannot_publish(tmp_path, monkeypatch, changed):
    controller, source, path, ref = reviewed_source(tmp_path)
    prepare_as_private_conversion(monkeypatch, path, tmp_path, controller)
    target = Profile("target", "Target", "opencode", tmp_path / "target")
    draft = controller.prepare(source, ref, target, reviewed_request(tmp_path), CancellationToken())
    if changed == "mode":
        object.__setattr__(draft.request, "reviewed_portability", False)
    else:
        controller.source_bytes = 32 * 1024 * 1024
    with pytest.raises(SessionMigrateError, match="options changed"):
        controller.commit(
            draft, CancellationToken(), lambda event: None, acknowledge_reviewed_portability=True
        )
    assert not (tmp_path / "synthetic-target.json").exists()


def test_reviewed_preview_requires_exact_destination_before_prepare(tmp_path):
    controller, source, path, ref = reviewed_source(tmp_path)
    with pytest.raises(SessionMigrateError, match="OpenCode 2.2.5"):
        controller.prepare(
            source,
            ref,
            Profile("target", "Target", "codex", tmp_path / "target"),
            reviewed_request(tmp_path),
            CancellationToken(),
        )


def test_legacy_fingerprint_is_unchanged_and_reviewed_mode_is_distinct(tmp_path):
    controller, source, path, ref = reviewed_source(tmp_path)
    target = Profile("target", "Target", "opencode", tmp_path / "target")
    request = replace(reviewed_request(tmp_path), reviewed_portability=False)
    original = (
        source.fingerprint,
        ref,
        target.fingerprint,
        request.cwd,
        request.target_cli_version,
        request.model_provider,
        request.model,
        request.session_id,
        request.target_title,
        request.title_policy,
        request.require_native_title,
    )
    assert (
        fingerprint(source, ref, target, request)
        == hashlib.sha256(repr(original).encode()).hexdigest()
    )
    assert fingerprint(
        source, ref, target, replace(request, reviewed_portability=True)
    ) != fingerprint(source, ref, target, request)


def test_keyboard_reviewed_preview_acceptance_and_mode_invalidation(tmp_path, monkeypatch):
    controller, source, path, ref = reviewed_source(tmp_path)
    requests = prepare_as_private_conversion(monkeypatch, path, tmp_path, controller)
    calls = []
    commit = controller.commit

    def accepted(*args, **kwargs):
        calls.append(kwargs)
        return commit(*args, **kwargs)

    monkeypatch.setattr(controller, "commit", accepted)

    async def flow():
        app = MigrationApp(controller)
        async with app.run_test(size=(120, 60)) as pilot:
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = source.id
            app.query_one("#reviewed-portability", Checkbox).value = True
            await pilot.pause()
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            await activate(app, pilot, "preview")
            assert app.snapshot and app.reviewed_mode
            assert "ASSISTANT_LITERAL" in app.query_one("#preview-text", TextArea).text
            await activate(app, pilot, "target")
            app.query_one("#target-format", Select).value = "opencode"
            fill(app, "schema", "2.2.5")
            fill(app, "target-root", tmp_path / "target")
            fill(app, "target-home", tmp_path / "target-home")
            fill(app, "cwd", tmp_path)
            await pilot.pause()
            await activate(app, pilot, "prepare")
            assert app.draft and requests[-1][0].reviewed_portability
            text = str(app.query_one("#proposal", Literal).render())
            assert "Source reviewed report" in text and '"status": "planned"' in text
            assert "ASSISTANT_LITERAL" not in text
            assert (
                str(app.query_one("#confirm", Button).label)
                == "Accept displayed differences and transfer"
            )
            assert calls == []
            draft = app.draft
            app.query_one("#reviewed-portability", Checkbox).value = False
            await pilot.pause()
            assert app.draft is None and app.snapshot is None
            assert app.query_one("#confirm", Button).disabled
            assert draft.prepared._state is None
            app.show("target-step")
            prepared_count = len(requests)
            await activate(app, pilot, "prepare")
            assert len(requests) == prepared_count and app.draft is None
            assert "fresh source preview" in str(app.query_one("#status", Literal).render())
            app.show("sessions")
            app.query_one("#reviewed-portability", Checkbox).value = True
            await pilot.pause()
            await activate(app, pilot, "preview")
            await activate(app, pilot, "target")
            await activate(app, pilot, "prepare")
            await activate(app, pilot, "confirm")
            assert app.completed and calls == [{"acknowledge_reviewed_portability": True}]
            app.query_one("#confirm", Button).press()
            await pilot.pause()
            assert len(calls) == 1

    asyncio.run(flow())
