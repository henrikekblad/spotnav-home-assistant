"""What closes the gap between the battery plan on forecasts and the plan with perfect foresight (3.0 wave 4, W4-A).

Not part of the integration: a research tool beside `scripts/battery_plan_backtest.py`, whose data, replay and
prices it uses (run it with the repository's `.venv` and the same `--cache DIR`). It reads, never writes, anything
outside DIR. Results: `plans/battery_plan_backtest/README.md` in the app repository, "Closing the forecast gap".

Each run plans with one house forecast and one sun forecast (`--runs`, names below) and is run against what really
happened, exactly as the battery plan backtest's replay does. One factor changes at a time; the other stays perfect.

Sun (`sun=`)
* `perfect`, `persist` (the same quarter hour of the day before; the backtest's `p75` runs).
* `om`: Open-Meteo's archived forecast as it was made a day ahead (`shortwave_radiation_previous_day1`, the hour's
  global horizontal irradiance) times one site factor (kW per W/m²) fitted on the weeks before the period. This
  stands in for a solar forecast as it was known at planning time: the Solcast history kept in Influx starts on
  2026-10-12 and holds day totals only.
* `solcast`: Solcast's p50 as it was known at the plan's time (Influx: `forecast_remaining_today`,
  `forecast_tomorrow`), each day's total spread over the day by the clear-sky shape of the last 14 days (the
  highest production per quarter hour of the day). From 2026-10-13 only.
* `+corr`: the W2-S correction (`site/solar_correction.py`) applied to that forecast, learnt each night on the raw
  day-ahead forecast against the outcome, cloud cover from Open-Meteo's day-ahead forecast.
* `+inday`: today's rest scaled by today's production so far over the forecast so far (damped and clamped like the
  house's in-day factor); only changes something when the plan is made again during the day.
* `xK`: the sun forecast times K (0.8: towards p10, 1.2: towards p90; Solcast's own p10/p90 are not in the history).

House (`house=`)
* `perfect`, `bt75` (the backtest's own p75 profile).
* `lf`: the integration's forecast (`site/load_forecast.py`), from hour rows built from the history, with
  `q50`/`q75` (the quantile), `t`/`not` (the temperature term on/off; the history's and the forecast's temperatures
  from Open-Meteo, the forecast made a day ahead) and `id`/`noid` (the in-day adjustment).

Plan (`every=`, `reserve=`)
* `every=4` plans again every hour, as the shadow plan does; 96 plans once a day at 13:00.
* `reserve=H`: the plan keeps back, at each slot, the energy the next H hours would need beyond the house forecast
  if the house drew its p90 (a soft floor: falling below it costs the dearest buy price of those hours).

Error band (`band=`, 3.0 wave 6, W6-C): the plan counts on a pessimistic figure from the learnt band
(`site/forecast_band.py`), learnt each plan from the last 14 days' misses of the same forecasts as made at 13:00 the
day before. Parts joined by `+`: `hd50`/`hd33` the house's p90 in the dearest half/third of the horizon's hours
(`battery/defensive.py`'s rule), `ha` the house's p90 in every hour, `sun` the sun's p10.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import statistics
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import battery_plan_backtest as bt  # noqa: E402
from custom_components.spotnav.battery import plan as plan_module  # noqa: E402
from custom_components.spotnav.battery.plan_model import MODE_DEFS, BatteryParams  # noqa: E402
from custom_components.spotnav.planning.demand_charge import HourMean  # noqa: E402
from custom_components.spotnav.site import forecast_band as fb  # noqa: E402
from custom_components.spotnav.site import load_forecast as lf  # noqa: E402
from custom_components.spotnav.site import solar_correction as sc  # noqa: E402

TZ = bt.TZ
SLOT = bt.SLOT
HOUR = timedelta(hours=1)
#: Bjärred, rounded to the town (the forecast is a weather model's grid cell anyway).
LATITUDE, LONGITUDE = 55.72, 13.02
OM_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
OM_VARS = (
    "shortwave_radiation,shortwave_radiation_previous_day1,temperature_2m,temperature_2m_previous_day1,"
    "cloud_cover_previous_day1"
)
SOLCAST = ("forecast_tomorrow", "forecast_remaining_today")
#: The in-day sun factor, as the house's: damping, clamp, and the forecast energy below which the ratio says nothing.
SUN_INDAY_DAMPING = 0.95
SUN_INDAY_MIN, SUN_INDAY_MAX = 0.3, 2.0
SUN_INDAY_MIN_KWH = 1.0


# ----------------------------------------------------------------------------- data


def load_open_meteo(cache: Path, start: date, end: date) -> dict[str, dict[datetime, float]]:
    """Open-Meteo's hourly series per UTC hour start. Radiation is the mean of the hour before its stamp, so it is
    moved back an hour; temperature and cloud cover are instants and kept."""
    path = cache / f"open_meteo_{start:%Y%m%d}_{end:%Y%m%d}.json.gz"

    def fetch() -> object:
        query = (
            f"{OM_URL}?latitude={LATITUDE}&longitude={LONGITUDE}&hourly={OM_VARS}"
            f"&start_date={start.isoformat()}&end_date={end.isoformat()}&timezone=UTC"
        )
        return json.loads(bt._get(urllib.request.Request(query), "open-meteo"))

    raw = bt._cached(path, fetch)["hourly"]
    out: dict[str, dict[datetime, float]] = {}
    stamps = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in raw["time"]]
    for name, values in raw.items():
        if name == "time":
            continue
        shift = HOUR if name.startswith("shortwave_radiation") else timedelta(0)
        out[name] = {t - shift: v for t, v in zip(stamps, values, strict=True) if v is not None}
    return out


def load_solcast(cache: Path, start: datetime, end: datetime) -> dict[str, list[tuple[datetime, float]]]:
    """Solcast's day totals (kWh, p50) as Influx kept them: `(time, value)` in time order."""
    path = cache / f"solcast_{start:%Y%m%d}_{end:%Y%m%d}.json.gz"
    raw = bt._cached(
        path,
        lambda: {
            name: bt._influx_minutes("kWh", f"sensor.solcast_pv_forecast_{name}", start, end, raw=True)
            for name in SOLCAST
        },
    )
    return {
        name: [(datetime.fromisoformat(t).replace(tzinfo=timezone.utc), v) for t, v in rows if v is not None]
        for name, rows in raw.items()
    }


def _value_at(series: list[tuple[datetime, float]], at: datetime) -> float | None:
    """The last value written before [at]."""
    value = None
    for t, v in series:
        if t > at:
            break
        value = v
    return value


# ----------------------------------------------------------------------------- forecasts


@dataclass
class Context:
    """What every forecaster may read: the history, the weather archive, Solcast's days, the site's GHI factor."""

    history: bt.History
    om: dict[str, dict[datetime, float]]
    solcast: dict[str, list[tuple[datetime, float]]] | None
    kw_per_ghi: float
    rows: list[HourMean]
    #: The corrections' factors per local day (`(clear, cloudy)`), per raw sun source, as they stood that morning.
    corrections: dict[str, dict[date, tuple[float, float]]]
    observed_max_kw: list[float]

    def quarter(self, i: int) -> datetime:
        return self.history.t0 + SLOT * i


def _hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def house_rows(history: bt.History, om: dict[str, dict[datetime, float]]) -> list[HourMean]:
    """The history's complete hours as the integration keeps them: the house without the car, the hour's outdoor
    temperature (Open-Meteo's, as the weather entity would have read it)."""
    rows = []
    temperature = om["temperature_2m"]
    for q0 in range(0, len(history.car) - 3, 4):
        values = [history.house(q0 + k) for k in range(4)]
        if any(v is None for v in values):
            continue
        start = history.t0 + SLOT * q0
        kw = sum(values) / 4
        rows.append(HourMean(start=start, kw=kw, house_kw=kw, outdoor_c=temperature.get(start)))
    return rows


def ghi_factor(history: bt.History, om: dict[str, dict[datetime, float]], until: datetime) -> float:
    """The site's kW per W/m² of the day-ahead GHI forecast, over the history before [until]."""
    pv = ghi = 0.0
    for i in range(history.at(until)):
        t = history.t0 + SLOT * i
        g = om["shortwave_radiation_previous_day1"].get(_hour(t))
        if g is None or history.pv[i] is None:
            continue
        pv += max(0.0, history.pv[i])
        ghi += g
    return pv / ghi


def envelope_shape(history: bt.History, at: datetime) -> dict[tuple[int, int], float]:
    """The highest production per local (hour, minute) over the 14 days before [at]: a clear day's shape."""
    shape: dict[tuple[int, int], float] = {}
    for i in range(max(0, history.at(at - timedelta(days=14))), history.at(at)):
        value = history.pv[i]
        if value is None:
            continue
        local = (history.t0 + SLOT * i).astimezone(TZ)
        key = (local.hour, local.minute)
        shape[key] = max(shape.get(key, 0.0), value)
    return shape


def raw_sun(ctx: Context, source: str, at: datetime, i: int, n: int) -> list[float]:
    """[source]'s forecast (kW per quarter) as known at [at] for the quarters i..i+n."""
    history = ctx.history
    if source == "persist":
        return bt.persistence_sun(history, i, n)
    if source == "perfect":
        return [max(0.0, history.pv[i + k] or 0.0) for k in range(n)]
    if source == "om":
        ghi = ctx.om["shortwave_radiation_previous_day1"]
        return [ctx.kw_per_ghi * ghi.get(_hour(ctx.quarter(i + k)), 0.0) for k in range(n)]
    if source == "solcast":
        assert ctx.solcast is not None
        shape = envelope_shape(history, at)
        today = at.astimezone(TZ).date()
        totals = {
            today: _value_at(ctx.solcast["forecast_remaining_today"], at),
            today + timedelta(days=1): _value_at(ctx.solcast["forecast_tomorrow"], at),
        }
        # Each day's shape over the quarters the total covers: the rest of today, the whole of tomorrow.
        weights: dict[date, float] = {}
        keys = []
        for k in range(n):
            local = ctx.quarter(i + k).astimezone(TZ)
            keys.append((local.date(), shape.get((local.hour, local.minute), 0.0)))
            weights[local.date()] = weights.get(local.date(), 0.0) + keys[-1][1]
        out = []
        for day, weight in keys:
            total = totals.get(day)
            out.append(0.0 if not total or weights[day] <= 0 else total * 4 * weight / weights[day])
        return out
    raise ValueError(source)


def day_ahead_sun(ctx: Context, source: str, i0: int, i1: int) -> list[float]:
    """[source]'s forecast for quarters i0..i1 as the 13:00 plan of the day before had it (the correction's raw
    series)."""
    out: list[float] = []
    i = i0
    while i < i1:
        local = ctx.quarter(i).astimezone(TZ)
        made = datetime.combine(local.date() - timedelta(days=1), datetime.min.time(), TZ).replace(hour=13)
        day_end = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), TZ)
        upto = min(i1, ctx.history.at(day_end))
        whole = raw_sun(ctx, source, made, ctx.history.at(made), upto - ctx.history.at(made))
        out += whole[i - ctx.history.at(made):]
        i = upto
    return out


def correction_factors(ctx: Context, source: str, first: date, last: date) -> dict[date, tuple[float, float]]:
    """The W2-S correction run night by night over [first]..[last] on [source]'s day-ahead forecast: the factors
    each morning, `(clear, cloudy)`."""
    history = ctx.history
    cloud = ctx.om["cloud_cover_previous_day1"]
    correction = sc.SolarCorrection()
    samples: list[sc.HourSample] = []
    out: dict[date, tuple[float, float]] = {}
    day = first - timedelta(days=sc.WINDOW_DAYS + 1)
    while day <= last:
        # The night before [day]: learn from the days up to yesterday.
        evidence = sc.day_evidence(samples, "Europe/Stockholm")
        correction.nightly(evidence, day, curtailed=None)
        out[day] = (correction.factors[sc.KIND_CLEAR], correction.factors[sc.KIND_CLOUDY])
        start = datetime.combine(day, datetime.min.time(), TZ)
        i0, i1 = history.at(start), history.at(start + timedelta(days=1))
        if i0 >= 96 and i1 <= len(history.pv):
            forecast = day_ahead_sun(ctx, source, i0, i1)
            for h in range(0, i1 - i0 - 3, 4):
                actual = [history.pv[i0 + h + k] for k in range(4)]
                if any(a is None for a in actual):
                    continue
                t = ctx.quarter(i0 + h)
                samples.append(
                    sc.HourSample(
                        start=t,
                        forecast_kwh=sum(forecast[h:h + 4]) / 4,
                        actual_kwh=max(0.0, sum(actual) / 4),
                        cloudy=cloud.get(_hour(t), 0.0) >= sc.CLOUDY_PCT,
                    )
                )
        samples = [s for s in samples if s.start >= start - timedelta(days=sc.WINDOW_DAYS + 1)]
        day += timedelta(days=1)
    return out


def corrected(ctx: Context, source: str, at: datetime, i: int, values: list[float]) -> list[float]:
    """[values] with the correction as it stood the morning of [at]'s day, capped like the integration's."""
    clear, cloudy = ctx.corrections[source][at.astimezone(TZ).date()]
    cloud = ctx.om["cloud_cover_previous_day1"]
    observed = ctx.observed_max_kw[i]
    out = []
    for k, value in enumerate(values):
        factor = cloudy if cloud.get(_hour(ctx.quarter(i + k)), 0.0) >= sc.CLOUDY_PCT else clear
        out.append(min(value * factor, max(value, observed)))
    return out


def sun_inday(ctx: Context, source: str, at: datetime, i: int, values: list[float], corr: bool) -> list[float]:
    """The rest of today scaled by today's production so far over the forecast so far (the corrected forecast
    when [corr], so the two do not count the same miss twice)."""
    history = ctx.history
    local = at.astimezone(TZ)
    midnight = datetime.combine(local.date(), datetime.min.time(), TZ)
    i0 = history.at(midnight)
    if i <= i0:
        return values
    made = day_ahead_sun(ctx, source, i0, i)
    if corr:
        made = corrected(ctx, source, at, i0, made)
    actual = [history.pv[k] for k in range(i0, i)]
    pairs = [(f, a) for f, a in zip(made, actual, strict=True) if a is not None]
    expected = sum(f for f, _a in pairs) / 4
    if expected < SUN_INDAY_MIN_KWH:
        return values
    ratio = sum(a for _f, a in pairs) / 4 / expected
    factor = min(SUN_INDAY_MAX, max(SUN_INDAY_MIN, 1.0 + SUN_INDAY_DAMPING * (ratio - 1.0)))
    end = history.at(midnight + timedelta(days=1)) - i
    return [v * factor if k < end else v for k, v in enumerate(values)]


def house_forecast(ctx: Context, spec: str, at: datetime, i: int, n: int, quantile: float | None = None) -> list[float]:
    history = ctx.history
    if spec == "perfect":
        return [bt._real(history, i + k)[0] for k in range(n)]
    if spec == "bt75":
        return bt.p75_house(history, at, n)
    parts = spec.split("_")
    assert parts[0] == "lf", spec
    q = quantile if quantile is not None else (0.5 if "q50" in parts else 0.9 if "q90" in parts else 0.75)
    temperature = "not" not in parts
    inday = "noid" not in parts
    rows = [r for r in ctx.rows if at - timedelta(days=lf.WINDOW_DAYS + 2) <= r.start < at]
    recent = [r.house_kw for r in rows[-24:]]
    temps = None
    if temperature:
        fc = ctx.om["temperature_2m_previous_day1"]
        temps = {_hour(ctx.quarter(i + k)): fc.get(_hour(ctx.quarter(i + k))) for k in range(0, n + 4, 4)}
        temps = {t: v for t, v in temps.items() if v is not None}
        # Without the term, rows carry no temperature: the fit then stays off.
    else:
        rows = [HourMean(start=r.start, kw=r.kw, house_kw=r.house_kw) for r in rows]
    saved = lf.QUANTILE
    lf.QUANTILE = q
    try:
        forecast = lf.forecast_house_load(
            rows, at, "Europe/Stockholm", flat_kw=sum(recent) / len(recent) if recent else None,
            temperatures=temps, inday=inday, horizon_h=n // 4 + 2,
        )
    finally:
        lf.QUANTILE = saved
    by_hour = forecast.by_hour()
    fallback = statistics.mean(by_hour.values()) if by_hour else 1.0
    return [by_hour.get(_hour(ctx.quarter(i + k)), fallback) for k in range(n)]


@dataclass(frozen=True)
class Variant:
    name: str
    house: str = "perfect"
    sun: str = "perfect"
    every: int = 96
    reserve_h: int = 0
    band: str = ""

    @staticmethod
    def parse(text: str) -> Variant:
        """`house=lf_q75_t_id,sun=om+corr,every=4,reserve=3` (any part may be left out)."""
        fields = dict(part.split("=", 1) for part in text.split(",") if part)
        return Variant(
            name=text,
            house=fields.get("house", "perfect"),
            sun=fields.get("sun", "perfect"),
            every=int(fields.get("every", 96)),
            reserve_h=int(fields.get("reserve", 0)),
            band=fields.get("band", ""),
        )


def _sun_spec(spec: str) -> tuple[str, list[str], float]:
    parts = spec.split("+")
    source, extras = parts[0], parts[1:]
    scale = 1.0
    for extra in list(extras):
        if extra.startswith("x"):
            scale = float(extra[1:])
            extras.remove(extra)
    return source, extras, scale


def sun_forecast(ctx: Context, spec: str, at: datetime, i: int, n: int) -> list[float]:
    source, extras, scale = _sun_spec(spec)
    sun = raw_sun(ctx, source, at, i, n)
    if "corr" in extras:
        sun = corrected(ctx, source, at, i, sun)
    if "inday" in extras:
        sun = sun_inday(ctx, source, at, i, sun, "corr" in extras)
    return [max(0.0, v * scale) for v in sun]


#: Per (house spec, sun spec, local day): the day's hours as the 13:00 forecast of the day before had them, beside the
#: outcome: `(start, house kW, house kW measured, sun kWh, sun kWh measured)`. Per process.
_DAY_PAIRS: dict[tuple[str, str, date], list[tuple[datetime, float, float, float, float]]] = {}


def day_pairs(ctx: Context, variant: Variant, day: date) -> list[tuple[datetime, float, float, float, float]]:
    key = (variant.house, variant.sun, day)
    if key in _DAY_PAIRS:
        return _DAY_PAIRS[key]
    history = ctx.history
    made = datetime.combine(day - timedelta(days=1), datetime.min.time(), TZ).replace(hour=13)
    start = datetime.combine(day, datetime.min.time(), TZ)
    i_made, i0, i1 = history.at(made), history.at(start), history.at(start + timedelta(days=1))
    out: list[tuple[datetime, float, float, float, float]] = []
    if i_made >= 0 and i1 <= len(history.pv):
        n = i1 - i_made
        house = house_forecast(ctx, variant.house, made, i_made, n)[i0 - i_made:]
        sun = sun_forecast(ctx, variant.sun.replace("+inday", ""), made, i_made, n)[i0 - i_made:]
        for h in range(0, i1 - i0 - 3, 4):
            houses = [history.house(i0 + h + k) for k in range(4)]
            pvs = [history.pv[i0 + h + k] for k in range(4)]
            if any(v is None for v in houses) or any(v is None for v in pvs):
                continue
            out.append((
                ctx.quarter(i0 + h).astimezone(timezone.utc),
                sum(house[h:h + 4]) / 4, sum(houses) / 4,  # type: ignore[arg-type]
                sum(sun[h:h + 4]) / 4, max(0.0, sum(pvs) / 4),  # type: ignore[arg-type]
            ))
    _DAY_PAIRS[key] = out
    return out


def learnt_bands(ctx: Context, variant: Variant, at: datetime) -> tuple[fb.ErrorBand | None, fb.ErrorBand | None]:
    """The house's and the sun's bands as the log would hold them at [at]: the misses of the days before today."""
    today = at.astimezone(TZ).date()
    rows = [row for k in range(fb.WINDOW_DAYS, 0, -1) for row in day_pairs(ctx, variant, today - timedelta(days=k))]
    house = fb.learn([(t, f, a) for t, f, a, _fs, _as in rows], at, "Europe/Stockholm", fb.FORM_ADDITIVE)
    sun = fb.learn([(t, fs, a_s) for t, _f, _a, fs, a_s in rows], at, "Europe/Stockholm", fb.FORM_RATIO)
    return house, sun


#: Counts per process of what the band did: plans made with it, and without one (learning).
BAND_STATS = {"plans": 0, "house_learning": 0, "sun_learning": 0}


def apply_band(ctx: Context, variant: Variant, at: datetime, i: int, house: list[float], sun: list[float]):
    parts = set(variant.band.split("+"))
    house_band, sun_band = learnt_bands(ctx, variant, at)
    BAND_STATS["plans"] += 1
    BAND_STATS["house_learning"] += house_band is None
    BAND_STATS["sun_learning"] += sun_band is None
    n = len(house)
    times = [ctx.quarter(i + k) for k in range(n)]
    if house_band is not None and parts & {"ha", "hd50", "hd33"}:
        dear = set(range(n))
        if "ha" not in parts:
            share = 0.5 if "hd50" in parts else 1 / 3
            # The hours' mean buy price; the dearest [share] of the horizon's hours.
            hours: dict[datetime, list[float]] = {}
            for k in range(n):
                spot = ctx.history.spot[i + k]
                if spot is not None:
                    hours.setdefault(_hour(times[k]), []).append(bt_site().buy(spot))
            means = {h: sum(v) / len(v) for h, v in hours.items() if v}
            cut = fb.quantile(list(means.values()), 1 - share)
            dear = {k for k in range(n) if means.get(_hour(times[k]), cut - 1) >= cut}
        house = [house_band.bounds(v, times[k])[1] if k in dear else v for k, v in enumerate(house)]
    if sun_band is not None and "sun" in parts:
        sun = [sun_band.bounds(v, times[k])[0] for k, v in enumerate(sun)]
    return house, sun


def make_forecaster(ctx: Context, variant: Variant):
    """The replay's forecaster for [variant]; also arms the reserve (`_RESERVE`) per plan."""

    def forecaster(history: bt.History, at: datetime, i: int, n: int) -> tuple[list[float], list[float]]:
        house = house_forecast(ctx, variant.house, at, i, n)
        sun = sun_forecast(ctx, variant.sun, at, i, n)
        if variant.band:
            house, sun = apply_band(ctx, variant, at, i, house, sun)
        _RESERVE.clear()
        if variant.reserve_h:
            high = house_forecast(ctx, variant.house.replace("q50", "q90").replace("q75", "q90"), at, i, n,
                                  quantile=0.9) if variant.house.startswith("lf") else None
            if high is not None:
                spare = [max(0.0, h - p) / 4 for h, p in zip(high, house, strict=True)]  # kWh per quarter
                window = variant.reserve_h * 4
                for k in range(n):
                    start = history.t0 + SLOT * (i + k)
                    upto = min(n, k + 1 + window)
                    need = sum(spare[k + 1:upto])
                    price = max((bt_site().buy(history.spot[i + j]) for j in range(k + 1, upto)), default=0.0)
                    _RESERVE[start] = (need, price)
        return house, sun

    return forecaster


# ----------------------------------------------------------------------------- the reserve, in the plan's search

#: Per slot start: (kWh to keep stored at the slot's end, öre per kWh short).
_RESERVE: dict[datetime, tuple[float, float]] = {}
_ORIGINAL_BEST = plan_module._best


def _best_with_reserve(slot, request, soc_kwh, after, grid):  # noqa: ANN001, ANN202 - plan._best's own signature
    if not _RESERVE:
        return _ORIGINAL_BEST(slot, request, soc_kwh, after, grid)
    need, price = _RESERVE.get(slot.start, (0.0, 0.0))
    battery = request.battery
    best = None
    best_total = 0.0
    for action, power in plan_module._candidates(slot, battery, soc_kwh, request):
        outcome = plan_module._apply(slot, battery, soc_kwh, action, power, request.cycle_cost, request.export_hurdle)
        short = max(0.0, battery.min_kwh + need - outcome.soc_end_kwh)
        total = outcome.cost + outcome.wear + outcome.hurdle + grid.value(after, outcome.soc_end_kwh) + short * price
        if best is None or total < best_total - plan_module._TIE:
            best, best_total = outcome, total
    return best, best_total


plan_module._best = _best_with_reserve


# ----------------------------------------------------------------------------- runs

_SHARED: dict = {}


def bt_site() -> bt.Site:
    return _SHARED["site"]


def _init(ctx: Context, site: bt.Site, first: datetime, last: datetime, end_value: float, mode: str, wear: float):
    _SHARED.update(ctx=ctx, site=site, first=first, last=last, end_value=end_value, mode=mode, wear=wear)


def _job(variant: Variant) -> tuple[Variant, tuple]:
    s = _SHARED
    ctx = s["ctx"]
    foresight = "self" if variant.name == "self" else make_forecaster(ctx, variant)
    result = bt.replay(ctx.history, s["site"], s["first"], s["last"], foresight, s["end_value"],
                       MODE_DEFS[s["mode"]], s["wear"], variant.every)
    if variant.band:
        print(f"{variant.name}: {BAND_STATS}", file=sys.stderr)
    return variant, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", type=Path, default=Path("."))
    parser.add_argument("--first", default="2026-08-31")
    parser.add_argument("--last", default="2026-11-13")
    parser.add_argument("--history-first", default="2026-08-31", help="the history loaded: as the backtest's --first")
    parser.add_argument("--history-last", default="2026-11-13")
    parser.add_argument("--mode", default="max_profit")
    parser.add_argument("--wear", type=float, default=15.0)
    parser.add_argument("--runs", nargs="+", required=True, help="variants, e.g. sun=om+corr house=lf_q75_t_id")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    first = datetime.combine(date.fromisoformat(args.first), datetime.min.time(), TZ).replace(hour=13)
    last = datetime.combine(date.fromisoformat(args.last), datetime.min.time(), TZ).replace(hour=13)
    # The history as the battery plan backtest loads it for its default window, so both share one cache.
    h_first = datetime.combine(date.fromisoformat(args.history_first), datetime.min.time(), TZ).replace(hour=13)
    h_last = datetime.combine(date.fromisoformat(args.history_last), datetime.min.time(), TZ).replace(hour=13)
    history_start = (h_first - timedelta(weeks=7)).astimezone(timezone.utc).replace(hour=0, minute=0)
    history_end_dt = (h_last + timedelta(days=2)).astimezone(timezone.utc).replace(hour=0, minute=0)
    history_end = history_end_dt.date()
    history = bt.load_history(history_start, history_end_dt, args.cache)
    history.balance = True
    om = load_open_meteo(args.cache, history_start.date(), history_end)
    solcast = None
    if any("solcast" in run for run in args.runs):
        solcast = load_solcast(args.cache, datetime(2026, 10, 12, tzinfo=timezone.utc), history_end_dt)

    site = bt.Site(
        battery=BatteryParams(usable_kwh=40.3, max_charge_kw=13.2, max_discharge_kw=13.2, min_soc_pct=0.0,
                              max_soc_pct=100.0, round_trip_efficiency=0.94),
        import_cap_kw=13.8, export_cap_kw=13.2, fee_minor=59.6, fee_percent=5.48, vat_percent=25.0,
        sell_credit_minor=6.6, sell_percent=5.0,
    )
    i0, i1 = history.at(first), history.at(last)
    end_value = statistics.median(site.buy(history.spot[i]) for i in range(i0, i1) if history.spot[i] is not None) * 0.94
    pre = datetime(2026, 8, 31, 13, 0, tzinfo=TZ)
    kw_per_ghi = ghi_factor(history, om, min(pre, first))
    running = 0.0
    observed = []
    for value in history.pv:
        observed.append(running)
        running = max(running, value or 0.0)
    ctx = Context(history, om, solcast, kw_per_ghi, house_rows(history, om), {}, observed)
    variants = [Variant("self") if run == "self" else Variant.parse(run) for run in args.runs]
    for source in {v.sun.split("+")[0] for v in variants if "+corr" in v.sun}:
        factors = correction_factors(ctx, source, first.date(), last.date())
        if any(v.band for v in variants):
            # The band learns from the days before the window too; the window's own factors stay as without it.
            early = first.date() - timedelta(days=fb.WINDOW_DAYS + 2)
            factors = {**correction_factors(ctx, source, early, first.date()), **factors}
        ctx.corrections[source] = factors

    results: dict[str, tuple] = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=args.workers, initializer=_init,
        initargs=(ctx, site, first, last, end_value, args.mode, args.wear),
    ) as pool:
        for variant, result in pool.map(_job, variants):
            results[variant.name] = result

    baseline = bt.revenue_by_day(history, site, first, last)
    days = sorted(baseline)
    months = sorted({d[:7] for d in days})
    print(f"{args.mode}, wear {args.wear:g}, {first:%Y-%m-%d %H:%M}..{last:%Y-%m-%d %H:%M}, "
          f"site kW per W/m2 {kw_per_ghi:.4f}, end value {end_value:.1f}")
    print("| Run | Cost kr, end valued | " + " | ".join(f"Revenue {m}" for m in months) + " | Revenue total |")
    print("|---|---|" + "---|" * (len(months) + 1))
    rows = {}
    for name, (tally, start_kwh, end_kwh) in results.items():
        adjusted = (tally.cost - (end_kwh - start_kwh) * end_value) / 100
        revenue = {d: (baseline[d] - tally.daily.get(d, 0.0)) / 100 for d in days}
        per_month = [sum(v for d, v in revenue.items() if d[:7] == m) for m in months]
        rows[name] = {"adjusted": adjusted, "months": dict(zip(months, per_month, strict=True)),
                      "total": sum(per_month), "daily": revenue}
        print(f"| {name} | {adjusted:.0f} | " + " | ".join(f"{v:.0f}" for v in per_month)
              + f" | {sum(per_month):.0f} |")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1))
    if any(math.isnan(r["adjusted"]) for r in rows.values()):
        raise SystemExit("a cost came out NaN")


if __name__ == "__main__":
    main()
