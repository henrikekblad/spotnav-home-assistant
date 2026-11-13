"""Field report, 12 November (2.5.1): an EV6 (kia_uvo) and a "Testbil" (MQTT) at one charger. The Kia's cloud cache
answered the plug-in's re-read twelve seconds late with the old position away from home; the re-read at the ask
deadline made that echo look like a fresh report, ruled the EV6 out and decided the Testbil by position, and the
Kia's real answer a second later (home, plugged in) was ignored."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service, MockConfigEntry

from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles.identification import (
    _fresh,
    Candidate,
    REFRESH_ECHO_S,
    REREAD_WAIT_S,
)
from custom_components.spotnav.vehicles.vehicle_refresh import limiter_for

from .world import setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

HA_START = datetime(2026, 11, 12, 11, 0, 0, tzinfo=timezone.utc)
EV6_LEFT = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
PLUG_IN = datetime(2026, 11, 12, 16, 31, 34, 820000, tzinfo=timezone.utc)
KIA_ECHO = datetime(2026, 11, 12, 16, 31, 46, 700000, tzinfo=timezone.utc)
DEADLINE = PLUG_IN + timedelta(seconds=180)
KIA_ANSWER = datetime(2026, 11, 12, 16, 34, 35, tzinfo=timezone.utc)
AWAY = {"source_type": "gps", "latitude": 55.70, "longitude": 13.19}
HOME = {"source_type": "gps", "latitude": 55.71, "longitude": 13.20}


def _entity(hass: HomeAssistant, platform: str, device_id: str, domain: str, key: str, object_id: str, state: str,
            **attributes: Any) -> str:
    entry = er.async_get(hass).async_get_or_create(
        domain, platform, f"{device_id}_{key}", device_id=device_id, suggested_object_id=object_id,
        original_name=key.replace("_", " "),
    )
    hass.states.async_set(entry.entity_id, state, attributes)
    return entry.entity_id


class Field:
    def __init__(self, hass: HomeAssistant, freezer: Any) -> None:
        self.hass, self.freezer = hass, freezer
        self.connected: bool | None = False
        #: When each re-read was asked for (one call per car).
        self.rereads: list[datetime] = []
        #: What the next re-read does before it returns (the cars' integrations answering), once.
        self.on_reread: Callable[[], Awaitable[None]] | None = None

    async def build(self) -> None:
        hass, freezer = self.hass, self.freezer
        freezer.move_to(HA_START)
        MockConfigEntry(domain="kia_uvo", entry_id="kia").add_to_hass(hass)
        ev6 = dr.async_get(hass).async_get_or_create(config_entry_id="kia", identifiers={("kia_uvo", "ev6")}, name="EV6")
        self.ev6 = ev6.id
        _entity(hass, "kia_uvo", ev6.id, "sensor", "ev_battery_level", "ev6_ev_battery_level", "62",
                device_class="battery", unit_of_measurement="%")
        _entity(hass, "kia_uvo", ev6.id, "sensor", "ev_driving_range", "ev6_ev_range", "300",
                device_class="distance", unit_of_measurement="km")
        self.ev6_plug = _entity(hass, "kia_uvo", ev6.id, "binary_sensor", "ev_battery_is_plugged_in",
                                "ev6_ev_battery_plug", "off", device_class="plug")
        self.ev6_tracker = _entity(hass, "kia_uvo", ev6.id, "device_tracker", "location", "ev6_location", "home",
                                   **HOME)
        MockConfigEntry(domain="mqtt", entry_id="mqtt").add_to_hass(hass)
        bil = dr.async_get(hass).async_get_or_create(config_entry_id="mqtt", identifiers={("mqtt", "testbil")},
                                                     name="Testbil")
        self.testbil = bil.id
        _entity(hass, "mqtt", bil.id, "sensor", "battery_level", "testbil_battery_level", "55",
                device_class="battery", unit_of_measurement="%")
        _entity(hass, "mqtt", bil.id, "sensor", "range", "testbil_range", "250", device_class="distance",
                unit_of_measurement="km")
        _entity(hass, "mqtt", bil.id, "sensor", "battery_capacity", "testbil_battery_capacity", "64",
                device_class="energy_storage", unit_of_measurement="kWh")
        self.testbil_plug = _entity(hass, "mqtt", bil.id, "binary_sensor", "charging_cable",
                                    "testbil_charging_cable", "off", device_class="plug")
        self.testbil_tracker = _entity(hass, "mqtt", bil.id, "device_tracker", "location", "testbil_location",
                                       "home", **HOME)
        self.entry = await setup_charger(hass, title="HALO")
        controller = charger_data(hass, self.entry.entry_id).controller
        controller.adapter.vehicle_connected = lambda: self.connected  # type: ignore[method-assign]
        async_mock_service(hass, "switch", "turn_on")
        async_mock_service(hass, "switch", "turn_off")

        async def refresh(call: ServiceCall) -> None:
            self.rereads.append(dt_util.utcnow())
            answer, self.on_reread = self.on_reread, None
            if answer is not None:
                await answer()

        hass.services.async_register("homeassistant", "update_entity", refresh)
        # The refresh limiter's minute on the test's clock, as it passes in the field.
        limiter_for(hass).clock = lambda: dt_util.utcnow().timestamp()
        store = domain_data(hass).auto_store
        await store.async_update(
            self.entry.entry_id,
            mutate=lambda s: replace(s, target=TargetSocIntent(vehicle_id=ev6.id, target_percent=80.0)),
        )
        await self.observe()
        # The EV6 drove off at noon; the Kia's cloud keeps that position until the car reports again.
        await self.at(EV6_LEFT)
        self.write(self.ev6_tracker, "not_home", AWAY)
        await hass.async_block_till_done()

    def write(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None) -> None:
        old = self.hass.states.get(entity_id)
        self.hass.states.async_set(entity_id, state, dict(old.attributes) if attributes is None and old else attributes)

    async def observe(self) -> None:
        state = self.hass.states.get("switch.charger_a")
        self.hass.states.async_set("switch.charger_a", "off", {"t": state.attributes.get("t", 0) + 1 if state else 0})
        await self.hass.async_block_till_done()

    async def plug_in(self) -> None:
        await self.at(PLUG_IN)
        self.connected = True
        await self.observe()

    async def at(self, moment: datetime) -> None:
        self.fire(moment)
        await self.hass.async_block_till_done()

    def fire(self, moment: datetime) -> None:
        self.freezer.move_to(moment)
        async_fire_time_changed(self.hass, moment)

    async def turns(self) -> None:
        """Let the loop run while a re-read is still unanswered (waiting for everything would wait for it)."""
        for _ in range(20):
            await asyncio.sleep(0)

    async def kia_echo(self) -> None:
        """The Kia's cache written again twelve seconds after the plug-in's re-read: the old position, plug unknown."""
        await self.at(KIA_ECHO)
        self.write(self.ev6_tracker, "not_home")
        self.write(self.ev6_plug, "unknown")
        await self.hass.async_block_till_done()

    @property
    def identifier(self) -> Any:
        return charger_data(self.hass, self.entry.entry_id).identifier

    @property
    def target(self) -> str | None:
        return domain_data(self.hass).auto_store.settings(self.entry.entry_id).target.vehicle_id

    def verdicts(self) -> dict[str, Any]:
        return {item["vehicle_id"]: item["verdict"] for item in self.identifier.diagnostics()["evidence"]}

    def entry_now(self) -> dict[str, Any]:
        return self.identifier.diagnostics()["history"][-1]


def _away(reported: datetime, *refreshed_at: datetime) -> Candidate:
    return Candidate(
        "ev6", location=State("device_tracker.ev6", "not_home"), location_changed=EV6_LEFT,
        location_reported=reported, refreshed_at=refreshed_at,
    )


def test_an_echo_of_an_earlier_re_read_stays_an_echo_after_a_later_one() -> None:
    assert not _fresh(_away(KIA_ECHO, PLUG_IN), PLUG_IN)
    assert not _fresh(_away(KIA_ECHO, PLUG_IN, DEADLINE), PLUG_IN), "the deadline's re-read made it fresh"
    assert _fresh(_away(KIA_ECHO, DEADLINE), PLUG_IN), "no re-read before it: the car reported"
    late = PLUG_IN + timedelta(seconds=REFRESH_ECHO_S + 1)
    assert _fresh(_away(late, PLUG_IN), PLUG_IN), "after the echo window it is a report"
    # A position that changed since the plug-in is the car's report, whatever was re-read.
    moved = replace(_away(KIA_ECHO, PLUG_IN), location_changed=KIA_ECHO)
    assert _fresh(moved, PLUG_IN)


async def test_the_field_case_the_echo_stays_an_echo_and_the_kia_answer_decides(
    hass: HomeAssistant, freezer: Any
) -> None:
    field = Field(hass, freezer)
    await field.build()
    await field.plug_in()
    assert set(field.rereads) == {PLUG_IN}, "one re-read per car, at the plug-in only"
    await field.kia_echo()
    assert field.verdicts()[field.ev6] is None, "the echo of the plug-in's re-read rules nothing out"
    seen: dict[str, Any] = {}

    async def kia_answers() -> None:
        # Before the Kia answers, the plug-in is judged again (as by a minute's tick): the echo is still one.
        field.identifier._evaluate()
        seen.update(state=field.identifier.diagnostics()["state"], verdicts=field.verdicts())
        field.freezer.move_to(KIA_ANSWER)
        field.write(field.ev6_tracker, "home", HOME)
        field.write(field.ev6_plug, "on")

    field.on_reread = kia_answers
    await field.at(DEADLINE)
    assert DEADLINE in field.rereads
    assert seen == {"state": "waiting", "verdicts": {field.ev6: None, field.testbil: "likely"}}
    assert field.identifier.method == "plug_sensor" and field.target == field.ev6
    entry = field.entry_now()
    assert (entry["vehicle_id"], entry["method"], entry["corrections"]) == (field.ev6, "plug_sensor", [])
    assert entry["question"] is None, "decided by the Kia's answer, nobody asked"


async def _testbil_by_position(field: Field) -> None:
    """The plug-in, the echo, and at the deadline the EV6 genuinely reported away: the Testbil by position."""
    await field.build()
    field.write(field.ev6_tracker, "home", HOME)
    await field.hass.async_block_till_done()
    await field.plug_in()
    await field.kia_echo()

    async def ev6_drives_off() -> None:
        field.write(field.ev6_tracker, "not_home", AWAY)

    field.on_reread = ev6_drives_off
    await field.at(DEADLINE)
    assert field.verdicts()[field.ev6] == "away"
    assert field.identifier.method == "location" and field.target == field.testbil


async def test_the_ev6_plugged_in_corrects_a_decision_by_position(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await _testbil_by_position(field)
    await field.at(DEADLINE + timedelta(minutes=5))
    field.write(field.ev6_tracker, "home", HOME)
    field.write(field.ev6_plug, "on")
    await hass.async_block_till_done()
    assert field.identifier.method == "plug_sensor" and field.target == field.ev6
    entry = field.entry_now()
    assert (entry["vehicle_id"], entry["method"]) == (field.testbil, "location")
    assert [(item["vehicle_id"], item["method"]) for item in entry["corrections"]] == [(field.ev6, "plug_sensor")]
    # One correction: a later report changes nothing more.
    field.write(field.testbil_plug, "on")
    await field.at(DEADLINE + timedelta(minutes=7))
    assert field.target == field.ev6 and len(field.entry_now()["corrections"]) == 1


async def test_a_person_s_answer_ends_the_correcting(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await _testbil_by_position(field)
    assert await field.identifier.async_answer(field.testbil)
    await field.at(DEADLINE + timedelta(minutes=5))
    field.write(field.ev6_tracker, "home", HOME)
    field.write(field.ev6_plug, "on")
    await hass.async_block_till_done()
    assert field.identifier.method == "answered" and field.target == field.testbil
    assert [item["method"] for item in field.entry_now()["corrections"]] == ["answered"]


async def test_no_correction_after_the_listening_window(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await _testbil_by_position(field)
    await field.at(PLUG_IN + timedelta(minutes=31))
    field.write(field.ev6_tracker, "home", HOME)
    field.write(field.ev6_plug, "on")
    await hass.async_block_till_done()
    assert field.identifier.method == "location" and field.target == field.testbil


async def test_the_car_decided_by_position_reporting_itself_away_brings_the_question_back(
    hass: HomeAssistant, freezer: Any
) -> None:
    field = Field(hass, freezer)
    await _testbil_by_position(field)
    await field.at(DEADLINE + timedelta(minutes=5))
    field.write(field.testbil_tracker, "not_home", AWAY)
    await hass.async_block_till_done()
    assert field.identifier.diagnostics()["state"] == "asking"
    assert field.target == field.testbil, "the question decides; until then the car stays"


async def test_the_deadline_waits_for_its_re_read_but_not_past_the_cap(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await field.build()
    await field.plug_in()
    never = asyncio.Event()

    async def silent() -> None:
        await never.wait()

    field.on_reread = silent
    field.fire(DEADLINE)
    await field.turns()
    assert DEADLINE in field.rereads
    assert field.identifier.diagnostics()["state"] == "waiting", "decided before the re-read answered"
    field.fire(DEADLINE + timedelta(seconds=REREAD_WAIT_S - 2))
    await field.turns()
    assert field.identifier.diagnostics()["state"] == "waiting"
    field.fire(DEADLINE + timedelta(seconds=REREAD_WAIT_S))
    await field.turns()
    assert field.identifier.diagnostics()["state"] == "asking", "the cap passed: nothing decides, so it asks"
    never.set()
    await hass.async_block_till_done()


async def test_a_re_read_the_limiter_skips_decides_at_once(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await field.build()
    await field.plug_in()
    # A person's Refresh half a minute before the deadline: the deadline's own re-read is skipped.
    await field.at(DEADLINE - timedelta(seconds=30))
    for car in (field.ev6, field.testbil):
        limiter_for(hass).note_refresh(car)
    await field.at(DEADLINE)
    assert set(field.rereads) == {PLUG_IN}, "one re-read per car, at the plug-in only"
    assert field.identifier.diagnostics()["state"] == "asking"


async def test_an_unplug_while_the_deadline_waits_does_nothing(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await field.build()
    await field.plug_in()
    answered = asyncio.Event()

    async def slow() -> None:
        await answered.wait()

    field.on_reread = slow
    field.fire(DEADLINE)
    await field.turns()
    field.connected = False
    field.hass.states.async_set("switch.charger_a", "off", {"t": 99})
    await field.turns()
    answered.set()
    await hass.async_block_till_done()
    assert field.identifier.diagnostics()["state"] == "waiting", "an empty charger is asked nothing"
    assert field.entry_now()["question"] is None and field.target == field.ev6


async def test_an_answer_while_the_deadline_waits_is_not_asked_again(hass: HomeAssistant, freezer: Any) -> None:
    field = Field(hass, freezer)
    await field.build()
    await field.plug_in()
    answered = asyncio.Event()

    async def slow() -> None:
        await answered.wait()

    field.on_reread = slow
    field.fire(DEADLINE)
    await field.turns()
    task = hass.async_create_task(field.identifier.async_answer(field.testbil))
    await field.turns()
    answered.set()
    await hass.async_block_till_done()
    assert task.result() is True
    assert field.identifier.diagnostics()["state"] == "decided" and field.target == field.testbil
    assert field.entry_now()["question"] is None
