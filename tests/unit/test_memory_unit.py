"""Investigation-memory logic without a database: revalidation and link rules."""
from types import SimpleNamespace

from blastgate import memory as M
from blastgate.graph import SCENARIOS


def report(affected=("checkout-api",), incidents=("INC-2041",), consumers=("checkout-api",), verdict="BLOCK"):
    return SimpleNamespace(affected_services=[{"service": s, "depth": 1, "tier": "critical"} for s in affected],
                           incidents=[{"incident_id": i} for i in incidents], consumers=[{"service": c} for c in consumers],
                           slas=[], reviewers=[], verdict=SimpleNamespace(value=verdict))


HISTORY = {"investigations": [{"id": "inv-1", "verdict": "CLEAR", "affected_services": ["checkout-api", "gone-svc"],
                               "incidents": ["INC-2041", "INC-OLD"]}],
           "code_reviews": [{"id": "cr-1", "symbol": "x.py::T", "code_dependent_services": ["checkout-api", "invoice-worker"]}]}


def test_revalidation_checks_every_remembered_fact_against_the_current_graph():
    out = {r["fact"]: (r["now"], r["from"]) for r in M.revalidate(HISTORY, report())}
    assert out["checkout-api was affected"] == ("still affected (current graph)", ["inv-1"])
    assert out["gone-svc was affected"] == ("not affected in the current graph", ["inv-1"])
    assert out["incident INC-OLD was cited"] == ("not returned by the current graph", ["inv-1"])
    assert "not reused" in out["earlier verdict CLEAR"][0]
    assert out["code graph found invoice-worker handling x.py::T"][0].endswith("unverified")
    assert out["code graph found checkout-api handling x.py::T"] == ("consumer in the current service graph", ["cr-1"])


def test_repeated_history_is_grouped_not_repeated():
    two = {"investigations": [dict(HISTORY["investigations"][0], id="inv-2"), HISTORY["investigations"][0]],
           "code_reviews": []}
    rows = M.revalidate(two, report())
    assert len(rows) == len({r["fact"] for r in rows})
    assert next(r for r in rows if r["fact"] == "checkout-api was affected")["from"] == ["inv-2", "inv-1"]


def test_no_history_means_nothing_to_revalidate():
    assert M.revalidate({"investigations": [], "code_reviews": []}, report()) == []


class RecordingDB:
    def __init__(self, graphs=()):
        self.params, self.graphs = None, list(graphs)

    def list_graphs(self):
        return self.graphs

    def select_graph(self, name):
        return SimpleNamespace(query=lambda q, p: setattr(self, "params", p))


def test_links_only_to_the_latest_prior_and_to_recalled_code_reviews():
    db = RecordingDB()
    inv = SimpleNamespace(verdict="BLOCK", verdict_reasons=["r"], agent_mode="m", stop_reason="completed",
                          tool_calls=1, tool_trace=[SimpleNamespace(tool="inspect_change", executed=True)])
    hist = {"investigations": [{"id": "inv-new"}, {"id": "inv-older"}], "code_reviews": [{"id": "cr-1"}]}
    M.record_investigation(db, "mem", inv, report(), SCENARIOS["PR-104"], hist, "cr-1")
    assert db.params["prior"] == ["inv-new"] and db.params["code_review"] == ["cr-1"]
    assert db.params["field_key"] == "pricing-api-v2.discount_tier"
    M.record_investigation(db, "mem", inv, report(), SCENARIOS["PR-104"], hist, "cr-never-recorded")
    assert db.params["code_review"] == []           # MERGE must never invent a CodeReview stub


def test_recall_on_a_missing_memory_graph_is_empty_history():
    assert M.recall(RecordingDB(graphs=["other"]), "mem", "c", "f") == {"investigations": [], "code_reviews": []}
