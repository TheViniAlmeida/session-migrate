"""Stock latest native capture and cold-resume gates using only a loopback stub.

Opt in with SESSION_MIGRATE_LATEST_{CLAUDE,CODEX}_BIN and matching _SHA256.
These tests never load a real HOME, account, provider, model or repository policy.
The Claude model is explicitly Haiku 4.5; the Codex model is a nonexistent stub ID.
No permission or sandbox bypass flags are used.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from offline_provider import OfflineProvider, _Handler, request_text

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    target_import_paths,
    write_artifact,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, codex
from session_migrate.model import EventKind, Role, TargetFormat
from session_migrate.operations import OperationRequest, prepare_operation

VERSIONS = {"claude": "2.1.306", "codex": "0.163.0"}
HISTORICAL_VERSIONS = {"claude": "2.1.209", "codex": "0.144.4"}
SHA256 = {
    "claude": "520bc6c1f40af466bd03a278b5ae1af1a2938bc7de5b9e5cb15526da20d71cfe",
    "codex": "399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215",
}
MODELS = {"claude": "claude-haiku-4-5-20251001", "codex": "fake-fixture-model"}
SESSION_ID = "83838383-8383-4383-8383-838383838383"
PROMPT = "LATEST_NATIVE_SYNTHETIC_PROMPT_COPPER_9217"
REPLY = "LATEST_NATIVE_SYNTHETIC_REPLY_ORBIT_9217"
FOLLOWUP = "LATEST_NATIVE_COLD_RESUME_FOLLOWUP_9217"
SECOND_REPLY = "LATEST_NATIVE_COLD_RESUME_REPLY_9217"


class LatestProviderHandler(_Handler):
    def _body(self, body: bytes, content_type: str) -> None:
        # Response item IDs are unique across turns, as required by the real APIs.
        assert isinstance(self.server, OfflineProvider)
        suffix = str(len(self.server.requests)).encode()
        body = body.replace(b"msg_session_migrate_offline", b"msg_latest_fixture_" + suffix)
        body = body.replace(b"resp_session_migrate_offline", b"resp_latest_fixture_" + suffix)
        super()._body(body, content_type)

    def do_POST(self) -> None:  # noqa: N802
        # Stock Claude appends ?beta=true; this is still the Messages endpoint.
        self.path = self.path.partition("?")[0]
        super().do_POST()

    def do_GET(self) -> None:  # noqa: N802
        # Optional model discovery stays entirely within the local protocol stub.
        if self.path.startswith("/v1/models"):
            model = {
                "id": MODELS["claude"],
                "type": "model",
                "display_name": "Synthetic Haiku 4.5",
                "created_at": "2026-10-21T00:00:00Z",
            }
            self._json({"data": [model], "has_more": False} if self.path == "/v1/models" else model)
            return
        super().do_GET()


@contextmanager
def offline_provider(reply: str) -> Iterator[OfflineProvider]:
    provider = OfflineProvider(("127.0.0.1", 0), LatestProviderHandler)
    provider.requests = []
    provider.reply = reply
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        yield provider
    finally:
        provider.shutdown()
        thread.join(timeout=5)
        provider.server_close()


class NativeClient:
    def __init__(self, root: Path, format_name: str, provider: OfflineProvider) -> None:
        self.root = root
        self.format_name = format_name
        self.client = root / "client"
        self.work = root / "work"
        for name in ("client", "work", "home", "cache", "config", "data", "temp"):
            (root / name).mkdir(mode=0o700)
        variable = f"SESSION_MIGRATE_LATEST_{format_name.upper()}"
        binary_value = os.environ.get(f"{variable}_BIN")
        digest = os.environ.get(f"{variable}_SHA256")
        if not binary_value or not digest:
            pytest.skip(f"set {variable}_BIN and {variable}_SHA256 to verified stock artifacts")
        binary = Path(binary_value).resolve(strict=True)
        assert digest == SHA256[format_name]
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest
        bwrap = shutil.which("bwrap")
        if not bwrap:
            pytest.skip("bubblewrap is required to mask all real user state")
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
            cwd=root,
            env={
                "HOME": str(root / "home"),
                "PATH": "/usr/bin:/bin",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert initialized.returncode == 0, initialized.stderr
        self.prefix = [bwrap, "--tmpfs", "/", "--unshare-pid", "--unshare-ipc"]
        for directory in ("/usr", "/lib", "/lib64", "/etc"):
            if Path(directory).exists():
                self.prefix += ["--ro-bind", directory, directory]
        self.executable = "/native/client" if format_name == "claude" else "/native/bin/codex"
        native_source = binary if format_name == "claude" else binary.parent.parent
        native_destination = "/native/client" if format_name == "claude" else "/native"
        self.prefix += [
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--dir",
            "/tmp",
            "--dir",
            "/native",
            "--ro-bind",
            str(native_source),
            native_destination,
            "--bind",
            str(root),
            "/fixture",
            "--chdir",
            "/fixture/work",
        ]
        self.environment = {
            "HOME": "/fixture/home",
            "XDG_CACHE_HOME": "/fixture/cache",
            "XDG_CONFIG_HOME": "/fixture/config",
            "XDG_DATA_HOME": "/fixture/data",
            "TMPDIR": "/fixture/temp",
            "PATH": "/usr/bin:/bin",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "NO_PROXY": "*",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        endpoint = f"http://127.0.0.1:{provider.server_address[1]}"
        if format_name == "claude":
            self.environment.update(
                {
                    "CLAUDE_CONFIG_DIR": "/fixture/client",
                    "ANTHROPIC_API_KEY": "synthetic-loopback-only",
                    "ANTHROPIC_BASE_URL": endpoint,
                    "DISABLE_TELEMETRY": "1",
                    "DISABLE_ERROR_REPORTING": "1",
                    "DISABLE_AUTOUPDATER": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                }
            )
        else:
            self.environment.update({"CODEX_HOME": "/fixture/client"})
            (self.client / "config.toml").write_text(
                f'model = "{MODELS[format_name]}"\nmodel_provider = "fixture"\n'
                'approval_policy = "on-request"\nsandbox_mode = "read-only"\n'
                '[model_providers.fixture]\nname = "Synthetic localhost only"\n'
                f'base_url = "{endpoint}/v1"\nwire_api = "responses"\n'
                "requires_openai_auth = false\n",
                encoding="utf-8",
            )
        version = self.run(["--version"])
        assert version.returncode == 0, (version.stdout, version.stderr)
        assert VERSIONS[format_name] in version.stdout.strip()

    def run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.prefix + [self.executable, *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    def turn(
        self,
        prompt: str,
        *,
        resume: bool,
        session_id: str = SESSION_ID,
    ) -> subprocess.CompletedProcess[str]:
        if self.format_name == "claude":
            args = [
                "--bare",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                MODELS["claude"],
                "--disable-slash-commands",
                "--no-chrome",
                "--resume" if resume else "--session-id",
                session_id,
                prompt,
            ]
        else:
            args = [
                "--disable",
                "apps",
                "--disable",
                "plugins",
                "--disable",
                "remote_plugin",
                "--ask-for-approval",
                "on-request",
                "--sandbox",
                "read-only",
                "exec",
            ]
            if resume:
                args += ["resume", session_id]
            args += ["--model", MODELS["codex"], "--json", prompt]
        return self.run(args)

    def history(self) -> Path:
        candidates = (
            list(self.client.glob("projects/*/*.jsonl"))
            if self.format_name == "claude"
            else list(self.client.glob("sessions/*/*/*/rollout-*.jsonl"))
        )
        assert len(candidates) == 1, candidates
        return candidates[0]


def _assert_messages(path: Path, format_name: str, expected: list[tuple[Role, str]]) -> None:
    parsed = (claude if format_name == "claude" else codex).parse(path)
    if format_name == "codex":
        records = [json.loads(line) for line in path.read_text().splitlines()]
        if records[0]["payload"].get("history_mode") == "paginated":
            assert [record.get("ordinal") for record in records] == list(range(len(records)))
        assert not any(
            "<environment_context>" in (event.text or "")
            for event in parsed.events
            if event.kind == EventKind.MESSAGE
        )
    actual = [
        (event.role, event.text) for event in parsed.events if event.kind == EventKind.MESSAGE
    ]
    for message in expected:
        assert actual.count(message) == 1, (message, actual)


def _assert_request(provider: OfflineProvider, markers: list[str], format_name: str) -> None:
    assert provider.requests, "the native client never reached the loopback provider"
    path, request = provider.requests[-1]
    assert path.rstrip("/").endswith("/messages" if format_name == "claude" else "/responses")
    assert request["model"] == MODELS[format_name]
    # Inspect actual native model history, excluding system/tool declarations.
    history = request_text({"history": request.get("messages", request.get("input"))})
    for marker in markers:
        assert history.count(marker) == 1, (marker, history)


def _record(
    native: NativeClient,
    provider: OfflineProvider,
    name: str,
    completed: subprocess.CompletedProcess[str],
    history: Path | None = None,
) -> None:
    """Optional synthetic-only evidence; no inherited environment or real user files."""
    directory = os.environ.get("SESSION_MIGRATE_LATEST_CAPTURE_DIR")
    if not directory:
        return
    label = os.environ.get("SESSION_MIGRATE_LATEST_CAPTURE_LABEL", "")
    assert not label or label.replace("-", "").isalnum()
    destination = Path(directory) / f"native-probe-{label}{native.format_name}-{name}.json"
    value = {
        "client": native.format_name,
        "version": VERSIONS[native.format_name],
        "model": MODELS[native.format_name],
        "network": "127.0.0.1 stub only",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "requests": provider.requests,
        "history": [json.loads(line) for line in history.read_text().splitlines()]
        if history and history.exists()
        else None,
    }
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("format_name", ("claude", "codex"))
def test_latest_stock_capture_and_cold_resume(tmp_path: Path, format_name: str) -> None:
    with offline_provider(REPLY) as provider:
        native = NativeClient(tmp_path, format_name, provider)
        completed = native.turn(PROMPT, resume=False)
        _record(native, provider, "capture-process", completed)
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        history = native.history()
        _record(native, provider, "capture", completed, history)
        captured_session = (claude if format_name == "claude" else codex).parse(history)
        assert captured_session.cli_version == VERSIONS[format_name]
        _assert_messages(history, format_name, [(Role.USER, PROMPT), (Role.ASSISTANT, REPLY)])
        _assert_request(provider, [PROMPT], format_name)
        before = history.read_bytes()
        # A second process must reload the persisted session, rather than retaining RAM state.
        captured = (claude if format_name == "claude" else codex).parse(history).session_id
        assert captured
        provider.reply = SECOND_REPLY
        resumed = native.turn(FOLLOWUP, resume=True, session_id=captured)
        _record(native, provider, "capture-resume", resumed, history)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        after = history.read_bytes()
        assert after != before
        if format_name == "claude":
            assert len(after) > len(before) and after.startswith(before)
        _assert_request(provider, [PROMPT, REPLY, FOLLOWUP], format_name)
        _assert_messages(
            history,
            format_name,
            [
                (Role.USER, PROMPT),
                (Role.ASSISTANT, REPLY),
                (Role.USER, FOLLOWUP),
                (Role.ASSISTANT, SECOND_REPLY),
            ],
        )
        if format_name == "codex":
            inference_count = len(provider.requests)
            with _app_server(native) as call:
                actual_history = call(
                    2,
                    "thread/read",
                    {
                        "threadId": captured,
                        "includeTurns": True,
                    },
                )
            turn_items = json.dumps(actual_history["thread"]["turns"], ensure_ascii=False)
            for marker in (PROMPT, REPLY, FOLLOWUP, SECOND_REPLY):
                assert turn_items.count(marker) == 1, (marker, actual_history)
            assert len(provider.requests) == inference_count


@pytest.mark.parametrize("format_name", ("claude", "codex"))
@pytest.mark.parametrize("history_kind", ("compacted", "tool-history"))
@pytest.mark.parametrize("writer_header", ("historical", "current"))
def test_latest_stock_cold_resumes_current_writer(
    tmp_path: Path,
    format_name: str,
    history_kind: str,
    writer_header: str,
) -> None:
    with offline_provider(SECOND_REPLY) as provider:
        native = NativeClient(tmp_path, format_name, provider)
        source = claude.parse(Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl")
        assert any(event.kind == EventKind.COMPACTION for event in source.events)
        if history_kind == "tool-history":
            source = replace(
                source,
                events=tuple(
                    event for event in source.events if event.kind != EventKind.COMPACTION
                ),
            )
        target_version = (
            VERSIONS[format_name]
            if writer_header == "current"
            else HISTORICAL_VERSIONS[format_name]
        )
        artifact = convert_session(
            source,
            ConversionOptions(
                target_format=TargetFormat(format_name),
                session_id=SESSION_ID,
                cwd=Path("/fixture/work"),
                model=MODELS[format_name],
                model_provider="fixture",
                target_cli_version=target_version,
            ),
        )
        destination, manifest = target_import_paths(artifact, native.client)
        write_artifact(artifact, output_path=destination, manifest_path=manifest)
        before = destination.read_bytes()
        records = [json.loads(line) for line in before.decode().splitlines()]
        if format_name == "claude":
            versions = {record["version"] for record in records if "version" in record}
            assert versions == {target_version}
        else:
            assert records[0]["payload"]["cli_version"] == target_version
        completed = native.turn(FOLLOWUP, resume=True)
        _record(
            native,
            provider,
            f"writer-resume-{writer_header}-{history_kind}",
            completed,
            destination,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        after = destination.read_bytes()
        assert after != before
        if format_name == "claude":
            assert len(after) > len(before) and after.startswith(before)
        _assert_request(
            provider,
            [
                "Continue after the synthetic compaction.",
                "The synthetic post-compaction fixture is complete.",
                FOLLOWUP,
            ],
            format_name,
        )
        if history_kind == "tool-history":
            _assert_request(
                provider,
                [
                    "Remember synthetic migrator nonce ALPHA-1042.",
                    "Synthetic fixture output.",
                ],
                format_name,
            )
        _assert_messages(
            destination,
            format_name,
            [
                (Role.USER, "Continue after the synthetic compaction."),
                (Role.ASSISTANT, "The synthetic post-compaction fixture is complete."),
                (Role.USER, FOLLOWUP),
                (Role.ASSISTANT, SECOND_REPLY),
            ],
        )


@contextmanager
def _app_server(native: NativeClient) -> Iterator[Any]:
    process = subprocess.Popen(
        native.prefix
        + [
            native.executable,
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
            "app-server",
        ],
        env=native.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    responses: queue.Queue[dict[str, Any]] = queue.Queue()

    def read() -> None:
        assert process.stdout
        for line in process.stdout:
            responses.put(json.loads(line))

    thread = threading.Thread(target=read, daemon=True)
    thread.start()

    def call(number: int, method: str, params: dict[str, Any]) -> Any:
        assert process.stdin
        process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        while True:
            value = responses.get(timeout=30)
            if value.get("id") == number:
                assert "error" not in value, value
                return value["result"]

    try:
        call(1, "initialize", {"clientInfo": {"name": "native-gate", "version": "1"}})
        yield call
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        thread.join(timeout=5)


def test_latest_codex_app_server_reads_writer_history(tmp_path: Path) -> None:
    """Read actual native TurnItems: physical provider records alone are insufficient."""
    with offline_provider(SECOND_REPLY) as provider:
        native = NativeClient(tmp_path, "codex", provider)
        source = claude.parse(Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl")
        artifact = convert_session(
            source,
            ConversionOptions(
                target_format=TargetFormat.CODEX,
                session_id=SESSION_ID,
                cwd=Path("/fixture/work"),
                model=MODELS["codex"],
                model_provider="fixture",
            ),
        )
        destination, manifest = target_import_paths(artifact, native.client)
        write_artifact(artifact, output_path=destination, manifest_path=manifest)
        with _app_server(native) as call:
            listing = call(2, "thread/list", {"limit": 100})
            assert SESSION_ID in {thread["id"] for thread in listing["data"]}, listing
            history = call(3, "thread/read", {"threadId": SESSION_ID, "includeTurns": True})
        text = json.dumps(history, ensure_ascii=False)
        for marker in (
            "Continue after the synthetic compaction.",
            "The synthetic post-compaction fixture is complete.",
        ):
            assert text.count(marker) == 1, (marker, history)
        assert not provider.requests, "history reads must not invoke inference"


def test_latest_claude_native_project_directory_override(tmp_path: Path) -> None:
    """Refuse publication before creating any state in the unsupported stock namespace.

    The separate native-probe-claude-namespace.json stock diagnostic retained
    only an atis-latch record despite a successful response, reproduced three
    times. The application must declare this publication scope unsupported.
    """
    home = tmp_path / "synthetic-home"
    destination = tmp_path / "uncreated-native-target"
    request = OperationRequest(
        "import",
        path=Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl",
        target_format=TargetFormat.CLAUDE,
        home=destination,
        session_id=SESSION_ID,
    )
    with pytest.raises(SessionMigrateError, match="custom project namespace is unsupported"):
        prepare_operation(
            request,
            environ={
                "HOME": str(home),
                "CLAUDE_CODE_PROJECT_DIR_NAME": "synthetic-project-override-9217",
            },
        )
    assert not home.exists()
    assert not destination.exists()
