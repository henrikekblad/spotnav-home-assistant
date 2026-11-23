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
    assert battery["summary"] == {
        "charge_kwh": 9.6, "grid_charge_kwh": 6.0, "cover_kwh": 16.2, "grid_charge_held_kwh": None,
    }
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


def test_the_shed_order_shows_paused_and_lowered_for_the_ceiling() -> None:
    """The water heater paused and the heat lowered one step by the shed order under the ceiling (`site/shed_order.py`)
    for the rest of the running hour: `paused` / `lower` with the reason `ceiling`; the watched heat says `would`."""
    from dataclasses import replace

    base = villa_inputs()
    heater = replace(base.water_heater, windows=((at(20), at(22), "cheap"),), shed=(NOW, at(21)))
    heat = replace(base.heat, shed=(NOW, at(21)))
    answer = plan_view(replace(base, water_heater=heater, heat=heat))
    lane = _load(answer, "water_heater")
    assert [(run["what"], run["reason"], run["start"], run["end"]) for run in lane["runs"]] == [
        ("paused", "ceiling", at(20).isoformat(), at(21).isoformat()),
        ("on", "cheap", at(21).isoformat(), at(22).isoformat()),
    ]
    assert lane["state"] == "live"
    heat_lane = _load(answer, "heat")
    assert heat_lane["state"] == "would"
    assert (heat_lane["runs"][0]["what"], heat_lane["runs"][0]["reason"]) == ("lower", "ceiling")
    assert heat_lane["level"][0] == -1


def test_a_grid_charge_that_ran_some_quarters_says_its_power_and_minutes() -> None:
    # A SolaX tester's night: 3.3 kW in four of the eight quarters of 22-24, holds between.
    start = at(22)
    slots = []
    for quarter in range(8):
        charging = quarter in (0, 2, 4, 6)
        slots.append(
            (
                start + timedelta(minutes=15 * quarter),
                start + timedelta(minutes=15 * (quarter + 1)),
                "grid_charge" if charging else "hold",
                3.3 if charging else 0.0,
            )
        )
    inputs = PlanInputs(
        "site",
        NOW,
        TZ,
        prices=prices(),
        battery=BatteryPlan("sensor.soc", None, tuple(slots), live=False),
    )
    battery = _load(plan_view(inputs), "home_battery")
    run = next(item for item in battery["runs"] if item["what"] == "grid_charge")
    assert run["kw"] == 3.3
    assert run["active_min"] == 60
    assert run["kwh"] == 3.3  # the energy: 3.3 kW for an hour

    # Every quarter charging keeps the old form: the mean, no minutes.
    full = [(start + timedelta(minutes=15 * q), start + timedelta(minutes=15 * (q + 1)), "grid_charge", 3.3) for q in range(8)]
    inputs = PlanInputs("site", NOW, TZ, prices=prices(), battery=BatteryPlan("sensor.soc", None, tuple(full), live=False))
    run = _load(plan_view(inputs), "home_battery")["runs"][0]
    assert run["kw"] == 3.3 and run["active_min"] is None


def test_the_pools_circulation_and_heat_pump_are_watched_lanes_inside_the_house() -> None:
    from dataclasses import replace

    from custom_components.spotnav.site.plan_view import PoolPlanView

    pool = PoolPlanView(
        circulation={at(h): why for h, why in ((24, "cheap"), (25, "cheap"), (26, "cheap"), (35, "sun"), (36, "sun"), (40, "extra"))},
        heat={at(35): "sun", at(36): "sun"},
        circulation_kw=0.4,
        heat_pump_kw=1.6,
        need_h=5.0,
        heat_h=2.0,
        circulation_name="Pool cirkulation",
        heat_pump_name="Poolpump",
    )
    base = villa_inputs()
    answer = plan_view(replace(base, pool=pool))
    _write_or_compare("pool.json", answer)
    circulation, heat = _load(answer, "pool_circulation"), _load(answer, "pool_heat")
    assert (circulation["id"], heat["id"]) == ("pool_circulation", "pool_heat")
    assert circulation["state"] == heat["state"] == "would" and circulation["in_house"] and heat["in_house"]
    assert [(run["what"], run["reason"], run["kwh"]) for run in circulation["runs"]] == [
        ("filter", "cheap", 1.2), ("filter", "sun", 0.8), ("extra", "extra", 0.4)
    ]
    assert [(run["what"], run["reason"], run["kw"]) for run in heat["runs"]] == [("heat", "sun", 1.6)]
    assert circulation["summary"] == {"need_h": 5.0, "planned_h": 6.0} and heat["summary"] == {"heat_h": 2.0, "planned_h": 2.0}
    # In the house: the net is as without them.
    assert [row["net_kw"] for row in answer["hours"]] == [row["net_kw"] for row in plan_view(base)["hours"]]


def test_the_spa_and_the_sauna_are_watched_lanes_with_their_heat_up_and_hold() -> None:
    from dataclasses import replace

    from custom_components.spotnav.site.plan_view import DeadlinePlanView

    spa = DeadlinePlanView(
        "spa",
        {at(30) + timedelta(minutes=15 * q): "cheap" for q in range(6)},
        hold=(at(31) + timedelta(minutes=30), at(34)),
        kw=3.0,
        target_c=38.0,
        ready_at=at(31) + timedelta(minutes=30),
        name="Spa",
    )
    sauna = DeadlinePlanView("sauna", {at(41) + timedelta(minutes=15 * q): "bath" for q in range(3)}, kw=9.0, target_c=80.0, name="Bastu")
    answer = plan_view(replace(villa_inputs(), deadlines=(spa, sauna)))
    _write_or_compare("spa_sauna.json", answer)
    lane = _load(answer, "spa")
    assert lane["id"] == "spa" and lane["state"] == "would" and lane["in_house"]
    assert [(run["what"], run["reason"]) for run in lane["runs"]] == [("heat", "cheap"), ("hold", None)]
    assert lane["summary"] == {"target_c": 38.0, "ready_at": (at(31) + timedelta(minutes=30)).isoformat(), "heat_h": 1.5}
    hour = [row["start"] for row in answer["hours"]].index(at(31).isoformat())
    assert lane["kw"][hour] == 1.5
    sauna_lane = _load(answer, "sauna")
    assert [(run["what"], run["kwh"]) for run in sauna_lane["runs"]] == [("heat", 6.75)]
