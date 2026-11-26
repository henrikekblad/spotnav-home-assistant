"""Small helpers shared by the executor's parts: an event row, whether a hand-back's reason blocks, and the site's
writes today."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .const import _BLOCKING_PREFIXES
from .state import ExecInputs, ExecState


def _event(now: datetime, kind: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"at": now.isoformat(), "kind": kind, "reason": reason} | detail


def _blocking(reason: str) -> bool:
    return reason.startswith(_BLOCKING_PREFIXES)


def _without(names: tuple[str, ...], name: str) -> tuple[str, ...]:
    return tuple(item for item in names if item != name)


def _writes_today(state: ExecState, inputs: ExecInputs) -> int:
    """The site's writes today: this instance's and the other's (the cap is shared)."""
    return state.writes_today + max(inputs.other_writes_today, 0)
