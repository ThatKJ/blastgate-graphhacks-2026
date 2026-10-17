# BlastGate — Milestone 1

Evidence-first pre-merge change-impact analysis on **FalkorDB**.
GraphHacks Track 01: *Agents That Act on Connected Data*.

BlastGate takes a proposed API/schema change, traces who consumes the changed field through a
FalkorDB graph, follows the service dependency chain downstream, attaches SLAs, pulls incident
history and candidate reviewers, and returns a deterministic **BLOCK / WARN / CLEAR** verdict.
An empty query result is never treated as "safe" unless contract coverage is verified complete.

Milestone 1 covers the graph model, the deterministic engine, the counterfactual simulation and the tests.
**No LLM agent, API server or frontend yet** (planned for later milestones).

> **All data is synthetic.** Services, engineers (`eng-alpha`…), SLAs, penalty rates and incidents
> are fabricated for this demo. They describe no real company, person, customer or measured loss.

## Setup

Requires Python 3.10+ and Docker.

```bash
# 1. FalkorDB (verified with falkordb/falkordb:latest = FalkorDB module 4.22.1)
docker run -d --name blastgate-falkordb -p 6379:6379 falkordb/falkordb:latest
#   later: docker start blastgate-falkordb

# 2. Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # FalkorDB==1.8.0 (official client), pytest

# 3. Optional config (defaults: localhost:6379, graph "blastgate_m1")
cp .env.example .env && set -a && source .env && set +a
```

## Commands

```bash
python blastgate_milestone1.py                    # seed graph, run PR-104/108/112, run PR-104 counterfactual
python blastgate_milestone1.py --scenario PR-108  # one scenario
python blastgate_milestone1.py --json             # structured report
python blastgate_milestone1.py --no-seed          # analyse the existing graph without reseeding

pytest tests/unit -v           # no database needed
pytest tests/integration -v -rs  # needs FalkorDB; SKIPPED (with reason) if unreachable
pytest -rs                     # everything
```

Seeding only ever deletes and recreates the graph named by `BLASTGATE_GRAPH`. Integration tests
use unique `blastgate_test_*` graphs and delete them afterwards.

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

Dependency chain (impact flows left to right; edges point right to left):

```text
pricing-engine → checkout-api → order-orchestrator → invoice-worker
                      └→ storefront-bff        └→ partner-gateway
catalog-service → search-indexer, storefront-bff
```

All data is defined as constants in `blastgate/graph.py` and loaded with parameterised `UNWIND`
queries, so every seed produces an identical graph (this is tested). `UNWIND … MATCH … CREATE` silently
skips rows whose `MATCH` fails, which could drop a consumer edge and turn BLOCK into CLEAR. So `seed()`
checks every node-label and relationship count against the source data and deletes the graph if they differ.

## Scenarios (synthetic)

| PR | Change | Graph evidence | Verdict |
| --- | --- | --- | --- |
| PR-104 | `pricing-api-v2.discount_tier` string → object | `checkout-api` consumes it with `fallback_enabled=false`; downstream depth 2–3 reaches `order-orchestrator`, `storefront-bff`, `invoice-worker`, `partner-gateway`; 3 synthetic SLAs; incident INC-2041; reviewer `eng-alpha` | **BLOCK** |
| PR-108 | `partner-webhook-v1.payload_signature` hex → base64 | No internal consumer indexed, but `consumer_mapping_status=incomplete` (external partners unmapped) | **WARN** |
| PR-112 | optional `catalog-api-v1.promo_badge` format | Coverage complete; only consumer `search-indexer` has an evidenced fallback | **CLEAR** (under modelled assumptions) |
| PR-104 sim | *Assume* `checkout-api` gains a compatible fallback | Run on a `GRAPH.COPY`; no vulnerable consumer remains | **CLEAR** (simulation only) |

Local unit tests are *represented* as passing in each scenario; nothing is executed against real code.

## How the analysis works (`blastgate/`)

| Module | Role |
| --- | --- |
| `graph.py` | connection from env vars, synthetic data, scenarios, deterministic `seed()`, `fingerprint()` |
| `queries.py` | all Cypher, every query parameterised (`$producer`, `$contract`, …) and run with `ro_query` |
| `analysis.py` | runs the queries for a scenario and builds a frozen `Report` |
| `verdict.py` | pure-Python `decide(Facts) -> Decision`; no database access, no free text in |
| `simulation.py` | copy → mitigate copy → re-analyse → delete copy (in `finally`) → verify original unchanged |

Queries (see `blastgate/queries.py` for the exact Cypher):

1. **COVERAGE**: `(producer)-[:EXPOSES]->(contract)`, `OPTIONAL MATCH` the field. No row means producer/contract mismatch (WARN). A null field means the field isn't modelled (WARN).
2. **CONSUMERS**: every `CONSUMES_FIELD` on that exact field, with `fallback_enabled` and `evidence_source`. This query, like INCIDENTS, REVIEWERS and the simulation write, starts from `(:Service {name:$producer})-[:EXPOSES]->`, so a contract with the same id under another producer can't leak in.
3. **DOWNSTREAM**: `(d)-[:DEPENDS_ON*1..8]->(vulnerable consumer)`. **Impact depth** = service-to-service hops from the producer: direct consumer = 1, each `DEPENDS_ON` hop adds 1, shortest route wins. SLA attachment is a separate query and never counts as a hop.
4. **SLAS**: `BACKS_SLA` for affected services. Exposure = Σ distinct synthetic penalty/hour × scenario `assumed_outage_hours`, computed in Python and labelled *ASSUMED*.
5. **CENTRALITY**: `CALL algo.betweenness({nodeLabels:['Service'], relationshipTypes:['DEPENDS_ON'], samplingSize:$size, samplingSeed:$seed})`, run **only** when there are affected services.
6. **INCIDENTS / REVIEWERS**: incidents `CAUSED_BY_CHANGE_IN` the contract, and engineers who resolved them or committed to affected services. Run **only** when there are affected services.

### Verdict rules (`verdict.py`)

1. Producer/contract not found → **WARN**. Field not found → **WARN**.
2. Breaking change, a consumer has `fallback_enabled=false`, and a critical service or SLA is affected → **BLOCK**. Without a critical service or SLA → **WARN**. A known impact blocks even when coverage is incomplete.
3. Mapping not `complete`, or any consumer with a non-boolean fallback or empty `evidence_source` → **WARN**.
4. Otherwise → **CLEAR**.

Database or query errors raise `AnalysisError` (the CLI exits 2). They never become a verdict.
The verdict is an `Enum` produced only by `decide()` from graph facts. A future LLM layer may
explain a `Report` but has no input path into `decide()`.

## Verified FalkorDB API facts (checked against docs/source and the live server)

- `FalkorDB(host, port, password)`, `db.select_graph(name)`, `db.list_graphs()`.
- `graph.query(q, params=None, timeout=None)` / `graph.ro_query(...)`. Params are sent as a `CYPHER k=v` header. `ro_query` rejects writes (`graph.RO_QUERY is to be executed only on read-only queries`).
- `graph.copy(clone)` issues `GRAPH.COPY` and returns the clone's `Graph`. It errors with `destination key already exists`. The copy is independent (verified: writes to the copy don't touch the source).
- `graph.delete()` issues `GRAPH.DELETE`. On a missing graph it raises `Invalid graph operation on empty key`, which is why code checks `list_graphs()` first.
- `algo.betweenness` defaults to `samplingSize=32` and a **time-based** `samplingSeed`. We observed the same node scoring 0.0 and 6.0 on identical graphs across runs, so BlastGate pins `samplingSize=32, samplingSeed=1`.

## Limitations (honest list)

- All data is synthetic and hand-modelled. Nothing is ingested from real repos, manifests, traces or incident tools.
- Betweenness is FalkorDB's **sampled approximation**: reproducible with the pinned seed, but not exact. Even with `samplingSize` ≥ node count, the scores didn't match hand-computed exact values. It's informational only and is never a verdict input.
- The counterfactual is a **topology simulation only**: *"Counterfactual graph simulation only. The proposed code mitigation remains unverified until contract or integration tests pass."*
- Financial exposure is penalty/hour × an assumed outage duration. It isn't a prediction or a measured loss.
- `breaking` is declared per scenario. There's no schema-diff detection yet.
- Downstream traversal is capped at 8 hops.
- No LLM agent, API server or UI yet. No auth or TLS configuration beyond an optional password.

## AI assistance disclosure

Built with an AI coding assistant (Claude Code). The team must review and be able to explain the graph model, queries and decisions, per the hackathon rules.
