"""The price history's backfill (`pricing/price_history.py`): the profile window from the area's provider, gently.

Nord Pool is the suite's fake `nordpool.get_prices_for_date` (`test_price_provider_nordpool.py`), ENTSO-E the fake
platform (`test_price_provider_entsoe.py`); the clock, the scheduler and the pauses are injected, so nothing waits.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.spotnav.pricing import price_history
from custom_components.spotnav.pricing.day_document import build_day_document
from custom_components.spotnav.pricing.entsoe_parse import market_day_bounds
from custom_components.spotnav.pricing.price_history import (
    BUSY_WAIT_S,
    DEEP_GAP_S,
    HISTORY_GAP_S,
    HistoryBackfill,
)

from . import test_price_provider_entsoe as entsoe_world
from . import test_price_provider_nordpool as nordpool_world
from .entsoe_helpers import area as entsoe_area, entsoe_body
from .relay import StubTransport

UTC = timezone.utc
BRUSSELS = "Europe/Brussels"
NOW = nordpool_world.NOW
TODAY = nordpool_world.TODAY
TOMORROW = nordpool_world.TOMORROW
rows = nordpool_world.rows


class Pauses:
    """The backfill's pauses: recorded, never waited; [hook] runs inside one (to act while the backfill waits)."""

    def __init__(self) -> None:
        self.seconds: list[float] = []
        self.hook = None

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        if self.hook is not None:
            await self.hook(seconds)


def attach(world: Any) -> tuple[HistoryBackfill, Pauses]:
    pauses = Pauses()
    backfill = HistoryBackfill(
        world.hass, world.repository, world.manager, now=world.clock, sleep=pauses, scheduler=world.scheduler
    )
    world.manager.attach_backfill(backfill)
    backfill.arm()
    return backfill, pauses


def serve_every_day(world: Any, code: str, first: date, last: date, zone: str = BRUSSELS) -> None:
    day = first
    while day <= last:
        world.service.serve(code, day, rows(day, zone=zone))
        day += timedelta(days=1)


def days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def backfill_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == price_history.__name__]


@pytest.fixture
async def nordpool(hass: HomeAssistant, transport: StubTransport):
    instance = nordpool_world.World(hass, nordpool_world.Clock(NOW), transport)
    yield instance
    if hasattr(instance, "manager"):
        await instance.stop()


@pytest.fixture
async def entsoe(hass: HomeAssistant, transport: StubTransport):
    instance = entsoe_world.World(hass, transport)
    yield instance
    if hasattr(instance, "manager"):
        await instance.stop()


# ------------------------------------------------------------------ Nord Pool


async def test_a_new_install_on_nord_pool_gets_its_profile_from_the_backfill(
    nordpool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=price_history.__name__)
    await nordpool.start()
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY, zone="Europe/Stockholm")
    backfill, pauses = attach(nordpool)
    assert nordpool.repository.profile_for("SE3") is None

    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    # Today first (the refresh cycle), then the window's 27 earlier days, newest first, each once.
    assert asked[0] == TODAY
    assert asked[1:] == sorted(days(TODAY - timedelta(days=27), TODAY - timedelta(days=1)), reverse=True)
    # One call per day, a pause before each after the first.
    assert pauses.seconds == [HISTORY_GAP_S] * 26 and HISTORY_GAP_S >= 2.0
    profile = nordpool.repository.profile_for("SE3")
    assert profile is not None and profile.from_date == TODAY - timedelta(days=27) and profile.to_date == TODAY
    stored = nordpool.store.payload["days"]
    assert len(stored) == 28 and {entry["provider"] for entry in stored.values()} == {"nordpool"}
    # Nord Pool keeps two months: no year is asked for.
    assert min(asked) == TODAY - timedelta(days=27)
    lines = backfill_lines(caplog)
    assert len(lines) == 2
    assert lines[0].startswith("Filling the price history of SE3 from nordpool: 27 day(s)")
    assert lines[1].startswith("Filled the price history of SE3 (window): 27 day(s) stored") and "usable" in lines[1]


async def test_days_beyond_the_providers_reach_are_never_asked(nordpool) -> None:
    await nordpool.start()
    nordpool.provider.history_reach = timedelta(days=10)
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY, zone="Europe/Stockholm")
    backfill, _ = attach(nordpool)

    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    assert min(asked) == TODAY - timedelta(days=10) and len(asked) == len(set(asked)) == 11
    # Eleven days are no profile, and asking again directly asks nothing either.
    assert nordpool.repository.profile_for("SE3") is None
    result = await nordpool.repository.async_fetch_history("SE3", [TODAY - timedelta(days=20)])
    assert result.stored == () and not result.missing and result.failed is None
    assert len(nordpool.service.calls) == 11


async def test_held_days_are_never_asked_again(nordpool) -> None:
    await nordpool.start()
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY, zone="Europe/Stockholm")
    held = [TODAY - timedelta(days=offset) for offset in (2, 3, 10, 20, 27)]
    for day in held:
        await nordpool.repository.async_get_file("SE3", day)
    nordpool.service.calls.clear()
    backfill, _ = attach(nordpool)

    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    assert not set(asked) & set(held) and len(asked) == 28 - len(held)
    assert nordpool.repository.profile_for("SE3") is not None

    # The next cycle, and the next day's, find nothing missing: no request beyond the new today.
    nordpool.service.calls.clear()
    await nordpool.cycle("SE3")
    await backfill.async_wait()
    assert nordpool.service.calls == []
    nordpool.clock.now += timedelta(days=1)
    await nordpool.cycle("SE3")
    await backfill.async_wait()
    assert nordpool.service.asked() == [("SE3", (TODAY + timedelta(days=1)).isoformat())]


async def test_a_failure_midway_backs_off_and_resumes_where_it_stopped(
    nordpool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=price_history.__name__)
    await nordpool.start()
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY, zone="Europe/Stockholm")
    broken = TODAY - timedelta(days=5)
    nordpool.service.serve(
        "SE3", broken, ServiceValidationError(translation_domain="nordpool", translation_key="connection_error")
    )
    backfill, _ = attach(nordpool)

    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    assert asked == [TODAY, *(TODAY - timedelta(days=offset) for offset in range(1, 6))]
    # The days before the failure are kept in the store; the area waits ten minutes.
    assert len(nordpool.store.payload["days"]) == 5
    [retry] = [timer for timer in nordpool.scheduler.pending if timer.when == NOW + timedelta(minutes=10)]
    assert backfill.summary("SE3")["failures"] == 1
    # A cycle meanwhile does not bring the area back early.
    await nordpool.cycle("SE3")
    await backfill.async_wait()
    assert len(nordpool.service.calls) == 6
    assert any("paused (nordpool_error)" in line for line in backfill_lines(caplog))

    nordpool.service.serve("SE3", broken, rows(broken, zone="Europe/Stockholm"))
    nordpool.clock.now += timedelta(minutes=10)
    retry.fire()
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    assert asked[6:] == sorted(days(TODAY - timedelta(days=27), broken), reverse=True)
    assert asked.count(broken) == 2 and len(set(asked)) == 28
    assert nordpool.repository.profile_for("SE3") is not None
    assert backfill.summary("SE3")["failures"] == 0


async def test_a_day_nord_pool_has_nothing_for_is_not_asked_again_the_same_day(nordpool) -> None:
    await nordpool.start()
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY, zone="Europe/Stockholm")
    empty = TODAY - timedelta(days=3)
    nordpool.service.serve("SE3", empty, [])
    backfill, _ = attach(nordpool)

    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()
    await nordpool.cycle("SE3")
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    assert asked.count(empty) == 1 and len(asked) == 28
    assert nordpool.repository.profile_for("SE3") is not None


async def test_today_and_tomorrow_come_before_the_backfill(nordpool) -> None:
    await nordpool.start()
    # Inside the publication window, tomorrow not out yet.
    nordpool.clock.now = datetime(2026, 10, 30, 11, 50, tzinfo=UTC)
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY, zone="Europe/Stockholm")
    backfill, pauses = attach(nordpool)

    async def publish(seconds: float) -> None:
        if seconds == BUSY_WAIT_S and nordpool.service.answers.get(("SE3", TOMORROW.isoformat())) is None:
            # While the backfill waits, tomorrow is published and the manager's next cycle fetches it.
            nordpool.service.serve("SE3", TOMORROW, rows(TOMORROW, zone="Europe/Stockholm"))
            nordpool.clock.now += timedelta(minutes=5)
            await nordpool.manager.async_refresh_area("SE3")

    pauses.hook = publish

    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    # Today, tomorrow not yet out, tomorrow out: only then the past, which waited once.
    assert asked[:3] == [TODAY, TOMORROW, TOMORROW]
    assert all(day < TODAY for day in asked[3:]) and len(asked[3:]) == 27
    assert pauses.seconds[0] == BUSY_WAIT_S and pauses.seconds.count(BUSY_WAIT_S) == 1


async def test_nothing_is_filled_for_an_area_whose_today_is_missing(nordpool) -> None:
    await nordpool.start()
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY - timedelta(days=1), zone="Europe/Stockholm")
    nordpool.service.serve(
        "SE3", TODAY, ServiceValidationError(translation_domain="nordpool", translation_key="connection_error")
    )
    backfill, _ = attach(nordpool)

    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    assert nordpool.service.asked() == [("SE3", TODAY.isoformat())]


async def test_an_eet_area_moving_to_nord_pool_gets_its_cet_window(nordpool) -> None:
    await nordpool.start(loaded=False)
    helsinki = "Europe/Helsinki"
    # Four weeks of the relay's Helsinki days are held: the profile is built from them.
    relay_days = {}
    for day in days(TODAY - timedelta(days=27), TODAY):
        start = datetime(day.year, day.month, day.day, tzinfo=nordpool_world.ZoneInfo(helsinki))
        count = int(((start + timedelta(days=1)).astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 900)
        document = build_day_document(
            area_id="FI", market_day=day, tz=helsinki, market_tz=helsinki, start=start, res=15,
            prices_eur_kwh=[0.05 + (index % 96) / 10000 for index in range(count)], fx=None, fx_date=None,
            fx_src=None, src="relay", published=None, retrieved=None,
        )
        relay_days[f"FI|{helsinki}|{day.isoformat()}"] = {
            "document": document, "fetched_at": "2026-10-01T00:00:00+00:00", "provider": "relay",
        }
    nordpool.repository._adopt({"schema": 2, "days": relay_days})  # noqa: SLF001 - the store's own path
    assert nordpool.repository.area_entry("FI").market_tz == helsinki
    assert nordpool.repository.profile_for("FI") is not None
    serve_every_day(nordpool, "FI", TODAY - timedelta(days=60), TODAY)
    backfill, _ = attach(nordpool)
    await nordpool.manager.async_subscribe(owner_id="charger", area_id="FI", tz=helsinki)
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()
    nordpool.service.calls.clear()

    nordpool.entry.mock_state(nordpool.hass, ConfigEntryState.LOADED)
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    asked = [date.fromisoformat(day) for _, day in nordpool.service.asked()]
    # Today's two CET files, then the window on the CET calendar: 28 days and the one before (a Helsinki day of
    # the window starts in the CET day before it), each once.
    assert asked[:2] == [TODAY - timedelta(days=1), TODAY]
    assert sorted(asked) == days(TODAY - timedelta(days=28), TODAY)
    profile = nordpool.repository.profile_for("FI")
    assert profile is not None and nordpool.repository.area_entry("FI").market_tz == BRUSSELS
    cet = [key for key in nordpool.store.payload["days"] if key.startswith(f"FI|{BRUSSELS}|")]
    assert len(cet) == 29
    # The relay's Helsinki files stay held under their own calendar.
    assert sum(key.startswith(f"FI|{helsinki}|") for key in nordpool.store.payload["days"]) == 28


async def test_a_change_of_provider_mid_way_stops_the_old_run(nordpool) -> None:
    await nordpool.start()
    serve_every_day(nordpool, "SE3", TODAY - timedelta(days=60), TODAY, zone="Europe/Stockholm")
    backfill, pauses = attach(nordpool)

    async def unload(_seconds: float) -> None:
        if len(nordpool.service.calls) == 4:
            nordpool.entry.mock_state(nordpool.hass, ConfigEntryState.NOT_LOADED)

    pauses.hook = unload
    await nordpool.manager.async_subscribe(owner_id="charger", area_id="SE3")
    await nordpool.hass.async_block_till_done()
    await backfill.async_wait()

    # Three past days from Nord Pool, then nothing more from it once it is gone.
    assert len(nordpool.service.calls) == 4


# ------------------------------------------------------------------ ENTSO-E


def platform_answer(url: str) -> tuple[int, str, dict[str, str]]:
    """The platform's answer for whatever period was asked: quarter hours from its start to its end."""
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    start = datetime.strptime(query["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    end = datetime.strptime(query["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    slots = int((end - start).total_seconds() // 900)
    body = entsoe_body(
        query["in_Domain"],
        start.strftime("%Y-%m-%dT%H:%MZ"),
        end.strftime("%Y-%m-%dT%H:%MZ"),
        "PT15M",
        [f"{40 + (i % 96) * 0.25:.2f}" for i in range(slots)],
    )
    return 200, body, {}


async def test_entsoe_fills_the_window_in_one_request_and_a_year_slowly(
    entsoe, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=price_history.__name__)
    await entsoe.start()
    entsoe.platform.fallback = platform_answer
    today = entsoe_world.TODAY
    backfill, pauses = attach(entsoe)

    await entsoe.manager.async_subscribe(owner_id="charger", area_id="SE4")
    await entsoe.hass.async_block_till_done()
    await backfill.async_wait()

    def period(day: date) -> str:
        return market_day_bounds(day, entsoe_area("SE4").market_tz)[0].strftime("%Y%m%d%H%M")

    starts = [query["periodStart"] for query in entsoe.platform.requests]
    ends = [query["periodEnd"] for query in entsoe.platform.requests]
    # Today; the window's 27 days in one request; then the year back to 365 days, a month per request.
    assert starts[0] == period(today)
    assert (starts[1], ends[1]) == (period(today - timedelta(days=27)), period(today))
    year = list(zip(starts[2:], ends[2:], strict=True))
    assert len(year) == 11 and year[0] == (period(today - timedelta(days=58)), period(today - timedelta(days=27)))
    assert year[-1][0] == period(today - timedelta(days=365))
    assert pauses.seconds == [DEEP_GAP_S] * 11
    held = [key for key in entsoe.cache.payload["days"] if key.startswith("SE4|")]
    assert len(held) == 366
    assert entsoe.repository.profile_for("SE4") is not None
    lines = backfill_lines(caplog)
    assert len(lines) == 4 and "27 day(s) of the window" in lines[0] and "338 day(s) of the year" in lines[2]

    # A restart's worth of cycles later: everything is held, nothing is asked.
    count = len(entsoe.platform.requests)
    await entsoe.cycle("SE4")
    await backfill.async_wait()
    assert len(entsoe.platform.requests) == count


async def test_an_entsoe_rate_limit_in_the_year_waits_at_least_as_long_as_asked(entsoe) -> None:
    await entsoe.start()
    today = entsoe_world.TODAY
    limited = market_day_bounds(today - timedelta(days=58), entsoe_area("SE4").market_tz)[0]

    def answer(url: str):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if query["periodStart"] == limited.strftime("%Y%m%d%H%M"):
            return 429, "", {"Retry-After": "900"}
        return platform_answer(url)

    entsoe.platform.fallback = answer
    backfill, _ = attach(entsoe)

    await entsoe.manager.async_subscribe(owner_id="charger", area_id="SE4")
    await entsoe.hass.async_block_till_done()
    await backfill.async_wait()

    # Today, the window, the first month of the year (refused): then it waits the platform's fifteen minutes, not
    # its own ten.
    assert len(entsoe.platform.requests) == 3
    assert any(timer.when == entsoe_world.NOW + timedelta(minutes=15) for timer in entsoe.scheduler.pending)
    assert entsoe.repository.profile_for("SE4") is not None
