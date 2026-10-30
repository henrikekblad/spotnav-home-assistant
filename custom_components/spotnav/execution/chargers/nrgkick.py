"""NRGkick: phases through the core integration's `phase_count` number (1 to 3); 1 or 3 is written.

The number's maximum is the attached connector's phase count, so a single-phase connector (maximum 1) has no
`PhaseSwitch`. The integration hides the 0 the charger reports while it switches, so there is no switching status.
"""

from __future__ import annotations

import math
from typing import Final

from .base import _finite, PhaseSwitch
from .phase_switches import charger_entities, PhaseCountNumber
from .registry import ChargerContext, register_phase_switch

NRGKICK_DOMAIN: Final = "nrgkick"

_PHASE_COUNT_KEYS: Final = ("phase_count",)


def _nrgkick(context: ChargerContext) -> PhaseSwitch | None:
    found = charger_entities(context, NRGKICK_DOMAIN, "number", _PHASE_COUNT_KEYS)
    if not found:
        return None
    state = context.hass.states.get(found[0])
    most = _finite(state.attributes.get("max")) if state is not None else None
    if most is not None and math.floor(most + 1e-9) < 3:
        # A single-phase connector: nothing to switch.
        return None
    return PhaseCountNumber(context.hass, found[0])


register_phase_switch(NRGKICK_DOMAIN, _nrgkick)
