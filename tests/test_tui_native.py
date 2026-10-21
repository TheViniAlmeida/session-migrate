"""Opt-in keyboard/native-core composition gate, with no provider/model calls.

OpenCode endpoints use the designated stock official importer/exporter in fresh
processes. Claude/Codex outputs are parsed here; their cold resume/append proof
is composed with the existing exact-client CI gates, not claimed by this module.
Skips outside the explicit native job are prerequisites missing, never a pass.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("textual", reason="native TUI gate requires the optional tui extra")

from native_sandbox import private_root_masks
from textual.widgets import Button, Input, OptionList, Select

from session_migrate.catalog import Catalog
from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    detect_opencode_version,
    install_opencode_artifact,
    load_opencode_session,
    load_session,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat, EventKind, TargetFormat
from session_migrate.tui.app import MigrationApp
from session_migrate.tui.controller import Controller
from session_migrate.tui.widgets import Literal

FIXTURES = Path(__file__).parent / "fixtures"
BIN_VARIABLE = "SESSION_MIGRATE_TEST_OPENCODE_V2"


def environment(base: Path) -> dict[str, str]:
    home = base / "home"
    # mkdir(parents=True) applies mode only to the leaf, leaving intermediate
    # directories group-writable under umask 0002 and invalid for the ledger.
    for directory in (base, home, home / ".local"):
        directory.mkdir(mode=0o700, exist_ok=True)
    values = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
    }
    for key, relative in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_STATE_HOME", ".local/state"),
        ("TMPDIR", ".cache/session-migrate-tmp"),
    ):
        path = home / relative
        path.mkdir(parents=True, mode=0o700)
        values[key] = str(path)
    return values


@dataclass(frozen=True, repr=False)
class StockClient:
    cli: Path
    release: str


@pytest.fixture
def stock(tmp_path):
    configured = os.environ.get(BIN_VARIABLE)
    if not configured:
        pytest.skip(f"explicit native gate requires ${BIN_VARIABLE}; PATH is not a fallback")
    binary = Path(configured)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise pytest.UsageError("designated native executable is unavailable")
    # Keep machine/account paths out of profile/traceback fixtures and saved UI instructions.
    proxy = tmp_path / "stock-opencode"
    proxy.symlink_to(binary)
    release = detect_opencode_version(proxy, environ=environment(tmp_path / "version-probe"))
    if release not in {"2.0.22", "2.0.23", "2.1.1"}:
        raise pytest.UsageError(
            "native TUI gate requires designated stock OpenCode 2.0.22/2.0.23/2.1.1"
        )
    return StockClient(proxy, release)


@dataclass(repr=False)
class Case:
    controller: Controller
    source_id: str
    source_root: Path
    source_path: Path | None
    source_env: dict[str, str]
    target_root: Path
    target_env: dict[str, str]
    project: Path
    native_id: str


def setup_case(tmp_path: Path, stock: StockClient, source: str, target: str) -> Case:
    project = tmp_path / "project"
    project.mkdir()
    # A shared project-only fixture, with no authentication/provider configuration.
    config = project / "opencode.json"
    config.write_text(json.dumps({"warming": False, "disabled_providers": ["opencode"]}))
    source_env = environment(tmp_path / "source")
    target_env = environment(tmp_path / "target")
    source_env["OPENCODE_CONFIG"] = target_env["OPENCODE_CONFIG"] = str(config)
    fixture = FIXTURES / (
        "claude-2.1.209/basic.jsonl" if source == "claude" else "codex-0.144.4/basic.jsonl"
    )
    source_session = load_session(
        fixture, AgentFormat.CLAUDE if source == "claude" else AgentFormat.CODEX
    )
    source_root = tmp_path / "source-root" / source
    source_path = None
    if source == "opencode":
        source_root = Path(source_env["XDG_DATA_HOME"]) / "opencode"
        artifact = convert_session(
            source_session,
            ConversionOptions(
                target_format=TargetFormat.OPENCODE,
                target_cli_version=stock.release,
                cwd=project,
                model_provider="synthetic",
                model="offline",
            ),
        )
        install_opencode_artifact(
            artifact,
            manifest_path=tmp_path / "bootstrap.json",
            target_cli=stock.cli,
            environ=source_env,
        )
        native_id = artifact.session_id
    else:
        native_id = source_session.session_id
        relative = (
            Path("projects/-synthetic") / f"{native_id}.jsonl"
            if source == "claude"
            else Path("sessions/2026/08/18") / f"rollout-synthetic-{native_id}.jsonl"
        )
        source_path = source_root / relative
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(fixture.read_bytes())
    target_root = (
        Path(target_env["XDG_DATA_HOME"]) / "opencode"
        if target == "opencode"
        else tmp_path / "target-root" / target
    )
    catalog_path = tmp_path / "catalog.sqlite3"
    with Catalog(catalog_path) as catalog:
        root = catalog.add_root(AgentFormat(source), source_root)
        assert catalog.refresh(include_auto=False).root_errors == 0
    controller = Controller(catalog_path, environ=source_env)
    return Case(
        controller,
        f"root-{root.id}",
        source_root,
        source_path,
        source_env,
        target_root,
        target_env,
        project,
        native_id,
    )


async def idle(app: MigrationApp, pilot) -> None:
    for _ in range(1200):
        await pilot.pause(0.02)
        if app.pending is None and app.preview_request is None:
            return
    raise AssertionError("native keyboard job did not finish within the test wait budget")


async def activate(app: MigrationApp, pilot, id: str) -> None:
    app.query_one(f"#{id}", Button).focus()
    await pilot.press("enter")
    await idle(app, pilot)


def fill(app: MigrationApp, id: str, value) -> None:
    app.query_one(f"#{id}", Input).value = str(value)


async def prepare_route(app, pilot, case: Case, stock: StockClient, source: str, target: str):
    await idle(app, pilot)
    app.query_one("#source-profile", Select).value = case.source_id
    await pilot.pause()
    for field, key in (("home", "HOME"), ("config", "XDG_CONFIG_HOME"), ("data", "XDG_DATA_HOME")):
        fill(app, f"source-{field}", case.source_env[key])
    if source == "opencode":
        fill(app, "source-cli", stock.cli)
    await pilot.pause()
    await activate(app, pilot, "list")
    assert app.stage == "sessions"
    assert len(app.entries) == 1
    assert app.entries[0].ref.native_id == case.native_id
    assert app.source.root == case.source_root
    app.query_one("#session-list", OptionList).highlighted = 0
    await activate(app, pilot, "preview")
    assert app.snapshot is not None
    assert app.stage == "preview-step"
    witness = case.controller.revision(app.source, app.selected.ref)
    # Repeated official export/stat guards must not invalidate an unchanged OP session.
    assert case.controller.revision(app.source, app.selected.ref) == witness
    await activate(app, pilot, "target")
    app.query_one("#target-format", Select).value = target
    fill(app, "target-root", case.target_root)
    fill(app, "target-name", "Isolated target")
    for field, key in (("home", "HOME"), ("config", "XDG_CONFIG_HOME"), ("data", "XDG_DATA_HOME")):
        fill(app, f"target-{field}", case.target_env[key])
    fill(app, "cwd", case.project)
    if target == "opencode":
        fill(app, "target-cli", stock.cli)
        fill(app, "schema", stock.release)
        fill(app, "provider", "synthetic")
        fill(app, "model", "offline")
    await pilot.pause()
    await activate(app, pilot, "prepare")
    assert app.stage == "review"
    assert app.draft is not None
    assert app.draft.source.root == case.source_root
    assert app.draft.target.root == case.target_root
    assert app.draft.source.environ["HOME"] != app.draft.target.environ["HOME"]
    assert (
        app.draft.source.environ["XDG_CONFIG_HOME"] != app.draft.target.environ["XDG_CONFIG_HOME"]
    )
    assert app.draft.result.cwd == str(case.project)
    return witness, app.draft


@pytest.mark.parametrize(
    "source,target", [("claude", "codex"), ("codex", "opencode"), ("opencode", "claude")]
)
def test_keyboard_real_core_native_routes(tmp_path, stock, source, target):
    case = setup_case(tmp_path, stock, source, target)
    source_bytes = case.source_path.read_bytes() if case.source_path else None
    source_environment, target_environment = dict(case.source_env), dict(case.target_env)

    async def flow():
        app = MigrationApp(case.controller)
        async with app.run_test(size=(120, 60)) as pilot:
            witness, draft = await prepare_route(app, pilot, case, stock, source, target)
            reserved = draft.result.session_id
            manifest = Path(draft.result.manifest)
            output = draft.result.output
            await pilot.resize_terminal(74, 22)
            await pilot.pause()
            await activate(app, pilot, "confirm")
            assert app.completed and not app.needs_reconcile
            assert app.stage == "result"
            assert reserved in str(app.query_one("#receipt", Literal).render())
            assert case.controller.revision(app.source, app.selected.ref) == witness
            assert manifest.is_file()
            metadata = json.loads(manifest.read_text())
            assert metadata["target"]["session_id"] == reserved
            if target == "opencode":
                # Official cold export reads the actual designated target store in a new process.
                installed = load_opencode_session(
                    reserved,
                    source_cli=stock.cli,
                    environ=case.target_env,
                    max_source_bytes=8 * 1024 * 1024,
                )
                assert output == f"opencode:{reserved}"
                assert case.target_root.is_dir()
                assert not (Path(case.source_env["XDG_DATA_HOME"]) / "opencode").exists()
            else:
                path = Path(output)
                assert path.is_relative_to(case.target_root)
                installed = load_session(path, AgentFormat(target))
            assert installed.session_id == reserved
            assert installed.cwd == case.project
            assert any(event.kind == EventKind.MESSAGE for event in installed.events)
            instruction = str(app.query_one("#receipt", Literal).render())
            expected_root = case.target_root.parent if target == "opencode" else case.target_root
            assert str(expected_root) in instruction
            assert case.target_env["HOME"] in instruction

    asyncio.run(flow())
    if case.source_path:
        assert case.source_path.read_bytes() == source_bytes
    assert case.source_env == source_environment
    assert case.target_env == target_environment


@pytest.mark.parametrize("source,target", [("claude", "codex"), ("codex", "opencode")])
def test_keyboard_native_route_source_drift_blocks_confirm(tmp_path, stock, source, target):
    case = setup_case(tmp_path, stock, source, target)

    async def flow():
        app = MigrationApp(case.controller)
        async with app.run_test(size=(120, 60)) as pilot:
            _, draft = await prepare_route(app, pilot, case, stock, source, target)
            manifest = Path(draft.result.manifest)
            reserved = draft.result.session_id
            case.source_path.write_bytes(case.source_path.read_bytes() + b"\n")
            await activate(app, pilot, "confirm")
            assert not app.completed
            assert app.draft is None
            assert app.query_one("#confirm", Button).disabled
            assert not manifest.exists()
            if target == "codex":
                assert not case.target_root.exists()
            else:
                # A dry-run probe may initialize the store; it must not publish this ID.
                with pytest.raises(SessionMigrateError):
                    load_opencode_session(reserved, source_cli=stock.cli, environ=case.target_env)

    asyncio.run(flow())


@pytest.fixture
def title_stock(tmp_path):
    """Keep new title-route native commands inside a private, offline namespace."""
    import hashlib
    import shlex
    import shutil

    configured = os.environ.get(BIN_VARIABLE)
    if not configured:
        pytest.skip(f"explicit title gate requires ${BIN_VARIABLE}")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("native title keyboard gate requires bubblewrap")
    binary = Path(configured).resolve()
    accepted = {
        "32cf5aa0a69a650e36277e3315d189835ddc79fb9aa1d0aef5025be5af5ad122",
        "a233de331f4438336e604d8e006fdab18e5911c4ece16de8e502eb61394c3586",
        "0e81c77fcc27b263708b133bbf9880cd641060c20e22d3dec8a79b9a8db90450",
    }
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest not in accepted:
        raise pytest.UsageError("native title keyboard gate requires an exact stock ELF")
    wrapper = tmp_path / "offline-stock-opencode"
    # Only a runtime scratch wrapper, never a source account/store or package script.
    args = [
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
        "/tmp/opencode-title-elf",
        "--proc",
        "/proc",
    ]
    # The launcher begins outside the project. Honor an explicit subprocess cwd
    # inside the private fixture, but keep default probes in the fixture root.
    root_literal = shlex.quote(str(tmp_path))
    wrapper.write_text(
        "#!/bin/sh\n"
        + f"title_root={root_literal}\n"
        + 'case "$PWD" in "$title_root"|"$title_root"/*) title_cwd="$PWD" ;; '
        + '*) title_cwd="$title_root" ;; esac\n'
        + "exec "
        + shlex.join(args)
        + ' --chdir "$title_cwd" -- /tmp/opencode-title-elf "$@"\n'
    )
    wrapper.chmod(0o700)
    release = detect_opencode_version(wrapper, environ=environment(tmp_path / "private-version"))
    assert release in {"2.0.22", "2.0.23", "2.1.1"}
    return StockClient(wrapper, release)


@pytest.mark.parametrize("target,policy", [("opencode", "native"), ("codex", "local_alias")])
def test_keyboard_native_title_and_explicit_alias_exact_artifact_two_homes(
    tmp_path,
    title_stock,
    target,
    policy,
):
    import hashlib

    from textual.widgets import Checkbox

    from session_migrate.profiles import Profile, local_alias_for_session

    case = setup_case(tmp_path, title_stock, "claude", target)
    scratch = tmp_path / "native-scratch"
    scratch.mkdir(mode=0o700)
    case.source_env["TMPDIR"] = case.target_env["TMPDIR"] = str(scratch)
    case.controller.environ["TMPDIR"] = str(scratch)
    before = case.source_path.read_bytes()
    title = "  Native or alias · Ação 🧪 日本語 [bold]literal[/bold]  "

    async def flow():
        app = MigrationApp(case.controller)
        async with app.run_test(size=(120, 60)) as pilot:
            # Assemble the same exact source/target profile route, then name it explicitly.
            await idle(app, pilot)
            app.query_one("#source-profile", Select).value = case.source_id
            await pilot.pause()
            for field, key in (
                ("home", "HOME"),
                ("config", "XDG_CONFIG_HOME"),
                ("data", "XDG_DATA_HOME"),
            ):
                fill(app, f"source-{field}", case.source_env[key])
            await activate(app, pilot, "list")
            app.query_one("#session-list", OptionList).highlighted = 0
            await activate(app, pilot, "preview")
            await activate(app, pilot, "target")
            app.query_one("#target-format", Select).value = target
            fill(app, "target-root", case.target_root)
            for field, key in (
                ("home", "HOME"),
                ("config", "XDG_CONFIG_HOME"),
                ("data", "XDG_DATA_HOME"),
            ):
                fill(app, f"target-{field}", case.target_env[key])
            fill(app, "cwd", case.project)
            fill(app, "target-title", title)
            if policy == "native":
                fill(app, "target-cli", title_stock.cli)
                fill(app, "schema", title_stock.release)
                fill(app, "provider", "synthetic")
                fill(app, "model", "unused")
                app.query_one("#require-native-title", Checkbox).value = True
            else:
                app.query_one("#local-title-alias", Checkbox).value = True
            await pilot.pause()
            await activate(app, pilot, "prepare")
            draft = app.draft
            proposal = draft.proposal.to_dict()
            assert proposal["title"]["requested"] == title
            assert draft.source.environ["HOME"] != draft.target.environ["HOME"]
            await pilot.resize_terminal(76, 25)
            await pilot.pause()
            await activate(app, pilot, "confirm")
            assert app.completed and not app.needs_reconcile
            rendered = str(app.query_one("#receipt", Literal).render())
            assert title in rendered and "Resume manually" in rendered
            assert draft.result.session_id in rendered
            metadata = json.loads(Path(draft.result.manifest).read_text())
            assert metadata["schema_version"] == 2
            assert metadata["target"]["sha256"] == proposal["target"]["sha256"]
            if policy == "native":
                observed = load_opencode_session(
                    draft.result.session_id, source_cli=title_stock.cli, environ=case.target_env
                )
                assert observed.title == title
                assert "native_verified" in rendered and "verified" in rendered
                assert not (Path(case.source_env["XDG_DATA_HOME"]) / "opencode").exists()
            else:
                assert (
                    hashlib.sha256(Path(draft.result.output).read_bytes()).hexdigest()
                    == proposal["target"]["sha256"]
                )
                assert "local_alias" in rendered and "published" in rendered
                profile = Profile(
                    "renamed",
                    "Renamed target profile",
                    target,
                    case.target_root,
                    environ={
                        key: value
                        for key, value in case.target_env.items()
                        if key
                        in {
                            "HOME",
                            "XDG_CONFIG_HOME",
                            "XDG_DATA_HOME",
                            "XDG_CACHE_HOME",
                            "XDG_STATE_HOME",
                        }
                    },
                )
                with Catalog(case.controller.catalog) as catalog:
                    catalog.add_root(AgentFormat(target), case.target_root)
                    catalog.refresh(include_auto=False)
                rows = case.controller.list(profile, "", 0)
                assert len(rows) == 1 and rows[0].local_alias == title
                assert rows[0].entry.title != title
                with Catalog(case.controller.catalog) as catalog:
                    assert (
                        local_alias_for_session(
                            catalog, profile, rows[0].ref, base_environ=case.source_env
                        )
                        == title
                    )
            title_receipt = Path(proposal["title_receipt"])
            assert title_receipt.is_file()
            assert not (
                Path(case.source_env["XDG_STATE_HOME"]) / "session-migrate/title-aliases"
            ).exists()

    asyncio.run(flow())
    assert case.source_path.read_bytes() == before
