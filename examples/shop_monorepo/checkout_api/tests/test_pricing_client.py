from decimal import Decimal

from checkout_api.pricing_client import parse_discount
from contracts.pricing import PriceQuote


def test_parse_discount_gold():
    assert parse_discount(PriceQuote("sku-1", "gold", Decimal("10"))) == Decimal("0.20")
