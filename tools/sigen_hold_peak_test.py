#!/usr/bin/env python3
"""On-site test: does a Sigenergy plant still cover a peak from its battery while SpotNav's hold stands?

A hold that stays in place while Home Assistant is down stops the battery from covering the house. This test asks
whether the plant's own grid import limit (40040, which acts in every Remote EMS mode and was shown to hold each
phase with nothing written, `tools/sigen_import_limit_test.py`) makes the plant discharge anyway when the house goes
over it during a hold. It needs no extra load: the import limit is lowered under the house's present load.

    python tools/sigen_hold_peak_test.py --url http://HA:8123 --token-file ~/tokens/ha-token [--dry-run]

For each of the two Sigenergy holds it compares (on site 2026-11-23 only `pv_first` passed, and it has been SpotNav's
only hold since, `commands.SIGENERGY_HOLDS`):

* `msc_no_discharge`: Remote EMS on, "Maximum Self Consumption", ESS max discharging limit 0;
* `pv_first`: Remote EMS on, "Command Charging (PV First)", ESS max charging limit 0;

it logs `--hold-s` with the import limit as found (the hold alone: the battery should idle and the grid take the
house), then lowers the import limit to `--limit-kw` for `--low-s`, then raises it back. PASS for a variant: with the
low limit the battery discharged at least `--min-discharge-kw` and the grid total came within `--tolerance-kw` of the
limit. Everything written is snapshotted first and put back at the end, on Ctrl-C and on an error: the control mode
to "Maximum Self Consumption", Remote EMS off, then the three limits. Turning Remote EMS off lands the plant in
Maximum Self Consumption, so AI mode must be set again in the Sigen app afterwards.

Before running: no sun (the test reads the house's own load), SpotNav's battery control off or shadow only (checked),
no automation writing the plant, the house drawing at least `--limit-kw` + 0.5 kW. The token is never printed.
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sigen_import_limit_test import (  # noqa: E402
    MODE_MSC,
    HomeAssistant,
    default_entities,
    number,
    spotnav_writes,
    text,
)

MODE_PV_FIRST = "Command Charging (PV First)"
LIMITS = ("import_limit", "charge_limit", "discharge_limit")


class Stop(Exception):
    pass


class Test:
    def __init__(self, ha: HomeAssistant, entities: dict[str, str], args: argparse.Namespace, writer) -> None:
        self.ha, self.e, self.args, self.writer = ha, entities, args, writer
        self.snapshot: dict[str, float] = {}
        self.touched = False
        self.step = "check"

    def read(self) -> dict[str, dict]:
        return self.ha.states()

    def sample(self, marker: str = "") -> dict[str, float | None]:
        states = self.read()
        row = {role: states.get(entity) for role, entity in self.e.items()}
        grid = [number(row[f"grid_{p}"]) for p in "abc"]
        plant = [number(row[f"plant_{p}"]) for p in "abc"]
        battery = number(row["battery"])
        values = {
            "grid_total": None if None in grid else sum(grid),  # type: ignore[arg-type]
            "battery": battery,
            "plant_total": None if None in plant else sum(plant),  # type: ignore[arg-type]
            "pv": number(states.get(self.e["pv"])),
            "import_limit": number(row["import_limit"]),
        }
        stamp = datetime.now().isoformat(timespec="seconds")
        self.writer.writerow([stamp, self.step, marker, *grid, *plant, battery, values["pv"], values["import_limit"],
                              text(row["remote_ems"]), text(row["control_mode"]), text(row["soc"])])
        fmt = lambda v: "   -  " if v is None else f"{v:6.2f}"  # noqa: E731
        print(f"{stamp[11:]} {self.step:<18} grid {' '.join(fmt(v) for v in grid)} = {fmt(values['grid_total'])}"
              f"  bat {fmt(battery)}  limit {fmt(values['import_limit'])}  {text(row['control_mode'])[:28]}"
              f"{'  << ' + marker if marker else ''}")
        if values["grid_total"] is not None and values["grid_total"] > self.args.abort_kw:
            raise Stop(f"grid import over {self.args.abort_kw} kW")
        return values

    def watch(self, seconds: float) -> list[dict[str, float | None]]:
        seen = []
        end = time.time() + seconds
        while time.time() < end:
            seen.append(self.sample())
            time.sleep(self.args.interval_s)
        return seen

    def set(self, role: str, value) -> None:
        entity = self.e[role]
        domain = entity.split(".", 1)[0]
        self.touched = True
        if domain == "number":
            self.ha.call("number", "set_value", {"entity_id": entity, "value": value})
        elif domain == "switch":
            self.ha.call("switch", "turn_on" if value == "on" else "turn_off", {"entity_id": entity})
        else:
            self.ha.call("select", "select_option", {"entity_id": entity, "option": value})
        end = time.time() + 30
        while time.time() < end:
            row = self.read().get(entity)
            got = number(row) if domain == "number" else None
            ok = (got is not None and abs(got - float(value)) < 0.02) if domain == "number" else text(row) == value
            if ok:
                print(f"  {entity} = {value}")
                return
            time.sleep(1)
        raise RuntimeError(f"{entity} did not read back {value} within 30 s")

    def check(self) -> None:
        states = self.read()
        missing = [r for r in ("import_limit", "charge_limit", "discharge_limit", "remote_ems", "control_mode",
                               "battery", "grid_a", "grid_b", "grid_c") if self.e[r] not in states]
        if missing:
            raise Stop(f"entities missing: {missing}")
        if text(states[self.e["remote_ems"]]) != "off":
            raise Stop("Remote EMS is already on: someone else is controlling the plant")
        writers = spotnav_writes(self.ha)
        if writers:
            raise Stop(f"SpotNav's battery control may write ({writers}): set it off or shadow only first")
        for role in LIMITS:
            value = number(states[self.e[role]])
            if value is None:
                raise Stop(f"{self.e[role]} reads nothing")
            self.snapshot[role] = value
        pv = number(states.get(self.e["pv"]))
        dark = [v for v in self.args.variants.split(",") if v != "pv_first_surplus"]
        if dark and pv is not None and pv > 0.1:
            raise Stop(f"the sun is up ({pv:.2f} kW): run {dark} after dark (only pv_first_surplus wants sun)")
        print(f"snapshot: {self.snapshot}")

    def restore(self) -> None:
        if not self.touched:
            return
        self.step = "restore"
        print("\nrestoring...")
        for role, value in (("control_mode", MODE_MSC), ("remote_ems", "off")):
            try:
                if role == "control_mode" and text(self.read().get(self.e["remote_ems"])) != "on":
                    continue
                self.set(role, value)
            except Exception as err:  # noqa: BLE001 - restore every role whatever fails
                print(f"  RESTORE FAILED {role}: {err}")
        for role in LIMITS:
            try:
                self.set(role, self.snapshot[role])
            except Exception as err:  # noqa: BLE001
                print(f"  RESTORE FAILED {role}: {err}")
        print("restored. Set AI mode again in the Sigen app if it was on.")

    def enter(self, name: str, mode: str, zero: str) -> None:
        self.step = f"{name}:enter"
        self.set(zero, 0)
        if text(self.read().get(self.e["remote_ems"])) != "on":
            # The control mode (40031) may still hold a charging command from someone else: Remote EMS goes on
            # with the charge limit at 0, as in `sigen_import_limit_test.py`, and the limit comes back after.
            if zero != "charge_limit":
                self.set("charge_limit", 0)
            self.set("remote_ems", "on")
            self.set("control_mode", mode)
            if zero != "charge_limit":
                self.set("charge_limit", self.snapshot["charge_limit"])
        else:
            self.set("control_mode", mode)

    def variant(self, name: str, mode: str, zero: str, charge_kw: float | None = None) -> dict:
        self.enter(name, mode, zero)
        if charge_kw is not None:
            # A charge limit left standing (Home Assistant died while SpotNav's surplus hold was storing the sun):
            # in "Command Charging (PV First)" with no sun the plant charges it from the grid.
            self.set("charge_limit", charge_kw)
        self.step = f"{name}:hold"
        hold = self.watch(self.args.hold_s)
        house = [v["plant_total"] + v["grid_total"] for v in hold if v["plant_total"] is not None
                 and v["grid_total"] is not None]
        house_kw = sum(house) / len(house) if house else 0.0
        if house_kw < self.args.limit_kw + 0.4:
            print(f"  house only {house_kw:.2f} kW: the low limit would not bind, skipping")
            return {"variant": name, "result": "NOT JUDGED", "house_kw": house_kw}
        self.step = f"{name}:low-limit"
        self.set("import_limit", self.args.limit_kw)
        low = self.watch(self.args.low_s)
        steady = low[len(low) // 3:]
        bat = [v["battery"] for v in steady if v["battery"] is not None]
        grid = [v["grid_total"] for v in steady if v["grid_total"] is not None]
        discharge = -min(bat) if bat else 0.0  # sigen: negative battery power = discharging
        mean_grid = sum(grid) / len(grid) if grid else float("nan")
        self.set("import_limit", self.snapshot["import_limit"])
        self.step = f"{name}:after"
        self.watch(20)
        passed = discharge >= self.args.min_discharge_kw and mean_grid <= self.args.limit_kw + self.args.tolerance_kw
        charging = [v["battery"] for v in hold[len(hold) // 3:] if v["battery"] is not None]
        return {"variant": name, "result": "PASS" if passed else "FAIL", "house_kw": round(house_kw, 2),
                "hold_battery_kw": round(sum(charging) / len(charging), 2) if charging else None,
                "max_discharge_kw": round(discharge, 2), "mean_grid_kw": round(mean_grid, 2)}

    def surplus(self, name: str) -> dict:
        """SpotNav's surplus hold: "Command Charging (PV First)" with the charge limit following the sun's measured
        surplus (PV less the house), so the sun is stored and nothing is taken from the grid. Needs sun."""
        first = self.sample("surplus check")
        house = (first["grid_total"] or 0.0) + (first["plant_total"] or 0.0)
        if (first["pv"] or 0.0) - house < self.args.min_surplus_kw:
            print(f"  surplus only {(first['pv'] or 0.0) - house:.2f} kW: run it in sunshine, skipping")
            return {"variant": name, "result": "NOT JUDGED"}
        self.enter(name, MODE_PV_FIRST, "charge_limit")
        self.step = f"{name}:track"
        seen, written, end = [], 0.0, time.time() + self.args.track_s
        while time.time() < end:
            v = self.sample()
            seen.append(v)
            if None not in (v["pv"], v["grid_total"], v["plant_total"]):
                # House = grid import + the plant's AC output (which already nets the sun and the battery).
                house = v["grid_total"] + v["plant_total"]  # type: ignore[operator]
                want = max(0.0, round(v["pv"] - house, 1))  # type: ignore[operator]
                if abs(want - written) >= self.args.deadband_kw:
                    self.set("charge_limit", want)
                    written = want
            time.sleep(self.args.interval_s)
        steady = seen[len(seen) // 3:]
        grid = [v["grid_total"] for v in steady if v["grid_total"] is not None]
        bat = [v["battery"] for v in steady if v["battery"] is not None]
        mean_grid = sum(grid) / len(grid) if grid else float("nan")
        mean_bat = sum(bat) / len(bat) if bat else 0.0
        passed = abs(mean_grid) <= self.args.tolerance_kw and mean_bat >= self.args.min_surplus_kw / 2
        return {"variant": name, "result": "PASS" if passed else "FAIL", "mean_grid_kw": round(mean_grid, 2),
                "mean_charge_kw": round(mean_bat, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--plant", default="sigen_plant")
    parser.add_argument("--limit-kw", type=float, default=0.6)
    parser.add_argument("--hold-s", type=float, default=60)
    parser.add_argument("--low-s", type=float, default=120)
    parser.add_argument("--interval-s", type=float, default=5)
    parser.add_argument("--min-discharge-kw", type=float, default=0.3)
    parser.add_argument("--tolerance-kw", type=float, default=0.3)
    parser.add_argument("--abort-kw", type=float, default=11.0, help="stop if the grid total goes over this")
    parser.add_argument("--variants", default="msc_no_discharge,pv_first",
                        help="msc_no_discharge, pv_first, pv_first_stale_charge (after dark), pv_first_surplus (sun)")
    parser.add_argument("--stale-charge-kw", type=float, default=1.0,
                        help="pv_first_stale_charge: the charge limit left standing")
    parser.add_argument("--track-s", type=float, default=300, help="pv_first_surplus: how long to follow the sun")
    parser.add_argument("--min-surplus-kw", type=float, default=0.5)
    parser.add_argument("--deadband-kw", type=float, default=0.3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.limit_kw < 0.3:
        parser.error("--limit-kw under 0.3 kW is refused (0 may mean 'no limit' on some firmware)")

    entities = default_entities(args.plant)
    entities["pv"] = f"sensor.{args.plant}_pv_power"
    ha = HomeAssistant(args.url, args.token_file.expanduser().read_text().strip(), dry_run=args.dry_run)
    log = Path(f"sigen_hold_peak_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with log.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["time", "step", "marker", "grid_a", "grid_b", "grid_c", "plant_a", "plant_b", "plant_c",
                         "battery_kw", "pv_kw", "import_limit_kw", "remote_ems", "control_mode", "soc"])
        test = Test(ha, entities, args, writer)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(Stop("terminated")))
        results = []
        try:
            test.check()
            test.sample("start")
            if args.dry_run:
                print("dry run: checks passed, nothing written")
                return 0
            plan = {"msc_no_discharge": (MODE_MSC, "discharge_limit", None),
                    "pv_first": (MODE_PV_FIRST, "charge_limit", None),
                    "pv_first_stale_charge": (MODE_PV_FIRST, "charge_limit", args.stale_charge_kw)}
            for name in args.variants.split(","):
                if name == "pv_first_surplus":
                    results.append(test.surplus(name))
                    test.set("charge_limit", test.snapshot["charge_limit"])
                    continue
                mode, zero, charge_kw = plan[name]
                results.append(test.variant(name, mode, zero, charge_kw))
                test.set(zero, test.snapshot[zero])
        except (Stop, KeyboardInterrupt, RuntimeError) as err:
            print(f"\nstopped: {err}")
        finally:
            test.restore()
    print(f"\nlog: {log}")
    for row in results:
        print(row)
    return 0 if results and all(r["result"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
