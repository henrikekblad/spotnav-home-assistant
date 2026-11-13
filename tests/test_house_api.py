"""`spotnav/get_house` end to end on the pilot's site (no battery, no solar forecast): the answer's shape, the same by
the site's own id, the house's measured hours in it, and the refusals."""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.house import websocket_get_house
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.house_view import AHEAD_HOURS, HOUR_KEYS, HOUSE_KEYS, PAST_HOURS

from .test_demand_site import pilot_site, run

pytestmark = pytest.mark.usefixtures("offline_relay")


class _Connection:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"id": message_id, "success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"id": message_id, "success": False, "error": {"code": code, "message": message}})


def ask(hass: HomeAssistant, message_id: int, **request: Any) -> dict[str, Any]:
    connection = _Connection()
    websocket_get_house(hass, connection, {"id": message_id, "type": "spotnav/get_house", **request})  # type: ignore[arg-type]
    (answer,) = connection.answers
    return answer


async def test_the_pilot_site_is_answered(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    await run(hass, freezer, 60, 3000.0)
    answer = ask(hass, 1, api_version=1, charger_id="entry_a")
    assert answer["success"], answer
    result = answer["result"]
    assert tuple(result) == HOUSE_KEYS
    assert (result["site_id"], result["state"], result["time_zone"]) == (site.entry_id, "shadow", "Europe/Stockholm")
    assert result["sun"] is None and result["battery"] is None
    hours = result["hours"]
    assert len(hours) == PAST_HOURS + AHEAD_HOURS and all(tuple(row) == HOUR_KEYS for row in hours)
    # The site's measured hours, the house's part of each (`None` where the chargers could not be read).
    tracker = site_controller_for(hass, site.entry_id).demand.tracker  # type: ignore[union-attr]
    assert len(tracker.hours) == 2
    by_start = {row["start"]: row["house_kw"] for row in hours}
    for hour in tracker.hours:
        expected = None if hour.house_kw is None else round(hour.house_kw, 3)
        assert by_start[hour.start.isoformat()] == expected
    assert result["sources"]["solar_forecasts"] == []
    assert ask(hass, 2, api_version=1, site_id=site.entry_id)["result"] == result


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
    ]
    for index, (request, code) in enumerate(cases, start=1):
        answer = ask(hass, index, **request)
        assert not answer["success"] and answer["error"]["code"] == code, request
    assert await hass.config_entries.async_unload(site.entry_id)
    answer = ask(hass, 99, api_version=1, site_id=site.entry_id)
    assert not answer["success"] and answer["error"]["code"] == "spotnav_no_site"
