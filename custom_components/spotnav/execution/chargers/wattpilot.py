"""Fronius Wattpilot (HACS): phases through the `psm` select (auto / one phase / three phases).

One or three phases is written as the select's own option, never auto, and auto reads as unknown. The `modelStatus`
sensor's "not charging because of phase switch" (`NotChargingBecausePhaseSwitch`, code 23) is the switching status.
The select and the status are the ones go-e has (`phase_switches.phase_mode_select`); only the option values differ.
"""

from __future__ import annotations

from typing import Final

from .phase_switches import phase_mode_select
from .registry import register_phase_switch

WATTPILOT_DOMAIN: Final = "wattpilot"

# Fronius Wattpilot (mk-maddin/wattpilot-HA): options Auto, 1 Phase, 3 Phases.
register_phase_switch(WATTPILOT_DOMAIN, phase_mode_select(WATTPILOT_DOMAIN, ("1 Phase",), ("3 Phases",)))
