"""Agent evaluation harness: 12 synthetic investigation tasks, run end to end (real MCP + live FalkorDB).

Default planner: the labelled deterministic demo planner (measures tools, guards, grounding, verdicts).
Opt-in real-LLM evaluation: BLASTGATE_EVAL_LIVE=1 with GROQ_API_KEY set; then tool-selection metrics are
measured but not asserted (model choices vary), while verdict and grounding still are.
Run with `-s` to print the metrics table.
"""
import os
import time
import uuid

import pytest
import redis

from blastgate import agent as AG
from blastgate import tools as T
from blastgate.analysis import AnalysisError, analyze
from blastgate.graph import SCENARIOS, seed

pytestmark = [pytest.mark.anyio, pytest.mark.integration]
LIVE = os.getenv("BLASTGATE_EVAL_LIVE") == "1" and bool(os.getenv("GROQ_API_KEY"))
INVESTIGATION = {"trace_blast_radius", "analyze_network_criticality", "retrieve_incidents_and_reviewers"}
ALL_FIVE = {"checkout-api", "order-orchestrator", "storefront-bff", "invoice-worker", "partner-gateway"}
SIM = {"consumer": "checkout-api", "assumption": "checkout-api adds a tolerant discount_tier parser"}


class RogueSimulator(AG.DeterministicPlanner):
    """Test planner that tries to run an unrequested simulation."""
    mode = "test: rogue planner"

    async def next(self, observations):
        step = await super().next(observations)
        if observations and observations[-1].name == "inspect_change":
            return [AG.ToolCall(id="rogue", name="simulate_mitigation",
                                arguments={"pr_id": self.task["pr_id"], **SIM})]
        return step


class FailingProvider(AG.DeterministicPlanner):
    mode = "test: failing provider"

    async def next(self, observations):
        raise AG.ProviderError("provider_network", "Network error reaching Groq.")


TASKS = [
    dict(id="T01 breaking change hits critical service", pr="PR-104", verdict="BLOCK", affected=ALL_FIVE,
         required={"trace_blast_radius", "retrieve_incidents_and_reviewers"}, evidence={"Incident:INC-2041"}),
    dict(id="T02 incomplete consumer coverage", pr="PR-108", verdict="WARN", forbidden=INVESTIGATION,
         unknown="incomplete"),
    dict(id="T03 safe compatible change", pr="PR-112", verdict="CLEAR", affected=set(), forbidden=INVESTIGATION),
    dict(id="T04 unknown field id", pr="PR-104", verdict="WARN", forbidden=INVESTIGATION,
         mutation="MATCH (f:SchemaField {name:'discount_tier'}) DETACH DELETE f"),
    dict(id="T05 producer/contract mismatch", pr="PR-104", verdict="WARN", forbidden=INVESTIGATION,
         mutation="MATCH (:Service {name:'pricing-engine'})-[e:EXPOSES]->(c:Contract {contract_id:'pricing-api-v2'}) "
                  "DELETE e WITH c MATCH (s:Service {name:'catalog-service'}) CREATE (s)-[:EXPOSES]->(c)"),
    dict(id="T06 duplicate relationship data", pr="PR-104", verdict="BLOCK", affected=ALL_FIVE,
         mutation="MATCH (s:Service {name:'checkout-api'})-[:CONSUMES_FIELD]->(f:SchemaField {name:'discount_tier'}) "
                  "CREATE (s)-[:CONSUMES_FIELD {fallback_enabled:false, evidence_source:'dup'}]->(f)"),
    dict(id="T07 missing SLA data", pr="PR-104", verdict="BLOCK", affected=ALL_FIVE, no_sla=True,
         mutation="MATCH ()-[b:BACKS_SLA]->() DELETE b"),
    dict(id="T08 irrelevant incident history", pr="PR-104", verdict="BLOCK",
         evidence={"Incident:INC-2041"}, absent={"Incident:INC-1877", "Engineer:eng-charlie"}),
    dict(id="T09 requested simulation", pr="PR-104", verdict="BLOCK", simulate=True,
         required={"simulate_mitigation"}),
    dict(id="T10 unrequested simulation attempt", pr="PR-104", verdict="BLOCK", planner=RogueSimulator,
         forbidden={"simulate_mitigation"}, rejected="simulation was not requested by the user"),
    dict(id="T11 database failure", pr="PR-104", db_down=True,
         error=(AnalysisError, redis.exceptions.ConnectionError)),
    dict(id="T12 provider failure", pr="PR-104", planner=FailingProvider, error=AG.ProviderError),
]
RESULTS = []


def planner_for(task):
    if task.get("planner"):
        return task["planner"]()
    return AG.GroqPlanner(os.getenv("AGENT_MODEL", "openai/gpt-oss-20b")) if LIVE else AG.DeterministicPlanner()


@pytest.mark.parametrize("task", TASKS, ids=[t["id"][:3] for t in TASKS])
async def test_eval_task(task, live_db, monkeypatch):
    name = f"blastgate_eval_{uuid.uuid4().hex[:8]}"
    g = seed(live_db, name)
    try:
        if task.get("mutation"):
            g.query(task["mutation"])
        if task.get("db_down"):
            monkeypatch.setenv("FALKORDB_PORT", "6390")   # MCP subprocess and verdict step both lose the DB
        ctx = T.Ctx(name)
        req = AG.InvestigateRequest(pr_id=task["pr"], simulate=SIM if task.get("simulate") else None)
        t0 = time.monotonic()
        try:
            rep = await AG.investigate(req, planner_for(task), AG.McpToolClient(name),
                                       verdict_fn=lambda pr: analyze(ctx.graph, SCENARIOS[pr]),
                                       known_services=lambda n: T.known_services(ctx, n), total_timeout_s=90)
        except Exception as e:
            assert task.get("error") and isinstance(e, task["error"]), f"unexpected {type(e).__name__}"
            RESULTS.append({"id": task["id"], "ok": True, "verdict": f"no verdict ({type(e).__name__})",
                            "calls": len(getattr(e, "trace", [])), "ms": int((time.monotonic() - t0) * 1000),
                            "grounded": "n/a", "selection": "n/a", "unsupported": 0})
            return
        assert not task.get("error"), "expected a safe failure, got a verdict"
        executed = {t.tool for t in rep.tool_trace if t.executed}
        evidence = {e for t in rep.tool_trace if t.executed for e in t.evidence}
        affected = {f.evidence[0].split(":", 1)[1] for f in rep.verified_facts
                    if f.source_tool == "trace_blast_radius" and "affected at depth" in f.claim}
        grounded = all(f.evidence and set(f.evidence) <= evidence for f in rep.verified_facts)
        selection = task.get("required", set()) <= executed and not (task.get("forbidden", set()) & executed)
        checks = {
            "verdict": rep.verdict == task["verdict"],
            "affected": "affected" not in task or affected == task["affected"],
            "evidence": task.get("evidence", set()) <= evidence and not (task.get("absent", set()) & evidence),
            "grounded": grounded,
            "no_sla": not task.get("no_sla") or not any("SLA" in f.claim for f in rep.verified_facts),
            "unknown": not task.get("unknown") or any(task["unknown"] in u for u in rep.unknowns),
            "rejected": not task.get("rejected") or any(t.rejected_reason == task["rejected"] for t in rep.tool_trace),
            "cap": rep.tool_calls <= AG.MAX_TOOL_CALLS,
            "selection": selection or LIVE,
        }
        RESULTS.append({"id": task["id"], "ok": all(checks.values()), "verdict": rep.verdict,
                        "calls": rep.tool_calls, "ms": rep.latency_ms, "grounded": grounded,
                        "selection": selection, "unsupported": len(rep.unsupported_references)})
        assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    finally:
        if name in live_db.list_graphs():   # live_db keeps its own connection even if the env port was broken
            live_db.select_graph(name).delete()


def test_eval_report():
    """Prints the metrics table (pytest -s). Runs last in this module."""
    if not RESULTS:
        pytest.skip("no eval tasks ran")
    n = len(RESULTS)
    scored = [r for r in RESULTS if r["grounded"] != "n/a"]
    print(f"\n\nBlastGate agent eval - planner: {'groq (LIVE)' if LIVE else 'deterministic-demo (no LLM)'}")
    print(f"{'task':42} {'ok':4} {'verdict':30} {'calls':5} {'ms':>6}")
    for r in RESULTS:
        print(f"{r['id'][:42]:42} {'PASS' if r['ok'] else 'FAIL':4} {r['verdict']:30} {r['calls']:5} {r['ms']:6}")
    print(f"tasks passed {sum(r['ok'] for r in RESULTS)}/{n} | grounding {sum(r['grounded'] is True for r in scored)}"
          f"/{len(scored)} | tool selection {sum(r['selection'] is True for r in scored)}/{len(scored)} | "
          f"unsupported refs {sum(r['unsupported'] for r in RESULTS)} | mean latency "
          f"{sum(r['ms'] for r in RESULTS) // n} ms | mean tool calls {sum(r['calls'] for r in RESULTS) / n:.1f}")
