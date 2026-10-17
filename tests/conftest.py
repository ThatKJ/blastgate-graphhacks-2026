"""Shared fixtures for the Milestone 2 suites (Milestone 1 tests keep their own fixtures)."""
import uuid
from types import SimpleNamespace

import anyio
import pytest

from blastgate import mcp_server
from blastgate.graph import connect, seed


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def tool_specs():
    """The real MCP tool schemas, read in-process from the server object (no transport)."""
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in anyio.run(mcp_server.mcp.list_tools)]


@pytest.fixture(scope="session")
def live_db():
    try:
        db = connect()
        db.connection.ping()
    except Exception as e:
        pytest.skip(f"FalkorDB unreachable ({type(e).__name__}); live test NOT run")
    return db


@pytest.fixture
def scratch_graph(live_db):
    """A private freshly seeded graph; deleted afterwards."""
    name = f"blastgate_test_{uuid.uuid4().hex[:8]}"
    g = seed(live_db, name)
    yield g
    if name in live_db.list_graphs():
        g.delete()


def fake_report(verdict="BLOCK", reasons=("deterministic reason",), field_found=True, consumers=(),
                vulnerable=(), affected=(), slas=(), incidents=None, reviewers=None):
    """Stand-in for blastgate.analysis.Report in orchestrator unit tests (same attribute names)."""
    return SimpleNamespace(verdict=SimpleNamespace(value=verdict), decision=SimpleNamespace(reasons=tuple(reasons)),
                           data_label="SYNTHETIC - test", field_found=field_found,
                           consumers=[{"service": c} for c in consumers],
                           vulnerable_consumers=[{"service": c} for c in vulnerable],
                           affected_services=[{"service": a, "tier": t, "depth": d} for a, t, d in affected],
                           slas=[{"sla_id": i, "service": svc} for i, svc in slas],
                           incidents=None if incidents is None else [{"incident_id": i} for i in incidents],
                           reviewers=None if reviewers is None else [{"handle": h} for h in reviewers])
