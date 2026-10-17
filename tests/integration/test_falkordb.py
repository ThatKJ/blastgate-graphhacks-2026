"""Real FalkorDB: Cypher, betweenness, GRAPH.COPY, isolation and cleanup.

Skipped (with the reason shown) if FalkorDB is unreachable. Skipped != passed.
Each run uses its own uniquely named graph and deletes it afterwards.
"""
import json
import uuid

import pytest

from blastgate import queries as Q
from blastgate.analysis import analyze
from blastgate.graph import SCENARIOS, Scenario, connect, fingerprint, seed
from blastgate.simulation import simulate_consumer_fallback
from blastgate.verdict import Verdict

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db():
    try:
        client = connect()
        client.connection.ping()
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable ({type(e).__name__}: {e}); integration tests NOT run")
    return client


@pytest.fixture(scope="module")
def graph(db):
    name = f"blastgate_test_{uuid.uuid4().hex[:8]}"
    g = seed(db, name)
    yield g
    if name in db.list_graphs():
        g.delete()


@pytest.mark.parametrize("pr, expected", [("PR-104", Verdict.BLOCK), ("PR-108", Verdict.WARN),
                                          ("PR-112", Verdict.CLEAR)])
def test_scenario_verdicts(graph, pr, expected):
    assert analyze(graph, SCENARIOS[pr]).verdict is expected


def test_pr104_evidence(graph):
    r = analyze(graph, SCENARIOS["PR-104"])
    assert [c["service"] for c in r.vulnerable_consumers] == ["checkout-api"]
    depths = {a["service"]: a["depth"] for a in r.affected_services}
    assert depths == {"checkout-api": 1, "order-orchestrator": 2, "storefront-bff": 2,
                      "invoice-worker": 3, "partner-gateway": 3}
    assert "pricing-engine" not in depths
    assert {s["sla_id"] for s in r.slas} == {"SLA-CHECKOUT-99.95", "SLA-ORDERS-99.9", "SLA-INVOICE-24H"}
    assert all(s["synthetic"] is True for s in r.slas)
    assert r.exposure["assumed_total_usd"] == (12000 + 8000 + 1500) * 2.0
    assert [i["incident_id"] for i in r.incidents] == ["INC-2041"]
    assert r.incidents[0]["resolved_by"] == ["eng-alpha"]
    assert r.reviewers[0]["handle"] == "eng-alpha"
    assert set(r.centrality) == set(depths)


def test_centrality_is_from_falkordb_and_reproducible(db, graph):
    """Scores must equal the raw procedure output for the pinned config, across fresh connections.
    We deliberately do not assert specific values: algo.betweenness is a sampled approximation."""
    svc = ["checkout-api", "order-orchestrator", "storefront-bff", "invoice-worker", "partner-gateway"]
    params = {"services": svc, **Q.CENTRALITY_SAMPLING}
    raw = {n: s for n, s in graph.ro_query(Q.CENTRALITY, params).result_set}
    again = {n: s for n, s in connect().select_graph(graph.name).ro_query(Q.CENTRALITY, params).result_set}
    assert raw == again
    assert analyze(graph, SCENARIOS["PR-104"]).centrality == raw
    assert all(isinstance(s, float) and s >= 0 for s in raw.values())
    assert raw["invoice-worker"] == 0.0  # a leaf (single DEPENDS_ON edge) can never lie between two services


def test_sla_attachment_is_not_a_hop(graph):
    rows = graph.ro_query(Q.DOWNSTREAM, {"consumers": ["checkout-api"]}).result_set
    assert all("SLA" not in str(path) for *_, path in rows)
    assert {r[0]: r[2] for r in rows}["invoice-worker"] == 2  # checkout <- order <- invoice


def test_pr108_and_pr112_skip_investigation_queries(graph):
    for pr in ("PR-108", "PR-112"):
        r = analyze(graph, SCENARIOS[pr])
        assert r.centrality is None and r.incidents is None and r.reviewers is None
    r108 = analyze(graph, SCENARIOS["PR-108"])
    assert r108.consumers == [] and r108.mapping_status == "incomplete"


def test_other_fields_on_same_contract_are_not_confused(graph):
    # audit-log consumes partner-webhook-v1.event_id without fallback; that must not leak into PR-108.
    r = analyze(graph, SCENARIOS["PR-108"])
    assert "audit-log" not in [c["service"] for c in r.consumers]


def test_missing_field_and_wrong_producer_are_warn(graph):
    base = SCENARIOS["PR-104"]
    missing = Scenario("T-1", "t", base.producer, base.contract_id, "no_such_field", "c", True, "n/a", 1.0)
    wrong = Scenario("T-2", "t", "catalog-service", base.contract_id, base.field, "c", True, "n/a", 1.0)
    assert analyze(graph, missing).verdict is Verdict.WARN
    r = analyze(graph, wrong)
    assert r.verdict is Verdict.WARN and not r.contract_found


def test_seed_is_deterministic(db, graph):
    name = f"blastgate_test_{uuid.uuid4().hex[:8]}"
    try:
        assert fingerprint(seed(db, name)) == fingerprint(graph)
        assert fingerprint(seed(db, name)) == fingerprint(graph)  # reseeding replaces, never duplicates
    finally:
        if name in db.list_graphs():
            db.select_graph(name).delete()


def test_counterfactual_clear_on_copy_original_isolated(db, graph):
    before = fingerprint(graph)
    sim = simulate_consumer_fallback(db, graph.name, SCENARIOS["PR-104"], "checkout-api")
    assert sim.before.verdict is Verdict.BLOCK
    assert sim.after.verdict is Verdict.CLEAR
    assert sim.after.vulnerable_consumers == []
    assert sim.temp_graph != graph.name
    assert sim.temp_graph_deleted and sim.temp_graph not in db.list_graphs()
    assert sim.original_unchanged and fingerprint(graph) == before
    assert analyze(graph, SCENARIOS["PR-104"]).verdict is Verdict.BLOCK
    assert sim.label.startswith("Counterfactual graph simulation only.")
    # The copied-graph report carries its own qualification, also after JSON round-trip.
    after = json.loads(json.dumps(sim.after.as_dict(), default=str))
    assert after["counterfactual"]["is_counterfactual"] is True
    assert after["counterfactual"]["verified_by_contract_or_integration_tests"] is False
    assert after["counterfactual"]["production_verdict"] == "BLOCK"
    assert all(r.startswith("[COUNTERFACTUAL") for r in after["reasons"])
    # Normal reports are untouched.
    normal = analyze(graph, SCENARIOS["PR-104"])
    assert normal.counterfactual is None and not normal.decision.reasons[0].startswith("[COUNTERFACTUAL")


def test_counterfactual_bad_target_cleans_up(db, graph):
    before_graphs, before_fp = set(db.list_graphs()), fingerprint(graph)
    with pytest.raises(ValueError):
        simulate_consumer_fallback(db, graph.name, SCENARIOS["PR-104"], "no-such-service")
    assert set(db.list_graphs()) == before_graphs
    assert fingerprint(graph) == before_fp


def test_ro_query_rejects_writes(graph):
    with pytest.raises(Exception, match="read-only"):
        graph.ro_query(Q.SET_FALLBACK, {"producer": "p", "contract": "x", "field": "y", "consumer": "z",
                                        "evidence": "e"})


# --- Audit regressions -------------------------------------------------------

@pytest.fixture
def scratch(db):
    """A private freshly-seeded graph for tests that mutate data."""
    name = f"blastgate_test_{uuid.uuid4().hex[:8]}"
    g = seed(db, name)
    yield g
    if name in db.list_graphs():
        g.delete()


def test_seed_rejects_silently_dropped_rows_and_leaves_no_partial_graph(db, monkeypatch):
    """Defect D1: a typo'd CONSUMES row was skipped by UNWIND/MATCH and PR-104 flipped BLOCK -> CLEAR."""
    import blastgate.graph as G
    monkeypatch.setattr(G, "CONSUMES", [dict(c, service="checkout-apii") if c["field"] == "discount_tier" else c
                                        for c in G.CONSUMES])
    name = f"blastgate_test_{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError, match="CONSUMES_FIELD"):
        G.seed(db, name)
    assert name not in db.list_graphs()


def test_same_contract_id_under_other_producer_does_not_leak(scratch):
    """Defect D2: another producer's same-named contract leaked a vulnerable consumer -> PR-112 BLOCK."""
    scratch.query("MATCH (s:Service {name:'partner-gateway'}) "
                  "CREATE (s)-[:EXPOSES]->(:Contract {contract_id:'catalog-api-v1', consumer_mapping_status:'complete'})"
                  "-[:HAS_FIELD]->(f:SchemaField {name:'promo_badge'}) WITH f "
                  "MATCH (x:Service {name:'checkout-api'}) "
                  "CREATE (x)-[:CONSUMES_FIELD {fallback_enabled:false, evidence_source:'e'}]->(f)")
    r = analyze(scratch, SCENARIOS["PR-112"])
    assert r.verdict is Verdict.CLEAR
    assert [c["service"] for c in r.consumers] == ["search-indexer"]


def test_incidents_and_reviewers_belong_to_the_scenario(graph):
    r = analyze(graph, SCENARIOS["PR-104"])
    assert "INC-1877" not in [i["incident_id"] for i in r.incidents]          # catalog incident
    assert "eng-charlie" not in [x["handle"] for x in r.reviewers]            # catalog engineer
    bravo = next(x for x in r.reviewers if x["handle"] == "eng-bravo")
    assert bravo["resolved_incidents"] == [] and bravo["authored_services"] == ["order-orchestrator"]


def test_duplicate_consumer_edge_does_not_duplicate_services_or_inflate_exposure(scratch):
    scratch.query("MATCH (s:Service {name:'checkout-api'})-[:CONSUMES_FIELD]->(f:SchemaField {name:'discount_tier'}) "
                  "CREATE (s)-[:CONSUMES_FIELD {fallback_enabled:false, evidence_source:'dup'}]->(f)")
    r = analyze(scratch, SCENARIOS["PR-104"])
    names = [a["service"] for a in r.affected_services]
    assert len(names) == len(set(names)) == 5
    assert r.exposure["assumed_total_usd"] == (12000 + 8000 + 1500) * 2.0


def test_simulation_failure_after_write_cleans_up_and_keeps_original(db, scratch, monkeypatch):
    """Fail *after* the mitigation was written to the copy: copy must be deleted, original untouched."""
    import blastgate.simulation as S
    real, calls = S.analyze, []

    def flaky(g, sc):
        calls.append(g.name)
        if g.name != scratch.name:
            raise RuntimeError("injected failure on copied graph")
        return real(g, sc)

    monkeypatch.setattr(S, "analyze", flaky)
    before_fp, before_graphs = fingerprint(scratch), set(db.list_graphs())
    with pytest.raises(RuntimeError, match="injected"):
        S.simulate_consumer_fallback(db, scratch.name, SCENARIOS["PR-104"], "checkout-api")
    assert len(calls) == 2 and calls[1].startswith(f"{scratch.name}__sim_")   # the failure really hit the copy
    assert set(db.list_graphs()) == before_graphs
    assert fingerprint(scratch) == before_fp
