// node --test web/tests  -- renders REAL recorded backend responses (see capture_fixtures.py) through view.js.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";
import {
  CATEGORIES, CENTRALITY_LABEL, STATUS, agentMode, agentPanel, describeError, detailTables, errorBlock, esc,
  evidenceInspector, formatCentrality, highestCentrality, impactMap, inventoryTable, selectableGraphItems,
  simulationPanel, timeoutError, verdictBlock,
} from "../view.js";

const dir = new URL("./fixtures/", import.meta.url);
const fx = (name) => JSON.parse(readFileSync(new URL(`${name}.json`, dir), "utf8"));
const scenarios = fx("scenarios").body;
const scenario = (pr) => scenarios.find((s) => s.pr_id === pr);
const text = (html) => html
  .replace(/<[^>]+>/g, " ")
  .replace(/&gt;/g, ">")
  .replace(/&lt;/g, "<")
  .replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'")
  .replace(/&amp;/g, "&")
  .replace(/\s+/g, " ");

test("fixtures exist and were recorded with the expected HTTP status", () => {
  const files = readdirSync(dir).filter((f) => f.endsWith(".json"));
  assert.ok(files.length >= 15);
  for (const [name, status] of [["analyze_PR-104", 200], ["analyze_PR-114", 200], ["error_unknown_scenario", 404],
    ["error_validation", 422], ["error_rate_limited.scripted", 429], ["error_agent_not_configured", 503],
    ["error_graph_unavailable", 503]]) {
    assert.equal(fx(name).status, status, name);
  }
});

test("all four scenario verdicts render with their real reasons", () => {
  for (const [pr, verdict] of [["PR-104", "BLOCK"], ["PR-108", "WARN"], ["PR-112", "CLEAR"], ["PR-114", "WARN"]]) {
    const a = fx(`analyze_${pr}`).body;
    assert.equal(a.verdict, verdict);
    const html = verdictBlock(a.verdict, a.reasons, "note");
    assert.match(html, new RegExp(`verdict-${verdict.toLowerCase()}`));
    for (const r of a.reasons) assert.ok(text(html).includes(text(esc(r))), r);
  }
});

test("every inventory status renders with its own label, glyph and items", () => {
  const inv104 = inventoryTable(fx("analyze_PR-104").body.inventory);
  assert.match(inv104, /status-found/);
  assert.ok(text(inv104).includes("INC-2041") && text(inv104).includes("SLA-CHECKOUT-99.95 (checkout-api)"));
  const inv112 = text(inventoryTable(fx("analyze_PR-112").body.inventory));
  assert.ok(inv112.includes("Checked, none found") && inv112.includes("Not checked"));
  const synthetic = { incidents: { status: "unknown", items: [], source: "s" },
    slas: { status: "check_failed", items: [], source: "s" } };
  const html = text(inventoryTable(synthetic));
  assert.ok(html.includes("Unknown") && html.includes("Check failed"));
  assert.deepEqual(Object.keys(STATUS).sort(), ["check_failed", "found", "none_found", "not_checked", "unknown"]);
});

test("inventory covers every category the API returns", () => {
  const keys = Object.keys(fx("analyze_PR-104").body.inventory).sort();
  assert.deepEqual(CATEGORIES.map(([k]) => k).sort(), keys);
});

test("'not looked up' and 'none found' stay distinct in detail tables", () => {
  const t112 = text(detailTables(fx("analyze_PR-112").body));
  assert.ok(t112.includes("Not looked up: no service is affected."));
  const t104 = text(detailTables(fx("analyze_PR-104").body));
  assert.ok(t104.includes("INC-2041") && t104.includes("eng-alpha") && t104.includes("(synthetic)"));
  const t114 = text(detailTables(fx("analyze_PR-114").body));
  assert.ok(t114.includes("INC-1877") && t114.includes("eng-charlie") && t114.includes("storefront-bff"));
});

test("centrality is dynamically identified, labelled as sampled approximation, and distinguishes null from 0.0", () => {
  const a104 = fx("analyze_PR-104").body;
  const top104 = highestCentrality(a104.centrality);
  assert.equal(top104.maxService, "order-orchestrator");
  assert.equal(top104.maxScore, 4.0);
  assert.equal(formatCentrality(a104.centrality, "order-orchestrator").short, "4.0 (highest)");
  assert.equal(formatCentrality(a104.centrality, "checkout-api").short, "2.0");
  assert.equal(formatCentrality(a104.centrality, "invoice-worker").short, "0.0");
  assert.equal(formatCentrality(a104.centrality, "invoice-worker").isHighest, false);

  // True zero (PR-114: storefront-bff is 0.0) is never marked as highest or unavailable
  const a114 = fx("analyze_PR-114").body;
  assert.equal(highestCentrality(a114.centrality).maxService, null);
  const c114 = formatCentrality(a114.centrality, "storefront-bff");
  assert.equal(c114.status, "available");
  assert.equal(c114.short, "0.0");
  assert.equal(c114.isHighest, false);

  // Null / unavailable centrality (PR-112 / PR-108) is never rendered as 0.0
  const c112 = formatCentrality(fx("analyze_PR-112").body.centrality, "search-indexer");
  assert.equal(c112.status, "unavailable");
  assert.equal(c112.short, "not run");
  assert.equal(formatCentrality(a104.centrality, "unlisted-service").status, "unavailable");

  const dt104 = text(detailTables(a104));
  assert.ok(dt104.includes("Betweenness (sampled)"));
  assert.ok(dt104.includes("4.0 (highest)") && dt104.includes("2.0") && dt104.includes("0.0"));
  assert.ok(dt104.includes(CENTRALITY_LABEL));
});

test("impact map draws only returned consumers and paths and supports selection state", () => {
  const a = fx("analyze_PR-104").body;
  const svg = impactMap(a, scenario("PR-104"));
  const drawn = [...svg.matchAll(/<g class="node node-(\w+)"[^>]*aria-label="([^"]+)"/g)];
  const services = new Set([...a.consumers.map((c) => c.service), ...a.affected_services.map((s) => s.service)]);
  assert.equal(drawn.length, services.size + 1);                // + the changed field
  for (const s of services) assert.ok(svg.includes(`aria-label="${s}:`), s);
  assert.equal((svg.match(/class="edge /g) || []).length, a.consumers.length + a.affected_services.filter((s) => s.depth >= 2).length);
  assert.match(svg, /SLA ×1/);
  assert.match(svg, /BT 4\.0 ★/);
  assert.match(svg, /BT 2\.0/);

  const selectedSvg = impactMap(a, scenario("PR-104"), { selected: { type: "node", id: "order-orchestrator" } });
  assert.match(selectedSvg, /data-node="order-orchestrator" data-selected="true"/);

  const selectedEdgeSvg = impactMap(a, scenario("PR-104"), { selected: { type: "edge", id: "checkout-api->order-orchestrator" } });
  assert.match(selectedEdgeSvg, /data-edge="checkout-api-&gt;order-orchestrator" data-rel="DEPENDS_ON" data-selected="true"/);

  const safe = impactMap(fx("analyze_PR-112").body, scenario("PR-112"));
  assert.match(safe, /node-safe/);
  assert.doesNotMatch(safe, /node-hit|edge-hit/);
  assert.match(impactMap(fx("analyze_PR-108").body, scenario("PR-108")), /No consumer of this field is indexed/);
});

test("evidence inspector shows real node and relationship evidence and never fabricates missing data", () => {
  const a104 = fx("analyze_PR-104").body, sc104 = scenario("PR-104");
  const items = selectableGraphItems(a104, sc104);
  assert.equal(items.nodes.length, 6);
  assert.equal(items.edges.length, 5);

  // Inspecting order-orchestrator (depth 2 chokepoint)
  const ordHtml = text(evidenceInspector(a104, sc104, { type: "node", id: "order-orchestrator" }));
  assert.ok(ordHtml.includes("Service:order-orchestrator") && ordHtml.includes("critical"));
  assert.ok(ordHtml.includes("2 service hops from producer pricing-engine"));
  assert.ok(ordHtml.includes("pricing-engine → checkout-api → order-orchestrator"));
  assert.ok(ordHtml.includes("(Service:checkout-api)-[:CONSUMES_FIELD {fallback_enabled: false}]->(SchemaField:pricing-api-v2.discount_tier)"));
  assert.ok(ordHtml.includes("(Service:order-orchestrator)-[:DEPENDS_ON]->(Service:checkout-api)"));
  assert.ok(ordHtml.includes("invoice-worker (depth 3)") && ordHtml.includes("partner-gateway (depth 3)"));
  assert.ok(ordHtml.includes("4.0 — highest sampled betweenness"));
  assert.ok(ordHtml.includes("SLA-ORDERS-99.9") && ordHtml.includes("INC-2041") && ordHtml.includes("eng-bravo"));

  // Inspecting field node (__field)
  const fieldHtml = text(evidenceInspector(a104, sc104, { type: "node", id: "__field" }));
  assert.ok(fieldHtml.includes("(Service:pricing-engine)-[:EXPOSES]->(Contract:pricing-api-v2)-[:HAS_FIELD]->(SchemaField:pricing-api-v2.discount_tier)"));
  assert.ok(fieldHtml.includes("CONSUMES_FIELD"));

  // Inspecting relationships (CONSUMES_FIELD vs DEPENDS_ON)
  const cfEdgeHtml = text(evidenceInspector(a104, sc104, { type: "edge", id: "__field->checkout-api" }));
  assert.ok(cfEdgeHtml.includes("Relationship: CONSUMES_FIELD (Direct dependency)"));
  assert.ok(cfEdgeHtml.includes("synthetic: checkout-api consumer contract (pact) manifest"));
  const depEdgeHtml = text(evidenceInspector(a104, sc104, { type: "edge", id: "order-orchestrator->invoice-worker" }));
  assert.ok(depEdgeHtml.includes("Relationship: DEPENDS_ON (Downstream dependency)"));
  assert.ok(depEdgeHtml.includes("(Service:invoice-worker)-[:DEPENDS_ON]->(Service:order-orchestrator)"));

  // PR-114 non-critical scenario inspector (INC-1877 & eng-charlie)
  const a114 = fx("analyze_PR-114").body, sc114 = scenario("PR-114");
  const sfHtml = text(evidenceInspector(a114, sc114, { type: "node", id: "storefront-bff" }));
  assert.ok(sfHtml.includes("INC-1877") && sfHtml.includes("eng-charlie") && sfHtml.includes("0.0 — sampled betweenness"));

  // Unknown / non-existent node or edge is never fabricated
  const bogusNode = text(evidenceInspector(a104, sc104, { type: "node", id: "non-existent-svc" }));
  assert.ok(bogusNode.includes("not in the returned graph analysis; no node or path is fabricated"));
  const bogusEdge = text(evidenceInspector(a104, sc104, { type: "edge", id: "checkout-api->non-existent" }));
  assert.ok(bogusEdge.includes("not in the returned graph analysis; no edge is fabricated"));

  // Missing target field / incomplete coverage / failed checks are surfaced accurately
  const a108 = fx("analyze_PR-108").body, sc108 = scenario("PR-108");
  const insp108 = text(evidenceInspector(a108, sc108));
  assert.ok(insp108.includes("Consumer mapping is 'incomplete'") && insp108.includes("Not checked"));
  const missingField = text(evidenceInspector({ ...a108, field_found: false }, sc108));
  assert.ok(missingField.includes("change target isn't in the graph"));
  const failedInv = {
    ...a112WithStatus("check_failed"),
  };
  const failedHtml = text(evidenceInspector(failedInv, scenario("PR-112"), { type: "node", id: "search-indexer" }));
  assert.ok(failedHtml.includes("Check failed") && !failedHtml.includes("Checked, none found on contract"));
});

function a112WithStatus(status) {
  const base = fx("analyze_PR-112").body;
  return {
    ...base,
    centrality: null,
    incidents: null,
    inventory: {
      ...base.inventory,
      incidents: { status, items: [], source: "retrieve_incidents_and_reviewers failed" },
    },
  };
}

test("accepted explanation is shown, labelled as interpretation, with the planner mode", () => {
  const r = fx("investigate_PR-104_demo").body;
  assert.equal(r.explanation_status, "accepted");
  const html = agentPanel(r);
  assert.ok(text(html).includes("interpretation, not verified evidence"));
  assert.ok(html.includes(esc(r.model_explanation)));
  assert.match(html, /mode-demo/);
  assert.ok(text(html).includes("Verified by tool results"));
});

test("withheld explanation is never rendered; the reason is", () => {
  const r = fx("investigate_PR-104_withheld.scripted").body;
  assert.equal(r.explanation_status, "withheld_inconsistent");
  const html = agentPanel(r);
  assert.doesNotMatch(html, /No incidents have been associated/);
  assert.doesNotMatch(html, /class="explanation"/);
  assert.ok(text(html).includes("withheld because it contradicted the graph evidence"));
  assert.ok(text(html).includes("claims no incidents"));
  assert.match(html, /mode-test/);
});

test("agent modes are told apart from the real agent_mode values", () => {
  assert.equal(agentMode("llm: groq/openai/gpt-oss-20b").kind, "live");
  assert.equal(agentMode(fx("investigate_PR-104_demo").body.agent_mode).kind, "demo");
  assert.equal(agentMode(fx("investigate_PR-104_withheld.scripted").body.agent_mode).kind, "test");
  assert.equal(agentMode("something else").kind, "unknown");
});

test("simulation shows the backend's exact label, renders before-and-after topology diff, and never claims a verified fix", () => {
  const s = fx("simulate_PR-104").body;
  const html = simulationPanel(s, scenario("PR-104"));
  const plain = text(html);
  assert.ok(html.includes(esc(s.label)));
  assert.ok(s.label.startsWith("Counterfactual graph simulation only."));
  assert.ok(plain.includes("UNVERIFIED"));
  assert.match(html, /chip-block[\s\S]*chip-clear/);            // real graph BLOCK, copy CLEAR
  assert.doesNotMatch(plain, /\bverified fix\b|\bfix works\b/i);

  // Verify before-and-after topology comparison is rendered from real simulation data
  assert.match(html, /data-sim-stage="before"/);
  assert.match(html, /data-sim-stage="after"/);
  assert.ok(plain.includes("(checkout-api)-[:CONSUMES_FIELD]->(pricing-api-v2.discount_tier)"));
  assert.ok(plain.includes("false → true"));
  assert.ok(plain.includes("order-orchestrator") && plain.includes("invoice-worker") && plain.includes("partner-gateway"));
  assert.ok(plain.includes("$43,000 (2 h assumed) → $0 (no SLA at risk)"));
});

test("real error responses become actionable messages", () => {
  const cases = {
    error_unknown_scenario: "Unknown change", error_graph_unavailable: "graph database is unreachable",
    error_agent_not_configured: "agent is not configured", error_invalid_target: "can't be simulated",
    "error_rate_limited.scripted": "rate-limiting",
  };
  for (const [name, needle] of Object.entries(cases)) {
    const { status, body } = fx(name);
    assert.ok(describeError(status, body).title.includes(needle), name);
  }
  const rl = describeError(429, fx("error_rate_limited.scripted").body);
  assert.equal(rl.retryAfter, "7");
  assert.ok(text(errorBlock(rl)).includes("Suggested wait: 7 s"));
  const v = describeError(422, fx("error_validation").body);
  assert.equal(v.code, "validation");
  assert.ok(v.detail.includes("assumption"));
  assert.equal(describeError(0, { message: "Failed to fetch" }).code, "network");
  assert.equal(timeoutError(200000).code, "timeout");
});

test("API and model text is escaped", () => {
  const evil = { ...fx("investigate_PR-104_demo").body, model_explanation: `<img src=x onerror="alert(1)">` };
  const html = agentPanel(evil);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.doesNotMatch(errorBlock({ title: "<script>", detail: "", action: "", retryAfter: null }), /<script>/);
});

test("legend lists only the edge kinds that were drawn", async () => {
  const { mapLegend } = await import("../view.js");
  const l104 = mapLegend(fx("analyze_PR-104").body), l112 = mapLegend(fx("analyze_PR-112").body);
  assert.ok(l104.includes("impact path") && !l104.includes("consumer with a fallback"));
  assert.ok(l112.includes("consumer with a fallback") && !l112.includes("impact path"));
  assert.equal(mapLegend(fx("analyze_PR-108").body), "");
});

test("live-access refusals and server rate limits get their own messages", () => {
  const denied = describeError(401, { error: "live_access_denied", message: "m" });
  const local = describeError(403, { error: "live_access_local_only", message: "m" });
  const limited = describeError(429, { error: "live_rate_limited", message: "m", retry_after: "42" });
  assert.ok(denied.title.includes("access token") && local.title.includes("own machine"));
  assert.ok(limited.title.includes("limit") && limited.retryAfter === "42");
});

test("code-graph review renders the real recorded report: diff, evidence, mapping, unknowns, counterfactual", async () => {
  const { codegraphPanel } = await import("../view.js");
  const r = fx("codegraph_report").body;
  assert.equal(fx("codegraph_report").status, 200);
  const html = codegraphPanel(r), t = text(html);
  assert.match(html, /verdict-block/);
  assert.ok(html.includes("-    discount_tier: str") && html.includes("+    discount_tier: dict"), "actual diff");
  assert.ok(t.includes("contracts/pricing.py::PriceQuote.discount_tier : type_changed (str → dict)"));
  assert.ok(t.includes("Mapping validated"));
  assert.ok(t.includes("reporting/revenue.py::tier_revenue") && html.includes("mapping-unmapped"), "unknown source evidence");
  for (const id of ["order-orchestrator", "SLA-CHECKOUT-99.95", "INC-2041", "eng-alpha"]) assert.ok(t.includes(id), id);
  assert.ok(t.includes("Counterfactual: unverified source-code mitigation"));
  assert.ok(t.includes("Treat this change as BLOCK until the mitigation is implemented"));
  assert.ok(!html.includes("<script"), "everything is escaped");
});

test("code-graph review escapes report text", async () => {
  const { codegraphPanel } = await import("../view.js");
  const r = structuredClone(fx("codegraph_report").body);
  r.diff = "<img src=x onerror=alert(1)>";
  r.contract_changes[0].uncertainties = ["<script>x</script>"];
  const html = codegraphPanel(r);
  assert.ok(!html.includes("<img") && !html.includes("<script>"));
});

test("agent timeline shows every real MCP call, why it ran, and gate rejections", async () => {
  const { agentTimeline } = await import("../view.js");
  const r = fx("investigate_PR-104_demo").body;
  const html = agentTimeline(r), t = text(html);
  for (const step of r.tool_trace) assert.ok(t.includes(step.tool), step.tool);
  assert.ok(t.includes("Followed step 1's next_actions"), "rule-based planner follows tool results");
  assert.equal((html.match(/class="step /g) || []).length, r.tool_trace.length);
  const live = { ...r, agent_mode: "llm: groq/x", tool_trace: [{ ...r.tool_trace[0], purpose: "selected by model" },
    { step: 2, tool: "simulate_mitigation", arguments: {}, purpose: "selected by model", executed: false,
      rejected_reason: "simulation was not requested by the user" }] };
  const lt = text(agentTimeline(live));
  assert.ok(lt.includes("Chosen by the model from the MCP tool list"));
  assert.ok(lt.includes("Rejected by the gate") && lt.includes("simulation was not requested by the user"));
});


test("code-graph origin shows the real diff and type-level limits; review links only registered changes", async () => {
  const { codeOrigin, codegraphPanel } = await import("../view.js");
  const r = fx("codegraph_report").body;
  const cc = { symbol: "contracts/pricing.py::PriceQuote", field: "discount_tier" };
  const o = codeOrigin(r, cc);
  assert.ok(o.includes("+    discount_tier: dict") && text(o).includes("not that it reads"));
  assert.ok(text(codeOrigin(r, { symbol: "x", field: "y" })).includes("not available"));
  assert.match(codegraphPanel(r, () => "PR-900"), /data-investigate="PR-900"/);
  assert.doesNotMatch(codegraphPanel(r), /data-investigate/);
});

test("app.js parses (it is not imported by these tests, so check its syntax explicitly)", async () => {
  const { execFileSync } = await import("node:child_process");
  execFileSync(process.execPath, ["--check", new URL("../app.js", import.meta.url).pathname]);
});



const stageOf = (html, name) => {
  const li = html.split("<li ").find((x) => x.includes(`loop-name">${name}<`));
  return li && li.match(/stage-(not_run|running|completed|failed|not_applicable)/)[1];
};

test("progression: every stage status comes from received data or a request in flight", async () => {
  const { loopStrip } = await import("../view.js");
  const declared = scenario("PR-104");
  const before = loopStrip({ scenario: declared });
  assert.equal(stageOf(before, "Code change"), "not_applicable");
  assert.equal(stageOf(before, "Service graph"), "not_run");
  assert.equal(stageOf(before, "MCP tools"), "not_run");
  assert.equal(stageOf(before, "Counterfactual"), "not_run");
  const running = loopStrip({ scenario: declared, busy: ["agent"] });
  assert.equal(stageOf(running, "MCP tools"), "running");
  assert.equal(stageOf(running, "Memory"), "running");
  const failed = loopStrip({ scenario: declared, errors: { agent: true, analyze: true } });
  assert.equal(stageOf(failed, "MCP tools"), "failed");
  assert.equal(stageOf(failed, "Service graph"), "failed");
  const a = fx("analyze_PR-104").body, ag = fx("investigate_PR-104_demo").body, sim = fx("simulate_PR-104").body;
  const done = loopStrip({ scenario: declared, analysis: a, agent: ag, simulation: sim });
  for (const n of ["Service graph", "MCP tools", "Decision", "Counterfactual"]) assert.equal(stageOf(done, n), "completed", n);
  assert.match(done, /tone-block/);
  assert.equal(stageOf(done, "Memory"), "not_applicable");                 // recorded with memory off
  assert.ok(text(done).includes(`${ag.tool_calls} call(s)`) && text(done).includes("BLOCK → CLEAR on a temporary copy"));
  const cc = { symbol: "contracts/pricing.py::PriceQuote", field: "discount_tier", old_type: "str", new_type: "dict",
    mapping: "validated", code_dependents: 6, code_routes: 2 };
  const derived = loopStrip({ scenario: { ...declared, code_change: cc } });
  for (const n of ["Code change", "Code graph", "Mapping"]) assert.equal(stageOf(derived, n), "completed", n);
  assert.ok(text(derived).includes("str → dict (git diff)") && text(derived).includes("(validated)"));
});

test("memory stage says 'recalled' only when the agent actually retrieved earlier records", async () => {
  const { loopStrip, memoryPanel } = await import("../view.js");
  const r = fx("investigate_PR-104_memory").body, m = r.memory;
  assert.equal(m.status, "recorded");
  const recalled = loopStrip({ scenario: scenario("PR-104"), agent: r });
  assert.equal(stageOf(recalled, "Memory"), "completed");
  assert.ok(text(recalled).includes("Recalled 1 earlier investigation(s) and 0 code review(s)"));
  const first = { ...r, memory: { ...m, prior_investigations: [], code_reviews: [], revalidation: [] } };
  const t1 = text(loopStrip({ scenario: scenario("PR-104"), agent: first }));
  assert.ok(t1.includes("No earlier investigation of this field") && !t1.includes("Recalled"));
  const broken = { ...r, memory: { ...m, status: "record_failed", recorded_id: null } };
  assert.equal(stageOf(loopStrip({ scenario: scenario("PR-104"), agent: broken }), "Memory"), "failed");
  const html = memoryPanel(m), t = text(html);
  assert.match(html, /class="historical"/);
  assert.match(html, /now-confirmed/);
  for (const v of m.revalidation) assert.ok(t.includes(v.fact) && t.includes(v.now), v.fact);
  assert.ok(text(memoryPanel(null)).includes("Memory is off"));
});

test("start panel leads with the registered code change, or says exactly why it is missing", async () => {
  const { startPanel } = await import("../view.js");
  const health = { features: ["codegraph_report"], codegraph: { report_available: true, registered: ["PR-900"] } };
  const pr900 = { ...scenario("PR-104"), pr_id: "PR-900", code_change: { symbol: "contracts/pricing.py::PriceQuote",
    field: "discount_tier", old_type: "str", new_type: "dict", mapping: "validated", code_dependents: 6, code_routes: 2 } };
  const ok = startPanel(health, [pr900]);
  assert.match(ok, /data-open="PR-900"/);
  assert.ok(text(ok).includes("6 function(s) and 2 route(s)"));
  assert.ok(text(startPanel({ features: [] }, [])).includes("older BlastGate process"));
  assert.ok(text(startPanel(health, [])).includes("was not started with it"));
  assert.ok(text(startPanel({ ...health, codegraph: { report_available: false } }, [])).includes("scripts/codegraph_demo.py"));
});

test("a network failure tells the viewer the page's server is not running and how to start it", () => {
  const e = describeError(0, { message: "Failed to fetch (http://127.0.0.1:8767)" });
  assert.equal(e.title, "No response from the server");
  assert.ok(e.detail.includes("127.0.0.1:8767") && e.action.includes("scripts/start_demo.sh"));
});
