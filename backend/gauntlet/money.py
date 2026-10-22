"""Exact monetary arithmetic.

All amounts cross process and API boundaries as decimal *strings* ("12.34")
and are represented internally as integer cents. Binary floating point is
never used. These helpers are shared by the harness, the oracle, and the
reference implementations; the oracle relies only on this module and never
on implementation code.
"""

from __future__ import annotations

import re
from decimal import Decimal

# NUMERIC(12,2): at most 10 integer digits and 2 fractional digits.
_AMOUNT_RE = re.compile(r"^-?\d{1,10}(\.\d{1,2})?$")
MAX_ABS_CENTS = 999_999_999_999


class MoneyError(ValueError):
    """Raised for amounts that cannot be represented exactly to the cent."""


def parse_cents(text: str) -> int:
    """Parse a decimal string into integer cents, rejecting sub-cent precision."""
    if not isinstance(text, str):
        raise MoneyError(f"amount must be a decimal string, got {type(text).__name__}")
    s = text.strip()
    if not _AMOUNT_RE.match(s):
        raise MoneyError(f"not an exact amount with at most 2 decimals: {text!r}")
    cents = Decimal(s) * 100
    if cents != cents.to_integral_value():  # pragma: no cover - regex guarantees this
        raise MoneyError(f"sub-cent precision in {text!r}")
    value = int(cents)
    if abs(value) > MAX_ABS_CENTS:
        raise MoneyError(f"amount out of NUMERIC(12,2) range: {text!r}")
    return value


def decimal_to_cents(value: Decimal) -> int:
    """Convert a Decimal (e.g. read from NUMERIC(12,2)) into cents, exactly."""
    if not isinstance(value, Decimal):
        raise MoneyError(f"expected Decimal, got {type(value).__name__}")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise MoneyError(f"value has sub-cent precision: {value}")
    return int(cents)


def format_cents(cents: int) -> str:
    """Format integer cents as a canonical decimal string with two decimals."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise MoneyError(f"cents must be int, got {type(cents).__name__}")
    sign = "-" if cents < 0 else ""
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def cents_to_decimal(cents: int) -> Decimal:
    return Decimal(format_cents(cents))
