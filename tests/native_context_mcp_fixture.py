"""Dependency-free stdio MCP server for the private native context gate.

The only operation is a deterministic string echo. The argument is never treated
as a command or path; the fixed log path belongs to the synthetic test scratch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    log_path = Path(sys.argv[1])
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        with log_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"method": method, "params": request.get("params", {})}) + "\n")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "native-context-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo_fixture",
                        "description": "Echo a synthetic nonce; no filesystem or shell access.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"nonce": {"type": "string"}},
                            "required": ["nonce"],
                            "additionalProperties": False,
                        },
                        "annotations": {"readOnlyHint": True, "destructiveHint": False},
                    }
                ]
            }
        elif method == "tools/call":
            params = request["params"]
            assert params["name"] == "echo_fixture"
            assert params["arguments"] == {"nonce": "NATIVE_CONTEXT_NONCE_309225"}
            result = {
                "content": [{"type": "text", "text": "MCP_RETURN_NATIVE_CONTEXT_NONCE_309225"}],
                "isError": False,
            }
        elif method == "ping":
            result = {}
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                ),
                flush=True,
            )
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
