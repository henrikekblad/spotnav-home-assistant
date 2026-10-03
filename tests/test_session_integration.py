"""Charge sessions through a real charger entry: recording, the plan's price maths, sensors, the
dashboard block, the WebSocket command and removal."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.dashboard import serialize_dashboard
from custom_components.spotnav.api.sessions import SESSIONS_API_VERSION
from custom_components.spotnav.const import CONF_ENERGY_REGISTER_ENTITY
from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.sessions.recorder import END_DEBOUNCE_S

from .helpers import make_entry
from .world import entity_id, go_auto, non_admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

NOW = "2026-09-22 06:00:00"
REGISTER = "sensor.charger_energy"
REGISTER_ATTRIBUTES = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}
UTC = timezone.utc


async def charger(hass: HomeAssistant, *, entry_id: str = "entry_a"):
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set(REGISTER, "100", REGISTER_ATTRIBUTES)
    entry = make_entry(
        hass, entry_id=entry_id, charge_control="switch.charger_a", current_limit=None,
        webhook_id=f"webhook-{entry_id}", title=entry_id, extra={CONF_ENERGY_REGISTER_ENTITY: REGISTER},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def recorder_of(hass: HomeAssistant, entry_id: str = "entry_a"):
    data = charger_data(hass, entry_id)
    assert data is not None and data.sessions is not None
    return data.sessions


async def one_charge(hass, frozen, entry, *, hours: float = 1.0, kwh: float = 6.0) -> None:
    """Start by hand, deliver `kwh` over `hours`, stop: the same steps a person's charge takes."""
    turn_on = async_mock_service(hass, "switch", "turn_on")
    controller = charger_data(hass, entry.entry_id).controller
    assert await controller.async_start(manual=True)
    assert turn_on
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert recorder_of(hass).session is not None, "the charge state report opened the session"
    start = datetime.now(UTC)
    frozen.move_to(start + timedelta(hours=hours))
    register = float(hass.states.get(REGISTER).state)
    hass.states.async_set(REGISTER, str(register + kwh), REGISTER_ATTRIBUTES)
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    frozen.move_to(datetime.now(UTC) + timedelta(seconds=END_DEBOUNCE_S + 1))
    recorder_of(hass).evaluate()
    await hass.async_block_till_done()


async def test_a_charge_is_recorded_and_costed_with_the_dashboards_own_effective_prices(
    hass: HomeAssistant,
) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        before = dashboard_api.capture_dashboard(hass, entry)
        assert before.intervals, "the plan's own effective prices are held"
        started = datetime.now(UTC)

        await one_charge(hass, frozen, entry, hours=1.0, kwh=6.0)

        (done,) = domain_data(hass).session_store.closed(entry.entry_id)
        assert done.started_by == "manual" and done.energy_source == "register"
        assert done.energy_kwh == pytest.approx(6.0)
        # Independently: 6 kWh spread over the hour, each quarter at that quarter's effective price.
        quarters = [
            row for row in before.intervals if started <= row.utc_start < started + timedelta(hours=1)
        ]
        assert len(quarters) == 4
        expected = sum(1.5 * row.effective_minor_per_kwh for row in quarters)
        assert done.cost_minor == pytest.approx(expected)
        assert done.currency == before.area_entry.currency
        assert done.average_price_minor_per_kwh == pytest.approx(expected / 6.0)


async def test_the_sensors_report_this_month_last_month_and_the_last_session(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        await one_charge(hass, frozen, entry, hours=1.0, kwh=6.0)
        await hass.async_block_till_done()

        def state(key: str):
            return hass.states.get(entity_id(hass, entry.entry_id, key))

        assert float(state("sessions_energy_this_month").state) == pytest.approx(6.0)
        assert float(state("sessions_cost_this_month").state) > 0
        assert state("sessions_cost_this_month").attributes["unit_of_measurement"] == "SEK"
        assert float(state("sessions_energy_last_month").state) == 0.0
        assert state("sessions_cost_last_month").state == "unknown"
        assert float(state("sessions_last_energy").state) == pytest.approx(6.0)
        assert float(state("sessions_last_cost").state) == pytest.approx(
            float(state("sessions_cost_this_month").state)
        )
        assert state("sessions_last_energy").attributes["started_by"] == "manual"
        assert state("sessions_last_energy").attributes["estimated"] is False


async def test_the_dashboard_carries_the_additive_sessions_summary(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        empty = serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)
        assert empty["sessions_summary"]["this_month"]["sessions"] == 0
        assert empty["sessions_summary"]["this_month"]["cost"] is None

        await one_charge(hass, frozen, entry)

        block = serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)["sessions_summary"]
        assert list(block) == ["this_month", "last_month"]
        assert block["this_month"]["period"] == "2026-09"
        assert block["this_month"]["sessions"] == 1
        assert block["this_month"]["energy_kwh"] == pytest.approx(6.0)
        assert block["this_month"]["savings_estimate"] is True
        assert block["last_month"]["period"] == "2026-08" and block["last_month"]["sessions"] == 0


async def test_a_reload_resumes_an_open_session(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        controller = charger_data(hass, entry.entry_id).controller
        async_mock_service(hass, "switch", "turn_on")
        await controller.async_start(manual=True)
        hass.states.async_set("switch.charger_a", "on")
        await hass.async_block_till_done()
        frozen.move_to(datetime.now(UTC) + timedelta(minutes=30))
        hass.states.async_set(REGISTER, "103", REGISTER_ATTRIBUTES)
        recorder_of(hass).evaluate()
        opened = recorder_of(hass).session.start

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        resumed = recorder_of(hass).session
        assert resumed is not None and resumed.start == opened
        assert resumed.energy_kwh == pytest.approx(3.0)


async def test_a_charger_removed_takes_its_sessions_with_it(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        await go_auto(hass)
        await one_charge(hass, frozen, entry)
        store = domain_data(hass).session_store
        assert store.closed(entry.entry_id)

        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

        assert store.closed(entry.entry_id) == ()


# ------------------------------------------------------------------------------ WebSocket


async def test_the_websocket_command_answers_summaries_and_the_latest_sessions(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await charger(hass)
    store = domain_data(hass).session_store
    from .sessions_helpers import session

    now = datetime.now(UTC)
    for index in range(3):
        store.close(session(now - timedelta(days=index, hours=3), charger_id=entry.entry_id, energy=10 + index), now)
    client = await hass_ws_client(hass)

    reply = await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id, "limit": 2})

    assert reply["success"] is True
    result = reply["result"]
    assert list(result) == [
        "api_version", "charger_id", "retention_days", "cost_basis", "this_month", "last_month", "months", "days",
        "open", "sessions", "month", "month_summary", "month_days", "month_sessions", "available_months",
    ]
    assert result["api_version"] == SESSIONS_API_VERSION == 1
    assert len(result["sessions"]) == 2
    assert result["sessions"][0]["start"] > result["sessions"][1]["start"], "newest first"
    assert result["sessions"][0]["savings_estimate"] is True
    assert result["open"] is None
    assert result["days"] and result["months"]


async def test_every_authenticated_user_may_read_the_history(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token
) -> None:
    entry = await charger(hass)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    reply = await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id})

    assert reply["success"] is True


async def test_the_csv_is_a_date_range_of_closed_sessions_with_a_file_name_made_of_dates(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await charger(hass)
    store = domain_data(hass).session_store
    from .sessions_helpers import session

    now = datetime.now(UTC)
    store.close(session(datetime(2026, 9, 1, 10, tzinfo=UTC), charger_id=entry.entry_id), now)
    store.close(session(datetime(2026, 9, 10, 10, tzinfo=UTC), charger_id=entry.entry_id), now)
    store.close(session(datetime(2026, 9, 20, 10, tzinfo=UTC), charger_id=entry.entry_id), now)
    client = await hass_ws_client(hass)

    reply = await ws_call(client, {
        "type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id,
        "format": "csv", "from": "2026-09-05", "to": "2026-09-15",
    })

    assert reply["success"] is True
    assert reply["result"]["filename"] == "spotnav-sessions-2026-09-05-2026-09-15.csv"
    rows = list(csv.DictReader(io.StringIO(reply["result"]["csv"])))
    assert len(rows) == 1 and rows[0]["start"].startswith("2026-09-10")
    assert entry.entry_id not in reply["result"]["filename"]


async def test_a_month_answers_its_summary_every_day_its_sessions_and_the_months_with_data(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await charger(hass)
    store = domain_data(hass).session_store
    from .sessions_helpers import session

    now = datetime.now(UTC)
    store.close(session(datetime(2026, 9, 1, 10, tzinfo=UTC), charger_id=entry.entry_id, energy=10), now)
    store.close(session(datetime(2026, 9, 10, 10, tzinfo=UTC), charger_id=entry.entry_id, energy=5), now)
    store.close(session(datetime(2026, 7, 4, 10, tzinfo=UTC), charger_id=entry.entry_id, energy=7), now)
    client = await hass_ws_client(hass)

    with freeze_time(NOW):
        default = (await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id}))["result"]
        july = (await ws_call(client, {
            "type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id, "month": "2026-07"}))["result"]
        empty = (await ws_call(client, {
            "type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id, "month": "2026-08"}))["result"]

        assert default["month"] == "2026-09" and default["month_summary"]["sessions"] == 2
        assert default["available_months"] == ["2026-09", "2026-07"]
        assert len(default["month_days"]) == 30 and len(default["month_sessions"]) == 2
        assert default["month_sessions"][0]["start"] > default["month_sessions"][1]["start"]
        assert july["month"] == "2026-07" and july["month_summary"]["energy_kwh"] == 7
        assert len(july["month_days"]) == 31 and july["available_months"] == default["available_months"]
        assert empty["month_summary"]["sessions"] == 0 and empty["month_sessions"] == []
        assert len(empty["month_days"]) == 31 and all(day["sessions"] == 0 for day in empty["month_days"])
        assert empty["this_month"] == default["this_month"], "the existing fields are untouched by `month`"


async def test_the_month_is_at_most_twenty_four_months_back_and_never_in_the_future(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await charger(hass)
    client = await hass_ws_client(hass)

    with freeze_time(NOW):
        async def ask(month):
            return await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 1,
                                          "charger_id": entry.entry_id, "month": month})

        assert (await ask("2024-09"))["success"] is True
        assert (await ask("2026-09"))["success"] is True
        for bad in ("2024-08", "2026-10", "2026-13", "2026-9", "20260", 202609, "2026-09-01"):
            reply = await ask(bad)
            assert reply["success"] is False and reply["error"]["code"] == "spotnav_invalid_range", bad


async def test_a_months_csv_covers_the_whole_month_and_is_refused_beside_a_date_range(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await charger(hass)
    store = domain_data(hass).session_store
    from .sessions_helpers import session

    now = datetime.now(UTC)
    for day in (datetime(2026, 8, 31, 12, tzinfo=UTC), datetime(2026, 9, 1, 12, tzinfo=UTC), datetime(2026, 9, 30, 12, tzinfo=UTC), datetime(2026, 10, 1, 12, tzinfo=UTC)):
        store.close(session(day, charger_id=entry.entry_id), now)
    client = await hass_ws_client(hass)
    with freeze_time(NOW):
        base = {"type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id, "format": "csv"}

        reply = await ws_call(client, {**base, "month": "2026-09"})
        both = await ws_call(client, {**base, "month": "2026-09", "from": "2026-09-02"})

        assert reply["result"]["filename"] == "spotnav-sessions-2026-09-01-2026-09-30.csv"
        assert [row["start"][:10] for row in csv.DictReader(io.StringIO(reply["result"]["csv"]))] == ["2026-09-01", "2026-09-30"]
        assert both["success"] is False and both["error"]["code"] == "spotnav_invalid_range"


async def test_the_webhook_sessions_action_answers_what_the_websocket_does(
    hass: HomeAssistant, hass_ws_client, hass_client_no_auth
) -> None:
    entry = await charger(hass)
    store = domain_data(hass).session_store
    from .sessions_helpers import session

    store.close(session(datetime(2026, 9, 10, 10, tzinfo=UTC), charger_id=entry.entry_id), datetime.now(UTC))
    socket = await hass_ws_client(hass)
    http = await hass_client_no_auth()
    webhook_id = f"webhook-{entry.entry_id}"

    with freeze_time(NOW):
        for extra in ({}, {"month": "2026-09"}, {"month": "2026-08", "limit": 1}, {"month": "2026-09", "format": "csv"}):
            expected = (await ws_call(socket, {
                "type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id, **extra}))["result"]
            response = await http.post(f"/api/webhook/{webhook_id}", json={"version": 1, "action": "sessions", "api_version": 1, **extra})
            assert response.status == 200
            assert await response.json() == {"ok": True, "action": "sessions", **expected}

        refused = await http.post(f"/api/webhook/{webhook_id}", json={"version": 1, "action": "sessions", "month": "2020-01"})
        wrong = await http.post(f"/api/webhook/{webhook_id}", json={"version": 1, "action": "sessions", "api_version": 2})
        assert refused.status == 400 and await refused.json() == {"ok": False, "error": "spotnav_invalid_range", "action": "sessions"}
        assert wrong.status == 400 and (await wrong.json())["error"] == "spotnav_unsupported_api_version"


@pytest.mark.parametrize(
    "extra",
    [{"format": "xml"}, {"limit": 0}, {"limit": 1000}, {"limit": "5"}, {"from": "yesterday"},
     {"from": "2026-09-10", "to": "2026-09-01"}],
)
async def test_a_bad_request_is_refused_with_a_stable_code(hass: HomeAssistant, hass_ws_client, extra) -> None:
    entry = await charger(hass)
    client = await hass_ws_client(hass)

    reply = await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 1, "charger_id": entry.entry_id, **extra})

    assert reply["success"] is False and reply["error"]["code"] == "spotnav_invalid_range"


async def test_a_wrong_version_and_an_unknown_charger_are_refused_like_the_other_contracts(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await charger(hass)
    client = await hass_ws_client(hass)

    wrong = await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 2, "charger_id": entry.entry_id})
    unknown = await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 1, "charger_id": "nope"})
    missing = await ws_call(client, {"type": "spotnav/get_sessions", "api_version": 1})

    assert wrong["error"]["code"] == "spotnav_unsupported_api_version"
    assert unknown["error"]["code"] == "spotnav_unknown_charger"
    assert missing["error"]["code"] == "spotnav_charger_required"


async def test_the_start_cause_is_consumed_once_and_only_while_fresh(hass: HomeAssistant) -> None:
    with freeze_time(NOW) as frozen:
        entry = await charger(hass)
        controller = charger_data(hass, entry.entry_id).controller
        async_mock_service(hass, "switch", "turn_on")

        assert controller.consume_start_cause() is None
        await controller.async_start(cause="solar")
        assert controller.consume_start_cause() == "solar"
        assert controller.consume_start_cause() is None, "once"

        hass.states.async_set("switch.charger_a", "off")
        await controller.async_start()
        frozen.move_to(datetime.now(UTC) + timedelta(minutes=10))
        assert controller.consume_start_cause() is None, "a stale cause belongs to nothing"
