"""`spotnav/get_power_month` and the webhook's `get_power_month`, end to end: a site measuring its hours with the
meter's total grid power, the pilot's two demand-charge components and a 6 kW import target.

The contract fixtures (`tests/fixtures/power_month/`, read by the card's tests with no copy, and the webhook's under
`tests/fixtures/webhook/`) are written from a seeded month: three weeks of hours put in the site's tracker, and the
month before kept as day summaries only (`SPOTNAV_WRITE_FIXTURES=1` rewrites them).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.dashboard import CAPABILITY_KEYS
from custom_components.spotnav.api import webhook as webhook_module
from custom_components.spotnav.api.power_month import POWER_MONTH_KEYS, websocket_get_power_month
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.pricing.price_source import site_demand_components
from custom_components.spotnav.runtime import site_controller_for

from .test_demand_site import GRID, pilot_site, run
from .test_price_device import NOW
from .test_webhook_parity import _pinned, post
from .world import setup_charger_and_site
from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE

pytestmark = pytest.mark.usefixtures("offline_relay")

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "power_month"
WRITE = os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1"
CEST = timedelta(hours=2)


def seeded_hours(year: int, month: int, last_day: int, *, last_hour: int = 24) -> list[HourMean]:
    """A plausible month of a 6 kW target's site (CEST): a house of 0.4-2.6 kW, and the car on most nights from
    01:00 or in the early evening, held at the target, paused once. Days 6 and 13 have a gap hour."""
    hours = []
    for day in range(1, last_day + 1):
        for local_hour in range(24 if day < last_day else last_hour):
            start = datetime(year, month, day, local_hour, tzinfo=timezone.utc) - CEST
            house = 0.4 + 0.15 * ((day * 7 + local_hour * 3) % 5)
            if 17 <= local_hour <= 20:
                house += 1.2 + 0.1 * (day % 4)
            car = 0.0
            held = paused = False
            if day % 3 != 0 and 1 <= local_hour <= 4:
                car = 4.1 + 0.3 * (day % 5)
            if day % 4 == 1 and local_hour in (18, 19):
                car = 3.7
            kw = house + car
            if kw > 5.95:
                car = max(0.0, 5.9 - house - 0.05 * (day % 3))
                kw = house + car
                held = True
            if day == 15 and local_hour == 19:
                kw, paused = house, True
            hours.append(
                HourMean(
                    start=start,
                    kw=round(kw, 3),
                    house_kw=round(house, 3),
                    incomplete=(day, local_hour) in ((6, 9), (13, 22)),
                    held=held,
                    paused=paused,
                )
            )
    return hours


async def seeded_site(hass: HomeAssistant, freezer: Any) -> Any:
    """The pilot's site at 2026-09-22 10:00 CEST with September's hours so far and August's days kept."""
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    demand = controller.demand
    components = site_demand_components(hass, site.entry_id)
    august = seeded_hours(2026, 8, 31)
    demand.archive.file_month((2026, 8), august, components, "Europe/Stockholm", NOW)
    demand.tracker.month, demand.tracker.hours = (2026, 9), seeded_hours(2026, 9, 22, last_hour=8)
    demand.tracker.previous_month, demand.tracker.previous = None, []
    # The site read its clock before the price was added (it reads it again every few minutes): the area's now.
    controller._demand_zone = None  # noqa: SLF001
    assert controller.demand_zone() == "Europe/Stockholm"
    return site


class _Connection:
    """What the command answers on, without a socket (a frozen clock and a socket's sign-in do not mix)."""

    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"id": message_id, "success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"id": message_id, "success": False, "error": {"code": code, "message": message}})


async def ask(hass: HomeAssistant, message_id: int, **request: Any) -> dict[str, Any]:
    """The command's answer, as the socket would carry it."""
    connection = _Connection()
    websocket_get_power_month(hass, connection, {"id": message_id, "type": "spotnav/get_power_month", **request})  # type: ignore[arg-type]
    (answer,) = connection.answers
    return answer


async def hook(hass: HomeAssistant, charger_id: str, **payload: Any) -> tuple[int, dict[str, Any]]:
    """The webhook action's answer for the charger's webhook, through the webhook's own table of actions."""
    entry = hass.config_entries.async_get_entry(charger_id)
    response = await webhook_module.ACTIONS["get_power_month"](hass, entry, payload)  # type: ignore[arg-type]
    assert response is not None
    return response.status, json.loads(response.body)


def _fixture(name: str, payload: dict[str, Any]) -> None:
    path = FIXTURE_DIR / name
    if WRITE:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == payload, f"{name} drifted from the backend"


async def test_the_month_is_answered_from_the_measured_hours(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)  # 08:00-09:00 CEST
    await run(hass, freezer, 60, 7000.0)  # 09:00-10:00
    await run(hass, freezer, 20, 3000.0)
    answer = await ask(hass, 1, api_version=1, charger_id="entry_a")
    assert answer["success"], answer
    result = answer["result"]
    assert tuple(result) == POWER_MONTH_KEYS
    assert (result["site_id"], result["month"], result["unit"], result["import_target_kw"]) == (
        site.entry_id,
        "2026-09",
        "kW",
        6.0,
    )
    assert result["available_months"] == ["2026-09"]
    assert [round(hour["kw"], 1) for hour in result["hours"]] == [4.0, 7.0]
    assert [hour["start"] for hour in result["hours"]] == ["2026-09-22T08:00:00+02:00", "2026-09-22T09:00:00+02:00"]
    (day,) = result["days"]
    assert (day["date"], day["peak_start"], day["counted"], day["held"], day["paused"]) == (
        "2026-09-22",
        "2026-09-22T09:00:00+02:00",
        [0],
        False,
        False,
    )
    assert result["bars"][0] == pytest.approx(6.975, abs=0.01) and result["bars"][1] is None
    # The same by the site's own id; the days survive a restart of the site.
    assert (await ask(hass, 2, api_version=1, site_id=site.entry_id))["result"] == result
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    again = (await ask(hass, 3, api_version=1, site_id=site.entry_id, month="2026-09"))["result"]
    assert again["days"] == result["days"] and again["hours"] == result["hours"]
    restored = site_controller_for(hass, site.entry_id).demand.archive.months  # type: ignore[union-attr]
    assert [day.day.isoformat() for day in restored[(2026, 9)].days] == ["2026-09-22"]


async def test_the_contract_fixtures_from_a_seeded_month(
    hass: HomeAssistant, freezer
) -> None:
    await seeded_site(hass, freezer)
    month = (await ask(hass, 1, api_version=1, charger_id="entry_a"))["result"]
    assert month["available_months"] == ["2026-09", "2026-08"]
    assert len(month["days"]) == 22 and len(month["hours"]) == 21 * 24 + 8
    assert any(day["held"] for day in month["days"]) and [day["date"] for day in month["days"] if day["paused"]] == [
        "2026-09-15"
    ]
    counted = {index: [day["date"] for day in month["days"] if index in day["counted"]] for index in (0, 1)}
    assert len(counted[0]) == 1 and len(counted[1]) == 1
    _fixture("pilot_month.json", month)
    older = (await ask(hass, 2, api_version=1, charger_id="entry_a", month="2026-08"))["result"]
    assert older["hours"] is None and len(older["days"]) == 31
    assert older["components"] == month["components"] and len(older["bars"]) == 2
    _fixture("pilot_older_month.json", older)
    # The webhook answers the same for the app, with the routing action beside it.
    status, body = await hook(hass, "entry_a", action="get_power_month", month="2026-08")
    assert status == 200 and body == {"ok": True, "action": "get_power_month", **older}
    _pinned("get_power_month_success.json", body)


async def test_refusals(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    cases = [
        ({"charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
        ({"api_version": 2, "charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
        ({"api_version": 1}, "spotnav_charger_required"),
        ({"api_version": 1, "charger_id": "nope"}, "spotnav_unknown_charger"),
        ({"api_version": 1, "charger_id": site.entry_id}, "spotnav_site_not_charger"),
        ({"api_version": 1, "site_id": "entry_a"}, "spotnav_unknown_site"),
        ({"api_version": 1, "site_id": 7}, "spotnav_unknown_site"),
        ({"api_version": 1, "charger_id": "entry_a", "month": "2026-10"}, "spotnav_invalid_range"),
        ({"api_version": 1, "charger_id": "entry_a", "month": "2024-08"}, "spotnav_invalid_range"),
        ({"api_version": 1, "charger_id": "entry_a", "month": "2026-9"}, "spotnav_invalid_range"),
        ({"api_version": 1, "charger_id": "entry_a", "month": 202609}, "spotnav_invalid_range"),
    ]
    for index, (request, code) in enumerate(cases, start=1):
        answer = await ask(hass, index, **request)
        assert not answer["success"] and answer["error"]["code"] == code, request
    # Two years back is still answered (empty).
    oldest = await ask(hass, 99, api_version=1, charger_id="entry_a", month="2024-09")
    assert oldest["success"] and oldest["result"]["days"] == [] and oldest["result"]["hours"] is None
    status, body = await hook(hass, "entry_a", month="soon")
    assert (status, body) == (400, {"ok": False, "error": "spotnav_invalid_range", "action": "get_power_month"})
    status, body = await hook(hass, "entry_a", api_version=2)
    assert (status, body["error"]) == (400, "spotnav_unsupported_api_version")
    assert "get_power_month" in CAPABILITY_KEYS


async def test_over_the_socket_and_the_webhook_a_site_that_cannot_measure_and_a_charger_in_none_are_refused(
    hass: HomeAssistant, hass_ws_client, hass_client_no_auth
) -> None:
    """The real socket and webhook (the clock not moved): the refusals, and a measuring site's empty month."""
    hass.states.async_set(GRID, "0", {"unit_of_measurement": "W"})
    await setup_charger_and_site(hass, extra_data={"import_target_kw": 6.0})
    await setup_charger_and_site(hass, "entry_b", site=False)
    await setup_charger_and_site(hass, "entry_c", extra_data={CONF_GRID_POWER_SOURCE: {"power": GRID}})
    client = await hass_ws_client(hass)

    async def socket(message_id: int, charger_id: str) -> dict[str, Any]:
        await client.send_json({"id": message_id, "type": "spotnav/get_power_month", "api_version": 1, "charger_id": charger_id})
        return await client.receive_json()

    assert (await socket(1, "entry_a"))["error"]["code"] == "spotnav_needs_total_grid_power"
    assert (await socket(2, "entry_b"))["error"]["code"] == "spotnav_no_site"
    measured = await socket(3, "entry_c")
    assert measured["success"] and measured["result"]["days"] == [] and measured["result"]["import_target_kw"] is None
    web = await hass_client_no_auth()
    status, body = await post(web, "webhook-entry_b", {"action": "get_power_month"})
    assert status == 400
    _pinned("get_power_month_no_site.json", body)
    status, body = await post(web, "webhook-entry_c", {"action": "get_power_month"})
    assert status == 200 and body["ok"] and body["action"] == "get_power_month" and body["hours"] == []


async def test_the_imported_month_fixture_with_the_months_peak(hass: HomeAssistant, freezer) -> None:
    """A month whose first days came from Home Assistant's statistics (`imported`), and an evening on the 8th when the
    house alone set the month's day peak (6.8 kW): from then on the line of the month's peak stands above the target
    in the day part's hours."""
    from dataclasses import replace

    site = await seeded_site(hass, freezer)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    tracker = controller.demand.tracker
    sauna = datetime(2026, 9, 8, 18, tzinfo=timezone.utc) - CEST
    hours = []
    for hour in tracker.hours:
        local_day = (hour.start + CEST).day
        if hour.start == sauna:
            hour = replace(hour, kw=6.8, house_kw=6.8, held=False, paused=True, floor_kw=6.8)
        if local_day <= 4:
            hour = replace(hour, held=False, paused=False, incomplete=False, imported=True, floor_kw=hour.kw)
        hours.append(hour)
    tracker.hours = hours
    month = (await ask(hass, 1, api_version=1, charger_id="entry_a"))["result"]
    assert [day["date"] for day in month["days"] if day["imported"]] == [f"2026-09-0{day}" for day in range(1, 5)]
    lifted = [hour for hour in month["hours"] if hour["month_peak_kw"] is not None]
    assert lifted and all(hour["start"] > "2026-09-08T18" for hour in lifted)
    assert {hour["month_peak_kw"] for hour in lifted} == {6.8}
    _fixture("pilot_imported_month.json", month)
