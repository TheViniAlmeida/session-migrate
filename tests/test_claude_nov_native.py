"""November stock Claude gates; run inside a loopback-only network namespace.

Opt in with SESSION_MIGRATE_NOV_CLAUDE_BIN and the matching _SHA256. Only
synthetic transcripts and an explicit Haiku model reach the local protocol stub.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import test_claude_codex_latest_native as native

VERSION = "2.1.318"
SHA256 = "708c77b204db30086ff6f1ed34577bda03e1be74ced80557cbf7b54249ebf0bd"
STOCK_SHA256 = {
    VERSION: SHA256,
    "2.1.317": "eb224419e344575faa4a1f39b290b649425a10cd5ee2ef1d003480cd9ae57eb0",
}


class _OuterNamespaceClient(native.NativeClient):
    """Reuse native semantics inside one already verified outer filesystem/netns."""

    def __init__(self, root, format_name, provider):
        assert format_name == "claude"
        assert os.geteuid() != 0
        assert {name for _, name in socket.if_nameindex()} == {"lo"}
        mounts = {
            fields[4]: (fields[5].split(","), line.split(" - ", 1)[1].split()[0])
            for line in Path("/proc/self/mountinfo").read_text().splitlines()
            if (fields := line.split())
        }
        assert mounts["/"][1] == "tmpfs"
        for private in ("/home", "/storage", "/root", "/run"):
            assert mounts[private][1] == "tmpfs"
            assert not list(Path(private).iterdir())
        assert "ro" in mounts["/repo"][0]
        assert "ro" in mounts["/stock/claude"][0]
        assert "rw" in mounts["/tmp"][0]
        assert root.is_relative_to(Path("/tmp"))
        self.root, self.format_name = root, format_name
        for name in ("client", "work", "home", "cache", "config", "data", "state", "temp"):
            (root / name).mkdir(mode=0o700)
        self.client, self.work = root / "client", root / "work"
        self.prefix = []
        self.executable = os.environ["SESSION_MIGRATE_NOV_CLAUDE_BIN"]
        assert self.executable == "/stock/claude"
        assert (
            hashlib.sha256(Path(self.executable).read_bytes()).hexdigest()
            == native.SHA256["claude"]
        )
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "NO_PROXY": "*",
            "HOME": str(root / "home"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "TMPDIR": str(root / "temp"),
            "CLAUDE_CONFIG_DIR": str(self.client),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "ANTHROPIC_API_KEY": "synthetic-loopback-only",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{provider.server_address[1]}",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        initialized = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "init.templateDir=",
                "-c",
                "init.defaultBranch=main",
                "init",
                "--quiet",
                str(self.work),
            ],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert initialized.returncode == 0, initialized.stderr
        version = self.run(["--version"])
        assert version.returncode == 0
        assert version.stdout.strip() == f"{native.VERSIONS['claude']} (Claude Code)"

    def run(self, arguments):
        return subprocess.run(
            [self.executable, *arguments],
            cwd=self.work,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )


@pytest.fixture(autouse=True)
def november_stock(monkeypatch):
    binary = os.environ.get("SESSION_MIGRATE_NOV_CLAUDE_BIN")
    if not binary:
        pytest.skip("set SESSION_MIGRATE_NOV_CLAUDE_BIN to the verified November stock binary")
    version = os.environ.get("SESSION_MIGRATE_NOV_CLAUDE_VERSION", VERSION)
    assert version in STOCK_SHA256
    digest = STOCK_SHA256[version]
    assert os.environ.get("SESSION_MIGRATE_NOV_CLAUDE_SHA256") == digest
    assert {name for _, name in socket.if_nameindex()} == {"lo"}, (
        "stock Claude probes require an outer loopback-only network namespace"
    )
    monkeypatch.setenv("SESSION_MIGRATE_LATEST_CLAUDE_BIN", binary)
    monkeypatch.setenv("SESSION_MIGRATE_LATEST_CLAUDE_SHA256", digest)
    monkeypatch.setitem(native.VERSIONS, "claude", version)
    monkeypatch.setitem(native.SHA256, "claude", digest)
    if os.environ.get("SESSION_MIGRATE_NOV_OUTER_NAMESPACE") == "1":
        monkeypatch.setattr(native, "NativeClient", _OuterNamespaceClient)


def test_nov_stock_capture_and_cold_resume(tmp_path):
    native.test_latest_stock_capture_and_cold_resume(tmp_path, "claude")


@pytest.mark.parametrize("history_kind", ("compacted", "tool-history"))
@pytest.mark.parametrize("writer_header", ("historical", "current"))
def test_nov_stock_cold_resumes_portable_writer(tmp_path, history_kind, writer_header):
    native.test_latest_stock_cold_resumes_current_writer(
        tmp_path, "claude", history_kind, writer_header
    )


def test_nov_stock_fork_persists_independent_transcript(tmp_path):
    with native.offline_provider(native.REPLY) as provider:
        client = native.NativeClient(tmp_path, "claude", provider)
        started = client.turn(native.PROMPT, resume=False)
        assert started.returncode == 0, (started.stdout, started.stderr)
        parent_path = client.history()
        before = parent_path.read_bytes()
        parent_id = native.claude.parse(parent_path).session_id
        assert parent_id
        provider.reply = native.SECOND_REPLY
        forked = client.run(
            [
                "--bare",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                native.MODELS["claude"],
                "--disable-slash-commands",
                "--no-chrome",
                "--resume",
                parent_id,
                "--fork-session",
                native.FOLLOWUP,
            ]
        )
        assert forked.returncode == 0, (forked.stdout, forked.stderr)
        assert parent_path.read_bytes() == before
        children = [
            path for path in client.client.glob("projects/*/*.jsonl") if path != parent_path
        ]
        assert len(children) == 1
        child = native.claude.parse(children[0])
        assert child.session_id and child.session_id != parent_id
        native._assert_request(provider, [native.PROMPT, native.REPLY, native.FOLLOWUP], "claude")
        native._assert_messages(
            children[0],
            "claude",
            [
                (native.Role.USER, native.PROMPT),
                (native.Role.ASSISTANT, native.REPLY),
                (native.Role.USER, native.FOLLOWUP),
                (native.Role.ASSISTANT, native.SECOND_REPLY),
            ],
        )
        rows = [json.loads(line) for line in children[0].read_text().splitlines()]
        assert all(row.get("isSidechain") is not True for row in rows)
        native._record(client, provider, "fork", forked, children[0])
        # Cold resume must reload the independent fork, rather than the original.
        resumed = client.turn("NOV_FORK_COLD_RESUME_9217", resume=True, session_id=child.session_id)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        native._assert_request(provider, [native.FOLLOWUP, "NOV_FORK_COLD_RESUME_9217"], "claude")
        assert parent_path.read_bytes() == before
        native._record(client, provider, "fork-resume", resumed, children[0])


def test_nov_custom_project_namespace_remains_guarded(tmp_path):
    native.test_latest_claude_native_project_directory_override(tmp_path)
