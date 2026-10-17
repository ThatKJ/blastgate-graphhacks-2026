"""Code-graph review: Gristle code graph -> validated mapping -> BlastGate service graph -> verdict -> report.

Gristle (MIT, github.com/Alchemy-Agentic/gristle) is the parser/indexer. It runs as a separate executable
(BLASTGATE_GRISTLE_BIN) because it pins mcp<2 while BlastGate pins mcp 2.x. It writes a code graph into the
same FalkorDB; everything here reads that graph with BlastGate's own parameterised ro_query Cypher.

Trust rules:
- Source entities reach the service graph ONLY through blastgate.map.json, read from the BASE commit (a PR
  cannot rewrite its own mapping) and validated against both graphs. Names and field strings are never
  matched: an unmapped, ambiguous or invalid mapping stays unknown.
- The verdict is the existing deterministic decide() over service-graph facts. Code evidence can only add
  caution (CLEAR -> WARN), never relax a verdict or create BLOCK on its own.
- Any indexing, git or query failure raises CodeGraphError. No data is never reported as safe.
- No LLM is involved. Both temporary code graphs are deleted afterwards.
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
import shutil

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import memory as M
from . import queries as Q
from .analysis import analyze
from .graph import SYNTHETIC, Scenario, connect, graph_name
from .simulation import diff_reports, simulate_consumer_fallback

MAP_FILE = "blastgate.map.json"
CODEGRAPH_SOURCE = "derived by the code graph from a git diff (blastgate.codegraph)"
REPORT_MAX_BYTES = 2_000_000
CODEGRAPH_PR_BASE = 900   # code-graph changes become PR-900, PR-901, ... (PR ids are pattern-checked everywhere)
ASSUMED_OUTAGE_HOURS = 2.0  # same assumption as the synthetic PR scenarios; exposure is labelled ASSUMED
RANK = {"CLEAR": 0, "WARN": 1, "BLOCK": 2}
EXIT_ERROR = 3       # never collides with a verdict exit code (argparse's own exit 2 is remapped too)
DIFF_LIMIT = 20_000  # characters of the diff embedded in the report
CONFIDENCE = {"exact": "high", "file_scoped": "high", "typed_receiver": "high",
              "import": "medium", "dotted": "medium", "same_file": "low", "unique_global": "low"}
KNOWN_GAPS = [
    "Gristle does not index attribute reads: an ACCEPTS/RETURNS link proves a function handles the contract "
    "type, not that it reads the changed field.",
    "HTTP/queue consumers in other repositories are invisible to the code graph; the service graph is the only "
    "source for them.",
]

# --- Code-graph queries (Gristle schema). Read-only, parameterised. ---------------------------------------
CG_SYMBOLS_IN_FILE = """
MATCH (n) WHERE (n:Function OR n:Class) AND n.file_path = $path
RETURN labels(n)[0], n.qualified_name, n.start_line, n.end_line
"""
CG_TYPE_FIELDS = """
MATCH (:Class {qualified_name: $symbol})-[:HAS_FIELD]->(f:TypeField)
RETURN f.name, f.type_annotation
"""
CG_TYPE_USERS = """
MATCH (fn:Function)-[r:ACCEPTS|RETURNS]->(:Class {qualified_name: $symbol})
RETURN fn.qualified_name, fn.file_path, type(r), coalesce(fn.is_test, false)
ORDER BY fn.qualified_name
"""
CG_CALLERS = """
MATCH p = (caller:Function)-[:CALLS*1..4]->(fn:Function)
WHERE fn.qualified_name IN $names AND NOT caller.qualified_name IN $names
RETURN caller.qualified_name, caller.file_path, coalesce(caller.is_test, false),
       [r IN relationships(p) | r.resolution], [n IN nodes(p) | n.qualified_name]
"""
CG_ROUTES = """
MATCH (r:Route)-[:HANDLES]->(fn:Function) WHERE fn.qualified_name IN $names
RETURN r.method, r.path, fn.qualified_name ORDER BY r.path
"""
CG_TESTS = """
MATCH (t)-[e:TESTS_FUNCTION]->(fn:Function) WHERE fn.qualified_name IN $names
RETURN coalesce(t.qualified_name, t.name), t.file_path, fn.qualified_name, e.depth
"""


class CodeGraphError(RuntimeError):
    """Indexing, git, mapping-file or query failure. Never converted into a verdict."""


# --- Mapping ----------------------------------------------------------------------------------------------
_SVC = r"^[a-z][a-z0-9-]{0,62}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServiceMap(_Strict):
    service: str = Field(pattern=_SVC)
    paths: list[str] = Field(min_length=1)


class ContractMap(_Strict):
    symbol: str = Field(min_length=3)       # Gristle qualified name, e.g. contracts/pricing.py::PriceQuote
    producer: str = Field(pattern=_SVC)
    contract_id: str = Field(min_length=1)
    fields: dict[str, str]                  # code field name -> service-graph SchemaField name


class CodeMap(_Strict):
    services: list[ServiceMap] = []
    contracts: list[ContractMap] = []

    def service_for(self, path: str, valid: set) -> tuple[str, str | None]:
        """(status, service). status: mapped | unmapped | ambiguous | invalid (service not in service graph)."""
        hits = {s.service for s in self.services for p in s.paths
                if path == p or (p.endswith("/") and path.startswith(p))}
        if not hits:
            return "unmapped", None
        if len(hits) > 1:
            return "ambiguous", None
        svc = hits.pop()
        return ("mapped" if svc in valid else "invalid"), svc

    def contract_for(self, symbol: str) -> ContractMap | None:
        return next((c for c in self.contracts if c.symbol == symbol), None)


def load_map(text: str | None) -> CodeMap:
    if text is None:
        return CodeMap()
    try:
        m = CodeMap.model_validate_json(text)
    except ValidationError as e:
        raise CodeGraphError(f"{MAP_FILE} is invalid: {e.error_count()} error(s)") from e
    symbols = [c.symbol for c in m.contracts]
    if len(symbols) != len(set(symbols)):
        raise CodeGraphError(f"{MAP_FILE} maps the same symbol twice; refusing an ambiguous contract mapping")
    return m


# --- Git ----------------------------------------------------------------------------------------------------
def _git(repo: Path, *args: str, binary=False, stdin: bytes | None = None):
    # argv list (no shell). Inherited GIT_* variables (GIT_DIR, GIT_WORK_TREE, ...) could redirect -C; drop them.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(repo), *args], input=stdin,
                           capture_output=True, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CodeGraphError(f"git {args[0]} could not run: {type(e).__name__}") from e
    if r.returncode != 0:
        raise CodeGraphError(f"git {args[0]} failed: {r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout if binary else r.stdout.decode()


def resolve_base(repo: Path, base: str) -> str:
    if not base or base.startswith("-"):
        raise CodeGraphError("invalid base ref")
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()


def export_tree(repo: Path, sha: str, dest: Path) -> None:
    """Write the commit's exact blobs to `dest`. Unlike `git archive`, .gitattributes export-ignore /
    export-subst cannot drop or rewrite files (a dropped contract would make every field look 'added')."""
    entries = []
    for rec in _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha, binary=True).split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            mode, kind, obj = meta.split()
            if kind == b"blob" and mode != b"120000":   # skip symlinks and submodules
                entries.append((obj, path.decode(errors="surrogateescape")))
    out = _git(repo, "cat-file", "--batch", binary=True, stdin=b"".join(o + b"\n" for o, _ in entries))
    root, pos = dest.resolve(), 0
    for obj, path in entries:
        nl = out.index(b"\n", pos)
        got, kind, size = out[pos:nl].split()
        if got != obj or kind != b"blob":
            raise CodeGraphError("git cat-file returned an unexpected object")
        pos = nl + 1 + int(size) + 1
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise CodeGraphError(f"refusing to export path outside the snapshot: {path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out[nl + 1:pos - 1])


def read_diff(repo: Path, sha: str) -> str:
    """Working tree vs `sha`. Every output-shaping option is pinned so user/repo git config (diff.noprefix,
    diff.relative, textconv, external diff, colour) cannot change the paths or line numbers parsed below."""
    return _git(repo, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-relative",
                "--src-prefix=a/", "--dst-prefix=b/", "-U0", sha, "--")


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def changed_lines(diff: str) -> dict:
    """{'old': {path: set(lines)}, 'new': {path: set(lines)}} from `git diff -U0 --no-renames`.
    A pure deletion/insertion (count 0) touches the line it sits next to."""
    out, old_path, new_path = {"old": {}, "new": {}}, None, None
    for line in diff.splitlines():
        if line.startswith("--- "):
            old_path = None if line[4:] == "/dev/null" else line[6:]
        elif line.startswith("+++ "):
            new_path = None if line[4:] == "/dev/null" else line[6:]
        elif (m := _HUNK.match(line)):
            for side, path, start, count in (("old", old_path, m[1], m[2]), ("new", new_path, m[3], m[4])):
                if path is None:
                    continue
                start, count = int(start), 1 if count is None else int(count)
                out[side].setdefault(path, set()).update(range(start, start + max(count, 1)))
    return out


# --- Indexing (Gristle) -------------------------------------------------------------------------------------
def index(src: Path, repo_id: str) -> list[str]:
    """Run Gristle's real ingestion pipeline on `src` into graph gristle_<repo_id>. Returns indexer warnings."""
    binary = os.getenv("BLASTGATE_GRISTLE_BIN")
    if not binary:
        raise CodeGraphError("BLASTGATE_GRISTLE_BIN is not set: the code-graph engine (Gristle) is unavailable")
    resolved_binary = str(Path(shutil.which(binary) or binary).resolve())
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "GRISTLE_FALKORDB_HOST": os.getenv("FALKORDB_HOST", "localhost"),
           "GRISTLE_FALKORDB_PORT": os.getenv("FALKORDB_PORT", "6379"),
           # Gristle's dependency checker would send the repo's package names to pypi.org / npm / osv.dev.
           "GRISTLE_DEPENDENCY_CHECK_ENABLED": "false"}
    if os.getenv("FALKORDB_PASSWORD"):
        env["GRISTLE_FALKORDB_PASSWORD"] = os.environ["FALKORDB_PASSWORD"]
    try:
        # A private empty cwd so Gristle's settings never pick up a stray .env file.
        with tempfile.TemporaryDirectory(prefix="blastgate-gristle-") as cwd:
            r = subprocess.run([resolved_binary, "ingest", str(src), "--repo-id", repo_id], capture_output=True, text=True,
                               timeout=300, env=env, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CodeGraphError(f"Gristle ingest could not run: {type(e).__name__}") from e
    if r.returncode != 0:
        raise CodeGraphError(f"Gristle ingest failed (exit {r.returncode}): {r.stderr.strip()[-200:]}")
    return [ln.strip() for ln in r.stderr.splitlines() if "non-fatal" in ln]


# --- Investigation ------------------------------------------------------------------------------------------
class Investigation:
    """Runs and records every graph query (graph, name, row count): the report's audit trail."""

    def __init__(self, db, service_graph: str, base_graph: str, head_graph: str):
        self.db, self.trace = db, []
        self.graphs = {"service": service_graph, "base": base_graph, "head": head_graph}

    def q(self, which: str, name: str, query: str, params: dict) -> list:
        rows = self.db.select_graph(self.graphs[which]).ro_query(query, params).result_set
        self.trace.append({"graph": which, "query": name, "rows": len(rows)})
        return rows


def changed_symbols(inv: Investigation, lines: dict) -> tuple[dict, list]:
    """{qualified_name: {kind, file, sides}} for code-graph symbols overlapping changed lines, plus the
    changed files whose changed lines contain no indexed symbol."""
    found, uncovered = {}, []
    for side, which in (("old", "base"), ("new", "head")):
        for path, nums in sorted(lines[side].items()):
            hit = False
            for kind, qn, start, end in inv.q(which, "symbols_in_file", CG_SYMBOLS_IN_FILE, {"path": path}):
                if start is not None and end is not None and nums & set(range(start, end + 1)):
                    hit = True
                    sides = found.setdefault(qn, {"kind": kind, "file": path, "sides": []})["sides"]
                    sides.append(which)
            if not hit:
                uncovered.append(f"{side}:{path}")
    return found, uncovered


def field_delta(inv: Investigation, symbol: str) -> list:
    """Contract-field changes, derived by comparing the class's TypeFields in the base and head code graphs."""
    base = dict(inv.q("base", "type_fields", CG_TYPE_FIELDS, {"symbol": symbol}))
    head = dict(inv.q("head", "type_fields", CG_TYPE_FIELDS, {"symbol": symbol}))
    out = []
    for name in sorted(base.keys() | head.keys()):
        if name not in head:
            out.append({"field": name, "kind": "removed", "old_type": base[name], "new_type": None})
        elif name not in base:
            out.append({"field": name, "kind": "added", "old_type": None, "new_type": head[name]})
        elif base[name] != head[name]:
            out.append({"field": name, "kind": "type_changed", "old_type": base[name], "new_type": head[name]})
    return out


def _confidence(resolutions: list) -> str:
    """A path is only as reliable as its weakest CALLS edge (Gristle's resolution labels)."""
    levels = {CONFIDENCE.get(r, "unknown") for r in resolutions}
    return next((lv for lv in ("unknown", "low", "medium") if lv in levels), "high")


def code_impact(inv: Investigation, which: str, roots: list, cmap: CodeMap, valid: set) -> dict:
    """Callers, routes and tests reachable from `roots` [{function, file, via}] in the `which` code graph."""
    def svc(path):
        status, service = cmap.service_for(path, valid)
        return {"mapping": status, "service": service}

    names = [r["function"] for r in roots]
    dependents = {r["function"]: {**r, **svc(r["file"]), "confidence": "type-level" if r["via"] != "changed"
                                  else "changed", "path": [r["function"]]} for r in roots}
    tests = {}
    if names:
        for qn, path, is_test, resolutions, chain in inv.q(which, "transitive_callers", CG_CALLERS,
                                                           {"names": names}):
            if is_test:
                tests.setdefault(qn, {"test": qn, "file": path, "covers": chain[-1], "via": "CALLS"})
                continue
            prev = dependents.get(qn)
            if prev is None or (prev["via"] == "CALLS" and len(chain) < len(prev["path"])):
                dependents[qn] = {"function": qn, "file": path, "via": "CALLS", **svc(path),
                                  "confidence": _confidence(resolutions), "resolutions": resolutions,
                                  "path": list(reversed(chain))}
    every = sorted(dependents)
    routes = [{"method": m, "path": p, "handler": h, "file": dependents[h]["file"], **svc(dependents[h]["file"])}
              for m, p, h in inv.q(which, "routes", CG_ROUTES, {"names": every})] if every else []
    for qn, path, covers, depth in (inv.q(which, "tests", CG_TESTS, {"names": every}) if every else []):
        tests[qn] = {"test": qn, "file": path, "covers": covers, "via": f"TESTS_FUNCTION depth {depth}"}
    return {"dependents": [dependents[k] for k in every], "routes": routes,
            "tests": sorted(tests.values(), key=lambda t: t["test"])}


def review_contract_change(inv: Investigation, cmap: CodeMap, valid: set, symbol: str, delta: dict,
                           simulate: str | None) -> dict:
    uncertainties, which = [], ("base" if delta["kind"] == "removed" else "head")
    users = inv.q(which, "type_users", CG_TYPE_USERS, {"symbol": symbol})
    roots = [{"function": qn, "file": path, "via": rel} for qn, path, rel, is_test in users if not is_test]
    impact = code_impact(inv, which, roots, cmap, valid)
    item = {"symbol": symbol, **delta, "breaking": delta["kind"] in ("removed", "type_changed"),
            "code_impact": impact}
    for d in impact["dependents"]:
        if d["mapping"] != "mapped":
            uncertainties.append(f"{d['function']} has a code dependency on {symbol} but its service mapping "
                                 f"is {d['mapping']}.")
        if d["confidence"] in ("low", "unknown"):
            uncertainties.append(f"{d['function']} is reached through a {d['confidence']}-confidence call edge.")

    contract = cmap.contract_for(symbol)
    sg_field = contract.fields.get(delta["field"]) if contract else None
    if sg_field is None:
        item.update(mapping="unmapped", verdict="WARN", recommended_tests=[
            f"run {t['test']} (covers {t['covers']})" for t in impact["tests"]],
            reasons=[f"{symbol}.{delta['field']} changed ({delta['kind']}) but is not mapped to a service-graph "
                     f"contract field in {MAP_FILE} at the base commit; service impact is unknown."],
            uncertainties=sorted(set(uncertainties)))
        return item

    sc = Scenario(pr_id=f"DIFF {symbol}.{delta['field']}", title=f"{delta['kind']} {symbol}.{delta['field']}",
                  producer=contract.producer, contract_id=contract.contract_id, field=sg_field,
                  change=f"{delta['kind']}: {delta['old_type']} -> {delta['new_type']}", breaking=item["breaking"],
                  local_tests="not run by BlastGate", assumed_outage_hours=ASSUMED_OUTAGE_HOURS,
                  new_type=delta["new_type"] or "removed", source=CODEGRAPH_SOURCE)
    report = analyze(inv.db.select_graph(inv.graphs["service"]), sc)
    inv.trace.append({"graph": "service", "query": "analysis.analyze: coverage, consumers, downstream, slas, "
                      "centrality, incidents, reviewers", "rows": len(report.consumers)})
    mapping = "validated" if report.contract_found and report.field_found and contract.producer in valid \
        else "invalid"
    if mapping == "invalid":
        uncertainties.append(f"{MAP_FILE} maps {symbol} to {contract.producer}/{contract.contract_id}.{sg_field}, "
                             "which the service graph does not confirm.")

    graph_consumers = {c["service"] for c in report.consumers}
    code_services = {d["service"] for d in impact["dependents"]
                     if d["mapping"] == "mapped" and d["service"] != contract.producer}
    for s in sorted(code_services - graph_consumers):
        uncertainties.append(f"{s} has a type-level code dependency on {symbol} but no CONSUMES_FIELD edge to "
                             f"{contract.contract_id}.{sg_field} in the service graph: unverified.")
    verdict, reasons = report.verdict.value, list(report.decision.reasons)
    if verdict == "CLEAR" and uncertainties:
        verdict = "WARN"
        reasons.append("Service graph alone is CLEAR, but code-graph evidence is unresolved (see uncertainties).")

    sg = report.as_dict()
    item.update(
        mapping=mapping, scenario=sc.__dict__, verdict=verdict, reasons=reasons,
        cross_check={"corroborated": sorted(code_services & graph_consumers),
                     "code_only": sorted(code_services - graph_consumers),
                     "service_graph_only": sorted(graph_consumers - code_services)},
        service_graph={k: sg[k] for k in ("mapping_status", "coverage_note", "consumers", "affected_services",
                                          "slas", "exposure", "centrality", "incidents", "reviewers")},
        recommended_tests=[f"run {t['test']} (covers {t['covers']})" for t in impact["tests"]]
        + [f"add/run a contract test: {c['service']} consuming {sc.contract_id}.{sg_field} as {sc.new_type}"
           for c in report.vulnerable_consumers],
        uncertainties=sorted(set(uncertainties)),
    )
    if simulate:
        item["counterfactual"] = _simulate(inv, sc, simulate)
    return item


def _simulate(inv: Investigation, sc: Scenario, consumer: str) -> dict:
    """Existing GRAPH.COPY counterfactual. Reported beside, never instead of, the production verdict."""
    try:
        sim = simulate_consumer_fallback(inv.db, inv.graphs["service"], sc, consumer)
    except ValueError:
        return {"status": "invalid_target",
                "summary": f"{consumer} is not an indexed consumer of {sc.contract_id}.{sc.field}"}
    if not (sim.temp_graph_deleted and sim.original_unchanged):
        raise CodeGraphError("simulation containment could not be verified; result discarded")
    return {"status": "ok", **sim.after.counterfactual, "changes": diff_reports(sc, consumer, sim.before, sim.after)}


def review(repo: Path, base: str, *, db=None, service_graph: str | None = None, simulate: str | None = None) -> dict:
    """Review the working tree of `repo` against `base`. Raises CodeGraphError on any failure."""
    repo = Path(repo).resolve()
    try:
        db = db or connect()
        service_graph = service_graph or graph_name()
        if service_graph not in db.list_graphs():
            raise CodeGraphError(f"service graph '{service_graph}' does not exist; seed it first")
        top = Path(_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
        if top != repo:   # diff paths are root-relative, Gristle paths are relative to the indexed directory
            raise CodeGraphError(f"{repo} is not a git repository root (root: {top})")
        sha = resolve_base(repo, base)
        has_map = _git(repo, "ls-tree", "--full-tree", "--name-only", sha, "--", MAP_FILE).strip()
        cmap = load_map(_git(repo, "show", f"{sha}:{MAP_FILE}") if has_map else None)
        diff = read_diff(repo, sha)
        lines = changed_lines(diff)
    except CodeGraphError:
        raise
    except Exception as e:
        raise CodeGraphError(f"setup failed: {type(e).__name__}") from e

    run = uuid.uuid4().hex[:10]
    ids = {"base": f"bgrev_{run}_base", "head": f"bgrev_{run}_head"}
    names = {k: f"gristle_{v}" for k, v in ids.items()}
    try:
        with tempfile.TemporaryDirectory(prefix="blastgate-base-") as tmp:
            export_tree(repo, sha, Path(tmp))
            warnings = index(Path(tmp), ids["base"]) + index(repo, ids["head"])
        for name in names.values():
            if name not in db.list_graphs():
                raise CodeGraphError(f"code graph {name} was not created by the indexer")
        inv = Investigation(db, service_graph, names["base"], names["head"])
        declared = sorted({s.service for s in cmap.services} | {c.producer for c in cmap.contracts})
        valid = {r[0] for r in inv.q("service", "services_exist", Q.SERVICES_EXIST, {"services": declared})}
        symbols, uncovered = changed_symbols(inv, lines)
        items = [review_contract_change(inv, cmap, valid, qn, d, simulate)
                 for qn, s in sorted(symbols.items()) if s["kind"] == "Class" for d in field_delta(inv, qn)]
        functions = [{"function": qn, "file": s["file"], "via": "changed"} for qn, s in sorted(symbols.items())
                     if s["kind"] == "Function" and "head" in s["sides"]]
        fn_impact = code_impact(inv, "head", functions, cmap, valid)
    except CodeGraphError:
        raise
    except Exception as e:  # redis/falkordb errors, malformed results
        raise CodeGraphError(f"code-graph investigation failed: {type(e).__name__}: {e}") from e
    finally:
        deleted = _drop_code_graphs(db, names.values())

    general = [f"Changed lines in {f} are not covered by any indexed code symbol." for f in uncovered]
    general += [f"Indexer: {w}" for w in warnings]
    memory = None
    if M.graph_name():   # opt-in shared memory: the investigation agent can later build on these records
        memory = {"graph": M.graph_name(), "recorded": []}
        for i in (i for i in items if i.get("mapping") == "validated"):
            try:
                memory["recorded"].append(M.record_code_review(db, M.graph_name(), sha, i))
            except Exception as e:   # never affects the verdict; reported instead
                general.append(f"Memory: code review for {i['symbol']}.{i['field']} not recorded ({type(e).__name__}).")
    changed_services = {cmap.service_for(f["file"], valid) for f in functions}
    for d in fn_impact["dependents"]:
        if d["via"] == "CALLS" and (d["mapping"] != "mapped" or (d["mapping"], d["service"]) not in changed_services):
            general.append(f"{d['function']} (service {d['service'] or '—'}, mapping {d['mapping']}) calls changed "
                           "code across a service or mapping boundary.")
    verdict = max([i["verdict"] for i in items] + (["WARN"] if general else ["CLEAR"]), key=RANK.__getitem__)
    reasons = [f"{i['symbol']}.{i['field']}: {r}" for i in items for r in i["reasons"]]
    if general:
        reasons.append("Code-graph uncertainties outside contract changes (see general uncertainties).")
    return {
        "action": "review_report", "verdict": verdict,
        "reasons": reasons or ["No contract-field change and no unresolved code dependency found in the diff."],
        "base": sha, "head": "working tree", "data_label": SYNTHETIC,
        "diff": diff if len(diff) <= DIFF_LIMIT else diff[:DIFF_LIMIT] + "\n[diff truncated]\n",
        "engine": "Gristle code graph + BlastGate service graph; deterministic, no LLM",
        "changed_symbols": [{"symbol": k, **v} for k, v in sorted(symbols.items())],
        "contract_changes": items, "changed_functions_impact": fn_impact,
        "general_uncertainties": sorted(set(general)), "known_gaps": KNOWN_GAPS,
        "investigation_trace": inv.trace,
        "code_graphs_deleted": deleted, "memory": memory,
    }


def _drop_code_graphs(db, names) -> bool:
    """Best-effort cleanup that never masks the real outcome. Only touches this run's uniquely named graphs."""
    try:
        for name in names:
            if name in db.list_graphs():
                db.select_graph(name).delete()
            db.connection.delete(f"telemetry{{{name}}}")
        return not (set(names) & set(db.list_graphs()))
    except Exception:
        return False


# --- Hand-off to the MCP agent -------------------------------------------------------------------------------
def report_path() -> Path | None:
    """The operator's BLASTGATE_CODEGRAPH_REPORT (relative paths resolve against the repo root), or None."""
    v = os.getenv("BLASTGATE_CODEGRAPH_REPORT")
    if not v:
        return None
    p = Path(v)
    return p if p.is_absolute() else Path(__file__).resolve().parent.parent / p


def register_report_scenarios(scenarios: dict) -> dict:
    """Opt-in (BLASTGATE_CODEGRAPH_REPORT set): add each VALIDATED contract change of a saved review as an
    investigable change (PR-900, ...), so the MCP tools, deterministic verdict and counterfactual run on what
    the code graph detected. Called by blastgate.tools at import, i.e. in both the API and the MCP server
    process. Unmapped/invalid changes are never registered. Any problem registers nothing (fail closed).
    Returns {pr_id: provenance}."""
    path = report_path()
    if path is None:
        return {}
    try:
        if path.stat().st_size > REPORT_MAX_BYTES:
            raise ValueError("report too large")
        r = json.loads(path.read_text())
        if r.get("action") != "review_report":
            raise ValueError("not a review report")
        fields = set(Scenario.__dataclass_fields__) - {"pr_id", "source"}
        out = {}
        for n, item in enumerate(c for c in r["contract_changes"] if c.get("mapping") == "validated"):
            pr = f"PR-{CODEGRAPH_PR_BASE + n}"
            raw = {k: v for k, v in item["scenario"].items() if k in fields}
            raw["title"] = (f"Git diff: {item['symbol'].rsplit('::', 1)[-1]}.{item['field']} "
                            f"{item['old_type']} → {item['new_type']}")
            sc = Scenario(pr_id=pr, source=CODEGRAPH_SOURCE, **raw)
            if pr in scenarios or not re.fullmatch(_SVC, sc.producer) or not isinstance(sc.breaking, bool):
                raise ValueError(f"refusing scenario {pr}")
            out[pr] = (sc, {"source": CODEGRAPH_SOURCE, "symbol": item["symbol"], "field": item["field"],
                            "kind": item["kind"], "old_type": item["old_type"], "new_type": item["new_type"],
                            "mapping": item["mapping"], "base": r["base"],
                            "code_dependents": len(item["code_impact"]["dependents"]),
                            "code_routes": len(item["code_impact"]["routes"]),
                            "uncertainties": len(item["uncertainties"])})
    except Exception as e:  # malformed operator file: register nothing rather than half a scenario set
        logging.getLogger("blastgate.codegraph").warning("code-graph report not registered: %s", type(e).__name__)
        return {}
    scenarios.update({pr: sc for pr, (sc, _) in out.items()})
    return {pr: prov for pr, (_, prov) in out.items()}


# --- Action: the review report -------------------------------------------------------------------------------
def render_markdown(r: dict) -> str:
    out = [f"# BlastGate code-graph review: **{r['verdict']}**", "",
           f"_{r['data_label']}_ · base `{r['base'][:12]}` → working tree · {r['engine']}", "", "## Why"]
    out += [f"- {x}" for x in r["reasons"]]
    out += ["", "## Actual git change", "```diff", r["diff"].rstrip() or "(empty diff)", "```",
            "", "Changed code-graph symbols: "
            + (", ".join(f"`{s['symbol']}` ({s['kind']})" for s in r["changed_symbols"]) or "none")]
    for i in r["contract_changes"]:
        out += ["", f"## `{i['symbol']}.{i['field']}`: {i['kind']} ({i['old_type']} → {i['new_type']})",
                f"Verdict **{i['verdict']}** · mapping **{i['mapping']}**", "", "### Source-code evidence (Gristle)"]
        out += [f"- `{d['function']}` via {d['via']} · confidence {d['confidence']} · "
                f"service {d['service'] or '—'} ({d['mapping']})" for d in i["code_impact"]["dependents"]]
        out += [f"- route {x['method']} {x['path']} → `{x['handler']}`" for x in i["code_impact"]["routes"]]
        if "service_graph" in i:
            sg = i["service_graph"]
            out += ["", "### Service-graph evidence (FalkorDB)", f"- coverage: {sg['mapping_status']}"]
            out += [f"- consumer {c['service']} · fallback {c['fallback_enabled']} · {c['evidence_source']}"
                    for c in sg["consumers"]]
            out += [f"- impact path {' → '.join(a['path'])} ({a['tier']})" for a in sg["affected_services"]]
            out += [f"- SLA {s['sla_id']} on {s['service']}" for s in sg["slas"]]
            out += [f"- incident {x['incident_id']} ({x['severity']}): {x['summary']}" for x in sg["incidents"] or []]
            if sg["exposure"]:
                out.append(f"- exposure: ${sg['exposure']['assumed_total_usd']:,.0f} ({sg['exposure']['label']})")
            out += ["", "### Cross-check (code graph vs service graph)"]
            out += [f"- {k}: {', '.join(v) or '—'}" for k, v in i["cross_check"].items()]
            out += ["", "### Reviewers"]
            out += [f"- {x['handle']} (incidents: {', '.join(x['resolved_incidents']) or '—'}; "
                    f"services: {', '.join(x['authored_services']) or '—'})" for x in sg["reviewers"] or []] or ["- —"]
        out += ["", "### Recommended tests", *[f"- {t}" for t in i["recommended_tests"] or ["—"]]]
        if i.get("counterfactual"):
            cf = i["counterfactual"]
            out += ["", "### Counterfactual (UNVERIFIED source-code mitigation, temporary graph copy)"]
            if cf["status"] == "ok":
                ch = cf["changes"]
                both = lambda k: ", ".join(ch[f"removed_{k}"] + ch[f"remaining_{k}"]) or "—"  # noqa: E731
                out += [f"- {cf['label']}", f"- {cf['assumption']}", "",
                        "| | real graph | under the assumption |", "|---|---|---|",
                        f"| verdict | {cf['production_verdict']} | {cf['graph_verdict_under_assumption']} |",
                        f"| {ch['consumer']} fallback | {ch['fallback_before']} | {ch['fallback_after']} |",
                        f"| vulnerable consumers | {both('vulnerable_consumers')} | "
                        f"{', '.join(ch['remaining_vulnerable_consumers']) or '—'} |",
                        f"| affected services | {both('affected_services')} | "
                        f"{', '.join(ch['remaining_affected_services']) or '—'} |",
                        f"| SLAs | {both('slas')} | {', '.join(ch['remaining_slas']) or '—'} |", "",
                        f"- {cf['production_recommendation']}"]
            else:
                out.append(f"- {cf['summary']}")
        out += ["", "### Uncertainties", *[f"- {u}" for u in i["uncertainties"] or ["—"]]]
    out += ["", "## General uncertainties", *[f"- {u}" for u in r["general_uncertainties"] or ["—"]],
            "", "## Known gaps", *[f"- {g}" for g in r["known_gaps"]],
            "", "## Investigation trace",
            *[f"- [{t['graph']}] {t['query']}: {t['rows']} row(s)" for t in r["investigation_trace"]]]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m blastgate.codegraph",
                                description="Review a local git diff with the code graph and the service graph. "
                                            "Exit: 0 CLEAR, 1 WARN, 2 BLOCK, 3 error (no verdict).")
    p.add_argument("repo")
    p.add_argument("--base", required=True, help="base commit/ref; the working tree is reviewed against it")
    p.add_argument("--simulate-fallback", metavar="SERVICE", help="counterfactual on a temporary graph copy")
    p.add_argument("--out", help="also write the report to OUT.md and OUT.json (outside the reviewed repo)")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:   # usage errors exit 2 in argparse, which would read as BLOCK
        return 0 if e.code == 0 else EXIT_ERROR

    def fail(msg):
        print(f"ERROR (no verdict produced): {msg}", file=sys.stderr)
        return EXIT_ERROR

    repo = Path(a.repo).resolve()
    outs = []
    if a.out:
        out = Path(a.out).resolve()
        outs = [out.with_suffix(".md"), out.with_suffix(".json")]
        if not out.parent.is_dir():
            return fail(f"--out directory does not exist: {out.parent}")
        if out.is_relative_to(repo):   # a report inside the reviewed tree would pollute the next review
            return fail("--out must be outside the reviewed repository")
        if any(o.is_symlink() for o in outs):
            return fail("--out target is a symlink")
    try:
        r = review(repo, a.base, simulate=a.simulate_fallback)
        md = render_markdown(r)
        for path, text in zip(outs, (md, json.dumps(r, indent=2, default=str))):
            path.write_text(text)
    except CodeGraphError as e:
        return fail(e)
    except Exception as e:  # a traceback's exit code 1 would read as WARN
        return fail(f"unexpected {type(e).__name__}")
    if not r["code_graphs_deleted"]:
        print("WARNING: temporary code graphs could not be confirmed deleted", file=sys.stderr)
    print(md)
    return RANK[r["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
