"""A session stores energy and raw spot prices; a cost is made when it is read, with the settings then."""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.sessions import sessions_answer
from custom_components.spotnav.planning.planner import FiscalChoice
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.sessions.costing import split_energy
from custom_components.spotnav.sessions.model import ChargeSession
from custom_components.spotnav.sessions.store import SessionStore
from custom_components.spotnav.sessions.summary import sessions_summary

from .sessions_helpers import day_intervals, session, STOCKHOLM, UTC

CHARGER = "entry_a"
DAY = date(2026, 9, 22)
MIDNIGHT = datetime(2026, 9, 22, tzinfo=STOCKHOLM).astimezone(UTC)
NOW = MIDNIGHT + timedelta(hours=20)


def intervals_session(hour: int, kwh: float) -> ChargeSession:
    """A session with its energy split over a flat 100 (minor unit per kWh) day, and no stored cost."""
    start = MIDNIGHT + timedelta(hours=hour)
    end = start + timedelta(hours=1)
    base = session(start, energy=kwh, cost_minor=None, currency="SEK", charger_id=CHARGER)
    return replace(
        base, intervals=tuple(split_energy(day_intervals(DAY, [100.0] * 24), start, end, kwh)), area_id="SE3"
    )


@pytest.fixture
async def store(hass: HomeAssistant):
    """A store whose current settings the test can change."""
    holder = {"fiscal": FiscalChoice()}
    sessions = SessionStore(hass)
    sessions.set_fiscal_resolver(lambda charger_id, area_id: holder["fiscal"])
    await sessions.async_load()
    sessions.holder = holder  # type: ignore[attr-defined]
    return sessions


def test_what_is_stored_is_energy_and_raw_prices_never_a_cost() -> None:
    record = intervals_session(3, 5.0).as_dict()

    assert "cost_minor" not in record and "reference_cost_minor" not in record and "priced_kwh" not in record
    assert record["intervals"] == [[int(MIDNIGHT.timestamp()) + 3 * 3600, int(MIDNIGHT.timestamp()) + 4 * 3600,
                                    5.0, 1.0, 1.0, 1.0, None]]
    assert record["area_id"] == "SE3"
    again = ChargeSession.from_dict(record)
    assert again is not None and again.intervals == intervals_session(3, 5.0).intervals


async def test_changing_the_vat_changes_every_historic_cost(store: SessionStore) -> None:
    for hour in (2, 5, 9):
        store.close(intervals_session(hour, 10.0), NOW)

    before = sessions_summary(store.closed(CHARGER), STOCKHOLM, NOW)["this_month"]
    store.holder["fiscal"] = FiscalChoice(vat_enabled=True, vat_percent=25.0)  # type: ignore[attr-defined]
    after = sessions_summary(store.closed(CHARGER), STOCKHOLM, NOW)["this_month"]

    assert before["cost"] == pytest.approx(30.0) and after["cost"] == pytest.approx(37.5)
    assert after["average_price_minor_per_kwh"] == pytest.approx(125.0)
    assert [item.cost_minor for item in store.closed(CHARGER)] == pytest.approx([1250.0] * 3)


async def test_without_resolvable_settings_a_cost_is_absent_not_guessed(store: SessionStore) -> None:
    store.close(intervals_session(2, 10.0), NOW)
    store.set_fiscal_resolver(lambda charger_id, area_id: None)

    (item,) = store.closed(CHARGER)

    assert item.cost_minor is None and item.energy_kwh == 10.0 and item.average_price_minor_per_kwh is None


async def test_the_csv_and_the_answer_use_the_current_settings(hass: HomeAssistant, store: SessionStore) -> None:
    domain_data(hass).session_store = store
    store.close(intervals_session(2, 10.0), NOW)

    def cost_cell() -> float:
        text = sessions_answer(hass, CHARGER, {"format": "csv"})["csv"]
        (row,) = list(csv.DictReader(io.StringIO(text)))
        return float(row["cost"])

    assert cost_cell() == pytest.approx(10.0)
    store.holder["fiscal"] = FiscalChoice(transfer_enabled=True, transfer_minor_per_kwh=30.0)  # type: ignore[attr-defined]
    assert cost_cell() == pytest.approx(13.0)
    answer = sessions_answer(hass, CHARGER, {})
    assert answer["cost_basis"] == "current_settings"
    assert answer["sessions"][0]["cost_basis"] == "current_settings"
    assert answer["sessions"][0]["cost"] == pytest.approx(13.0)


def legacy_record() -> dict:
    record = session(MIDNIGHT + timedelta(hours=3), energy=10.0, cost_minor=480.0, reference_minor=520.0).as_dict()
    # What a record looked like before intervals existed.
    for key in ("legacy_priced_kwh", "legacy_cost_minor", "legacy_reference_cost_minor", "intervals"):
        record.pop(key, None)
    record.update(priced_kwh=10.0, cost_minor=480.0, reference_cost_minor=520.0)
    return record


async def test_an_old_record_keeps_its_cost_and_says_it_is_stored(hass: HomeAssistant, store: SessionStore) -> None:
    old = ChargeSession.from_dict(legacy_record())

    assert old is not None and old.cost_minor == 480.0 and old.reference_cost_minor == 520.0
    assert old.cost_basis == "stored" and old.public()["cost_basis"] == "stored"
    written = old.as_dict()
    assert written["legacy_cost_minor"] == 480.0 and "cost_minor" not in written
    again = ChargeSession.from_dict(written)
    assert again is not None and again.cost_minor == 480.0 and again.priced_kwh == 10.0

    store.close(old, NOW)
    store.holder["fiscal"] = FiscalChoice(vat_enabled=True, vat_percent=25.0)  # type: ignore[attr-defined]
    (held,) = store.closed(CHARGER)
    assert held.cost_minor == 480.0, "a stored cost is not a basis for the new settings"


async def test_the_stored_document_round_trips_old_and_new_records(hass: HomeAssistant, store: SessionStore) -> None:
    store.close(ChargeSession.from_dict(legacy_record()), NOW)  # type: ignore[arg-type]
    store.close(intervals_session(8, 4.0), NOW)
    await store.async_flush()

    reloaded = SessionStore(hass)
    await reloaded.async_load()

    old, new = reloaded.closed_raw(CHARGER)
    assert old.cost_basis == "stored" and old.cost_minor == 480.0
    assert new.cost_basis == "current_settings" and len(new.intervals) == 1
