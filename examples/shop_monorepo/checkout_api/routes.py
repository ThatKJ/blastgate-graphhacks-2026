from fastapi import APIRouter

from checkout_api.pricing_client import checkout_total
from contracts.pricing import PriceQuote

router = APIRouter()


@router.post("/checkout")
def checkout(quote: PriceQuote, quantity: int = 1) -> dict:
    return {"total": str(checkout_total(quote, quantity))}
