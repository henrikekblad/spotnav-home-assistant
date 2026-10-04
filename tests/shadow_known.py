"""Disagreements between the charge-ownership core's shadow and today's code that are explained: real differences of
today's code (step 1 copies today's rules, and fixes nothing in today's code), each named with its event sequence.

`explain(record)` returns the name of the explanation, or `None` for a disagreement nobody explained (the test then
fails, `conftest.ownership_shadow_agrees`).
"""

from __future__ import annotations

from typing import Any


def explain(record: dict[str, Any]) -> str | None:
    return None
