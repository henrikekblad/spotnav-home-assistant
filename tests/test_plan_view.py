"""`spotnav/get_plan`'s answer from the pure view (`site/plan_view.py`), and the contract fixtures the card reads
(`tests/fixtures/plan/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them):

* `villa.json`: a villa on 16 November 2026 at 20:10 with tomorrow's prices published: a car planned 01-05 at
  3.7 kW, a home battery whose plan SpotNav runs (it covers the evening, grid-charges 03-05 and charges from the sun
  at noon), a heat pump whose plan SpotNav only watches (lowered 07-09 and 17-19, pre-heated 05-07), one floor
  zone watched, a water heater SpotNav switches (23-01, cheap), a demand charge with a soft ceiling of 5 kW and the
  month's peak so far 5.4 kW; the import target leaves the car 3 kWh short of its 07:00 departure (`does_not_fit`).
* `car_only.json`: a flat with only a car, no power tariff, no battery and no sun: the main fuse is the ceiling,
  late evening before tomorrow's prices.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.spotnav.site.plan_view import (
    HOUR_KEYS,
    LOAD_KEYS,
    MAX_HOURS,
    PLAN_KEYS,
    RUN_KEYS,
    BatteryPlan,
    CarPlan,
    HeatPlan,
    PlanInputs,
    PriceHour,
    WaterHeaterPlan,
    ZonePlan,
    plan_view,
    window,
)

from custom_components.spotnav.site.does_not_fit import does_not_fit
from custom_components.spotnav.site.site_capacity_controller import ImportTargetStatus, TargetRisk

FIXTURES = Path(__file__).parent / "fixtures" / "plan"
TZ = "Europe/Stockholm"
ZONE = ZoneInfo(TZ)
MIDNIGHT = datetime(2026, 11, 16, 0, 0, tzinfo=ZONE).astimezone(timezone.utc)
NOW = MIDNIGHT + timedelta(hours=20, minutes=10)
HOUR = timedelta(hours=1)
#: Öre/kWh with every fee, today and tomorrow from midnight.
TODAY = [80, 78, 76, 75, 77, 85, 120, 190, 210, 160, 120, 110, 105, 100, 105, 130, 180, 240, 260, 220, 150, 110, 95, 85]
TOMORROW = [70, 62, 55, 50, 49, 60, 110, 180, 200, 150, 110, 95, 90, 88, 95, 120, 170, 230, 250, 210, 140, 100, 90, 80]


def at(hours: float) -> datetime:
    """A moment `hours` after today's local midnight (UTC)."""
    return MIDNIGHT + timedelta(hours=hours)


def prices(days: int = 2) -> tuple[PriceHour, ...]:
    out = []
    for index, price in enumerate((TODAY + TOMORROW)[: 24 * days]):
        start = at(index)
        for quarter in range(4):
            out.append(
                PriceHour(start + timedelta(minutes=15 * quarter), start + timedelta(minutes=15 * (quarter + 1)), float(price), float(price) * 0.6)
            )
    return tuple(out)


def _write_or_compare(name: str, answer: dict[str, Any]) -> None:
    path = FIXTURES / name
    text = json.dumps(answer, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text)


def _short_status() -> ImportTargetStatus:
    """The import target holds the garage's car 3 kWh short of its 07:00 departure (14.8 of 17.8 kWh fit); all of it
    fits by 08:00."""
    return ImportTargetStatus(
        target_kw=6.0,
        set_kw=6.0,
        hour_kw=4.1,
        held_a=None,
        paused_until=None,
        released_for=None,
        house_over=False,
        at_risk=TargetRisk(
            departure=at(31).isoformat(),
            missing_kwh=3.0,
            target_kw=6.0,
            need_kwh=17.8,
            later_departure=at(32).isoformat(),
        ),
        release_until=at(31).isoformat(),
        release_peak_kw=13.0,
    )


def villa_inputs() -> PlanInputs:
    house = {at(h): 1.6 + (0.8 if h % 24 in (17, 18, 19, 7, 8) else 0.0) for h in range(20, 48)}
    sun = {at(h): (2.0 if h % 24 in (11, 12, 13) else 0.6 if h % 24 in (10, 14) else 0.0) for h in range(20, 48)}
    battery_slots = []
    for h in range(20, 48):
        local = h % 24
        if local in (20, 21, 22) or local in (17, 18, 19):
            action, kw = "discharge_house", -1.8
        elif local in (3, 4):
            action, kw = "grid_charge", 3.0
        elif local in (11, 12, 13):
            action, kw = "self_consume", 1.2
        else:
            action, kw = "hold", 0.0
        for quarter in range(4):
            start = at(h) + timedelta(minutes=15 * quarter)
            battery_slots.append((start, start + timedelta(minutes=15), action, kw))
    band = []
    for h in range(24, 48):
        local = h % 24
        if local in (7, 8):
            band.append((at(h), "lower", "dear_hour"))
        elif local in (17, 18):
            band.append((at(h), "lower", "tight_hour"))
        elif local in (5, 6):
            band.append((at(h), "preheat", "cheap_before_dear"))
        else:
            band.append((at(h), "normal", "normal"))
    return PlanInputs(
        site_id="site_villa",
        now=NOW,
        tz=TZ,
        prices=prices(),
        money=("SEK", "kr", "öre"),
        house=house,
        house_basis="gross",
        sun_in_house=False,
        sun=sun,
        cars=(
            CarPlan(
                "charger_garage",
                "Garage",
                ((at(25), at(29), 3.7),),
                live=True,
                departure=at(31),
                does_not_fit=does_not_fit(_short_status(), ceiling="import_target", peak_cost=4500.0),
            ),
        ),
        battery=BatteryPlan("sensor.battery_soc", "Sigenergy", tuple(battery_slots), live=True),
        heat=HeatPlan(
            "switch.sg_ready_a", "SG Ready A", tuple(band), live=False, heat_kw=lambda _hour: 1.0, lower_share=0.5
        ),
        zones=(ZonePlan("climate.golvvarme_hall", "Golvvärme hall", ((at(31), at(33)),), live=False),),
        water_heater=WaterHeaterPlan("switch.varmvattenberedare", "Varmvattenberedare", ((at(23), at(25), "cheap"),), 3.0, live=True),
        soft_kw={at(h): 5.0 for h in range(20, 48)},
        fuse_kw=13.8,
        power_tariff=True,
        month_peak=(5.4, at(-4 * 24 + 18)),
    )


def car_only_inputs() -> PlanInputs:
    late = MIDNIGHT + timedelta(hours=22, minutes=40)
    return PlanInputs(
        site_id="site_flat",
        now=late,
        tz=TZ,
        prices=prices(1),
        money=("SEK", "kr", "öre"),
        house={at(h): 0.9 for h in range(22, 46)},
        cars=(CarPlan("charger_flat", "Laddbox", ((at(23), at(24), 11.0),), live=True, departure=at(31)),),
        fuse_kw=13.8,
    )


def _load(answer: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(load for load in answer["loads"] if load["kind"] == kind)


def test_the_villa() -> None:
    answer = plan_view(villa_inputs())
    _write_or_compare("villa.json", answer)
    assert tuple(answer) == PLAN_KEYS
    assert all(tuple(row) == HOUR_KEYS for row in answer["hours"])
    assert all(tuple(load) == LOAD_KEYS for load in answer["loads"])
    assert all(tuple(run) == RUN_KEYS for load in answer["loads"] for run in load["runs"])
    # A rolling window: from 20:00 tonight to the end of tomorrow's prices, never only today.
    assert answer["window"]["start"] == at(20).isoformat() and answer["window"]["end"] == at(48).isoformat()
    assert answer["window"]["hours"] == 28 == len(answer["hours"])
    assert [load["kind"] for load in answer["loads"]] == ["car", "home_battery", "heat", "heat_zone", "water_heater"]
    car = _load(answer, "car")
    assert car["state"] == "live" and not car["in_house"]
    assert [(run["start"], run["end"], run["what"], run["kw"], run["kwh"]) for run in car["runs"]] == [
        (at(25).isoformat(), at(29).isoformat(), "charge", 3.7, 14.8)
    ]
    # Its charge does not fit under the import target by 07:00: the block and the choices (`site/does_not_fit.py`).
    block = car["does_not_fit"]
    assert (block["short_kwh"], block["deliverable_kwh"], block["ceiling"], block["chosen"]) == (3.0, 14.8, "import_target", None)
    assert [choice["code"] for choice in block["choices"]] == ["takes_what_fits", "later", "accept_peak"]
    assert block["choices"][1]["departure"] == at(32).isoformat()
    assert all(load["does_not_fit"] is None for load in answer["loads"] if load["kind"] != "car")
    battery = _load(answer, "home_battery")
    assert [run["what"] for run in battery["runs"]] == ["cover", "grid_charge", "sun_charge", "cover"]
    assert battery["summary"] == {"charge_kwh": 9.6, "grid_charge_kwh": 6.0, "cover_kwh": 16.2}
    # The net: the house, the car and the battery, less the sun; the water heater is inside the house's figure.
    row = answer["hours"][5]  # 01:00 tomorrow: the car charges
    assert row["net_kw"] == round(1.6 + 3.7, 2)
    noon = answer["hours"][16]  # 12:00 tomorrow: the sun charges the battery
    assert noon["net_kw"] == round(1.6 + 1.2 - 2.0, 2)
    assert noon["ceiling"] == "power_tariff" and noon["ceiling_kw"] == 5.0
    assert answer["month_peak"] == {"kw": 5.4, "start": at(-78).isoformat()}
    assert answer["summary"]["peak_kw"] == 8.3 and answer["summary"]["peak_start"] == at(27).isoformat() and answer["summary"]["over"] is True
    assert answer["summary"]["peak_ceiling"] == "power_tariff"


def test_a_load_only_watched_says_would() -> None:
    answer = plan_view(villa_inputs())
    heat = _load(answer, "heat")
    assert heat["state"] == "would" and heat["in_house"]
    assert [(run["what"], run["reason"]) for run in heat["runs"]] == [
        ("preheat", "cheap_before_dear"),
        ("lower", "dear_hour"),
        ("lower", "tight_hour"),
    ]
    # Lowered hours take the learnt share off the pump's heat; the level says lowered (-1) or raised (+1).
    morning = [answer["hours"].index(row) for row in answer["hours"] if row["start"] == at(31).isoformat()][0]
    assert (heat["level"][morning], heat["kw"][morning]) == (-1, 0.5)
    assert _load(answer, "heat_zone")["state"] == "would"
    assert _load(answer, "water_heater")["state"] == "live"
    assert answer["would"] is True
    live = villa_inputs()
    from dataclasses import replace

    only_live = replace(live, heat=None, zones=())
    assert plan_view(only_live)["would"] is False


def test_only_a_car_and_the_fuse() -> None:
    answer = plan_view(car_only_inputs())
    _write_or_compare("car_only.json", answer)
    # The car's charge fits: no block.
    assert _load(answer, "car")["does_not_fit"] is None
    # Tomorrow's prices are not out yet: the window ends with today's.
    assert answer["window"]["hours"] == 2
    assert answer["power_tariff"] is False and answer["month_peak"] is None
    assert [load["kind"] for load in answer["loads"]] == ["car"]
    assert {row["ceiling"] for row in answer["hours"]} == {"fuse"}
    assert {row["ceiling_kw"] for row in answer["hours"]} == {13.8}
    assert all(row["sun_kw"] is None for row in answer["hours"])
    assert answer["hours"][1]["net_kw"] == 11.9
    assert answer["summary"]["over"] is False
    # Import 0.9 kW at 95 öre, then 11.9 kW at 85 öre.
    assert answer["summary"]["cost"] == round(0.9 * 95 + 11.9 * 85, 2)


def test_the_import_target_is_the_ceiling_where_it_is_lower() -> None:
    from dataclasses import replace

    inputs = replace(villa_inputs(), hold_kw={at(h): 4.5 for h in range(20, 48)}, hold_kind="import_target")
    answer = plan_view(inputs)
    assert {(row["ceiling"], row["ceiling_kw"]) for row in answer["hours"]} == {("import_target", 4.5)}


def test_the_window() -> None:
    # No prices: a day from the running hour; prices reaching far: at most MAX_HOURS.
    assert len(window(NOW, TZ, ())) == 24
    far = tuple(PriceHour(at(h), at(h + 1), 1.0, 1.0) for h in range(0, 72))
    assert len(window(NOW, TZ, far)) == MAX_HOURS
    assert window(NOW, TZ, far)[0] == at(20)
