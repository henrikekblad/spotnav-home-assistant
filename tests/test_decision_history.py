"""The decision history (plan `plans/v3_decision_history.md`): the ledger (`site/decision_ledger.py`) and the Plan
tab's past days (`site/plan_history.py`), pure; and the card's fixtures (`tests/fixtures/plan/`,
`SPOTNAV_WRITE_FIXTURES=1` rewrites them):

* `past_day.json`: the villa's 17 November (`test_plan_view.villa_inputs`) seen the next morning, every hour's plan
  read at its start and what ran: the car taking 2.1 of its planned 3.7 kW at 01 (the car limited), Home Assistant
  down 02-05 (missing), 05 only partly recorded, the heat and a floor zone only watched (would), the rest as planned.
* `today_mixed.json`: the same day at 14:30: its hours gone by from the history, the running hour on from the plan.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.spotnav.site.decision_ledger import (
    HINT_CAR_LIMITED,
    KEPT_DAYS,
    REASON_CEILING,
    REASON_IMPORT_TARGET,
    Ledger,
    LoadSample,
    OpenHour,
    Sample,
    close,
    hour_number,
    snapshot,
)
from custom_components.spotnav.site.plan_history import day_hours, past_view
from custom_components.spotnav.site.plan_view import HOUR_KEYS, LOAD_KEYS, PLAN_KEYS, RUN_KEYS, plan_view

from .test_plan_view import at, villa_inputs

FIXTURES = Path(__file__).parent / "fixtures" / "plan"
TZ = "Europe/Stockholm"
ZONE = ZoneInfo(TZ)
DAY = date(2026, 11, 17)


def _write_or_compare(name: str, answer: dict[str, Any]) -> None:
    path = FIXTURES / name
    text = json.dumps(answer, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.write_text(text, encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text)


def _plan_at(moment: datetime) -> dict[str, Any]:
    return plan_view(replace(villa_inputs(), now=moment))


def _samples_as_planned(opened: OpenHour, count: int = 60, **change: dict[str, Any]) -> None:
    """`count` samples that ran as the plan at the start said, a load's fields changed by `change[load_id]`."""
    for _ in range(count):
        sample = Sample(grid_kw=None, house_kw=None)
        for load_id, load in opened.loads.items():
            what = load.planned.what
            if load.kind == "home_battery" and what is not None:
                what = "charge" if (load.planned.kw or 0) > 0 else "cover"
            seen = {"kind": load.kind, "name": load.name, "kw": load.planned.kw, "what": None if load.shadow else what, "shadow": load.shadow}
            seen.update(change.get(load_id, {}))
            sample.loads[load_id] = LoadSample(**seen)
        site = opened.site
        sample.grid_kw = None if site is None else site.net_kw
        sample.house_kw = None if site is None or site.net_kw is None else site.net_kw - 0.2
        opened.add(sample)


def _record(ledger: Ledger, hour: int, change: dict[str, dict[str, Any]] | None = None, count: int = 60) -> None:
    opened = OpenHour(hour_number(at(hour)))
    site, loads = snapshot(_plan_at(at(hour) + timedelta(seconds=40)), at(hour))  # type: ignore[misc]
    assert opened.plan(at(hour) + timedelta(seconds=40), site, loads)
    _samples_as_planned(opened, count, **(change or {}))
    ledger.keep(*close(opened))


def villa_ledger() -> Ledger:
    """The villa's hours from 16 November 20:00 to 17 November 23:00 (see the module)."""
    ledger = Ledger()
    for hour in range(20, 48):
        if hour in (26, 27, 28):
            continue  # Home Assistant was down 02-05.
        change: dict[str, dict[str, Any]] = {}
        if hour == 25:
            change["charger_garage"] = {"kw": 2.1, "hint": HINT_CAR_LIMITED}
        if hour == 23:
            change["switch.varmvattenberedare"] = {"kw": 0.0, "what": "paused", "reason": REASON_CEILING}
        _record(ledger, hour, change, count=40 if hour == 29 else 60)
    return ledger


def _load(answer: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(load for load in answer["loads"] if load["kind"] == kind)


# ------------------------------------------------------------------------------------------------ the ledger


def test_a_recorded_hour_is_the_plan_at_its_start_even_when_the_plan_changes_later() -> None:
    opened = OpenHour(hour_number(at(25)))
    first = snapshot(_plan_at(at(25) + timedelta(seconds=30)), at(25))
    assert first is not None
    assert opened.plan(at(25) + timedelta(seconds=30), *first)
    # Later in the hour the plan has the car at another power: never taken.
    changed = replace(villa_inputs(), now=at(25) + timedelta(minutes=30))
    from custom_components.spotnav.site.plan_view import CarPlan  # noqa: PLC0415

    changed = replace(changed, cars=(CarPlan("charger_garage", "Garage", ((at(25), at(29), 1.4),), live=True),))
    later = snapshot(plan_view(changed), at(25))
    assert later is not None and later[1]["charger_garage"].planned.kw == 1.4
    assert not opened.plan(at(25) + timedelta(minutes=30), *later)
    _samples_as_planned(opened)
    hour_row, rows = close(opened)
    car = next(row for row in rows if row.load_id == "charger_garage")
    assert car.planned is not None and (car.planned.what, car.planned.kw) == ("charge", 3.7)
    assert (car.ran_what, car.ran_kw) == ("charge", 3.7)
    assert hour_row.planned_at_min == 0 and hour_row.recorded == "full"


def test_a_restart_mid_hour_keeps_the_plan_read_at_its_start_and_the_samples() -> None:
    ledger = Ledger()
    opened = ledger.open = OpenHour(hour_number(at(25)))
    site, loads = snapshot(_plan_at(at(25) + timedelta(seconds=20)), at(25))  # type: ignore[misc]
    opened.plan(at(25) + timedelta(seconds=20), site, loads)
    _samples_as_planned(opened, 20, charger_garage={"kw": 3.0})
    stored = json.loads(json.dumps(ledger.as_store()))
    restored = Ledger.from_store(stored)
    assert restored.open is not None and restored.open.planned_at == at(25) + timedelta(seconds=20)
    # After the restart the plan says something else: the one read at the start stays.
    assert not restored.open.plan(at(25) + timedelta(minutes=25), site, {})
    _samples_as_planned(restored.open, 25, charger_garage={"kw": 1.0})
    hour_row, rows = close(restored.open)
    car = next(row for row in rows if row.load_id == "charger_garage")
    assert car.planned is not None and car.planned.kw == 3.7
    assert car.ran_kw == round((20 * 3.0 + 25 * 1.0) / 45, 2)
    # 45 of 60 minutes: partial.
    assert hour_row.samples == 45 and hour_row.recorded == "partial"


def test_a_plan_read_late_says_so() -> None:
    opened = OpenHour(hour_number(at(25)))
    site, loads = snapshot(_plan_at(at(25) + timedelta(minutes=23)), at(25))  # type: ignore[misc]
    opened.plan(at(25) + timedelta(minutes=23, seconds=10), site, loads)
    hour_row, _rows = close(opened)
    assert hour_row.planned_at_min == 23


def test_what_decided_wins_over_the_plan_and_a_hint_explains_a_difference() -> None:
    ledger = Ledger()
    _record(ledger, 25, {"charger_garage": {"kw": 1.5, "reason": REASON_IMPORT_TARGET}})
    _record(ledger, 26, {"charger_garage": {"kw": 2.0, "hint": HINT_CAR_LIMITED}})
    _record(ledger, 27, {"charger_garage": {"hint": HINT_CAR_LIMITED}})
    car = {row.hour: row for rows in ledger.loads.values() for row in rows if row.load_id == "charger_garage"}
    assert car[hour_number(at(25))].reason == REASON_IMPORT_TARGET
    assert car[hour_number(at(26))].reason == HINT_CAR_LIMITED
    # As planned: the plan's own reason (none for the car), never the hint.
    assert car[hour_number(at(27))].reason is None
    heater = next(row for row in ledger.loads[hour_number(at(25))] if row.kind == "water_heater")
    assert heater.planned is not None and heater.reason == heater.planned.reason


def test_seven_days_are_kept_and_today() -> None:
    ledger = Ledger()
    today = date(2026, 11, 24)
    for day_back in range(10, -1, -1):
        moment = datetime(2026, 11, 24 - day_back, 12, tzinfo=ZONE)
        opened = OpenHour(hour_number(moment))
        ledger.keep(*close(opened))
    assert ledger.trim(today, ZONE)
    kept = sorted(datetime.fromtimestamp(hour * 3600, tz=ZONE).date() for hour in ledger.hours)
    assert kept[0] == today - timedelta(days=KEPT_DAYS) and kept[-1] == today and len(kept) == KEPT_DAYS + 1
    assert not ledger.trim(today, ZONE)


def test_the_store_reads_back_and_refuses_what_does_not_read() -> None:
    ledger = villa_ledger()
    stored = json.loads(json.dumps(ledger.as_store()))
    again = Ledger.from_store(stored)
    assert again.hours == ledger.hours and again.loads == ledger.loads
    assert Ledger.from_store({"schema": 99}).hours == {}
    stored["rows"].append(["x"])
    stored["hours"].append([1, "y"])
    assert Ledger.from_store(stored).hours == ledger.hours
    # The size budget: a villa with five loads, 25 hours, under 40 kB.
    assert len(json.dumps(stored)) < 40_000


# ------------------------------------------------------------------------------------------------ the past days


def test_a_past_day() -> None:
    ledger = villa_ledger()
    hours = day_hours(DAY, TZ)
    answer = past_view(
        site_id="site_villa",
        tz=TZ,
        now=at(48 + 10),
        day=DAY,
        rows=ledger.between(hours[0], hours[-1] + timedelta(hours=1)),
        history_first=at(20),
        money={"currency": "SEK", "major_unit": "kr", "minor_unit": "öre"},
    )
    _write_or_compare("past_day.json", answer)
    assert tuple(answer) == PLAN_KEYS and answer["past"] is True and answer["date"] == "2026-11-17"
    assert answer["history"] == {"days": 7, "first": at(20).isoformat()}
    assert all(tuple(row) == HOUR_KEYS for row in answer["hours"])
    assert all(tuple(load) == LOAD_KEYS for load in answer["loads"])
    assert all(tuple(run) == RUN_KEYS for load in answer["loads"] for run in load["runs"])
    assert len(answer["hours"]) == 24 and all(row["past"] for row in answer["hours"])
    # Home Assistant down 02-05: missing, every figure null, never invented.
    missing = [row for row in answer["hours"] if row["recorded"] == "missing"]
    assert [row["start"] for row in missing] == [at(h).isoformat() for h in (26, 27, 28)]
    assert all(row["net_kw"] is None and row["price"] is None for row in missing)
    assert answer["hours"][5]["recorded"] == "partial"
    car = _load(answer, "car")
    assert car["state"] == "live"
    assert car["planned"][1] == {"what": "charge", "kw": 3.7, "reason": None}
    assert car["ran"][1] == {"what": "charge", "kw": 2.1} and car["reason"][1] == "car_limited"
    assert car["planned"][2] is None and car["ran"][2] is None
    # The heat only watched says what SpotNav would have done, as live.
    heat = _load(answer, "heat")
    assert heat["state"] == "would" and answer["would"] is True
    assert [run["what"] for run in heat["runs"]] == ["preheat", "lower", "lower"]
    assert heat["level"][7] == -1


def test_today_is_history_then_plan() -> None:
    ledger = villa_ledger()
    now = at(24 + 14.5)
    hours = day_hours(DAY, TZ)
    answer = past_view(
        site_id="site_villa",
        tz=TZ,
        now=now,
        day=DAY,
        rows=ledger.between(hours[0], hours[-1] + timedelta(hours=1)),
        history_first=at(20),
        live=_plan_at(now),
    )
    _write_or_compare("today_mixed.json", answer)
    assert len(answer["hours"]) == 24
    assert [row["past"] for row in answer["hours"]] == [True] * 14 + [False] * 10
    assert answer["hours"][14] == _plan_at(now)["hours"][0]
    car = _load(answer, "car")
    assert car["planned"][14] is None and car["ran"][14] is None
    assert car["does_not_fit"] is not None


def test_no_history_at_all_is_every_hour_missing() -> None:
    answer = past_view(site_id="s", tz=TZ, now=at(48), day=DAY, rows=[], history_first=None)
    assert {row["recorded"] for row in answer["hours"]} == {"missing"}
    assert answer["loads"] == [] and answer["summary"]["cost"] is None and answer["history"]["first"] is None


def test_a_day_the_clock_changes_has_its_own_hours() -> None:
    assert len(day_hours(date(2026, 10, 25), TZ)) == 25
    assert len(day_hours(date(2026, 3, 29), TZ)) == 23
    assert day_hours(DAY, TZ)[0] == datetime(2026, 11, 17, tzinfo=ZONE).astimezone(timezone.utc)
