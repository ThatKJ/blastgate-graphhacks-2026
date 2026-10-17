"""Orchestrator limits and grounding with a FAKE tool client and SCRIPTED planners (no DB, no MCP process).
The tool schemas are the real ones from the MCP server object."""
import pytest
from conftest import fake_report

from blastgate import agent as AG

pytestmark = pytest.mark.anyio
SIM = {"consumer": "checkout-api", "assumption": "checkout-api adds a tolerant parser"}


def result(tool, **over):
    """Canned ToolResult payloads shaped like the real tools'."""
    base = {
        "inspect_change": {"status": "success", "summary": "ok", "evidence": ["Contract:pricing-api-v2"],
                           "next_actions": ["inspect_contract_coverage"],
                           "data": {"declared_by_pr": {"producer": "pricing-engine", "contract_id": "pricing-api-v2",
                                                       "field": "discount_tier", "new_type": "object",
                                                       "change": "c", "breaking": True, "local_tests": "passing"},
                                    "graph": {"field_found": True, "old_type": "string"}}},
        "inspect_contract_coverage": {"status": "warning", "summary": "1 vulnerable",
                                      "evidence": ["CONSUMES_FIELD:checkout-api->pricing-api-v2.discount_tier"],
                                      "next_actions": ["trace_blast_radius"],
                                      "data": {"mapping_status": "complete", "consumers": [
                                          {"service": "checkout-api", "fallback_enabled": False,
                                           "evidence_source": "synthetic"}], "vulnerable_consumers": ["checkout-api"]}},
        "trace_blast_radius": {"status": "warning", "summary": "2 affected",
                               "evidence": ["Service:checkout-api", "Service:order-orchestrator"],
                               "next_actions": ["analyze_network_criticality", "retrieve_incidents_and_reviewers"],
                               "data": {"vulnerable_consumers": ["checkout-api"], "slas": [], "exposure_estimate": None,
                                        "affected_services": [
                                            {"service": "checkout-api", "tier": "critical", "depth": 1,
                                             "path": ["pricing-engine", "checkout-api"]},
                                            {"service": "order-orchestrator", "tier": "critical", "depth": 2,
                                             "path": ["pricing-engine", "checkout-api", "order-orchestrator"]}]}},
        "analyze_network_criticality": {"status": "success", "summary": "s", "evidence": ["Service:checkout-api"],
                                        "data": {"scores": {"checkout-api": 2.0}}},
        "retrieve_incidents_and_reviewers": {"status": "success", "summary": "s",
                                             "evidence": ["Incident:INC-2041", "Engineer:eng-alpha"],
                                             "data": {"incidents": [{"incident_id": "INC-2041", "severity": "SEV-2",
                                                                     "resolved_by": ["eng-alpha"]}],
                                                      "reviewers": [{"handle": "eng-alpha", "resolved_incidents": [],
                                                                     "authored_services": []}]}},
        "simulate_mitigation": {"status": "success", "summary": "counterfactual", "evidence": [],
                                "unknowns": ["Counterfactual graph simulation only."],
                                "data": {"counterfactual": {"assumption": "ASSUMPTION: x",
                                                            "production_verdict": "BLOCK",
                                                            "graph_verdict_under_assumption": "CLEAR"}}},
    }[tool]
    return {**base, **over}


class FakeTools:
    def __init__(self, specs, overrides=None, fail_on=None):
        self.specs, self.overrides, self.fail_on, self.calls = specs, overrides or {}, fail_on, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_tools(self):
        return self.specs

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == self.fail_on:
            raise AG.ToolServerError("MCP call failed (BrokenResourceError)")
        return self.overrides.get(name, result(name)), False


class Scripted:
    """Planner that replays fixed steps, then finishes."""
    mode = "test: scripted planner"

    def __init__(self, steps, finish=None):
        self.steps, self.finish, self.seen = list(steps), finish or AG.Finish(summary="done"), []

    async def start(self, task, tools):
        self.task = task

    async def next(self, observations):
        self.seen.append(observations)
        return self.steps.pop(0) if self.steps else self.finish


def call(name, i=0, **args):
    return AG.ToolCall(id=f"c{i}-{name}", name=name, arguments=args)


async def run(planner, tools, req=None, verdict="BLOCK", known=lambda names: set()):
    return await AG.investigate(req or AG.InvestigateRequest(pr_id="PR-104"), planner, tools,
                                verdict_fn=lambda pr: fake_report(verdict), known_services=known)


async def test_unknown_tool_unparseable_and_extra_args_are_rejected(tool_specs):
    tools = FakeTools(tool_specs)
    steps = [[call("query_graph", query="MATCH (n) DETACH DELETE n"),
              AG.ToolCall(id="x", name="inspect_change", parse_error="arguments were not valid JSON"),
              call("inspect_change", pr_id="PR-104", cypher="x"),
              call("trace_blast_radius", pr_id="PR-104", max_depth=99)]]
    rep = await run(Scripted(steps), tools)
    reasons = [t.rejected_reason for t in rep.tool_trace]
    assert reasons[0].startswith("unknown tool") and reasons[1] == "arguments were not valid JSON"
    assert reasons[2].startswith("unexpected arguments") and reasons[3].startswith("invalid arguments at max_depth")
    assert tools.calls == [] and rep.tool_calls == 0


async def test_pr_is_pinned_and_simulation_needs_user_request(tool_specs):
    tools = FakeTools(tool_specs)
    rep = await run(Scripted([[call("inspect_change", pr_id="PR-108"),
                               call("simulate_mitigation", pr_id="PR-104", **SIM)]]), tools)
    assert [t.rejected_reason for t in rep.tool_trace] == [
        "pr_id must be PR-104, the PR under investigation", "simulation was not requested by the user"]
    assert tools.calls == []


async def test_simulation_uses_the_users_arguments_not_the_models(tool_specs):
    tools = FakeTools(tool_specs)
    req = AG.InvestigateRequest(pr_id="PR-104", simulate=SIM)
    await run(Scripted([[call("simulate_mitigation", pr_id="PR-104", consumer="order-orchestrator",
                              assumption="model-invented assumption text")]]), tools, req)
    assert tools.calls == [("simulate_mitigation", {"pr_id": "PR-104", **SIM})]


async def test_duplicates_are_rejected_and_the_loop_is_bounded(tool_specs):
    tools = FakeTools(tool_specs)
    same = [[call("inspect_change", i, pr_id="PR-104")] for i in range(20)]
    rep = await run(Scripted(same), tools)
    assert len(tools.calls) == 1 and rep.stop_reason == "too_many_rejected_calls"
    assert all("duplicate" in t.rejected_reason for t in rep.tool_trace[1:])


async def test_hard_cap_of_six_tool_calls(tool_specs):
    tools = FakeTools(tool_specs)
    req = AG.InvestigateRequest(pr_id="PR-104", simulate=SIM)
    steps = [[call("inspect_change", pr_id="PR-104")], [call("inspect_contract_coverage", pr_id="PR-104")],
             [call("trace_blast_radius", pr_id="PR-104")], [call("trace_blast_radius", pr_id="PR-104", max_depth=2)],
             [call("analyze_network_criticality", services=["checkout-api"])],
             [call("retrieve_incidents_and_reviewers", pr_id="PR-104", services=["checkout-api"])],
             [call("simulate_mitigation", pr_id="PR-104", **SIM)]]
    rep = await run(Scripted(steps), tools, req)
    assert rep.tool_calls == 6 == len(tools.calls)
    assert rep.tool_trace[-1].rejected_reason.startswith("tool-call budget of 6 exhausted")
    rep = await AG.investigate(req, Scripted(steps), FakeTools(tool_specs),
                               verdict_fn=lambda pr: fake_report(), known_services=lambda n: set(),
                               max_tool_calls=50)   # a caller cannot raise the cap
    assert rep.tool_calls == 6


async def test_rejections_are_fed_back_with_matching_ids(tool_specs):
    planner = Scripted([[call("query_graph", 7)]])
    await run(planner, FakeTools(tool_specs))
    obs = planner.seen[1][0]
    assert obs.call_id == "c7-query_graph" and obs.is_error and obs.result["summary"].startswith("Rejected")


async def test_planner_cannot_override_the_deterministic_verdict(tool_specs):
    planner = Scripted([], AG.Finish(summary="Looks safe", recommended_verdict="CLEAR"))
    rep = await run(planner, FakeTools(tool_specs), verdict="BLOCK")
    assert rep.verdict == "BLOCK"
    assert any("recommended CLEAR" in w and "BLOCK stands" in w for w in rep.warnings)


async def test_provider_error_stops_safely_with_partial_trace(tool_specs):
    class Failing(Scripted):
        async def next(self, observations):
            if self.steps:
                return self.steps.pop(0)
            raise AG.ProviderError("provider_rate_limited", "Groq rate limit reached (HTTP 429).", "3")

    with pytest.raises(AG.ProviderError) as e:
        await run(Failing([[call("inspect_change", pr_id="PR-104")]]), FakeTools(tool_specs))
    assert e.value.code == "provider_rate_limited" and [t.tool for t in e.value.trace] == ["inspect_change"]


async def test_mcp_transport_failure_stops_and_keeps_trace(tool_specs):
    rep = await run(Scripted([[call("inspect_change", pr_id="PR-104")], [call("trace_blast_radius", pr_id="PR-104")]]),
                    FakeTools(tool_specs, fail_on="inspect_change"))
    assert rep.stop_reason == "tool_server_error" and rep.tool_trace[0].status == "error"
    assert rep.verdict == "BLOCK" and any("ended early" in w for w in rep.warnings)


async def test_facts_are_grounded_in_tool_payloads(tool_specs):
    rep = await run(AG.DeterministicPlanner(), FakeTools(tool_specs))
    evidence = {e for t in rep.tool_trace for e in t.evidence}
    assert rep.verified_facts and all(set(f.evidence) <= evidence and f.evidence for f in rep.verified_facts)
    assert {f.source_tool for f in rep.verified_facts} <= {t.tool for t in rep.tool_trace if t.executed}


async def test_unsupported_references_in_planner_text_are_flagged(tool_specs):
    planner = Scripted([[call("inspect_change", pr_id="PR-104")]],
                       AG.Finish(summary="Like INC-9999, invoice-worker will break; see SLA-FAKE-1."))
    rep = await run(planner, FakeTools(tool_specs), known=lambda names: {"invoice-worker"} & set(names))
    assert rep.unsupported_references == ["INC-9999", "SLA-FAKE-1", "invoice-worker"]
    assert any("no tool returned" in w for w in rep.warnings)


async def test_deterministic_planner_follows_tool_results(tool_specs):
    """Same planner, different tool outputs -> different tool paths."""
    vulnerable = await run(AG.DeterministicPlanner(), FakeTools(tool_specs))
    safe_cov = result("inspect_contract_coverage", next_actions=[], status="success",
                      data={"mapping_status": "complete", "consumers": [], "vulnerable_consumers": []})
    safe = await run(AG.DeterministicPlanner(), FakeTools(tool_specs, {"inspect_contract_coverage": safe_cov}))
    assert [t.tool for t in vulnerable.tool_trace] == ["inspect_change", "inspect_contract_coverage",
                                                       "trace_blast_radius", "analyze_network_criticality",
                                                       "retrieve_incidents_and_reviewers"]
    assert [t.tool for t in safe.tool_trace] == ["inspect_change", "inspect_contract_coverage"]


async def test_simulation_caveat_survives_into_report(tool_specs):
    rep = await run(AG.DeterministicPlanner(), FakeTools(tool_specs),
                    AG.InvestigateRequest(pr_id="PR-104", simulate=SIM))
    assert rep.simulation["counterfactual"]["production_verdict"] == "BLOCK"
    assert rep.verdict == "BLOCK"   # the simulated CLEAR never becomes the verdict
    assert "Counterfactual graph simulation only." in rep.unknowns
    assert any("not proof" in r for r in rep.recommendations)


# --- Evidence grounding of the final report (regression: live PR-112 prose said "no incidents") ------

PR104_REPORT = dict(consumers=["checkout-api"], vulnerable=["checkout-api"],
                    affected=[("checkout-api", "critical", 1), ("order-orchestrator", "critical", 2)],
                    slas=[("SLA-CHECKOUT-99.95", "checkout-api")], incidents=["INC-2041"], reviewers=["eng-alpha"])


async def grounded_run(tool_specs, finish, steps=(), tools=None, **report):
    return await AG.investigate(AG.InvestigateRequest(pr_id="PR-104"), Scripted(list(steps), finish),
                                tools or FakeTools(tool_specs), verdict_fn=lambda pr: fake_report(**report),
                                known_services=lambda names: set())


async def test_model_denying_returned_incident_is_withheld(tool_specs):
    """The live failure: the incident tool returned an incident, the model wrote 'no incidents'."""
    finish = AG.Finish(summary="Safe change. No incidents have been associated with this contract.",
                       recommendations=["Merge it."])
    steps = [[call("retrieve_incidents_and_reviewers", pr_id="PR-104", services=["search-indexer"])]]
    rep = await grounded_run(tool_specs, finish, steps, verdict="CLEAR",
                             consumers=["search-indexer"])   # engine did not look incidents up (no impact)
    assert rep.inventory["incidents"].status == "found" and rep.inventory["incidents"].items == ["INC-2041"]
    assert rep.explanation_status == "withheld_inconsistent" and rep.model_explanation is None
    assert rep.recommendations == [] and rep.inferences == []
    assert "incidents on this contract: INC-2041" in rep.summary and rep.verdict == "CLEAR"
    assert any("claims no incidents" in w and "INC-2041" in w for w in rep.warnings)


async def test_omitted_critical_consumer_and_sla_still_appear_in_code_summary(tool_specs):
    finish = AG.Finish(summary="This change looks fine to me.")   # omits everything, denies nothing
    rep = await grounded_run(tool_specs, finish, **PR104_REPORT)
    assert rep.explanation_status == "accepted" and rep.model_explanation == "This change looks fine to me."
    for needle in ("vulnerable consumers: checkout-api", "checkout-api, order-orchestrator",
                   "SLA-CHECKOUT-99.95 (checkout-api)", "INC-2041"):
        assert needle in rep.summary
    assert rep.summary.startswith("BLOCK:")


@pytest.mark.parametrize("text", ["No SLAs are affected.", "There are no vulnerable consumers.",
                                  "All consumers have a fallback.", "No downstream services are impacted."])
async def test_denials_of_returned_consumers_slas_or_impact_are_withheld(tool_specs, text):
    rep = await grounded_run(tool_specs, AG.Finish(summary=text), **PR104_REPORT)
    assert rep.explanation_status == "withheld_inconsistent" and rep.verdict == "BLOCK"


async def test_invented_incident_id_is_withheld(tool_specs):
    rep = await grounded_run(tool_specs, AG.Finish(summary="Similar to INC-9999, this will break."),
                             **PR104_REPORT)
    assert rep.unsupported_references == ["INC-9999"] and rep.explanation_status == "withheld_inconsistent"


async def test_absence_claim_without_a_check_is_withheld(tool_specs):
    """Incidents not looked up at all -> 'not checked', so 'no incidents' is unsupported."""
    rep = await grounded_run(tool_specs, AG.Finish(summary="No incidents exist."), consumers=["search-indexer"])
    assert rep.inventory["incidents"].status == "not_checked"
    assert "incidents on this contract: not checked" in rep.summary
    assert rep.explanation_status == "withheld_inconsistent"


async def test_failed_lookup_is_check_failed_not_none(tool_specs):
    failed = {"retrieve_incidents_and_reviewers": result("retrieve_incidents_and_reviewers", status="error",
                                                         summary="Graph query failed", data={}, evidence=[])}
    steps = [[call("retrieve_incidents_and_reviewers", pr_id="PR-104", services=["search-indexer"])]]
    rep = await grounded_run(tool_specs, AG.Finish(summary="No incidents found."), steps,
                             FakeTools(tool_specs, failed), consumers=["search-indexer"])
    assert rep.inventory["incidents"].status == "check_failed"
    assert "incidents on this contract: check failed" in rep.summary
    assert rep.explanation_status == "withheld_inconsistent"


async def test_unverified_target_makes_everything_unknown(tool_specs):
    rep = await grounded_run(tool_specs, AG.Finish(summary="No vulnerable consumers."), field_found=False)
    assert {c.status for k, c in rep.inventory.items() if k not in ("incidents", "reviewers")} == {"unknown"}
    assert "unknown" in rep.summary and rep.explanation_status == "withheld_inconsistent"


async def test_true_absence_is_accepted(tool_specs):
    """Incidents were checked and none returned: 'no incidents' is supported and the text is kept."""
    empty = {"retrieve_incidents_and_reviewers": result("retrieve_incidents_and_reviewers",
                                                        data={"incidents": [], "reviewers": []}, evidence=[])}
    steps = [[call("retrieve_incidents_and_reviewers", pr_id="PR-104", services=["search-indexer"])]]
    finish = AG.Finish(summary="No incidents were found and no vulnerable consumers exist.")
    rep = await grounded_run(tool_specs, finish, steps, FakeTools(tool_specs, empty), verdict="CLEAR",
                             consumers=["search-indexer"])
    assert rep.inventory["incidents"].status == "none_found"
    assert "incidents on this contract: none found in the checked graph" in rep.summary
    assert rep.explanation_status == "accepted" and rep.model_explanation.startswith("No incidents")


async def test_no_finish_means_no_model_text(tool_specs):
    rep = await AG.investigate(AG.InvestigateRequest(pr_id="PR-104"),
                               Scripted([[call("inspect_change", i, pr_id="PR-104")] for i in range(20)]),
                               FakeTools(tool_specs), verdict_fn=lambda pr: fake_report(**PR104_REPORT),
                               known_services=lambda n: set())
    assert rep.explanation_status == "not_produced" and rep.model_explanation is None
    assert rep.summary.startswith("BLOCK:") and "INC-2041" in rep.summary
