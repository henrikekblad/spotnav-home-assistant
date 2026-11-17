"""The cars' energy from SpotNav's own charging sessions, where a charger's register keeps no statistics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.spotnav.api import house as house_api
from custom_components.spotnav.const import CONF_CHARGER_ENTRY_IDS
from custom_components.spotnav.sessions.costing import Slice
from custom_components.spotnav.sessions.model import SOURCE_REGISTER, ChargeSession
from custom_components.spotnav.site.session_car import car_energy_from_sessions

DAY = datetime(2026, 11, 10, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _session(start: datetime, end: datetime | None, kwh: float, intervals: tuple[Slice, ...] = ()) -> ChargeSession:
    return ChargeSession(
        id=f"c-{int(start.timestamp())}", charger_id="c", start=start, end=end, energy_kwh=kwh,
        energy_source=SOURCE_REGISTER, priced_kwh=0.0, cost_minor=None, reference_cost_minor=None, area_id=None,
        currency=None, major_unit=None, minor_unit=None, started_by="other", strategy=None, vehicle_id=None,
        vehicle_name=None, solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None,
        intervals=intervals,
    )


def _hours(count: int) -> list[datetime]:
    return [DAY + index * HOUR for index in range(count)]


def test_energy_is_spread_evenly_over_the_hours_a_session_spans() -> None:
    session = _session(DAY + timedelta(minutes=30), DAY + timedelta(hours=2, minutes=30), 8.0)
    out = car_energy_from_sessions([session], _hours(4), HOUR)
    assert out == {
        DAY: pytest.approx(2.0),
        DAY + HOUR: pytest.approx(4.0),
        DAY + 2 * HOUR: pytest.approx(2.0),
        DAY + 3 * HOUR: 0.0,
    }


def test_price_intervals_say_when_the_energy_was_drawn() -> None:
    slices = (
        Slice(DAY, DAY + HOUR, 6.0, 0.1, 1.0, None, 0.1),
        Slice(DAY + 3 * HOUR, DAY + 4 * HOUR, 3.0, 0.1, 1.0, None, 0.1),
    )
    session = _session(DAY, DAY + 4 * HOUR, 9.0, slices)
    out = car_energy_from_sessions([session], _hours(4), HOUR)
    assert [round(out[start], 3) for start in _hours(4)] == [6.0, 0.0, 0.0, 3.0]


def test_hours_before_the_first_session_are_unknown_and_none_means_no_figures() -> None:
    session = _session(DAY + 2 * HOUR, DAY + 3 * HOUR, 5.0)
    out = car_energy_from_sessions([session], _hours(4), HOUR)
    assert list(out) == [DAY + 2 * HOUR, DAY + 3 * HOUR]
    assert car_energy_from_sessions([], _hours(4), HOUR) == {}


def test_a_quarter_hour_period_takes_its_share() -> None:
    session = _session(DAY, DAY + HOUR, 4.0)
    quarters = [DAY + index * timedelta(minutes=15) for index in range(4)]
    assert car_energy_from_sessions([session], quarters, timedelta(minutes=15)) == {q: pytest.approx(1.0) for q in quarters}


def test_the_sessions_fill_only_the_hours_the_register_and_plan_have_none_for(hass, monkeypatch) -> None:
    session = _session(DAY, DAY + 2 * HOUR, 6.0)
    store = SimpleNamespace(
        closed_raw=lambda charger_id: (session,) if charger_id == "c1" else (), open_raw=lambda _id: None
    )
    monkeypatch.setattr(house_api, "domain_data", lambda _hass: SimpleNamespace(session_store=store))
    controller = SimpleNamespace(
        battery_shadow=SimpleNamespace(facts=SimpleNamespace(battery_entity_id=None)),
        config={CONF_CHARGER_ENTRY_IDS: ["c1"]},
    )
    rows = {"sensor.reg": [{"start": DAY, "change": 1.5}]}
    car = house_api._car_hours(hass, controller, rows, ("sensor.reg",), _hours(3))
    assert car == {DAY: 1.5, DAY + HOUR: pytest.approx(3.0), DAY + 2 * HOUR: 0.0}
    # Without starts the register alone answers, as before.
    assert house_api._car_hours(hass, controller, rows, ("sensor.reg",)) == {DAY: 1.5}
