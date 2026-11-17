"""The own solar forecast in shadow, pure parts: the sun's position, the Open-Meteo fetch (position, parsing, transport,
back-off), the plane fit, the candidates and the sun-position factor, what is kept and the review."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import json
import math
from typing import Any

import aiohttp
import pytest

from custom_components.spotnav.site import open_meteo as om
from custom_components.spotnav.site import solar_own as so
from custom_components.spotnav.site.open_meteo import Backoff, FetchResult, Weather
from custom_components.spotnav.site.solar_planes import envelope, fit_planes
from custom_components.spotnav.site.solar_shadow import due_issues, fetch_due
from custom_components.spotnav.site.solar_shadow_store import (
    ISSUE_DAY_AHEAD,
    ISSUE_MORNING,
    IssueLog,
    SolarHistory,
    decode,
    encode,
    planes_from_rows,
)
from custom_components.spotnav.site.sun_geometry import array_kw, hour_key, hour_sun, key_start, sun

POS = (55.7, 13.2)
TZ = "Europe/Stockholm"


# ---------------------------------------------------------------------------------------------- the sun


def test_the_sun_is_due_south_at_solar_noon_and_as_high_as_the_season_says() -> None:
    # 21 June at 55.7 N: the noon sun stands 90 - 55.7 + 23.44 = 57.7 degrees high.
    best = max(
        (sun(datetime(2026, 6, 21, 10, 0, tzinfo=UTC) + timedelta(minutes=m), *POS), m) for m in range(0, 120)
    )
    (cz, az), _minute = best
    assert math.degrees(math.asin(cz)) == pytest.approx(57.7, abs=0.3)
    assert az == pytest.approx(180, abs=1.5)
    # Morning: east of south; evening: west; night: below the horizon and no clear sky.
    assert sun(datetime(2026, 6, 21, 4, tzinfo=UTC), *POS)[1] < 90
    assert sun(datetime(2026, 6, 21, 17, tzinfo=UTC), *POS)[1] > 270
    assert not hour_sun(datetime(2026, 12, 21, 22, tzinfo=UTC), *POS).daylight


def test_an_array_makes_more_of_a_clear_sky_than_an_overcast_one_and_less_when_hot() -> None:
    hs = hour_sun(datetime(2026, 6, 21, 10, tzinfo=UTC), *POS)
    planes = [(30.0, 180.0, 5.0)]
    clear = array_kw(hs, planes)
    assert 3.0 < clear < 5.5
    overcast = array_kw(hs, planes, ghi=150.0, beam=0.0, temp_c=15.0)
    assert overcast < clear / 3
    cool = array_kw(hs, planes, ghi=700.0, beam=500.0, temp_c=0.0)
    hot = array_kw(hs, planes, ghi=700.0, beam=500.0, temp_c=35.0)
    assert hot < cool
    assert array_kw(hs, planes, ghi=0.0, beam=0.0) == 0.0


# ---------------------------------------------------------------------------------------------- the position


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        (55.7234, 13.1876, (55.7, 13.2)),
        (-33.8688, 151.2093, (-33.9, 151.2)),
        (0.04, 0.03, None),  # 0/0: Home Assistant's value before onboarding
        (0.0, 0.0, None),
        (None, 13.0, None),
        (55.7, float("nan"), None),
        (True, 13.0, None),
        ("55.7", 13.0, None),
        (91.0, 13.0, None),
        (55.7, -181.0, None),
    ],
)
def test_the_home_position_is_rounded_to_a_tenth_or_refused(lat: Any, lon: Any, expected: Any) -> None:
    assert om.rounded_position(lat, lon) == expected


def test_only_the_rounded_position_is_sent() -> None:
    position = om.rounded_position(55.723456, 13.187654)
    assert position is not None
    params = om.forecast_params(position)
    assert (params["latitude"], params["longitude"]) == ("55.7", "13.2")
    assert params["forecast_days"] == "3" and params["timezone"] == "UTC"
    assert "23456" not in json.dumps(params) and "87654" not in json.dumps(params)
    archive = om.archive_params(position, date(2026, 1, 1), date(2026, 3, 31))
    assert (archive["latitude"], archive["start_date"], archive["end_date"]) == ("55.7", "2026-01-01", "2026-03-31")
    assert "shortwave_radiation_previous_day1" in archive["hourly"]
    assert "temperature_2m_previous_day2" in archive["hourly"]


# ---------------------------------------------------------------------------------------------- parsing


def _body(suffix: str = "") -> dict[str, Any]:
    return {
        "hourly": {
            "time": ["2026-06-01T09:00", "2026-06-01T10:00", "2026-06-01T11:00", "2026-06-01T12:00"],
            f"shortwave_radiation{suffix}": [100.0, 400.0, 500.0, None],
            f"direct_radiation{suffix}": [50.0, 300.0, 900.0, 0.0],
            f"cloud_cover{suffix}": [20.0, 40.0, None, 80.0],
            f"temperature_2m{suffix}": [10.0, 12.0, 14.0, 16.0],
        }
    }


def test_radiation_is_taken_from_the_end_of_its_hour_and_cloud_from_both_ends() -> None:
    parsed = om.parse_hourly(_body())
    nine, ten, eleven = (datetime(2026, 6, 1, h, tzinfo=UTC) for h in (9, 10, 11))
    # The hour from 09:00 takes the radiation stamped 10:00 (Open-Meteo's mean of the preceding hour).
    assert parsed[nine] == Weather(ghi=400.0, beam=300.0, cloud=30.0, temp=11.0)
    # A beam above the GHI is cut to it; one missing cloud end leaves the other.
    assert parsed[ten] == Weather(ghi=500.0, beam=500.0, cloud=40.0, temp=13.0)
    # Without a GHI the hour is left out, and so is the last stamp (no end).
    assert eleven not in parsed and len(parsed) == 2


def test_an_archive_answer_gives_both_runs_and_junk_gives_nothing() -> None:
    body = {"hourly": {**_body("_previous_day1")["hourly"], **_body("_previous_day2")["hourly"]}}
    runs = om.parse_archive(body)
    assert set(runs) == {1, 2} and len(runs[1]) == len(runs[2]) == 2
    assert om.parse_hourly(None) == {}
    assert om.parse_hourly({"hourly": {"time": "x"}}) == {}
    assert om.parse_hourly({"hourly": {"time": ["bad", "2026-06-01T10:00"], "shortwave_radiation": [1, 2]}}) == {}


# ---------------------------------------------------------------------------------------------- the transport


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for index in range(0, len(self._body), size):
            yield self._body[index : index + size]


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status, self.headers, self.content = status, headers or {}, _Content(body)

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Session:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.redirects: list[bool] = []

    def get(self, url: str, params: dict[str, str], headers: dict[str, str], allow_redirects: bool = True) -> Any:
        self.requests.append((url, params, headers))
        self.redirects.append(allow_redirects)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


async def test_a_good_answer_is_parsed_with_the_user_agent_sent() -> None:
    session = _Session(_Response(200, json.dumps(_body()).encode()))
    result = await om.async_fetch_json(session, om.FORECAST_URL, {"latitude": "55.7"}, "SpotNav test")  # type: ignore[arg-type]
    assert result.error is None and result.body == _body()
    assert session.requests[0][2] == {"User-Agent": "SpotNav test"}
    # A redirect is never followed: it is an HTTP error.
    assert session.redirects == [False]
    moved = await om.async_fetch_json(_Session(_Response(302, b"")), om.FORECAST_URL, {}, "ua")  # type: ignore[arg-type]
    assert (moved.error, moved.status) == (om.ERROR_HTTP, 302)


@pytest.mark.parametrize(
    ("answer", "error", "retry_after"),
    [
        (_Response(429, b"", {"Retry-After": "120"}), om.ERROR_RATE_LIMITED, 120.0),
        (_Response(503, b"down"), om.ERROR_HTTP, None),
        (_Response(200, b"{not json"), om.ERROR_BODY, None),
        (_Response(200, b"x" * (om.MAX_BODY_BYTES + 10)), om.ERROR_TOO_LARGE, None),
        (aiohttp.ClientConnectionError(), om.ERROR_NETWORK, None),
        (TimeoutError(), om.ERROR_TIMEOUT, None),
    ],
)
async def test_every_failure_is_a_code_never_an_exception(answer: Any, error: str, retry_after: Any) -> None:
    result = await om.async_fetch_json(_Session(answer), om.FORECAST_URL, {}, "ua")  # type: ignore[arg-type]
    assert (result.body, result.error, result.retry_after_s) == (None, error, retry_after)


async def test_a_hanging_server_meets_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Slow(_Response):
        async def __aenter__(self) -> _Response:
            await asyncio.sleep(10)
            return self

    monkeypatch.setattr(om, "REQUEST_TIMEOUT_S", 0.01)
    result = await om.async_fetch_json(_Session(Slow(200, b"{}")), om.FORECAST_URL, {}, "ua")  # type: ignore[arg-type]
    assert result.error == om.ERROR_TIMEOUT


def test_the_back_off_doubles_to_its_cap_honours_the_server_and_resets() -> None:
    backoff = Backoff()
    assert backoff.ready(0.0)
    waits = [backoff.failed(0.0) for _ in range(7)]
    assert waits[:4] == [900.0, 1800.0, 3600.0, 7200.0]
    assert waits[-1] == om.BACKOFF_MAX_S
    assert not backoff.ready(om.BACKOFF_MAX_S - 1) and backoff.ready(om.BACKOFF_MAX_S)
    backoff.succeeded()
    assert backoff.ready(0.0) and backoff.failures == 0
    assert backoff.failed(0.0, FetchResult(error=om.ERROR_RATE_LIMITED)) == om.BACKOFF_RATE_LIMITED_S
    assert backoff.failed(0.0, FetchResult(error=om.ERROR_HTTP, retry_after_s=20000.0)) == 20000.0
    # Thousands of failures in a row: still the cap, never an overflow.
    backoff.failures = 5000
    assert backoff.failed(0.0) == om.BACKOFF_MAX_S


# ---------------------------------------------------------------------------------------------- the schedule


def test_the_forecast_is_fetched_once_per_fetch_hour() -> None:
    at = datetime(2026, 6, 1, 11, 5, tzinfo=UTC)  # 13:05 local
    assert fetch_due(None, at, TZ)
    assert not fetch_due(at, at + timedelta(minutes=50), TZ)
    # 14:05 and 17:05 local: still the 13:00 fetch's turn; 18:05: a new one.
    assert not fetch_due(at, at + timedelta(hours=4), TZ)
    assert fetch_due(at, at + timedelta(hours=5), TZ)


def test_the_issues_are_due_at_six_before_noon_and_from_one_oclock() -> None:
    assert due_issues(datetime(2026, 6, 1, 3, 5, tzinfo=UTC), TZ) == []  # 05:05 local
    morning = due_issues(datetime(2026, 6, 1, 4, 5, tzinfo=UTC), TZ)  # 06:05 local
    assert [(issue, day) for issue, day, _ in morning] == [(ISSUE_MORNING, date(2026, 6, 1))]
    assert morning[0][2][0] == datetime(2026, 6, 1, 5, tzinfo=UTC) and len(morning[0][2]) == 17
    assert due_issues(datetime(2026, 6, 1, 10, 5, tzinfo=UTC), TZ) == []  # 12:05: a late morning issue is skipped
    ahead = due_issues(datetime(2026, 6, 1, 11, 5, tzinfo=UTC), TZ)  # 13:05
    assert [(issue, day, len(hours)) for issue, day, hours in ahead] == [(ISSUE_DAY_AHEAD, date(2026, 6, 2), 24)]


# ---------------------------------------------------------------------------------------------- the planes


def _clear_pv(planes: list[tuple[float, float, float]], first: int, end: int) -> dict[int, float]:
    return {key: array_kw(hour_sun(key_start(key), *POS, 4), planes) for key in range(first, end)}


def test_one_steep_south_plane_is_found_from_its_clear_sky_history() -> None:
    end = hour_key(datetime(2026, 5, 1, tzinfo=UTC))
    first = end - 40 * 24
    rows = envelope(_clear_pv([(40.0, 180.0, 6.0)], first, end), first, end, *POS)
    planes = fit_planes(rows, *POS, two=False)
    assert planes is not None and len(planes) == 1
    tilt, az, kwp = planes[0]
    assert (tilt, az) == (40.0, 180.0)
    assert kwp == pytest.approx(6.0, rel=0.05)


def test_too_little_history_fits_nothing() -> None:
    end = hour_key(datetime(2026, 5, 1, tzinfo=UTC))
    rows = envelope(_clear_pv([(40.0, 180.0, 6.0)], end - 5 * 24, end), end - 5 * 24, end, *POS)
    assert fit_planes(rows, *POS, two=True) is None


# ---------------------------------------------------------------------------------------------- the candidates


PLANES = [(30.0, 180.0, 5.0)]
UPTO = date(2026, 5, 31)


def _history(*, low_sun_south_loss: float = 1.0, days: int = 120, provider_scale: float | None = None) -> SolarHistory:
    """`days` of forecasts and production: the production is what the planes make of the forecast, cut to
    `low_sun_south_loss` with the sun under 15 degrees between south-east and south-west (an obstruction)."""
    history = SolarHistory()
    end = hour_key(datetime(2026, 6, 1, tzinfo=UTC)) - 2  # local midnight
    pv: dict[datetime, float] = {}
    for key in range(end - days * 24, end):
        start = key_start(key)
        hs = hour_sun(start, *POS)
        if not hs.daylight:
            pv[start] = 0.0
            continue
        kt = 0.35 + 0.6 * ((key * 7919) % 100) / 100
        ghi = hs.mean_clear * kt
        weather = Weather(ghi=ghi, beam=ghi * kt * 0.8, cloud=(1 - kt) * 100, temp=12.0)
        made = array_kw(hs, PLANES, weather.ghi, weather.beam, weather.temp)
        if hs.elevation < 15 and 120 <= hs.azimuth < 240:
            made *= low_sun_south_loss
        pv[start] = made
        for run in (1, 2):
            history._put_weather(run, key, weather)
        if provider_scale is not None:
            history.provider[1][key] = round(made * provider_scale * 100)
    history.merge_pv(pv)
    return history


def test_the_sun_position_factor_learns_a_low_sun_obstruction_and_the_ratio_cannot() -> None:
    factors = so.learn(_history(low_sun_south_loss=0.3, days=200), PLANES, POS, TZ, UPTO)
    assert factors.hours > 1000
    # Low winter sun to the south (an hour at about 8 degrees, azimuth ~150) against high summer sun.
    low = hour_sun(datetime(2026, 2, 1, 9, tzinfo=UTC), *POS)
    assert low.elevation < 15 and 120 <= low.azimuth < 240
    low_cell = so.pos_cell(1, 20.0, low)
    high_cell = so.pos_cell(1, 20.0, hour_sun(datetime(2026, 5, 20, 11, tzinfo=UTC), *POS))
    assert factors.pos_own.get(low_cell) < 0.6
    assert factors.pos_own.get(high_cell) == pytest.approx(1.0, abs=0.08)
    # The shown factors name the obstruction.
    shown = factors.as_dict()["sun_position_own"]["strongest"]
    assert shown and "elev" in shown[0]["cell"] and shown[0]["factor"] < 0.6


def test_shrinkage_pulls_a_thin_cell_toward_the_window_ratio() -> None:
    learner = so.Learner(window=30)
    learner.add(UPTO, ("thin",), 1.0, 0.0)  # one kWh of reference, nothing measured
    learner.add(UPTO, ("thick",), 100.0, 50.0)
    learner.add(UPTO, ("skipped",), 0.01, 5.0)  # under the reference floor
    learner.add(UPTO - timedelta(days=31), ("old",), 10.0, 0.0)  # outside the window
    factors = learner.factors(UPTO)
    assert factors.overall == pytest.approx(50.0 / 101.0)
    assert factors.get(("thin",)) == pytest.approx((0.0 + 3 * factors.overall) / (1.0 + 3))
    assert ("skipped",) not in factors.cells and ("old",) not in factors.cells
    assert factors.get(("unknown",)) == factors.overall


def _weather_for(day: date) -> dict[datetime, Weather]:
    out = {}
    for hour in range(24):
        start = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
        hs = hour_sun(start, *POS)
        if hs.daylight:
            out[start] = Weather(ghi=hs.mean_clear * 0.8, beam=hs.mean_clear * 0.5, cloud=20.0, temp=15.0)
    return out


def test_both_variants_and_the_cloud_only_baseline_forecast_close_to_the_truth() -> None:
    factors = so.learn(_history(), PLANES, POS, TZ, UPTO)
    weather = _weather_for(date(2026, 6, 2))
    issued = datetime(2026, 6, 1, 11, 5, tzinfo=UTC)
    forecast, used = so.own_forecast(factors, PLANES, POS, issued, list(weather), weather)
    assert set(forecast) == set(so.OWN_CANDIDATES)
    assert used == {"open_meteo": len(weather), "weather_entity": 0}
    truth = {start: array_kw(hour_sun(start, *POS), PLANES, w.ghi, w.beam, w.temp) for start, w in weather.items()}
    day = sum(truth.values())
    for name in (so.CANDIDATE_OWN, so.CANDIDATE_OWN_SUNPOS, so.CANDIDATE_GHI, so.CANDIDATE_GHI_SUNPOS):
        assert sum(forecast[name].values()) == pytest.approx(day, rel=0.2), name
    # GHI x factor is the GHI times the learned kWh per W/m2 of its weather run.
    noon = datetime(2026, 6, 2, 11, tzinfo=UTC)
    run = so.run_for(noon, issued)
    assert forecast[so.CANDIDATE_GHI][noon] == pytest.approx(factors.kpg[run] * weather[noon].ghi)
    assert run == 2 and so.run_for(datetime(2026, 6, 2, 5, tzinfo=UTC), issued) == 1


def test_without_fetched_irradiance_only_the_cloud_only_fallback_is_made() -> None:
    factors = so.learn(_history(), PLANES, POS, TZ, UPTO)
    hours = list(_weather_for(date(2026, 6, 2)))
    clouds = {start: 30.0 for start in hours[:5]}
    forecast, used = so.own_forecast(factors, PLANES, POS, datetime(2026, 6, 1, 11, tzinfo=UTC), hours, {}, clouds)
    assert set(forecast) == {so.CANDIDATE_CLOUD_ONLY}
    assert len(forecast[so.CANDIDATE_CLOUD_ONLY]) == 5
    assert used == {"open_meteo": 0, "weather_entity": 5}
    assert so.own_forecast(factors, PLANES, POS, datetime(2026, 6, 1, 11, tzinfo=UTC), hours, {}, None)[0] == {}


def test_the_provider_factor_learns_a_provider_that_runs_high() -> None:
    factors = so.learn(_history(provider_scale=1.5), PLANES, POS, TZ, UPTO)
    assert factors.pos_provider.overall == pytest.approx(1 / 1.5, rel=0.05)
    noon = datetime(2026, 6, 2, 11, tzinfo=UTC)
    corrected = so.provider_sunpos(factors, POS, datetime(2026, 6, 1, 11, tzinfo=UTC), {noon: 3.0}, {noon: 10.0})
    assert corrected[noon] == pytest.approx(2.0, rel=0.08)
    # Night hours are not forecast.
    assert so.provider_sunpos(factors, POS, noon, {datetime(2026, 6, 2, 23, tzinfo=UTC): 1.0}, {}) == {}


# ---------------------------------------------------------------------------------------------- the store


def test_a_fetch_fills_each_hours_run_by_its_lead_and_the_first_wins() -> None:
    history = SolarHistory()
    hour = datetime(2026, 6, 3, 10, tzinfo=UTC)
    sunny = {hour: Weather(500.0, 300.0, 20.0, 15.0)}
    assert history.note_weather(sunny, hour - timedelta(hours=50))  # lead 50 h: run 2
    assert history.weather_at(2, hour_key(hour)) == Weather(500.0, 300.0, 20.0, 15.0)
    assert history.weather_at(1, hour_key(hour)) is None
    assert not history.note_weather({hour: Weather(100.0, 0.0, 90.0, 10.0)}, hour - timedelta(hours=40))
    assert history.note_weather({hour: Weather(200.0, 50.0, 70.0, 11.0)}, hour - timedelta(hours=28))  # run 1
    assert not history.note_weather({hour: Weather(900.0, 800.0, 0.0, 20.0)}, hour - timedelta(hours=3))
    assert history.weather_at(1, hour_key(hour)).ghi == 200.0  # type: ignore[union-attr]
    # The archive fills only what is missing; a dark hour is not kept.
    day = hour.date()
    assert not history.note_archive(1, {hour: Weather(1.0, 0.0, 0.0, 0.0)}, day, day)
    assert not history.note_archive(1, {hour + timedelta(hours=12): Weather(0.0, 0.0, 0.0, 0.0)}, day, day)
    # An hour outside the dates asked for is not kept.
    later = hour + timedelta(days=2)
    assert not history.note_archive(1, {later: Weather(400.0, 200.0, 10.0, 12.0)}, day, day)
    assert history.weather_at(1, hour_key(later)) is None
    assert history.note_archive(1, {later: Weather(400.0, 200.0, 10.0, 12.0)}, day, later.date())
    assert history.note_provider({hour: 2.341}, hour - timedelta(hours=10))
    assert history.provider[1][hour_key(hour)] == 234


def test_the_inputs_survive_a_restore_compactly_and_are_pruned_after_a_year() -> None:
    history = _history(days=30, provider_scale=1.2)
    stored = history.as_dict()
    assert all(isinstance(value, str) for name, value in stored.items() if name != "first")
    again = SolarHistory()
    again.restore(json.loads(json.dumps(stored)))
    assert again.pv == history.pv and again.weather == history.weather and again.provider == history.provider
    # A malformed value is dropped, the rest kept.
    assert decode("5,,x,7,99999999", 10, 0, 1000) == {10: 5, 13: 7}
    assert encode({10: 5, 13: 7}, 10) == "5,,,7"
    pruned = SolarHistory()
    pruned.restore(stored)
    assert pruned.prune(datetime(2027, 8, 1, tzinfo=UTC))
    assert not pruned.pv and not pruned.weather[1]["g"]
    junk = SolarHistory()
    junk.restore({"first": "nonsense", "pv": "1,2"})
    assert junk.pv == {}
    assert planes_from_rows([[30, 180, 5.0]]) == ((30.0, 180.0, 5.0),)
    assert planes_from_rows([[30, 400, 5.0]]) is None and planes_from_rows("x") is None


def _logged_day(log: IssueLog, history: SolarHistory, day: date, forecast_kwh: float, measured_kwh: float) -> None:
    hours = {datetime(day.year, day.month, day.day, h, tzinfo=UTC): forecast_kwh for h in range(6, 16)}
    log.record(ISSUE_DAY_AHEAD, day, "own", {"own": hours}, {"made_at": "x"})
    history.merge_pv({start: measured_kwh for start in hours})


def test_the_review_scores_each_issued_day_once_it_is_measured() -> None:
    history, log = SolarHistory(), IssueLog(tz=TZ)
    day = date(2026, 6, 1)
    _logged_day(log, history, day, 2.0, 1.5)
    # A second issue for the same day changes nothing: the first wins.
    assert log.record(ISSUE_DAY_AHEAD, day, "own", {"own": {datetime(2026, 6, 1, 8, tzinfo=UTC): 9.0}}, {}) == 0
    # The day's last hour (23:00 local) is not measured yet: wait, at most two days.
    assert log.review(history, datetime(2026, 6, 2, 1, tzinfo=UTC)) == []
    made = log.review(history, datetime(2026, 6, 3, 1, tzinfo=UTC))
    assert len(made) == 1
    key, score = made[0]
    assert key == ("own", ISSUE_DAY_AHEAD, "2026-06-01")
    assert (score.hours, score.mae, score.error_kwh) == (10, pytest.approx(0.5), pytest.approx(5.0))
    assert log.review(history, datetime(2026, 6, 4, 1, tzinfo=UTC)) == []
    # Measured through the day's end: reviewed the next night.
    day2 = date(2026, 6, 4)
    _logged_day(log, history, day2, 1.0, 1.0)
    history.merge_pv({datetime(2026, 6, 4, 21, tzinfo=UTC): 0.0})
    assert len(log.review(history, datetime(2026, 6, 5, 0, 30, tzinfo=UTC))) == 1


def test_the_summary_combines_thirty_days_and_old_days_go() -> None:
    history, log = SolarHistory(), IssueLog(tz=TZ)
    for offset in range(40):
        _logged_day(log, history, date(2026, 5, 1) + timedelta(days=offset), 2.0, 1.0 if offset % 2 else 3.0)
    now = datetime(2026, 6, 12, 12, tzinfo=UTC)
    log.review(history, now)
    summary = log.summary(now)["own"][ISSUE_DAY_AHEAD]
    assert summary["days"] == 28  # 13 May to 9 June: the thirty days before 12 June that were logged
    assert summary["hourly_mae_kwh"] == pytest.approx(1.0)
    assert summary["daily_mae_kwh"] == pytest.approx(10.0)
    assert summary["daily_bias_kwh"] == pytest.approx(0.0)
    assert log.recent(["own"])[-1]["own"]["mae"] == pytest.approx(1.0)
    again = IssueLog(tz=TZ)
    again.restore(json.loads(json.dumps(log.as_dict())))
    assert again.forecasts == log.forecasts and again.reviews == log.reviews and again.issues == log.issues
    log.prune(datetime(2026, 8, 1, tzinfo=UTC))
    assert min(day for _c, _i, day in log.forecasts) >= "2026-06-02"
