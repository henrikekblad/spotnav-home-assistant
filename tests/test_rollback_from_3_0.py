"""Going back from SpotNav 3.0 to this release keeps what 3.0 stored.

`fixtures/v3_records/records.json` is what 3.0's own writers stored (made from the v3 branch by
`fixtures/v3_records/make_v3_records.py`): the two session stores, the Auto settings, a price entry's grid tariff, the
site's demand month and running hour, and a charger's core session record. Each is read here as this release reads it
from disk, saved again by this release, and read back: nothing 3.0 recorded is dropped, and what this release does
not know is written back as it was, so going forward to 3.0 again loses nothing either.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.core.session import ChargeSession as CoreSession
from custom_components.spotnav.planning.auto_settings import AutoSettingsStore, FiscalOverride, GridTariff
from custom_components.spotnav.sessions.model import ChargeSession
from custom_components.spotnav.sessions.store import SessionStore
from custom_components.spotnav.site.demand_tracker import HourTracker

from .harness import FlakyStore

V3: dict[str, Any] = json.loads((Path(__file__).parent / "fixtures" / "v3_records" / "records.json").read_text())
BIG, SMALL = "spotnav_sessions", "spotnav_sessions_open"


def v3(name: str) -> Any:
    return copy.deepcopy(V3[name])


def test_the_fixture_is_3_0s_shape() -> None:
    """The records carry what 3.0 adds: a test that read them without it would prove nothing."""
    rows = v3("sessions_big")["sessions"]["charger_a"]
    assert all("plugged_in_at" in row for row in rows)
    assert rows[0]["vehicle_decided_by"] == "camera"
    assert "plugged_in_at" in v3("sessions_small")["open"]["charger_a"]
    assert v3("auto_settings")["chargers"]["charger_a"]["settings"]["departure_calendar"] == "calendar.work"
    assert {"supplier_markup", "sell_adjustment"} <= set(v3("price_grid_tariff"))
    assert {"outdoor_c", "ex_battery_kw"} <= set(v3("demand_month"))


def test_every_session_3_0_recorded_is_read() -> None:
    for row in [*v3("sessions_big")["sessions"]["charger_a"], v3("sessions_small")["open"]["charger_a"]]:
        session = ChargeSession.from_dict(row)
        assert session is not None, row["id"]
        assert session.as_dict() == row, "written back exactly as 3.0 wrote it"


async def test_the_session_stores_lose_nothing_through_a_save_and_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    hass_storage[BIG] = {"version": 1, "minor_version": 1, "key": BIG, "data": v3("sessions_big")}
    hass_storage[SMALL] = {"version": 1, "minor_version": 1, "key": SMALL, "data": v3("sessions_small")}
    store = SessionStore(hass)
    await store.async_load()

    finished = store.closed_raw("charger_a")
    assert [item.id for item in finished] == ["A-1", "A-2"]
    assert finished[0].vehicle_decided_by == "camera"
    opened = store.open_raw("charger_a")
    assert opened is not None and opened.id == "A-3"

    # This release goes on with the open charge and ends it, then stops (a full write of both stores).
    end = opened.start + timedelta(hours=1)
    store.close(replace(opened, end=end, energy_kwh=7.0, last_register_kwh=None, last_sample_at=None), end)
    await store.async_flush()
    await hass.async_block_till_done(wait_background_tasks=True)

    again = SessionStore(hass)
    await again.async_load()
    rows = {item.id: item.as_dict() for item in again.closed_raw("charger_a")}
    assert set(rows) == {"A-1", "A-2", "A-3"}
    for row in v3("sessions_big")["sessions"]["charger_a"]:
        assert rows[row["id"]] == row, "a finished session is kept as 3.0 wrote it"
    small_open = v3("sessions_small")["open"]["charger_a"]
    assert rows["A-3"]["plugged_in_at"] == small_open["plugged_in_at"], "the open one keeps 3.0's field through its end"
    assert rows["A-3"]["energy_kwh"] == 7.0
    on_disk = hass_storage[BIG]["data"]["sessions"]["charger_a"]
    assert all("plugged_in_at" in row for row in on_disk)


async def test_the_auto_settings_keep_3_0s_fields_through_a_change(hass: HomeAssistant) -> None:
    disk = FlakyStore(v3("auto_settings"))
    store = AutoSettingsStore(hass, store=disk)
    await store.async_load()
    settings = store.settings("charger_a")
    assert (settings.revision, settings.amps, settings.target.target_percent) == (4, 16, 80.0)

    await store.async_update("charger_a", mutate=lambda current: replace(current, amps=10))
    written = disk.payload["chargers"]["charger_a"]["settings"]
    assert written["amps"] == 10
    assert written["departure_calendar"] == "calendar.work" and written["departure_calendar_only"] is True

    again = AutoSettingsStore(hass, store=FlakyStore(copy.deepcopy(disk.payload)))
    await again.async_load()
    assert again.settings("charger_a").amps == 10


def test_a_price_entrys_grid_tariff_reads_past_3_0s_supplier_figures() -> None:
    tariff = GridTariff.from_data(v3("price_grid_tariff"))
    assert tariff.transfer_percent == FiscalOverride(True, 5.0)


def test_the_demand_month_keeps_3_0s_temperatures_and_battery_figures() -> None:
    month, running = v3("demand_month"), v3("demand_running")
    tracker = HourTracker(tz="Europe/Stockholm")
    tracker.restore(month, running["running"])
    assert len(tracker.hours) == 2
    assert tracker.running is not None and tracker.running.import_wh == 500.0
    written = tracker.month_dict()
    assert written == month, "the month is written back as 3.0 wrote it"

    again = HourTracker(tz="Europe/Stockholm")
    again.restore(json.loads(json.dumps(written)), None)
    assert again.month_dict() == month


@pytest.mark.parametrize("extra", [{}, {"later_field": 1}])
def test_a_chargers_core_session_record_from_3_0_is_read(extra: dict[str, Any]) -> None:
    record = {**v3("core_session"), **extra}
    session = CoreSession.from_store(record)
    assert session.owner == "plan" and session.car_ended is not None and session.car_ended.soc == 80.0


def test_a_later_releases_notification_settings_keep_the_chargers_record() -> None:
    from custom_components.spotnav.notifications.settings import NotificationSettings, NotificationSettingsError

    read = NotificationSettings.from_stored(
        {"targets": ["mobile_app_x"], "events": ["plan_stopped", "battery_low"], "url": None, "event_set": 3, "z": 1}
    )
    assert read.targets == ("mobile_app_x",) and read.events == ("plan_stopped",)
    with pytest.raises(NotificationSettingsError):
        NotificationSettings.from_stored({"targets": "mobile_app_x", "events": [], "event_set": 3})
    with pytest.raises(NotificationSettingsError):
        NotificationSettings.from_stored({"targets": [], "events": ["plan_stopped"], "event_set": "3"})
    with pytest.raises(NotificationSettingsError):
        NotificationSettings.from_dict({"targets": [], "events": [], "z": 1})


def test_a_later_releases_camera_keeps_the_camera() -> None:
    from custom_components.spotnav.vehicles.camera_settings import CameraSettings, CameraSettingsError

    stored = {
        "camera_entity_id": "camera.drive",
        "ai_task_entity_id": None,
        "frame": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5, "angle": 0},
        "night_mode": True,
    }
    camera = CameraSettings.from_stored(stored)
    assert camera is not None and camera.camera_entity_id == "camera.drive"
    with pytest.raises(CameraSettingsError):
        CameraSettings.from_wire(stored)
    with pytest.raises(CameraSettingsError):
        CameraSettings.from_stored({"camera_entity_id": "camera.drive", "frame": None})
