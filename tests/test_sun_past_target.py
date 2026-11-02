"""The sun fills past a reached target, up to the car's own limit (Solar and Hybrid).

Hybrid's grid part stops at the target; once its need is met, surplus may keep charging the car up to the limit the
car states (`target_stop.sun_fill_limit_percent`), by the solar rules only. No known limit, no level or a manual
amount: no extension. Built on `tests/test_hybrid_wiring.py`'s satisfied `target_soc` case.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator
from custom_components.spotnav.execution.target_stop import SocReading, sun_fill_limit_percent
from custom_components.spotnav.planning.auto_controller import LiveVehicleFacts
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_TARGET_SOC,
    STRATEGY_CHEAPEST,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
    TargetSocIntent,
)
from custom_components.spotnav.runtime import charger_data, preview_for
from custom_components.spotnav.sessions.inputs import solar_share_of

from .world import controller_of, set_charger_delivered_a, set_derived_site_entities, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay", "chargers_not_switch_only")


def _reading(percent: float, *, estimated: bool = False) -> SocReading:
    return SocReading(soc_percent=percent, source="vehicle", entity_id=None, vehicle_id="car-1", age_s=0.0, estimated=estimated)


# ------------------------------------------------------------------ the rule


def test_the_sun_fills_from_the_target_up_to_the_cars_own_limit() -> None:
    assert sun_fill_limit_percent(target_soc_percent=80, reading=_reading(80), vehicle_max_percent=90) == 90
    assert sun_fill_limit_percent(target_soc_percent=80, reading=_reading(85.5), vehicle_max_percent=90.7) == 90


def test_no_fill_short_of_the_target_or_at_the_limit() -> None:
    assert sun_fill_limit_percent(target_soc_percent=80, reading=_reading(79.9), vehicle_max_percent=90) is None
    assert sun_fill_limit_percent(target_soc_percent=80, reading=_reading(90), vehicle_max_percent=90) is None
    # A target at or above the car's limit is already a charge the car ends itself.
    assert sun_fill_limit_percent(target_soc_percent=90, reading=_reading(90), vehicle_max_percent=90) is None


def test_no_fill_without_a_known_limit_a_target_or_a_level() -> None:
    assert sun_fill_limit_percent(target_soc_percent=80, reading=_reading(80), vehicle_max_percent=None) is None
    assert sun_fill_limit_percent(target_soc_percent=None, reading=_reading(80), vehicle_max_percent=90) is None
    assert sun_fill_limit_percent(target_soc_percent=80, reading=None, vehicle_max_percent=90) is None
    unusable = replace(_reading(80), soc_percent=None)
    assert sun_fill_limit_percent(target_soc_percent=80, reading=unusable, vehicle_max_percent=90) is None


def test_an_estimate_at_the_limit_leaves_the_end_to_the_car() -> None:
    assert sun_fill_limit_percent(target_soc_percent=80, reading=_reading(91, estimated=True), vehicle_max_percent=90) == 90


# ------------------------------------------------------------------ in the coordinator


class _Car:
    """The car's level and its own limit, as the coordinator reads them."""

    def __init__(self, level: float, limit: float | None) -> None:
        self.level = level
        self.limit = limit

    def reading(self, _vehicle_id: str | None) -> SocReading:
        return _reading(self.level)


async def _target_met(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, *, strategy: str, limit: float | None
) -> tuple[object, SolarExecutionCoordinator, object, list, list, _Car]:
    """A charger whose `target_soc` of 80 % is met (the car reads 80 %), the car stating `limit` as its own."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await solar_setup(
        hass, strategy=strategy
    )
    car = _Car(80.0, limit)
    controller._vehicle_limit_reader = lambda _vehicle_id: car.limit  # noqa: SLF001
    data = charger_data(hass, charger.entry_id)
    assert data is not None
    monkeypatch.setattr(data.soc_reader, "peek", car.reading)
    preview = preview_for(hass, charger.entry_id)
    assert preview is not None
    preview._vehicle_reader = lambda _vehicle_id: LiveVehicleFacts(  # noqa: SLF001
        vehicle_id="car-1", soc_percent=car.level, reported_capacity_kwh=60.0, max_percent=car.limit
    )
    snapshot = await preview.async_apply_settings(
        mutate=lambda s: replace(
            s, strategy=strategy, area_id="SE4", amps=10, phases=3, departure_enabled=False,
            driver=DRIVER_TARGET_SOC, target=TargetSocIntent(vehicle_id="car-1", target_percent=80.0),
        )
    )
    if strategy == STRATEGY_HYBRID:
        assert snapshot.state == "nothing_to_charge" and snapshot.reason == "already_at_target"
    return controller_of(hass, site_entry.entry_id), coordinator, clock, turn_on_calls, turn_off_calls, car


async def _surplus_ticks(hass: HomeAssistant, site_controller: object, clock: object, *, watts: float = -1400.0) -> None:
    set_derived_site_entities(hass, "solar_site", watts)
    set_charger_delivered_a(hass, "solar_charger", 0.0)
    await tick_site(hass, site_controller)
    clock.value = 125.0  # type: ignore[attr-defined]
    await tick_site(hass, site_controller)


async def test_hybrid_fills_on_surplus_past_a_met_target_up_to_the_cars_limit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, coordinator, clock, turn_on_calls, turn_off_calls, car = await _target_met(
        hass, monkeypatch, strategy=STRATEGY_HYBRID, limit=90.0
    )
    await _surplus_ticks(hass, site, clock)

    assert len(turn_on_calls) == 1, "the sun fills on past the met target"
    state = coordinator.state
    assert state is not None and state.state == "on" and not state.satisfied
    assert state.sun_fill_limit_percent == 90.0
    entry_id = next(iter(site.solar_surplus_snapshot))
    assert site.solar_surplus_snapshot[entry_id]["sun_fill_limit_percent"] == 90.0
    # It takes its place in the site's surplus split, and load balancing's, as any charge of the sun's.
    assert coordinator.share_member(site, order=0) is not None

    # The car reaches its own limit: the sun stands down again.
    hass.states.async_set("switch.solar_charger", "on")
    car.level = 90.0
    await tick_site(hass, site)
    assert len(turn_off_calls) == 1, "the fill ends at the car's limit"
    assert coordinator.state is not None and coordinator.state.satisfied is True


async def test_hybrid_without_surplus_stays_stopped_past_a_met_target(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, coordinator, clock, turn_on_calls, _off, _car = await _target_met(
        hass, monkeypatch, strategy=STRATEGY_HYBRID, limit=90.0
    )
    await _surplus_ticks(hass, site, clock, watts=300.0)

    assert turn_on_calls == [], "never from the grid: no surplus, no fill"
    assert coordinator.state is not None and coordinator.state.state == "off"


async def test_hybrid_without_a_known_limit_stops_at_the_target_as_before(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, coordinator, clock, turn_on_calls, _off, _car = await _target_met(
        hass, monkeypatch, strategy=STRATEGY_HYBRID, limit=None
    )
    await _surplus_ticks(hass, site, clock)

    assert turn_on_calls == []
    assert coordinator.state is not None and coordinator.state.satisfied is True
    assert coordinator.state.sun_fill_limit_percent is None
    assert coordinator.share_member(site, order=0) is None


async def test_solar_fills_past_a_reached_target_and_says_so(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, coordinator, clock, turn_on_calls, _off, _car = await _target_met(
        hass, monkeypatch, strategy=STRATEGY_SOLAR, limit=90.0
    )
    await _surplus_ticks(hass, site, clock)

    assert len(turn_on_calls) == 1
    assert coordinator.state is not None and coordinator.state.sun_fill_limit_percent == 90.0


async def test_solar_without_a_known_limit_charges_as_before_and_says_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, coordinator, clock, turn_on_calls, _off, _car = await _target_met(
        hass, monkeypatch, strategy=STRATEGY_SOLAR, limit=None
    )
    await _surplus_ticks(hass, site, clock)

    assert len(turn_on_calls) == 1
    assert coordinator.state is not None and coordinator.state.sun_fill_limit_percent is None


async def test_the_fill_counts_as_solar_in_the_sessions_split(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, coordinator, clock, _on, _off, _car = await _target_met(
        hass, monkeypatch, strategy=STRATEGY_HYBRID, limit=90.0
    )
    await _surplus_ticks(hass, site, clock)
    hass.states.async_set("switch.solar_charger", "on")
    set_charger_delivered_a(hass, "solar_charger", 6.0)
    await tick_site(hass, site)

    assert coordinator.state is not None and coordinator.state.state == "on"
    share = solar_share_of(hass, "solar_charger", STRATEGY_HYBRID)
    assert share is not None and share > 0


async def test_cheapest_never_fills_past_the_target(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    _site, coordinator, _clock, _on, _off, _car = await _target_met(
        hass, monkeypatch, strategy=STRATEGY_HYBRID, limit=90.0
    )
    store = coordinator._store  # noqa: SLF001
    await store.async_update("solar_charger", mutate=lambda s: replace(s, strategy=STRATEGY_CHEAPEST))
    assert coordinator._sun_fill_limit() is None  # noqa: SLF001


# ------------------------------------------------------------------ an app released before the line


def _body(*lines: dict) -> dict:
    return {"ok": True, "status": {"tone": "normal", "lines": list(lines)}}


FILL = {"code": "sun_past_target", "params": {"percent": 90}}


def test_an_older_app_gets_hybrids_need_met_line_in_its_place() -> None:
    from custom_components.spotnav.api.webhook import _for_app

    older = _for_app(_body(FILL), {"action": "dashboard"})
    assert older["status"]["lines"] == [{"code": "hybrid_satisfied", "params": {}}]


def test_an_older_app_gets_only_the_solar_headline() -> None:
    from custom_components.spotnav.api.webhook import _for_app

    headline = {"code": "solar_charging", "params": {"requested_a": 8.0}}
    older = _for_app(_body(headline, FILL), {"action": "dashboard"})
    assert older["status"]["lines"] == [headline]


def test_an_app_that_reads_the_line_gets_it() -> None:
    from custom_components.spotnav.api.webhook import _for_app

    body = _body(FILL)
    assert _for_app(body, {"action": "dashboard", "reads": ["sun_past_target"]}) == body
