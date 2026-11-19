"""Amps to kW, once: the one rule every part of SpotNav turns a current into power with, and the readers of the site's
electric settings it is fed from. Pure: no Home Assistant imports.

A site is three-phase. Its network sets the voltage between two phases: 400 V on a TN network, 230 V on an IT
network (much of Norway). Three-phase power is `sqrt(3) x V_between_phases x I`, not a "230 x 3" shortcut, which
on an IT network overstates by 73 %. A single-phase load sees 230 V on either network (on an IT network it sits
between two phases).
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any, Final

from ..const import (
    CONF_MAIN_FUSE_A,
    CONF_SAFETY_MARGIN_A,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    DEFAULT_SAFETY_MARGIN_A,
    DEFAULT_VOLTAGE_BETWEEN_PHASES_V,
    VOLTAGE_BETWEEN_PHASES_CHOICES,
)

#: The nominal voltage across a single-phase load (phase to neutral on TN, between two phases on IT), and the
#: nominal phase voltage a measured current is turned into watts with where no voltage is read.
PHASE_V: Final = 230.0
#: The voltage between phases where none is known: a TN network.
TN_VOLTS_BETWEEN_PHASES: Final = float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def three_phase_kw(amps: float, volts_between_phases: float) -> float:
    """Three-phase power (kW) at `amps` on each phase."""
    return math.sqrt(3.0) * volts_between_phases * amps / 1000.0


def power_kw(amps: float, phases: int, voltage_between_phases_v: float = TN_VOLTS_BETWEEN_PHASES) -> float:
    """The power a connector draws: 230 V single phase, or three-phase at the voltage between phases."""
    if phases == 1:
        return PHASE_V * amps / 1000.0
    return three_phase_kw(amps, voltage_between_phases_v)


def phase_volts(volts_between_phases: float) -> float:
    """One phase's voltage to neutral."""
    return volts_between_phases / math.sqrt(3.0)


def fuse_kw(main_fuse_a: float | None, volts_between_phases: float, margin_a: float = 0.0) -> float | None:
    """The fuse as three-phase power (kW), less `margin_a` per phase; `None` when the fuse is unknown."""
    fuse = _number(main_fuse_a)
    if fuse is None or fuse <= 0 or volts_between_phases <= 0:
        return None
    return three_phase_kw(max(fuse - margin_a, 0.0), volts_between_phases)


def stored_voltage_between_phases_v(data: Mapping[str, Any] | None) -> float:
    """The stored choice of an entry's data; anything but a known choice is the default."""
    value = None if data is None else data.get(CONF_VOLTAGE_BETWEEN_PHASES_V)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return TN_VOLTS_BETWEEN_PHASES
    return float(value) if value in VOLTAGE_BETWEEN_PHASES_CHOICES else TN_VOLTS_BETWEEN_PHASES


def stored_safety_margin_a(data: Mapping[str, Any] | None) -> float:
    """The site's safety margin (A per phase): the stored one, else `DEFAULT_SAFETY_MARGIN_A` (also for a value that
    is not a finite, non-negative number)."""
    margin = None if data is None else _number(data.get(CONF_SAFETY_MARGIN_A))
    return DEFAULT_SAFETY_MARGIN_A if margin is None or margin < 0 else margin


def site_fuse_kw(data: Mapping[str, Any] | None) -> float | None:
    """The site's main fuse as three-phase power (kW) on its network, without the margin; `None` where none is set."""
    if data is None:
        return None
    return fuse_kw(data.get(CONF_MAIN_FUSE_A), stored_voltage_between_phases_v(data))
