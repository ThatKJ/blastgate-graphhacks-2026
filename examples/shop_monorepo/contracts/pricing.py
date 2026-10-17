"""Shared pricing API contract (pricing-api-v2). SYNTHETIC."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class PriceQuote:
    sku: str
    discount_tier: str
    final_price: Decimal
