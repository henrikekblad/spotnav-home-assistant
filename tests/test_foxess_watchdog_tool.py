"""The supervised FoxESS watchdog test (`tools/foxess_watchdog_test.py`): what it refuses to start on, that it caps the
charge current before Force Charge and never raises it, that it puts everything back with Self Use first (also after
an abort), that a dry run writes nothing, and what its summary says about the inverter's fallback."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).parent.parent / "tools"


def _tool() -> Any:
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("foxess_watchdog_test", TOOLS / "foxess_watchdog_test.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _tool()
E = tool.default_entities("foxess")
#: kW per ampere of charge current on the fake's high-voltage battery.
KW_PER_A = 0.35


class FakeHA:
    """A FoxESS inverter behind Home Assistant: Force Charge charges at the force power within the current cap; a
    reload of the integration leaves the inverter in its fallback mode (`fallback`), which charges at the cap."""

    def __init__(self, *, mode: str = "Self Use", soc: str = "50", spotnav_writes: bool = False,
                 fallback: str = "Back-up", cap_holds: bool = True, dry_run: bool = False) -> None:
        self.values = {E["work_mode"]: mode, E["force_charge_power"]: "0.0", E["max_charge_current"]: "25.0",
                       E["battery"]: "0.5", E["soc"]: soc}
        self.writes: list[str] = []
        self.spotnav_writes = spotnav_writes
        self.fallback, self.cap_holds, self.dry_run = fallback, cap_holds, dry_run

    def _battery(self) -> None:
        mode, cap = self.values[E["work_mode"]], float(self.values[E["max_charge_current"]])
        limit = cap * KW_PER_A if self.cap_holds else 10.0
        if mode == "Force Charge":
            charge = min(float(self.values[E["force_charge_power"]]), limit)
        elif mode == "Back-up":
            charge = limit
        else:
            charge = -0.5
        self.values[E["battery"]] = f"{-charge:.2f}"

    def states(self) -> dict[str, dict[str, Any]]:
        self._battery()
        options = ["Self Use", "Feed-in First", "Back-up", "Force Charge", "Force Discharge"]
        return {entity: {"entity_id": entity, "state": value,
                         "attributes": {"options": options} if entity == E["work_mode"] else {}}
                for entity, value in self.values.items()}

    def get(self, path: str) -> Any:
        if path.startswith("/api/config/config_entries"):
            return [{"entry_id": "site", "title": "My Home", "state": "loaded"}]
        return {"data": {"config": {"battery_control_enabled": True,
                                    "battery_control_shadow_only": not self.spotnav_writes}}}

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        if self.dry_run:
            raise RuntimeError("dry run")
        if service == "reload_config_entry":
            self.writes.append("reload")
            # The integration's remote control was on (Force Charge): the inverter falls back to the mode the
            # integration wrote before enabling it, and the select shows it after the reload.
            if self.values[E["work_mode"]] == "Force Charge":
                self.values[E["work_mode"]] = self.fallback
            return
        entity = data["entity_id"]
        value = data.get("value", data.get("option"))
        self.writes.append(f"{entity}={value}")
        self.values[entity] = str(float(value)) if domain == "number" else str(value)


def _args(**overrides: Any) -> argparse.Namespace:
    values = {"grid_entity": "", "cap_a": 3.0, "charge_kw": 1.0, "charge_s": 20, "zero_s": 10, "observe_s": 30,
              "interval_s": 5, "max_soc": 90, "abort_charge_kw": 2.0, "abort_kw": 11.0, "manual_restart": False}
    return argparse.Namespace(**(values | overrides))


def _test(ha: FakeHA, **overrides: Any) -> Any:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return tool.Test(ha, E, _args(**overrides), csv.writer(io.StringIO()), clock=lambda: now[0], sleep=sleep)


@pytest.mark.parametrize(
    ("ha", "why"),
    [
        (FakeHA(mode="Feed-in First"), "Self Use first"),
        (FakeHA(spotnav_writes=True), "SpotNav's battery control may write"),
        (FakeHA(soc="95"), "under 90"),
    ],
)
def test_it_refuses_to_start_unless_the_plant_is_in_self_use_spotnav_is_quiet_and_the_battery_has_room(
    ha: FakeHA, why: str
) -> None:
    test = _test(ha)
    with pytest.raises(tool.Stop, match=why):
        test.check()
    test.restore()
    assert ha.writes == []


def test_a_dry_run_writes_nothing() -> None:
    ha = FakeHA(dry_run=True)
    test = _test(ha)
    test.check()
    test.restore()
    assert ha.writes == []


def test_it_caps_the_current_forces_a_charge_reloads_and_reports_the_fallback_then_restores_self_use_first() -> None:
    ha = FakeHA()
    test = _test(ha)
    test.check()
    try:
        test.run()
    finally:
        test.restore()
    order = [write.split("=")[0] for write in ha.writes]
    # The cap before Force Charge; the reload with Force Charge at 0.
    assert ha.writes[:3] == [f"{E['max_charge_current']}=3.0", f"{E['force_charge_power']}=1.0",
                             f"{E['work_mode']}=Force Charge"]
    assert ha.writes[3:5] == [f"{E['force_charge_power']}=0.0", "reload"]
    # Restore: Self Use first, then the numbers as found.
    assert order[5:] == [E["work_mode"], E["force_charge_power"], E["max_charge_current"]]
    assert ha.values[E["work_mode"]] == "Self Use" and ha.values[E["max_charge_current"]] == "25.0"
    summary = test.summary
    assert summary["force_charge_kw"] == 1.0 and summary["force_zero_idles"]
    assert summary["work_mode_at_end"] == "Back-up" and summary["fallback_charges"]
    assert summary["fallback_charge_kw"] == pytest.approx(3.0 * KW_PER_A)
    assert summary["self_use_after_restore"]


def test_a_fallback_that_ignores_the_cap_is_aborted_and_everything_is_put_back() -> None:
    ha = FakeHA(cap_holds=False)
    test = _test(ha, charge_kw=0.5)
    test.check()
    with pytest.raises(tool.Stop, match="charges"):
        test.run()
    test.restore()
    assert ha.values[E["work_mode"]] == "Self Use"
    assert ha.values[E["max_charge_current"]] == "25.0" and ha.values[E["force_charge_power"]] == "0.0"


def test_the_cap_is_never_raised_above_what_was_found() -> None:
    ha = FakeHA()
    ha.values[E["max_charge_current"]] = "2.0"
    test = _test(ha)
    test.check()
    test.run()
    test.restore()
    assert ha.writes[0] == f"{E['max_charge_current']}=2.0" and test.summary["cap_a"] == 2.0
