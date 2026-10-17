"""Connection, deterministic synthetic graph data, scenarios and seeding.

ALL DATA IN THIS MODULE IS SYNTHETIC. Services, engineers, SLAs, penalty rates
and incidents are fabricated for a hackathon demo. They describe no real
company, person, customer or measured loss.
"""
import os
from dataclasses import dataclass

from falkordb import FalkorDB

DEFAULT_GRAPH = "blastgate_m1"
SYNTHETIC = "SYNTHETIC - fabricated demo data, not real customer or production data"


def connect() -> FalkorDB:
    """Connect using FALKORDB_HOST / FALKORDB_PORT / FALKORDB_PASSWORD."""
    return FalkorDB(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        password=os.getenv("FALKORDB_PASSWORD") or None,
        socket_timeout=10,
    )


def graph_name() -> str:
    return os.getenv("BLASTGATE_GRAPH", DEFAULT_GRAPH)


# --- Synthetic data -----------------------------------------------------------

SERVICES = [
    {"name": "pricing-engine", "tier": "critical", "team": "pricing"},
    {"name": "checkout-api", "tier": "critical", "team": "checkout"},
    {"name": "order-orchestrator", "tier": "critical", "team": "fulfilment"},
    {"name": "invoice-worker", "tier": "standard", "team": "billing"},
    {"name": "storefront-bff", "tier": "standard", "team": "web"},
    {"name": "partner-gateway", "tier": "standard", "team": "partners"},
    {"name": "audit-log", "tier": "standard", "team": "platform"},
    {"name": "catalog-service", "tier": "standard", "team": "catalog"},
    {"name": "search-indexer", "tier": "standard", "team": "search"},
]

# (dependent, dependency): if B depends on A we store (B)-[:DEPENDS_ON]->(A).
# Chain: pricing-engine -> checkout-api -> order-orchestrator -> invoice-worker
DEPENDS_ON = [
    ("checkout-api", "pricing-engine"),
    ("order-orchestrator", "checkout-api"),
    ("invoice-worker", "order-orchestrator"),
    ("storefront-bff", "checkout-api"),
    ("storefront-bff", "catalog-service"),
    ("partner-gateway", "order-orchestrator"),
    ("search-indexer", "catalog-service"),
]

CONTRACTS = [
    {"contract_id": "pricing-api-v2", "producer": "pricing-engine", "kind": "REST",
     "consumer_mapping_status": "complete",
     "coverage_note": "All internal consumers indexed from synthetic consumer-contract manifests."},
    {"contract_id": "partner-webhook-v1", "producer": "partner-gateway", "kind": "webhook",
     "consumer_mapping_status": "incomplete",
     "coverage_note": "External partner consumers are not mapped; only internal subscribers are indexed."},
    {"contract_id": "catalog-api-v1", "producer": "catalog-service", "kind": "REST",
     "consumer_mapping_status": "complete",
     "coverage_note": "All internal consumers indexed from synthetic consumer-contract manifests."},
]

FIELDS = [
    {"contract_id": "pricing-api-v2", "name": "discount_tier", "field_type": "string", "required": True},
    {"contract_id": "pricing-api-v2", "name": "final_price", "field_type": "decimal", "required": True},
    {"contract_id": "partner-webhook-v1", "name": "payload_signature", "field_type": "string(hex)", "required": True},
    {"contract_id": "partner-webhook-v1", "name": "event_id", "field_type": "string", "required": True},
    {"contract_id": "catalog-api-v1", "name": "promo_badge", "field_type": "string", "required": False},
    {"contract_id": "catalog-api-v1", "name": "sku", "field_type": "string", "required": True},
]

CONSUMES = [
    {"service": "checkout-api", "contract_id": "pricing-api-v2", "field": "discount_tier",
     "fallback_enabled": False, "evidence_source": "synthetic: checkout-api consumer contract (pact) manifest"},
    {"service": "checkout-api", "contract_id": "pricing-api-v2", "field": "final_price",
     "fallback_enabled": False, "evidence_source": "synthetic: checkout-api consumer contract (pact) manifest"},
    {"service": "audit-log", "contract_id": "partner-webhook-v1", "field": "event_id",
     "fallback_enabled": False, "evidence_source": "synthetic: audit-log subscriber manifest"},
    {"service": "search-indexer", "contract_id": "catalog-api-v1", "field": "promo_badge",
     "fallback_enabled": True, "evidence_source": "synthetic: search-indexer contract test asserting null-safe default"},
    {"service": "storefront-bff", "contract_id": "catalog-api-v1", "field": "sku",
     "fallback_enabled": False, "evidence_source": "synthetic: storefront-bff consumer contract manifest"},
]

SLAS = [
    {"sla_id": "SLA-CHECKOUT-99.95", "service": "checkout-api", "name": "Checkout availability 99.95%",
     "penalty_per_hour_usd": 12000},
    {"sla_id": "SLA-ORDERS-99.9", "service": "order-orchestrator", "name": "Order fulfilment 99.9%",
     "penalty_per_hour_usd": 8000},
    {"sla_id": "SLA-INVOICE-24H", "service": "invoice-worker", "name": "Invoice issued within 24h",
     "penalty_per_hour_usd": 1500},
]

INCIDENTS = [
    {"incident_id": "INC-2041", "contract_id": "pricing-api-v2", "severity": "SEV-2",
     "summary": "discount_tier enum rename broke checkout-api price parsing", "resolved_by": "eng-alpha"},
    {"incident_id": "INC-1877", "contract_id": "catalog-api-v1", "severity": "SEV-4",
     "summary": "sku casing change delayed storefront-bff cache refresh", "resolved_by": "eng-charlie"},
]

ENGINEERS = [
    {"handle": "eng-alpha", "display_name": "Synthetic Engineer Alpha", "authored": ["checkout-api", "pricing-engine"]},
    {"handle": "eng-bravo", "display_name": "Synthetic Engineer Bravo", "authored": ["order-orchestrator"]},
    {"handle": "eng-charlie", "display_name": "Synthetic Engineer Charlie", "authored": ["catalog-service"]},
]


@dataclass(frozen=True)
class Scenario:
    pr_id: str
    title: str
    producer: str
    contract_id: str
    field: str
    change: str
    breaking: bool
    local_tests: str
    assumed_outage_hours: float
    new_type: str = "unspecified"  # declared by the PR; the old type is read from the graph
    source: str = "declared by the PR (synthetic scenario)"  # or derived by the code graph from a git diff


SCENARIOS = {
    s.pr_id: s for s in [
        Scenario("PR-104", "Change discount_tier from string to object", "pricing-engine", "pricing-api-v2",
                 "discount_tier", "type change: string -> object", True, "passing (represented, synthetic)", 2.0,
                 "object"),
        Scenario("PR-108", "Change partner webhook payload_signature encoding", "partner-gateway",
                 "partner-webhook-v1", "payload_signature", "encoding change: hex -> base64", True,
                 "passing (represented, synthetic)", 2.0, "string(base64)"),
        Scenario("PR-112", "Change optional catalog promo_badge format", "catalog-service", "catalog-api-v1",
                 "promo_badge", "format change on optional field", True, "passing (represented, synthetic)", 2.0,
                 "string(badge-v2), optional"),
        Scenario("PR-114", "Change required catalog sku format", "catalog-service", "catalog-api-v1",
                 "sku", "format change: string -> string(urn)", True, "passing (represented, synthetic)", 2.0,
                 "string(urn)"),
    ]
}


def seed(db: FalkorDB, name: str):
    """(Re)build graph `name` from the constants above and return it. Deterministic.

    Only ever drops the graph named `name`; nothing else in the database is touched.
    """
    if name in db.list_graphs():
        db.select_graph(name).delete()
    graph = db.select_graph(name)
    try:
        _create(graph)
        _verify_counts(graph)
    except Exception:
        # A partial graph (e.g. a dropped CONSUMES_FIELD edge) can turn BLOCK into CLEAR. Never leave one.
        if name in db.list_graphs():
            graph.delete()
        raise
    return graph


def _verify_counts(graph) -> None:
    """UNWIND ... MATCH ... CREATE silently skips rows whose MATCH fails; catch that here."""
    expected = {
        "Service": len(SERVICES), "Contract": len(CONTRACTS), "SchemaField": len(FIELDS),
        "SLAContract": len(SLAS), "Incident": len(INCIDENTS), "Engineer": len(ENGINEERS),
        "DEPENDS_ON": len(DEPENDS_ON), "EXPOSES": len(CONTRACTS), "HAS_FIELD": len(FIELDS),
        "CONSUMES_FIELD": len(CONSUMES), "BACKS_SLA": len(SLAS), "CAUSED_BY_CHANGE_IN": len(INCIDENTS),
        "AUTHORED_COMMITS": sum(len(e["authored"]) for e in ENGINEERS), "RESOLVED_INCIDENT": len(INCIDENTS),
    }
    actual = dict(graph.ro_query("MATCH (n) RETURN labels(n)[0], count(n)").result_set)
    actual.update(dict(graph.ro_query("MATCH ()-[r]->() RETURN type(r), count(r)").result_set))
    if actual != expected:
        diff = {k: (expected.get(k, 0), actual.get(k, 0)) for k in expected.keys() | actual.keys()
                if expected.get(k, 0) != actual.get(k, 0)}
        raise RuntimeError(f"seed integrity check failed (expected, actual): {diff}")


def _create(graph) -> None:
    q = graph.query
    q("UNWIND $rows AS r CREATE (:Service {name: r.name, tier: r.tier, team: r.team, synthetic: true})",
      {"rows": SERVICES})
    q("UNWIND $rows AS r MATCH (b:Service {name: r[0]}), (a:Service {name: r[1]}) CREATE (b)-[:DEPENDS_ON]->(a)",
      {"rows": [list(p) for p in DEPENDS_ON]})
    q("UNWIND $rows AS r MATCH (p:Service {name: r.producer}) "
      "CREATE (p)-[:EXPOSES]->(:Contract {contract_id: r.contract_id, kind: r.kind, "
      "consumer_mapping_status: r.consumer_mapping_status, coverage_note: r.coverage_note, synthetic: true})",
      {"rows": CONTRACTS})
    q("UNWIND $rows AS r MATCH (c:Contract {contract_id: r.contract_id}) "
      "CREATE (c)-[:HAS_FIELD]->(:SchemaField {name: r.name, contract_id: r.contract_id, "
      "field_type: r.field_type, required: r.required})", {"rows": FIELDS})
    q("UNWIND $rows AS r MATCH (s:Service {name: r.service}), "
      "(f:SchemaField {name: r.field, contract_id: r.contract_id}) "
      "CREATE (s)-[:CONSUMES_FIELD {fallback_enabled: r.fallback_enabled, evidence_source: r.evidence_source}]->(f)",
      {"rows": CONSUMES})
    q("UNWIND $rows AS r MATCH (s:Service {name: r.service}) "
      "CREATE (s)-[:BACKS_SLA]->(:SLAContract {sla_id: r.sla_id, name: r.name, "
      "penalty_per_hour_usd: r.penalty_per_hour_usd, synthetic: true, label: $label})",
      {"rows": SLAS, "label": SYNTHETIC})
    q("UNWIND $rows AS r MATCH (c:Contract {contract_id: r.contract_id}) "
      "CREATE (:Incident {incident_id: r.incident_id, severity: r.severity, summary: r.summary, "
      "synthetic: true, label: $label})-[:CAUSED_BY_CHANGE_IN]->(c)",
      {"rows": INCIDENTS, "label": SYNTHETIC})
    q("UNWIND $rows AS r CREATE (e:Engineer {handle: r.handle, display_name: r.display_name, synthetic: true}) "
      "WITH e, r UNWIND r.authored AS svc MATCH (s:Service {name: svc}) CREATE (e)-[:AUTHORED_COMMITS]->(s)",
      {"rows": ENGINEERS})
    q("UNWIND $rows AS r MATCH (e:Engineer {handle: r.resolved_by}), (i:Incident {incident_id: r.incident_id}) "
      "CREATE (e)-[:RESOLVED_INCIDENT]->(i)", {"rows": INCIDENTS})


def fingerprint(graph) -> list:
    """Order-independent snapshot of every node and edge, used to prove isolation."""
    nodes = graph.ro_query("MATCH (n) RETURN labels(n), properties(n)").result_set
    edges = graph.ro_query(
        "MATCH (a)-[r]->(b) RETURN labels(a), properties(a), type(r), properties(r), labels(b), properties(b)"
    ).result_set
    return sorted(repr(row) for row in nodes + edges)
