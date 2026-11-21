#!/usr/bin/env python3
"""On-site test: does a Sigenergy plant hold each phase's grid import under its own import limit (40040)?

Runs the field test of `plans/v3_sigen_local_fuse_protection.md` §3 on one phase, through Home Assistant's REST API
and the `sigen` integration's entities. Only Python's standard library is needed.

    python tools/sigen_import_limit_test.py --url http://HA:8123 --token-file ~/tokens/ha-token [--dry-run]

What it writes (and nothing else), each value snapshotted first and put back at the end, on Ctrl-C and on an error:

* the grid import limit (40040, `number.<plant>_grid_import_limitation`) to `--limit-kw` (6 kW: 2 kW per phase);
* the ESS max charging limit (40032) to 0 before Remote EMS goes on, then to `--charge-kw` (4 kW);
* Remote EMS (40029) on, and its control mode (40031) to "Command Charging (Grid First)", the mode SpotNav's grid
  charge uses. Back: the mode to "Maximum Self Consumption" (so 40031 keeps no command), Remote EMS off, the limits.

Steps (each waits for Enter; typing `q` and Enter at any prompt stops and restores):

1. Baseline: the charge at `--charge-kw` under the limit. Every phase should stay at or under limit / 3.
2. Load at full charge (the limit binds): a 2-3 kW single-phase load (kettle, oven). Enter when it goes on, Enter
   when it goes off (markers "load on" / "load off"). The phase it landed on is found from the jump in that phase's
   house load (grid + the plant's output on it).
3. Load under the limit (the decisive one): the charge is lowered to `--low-charge-kw` (1 kW) so that the total stays
   under the limit while the loaded phase alone goes over limit / 3. A plant that holds each phase pulls that phase
   down by itself; a plant that only limits the total leaves it over. Only the first lets SpotNav drop its HA-down
   cap (`battery/gate.py`).
4. Writes and polls stopped: the charge back at `--charge-kw`; the script writes nothing until the restore. The
   person disables SpotNav's entries and then the `sigen` integration (Settings > Devices & services > the entry's
   menu > Disable); the script waits ten minutes, the same load runs again with markers, and both are re-enabled.
   While `sigen` is disabled Home Assistant reads nothing of the plant, so the step is judged only on an independent
   per-phase meter at the grid connection (`--meter a=...,b=...,c=...`, any entity not from `sigen`: a HAN/P1
   reader, a Shelly 3EM, a Tibber Pulse). A full PASS needs both: the plant really unread in the window and the
   meter. With `sigen` still read the step is the binding step again and is not judged; without a meter the
   person's y/n from the mySigen app or a clamp meter is kept, labelled as a fallback that cannot record the fact.
   After re-enabling, the script records whether Remote EMS and its mode were kept.
5. Restore, then the pass/fail summary: every phase at or under limit / 3 + 0.1 kW within 15 s of the load going on,
   judged only where the load took its phase over a third of the limit.

Before running it (the script checks what it can and refuses where it must):

* SpotNav's battery control off or shadow only (the script reads SpotNav's diagnostics). SpotNav never starts its
  own control while Remote EMS is on (`remote_ems_already_on`), and a running control that sees its roles changed
  aborts (`changed_outside_spotnav`), so only a control that writes would notice; with it off or shadow only
  SpotNav writes nothing to the plant. It may count the changes as a person trying modes (`outside_control.py`:
  logged, nothing raised) and show "two limits on one fuse" while the limit reads 6 kW; both clear by themselves.
* No car charging: the charger off or the car unplugged (SpotNav's load balancing would move with the test).
* No automation or other integration writing the plant's Remote EMS, mode or limits.
* The Sigen app: whatever mode it is in (AI mode on the owner's plant), Remote EMS overrides it while on; turning
  Remote EMS off lands the plant in Maximum Self Consumption, so set AI mode again in the app afterwards.
* Independent phase power control (40030) on; the plant's active power targets (40001, 40008-40012) at 0 (a target
  left there would act in the seconds of "PCS Remote Control" a Remote EMS switch-on passes through).
* Enable the disabled entity "Grid Sensor Status" (`sensor.<plant>_grid_sensor_status`) if wanted: it is logged.

`--dry-run` reads and logs with markers, checks everything above and writes nothing.
The token is read from the file and never printed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MODE_GRID_FIRST = "Command Charging (Grid First)"
MODE_MSC = "Maximum Self Consumption"
#: Pass criteria (§3): every phase at or under limit / 3 + this, from at most `SETTLE_S` after the load went on.
PHASE_TOLERANCE_KW = 0.1
SETTLE_S = 15.0
#: A step needs this long with the load on to be judged (s).
MIN_LOAD_S = 45.0
#: A phase's house load must be at least this far under a third of the limit before the test starts (kW): a phase
#: already near it would be held without the load, and the load step would show nothing.
BASELINE_ROOM_KW = 0.5
#: The highest `--abort-phase-kw` allowed: 20 A at 230 V, the smallest fuse this test is meant for.
ABORT_PHASE_MAX_KW = 4.6
#: How long the plant runs with nothing written and nothing polled before the stopped step's load (s).
STOPPED_WAIT_S = 600.0
#: How long a write may take to read back (s).
READBACK_S = 30.0


def default_entities(plant: str) -> dict[str, str]:
    """The `sigen` 2.0.3 entity ids for a plant device named `plant` (the owner's: `sigen_plant`)."""
    entities = {
        "import_limit": f"number.{plant}_grid_import_limitation",
        "charge_limit": f"number.{plant}_ess_max_charging_limit",
        "discharge_limit": f"number.{plant}_ess_max_discharging_limit",
        "remote_ems": f"switch.{plant}_remote_ems_controlled_by_home_assistant",
        "control_mode": f"select.{plant}_remote_ems_control_mode",
        "phase_control": f"switch.{plant}_independent_phase_power_control",
        "work_mode": f"sensor.{plant}_ems_work_mode",
        "grid_sensor": f"sensor.{plant}_grid_sensor_status",
        "battery": f"sensor.{plant}_battery_power",
        "soc": f"sensor.{plant}_battery_state_of_charge",
        "rated_charge": f"sensor.{plant}_ess_rated_charging_power",
        "total_target": f"number.{plant}_active_power_fixed_adjustment",
    }
    for phase in "abc":
        entities[f"grid_{phase}"] = f"sensor.{plant}_grid_phase_{phase}_active_power"
        entities[f"plant_{phase}"] = f"sensor.{plant}_phase_{phase}_active_power"
        entities[f"target_{phase}"] = f"number.{plant}_phase_{phase}_active_power_fixed_adjustment"
    return entities


#: Roles that must exist and read a value before anything is written.
REQUIRED = (
    "import_limit", "charge_limit", "remote_ems", "control_mode", "phase_control", "battery",
    "grid_a", "grid_b", "grid_c", "plant_a", "plant_b", "plant_c",
)
#: What the test writes, in the order the restore writes it back.
RESTORED = ("control_mode", "remote_ems", "charge_limit", "import_limit")


class Stop(Exception):
    """The person typed `q`, or the script must stop."""


class HomeAssistant:
    def __init__(self, url: str, token: str, *, dry_run: bool) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self.dry_run = dry_run
        self.writes: list[str] = []

    def _request(self, method: str, path: str, body: Any = None, timeout: float = 10.0) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self._url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:  # never echo the request (it carries the token)
            raise RuntimeError(f"{method} {path}: HTTP {err.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise RuntimeError(f"{method} {path}: {type(err).__name__}") from None
        return json.loads(raw) if raw else None

    def states(self) -> dict[str, dict[str, Any]]:
        return {row["entity_id"]: row for row in self._request("GET", "/api/states")}

    def get(self, path: str) -> Any:
        return self._request("GET", path, timeout=30.0)

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        what = f"{domain}.{service} {json.dumps(data)}"
        if self.dry_run:
            raise RuntimeError(f"dry run: refusing to write {what}")
        self.writes.append(what)
        self._request("POST", f"/api/services/{domain}/{service}", data)


def number(row: dict[str, Any] | None) -> float | None:
    """A state as kW (W converted), `None` when it reads nothing."""
    if row is None:
        return None
    try:
        value = float(row["state"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value / 1000.0 if row.get("attributes", {}).get("unit_of_measurement") == "W" else value


#: The voltage a meter's current is taken at, to compare it with power (V).
METER_VOLTS = 230.0


def meter_kw(row: dict[str, Any] | None) -> float | None:
    """An independent meter's reading as kW: power in W or kW as it is, a current in A at `METER_VOLTS`."""
    if row is not None and row.get("attributes", {}).get("unit_of_measurement") == "A":
        try:
            amps = float(row["state"])
        except (TypeError, ValueError):
            return None
        return amps * METER_VOLTS / 1000.0 if math.isfinite(amps) else None
    return number(row)


def text(row: dict[str, Any] | None) -> str:
    return "" if row is None else str(row.get("state", ""))


@dataclass
class Sample:
    t: float
    step: str
    marker: str
    grid: tuple[float | None, float | None, float | None]
    plant: tuple[float | None, float | None, float | None]
    values: dict[str, str]
    #: An independent meter's import on each phase (kW, `--meter`), where one is given.
    meter: tuple[float | None, float | None, float | None] = (None, None, None)

    def house(self) -> tuple[float | None, ...]:
        """Each phase's house load: the grid's import on it plus the plant's output on it (+ output)."""
        return tuple(
            None if g is None or p is None else g + p for g, p in zip(self.grid, self.plant, strict=True)
        )

    def grid_total(self) -> float | None:
        return None if any(v is None for v in self.grid) else sum(self.grid)  # type: ignore[arg-type]


@dataclass
class Window:
    step: str
    on: float
    off: float | None = None
    stopped: bool = False
    charge_kw: float = 0.0


@dataclass
class Run:
    ha: HomeAssistant
    entities: dict[str, str]
    limit_kw: float
    interval_s: float
    abort_phase_kw: float
    extra: list[str]
    writer: Any
    out: Any
    samples: list[Sample] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    step: str = "check"
    lines: queue.Queue[str] = field(default_factory=queue.Queue)
    guarding: bool = False
    charge_kw: float = 0.0
    over: int = 0
    notes: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    #: An independent per-phase meter's entities (A, B, C), not from the plant's integration (`--meter`).
    meter: tuple[str, str, str] | None = None

    # --- reading -------------------------------------------------------------------------------------------------

    def read(self) -> dict[str, dict[str, Any]]:
        return self.ha.states()

    def sample(self, marker: str = "") -> Sample:
        try:
            states = self.read()
        except RuntimeError as err:
            print(f"  read failed: {err}")
            states = {}
        row = {role: states.get(entity) for role, entity in self.entities.items()}
        sample = Sample(
            time.time(), self.step, marker,
            (number(row["grid_a"]), number(row["grid_b"]), number(row["grid_c"])),
            (number(row["plant_a"]), number(row["plant_b"]), number(row["plant_c"])),
            {
                "battery_kw": text(row["battery"]), "soc_pct": text(row.get("soc")),
                "import_limit_kw": text(row["import_limit"]), "charge_limit_kw": text(row["charge_limit"]),
                "remote_ems": text(row["remote_ems"]), "control_mode": text(row["control_mode"]),
                "work_mode": text(row.get("work_mode")), "grid_sensor": text(row.get("grid_sensor")),
                "phase_control": text(row["phase_control"]),
                **{entity: text(states.get(entity)) for entity in self.extra},
            },
        )
        if self.meter is not None:
            sample.meter = (meter_kw(states.get(self.meter[0])), meter_kw(states.get(self.meter[1])),
                            meter_kw(states.get(self.meter[2])))
        self.samples.append(sample)
        self._write(sample)
        self._print(sample)
        self._guard(sample)
        return sample

    def _write(self, sample: Sample) -> None:
        house = sample.house()
        fmt = lambda v: "" if v is None else f"{v:.3f}"  # noqa: E731
        self.writer.writerow([
            datetime.fromtimestamp(sample.t).isoformat(timespec="seconds"), sample.step, sample.marker,
            *map(fmt, sample.grid), fmt(sample.grid_total()), *map(fmt, sample.plant), *map(fmt, house),
            *sample.values.values(), *map(fmt, sample.meter),
        ])
        self.out.flush()

    def _print(self, sample: Sample) -> None:
        fmt = lambda v: "   -  " if v is None else f"{v:6.2f}"  # noqa: E731
        cap = self.limit_kw / 3.0 + PHASE_TOLERANCE_KW
        flags = "".join("!" if v is not None and v > cap else " " for v in sample.grid)
        print(
            f"{datetime.fromtimestamp(sample.t):%H:%M:%S} {sample.step:<10} grid {' '.join(map(fmt, sample.grid))}"
            f" [{flags}] plant {' '.join(map(fmt, sample.plant))} bat {sample.values['battery_kw']:>6}"
            f" {sample.values['control_mode'][:22]}{'  << ' + sample.marker if sample.marker else ''}"
        )

    def _guard(self, sample: Sample) -> None:
        """Abort and restore when a phase stays over `abort_phase_kw` for two samples while the test's command stands
        (also in the writes-stopped step: the restore then ends that step, which is the safe side)."""
        high = any(v is not None and v > self.abort_phase_kw for v in (*sample.grid, *sample.meter))
        self.over = self.over + 1 if high else 0
        if self.guarding and self.over >= 2:
            raise Stop(f"a phase imported over {self.abort_phase_kw} kW twice in a row: switch the load off")

    # --- prompts -------------------------------------------------------------------------------------------------

    def wait(self, prompt: str, *, least_s: float = 0.0) -> str:
        """Log every `interval_s` until the person presses Enter (after `least_s`, earlier presses are refused)."""
        print(f"\n>>> {prompt}")
        start = time.time()
        while True:
            self.sample()
            deadline = time.time() + self.interval_s
            while time.time() < deadline:
                try:
                    line = self.lines.get(timeout=max(deadline - time.time(), 0.05)).strip().lower()
                except queue.Empty:
                    continue
                if line == "q":
                    raise Stop("stopped by the person")
                if time.time() - start < least_s:
                    print(f"    (wait {least_s - (time.time() - start):.0f} s more first)")
                    continue
                return line

    def marker(self, name: str) -> float:
        sample = self.sample(name)
        return sample.t

    def settle(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.sample()
            try:
                if self.lines.get(timeout=self.interval_s).strip().lower() == "q":
                    raise Stop("stopped by the person")
            except queue.Empty:
                pass

    # --- writing -------------------------------------------------------------------------------------------------

    def set(self, role: str, value: Any, *, wait_s: float | None = None) -> None:
        entity = self.entities[role]
        domain = entity.split(".", 1)[0]
        if domain == "number":
            self.ha.call("number", "set_value", {"entity_id": entity, "value": value})
        elif domain == "switch":
            self.ha.call("switch", "turn_on" if value == "on" else "turn_off", {"entity_id": entity})
        elif domain == "select":
            self.ha.call("select", "select_option", {"entity_id": entity, "option": value})
        else:
            raise RuntimeError(f"cannot write {entity}")
        if role == "charge_limit":
            self.charge_kw = float(value)
        wait_s = READBACK_S if wait_s is None else wait_s
        end = time.time() + wait_s
        row = None
        while time.time() < end:
            row = self.read().get(entity)
            if domain == "number":
                got = number(row)
                if got is not None and abs(got - float(value)) < 0.02:
                    print(f"  {entity} = {value}")
                    return
            elif text(row) == value:
                print(f"  {entity} = {value}")
                return
            time.sleep(min(1.0, wait_s / 10))
        raise RuntimeError(f"{entity} did not read back {value} within {wait_s:.0f} s (reads {text(row)})")

    def wait_available(self, role: str, wait_s: float) -> bool:
        end = time.time() + wait_s
        while time.time() < end:
            if text(self.read().get(self.entities[role])) not in ("", "unavailable", "unknown"):
                return True
            time.sleep(1.0)
        return False


# --- checks and snapshot ---------------------------------------------------------------------------------------------


def spotnav_writes(ha: HomeAssistant) -> list[str]:
    """SpotNav site entries whose battery control may write (enabled and not shadow only), by title; a list with
    "unread" when SpotNav's diagnostics could not be read."""
    found: list[str] = []
    try:
        entries = ha.get("/api/config/config_entries/entry?domain=spotnav") or []
    except RuntimeError:
        return ["unread"]
    for entry in entries:
        if entry.get("state") != "loaded":
            continue
        try:
            diag = ha.get(f"/api/diagnostics/config_entry/{entry['entry_id']}")
        except RuntimeError:
            continue
        config = ((diag or {}).get("data") or {}).get("config") or {}
        if config.get("battery_control_enabled") is True and config.get("battery_control_shadow_only") is not True:
            found.append(str(entry.get("title")))
    return found


def check(run: Run, *, force_spotnav: bool) -> dict[str, Any]:
    """What must read so before the test writes, and the snapshot of every value it writes."""
    states = run.read()
    row = {role: states.get(entity) for role, entity in run.entities.items()}
    problems: list[str] = []
    for role in REQUIRED:
        if role == "control_mode":
            continue  # unavailable while Remote EMS is off
        if text(row[role]) in ("", "unavailable", "unknown") and not (role == "charge_limit" and row[role]):
            problems.append(f"{run.entities[role]} reads {text(row[role]) or 'nothing (missing or disabled)'}")
    if row["control_mode"] is None:
        problems.append(f"{run.entities['control_mode']} is missing or disabled")
    if text(row["remote_ems"]) == "on":
        problems.append("Remote EMS is already on: something else controls the plant (turn it off first)")
    if text(row["phase_control"]) != "on":
        problems.append("independent phase power control (40030) is not on: this test is about the plant with it on")
    for role in ("total_target", "target_a", "target_b", "target_c"):
        value = number(row.get(role))
        if value is not None and abs(value) > 0.001:
            problems.append(f"{run.entities[role]} reads {value} kW, not 0 (it would act as Remote EMS turns on)")
    writers = spotnav_writes(run.ha)
    if writers == ["unread"]:
        run.notes.append("SpotNav's diagnostics could not be read: make sure its battery control is off/shadow only")
    elif writers and not force_spotnav:
        problems.append(f"SpotNav's battery control writes on {', '.join(writers)}: turn it off or to shadow only")
    if text(row.get("grid_sensor")) not in ("online",):
        run.notes.append(f"grid sensor status reads '{text(row.get('grid_sensor')) or 'nothing'}' (enable"
                         f" {run.entities['grid_sensor']} to log it; 'online' is what the plant's limit needs)")
    charge = number(row["charge_limit"])
    if charge is None or charge > 1000:
        rated = number(row.get("rated_charge"))
        run.notes.append(f"the charge limit reads '{text(row['charge_limit'])}' (never set): restored as the rated"
                         f" charge {rated} kW")
        charge = rated
    limit = number(row["import_limit"])
    snapshot = {
        "import_limit": limit, "charge_limit": charge, "remote_ems": text(row["remote_ems"]),
        "control_mode": MODE_MSC,  # handed back like SpotNav does: 40031 keeps no command
        "work_mode": text(row.get("work_mode")), "phase_control": text(row["phase_control"]),
        "discharge_limit": text(row.get("discharge_limit")),
    }
    if limit is None or charge is None:
        problems.append("the import or charge limit cannot be snapshotted, so it could not be restored")
    near = run.limit_kw / 3.0 - BASELINE_ROOM_KW
    for phase in "abc":
        grid, plant = number(row[f"grid_{phase}"]), number(row[f"plant_{phase}"])
        if grid is not None and plant is not None and grid + plant >= near:
            problems.append(f"phase {phase.upper()} already draws {grid + plant:.2f} kW in the house, within"
                            f" {BASELINE_ROOM_KW} kW of a third of the limit: switch large loads off first")
    soc = number(row.get("soc"))
    if soc is not None and soc >= 95.0:
        problems.append(f"the battery is at {soc:.0f} %: too full to take a grid charge for the test")
    return {"problems": problems, "snapshot": snapshot}


#: Signals that stop the test (and restore); all of them are ignored while the restore runs.
STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name))
#: Failed writes of the control mode before the restore moves on to turning Remote EMS off.
MODE_RESTORE_TRIES = 3
#: How long the restore keeps trying, and its pause between tries (s).
RESTORE_DEADLINE_S = 300.0
RESTORE_RETRY_S = 5.0


def snapshot_path(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.stem + ".snapshot.json")


def save_snapshot(path: Path, snapshot: dict[str, Any], entities: dict[str, str]) -> None:
    """Kept on disk before the first write, so `--restore-from` can put the plant back after a crash."""
    path.write_text(json.dumps({"snapshot": snapshot, "entities": entities}, indent=1), encoding="utf-8")


def restore(run: Run, snapshot: dict[str, Any], *, force: bool = False) -> bool:
    """Put back every value the test writes, in the hand-back order; retried for up to five minutes. The control mode
    is skipped only where Remote EMS reads exactly `off` (anything unread is tried again), and given up after
    `MODE_RESTORE_TRIES` failed writes so that Remote EMS still goes off. Stop signals are ignored meanwhile."""
    if not run.ha.writes and not force:
        return True
    run.guarding = False
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in STOP_SIGNALS}
    print("\n=== Restoring ===")
    try:
        end = time.time() + RESTORE_DEADLINE_S
        pending = list(RESTORED)
        mode_fails = 0
        while pending and time.time() < end:
            role = pending[0]
            try:
                if role == "control_mode":
                    remote = text(run.read().get(run.entities["remote_ems"]))
                    if remote == "on":
                        try:
                            run.set("control_mode", snapshot["control_mode"])
                        except RuntimeError:
                            mode_fails += 1
                            if mode_fails < MODE_RESTORE_TRIES:
                                raise
                            print(f"  the control mode would not take after {mode_fails} tries: turning Remote EMS"
                                  " off instead (it lands in Maximum Self Consumption)")
                    elif remote != "off":
                        raise RuntimeError(f"Remote EMS reads '{remote or 'nothing'}'")
                elif role == "remote_ems":
                    run.set("remote_ems", snapshot["remote_ems"] or "off")
                else:
                    run.set(role, round(float(snapshot[role]), 3))
                pending.pop(0)
            except RuntimeError as err:
                print(f"  restore of {role} failed ({err}); trying again (is the sigen integration enabled?)")
                time.sleep(RESTORE_RETRY_S)
        if pending:
            print("\n!!! RESTORE NOT DONE. Set by hand (Home Assistant or the mySigen app):")
            for role in pending:
                print(f"    {run.entities[role]} -> {snapshot[role]}")
            return False
        print(f"Restored. The plant's EMS mode was '{snapshot['work_mode']}' before the test: Remote EMS off lands in"
              " Maximum Self Consumption, so set that mode again in the mySigen app (AI mode on the owner's plant).")
        return True
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# --- judging ---------------------------------------------------------------------------------------------------------


def _held_from(samples: list[tuple[float, tuple[float | None, ...]]], cap: float) -> float | None:
    """The first time from which every later reading has every phase at or under `cap`."""
    for index, (t, _phases) in enumerate(samples):
        if all(None not in phases and max(phases) <= cap for _t, phases in samples[index:]):  # type: ignore[type-var]
            return t
    return None


def _verdict(window: Window, end: float, held_from: float | None) -> str:
    if end - window.on < MIN_LOAD_S:
        return f"not judged (the load was on for less than {MIN_LOAD_S:.0f} s)"
    return "PASS" if held_from is not None and held_from - window.on <= SETTLE_S else "FAIL"


NOT_PUSHED = "not judged (the load did not take its phase over a third of the limit: use a larger one)"


def judge_stopped(run: Run, window: Window) -> dict[str, Any]:
    """The writes-stopped step. A full verdict only where the plant's integration was really not read during the
    window (its polls stopped, not only SpotNav's writes) and an independent meter (`--meter`) shows each phase; the
    load must take its phase over a third of the limit by the earlier steps' measure of it (the same load)."""
    end = window.off if window.off is not None else run.samples[-1].t
    during = [s for s in run.samples if window.on <= s.t <= end]
    out: dict[str, Any] = {"step": window.step, "duration_s": round(end - window.on)}
    polled = any(v is not None for s in during for v in (*s.grid, *s.plant))
    out["polls_stopped"] = bool(during) and not polled
    if run.meter is None:
        out["verdict"] = "not judged (no independent meter: --meter)"
        return out
    if polled:
        out["verdict"] = "not judged (the sigen integration kept being read: this is the binding step again)"
        return out
    before = [s for s in run.samples if window.on - 30.0 <= s.t < window.on and None not in s.meter]
    steady = [s for s in during if s.t >= window.on + SETTLE_S and None not in s.meter]
    earlier = [r for r in run.results if "phase" in r and "load_kw" in r]
    if not before or not steady or not earlier:
        out["verdict"] = "not judged (the meter could not be read, or no earlier step measured the load)"
        return out
    cap = run.limit_kw / 3.0 + PHASE_TOLERANCE_KW
    phase = "ABC".index(earlier[-1]["phase"])
    load_kw = max(r["load_kw"] for r in earlier)
    base = sum(s.meter[phase] for s in before) / len(before)  # type: ignore[misc]
    held_from = _held_from([(s.t, s.meter) for s in during], cap)
    out.update({
        "phase": "ABC"[phase], "load_kw": load_kw, "cap_kw": round(cap, 2), "judged_on": "meter",
        "worst_phase_kw": round(max(max(s.meter) for s in steady), 2),  # type: ignore[type-var]
        "unheld_phase_kw": round(base + load_kw, 2),
        "settled_s": None if held_from is None else round(held_from - window.on),
    })
    out["verdict"] = NOT_PUSHED if base + load_kw <= cap else _verdict(window, end, held_from)
    return out


def judge(run: Run, window: Window) -> dict[str, Any]:
    """The phase the load landed on (the largest jump in a phase's house load) and whether every phase was held at
    limit / 3 + tolerance from at most `SETTLE_S` after the load went on until it went off. Judged only where the
    load took its phase over a third of the limit (its house load plus an even share of the charge): a phase that
    never needed holding shows nothing."""
    if window.stopped:
        return judge_stopped(run, window)
    before = [s for s in run.samples if window.on - 20.0 <= s.t < window.on and None not in s.house()]
    end = window.off if window.off is not None else run.samples[-1].t
    during = [s for s in run.samples if window.on <= s.t <= end]
    steady = [s for s in during if s.t >= window.on + SETTLE_S and None not in s.house()]
    out: dict[str, Any] = {"step": window.step, "duration_s": round(end - window.on)}
    if not before or not steady:
        out["verdict"] = "not judged (the plant could not be read: use the app or a meter)"
        return out
    base = [sum(s.house()[i] for s in before) / len(before) for i in range(3)]  # type: ignore[misc]
    load = [sum(s.house()[i] for s in steady) / len(steady) for i in range(3)]  # type: ignore[misc]
    jumps = [load[i] - base[i] for i in range(3)]
    phase = max(range(3), key=lambda i: jumps[i])
    cap = run.limit_kw / 3.0 + PHASE_TOLERANCE_KW
    held_from = _held_from([(s.t, s.grid) for s in during], cap)
    worst = max(max(v for v in s.grid if v is not None) for s in steady)
    totals = [s.grid_total() for s in steady if s.grid_total() is not None]
    out.update({
        "phase": "ABC"[phase], "load_kw": round(jumps[phase], 2),
        "worst_phase_kw": round(worst, 2), "cap_kw": round(cap, 2),
        "total_max_kw": round(max(totals), 2) if totals else None,
        "plant_on_phase_kw": round(min(s.plant[phase] for s in steady if s.plant[phase] is not None), 2),
        "plant_on_phase_max_out_kw": round(max(s.plant[phase] for s in steady if s.plant[phase] is not None), 2),
        "settled_s": None if held_from is None else round(held_from - window.on),
    })
    out["verdict"] = _verdict(window, end, held_from)
    unheld_phase = load[phase] + window.charge_kw / 3.0
    unheld_total = sum(load) + window.charge_kw
    out.update({"unheld_phase_kw": round(unheld_phase, 2), "unheld_total_kw": round(unheld_total, 2)})
    if out["verdict"] in ("PASS", "FAIL") and unheld_phase <= cap:
        out["verdict"] = NOT_PUSHED
    if window.step.startswith("under") and out["verdict"] in ("PASS", "FAIL"):
        # Decisive only while the total stays under the limit: a plant that limits just the total would then leave
        # the loaded phase over a third of it.
        if unheld_total >= run.limit_kw - 0.3:
            out["verdict"] = "not judged (the total reached the limit: lower --low-charge-kw or use a smaller load)"
        elif out["verdict"] == "FAIL":
            out["verdict"] = "FAIL: the plant limits only the total, not each phase"
    return out


def summary(run: Run, results: list[dict[str, Any]], restored: bool) -> None:
    print("\n=== Summary ===")
    for result in results:
        print(json.dumps(result))
    verdict = {r["step"]: r["verdict"] for r in results}
    print(f"Writes made: {len(run.ha.writes)}; restored: {'yes' if restored else 'NO (see above)'}")
    for note in run.notes:
        print(f"Note: {note}")
    full = verdict.get("binding") == "PASS"
    per_phase = all(v == "PASS" for k, v in verdict.items() if k.startswith("under")) and "under" in verdict
    stopped = verdict.get("stopped")
    person = next((r.get("verdict_from_person") for r in results if r["step"] == "stopped"), None)
    print(f"Load at full charge held at limit/3 within {SETTLE_S:.0f} s: {verdict.get('binding', 'not run')}")
    print(f"Load under the limit held per phase (decisive): {verdict.get('under', 'not run')}")
    if "under2" in verdict:
        print(f"The same on a second phase: {verdict['under2']}")
    print(f"Writes and polls stopped, on an independent meter: {stopped or 'not run'}")
    if stopped is not None and stopped != "PASS" and person in ("y", "n"):
        print(f"FALLBACK only: the person read the app or a meter and answered '{person}'. That is not enough to"
              " record the fact.")
    if full and per_phase and stopped == "PASS":
        print("RESULT: PASS. The plant holds each phase at limit/3 by itself. SpotNav's recipe fact may be recorded.")
    elif full and per_phase:
        print("RESULT: PASS with HA running only. The writes-and-polls-stopped step needs an independent meter"
              " (--meter) and the sigen integration disabled before the fact may be recorded.")
    else:
        print("RESULT: NOT PASSED. SpotNav keeps its HA-down cap (the fact stays unrecorded).")


# --- the run ---------------------------------------------------------------------------------------------------------


def load_step(run: Run, step: str, *, stopped: bool = False) -> dict[str, Any]:
    """One load on and off with markers, judged and kept in `run.results` before the samples after it."""
    run.step = step
    run.wait("Switch the single-phase load ON (kettle or oven, 2-3 kW), then press Enter.")
    window = Window(step, run.marker("load on"), stopped=stopped, charge_kw=run.charge_kw)
    run.windows.append(window)
    run.wait(f"Leave it on at least {MIN_LOAD_S:.0f} s; press Enter as you switch it OFF.", least_s=MIN_LOAD_S)
    window.off = run.marker("load off")
    result = judge(run, window)
    run.results.append(result)
    print(f"    {step}: {result['verdict']}")
    run.settle(20.0)
    return result


def steps(run: Run, args: argparse.Namespace) -> None:
    """The test's steps (see the module); each result is kept in `run.results` as soon as it is judged."""
    run.wait(f"Ready: the plant goes to Remote EMS '{MODE_GRID_FIRST}' with the import limit at {args.limit_kw} kW"
             f" ({args.limit_kw / 3:.1f} kW per phase). Press Enter to start.")
    run.step = "setup"
    run.guarding = True
    # The lower import limit first; the charge limit at 0 before Remote EMS goes on, so a command mode 40031 still
    # holds acts on nothing until the mode is written (as SpotNav's own control primes it).
    run.set("import_limit", args.limit_kw)
    run.set("charge_limit", 0.0)
    run.set("remote_ems", "on")
    if not run.wait_available("control_mode", 30.0):
        raise RuntimeError("the control mode did not become available after Remote EMS went on")
    run.set("control_mode", MODE_GRID_FIRST)
    run.set("charge_limit", args.charge_kw)
    run.step = "baseline"
    run.settle(60.0)
    load_step(run, "binding")
    run.step = "under"
    run.set("charge_limit", args.low_charge_kw)
    run.settle(30.0)
    load_step(run, "under")
    if args.skip_stopped:
        return
    run.step = "stopped"
    run.set("charge_limit", args.charge_kw)
    run.settle(30.0)
    # From here nothing is written until the restore (the over-current guard may still end the step by restoring).
    run.wait("Writes stop now. Disable SpotNav's entries, then the sigen integration (Settings > Devices & services"
             " > the entry's menu > Disable), so that nothing writes or polls the plant. Press Enter once both are"
             " disabled.")
    print(f"    Waiting {STOPPED_WAIT_S:.0f} s with nothing written or polled (q and Enter stops and restores).")
    run.settle(STOPPED_WAIT_S)
    result = load_step(run, "stopped", stopped=True)
    run.wait("Re-enable the sigen integration (and SpotNav), then press Enter.")
    if not run.wait_available("battery", 120.0):
        run.notes.append("the sigen entities did not come back within two minutes")
    after = run.read()
    run.notes.append(
        f"after writes stopped: Remote EMS {text(after.get(run.entities['remote_ems']))}, mode"
        f" '{text(after.get(run.entities['control_mode']))}', import limit"
        f" {text(after.get(run.entities['import_limit']))} kW"
    )
    if result["verdict"] != "PASS":
        answer = run.wait(
            f"Did the loaded phase stay at or under {args.limit_kw / 3 + PHASE_TOLERANCE_KW:.1f} kW within"
            f" {SETTLE_S:.0f} s in the mySigen app or on a meter? Type y or n and press Enter (kept as a fallback"
            " only)."
        )
        result["verdict_from_person"] = answer


def parse_meter(value: str) -> tuple[str, str, str] | None:
    """`a=sensor.x,b=sensor.y,c=sensor.z` as the entities for A, B and C; `None` when it is not that."""
    parts = dict(item.split("=", 1) for item in value.split(",") if "=" in item)
    if set(parts) != {"a", "b", "c"} or not all(parts.values()):
        return None
    return (parts["a"].strip(), parts["b"].strip(), parts["c"].strip())


def _terminate(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Home Assistant's base URL, e.g. http://192.168.1.10:8123")
    parser.add_argument("--token-file", required=True, type=Path, help="file holding a long-lived access token")
    parser.add_argument("--plant", default="sigen_plant", help="the plant device's entity prefix (sigen_plant)")
    parser.add_argument("--entity", action="append", default=[], metavar="ROLE=ENTITY_ID",
                        help="override one entity id (roles: see default_entities in the script)")
    parser.add_argument("--extra", action="append", default=[], metavar="ENTITY_ID",
                        help="another entity to log each sample (a separate meter, say)")
    parser.add_argument("--limit-kw", type=float, default=6.0, help="the import limit during the test (6 kW)")
    parser.add_argument("--charge-kw", type=float, default=4.0, help="the charge limit (4 kW)")
    parser.add_argument("--low-charge-kw", type=float, default=1.0, help="the charge in the under-limit step (1)")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between samples (5)")
    parser.add_argument("--abort-phase-kw", type=float, default=4.0,
                        help="restore at once when a phase imports more than this twice in a row (4 kW, 17 A)")
    parser.add_argument("--csv", type=Path, help="where to log (default: sigen_import_limit_<time>.csv here)")
    parser.add_argument("--dry-run", action="store_true", help="read, check and log only; write nothing")
    parser.add_argument("--skip-stopped", action="store_true", help="leave out the writes-stopped step")
    parser.add_argument("--meter", metavar="a=ID,b=ID,c=ID",
                        help="an independent per-phase meter at the grid connection (W, kW or A), not from the plant's"
                             " integration: what the writes-stopped step is judged on")
    parser.add_argument("--restore-from", type=Path, metavar="FILE",
                        help="only put back the values kept in a snapshot file (<csv>.snapshot.json) and exit")
    parser.add_argument("--ignore-spotnav", action="store_true",
                        help="run although SpotNav's battery control reads as writing (only if you know it is not)")
    args = parser.parse_args(argv)

    if not (0.5 <= args.limit_kw <= 9.0) or not (0.5 <= args.charge_kw <= 8.0) or args.low_charge_kw < 0:
        parser.error("keep the test far from the fuse: --limit-kw 0.5-9, --charge-kw 0.5-8")
    if args.charge_kw >= args.limit_kw or args.low_charge_kw >= args.limit_kw:
        parser.error("--charge-kw and --low-charge-kw must be under --limit-kw")
    if not (0.0 < args.abort_phase_kw <= ABORT_PHASE_MAX_KW):
        parser.error(f"--abort-phase-kw must be above 0 and at most {ABORT_PHASE_MAX_KW} kW (20 A)")
    token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
    entities = default_entities(args.plant)
    for item in args.entity:
        role, _, entity = item.partition("=")
        if role not in entities or not entity:
            parser.error(f"--entity {item}: unknown role or empty id")
        entities[role] = entity
    meter = None if args.meter is None else parse_meter(args.meter)
    if args.meter is not None and meter is None:
        parser.error("--meter takes a=ENTITY,b=ENTITY,c=ENTITY")
    if meter is not None and set(meter) & set(entities.values()):
        parser.error("--meter must not be one of the plant's own entities")
    ha = HomeAssistant(args.url, token, dry_run=args.dry_run)
    if args.restore_from is not None:
        kept = json.loads(args.restore_from.read_text(encoding="utf-8"))
        out = io.StringIO()
        run = Run(ha, kept["entities"], args.limit_kw, args.interval, args.abort_phase_kw, [], csv.writer(out), out)
        return 0 if restore(run, kept["snapshot"], force=True) else 1
    path = args.csv or Path(f"sigen_import_limit_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "time", "step", "marker", "grid_a_kw", "grid_b_kw", "grid_c_kw", "grid_total_kw",
            "plant_a_kw", "plant_b_kw", "plant_c_kw", "house_a_kw", "house_b_kw", "house_c_kw",
            "battery_kw", "soc_pct", "import_limit_kw", "charge_limit_kw", "remote_ems", "control_mode",
            "work_mode", "grid_sensor", "phase_control", *args.extra, "meter_a_kw", "meter_b_kw", "meter_c_kw",
        ])
        run = Run(ha, entities, args.limit_kw, args.interval, args.abort_phase_kw, args.extra, writer, handle)
        run.meter = meter
        threading.Thread(target=_stdin, args=(run.lines,), daemon=True).start()
        for sig in STOP_SIGNALS:
            if sig != signal.SIGINT:
                signal.signal(sig, _terminate)
        print(f"Logging to {path}. Type q and Enter at any prompt to stop and restore.")
        found = check(run, force_spotnav=args.ignore_spotnav)
        snapshot = found["snapshot"]
        print("Snapshot:", json.dumps(snapshot))
        for note in run.notes:
            print("Note:", note)
        if found["problems"]:
            print("\nCannot run the test:")
            for problem in found["problems"]:
                print("  -", problem)
            if not args.dry_run:
                return 2
        restored = True
        if not args.dry_run:
            save_snapshot(snapshot_path(path), snapshot, entities)
            print(f"Snapshot kept in {snapshot_path(path)} (put it back with --restore-from that file).")
        try:
            if args.dry_run:
                print(f"\nDry run: would write the import limit {args.limit_kw} kW, the charge limit 0 and then"
                      f" {args.charge_kw} kW, Remote EMS on and the mode '{MODE_GRID_FIRST}'. Nothing is written.")
                load_step(run, "dry_run")
            else:
                steps(run, args)
        except Stop as stop:
            print(f"\n=== Stopped: {stop} ===")
        except KeyboardInterrupt:
            print("\n=== Interrupted ===")
        except Exception as err:  # noqa: BLE001 - any error: restore first
            print(f"\n=== Error: {err} ===")
        finally:
            restored = restore(run, snapshot)
            run.step = "restored"
            try:
                run.sample("after restore")
            except Exception:  # noqa: BLE001 - only a last reading
                pass
        summary(run, run.results, restored)
        print(f"CSV: {path.resolve()}")
    return 0 if restored else 1


def _stdin(lines: queue.Queue[str]) -> None:
    for line in sys.stdin:
        lines.put(line)


if __name__ == "__main__":
    sys.exit(main())
