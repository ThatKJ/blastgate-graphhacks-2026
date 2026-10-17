from decimal import Decimal

from contracts.pricing import PriceQuote

TIERS = {"gold": Decimal("0.20"), "silver": Decimal("0.10")}


def build_quote(sku: str, base: Decimal, tier: str) -> PriceQuote:
    return PriceQuote(sku=sku, discount_tier=tier, final_price=base * (1 - TIERS.get(tier, Decimal(0))))
