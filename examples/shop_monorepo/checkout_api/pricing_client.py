from decimal import Decimal

from contracts.pricing import PriceQuote

DISCOUNTS = {"gold": Decimal("0.20"), "silver": Decimal("0.10")}


def parse_discount(quote: PriceQuote) -> Decimal:
    # Reads discount_tier as a plain string: an object here breaks the lookup.
    return DISCOUNTS[quote.discount_tier.lower()]


def checkout_total(quote: PriceQuote, quantity: int) -> Decimal:
    return quote.final_price * quantity * (1 - parse_discount(quote))
