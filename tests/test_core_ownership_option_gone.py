"""The choice of the logic before the charge-ownership core is gone. An earlier release let a charger keep that logic
(`core_ownership: false` in its entry data); now the core decides for every charger: Configure no longer offers the
choice, and a charger whose data still carries the key has it dropped once at setup, without a reload, keeping a
charge that runs."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_CORE_OWNERSHIP
from custom_components.spotnav.execution.controller import ChargingPlan, SESSION_STORE_KEY, STORE_VERSION

from .helpers import make_entry
from .world import controller_of

SWITCH = "switch.option_gone"
ENTRY = "charger_option_gone"


def _running_plan_charge() -> dict[str, Any]:
    """Today's keys as the previous logic left them while a plan window's charge ran: no record of the core's."""
    now = dt_util.utcnow()
    start, end = now - timedelta(minutes=10), now + timedelta(minutes=50)
    plan = ChargingPlan(
        start=start.isoformat(),
        end=end.isoformat(),
        amps=10,
        phases=3,
        power_kw=None,
        energy_kwh=None,
        price_area=None,
        periods=[{"start": start.isoformat(), "end": end.isoformat()}],
        target_soc_percent=None,
        vehicle_id=None,
        auto_identity="0123456789abcdef0123456789abcdef",
        auto_settings_revision=1,
        auto_price_identity="test-price-identity",
    )
    return {
        "plan": asdict(plan),
        "requested_current_a": 10,
        "plan_charge": True,
        "charge_origin": "plan_window",
        "plugged_in_at": None,
        "target_stop": None,
        "top_off_until": None,
        "car_ended_at": None,
        "car_ended_soc": None,
        "car_ended_vehicle": None,
        "car_ended_wait": None,
        "balancing_pause": None,
        "hold": {"held": False, "overridden": False},
        "adapter_memory": {},
        "solar_credit_backoff": {"until": None, "next_s": None},
        "solar_car_ended": None,
    }


def _serve(hass: HomeAssistant) -> list[str]:
    commands: list[str] = []

    def handler(kind: str, state: str):
        async def handle(call: ServiceCall) -> None:
            commands.append(kind)
            hass.states.async_set(SWITCH, state)

        return handle

    hass.services.async_register("switch", "turn_on", handler("start", "on"))
    hass.services.async_register("switch", "turn_off", handler("stop", "off"))
    return commands


@pytest.mark.parametrize("stored", [False, True])
async def test_an_entry_that_kept_the_previous_logic_upgrades_and_keeps_its_running_plan_charge(
    hass: HomeAssistant, hass_storage: dict[str, Any], caplog: pytest.LogCaptureFixture, stored: bool
) -> None:
    hass.states.async_set(SWITCH, "on")
    hass_storage[f"spotnav.{ENTRY}"] = {
        "version": STORE_VERSION,
        "minor_version": 1,
        "key": f"spotnav.{ENTRY}",
        "data": _running_plan_charge(),
    }
    entry = make_entry(
        hass,
        entry_id=ENTRY,
        charge_control=SWITCH,
        current_limit=None,
        webhook_id="webhook-option-gone",
        title="Garage",
        extra={CONF_CORE_OWNERSHIP: stored},
    )
    caplog.set_level(logging.INFO, logger="custom_components.spotnav")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    commands = _serve(hass)

    assert CONF_CORE_OWNERSHIP not in entry.data, "dropped once at setup"
    told = [record for record in caplog.records if "previous logic is no longer available" in record.getMessage()]
    assert len(told) == (0 if stored else 1)
    assert all(record.levelno == logging.INFO for record in told)
    controller = controller_of(hass, entry.entry_id)
    assert controller.ownership_shadow.drives
    assert controller.charge_origin == "plan_window" and controller.ownership_shadow.session.owner == "plan"
    assert controller.charging and commands == [], "the running charge is left as it is"
    assert hass_storage[f"spotnav.{ENTRY}"]["data"][SESSION_STORE_KEY]["owner"] == "plan", "the core's record is written"

    hass.states.async_set(SWITCH, "on", {"report": "after the upgrade"})
    await hass.async_block_till_done()
    assert commands == [] and controller.charge_origin == "plan_window"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_configure_no_longer_offers_the_previous_logic(hass: HomeAssistant) -> None:
    hass.states.async_set(SWITCH, "off")
    entry = make_entry(
        hass,
        entry_id=ENTRY,
        charge_control=SWITCH,
        current_limit=None,
        webhook_id="webhook-option-gone",
        title="Garage",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "init"
    assert "charging_decisions" not in {str(key) for key in result["data_schema"].schema}
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
