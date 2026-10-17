"""BlastGate Milestone 1 CLI: seed the synthetic graph, analyse scenarios, run the counterfactual.

Usage:
    python blastgate_milestone1.py                    # seed + all scenarios + PR-104 simulation
    python blastgate_milestone1.py --scenario PR-108  # one scenario
    python blastgate_milestone1.py --json             # machine-readable output
    python blastgate_milestone1.py --no-seed          # analyse the existing graph as-is

Exit codes: 0 ok, 2 database/analysis error.
"""
import argparse
import json
import sys

from blastgate.analysis import AnalysisError, analyze
from blastgate.graph import SCENARIOS, SYNTHETIC, connect, graph_name, seed
from blastgate.queries import CENTRALITY_SAMPLING
from blastgate.simulation import simulate_consumer_fallback


def print_report(r) -> None:
    sc = r.scenario
    print(f"\n=== {sc.pr_id}: {sc.title}  [{SYNTHETIC}]")
    print(f"  change: {sc.change} | breaking: {sc.breaking} | local tests: {sc.local_tests}")
    print(f"  producer/contract/field: {sc.producer} / {sc.contract_id} / {sc.field} "
          f"(contract found: {r.contract_found}, field found: {r.field_found})")
    print(f"  consumer mapping: {r.mapping_status} - {r.coverage_note}")
    print(f"  indexed consumers of field: "
          + (", ".join(f"{c['service']} (fallback={c['fallback_enabled']})" for c in r.consumers) or "none"))
    for a in r.affected_services:
        print(f"    impact depth {a['depth']}: {a['service']} [{a['tier']}]  path: {' -> '.join(a['path'])}")
    for s in r.slas:
        print(f"    SLA {s['sla_id']} on {s['service']}: ${s['penalty_per_hour_usd']}/h (synthetic={s['synthetic']})")
    print(f"  betweenness (FalkorDB sampled approximation, {CENTRALITY_SAMPLING}; informational, not a verdict "
          f"input): {r.centrality if r.centrality is not None else 'not run'}")
    print(f"  incidents: {[i['incident_id'] for i in r.incidents] if r.incidents is not None else 'not run'}")
    print(f"  reviewers: {[x['handle'] for x in r.reviewers] if r.reviewers is not None else 'not run'}")
    if r.exposure:
        print(f"  exposure: ${r.exposure['assumed_total_usd']:,.0f}  ({r.exposure['label']})")
    print(f"  VERDICT: {r.verdict.value}")
    for reason in r.decision.reasons:
        print(f"    - {reason}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scenario", choices=sorted(SCENARIOS), help="run one scenario (default: all)")
    p.add_argument("--no-seed", action="store_true", help="do not rebuild the graph first")
    p.add_argument("--no-simulate", action="store_true", help="skip the PR-104 counterfactual")
    p.add_argument("--json", action="store_true", help="emit JSON")
    args = p.parse_args(argv)

    name = graph_name()
    try:
        db = connect()
        graph = db.select_graph(name) if args.no_seed else seed(db, name)
        reports = [analyze(graph, SCENARIOS[k]) for k in ([args.scenario] if args.scenario else sorted(SCENARIOS))]
        sim = None
        if not args.no_simulate and args.scenario in (None, "PR-104"):
            sim = simulate_consumer_fallback(db, name, SCENARIOS["PR-104"], "checkout-api")
    except AnalysisError as e:
        print(f"ANALYSIS ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # connection refused, auth, etc.
        print(f"DATABASE ERROR ({type(e).__name__}): {e}\n"
              f"Is FalkorDB running? docker run -d --name blastgate-falkordb -p 6379:6379 falkordb/falkordb:latest",
              file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"graph": name, "reports": [r.as_dict() for r in reports],
                          "simulation": sim.as_dict() if sim else None}, indent=2, default=str))
        return 0

    print(f"BlastGate Milestone 1 - graph '{name}'" + (" (seeded)" if not args.no_seed else ""))
    for r in reports:
        print_report(r)
    if sim:
        print(f"\n=== Counterfactual: PR-104\n  {sim.label}\n  {sim.assumption}")
        print(f"  before: {sim.before.verdict.value} (vulnerable: "
              f"{[c['service'] for c in sim.before.vulnerable_consumers]})")
        print(f"  after:  {sim.after.verdict.value} (vulnerable: "
              f"{[c['service'] for c in sim.after.vulnerable_consumers]})")
        print(f"  temp graph {sim.temp_graph} deleted: {sim.temp_graph_deleted} | "
              f"original graph unchanged: {sim.original_unchanged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
