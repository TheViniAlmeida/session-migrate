"""Codex 0.166.0 proofs against a configured loopback provider and private roots.

The native processes inherit the network namespace. These checks prove offline
provider traffic and masked user state, not kernel-level network isolation.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_claude_codex_latest_native as native
from native_sandbox import private_root_masks

VERSION = "0.166.0"
SHA256 = "b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8"


@pytest.fixture(autouse=True)
def stock_nov_client(monkeypatch):
    binary = os.environ.get("SESSION_MIGRATE_NOV_CODEX_BIN")
    if not binary:
        pytest.skip(
            "set SESSION_MIGRATE_NOV_CODEX_BIN to the verified stock 0.166.0 bundle/bin/codex"
        )
    monkeypatch.setitem(native.VERSIONS, "codex", VERSION)
    monkeypatch.setitem(native.SHA256, "codex", SHA256)
    monkeypatch.setenv("SESSION_MIGRATE_LATEST_CODEX_BIN", binary)
    monkeypatch.setenv("SESSION_MIGRATE_LATEST_CODEX_SHA256", SHA256)
    original = native.NativeClient.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        # The existing native harness starts with an empty root. Explicit masks
        # also document and enforce the shared private-root regression contract.
        self.prefix[3:3] = private_root_masks()

    monkeypatch.setattr(native.NativeClient, "__init__", initialize)


def test_nov_stock_capture_cold_resume_and_app_server_read(tmp_path: Path):
    native.test_latest_stock_capture_and_cold_resume(tmp_path, "codex")


@pytest.mark.parametrize("history_kind", ("compacted", "tool-history"))
@pytest.mark.parametrize("writer_header", ("historical", "current"))
def test_nov_stock_cold_resumes_writer(tmp_path: Path, history_kind, writer_header):
    native.test_latest_stock_cold_resumes_current_writer(
        tmp_path, "codex", history_kind, writer_header
    )


def test_nov_stock_lists_and_reads_writer_without_inference(tmp_path: Path):
    native.test_latest_codex_app_server_reads_writer_history(tmp_path)


def test_nov_stock_persisted_fork_identity_and_inherited_read(tmp_path: Path):
    """Native session trees distinguish fork ancestry from subagent parentage."""
    with native.offline_provider(native.REPLY) as provider:
        client = native.NativeClient(tmp_path, "codex", provider)
        completed = client.turn(native.PROMPT, resume=False)
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        source = native.codex.parse(client.history()).session_id
        assert source
        inference_count = len(provider.requests)
        with _experimental_app_server(client) as call:
            fork = call(2, "thread/fork", {"threadId": source, "excludeTurns": False})["thread"]
            assert fork["id"] != source
            assert fork["forkedFromId"] == source
            assert fork["parentThreadId"] is None
            assert fork["sessionId"] == fork["id"]
            read = call(3, "thread/read", {"threadId": fork["id"], "includeTurns": True})
            history = json.dumps(read["thread"]["turns"])
            assert history.count(native.PROMPT) == 1
            assert history.count(native.REPLY) == 1
            listing = call(
                4,
                "thread/list",
                {"limit": 100, "sourceKinds": ["exec", "appServer"], "modelProviders": []},
            )
            assert source in {t["id"] for t in listing["data"]}
            resumed = call(5, "thread/resume", {"threadId": fork["id"], "excludeTurns": True})
            assert resumed["thread"]["id"] == fork["id"]
        assert len(provider.requests) == inference_count
        completed = client.turn(native.FOLLOWUP, resume=True, session_id=fork["id"])
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        native._assert_request(provider, [native.PROMPT, native.REPLY, native.FOLLOWUP], "codex")
        metas = [
            json.loads(path.read_text().splitlines()[0])["payload"]
            for path in client.client.glob("sessions/*/*/*/rollout-*.jsonl")
        ]
        fork_meta = next(meta for meta in metas if meta["id"] == fork["id"])
        assert fork_meta["session_id"] == fork["id"]
        assert fork_meta["forked_from_id"] == source
        assert fork_meta.get("parent_thread_id") is None
        assert fork_meta["history_base"]["thread_id"] == source
        assert fork_meta["forked_from_ordinal_exclusive"] > 0
        destination = os.environ.get("SESSION_MIGRATE_LATEST_CAPTURE_DIR")
        if destination:
            Path(destination, "native-nov-fork-metadata.json").write_text(
                json.dumps({"fork": fork, "metas": metas}, indent=2) + "\n"
            )


@contextmanager
def _experimental_app_server(client):
    """Enable the documented experimental attached-thread API in a private store."""
    process = subprocess.Popen(
        client.prefix
        + [
            client.executable,
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
            "app-server",
        ],
        env=client.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    responses = queue.Queue()

    def read():
        assert process.stdout
        for line in process.stdout:
            responses.put(json.loads(line))

    thread = threading.Thread(target=read, daemon=True)
    thread.start()

    def call(number, method, params):
        assert process.stdin
        process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        while True:
            result = responses.get(timeout=30)
            if result.get("id") == number:
                assert "error" not in result, result
                return result["result"]

    try:
        call(
            1,
            "initialize",
            {
                "clientInfo": {"name": "nov-synthetic-gate", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        yield call
    finally:
        # EOF lets app-server release live rollout writers before the private
        # client process is re-entered for an independent cold resume.
        assert process.stdin
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        thread.join(timeout=5)


def test_nov_stock_attached_fork_is_persisted_and_hidden_from_ordinary_listing(tmp_path: Path):
    with native.offline_provider(native.REPLY) as provider:
        client = native.NativeClient(tmp_path, "codex", provider)
        completed = client.turn(native.PROMPT, resume=False)
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        source = native.codex.parse(client.history()).session_id
        assert source
        inference_count = len(provider.requests)
        with _experimental_app_server(client) as call:
            attached = call(
                2,
                "thread/fork",
                {
                    "threadId": source,
                    "attachToSource": True,
                    "excludeTurns": True,
                },
            )["thread"]
            assert attached["forkedFromId"] == source
            assert attached["parentThreadId"] is None
            listing = call(
                3,
                "thread/list",
                {
                    "limit": 100,
                    "sourceKinds": ["exec", "appServer"],
                    "modelProviders": [],
                },
            )
            assert attached["id"] not in {t["id"] for t in listing["data"]}
            children = call(
                4,
                "thread/list",
                {
                    "attachedToThreadId": source,
                    "modelProviders": [],
                },
            )
            assert attached["id"] in {t["id"] for t in children["data"]}
            read = call(5, "thread/read", {"threadId": attached["id"], "includeTurns": True})
            assert read["thread"]["turns"] == []
        assert len(provider.requests) == inference_count
        metas = [
            json.loads(path.read_text().splitlines()[0])["payload"]
            for path in client.client.glob("sessions/*/*/*/rollout-*.jsonl")
        ]
        meta = next(m for m in metas if m["id"] == attached["id"])
        assert meta["attached_thread"] is True
        assert meta["forked_from_id"] == source
        assert meta.get("parent_thread_id") is None
        destination = os.environ.get("SESSION_MIGRATE_LATEST_CAPTURE_DIR")
        if destination:
            Path(destination, "native-nov-attached-fork-metadata.json").write_text(
                json.dumps({"attached": attached, "metas": metas}, indent=2) + "\n"
            )


def test_nov_stock_reads_compressed_rollout_and_materializes_on_cold_resume(tmp_path: Path):
    """A precompressed synthetic writer fixture also runs on supported Python 3.11+."""
    from base64 import b64decode
    from hashlib import sha256

    # Generated once with Python 3.14 compression.zstd from serialize_events:
    # fixed SESSION_ID, legacy 0.166.0, PROMPT/REPLY, /fixture/work, fixture provider.
    # Runtime decoding is performed by the verified native Codex, not Python.
    compressed = b64decode(
        "KLUv/WD4An0LAPbTQCbwyqoOFrwKmUAzWQZFW67dzqvJYrEAu03iVgURGY34JvAMXB0GGjQANAA4AInLdHdGQAPA"
        "+Ds1M++UrJYmsbvLZhqvOu/l+3fGtL57wnMcKOA0z9MsFI7i7xiVfZnOfiZMTUu1WLSYqvTu7zzZrN67O4jF4kE4"
        "f7dsidOa7s4tbTVpY2vLa1bT1rs32HpJsM0aMxcCkcQRYXQgkaLoeEDIAEYokwghPGAjEuEEgg3IXyXvZRot36y6"
        "NPbFFj5s27VJVmz9PWq29p0Mdw+Hbat0VvuSfee9Ej4KJEmkQCJIqmSlMdyxM+yaCu99cV91urtV7R3lnpHvVbHL"
        "duBREEQJigIoIDACYwhRuwFCPdBK0KhCOQXK1C9tMD0QqoJQi1GKAVB0+9hCc4GTCxmmn2qhz2pCIsD8PqNErDLZ"
        "2uB6zkIW8hGX2TdZnpIHjXcll8m6gkMA/mPbCaTtoZl7cH+3GFiI3DBsrYdXcQI="
    )
    assert sha256(compressed).hexdigest() == (
        "18f8ac87ccf920d13b46737d3d390c58028d5df7e735f1e2c14df763797ab99e"
    )
    with native.offline_provider(native.REPLY) as provider:
        client = native.NativeClient(tmp_path, "codex", provider)
        history = client.client / native.codex.rollout_relative_path(
            native.SESSION_ID, "2026-11-05T00:00:00Z"
        )
        history.parent.mkdir(parents=True)
        history.with_suffix(".jsonl.zst").write_bytes(compressed)
        with _experimental_app_server(client) as call:
            read = call(2, "thread/read", {"threadId": native.SESSION_ID, "includeTurns": True})
            view = json.dumps(read["thread"]["turns"])
            assert view.count(native.PROMPT) == 1
            assert view.count(native.REPLY) == 1
        assert not provider.requests
        resumed = client.turn(native.FOLLOWUP, resume=True)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert history.is_file()
        native._assert_request(provider, [native.PROMPT, native.REPLY, native.FOLLOWUP], "codex")
        native._record(client, provider, "compressed-cold-resume", resumed, history)
