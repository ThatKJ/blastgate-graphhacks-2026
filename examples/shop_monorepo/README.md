# shop_monorepo (SYNTHETIC)

A tiny fabricated monorepo used only to exercise BlastGate's code-graph review
(`python -m blastgate.codegraph`). It describes no real system.

- `contracts/pricing.py::PriceQuote` is the shared pricing API contract.
- `pricing_engine/` produces it, `checkout_api/` consumes `discount_tier`.
- `reporting/` also consumes it but is deliberately absent from `blastgate.map.json`.
- `catalog_service/badges.py::PromoBadge` has a field that is *also* named
  `discount_tier`; it is a different contract and must never be confused with it.
