"""What the debug bundle keeps of the import target: its columns in the minute rows, and its change log (what it did
to each charger, when it was not applied, the departure's notice, every release with its source and its end), kept
24 hours across a restart (`site/site_history.py`)."""

from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_IMPORT_TARGET_KW
from custom_components.spotnav.debug_day import format_row, parse_row
from custom_components.spotnav.site.site_history import CHARGER_COLUMNS, SITE_COLUMNS, SiteHistory

from .test_import_target_hold import _charging, _HOUR, _tight_night, _world


def _row(columns, row: str) -> dict[str, str]:
    return dict(zip(columns, parse_row(row), strict=False))


def _actions(log: list[dict], charger: str | None = None) -> list[str]:
    return [entry["action"] for entry in log if charger is None or entry.get("charger") == charger]


async def test_the_rows_say_what_the_target_holds_and_does_to_each_charger(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "rows")
        await world.step()
        await _charging(world)
        await world.run(5)
        site = world.halo.controller
        charger = world.halo.charger.entry_id
        site._history.sample(site)  # noqa: SLF001
        samples = site._history.samples  # noqa: SLF001
        row = _row(SITE_COLUMNS, samples["rows"][-1])
        # Held under 5.88 kW (the 6.0 kW target less its margin), the house's 1 kW used, applied.
        assert row["target_hold_kw"] == "5.88"
        assert row["target_house_w"] == "1000"
        assert int(row["target_allowed_w"]) > 0
        assert row["target_not_applied"] == ""
        held = _row(CHARGER_COLUMNS, samples["chargers"][charger]["rows"][-1])
        assert (held["target_action"], held["target_cap_a"]) == ("held", "7")

        # The meter goes quiet for more than ten minutes: not applied, and why.
        world.silent = True
        await world.run(12)
        site._history.sample(site)  # noqa: SLF001
        row = _row(SITE_COLUMNS, site._history.samples["rows"][-1])  # noqa: SLF001
        assert row["target_hold_kw"] == "" and row["target_not_applied"] in ("gap", "no_reading")
        log = site.import_target_snapshot()["log"]
        site_entries = [entry for entry in log if entry["charger"] is None]
        states = [entry["action"] for entry in site_entries]
        # Only the transitions: off before the target was set, not applied until the first reading, applied, then not.
        assert states[-2:] == ["applied", "not_applied"], states
        assert site_entries[-1]["reason"] == row["target_not_applied"]

        # The target is taken away: the columns are empty, and the log says it is off.
        entry = hass.config_entries.async_get_entry(site.entry_id)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_IMPORT_TARGET_KW: None})
        world.silent = False
        await world.run(2)
        site._history.sample(site)  # noqa: SLF001
        row = _row(SITE_COLUMNS, site._history.samples["rows"][-1])  # noqa: SLF001
        assert [row[column] for column in ("target_hold_kw", "target_allowed_w", "target_not_applied")] == ["", "", ""]
        assert site.import_target_snapshot()["log"][-1]["action"] == "off"


def test_rows_kept_from_before_the_target_columns_still_read() -> None:
    now = dt_util.utcnow().replace(microsecond=0)
    old = format_row([now.strftime("%Y-%m-%dT%H:%M:%SZ"), 1200.0, "total", 5.1, 5.0, 5.2, None])
    history = SiteHistory()
    history._restore({"rows": [old], "chargers": {}})  # noqa: SLF001
    rows = history.samples["rows"]
    assert rows == [old]
    row = _row(SITE_COLUMNS, rows[0])
    assert row["grid_w"] == "1200" and "target_hold_kw" not in row


def test_the_change_log_survives_a_restart_and_keeps_only_the_last_day() -> None:
    now = dt_util.utcnow()
    history = SiteHistory()
    history.note_target({"at": now.isoformat(), "charger": "a", "action": "held", "cap_a": 7.0})
    stored = history._to_store()  # noqa: SLF001
    stored["import_target_log"].insert(
        0, {"at": (now - timedelta(hours=25)).isoformat(), "charger": "a", "action": "paused"}
    )
    stored["import_target_log"].append({"at": "garbage", "charger": "a", "action": "held"})
    stored["import_target_log"].append({"at": now.isoformat(), "charger": "a", "action": "held", "nested": {"x": 1}})
    fresh = SiteHistory()
    fresh.note_target({"at": now.isoformat(), "charger": "a", "action": "free"})  # taken before the store was read
    fresh._restore(stored)  # noqa: SLF001
    assert [entry["action"] for entry in fresh.target_log] == ["held", "free"]


async def test_the_notice_and_every_release_are_logged_with_their_source_and_end(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.api.import_target import release_import_target, webhook_release_import_target

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "logged")
        await world.run(10)
        site = world.halo.controller
        charger = world.halo.charger.entry_id
        log = site.import_target_snapshot()["log"]
        told = next(entry for entry in log if entry["action"] == "notice_told")
        assert told["charger"] == charger and told["departure"] == world.departure.isoformat()
        assert told["missing_kwh"] > 5
        assert "at_risk" in _actions(log, charger)

        # The phone's button: released, and the notice taken off the phones.
        stamp = int(world.departure.timestamp())
        hass.bus.async_fire(
            "mobile_app_notification_action", {"action": f"SPOTNAV_IMPORT_TARGET_RELEASE_{charger}_{stamp}"}
        )
        await hass.async_block_till_done()
        await world.run(1)
        log = site.import_target_snapshot()["log"]
        release = next(entry for entry in log if entry["action"] == "release")
        assert (release["source"], release["until"]) == ("phone", world.departure.isoformat())
        cleared = next(entry for entry in log if entry["action"] == "notice_cleared")
        assert cleared["departure"] == world.departure.isoformat() and cleared["missing_kwh"] == told["missing_kwh"]

        # The card's command and the app's webhook name their source, a refusal too.
        assert release_import_target(hass, world.halo.charger, "card") is None
        response = await webhook_release_import_target(hass, world.halo.charger, {})
        assert response.status == 200
        sources = [entry["source"] for entry in site.import_target_snapshot()["log"] if entry["action"] == "release"]
        assert sources == ["phone", "card", "app"]

        # The departure passes: the release's end says why.
        frozen.tick(timedelta(hours=2))
        world.halo.advance(7200.0)
        site._prune_released(dt_util.utcnow())  # noqa: SLF001
        ended = [entry for entry in site.import_target_snapshot()["log"] if entry["action"] == "release_ended"]
        assert ended[-1]["reason"] == "departure_passed" and ended[-1]["until"] == world.departure.isoformat()


async def test_the_plans_own_release_and_its_end_on_unplug_are_logged(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.execution.chargers.adapter import ChargerAdapter

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "planlogged", choice="release")
        await world.run(5)
        site = world.halo.controller
        charger = world.halo.charger.entry_id
        release = next(entry for entry in site.import_target_snapshot()["log"] if entry["action"] == "release")
        assert (release["source"], release["automatic"]) == ("plan", True)
        monkeypatch.setattr(ChargerAdapter, "vehicle_connected", lambda self: False)
        site._prune_released(dt_util.utcnow())  # noqa: SLF001
        ended = next(entry for entry in site.import_target_snapshot()["log"] if entry["action"] == "release_ended")
        assert (ended["charger"], ended["reason"], ended["automatic"]) == (charger, "unplugged", True)
