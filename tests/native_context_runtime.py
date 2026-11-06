"""Private native source/target oracle for the synthetic Claude 309 context contract.

The host only generates the born-synthetic fixture and starts one bubblewrap.
Production modules and both native CLIs execute inside its private root/netns.
Providers are deterministic loopback protocol stubs, not external model calls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

CLAUDE_SHA256 = "4d704a8aecbe038affa3000f498ae17c1b4f753add8facb8764120affc99b658"
OPENCODE_SHA256 = "8d060e1d2fbb9bb44cf09ae6ed1f3f6a7cff070b5227e2f90970132b23a92050"
MODEL = "claude-haiku-4-5-20251001"
NONCE = "NATIVE_CONTEXT_NONCE_309225"
RETURN = "MCP_RETURN_" + NONCE
FOLLOWUP = "NATIVE_CONTEXT_FOLLOWUP_309225"
SOURCE_REPLY = "NATIVE_CONTEXT_SOURCE_REPLY"
HISTORICAL_TOOL = {
    "id": "toolu_closed_fixture",
    "name": "Read",
    "input": {"file": "HISTORICAL_INPUT"},
    "output": "HISTORICAL_RESULT",
}
BODIES = (
    "PERSISTED_RULES_CONTEXT",
    "ORIGINAL_COMPACTION_CONTEXT",
    "KEPT_ASSISTANT_TEXT",
    "HISTORICAL_RESULT",
    "TYPED_METADATA_CONTEXT",
    "FINAL_REPLY_MUST_SURVIVE",
    "FINAL_CONTEXT_A",
    "FINAL_CONTEXT_B",
)
CONTEXT_BODIES = (
    BODIES[0],
    BODIES[1],
    BODIES[4],
    BODIES[6],
    BODIES[7],
)
RETIRED = "RETIRED_HISTORY_MUST_NOT_REPLAY"


def closed_carriers_fixture() -> bytes:
    """Nine original synthetic records: no anonymized/renamed production carriers."""
    ids = {
        name: str(uuid.uuid5(uuid.NAMESPACE_URL, "native-context-" + name))
        for name in (
            "session",
            "retired",
            "kept",
            "result",
            "boundary",
            "instructions",
            "summary",
            "metadata",
            "reply",
            "sections",
        )
    }

    def record(kind: str, name: str, parent: str | None, index: int, **fields: Any) -> dict:
        return {
            "type": kind,
            "uuid": ids[name],
            "parentUuid": ids[parent] if parent else None,
            "sessionId": ids["session"],
            "cwd": "/fixture/work",
            "version": "2.1.309",
            "isSidechain": False,
            "timestamp": f"2026-01-01T00:00:0{index}.000Z",
            **fields,
        }

    rows = [
        record("user", "retired", None, 0, message={"role": "user", "content": RETIRED}),
        record(
            "assistant",
            "kept",
            "retired",
            1,
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": BODIES[2]},
                    {
                        "type": "tool_use",
                        "id": HISTORICAL_TOOL["id"],
                        "name": HISTORICAL_TOOL["name"],
                        "input": HISTORICAL_TOOL["input"],
                    },
                ],
            },
        ),
        record(
            "user",
            "result",
            "kept",
            2,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": HISTORICAL_TOOL["id"],
                        "content": HISTORICAL_TOOL["output"],
                        "is_error": False,
                    },
                ],
            },
        ),
        record(
            "system",
            "boundary",
            None,
            3,
            subtype="compact_boundary",
            logicalParentUuid=ids["metadata"],
            compactMetadata={
                "preservedSegment": {
                    "headUuid": ids["kept"],
                    "tailUuid": ids["result"],
                    "anchorUuid": ids["summary"],
                },
                "preservedMessages": {
                    "anchorUuid": ids["summary"],
                    "uuids": [ids["kept"], ids["result"]],
                    "allUuids": [ids["kept"], ids["result"], ids["metadata"]],
                },
            },
        ),
        record(
            "attachment",
            "instructions",
            "boundary",
            4,
            attachment={
                "type": "instructions",
                "files": [
                    {
                        "path": "/synthetic/CLAUDE.md",
                        "type": "Project",
                        "content": "UNUSED_FORMATTER_TEXT",
                    }
                ],
            },
            rendered=[{"content": BODIES[0]}],
            renderedRole="user",
        ),
        record(
            "user",
            "summary",
            "instructions",
            5,
            isCompactSummary=True,
            message={"role": "user", "content": BODIES[1]},
        ),
        record(
            "user",
            "metadata",
            "result",
            6,
            isMeta=True,
            message={"role": "user", "content": BODIES[4]},
        ),
        record(
            "assistant", "reply", "metadata", 7, message={"role": "assistant", "content": BODIES[5]}
        ),
        record(
            "attachment",
            "sections",
            "reply",
            8,
            attachment={
                "type": "context_sections",
                "sections": [{"name": "fixture", "text": "UNUSED_SECTION_TEXT"}],
            },
            renderedRole="user",
            rendered=[
                {
                    "content": [
                        {"type": "text", "text": BODIES[6]},
                        {"type": "text", "text": BODIES[7]},
                    ]
                }
            ],
        ),
    ]
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode()


def run_native_context_gate(
    scratch: Path,
    claude: Path,
    opencode: Path,
    protocol: str,
    mcp_present: bool,
) -> dict:
    """Start exactly one sandbox; inputs and repository code stay read-only."""
    assert protocol in {"chat", "responses"}
    fixture = scratch / "original.jsonl"
    fixture.write_bytes(closed_carriers_fixture())
    fixture.chmod(0o400)
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    writable = scratch / "private"
    writable.mkdir(mode=0o700)
    repo = Path(__file__).resolve().parents[1]
    command = [
        "bwrap",
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--tmpfs",
        "/",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--dir",
        "/etc",
        "--ro-bind",
        "/etc/ssl",
        "/etc/ssl",
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--bind",
        str(writable),
        "/fixture",
        "--ro-bind",
        str(repo / "src"),
        "/repo/src",
        "--ro-bind",
        str(repo / "tests"),
        "/repo/tests",
        "--ro-bind",
        str(fixture),
        "/input/original.jsonl",
        "--ro-bind",
        str(claude),
        "/stock/claude",
        "--ro-bind",
        str(opencode),
        "/stock/opencode",
        "--chdir",
        "/fixture",
        "--",
        "/usr/bin/python3",
        "-I",
        "-B",
        "/repo/tests/native_context_runtime.py",
        "--worker",
        protocol,
        str(int(mcp_present)),
    ]
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/fixture/home"}
    (scratch / "command.json").write_text(json.dumps(command, indent=2))
    completed = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=180
    )
    (scratch / "worker.stdout").write_text(completed.stdout)
    (scratch / "worker.stderr").write_text(completed.stderr)
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == digest
    assert completed.returncode == 0, completed.stderr[-8000:]
    return json.loads((writable / "result.json").read_text())


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        return value.get("text", "")
    return ""


def _assert_bodies(history: Any) -> None:
    encoded = json.dumps(history, ensure_ascii=False)
    assert RETIRED not in encoded
    assert "UNUSED_FORMATTER_TEXT" not in encoded and "UNUSED_SECTION_TEXT" not in encoded
    positions = [encoded.index(body) for body in BODIES]
    assert positions == sorted(positions), "Portable context order changed"
    assert all(encoded.count(body) == 1 for body in BODIES), "Portable body duplicated"


def _wire_tool(body: dict, protocol: str) -> dict:
    if protocol == "source":
        blocks = [
            part
            for message in body["messages"]
            for part in message.get("content", [])
            if isinstance(part, dict)
        ]
        call = next(part for part in blocks if part.get("type") == "tool_use")
        result = next(part for part in blocks if part.get("type") == "tool_result")
        assert result["tool_use_id"] == call["id"]
        return {
            "id": call["id"],
            "name": call["name"],
            "input": call["input"],
            "output": _text(result["content"]),
        }
    if protocol == "chat":
        call = next(call for message in body["messages"] for call in message.get("tool_calls", []))
        result = next(message for message in body["messages"] if message.get("role") == "tool")
        assert result["tool_call_id"] == call["id"]
        return {
            "id": call["id"],
            "name": call["function"]["name"],
            "input": json.loads(call["function"]["arguments"]),
            "output": _text(result["content"]),
        }
    call = next(item for item in body["input"] if item.get("type") == "function_call")
    result = next(item for item in body["input"] if item.get("type") == "function_call_output")
    assert result["call_id"] == call["call_id"]
    return {
        "id": call["call_id"],
        "name": call["name"],
        "input": json.loads(call["arguments"]),
        "output": _text(result["output"]),
    }


def _chat_reply(handler: BaseHTTPRequestHandler, body: dict, tool: dict | None) -> None:
    if tool:
        delta = {
            "role": "assistant",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_native_context",
                    "type": "function",
                    "function": {"name": tool["name"], "arguments": json.dumps({"nonce": NONCE})},
                }
            ],
        }
    else:
        delta = {"role": "assistant", "content": "NATIVE_CONTEXT_TARGET_REPLY"}
    base = {
        "id": "chatcmpl_context",
        "object": "chat.completion.chunk",
        "created": 1788220000,
        "model": body["model"],
    }
    chunks = [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {
            **base,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        },
    ]
    _sse(handler, ["data: " + json.dumps(chunk) + "\n\n" for chunk in chunks])


def _sse(handler: BaseHTTPRequestHandler, frames: list[str]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(("".join(frames) + "data: [DONE]\n\n").encode())
    handler.wfile.flush()
    handler.close_connection = True


def _responses_reply(handler: BaseHTTPRequestHandler, body: dict, tool: dict | None) -> None:
    if tool:
        arguments = json.dumps({"nonce": NONCE})
        item = {
            "id": "fc_native_context",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_native_context",
            "name": tool["name"],
            "arguments": arguments,
        }
        if tool.get("namespace"):
            item["namespace"] = tool["namespace"]
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": item["id"],
                "output_index": 0,
                "delta": arguments,
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "output_index": 0,
                "arguments": arguments,
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
    else:
        text = "NATIVE_CONTEXT_TARGET_REPLY"
        item = {
            "id": "msg_native_context",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "content": []},
            },
            {
                "type": "response.content_part.added",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            {
                "type": "response.output_text.delta",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
            {
                "type": "response.output_text.done",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
            {
                "type": "response.content_part.done",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "part": item["content"][0],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
    response = {
        "id": "resp_native_context_" + ("call" if tool else "text"),
        "object": "response",
        "created_at": 1788220000,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": body["model"],
        "output": [item],
        "store": False,
        "usage": {
            "input_tokens": 20,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 25,
        },
    }
    events = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        *events,
        {"type": "response.completed", "response": response},
    ]
    _sse(
        handler,
        [
            "event: "
            + event["type"]
            + "\ndata: "
            + json.dumps({**event, "sequence_number": index})
            + "\n\n"
            for index, event in enumerate(events)
        ],
    )


@contextmanager
def _provider(protocol: str, mcp_present: bool = False):
    from offline_provider import OfflineProvider, _Handler

    class Handler(_Handler):
        def do_GET(self) -> None:  # noqa: N802
            if protocol == "source" and self.path.startswith("/v1/models"):
                self._json(
                    {
                        "data": [
                            {
                                "id": MODEL,
                                "type": "model",
                                "display_name": "Fixture",
                                "created_at": "2026-01-01T00:00:00Z",
                            }
                        ],
                        "has_more": False,
                    }
                )
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            self.path = self.path.partition("?")[0]
            if protocol == "source":
                super().do_POST()
                return
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            provider.requests.append((self.path, body))
            try:
                assert self.path == (
                    "/v1/responses" if protocol == "responses" else "/v1/chat/completions"
                )
                turn = len(provider.requests)
                history = body["input" if protocol == "responses" else "messages"]
                if turn == 1:
                    _assert_bodies(history)
                    assert _wire_tool(body, protocol) == HISTORICAL_TOOL
                candidates = []
                for entry in body.get("tools", []):
                    if protocol == "chat":
                        candidate = entry["function"]
                        if candidate["name"].endswith("echo_fixture"):
                            candidates.append(candidate)
                    elif entry.get("type") == "namespace":
                        for candidate in entry["tools"]:
                            if candidate["name"].endswith("echo_fixture"):
                                candidates.append({**candidate, "namespace": entry["name"]})
                    elif entry.get("name", "").endswith("echo_fixture"):
                        candidates.append(entry)
                assert len(candidates) == int(mcp_present)
                assert turn <= (2 if mcp_present else 1), "Unexpected model retry/extra request"
                tool = candidates[0] if mcp_present and turn == 1 else None
                if mcp_present and turn == 2:
                    if protocol == "chat":
                        actual = [
                            m
                            for m in history
                            if m.get("role") == "tool"
                            and m.get("tool_call_id") == "call_native_context"
                        ]
                    else:
                        actual = [
                            m
                            for m in history
                            if m.get("type") == "function_call_output"
                            and m.get("call_id") == "call_native_context"
                        ]
                    assert len(actual) == 1 and RETURN in json.dumps(actual)
                if protocol == "chat":
                    _chat_reply(self, body, tool)
                else:
                    _responses_reply(self, body, tool)
            except Exception as error:
                provider.failures.append(repr(error))
                self.send_error(500, "Synthetic native context assertion failed")

    provider = OfflineProvider(("127.0.0.1", 0), Handler)
    provider.requests, provider.failures, provider.reply = [], [], SOURCE_REPLY
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        yield provider
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=3)


def _environment(root: Path) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "NO_PROXY": "*",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        directory = root / key.lower()
        directory.mkdir(parents=True, mode=0o700)
        env[key] = str(directory)
    return env


def _run(command: list[str], cwd: Path, env: dict[str, str], evidence: Path) -> str:
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=90
    )
    evidence.with_suffix(".stdout").write_text(completed.stdout)
    evidence.with_suffix(".stderr").write_text(completed.stderr)
    assert completed.returncode == 0, (command, completed.stdout, completed.stderr)
    return completed.stdout


def _source_oracle(original: bytes) -> dict:
    root = Path("/fixture/source")
    root.mkdir()
    work = root / "work"
    work.mkdir()
    env = _environment(root)
    client = root / "client"
    client.mkdir()
    env.update(
        {
            "CLAUDE_CONFIG_DIR": str(client),
            "ANTHROPIC_API_KEY": "synthetic-loopback-only",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    rows = [json.loads(line) for line in original.splitlines()]
    session_id = rows[0]["sessionId"]
    # Native Claude namespace follows its actual scratch working directory.
    namespace = str(work).replace("/", "-")
    history = client / "projects" / namespace / f"{session_id}.jsonl"
    history.parent.mkdir(parents=True)
    history.write_bytes(original)
    history.chmod(0o600)
    version = _run(["/stock/claude", "--version"], work, env, root / "version").strip()
    assert version == "2.1.309 (Claude Code)"
    with _provider("source") as provider:
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{provider.server_port}"
        stdout = _run(
            [
                "/stock/claude",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                MODEL,
                "--tools",
                "",
                "--disable-slash-commands",
                "--no-chrome",
                "--resume",
                session_id,
                FOLLOWUP,
            ],
            work,
            env,
            root / "resume",
        )
        (root / "requests.json").write_text(json.dumps(provider.requests, indent=2))
        matching = [body for _, body in provider.requests if FOLLOWUP in json.dumps(body)]
        assert len(matching) == 1
        request = matching[0]
        assert request["model"] == MODEL
        assert not request.get("tools")
        _assert_bodies(request["messages"])
        assert _wire_tool(request, "source") == HISTORICAL_TOOL
        assert SOURCE_REPLY in stdout
        return {"version": version, "request": request}


def _target(original: bytes, protocol: str, mcp_present: bool, source_request: dict) -> dict:
    # Production interfaces are deliberately imported only inside the private worker.
    from session_migrate.conversion import ConversionOptions, convert_session
    from session_migrate.formats import claude
    from session_migrate.formats.context_text import is_marked_synthetic_text
    from session_migrate.model import TargetFormat

    root = Path("/fixture/target")
    root.mkdir()
    project = root / "project"
    project.mkdir()
    env = _environment(root)
    env.update(
        {
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_PRUNE": "true",
            "OPENCODE_SERVER_PASSWORD": "SYNTHETIC_LOCAL_TEST_PASSWORD",
        }
    )
    version = _run(["/stock/opencode", "--version"], project, env, root / "version").strip()
    assert version == "opencode v2.2.5"
    source = claude.parse_bytes(original, Path("/input/original.jsonl"))
    artifact = convert_session(
        source,
        ConversionOptions(
            TargetFormat.OPENCODE,
            session_id="52184584-2102-4eaa-972d-e82dd034169f",
            cwd=project,
            target_cli_version="2.2.5",
            model_provider="fixture",
            model="test",
        ),
    )
    bundle = json.loads(artifact.native_bytes)
    assert not any(message["type"] == "compaction" for message in bundle["messages"])
    contexts = [message for message in bundle["messages"] if message["type"] == "synthetic"]
    assert [message["text"] for message in contexts] == list(CONTEXT_BODIES), contexts
    assert all(is_marked_synthetic_text(message) for message in contexts)
    _assert_bodies(bundle["messages"])
    tools = [
        part
        for message in bundle["messages"]
        for part in message.get("content", [])
        if part.get("type") == "tool"
    ]
    assert len(tools) == 1
    tool = tools[0]
    assert {
        "id": tool["id"],
        "name": tool["name"],
        "input": tool["state"]["input"],
        "output": _text(tool["state"]["content"]),
    } == HISTORICAL_TOOL
    assert tool["state"]["status"] == "completed"
    imported = root / "import.json"
    imported.write_bytes(artifact.native_bytes)
    auth = base64.b64encode(("opencode:" + env["OPENCODE_SERVER_PASSWORD"]).encode()).decode()
    with _provider(protocol, mcp_present) as provider:
        config = {
            "warming": False,
            "snapshots": False,
            "disabled_providers": ["opencode"],
            "providers": {
                "fixture": {
                    "package": "@opencode/ai/providers/openai"
                    if protocol == "responses"
                    else "@opencode/ai/providers/openai-compatible",
                    "settings": {
                        "apiKey": "synthetic-placeholder",
                        "baseURL": f"http://127.0.0.1:{provider.server_port}/v1",
                    },
                    "models": {"test": {"limit": {"context": 64000, "output": 4096}}},
                }
            },
        }
        if mcp_present:
            config["mcp"] = {
                "servers": {
                    "fixture": {
                        "type": "local",
                        "codemode": False,
                        "command": [
                            "/usr/bin/python3",
                            "-I",
                            "-B",
                            "/repo/tests/native_context_mcp_fixture.py",
                            str(root / "mcp.jsonl"),
                        ],
                    }
                }
            }
            config["permissions"] = [
                {"action": "fixture_echo_fixture", "resource": "*", "effect": "allow"}
            ]
        (project / "opencode.json").write_text(json.dumps(config))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        server_log = (root / "server.log").open("w")

        def start() -> subprocess.Popen:
            server = subprocess.Popen(
                [
                    "/stock/opencode",
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=project,
                env=env,
                stdout=server_log,
                stderr=server_log,
            )
            try:
                for _ in range(250):
                    assert server.poll() is None, "Native server exited"
                    try:
                        request = urllib.request.Request(
                            origin + "/api/mcp?directory=" + urllib.parse.quote(str(project)),
                            headers={"Authorization": "Basic " + auth},
                        )
                        with urllib.request.urlopen(request, timeout=1) as response:
                            servers = json.load(response)["data"]
                        if not mcp_present or any(
                            s["name"] == "fixture" and s["status"]["status"] == "connected"
                            for s in servers
                        ):
                            return server
                    except OSError:
                        pass
                    time.sleep(0.05)
                raise AssertionError("MCP discovery did not settle before first model request")
            except BaseException:
                server.terminate()
                server.wait(timeout=10)
                raise

        transport = ["--server", origin]
        counter = 0

        def cli(*arguments: str) -> str:
            nonlocal counter
            label = root / f"cli-{counter}"
            counter += 1
            return _run(["/stock/opencode", *arguments, *transport], project, env, label)

        server = start()
        try:
            cli("session", "import", str(imported), "--directory", str(project))
            before = json.loads(cli("session", "export", artifact.session_id))
            server.terminate()
            server.wait(timeout=10)
            transport = ["--standalone"]
            cold = json.loads(cli("session", "export", artifact.session_id))
            assert cold["messages"] == before["messages"] == bundle["messages"]
            assert not provider.requests
            server = start()
            transport = ["--server", origin]
            cli(
                "run",
                "--session",
                artifact.session_id,
                "--model",
                "fixture/test",
                "--format",
                "json",
                FOLLOWUP,
            )
            server.terminate()
            server.wait(timeout=10)
            transport = ["--standalone"]
            after = json.loads(cli("session", "export", artifact.session_id))
            (root / "cold-before.json").write_text(json.dumps(cold, indent=2))
            (root / "cold-after.json").write_text(json.dumps(after, indent=2))
            assert not provider.failures, provider.failures
            assert len(provider.requests) == (2 if mcp_present else 1)
            first = provider.requests[0][1]
            assert _wire_tool(first, protocol) == _wire_tool(source_request, "source")
            _assert_bodies(first["input" if protocol == "responses" else "messages"])
            existing = {message["id"]: message for message in cold["messages"]}
            reopened = {message["id"]: message for message in after["messages"]}
            assert all(reopened[key] == value for key, value in existing.items())
            new_tools = [
                part
                for message in after["messages"]
                if message["id"] not in existing
                for part in message.get("content", [])
                if part.get("type") == "tool"
            ]
            log = root / "mcp.jsonl"
            events = (
                [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            )
            executions = [event for event in events if event["method"] == "tools/call"]
            assert len(executions) == len(new_tools) == int(mcp_present)
            if mcp_present:
                assert executions[0]["params"]["arguments"] == {"nonce": NONCE}
                assert new_tools[0]["state"]["status"] == "completed"
                assert RETURN in json.dumps(new_tools[0])
            else:
                assert not events
            assert after["info"]["outcome"] == "succeeded"
            return {
                "target_version": version,
                "target_requests": len(provider.requests),
                "new_mcp_executions": len(executions),
            }
        finally:
            (root / "requests.json").write_text(json.dumps(provider.requests, indent=2))
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
            server_log.close()


def _worker(protocol: str, mcp_present: bool) -> None:
    sys.path[:0] = ["/repo/src", "/repo/tests"]
    assert os.geteuid() != 0
    assert {name for _, name in socket.if_nameindex()} == {"lo"}
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    root_mount = next(fields for fields in mounts if fields[4] == "/")
    assert "tmpfs" in root_mount[root_mount.index("-") + 1 :]
    assert not any(Path(path).exists() for path in ("/home", "/storage", "/root", "/run"))
    for name, digest in (("claude", CLAUDE_SHA256), ("opencode", OPENCODE_SHA256)):
        assert hashlib.sha256(Path("/stock", name).read_bytes()).hexdigest() == digest
    Path("/tmp").chmod(0o1777)
    original = Path("/input/original.jsonl").read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    source = _source_oracle(original)
    target = _target(original, protocol, mcp_present, source["request"])
    assert hashlib.sha256(Path("/input/original.jsonl").read_bytes()).hexdigest() == digest
    result = {
        **target,
        "source_version": source["version"],
        "protocol": protocol,
        "source_provider": "loopback Messages protocol stub; no external model",
        "cold_history_preserved": True,
        "portable_order_and_bodies": True,
        "historical_tool_preserved": True,
    }
    Path("/fixture/result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    assert sys.argv[1] == "--worker"
    _worker(sys.argv[2], bool(int(sys.argv[3])))
