"""Lektrico: phases through the core integration's `force_single_phase` switch.

On = one phase, off = three. The integration creates the switch only on the three-phase model (`3p22k`); without it
the charger has no `PhaseSwitch`. It is found on the charger's own device by its translation key or unique id
(`<serial>_force_single_phase`). The `limit_reason` sensor says `switching_phases` while the charger switches.
"""

from __future__ import annotations

from typing import Final

from .base import PhaseSwitch
from .phase_switches import charger_entities, force_single_phase
from .registry import ChargerContext, register_phase_switch

LEKTRICO_DOMAIN: Final = "lektrico"

_LIMIT_REASON_KEYS: Final = ("limit_reason",)
_SWITCHING: Final = frozenset({"switching_phases"})


def _lektrico(context: ChargerContext) -> PhaseSwitch | None:
    return force_single_phase(
        context,
        LEKTRICO_DOMAIN,
        switching_entity_ids=tuple(charger_entities(context, LEKTRICO_DOMAIN, "sensor", _LIMIT_REASON_KEYS)),
        switching_values=_SWITCHING,
    )


register_phase_switch(LEKTRICO_DOMAIN, _lektrico)
