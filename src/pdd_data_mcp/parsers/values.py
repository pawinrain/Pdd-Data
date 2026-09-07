from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pdd_data_mcp.errors import ValidationFailure


@dataclass(frozen=True)
class ParsedNumber:
    value: int | str
    precision: str


def _finite_decimal(raw: str | int | Decimal) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailure("invalid decimal value") from exc
    if not value.is_finite():
        raise ValidationFailure("NaN and Infinity are forbidden")
    return value


def money_to_cents(raw: str | int | Decimal) -> ParsedNumber:
    text = str(raw).strip()
    multiplier = Decimal("100")
    precision = "EXACT"
    if text.endswith("万"):
        text = text[:-1]
        multiplier *= Decimal("10000")
        precision = "APPROXIMATE"
    amount = _finite_decimal(text)
    cents = amount * multiplier
    if cents != cents.to_integral_value():
        raise ValidationFailure("money cannot be represented as integer cents")
    if cents < 0:
        raise ValidationFailure("money cannot be negative")
    return ParsedNumber(value=int(cents), precision=precision)


def ratio_to_string(raw: str | int | Decimal, *, percent: bool = False) -> ParsedNumber:
    value = _finite_decimal(raw)
    if percent:
        value /= Decimal("100")
    if value < 0:
        raise ValidationFailure("ratio cannot be negative")
    normalized = format(value.normalize(), "f")
    return ParsedNumber(value=normalized, precision="EXACT")
