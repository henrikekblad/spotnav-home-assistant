"""Replay the house battery plan (`battery/plan.py`) on a site's history and compare it with what happened.

Not part of the integration: a research tool, run by hand with the repository's virtual environment
(`.venv/bin/python scripts/battery_plan_backtest.py --cache DIR`). It reads, never writes, anything outside DIR.

Data
* The car's energy is the HALO charger's energy register steps (not forward-filled power, which overcounts over write
  gaps); the power sensor only where the register has no data.
* Minute means from InfluxDB 3 (database `ha`, written by `influx3_writer`), forward-filled within up-time (no grid
  sample for 10 min is down-time, as in the load-forecast backtest), then quarter-hour means.
* The day-ahead spot price from ENTSO-E (EUR/MWh) at the ECB's EUR rate of the day before delivery.
* Tokens are read from files (`~/tokens/influx-ha-token`, `~/tokens/entsoe-security-token`) and never printed: an
  HTTP error is reported by its status alone.

Replay
* Each day at 13:00 local (when the next day's prices are out) a plan is made to the end of the next day and run for
  24 h against what really happened; the battery's energy carries from one day to the next. `--every 4` plans again
  every hour instead, each time to the end of the last published day, as a plan running on a site would.
* `perfect`: the plan knows the house's load and the sun exactly.
* `forecast`: the house load is the p75 weekday/weekend x hour profile of the 6 weeks before (half-life 2 weeks, car
  hours left out), the sun is the same quarter hour of the day before. The car is a fixed load SpotNav plans, so both
  replays know it.
* The planned action is run against the real load: self-consumption, hold and discharge-to-house follow the real
  load; a grid charge keeps its planned power. The fuse and export caps are enforced in every slot.
* `self-consumption`: the same battery left to itself. `actual`: the grid meter as it was, under the site's own
  battery control (Sigenergy AI mode on the owner's site).
* Costs are compared with the energy each ends with valued at the period's median buy price x the round trip.
* `--house balance` takes the house as grid + sun - battery - car (Sigenergy's own daily load, inverter losses of
  about 0.2 kW included) in place of the total-load sensor; `--revenue CSV` then prints each run's daily "revenue"
  as Sigenergy's Revenue Calendar counts it (see `revenue_by_day`) beside the calendar's CSV.
* The buy price is `(spot x (1 + fee%) + fee) x VAT`, the sell price `spot x (1 + sell%) + credit`. The owner's
  Sigenergy tariff (buy 1.3185 x spot + 74.5, sell 1.05 x spot + 6.6) is `--fee 59.6 --fee-percent 5.48
  --sell-percent 5 --sell-credit 6.6`.

Modes and wear (3.0 wave 3)
* `--mode` plans as one battery mode (`plan_model.MODE_DEFS`): `balanced` weighs twice the wear and sells from the
  battery only through the spread gate; `max_profit` may sell freely at the true wear; `self_consumption` does not
  plan. Every mode keeps the battery's own floor. `--wear` is the wear per kWh each
  way (öre); `--gate-factor` overrides Balanced's gate margin in multiples of the wear (`off`: no sale at all).
* `--matrix` runs every mode x wear {0, 5, 15, 25, 40} x {perfect, p75}, and Balanced's gate at 15 öre, in parallel,
  and prints one table: cost, saving against self-consumption, the same with every run's throughput charged at
  `--wear` (the wear really paid, if that figure is right), full cycles a day, sold from the battery, the lowest SoC
  reached.
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import gzip
import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.spotnav.battery.plan import (  # noqa: E402
    action_power,
    plan_mode,
    run_slot,
)
from custom_components.spotnav.battery.plan_model import (  # noqa: E402
    ACTION_SELF_CONSUME,
    EXPORT_GATE,
    EXPORT_OFF,
    MODE_BALANCED,
    MODE_DEFS,
    MODE_MAX_PROFIT,
    MODE_SELF_CONSUMPTION,
    BatteryMode,
    BatteryParams,
    PlanRequest,
    PlanSlot,
)

TZ = ZoneInfo("Europe/Stockholm")
INFLUX_URL = "http://influxdb3.internal:8181/api/v3/query_sql"
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
SE4 = "10Y1001A1001A47J"
SLOT = timedelta(minutes=15)

#: (table, entity) per series of the owner's site. Power in kW: grid + import, battery + charge.
SERIES = {
    "grid_kw": ("kW", "sensor.sigen_plant_grid_active_power"),
    "pv_kw": ("kW", "sensor.sigen_plant_pv_power"),
    "batt_kw": ("kW", "sensor.sigen_plant_battery_power"),
    "load_kw": ("kW", "sensor.sigen_plant_total_load_power"),
    "soc": ("%", "sensor.sigen_plant_battery_state_of_charge"),
    "car_w": ("W", "sensor.billaddning_effekt"),
    "car_reg": ("kWh", "sensor.halo_charger_connector_1_energy_active_import_register"),
}
#: A register step over a gap up to this long is spread over the gap; a longer gap with energy in it is missing.
REGISTER_GAP = timedelta(minutes=10)
#: The car power sensor is held over a write gap for at most this many minutes.
CAR_POWER_FILL = 3
#: Sigenergy's total load reads 0.23 kW below grid + pv - battery (load-forecast backtest); used before it existed.
DERIVED_BIAS_KW = 0.23


def _token(name: str) -> str:
    return Path(os.path.expanduser(f"~/tokens/{name}")).read_text().strip()


def _get(request: urllib.request.Request, what: str) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{what}: HTTP {err.code}") from None
    except urllib.error.URLError as err:
        raise SystemExit(f"{what}: {type(err.reason).__name__}") from None


def _cached(path: Path, fetch: Callable[[], object]) -> object:
    if path.exists():
        with gzip.open(path, "rt") as handle:
            return json.load(handle)
    data = fetch()
    with gzip.open(path, "wt") as handle:
        json.dump(data, handle)
    return data


# ----------------------------------------------------------------------------- data


def _influx_minutes(table: str, entity: str, start: datetime, end: datetime, raw: bool = False) -> list[list]:
    token = _token("influx-ha-token")
    rows: list[list] = []
    month = start
    while month < end:
        upto = min(end, month + timedelta(days=31))
        sql = (
            f"SELECT time AS t, value AS v FROM \"{table}\" "
            f"WHERE entity_id = '{entity}' AND time >= '{month:%Y-%m-%dT%H:%M:%S}' "
            f"AND time < '{upto:%Y-%m-%dT%H:%M:%S}' ORDER BY t"
            if raw
            else f"SELECT date_bin(interval '1 minute', time) AS t, AVG(value) AS v FROM \"{table}\" "
            f"WHERE entity_id = '{entity}' AND time >= '{month:%Y-%m-%dT%H:%M:%S}' "
            f"AND time < '{upto:%Y-%m-%dT%H:%M:%S}' GROUP BY t ORDER BY t"
        )
        body = json.dumps({"db": "ha", "format": "json", "q": sql}).encode()
        request = urllib.request.Request(
            INFLUX_URL, data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        rows += [[r["t"], r["v"]] for r in json.loads(_get(request, f"influx {entity}") or b"[]")]
        month = upto
    return rows


def _entsoe_prices(start: datetime, end: datetime) -> dict[str, float]:
    """EUR/MWh per quarter hour (UTC ISO), A03 curves carried forward, PT60M spread over its quarters."""
    params = {
        "securityToken": _token("entsoe-security-token"),
        "documentType": "A44",
        "in_Domain": SE4,
        "out_Domain": SE4,
        "periodStart": f"{start:%Y%m%d%H%M}",
        "periodEnd": f"{end:%Y%m%d%H%M}",
    }
    body = _get(urllib.request.Request(f"{ENTSOE_URL}?{urllib.parse.urlencode(params)}"), "entsoe")
    root = ElementTree.fromstring(body)
    ns = {"n": root.tag.split("}")[0].strip("{")}
    out: dict[str, float] = {}
    for period in root.iterfind(".//n:Period", ns):
        first = datetime.fromisoformat(period.find("n:timeInterval/n:start", ns).text.replace("Z", "+00:00"))
        last = datetime.fromisoformat(period.find("n:timeInterval/n:end", ns).text.replace("Z", "+00:00"))
        step = timedelta(minutes=15 if period.find("n:resolution", ns).text == "PT15M" else 60)
        points = {
            int(p.find("n:position", ns).text): float(p.find("n:price.amount", ns).text)
            for p in period.iterfind("n:Point", ns)
        }
        price = None
        position, at = 1, first
        while at < last:
            price = points.get(position, price)
            for quarter in range(int(step / SLOT)):
                out[(at + SLOT * quarter).isoformat()] = price
            position, at = position + 1, at + step
    return out


def _ecb_sek() -> dict[str, float]:
    root = ElementTree.fromstring(_get(urllib.request.Request(ECB_URL), "ecb"))
    out: dict[str, float] = {}
    for cube in root.iter():
        if "time" in cube.attrib:
            for rate in cube:
                if rate.attrib.get("currency") == "SEK":
                    out[cube.attrib["time"]] = float(rate.attrib["rate"])
    return out


def _quarter_means(
    rows: list[list], up: list[bool], t0: datetime, minutes: int, max_fill: int | None = None
) -> list[float | None]:
    """Forward-filled minute values, blanked in down-time, as quarter-hour means. `max_fill`: hold a value at most
    this many minutes past its last sample."""
    filled: list[float | None] = [None] * minutes
    index = {(datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - t0) // timedelta(minutes=1): v for t, v in rows}
    value = None
    last = None
    for minute in range(minutes):
        if minute in index and index[minute] is not None:
            value = index[minute]
            last = minute
        held = max_fill is None or (last is not None and minute - last <= max_fill)
        filled[minute] = value if up[minute] and held else None
    out: list[float | None] = []
    for q in range(minutes // 15):
        block = filled[q * 15:(q + 1) * 15]
        chunk = [v for v in block if v is not None]
        if max_fill is not None:  # a held-only value: minutes without a sample count as zero, never as the last value
            out.append(sum(chunk) / 15 if sum(up[q * 15:(q + 1) * 15]) >= 10 else None)
        else:
            out.append(sum(chunk) / len(chunk) if len(chunk) >= 10 else None)
    return out


def _register_quarters(points: list[list], t0: datetime, quarters: int) -> tuple[list[float | None], int]:
    """The car's energy per quarter hour (kWh) from an energy register's samples, None where the register says
    nothing: before its first sample, after its last, over a reset, and over a gap longer than REGISTER_GAP with
    energy in it (no energy is invented there). Returns the quarters and the number of missing steps."""
    energy: list[float | None] = [None] * quarters
    missing = 0
    stamps = [(datetime.fromisoformat(t).replace(tzinfo=timezone.utc), v) for t, v in points if v is not None]

    def spread(a: datetime, b: datetime, kwh: float) -> None:
        span = (b - a).total_seconds()
        first, last = max(0, (a - t0) // SLOT), min(quarters - 1, (b - t0) // SLOT)
        for q in range(first, last + 1):
            lo, hi = max(a, t0 + SLOT * q), min(b, t0 + SLOT * (q + 1))
            if hi > lo:
                energy[q] = (energy[q] or 0.0) + (kwh * (hi - lo).total_seconds() / span if span > 0 else 0.0)

    def idle(a: datetime, b: datetime) -> None:
        for q in range(max(0, (a - t0) // SLOT), min(quarters - 1, (b - t0) // SLOT) + 1):
            energy[q] = energy[q] or 0.0

    for (a, va), (b, vb) in zip(stamps, stamps[1:]):
        delta = vb - va
        if delta < -0.0005:
            missing += 1
        elif b - a <= REGISTER_GAP:
            spread(a, b, delta)
        elif delta <= 0.005:
            idle(a, b)
        else:
            missing += 1
    return energy, missing


@dataclass
class History:
    t0: datetime
    grid: list[float | None]
    pv: list[float | None]
    batt: list[float | None]
    load: list[float | None]
    soc: list[float | None]
    car: list[float]
    spot: list[float | None] = field(default_factory=list)  # SEK/kWh
    balance: bool = False

    def at(self, when: datetime) -> int:
        return (when - self.t0) // SLOT

    def total(self, i: int) -> float | None:
        """The site's load, car included: Sigenergy's total load where it exists (not with `balance`), else grid + pv
        - battery."""
        total = None if self.balance else self.load[i]
        bias = 0.0 if self.balance else DERIVED_BIAS_KW
        if total is None and None not in (self.grid[i], self.pv[i], self.batt[i]):
            total = self.grid[i] + self.pv[i] - self.batt[i] - bias
        return total

    def car_kw(self, i: int) -> float:
        """The car as part of the load. With `balance` it is cut to the site's load (a guard; the register-based car no
        longer exceeds it)."""
        total = self.total(i)
        return min(self.car[i], max(0.0, total)) if self.balance and total is not None else self.car[i]

    def house(self, i: int) -> float | None:
        """The house without the car."""
        total = self.total(i)
        return None if total is None else max(0.0, total - self.car_kw(i))


def load_history(start: datetime, end: datetime, cache: Path) -> History:
    minutes = int((end - start) / timedelta(minutes=1))
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"
    raw = {
        name: _cached(
            cache / f"{name}_{tag}.json.gz",
            lambda t=table, e=entity, r=(name == "car_reg"): _influx_minutes(t, e, start, end, r),
        )
        for name, (table, entity) in SERIES.items()
    }
    seen = [False] * minutes
    for t, _v in raw["grid_kw"]:
        m = (datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - start) // timedelta(minutes=1)
        if 0 <= m < minutes:
            seen[m] = True
    up = [any(seen[max(0, m - 9):m + 1]) for m in range(minutes)]
    q = {
        name: _quarter_means(rows, up, start, minutes, CAR_POWER_FILL if name == "car_w" else None)
        for name, rows in raw.items()
        if name != "car_reg"
    }
    # The car's energy: the HALO register's steps where it has them, else the power sensor (held over short gaps only).
    register, _missing = _register_quarters(raw["car_reg"], start, minutes // 15)
    car = [4 * e if e is not None else (v or 0.0) / 1000 for e, v in zip(register, q["car_w"])]
    history = History(start, q["grid_kw"], q["pv_kw"], q["batt_kw"], q["load_kw"], q["soc"], car)

    prices = _cached(cache / f"entsoe_{tag}.json.gz", lambda: _entsoe_prices(start - timedelta(days=1), end))
    rates = _cached(cache / f"ecb_{tag}.json.gz", _ecb_sek)
    days = sorted(rates)
    for i in range(len(car)):
        at = start + SLOT * i
        eur = prices.get(at.isoformat())
        before = (at.astimezone(TZ).date() - timedelta(days=1)).isoformat()
        k = bisect.bisect_right(days, before) - 1
        history.spot.append(None if eur is None or k < 0 else eur * rates[days[k]] / 1000)
    return history


# ----------------------------------------------------------------------------- forecasts


def p75_house(history: History, at: datetime, n: int) -> list[float]:
    """The weighted p75 of the house per weekday/weekend x local hour, from the 6 weeks before [at]."""
    cells: dict[tuple[bool, int], list[tuple[float, float]]] = {}
    first = max(0, history.at(at - timedelta(weeks=6)))
    for i in range(first, history.at(at)):
        value = history.house(i)
        if value is None or history.car[i] > 0.5:
            continue
        local = (history.t0 + SLOT * i).astimezone(TZ)
        age_days = (at - (history.t0 + SLOT * i)).total_seconds() / 86400
        cells.setdefault((local.weekday() >= 5, local.hour), []).append((value, 0.5 ** (age_days / 14)))

    def quantile(pairs: list[tuple[float, float]], q: float) -> float:
        pairs = sorted(pairs)
        total = sum(w for _, w in pairs)
        running = 0.0
        for value, weight in pairs:
            running += weight
            if running >= q * total:
                return value
        return pairs[-1][0]

    fallback = quantile([p for c in cells.values() for p in c], 0.75)
    out = []
    for k in range(n):
        local = (at + SLOT * k).astimezone(TZ)
        pairs = cells.get((local.weekday() >= 5, local.hour))
        out.append(quantile(pairs, 0.75) if pairs and len(pairs) >= 8 else fallback)
    return out


def persistence_sun(history: History, start: int, n: int) -> list[float]:
    """The sun of the same quarter hour one day before (two for the plan's second day)."""
    out = []
    for k in range(n):
        back = 96 * (1 + k // 96)
        value = history.pv[start + k - back] if start + k - back >= 0 else None
        out.append(max(0.0, value or 0.0))
    return out


# ----------------------------------------------------------------------------- replay


@dataclass(frozen=True)
class Site:
    battery: BatteryParams
    import_cap_kw: float
    export_cap_kw: float
    fee_minor: float  # energy tax + grid fee per kWh, before VAT
    fee_percent: float  # the grid fee's part that is a share of the spot price, before VAT
    vat_percent: float
    sell_credit_minor: float
    sell_percent: float = 0.0

    def buy(self, spot_sek: float) -> float:
        spot = spot_sek * 100
        return (spot + spot * self.fee_percent / 100 + self.fee_minor) * (1 + self.vat_percent / 100)

    def sell(self, spot_sek: float) -> float:
        return spot_sek * 100 * (1 + self.sell_percent / 100) + self.sell_credit_minor


def _slots(history: History, site: Site, start: int, n: int, house: Sequence[float], sun: Sequence[float]) -> tuple:
    return tuple(
        PlanSlot(
            start=history.t0 + SLOT * (start + k),
            end=history.t0 + SLOT * (start + k + 1),
            buy=site.buy(history.spot[start + k]),
            sell=site.sell(history.spot[start + k]),
            house_kw=max(0.0, house[k]),
            solar_kw=max(0.0, sun[k]),
            car_kw=history.car_kw(start + k),
            import_cap_kw=site.import_cap_kw,
            export_cap_kw=site.export_cap_kw,
        )
        for k in range(n)
    )


def _real(history: History, i: int) -> tuple[float, float]:
    house = history.house(i)
    return (house if house is not None else 0.0), max(0.0, history.pv[i] or 0.0)


@dataclass
class Tally:
    cost: float = 0.0
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    grid_charge_kwh: float = 0.0
    #: AC energy discharged by the battery, and the part of it sold (beyond the site's own deficit).
    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0
    battery_export_kwh: float = 0.0
    min_soc_pct: float = 100.0
    #: The cost (öre) per local day, of the quarter hours with a meter reading.
    daily: dict[str, float] = field(default_factory=dict)


def replay(
    history: History,
    site: Site,
    first: datetime,
    last: datetime,
    foresight: str,
    end_value: float,
    mode: BatteryMode = MODE_DEFS[MODE_BALANCED],
    wear: float = 15.0,
    every: int = 96,
) -> tuple:
    """Day by day from 13:00 local on [first] to 13:00 on [last] as [mode] with [wear]; [foresight] is `self`
    (no plan), `perfect` or `forecast`. Returns (tally, start kWh, end kWh)."""
    battery = site.battery
    if foresight == "self" or not mode.plans:
        foresight = "self"
    i = history.at(first)
    soc_start = history.soc[i] if history.soc[i] is not None else 50.0
    soc_kwh = battery.usable_kwh * soc_start / 100
    start_kwh = soc_kwh
    tally = Tally()
    at = first
    while at < last:
        # Prices reach the end of tomorrow from 13:00, else the end of today.
        local = at.astimezone(TZ)
        days = 2 if local.hour >= 13 else 1
        local_end = datetime.combine(local.date() + timedelta(days=days), datetime.min.time(), TZ)
        n = min(history.at(local_end) - i, len(history.spot) - i)
        while n > 0 and history.spot[i + n - 1] is None:
            n -= 1
        run = min(every, n, history.at(last) - i)
        if foresight == "self":
            actions = None
        else:
            if foresight == "perfect":
                pairs = [_real(history, i + k) for k in range(n)]
                house, sun = [p[0] for p in pairs], [p[1] for p in pairs]
            else:
                house, sun = p75_house(history, at, n), persistence_sun(history, i, n)
            request = PlanRequest(
                slots=_slots(history, site, i, n, house, sun),
                battery=battery,
                soc_now_pct=min(100.0, max(0.0, soc_kwh / battery.usable_kwh * 100)),
                end_value=end_value,
            )
            actions = plan_mode(request, mode, wear).plan.decisions
        real_house_sun = [_real(history, i + k) for k in range(run)]
        slots = _slots(
            history, site, i, run, [h for h, _ in real_house_sun], [s for _, s in real_house_sun]
        )
        for k, slot in enumerate(slots):
            action = ACTION_SELF_CONSUME if actions is None else actions[k].action
            planned_kw = 0.0 if actions is None else actions[k].battery_kw
            power = action_power(slot, battery, soc_kwh, action, planned_kw)
            outcome = run_slot(slot, battery, soc_kwh, action, power)
            soc_kwh = battery.usable_kwh * outcome.soc_end_pct / 100
            tally.min_soc_pct = min(tally.min_soc_pct, outcome.soc_end_pct)
            if history.grid[i + k] is None:
                continue  # no meter reading: left out of every run, as of the actual one
            tally.cost += outcome.cost
            day = slot.start.astimezone(TZ).date().isoformat()
            tally.daily[day] = tally.daily.get(day, 0.0) + outcome.cost
            hours = slot.hours
            tally.import_kwh += max(0.0, outcome.grid_kw) * hours
            tally.export_kwh += max(0.0, -outcome.grid_kw) * hours
            if outcome.battery_kw > 0 and outcome.grid_kw > 0:
                tally.grid_charge_kwh += min(outcome.battery_kw, outcome.grid_kw) * hours
            if outcome.battery_kw > 0:
                tally.charge_kwh += outcome.battery_kw * hours
            if outcome.battery_kw < 0:
                tally.discharge_kwh += -outcome.battery_kw * hours
                tally.battery_export_kwh += max(0.0, min(-outcome.battery_kw, -outcome.grid_kw)) * hours
        i += run
        at += SLOT * run
    return tally, start_kwh, soc_kwh


def actual(history: History, site: Site, first: datetime, last: datetime) -> tuple:
    tally = Tally()
    i0, i1 = history.at(first), history.at(last)
    for i in range(i0, i1):
        grid, spot = history.grid[i], history.spot[i]
        if grid is None or spot is None:
            continue
        hours = 0.25
        day = (history.t0 + SLOT * i).astimezone(TZ).date().isoformat()
        cost = grid * hours * (site.buy(spot) if grid >= 0 else site.sell(spot))
        tally.daily[day] = tally.daily.get(day, 0.0) + cost
        if grid >= 0:
            tally.cost += grid * hours * site.buy(spot)
            tally.import_kwh += grid * hours
        else:
            tally.cost += grid * hours * site.sell(spot)
            tally.export_kwh += -grid * hours
    usable = site.battery.usable_kwh
    soc0 = next(history.soc[i] for i in range(i0, i1) if history.soc[i] is not None)
    soc1 = next(history.soc[i] for i in range(i1 - 1, i0, -1) if history.soc[i] is not None)
    return tally, usable * soc0 / 100, usable * soc1 / 100


def revenue_by_day(history: History, site: Site, first: datetime, last: datetime) -> dict[str, float]:
    """The cost (öre) per local day of the site's whole load (house + car) bought from the grid, no sun and no
    battery, over the quarter hours with a meter reading. Sigenergy's daily "revenue" is this less the day's cost
    (`Tally.daily`); fitted on the owner's calendar, 2026-09-01..11-12, with `--house balance`."""
    out: dict[str, float] = {}
    for i in range(history.at(first), history.at(last)):
        house, spot = history.house(i), history.spot[i]
        if history.grid[i] is None or house is None or spot is None:
            continue
        day = (history.t0 + SLOT * i).astimezone(TZ).date().isoformat()
        out[day] = out.get(day, 0.0) + (house + history.car_kw(i)) * 0.25 * site.buy(spot)
    return out


def _gate(factor: str | None) -> BatteryMode:
    """Balanced with its gate margin overridden: `off` sells nothing, a number is the margin in multiples of the wear."""
    balanced = MODE_DEFS[MODE_BALANCED]
    if factor is None:
        return balanced
    if factor == "off":
        return replace(balanced, export=EXPORT_OFF, gate_factor=0.0)
    return replace(balanced, export=EXPORT_GATE, gate_factor=float(factor))


def _row(result: tuple, end_value: float, usable_kwh: float, days: float) -> dict:
    tally, start_kwh, end_kwh = result
    return {
        "cost": tally.cost / 100,
        "adjusted": (tally.cost - (end_kwh - start_kwh) * end_value) / 100,
        "start_kwh": start_kwh,
        "end_kwh": end_kwh,
        "import_kwh": tally.import_kwh,
        "export_kwh": tally.export_kwh,
        "grid_charge_kwh": tally.grid_charge_kwh,
        "battery_export_kwh": tally.battery_export_kwh,
        "fce_per_day": tally.discharge_kwh / usable_kwh / days,
        "throughput_kwh": tally.charge_kwh + tally.discharge_kwh,
        "min_soc_pct": tally.min_soc_pct,
    }


#: The worker processes' shared inputs (set once per process by `_init`).
_SHARED: dict = {}


def _init(history: History, site: Site, first: datetime, last: datetime, end_value: float, every: int) -> None:
    _SHARED.update(history=history, site=site, first=first, last=last, end_value=end_value, every=every)


def _job(job: tuple) -> tuple:
    key, foresight, mode, wear = job
    s = _SHARED
    result = replay(s["history"], s["site"], s["first"], s["last"], foresight, s["end_value"], mode, wear, s["every"])
    return key, result


WEARS = (0.0, 5.0, 15.0, 25.0, 40.0)
GATES = ("off", "0", "1", "2", "3")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", type=Path, default=Path("."))
    parser.add_argument("--first", default="2026-10-13", help="first local day (replay starts 13:00)")
    parser.add_argument("--last", default="2026-11-12", help="last local day (replay ends 13:00)")
    parser.add_argument("--usable-kwh", type=float, default=40.3)
    parser.add_argument("--max-kw", type=float, default=13.2, help="the plant's max active power, both ways")
    parser.add_argument("--min-soc", type=float, default=0.0)
    parser.add_argument("--efficiency", type=float, default=0.94, help="measured over the default period")
    parser.add_argument("--import-cap", type=float, default=13.8, help="20 A x 3 x 230 V")
    parser.add_argument("--export-cap", type=float, default=13.2)
    parser.add_argument("--fee", type=float, default=61.9, help="energy tax + grid fee, öre/kWh before VAT")
    parser.add_argument("--fee-percent", type=float, default=2.8, help="grid fee as %% of spot, before VAT")
    parser.add_argument("--vat", type=float, default=25.0)
    parser.add_argument("--sell-credit", type=float, default=0.0, help="öre/kWh on top of spot for export")
    parser.add_argument("--sell-percent", type=float, default=0.0, help="%% of spot on top of spot for export")
    parser.add_argument("--house", choices=("load", "balance"), default="load",
                        help="the house: the total-load sensor, or grid + sun - battery (Sigenergy's daily load)")
    parser.add_argument("--revenue", type=Path, help="Sigenergy's Revenue Calendar CSV (date,revenue_kr)")
    parser.add_argument("--mode", choices=(*MODE_DEFS, "all"), default="all")
    parser.add_argument("--wear", type=float, default=15.0, help="öre per kWh through the battery, each way")
    parser.add_argument("--gate-factor", default=None, help="Balanced's gate margin x wear, or 'off'")
    parser.add_argument("--matrix", action="store_true", help="every mode x wear x foresight, and the gate")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--every", type=int, default=96, help="quarter hours between plans (96: day by day)")
    parser.add_argument("--json", type=Path, help="also write the table's numbers here")
    args = parser.parse_args()

    first = datetime.combine(date.fromisoformat(args.first), datetime.min.time(), TZ).replace(hour=13)
    last = datetime.combine(date.fromisoformat(args.last), datetime.min.time(), TZ).replace(hour=13)
    history_start = (first - timedelta(weeks=7)).astimezone(timezone.utc).replace(hour=0, minute=0)
    history_end = (last + timedelta(days=2)).astimezone(timezone.utc).replace(hour=0, minute=0)
    args.cache.mkdir(parents=True, exist_ok=True)
    history = load_history(history_start, history_end, args.cache)
    history.balance = args.house == "balance"

    site = Site(
        battery=BatteryParams(
            usable_kwh=args.usable_kwh,
            max_charge_kw=args.max_kw,
            max_discharge_kw=args.max_kw,
            min_soc_pct=args.min_soc,
            max_soc_pct=100.0,
            round_trip_efficiency=args.efficiency,
        ),
        import_cap_kw=args.import_cap,
        export_cap_kw=args.export_cap,
        fee_minor=args.fee,
        fee_percent=args.fee_percent,
        vat_percent=args.vat,
        sell_credit_minor=args.sell_credit,
        sell_percent=args.sell_percent,
    )
    i0, i1 = history.at(first), history.at(last)
    buys = [site.buy(history.spot[i]) for i in range(i0, i1) if history.spot[i] is not None]
    end_value = statistics.median(buys) * args.efficiency
    days = (last - first).total_seconds() / 86400

    jobs: list[tuple] = [(("self_consumption", "-", "-"), "self", MODE_DEFS[MODE_SELF_CONSUMPTION], 0.0)]
    if args.matrix:
        for foresight in ("perfect", "forecast"):
            for name in (MODE_BALANCED, MODE_MAX_PROFIT):
                for wear in WEARS:
                    jobs.append(((name, foresight, wear), foresight, MODE_DEFS[name], wear))
            for gate in GATES:
                jobs.append(((f"balanced gate {gate}", foresight, args.wear), foresight, _gate(gate), args.wear))
    else:
        names = [m for m in MODE_DEFS if m != MODE_SELF_CONSUMPTION] if args.mode == "all" else [args.mode]
        for foresight in ("perfect", "forecast"):
            for name in names:
                mode = _gate(args.gate_factor) if name == MODE_BALANCED else MODE_DEFS[name]
                jobs.append(((name, foresight, args.wear), foresight, mode, args.wear))
    jobs = list(dict.fromkeys(jobs))

    results: dict = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=args.workers, initializer=_init, initargs=(history, site, first, last, end_value, args.every)
    ) as pool:
        for key, result in pool.map(_job, jobs):
            results[key] = result

    usable = site.battery.usable_kwh
    rows = {("actual", "-", "-"): _row(actual(history, site, first, last), end_value, usable, days)}
    rows["actual", "-", "-"]["fce_per_day"] = None
    rows["actual", "-", "-"]["throughput_kwh"] = None
    rows["actual", "-", "-"]["battery_export_kwh"] = None
    rows["actual", "-", "-"]["min_soc_pct"] = min(s for s in history.soc[i0:i1] if s is not None)
    for key, _f, _m, _w in jobs:
        rows[key] = _row(results[key], end_value, usable, days)
    base = rows["self_consumption", "-", "-"]["adjusted"]
    base_worn = base + rows["self_consumption", "-", "-"]["throughput_kwh"] * args.wear / 100

    print(f"Period {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} local, end value {end_value:.1f} öre/kWh")
    print("| Run | Foresight | Wear öre | Cost kr, end energy valued | vs self-consumption kr | "
          f"same, wear at {args.wear:g} öre paid kr | FCE/day | "
          "Import kWh | Export kWh | Sold from battery kWh | Grid charge kWh | Min SoC % |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")

    def fmt(value, spec: str) -> str:
        return "" if value is None else format(value, spec)

    for (name, foresight, wear), row in rows.items():
        foresight_label = {"perfect": "perfect", "forecast": "p75", "-": ""}[foresight]
        wear_label = "" if wear == "-" else f"{wear:g}"
        grid_charge = "" if name == "actual" else f"{row['grid_charge_kwh']:.0f}"
        worn = None
        if row["throughput_kwh"] is not None:
            worn = row["adjusted"] + row["throughput_kwh"] * args.wear / 100 - base_worn
        print(f"| {name} | {foresight_label} | {wear_label} | {row['adjusted']:.0f} | {row['adjusted'] - base:+.0f} | "
              f"{fmt(worn, '+.0f')} | {fmt(row['fce_per_day'], '.2f')} | {row['import_kwh']:.0f} | {row['export_kwh']:.0f} | "
              f"{fmt(row['battery_export_kwh'], '.0f')} | {grid_charge} | {fmt(row['min_soc_pct'], '.0f')} |")
    if args.json:
        args.json.write_text(json.dumps(
            {"end_value": end_value, "rows": [{"run": k[0], "foresight": k[1], "wear": k[2], **v} for k, v in rows.items()]},
            indent=2,
        ))
    if args.revenue:
        _print_revenue(args.revenue, revenue_by_day(history, site, first, last), actual(history, site, first, last),
                       {key: results[key] for key, _f, _m, _w in jobs})
    if any(math.isnan(r["cost"]) for r in rows.values()):
        raise SystemExit("a cost came out NaN")


def _print_revenue(path: Path, baseline: dict[str, float], real: tuple, runs: dict) -> None:
    """Each run's daily revenue (kr) on Sigenergy's definition, by month and in total, beside the calendar; then the
    day-level fit of the actual run and how often each plan beat the actual day."""
    import csv  # noqa: PLC0415

    with path.open() as handle:
        calendar = {row["date"]: float(row["revenue_kr"]) for row in csv.DictReader(handle)}
    days = sorted(d for d in calendar if d in baseline)
    names = {("actual", "-", "-"): real, **runs}
    revenue = {key: {d: (baseline[d] - result[0].daily.get(d, 0.0)) / 100 for d in days} for key, result in names.items()}
    months = sorted({d[:7] for d in days})
    print(f"\nRevenue as Sigenergy counts it, {days[0]}..{days[-1]} ({len(days)} days), kr")
    print("| Run | Foresight | " + " | ".join(months) + " | Total | Mean/day | Days above actual |")
    print("|---|---|" + "---|" * (len(months) + 3))
    cal_row = [sum(calendar[d] for d in days if d[:7] == m) for m in months]
    print("| Calendar (Sigenergy app) | | " + " | ".join(f"{v:.0f}" for v in cal_row)
          + f" | {sum(cal_row):.0f} | {sum(cal_row) / len(days):.1f} | |")
    for (name, foresight, _wear), by_day in revenue.items():
        per_month = [sum(v for d, v in by_day.items() if d[:7] == m) for m in months]
        above = "" if name == "actual" else str(sum(by_day[d] > revenue["actual", "-", "-"][d] for d in days))
        label = {"perfect": "perfect", "forecast": "p75", "-": ""}[foresight]
        print(f"| {name} | {label} | " + " | ".join(f"{v:.0f}" for v in per_month)
              + f" | {sum(per_month):.0f} | {sum(per_month) / len(days):.1f} | {above} |")
    actual_days = revenue["actual", "-", "-"]
    diffs = [actual_days[d] - calendar[d] for d in days]
    mean_a = sum(actual_days.values()) / len(days)
    mean_c = sum(calendar[d] for d in days) / len(days)
    cov = sum((actual_days[d] - mean_a) * (calendar[d] - mean_c) for d in days)
    var_a = sum((actual_days[d] - mean_a) ** 2 for d in days)
    var_c = sum((calendar[d] - mean_c) ** 2 for d in days)
    print(f"Actual against the calendar: r = {cov / math.sqrt(var_a * var_c):.4f}, "
          f"mean |diff| = {sum(abs(x) for x in diffs) / len(days):.1f} kr, mean diff = {sum(diffs) / len(days):+.1f} kr")
    print("\n| Day | Calendar | " + " | ".join(f"{k[0]} {k[1]}".replace(" -", "") for k in revenue) + " |")
    print("|---|---|" + "---|" * len(revenue))
    for d in days:
        print(f"| {d} | {calendar[d]:.1f} | " + " | ".join(f"{revenue[k][d]:.1f}" for k in revenue) + " |")


if __name__ == "__main__":
    main()
