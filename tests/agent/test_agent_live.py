"""End-to-end agent runs: real MCP subprocess + live FalkorDB.

- DeterministicPlanner: the labelled rule-based demo planner.
- Groq loop: SCRIPTED MOCK Groq responses (no key, no network) driving the REAL MCP tools, proving the
  chat-completions message loop, tool-call id association and result-dependent selection end to end.
"""
import json

import groq
import httpx
import pytest

from blastgate import agent as AG
from blastgate import tools as T
from blastgate.analysis import analyze
from blastgate.graph import SCENARIOS

pytestmark = [pytest.mark.anyio, pytest.mark.integration]
SIM = {"consumer": "checkout-api", "assumption": "checkout-api adds a tolerant discount_tier parser"}


async def investigate(graph, req, planner):
    ctx = T.Ctx(graph.name)
    return await AG.investigate(req, planner, AG.McpToolClient(graph.name),
                                verdict_fn=lambda pr: analyze(ctx.graph, SCENARIOS[pr]),
                                known_services=lambda names: T.known_services(ctx, names), total_timeout_s=60)


@pytest.mark.parametrize("pr, verdict, path", [
    ("PR-104", "BLOCK", ["inspect_change", "inspect_contract_coverage", "trace_blast_radius",
                         "analyze_network_criticality", "retrieve_incidents_and_reviewers"]),
    ("PR-108", "WARN", ["inspect_change", "inspect_contract_coverage"]),
    ("PR-112", "CLEAR", ["inspect_change", "inspect_contract_coverage"]),
    ("PR-114", "WARN", ["inspect_change", "inspect_contract_coverage", "trace_blast_radius",
                        "analyze_network_criticality", "retrieve_incidents_and_reviewers"]),
])
async def test_deterministic_agent_paths_and_verdicts(scratch_graph, pr, verdict, path):
    rep = await investigate(scratch_graph, AG.InvestigateRequest(pr_id=pr), AG.DeterministicPlanner())
    assert rep.verdict == verdict and [t.tool for t in rep.tool_trace] == path and rep.stop_reason == "completed"
    assert rep.agent_mode.startswith("deterministic-demo") and rep.unsupported_references == []
    if pr == "PR-108":
        assert any("incomplete" in u for u in rep.unknowns)
    if pr == "PR-112":
        assert not any("affected at depth" in f.claim for f in rep.verified_facts)   # no invented impact
    if pr == "PR-104":
        claims = " ".join(f.claim for f in rep.verified_facts)
        assert "INC-2041" in claims and "eng-alpha" in claims and "invoice-worker" in claims


async def test_requested_simulation_runs_and_stays_counterfactual(scratch_graph):
    rep = await investigate(scratch_graph, AG.InvestigateRequest(pr_id="PR-104", simulate=SIM),
                            AG.DeterministicPlanner())
    assert rep.tool_calls == 6 and rep.tool_trace[-1].tool == "simulate_mitigation"
    assert rep.verdict == "BLOCK"
    cf = rep.simulation["counterfactual"]
    assert cf["graph_verdict_under_assumption"] == "CLEAR" and cf["production_verdict"] == "BLOCK"
    assert cf["verified_by_contract_or_integration_tests"] is False
    assert all(r.startswith("[COUNTERFACTUAL") for r in rep.simulation["after_reasons"])


def groq_script():
    """A scripted mock 'model' that decides its next call from the REAL tool results it receives."""
    sent = []

    def reply(message):
        return httpx.Response(200, json={"id": "t", "object": "chat.completion", "created": 0, "model": "m",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", **message}}]})

    def call(n, name, args):
        return reply({"content": None, "tool_calls": [{"id": f"call_{n}", "type": "function",
                                                       "function": {"name": name, "arguments": json.dumps(args)}}]})

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        tools_seen = [m for m in body["messages"] if m["role"] == "tool"]
        pr = body["messages"][1]["content"].split()[1].rstrip(".")
        n = len(tools_seen)
        if n == 0:
            return call(n, "inspect_contract_coverage", {"pr_id": pr})
        last = json.loads(tools_seen[-1]["content"])
        if tools_seen[-1]["name"] == "inspect_contract_coverage" and last["data"]["vulnerable_consumers"]:
            return call(n, "trace_blast_radius", {"pr_id": pr})
        if tools_seen[-1]["name"] == "trace_blast_radius":
            svcs = [a["service"] for a in last["data"]["affected_services"]]
            return call(n, "retrieve_incidents_and_reviewers", {"pr_id": pr, "services": svcs})
        return call(n, "submit_report", {"summary": f"Scripted test summary after {n} tool(s).",
                                         "recommended_verdict": "CLEAR"})

    client = groq.AsyncGroq(api_key="test-key-not-real", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return AG.GroqPlanner("openai/gpt-oss-20b", client=client), sent


@pytest.mark.parametrize("pr, expected_path", [
    ("PR-104", ["inspect_contract_coverage", "trace_blast_radius", "retrieve_incidents_and_reviewers"]),
    ("PR-112", ["inspect_contract_coverage"]),
])
async def test_groq_loop_over_real_mcp_selects_from_real_results(scratch_graph, pr, expected_path):
    planner, sent = groq_script()
    rep = await investigate(scratch_graph, AG.InvestigateRequest(pr_id=pr), planner)
    assert [t.tool for t in rep.tool_trace] == expected_path and all(t.executed for t in rep.tool_trace)
    assert rep.agent_mode == "llm: groq/openai/gpt-oss-20b"
    # Every tool message answers the id of the assistant tool call immediately before it.
    msgs = sent[-1]["messages"]
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            assert msgs[i - 1]["tool_calls"][0]["id"] == m["tool_call_id"]
    # The scripted model 'recommended' CLEAR; the deterministic verdict wins where they differ.
    assert rep.verdict == {"PR-104": "BLOCK", "PR-112": "CLEAR"}[pr]
    if pr == "PR-104":
        assert any("recommended CLEAR" in w for w in rep.warnings)


async def test_provider_error_through_real_mcp_is_not_wrapped(scratch_graph):
    """Regression: raised inside the MCP session it surfaced as an ExceptionGroup -> HTTP 500."""
    class Failing(AG.DeterministicPlanner):
        mode = "test: failing provider"

        async def next(self, observations):
            if not observations:
                return await super().next(observations)
            raise AG.ProviderError("provider_rate_limited", "Groq rate limit reached (HTTP 429).", "5")

    with pytest.raises(AG.ProviderError) as e:
        await investigate(scratch_graph, AG.InvestigateRequest(pr_id="PR-104"), Failing())
    assert e.value.code == "provider_rate_limited" and [t.tool for t in e.value.trace] == ["inspect_change"]


@pytest.mark.parametrize("code, status", [("provider_rate_limited", 429), ("provider_auth", 502),
                                          ("provider_timeout", 504)])
def test_api_maps_real_provider_errors(monkeypatch, scratch_graph, code, status):
    from fastapi.testclient import TestClient

    from blastgate.api import Settings, create_app

    class Failing(AG.DeterministicPlanner):
        mode = "test: failing provider"

        async def next(self, observations):
            raise AG.ProviderError(code, "safe message", "5" if code == "provider_rate_limited" else None)

    monkeypatch.setenv("AGENT_PROVIDER", "deterministic")
    monkeypatch.setenv("BLASTGATE_GRAPH", scratch_graph.name)
    monkeypatch.setattr(Settings, "planner", lambda self: Failing())
    r = TestClient(create_app(), raise_server_exceptions=False).post("/api/agent/investigate", json={"pr_id": "PR-104"})
    assert r.status_code == status and r.json()["error"] == code
