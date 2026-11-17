"""Sigenergy's DC charger (EVDC): the safety review's findings, each kept as a regression test.

Three-phase only (a power written for three phases on one would draw three times the current counted), the rating
when its sensor is gone, a lowering right after a raise while the number's state lags, Start only with the session's
limit in effect, the output power read at the site's live voltage, the cap at the fuse, and the stop inside the write
interval counted for the tester.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service, MockConfigEntry

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_PHASES,
    CONF_ENTRY_TYPE,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_PHASE_WIRING,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    CURRENT_CONTROL_POWER,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_UNSUPPORTED,
    WRITE_REGULATOR,
    WRITE_SESSION_START,
)
from custom_components.spotnav.execution.chargers.sigenergy import (
    evdc_fuse_cap_a,
    kw_per_amp,
    RATING_STORE_KEY,
    RatingMemory,
)
from custom_components.spotnav.execution.controller import power_range
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.planning.planner import power_kw
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict
from custom_components.spotnav.vehicles.discovery import discover_charger_current_sources

from ..charger_helpers import adapter_for, Clock, detected_config
from ..charger_shapes import register_shape, SHAPES
from .test_sigenergy import _found, _mock_writes, _set_limit, LIMIT, OUTPUT, RATED, START, STATE, STOP

KW_400 = kw_per_amp(400.0)


def _charger_entry(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="evdc",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER, CONF_CHARGE_CONTROL: START, **data},
    )
    entry.add_to_hass(hass)
    return entry


# -- B2: three phases only --------------------------------------------------------------------------


def test_one_phase_would_draw_three_times_the_current_counted() -> None:
    """Why one phase is refused: the plan counts 230 x I on one phase, the write is sqrt(3) x 400 x I."""
    for amps in (6, 16, 32):
        written = amps * KW_400
        assert power_kw(amps, 1, 400.0) == pytest.approx(amps * 0.23, rel=0.01)
        assert written * 1000 / 230 == pytest.approx(3 * amps, rel=0.01)


async def test_a_charger_wired_as_one_phase_is_never_given_a_power(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    _charger_entry(hass, **{CONF_CHARGER_PHASES: 1})
    _set_limit(hass, 1.0)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)

    assert await adapter.async_set_current(16, reason=WRITE_SESSION_START) == ASSIGN_UNSUPPORTED
    assert calls == []
    assert adapter.diagnostics(START)["current_state"]["three_phase"] is False

    hass.config_entries.async_update_entry(
        hass.config_entries.async_get_entry("evdc"),
        data={**hass.config_entries.async_get_entry("evdc").data, CONF_CHARGER_PHASES: 3},
    )
    assert await adapter.async_set_current(16, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    adapter.cancel_pending_reads()


async def test_the_wiring_step_refuses_one_phase_for_a_dc_charger(hass: HomeAssistant) -> None:
    from custom_components.spotnav import texts
    from custom_components.spotnav.flows.charger_wiring import ChargerWiringSteps
    from custom_components.spotnav.flows.site_form import PendingSiteDetails

    await texts.async_load(hass)
    _ids, found = _found(hass)
    _charger_entry(hass, **{k: v for k, v in detected_config(found).items() if k != CONF_CHARGE_CONTROL})
    shown: list[dict[str, Any]] = []
    finished: list[bool] = []

    class Flow(ChargerWiringSteps):
        def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
            shown.append(kwargs)
            return kwargs

        async def _finish_pending_site(self) -> dict[str, Any]:
            finished.append(True)
            return {}

    flow = Flow()
    flow.hass = hass
    flow._init_charger_wiring()
    await flow._begin_charger_wiring(
        PendingSiteDetails(details={}, charger_entry_ids=["evdc"], mode="direct", skip_direct_fields=False,
                           charger_candidates={})
    )

    await flow.async_step_site_charger_wiring({"phases": 1})
    assert shown[-1]["errors"] == {"phases": "dc_charger_three_phase"}
    assert finished == [] and flow._pending_site.charger_inputs == {}

    await flow.async_step_site_charger_wiring({"phases": 3})
    assert finished == [True]
    assert flow._pending_site.charger_inputs == {"evdc": {"phases": 3}}


async def test_the_card_refuses_one_phase_for_a_dc_charger(hass: HomeAssistant) -> None:
    from custom_components.spotnav.api import entity_fields

    _ids, found = _found(hass)
    entry = _charger_entry(hass, **{k: v for k, v in detected_config(found).items() if k != CONF_CHARGE_CONTROL})
    one = entity_fields.charger_field_errors(hass, entry=entry, changes={entity_fields.FIELD_CHARGER_PHASES: "1"})
    three = entity_fields.charger_field_errors(hass, entry=entry, changes={entity_fields.FIELD_CHARGER_PHASES: "3"})

    assert entity_fields.FieldError(entity_fields.FIELD_CHARGER_PHASES, entity_fields.ERR_INVALID_VALUE) in one
    assert not [error for error in three if error.field == entity_fields.FIELD_CHARGER_PHASES]


async def test_the_dc_side_current_is_never_offered_as_a_phase_current(hass: HomeAssistant) -> None:
    """`dc_charger_charging_current` is the car battery's current: only the output power, over three phases."""
    ids = register_shape(hass, SHAPES["sigen"])
    hass.states.async_set("sensor.sigen_dc_charger_charging_current", "19.5", {"unit_of_measurement": "A",
                                                                                 "device_class": "current"})
    candidates = discover_charger_current_sources(hass, charger_device_id=ids["device_id"])
    entity_ids = {candidate.mapping.entity_id for candidate in candidates} | {
        entity_id for candidate in candidates for entity_id in dict(candidate.mapping.entity_ids or {}).values()
    }
    assert "sensor.sigen_dc_charger_charging_current" not in entity_ids
    assert [candidate.mapping.kind for candidate in candidates] == ["power"]


async def test_a_new_dc_charger_is_not_asked_for_its_phases(hass: HomeAssistant) -> None:
    from custom_components.spotnav.flows.flow import SpotNavChargingConfigFlow

    stored: list[dict[str, Any]] = []
    flow = SpotNavChargingConfigFlow()
    flow.hass = hass

    async def finish(title: str, data: dict[str, Any]) -> dict[str, Any]:
        stored.append(data)
        return {}

    flow._finish_charger_entry = finish  # type: ignore[method-assign]
    await flow._finish_charger("Sigen DC Charger", {"charger_platform": "sigen"})
    assert stored == [{"charger_platform": "sigen", CONF_CHARGER_PHASES: 3}]


# -- S3: the rating when its sensor is gone -----------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "unit"),
    [("unavailable", "kW"), ("0", "kW"), ("12000", "kW")],  # gone, not read yet, W labelled kW
)
async def test_without_a_plausible_rating_the_last_one_stands(hass: HomeAssistant, state: str, unit: str) -> None:
    _ids, found = _found(hass)
    _set_limit(hass, 1.0)
    calls = _mock_writes(hass)
    clock = Clock()
    adapter = adapter_for(hass, found, clock=clock)
    assert adapter.current.max_a() == 17  # read once: 12 kW

    hass.states.async_set(RATED, state, {"unit_of_measurement": unit})
    clock.advance(60)
    assert await adapter.async_set_current(25, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED

    assert calls[-1].data["value"] == 12
    assert power_range(adapter.current)["max_a"] == 17
    adapter.cancel_pending_reads()


async def test_with_no_rating_ever_read_the_limit_is_never_raised(hass: HomeAssistant) -> None:
    """Never the number's own maximum (100 kW): with no rating known, no more than the limit it carries."""
    _ids, found = _found(hass)
    er.async_get(hass).async_update_entity(RATED, disabled_by=er.RegistryEntryDisabler.USER)
    hass.states.async_remove(RATED)
    _set_limit(hass, 7.0)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)

    assert await adapter.async_set_current(25, reason=WRITE_SESSION_START) in ("unchanged", ASSIGN_ASSIGNED)
    assert all(call.data["value"] <= 7.0 for call in calls)
    assert adapter.current.max_a() == 10
    # A lowering is still written.
    assert await adapter.async_set_current(6, reason=WRITE_SESSION_START) in (ASSIGN_ASSIGNED, "rate_limited")
    adapter.cancel_pending_reads()


async def test_with_nothing_carried_and_no_rating_nothing_is_written(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    hass.states.async_remove(RATED)
    _set_limit(hass, 1.0)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)

    assert await adapter.async_set_current(16, reason=WRITE_SESSION_START) == ASSIGN_BELOW_MINIMUM
    assert calls == []


async def test_the_rating_is_kept_across_a_restart(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    memory = RatingMemory(hass)
    memory.remember(LIMIT, 12.0)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    assert hass_storage[RATING_STORE_KEY]["data"] == {"ratings": {LIMIT: 12.0}}

    hass_storage[RATING_STORE_KEY]["data"]["ratings"]["number.bad"] = 9999
    restarted = RatingMemory(hass)
    restarted.ensure_loaded(hass)
    await hass.async_block_till_done()
    assert restarted.get(LIMIT) == 12.0
    assert restarted.get("number.bad") is None, "an implausible stored rating is not taken"


async def test_disabled_rating_and_output_sensors_are_offered_to_be_enabled(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["sigen"])
    registry = er.async_get(hass)
    registry.async_update_entity(RATED, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    registry.async_update_entity(OUTPUT, disabled_by=er.RegistryEntryDisabler.INTEGRATION)

    found = detect_charger(hass, ids["device_id"])

    assert found is not None and {RATED, OUTPUT} <= set(found.disabled_useful)


# -- S4: a lowering right after a raise, while the number's state lags ----------------------------------


async def test_a_lowering_before_the_state_follows_a_raise_is_written(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    _set_limit(hass, round(10 * KW_400, 3))
    sent: list[float] = []

    async def set_value(call) -> None:  # the integration refreshes the state later, not during the write
        sent.append(call.data["value"])

    hass.services.async_register("number", "set_value", set_value)
    clock = Clock()
    adapter = adapter_for(hass, found, clock=clock)
    clock.advance(60)

    assert await adapter.async_set_current(16, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert adapter.current.setpoint_a() == 16, "the value written stands for the stale state"
    clock.advance(11)
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED

    assert sent == [11.085, 6.928]
    # Once the state shows the write, it is the state again; after `PENDING_WRITE_S` too.
    _set_limit(hass, 6.928)
    assert adapter.current.setpoint_a() == 10
    adapter.cancel_pending_reads()


async def test_a_raise_waits_for_the_settle_from_the_value_written(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    _set_limit(hass, round(10 * KW_400, 3))
    hass.services.async_register("number", "set_value", lambda call: None)
    clock = Clock()
    adapter = adapter_for(hass, found, clock=clock)
    clock.advance(60)

    assert await adapter.async_set_current(8, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    clock.advance(11)
    # The state still reads 10 A; against what was written (8 A) this is a raise, so it waits for the settle.
    assert await adapter.async_set_current(9, reason=WRITE_REGULATOR) == "rate_limited"
    adapter.cancel_pending_reads()


# -- nit: the read-back of a limit rounded to its step ---------------------------------------------------


@pytest.mark.parametrize("step", [0.001, 0.01])
@pytest.mark.parametrize("volts", [230.0, 400.0, 415.0])
def test_a_limit_rounded_down_to_its_step_reads_back_as_the_current_written(step: float, volts: float) -> None:
    from custom_components.spotnav.execution.chargers.sigenergy import READ_BACK_SLACK_KW

    per_a = kw_per_amp(volts)
    for amps in range(6, 81):
        kw = math.floor(amps * per_a / step + 1e-9) * step
        slack = max(READ_BACK_SLACK_KW, step) + 1e-6
        assert int(math.floor((kw + slack) / per_a)) == amps


# -- S6: the output power at the site's live voltage ----------------------------------------------------


async def test_a_power_source_follows_the_sites_voltage(hass: HomeAssistant) -> None:
    from custom_components.spotnav.site.site_join import backfill_profile_measured_sources

    from ..helpers import make_site_entry

    stale = source_to_dict(PhaseMeasurementSource(kind="power", entity_id=OUTPUT, voltage_between_phases_v=230.0))
    site = make_site_entry(
        hass,
        entry_id="home",
        charger_entry_ids=["evdc"],
        phase_wiring={"evdc": {"phases": 3, "phase": None, CONF_MEASURED_CURRENT_SOURCE: stale}},
        extra_data={CONF_VOLTAGE_BETWEEN_PHASES_V: 400},
    )

    filled = backfill_profile_measured_sources(hass, site)

    assert filled is not None
    assert filled[CONF_PHASE_WIRING]["evdc"][CONF_MEASURED_CURRENT_SOURCE]["voltage_between_phases_v"] == 400.0
    hass.config_entries.async_update_entry(site, data=filled)
    assert backfill_profile_measured_sources(hass, site) is None


async def test_the_site_reads_a_power_source_at_its_own_voltage(hass: HomeAssistant) -> None:
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    hass.states.async_set(OUTPUT, "11.085", {"unit_of_measurement": "kW"})
    stale = source_to_dict(PhaseMeasurementSource(kind="power", entity_id=OUTPUT, voltage_between_phases_v=230.0))
    controller = SiteCapacityController.__new__(SiteCapacityController)
    controller.hass = hass
    controller.config = {CONF_VOLTAGE_BETWEEN_PHASES_V: 400}

    reading = controller._read_charger_measured_current({CONF_MEASURED_CURRENT_SOURCE: stale})

    assert reading is not None and reading.l1.value == pytest.approx(16.0, abs=0.01)


# -- S7: the cap at the fuse -----------------------------------------------------------------------------


async def test_a_dc_charger_on_a_site_is_capped_at_the_fuse(hass: HomeAssistant) -> None:
    """20 A fuse, 1 A margin, 400 V: never more than 19 A, 13.16 kW, whatever the 25 kW charger is rated."""
    from ..helpers import make_site_entry

    _ids, found = _found(hass)
    hass.states.async_set(RATED, "25", {"unit_of_measurement": "kW"})
    _charger_entry(hass)
    site = make_site_entry(hass, entry_id="home", main_fuse_a=20.0, safety_margin_a=1.0, charger_entry_ids=["evdc"],
                           phase_wiring={"evdc": {"phases": 3, "phase": None}})
    _set_limit(hass, 1.0)
    calls = _mock_writes(hass)
    adapter = adapter_for(hass, found)

    assert evdc_fuse_cap_a(hass, "evdc") == 19
    assert await adapter.async_set_current(32, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert calls[-1].data["value"] == pytest.approx(19 * KW_400, abs=0.001)
    assert power_range(adapter.current)["max_a"] == 19

    # The site's battery import limit confirmed to act (test T3): the cap is lifted, the rating bounds it.
    hass.config_entries.async_update_entry(site, data={**site.data, "battery_control_import_limit_acts": True})
    assert evdc_fuse_cap_a(hass, "evdc") is None
    assert power_range(adapter.current)["max_a"] == 36
    adapter.cancel_pending_reads()


async def test_a_dc_charger_in_no_site_has_no_fuse_cap(hass: HomeAssistant) -> None:
    _charger_entry(hass)
    assert evdc_fuse_cap_a(hass, "evdc") is None
    assert evdc_fuse_cap_a(hass, None) is None


# -- S5 and S8: the controller ---------------------------------------------------------------------------


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Time stands still; the controller's wait before a retried start write moves it on."""
    from custom_components.spotnav.execution import controller as controller_module

    with freeze_time(datetime(2026, 11, 17, 12, 0, 0)) as frozen_time:
        waits: list[float] = []

        async def sleep(seconds: float) -> None:
            waits.append(seconds)
            frozen_time.tick(seconds)

        monkeypatch.setattr(controller_module, "power_start_retry_sleep", sleep)
        frozen_time.waits = waits
        yield frozen_time


async def _evdc_controller(hass: HomeAssistant, *, limit_kw: float):
    from ..helpers import make_entry, make_site_entry
    from ..world import controller_of, set_derived_site_entities

    _ids, found = _found(hass)
    _set_limit(hass, limit_kw)
    data = detected_config(found)
    charger = make_entry(
        hass,
        entry_id="evdc",
        charge_control=START,
        current_limit=LIMIT,
        webhook_id="webhook-evdc",
        title="Sigen DC Charger",
        current_control=CURRENT_CONTROL_POWER,
        extra={key: value for key, value in data.items() if key not in (CONF_CHARGE_CONTROL,)},
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    # After the setup: the button platform it sets up registers the real service.
    presses = async_mock_service(hass, "button", "press")
    make_site_entry(
        hass,
        entry_id="home",
        main_fuse_a=25.0,
        charger_entry_ids=[charger.entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=set_derived_site_entities(hass, "home", 2000.0),
        phase_wiring={charger.entry_id: {"phases": 3, "phase": None}},
    )
    hass.states.async_set(STATE, "occupied", {"device_class": "enum"})
    return controller_of(hass, charger.entry_id), presses, charger


async def _unload(hass: HomeAssistant, charger: MockConfigEntry) -> None:
    await hass.config_entries.async_unload(charger.entry_id)
    await hass.async_block_till_done()


async def test_start_is_not_pressed_while_the_limit_cannot_be_set(hass: HomeAssistant, frozen: Any) -> None:
    """The register holds the full 12 kW from before: the write fails twice, so Start is never pressed."""
    controller, presses, charger = await _evdc_controller(hass, limit_kw=12.0)
    tries: list[float] = []

    async def failing(call) -> None:
        tries.append(call.data["value"])
        raise HomeAssistantError("modbus timeout")

    hass.services.async_register("number", "set_value", failing)

    assert await controller.async_start(amps=10, manual=True) is False

    assert len(tries) == 2, "the write is tried again before giving up"
    assert frozen.waits == [10.0], "after the write interval the failed write took"
    assert [call.data["entity_id"] for call in presses] == []
    await _unload(hass, charger)


async def test_start_is_pressed_once_a_retried_limit_is_set(hass: HomeAssistant, frozen: Any) -> None:
    controller, presses, charger = await _evdc_controller(hass, limit_kw=12.0)
    tries: list[float] = []

    async def flaky(call) -> None:
        tries.append(call.data["value"])
        if len(tries) == 1:
            raise HomeAssistantError("modbus timeout")
        _set_limit(hass, call.data["value"])

    hass.services.async_register("number", "set_value", flaky)

    assert await controller.async_start(amps=10, manual=True) is True

    assert tries == [6.928, 6.928]
    assert [call.data["entity_id"] for call in presses] == [START]
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


async def test_start_is_pressed_when_the_carried_limit_is_already_within_the_request(hass: HomeAssistant, frozen: Any) -> None:
    controller, presses, charger = await _evdc_controller(hass, limit_kw=1.0)

    async def failing(call) -> None:
        raise HomeAssistantError("modbus timeout")

    hass.services.async_register("number", "set_value", failing)

    assert await controller.async_start(amps=10, manual=True) is True
    assert [call.data["entity_id"] for call in presses] == [START]
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


async def test_a_must_lower_inside_the_write_interval_stops_and_is_counted(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, frozen: Any
) -> None:
    controller, presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    _mock_writes(hass)
    assert await controller.async_start(amps=16, manual=True) is True
    hass.states.async_set(STATE, "charging", {"device_class": "enum"})
    await hass.async_block_till_done()

    with caplog.at_level(logging.INFO):
        result = await controller.async_apply_regulated_current(10, must_lower=True)

    assert [call.data["entity_id"] for call in presses] == [START, STOP]
    assert result.outcome != ASSIGN_ASSIGNED
    assert controller.adapter.current.interval_stops == 1
    assert controller.adapter.diagnostics(START)["current_state"]["interval_stops"] == 1
    assert "DC charger stopped to protect the fuse (write interval)" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING and "stopping the charge" in record.message]
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)
