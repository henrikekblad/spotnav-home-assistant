"""A car whose level cannot be read is planned on, and the status says so calmly.

The field morning of 24 November: the car's cloud integration lost its login at 23:01 and its level sensor read
`unavailable` from then on. SpotNav held the evening's 88 % and the charger's energy register, yet refused to plan
(`incomplete_settings`, `missing: live_soc`) and the card asked to finish the settings, naming the raw key. Now a
target is planned from the estimate, else the last reading, else a full charge, and `soc_unreadable` says which (with
the car integration's login problem where a person can fix it), never while the car is away.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_config_flow,
    mock_integration,
    mock_platform,
    MockModule,
)

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution import charger_connection
from custom_components.spotnav.runtime import preview_for
from custom_components.spotnav.vehicles import level_problem
from tests.relay import serve as serve_prices
from tests.test_dashboard_api import NOW

from .world import REGISTER, charger_and_car, controller_of

pytestmark = pytest.mark.usefixtures("offline_relay")

_BATTERY = {"device_class": "battery", "unit_of_measurement": "%"}
_ENERGY = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}


def _status(hass: HomeAssistant, charger: Any) -> dict[str, dict[str, Any]]:
    payload = dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, charger), can_act=True)
    return {line["code"]: line["params"] for line in payload["status"]["lines"]}


async def _replan(hass: HomeAssistant, charger: Any) -> Any:
    preview = preview_for(hass, charger.entry_id)
    await preview.async_recalculate()
    await hass.async_block_till_done()
    return preview.snapshot()


async def _car_read_then_lost(hass: HomeAssistant, transport: Any, frozen: Any) -> tuple[Any, str]:
    """The car reads 40 %, the register posts after it (the baseline), then the car's sensor goes unavailable."""
    serve_prices(transport)
    charger, _car, soc_entity = await charger_and_car(hass, soc_percent="40")
    frozen.tick(timedelta(seconds=5))
    hass.states.async_set(REGISTER, "1000.1", _ENERGY)
    await hass.async_block_till_done()
    await _replan(hass, charger)
    frozen.tick(timedelta(minutes=1))
    hass.states.async_set(soc_entity, "unavailable", _BATTERY)
    await hass.async_block_till_done()
    return charger, soc_entity


async def test_an_unreadable_level_with_an_estimate_plans_from_the_estimate(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        charger, _soc = await _car_read_then_lost(hass, transport, frozen)
        # 7.8 kWh delivered since the reading's baseline (1000.0): 40 % + 7.8 x 0.9 / 77 kWh = 49.1 %.
        hass.states.async_set(REGISTER, "1007.8", _ENERGY)
        await hass.async_block_till_done()
        snapshot = await _replan(hass, charger)
        assert snapshot.state != "incomplete_settings" and "live_soc" not in snapshot.missing
        assert snapshot.soc_basis == "estimate"
        estimate = 40.0 + 7.8 * 0.9 / 77.0 * 100.0
        assert snapshot.soc_basis_percent == pytest.approx(estimate, abs=0.01)
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(77.0 * (80.0 - estimate) / 100.0 / 0.9, abs=0.05)

        # Too soon to say (a cloud poll that drops out says nothing) ...
        assert "soc_unreadable" not in _status(hass, charger)
        # ... and said once it has read nothing for a while.
        frozen.tick(timedelta(seconds=level_problem.SOC_UNREADABLE_NOTICE_S))
        status = _status(hass, charger)
        assert status["soc_unreadable"] == {
            "basis": "estimate", "soc_percent": 49.1, "read_at": None, "integration": None, "problem": None,
        }
        assert "settings_incomplete" not in status and "target_unverifiable" not in status


async def test_an_unreadable_level_without_an_estimate_plans_from_the_last_reading(
    hass: HomeAssistant, transport: Any
) -> None:
    with freeze_time(NOW) as frozen:
        charger, _soc = await _car_read_then_lost(hass, transport, frozen)
        # The register cannot be read either: no estimate, the last reading stands.
        hass.states.async_set(REGISTER, "unavailable", _ENERGY)
        frozen.tick(timedelta(seconds=level_problem.SOC_UNREADABLE_NOTICE_S))
        await hass.async_block_till_done()
        snapshot = await _replan(hass, charger)
        assert snapshot.soc_basis == "last_known" and snapshot.soc_basis_percent == pytest.approx(40.0)
        assert snapshot.soc_basis_read_at == datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(77.0 * 0.4 / 0.9, abs=0.05)
        status = _status(hass, charger)
        assert status["soc_unreadable"]["basis"] == "last_known"
        assert status["soc_unreadable"]["soc_percent"] == 40.0
        assert status["soc_unreadable"]["read_at"] == "2026-09-22T06:00:00+00:00"


async def test_a_car_never_read_plans_a_full_charge_with_a_notice(hass: HomeAssistant, transport: Any) -> None:
    serve_prices(transport)
    with freeze_time(NOW) as frozen:
        charger, _car, _soc = await charger_and_car(hass, capacity=40.0, soc_percent="unavailable")
        snapshot = await _replan(hass, charger)
        assert snapshot.state != "incomplete_settings"
        assert snapshot.soc_basis == "full_charge" and snapshot.soc_basis_percent is None
        assert snapshot.proposal is not None
        assert snapshot.proposal.requested_kwh == pytest.approx(40.0 * 0.8 / 0.9, abs=0.05)
        frozen.tick(timedelta(seconds=level_problem.SOC_UNREADABLE_NOTICE_S))
        status = _status(hass, charger)
        assert status["soc_unreadable"]["basis"] == "full_charge"
        assert status["soc_unreadable"]["soc_percent"] is None


async def test_an_expired_login_is_named_at_once_and_clears_when_the_level_reads(
    hass: HomeAssistant, transport: Any
) -> None:
    class _Flow(config_entries.ConfigFlow):
        VERSION = 1

        async def async_step_reauth(self, entry_data: Any) -> Any:
            return await self.async_step_reauth_confirm()

        async def async_step_reauth_confirm(self, user_input: Any = None) -> Any:
            return self.async_show_form(step_id="reauth_confirm")

    mock_integration(hass, MockModule("test", partial_manifest={"name": "Kia Connect"}))
    mock_platform(hass, "test.config_flow", None)
    with freeze_time(NOW) as frozen, mock_config_flow("test", _Flow):
        charger, soc_entity = await _car_read_then_lost(hass, transport, frozen)
        car_entry = hass.config_entries.async_get_entry("car_entry")
        assert car_entry is not None
        car_entry.mock_state(hass, ConfigEntryState.LOADED)
        er.async_get(hass).async_update_entity(soc_entity, config_entry_id="car_entry")
        # A title a person may hold an e-mail address in: never said, the integration's own name is.
        hass.config_entries.async_update_entry(car_entry, title="Kia Europe someone@example.com")
        car_entry.async_start_reauth(hass)
        await hass.async_block_till_done()

        # At once: a person can fix it.
        status = _status(hass, charger)
        assert status["soc_unreadable"]["problem"] == "reauth"
        assert status["soc_unreadable"]["integration"] == "Kia Connect"
        assert "@" not in repr(status), "the entry's title (an e-mail address) reached the status"

        # The login is done and the car reads again: the line is gone, and the plan is made from the reading.
        for flow in hass.config_entries.flow.async_progress():
            hass.config_entries.flow.async_abort(flow["flow_id"])
        frozen.tick(timedelta(minutes=1))
        hass.states.async_set(soc_entity, "45", _BATTERY)
        await hass.async_block_till_done()
        snapshot = await _replan(hass, charger)
        assert snapshot.soc_basis is None
        assert "soc_unreadable" not in _status(hass, charger)


async def test_a_failed_start_of_the_cars_integration_is_named(hass: HomeAssistant) -> None:
    """Named by the integration's own name, else its domain: never by the entry's title, which can hold an e-mail."""
    entry = MockConfigEntry(domain="test", entry_id="broken", title="someone@example.com")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_ERROR)
    entity = er.async_get(hass).async_get_or_create("sensor", "test", "broken_soc", config_entry=entry)
    # Not loaded: the domain.
    assert level_problem.integration_problem(hass, entity.entity_id) == ("test", "setup_error")
    mock_integration(hass, MockModule("test", partial_manifest={"name": "Car Cloud"}))
    assert level_problem.integration_problem(hass, entity.entity_id) == ("Car Cloud", "setup_error")
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    assert level_problem.integration_problem(hass, entity.entity_id) is None, "a retry is the integration's own"


async def test_an_unplugged_car_says_nothing(hass: HomeAssistant, transport: Any, monkeypatch: Any) -> None:
    with freeze_time(NOW) as frozen:
        charger, _soc = await _car_read_then_lost(hass, transport, frozen)
        frozen.tick(timedelta(seconds=level_problem.SOC_UNREADABLE_NOTICE_S))
        await _replan(hass, charger)
        assert "soc_unreadable" in _status(hass, charger)
        controller = controller_of(hass, charger.entry_id)
        monkeypatch.setattr(controller, "connection", lambda: (charger_connection.DISCONNECTED, None))
        assert "soc_unreadable" not in _status(hass, charger)
