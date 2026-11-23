"""Every recipe's control facts and the fuse rules' outputs on them, pinned in one table before the recipes' watchdog,
dead-man, charge block and read-back data move into `commands.Expiry` and per-role read-back kinds: each move must
leave every row as it was (`fixtures/battery/expiry_golden.json`, recorded on v3 at a24bed5a).

The rows: each control in `CONTROLS` and `EXPORT_CONTROLS`, each of its hold variants and, where it has targets per
phase, its phased control: the legacy facts the engine reads (watchdog roles and interval, the charge block, slow,
fixed charge, local read-back, read-back wait, settle, the phase spec's dead-man, per role whether a call carries a
duration and whether the device answers), the state every action wants at two rates, each service role's calls, and
the gate's HA-down caps over a grid of sites.

Regenerate only on purpose, on the engine whose behaviour is wanted: `python -m tests.test_battery_expiry_golden`."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.commands import (
    ACTION_CURTAIL_EXPORT,
    CONTROLS,
    EXPORT_CONTROLS,
    BrandControl,
    Role,
    service_call,
)
from custom_components.spotnav.battery.control import _accept_applies
from custom_components.spotnav.battery.executor import Executor
from custom_components.spotnav.battery.plan_model import ACTION_GRID_CHARGE, ACTION_SELF_CONSUME

FIXTURE = Path(__file__).parent / "fixtures" / "battery" / "expiry_golden.json"
VOLTS = 400.0
FUSES = (16.0, 20.0, 25.0)
MARGINS = (0.0, 2.0)
CARS = (0.0, 10.0)
HOUSE_PEAKS = (None, 3.0)
PHASE_PEAKS = (None, {"L1": 8.0, "L2": 2.0, "L3": 2.0})
ACCEPT = (False, True)
IMPORT_LIMITS = (None, 4.0, 13.8)
#: The site's own safety margin (the accepted and per-phase rules' margin).
SITE_MARGIN_A = 1.0
#: The firmware key the gate is asked about (no recipe is measured on any).
KEY = "V100R001C21SPC117 | sigen 2.0.3"


def _lifts(control: BrandControl, *, phased: bool) -> bool:
    """Whether the grid charge's own expiry lifts its HA-down cap, as the shell reckons it
    (`control.BatteryControl._expiry_lifts`)."""
    base = control.with_phases(False)
    if phased:
        if base.phase_control is None:
            return False
        expiry = base.phase_control.expiry
    else:
        expiry = base.expiry_by_action.get(ACTION_GRID_CHARGE, base.expiry)
    return gate.expiry_lifts(
        expiry, measured=gate.expiry_measured(expiry, KEY), slow=base.slow, fixed_charge=base.fixed_charge
    )


def _controls() -> dict[str, BrandControl]:
    """Each control by a stable name: brand, hold variant, phased."""
    out: dict[str, BrandControl] = {}
    for base in CONTROLS:
        for phased in (False, True):
            if phased and base.phase_control is None:
                continue
            control = base.with_phases(True) if phased else base
            names = control.variant_names() or (None,)
            for name in names:
                variant = control.with_hold(name)
                out[f"{base.brand}/{name or '-'}/{'phased' if phased else 'balanced'}"] = variant
    for base in EXPORT_CONTROLS:
        out[f"{base.brand}/export"] = base
    return out


def _lease(role: Role) -> bool:
    """Whether a service role's call with a power carries the window as its duration."""
    return role.lease


def _service(role: Role, value: str, window_s: int) -> list[Any]:
    try:
        domain, service, data = service_call(
            role.method, "entity.x", value, device_id="D", services=role.services, watchdog_s=window_s,
            device_field=role.device_field, duration=_lease(role),
        )
    except ValueError as err:
        return ["error", str(err)]
    return [domain, service, data]


def _facts(control: BrandControl) -> dict[str, Any]:
    spec = control.phase_control
    return {
        "watchdog_roles": list(control.watchdog_roles),
        "watchdog_renew_s": control.watchdog_renew_s,
        "charge_block_s": control.charge_block_s,
        "slow": control.slow,
        "fixed_charge": control.fixed_charge,
        "local_readback": control.local_readback,
        "readback_s": control.readback_s,
        "settle_s": control.settle_s,
        # The phase spec's dead-man the gate lifts a phase's cap on (none measured).
        "silence_fallback_s": spec.expiry.window_s if spec is not None and _lifts(control, phased=True) else None,
        "roles": {
            role.name: {
                "lease": _lease(role) if role.method == "service" else None,
                "answer": role.answer is not None,
            }
            for role in control.roles
        },
    }


def _wanted(control: BrandControl, window_s: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    actions = [ACTION_SELF_CONSUME, *control.states]
    for action, rate in itertools.product(actions, (1.0, 3.0)):
        out[f"{action}@{rate}"] = control.wanted(
            action, rate, window_s, soc_pct=50, rated_kw=10, capacity_kwh=13.5
        )
        if action in control.house_states:
            out[f"{action}@{rate}/house"] = control.wanted(
                action, rate, window_s, house_kw=1.5, soc_pct=50, rated_kw=10, capacity_kwh=13.5
            )
        if control.phased and action == ACTION_GRID_CHARGE:
            out[f"{action}@{rate}/phases"] = control.wanted(
                action, rate, window_s, (1.0, 2.0, 0.5), soc_pct=50, rated_kw=10, capacity_kwh=13.5
            )
    return out


def _services(control: BrandControl, window_s: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for role in control.roles:
        if role.method != "service":
            continue
        values = ["charge:2000", "discharge:1500", *(kind for kind in role.services if kind not in ("charge", "discharge"))]
        out[role.name] = {value: _service(role, value, window_s) for value in values}
    return out


def _gate(control: BrandControl) -> dict[str, Any]:
    """The HA-down caps as the shell reckons them for this control (`control._phase_caps_kw`, `_grid_charge_cap_kw`,
    `_ha_down_status`), over the grid of sites."""
    phase_lifts, lifts = _lifts(control, phased=True), _lifts(control, phased=False)
    applies = _accept_applies(control)
    phase_tested = control.import_limit_phases is not None and control.import_limit_phases.field_tested
    phase_caps: list[Any] = []
    caps: list[Any] = []
    for fuse, margin, cars, house, peaks, accept in itertools.product(
        FUSES, MARGINS, CARS, HOUSE_PEAKS, PHASE_PEAKS, ACCEPT
    ):
        phase_caps.append(list(gate.grid_charge_phase_caps_kw(
            fuse, VOLTS, margin_a=margin, cars_a=cars, house_peak_kw=house, house_phase_peaks_a=peaks,
            expiry_lifts=phase_lifts, accept_ha_down=accept and applies, site_margin_a=SITE_MARGIN_A,
        )))
        for limit, acts, per_phase in itertools.product(IMPORT_LIMITS, (False, True), sorted({False, phase_tested})):
            caps.append(gate.grid_charge_cap_kw(
                fuse, VOLTS, margin_a=margin, cars_a=cars, house_peak_kw=house,
                plant_phases=control.grid_charge_phases, import_limit_kw=limit, import_limit_acts=acts,
                house_phase_peaks_a=peaks, import_limit_per_phase=per_phase, phase_margin_a=SITE_MARGIN_A,
                accept_ha_down=accept and applies, expiry_lifts=lifts,
            ))
    return {
        "accept_applies": applies,
        "ha_down_lifted": phase_lifts or lifts,
        "phase_caps_kw": phase_caps,
        "caps_kw": caps,
    }


def table() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, control in _controls().items():
        window_s = Executor(control).watchdog_s
        row: dict[str, Any] = {"window_s": window_s, "facts": _facts(control)}
        if control.kind == "export":
            row["wanted"] = {
                f"{action}@0.0": control.wanted(action, 0.0, window_s)
                for action in (ACTION_SELF_CONSUME, ACTION_CURTAIL_EXPORT)
            }
        else:
            row["wanted"] = _wanted(control, window_s)
            row["services"] = _services(control, window_s)
            row["gate"] = _gate(control)
        out[name] = row
    return json.loads(json.dumps(out, default=str))


def test_every_recipe_keeps_its_control_facts_and_fuse_caps_as_recorded() -> None:
    expected = json.loads(FIXTURE.read_text())
    got = table()
    assert set(got) == set(expected)
    for name in expected:
        for part in expected[name]:
            assert got[name][part] == expected[name][part], f"{name}: {part}"


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(table(), indent=1, sort_keys=True) + "\n")
