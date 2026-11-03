"""`spotnav/suggest_import_target` and the webhook's `suggest_import_target`, end to end on the pilot's site (a
charger in a direct site with the meter's total grid power, the pilot's two demand-charge parts), with a fake
recorder; and the dashboard's effective level beside the import target.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import target_suggestion as module
from custom_components.spotnav.api import webhook as webhook_module
from custom_components.spotnav.api.dashboard import CAPABILITY_KEYS
from custom_components.spotnav.api.target_suggestion import build_suggestion, SUGGEST_KEYS
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.hour_statistics import StatMeta

from .test_demand_site import demand_of, GRID, pilot_site, run
from .test_hour_statistics import cest, FakeRecorder, hours_between, rows
from .test_webhook_parity import _pinned
from .world import non_admin, setup_charger_and_site, ws_call
from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE

pytestmark = pytest.mark.usefixtures("offline_relay")


def recorder_for_site(days: int = 30) -> FakeRecorder:
    """Thirty days before 2026-09-22 of a house peaking 2.0-2.8 kW at 19:00 (and once 7.5 kW), 0.6 kW otherwise."""
    first = cest(2026, 8, 23, 0)
    starts = hours_between(first, days * 24)

    def house(start: Any) -> float:
        local = start + timedelta(hours=2)
        day = (local - (first + timedelta(hours=2))).days
        if local.hour != 19:
            return 600.0
        return 7500.0 if day == 12 else 2000.0 + 100.0 * (day % 9)

    return FakeRecorder({GRID: StatMeta("W", True, False)}, {GRID: rows(starts, house)})


async def area_site(hass: HomeAssistant, freezer: Any) -> Any:
    """The pilot's site, its clock read again now that the price (SE4, Stockholm's clock) is added."""
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    controller._demand_zone = None  # noqa: SLF001
    return site


async def test_the_suggestion_from_the_sites_statistics(hass: HomeAssistant, freezer) -> None:
    site = await area_site(hass, freezer)
    answer = await build_suggestion(hass, site, recorder_for_site())
    assert tuple(answer) == SUGGEST_KEYS
    assert answer["available"] and answer["reason"] is None
    # The pilot's day and night parts count every hour between them (the first morning's hours belong to the night
    # before: 31 days); the charger's own 6 A on three phases.
    assert (answer["counted"], answer["days"], answer["car_min_kw"]) == ("windows", 31, 4.14)
    assert answer["typical_kw"] == pytest.approx(2.4) and answer["suggestion_kw"] == 7.0
    assert answer["highest"] == {"kw": 7.5, "start": "2026-09-04T19:00:00+02:00", "date": "2026-09-04"}
    assert answer["car_kw"] == pytest.approx(6.4)
    assert [month["month"] for month in answer["months"]] == ["2026-08", "2026-09"]
    assert answer["grid_entities"] == [GRID]
    # The charger has no energy register, power sensor or measured current: named, the grid stands for the house.
    assert answer["chargers"] == [{"charger_id": "entry_a", "source": None, "has_statistics": False}]
    assert answer["period"]["end"] == "2026-09-22T08:00:00+02:00"


async def test_without_statistics_there_is_no_suggestion_and_the_reason_is_said(hass: HomeAssistant, freezer) -> None:
    site = await area_site(hass, freezer)
    bare = await build_suggestion(hass, site, FakeRecorder({}, {}))
    assert (bare["available"], bare["reason"], bare["suggestion_kw"]) == (False, "no_grid_statistics", None)
    few = await build_suggestion(hass, site, recorder_for_site(days=3))
    assert (few["available"], few["reason"], few["days"]) == (False, "too_few_days", 4)
    # The recorder is not running in the tests' Home Assistant: said, nothing read.
    assert (await build_suggestion(hass, site))["reason"] == "no_recorder"


async def test_the_answer_is_kept_per_site_for_some_minutes(hass: HomeAssistant, freezer, monkeypatch) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    reads: list[str] = []

    async def counted(hass_: HomeAssistant, entry: Any, reader: Any = None) -> dict[str, Any]:
        reads.append(entry.entry_id)
        return await build_suggestion(hass_, entry, recorder_for_site())

    monkeypatch.setattr(module, "build_suggestion", counted)
    first = await module.suggestion_answer(hass, site)
    again = await module.suggestion_answer(hass, site)
    assert first == again and reads == [site.entry_id]
    monkeypatch.setattr(module.time, "monotonic", lambda: 1e12)
    await module.suggestion_answer(hass, site)
    assert len(reads) == 2


async def test_over_the_socket_administrators_only_and_the_webhook_for_the_app(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, monkeypatch
) -> None:
    """The real socket (the clock not moved: a frozen clock and a socket's sign-in do not mix)."""
    hass.states.async_set(GRID, "0", {"unit_of_measurement": "W"})
    _charger, site = await setup_charger_and_site(hass, extra_data={CONF_GRID_POWER_SOURCE: {"power": GRID}})
    assert site is not None

    async def fake(hass_: HomeAssistant, entry: Any, reader: Any = None) -> dict[str, Any]:
        return {"api_version": 1, "site_id": entry.entry_id, "available": True, "suggestion_kw": 7.0}

    monkeypatch.setattr(module, "build_suggestion", fake)
    client = await hass_ws_client(hass)
    request = {"type": "spotnav/suggest_import_target", "api_version": 1, "charger_id": "entry_a"}
    await client.send_json({"id": 1, **request})
    frame = await client.receive_json()
    assert frame["success"] and frame["result"]["suggestion_kw"] == 7.0
    await client.send_json({"id": 2, "type": "spotnav/suggest_import_target", "api_version": 1, "site_id": site.entry_id})
    assert (await client.receive_json())["result"] == frame["result"]
    for index, (body, code) in enumerate(
        [
            ({"charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
            ({"api_version": 1}, "spotnav_charger_required"),
            ({"api_version": 1, "site_id": "entry_a"}, "spotnav_unknown_site"),
            ({"api_version": 1, "charger_id": "nope"}, "spotnav_unknown_charger"),
        ],
        start=3,
    ):
        await client.send_json({"id": index, "type": "spotnav/suggest_import_target", **body})
        answer = await client.receive_json()
        assert not answer["success"] and answer["error"]["code"] == code, body
    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    denied = await ws_call(reader, dict(request))
    assert denied["success"] is False and denied["error"]["code"] == "spotnav_not_admin"

    entry = hass.config_entries.async_get_entry("entry_a")
    response = await webhook_module.ACTIONS["suggest_import_target"](hass, entry, {"action": "suggest_import_target"})  # type: ignore[arg-type]
    body = json.loads(response.body)
    assert response.status == 200 and body == {"ok": True, "action": "suggest_import_target", **frame["result"]}
    response = await webhook_module.ACTIONS["suggest_import_target"](hass, entry, {"api_version": 2})  # type: ignore[arg-type]
    assert response.status == 400 and json.loads(response.body)["error"] == "spotnav_unsupported_api_version"
    assert "suggest_import_target" in CAPABILITY_KEYS


async def test_the_dashboard_shows_the_level_the_months_peak_lifted_the_target_to(hass: HomeAssistant, freezer) -> None:
    await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)  # 08:00-09:00 CEST
    assert demand_of(hass)["effective_target_kw"] == 6.0
    await run(hass, freezer, 60, 7000.0)  # 09:00-10:00: the day part's peak
    await run(hass, freezer, 10, 3000.0)
    demand = demand_of(hass)
    assert demand["import_target_kw"] == 6.0 and demand["effective_target_kw"] > 6.5


async def test_an_imported_hour_never_lifts_the_dashboards_level(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 70, 3000.0)  # 08:00-09:10 CEST measured
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    demand = controller.demand
    imported = [
        HourMean(start=cest(2026, 9, 22, 0) + timedelta(hours=index), kw=9.0, floor_kw=9.0, imported=True)
        for index in range(8)
    ]
    assert demand.apply_backfill(imported, (), demand.now()) == 8
    assert demand_of(hass)["effective_target_kw"] == 6.0
    assert demand_of(hass)["per_component"][0]["bar_kw"] == 9.0


async def test_the_contract_fixture(hass: HomeAssistant, freezer) -> None:
    """`target_suggestion/pilot.json`, the card's and the app's copy of the answer (`SPOTNAV_WRITE_FIXTURES=1`
    rewrites it), and the webhook's."""
    import os
    from pathlib import Path

    site = await area_site(hass, freezer)
    answer = json.loads(json.dumps(await build_suggestion(hass, site, recorder_for_site())))
    path = Path(__file__).resolve().parent / "fixtures" / "target_suggestion" / "pilot.json"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(answer, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == answer
    _pinned("suggest_import_target_success.json", {"ok": True, "action": "suggest_import_target", **answer})
