"""A made-up house for the screenshot tool: sun, house load, heat pump, home battery, grid and weather.

One deterministic simulation in five-minute steps from `DAYS` days back to now, kept in this process. The live
sensors read the step of the moment, the statistics the tool imports are the same steps, so the live counters and
the history agree. Nothing here is a real person's or a real installation's figures.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Stockholm")
STEP_S = 300
DAYS = 56
CAPACITY_KWH = 15.0
BATTERY_KW = 5.0
COUNTER_BASE = {"grid_in": 4210.0, "grid_out": 310.0, "solar": 1385.0, "batt_in": 620.0, "batt_out": 575.0, "heat": 2210.0}
#: Cloudiness of the days, newest last; today and yesterday are bright.
WEATHER = [0.9, 0.5, 0.2, 0.7, 1.0, 0.35, 0.15, 0.6, 0.8, 0.3, 0.55, 0.95, 0.45, 0.25]

_model = None


def _noise(n: int) -> float:
    """A repeatable value in -1..1."""
    return math.sin(n * 12.9898) * 0.5 + math.sin(n * 4.1414 + 1.3) * 0.5


def _gauss(t: float, mid: float, width: float) -> float:
    return math.exp(-(((t - mid) / width) ** 2))


class Model:
    def __init__(self, now: datetime | None = None) -> None:
        now = (now or datetime.now(timezone.utc)).astimezone(ZONE)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.start = (today - timedelta(days=DAYS)).astimezone(timezone.utc)
        self.today = today
        n = DAYS * 288 + 288  # to the end of today
        self.n = n
        self.t = []
        self.sun, self.house, self.heat, self.batt, self.grid, self.soc = [], [], [], [], [], []
        self.t_out, self.rooms = [], []
        soc = 45.0
        hot_runs: dict[int, list[tuple[float, float]]] = {}
        for i in range(n):
            at = self.start + timedelta(seconds=i * STEP_S)
            local = at.astimezone(ZONE)
            day = (local.date() - self.start.astimezone(ZONE).date()).days
            hour = local.hour + local.minute / 60
            w = WEATHER[(day + 3) % len(WEATHER)]
            if local.date() == today.date():
                w = 0.9
            if local.date() == today.date() - timedelta(days=1):
                w = 0.8
            x = (hour - 7.6) / 8.1
            sun = 4.6 * w * math.sin(math.pi * x) ** 1.5 if 0 < x < 1 else 0.0
            sun = max(0.0, sun * (1 + 0.06 * _noise(i)))
            t_out = 3.0 + 3.0 * math.sin(day * 0.7) - 2.5 * math.cos(2 * math.pi * (hour - 15) / 24) + 0.2 * _noise(i + 7)
            house = (0.34 + 0.25 * _gauss(hour, 7.2, 1.0) + 0.9 * _gauss(hour, 18.5, 1.6) + 0.3 * _gauss(hour, 12.5, 1.5)
                     + 0.06 * _noise(i + 3))
            base = 0.35 + 0.095 * (18 - t_out) + 0.07 * _noise(i + 11)
            if day not in hot_runs:
                morning = 4.4 + 0.25 * _noise(day * 3 + 1)
                evening = 17.1 + 0.3 * _noise(day * 3 + 2)
                runs = [(morning, 0.45), (evening, 0.5)]
                if day % 4 == 1:
                    runs.append((12.3 + 0.2 * _noise(day), 0.4))
                hot_runs[day] = runs
            heat = base
            for start_h, length_h in hot_runs[day]:
                if start_h <= hour < start_h + length_h:
                    heat = max(3.3, 1.9 * base) + 0.1 * _noise(i + 5)
            # The battery: a grid charge in the cheap small hours, then it covers what the sun does not.
            surplus = sun - (house + heat)
            if surplus > 0:
                b = min(surplus, BATTERY_KW, (100 - soc) / 100 * CAPACITY_KWH * 12)
            else:
                b = -min(-surplus, BATTERY_KW, max(0.0, (soc - 10) / 100 * CAPACITY_KWH * 12))
            if 2 <= hour < 5.5 and soc < 90:
                b = max(b, 3.0)
            soc += b * STEP_S / 3600 / CAPACITY_KWH * 100
            grid = house + heat + b - sun
            self.t.append(at)
            self.sun.append(sun); self.house.append(house); self.heat.append(heat)
            self.batt.append(b); self.grid.append(grid); self.soc.append(soc); self.t_out.append(t_out)
            living = 21.0 + 0.35 * math.sin(2 * math.pi * (hour - 9) / 24) + 0.12 * _noise(i + 21) - 0.03 * max(0, 3 - t_out)
            self.rooms.append((living, living - 1.1 + 0.1 * _noise(i + 4), living - 0.4 + 0.1 * _noise(i + 8)))
        hour_k = STEP_S / 3600
        # The hot-water runs of every day (start, end, kWh), as the meter shows them.
        self.runs = []
        first = self.start.astimezone(ZONE).date()
        for day, runs in sorted(hot_runs.items()):
            midnight = datetime.combine(first + timedelta(days=day), datetime.min.time(), tzinfo=ZONE)
            for start_h, length_h in runs:
                begin = (midnight + timedelta(hours=start_h)).astimezone(timezone.utc)
                end = begin + timedelta(hours=length_h)
                lo, hi = self.index(begin), self.index(end)
                kwh = sum(self.heat[lo:hi]) * hour_k
                self.runs.append((begin, begin + timedelta(minutes=5 * (hi - lo)), kwh))
        self.counters = {k: [] for k in COUNTER_BASE}
        acc = dict.fromkeys(COUNTER_BASE, 0.0)
        for i in range(n):
            g, b = self.grid[i], self.batt[i]
            acc["grid_in"] += max(g, 0) * hour_k
            acc["grid_out"] += max(-g, 0) * hour_k
            acc["solar"] += self.sun[i] * hour_k
            acc["batt_in"] += max(b, 0) * hour_k
            acc["batt_out"] += max(-b, 0) * hour_k
            acc["heat"] += self.heat[i] * hour_k
            for k, v in acc.items():
                self.counters[k].append(v)

    def index(self, at: datetime) -> int:
        return max(0, min(self.n - 1, int((at - self.start).total_seconds() // STEP_S)))

    def live(self, at: datetime | None = None) -> dict[str, float]:
        i = self.index(at or datetime.now(timezone.utc))
        return {
            "sun": self.sun[i], "house": self.house[i], "heat": self.heat[i], "batt": self.batt[i], "grid": self.grid[i],
            "soc": self.soc[i], "t_out": self.t_out[i], "living": self.rooms[i][0], "bedroom": self.rooms[i][1],
            "office": self.rooms[i][2],
            **{"e_" + k: COUNTER_BASE[k] + self.counters[k][i] for k in COUNTER_BASE},
        }


def get() -> Model:
    global _model
    if _model is None:
        _model = Model()
    return _model


#: What the screenshot driver pins for a picture (`demo_house.docs_scene`): kW for sun, house, heat and battery
#: (charge positive); empty is the simulation.
SCENE: dict[str, float] = {}


#: What the battery's control entities (the `sigen` demo's number, select and switch) were set to: a plant that
#: obeys, so a check of its control sees it do what it is told.
CONTROL: dict[str, object] = {
    "remote_ems": False, "mode": "Maximum Self Consumption", "charge_limit": 6.0, "discharge_limit": 6.0,
}


def live() -> dict[str, float]:
    """The simulation's values now, with the pinned scene on top and the battery's commands applied; the grid follows
    from the rest."""
    values = get().live()
    if SCENE:
        values.update(SCENE)
    if CONTROL["remote_ems"]:
        mode = str(CONTROL["mode"])
        charge, discharge = float(CONTROL["charge_limit"]), float(CONTROL["discharge_limit"])
        if "Command Charging" in mode:
            values["batt"] = charge
        elif "Command Discharging" in mode:
            values["batt"] = -discharge
        values["batt"] = max(-discharge, min(charge, values["batt"]))
    if SCENE or CONTROL["remote_ems"]:
        values["grid"] = values["house"] + values["heat"] + values["batt"] - values["sun"]
    return values
