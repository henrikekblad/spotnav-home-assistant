"""Thermia Genesis's comfort wheel as a stored heat-curve offset (`heat/paths.py`, `heat/control.py`, plan
`plans/v3_thermia_genesis.md`): found, detected and enabled by the opt-in, checked on the calculated supply alone,
lowered from the value found and put back, at most 12 writes a day, and never against someone else's change. Every
write goes to fake services in the test; nothing reaches a device."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.heat import optin, paths
from custom_components.spotnav.heat.config import read_config
from custom_components.spotnav.heat.control import HeatControl
from custom_components.spotnav.heat.detect import offset_capability, read_heat_pumps
from custom_components.spotnav.heat.relay_control import Check

from .test_heat_control_paths import MIN, SEC, H, Pump, _look, _running

pytestmark = pytest.mark.usefixtures("offline_relay")

WHEEL = "number.comfort_wheel_setting"
CALC = "sensor.system_supply_line_calculated_set_point"
DEMAND = "sensor.first_prioritised_demand"
ANCHOR = "sensor.outdoor_temperature"
WHEEL_A = paths.contact_key(WHEEL, paths.KIND_OFFSET, paths.ROLE_LOWER)
#: The integration's own number range for a temperature: no range at all.
WHEEL_ATTRIBUTES = {"min": -40.0, "max": 100.0, "step": 1.0, "unit_of_measurement": "°C"}

_ENTITIES = (
    (WHEEL, "holding_comfort_wheel_setting"),
    (CALC, "input_system_supply_line_calculated_set_point"),
    (DEMAND, "input_first_prioritised_demand"),
    (ANCHOR, "input_outdoor_temperature"),
)


def _thermia(hass: HomeAssistant, wheel: str = "20.0", *, disabled: bool = False) -> dict[str, Any]:
    """A Thermia Genesis config entry with the wheel, the calculated supply, the demand and the outdoor anchor, as
    `thermiagenesis` names them (`thermiagenesis_<register key>`, one device)."""
    owner = MockConfigEntry(domain="thermiagenesis", entry_id="THERMIA", title="Thermia Genesis")
    owner.add_to_hass(hass)
    registry = er.async_get(hass)
    for entity_id, key in _ENTITIES:
        domain, name = entity_id.split(".", 1)
        registry.async_get_or_create(
            domain,
            "thermiagenesis",
            f"thermiagenesis_{key}",
            suggested_object_id=name,
            config_entry=owner,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION if disabled else None,
        )
    if not disabled:
        hass.states.async_set(WHEEL, wheel, WHEEL_ATTRIBUTES)
        hass.states.async_set(CALC, "35.0", {"unit_of_measurement": "°C"})
        hass.states.async_set(DEMAND, "4")
        hass.states.async_set(ANCHOR, "4.0", {"unit_of_measurement": "°C"})
    return {"control_path": "offset", "control_entity": WHEEL}


# ------------------------------------------------------------------------------------------------ detection


def test_the_wheels_rules_are_the_stored_offsets_with_a_lower_cap() -> None:
    assert paths.max_writes(WHEEL_A, "thermia_genesis") == 12 and paths.max_writes(WHEEL_A, "nibe_f") == 24
    assert paths.max_writes(WHEEL_A) == 24 and paths.max_writes("switch.vp", "thermia_genesis") == 6
    assert paths.depth_of("thermia_genesis") == paths.DEPTH_THERMIA and "thermia_genesis" in paths.OFFSET_BRANDS
    assert paths.write_class(WHEEL_A) == "setting" and "thermia_genesis" in paths.SUPPLY_PROOF
    # Only a whole step inside the integration's own 10..40 is a value to move from.
    assert paths.found_fits("thermia_genesis", 20.0) and paths.found_fits("thermia_genesis", 10.0)
    assert not paths.found_fits("thermia_genesis", 9.0) and not paths.found_fits("thermia_genesis", 41.0)
    assert not paths.found_fits("thermia_genesis", 20.5) and not paths.found_fits("thermia_genesis", None)
    assert paths.found_fits("nibe_f", -10.0) and paths.found_fits(None, 0.5)


async def test_a_thermia_is_found_and_offers_its_comfort_wheel_while_still_disabled(hass: HomeAssistant) -> None:
    _thermia(hass, disabled=True)
    pumps = read_heat_pumps(hass)
    assert len(pumps) == 1 and pumps[0].brand == "thermia_genesis" and pumps[0].missing["outdoor_temp"] == "disabled"
    assert paths.detected_paths(pumps[0]) == [{"path": "offset", "control_entity": WHEEL, "control_entity_b": None}]
    # A stored setting SpotNav knows, not through a cloud.
    assert offset_capability(hass, WHEEL) == ("thermia_genesis", False, False)


async def test_the_opt_in_enables_the_wheel_the_calculated_supply_and_the_demand(hass: HomeAssistant, monkeypatch) -> None:
    _thermia(hass, disabled=True)
    reloads: list[str] = []

    async def reload(entry_id: str) -> bool:
        reloads.append(entry_id)
        return True

    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    config = read_config({"control_path": "offset", "control_entity": WHEEL})
    found = optin.describe(hass, config)
    assert found is not None and found["integration"] == "Thermia Genesis"
    assert [item["entity_id"] for item in found["entities"]] == [WHEEL, CALC, DEMAND]
    assert await optin.async_enable(hass, config) == [WHEEL, CALC, DEMAND]
    registry = er.async_get(hass)
    assert reloads == ["THERMIA"] and all(registry.async_get(entity).disabled_by is None for entity in (WHEEL, CALC, DEMAND))  # type: ignore[union-attr]
    # Only what the path needs: the outdoor anchor stays as it was.
    assert registry.async_get(ANCHOR).disabled_by is not None  # type: ignore[union-attr]
    assert optin.describe(hass, config) is None


# ------------------------------------------------------------------------------------------------ the check


async def _checking(hass: HomeAssistant, freezer: Any) -> tuple[HeatControl, Pump, datetime]:
    """A site steering the wheel whose check has not run yet."""
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass), {})
    control.checks.clear()
    control.check = Check()
    return control, pump, now


async def _looks(hass: HomeAssistant, control: HeatControl, now: datetime, minutes: int, *, moves: bool = True, demand: str = "4") -> datetime:
    """A look a minute; the calculated supply follows the wheel 3 °C a step where `moves`, as the curve shift does (a tester's Genesis)."""
    for _ in range(minutes):
        wheel = float(hass.states.get(WHEEL).state)  # type: ignore[union-attr]
        supply = 35.0 + 3.0 * (wheel - 20.0) if moves else 35.0
        hass.states.async_set(CALC, f"{supply:.1f}", {"unit_of_measurement": "°C"})
        hass.states.async_set(DEMAND, demand)
        await _look(control, now, {})
        now += MIN
    return now


async def test_the_check_passes_when_the_calculated_supply_follows_the_wheel(hass: HomeAssistant, freezer) -> None:
    control, pump, now = await _checking(hass, freezer)
    now = await _looks(hass, control, now, 15)
    assert pump.calls == [(WHEEL, 19.0), (WHEEL, 20.0)] and pump.value(WHEEL) == "20.0"
    assert control.checks[WHEEL_A]["result"] == "passed"
    await _looks(hass, control, now, 1)
    assert control.status()["state"] == "running"


async def test_the_check_fails_when_the_wheel_does_not_move_the_calculated_supply(hass: HomeAssistant, freezer) -> None:
    control, pump, now = await _checking(hass, freezer)
    now = await _looks(hass, control, now, 30, moves=False)
    # The value found is written back, and nothing more is written: the path stays read only.
    assert pump.calls == [(WHEEL, 19.0), (WHEEL, 20.0)] and pump.value(WHEEL) == "20.0"
    assert control.checks[WHEEL_A] == {"result": "failed", "outcome": "failed:no_effect", "at": control.checks[WHEEL_A]["at"]}
    status = control.status()
    assert (status["state"], status["reason"], status["check"]) == ("waiting", "check_failed", "failed")
    await _looks(hass, control, now, 60, moves=False)
    assert pump.calls == [(WHEEL, 19.0), (WHEEL, 20.0)]


async def test_the_check_judges_the_calculated_supply_alone(hass: HomeAssistant, freezer) -> None:
    control, pump, now = await _checking(hass, freezer)
    # The meter's power is never read as a proof here.
    sample = await control.async_sample(now)
    assert (sample.kw, sample.supply_c) == (None, 35.0) and control.signal
    # Without the calculated supply nothing can be judged, whatever meter the site has: nothing is written.
    hass.states.async_set(CALC, "unavailable")
    for minute in range(10):
        hass.states.async_set(DEMAND, "4")
        await _look(control, now + minute * MIN, {})
    assert pump.calls == [] and control.status()["reason"] == "no_signal"


async def test_the_check_waits_while_the_pump_does_not_heat(hass: HomeAssistant, freezer) -> None:
    control, pump, now = await _checking(hass, freezer)
    # Hot water (3), then anti-legionella (7): no try starts, nothing is written.
    now = await _looks(hass, control, now, 10, demand="3")
    now = await _looks(hass, control, now, 10, demand="7")
    assert pump.calls == [] and control.status()["state"] == "verifying"
    # Heating again: the try runs.
    now = await _looks(hass, control, now, 15)
    assert pump.calls == [(WHEEL, 19.0), (WHEEL, 20.0)] and control.checks[WHEEL_A]["result"] == "passed"


async def test_a_hot_water_run_under_a_try_ends_it_unjudged_and_puts_the_wheel_back(hass: HomeAssistant, freezer) -> None:
    control, pump, now = await _checking(hass, freezer)
    now = await _looks(hass, control, now, 6)
    assert pump.calls == [(WHEEL, 19.0)]
    await _looks(hass, control, now, 1, demand="3")
    assert pump.calls == [(WHEEL, 19.0), (WHEEL, 20.0)] and WHEEL_A not in control.checks
    assert control.check.last == "not_judgeable:not_heating"


# ------------------------------------------------------------------------------------------------ control


async def test_the_wheel_is_lowered_from_the_value_found_and_put_back(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass, "21.0"), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert pump.calls == [(WHEEL, 20.0)] and control.held
    snapshot = control.snapshots[WHEEL_A]
    assert (snapshot.found, snapshot.written, snapshot.volatile) == (21.0, 20.0, False)
    await _look(control, now + 2 * H, {})
    assert pump.calls == [(WHEEL, 20.0), (WHEEL, 21.0)] and pump.value(WHEEL) == "21.0" and not control.held
    assert control.writes[control._day(now)] == 2  # noqa: SLF001
    writes = control.diagnostics()["writes"]
    assert (writes["class"], writes["cap"]) == ("setting", 12)


async def test_the_wheel_is_lowered_one_step_also_when_cold(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass, "21.0"), {0: "lower"}, outdoor="-8.0")
    await _look(control, now)
    assert pump.calls == [(WHEEL, 20.0)]


async def test_a_raise_is_one_step_above_the_value_found(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass, "21.0"), {0: "preheat"})
    await _look(control, now)
    assert pump.calls == [(WHEEL, 22.0)] and control.raised
    await _look(control, now + H, {})
    assert pump.calls == [(WHEEL, 22.0), (WHEEL, 21.0)] and not control.raised


async def test_the_lowering_stays_inside_the_integrations_own_range(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass, "11.0"), {0: "lower", 1: "lower"})
    await _look(control, now)
    # Not -40 because the number says so: 10 is the wheel's least.
    assert pump.calls == [(WHEEL, 10.0)]


@pytest.mark.parametrize("found", ["5.0", "20.5", "45.0"])
async def test_a_wheel_found_outside_its_range_or_between_steps_is_never_moved(hass: HomeAssistant, freezer, found: str) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass, found), {0: "lower", 1: "lower", 2: "preheat"})
    await _look(control, now)
    await _look(control, now + MIN)
    assert pump.calls == [] and not control.held
    assert any(entry.get("why") == "offset_out_of_range" for entry in control.log)


async def test_twelve_writes_a_day_at_most(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass), {0: "lower", 1: "lower"})
    # Eleven used: a lowering and its hand-back would make 13.
    control.writes[control._day(now)] = 11  # noqa: SLF001
    await _look(control, now)
    assert pump.calls == [] and control.decision is not None and control.decision.reason == "write_cap"
    control.writes[control._day(now)] = 10  # noqa: SLF001
    await _look(control, now + MIN)
    assert pump.calls == [(WHEEL, 19.0)]


async def test_a_write_read_back_only_at_the_integrations_next_poll_is_taken(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass), {0: "lower", 1: "lower"})
    # The integration shows a written value only at its next poll (every 30 s).
    pump.ignore.add(WHEEL)
    await _look(control, now)
    assert control.pending is not None and pump.calls == [(WHEEL, 19.0)]
    await _look(control, now + 30 * SEC)
    assert control.pending is not None and control.paused_until is None
    hass.states.async_set(WHEEL, "19.0", WHEEL_ATTRIBUTES)
    await _look(control, now + 40 * SEC)
    assert control.pending is None and control.held and control.paused_until is None


async def test_a_write_the_integration_swallowed_puts_the_value_found_back_and_pauses(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass), {0: "lower", 1: "lower"})
    # `pythermiagenesis` logs a failed write and returns: the number never shows it.
    pump.ignore.add(WHEEL)
    await _look(control, now)
    await _look(control, now + 2 * MIN)
    assert control.paused_reason == "no_read_back" and not control.held
    assert pump.calls == [(WHEEL, 19.0), (WHEEL, 20.0)]


async def test_a_wheel_someone_else_turned_wins(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    # Turned on the pump's display (or through the integration's climate entity) while SpotNav holds 19.
    hass.states.async_set(WHEEL, "22.0", WHEEL_ATTRIBUTES)
    await _look(control, now + MIN)
    await _look(control, now + 2 * MIN + 5 * SEC)
    assert control.paused_reason == "foreign_change" and not control.held
    await _look(control, now + 3 * H, {})
    assert pump.calls == [(WHEEL, 19.0)] and pump.value(WHEEL) == "22.0" and control.snapshots == {}


async def test_the_stop_puts_the_wheel_back(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _thermia(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert pump.value(WHEEL) == "19.0"
    await control.async_shutdown()
    assert pump.calls == [(WHEEL, 19.0), (WHEEL, 20.0)] and not control.held
