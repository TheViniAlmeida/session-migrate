"""Handoff uses real issued C1 captures and literal reviewed USER content."""

import copy
import gc
import hashlib
import json
import os
import weakref
from dataclasses import replace
from pathlib import Path

import pytest
from test_continuity import SOURCE_ID, actual_b3_fixture

from session_migrate import handoff
from session_migrate.canonical import CanonicalPolicy
from session_migrate.catalog import Catalog
from session_migrate.continuity_observation import (
    EndpointObserver,
    _original_authority,
    observe_endpoint,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.handoff_types import (
    HANDOFF_PREFIX,
    MAX_DOCUMENT_BYTES,
    DerivedUserHandoffInput,
    HandoffError,
    HandoffLimits,
    OriginalNativeAuthority,
    _authority_state,
    _issue_user_input,
    validate_derived_input,
)
from session_migrate.lineage import SelectionRangeV1
from session_migrate.model import AgentFormat, EventKind, Role
from session_migrate.profiles import Profile, SessionRef

ID = "61111111-1111-4111-8111-111111111111"


def source_fixture(tmp_path):
    tmp_path.chmod(0o700)
    root = tmp_path / "source"
    source = (
        root / "projects" / claude.project_directory_name(Path("/synthetic/work")) / (ID + ".jsonl")
    )
    source.parent.mkdir(parents=True)
    records = [
        {
            "type": role,
            "uuid": str(i),
            "parentUuid": str(i - 1) if i else None,
            "sessionId": ID,
            "cwd": "/synthetic/work",
            "version": "2.1.209",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        for i, (role, text) in enumerate(
            (("user", "literal synthetic request"), ("assistant", "literal synthetic answer"))
        )
    ]
    source.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in records))
    source.chmod(0o600)
    catalog = Catalog(tmp_path / "catalog/state.sqlite")
    catalog.refresh(claude_roots=(root,), include_auto=False)
    profile = Profile("source", "Source", AgentFormat.CLAUDE, root)
    ref = SessionRef(
        profile.format, profile.root, ID, profile.id, profile_fingerprint=profile.fingerprint
    )
    observer = EndpointObserver(catalog, (profile,))
    endpoint = observe_endpoint(
        observer, ref, client_version="2.1.209", canonical_policy=CanonicalPolicy()
    )
    return source, catalog, observer, ref, endpoint


def test_original_authority_borrows_actual_issued_snapshot_and_full_native_session(tmp_path):
    source, catalog, observer, ref, endpoint = source_fixture(tmp_path)
    authority = _original_authority(endpoint, observer, ref)
    authority.check()
    assert authority.snapshot.authority_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert authority.identity == endpoint.identity
    assert authority.session.source_path == source
    assert authority.session.session_id == ID
    assert authority.versions.source_client == "2.1.209"
    with pytest.raises(HandoffError, match="invalid_handle"):
        OriginalNativeAuthority()
    with pytest.raises(HandoffError, match="invalid_handle"):
        copy.copy(authority).check()
    catalog.close()


def test_original_authority_reobserves_changed_native_bytes_and_blocks(tmp_path):
    source, catalog, observer, ref, endpoint = source_fixture(tmp_path)
    authority = _original_authority(endpoint, observer, ref)
    old = source.stat()
    source.write_bytes(source.read_bytes().replace(b"synthetic answer", b"synthetic broken"))
    import os

    os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(HandoffError, match="source_changed"):
        authority.check()
    catalog.close()


def test_actual_b3_closed_catalog_is_fixed_source_failure_and_consumes_reviewed(tmp_path):
    catalog, observer, _, operation, _, profiles, _ = actual_b3_fixture(tmp_path, "profiles")
    try:
        profile = profiles[0]
        ref = SessionRef(
            profile.format,
            profile.root,
            SOURCE_ID,
            profile.id,
            profile_fingerprint=profile.fingerprint,
        )
        draft = handoff.prepare_handoff(
            ref,
            (SelectionRangeV1(0, 1, None),),
            HandoffLimits(),
            observer,
            client_version="2.1.209",
        )
        assert (
            handoff.handoff_metadata(draft)["source_authority_sha256"]
            == operation.record.source.artifact_sha256
        )
        accepted = reviewed(draft)
        catalog.close()
        with pytest.raises(HandoffError, match="^Handoff: source_changed\\.$") as error:
            handoff._consume_reviewed(accepted)
        assert error.value.code == "source_changed"
        with pytest.raises(HandoffError, match="invalid_handle"):
            handoff._consume_reviewed(accepted)
    finally:
        catalog.close()


def test_derived_user_input_is_not_a_native_source_and_preserves_literal_bytes():
    body = b"Objective: unknown\n<script>alert('literal')</script>\x1b[31m"
    derived = _issue_user_input(body)
    validate_derived_input(derived)
    assert derived.document_bytes == body
    assert derived.identity.document_sha256 == hashlib.sha256(body).hexdigest()
    assert derived.identity.document_sha256 != derived.identity.event_sha256
    event = derived.events[0]
    assert event.kind is EventKind.MESSAGE and event.role is Role.USER
    assert event.text == HANDOFF_PREFIX + "\n\n" + body.decode()
    assert event.provenance.source_id is None and event.timestamp is None
    assert event.tool_name is event.tool_call_id is None and event.payload == {}
    assert not hasattr(derived, "source_format") and not hasattr(derived, "source_path")
    assert "Objective" not in repr(derived)
    with pytest.raises(HandoffError, match="invalid_handle"):
        DerivedUserHandoffInput()
    with pytest.raises(HandoffError):
        validate_derived_input(copy.copy(derived))


def test_derived_input_mutation_is_not_a_valid_user_event():
    derived = _issue_user_input(b"accepted body")
    derived.event.payload["system_prompt"] = "not authority"
    with pytest.raises(HandoffError):
        validate_derived_input(derived)


def draft_fixture(tmp_path):
    source, catalog, observer, ref, _ = source_fixture(tmp_path)
    draft = handoff.prepare_handoff(
        ref,
        (SelectionRangeV1(0, 1, None),),
        HandoffLimits(),
        observer,
        client_version="2.1.209",
    )
    return source, catalog, observer, ref, draft


def reviewed(draft, body=b"User reviewed next step: inspect fixture"):
    return handoff.accept_user_text(draft, body, confirmed_sha256=hashlib.sha256(body).hexdigest())


def test_offline_draft_has_explicit_unknowns_literal_selection_and_one_shot_review(tmp_path):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    body = handoff.render_handoff(draft)
    assert "Objective: unknown" in body and "Decisions: unknown" in body
    assert "Verifications: unknown" in body and "Next step: unknown" in body
    assert "literal synthetic request" in body and "literal synthetic answer" in body
    metadata = handoff.handoff_metadata(draft)
    assert "literal" not in json.dumps(metadata) and "document" not in metadata
    accepted = reviewed(draft, b"<html>literal user edit</html>\x1b[31m")
    with pytest.raises(HandoffError, match="invalid_handle"):
        handoff.render_handoff(draft)
    authority, derived, transformation = handoff._consume_reviewed(accepted)
    authority.check()
    assert derived.document_bytes == b"<html>literal user edit</html>\x1b[31m"
    assert transformation.input == derived.identity and transformation.provenance == "user_supplied"
    with pytest.raises(HandoffError, match="invalid_handle"):
        handoff._consume_reviewed(accepted)
    catalog.close()


@pytest.mark.parametrize("change", ("range", "overlap", "source_bytes", "excerpt", "events", "doc"))
def test_selection_and_bounds_fail_without_truncation(tmp_path, change):
    _, catalog, observer, ref, _ = source_fixture(tmp_path)
    selection = (SelectionRangeV1(0, 1, None),)
    limits = HandoffLimits()
    if change == "range":
        selection = (SelectionRangeV1(0, 2, None),)
    elif change == "overlap":
        selection *= 2
    else:
        key = {
            "source_bytes": "max_source_bytes",
            "excerpt": "max_excerpt_bytes",
            "events": "max_events",
            "doc": "max_document_bytes",
        }[change]
        limits = replace(limits, **{key: 1})
    with pytest.raises(SessionMigrateError):
        handoff.prepare_handoff(ref, selection, limits, observer, client_version="2.1.209")
    catalog.close()


def test_export_edit_digest_mode_and_successful_review(tmp_path):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    folder.mkdir(mode=0o700)
    path = folder / "handoff.md"
    expected = handoff.render_handoff(draft).encode()
    exported = handoff.export_handoff(draft, path)
    assert path.read_bytes() == expected and path.stat().st_mode & 0o777 == 0o600
    user_bytes = b"Objective: supplied by user\nNext: user supplied"
    path.write_bytes(user_bytes)
    result = handoff.accept_edit(exported, confirmed_sha256=hashlib.sha256(user_bytes).hexdigest())
    _, derived, transformation = handoff._consume_reviewed(result)
    assert derived.document_bytes == user_bytes and transformation.provenance == "user_supplied"
    assert path.read_bytes() == user_bytes
    catalog.close()


@pytest.mark.parametrize("change", ("digest", "mode", "link", "symlink", "replace", "parent"))
def test_edit_guard_failure_consumes_handle_and_preserves_user_files(tmp_path, change):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    folder.mkdir(mode=0o700)
    path = folder / "handoff.md"
    exported = handoff.export_handoff(draft, path)
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if change == "digest":
        digest = "0" * 64
    elif change == "mode":
        path.chmod(0o644)
    elif change == "link":
        os.link(path, folder / "hardlink.md")
    elif change in {"symlink", "replace"}:
        path.rename(folder / "original.md")
        if change == "symlink":
            path.symlink_to(folder / "original.md")
        else:
            path.write_bytes(body)
            path.chmod(0o600)
    else:
        folder.rename(tmp_path / "moved")
        folder.mkdir(mode=0o700)
        path.write_bytes(body)
        path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(HandoffError):
        handoff.accept_edit(exported, confirmed_sha256=digest)
    assert path.read_bytes() == before
    with pytest.raises(HandoffError, match="invalid_handle"):
        handoff.accept_edit(exported, confirmed_sha256=digest)
    catalog.close()


@pytest.mark.parametrize("mutation", ("none", "user_edit", "replacement"))
def test_failed_export_cleanup_removes_only_unchanged_own_writing(tmp_path, monkeypatch, mutation):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    folder.mkdir(mode=0o700)
    path = folder / "handoff.md"

    def fault(event):
        if event == "after_export_write":
            if mutation == "user_edit":
                path.write_bytes(b"USER EDIT MUST SURVIVE")
            elif mutation == "replacement":
                path.rename(folder / "owned.md")
                path.write_bytes(b"USER REPLACEMENT MUST SURVIVE")
                path.chmod(0o600)
            raise OSError("private failure")

    monkeypatch.setattr(handoff, "_FAULT_HOOK", fault)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(HandoffError, match="export_failed"):
        handoff.export_handoff(draft, path)
    assert len(os.listdir("/proc/self/fd")) == before
    assert path.exists() == (mutation != "none")
    if mutation != "none":
        assert path.read_bytes().startswith(b"USER")
    catalog.close()


def test_mutation_during_edit_read_blocks_and_does_not_delete_document(tmp_path, monkeypatch):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    folder.mkdir(mode=0o700)
    path = folder / "handoff.md"
    exported = handoff.export_handoff(draft, path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    changed = False

    def mutate(event):
        nonlocal changed
        if event == "after_edit_read" and not changed:
            changed = True
            path.write_bytes(b"USER CHANGED DURING READ")

    monkeypatch.setattr(handoff, "_FAULT_HOOK", mutate)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(HandoffError):
        handoff.accept_edit(exported, confirmed_sha256=expected)
    assert changed and path.read_bytes() == b"USER CHANGED DURING READ"
    assert len(os.listdir("/proc/self/fd")) == before
    catalog.close()


@pytest.mark.parametrize("action", ("render", "accept", "export"))
def test_native_source_freshness_remains_authority_after_draft(tmp_path, action):
    source, catalog, _, _, draft = draft_fixture(tmp_path)
    source.write_bytes(source.read_bytes().replace(b"synthetic answer", b"synthetic broken"))
    with pytest.raises(HandoffError):
        if action == "render":
            handoff.render_handoff(draft)
        elif action == "accept":
            reviewed(draft)
        else:
            handoff.export_handoff(draft, tmp_path / "doc.md")
    catalog.close()


def submission(draft, *, credential_env_ref=None):
    return handoff.prepare_submission(
        reviewed(draft),
        model_id="fake-local-v1",
        max_input_bytes=1024,
        max_output_bytes=1024,
        timeout_ns=10_000_000_000,
        budget_units=3,
        credential_env_ref=credential_env_ref,
    )


@pytest.mark.parametrize(
    "failure",
    ("consent", "model", "budget", "timeout", "failure", "cancel", "oversize", "external"),
)
def test_fake_submission_requires_explicit_consent_and_never_retries(tmp_path, failure):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    pending = submission(draft, credential_env_ref="TEST_PROVIDER_TOKEN")
    state = handoff._HANDLES[pending]
    consent = handoff.SubmissionConsent(state.request.input_sha256, "fake-local-v1", 1024, 3, True)
    provider = handoff.FakeHandoffProvider(b"model response requires review")
    if failure == "consent":
        consent = replace(consent, approved=False)
    elif failure == "model":
        consent = replace(consent, model_id="wrong-model")
    elif failure == "budget":
        consent = replace(consent, budget_units=9)
    elif failure == "timeout":
        provider = replace(provider, latency_ns=20_000_000_000)
    elif failure == "failure":
        provider = replace(provider, fail=True)
    elif failure == "oversize":
        provider = replace(provider, response=b"x" * 1025)
    elif failure == "external":

        class External:
            def generate(self, request):
                pytest.fail("external provider called")

        provider = External()
    assert "model response" not in repr(provider)
    assert "User reviewed" not in repr(state.request)
    with pytest.raises(HandoffError):
        handoff.submit(pending, provider, consent, cancelled=failure == "cancel")
    with pytest.raises(HandoffError, match="invalid_handle"):
        handoff.submit(pending, provider, consent)
    catalog.close()


def test_fake_response_requires_new_user_review_and_has_distinct_provenance(tmp_path):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    pending = submission(draft)
    request = handoff._HANDLES[pending].request
    consent = handoff.SubmissionConsent(request.input_sha256, request.model_id, 1024, 3, True)
    response = b"Objective: unknown\nNext: user should review"
    next_draft = handoff.submit(pending, handoff.FakeHandoffProvider(response), consent)
    assert handoff.render_handoff(next_draft).encode() == response
    accepted = reviewed(next_draft, response)
    _, derived, transformation = handoff._consume_reviewed(accepted)
    assert derived.document_bytes == response and transformation.provenance == "fake_model_reviewed"
    catalog.close()


def test_handoff_handles_gc_and_live_capacity_have_no_strong_backedge(tmp_path):
    _, catalog, observer, ref, _ = source_fixture(tmp_path)
    selection = (SelectionRangeV1(0, 0, None),)
    existing = {id(handle) for handle in handoff._HANDLES}
    for _ in range(66):
        draft = handoff.prepare_handoff(
            ref, selection, HandoffLimits(), observer, client_version="2.1.209"
        )
        weak = weakref.ref(draft)
        del draft
        gc.collect()
        assert weak() is None and {id(handle) for handle in handoff._HANDLES} <= existing
    live = [
        handoff.prepare_handoff(ref, selection, HandoffLimits(), observer, client_version="2.1.209")
        for _ in range(handoff._MAX_HANDLES - len(handoff._HANDLES))
    ]
    with pytest.raises(HandoffError, match="handle_capacity"):
        handoff.prepare_handoff(ref, selection, HandoffLimits(), observer, client_version="2.1.209")
    for draft in live:
        draft.discard()
    catalog.close()


@pytest.mark.parametrize("change", ("existing", "missing_parent", "public_parent", "ancestor_link"))
def test_export_refuses_preexisting_or_unsafe_namespace_without_creation(tmp_path, change):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    if change != "missing_parent":
        folder.mkdir(mode=0o700)
    path = folder / "handoff.md"
    if change == "existing":
        path.write_bytes(b"PREEXISTING USER FILE")
        path.chmod(0o600)
    elif change == "public_parent":
        folder.chmod(0o755)
    elif change == "ancestor_link":
        linked = tmp_path / "linked"
        linked.symlink_to(folder, target_is_directory=True)
        path = linked / "handoff.md"
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(HandoffError, match="export_failed"):
        handoff.export_handoff(draft, path)
    assert len(os.listdir("/proc/self/fd")) == before
    if change == "existing":
        assert path.read_bytes() == b"PREEXISTING USER FILE"
    else:
        assert not path.exists()
    assert folder.exists() == (change != "missing_parent")
    with pytest.raises(HandoffError, match="invalid_handle"):
        handoff.export_handoff(draft, path)
    catalog.close()


def test_export_preserves_user_edit_before_initial_owned_witness(tmp_path, monkeypatch):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    folder.mkdir(mode=0o700)
    path = folder / "handoff.md"

    def edit(event):
        if event == "after_export_open":
            path.write_bytes(b"EARLY USER EDIT")

    monkeypatch.setattr(handoff, "_FAULT_HOOK", edit)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(HandoffError, match="export_failed"):
        handoff.export_handoff(draft, path)
    assert path.read_bytes() == b"EARLY USER EDIT"
    assert len(os.listdir("/proc/self/fd")) == before
    catalog.close()


@pytest.mark.parametrize("stage", ("after_export_file_fsync", "after_export_directory_fsync"))
@pytest.mark.parametrize("change", ("source", "metadata", "parent", "handle", "closed_fd"))
def test_export_post_fsync_guards_close_descriptors_and_preserve_changed_files(
    tmp_path, monkeypatch, stage, change
):
    source, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    folder.mkdir(mode=0o700)
    path = folder / "handoff.md"
    expected = handoff.render_handoff(draft).encode()

    def fault(event):
        if event != stage:
            return
        if change == "source":
            before = source.stat()
            source.write_bytes(
                source.read_bytes().replace(b"synthetic answer", b"synthetic broken")
            )
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif change == "metadata":
            path.chmod(0o640)
        elif change == "parent":
            folder.rename(tmp_path / "moved")
            folder.mkdir(mode=0o700)
            path.write_bytes(b"NEW USER NAMESPACE")
            path.chmod(0o600)
        elif change == "handle":
            draft._seal = object()
        else:
            descriptors = []
            for name in os.listdir("/proc/self/fd"):
                try:
                    if os.readlink("/proc/self/fd/" + name) == str(path):
                        descriptors.append(int(name))
                except FileNotFoundError:
                    pass
            assert len(descriptors) == 1
            os.close(descriptors[0])

    monkeypatch.setattr(handoff, "_FAULT_HOOK", fault)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(HandoffError):
        handoff.export_handoff(draft, path)
    assert len(os.listdir("/proc/self/fd")) == before
    if change in {"source", "handle"}:
        assert not path.exists()
    elif change == "parent":
        assert path.read_bytes() == b"NEW USER NAMESPACE"
        assert (tmp_path / "moved" / path.name).read_bytes() == expected
    else:
        assert path.read_bytes() == expected
    catalog.close()


@pytest.mark.parametrize("body", (b"", b"\xff", b"x" * (MAX_DOCUMENT_BYTES + 1)))
def test_invalid_reviewed_document_consumes_handle_without_input(tmp_path, body):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    with pytest.raises(HandoffError):
        reviewed(draft, body)
    with pytest.raises(HandoffError, match="invalid_handle"):
        reviewed(draft, b"retry is not authorized")
    catalog.close()


def test_maximum_document_is_integral_and_prefix_is_outside_document_digest(tmp_path):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    body = b"u" * MAX_DOCUMENT_BYTES
    accepted = reviewed(draft, body)
    _, derived, _ = handoff._consume_reviewed(accepted)
    assert derived.document_bytes == body
    assert derived.identity.document_sha256 == hashlib.sha256(body).hexdigest()
    assert derived.event.text.encode() == HANDOFF_PREFIX.encode() + b"\n\n" + body
    catalog.close()


@pytest.mark.parametrize(
    "selection",
    ((SelectionRangeV1(0, 0, 1),), (SelectionRangeV1(0, 0, None), SelectionRangeV1(0, 0, 0))),
)
def test_block_selection_rejects_absent_or_overlapping_blocks(tmp_path, selection):
    _, catalog, observer, ref, _ = source_fixture(tmp_path)
    with pytest.raises(HandoffError, match="selection_invalid"):
        handoff.prepare_handoff(ref, selection, HandoffLimits(), observer, client_version="2.1.209")
    catalog.close()


def test_literal_block_selection_records_exact_omitted_event_count(tmp_path):
    _, catalog, observer, ref, _ = source_fixture(tmp_path)
    draft = handoff.prepare_handoff(
        ref, (SelectionRangeV1(0, 0, 0),), HandoffLimits(), observer, client_version="2.1.209"
    )
    assert "literal synthetic request" in handoff.render_handoff(draft)
    assert "literal synthetic answer" not in handoff.render_handoff(draft)
    assert handoff.handoff_metadata(draft)["omissions"] == [{"kind": "message", "count": 1}]
    draft.discard()
    catalog.close()


@pytest.mark.parametrize("field", ("_kind", "_binding", "_seal"))
@pytest.mark.parametrize("mutation", ("change", "delete"))
def test_cloned_and_mutated_handles_cannot_be_used_as_review_authority(tmp_path, field, mutation):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    with pytest.raises(HandoffError, match="invalid_handle"):
        reviewed(copy.copy(draft))
    original = getattr(draft, field)
    if mutation == "change":
        setattr(draft, field, object())
    else:
        delattr(draft, field)
    with pytest.raises(HandoffError, match="invalid_handle"):
        reviewed(draft)
    setattr(draft, field, original)
    with pytest.raises(HandoffError, match="invalid_handle"):
        reviewed(draft)
    catalog.close()


@pytest.mark.parametrize("field", ("max_output_bytes", "budget_units"))
def test_submission_consent_boolean_is_not_an_integer_authorization(tmp_path, field):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    pending = handoff.prepare_submission(
        reviewed(draft),
        model_id="fake-local-v1",
        max_input_bytes=1024,
        max_output_bytes=1,
        timeout_ns=10_000_000_000,
        budget_units=1,
    )
    request = handoff._HANDLES[pending].request
    consent = handoff.SubmissionConsent(request.input_sha256, request.model_id, 1, 1, True)
    object.__setattr__(consent, field, True)
    with pytest.raises(HandoffError, match="consent_required"):
        handoff.submit(pending, handoff.FakeHandoffProvider(b"r"), consent)
    object.__setattr__(consent, field, 1)
    with pytest.raises(HandoffError, match="invalid_handle"):
        handoff.submit(pending, handoff.FakeHandoffProvider(b"r"), consent)
    catalog.close()


def test_fake_submission_uses_credential_name_without_environment_access(tmp_path, monkeypatch):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    pending = submission(draft, credential_env_ref="TEST_PROVIDER_TOKEN")
    request = handoff._HANDLES[pending].request
    consent = handoff.SubmissionConsent(request.input_sha256, request.model_id, 1024, 3, True)

    class UnreadableEnvironment(dict):
        def __getitem__(self, key):
            pytest.fail("environment must not be read")

        def get(self, key, default=None):
            pytest.fail("environment must not be read")

    monkeypatch.setattr(handoff.os, "environ", UnreadableEnvironment())
    response = handoff.submit(pending, handoff.FakeHandoffProvider(b"local response"), consent)
    assert handoff.render_handoff(response) == "local response"
    response.discard()
    catalog.close()


def test_abandoned_review_releases_issued_authority_observer_and_input(tmp_path):
    _, catalog, _, _, draft = draft_fixture(tmp_path)
    accepted = reviewed(draft)
    state = handoff._HANDLES[accepted]
    authority = state.authority
    witnesses = (
        weakref.ref(accepted),
        weakref.ref(authority),
        weakref.ref(authority.observer),
        weakref.ref(_authority_state(authority).endpoint),
        weakref.ref(state.derived_input),
    )
    del state, authority, accepted, draft
    gc.collect()
    assert all(witness() is None for witness in witnesses)
    catalog.close()


@pytest.mark.parametrize("change", ("metadata", "parent", "source", "handle", "closed_fd"))
def test_edit_post_read_guards_are_integral_and_close_all_descriptors(
    tmp_path, monkeypatch, change
):
    source, catalog, _, _, draft = draft_fixture(tmp_path)
    folder = tmp_path / "edit"
    folder.mkdir(mode=0o700)
    path = folder / "handoff.md"
    exported = handoff.export_handoff(draft, path)
    original = path.read_bytes()
    changed = False

    def mutate(event):
        nonlocal changed
        if event != "after_edit_read" or changed:
            return
        changed = True
        if change == "metadata":
            path.chmod(0o640)
        elif change == "parent":
            folder.rename(tmp_path / "moved")
            folder.mkdir(mode=0o700)
            path.write_bytes(b"NEW USER DOCUMENT")
            path.chmod(0o600)
        elif change == "source":
            before = source.stat()
            source.write_bytes(
                source.read_bytes().replace(b"synthetic answer", b"synthetic broken")
            )
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif change == "handle":
            exported._kind = "changed"
        else:
            descriptors = []
            for name in os.listdir("/proc/self/fd"):
                try:
                    if os.readlink("/proc/self/fd/" + name) == str(path):
                        descriptors.append(int(name))
                except FileNotFoundError:
                    pass
            assert len(descriptors) == 1
            os.close(descriptors[0])

    monkeypatch.setattr(handoff, "_FAULT_HOOK", mutate)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(HandoffError):
        handoff.accept_edit(exported, confirmed_sha256=hashlib.sha256(original).hexdigest())
    assert changed and len(os.listdir("/proc/self/fd")) == before
    assert path.read_bytes() == (b"NEW USER DOCUMENT" if change == "parent" else original)
    with pytest.raises(HandoffError, match="invalid_handle"):
        handoff.accept_edit(exported, confirmed_sha256=hashlib.sha256(original).hexdigest())
    catalog.close()
