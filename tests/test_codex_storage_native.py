"""Opt-in stock Codex proofs with private filesystem roots and synthetic turns."""

from __future__ import annotations

import json
from pathlib import Path

import test_claude_codex_latest_native as native
from test_codex_nov_native import _experimental_app_server, stock_nov_client  # noqa: F401
from zstd_fixture import compress_fixture

from session_migrate import codex_storage as storage
from session_migrate.formats import codex
from session_migrate.model import EventKind


def _messages(path: Path) -> list[str]:
    return [event.text for event in codex.parse(path).events if event.kind == EventKind.MESSAGE]


def test_stock_compressed_ancestor_matches_offline_fork_and_cold_resume(tmp_path: Path):
    with native.offline_provider(native.REPLY) as provider:
        client = native.NativeClient(tmp_path, "codex", provider)
        completed = client.turn(native.PROMPT, resume=False)
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        source_path = client.history()
        source_id = codex.parse(source_path).session_id
        before = len(provider.requests)
        with _experimental_app_server(client) as call:
            fork = call(2, "thread/fork", {"threadId": source_id, "excludeTurns": False})["thread"]
        child = next(client.client.glob(f"sessions/*/*/*/*-{fork['id']}.jsonl"))
        compressed = source_path.with_suffix(".jsonl.zst")
        compressed.write_bytes(compress_fixture(source_path.read_bytes()))
        source_path.unlink()
        history = storage.materialize_codex_history(client.client, child)
        assert history.components[1].source_path == compressed
        messages = _messages(child)
        assert messages.count(native.PROMPT) == 1
        assert messages.count(native.REPLY) == 1
        with _experimental_app_server(client) as call:
            read = call(2, "thread/read", {"threadId": fork["id"], "includeTurns": True})
            view = json.dumps(read["thread"]["turns"])
            assert view.count(native.PROMPT) == 1
            assert view.count(native.REPLY) == 1
        assert len(provider.requests) == before
        resumed = client.turn(native.FOLLOWUP, resume=True, session_id=fork["id"])
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        native._assert_request(provider, [native.PROMPT, native.REPLY, native.FOLLOWUP], "codex")
        messages = _messages(child)
        assert messages.count(native.PROMPT) == 1
        assert messages.count(native.REPLY) == 2
        assert messages.count(native.FOLLOWUP) == 1


def test_stock_attached_empty_history_agrees_with_offline_projection(tmp_path: Path):
    with native.offline_provider(native.REPLY) as provider:
        client = native.NativeClient(tmp_path, "codex", provider)
        completed = client.turn(native.PROMPT, resume=False)
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        source_id = codex.parse(client.history()).session_id
        before = len(provider.requests)
        with _experimental_app_server(client) as call:
            attached = call(
                2,
                "thread/fork",
                {"threadId": source_id, "attachToSource": True, "excludeTurns": True},
            )["thread"]
            read = call(3, "thread/read", {"threadId": attached["id"], "includeTurns": True})
            assert read["thread"]["turns"] == []
            fork = call(4, "thread/fork", {"threadId": attached["id"], "excludeTurns": False})[
                "thread"
            ]
            read = call(5, "thread/read", {"threadId": fork["id"], "includeTurns": True})
            assert read["thread"]["turns"] == []
        child = next(client.client.glob(f"sessions/*/*/*/*-{attached['id']}.jsonl"))
        result = storage.materialize_codex_history(client.client, child)
        assert len(result.components) == 2
        assert _messages(child) == []
        fork_path = next(client.client.glob(f"sessions/*/*/*/*-{fork['id']}.jsonl"))
        assert _messages(fork_path) == []
        assert len(provider.requests) == before
