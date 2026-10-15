from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import Field, PlainSerializer

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
# Largest value that fits NUMERIC(12,2).
MAX_MONEY = Decimal("9999999999.99")


def q(value: Decimal | int | str | None) -> Decimal:
    """Quantize to 2 decimals (COP stored as NUMERIC(12,2))."""
    if value is None:
        return ZERO
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


_ser = PlainSerializer(lambda v: format(q(v), "f"), return_type=str, when_used="json")

# Output money: always a JSON string with 2 decimals, e.g. "12500.00".
Money = Annotated[Decimal, _ser]
# Input money: JSON number or numeric string, at most 2 decimals.
MoneyIn = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2), _ser]
PositiveMoneyIn = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2), _ser]
