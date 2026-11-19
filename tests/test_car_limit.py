"""A car that takes clearly less than the charger gives it is noticed within minutes and the plan counts on that.

The field case (SN-NKA4, 2026-11-17): a BMW i4 on an Alfen planned at 20 A on three phases (13.9 kW) takes about
16 A (11.2 kW). The plan watch judged the window at its end only (load balancing had held it a moment), where it
delivered just above the share it lets pass, and the learned power had one plug-in of nine minutes, not trusted. So a
clean night would still have ended short. Now a steady stretch of the charge in which the car draws below 90 % of
the current in force is the car's own limit (`planning/car_limit.py`): the plan is made again at once with it, it is
kept with the plug-in, and the next plug-in plans with it.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning import auto_controller as auto_controller_module
from custom_components.spotnav.planning.auto_settings import TargetSocIntent
from custom_components.spotnav.planning.car_limit import CarLimitWatch, DWELL_S, LimitFacts, SETTLE_S
from custom_components.spotnav.planning.learned_power import subject_for, Wiring
from custom_components.spotnav.planning.planner import power_kw
from custom_components.spotnav.runtime import preview_for

from .relay import serve
from .test_charge_behind_plan import _record_starts_and_stops
from .test_dashboard_api import NOW
from .world import charger_and_car, controller_of, entity_id, go_auto, REGISTER

pytestmark = pytest.mark.usefixtures("offline_relay")

PLANNED_KW = power_kw(20, 3)
#: What an i4 takes: about 16 A on each phase.
TAKEN_KW = power_kw(16, 3)
WIRING = Wiring(phases=3, voltage_v=400.0, amps=20)
ATTRIBUTES = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
WINDOW_START = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)


def _record_limit_replans(preview: Any) -> list[datetime]:
    """When the plan was made again for the car's own limit."""
    replans: list[datetime] = []
    original = preview._async_replan_behind  # noqa: SLF001

    async def recording(*, rebase: bool = False) -> None:
        if rebase:
            replans.append(dt_util.utcnow())
        await original(rebase=rebase)

    preview._async_replan_behind = recording  # noqa: SLF001
    return replans


async def _charge(hass: HomeAssistant, frozen: Any, minutes: int, register: float, kw: float) -> float:
    """`minutes` one-minute steps, the car drawing `kw` while the switch is on, the register posting each minute."""
    for _minute in range(minutes):
        frozen.tick(timedelta(minutes=1))
        if hass.states.get("switch.wallbox").state == "on":
            register += kw / 60
        hass.states.async_set(REGISTER, f"{register:.4f}", ATTRIBUTES)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    return register


def _plan_end(preview: Any) -> datetime | None:
    proposal = preview.snapshot().proposal
    return None if proposal is None or not proposal.has_plan else proposal.periods[-1][1]


# ------------------------------------------------------------------ the rule


def test_a_steady_stretch_below_its_offer_is_the_cars_limit() -> None:
    watch = CarLimitWatch()
    start = WINDOW_START
    seen = None
    for minute in range(0, 12):
        at = start + timedelta(minutes=minute)
        facts = LimitFacts(session="p", offered_kw=PLANNED_KW, written_at=start, reading_kwh=TAKEN_KW * minute / 60)
        seen = watch.check(at, facts) or seen
        if minute * 60 < SETTLE_S + DWELL_S:
            assert seen is None, f"not judged before the ramp and the dwell ({minute} min)"
    assert seen is not None and seen[0] == pytest.approx(TAKEN_KW) and seen[1] >= DWELL_S / 60
    assert watch.limit_kw("p") == seen and watch.limit_kw("another plug-in") is None


def test_a_car_taking_what_it_is_given_or_a_stretch_broken_says_nothing() -> None:
    watch = CarLimitWatch()
    for minute in range(0, 15):
        at = WINDOW_START + timedelta(minutes=minute)
        facts = LimitFacts(session="p", offered_kw=TAKEN_KW, written_at=None, reading_kwh=TAKEN_KW * 0.97 * minute / 60)
        assert watch.check(at, facts) is None
    assert watch.last_measure is not None and not watch.last_measure.limited
    # A current written every few minutes (load balancing) restarts the stretch each time: never a measure.
    broken = CarLimitWatch()
    for minute in range(0, 30):
        at = WINDOW_START + timedelta(minutes=minute)
        written = WINDOW_START + timedelta(minutes=minute - minute % 4)
        facts = LimitFacts(session="p", offered_kw=PLANNED_KW, written_at=written, reading_kwh=TAKEN_KW * minute / 60)
        assert broken.check(at, facts) is None
    assert broken.last_measure is None


# ------------------------------------------------------------------ in Home Assistant


async def test_an_i4_at_16_a_on_a_20_a_plan_is_noticed_within_minutes_and_the_plan_extended(
    hass: HomeAssistant, transport: Any, caplog: pytest.LogCaptureFixture
) -> None:
    serve(transport, rising=True)
    _record_starts_and_stops(hass)
    with freeze_time(NOW) as frozen:
        charger, device_id, _ = await charger_and_car(hass, soc_percent="70", amps=20, phases=3)
        preview = preview_for(hass, charger.entry_id)
        replans = _record_limit_replans(preview)
        first = preview.snapshot().proposal
        assert first.power_kw == pytest.approx(PLANNED_KW)
        first_end = _plan_end(preview)
        plan = controller_of(hass, charger.entry_id).plan
        assert plan is not None and plan.windows[0][0] == WINDOW_START

        await _charge(hass, frozen, 12, 1000.0, TAKEN_KW)

        assert replans and replans[0] - WINDOW_START <= timedelta(minutes=10), "noticed within minutes"
        proposal = preview.snapshot().proposal
        assert proposal.power_kw == pytest.approx(TAKEN_KW, rel=0.02), "planned with what the car takes"
        assert _plan_end(preview) > first_end, "and more is bought than the first plan had"
        assert "the car takes" in caplog.text
        facts = preview.plan_watch_facts()
        assert facts["car_limit"]["session_kw"] == pytest.approx(TAKEN_KW, rel=0.02)
        assert facts["car_limit"]["last_measure"]["limited"]
        assert len(replans) == 1, "once: the plan then counts on what the car takes"

        # Kept with the plug-in: the learned power is trusted for the next one.
        await _charge(hass, frozen, 10, float(hass.states.get(REGISTER).state), TAKEN_KW)
        learned = preview._learned.power(subject_for(device_id), WIRING, 1.0, PLANNED_KW)  # noqa: SLF001
        assert learned.trusted and learned.cap_kw == pytest.approx(TAKEN_KW, rel=0.02)


async def test_the_learned_limit_is_planned_with_at_the_next_plug_in(hass: HomeAssistant, transport: Any) -> None:
    serve(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, _ = await charger_and_car(hass, soc_percent="70", amps=20, phases=3)
        preview = preview_for(hass, charger.entry_id)
        assert preview.snapshot().proposal.power_kw == pytest.approx(PLANNED_KW)
        # The last plug-in: its clean minutes were few (load balancing held it), but the car's limit was seen.
        preview._learned.add(subject_for(device_id), WIRING, "plug_in:last", TAKEN_KW * 9 / 60, 9)  # noqa: SLF001
        preview._learned.set_limit(subject_for(device_id), WIRING, "plug_in:last", TAKEN_KW, 25)  # noqa: SLF001
        await preview.async_recalculate()
        proposal = preview.snapshot().proposal
        assert proposal.power_kw == pytest.approx(TAKEN_KW)
        assert preview.plan_watch_facts()["learned_power"]["planned_with"]
        assert preview.plan_watch_facts()["car_limit"]["session_kw"] is None, "not this plug-in's own"


class _HoldingSite:
    """A site whose load balancing holds the charger at `limit_a`."""

    def __init__(self, limit_a: float) -> None:
        self._limit_a = limit_a

    def load_balancing_limit(self, _charger_entry_id: str) -> tuple[float | None, str | None]:
        return self._limit_a, None

    def import_target_plan_kw(self, _charger_entry_id: str) -> float | None:
        return None

    def import_target_holds(self, _charger_entry_id: str) -> bool:
        return False


@pytest.mark.parametrize(
    ("limit_a", "taken_kw"),
    [
        (16.0, TAKEN_KW),  # held at 16 A, the car takes all of it
        (6.0, power_kw(6, 3) * 0.85),  # held at 6 A, the car takes about 5.1 A of it: below the 90 % a limit needs
    ],
)
async def test_a_balancing_hold_is_not_taken_as_the_cars_limit(
    hass: HomeAssistant, transport: Any, monkeypatch: pytest.MonkeyPatch, limit_a: float, taken_kw: float
) -> None:
    serve(transport, rising=True)
    _record_starts_and_stops(hass)
    monkeypatch.setattr(
        auto_controller_module, "site_controller_for_charger", lambda _hass, _entry_id: _HoldingSite(limit_a)
    )
    with freeze_time(NOW) as frozen:
        charger, device_id, _ = await charger_and_car(hass, soc_percent="70", amps=20, phases=3)
        preview = preview_for(hass, charger.entry_id)
        replans = _record_limit_replans(preview)
        await _charge(hass, frozen, 20, 1000.0, taken_kw)
        assert not replans
        # A held current is never measured: nothing for this plug-in, nothing kept for the next.
        assert preview.plan_watch_facts()["car_limit"] == {"session_kw": None, "last_measure": None}
        learned = preview._learned.power(subject_for(device_id), WIRING, 1.0, PLANNED_KW)  # noqa: SLF001
        assert learned.kw is None and not learned.trusted, "and no clean minute either"


async def test_a_plan_at_the_chargers_least_current_measures_no_limit(hass: HomeAssistant, transport: Any) -> None:
    """At a 6 A pilot a car may take about 5.3 A: the charger's floor, not a limit of the car's."""
    serve(transport, rising=True)
    _record_starts_and_stops(hass)
    with freeze_time(NOW) as frozen:
        charger, device_id, _ = await charger_and_car(hass, soc_percent="70", amps=6, phases=3)
        preview = preview_for(hass, charger.entry_id)
        replans = _record_limit_replans(preview)
        await _charge(hass, frozen, 20, 1000.0, power_kw(6, 3) * 0.85)
        assert not replans
        assert preview.plan_watch_facts()["car_limit"] == {"session_kw": None, "last_measure": None}


async def test_the_taper_near_full_is_not_taken_as_the_cars_limit(hass: HomeAssistant, transport: Any) -> None:
    serve(transport, rising=True)
    calls = _record_starts_and_stops(hass)
    with freeze_time(NOW) as frozen:
        charger, device_id, soc = await charger_and_car(hass, soc_percent="91", amps=20, phases=3)
        await go_auto(
            hass, charger.entry_id, amps=20, phases=3, target=TargetSocIntent(vehicle_id=device_id, target_percent=100)
        )
        preview = preview_for(hass, charger.entry_id)
        assert preview.snapshot().proposal.has_plan
        replans = _record_limit_replans(preview)
        # A car tapering at 91 %: a third of the power.
        register = await _charge(hass, frozen, 15, 1000.0, PLANNED_KW / 3)
        assert [service for service, _at in calls][:1] == ["turn_on"] and register > 1001.0, "it charged"
        assert not replans
        assert preview.plan_watch_facts()["car_limit"] == {"session_kw": None, "last_measure": None}


async def test_a_limit_that_misses_the_departure_is_reported(hass: HomeAssistant, transport: Any) -> None:
    serve(transport, rising=True)
    _record_starts_and_stops(hass)
    with freeze_time(NOW) as frozen:
        # 08:45 Stockholm: three quarter-hours from 08:00, enough at 20 A, too few at 16 A.
        charger, _device_id, _ = await charger_and_car(
            hass, soc_percent="70", amps=20, phases=3, departure=time(8, 45), departure_enabled=True
        )
        preview = preview_for(hass, charger.entry_id)
        first = preview.snapshot().proposal
        assert first.has_plan and not first.short_of_deadline
        replans = _record_limit_replans(preview)

        await _charge(hass, frozen, 12, 1000.0, TAKEN_KW)

        assert replans
        snapshot = preview.snapshot()
        assert snapshot.proposal.has_plan and snapshot.proposal.short_of_deadline, "the car will not be ready"
        assert snapshot.expected_soc_percent is not None and snapshot.expected_soc_percent < 80
        events = hass.states.get(entity_id(hass, charger.entry_id, "charger_events", platform="event"))
        assert events is not None and events.attributes.get("event_type") == "plan_at_risk"
