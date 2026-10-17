"""BlastGate MCP server (stdio). Exposes the typed domain tools in blastgate.tools - nothing else.

Run: python -m blastgate.mcp_server
The graph is fixed server-side from BLASTGATE_GRAPH; no tool accepts a graph name or Cypher.
"""
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import tools as T
from .graph import graph_name

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
CTX = T.Ctx(graph_name())

mcp = MCPServer(
    "blastgate",
    instructions=("Read-only change-impact evidence from a synthetic FalkorDB service graph. Results carry "
                  "graph evidence IDs. The BLOCK/WARN/CLEAR verdict is computed by the deterministic engine, "
                  "not by tools or models."),
    log_level="WARNING",
)


@mcp.tool(annotations=READ_ONLY)
def inspect_change(pr_id: T.PrId) -> T.ToolResult:
    """Validate a known PR's change target: producer, contract, field, old type (from graph) and declared new type."""
    return T.inspect_change(CTX, pr_id)


@mcp.tool(annotations=READ_ONLY)
def inspect_contract_coverage(pr_id: T.PrId) -> T.ToolResult:
    """Consumer-mapping status of the changed contract and every indexed consumer of the field, with fallback state."""
    return T.inspect_contract_coverage(CTX, pr_id)


@mcp.tool(annotations=READ_ONLY)
def trace_blast_radius(pr_id: T.PrId, max_depth: T.MaxDepth = 8) -> T.ToolResult:
    """Downstream services reached through consumers without fallback: paths, hop depth, attached synthetic SLAs."""
    return T.trace_blast_radius(CTX, pr_id, max_depth)


@mcp.tool(annotations=READ_ONLY)
def analyze_network_criticality(services: T.ServiceIds) -> T.ToolResult:
    """FalkorDB betweenness centrality for affected service IDs (sampled approximation; informational only)."""
    return T.analyze_network_criticality(CTX, services)


@mcp.tool(annotations=READ_ONLY)
def retrieve_incidents_and_reviewers(pr_id: T.PrId, services: T.ServiceIds) -> T.ToolResult:
    """Synthetic incidents caused by changes to this PR's contract, and engineers connected to them or to the services."""
    return T.retrieve_incidents_and_reviewers(CTX, pr_id, services)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def simulate_mitigation(pr_id: T.PrId, consumer: T.ServiceId, assumption: T.Assumption) -> T.ToolResult:
    """Counterfactual only: assume `consumer` gains a fallback, re-analyse on a temporary graph copy, verify the
    source graph is unchanged. Never proof that a code fix works."""
    return T.simulate_mitigation(CTX, pr_id, consumer, assumption)


if __name__ == "__main__":
    mcp.run("stdio")
