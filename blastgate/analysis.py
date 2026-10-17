"""Runs the graph queries for one scenario and assembles a structured report."""
from dataclasses import dataclass

from . import queries as Q
from .graph import SYNTHETIC, Scenario
from .verdict import Decision, Facts, decide

EXPOSURE_LABEL = ("ASSUMED exposure = sum(synthetic SLA penalty/hour) x scenario assumed outage hours. "
                  "Not a measured or predicted loss.")


class AnalysisError(RuntimeError):
    """A database/query failure. Never converted into a verdict: no data is not 'safe'."""


@dataclass(frozen=True)
class Report:
    scenario: Scenario
    contract_found: bool
    field_found: bool
    mapping_status: str | None
    coverage_note: str | None
    consumers: list           # [{service, tier, fallback_enabled, evidence_source}]
    vulnerable_consumers: list
    affected_services: list   # [{service, tier, depth, path}] depth = service hops from producer
    slas: list
    centrality: dict | None   # None => algorithm not run
    incidents: list | None    # None => query not run
    reviewers: list | None
    exposure: dict | None
    decision: Decision
    data_label: str = SYNTHETIC
    counterfactual: dict | None = None  # set only by simulation; None => verdict derived from the real graph

    @property
    def verdict(self):
        return self.decision.verdict

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("scenario", "decision")}
        d["scenario"] = self.scenario.__dict__
        d["verdict"] = self.verdict.value
        d["reasons"] = list(self.decision.reasons)
        return d


def analyze(graph, sc: Scenario) -> Report:
    try:
        return _analyze(graph, sc)
    except AnalysisError:
        raise
    except Exception as e:  # redis/falkordb errors, connection loss, malformed results
        raise AnalysisError(f"{sc.pr_id}: graph query failed: {type(e).__name__}: {e}") from e


def _analyze(graph, sc: Scenario) -> Report:
    base = {"producer": sc.producer, "contract": sc.contract_id, "field": sc.field}

    cov = Q.rows(graph, Q.COVERAGE, base)
    if len(cov) > 1:
        raise AnalysisError(f"{sc.pr_id}: ambiguous coverage result ({len(cov)} rows) - graph is inconsistent")
    contract_found = bool(cov)
    field_found = contract_found and cov[0][4] is not None
    mapping_status, coverage_note = (cov[0][2], cov[0][3]) if cov else (None, None)

    consumers, vulnerable, affected, slas = [], [], [], []
    if field_found:
        consumers = [{"service": r[0], "tier": r[1], "fallback_enabled": r[2], "evidence_source": r[3]}
                     for r in Q.rows(graph, Q.CONSUMERS, base)]
        vulnerable = [c for c in consumers if c["fallback_enabled"] is False]

    if vulnerable:
        # Direct consumers sit 1 service hop from the producer; dependents add their DEPENDS_ON path length.
        best = {c["service"]: {"service": c["service"], "tier": c["tier"], "depth": 1,
                               "path": [sc.producer, c["service"]]} for c in vulnerable}
        names = [c["service"] for c in vulnerable]
        for name, tier, hops, path in Q.rows(graph, Q.DOWNSTREAM, {"consumers": names}):
            depth = 1 + hops
            if name != sc.producer and (name not in best or depth < best[name]["depth"]):
                best[name] = {"service": name, "tier": tier, "depth": depth,
                              "path": [sc.producer] + list(reversed(path))}
        affected = sorted(best.values(), key=lambda a: (a["depth"], a["service"]))
        slas = [{"service": r[0], "sla_id": r[1], "name": r[2], "penalty_per_hour_usd": r[3], "synthetic": r[4]}
                for r in Q.rows(graph, Q.SLAS, {"services": [a["service"] for a in affected]})]

    # Centrality, incidents and reviewers only run when there is an affected-service investigation.
    centrality = incidents = reviewers = exposure = None
    if affected:
        svc = [a["service"] for a in affected]
        centrality = {r[0]: r[1] for r in Q.rows(graph, Q.CENTRALITY, {"services": svc, **Q.CENTRALITY_SAMPLING})}
        incidents = [{"incident_id": r[0], "severity": r[1], "summary": r[2], "synthetic": r[3], "resolved_by": r[4]}
                     for r in Q.rows(graph, Q.INCIDENTS, base)]
        reviewers = [{"handle": r[0], "display_name": r[1], "resolved_incidents": r[2], "authored_services": r[3]}
                     for r in Q.rows(graph, Q.REVIEWERS, {**base, "services": svc})]
    if slas:
        per_hour = sum(s["penalty_per_hour_usd"] for s in {s["sla_id"]: s for s in slas}.values())
        exposure = {"penalty_per_hour_usd": per_hour, "assumed_outage_hours": sc.assumed_outage_hours,
                    "assumed_total_usd": per_hour * sc.assumed_outage_hours, "label": EXPOSURE_LABEL}

    decision = decide(Facts(
        contract_found=contract_found, field_found=field_found, mapping_status=mapping_status,
        breaking=sc.breaking,
        consumers=tuple((c["service"], c["fallback_enabled"], c["evidence_source"]) for c in consumers),
        critical_affected=tuple(a["service"] for a in affected if a["tier"] == "critical"),
        sla_ids=tuple(s["sla_id"] for s in slas),
    ))
    return Report(sc, contract_found, field_found, mapping_status, coverage_note, consumers, vulnerable,
                  affected, slas, centrality, incidents, reviewers, exposure, decision)
