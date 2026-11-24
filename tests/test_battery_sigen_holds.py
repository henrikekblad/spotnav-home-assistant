"""Sigenergy's hold against the Modbus protocol's two generations: which limit each action writes in which Remote EMS
control mode, the mode-4 hold (`pv_first`, the only one since 2026-11-23) and its peak cover, the order of the limits
around a change of mode, a hold that charges from the grid, its surplus follow (off until tested on site), the
verification trying a brand's hold variants in order (on a fixture with two), and the hand-back."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.commands import (
    SIGENERGY_CONTROL,
    SIGENERGY_HOLD_SURPLUS,
    SIGENERGY_HOLDS,
    SIGENERGY_LIMIT_MODES,
    SIGENERGY_OUT_OF_SCOPE_V27,
    SIGENERGY_SURPLUS_TRACKING,
    control_for,
    table,
)
from custom_components.spotnav.battery.executor import (
    HOLD_CHARGE_S,
    MANAGEMENT_BLOCK_S,
    PEAK_RECOVER_S,
    ExecState,
    Executor,
    preview,
)
from custom_components.spotnav.battery.ledger import Ledger
from custom_components.spotnav.battery.monitor import Capability, Command, Monitor, judge
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S
from custom_components.spotnav.battery.verify import Sample, Verification
from tests.battery_control_fakes import (
    LIMIT_HOLD_CONTROL,
    SIGENERGY_STATES,
    T0,
    TICK,
    TWO_HOLD_CONTROL,
    FakeBattery,
    events,
    inputs,
    run,
    written,
)

PV_FIRST = "Command Charging (PV First)"
MSC = "Maximum Self Consumption"
PV_FIRST_CONTROL = control_for("sigenergy", "pv_first")
LIMITS = ("charge_limit", "discharge_limit")


def _looks(seconds: float) -> int:
    return int(seconds / TICK.total_seconds()) + 1


def _pv_first(**kwargs) -> tuple[Executor, FakeBattery]:
    assert PV_FIRST_CONTROL is not None
    return Executor(PV_FIRST_CONTROL), FakeBattery(PV_FIRST_CONTROL, dict(SIGENERGY_STATES), **kwargs)


def _held(executor: Executor, battery: FakeBattery) -> object:
    now, _results = run(executor, battery, T0, 6, "hold")
    assert executor.state.phase == "active" and executor.state.action == "hold"
    return now


# --- the protocol's scopes ---------------------------------------------------------------------------------------


def _writes() -> list[tuple[str, str, str]]:
    """(action or `hold:<variant>`, mode, limit) for every limit a Sigenergy command writes."""
    rows: list[tuple[str, str, str]] = []
    for action in ("grid_charge", "discharge_house", "export_discharge"):
        wanted = SIGENERGY_CONTROL.wanted(action, 4.0)
        rows += [(action, str(wanted["control_mode"]), name) for name in LIMITS if wanted[name] != "<snapshot>"]
    for variant in SIGENERGY_HOLDS:
        wanted = SIGENERGY_CONTROL.with_hold(variant.name).wanted("hold", 0.0)
        rows += [(f"hold:{variant.name}", str(wanted["control_mode"]), name) for name in LIMITS
                 if wanted[name] != "<snapshot>"]
    return rows


def test_which_limit_each_command_writes_in_which_mode_is_pinned_against_both_protocol_generations() -> None:
    """V2.7 plant parameters 23-25 (sig27.txt lines 1133-1149): 40032 acts in mode 3 or 4, 40034 in 5 or 6. V2.9
    Table 5-2-1 rows 1-3 (sig29.txt lines 1120-1138): both "globally regardless of the EMS operating mode"."""
    assert _writes() == [
        ("grid_charge", "Command Charging (Grid First)", "charge_limit"),
        ("discharge_house", MSC, "charge_limit"),
        ("export_discharge", "Command Discharging (PV First)", "discharge_limit"),
        ("hold:pv_first", PV_FIRST, "charge_limit"),
    ]
    v27, v29 = SIGENERGY_LIMIT_MODES["V2.7"], SIGENERGY_LIMIT_MODES["V2.9"]
    assert v27["charge_limit"] == {"Command Charging (Grid First)", PV_FIRST}
    assert v27["discharge_limit"] == {"Command Discharging (PV First)", "Command Discharging (ESS First)"}
    outside = set()
    for key, mode, limit in _writes():
        assert v29[limit] is None, "V2.9: every limit acts in every mode"
        if mode not in v27[limit]:
            outside.add((key, limit))
    # Only the discharge to the house leans on V2.9 (ignored under V2.7, it is self-consumption).
    assert outside == SIGENERGY_OUT_OF_SCOPE_V27
    # The hold keeps to both generations.
    assert ("hold:pv_first", "charge_limit") not in outside


def test_the_only_hold_is_pv_first_and_the_table_shows_it() -> None:
    # Owner decision 2026-11-23 (variant B): the Maximum Self Consumption hold failed the HA-down peak test.
    assert [variant.name for variant in SIGENERGY_HOLDS] == ["pv_first"]
    assert SIGENERGY_CONTROL is PV_FIRST_CONTROL and SIGENERGY_CONTROL.variant == "pv_first"
    assert control_for("sigenergy", "msc_no_discharge") is SIGENERGY_CONTROL, "a stored variant it no longer has"
    assert PV_FIRST_CONTROL.wanted("hold", 0.0) == {
        "charge_limit": 0.0, "discharge_limit": "<snapshot>", "remote_ems": True, "control_mode": PV_FIRST,
    }
    assert PV_FIRST_CONTROL.hold_idle()
    assert PV_FIRST_CONTROL.peak_first["hold"] == ("control_mode",)
    assert PV_FIRST_CONTROL.peak_first["grid_charge"] == ("control_mode",)
    row = next(row for row in table() if row["brand"] == "sigenergy" and row["action"] == "hold")
    assert set(row["variants"]) == {"pv_first"}
    assert row["variants"]["pv_first"]["control_mode"] == PV_FIRST


def test_the_mode_texts_are_typqxqs_select_options() -> None:
    # Appendix 6: 0x04 "Command charging (consume PV power first)" (sig27.txt lines 2180-2183, sig29.txt line 3346).
    """TypQxQ/Sigenergy-Local-Modbus custom_components/sigen/select.py lines 105-114 (options) and 116-124 (map to
    Appendix 6) at 94e38dae and 41182ec: 2, 3, 4, 5."""
    assert PV_FIRST_CONTROL.wanted("hold", 0.0)["control_mode"] == "Command Charging (PV First)"  # 4


# --- the mode-4 hold ---------------------------------------------------------------------------------------------


def test_a_pv_first_hold_writes_the_charge_limit_at_0_then_remote_ems_then_mode_4() -> None:
    executor, battery = _pv_first()
    _now, results = run(executor, battery, T0, 6, "hold")
    assert written(results) == [("charge_limit", 0.0), ("remote_ems", True), ("control_mode", PV_FIRST)]
    assert executor.state.variant == "pv_first"
    assert executor.state.snapshot == {"charge_limit": "unknown", "discharge_limit": "23.04"}
    assert battery.states["discharge_limit"] == "23.04", "the discharge limit is left as found"


def test_a_peak_under_a_pv_first_hold_writes_the_mode_back_to_msc_in_one_write() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    now, results = run(executor, battery, now, 3, "hold", headroom_a=1.2)
    assert written(results) == [("control_mode", MSC)]
    assert events(results, "peak_cover")[0]["roles"] == ["control_mode"]
    assert battery.states["remote_ems"] == "on" and executor.state.covered_at is not None
    # Not covered within the recover time: the whole hold is handed back.
    _now, results = run(executor, battery, now, _looks(PEAK_RECOVER_S) + 2, "hold", headroom_a=1.2)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert battery.states["remote_ems"] == "off" and battery.states["charge_limit"] == "10"
    assert executor.state.phase == "idle"


def test_a_pv_first_hold_under_the_hand_back_mark_writes_the_mode_first_and_the_raised_limit_once_it_read_back() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 3, "hold", headroom_a=0.5)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    # The mode and Remote EMS at once; the charge limit stays 0 until both read back, then goes back as the rating
    # (never set before).
    assert [(op.role, op.value) for op in results[0].ops] == [("control_mode", MSC), ("remote_ems", False)]
    assert written(results)[2:] == [("charge_limit", 10.0)]
    assert battery.states["remote_ems"] == "off" and battery.states["charge_limit"] == "10"


@pytest.mark.parametrize("how", ["raises", "drops"])
def test_a_fuse_hand_back_whose_remote_ems_write_is_lost_keeps_the_charge_limit_at_0(how: str) -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    getattr(battery, how).add("remote_ems")
    battery.register_40031 = "Command Charging (PV First)"
    battery.states["control_mode"] = "Command Charging (PV First)"
    getattr(battery, how).add("control_mode")
    now, results = run(executor, battery, now, 8, "hold", headroom_a=0.5)
    # Never raised while neither the mode nor Remote EMS reads back: Command Charging (PV First) at the full limit
    # would charge from the grid with the fuse at its closest.
    assert "charge_limit" not in [role for role, _value in written(results)] and battery.states["charge_limit"] == "0"
    # Once the writes are taken again, the hand-back goes on and the limit goes back last.
    getattr(battery, how).discard("remote_ems")
    getattr(battery, how).discard("control_mode")
    _now, results = run(executor, battery, now, _looks(300), "hold", headroom_a=0.5)
    assert battery.states["remote_ems"] == "off" and battery.states["charge_limit"] == "10"
    order = [role for role, _value in written(results)]
    assert order.index("charge_limit") > max(index for index, role in enumerate(order) if role != "charge_limit")


@pytest.mark.parametrize("how", ["raises", "drops"])
def test_a_fuse_hand_back_whose_mode_write_alone_is_lost_raises_the_limit_only_once_remote_ems_reads_off(
    how: str,
) -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    getattr(battery, how).add("control_mode")
    _now, results = run(executor, battery, now, 8, "hold", headroom_a=0.5)
    looks = [[op.role for op in result.ops] for result in results]
    # Never in the look that writes Remote EMS off: only once it reads off (the plant out of its Command modes, the
    # mode idle) does the limit go back.
    assert "charge_limit" not in looks[0] and any("charge_limit" in ops for ops in looks[1:])
    assert battery.states["remote_ems"] == "off" and battery.states["charge_limit"] == "10"


def test_a_pv_first_charge_limit_someone_else_changed_is_given_up_not_written_over() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    battery.states["charge_limit"] = "4"  # the app set its own limit
    _now, results = run(executor, battery, now, _looks(MIN_DIVERGENCE_S) + 6, "hold")
    assert events(results, "external")[0]["reason"] == "charge_limit"
    assert events(results, "hand_back")[0]["reason"].startswith("changed_outside_spotnav:charge_limit")
    assert "charge_limit" not in [role for role, _value in written(results)] and battery.states["charge_limit"] == "4"
    assert battery.states["remote_ems"] == "off" and executor.state.phase == "blocked"


def test_a_pv_first_limit_that_reads_unknown_after_its_write_never_has_unknown_written_back() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    battery.states["charge_limit"] = "unknown"  # the entity blinks after SpotNav's write
    _now, results = run(executor, battery, now, 12, "hold", stopping="stop")
    assert all(value != "unknown" for _role, value in written(results))
    assert battery.states["remote_ems"] == "off"


def test_a_pv_first_hold_is_handed_back_to_every_value_found_after_a_restart() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    stored = executor.as_dict()
    assert stored["state"]["variant"] == "pv_first"
    again = Executor(
        SIGENERGY_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        ExecState.from_dict(stored["state"]),
    )
    again.restart(now)
    _now, results = run(again, battery, now, 12, "hold", stopping="stop")
    assert written(results)[0] == ("control_mode", MSC), "the mode first: the battery covers a peak again"
    # A never-set limit SpotNav wrote goes back as the rating; everything else as found, Remote EMS off.
    assert battery.states == dict(SIGENERGY_STATES) | {"charge_limit": "10"}
    assert again.state.phase == "idle" and not again.state.holds_snapshot


def test_a_hold_stored_by_the_retired_msc_hold_is_handed_back_to_every_value_found_after_a_restart() -> None:
    # A hold running when the update came: Maximum Self Consumption with the discharge limit at 0, stored with its
    # variant's name, which the recipe no longer has.
    executor, battery = Executor(LIMIT_HOLD_CONTROL), FakeBattery(LIMIT_HOLD_CONTROL, dict(SIGENERGY_STATES))
    now = _held(executor, battery)
    assert battery.states["discharge_limit"] == "0"
    stored = executor.as_dict()
    stored["state"]["variant"] = "msc_no_discharge"
    again = Executor(
        SIGENERGY_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        ExecState.from_dict(stored["state"]),
    )
    assert again.control.variant == "pv_first"
    again.restart(now)
    _now, _results = run(again, battery, now, 12, "hold", stopping="stop")
    assert battery.states == dict(SIGENERGY_STATES)
    assert again.state.phase == "idle" and not again.state.holds_snapshot


def test_the_preview_of_a_pv_first_hold_names_its_writes_and_hand_back() -> None:
    _executor, battery = _pv_first()
    seen = preview(PV_FIRST_CONTROL, inputs(battery, T0, "hold"))
    assert [(op.role, op.value) for op in seen.writes] == [
        ("charge_limit", 0.0), ("remote_ems", True), ("control_mode", PV_FIRST)
    ]
    assert [op.role for op in seen.restores][0] == "control_mode"


# --- leaving a grid charge -------------------------------------------------------------------------------------


def test_a_grid_charge_that_turns_into_a_hold_lowers_the_charge_limit_before_the_mode() -> None:
    executor, battery = _pv_first()
    now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0, grid_import_kw=4.0)
    assert executor.state.phase == "active" and battery.states["control_mode"] == "Command Charging (Grid First)"
    _now, results = run(executor, battery, now, 8, "hold", grid_import_kw=4.0)
    # The charge stops first; never the new mode on the charge's limit.
    assert written(results) == [("charge_limit", 0.0), ("control_mode", PV_FIRST)]


def test_a_grid_charge_that_turns_into_an_export_discharge_raises_the_charge_limit_only_after_the_mode() -> None:
    executor, battery = Executor(SIGENERGY_CONTROL), FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0)
    _now, results = run(executor, battery, now, 8, "export_discharge", 2.0)
    # The discharge limit falls to the sale's rate first (it does nothing in Command Charging), then the mode, then the
    # charge limit back at the rating once the plant no longer charges by it.
    assert written(results) == [
        ("discharge_limit", 2.0), ("control_mode", "Command Discharging (PV First)"), ("charge_limit", 10.0)
    ]


def test_an_export_discharge_that_turns_into_a_hold_raises_the_discharge_limit_only_after_the_mode() -> None:
    executor, battery = Executor(SIGENERGY_CONTROL), FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 8, "export_discharge", 2.0)
    assert battery.states["control_mode"] == "Command Discharging (PV First)"
    _now, results = run(executor, battery, now, 8, "hold")
    # Never Command Discharging (PV First) at the discharge limit found (a sale at full power), never Command Charging
    # (PV First) at the charge limit's rating.
    assert written(results) == [("charge_limit", 0.0), ("control_mode", PV_FIRST), ("discharge_limit", 23.04)]


def test_a_grid_charge_handed_back_to_self_consumption_writes_the_mode_first() -> None:
    for control in (SIGENERGY_CONTROL, PV_FIRST_CONTROL):
        executor, battery = Executor(control), FakeBattery(control, dict(SIGENERGY_STATES))
        now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0)
        _now, results = run(executor, battery, now, 8, "self_consume")
        assert written(results) == [("control_mode", MSC), ("remote_ems", False), ("charge_limit", 10.0)]
        assert battery.states == dict(SIGENERGY_STATES) | {"charge_limit": "10"}


def test_a_hold_that_turns_into_a_grid_charge_raises_the_charge_limit_after_the_mode() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 8, "grid_charge", 3.0)
    assert written(results) == [("control_mode", "Command Charging (Grid First)"), ("charge_limit", 3.0)]


# --- an idle hold never charges --------------------------------------------------------------------------------


@dataclass
class ForcedModeFour(FakeBattery):
    """Mode 4 as haeo and Predbat read it: a charge at the charge limit, from the grid for what the sun does not give
    (the discharge limit does nothing in Maximum Self Consumption, as V2.7 says)."""

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if self.states.get("remote_ems") == "on" and self.states.get("control_mode") == PV_FIRST:
            limit = self.states.get("charge_limit")
            return 10.0 if limit in ("unknown", None) else float(limit)
        return self.idle_kw


@dataclass
class ChargingHold(FakeBattery):
    """A plant whose mode 4 charges whatever its charge limit says."""

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if action == "hold" and self.states.get("control_mode") == PV_FIRST:
            return 3.0
        return super().power_kw(action, rate_kw)


@pytest.mark.parametrize("grid_kw", [None, 4.0, -2.0])
def test_an_idle_hold_that_charges_is_handed_back_within_a_minute_whatever_the_grid_reads(grid_kw) -> None:
    executor = Executor(PV_FIRST_CONTROL)
    battery = ChargingHold(PV_FIRST_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 4, "hold", grid_import_kw=grid_kw)
    now, results = run(executor, battery, now, _looks(HOLD_CHARGE_S) - 2, "hold", grid_import_kw=grid_kw)
    assert events(results, "hand_back") == []
    _now, results = run(executor, battery, now, 8, "hold", grid_import_kw=grid_kw)
    assert events(results, "hand_back")[0]["reason"] == "not_obeyed:hold:charging"
    assert executor.state.phase == "blocked" and battery.states["remote_ems"] == "off"


def test_a_forced_mode_4_at_a_charge_limit_of_0_stands_idle_and_never_charges() -> None:
    executor = Executor(PV_FIRST_CONTROL)
    # 6 A of headroom on a 20 A fuse: a mode 4 that charged at the rating would take the site over it.
    battery = ForcedModeFour(PV_FIRST_CONTROL, dict(SIGENERGY_STATES), idle_kw=-0.5)
    now, results = run(executor, battery, T0, 6, "hold", grid_import_kw=None, headroom_a=6.0)
    # The limit is at 0 before Remote EMS and mode 4: no look ever sees mode 4 at another limit.
    roles = [role for role, _value in written(results)]
    assert roles.index("charge_limit") < roles.index("remote_ems") < roles.index("control_mode")
    assert battery.power_kw("hold", 0.0) == 0.0
    _now, results = run(executor, battery, now, _looks(4 * HOLD_CHARGE_S), "hold", grid_import_kw=None, headroom_a=6.0)
    assert events(results, "hand_back") == [] and executor.state.phase == "active"


def test_a_short_charge_under_an_idle_hold_is_no_reason_to_hand_back() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    now, _ = run(executor, battery, now, 3, "hold", power_kw=2.0)
    now, _ = run(executor, battery, now, 2, "hold", power_kw=0.0)
    _now, results = run(executor, battery, now, 4, "hold", power_kw=2.0)
    assert events(results, "hand_back") == [] and executor.state.phase == "active"


def test_a_hold_that_stores_the_sun_is_never_handed_back_for_charging_even_while_a_car_imports() -> None:
    executor = Executor(LIMIT_HOLD_CONTROL)
    battery = FakeBattery(LIMIT_HOLD_CONTROL, dict(SIGENERGY_STATES))
    now = _held(executor, battery)
    # The sun charges the battery 3 kW while a car draws 11 kW from the grid.
    _now, results = run(executor, battery, now, _looks(4 * HOLD_CHARGE_S), "hold", power_kw=3.0, grid_import_kw=11.0)
    assert events(results, "hand_back") == [] and executor.state.phase == "active"


def test_the_block_of_an_idle_hold_that_charged_clears_by_itself() -> None:
    executor = Executor(PV_FIRST_CONTROL)
    battery = ChargingHold(PV_FIRST_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, _looks(HOLD_CHARGE_S) + 12, "hold")
    assert executor.state.phase == "blocked" and executor.state.blocked == "not_obeyed:hold:charging"
    _now, results = run(executor, battery, now, 1, "hold")
    assert events(results, "unblocked") == [] and executor.state.phase == "blocked"
    _now, results = run(executor, battery, now + timedelta(seconds=MANAGEMENT_BLOCK_S), 1, "self_consume")
    assert events(results, "unblocked")[0]["reason"] == "not_obeyed:hold:charging"
    assert executor.state.phase == "idle"


def test_the_monitor_judges_an_idle_hold_on_the_batterys_own_power() -> None:
    idle = Command("hold", T0, 0.0, before_kw=-3.0, written=True, idle=True)
    assert judge(idle, 0.0, 0.0, None, 50.0) == "ok"
    assert judge(idle, 0.0, 3.0, None, 50.0) == "no_effect"  # grid unknown: a charge is still a hold not taken
    assert judge(idle, 0.0, 3.0, -1.0, 50.0) == "no_effect"  # even from the sun: the battery is to stand idle
    assert judge(idle, 0.0, 0.0, -2.0, 50.0) == "ok"  # the sun is exported: what an idle hold does
    assert judge(idle, 0.0, -3.0, 1.0, 50.0) == "no_effect"  # still discharging
    assert Command.from_dict(idle.as_dict()) == idle


def test_the_monitor_judges_a_hold_that_stores_the_sun() -> None:
    hold = Command("hold", T0, 0.0, before_kw=-3.0, written=True)
    assert judge(hold, 0.0, 0.0, 1.0, 50.0) == "ok"  # the grid covers the house
    assert judge(hold, 0.0, 3.0, 4.0, 50.0) == "ok"  # Maximum Self Consumption never charges from the grid
    assert judge(hold, 0.0, 2.0, -1.0, 50.0) == "ok"  # the sun charges it
    assert judge(hold, 0.0, 0.0, -2.0, 50.0) == "partial"  # the sun is exported, not stored
    assert judge(hold, 0.0, 0.0, -2.0, 95.0) == "ok"  # nearly full: nothing to store
    assert judge(hold, 0.0, -3.0, 1.0, 50.0) == "no_effect"  # still discharging
    assert judge(hold, 0.0, 0.0, None, 50.0) == "ok"  # grid unknown: the battery's own power, no discharge


# --- the verification tries the variants in order (a brand with two holds) ------------------------------------


@dataclass
class OnlyModeFour(FakeBattery):
    """Firmware that follows V2.7: the discharge limit does nothing in Maximum Self Consumption."""

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if action == "hold" and self.states.get("control_mode") != PV_FIRST:
            return self.idle_kw
        return super().power_kw(action, rate_kw)


def _verify(executor: Executor, battery: FakeBattery, *, looks: int = 900, grid_kw: float | None = 1.0,
            tried: tuple = (), modes: tuple[str, ...] | None = ("hold",)) -> Verification:
    now = T0
    verification = Verification(executor, now, firmware="V100R001C21SPC117", modes=modes, tried=tried)
    failed: tuple[str, ...] = ()
    capability = Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0)
    for _ in range(looks):
        power = battery.power_kw(battery.action, battery.rate_kw)
        sample = Sample(power, 5.0, battery.soc_pct, 2.0, grid_kw)
        action, rate = verification.command(now, sample, 10.0)
        result = executor.step(inputs(battery, now, action, rate, failed=failed, house_deficit_kw=2.0,
                                      grid_import_kw=grid_kw))
        failed = tuple(op.role for op in result.ops if not battery.apply(op))
        state = executor.state
        battery.action = state.action if state.phase == "active" else None
        battery.rate_kw = state.rate_kw
        verification.after(now, result, sample, capability)
        now += TICK
        if verification.done and state.phase in ("idle", "blocked") and not result.ops:
            break
    return verification


def test_a_plant_that_ignores_the_limit_hold_passes_with_the_mode_4_hold_and_control_uses_it() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = OnlyModeFour(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES))
    verification = _verify(executor, battery)
    assert verification.done
    hold = verification.report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "pv_first"
    assert [row["variant"] for row in hold["variants"]] == ["limit_hold", "pv_first"]
    assert hold["variants"][0]["outcome"] == "ignored"
    assert verification.report()["modes"]["normal"]["outcome"] == "restored"
    # Never both at once: every hold write of the first variant was handed back before mode 4 was written.
    modes = [op.value for op in battery.writes if op.role == "control_mode"]
    assert modes.index(PV_FIRST) > modes.index(MSC)
    assert battery.states == dict(SIGENERGY_STATES) | {"charge_limit": "10"}
    assert gate.hold_variant({"modes": {"hold": hold}}) == "pv_first"


@pytest.mark.parametrize("grid_kw", [None, 2.0])
def test_a_plant_whose_mode_4_is_a_forced_charge_passes_the_mode_4_hold_idle_with_or_without_a_grid_reading(
    grid_kw,
) -> None:
    executor = Executor(SIGENERGY_CONTROL)
    battery = ForcedModeFour(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    charged: list[float] = []
    original = battery.power_kw

    def power_kw(action: str | None, rate_kw: float) -> float:
        power = original(action, rate_kw)
        charged.append(power)
        return power

    battery.power_kw = power_kw  # type: ignore[method-assign]
    hold = _verify(executor, battery, grid_kw=grid_kw).report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "pv_first"
    assert max(charged) <= 0.0, "mode 4 never charged: its limit was at 0 before Remote EMS and the mode"
    assert executor.state.phase == "idle" and battery.states["remote_ems"] == "off"


def test_a_plant_that_obeys_the_limit_hold_never_tries_mode_4() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = FakeBattery(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES))
    hold = _verify(executor, battery).report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "limit_hold"
    assert [row["variant"] for row in hold["variants"]] == ["limit_hold"]
    assert PV_FIRST not in [op.value for op in battery.writes]
    assert gate.hold_variant({"modes": {"hold": hold}}) == "limit_hold"


def test_a_mode_4_hold_that_charges_fails_and_hold_fails_with_both_variants_recorded() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = _GridChargingV27(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES))
    hold = _verify(executor, battery, grid_kw=4.0).report()["modes"]["hold"]
    assert gate.mode_verdict(hold["outcome"]) == "fail"
    assert [row["variant"] for row in hold["variants"]] == ["limit_hold", "pv_first"]
    assert hold["variants"][1]["outcome"] == "aborted:not_obeyed:hold:charging"
    assert gate.hold_variant({"modes": {"hold": hold}}) is None
    assert executor.state.phase == "blocked" and not executor.state.holds_snapshot
    assert battery.states["remote_ems"] == "off"


@dataclass
class _GridChargingV27(FakeBattery):
    """V2.7 firmware whose mode 4 is a grid charge: neither hold works."""

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if action == "hold":
            return 3.0 if self.states.get("control_mode") == PV_FIRST else self.idle_kw
        return super().power_kw(action, rate_kw)


def test_a_later_run_starts_after_the_variants_that_failed() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = OnlyModeFour(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES))
    tried = ({"variant": "limit_hold", "outcome": "ignored"},)
    hold = _verify(executor, battery, tried=tried).report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "pv_first"
    assert [row["variant"] for row in hold["variants"]] == ["limit_hold", "pv_first"]
    assert MSC not in [op.value for op in battery.writes if op.role == "control_mode"][:1]
    report = {"modes": {"hold": {"outcome": "not_judgeable:battery_not_discharging", "variant": "pv_first",
                                 "variants": list(tried)}}}
    assert gate.variants_tried(report) == tried and gate.hold_variant(report) is None


@dataclass
class HalfHeld(FakeBattery):
    """A plant that halves its discharge under the first hold: neither ignored nor obeyed."""

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if action == "hold" and self.states.get("control_mode") != PV_FIRST:
            return -1.5
        return super().power_kw(action, rate_kw)


def test_a_partial_hold_is_not_given_the_next_variant() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = HalfHeld(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES))
    hold = _verify(executor, battery).report()["modes"]["hold"]
    assert hold["outcome"] == "partial" and hold["variants"] == [{"variant": "limit_hold", "outcome": "partial"}]
    assert PV_FIRST not in [op.value for op in battery.writes]


def test_a_hold_write_not_taken_is_not_given_the_next_variant() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = FakeBattery(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES), drops={"discharge_limit"})
    hold = _verify(executor, battery).report()["modes"]["hold"]
    assert hold["outcome"].startswith("aborted:write_not_taken:discharge_limit")
    assert [row["variant"] for row in hold["variants"]] == ["limit_hold"]
    assert PV_FIRST not in [op.value for op in battery.writes]
    assert battery.states["remote_ems"] == "off"


@dataclass
class SlowToDischarge(OnlyModeFour):
    """After the first hold is handed back the battery stays idle for `idle_looks` looks (the house draws less, the
    plant ramps slowly) before it discharges again."""

    idle_looks: int = 10**6
    seen_hold: bool = False
    looks_since: int = 0

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if action == "hold":
            self.seen_hold = True
        elif self.seen_hold and action is None:
            self.looks_since += 1
            if self.looks_since <= self.idle_looks:
                return 0.0
        return super().power_kw(action, rate_kw)


def test_the_next_variant_waits_for_the_battery_to_discharge_again() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = SlowToDischarge(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES), idle_looks=12)
    verification = _verify(executor, battery)
    hold = verification.report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "pv_first"
    assert hold["before_kw"] <= -1.0, "mode 4 was judged against a discharge it stopped"


def test_a_battery_that_does_not_discharge_again_leaves_the_next_variant_for_a_later_run() -> None:
    executor = Executor(TWO_HOLD_CONTROL)
    battery = SlowToDischarge(TWO_HOLD_CONTROL, dict(SIGENERGY_STATES))
    report = _verify(executor, battery).report()
    hold = report["modes"]["hold"]
    assert hold["outcome"] == "not_judgeable:battery_not_discharging" and hold["variant"] == "pv_first"
    assert hold["variants"] == [{"variant": "limit_hold", "outcome": "ignored"}]
    assert gate.mode_verdict(hold["outcome"]) == "pending" and gate.hold_variant(report) is None
    assert PV_FIRST not in [op.value for op in battery.writes]
    assert report["modes"]["normal"]["outcome"] == "restored"
    assert battery.states == dict(SIGENERGY_STATES)


def test_the_variant_to_run_is_the_one_that_passed_else_the_first_not_failed() -> None:
    names = ("limit_hold", "pv_first")

    def report(outcome: str, variant: str, *rows: tuple[str, str]) -> dict:
        variants = [{"variant": name, "outcome": result} for name, result in rows]
        return {"modes": {"hold": {"outcome": outcome, "variant": variant, "variants": variants}}}

    assert gate.hold_variant_to_run(None, names) == "limit_hold"
    assert gate.hold_variant_to_run(report("obeyed", "pv_first", ("limit_hold", "ignored"),
                                           ("pv_first", "obeyed")), names) == "pv_first"
    assert gate.hold_variant_to_run(report("not_judgeable:battery_not_discharging", "pv_first",
                                           ("limit_hold", "ignored")), names) == "pv_first"
    assert gate.hold_variant_to_run(report("partial", "pv_first", ("limit_hold", "ignored"),
                                           ("pv_first", "partial")), names) is None
    assert gate.hold_variant_to_run(None, ()) is None


# --- a mode write lost in a fuse hand-back ----------------------------------------------------------------------


def _grid_charge_lost_mode() -> tuple[Executor, FakeBattery, object]:
    """A grid charge handed back for the fuse whose mode write the plant dropped: 40031 still holds mode 3."""
    executor, battery = Executor(SIGENERGY_CONTROL), FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0)
    battery.drops.add("control_mode")
    now, results = run(executor, battery, now, 40, "grid_charge", 3.0, headroom_a=0.5)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert executor.state.phase == "idle" and battery.states["remote_ems"] == "off"
    assert battery.register_40031 == "Command Charging (Grid First)"
    battery.drops.clear()
    return executor, battery, now


def test_a_mode_write_lost_in_a_fuse_hand_back_is_remembered_over_a_restart() -> None:
    executor, _battery, _now = _grid_charge_lost_mode()
    assert executor.state.unsure == ("control_mode",)
    assert ExecState.from_dict(executor.state.as_dict()).unsure == ("control_mode",)


def test_the_next_enable_after_a_lost_mode_write_sets_both_limits_to_0_before_the_switch() -> None:
    executor, battery, now = _grid_charge_lost_mode()
    now, results = run(executor, battery, now + timedelta(minutes=5), 12, "hold")
    roles = [role for role, _value in written(results)]
    assert written(results)[:4] == [
        ("charge_limit", 0.0), ("discharge_limit", 0.0), ("remote_ems", True), ("control_mode", PV_FIRST)
    ]
    # Then the hold's own values: the charge limit stays at 0, the discharge limit back as found.
    assert written(results)[4:] == [("discharge_limit", 23.04)]
    assert roles.index("remote_ems") > roles.index("discharge_limit")
    assert events(results, "primed") and executor.state.unsure == () and not executor.state.priming
    assert battery.states == dict(SIGENERGY_STATES) | {
        "remote_ems": "on", "control_mode": PV_FIRST, "charge_limit": "0", "discharge_limit": "23.04"
    }


def test_a_hand_back_that_read_its_mode_back_primes_nothing() -> None:
    executor, battery = Executor(SIGENERGY_CONTROL), FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0)
    now, _ = run(executor, battery, now, 12, "self_consume")
    assert executor.state.phase == "idle" and executor.state.unsure == ()
    _now, results = run(executor, battery, now + timedelta(minutes=5), 8, "hold")
    # 40031 kept the Maximum Self Consumption the hand-back wrote: no prime, the hold's own writes only.
    assert written(results) == [("charge_limit", 0.0), ("remote_ems", True), ("control_mode", PV_FIRST)]
    assert not events(results, "primed") and battery.states["control_mode"] == PV_FIRST


# --- the surplus follow (variant B; off until tested on site) ---------------------------------------------------

_FOLLOWING = replace(SIGENERGY_HOLDS[0], surplus=SIGENERGY_HOLD_SURPLUS)
#: The shipping recipe with its surplus follow switched on (`SIGENERGY_SURPLUS_TRACKING`).
SURPLUS_CONTROL = replace(SIGENERGY_CONTROL, hold_variants=(_FOLLOWING,))


@dataclass
class SunPlant(FakeBattery):
    """Mode 4 in sunshine: the battery charges at its charge limit, from the sun first, the grid for the rest."""

    pv_kw: float = 0.0
    house_kw: float = 1.0

    def charge_kw(self) -> float:
        if self.states.get("remote_ems") == "on" and self.states.get("control_mode") == PV_FIRST:
            limit = self.states.get("charge_limit")
            return 0.0 if limit in ("unknown", None) else float(limit)
        return 0.0

    def grid_kw(self) -> float:
        return self.house_kw + self.charge_kw() - self.pv_kw


def _sun(executor: Executor, plant: SunPlant, start, looks: int, *, cap: float = math.inf, **overrides):
    now, results, failed = start, [], ()
    for _ in range(looks):
        look = inputs(plant, now, "hold", power_kw=plant.charge_kw(), grid_import_kw=plant.grid_kw(),
                      hold_charge_cap_kw=cap, failed=failed, **overrides)
        result = executor.step(look)
        results.append(result)
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        now += TICK
    return now, results


def _limits(results) -> list[float]:
    return [value for role, value in written(results) if role == "charge_limit"]


def test_the_surplus_follow_is_off_until_its_on_site_test_passed_and_the_hold_exports_the_sun() -> None:
    assert SIGENERGY_SURPLUS_TRACKING is False
    assert SIGENERGY_CONTROL.hold_surplus is None and SIGENERGY_CONTROL.hold_idle()
    assert SURPLUS_CONTROL.hold_surplus == SIGENERGY_HOLD_SURPLUS and not SURPLUS_CONTROL.hold_idle()
    executor = Executor(SIGENERGY_CONTROL)
    plant = SunPlant(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), pv_kw=4.0)
    _now, results = _sun(executor, plant, T0, 30)
    assert _limits(results) == [0.0] and not events(results, "surplus")
    assert plant.grid_kw() == -3.0, "the sun is exported"
    assert executor.state.phase == "active"


def test_with_the_follow_on_the_hold_stores_the_surplus_and_takes_nothing_from_the_grid() -> None:
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=4.0)
    _now, results = _sun(executor, plant, T0, _looks(4 * HOLD_CHARGE_S))
    assert _limits(results) == [0.0, 3.0]
    assert events(results, "surplus")[0]["kw"] == 3.0
    assert plant.grid_kw() == 0.0 and plant.charge_kw() == 3.0
    assert not events(results, "hand_back") and executor.state.phase == "active"


def test_the_follow_keeps_its_deadband_raises_at_most_once_a_minute_and_falls_at_once() -> None:
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=4.0)
    now, _results = _sun(executor, plant, T0, 8)
    assert plant.states["charge_limit"] == "3"
    # 0.3 kW more: inside the deadband.
    plant.pv_kw = 4.3
    now, results = _sun(executor, plant, now, 12)
    assert _limits(results) == []
    # A cloud: the limit falls to the surplus at the next look.
    plant.pv_kw = 2.0
    now, results = _sun(executor, plant, now, 1)
    assert _limits(results) == [1.0], "a fall is written at once"
    plant.pv_kw = 4.0
    now, results = _sun(executor, plant, now, 12)
    assert _limits(results) == [3.0]
    # The sun goes: under the deadband the limit is 0, at once.
    plant.pv_kw = 1.2
    _now, results = _sun(executor, plant, now, 2)
    assert _limits(results) == [0.0] and plant.grid_kw() == pytest.approx(-0.2)
    assert SIGENERGY_HOLD_SURPLUS.deadband_kw == 0.4


def test_a_raise_waits_for_the_gap_after_the_last_raise() -> None:
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=2.0)
    now, results = _sun(executor, plant, T0, 8)
    assert _limits(results) == [0.0, 1.0]
    first = datetime.fromisoformat(events(results, "surplus")[0]["at"])
    plant.pv_kw = 4.0
    _now, results = _sun(executor, plant, now, 12)
    second = datetime.fromisoformat(events(results, "surplus")[0]["at"])
    assert SIGENERGY_HOLD_SURPLUS.raise_gap_s == 60.0
    assert (second - first).total_seconds() >= SIGENERGY_HOLD_SURPLUS.raise_gap_s > (now - first).total_seconds()


def test_the_follow_never_leaves_more_than_the_ha_down_cap_standing() -> None:
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=4.0)
    _now, results = _sun(executor, plant, T0, 12, cap=1.5)
    assert _limits(results) == [0.0, 1.5]
    # No cap given (the shell's default): no follow at all.
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=4.0)
    _now, results = _sun(executor, plant, T0, 12, cap=0.0)
    assert _limits(results) == [0.0]


def test_the_follow_goes_to_0_without_the_grid_reading_and_once_the_days_writes_run_low() -> None:
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=4.0)
    now, _results = _sun(executor, plant, T0, 8)
    assert plant.states["charge_limit"] == "3"
    look = inputs(plant, now, "hold", power_kw=3.0, grid_import_kw=None, hold_charge_cap_kw=math.inf)
    result = executor.step(look)
    assert [(op.role, op.value) for op in result.ops] == [("charge_limit", 0.0)]
    plant.apply(result.ops[0])
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=4.0)
    now, _results = _sun(executor, plant, T0, 8)
    executor.state = replace(executor.state, writes_today=executor._daily_cap - executor._charge_reserve)
    _now, results = _sun(executor, plant, now, 4)
    assert _limits(results) == [0.0]


def test_no_follow_under_a_peak_cover() -> None:
    executor = Executor(SURPLUS_CONTROL)
    plant = SunPlant(SURPLUS_CONTROL, dict(SIGENERGY_STATES), pv_kw=1.0)
    now, _results = _sun(executor, plant, T0, 8)
    now, results = _sun(executor, plant, now, 2, headroom_a=1.2)
    assert events(results, "peak_cover") and executor.state.covered_at is not None
    plant.pv_kw = 5.0
    _now, results = _sun(executor, plant, now, 2, headroom_a=1.2)
    assert not events(results, "surplus")


def test_a_following_hold_that_charges_from_the_grid_is_still_handed_back() -> None:
    executor = Executor(SURPLUS_CONTROL)
    battery = FakeBattery(SURPLUS_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 6, "hold", hold_charge_cap_kw=math.inf, grid_import_kw=0.5)
    _now, results = run(executor, battery, now, _looks(HOLD_CHARGE_S) + 4, "hold", hold_charge_cap_kw=math.inf,
                        power_kw=2.0, grid_import_kw=2.0)
    assert events(results, "hand_back")[0]["reason"] == "not_obeyed:hold:charging"
