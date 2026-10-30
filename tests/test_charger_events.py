"""The charger's event entity: the tracker's rules, and the entity a real charger gets."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.charger_events import (
    CHARGER_EVENT_TYPES,
    ChargerEventTracker,
    ChargerFacts,
)
from custom_components.spotnav.planning.planner import FiscalChoice
from custom_components.spotnav.sessions.costing import merge_slices, split_energy
from custom_components.spotnav.sessions.model import ChargeSession
from custom_components.spotnav.sessions.summary import cost_figures

from .sessions_helpers import day_intervals, session, STOCKHOLM, UTC
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


def test_an_unreadable_charge_is_no_observation_so_a_reload_is_no_event() -> None:
    """The charger's integration reloads: its entities read unavailable for a while, then charging again."""
    tracker = ChargerEventTracker()
    tracker.observe(ChargerFacts(charging=True, connected=True, register_kwh=100.0))

    assert tracker.observe(ChargerFacts(charging=None, connected=None, register_kwh=None)) == []
    assert tracker.observe(ChargerFacts(charging=True, connected=True, register_kwh=101.0)) == []
    # A charge that did end meanwhile still finishes, counted from its start.
    tracker.observe(ChargerFacts(charging=None, connected=None))
    finished = tracker.observe(ChargerFacts(charging=False, connected=True, register_kwh=103.0))
    assert finished == [("charge_finished", {"unplugged": False, "energy_kwh": 3.0})]


def test_a_charge_read_first_after_an_unreadable_start_is_the_baseline() -> None:
    tracker = ChargerEventTracker()
    tracker.observe(ChargerFacts(charging=None, connected=None))
    assert tracker.observe(ChargerFacts(charging=True, connected=None)) == []
    assert kinds(tracker.observe(ChargerFacts(charging=False, connected=None))) == ["charge_finished"]


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


async def test_a_charger_integration_reload_fires_no_charge_event(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    event = entity_id(hass, entry.entry_id, "charger_events", "event")
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    fired = hass.states.get(event).last_changed

    hass.states.async_set("switch.charger_a", "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()

    state = hass.states.get(event)
    assert state.attributes["event_type"] == "charge_started" and state.last_changed == fired, (
        "neither a finished nor a new charge"
    )


# ---- what a finished charge cost


def charge_session(
    *, energy: float, spans: list[tuple[float, float]], currency: str = "SEK", fx: float = 1.0
) -> ChargeSession:
    """A session of `energy` kWh at 06:00, drawn in (hours from 06:00, kWh) `spans`; only 06:00-08:00 has prices
    (100 and 200 minor per kWh before the rate `fx`), so energy drawn outside them is unpriced."""
    start = datetime(2026, 9, 22, 6, tzinfo=STOCKHOLM).astimezone(UTC)
    spot = [replace(item, fx=fx) for item in day_intervals(date(2026, 9, 22), [0.0] * 6 + [100.0, 200.0])]
    intervals: tuple = ()
    for offset, kwh in spans:
        begin = start + timedelta(hours=offset)
        intervals = merge_slices(intervals, split_energy(spot, begin, begin + timedelta(minutes=30), kwh))
    item = session(start, energy=energy, cost_minor=None, currency=currency)
    return replace(item, intervals=intervals, end=None)


def test_a_priced_charge_has_its_cost_currency_and_average_in_the_major_unit() -> None:
    run = charge_session(energy=12.0, spans=[(0.0, 6.0), (1.0, 6.0)]).since(None)
    # 6 kWh at 1.00 and 6 kWh at 2.00, plus 10 öre tax and 25 % VAT: (110 + 210) x 1.25 x 6 öre.
    fiscal = FiscalChoice(tax_enabled=True, tax_minor_per_kwh=10.0, vat_enabled=True, vat_percent=25.0)
    figures = cost_figures(run.priced(fiscal))
    assert figures == {"cost": 24.0, "currency": "SEK", "average_price": 2.0, "unpriced_kwh": 0.0}


def test_a_euro_charge_is_in_euro_at_the_days_rate() -> None:
    run = charge_session(energy=4.0, spans=[(1.0, 4.0)], currency="EUR", fx=0.5).since(None)
    assert cost_figures(run.priced(FiscalChoice())) == {
        "cost": 4.0, "currency": "EUR", "average_price": 1.0, "unpriced_kwh": 0.0,
    }


def test_a_partly_priced_charge_says_how_much_energy_had_no_price() -> None:
    # 2 kWh at 07:00 (priced at 2.00), 3 kWh at 08:00 where no price is published.
    run = charge_session(energy=5.0, spans=[(1.0, 2.0), (2.0, 3.0)]).since(None)
    figures = cost_figures(run.priced(FiscalChoice()))
    assert figures == {"cost": 4.0, "currency": "SEK", "average_price": 2.0, "unpriced_kwh": 3.0}


def test_an_unpriced_charge_has_no_cost_never_zero() -> None:
    run = charge_session(energy=5.0, spans=[(2.0, 5.0)]).since(None)
    assert cost_figures(run.priced(FiscalChoice())) == {
        "cost": None, "currency": None, "average_price": None, "unpriced_kwh": 5.0,
    }
    # No fiscal settings to price with: the same.
    assert cost_figures(run.priced(None))["cost"] is None
    assert cost_figures(None) == {"cost": None, "currency": None, "average_price": None, "unpriced_kwh": None}


def test_a_charge_after_a_short_pause_costs_only_its_own_energy() -> None:
    """A pause shorter than the recorder's debounce continues the session: the second charge is its part."""
    earlier = charge_session(energy=6.0, spans=[(0.0, 6.0)])
    later = charge_session(energy=10.0, spans=[(0.0, 6.0), (1.0, 4.0)])
    run = later.since(earlier)
    assert run.energy_kwh == pytest.approx(4.0)
    assert cost_figures(run.priced(FiscalChoice())) == {
        "cost": 8.0, "currency": "SEK", "average_price": 2.0, "unpriced_kwh": 0.0,
    }
    other = replace(earlier, id="another")
    assert later.since(other).energy_kwh == pytest.approx(10.0), "another session's start is no baseline"
