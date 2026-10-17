// Pure view functions: API JSON in, escaped HTML strings out. No DOM, no fetch, so node --test can run them.
// Rule: render only what the API returned. Absent data is shown as absent, never filled in.

const ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ENT[c]);
const code = (v) => `<code>${esc(v)}</code>`;

// Each description restates the engine's rule (README "Verdict semantics"); the reasons come from the API.
export const VERDICTS = {
  BLOCK: { glyph: "✕", word: "Block", tone: "block",
    meaning: "A breaking change reaches a consumer without a fallback, and a critical service or SLA." },
  WARN: { glyph: "!", word: "Warn", tone: "warn",
    meaning: "Coverage is incomplete, the target is unverified, or the impact can't be resolved." },
  CLEAR: { glyph: "✓", word: "Clear", tone: "clear",
    meaning: "Nothing in the mapped graph blocks or warns, under the modelled assumptions." },
};

export const STATUS = {
  found: { label: "Found", glyph: "●" },
  none_found: { label: "Checked, none found", glyph: "○" },
  unknown: { label: "Unknown", glyph: "?" },
  not_checked: { label: "Not checked", glyph: "–" },
  check_failed: { label: "Check failed", glyph: "✕" },
};

export const CATEGORIES = [
  ["consumers", "Consumers of the field"],
  ["vulnerable_consumers", "Consumers without a fallback"],
  ["affected_services", "Affected services"],
  ["critical_services", "Affected critical services"],
  ["slas", "SLAs at risk"],
  ["incidents", "Incidents on this contract"],
  ["reviewers", "Suggested reviewers"],
];

export const CENTRALITY_LABEL =
  "FalkorDB algo.betweenness over Service/DEPENDS_ON (sampled approximation, size=32, seed=1; informational only, never a verdict input).";

/** Identify the highest positive betweenness score dynamically from backend centrality data. */
export function highestCentrality(centrality) {
  if (!centrality || typeof centrality !== "object") return { maxService: null, maxScore: null };
  const entries = Object.entries(centrality).filter(([, v]) => typeof v === "number" && !Number.isNaN(v));
  if (!entries.length) return { maxService: null, maxScore: null };
  entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const [topService, topScore] = entries[0];
  return { maxService: topScore > 0 ? topService : null, maxScore: topScore };
}

/** Distinguish unavailable centrality (null / missing) from a true 0.0 and positive scores. */
export function formatCentrality(centrality, service) {
  if (centrality === null || centrality === undefined) {
    return { status: "unavailable", score: null, isHighest: false, short: "not run",
      text: "Not run (centrality algorithm only runs when services are affected)." };
  }
  const raw = centrality[service];
  if (typeof raw !== "number" || Number.isNaN(raw)) {
    return { status: "unavailable", score: null, isHighest: false, short: "unavailable",
      text: "Unavailable for this service." };
  }
  const { maxScore } = highestCentrality(centrality);
  const isHighest = maxScore !== null && maxScore > 0 && raw === maxScore;
  const num = raw.toFixed(1);
  return {
    status: "available",
    score: raw,
    isHighest,
    short: isHighest ? `${num} (highest)` : num,
    text: isHighest
      ? `${num} — highest sampled betweenness among affected services (informational chokepoint indicator, not a verdict input)`
      : `${num} — sampled betweenness approximation (informational only, not a verdict input)`,
  };
}

/** Which planner produced an investigation, from the backend's agent_mode string. */
export function agentMode(mode = "") {
  if (mode.startsWith("llm: ")) return { kind: "live", label: `Live model: ${mode.slice(5)}` };
  if (mode.startsWith("deterministic-demo")) return { kind: "demo", label: "Demo planner, rule-based, no LLM" };
  if (mode.startsWith("test:")) return { kind: "test", label: `Test planner: ${mode.slice(5).trim()}` };
  return { kind: "unknown", label: mode || "Unknown planner" };
}

const ERRORS = {
  unknown_scenario: ["Unknown change", "Pick a change from the list."],
  graph_unavailable: ["The graph database is unreachable", "Start FalkorDB, then retry."],
  analysis_failed: ["Graph analysis failed", "No verdict was produced. Retry, or check the server log."],
  agent_provider_not_configured: ["The agent is not configured",
    "Set AGENT_PROVIDER and the provider key on the server. Deterministic analysis still works."],
  provider_rate_limited: ["The model provider is rate-limiting requests", "Wait, then retry."],
  provider_timeout: ["The model provider timed out", "Retry; if it keeps happening, raise AGENT_TIMEOUT_S."],
  provider_auth: ["The model provider rejected the server's API key", "Replace the key on the server."],
  provider_model_unavailable: ["The configured model isn't available", "Set AGENT_MODEL to an available model."],
  provider_bad_request: ["The model produced an invalid request", "Retry the investigation."],
  provider_bad_response: ["The model returned an unusable answer", "Retry the investigation."],
  provider_network: ["Couldn't reach the model provider", "Check the server's network, then retry."],
  provider_error: ["The model provider returned an error", "Retry later."],
  tool_server_unavailable: ["The agent's tool server didn't start", "Check the server log, then retry."],
  live_access_denied: ["Live investigations need an access token", "Enter the server's live access token, then retry."],
  live_access_local_only: ["Live investigations only run on the server's own machine",
    "Open the console on that machine, or ask the owner to set AGENT_LIVE_TOKEN."],
  live_rate_limited: ["This server's live investigation limit was reached", "Wait, then retry."],
  invalid_target: ["That consumer can't be simulated", "Pick a service that consumes this field."],
  invalid_input: ["The request was rejected", "Check the inputs."],
  unavailable: ["The graph database is unreachable", "Start FalkorDB, then retry."],
  integrity: ["The simulation couldn't be verified as contained", "The result was discarded. Retry."],
  internal_error: ["The server hit an unexpected error", "Retry; check the server log if it persists."],
};

/** Normalise any failed response (or a network/timeout failure, status 0) into display fields. */
export function describeError(status, body) {
  if (status === 0) {
    return { title: "No response from the server", detail: body?.message || "",
      action: "This page's server is not running. Start it (scripts/start_demo.sh), open the URL it prints, then retry.",
      retryAfter: null, code: "network" };
  }
  if (status === 422 && Array.isArray(body?.detail)) {
    const parts = body.detail.map((d) => `${(d.loc || []).filter((x) => x !== "body").join(".")}: ${d.msg}`);
    return { title: "The request was rejected", detail: parts.join("; "), action: "Fix the input and retry.",
      retryAfter: null, code: "validation" };
  }
  const [title, action] = ERRORS[body?.error] || [`Request failed (HTTP ${status})`, "Retry."];
  return { title, detail: body?.message || "", action, retryAfter: body?.retry_after ?? null, code: body?.error };
}

export function timeoutError(ms) {
  return { title: "The request timed out", detail: `No answer within ${Math.round(ms / 1000)} s.`,
    action: "Retry. A live model may be slow or rate-limited.", retryAfter: null, code: "timeout" };
}

export function errorBlock(err, { retryLabel = "Retry" } = {}) {
  const wait = err.retryAfter ? ` Suggested wait: ${esc(err.retryAfter)} s.` : "";
  return `<div class="error" role="alert">
    <p class="error-title">${esc(err.title)}</p>
    ${err.detail ? `<p class="error-detail">${esc(err.detail)}</p>` : ""}
    <p class="error-action">${esc(err.action)}${wait}</p>
    <button type="button" class="ghost" data-action="retry">${esc(retryLabel)}</button>
  </div>`;
}

export function verdictChip(verdict) {
  const v = VERDICTS[verdict];
  return v ? `<span class="chip chip-${v.tone}"><span aria-hidden="true">${v.glyph}</span> ${esc(verdict)}</span>` : esc(verdict);
}

export function verdictBlock(verdict, reasons, sourceNote) {
  const v = VERDICTS[verdict];
  if (!v) return `<p class="muted">No verdict.</p>`;
  return `<div class="verdict verdict-${v.tone}">
    <p class="stamp" aria-label="Verdict: ${esc(v.word)}"><span class="stamp-glyph" aria-hidden="true">${v.glyph}</span>${esc(verdict)}</p>
    <div class="verdict-body">
      <p class="verdict-meaning">${esc(v.meaning)}</p>
      <ul class="reasons">${(reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <p class="source-note">${esc(sourceNote)}</p>
    </div>
  </div>`;
}

export function inventoryTable(inventory) {
  if (!inventory) return "";
  const rows = CATEGORIES.filter(([k]) => inventory[k]).map(([k, label]) => {
    const c = inventory[k], s = STATUS[c.status] || { label: c.status, glyph: "?" };
    const items = c.items?.length ? c.items.map(code).join(" ") : `<span class="muted" aria-label="no items">—</span>`;
    return `<tr class="status-${esc(c.status)}">
      <th scope="row">${esc(label)}</th>
      <td><span class="status"><span aria-hidden="true">${s.glyph}</span> ${esc(s.label)}</span></td>
      <td>${items}</td>
      <td class="muted">${esc(c.source)}</td></tr>`;
  });
  return `<table class="inventory"><thead><tr><th>Evidence</th><th>Status</th><th>Items</th><th>Source</th></tr></thead>
    <tbody>${rows.join("")}</tbody></table>`;
}

const table = (head, rows, empty) => rows.length
  ? `<table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`
  : `<p class="muted">${esc(empty)}</p>`;

/** Detail tables from /api/analyze. null means "not looked up", which differs from an empty list. */
export function detailTables(a) {
  const yesNo = (v) => (v === true ? "yes" : v === false ? "no" : `unknown (${esc(v)})`);
  const consumers = table(["Service", "Tier", "Fallback", "Evidence source"], a.consumers.map((c) =>
    `<tr><td>${code(c.service)}</td><td>${esc(c.tier)}</td><td>${yesNo(c.fallback_enabled)}</td><td class="muted">${esc(c.evidence_source)}</td></tr>`),
    "No indexed consumer of this field.");
  const affected = table(["Service", "Tier", "Hops from producer", "Betweenness (sampled)", "Path"], a.affected_services.map((s) => {
    const cent = formatCentrality(a.centrality, s.service);
    return `<tr><td>${code(s.service)}</td><td>${esc(s.tier)}</td><td>${esc(s.depth)}</td><td>${esc(cent.short)}</td><td>${s.path.map(code).join(" ")}</td></tr>`;
  }), "No affected service.");
  const centNote = a.affected_services.length ? `<p class="muted centrality-note">${esc(CENTRALITY_LABEL)}</p>` : "";
  const slas = table(["SLA", "Name", "Backed by", "Penalty per hour"], a.slas.map((s) =>
    `<tr><td>${code(s.sla_id)}</td><td>${esc(s.name)}</td><td>${code(s.service)}</td><td>$${esc(s.penalty_per_hour_usd)} (synthetic)</td></tr>`),
    "No SLA at risk.");
  const exposure = a.exposure ? `<p class="exposure">Assumed exposure <strong>$${esc(a.exposure.assumed_total_usd.toLocaleString("en-US"))}</strong>
    over ${esc(a.exposure.assumed_outage_hours)} h. <span class="muted">${esc(a.exposure.label)}</span></p>` : "";
  const incidents = a.incidents === null ? `<p class="muted">Not looked up: no service is affected.</p>`
    : table(["Incident", "Severity", "Summary", "Resolved by"], a.incidents.map((i) =>
      `<tr><td>${code(i.incident_id)}</td><td>${esc(i.severity)}</td><td>${esc(i.summary)}</td><td>${i.resolved_by.map(code).join(" ")}</td></tr>`),
      "None found on this contract.");
  const reviewers = a.reviewers === null ? `<p class="muted">Not looked up: no service is affected.</p>`
    : table(["Engineer", "Resolved incidents", "Committed to"], a.reviewers.map((r) =>
      `<tr><td>${code(r.handle)} <span class="muted">${esc(r.display_name)}</span></td><td>${r.resolved_incidents.map(code).join(" ")}</td><td>${r.authored_services.map(code).join(" ")}</td></tr>`),
      "No connected engineer.");
  return `<div class="details">
    <section><h3>Consumers</h3>${consumers}</section>
    <section><h3>Affected services</h3>${affected}${centNote}</section>
    <section><h3>SLAs</h3>${slas}${exposure}</section>
    <section><h3>Incidents</h3>${incidents}</section>
    <section><h3>Reviewers</h3>${reviewers}</section>
  </div>`;
}

/** Legend for the edge kinds actually drawn by impactMap for this response. */
export function mapLegend(a) {
  if (!a.field_found || !a.consumers.length) return "";
  const parts = [];
  if (a.consumers.some((c) => c.fallback_enabled !== true)) {
    parts.push(`<span class="swatch" aria-hidden="true"></span>impact path, in the verdict colour`);
  }
  if (a.consumers.some((c) => c.fallback_enabled === true)) {
    parts.push(`<span class="swatch safe" aria-hidden="true"></span>consumer with a fallback`);
  }
  return `<p class="legend">${parts.join(" ")}. Arrows show how impact flows; in the graph, DEPENDS_ON edges
    point the other way. Only edges the API returned are drawn.</p>`;
}

function normaliseSelection(selected) {
  if (!selected) return null;
  if (typeof selected === "string") {
    if (selected.startsWith("edge:")) return { type: "edge", id: selected.slice(5) };
    if (selected.startsWith("node:")) return { type: "node", id: selected.slice(5) };
    return { type: selected.includes("->") ? "edge" : "node", id: selected };
  }
  if (typeof selected === "object" && selected.type && selected.id) return selected;
  return null;
}

/** Node-link map built only from returned consumers (CONSUMES_FIELD) and affected-service paths. */
export function impactMap(a, scenario, opts = {}) {
  if (!a.field_found) return `<p class="muted">The change target isn't in the graph, so there is nothing to map.</p>`;
  const sel = normaliseSelection(opts.selected);
  const W = 176, H = 50, GX = 56, GY = 14, PAD = 8;
  const slaCount = {};
  (a.slas || []).forEach((s) => { slaCount[s.service] = (slaCount[s.service] || 0) + 1; });
  const affected = new Map((a.affected_services || []).map((s) => [s.service, s]));
  const consumerBySvc = new Map((a.consumers || []).map((c) => [c.service, c]));
  const nodes = new Map([["__field", { id: "__field", col: 0, kind: "field", title: scenario.field,
    sub: scenario.contract_id, tip: `Changed field ${scenario.contract_id}.${scenario.field}, exposed by ${scenario.producer}` }]]);
  const edges = [];
  (a.consumers || []).forEach((c) => {
    nodes.set(c.service, { id: c.service, col: 1, kind: affected.has(c.service) ? "hit" : "safe", title: c.service,
      sub: c.fallback_enabled === true ? "has fallback" : c.fallback_enabled === false ? "no fallback" : "fallback unknown" });
    edges.push(["__field", c.service, c.fallback_enabled === true ? "safe" : "hit"]);
  });
  (a.affected_services || []).filter((s) => s.depth >= 2).forEach((s) => {
    nodes.set(s.service, { id: s.service, col: s.depth, kind: "hit", title: s.service, sub: `${s.tier}, ${s.depth} hops` });
  });
  (a.affected_services || []).forEach((s) => s.path.slice(1).forEach((n, i, p) => {
    if (i && !edges.some((e) => e[0] === p[i - 1] && e[1] === n)) edges.push([p[i - 1], n, "hit"]);
  }));
  if (nodes.size === 1) return `<p class="muted">No consumer of this field is indexed in the graph.</p>`;
  const cols = {};
  [...nodes.values()].sort((x, y) => x.id.localeCompare(y.id)).forEach((n) => { (cols[n.col] ||= []).push(n); });
  const maxRows = Math.max(...Object.values(cols).map((c) => c.length));
  Object.values(cols).forEach((c) => c.forEach((n, i) => {
    n.x = PAD + n.col * (W + GX);
    n.y = PAD + ((maxRows - c.length) * (H + GY)) / 2 + i * (H + GY);
  }));
  const width = PAD * 2 + (Math.max(...Object.keys(cols).map(Number)) + 1) * (W + GX) - GX;
  const height = PAD * 2 + maxRows * (H + GY) - GY;
  const edgeSvg = edges.map(([f, t, k]) => {
    const s = nodes.get(f), d = nodes.get(t);
    if (!s || !d) return "";
    const edgeId = `${f}->${t}`;
    const isSel = sel?.type === "edge" && sel.id === edgeId;
    const c = f === "__field" ? consumerBySvc.get(t) : null;
    const relType = f === "__field" ? "CONSUMES_FIELD" : "DEPENDS_ON";
    const edgeTip = f === "__field"
      ? `CONSUMES_FIELD: (${t})-[:CONSUMES_FIELD {fallback_enabled: ${c?.fallback_enabled}}]->(${scenario.contract_id}.${scenario.field})`
      : `DEPENDS_ON: (${t})-[:DEPENDS_ON]->(${f}) (impact flows ${f} -> ${t})`;
    const x1 = s.x + W, y1 = s.y + H / 2, x2 = d.x - 2, y2 = d.y + H / 2, mx = (x1 + x2) / 2;
    return `<path class="edge edge-${k}" data-edge="${esc(edgeId)}" data-rel="${esc(relType)}"${isSel ? ' data-selected="true"' : ""} tabindex="0" role="button" aria-label="${esc(edgeTip)}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" marker-end="url(#arrow-${k})"><title>${esc(edgeTip)}</title></path>`;
  }).join("");
  const nodeSvg = [...nodes.values()].map((n) => {
    const sla = slaCount[n.id];
    const cent = n.id !== "__field" ? formatCentrality(a.centrality, n.id) : { status: "unavailable" };
    const btBadge = cent.status === "available"
      ? `<text class="badge badge-bt" x="${n.x + W - 10}" y="${n.y + 21}" text-anchor="end">BT ${esc(cent.score.toFixed(1))}${cent.isHighest ? " ★" : ""}</text>`
      : "";
    const badge = sla ? `<text class="badge" x="${n.x + W - 10}" y="${n.y + 38}" text-anchor="end">SLA ×${sla}</text>` : "";
    const centTip = cent.status === "available" ? `, betweenness ${cent.short}` : "";
    const tip = n.tip || `${n.title}: ${n.sub}${centTip}${sla ? `, backs ${sla} SLA` : ""}`;
    const title = n.title.length > 22 ? `${n.title.slice(0, 21)}…` : n.title;
    const isSel = sel?.type === "node" && sel.id === n.id;
    return `<g class="node node-${n.kind}" data-node="${esc(n.id)}"${isSel ? ' data-selected="true"' : ""} tabindex="0" role="button" aria-label="${esc(tip)}"><title>${esc(tip)}</title>
      <rect x="${n.x}" y="${n.y}" width="${W}" height="${H}" rx="6"/>
      <text class="node-title" x="${n.x + 12}" y="${n.y + 21}">${esc(title)}</text>
      <text class="node-sub" x="${n.x + 12}" y="${n.y + 38}">${esc(n.sub)}</text>${btBadge}${badge}</g>`;
  }).join("");
  return `<svg class="impact-map" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="group" aria-label="Impact map">
    <defs>${["hit", "safe"].map((k) => `<marker id="arrow-${k}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path class="arrow-${k}" d="M0,0 L8,4 L0,8 z"/></marker>`).join("")}</defs>
    ${edgeSvg}${nodeSvg}</svg>`;
}

/** Build the list of actual selectable nodes and edges from the API response. */
export function selectableGraphItems(a, scenario) {
  if (!a || !a.field_found) return { nodes: [], edges: [] };
  const affected = new Map((a.affected_services || []).map((s) => [s.service, s]));
  const nodes = [{ id: "__field", label: `${scenario.contract_id}.${scenario.field} (SchemaField)` }];
  const seenNodes = new Set(["__field"]);
  const edges = [];
  (a.consumers || []).forEach((c) => {
    if (!seenNodes.has(c.service)) {
      seenNodes.add(c.service);
      nodes.push({ id: c.service, label: `${c.service} (Consumer)` });
    }
    edges.push({ id: `__field->${c.service}`, rel: "CONSUMES_FIELD", from: "__field", to: c.service,
      label: `CONSUMES_FIELD: ${c.service} -> ${scenario.field}` });
  });
  (a.affected_services || []).filter((s) => s.depth >= 2).forEach((s) => {
    if (!seenNodes.has(s.service)) {
      seenNodes.add(s.service);
      nodes.push({ id: s.service, label: `${s.service} (depth ${s.depth})` });
    }
  });
  (a.affected_services || []).forEach((s) => s.path.slice(1).forEach((n, i, p) => {
    if (i && !edges.some((e) => e.from === p[i - 1] && e.to === n)) {
      edges.push({ id: `${p[i - 1]}->${n}`, rel: "DEPENDS_ON", from: p[i - 1], to: n,
        label: `DEPENDS_ON: ${n} -> ${p[i - 1]}`, depth: affected.get(n)?.depth });
    }
  }));
  return { nodes, edges };
}

function renderUnknownsForInspector(a, inv) {
  const items = [];
  if (!a.contract_found) items.push("Producer/contract exposure relationship is not verified in the graph.");
  else if (!a.field_found) items.push("Target schema field is not modelled on the contract.");
  if (a.mapping_status && a.mapping_status !== "complete") {
    items.push(`Consumer mapping is '${a.mapping_status}': ${a.coverage_note || "unindexed consumers may exist."}`);
  }
  (a.consumers || []).forEach((c) => {
    if (typeof c.fallback_enabled !== "boolean" || !c.evidence_source) {
      items.push(`Fallback evidence is unverified for consumer ${c.service}.`);
    }
  });
  if (inv) {
    CATEGORIES.forEach(([k, label]) => {
      const check = inv[k];
      if (check && (check.status === "unknown" || check.status === "not_checked" || check.status === "check_failed")) {
        const st = STATUS[check.status]?.label || check.status;
        items.push(`${label}: ${st} (${check.source}).`);
      }
    });
  }
  return items;
}

/** Interactive Graph Evidence Inspector for a selected node or relationship. */
export function evidenceInspector(a, scenario, selected = null, rep = null) {
  if (!a || !a.field_found) {
    return `<div class="inspector inspector-empty" role="region" aria-label="Graph evidence inspector">
      <p class="muted">The change target isn't in the graph, so no node or path evidence can be inspected.</p>
    </div>`;
  }
  const inv = rep?.inventory || a.inventory || null;
  const sel = normaliseSelection(selected);
  const { nodes, edges } = selectableGraphItems(a, scenario);
  if (nodes.length <= 1) {
    const unknowns = renderUnknownsForInspector(a, inv);
    return `<div class="inspector" role="region" aria-label="Graph evidence inspector">
      <p class="muted">No indexed consumers exist for ${code(`${scenario.contract_id}.${scenario.field}`)} in the checked graph.</p>
      ${unknowns.length ? `<h4>Unknown, unchecked or failed evidence</h4><ul class="inspector-unknowns">${unknowns.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}
    </div>`;
  }

  const nodeButtons = nodes.map((n) => {
    const active = sel?.type === "node" && sel.id === n.id;
    return `<button type="button" class="pill${active ? " is-active" : ""}" data-select-node="${esc(n.id)}" aria-pressed="${active}">${esc(n.label)}</button>`;
  }).join("");
  const edgeButtons = edges.map((e) => {
    const active = sel?.type === "edge" && sel.id === e.id;
    return `<button type="button" class="pill pill-edge${active ? " is-active" : ""}" data-select-edge="${esc(e.id)}" aria-pressed="${active}">${esc(e.label)}</button>`;
  }).join("");
  const clearBtn = sel ? `<button type="button" class="ghost inspector-close" data-action="close-inspector">Close selection</button>` : "";

  const toolbar = `<div class="inspector-bar" role="toolbar" aria-label="Inspect graph nodes and relationships">
    <div class="inspector-group"><span class="inspector-group-label">Nodes:</span>${nodeButtons}</div>
    ${edges.length ? `<div class="inspector-group"><span class="inspector-group-label">Relationships:</span>${edgeButtons}</div>` : ""}
    ${clearBtn}
  </div>`;

  if (!sel) {
    return `<div class="inspector" role="region" aria-label="Graph evidence inspector">
      ${toolbar}
      <p class="inspector-hint muted">Select any node or relationship in the map or toolbar above (mouse click or Tab + Enter/Space) to inspect its FalkorDB graph records, hop depth, exact relationship chain, centrality, SLAs, incidents and reviewers.</p>
    </div>`;
  }

  if (sel.type === "edge") {
    const edge = edges.find((e) => e.id === sel.id);
    if (!edge) {
      return `<div class="inspector" role="region" aria-label="Graph evidence inspector">${toolbar}
        <p class="muted">Selected relationship ${code(sel.id)} is not in the returned graph analysis; no edge is fabricated.</p></div>`;
    }
    if (edge.from === "__field") {
      const c = (a.consumers || []).find((x) => x.service === edge.to);
      const aff = (a.affected_services || []).find((x) => x.service === edge.to);
      return `<div class="inspector" role="region" aria-label="Graph evidence inspector">${toolbar}
        <div class="inspector-card" data-inspected-edge="${esc(edge.id)}">
          <h3>Relationship: ${code("CONSUMES_FIELD")} (Direct dependency)</h3>
          <dl>
            <dt>Relation type</dt><dd>${code("CONSUMES_FIELD")} (anchored via ${code(`(:Service {name: "${scenario.producer}"})-[:EXPOSES]->(:Contract {contract_id: "${scenario.contract_id}"})-[:HAS_FIELD]->(:SchemaField {name: "${scenario.field}"})`)}</dd>
            <dt>Graph endpoints</dt><dd>${code(`(Service:${edge.to})-[:CONSUMES_FIELD]->(SchemaField:${scenario.contract_id}.${scenario.field})`)}</dd>
            <dt>Impact propagation</dt><dd>${code(`${scenario.contract_id}.${scenario.field}`)} → ${code(edge.to)} (${aff ? `vulnerable consumer at hop depth ${aff.depth}` : "mitigated at consumer by fallback"})</dd>
            <dt>Fallback enabled</dt><dd>${code(String(c?.fallback_enabled))}</dd>
            <dt>Evidence source</dt><dd>${esc(c?.evidence_source || "unverified")}</dd>
          </dl>
        </div>
      </div>`;
    }
    const fromAff = (a.affected_services || []).find((x) => x.service === edge.from);
    const toAff = (a.affected_services || []).find((x) => x.service === edge.to);
    return `<div class="inspector" role="region" aria-label="Graph evidence inspector">${toolbar}
      <div class="inspector-card" data-inspected-edge="${esc(edge.id)}">
        <h3>Relationship: ${code("DEPENDS_ON")} (Downstream dependency)</h3>
        <dl>
          <dt>Relation type</dt><dd>${code("DEPENDS_ON")} (service-to-service dependency in FalkorDB)</dd>
          <dt>Graph endpoints</dt><dd>${code(`(Service:${edge.to})-[:DEPENDS_ON]->(Service:${edge.from})`)} (${code(edge.to)} depends on ${code(edge.from)})</dd>
          <dt>Impact propagation</dt><dd>${code(edge.from)} (depth ${esc(fromAff?.depth ?? 1)}) → ${code(edge.to)} (depth ${esc(toAff?.depth ?? "?")})</dd>
          <dt>Verified path from producer</dt><dd>${toAff ? toAff.path.map(code).join(" → ") : code(`${edge.from} -> ${edge.to}`)}</dd>
        </dl>
      </div>
    </div>`;
  }

  // Node inspection
  if (sel.id === "__field") {
    const unknowns = renderUnknownsForInspector(a, inv);
    const consumerLinks = (a.consumers || []).map((c) =>
      `<li>${code(`(Service:${c.service})-[:CONSUMES_FIELD {fallback_enabled: ${c.fallback_enabled}}]->(SchemaField:${scenario.contract_id}.${scenario.field})`)} <span class="muted">(${esc(c.evidence_source || "unverified")})</span></li>`
    ).join("");
    const incHtml = a.incidents === null
      ? `<span class="muted">Not checked: no service is affected.</span>`
      : a.incidents.length
        ? a.incidents.map((i) => `${code(`(Incident:${i.incident_id})-[:CAUSED_BY_CHANGE_IN]->(Contract:${scenario.contract_id})`)} (${esc(i.severity)}: ${esc(i.summary)})`).join("; ")
        : `<span class="muted">Checked, none found on this contract.</span>`;
    return `<div class="inspector" role="region" aria-label="Graph evidence inspector">${toolbar}
      <div class="inspector-card" data-inspected-node="__field">
        <h3>Node: ${code(`${scenario.contract_id}.${scenario.field}`)} <span class="muted">(Entity type: SchemaField)</span></h3>
        <dl>
          <dt>Entity ID and type</dt><dd>${code(`SchemaField:${scenario.contract_id}.${scenario.field}`)} on ${code(`Contract:${scenario.contract_id}`)}, exposed by ${code(`Service:${scenario.producer}`)}</dd>
          <dt>Producer relationship</dt><dd>${code(`(Service:${scenario.producer})-[:EXPOSES]->(Contract:${scenario.contract_id})-[:HAS_FIELD]->(SchemaField:${scenario.contract_id}.${scenario.field})`)}</dd>
          <dt>Hop depth</dt><dd>0 (origin of the contract field change; direct consumers sit at depth 1)</dd>
          <dt>Consumer mapping status</dt><dd>${code(a.mapping_status || "unknown")} — ${esc(a.coverage_note || "")}</dd>
          <dt>Direct consumers (${esc((a.consumers || []).length)})</dt><dd>${consumerLinks ? `<ul>${consumerLinks}</ul>` : `<span class="muted">None indexed.</span>`}</dd>
          <dt>Downstream affected services (${esc((a.affected_services || []).length)})</dt><dd>${(a.affected_services || []).length ? a.affected_services.map((s) => `${code(s.service)} (depth ${esc(s.depth)})`).join(", ") : `<span class="muted">None affected.</span>`}</dd>
          <dt>Contract incidents (${code("CAUSED_BY_CHANGE_IN")})</dt><dd>${incHtml}</dd>
        </dl>
        ${unknowns.length ? `<h4>Unknown, unchecked or failed evidence</h4><ul class="inspector-unknowns">${unknowns.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}
      </div>
    </div>`;
  }

  const consumer = (a.consumers || []).find((c) => c.service === sel.id);
  const aff = (a.affected_services || []).find((s) => s.service === sel.id);
  if (!consumer && !aff) {
    return `<div class="inspector" role="region" aria-label="Graph evidence inspector">${toolbar}
      <p class="muted">Selected node ${code(sel.id)} is not in the returned graph analysis; no node or path is fabricated.</p></div>`;
  }

  const tier = aff?.tier ?? consumer?.tier ?? "unknown";
  const cent = formatCentrality(a.centrality, sel.id);
  const nodeSlas = (a.slas || []).filter((s) => s.service === sel.id);
  const downstream = (a.affected_services || []).filter((s) => s.path.length >= 2 && s.path[s.path.length - 2] === sel.id);
  const rootConsumerName = aff ? aff.path[1] : consumer?.service;
  const rootConsumer = (a.consumers || []).find((c) => c.service === rootConsumerName);

  // Build exact step-by-step relationship chain from the returned path
  const pathSteps = [];
  if (aff) {
    pathSteps.push(
      `<li><strong>Direct dependency (hop 1, ${code("CONSUMES_FIELD")}):</strong> ` +
      `${code(`(Service:${aff.path[1]})-[:CONSUMES_FIELD {fallback_enabled: false}]->(SchemaField:${scenario.contract_id}.${scenario.field})<-[:HAS_FIELD]-(Contract:${scenario.contract_id})<-[:EXPOSES]-(Service:${aff.path[0]})`)} ` +
      `<span class="muted">(${esc(rootConsumer?.evidence_source || "manifest")})</span></li>`
    );
    for (let i = 1; i < aff.path.length - 1; i += 1) {
      const upstream = aff.path[i];
      const dep = aff.path[i + 1];
      pathSteps.push(
        `<li><strong>Downstream dependency (hop ${i + 1}, ${code("DEPENDS_ON")}):</strong> ` +
        `${code(`(Service:${dep})-[:DEPENDS_ON]->(Service:${upstream})`)} ` +
        `<span class="muted">(${esc(dep)} depends on ${esc(upstream)}; impact flows ${esc(upstream)} → ${esc(dep)})</span></li>`
      );
    }
  } else if (consumer) {
    pathSteps.push(
      `<li><strong>Direct dependency (${code("CONSUMES_FIELD")}, mitigated by fallback):</strong> ` +
      `${code(`(Service:${consumer.service})-[:CONSUMES_FIELD {fallback_enabled: ${consumer.fallback_enabled}}]->(SchemaField:${scenario.contract_id}.${scenario.field})`)} ` +
      `<span class="muted">(${esc(consumer.evidence_source || "manifest")})</span></li>`
    );
  }

  const slaHtml = nodeSlas.length
    ? nodeSlas.map((s) => `${code(`(Service:${sel.id})-[:BACKS_SLA]->(SLAContract:${s.sla_id})`)} (${esc(s.name)}, $${esc(s.penalty_per_hour_usd)}/h synthetic; SLA link is not counted as a hop)`).join("; ")
    : `<span class="muted">Checked, no SLA attached to ${esc(sel.id)}.</span>`;

  const incHtml = a.incidents === null
    ? `<span class="muted">Not checked (${esc(inv?.incidents?.source || "no service affected")}).</span>`
    : a.incidents.length
      ? a.incidents.map((i) => `${code(`(Incident:${i.incident_id})-[:CAUSED_BY_CHANGE_IN]->(Contract:${scenario.contract_id})`)} (${esc(i.severity)}: ${esc(i.summary)}; resolved by ${i.resolved_by.map(code).join(", ")})`).join("; ")
      : `<span class="muted">Checked, none found on contract ${esc(scenario.contract_id)}.</span>`;

  let revHtml;
  if (a.reviewers === null) {
    revHtml = `<span class="muted">Not checked (${esc(inv?.reviewers?.source || "no service affected")}).</span>`;
  } else {
    const matchingRev = a.reviewers.filter((r) => r.authored_services.includes(sel.id) || r.resolved_incidents.length > 0);
    revHtml = matchingRev.length
      ? matchingRev.map((r) => {
          const edgesDesc = [
            ...(r.authored_services.includes(sel.id) ? [code(`(Engineer:${r.handle})-[:AUTHORED_COMMITS]->(Service:${sel.id})`)] : []),
            ...r.resolved_incidents.map((incId) => code(`(Engineer:${r.handle})-[:RESOLVED_INCIDENT]->(Incident:${incId})`)),
          ].join(", ");
          return `${code(r.handle)} (${esc(r.display_name)}): ${edgesDesc}`;
        }).join("; ")
      : `<span class="muted">Checked, no engineer directly linked to ${esc(sel.id)} or contract incidents.</span>`;
  }

  const unknowns = renderUnknownsForInspector(a, inv);

  return `<div class="inspector" role="region" aria-label="Graph evidence inspector">${toolbar}
    <div class="inspector-card" data-inspected-node="${esc(sel.id)}">
      <h3>Node: ${code(sel.id)} <span class="muted">(Entity type: Service, tier: ${esc(tier)})</span></h3>
      <dl>
        <dt>Entity ID and type</dt><dd>${code(`Service:${sel.id}`)} (tier: ${code(tier)})</dd>
        <dt>Hop depth</dt><dd>${aff ? `${esc(aff.depth)} service hop${aff.depth === 1 ? "" : "s"} from producer ${code(scenario.producer)} (direct consumer = 1; each DEPENDS_ON hop +1; SLA links are not hops)` : `Not affected (direct consumer has fallback_enabled=true)`}</dd>
        <dt>Verified graph path</dt><dd>${aff ? aff.path.map(code).join(" → ") : `${code(scenario.producer)} → ${code(sel.id)} (stopped by fallback)`}</dd>
        <dt>Relationship chain</dt><dd><ul class="inspector-chain">${pathSteps.join("")}</ul></dd>
        <dt>Connected downstream services</dt><dd>${downstream.length ? downstream.map((d) => `${code(d.service)} (depth ${esc(d.depth)})`).join(", ") : `<span class="muted">None reached downstream of ${esc(sel.id)} in returned paths.</span>`}</dd>
        <dt>Betweenness centrality</dt><dd>${esc(cent.text)} <span class="muted">${esc(CENTRALITY_LABEL)}</span></dd>
        <dt>Attached SLA evidence (${code("BACKS_SLA")})</dt><dd>${slaHtml}</dd>
        <dt>Contract incident evidence (${code("CAUSED_BY_CHANGE_IN")})</dt><dd>${incHtml}</dd>
        <dt>Responsible reviewer evidence</dt><dd>${revHtml}</dd>
      </dl>
      ${unknowns.length ? `<h4>Unknown, unchecked or failed evidence</h4><ul class="inspector-unknowns">${unknowns.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}
    </div>
  </div>`;
}

const EXPLANATION = {
  accepted: "The agent's reading of the evidence. It is interpretation, not verified evidence.",
  withheld_inconsistent: "The agent's explanation was withheld because it contradicted the graph evidence. The verdict and evidence are unaffected.",
  not_produced: "The agent finished without an explanation.",
};

export function agentPanel(r) {
  const mode = agentMode(r.agent_mode);
  const list = (xs) => `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
  const explanation = r.explanation_status === "accepted"
    ? `<blockquote class="explanation">${esc(r.model_explanation)}</blockquote>
       ${r.inferences.length ? `<h4>Inferences</h4>${list(r.inferences)}` : ""}
       ${r.recommendations.length ? `<h4>Recommendations</h4>${list(r.recommendations)}` : ""}`
    : "";
  const trace = r.tool_trace.map((t) => `<tr class="${t.executed ? "" : "rejected"}">
      <td>${esc(t.step)}</td><td>${code(t.tool)}</td><td>${code(JSON.stringify(t.arguments))}</td>
      <td>${t.executed ? esc(t.status) : `rejected: ${esc(t.rejected_reason)}`}</td>
      <td>${esc(t.summary || "")}</td><td>${t.executed ? `${esc(t.duration_ms)} ms` : ""}</td></tr>`).join("");
  return `<div class="agent">
    <p class="mode mode-${mode.kind}"><span class="mode-label">${esc(mode.label)}</span>
      <span class="muted">${esc(r.tool_calls)} tool call${r.tool_calls === 1 ? "" : "s"}, ${esc(Math.round(r.latency_ms / 100) / 10)} s, ended: ${esc(r.stop_reason)}</span></p>
    <p class="explanation-status status-${esc(r.explanation_status)}">${esc(EXPLANATION[r.explanation_status] || r.explanation_status)}</p>
    ${explanation}
    ${r.warnings.length ? `<div class="warnings" role="note"><h4>Warnings</h4>${list(r.warnings)}</div>` : ""}
    ${r.unknowns.length ? `<h4>Unknowns</h4>${list(r.unknowns)}` : ""}
    ${memoryPanel(r.memory)}
    <h4>Tool calls the agent made over MCP (${esc(r.tool_trace.length)})</h4>
    ${agentTimeline(r)}
    <h4>Verified by tool results</h4>
    ${r.verified_facts.length ? `<ul class="facts">${r.verified_facts.map((f) => `<li>${esc(f.claim)} <span class="muted">via ${code(f.source_tool)}</span></li>`).join("")}</ul>` : `<p class="muted">No tool returned facts.</p>`}
    <details><summary>Raw tool-call table</summary>
      <table class="trace"><thead><tr><th>#</th><th>Tool</th><th>Arguments</th><th>Result</th><th>Summary</th><th>Time</th></tr></thead><tbody>${trace}</tbody></table>
    </details>
  </div>`;
}

/** The agent's real MCP tool calls in order, with why each one ran. Built only from tool_trace. */
export function agentTimeline(r) {
  const live = agentMode(r.agent_mode).kind === "live";
  const suggestedBy = {};   // tool -> first earlier step whose result listed it in next_actions
  const rows = r.tool_trace.map((t) => {
    const by = suggestedBy[t.tool];
    const why = live ? (by ? `Chosen by the model; step ${by}'s result listed it in next_actions`
      : "Chosen by the model from the MCP tool list")
      : (by ? `Followed step ${by}'s next_actions${t.purpose.startsWith("Previous result pointed") ? "" : `. ${t.purpose}`}` : t.purpose);
    if (t.executed) for (const a of t.next_actions || []) suggestedBy[a] ??= t.step;
    const ev = t.evidence || [];
    const status = !t.executed ? ["rejected", "Rejected by the gate"] : t.status === "error" ? ["failed", "Failed"]
      : t.status === "warning" ? ["warn", "Returned with unknowns"] : ["ok", "Returned"];
    return `<li class="step step-${status[0]}">
      <span class="step-n" aria-hidden="true">${esc(t.step)}</span>
      <div class="step-body">
        <p class="step-head">${code(t.tool)} <span class="step-status">${esc(status[1])}</span>
          ${t.executed ? `<span class="muted">${esc(t.duration_ms)} ms</span>` : ""}</p>
        <p class="step-why"><span class="muted">Why:</span> ${esc(why)}</p>
        <p class="step-args"><span class="muted">Arguments:</span> ${code(JSON.stringify(t.arguments))}</p>
        ${t.executed ? `<p class="step-result">${esc(t.summary || "")}</p>` : `<p class="step-result">${esc(t.rejected_reason)}</p>`}
        ${ev.length ? `<p class="step-ev"><span class="muted">Graph evidence (${esc(ev.length)}):</span> ${ev.slice(0, 8).map(code).join(" ")}${ev.length > 8 ? ` <span class="muted">+${esc(ev.length - 8)} more</span>` : ""}</p>` : ""}
        ${t.unknowns?.length ? `<p class="step-unknown"><span class="muted">Unknown:</span> ${esc(t.unknowns.join(" "))}</p>` : ""}
        ${t.next_actions?.length ? `<p class="muted">Pointed next to: ${t.next_actions.map(code).join(" ")}</p>` : ""}
      </div></li>`;
  });
  return rows.length ? `<ol class="timeline">${rows.join("")}</ol>` : `<p class="muted">The agent made no tool calls.</p>`;
}

const MEMORY_STATUS = {
  recorded: "This investigation was saved",
  recall_failed: "Earlier investigations could not be read; this one was still investigated from the current graph",
  record_failed: "This investigation could not be saved; its result is unaffected",
};

/** Investigation memory: earlier runs on the SAME contract field, and what the current graph says about them now. */
const when = (iso) => String(iso ?? "").replace("T", " ").replace(/:\d\d(\+00:00|Z)$/, " UTC");

export function memoryPanel(m) {
  if (!m) return `<p class="muted memory-off">Memory is off. Set BLASTGATE_MEMORY_GRAPH to remember investigations across runs.</p>`;
  const now = (text) => /^(still|consumer in)/.test(text) ? "now-confirmed" : /not reused/.test(text) ? "now-superseded" : "now-changed";
  const runs = table(["When", "Change", "Verdict then", "Affected then", "Incidents then"], m.prior_investigations.map((h) =>
    `<tr class="historical"><td class="when">${esc(when(h.at))}</td><td>${code(h.pr_id)} <span class="muted">${esc(h.change)}</span></td>
     <td>${verdictChip(h.verdict)}</td><td>${h.affected_services.map(code).join(" ") || "—"}</td>
     <td>${h.incidents.map(code).join(" ") || "—"}</td></tr>`), "No earlier investigation of this contract field.");
  const reviews = m.code_reviews.length ? `<h5>Saved by the code-graph reviewer</h5>${table(["When", "Base", "Detected change", "Code dependents in"],
    m.code_reviews.map((c) => `<tr class="historical"><td class="when">${esc(when(c.at))}</td><td>${code(String(c.base).slice(0, 12))}</td>
      <td>${code(`${c.symbol}.${c.code_field}`)} ${esc(c.kind)} (${esc(c.old_type)} → ${esc(c.new_type)})</td>
      <td>${c.code_dependent_services.map(code).join(" ") || "—"}</td></tr>`), "")}` : "";
  const checks = m.revalidation.length ? `<h5>Checked against the current graph</h5>${table(["Remembered", "From", "Current graph says"],
    m.revalidation.map((v) => `<tr><td class="historical-cell">${esc(v.fact)}</td><td>${v.from.map(code).join(" ")}</td>
      <td class="${now(v.now)}">${esc(v.now)}</td></tr>`), "")}` : "";
  return `<section class="memory" aria-label="Investigation memory">
    <h4>Memory</h4>
    <p class="memory-status status-${esc(m.status)}">${esc(MEMORY_STATUS[m.status] || m.status)}${m.recorded_id ? ` as ${code(m.recorded_id)}` : ""}.
      <span class="muted">${esc(m.note)} ${esc(m.planner_use)}</span></p>
    <h5>Earlier investigations of this contract field</h5>${runs}${reviews}${checks}
  </section>`;
}

const STAGE_LABEL = { not_run: "Not run", running: "Running", completed: "Completed", failed: "Failed",
  not_applicable: "Not applicable" };

/** The investigation progression. Every status is derived from data this page received or a request in flight:
 * busy = request keys running now ("analyze" | "agent" | "simulate"); errors = keys whose last request failed. */
export function loopStrip({ scenario, analysis = null, agent = null, simulation = null, busy = [], errors = {} }) {
  if (!scenario) return "";
  const run = new Set(busy), cc = scenario.code_change;
  const verdict = agent?.verdict || analysis?.verdict;
  const sim = simulation?.counterfactual || agent?.simulation?.counterfactual;
  const target = `${scenario.contract_id}.${scenario.field}`;
  const m = agent?.memory;
  const recalled = m ? m.prior_investigations.length + m.code_reviews.length : 0;
  const stage = (status, detail) => ({ status, detail });
  const stages = [
    ["Code change", cc ? stage("completed", `${cc.symbol}.${cc.field}: ${cc.old_type} → ${cc.new_type} (git diff)`)
      : stage("not_applicable", "Declared by the PR (synthetic scenario), no git diff")],
    ["Code graph", cc ? stage("completed", `${cc.code_dependents} dependent function(s), ${cc.code_routes} route(s), indexed by Gristle`)
      : stage("not_applicable", "The code graph is not used for declared scenarios")],
    ["Mapping", cc ? stage(cc.mapping === "validated" ? "completed" : "failed", `${cc.symbol} → ${target} (${cc.mapping})`)
      : stage("not_applicable", `Declared directly as ${scenario.producer} → ${target}`)],
    ["Memory", run.has("agent") ? stage("running", "Reading earlier investigations of this field")
      : errors.agent ? stage("failed", "The investigation did not complete")
      : !agent ? stage("not_run", "Read when the agent runs")
      : !m ? stage("not_applicable", "Memory is off on this server")
      : m.status !== "recorded" ? stage("failed", `${m.status.replace("_", " ")}; the verdict is unaffected`)
      : recalled ? stage("completed", `Recalled ${m.prior_investigations.length} earlier investigation(s) and ${m.code_reviews.length} code review(s); this run saved as ${m.recorded_id}`)
      : stage("completed", `No earlier investigation of this field; this run saved as ${m.recorded_id}`)],
    ["Service graph", analysis ? stage("completed", `${analysis.affected_services.length} affected service(s), ${analysis.slas.length} SLA(s)`)
      : run.has("analyze") || run.has("agent") ? stage("running", "Traversing FalkorDB from the changed field")
      : errors.analyze ? stage("failed", "The graph analysis request failed") : stage("not_run", "Not analyzed yet")],
    ["MCP tools", run.has("agent") ? stage("running", "The agent is calling tools over MCP")
      : agent ? stage(agent.stop_reason === "completed" ? "completed" : "failed",
        `${agent.tool_calls} call(s), ${agentMode(agent.agent_mode).label}, ended: ${agent.stop_reason}`)
      : errors.agent ? stage("failed", "The agent request failed") : stage("not_run", "Agent not run")],
    ["Decision", verdict ? stage("completed", `${verdict} from the deterministic engine`)
      : run.has("analyze") || run.has("agent") ? stage("running", "Waiting for the graph evidence") : stage("not_run", "No verdict yet")],
    ["Counterfactual", run.has("simulate") ? stage("running", "Running on a temporary graph copy")
      : sim ? stage("completed", `${sim.production_verdict} → ${sim.graph_verdict_under_assumption} on a temporary copy (assumed, unverified)`)
      : errors.simulate ? stage("failed", "The simulation request failed") : stage("not_run", "Not simulated")],
  ];
  return `<ol class="loop" aria-label="Investigation progress">${stages.map(([name, st], i) => {
    const tone = name === "Decision" && verdict ? ` tone-${verdict.toLowerCase()}` : "";
    return `<li class="stage stage-${st.status}${tone}"><span class="loop-n" aria-hidden="true">${i + 1}</span>
      <span class="loop-name">${esc(name)}</span><span class="stage-status">${esc(STAGE_LABEL[st.status])}</span>
      <span class="loop-detail">${esc(st.detail)}</span></li>`;
  }).join("")}</ol>`;
}

/** Empty state before any change is opened. Leads with the real code change when this server registered one. */
export function startPanel(health, flagship = []) {
  if (!health) return `<p class="empty">Connecting to the BlastGate server…</p>`;
  if (!(health.features || []).includes("codegraph_report")) {
    return `<div class="start start-problem"><h2>This server is an older BlastGate process</h2>
      <p>It serves the current console but not the code-graph API, so the real code change can't be shown here.
        Start the current server on a free port with <code>scripts/start_demo.sh</code> and open the URL it prints.
        The declared scenarios on the left still work.</p></div>`;
  }
  if (!flagship.length) {
    return `<div class="start start-problem"><h2>No code change is registered on this server</h2>
      <p>${health.codegraph?.report_available
        ? "A code-graph review exists, but this server was not started with it, so the agent can't investigate it. Restart with <code>scripts/start_demo.sh</code>."
        : "Generate one with <code>scripts/codegraph_demo.py</code>, then start <code>scripts/start_demo.sh</code>."}
        The declared scenarios on the left still work.</p></div>`;
  }
  const s = flagship[0], cc = s.code_change;
  return `<div class="start"><h2>Start with ${esc(s.pr_id)}: a real code change</h2>
    <p class="start-lead">${code(`${cc.symbol}.${cc.field}`)} changed from ${code(cc.old_type)} to ${code(cc.new_type)} in a git diff.
      Gristle's code graph linked it to ${esc(cc.code_dependents)} function(s) and ${esc(cc.code_routes)} route(s), and a validated
      mapping connects it to ${code(`${s.contract_id}.${s.field}`)} in the FalkorDB service graph.</p>
    <ol class="start-steps">
      <li><strong>Analyze impact</strong>: follow the service graph from the changed field to consumers, downstream services, SLAs and incidents.</li>
      <li><strong>Investigate with agent</strong>: the agent calls the six MCP tools; the deterministic engine decides BLOCK, WARN or CLEAR.</li>
      <li><strong>Run simulation</strong>: try a mitigation on a temporary graph copy; the real verdict is never changed.</li>
    </ol>
    <p><button type="button" class="primary" data-open="${esc(s.pr_id)}">Open ${esc(s.pr_id)}</button></p></div>`;
}

/** Header line: what is open and what is happening to it right now. */
export function currentBar(scenario, { verdict = null, progress = "" } = {}) {
  if (!scenario) return "";
  return `<span class="current-pr">${esc(scenario.pr_id)}</span> <span class="current-title">${esc(scenario.title)}</span>
    ${verdict ? verdictChip(verdict) : ""}<span class="current-status">${esc(progress)}</span>`;
}

/** Where a code-graph-derived change came from: the real diff and the code entities Gristle connected to it. */
export function codeOrigin(report, cc) {
  const item = report?.contract_changes?.find((c) => c.symbol === cc.symbol && c.field === cc.field);
  if (!item) return `<p class="muted">The code-graph report for this change is not available.</p>`;
  return `<details class="origin"><summary>Derived from a git diff by the code graph: show the diff and
    ${esc(item.code_impact.dependents.length)} connected code entities</summary>
    <pre class="diff">${esc(report.diff)}</pre>
    ${table(["Code entity", "Link to the contract", "Confidence", "Service (mapping)"], item.code_impact.dependents.map((d) =>
      `<tr><td>${code(d.function)}</td><td>${esc(d.via)}</td><td>${esc(d.confidence)}</td>
       <td class="mapping-${esc(d.mapping)}">${d.service ? code(d.service) : "—"} ${esc(d.mapping)}</td></tr>`), "No code entity depends on this type.")}
    <p class="muted">A type-level link (ACCEPTS/RETURNS) proves the function handles ${code(cc.symbol)}, not that it reads ${code(cc.field)}.</p>
    ${item.uncertainties.length ? `<p><strong>Unresolved:</strong></p>${bullets(item.uncertainties, "")}` : ""}
    <p><button type="button" class="ghost" data-action="open-codegraph">Open the full code-graph review</button></p>
  </details>`;
}

export function simulationPanel(s, scenario = null) {
  const cf = s.counterfactual;
  const sc = scenario || {
    pr_id: s.pr_id,
    producer: s.before?.affected_services?.[0]?.path?.[0] || "producer",
    contract_id: s.changes?.contract_id || "contract",
    field: s.changes?.field || "field",
  };
  const hasTopology = Boolean(s.before && s.after && s.changes);
  const listOrNone = (xs) => (xs && xs.length ? xs.map(code).join(" ") : `<span class="muted">none</span>`);
  const fmtExposure = (exp) => exp ? `$${esc(exp.assumed_total_usd.toLocaleString("en-US"))} (${esc(exp.assumed_outage_hours)} h assumed)` : "$0 (no SLA at risk)";

  const comparisonHtml = hasTopology ? `
    <div class="sim-graphs" role="group" aria-label="Before and after simulation graph comparison">
      <div class="sim-graph-col" data-sim-stage="before">
        <h3>Before: Real graph (${verdictChip(s.before.verdict)})</h3>
        <p class="muted">Vulnerable consumers: ${esc(s.before.vulnerable_consumers.length)} · Affected services: ${esc(s.before.affected_services.length)} · SLAs at risk: ${esc(s.before.slas.length)} (${fmtExposure(s.before.exposure)})</p>
        <div class="map-wrap">${impactMap(s.before, sc)}</div>
      </div>
      <div class="sim-graph-col" data-sim-stage="after">
        <h3>After: Copied graph under counterfactual assumption (${verdictChip(s.after.verdict)})</h3>
        <p class="muted">Counterfactual assumption only (code-level correctness unverified). Vulnerable consumers: ${esc(s.after.vulnerable_consumers.length)} · Affected services: ${esc(s.after.affected_services.length)} · SLAs at risk: ${esc(s.after.slas.length)} (${fmtExposure(s.after.exposure)})</p>
        <div class="map-wrap">${impactMap(s.after, sc)}</div>
      </div>
    </div>
    <dl class="sim-diff">
      <dt>Changed relationship on copy</dt>
      <dd>${code(s.changes.relationship)}: <code>fallback_enabled</code> ${code(String(s.changes.fallback_before))} → ${code(String(s.changes.fallback_after))}</dd>
      <dt>Consumers no longer vulnerable</dt>
      <dd>${listOrNone(s.changes.removed_vulnerable_consumers)}</dd>
      <dt>Remaining vulnerable consumers</dt>
      <dd>${listOrNone(s.changes.remaining_vulnerable_consumers)}</dd>
      <dt>Services no longer affected</dt>
      <dd>${listOrNone(s.changes.removed_affected_services)}</dd>
      <dt>Remaining impacted services</dt>
      <dd>${listOrNone(s.changes.remaining_affected_services)}</dd>
      <dt>SLAs no longer at risk</dt>
      <dd>${listOrNone(s.changes.removed_slas)}</dd>
      <dt>Remaining SLAs at risk</dt>
      <dd>${listOrNone(s.changes.remaining_slas)}</dd>
      <dt>Assumed SLA exposure (before → after)</dt>
      <dd>${fmtExposure(s.before.exposure)} → ${fmtExposure(s.after.exposure)}</dd>
    </dl>` : "";

  return `<div class="simulation">
    <p class="sim-label">${esc(s.label)}</p>
    <div class="sim-compare">
      <p><span class="muted">Real graph</span> ${verdictChip(cf.production_verdict)}</p>
      <p><span class="muted">Copy with the assumed fix</span> ${verdictChip(cf.graph_verdict_under_assumption)}</p>
    </div>
    <p><strong>Code-level correctness:</strong> ${esc(s.code_level_correctness)}</p>
    <p>${esc(cf.production_recommendation)}</p>
    ${comparisonHtml}
    <dl>
      <dt>Your assumption</dt><dd>${esc(s.user_assumption)}</dd>
      <dt>What changed, on a temporary copy only</dt><dd>${esc(s.modelled_mutation)}</dd>
      <dt>Original graph unchanged</dt><dd>${s.original_unchanged ? "yes, verified" : "not verified"}</dd>
      <dt>Temporary copy deleted</dt><dd>${s.temp_graph_deleted ? "yes" : "no"}</dd>
    </dl>
  </div>`;
}

const bullets = (xs, empty) => xs?.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
  : `<p class="muted">${esc(empty)}</p>`;

/** GET /api/codegraph/report: a pre-generated git-diff review (Gristle code graph + service graph). */
export function codegraphPanel(r, prFor = () => null) {
  const list = (xs) => (xs?.length ? xs.map(code).join(" ") : `<span class="muted">none</span>`);
  const changes = r.contract_changes.map((c) => {
    const ci = c.code_impact, sg = c.service_graph, cf = c.counterfactual;
    const deps = table(["Function", "Link", "Confidence", "Service", "Mapping"], ci.dependents.map((d) =>
      `<tr><td>${code(d.function)}</td><td>${esc(d.via)}</td><td>${esc(d.confidence)}</td>
       <td>${d.service ? code(d.service) : "—"}</td><td class="mapping-${esc(d.mapping)}">${esc(d.mapping)}</td></tr>`),
      "No indexed code depends on this type.");
    const routes = table(["Route", "Handler", "Service"], ci.routes.map((x) =>
      `<tr><td>${esc(x.method)} ${code(x.path)}</td><td>${code(x.handler)}</td><td>${x.service ? code(x.service) : esc(x.mapping)}</td></tr>`),
      "No route handles the dependent code.");
    const service = sg ? `
      <h4>Service-graph evidence (FalkorDB)</h4>
      <p class="muted">Coverage: ${esc(sg.mapping_status)}. ${esc(sg.coverage_note)}</p>
      ${detailTables(sg)}
      <h4>Cross-check: code graph vs service graph</h4>
      <dl><dt>Corroborated by both</dt><dd>${list(c.cross_check.corroborated)}</dd>
        <dt>Code only (unverified)</dt><dd>${list(c.cross_check.code_only)}</dd>
        <dt>Service graph only</dt><dd>${list(c.cross_check.service_graph_only)}</dd></dl>`
      : `<p class="muted">Not mapped to a service-graph contract, so no service evidence was used.</p>`;
    const sim = !cf ? "" : cf.status !== "ok" ? `<p class="muted">${esc(cf.summary)}</p>` : `
      <h4>Counterfactual: unverified source-code mitigation</h4>
      <p class="sim-label">${esc(cf.label)}</p><p>${esc(cf.assumption)}</p>
      ${table(["", "Real graph", "Under the assumption"], [
        ["Verdict", verdictChip(cf.production_verdict), verdictChip(cf.graph_verdict_under_assumption)],
        [`${esc(cf.changes.consumer)} fallback`, code(String(cf.changes.fallback_before)), code(String(cf.changes.fallback_after))],
        ["Affected services", list([...cf.changes.removed_affected_services, ...cf.changes.remaining_affected_services]),
          list(cf.changes.remaining_affected_services)],
        ["SLAs", list([...cf.changes.removed_slas, ...cf.changes.remaining_slas]), list(cf.changes.remaining_slas)],
      ].map(([k, a, b]) => `<tr><th scope="row">${k}</th><td>${a}</td><td>${b}</td></tr>`), "")}
      <p>${esc(cf.production_recommendation)}</p>`;
    const pr = prFor(c);
    return `<section class="cg-change">
      <h3>${code(`${c.symbol}.${c.field}`)}: ${esc(c.kind)} (${esc(c.old_type ?? "—")} → ${esc(c.new_type ?? "—")})</h3>
      <p>${verdictChip(c.verdict)} Mapping <strong>${esc(c.mapping)}</strong>${c.breaking ? " · breaking" : ""}</p>
      ${pr ? `<p><button type="button" class="primary" data-investigate="${esc(pr)}">Investigate ${esc(pr)} with the MCP agent</button>
        <span class="muted">Runs the same six allowlisted tools and the deterministic verdict on this detected change.</span></p>`
        : c.mapping === "validated" ? `<p class="muted">Start the API with BLASTGATE_CODEGRAPH_REPORT set to hand this change to the agent.</p>` : ""}
      <h4>Source-code evidence (Gristle code graph)</h4>${deps}${routes}
      ${service}
      <h4>Recommended tests</h4>${bullets(c.recommended_tests, "None found.")}
      ${sim}
      <h4>Uncertainties</h4>${bullets(c.uncertainties, "None recorded.")}
    </section>`;
  }).join("");
  return `<div class="cg">${verdictBlock(r.verdict, r.reasons, `Deterministic rules on the service graph; code-graph evidence can only add caution. ${r.engine}.`)}
    <p class="muted">${esc(r.data_label)} · base ${code(String(r.base).slice(0, 12))} → ${esc(r.head)}</p>
    <h2 class="section">Actual git change</h2>
    <pre class="diff">${esc(r.diff || "(empty diff)")}</pre>
    <p>Changed symbols: ${list(r.changed_symbols.map((s) => `${s.symbol} (${s.kind})`))}</p>
    ${changes || `<p class="muted">No contract-field change in this diff.</p>`}
    <h2 class="section">General uncertainties</h2>${bullets(r.general_uncertainties, "None recorded.")}
    <h2 class="section">Known gaps</h2>${bullets(r.known_gaps, "")}
    <h2 class="section">Investigation trace</h2>
    ${table(["Graph", "Query", "Rows"], r.investigation_trace.map((t) =>
      `<tr><td>${esc(t.graph)}</td><td>${esc(t.query)}</td><td>${esc(t.rows)}</td></tr>`), "No queries recorded.")}</div>`;
}
