"""The GoodWe (HACS fork) and Tesla Powerwall (cloud, slow) recipes: recipe data, the mapping per action, the
hand-back, the slow-recipe rules and the note for core GoodWe. Both recipes are mapped from source only, never run
on a device."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import timedelta

from custom_components.spotnav.battery.adapters import adapter_for
from custom_components.spotnav.battery.commands import (
    GOODWE_CONTROL,
    SOC_DEPTH,
    SOC_FLOOR,
    TESLA_CONTROL,
    TESLA_WRITES_PER_DAY,
    control_for,
    fixed_rate_kw,
    pct_of_rated,
    service_call,
)
from custom_components.spotnav.battery.executor import (
    REARM_S,
    Executor,
    RoleRead,
)
from custom_components.spotnav.battery.facts import BatteryFacts, Management
from custom_components.spotnav.battery.gate import restricted
from custom_components.spotnav.battery.ledger import READBACK_DEADLINE_S
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, run, written

GOODWE_STATES = {"operation_mode": "general", "eco_power": "0", "eco_soc": "0", "depth_of_discharge": "80"}
TESLA_STATES = {"reserve": "20", "grid_charging": "off"}


@dataclass
class _Fake(FakeBattery):
    capacity_kwh: float | None = None
    rated_kw: float | None = 10.0

    def management(self) -> Management:
        return Management("free", "self_consumption", {})

    def facts(self, power_kw: float) -> BatteryFacts:
        return replace(
            super().facts(power_kw), rated_charge_kw=self.rated_kw, capacity_kwh=self.capacity_kwh,
        )


def _goodwe(**kw) -> tuple[Executor, _Fake]:
    return Executor(GOODWE_CONTROL), _Fake(GOODWE_CONTROL, dict(GOODWE_STATES), soc_pct=42.7, **kw)


def _tesla(**kw) -> tuple[Executor, _Fake]:
    return Executor(TESLA_CONTROL), _Fake(TESLA_CONTROL, dict(TESLA_STATES), soc_pct=42.7, **kw)


# --- recipe data ---------------------------------------------------------------------------------------------------


def test_the_recipes_are_registered_and_their_adapters_found_by_platform() -> None:
    assert control_for("goodwe") is GOODWE_CONTROL and control_for("tesla") is TESLA_CONTROL
    assert adapter_for("goodwe").brand == "goodwe"
    assert adapter_for("tesla_fleet").brand == "tesla" and adapter_for("teslemetry").brand == "tesla"


def test_the_recipe_data_of_each_brand() -> None:
    for control in (GOODWE_CONTROL, TESLA_CONTROL):
        # No dead-man, no setpoints: settings only.
        assert control.watchdog_roles == () and control.local_readback
        assert all(role.write_class == "setting" for role in control.roles)
        assert control.grid_charge_phases == 1
        assert not control.supports("export_discharge")
        assert control.supports("hold") and control.supports("grid_charge") and control.supports("discharge_house")
    assert GOODWE_CONTROL.write_cap("setting") == 96 and not GOODWE_CONTROL.slow and not GOODWE_CONTROL.fixed_charge
    assert GOODWE_CONTROL.hacs_roles == ("eco_power", "eco_soc")
    assert TESLA_CONTROL.slow and TESLA_CONTROL.fixed_charge
    assert TESLA_CONTROL.write_cap("setting") == TESLA_WRITES_PER_DAY == 24
    assert TESLA_CONTROL.readback_s and TESLA_CONTROL.readback_s > READBACK_DEADLINE_S
    assert TESLA_CONTROL.rearm_s and TESLA_CONTROL.rearm_s > REARM_S
    # The mode is read, never written; the switch only exists with solar.
    assert "default_real_mode" not in " ".join(r.match.pattern.pattern for r in TESLA_CONTROL.roles)
    assert TESLA_CONTROL.role("grid_charging").optional


def test_the_entity_matchers_know_each_integrations_unique_id_shape() -> None:
    gw = {role.name: role.match for role in GOODWE_CONTROL.roles}
    assert gw["operation_mode"].matches("select", "goodwe-operation_mode-9010KETU000W0000")
    assert gw["eco_power"].matches("number", "goodwe-eco_mode_power-9010KETU000W0000")
    assert gw["depth_of_discharge"].matches("number", "goodwe-battery_discharge_depth-SN1")
    assert not gw["eco_power"].matches("number", "goodwe-eco_mode_power_extra-SN1")
    ts = {role.name: role.match for role in TESLA_CONTROL.roles}
    assert ts["reserve"].matches("number", "1234567-backup_reserve_percent")
    assert ts["grid_charging"].matches("switch", "1234567-components_disallow_charge_from_grid_with_solar_installed")
    assert not ts["reserve"].matches("number", "LRW3E7-backup_reserve_percent_x")


# --- the mapping ---------------------------------------------------------------------------------------------------


def test_goodwe_maps_each_action() -> None:
    hold = GOODWE_CONTROL.wanted("hold", 0.0, soc_pct=42.7)
    assert hold["depth_of_discharge"] == 100.0 - 42.0 and hold["operation_mode"] == "<snapshot>"
    charge = GOODWE_CONTROL.wanted("grid_charge", 3.0, rated_kw=10.0)
    assert charge == {"eco_soc": 100.0, "eco_power": 30.0, "depth_of_discharge": "<snapshot>", "operation_mode": "eco_charge"}
    # Rounded down to a whole percent, at least 1, at most 100.
    assert GOODWE_CONTROL.wanted("grid_charge", 2.99, rated_kw=10.0)["eco_power"] == 29.0
    assert GOODWE_CONTROL.wanted("grid_charge", 0.01, rated_kw=10.0)["eco_power"] == 1.0
    assert GOODWE_CONTROL.wanted("grid_charge", 50.0, rated_kw=10.0)["eco_power"] == 100.0
    house = GOODWE_CONTROL.wanted("discharge_house", 0.0)
    assert house["operation_mode"] == "general" and house["eco_power"] == "<snapshot>"
    assert GOODWE_CONTROL.enter_order[-1] == "operation_mode", "numbers first, the select sends the group last"
    assert GOODWE_CONTROL.restore_order[0] == "operation_mode", "mode first back, so no eco group is live"
    # The depth never passes the number's range (0-99).
    assert GOODWE_CONTROL.wanted("hold", 0.0, soc_pct=0.4)["depth_of_discharge"] == 99.0
    assert GOODWE_CONTROL.wanted("hold", 0.0, soc_pct=100.0)["depth_of_discharge"] == 0.0


def test_tesla_maps_each_action() -> None:
    assert TESLA_CONTROL.wanted("hold", 0.0, soc_pct=42.7)["reserve"] == 42.0, "never rounded up"
    assert TESLA_CONTROL.wanted("hold", 0.0, soc_pct=42.7)["grid_charging"] == "<snapshot>"
    charge = TESLA_CONTROL.wanted("grid_charge", 3.0)
    assert charge == {"reserve": 100.0, "grid_charging": True}
    assert TESLA_CONTROL.wanted("discharge_house", 0.0) == {"reserve": "<snapshot>", "grid_charging": "<snapshot>"}
    assert TESLA_CONTROL.enter_order == ("grid_charging", "reserve") and TESLA_CONTROL.restore_order[0] == "reserve"
    # No site without solar: the optional switch is left out of every state.
    assert "grid_charging" not in TESLA_CONTROL.wanted("grid_charge", 3.0, absent=frozenset({"grid_charging"}))
    # The inverted switch is written as any switch: on means grid charging allowed.
    assert service_call("switch", "switch.x", True)[1] == "turn_on"


def test_a_value_that_needs_a_reading_is_unresolved_without_it() -> None:
    assert GOODWE_CONTROL.unresolved(GOODWE_CONTROL.wanted("hold", 0.0)) == "soc_unknown"
    assert GOODWE_CONTROL.unresolved(GOODWE_CONTROL.wanted("grid_charge", 3.0)) == "rated_unknown"
    assert TESLA_CONTROL.unresolved(TESLA_CONTROL.wanted("hold", 0.0)) == "soc_unknown"
    assert TESLA_CONTROL.unresolved(TESLA_CONTROL.wanted("hold", 0.0, soc_pct=50.0)) is None
    assert SOC_FLOOR != SOC_DEPTH and pct_of_rated(1.5).startswith("<pct_of_rated:")


# --- the executor: writes and hand-back -------------------------------------------------------------------------


def test_a_goodwe_grid_charge_writes_numbers_then_mode_and_hands_back_in_the_restore_order() -> None:
    executor, battery = _goodwe()
    now, results = run(executor, battery, T0, 10, "grid_charge", 4.0)
    assert written(results) == [("eco_soc", 100.0), ("eco_power", 40.0), ("operation_mode", "eco_charge")]
    assert battery.states["operation_mode"] == "eco_charge"
    _now, results = run(executor, battery, now, 10, "self_consume")
    assert written(results) == [("operation_mode", "general"), ("eco_power", 0.0), ("eco_soc", 0.0)]
    assert battery.states == GOODWE_STATES | {"eco_power": "0", "eco_soc": "0"}
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_a_goodwe_hold_sets_the_cut_off_to_the_level_and_hands_the_depth_back() -> None:
    executor, battery = _goodwe()
    now, results = run(executor, battery, T0, 6, "hold")
    assert written(results) == [("depth_of_discharge", 58.0)]
    _now, results = run(executor, battery, now, 8, "self_consume")
    assert written(results) == [("depth_of_discharge", 80.0)]
    assert battery.states["depth_of_discharge"] == "80"


@dataclass
class _GoodweModes(_Fake):
    """GoodWe with the adapter's own mode rule: free only in `general` (`adapters.GOODWE`, as `facts._management`
    reads it)."""

    def management(self) -> Management:
        mode = self.states["operation_mode"]
        state = "free" if mode == "general" else "self_managed"
        return Management(state, "self_consumption" if state == "free" else "operating mode", {"operating mode": mode})


def test_a_goodwe_grid_charge_is_not_taken_for_the_plants_own_management() -> None:
    executor, battery = Executor(GOODWE_CONTROL), _GoodweModes(GOODWE_CONTROL, dict(GOODWE_STATES), soc_pct=42.7)
    _now, results = run(executor, battery, T0, 30, "grid_charge", 4.0)
    assert battery.states["operation_mode"] == "eco_charge"
    assert executor.state.phase == "active" and not events(results, "hand_back")
    # An eco charge found at the start is someone else's: refused.
    executor = Executor(GOODWE_CONTROL)
    battery = _GoodweModes(GOODWE_CONTROL, dict(GOODWE_STATES) | {"operation_mode": "eco_charge"}, soc_pct=42.7)
    _now, results = run(executor, battery, T0, 4, "grid_charge", 4.0)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"].startswith("self_managed")


def test_goodwe_without_a_battery_rating_does_not_start_a_grid_charge() -> None:
    executor, battery = _goodwe(rated_kw=None)
    _now, results = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "rated_unknown"


def test_core_goodwe_without_the_eco_numbers_says_it_needs_the_hacs_integration() -> None:
    executor, battery = _goodwe(missing={"eco_power", "eco_soc"})
    _now, results = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "needs_hacs_integration"
    # Every action restores the eco values, so a hold is not started either.
    executor, battery = _goodwe(missing={"eco_power", "eco_soc"})
    _now, results = run(executor, battery, T0, 6, "hold")
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "needs_hacs_integration"


def test_a_tesla_hold_and_grid_charge_and_the_hand_back_order() -> None:
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    now, results = run(executor, battery, T0, 8, "hold")
    assert written(results) == [("reserve", 42.0)]
    now += timedelta(hours=1)
    _now, results = run(executor, battery, now, 12, "self_consume")
    assert written(results) == [("reserve", 20.0)]
    now += timedelta(hours=1)
    now, results = run(executor, battery, now, 10, "grid_charge", 3.0, headroom_a=30.0)
    assert written(results) == [("grid_charging", True), ("reserve", 100.0)]
    _now, results = run(executor, battery, now, 12, "self_consume")
    assert written(results) == [("reserve", 20.0), ("grid_charging", "off")]
    assert battery.states == TESLA_STATES | {"reserve": "20", "grid_charging": "off"}


def test_a_tesla_site_without_solar_has_no_grid_charging_switch_and_still_charges_by_the_reserve() -> None:
    executor, battery = _tesla(missing={"grid_charging"}, capacity_kwh=13.5, rated_kw=None)
    _now, results = run(executor, battery, T0, 8, "grid_charge", 3.0, headroom_a=30.0)
    assert written(results) == [("reserve", 100.0)]
    assert not events(results, "not_started")


# --- the slow recipe's rules ---------------------------------------------------------------------------------------


def test_a_slow_recipe_has_no_peak_cover_and_a_fixed_charge_judged_only_on_charging() -> None:
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    now, _ = run(executor, battery, T0, 6, "hold")
    # A peak that would lift a fast hold changes nothing here.
    now, results = run(executor, battery, now, 6, "hold", headroom_a=1.6)
    assert not events(results, "peak_cover") and written(results) == []
    assert TESLA_CONTROL.peak_first.get("hold") is None


def test_the_slow_recipe_changes_its_action_no_sooner_than_its_minimum() -> None:
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    now, _ = run(executor, battery, T0, 8, "hold")
    now, results = run(executor, battery, now, 6, "grid_charge", 3.0, headroom_a=30.0)
    assert not events(results, "change"), "held back within min_change_s"
    now += timedelta(seconds=TESLA_CONTROL.min_change_s)
    _now, results = run(executor, battery, now, 4, "grid_charge", 3.0, headroom_a=30.0)
    assert events(results, "change")[0]["reason"] == "grid_charge"


def test_a_slow_recipe_waits_longer_to_rearm_and_its_read_back_longer() -> None:
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    now, _ = run(executor, battery, T0, 8, "hold")
    now, _ = run(executor, battery, now, 12, "self_consume")
    assert executor.state.phase == "idle"
    _now, results = run(executor, battery, now + timedelta(seconds=REARM_S + 10), 3, "hold")
    assert written(results) == [], "quiet: still rearming after the fast recipes' wait"
    _now, results = run(executor, battery, now + timedelta(seconds=TESLA_CONTROL.rearm_s + 10), 6, "hold")
    assert written(results) == [("reserve", 42.0)]
    # The ledger waits for the cloud: the claim's deadline is the recipe's.
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    run(executor, battery, T0, 1, "hold")
    claim = next(iter(executor.ledger.as_dict()["claims"].values()))
    assert claim["deadline"] == (T0 + timedelta(seconds=TESLA_CONTROL.readback_s)).isoformat()


def test_a_slow_recipe_stops_at_its_own_daily_write_cap() -> None:
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    executor.state = replace(executor.state, day=T0.date().isoformat(), writes_today=TESLA_WRITES_PER_DAY)
    _now, results = run(executor, battery, T0, 4, "hold")
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "daily_write_cap"


def test_the_fixed_charge_rate_is_the_plants_own_maximum() -> None:
    facts = BatteryFacts(capacity_kwh=13.5)
    assert fixed_rate_kw(TESLA_CONTROL, facts) == 5.0
    assert fixed_rate_kw(TESLA_CONTROL, BatteryFacts(capacity_kwh=27.0)) == 10.0
    # The size unknown: the number of Powerwalls is unknown, so the rate is unbounded (two would be checked as one).
    assert fixed_rate_kw(TESLA_CONTROL, BatteryFacts()) == math.inf
    assert fixed_rate_kw(TESLA_CONTROL, BatteryFacts(rated_charge_kw=7.0)) == math.inf
    assert fixed_rate_kw(TESLA_CONTROL, BatteryFacts(capacity_kwh=13.5, rated_charge_kw=7.0)) == 7.0
    assert fixed_rate_kw(GOODWE_CONTROL, facts) is None


def test_a_fixed_charge_that_does_not_fit_under_the_fuse_is_refused_not_lowered() -> None:
    slot = ("grid_charge", 3.0, None)
    assert restricted(slot, {"grid_charge"}, 4.0, None, 5.0) == ((("self_consume", 0.0, None)), "grid_charge_cap_fixed")
    assert restricted(slot, {"grid_charge"}, 6.0, None, 5.0) == (slot, None)
    assert restricted(slot, {"grid_charge"}, None, None, 5.0) == (slot, None)
    # The size unknown: never run, whatever the cap.
    assert restricted(slot, {"grid_charge"}, None, None, math.inf) == (("self_consume", 0.0, None), "grid_charge_size_unknown")
    # A settable rate is lowered to the cap, as before.
    assert restricted(slot, {"grid_charge"}, 2.0) == (("grid_charge", 2.0, None), "grid_charge_cap")


def test_the_executor_refuses_a_fixed_charge_over_the_live_headroom() -> None:
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    _now, results = run(executor, battery, T0, 4, "grid_charge", 3.0, headroom_a=4.0)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "fuse_headroom"
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    _now, results = run(executor, battery, T0, 8, "grid_charge", 3.0, headroom_a=30.0)
    assert executor.state.phase == "active" and executor.state.rate_kw == 5.0


def test_a_fixed_charge_without_the_batterys_size_is_refused() -> None:
    executor, battery = _tesla(capacity_kwh=None, rated_kw=None)
    _now, results = run(executor, battery, T0, 4, "grid_charge", 3.0, headroom_a=60.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "size_unknown"


def test_the_live_check_counts_a_fixed_charge_on_one_phase() -> None:
    # 5 kW on one phase is 21.7 A: 9.5 A of headroom (7.5 A once the margin is kept) is far too little, though as
    # three-phase power it would be 5.2 kW.
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    _now, results = run(executor, battery, T0, 4, "grid_charge", 3.0, headroom_a=9.5)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "fuse_headroom"
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    _now, results = run(executor, battery, T0, 4, "grid_charge", 3.0, headroom_a=23.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "fuse_headroom"
    executor, battery = _tesla(capacity_kwh=13.5, rated_kw=None)
    _now, results = run(executor, battery, T0, 8, "grid_charge", 3.0, headroom_a=24.0)
    assert executor.state.phase == "active"


def test_the_reads_of_an_absent_optional_role_are_not_needed() -> None:
    assert RoleRead().found is False and TICK
