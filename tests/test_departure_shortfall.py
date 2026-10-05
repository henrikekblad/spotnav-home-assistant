"""A need the departure leaves too little time for: best effort, never a refusal.

The field case (a debug bundle, 2026-10-05): Cheapest, a 70 % target on a 15.6 kWh car at 2 %, so
11.79 kWh from the wall, on one phase at 16 A (3.68 kW, about 3.2 hours), at 09:58 with the departure
at 12:00. Every slot from 10:00 to 12:00 is planned and charged, and the status warns what the car
will have by then instead of the blocking "no plan with the data available".
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.event import charger_facts
from custom_components.spotnav.execution.charger_events import ChargerEventTracker
from custom_components.spotnav.execution.controller import ChargingPlan
from custom_components.spotnav.planning.auto_controller import LiveVehicleFacts
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_TARGET_SOC,
    STRATEGY_HYBRID,
    TargetSocIntent,
)
from custom_components.spotnav.planning.planner import (
    PlanRequest,
    calculate_plan,
    plan_unpriced,
    price_gap,
)
from custom_components.spotnav.planning.status_compose import (
    PlanningFacts,
    ProposalFacts,
    StatusFacts,
    compose_status,
)
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY

from .harness import Harness, Session, assert_nothing_executed
from .planning import build_request, parse_documents
from .relay import Clock, FakeScheduler, StubTransport, serve

pytestmark = pytest.mark.usefixtures("offline_relay")

CEST = timezone(timedelta(hours=2))

#: The field case's car and charger.
CAPACITY_KWH = 15.6
SOC_PERCENT = 2.0
TARGET_PERCENT = 70
#: (70 - 2) % of 15.6 kWh from the wall at the charging efficiency: 11.79 kWh.
NEED_KWH = (TARGET_PERCENT - SOC_PERCENT) / 100 * CAPACITY_KWH / CHARGE_EFFICIENCY
#: One phase at 16 A: 3.68 kW, 0.92 kWh a quarter-hour; 10:00 to 12:00 holds eight of them.
PER_SLOT_KWH = 0.92
SHORT_KWH = 8 * PER_SLOT_KWH
EXPECTED_SOC = SOC_PERCENT + SHORT_KWH * CHARGE_EFFICIENCY / CAPACITY_KWH * 100


# ------------------------------------------------------------------ the planner itself


def day(date: str, prices: list[float]) -> dict[str, Any]:
    return {
        "area": "SE4",
        "date": date,
        "tz": "Europe/Stockholm",
        "res": 15,
        "start": f"{date}T00:00:00+02:00",
        "unit": "EUR/kWh",
        "prices": prices,
    }


def expensive_middle(date: str) -> dict[str, Any]:
    """Cheap at 10:00-10:30 and 11:30-12:00, dear in between: a cheapest plan would want two runs."""
    prices = [1.0] * 96
    for index in range(40, 48):  # 10:00 .. 12:00
        prices[index] = 0.1 if index in (40, 41, 46, 47) else 3.0
    return day(date, prices)


def request(
    *,
    now: str = "2026-10-05T09:58:00+02:00",
    departure: str | None = "12:00",
    requested_kwh: float = NEED_KWH,
    max_periods: int = 1,
    documents: list[dict[str, Any]] | None = None,
) -> PlanRequest:
    docs = documents if documents is not None else [day("2026-10-05", [1.0] * 96), day("2026-10-06", [1.0] * 96)]
    return build_request(
        {
            "area_id": "SE4",
            "timezone": "Europe/Stockholm",
            "currency": "EUR",
            "major_unit": "€",
            "minor_unit": "cent",
            "now": now,
            "phases": 1,
            "amps": 16,
            "requested_kwh": requested_kwh,
            "consumption_kwh_per_10km": 2.0,
            "max_periods": max_periods,
            "departure": departure,
        },
        parse_documents(docs),
    )


def test_the_field_case_plans_every_slot_from_now_to_the_departure() -> None:
    result = calculate_plan(request())

    assert result.has_plan and result.reason is None
    assert result.short_of_deadline is True
    assert result.periods == ((datetime(2026, 10, 5, 10, 0, tzinfo=CEST), datetime(2026, 10, 5, 12, 0, tzinfo=CEST)),)
    assert result.slots_needed == 8 and len(result.slots) == 8
    assert result.delivered_kwh == pytest.approx(SHORT_KWH)
    assert result.requested_kwh == pytest.approx(NEED_KWH), "the need is reported as it was asked"
    assert result.unpriced is False and result.priced_slots == 8


def test_a_need_that_fits_exactly_is_an_ordinary_plan() -> None:
    """7.3 kWh is eight quarter-hours: all of 10:00-12:00, and not short of anything."""
    result = calculate_plan(request(requested_kwh=7.3))

    assert result.has_plan and result.short_of_deadline is False
    assert result.slots_needed == 8
    assert result.periods[-1][1] == datetime(2026, 10, 5, 12, 0, tzinfo=CEST)


def test_a_departure_already_passed_today_plans_for_tomorrows() -> None:
    """At 12:30 the 12:00 departure is tomorrow's: 11.79 kWh fits easily, an ordinary plan."""
    result = calculate_plan(request(now="2026-10-05T12:30:00+02:00"))

    assert result.has_plan and result.short_of_deadline is False
    assert result.slots_needed == 13
    assert result.periods[-1][1] <= datetime(2026, 10, 6, 12, 0, tzinfo=CEST)


@pytest.mark.parametrize("max_periods", [1, 2, 8])
def test_the_period_limit_never_costs_the_best_effort_energy(max_periods: int) -> None:
    """However many runs are allowed and whatever the prices, best effort is the whole window, one run."""
    result = calculate_plan(
        request(max_periods=max_periods, documents=[expensive_middle("2026-10-05"), day("2026-10-06", [1.0] * 96)])
    )

    assert result.short_of_deadline is True
    assert len(result.periods) == 1 and result.slots_needed == 8
    assert result.delivered_kwh == pytest.approx(SHORT_KWH)


def test_not_one_whole_slot_before_the_departure_is_still_refused_by_name() -> None:
    result = calculate_plan(request(now="2026-10-05T11:50:00+02:00", departure="12:05"))

    assert result.reason == "deadline_too_short" and not result.has_plan


def test_a_callers_own_tighter_window_is_refused_rather_than_shortened() -> None:
    """`window_end` asks about that window (the published part, a purchase before a publication)."""
    from dataclasses import replace

    tight = replace(request(), window_end=datetime(2026, 10, 5, 11, 0, tzinfo=CEST))
    assert calculate_plan(tight).reason == "deadline_too_short"


def test_a_short_window_with_unpublished_prices_is_named_short_and_charged_at_once() -> None:
    """Departure 01:00 tomorrow with tomorrow unpublished: the gap says short, and the unpriced plan is
    every slot up to the departure."""
    late = request(
        now="2026-10-05T22:58:00+02:00", departure="01:00", documents=[day("2026-10-05", [1.0] * 96)]
    )
    assert calculate_plan(late).reason == "insufficient_price_horizon"
    gap = price_gap(late)
    assert gap is not None and gap.short is True

    unpriced = plan_unpriced(late)
    assert unpriced.has_plan and unpriced.short_of_deadline is True
    assert unpriced.periods == (
        (datetime(2026, 10, 5, 23, 0, tzinfo=CEST), datetime(2026, 10, 6, 1, 0, tzinfo=CEST)),
    )


# ------------------------------------------------------------------ the controller

#: 09:58 Stockholm on the fixture day (CEST), and its departure at 12:00.
FIELD_NOW = datetime(2026, 9, 22, 7, 58, tzinfo=timezone.utc)
DEPARTURE = time(12, 0)
DEPARTURE_AT = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> Clock:
    return Clock(FIELD_NOW)


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """The controller's own plan rules read `dt_util.utcnow()`: pinned to the injected clock."""
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: clock())


def the_car(vehicle_id: str) -> LiveVehicleFacts:
    return LiveVehicleFacts(vehicle_id=vehicle_id, soc_percent=SOC_PERCENT, reported_capacity_kwh=CAPACITY_KWH)


TARGET = {
    "driver": DRIVER_TARGET_SOC,
    "target": TargetSocIntent(vehicle_id="veh-1", target_percent=TARGET_PERCENT),
}


def lines(snapshot: Any, **facts: Any) -> dict[str, Any]:
    """The composed status for a snapshot, as the dashboard builds its facts."""
    proposal = snapshot.proposal
    return compose_status(
        StatusFacts(
            now=snapshot.calculated_at,
            has_settings=True,
            strategy="cheapest",
            departure_enabled=True,
            price_state="ready",
            usable_price_rows=96,
            planning=PlanningFacts(
                state=snapshot.state,
                reason=snapshot.reason,
                departure_at=None if snapshot.departure_at is None else snapshot.departure_at.astimezone(timezone.utc),
                expected_soc_percent=snapshot.expected_soc_percent,
            ),
            proposal=ProposalFacts(
                periods=proposal.periods,
                planned_kwh=proposal.delivered_kwh,
                requested_kwh=proposal.requested_kwh,
                short_of_deadline=proposal.short_of_deadline,
            ),
            **facts,
        )
    )


async def test_the_field_case_is_a_plan_with_a_warning_not_a_refusal(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    serve(harness.transport)
    harness.vehicle_reader = the_car
    controller = await harness.auto(amps=16, departure=DEPARTURE, **TARGET)

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan and proposal.short_of_deadline
    assert proposal.periods == ((datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc), DEPARTURE_AT),)
    assert proposal.delivered_kwh == pytest.approx(SHORT_KWH)
    assert proposal.requested_kwh == pytest.approx(NEED_KWH)
    assert snapshot.departure_at == DEPARTURE_AT
    assert snapshot.expected_soc_percent == pytest.approx(EXPECTED_SOC)
    assert round(EXPECTED_SOC) == 44

    block = lines(snapshot)
    assert block["tone"] == "notice"
    warning = [line for line in block["lines"] if line["code"] == "departure_shortfall"]
    assert warning == [
        {
            "code": "departure_shortfall",
            "params": {
                "kwh": 7.36,
                "requested_kwh": round(NEED_KWH, 2),
                "soc_percent": round(EXPECTED_SOC, 1),
                "departure": DEPARTURE_AT.isoformat(),
            },
        }
    ]
    assert not any(line["code"] == "planning_unavailable" for line in block["lines"])
    assert_nothing_executed(no_execution)


async def test_a_manual_need_says_the_energy_and_no_state_of_charge(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    serve(harness.transport)
    controller = await harness.auto(amps=16, departure=DEPARTURE, requested_kwh=12.0)

    snapshot = controller.snapshot()
    proposal = snapshot.proposal
    assert snapshot.state == "proposal_ready" and proposal is not None and proposal.short_of_deadline
    assert snapshot.expected_soc_percent is None
    warning = next(line for line in lines(snapshot)["lines"] if line["code"] == "departure_shortfall")
    assert warning["params"] == {
        "kwh": 7.36, "requested_kwh": 12.0, "soc_percent": None, "departure": DEPARTURE_AT.isoformat()
    }


async def test_a_need_that_fits_says_nothing_about_a_shortfall(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    serve(harness.transport)
    controller = await harness.auto(amps=16, departure=DEPARTURE, requested_kwh=7.3)

    snapshot = controller.snapshot()
    assert snapshot.proposal is not None and snapshot.proposal.short_of_deadline is False
    assert not any(line["code"] == "departure_shortfall" for line in lines(snapshot)["lines"])


async def test_after_the_departure_the_next_one_is_planned_normally(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    serve(harness.transport)
    harness.vehicle_reader = the_car
    controller = await harness.auto(amps=16, departure=DEPARTURE, **TARGET)
    assert controller.snapshot().proposal.short_of_deadline

    harness.clock.now = DEPARTURE_AT + timedelta(minutes=1)
    snapshot = await controller.async_recalculate()

    assert snapshot.state == "proposal_ready"
    assert snapshot.proposal is not None and snapshot.proposal.short_of_deadline is False
    assert snapshot.departure_at == datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert snapshot.expected_soc_percent is None


async def test_hybrid_plans_the_grid_part_as_best_effort(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """No forecast is configured: hybrid buys the whole need from the grid, every slot to the departure."""
    serve(harness.transport)
    harness.vehicle_reader = the_car
    controller = await harness.auto(amps=16, departure=DEPARTURE, strategy=STRATEGY_HYBRID, **TARGET)

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready"
    proposal = snapshot.proposal
    assert proposal is not None and proposal.short_of_deadline
    assert proposal.periods == ((datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc), DEPARTURE_AT),)
    assert snapshot.expected_soc_percent == pytest.approx(EXPECTED_SOC)


async def test_the_best_effort_plan_is_installed_and_charging_starts(
    hass: HomeAssistant,
    transport: StubTransport,
    clock: Clock,
    timers: FakeScheduler,
    install_spy: list[ChargingPlan],
) -> None:
    session = Session(hass, transport, clock, timers)
    session.vehicle_reader = the_car
    await session.start()
    serve(transport)

    snapshot = await session.set_auto(amps=16, departure=DEPARTURE, **TARGET)

    assert snapshot.proposal is not None and snapshot.proposal.short_of_deadline
    assert len(install_spy) == 1
    plan = install_spy[0]
    assert plan.windows[0][0] == datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
    assert plan.windows[-1][1] == DEPARTURE_AT
    assert plan.target_soc_percent == TARGET_PERCENT

    # 10:00: the window opens and the charger is switched on.
    clock.now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
    await session.boundary()
    assert session.on_calls, "the charger was started at the window's start"


def test_the_risk_event_fires_once_for_a_best_effort_plan_and_again_only_after_it_clears() -> None:
    """`plan_at_risk` is tied to the best-effort plan as it was to the refusal: once per time it becomes so."""

    class Snapshot:
        def __init__(self, short: bool, reason: str = "ready") -> None:
            self.reason = reason
            self.proposal = None if reason != "ready" else type("P", (), {"short_of_deadline": short})()

    class Adapter:
        @staticmethod
        def vehicle_connected() -> bool:
            return True

    class Controller:
        plan = None
        charging = False
        adapter = Adapter()
        energy_register_entity_id = None

    class Settings:
        departure = DEPARTURE
        requested_kwh = 12.0

    def facts(snapshot: Snapshot) -> Any:
        return charger_facts(None, Controller(), snapshot, Settings())  # type: ignore[arg-type]

    tracker = ChargerEventTracker()
    tracker.observe(facts(Snapshot(short=False)))
    risk = tracker.observe(facts(Snapshot(short=True)))
    assert risk == [("plan_at_risk", {"departure_time": "12:00", "requested_kwh": 12.0})]
    assert tracker.observe(facts(Snapshot(short=True))) == [], "a replan that is still short is not news"
    assert tracker.observe(facts(Snapshot(short=False))) == []
    assert [kind for kind, _ in tracker.observe(facts(Snapshot(short=True)))] == ["plan_at_risk"]
    # A departure with not one whole slot left is still the refusal, and still at risk.
    tracker.observe(facts(Snapshot(short=False)))
    assert [kind for kind, _ in tracker.observe(facts(Snapshot(False, "deadline_too_short")))] == ["plan_at_risk"]
