"""Money arithmetic. Decimal at the boundary, integer cents for comparisons; DB columns stay Float."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_decimal(x: float | int | str | Decimal) -> Decimal:
    """Floats go through str() so 0.1 becomes Decimal('0.1'), not its binary approximation."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x)) if isinstance(x, float) else Decimal(x)


def quantize(x: float | int | str | Decimal) -> Decimal:
    """Round to cents, half up (commercial rounding)."""
    return to_decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def cents(x: float | int | str | Decimal) -> int:
    return int(quantize(x) * 100)


def vat_split(gross: float | Decimal, rate: float | Decimal) -> tuple[Decimal, Decimal]:
    """Split a VAT-inclusive price into (net, vat); net + vat == quantize(gross) exactly."""
    g, r = quantize(gross), to_decimal(rate)
    vat = quantize(g * r / (1 + r))
    return g - vat, vat
