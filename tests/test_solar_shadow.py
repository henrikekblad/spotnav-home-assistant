"""The own solar forecast in shadow on a site (`site/solar_shadow.py`): the warm start, the fetch schedule and its
back-off, the archive, the issues of every candidate (the provider's through the solar correction), the nightly
review in the diagnostics, an unset position, and a restart."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site import open_meteo as om
from custom_components.spotnav.site import solar_shadow as shell
from custom_components.spotnav.site.open_meteo import FetchResult
from custom_components.spotnav.site.solar_own import CANDIDATES, OWN_CANDIDATES
from custom_components.spotnav.site.solar_shadow_store import ISSUE_DAY_AHEAD, ISSUE_MORNING, planes_as_rows
from custom_components.spotnav.site.sun_geometry import array_kw, hour_sun

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")

LAT, LON = 55.7123, 13.1876
POS = (55.7, 13.2)
PLANES = [(30.0, 180.0, 5.0)]
KT = 0.8


def _ghi(start: datetime) -> float:
    return hour_sun(start, *POS).mean_clear * KT


def _made(start: datetime) -> float:
    """What the array makes in the hour: the planes on the forecast irradiance, less with low sun to the south."""
    hs = hour_sun(start, *POS)
    made = array_kw(hs, PLANES, _ghi(start), _ghi(start) * 0.6, 12.0)
    return made * (0.4 if hs.elevation < 15 and 120 <= hs.azimuth < 240 else 1.0)


def _om_body(first: datetime, hours: int, suffixes: tuple[str, ...]) -> dict[str, Any]:
    """An Open-Meteo answer from `first` for `hours` stamps: radiation stamped at the end of its hour."""
    stamps = [first + timedelta(hours=h) for h in range(hours)]
    hourly: dict[str, Any] = {"time": [s.strftime("%Y-%m-%dT%H:%M") for s in stamps]}
    for suffix in suffixes:
        ghi = [_ghi(s - timedelta(hours=1)) for s in stamps]
        hourly[f"shortwave_radiation{suffix}"] = ghi
        hourly[f"direct_radiation{suffix}"] = [g * 0.6 for g in ghi]
        hourly[f"cloud_cover{suffix}"] = [20.0] * hours
        hourly[f"temperature_2m{suffix}"] = [12.0] * hours
    return {"hourly": hourly}


class World:
    """The Energy dashboard's solar statistic, the recorder's hours of it, and Open-Meteo (or its failure)."""

    def __init__(
        self, shadow: Any, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False, fail_archive: bool = False
    ) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.fail = fail
        self.fail_archive = fail_archive
        self.clock: datetime | None = None

        async def energy_ids() -> tuple[str, ...]:
            return ("sensor.pv",)

        async def statistics(ids: Any, start: datetime, end: datetime) -> dict[str, list[dict[str, Any]]]:
            rows = []
            moment = start
            while moment < end:
                rows.append({"start": moment.timestamp(), "change": _made(moment)})
                moment += timedelta(hours=1)
            return {"sensor.pv": rows}

        async def get(url: str, params: dict[str, str]) -> FetchResult:
            self.requests.append((url, dict(params)))
            if self.fail:
                return FetchResult(error=om.ERROR_TIMEOUT)
            if url == om.ARCHIVE_URL and self.fail_archive:
                return FetchResult(error=om.ERROR_HTTP, status=500)
            if url == om.ARCHIVE_URL:
                first = datetime.fromisoformat(params["start_date"]).replace(tzinfo=UTC)
                last = datetime.fromisoformat(params["end_date"]).replace(tzinfo=UTC)
                hours = int((last - first).total_seconds() // 3600) + 24
                return FetchResult(body=_om_body(first, hours, ("_previous_day1", "_previous_day2")), status=200)
            assert self.clock is not None
            first = self.clock.replace(hour=0, minute=0, second=0, microsecond=0)
            return FetchResult(body=_om_body(first, 72, ("",)), status=200)

        monkeypatch.setattr(shadow, "_async_energy_ids", energy_ids)
        monkeypatch.setattr(shadow, "_async_statistics", statistics)
        monkeypatch.setattr(shadow, "_async_get", get)

    async def hours(self, shadow: Any, first: datetime, count: int, freezer: Any = None) -> datetime:
        moment = first
        for _ in range(count):
            self.clock = moment
            if freezer is not None:
                freezer.move_to(moment)
            await shadow.async_refresh(moment)
            moment += timedelta(hours=1)
        return moment

    def forecast_calls(self) -> list[dict[str, str]]:
        return [params for url, params in self.requests if url == om.FORECAST_URL]

    def archive_calls(self) -> list[dict[str, str]]:
        return [params for url, params in self.requests if url == om.ARCHIVE_URL]


async def _site(hass: HomeAssistant, freezer: Any, **site: Any) -> Any:
    hass.config.latitude, hass.config.longitude = LAT, LON
    await hass.config.async_set_time_zone("Europe/Stockholm")
    entry = await pilot_site(hass, freezer, **site)
    controller = site_controller_for(hass, entry.entry_id)
    await controller.solar_shadow.async_shutdown()  # driven by hand below, not by its timer
    controller.solar_shadow._closed = False
    return entry, controller


async def test_three_days_warm_start_fetch_issue_and_review_every_own_candidate(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, controller = await _site(hass, freezer)
    shadow = controller.solar_shadow
    world = World(shadow, monkeypatch)

    start = datetime(2026, 5, 31, 22, 5, tzinfo=UTC)  # 00:05 local, 1 June
    await world.hours(shadow, start, 3 * 24, freezer)

    # A year of production was read at the start, and the planes fitted on it.
    assert shadow.status == shell.STATUS_SHADOW
    assert len(shadow.history.pv) > 365 * 24
    assert shadow.planes is not None
    # Four forecast fetches a day, the first at the start; nothing but the rounded position is sent.
    calls = world.forecast_calls()
    assert len(calls) == 3 * 4 and calls == world.forecast_calls()
    assert all((params["latitude"], params["longitude"]) == ("55.7", "13.2") for _url, params in world.requests)
    # The archive: a year back, one chunk per refresh, then done.
    archive = world.archive_calls()
    assert 4 <= len(archive) <= 5 and shadow.archive_done
    assert len({key // 24 for key in shadow.history.weather[1]["g"]}) > 300
    # Each day's issues, the own group, with what they were made from.
    assert shadow.log.issued(ISSUE_MORNING, date(2026, 6, 2), shell.GROUP_OWN)
    assert shadow.log.issued(ISSUE_DAY_AHEAD, date(2026, 6, 3), shell.GROUP_OWN)
    meta = shadow.log.issues[(ISSUE_DAY_AHEAD, "2026-06-03")][shell.GROUP_OWN]
    assert meta["inputs"]["open_meteo"] > 10 and meta["inputs"]["weather_entity"] == 0
    # The reviewed days in the diagnostics: every own candidate, and the sun-position factor beats the plain ratio
    # on an array shaded at low sun.
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    block = diagnostics["solar_shadow"]
    assert block["status"] == shell.STATUS_SHADOW
    assert block["candidates"] == list(CANDIDATES)
    summary = block["review_30d"]
    assert set(OWN_CANDIDATES) <= set(summary)
    ahead = {name: summary[name][ISSUE_DAY_AHEAD] for name in OWN_CANDIDATES}
    assert ahead["own_sunpos"]["hourly_mae_kwh"] < ahead["own"]["hourly_mae_kwh"]
    assert block["review_recent"]
    assert any("own_sunpos" in hour for hour in block["next_hours"])
    assert block["factors"]["sun_position_own"]["strongest"]
    assert block["fetch"]["last_error"] is None and block["fetch"]["source"].startswith("open-meteo.com")
    assert "55.7" not in str(block)  # the position is not shown


async def test_the_provider_candidates_are_issued_from_the_corrections_read(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entry, controller = await _site(hass, freezer, **{CONF_SOLAR_FORECAST_ENTRIES: ["forecast_solar_1"]})
    shadow = controller.solar_shadow
    world = World(shadow, monkeypatch)
    at = datetime(2026, 6, 1, 11, 5, tzinfo=UTC)  # 13:05 local
    await world.hours(shadow, at, 1)

    tomorrow = [datetime(2026, 6, 1, 22, tzinfo=UTC) + timedelta(hours=h) for h in range(24)]
    raw = {start: _made(start) * 1.5 for start in tomorrow}
    shadow.note_provider(at, raw, {start: kwh * 0.8 for start, kwh in raw.items()})

    assert shadow.log.issued(ISSUE_DAY_AHEAD, date(2026, 6, 2), shell.GROUP_PROVIDER)
    logged = {name for name, issue, day in shadow.log.forecasts if day == "2026-06-02"}
    assert {"provider", "provider_corrected", "provider_sunpos"} <= logged
    # Kept for the provider's own factor: tomorrow's hours as run 1 or 2 by their lead.
    assert shadow.history.provider[1] or shadow.history.provider[2]
    # A second read the same afternoon issues nothing more.
    before = dict(shadow.log.forecasts)
    shadow.note_provider(at + timedelta(hours=1), {start: 0.0 for start in tomorrow}, {})
    assert shadow.log.forecasts == before


async def test_a_failing_fetch_backs_off_and_the_issue_falls_back_to_cloud_cover(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entry, controller = await _site(hass, freezer)
    shadow = controller.solar_shadow
    world = World(shadow, monkeypatch, fail=True)
    tomorrow = [datetime(2026, 6, 1, 22, tzinfo=UTC) + timedelta(hours=h) for h in range(24)]

    async def clouds() -> dict[datetime, float]:
        return {start: 40.0 for start in tomorrow}

    monkeypatch.setattr(shadow, "_async_clouds", clouds)
    await world.hours(shadow, datetime(2026, 6, 1, 6, 5, tzinfo=UTC), 8)  # 08:05 to 15:05 local

    # Failures back off (15 min, 30, 60, 120...): fewer requests than refreshes, and no archive while failing.
    assert 3 <= len(world.requests) < 8
    assert not world.archive_calls()
    assert shadow.backoff.failures == len(world.requests)
    assert shadow.snapshot()["fetch"]["last_error"] == om.ERROR_TIMEOUT
    # The day-ahead issue is made from the weather entity's cloud cover, marked, with only the cloud-only candidate.
    meta = shadow.log.issues[(ISSUE_DAY_AHEAD, "2026-06-02")][shell.GROUP_OWN]
    assert meta["inputs"]["open_meteo"] == 0 and meta["inputs"]["weather_entity"] > 10
    logged = {name for name, issue, day in shadow.log.forecasts if day == "2026-06-02"}
    assert logged == {"cloud_only"}


async def test_a_failing_archive_backs_off_on_its_own_and_skips_a_chunk_that_keeps_failing(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entry, controller = await _site(hass, freezer)
    shadow = controller.solar_shadow
    world = World(shadow, monkeypatch, fail_archive=True)
    await world.hours(shadow, datetime(2026, 5, 31, 22, 5, tzinfo=UTC), 3 * 24, freezer)

    # The forecast is fetched four times a day all along: the archive's failures never hold it back.
    assert len(world.forecast_calls()) == 3 * 4
    assert shadow.backoff.failures == 0 and shadow.snapshot()["fetch"]["backoff"]["failures"] == 0
    # The archive backs off on its own; the first chunk was skipped after its tries, and later chunks go on.
    archive = world.archive_calls()
    assert len(archive) >= shell.ARCHIVE_CHUNK_TRIES
    assert archive[0]["end_date"] == archive[shell.ARCHIVE_CHUNK_TRIES - 1]["end_date"]
    assert shadow.archive_from is not None
    assert shadow.archive_from.isoformat() <= archive[0]["start_date"]
    assert len(archive) < 3 * 24 - 12
    snapshot = shadow.snapshot()["archive"]
    assert snapshot["backoff"]["failures"] >= 1 and snapshot["chunk_failures"] < shell.ARCHIVE_CHUNK_TRIES
    # Once the archive answers again it goes on where it was (the skipped chunks are not asked again) and ends.
    world.fail_archive = False
    shadow.archive_backoff.succeeded()
    await world.hours(shadow, datetime(2026, 6, 3, 22, 5, tzinfo=UTC), 24, freezer)
    later = world.archive_calls()[len(archive) :]
    assert later and all(call["end_date"] <= archive[-1]["end_date"] for call in later)
    assert shadow.archive_done


@pytest.mark.parametrize(("lat", "lon"), [(0.0, 0.0), (float("nan"), 13.0)])
async def test_without_a_plausible_home_position_nothing_is_fetched(
    hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch, lat: float, lon: float
) -> None:
    _entry, controller = await _site(hass, freezer)
    shadow = controller.solar_shadow
    world = World(shadow, monkeypatch)
    hass.config.latitude, hass.config.longitude = lat, lon
    await world.hours(shadow, datetime(2026, 6, 1, 11, 5, tzinfo=UTC), 3)

    assert shadow.status == shell.STATUS_NO_POSITION
    assert world.requests == []
    assert shadow.snapshot()["status"] == shell.STATUS_NO_POSITION
    # The provider's reads are not kept either.
    shadow.note_provider(datetime(2026, 6, 1, 13, tzinfo=UTC), {datetime(2026, 6, 2, 10, tzinfo=UTC): 1.0}, {})
    assert not shadow.history.provider[1] and not shadow.log.forecasts


async def test_a_site_without_solar_statistics_stays_idle(hass: HomeAssistant, freezer) -> None:
    _entry, controller = await _site(hass, freezer)
    shadow = controller.solar_shadow
    await shadow.async_refresh(datetime(2026, 6, 1, 11, 5, tzinfo=UTC))
    assert shadow.status == shell.STATUS_NO_PRODUCTION
    assert shadow.fetched_at is None


async def test_what_was_kept_survives_a_restart(hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch) -> None:
    entry, controller = await _site(hass, freezer)
    shadow = controller.solar_shadow
    world = World(shadow, monkeypatch)
    await world.hours(shadow, datetime(2026, 6, 1, 3, 5, tzinfo=UTC), 30)
    kept = shadow._stored()
    assert kept["history"]["pv"] and kept["log"]["forecasts"] and kept["planes"]
    shadow._store.changed()
    await shadow._store.async_close()

    monkeypatch.undo()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = site_controller_for(hass, entry.entry_id).solar_shadow
    assert again is not shadow
    assert again.history.pv == shadow.history.pv
    assert again.log.forecasts == shadow.log.forecasts
    assert planes_as_rows(again.planes) == planes_as_rows(shadow.planes)
    assert again.archive_upto == shadow.archive_upto


async def test_the_snapshot_states_the_latitude_in_whole_degrees_only_and_never_the_longitude(
    hass: HomeAssistant, freezer
) -> None:
    _entry, controller = await _site(hass, freezer)
    shadow = controller.solar_shadow
    hass.config.latitude, hass.config.longitude = 55.6789, 13.0123
    snapshot = shadow.snapshot()
    assert snapshot["latitude"] == 56
    assert "longitude" not in snapshot and "13.0" not in str(snapshot)
    hass.config.latitude = None  # type: ignore[assignment]
    assert shadow.snapshot()["latitude"] is None
