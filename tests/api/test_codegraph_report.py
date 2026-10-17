"""GET /api/codegraph/report: serves one server-configured, pre-generated review. No caller input, no subprocess."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blastgate import api as API
from blastgate.api import Settings, create_app

FIXTURE = Path(__file__).resolve().parents[2] / "web" / "tests" / "fixtures" / "codegraph_report.json"


def client(report_path):
    s = Settings(graph_name="unused", provider=None, model="m", provider_timeout_s=1, mcp_timeout_s=1,
                 max_tool_calls=1, codegraph_report=report_path)
    return TestClient(create_app(s), raise_server_exceptions=False)


@pytest.fixture
def report(tmp_path):
    p = tmp_path / "report.json"
    p.write_text(json.dumps(json.loads(FIXTURE.read_text())["body"]))
    return p


def test_serves_the_configured_report(report):
    r = client(report).get("/api/codegraph/report")
    assert r.status_code == 200 and r.json()["verdict"] == "BLOCK"
    assert r.headers["content-security-policy"]


def test_missing_report_is_a_clear_404(tmp_path):
    r = client(tmp_path / "absent.json").get("/api/codegraph/report")
    assert r.status_code == 404 and r.json()["error"] == "no_codegraph_report"


@pytest.mark.parametrize("content", ["not json", "[]", '{"action": "review_report", "verdict": "SAFE", '
                                     '"contract_changes": []}', '{"verdict": "CLEAR", "contract_changes": []}'])
def test_malformed_report_is_never_served(tmp_path, content):
    p = tmp_path / "report.json"
    p.write_text(content)
    r = client(p).get("/api/codegraph/report")
    assert r.status_code == 500 and r.json()["error"] == "invalid_codegraph_report"


def test_oversized_report_is_refused(report, monkeypatch):
    monkeypatch.setattr(API, "CODEGRAPH_REPORT_MAX_BYTES", 10)
    assert client(report).get("/api/codegraph/report").status_code == 500


def test_caller_cannot_choose_the_file_or_start_a_review(report, tmp_path, monkeypatch):
    secret = tmp_path / "other.json"
    secret.write_text('{"action": "review_report", "verdict": "CLEAR", "contract_changes": [], "x": "secret"}')
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("the endpoint must never run a subprocess"))
    c = client(report)
    r = c.get("/api/codegraph/report", params={"path": str(secret), "repo": "/", "base": "HEAD"})
    assert r.status_code == 200 and "secret" not in r.text
    assert c.post("/api/codegraph/report", json={"path": str(secret)}).status_code == 405


def test_health_declares_what_this_process_serves(report, tmp_path):
    # Root cause of "Failed to fetch"/404 on the review: the console's static files are read from disk per request,
    # so an older API process can serve the new UI. /health must say which APIs THIS process has.
    h = client(report).get("/health").json()
    assert {"codegraph_report", "codegraph_scenarios", "investigation_memory"} <= set(h["features"])
    assert h["codegraph"]["report_available"] is True and isinstance(h["codegraph"]["registered"], list)
    assert client(tmp_path / "absent.json").get("/health").json()["codegraph"]["report_available"] is False
