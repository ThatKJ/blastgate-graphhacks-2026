"""The full Track 01 loop on real infrastructure: a saved code-graph review -> PR-900 -> real MCP server
subprocess -> six allowlisted tools -> deterministic verdict -> counterfactual on a graph copy. No LLM.

Registration happens at import time, so the API runs in a fresh subprocess with the operator's
BLASTGATE_CODEGRAPH_REPORT set. Skipped (with the reason shown) if FalkorDB is unreachable.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from blastgate.graph import fingerprint

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "web" / "tests" / "fixtures" / "codegraph_report.json"

DRIVER = """
import json
from fastapi.testclient import TestClient
from blastgate.api import create_app
c = TestClient(create_app())
out = {"scenarios": c.get("/api/scenarios").json()}
out["analyze"] = c.post("/api/analyze", json={"pr_id": "PR-900"}).json()
r = c.post("/api/agent/investigate", json={"pr_id": "PR-900", "simulate": {"consumer": "checkout-api",
           "assumption": "checkout-api adds a backward-compatible fallback for discount_tier"}})
out["investigate_status"], out["investigate"] = r.status_code, r.json()
print("RESULT" + json.dumps(out))
"""


def run_loop(tmp_path, graph):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(json.loads(FIXTURE.read_text())["body"]))
    env = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "AGENT_LIVE_TOKEN")}
    env.update(BLASTGATE_CODEGRAPH_REPORT=str(report), BLASTGATE_GRAPH=graph, AGENT_PROVIDER="deterministic")
    p = subprocess.run([sys.executable, "-c", DRIVER], cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.split("RESULT", 1)[1])


def test_code_graph_change_drives_the_mcp_agent_and_verdict(scratch_graph, live_db, tmp_path):
    before = fingerprint(scratch_graph)
    out = run_loop(tmp_path, scratch_graph.name)

    sc = next(s for s in out["scenarios"] if s["pr_id"] == "PR-900")
    assert sc["source"].startswith("derived by the code graph") and sc["code_change"]["mapping"] == "validated"
    assert (sc["contract_id"], sc["field"], sc["new_type"]) == ("pricing-api-v2", "discount_tier", "dict")

    assert out["analyze"]["verdict"] == "BLOCK"
    inv = out["investigate"]
    assert out["investigate_status"] == 200 and inv["verdict"] == "BLOCK" and inv["stop_reason"] == "completed"
    assert inv["agent_mode"].startswith("deterministic-demo")      # labelled: no LLM chose these tools
    tools = [t["tool"] for t in inv["tool_trace"]]
    assert tools[0] == "inspect_change" and "trace_blast_radius" in tools and tools[-1] == "simulate_mitigation"
    assert all(t["executed"] for t in inv["tool_trace"]) and inv["tool_calls"] == len(tools) <= 6
    assert any("Derived by the code graph from a git diff" in a for a in inv["assumptions"])
    affected = " ".join(inv["inventory"]["affected_services"]["items"])   # e.g. "order-orchestrator (depth 2)"
    assert "order-orchestrator (depth 2)" in affected and "invoice-worker (depth 3)" in affected

    cf = inv["simulation"]["counterfactual"]
    assert (cf["production_verdict"], cf["graph_verdict_under_assumption"]) == ("BLOCK", "CLEAR")
    assert inv["simulation"]["original_unchanged"] and inv["simulation"]["temp_graph_deleted"]
    assert fingerprint(scratch_graph) == before
    assert not any("__sim_" in g for g in live_db.list_graphs())
