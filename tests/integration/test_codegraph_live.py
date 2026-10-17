"""Real Gristle indexing + real git diffs + real FalkorDB service graph. No LLM.

Skipped (with the reason shown) unless FalkorDB is reachable and BLASTGATE_GRISTLE_BIN points at a Gristle
executable. Skipped != passed. Every test uses its own seeded service graph and its own temporary code graphs.
"""
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from blastgate.codegraph import review
from blastgate.graph import fingerprint, seed

pytestmark = pytest.mark.integration
APP = Path(__file__).resolve().parents[2] / "examples" / "shop_monorepo"


@pytest.fixture
def svc(live_db):
    if not shutil.which(os.getenv("BLASTGATE_GRISTLE_BIN", "") or "\0"):
        pytest.skip("BLASTGATE_GRISTLE_BIN not set to a Gristle executable; code-graph live tests NOT run")
    name = f"blastgate_test_{uuid.uuid4().hex[:8]}"
    seed(live_db, name)
    yield name
    if name in live_db.list_graphs():
        live_db.select_graph(name).delete()


def make_repo(tmp_path, edit, base_edit=None) -> Path:
    """Copy the synthetic app, apply base_edit, commit as the base, then apply `edit` to the working tree."""
    repo = tmp_path / "app"
    shutil.copytree(APP, repo)
    if base_edit:
        base_edit(repo)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "base"], check=True)
    edit(repo)
    return repo


def replace(repo, path, old, new):
    p = repo / path
    assert old in p.read_text()
    p.write_text(p.read_text().replace(old, new))


def tier_to_dict(repo):
    replace(repo, "contracts/pricing.py", "discount_tier: str", "discount_tier: dict")


def run(live_db, svc, repo, **kw):
    return review(repo, "HEAD", db=live_db, service_graph=svc, **kw)


def only(report):
    assert len(report["contract_changes"]) == 1
    return report["contract_changes"][0]


def test_contract_field_change_blocks_via_code_and_service_graph(live_db, svc, tmp_path):
    before = fingerprint(live_db.select_graph(svc))
    r = run(live_db, svc, make_repo(tmp_path, tier_to_dict))
    item = only(r)
    assert r["verdict"] == "BLOCK" and item["mapping"] == "validated"
    assert (item["symbol"], item["field"], item["old_type"], item["new_type"]) == (
        "contracts/pricing.py::PriceQuote", "discount_tier", "str", "dict")
    deps = {d["function"]: d for d in item["code_impact"]["dependents"]}
    assert deps["checkout_api/pricing_client.py::parse_discount"]["service"] == "checkout-api"
    assert deps["reporting/revenue.py::tier_revenue"]["mapping"] == "unmapped"
    assert {x["path"] for x in item["code_impact"]["routes"]} == {"/checkout", "/v2/quote"}
    assert item["cross_check"]["corroborated"] == ["checkout-api"]
    sg = item["service_graph"]
    assert {a["service"] for a in sg["affected_services"]} >= {"checkout-api", "order-orchestrator", "invoice-worker"}
    assert [i["incident_id"] for i in sg["incidents"]] == ["INC-2041"]
    assert any("test_parse_discount_gold" in t for t in item["recommended_tests"])
    assert r["code_graphs_deleted"] and fingerprint(live_db.select_graph(svc)) == before
    json.dumps(r, default=str)


def test_removing_a_source_call_removes_its_test_from_the_impact(live_db, svc, tmp_path):
    def edit(repo):
        tier_to_dict(repo)
        replace(repo, "checkout_api/tests/test_pricing_client.py",
                'assert parse_discount(PriceQuote("sku-1", "gold", Decimal("10"))) == Decimal("0.20")',
                'assert PriceQuote("sku-1", "gold", Decimal("10")).sku == "sku-1"')
    item = only(run(live_db, svc, make_repo(tmp_path, edit)))
    assert not any("test_parse_discount_gold" in t["test"] for t in item["code_impact"]["tests"])


def test_same_named_field_on_another_type_is_not_the_contract(live_db, svc, tmp_path):
    r = run(live_db, svc, make_repo(tmp_path, lambda repo: replace(
        repo, "catalog_service/badges.py", "discount_tier: str", "discount_tier: int")))
    item = only(r)
    assert item["symbol"] == "catalog_service/badges.py::PromoBadge"
    assert item["mapping"] == "unmapped" and r["verdict"] == "WARN"
    assert "service_graph" not in item and "pricing-api-v2" not in json.dumps(r)


def test_ambiguous_mapping_does_not_corroborate_a_consumer(live_db, svc, tmp_path):
    def ambiguous(repo):
        replace(repo, "blastgate.map.json", '"paths": ["pricing_engine/"]', '"paths": ["pricing_engine/", "checkout_api/"]')
    item = only(run(live_db, svc, make_repo(tmp_path, tier_to_dict, base_edit=ambiguous)))
    assert item["cross_check"]["corroborated"] == []
    assert any("checkout_api" in u and "ambiguous" in u for u in item["uncertainties"])


def test_mapping_is_read_from_base_so_a_diff_cannot_unmap_itself(live_db, svc, tmp_path):
    def edit(repo):
        tier_to_dict(repo)
        (repo / "blastgate.map.json").write_text('{"services": [], "contracts": []}')
    r = run(live_db, svc, make_repo(tmp_path, edit))
    assert r["verdict"] == "BLOCK" and only(r)["mapping"] == "validated"


def test_no_mapping_file_means_unknown_not_block(live_db, svc, tmp_path):
    r = run(live_db, svc, make_repo(tmp_path, tier_to_dict,
                                    base_edit=lambda repo: (repo / "blastgate.map.json").unlink()))
    assert r["verdict"] == "WARN" and only(r)["mapping"] == "unmapped"


def test_removing_a_downstream_dependency_changes_the_impact(live_db, svc, tmp_path):
    repo = make_repo(tmp_path, tier_to_dict)
    before = only(run(live_db, svc, repo))["service_graph"]
    live_db.select_graph(svc).query(
        "MATCH (:Service {name: 'order-orchestrator'})-[r:DEPENDS_ON]->(:Service {name: 'checkout-api'}) DELETE r")
    after = only(run(live_db, svc, repo))["service_graph"]
    gone = {a["service"] for a in before["affected_services"]} - {a["service"] for a in after["affected_services"]}
    assert gone == {"order-orchestrator", "invoice-worker", "partner-gateway"}
    assert {s["sla_id"] for s in after["slas"]} == {"SLA-CHECKOUT-99.95"}


def test_counterfactual_is_reported_beside_the_unchanged_verdict(live_db, svc, tmp_path):
    r = run(live_db, svc, make_repo(tmp_path, tier_to_dict), simulate="checkout-api")
    cf = only(r)["counterfactual"]
    assert r["verdict"] == "BLOCK" and cf["production_verdict"] == "BLOCK"
    assert cf["graph_verdict_under_assumption"] == "CLEAR" and cf["verified_by_contract_or_integration_tests"] is False
    assert svc in live_db.list_graphs() and not any("__sim_" in g for g in live_db.list_graphs())


def test_function_only_change_lists_cross_service_callers(live_db, svc, tmp_path):
    r = run(live_db, svc, make_repo(tmp_path, lambda repo: replace(
        repo, "checkout_api/pricing_client.py", "quote.discount_tier.lower()", "quote.discount_tier.strip().lower()")))
    assert r["contract_changes"] == []
    impacted = {d["function"] for d in r["changed_functions_impact"]["dependents"]}
    assert {"checkout_api/pricing_client.py::parse_discount", "checkout_api/routes.py::checkout"} <= impacted
    assert any(t["test"].endswith("test_parse_discount_gold") for t in r["changed_functions_impact"]["tests"])
    assert r["verdict"] == "CLEAR"   # all callers stay inside checkout-api; nothing crosses a service boundary


def test_review_records_validated_changes_to_shared_memory(live_db, svc, tmp_path, monkeypatch):
    mem = f"blastgate_mem_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("BLASTGATE_MEMORY_GRAPH", mem)
    try:
        r = run(live_db, svc, make_repo(tmp_path, tier_to_dict))
        assert r["verdict"] == "BLOCK" and len(r["memory"]["recorded"]) == 1
        rows = live_db.select_graph(mem).ro_query(
            "MATCH (c:CodeReview)-[:DETECTED_CHANGE_TO]->(f:ContractField) "
            "OPTIONAL MATCH (c)-[:CODE_DEPENDENT_IN]->(s:Service) RETURN c.id, f.key, collect(s.name)").result_set
        assert rows == [[r["memory"]["recorded"][0], "pricing-api-v2.discount_tier", ["checkout-api"]]]
    finally:
        if mem in live_db.list_graphs():
            live_db.select_graph(mem).delete()
        live_db.connection.delete(f"telemetry{{{mem}}}")
