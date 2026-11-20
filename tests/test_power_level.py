"""The power level (Göteborg Energi's tidsindelad 6/10/14 … kW in the low-price hours): stored on the price entry
beside the demand-charge parts, read tolerantly, and its exceedances counted over a rolling year from the site's
hours and the ones it keeps of older months.

Stockholm's clock; the high-price window is weekdays 07-20, November to March, and the level holds outside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.auto_settings import (
    AutoSettingsError,
    DemandComponent,
    GridTariff,
    PowerLevel,
)
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.planning.planner import HighLoadWindow
from custom_components.spotnav.pricing.price_source import fees_from_data, source_revision
from custom_components.spotnav.site.demand_tracker import SiteDemand
from custom_components.spotnav.site.power_history import PowerArchive

TZ = "Europe/Stockholm"
STOCKHOLM = ZoneInfo(TZ)
UTC = timezone.utc
GBG_WINDOW = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20)
#: 10 kW in the hours the high-price window does not count.
LEVEL = PowerLevel(level_kw=10.0, window=GBG_WINDOW, outside=True)
LEVEL_DICT = {
    "level_kw": 10.0,
    "window": {"days": "weekdays", "from_month": 11, "to_month": 3, "from_hour": 7, "to_hour": 20},
    "outside": True,
}


def local(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=STOCKHOLM).astimezone(UTC)


def hour(start: datetime, kw: float, **fields: object) -> HourMean:
    return HourMean(start=start, kw=kw, floor_kw=kw, **fields)


# ------------------------------------------------------------------ the part


def test_the_level_holds_outside_its_window_on_holidays_too_and_in_all_hours_without_one() -> None:
    # Monday 16 November 2026, 08:00: inside the high-price window, so not the level's hour.
    assert not LEVEL.counts(local(2026, 11, 16, 8), TZ)
    assert LEVEL.counts(local(2026, 11, 16, 21), TZ)
    assert LEVEL.counts(local(2026, 11, 21, 12), TZ)  # a Saturday
    assert LEVEL.counts(local(2026, 7, 6, 12), TZ)  # summer: no window
    # Christmas Eve is a holiday the window leaves out: the level holds then.
    christmas = PowerLevel(10.0, HighLoadWindow(**{**LEVEL_DICT["window"], "holidays": frozenset({local(2026, 12, 24, 12).date()})}), True)
    assert christmas.counts(local(2026, 12, 24, 12), TZ)
    assert PowerLevel(10.0).counts(local(2026, 11, 16, 8), TZ)
    assert PowerLevel(10.0, GBG_WINDOW).counts(local(2026, 11, 16, 8), TZ)


def test_a_level_is_read_exactly_and_refused_when_it_is_not_one() -> None:
    assert PowerLevel.from_dict(LEVEL_DICT) == LEVEL
    assert LEVEL.as_dict() == LEVEL_DICT
    for bad in (
        {**LEVEL_DICT, "level_kw": 0},
        {**LEVEL_DICT, "level_kw": -1},
        {**LEVEL_DICT, "level_kw": True},
        {**LEVEL_DICT, "level_kw": 5000},
        {**LEVEL_DICT, "outside": "yes"},
        {**LEVEL_DICT, "window": None},  # outside of no window
        {**LEVEL_DICT, "extra": 1},
        {"level_kw": 10.0},
    ):
        with pytest.raises(AutoSettingsError):
            PowerLevel.from_dict(bad)


def test_the_level_is_a_key_of_its_own_so_the_parts_read_as_before_and_a_bad_one_drops_only_itself() -> None:
    part = DemandComponent(top_n=3, window=GBG_WINDOW, price_winter=135.0, price_summer=0.0)
    tariff = GridTariff(demand=(part,), power_level=LEVEL)
    stored = tariff.as_dict()
    # A release before it reads the components exactly: the level is not among them.
    assert set(stored["demand_components"][0]) == {"window", "top_n", "price_per_kw"}
    assert stored["power_level"] == LEVEL_DICT
    assert GridTariff.from_data(stored) == tariff
    assert GridTariff.from_data({**stored, "power_level": {"level_kw": "ten"}}) == GridTariff(demand=(part,))
    assert GridTariff.from_data({**stored, "power_level": None}).power_level is None


def test_an_entry_without_a_level_keeps_its_revision() -> None:
    before = fees_from_data("SE3", {"high_load_window": LEVEL_DICT["window"]})
    revision = source_revision(before)
    data = {"high_load_window": LEVEL_DICT["window"], "power_level": None}
    assert source_revision(fees_from_data("SE3", data)) == revision
    assert source_revision(fees_from_data("SE3", {**data, "power_level": LEVEL_DICT})) != revision


# ------------------------------------------------------------------ the count


def demand_at(now: datetime) -> SiteDemand:
    return SiteDemand(None, None, TZ, now=lambda: now)


def test_the_count_takes_complete_hours_over_the_level_in_its_hours_only() -> None:
    now = local(2026, 11, 20, 12)
    demand = demand_at(now)
    tracker = demand.tracker
    tracker.month, tracker.previous_month = (2026, 11), (2026, 10)
    tracker.hours = [
        hour(local(2026, 11, 16, 8), 14.0),  # inside the window: not the level's hour
        hour(local(2026, 11, 16, 22), 11.0),  # over
        hour(local(2026, 11, 17, 22), 10.0),  # at the level, not over
        hour(local(2026, 11, 18, 22), 12.0, incomplete=True),  # a gap: never counts
        hour(local(2026, 11, 19, 22), 13.0, imported=True),  # read from statistics, not shown complete
    ]
    tracker.previous = [hour(local(2026, 10, 3, 12), 10.5)]  # October: no window, the level holds
    count = demand.level_count(LEVEL)
    assert count.exceeded == (local(2026, 10, 3, 12), local(2026, 11, 16, 22))
    assert (count.used, count.allowed, count.next_number, count.next_is_last) == (2, 3, 3, True)


def test_older_months_keep_their_exceedances_against_the_level_they_were_measured_at_for_a_year() -> None:
    now = local(2026, 11, 20, 12)
    demand = demand_at(now)
    tracker = demand.tracker
    tracker.month, tracker.previous_month = (2026, 11), (2026, 10)
    tracker.previous = [hour(local(2026, 10, 3, 12), 10.5)]
    tracker.hours = [hour(local(2026, 11, 16, 22), 11.0)]
    demand._file_days(now, (), LEVEL)
    assert set(demand.archive.exceeded) == {local(2026, 10, 3, 12), local(2026, 11, 16, 22)}
    # An older month the tracker no longer holds keeps its own, even after the level is raised.
    demand.archive.exceeded[local(2026, 3, 1, 12)] = (8.0, 6.0)
    demand.archive.exceeded[local(2025, 11, 1, 12)] = (8.0, 6.0)  # over a year ago
    raised = PowerLevel(10.6, GBG_WINDOW, True)
    demand._file_days(now, (), raised)
    assert set(demand.archive.exceeded) == {local(2026, 3, 1, 12), local(2026, 11, 16, 22), local(2025, 11, 1, 12)}
    count = demand.level_count(raised)
    assert count.exceeded == (local(2026, 3, 1, 12), local(2026, 11, 16, 22))
    # Without a level the kept ones stay as they are.
    demand._file_days(now, (), None)
    assert local(2026, 11, 16, 22) in demand.archive.exceeded


def test_the_kept_exceedances_are_stored_read_back_tolerantly_and_pruned_after_thirteen_months() -> None:
    archive = PowerArchive()
    archive.exceeded = {local(2026, 3, 1, 12): (8.0, 6.0), local(2025, 9, 1, 12): (7.0, 6.0)}
    stored = archive.as_dict()
    assert stored["level_exceeded"][0] == [local(2025, 9, 1, 12).isoformat(), 7.0, 6.0]
    again = PowerArchive()
    again.restore({**stored, "level_exceeded": [*stored["level_exceeded"], ["not a time", 1, 2], [1, 2], "x"]})
    assert again.exceeded == archive.exceeded
    assert again.prune((2026, 11)) is True
    assert list(again.exceeded) == [local(2026, 3, 1, 12)]
    # An archive without any writes no key: a store from before reads the same.
    assert "level_exceeded" not in PowerArchive().as_dict()


def test_the_rolling_year_starts_a_year_before_now() -> None:
    now = local(2026, 11, 20, 12)
    demand = demand_at(now)
    demand.tracker.month = (2026, 11)
    demand.archive.exceeded = {
        local(2025, 11, 20, 13): (11.0, 10.0),  # inside the year
        local(2025, 11, 20, 11): (11.0, 10.0),  # just before it
    }
    assert demand.level_count(LEVEL).exceeded == (local(2025, 11, 20, 13),)
    assert demand.level_count(LEVEL, now + timedelta(hours=2)).used == 0


# ------------------------------------------------------------------ the dashboard and the writes


async def test_the_level_is_written_shown_with_its_count_and_kept_by_a_writer_that_does_not_know_it(
    hass, hass_client_no_auth, freezer, offline_relay
) -> None:
    from custom_components.spotnav.api.grid_tariff import GridTariffRefusal, update_grid_tariff
    from custom_components.spotnav.runtime import site_controller_for

    from .test_demand_site import demand_of, pilot_site, run
    from .test_grid_tariff_api import OWNER

    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    app = await hass_client_no_auth()
    await run(hass, freezer, 60, 4000.0)
    price = hass.config_entries.async_get_entry("price_a")
    level = {"level_kw": 3.0, "window": None, "outside": False}

    update_grid_tariff(hass, "entry_a", 0, {**OWNER, "power_level": level})
    await hass.async_block_till_done()
    assert price.data["power_level"] == level
    # The hour before (4 kW for the whole hour) was over 3 kW: one of three.
    demand = demand_of(hass)
    assert demand["power_level"] == level
    count = demand["level_count"]
    assert (count["used"], count["allowed"], count["counts_now"]) == (1, 3, True)
    assert count["next_counted_start"] == "2026-09-22T07:00:00+00:00"
    controller = site_controller_for(hass, site.entry_id)
    assert controller.demand.level_count(PowerLevel(3.0)).used == 1

    # A body without it (an older card or app) keeps it; `null` removes it.
    update_grid_tariff(hass, "entry_a", 1, {**OWNER, "transfer_percent": {"enabled": False, "value": None}})
    await hass.async_block_till_done()
    assert price.data["power_level"] == level
    with pytest.raises(GridTariffRefusal) as refused:
        update_grid_tariff(hass, "entry_a", 2, {**OWNER, "power_level": {**level, "level_kw": -1}})
    assert (refused.value.code, refused.value.field) == ("invalid_fiscal", "power_level")

    # An app that does not read it never meets the keys; one that does reads them.
    async def ask(*reads: str):
        response = await app.post(
            "/api/webhook/webhook-entry_a",
            json={"version": 1, "action": "dashboard", "api_version": 1, "reads": ["grid_tariff", *reads]},
        )
        return (await response.json())["grid_tariff"]["demand"]

    plain = await ask()
    assert "power_level" not in plain and "level_count" not in plain
    reading = await ask("power_level")
    assert reading["power_level"] == level and reading["level_count"]["used"] == 1

    update_grid_tariff(hass, "entry_a", 2, {**OWNER, "power_level": None})
    await hass.async_block_till_done()
    assert price.data["power_level"] is None
    assert demand_of(hass)["level_count"] is None
