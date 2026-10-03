"""Damping for active control: two bands, a deadband and a dwell.

Reacting instantly to every dip makes the charger dither and denies a
self-regulating load on the site (a house battery) time to give way. So the
split is by what a change is *for*, not by direction:

* Protection: the measurement is near the limit and the change is a
  reduction. Written immediately, at the largest safe value the decision
  already computed, with no deadband and no dwell. A non-reduction is never
  protection.
* Regulation: everything else. Needs a deadband (changes smaller than
  `deadband_a` are ignored) and a dwell (the new level must stay justified for
  `dwell_s` before it is written). Both come from the site options
  (`CONF_REGULATOR_DEADBAND_A` / `CONF_REGULATOR_DWELL_S`).

Damping only chooses between the proposal and what is already on the charger,
never anything bolder. Whether a proposal may be written at all is
`regulator.decision_still_applyable`'s job; a refusal
(`proposed_current_a is None`) means "write nothing", not "keep what is there".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from homeassistant.util import dt as dt_util

from ..const import DEFAULT_REGULATOR_DEADBAND_A, DEFAULT_REGULATOR_DWELL_S


_LOGGER = logging.getLogger(__name__)

RegulatorBand = Literal["protection", "regulation"]
DampingReason = Literal[
    "first_write",
    "protection",
    "deadband",
    "dwell_pending",
    "dwell_elapsed",
    "no_proposal",
    "margin_unusable",
    "urgent",
    "verified_step",
]

# Remaining uncredited margin (A) at or below which a reduction is protection;
# one amp is the smallest step the regulator can express.
PROTECTION_BAND_A = 1.0


def band_for_measurement(
    margin_a: float | None, *, protection_band_a: float = PROTECTION_BAND_A
) -> RegulatorBand | None:
    """Band for a phase's margin; `None` (unusable margin) means hold."""
    if margin_a is None:
        return None
    return "protection" if margin_a <= protection_band_a else "regulation"


@dataclass(frozen=True, slots=True)
class DampingDecision:
    """Whether to write, what to write, and which rule decided it.

    When `write` is False, `current_a` is the last value written, or `None` if unknown.
    """

    write: bool
    current_a: float | None
    reason: DampingReason


class RegulatorDamper:
    """Per-charger damping state (last written value, pending candidate); clock injected."""

    def __init__(
        self,
        *,
        deadband_a: float = DEFAULT_REGULATOR_DEADBAND_A,
        dwell_s: float = DEFAULT_REGULATOR_DWELL_S,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self._deadband_a = deadband_a
        self._dwell_s = dwell_s
        self._now = now
        # Last value actually written (None before the first write).
        self._written: float | None = None
        self._pending: float | None = None
        self._pending_since: datetime | None = None

    @property
    def last_written_a(self) -> float | None:
        return self._written

    def consider(
        self,
        *,
        proposed_current_a: float | None,
        margin_a: float | None,
        urgent: bool = False,
        verified_step: bool = False,
    ) -> DampingDecision:
        """Whether this proposal may be written now, and under which rule.

        `urgent` (from the yield-stepping layer) skips deadband and dwell, for a
        reduction only. `verified_step` is the one exception for an increase: a step the
        yield-stepping layer licensed on a battery it verified gives way, which has its own
        settle time and spacing, so a 1 A step near the target is not swallowed by the deadband
        and the climb is not held for the dwell.
        """
        if proposed_current_a is None:
            return DampingDecision(False, self._written, "no_proposal")

        band = band_for_measurement(margin_a)
        if band is None:
            return DampingDecision(False, self._written, "margin_unusable")

        if self._written is None:
            self._write(proposed_current_a)
            return DampingDecision(True, proposed_current_a, "first_write")

        if band == "protection" and proposed_current_a < self._written:
            # Near the limit and reducing: immediate, at the decision\'s own value.
            self._write(proposed_current_a)
            return DampingDecision(True, proposed_current_a, "protection")

        change_a = abs(proposed_current_a - self._written)
        if change_a == 0.0:
            # Already at this value: not a candidate.
            self._pending = None
            self._pending_since = None
            return DampingDecision(False, self._written, "deadband")

        # Urgency only hastens a reduction; nothing makes the regulator bolder faster.
        if urgent and proposed_current_a < self._written:
            self._write(proposed_current_a)
            return DampingDecision(True, proposed_current_a, "urgent")

        if verified_step and proposed_current_a > self._written:
            self._write(proposed_current_a)
            return DampingDecision(True, proposed_current_a, "verified_step")

        if change_a < self._deadband_a:
            self._pending = None
            self._pending_since = None
            return DampingDecision(False, self._written, "deadband")

        if self._pending is None or abs(proposed_current_a - self._pending) > self._deadband_a:
            # ">" not ">=": with deadband 0, ">=" would restart the dwell every pass.
            self._pending = proposed_current_a
            self._pending_since = self._now()
            if self._dwell_s <= 0.0:
                self._write(self._pending)
                return DampingDecision(True, self._written, "dwell_elapsed")
            return DampingDecision(False, self._written, "dwell_pending")

        elapsed = (self._now() - self._pending_since).total_seconds()
        if elapsed < self._dwell_s:
            return DampingDecision(False, self._written, "dwell_pending")
        self._write(self._pending)
        return DampingDecision(True, self._written, "dwell_elapsed")

    def forget_write(self, previous: float | None) -> None:
        """Undo the last `consider` that said write, because the charger was not written.

        The damper records a write when it decides one; an adapter that refused it (a rate limit, a
        flash setting) must not leave it believing the charger moved. Back to what was really on it.
        """
        self._written = previous
        self._pending = None
        self._pending_since = None

    def _write(self, value: float) -> None:
        self._written = value
        self._pending = None
        self._pending_since = None
