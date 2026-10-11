"""Launch configuration validation does not open a network listener."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from session_migrate.cli import build_parser, main
from session_migrate.errors import SessionMigrateError
from session_migrate.model import AgentFormat
from session_migrate.profiles import Profile, ProfileStore
from session_migrate.web.launch import launch_configuration


@pytest.fixture
def config(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    root = tmp_path / "native"
    root.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    profiles = private / "profiles.json"
    ProfileStore(profiles).create((Profile("selected", "Selected", AgentFormat.CLAUDE, root),))
    return SimpleNamespace(
        port=0,
        profiles=profiles,
        selected_profiles=["selected"],
        project=["Fixture=" + str(work)],
        catalog=private / "catalog.sqlite",
    )


def test_launch_configuration_selects_only_saved_profile_ids(config):
    profiles, projects = launch_configuration(config)
    assert len(profiles) == 1 and profiles[0].id == "selected"
    assert projects["Fixture"].is_absolute()


@pytest.mark.parametrize("action", ["adopt", "reconcile", "cancel_ready"])
def test_service_launch_flags_require_publish_and_only_select_named_action(config, action):
    from session_migrate.web.launch import selected_service_actions

    assert selected_service_actions(config) == frozenset()
    setattr(config, "allow_" + action, True)
    config.allow_publish = False
    with pytest.raises(SessionMigrateError, match="web_services_require_publish"):
        launch_configuration(config)
    config.allow_publish = True
    assert selected_service_actions(config) == frozenset({action})
    launch_configuration(config)


@pytest.mark.parametrize("selected", [[], ["selected", "selected"], ["unknown"], ["../escape"]])
def test_launch_profile_selection_rejects_unknown_and_duplicate(config, selected):
    config.selected_profiles = selected
    with pytest.raises(SessionMigrateError, match="web_profile_selection_invalid"):
        launch_configuration(config)


@pytest.mark.parametrize("port", [-1, 1, 1023, 65536, True])
def test_launch_port_is_loopback_unprivileged_or_assigned(config, port):
    config.port = port
    with pytest.raises(SessionMigrateError, match="web_port_invalid"):
        launch_configuration(config)


@pytest.mark.parametrize("project", [["bad"], ["name=relative"], ["name=/absent-project-fixture"]])
def test_launch_project_requires_explicit_existing_absolute_directory(config, project):
    config.project = project
    with pytest.raises(SessionMigrateError, match="web_project_selection_invalid"):
        launch_configuration(config)


def test_launch_requires_named_catalog(config):
    config.catalog = None
    with pytest.raises(SessionMigrateError, match="web_requires_explicit_existing_catalog"):
        launch_configuration(config)


def test_cli_web_is_explicit_default_readonly_and_does_not_import_web_framework():
    args = build_parser().parse_args(
        [
            "--catalog",
            "/fixture/catalog",
            "web",
            "--profiles",
            "/fixture/profiles",
            "--profile",
            "selected",
            "--bootstrap",
            "/fixture/launch",
        ]
    )
    assert args.command == "web" and args.port == 8021 and args.allow_publish is False
    assert args.selected_profiles == ["selected"] and type(args.bootstrap) is type(Path("/"))


def test_missing_web_extra_exits_before_socket(monkeypatch, capsys):
    import builtins
    import socket

    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("synthetic absent extra")
        return original(name, *args, **kwargs)

    def socket_forbidden(*_, **__):
        pytest.fail("missing extra must not open a listener")

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(socket, "socket", socket_forbidden)
    result = main(
        [
            "--catalog",
            "/fixture/catalog",
            "web",
            "--profiles",
            "/fixture/profiles",
            "--profile",
            "selected",
            "--bootstrap",
            "/fixture/launch",
        ]
    )
    assert result == 2
    assert "web_extra_unavailable" in capsys.readouterr().err
