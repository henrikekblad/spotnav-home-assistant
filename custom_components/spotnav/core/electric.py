"""Amps to kW, once: the one rule every part of SpotNav turns a current into power with, and the readers of the site's
electric settings it is fed from. Pure: no Home Assistant imports.

A site is three-phase unless its meter measures L1 alone (`site_phase_count`): then it is a single-phase supply,
its main fuse one phase at the stated amps, never three. Its network sets the voltage between two phases: 400 V on a TN network, 230 V on an IT
network (much of Norway). Three-phase power is `sqrt(3) x V_between_phases x I`, not a "230 x 3" shortcut, which
on an IT network overstates by 73 %. A single-phase load sees 230 V on either network (on an IT network it sits
between two phases).
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any, Final

from .parse import finite_number
from ..const import (
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_MAIN_FUSE_A,
    CONF_MEASUREMENT_MODE,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SOURCE,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    DEFAULT_SAFETY_MARGIN_A,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
    DEFAULT_VOLTAGE_BETWEEN_PHASES_V,
    VOLTAGE_BETWEEN_PHASES_CHOICES,
)

#: The nominal voltage across a single-phase load (phase to neutral on TN, between two phases on IT), and the
#: nominal phase voltage a measured current is turned into watts with where no voltage is read.
PHASE_V: Final = 230.0
#: The voltage between phases where none is known: a TN network.
TN_VOLTS_BETWEEN_PHASES: Final = float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V)


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


def fuse_kw(
    main_fuse_a: float | None, volts_between_phases: float, margin_a: float = 0.0, phases: int = 3
) -> float | None:
    """The fuse as power (kW), less `margin_a` per phase: three-phase, or on a single-phase site (`phases` 1) the one
    phase at `PHASE_V`; `None` when the fuse is unknown."""
    fuse = finite_number(main_fuse_a)
    if fuse is None or fuse <= 0 or volts_between_phases <= 0:
        return None
    return power_kw(max(fuse - margin_a, 0.0), 1 if phases == 1 else 3, volts_between_phases)


#: The phases a site's meter measures when it measures L1 alone.
_SINGLE_PHASE_KEYS: Final = frozenset({"L1"})


def _filled_phases(mapping: Any) -> frozenset[str]:
    """The phase keys of a stored `{phase: value}` mapping whose value is set."""
    if not isinstance(mapping, Mapping):
        return frozenset()
    return frozenset(str(phase) for phase, value in mapping.items() if value)


def site_phase_count(data: Mapping[str, Any] | None) -> int:
    """The phases a site has: 1 when its meter measures L1 alone (a direct site's current source or entities, a
    derived site's power), else 3. Not a setting: a single-phase supply has no L2 or L3 to read, and nothing else says
    how many phases a site has, so a site measured otherwise (all three phases, two of them, or no per-phase reading
    at all) stays three-phase, as before."""
    if data is None:
        return 3
    mode = data.get(CONF_MEASUREMENT_MODE)
    keys: frozenset[str] = frozenset()
    if mode == MEASUREMENT_MODE_DIRECT:
        source = data.get(CONF_SITE_CURRENT_SOURCE)
        if isinstance(source, Mapping) and source.get("kind") in ("separate_entities", "attributes"):
            keys = _filled_phases(source.get("entity_ids") if source.get("kind") == "separate_entities" else source.get("attributes"))
        else:
            keys = _filled_phases(data.get(CONF_DIRECT_ENTITIES))
    elif mode == MEASUREMENT_MODE_DERIVED:
        derived = data.get(CONF_DERIVED_ENTITIES)
        if isinstance(derived, Mapping):
            keys = frozenset(
                str(phase) for phase, entities in derived.items() if isinstance(entities, Mapping) and entities.get("power")
            )
    return 1 if keys == _SINGLE_PHASE_KEYS else 3


def stored_voltage_between_phases_v(data: Mapping[str, Any] | None) -> float:
    """The stored choice of an entry's data; anything but a known choice is the default."""
    value = None if data is None else data.get(CONF_VOLTAGE_BETWEEN_PHASES_V)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return TN_VOLTS_BETWEEN_PHASES
    return float(value) if value in VOLTAGE_BETWEEN_PHASES_CHOICES else TN_VOLTS_BETWEEN_PHASES


def stored_safety_margin_a(data: Mapping[str, Any] | None) -> float:
    """The site's safety margin (A per phase): the stored one, else `DEFAULT_SAFETY_MARGIN_A` (also for a value that
    is not a finite, non-negative number)."""
    margin = None if data is None else finite_number(data.get(CONF_SAFETY_MARGIN_A))
    return DEFAULT_SAFETY_MARGIN_A if margin is None or margin < 0 else margin


def site_fuse_kw(data: Mapping[str, Any] | None, phases: int | None = None) -> float | None:
    """The site's main fuse as power (kW) on its network, without the margin: three-phase, or one phase on a
    single-phase site (`site_phase_count`); `None` where none is set. Fails closed: a fuse that is stored but unusable
    (0, negative, not a finite number) is 0 kW, no import at all. `phases` is the site's phase count where the caller
    knows it better than the stored data (`SiteCapacityController.site_phases`)."""
    stored = None if data is None else data.get(CONF_MAIN_FUSE_A)
    if stored is None:
        return None
    kw = fuse_kw(stored, stored_voltage_between_phases_v(data), phases=site_phase_count(data) if phases is None else phases)
    return 0.0 if kw is None else kw
