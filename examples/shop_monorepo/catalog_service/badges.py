from dataclasses import dataclass


@dataclass
class PromoBadge:
    """Unrelated to pricing-api-v2, despite the same field name."""
    label: str
    discount_tier: str


def render_badge(badge: PromoBadge) -> str:
    return f"{badge.label} ({badge.discount_tier})"
