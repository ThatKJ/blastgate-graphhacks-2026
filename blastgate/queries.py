"""Parameterised, read-only Cypher used by the analysis. Every query runs via ro_query."""

# 1. Producer -> contract -> field, plus coverage metadata.
#    No row      => the producer does not expose this contract (scenario mismatch).
#    field null  => the contract exists but the target field is not modelled.
COVERAGE = """
MATCH (p:Service {name: $producer})-[:EXPOSES]->(c:Contract {contract_id: $contract})
OPTIONAL MATCH (c)-[:HAS_FIELD]->(f:SchemaField {name: $field})
RETURN p.name, c.contract_id, c.consumer_mapping_status, c.coverage_note, f.name, f.field_type, f.required
"""

# Every contract query below is anchored on the verified producer, so a same-named contract exposed by
# another service can never leak consumers, incidents or mitigations into this scenario.

# 2a. Every indexed consumer of the exact field (fallback evaluated in Python).
CONSUMERS = """
MATCH (:Service {name: $producer})-[:EXPOSES]->(:Contract {contract_id: $contract})-[:HAS_FIELD]->(f:SchemaField {name: $field})<-[r:CONSUMES_FIELD]-(s:Service)
RETURN s.name, s.tier, r.fallback_enabled, r.evidence_source
ORDER BY s.name
"""

# 2b. Services that transitively depend on the vulnerable consumers.
#     Only DEPENDS_ON edges are traversed, so length(p) is a pure service-to-service hop count.
DOWNSTREAM = """
MATCH p = (d:Service)-[:DEPENDS_ON*1..8]->(v:Service)
WHERE v.name IN $consumers
RETURN d.name, d.tier, length(p), [n IN nodes(p) | n.name]
"""

# 2c. SLAs attached to affected services. BACKS_SLA is never counted as a hop.
SLAS = """
MATCH (s:Service)-[:BACKS_SLA]->(sla:SLAContract)
WHERE s.name IN $services
RETURN s.name, sla.sla_id, sla.name, sla.penalty_per_hour_usd, sla.synthetic
ORDER BY sla.sla_id
"""

# 4. FalkorDB betweenness centrality over the service dependency graph, filtered to affected services.
#    algo.betweenness samples source nodes; with the default (time-based) seed, scores vary between
#    runs (documented, and observed: 0.0 vs 6.0 for the same node). We pin both knobs so results are
#    reproducible, and report scores as a sampled approximation, never as exact or as a verdict input.
CENTRALITY_SAMPLING = {"size": 32, "seed": 1}
CENTRALITY = """
CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON'],
                       samplingSize: $size, samplingSeed: $seed})
YIELD node, score
WITH node, score WHERE node.name IN $services
RETURN node.name, score
ORDER BY score DESC, node.name
"""

# 5a. Historical incidents caused by changes in this contract.
INCIDENTS = """
MATCH (i:Incident)-[:CAUSED_BY_CHANGE_IN]->(:Contract {contract_id: $contract})<-[:EXPOSES]-(:Service {name: $producer})
OPTIONAL MATCH (e:Engineer)-[:RESOLVED_INCIDENT]->(i)
RETURN i.incident_id, i.severity, i.summary, i.synthetic, collect(e.handle)
ORDER BY i.incident_id
"""

# 5b. Candidate reviewers: resolved an incident on this contract and/or committed to an affected service.
REVIEWERS = """
MATCH (e:Engineer)
OPTIONAL MATCH (e)-[:RESOLVED_INCIDENT]->(i:Incident)-[:CAUSED_BY_CHANGE_IN]->(:Contract {contract_id: $contract})
               <-[:EXPOSES]-(:Service {name: $producer})
WITH e, collect(DISTINCT i.incident_id) AS incidents
OPTIONAL MATCH (e)-[:AUTHORED_COMMITS]->(s:Service)
WHERE s.name IN $services
WITH e, incidents, collect(DISTINCT s.name) AS services
WHERE size(incidents) > 0 OR size(services) > 0
RETURN e.handle, e.display_name, incidents, services
ORDER BY size(incidents) DESC, size(services) DESC, e.handle
"""

# Counterfactual mitigation (write) - only ever run against a copied graph.
SET_FALLBACK = """
MATCH (:Service {name: $producer})-[:EXPOSES]->(:Contract {contract_id: $contract})-[:HAS_FIELD]->(:SchemaField {name: $field})
      <-[r:CONSUMES_FIELD]-(:Service {name: $consumer})
SET r.fallback_enabled = true, r.evidence_source = $evidence
RETURN count(r)
"""


def rows(graph, query: str, params: dict) -> list:
    return graph.ro_query(query, params).result_set
