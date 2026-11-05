"""The costed sessions are made once per change, and say exactly what costing them on every read said.

`SessionStore.closed`, `month_summary` and `sessions_summary` cache what they cost. These tests hold the cached
answers against the path every reader took before (each session `priced` with the fiscal choice of its market,
then `summary.month_summary` / `sessions_summary` over all of them) on a long synthetic history, and pin when the
cache is made again: a finished session, a changed fee, never an open session's minute or a controller notify.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.planner import FiscalChoice, HighLoadWindow
from custom_components.spotnav.sessions import model as model_module
from custom_components.spotnav.sessions.costing import Slice
from custom_components.spotnav.sessions.model import ChargeSession, SOURCE_IMPORTED_HOURLY
from custom_components.spotnav.sessions.store import SessionStore
from custom_components.spotnav.sessions.summary import (
    available_months,
    month_key,
    month_summary,
    previous_month,
    sessions_csv,
    sessions_summary,
)


UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")
CHARGER = "entry_a"
END = datetime(2026, 11, 4, 22, tzinfo=UTC)

PLAIN = FiscalChoice(
    tax_enabled=True, tax_minor_per_kwh=42.8, transfer_enabled=True, transfer_minor_per_kwh=30.0,
    vat_enabled=True, vat_percent=25.0,
)
WINDOW = replace(
    PLAIN,
    transfer_high_enabled=True,
    transfer_high_minor_per_kwh=80.0,
    high_load_window=HighLoadWindow(tz="Europe/Stockholm"),
)


def history(days: int = 730) -> list[ChargeSession]:
    """One charge a day for `days` days: most priced in quarter hours in two markets, some imported, some with only
    a stored cost, some with no price at all, a few in another currency."""
    out = []
    first = END - timedelta(days=days)
    for index in range(days):
        start = first + timedelta(days=index, minutes=7 * (index % 9))
        slices = tuple(
            Slice(
                start + timedelta(minutes=15 * k),
                start + timedelta(minutes=15 * (k + 1)),
                0.25 + 0.01 * (k % 5),
                -0.01 + 0.003 * ((index + k) % 40),
                11.2,
                date(2026, 1, 1),
                0.07,
            )
            for k in range(32)
        )
        kind = index % 10
        stored_cost = kind == 7
        out.append(
            ChargeSession(
                id=f"{CHARGER}-{index}",
                charger_id=CHARGER,
                start=start,
                end=start + timedelta(hours=8),
                energy_kwh=9.6 + 0.1 * kind,
                energy_source="estimated" if kind == 3 else "register",
                priced_kwh=8.0 if stored_cost else 0.0,
                cost_minor=900.0 if stored_cost else None,
                reference_cost_minor=950.0 if stored_cost else None,
                currency="EUR" if kind == 9 else "SEK",
                major_unit="kr",
                minor_unit="öre",
                started_by="unknown" if kind == 5 else "manual",
                strategy="cheapest",
                vehicle_id=None,
                vehicle_name="Car" if kind % 2 else None,
                solar_kwh=1.0 if kind == 4 else 0.0,
                solar_known_kwh=4.0 if kind == 4 else 0.0,
                last_register_kwh=None,
                last_sample_at=None,
                source=SOURCE_IMPORTED_HOURLY if kind == 5 else None,
                intervals=() if stored_cost or kind == 8 else slices,
                area_id=None if stored_cost or kind == 8 else ("SE4" if kind == 6 else "SE3"),
            )
        )
    return out


def costed_as_before(store: SessionStore, fiscal_of) -> tuple[ChargeSession, ...]:
    """Every session priced on its own read, the way `closed` did it before the cache."""
    return tuple(
        item if not item.intervals else item.priced(fiscal_of(item.charger_id, item.area_id))
        for item in store.closed_raw(CHARGER)
    )


@pytest.fixture
async def store(hass: HomeAssistant):
    holder = {"fiscal": {"SE3": WINDOW, "SE4": PLAIN}}
    sessions = SessionStore(hass)

    def resolve(charger_id: str, area_id: str | None) -> FiscalChoice | None:
        return holder["fiscal"].get(area_id)

    sessions.set_fiscal_resolver(resolve)
    await sessions.async_load()
    sessions.replace_closed(CHARGER, history(), END)
    sessions.resolve = resolve  # type: ignore[attr-defined]
    sessions.holder = holder  # type: ignore[attr-defined]
    return sessions


def assert_same_as_before(store: SessionStore) -> None:
    before = costed_as_before(store, store.resolve)  # type: ignore[attr-defined]
    after = store.closed(CHARGER)
    assert after == before
    assert [item.public(STOCKHOLM) for item in after] == [item.public(STOCKHOLM) for item in before]
    months = available_months(before, STOCKHOLM)
    assert len(months) >= 24
    for key in months:
        assert store.month_summary(CHARGER, STOCKHOLM, key) == month_summary(before, STOCKHOLM, key)
    assert store.month_summary(CHARGER, STOCKHOLM, "2020-01") == month_summary(before, STOCKHOLM, "2020-01")
    for moment in (END, END - timedelta(days=40), END + timedelta(days=31)):
        assert store.sessions_summary(CHARGER, STOCKHOLM, moment) == sessions_summary(before, STOCKHOLM, moment)
    assert sessions_csv(after, STOCKHOLM, first=None, last=None) == sessions_csv(
        before, STOCKHOLM, first=None, last=None
    )


async def test_the_cached_figures_are_the_ones_costing_on_every_read_made(store: SessionStore) -> None:
    assert_same_as_before(store)


async def test_a_changed_fee_costs_the_history_again(store: SessionStore) -> None:
    first = store.month_summary(CHARGER, STOCKHOLM, month_key(END.date()))
    store.holder["fiscal"] = {  # type: ignore[attr-defined]
        "SE3": replace(WINDOW, tax_minor_per_kwh=10.0),
        "SE4": replace(PLAIN, vat_percent=0.0),
    }

    assert store.month_summary(CHARGER, STOCKHOLM, month_key(END.date())) != first
    assert_same_as_before(store)

    store.holder["fiscal"] = {}  # type: ignore[attr-defined]
    assert_same_as_before(store)


async def test_a_finished_session_costs_the_history_again_and_an_open_ones_minute_does_not(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    real = model_module.price_slices

    def counted(slices, fiscal):
        calls.append(1)
        return real(slices, fiscal)

    monkeypatch.setattr(model_module, "price_slices", counted)
    store.closed(CHARGER)
    priced = sum(1 for item in store.closed_raw(CHARGER) if item.intervals)
    assert len(calls) == priced

    # Many readers, an open session's minutes, an import marker: nothing is costed again.
    calls.clear()
    template = store.closed_raw(CHARGER)[-1]
    running = replace(template, id="open", start=END + timedelta(hours=1), end=None)
    for _ in range(5):
        store.set_open(running, charger_id=CHARGER)
        store.closed(CHARGER)
        store.sessions_summary(CHARGER, STOCKHOLM, END)
        store.month_summary(CHARGER, STOCKHOLM, previous_month(END.date()))
    store.set_import_marker(CHARGER, {"version": 2})
    store.closed(CHARGER)
    assert calls == []

    # The session ends: the history is costed once more, then read from the cache again.
    store.close(replace(running, end=END + timedelta(hours=3)), END + timedelta(hours=3))
    store.closed(CHARGER)
    store.closed(CHARGER)
    assert len(calls) == priced + 1
    assert_same_as_before(store)


async def test_a_month_summary_handed_out_is_a_copy(store: SessionStore) -> None:
    key = month_key(END.date())
    summary = store.month_summary(CHARGER, STOCKHOLM, key)
    summary["sessions"] = -1

    assert store.month_summary(CHARGER, STOCKHOLM, key)["sessions"] != -1


async def test_a_removed_charger_drops_its_costed_sessions(store: SessionStore) -> None:
    assert store.closed(CHARGER)

    await store.async_remove_charger(CHARGER)

    assert store.closed(CHARGER) == ()
    assert store.month_summary(CHARGER, STOCKHOLM, month_key(END.date()))["sessions"] == 0
