"""The console is served by the API app, with a CSP; /api/analyze carries the code-owned inventory;
recorded frontend fixtures still match the live response models (drift guard)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastgate.agent import InvestigationReport
from blastgate.api import AnalyzeResponse, SimulateResponse, create_app

FIXTURES = Path(__file__).resolve().parents[2] / "web" / "tests" / "fixtures"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return TestClient(create_app(), raise_server_exceptions=False)


def test_console_and_assets_are_served_with_csp(client):
    page = client.get("/")
    assert page.status_code == 200 and "What could break if I merge this change?" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    for asset, ctype in (("/app.js", "javascript"), ("/view.js", "javascript"), ("/styles.css", "text/css")):
        r = client.get(asset)
        assert r.status_code == 200 and ctype in r.headers["content-type"], asset


def test_api_routes_win_over_static_and_docs_keep_working(client):
    assert client.get("/api/scenarios").headers["content-type"].startswith("application/json")
    docs = client.get("/docs")
    assert docs.status_code == 200 and "content-security-policy" not in docs.headers   # Swagger needs its CDN
    assert client.get("/nope.js").status_code == 404


def test_product_pages_served_with_clean_urls_and_csp(client):
    for path, expected_text in [
        ("/console", "Investigation Console"),
        ("/how-it-works", "7-Stage"),
        ("/architecture", "Architecture"),
        ("/demo-guide", "Judge Walkthrough"),
    ]:
        res = client.get(path)
        assert res.status_code == 200, f"Route {path} failed: {res.status_code}"
        assert expected_text in res.text, f"Route {path} missing expected text: {expected_text}"
        csp = res.headers.get("content-security-policy", "")
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_no_secrets_in_served_files(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_must_never_appear")
    c = TestClient(create_app(), raise_server_exceptions=False)
    for path in ("/", "/app.js", "/view.js", "/styles.css", "/health"):
        # The variable *name* may appear in /health's configuration hint; its *value* must never appear.
        assert "gsk_must_never_appear" not in c.get(path).text, path


@pytest.mark.integration
def test_analyze_returns_code_owned_summary_and_inventory(monkeypatch, scratch_graph):
    monkeypatch.setenv("BLASTGATE_GRAPH", scratch_graph.name)
    c = TestClient(create_app(), raise_server_exceptions=False)
    a = c.post("/api/analyze", json={"pr_id": "PR-112"}).json()
    assert {k: v["status"] for k, v in a["inventory"].items()} == {
        "consumers": "found", "vulnerable_consumers": "none_found", "affected_services": "none_found",
        "critical_services": "none_found", "slas": "none_found", "incidents": "not_checked",
        "reviewers": "not_checked"}
    assert a["summary"].startswith("CLEAR:") and "incidents on this contract: not checked" in a["summary"]
    b = c.post("/api/analyze", json={"pr_id": "PR-104"}).json()
    assert b["inventory"]["incidents"] == {"status": "found", "items": ["INC-2041"], "source": "deterministic analysis"}


@pytest.mark.parametrize("prefix, model", [("analyze_", AnalyzeResponse), ("investigate_", InvestigationReport),
                                           ("simulate_", SimulateResponse)])
def test_recorded_fixtures_match_current_response_models(prefix, model):
    files = sorted(FIXTURES.glob(f"{prefix}*.json"))
    assert files, f"no {prefix} fixtures; run python web/tests/capture_fixtures.py"
    for f in files:
        body = json.loads(f.read_text())["body"]
        assert set(body) == set(model.model_fields), f"{f.name} is stale; re-run capture_fixtures.py"
        model.model_validate(body)
