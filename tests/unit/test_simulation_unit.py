"""Cleanup and before/after snapshot guarantees of the simulation, with mocks. Real copy/isolation is tested in integration."""
import json
from unittest import mock

import pytest

from blastgate import simulation
from blastgate.analysis import Report
from blastgate.graph import SCENARIOS
from blastgate.verdict import Decision, Verdict


class FakeDB:
    def __init__(self, set_fallback_count=1, copy_error=None):
        self.graphs = {"src"}
        self.deleted = []
        self.count, self.copy_error = set_fallback_count, copy_error

    def list_graphs(self):
        return sorted(self.graphs)

    def select_graph(self, name):
        db = self
        g = mock.Mock(name=name)
        g.name = name

        def copy(clone):
            if db.copy_error:
                raise db.copy_error
            db.graphs.add(clone)
            return db.select_graph(clone)

        def delete():
            db.graphs.discard(name)
            db.deleted.append(name)

        g.copy.side_effect = copy
        g.delete.side_effect = delete
        g.query.return_value.result_set = [[db.count]]
        return g


def fake_report(graph, sc):
    """Real Report objects: source graph -> BLOCK, copied graph -> CLEAR."""
    if graph.name == "src":
        consumers = [{"service": "checkout-api", "tier": "critical", "fallback_enabled": False,
                      "evidence_source": "synthetic: manifest"}]
        vuln = list(consumers)
        affected = [{"service": "checkout-api", "tier": "critical", "depth": 1,
                     "path": ["pricing-engine", "checkout-api"]},
                    {"service": "order-orchestrator", "tier": "critical", "depth": 2,
                     "path": ["pricing-engine", "checkout-api", "order-orchestrator"]}]
        slas = [{"service": "checkout-api", "sla_id": "SLA-CHECKOUT-99.95", "name": "Checkout",
                 "penalty_per_hour_usd": 12000.0, "synthetic": True}]
        exp = {"penalty_per_hour_usd": 12000.0, "assumed_outage_hours": 2.0, "assumed_total_usd": 24000.0,
               "label": "ASSUMED"}
        cent = {"checkout-api": 2.0, "order-orchestrator": 4.0}
        return Report(sc, True, True, "complete", "n", consumers, vuln, affected, slas, cent, [], [], exp,
                      Decision(Verdict.BLOCK, ("graph reason BLOCK",)))
    consumers = [{"service": "checkout-api", "tier": "critical", "fallback_enabled": True,
                  "evidence_source": "counterfactual-assumption"}]
    return Report(sc, True, True, "complete", "n", consumers, [], [], [], None, None, None, None,
                  Decision(Verdict.CLEAR, ("graph reason CLEAR",)))


@pytest.fixture(autouse=True)
def stub_analysis(monkeypatch):
    monkeypatch.setattr(simulation, "fingerprint", lambda g: ["same"])
    monkeypatch.setattr(simulation, "analyze", fake_report)


def test_counterfactual_marker_survives_standalone_report_and_json():
    res = simulation.simulate_consumer_fallback(FakeDB(), "src", SCENARIOS["PR-104"], "checkout-api")
    for payload in (res.after.as_dict(), json.loads(json.dumps(res.as_dict(), default=str))["after"]):
        cf = payload["counterfactual"]
        assert cf["is_counterfactual"] is True
        assert cf["mitigation_status"] == "assumed_unverified"
        assert cf["verified_by_contract_or_integration_tests"] is False
        assert cf["graph_verdict_under_assumption"] == "CLEAR" and payload["verdict"] == "CLEAR"
        assert cf["production_verdict"] == "BLOCK" and "BLOCK" in cf["production_recommendation"]
        assert cf["label"] == simulation.SIMULATION_LABEL and "checkout-api" in cf["assumption"]
        assert all(r.startswith(simulation.REASON_PREFIX) for r in payload["reasons"])
        assert "graph reason CLEAR" in payload["reasons"][-1]   # original graph-derived reason kept
    assert res.before.counterfactual is None
    assert res.before.decision.reasons == ("graph reason BLOCK",)


def test_impact_snapshot_and_diff_reports():
    res = simulation.simulate_consumer_fallback(FakeDB(), "src", SCENARIOS["PR-104"], "checkout-api")
    before = simulation.impact_snapshot(res.before)
    after = simulation.impact_snapshot(res.after)
    changes = simulation.diff_reports(SCENARIOS["PR-104"], "checkout-api", res.before, res.after)
    assert before["verdict"] == "BLOCK" and after["verdict"] == "CLEAR"
    assert before["vulnerable_consumers"] == ["checkout-api"] and after["vulnerable_consumers"] == []
    assert [a["service"] for a in before["affected_services"]] == ["checkout-api", "order-orchestrator"]
    assert after["affected_services"] == [] and after["slas"] == [] and after["exposure"] is None
    assert changes["consumer"] == "checkout-api"
    assert changes["relationship"] == "(checkout-api)-[:CONSUMES_FIELD]->(pricing-api-v2.discount_tier)"
    assert changes["fallback_before"] is False and changes["fallback_after"] is True
    assert changes["removed_vulnerable_consumers"] == ["checkout-api"]
    assert changes["removed_affected_services"] == ["checkout-api", "order-orchestrator"]
    assert changes["removed_slas"] == ["SLA-CHECKOUT-99.95"]


def test_temp_graph_deleted_after_success():
    db = FakeDB()
    res = simulation.simulate_consumer_fallback(db, "src", SCENARIOS["PR-104"], "checkout-api")
    assert res.temp_graph_deleted and res.temp_graph in db.deleted and db.graphs == {"src"}
    assert res.original_unchanged
    assert "Counterfactual graph simulation only" in res.label


def test_temp_graph_deleted_when_mitigation_target_missing():
    db = FakeDB(set_fallback_count=0)
    with pytest.raises(ValueError, match="expected 1"):
        simulation.simulate_consumer_fallback(db, "src", SCENARIOS["PR-104"], "nobody")
    assert db.graphs == {"src"} and len(db.deleted) == 1 and db.deleted[0] != "src"


def test_failed_copy_never_deletes_source():
    db = FakeDB(copy_error=RuntimeError("copy failed"))
    with pytest.raises(RuntimeError):
        simulation.simulate_consumer_fallback(db, "src", SCENARIOS["PR-104"], "checkout-api")
    assert db.deleted == [] and db.graphs == {"src"}
