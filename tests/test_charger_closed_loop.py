"""Closed loop: the real regulator writing a real (mock-served) charger through a rate-limited adapter.

A Wallbox-class charger takes a current at most every 90 s. The regulator decides every few seconds.
Two things must hold whatever the load does: the current is never written faster than the policy
allows, and when the fuse needs a lower current than that policy lets through in time, the charge is
stopped rather than left above the fuse.
"""

from __future__ import annotations

from datetime import datetime

from freezegun import freeze_time
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import (
    CONF_CHARGER_PLATFORM,
    CONF_MEASURED_CURRENT_SOURCE,
    CURRENT_CONTROL_NUMBER,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.site.regulator import PhaseDecisionBasis, RegulatorDecision
from custom_components.spotnav.site.regulator_damping import RegulatorDamper

from .helpers import make_entry, make_site_entry
from .world import (
    controller_of,
    separate_entities_source,
    set_charger_delivered_a,
    set_derived_site_entities,
    set_site_current_a,
)

FUSE_A = 20.0
START = datetime(2026, 9, 30, 12, 0, 0)
AMPERE = {"unit_of_measurement": "A", "min": 6, "max": 16, "step": 1}


class Charger:
    """A mock-served Wallbox-class charger: the number takes a value, the switch turns on and off."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        self.hass, self.name = hass, name
        self.writes: list[tuple[datetime, int]] = []
        self.stops: list[datetime] = []
        self.starts = 0
        hass.states.async_set(f"switch.{name}", "off")
        hass.states.async_set(f"number.{name}_limit", "13", AMPERE)

    def serve(self) -> None:
        """Register the services. After the integration is set up: loading its `number` and `switch`
        platforms brings Home Assistant's own services, which would replace these.
        """
        self.hass.services.async_register("number", "set_value", self._set_value)
        self.hass.services.async_register("switch", "turn_on", self._turn_on)
        self.hass.services.async_register("switch", "turn_off", self._turn_off)

    async def _set_value(self, call: ServiceCall) -> None:
        self.writes.append((dt_util.utcnow(), int(call.data["value"])))
        self.hass.states.async_set(call.data["entity_id"], str(call.data["value"]), AMPERE)
        set_charger_delivered_a(self.hass, self.name, call.data["value"])

    async def _turn_on(self, call: ServiceCall) -> None:
        self.starts += 1
        self.hass.states.async_set(f"switch.{self.name}", "on")

    async def _turn_off(self, call: ServiceCall) -> None:
        self.stops.append(dt_util.utcnow())
        self.hass.states.async_set(f"switch.{self.name}", "off")
        set_charger_delivered_a(self.hass, self.name, 0)

    @property
    def limit(self) -> int:
        return int(float(self.hass.states.get(f"number.{self.name}_limit").state))


async def _world(hass: HomeAssistant, name: str = "cl"):
    charger = Charger(hass, f"{name}_charger")
    entry = make_entry(
        hass,
        entry_id=f"charger_{name}",
        charge_control=f"switch.{name}_charger",
        current_limit=f"number.{name}_charger_limit",
        webhook_id=f"webhook-{name}",
        title="Wallbox",
        current_control=CURRENT_CONTROL_NUMBER,
        extra={CONF_CHARGER_PLATFORM: "wallbox"},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    charger.serve()
    controller = controller_of(hass, entry.entry_id)
    assert controller.adapter.policy.min_interval_s == 90.0

    derived = set_derived_site_entities(hass, f"{name}_site", 0.0)
    site = make_site_entry(
        hass,
        entry_id=f"site_{name}",
        main_fuse_a=FUSE_A,
        safety_margin_a=1.0,
        charger_entry_ids=[entry.entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived,
        phase_wiring={
            entry.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                    *(f"sensor.{name}_charger_l{n}" for n in (1, 2, 3))
                ),
            }
        },
        active_control_enabled=True,
    )
    return charger, entry, controller, site, f"{name}_site"


async def test_an_overload_the_adapter_cannot_lower_in_time_stops_the_charge_and_writes_nothing(
    hass: HomeAssistant,
) -> None:
    with freeze_time(START) as frozen:
        charger, entry, controller, site, prefix = await _world(hass)
        await controller.async_start(amps=16)
        set_site_current_a(hass, prefix, 16.0)
        assert await hass.config_entries.async_setup(site.entry_id)
        await hass.async_block_till_done()
        site_controller = controller_of(hass, site.entry_id)
        assert charger.limit == 16 and len(charger.writes) == 1 and charger.stops == []

        # 20 s after the session start wrote the current: the house load rises and the site is over
        # the fuse. The policy will not take another write for 70 s.
        frozen.tick(20)
        set_site_current_a(hass, prefix, 22.0)
        await hass.async_block_till_done()

        decision = site_controller.regulator_decisions[entry.entry_id]
        assert decision.reason == "reducing_current_due_to_active_import_overload"
        assert len(charger.writes) == 1, "nothing was written faster than the 90 s policy allows"
        assert len(charger.stops) == 1, "the fuse could not be protected by a write, so the charge was stopped"
        assert controller.charging is False
        # The damper says what the charger really carries: nothing, it was stopped. Neither the 14 A the
        # regulator wanted and the charger never received, nor the 16 A it had before the stop.
        assert site_controller._dampers[entry.entry_id].last_written_a == 0.0


def _decision(amps: float) -> RegulatorDecision:
    return RegulatorDecision(
        proposed_current_a=amps,
        reason="increase_within_safe_uncredited_margin",
        limiting_phase="L1",
        basis={
            "L1": PhaseDecisionBasis(
                signed_active_power_w=2000.0,
                signed_active_power_age_s=5.0,
                measured_current_a=6.0,
                measured_current_age_s=5.0,
                requested_current_a=amps,
                direction="net_import",
                confirmed_direction="net_import",
            )
        },
    )


async def test_under_proposals_that_keep_changing_the_current_is_never_written_faster_than_its_policy(
    hass: HomeAssistant,
) -> None:
    with freeze_time(START) as frozen:
        charger, entry, controller, site, prefix = await _world(hass)
        await controller.async_start(amps=16)
        set_site_current_a(hass, prefix, 16.0)
        assert await hass.config_entries.async_setup(site.entry_id)
        await hass.async_block_till_done()
        site_controller = controller_of(hass, site.entry_id)
        # No deadband and no dwell, so every proposal that changes the value is tried at once.
        site_controller._dampers[entry.entry_id] = RegulatorDamper(deadband_a=0.0, dwell_s=0.0)

        # Twenty minutes, a proposal every ten seconds, never the same two in a row.
        proposals = [12.0, 14.0, 10.0, 15.0, 11.0]
        for tick in range(120):
            frozen.tick(10)
            # A reading that differs from the last one, so it is a fresh report.
            set_derived_site_entities(hass, prefix, 3680.0 + (tick % 2) * 10.0, voltage=230.0 + (tick % 2) * 0.1)
            set_charger_delivered_a(hass, "cl_charger", charger.limit + (tick % 2) * 0.1)
            await hass.async_block_till_done()
            site_controller.regulator_decisions = {entry.entry_id: _decision(proposals[tick % len(proposals)])}
            await site_controller._async_apply_active_control()

        times = [moment for moment, _ in charger.writes]
        gaps = [(later - earlier).total_seconds() for earlier, later in zip(times, times[1:], strict=False)]
        assert len(gaps) >= 10, "the loop must have written many times, or this proves nothing"
        assert min(gaps) >= 90.0, gaps
        assert charger.stops == [], "an optional change is held, it never stops the charge"
        # The damper's memory is what the charger really carries, after every refused attempt.
        assert site_controller._dampers[entry.entry_id].last_written_a == float(charger.limit)
