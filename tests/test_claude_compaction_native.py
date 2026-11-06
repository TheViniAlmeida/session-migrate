"""Pinned source/target native replay with a declared offline protocol stub.

Opt in with both SESSION_MIGRATE_TEST_CLAUDE_309_BIN and
SESSION_MIGRATE_TEST_OPENCODE_V22. Hashes are fixed in the helper, never supplied
by an environment override. No vendor account or model inference is used.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest
from native_context_runtime import CLAUDE_SHA256, OPENCODE_SHA256, run_native_context_gate


@pytest.fixture(scope="module")
def native_context_binaries() -> tuple[Path, Path]:
    variables = ("SESSION_MIGRATE_TEST_CLAUDE_309_BIN", "SESSION_MIGRATE_TEST_OPENCODE_V22")
    missing = [name for name in variables if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "native context gate requires both pinned binaries; missing " + ", ".join(missing)
        )
    if sys.platform != "linux" or not shutil.which("bwrap"):
        pytest.skip("native context gate requires Linux bubblewrap with private filesystem/netns")
    if os.geteuid() == 0:
        pytest.skip("normal stock Claude gate requires an unprivileged user; no bypass flags")
    binaries = tuple(Path(os.environ[name]).resolve(strict=True) for name in variables)
    for binary, expected in zip(binaries, (CLAUDE_SHA256, OPENCODE_SHA256), strict=True):
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == expected, binary.name
    return binaries


@pytest.mark.parametrize("protocol", ("chat", "responses"))
@pytest.mark.parametrize("mcp_present", (True, False), ids=("mcp-present", "mcp-absent"))
def test_claude_309_context_cold_native_resume(
    tmp_path: Path,
    native_context_binaries: tuple[Path, Path],
    protocol: str,
    mcp_present: bool,
) -> None:
    claude, opencode = native_context_binaries
    result = run_native_context_gate(tmp_path, claude, opencode, protocol, mcp_present)
    assert result["source_version"] == "2.1.309 (Claude Code)"
    assert result["target_version"] == "opencode v2.2.5"
    assert result["protocol"] == protocol
    assert result["source_provider"] == "loopback Messages protocol stub; no external model"
    assert result["cold_history_preserved"] is True
    assert result["portable_order_and_bodies"] is True
    assert result["historical_tool_preserved"] is True
    assert result["new_mcp_executions"] == int(mcp_present)
    assert result["target_requests"] == (2 if mcp_present else 1)
