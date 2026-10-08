"""NPI (National Provider Identifier) checks.

An NPI is 10 digits; the last digit is a Luhn check digit computed over the
digits with the prefix "80840" in front. Analogy: it's the same trick credit
cards use: the last digit is a "typo detector" derived from the others, so a
single mistyped digit (by a human OR an LLM misreading a scan) gets caught.
"""
from __future__ import annotations

NPI_PREFIX = "80840"


def luhn_valid(number: str) -> bool:
    if not number.isdigit():
        return False
    total = 0
    # Walk right-to-left; double every second digit.
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_npi(npi: str) -> bool:
    return len(npi) == 10 and npi.isdigit() and luhn_valid(NPI_PREFIX + npi)


def make_npi(first_nine: str) -> str:
    """Build a *valid-looking* NPI from 9 digits (used only for synthetic data)."""
    if len(first_nine) != 9 or not first_nine.isdigit():
        raise ValueError("need exactly 9 digits")
    for check in "0123456789":
        candidate = first_nine + check
        if is_valid_npi(candidate):
            return candidate
    raise AssertionError("unreachable: one check digit always works")
