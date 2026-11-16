"""Sigenergy's two holds against the Modbus protocol's two generations: which limit each action writes in which
Remote EMS control mode, the mode-4 hold (`pv_first`) and its peak cover, a hold that charges from the grid, the
verification trying the variants in order, and the hand-back after either."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest

from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.commands import (
    SIGENERGY_CONTROL,
    SIGENERGY_HOLDS,
    SIGENERGY_LIMIT_MODES,
    SIGENERGY_OUT_OF_SCOPE_V27,
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
from tests.battery_control_fakes import SIGENERGY_STATES, T0, TICK, FakeBattery, events, inputs, run, written

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
        ("hold:msc_no_discharge", MSC, "discharge_limit"),
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
    # Only these two lean on V2.9: the first hold (its verification falls to pv_first where they are ignored) and the
    # discharge to the house (ignored, it is self-consumption).
    assert outside == SIGENERGY_OUT_OF_SCOPE_V27
    # Each generation has a hold within its scope: pv_first for V2.7, msc_no_discharge for V2.9.
    assert ("hold:pv_first", "charge_limit") not in outside


def test_the_hold_variants_are_tried_msc_first_and_the_table_shows_both() -> None:
    assert [variant.name for variant in SIGENERGY_HOLDS] == ["msc_no_discharge", "pv_first"]
    assert SIGENERGY_CONTROL.variant == "msc_no_discharge" and control_for("sigenergy").variant == "msc_no_discharge"
    assert control_for("sigenergy", "unknown").variant == "msc_no_discharge"
    assert PV_FIRST_CONTROL.wanted("hold", 0.0) == {
        "charge_limit": 0.0, "discharge_limit": "<snapshot>", "remote_ems": True, "control_mode": PV_FIRST,
    }
    assert PV_FIRST_CONTROL.hold_idle() and not SIGENERGY_CONTROL.hold_idle()
    assert PV_FIRST_CONTROL.peak_first["hold"] == ("control_mode",)
    assert SIGENERGY_CONTROL.peak_first["hold"] == ("discharge_limit",)
    # The grid charge's peak cover is the mode either way.
    assert PV_FIRST_CONTROL.peak_first["grid_charge"] == ("control_mode",)
    row = next(row for row in table() if row["brand"] == "sigenergy" and row["action"] == "hold")
    assert set(row["variants"]) == {"msc_no_discharge", "pv_first"}
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


def test_a_pv_first_hold_under_the_hand_back_mark_writes_every_role_in_one_look_mode_first() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 1, "hold", headroom_a=0.5)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    # The never-set charge limit goes back as the rating.
    assert [(op.role, op.value) for op in results[0].ops] == [
        ("control_mode", MSC), ("remote_ems", False), ("charge_limit", 10.0)
    ]


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


@pytest.mark.parametrize("variant", ["msc_no_discharge", "pv_first"])
def test_either_variant_is_handed_back_to_every_value_found_and_a_restart_uses_the_stored_variant(variant) -> None:
    control = control_for("sigenergy", variant)
    executor, battery = Executor(control), FakeBattery(control, dict(SIGENERGY_STATES))
    now = _held(executor, battery)
    stored = executor.as_dict()
    assert stored["state"]["variant"] == variant
    # Restored with the brand's default control: the stored variant decides the order.
    again = Executor(
        SIGENERGY_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        ExecState.from_dict(stored["state"]),
    )
    assert again.control.variant == variant
    again.restart(now)
    _now, results = run(again, battery, now, 12, "hold", stopping="stop")
    first = written(results)[0][0]
    assert first == ("discharge_limit" if variant == "msc_no_discharge" else "control_mode")
    # A never-set limit SpotNav wrote goes back as the rating; everything else as found, Remote EMS off.
    expected = dict(SIGENERGY_STATES)
    if variant == "pv_first":
        expected["charge_limit"] = "10"
    assert battery.states == expected
    assert again.state.phase == "idle" and not again.state.holds_snapshot


def test_the_variant_changes_only_while_nothing_is_held() -> None:
    executor, battery = Executor(SIGENERGY_CONTROL), FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now = _held(executor, battery)
    assert executor.use_variant("pv_first") is False and executor.control.variant == "msc_no_discharge"
    run(executor, battery, now, 10, "hold", stopping="stop")
    assert executor.use_variant("pv_first") is True and executor.state.variant == "pv_first"
    assert executor.use_variant("pv_first") is True


def test_the_preview_of_a_pv_first_hold_names_its_writes_and_hand_back() -> None:
    _executor, battery = _pv_first()
    seen = preview(PV_FIRST_CONTROL, inputs(battery, T0, "hold"))
    assert [(op.role, op.value) for op in seen.writes] == [
        ("charge_limit", 0.0), ("remote_ems", True), ("control_mode", PV_FIRST)
    ]
    assert [op.role for op in seen.restores][0] == "control_mode"


# --- leaving a grid charge -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("variant", "then"), [
    ("msc_no_discharge", [("control_mode", MSC), ("charge_limit", 10.0), ("discharge_limit", 0.0)]),
    ("pv_first", [("control_mode", PV_FIRST), ("charge_limit", 0.0)]),
])
def test_a_grid_charge_that_turns_into_a_hold_writes_the_mode_before_any_limit(variant, then) -> None:
    control = control_for("sigenergy", variant)
    executor, battery = Executor(control), FakeBattery(control, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0, grid_import_kw=4.0)
    assert executor.state.phase == "active" and battery.states["control_mode"] == "Command Charging (Grid First)"
    _now, results = run(executor, battery, now, 8, "hold", grid_import_kw=4.0)
    # Never the charge limit back at the rating while the plant is still in Command Charging (Grid First).
    assert written(results) == then


def test_a_grid_charge_that_turns_into_an_export_discharge_writes_the_mode_first() -> None:
    executor, battery = Executor(SIGENERGY_CONTROL), FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0)
    _now, results = run(executor, battery, now, 8, "export_discharge", 2.0)
    assert written(results)[0] == ("control_mode", "Command Discharging (PV First)")
    assert [role for role, _value in written(results)][1:] == ["charge_limit", "discharge_limit"]


def test_a_grid_charge_handed_back_to_self_consumption_writes_the_mode_first() -> None:
    for control in (SIGENERGY_CONTROL, PV_FIRST_CONTROL):
        executor, battery = Executor(control), FakeBattery(control, dict(SIGENERGY_STATES))
        now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0)
        _now, results = run(executor, battery, now, 8, "self_consume")
        assert written(results) == [("control_mode", MSC), ("remote_ems", False), ("charge_limit", 10.0)]
        assert battery.states == dict(SIGENERGY_STATES) | {"charge_limit": "10"}


def test_a_hold_that_turns_into_a_grid_charge_keeps_the_brands_order() -> None:
    executor, battery = _pv_first()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 8, "grid_charge", 3.0)
    assert written(results) == [("charge_limit", 3.0), ("control_mode", "Command Charging (Grid First)")]


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
    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
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


# --- the verification tries the variants in order ---------------------------------------------------------------


@dataclass
class OnlyModeFour(FakeBattery):
    """Firmware that follows V2.7: the discharge limit does nothing in Maximum Self Consumption."""

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if action == "hold" and self.states.get("control_mode") != PV_FIRST:
            return self.idle_kw
        return super().power_kw(action, rate_kw)


def _verify(executor: Executor, battery: FakeBattery, *, looks: int = 900, grid_kw: float = 1.0,
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


def test_a_plant_that_ignores_the_msc_hold_passes_with_the_mode_4_hold_and_control_uses_it() -> None:
    executor = Executor(SIGENERGY_CONTROL)
    battery = OnlyModeFour(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    verification = _verify(executor, battery)
    assert verification.done
    hold = verification.report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "pv_first"
    assert [row["variant"] for row in hold["variants"]] == ["msc_no_discharge", "pv_first"]
    assert hold["variants"][0]["outcome"] == "ignored"
    assert verification.report()["modes"]["normal"]["outcome"] == "restored"
    # Never both at once: every hold write of the first variant was handed back before mode 4 was written.
    modes = [op.value for op in battery.writes if op.role == "control_mode"]
    assert modes.index(PV_FIRST) > modes.index(MSC)
    assert battery.states == dict(SIGENERGY_STATES) | {"charge_limit": "10"}
    assert gate.hold_variant({"modes": {"hold": hold}}) == "pv_first"


def test_a_plant_that_obeys_the_msc_hold_never_tries_mode_4() -> None:
    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    hold = _verify(executor, battery).report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "msc_no_discharge"
    assert [row["variant"] for row in hold["variants"]] == ["msc_no_discharge"]
    assert PV_FIRST not in [op.value for op in battery.writes]
    assert gate.hold_variant({"modes": {"hold": hold}}) == "msc_no_discharge"


def test_a_mode_4_hold_that_charges_fails_and_hold_fails_with_both_variants_recorded() -> None:
    executor = Executor(SIGENERGY_CONTROL)
    battery = _GridChargingV27(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    hold = _verify(executor, battery, grid_kw=4.0).report()["modes"]["hold"]
    assert gate.mode_verdict(hold["outcome"]) == "fail"
    assert [row["variant"] for row in hold["variants"]] == ["msc_no_discharge", "pv_first"]
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
    executor = Executor(SIGENERGY_CONTROL)
    battery = OnlyModeFour(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    tried = ({"variant": "msc_no_discharge", "outcome": "ignored"},)
    hold = _verify(executor, battery, tried=tried).report()["modes"]["hold"]
    assert hold["outcome"] == "obeyed" and hold["variant"] == "pv_first"
    assert [row["variant"] for row in hold["variants"]] == ["msc_no_discharge", "pv_first"]
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
    executor = Executor(SIGENERGY_CONTROL)
    battery = HalfHeld(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    hold = _verify(executor, battery).report()["modes"]["hold"]
    assert hold["outcome"] == "partial" and hold["variants"] == [{"variant": "msc_no_discharge", "outcome": "partial"}]
    assert PV_FIRST not in [op.value for op in battery.writes]


def test_a_hold_write_not_taken_is_not_given_the_next_variant() -> None:
    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), drops={"discharge_limit"})
    hold = _verify(executor, battery).report()["modes"]["hold"]
    assert hold["outcome"].startswith("aborted:write_not_taken:discharge_limit")
    assert [row["variant"] for row in hold["variants"]] == ["msc_no_discharge"]
    assert PV_FIRST not in [op.value for op in battery.writes]
    assert battery.states["remote_ems"] == "off"
