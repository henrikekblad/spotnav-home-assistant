"""A test action asked for by a person (`Trial`), and the shell's two refusals: a test action refused and a plant whose
controls cannot be reached."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..commands import ACTION_CURTAIL_EXPORT


class ControlNotReachable(RuntimeError):
    """A look or a write was asked for that the site's gate does not allow now."""


class TrialRefused(ValueError):
    """The owner's test action cannot run: `code` says why (a stable code for the service's error)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Trial:
    """The owner's test action (`spotnav.battery_control_test`): one action through the executor until `until`."""

    action: str
    rate_kw: float
    started_at: datetime
    until: datetime
    force: bool

    @property
    def export(self) -> bool:
        return self.action == ACTION_CURTAIL_EXPORT

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "rate_kw": self.rate_kw, "started_at": self.started_at.isoformat(),
                "until": self.until.isoformat(), "force_test": self.force}
