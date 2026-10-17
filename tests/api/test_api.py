"""FastAPI contract tests. Routes that read the graph use live FalkorDB (skipped with a reason otherwise);
configuration, validation and failure paths run without it."""
import pytest
from fastapi.testclient import TestClient

from blastgate import agent as AG
from blastgate.api import Settings, create_app

SIM = {"pr_id": "PR-104", "consumer": "checkout-api", "assumption": "checkout-api adds a tolerant parser"}


def client(monkeypatch, local=False, **env):
    for k in ("AGENT_PROVIDER", "AGENT_MODEL", "GROQ_API_KEY", "AGENT_MAX_TOOL_CALLS", "AGENT_LIVE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(), raise_server_exceptions=False,
                      client=("127.0.0.1", 50000) if local else ("testclient", 50000))


# --- configuration (no DB) ---------------------------------------------------------------------

def test_settings_parsing_and_hard_cap(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "50")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    s = Settings.from_env()
    assert s.max_tool_calls == 6 and s.model == "openai/gpt-oss-20b"
    monkeypatch.setenv("AGENT_MODEL", "some/other-model")
    assert Settings.from_env().model == "some/other-model"


@pytest.mark.parametrize("env, configured, detail", [
    ({}, False, "AGENT_PROVIDER is not set"),
    ({"AGENT_PROVIDER": "groq"}, False, "GROQ_API_KEY is not set"),
    ({"AGENT_PROVIDER": "groq", "GROQ_API_KEY": "gsk_test_not_real"}, True, "groq/openai/gpt-oss-20b"),
    ({"AGENT_PROVIDER": "deterministic"}, True, "deterministic-demo"),
    ({"AGENT_PROVIDER": "openai"}, False, "unsupported AGENT_PROVIDER"),
])
def test_provider_status(monkeypatch, env, configured, detail):
    for k in ("AGENT_PROVIDER", "GROQ_API_KEY", "AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    ok, msg = Settings.from_env().provider_status()
    assert ok is configured and detail in msg and "gsk_test_not_real" not in msg


@pytest.mark.parametrize("env, needle", [({}, "AGENT_PROVIDER is not set"),
                                         ({"AGENT_PROVIDER": "groq"}, "GROQ_API_KEY is not set")])
def test_investigate_without_provider_is_an_explicit_503(monkeypatch, env, needle):
    r = client(monkeypatch, **env).post("/api/agent/investigate", json={"pr_id": "PR-104"})
    assert r.status_code == 503 and r.json()["error"] == "agent_provider_not_configured"
    assert needle in r.json()["message"]


def test_request_validation(monkeypatch):
    c = client(monkeypatch)
    assert c.post("/api/analyze", json={"pr_id": "MATCH (n) DETACH DELETE n"}).status_code == 422
    assert c.post("/api/analyze", json={}).status_code == 422
    assert c.post("/api/simulate", json={**SIM, "consumer": "Bad Name!"}).status_code == 422
    assert c.post("/api/simulate", json={**SIM, "assumption": "x"}).status_code == 422
    assert c.post("/api/agent/investigate", json={"pr_id": "PR-104", "question": "q" * 501}).status_code == 422
    r = c.post("/api/analyze", json={"pr_id": "PR-999"})
    assert r.status_code == 404 and r.json()["error"] == "unknown_scenario"


def test_scenarios_and_openapi(monkeypatch):
    c = client(monkeypatch)
    sc = c.get("/api/scenarios").json()
    assert [s["pr_id"] for s in sc] == ["PR-104", "PR-108", "PR-112", "PR-114"]
    assert all(s["data_label"].startswith("SYNTHETIC") and s["new_type"] for s in sc)
    spec = c.get("/openapi.json").json()
    assert {"/health", "/ready", "/api/scenarios", "/api/analyze", "/api/simulate",
            "/api/agent/investigate"} <= set(spec["paths"])
    assert spec["components"]["schemas"]["SimulateRequest"]["examples"]
    sim_props = spec["components"]["schemas"]["SimulateResponse"]["properties"]
    assert {"before", "after", "changes", "counterfactual", "code_level_correctness"} <= set(sim_props)


def test_database_unavailable_is_a_safe_503(monkeypatch):
    c = client(monkeypatch, FALKORDB_PORT="6390")
    h = c.get("/health")
    assert h.status_code == 200 and h.json()["falkordb"] == "down"
    assert c.get("/ready").status_code == 503
    r = c.post("/api/analyze", json={"pr_id": "PR-104"})
    assert r.status_code == 503 and r.json() == {"error": "graph_unavailable", "message": "FalkorDB is unreachable."}
    r = c.post("/api/simulate", json=SIM)
    assert r.status_code == 503 and "6390" not in r.text and "Traceback" not in r.text


def test_provider_errors_map_to_status_codes(monkeypatch):
    async def boom(*a, **k):
        raise AG.ProviderError("provider_rate_limited", "Groq rate limit reached (HTTP 429); retry after 7s.", "7")
    c = client(monkeypatch, AGENT_PROVIDER="deterministic")
    monkeypatch.setattr(AG, "investigate", boom)
    r = c.post("/api/agent/investigate", json={"pr_id": "PR-104"})
    assert r.status_code == 429 and r.headers["retry-after"] == "7" and r.json()["error"] == "provider_rate_limited"


# --- live graph ----------------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize("pr, verdict", [("PR-104", "BLOCK"), ("PR-108", "WARN"),
                                         ("PR-112", "CLEAR"), ("PR-114", "WARN")])
def test_analyze_verdicts(monkeypatch, scratch_graph, pr, verdict):
    r = client(monkeypatch, BLASTGATE_GRAPH=scratch_graph.name).post("/api/analyze", json={"pr_id": pr})
    assert r.status_code == 200 and r.json()["verdict"] == verdict
    if pr == "PR-104":
        assert r.json()["exposure"]["label"].startswith("ASSUMED") and len(r.json()["slas"]) == 3
    if pr == "PR-114":
        body = r.json()
        assert [a["service"] for a in body["affected_services"]] == ["storefront-bff"]
        assert body["slas"] == [] and body["exposure"] is None
        assert [i["incident_id"] for i in body["incidents"]] == ["INC-1877"]
        assert [rev["handle"] for rev in body["reviewers"]] == ["eng-charlie"]


@pytest.mark.integration
def test_simulate_is_counterfactual_and_contained(monkeypatch, scratch_graph):
    from blastgate.graph import fingerprint
    before = fingerprint(scratch_graph)
    c = client(monkeypatch, BLASTGATE_GRAPH=scratch_graph.name)
    r = c.post("/api/simulate", json=SIM).json()
    assert r["counterfactual"]["graph_verdict_under_assumption"] == "CLEAR"
    assert r["counterfactual"]["production_verdict"] == "BLOCK"
    assert r["before"]["verdict"] == "BLOCK" and r["after"]["verdict"] == "CLEAR"
    assert len(r["before"]["affected_services"]) == 5 and r["after"]["affected_services"] == []
    assert r["before"]["exposure"]["assumed_total_usd"] == 43000.0 and r["after"]["exposure"] is None
    assert r["changes"]["fallback_before"] is False and r["changes"]["fallback_after"] is True
    assert set(r["changes"]["removed_affected_services"]) == {
        "checkout-api", "order-orchestrator", "storefront-bff", "invoice-worker", "partner-gateway"}
    assert r["code_level_correctness"].startswith("UNVERIFIED") and r["original_unchanged"]
    assert fingerprint(scratch_graph) == before
    assert c.post("/api/simulate", json={**SIM, "consumer": "search-indexer"}).status_code == 400


@pytest.mark.integration
def test_investigate_deterministic_demo_over_real_mcp(monkeypatch, scratch_graph):
    c = client(monkeypatch, AGENT_PROVIDER="deterministic", BLASTGATE_GRAPH=scratch_graph.name)
    r = c.post("/api/agent/investigate", json={"pr_id": "PR-104", "simulate": {k: SIM[k] for k in ("consumer", "assumption")}})
    j = r.json()
    assert r.status_code == 200 and j["verdict"] == "BLOCK" and j["tool_calls"] == 6
    assert j["agent_mode"].startswith("deterministic-demo") and j["simulation"]["counterfactual"]["production_verdict"] == "BLOCK"


def test_groq_failure_never_becomes_a_demo_success(monkeypatch, tool_specs):
    """AGENT_PROVIDER=groq + a Groq tool_use_failed twice -> 502 typed error, not a deterministic-demo report."""
    import json

    import groq
    import httpx

    def handler(request):
        return httpx.Response(400, json={"error": {"code": "tool_use_failed", "failed_generation": "x"}})

    class Tools:   # offline stand-in for the MCP client; the failure happens before any tool runs
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return None
        async def list_tools(self): return tool_specs
        async def call(self, name, args): raise AssertionError("no tool should run")

    scripted = AG.GroqPlanner("openai/gpt-oss-20b", client=groq.AsyncGroq(
        api_key="test-key-not-real", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    c = client(monkeypatch, local=True, AGENT_PROVIDER="groq", GROQ_API_KEY="test-key-not-real")
    monkeypatch.setattr(Settings, "planner", lambda self: scripted)
    c.app.state.tool_client_factory = Tools
    r = c.post("/api/agent/investigate", json={"pr_id": "PR-112"})
    body = r.json()
    assert r.status_code == 502 and body["error"] == "provider_bad_request"
    assert "verdict" not in body and "agent_mode" not in body and "deterministic" not in json.dumps(body)
    assert "test-key-not-real" not in r.text
