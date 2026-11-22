"""`spotnav/get_plan` end to end on the pilot's site (a charger, no battery, no sun, a 6 kW import target and a demand
charge): the answer's shape, the same by the site's own id, and the refusals."""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.plan import websocket_get_plan
from custom_components.spotnav.site.plan_view import HOUR_KEYS, LOAD_KEYS, PLAN_KEYS
from .helpers import answers_of

from .test_demand_site import pilot_site, run

pytestmark = pytest.mark.usefixtures("offline_relay")


class _Connection:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    def send_result(self, message_id: int, result: Any) -> None:
        self.answers.append({"id": message_id, "success": True, "result": json.loads(json.dumps(result))})

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.answers.append({"id": message_id, "success": False, "error": {"code": code, "message": message}})


async def ask(hass: HomeAssistant, message_id: int, **request: Any) -> dict[str, Any]:
    connection = _Connection()
    websocket_get_plan(hass, connection, {"id": message_id, "type": "spotnav/get_plan", **request})  # type: ignore[arg-type]
    (answer,) = await answers_of(hass, connection)
    return answer


async def test_the_pilot_site_is_answered(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    await run(hass, freezer, 60, 4000.0)
    answer = await ask(hass, 1, api_version=1, charger_id="entry_a")
    assert answer["success"], answer
    result = answer["result"]
    assert tuple(result) == PLAN_KEYS
    assert (result["site_id"], result["time_zone"]) == (site.entry_id, "Europe/Stockholm")
    assert result["power_tariff"] is True
    assert result["month_peak"] is not None and result["month_peak"]["kw"] == pytest.approx(4.0, abs=0.1)
    assert all(tuple(row) == HOUR_KEYS for row in result["hours"])
    assert 1 <= result["window"]["hours"] <= 36
    assert all(tuple(load) == LOAD_KEYS for load in result["loads"])
    assert [load["kind"] for load in result["loads"]] == ["car"]
    assert all(row["sun_kw"] is None for row in result["hours"])
    # The import target holds every hour: it is the ceiling, never the fuse.
    assert {row["ceiling"] for row in result["hours"]} <= {"import_target", "power_tariff"}
    assert (await ask(hass, 2, api_version=1, site_id=site.entry_id))["result"] == result


async def test_refusals(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, import_target_kw=6.0)
    cases = [
        ({"charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
        ({"api_version": 2, "charger_id": "entry_a"}, "spotnav_unsupported_api_version"),
        ({"api_version": 1}, "spotnav_charger_required"),
        ({"api_version": 1, "charger_id": "nope"}, "spotnav_unknown_charger"),
        ({"api_version": 1, "site_id": "entry_a"}, "spotnav_unknown_site"),
    ]
    for index, (request, code) in enumerate(cases, start=1):
        answer = await ask(hass, index, **request)
        assert not answer["success"] and answer["error"]["code"] == code, request
    assert await hass.config_entries.async_unload(site.entry_id)
    answer = await ask(hass, 99, api_version=1, site_id=site.entry_id)
    assert not answer["success"] and answer["error"]["code"] == "spotnav_no_site"


async def test_a_target_written_without_a_reload_is_labelled_the_import_target(hass: HomeAssistant, freezer) -> None:
    # A low target (under the soft ceiling) written to the site's entry without a reload, as the card's editor writes it: the hold reads it
    # from there (`_import_target_kw`), and so does the label, never the controller's older copy of the config.
    from custom_components.spotnav.const import CONF_IMPORT_TARGET_KW
    from custom_components.spotnav.runtime import site_controller_for

    site = await pilot_site(hass, freezer)
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_IMPORT_TARGET_KW: 2.0})
    await run(hass, freezer, 60, 4000.0)
    controller = site_controller_for(hass, site.entry_id)
    assert CONF_IMPORT_TARGET_KW not in controller.config, "the controller's copy is the older one"
    result = (await ask(hass, 1, api_version=1, site_id=site.entry_id))["result"]
    labels = {row["ceiling"] for row in result["hours"]}
    assert "import_target" in labels and "power_level" not in labels


async def test_the_plan_steps_back_into_the_decision_history(hass: HomeAssistant, freezer) -> None:
    """Plan `plans/v3_decision_history.md`: each hour as planned at its start and as it ran, a restart mid-hour
    keeping the plan read at the start, the days before as missing, the debug bundle's cut, and the refusals."""
    from datetime import timedelta

    from custom_components.spotnav.debug_bundle import async_build_debug_bundle
    from custom_components.spotnav.runtime import site_controller_for

    site = await pilot_site(hass, freezer, import_target_kw=6.0)  # 08:00 CEST (06:00 UTC)
    await run(hass, freezer, 60, 4000.0)
    await run(hass, freezer, 30, 3000.0)  # to 09:30
    controller = site_controller_for(hass, site.entry_id)
    opened = controller.decision_history.ledger.open
    assert opened is not None and opened.planned_at is not None
    read_at = opened.planned_at
    assert read_at.minute == 0
    # A restart in the hour: the plan read at its start is kept with the samples so far.
    assert await hass.config_entries.async_reload(site.entry_id)
    await hass.async_block_till_done()
    controller = site_controller_for(hass, site.entry_id)
    assert controller.decision_history.ledger.open.planned_at == read_at
    freezer.tick(timedelta(minutes=2))
    await run(hass, freezer, 30, 3000.0)  # to 10:02

    result = (await ask(hass, 1, api_version=1, site_id=site.entry_id, date="2026-09-22"))["result"]
    assert tuple(result) == PLAN_KEYS and result["past"] is True and result["date"] == "2026-09-22"
    assert all(tuple(row) == HOUR_KEYS for row in result["hours"])
    assert all(tuple(load) == LOAD_KEYS for load in result["loads"])
    assert len(result["hours"]) == 24
    by_start = {row["start"]: row for row in result["hours"]}
    # Before SpotNav ran: missing, never filled in.
    assert by_start["2026-09-22T05:00:00+00:00"]["recorded"] == "missing"
    seven = by_start["2026-09-22T06:00:00+00:00"]
    eight = by_start["2026-09-22T07:00:00+00:00"]
    assert seven["past"] and seven["recorded"] in ("full", "partial") and seven["net_kw"] == pytest.approx(4.0, abs=0.1)
    assert eight["past"] and eight["net_kw"] == pytest.approx(3.0, abs=0.1) and eight["ceiling"] is not None
    # The running hour on is the plan.
    assert by_start["2026-09-22T08:00:00+00:00"]["past"] is False
    (car,) = [load for load in result["loads"] if load["kind"] == "car"]
    # Local 08:00 and 09:00 ran; 10:00 is the plan's.
    assert car["planned"][7] is None and car["ran"][7] is None
    assert car["planned"][8] is not None and car["ran"][8] is not None and car["ran"][9] is not None
    assert car["planned"][10] is None
    assert result["history"]["first"] == "2026-09-22T06:00:00+00:00"
    # The live answer is unchanged but for where the history begins.
    live = (await ask(hass, 2, api_version=1, site_id=site.entry_id))["result"]
    assert live["past"] is False and live["date"] is None and live["history"] == result["history"]
    # A day before SpotNav ran: every hour missing. `start` asks for its local day.
    before = (await ask(hass, 3, api_version=1, site_id=site.entry_id, start="2026-09-21T12:00:00+02:00"))["result"]
    assert before["date"] == "2026-09-21" and {row["recorded"] for row in before["hours"]} == {"missing"}
    for index, asked in enumerate(({"date": "2026-09-23"}, {"date": "2026-09-14"}, {"date": "nope"}, {"start": 5}), start=4):
        answer = await ask(hass, index, api_version=1, site_id=site.entry_id, **asked)
        assert not answer["success"] and answer["error"]["code"] == "spotnav_invalid_date", asked
    bundle = await async_build_debug_bundle(hass)
    history = bundle["sites"][0]["decision_history"]
    assert history["schema"] == 1 and len(history["hours"]) == 2 and history["open"]["hour"] == history["hours"][-1][0] + 1
    # The hour of the restart: its plan read at its start (0 minutes late), not after the restart.
    assert history["hours"][-1][history["hour_columns"].index("planned_at_min")] == 0
