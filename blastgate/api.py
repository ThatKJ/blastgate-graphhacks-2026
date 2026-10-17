"""BlastGate HTTP API (FastAPI).

Run: uvicorn blastgate.api:app --port 8000
Deterministic routes (/api/analyze, /api/simulate) need only FalkorDB. /api/agent/investigate needs
AGENT_PROVIDER=groq + GROQ_API_KEY, or AGENT_PROVIDER=deterministic for the labelled rule-based demo.
"""
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import agent as AG
from . import memory as M
from . import tools as T
from .analysis import AnalysisError, analyze
from .graph import SCENARIOS, SYNTHETIC, graph_name

log = logging.getLogger("blastgate.api")
DEFAULT_MODEL = "openai/gpt-oss-20b"
WEB_DIR = AG.ROOT / "web"
# The console renders model-written text; a strict CSP is defence in depth on top of escaping.
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
CSP_EXEMPT = ("/docs", "/redoc", "/openapi.json")   # Swagger UI needs its CDN and inline scripts
CODEGRAPH_REPORT_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Settings:
    graph_name: str
    provider: str | None          # "groq" | "deterministic" | None
    model: str
    provider_timeout_s: float
    mcp_timeout_s: float
    max_tool_calls: int
    live_token: str | None = None          # AGENT_LIVE_TOKEN: required for remote live investigations
    live_per_client_minute: int = 2
    live_per_minute: int = 3
    live_per_day: int = 60
    codegraph_report: Path | None = None   # BLASTGATE_CODEGRAPH_REPORT: a pre-generated codegraph review JSON
    memory_graph: str | None = None        # BLASTGATE_MEMORY_GRAPH: opt-in investigation memory (separate graph)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(graph_name=graph_name(), memory_graph=M.graph_name(),
                   codegraph_report=Path(os.getenv("BLASTGATE_CODEGRAPH_REPORT")
                                         or AG.ROOT / "codegraph_demo_out" / "report.json"),
                   provider=(os.getenv("AGENT_PROVIDER") or "").strip().lower() or None,
                   model=os.getenv("AGENT_MODEL") or DEFAULT_MODEL,
                   provider_timeout_s=float(os.getenv("AGENT_TIMEOUT_S", "30")),
                   mcp_timeout_s=float(os.getenv("MCP_TIMEOUT_S", "30")),
                   max_tool_calls=min(int(os.getenv("AGENT_MAX_TOOL_CALLS", "6")), AG.MAX_TOOL_CALLS),
                   live_token=os.getenv("AGENT_LIVE_TOKEN") or None,
                   live_per_client_minute=max(1, int(os.getenv("AGENT_LIVE_MAX_PER_CLIENT_MINUTE", "2"))),
                   live_per_minute=max(1, int(os.getenv("AGENT_LIVE_MAX_PER_MINUTE", "3"))),
                   live_per_day=max(1, int(os.getenv("AGENT_LIVE_MAX_PER_DAY", "60"))))

    @property
    def live_access(self) -> str | None:
        """How live (paid / quota-limited) investigations are gated; None when no live provider is configured."""
        if self.provider != "groq":
            return None
        return "token" if self.live_token else "localhost_only"

    def provider_status(self) -> tuple[bool, str]:
        if self.provider == "groq":
            return (True, f"groq/{self.model}") if os.getenv("GROQ_API_KEY") else \
                (False, "AGENT_PROVIDER=groq but GROQ_API_KEY is not set")
        if self.provider == "deterministic":
            return True, "deterministic-demo (rule-based, no LLM)"
        if self.provider:
            return False, f"unsupported AGENT_PROVIDER {self.provider!r}; use 'groq' or 'deterministic'"
        return False, "AGENT_PROVIDER is not set; set AGENT_PROVIDER=groq and GROQ_API_KEY"

    def planner(self):
        ok, detail = self.provider_status()
        if not ok:
            raise AG.ProviderNotConfigured(detail)
        if self.provider == "groq":
            return AG.GroqPlanner(self.model, timeout_s=self.provider_timeout_s)
        return AG.DeterministicPlanner()


# --- Live-provider protection ------------------------------------------------------------------

class LiveLimiter:
    """Sliding-window caps on live investigations: per client per minute, global per minute, global per day.
    ponytail: in-process memory; with several workers or replicas the caps multiply. Move to a shared
    store (the FalkorDB/Redis server would do) before running more than one process."""

    def __init__(self, settings: Settings):
        self.rules = [("client", settings.live_per_client_minute, 60), ("global", settings.live_per_minute, 60),
                      ("global", settings.live_per_day, 86_400)]
        self.hits: dict = defaultdict(deque)

    def check(self, client: str) -> int | None:
        """Record a hit and return None, or return the seconds to wait without recording anything."""
        now, keys, waits = time.monotonic(), [], []
        for scope, limit, window in self.rules:
            q = self.hits[(scope, client if scope == "client" else "*", window)]
            while q and now - q[0] >= window:
                q.popleft()
            keys.append(q)
            if len(q) >= limit:
                waits.append(int(window - (now - q[0])) + 1)
        if waits:
            return max(waits)
        for q in keys:
            q.append(now)
        return None


def live_access_denied(request: Request, token: str | None) -> tuple[int, str, str] | None:
    """None if this request may start a live investigation, else (status, error code, message)."""
    if token:
        given = request.headers.get("authorization", "")
        if hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
            return None
        return 401, "live_access_denied", "Live investigations need the server's access token (Authorization: Bearer)."
    # No token configured: this machine only. Any proxy header means the caller may be remote.
    proxied = "x-forwarded-for" in request.headers or "forwarded" in request.headers
    if not proxied and request.client and request.client.host in ("127.0.0.1", "::1"):
        return None
    return 403, "live_access_local_only", ("Live investigations are limited to the server's own machine. "
                                           "Set AGENT_LIVE_TOKEN on the server to allow remote access.")


# --- Schemas -----------------------------------------------------------------------------------

class PrRequest(BaseModel):
    pr_id: AG.PrIdStr
    model_config = ConfigDict(json_schema_extra={"examples": [{"pr_id": "PR-104"}]})


class SimulateRequest(PrRequest):
    consumer: T.ServiceId
    assumption: T.Assumption
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "pr_id": "PR-104", "consumer": "checkout-api",
        "assumption": "checkout-api adds a tolerant parser for the object-shaped discount_tier"}]})


class InvestigateBody(AG.InvestigateRequest):
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"pr_id": "PR-104", "question": "Is it safe to merge?"},
        {"pr_id": "PR-104", "simulate": {"consumer": "checkout-api",
                                         "assumption": "checkout-api adds a tolerant discount_tier parser"}}]})


class ScenarioOut(BaseModel):
    pr_id: str
    title: str
    producer: str
    contract_id: str
    field: str
    change: str
    new_type: str
    old_type_source: str = "graph (SchemaField.field_type), returned by /api/analyze and inspect_change"
    breaking: bool
    data_label: str = SYNTHETIC
    source: str = "declared by the PR (synthetic scenario)"
    code_change: dict[str, Any] | None = None   # provenance when the code graph derived this change from a diff


class Consumer(BaseModel):
    service: str
    tier: str | None
    fallback_enabled: Any
    evidence_source: str | None


class AffectedService(BaseModel):
    service: str
    tier: str | None
    depth: int
    path: list[str]


class Sla(BaseModel):
    service: str
    sla_id: str
    name: str | None
    penalty_per_hour_usd: float | None
    synthetic: bool | None


class Incident(BaseModel):
    incident_id: str
    severity: str | None
    summary: str | None
    synthetic: bool | None
    resolved_by: list[str]


class Reviewer(BaseModel):
    handle: str
    display_name: str | None
    resolved_incidents: list[str]
    authored_services: list[str]


class Exposure(BaseModel):
    penalty_per_hour_usd: float
    assumed_outage_hours: float
    assumed_total_usd: float
    label: str


class AnalyzeResponse(BaseModel):
    pr_id: str
    verdict: str
    reasons: list[str]
    summary: str                         # code-rendered, same renderer as the agent report
    inventory: dict[str, AG.Check]       # found / none_found / unknown / not_checked / check_failed
    data_label: str
    contract_found: bool
    field_found: bool
    mapping_status: str | None
    coverage_note: str | None
    consumers: list[Consumer]
    vulnerable_consumers: list[Consumer]
    affected_services: list[AffectedService]
    slas: list[Sla]
    exposure: Exposure | None
    centrality: dict[str, float] | None
    incidents: list[Incident] | None
    reviewers: list[Reviewer] | None


class SimulationSnapshot(BaseModel):
    verdict: str
    reasons: list[str]
    contract_found: bool
    field_found: bool
    mapping_status: str | None
    coverage_note: str | None
    consumers: list[Consumer]
    vulnerable_consumers: list[str]
    affected_services: list[AffectedService]
    slas: list[Sla]
    exposure: Exposure | None
    centrality: dict[str, float] | None


class SimulationChanges(BaseModel):
    consumer: str
    contract_id: str
    field: str
    relationship: str
    fallback_before: Any
    fallback_after: Any
    removed_vulnerable_consumers: list[str]
    remaining_vulnerable_consumers: list[str]
    removed_affected_services: list[str]
    remaining_affected_services: list[str]
    removed_slas: list[str]
    remaining_slas: list[str]


class SimulateResponse(BaseModel):
    pr_id: str
    label: str
    user_assumption: str
    modelled_mutation: str
    counterfactual: dict[str, Any]
    after_reasons: list[str]
    before: SimulationSnapshot
    after: SimulationSnapshot
    changes: SimulationChanges
    temp_graph_deleted: bool
    original_unchanged: bool
    code_level_correctness: str = "UNVERIFIED - requires contract or integration tests"


# What THIS process serves. The console reads it from /health, so a browser showing new static files against an
# older API process (static files are read from disk per request) is told to restart it instead of seeing a 404.
FEATURES = ("codegraph_report", "codegraph_scenarios", "investigation_memory")


class Health(BaseModel):
    status: str
    falkordb: str
    agent_provider: dict[str, Any]
    features: list[str] = []
    codegraph: dict[str, Any] = {}


class ErrorBody(BaseModel):
    error: str
    message: str
    retry_after: str | None = None
    partial_trace: list[AG.TraceEntry] | None = None


# --- App ---------------------------------------------------------------------------------------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="BlastGate", version="0.2.0",
                  description="Evidence-first change-impact analysis on FalkorDB (synthetic demo data).")
    app.state.settings = settings
    app.state.ctx = T.Ctx(settings.graph_name)   # lazy FalkorDB connection
    app.state.live_limiter = LiveLimiter(settings)
    app.state.tool_client_factory = lambda: AG.McpToolClient(settings.graph_name, settings.mcp_timeout_s)
    err = {r: {"model": ErrorBody} for r in (404, 429, 500, 502, 503, 504)}

    def ctx(request: Request) -> T.Ctx:
        return request.app.state.ctx

    def scenario(pr_id: str):
        if pr_id not in SCENARIOS:
            raise HTTPException(404, detail={"error": "unknown_scenario",
                                             "message": f"Unknown PR {pr_id}; see GET /api/scenarios"})
        return SCENARIOS[pr_id]

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not request.url.path.startswith(CSP_EXEMPT):
            response.headers["Content-Security-Policy"] = CSP
        log.info("request method=%s path=%s status=%s ms=%d", request.method, request.url.path,
                 response.status_code, (time.monotonic() - t0) * 1000)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_, e: HTTPException):
        body = e.detail if isinstance(e.detail, dict) else {"error": "http_error", "message": str(e.detail)}
        return JSONResponse(body, status_code=e.status_code, headers=e.headers)

    @app.exception_handler(AnalysisError)
    @app.exception_handler(redis.exceptions.RedisError)
    async def graph_error(_, e: Exception):
        unavailable = isinstance(e, redis.exceptions.ConnectionError) or \
            isinstance(e.__cause__, redis.exceptions.ConnectionError)
        log.warning("graph error type=%s cause=%s", type(e).__name__, type(e.__cause__).__name__)
        return JSONResponse({"error": "graph_unavailable" if unavailable else "analysis_failed",
                             "message": "FalkorDB is unreachable." if unavailable else
                             "Graph analysis failed; no verdict produced."}, status_code=503 if unavailable else 500)

    @app.exception_handler(Exception)
    async def unexpected(_, e: Exception):
        log.error("unhandled error type=%s", type(e).__name__)
        return JSONResponse({"error": "internal_error", "message": "Unexpected server error."}, status_code=500)

    @app.get("/health", response_model=Health, summary="Liveness plus dependency status (always 200)")
    def health(c: T.Ctx = Depends(ctx)):
        try:
            c.db.connection.ping()
            db = "up"
        except Exception:
            c._db, db = None, "down"   # reconnect lazily next time
        ok, detail = settings.provider_status()
        return Health(status="ok", falkordb=db, agent_provider={
            "name": settings.provider, "model": settings.model if settings.provider == "groq" else None,
            "configured": ok, "detail": detail, "live_access": settings.live_access},
            features=list(FEATURES), codegraph={
                "report_available": bool(settings.codegraph_report and settings.codegraph_report.is_file()),
                "registered": sorted(T.CODEGRAPH_PROVENANCE), "memory_enabled": bool(settings.memory_graph)})

    @app.get("/ready", summary="Readiness: 200 only when FalkorDB is reachable",
             responses={503: {"model": ErrorBody}})
    def ready(c: T.Ctx = Depends(ctx)):
        if health(c).falkordb != "up":
            return JSONResponse({"error": "graph_unavailable", "message": "FalkorDB is unreachable."}, 503)
        return {"status": "ready"}

    @app.get("/api/scenarios", response_model=list[ScenarioOut], summary="Synthetic demo PRs")
    def scenarios():
        return [ScenarioOut(pr_id=s.pr_id, title=s.title, producer=s.producer, contract_id=s.contract_id,
                            field=s.field, change=s.change, new_type=s.new_type, breaking=s.breaking,
                            source=s.source, code_change=T.CODEGRAPH_PROVENANCE.get(s.pr_id))
                for s in SCENARIOS.values()]

    @app.post("/api/analyze", response_model=AnalyzeResponse, responses=err,
              summary="Deterministic Milestone 1 analysis (no LLM)")
    def analyze_route(body: PrRequest, c: T.Ctx = Depends(ctx)):
        sc = scenario(body.pr_id)   # validate before touching the database
        r = analyze(c.graph, sc)
        d = r.as_dict()
        inv = AG.evidence_inventory(r, [])   # no agent tools ran: unrun lookups report 'not_checked'
        return AnalyzeResponse(pr_id=body.pr_id, verdict=d["verdict"], reasons=d["reasons"],
                               summary=AG.render_summary(d["verdict"], d["reasons"], inv), inventory=inv,
                               data_label=d["data_label"], contract_found=r.contract_found,
                               field_found=r.field_found, mapping_status=r.mapping_status,
                               coverage_note=r.coverage_note, consumers=r.consumers,
                               vulnerable_consumers=r.vulnerable_consumers, affected_services=r.affected_services,
                               slas=r.slas, exposure=r.exposure, centrality=r.centrality, incidents=r.incidents,
                               reviewers=r.reviewers)

    @app.post("/api/simulate", response_model=SimulateResponse, responses={400: {"model": ErrorBody}, **err},
              summary="Counterfactual on a temporary graph copy (never writes the primary graph)")
    def simulate_route(body: SimulateRequest, c: T.Ctx = Depends(ctx)):
        scenario(body.pr_id)
        res = T.simulate_mitigation(c, body.pr_id, body.consumer, body.assumption)
        if res.status == "error":
            status = {"invalid_target": 400, "invalid_input": 400, "unavailable": 503}.get(res.error_code, 500)
            raise HTTPException(status, detail={"error": res.error_code, "message": res.summary})
        return SimulateResponse(pr_id=body.pr_id, label=res.unknowns[0], **{
            k: res.data[k] for k in ("user_assumption", "modelled_mutation", "counterfactual", "after_reasons",
                                     "before", "after", "changes", "temp_graph_deleted", "original_unchanged")})

    @app.post("/api/agent/investigate", response_model=AG.InvestigationReport, responses=err,
              summary="Evidence-driven agent over MCP tools; verdict stays deterministic")
    async def investigate_route(body: InvestigateBody, request: Request, c: T.Ctx = Depends(ctx)):
        scenario(body.pr_id)
        try:
            planner = settings.planner()
        except AG.ProviderNotConfigured as e:
            raise HTTPException(503, detail={"error": "agent_provider_not_configured", "message": str(e)})
        if settings.live_access:   # a paid / quota-limited provider call would follow: gate it server-side
            denied = live_access_denied(request, settings.live_token)
            if denied:
                log.warning("live investigation refused code=%s", denied[1])
                raise HTTPException(denied[0], detail={"error": denied[1], "message": denied[2]})
            wait = app.state.live_limiter.check(request.client.host if request.client else "unknown")
            if wait:   # checked only after access passes, so refused callers can't spend the budget
                raise HTTPException(429, headers={"Retry-After": str(wait)}, detail={
                    "error": "live_rate_limited", "retry_after": str(wait),
                    "message": f"This server's live investigation limit was reached; retry in {wait} s."})
        sc, history, recall_error = SCENARIOS[body.pr_id], None, None
        if settings.memory_graph:   # history is advisory: a recall failure is reported, never fatal
            try:
                history = M.recall(c.db, settings.memory_graph, sc.contract_id, sc.field)
            except Exception as e:
                log.warning("memory recall failed type=%s", type(e).__name__)
                recall_error = type(e).__name__
        used = {}   # the exact deterministic report the verdict came from, reused for the memory record

        def verdict_fn(pr):
            used["report"] = analyze(c.graph, SCENARIOS[pr])
            return used["report"]
        try:
            inv = await AG.investigate(
                body, planner, app.state.tool_client_factory(),
                verdict_fn=verdict_fn,
                known_services=lambda names: T.known_services(c, names),
                max_tool_calls=settings.max_tool_calls, history=history)
        except AG.ProviderError as e:
            status = {"provider_rate_limited": 429, "provider_timeout": 504}.get(e.code, 502)
            log.warning("provider error code=%s", e.code)
            return JSONResponse(ErrorBody(error=e.code, message=str(e), retry_after=e.retry_after,
                                          partial_trace=e.trace).model_dump(),
                                status_code=status, headers={"Retry-After": e.retry_after} if e.retry_after else None)
        except AG.ToolServerError as e:
            raise HTTPException(503, detail={"error": "tool_server_unavailable", "message": str(e)})
        if settings.memory_graph:
            inv.memory = remember(c, sc, inv, used["report"], history, recall_error, planner.mode)
        return inv

    def remember(c, sc, inv, report, history, recall_error, mode) -> dict:
        """Revalidate recalled history against the current report, then record this investigation."""
        mem = {"graph": settings.memory_graph, "note": M.NOTE, "recorded_id": None,
               "planner_use": ("Given to the live model as context; it must re-check facts with tools."
                               if mode.startswith("llm") else
                               "The rule-based demo planner does not read history; code revalidates it below."),
               "prior_investigations": [], "code_reviews": [], "revalidation": []}
        if recall_error:
            mem.update(status="recall_failed", error=recall_error)
            history = {"investigations": [], "code_reviews": []}
        else:
            mem.update(prior_investigations=history["investigations"], code_reviews=history["code_reviews"],
                       revalidation=M.revalidate(history, report))
        prov = T.CODEGRAPH_PROVENANCE.get(sc.pr_id)
        review_id = M.code_review_id(prov["base"], prov["symbol"], prov["field"]) if prov else None
        try:
            mem["recorded_id"] = M.record_investigation(c.db, settings.memory_graph, inv, report, sc, history,
                                                        review_id)
            mem.setdefault("status", "recorded")
        except Exception as e:   # the investigation result stands; only the memory write is reported as failed
            log.warning("memory record failed type=%s", type(e).__name__)
            mem.update(status="record_failed", error=type(e).__name__)
        return mem

    @app.get("/api/codegraph/report", responses={404: {"model": ErrorBody}, 500: {"model": ErrorBody}},
             summary="The latest pre-generated code-graph review (read-only; no LLM, no subprocess)")
    def codegraph_report():
        # Serves ONE server-configured file. It takes no caller input: no path, repo, ref or command, so a
        # visitor can never make the server read other files or run git/Gristle. Generate it with
        # scripts/codegraph_demo.py or `python -m blastgate.codegraph ... --out`.
        path = settings.codegraph_report
        invalid = HTTPException(500, detail={"error": "invalid_codegraph_report",
                                             "message": "The configured code-graph report is not a valid review."})
        if path is None or not path.is_file():
            raise HTTPException(404, detail={"error": "no_codegraph_report",
                                             "message": "No code-graph review has been generated yet. "
                                                        "Run scripts/codegraph_demo.py."})
        try:
            if path.stat().st_size > CODEGRAPH_REPORT_MAX_BYTES:
                raise invalid
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            raise invalid from None
        if not (isinstance(data, dict) and data.get("action") == "review_report"
                and data.get("verdict") in ("BLOCK", "WARN", "CLEAR")
                and isinstance(data.get("contract_changes"), list)):
            raise invalid
        return data

    if WEB_DIR.is_dir():   # the console; mounted last so API routes always win
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
