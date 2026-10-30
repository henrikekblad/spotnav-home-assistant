"""Phase switches (`PhaseSwitch`) built on an integration's own entities: a switch, a number or a select.

Each is found on the charger's own device through the entity registry, by its translation key or unique id, never by
its entity id; a disabled entity is not found, and without the entity the charger has no `PhaseSwitch` (the setting
is then not offered). Where the integration reports that the charger is switching (it interrupts the charge for it),
that status counts as a settle (`PhaseSwitch.switching`).

* Peblar (core, `peblar.py` registers it) and Lektrico (core): the `force_single_phase` switch, on = one phase, off =
  three. Lektrico creates it only on its three-phase model (`3p22k`); its `limit_reason` sensor says
  `switching_phases` while the charger switches.
* NRGkick (core): the `phase_count` number (1 to 3); 1 or 3 is written. Its maximum is the attached connector's phase
  count, so a single-phase connector (maximum 1) has no `PhaseSwitch`. The integration hides the 0 the charger reports
  while it switches, so there is no switching status.
* go-e (HACS, API v2 and MQTT) and Fronius Wattpilot (HACS): the `psm` select (auto / one phase / three phases); one or
  three phases is written, never auto, and auto reads as unknown. The `modelStatus` sensor's
  "not charging because of phase switch" (code 23) is the switching status.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Final, Literal

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from .base import (
    _entity_unavailable,
    _finite,
    _lower,
    _not_executed,
    call_service,
    device_entities,
    PhaseSwitch,
    PhaseSwitchResult,
    PHASES_INVALID,
    PHASES_TARGET_UNAVAILABLE,
)
from .registry import ChargerContext, register_phase_switch

LEKTRICO_DOMAIN: Final = "lektrico"
NRGKICK_DOMAIN: Final = "nrgkick"
GOE_API2_DOMAIN: Final = "goecharger_api2"
GOE_MQTT_DOMAIN: Final = "goecharger_mqtt"
WATTPILOT_DOMAIN: Final = "wattpilot"

FORCE_SINGLE_PHASE_KEYS: Final = ("force_single_phase",)
_LEKTRICO_LIMIT_REASON_KEYS: Final = ("limit_reason",)
_LEKTRICO_SWITCHING: Final = frozenset({"switching_phases"})
_NRGKICK_PHASE_COUNT_KEYS: Final = ("phase_count",)
_PSM_KEYS: Final = ("psm",)
#: go-e API v2's raw code sensor (`modelstatus`, disabled by default) and its worded one (`modelstatus_value`), go-e
#: MQTT's `modelStatus` sensors (slug and code), Wattpilot's `modelStatus` sensor.
_MODEL_STATUS_KEYS: Final = ("modelstatus", "modelstatus_value")
#: The model status "not charging because of phase switch" (23) as each integration words it: the code, go-e MQTT's
#: slug, go-e API v2's English and German text, Wattpilot's enum name (all lower case).
_GOE_SWITCHING: Final = frozenset(
    {
        "23",
        "phase_switch",
        "not charging because of phase switch",
        "nicht laden während der phasenumschaltung",
        "notchargingbecausephaseswitch",
    }
)


def _charger_entities(context: ChargerContext, platform: str, domain: str, keys: tuple[str, ...]) -> list[str]:
    return device_entities(
        context.hass, platform, domain, keys, context.charge_control, context.status_entity_id, context.current_limit
    )


class ForceSinglePhaseSwitch(PhaseSwitch):
    """A force-single-phase switch: on charges on one phase, off on the phases connected (three)."""

    async def async_set_phases(self, phases: Literal[1, 3]) -> PhaseSwitchResult:
        if phases not in (1, 3):
            return PhaseSwitchResult(False, PHASES_INVALID)
        if _entity_unavailable(self.hass, self.entity_id):
            _not_executed(self.entity_id)
            return PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)
        service = "turn_on" if phases == 1 else "turn_off"
        await call_service(self.hass, "switch", service, {"entity_id": self.entity_id}, blocking=True)
        return PhaseSwitchResult(True)

    def phases_now(self) -> int | None:
        state = _lower(self.hass.states.get(self.entity_id))
        if state == STATE_ON:
            return 1
        if state == STATE_OFF:
            return 3
        return None


class PhaseCountNumber(PhaseSwitch):
    """A number that is the phase count to charge on: 1 or 3 is written; 2, or anything else, reads as unknown."""

    async def async_set_phases(self, phases: Literal[1, 3]) -> PhaseSwitchResult:
        if phases not in (1, 3):
            return PhaseSwitchResult(False, PHASES_INVALID)
        if _entity_unavailable(self.hass, self.entity_id):
            _not_executed(self.entity_id)
            return PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)
        await call_service(
            self.hass, "number", "set_value", {"entity_id": self.entity_id, "value": phases}, blocking=True
        )
        return PhaseSwitchResult(True)

    def phases_now(self) -> int | None:
        state = self.hass.states.get(self.entity_id)
        value = _finite(state.state) if state is not None else None
        if value == 1:
            return 1
        if value == 3:
            return 3
        return None


class PhaseModeSelect(PhaseSwitch):
    """A select of auto, one phase or three phases: one or three is written (as the select's own option text), never
    auto; auto, or anything else, reads as unknown."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        *,
        one: tuple[str, ...],
        three: tuple[str, ...],
        switching_entity_ids: tuple[str, ...] = (),
        switching_values: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(
            hass, entity_id, switching_entity_ids=switching_entity_ids, switching_values=switching_values
        )
        self._options: dict[int, tuple[str, ...]] = {1: one, 3: three}

    def _option(self, phases: int) -> str | None:
        """The select's own option for `phases`, or `None` when it offers none of the known ones."""
        wanted = tuple(option.lower() for option in self._options[phases])
        state = self.hass.states.get(self.entity_id)
        offered = state.attributes.get("options") if state is not None else None
        if not isinstance(offered, (list, tuple)):
            return self._options[phases][0]
        for option in offered:
            if str(option).strip().lower() in wanted:
                return str(option)
        return None

    async def async_set_phases(self, phases: Literal[1, 3]) -> PhaseSwitchResult:
        if phases not in (1, 3):
            return PhaseSwitchResult(False, PHASES_INVALID)
        option = None if _entity_unavailable(self.hass, self.entity_id) else self._option(phases)
        if option is None:
            _not_executed(self.entity_id)
            return PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)
        await call_service(
            self.hass, "select", "select_option", {"entity_id": self.entity_id, "option": option}, blocking=True
        )
        return PhaseSwitchResult(True)

    def phases_now(self) -> int | None:
        state = _lower(self.hass.states.get(self.entity_id))
        for phases, options in self._options.items():
            if state in (option.lower() for option in options):
                return phases
        return None


def force_single_phase(
    context: ChargerContext,
    platform: str,
    *,
    switching_entity_ids: tuple[str, ...] = (),
    switching_values: frozenset[str] = frozenset(),
) -> PhaseSwitch | None:
    """The charger's force-single-phase switch of `platform`, or `None` when it has none."""
    found = _charger_entities(context, platform, "switch", FORCE_SINGLE_PHASE_KEYS)
    if not found:
        return None
    return ForceSinglePhaseSwitch(
        context.hass, found[0], switching_entity_ids=switching_entity_ids, switching_values=switching_values
    )


def _lektrico(context: ChargerContext) -> PhaseSwitch | None:
    return force_single_phase(
        context,
        LEKTRICO_DOMAIN,
        switching_entity_ids=tuple(_charger_entities(context, LEKTRICO_DOMAIN, "sensor", _LEKTRICO_LIMIT_REASON_KEYS)),
        switching_values=_LEKTRICO_SWITCHING,
    )


def _nrgkick(context: ChargerContext) -> PhaseSwitch | None:
    found = _charger_entities(context, NRGKICK_DOMAIN, "number", _NRGKICK_PHASE_COUNT_KEYS)
    if not found:
        return None
    state = context.hass.states.get(found[0])
    most = _finite(state.attributes.get("max")) if state is not None else None
    if most is not None and math.floor(most + 1e-9) < 3:
        # A single-phase connector: nothing to switch.
        return None
    return PhaseCountNumber(context.hass, found[0])


def _phase_mode_select(
    platform: str, one: tuple[str, ...], three: tuple[str, ...]
) -> Callable[[ChargerContext], PhaseSwitch | None]:
    def factory(context: ChargerContext) -> PhaseSwitch | None:
        found = _charger_entities(context, platform, "select", _PSM_KEYS)
        if not found:
            return None
        return PhaseModeSelect(
            context.hass,
            found[0],
            one=one,
            three=three,
            switching_entity_ids=tuple(_charger_entities(context, platform, "sensor", _MODEL_STATUS_KEYS)),
            switching_values=_GOE_SWITCHING,
        )

    return factory


register_phase_switch(LEKTRICO_DOMAIN, _lektrico)
register_phase_switch(NRGKICK_DOMAIN, _nrgkick)
# go-e API v2 (marq24/ha-goecharger-api2): options "0" auto, "1" force one phase, "2" force three phases.
register_phase_switch(GOE_API2_DOMAIN, _phase_mode_select(GOE_API2_DOMAIN, ("1",), ("2",)))
# go-e MQTT (syssi/homeassistant-goecharger-mqtt): options auto, one_phase, three_phases.
register_phase_switch(GOE_MQTT_DOMAIN, _phase_mode_select(GOE_MQTT_DOMAIN, ("one_phase",), ("three_phases",)))
# Fronius Wattpilot (mk-maddin/wattpilot-HA): options Auto, 1 Phase, 3 Phases.
register_phase_switch(WATTPILOT_DOMAIN, _phase_mode_select(WATTPILOT_DOMAIN, ("1 Phase",), ("3 Phases",)))
