"""Investigation memory on real FalkorDB, across SEPARATE processes. No LLM.

Each investigation runs the real API + real MCP server subprocess in its own Python process, so "remembers
across runs" is literal. Every test uses its own scratch service graph and its own memory graph.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from blastgate import memory as M
from blastgate.graph import fingerprint

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "web" / "tests" / "fixtures" / "codegraph_report.json"

DRIVER = """
import json, sys
from fastapi.testclient import TestClient
from blastgate.api import create_app
r = TestClient(create_app()).post("/api/agent/investigate", json={"pr_id": sys.argv[1]})
print("RESULT" + json.dumps({"status": r.status_code, "body": r.json()}))
"""


@pytest.fixture
def mem(live_db):
    name = f"blastgate_mem_test_{uuid.uuid4().hex[:8]}"
    yield name
    if name in live_db.list_graphs():
        live_db.select_graph(name).delete()
    live_db.connection.delete(name, f"telemetry{{{name}}}")


def investigate(pr, graph, memory, report=None):
    env = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "AGENT_LIVE_TOKEN")}
    env.update(BLASTGATE_GRAPH=graph, BLASTGATE_MEMORY_GRAPH=memory, AGENT_PROVIDER="deterministic")
    if report:
        env["BLASTGATE_CODEGRAPH_REPORT"] = str(report)
    p = subprocess.run([sys.executable, "-c", DRIVER, pr], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=240)
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout.split("RESULT", 1)[1])
    assert out["status"] == 200, out
    return out["body"]


def test_second_run_recalls_and_revalidates_the_first(scratch_graph, live_db, mem):
    before = fingerprint(scratch_graph)
    first = investigate("PR-104", scratch_graph.name, mem)
    assert first["memory"]["status"] == "recorded" and first["memory"]["prior_investigations"] == []
    second = investigate("PR-104", scratch_graph.name, mem)

    m = second["memory"]
    assert [p["id"] for p in m["prior_investigations"]] == [first["memory"]["recorded_id"]]
    prior = m["prior_investigations"][0]
    assert prior["verdict"] == "BLOCK" and "order-orchestrator" in prior["affected_services"]
    assert prior["incidents"] == ["INC-2041"] and prior["reviewers"] == ["eng-alpha", "eng-bravo"]
    assert {"from": [prior["id"]], "fact": "order-orchestrator was affected", "now": "still affected (current graph)"} \
        in m["revalidation"]
    assert second["verdict"] == first["verdict"] == "BLOCK"
    assert fingerprint(scratch_graph) == before                  # memory never writes the service graph

    g = live_db.select_graph(mem)
    follows = g.ro_query("MATCH (a:Investigation)-[:FOLLOWS]->(b:Investigation) RETURN a.id, b.id").result_set
    assert follows == [[m["recorded_id"], first["memory"]["recorded_id"]]]
    assert g.ro_query("MATCH (:ContractField) RETURN count(*)").result_set == [[1]]   # MERGE: no duplicates


def test_memory_is_scoped_to_the_exact_contract_field(scratch_graph, mem):
    investigate("PR-104", scratch_graph.name, mem)                          # pricing-api-v2.discount_tier
    other = investigate("PR-114", scratch_graph.name, mem)                  # catalog-api-v1.sku
    assert other["memory"]["prior_investigations"] == [] and other["memory"]["revalidation"] == []


def test_a_prior_clear_never_clears_a_new_block(scratch_graph, live_db, mem):
    live_db.select_graph(mem).query(
        "CREATE (:Investigation {id: 'inv-old', at: '2026-01-01T00:00:00+00:00', pr_id: 'PR-104', verdict: 'CLEAR', "
        "source: 'test'})-[:EXAMINED]->(:Change {key: 'old'})-[:MODIFIES]->"
        "(:ContractField {key: 'pricing-api-v2.discount_tier'})")
    r = investigate("PR-104", scratch_graph.name, mem)
    assert r["verdict"] == "BLOCK"
    assert {"from": ["inv-old"], "fact": "earlier verdict CLEAR",
            "now": "current deterministic verdict is BLOCK; the earlier verdict is not reused"} in r["memory"]["revalidation"]


def test_code_reviewer_and_agent_share_state_through_the_memory_graph(scratch_graph, live_db, mem, tmp_path):
    body = json.loads(FIXTURE.read_text())["body"]
    report = tmp_path / "report.json"
    report.write_text(json.dumps(body))
    item = next(c for c in body["contract_changes"] if c["mapping"] == "validated")
    review_id = M.record_code_review(live_db, mem, body["base"], item)      # what blastgate.codegraph writes
    assert M.record_code_review(live_db, mem, body["base"], item) == review_id   # idempotent retry

    r = investigate("PR-900", scratch_graph.name, mem, report=report)      # a different process reads it
    m = r["memory"]
    assert [c["id"] for c in m["code_reviews"]] == [review_id]
    assert m["code_reviews"][0]["code_dependent_services"] == ["checkout-api"]   # producer excluded
    assert {"from": [review_id], "fact": f"code graph found checkout-api handling {item['symbol']}",
            "now": "consumer in the current service graph"} in m["revalidation"]
    g = live_db.select_graph(mem)
    assert g.ro_query("MATCH (i:Investigation)-[:BUILDS_ON]->(c:CodeReview) RETURN i.id, c.id").result_set == \
        [[m["recorded_id"], review_id]]
    assert g.ro_query("MATCH (c:CodeReview) RETURN count(c)").result_set == [[1]]


def test_broken_memory_store_never_breaks_the_investigation(scratch_graph, live_db, mem):
    live_db.connection.set(mem, "not a graph")                              # a plain key where the graph should be
    r = investigate("PR-104", scratch_graph.name, mem)
    assert r["verdict"] == "BLOCK" and r["memory"]["status"] == "record_failed"
    assert r["memory"]["recorded_id"] is None


def test_memory_disabled_by_default_changes_nothing(scratch_graph, live_db):
    env = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "BLASTGATE_MEMORY_GRAPH")}
    env.update(BLASTGATE_GRAPH=scratch_graph.name, AGENT_PROVIDER="deterministic")
    graphs = set(live_db.list_graphs())
    p = subprocess.run([sys.executable, "-c", DRIVER, "PR-104"], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=240)
    body = json.loads(p.stdout.split("RESULT", 1)[1])["body"]
    assert body["memory"] is None and set(live_db.list_graphs()) == graphs
