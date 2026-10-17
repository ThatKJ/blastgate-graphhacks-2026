"""Record REAL backend responses as frontend test fixtures (no Groq calls).

Run from the repo root with FalkorDB up:   python web/tests/capture_fixtures.py
Every fixture comes from the actual FastAPI app, live FalkorDB and the real MCP server. Two are produced
by scripted planners (no LLM) because they can't be triggered on demand; their file names say so.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.pop("GROQ_API_KEY", None)
os.environ["AGENT_PROVIDER"] = "deterministic"

from fastapi.testclient import TestClient  # noqa: E402

from blastgate import agent as AG  # noqa: E402
from blastgate.api import Settings, create_app  # noqa: E402

OUT = Path(__file__).parent / "fixtures"
OUT.mkdir(exist_ok=True)
SIM = {"pr_id": "PR-104", "consumer": "checkout-api",
       "assumption": "checkout-api adds a backward-compatible fallback for discount_tier"}


def save(name, response):
    (OUT / f"{name}.json").write_text(json.dumps({"status": response.status_code, "body": response.json()},
                                                 indent=2, sort_keys=True) + "\n")
    print(f"{name}: HTTP {response.status_code}")


class DenyingPlanner(AG.DeterministicPlanner):
    """Scripted (no LLM): gathers real evidence, then writes a final text that denies the incidents found."""
    mode = "test: scripted planner that denies incidents"

    async def next(self, observations):
        step = await super().next(observations)
        if isinstance(step, AG.Finish):
            return AG.Finish(summary="No incidents have been associated with this contract, so merging is safe.",
                             recommended_verdict="CLEAR")
        return step


class RateLimited(AG.DeterministicPlanner):
    mode = "test: scripted rate limit"

    async def next(self, observations):
        raise AG.ProviderError("provider_rate_limited", "Groq rate limit reached (HTTP 429); retry after 7s.", "7")


def main():
    c = TestClient(create_app(), raise_server_exceptions=False)
    save("health", c.get("/health"))
    save("scenarios", c.get("/api/scenarios"))
    for pr in ("PR-104", "PR-108", "PR-112", "PR-114"):
        save(f"analyze_{pr}", c.post("/api/analyze", json={"pr_id": pr}))
    save("investigate_PR-104_demo", c.post("/api/agent/investigate", json={"pr_id": "PR-104"}))
    save("simulate_PR-104", c.post("/api/simulate", json=SIM))
    save("error_unknown_scenario", c.post("/api/analyze", json={"pr_id": "PR-999"}))
    save("error_validation", c.post("/api/simulate", json={**SIM, "assumption": "short"}))
    save("error_invalid_target", c.post("/api/simulate", json={**SIM, "consumer": "search-indexer"}))
    original = Settings.planner
    try:
        Settings.planner = lambda self: DenyingPlanner()
        save("investigate_PR-104_withheld.scripted", c.post("/api/agent/investigate", json={"pr_id": "PR-104"}))
        Settings.planner = lambda self: RateLimited()
        save("error_rate_limited.scripted", c.post("/api/agent/investigate", json={"pr_id": "PR-104"}))
    finally:
        Settings.planner = original
    os.environ.pop("AGENT_PROVIDER")
    save("error_agent_not_configured",
         TestClient(create_app(), raise_server_exceptions=False).post("/api/agent/investigate", json={"pr_id": "PR-104"}))
    os.environ["FALKORDB_PORT"] = "6390"   # nothing listens there
    save("error_graph_unavailable",
         TestClient(create_app(), raise_server_exceptions=False).post("/api/analyze", json={"pr_id": "PR-104"}))


if __name__ == "__main__":
    main()
