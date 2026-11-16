"""The site's heat shadow on the pilot's site (`heat/runtime.py`): the relay known to the plan (its closing counted in
what the plans see, the rebound after), the household's automation noticed, the plan kept per hour, and nothing ever
written to the relay."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

from custom_components.spotnav.const import CONF_HEAT
from custom_components.spotnav.heat.relay_effect import Effect, hour_of
from custom_components.spotnav.heat.runtime import SOURCE_UNKNOWN, coldest_by_period, merged, tight_hours
from custom_components.spotnav.runtime import site_controller_for

from .test_demand_site import pilot_site
from .test_heat_api import read

pytestmark = pytest.mark.usefixtures("offline_relay")

RELAY = "switch.vp_rele"


async def _relay_site(hass: HomeAssistant, freezer) -> tuple[object, object]:
    site = await pilot_site(hass, freezer)
    hass.states.async_set(RELAY, "off")
    hass.config_entries.async_update_entry(
        site, data={**site.data, CONF_HEAT: {"control_path": "relay", "control_entity": RELAY, "zones": []}}
    )
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    return site, controller


async def test_a_closing_by_anyone_is_counted_in_what_the_plans_see(hass: HomeAssistant, freezer) -> None:
    site, controller = await _relay_site(hass, freezer)
    shadow = controller.heat_shadow
    switched: list[ServiceCall] = []
    hass.services.async_register("switch", "turn_on", switched.append)
    hass.services.async_register("switch", "turn_off", switched.append)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "own_relay",
                    "alias": "Eget relä efter pris",
                    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.dyrt"}],
                    "actions": [{"action": "switch.turn_on", "target": {"entity_id": RELAY}}],
                }
            ]
        },
    )
    await hass.async_block_till_done()
    now = controller.demand.now()
    hass.states.async_set(RELAY, "on")
    shadow.tick(now)
    await hass.async_block_till_done()
    assert shadow.relay_id == RELAY
    assert [item.name for item in shadow.automations] == ["Eget relä efter pris"]
    source, automation = shadow.relay_source()
    assert source == SOURCE_UNKNOWN and automation is not None and automation.rule is None
    past, ahead = shadow.relay_intervals(now)
    # Someone else's closing: at most three hours from its start.
    assert ahead and ahead[-1][1] <= now + timedelta(hours=3, seconds=1)
    shadow.heat_kw = lambda: (lambda _hour: 1.2)  # type: ignore[method-assign]
    shadow.effect = Effect(0.5, 1.0, 0)
    shadow._delta_key = None  # noqa: SLF001 - the effect changed under the test
    delta = shadow.forecast_delta(now)
    current = hour_of(now)
    assert delta[current] < 0 and delta[current + timedelta(hours=4)] > 0
    assert sum(delta.values()) == pytest.approx(0.0, abs=1e-6)
    # What the plans count on: the lowered hour less, never below zero; the forecast's own figure without it.
    forecast = controller.house_forecast
    assert forecast._with_heat({current: 2.0, current + timedelta(hours=4): 1.0}, now) == {  # noqa: SLF001
        current: pytest.approx(2.0 + delta[current]),
        current + timedelta(hours=4): pytest.approx(1.0 + delta[current + timedelta(hours=4)]),
    }
    assert forecast._with_heat({current: 0.1}, now)[current] == 0.0  # noqa: SLF001
    # The card: the relay and one actionable note for the automation that drives it.
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    assert answer["relay"]["entity_id"] == RELAY and answer["relay"]["closed"] is True
    assert answer["relay"]["source"] == SOURCE_UNKNOWN
    notes = [note for note in answer["notes"] if note["code"] == "automation_steers"]
    assert notes == [
        {
            "code": "automation_steers",
            "params": {"entity_id": "automation.eget_rela_efter_pris", "name": "Eget relä efter pris", "targets": [], "relay": True},
        }
    ]
    assert any(hour["relay_min"] for hour in answer["band"]["hours"])
    # The plan's running hour is kept for the day, and the diagnostics show the shadow.
    today = now.astimezone(ZoneInfo(controller.demand.tracker.tz)).date()
    assert current in shadow.kept_actions(today)
    assert shadow.diagnostics()["relay"] == RELAY
    # Shadow only: SpotNav never switched the relay.
    assert switched == []


def test_the_joint_plans_tight_hours_and_joined_intervals() -> None:
    t0 = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
    hours = {t0: 6.0, t0 + timedelta(hours=1): 3.0}
    tight = tight_hours(
        13.8, hours, lambda hour: 7.4 if hour == t0 else 0.0, {}, lambda _hour: 1.2, Effect(0.5, 1.0, 0)
    )
    # 6.0 + 7.4 = 13.4 kW is within the 0.6 kW lowering frees of the 13.8 kW cap; 3.0 kW is not.
    assert tight == frozenset({t0})
    assert tight_hours(None, hours, lambda _hour: 0.0, {}, lambda _hour: 1.2, Effect()) == frozenset()
    assert merged([(t0, t0 + timedelta(hours=1)), (t0 + timedelta(hours=1), t0 + timedelta(hours=2))]) == [
        (t0, t0 + timedelta(hours=2))
    ]


def test_the_reviews_coldest_room_counts_each_rooms_offset() -> None:
    t0 = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
    rows = {"sensor.uppe": [{"start": t0, "mean": 19.6}], "sensor.kok": [{"start": t0.timestamp(), "mean": 20.8}]}
    # Upstairs aims 1.5 lower: 19.6 counts as 21.1, the kitchen's 20.8 is the coldest against the house.
    assert coldest_by_period(rows, {"sensor.uppe": -1.5}) == {t0: pytest.approx(20.8)}
    assert coldest_by_period(rows) == {t0: pytest.approx(19.6)}
