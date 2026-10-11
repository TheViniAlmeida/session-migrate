"""Opt-in loopback launch; no autostart, remote listener or browser invocation."""

from __future__ import annotations

import os
import socket
from pathlib import Path

from session_migrate.errors import SessionMigrateError


def add_web_command(subparsers, path_parser):
    parser = subparsers.add_parser("web", help="optional local dashboard (web extra)")
    parser.add_argument(
        "--profiles", required=True, type=path_parser, help="existing private saved-profile file"
    )
    parser.add_argument(
        "--profile",
        action="append",
        required=True,
        dest="selected_profiles",
        help="saved profile ID selected for this run; repeat explicitly",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="explicit project NAME=/absolute/directory; repeat",
    )
    parser.add_argument(
        "--bootstrap",
        required=True,
        type=path_parser,
        help="new launch file in an existing private directory",
    )
    parser.add_argument(
        "--port", type=int, default=8021, help="loopback port 1024-65535, or 0 for an assigned port"
    )
    parser.add_argument(
        "--allow-publish",
        action="store_true",
        help="enable explicit reviewed transfers for the selected profiles",
    )
    for action in ("adopt", "reconcile", "cancel-ready", "handoff", "return-new"):
        parser.add_argument(
            f"--allow-{action}",
            action="store_true",
            help=f"also enable reviewed {action} for this run; requires --allow-publish",
        )


def selected_service_actions(args):
    enabled = set()
    for name in ("adopt", "reconcile", "cancel_ready"):
        value = getattr(args, "allow_" + name, False)
        if type(value) is not bool:
            raise SessionMigrateError("web_service_selection_invalid")
        if value:
            enabled.add(name)
    if enabled and not getattr(args, "allow_publish", False):
        raise SessionMigrateError("web_services_require_publish")
    return frozenset(enabled)


def selected_runtime_actions(args):
    enabled = set()
    for name, actions in (
        ("handoff", {"summary"}),
        ("return_new", {"return_new_full", "return_new_summary"}),
    ):
        value = getattr(args, "allow_" + name, False)
        if type(value) is not bool:
            raise SessionMigrateError("web_runtime_selection_invalid")
        if value:
            enabled.update(actions)
    if enabled and not getattr(args, "allow_publish", False):
        raise SessionMigrateError("web_runtime_requires_publish")
    return frozenset(enabled)


def launch_configuration(args):
    from session_migrate.profiles import ProfileStore

    selected_service_actions(args)
    selected_runtime_actions(args)
    if type(args.port) is not int or (args.port != 0 and not 1024 <= args.port <= 65535):
        raise SessionMigrateError("web_port_invalid")
    profiles = ProfileStore(args.profiles).load()
    requested = args.selected_profiles
    if not requested or len(requested) > 32 or len(set(requested)) != len(requested):
        raise SessionMigrateError("web_profile_selection_invalid")
    selected = tuple(profile for profile in profiles if profile.id in requested)
    if len(selected) != len(requested):
        raise SessionMigrateError("web_profile_selection_invalid")
    projects = {}
    for item in args.project:
        if type(item) is not str or "=" not in item:
            raise SessionMigrateError("web_project_selection_invalid")
        name, raw = item.split("=", 1)
        path = Path(raw)
        if not name or name in projects or not path.is_absolute() or not path.is_dir():
            raise SessionMigrateError("web_project_selection_invalid")
        projects[name] = path
    if args.catalog is None:
        raise SessionMigrateError("web_requires_explicit_existing_catalog")
    return selected, projects


def run_web(args):
    try:
        import uvicorn

        from session_migrate.web.app import create_app
    except ImportError:
        raise SessionMigrateError("web_extra_unavailable: install session-migrate[web]") from None
    from session_migrate.web.auth import RunRegistry
    from session_migrate.web.backend import WorkspaceBackend
    from session_migrate.web.bootstrap import write_bootstrap

    selected, projects = launch_configuration(args)
    registry = RunRegistry()
    backend = owner = listener = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        backend = WorkspaceBackend(
            registry,
            args.catalog,
            selected,
            projects=projects,
            base_environ=dict(os.environ),
            state_home=args.state_home,
            service_actions=selected_service_actions(args),
            runtime_actions=selected_runtime_actions(args),
        )
        credential = registry.create(
            origin, frozenset({"readonly", "publish"} if args.allow_publish else {"readonly"})
        )
        app = create_app(registry, origin, backend)
        owner = write_bootstrap(registry, credential, args.bootstrap)
        print(f"Dashboard: {origin}")
        print(f"Connect using launch file: {owner.path}")
        configuration = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=listener.getsockname()[1],
            access_log=False,
            proxy_headers=False,
            server_header=False,
            log_level="warning",
            lifespan="off",
            ws="none",
            http="h11",
            limit_concurrency=16,
            timeout_keep_alive=5,
        )
        uvicorn.Server(configuration).run(sockets=[listener])
        return 0
    except (OSError, ValueError):
        raise SessionMigrateError("web_launch_unavailable") from None
    finally:
        # Revocation cancels pre-barrier jobs; active publication keeps core ownership.
        try:
            registry.close()
        finally:
            try:
                if backend is not None:
                    backend.close()
            finally:
                try:
                    if owner is not None:
                        state = owner.close()
                        if state == "uncertain":
                            print("Launch-file cleanup is uncertain; inspect the selected file.")
                finally:
                    if listener is not None:
                        listener.close()
