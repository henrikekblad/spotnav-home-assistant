"""The day's records (`day_record.py`), the `export_day` service that writes them (`day_export.py`) and their part of
the debug bundle.

A real site with the Sigenergy plant (`tests/test_battery_runtime.py`) and the SE4 electricity price on the offline
relay (2026-09-22 and the next day published), in Stockholm. The day's forecast log, quarter hours, review and house
hours are filled in directly, as the site would have kept them; the clock is then moved to the next morning.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
import yaml
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from custom_components.spotnav import day_export
from custom_components.spotnav.battery.plan_model import ACTION_GRID_CHARGE, ACTION_SELF_CONSUME, MODE_BALANCED
from custom_components.spotnav.battery.plan_shadow import BUCKET, Bucket, battery_params, review_day
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.day_export import archive_path, encode, write_atomic
from custom_components.spotnav.day_record import (
    MISSING_NO_LOAD,
    MISSING_NO_PRICES,
    MISSING_NOT_KEPT,
    QUARTER_COLUMNS,
    SCHEMA,
    day_bounds,
)
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.services import EXPORT_DAY_SCHEMA

from tests.test_battery_runtime import _site
from tests.test_price_device import add_price

pytestmark = pytest.mark.usefixtures("offline_relay")

DAY = date(2026, 9, 22)
#: 08:00 in Stockholm on the day, when the price entry is set up.
SET_UP = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
#: The next morning, when the day is exported.
NEXT_MORNING = datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)
PACKAGE = Path(__file__).parents[1] / "custom_components" / "spotnav"


async def _world(hass: HomeAssistant, freezer: Any, tmp_path: Path) -> tuple[Any, Any]:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    hass.config.config_dir = str(tmp_path)
    freezer.move_to(SET_UP)
    await add_price(hass)
    site, controller = await _site(hass)
    _fill_day(controller)
    freezer.move_to(NEXT_MORNING)
    return site, controller


def _fill_day(controller: Any) -> None:
    """The day as the site would have kept it: forecasts and outcomes, 96 quarter hours, the review, house hours."""
    tz = controller.demand.tracker.tz
    start, end = day_bounds(DAY, tz)
    log = controller.house_forecast.log
    hours = [start + timedelta(hours=i) for i in range(24)]
    made_at = start - timedelta(hours=11)
    log.record_forecast("house_kw", dict.fromkeys(hours, 1.5), made_at)
    log.record_forecast("sun_kwh", dict.fromkeys(hours, 0.4), made_at)
    for hour in hours:
        log.record_actual("house_kw", hour, 1.7)
        log.record_actual("sun_kwh", hour, 0.3)
    log.review(NEXT_MORNING)

    plan = controller.battery_shadow.plan
    for index in range(96):
        moment = start + BUCKET * index
        charging = index < 16
        plan.actuals.buckets[moment] = Bucket(
            start=moment,
            buy=60.0 if charging else 200.0,
            sell=30.0,
            cap_kw=13.0,
            import_kwh=1.5 if charging else 0.4,
            export_kwh=0.0,
            car_kwh=0.0,
            battery_kwh=1.0 if charging else -0.2,
            covered_s=900.0,
            soc_start=40.0 + index * 0.1,
            soc_end=40.1 + index * 0.1,
            first_action=ACTION_GRID_CHARGE if charging else ACTION_SELF_CONSUME,
            first_soc=41.0,
            ran_action=ACTION_GRID_CHARGE if charging else ACTION_SELF_CONSUME,
            ran_kw=4.0 if charging else 0.0,
            ran_soc=41.0,
            ran_modes={MODE_BALANCED: (ACTION_GRID_CHARGE if charging else ACTION_SELF_CONSUME, 4.0 if charging else 0.0)},
        )
    params, _reason, _estimates = battery_params(controller.battery_shadow.facts)
    assert params is not None
    review = review_day(DAY, plan.actuals.buckets.values(), params, tz, NEXT_MORNING, (MODE_BALANCED,), MODE_BALANCED)
    assert review is not None
    plan.reviews.append(review)

    controller.demand.tracker.hours.extend(
        HourMean(start=hour, kw=2.5, house_kw=1.7, outdoor_c=8.0, ex_battery_kw=0.9) for hour in hours
    )


async def _export(hass: HomeAssistant, data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    return await hass.services.async_call(DOMAIN, "export_day", data or {}, blocking=True, **kwargs)


# ------------------------------------------------------------------ the file


async def test_yesterday_is_written_with_every_part_and_answered(hass: HomeAssistant, freezer, tmp_path: Path) -> None:
    site, _controller = await _world(hass, freezer, tmp_path)

    response = await _export(hass, return_response=True)

    path = tmp_path / "spotnav_archive" / site.entry_id / "2026-09-22.json"
    assert response["path"] == str(path) and response["date"] == "2026-09-22" and response["site_id"] == site.entry_id
    assert response["bytes"] == path.stat().st_size
    assert response["complete"] is True
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == SCHEMA and record["kind"] == "spotnav_day"
    assert record["tz"] == "Europe/Stockholm"
    assert record["start"] == "2026-09-21T22:00:00+00:00" and record["end"] == "2026-09-22T22:00:00+00:00"
    assert set(record["units"]) >= {"price", "power", "energy", "soc", "time"}
    assert response["counts"] == record["counts"] == {
        "forecast_rows": 48,
        "forecast_kinds": 2,
        "price_intervals": 96,
        "quarters": 96,
        "quarters_in_day": 96,
        "house_hours": 24,
        "battery_review": True,
    }
    # Only the switched load is missing: the site has none.
    assert record["missing"] == response["missing"] == {"switch_load": MISSING_NO_LOAD}

    forecasts = record["forecasts"]
    assert forecasts["columns"] == ["hour", "value", "made_at", "outcome"]
    assert forecasts["kinds"]["house_kw"][0] == ["2026-09-21T22:00:00+00:00", 1.5, "2026-09-21T11:00:00+00:00", 1.7]
    assert forecasts["reviews"]["house_kw"]["all"]["mae"] == 0.2

    prices = record["prices"]
    assert prices["area"] == "SE4" and prices["currency"] == "SEK" and prices["fees_as_of"] == "export"
    assert prices["columns"] == ["start", "end", "spot", "buy", "sell"]
    first = prices["rows"][0]
    assert first[0] == "2026-09-21T22:00:00+00:00" and first[1] == "2026-09-21T22:15:00+00:00"
    # Buy with every fee and VAT above the spot price; sell at spot plus the sell adjustment (none here).
    assert first[3] > first[2] and first[4] == first[2]
    assert prices["fees"]["vat_enabled"] is True and prices["sell_adjustment"] == 0.0

    quarters = record["quarters"]
    assert quarters["columns"] == list(QUARTER_COLUMNS) and len(quarters["rows"]) == 96
    assert quarters["rows"][0][13] == ACTION_GRID_CHARGE and quarters["rows"][0][16] == {MODE_BALANCED: [ACTION_GRID_CHARGE, 4.0]}

    review = record["battery_review"]
    assert review["date"] == "2026-09-22" and review["mode"] == MODE_BALANCED
    assert "baselines" in review and review["battery_saving"] is not None and "saving" in review

    house = record["house_hours"]
    assert house["rows"][0][:8] == ["2026-09-21T22:00:00+00:00", 2.5, 1.7, 0.8, 0.9, None, None, 8.0]

    settings = record["settings"]
    assert settings["as_of"] == "export" and settings["battery"]["usable_kwh"] == 40.3
    assert settings["price_area"] == "SE4" and settings["currency"] == "SEK" and settings["battery_mode"] == MODE_BALANCED
    assert record["export_curtail"] == {"log": [], "day": None}
    assert [charger["entry_id"] for charger in record["chargers"]] == ["entry_a"]


async def test_the_same_day_is_overwritten_and_no_temporary_file_is_left(
    hass: HomeAssistant, freezer, tmp_path: Path
) -> None:
    site, controller = await _world(hass, freezer, tmp_path)
    await _export(hass, {"date": "2026-09-22"})
    controller.battery_shadow.plan.actuals.buckets.clear()

    response = await _export(hass, {"site": site.entry_id, "date": "2026-09-22"}, return_response=True)

    folder = tmp_path / "spotnav_archive" / site.entry_id
    assert sorted(os.listdir(folder)) == ["2026-09-22.json"]
    assert json.loads((folder / "2026-09-22.json").read_text(encoding="utf-8"))["counts"]["quarters"] == 0
    assert response["missing"]["quarters"] == "none_recorded"


async def test_a_write_drops_the_day_files_older_than_the_bound(hass: HomeAssistant, freezer, tmp_path: Path) -> None:
    site, _controller = await _world(hass, freezer, tmp_path)
    folder = tmp_path / "spotnav_archive" / site.entry_id
    folder.mkdir(parents=True)
    oldest_kept = DAY - timedelta(days=day_export.ARCHIVE_KEPT_DAYS)
    for name in ((oldest_kept - timedelta(days=1)).isoformat(), oldest_kept.isoformat(), "notes"):
        (folder / f"{name}.json").write_text("{}", encoding="utf-8")

    await _export(hass, {"date": DAY.isoformat()})

    assert sorted(os.listdir(folder)) == sorted(
        ["2026-09-22.json", "notes.json", f"{oldest_kept.isoformat()}.json"]
    )


async def test_removing_the_site_removes_its_archive(hass: HomeAssistant, freezer, tmp_path: Path) -> None:
    site, _controller = await _world(hass, freezer, tmp_path)
    await _export(hass, {"date": DAY.isoformat()})
    folder = tmp_path / "spotnav_archive" / site.entry_id
    assert folder.is_dir()

    assert await hass.config_entries.async_remove(site.entry_id)
    await hass.async_block_till_done()

    assert not folder.exists()


async def test_a_day_no_longer_kept_exports_what_is_left_and_says_what_is_missing(
    hass: HomeAssistant, freezer, tmp_path: Path
) -> None:
    site, _controller = await _world(hass, freezer, tmp_path)

    response = await _export(hass, {"date": "2026-09-02"}, return_response=True)

    assert response["counts"]["quarters"] == 0 and response["counts"]["forecast_rows"] == 0
    assert response["missing"] == {
        "forecasts": MISSING_NOT_KEPT,
        "prices": MISSING_NO_PRICES,
        "quarters": MISSING_NOT_KEPT,
        "battery_review": "none_recorded",
        "house_hours": "none_recorded",
        "switch_load": MISSING_NO_LOAD,
    }
    assert (tmp_path / "spotnav_archive" / site.entry_id / "2026-09-02.json").exists()


async def test_today_is_written_as_incomplete_and_a_day_to_come_is_refused(
    hass: HomeAssistant, freezer, tmp_path: Path
) -> None:
    await _world(hass, freezer, tmp_path)

    response = await _export(hass, {"date": "2026-09-23"}, return_response=True)
    assert response["complete"] is False

    with pytest.raises(ServiceValidationError) as refused:
        await _export(hass, {"date": "2026-09-24"})
    assert refused.value.translation_key == "export_date_future"


# ------------------------------------------------------------------ the service


async def test_the_site_is_named_when_there_are_several_and_an_unknown_one_is_refused(
    hass: HomeAssistant, freezer, tmp_path: Path
) -> None:
    from tests.world import setup_charger_and_site

    await _world(hass, freezer, tmp_path)
    with pytest.raises(ServiceValidationError) as unknown:
        await _export(hass, {"site": "nope"})
    assert unknown.value.translation_key == "export_site_unknown"

    _charger, second = await setup_charger_and_site(hass, "entry_b")
    with pytest.raises(ServiceValidationError) as several:
        await _export(hass)
    assert several.value.translation_key == "export_site_required"
    response = await _export(hass, {"site": second.entry_id}, return_response=True)
    assert response["site_id"] == second.entry_id


async def test_without_a_site_the_call_is_refused(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(SET_UP)
    await add_price(hass)

    with pytest.raises(ServiceValidationError) as refused:
        await _export(hass)
    assert refused.value.translation_key == "export_no_site"


async def test_only_an_administrator_or_an_automation_may_export(
    hass: HomeAssistant, freezer, tmp_path: Path, hass_read_only_user: Any, hass_admin_user: Any
) -> None:
    await _world(hass, freezer, tmp_path)

    with pytest.raises(Unauthorized):
        await _export(hass, context=Context(user_id=hass_read_only_user.id))
    await _export(hass, context=Context(user_id=hass_admin_user.id))
    await _export(hass)


async def test_a_write_that_fails_is_a_translated_error(
    hass: HomeAssistant, freezer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _world(hass, freezer, tmp_path)

    def refuse(_path: Path, _data: bytes) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(day_export, "write_atomic", refuse)
    with pytest.raises(HomeAssistantError) as failed:
        await _export(hass)
    assert failed.value.translation_key == "export_write_failed"
    assert "path" in failed.value.translation_placeholders


def test_the_schema_takes_a_site_and_a_date_and_nothing_else() -> None:
    assert EXPORT_DAY_SCHEMA({"site": "abc", "date": "2026-09-22"}) == {"site": "abc", "date": DAY}
    assert EXPORT_DAY_SCHEMA({}) == {}
    with pytest.raises(vol.Invalid):
        EXPORT_DAY_SCHEMA({"date": "yesterday"})
    with pytest.raises(vol.Invalid):
        EXPORT_DAY_SCHEMA({"path": "/tmp"})
    services = yaml.safe_load((PACKAGE / "services.yaml").read_text(encoding="utf-8"))
    assert set(services["export_day"]["fields"]) == {"site", "date"}
    english = json.loads((PACKAGE / "translations" / "en.json").read_text(encoding="utf-8"))
    assert set(english["services"]["export_day"]["fields"]) == {"site", "date"}
    for key in ("export_site_unknown", "export_no_site", "export_site_required", "export_date_future", "export_write_failed"):
        assert key in english["exceptions"]


# ------------------------------------------------------------------ the atomic write


def test_the_write_makes_its_folders_and_replaces_a_file_whole(tmp_path: Path) -> None:
    path = archive_path(tmp_path, "site_x", "2026-09-22")
    assert path == tmp_path / "spotnav_archive" / "site_x" / "2026-09-22.json"

    write_atomic(path, b"first")
    write_atomic(path, b"second")

    assert path.read_bytes() == b"second"
    assert os.listdir(path.parent) == ["2026-09-22.json"]


def test_a_failed_write_keeps_the_earlier_file_and_leaves_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = archive_path(tmp_path, "site_x", "2026-09-22")
    write_atomic(path, b"kept")

    def broken(_source: Any, _target: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(day_export.os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(path, b"lost")

    assert path.read_bytes() == b"kept"
    assert os.listdir(path.parent) == ["2026-09-22.json"]


def test_the_encoding_is_json_with_one_line_per_part() -> None:
    record = {"schema": 1, "rows": [[1, "å"], [2, None]]}

    data = encode(record)

    assert json.loads(data) == record
    assert data.decode("utf-8").splitlines() == ["{", '"schema": 1,', '"rows": [[1,"å"],[2,null]]', "}"]


# ------------------------------------------------------------------ the debug bundle


async def test_the_debug_bundle_carries_the_last_three_days(hass: HomeAssistant, freezer, tmp_path: Path) -> None:
    await _world(hass, freezer, tmp_path)

    bundle = await async_build_debug_bundle(hass)

    records = bundle["sites"][0]["day_records"]
    assert [record["date"] for record in records] == ["2026-09-22", "2026-09-21", "2026-09-20"]
    assert records[0]["counts"]["quarters"] == 96 and records[0]["battery_review"]["date"] == "2026-09-22"
    assert "chargers" not in records[0]
    assert records[0]["spotnav_version"] and records[0]["spotnav_version"] == bundle["versions"]["spotnav"]
    assert "solar_statistics" not in records[0]["settings"]["last_plan_inputs"]
    text = json.dumps(records)
    assert "latitude" not in text and "longitude" not in text
