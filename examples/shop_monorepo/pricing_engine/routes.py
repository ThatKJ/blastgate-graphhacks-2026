from decimal import Decimal

from fastapi import APIRouter

from contracts.pricing import PriceQuote
from pricing_engine.service import build_quote

router = APIRouter()


@router.get("/v2/quote")
def get_quote(sku: str, tier: str = "silver") -> PriceQuote:
    return build_quote(sku, Decimal("100"), tier)
