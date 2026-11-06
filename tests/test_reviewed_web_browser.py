"""Packaged UI distinguishes reviewed preparation, literal review and explicit consent."""

import json
import os
from importlib.resources import files

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_reviewed_ui_preview_and_confirmation_are_separate_literal_actions():
    executable = os.environ.get("SESSION_MIGRATE_TEST_CHROMIUM")
    if not executable:
        pytest.skip("set SESSION_MIGRATE_TEST_CHROMIUM to an existing Chromium executable")
    origin = "http://127.0.0.1:8021"
    assets = files("session_migrate.web.assets")
    prepared, confirmed, previews, errors = [], [], [], []
    hostile = "<img src=x onerror=alert(1)>"

    def route_request(route):
        path = route.request.url.removeprefix(origin).split("?", 1)[0]
        if path in {"/", "/static/dashboard.js", "/static/dashboard.css"}:
            name = "index.html" if path == "/" else path.rsplit("/", 1)[1]
            kind = {"html": "text/html", "js": "text/javascript", "css": "text/css"}
            route.fulfill(
                body=assets.joinpath(name).read_text(), content_type=kind[name.split(".")[-1]]
            )
            return
        if path == "/api/profiles":
            value = {
                "profiles": [
                    {"id": "source", "name": "Synthetic source", "format": "claude"},
                    {"id": "target", "name": "Synthetic target", "format": "opencode"},
                ],
                "projects": [{"id": "project", "name": "Synthetic project"}],
            }
        elif path == "/api/capabilities":
            value = {"publish_enabled": True, "tree_available": True}
        elif path == "/api/sessions":
            value = {
                "sessions": [
                    {"id": "source-session", "title": "Synthetic source", "format": "claude"}
                ]
            }
        elif path.endswith("/preview"):
            previews.append(route.request.url)
            if "reviewed_portability=true" not in route.request.url:
                route.fulfill(status=500, json={"reason": "operation_unavailable"})
                return
            value = {
                "mode": "reviewed_portability",
                "text": hostile + "\nReviewed conversation DATA",
                "truncated": False,
            }
        elif path == "/api/proposals":
            prepared.append(route.request.post_data_json)
            value = {"job_id": "prepare-job", "state": "queued"}
        elif path == "/api/jobs/prepare-job":
            value = {
                "job_id": "prepare-job",
                "state": "reviewable",
                "result": {"proposal_id": "proposal"},
            }
        elif path == "/api/proposals/proposal":
            value = {
                "proposal_id": "proposal",
                "preparation_job_id": "prepare-job",
                "fingerprint": "a" * 64,
                "expires_at": "2099-01-01T00:00:00Z",
                "metadata": {
                    "mode": "reviewed_portability",
                    "reviewed_portability": {"differences": [{"code": hostile, "count": 1}]},
                    "reviewed_archive": {
                        "status": "planned",
                        "sha256": "b" * 64,
                        "path": "/private/source.jsonl",
                    },
                },
            }
        elif path == "/api/proposals/proposal/confirm":
            confirmed.append(route.request.post_data_json)
            value = {"job_id": "confirm-job", "state": "queued"}
        elif path == "/api/jobs/confirm-job":
            value = {
                "job_id": "confirm-job",
                "state": "completed",
                "result": {"status": "completed"},
            }
        else:
            route.fulfill(status=404, json={"reason": "unavailable"})
            return
        route.fulfill(json=value)

    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(
            executable_path=executable, headless=True, chromium_sandbox=True
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1200})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: errors.append(dialog.message))
            page.route("**/*", route_request)
            page.goto(origin)
            page.locator("#launch-file").set_input_files(
                {
                    "name": "synthetic-launch.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(
                        {
                            "schema_version": 1,
                            "run_id": "synthetic-run",
                            "origin": origin,
                            "expires_at": "2099-01-01T00:00:00Z",
                            "access_token": "SYNTHETIC_TEST_TOKEN",
                            "csrf_nonce": "SYNTHETIC_TEST_NONCE",
                        }
                    ).encode(),
                }
            )
            playwright.expect(page.locator("#connection-state")).to_have_text("Connected")
            page.locator("#source-profile").select_option("source")
            page.locator("#target-profile").select_option("target")
            page.locator(".session-button").click()
            playwright.expect(page.locator("#preview")).to_contain_text("Preview unavailable")
            playwright.expect(page.locator("#prepare-transfer")).to_be_enabled()
            assert not page.locator("#reviewed-portability").is_checked()
            assert not page.locator("#tree-reviewed-portability").is_checked()
            page.locator("#reviewed-portability").check()
            playwright.expect(page.locator("#preview")).to_contain_text(hostile)
            page.locator("#prepare-transfer").click()
            playwright.expect(page.locator("#proposal")).to_be_visible()
            playwright.expect(page.locator("#proposal-details")).to_contain_text(hostile)
            playwright.expect(page.locator("#proposal-details")).to_contain_text("planned")
            assert prepared[0]["reviewed_portability"] is True
            assert "acknowledge_reviewed_portability" not in prepared[0]
            assert confirmed == []
            assert page.locator("#proposal-details img").count() == 0
            assert page.locator("#preview img").count() == 0
            playwright.expect(page.locator("#confirm-transfer")).to_have_text(
                "Accept displayed runtime/order differences and publish"
            )
            page.locator("#confirm-transfer").click()
            page.wait_for_function(
                "document.getElementById('notice').textContent.includes('Transfer completed')"
            )
            assert len(confirmed) == 1 and confirmed[0]["acknowledge_reviewed_portability"] is True
            assert confirmed[0]["body_fingerprint"] == "a" * 64
            assert len(previews) >= 2 and previews[-1].endswith("?reviewed_portability=true")
            assert errors == []
        finally:
            browser.close()
