from contracts.pricing import PriceQuote


def tier_revenue(quotes: list[PriceQuote]) -> dict:
    out: dict = {}
    for q in quotes:
        out[q.discount_tier] = out.get(q.discount_tier, 0) + q.final_price
    return out
