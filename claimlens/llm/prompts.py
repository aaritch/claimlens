"""Prompts are versioned files, not inline strings, so changes show up in git
diffs and every result can record which prompt version produced it."""
from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """name like 'classify_v1' -> contents of prompts/classify_v1.txt"""
    return (PROMPT_DIR / f"{name}.txt").read_text().strip()
