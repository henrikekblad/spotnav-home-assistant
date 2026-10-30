"""Fronius Wattpilot (HACS): phases through the `psm` select (auto / one phase / three phases).

One or three phases is written as the select's own option, never auto, and auto reads as unknown. The `modelStatus`
sensor's "not charging because of phase switch" (`NotChargingBecausePhaseSwitch`, code 23) is the switching status.
The select and the status are the ones go-e has (`phase_switches.phase_mode_select`); only the option values differ.

Phases are set only right before a solar start, never while it charges (`PhaseSwitch.start_only`): after a `psm`
switch a Wattpilot may stay paused until its start button is pressed (sem-community #804). Until a tester shows a live
switch is fine, a charge that runs follows the rules without switching.
"""

from __future__ import annotations

from typing import Final

from .phase_switches import phase_mode_select, PhaseModeSelect
from .registry import register_phase_switch

WATTPILOT_DOMAIN: Final = "wattpilot"


class WattpilotPhaseMode(PhaseModeSelect):
    """The `psm` select, set before a solar start only (module docstring)."""

    start_only = True


# Fronius Wattpilot (mk-maddin/wattpilot-HA): options Auto, 1 Phase, 3 Phases.
register_phase_switch(
    WATTPILOT_DOMAIN,
    phase_mode_select(WATTPILOT_DOMAIN, ("1 Phase",), ("3 Phases",), switch_class=WattpilotPhaseMode),
)
