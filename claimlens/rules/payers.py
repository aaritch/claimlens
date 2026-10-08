"""Payer-name normalization: turn "Blue Cross Blue Shield" / "BCBS" into one canonical name."""
from __future__ import annotations

import difflib
import re
from typing import Optional

PAYER_ALIASES: dict[str, list[str]] = {
    "BlueCross BlueShield": ["BlueCross BlueShield", "Blue Cross Blue Shield", "BCBS", "Blue Cross and Blue Shield"],
    "Aetna": ["Aetna", "Aetna Inc"],
    "Cigna": ["Cigna", "Cigna Healthcare"],
    "UnitedHealthcare": ["UnitedHealthcare", "United Healthcare", "UHC"],
    "Humana": ["Humana", "Humana Inc"],
    "Anthem": ["Anthem", "Anthem Health Plans"],
}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_INDEX = {_key(alias): canon for canon, aliases in PAYER_ALIASES.items() for alias in aliases}


def normalize_payer(name: str) -> Optional[str]:
    """Return the canonical payer name, or None if we don't recognise it."""
    key = _key(name)
    if key in _INDEX:
        return _INDEX[key]
    close = difflib.get_close_matches(key, _INDEX.keys(), n=1, cutoff=0.85)
    return _INDEX[close[0]] if close else None
