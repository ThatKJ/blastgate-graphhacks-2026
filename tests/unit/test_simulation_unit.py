"""Cleanup guarantees of the simulation, with mocks. Real copy/isolation is tested in integration."""
from unittest import mock

import pytest

from blastgate import simulation
from blastgate.graph import SCENARIOS


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


@pytest.fixture(autouse=True)
def stub_analysis(monkeypatch):
    monkeypatch.setattr(simulation, "fingerprint", lambda g: ["same"])
    monkeypatch.setattr(simulation, "analyze", lambda g, sc: mock.Mock(name=f"report:{g.name}"))


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
