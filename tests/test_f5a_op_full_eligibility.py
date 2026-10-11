"""Unproved full OP fidelity is blocked before parent reads or publication."""

from dataclasses import replace

import pytest
from test_f5a_return import return_fixture

from session_migrate import handoff_operations
from session_migrate.handoff_operations import HandoffOperationError, prepare_return_new
from session_migrate.model import AgentFormat
from session_migrate.profiles import Profile


@pytest.mark.parametrize("version", ["2.0.22", "2.0.23"])
def test_full_op_return_rejects_before_parent_read_and_preserves_ledger(
    tmp_path, monkeypatch, version
):
    catalog, observer, request, state_home, env, _, _, _ = return_fixture(tmp_path, "full")
    destination = Profile("op-target", "OP target", AgentFormat.OPENCODE, tmp_path / "opencode")
    request = replace(
        request,
        current_target_profile=destination,
        target_options=replace(request.target_options, target_cli_version=version),
    )
    ledger = state_home / "lineage-v1/ledger.sqlite3"
    before = ledger.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("unproved route reached parent/native preparation")

    monkeypatch.setattr(handoff_operations, "_verified_parent", forbidden)
    try:
        with pytest.raises(HandoffOperationError) as failure:
            prepare_return_new(request, observer, state_home, base_environ=env)
        assert failure.value.code == "target_full_history_unproven"
        assert ledger.read_bytes() == before and not destination.root.exists()
    finally:
        catalog.close()
