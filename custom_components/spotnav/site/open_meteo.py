"""Open-Meteo's hourly irradiance forecast for the own solar forecast in shadow (SpotNav 3.0, `site/solar_shadow.py`).

Open-Meteo (open-meteo.com, data CC BY 4.0, free without a key for non-commercial use) is fetched from the user's own
installation, at Home Assistant's home position rounded to 0.1° (`rounded_position`; the owner's decision of
2026-11-17). Nothing else about the site is sent. An unset or implausible position (not a number, outside the
globe, or 0°/0°, Home Assistant's value before onboarding) sends nothing.

* **Forecast** (`forecast_params`): the newest run, GHI (`shortwave_radiation`), beam (`direct_radiation`), total
  cloud cover and air temperature, three UTC days (today and the two after), hourly.
* **Archive** (`archive_params`): the previous-runs API's `_previous_day1` / `_previous_day2` runs of the same
  variables for past dates, once, so the learned factors start warm where the Energy dashboard has a year of solar
  production (the experiment's data source).
* **Alignment** (`parse_hourly`): Open-Meteo stamps radiation at the end of its hour (the mean of the preceding
  hour): the hour starting at t takes the value stamped t + 1 h (the experiment's lag test: correlation 0.944 against
  0.893). Cloud cover and temperature are instantaneous at the stamp: the hour takes the mean of its two ends.
* **Transport** (`async_fetch_json`): Home Assistant's shared aiohttp session, SpotNav's User-Agent, one overall
  timeout, no redirects followed (a redirect is an HTTP error) and a bounded body; the outcome is a body or a stable error code, never an exception.
* **Back-off** (`Backoff`): after a failure nothing is fetched for `BACKOFF_FIRST_S`, doubling with each failure in
  a row up to `BACKOFF_MAX_S` (at least a `Retry-After` the server gave, and at least an hour after a 429); one
  success resets it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
from typing import Any, Final

import aiohttp

FORECAST_URL: Final = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL: Final = "https://previous-runs-api.open-meteo.com/v1/forecast"
VARIABLES: Final = ("shortwave_radiation", "direct_radiation", "cloud_cover", "temperature_2m")
#: The archived runs kept, by their suffix (the run made one and two days before the hour).
ARCHIVE_RUNS: Final = {1: "_previous_day1", 2: "_previous_day2"}
FORECAST_DAYS: Final = 3
#: Days per archive request (about 120 kB of JSON each).
ARCHIVE_CHUNK_DAYS: Final = 92
REQUEST_TIMEOUT_S: Final = 30.0
MAX_BODY_BYTES: Final = 2_000_000
BACKOFF_FIRST_S: Final = 900.0
BACKOFF_MAX_S: Final = 6 * 3600.0
BACKOFF_RATE_LIMITED_S: Final = 3600.0

ERROR_TIMEOUT: Final = "timeout"
ERROR_NETWORK: Final = "network"
ERROR_TOO_LARGE: Final = "too_large"
ERROR_RATE_LIMITED: Final = "rate_limited"
ERROR_HTTP: Final = "http"
ERROR_BODY: Final = "bad_body"


def rounded_position(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    """Home Assistant's home position rounded to 0.1°, `None` when it is unset or implausible (see the module)."""
    numbers = [latitude, longitude]
    if not all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) for v in numbers):
        return None
    lat, lon = float(latitude), float(longitude)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    lat, lon = round(lat, 1) + 0.0, round(lon, 1) + 0.0
    if lat == 0.0 and lon == 0.0:
        return None
    return lat, lon


def _coordinate(value: float) -> str:
    return f"{value:.1f}"


def forecast_params(position: tuple[float, float]) -> dict[str, str]:
    """The forecast request's query (the rounded position only)."""
    return {
        "latitude": _coordinate(position[0]),
        "longitude": _coordinate(position[1]),
        "hourly": ",".join(VARIABLES),
        "forecast_days": str(FORECAST_DAYS),
        "timezone": "UTC",
    }


def archive_params(position: tuple[float, float], first: date, last: date) -> dict[str, str]:
    """The archive request's query for the dates `first`..`last` (UTC), both archived runs."""
    return {
        "latitude": _coordinate(position[0]),
        "longitude": _coordinate(position[1]),
        "hourly": ",".join(f"{v}{suffix}" for suffix in ARCHIVE_RUNS.values() for v in VARIABLES),
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "timezone": "UTC",
    }


@dataclass(frozen=True, slots=True)
class Weather:
    """One hour's forecast: GHI and beam (W/m2, the hour's mean), cloud cover (%) and air temperature (°C)."""

    ghi: float
    beam: float
    cloud: float | None
    temp: float | None


def _value(series: Any, index: int) -> float | None:
    if not isinstance(series, list) or not 0 <= index < len(series):
        return None
    value = series[index]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _mean(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return (a + b) / 2


def _stamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_hourly(body: Any, suffix: str = "") -> dict[datetime, Weather]:
    """An Open-Meteo answer's hourly block as `{UTC hour start: Weather}` for the variables with `suffix` (see the
    module for the alignment). Hours without a GHI are left out; a negative radiation counts as 0."""
    hourly = body.get("hourly") if isinstance(body, Mapping) else None
    if not isinstance(hourly, Mapping) or not isinstance(hourly.get("time"), list):
        return {}
    times = hourly["time"]
    ghi, beam = hourly.get(f"shortwave_radiation{suffix}"), hourly.get(f"direct_radiation{suffix}")
    cloud, temp = hourly.get(f"cloud_cover{suffix}"), hourly.get(f"temperature_2m{suffix}")
    out: dict[datetime, Weather] = {}
    for index in range(len(times) - 1):
        start, end = _stamp(times[index]), _stamp(times[index + 1])
        if start is None or end is None or end - start != timedelta(hours=1) or start.minute:
            continue
        g = _value(ghi, index + 1)
        if g is None:
            continue
        b = _value(beam, index + 1)
        c = _mean(_value(cloud, index), _value(cloud, index + 1))
        out[start] = Weather(
            ghi=max(0.0, g),
            beam=min(max(0.0, g), max(0.0, b or 0.0)),
            cloud=None if c is None else min(100.0, max(0.0, c)),
            temp=_mean(_value(temp, index), _value(temp, index + 1)),
        )
    return out


def parse_archive(body: Any) -> dict[int, dict[datetime, Weather]]:
    """An archive answer as `{run: {UTC hour start: Weather}}` for the runs of `ARCHIVE_RUNS`."""
    return {run: parse_hourly(body, suffix) for run, suffix in ARCHIVE_RUNS.items()}


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A fetched JSON body, or why there is none (`error`, and the server's `Retry-After` seconds when it gave one)."""

    body: Any = None
    error: str | None = None
    status: int | None = None
    retry_after_s: float | None = None


def _retry_after(raw: str | None) -> float | None:
    try:
        seconds = float(raw) if raw is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds > 0 else None


async def async_fetch_json(
    session: aiohttp.ClientSession, url: str, params: Mapping[str, str], user_agent: str
) -> FetchResult:
    """One GET of `url`, its JSON body or a stable error code; never raises (but for cancellation)."""
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            async with session.get(
                url, params=dict(params), headers={"User-Agent": user_agent}, allow_redirects=False
            ) as response:
                if response.status == 429:
                    return FetchResult(
                        error=ERROR_RATE_LIMITED,
                        status=429,
                        retry_after_s=_retry_after(response.headers.get("Retry-After")),
                    )
                if response.status != 200:
                    return FetchResult(
                        error=ERROR_HTTP,
                        status=response.status,
                        retry_after_s=_retry_after(response.headers.get("Retry-After")),
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        return FetchResult(error=ERROR_TOO_LARGE, status=200)
                    chunks.append(chunk)
    except TimeoutError:
        return FetchResult(error=ERROR_TIMEOUT)
    except aiohttp.ClientError:
        return FetchResult(error=ERROR_NETWORK)
    try:
        return FetchResult(body=json.loads(b"".join(chunks)), status=200)
    except ValueError:
        return FetchResult(error=ERROR_BODY, status=200)


@dataclass(slots=True)
class Backoff:
    """When the next request may go (see the module), on a clock of seconds."""

    failures: int = 0
    until: float | None = None

    def ready(self, now: float) -> bool:
        return self.until is None or now >= self.until

    def failed(self, now: float, result: FetchResult | None = None) -> float:
        """A failure: the wait before the next request (s)."""
        self.failures += 1
        # The exponent is clamped: the wait reaches `BACKOFF_MAX_S` long before, and an unbounded power overflows.
        wait = min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2.0 ** min(self.failures - 1, 32))
        if result is not None:
            if result.error == ERROR_RATE_LIMITED:
                wait = max(wait, BACKOFF_RATE_LIMITED_S)
            if result.retry_after_s is not None:
                wait = max(wait, min(result.retry_after_s, 24 * 3600.0))
        self.until = now + wait
        return wait

    def succeeded(self) -> None:
        self.failures = 0
        self.until = None

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "wait_s": None if self.until is None or self.until <= now else round(self.until - now),
        }
