"""The heat control's paths beside the relay (`heat/paths.py`, `heat/control.py`): NIBE's SG Ready through the
integration, CTC's volatile curve offset and NIBE's stored one, each behind the same rules, read-back and
foreign-writer pause as the relay. Every write goes to fake services in the test; nothing reaches a device."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import CONF_HEAT, CONF_HEAT_OFFSET_HOME
from custom_components.spotnav.heat import optin, paths
from custom_components.spotnav.heat.adapters import ADAPTERS
from custom_components.spotnav.heat.config import HeatConfig, apply_changes, read_config
from custom_components.spotnav.heat.control import HeatControl, lower_entity, raise_entity, store_key
from custom_components.spotnav.heat.facts import CapabilityStatus, HeatPumpFacts
from custom_components.spotnav.heat.relay_effect import Effect
from custom_components.spotnav.heat.runtime import lowering_states
from custom_components.spotnav.heat.shadow_rule import BandHour
from custom_components.spotnav.runtime import site_controller_for

from .test_demand_site import pilot_site
from .test_heat_api import read, update

pytestmark = pytest.mark.usefixtures("offline_relay")

MODE = "number.nibe_sg_ready_mode_46009"
API = "switch.nibe_sg_ready_api_43033"
CTC = "number.ctc_curve_adjustment_override"
NIBE = "number.nibe_heat_offset_s1_47011"
OUTDOOR = "sensor.ute"
ROOM = "sensor.vardagsrum"
SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
H = timedelta(hours=1)

MODE_A = paths.contact_key(MODE, paths.KIND_MODE, paths.ROLE_LOWER)
MODE_B = paths.contact_key(MODE, paths.KIND_MODE, paths.ROLE_RAISE)
CTC_A = paths.contact_key(CTC, paths.KIND_OFFSET, paths.ROLE_LOWER)
NIBE_A = paths.contact_key(NIBE, paths.KIND_OFFSET, paths.ROLE_LOWER)
NIBE_B = paths.contact_key(NIBE, paths.KIND_OFFSET, paths.ROLE_RAISE)


class Pump:
    """Fake `number.set_value` and `switch.turn_on/off`: each call recorded as `(entity, value)`; an entity can
    ignore calls or raise. A released CTC override reads `unknown`, as `ctc_bms` shows it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, Any]] = []
        self.ignore: set[str] = set()
        self.fail: set[str] = set()
        hass.services.async_register("number", "set_value", self._set)
        hass.services.async_register("switch", "turn_on", self._on)
        hass.services.async_register("switch", "turn_off", self._off)

    def _take(self, entity_id: str, value: Any) -> bool:
        self.calls.append((entity_id, value))
        if entity_id in self.fail:
            raise RuntimeError("pump offline")
        return entity_id not in self.ignore

    async def _set(self, call: ServiceCall) -> None:
        entity_id, value = call.data["entity_id"], float(call.data["value"])
        if self._take(entity_id, value):
            attributes = dict(self.hass.states.get(entity_id).attributes)  # type: ignore[union-attr]
            released = entity_id == CTC and value == 0.0
            self.hass.states.async_set(entity_id, "unknown" if released else str(value), attributes)

    async def _on(self, call: ServiceCall) -> None:
        if self._take(call.data["entity_id"], "on"):
            self.hass.states.async_set(call.data["entity_id"], "on")

    async def _off(self, call: ServiceCall) -> None:
        if self._take(call.data["entity_id"], "off"):
            self.hass.states.async_set(call.data["entity_id"], "off")

    def value(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        return None if state is None else state.state

    def written(self, entity_id: str) -> list[Any]:
        return [value for entity, value in self.calls if entity == entity_id]


def _register(hass: HomeAssistant, entity_id: str, platform: str, unique_id: str) -> None:
    domain, name = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(domain, platform, unique_id, suggested_object_id=name)


def _plan(control: HeatControl, now: datetime, actions: dict[int, str]) -> None:
    current = now.replace(minute=0, second=0, microsecond=0)
    control.shadow.plan = [
        BandHour(current + h * H, actions.get(h, "normal"), 21.0, "cheap_before_dear" if actions.get(h) == "preheat" else "dear_hour", 100.0)
        for h in range(-2, 12)
    ]
    control.shadow.made_at = now


async def _running(
    hass: HomeAssistant, freezer: Any, heat: dict[str, Any], actions: dict[int, str], outdoor: str = "4.0"
) -> tuple[Any, HeatControl, Pump, datetime]:
    """A site on the path `heat` names whose check passed and whose data has been good for ten minutes, at a whole
    hour, with `actions` planned from then."""
    site = await pilot_site(hass, freezer)
    pump = Pump(hass)
    hass.states.async_set(OUTDOOR, outdoor, {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(ROOM, "21.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    config = {"outdoor_sensor": OUTDOOR, "indoor_sensors": [ROOM], "control_enabled": True, **heat}
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: config})
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    await hass.async_block_till_done()
    control: HeatControl = controller.heat_control
    key = lower_entity(read_config(config))
    assert key is not None
    control.checks[key] = {"result": "passed", "outcome": "passed", "at": "2026-11-01T00:00:00+00:00"}
    now = control.site.demand.now().replace(minute=0, second=0, microsecond=0) + H
    _plan(control, now - 11 * MIN, {})
    await control.async_look(now - 11 * MIN)
    _plan(control, now, actions)
    return site, control, pump, now


async def _look(control: HeatControl, now: datetime, actions: dict[int, str] | None = None) -> None:
    if actions is not None:
        _plan(control, now, actions)
    await control.async_look(now)


def _turn_off(hass: HomeAssistant, site: Any) -> None:
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_HEAT: {**site.data[CONF_HEAT], "control_enabled": False}})


SG_API = {"control_path": "sg_ready_api", "control_entity": MODE, "control_entity_b": API}


def _sg_api(hass: HomeAssistant, mode: str = "1", api: str = "off") -> dict[str, Any]:
    _register(hass, MODE, "nibe_heatpump", "S1155-46009")
    _register(hass, API, "nibe_heatpump", "S1155-43033")
    hass.states.async_set(MODE, mode, {"min": 0, "max": 3, "step": 1})
    hass.states.async_set(API, api)
    return SG_API


def _ctc(hass: HomeAssistant, value: str = "unknown") -> dict[str, Any]:
    _register(hass, CTC, "ctc_bms", "ENTRY_1023")
    hass.states.async_set(CTC, value, {"min": -10.0, "max": 10.0, "step": 0.1, "unit_of_measurement": "°C"})
    return {"control_path": "offset", "control_entity": CTC}


def _nibe(hass: HomeAssistant, value: str = "-1") -> dict[str, Any]:
    _register(hass, NIBE, "nibe_heatpump", "F1155-47011")
    hass.states.async_set(NIBE, value, {"min": -10.0, "max": 10.0, "step": 1.0})
    return {"control_path": "offset", "control_entity": NIBE}


# ------------------------------------------------------------------------------------------------ the mapping


def test_each_path_reads_as_two_contacts() -> None:
    relay = read_config({"control_path": "relay", "control_entity": "switch.vp"})
    assert (lower_entity(relay), raise_entity(relay)) == ("switch.vp", None)
    sg = read_config({"control_path": "sg_ready", "control_entity": "switch.a", "control_entity_b": "switch.b"})
    assert (lower_entity(sg), raise_entity(sg)) == ("switch.a", "switch.b")
    api = read_config(SG_API)
    assert (lower_entity(api), raise_entity(api)) == (MODE_A, MODE_B) and paths.activation_switch(api) == API
    # The mode acts only through its activation switch: without it there is no path at all.
    assert lower_entity(read_config({**SG_API, "control_entity_b": None})) is None
    offset = read_config({"control_path": "offset", "control_entity": NIBE})
    assert (lower_entity(offset), raise_entity(offset)) == (NIBE_A, NIBE_B)
    assert paths.parse(NIBE_A) == (NIBE, "offset", "lower") and paths.parse("switch.vp") == ("switch.vp", "switch", "")
    assert paths.max_writes("switch.vp") == paths.max_writes(MODE_A) == 6 and paths.max_writes(NIBE_A) == 24
    assert paths.write_class(MODE_A) == "setting" and paths.write_class(NIBE_A) == "setting"
    assert paths.write_class(CTC_A, volatile=True) == "setpoint"


def test_the_sg_ready_api_modes_are_the_four_states_and_the_forced_raise_is_never_written() -> None:
    read = {mode: (paths.mode_closed(mode, "lower"), paths.mode_closed(mode, "raise")) for mode in range(4)}
    assert read == {0: (True, False), 1: (False, False), 2: (False, True), 3: (True, True)}
    written = {paths.mode_to_write(role, closed) for role in ("lower", "raise") for closed in (True, False)}
    assert written == {0, 1, 2}
    assert paths.mode_of("2.0") == 2 and paths.mode_of("unknown") is None and paths.mode_of("4") is None and paths.mode_of("1.5") is None


def test_the_offsets_depth_keeps_to_the_step_the_range_and_three_steps() -> None:
    nibe, ctc = paths.DEPTHS["nibe_f"], paths.DEPTHS["ctc_bms"]
    assert paths.offset_target("lower", -1.0, nibe, minimum=-10, maximum=10, step=1) == -3.0
    assert paths.offset_target("lower", -1.0, nibe, cold=True, minimum=-10, maximum=10, step=1) == -2.0
    assert paths.offset_target("raise", -1.0, nibe, minimum=-10, maximum=10, step=1) == 0.0
    # At the end of the range there is less room, and none at all is no move.
    assert paths.offset_target("lower", -9.0, nibe, minimum=-10, maximum=10, step=1, home=-9.0) == -10.0
    assert paths.offset_target("lower", -10.0, nibe, minimum=-10, maximum=10, step=1, home=-10.0) is None
    assert paths.offset_target("raise", 10.0, nibe, minimum=-10, maximum=10, step=1, home=10.0) is None
    # Never further than three steps from the household's own value, whatever was found: a lowering SpotNav left
    # standing (found -2 where the household's is 0) is no start for a deeper one.
    assert paths.offset_target("lower", -2.0, nibe, minimum=-10, maximum=10, step=1, home=0.0) == -3.0
    assert paths.offset_target("lower", -3.0, nibe, minimum=-10, maximum=10, step=1, home=0.0) is None
    assert paths.offset_target("raise", 3.0, nibe, minimum=-10, maximum=10, step=1, home=0.0) is None
    # Without a known own value the bound is the plan's -3..+3.
    assert paths.offset_target("lower", -2.0, nibe, minimum=-10, maximum=10, step=1) == -3.0
    assert paths.offset_target("lower", -5.0, nibe, minimum=-10, maximum=10, step=1) is None
    # CTC's adjustment is in °C of supply, from "nothing held".
    assert paths.offset_target("lower", None, ctc, minimum=-10, maximum=10, step=0.1) == -3.0
    assert paths.offset_target("lower", None, ctc, cold=True, minimum=-10, maximum=10, step=0.1) == -2.0
    assert paths.offset_target("raise", None, ctc, minimum=-10, maximum=10, step=0.1) == 2.0
    # A coarse step never makes the move deeper than asked, and no depth passes three.
    assert paths.offset_target("lower", 0.0, nibe, step=1.5) == -1.5
    assert paths.offset_target("lower", 0.0, paths.Depth(8.0, 8.0, 8.0), step=1) == -3.0


def test_an_offset_reads_closed_only_at_exactly_what_spotnav_wrote() -> None:
    at = datetime.fromisoformat("2026-11-18T10:00:00+00:00")
    stored = paths.Snapshot(-1.0, -3.0, False, at)
    assert paths.offset_closed(-3.0, "lower", stored, False) is True
    # Any other value, a deeper one too, is someone else's: the lowering is over.
    assert paths.offset_closed(-1.0, "lower", stored, False) is False
    assert paths.offset_closed(-4.0, "lower", stored, False) is False
    # Without a move of SpotNav's a stored offset is open whatever it holds; unreadable is unreadable.
    assert paths.offset_closed(-5.0, "lower", None, False) is False
    assert paths.offset_closed(None, "lower", stored, False) is None
    # A volatile one that holds nothing is open; a value SpotNav did not write is someone else's lowering or raise.
    assert paths.offset_closed(None, "lower", None, True) is False
    assert paths.offset_closed(-2.0, "lower", None, True) is True and paths.offset_closed(-2.0, "raise", None, True) is False
    assert paths.offset_closed(1.5, "raise", None, True) is True
    held = paths.Snapshot(None, -3.0, True, at)
    assert paths.offset_closed(-3.0, "lower", held, True) is True and held.restore() == paths.RELEASE
    assert stored.restore() == -1.0 and paths.Snapshot.from_dict(stored.as_dict()) == stored


def test_the_paths_found_on_a_pump() -> None:
    def capability(kind: str, entity_id: str) -> CapabilityStatus:
        return CapabilityStatus(kind, entity_id, entity_id.split(".", 1)[0], enabled=False, available=False)  # type: ignore[arg-type]

    def pump(brand: str, *capabilities: CapabilityStatus, cloud: bool = False) -> HeatPumpFacts:
        return HeatPumpFacts("x", brand, "A pump", "sensor.bt1", cloud=cloud, capabilities=capabilities)

    s_series = pump("nibe_s", capability("offset", "number.o"), capability("sg_ready", API), capability("sg_ready", MODE))
    # Disabled entities count (the opt-in enables them); SG Ready first, since it also blocks.
    assert paths.detected_paths(s_series) == [
        {"path": "sg_ready_api", "control_entity": MODE, "control_entity_b": API},
        {"path": "offset", "control_entity": "number.o", "control_entity_b": None},
    ]
    assert paths.detected_paths(pump("nibe_f", capability("offset", NIBE))) == [{"path": "offset", "control_entity": NIBE, "control_entity_b": None}]
    assert paths.detected_paths(pump("ctc_bms", capability("offset", CTC), capability("setpoint", "number.r")))[0]["control_entity"] == CTC
    # Not through a cloud, not a direct temperature, and not where the pump has two.
    assert paths.detected_paths(pump("myuplink", capability("offset", "number.o"), cloud=True)) == []
    assert paths.detected_paths(pump("heishamon", capability("offset", "number.o"))) == []
    assert paths.detected_paths(pump("nibe_f", capability("offset", "number.o1"), capability("offset", "number.o2"))) == []
    assert paths.detected_paths(None) == []


def test_the_enable_list_is_the_adapters_own_control_entities() -> None:
    by_brand = {adapter.brand: adapter for adapter in ADAPTERS}
    for (brand, _path), keys in optin.ENABLE_KEYS.items():
        adapter = by_brand[brand]
        known = [capability.match for capability in adapter.capabilities] + [match for found in adapter.facts.values() for match in found]
        for domain, key in keys:
            assert any(match.matches(domain, f"entry-{key}") for match in known), (brand, domain, key)


def test_the_settings_take_the_sg_ready_api_as_a_number_and_a_switch(hass: HomeAssistant) -> None:
    now = datetime.fromisoformat("2026-11-18T10:00:00+00:00")
    updated, errors = apply_changes(HeatConfig(), dict(SG_API), now=now, check=lambda _entity, _role: True)
    assert errors == [] and (updated.control_path, updated.control_entity, updated.control_entity_b) == ("sg_ready_api", MODE, API)
    _same, errors = apply_changes(HeatConfig(), {**SG_API, "control_entity": "switch.annan"}, now=now, check=lambda _entity, _role: True)
    assert [(error.field, error.code) for error in errors] == [("control_entity", "wrong_kind")]
    # Another path drops the switch with the mode.
    moved, errors = apply_changes(updated, {"control_path": "offset", "control_entity": NIBE}, now=now, check=lambda _entity, _role: True)
    assert errors == [] and moved.control_entity_b is None


CLOUD_OFFSET = "number.myuplink_heating_offset_climate_system_1_47011"


async def test_the_sg_ready_api_takes_only_the_pumps_own_sg_ready_entities(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer)
    _sg_api(hass)
    _register(hass, CLOUD_OFFSET, "myuplink", "DEV-47011")
    hass.states.async_set(CLOUD_OFFSET, "0", {"min": -10, "max": 10, "step": 1})
    hass.states.async_set("switch.annan", "off")
    hass.states.async_set("input_number.eget", "1", {"min": 0, "max": 3, "step": 1})

    async def errors(changes: dict[str, Any]) -> list[tuple[str, str]]:
        answer = (await update(hass, api_version=1, site_id=site.entry_id, changes=changes, expected={}))["result"]
        return [(item["field"], item["code"]) for item in answer["field_errors"]]

    # Any other number, a cloud offset too, and any other switch are refused: SpotNav would write 0..2 to them.
    assert await errors({**SG_API, "control_entity": CLOUD_OFFSET}) == [("control_entity", "wrong_kind")]
    assert await errors({**SG_API, "control_entity": "input_number.eget"}) == [("control_entity", "wrong_kind")]
    assert await errors({**SG_API, "control_entity_b": "switch.annan"}) == [("control_entity_b", "wrong_kind")]
    # The pump's own pair is taken.
    assert await errors(dict(SG_API)) == []
    assert read_config(site.data[CONF_HEAT]).control_entity == MODE


async def test_nothing_is_written_through_a_cloud_entity_or_a_number_that_is_no_sg_ready_mode(hass: HomeAssistant, freezer) -> None:
    _register(hass, CLOUD_OFFSET, "myuplink", "DEV-47011")
    hass.states.async_set(CLOUD_OFFSET, "1", {"min": -10, "max": 10, "step": 1})
    hass.states.async_set(API, "off")
    heat = {"control_path": "sg_ready_api", "control_entity": CLOUD_OFFSET, "control_entity_b": API}
    _site_, control, pump, now = await _running(hass, freezer, heat, {0: "lower", 1: "lower"})
    await _look(control, now)
    await _look(control, now + MIN)
    assert pump.calls == [] and control.reason == "no_relay"
    # A switch behind the cloud on the relay path: the same.
    _register(hass, "switch.myuplink_lux_50004", "myuplink", "DEV-50004")
    hass.states.async_set("switch.myuplink_lux_50004", "off")
    entry = hass.config_entries.async_get_entry(control.site.entry_id)
    assert entry is not None
    relay = {**entry.data[CONF_HEAT], "control_path": "relay", "control_entity": "switch.myuplink_lux_50004", "control_entity_b": None}
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HEAT: relay})
    control.checks["switch.myuplink_lux_50004"] = {"result": "passed"}
    await _look(control, now + 2 * MIN)
    await _look(control, now + 3 * MIN)
    assert pump.calls == [] and control.reason == "no_relay"


# ------------------------------------------------------------------------------------------------ SG Ready via the API


async def test_the_sg_ready_api_blocks_in_a_lowered_hour_with_its_switch_on_first(hass: HomeAssistant, freezer) -> None:
    site, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert pump.calls == [(API, "on"), (MODE, 0.0)]
    status = control.status()
    assert status["lowered"] is True and status["state"] == "running" and status["path"] == "sg_ready_api" and status["untested"] is True
    assert control.activation == {"entity": API, "mode": MODE}
    # Back to normal at the run's end; the switch stays on while SpotNav controls.
    await _look(control, now + 2 * H, {})
    await _look(control, now + 2 * H + 10 * MIN, {})
    assert pump.calls == [(API, "on"), (MODE, 0.0), (MODE, 1.0)] and pump.value(API) == "on"
    assert control.writes[control._day(now)] == 2  # noqa: SLF001 - the mode's two writes; the switch is counted apart
    assert control.diagnostics()["writes"]["activations"]["used"] == 1
    # The hand-back: the mode is normal, then the switch goes off.
    _turn_off(hass, site)
    await _look(control, now + 2 * H + 11 * MIN, {})
    await _look(control, now + 2 * H + 12 * MIN, {})
    assert pump.calls[3:] == [(API, "off")] and control.activation is None


async def test_turning_control_off_while_blocked_writes_normal_and_then_the_switch_off(hass: HomeAssistant, freezer) -> None:
    site, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    _turn_off(hass, site)
    await _look(control, now + 10 * MIN)
    assert pump.calls == [(API, "on"), (MODE, 0.0), (MODE, 1.0), (API, "off")]
    assert pump.value(MODE) == "1.0" and pump.value(API) == "off" and not control.held


async def test_a_switch_that_was_on_already_is_not_spotnavs_to_turn_off(hass: HomeAssistant, freezer) -> None:
    site, control, pump, now = await _running(hass, freezer, _sg_api(hass, api="on"), {0: "lower", 1: "lower"})
    await _look(control, now)
    _turn_off(hass, site)
    await _look(control, now + 10 * MIN)
    await _look(control, now + 20 * MIN)
    assert pump.calls == [(MODE, 0.0), (MODE, 1.0)] and pump.value(API) == "on" and control.activation is None


async def test_the_sg_ready_api_goes_through_normal_and_never_writes_the_forced_raise(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "preheat", 1: "lower", 2: "lower"})
    await _look(control, now)
    assert pump.written(MODE) == [2.0] and control.status()["raised"] is True
    # The lowered run begins: normal first, read back, and the block only once normal has read for the dead time.
    await _look(control, now + H, {0: "lower", 1: "lower"})
    assert pump.written(MODE) == [2.0, 1.0]
    await _look(control, now + H + 5 * SEC, {0: "lower", 1: "lower"})
    assert pump.written(MODE) == [2.0, 1.0]
    await _look(control, now + H + 30 * SEC, {0: "lower", 1: "lower"})
    assert pump.written(MODE) == [2.0, 1.0, 0.0]
    await _look(control, now + 3 * H, {})
    assert pump.written(MODE) == [2.0, 1.0, 0.0, 1.0]


async def test_a_block_ending_into_a_raise_writes_normal_first(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "lower", 1: "preheat"})
    await _look(control, now)
    await _look(control, now + H, {0: "preheat"})
    await _look(control, now + H + 5 * SEC, {0: "preheat"})
    assert pump.written(MODE) == [0.0, 1.0]
    await _look(control, now + H + 30 * SEC, {0: "preheat"})
    await _look(control, now + 2 * H, {})
    values = pump.written(MODE)
    assert values == [0.0, 1.0, 2.0, 1.0] and paths.MODE_FORCED not in values
    # Blocked and raised never follow each other without normal between them.
    assert all({before, after} != {0.0, 2.0} for before, after in zip(values, values[1:]))


async def test_a_forced_raise_set_by_someone_else_is_never_taken_over(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass, api="on"), {0: "lower", 1: "lower"})
    hass.states.async_set(MODE, "3", {"min": 0, "max": 3, "step": 1})
    await _look(control, now)
    await _look(control, now + 2 * MIN)
    assert pump.calls == [] and not control.held and control.state == "waiting"


@pytest.mark.parametrize("entity_id", [MODE, API])
async def test_no_raise_through_the_api_while_an_input_is_only_assumed(hass: HomeAssistant, freezer, entity_id: str) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass, api="on"), {0: "preheat", 1: "preheat"})
    state = hass.states.get(entity_id)
    assert state is not None
    hass.states.async_set(entity_id, state.state, {**state.attributes, "assumed_state": True})
    await _look(control, now)
    await _look(control, now + MIN)
    assert pump.calls == [] and control.raise_decision is not None and control.raise_decision.reason == "raise_assumed_state"


async def test_a_mode_someone_else_changed_pauses_control_and_is_left_alone(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    hass.states.async_set(MODE, "1", {"min": 0, "max": 3, "step": 1})
    await _look(control, now + MIN)
    await _look(control, now + 2 * MIN + 5 * SEC)
    assert control.paused_reason == "foreign_change" and not control.held
    assert pump.calls == [(API, "on"), (MODE, 0.0)]
    # Nothing is fought; the switch SpotNav turned on is handed back once the pause has lasted a while.
    await _look(control, now + 10 * MIN)
    assert pump.calls == [(API, "on"), (MODE, 0.0), (API, "off")]


async def test_a_switch_that_cannot_be_turned_on_writes_no_block(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "lower", 1: "lower"})
    pump.fail.add(API)
    await _look(control, now)
    assert pump.written(MODE) in ([], [1.0]) and paths.MODE_BLOCK not in pump.written(MODE)
    assert control.paused_reason == "no_read_back" and not control.held


async def test_a_block_whose_mode_does_not_go_back_at_the_stop_is_ended_by_the_switch(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert pump.value(MODE) == "0.0" and pump.value(API) == "on"
    pump.fail.add(MODE)
    await control.async_shutdown()
    # The mode could not be written: the switch SpotNav turned on is the second way back.
    assert pump.calls == [(API, "on"), (MODE, 0.0), (MODE, 1.0), (API, "off")] and pump.value(API) == "off"
    assert control.held and control.activation is None
    assert [entry["what"] for entry in control.log][-1] == "deactivate_fallback"


async def test_openings_of_the_mode_that_keep_failing_turn_the_switch_off(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _sg_api(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    pump.fail.add(MODE)
    moment = now + 2 * H
    for _try in range(4):
        await _look(control, moment, {})
        moment += 6 * MIN
    assert pump.written(MODE)[-1] == 1.0 and pump.value(MODE) == "0.0"
    assert pump.value(API) == "off" and pump.written(API) == ["on", "off"] and control.activation is None
    # The mode is put back once it can be written; the switch stays off.
    pump.fail.clear()
    for _try in range(3):
        await _look(control, moment, {})
        moment += 6 * MIN
    assert pump.value(MODE) == "1.0" and not control.held and pump.written(API) == ["on", "off"]


def test_what_reads_as_a_hot_water_boost() -> None:
    assert paths.boost_on("switch.lux", "on") and not paths.boost_on("switch.lux", "off")
    assert paths.boost_on("select.temporary_lux_48132", "3 hours") and not paths.boost_on("select.temporary_lux_48132", "off")
    assert paths.boost_on("number.extra_hot_water_1006", "2") and not paths.boost_on("number.extra_hot_water_1006", "0")
    assert not paths.boost_on("number.x", "unknown") and not paths.boost_on("button.boost", "2026-11-18T10:00:00+00:00")


async def test_a_hot_water_boost_someone_started_is_never_cut_off_by_a_block(hass: HomeAssistant, freezer) -> None:
    hass.states.async_set("switch.varmvatten_lux", "off")
    _site_, control, pump, now = await _running(
        hass, freezer, {**_sg_api(hass), "hot_water_entity": "switch.varmvatten_lux"}, {0: "lower", 1: "lower"}
    )
    hass.states.async_set("switch.varmvatten_lux", "on")
    await _look(control, now)
    # No block starts on it.
    assert pump.calls == [] and control.safety == "hot_water" and control.status()["reason"] == "hot_water"
    hass.states.async_set("switch.varmvatten_lux", "off")
    await _look(control, now + 15 * MIN)
    assert pump.calls == [(API, "on"), (MODE, 0.0)] and control.held
    # Started during the block: the block ends at once, so the boost runs.
    hass.states.async_set("switch.varmvatten_lux", "on")
    await _look(control, now + 20 * MIN)
    assert pump.written(MODE) == [0.0, 1.0] and not control.held


async def test_a_curve_offset_goes_on_beside_a_hot_water_boost(hass: HomeAssistant, freezer) -> None:
    hass.states.async_set("switch.varmvatten_lux", "on")
    _site_, control, pump, now = await _running(
        hass, freezer, {**_nibe(hass, "-1"), "hot_water_entity": "switch.varmvatten_lux"}, {0: "lower", 1: "lower"}
    )
    await _look(control, now)
    assert pump.calls == [(NIBE, -3.0)] and control.safety is None


# ------------------------------------------------------------------------------------------------ CTC's volatile offset


async def test_ctcs_offset_is_lowered_renewed_and_released(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _ctc(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert pump.calls == [(CTC, -3.0)] and control.held and control.snapshots[CTC_A].volatile is True
    # The watchdog: written again within the controller's five minutes, and not at every look.
    await _look(control, now + 30 * SEC)
    await _look(control, now + 90 * SEC)
    assert pump.calls == [(CTC, -3.0)]
    await _look(control, now + 2 * MIN)
    await _look(control, now + 3 * MIN)
    await _look(control, now + 4 * MIN)
    assert pump.calls == [(CTC, -3.0)] * 3
    day = control._day(now)  # noqa: SLF001
    assert control.renewals[day] == 2 and control.writes[day] == 1
    gaps = [later - earlier for earlier, later in zip([now, now + 2 * MIN], [now + 2 * MIN, now + 4 * MIN])]
    assert all(gap < paths.EXPIRY for gap in gaps)
    # The hand-back is the release: the entity holds nothing again.
    await _look(control, now + 2 * H, {})
    assert pump.calls[-1] == (CTC, 0.0) and pump.value(CTC) == "unknown" and not control.held
    await _look(control, now + 2 * H + 3 * MIN, {})
    assert pump.calls[-1] == (CTC, 0.0) and CTC_A not in control.snapshots
    assert control.diagnostics()["writes"]["class"] == "setpoint"


async def test_ctcs_offset_is_shallower_when_cold_and_raised_where_the_plan_raises(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _ctc(hass), {0: "lower", 1: "lower", 3: "preheat"}, outdoor="-6.0")
    await _look(control, now)
    assert pump.calls == [(CTC, -2.0)]
    await _look(control, now + 2 * H, {1: "preheat"})
    hass.states.async_set(OUTDOOR, "4.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    await _look(control, now + 3 * H, {0: "preheat"})
    await _look(control, now + 3 * H + 30 * SEC, {0: "preheat"})
    assert pump.written(CTC) == [-2.0, 0.0, 2.0] and control.raised
    await _look(control, now + 4 * H, {})
    assert pump.written(CTC)[-1] == 0.0 and not control.raised


async def test_a_renewal_that_fails_past_the_expiry_ends_the_lowering_for_the_day(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _ctc(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    pump.fail.add(CTC)
    await _look(control, now + 2 * MIN)
    await _look(control, now + 4 * MIN)
    assert control.held and control.paused_until is None
    await _look(control, now + 5 * MIN)
    assert control.paused_reason == "no_read_back"
    # The release is tried again until it reads back; the controller has forgotten the value by itself meanwhile.
    pump.fail.clear()
    await _look(control, now + 11 * MIN)
    assert pump.calls[-1] == (CTC, 0.0) and not control.held and pump.value(CTC) == "unknown"


async def test_an_override_someone_else_holds_on_ctc_is_left_alone(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _ctc(hass, "1.5"), {0: "lower", 1: "lower"})
    await _look(control, now)
    await _look(control, now + 2 * MIN)
    assert pump.calls == [] and not control.held and control.safety == "sg_ready_b"


async def test_home_assistants_restart_finds_ctcs_override_gone(hass: HomeAssistant, freezer, hass_storage: dict[str, Any]) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _ctc(hass), {0: "lower", 1: "lower"})
    await _look(control, now)
    await control._async_save_now()  # noqa: SLF001
    kept = dict(hass_storage[store_key(control.site.entry_id)]["data"])
    control.held = False  # the old one's stop writes nothing more
    await control.async_shutdown()
    # The integration comes up with nothing held: the entity reads unknown, the pump is back on its own curve.
    hass.states.async_set(CTC, "unknown", dict(hass.states.get(CTC).attributes))  # type: ignore[union-attr]
    fresh = HeatControl(control.hass, control.shadow)
    fresh._restore(kept)  # noqa: SLF001
    fresh._loaded = True  # noqa: SLF001
    fresh.checks[CTC_A] = {"result": "passed"}
    assert fresh.held
    await _look(fresh, now + 5 * MIN, {})
    assert not fresh.held and pump.calls == [(CTC, -3.0)]


# ------------------------------------------------------------------------------------------------ NIBE's stored offset


async def test_nibes_offset_is_lowered_from_the_value_found_and_put_back(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert pump.calls == [(NIBE, -3.0)] and control.held
    snapshot = control.snapshots[NIBE_A]
    assert (snapshot.found, snapshot.written, snapshot.volatile) == (-1.0, -3.0, False)
    # A stored setting: no renewal, however long the lowering lasts.
    for minutes in (2, 5, 30, 119):
        await _look(control, now + minutes * MIN)
    assert pump.calls == [(NIBE, -3.0)] and control.renewals == {}
    await _look(control, now + 2 * H, {})
    assert pump.calls == [(NIBE, -3.0), (NIBE, -1.0)] and pump.value(NIBE) == "-1.0" and not control.held
    assert control.writes[control._day(now)] == 2  # noqa: SLF001
    writes = control.diagnostics()["writes"]
    assert (writes["class"], writes["cap"]) == ("setting", 24)
    # Once over, the value the number holds is the household's own again.
    await _look(control, now + 2 * H + 6 * MIN, {})
    assert control.snapshots == {}


async def test_nibes_offset_is_raised_one_step_where_the_plan_raises(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "preheat"})
    await _look(control, now)
    assert pump.calls == [(NIBE, 0.0)] and control.raised
    await _look(control, now + H, {})
    assert pump.calls == [(NIBE, 0.0), (NIBE, -1.0)] and not control.raised


async def test_an_offset_at_the_end_of_its_range_is_not_written(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-10"), {0: "lower", 1: "lower"})
    await _look(control, now)
    await _look(control, now + MIN)
    assert pump.calls == [] and not control.held
    assert [entry["what"] for entry in control.log].count("close_not_possible") == 1


async def test_the_stop_puts_nibes_offset_back(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "2"), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert pump.value(NIBE) == "0.0"
    await control.async_shutdown()
    assert pump.calls == [(NIBE, 0.0), (NIBE, 2.0)] and not control.held


async def test_a_lowering_left_standing_is_put_back_at_the_next_start(hass: HomeAssistant, freezer, hass_storage: dict[str, Any]) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    real = control._async_call  # noqa: SLF001
    kept: dict[str, Any] = {}

    class Crash(BaseException):
        """Home Assistant gone right after the write reached the pump."""

    async def call(key: str, closed: bool) -> None:
        await real(key, closed)
        kept.update(hass_storage[store_key(control.site.entry_id)]["data"])
        raise Crash

    control._async_call = call  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(Crash):
        await control.async_look(now)
    # What the store held when the write went out: SpotNav holds the lowering, and the value found.
    assert pump.value(NIBE) == "-3.0" and kept["held"] is True and kept["relay"] == NIBE_A
    assert kept["snapshots"][NIBE_A]["found"] == -1.0 and kept["snapshots"][NIBE_A]["written"] == -3.0
    control._async_call = real  # type: ignore[method-assign]  # noqa: SLF001
    control.held = False  # the old one's stop writes nothing more
    await control.async_shutdown()
    fresh = HeatControl(control.hass, control.shadow)
    fresh._restore(kept)  # noqa: SLF001
    fresh._loaded = True  # noqa: SLF001
    fresh.checks[NIBE_A] = {"result": "passed"}
    await _look(fresh, now + 5 * MIN, {0: "lower"})
    assert pump.calls == [(NIBE, -3.0), (NIBE, -1.0)] and pump.value(NIBE) == "-1.0" and not fresh.held


async def test_a_lowering_changed_by_hand_meanwhile_is_not_undone_at_the_next_start(hass: HomeAssistant, freezer, hass_storage: dict[str, Any]) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    await _look(control, now)
    await control._async_save_now()  # noqa: SLF001
    kept = dict(hass_storage[store_key(control.site.entry_id)]["data"])
    control.held = False  # the old one's stop writes nothing more
    await control.async_shutdown()
    hass.states.async_set(NIBE, "1", dict(hass.states.get(NIBE).attributes))  # type: ignore[union-attr]
    fresh = HeatControl(control.hass, control.shadow)
    fresh._restore(kept)  # noqa: SLF001
    fresh._loaded = True  # noqa: SLF001
    await _look(fresh, now + 5 * MIN, {})
    assert pump.calls == [(NIBE, -3.0)] and pump.value(NIBE) == "1" and not fresh.held


def test_a_stored_offsets_moves_keep_the_plans_spacing() -> None:
    at = datetime.fromisoformat("2026-11-18T10:00:00+00:00")
    down, up = -1, 1
    assert paths.move_direction("lower", True) == down and paths.move_direction("raise", False) == down
    assert paths.move_direction("lower", False) == up and paths.move_direction("raise", True) == up
    # At most one move per 15 minutes, and none back the other way within 45.
    assert paths.move_waits([(at, down)], down, at + 14 * MIN) and not paths.move_waits([(at, down)], down, at + 15 * MIN)
    assert paths.move_waits([(at, down)], up, at + 44 * MIN) and not paths.move_waits([(at, down)], up, at + 45 * MIN)
    assert not paths.move_waits([], up, at)


async def test_a_lowering_after_a_raise_on_a_stored_offset_waits_for_the_spacing(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "preheat", 1: "lower", 2: "lower"})
    await _look(control, now)
    assert pump.calls == [(NIBE, 0.0)] and control.raised
    # The raise ends into the lowering: back to the value found at once, the lowering a quarter later.
    await _look(control, now + H, {0: "lower", 1: "lower"})
    await _look(control, now + H + 30 * SEC, {0: "lower", 1: "lower"})
    await _look(control, now + H + 14 * MIN, {0: "lower", 1: "lower"})
    assert pump.calls == [(NIBE, 0.0), (NIBE, -1.0)] and not control.held
    assert "close_waits_for_spacing" in [entry["what"] for entry in control.log]
    await _look(control, now + H + 15 * MIN, {0: "lower", 1: "lower"})
    assert pump.calls == [(NIBE, 0.0), (NIBE, -1.0), (NIBE, -3.0)] and control.held


async def test_a_room_gone_cold_puts_a_stored_offset_back_at_once(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    await _look(control, now)
    hass.states.async_set(ROOM, "20.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    await _look(control, now + 2 * MIN)
    # Within the spacing, and a reversal: the hand-back is never held back.
    assert pump.calls == [(NIBE, -3.0), (NIBE, -1.0)] and not control.held and control.safety == "room_cold"


def _homes(hass: HomeAssistant, site: Any) -> Any:
    entry = hass.config_entries.async_get_entry(site.entry_id)
    return entry.data.get(CONF_HEAT_OFFSET_HOME)


async def _store_lost(control: HeatControl, now: datetime) -> HeatControl:
    """Home Assistant comes back with the heat control's store gone (an old backup, a dead card) while a lowering
    stands: the kept move is lost, the household's own value is in the site's entry."""
    control.held = False  # the old one's stop writes nothing more
    await control.async_shutdown()
    fresh = HeatControl(control.hass, control.shadow)
    fresh._open_unknown()  # noqa: SLF001
    fresh._loaded = True  # noqa: SLF001
    key = lower_entity(control.shadow.config())
    assert key is not None
    fresh.checks[key] = {"result": "passed"}
    return fresh


async def test_a_lost_store_never_lowers_the_offset_further_and_asks_to_put_it_back(hass: HomeAssistant, freezer) -> None:
    site, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    # The household's own value is kept in the site's entry when the path is first seen.
    assert _homes(hass, site) == {NIBE: -1.0}
    await _look(control, now)
    assert pump.calls == [(NIBE, -3.0)]
    fresh = await _store_lost(control, now)
    # Nothing is written back by itself: the value SpotNav would need is not in its store.
    await _look(fresh, now + 5 * MIN, {})
    await _look(fresh, now + 6 * MIN, {})
    assert pump.calls == [(NIBE, -3.0)] and not fresh.held and _homes(hass, site) == {NIBE: -1.0}
    assert fresh.status()["offset_home"] == {"entity_id": NIBE, "value": -3.0, "home": -1.0}
    # The next lowering goes from what it reads, but never past three steps below the household's value.
    await _look(fresh, now + 2 * H, {0: "lower", 1: "lower"})
    assert pump.calls[-1] == (NIBE, -4.0) and fresh.status()["offset_home"] is None
    await _look(fresh, now + 4 * H, {})
    await _look(fresh, now + 4 * H + 6 * MIN, {})
    assert pump.calls[-1] == (NIBE, -3.0) and fresh.status()["offset_home"] == {"entity_id": NIBE, "value": -3.0, "home": -1.0}
    # A second loss of the same kind ends at the same bound, never lower.
    await _look(fresh, now + 6 * H, {0: "lower", 1: "lower"})
    again = await _store_lost(fresh, now + 6 * H)
    await _look(again, now + 6 * H + 5 * MIN, {})
    await _look(again, now + 8 * H, {0: "lower", 1: "lower"})
    assert min(value for _entity, value in pump.calls) == -4.0
    await again.async_shutdown()


async def test_the_offsets_line_is_answered_by_putting_it_back_or_keeping_it(hass: HomeAssistant, freezer) -> None:
    site, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    await _look(control, now)
    fresh = await _store_lost(control, now)
    site_controller_for(hass, site.entry_id).heat_control = fresh  # type: ignore[union-attr]
    await _look(fresh, now + 5 * MIN, {})
    await _look(fresh, now + 6 * MIN, {})
    assert fresh.offset_home is not None
    # Only an administrator answers it.
    refused = await update(hass, admin=False, api_version=1, site_id=site.entry_id, changes={}, expected={}, offset_home="restore")
    assert refused["result"]["error"] == "spotnav_not_admin" and pump.calls == [(NIBE, -3.0)]
    done = await update(hass, api_version=1, site_id=site.entry_id, changes={}, expected={}, offset_home="restore")
    assert done["result"]["ok"] is True and pump.calls == [(NIBE, -3.0), (NIBE, -1.0)]
    await _look(fresh, now + 7 * MIN, {})
    assert fresh.offset_home is None and _homes(hass, site) == {NIBE: -1.0}
    # Nothing left to answer: refused, nothing written.
    again = await update(hass, api_version=1, site_id=site.entry_id, changes={}, expected={}, offset_home="keep")
    assert again["result"]["error"] == "spotnav_offset_home_failed" and len(pump.calls) == 2
    # Changed while Home Assistant was down: shown, and kept as the household's own on the answer.
    hass.states.async_set(NIBE, "2", dict(hass.states.get(NIBE).attributes))  # type: ignore[union-attr]
    fresh._offset_seen.clear()  # noqa: SLF001 - as after a restart
    await _look(fresh, now + 8 * MIN, {})
    assert fresh.offset_home == {"entity_id": NIBE, "value": 2.0, "home": -1.0}
    kept = await update(hass, api_version=1, site_id=site.entry_id, changes={}, expected={}, offset_home="keep")
    assert kept["result"]["ok"] is True and _homes(hass, site) == {NIBE: 2.0} and len(pump.calls) == 2
    await _look(fresh, now + 9 * MIN, {})
    assert fresh.offset_home is None
    await fresh.async_shutdown()


async def test_an_offset_changed_by_hand_while_spotnav_holds_nothing_is_the_households_own(hass: HomeAssistant, freezer) -> None:
    site, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {})
    await _look(control, now)
    hass.states.async_set(NIBE, "1", dict(hass.states.get(NIBE).attributes))  # type: ignore[union-attr]
    await _look(control, now + MIN)
    assert _homes(hass, site) == {NIBE: 1.0} and control.offset_home is None and pump.calls == []


async def test_an_offset_someone_else_changed_pauses_control_and_is_left_alone(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    await _look(control, now)
    hass.states.async_set(NIBE, "-2", dict(hass.states.get(NIBE).attributes))  # type: ignore[union-attr]
    await _look(control, now + MIN)
    assert control.paused_until is None
    await _look(control, now + 2 * MIN + 5 * SEC)
    assert control.paused_reason == "foreign_change" and not control.held
    await _look(control, now + 10 * MIN)
    await _look(control, now + 3 * H, {})
    assert pump.calls == [(NIBE, -3.0)] and pump.value(NIBE) == "-2" and control.snapshots == {}


async def test_a_write_that_is_not_read_back_puts_the_value_found_back_and_pauses(hass: HomeAssistant, freezer) -> None:
    _site_, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    pump.ignore.add(NIBE)
    await _look(control, now)
    assert control.pending is not None and pump.calls == [(NIBE, -3.0)]
    await _look(control, now + 2 * MIN)
    assert control.paused_reason == "no_read_back" and not control.held
    assert pump.calls == [(NIBE, -3.0), (NIBE, -1.0)]


# ------------------------------------------------------------------------------------------------ the heat shadow


def test_the_controls_lowerings_read_as_a_relays_states() -> None:
    at = datetime.fromisoformat("2026-11-18T10:00:00+00:00")
    lowerings = [[at - 30 * H, at - 29 * H], [at - H, at], [at + H, None]]
    states = lowering_states(lowerings, at - 2 * H, at + 2 * H)
    assert states == [(at - 2 * H, False), (at - H, True), (at, False), (at + H, True)]
    # A lowering that covers the start reads closed from it.
    assert lowering_states([[at - H, None]], at, at + H) == [(at, False), (at, True)]


async def test_the_heat_shadow_sees_spotnavs_own_lowerings_on_the_pumps_own_settings(hass: HomeAssistant, freezer) -> None:
    site, control, pump, now = await _running(hass, freezer, _nibe(hass, "-1"), {0: "lower", 1: "lower"})
    await _look(control, now)
    assert control.held and pump.value(NIBE) == "-3.0"
    shadow = control.shadow
    shadow.tick(now + MIN)
    assert (shadow.relay_id, shadow.steered) == (None, NIBE_A)
    past, ahead = shadow.relay_intervals(now + MIN)
    assert past == [(now, now + MIN)] and ahead and ahead[0][0] == now + MIN
    # The house forecast, and so the joint plan, the battery and the cars, see the lowering and its rebound.
    shadow.heat_kw = lambda: (lambda _hour: 1.2)  # type: ignore[method-assign]
    shadow.effect = Effect(0.5, 1.0, 0)
    shadow._delta_key = None  # noqa: SLF001 - the effect changed under the test
    delta = shadow.forecast_delta(now + MIN)
    assert delta[now] < 0 and any(kw > 0 for kw in delta.values())
    # The card's relay block stays a relay's: nothing is shown as a relay here.
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    assert answer["relay"] is None and shadow.diagnostics()["steered"] == NIBE_A


# ------------------------------------------------------------------------------------------------ the opt-in


def _disabled_coils(hass: HomeAssistant) -> MockConfigEntry:
    owner = MockConfigEntry(domain="nibe_heatpump", entry_id="NIBE", title="S1155")
    owner.add_to_hass(hass)
    registry = er.async_get(hass)
    for entity_id, unique in ((API, "NIBE-43033"), (MODE, "NIBE-46009"), ("sensor.calc_supply_31018", "NIBE-31018"), ("number.offset_40031", "NIBE-40031")):
        domain, name = entity_id.split(".", 1)
        registry.async_get_or_create(
            domain, "nibe_heatpump", unique, suggested_object_id=name, config_entry=owner, disabled_by=er.RegistryEntryDisabler.INTEGRATION
        )
    return owner


async def test_the_opt_in_lists_and_enables_the_paths_disabled_coils(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site = await pilot_site(hass, freezer)
    owner = _disabled_coils(hass)
    reloads: list[str] = []

    async def reload(entry_id: str) -> bool:
        reloads.append(entry_id)
        return True

    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    written = await update(hass, api_version=1, site_id=site.entry_id, changes=dict(SG_API), expected={})
    assert written["result"]["ok"] is True
    config = read_config(site.data[CONF_HEAT])
    found = optin.describe(hass, config)
    assert found == {
        "integration": "S1155",
        "entities": [
            {"entity_id": API, "name": API},
            {"entity_id": MODE, "name": MODE},
            {"entity_id": "sensor.calc_supply_31018", "name": "sensor.calc_supply_31018"},
        ],
    }
    answer = (await read(hass, api_version=1, site_id=site.entry_id))["result"]
    assert answer["control"]["enable_entities"] == found and answer["control"]["path"] == "sg_ready_api"
    # Nothing is enabled by the read, by a write without the confirm, or for someone who is no administrator.
    await update(hass, api_version=1, site_id=site.entry_id, changes={"target_c": 20.5}, expected={})
    refused = await update(hass, admin=False, api_version=1, site_id=site.entry_id, changes={"target_c": 21.0}, expected={}, enable_control_entities=True)
    assert refused["result"]["error"] == "spotnav_not_admin" and reloads == []
    registry = er.async_get(hass)
    assert registry.async_get(MODE).disabled_by is not None  # type: ignore[union-attr]
    # The confirm: exactly those three, the integration reloaded once, and the offset's coil left as it was.
    done = await update(hass, api_version=1, site_id=site.entry_id, changes={"control_enabled": True}, expected={}, enable_control_entities=True)
    assert done["result"]["ok"] is True and done["result"]["config"]["control_enabled"] is True
    assert reloads == [owner.entry_id]
    assert all(registry.async_get(entity).disabled_by is None for entity in (API, MODE, "sensor.calc_supply_31018"))  # type: ignore[union-attr]
    assert registry.async_get("number.offset_40031").disabled_by is not None  # type: ignore[union-attr]
    assert optin.describe(hass, read_config(site.data[CONF_HEAT])) is None


async def test_the_opt_in_asks_nothing_on_a_relay_or_an_unknown_number(hass: HomeAssistant) -> None:
    _disabled_coils(hass)
    hass.states.async_set("switch.vp", "off")
    assert optin.describe(hass, read_config({"control_path": "relay", "control_entity": "switch.vp"})) is None
    assert optin.describe(hass, read_config({"control_path": "offset", "control_entity": "number.eget"})) is None
    found = optin.describe(hass, read_config({"control_path": "offset", "control_entity": "number.offset_40031"}))
    assert found is not None and [item["entity_id"] for item in found["entities"]] == ["number.offset_40031", "sensor.calc_supply_31018"]
