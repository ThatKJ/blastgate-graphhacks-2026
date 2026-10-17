"""Real MCP over stdio: the server runs as a subprocess and is reached only through JSON-RPC.
Needs live FalkorDB (skipped with a reason otherwise)."""
import subprocess

import pytest

from blastgate.agent import ALLOWED_TOOLS, McpToolClient
from blastgate.graph import fingerprint

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def server_processes():
    out = subprocess.run(["pgrep", "-f", "blastgate.mcp_server"], capture_output=True, text=True).stdout
    return set(out.split())


async def test_startup_discovery_and_shutdown(live_db, scratch_graph):
    before = server_processes()
    async with McpToolClient(scratch_graph.name) as mcp:
        assert server_processes() - before, "server subprocess should be running"
        specs = {s["name"]: s["input_schema"] for s in await mcp.list_tools()}
    assert set(specs) == ALLOWED_TOOLS                          # exactly the domain tools, no raw Cypher
    assert specs["inspect_change"]["properties"]["pr_id"]["enum"] == ["PR-104", "PR-108", "PR-112", "PR-114"]
    assert {"minimum": 1, "maximum": 8}.items() <= specs["trace_blast_radius"]["properties"]["max_depth"].items()
    assert not any("graph" in p or "query" in p for s in specs.values() for p in s.get("properties", {}))
    assert server_processes() == before, "server subprocess should exit with the session"


async def test_real_results_and_safe_errors(live_db, scratch_graph):
    async with McpToolClient(scratch_graph.name) as mcp:
        res, err = await mcp.call("trace_blast_radius", {"pr_id": "PR-104"})
        assert not err and res["status"] == "warning"
        assert {a["service"] for a in res["data"]["affected_services"]} == {
            "checkout-api", "order-orchestrator", "storefront-bff", "invoice-worker", "partner-gateway"}
        bad, err = await mcp.call("trace_blast_radius", {"pr_id": "PR-104", "max_depth": 0})
        assert err and bad["status"] == "error"
        unknown, err = await mcp.call("query_graph", {"query": "MATCH (n) DETACH DELETE n"})
        assert err and "Unknown tool" in unknown["summary"]


async def test_database_down_is_a_safe_tool_error(live_db, monkeypatch):
    monkeypatch.setenv("FALKORDB_PORT", "6390")   # nothing listens there; passed to the server subprocess
    async with McpToolClient("blastgate_m1") as mcp:
        res, err = await mcp.call("inspect_change", {"pr_id": "PR-104"})
    assert res["status"] == "error" and res["error_code"] == "unavailable"
    assert "6390" not in str(res) and "refused" not in str(res).lower()


async def test_simulation_over_mcp_never_mutates_the_source_graph(live_db, scratch_graph):
    before_fp, before_graphs = fingerprint(scratch_graph), set(live_db.list_graphs())
    async with McpToolClient(scratch_graph.name) as mcp:
        res, err = await mcp.call("simulate_mitigation", {"pr_id": "PR-104", "consumer": "checkout-api",
                                                          "assumption": "checkout-api adds a tolerant parser"})
        bad, _ = await mcp.call("simulate_mitigation", {"pr_id": "PR-104", "consumer": "search-indexer",
                                                        "assumption": "search-indexer adds a parser"})
    assert not err and res["data"]["counterfactual"]["graph_verdict_under_assumption"] == "CLEAR"
    assert res["data"]["original_unchanged"] and res["data"]["temp_graph_deleted"]
    assert bad["error_code"] == "invalid_target"
    assert fingerprint(scratch_graph) == before_fp and set(live_db.list_graphs()) == before_graphs


async def test_server_that_cannot_start_raises_tool_server_error():
    from blastgate.agent import ToolServerError
    client = McpToolClient("blastgate_m1", timeout_s=5)
    client.params = client.params.model_copy(update={"command": "/nonexistent/python"})
    with pytest.raises(ToolServerError, match="failed to start"):
        async with client:
            pass


def test_api_reports_tool_server_unavailable(monkeypatch, live_db):
    from fastapi.testclient import TestClient

    from blastgate.api import create_app
    monkeypatch.setenv("AGENT_PROVIDER", "deterministic")
    app = create_app()
    broken = McpToolClient("blastgate_m1", timeout_s=5)
    broken.params = broken.params.model_copy(update={"command": "/nonexistent/python"})
    app.state.tool_client_factory = lambda: broken
    r = TestClient(app, raise_server_exceptions=False).post("/api/agent/investigate", json={"pr_id": "PR-104"})
    assert r.status_code == 503 and r.json()["error"] == "tool_server_unavailable"
