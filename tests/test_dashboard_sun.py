"""The dashboard's `sun` block for the charger card's price chart (`api/sun.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.api.sun import HOUR_KEYS, PEAK_KEY, MAX_QUARTERS, QUARTER_KEYS, SUN_KEYS, peak_kw, sun_block_from
from custom_components.spotnav.api.webhook import APP_DASHBOARD_BLOCK_READS, APP_READS_SUN, _for_app

TZ = "Europe/Stockholm"
# 2026-11-17 12:20 in Stockholm (UTC+1).
NOW = datetime(2026, 11, 17, 11, 20, tzinfo=timezone.utc)


def _hour(day_hour_utc: int, day: int = 17) -> datetime:
    return datetime(2026, 11, day, day_hour_utc, tzinfo=timezone.utc)


def test_measured_before_the_running_hour_and_corrected_forecast_from_it_on() -> None:
    measured = {_hour(9): 0.4, _hour(10): 0.7, _hour(11): 9.9}
    forecast = {_hour(11): 0.9, _hour(12): 0.5, _hour(9): 9.9}
    block = sun_block_from(NOW, TZ, measured, forecast)
    assert tuple(block) == SUN_KEYS
    by_start = {hour["start"]: hour for hour in block["hours"]}
    assert all(tuple(hour) == HOUR_KEYS for hour in block["hours"])
    assert by_start[_hour(9).isoformat()] == {"start": _hour(9).isoformat(), "kw": 0.4, "measured": True}
    assert by_start[_hour(10).isoformat()]["measured"] is True
    # The running hour (11:00 UTC) is forecast, never the measured figure.
    assert by_start[_hour(11).isoformat()] == {"start": _hour(11).isoformat(), "kw": 0.9, "measured": False}
    assert by_start[_hour(12).isoformat()]["measured"] is False
    assert len(block["hours"]) == 4


def test_the_running_hour_has_a_forecast_so_the_hours_run_without_a_gap() -> None:
    measured = {_hour(9): 0.4, _hour(10): 0.7}
    forecast = {_hour(11): 0.9, _hour(12): 0.5}
    block = sun_block_from(NOW, TZ, measured, forecast)
    assert [(hour["start"], hour["measured"]) for hour in block["hours"]] == [
        (_hour(9).isoformat(), True),
        (_hour(10).isoformat(), True),
        (_hour(11).isoformat(), False),
        (_hour(12).isoformat(), False),
    ]


def test_todays_measured_quarters_are_sent_up_to_now_and_none_where_statistics_are_missing() -> None:
    quarter = _hour(11)
    quarters = {
        _hour(9) - timedelta(days=1): 5.0,  # yesterday
        _hour(9): 0.4,
        quarter: 1.0,
        quarter + timedelta(minutes=15): 1.2,  # the quarter running at now (11:20) is sent
        quarter + timedelta(minutes=30): 9.0,  # in the future
    }
    block = sun_block_from(NOW, TZ, {}, {}, quarters)
    assert [item["start"] for item in block["quarters"]] == [
        _hour(9).isoformat(),
        quarter.isoformat(),
        (quarter + timedelta(minutes=15)).isoformat(),
    ]
    assert all(tuple(item) == QUARTER_KEYS for item in block["quarters"])
    assert sun_block_from(NOW, TZ, {}, {})["quarters"] == []
    many = {_hour(0) + timedelta(minutes=15 * index): 1.0 for index in range(200)}
    assert len(sun_block_from(_hour(23), TZ, {}, {}, many)["quarters"]) <= MAX_QUARTERS


def test_an_hour_with_no_figure_is_left_out_and_nothing_is_negative() -> None:
    block = sun_block_from(NOW, TZ, {_hour(8): -0.2}, {})
    assert block["hours"] == [{"start": _hour(8).isoformat(), "kw": 0.0, "measured": True}]


def test_only_today_and_tomorrow_are_sent() -> None:
    far = NOW + timedelta(days=3)
    near_past = NOW - timedelta(days=2)
    block = sun_block_from(NOW, TZ, {near_past.replace(minute=0, second=0, microsecond=0): 1.0}, {far.replace(minute=0, second=0, microsecond=0): 1.0})
    assert block["hours"] == []
    tomorrow = _hour(10, day=18)
    block = sun_block_from(NOW, TZ, {}, {tomorrow: 0.6})
    assert [hour["start"] for hour in block["hours"]] == [tomorrow.isoformat()]


def test_an_app_gets_the_block_only_when_it_reads_it() -> None:
    assert APP_DASHBOARD_BLOCK_READS["sun"] == APP_READS_SUN == "sun"
    dashboard = {"settings": {"amps": 10}, "sun": {"hours": []}}
    assert "sun" not in _for_app(dashboard, {})
    assert _for_app(dashboard, {"reads": ["sun"]})["sun"] == {"hours": []}


def test_the_block_reads_the_sites_corrected_forecast_and_measured_log(monkeypatch) -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.api import dashboard, sun
    from custom_components.spotnav.site.forecast_log import KIND_SUN, LoggedHour

    made = _hour(6)
    log = SimpleNamespace(hours=lambda kind: {_hour(9): LoggedHour(1.0, made, 0.6)} if kind == KIND_SUN else {})
    controller = SimpleNamespace(house_forecast=SimpleNamespace(log=log), config={})
    from custom_components.spotnav.site.sun_geometry import hour_key

    controller.solar_correction = SimpleNamespace(quarters={_hour(11): 1.234}, observed_max_kwh=2.5, array_kwp=16.65)
    # The stored year holds a far higher hour than the correction's recent window.
    controller.solar_shadow = SimpleNamespace(history=SimpleNamespace(pv={hour_key(_hour(3)): 1010}))
    forecast = SimpleNamespace(corrected={_hour(12): 0.8}, raw={_hour(12): 1.4})
    monkeypatch.setattr(dashboard, "site_binding", lambda _hass, _id: SimpleNamespace(entry_id="site"))
    monkeypatch.setattr(sun, "site_controller_for", lambda _hass, _id: controller)
    monkeypatch.setattr(sun, "_sun", lambda _hass, _controller: forecast)
    block = sun.capture_sun(None, "charger", NOW, TZ)
    assert block == {
        "hours": [
            {"start": _hour(9).isoformat(), "kw": 0.6, "measured": True},
            {"start": _hour(12).isoformat(), "kw": 0.8, "measured": False},
        ],
        "quarters": [{"start": _hour(11).isoformat(), "kw": 1.23}],
        "peak_kw": 10.1,
    }
    assert sun.capture_sun(None, "charger", NOW, None) is None
    monkeypatch.setattr(sun, "_sun", lambda _hass, _controller: None)
    assert sun.capture_sun(None, "charger", NOW, TZ) is None
    monkeypatch.setattr(dashboard, "site_binding", lambda _hass, _id: None)
    assert sun.capture_sun(None, "charger", NOW, TZ) is None


def test_without_a_forecast_a_solar_capable_site_gets_the_measured_production_only(monkeypatch) -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.api import dashboard, sun
    from custom_components.spotnav.const import CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DERIVED
    from custom_components.spotnav.site.sun_geometry import hour_key

    history = SimpleNamespace(pv={hour_key(_hour(9)): 60, hour_key(_hour(11)): 999, hour_key(_hour(12)): 500})
    controller = SimpleNamespace(
        config={CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DERIVED},
        solar_shadow=SimpleNamespace(history=history),
        solar_correction=SimpleNamespace(quarters={_hour(10): 2.0}, observed_max_kwh=None, array_kwp=6.0),
    )
    monkeypatch.setattr(dashboard, "site_binding", lambda _hass, _id: SimpleNamespace(entry_id="site"))
    monkeypatch.setattr(sun, "site_controller_for", lambda _hass, _id: controller)
    monkeypatch.setattr(sun, "_sun", lambda _hass, _controller: None)
    block = sun.capture_sun(None, "charger", NOW, TZ)
    # The running hour (11:00 UTC) and later are never measured hours; nothing is forecast.
    assert block == {
        "hours": [{"start": _hour(9).isoformat(), "kw": 0.6, "measured": True}],
        "quarters": [{"start": _hour(10).isoformat(), "kw": 2.0}],
        # The highest hour of the stored history (the correction has observed nothing).
        "peak_kw": 9.99,
    }
    controller.config = {}
    assert sun.capture_sun(None, "charger", NOW, TZ) is None


def test_the_peak_is_the_highest_hour_ever_measured_before_anything_else() -> None:
    assert peak_kw(9.8, 16.65) == 9.8
    assert peak_kw(10.126, None) == 10.13
    # The array's size only where nothing has been measured yet.
    assert peak_kw(None, 16.65) == 16.65
    assert peak_kw(0.0, None) is None
    assert peak_kw(None, None) is None


def test_the_block_carries_the_peak_only_where_one_is_known() -> None:
    assert PEAK_KEY not in sun_block_from(NOW, TZ, {}, {})
    block = sun_block_from(NOW, TZ, {_hour(9): 0.4}, {}, observed_max_kwh=10.1, array_kwp=16.65)
    assert block[PEAK_KEY] == 10.1
    assert tuple(block) == (*SUN_KEYS, PEAK_KEY)


def test_the_peak_comes_from_the_long_history_when_the_recent_window_is_low() -> None:
    assert peak_kw(2.5, 16.65, 10.13) == 10.13
    # A recent window above the history still wins.
    assert peak_kw(11.0, None, 10.13) == 11.0
    # No history: the window, then the array.
    assert peak_kw(2.5, 16.65, None) == 2.5
    assert peak_kw(None, 16.65, 0.0) == 16.65
    block = sun_block_from(NOW, TZ, {}, {}, observed_max_kwh=2.5, array_kwp=16.65, history_max_kwh=10.1)
    assert block[PEAK_KEY] == 10.1
