"""The Auto preview: states, price precedence and the absence of execution.

Every case runs to its conclusion immediately — the clock is injected, the manager's
scheduler records instead of waiting, the wire is a stub, and the webhook is driven through
Home Assistant's own HTTP client. Nothing sleeps.

The regression half of this module is the point of it: Auto calculates, and the spies here
exist to prove it does nothing else. `no_execution` closes every route to a charger at the
*class* level, because a preview that called `async_install` and then failed to reach a
service would still be a forbidden call.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.planning.auto_controller import AutoPlannerController, LiveVehicleFacts
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_TARGET_SOC,
    AreaAutoSettings,
    AutoSettingsError,
    FiscalOverride,
    PauseIntent,
    TargetSocIntent,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.pricing.price_refresh import PriceRefreshManager

from .helpers import future_window, make_entry, make_site_entry
from .relay import StubTransport
from .relay import TODAY, TOMORROW, YESTERDAY, day_body
from .relay import DE_LU, SE4, cheap_midday_day, cheap_night_day, serve, serve_index
from .harness import Harness, Recording, assert_nothing_executed
from .world import go_auto, setup_charger
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import preview_for
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

pytestmark = pytest.mark.usefixtures("offline_relay")

BASE_URL = "https://relay.test"
#: A second area with prices of the same shape as SE4, shifted by an hour, for the change tests
#: that need the hours to move without the money or the slot count moving with them.
OTHER_AREA = DE_LU  # a euro area: no rate table, and its own prices

#: The two area-local dates the fixture clock (08:00 Stockholm on 2026-09-22) needs.
_BOTH = (TODAY, TOMORROW)


# ------------------------------------------------------------------ the doubles


#: A test below installs an Auto plan and then asserts it is installed
#: (`test_manual_actions_never_change_the_settings`); it freezes `2026-09-22 06:00:00`, the
#: same instant this `clock` starts at. `ChargingController` validates a fresh plan against
#: `dt_util.utcnow()` ("in the future, and within seven days"), which is the *real* clock, so
#: without the freeze a plan ending at 08:00 Stockholm stops being installable the moment the
#: wall clock passes it -- a suite that passes in the morning and fails in the afternoon.


# ------------------------------------------------- defaults and the plain proposal


async def test_absent_settings_are_incomplete_and_subscribe_to_nothing(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """What every installation predating Auto has: no subscription, no calculation, nothing."""
    controller = await harness.preview()
    snapshot = controller.snapshot()

    assert snapshot.state == "incomplete_settings" and snapshot.reason == "settings_missing"
    assert snapshot.proposal is None
    assert snapshot.applied is False and snapshot.in_process is False
    assert snapshot.settings_revision == 0 and snapshot.missing == ("area", "amps")
    # A public observation rather than an internal flag: the manager knows no such area.
    assert harness.area is None
    assert harness.transport.calls == []
    assert_nothing_executed(no_execution)


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({"area_id": None}, ("area",)),
        ({"area_id": None, "amps": None}, ("area", "amps")),
        ({"driver": DRIVER_TARGET_SOC}, ("vehicle", "target_percent")),
        (
            {"driver": DRIVER_TARGET_SOC, "target": TargetSocIntent(vehicle_id="veh-1")},
            ("target_percent",),
        ),
    ],
)
async def test_every_missing_field_is_named_and_nothing_is_executed(
    harness: Harness,
    no_execution: dict[str, Any],
    changes: dict[str, Any],
    missing: tuple[str, ...],
) -> None:
    """Auto refuses to guess, says exactly which input it is missing, and does nothing else."""
    harness.transport.serve_area()
    controller = await harness.auto(**changes)
    snapshot = controller.snapshot()

    assert snapshot.state == "incomplete_settings" and snapshot.reason == "settings_missing"
    assert snapshot.missing == missing
    assert snapshot.proposal is None and snapshot.applied is False
    assert harness.manager is not None
    if "area" in missing:
        # Nothing is subscribed for a charger that cannot be calculated at all, not even
        # the shared stream: an area is the one setting that decides that.
        assert harness.manager.manager_snapshot().areas == ()
    else:
        # The stream is open -- the prices are what the missing setting will be used
        # against -- and the preview still refuses to guess the field it lacks.
        assert harness.area is not None and harness.area.subscriber_count == 1
    assert_nothing_executed(no_execution)


async def test_a_fiscal_component_with_no_suggestion_and_no_override_is_named(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """DK1 publishes no grid fee: enabling it without a figure is incomplete, not zero."""
    serve(harness.transport, "DK1", days=(TODAY,))
    controller = await harness.auto(
        area_id="DK1",
        overrides=(AreaAutoSettings(area_id="DK1", transfer=FiscalOverride(enabled=True)),),
    )
    snapshot = controller.snapshot()

    assert snapshot.state == "incomplete_settings" and snapshot.reason == "fiscal_value_missing"
    assert snapshot.proposal is None and snapshot.missing == ()
    assert snapshot.area_id == "DK1" and snapshot.currency == "DKK"
    assert_nothing_executed(no_execution)


async def test_entering_auto_with_an_area_calculates_a_real_proposal(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """The proposal path end to end: one stream, one plan, and nothing executed."""
    serve(harness.transport)
    controller = await harness.auto()
    snapshot = controller.snapshot()

    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    assert snapshot.proposal is not None, "a ready state must carry the proposal it names"
    proposal = snapshot.proposal
    assert proposal.has_plan
    # SE4's money identity, as the catalogue states it -- SEK, kr, öre.
    assert snapshot.area_id == SE4
    assert snapshot.currency == "SEK"
    assert snapshot.major_unit == "kr"
    assert snapshot.minor_unit == "öre"
    assert snapshot.timezone == "Europe/Stockholm"
    assert snapshot.today == TODAY.isoformat() and snapshot.tomorrow == TOMORROW.isoformat()
    # Counts agree, the plan is fully known, and it is a preview: never applied.
    assert snapshot.priced_slots + snapshot.unpriced_slots == proposal.slots_needed
    assert snapshot.priced_slots == proposal.slots_needed
    assert snapshot.unpriced_slots == 0 and snapshot.unpriced is False
    assert snapshot.applied is False and snapshot.in_process is True
    assert snapshot.calculated_at == harness.clock()
    assert snapshot.price_identity is not None and snapshot.last_error_code is None
    # One stream for the installation, and one subscriber: this charger.
    assert harness.transport.call_count("/v1/index.json") == 1
    assert harness.area is not None and harness.area.subscriber_count == 1
    assert_nothing_executed(no_execution)


async def test_two_chargers_share_one_price_stream_and_calculate_separately(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """Two previews, one area: one network stream, two independent proposals."""
    serve(harness.transport)
    first = await harness.auto("entry-a", requested_kwh=20.0)
    second = await harness.auto("entry-b", requested_kwh=30.0)

    assert first.snapshot().state == "proposal_ready"
    assert second.snapshot().state == "proposal_ready"
    assert harness.transport.call_count("/v1/index.json") == 1
    assert harness.transport.call_count("/v1/areas.json") == 1
    assert harness.transport.call_count(harness.transport.day_path(SE4, TODAY)) == 1
    assert harness.area is not None and harness.area.subscriber_count == 2
    # The same prices, two different energy requests: two different plans.
    first_plan, second_plan = first.snapshot().proposal, second.snapshot().proposal
    assert first_plan is not None and second_plan is not None
    assert first_plan.requested_kwh == 20.0 and second_plan.requested_kwh == 30.0
    assert first_plan.slots_needed < second_plan.slots_needed
    assert_nothing_executed(no_execution)


async def test_moving_an_area_moves_the_stream_and_never_leaks_the_old_proposal(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """An area change is one atomic move, and the new area is calculated from its own data."""
    serve(harness.transport)
    serve(harness.transport, DE_LU, days=(TODAY, TOMORROW))
    # One index naming both areas: the move must not be what makes either day publishable.
    serve_index(harness.transport, {SE4: list(_BOTH), DE_LU: list(_BOTH)})
    controller = await harness.auto()
    assert controller.snapshot().proposal is not None

    moved = await controller.async_apply_settings(
        mutate=lambda settings: replace(settings, area_id=DE_LU)
    )

    assert moved.area_id == DE_LU
    assert moved.currency == "EUR" and moved.major_unit == "€"
    assert moved.proposal is not None and moved.proposal.area_id == DE_LU
    # The old area lost its only subscriber and is not tracked at all.
    assert harness.manager is not None
    assert harness.manager.area_snapshot(SE4) is None
    assert harness.manager.area_snapshot(DE_LU) is not None
    assert controller.snapshot().state == "proposal_ready"
    assert_nothing_executed(no_execution)


async def test_a_refused_edit_leaves_the_stored_state_and_names_the_refusal(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A rejected value is not a new state: the old one stands, with the reason beside it."""
    serve(harness.transport)
    controller = await harness.auto()
    before = controller.snapshot()

    with pytest.raises(AutoSettingsError) as refused:
        await controller.async_apply_settings(mutate=lambda settings: replace(settings, amps=999))

    assert refused.value.code == "invalid_amps"
    after = controller.snapshot()
    assert after.last_error_code == "invalid_amps"
    assert after.state == before.state and after.proposal is before.proposal
    assert after.settings_revision == before.settings_revision
    assert harness.store is not None and harness.store.settings("entry-a").amps == 10
    assert_nothing_executed(no_execution)


# ------------------------------------------------------- price-state precedence


async def test_nothing_asked_yet_is_waiting_and_becomes_a_proposal(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A charger that has just been switched to Auto is waiting, not regretting stale data.

    The held *index* request is what makes this deterministic: the catalogue is already
    known, the day has never been attempted, and nothing has ever been held, so the honest
    state is `waiting_for_prices` -- the same state a request in flight produces.
    """
    serve(harness.transport)
    harness.transport.hold("/v1/index.json")
    controller = await harness.auto()

    waiting = controller.snapshot()
    assert waiting.state == "waiting_for_prices" and waiting.reason == "no_prices_yet"
    assert waiting.proposal is None and waiting.in_process is False
    # No day has been attempted and no index has landed: the area's own state is
    # `unavailable` while the index request is the thing in flight.
    assert waiting.price_state == "unavailable"
    assert harness.area is not None and harness.area.index_state == "loading"
    assert waiting.today == TODAY.isoformat()

    harness.transport.release("/v1/index.json")
    await harness.hass.async_block_till_done()
    assert controller.snapshot().state == "proposal_ready"
    assert controller.snapshot().proposal is not None
    assert_nothing_executed(no_execution)


async def test_a_request_in_flight_is_waiting_even_with_prices_already_held(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """The first fetch, held open: waiting, and then a proposal once it lands."""
    serve(harness.transport)
    path = harness.transport.day_path(SE4, TODAY)
    harness.transport.hold(path)
    controller = await harness.auto()
    await harness.transport.entered[path].wait()

    waiting = controller.snapshot()
    assert waiting.state == "waiting_for_prices" and waiting.reason == "no_prices_yet"
    assert waiting.proposal is None

    harness.transport.release(path)
    await harness.hass.async_block_till_done()
    landed = controller.snapshot()
    assert landed.state == "proposal_ready" and landed.proposal is not None
    # Nothing about a wait is remembered as a loss: the prices that arrived are the ones
    # the plan was built from, and their identity says so.
    assert landed.price_identity is not None and landed.in_process is True
    assert_nothing_executed(no_execution)


async def test_prices_that_stop_being_published_keep_the_exact_proposal(harness: Harness) -> None:
    """Stale keeps the plan, and keeps it *unchanged*: not recomputed, not re-stamped."""
    serve(harness.transport)
    controller = await harness.auto()
    recording = Recording()
    controller.add_listener(recording)
    ready = controller.snapshot()
    assert ready.state == "proposal_ready"

    # The relay stops listing today: the held document is no longer published. The plan itself
    # is unchanged -- only the state of the data behind it.
    serve_index(harness.transport, {SE4: [YESTERDAY]})
    await harness.tick()

    stale = controller.snapshot()
    assert stale.state == "price_data_stale" and stale.reason == "price_data_stale"
    assert stale.proposal is ready.proposal
    assert stale.price_identity == ready.price_identity
    assert stale.calculated_at == ready.calculated_at
    assert stale.in_process is True and stale.applied is False
    assert stale.priced_slots == ready.priced_slots
    assert recording.states[-1] == "price_data_stale"


async def test_stale_prices_with_no_proposal_are_stale_and_carry_nothing(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """An unreachable relay with nothing held: the state is named, and no plan is invented."""
    serve(harness.transport)
    harness.transport.serve(harness.transport.day_path(SE4, TODAY), 500, "boom")
    controller = await harness.auto()

    stale = controller.snapshot()
    assert stale.state == "price_data_stale" and stale.reason == "price_data_stale"
    assert stale.proposal is None and stale.in_process is False and stale.applied is False
    assert stale.price_identity is not None, "the identity describes the data that failed"
    assert_nothing_executed(no_execution)


async def test_today_not_published_at_all_is_named_as_such(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A current valid index that omits today is a statement of absence, not a wait."""
    serve(harness.transport, days=(TODAY,), listed=(YESTERDAY, TOMORROW))
    controller = await harness.auto()

    snapshot = controller.snapshot()
    assert snapshot.state == "planning_unavailable"
    assert snapshot.reason == "no_published_prices"
    assert snapshot.proposal is None
    # And the request for a day the index does not list was never made.
    assert harness.transport.call_count(harness.transport.day_path(SE4, TODAY)) == 0
    assert_nothing_executed(no_execution)


async def test_contract_invalid_prices_are_never_a_proposal(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A day document that is not this area's is refused, and says which failure it was."""
    serve(harness.transport)
    harness.transport.serve(harness.transport.day_path(SE4, TODAY), 200, day_body("FI", TODAY))
    controller = await harness.auto()

    snapshot = controller.snapshot()
    assert snapshot.state == "planning_unavailable" and snapshot.reason == "price_data_invalid"
    assert snapshot.proposal is None and snapshot.in_process is False
    assert snapshot.last_error_code == "invalid_contract"
    assert_nothing_executed(no_execution)


async def test_a_failed_tomorrow_attempt_keeps_a_valid_today_proposal(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """Tomorrow failing must not discard today: the plan it produced is still exactly valid."""
    serve(harness.transport, listed=(TODAY, TOMORROW))
    harness.transport.serve(harness.transport.day_path(SE4, TOMORROW), 500, "boom")
    controller = await harness.auto(departure=time(20, 0))

    # The area itself is honest about the impaired horizon ...
    assert harness.area is not None and harness.area.state == "degraded"
    # ... and the preview still holds the plan that today's own prices support.
    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.proposal is not None
    assert snapshot.tomorrow == TOMORROW.isoformat() and snapshot.price_state == "degraded"
    assert_nothing_executed(no_execution)


async def test_a_later_fresh_snapshot_replaces_a_stale_proposal(harness: Harness) -> None:
    """Stale is a state of the data, never a trap: the next good answer wins."""
    serve(harness.transport)
    controller = await harness.auto()
    recording = Recording()
    controller.add_listener(recording)
    stale_identity = controller.snapshot().price_identity

    serve_index(harness.transport, {SE4: [YESTERDAY]})
    await harness.tick()
    assert controller.snapshot().state == "price_data_stale"

    # The clock moves on, so the fresh calculation is visibly a *new* calculation.
    harness.clock.advance(minutes=5)
    serve_index(harness.transport, {SE4: list(_BOTH)})
    await harness.tick()

    fresh = controller.snapshot()
    assert fresh.state == "proposal_ready" and fresh.proposal is not None
    assert fresh.calculated_at == harness.clock()
    assert fresh.price_identity == stale_identity, "the same held document is still the source"
    assert recording.states[-2:] == ["price_data_stale", "proposal_ready"]


async def test_a_need_that_can_wait_for_tomorrows_prices_waits_and_says_when(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """One published day and a departure at 08:00 tomorrow: the plan needs prices that are not out.

    It is 08:00; 20 kWh at 2.3 kW would fill 8.7 hours, and 18 hours remain after the expected
    publication (13:00 plus the 45-minute margin), so nothing is planned now, no price is
    guessed, and the state is a normal one carrying when to look again.
    """
    serve(harness.transport, days=(TODAY,), listed=(TODAY,))
    harness.transport.serve(
        harness.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY)
    )
    controller = await harness.auto(departure=time(8, 0))

    snapshot = controller.snapshot()
    assert snapshot.state == "waiting_for_publication" and snapshot.reason == "publication_pending"
    assert snapshot.price_wait == "waiting" and snapshot.must_buy_kwh == 0.0
    assert snapshot.publication_at == datetime(2026, 9, 22, 11, 45, tzinfo=timezone.utc)
    assert snapshot.proposal is not None and snapshot.proposal.has_plan is False
    assert snapshot.unpriced is False and snapshot.unpriced_slots == 0
    assert snapshot.applied is False
    assert_nothing_executed(no_execution)
    assert any(
        abs((timer.when - datetime(2026, 9, 22, 18, 52, 51, tzinfo=timezone.utc)).total_seconds()) < 5
        for timer in harness.scheduler.pending
    ), f"{[t.when for t in harness.scheduler.pending]}: an appointment at the latest safe start: 08:00 tomorrow less 20 kWh at 80 percent of 2.3 kW, less one slot"


async def test_only_what_cannot_wait_is_bought_now_from_known_prices(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """40 kWh cannot all wait: 6.4 kWh is planned before the publication, from real prices only."""
    serve(harness.transport, days=(TODAY,), listed=(TODAY,))
    harness.transport.serve(
        harness.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY)
    )
    controller = await harness.auto(requested_kwh=40.0, departure=time(8, 0))

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "buying_before_publication"
    assert snapshot.price_wait == "buy_now"
    assert snapshot.must_buy_kwh == pytest.approx(40.0 - 18.25 * 2.3 * 0.8, abs=0.05)
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan
    assert proposal.requested_kwh == pytest.approx(snapshot.must_buy_kwh)
    assert proposal.unpriced_slots == 0 and proposal.unpriced is False
    assert all(end <= snapshot.publication_at for _, end in proposal.periods)
    assert_nothing_executed(no_execution)


async def test_the_guarantee_charges_at_unknown_prices_once_waiting_is_no_longer_safe(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """The prices never came and the latest safe start has passed: charge now, say so, keep the deadline."""
    serve(harness.transport, days=(TODAY,), listed=(TODAY,))
    harness.transport.serve(
        harness.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY)
    )
    controller = await harness.auto(departure=time(8, 0))
    assert controller.snapshot().state == "waiting_for_publication"

    harness.clock.now = datetime(2026, 9, 22, 19, 30, tzinfo=timezone.utc)
    snapshot = await controller.async_recalculate()

    assert snapshot.state == "proposal_unpriced"
    assert snapshot.reason == "charging_without_prices" and snapshot.price_wait == "guarantee"
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan
    assert proposal.unpriced_slots == proposal.slots_needed and proposal.priced_slots == 0
    assert proposal.periods[0][0] == datetime(2026, 9, 22, 21, 30, tzinfo=timezone(timedelta(hours=2)))
    assert proposal.periods[-1][1] <= datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc), "the deadline is kept"
    assert snapshot.applied is False
    assert_nothing_executed(no_execution)


async def test_a_deadline_that_cannot_be_met_is_planned_as_best_effort(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A need the departure leaves no time for is charged in every slot up to it, and marked short."""
    serve(harness.transport)
    controller = await harness.auto(departure=time(9, 0), requested_kwh=200.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "proposal_ready" and snapshot.reason == "ready"
    proposal = snapshot.proposal
    assert proposal is not None and proposal.has_plan and proposal.short_of_deadline
    assert proposal.slots_needed == 4 and len(proposal.periods) == 1
    assert_nothing_executed(no_execution)


async def test_a_departure_with_not_one_whole_slot_left_is_named(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """Ten minutes to the departure hold no quarter-hour: refused by name, with the deadline it cannot meet."""
    serve(harness.transport)
    controller = await harness.auto(departure=time(8, 10), requested_kwh=10.0)

    snapshot = controller.snapshot()
    assert snapshot.state == "planning_unavailable" and snapshot.reason == "deadline_too_short"
    assert snapshot.proposal is None or snapshot.proposal.has_plan is False
    assert_nothing_executed(no_execution)


async def test_a_currency_with_no_published_rate_is_named(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """DKK with no DKK rate in the day's own table: refused, never converted at 1:1."""
    serve(harness.transport, "DK1", days=(TODAY,))
    controller = await harness.auto(area_id="DK1")

    snapshot = controller.snapshot()
    assert snapshot.currency == "DKK"
    assert snapshot.state == "planning_unavailable" and snapshot.reason == "missing_fx_rate"
    assert_nothing_executed(no_execution)


# ----------------------------------------------------------------- target mode


async def test_target_mode_needs_a_vehicle_to_read(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A target with no vehicle named is incomplete, not a guess about a car."""
    serve(harness.transport)
    controller = await harness.auto(driver=DRIVER_TARGET_SOC, target=TargetSocIntent(target_percent=80))

    snapshot = controller.snapshot()
    assert snapshot.state == "incomplete_settings" and snapshot.missing == ("vehicle",)
    assert_nothing_executed(no_execution)


@pytest.mark.parametrize(
    "reader",
    [
        None,
        lambda _vehicle_id: None,
        lambda vehicle_id: LiveVehicleFacts(vehicle_id=vehicle_id),
    ],
    ids=["no-reader", "no-vehicle", "no-reading"],
)
async def test_target_mode_without_a_live_reading_is_incomplete(
    harness: Harness, no_execution: dict[str, Any], reader: Any
) -> None:
    """Live state of charge is never inferred from settings: absent means incomplete."""
    serve(harness.transport)
    harness.vehicle_reader = reader
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-1", target_percent=80),
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "incomplete_settings" and snapshot.reason == "target_soc_unknown"
    assert snapshot.missing == ("live_soc",)
    assert snapshot.proposal is None
    assert_nothing_executed(no_execution)


async def test_target_mode_without_any_capacity_is_incomplete(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A state of charge and a target are not enough without a battery size."""
    serve(harness.transport)
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(vehicle_id=vehicle_id, soc_percent=50.0)
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-1", target_percent=80),
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "incomplete_settings" and snapshot.reason == "target_capacity_unknown"
    assert snapshot.missing == ("capacity",)
    assert snapshot.proposal is None
    assert_nothing_executed(no_execution)


async def test_target_mode_plans_from_the_reported_capacity(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """The vehicle's resolved figure is the one used: 30 % of 40 kWh is 12 kWh (13.3 from the wall)."""
    serve(harness.transport)
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(
        vehicle_id=vehicle_id, soc_percent=50.0, reported_capacity_kwh=40.0
    )
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-1", target_percent=80),
    )

    snapshot = controller.snapshot()
    assert snapshot.proposal is not None and snapshot.proposal.requested_kwh == pytest.approx(12.0 / 0.9)
    assert_nothing_executed(no_execution)


async def test_the_vehicles_own_ceiling_caps_the_target(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """A stored 90 % against a car that stops at 70 % is 20 % of 60 kWh, not 40 %."""
    serve(harness.transport)
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(
        vehicle_id=vehicle_id, soc_percent=50.0, reported_capacity_kwh=60.0, max_percent=70.0
    )
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-1", target_percent=90),
    )

    snapshot = controller.snapshot()
    assert snapshot.proposal is not None and snapshot.proposal.requested_kwh == pytest.approx(12.0 / 0.9)
    assert_nothing_executed(no_execution)


async def test_a_car_already_at_its_target_needs_nothing(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """Nothing to charge is its own state: not a plan with a 1 kWh floor in it."""
    serve(harness.transport)
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(
        vehicle_id=vehicle_id, soc_percent=90.0, reported_capacity_kwh=60.0
    )
    controller = await harness.auto(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-1", target_percent=80),
    )

    snapshot = controller.snapshot()
    assert snapshot.state == "nothing_to_charge" and snapshot.reason == "already_at_target"
    assert snapshot.proposal is None and snapshot.applied is False
    assert_nothing_executed(no_execution)


# ------------------------------------------------------------------- listeners


class CapturedListeners:
    """The listeners the controller handed the manager, captured at its public call.

    The manager's `async_subscribe` is wrapped rather than the controller's internals
    reached into: what is captured is exactly what the manager was given, so a test can
    deliver a *late* callback the way the manager would.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, manager: PriceRefreshManager) -> None:
        self.rows: list[tuple[str, Any]] = []
        original = manager.async_subscribe

        async def spy(*, owner_id: str, area_id: str, listener: Any = None, tz: str | None = None):
            unsubscribe = await original(owner_id=owner_id, area_id=area_id, listener=listener, tz=tz)
            self.rows.append((area_id, listener))
            return unsubscribe

        monkeypatch.setattr(manager, "async_subscribe", spy)


async def test_an_identical_recalculation_is_not_news(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """Same prices, same settings, same plan: nobody is told twice."""
    serve(harness.transport)
    controller = await harness.auto()
    recording = Recording()
    controller.add_listener(recording)
    assert recording.states == ["proposal_ready"]
    timers = len(harness.scheduler.pending)

    await controller.async_recalculate()
    await controller.async_recalculate()

    assert recording.states == ["proposal_ready"]
    assert len(harness.scheduler.pending) == timers, "a recalculation arms nothing"
    assert_nothing_executed(no_execution)


async def test_the_same_cost_at_different_hours_is_news(harness: Harness) -> None:
    """Equal money and equal slot count are not equal plans: the hours changed.

    Two facts, in the two places they live. The live one: a period-cap change inside the 24-hour window
    moves this day's plan to other hours at the same money (equal costs go to the latest slots), and
    the key a listener is notified by is built from the proposal's own *hours*, so it is news. And the
    deciding one: a plan moved by an hour -- same money, same slot count -- is news too.
    """
    serve(harness.transport)
    for day in _BOTH:
        harness.transport.serve(
            harness.transport.day_path(SE4, day), 200, cheap_midday_day(SE4, day)
        )
    controller = await harness.auto(max_periods=1)
    recording = Recording()
    controller.add_listener(recording)
    first = controller.snapshot()

    second = await controller.async_apply_settings(
        mutate=lambda settings: replace(settings, max_periods=2)
    )

    assert first.proposal is not None and second.proposal is not None
    assert second.proposal.slots_needed == first.proposal.slots_needed
    assert second.proposal.estimated_cost == first.proposal.estimated_cost
    assert second.proposal.periods != first.proposal.periods, "the same money, spent at other hours"
    assert second.meaningful_key() != first.meaningful_key()
    assert recording.states == ["proposal_ready", "proposal_ready"]

    # The rule itself, on the values the key is made of: same count, same cost, one hour later.
    shifted = replace(
        second,
        proposal=replace(
            second.proposal,
            periods=tuple((start + timedelta(hours=1), end + timedelta(hours=1)) for start, end in second.proposal.periods),
            slots=tuple(
                replace(slot, start=slot.start + timedelta(hours=1), end=slot.end + timedelta(hours=1))
                for slot in second.proposal.slots
            ),
        ),
    )
    assert shifted.meaningful_key() != second.meaningful_key(),         "a plan that charges at different hours is a different plan"
    assert shifted.proposal.estimated_cost == second.proposal.estimated_cost
    assert len(shifted.proposal.periods) == len(second.proposal.periods)


async def test_state_reason_and_error_changes_are_news(harness: Harness) -> None:
    """Everything a reader can see is worth hearing about, once per change."""
    serve(harness.transport)
    controller = await harness.auto()
    recording = Recording()
    controller.add_listener(recording)

    with pytest.raises(AutoSettingsError):
        await controller.async_apply_settings(mutate=lambda settings: replace(settings, amps=999))
    assert controller.snapshot().last_error_code == "invalid_amps"

    await controller.async_apply_settings(mutate=lambda settings: replace(settings, amps=12))

    assert recording.states == ["proposal_ready", "proposal_ready", "proposal_ready"]
    assert recording.snapshots[1].last_error_code == "invalid_amps"


async def test_one_broken_listener_does_not_stop_the_others(harness: Harness) -> None:
    """A listener that raises is logged, and the rest of them still run."""
    serve(harness.transport)
    controller = await harness.auto()
    broken = Recording(raises=True)
    healthy = Recording()
    controller.add_listener(broken)
    controller.add_listener(healthy)

    await controller.async_apply_settings(mutate=lambda settings: replace(settings, amps=12))

    assert broken.states == ["proposal_ready", "proposal_ready"]
    assert healthy.states == ["proposal_ready", "proposal_ready"]


async def test_a_removed_listener_is_not_called_again(harness: Harness) -> None:
    """The handle `add_listener` returns is the whole subscription, and it is idempotent."""
    serve(harness.transport)
    controller = await harness.auto()
    recording = Recording()
    remove = controller.add_listener(recording)
    assert recording.states == ["proposal_ready"]

    remove()
    remove()
    await controller.async_recalculate()

    assert recording.states == ["proposal_ready"]


async def test_a_callback_from_the_area_this_charger_left_is_inert(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late notification for the old area must not publish anything at all."""
    serve(harness.transport)
    serve(harness.transport, DE_LU, days=(TODAY, TOMORROW))
    serve_index(harness.transport, {SE4: list(_BOTH), DE_LU: list(_BOTH)})
    assert harness.manager is not None
    captured = CapturedListeners(monkeypatch, harness.manager)
    controller = await harness.auto()
    recording = Recording()
    controller.add_listener(recording)

    await controller.async_apply_settings(mutate=lambda settings: replace(settings, area_id=DE_LU))
    after_move = controller.snapshot()
    assert after_move.area_id == DE_LU
    delivered = len(recording.snapshots)

    # The manager was holding this listener for the area the charger left behind.
    old_area, old_listener = captured.rows[0]
    assert old_area == SE4 and old_listener is not None
    await harness.manager.async_subscribe(owner_id="probe", area_id=SE4, listener=None)
    old_listener(harness.manager.area_snapshot(SE4))
    await harness.hass.async_block_till_done()

    assert controller.snapshot() is after_move
    assert len(recording.snapshots) == delivered


async def test_a_callback_after_shutdown_is_inert(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown is terminal: nothing a late callback does can publish again."""
    serve(harness.transport)
    assert harness.manager is not None
    captured = CapturedListeners(monkeypatch, harness.manager)
    controller = await harness.auto()
    await controller.async_shutdown()
    stopped = controller.snapshot()
    assert stopped.state == "planning_unavailable" and stopped.reason == "shutdown"

    _area, listener = captured.rows[-1]
    listener(harness.manager.area_snapshot(SE4))
    await harness.hass.async_block_till_done()

    assert controller.snapshot() is stopped


# ------------------------------------------------- real entries (lifecycle, authority)


async def post_action(client: Any, webhook_id: str, **payload: Any) -> Any:
    """Send one action to a charger's webhook the way the app does."""
    return await client.post(f"/api/webhook/{webhook_id}", json={"version": 1, **payload})


def schedule_payload(**overrides: Any) -> dict[str, Any]:
    """A schedule the controller's own rules accept, unless a test changes a field."""
    start, end = future_window()
    payload: dict[str, Any] = {
        "action": "schedule",
        "start": start,
        "end": end,
        "amps": 10,
        "phases": 3,
    }
    payload.update(overrides)
    return payload


async def test_unload_stops_the_preview_and_keeps_the_settings(hass: HomeAssistant) -> None:
    """An unload is not a deletion: the preview goes, the intent stays."""
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id, amps=16)
    store = domain_data(hass).auto_store
    assert store is not None
    revision = store.settings(entry.entry_id).revision

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert preview_for(hass, entry.entry_id) is None
    assert store.settings(entry.entry_id).amps == 16
    assert store.settings(entry.entry_id).revision == revision


async def test_unloading_one_entry_leaves_another_entrys_preview_running(
    hass: HomeAssistant,
) -> None:
    """The preview is per entry: one charger unloading must not stop another."""
    first = await setup_charger(hass, entry_id="entry_a", charge_control="switch.charger_a")
    second = await setup_charger(
        hass,
        entry_id="entry_b",
        charge_control="switch.charger_b",
        webhook_id="webhook-b",
    )
    await go_auto(hass, first.entry_id)
    await go_auto(hass, second.entry_id)
    shelf_life = preview_for(hass, second.entry_id)
    assert shelf_life is not None

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()

    assert preview_for(hass, first.entry_id) is None
    assert preview_for(hass, second.entry_id) is shelf_life
    assert domain_data(hass).auto_store.settings(first.entry_id).amps == 10


async def test_reload_restores_the_settings_and_a_new_preview(hass: HomeAssistant) -> None:
    """A reload finds the settings it had, and a fresh controller rather than the old one."""
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id, amps=16)
    before = preview_for(hass, entry.entry_id)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = preview_for(hass, entry.entry_id)
    assert after is not None and after is not before
    assert after.snapshot().settings_revision == 1
    assert domain_data(hass).auto_store.settings(entry.entry_id).amps == 16


async def test_deleting_one_entry_removes_only_its_auto_state(hass: HomeAssistant) -> None:
    """`async_remove_entry` is the one place settings disappear, and it is per entry."""
    first = await setup_charger(hass, entry_id="entry_a", charge_control="switch.charger_a")
    second = await setup_charger(
        hass,
        entry_id="entry_b",
        charge_control="switch.charger_b",
        webhook_id="webhook-b",
    )
    await go_auto(hass, first.entry_id, amps=16)
    await go_auto(hass, second.entry_id, amps=32)
    store = domain_data(hass).auto_store
    assert store is not None
    assert store.entry_ids() == ("entry_a", "entry_b")

    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()

    assert store.entry_ids() == ("entry_b",)
    assert store.settings("entry_b").amps == 32
    assert preview_for(hass, "entry_b") is not None


async def test_removing_a_site_entry_never_touches_auto_settings(hass: HomeAssistant) -> None:
    """A site entry has no Auto state of its own, so its removal must change nothing."""
    charger = await setup_charger(hass)
    await go_auto(hass, charger.entry_id)
    store = domain_data(hass).auto_store
    assert store is not None
    before = store.settings(charger.entry_id)

    site = make_site_entry(hass, entry_id="entry_site")
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(site.entry_id)
    await hass.async_block_till_done()

    assert store.settings(charger.entry_id) == before
    assert store.entry_ids() == (charger.entry_id,)
    assert preview_for(hass, charger.entry_id) is not None


async def test_a_failed_preview_start_leaves_nothing_running(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setup that fails half way stops what it started: no boundary, no webhook, no subscription."""

    async def explode(self: AutoPlannerController) -> Any:
        raise RuntimeError("no subscription today")

    monkeypatch.setattr(AutoPlannerController, "async_start", explode)
    entry = make_entry(
        hass,
        entry_id="entry_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="entry_a",
    )

    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "webhook-a" not in hass.data["webhook"]
    assert entry.runtime_data.executor._shutdown is True
    manager = domain_data(hass).price_refresh
    assert manager is not None and manager.area_snapshot(SE4) is None


async def test_the_domain_stop_shuts_every_preview_down(hass: HomeAssistant) -> None:
    """Home Assistant stopping takes the price manager and the previews with it."""
    first = await setup_charger(hass, entry_id="entry_a", charge_control="switch.charger_a")
    second = await setup_charger(
        hass, entry_id="entry_b", charge_control="switch.charger_b", webhook_id="webhook-b"
    )
    await go_auto(hass, first.entry_id, amps=16)
    await go_auto(hass, second.entry_id, amps=32)
    controller = preview_for(hass, first.entry_id)
    other = preview_for(hass, second.entry_id)
    assert controller is not None and other is not None

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert domain_data(hass).price_refresh is None
    assert other.snapshot().reason == "shutdown"
    assert controller.snapshot().state == "planning_unavailable"
    assert controller.snapshot().reason == "shutdown"
    # Terminal: a recalculation after the stop publishes nothing new.
    assert await controller.async_recalculate() == controller.snapshot()
    # And the settings are still exactly what they were.
    assert domain_data(hass).auto_store.settings(first.entry_id).amps == 16
    assert domain_data(hass).auto_store.settings(second.entry_id).amps == 32


# ---------------------------------------------------------------- installation spy


@pytest.fixture
def install_spy(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Count every real installation, through the controller's own method."""
    plans: list[Any] = []
    original = ChargingController.async_install

    async def spy(self: ChargingController, plan: Any) -> None:
        plans.append(plan)
        return await original(self, plan)

    monkeypatch.setattr(ChargingController, "async_install", spy)
    return plans


# ------------------------------------------------------------------- diagnostics


def assert_dump_is_summary(section: dict[str, Any]) -> None:
    """The Auto section is scalars, short labels and period pairs — never data.

    A diagnostics dump has to answer "why is this charger not charging by itself" without
    carrying the thing it is describing: no price array, no day document, no slot list. The
    one list that may be long is a proposal's periods, and each entry must then be the pair
    of instants that makes it an *interval* rather than a bare start.
    """

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            if path.endswith("periods"):
                for pair in value:
                    assert isinstance(pair, list) and len(pair) == 2, f"{path} is not a pair"
                    assert all(isinstance(instant, str) and "T" in instant for instant in pair)
                return
            assert len(value) <= 8, f"{path} looks like data: {len(value)} items"
            assert all(isinstance(item, str) and len(item) <= 24 for item in value), path

    walk(section, "auto_price")
    # `charging_without_prices` is a stable reason code, not price data.
    blob = json.dumps(section).replace("charging_without_prices", "")
    for forbidden in ("prices", "document", "interval", "Traceback", "webhook"):
        assert forbidden not in blob, f"{forbidden!r} leaked into the Auto diagnostics"
    # No slot list under any name: counts may be reported, the slots themselves may not.
    assert "slots" not in keys_of(section)
    for identifying in ("entity_id", "vehicle_id", "device_id"):
        assert identifying not in blob


def keys_of(value: Any) -> set[str]:
    """Every mapping key in a nested structure, which is where a leaked field would sit."""
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            found.add(str(key))
            found |= keys_of(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= keys_of(item)
        return found
    return set()


async def auto_section(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """The one Auto section of a diagnostics dump, through the public entry point."""
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert list(diagnostics).count("auto_price") == 1
    section = diagnostics["auto_price"]
    assert_dump_is_summary(section)
    assert str(entry.entry_id) not in json.dumps(section)
    return section


def registered_entry(hass: HomeAssistant) -> Any:
    """A charger entry registered with Home Assistant but deliberately *not* set up.

    Diagnostics reads the entry for its id and its config; nothing about the Auto section
    needs the entry to be loaded, and a dump of a broken entry is exactly when it matters.
    """
    return make_entry(
        hass,
        entry_id="entry-a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="entry-a",
    )


async def test_diagnostics_describe_every_auto_state(
    hass: HomeAssistant, harness: Harness, transport: StubTransport
) -> None:
    """One section per state, flat, redacted, and never raising."""
    entry = registered_entry(hass)

    # 1. Nothing stored at all: no preview, and the store's own defaults.
    absent = await auto_section(hass, entry)
    assert absent["available"] is True and absent["live"] is False
    assert absent["proposal"] is None
    assert absent["settings_revision"] == 0

    # 2. Auto without the inputs it needs.
    controller = await harness.preview()
    incomplete = await auto_section(hass, entry)
    assert incomplete["live"] is True
    assert incomplete["state"] == "incomplete_settings"
    assert incomplete["missing"] == ["area", "amps"]

    # 3. A ready proposal.
    await controller.async_apply_settings(
        mutate=lambda settings: replace(settings, area_id=SE4, amps=10, phases=1, departure_enabled=False)
    )
    ready = await auto_section(hass, entry)
    assert ready["state"] == "proposal_ready" and ready["reason"] == "ready"
    assert ready["currency"] == "SEK" and ready["major_unit"] == "kr" and ready["minor_unit"] == "öre"
    assert ready["today"] == TODAY.isoformat() and ready["tomorrow"] == TOMORROW.isoformat()
    assert ready["price_identity"] is not None and ready["price_state"] == "ready"
    assert ready["priced_slots"] > 0 and ready["unpriced_slots"] == 0
    assert ready["unpriced"] is False and ready["applied"] is False
    assert ready["error_code"] is None
    proposal = ready["proposal"]
    assert proposal["historical"] is False and proposal["in_process"] is True
    assert proposal["applied"] is False and len(proposal["periods"]) >= 1

    # 4. Prices that stopped being published: the same proposal, a different state.
    serve_index(harness.transport, {SE4: [YESTERDAY]})
    await harness.tick()
    stale = await auto_section(hass, entry)
    assert stale["state"] == "price_data_stale"
    assert stale["proposal"]["in_process"] is True
    assert stale["proposal"]["applied"] is False

    # 5. Shut down: the live preview is gone and the stored summary is history.
    await controller.async_shutdown()
    entry.runtime_data.preview = None
    stopped = await auto_section(hass, entry)
    assert stopped["available"] is True and stopped["live"] is False
    assert stopped["proposal"]["historical"] is True
    assert stopped["proposal"]["applied"] is False


async def test_the_appointment_at_the_latest_safe_start_replans_by_itself(
    harness: Harness, no_execution: dict[str, Any]
) -> None:
    """Nothing but the clock moves: the timer armed while waiting is what turns the wait into the guarantee."""
    serve(harness.transport, days=(TODAY,), listed=(TODAY,))
    harness.transport.serve(
        harness.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY)
    )
    controller = await harness.auto(departure=time(8, 0))
    assert controller.snapshot().state == "waiting_for_publication"
    wakes = [t for t in harness.scheduler.pending if t.when.tzinfo is timezone.utc and t.when.hour == 18]
    assert len(wakes) == 1

    harness.clock.now = wakes[0].when
    wakes[0].fire()
    await harness.hass.async_block_till_done()

    assert controller.snapshot().reason == "charging_without_prices"
    assert wakes[0].fired


async def test_diagnostics_report_a_charge_without_prices_honestly(
    hass: HomeAssistant, harness: Harness
) -> None:
    """The guarantee is described as a plan with no published prices, counts and all."""
    entry = registered_entry(hass)
    serve(harness.transport, days=(TODAY,), listed=(TODAY,))
    harness.transport.serve(
        harness.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY)
    )
    harness.clock.now = datetime(2026, 9, 22, 19, 30, tzinfo=timezone.utc)
    await harness.auto("entry-a", departure=time(8, 0))

    section = await auto_section(hass, entry)

    assert section["state"] == "proposal_unpriced"
    assert section["reason"] == "charging_without_prices"
    assert section["unpriced"] is True
    assert section["unpriced_slots"] > 0
    assert section["proposal"]["unpriced"] is True
    assert section["proposal"]["unpriced_slots"] == section["unpriced_slots"]
    assert section["applied"] is False


async def test_diagnostics_never_mention_a_vehicle_or_an_exception(
    hass: HomeAssistant, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing that identifies a car, and no exception text, has a path into a dump."""
    entry = registered_entry(hass)
    harness.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(
        vehicle_id=vehicle_id, soc_percent=50.0, reported_capacity_kwh=60.0
    )
    await harness.auto(
        "entry-a",
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-secret", target_percent=80),
    )

    section = await auto_section(hass, entry)

    assert "veh-secret" not in json.dumps(section)
    assert section["state"] == "proposal_ready"

    def explode(self: AutoPlannerController) -> Any:
        # Deliberately *not* a coroutine: `snapshot` is synchronous, and patching it with an
        # async function would leave an un-awaited coroutine rather than proving anything.
        raise RuntimeError("a secret-looking failure")

    monkeypatch.setattr(AutoPlannerController, "snapshot", explode)
    broken = await auto_section(hass, entry)
    assert broken == {"available": False, "reason": "unavailable"}


async def test_diagnostics_with_nothing_set_up_are_explicit(hass: HomeAssistant) -> None:
    """Before the domain has ever been set up, the section says so rather than guessing."""
    entry = registered_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["auto_price"] == {"available": False, "reason": "not_set_up"}


async def test_no_auto_path_ever_reaches_a_charger(
    hass: HomeAssistant, harness: Harness, no_execution: dict[str, Any]
) -> None:
    """The whole battery, with every route closed: Auto calculates and does nothing else.

    Every kind of Auto action in one run — entering Auto, calculating, being refused,
    changing area and mode, target-SoC resolution, an estimated plan, a planner refusal,
    stale prices — and then the assertion that a charger was never in reach. The controller
    methods are spied on at the class and the services at the registry, so a plan that was
    only *queued* for installation would fail this too.
    """
    harness.transport.serve_area()
    controller = await harness.auto("entry-a")

    await controller.async_recalculate()
    with pytest.raises(AutoSettingsError):
        await controller.async_apply_settings(mutate=lambda settings: replace(settings, phases=2))
    await controller.async_apply_settings(
        mutate=lambda settings: replace(settings, area_id=DE_LU)
    )
    await controller.async_apply_settings(
        mutate=lambda settings: replace(
            settings, area_id=SE4, driver=DRIVER_TARGET_SOC, target=TargetSocIntent(vehicle_id="v")
        )
    )
    await controller.async_apply_settings(
        mutate=lambda settings: replace(
            settings,
            driver="manual_kwh",
            target=TargetSocIntent(),
            departure_enabled=True,
            departure=time(9, 0),
            requested_kwh=200.0,
        )
    )
    await controller.async_apply_settings(
        mutate=lambda settings: replace(
            settings,
            departure_enabled=False,
            requested_kwh=20.0,
        )
    )
    serve_index(harness.transport, {SE4: [YESTERDAY]})
    await harness.tick()

    assert_nothing_executed(no_execution)
    # And the charger's own plan was never set: nothing was even described to it.


@freeze_time("2026-09-22 06:00:00")
async def test_manual_actions_never_change_the_settings(
    hass: HomeAssistant,
    hass_client_no_auth: Any,
    install_spy: list[Any],
) -> None:
    """Starting or stopping a charge by hand pauses Auto for the plug-in session and changes no planning
    setting: what Auto plans once it resumes is what it planned before."""
    hass.states.async_set("switch.charger_a", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    entry = await setup_charger(hass)
    await go_auto(hass, entry.entry_id)
    controller = preview_for(hass, entry.entry_id)
    store = domain_data(hass).auto_store
    assert controller is not None and store is not None
    planning = replace(store.settings(entry.entry_id), revision=0, pause=PauseIntent())
    installed = controller.snapshot().applied_identity
    installs_before = len(install_spy)
    assert installed is not None, "Auto installed its proposal when it calculated"
    client = await hass_client_no_auth()

    responses = [
        await post_action(client, "webhook-a", action="start", amps=16),
        await post_action(client, "webhook-a", action="stop"),
    ]
    await hass.async_block_till_done()

    assert all(response.status == 200 for response in responses)
    # The planning settings are exactly what they were, and the actions caused no installation of their own.
    after = store.settings(entry.entry_id)
    assert replace(after, revision=0, pause=PauseIntent()) == planning
    assert (after.pause.choice, after.pause.action) == ("manual", "stop")
    assert len(install_spy) == installs_before
    still_auto = await controller.async_recalculate()
    assert still_auto.state in ("proposal_ready", "proposal_unpriced", "waiting_for_prices")


async def test_the_area_listener_given_to_the_manager_is_a_loop_callback(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager calls listeners on the loop; the marker keeps that a contract, not luck."""
    from homeassistant.core import is_callback

    serve(harness.transport)
    assert harness.manager is not None
    captured = CapturedListeners(monkeypatch, harness.manager)
    await harness.auto()

    assert captured.rows and all(is_callback(listener) for _area, listener in captured.rows)
