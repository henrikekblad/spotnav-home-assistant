"""A grid charge per phase through the shell (`battery/control.py`) on the owner's plant in a fake Home Assistant:
when it is used and when the balanced charge is, the verification's mode `per_phase` (the sign and the phase
mapping), its `charge` tried per phase where the balanced charge has no room (and so the hardware test's), the
status, and an unbalanced house under the fuse when Home Assistant stops."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.control import PER_PHASE_GAIN_KW, BatteryControl
from tests.test_battery_control import (
    CHARGE,
    DISCHARGE,
    HOUSE_KW,
    MODE,
    SWITCH,
    _import_limit,
    _looks,
    _on,
    _options,
    _passed,
    _site,
)

pytestmark = pytest.mark.usefixtures("offline_relay")

VOLTS = 400.0 / 3 ** 0.5
TARGETS = tuple(f"number.sigen_plant_phase_{letter}_active_power_fixed_adjustment" for letter in "abc")
PHASE_SWITCH = "switch.sigen_plant_independent_phase_power_control"
TOTAL = "number.sigen_plant_active_power_fixed_adjustment"
#: The owner's house (2026-11-18): the learned peaks on a 20 A fuse.
PEAKS = {"L1": 13.6, "L2": 18.0, "L3": 17.4}


def _phase_controls(hass: HomeAssistant, *, switch: str = "on") -> None:
    """The plant's targets per phase and its independent phase switch, enabled (disabled by default in the
    integration)."""
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id="ENTRY", identifiers={("sigen", "plant")})
    owner = hass.config_entries.async_get_entry("ENTRY")
    for letter, entity_id in zip("abc", TARGETS, strict=True):
        entry = registry.async_get_or_create(
            "number", "sigen", f"ENTRY_sigen_plant_plant_phase_{letter}_active_power_fixed_target",
            device_id=device.id, config_entry=owner, suggested_object_id=entity_id.split(".", 1)[1],
        )
        assert entry.entity_id == entity_id
        hass.states.async_set(entity_id, "0.0", {"unit_of_measurement": "kW", "step": 0.001})
    entry = registry.async_get_or_create(
        "number", "sigen", "ENTRY_sigen_plant_plant_active_power_fixed_target", device_id=device.id,
        config_entry=owner, suggested_object_id=TOTAL.split(".", 1)[1],
    )
    assert entry.entity_id == TOTAL
    hass.states.async_set(TOTAL, "0.0", {"unit_of_measurement": "kW", "step": 0.001})
    entry = registry.async_get_or_create(
        "switch", "sigen", "ENTRY_sigen_plant_plant_independent_phase_power_control_enable", device_id=device.id,
        config_entry=owner, suggested_object_id=PHASE_SWITCH.split(".", 1)[1],
    )
    assert entry.entity_id == PHASE_SWITCH
    hass.states.async_set(PHASE_SWITCH, switch)


class World:
    """The house on three phases and what the plant does on each: it follows its targets in PCS Remote Control,
    stands still under a hold, charges evenly in Command Charging and covers each phase's house otherwise."""

    def __init__(self, hass: HomeAssistant, plant: Any, fuse_a: float = 20.0) -> None:
        self.hass, self.plant, self.fuse_a = hass, plant, fuse_a
        self.house_kw = [HOUSE_KW / 4, HOUSE_KW / 2, HOUSE_KW / 4]
        #: The meter's phase each of the plant's phases is on (the identity when the wiring is as named).
        self.wiring = (0, 1, 2)
        self.follows = True
        self.known = True
        #: -1: the plant does the opposite of what its targets say (a sign error in the recipe).
        self.sign = 1.0

    def targets(self) -> tuple[float, float, float]:
        states = [self.hass.states.get(entity_id) for entity_id in TARGETS]
        return tuple(0.0 if state is None else float(state.state) for state in states)  # type: ignore[return-value]

    def plant_kw(self) -> tuple[float, float, float]:
        """The plant's own power per phase as it names its phases (kW, + output)."""
        state = self.hass.states.get
        if state(SWITCH).state == "on" and state(MODE).state == "PCS Remote Control":
            if not self.follows:
                return (0.0, 0.0, 0.0)
            return tuple(self.sign * target for target in self.targets())  # type: ignore[return-value]
        if state(SWITCH).state == "on" and state(MODE).state == "Command Charging (Grid First)":
            each = -float(state(CHARGE).state) / 3
            return (each, each, each)
        if state(DISCHARGE).state == "0":
            return (0.0, 0.0, 0.0)
        # Its own mode: it covers each phase's house (as wired).
        return tuple(self.house_kw[self.wiring[index]] for index in range(3))  # type: ignore[return-value]

    def grid_kw(self) -> tuple[float, float, float]:
        grid = list(self.house_kw)
        for index, power in enumerate(self.plant_kw()):
            grid[self.wiring[index]] -= power
        return (grid[0], grid[1], grid[2])

    def headroom_a(self) -> tuple[float, float, float]:
        return tuple(self.fuse_a - power * 1000 / VOLTS for power in self.grid_kw())  # type: ignore[return-value]

    def stopped_meter_a(self, peaks: dict[str, float]) -> list[float]:
        """`stopped_a` on the meter's phases as the plant is wired: a target's charge lands on the meter phase its
        plant phase is on (`wiring`)."""
        amps = list(peaks.values())
        for index, target in enumerate(self.targets()):
            amps[self.wiring[index]] += max(-target * self.sign, 0.0) * 1000 / VOLTS
        return amps

    def stopped_a(self, peaks: dict[str, float]) -> list[float]:
        """Each phase's current if Home Assistant stops now and the house goes to its peak on every phase: the peak
        and what the plant's target takes there."""
        return [peak + max(-target, 0.0) * 1000 / VOLTS for peak, target in zip(peaks.values(), self.targets(), strict=True)]


async def _world(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], *, controls: bool = True,
    peaks: dict[str, float] | None = PEAKS, fuse_a: float = 20.0,
):
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=fuse_a)
    if controls:
        _phase_controls(hass)
    world = World(hass, plant, fuse_a)
    monkeypatch.setattr(BatteryControl, "_phase_headroom_a", lambda self: world.headroom_a())
    monkeypatch.setattr(BatteryControl, "_grid_phase_kw", lambda self: world.grid_kw())
    monkeypatch.setattr(BatteryControl, "_plant_phase_kw", lambda self, now: world.plant_kw() if world.known else None)
    monkeypatch.setattr(BatteryControl, "_battery_power", lambda self: (-0.8 * sum(world.plant_kw()), 5.0))
    monkeypatch.setattr(BatteryControl, "_grid_import_kw", lambda self: sum(world.grid_kw()))
    monkeypatch.setattr(BatteryControl, "_phase_peaks", lambda self, now: peaks)
    return site, controller, plant, world


async def _control(hass, site, controller, plan, *, per_phase: str | None = "obeyed") -> BatteryControl:
    control = BatteryControl(hass, controller, plan_source=plan)
    await control.async_start()
    _on(hass, site, control)
    if per_phase is not None:
        _passed(control, per_phase=per_phase)
    return control


def _charge(_now):
    return ("grid_charge", 12.0, None)


def _writes(plant: Any) -> list[tuple[str, Any]]:
    return [(call[2]["entity_id"], call[2].get("value", call[2].get("option", call[1]))) for call in plant.calls]


# --- when it is used ------------------------------------------------------------------------------------------------


async def test_an_unbalanced_house_charges_per_phase_and_stays_under_the_fuse_when_home_assistant_stops(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = await _control(hass, site, controller, _charge)
    # The balanced charge has no room: L2's 18 A is the house on every phase.
    assert control._grid_charge_cap_kw(dt_util.now(), control.options()) == 0.0
    now = await _looks(control, dt_util.now(), 8)
    assert _writes(plant) == [
        (TARGETS[0], -0.92), (SWITCH, "turn_on"),
    ], "whole amps: L1 4 A, L2 and L3 none; the mode register already reads PCS Remote Control"
    assert hass.states.get(MODE).state == "PCS Remote Control" and world.targets() == (-0.92, 0.0, 0.0)
    assert hass.states.get(CHARGE).state == "unknown", "the battery limits are left as found"
    executor = control.executor
    assert executor.control.phased and executor.state.phase_kw == (0.92, 0.0, 0.0)
    # Home Assistant stops here: the targets stay. The house at its learned peak on every phase.
    stopped = world.stopped_a(PEAKS)
    assert all(amps <= 20.0 - 2.0 + 1e-9 for amps in stopped), stopped
    assert stopped[1] == 18.0 and stopped[0] == pytest.approx(13.6 + 920 / VOLTS)
    status = control.status()
    assert status["per_phase"] == {
        "state": "verified", "why": None, "used": True, "caps_kw": {"L1": 1.02, "L2": 0.0, "L3": 0.14},
        "charging_kw": {"L1": 0.92, "L2": 0.0, "L3": 0.0},
    }
    assert status["grid_charge_cap"]["cap_kw"] == 0.0, "the balanced cap is said as before"
    assert [row["rate_kw"] for row in control.log if row["kind"] == "restricted"] == [pytest.approx(1.155, abs=0.01)]
    # Turned off: the targets to 0, the mode to Maximum Self Consumption, Remote EMS off.
    plant.calls.clear()
    _options(hass, site, enabled=False)
    await _looks(control, now, 8)
    assert _writes(plant) == [(TARGETS[0], 0.0), (MODE, "Maximum Self Consumption"), (SWITCH, "turn_off")]
    assert world.targets() == (0.0, 0.0, 0.0) and control.status()["per_phase"]["charging_kw"] is None


@pytest.mark.parametrize(
    ("case", "why"),
    [
        ("no_controls", "control_not_found:power_target"),
        ("switch_off", "phase_control_off"),
        ("not_verified", None),
        ("failed", None),
    ],
)
async def test_without_the_targets_or_their_verification_the_charge_is_the_balanced_one_under_its_cap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], case: str, why: str | None
) -> None:
    site, controller, plant, world = await _world(
        hass, monkeypatch, hass_storage, controls=case != "no_controls", fuse_a=25.0, peaks=None
    )
    if case == "switch_off":
        hass.states.async_set(PHASE_SWITCH, "off")
    outcome = {"not_verified": None, "failed": "partial"}.get(case, "obeyed")
    control = await _control(hass, site, controller, _charge, per_phase=outcome)
    await _looks(control, dt_util.now(), 8)
    # 25 A and nothing learned: 3.935 kW balanced, exactly as before.
    assert hass.states.get(MODE).state == "Command Charging (Grid First)"
    assert hass.states.get(CHARGE).state.startswith("3.935")
    assert not any(entity_id in TARGETS for entity_id, _value in _writes(plant))
    assert control.executor.control.phased is False and control.executor.state.phase_kw is None
    block = control.status()["per_phase"]
    expected = {"no_controls": "unavailable", "switch_off": "unavailable", "not_verified": "pending", "failed": "failed"}
    assert block["state"] == expected[case] and block["why"] == why and block["used"] is False


async def test_a_balanced_house_and_a_guarding_import_limit_keep_the_balanced_charge(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    even = {"L1": 9.0, "L2": 9.0, "L3": 9.0}
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage, peaks=even)
    control = await _control(hass, site, controller, _charge)
    now = dt_util.now()
    cap, caps = control._charge_limits(now, control.options())
    assert caps is None and cap == pytest.approx(9.0 * 3 * VOLTS / 1000), "the phases allow no more than the balance"
    await _looks(control, now, 8)
    assert hass.states.get(MODE).state == "Command Charging (Grid First)" and world.targets() == (0.0, 0.0, 0.0)
    assert control.executor.control.phased, "the phased control runs the balanced charge, the targets at 0"
    assert control.status()["per_phase"]["used"] is False
    # A phase a little lower is not worth another kind of charge; one much lower is.
    monkeypatch.setattr(BatteryControl, "_phase_peaks", lambda self, now: {**even, "L1": 9.0 - 2.0})
    assert control._charge_limits(now, control.options())[1] is None
    assert 2.0 * VOLTS / 1000 < PER_PHASE_GAIN_KW
    monkeypatch.setattr(BatteryControl, "_phase_peaks", lambda self, now: {**even, "L1": 3.0})
    cap, caps = control._charge_limits(now, control.options())
    assert caps is not None and cap == pytest.approx(sum(caps))
    # The plant's own import limit shown to act and set to cover the worst phase: its balanced charge, no caps.
    _import_limit(hass, "4.0")
    _options(hass, site, import_limit_acts=True)
    assert control._charge_limits(now, control.options()) == (None, None)


async def test_a_reading_per_phase_that_goes_missing_ends_the_charge_per_phase(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = await _control(hass, site, controller, _charge)
    now = await _looks(control, dt_util.now(), 8)
    assert world.targets() == (-0.92, 0.0, 0.0)
    # The site no longer reads each phase: per phase is not there, the balanced cap (nothing here) counts again.
    monkeypatch.setattr(BatteryControl, "_phase_headroom_a", lambda self: None)
    await _looks(control, now, 8)
    assert world.targets() == (0.0, 0.0, 0.0) and hass.states.get(SWITCH).state == "off"
    assert control.status()["per_phase"]["why"] == "site_phases"


async def test_a_plant_that_stops_following_its_targets_loses_the_capability(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = await _control(hass, site, controller, _charge)
    now = await _looks(control, dt_util.now(), 8)
    world.follows = False
    await _looks(control, now, 40)
    assert control.report()["modes"]["per_phase"]["outcome"] == "ignored"
    assert control.status()["per_phase"]["state"] == "failed"
    assert control.executor.state.blocked == "no_effect:grid_charge" and world.targets() == (0.0, 0.0, 0.0)
    assert any(row["kind"] == "per_phase_withdrawn" for row in control.log)


async def test_the_owner_s_test_action_charges_per_phase_too(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = await _control(hass, site, controller, lambda _now: None)
    answer = await control.async_trial("grid_charge", 4.0, 10, force=False)
    assert answer["trial"]["rate_kw"] == pytest.approx(1.155, abs=0.01)
    await _looks(control, dt_util.now(), 8)
    assert world.targets() == (-0.92, 0.0, 0.0)
    await control.async_trial("stop", None, 1, force=False)
    await _looks(control, dt_util.now() + timedelta(seconds=10), 6)
    assert world.targets() == (0.0, 0.0, 0.0) and hass.states.get(SWITCH).state == "off"


# --- the verification ------------------------------------------------------------------------------------------------


async def _verified(hass, site, controller, world, looks: int = 130) -> BatteryControl:
    """A plant whose other modes passed, with the mode `per_phase` still to try."""
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _on(hass, site, control)
    # An hour after the run that passed the others (the routine's gap between runs).
    await _looks(control, dt_util.now() + timedelta(hours=2), looks)
    return control


async def test_the_check_probes_each_phase_with_a_small_discharge_and_records_the_capability(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = await _verified(hass, site, controller, world)
    assert control.verification is None
    row = control.report()["modes"]["per_phase"]
    assert row["outcome"] == "obeyed"
    assert [(phase["phase"], phase["target_kw"], phase["outcome"]) for phase in row["phases"]] == [
        ("L1", -1.0, "obeyed"), ("L2", -1.0, "obeyed"), ("L3", -1.0, "obeyed")
    ]
    assert all(len(phase["readings"]) == 3 for phase in row["phases"])
    # One phase at a time, a discharge of 1 kW (a positive target), then everything back.
    targets = [(entity_id, value) for entity_id, value in _writes(plant) if entity_id in TARGETS]
    assert targets == [(TARGETS[0], 1.0), (TARGETS[0], 0.0), (TARGETS[1], 1.0), (TARGETS[1], 0.0), (TARGETS[2], 1.0),
                       (TARGETS[2], 0.0)], "each phase back at 0 before the next one's discharge"
    assert not any(value < 0 for _entity_id, value in targets), "the check never charges"
    assert world.targets() == (0.0, 0.0, 0.0) and hass.states.get(SWITCH).state == "off"
    assert control.status()["per_phase"]["state"] == "verified" and gate.per_phase(control.report()) == "verified"
    # Only now are the plant's phases taken as the meter's of the same name (the cover per phase before a charge).
    executor = control.executor
    assert control.inputs(dt_util.now(), executor.control).plant_phases_mapped is True
    control.reports[control.firmware_key()]["modes"]["per_phase"]["outcome"] = "wrong_phase"
    assert control.inputs(dt_util.now(), executor.control).plant_phases_mapped is False
    control.reports[control.firmware_key()]["modes"]["per_phase"]["outcome"] = "obeyed"
    # The other modes' rows are as they were, and control runs.
    assert control.report()["modes"]["hold"]["outcome"] == "obeyed" and control.state() == "on"
    # Decided: no further run.
    assert control._per_phase_due(control.report()) is False


@pytest.mark.parametrize(
    ("fault", "outcome", "state"),
    [
        # The plant's phase A is the meter's L2 (and B its L3, C its L1): a target would land on another phase.
        ("rotated", "partial", "failed"),
        ("ignores", "ignored", "failed"),
        # It takes its targets with the other sign: told to discharge it charges, which is the executor's runaway
        # or the step's own readings, whichever comes first; either is this check's answer, not a block.
        ("sign", "partial", "failed"),
        ("no_reading", "not_judgeable:phase_reading_stale", "pending"),
    ],
)
async def test_a_plant_whose_phases_are_not_the_meter_s_or_that_ignores_its_targets_never_charges_per_phase(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], fault: str, outcome: str,
    state: str,
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    if fault == "rotated":
        world.wiring = (1, 2, 0)
    elif fault == "ignores":
        world.follows = False
    elif fault == "sign":
        world.sign = -1.0
    else:
        world.known = False
    control = await _verified(hass, site, controller, world)
    row = control.report()["modes"]["per_phase"]
    assert row["outcome"] == outcome, row
    if fault == "rotated":
        assert row["phases"][0]["outcome"] == "wrong_phase" and len(row["phases"]) == 1, "it stops at the first"
        assert all(reading["house_moved_kw"] == pytest.approx(1.0) for reading in row["phases"][0]["readings"])
    assert control.status()["per_phase"]["state"] == state
    if fault == "sign":
        assert row["phases"][0]["outcome"] in ("wrong_sign", "phase_over") and row["phases"][0]["sign"] in ("wrong", None)
    assert control.executor is not None and control.executor.state.phase == "idle", "control is not left blocked"
    assert world.targets() == (0.0, 0.0, 0.0) and hass.states.get(SWITCH).state == "off"
    # Control stays on (hold and normal passed), and a grid charge is the balanced one under its cap: none here.
    assert control.state() == "on" and control._charge_limits(dt_util.now(), control.options()) == (0.0, None)


async def test_the_check_does_not_try_per_phase_where_the_targets_are_not_there(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage, controls=False)
    control = await _verified(hass, site, controller, world, looks=20)
    assert control.verification is None and plant.calls == []
    assert "per_phase" not in control.report()["modes"]
    assert control.status()["per_phase"] == {
        "state": "unavailable", "why": "control_not_found:power_target", "used": False,
        "caps_kw": {"L1": 1.02, "L2": 0.0, "L3": 0.14}, "charging_kw": None,
    }
    # The person enables them later: the check runs for that mode alone.
    _phase_controls(hass)
    assert control.status()["per_phase"]["state"] == "pending"
    assert control._per_phase_due(control.report()) is True


async def test_a_moment_that_could_make_a_sound_plant_look_wrong_is_never_a_decisive_answer(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    from custom_components.spotnav.battery.verify import Sample, _probe_unfit

    fine = Sample(-1.0, 5.0, 50.0, grid_kw=0.0)
    assert _probe_unfit(fine, before=True) is None
    # The sun gives more than the house takes: the site exports, or the battery charges, before the probe.
    assert _probe_unfit(Sample(-1.0, 5.0, 50.0, grid_kw=-0.8), before=True) == "sun_surplus"
    assert _probe_unfit(Sample(1.2, 5.0, 50.0, grid_kw=0.0), before=True) == "sun_surplus"
    assert _probe_unfit(Sample(1.2, 5.0, 50.0, grid_kw=-0.8), before=False) is None, "under the probe that is the probe"
    # Near full, near the battery's own floor, or not known; and the export limit held at 0.
    for soc, floor in ((92.0, 0.0), (4.0, 0.0), (23.0, 20.0), (None, 0.0)):
        assert _probe_unfit(Sample(-1.0, 5.0, soc, grid_kw=0.0, soc_min_pct=floor), before=False) == "soc_near_end"
    assert _probe_unfit(Sample(-1.0, 5.0, 50.0, grid_kw=0.0, curtailing=True), before=True) == "export_curtailed"
    # The hardware test's charge step is tried up to 98 % (judging only, not safety).
    assert _probe_unfit(Sample(-1.0, 5.0, 95.0, grid_kw=0.0), before=False, soc_max_pct=98.0) is None
    assert _probe_unfit(Sample(-1.0, 5.0, 98.0, grid_kw=0.0), before=False, soc_max_pct=98.0) == "soc_near_end"
    # Through the shell: the step is left for a later run, nothing is written, and no fail is recorded.
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    monkeypatch.setattr(BatteryControl, "_curtailing", lambda self: True)
    control = await _verified(hass, site, controller, world, looks=30)
    assert control.report()["modes"]["per_phase"]["outcome"] == "not_judgeable:export_curtailed"
    assert control.status()["per_phase"]["state"] == "pending" and plant.calls == []
    assert control._per_phase_due(control.report()) is True


async def test_the_diagnostics_show_the_day_s_writes_left_per_class(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = await _control(hass, site, controller, _charge)
    await _looks(control, dt_util.now(), 8)
    assert control.snapshot()["write_budget"] == {
        "setting": {"cap": 96, "used": 1, "left": 95},
        "setpoint": {"cap": 500, "used": 1, "left": 499},
    }


async def test_the_plain_control_starts_nothing_while_a_phase_target_is_left_standing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    hass.states.async_set(TARGETS[1], "-2.0", {"unit_of_measurement": "kW", "step": 0.001})
    control = await _control(hass, site, controller, lambda _now: ("hold", 0.0, None), per_phase=None)
    await _looks(control, dt_util.now(), 4)
    assert plant.calls == [] and control.executor.control.phased is False
    assert [row["reason"] for row in control.log if row["kind"] == "not_started"][0] == "stale_target:phase_l2_target"
    hass.states.async_set(TARGETS[1], "0.0", {"unit_of_measurement": "kW", "step": 0.001})
    await _looks(control, dt_util.now() + timedelta(minutes=1), 4)
    assert hass.states.get(SWITCH).state == "on"


# --- `charge` per phase where the balanced charge has no room ------------------------------------------------------


async def _hardware_test(control: BatteryControl, hass, site) -> dict[str, Any]:
    """A person's hardware test run to its end (under the test's ten minutes); its report."""
    _options(hass, site, enabled=True, shadow_only=True)
    control.facts = control._read_facts()
    assert await control.async_hardware_test_start() is None
    started = now = dt_util.now()
    while control.hardware_test_progress()["running"] and now < started + timedelta(minutes=15):
        now = await _looks(control, now, 1)
    assert now - started <= timedelta(minutes=10), "within the hardware test's deadline"
    return control.hardware_test_progress()["report"]


def _charge_targets(plant: Any) -> list[tuple[str, float]]:
    return [(entity_id, value) for entity_id, value in _writes(plant) if entity_id in TARGETS]


def _watch_stop(control: BatteryControl, world: World, monkeypatch: pytest.MonkeyPatch) -> list[list[float]]:
    """At every look of the check: each meter phase's current were Home Assistant to stop there, the house at its
    learned peak and every target standing (Sigenergy has no dead-man)."""
    stopped: list[list[float]] = []
    original = control._async_verify

    async def verify(now):
        result = await original(now)
        stopped.append(world.stopped_meter_a(PEAKS))
        return result

    monkeypatch.setattr(control, "_async_verify", verify)
    return stopped


async def test_the_hardware_test_charges_on_the_phase_with_room_where_the_balanced_charge_has_none(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """The owner's site on 2026-11-18: L2 full at its learned peak, so the balanced cap is 0 and the charge step was
    `not_judgeable:grid_charge_cap`. Now it charges on L1, within L1's own HA-down cap, and hands back."""
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    assert control._grid_charge_cap_kw(dt_util.now(), control.options()) == 0.0
    stopped = _watch_stop(control, world, monkeypatch)
    report = await _hardware_test(control, hass, site)
    modes = {mode: row["outcome"] for mode, row in report["modes"].items()}
    assert modes == {"hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"}
    row = report["modes"]["charge"]
    assert row["via"] == "per_phase" and row["rate_kw"] == pytest.approx(0.923)
    # `per_phase` has not passed yet: a discharge on L1 shows its sign and mapping before L1 is charged.
    assert [(phase["phase"], phase["target_kw"], phase["outcome"], phase["sign"], phase["mapping"])
            for phase in row["phases"]] == [("L1", -1.0, "obeyed", "ok", "ok"), ("L1", 0.923, "obeyed", "ok", "ok")]
    assert all(reading["plant_kw"][0] == pytest.approx(-0.92) for reading in row["phases"][1]["readings"])
    # L1 alone: a discharge of 1 kW (a positive target), through 0, a charge (a negative target) of 4 A, back to 0;
    # the balanced charge is never commanded.
    assert _charge_targets(plant) == [(TARGETS[0], 1.0), (TARGETS[0], 0.0), (TARGETS[0], -0.92), (TARGETS[0], 0.0)]
    assert not any(call[2].get("option") == "Command Charging (Grid First)" for call in plant.calls)
    # Every look: were Home Assistant to stop, every phase stays under the fuse with the house at its peak.
    assert stopped and all(amps <= 20.0 - 2.0 + 1e-9 for look in stopped for amps in look)
    assert world.targets() == (0.0, 0.0, 0.0) and hass.states.get(SWITCH).state == "off"
    assert control.executor is not None and not control.executor.control.phased, "the control as it was before"
    assert control.reports == {}, "the hardware test never counts as the routine"


@pytest.mark.parametrize(("fault", "phase_outcome"), [("sign", "wrong_sign"), ("rotated", "wrong_phase")])
async def test_a_plant_of_the_wrong_sign_or_on_another_phase_is_found_before_any_charge_and_handed_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], fault: str,
    phase_outcome: str,
) -> None:
    """The discharge on the chosen phase comes first: a plant whose phase A is the meter's L2 (the full one) or
    that takes its targets with the other sign is found by a step that cannot charge, and no charge is written.
    Were Home Assistant to stop at any look of the check, no meter phase passes the fuse."""
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    if fault == "sign":
        world.sign = -1.0
    else:
        # A P1 meter and an inverter that name their phases in a different order.
        world.wiring = (1, 2, 0)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    stopped = _watch_stop(control, world, monkeypatch)
    report = await _hardware_test(control, hass, site)
    modes = {mode: row["outcome"] for mode, row in report["modes"].items()}
    assert modes == {"hold": "obeyed", "charge": "partial", "discharge_house": "skipped", "normal": "restored"}
    phases = report["modes"]["charge"]["phases"]
    assert [(phase["phase"], phase["target_kw"], phase["outcome"]) for phase in phases] == [
        ("L1", -1.0, phase_outcome)
    ], "the charge is never commanded"
    if fault == "sign":
        assert phases[0]["sign"] == "wrong" and phases[0]["mapping"] is None
    else:
        assert phases[0]["sign"] == "ok" and phases[0]["mapping"] == "wrong"
        assert all(reading["house_moved_kw"] == pytest.approx(1.0) for reading in phases[0]["readings"])
    assert _charge_targets(plant) == [(TARGETS[0], 1.0), (TARGETS[0], 0.0)], "handed back at once"
    # With the sign wrong, told to discharge it charges 1 kW on L1 for the step's readings (the discharge probe's
    # own exposure); L1 has room for that at its peak here.
    assert stopped and all(amps <= 20.0 - 2.0 + 1e-9 for look in stopped for amps in look)
    assert world.targets() == (0.0, 0.0, 0.0) and hass.states.get(SWITCH).state == "off"
    assert control.executor is not None and control.executor.state.phase == "idle", "nothing left blocked"
    # In the routine's report it is the capability's answer too, and no grid charge is allowed on it.
    routine = {"modes": {**report["modes"], "per_phase": {"mode": "per_phase", "outcome": "obeyed"}}}
    assert gate.per_phase(routine) == "failed" and gate.charge_by_phases(routine)
    assert gate.report_status(routine) == "passed"
    assert "grid_charge" not in gate.allowed_actions(routine, control.options())


async def test_once_per_phase_passed_the_charge_runs_without_the_discharge_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _passed(control, charge="not_judgeable:grid_charge_cap", per_phase="obeyed")
    stopped = _watch_stop(control, world, monkeypatch)
    report = await _hardware_test(control, hass, site)
    row = report["modes"]["charge"]
    assert row["outcome"] == "obeyed"
    assert [(phase["phase"], phase["target_kw"]) for phase in row["phases"]] == [("L1", 0.923)]
    assert _charge_targets(plant) == [(TARGETS[0], -0.92), (TARGETS[0], 0.0)]
    assert stopped and all(amps <= 20.0 - 2.0 + 1e-9 for look in stopped for amps in look)


@pytest.mark.parametrize(("taken", "outcome"), [(1.0, "obeyed"), (0.3, "not_judgeable:battery_full")])
async def test_the_hardware_test_charges_per_phase_near_full_and_a_short_take_is_not_a_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], taken: float, outcome: str
) -> None:
    from dataclasses import replace

    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    follow = world.plant_kw

    def plant_kw() -> tuple[float, float, float]:
        # A battery at 95 % takes only part of a charge (a negative target); a discharge it gives in full.
        return tuple(value * taken if value < 0 else value for value in follow())  # type: ignore[return-value]

    world.plant_kw = plant_kw  # type: ignore[method-assign]
    outside = BatteryControl._with_outside
    monkeypatch.setattr(BatteryControl, "_with_outside", lambda self, facts: replace(outside(self, facts), soc_pct=95.0))
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _passed(control, charge="not_judgeable:grid_charge_cap", per_phase="obeyed")
    report = await _hardware_test(control, hass, site)
    assert report["modes"]["charge"]["outcome"] == outcome
    assert world.targets() == (0.0, 0.0, 0.0)


async def test_without_room_on_any_phase_the_charge_step_says_so_and_writes_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(
        hass, monkeypatch, hass_storage, peaks={"L1": 16.0, "L2": 18.0, "L3": 17.4}
    )
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    report = await _hardware_test(control, hass, site)
    assert report["modes"]["charge"]["outcome"] == "not_judgeable:no_phase_room"
    assert "via" not in report["modes"]["charge"]
    assert report["modes"]["discharge_house"]["outcome"] == "obeyed", "the test goes on"
    assert _charge_targets(plant) == []


async def test_without_the_targets_the_charge_step_stays_left_for_the_balanced_cap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage, controls=False)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    report = await _hardware_test(control, hass, site)
    assert report["modes"]["charge"]["outcome"] == "not_judgeable:grid_charge_cap"


async def test_the_routine_s_charge_per_phase_lets_control_charge_only_per_phase(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    plan: dict[str, Any] = {"slot": None}
    control = BatteryControl(hass, controller, plan_source=lambda _now: plan["slot"])
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    now = await _looks(control, dt_util.now(), 400)
    report = control.report()
    assert report["modes"]["charge"]["outcome"] == "obeyed" and report["modes"]["charge"]["via"] == "per_phase"
    assert report["modes"]["per_phase"]["outcome"] == "obeyed"
    assert gate.per_phase(report) == "verified" and "grid_charge" in control.allowed()
    # A grid charge in the plan runs per phase, within each phase's cap.
    plant.calls.clear()
    plan["slot"] = ("grid_charge", 12.0, None)
    now = await _looks(control, now, 8)
    assert world.targets() == (-0.92, 0.0, 0.0)
    assert not any(call[2].get("option") == "Command Charging (Grid First)" for call in plant.calls)
    # A reading per phase gone: the charge per phase cannot run, and the balanced one was never shown: none.
    plan["slot"] = None
    now = await _looks(control, now, 8)
    monkeypatch.setattr(BatteryControl, "_phase_headroom_a", lambda self: None)
    control._look_id += 1  # `_phase_missing` is read once a look
    plan["slot"] = ("grid_charge", 12.0, None)
    assert control._control_slot(now, control.options()) == ("self_consume", 0.0, None)
    assert control._restricted == "not_verified:grid_charge"


def test_the_charge_per_phase_tries_the_phase_with_the_largest_cap_in_whole_amps() -> None:
    from custom_components.spotnav.battery.verify import phase_charge_step

    # At most 1 kW and the phase's cap, in whole amps at 230.9 V; none under 0.5 kW; the first of equals.
    assert phase_charge_step((1.02, 0.0, 0.14), 400.0) == (0, 0.923)
    assert phase_charge_step((0.0, 0.7, 0.6), 400.0) == (1, 0.692)
    assert phase_charge_step((0.3, 0.0, 0.46), 400.0) is None
    assert phase_charge_step((2.0, 2.0, 0.0), 400.0) == (0, 0.923)


@pytest.mark.parametrize(
    ("house_a", "outcome"),
    [(17.5, "not_judgeable:fuse_headroom"), (16.5, "not_judgeable:fuse_headroom"), (15.0, "obeyed")],
)
async def test_the_charge_per_phase_is_judged_at_what_the_live_room_leaves_and_not_at_all_without_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], house_a: float, outcome: str
) -> None:
    # L1's house high in the charge step (its learned peak still leaves a cap). With a little room, from the step's
    # start: the charge is lowered to it in whole amps (3 A) and judged at that. With 1 A of room (0.23 kW, which a
    # plant doing nothing would read as within its tolerance) or none under the 2 A kept, from the charge itself on (every target at 0 first, as the step starts, and a step that starts with no
    # room is the start rule's): the executor keeps the charge at nothing, so the step is left for later, not failed.
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    original = control._async_verify

    async def verify(now):
        verification = control.verification
        current = None if verification is None else verification.current
        probe = None if verification is None else verification.probe()
        if outcome == "obeyed":
            high = current is not None and current.mode == "charge"
        else:
            high = probe is not None and any(value > 0 for value in probe)
        world.house_kw[0] = house_a * VOLTS / 1000 if high else HOUSE_KW / 4
        return await original(now)

    monkeypatch.setattr(control, "_async_verify", verify)
    report = await _hardware_test(control, hass, site)
    row = report["modes"]["charge"]
    assert row["outcome"] == outcome, row["phases"]
    assert report["modes"]["discharge_house"]["outcome"] == "obeyed"
    assert row["phases"][0]["target_kw"] == -1.0 and row["phases"][0]["outcome"] == "obeyed"
    if outcome != "obeyed":
        assert len(row["phases"]) == 1, "nothing judged at a charge that small"
        assert all(value >= -0.231 for _entity_id, value in _charge_targets(plant)), "at most 1 A written"
    else:
        assert row["phases"][1]["target_kw"] == pytest.approx(0.923)
        assert [reading["target_kw"] for reading in row["phases"][1]["readings"]] == [pytest.approx(0.69)] * 2
        assert (TARGETS[0], -0.69) in _charge_targets(plant)


# --- fuse review S3: what SpotNav itself adds to the house stands in the HA-down cap ---------------------------------


async def test_fuse_s3_a_standing_boost_or_raise_comes_off_each_phases_ha_down_cap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """The owner's L1 cap is 1.0 kW (20 A less 2 A, less the learned 13.6 A). A raise held drives the compressor
    1 kW above anything learned, a boost SpotNav started its learned power on one phase: each comes off every phase's
    cap (and off the balanced cap's room), since either stays if Home Assistant stops."""
    from types import SimpleNamespace

    from custom_components.spotnav.heat.dhw_ledger import SOURCE_BUTTON, SOURCE_PLAN

    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage, peaks={"L1": 13.6, "L2": 9.0, "L3": 9.0})
    control = await _control(hass, site, controller, _charge)
    now = dt_util.now()
    caps = control._phase_caps_kw(now)
    balanced = control._grid_charge_cap_kw(now, control.options())
    assert caps[0] == pytest.approx(4.4 * VOLTS / 1000)
    raise_a = 1000.0 / VOLTS
    monkeypatch.setattr(controller.heat_control, "raised", True)
    held = control._phase_caps_kw(now)
    assert held == pytest.approx(tuple(cap - raise_a * VOLTS / 1000 for cap in caps))
    assert control._grid_charge_cap_kw(now, control.options()) == pytest.approx(balanced - 3 * 1.0)
    monkeypatch.setattr(controller.heat_control, "raised", False)
    # A boost SpotNav (or the button) started may run: 2 kW on one phase, 8.7 A off each phase.
    hot_water = controller.heat_shadow.hot_water
    hot_water.boost_kw = 2.0
    monkeypatch.setattr(hot_water, "route", lambda *args, **kwargs: SimpleNamespace(kind="one_shot", entity_id="select.x"))
    for source in (SOURCE_PLAN, SOURCE_BUTTON):
        monkeypatch.setattr(hot_water.ledger, "owner", lambda _entity_id, _now, source=source: source)
        assert control._phase_caps_kw(now) == pytest.approx((0.0, caps[1] - 2.0, caps[2] - 2.0))
    # The household's own boost is in the learned peak already: nothing more.
    monkeypatch.setattr(hot_water.ledger, "owner", lambda _entity_id, _now: None)
    assert control._phase_caps_kw(now) == pytest.approx(caps)


# --- fuse review S4: the hardware test reads the house with the targets at 0 only after they settled ---------------


async def test_fuse_s4_the_hardware_test_reads_the_house_baseline_after_the_zeros_settled(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """A battery that was charging just before must not show in the house per phase that the mapping is judged
    against: the zeros are read back, then `ZERO_SETTLE_S` passes before that reading, and the test still ends
    within its ten minutes."""
    from custom_components.spotnav.battery.verify import ZERO_SETTLE_S, Timing, Verification

    assert Timing.quick().zero_settle_s == ZERO_SETTLE_S >= 10.0
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    baselines: list[float] = []
    original = Verification._read_phase

    def read_phase(self, now, result, sample, commanded):
        if self.probe_index < 0 and result.taken_at is not None:
            baselines.append((now - result.taken_at).total_seconds())
        return original(self, now, result, sample, commanded)

    monkeypatch.setattr(Verification, "_read_phase", read_phase)
    report = await _hardware_test(control, hass, site)
    assert report["modes"]["charge"]["outcome"] == "obeyed"
    assert baselines and min(baselines) >= ZERO_SETTLE_S


async def test_a_charge_shown_only_per_phase_keeps_running_per_phase_once_the_ha_down_risk_is_accepted(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """Accepted, the phases' caps add up to the balanced cap (both the empty house's fuse): the charge per phase must
    not give way to a balanced charge that was never shown (`charge` passed `via: per_phase`)."""
    site, controller, plant, world = await _world(hass, monkeypatch, hass_storage)
    control = await _control(hass, site, controller, _charge)
    key = control.firmware_key()
    control.reports[key]["modes"]["charge"]["via"] = "per_phase"
    assert gate.charge_by_phases(control.report())
    _options(hass, site, accept_ha_down=True)
    now = await _looks(control, dt_util.now(), 8)
    cap, caps = control._charge_limits(now, control.options())
    assert caps is not None and cap == pytest.approx(sum(caps))
    assert caps == pytest.approx((19.0 * VOLTS / 1000,) * 3), "each phase: the fuse less the site's 1 A"
    assert "grid_charge" in control.allowed() and control._control_slot(now, control.options())[0] == "grid_charge"
    assert control.executor.control.phased and any(target < 0 for target in world.targets())
    assert not any(call[2].get("option") == "Command Charging (Grid First)" for call in plant.calls)
    assert control.status()["per_phase"]["used"] is True
