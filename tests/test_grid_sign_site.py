"""The grid sign's step test on a real site (`SiteCapacityController`, `site/grid_sign.py`): a charger's steps against
the site meter, the warning as a "Get started" row, its tally across a restart, and the one option its button writes.

The field report of 2026-11-05: a meter that counts export as negative, read with "shows export as positive" ticked,
so SpotNav took export for import and solar never started.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.const import (
    CONF_GRID_POWER_INVERTED,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_SITE_CURRENT_SIGNED,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.site.grid_sign import KIND_CURRENT, KIND_POWER

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import (
    admin,
    controller_of,
    separate_entities_source,
    set_charger_delivered_a,
    set_derived_site_entities,
    ws_call,
)

pytestmark = pytest.mark.usefixtures("offline_relay")

V = 230.0
HOUSE_W = 400.0
CAR_A = 10.0
SITE = "site_a"
CHARGER = "entry_a"
POWER_ROW = {"id": "grid_power_sign", "dismissible": False, "field": "grid_power_inverted", "value": False}
CURRENT_ROW = {"id": "grid_current_sign", "dismissible": False, "field": "site_current_signed", "value": False}


class World:
    """One charger on a site whose meter reports import as positive; `site_flags` are the site's sign options."""

    def __init__(self, hass: HomeAssistant, freezer: Any, derived: bool) -> None:
        self.hass = hass
        self.freezer = freezer
        self.derived = derived
        self.car_a = 0.0

    def read(self) -> None:
        """Every sensor reports again: the meter the house plus the car, the charger its own current."""
        phase_w = HOUSE_W / 3 + self.car_a * V
        if self.derived:
            set_derived_site_entities(self.hass, SITE, phase_w)
        else:
            # The meter's signed currents count import as negative (export positive).
            for phase in ("l1", "l2", "l3"):
                set_current_sensor(self.hass, f"sensor.{SITE}_{phase}", -phase_w / V)
        set_charger_delivered_a(self.hass, CHARGER, self.car_a)

    def tick(self, seconds: float = 60.0, every: float = 5.0) -> None:
        elapsed = 0.0
        while elapsed < seconds:
            self.freezer.tick(every)
            elapsed += every
            self.read()
            controller_of(self.hass, SITE)._recompute(schedule_apply=False)  # noqa: SLF001

    def step(self, on: bool) -> None:
        self.hass.states.async_set(f"switch.{CHARGER}", "on" if on else "off")
        self.car_a = CAR_A if on else 0.0
        self.tick(120.0)


async def _world(hass: HomeAssistant, freezer: Any, *, derived: bool = True, flags: dict[str, bool]) -> World:
    world = World(hass, freezer, derived)
    hass.states.async_set(f"switch.{CHARGER}", "off")
    charger = make_entry(
        hass,
        entry_id=CHARGER,
        charge_control=f"switch.{CHARGER}",
        current_limit=None,
        webhook_id=f"webhook-{CHARGER}",
        title=CHARGER,
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    world.read()
    site = make_site_entry(
        hass,
        entry_id=SITE,
        charger_entry_ids=[CHARGER],
        phase_wiring={
            CHARGER: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                    f"sensor.{CHARGER}_l1", f"sensor.{CHARGER}_l2", f"sensor.{CHARGER}_l3"
                ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED if derived else "direct_phase_current",
        derived_entities=set_derived_site_entities(hass, SITE, HOUSE_W / 3) if derived else None,
        extra_data=flags,
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    return world


def rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    entry = hass.config_entries.async_get_entry(CHARGER)
    body = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)
    return [item for item in body["setup"]["items"] if item["id"].startswith("grid_")]


async def test_a_correct_sign_gives_no_row(hass: HomeAssistant, freezer) -> None:
    world = await _world(hass, freezer, flags={CONF_GRID_POWER_INVERTED: False})
    world.tick()
    for index in range(6):
        world.step(index % 2 == 0)
    assert rows(hass) == []
    assert controller_of(hass, SITE).grid_sign_snapshot()["tallies"][KIND_POWER]["recent"] == ["consistent"] * 5


async def test_an_inverted_grid_power_asks_for_the_setting_off_and_logs_it(
    hass: HomeAssistant, freezer, caplog: pytest.LogCaptureFixture
) -> None:
    world = await _world(hass, freezer, flags={CONF_GRID_POWER_INVERTED: True})
    world.tick()
    with caplog.at_level(logging.INFO):
        world.step(True)
        world.step(False)
        assert rows(hass) == []
        world.step(True)
    assert rows(hass) == [POWER_ROW]
    raised = [record.getMessage() for record in caplog.records if "sign looks reversed" in record.getMessage()]
    assert len(raised) == 1
    assert "grid_power_inverted is True" in raised[0] and "3 of the last" in raised[0]
    # Nothing was changed by the warning itself.
    assert hass.config_entries.async_get_entry(SITE).data[CONF_GRID_POWER_INVERTED] is True


async def test_the_warning_survives_a_restart(hass: HomeAssistant, freezer, hass_storage: dict[str, Any]) -> None:
    world = await _world(hass, freezer, flags={CONF_GRID_POWER_INVERTED: True})
    world.tick()
    for index in range(3):
        world.step(index % 2 == 0)
    assert rows(hass) == [POWER_ROW]
    freezer.tick(10)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(SITE)
    await hass.async_block_till_done()
    assert rows(hass) == [POWER_ROW]
    assert hass_storage[f"{DOMAIN}.grid_sign.{SITE}"]["data"][KIND_POWER]["warned"] is True


async def test_the_button_writes_that_one_option_and_the_row_goes(
    hass: HomeAssistant, freezer, hass_ws_client, caplog: pytest.LogCaptureFixture
) -> None:
    world = await _world(hass, freezer, flags={CONF_GRID_POWER_INVERTED: True, CONF_SITE_CURRENT_SIGNED: False})
    world.tick()
    for index in range(3):
        world.step(index % 2 == 0)
    assert rows(hass) == [POWER_ROW]
    before = dict(hass.config_entries.async_get_entry(SITE).data)
    client = await admin(hass, hass_ws_client)
    with caplog.at_level(logging.INFO):
        answer = await ws_call(
            client,
            {
                "type": "spotnav/update_entity_config",
                "api_version": 1,
                "charger_id": CHARGER,
                "scope": "site",
                "expected": {"grid_power_inverted": True},
                "changes": {"grid_power_inverted": False},
            },
        )
        await hass.async_block_till_done()
    assert answer["success"] and answer["result"]["ok"], answer
    after = dict(hass.config_entries.async_get_entry(SITE).data)
    assert {key for key in after if after[key] != before.get(key)} == {CONF_GRID_POWER_INVERTED}
    assert after[CONF_GRID_POWER_INVERTED] is False
    assert rows(hass) == []
    assert any("was changed" in record.getMessage() for record in caplog.records)
    # The meter is now read the right way round: its steps count as consistent.
    world.step(False)
    assert controller_of(hass, SITE).grid_sign_snapshot()["tallies"][KIND_POWER]["recent"] == ["consistent"]


async def test_signed_phase_currents_of_the_other_sign_ask_for_the_setting_off(hass: HomeAssistant, freezer) -> None:
    world = await _world(hass, freezer, derived=False, flags={CONF_SITE_CURRENT_SIGNED: True})
    world.tick()
    for index in range(3):
        world.step(index % 2 == 0)
    assert rows(hass) == [CURRENT_ROW]
    assert controller_of(hass, SITE).grid_sign_flag(KIND_POWER) is None
    assert controller_of(hass, SITE).grid_sign_warned(KIND_CURRENT)
