"""Reproducible, synthetic code-graph review demo. No LLM, no network, nothing outside --out is kept.

    export BLASTGATE_GRISTLE_BIN="$PWD/.gristle-venv/bin/gristle"     # scripts/setup_gristle.sh
    .venv/bin/python scripts/codegraph_demo.py                         # -> codegraph_demo_out/report.{md,json}

1. Copies examples/shop_monorepo into a temporary git repo and commits it as the base.
2. Applies one real source change: PriceQuote.discount_tier `str` -> `dict`.
3. Seeds a dedicated demo service graph (never the configured BlastGate graph), runs the review with a
   counterfactual for checkout-api, writes the report, and drops the demo graph again.
The console shows the report at "Code-graph review" (GET /api/codegraph/report reads report.json).
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blastgate.codegraph import RANK, CodeGraphError, render_markdown, review  # noqa: E402
from blastgate.graph import connect, graph_name, seed  # noqa: E402

APP = ROOT / "examples" / "shop_monorepo"
CHANGE = ("contracts/pricing.py", "    discount_tier: str\n", "    discount_tier: dict\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=str(ROOT / "codegraph_demo_out"))
    p.add_argument("--graph", default="blastgate_codegraph_demo", help="temporary demo service graph")
    a = p.parse_args()
    if a.graph == graph_name():
        sys.exit(f"refusing to reseed the configured BlastGate graph '{a.graph}'; pick another --graph")
    out = Path(a.out)
    out.mkdir(exist_ok=True)

    db = connect()
    try:
        with tempfile.TemporaryDirectory(prefix="blastgate-demo-") as tmp:
            repo = Path(tmp) / "shop_monorepo"
            shutil.copytree(APP, repo)
            git = ["git", "-C", str(repo), "-c", "user.name=demo", "-c", "user.email=demo@example.invalid"]
            subprocess.run(git[:3] + ["init", "-q"], check=True)
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["commit", "-qm", "base: synthetic shop monorepo"], check=True)
            path, old, new = CHANGE
            src = repo / path
            src.write_text(src.read_text().replace(old, new, 1))

            seed(db, a.graph)
            r = review(repo, "HEAD", db=db, service_graph=a.graph, simulate="checkout-api")
    except CodeGraphError as e:
        print(f"ERROR (no verdict produced): {e}", file=sys.stderr)
        return 3
    finally:
        if a.graph in db.list_graphs():
            db.select_graph(a.graph).delete()
        db.connection.delete(f"telemetry{{{a.graph}}}")

    md = render_markdown(r)
    (out / "report.md").write_text(md)
    (out / "report.json").write_text(__import__("json").dumps(r, indent=2, default=str))
    (out / "change.diff").write_text(r["diff"])
    print(md)
    print(f"Wrote {out / 'report.md'}, {out / 'report.json'}, {out / 'change.diff'}", file=sys.stderr)
    return RANK[r["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
