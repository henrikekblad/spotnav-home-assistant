"""The charger's event entity: the tracker's rules, and the entity a real charger gets."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.charger_events import (
    CHARGER_EVENT_TYPES,
    ChargerEventTracker,
    ChargerFacts,
)

from .world import entity_id, setup_charger


def kinds(events: list) -> list[str]:
    return [kind for kind, _ in events]


def test_the_first_observation_is_a_baseline_not_an_event() -> None:
    tracker = ChargerEventTracker()
    assert tracker.observe(ChargerFacts(charging=True, connected=True, plan_key="p", at_risk=True)) == []


def test_charging_and_plugging_make_events_and_a_finished_charge_counts_its_energy() -> None:
    tracker = ChargerEventTracker()
    tracker.observe(ChargerFacts(charging=False, connected=False, register_kwh=100.0))

    assert kinds(tracker.observe(ChargerFacts(charging=False, connected=True, register_kwh=100.0))) == ["plugged_in"]
    started = tracker.observe(ChargerFacts(charging=True, connected=True, register_kwh=100.0))
    assert kinds(started) == ["charge_started"]
    assert tracker.observe(ChargerFacts(charging=True, connected=True, register_kwh=104.5)) == []
    finished = tracker.observe(ChargerFacts(charging=False, connected=True, register_kwh=112.25))
    assert finished == [("charge_finished", {"unplugged": False, "energy_kwh": 12.25})]
    assert kinds(tracker.observe(ChargerFacts(charging=False, connected=False, register_kwh=112.25))) == ["unplugged"]


def test_an_unknown_connection_is_never_a_plug_event() -> None:
    tracker = ChargerEventTracker()
    tracker.observe(ChargerFacts(charging=False, connected=None))
    assert tracker.observe(ChargerFacts(charging=False, connected=True)) == []
    assert tracker.observe(ChargerFacts(charging=False, connected=None)) == []


def test_a_charge_that_ends_by_unplugging_says_so_and_has_no_energy_without_a_register() -> None:
    tracker = ChargerEventTracker()
    tracker.observe(ChargerFacts(charging=True, connected=True))
    events = tracker.observe(ChargerFacts(charging=False, connected=False))
    assert events == [("unplugged", {}), ("charge_finished", {"unplugged": True})]


def test_a_new_plan_is_installed_once_and_a_risk_fires_when_it_begins() -> None:
    tracker = ChargerEventTracker()
    tracker.observe(ChargerFacts(charging=False, connected=True))
    plan = {"start": "a", "end": "b", "amps": 10}

    installed = tracker.observe(ChargerFacts(charging=False, connected=True, plan_key="p1", plan=plan))
    assert installed == [("plan_installed", plan)]
    assert tracker.observe(ChargerFacts(charging=False, connected=True, plan_key="p1", plan=plan)) == []
    assert kinds(tracker.observe(ChargerFacts(charging=False, connected=True, plan_key="p2", plan=plan))) == ["plan_installed"]

    info = {"departure_time": "07:00", "requested_kwh": 40.0}
    risk = ChargerFacts(charging=False, connected=True, plan_key="p2", at_risk=True, at_risk_info=info)
    assert tracker.observe(risk) == [("plan_at_risk", info)]
    assert tracker.observe(risk) == [], "once while it lasts"
    tracker.observe(ChargerFacts(charging=False, connected=True, plan_key="p2"))
    assert kinds(tracker.observe(risk)) == ["plan_at_risk"], "and again when it comes back"


async def test_a_charger_gets_one_event_entity_that_fires_on_charging(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    event = entity_id(hass, entry.entry_id, "charger_events", "event")
    state = hass.states.get(event)
    assert state is not None and state.attributes["event_types"] == list(CHARGER_EVENT_TYPES)

    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    state = hass.states.get(event)
    assert state.attributes["event_type"] == "charge_started"

    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    assert hass.states.get(event).attributes["event_type"] == "charge_finished"
