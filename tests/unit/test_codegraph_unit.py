"""Code-graph review logic against fake graphs: no FalkorDB, no Gristle, no LLM."""
import pytest

from blastgate import codegraph as CG
from blastgate import queries as Q

SYMBOL = "contracts/pricing.py::PriceQuote"
MAP = CG.load_map("""{
  "services": [{"service": "pricing-engine", "paths": ["pricing_engine/"]},
               {"service": "checkout-api", "paths": ["checkout_api/"]}],
  "contracts": [{"symbol": "contracts/pricing.py::PriceQuote", "producer": "pricing-engine",
                 "contract_id": "pricing-api-v2", "fields": {"discount_tier": "discount_tier"}}]}""")
VALID = {"pricing-engine", "checkout-api"}
DELTA = {"field": "discount_tier", "kind": "type_changed", "old_type": "str", "new_type": "dict"}


class FakeResult:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self, responses):
        self.responses = responses

    def ro_query(self, q, params=None):
        r = self.responses.get(q, [])
        return FakeResult(r(params) if callable(r) else r)


class FakeDB:
    def __init__(self, graphs):
        self.graphs = graphs

    def select_graph(self, name):
        return self.graphs[name]


def code_graph(users=None, callers=None):
    return FakeGraph({
        CG.CG_TYPE_USERS: users if users is not None else [
            ["checkout_api/pricing_client.py::parse_discount", "checkout_api/pricing_client.py", "ACCEPTS", False],
            ["pricing_engine/service.py::build_quote", "pricing_engine/service.py", "RETURNS", False]],
        CG.CG_CALLERS: callers if callers is not None else [
            ["checkout_api/routes.py::checkout", "checkout_api/routes.py", False, ["import", "file_scoped"],
             ["checkout_api/routes.py::checkout", "checkout_api/pricing_client.py::checkout_total",
              "checkout_api/pricing_client.py::parse_discount"]],
            ["checkout_api/tests/test_x.py::test_parse", "checkout_api/tests/test_x.py", True, ["import"],
             ["checkout_api/tests/test_x.py::test_parse", "checkout_api/pricing_client.py::parse_discount"]]],
        CG.CG_ROUTES: lambda p: [["POST", "/checkout", h] for h in p["names"] if h == "checkout_api/routes.py::checkout"],
    })


def service_graph(consumers=(("checkout-api", "critical", False, "synthetic: manifest"),), depends=True):
    return FakeGraph({
        Q.COVERAGE: [["pricing-engine", "pricing-api-v2", "complete", "note", "discount_tier", "string", True]],
        Q.CONSUMERS: [list(c) for c in consumers],
        Q.DOWNSTREAM: [["order-orchestrator", "critical", 1, ["order-orchestrator", "checkout-api"]]] if depends else [],
        Q.SLAS: lambda p: [[s, f"SLA-{s}", "n", 100, True] for s in p["services"]],
    })


def run(code=None, svc=None, cmap=MAP, delta=DELTA, valid=VALID):
    code = code or code_graph()
    inv = CG.Investigation(FakeDB({"svc": svc or service_graph(), "base": code, "head": code}), "svc", "base", "head")
    return CG.review_contract_change(inv, cmap, valid, SYMBOL, delta, None)


# --- Diff parsing and changed symbols -----------------------------------------------------------------------

def test_changed_lines_tracks_old_and_new_sides():
    diff = ("diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -9 +9 @@\n-x\n+y\n"
            "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,3 @@\n+a\n")
    assert CG.changed_lines(diff) == {"old": {"c.py": {9}}, "new": {"c.py": {9}, "n.py": {1, 2, 3}}}


def test_changed_symbols_uses_indexed_line_ranges():
    g = FakeGraph({CG.CG_SYMBOLS_IN_FILE: lambda p: [["Class", SYMBOL, 7, 10], ["Function", "other", 12, 14]]
                   if p["path"] == "contracts/pricing.py" else []})
    inv = CG.Investigation(FakeDB({"base": g, "head": g}), "svc", "base", "head")
    found, uncovered = CG.changed_symbols(inv, {"old": {"contracts/pricing.py": {9}},
                                                "new": {"contracts/pricing.py": {9}, "README.md": {1}}})
    assert found == {SYMBOL: {"kind": "Class", "file": "contracts/pricing.py", "sides": ["base", "head"]}}
    assert uncovered == ["new:README.md"]


def test_field_delta_compares_base_and_head_code_graphs():
    base = FakeGraph({CG.CG_TYPE_FIELDS: [["discount_tier", "str"], ["sku", "str"], ["old", "int"]]})
    head = FakeGraph({CG.CG_TYPE_FIELDS: [["discount_tier", "dict"], ["sku", "str"], ["new", "int"]]})
    inv = CG.Investigation(FakeDB({"base": base, "head": head}), "svc", "base", "head")
    assert [(d["field"], d["kind"]) for d in CG.field_delta(inv, SYMBOL)] == [
        ("discount_tier", "type_changed"), ("new", "added"), ("old", "removed")]


# --- Mapping ------------------------------------------------------------------------------------------------

def test_service_for_never_guesses():
    m = CG.load_map('{"services": [{"service": "a", "paths": ["shared/"]}, {"service": "b", "paths": ["shared/"]},'
                    ' {"service": "c", "paths": ["svc_c/"]}]}')
    assert m.service_for("shared/x.py", {"a", "b", "c"}) == ("ambiguous", None)
    assert m.service_for("svc_c/x.py", {"c"}) == ("mapped", "c")
    assert m.service_for("svc_c/x.py", set()) == ("invalid", "c")       # not a Service node
    assert m.service_for("svc_c_v2/x.py", {"c"}) == ("unmapped", None)  # prefix is a directory, not a string
    assert m.service_for("c/x.py", {"c"}) == ("unmapped", None)          # a dir named like the service is not proof


@pytest.mark.parametrize("text", [
    '{"services": [], "extra": 1}',
    '{"contracts": [{"symbol": "a.py::X", "producer": "p", "contract_id": "c", "fields": {}},'
    ' {"symbol": "a.py::X", "producer": "q", "contract_id": "d", "fields": {}}]}',
    '{"services": [{"service": "Bad Name", "paths": ["x/"]}]}',
])
def test_invalid_mapping_file_is_an_error_not_a_verdict(text):
    with pytest.raises(CG.CodeGraphError):
        CG.load_map(text)


# --- Graph structure drives the result -------------------------------------------------------------------------

def test_validated_mapping_reaches_downstream_service_risk():
    item = run()
    assert item["mapping"] == "validated" and item["verdict"] == "BLOCK"
    assert [a["service"] for a in item["service_graph"]["affected_services"]] == ["checkout-api", "order-orchestrator"]
    assert item["cross_check"]["corroborated"] == ["checkout-api"]
    route = item["code_impact"]["routes"][0]
    assert (route["path"], route["service"]) == ("/checkout", "checkout-api")
    assert ("run checkout_api/tests/test_x.py::test_parse (covers checkout_api/pricing_client.py::parse_discount)"
            in item["recommended_tests"])


def test_changed_symbol_reaches_its_actual_dependents():
    deps = {d["function"]: d for d in run()["code_impact"]["dependents"]}
    assert set(deps) == {"checkout_api/pricing_client.py::parse_discount", "pricing_engine/service.py::build_quote",
                         "checkout_api/routes.py::checkout"}
    assert deps["checkout_api/routes.py::checkout"]["confidence"] == "medium"   # weakest edge: import
    assert deps["checkout_api/routes.py::checkout"]["path"][0] == "checkout_api/pricing_client.py::parse_discount"


def test_removing_a_source_relationship_changes_source_impact():
    item = run(code=code_graph(callers=[]))
    assert "checkout_api/routes.py::checkout" not in {d["function"] for d in item["code_impact"]["dependents"]}
    assert item["code_impact"]["tests"] == [] and item["code_impact"]["routes"] == []


def test_removing_a_downstream_dependency_changes_service_impact():
    before, after = run(), run(svc=service_graph(depends=False))
    assert "order-orchestrator" in {a["service"] for a in before["service_graph"]["affected_services"]}
    assert {a["service"] for a in after["service_graph"]["affected_services"]} == {"checkout-api"}
    assert len(after["service_graph"]["slas"]) < len(before["service_graph"]["slas"])


def test_unmapped_contract_is_unknown_not_block():
    item = run(cmap=CG.CodeMap(services=MAP.services))
    assert item["mapping"] == "unmapped" and item["verdict"] == "WARN" and "service_graph" not in item


def test_mapping_to_a_contract_the_service_graph_denies_is_invalid():
    svc = FakeGraph({**service_graph().responses, Q.COVERAGE: []})   # producer does not EXPOSE the contract
    item = run(svc=svc)
    assert item["mapping"] == "invalid" and item["verdict"] == "WARN"


def test_ambiguous_mapping_never_becomes_a_verified_dependency():
    amb = CG.load_map(MAP.model_dump_json().replace('["pricing_engine/"]', '["pricing_engine/", "checkout_api/"]'))
    item = run(cmap=amb)
    assert item["cross_check"]["corroborated"] == []
    assert all(d["service"] is None for d in item["code_impact"]["dependents"] if d["file"].startswith("checkout_api/"))
    assert any("ambiguous" in u for u in item["uncertainties"])
    assert item["verdict"] == "BLOCK"   # the service graph still carries its own evidence


def test_code_only_dependency_turns_service_clear_into_warn():
    # Service graph: checkout-api has an evidenced fallback (CLEAR). Code graph: a mapped service with no
    # CONSUMES_FIELD edge also handles the type -> unverified, so CLEAR is downgraded, never kept.
    m = CG.load_map(MAP.model_dump_json().replace(
        '"services":[', '"services":[{"service":"invoice-worker","paths":["invoice/"]},'))
    code = code_graph(users=[["invoice/x.py::bill", "invoice/x.py", "ACCEPTS", False]], callers=[])
    item = run(code=code, svc=service_graph(consumers=[("checkout-api", "critical", True, "ev")]), cmap=m,
               valid=VALID | {"invoice-worker"})
    assert item["cross_check"]["code_only"] == ["invoice-worker"]
    assert item["verdict"] == "WARN"


def test_code_evidence_cannot_relax_a_block():
    item = run(code=code_graph(users=[], callers=[]))
    assert item["verdict"] == "BLOCK" and item["code_impact"]["dependents"] == []


# --- Failure handling -------------------------------------------------------------------------------------------

def test_missing_engine_is_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv("BLASTGATE_GRISTLE_BIN", raising=False)
    with pytest.raises(CG.CodeGraphError, match="unavailable"):
        CG.index(tmp_path, "x")


def test_failing_engine_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BLASTGATE_GRISTLE_BIN", "/usr/bin/false")
    with pytest.raises(CG.CodeGraphError, match="exit 1"):
        CG.index(tmp_path, "x")


def test_cli_engine_failure_produces_no_verdict(monkeypatch, capsys):
    def boom(*a, **k):
        raise CG.CodeGraphError("Gristle ingest failed (exit 1)")
    monkeypatch.setattr(CG, "review", boom)
    assert CG.main(["repo", "--base", "HEAD"]) == 3
    out = capsys.readouterr()
    assert out.out == "" and "no verdict" in out.err


def test_option_like_base_ref_is_rejected(tmp_path):
    with pytest.raises(CG.CodeGraphError):
        CG.resolve_base(tmp_path, "--output=/tmp/x")


# --- Audit regressions (real local git, no Gristle, no FalkorDB) ----------------------------------------------

def git_repo(tmp_path, files, attrs=None):
    import subprocess
    repo = tmp_path / "r"
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    if attrs:
        (repo / ".gitattributes").write_text(attrs)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "base"], check=True)
    return repo, git


def test_base_snapshot_ignores_export_ignore_and_export_subst(tmp_path):
    # git archive would drop contracts/ (every field then looks 'added', i.e. non-breaking) and rewrite $Format$.
    repo, _ = git_repo(tmp_path, {"contracts/pricing.py": "x: str  # $Format:%H$\n"},
                       attrs="contracts/ export-ignore\n*.py export-subst\n")
    out = tmp_path / "snap"
    CG.export_tree(repo, CG.resolve_base(repo, "HEAD"), out)
    assert (out / "contracts/pricing.py").read_text() == "x: str  # $Format:%H$\n"


def test_diff_paths_survive_user_git_config(tmp_path):
    import subprocess
    repo, git = git_repo(tmp_path, {"contracts/pricing.py": "a\nb\n"})
    subprocess.run(git + ["config", "diff.noprefix", "true"], check=True)
    (repo / "contracts/pricing.py").write_text("a\nc\n")
    assert CG.changed_lines(CG.read_diff(repo, "HEAD")) == {"old": {"contracts/pricing.py": {2}},
                                                             "new": {"contracts/pricing.py": {2}}}


def test_subdirectory_of_a_repo_is_rejected(tmp_path):
    repo, _ = git_repo(tmp_path, {"svc/a.py": "x = 1\n"})

    class DB:
        def list_graphs(self):
            return ["svc"]
    with pytest.raises(CG.CodeGraphError, match="not a git repository root"):
        CG.review(repo / "svc", "HEAD", db=DB(), service_graph="svc")


def test_indexer_gets_minimal_env_with_dependency_network_check_disabled(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, env=kw["env"], shell=kw.get("shell", False))
        return type("R", (), {"returncode": 0, "stderr": "  1 non-fatal errors (first: x)\n"})()
    monkeypatch.setenv("BLASTGATE_GRISTLE_BIN", "/opt/gristle")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_must_not_leak")
    monkeypatch.setattr(CG.subprocess, "run", fake_run)
    assert CG.index(tmp_path, "rid") == ["1 non-fatal errors (first: x)"]
    assert seen["argv"] == ["/opt/gristle", "ingest", str(tmp_path), "--repo-id", "rid"] and not seen["shell"]
    assert seen["env"]["GRISTLE_DEPENDENCY_CHECK_ENABLED"] == "false"
    assert "GROQ_API_KEY" not in seen["env"]


def test_indexer_timeout_is_an_error(monkeypatch, tmp_path):
    def slow(*a, **k):
        raise CG.subprocess.TimeoutExpired("gristle", 300)
    monkeypatch.setenv("BLASTGATE_GRISTLE_BIN", "/opt/gristle")
    monkeypatch.setattr(CG.subprocess, "run", slow)
    with pytest.raises(CG.CodeGraphError, match="TimeoutExpired"):
        CG.index(tmp_path, "rid")


def test_cleanup_failure_is_reported_not_raised():
    class Down:
        def list_graphs(self):
            raise ConnectionError("down")
    assert CG._drop_code_graphs(Down(), ["gristle_x"]) is False


@pytest.mark.parametrize("argv", [[], ["repo"], ["repo", "--base", "HEAD", "--bogus"]])
def test_cli_usage_errors_never_look_like_a_verdict(argv, capsys):
    assert CG.main(argv) == CG.EXIT_ERROR   # argparse's own exit 2 would read as BLOCK


def test_cli_unexpected_exception_never_looks_like_warn(monkeypatch, capsys):
    monkeypatch.setattr(CG, "review", lambda *a, **k: 1 / 0)
    assert CG.main(["repo", "--base", "HEAD"]) == CG.EXIT_ERROR
    assert "ZeroDivisionError" in capsys.readouterr().err


def test_cli_rejects_report_paths_inside_repo_or_missing_dirs(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(CG, "review", lambda *a, **k: pytest.fail("must validate --out before reviewing"))
    assert CG.main([str(tmp_path), "--base", "HEAD", "--out", str(tmp_path / "report")]) == CG.EXIT_ERROR
    assert CG.main([str(tmp_path / "r"), "--base", "HEAD", "--out", str(tmp_path / "no" / "report")]) == CG.EXIT_ERROR


# --- Hand-off to the MCP agent (opt-in scenario registration) -------------------------------------------------

def saved_report(tmp_path, mutate=None):
    import json
    from pathlib import Path
    fx = Path(__file__).resolve().parents[2] / "web" / "tests" / "fixtures" / "codegraph_report.json"
    body = json.loads(fx.read_text())["body"]
    if mutate:
        mutate(body)
    p = tmp_path / "report.json"
    p.write_text(json.dumps(body))
    return p


def test_registration_is_off_unless_the_operator_opts_in(monkeypatch):
    monkeypatch.delenv("BLASTGATE_CODEGRAPH_REPORT", raising=False)
    scenarios = {}
    assert CG.register_report_scenarios(scenarios) == {} and scenarios == {}


def test_validated_change_becomes_an_investigable_scenario(monkeypatch, tmp_path):
    monkeypatch.setenv("BLASTGATE_CODEGRAPH_REPORT", str(saved_report(tmp_path)))
    scenarios = {"PR-104": object()}
    prov = CG.register_report_scenarios(scenarios)
    sc = scenarios["PR-900"]
    assert (sc.producer, sc.contract_id, sc.field, sc.breaking, sc.new_type) == (
        "pricing-engine", "pricing-api-v2", "discount_tier", True, "dict")
    assert sc.source == CG.CODEGRAPH_SOURCE and "PR-104" in scenarios
    assert prov["PR-900"]["symbol"] == "contracts/pricing.py::PriceQuote" and prov["PR-900"]["mapping"] == "validated"


def test_unmapped_changes_are_never_registered(monkeypatch, tmp_path):
    def unmap(body):
        body["contract_changes"][0]["mapping"] = "unmapped"
    monkeypatch.setenv("BLASTGATE_CODEGRAPH_REPORT", str(saved_report(tmp_path, unmap)))
    scenarios = {}
    assert CG.register_report_scenarios(scenarios) == {} and scenarios == {}


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(action="something_else"),
    lambda b: b["contract_changes"][0]["scenario"].update(producer="Bad Producer!"),
    lambda b: b["contract_changes"][0]["scenario"].update(breaking="yes"),
    lambda b: b["contract_changes"][0].pop("scenario"),
])
def test_malformed_report_registers_nothing(monkeypatch, tmp_path, mutate):
    monkeypatch.setenv("BLASTGATE_CODEGRAPH_REPORT", str(saved_report(tmp_path, mutate)))
    scenarios = {}
    assert CG.register_report_scenarios(scenarios) == {} and scenarios == {}


def test_registration_never_overrides_an_existing_scenario(monkeypatch, tmp_path):
    monkeypatch.setenv("BLASTGATE_CODEGRAPH_REPORT", str(saved_report(tmp_path)))
    existing = object()
    scenarios = {"PR-900": existing}
    assert CG.register_report_scenarios(scenarios) == {} and scenarios["PR-900"] is existing


def test_mcp_server_process_receives_the_same_report_setting(monkeypatch):
    from blastgate.agent import McpToolClient
    monkeypatch.setenv("BLASTGATE_CODEGRAPH_REPORT", "/srv/report.json")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_never_forwarded")
    env = McpToolClient("g").params.env
    assert env["BLASTGATE_CODEGRAPH_REPORT"] == "/srv/report.json" and "GROQ_API_KEY" not in env
