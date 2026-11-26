"""A battery's rating is the lowest of every limit known, and a rating that is not read comes down when the battery
stops under what was asked of it (`battery/facts.py`, `battery/learned.py`).

The field case (2026-11-26): a Victron MultiPlus-II 48/8000/110 on a 32 kWh bank whose BMS allows 314 A at 52 V
(16 kW). The Multi's charger takes about 6 kW, so a plan that counted on 16 kW put 29.7 kWh into two hours and the
battery reached 48 %, not 99 %."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.spotnav.battery.facts import BatteryFacts, EntityFact, battery_facts
from custom_components.spotnav.battery.learned import (
    EST_RATED_LEARNED,
    SOURCE_LEARNED,
    LearnedBattery,
    with_learned,
)
from custom_components.spotnav.battery.plan import plan_battery
from custom_components.spotnav.battery.plan_model import ACTION_GRID_CHARGE, PlanRequest, PlanSlot
from custom_components.spotnav.battery.plan_runtime import limited_params
from custom_components.spotnav.battery.plan_shadow import BUCKET, Bucket, PlanActuals, battery_params, bucket_start
from tests.test_battery_facts import BRANDS, _entities, _facts

T0 = datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=2)

#: The field case's BMS: 314 A at 52 V and a 32 kWh bank (615 Ah).
FIELD = {
    "sensor.victron_battery_max_charge_current": {"state": "314"},
    "sensor.victron_battery_max_discharge_current": {"state": "314"},
    "sensor.victron_system_battery_voltage": {"state": "52.0"},
    "sensor.victron_battery_installed_capacity": {"state": "615"},
    "sensor.victron_system_battery_soc": {"state": "20"},
}


def _extra(unique: str, state: str, unit: str, domain: str = "sensor", device: str = "vebus") -> EntityFact:
    return EntityFact(
        entity_id=f"{domain}.victron_{unique}", platform="victron_gx", unique_id=f"c0619ab1_x_0_{unique}",
        device_id=device, config_entry_id="entry", state=state, unit=unit,
    )


def _victron(*extra: EntityFact, found: dict[str, str] | None = None, **changes: dict[str, Any]) -> BatteryFacts:
    brand = BRANDS["victron"]
    entities = [*_entities(brand, **(FIELD | changes)), *extra]
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])
    return battery_facts(battery, entities, manufacturer=brand["manufacturer"], power_kw=0.0, found_states=found)


# ---- the lowest of every limit known -------------------------------------------------------------------------------


def test_the_field_case_plans_with_the_multis_power_not_the_bms_limit() -> None:
    facts = _victron(_extra("vebus_nominal_inverter_power", "6400", "W"))

    assert facts.rated_charge_kw == pytest.approx(16.328), "the BMS' limit times the voltage is still what is read"
    assert facts.inverter_charge_kw == 6.4 and facts.inverter_discharge_kw == 6.4
    assert facts.max_charge_kw == 6.4 and facts.max_discharge_kw == 6.4
    limits = facts.limits()
    assert limits["charge"]["set_by"] == "inverter"
    assert limits["charge"]["source"] == "sensor.victron_vebus_nominal_inverter_power"
    assert limits["charge"]["battery_kw"] == pytest.approx(16.328)
    assert facts.as_dict()["limits"] == limits

    params, reason, _estimates = limited_params(*battery_params(facts), facts)
    assert reason is None and params is not None
    assert (params.max_charge_kw, params.max_discharge_kw) == (6.4, 6.4)

    # Two cheap hours before an expensive day: the plan fills what the charger carries, not 29.7 kWh.
    slots = tuple(
        PlanSlot(start=T0 + timedelta(hours=i), end=T0 + timedelta(hours=i + 1), buy=price, sell=price - 60,
                 house_kw=1.0, solar_kw=0.0, car_kw=0.0)
        for i, price in enumerate([20, 20, *[300] * 22])
    )
    result = plan_battery(PlanRequest(slots=slots, battery=params, soc_now_pct=20, end_value=0))
    charged = sum(d.battery_kw for d in result.plan.decisions[:2] if d.action == ACTION_GRID_CHARGE)
    assert 0 < charged <= 2 * 6.4 + 1e-6


def test_without_the_multis_power_the_bms_limit_stands_until_the_battery_shows_its_own() -> None:
    facts = _victron()

    assert facts.inverter_charge_kw is None
    assert facts.max_charge_kw == pytest.approx(16.328)
    assert facts.limits()["charge"]["set_by"] == "battery"


def test_the_lowest_of_the_ess_and_dvcc_settings_counts_and_minus_one_is_no_limit() -> None:
    unset = _victron(
        _extra("system_ess_max_charge_power", "-1", "W", "number", "settings"),
        _extra("system_ess_max_charge_current", "-1", "A", "number", "settings"),
    )
    assert unset.inverter_charge_kw is None and unset.max_charge_kw == pytest.approx(16.328)

    dvcc = _victron(
        _extra("vebus_nominal_inverter_power", "6400", "W"),
        _extra("system_ess_max_charge_power", "8000", "W", "number", "settings"),
        _extra("system_ess_max_charge_current", "100", "A", "number", "settings"),
    )
    assert dvcc.inverter_charge_kw == pytest.approx(5.2), "100 A at 52 V is under both powers"
    assert dvcc.sources["inverter_charge"] == (
        "number.victron_system_ess_max_charge_current x sensor.victron_system_battery_voltage"
    )
    assert dvcc.inverter_discharge_kw == 6.4, "the fixture's discharge power setting is -1: the Multi's power counts"


def test_a_setting_spotnav_wrote_itself_is_read_as_it_was_found() -> None:
    charge = _extra("system_ess_max_charge_power", "1500", "W", "number", "settings")

    assert _victron(charge).inverter_charge_kw == 1.5, "a person's own setting is a limit"
    assert _victron(charge, found={charge.entity_id: "-1"}).inverter_charge_kw is None, "SpotNav's own rate is not"
    held = _victron(found={"number.victron_settings_ess_max_inverter_power_limit": "5000"},
                    **{"number.victron_settings_ess_max_inverter_power_limit": {"state": "0"}})
    assert held.inverter_discharge_kw == 5.0, "a hold's 0 is SpotNav's; the 5 kW found before it is the limit"
    assert _victron(**{"number.victron_settings_ess_max_inverter_power_limit": {"state": "0"}}).inverter_discharge_kw is None


def test_the_modbus_integration_reads_dvcc_and_the_discharge_power() -> None:
    brand = BRANDS["victron_modbus"]

    def limit(unique: str, state: str, unit: str) -> EntityFact:
        return EntityFact(entity_id=f"number.victron_{unique}", platform="victron", unique_id=f"100_{unique}",
                          device_id="system", config_entry_id="entry", state=state, unit=unit)

    entities = [*_entities(brand), limit("settings_ess_maxchargecurrent", "50", "A"),
                limit("settings_ess_maxdischargepower", "3000", "W")]
    battery = next(entity for entity in entities if entity.entity_id == brand["battery"])
    facts = battery_facts(battery, entities, power_kw=0.0)

    assert (facts.rated_charge_kw, facts.rated_discharge_kw) == (4.0, 5.0)
    assert (facts.max_charge_kw, facts.max_discharge_kw) == (2.5, 3.0)


@pytest.mark.parametrize("name", sorted(BRANDS))
def test_a_brand_without_a_lower_limit_keeps_its_rating(name: str) -> None:
    facts = _facts(name)

    assert facts.inverter_charge_kw is None and facts.inverter_discharge_kw is None
    plant = facts.plant_max_kw
    for rated, most in ((facts.rated_charge_kw, facts.max_charge_kw), (facts.rated_discharge_kw, facts.max_discharge_kw)):
        assert most == (rated if rated is None or plant is None else min(rated, plant))


def test_a_limit_alone_is_no_rating() -> None:
    gone = {"sensor.victron_battery_max_charge_current": {"state": "unavailable"}}
    facts = _victron(_extra("vebus_nominal_inverter_power", "6400", "W"), **gone)

    assert facts.rated_charge_kw is None and facts.max_charge_kw is None, "learnt first, then capped"
    learned = LearnedBattery(rated_charge_kw=8.0)
    assert with_learned(facts, learned).max_charge_kw == 6.4


# ---- the learnt rating falls ---------------------------------------------------------------------------------------


def _asked(
    start: datetime, kw: float, asked: float | None, count: int, *, soc: float = 30.0, import_kw: float | None = None,
    cap_kw: float | None = 20.0, covered: float = 900.0,
) -> list[Bucket]:
    """`count` quarter hours at `kw` (+ charging) under a command of `asked` kW."""
    out = []
    for i in range(count):
        bucket = Bucket(start=start + BUCKET * i, cap_kw=cap_kw)
        hours = covered / 3600.0
        bucket.battery_kwh, bucket.covered_s, bucket.asked_kw = kw * hours, covered, asked
        bucket.import_kwh = (max(kw, 0.0) + 1.0 if import_kw is None else import_kw) * hours
        step = kw * hours / 32.0 * 100.0
        bucket.soc_start, bucket.soc_end = soc + step * i, soc + step * (i + 1)
        out.append(bucket)
    return out


def _bms(charge_kw: float = 16.3, discharge_kw: float = 16.3) -> BatteryFacts:
    return BatteryFacts(
        battery_entity_id="sensor.battery", soc_pct=50.0, capacity_kwh=32.0,
        rated_charge_kw=charge_kw, rated_discharge_kw=discharge_kw, estimates=("rated_from_current",),
        sources={"rated_charge": "sensor.ccl x sensor.volts", "rated_discharge": "sensor.dcl x sensor.volts"},
    )


def test_the_rating_falls_to_what_the_battery_held_under_a_higher_command() -> None:
    learned = LearnedBattery()
    levels = [5.8, 5.9, 6.0, 5.9, 5.7]
    buckets = [bucket for i, kw in enumerate(levels) for bucket in _asked(T0 + BUCKET * i, kw, 16.3, 1, soc=30 + 4 * i)]

    assert learned.learn(buckets[:3], NOW) is True, "the highest held is learnt as before"
    assert learned.charge_cap_kw is None, "three quarter hours are not enough"
    assert learned.learn(buckets, NOW) is True
    assert learned.charge_cap_kw == 6.0, "the 90th percentile of the five"
    assert learned.rated_charge_kw == 5.9, "the highest two in a row held, as before"

    filled = with_learned(_bms(), learned)
    assert filled.rated_charge_kw == 6.0 and filled.max_charge_kw == 6.0
    assert filled.sources["rated_charge"] == SOURCE_LEARNED and filled.sources["rated_charge_learned"] == "saturated"
    assert EST_RATED_LEARNED in filled.estimates
    assert filled.rated_discharge_kw == 16.3, "the other way is untouched"
    params, _reason, _estimates = limited_params(*battery_params(filled), filled)
    assert params is not None and params.max_charge_kw == 6.0

    assert learned.learn(buckets, NOW) is False, "the same quarter hours again teach nothing"
    again = LearnedBattery.from_dict(learned.as_dict())
    assert (again.charge_cap_kw, again.charge_cap_at) == (6.0, buckets[-1].end)
    assert learned.view()["charge_cap"] == [6.0, buckets[-1].end.isoformat()]


def test_a_rating_the_plant_states_does_not_fall() -> None:
    learned = LearnedBattery()
    learned.learn(_asked(T0, 6.0, 10.0, 6), NOW)
    assert learned.charge_cap_kw == 6.0
    stated = BatteryFacts(battery_entity_id="sensor.battery", rated_charge_kw=10.0, rated_discharge_kw=10.0,
                          sources={"rated_charge": "sensor.rated", "rated_discharge": "sensor.rated"})

    assert with_learned(stated, learned).rated_charge_kw == 10.0


@pytest.mark.parametrize(
    ("why", "kwargs"),
    [
        ("the taper near full", {"soc": 88.0}),
        ("the fuse holds the charge back", {"import_kw": 18.0}),
        ("nothing was asked", {"asked": None}),
        ("not asked throughout", {"asked": 0.0}),
        ("asked too little", {"asked": 0.9, "kw": 0.5}),
        ("the battery follows the command", {"asked": 6.5}),
        ("a quarter hour half measured", {"covered": 500.0}),
    ],
)
def test_the_rating_does_not_fall_when(why: str, kwargs: dict[str, Any]) -> None:
    learned = LearnedBattery()
    asked = kwargs.pop("asked", 16.3)
    kw = kwargs.pop("kw", 6.0)

    learned.learn(_asked(T0, kw, asked, 6, **kwargs), NOW)

    assert learned.charge_cap_kw is None, why
    assert with_learned(_bms(), learned).rated_charge_kw == 16.3, why


def test_a_level_that_moves_is_no_limit_but_the_level_it_settles_at_is() -> None:
    moving = LearnedBattery()
    moving.learn([b for i, kw in enumerate([3.0, 6.0, 4.0, 7.0, 3.5, 5.5])
                  for b in _asked(T0 + BUCKET * i, kw, 16.3, 1)], NOW)
    assert moving.charge_cap_kw is None, "a house load that eats the fuse's headroom moves"

    settled = LearnedBattery()
    settled.learn([b for i, kw in enumerate([3.0, 4.5, 6.0, 6.0, 5.9, 6.0])
                   for b in _asked(T0 + BUCKET * i, kw, 16.3, 1)], NOW)
    assert settled.charge_cap_kw == 6.0


def test_a_gap_breaks_the_run() -> None:
    learned = LearnedBattery()
    learned.learn([*_asked(T0, 6.0, 16.3, 3), *_asked(T0 + BUCKET * 4, 6.0, 16.3, 3)], NOW)
    assert learned.charge_cap_kw is None


def test_the_fallen_rating_rises_again_when_more_is_held_later() -> None:
    learned = LearnedBattery()
    low = _asked(T0, 6.0, 16.3, 5)
    learned.learn(low, NOW)
    assert learned.charge_cap_kw == 6.0
    # The sun (nothing asked) later charges with 9 kW for two quarter hours: the battery manages it after all.
    more = _asked(T0 + timedelta(hours=10), 9.0, None, 2)

    assert learned.learn([*low, *more], NOW) is True
    assert learned.charge_cap_kw == 9.0 and learned.rated_charge_kw == 9.0
    assert learned.learn([*low, *more], NOW) is False, "the old saturated run does not pull it down again"
    assert with_learned(_bms(), learned).rated_charge_kw == 9.0


def test_more_held_before_the_cap_does_not_lift_it() -> None:
    learned = LearnedBattery()
    before = _asked(T0 - timedelta(hours=10), 9.0, None, 2)
    low = _asked(T0, 6.0, 16.3, 5)

    learned.learn([*before, *low], NOW)

    assert learned.rated_charge_kw == 9.0, "the highest held only rises"
    assert learned.charge_cap_kw == 6.0, "but the charger was set down since: what it stops at now counts"
    assert learned.learn([*before, *low], NOW) is False


def test_a_later_saturated_run_moves_the_cap_only_when_it_differs_clearly() -> None:
    learned = LearnedBattery()
    first = _asked(T0, 6.0, 16.3, 5)
    near = _asked(T0 + timedelta(hours=24), 5.8, 16.3, 5)
    lower = _asked(T0 + timedelta(hours=30), 4.0, 16.3, 5)
    learned.learn(first, NOW)

    learned.learn([*first, *near], NOW)
    assert (learned.charge_cap_kw, learned.charge_cap_at) == (6.0, first[-1].end)
    learned.learn([*first, *near, *lower], NOW)
    assert (learned.charge_cap_kw, learned.charge_cap_at) == (4.0, lower[-1].end)


def test_the_discharge_rating_falls_the_same_way_but_not_near_empty() -> None:
    learned = LearnedBattery()
    learned.learn(_asked(T0, -6.2, -16.3, 5, soc=80.0), NOW)
    assert learned.discharge_cap_kw == 6.2 and learned.charge_cap_kw is None
    filled = with_learned(_bms(), learned)
    assert (filled.rated_charge_kw, filled.rated_discharge_kw) == (16.3, 6.2)

    empty = LearnedBattery()
    empty.learn(_asked(T0, -6.2, -16.3, 5, soc=30.0), NOW)
    assert empty.discharge_cap_kw is None, "the last quarter hours end under 20 %"


def test_a_rating_only_learnt_falls_too_and_rises_as_before() -> None:
    unread = BatteryFacts(battery_entity_id="sensor.battery", soc_pct=50.0, capacity_kwh=10.0)
    learned = LearnedBattery()
    learned.learn(_asked(T0, 5.0, None, 2), NOW)
    assert with_learned(unread, learned).rated_charge_kw == 5.0
    assert with_learned(unread, learned).sources["rated_charge_learned"] == "held"

    learned.learn(_asked(T0 + timedelta(hours=5), 3.0, 5.0, 5), NOW)

    assert with_learned(unread, learned).rated_charge_kw == 3.0
    assert learned.rated_charge_kw == 5.0


# ---- what the control asked, per quarter hour ----------------------------------------------------------------------


def _observed(asked: list[float | None]) -> Bucket:
    actuals = PlanActuals()
    start = bucket_start(T0)
    actuals.open(start, price=None, cap_kw=None, ran=None)
    asked = [value for value in asked for _minute in range(5)]
    step = BUCKET / len(asked)
    for i, value in enumerate(asked):
        actuals.observe(start + step * i, 7.0, 0.0, 6.0, 50.0, None, value)
    actuals.observe(start + BUCKET, 7.0, 0.0, 6.0, 51.0, None, None)
    return actuals.buckets[start]


def test_a_quarter_hour_keeps_the_lowest_rate_asked_all_through_it() -> None:
    assert _observed([16.0, 12.0, 16.0]).asked_kw == 12.0
    assert _observed([16.0, None, 16.0]).asked_kw == 0.0, "not asked for a part of it"
    assert _observed([16.0, -5.0, 16.0]).asked_kw == 0.0, "not the same way"
    assert _observed([None, None, None]).asked_kw == 0.0
    assert _observed([-5.0, -4.0, -6.0]).asked_kw == -4.0


def test_what_was_asked_survives_a_restart_and_an_older_row_has_none() -> None:
    bucket = _observed([16.0, 12.0, 16.0])
    row = bucket.as_row()

    assert Bucket.from_row(row).asked_kw == 12.0  # type: ignore[union-attr]
    assert Bucket.from_row(row[:18]).asked_kw is None  # type: ignore[union-attr]
    assert Bucket.from_row([*row[:18], "x"]) is None


def _runtime_asked(action: str | None, rate: float, phase: str = "active", **facts: Any) -> float | None:
    from types import SimpleNamespace

    from custom_components.spotnav.battery.plan_runtime import BatteryPlanShadow

    state = SimpleNamespace(phase=phase, action=action, rate_kw=rate)
    runtime = SimpleNamespace(site=SimpleNamespace(battery_control=SimpleNamespace(executor=SimpleNamespace(state=state))))
    return BatteryPlanShadow._asked_kw(runtime, BatteryFacts(**facts))  # type: ignore[arg-type]


def test_the_runtime_asks_what_its_executor_has_standing() -> None:
    from custom_components.spotnav.battery.plan_model import ACTION_EXPORT_DISCHARGE, ACTION_HOLD

    assert _runtime_asked(ACTION_GRID_CHARGE, 8.0) == 8.0
    assert _runtime_asked(ACTION_EXPORT_DISCHARGE, 5.0) == -5.0
    assert _runtime_asked(ACTION_GRID_CHARGE, 8.0, bms_charge_kw=3.0) == 3.0, "the BMS' present limit is what counts"
    assert _runtime_asked(ACTION_GRID_CHARGE, 8.0, cell_temperature_c=4.0) is None, "a cold pack says nothing"
    assert _runtime_asked(ACTION_GRID_CHARGE, 8.0, phase="restoring") is None
    assert _runtime_asked(ACTION_HOLD, 0.0) is None
    assert _runtime_asked(None, 0.0, phase="idle") is None
