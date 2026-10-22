from decimal import Decimal

import pytest

from gauntlet.money import MoneyError, decimal_to_cents, format_cents, parse_cents
from gauntlet.scenarios.script import float_hostile


@pytest.mark.parametrize("text,cents", [
    ("0", 0), ("0.1", 10), ("0.10", 10), ("19.99", 1999), ("0.29", 29), ("-4.35", -435),
    ("9999999999.99", 999_999_999_999), ("1.01", 101),
])
def test_parse_exact(text, cents):
    assert parse_cents(text) == cents


@pytest.mark.parametrize("bad", ["1.234", "abc", "", "1e3", "10000000000.00", "1.", ".5", "1,00", " 1.0 0"])
def test_parse_rejects(bad):
    with pytest.raises(MoneyError):
        parse_cents(bad)


def test_parse_rejects_non_string():
    with pytest.raises(MoneyError):
        parse_cents(19.99)  # type: ignore[arg-type]


@pytest.mark.parametrize("cents,text", [(0, "0.00"), (5, "0.05"), (1999, "19.99"), (-435, "-4.35"), (100, "1.00")])
def test_format(cents, text):
    assert format_cents(cents) == text


def test_roundtrip_exhaustive_small_range():
    for c in range(-20_000, 20_001):
        assert parse_cents(format_cents(c)) == c


def test_decimal_to_cents_rejects_subcent():
    assert decimal_to_cents(Decimal("12.30")) == 1230
    with pytest.raises(MoneyError):
        decimal_to_cents(Decimal("0.001"))


def test_float_hostile_detects_known_cases():
    # The reason exact arithmetic is required: binary floats truncate these.
    assert float_hostile(1999) and float_hostile(29)
    assert not float_hostile(1000)
