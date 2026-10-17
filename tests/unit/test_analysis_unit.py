"""Analysis logic against a fake graph: no FalkorDB needed."""
import pytest

from blastgate import queries as Q
from blastgate.analysis import AnalysisError, analyze
from blastgate.graph import SCENARIOS, Scenario
from blastgate.verdict import Verdict

SC = SCENARIOS["PR-104"]
EV = "synthetic: manifest"


class FakeResult:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self, responses, error=None):
        self.responses, self.error, self.calls = responses, error, []

    def ro_query(self, q, params=None):
        self.calls.append(q)
        if self.error and q in self.error:
            raise self.error[q]
        return FakeResult(self.responses.get(q, []))

    def query(self, *a, **k):
        raise AssertionError("analysis must never write")


def cov(status="complete", field="discount_tier"):
    return [["pricing-engine", "pricing-api-v2", status, "note", field, "string", True]]


BLOCK_GRAPH = {
    Q.COVERAGE: cov(),
    Q.CONSUMERS: [["checkout-api", "critical", False, EV]],
    # Two routes to invoice-worker: depth must take the shortest (1 + 2 = 3).
    Q.DOWNSTREAM: [
        ["order-orchestrator", "critical", 1, ["order-orchestrator", "checkout-api"]],
        ["invoice-worker", "standard", 2, ["invoice-worker", "order-orchestrator", "checkout-api"]],
        ["invoice-worker", "standard", 3, ["invoice-worker", "x", "order-orchestrator", "checkout-api"]],
    ],
    Q.SLAS: [["checkout-api", "SLA-A", "A", 1000, True],
             ["order-orchestrator", "SLA-B", "B", 500, True],
             ["invoice-worker", "SLA-B", "B", 500, True]],  # same SLA reached twice -> counted once
    Q.CENTRALITY: [["order-orchestrator", 6.0], ["checkout-api", 5.0]],
    Q.INCIDENTS: [["INC-1", "SEV-2", "s", True, ["eng-a"]]],
    Q.REVIEWERS: [["eng-a", "A", ["INC-1"], ["checkout-api"]]],
}


def test_block_report_depths_paths_and_conditional_queries():
    g = FakeGraph(BLOCK_GRAPH)
    r = analyze(g, SC)
    assert r.verdict is Verdict.BLOCK
    depths = {a["service"]: a["depth"] for a in r.affected_services}
    assert depths == {"checkout-api": 1, "order-orchestrator": 2, "invoice-worker": 3}
    inv = next(a for a in r.affected_services if a["service"] == "invoice-worker")
    assert inv["path"] == ["pricing-engine", "checkout-api", "order-orchestrator", "invoice-worker"]
    assert {Q.CENTRALITY, Q.INCIDENTS, Q.REVIEWERS} <= set(g.calls)
    assert r.centrality == {"order-orchestrator": 6.0, "checkout-api": 5.0}
    assert r.incidents[0]["incident_id"] == "INC-1"
    assert r.reviewers[0]["handle"] == "eng-a"


def test_sla_aggregation_dedupes_and_uses_assumed_hours():
    r = analyze(FakeGraph(BLOCK_GRAPH), SC)
    assert r.exposure["penalty_per_hour_usd"] == 1500
    assert r.exposure["assumed_total_usd"] == 1500 * SC.assumed_outage_hours
    assert "ASSUMED" in r.exposure["label"]


def test_no_vulnerable_consumers_skips_traversal_centrality_and_incidents():
    g = FakeGraph({Q.COVERAGE: cov(), Q.CONSUMERS: [["search-indexer", "standard", True, EV]]})
    r = analyze(g, SC)
    assert r.verdict is Verdict.CLEAR
    assert not {Q.DOWNSTREAM, Q.SLAS, Q.CENTRALITY, Q.INCIDENTS, Q.REVIEWERS} & set(g.calls)
    assert r.centrality is None and r.incidents is None and r.reviewers is None and r.exposure is None


def test_incomplete_coverage_with_no_consumers_is_warn():
    r = analyze(FakeGraph({Q.COVERAGE: cov(status="incomplete")}), SC)
    assert r.verdict is Verdict.WARN and r.consumers == []


def test_missing_field_is_warn_and_does_not_look_for_consumers():
    g = FakeGraph({Q.COVERAGE: cov(field=None)})
    r = analyze(g, SC)
    assert r.verdict is Verdict.WARN and r.contract_found and not r.field_found
    assert Q.CONSUMERS not in g.calls


def test_producer_contract_mismatch_is_warn():
    r = analyze(FakeGraph({}), Scenario("X", "t", "wrong-producer", "pricing-api-v2", "discount_tier",
                                        "c", True, "n/a", 1.0))
    assert r.verdict is Verdict.WARN and not r.contract_found


def test_ambiguous_coverage_raises():
    with pytest.raises(AnalysisError):
        analyze(FakeGraph({Q.COVERAGE: cov() + cov()}), SC)


@pytest.mark.parametrize("failing", [Q.COVERAGE, Q.CONSUMERS, Q.DOWNSTREAM, Q.CENTRALITY])
def test_database_errors_raise_never_clear(failing):
    g = FakeGraph(BLOCK_GRAPH, error={failing: ConnectionError("boom")})
    with pytest.raises(AnalysisError, match="boom"):
        analyze(g, SC)
