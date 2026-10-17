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


# --- Composable steps. analyze() runs them in order; the agent tool layer calls them individually. ---

def _base(sc: Scenario) -> dict:
    return {"producer": sc.producer, "contract": sc.contract_id, "field": sc.field}


def coverage(graph, sc: Scenario) -> dict:
    cov = Q.rows(graph, Q.COVERAGE, _base(sc))
    if len(cov) > 1:
        raise AnalysisError(f"{sc.pr_id}: ambiguous coverage result ({len(cov)} rows) - graph is inconsistent")
    if not cov:
        return {"contract_found": False, "field_found": False, "mapping_status": None, "coverage_note": None,
                "field_type": None, "field_required": None}
    r = cov[0]
    return {"contract_found": True, "field_found": r[4] is not None, "mapping_status": r[2], "coverage_note": r[3],
            "field_type": r[5], "field_required": r[6]}


def consumers(graph, sc: Scenario) -> list:
    return [{"service": r[0], "tier": r[1], "fallback_enabled": r[2], "evidence_source": r[3]}
            for r in Q.rows(graph, Q.CONSUMERS, _base(sc))]


def blast_radius(graph, sc: Scenario, vulnerable: list) -> tuple[list, list]:
    """Affected services (shortest depth wins) and the SLAs they back."""
    if not vulnerable:
        return [], []
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
    return affected, slas


def centrality(graph, services: list) -> dict:
    return {r[0]: r[1] for r in Q.rows(graph, Q.CENTRALITY, {"services": services, **Q.CENTRALITY_SAMPLING})}


def incidents(graph, sc: Scenario) -> list:
    return [{"incident_id": r[0], "severity": r[1], "summary": r[2], "synthetic": r[3], "resolved_by": r[4]}
            for r in Q.rows(graph, Q.INCIDENTS, _base(sc))]


def reviewers(graph, sc: Scenario, services: list) -> list:
    return [{"handle": r[0], "display_name": r[1], "resolved_incidents": r[2], "authored_services": r[3]}
            for r in Q.rows(graph, Q.REVIEWERS, {**_base(sc), "services": services})]


def exposure(sc: Scenario, slas: list) -> dict | None:
    if not slas:
        return None
    per_hour = sum(s["penalty_per_hour_usd"] for s in {s["sla_id"]: s for s in slas}.values())
    return {"penalty_per_hour_usd": per_hour, "assumed_outage_hours": sc.assumed_outage_hours,
            "assumed_total_usd": per_hour * sc.assumed_outage_hours, "label": EXPOSURE_LABEL}


def _analyze(graph, sc: Scenario) -> Report:
    cov = coverage(graph, sc)
    contract_found, field_found = cov["contract_found"], cov["field_found"]
    mapping_status, coverage_note = cov["mapping_status"], cov["coverage_note"]

    found = consumers(graph, sc) if field_found else []
    vulnerable = [c for c in found if c["fallback_enabled"] is False]
    affected, slas = blast_radius(graph, sc, vulnerable)

    # Centrality, incidents and reviewers only run when there is an affected-service investigation.
    cent = inc = rev = None
    if affected:
        svc = [a["service"] for a in affected]
        cent, inc, rev = centrality(graph, svc), incidents(graph, sc), reviewers(graph, sc, svc)
    exp = exposure(sc, slas)

    decision = decide(Facts(
        contract_found=contract_found, field_found=field_found, mapping_status=mapping_status,
        breaking=sc.breaking,
        consumers=tuple((c["service"], c["fallback_enabled"], c["evidence_source"]) for c in found),
        critical_affected=tuple(a["service"] for a in affected if a["tier"] == "critical"),
        sla_ids=tuple(s["sla_id"] for s in slas),
    ))
    return Report(sc, contract_found, field_found, mapping_status, coverage_note, found, vulnerable,
                  affected, slas, cent, inc, rev, exp, decision)
