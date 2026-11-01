"""`spotnav/plan_preview`: the plan a Save would make, with nothing saved.

* The preview is the plan a Save of the same body then makes, at the same moment (also across a 25-hour day).
* Nothing happens: no settings, revision, count or proposal written, no notice, no event, no appointment, nothing
  asked of the charger or the car, no listener told.
* A body a Save refuses is refused with the Save's own code; a caller a Save refuses is refused the same way.
* The transport: the answer's shape, the rate limit per connection and the body cap.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import date, datetime
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import plan_preview
from custom_components.spotnav.api.plan_preview import async_plan_preview, encode_preview
from custom_components.spotnav.api.settings import async_update_settings, encode_settings
from custom_components.spotnav.planning.auto_settings import AutoSettingsError
from custom_components.spotnav.runtime import charger_data, domain_data, preview_for

from .messages import forbid_charger_writes
from .relay import STOCKHOLM, SE4, StubTransport, day_body, index_listing, serve
from .world import admin, go_auto, settings_of, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

#: 08:00 in Stockholm on the served fixture day (as the dashboard tests use it).
NOW = "2026-09-22 06:00:00"


def body_of(hass: HomeAssistant, entry_id: str, **changes: Any) -> dict[str, Any]:
    """The Plan dialog's Save body: the stored record as the wire carries it, less its revision, with changes."""
    encoded = encode_settings(settings_of(hass, entry_id))
    return {key: value for key, value in encoded.items() if key not in ("revision", "fiscal_included")} | changes


def preview_message(entry_id: str, body: Any, api_version: Any = 1) -> dict[str, Any]:
    return {"type": "spotnav/plan_preview", "api_version": api_version, "charger_id": entry_id, "settings": body}


async def saved_preview(hass: HomeAssistant, entry_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Save `body` for real, then encode what the controller planned, as a preview encodes it."""
    await async_update_settings(
        hass, entry_id, expected_revision=settings_of(hass, entry_id).revision, replacement=body
    )
    await hass.async_block_till_done()
    controller = preview_for(hass, entry_id)
    assert controller is not None
    return encode_preview(controller.snapshot(), settings_of(hass, entry_id))


# ------------------------------------------------------------------------------- what it plans


@freeze_time(NOW)
async def test_the_preview_is_the_plan_a_save_then_makes(hass: HomeAssistant, transport: StubTransport) -> None:
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass, requested_kwh=6.0, departure_enabled=True, departure=datetime(2026, 1, 1, 18, 0).time())
    body = body_of(hass, entry.entry_id, requested_kwh=11.5, departure_time="16:15")

    answer = await async_plan_preview(hass, entry.entry_id, body, plan_only=False)
    assert answer["ok"] is True and answer["error"] is None
    preview = answer["preview"]
    assert preview["plan"] is not None, "the served days hold the whole window"
    assert preview["state"] == "proposal_ready"

    saved = await saved_preview(hass, entry.entry_id, body)
    assert preview == saved, "the plan a Save makes is the one the preview showed"
    assert settings_of(hass, entry.entry_id).requested_kwh == 11.5


@freeze_time(NOW)
async def test_the_answer_states_the_plan_its_status_and_strip(hass: HomeAssistant, transport: StubTransport) -> None:
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass, requested_kwh=6.0, departure_enabled=True, departure=datetime(2026, 1, 1, 18, 0).time())
    answer = await async_plan_preview(hass, entry.entry_id, body_of(hass, entry.entry_id), plan_only=False)
    preview = answer["preview"]
    assert set(answer) == {"api_version", "ok", "error", "preview"}
    assert set(preview) == {"state", "reason", "plan", "status", "strip"}
    plan = preview["plan"]
    assert set(plan) == {
        "periods", "planned_kwh", "requested_kwh", "power_kw", "cost", "finish", "unpriced", "short_of_deadline",
    }
    assert plan["finish"] == plan["periods"][-1]["end"]
    assert plan["cost"]["currency"] and plan["cost"]["value"] > 0
    assert plan["short_of_deadline"] is False
    strip = preview["strip"]
    assert set(strip) == {"start", "end", "quarters"}
    planned = [quarter for quarter in strip["quarters"] if quarter["planned"]]
    assert planned and planned[0]["start"] == plan["periods"][0]["start"]
    assert all(set(quarter) == {"start", "price", "planned"} for quarter in strip["quarters"])
    # The departure ends the strip: 18:00 in Stockholm.
    assert datetime.fromisoformat(strip["end"]).astimezone(STOCKHOLM).hour == 18
    assert preview["status"]["tone"] == "normal"


@freeze_time(NOW)
async def test_a_departure_the_need_does_not_make_says_so(hass: HomeAssistant, transport: StubTransport) -> None:
    """Best effort: every quarter to the departure, and the status line that says what the car gets."""
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass, requested_kwh=6.0)
    body = body_of(hass, entry.entry_id, requested_kwh=60.0, departure_enabled=True, departure_time="09:00")
    preview = (await async_plan_preview(hass, entry.entry_id, body, plan_only=False))["preview"]
    assert preview["plan"]["short_of_deadline"] is True
    assert [line["code"] for line in preview["status"]["lines"]] == ["departure_shortfall"]
    assert preview == await saved_preview(hass, entry.entry_id, body)


@freeze_time(NOW)
async def test_no_plan_is_worded_by_the_status_codes(hass: HomeAssistant) -> None:
    """Without a price area there is no plan: the status says what is missing, as the dashboard's does."""
    entry = await setup_charger(hass)
    body = body_of(hass, entry.entry_id, area_id=None)
    preview = (await async_plan_preview(hass, entry.entry_id, body, plan_only=False))["preview"]
    assert preview["plan"] is None and preview["strip"] is None
    assert preview["state"] == "incomplete_settings"
    assert [line["code"] for line in preview["status"]["lines"]] == ["settings_incomplete"]


def serve_autumn_change(transport: StubTransport) -> None:
    """Stockholm's 25-hour day (the clocks go back at 03:00 on 25 October 2026), and the day after."""
    transport.serve_area(SE4)
    for day, quarters in ((date(2026, 10, 25), 100), (date(2026, 10, 26), 96)):
        document = json.loads(day_body(SE4, day))
        # Dearest first, so the cheapest quarters are the last before the departure, after the repeated hour.
        document["prices"] = [round(0.30 - index * 0.001, 6) for index in range(quarters)]
        transport.serve(transport.day_path(SE4, day), 200, json.dumps(document))
    transport.serve("/v1/index.json", 200, index_listing(SE4, ["2026-10-25", "2026-10-26"]))


@freeze_time("2026-10-24 22:30:00")
async def test_the_preview_holds_across_the_autumn_change(hass: HomeAssistant, transport: StubTransport) -> None:
    """00:30 local on the 25-hour day, departure 06:00: the window holds the repeated hour, and the preview is still
    the plan a Save makes, quarter by quarter fifteen elapsed minutes apart."""
    serve_autumn_change(transport)
    entry = await setup_charger(hass)
    await go_auto(hass, requested_kwh=8.0)
    body = body_of(hass, entry.entry_id, departure_enabled=True, departure_time="06:00")
    preview = (await async_plan_preview(hass, entry.entry_id, body, plan_only=False))["preview"]
    assert preview["plan"] is not None
    starts = [datetime.fromisoformat(quarter["start"]) for quarter in preview["strip"]["quarters"]]
    steps = {(later - earlier).total_seconds() for earlier, later in zip(starts, starts[1:])}
    assert steps == {900.0}, "fifteen elapsed minutes, through the repeated hour"
    # 00:30 (+02:00) to 06:00 (+01:00) is six and a half hours: 26 quarters.
    assert len(starts) == 26
    assert preview == await saved_preview(hass, entry.entry_id, body)


# ------------------------------------------------------------------------------- nothing happens


async def assert_previews_change_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, entry_id: str, bodies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run a preview of each body and prove nothing moved: the store's record (the very same object), count,
    revision and suggestions, the controller (its appointments, its count in memory, the register's unread note and
    whom it asked), the executor, the session runner and the charger, the plan notice, the hybrid state and memory;
    no store write, no charger command, no event, no listener told. Returns the previews."""
    store = domain_data(hass).auto_store
    data = charger_data(hass, entry_id)
    controller = preview_for(hass, entry_id)
    assert store is not None and data is not None and controller is not None and data.executor is not None
    await hass.async_block_till_done()

    commits: list[str] = []
    real_commit = store._async_commit  # noqa: SLF001

    async def counted_commit(*args: Any, **kwargs: Any) -> Any:
        commits.append("commit")
        return await real_commit(*args, **kwargs)

    monkeypatch.setattr(store, "_async_commit", counted_commit)
    charger_calls = forbid_charger_writes(monkeypatch)
    events: list[str] = []
    remove_events = hass.bus.async_listen(MATCH_ALL, lambda event: events.append(event.event_type))
    heard: list[Any] = []
    remove_listener = controller.add_listener(heard.append)
    heard.clear()  # the listener is told the current snapshot once when it joins

    def state() -> dict[str, Any]:
        return {
            "settings": store.settings(entry_id),
            "baseline": store.energy_baseline(entry_id),
            "stored": store._entries[entry_id],  # noqa: SLF001
            "suggested": store._entries[entry_id].suggested,  # noqa: SLF001
            "controller": dict(vars(controller)),
            "executor": dict(vars(data.executor)),
            "runner": dict(vars(data.executor._runner)),  # noqa: SLF001
            "charger": dict(vars(data.controller)),
            "plan": data.controller.plan,
            "notice": None if data.notifier is None else data.notifier.plan_notice,
            "hybrid": (data.hybrid_state, data.hybrid_memory),
        }

    before = state()
    previews = []
    for body in bodies:
        answer = await async_plan_preview(hass, entry_id, body, plan_only=False)
        assert answer["ok"] is True, answer
        previews.append(answer["preview"])
    await hass.async_block_till_done()
    remove_listener()
    remove_events()
    monkeypatch.setattr(store, "_async_commit", real_commit)

    after = state()
    for key, value in before.items():
        assert after[key] == value, f"a preview moved {key}"
    assert after["stored"] is before["stored"], "the store's record is the very same"
    assert commits == [], "nothing was written to the store"
    assert charger_calls == [], "nothing reached the charger"
    assert events == [], "no event was fired"
    assert heard == [], "no listener heard of a preview"
    return previews


@freeze_time(NOW)
async def test_a_burst_of_previews_changes_nothing(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass, requested_kwh=6.0, departure_enabled=True, departure=datetime(2026, 1, 1, 18, 0).time())
    bodies = [
        body_of(
            hass, entry.entry_id, requested_kwh=kwh, departure_time=departure, strategy=strategy,
            departure_date="2026-09-23" if departure == "07:00" else None,
        )
        for kwh in (2.0, 5.5, 9.0, 14.0, 30.0, 60.0)
        for departure in ("07:00", "09:00", "16:15", "23:45")
        for strategy in ("cheapest", "hybrid")
    ]
    await assert_previews_change_nothing(hass, monkeypatch, entry.entry_id, bodies)


async def test_previews_leave_an_energy_registers_count_alone(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual need counted on the charger's register, unread since a restart: the count, the register's unread
    note and the appointment that says so at the end of its grace stay as the real calculation left them."""
    from .test_register_unread_notice import _charged_and_restarted

    with freeze_time(NOW) as frozen:
        car = await _charged_and_restarted(hass, transport, frozen)
        controller = car.preview
        assert controller._register_unread_since is not None  # noqa: SLF001
        assert controller._unread_notice_cancel is not None, "the grace's appointment is armed"  # noqa: SLF001
        assert car.baseline() is not None
        entry_id = car.charger.entry_id
        bodies = [
            body_of(hass, entry_id, requested_kwh=kwh, departure_time=departure)
            for kwh in (4.0, 8.0, 12.0)
            # The stored departure (the same count) and another one (a count of its own).
            for departure in ("20:00", "06:30")
        ]
        previews = await assert_previews_change_nothing(hass, monkeypatch, entry_id, bodies)
        assert all(preview["plan"] is not None for preview in previews)


async def test_previews_of_a_target_waiting_for_the_cars_level_do_not_ask_the_car(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target whose need a charge already delivered on a stale reading waits for the car's new level. Previews say
    so and ask the car nothing; the real calculation after them asks it, once per charge."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from .test_vehicle_update_wait import _charged

    refreshes = async_mock_service(hass, "homeassistant", "update_entity")
    with freeze_time(NOW) as frozen:
        world = await _charged(hass, transport, frozen)
        await hass.async_block_till_done()
        # The charge's end already planned again and asked the car once; forget that, as for a charge not yet asked
        # about, so a preview that asked would show.
        assert world["preview"].snapshot().reason == "waiting_for_vehicle_update"
        world["preview"]._update_asked_for = None  # noqa: SLF001
        asked = len(refreshes)
        entry_id = world["charger"].entry_id
        bodies = [body_of(hass, entry_id), body_of(hass, entry_id, amps=10), body_of(hass, entry_id, amps=12)]
        previews = await assert_previews_change_nothing(hass, monkeypatch, entry_id, bodies)
        assert [preview["reason"] for preview in previews] == ["waiting_for_vehicle_update"] * 3
        assert len(refreshes) == asked, "no preview asked the car"

        # The real calculation still asks, once per charge (`_wait_for_vehicle_update`).
        waiting = await world["preview"].async_recalculate()
        await hass.async_block_till_done()
        assert waiting.reason == "waiting_for_vehicle_update"
        assert world["preview"]._update_asked_for is not None, "the calculation asked the car"  # noqa: SLF001
        after = len(refreshes)
        await world["preview"].async_recalculate()
        await hass.async_block_till_done()
        assert len(refreshes) == after, "once per charge"


@freeze_time(NOW)
async def test_previews_past_the_published_prices_arm_no_wake(
    hass: HomeAssistant, transport: StubTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only today is published, and the departures run into tomorrow: the price wait decides, and its wake-up
    appointment stays as the real calculation left it."""
    from .relay import TODAY

    entry = await setup_charger(hass)
    serve(transport, days=(TODAY,), rising=True)
    await go_auto(hass, requested_kwh=6.0, departure_enabled=True, departure=datetime(2026, 1, 1, 7, 0).time())
    controller = preview_for(hass, entry.entry_id)
    assert controller is not None
    bodies = [
        body_of(hass, entry.entry_id, requested_kwh=kwh, departure_time=departure)
        for kwh in (3.0, 20.0, 45.0)
        for departure in ("06:00", "07:00", "18:00")
    ]
    previews = await assert_previews_change_nothing(hass, monkeypatch, entry.entry_id, bodies)
    assert {preview["state"] for preview in previews} - {"proposal_ready", "proposal_unpriced"}, (
        "some previews wait for the publication"
    )


@freeze_time(NOW)
async def test_a_real_hybrid_calculation_still_keeps_its_memory_and_a_preview_does_not(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass, requested_kwh=6.0, strategy="hybrid")
    data = charger_data(hass, entry.entry_id)
    assert data is not None and data.hybrid_memory is not None
    assert data.hybrid_memory.remaining_need_kwh == pytest.approx(6.0)
    kept = data.hybrid_memory
    await async_plan_preview(hass, entry.entry_id, body_of(hass, entry.entry_id, requested_kwh=9.0), plan_only=False)
    assert data.hybrid_memory is kept
    await saved_preview(hass, entry.entry_id, body_of(hass, entry.entry_id, requested_kwh=9.0))
    assert data.hybrid_memory is not kept and data.hybrid_memory.remaining_need_kwh == pytest.approx(9.0)


# ------------------------------------------------------------------------------- refusals


@freeze_time(NOW)
async def test_a_body_a_save_refuses_is_refused_with_the_saves_code(
    hass: HomeAssistant, transport: StubTransport
) -> None:
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass)
    for changes, code in (
        ({"amps": 99}, "invalid_amps"),
        ({"departure_date": "2026-09-01"}, "invalid_departure"),
        ({"requested_kwh": -1}, None),
        ({"strategy": "fastest"}, None),
    ):
        body = body_of(hass, entry.entry_id, **changes)
        with pytest.raises(AutoSettingsError) as refused:
            await async_update_settings(
                hass, entry.entry_id, expected_revision=settings_of(hass, entry.entry_id).revision, replacement=body
            )
        answer = await async_plan_preview(hass, entry.entry_id, body, plan_only=False)
        assert answer == {"api_version": 1, "ok": False, "error": refused.value.code, "preview": None}
        if code is not None:
            assert refused.value.code == code
    answer = await async_plan_preview(hass, entry.entry_id, {"not": "a body"}, plan_only=False)
    assert answer["ok"] is False and answer["error"]


async def test_the_socket_answers_and_refuses_as_save_does(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await setup_charger(hass)
    await go_auto(hass)
    socket = await admin(hass, hass_ws_client)
    answer = await ws_call(socket, preview_message(entry.entry_id, body_of(hass, entry.entry_id)))
    assert answer["success"] is True
    assert set(answer["result"]) == {"api_version", "ok", "error", "preview"}
    assert answer["result"]["ok"] is True

    wrong = await ws_call(socket, preview_message(entry.entry_id, body_of(hass, entry.entry_id), api_version=99))
    assert wrong["error"]["code"] == "spotnav_unsupported_api_version"
    unknown = await ws_call(socket, preview_message("nope", body_of(hass, entry.entry_id)))
    assert unknown["error"]["code"] == "spotnav_unknown_charger"


async def test_a_user_who_may_not_save_may_not_preview(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    from .world import non_admin

    entry = await setup_charger(hass)
    await go_auto(hass)
    socket = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    refused = await ws_call(socket, preview_message(entry.entry_id, body_of(hass, entry.entry_id)))
    assert refused["error"]["code"] == "spotnav_not_admin"


async def test_previews_are_limited_per_connection(
    hass: HomeAssistant, hass_ws_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    await go_auto(hass)
    clock = [1000.0]
    monkeypatch.setattr(plan_preview.time, "monotonic", lambda: clock[0])
    socket = await admin(hass, hass_ws_client)
    body = body_of(hass, entry.entry_id)
    for _ in range(plan_preview.RATE_PER_S):
        assert (await ws_call(socket, preview_message(entry.entry_id, body)))["success"] is True
    limited = await ws_call(socket, preview_message(entry.entry_id, body))
    assert limited["error"]["code"] == "spotnav_rate_limited"
    # Another connection has its own allowance.
    other = await admin(hass, hass_ws_client)
    assert (await ws_call(other, preview_message(entry.entry_id, body)))["success"] is True
    clock[0] += 1.0
    assert (await ws_call(socket, preview_message(entry.entry_id, body)))["success"] is True


async def test_a_body_beyond_the_cap_is_refused_unread(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await setup_charger(hass)
    await go_auto(hass)
    socket = await admin(hass, hass_ws_client)
    body = body_of(hass, entry.entry_id) | {"padding": "x" * plan_preview.MAX_BODY_BYTES}
    refused = await ws_call(socket, preview_message(entry.entry_id, body))
    assert refused["error"]["code"] == "spotnav_body_too_large"


# ------------------------------------------------------------------------------- the card's fixtures

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plan_preview"


@pytest.mark.parametrize(
    ("name", "changes"),
    [
        ("ready", {"requested_kwh": 11.5, "departure_enabled": True, "departure_time": "16:15"}),
        ("shortfall", {"requested_kwh": 60.0, "departure_enabled": True, "departure_time": "09:00"}),
        ("no_area", {"area_id": None}),
    ],
)
@freeze_time(NOW)
async def test_the_cards_fixtures_are_this_answer(
    hass: HomeAssistant, transport: StubTransport, name: str, changes: dict[str, Any]
) -> None:
    """The answers the card's tests decode (`frontend/test/plan-preview.test.ts`), written by this very code.

    Set `SPOTNAV_WRITE_FIXTURES=1` to (re)write them; the default run only compares.
    """
    entry = await setup_charger(hass)
    serve(transport, rising=True)
    await go_auto(hass, requested_kwh=6.0)
    answer = await async_plan_preview(hass, entry.entry_id, body_of(hass, entry.entry_id, **changes), plan_only=False)
    path = FIXTURES / f"{name}.json"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(answer, indent=2) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(answer))


# ------------------------------------------------------------------------------- a price entry and a user


async def test_a_user_who_may_plan_previews_what_their_save_makes_on_a_price_entry(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, freezer
) -> None:
    """With an electricity price entry the record carries its area and fees; a user under `users_change_plan` echoes
    them as read, as their Save does, and the preview is the plan that Save then makes."""
    from .test_price_writes import body_of as wire_body, on_one_price, read, two_chargers, write
    from .world import non_admin

    await two_chargers(hass)
    owner = await admin(hass, hass_ws_client)
    user = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    await on_one_price(hass, freezer)
    record = await read(owner)
    record = (await write(owner, record["revision"], wire_body(record, users_change_plan=True)))["result"]["settings"]

    body = wire_body(record, amps=16, requested_kwh=9.5)
    answer = await ws_call(user, preview_message("entry_a", body))
    assert answer["success"] is True, answer
    assert answer["result"]["ok"] is True, answer["result"]
    saved = await write(user, record["revision"], body)
    assert saved["success"] is True and saved["result"]["ok"] is True, saved
    await hass.async_block_till_done()
    controller = preview_for(hass, "entry_a")
    assert controller is not None
    assert answer["result"]["preview"] == json.loads(
        json.dumps(encode_preview(controller.snapshot(), settings_of(hass, "entry_a")))
    )
