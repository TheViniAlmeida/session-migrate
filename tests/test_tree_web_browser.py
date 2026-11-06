"""Exercise the packaged tree mode selector against synthetic HTTP responses."""

import json
import os
from importlib.resources import files

import pytest

playwright = pytest.importorskip("playwright.sync_api")


def test_browser_tree_mode_defaults_opt_in_review_and_literal_metadata(tmp_path):
    executable = os.environ.get("SESSION_MIGRATE_TEST_CHROMIUM")
    if not executable:
        pytest.skip("set SESSION_MIGRATE_TEST_CHROMIUM to an existing Chromium executable")
    origin = "http://127.0.0.1:8021"
    assets = files("session_migrate.web.assets")
    prepared, confirmed, errors = [], [], []
    title = "<img src=x onerror=alert(1)>"

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
                    {"id": "target", "name": "Synthetic destination", "format": "opencode"},
                ],
                "projects": [{"id": "project", "name": "Synthetic project"}],
            }
        elif path == "/api/capabilities":
            value = {"publish_enabled": True, "tree_available": True}
        elif path == "/api/sessions":
            value = {"sessions": [], "next_cursor": None}
        elif path == "/api/trees/prepare":
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
                "fingerprint": "a" * 64,
                "native_action": "tree",
                "preparation_job_id": "prepare-job",
                "metadata": {
                    "publication_mode": prepared[-1]["publication_mode"],
                    "fork_boundary_proven": False,
                    "nodes": [{"title": title}],
                },
            }
        elif path == "/api/jobs/prepare-job/cancel":
            value = {"job_id": "prepare-job", "state": "cancelled"}
        elif path == "/api/trees/proposal/confirm":
            confirmed.append(route.request.post_data_json)
            value = {"job_id": "confirm-job", "state": "completed"}
        elif path == "/api/jobs/confirm-job":
            value = {"job_id": "confirm-job", "state": "completed", "result": {}}
        else:
            raise AssertionError(f"Unexpected synthetic route: {path}")
        route.fulfill(json=value)

    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(
            executable_path=executable, headless=True, chromium_sandbox=True
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1200})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(origin + "/**", route_request)
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
            page.locator("#target-project").select_option("project")
            page.locator("#tree-source-version").fill("2.1.318")
            page.locator("#tree-provider").fill("fixture")
            page.locator("#tree-model").fill("test")
            page.locator("#tree-manifest").set_input_files(
                {
                    "name": "synthetic-tree.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(
                        {"kind": "session-tree-inventory", "schema_version": 1}
                    ).encode(),
                }
            )
            selector = page.locator("#tree-publication-mode")
            playwright.expect(selector).to_have_value("independent")
            page.locator("#prepare-tree").click()
            playwright.expect(page.locator("#proposal")).to_be_visible()
            assert prepared[-1]["publication_mode"] == "independent" and confirmed == []
            playwright.expect(page.locator("#proposal-details")).to_contain_text(title)
            assert page.locator("#proposal-details img").count() == 0
            page.screenshot(path=str(tmp_path / "independent-tree-review.png"), full_page=True)
            selector.select_option("native-subagent")
            playwright.expect(page.locator("#proposal")).to_be_hidden()
            page.locator("#prepare-tree").click()
            playwright.expect(page.locator("#proposal-details")).to_contain_text("native-subagent")
            assert prepared[-1]["publication_mode"] == "native-subagent" and confirmed == []
            page.locator("#confirm-transfer").click()
            playwright.expect(page.locator("#notice")).to_contain_text("Transfer completed")
            assert len(confirmed) == 1 and confirmed[0]["body_fingerprint"] == "a" * 64
            assert errors == []
        finally:
            browser.close()
