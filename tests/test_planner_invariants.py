"""Structural invariants that hold for every plan the planner produces.

A golden scenario pins the behaviour it is named for; these pin the properties that must
hold for *all* of them, so a scenario does not have to restate its whole result to be
trusted. They are also where the architectural boundary lives: the planner is arithmetic,
and these tests prove that by running it with no event loop, no network and no HA service
surface anywhere in reach.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.planner import (
    STEP_MINUTES,
    calculate_plan,
    effective_minor_per_kwh,
    energy_per_slot_kwh,
    power_kw,
    resolve_departure,
    slots_needed,
)
from tests.planning import build_request, fixture_payload, parse_documents

SUCCESSFUL = [
    scenario
    for scenario in fixture_payload()["scenarios"]
    if scenario["expect"]["reason"] is None
]
TOLERANCE = fixture_payload()["tolerance"]


@pytest.mark.parametrize("scenario", SUCCESSFUL, ids=lambda item: item["name"])
def test_repeated_calculation_is_equal_and_immutable(scenario: dict[str, Any]) -> None:
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    first = calculate_plan(request)
    second = calculate_plan(request)
    assert first == second
    assert first is not second  # a fresh immutable value each time, never a shared cache
    with pytest.raises(Exception):
        first.reason = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("scenario", SUCCESSFUL, ids=lambda item: item["name"])
def test_inputs_are_unchanged_by_a_calculation(scenario: dict[str, Any]) -> None:
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    before = request
    documents_before = tuple(request.documents)
    prices_before = tuple(document.prices for document in documents_before)
    intervals_before = tuple(document.intervals for document in documents_before)

    calculate_plan(request)

    assert request == before
    assert tuple(request.documents) == documents_before
    assert tuple(document.prices for document in request.documents) == prices_before
    assert tuple(document.intervals for document in request.documents) == intervals_before


@pytest.mark.parametrize("scenario", SUCCESSFUL, ids=lambda item: item["name"])
def test_slots_are_unique_ordered_on_the_grid_and_fifteen_minutes(scenario: dict[str, Any]) -> None:
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)
    starts = [slot.start for slot in result.slots]

    assert starts == sorted(starts), "slots must be ordered by UTC instant"
    assert len(set(starts)) == len(starts), "slots must be unique"
    for slot in result.slots:
        assert slot.start.tzinfo is not None and slot.end.tzinfo is not None
        assert slot.end - slot.start == timedelta(minutes=STEP_MINUTES)
        # On the absolute 15-minute grid: whole quarter hours since the epoch, to the second.
        epoch_seconds = (slot.start.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
        assert epoch_seconds % (STEP_MINUTES * 60) == 0, slot.start.isoformat()


@pytest.mark.parametrize("scenario", SUCCESSFUL, ids=lambda item: item["name"])
def test_periods_are_the_merge_of_adjacent_slots_and_within_the_cap(scenario: dict[str, Any]) -> None:
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)

    merged: list[list[datetime]] = []
    for slot in result.slots:
        if merged and merged[-1][1] == slot.start:
            merged[-1][1] = slot.end
        else:
            merged.append([slot.start, slot.end])
    assert result.periods == tuple((start, end) for start, end in merged)

    previous_end: datetime | None = None
    for start, end in result.periods:
        assert end > start
        if previous_end is not None:
            assert start >= previous_end, "periods must not overlap"
        previous_end = end
    assert len(result.periods) <= request.max_periods
    assert len(result.periods) >= 1


@pytest.mark.parametrize("scenario", SUCCESSFUL, ids=lambda item: item["name"])
def test_every_slot_ends_by_the_independently_resolved_deadline(scenario: dict[str, Any]) -> None:
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)
    if request.departure is None:
        # Without a departure the only bound is the 24-hour horizon from the first slot.
        assert result.periods[-1][1] <= result.slots[0].start + timedelta(hours=24, minutes=STEP_MINUTES * len(result.slots))
        return

    zone = dt_util.get_time_zone(request.timezone)
    local = request.now.astimezone(zone)
    floored = local - timedelta(minutes=local.minute % STEP_MINUTES, seconds=local.second, microseconds=local.microsecond)
    first = floored if floored >= local else floored + timedelta(minutes=STEP_MINUTES)
    deadline = resolve_departure(request.now, request.timezone, request.departure, first).astimezone(timezone.utc)

    assert max(slot.end for slot in result.slots) <= deadline
    assert all(end <= deadline for _, end in result.periods)


@pytest.mark.parametrize("scenario", SUCCESSFUL, ids=lambda item: item["name"])
def test_counts_agree_with_the_slots(scenario: dict[str, Any]) -> None:
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)

    assert result.slots_needed == len(result.slots)
    assert result.slots_needed == slots_needed(request.requested_kwh, request.amps, request.phases)
    assert result.priced_slots + result.unpriced_slots == result.slots_needed
    assert result.unpriced_slots == sum(1 for slot in result.slots if slot.unpriced)
    assert result.unpriced == (result.unpriced_slots > 0)


@pytest.mark.parametrize("scenario", SUCCESSFUL, ids=lambda item: item["name"])
def test_energy_cost_and_distance_recompute_from_the_slots(scenario: dict[str, Any]) -> None:
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)

    per_slot = energy_per_slot_kwh(request.amps, request.phases)
    assert result.power_kw == pytest.approx(power_kw(request.amps, request.phases), abs=TOLERANCE)
    assert result.delivered_kwh == pytest.approx(result.slots_needed * per_slot, abs=TOLERANCE)
    assert result.requested_kwh == pytest.approx(request.requested_kwh, abs=TOLERANCE)

    expected_cost = sum(
        per_slot * effective_minor_per_kwh(slot.local_major_per_kwh, request.fiscal) for slot in result.slots
    ) / 100
    assert result.estimated_cost == pytest.approx(expected_cost, abs=TOLERANCE)
    for slot in result.slots:
        assert slot.effective_minor_per_kwh == pytest.approx(
            effective_minor_per_kwh(slot.local_major_per_kwh, request.fiscal), abs=TOLERANCE
        )

    # Distance uses the validated consumption input exactly, with no defensive floor.
    assert result.distance_mil == pytest.approx(result.delivered_kwh / request.consumption_kwh_per_10km, abs=TOLERANCE)


def test_a_sub_tenth_consumption_is_used_exactly() -> None:
    """The deliberate divergence: a positive 0.05 kWh/10 km means what it says."""
    scenario = next(s for s in SUCCESSFUL if s["name"] == "power_single_phase_whole_slot_overdelivery")
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)
    thrifty = build_request(
        {**scenario["request"], "consumption_kwh_per_10km": 0.05}, parse_documents(scenario["documents"])
    )
    thrifty_result = calculate_plan(thrifty)

    assert thrifty_result.distance_mil == pytest.approx(thrifty_result.delivered_kwh / 0.05, abs=TOLERANCE)
    assert thrifty_result.distance_mil == pytest.approx(result.distance_mil * 40, rel=1e-9)
    # Zero, negative and non-finite consumption are still refused.
    for bad in (0, -0.05, float("nan"), float("inf")):
        with pytest.raises(Exception):
            build_request({**scenario["request"], "consumption_kwh_per_10km": bad}, parse_documents(scenario["documents"]))


def test_planning_never_mutates_its_inputs_or_a_container_holding_them() -> None:
    """Even a refused calculation (a price gap) and its gap report leave the documents alone."""
    scenario = next(
        s for s in fixture_payload()["scenarios"] if s["name"] == "late_night_future_is_unpublished"
    )
    documents = parse_documents(scenario["documents"])
    snapshot = {"documents": documents, "prices": {document.day: document.prices for document in documents}}
    before = {document.day: document.prices for document in documents}
    request = build_request(scenario["request"], documents)

    result = calculate_plan(request)

    assert result.reason == "insufficient_price_horizon", "the estimated fallback is retired"
    assert result.slots == () and result.unpriced_slots == 0
    assert {document.day: document.prices for document in documents} == before
    assert snapshot["documents"] == documents
    assert snapshot["prices"] == before


# ------------------------------------------------------------------ purity boundary


def test_calculation_needs_no_event_loop_and_no_network() -> None:
    """A synchronous call, with sockets and the HTTP client themselves booby-trapped."""
    scenario = next(s for s in SUCCESSFUL if s["name"] == "power_single_phase_whole_slot_overdelivery")
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))

    def forbidden_connect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the planner must not open a network connection")

    class ForbiddenSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("the planner must not construct an HTTP client")

    original_connect = socket.socket.connect
    socket.socket.connect = forbidden_connect  # type: ignore[assignment]
    try:
        import aiohttp

        original_session = aiohttp.ClientSession
        aiohttp.ClientSession = ForbiddenSession  # type: ignore[misc]
        try:
            result = calculate_plan(request)
        finally:
            aiohttp.ClientSession = original_session  # type: ignore[misc]
    finally:
        socket.socket.connect = original_connect  # type: ignore[assignment]

    assert result.has_plan
    # And there is no running loop to have needed: this test is synchronous, and asking
    # for a loop here fails, which is what "no event loop required" means.
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
