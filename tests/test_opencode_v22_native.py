"""Exact stock 2.2.5 gates inside private filesystem and network namespaces."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from native_sandbox import private_root_masks
from test_opencode_v21 import bundle

from session_migrate.formats import opencode

STOCK_SHA256 = "8d060e1d2fbb9bb44cf09ae6ed1f3f6a7cff070b5227e2f90970132b23a92050"


@pytest.fixture
def native(tmp_path):
    selected = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V22")
    if not selected:
        pytest.skip("set SESSION_MIGRATE_TEST_OPENCODE_V22 to the exact stock 2.2.5 ELF")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap is required for the private 2.2.5 native gate")
    binary = Path(selected).resolve()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == STOCK_SHA256
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONPATH": "/tmp/session-migrate-src:/tmp/session-migrate-tests",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
        "OPENCODE_SERVER_PASSWORD": "SYNTHETIC_LOCAL_TEST_PASSWORD",
        "OPENCODE_CONFIG_CONTENT": '{"warming":false,"disabled_providers":["opencode"]}',
    }
    for name in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        folder = tmp_path / name.lower()
        folder.mkdir(mode=0o700)
        env[name] = str(folder)
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    repo = Path(__file__).resolve().parents[1]
    prefix = [
        bwrap,
        "--die-with-parent",
        "--unshare-net",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        *private_root_masks(),
        "--dev",
        "/dev",
        "--bind",
        str(tmp_path),
        str(tmp_path),
        "--ro-bind",
        str(binary),
        "/tmp/opencode-v22",
        "--ro-bind",
        str(repo / "src"),
        "/tmp/session-migrate-src",
        "--ro-bind",
        str(repo / "tests"),
        "/tmp/session-migrate-tests",
        "--proc",
        "/proc",
        "--chdir",
        str(project),
        "--",
    ]

    def run(args, *, worker=False):
        result = subprocess.run(
            prefix
            + (
                [sys.executable, "-c", args, str(tmp_path)]
                if worker
                else ["/tmp/opencode-v22", *args]
            ),
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert max(len(result.stdout), len(result.stderr)) < 4 * 1024 * 1024
        return result.stdout

    assert run(["--version"]).strip() == "opencode v2.2.5"
    return project, run


@pytest.mark.parametrize("private_window", [False, True])
def test_stock_225_cold_transfer_retains_portable_history(native, tmp_path, private_window):
    project, run = native
    value = bundle(project, "2.2.5")
    checkpoint = next(message for message in value["messages"] if message["type"] == "compaction")
    user = next(message for message in value["messages"] if message["type"] == "user")
    if private_window:
        checkpoint["native"] = {"messages": [], "opaque": "PRIVATE_REPLAY_225"}
    else:
        checkpoint["metadata"] = {"keepID": user["id"]}
    source = tmp_path / "import.json"
    source.write_text(json.dumps(value))
    run(["session", "import", str(source), "--directory", str(project), "--standalone"])
    exported = json.loads(run(["session", "export", value["info"]["id"], "--standalone"]))
    assert exported["info"]["title"] == value["info"]["title"]
    actual = next(message for message in exported["messages"] if message["type"] == "compaction")
    assert "recent" not in actual
    assert any(message.get("files") for message in exported["messages"])
    source.write_text(json.dumps(exported))
    session = opencode.parse_session(source)
    written, _ = opencode.serialize(
        session, session_id="ses_reimport225", cwd=project, cli_version="2.2.5"
    )
    assert b"PRIVATE_REPLAY_225" not in written
    if private_window:
        assert any(event.text == user["text"] for event in session.events)
    else:
        assert any(user["text"] in (event.text or "") for event in session.events)
    source.write_bytes(written)
    run(["session", "import", str(source), "--directory", str(project), "--standalone"])
    reopened = json.loads(run(["session", "export", "ses_reimport225", "--standalone"]))
    assert reopened["info"]["id"] == "ses_reimport225"
    assert reopened["messages"]


_SERVER_PROBE = r"""
import base64, json, os, pathlib, socket, subprocess, sys, time, urllib.error, urllib.request
from session_migrate import openchamber
from offline_provider import offline_provider
root = pathlib.Path(sys.argv[1]); project = root / "project"; binary = "/tmp/opencode-v22"
provider_context = offline_provider("CHILD225_RESUMED_MARKER")
provider = provider_context.__enter__()
(project / "opencode.json").write_text(json.dumps({
    "warming":False,"disabled_providers":["opencode"],"providers":{"fixture":{
        "package":"@opencode/ai/providers/openai-compatible",
        "settings":{"apiKey":"fake-only","baseURL":f"http://127.0.0.1:{provider.server_port}/v1"},
        "models":{"test":{"limit":{"context":64000,"output":4096}}}
    }}}))
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
origin = f"http://127.0.0.1:{port}"
auth = base64.b64encode(("opencode:" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()).decode()
def request(route, payload=None):
    req = urllib.request.Request(origin + route,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"content-type":"application/json", "Authorization":"Basic " + auth})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)
def cli(*args):
    result = subprocess.run([binary, *args, "--server", origin], cwd=project,
        env=os.environ, capture_output=True, text=True, timeout=50)
    assert result.returncode == 0, result.stderr
    return result.stdout
def start():
    server = subprocess.Popen([binary,"serve","--hostname","127.0.0.1","--port",str(port)],
        cwd=project, env=os.environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        if server.poll() is not None: raise AssertionError("synthetic server exited")
        try:
            assert request("/api/info")["version"] == "2.2.5"
            return server
        except (OSError, urllib.error.URLError): time.sleep(.05)
    server.terminate(); server.wait(timeout=10)
    raise AssertionError("synthetic server timed out")
server = start()
try:
    missing = json.loads((root / "child.json").read_text())
    missing["info"]["id"] = "ses_missingparent225"
    missing["info"]["parentID"] = "ses_absent225"
    try: request("/api/experimental/session/import",
        {**missing,"location":{"directory":str(project)}})
    except urllib.error.HTTPError as error: assert error.code == 404
    else: raise AssertionError("missing parent import unexpectedly succeeded")
    for name in ["parent", "child"]:
        assert "Imported session:" in cli("session","import",str(root / (name + ".json")),
            "--directory",str(project))
    binding = openchamber.probe_backend(origin, os.environ)
    assert binding["version"] == "2.2.5"
    assert openchamber.session_exists(origin,"ses_child225",os.environ)
    assert openchamber.list_session_ids(origin,os.environ) == {"ses_parent225","ses_child225"}
    all_ids = {item["id"] for item in request("/api/session?limit=10&order=desc")["data"]}
    roots = {item["id"] for item in request("/api/session?parentID=null&limit=10")["data"]}
    children = {item["id"] for item in
        request("/api/session?parentID=ses_parent225&limit=10")["data"]}
    assert all_ids == {"ses_parent225","ses_child225"}
    assert roots == {"ses_parent225"} and children == {"ses_child225"}
    messages = request("/api/session/ses_child225/message?limit=100")
    assert messages["data"]
    exported = json.loads(cli("session","export","ses_child225"))
    assert exported["info"]["parentID"] == "ses_parent225"
    assert exported["messages"]
finally:
    server.terminate(); server.wait(timeout=10)
server = start()
try:
    assert request("/api/session/ses_child225")["data"]["parentID"] == "ses_parent225"
    before = json.loads(cli("session","export","ses_child225"))
    assert before["info"]["title"] == "child persistent fixture 225"
    cli("run","--session","ses_child225","--model","fixture/test","--format","json",
        "Continue this persistent child independently")
    after = json.loads(cli("session","export","ses_child225"))
    assert after["info"]["parentID"] == "ses_parent225"
    assert after["info"]["title"] == before["info"]["title"]
    assert len(after["messages"]) > len(before["messages"])
    assert any(content.get("text") == provider.reply for message in after["messages"]
        if message["type"] == "assistant" for content in message["content"])
    assert provider.requests
    replay = json.dumps(provider.requests)
    assert "CHILD225_HISTORY_MARKER" in replay
    assert "PARENT225_HISTORY_MARKER" not in replay
    print(json.dumps({"version":"2.2.5","cold_parent_id":True,"api_list":True,
        "api_message":True,"backend_identity":True,"cli_server_import_export":True,
        "child_resumable":True,"child_history_independent":True}))
finally:
    server.terminate(); server.wait(timeout=10)
    provider_context.__exit__(None,None,None)
"""


def test_stock_225_server_import_api_and_persistent_parent(native, tmp_path):
    project, run = native
    for name in ("parent", "child"):
        value = bundle(project, "2.2.5")
        value["info"]["id"] = f"ses_{name}225"
        value["info"]["title"] = f"{name} persistent fixture 225"
        value["messages"] = [
            message for message in value["messages"] if message["type"] != "compaction"
        ]
        next(message for message in value["messages"] if message["type"] == "user")["text"] = (
            f"{name.upper()}225_HISTORY_MARKER"
        )
        if name == "child":
            value["info"]["parentID"] = "ses_parent225"
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    result = json.loads(run(_SERVER_PROBE, worker=True))
    assert result["version"] == "2.2.5"
    assert all(value for key, value in result.items() if key != "version")
