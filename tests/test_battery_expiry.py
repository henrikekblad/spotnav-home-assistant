"""A recipe's expiry (`commands.Expiry`): how a command ends by itself when Home Assistant stops writing, and its
validation."""

from __future__ import annotations

import math
import re

import pytest

from custom_components.spotnav.battery.commands import NO_EXPIRY, Expiry


def test_no_expiry_is_none_renewed_by_nobody() -> None:
    assert NO_EXPIRY.kind == "none" and NO_EXPIRY.renewer == "nobody" and NO_EXPIRY.window_s is None
    assert NO_EXPIRY.renewed_roles == () and NO_EXPIRY.evidence == "stated"


def test_valid_expiries() -> None:
    Expiry("device_deadman", "spotnav", 60.0, ("a", "b"), renew_s=20)
    Expiry("device_lease", "spotnav", 1800.0, ("forcible",))
    Expiry("device_lease", "nobody", 1800.0)
    Expiry("device_deadman", "integration", 30.0, integration="ecoflow")
    Expiry("soc_bound", "spotnav", 1800.0, ("reserve",))
    Expiry("clock_window", "nobody", 900.0)
    Expiry("device_deadman", "spotnav", 60.0, ("a",), renew_s=20, evidence="measured",
           measured_on=frozenset({"fw | x 1"}))
    Expiry("device_lease", "spotnav", 60.0, ("a",), renew_s=20, fallback_values=(("a", "Disabled"),))


@pytest.mark.parametrize(
    "kwargs",
    [
        # SpotNav renews: roles needed; nobody or an integration: none.
        {"kind": "device_deadman", "renewer": "spotnav", "window_s": 60.0},
        {"kind": "device_lease", "renewer": "integration", "window_s": 60.0, "renewed_roles": ("a",)},
        {"kind": "none", "renewer": "spotnav", "renewed_roles": ("a",)},
        {"kind": "clock_window", "renewer": "spotnav", "window_s": 900.0, "renewed_roles": ("a",)},
        # A dead-man nobody renews.
        {"kind": "device_deadman", "renewer": "nobody", "window_s": 60.0},
        # The window.
        {"kind": "device_lease", "renewer": "nobody"},
        {"kind": "device_lease", "renewer": "nobody", "window_s": 0.0},
        {"kind": "device_lease", "renewer": "nobody", "window_s": -5.0},
        {"kind": "device_lease", "renewer": "nobody", "window_s": math.inf},
        {"kind": "device_lease", "renewer": "nobody", "window_s": math.nan},
        # The renewal inside the window.
        {"kind": "device_deadman", "renewer": "spotnav", "window_s": 60.0, "renewed_roles": ("a",), "renew_s": 60},
        {"kind": "device_deadman", "renewer": "spotnav", "window_s": 60.0, "renewed_roles": ("a",), "renew_s": 0},
        # A window under the shared renewal needs its own; a fallback value is for a renewed role.
        {"kind": "device_deadman", "renewer": "spotnav", "window_s": 60.0, "renewed_roles": ("a",)},
        {"kind": "device_deadman", "renewer": "spotnav", "window_s": 60.0, "renewed_roles": ("a",), "renew_s": 20,
         "fallback_values": (("b", "x"),)},
        # Measured names its firmware.
        {"kind": "device_deadman", "renewer": "spotnav", "window_s": 60.0, "renewed_roles": ("a",),
         "evidence": "measured"},
    ],
)
def test_invalid_expiries_raise(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        Expiry(**kwargs)


def test_expiry_id_tells_a_phase_expiry_from_a_balanced_one() -> None:
    phase = Expiry("device_deadman", "spotnav", 60.0, ("phase_l1_target", "phase_l2_target"), renew_s=20)
    lease = Expiry("device_lease", "spotnav", 1800.0, ("forcible",))
    assert phase.expiry_id == "device_deadman:60:phase_l1_target,phase_l2_target"
    assert lease.expiry_id == "device_lease:1800:forcible"
    assert NO_EXPIRY.expiry_id == "none:-:"


def test_a_role_is_read_back_by_the_devices_answer_exactly_where_it_has_one() -> None:
    from custom_components.spotnav.battery.adapters import Match  # noqa: PLC0415
    from custom_components.spotnav.battery.commands import Answer, Role  # noqa: PLC0415

    match = Match(("number",), re.compile("x"))
    answer = Answer("t", "s", frozenset({"ack"}))
    Role("a", "service", match, answer=answer, readback="answer")
    with pytest.raises(ValueError):
        Role("a", "service", match, answer=answer)
    with pytest.raises(ValueError):
        Role("a", "number", match, readback="answer")


def test_read_back_kinds_per_brand_keep_the_old_control_facts() -> None:
    from custom_components.spotnav.battery.commands import CONTROLS  # noqa: PLC0415

    kinds = {control.brand: {role.name: role.readback for role in control.roles} for control in CONTROLS}
    assert set(kinds["sigenergy"].values()) == {"register"} and set(kinds["solinteg"].values()) == {"register"}
    assert kinds["solax"] == {"autorepeat": "echo", "active_power": "echo", "power_control": "echo", "trigger": "none"}
    assert set(kinds["goodwe"].values()) == {"echo"} and set(kinds["tesla"].values()) == {"echo"}
    assert kinds["ferroamp"] == {"command": "answer"}
    # EcoFlow's controls are the integration's own state: the battery's power judges the effect.
    assert set(kinds["ecoflow"].values()) == {"echo"}
    # Solis's roles are each read back by their registers (the force from its switches and the powers' sensors).
    assert set(kinds["solis"].values()) == {"register"}
    # Kostal's switches and rate are the integration's own state (its loops send them); 1080 is read, never written.
    assert kinds["kostal"] == {"management_mode": "register", "block_discharge": "echo", "charge_rate": "echo",
                               "charge_start": "echo"}
    local = {control.brand for control in CONTROLS if control.local_readback}
    assert local == {"solax", "goodwe", "tesla", "ferroamp", "ecoflow", "kostal"}


def test_a_recipe_measurement_of_a_phase_expiry_names_its_phases() -> None:
    from custom_components.spotnav.battery.gate import expiry_measured, expiry_phases_measured  # noqa: PLC0415

    key = "fw | x 1"
    on_l1 = Expiry("device_deadman", "spotnav", 60.0, ("a",), renew_s=20, evidence="measured",
                   measured_on=frozenset({key}), measured_phases=frozenset({"L1"}))
    assert expiry_phases_measured(on_l1, key, None) == (True, False, False)
    named_none = Expiry("device_deadman", "spotnav", 60.0, ("a",), renew_s=20, evidence="measured",
                        measured_on=frozenset({key}))
    assert expiry_phases_measured(named_none, key, None) == (False, False, False)
    # Measured on the whole command: counts there (a balanced expiry), never for a phase.
    assert expiry_measured(named_none, key) and not expiry_measured(named_none, key, None, "L2")
    for bad in (frozenset({"L4"}),):
        with pytest.raises(ValueError):
            Expiry("device_deadman", "spotnav", 60.0, ("a",), renew_s=20, evidence="measured",
                   measured_on=frozenset({key}), measured_phases=bad)
    with pytest.raises(ValueError):
        Expiry("device_deadman", "spotnav", 60.0, ("a",), renew_s=20, measured_phases=frozenset({"L1"}))
