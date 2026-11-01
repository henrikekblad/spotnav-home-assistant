"""A kWh need counted with an energy register ends its charge when the need is delivered, so its plan says the
need and not its whole quarters: the stated energy is the request, the cost counts every quarter whole but the
last one in time, which counts only for the share the need takes, and the card's plan ends where the need is
expected to be met. Without a count nothing ends the charge early, and the plan is unchanged.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.spotnav.api.dashboard import _installed_shown, _proposal_section
from custom_components.spotnav.planning.planner import calculate_plan, ending_at_need, energy_per_slot_kwh, slots_needed
from custom_components.spotnav.planning.status_compose import compose_status, ProposalFacts, StatusFacts

from .harness import Session
from .planning import build_request, parse_documents
from .relay import Clock, serve

#: 16 A on three phases at 400 V: about 11.09 kW, 2.77 kWh a quarter.
PER_SLOT_KWH = energy_per_slot_kwh(16, 3)
ENERGY_ENTITY = "sensor.entry_a_energy_register"


def _day(date: str, prices: dict[int, float]) -> dict[str, Any]:
    return {
        "area": "SE4",
        "date": date,
        "tz": "Europe/Stockholm",
        "res": 15,
        "start": f"{date}T00:00:00+02:00",
        "unit": "EUR/kWh",
        "prices": [prices.get(index, 1.0) for index in range(96)],
    }


def _plan(requested_kwh: float, *, departure: str | None = "07:00", prices: dict[int, float] | None = None) -> Any:
    # Evening dear (2.0), the night cheap (0.5) from 00:00 with its first quarters cheapest, so the chosen quarters
    # are not in price order and the last one in time is not the dearest one.
    night = {index: 0.5 + index * 0.01 for index in range(28)} if prices is None else prices
    documents = [_day("2026-09-22", {index: 2.0 for index in range(72, 96)}), _day("2026-09-23", night)]
    return calculate_plan(
        build_request(
            {
                "area_id": "SE4",
                "timezone": "Europe/Stockholm",
                "currency": "EUR",
                "now": "2026-09-22T18:00:00+02:00",
                "phases": 3,
                "amps": 16,
                "requested_kwh": requested_kwh,
                "consumption_kwh_per_10km": 2.0,
                "max_periods": 2,
                "departure": departure,
            },
            parse_documents(documents),
        )
    )


def _slot_cost(slot: Any, kwh: float = PER_SLOT_KWH) -> float:
    return kwh * slot.effective_minor_per_kwh / 100


# ------------------------------------------------------------------------------------------- the planner


@pytest.mark.parametrize(
    "requested_kwh",
    [
        # The owner's: 30.5 kWh is eleven quarters and a sliver of a twelfth; the plan said 33.3 kWh.
        30.5,
        # A last quarter nearly full.
        11 * PER_SLOT_KWH + 0.97 * PER_SLOT_KWH,
    ],
)
def test_a_need_that_stops_at_its_count_states_the_need_and_its_share_of_the_last_quarter(
    requested_kwh: float,
) -> None:
    whole = _plan(requested_kwh)
    assert whole.slots_needed == 12
    assert whole.delivered_kwh == pytest.approx(12 * PER_SLOT_KWH)

    result = ending_at_need(whole)

    share = requested_kwh / PER_SLOT_KWH - 11
    last = max(result.slots, key=lambda slot: slot.start)
    others = [slot for slot in result.slots if slot is not last]
    assert result.ends_at_need is True
    assert result.delivered_kwh == pytest.approx(requested_kwh)
    assert result.estimated_cost == pytest.approx(
        sum(_slot_cost(slot) for slot in others) + _slot_cost(last, share * PER_SLOT_KWH)
    )
    assert result.distance_mil == pytest.approx(requested_kwh / 2.0)
    # What is installed stays whole: the count ends the charge.
    assert result.periods == whole.periods and result.slots == whole.slots
    met = whole.periods[-1][1] - timedelta(minutes=15) + timedelta(minutes=15 * share)
    assert result.need_met_at == pytest.approx(met, abs=timedelta(seconds=1))
    assert result.shown_periods[:-1] == whole.periods[:-1]
    assert result.shown_periods[-1] == (whole.periods[-1][0], result.need_met_at)


def test_charging_now_is_counted_the_same_way() -> None:
    whole = _plan(30.5)
    result = ending_at_need(whole)

    extra = whole.delivered_kwh - 30.5
    now_last = whole.horizon[whole.slots_needed - 1]
    assert result.now_cost == pytest.approx(whole.now_cost - extra * now_last.effective_minor_per_kwh / 100)


def test_a_need_that_fills_its_quarters_exactly_is_unchanged() -> None:
    whole = _plan(12 * PER_SLOT_KWH)
    assert whole.slots_needed == 12

    result = ending_at_need(whole)

    assert result.delivered_kwh == whole.delivered_kwh
    assert result.estimated_cost == whole.estimated_cost
    assert result.need_met_at is None
    assert result.shown_periods == whole.periods


def test_a_plan_short_of_its_departure_is_unchanged() -> None:
    whole = _plan(30.5, departure="18:45")
    assert whole.short_of_deadline is True

    result = ending_at_need(whole)

    assert result.delivered_kwh == whole.delivered_kwh and result.estimated_cost == whole.estimated_cost
    assert result.need_met_at is None


# ------------------------------------------------------------------------------------------- the status


def test_the_charge_in_the_last_window_says_it_ends_where_the_need_is_met() -> None:
    requested = 11.97 * PER_SLOT_KWH
    result = ending_at_need(_plan(requested))
    end = result.periods[-1][1]
    met = result.need_met_at
    proposal = ProposalFacts(periods=result.shown_periods, planned_kwh=requested, need_met_at=met)
    now = end - timedelta(minutes=10)
    assert now < met < end

    lines = compose_status(
        StatusFacts(
            now=now,
            has_settings=True,
            charging=True,
            installed_periods=tuple(result.periods),
            proposal=proposal,
            relation_applied=True,
        )
    )["lines"]

    charging = next(line for line in lines if line["code"] == "charging_now")
    assert charging["params"]["until"] == met.astimezone(dt_util.UTC).isoformat()


# ------------------------------------------------------------------------------------------- Auto's plan


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    now = clock()
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: now)


def _register(session: Session, kwh: float) -> None:
    session.controller.energy_register_entity_id = ENERGY_ENTITY
    session.hass.states.async_set(
        ENERGY_ENTITY,
        str(kwh),
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )


async def test_autos_plan_states_the_need_with_a_register_and_whole_quarters_without(session: Session) -> None:
    serve(session.transport, flat=True)
    await session.set_auto(requested_kwh=30.5, amps=16, phases=3, departure=time(20, 0))
    whole = session.preview.snapshot().proposal
    assert whole.ends_at_need is False
    assert whole.delivered_kwh == pytest.approx(12 * PER_SLOT_KWH)

    _register(session, 100.0)
    counted = (await session.preview.async_recalculate()).proposal

    assert counted.ends_at_need is True
    assert counted.delivered_kwh == pytest.approx(30.5)
    assert counted.estimated_cost < whole.estimated_cost
    assert counted.periods == whole.periods
    assert counted.need_met_at is not None and counted.need_met_at < counted.periods[-1][1]


async def test_a_hybrid_need_keeps_its_whole_quarters(session: Session) -> None:
    """Part of a hybrid need is the sun's, so its grid quarters are not where the count ends it."""
    serve(session.transport, flat=True)
    _register(session, 100.0)

    await session.set_auto(requested_kwh=30.5, amps=16, phases=3, departure=time(20, 0), strategy="hybrid")

    proposal = session.preview.snapshot().proposal
    assert proposal is not None and proposal.has_plan
    assert proposal.ends_at_need is False


def test_the_need_met_instant_is_inside_the_last_quarter() -> None:
    result = ending_at_need(_plan(30.5))
    start, end = result.periods[-1]
    assert end - timedelta(minutes=15) < result.need_met_at < end
    assert isinstance(result.need_met_at, datetime)


# ------------------------------------------------------------------------------------------- the card's periods


def _capture(result: Any, *, applied: bool | None) -> Any:
    snapshot = SimpleNamespace(proposal=result, applied=applied, proposal_identity="p1", settings_revision=1)
    return SimpleNamespace(snapshot=snapshot, area_entry=SimpleNamespace(currency="EUR"), settings=None)


def test_the_cards_proposal_and_installed_plan_end_where_the_need_is_met() -> None:
    result = ending_at_need(_plan(30.5))
    met = result.need_met_at
    windows = list(result.periods)

    section = _proposal_section(_capture(result, applied=True))
    assert section["planned_kwh"] == pytest.approx(30.5)
    assert section["cost"]["value"] == pytest.approx(result.estimated_cost)
    assert section["periods"][-1]["end"] == met.astimezone(dt_util.UTC).isoformat()

    shown = _installed_shown(_capture(result, applied=True), windows)
    assert shown[:-1] == windows[:-1] and shown[-1] == (windows[-1][0], met)
    # Another plan installed beside this proposal keeps its own windows.
    assert _installed_shown(_capture(result, applied=False), windows) == windows


def test_without_a_count_the_cards_periods_are_the_whole_quarters() -> None:
    whole = _plan(30.5)

    section = _proposal_section(_capture(whole, applied=True))
    assert section["planned_kwh"] == pytest.approx(12 * PER_SLOT_KWH)
    assert section["periods"][-1]["end"] == whole.periods[-1][1].astimezone(dt_util.UTC).isoformat()
    assert _installed_shown(_capture(whole, applied=True), list(whole.periods)) == list(whole.periods)


def test_a_need_of_exactly_whole_quarters_buys_no_extra_quarter() -> None:
    assert slots_needed(12 * PER_SLOT_KWH, 16, 3) == 12
    assert slots_needed(12 * PER_SLOT_KWH + 0.001, 16, 3) == 13
    assert _plan(12 * PER_SLOT_KWH).slots_needed == 12


async def test_a_count_from_a_power_sensor_that_reports_nothing_keeps_whole_quarters(session: Session) -> None:
    serve(session.transport, flat=True)
    _register(session, 0.0)
    session.controller.energy_from_power = True
    session.controller.energy_power_entity_id = "sensor.charger_power"

    await session.set_auto(requested_kwh=30.5, amps=16, phases=3, departure=time(20, 0))
    assert session.preview.snapshot().proposal.ends_at_need is False

    session.hass.states.async_set("sensor.charger_power", "0", {"device_class": "power", "unit_of_measurement": "W"})
    assert (await session.preview.async_recalculate()).proposal.ends_at_need is True
