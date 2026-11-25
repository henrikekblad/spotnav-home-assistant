"""Kostal Plenticore's control recipe (`commands.KOSTAL_CONTROL`, `kostal_modbus_control` 4.3.0) through the executor
against a fake inverter that does what the integration's source says: switches whose state is the integration's own
flag and that turn each other off, a rate the charge loop sends as it reads, and nothing taken while the installer's
external control is off. The recipe as data, the order on every change of action (SpotNav never sees its own switch
turned off by the integration), another system's switch never written over, the HA-down cap, the check in full and
in the hardware test's quick timing. Nothing here has run on an inverter."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from custom_components.spotnav.battery import optin
from custom_components.spotnav.battery.adapters import KOSTAL, adapter_for
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    FOLLOWS_MAX,
    KOSTAL_CONTROL,
    KOSTAL_MGMT_MODBUS,
    SNAPSHOT,
    control_for,
    field_tested,
    hardware_test_first,
    table,
)
from custom_components.spotnav.battery.executor import HEADROOM_MARGIN_A, Executor, preview
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.gate import deadman_testable, expiry_lifts, grid_charge_cap_kw
from tests.battery_control_fakes import T0, FakeBattery, events, inputs, run, written

VOLTS = 400.0 / 3 ** 0.5
ENTRY = "01jkostalmodbus00000000000"
KEYS = {
    "management_mode": ("sensor", f"{ENTRY}_battery_mgmt_mode"),
    "block_discharge": ("switch", f"{ENTRY}_block_discharge"),
    "charge_rate": ("number", f"{ENTRY}_charge_rate"),
    "charge_start": ("switch", f"{ENTRY}_charge_start"),
}
#: As found: external control on (the installer's setting), nothing running, the rate following the maximum.
STATES = {
    "management_mode": KOSTAL_MGMT_MODBUS, "block_discharge": "off", "charge_rate": "5200", "charge_start": "off",
}
#: The integration's switches that SpotNav never writes (mutually exclusive with its two).
OTHERS = ("discharge_start", "block_charge", "inverter_control")
LABELS = {
    "charge_start": "Charge Start", "block_discharge": "Block Discharge", "discharge_start": "Discharge Start",
    "block_charge": "Block Charge", "inverter_control": "Inverter Control",
}


@dataclass
class Plenticore(FakeBattery):
    """A Plenticore through `kostal_modbus_control`: turning a switch on turns every other off (`switch.py`
    `async_turn_on`); Charge Start charges at the rate as it reads now, Block Discharge stores the sun and covers
    nothing; with no switch on the inverter covers the house itself. Outside external control over Modbus every write
    is ignored without an error (README), the switches still showing on."""

    house_kw: float = 2.0
    #: Set Charge Rate's maximum (W): the battery's voltage times its current. A value at or above it latches
    #: "follow the maximum" (`number.py` `async_set_native_value`), shown as the maximum with `follows_max` true.
    max_w: float = 5200.0
    others: dict[str, str] = field(default_factory=lambda: dict.fromkeys(OTHERS, "off"))
    predbat: str = "Not installed"
    #: Switch writes the integration made itself (its mutual exclusion).
    turned_off: list[str] = field(default_factory=list)

    def apply(self, op: Any) -> bool:
        if op.method == "switch" and op.value is True and op.role not in self.drops:
            for name in ("charge_start", "block_discharge"):
                if name != op.role and self.states[name] == "on":
                    self.states[name] = "off"
                    self.turned_off.append(name)
            for name in self.others:
                self.others[name] = "off"
        if op.method == "number" and op.role == "charge_rate" and op.value == FOLLOWS_MAX:
            op = replace(op, value=self.max_w)
        taken = super().apply(op)
        if op.method == "number" and taken and op.role not in self.drops:
            follows = float(op.value) >= self.max_w
            self.states[op.role] = str(int(self.max_w if follows else float(op.value)))
            self.attributes[op.role] = {"follows_max": follows, "max": self.max_w}
        return taken

    def turn_on_other(self, name: str) -> None:
        """Someone else's switch turned on (an automation, a person): the integration turns SpotNav's off."""
        for own in ("charge_start", "block_discharge"):
            self.states[own] = "off"
        self.others[name] = "on"

    def management(self) -> Management:
        settings = {"battery management": self.states["management_mode"], "Predbat": self.predbat}
        settings |= {LABELS[name]: self.states[name] for name in ("charge_start", "block_discharge")}
        settings |= {LABELS[name]: state for name, state in self.others.items()}
        busy = [f"{LABELS[name]}:on" for name, state in self.others.items() if state == "on"]
        busy += [f"{LABELS[name]}:on" for name in ("charge_start", "block_discharge") if self.states[name] == "on"]
        if self.predbat == "Enabled":
            busy.append("Predbat:Enabled")
        if self.states["management_mode"] == "External battery management via digital I/O":
            busy.append(f"battery management:{self.states['management_mode']}")
        return Management("self_managed", ", ".join(busy), settings) if busy else Management(
            "free", "self_consumption", settings
        )

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        """The battery's power (kW, + charging) from what the inverter holds, whatever the executor thinks."""
        if not self.obeys or self.states["management_mode"] != KOSTAL_MGMT_MODBUS:
            return -self.house_kw
        if self.states["charge_start"] == "on":
            return float(self.states["charge_rate"]) / 1000
        if self.states["block_discharge"] == "on":
            return 0.0
        return -self.house_kw


def _plant(**kwargs: Any) -> tuple[Executor, Plenticore]:
    plant = Plenticore(KOSTAL_CONTROL, dict(STATES), idle_kw=-2.0, **kwargs)
    plant.attributes["charge_rate"] = {"follows_max": True, "max": plant.max_w}
    return Executor(KOSTAL_CONTROL), plant


def _run(executor: Executor, plant: Plenticore, start: Any, looks: int, action: str | None, rate: float = 0.0,
         **overrides: Any) -> tuple[Any, list[Any]]:
    """`run` with the person's acknowledgement that SpotNav's commands override the inverter's own strategy, which
    this integration does not show (`strategy_unread`)."""
    return run(executor, plant, start, looks, action, rate, **({"takeover": True} | overrides))


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_block_discharge_to_hold_and_the_rate_then_charge_start_to_charge() -> None:
    control = KOSTAL_CONTROL
    assert control_for("kostal") is control and control in CONTROLS and control.adapter is KOSTAL
    assert adapter_for("kostal_modbus_control", None) is KOSTAL
    assert control.wanted("hold", 0.0) == {"block_discharge": True, "charge_start": SNAPSHOT, "charge_rate": SNAPSHOT}
    assert control.wanted("grid_charge", 2.345) == {
        "block_discharge": SNAPSHOT, "charge_rate": 2300.0, "charge_start": True,
    }, "W, rounded down to the rate's 100 W step"
    assert control.wanted("discharge_house", 2.0) == {
        "block_discharge": SNAPSHOT, "charge_start": SNAPSHOT, "charge_rate": SNAPSHOT,
    }
    assert not control.supports("export_discharge"), "no sale: Discharge Start is never written"
    assert not control.hold_idle(), "the sun is still stored under Block Discharge"
    rows = [row for row in table() if row["brand"] == "kostal"]
    assert {row["action"] for row in rows} == {"hold", "grid_charge", "discharge_house", "self_consume"}
    assert control.own_states == {"Charge Start": frozenset({"on"}), "Block Discharge": frozenset({"on"})}
    assert control.requires == (("management_mode", frozenset({KOSTAL_MGMT_MODBUS}), "external_control_off"),)
    assert control.strategy_unread and control.hold_grid_guard
    assert control.grid_charge_phases == 1 and control.autonomous_import is None and control.import_limit is None
    assert control.charge_power_bound == "charge_rate" and control.charge_power_bound_exclusive


def test_every_role_is_the_integrations_key_and_the_card_offers_to_enable_each() -> None:
    roles = {role.name: role for role in KOSTAL_CONTROL.roles}
    assert set(roles) == set(KEYS)
    for name, (domain, unique_id) in KEYS.items():
        assert [key for key, role in roles.items() if role.match.matches(domain, unique_id)] == [name]
    # Never a role: the sale, the other switches, the discharge rate, the SoC limits, core's REST controls.
    for domain, unique_id in (
        ("switch", f"{ENTRY}_discharge_start"), ("switch", f"{ENTRY}_block_charge"),
        ("switch", f"{ENTRY}_inverter_control"), ("switch", f"{ENTRY}_ems_grid_protection"),
        ("number", f"{ENTRY}_discharge_rate"), ("number", f"{ENTRY}_min_soc_limit"),
        ("switch", f"{ENTRY}_devices:local_Battery:ManualCharge"), ("number", f"{ENTRY}_devices:local_Battery:MinSoc"),
    ):
        assert not any(role.match.matches(domain, unique_id) for role in roles.values()), unique_id
    assert optin.ENABLE_KEYS["kostal"] == (
        ("sensor", "battery_mgmt_mode"), ("switch", "block_discharge"), ("number", "charge_rate"),
        ("switch", "charge_start"),
    )
    for role in roles.values():
        assert any(role.match.matches(domain, f"{ENTRY}_{key}") for domain, key in optin.ENABLE_KEYS["kostal"])
    # Only `kostal_modbus_control` has them: on core alone the status names the integration needed.
    assert set(KOSTAL_CONTROL.hacs_roles) == set(roles)
    assert KOSTAL_CONTROL.hacs_repo == "mbuhansen/kostal_plenticore_battery_control"


def test_the_dead_man_is_the_inverters_renewed_by_the_integration_and_never_lifts_a_cap() -> None:
    control = KOSTAL_CONTROL
    expiry = control.expiry
    assert (expiry.kind, expiry.renewer, expiry.window_s) == ("device_deadman", "integration", 60.0)
    assert expiry.evidence == "stated" and expiry.silence == () and control.watchdog_roles == ()
    # Not even if it were measured: the integration's loops run on while SpotNav alone stalls.
    assert not expiry_lifts(expiry, measured=True, slow=False, fixed_charge=False)
    # No write of SpotNav's stops the integration's loops and leaves the command standing: not timed by the test.
    assert not deadman_testable(expiry, slow=False, fixed_charge=False)
    assert control.renewal_cap is None and not control.deadman_must_pass, "SpotNav renews nothing"
    assert control.write_cap("setpoint") == 500
    assert {role.name: role.write_class for role in control.roles} == {
        "management_mode": "setting", "block_discharge": "setpoint", "charge_rate": "setpoint",
        "charge_start": "setpoint",
    }
    # The rate is written back last, as found (the person's own Charge Start runs at it); 1080 is never written.
    assert {role.name for role in control.roles if role.restored} == {"block_discharge", "charge_start", "charge_rate"}
    assert control.restore_order[-1] == "charge_rate"
    assert control.local_readback
    # Never run on a plant: nothing written until a hardware test passed on its firmware.
    assert control.hardware_test_first and not control.field_tested
    assert hardware_test_first("kostal") and not field_tested("kostal")


# --- the writes and their read-back ----------------------------------------------------------------------------------


def test_a_hold_turns_block_discharge_on_alone_and_the_hand_back_turns_it_off() -> None:
    executor, plant = _plant()
    now, results = _run(executor, plant, T0, 6, "hold")
    assert written(results) == [("block_discharge", True)]
    assert executor.state.phase == "active" and plant.power_kw() == 0.0
    now, results = _run(executor, plant, now, 6, "self_consume")
    assert written(results) == [("block_discharge", "off")]
    assert plant.states == STATES and executor.state.phase == "idle"


def test_a_charge_writes_the_rate_then_charge_start_each_read_back_and_a_lower_rate_alone() -> None:
    executor, plant = _plant()
    now, results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    assert written(results) == [("charge_rate", 1000.0), ("charge_start", True)]
    assert len(events(results, "confirmed")) == 2
    assert plant.power_kw() == 1.0 and executor.state.phase == "active"
    seen = preview(KOSTAL_CONTROL, inputs(Plenticore(KOSTAL_CONTROL, dict(STATES)), T0, "grid_charge", 1.0,
                                          takeover=True))
    assert [(op.role, op.value) for op in seen.writes] == written(results)
    assert [(op.role, op.value) for op in seen.restores] == [("charge_start", "off"), ("charge_rate", 5200.0)]
    # A lower rate is the rate alone: the integration's loop sends it at its next tick.
    now, results = _run(executor, plant, now, 3, "grid_charge", 0.5)
    assert written(results) == [("charge_rate", 500.0)] and plant.power_kw() == 0.5
    # The hand-back: Charge Start off, then the rate as found: it followed the maximum, so the maximum again.
    _now, results = _run(executor, plant, now, 6, "self_consume")
    assert written(results) == [("charge_start", "off"), ("charge_rate", FOLLOWS_MAX)]
    assert plant.states == STATES and plant.attributes["charge_rate"]["follows_max"] is True
    assert executor.state.phase == "idle"


def test_a_fixed_rate_the_person_set_is_written_back_as_found() -> None:
    executor, plant = _plant()
    plant.states["charge_rate"], plant.attributes["charge_rate"] = "3000", {"follows_max": False, "max": 5200.0}
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    _now, results = _run(executor, plant, now, 6, "self_consume")
    assert written(results) == [("charge_start", "off"), ("charge_rate", 3000.0)]
    assert plant.states["charge_rate"] == "3000" and executor.state.phase == "idle"


def test_a_change_of_action_turns_the_old_switch_off_first_so_the_integration_never_turns_one_off() -> None:
    # A charge, then a hold: Charge Start off (a charge's stop), then Block Discharge on.
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    now, results = _run(executor, plant, now, 8, "hold")
    # The rate goes back as found too (it acts only under Charge Start).
    assert written(results) == [("charge_start", "off"), ("block_discharge", True), ("charge_rate", FOLLOWS_MAX)]
    assert executor.state.action == "hold" and plant.power_kw() == 0.0
    # And back to a charge: Block Discharge off, the rate, Charge Start.
    _now, results = _run(executor, plant, now, 8, "grid_charge", 2.0)
    assert written(results) == [("block_discharge", "off"), ("charge_rate", 2000.0), ("charge_start", True)]
    assert plant.turned_off == [], "no switch of SpotNav's was turned off by the integration"
    assert not events(results, "external") and executor.state.phase == "active" and plant.power_kw() == 2.0


def test_a_discharge_to_the_house_is_the_inverters_own_control_and_writes_nothing() -> None:
    executor, plant = _plant()
    _now, results = _run(executor, plant, T0, 6, "discharge_house", 2.0)
    assert written(results) == [] and executor.state.phase == "active" and plant.power_kw() == -2.0


# --- when it does not start ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["No external battery management", "External battery management via digital I/O"])
def test_nothing_is_written_without_the_installers_external_control_over_modbus(mode: str) -> None:
    executor, plant = _plant()
    plant.states["management_mode"] = mode
    _now, results = _run(executor, plant, T0, 3, "hold")
    assert written(results) == [] and executor.state.phase == "idle"
    reason = events(results, "not_started")[0]["reason"]
    assert reason == ("external_control_off" if mode.startswith("No") else f"self_managed:battery management:{mode}")


def test_nothing_is_written_without_the_acknowledgement_that_the_inverters_own_strategy_is_overridden() -> None:
    executor, plant = _plant()
    _now, results = _run(executor, plant, T0, 3, "hold", takeover=False)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "strategy_unread"


@pytest.mark.parametrize("other", OTHERS)
def test_another_switch_of_the_integration_on_is_someones_command_and_nothing_starts(other: str) -> None:
    executor, plant = _plant()
    plant.others[other] = "on"
    _now, results = _run(executor, plant, T0, 3, "grid_charge", 1.0)
    assert written(results) == [] and executor.state.phase == "idle"
    assert events(results, "not_started")[0]["reason"] == f"self_managed:{LABELS[other]}:on"


def test_an_active_predbat_runs_charge_start_and_nothing_starts() -> None:
    executor, plant = _plant(predbat="Enabled")
    _now, results = _run(executor, plant, T0, 3, "grid_charge", 1.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "self_managed:Predbat:Enabled"


def test_without_the_modbus_integration_the_status_names_it() -> None:
    executor, plant = _plant(missing=set(KEYS))
    _now, results = _run(executor, plant, T0, 2, "hold")
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "needs_hacs_integration"


# --- someone else's command, never written over ----------------------------------------------------------------------


def test_a_switch_turned_on_by_someone_else_during_a_charge_is_never_turned_off_by_spotnav() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    plant.turn_on_other("discharge_start")
    _now, results = _run(executor, plant, now, 30, "grid_charge", 1.0)
    assert executor.state.phase != "active"
    assert all(role in ("charge_start", "block_discharge", "charge_rate") for role, _value in written(results))
    assert ("charge_start", True) not in written(results), "never started again over the other command"
    assert plant.others["discharge_start"] == "on"


def test_charge_start_turned_on_by_someone_else_under_spotnavs_hold_is_left_alone() -> None:
    # The integration turns Block Discharge off itself; SpotNav gives the hold up and writes nothing over the charge.
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 6, "hold")
    plant.states["block_discharge"], plant.states["charge_start"] = "off", "on"
    _now, results = _run(executor, plant, now, 30, "hold")
    assert written(results) == [] and plant.states["charge_start"] == "on"
    assert executor.state.phase != "active"


def test_a_rate_changed_by_someone_else_during_a_charge_is_given_up_not_written_back() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    plant.states["charge_rate"] = "4000"
    _now, results = _run(executor, plant, now, 40, "grid_charge", 1.0)
    # SpotNav ends its own charge (its switch) and never writes the rate back over the change.
    assert written(results) == [("charge_start", "off")] and plant.states["charge_rate"] == "4000"
    assert events(results, "external")[0]["reason"] == "charge_rate" and executor.state.phase == "blocked"


# --- the fuse --------------------------------------------------------------------------------------------------------


def test_a_near_fuse_stops_the_charge_in_one_look_and_a_holds_peak_releases_block_discharge() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    _now, results = _run(executor, plant, now, 1, "grid_charge", 1.0, headroom_a=0.5)
    # Charge Start off first; the rate as found after it.
    assert written(results) == [("charge_start", "off"), ("charge_rate", FOLLOWS_MAX)]
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 6, "hold")
    _now, results = _run(executor, plant, now, 2, "hold", headroom_a=1.2)
    assert written(results) == [("block_discharge", "off")] and events(results, "peak_cover")


def test_a_charge_keeps_the_fuse_when_home_assistant_stops_all_on_one_phase() -> None:
    """The dead-man is stated and the integration's: the HA-down cap takes the whole charge on the fullest phase."""
    peaks = {"L1": 6.0, "L2": 9.0, "L3": 4.0}
    for fuse_a in (16.0, 20.0, 25.0):
        cap = grid_charge_cap_kw(fuse_a, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                                 plant_phases=KOSTAL_CONTROL.grid_charge_phases, house_phase_peaks_a=peaks)
        assert cap is not None and max(peaks.values()) + cap * 1000 / VOLTS <= fuse_a - HEADROOM_MARGIN_A + 1e-9


def test_a_charge_that_does_nothing_is_handed_back_and_left_as_found() -> None:
    # The installer's setting gone after a firmware update: the switches show on, the inverter ignores them.
    executor, plant = _plant(obeys=False)
    _now, results = _run(executor, plant, T0, 80, "grid_charge", 1.0)
    assert events(results, "hand_back")[0]["reason"] == "no_effect:grid_charge" and executor.state.phase == "blocked"
    assert plant.states["charge_start"] == "off" and plant.states["block_discharge"] == "off"


def test_the_charge_is_bounded_well_below_the_rates_maximum_which_means_follow_the_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_battery_control import _bare_control  # noqa: PLC0415

    # 5 % below the maximum (it moves with the voltage between the read and the write), on the 100 W step.
    control = _bare_control("kostal", {"charge_rate": ("number.kostal_set_charge_rate", {"max": 5200})}, monkeypatch)
    assert control._charge_entity_max_kw(KOSTAL_CONTROL) == pytest.approx(4.9)
    assert control._within_charge_power(9.0) == pytest.approx(4.9) and control._within_charge_power(2.0) == 2.0
    # A small maximum: the step is the larger margin.
    control = _bare_control("kostal", {"charge_rate": ("number.kostal_set_charge_rate", {"max": 1500})}, monkeypatch)
    assert control._charge_entity_max_kw(KOSTAL_CONTROL) == pytest.approx(1.4)


def test_a_rate_that_latched_follow_the_maximum_is_not_taken_and_charge_start_never_goes_on() -> None:
    # The maximum fell below the rate between the read and the write: the integration follows its maximum, which
    # then shows the value written. Not taken: Charge Start is never turned on.
    executor, plant = _plant(max_w=1000.0)
    _now, results = _run(executor, plant, T0, 12, "grid_charge", 1.0)
    assert ("charge_rate", 1000.0) in written(results)
    assert ("charge_start", True) not in written(results) and plant.states["charge_start"] == "off"
    assert not any(event["reason"] == "charge_rate" for event in events(results, "confirmed"))


# --- the check before control and the hardware test ------------------------------------------------------------------


def test_the_verification_runs_every_mode_and_leaves_the_inverter_as_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.test_battery_verify as verify_tests  # noqa: PLC0415

    # The person's acknowledgement is the site's: the check runs as control would.
    real = verify_tests.inputs
    monkeypatch.setattr(verify_tests, "inputs", lambda *args, **kwargs: real(*args, **({"takeover": True} | kwargs)))
    for quick in (False, True):
        executor, plant = _plant()
        verification = verify_tests._verify(executor, plant, quick=quick)
        assert verification.done, quick
        assert verify_tests._outcomes(verification) == {
            "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored",
        }, quick
        assert verification.report()["brand"] == "kostal"
        assert plant.states["charge_start"] == "off" and plant.states["block_discharge"] == "off"
        assert executor.state.phase == "idle"


def test_the_shell_writes_a_rate_found_following_its_maximum_as_the_maximum_now() -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    from custom_components.spotnav.battery.control import resolve_follows_max  # noqa: PLC0415
    from custom_components.spotnav.battery.executor import Op  # noqa: PLC0415

    states = {"number.kostal_set_charge_rate": SimpleNamespace(attributes={"max": 6100.0, "follows_max": False})}
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    op = Op("charge_rate", "number", "number.kostal_set_charge_rate", FOLLOWS_MAX, restore=True)
    assert resolve_follows_max(hass, op).value == 6100.0
    # Any other write as it is; no maximum shown: not written.
    assert resolve_follows_max(hass, replace(op, value=3000.0)).value == 3000.0
    states["number.kostal_set_charge_rate"] = SimpleNamespace(attributes={})
    assert resolve_follows_max(hass, op) is None
