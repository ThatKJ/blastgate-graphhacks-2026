"""Investigation memory: a separate FalkorDB graph (BLASTGATE_MEMORY_GRAPH) shared across runs and agents.

Opt-in. The service graph is never written. Every node is keyed by an identifier a tool or the deterministic
engine actually returned (contract_id.field, service name, SLA id, incident id, engineer handle, PR id, git
base sha); names are never fuzzy-matched. Writers:
- the investigation agent (api.investigate_route) records each completed investigation;
- the code-graph reviewer (blastgate.codegraph, a separate process) records each validated contract change.
Readers get bounded history for the SAME contract field. History is context, never evidence: the current graph
revalidates it (revalidate()) and the verdict is computed without it.
Each record is ONE Cypher query (atomic in FalkorDB) using MERGE, so a retry with the same id is idempotent.
"""
import os
import uuid
from datetime import datetime, timezone

HISTORY_LIMIT = 5
NOTE = ("History from earlier runs. It guides what to check but is not evidence: every fact below is revalidated "
        "against the current graph, and the verdict is computed without it.")

RECORD_INVESTIGATION = """
MERGE (f:ContractField {key: $field_key}) SET f.contract_id = $contract_id, f.field = $field
MERGE (ch:Change {key: $change_key}) SET ch.pr_id = $pr_id, ch.source = $source, ch.change = $change
MERGE (ch)-[:MODIFIES]->(f)
MERGE (i:Investigation {id: $id})
SET i.at = $at, i.pr_id = $pr_id, i.verdict = $verdict, i.reasons = $reasons, i.agent_mode = $agent_mode,
    i.stop_reason = $stop_reason, i.tool_calls = $tool_calls, i.tools = $tools, i.source = $source
MERGE (i)-[:EXAMINED]->(ch)
FOREACH (a IN $affected | MERGE (s:Service {name: a.service})
    MERGE (i)-[r:FOUND_AFFECTED]->(s) SET r.depth = a.depth, r.tier = a.tier)
FOREACH (x IN $slas | MERGE (s:SLA {sla_id: x}) MERGE (i)-[:FOUND_AT_RISK]->(s))
FOREACH (x IN $incidents | MERGE (n:Incident {incident_id: x}) MERGE (i)-[:CITED]->(n))
FOREACH (r IN $reviewers | MERGE (e:Engineer {handle: r.handle})
    MERGE (i)-[rr:RECOMMENDED_REVIEWER]->(e) SET rr.resolved_incidents = r.resolved_incidents,
    rr.authored_services = r.authored_services)
FOREACH (p IN $prior | MERGE (pi:Investigation {id: p}) MERGE (i)-[:FOLLOWS]->(pi))
FOREACH (c IN $code_review | MERGE (cr:CodeReview {id: c}) MERGE (i)-[:BUILDS_ON]->(cr))
RETURN i.id
"""

RECORD_CODE_REVIEW = """
MERGE (f:ContractField {key: $field_key}) SET f.contract_id = $contract_id, f.field = $field
MERGE (cr:CodeReview {id: $id})
SET cr.at = $at, cr.base = $base, cr.symbol = $symbol, cr.code_field = $code_field, cr.kind = $kind,
    cr.old_type = $old_type, cr.new_type = $new_type, cr.verdict = $verdict, cr.dependents = $dependents,
    cr.routes = $routes, cr.uncertainties = $uncertainties
MERGE (cr)-[:DETECTED_CHANGE_TO]->(f)
FOREACH (s IN $services | MERGE (sv:Service {name: s}) MERGE (cr)-[:CODE_DEPENDENT_IN]->(sv))
RETURN cr.id
"""

RECALL_INVESTIGATIONS = """
MATCH (:ContractField {key: $field_key})<-[:MODIFIES]-(ch:Change)<-[:EXAMINED]-(i:Investigation)
WITH i, ch ORDER BY i.at DESC LIMIT $limit
OPTIONAL MATCH (i)-[a:FOUND_AFFECTED]->(s:Service)
WITH i, ch, collect(DISTINCT s.name) AS affected
OPTIONAL MATCH (i)-[:CITED]->(n:Incident)
WITH i, ch, affected, collect(DISTINCT n.incident_id) AS incidents
OPTIONAL MATCH (i)-[:RECOMMENDED_REVIEWER]->(e:Engineer)
RETURN i.id, i.at, i.pr_id, i.verdict, i.agent_mode, i.source, ch.change, affected, incidents,
       collect(DISTINCT e.handle)
ORDER BY i.at DESC
"""

RECALL_CODE_REVIEWS = """
MATCH (cr:CodeReview)-[:DETECTED_CHANGE_TO]->(:ContractField {key: $field_key})
WITH cr ORDER BY cr.at DESC LIMIT $limit
OPTIONAL MATCH (cr)-[:CODE_DEPENDENT_IN]->(s:Service)
RETURN cr.id, cr.at, cr.base, cr.symbol, cr.code_field, cr.kind, cr.old_type, cr.new_type, cr.verdict,
       cr.dependents, cr.routes, cr.uncertainties, collect(s.name)
ORDER BY cr.at DESC
"""


def graph_name() -> str | None:
    return os.getenv("BLASTGATE_MEMORY_GRAPH") or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def field_key(contract_id: str, field: str) -> str:
    return f"{contract_id}.{field}"


def code_review_id(base: str, symbol: str, field: str) -> str:
    return f"{base}:{symbol}.{field}"


def recall(db, name: str, contract_id: str, field: str, limit: int = HISTORY_LIMIT) -> dict:
    """Bounded history for exactly this contract field. An absent memory graph means no history yet."""
    if name not in db.list_graphs():
        return {"investigations": [], "code_reviews": []}
    g, p = db.select_graph(name), {"field_key": field_key(contract_id, field), "limit": limit}
    inv = [{"id": r[0], "at": r[1], "pr_id": r[2], "verdict": r[3], "agent_mode": r[4], "source": r[5],
            "change": r[6], "affected_services": sorted(r[7]), "incidents": sorted(r[8]), "reviewers": sorted(r[9])}
           for r in g.ro_query(RECALL_INVESTIGATIONS, p).result_set]
    crs = [{"id": r[0], "at": r[1], "base": r[2], "symbol": r[3], "code_field": r[4], "kind": r[5],
            "old_type": r[6], "new_type": r[7], "verdict": r[8], "dependents": r[9], "routes": r[10],
            "uncertainties": r[11], "code_dependent_services": sorted(r[12])}
           for r in g.ro_query(RECALL_CODE_REVIEWS, p).result_set]
    return {"investigations": inv, "code_reviews": crs}


def revalidate(history: dict, report) -> list:
    """Check each DISTINCT remembered fact against the CURRENT deterministic report (one row per fact, listing
    every earlier record that held it). Memory never overrides the current graph."""
    now_aff = {a["service"] for a in report.affected_services}
    now_inc = {i["incident_id"] for i in (report.incidents or [])}
    now_cons = {c["service"] for c in report.consumers}
    now_verdict = report.verdict.value
    rows: dict = {}

    def add(fact, now, source):
        rows.setdefault((fact, now), []).append(source)

    for h in history["investigations"]:
        for s in h["affected_services"]:
            add(f"{s} was affected", "still affected (current graph)" if s in now_aff
                else "not affected in the current graph", h["id"])
        for n in h["incidents"]:
            add(f"incident {n} was cited", "still linked to this contract" if n in now_inc
                else "not returned by the current graph", h["id"])
        if h["verdict"] != now_verdict:
            add(f"earlier verdict {h['verdict']}",
                f"current deterministic verdict is {now_verdict}; the earlier verdict is not reused", h["id"])
    for c in history["code_reviews"]:
        for s in c["code_dependent_services"]:
            add(f"code graph found {s} handling {c['symbol']}", "consumer in the current service graph"
                if s in now_cons else "no CONSUMES_FIELD edge in the current service graph: unverified", c["id"])
    return [{"fact": f, "now": n, "from": src} for (f, n), src in rows.items()]


def record_investigation(db, name: str, inv, report, scenario, history: dict, code_review: str | None) -> str:
    """Persist one completed investigation and its validated links. Returns the investigation id."""
    iid = f"inv-{uuid.uuid4().hex[:12]}"
    key = field_key(scenario.contract_id, scenario.field)
    db.select_graph(name).query(RECORD_INVESTIGATION, {
        "id": iid, "at": _now(), "field_key": key, "contract_id": scenario.contract_id, "field": scenario.field,
        "change_key": f"{scenario.pr_id}:{key}:{scenario.change}",
        "pr_id": scenario.pr_id, "source": scenario.source, "change": scenario.change,
        "verdict": inv.verdict, "reasons": list(inv.verdict_reasons), "agent_mode": inv.agent_mode,
        "stop_reason": inv.stop_reason, "tool_calls": inv.tool_calls,
        "tools": [t.tool for t in inv.tool_trace if t.executed],
        "affected": [{"service": a["service"], "depth": a["depth"], "tier": a["tier"]} for a in report.affected_services],
        "slas": [s["sla_id"] for s in report.slas],
        "incidents": [i["incident_id"] for i in (report.incidents or [])],
        "reviewers": [{"handle": r["handle"], "resolved_incidents": r["resolved_incidents"],
                       "authored_services": r["authored_services"]} for r in (report.reviewers or [])],
        "prior": [h["id"] for h in history["investigations"][:1]],   # FOLLOWS the most recent prior only
        # Link only to a code review that recall() actually returned, so MERGE never invents a stub node.
        "code_review": [code_review] if code_review in {c["id"] for c in history["code_reviews"]} else [],
    })
    return iid


def record_code_review(db, name: str, base: str, item: dict) -> str:
    """Persist one VALIDATED code-graph contract change (called by blastgate.codegraph, a separate process)."""
    sc = item["scenario"]
    cid = code_review_id(base, item["symbol"], item["field"])
    db.select_graph(name).query(RECORD_CODE_REVIEW, {
        "id": cid, "at": _now(), "field_key": field_key(sc["contract_id"], sc["field"]),
        "contract_id": sc["contract_id"], "field": sc["field"], "base": base, "symbol": item["symbol"],
        "code_field": item["field"], "kind": item["kind"], "old_type": item["old_type"] or "",
        "new_type": item["new_type"] or "", "verdict": item["verdict"],
        "dependents": len(item["code_impact"]["dependents"]), "routes": len(item["code_impact"]["routes"]),
        "uncertainties": len(item["uncertainties"]),
        # consumer side only: the producer handles the type by definition
        "services": sorted({d["service"] for d in item["code_impact"]["dependents"]
                            if d["mapping"] == "mapped" and d["service"] and d["service"] != sc["producer"]}),
    })
    return cid
