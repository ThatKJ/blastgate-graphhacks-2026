"""Deterministic three-state verdict. Pure Python: no database, no LLM.

The verdict is computed only from structured graph facts. Any future narrative layer
(LLM or otherwise) may explain a verdict but has no input into this function.
"""
from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    CLEAR = "CLEAR"


@dataclass(frozen=True)
class Facts:
    contract_found: bool              # producer EXPOSES contract, as the scenario claims
    field_found: bool
    mapping_status: str | None        # "complete" is the only value that counts as verified coverage
    breaking: bool
    consumers: tuple = ()             # ((name, fallback_enabled, evidence_source), ...)
    critical_affected: tuple = ()     # affected services with tier == "critical"
    sla_ids: tuple = ()


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reasons: tuple = field(default_factory=tuple)


def decide(f: Facts) -> Decision:
    if not f.contract_found:
        return Decision(Verdict.WARN, ("Producer/contract pair not found in graph; scenario mapping unverified.",))
    if not f.field_found:
        return Decision(Verdict.WARN, ("Target field is not modelled on the contract; impact cannot be resolved.",))

    vulnerable = [c for c in f.consumers if c[1] is False]
    unverified = [c for c in f.consumers if not isinstance(c[1], bool) or not c[2]]

    if f.breaking and vulnerable:
        if f.critical_affected or f.sla_ids:
            return Decision(Verdict.BLOCK, (
                f"Breaking change reaches consumer(s) without fallback: {', '.join(c[0] for c in vulnerable)}.",
                f"Affected critical services: {', '.join(f.critical_affected) or 'none'}; "
                f"SLAs: {', '.join(f.sla_ids) or 'none'}.",
            ))
        return Decision(Verdict.WARN, (
            f"Breaking change reaches consumer(s) without fallback ({', '.join(c[0] for c in vulnerable)}) "
            "but no critical service or SLA is affected; review required.",
        ))

    reasons = []
    if f.mapping_status != "complete":
        reasons.append(f"Consumer mapping is '{f.mapping_status}', not complete: "
                       "no indexed vulnerable consumer does not mean no consumer.")
    if unverified:
        reasons.append(f"Consumer evidence unverified for: {', '.join(c[0] for c in unverified)}.")
    if reasons:
        return Decision(Verdict.WARN, tuple(reasons))

    if not f.breaking:
        return Decision(Verdict.CLEAR, ("Change is not modelled as breaking and coverage is complete.",))
    if not f.consumers:
        return Decision(Verdict.CLEAR, ("Coverage complete and no consumer of this field is indexed "
                                        "(under modelled scenario assumptions).",))
    return Decision(Verdict.CLEAR, (
        f"Coverage complete; all {len(f.consumers)} indexed consumer(s) have an evidenced fallback "
        "(under modelled scenario assumptions).",
    ))
