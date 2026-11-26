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
from custom_components.spotnav.execution.chargers import sigenergy
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

    # The person's word that the plant's import limit acts lifts nothing: the lift is off until it is shown on site
    # that the limit curtails this charger.
    hass.config_entries.async_update_entry(site, data={**site.data, "battery_control_import_limit_acts": True})
    assert sigenergy.EVDC_CAP_LIFTED_BY_IMPORT_LIMIT is False
    assert evdc_fuse_cap_a(hass, "evdc") == 19
    assert power_range(adapter.current)["max_a"] == 19
    adapter.cancel_pending_reads()


async def test_the_fuse_cap_would_lift_only_while_the_import_limit_holds_now(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the lift turned on: only while the battery gate's own rule holds now, asked without this charger's own
    current; the cap stays when the site has no battery control, the rule does not hold, or the asking raises."""
    from custom_components.spotnav import runtime

    from ..helpers import make_site_entry

    _charger_entry(hass)
    site = make_site_entry(hass, entry_id="home", main_fuse_a=20.0, safety_margin_a=1.0, charger_entry_ids=["evdc"],
                           phase_wiring={"evdc": {"phases": 3, "phase": None}})
    hass.config_entries.async_update_entry(site, data={**site.data, "battery_control_import_limit_acts": True})
    monkeypatch.setattr(sigenergy, "EVDC_CAP_LIFTED_BY_IMPORT_LIMIT", True)
    assert evdc_fuse_cap_a(hass, "evdc") == 19, "no site controller: nothing to ask"

    asked: list[str | None] = []

    class Control:
        answer: Any = True

        def import_limit_guards(self, *, without_charger: str | None = None) -> bool:
            asked.append(without_charger)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    control = Control()
    monkeypatch.setattr(runtime, "site_controller_for", lambda hass, entry_id: type("Site", (), {"battery_control": control})())
    assert evdc_fuse_cap_a(hass, "evdc") is None and asked == ["evdc"]
    control.answer = False
    assert evdc_fuse_cap_a(hass, "evdc") == 19
    control.answer = RuntimeError("no reading")
    assert evdc_fuse_cap_a(hass, "evdc") == 19
    hass.config_entries.async_update_entry(site, data={**site.data, "battery_control_import_limit_acts": False})
    control.answer = True
    assert evdc_fuse_cap_a(hass, "evdc") == 19, "never without the person's word"


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


# -- the DC charger's own floor --------------------------------------------------------------------------
#
# A beta site: a Sigen DC charger on a winter surplus of 1.37-1.57 kW stayed off under the sun (`off_no_surplus`,
# 2.02 A available against a 6 A start) while Sigenergy's own surplus mode charged the same car at about 1.8 kW.
# The charger's floor is its lowest power (`EVDC_MIN_POWER_KW`) as a current per phase, not the AC pilot's 6 A.


async def test_the_dc_chargers_floor_is_its_least_power_on_the_site(hass: HomeAssistant, frozen: Any) -> None:
    from custom_components.spotnav.runtime import site_controller_for

    controller, _presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    assert await hass.config_entries.async_setup("home")
    await hass.async_block_till_done()
    site = site_controller_for(hass, "home")
    assert site is not None

    assert controller.min_current_a == 2.17
    assert site.charger_min_current_a(charger.entry_id) == 2.17
    assert site._minimum_setpoint(charger.entry_id) == 2.17  # noqa: SLF001
    # A charger the site has no controller for is reckoned as an AC charger: 6 A.
    assert site.charger_min_current_a("no-such-charger") == 6.0
    assert site._minimum_setpoint("no-such-charger") == 6  # noqa: SLF001
    controller.adapter.cancel_pending_reads()
    await hass.config_entries.async_unload("home")
    await _unload(hass, charger)


async def test_load_balancing_holds_a_dc_charger_at_its_floor_and_pauses_below_it(
    hass: HomeAssistant, frozen: Any
) -> None:
    controller, presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    calls = _mock_writes(hass)
    assert await controller.async_start(amps=16, manual=True) is True
    hass.states.async_set(STATE, "charging", {"device_class": "enum"})
    await hass.async_block_till_done()
    frozen.tick(sigenergy.EVDC_SETTLE_S + 1)

    held = await controller.async_apply_regulated_current(2.17, must_lower=True)

    assert held.written
    assert calls[-1].data["value"] == pytest.approx(1.503, abs=0.001)
    assert [call.data["entity_id"] for call in presses] == [START]
    frozen.tick(sigenergy.EVDC_SETTLE_S + 1)

    paused = await controller.async_apply_regulated_current(2, must_lower=True)

    assert not paused.written
    assert [call.data["entity_id"] for call in presses] == [START, STOP]
    assert all(call.data["value"] >= sigenergy.EVDC_MIN_POWER_KW for call in calls)
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


async def test_a_start_within_a_small_allowance_starts_at_the_floor(hass: HomeAssistant, frozen: Any) -> None:
    """The site allows 2.7 A: the start goes out at the floor (1.50 kW), not held back below a 6 A floor, and never
    above the allowance. A start needs half an ampere above the floor (`EVDC_START_MARGIN_A`): at 2.6 A it is held
    back, so a start never takes the whole allowance."""
    controller, presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    calls = _mock_writes(hass)
    assert sigenergy.EVDC_START_MARGIN_A == 0.5
    controller.set_start_cap(lambda: 2.7)

    assert await controller.async_start(amps=16, manual=True) is True

    assert calls[-1].data["value"] == pytest.approx(1.503, abs=0.001)
    assert [call.data["entity_id"] for call in presses] == [START]
    await controller.async_stop()
    controller.set_start_cap(lambda: 2.6)
    frozen.tick(60)

    assert await controller.async_start(amps=16, manual=True) is False
    assert controller.start_held_by_balancing
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


def test_the_regulator_keeps_a_dc_charger_at_its_floor_instead_of_rounding_it_into_a_pause() -> None:
    from custom_components.spotnav.site.regulator import _decide_phase
    from custom_components.spotnav.site.site_capacity import PhaseValue, round_down_to_step

    def lowered(ceiling_a: float, min_current_a: float) -> float | None:
        # An import overload while the car draws 4 A: the ceiling is what the fuse leaves it.
        proposed, reason = _decide_phase(
            safe_uncredited_headroom_a=ceiling_a - 4.0,
            signed_active_power_w=5000.0,
            signed_active_power_age_s=1.0,
            signed_active_power_reason=None,
            voltage_v=230.0,
            requested_current_a=16.0,
            measured_current=PhaseValue(4.0, 1.0, None),
            confirmed_dir="net_import",
            max_age_s=120.0,
            zero_margin_w=50.0,
            help_margin_factor=0.5,
            min_current_a=min_current_a,
        )
        assert reason in ("reducing_current_due_to_active_import_overload", "paused_safe_current_below_charger_minimum")
        return proposed

    assert lowered(2.6, 2.17) == 2.17
    assert lowered(3.4, 2.17) == 3.0
    assert lowered(2.1, 2.17) == 0.0
    # An AC charger as before: below 6 A a pause.
    assert lowered(5.9, 6.0) == 0.0
    assert round_down_to_step(6.9, floor=6.0) == 6.0
    assert round_down_to_step(2.16, floor=2.17) == 2.0


def _sun(now: float, *, export_w: float, car_a: float = 0.0):
    from custom_components.spotnav.site.solar_surplus import SolarObservation

    phases = ("L1", "L2", "L3")
    volts = 400.0 / math.sqrt(3)
    return SolarObservation(
        now=now,
        signed_grid_w={phase: -export_w / 3 for phase in phases},
        voltage_v={phase: volts for phase in phases},
        car_delivered_a={phase: car_a for phase in phases},
        battery_w=None,
        car_phases=phases,
    )


async def test_the_sun_starts_a_dc_charger_at_its_floor_and_holds_it_there(hass: HomeAssistant, frozen: Any) -> None:
    """The field case: 1.6 kW of surplus starts the DC charger at its floor (2.17 A, 1.50 kW), where it stays while
    the surplus carries it; 1.4 kW does not start it; an AC charger still needs 6 A."""
    from dataclasses import replace
    from types import SimpleNamespace

    from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator
    from custom_components.spotnav.runtime import site_controller_for
    from custom_components.spotnav.site.solar_surplus import SolarController

    controller, _presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    assert await hass.config_entries.async_setup("home")
    await hass.async_block_till_done()
    site = site_controller_for(hass, "home")
    assert site is not None
    coordinator = SimpleNamespace(
        _charger_entry_id=charger.entry_id, _controller=controller, _solar_ceiling_a=lambda: 16.0
    )
    config = SolarExecutionCoordinator._solar_config(coordinator, site)  # noqa: SLF001
    assert (config.start_a, config.min_current_a) == (2.17, 2.17)
    assert config.stop_a == pytest.approx(1.17)
    # The sun's request goes to the charger as it is: the floor, else whole amps.
    assert SolarExecutionCoordinator._amps(coordinator, 2.17) == 2.17  # noqa: SLF001
    assert SolarExecutionCoordinator._amps(coordinator, 3.0) == 3
    config = replace(config, start_delay_s=0.0, min_off_s=0.0, verify_s=0.0)

    solar = SolarController(config)
    started = solar.observe(_sun(0.0, export_w=1600.0))
    assert (started.action, started.requested_a) == ("start", 2.17)
    # The car draws its 1.50 kW: the surplus is unchanged, the charge stays at the floor.
    for tick in range(1, 30):
        held = solar.observe(_sun(float(tick * 10), export_w=1600.0 - 1503.0, car_a=2.17))
        assert held.state == "on" and held.action == "hold", held

    assert SolarController(config).observe(_sun(0.0, export_w=1400.0)).action == "hold"
    ac = SolarController(replace(config, min_current_a=6.0, start_a=None, stop_a=None))
    assert ac.observe(_sun(0.0, export_w=1600.0)).action == "hold"
    controller.adapter.cancel_pending_reads()
    await hass.config_entries.async_unload("home")
    await _unload(hass, charger)


async def test_the_suns_floor_is_written_to_the_dc_charger_not_rounded_into_a_refusal(
    hass: HomeAssistant, frozen: Any
) -> None:
    """The sun's start at 2.17 A through the controller writes 1.50 kW and presses Start; whole amps above it."""
    controller, presses, charger = await _evdc_controller(hass, limit_kw=1.0)
    calls = _mock_writes(hass)

    assert await controller.async_start(amps=2.17, cause="solar") is True

    assert calls[-1].data["value"] == pytest.approx(1.503, abs=0.001)
    assert controller.requested_current_a == 2.17
    assert [call.data["entity_id"] for call in presses] == [START]
    controller.adapter.cancel_pending_reads()
    await _unload(hass, charger)


def test_a_dc_chargers_regulated_current_is_never_rounded_above_the_licence() -> None:
    """At 230 V the floor is 3.77 A: a yield step of 2 A from it licenses 5.77 A, which is written as 5 A, not 6."""
    from custom_components.spotnav.site.site_capacity_controller import regulated_setpoint

    floor = 3.77
    for licence in (5.77, 4.5, 3.99, 6.5, 12.9):
        written = regulated_setpoint(licence, floor)
        assert floor <= written <= licence
    assert regulated_setpoint(5.77, floor) == 5
    assert regulated_setpoint(3.77, floor) == 3.77
    assert regulated_setpoint(2.17, 2.17) == 2.17
    assert regulated_setpoint(3.0, 2.17) == 3
    assert regulated_setpoint(3.7, 2.17) == 3
    # An AC charger as before: the nearest ampere.
    assert regulated_setpoint(6.6, 6.0) == 7
    assert regulated_setpoint(6.4, 6.0) == 6


async def test_the_wiring_step_refuses_a_dc_charger_on_a_single_phase_site(hass: HomeAssistant) -> None:
    """A DC charger is driven only on three phases: on a single-phase site the step says so and never saves it."""
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

        def _pending_single_phase(self) -> bool:
            return True

    flow = Flow()
    flow.hass = hass
    flow._init_charger_wiring()
    await flow._begin_charger_wiring(
        PendingSiteDetails(details={"direct_L1": "sensor.l1"}, charger_entry_ids=["evdc"], mode="direct_phase_current",
                           skip_direct_fields=False, charger_candidates={})
    )
    # Said as the form opens, and at every submission: one phase or three, it is never taken.
    assert shown[-1]["errors"] == {"base": "dc_charger_single_phase_site"}
    for phases in (1, 3):
        await flow.async_step_site_charger_wiring({"phases": phases})
        assert shown[-1]["errors"] == {"base": "dc_charger_single_phase_site"}
    assert finished == [] and flow._pending_site.charger_inputs == {}
