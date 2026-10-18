// Console controller: real API calls only. Each panel shows its own loading / error / result state, and a
// failure in one panel is never papered over with another panel's data.
import {
  agentMode, agentPanel, codeOrigin, codegraphPanel, currentBar, describeError, loopStrip, startPanel, detailTables, errorBlock, esc, evidenceInspector, impactMap,
  inventoryTable, mapLegend, simulationPanel, timeoutError, verdictBlock,
} from "./view.js";

const $ = (id) => document.getElementById(id);
const TIMEOUT = { quick: 20_000, analyze: 60_000, investigate: 200_000, simulate: 60_000 };
const state = {
  scenarios: [], current: null, analysis: null, agent: null, selected: null, simulation: null, cgReport: null,
  busy: new Set(), token: 0, liveAccess: null, errors: {}, health: null, offline: false,
};

async function api(path, { body, headers = {}, timeoutMs = TIMEOUT.quick } = {}) {
  try {
    const res = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: { ...(body ? { "content-type": "application/json" } : {}), ...headers },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (state.offline) { state.offline = false; loadHealth(); }   // the server is back: restore the header
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON error body */ }
    return res.ok ? { ok: true, data } : { ok: false, error: describeError(res.status, data) };
  } catch (e) {
    if (e.name === "TimeoutError") return { ok: false, error: timeoutError(timeoutMs) };
    markOffline();
    return { ok: false, error: describeError(0, { message: `${e.message} (${location.origin})` }) };
  }
}

// --- Header: backend health --------------------------------------------------------------------
async function loadHealth() {
  const [ready, health] = await Promise.all([api("/ready"), api("/health")]);
  const db = ready.ok ? ["ok", "Graph database ready"] : ["bad", ready.error.title];
  let agent;
  if (!health.ok) agent = ["bad", "Agent status unavailable"];
  else {
    const p = health.data.agent_provider;
    state.liveAccess = p.live_access ?? null;
    const gate = { token: ", access token required", localhost_only: ", this machine only" }[p.live_access] || "";
    agent = !p.configured ? ["note", `Agent not configured: ${p.detail}`]
      : p.name === "groq" ? ["ok", `Agent: live model ${p.detail}${gate}`]
      : ["note", `Agent: ${agentMode("deterministic-demo").label}`];
  }
  const items = [db, agent];
  if (health.ok) {
    state.health = health.data;
    if (!(health.data.features || []).includes("codegraph_report")) {
      items.push(["bad", "Older server: no code-graph API (restart it with scripts/start_demo.sh)"]);
    }
  }
  $("health").innerHTML = items.map(([cls, text]) => `<span class="${cls}"><span class="dot" aria-hidden="true"></span>${esc(text)}</span>`).join("");
  renderStart();
}

// A network failure means this page's own server stopped: say so in the header, not only in one panel.
function markOffline() {
  if (state.offline) return;
  state.offline = true;
  $("health").innerHTML = `<span class="bad"><span class="dot" aria-hidden="true"></span>${esc(`No server at ${location.origin}. Start scripts/start_demo.sh and open the URL it prints.`)}</span>`;
}

function renderStart() {
  if (state.current || $("result").dataset.view === "codegraph") return;
  $("change").innerHTML = startPanel(state.health, state.scenarios.filter((s) => s.code_change));
  $("change").querySelector("[data-open]")?.addEventListener("click", (e) => select(e.currentTarget.dataset.open));
}

function renderCurrent() {
  const verdict = state.agent?.verdict || state.analysis?.verdict || null;
  $("current").innerHTML = currentBar(state.current, { verdict, progress: $("progress").textContent });
}

// --- Scenario rail -----------------------------------------------------------------------------
async function loadScenarios() {
  const r = await api("/api/scenarios");
  const list = $("scenarios");
  if (!r.ok) {
    list.innerHTML = `<li>${errorBlock(r.error, { retryLabel: "Reload changes" })}</li>`;
    list.querySelector("[data-action=retry]").onclick = loadScenarios;
    return;
  }
  state.scenarios = r.data;
  const item = (s) => `<li><button type="button" data-pr="${esc(s.pr_id)}">
      <span class="pr">${esc(s.pr_id)}</span><span class="title">${esc(s.title)}</span></button></li>`;
  const flagship = r.data.filter((s) => s.code_change);
  $("flagship").innerHTML = flagship.length ? flagship.map(item).join("")
    : `<li class="muted rail-note-tight">None registered on this server.</li>`;
  list.innerHTML = r.data.filter((s) => !s.code_change).map(item).join("");
  document.querySelectorAll("[data-pr]").forEach((b) => { b.onclick = () => select(b.dataset.pr); });
  renderStart();
  const hash = (window.location.hash || "").slice(1);
  if (hash === "codegraph") openCodegraph();
  else if (hash === "tour") {
    const flagshipPr = (r.data.find((s) => s.code_change) || r.data[0])?.pr_id;
    if (flagshipPr) select(flagshipPr);
    startTour(0);
  }
  else if (hash && r.data.some((s) => s.pr_id === hash)) select(hash);
}

function select(prId) {
  const s = state.scenarios.find((x) => x.pr_id === prId);
  if (!s) return;
  state.token += 1;                     // responses for a previous selection are ignored
  Object.assign(state, { current: s, analysis: null, agent: null, selected: null, simulation: null, errors: {} });
  delete $("result").dataset.view;
  if (window.location.hash !== `#${prId}`) history.replaceState(null, "", `#${prId}`);
  document.querySelectorAll("[data-pr]").forEach((b) => b.setAttribute("aria-current", String(b.dataset.pr === prId)));
  $("codegraph").setAttribute("aria-current", "false");
  $("change").innerHTML = `
    <div class="change-head"><p class="pr">${esc(s.pr_id)}</p><p class="title">${esc(s.title)}</p></div>
    <p class="change-meta">${s.code_change
      ? `Detected by the code graph in a git diff: <code>${esc(s.code_change.symbol)}.${esc(s.code_change.field)}</code>
         ${esc(s.code_change.old_type)} → ${esc(s.code_change.new_type)}, mapped to <code>${esc(s.contract_id)}.${esc(s.field)}</code>
         produced by <code>${esc(s.producer)}</code>. ${s.breaking ? `<span class="breaking">Breaking: a field type changed.</span>` : "Not breaking."}`
      : `<code>${esc(s.producer)}</code> changes <code>${esc(s.contract_id)}.${esc(s.field)}</code>: ${esc(s.change)}.
         Declared new type <code>${esc(s.new_type)}</code>.
         ${s.breaking ? `<span class="breaking">Declared breaking.</span>` : "Declared non-breaking."}`}</p>
    <div class="actions">
      <button type="button" class="primary" id="run-analyze">Analyze impact</button>
      <label class="question">Question for the agent (optional)
        <textarea id="question" maxlength="500" rows="1" placeholder="Is it safe to merge?"></textarea></label>
      ${state.liveAccess === "token" ? `<label class="question token">Live access token
        <input id="live-token" type="password" autocomplete="off" spellcheck="false"></label>` : ""}
      <button type="button" class="secondary" id="run-agent">Investigate with agent</button>
    </div>`;
  $("run-analyze").onclick = runAnalyze;
  $("run-agent").onclick = runAgent;
  ["result", "agent", "simulation"].forEach((id) => { $(id).innerHTML = ""; $(id).removeAttribute("data-tone"); });
  $("progress").textContent = "";
  if (s.code_change) {
    $("change").insertAdjacentHTML("beforeend", `<div id="origin"><p class="muted">Loading the code-graph evidence…</p></div>`);
    loadOrigin(state.token, s.code_change);
  }
  renderLoop();
}

function renderLoop() {
  const { current: scenario, analysis, agent, simulation, errors } = state;
  $("loop").innerHTML = loopStrip({ scenario, analysis, agent, simulation, errors, busy: [...state.busy] });
  renderCurrent();
}

// The code-graph report is read-only and server-configured; it is fetched, never generated, by the page.
async function codegraphReport() {
  if (state.cgReport) return { ok: true, data: state.cgReport };
  const r = await api("/api/codegraph/report");
  if (r.ok) state.cgReport = r.data;
  return r;
}

async function loadOrigin(token, cc) {
  const r = await codegraphReport();
  if (token !== state.token || !$("origin")) return;
  $("origin").innerHTML = r.ok ? codeOrigin(r.data, cc)
    : `<p class="muted">Code-graph evidence unavailable: ${esc(r.error.title)}.</p>`;
  $("origin").querySelector("[data-action=open-codegraph]")?.addEventListener("click", openCodegraph);
}

function setBusy(key, on, message) {
  on ? state.busy.add(key) : state.busy.delete(key);
  const any = state.busy.size > 0;
  ["run-analyze", "run-agent"].forEach((id) => { if ($(id)) $(id).disabled = any; });
  const sim = $("run-sim");
  if (sim) sim.disabled = any;
  $("progress").textContent = on ? message : any ? $("progress").textContent : "";
  $(key === "simulate" ? "simulation" : key === "agent" ? "agent" : "result").setAttribute("aria-busy", String(on));
  renderLoop();
}

// --- Deterministic analysis ----------------------------------------------------------------------
async function runAnalyze() {
  if (state.busy.size) return;
  const token = state.token, s = state.current;
  setBusy("analyze", true, `Analyzing ${s.pr_id} against the graph…`);
  const r = await api("/api/analyze", { body: { pr_id: s.pr_id }, timeoutMs: TIMEOUT.analyze });
  setBusy("analyze", false);
  if (token !== state.token) return;
  state.errors.analyze = !r.ok;
  if (!r.ok) { renderLoop(); return showError("result", r.error, runAnalyze, "Retry analysis"); }
  state.analysis = r.data;
  state.selected = null;
  renderResult();
  renderSimulationForm();
  renderLoop();
}

function chooseSelection(next) {
  if (next && state.selected && state.selected.type === next.type && state.selected.id === next.id) {
    state.selected = null;
  } else {
    state.selected = next;
  }
  const a = state.analysis, rep = state.agent, s = state.current;
  if (!a || !s) return;
  const mapEl = $("map-stage");
  const inspEl = $("inspector-stage");
  if (mapEl) mapEl.innerHTML = impactMap(a, s, { selected: state.selected });
  if (inspEl) inspEl.innerHTML = evidenceInspector(a, s, state.selected, rep);
  bindMapInteractions();
}

function bindMapInteractions() {
  const root = $("result");
  if (!root) return;
  const onActivate = (el, fn) => {
    el.onclick = fn;
    el.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        fn();
      } else if (e.key === "Escape" && state.selected) {
        e.preventDefault();
        chooseSelection(null);
      }
    };
  };
  root.querySelectorAll("[data-node]").forEach((el) => {
    onActivate(el, () => chooseSelection({ type: "node", id: el.dataset.node }));
  });
  root.querySelectorAll("[data-edge]").forEach((el) => {
    onActivate(el, () => chooseSelection({ type: "edge", id: el.dataset.edge }));
  });
  root.querySelectorAll("[data-select-node]").forEach((btn) => {
    btn.onclick = () => chooseSelection({ type: "node", id: btn.dataset.selectNode });
  });
  root.querySelectorAll("[data-select-edge]").forEach((btn) => {
    btn.onclick = () => chooseSelection({ type: "edge", id: btn.dataset.selectEdge });
  });
  const closeBtn = root.querySelector("[data-action=close-inspector]");
  if (closeBtn) closeBtn.onclick = () => chooseSelection(null);
}

function renderResult() {
  const a = state.analysis, rep = state.agent, s = state.current;
  const source = a || rep;
  if (!source) return;
  const verdict = a ? a.verdict : rep.verdict;
  const reasons = a ? a.reasons : rep.verdict_reasons;
  // Prefer the agent's inventory when present: it adds lookups the agent ran (for example incidents).
  const inv = rep ? rep.inventory : a.inventory;
  const summary = rep ? rep.summary : a.summary;
  const invSource = rep ? "From the agent investigation, including lookups it ran." : "From deterministic analysis.";
  $("result").dataset.tone = verdict.toLowerCase();
  $("result").innerHTML = `
    ${verdictBlock(verdict, reasons, "Decided by the deterministic rules engine. Agent output cannot change it.")}
    <p class="summary">${esc(summary)}</p>
    ${a ? `<h2 class="section">Impact map &amp; graph evidence explorer</h2>
      <div class="map-wrap" id="map-stage">${impactMap(a, s, { selected: state.selected })}</div>${mapLegend(a)}
      <div id="inspector-stage">${evidenceInspector(a, s, state.selected, rep)}</div>` : ""}
    <h2 class="section">Evidence</h2>
    <p class="muted">${esc(invSource)}</p>
    ${inventoryTable(inv)}
    ${a ? detailTables(a) : `<p class="muted">Run <em>Analyze impact</em> for consumer, path, SLA and incident detail.</p>`}`;
  bindMapInteractions();
}

// --- Agent investigation -------------------------------------------------------------------------
async function runAgent() {
  if (state.busy.size) return;
  const token = state.token, s = state.current;
  const question = $("question").value.trim();
  setBusy("agent", true, `Investigating ${s.pr_id} with the agent. Live model runs can take up to a minute…`);
  const [analysis, agent] = await Promise.all([
    state.analysis ? Promise.resolve({ ok: true, data: state.analysis })
      : api("/api/analyze", { body: { pr_id: s.pr_id }, timeoutMs: TIMEOUT.analyze }),
    api("/api/agent/investigate", { body: { pr_id: s.pr_id, ...(question ? { question } : {}) },
      headers: liveTokenHeader(), timeoutMs: TIMEOUT.investigate }),
  ]);
  setBusy("agent", false);
  if (token !== state.token) return;
  if (analysis.ok) state.analysis = analysis.data;
  if (agent.ok) state.agent = agent.data;
  Object.assign(state.errors, { analyze: !analysis.ok, agent: !agent.ok });
  if (analysis.ok || agent.ok) { renderResult(); renderSimulationForm(); }
  else showError("result", analysis.error, runAnalyze, "Retry analysis");
  renderLoop();
  // A failed agent run is shown as failed. The deterministic result above (if any) is labelled as such.
  if (agent.ok) $("agent").innerHTML = `<h2 class="section">Agent investigation</h2>${agentPanel(agent.data)}`;
  else showError("agent", agent.error, runAgent, "Retry investigation",
    `<h2 class="section">Agent investigation</h2><p class="muted">The agent did not complete. No agent output is shown.</p>`);
}

// The token stays in the field (memory only) and is sent only to this origin's investigate endpoint.
function liveTokenHeader() {
  const t = $("live-token")?.value.trim();
  return t ? { authorization: `Bearer ${t}` } : {};
}

// --- Counterfactual simulation -------------------------------------------------------------------
function renderSimulationForm() {
  const a = state.analysis, s = state.current;
  if (!a) return;
  const head = `<h2 class="section">Counterfactual simulation</h2>
    <p class="muted">Copies the graph, assumes one consumer gains a fallback, and re-runs the rules on the copy.
      It shows what the topology would imply, not whether real code works.</p>`;
  if (!a.consumers.length) {
    $("simulation").innerHTML = `${head}<p class="muted">This field has no indexed consumer, so there is nothing to simulate.</p>`;
    return;
  }
  const first = (a.vulnerable_consumers[0] || a.consumers[0]).service;
  const template = (c) => `${c} adds a backward-compatible fallback for ${s.field}`;
  $("simulation").innerHTML = `${head}
    <form class="sim-form" id="sim-form">
      <label>Consumer that gains a fallback
        <select id="sim-consumer">${a.consumers.map((c) => `<option value="${esc(c.service)}"${c.service === first ? " selected" : ""}>${esc(c.service)}${c.fallback_enabled === false ? " (no fallback today)" : ""}</option>`).join("")}</select></label>
      <label>Your assumption (10 to 300 characters)
        <textarea id="sim-assumption" minlength="10" maxlength="300" rows="2" required>${esc(template(first))}</textarea></label>
      <div><button type="submit" class="secondary" id="run-sim">Run simulation</button></div>
    </form>
    <div id="sim-result"></div>`;
  let edited = false;
  $("sim-assumption").oninput = () => { edited = true; };
  $("sim-consumer").onchange = (e) => { if (!edited) $("sim-assumption").value = template(e.target.value); };
  $("sim-form").onsubmit = (e) => { e.preventDefault(); runSimulation(); };
}

async function runSimulation() {
  if (state.busy.size) return;
  const token = state.token, s = state.current;
  const body = { pr_id: s.pr_id, consumer: $("sim-consumer").value, assumption: $("sim-assumption").value.trim() };
  setBusy("simulate", true, "Simulating on a temporary copy of the graph…");
  const r = await api("/api/simulate", { body, timeoutMs: TIMEOUT.simulate });
  setBusy("simulate", false);
  if (token !== state.token) return;
  state.errors.simulate = !r.ok;
  if (!r.ok) { renderLoop(); return showError("sim-result", r.error, runSimulation, "Retry simulation"); }
  state.simulation = r.data;
  $("sim-result").innerHTML = simulationPanel(r.data, s);
  renderLoop();
}

// --- Code-graph review (read-only: shows a pre-generated report; this page cannot start a review) ---------
async function openCodegraph() {
  if (state.busy.size) return;
  state.token += 1;
  const token = state.token;
  Object.assign(state, { current: null, analysis: null, agent: null, selected: null });
  document.querySelectorAll("[data-pr]").forEach((b) => b.setAttribute("aria-current", "false"));
  $("codegraph").setAttribute("aria-current", "true");
  if (window.location.hash !== "#codegraph") history.replaceState(null, "", "#codegraph");
  $("result").dataset.view = "codegraph";
  $("current").innerHTML = "";
  $("change").innerHTML = `
    <div class="change-head"><p class="pr">Git diff</p><p class="title">Code-graph review</p></div>
    <p class="change-meta">A real git change reviewed by Gristle's code graph, mapped through validated contracts into the
      service graph. The report is generated offline by <code>scripts/codegraph_demo.py</code>; this page only reads it.</p>`;
  ["result", "agent", "simulation", "loop"].forEach((id) => { $(id).innerHTML = ""; $(id).removeAttribute("data-tone"); });
  setBusy("analyze", true, "Loading the code-graph review…");
  state.cgReport = null;   // always show the file as it is now
  const r = await codegraphReport();
  setBusy("analyze", false);
  if (token !== state.token) return;
  if (!r.ok) return showError("result", r.error, openCodegraph, "Reload review");
  // A detected change can be handed to the agent only if the server registered it (BLASTGATE_CODEGRAPH_REPORT).
  const prFor = (c) => state.scenarios.find((s) => s.code_change
    && s.code_change.symbol === c.symbol && s.code_change.field === c.field)?.pr_id ?? null;
  $("result").dataset.tone = r.data.verdict.toLowerCase();
  $("result").innerHTML = codegraphPanel(r.data, prFor);
  $("result").querySelectorAll("[data-investigate]").forEach((b) => { b.onclick = () => select(b.dataset.investigate); });
}
$("codegraph").onclick = openCodegraph;

// --- Errors ------------------------------------------------------------------------------------
function showError(targetId, err, retry, label, prefix = "") {
  const el = $(targetId);
  if (targetId === "result") el.removeAttribute("data-tone");
  el.innerHTML = prefix + errorBlock(err, { retryLabel: label });
  const btn = el.querySelector("[data-action=retry]");
  btn.onclick = retry;
  const wait = Number(err.retryAfter);
  if (wait > 0) {                      // honour the provider's retry hint
    btn.disabled = true;
    setTimeout(() => { btn.disabled = false; }, wait * 1000);
  }
}

// --- Judge-Friendly Guided Tour ----------------------------------------------------------------
const TOUR_STEPS = [
  {
    target: "change",
    title: "Step 1: Real Code Change (PR-900)",
    text: "Here is the real Git diff in contracts/pricing.py: PriceQuote.discount_tier changes from string to dict. A single-line change, but what could break across downstream microservices?",
  },
  {
    target: "origin",
    altTarget: "codegraph",
    title: "Step 2: Gristle AST Code Graph",
    text: "Gristle indexes Python AST structures into FalkorDB, discovering 6 dependent functions and 2 API routes connected to PriceQuote. Code-level relationships are clearly distinguished from runtime service calls.",
  },
  {
    target: "loop",
    title: "Step 3: Validated Contract Mapping",
    text: "blastgate.map.json validates that the code-side symbol PriceQuote.discount_tier maps directly to pricing-api-v2.discount_tier emitted by the pricing-engine service.",
  },
  {
    target: "run-agent",
    altTarget: "agent",
    title: "Step 4: FalkorDB Multi-Hop & MCP Investigation",
    text: "When you click 'Investigate with agent', the agent calls 5 allowlisted Model Context Protocol (MCP) tools: inspecting schema coverage, traversing downstream graph blast radius, computing betweenness centrality, and pulling past incidents.",
  },
  {
    target: "result",
    title: "Step 5: Deterministic BLOCK Verdict",
    text: "The deterministic policy engine returns BLOCK: checkout-api consumes this field with NO fallback enabled, cascading to critical order-orchestrator ($43,000 assumed SLA downtime penalty).",
  },
  {
    target: "simulation",
    title: "Step 6: Counterfactual Simulation (GRAPH.COPY)",
    text: "Run a what-if experiment: what if checkout-api adds a fallback? BlastGate creates an isolated temporary copy of the graph (GRAPH.COPY). The simulated verdict shifts to CLEAR, while production state remains strictly untouched.",
  },
  {
    target: "inspector-stage",
    altTarget: "result",
    title: "Step 7: Traceable Evidence & Reviewers",
    text: "Every conclusion links to verified Cypher evidence: betweenness centrality (4.0 highest on order-orchestrator), outage INC-2041, and suggested reviewers (eng-alpha, eng-bravo).",
  },
];

let tourIndex = 0;

function startTour(step = 0) {
  const guide = $("tour-guide");
  if (!guide) return;
  tourIndex = Math.max(0, Math.min(step, TOUR_STEPS.length - 1));
  guide.hidden = false;
  renderTourStep();
}

function closeTour() {
  const guide = $("tour-guide");
  if (!guide) return;
  guide.hidden = true;
  document.querySelectorAll(".tour-highlight").forEach((el) => el.classList.remove("tour-highlight"));
}

function renderTourStep() {
  const step = TOUR_STEPS[tourIndex];
  if (!step) return closeTour();
  const badge = $("tour-step-badge");
  if (badge) badge.textContent = `Step ${tourIndex + 1} of ${TOUR_STEPS.length}`;
  const title = $("tour-title");
  if (title) title.textContent = step.title;
  const text = $("tour-text");
  if (text) text.textContent = step.text;
  const prevBtn = $("tour-prev");
  if (prevBtn) prevBtn.disabled = tourIndex === 0;
  const nextBtn = $("tour-next");
  if (nextBtn) nextBtn.textContent = tourIndex === TOUR_STEPS.length - 1 ? "Finish Tour ✓" : "Next Step →";

  document.querySelectorAll(".tour-highlight").forEach((el) => el.classList.remove("tour-highlight"));
  const el = $(step.target) || $(step.altTarget);
  if (el) {
    el.classList.add("tour-highlight");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function nextTourStep() {
  if (tourIndex >= TOUR_STEPS.length - 1) {
    closeTour();
  } else {
    tourIndex += 1;
    renderTourStep();
  }
}

function prevTourStep() {
  if (tourIndex > 0) {
    tourIndex -= 1;
    renderTourStep();
  }
}

function bindTourControls() {
  const btnStart = $("btn-start-tour");
  if (btnStart) btnStart.onclick = () => {
    if (!state.current) {
      const flagshipPr = (state.scenarios.find((s) => s.code_change) || state.scenarios[0])?.pr_id;
      if (flagshipPr) select(flagshipPr);
    }
    startTour(0);
  };
  const btnClose = $("tour-close");
  if (btnClose) btnClose.onclick = closeTour;
  const btnPrev = $("tour-prev");
  if (btnPrev) btnPrev.onclick = prevTourStep;
  const btnNext = $("tour-next");
  if (btnNext) btnNext.onclick = nextTourStep;
  const backdrop = $("tour-backdrop");
  if (backdrop) backdrop.onclick = closeTour;

  window.addEventListener("keydown", (e) => {
    const guide = $("tour-guide");
    if (!guide || guide.hidden) return;
    if (e.key === "Escape") closeTour();
    else if (e.key === "ArrowRight") nextTourStep();
    else if (e.key === "ArrowLeft") prevTourStep();
  });
}

loadHealth();
loadScenarios();
bindTourControls();

window.addEventListener("hashchange", () => {
  const hash = (window.location.hash || "").slice(1);
  if (hash === "codegraph") openCodegraph();
  else if (hash === "tour") {
    if (!state.current) {
      const flagshipPr = (state.scenarios.find((s) => s.code_change) || state.scenarios[0])?.pr_id;
      if (flagshipPr) select(flagshipPr);
    }
    startTour(0);
  }
  else if (hash && state.scenarios.some((s) => s.pr_id === hash)) select(hash);
});
