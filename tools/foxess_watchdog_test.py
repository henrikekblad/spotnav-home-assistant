#!/usr/bin/env python3
"""Supervised on-site test: what does a FoxESS inverter do when the `foxess_modbus` remote control stops being
renewed, and which work mode does it show afterwards?

SpotNav's FoxESS control is limits only (`commands.FOXESS_CONTROL`): it has no grid charge, because the integration's
only grid charge on H3, H3-Pro and 1KOMMA5 is its remote control ("Force Charge" in the work mode select), and before
enabling it the integration writes a fallback work mode, Back-up, which the inverter runs once the remote control
times out (2 x the poll rate after the last renewal). The integration never writes the mode back after a restart. Back-up
charges as hard as the inverter can (the integration's own comment), so a Home Assistant that stops mid-charge could
leave a plant charging at full power. This test shows, on the tester's plant, what really happens.

    python tools/foxess_watchdog_test.py --url http://HA:8123 --token-file ~/tokens/ha-token [--prefix foxess] \
        [--grid-entity sensor.grid_power] [--dry-run]

Steps (the integration's entities only, through Home Assistant's services):

1. `cap`: the battery's max charge current is lowered to `--cap-a` (default 3 A, about 1 kW on a high-voltage
   battery; never raised above what it was), so that whatever the inverter does later charges at most that.
2. `force`: force charge power `--charge-kw` (default 1 kW), work mode "Force Charge", watched `--charge-s`. Expected:
   the battery charges at about the lesser of the power and the cap.
3. `zero`: force charge power 0, watched `--zero-s`. Question: does the battery stand idle (a possible hold)?
4. `watchdog`: with Force Charge at 0, the `foxess_modbus` config entry is reloaded (or, with `--manual-restart`,
   you restart Home Assistant yourself when asked). A reload is the same as a restart for the integration: it comes
   back with remote control off on its side and does not renew the inverter's. Watched `--observe-s`. Questions: what
   does the work mode read afterwards (Back-up?), and does the battery start charging from the grid by itself (when,
   how hard, and does it keep to the capped current)?
5. `restore` (always, also on Ctrl-C, an abort or an error): work mode "Self Use" first (it stops any charge), then
   force charge power and max charge current as found.

Abort (straight to restore): the battery charges more than `--abort-charge-kw` (default 2 kW: the cap did not hold),
or the site imports more than `--abort-kw` (with `--grid-entity`).

Before running: SpotNav's battery control off or shadow only (checked), no automation writing the inverter, the work
mode "Self Use" (checked), no sun (the battery must not be charging when it starts; checked), the battery under
`--max-soc` (checked), someone at the plant who can switch to Self Use in the Fox app if Home Assistant does not come
back. Send the CSV log and the summary printed at the end. The token is never printed.
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sigen_import_limit_test import HomeAssistant, number, spotnav_writes, text  # noqa: E402

SELF_USE = "Self Use"
FORCE_CHARGE = "Force Charge"
#: Readings that are no reading (the integration reloading, the inverter not answering).
NO_READING = frozenset({"", "unknown", "unavailable"})
#: A battery charge (kW) that counts as charging.
CHARGING_KW = 0.15


class Stop(Exception):
    pass


def default_entities(prefix: str) -> dict[str, str]:
    """`foxess_modbus`'s entity ids: `<domain>.<prefix>_<key>`, or `<domain>.<key>` without a prefix."""
    p = f"{prefix}_" if prefix else ""
    return {
        "work_mode": f"select.{p}work_mode",
        "force_charge_power": f"number.{p}force_charge_power",
        "max_charge_current": f"number.{p}max_charge_current",
        "battery": f"sensor.{p}invbatpower",
        "soc": f"sensor.{p}battery_soc",
        "soc_1": f"sensor.{p}battery_soc_1",
    }


class Test:
    def __init__(
        self, ha: Any, entities: dict[str, str], args: argparse.Namespace, writer: Any,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ha, self.e, self.args, self.writer = ha, entities, args, writer
        self.clock, self.sleep = clock, sleep
        self.snapshot: dict[str, float] = {}
        self.touched = False
        self.step = "check"
        self.summary: dict[str, Any] = {}

    def read(self) -> dict[str, dict[str, Any]]:
        return self.ha.states()

    def sample(self, marker: str = "") -> dict[str, Any]:
        states = self.read()
        battery = number(states.get(self.e["battery"]))
        # `invbatpower` is discharge-positive (kW): a charge is its negative.
        charge = None if battery is None else -battery
        soc = number(states.get(self.e["soc"])) or number(states.get(self.e["soc_1"]))
        grid = number(states.get(self.args.grid_entity)) if self.args.grid_entity else None
        mode = text(states.get(self.e["work_mode"]))
        row = {"at": self.clock(), "charge_kw": charge, "soc": soc, "grid_kw": grid, "work_mode": mode,
               "force_kw": number(states.get(self.e["force_charge_power"])),
               "cap_a": number(states.get(self.e["max_charge_current"]))}
        stamp = datetime.now().isoformat(timespec="seconds")
        self.writer.writerow([stamp, self.step, marker, charge, soc, grid, mode, row["force_kw"], row["cap_a"]])
        fmt = lambda v: "   -  " if v is None else f"{v:6.2f}"  # noqa: E731
        print(f"{stamp[11:]} {self.step:<10} charge {fmt(charge)} kW  grid {fmt(grid)}  soc {fmt(soc)}  "
              f"mode {mode:<14} force {fmt(row['force_kw'])} cap {fmt(row['cap_a'])} A"
              f"{'  << ' + marker if marker else ''}")
        if charge is not None and charge > self.args.abort_charge_kw:
            raise Stop(f"the battery charges {charge:.2f} kW, over {self.args.abort_charge_kw} kW")
        if grid is not None and grid > self.args.abort_kw:
            raise Stop(f"the site imports {grid:.2f} kW, over {self.args.abort_kw} kW")
        return row

    def watch(self, seconds: float) -> list[dict[str, Any]]:
        seen = [self.sample()]
        end = self.clock() + seconds
        while self.clock() < end:
            self.sleep(self.args.interval_s)
            seen.append(self.sample())
        return seen

    def set(self, role: str, value: Any) -> None:
        entity = self.e[role]
        domain = entity.split(".", 1)[0]
        self.touched = True
        if domain == "number":
            self.ha.call("number", "set_value", {"entity_id": entity, "value": value})
        else:
            self.ha.call("select", "select_option", {"entity_id": entity, "option": value})
        end = self.clock() + 30
        while True:
            row = self.read().get(entity)
            got = number(row) if domain == "number" else None
            ok = (got is not None and abs(got - float(value)) < 0.06) if domain == "number" else text(row) == value
            if ok:
                print(f"  {entity} = {value}")
                return
            if self.clock() >= end:
                raise RuntimeError(f"{entity} did not read back {value} within 30 s")
            self.sleep(1)

    def check(self) -> None:
        states = self.read()
        missing = [role for role in ("work_mode", "force_charge_power", "max_charge_current", "battery")
                   if self.e[role] not in states]
        if missing:
            raise Stop(f"entities missing: {[self.e[role] for role in missing]} (is --prefix right?)")
        mode = states[self.e["work_mode"]]
        if text(mode) != SELF_USE:
            raise Stop(f"the work mode reads {text(mode)!r}: set it to Self Use first")
        if FORCE_CHARGE not in (mode.get("attributes") or {}).get("options", []):
            raise Stop("the work mode has no Force Charge: this model has no remote control in the integration")
        writers = spotnav_writes(self.ha)
        if writers:
            raise Stop(f"SpotNav's battery control may write ({writers}): set it off or shadow only first")
        first = self.sample("start")
        if first["charge_kw"] is None:
            raise Stop(f"{self.e['battery']} reads nothing")
        if first["charge_kw"] > CHARGING_KW:
            raise Stop("the battery is charging already (sun?): run it after dark")
        if first["soc"] is not None and first["soc"] >= self.args.max_soc:
            raise Stop(f"the battery is at {first['soc']:.0f} %: run it under {self.args.max_soc:.0f} %")
        for role in ("force_charge_power", "max_charge_current"):
            value = number(states[self.e[role]])
            if value is None:
                raise Stop(f"{self.e[role]} reads nothing")
            self.snapshot[role] = value
        print(f"snapshot: {self.snapshot}")

    def restore(self) -> None:
        if not self.touched:
            return
        self.step = "restore"
        print("\nrestoring...")
        for role, value in (("work_mode", SELF_USE), ("force_charge_power", self.snapshot.get("force_charge_power")),
                            ("max_charge_current", self.snapshot.get("max_charge_current"))):
            if value is None:
                continue
            try:
                self.set(role, value)
            except Exception as err:  # noqa: BLE001 - restore every role whatever fails
                print(f"  RESTORE FAILED {role}: {err}  (set it by hand, in the Fox app if need be)")
        try:
            after = self.watch(min(self.args.observe_s, 30.0))
            self.summary["self_use_after_restore"] = after[-1]["work_mode"] == SELF_USE
            self.summary["charge_after_restore_kw"] = after[-1]["charge_kw"]
        except Exception as err:  # noqa: BLE001 - the summary only
            print(f"  could not read after the restore: {err}")
        print("restored.")

    def _mean_charge(self, rows: list[dict[str, Any]]) -> float | None:
        steady = [row["charge_kw"] for row in rows[len(rows) // 3:] if row["charge_kw"] is not None]
        return round(sum(steady) / len(steady), 2) if steady else None

    def run(self) -> None:
        self.step = "cap"
        cap = min(self.args.cap_a, self.snapshot["max_charge_current"])
        self.set("max_charge_current", cap)
        self.summary["cap_a"] = cap
        self.step = "force"
        self.set("force_charge_power", self.args.charge_kw)
        self.set("work_mode", FORCE_CHARGE)
        self.summary["force_charge_kw"] = self._mean_charge(self.watch(self.args.charge_s))
        self.step = "zero"
        self.set("force_charge_power", 0.0)
        zero = self._mean_charge(self.watch(self.args.zero_s))
        self.summary["force_zero_charge_kw"] = zero
        self.summary["force_zero_idles"] = zero is not None and abs(zero) < CHARGING_KW
        self.step = "watchdog"
        started = self.clock()
        if self.args.manual_restart:
            input("Restart Home Assistant now (Settings, System, Restart), then press Enter here... ")
            self._wait_for_api()
        else:
            self.ha.call("homeassistant", "reload_config_entry", {"entity_id": self.e["work_mode"]})
            print("  foxess_modbus reloaded")
        rows = self.watch(self.args.observe_s)
        readable = [row for row in rows if row["work_mode"] not in NO_READING]
        modes = [row["work_mode"] for row in readable]
        self.summary["work_modes_after"] = sorted(set(modes))
        self.summary["work_mode_at_end"] = modes[-1] if modes else None
        charging = [row for row in rows if row["charge_kw"] is not None and row["charge_kw"] > CHARGING_KW]
        self.summary["fallback_charges"] = bool(charging)
        self.summary["fallback_after_s"] = round(charging[0]["at"] - started) if charging else None
        self.summary["fallback_charge_kw"] = max(row["charge_kw"] for row in charging) if charging else None

    def _wait_for_api(self) -> None:
        end = self.clock() + 600
        while self.clock() < end:
            try:
                if self.e["work_mode"] in self.read():
                    return
            except RuntimeError:
                pass
            self.sleep(5)
        raise Stop("Home Assistant did not come back within 10 minutes: set Self Use in the Fox app")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--prefix", default="", help="the entity prefix chosen in the integration's setup, if any")
    parser.add_argument("--grid-entity", default="", help="the site's grid import (+ import, W or kW), for the abort")
    parser.add_argument("--cap-a", type=float, default=3.0, help="max charge current during the test (A)")
    parser.add_argument("--charge-kw", type=float, default=1.0)
    parser.add_argument("--charge-s", type=float, default=60)
    parser.add_argument("--zero-s", type=float, default=30)
    parser.add_argument("--observe-s", type=float, default=120)
    parser.add_argument("--interval-s", type=float, default=5)
    parser.add_argument("--max-soc", type=float, default=90)
    parser.add_argument("--abort-charge-kw", type=float, default=2.0)
    parser.add_argument("--abort-kw", type=float, default=11.0)
    parser.add_argument("--manual-restart", action="store_true", help="restart Home Assistant by hand instead")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 0 < args.cap_a <= 10:
        parser.error("--cap-a must be over 0 and at most 10 A")
    if not 0 < args.charge_kw <= 2:
        parser.error("--charge-kw must be over 0 and at most 2 kW")

    ha = HomeAssistant(args.url, args.token_file.expanduser().read_text().strip(), dry_run=args.dry_run)
    log = Path(f"foxess_watchdog_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with log.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["time", "step", "marker", "charge_kw", "soc", "grid_kw", "work_mode", "force_kw", "cap_a"])
        test = Test(ha, default_entities(args.prefix), args, writer)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(Stop("terminated")))
        try:
            test.check()
            if args.dry_run:
                print("dry run: checks passed, nothing written")
                return 0
            test.run()
        except (Stop, KeyboardInterrupt, RuntimeError) as err:
            print(f"\nstopped: {err}")
            test.summary["stopped"] = str(err)
        finally:
            test.restore()
    print(f"\nlog: {log}")
    for key, value in test.summary.items():
        print(f"{key}: {value}")
    return 0 if "stopped" not in test.summary else 1


if __name__ == "__main__":
    sys.exit(main())
