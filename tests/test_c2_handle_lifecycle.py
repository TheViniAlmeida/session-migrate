"""Actual B3/C1 capabilities expire without owning their public DTOs."""

import copy
import gc
import hashlib
import json
import os
import weakref
from dataclasses import FrozenInstanceError, replace

import pytest
import test_c2_services as fixtures

from session_migrate import adoption, reconciliation
from session_migrate.baselines import BaselineStore
from session_migrate.lineage import LineageStore, NativeState, OperationState


def setup_service(tmp_path, monkeypatch, kind):
    if kind == "adoption":
        pair, observer, home, catalog, _ = fixtures.adoption_fixture(tmp_path)

        def prepare():
            return fixtures.plan_adoption(pair, observer, home)

        return adoption, prepare, home, catalog
    data = (
        fixtures.profile_recovery_fixture(tmp_path, monkeypatch)
        if kind == "profile"
        else fixtures.recovery_fixture(tmp_path, monkeypatch)
    )
    operation_id, path, context, observer, catalog = data

    def prepare():
        return reconciliation.prepare_reconciliation(
            operation_id, store_path=path, context=context, observer=observer
        )

    return reconciliation, prepare, path, catalog


def commit(module, handle):
    return (
        adoption.commit_adoption(handle)
        if module is adoption
        else reconciliation.commit_reconciliation(handle)
    )


def registry_ids(module):
    return {id(handle) for handle in module._HANDLES}


def file_hashes(tmp_path):
    return {
        str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("kind", ("portable", "profile", "adoption"))
def test_more_than_capacity_abandoned_actual_proposals_release_registry_and_state(
    tmp_path, monkeypatch, kind
):
    module, prepare, _, catalog = setup_service(tmp_path, monkeypatch, kind)
    fixtures.no_native(monkeypatch)
    gc.collect()
    unrelated = registry_ids(module)
    previous = file_hashes(tmp_path)
    descriptors = len(os.listdir("/proc/self/fd"))
    for _ in range(module._MAX_HANDLES + 2):
        proposal = prepare()
        assert proposal.status == "ready", proposal.reason
        handle = proposal.handle
        state = module._HANDLES[handle]
        assert state.proposal is not proposal and state.proposal.handle is None
        assert state.proposal.to_dict() == proposal.to_dict()
        refs = tuple(weakref.ref(value) for value in (proposal, handle, state, state.proposal))
        del state, handle, proposal
        gc.collect()
        assert all(ref() is None for ref in refs)
        assert registry_ids(module) <= unrelated
    assert file_hashes(tmp_path) == previous
    assert len(os.listdir("/proc/self/fd")) == descriptors
    catalog.close()


@pytest.mark.parametrize("kind", ("portable", "profile", "adoption"))
def test_dropping_last_handle_releases_its_private_observer_and_proof_dependencies(
    tmp_path, monkeypatch, kind
):
    module, prepare, _, catalog = setup_service(tmp_path, monkeypatch, kind)
    proposal = prepare()
    assert proposal.status == "ready", proposal.reason
    handle = proposal.handle
    state = module._HANDLES[handle]
    refs = tuple(
        weakref.ref(value)
        for value in (
            proposal,
            handle,
            state,
            state.proposal,
            state.observer,
        )
    )
    catalog.close()
    del prepare, catalog, state, handle, proposal
    gc.collect()
    assert all(ref() is None for ref in refs)


@pytest.mark.parametrize("kind", ("portable", "adoption"))
def test_capacity_is_reserved_only_by_live_capabilities_and_recovers_after_drop(
    tmp_path, monkeypatch, kind
):
    module, prepare, _, catalog = setup_service(tmp_path, monkeypatch, kind)
    fixtures.no_native(monkeypatch)
    gc.collect()
    unrelated = registry_ids(module)
    previous = file_hashes(tmp_path)
    # Respect unrelated capabilities owned by the surrounding interpreter.
    remaining = module._MAX_HANDLES - len(unrelated)
    proposals = [prepare() for _ in range(remaining)]
    assert all(p.status == "ready" for p in proposals)
    refs = [weakref.ref(p.handle) for p in proposals]
    assert len(module._HANDLES) == module._MAX_HANDLES
    blocked = prepare()
    assert blocked.reason == "handle_capacity" and blocked.handle is None
    assert file_hashes(tmp_path) == previous
    del proposals
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert registry_ids(module) <= unrelated
    recovered = prepare()
    assert recovered.status == "ready", recovered.reason
    recovered.handle.discard()
    assert file_hashes(tmp_path) == previous
    catalog.close()


@pytest.mark.parametrize("kind", ("portable", "profile", "adoption"))
@pytest.mark.parametrize("action", ("commit", "discard", "handle_only_commit"))
def test_actual_live_capability_is_consumed_once_with_or_without_public_dto(
    tmp_path, monkeypatch, kind, action
):
    module, prepare, path, catalog = setup_service(tmp_path, monkeypatch, kind)
    proposal = prepare()
    assert proposal.status == "ready", proposal.reason
    handle = proposal.handle
    proposal_ref = weakref.ref(proposal)
    previous = file_hashes(tmp_path)
    fixtures.no_native(monkeypatch)
    if action == "handle_only_commit":
        del proposal
        gc.collect()
        assert proposal_ref() is None and handle in module._HANDLES
    if action == "discard":
        handle.discard()
        assert file_hashes(tmp_path) == previous
    else:
        receipt = commit(module, handle)
        if module is reconciliation:
            reopened = LineageStore.inspect_existing(path).operations[0]
            assert reopened.durable_state is OperationState.COMPLETED
            assert reopened.durable_native_state is NativeState.ACKNOWLEDGED
            if kind == "portable":
                assert receipt.continuity_reason == "identity_untracked"
                assert reopened.source_checkpoint.identity is None
        else:
            assert adoption.inspect_adoption(receipt.baseline_id, state_home=path) == receipt
            assert not LineageStore.inspect_existing(path / "lineage-v1").operations
    assert handle not in module._HANDLES
    with pytest.raises(
        module.ReconciliationError if module is reconciliation else module.AdoptionError
    ):
        commit(module, handle)
    handle.discard()
    catalog.close()


@pytest.mark.parametrize("kind", ("portable", "adoption"))
@pytest.mark.parametrize("tamper", ("metadata", "handle_clone", "binding", "public_handle"))
def test_public_metadata_and_handle_tamper_cannot_authorize_private_copy(
    tmp_path, monkeypatch, kind, tamper
):
    module, prepare, _, catalog = setup_service(tmp_path, monkeypatch, kind)
    proposal = prepare()
    assert proposal.status == "ready", proposal.reason
    handle = proposal.handle
    previous = file_hashes(tmp_path)
    if tamper == "metadata":
        object.__setattr__(proposal, "status", "blocked")
    elif tamper == "handle_clone":
        handle = copy.copy(handle)
    elif tamper == "binding":
        object.__setattr__(handle, "_binding", "0" * 64)
    else:
        object.__setattr__(proposal, "handle", None)
    error = module.ReconciliationError if module is reconciliation else module.AdoptionError
    with pytest.raises(error, match="invalid_handle"):
        commit(module, handle)
    assert file_hashes(tmp_path) == previous
    if tamper == "handle_clone":
        proposal.handle.discard()
    catalog.close()


@pytest.mark.parametrize("kind", ("portable", "adoption"))
def test_public_proposal_wire_metadata_remains_handle_free_and_frozen(tmp_path, monkeypatch, kind):
    module, prepare, _, catalog = setup_service(tmp_path, monkeypatch, kind)
    proposal = prepare()
    assert proposal.status == "ready", proposal.reason
    metadata = proposal.to_dict()
    private = module._HANDLES[proposal.handle].proposal
    assert private.to_dict() == metadata and private.handle is None
    assert replace(proposal, handle=None).to_dict() == metadata
    assert "handle" not in metadata and "public_proposal" not in metadata
    assert "synthetic request" not in json.dumps(metadata)
    assert "synthetic" not in repr(proposal) and "synthetic" not in repr(proposal.handle)
    with pytest.raises(FrozenInstanceError):
        proposal.status = "blocked"
    proposal.handle.discard()
    catalog.close()


@pytest.mark.parametrize("kind", ("portable", "adoption"))
def test_public_metadata_is_rechecked_during_actual_persistence_io(tmp_path, monkeypatch, kind):
    module, prepare, path, catalog = setup_service(tmp_path, monkeypatch, kind)
    if module is adoption:
        commit(module, prepare().handle)
    proposal = prepare()
    assert proposal.status == "ready", proposal.reason
    image = path / ("lineage-v1/ledger.sqlite3" if module is adoption else "ledger.sqlite3")
    previous = image.read_bytes()
    baseline_previous = (
        (path / "baselines-v1/ledger.sqlite3").read_bytes() if module is adoption else None
    )
    owner = LineageStore if module is reconciliation else BaselineStore
    name = "complete_reconciled" if module is reconciliation else "append"
    original = getattr(owner, name)
    attacked = False

    def mutate(store, *args, **kwargs):
        def fault(event):
            nonlocal attacked
            if event == "before_rename" and not attacked:
                attacked = True
                object.__setattr__(proposal, "available_effects", ())

        store._fault_hook = fault
        return original(store, *args, **kwargs)

    monkeypatch.setattr(owner, name, mutate)
    error = module.ReconciliationError if module is reconciliation else module.AdoptionError
    with pytest.raises(error):
        commit(module, proposal.handle)
    assert attacked and image.read_bytes() == previous
    if module is adoption:
        assert (path / "baselines-v1/ledger.sqlite3").read_bytes() == baseline_previous
    if module is reconciliation:
        assert (
            LineageStore.inspect_existing(path).operations[0].durable_state
            is OperationState.NEEDS_RECONCILE
        )
    catalog.close()
