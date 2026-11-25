"""Fronius's control recipe (`commands.FRONIUS_CONTROL`, core `fronius` over Modbus) through the executor against a
fake Gen24 that does what core's source says (home-assistant/core `d33dfe3a`): the limits in % of WChaMax acting only
while their switch is on, a negative limit forcing the other direction, a pair of enabled limits summing below 0
refused (`coordinator._refused_battery_limits`), grid charging read and never written. The recipe as data, each
action's writes and their order, every change of action without a refused pair, the rating as read, the hand-back,
the fuse with Home Assistant stopped (core's revert option is no lift), the check before control in both timings and
the gate. Nothing here has run on a device."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import pytest

from custom_components.spotnav.battery import gate, optin
from custom_components.spotnav.battery.adapters import FRONIUS, adapter_for
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    FRONIUS_CONTROL,
    FRONIUS_REVERT_S,
    SETPOINT_WRITES_UNSTATED,
    control_for,
    field_tested,
    hardware_test_first,
    pct_of_rated,
    table,
)
from custom_components.spotnav.battery.executor import HEADROOM_MARGIN_A, REARM_S, Executor, Op, preview
from custom_components.spotnav.battery.facts import BatteryFacts, Management
from custom_components.spotnav.battery.gate import grid_charge_cap_kw, shadow_default
from tests import battery_control_fakes
from tests.battery_control_fakes import T0, FakeBattery, events, inputs, written

VOLTS = 400.0 / 3 ** 0.5
#: Core's unique ids, `<inverter id>-modbus-<key>`, per role.
INVERTER = "123456789"
KEYS = {
    "charge_limit": ("number", f"{INVERTER}-modbus-battery_charge_power_limit"),
    "discharge_limit": ("number", f"{INVERTER}-modbus-battery_discharge_power_limit"),
    "charge_limit_on": ("switch", f"{INVERTER}-modbus-battery_charge_power_limit_enabled"),
    "discharge_limit_on": ("switch", f"{INVERTER}-modbus-battery_discharge_power_limit_enabled"),
    "grid_charging": ("switch", f"{INVERTER}-modbus-battery_grid_charging"),
}
STATES = {
    "charge_limit": "100", "discharge_limit": "100", "charge_limit_on": "off", "discharge_limit_on": "off",
    "grid_charging": "on",
}
#: WChaMax (kW): the base of both limits.
RATED_KW = 6.0


@dataclass
class Gen24(FakeBattery):
    """A Gen24 as core `fronius` shows it. With both limit switches off it runs its own self-consumption: it covers
    the house (less the sun) and stores a surplus, each up to WChaMax. A switch on applies its limit: a positive one
    caps that direction at its share of WChaMax, a negative one forces the other direction at least that fast (a
    forced charge only with grid charging on). Core refuses a write that would leave both limits enabled summing
    below 0, with an error and nothing written."""

    house_kw: float = 2.0
    sun_kw: float = 0.0
    rated_kw: float | None = RATED_KW
    #: Where the rating came from (`facts.sources["rated_charge"]`).
    rating_source: str = "sensor.inverter_battery_power_limit_reference"
    #: Writes core refused (a pair summing below 0).
    refused: list[Op] = field(default_factory=list)

    def apply(self, op: Any) -> bool:
        after = dict(self.states)
        if op.method == "switch":
            after[op.role] = "on" if op.value in (True, "on") else "off"
        elif op.method == "number":
            after[op.role] = f"{float(op.value):g}"
        if (
            after["charge_limit_on"] == "on" and after["discharge_limit_on"] == "on"
            and float(after["charge_limit"]) + float(after["discharge_limit"]) < 0
        ):
            self.writes.append(op)
            self.refused.append(op)
            return False
        taken = super().apply(op)
        if op.method == "number" and taken and op.role not in self.drops:
            self.states[op.role] = f"{float(op.value):g}"
        return taken

    def management(self) -> Management:
        settings = {"charge limit": self.states["charge_limit_on"], "discharge limit": self.states["discharge_limit_on"]}
        on = [label for label, state in settings.items() if state == "on"]
        if not on:
            return Management("free", "self_consumption", settings)
        return Management("self_managed", f"{on[0]}:on", settings)

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        """The battery's power (kW, + charging) from what the inverter holds, whatever the executor thinks."""
        if not self.obeys:
            return self.idle_kw
        top = RATED_KW
        charge_on, discharge_on = self.states["charge_limit_on"] == "on", self.states["discharge_limit_on"] == "on"
        charge, discharge = float(self.states["charge_limit"]), float(self.states["discharge_limit"])
        if discharge_on and discharge < 0 and self.states["grid_charging"] == "on":
            # A forced charge at least that fast; the sun may add to it, up to the charge limit where it is on.
            forced = max(-discharge / 100 * top, self.sun_kw - self.house_kw)
            return min(forced, charge / 100 * top) if charge_on else forced
        if charge_on and charge < 0:
            return charge / 100 * top
        need = self.house_kw - self.sun_kw
        if need > 0:
            return -min(need, top * (max(discharge, 0.0) / 100 if discharge_on else 1.0))
        return min(-need, top * (max(charge, 0.0) / 100 if charge_on else 1.0))

    def facts(self, power_kw: float) -> BatteryFacts:
        return BatteryFacts(
            battery_entity_id="sensor.solarnet_power_battery", integration="fronius", brand="fronius",
            soc_pct=self.soc_pct, power_kw=power_kw, rated_charge_kw=self.rated_kw, rated_discharge_kw=self.rated_kw,
            firmware="1.39.5-1", on_grid=None, management=self.management(),
            sources={"rated_charge": self.rating_source},
        )


def run(executor: Executor, plant: Gen24, start: Any, looks: int, action: str | None, rate_kw: float = 0.0,
        **overrides: Any) -> Any:
    """`battery_control_fakes.run` with the person's acknowledgement that SpotNav's limits override the web UI's own
    strategy (`BrandControl.strategy_unread`), as the card asks for it first."""
    return battery_control_fakes.run(executor, plant, start, looks, action, rate_kw, **({"takeover": True} | overrides))


def _plant(**kwargs: Any) -> tuple[Executor, Gen24]:
    return Executor(FRONIUS_CONTROL), Gen24(FRONIUS_CONTROL, dict(STATES), idle_kw=-2.0, **kwargs)


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_one_limit_and_its_switch_per_action() -> None:
    control = FRONIUS_CONTROL
    assert control_for("fronius") is control and control in CONTROLS and control.adapter is FRONIUS
    assert adapter_for("fronius", "Fronius") is FRONIUS
    assert control.wanted("hold", 0.0) == {
        "charge_limit": "<snapshot>", "discharge_limit": 0.0, "charge_limit_on": "<snapshot>",
        "discharge_limit_on": True,
    }, "grid charging is never part of a state: it is not restored, nor written"
    # The rate as a negative share of WChaMax, rounded down (1 kW of 6 kW is 16 %, 0.96 kW), at most 100 %, and the
    # charge limit at the same share as its cap.
    charge = control.wanted("grid_charge", 1.0, rated_kw=RATED_KW)
    assert (charge["discharge_limit"], charge["charge_limit"]) == (-16.0, 16.0)
    assert charge["discharge_limit_on"] is True and charge["charge_limit_on"] is True
    assert control.rising_first == ("charge_limit", "discharge_limit")
    assert control.wanted("grid_charge", 9.0, rated_kw=RATED_KW)["discharge_limit"] == -100.0
    assert control.wanted("grid_charge", 0.01, rated_kw=RATED_KW)["discharge_limit"] == -1.0
    # A rate of nothing is a hold, never -0 % or a force at the minimum share.
    assert control.wanted("grid_charge", 0.0, rated_kw=RATED_KW) == control.wanted("hold", 0.0)
    # Without a rating the share stays unresolved: the charge does not start.
    assert control.unresolved(control.wanted("grid_charge", 1.0)) == "rated_unknown"
    assert control.wanted("discharge_house", 2.0) == {
        "charge_limit": 0.0, "discharge_limit": "<snapshot>", "charge_limit_on": True,
        "discharge_limit_on": "<snapshot>",
    }
    assert all(value == "<snapshot>" for value in control.wanted("self_consume", 0.0).values())
    # No sale yet, and the hold stores the sun (not idle).
    assert not control.supports("export_discharge") and not control.hold_idle()
    rows = [row for row in table() if row["brand"] == "fronius"]
    assert {row["action"] for row in rows} == {"hold", "grid_charge", "discharge_house", "self_consume"}
    assert [(rule.label, rule.free) for rule in FRONIUS.modes] == [
        ("charge limit", frozenset({"off"})), ("discharge limit", frozenset({"off"})),
    ]
    assert control.own_states == {"charge limit": frozenset({"on"}), "discharge limit": frozenset({"on"})}
    assert control.strategy_unread, "web-UI schedules and the Energy Cost Assistant cannot be read"


def test_a_negative_power_is_a_negative_share_and_a_positive_one_as_before() -> None:
    from custom_components.spotnav.battery.commands import _resolve  # noqa: PLC0415

    assert _resolve(pct_of_rated(1.0), None, 6.0) == 16.0
    assert _resolve(pct_of_rated(-1.0), None, 6.0) == -16.0
    assert _resolve(pct_of_rated(-0.001), None, 6.0) == -1.0 and _resolve(pct_of_rated(0.0), None, 6.0) == 1.0
    assert _resolve(pct_of_rated(-60.0), None, 6.0) == -100.0
    assert _resolve(pct_of_rated(-1.0), None, None) == pct_of_rated(-1.0)


def test_every_role_is_cores_key_and_the_card_offers_each() -> None:
    roles = {role.name: role for role in FRONIUS_CONTROL.roles}
    assert set(roles) == set(KEYS)
    for name, (domain, unique_id) in KEYS.items():
        assert [key for key, role in roles.items() if role.match.matches(domain, unique_id)] == [name]
    # Never a role: the reserve, the AC power limit and its switch, the storage's own sensors.
    for domain, unique_id in (
        ("number", f"{INVERTER}-modbus-battery_minimum_reserve"), ("number", f"{INVERTER}-modbus-ac_power_limit"),
        ("switch", f"{INVERTER}-modbus-ac_power_limit_enabled"), ("sensor", "BATSERIAL1-state_of_charge"),
        ("sensor", f"{INVERTER}-modbus-battery_power_limit_reference"),
    ):
        assert not any(role.match.matches(domain, unique_id) for role in roles.values()), unique_id
    for role in roles.values():
        assert any(
            role.match.matches(domain, f"{INVERTER}-modbus-{key}") for domain, key in optin.ENABLE_KEYS["fronius"]
        )
    assert not roles["grid_charging"].restored
    assert all(role.restored_below == (0.0, 100.0) for name, role in roles.items() if role.method == "number")


def test_the_revert_timer_is_cores_and_never_lifts_a_cap() -> None:
    control = FRONIUS_CONTROL
    expiry = control.expiry
    assert (expiry.kind, expiry.renewer, expiry.window_s, expiry.integration) == (
        "device_deadman", "integration", FRONIUS_REVERT_S, "fronius"
    )
    # SpotNav renews nothing (core's heartbeat does), and a person's hardware test cannot time it.
    assert control.watchdog_roles == () and control.renewal_cap is None and not expiry.silence
    assert not gate.deadman_testable(expiry, slow=False, fixed_charge=False)
    for measured in (False, True):
        assert not gate.expiry_lifts(expiry, measured=measured, slow=False, fixed_charge=False)
    assert gate.expiry_note(expiry, measured=False) == "continues while HA runs"
    assert gate.expiry_energy_kwh(expiry, 3.0) is None
    assert control.import_limit is None and control.autonomous_import is None and control.phase_control is None
    assert control.grid_charge_phases == 1 and control.output_type is None
    assert control.write_cap("setpoint") == SETPOINT_WRITES_UNSTATED and control.write_cap("setting") == 96
    assert {role.name for role in control.roles if role.write_class == "setpoint"} == {
        "charge_limit", "discharge_limit", "charge_limit_on", "discharge_limit_on",
    }
    assert all(role.readback == "register" for role in control.roles) and not control.local_readback


def test_nothing_is_written_until_a_hardware_test_passed() -> None:
    assert FRONIUS_CONTROL.hardware_test_first and not FRONIUS_CONTROL.field_tested
    assert hardware_test_first("fronius") and not field_tested("fronius")
    assert shadow_default(field_tested=False, beta=False)


# --- the writes and their read-back ----------------------------------------------------------------------------------


def test_a_hold_writes_the_discharge_limit_then_its_switch_and_hands_back_switch_first() -> None:
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 6, "hold")
    assert written(results) == [("discharge_limit", 0.0), ("discharge_limit_on", True)]
    assert [len(result.ops) for result in results[:3]] == [1, 1, 0], "one write a look, each read back first"
    assert executor.state.phase == "active" and plant.power_kw() == 0.0
    plant.sun_kw = 3.0
    assert plant.power_kw() == 1.0, "the sun is still stored"
    plant.sun_kw = 0.0
    now, results = run(executor, plant, now, 6, "self_consume")
    assert written(results) == [("discharge_limit_on", "off"), ("discharge_limit", 100.0)]
    assert plant.states == STATES and executor.state.phase == "idle" and plant.refused == []


def test_a_grid_charge_is_the_discharge_limit_below_0_with_the_charge_limit_as_its_cap() -> None:
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 10, "grid_charge", 1.0)
    # The forced charge (-p) and its cap (+p): the pair sums to 0, which core accepts.
    assert written(results) == [
        ("charge_limit", 16.0), ("discharge_limit", -16.0), ("charge_limit_on", True), ("discharge_limit_on", True),
    ]
    assert plant.power_kw() == pytest.approx(0.96) and executor.state.phase == "active" and plant.refused == []
    # The sun cannot take the charge above the rate.
    plant.sun_kw = 6.0
    assert plant.power_kw() == pytest.approx(0.96)
    plant.sun_kw = 0.0
    seen = preview(FRONIUS_CONTROL, inputs(Gen24(FRONIUS_CONTROL, dict(STATES)), T0, "grid_charge", 1.0, takeover=True))
    assert [(op.role, op.value) for op in seen.writes] == written(results)
    assert [(op.role, op.value) for op in seen.restores] == [
        ("discharge_limit_on", "off"), ("charge_limit_on", "off"), ("discharge_limit", 100.0), ("charge_limit", 100.0),
    ]
    # A lower rate: the limit that rises first (the force eases before the cap comes down), never a refused pair.
    now, results = run(executor, plant, now, 5, "grid_charge", 0.5)
    assert written(results) == [("discharge_limit", -8.0), ("charge_limit", 8.0)]
    assert plant.power_kw() == pytest.approx(0.48) and plant.refused == []
    # A higher one (after the dwell): the cap first.
    now, results = run(executor, plant, now, 400, "grid_charge", 2.0)
    assert written(results) == [("charge_limit", 33.0), ("discharge_limit", -33.0)]
    assert plant.power_kw() == pytest.approx(1.98) and plant.refused == []
    # Handed back: the switches first (the forced charge ends at once), then the limits as found.
    now, results = run(executor, plant, now, 10, "self_consume")
    assert written(results) == [
        ("discharge_limit_on", "off"), ("charge_limit_on", "off"), ("discharge_limit", 100.0), ("charge_limit", 100.0),
    ]
    assert plant.states == STATES and plant.refused == []


def test_a_discharge_to_the_house_caps_the_charge_at_0_and_stores_nothing() -> None:
    executor, plant = _plant(sun_kw=3.0)
    _now, results = run(executor, plant, T0, 6, "discharge_house", 2.0)
    assert written(results) == [("charge_limit", 0.0), ("charge_limit_on", True)]
    assert plant.power_kw() == 0.0, "a sun surplus is not stored"
    plant.sun_kw = 0.0
    assert plant.power_kw() == -2.0


def test_no_change_of_action_ever_asks_core_for_a_refused_pair() -> None:
    """Every action after every other, and back: the values are written before their switches and a rising limit
    before a falling one, so no moment has both on below 0 (core would refuse with a bare Modbus error)."""
    actions = (("hold", 0.0), ("grid_charge", 2.0), ("discharge_house", 2.0), ("grid_charge", 1.0))
    for first, second in itertools.permutations(actions, 2):
        if first[0] == second[0]:
            continue
        executor, plant = _plant()
        now, _results = run(executor, plant, T0, 8, *first)
        assert executor.state.action == first[0], (first, second)
        now, results = run(executor, plant, now, 10, *second)
        assert executor.state.action == second[0] and executor.state.phase == "active", (first, second)
        assert plant.refused == [], (first, second, written(results))
        now, _results = run(executor, plant, now, 10, "self_consume")
        assert plant.states == STATES and plant.refused == [], (first, second)


def test_a_negative_limit_found_behind_its_switch_is_handed_back_as_no_limit() -> None:
    found = STATES | {"discharge_limit": "-50"}
    executor, plant = Executor(FRONIUS_CONTROL), Gen24(FRONIUS_CONTROL, dict(found))
    now, results = run(executor, plant, T0, 6, "hold")
    assert events(results, "restore_replaced")[0]["reason"] == "discharge_limit"
    now, results = run(executor, plant, now, 6, "self_consume")
    assert plant.states["discharge_limit"] == "100" and plant.states["discharge_limit_on"] == "off"


def test_it_never_starts_over_a_limit_someone_else_left_on() -> None:
    for switch, why in (("charge_limit_on", "charge_limit_in_force"), ("discharge_limit_on", "discharge_limit_in_force")):
        executor, plant = _plant()
        plant.states[switch] = "on"
        _now, results = run(executor, plant, T0, 3, "hold")
        assert written(results) == [] and executor.state.phase == "idle"
        # Read as the inverter run by someone else, and refused by the recipe's own rule were it not.
        assert events(results, "not_started")[0]["reason"].startswith("self_managed:")
        free = Management("free", "self_consumption", {"charge limit": "off", "discharge limit": "off"})
        _now, results = run(executor, plant, T0, 3, "hold", management=free)
        assert written(results) == [] and events(results, "not_started")[0]["reason"] == why


def test_a_grid_charge_waits_for_grid_charging_which_it_never_sets() -> None:
    executor, plant = _plant()
    plant.states["grid_charging"] = "off"
    _now, results = run(executor, plant, T0, 3, "grid_charge", 1.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "grid_charging_off"
    executor, plant = _plant()
    plant.states["grid_charging"] = "off"
    _now, results = run(executor, plant, T0, 6, "hold")
    assert written(results) == [("discharge_limit", 0.0), ("discharge_limit_on", True)]
    assert all(op.role != "grid_charging" for op in plant.writes)


def test_the_share_is_of_the_rating_as_read_never_of_a_learnt_one() -> None:
    # A learnt rating (WChaMax unread) below the real one would ask for more than the rate.
    executor, plant = _plant(rated_kw=3.0, rating_source="learned")
    _now, results = run(executor, plant, T0, 3, "grid_charge", 1.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "rated_unknown"
    # A hold needs no rating.
    _now, results = run(executor, plant, T0, 4, "hold")
    assert written(results) == [("discharge_limit", 0.0), ("discharge_limit_on", True)]
    # The rating lost mid-charge: the next change of rate hands back instead of writing a share of nothing.
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 2.0)
    assert plant.states["discharge_limit"] == "-33"
    plant.rated_kw, plant.rating_source = 3.0, "learned"
    _now, results = run(executor, plant, now, 6, "grid_charge", 1.0)
    assert events(results, "hand_back")[0]["reason"] == "rated_unknown"
    assert all(not isinstance(value, str) or value in ("on", "off") for _role, value in written(results))
    assert plant.states["discharge_limit_on"] == "off"


def test_a_near_fuse_hands_back_in_one_look_and_a_holds_peak_is_covered_by_the_switch() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 1.0)
    _now, results = run(executor, plant, now, 1, "grid_charge", 1.0, headroom_a=0.5)
    assert written(results)[0] == ("discharge_limit_on", "off")
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "hold")
    _now, results = run(executor, plant, now, 2, "hold", headroom_a=1.2)
    assert written(results)[0] == ("discharge_limit_on", "off") and events(results, "peak_cover")


def test_a_charge_that_does_nothing_is_handed_back_and_left_as_found() -> None:
    # The web UI's "Battery charging from other sources" off cannot be read: the battery simply does not charge.
    executor, plant = _plant(obeys=False)
    _now, results = run(executor, plant, T0, 80, "grid_charge", 1.0)
    assert events(results, "hand_back")[0]["reason"] == "no_effect:grid_charge" and executor.state.phase == "blocked"
    assert plant.states == STATES


def test_the_rearm_after_a_hand_back_holds_for_fronius_too() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 1.0)
    now, _results = run(executor, plant, now, 6, "self_consume")
    _now, results = run(executor, plant, now, 2, "hold")
    assert written(results) == []
    _now, results = run(executor, plant, now + timedelta(seconds=REARM_S), 4, "hold")
    assert written(results)[0] == ("discharge_limit", 0.0) and replace(executor.state).action == "hold"


# --- the fuse with Home Assistant stopped ----------------------------------------------------------------------------


def test_a_charge_keeps_the_fuse_when_home_assistant_stops_all_on_one_phase() -> None:
    """Core's revert option ends a limit an hour after Home Assistant stops, if it is on: no lift either way, the cap
    takes the whole charge on the phase the house peaks."""
    peaks = {"L1": 6.0, "L2": 9.0, "L3": 4.0}
    cap = grid_charge_cap_kw(16.0, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                             plant_phases=FRONIUS_CONTROL.grid_charge_phases, house_phase_peaks_a=peaks)
    assert cap is not None and cap > 0
    executor, plant = _plant()
    run(executor, plant, T0, 6, "grid_charge", cap)
    charge_a = plant.power_kw() * 1000 / VOLTS
    assert charge_a <= cap * 1000 / VOLTS + 1e-9, "a share rounded down never charges above the rate"
    assert max(peaks.values()) + charge_a <= 16.0 - HEADROOM_MARGIN_A + 1e-9


# --- the check before control and the hardware test ------------------------------------------------------------------


def test_without_the_persons_acknowledgement_nothing_is_written() -> None:
    executor, plant = _plant()
    _now, results = run(executor, plant, T0, 3, "hold", takeover=False)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "strategy_unread"


def test_the_verification_runs_every_mode_and_leaves_the_battery_as_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import test_battery_verify  # noqa: PLC0415
    from tests.test_battery_verify import _outcomes, _verify  # noqa: PLC0415

    monkeypatch.setattr(test_battery_verify, "inputs", lambda *args, **kwargs: inputs(*args, takeover=True, **kwargs))

    for quick in (False, True):
        executor, plant = _plant()
        verification = _verify(executor, plant, quick=quick)
        assert verification.done, quick
        assert _outcomes(verification) == {
            "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"
        }, quick
        assert verification.report()["brand"] == "fronius"
        assert plant.states == STATES and plant.refused == [] and executor.state.phase == "idle", quick
    executor, plant = _plant(obeys=False)
    outcomes = _outcomes(_verify(executor, plant, looks=600))
    assert outcomes["hold"] in ("ignored", "aborted:no_effect:hold") and plant.states == STATES
