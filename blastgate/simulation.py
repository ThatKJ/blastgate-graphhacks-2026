"""Counterfactual simulation on a GRAPH.COPY of the working graph. The original is never written."""
import uuid
from dataclasses import dataclass, replace

from . import queries as Q
from .analysis import Report, analyze
from .graph import Scenario, fingerprint
from .verdict import Decision

SIMULATION_LABEL = ("Counterfactual graph simulation only. The proposed code mitigation remains unverified "
                    "until contract or integration tests pass.")
REASON_PREFIX = "[COUNTERFACTUAL - assumed mitigation, unverified] "


def mark_counterfactual(after: Report, before: Report, assumption: str) -> Report:
    """Stamp the copied-graph report so it can't be passed off as a real verdict, even on its own."""
    return replace(
        after,
        counterfactual={
            "is_counterfactual": True,
            "label": SIMULATION_LABEL,
            "assumption": assumption,
            "mitigation_status": "assumed_unverified",
            "verified_by_contract_or_integration_tests": False,
            "graph_verdict_under_assumption": after.verdict.value,
            "production_verdict": before.verdict.value,
            "production_recommendation": (f"Treat this change as {before.verdict.value} until the mitigation is "
                                          "implemented and passes contract or integration tests."),
        },
        decision=Decision(after.verdict, (REASON_PREFIX + SIMULATION_LABEL + " " + assumption,)
                          + tuple(REASON_PREFIX + r for r in after.decision.reasons)),
    )


@dataclass(frozen=True)
class SimulationResult:
    assumption: str
    before: Report
    after: Report
    temp_graph: str
    temp_graph_deleted: bool
    original_unchanged: bool
    label: str = SIMULATION_LABEL

    def as_dict(self) -> dict:
        return {"label": self.label, "assumption": self.assumption,
                "before": {"verdict": self.before.verdict.value,
                           "vulnerable_consumers": [c["service"] for c in self.before.vulnerable_consumers],
                           "affected_services": [a["service"] for a in self.before.affected_services]},
                "after": self.after.as_dict(),
                "temp_graph": self.temp_graph, "temp_graph_deleted": self.temp_graph_deleted,
                "original_unchanged": self.original_unchanged}


def simulate_consumer_fallback(db, source_name: str, sc: Scenario, consumer: str) -> SimulationResult:
    """Assume `consumer` gains a compatible fallback for sc.field, on a copied graph only."""
    assumption = (f"ASSUMPTION: {consumer} gains a compatible fallback for "
                  f"{sc.contract_id}.{sc.field} ({sc.change}).")
    source = db.select_graph(source_name)
    before_fp = fingerprint(source)
    before = analyze(source, sc)

    temp_name = f"{source_name}__sim_{uuid.uuid4().hex[:8]}"
    copy = None
    try:
        copy = source.copy(temp_name)  # GRAPH.COPY; errors if temp_name already exists
        changed = copy.query(Q.SET_FALLBACK, {"producer": sc.producer, "contract": sc.contract_id,
                                              "field": sc.field,
                                              "consumer": consumer,
                                              "evidence": f"counterfactual-assumption: {assumption}"}).result_set
        if changed[0][0] != 1:
            raise ValueError(f"mitigation target {consumer} -> {sc.contract_id}.{sc.field} matched "
                             f"{changed[0][0]} edges, expected 1")
        after = analyze(copy, sc)
    finally:
        if copy is not None or temp_name in db.list_graphs():
            db.select_graph(temp_name).delete()

    return SimulationResult(assumption, before, mark_counterfactual(after, before, assumption), temp_name,
                            temp_graph_deleted=temp_name not in db.list_graphs(),
                            original_unchanged=fingerprint(source) == before_fp)
