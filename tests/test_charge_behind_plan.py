"""A charge that falls behind its plan: the need is planned again during the plug-in and more is bought.

The plan counts on the charger's current times its phases (`planner.energy_per_slot_kwh`); a car that draws less (a
cold battery, a limit in the car) falls behind. Nothing watches for that as such. What plans again during a plug-in
is any of: a new reading of the car's level that moved `RECALCULATE_DELTA_PERCENT` (target only), the car ending the
charge, a manual need delivered or open again, a settings write, a plug-in or unplug, the departure passing, and the
area's price stream: every poll of the relay moves `next_attempt`, which is part of what listeners are told
(`AreaPriceSnapshot.meaningful_key`), so the preview calculates again at least every `SETTLED_POLL_SECONDS` (plus its
jitter). Each calculation counts from what was delivered (the estimate from the energy register for a target, the
counted energy for a manual need), so a car that drew half the plan's power gets more quarter-hours bought at the
next one, through the plan, with no reading from the car at all.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC, TargetSocIntent
from custom_components.spotnav.planning.planner import power_kw
from custom_components.spotnav.pricing.price_refresh import JITTER_FRACTION, SETTLED_POLL_SECONDS
from custom_components.spotnav.runtime import preview_for
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY

from .relay import serve
from .test_dashboard_api import NOW
from .world import CAPACITY, REGISTER, charger_and_car, controller_of

pytestmark = pytest.mark.usefixtures("offline_relay")

#: What the plan counts on: 16 A on three phases.
PLANNED_KW = power_kw(16, 3)
#: What the car draws: half of it.
DRAWN_KW = PLANNED_KW / 2
#: 70 % to 80 % of the car's battery, from the wall.
TARGET_NEED_KWH = 0.10 * CAPACITY / CHARGE_EFFICIENCY
MANUAL_NEED_KWH = 8.0
ATTRIBUTES = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}


def _record_starts_and_stops(hass: HomeAssistant) -> list[tuple[str, Any]]:
    """Every start and stop the charger receives, with the switch following it as a real one would."""
    calls: list[tuple[str, Any]] = []

    @callback
    def on_call(event: Any) -> None:
        service = event.data["service"]
        if service not in ("turn_on", "turn_off"):
            return
        targets = event.data.get("service_data", {}).get("entity_id", [])
        for entity_id in [targets] if isinstance(targets, str) else targets:
            calls.append((service, dt_util.utcnow()))
            hass.states.async_set(entity_id, "on" if service == "turn_on" else "off")

    hass.bus.async_listen("call_service", on_call)
    return calls


@pytest.mark.parametrize(
    ("driver", "need_kwh"),
    [(DRIVER_TARGET_SOC, TARGET_NEED_KWH), (DRIVER_MANUAL_KWH, MANUAL_NEED_KWH)],
    ids=["target-estimated", "manual-kwh"],
)
async def test_a_car_drawing_half_the_planned_power_gets_more_quarter_hours_bought(
    hass: HomeAssistant, transport: Any, driver: str, need_kwh: float
) -> None:
    serve(transport, rising=True)
    calls = _record_starts_and_stops(hass)
    extra: dict[str, Any] = {"amps": 16, "phases": 3}
    if driver == DRIVER_MANUAL_KWH:
        extra.update(driver=DRIVER_MANUAL_KWH, requested_kwh=MANUAL_NEED_KWH, target=TargetSocIntent())
    with freeze_time(NOW) as frozen:
        charger, _, soc_entity = await charger_and_car(hass, soc_percent="70", **extra)
        controller = controller_of(hass, charger.entry_id)
        plan = controller.plan
        assert plan is not None and plan.auto_owned
        first_end = plan.windows[-1][1]
        planned = sum((end - start).total_seconds() for start, end in plan.windows) / 3600 * PLANNED_KW
        assert need_kwh <= planned < need_kwh + PLANNED_KW / 4, "the plan bought just enough at the planned power"
        reading = hass.states.get(soc_entity)

        register = 1000.0
        for _step in range(48):
            frozen.tick(timedelta(minutes=5))
            if hass.states.get("switch.wallbox").state == "on":
                register += DRAWN_KW * 5 / 60
            hass.states.async_set(REGISTER, f"{register:.3f}", ATTRIBUTES)
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()

        assert hass.states.get(soc_entity) == reading, "the car never reported its level"
        starts = [at for service, at in calls if service == "turn_on"]
        assert starts[0] == dt_util.parse_datetime("2026-09-22T06:00:00+00:00")
        later = [at for at in starts if at >= first_end]
        assert later, "more was bought after the plan fell behind"
        poll = timedelta(seconds=SETTLED_POLL_SECONDS * (1 + JITTER_FRACTION))
        assert later[0] <= first_end + poll + timedelta(minutes=15), "at the price stream's next poll at the latest"
        assert register - 1000.0 >= need_kwh, "the need was delivered in the end"
        assert controller.plan is None and calls[-1][0] == "turn_off", "and the charge ended"
        snapshot = preview_for(hass, charger.entry_id).snapshot()
        assert snapshot.state == "nothing_to_charge"
