"""Cancellation is explicit and atomic; progress observers remain observational."""

import hashlib
import threading
from pathlib import Path

import pytest

from session_migrate import operations
from session_migrate.errors import OperationCancelledError, SessionMigrateError
from session_migrate.model import TargetFormat
from session_migrate.operations import (
    CancellationToken,
    OperationRequest,
    ProgressStage,
    run_operation,
)

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"


def request(tmp_path):
    return OperationRequest(
        "import",
        path=FIXTURE,
        target_format=TargetFormat.CODEX,
        home=tmp_path / "target",
        cwd=tmp_path,
    )


def test_cancel_before_loading_and_token_cannot_be_reused(tmp_path, monkeypatch):
    token = CancellationToken()
    assert token.request_cancel()
    monkeypatch.setattr(operations, "load_session", lambda *args: pytest.fail("must not load"))
    with pytest.raises(OperationCancelledError):
        run_operation(request(tmp_path), environ={"HOME": str(tmp_path)}, cancellation=token)
    assert not (tmp_path / "target").exists()
    with pytest.raises(SessionMigrateError, match="already belongs"):
        run_operation(request(tmp_path), environ={"HOME": str(tmp_path)}, cancellation=token)


@pytest.mark.parametrize("stage", [ProgressStage.CONVERTING, ProgressStage.PREFLIGHT])
def test_cancel_after_parse_or_preflight_never_installs(tmp_path, stage):
    token = CancellationToken()

    def progress(event):
        if event.stage == stage:
            assert token.request_cancel()

    with pytest.raises(OperationCancelledError):
        run_operation(
            request(tmp_path),
            environ={"HOME": str(tmp_path)},
            cancellation=token,
            progress=progress,
        )
    assert not (tmp_path / "target").exists()


def test_after_publication_cancel_is_refused_and_receipt_is_completed(tmp_path):
    token = CancellationToken()
    events = []

    def progress(event):
        events.append(event.stage)
        if event.stage == ProgressStage.INSTALLING:
            assert token.publishing
            assert not token.request_cancel()

    result = run_operation(
        request(tmp_path), environ={"HOME": str(tmp_path)}, cancellation=token, progress=progress
    )
    assert Path(result.output).is_file()
    assert events[-1] == ProgressStage.COMPLETED


def test_atomic_cancel_and_publication_have_one_winner():
    for _ in range(30):
        token = CancellationToken()
        token.claim()
        barrier = threading.Barrier(2)
        results = []

        def publish(barrier=barrier, token=token, results=results):
            barrier.wait()
            try:
                token.begin_publication()
                results.append("published")
            except OperationCancelledError:
                results.append("cancelled")

        worker = threading.Thread(target=publish)
        worker.start()
        barrier.wait()
        accepted = token.request_cancel()
        worker.join()
        assert results == (["cancelled"] if accepted else ["published"])


def test_tui_command_non_tty_keeps_cli_contract(capsys):
    from session_migrate.cli import main

    assert main(["tui"]) == 2
    assert "interactive terminal" in capsys.readouterr().err
    with pytest.raises(SystemExit) as failure:
        main([])
    assert failure.value.code == 2


def test_tui_extra_missing_is_lazy_and_never_installed(monkeypatch, capsys):
    import builtins
    import sys

    from session_migrate.cli import main

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "session_migrate.tui.app":
            raise ImportError("synthetic missing extra")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert main(["tui"]) == 2
    assert "session-migrate[tui]" in capsys.readouterr().err


def test_token_closes_when_environment_path_setup_fails(tmp_path):
    token = CancellationToken()
    with pytest.raises(SessionMigrateError, match="explicit HOME"):
        run_operation(
            OperationRequest(
                "convert",
                path=Path("~/source.jsonl"),
                target_format="codex",
                output=tmp_path / "result.jsonl",
            ),
            environ={},
            cancellation=token,
        )
    assert not token.request_cancel()
    with pytest.raises(SessionMigrateError, match="already belongs"):
        run_operation(request(tmp_path), environ={"HOME": str(tmp_path)}, cancellation=token)


@pytest.mark.parametrize("drift_at", ["after_load", "after_preflight"])
def test_prepublication_check_rejects_source_drift_without_output(tmp_path, monkeypatch, drift_at):
    source = tmp_path / "source.jsonl"
    source.write_bytes(FIXTURE.read_bytes())
    original_capture = operations.capture_source
    original_check = operations.ensure_target_paths_available
    original_install = operations._install
    native_calls = []
    initial = source.read_bytes()

    def capture(*args, **kwargs):
        snapshot = original_capture(*args, **kwargs)
        if drift_at == "after_load":
            source.write_bytes(initial + b"\n")
        return snapshot

    def preflight(*paths):
        original_check(*paths)
        if drift_at == "after_preflight":
            source.write_bytes(initial + b"\n")

    def install(*values):
        if not values[-1]:
            native_calls.append(True)
        return original_install(*values)

    monkeypatch.setattr(operations, "capture_source", capture)
    monkeypatch.setattr(operations, "ensure_target_paths_available", preflight)
    monkeypatch.setattr(operations, "_install", install)

    def validate():
        if source.read_bytes() != initial:
            raise SessionMigrateError("Source changed.")

    token = CancellationToken()
    operation = OperationRequest(
        "import", path=source, target_format="codex", home=tmp_path / "target", cwd=tmp_path
    )
    with pytest.raises(SessionMigrateError, match="Source changed"):
        run_operation(
            operation,
            environ={"HOME": str(tmp_path)},
            cancellation=token,
            prepublication_check=validate,
        )
    assert not token.publishing
    assert not token.request_cancel()
    assert not (tmp_path / "target").exists()
    assert native_calls == []
    assert hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(initial).digest()
