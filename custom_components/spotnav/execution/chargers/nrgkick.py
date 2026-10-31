"""NRGkick: phases through the core integration's `phase_count` number (1 to 3); 1 or 3 is written.

The number's maximum is the attached connector's phase count, so a single-phase connector (maximum 1) has no
`PhaseSwitch`. The integration hides the 0 the charger reports while it switches, so there is no switching status.

Phases are set only right before a solar start, never while it charges (`PhaseSwitch.start_only`): a switch under
load faulted a car (a Skoda Enyaq needed unplugging; evcc #25665), while pause, switch and resume worked. Until a
tester shows a live switch is fine, a charge that runs follows the rules without switching.
"""

from __future__ import annotations

import math
from typing import Final

from .base import _finite, PhaseSwitch
from .phase_switches import charger_entities, PhaseCountNumber
from .registry import ChargerContext, register_phase_switch

NRGKICK_DOMAIN: Final = "nrgkick"

_PHASE_COUNT_KEYS: Final = ("phase_count",)


class NrgkickPhaseCount(PhaseCountNumber):
    """The phase count, set before a solar start only (module docstring)."""

    start_only = True


def _nrgkick(context: ChargerContext) -> PhaseSwitch | None:
    found = charger_entities(context, NRGKICK_DOMAIN, "number", _PHASE_COUNT_KEYS)
    if not found:
        return None
    state = context.hass.states.get(found[0])
    most = _finite(state.attributes.get("max")) if state is not None else None
    if most is not None and math.floor(most + 1e-9) < 3:
        # A single-phase connector: nothing to switch.
        return None
    return NrgkickPhaseCount(context.hass, found[0])


register_phase_switch(NRGKICK_DOMAIN, _nrgkick)
