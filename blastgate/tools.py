"""Typed domain tools: the only graph operations an agent can reach (via MCP).

Each tool reuses the Milestone 1 analysis steps, re-validates its inputs at this boundary
(@validate_call), runs parameterised read-only Cypher, and returns a JSON-serialisable ToolResult.
There is no raw-Cypher tool, no tool takes a graph name, and the only write path is
simulate_mitigation, which writes to a temporary GRAPH.COPY and verifies the source is unchanged.
"""
import logging
from dataclasses import dataclass
from functools import wraps
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, validate_call

from . import analysis as A
from . import queries as Q
from .codegraph import register_report_scenarios
from .graph import SCENARIOS, SYNTHETIC, connect
from .simulation import SIMULATION_LABEL, diff_reports, impact_snapshot, simulate_consumer_fallback

log = logging.getLogger("blastgate.tools")

# Opt-in (operator sets BLASTGATE_CODEGRAPH_REPORT): validated code-graph changes become PR-900... in BOTH the
# API and the MCP server process, before the enum below is built. The model can never add a scenario.
CODEGRAPH_PROVENANCE = register_report_scenarios(SCENARIOS)
PrId = Literal[tuple(SCENARIOS)]  # enum in the tool schema; unknown PRs never reach Cypher
ServiceId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
ServiceIds = Annotated[list[ServiceId], Field(min_length=1, max_length=20)]
MaxDepth = Annotated[int, Field(ge=1, le=8)]
Assumption = Annotated[str, Field(min_length=10, max_length=300)]
CENTRALITY_NOTE = ("FalkorDB algo.betweenness over Service/DEPENDS_ON, sampled approximation with a pinned seed "
                   f"{Q.CENTRALITY_SAMPLING}. Highlights chokepoints for review; never an input to the verdict.")


class ToolResult(BaseModel):
    status: Literal["success", "warning", "error"]
    summary: str
    data: dict[str, Any] = {}
    evidence: list[str] = []      # graph identifiers the result is derived from
    unknowns: list[str] = []
    next_actions: list[str] = []
    error_code: str | None = None  # invalid_input | invalid_target | unavailable | integrity


@dataclass
class Ctx:
    """Server-side configuration. Never supplied by a caller or a model."""
    graph_name: str
    _db: Any = None

    @property
    def db(self):
        # FalkorDB() connects eagerly (INFO for Sentinel detection), so connect lazily inside the
        # guarded tool call: a down database becomes an 'unavailable' result, not a crashed server.
        if self._db is None:
            self._db = connect()
        return self._db

    @property
    def graph(self):
        return self.db.select_graph(self.graph_name)


def _tool(fn):
    """Validate at the boundary and turn every failure into a safe, structured error (no internals leaked)."""
    checked = validate_call(fn, config=ConfigDict(arbitrary_types_allowed=True))

    @wraps(fn)
    def run(*args, **kwargs) -> ToolResult:
        try:
            return checked(*args, **kwargs)
        except ValidationError as e:
            fields = sorted({".".join(map(str, err["loc"])) for err in e.errors()})
            return ToolResult(status="error", summary=f"Invalid input: {', '.join(fields)}", error_code="invalid_input")
        except Exception as e:  # database/connection/query failures
            log.warning("tool %s failed: %s", fn.__name__, type(e).__name__)
            return ToolResult(status="error", summary="Graph query failed; no result produced. Nothing is assumed safe.",
                              error_code="unavailable")
    return run


def _sc(pr_id):
    return SCENARIOS[pr_id]


def _existing(ctx: Ctx, services: list) -> tuple[list, list]:
    found = {r[0] for r in Q.rows(ctx.graph, Q.SERVICES_EXIST, {"services": services})}
    return [s for s in services if s in found], [s for s in services if s not in found]


def known_services(ctx: Ctx, names: list) -> set:
    """Which of `names` are real Service nodes (used to flag unsupported references in planner text)."""
    return set(_existing(ctx, names)[0]) if names else set()


@_tool
def inspect_change(ctx: Ctx, pr_id: PrId) -> ToolResult:
    sc = _sc(pr_id)
    cov = A.coverage(ctx.graph, sc)
    ok = cov["contract_found"] and cov["field_found"]
    unknowns = []
    if not cov["contract_found"]:
        unknowns.append(f"{sc.producer} does not expose {sc.contract_id} in the graph; the PR's mapping is unverified.")
    elif not cov["field_found"]:
        unknowns.append(f"{sc.contract_id}.{sc.field} is not modelled in the graph; impact cannot be resolved.")
    return ToolResult(
        status="success" if ok else "warning",
        summary=(f"{pr_id} changes {sc.contract_id}.{sc.field} on {sc.producer} "
                 f"({cov['field_type'] or 'type unknown'} -> {sc.new_type})" if ok
                 else f"{pr_id}: change target not verified in graph"),
        data={"pr_id": pr_id, "title": sc.title, "data_label": SYNTHETIC,
              "declared_by_pr": {"producer": sc.producer, "contract_id": sc.contract_id, "field": sc.field,
                                 "new_type": sc.new_type, "change": sc.change, "breaking": sc.breaking,
                                 "local_tests": sc.local_tests, "source": sc.source},
              "graph": {"contract_found": cov["contract_found"], "field_found": cov["field_found"],
                        "old_type": cov["field_type"], "field_required": cov["field_required"]}},
        evidence=([f"Service:{sc.producer}", f"Contract:{sc.contract_id}"] if cov["contract_found"] else [])
        + ([f"SchemaField:{sc.contract_id}.{sc.field}"] if ok else []),
        unknowns=unknowns,
        next_actions=["inspect_contract_coverage"] if ok else [],
    )


@_tool
def inspect_contract_coverage(ctx: Ctx, pr_id: PrId) -> ToolResult:
    sc = _sc(pr_id)
    cov = A.coverage(ctx.graph, sc)
    if not cov["field_found"]:
        return ToolResult(status="warning", summary=f"{pr_id}: target field not found; coverage cannot be assessed",
                          data={"pr_id": pr_id, "contract_found": cov["contract_found"], "field_found": False},
                          unknowns=["Consumers of an unmodelled field are unknown."])
    found = A.consumers(ctx.graph, sc)
    vulnerable = [c["service"] for c in found if c["fallback_enabled"] is False]
    unverified = [c["service"] for c in found if not isinstance(c["fallback_enabled"], bool) or not c["evidence_source"]]
    complete = cov["mapping_status"] == "complete"
    unknowns = ([] if complete else [f"Consumer mapping is '{cov['mapping_status']}': {cov['coverage_note']}"]) + \
               [f"Fallback evidence unverified for {s}." for s in unverified]
    return ToolResult(
        status="success" if complete and not vulnerable and not unverified else "warning",
        summary=(f"Mapping {cov['mapping_status']}; {len(found)} indexed consumer(s) of {sc.field}, "
                 f"{len(vulnerable)} without fallback"),
        data={"pr_id": pr_id, "mapping_status": cov["mapping_status"], "coverage_note": cov["coverage_note"],
              "consumers": found, "vulnerable_consumers": vulnerable},
        evidence=[f"Contract:{sc.contract_id}"]
        + [f"CONSUMES_FIELD:{c['service']}->{sc.contract_id}.{sc.field}" for c in found],
        unknowns=unknowns,
        next_actions=["trace_blast_radius"] if vulnerable else [],
    )


@_tool
def trace_blast_radius(ctx: Ctx, pr_id: PrId, max_depth: MaxDepth = 8) -> ToolResult:
    sc = _sc(pr_id)
    cov = A.coverage(ctx.graph, sc)
    found = A.consumers(ctx.graph, sc) if cov["field_found"] else []
    vulnerable = [c for c in found if c["fallback_enabled"] is False]
    affected, slas = A.blast_radius(ctx.graph, sc, vulnerable)
    shown = [a for a in affected if a["depth"] <= max_depth]
    slas = [s for s in slas if s["service"] in {a["service"] for a in shown}]
    exp = A.exposure(sc, slas)
    unknowns = [] if cov["field_found"] else ["Target field not modelled; nothing can be traced."]
    if len(shown) < len(affected):
        unknowns.append(f"{len(affected) - len(shown)} affected service(s) beyond depth {max_depth} not shown.")
    return ToolResult(
        status="warning" if shown else "success",
        summary=(f"{len(shown)} affected service(s) up to depth {max(a['depth'] for a in shown)}, "
                 f"{len(slas)} synthetic SLA(s)" if shown else "No vulnerable consumer: no blast radius to trace"),
        data={"pr_id": pr_id, "vulnerable_consumers": [c["service"] for c in vulnerable],
              "affected_services": shown, "slas": slas, "exposure_estimate": exp,
              "depth_convention": "direct consumer = 1; each DEPENDS_ON hop +1; SLA links are not hops"},
        evidence=[f"Service:{a['service']}" for a in shown] + [f"SLAContract:{s['sla_id']}" for s in slas],
        unknowns=unknowns,
        next_actions=["analyze_network_criticality", "retrieve_incidents_and_reviewers"] if shown else [],
    )


@_tool
def analyze_network_criticality(ctx: Ctx, services: ServiceIds) -> ToolResult:
    known, unknown = _existing(ctx, services)
    scores = A.centrality(ctx.graph, known) if known else {}
    return ToolResult(
        status="success" if known else "warning",
        summary=(f"Betweenness for {len(scores)} service(s); highest: {max(scores, key=scores.get)}" if scores
                 else "No known services to score"),
        data={"scores": scores, "algorithm": "algo.betweenness", "note": CENTRALITY_NOTE},
        evidence=[f"Service:{s}" for s in known],
        unknowns=[f"Service {s} not found in graph." for s in unknown],
    )


@_tool
def retrieve_incidents_and_reviewers(ctx: Ctx, pr_id: PrId, services: ServiceIds) -> ToolResult:
    sc = _sc(pr_id)
    known, unknown = _existing(ctx, services)
    inc = A.incidents(ctx.graph, sc)
    rev = A.reviewers(ctx.graph, sc, known) if known else []
    return ToolResult(
        status="success",
        summary=f"{len(inc)} synthetic incident(s) on {sc.contract_id}; {len(rev)} candidate reviewer(s)",
        data={"pr_id": pr_id, "incidents": inc, "reviewers": rev, "data_label": SYNTHETIC},
        evidence=[f"Incident:{i['incident_id']}" for i in inc] + [f"Engineer:{r['handle']}" for r in rev],
        unknowns=[f"Service {s} not found in graph." for s in unknown]
        + ([] if inc else [f"No recorded incident for {sc.contract_id}; absence of history is not evidence of safety."]),
    )


@_tool
def simulate_mitigation(ctx: Ctx, pr_id: PrId, consumer: ServiceId, assumption: Assumption) -> ToolResult:
    sc = _sc(pr_id)
    try:
        sim = simulate_consumer_fallback(ctx.db, ctx.graph_name, sc, consumer)
    except ValueError:  # consumer is not an indexed consumer of this field
        return ToolResult(status="error", error_code="invalid_target",
                          summary=f"{consumer} is not an indexed consumer of {sc.contract_id}.{sc.field}")
    if not (sim.temp_graph_deleted and sim.original_unchanged):  # fail closed on any containment doubt
        log.error("simulation containment check failed for %s", pr_id)
        return ToolResult(status="error", error_code="integrity",
                          summary="Simulation containment could not be verified; result discarded.")
    cf = sim.after.counterfactual
    return ToolResult(
        status="success",
        summary=(f"Counterfactual only: {cf['production_verdict']} -> {cf['graph_verdict_under_assumption']} "
                 "if the assumed mitigation existed; code fix unverified"),
        data={"pr_id": pr_id, "user_assumption": assumption,
              "modelled_mutation": f"{consumer} CONSUMES_FIELD.fallback_enabled := true, on a temporary graph copy only",
              "counterfactual": cf, "after_reasons": list(sim.after.decision.reasons),
              "before": impact_snapshot(sim.before),
              "after": impact_snapshot(sim.after),
              "changes": diff_reports(sc, consumer, sim.before, sim.after),
              "temp_graph_deleted": sim.temp_graph_deleted, "original_unchanged": sim.original_unchanged},
        evidence=[f"CONSUMES_FIELD:{consumer}->{sc.contract_id}.{sc.field}"],
        unknowns=[SIMULATION_LABEL],
    )


TOOLS = {f.__name__: f for f in (inspect_change, inspect_contract_coverage, trace_blast_radius,
                                 analyze_network_criticality, retrieve_incidents_and_reviewers, simulate_mitigation)}
