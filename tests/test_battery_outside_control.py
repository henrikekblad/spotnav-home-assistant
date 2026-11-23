"""Something other than SpotNav drives the battery: the signals, the verdict, one told occurrence, its end and a
restart (`battery/outside_control.py`), and the coexistence table (`battery/coexist.py`)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.spotnav.battery.coexist import (
    COEXISTENCE,
    coexistence_for,
    outside_integrations,
)
from custom_components.spotnav.battery.facts import BatteryFacts, Management, with_outside_control
from custom_components.spotnav.battery.outside_control import (
    BEHAVIOUR_S,
    CLEAR_AFTER,
    SETTING_WINDOW,
    Observation,
    OutsideControlWatch,
)

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
MODE = "EMS work mode"
LIMIT = "number.sigen_plant_ess_max_discharging_limit"


def _obs(at: datetime, **kwargs) -> Observation:
    base = {
        "now": at,
        "management": "free",
        "modes": {MODE: "max_self_consumption"},
        "controls": {LIMIT: "23.04"},
        "power_kw": 0.0,
        "grid_kw": 0.5,
        "soc_pct": 50.0,
        "reserve_soc_pct": 10.0,
        "battery_age_s": 5.0,
        "on_grid": True,
    }
    return Observation(**(base | kwargs))


def _run(watch: OutsideControlWatch, start: datetime, seconds: float, step: float = 10.0, **kwargs) -> list[dict]:
    events: list[dict] = []
    t = 0.0
    while t <= seconds:
        events += watch.observe(_obs(start + timedelta(seconds=t), **kwargs))
        t += step
    return events


# --- the table ----------------------------------------------------------------------------------------------------


def test_every_brand_named_in_the_research_has_a_row_and_is_watch_only() -> None:
    brands = {row.brand for row in COEXISTENCE}
    assert {"sigenergy", "huawei", "sungrow", "goodwe", "pixii", "ferroamp", "homevolt", "sonnen", "emaldo"} <= brands
    assert all(row.watch_only for row in COEXISTENCE)
    assert coexistence_for("sigenergy").platforms == ("sigen",)
    assert coexistence_for(None, "sonnenbatterie").brand == "sonnen"
    assert coexistence_for("nobody", "nothing") is None


def test_emaldo_has_no_integration_and_names_its_own_plan_and_partner() -> None:
    row = coexistence_for("emaldo")
    assert row is not None and row.platforms == ()
    assert "Bixia" in row.aggregators[0]


def test_only_an_integration_that_runs_batteries_marks_by_itself() -> None:
    found = outside_integrations({"tibber", "checkwatt", "met"})
    assert [(row.domain, row.strength) for row in found] == [("checkwatt", "runs"), ("tibber", "hint")]


def test_omnibattery_runs_a_battery_by_its_presence_alone() -> None:
    found = outside_integrations({"omnibattery", "tibber"})
    assert [(row.domain, row.strength) for row in found] == [("tibber", "hint"), ("omnibattery", "runs")]


# --- integrations and planners ------------------------------------------------------------------------------------


def test_a_set_up_checkwatt_marks_the_battery_without_telling_and_ends_when_it_goes() -> None:
    watch = OutsideControlWatch()
    [started] = watch.observe(_obs(T0, integrations=frozenset({"checkwatt"})))
    assert started["kind"] == "started" and started["reason"] == "integration:checkwatt"
    assert watch.verdict.active and watch.verdict.source == "CheckWatt" and not watch.verdict.told
    assert watch.observe(_obs(T0 + timedelta(hours=1), integrations=frozenset({"checkwatt"}))) == []
    [ended] = watch.observe(_obs(T0 + timedelta(hours=2)))
    assert ended["kind"] == "ended" and not watch.verdict.active
    assert watch.history[-1]["reason"] == "integration:checkwatt"


def test_tibber_alone_never_marks_but_is_named_when_the_battery_shows_it() -> None:
    watch = OutsideControlWatch()
    assert _run(watch, T0, 3600, integrations=frozenset({"tibber"})) == []
    events = _run(watch, T0 + timedelta(hours=2), BEHAVIOUR_S + 60, step=60, integrations=frozenset({"tibber"}),
                  power_kw=3.0, grid_kw=4.0, soc_pct=None) or []
    assert events == []  # no level: never judged
    soc = iter(range(50, 80))
    events = []
    for minute in range(12):
        events += watch.observe(_obs(T0 + timedelta(hours=3, minutes=minute), integrations=frozenset({"tibber"}),
                                     power_kw=3.0, grid_kw=4.0, soc_pct=float(next(soc))))
    [started] = events
    assert started["reason"] == "grid_charge_behaviour" and started["source"] == "Tibber" and started["told"]


def test_a_planner_marks_while_it_runs() -> None:
    watch = OutsideControlWatch()
    [started] = watch.observe(_obs(T0, planner="predbat"))
    assert started["reason"] == "planner:predbat" and started["source"] == "Predbat" and not started["told"]


# --- settings -----------------------------------------------------------------------------------------------------


def test_one_or_two_setting_changes_are_a_person_three_are_someone_else() -> None:
    watch = OutsideControlWatch()
    watch.observe(_obs(T0))
    assert watch.observe(_obs(T0 + timedelta(minutes=1), modes={MODE: "ai_mode"}, management="self_managed")) == []
    assert watch.observe(_obs(T0 + timedelta(minutes=2))) == []
    [started] = watch.observe(_obs(T0 + timedelta(minutes=3), controls={LIMIT: "0.0"}))
    assert started["reason"] == "settings_changed" and started["told"]
    assert watch.counts == {"settings_changed": 1}


def test_unavailable_readings_are_no_change() -> None:
    watch = OutsideControlWatch()
    for minute, state in enumerate(("23.04", "unavailable", "23.04", "unknown", "23.04", "unavailable", "23.04")):
        assert watch.observe(_obs(T0 + timedelta(minutes=minute), controls={LIMIT: state})) == []


def test_changes_spread_over_more_than_a_day_do_not_mark() -> None:
    watch = OutsideControlWatch()
    watch.observe(_obs(T0))
    gap = SETTING_WINDOW / 2 + timedelta(minutes=1)
    for n, limit in enumerate(("10", "20", "10", "20")):
        assert watch.observe(_obs(T0 + gap * (n + 1), controls={LIMIT: limit})) == []


def test_under_its_own_optimiser_only_mode_changes_count() -> None:
    watch = OutsideControlWatch()
    for minute, limit in enumerate(("23.04", "10", "20", "5", "15")):
        assert watch.observe(
            _obs(T0 + timedelta(minutes=minute), management="self_managed", modes={MODE: "ai_mode"}, controls={LIMIT: limit})
        ) == []


def test_spotnav_acting_is_not_someone_else() -> None:
    watch = OutsideControlWatch()
    for minute, limit in enumerate(("23.04", "0", "23.04", "0", "23.04")):
        assert watch.observe(_obs(T0 + timedelta(minutes=minute), controls={LIMIT: limit}, spotnav_acting=True)) == []
    # Once it lets go the baseline starts again: its last write is no change.
    assert watch.observe(_obs(T0 + timedelta(minutes=10), controls={LIMIT: "23.04"})) == []


# --- behaviour ----------------------------------------------------------------------------------------------------


def _charging_from_grid(watch: OutsideControlWatch, start: datetime, minutes: int, soc_step: float = 0.2, **kwargs):
    events = []
    for minute in range(minutes + 1):
        events += watch.observe(
            _obs(start + timedelta(minutes=minute), power_kw=3.0, grid_kw=4.0, soc_pct=50.0 + soc_step * minute, **kwargs)
        )
    return events


def test_a_grid_charge_self_consumption_never_makes_marks_after_ten_minutes() -> None:
    watch = OutsideControlWatch()
    assert _charging_from_grid(watch, T0, 9) == []
    watch = OutsideControlWatch()
    [started] = _charging_from_grid(watch, T0, 10)
    assert started["reason"] == "grid_charge_behaviour" and started["source"] is None


def test_an_export_discharge_marks() -> None:
    watch = OutsideControlWatch()
    events = []
    for minute in range(11):
        events += watch.observe(_obs(T0 + timedelta(minutes=minute), power_kw=-4.0, grid_kw=-3.0, soc_pct=80.0 - minute))
    assert [event["reason"] for event in events] == ["export_discharge_behaviour"]


def test_a_reading_with_its_sign_the_wrong_way_does_not_mark() -> None:
    """A night's discharge read as a charge: the level falls, so it is not a grid charge."""
    watch = OutsideControlWatch()
    assert _charging_from_grid(watch, T0, 30, soc_step=-0.3) == []


def test_a_maintenance_charge_near_the_floor_its_own_optimiser_and_stale_readings_do_not_mark() -> None:
    assert _charging_from_grid(OutsideControlWatch(), T0, 20, reserve_soc_pct=48.0) == []
    assert _charging_from_grid(OutsideControlWatch(), T0, 20, management="self_managed") == []
    assert _charging_from_grid(OutsideControlWatch(), T0, 20, battery_age_s=600.0) == []
    assert _charging_from_grid(OutsideControlWatch(), T0, 20, on_grid=False) == []
    assert _charging_from_grid(OutsideControlWatch(), T0, 20, measurement_ok=False) == []


def test_a_short_frequency_reserve_activation_does_not_mark() -> None:
    watch = OutsideControlWatch()
    events = []
    for minute in range(60):
        busy = minute % 6 < 3
        events += watch.observe(
            _obs(T0 + timedelta(minutes=minute), power_kw=3.0 if busy else 0.0, grid_kw=4.0, soc_pct=50.0 + minute * 0.1)
        )
    assert events == []


# --- an occurrence ------------------------------------------------------------------------------------------------


def test_an_occurrence_is_told_once_and_ends_two_days_after_the_last_signal() -> None:
    watch = OutsideControlWatch()
    [started] = _charging_from_grid(watch, T0, 10)
    since = watch.verdict.since
    # The battery shows it again the next day: the same occurrence, nothing new to tell.
    assert _charging_from_grid(watch, T0 + timedelta(days=1), 10) == []
    assert watch.verdict.since == since and watch.counts["grid_charge_behaviour"] == 2
    last = T0 + timedelta(days=1, minutes=10)
    assert watch.observe(_obs(last + CLEAR_AFTER - timedelta(minutes=1))) == []
    [ended] = watch.observe(_obs(last + CLEAR_AFTER))
    assert ended["kind"] == "ended" and ended["told"] and not watch.verdict.active


def test_a_restart_keeps_the_occurrence_and_does_not_tell_it_again() -> None:
    watch = OutsideControlWatch()
    _charging_from_grid(watch, T0, 10)
    again = OutsideControlWatch.from_dict(watch.as_dict())
    assert again.verdict == watch.verdict and again.counts == watch.counts
    assert _charging_from_grid(again, T0 + timedelta(hours=1), 10) == []
    assert OutsideControlWatch.from_dict({"verdict": {"active": True, "since": "not a time"}}).verdict.active is False
    assert OutsideControlWatch.from_dict(None).verdict.active is False


def test_a_person_clears_a_settings_verdict_and_the_old_changes_do_not_mark_again() -> None:
    watch = OutsideControlWatch()
    watch.observe(_obs(T0))
    watch.observe(_obs(T0 + timedelta(minutes=1), controls={LIMIT: "0.0"}))
    watch.observe(_obs(T0 + timedelta(minutes=2), controls={LIMIT: "23.04"}))
    [started] = watch.observe(_obs(T0 + timedelta(minutes=3), controls={LIMIT: "0.0"}))
    # One more change after the mark: counted, then forgotten by the clear.
    watch.observe(_obs(T0 + timedelta(minutes=4), controls={LIMIT: "5.0"}))
    at = T0 + timedelta(minutes=5)
    ended = watch.clear_by_person(at)
    assert ended is not None and ended["kind"] == "ended" and ended["cleared_by"] == "person"
    assert ended["reason"] == "settings_changed" and ended["ended"] == at.isoformat()
    assert not watch.verdict.active and watch.history[-1]["cleared_by"] == "person"
    assert watch.clear_by_person(at) is None, "nothing to clear"
    # The same settings read again, and two new changes: no new mark (the third would be).
    assert watch.observe(_obs(at + timedelta(minutes=1), controls={LIMIT: "5.0"})) == []
    assert watch.observe(_obs(at + timedelta(minutes=2), controls={LIMIT: "6.0"})) == []
    assert watch.observe(_obs(at + timedelta(minutes=3), controls={LIMIT: "7.0"})) == []
    assert not watch.verdict.active
    # Kept across a restart, in the history the diagnostics show.
    again = OutsideControlWatch.from_dict(watch.as_dict())
    assert not again.verdict.active and again.history[-1]["cleared_by"] == "person"


def test_a_person_clears_a_behaviour_verdict_but_never_an_integration_or_a_planner() -> None:
    watch = OutsideControlWatch()
    _charging_from_grid(watch, T0, 10)
    assert watch.verdict.reason == "grid_charge_behaviour"
    assert watch.clear_by_person(T0 + timedelta(hours=1)) is not None and not watch.verdict.active
    for kwargs in ({"integrations": frozenset({"checkwatt"})}, {"planner": "predbat"}):
        watch = OutsideControlWatch()
        watch.observe(_obs(T0, **kwargs))
        assert watch.clear_by_person(T0 + timedelta(minutes=1)) is None
        assert watch.verdict.active and not watch.clearable


# --- the facts ----------------------------------------------------------------------------------------------------


def test_the_verdict_marks_the_facts_and_keeps_their_settings() -> None:
    facts = BatteryFacts(battery_entity_id="sensor.battery", management=Management("free", "self_consumption", {MODE: "x"}))
    marked = with_outside_control(facts, True, "settings_changed")
    assert marked.management == Management("externally_controlled", "settings_changed", {MODE: "x"})
    assert with_outside_control(facts, False, None) is facts
    assert with_outside_control(BatteryFacts(), True, "settings_changed").management.state == "unknown"
    planner = replace(facts, management=Management("externally_controlled", "planner:predbat"))
    assert with_outside_control(planner, True, "settings_changed") is planner
