"""Salt test results.

During a salt test the unit streams readings ("TDS=<ppm>&condition=<verdict>")
for about a minute. When the cell changes direction mid-test it briefly reports
a much lower figure, so the result is the middle (median) reading of the test,
which ignores those dips. The verdict is the one that came with that reading,
e.g. "TDS PERFECT", "TDS HIGH but OK", "CHECK and ADD SALT" or "OK".
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

SESSION_GAP_MS = 15_000  # readings further apart than this belong to different tests


def parse_reading(param: Any) -> tuple[int, str] | None:
    """(ppm, verdict) from one salt test screen message, or None if it has no reading."""
    if not isinstance(param, str):
        return None
    fields = dict(parse_qsl(param))
    condition = fields.get("condition", "").strip()
    try:
        value = int(fields.get("TDS", "0"))
    except ValueError:
        return None
    if value <= 0 or not condition:
        return None
    return value, condition


def summarise(readings: list[tuple[int, str]]) -> dict[str, Any] | None:
    """Median reading of one test, with its verdict."""
    if not readings:
        return None
    ordered = sorted(readings, key=lambda r: r[0])
    value, condition = ordered[len(ordered) // 2]
    return {"value": value, "condition": condition}


def condition_text(condition: Any) -> str | None:
    """Tidy the unit's verdict for display: "TDS PERFECT" -> "TDS perfect"."""
    if not isinstance(condition, str) or not condition.strip():
        return None
    keep_upper = {"TDS", "OK", "PPM"}
    words = [w if w.upper() in keep_upper else w.lower() for w in condition.split()]
    text = " ".join(words)
    return text[0].upper() + text[1:]
