# BlastGate

Evidence-first pre-merge change-impact analysis on **FalkorDB**.
GraphHacks Track 01: *Agents That Act on Connected Data*.

> **BlastGate doesn't just display a graph. It uses connected graph data to investigate a change, guide its next steps, and explain the decision it makes.**

Imagine a developer changes `PriceQuote.discount_tier` from a string to an object (`dict`). That looks like one small code change, but the consequences can cross multiple services.

1. **Detect & Code Graph:** BlastGate analyzes the real Git change with Gristle, constructing an AST-level code graph in FalkorDB.
2. **Validated Contract Mapping:** Connects the code change to the service graph through contract manifests (`blastgate.map.json`) from the base commit without string guessing.
3. **Multi-Hop Agent Investigation:** The agent investigates the connected systems through FalkorDB-backed MCP tools. It discovers that `checkout-api` lacks a fallback, traces downstream dependencies, and retrieves relevant synthetic SLA and incident evidence.
4. **Deterministic Verdict:** Based on that evidence, BlastGate's deterministic policy returns **BLOCK**. The interface shows the affected services, reasons, evidence, recommended tests, and reviewers.
5. **Counterfactual Simulation:** Finally, we run a counterfactual simulation. Adding an assumed fallback changes the outcome to **CLEAR** on a temporary graph copy (`GRAPH.COPY`), but the original decision remains BLOCK. The simulation is explicitly marked unverified until contract tests pass.

- **Milestone 1:** The graph model, a deterministic verdict engine, and counterfactual simulation on an isolated `GRAPH.COPY`.
- **Milestone 2:** A FastAPI backend, typed MCP tool server (stdio), evidence-driven agent with GroqCloud inference option, and the obsidian-themed engineering console with interactive SVG graph evidence & counterfactual explorer.

> **All data is synthetic.** Services, engineers (`eng-alpha`…), SLAs, penalty rates and incidents are fabricated for this demo. They describe no real company, person, customer or measured loss. **Groq powers live LLM inference; synthetic data powers the demo.** Nothing here is production-validated.

## Track 01 at a glance

**The loop:** real git diff → Gristle **code graph** (FalkorDB) → validated mapping → **service graph** (FalkorDB) →
agent picks among **6 allowlisted MCP tools** → deterministic **BLOCK / WARN / CLEAR** → recommended tests and
reviewers → **counterfactual** on a temporary `GRAPH.COPY`.

| Track 01 asks for | Where it happens | Where a judge sees it |
| --- | --- | --- |
| Multi-hop reasoning | Code graph: `ACCEPTS`/`RETURNS` → `CALLS*1..4` → `Route-[:HANDLES]` → `TESTS_FUNCTION`. Service graph: `CONSUMES_FIELD` → `DEPENDS_ON*1..8` → `BACKS_SLA`, plus `CAUSED_BY_CHANGE_IN` → `RESOLVED_INCIDENT` / `AUTHORED_COMMITS` | Impact map (`pricing-engine → checkout-api → order-orchestrator → invoice-worker`); loop strip stages 1–4 |
| Graph queries and algorithms as tools | `blastgate/queries.py` (coverage, consumers, downstream, SLAs, `algo.betweenness`, incidents, reviewers), `blastgate/codegraph.py` `CG_*` queries, `GRAPH.COPY` | Agent timeline (graph evidence IDs per call); the review's investigation trace |
| Tool selection through MCP | `blastgate/mcp_server.py` (6 typed, read-only tools over stdio). The live planner (`GroqPlanner`) lets the model choose each call. The offline planner (`DeterministicPlanner`, labelled "rule-based, no LLM") follows each tool result's `next_actions` | Agent timeline: tool, arguments, **why** it ran, result, evidence; the planner mode is shown on every run |
| Actions driven by graph evidence | `verdict.decide()` (deterministic and authoritative), recommended tests and reviewers, the written review report, the counterfactual | Decision stage, reasons, recommendations, `codegraph_demo_out/report.md` |
| Explainable results | Evidence inventory (`found` / `none_found` / `unknown` / `not_checked` / `check_failed`), `verified_facts` built from tool payloads only, the explanation gate, uncertainties and known gaps | Evidence panel, "Unresolved" lists, counterfactual labelled **assumed, unverified** |
| Remember across runs / share state | Opt-in memory graph (`blastgate/memory.py`): the code-graph reviewer and the investigation agent are separate processes that both write to it and read from it, keyed by exact ids | Memory panel: earlier investigations, code reviews, and each remembered fact checked against the current graph |

**Which planner chose the tools.** Every run verified in this repo used the offline deterministic planner: the
tool order there follows the graph's `next_actions`, and no LLM chose it. The live-model path is covered by tests
with scripted Groq responses (`tests/agent/test_groq_planner.py`). A real Groq run was **not** part of this
verification.

## Judge demo (about 5 minutes, no LLM key needed)

```bash
docker run -d --name blastgate-falkordb -p 6379:6379 falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python blastgate_milestone1.py                 # seed the synthetic service graph "blastgate_m1"
scripts/setup_gristle.sh && export BLASTGATE_GRISTLE_BIN="$PWD/.gristle-venv/bin/gristle"
export BLASTGATE_MEMORY_GRAPH=blastgate_memory           # shared, persistent investigation memory
.venv/bin/python scripts/codegraph_demo.py               # real git diff -> code graph -> BLOCK (exit 2)
scripts/start_demo.sh                                    # open http://127.0.0.1:8766/ (PORT=... to change)
```

`scripts/start_demo.sh` serves the console and the API from **one origin** with the code-graph change (PR-900),
memory, and the deterministic planner enabled. It refuses a port that is already in use and never stops another
process. Open the URL it prints.

- **Stale server.** An older BlastGate process still serves the new console files, because they are read from disk on
  every request. The console checks `/health.features` and says *"older server"* instead of failing.
- **Stopped server.** A tab whose server has stopped shows *"No response from the server … (origin)"* along with
  the start command.

To record from a clean memory, drop the memory graph and regenerate the review before starting the server:

```bash
.venv/bin/python -c "from blastgate.graph import connect; connect().select_graph('blastgate_memory').delete()"
.venv/bin/python scripts/codegraph_demo.py && scripts/start_demo.sh
```

These exact steps were run from a clean copy (fresh venvs, no `.env`, Python 3.12, memory on). Results:
- seed exit 0;
- demo exit 2 (BLOCK);
- the first PR-900 agent run returned BLOCK and recalled the reviewer's code review;
- the second run recalled the first and returned BLOCK again;
- `pytest` gave 226 passed / 1 skipped (live Groq), exit 0.

1. **Change.** The start panel leads with PR-900; *Code-graph review* in the rail opens the full report. Shows the real diff (`discount_tier: str → dict`) and the
   changed symbol `contracts/pricing.py::PriceQuote`.
2. **Investigate.** Gristle links 6 functions and 2 routes to `PriceQuote`. These links are type-level and do
   not prove a field read. `reporting/` is unmapped, so it stays **unknown**.
3. **Follow graph paths.** *Investigate PR-900 with the MCP agent* → *Analyze impact*. The validated mapping
   reaches `checkout-api`, which has no fallback, and then 4 downstream services and 3 SLAs.
4. **Select tools.** *Investigate with agent*. The timeline shows each MCP call, its arguments and why it ran.
5. **Decide.** BLOCK comes from the deterministic engine; the planner can't change it.
6. **Prove.** Evidence inventory, verified facts, INC-2041, reviewers `eng-alpha` and `eng-bravo`, and the
   recommended tests.
7. **Simulate.** *Run simulation*. BLOCK → CLEAR on a temporary copy, labelled assumed and unverified. The real
   verdict stays BLOCK and the original graph is unchanged.
8. **Remember.** With `BLASTGATE_MEMORY_GRAPH` set, the first agent run already recalls the code review that the
   reviewer process saved. *Investigate with agent* again: the Memory panel lists the earlier run and checks each
   remembered fact against the current graph, and the verdict is recomputed without it.

## Architecture

```text
git diff ──► blastgate.codegraph ──► Gristle (separate venv) ──► FalkorDB code graphs (base, head; temporary)
               │ CG_* read-only Cypher · blastgate.map.json from the base commit, validated against both graphs
               └─► review report (codegraph_demo_out/report.json) ──► opt-in: registered as PR-900 for the agent

HTTP client ──► FastAPI (blastgate/api.py)
                 ├─ POST /api/analyze ─────────────► analysis.analyze() ─► verdict.decide()      (no LLM)
                 ├─ POST /api/simulate ────────────► tools.simulate_mitigation ─► GRAPH.COPY      (no LLM)
                 └─ POST /api/agent/investigate ──► agent.investigate()
                        planner (Groq LLM, or the labelled deterministic demo)
                           │ proposes tool calls          ▲ tool results
                           ▼                              │
                        orchestrator: allowlist · JSON-schema validation · PR pinning · no duplicates
                                      · ≤ 6 calls · simulation only if the user asked
                           │  MCP JSON-RPC over stdio (subprocess: python -m blastgate.mcp_server)
                           ▼
                        mcp_server.py ─► tools.py ─► analysis.py steps ─► parameterised ro_query ─► FalkorDB
                        after the loop: verdict = analysis.analyze() (deterministic, authoritative)
```

- **Graph facts come only from FalkorDB.** Code builds `verified_facts` from tool payloads, and `summary` from
  the verdict and an evidence `inventory`. Model text never becomes the summary.
- **Model text is kept only if it matches the evidence.** It goes to `model_explanation`, `inferences` and
  `recommendations`. All three are withheld, with a warning naming the reason, if the text claims something is
  absent that wasn't verified empty, or names an incident, SLA, engineer or service that no tool returned (see
  *Evidence inventory and explanation gate*).
- **The LLM can't change the verdict.** If its `recommended_verdict` differs from the engine's, the response
  shows the disagreement in `warnings`.
- **Each layer does one job.** The tools reuse `analysis.py`'s steps (`coverage`, `consumers`, `blast_radius`,
  `centrality`, `incidents`, `reviewers`, `exposure`); the MCP server only wraps `tools.py`; the agent never
  imports the graph functions.

## Setup

Requires Python 3.10+ and Docker.

```bash
docker run -d --name blastgate-falkordb -p 6379:6379 falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a   # graph module 4.22.1, the image these tests ran on
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # git-ignored; edit locally, never commit
set -a; source .env; set +a   # load it into the shell
python blastgate_milestone1.py   # seeds the synthetic graph "blastgate_m1" and prints the three verdicts
```

### Configure the Groq LLM (optional, needed only for the live agent)

1. Create a free account at <https://console.groq.com>, open **API Keys**, and create a key.
2. In your local `.env` (never in a tracked file): `AGENT_PROVIDER=groq`, `GROQ_API_KEY=<your key>`, and
   optionally `AGENT_MODEL=openai/gpt-oss-20b` (the default). To use another model, change `AGENT_MODEL`; it must
   be a tool-use-capable model available to your account (see <https://console.groq.com/docs/models>).
3. Reload: `set -a; source .env; set +a`.

`AGENT_PROVIDER=deterministic` runs a **rule-based demo planner (no LLM, no key)**. Every response states which planner ran in `agent_mode`: `llm: groq/<model>` for live Groq, `deterministic-demo (rule-based planner, no LLM)…` for the demo, and `test: …` for test-only planners. A Groq failure returns an error status, never a demo-planner report. If `AGENT_PROVIDER` is unset, or set to `groq` with no key, `/api/agent/investigate`
returns `503 agent_provider_not_configured`. No other endpoint needs a key.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FALKORDB_HOST` / `FALKORDB_PORT` / `FALKORDB_PASSWORD` | `localhost` / `6379` / – | database |
| `BLASTGATE_GRAPH` | `blastgate_m1` | graph used by the API and MCP server (never chosen by the model) |
| `AGENT_PROVIDER` | unset | `groq` or `deterministic` |
| `AGENT_MODEL` | `openai/gpt-oss-20b` | Groq model ID |
| `GROQ_API_KEY` | – | secret; read by the official `groq` SDK |
| `AGENT_TIMEOUT_S` / `MCP_TIMEOUT_S` | `30` / `30` | per-request timeouts (seconds) |
| `AGENT_MAX_TOOL_CALLS` | `6` | clamped to the hard cap of 6 |

## Run the backend

```bash
uvicorn blastgate.api:app --port 8000      # OpenAPI docs: http://localhost:8000/docs
```

| Route | Purpose |
| --- | --- |
| `GET /health` | always 200 (liveness); reports `falkordb: up/down` and whether a provider is configured. Never returns secrets |
| `GET /ready` | 200 only when FalkorDB is reachable, else 503 |
| `GET /api/scenarios` | synthetic PRs: id, title, contract, field, change, new type, breaking flag, synthetic label |
| `POST /api/analyze` | deterministic Milestone 1 analysis (no LLM) |
| `POST /api/simulate` | counterfactual on a temporary graph copy; `code_level_correctness: UNVERIFIED` |
| `POST /api/agent/investigate` | evidence-driven agent over MCP; returns code-rendered `summary` and `inventory`, `verified_facts`, gated `model_explanation`, assumptions, unknowns, tool trace |

```bash
curl -s localhost:8000/api/analyze -H 'content-type: application/json' -d '{"pr_id":"PR-104"}'
curl -s localhost:8000/api/simulate -H 'content-type: application/json' \
  -d '{"pr_id":"PR-104","consumer":"checkout-api","assumption":"checkout-api adds a tolerant discount_tier parser"}'
curl -s localhost:8000/api/agent/investigate -H 'content-type: application/json' \
  -d '{"pr_id":"PR-104","question":"Safe to merge?","simulate":{"consumer":"checkout-api","assumption":"checkout-api adds a tolerant discount_tier parser"}}'
```

**Errors.** Errors use the shape `{"error": code, "message": safe text}`. A malformed body gets a 422. An unknown
PR gets `404 unknown_scenario`. If FalkorDB is down you get `503 graph_unavailable`. An invalid simulation target
gets a 400. Provider problems map to statuses as follows; the `trace` field carries the partial tool trace:
`provider_rate_limited` gives 429 with a `Retry-After` header, `provider_timeout` gives 504, and
`provider_auth`, `provider_model_unavailable`, `provider_bad_request` and `provider_network` give 502. If the MCP
server can't start you get `503 tool_server_unavailable`. A failed agent run is never returned as a completed
investigation.

## Console (frontend)

Open <http://localhost:8000> after starting the backend. The FastAPI app serves the console from `web/` on the
same origin, so there's no build step, no CORS, and no keys in the browser. It answers one question: *what
could break if I merge this change?*

The **agent loop** strip across the top has seven stages: code change → code graph → mapping → service graph →
MCP tools → decision → counterfactual. Each stage shows only what this page actually received. A stage that
hasn't run says so, and a declared (non-code-graph) scenario marks the code stages as not applicable. The
**agent timeline** lists every real MCP call in order: the tool, its arguments, why it ran, the result, the graph
evidence IDs, and any gate rejection.

1. **Pick a change.** The list comes from `GET /api/scenarios`.
2. **Analyze impact** (`POST /api/analyze`, no LLM) shows the deterministic verdict and the engine's reasons,
   the code-rendered `summary`, an **impact map**, the **evidence inventory** with its five statuses, and detail
   tables for consumers, paths, synthetic SLAs, incidents and reviewers.
3. **Investigate with agent** (`POST /api/agent/investigate`) adds a separate panel:
   - **Planner badge:** live model, demo planner or test planner, read from `agent_mode`.
   - **Model explanation:** shown only when `explanation_status` is `accepted`, and labelled as interpretation.
     A withheld explanation is never rendered; the reason is.
   - **Also shown:** the verified facts and the tool calls.

   If the agent fails, the panel says so, and any deterministic result above stays labelled "From deterministic
   analysis". A demo result is never substituted.
4. **Counterfactual simulation** (`POST /api/simulate`) shows the backend's exact label, the real-graph verdict
   next to the copy's, side-by-side before-and-after impact maps, the explicit relationship and exposure diff, and
   `UNVERIFIED` for code-level correctness.

**The impact map & graph evidence explorer** draws only what the API returned:
- the changed field;
- each consumer's `CONSUMES_FIELD` edge (dashed if it has a fallback);
- each affected service's path, by hop depth;
- sampled `algo.betweenness` (`BT`) and `SLA ×n` badges;
- interactive node and relationship selection (mouse or keyboard) opening the graph evidence inspector.

Arrows show impact flow, which runs opposite to `DEPENDS_ON`. The map's table view is the detail tables.

**Status colours:** `#E5534B` block, `#D4A72C` warn and `#3FB68B` clear are used only for verdict status. Each
always comes with a glyph and a word, and each passes 3:1 contrast and colour-blind separation on the
`#1A2230` surface (checked with a palette validator).

**Frontend checks:**
- `node --test web/tests/*.test.mjs` renders **real recorded backend responses** through the view functions.
- `python web/tests/capture_fixtures.py` re-records those responses from the live app. Two of them, a withheld
  explanation and a 429, come from scripted planners, and their file names say so.
- `tests/api/test_web.py` checks the static serving, the CSP, and that the fixtures still match the response
  models.

**Contract change:** `/api/analyze` now also returns `summary` and `inventory`, built by the same code as the
agent report. Before this, a user without an LLM provider couldn't tell "not checked" from "none found".

## Deployment safety (live provider)

`POST /api/agent/investigate` is the **only** route that can trigger a paid or quota-limited provider call, and
only when `AGENT_PROVIDER=groq`. Every other route is deterministic, and so is the agent in `deterministic` mode;
none of them call a provider. In live mode the server enforces two protections itself; neither depends on the
frontend.

- **Access.** If `AGENT_LIVE_TOKEN` is set, each live investigation must send `Authorization: Bearer <token>`,
  checked with a constant-time compare (the console shows a token field). If it isn't set, live investigations
  are accepted only from loopback clients with no `X-Forwarded-For` or `Forwarded` header. That covers local
  judging; a proxied or remote caller gets `403 live_access_local_only`.
- **Rate limits.** Defaults are 2 per client per minute, 3 per minute overall and 60 per day overall
  (`AGENT_LIVE_MAX_*`), with 429 and `Retry-After`. They're checked only after access passes, so refused callers
  can't use up the budget. The counters live in process memory: with several workers or replicas the limits
  multiply, so use one process or move the counters to a shared store.

Other notes:
- `GROQ_API_KEY` stays on the server; no response or served file contains it, which is tested.
- No CORS middleware is installed, so browsers on other origins can't call the API. CORS isn't authentication,
  though: it doesn't stop scripts or curl.
- `uvicorn` binds to `127.0.0.1` by default. Binding `--host 0.0.0.0`, tunnelling, or deploying publicly without
  `AGENT_LIVE_TOKEN` keeps live mode limited to the host machine; with a token, anyone holding it can spend quota
  up to the limits.
- For a public demo, the safest choice is `AGENT_PROVIDER=deterministic` (no provider calls at all), with live
  mode only on the owner's machine.

## Agent tools (MCP server `blastgate`, stdio)

`python -m blastgate.mcp_server` is started as a subprocess for each investigation by `agent.McpToolClient`
(official `mcp` 2.3.0 SDK: `MCPServer` on the server side, `Client(StdioServerParameters)` on the client side). Tools are
discovered with `list_tools`, and their schemas are what the LLM sees. Every tool returns
`{status, summary, data, evidence[], unknowns[], next_actions[], error_code}`.

| Tool | Input (validated) | Output |
| --- | --- | --- |
| `inspect_change` | `pr_id` (enum of known PRs) | producer, contract, field, old type (from the graph) vs declared new type, breaking flag |
| `inspect_contract_coverage` | `pr_id` | mapping status and note, every consumer of the field with `fallback_enabled` and `evidence_source` |
| `trace_blast_radius` | `pr_id`, `max_depth` 1–8 | affected services with depth and path, synthetic SLAs, labelled exposure estimate |
| `analyze_network_criticality` | `services` (1–20 IDs, `^[a-z][a-z0-9-]*$`) | `algo.betweenness` scores (sampled approximation, informational) |
| `retrieve_incidents_and_reviewers` | `pr_id`, `services` | incidents caused by changes to *this* contract; engineers connected by graph edges |
| `simulate_mitigation` | `pr_id`, `consumer`, `assumption` (10–300 chars) | before/after on a `GRAPH.COPY`; source integrity checked; fails closed |

There's no raw-Cypher tool, no tool takes a graph name, and IDs are re-validated at the tool boundary
(`pydantic.validate_call`). The only write happens inside `simulate_mitigation`, which writes to a temporary
copy that is deleted in `finally`; if containment can't be verified, the result is discarded with
`error_code: integrity`.

## How the Groq tool-calling loop works

`agent.GroqPlanner` uses the official `groq` SDK (`AsyncGroq`, chat completions, `tool_choice="auto"`):

1. It sends the system and user messages plus the MCP-discovered tool schemas, and a `submit_report` function
   for the final structured answer.
2. It parses `message.tool_calls`; each `function.arguments` value is a JSON string. If parsing fails, the call
   becomes a rejected call, not a crash.
3. The orchestrator checks each call:
   - the tool must be on the **static allowlist** and discovered over MCP;
   - its arguments must pass **JSON-schema validation**;
   - `pr_id` must be the PR under investigation;
   - it must not repeat an earlier call;
   - at most **6 calls** run in total;
   - `simulate_mitigation` runs only if the user asked for it, and always with the user's own `consumer` and
     `assumption`.
4. Allowed calls run **one at a time** over MCP. Each result goes back to Groq as
   `{"role": "tool", "tool_call_id": <id>, "name", "content"}`, right after the assistant turn that made the
   call. Rejected calls get an error result with the same ID.
5. The loop stops when the model calls `submit_report` or replies in plain text, or when the budget runs out, a
   timeout hits, or a safe error stops it.
6. **The model's `reasoning` field is never stored or returned.** The trace records only the tool, arguments,
   purpose, result summary, timing and errors.

**Retries.**
- **SDK:** one SDK retry (`max_retries=1`) covers transient errors and honours `retry-after` on 429. Any further 429 goes back to the caller with `Retry-After`.
- **`tool_use_failed`:** this is the only error BlastGate retries itself. It means Groq rejected a generated tool call that broke the advertised schema; observed live, the model called `submit_report` without the required `summary`. BlastGate retries exactly once, at temperature 0. The `submit_report` schema stays strict.
- **Never retried by BlastGate:** other 400s, 401, 429 and 5xx.
- **No hidden fallbacks:** there are no unbounded sleeps or retry loops, no automatic switch to another provider, and no fallback to the demo planner.
- **Exact tool names:** names must match exactly. A `functions.`-prefixed name (seen live) is rejected by the allowlist, not rewritten.

## Evidence inventory and explanation gate

Every investigation report carries a code-owned `inventory`. It covers vulnerable consumers, affected (critical)
services, SLAs at risk, incidents on the contract, and reviewers, each with a status:

| Status | Meaning | Supports "there are none"? |
| --- | --- | --- |
| `found` | the graph returned these items | no |
| `none_found` | checked; nothing in the checked graph | **yes**, and only this status does |
| `unknown` | the change target (contract/field) isn't verified in the graph | no |
| `not_checked` | nobody looked it up in this investigation | no |
| `check_failed` | the lookup errored | no |

`summary` is rendered from the deterministic verdict and this inventory. Example: `incidents on this contract:
INC-1877` or `…: not checked`.

The model's text is checked against the inventory. It is withheld, and `explanation_status` becomes
`withheld_inconsistent` with a warning naming the conflict, if it:
- claims there are no incidents, vulnerable consumers, SLAs or downstream impact when the status isn't
  `none_found`; or
- names entities no tool returned.

This came from a real live run: `gpt-oss-20b` wrote "no incidents have been associated with this contract" right
after its own tool call returned INC-1877. The incident was in the tool trace and in `verified_facts`; only the
prose was wrong, and that prose had been passed through as `summary`.

The absence-claim check is a conservative phrase match. It may withhold correct text that happens to look like a
denial; it doesn't try to understand free text.

## Verdict semantics

1. Producer/contract not found → **WARN**. Field not found → **WARN**.
2. Breaking change, a consumer has `fallback_enabled=false`, and a critical service or SLA is affected → **BLOCK**.
   Without a critical service or SLA → **WARN**. A known impact blocks even when coverage is incomplete.
3. Mapping not `complete`, or any consumer with a non-boolean fallback or empty `evidence_source` → **WARN**.
4. Otherwise → **CLEAR**.

**Why CLEAR depends on verified coverage.** An empty consumer list means "nobody we know of", not "nobody". CLEAR
requires `consumer_mapping_status=complete`, plus evidenced fallbacks for every indexed consumer. PR-108 has no
indexed consumer but incomplete mapping, so it is WARN, never CLEAR.

**Why a simulation isn't proof.** Every simulation result carries the label *"Counterfactual graph simulation only. The proposed code mitigation remains unverified until contract or integration tests pass."*
`simulate_mitigation` flips one graph property on a copy and re-runs the rules.
That shows what the topology would imply *if* the fix existed. It says nothing about whether real code parses the
new type. Every simulated result carries a structured `counterfactual` block (`mitigation_status:
"assumed_unverified"`, `verified_by_contract_or_integration_tests: false`, `production_verdict`), and its reasons
are prefixed `[COUNTERFACTUAL - assumed mitigation, unverified]`. The investigation verdict for PR-104 stays
**BLOCK**.

## Graph model

```text
(:Service {name, tier: critical|standard, team, synthetic})
(:Contract {contract_id, kind, consumer_mapping_status: complete|incomplete, coverage_note, synthetic})
(:SchemaField {name, contract_id, field_type, required})
(:SLAContract {sla_id, name, penalty_per_hour_usd, synthetic: true, label})
(:Incident {incident_id, severity, summary, synthetic: true, label})
(:Engineer {handle, display_name, synthetic: true})

(Service)-[:EXPOSES]->(Contract)
(Contract)-[:HAS_FIELD]->(SchemaField)
(Service)-[:CONSUMES_FIELD {fallback_enabled, evidence_source}]->(SchemaField)
(Service)-[:DEPENDS_ON]->(Service)            # (B)-[:DEPENDS_ON]->(A) means B depends on A
(Service)-[:BACKS_SLA]->(SLAContract)
(Incident)-[:CAUSED_BY_CHANGE_IN]->(Contract)
(Engineer)-[:AUTHORED_COMMITS]->(Service)
(Engineer)-[:RESOLVED_INCIDENT]->(Incident)
```

```text
pricing-engine → checkout-api → order-orchestrator → invoice-worker
                      └→ storefront-bff        └→ partner-gateway
catalog-service → search-indexer, storefront-bff
```

All data lives as constants in `blastgate/graph.py` and is loaded with parameterised `UNWIND` queries. `seed()`
checks every node and relationship count against the source data and deletes the graph if they differ.
`UNWIND … MATCH … CREATE` silently skips rows whose match fails, so without this check a dropped consumer edge
could quietly turn BLOCK into CLEAR.

**Impact depth.** A direct consumer is at depth 1, and each `DEPENDS_ON` hop adds 1; the shortest route wins.
SLA links never count as hops. The consumer, incident, reviewer and simulation queries are anchored at
`(:Service {name:$producer})-[:EXPOSES]->`, so a contract with the same ID under another producer can't leak
in.

## Scenarios (synthetic)

| PR | Change | Graph evidence | Verdict |
| --- | --- | --- | --- |
| PR-104 | `pricing-api-v2.discount_tier` string → object | `checkout-api` consumes it with `fallback_enabled=false`; depth 2–3 reaches 4 more services; 3 synthetic SLAs; INC-2041; reviewer `eng-alpha` | **BLOCK** |
| PR-108 | `partner-webhook-v1.payload_signature` hex → base64 | no internal consumer indexed, `consumer_mapping_status=incomplete` | **WARN** |
| PR-112 | optional `catalog-api-v1.promo_badge` format | coverage complete; only consumer `search-indexer` has an evidenced fallback | **CLEAR** (modelled assumptions) |
| PR-114 | required `catalog-api-v1.sku` format | `storefront-bff` (`standard` tier) consumes it with `fallback_enabled=false`; no critical service or SLA affected; INC-1877; reviewer `eng-charlie` | **WARN** |
| PR-104 sim | *assume* `checkout-api` gains a fallback | on a `GRAPH.COPY`; no vulnerable consumer remains | **CLEAR** (simulation only) |

## Tests

None of these need a Groq key. FalkorDB-backed tests are **skipped with a reason** if the database is unreachable.
Skipped isn't passed.

```bash
pytest -rs                                  # everything (live tests auto-skip without FalkorDB / key)
pytest tests/unit tests/agent/test_groq_planner.py tests/agent/test_orchestrator_unit.py tests/api -m "not integration"  # no DB
pytest tests/mcp -rs                        # real MCP stdio subprocess + live FalkorDB
pytest tests/evaluation -s -rs              # 12-task agent eval; prints the metrics table
```

| Suite | What it proves | Needs |
| --- | --- | --- |
| `tests/unit`, `tests/integration` | Milestone 1 engine, simulation, isolation | integration: FalkorDB |
| `tests/agent/test_groq_planner.py` | Groq request shape, tool-call parsing, `tool_call_id` association, malformed output, 401/403/404/429/400/5xx/timeout/network | nothing (**scripted mock** Groq responses) |
| `tests/agent/test_orchestrator_unit.py` | allowlist, schema validation, PR pinning, duplicates, 6-call cap, unrequested simulation, verdict integrity, grounding, unsupported references | nothing (fake tool client) |
| `tests/agent/test_agent_live.py` | full agent over **real MCP** + live graph; scripted Groq loop choosing tools from real results; provider errors map to 429/502/504 | FalkorDB |
| `tests/mcp` | server start/stop, discovery, schemas, real results, safe errors, DB-down, primary-graph isolation, server-unavailable → 503 | FalkorDB |
| `tests/api` | settings parsing, missing-key 503, validation, 404/422/503, OpenAPI, analyze/simulate/investigate | partly FalkorDB |
| `tests/evaluation` | 12 synthetic tasks (below) with machine-checked expectations | FalkorDB |
| `tests/providers/test_groq_live.py` | **opt-in** live smoke test: one small real Groq request | `GROQ_API_KEY` |
| `tests/unit/test_codegraph_unit.py` | diff parsing, changed symbols, field delta, strict mapping, graph structure changes the result, audit regressions, PR-900 registration | nothing (fake graphs, local git) |
| `tests/integration/test_codegraph_live.py` | 9 scenarios on real Gristle indexing + FalkorDB | `BLASTGATE_GRISTLE_BIN`, FalkorDB |
| `tests/integration/test_codegraph_agent_loop.py` | saved review → PR-900 → real MCP agent → BLOCK → counterfactual; graph unchanged | FalkorDB |
| `tests/unit/test_memory_unit.py` | revalidation, grouping, link rules (no stub `CodeReview`) | nothing |
| `tests/integration/test_memory_live.py` | memory across **separate processes**: recall + `FOLLOWS`, field scoping, a prior CLEAR never clears, reviewer → agent `BUILDS_ON`, broken store, off by default | FalkorDB |
| `tests/api/test_codegraph_report.py` | read-only report endpoint: no caller input, malformed/oversized refused | nothing |
| `web/tests/view.test.mjs` (`node --test web/tests/view.test.mjs`) | console rendering of real recorded responses, agent timeline, loop strip, escaping | Node |

**Eval tasks.** The 12 tasks cover:
- a breaking change that hits a critical service
- incomplete coverage
- a safe change
- an unknown field
- a producer/contract mismatch
- duplicate edges
- missing SLA data
- incident history that isn't relevant
- a requested simulation
- an attempted simulation the user didn't request
- a database failure
- a provider failure

Each task checks the verdict, the affected services, evidence that must and must not appear, grounding, allowed
and forbidden tools, and the 6-call cap. It also reports latency and call counts. By default the tasks run with
the **deterministic demo planner**, which measures the tools, guards and grounding rather than LLM quality. For a
real-LLM eval, set `BLASTGATE_EVAL_LIVE=1`, `AGENT_PROVIDER=groq` and `GROQ_API_KEY`; mind the free-tier limits.

**Live Groq smoke test** (makes one small request):

```bash
set -a; source .env; set +a
pytest tests/providers -m live_provider -rs
```

## Groq free tier and troubleshooting

According to Groq's rate-limit docs, `openai/gpt-oss-20b` on the free tier is listed at **30 requests/min,
1K requests/day, 8K tokens/min and 200K tokens/day**; check your organisation's values in the console.
An investigation makes about 3–7 model requests, so a few investigations a minute can hit the token-per-minute
limit. Availability and limits are set by Groq and can change.

| Symptom | Meaning / fix |
| --- | --- |
| `503 agent_provider_not_configured` | set `AGENT_PROVIDER=groq` and `GROQ_API_KEY` in `.env`, then reload it into the shell |
| `502 provider_auth` | key invalid or revoked (401/403): create a new key in the Groq console |
| `502 provider_model_unavailable` | `AGENT_MODEL` isn't available to your account (404): pick a tool-use model from the docs |
| `429 provider_rate_limited` | free-tier limit hit: wait for `Retry-After` seconds |
| `502 provider_bad_request (tool_use_failed)` | the model produced a schema-invalid tool call twice in a row (BlastGate already retried once); retry the request, or try a larger model |
| `504 provider_timeout` / `502 provider_network` | raise `AGENT_TIMEOUT_S` or check connectivity |
| `503 graph_unavailable` | start FalkorDB: `docker start blastgate-falkordb` |

## Verified FalkorDB / SDK facts (checked against docs and the live server)

- `graph.query/ro_query(q, params)` send params as a `CYPHER k=v` header; `ro_query` rejects writes.
- `graph.copy(clone)` is `GRAPH.COPY`; it errors if the target exists, and the copy is independent (verified).
  `graph.delete()` on a missing graph raises, so the code checks `list_graphs()` first.
- `FalkorDB()` connects **eagerly** (`INFO` for Sentinel detection), so BlastGate connects lazily inside guarded
  calls and a down database becomes a clean 503 instead of a crash.
- `algo.betweenness` defaults to a time-based sampling seed. We observed the same node scoring 0.0 and 6.0 across
  runs, so BlastGate pins `samplingSize=32, samplingSeed=1`.
- `mcp` 2.x renamed FastMCP to `MCPServer`. Unexpected extra tool arguments are silently ignored by the SDK, so
  the orchestrator rejects them itself. Raising inside an MCP client session surfaces as an anyio
  `ExceptionGroup`, so provider errors are raised only after the session closes.

## Code-graph review (Gristle + git diff → verdict → review report)

`python -m blastgate.codegraph REPO --base REF [--simulate-fallback SERVICE] [--out PATH]` reviews the working
tree of a local git repo against `REF`, with no LLM. It exits 0 CLEAR, 1 WARN, 2 BLOCK, or 3 error with no verdict.

1. [Gristle](https://github.com/Alchemy-Agentic/gristle) (MIT) indexes the base commit and the working tree into
   two temporary FalkorDB code graphs, using its real tree-sitter parser. It runs as its own executable
   (`BLASTGATE_GRISTLE_BIN`) because it pins `mcp<2`.
2. Changed symbols are the code-graph `Function`/`Class` nodes whose line ranges overlap the `git diff -U0` hunks.
   Contract-field changes come from comparing the class's `TypeField`s across the two graphs (removed and
   type-changed fields are breaking; added fields are not).
3. Code impact uses BlastGate's own read-only Cypher over Gristle's schema: `ACCEPTS`/`RETURNS` (type-level),
   transitive `CALLS` (confidence = weakest `resolution`), `Route-[:HANDLES]`, and `TESTS_FUNCTION`.
4. Source → service only via `blastgate.map.json` **read from the base commit**. Each mapping is validated:
   the service must exist, and the producer must `EXPOSE` the contract and field. Unmapped, ambiguous or
   invalid mappings stay unknown. Names and field strings are never matched.
5. The verdict is the existing `analyze()`/`decide()` on the service graph. Code evidence can only add caution
   (CLEAR → WARN). The report lists code evidence, service evidence, the cross-check (corroborated, code-only,
   service-graph-only), recommended tests, reviewers, uncertainties, known gaps and the full query trace.

`REPO` must be a git repository root. The base snapshot is written from exact blobs (`git ls-tree`/`cat-file`),
so `.gitattributes` `export-ignore`/`export-subst` can't drop or rewrite files. Diff output options are pinned
so `diff.noprefix` and similar user config can't change the parsed paths. Gristle's dependency checker (which
would send package names to pypi.org / npm / osv.dev) is disabled. Repository code goes to no model provider
and no network service.

### Reproducible setup (separate venv; never install Gristle into `.venv`)

```bash
scripts/setup_gristle.sh       # Python >= 3.11 (default python3.12) -> .gristle-venv/, Gristle v0.12.0 pinned
                               # by commit d72083d, deps pinned by scripts/gristle-constraints.txt.
                               # An existing venv is never removed or recreated.
export BLASTGATE_GRISTLE_BIN="$PWD/.gristle-venv/bin/gristle"   # the `gristle` console script itself
```

### Demo (synthetic, needs FalkorDB; no LLM)

```bash
.venv/bin/python scripts/codegraph_demo.py      # exit 2 = BLOCK; writes codegraph_demo_out/report.{md,json}
.venv/bin/uvicorn blastgate.api:app --port 8000 # console -> "Code-graph review" -> "Git diff"
```

The demo commits `examples/shop_monorepo` into a temporary git repo and changes
`PriceQuote.discount_tier: str → dict`. It reviews the change against a temporary demo service graph (never
`BLASTGATE_GRAPH`) with a checkout-api counterfactual, then drops that graph. The console reads the report through
`GET /api/codegraph/report`, which serves only the file at `BLASTGATE_CODEGRAPH_REPORT` (default
`codegraph_demo_out/report.json`). It takes no caller input and never runs git, Gristle or any subprocess.

Live tests: `BLASTGATE_GRISTLE_BIN=... .venv/bin/python -m pytest tests/integration/test_codegraph_live.py`.
They skip with a reason when the binary or FalkorDB is unavailable. Skipped is not passed.

### Hand-off to the MCP agent (PR-900)

When the operator sets `BLASTGATE_CODEGRAPH_REPORT`, each **validated** contract change in that saved review
becomes an investigable change: `PR-900`, `PR-901`, and so on. Registration happens in `blastgate/tools.py` at
import, in both the API and the MCP server process; `McpToolClient` forwards that one variable. From there the
existing `/api/analyze`, `/api/agent/investigate` and `/api/simulate` run unchanged on the change the code graph
detected, with the same six tools and the same gate.

- Unmapped, ambiguous or invalid changes are never registered.
- Existing PR ids are never overridden.
- A malformed file registers nothing; a warning is logged and the server still starts.
- The model can't add or alter a scenario.

With the variable unset, nothing changes.

### Cypher over the code graph (Gristle schema, all `ro_query`, parameterised)

| Query | Purpose |
| --- | --- |
| `CG_SYMBOLS_IN_FILE` | `Function`/`Class` line ranges in a changed file, matched against diff hunks (base and head) |
| `CG_TYPE_FIELDS` | `(:Class)-[:HAS_FIELD]->(:TypeField)`, compared base vs head to detect removed or type-changed fields |
| `CG_TYPE_USERS` | `(:Function)-[:ACCEPTS\|RETURNS]->(:Class)`: code that handles the contract type |
| `CG_CALLERS` | `(:Function)-[:CALLS*1..4]->(:Function)`, keeping each edge's `resolution` for confidence |
| `CG_ROUTES` | `(:Route)-[:HANDLES]->(:Function)` |
| `CG_TESTS` | `()-[:TESTS_FUNCTION]->(:Function)`: tests to recommend |

## Investigation memory (opt-in, `BLASTGATE_MEMORY_GRAPH`)

Memory lives in a **separate** FalkorDB graph, so the service graph is never written. Two processes share it:
`blastgate.codegraph` (the code-graph reviewer) and the investigation agent behind `/api/agent/investigate`.

```text
(:Investigation)-[:EXAMINED]->(:Change)-[:MODIFIES]->(:ContractField {key: "pricing-api-v2.discount_tier"})
(:Investigation)-[:FOUND_AFFECTED {depth, tier}]->(:Service)     (:Investigation)-[:FOUND_AT_RISK]->(:SLA)
(:Investigation)-[:CITED]->(:Incident)     (:Investigation)-[:RECOMMENDED_REVIEWER {resolved_incidents, authored_services}]->(:Engineer)
(:Investigation)-[:FOLLOWS]->(:Investigation)       (:Investigation)-[:BUILDS_ON]->(:CodeReview)
(:CodeReview)-[:DETECTED_CHANGE_TO]->(:ContractField)   (:CodeReview)-[:CODE_DEPENDENT_IN]->(:Service)   (consumer side only)
```

- **Keys are exact ids** returned by the tools or the engine. Recall covers the same `contract_id.field` only,
  with at most 5 investigations and 5 reviews.
- **History is context, not evidence.** Each remembered fact is shown with what the current graph says now
  (`revalidation`). The verdict is computed without memory, so a prior CLEAR never clears a new change and a prior
  BLOCK never blocks an unrelated one.
- **Who reads history.** The live model gets it as context, and the existing explanation gate withholds its
  text if that text cites anything the tools didn't return. The rule-based demo planner doesn't read history;
  code revalidates it.
- **Writes are safe to retry.** Each record is one atomic Cypher query using `MERGE`, so a retry with the same id
  creates nothing new. A `BUILDS_ON` link is written only to a code review that recall actually returned, never to
  a stub.
- **Failures never change the result.** A recall or write failure is reported as `recall_failed` /
  `record_failed`, and the investigation result is unchanged. With the variable unset, `memory` is `null` and
  nothing is written.
- **Not modelled:** service **owners**. `Service.team` is a property, not a relationship, so BlastGate names
  reviewers through `RESOLVED_INCIDENT` and `AUTHORED_COMMITS` and never infers owners. There is no repository
  node either; changes are keyed by PR id and, for code-graph changes, the git base sha.

## Limitations

- Synthetic, hand-modelled service data. There's no ingestion from real manifests, traces or incident tools.
  `breaking` is declared per scenario for the PR scenarios; only the code-graph review derives it from a diff.
- The code-graph review is Python/TypeScript-only (Gristle's parsers). It can't see attribute reads or
  cross-repo HTTP consumers, and it reviews tracked changes only (untracked files are indexed but not diffed).
- Betweenness is a sampled approximation: reproducible, but not exact. It's informational only.
- Exposure is penalty/hour × an assumed outage duration, not a prediction or a measured loss.
- The MCP server starts once per investigation (about 0.3 s). Downstream traversal is capped at 8 hops.
- Live Groq behaviour depends on the model: a 20B model may choose tools less well. The verdict is unaffected.
- **Not production-ready.** Live agent investigations support optional bearer-token gating (`AGENT_LIVE_TOKEN`) and in-process rate limits (`LiveLimiter`), but demo routes are unauthenticated and TLS/shared multi-worker stores are not included.

## AI assistance disclosure

This project was built with AI coding assistance, disclosed per the GraphHacks rules:

- **Claude Code** (Anthropic) wrote and ran the code and tests under human direction.
- **Everything Claude Code (ECC)** workflows:
  - `search-first` to check the FalkorDB client, Cypher, `GRAPH.COPY`, `algo.betweenness`, the `mcp` 2.x SDK and
    the `groq` SDK against official docs and installed sources before coding;
  - `change-impact` for an independent correctness audit with adversarial probes. It found and fixed a
    silent-CLEAR seeding bug and a cross-producer contract leak.
- **Ponytail** for minimal-code discipline: no unrequested abstractions or dependencies.
- **Runtime:** GroqCloud provides live LLM inference for the agent.

The team must review and be able to explain the graph model, queries, agent loop and decisions.
