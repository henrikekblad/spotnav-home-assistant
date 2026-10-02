"""The charger-scoped webhook: the settings action, the control actions, and its equivalence with the socket.

Every case goes through Home Assistant's real `/api/webhook/<id>` route with a real charger entry and
the reviewed offline relay seam. The settings envelope is checked separately from the HTTP status, and
both are checked against what the WebSocket command answers for identical state and input.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.planning.auto_controller import AutoPlannerController
from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED, AutoSettings
from custom_components.spotnav.execution.auto_execution import decide_axes
from custom_components.spotnav.api.common import ERROR_UNSUPPORTED_VERSION
from custom_components.spotnav.api.settings import SETTINGS_API_VERSION, encode_settings
from tests.helpers import as_app_sees, webhook_dashboard
from tests.relay import SE4
from tests.world import setup_charger
from tests.messages import (
    audit_privacy,
    break_persistence,
    forbid_charger_writes,
    read_settings_message,
    update_settings_message,
)
from .world import controller_of
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import executor_for

pytestmark = pytest.mark.usefixtures("offline_relay")

_message_ids = itertools.count(1000)


def _axes_of(control: dict[str, Any]) -> dict[str, Any]:
    """The two axes and their choices out of a dashboard `control` block."""
    keys = (
        "immediate_action",
        "immediate_action_reason",
        "automatic_action",
        "automatic_action_reason",
        "pause_choices",
    )
    return {key: control[key] for key in keys}


async def post(client, webhook_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """One webhook call: the HTTP status and the JSON body, separately."""
    response = await client.post(f"/api/webhook/{webhook_id}", json=payload)
    return response.status, await response.json()


async def test_the_retired_status_schedule_and_cancel_actions_are_unsupported(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """`dashboard` is the only read, and there is no schedule or cancel action any more."""
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    controller = controller_of(hass, entry.entry_id)
    before = controller.plan

    for action in ("status", "schedule", "cancel"):
        code, refused = await post(client, "webhook-a", {"version": 1, "action": action})
        assert (code, refused) == (400, {"ok": False, "error": "Unsupported action"}), action
    assert controller.plan is before


async def ws_call(client, message: dict[str, Any]) -> dict[str, Any]:
    message["id"] = next(_message_ids)
    await client.send_json(message)
    return await client.receive_json()


def stored(hass: HomeAssistant, entry_id: str = "entry_a") -> AutoSettings:
    store = domain_data(hass).auto_store
    assert store is not None
    return store.settings(entry_id)


def settings_payload(settings: AutoSettings, **changes: Any) -> dict[str, Any]:
    encoded = encode_settings(settings)
    body = {key: value for key, value in encoded.items() if key != "revision"}
    body.update(changes)
    return {"version": 1, "action": "settings", "expected_revision": settings.revision, "settings": body}


def _canonical(value: Any) -> str:
    """One record as a single comparable string, so two answers can be compared as values."""
    return json.dumps(value, sort_keys=True)


async def test_the_dashboard_settings_equal_the_websocket_read_value(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        entry.entry_id, mutate=lambda settings: replace(settings, area_id=SE4, amps=16, phases=3)
    )
    client = await hass_client_no_auth()
    socket = await hass_ws_client(hass)

    dashboard = await webhook_dashboard(client, "webhook-a")
    frame = await ws_call(socket, read_settings_message(entry.entry_id))

    assert frame["success"] is True
    assert dashboard["settings"] == as_app_sees(frame["result"]["settings"])
    assert "departure_date" in frame["result"]["settings"] and "departure_date" not in dashboard["settings"]
    assert dashboard["settings"]["revision"] == 1
    assert dashboard["control"]["pause"] == frame["result"]["pause"]


@pytest.mark.parametrize(
    "bad_version", [2, 5, "1", 1.5, True, False, [], {}],
    ids=["v2", "v5", "str1", "float", "true", "false", "list", "dict"],
)
async def test_settings_write_refuses_an_api_version_this_release_does_not_speak(
    hass: HomeAssistant, hass_client_no_auth, bad_version: Any
) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    before = stored(hass, entry.entry_id)

    status_code, answer = await post(
        client,
        "webhook-a",
        {**settings_payload(before, amps=16), "api_version": bad_version},
    )

    assert status_code == 400
    assert answer == {"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": "settings"}
    # Refused before anything was touched: same revision, same amps.
    assert stored(hass, entry.entry_id) == before


async def test_settings_write_accepts_the_one_version_and_no_negotiation_exists(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """`api_version: 1` may be stated, and leaving it out answers the identical envelope."""
    entry = await setup_charger(hass)
    second = await setup_charger(
        hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b"
    )
    client = await hass_client_no_auth()
    assert stored(hass, entry.entry_id) == stored(hass, second.entry_id)

    explicit_status, explicit = await post(
        client,
        "webhook-a",
        {**settings_payload(stored(hass, entry.entry_id), amps=16), "api_version": 1},
    )
    absent_status, absent = await post(
        client, "webhook-b", settings_payload(stored(hass, second.entry_id), amps=16)
    )

    assert explicit_status == absent_status == 200
    assert absent == explicit
    assert "settings_api_version" not in absent


async def test_a_valid_replacement_returns_the_success_envelope_and_subscribes(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    payload = settings_payload(stored(hass), area_id=SE4, amps=16, phases=3)

    status_code, answer = await post(client, "webhook-a", payload)

    assert status_code == 200
    assert answer["action"] == "settings"
    envelope = {key: value for key, value in answer.items() if key != "action"}
    assert envelope["api_version"] == SETTINGS_API_VERSION == 1
    assert envelope["ok"] is True and envelope["error"] is None
    committed = stored(hass)
    assert committed.revision == 1
    assert envelope["settings"] == as_app_sees(encode_settings(committed))
    assert committed.amps == 16
    # The real side effect: reaching Auto with an area subscribes to that area's prices.
    manager = domain_data(hass).price_refresh
    assert manager is not None and manager.area_snapshot(SE4) is not None


async def test_an_invalid_replacement_returns_400_with_the_current_record(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    before = stored(hass)
    payload = settings_payload(before, amps=16)
    payload["settings"]["amps"] = "16"

    status_code, answer = await post(client, "webhook-a", payload)

    assert status_code == 400
    assert answer["action"] == "settings"
    assert answer["api_version"] == SETTINGS_API_VERSION
    assert answer["ok"] is False and answer["error"] == "invalid_amps"
    assert answer["settings"] == as_app_sees(encode_settings(before))
    assert stored(hass) == before


async def test_a_retired_key_is_refused_by_name_and_nothing_is_committed(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The estimate switch and the vehicle keys are gone: a body that carries one is refused as an
    unknown field, the current record travels back, and nothing is committed."""
    await setup_charger(hass)
    client = await hass_client_no_auth()
    before = stored(hass)

    for retired in ("allow_estimated_prices", "execution_paused", "mode", "consumption_kwh_per_10km"):
        status_code, answer = await post(
            client, "webhook-a", settings_payload(before, **{retired: False})
        )
        assert status_code == 400, retired
        assert answer["ok"] is False and answer["error"] == "unknown_field", retired
        assert answer["settings"] == as_app_sees(encode_settings(before))
    assert stored(hass) == before, "nothing was committed"


async def test_a_stale_revision_returns_409_with_revision_conflict(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    current = await store.async_update(entry.entry_id, mutate=lambda s: replace(s, amps=10))
    client = await hass_client_no_auth()

    status_code, answer = await post(
        client, "webhook-a", settings_payload(current, amps=32) | {"expected_revision": 0}
    )

    assert status_code == 409
    assert answer["api_version"] == SETTINGS_API_VERSION
    assert answer["ok"] is False and answer["error"] == "revision_conflict"
    assert answer["settings"] == as_app_sees(encode_settings(current))
    assert stored(hass).amps == 10


async def test_a_persistence_failure_is_500_with_the_old_record(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    store = domain_data(hass).auto_store
    assert store is not None
    before = stored(hass)
    break_persistence(hass, monkeypatch)

    payload = settings_payload(before, amps=16)
    status_code, answer = await post(client, "webhook-a", payload)

    # Valid request, resolved charger, readable old record: our own persistence failed. 500 (not 400,
    # and not 502, which this handler uses for "the charger's own command failed").
    assert status_code == 500
    assert answer["action"] == "settings"
    assert answer["api_version"] == SETTINGS_API_VERSION
    assert answer["ok"] is False
    assert answer["error"] == "spotnav_settings_not_committed"
    assert answer["settings"] == as_app_sees(encode_settings(before))
    assert "disk said no" not in str(answer)
    assert stored(hass) == before
    assert stored(hass).revision == 0


async def test_a_post_commit_reconcile_failure_is_500_with_the_committed_record(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    before = stored(hass)

    async def failing_reconcile(_self, _committed):
        raise RuntimeError("the calculation exploded")

    monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_reconcile)
    status_code, answer = await post(client, "webhook-a", settings_payload(before, amps=16))

    assert status_code == 500
    assert answer["action"] == "settings"
    assert answer["api_version"] == SETTINGS_API_VERSION
    assert answer["ok"] is False
    assert answer["error"] == "spotnav_settings_reconcile_failed"
    # Committed, so the record that travels back is the *new* one; no rollback, no prose.
    committed = stored(hass)
    assert committed.revision == 1 and committed.amps == 16
    assert answer["settings"] == as_app_sees(encode_settings(committed))
    assert "exploded" not in str(answer)


async def test_a_foreign_charger_id_cannot_address_another_entry(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    first = await setup_charger(hass)
    second = await setup_charger(
        hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b"
    )
    client = await hass_client_no_auth()
    untouched = encode_settings(stored(hass, "entry_b"))

    payload = settings_payload(stored(hass, first.entry_id), amps=16)
    payload["charger_id"] = second.entry_id
    status_code, answer = await post(client, "webhook-a", payload)

    # The id is never read: the route stays bound to its own charger, and the other record is intact.
    assert status_code == 200 and answer["ok"] is True
    assert stored(hass, first.entry_id).amps == 16
    assert encode_settings(stored(hass, "entry_b")) == untouched


async def test_two_webhooks_mutate_only_their_own_records(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    first = await setup_charger(hass)
    second = await setup_charger(
        hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b"
    )
    client = await hass_client_no_auth()
    other = stored(hass, second.entry_id)

    first_status, first_answer = await post(
        client, "webhook-a", settings_payload(stored(hass, first.entry_id), amps=16)
    )
    second_status, second_answer = await post(
        client, "webhook-b", settings_payload(other, phases=3)
    )

    assert (first_status, second_status) == (200, 200)
    assert first_answer["ok"] is True and second_answer["ok"] is True
    mine, theirs = stored(hass, first.entry_id), stored(hass, second.entry_id)
    assert mine.revision == 1 and mine.amps == 16 and mine.phases == other.phases
    assert theirs.revision == 1 and theirs.phases == 3 and theirs.amps == other.amps
    assert [_canonical(first_answer["settings"]), _canonical(second_answer["settings"])] == [
        _canonical(as_app_sees(encode_settings(mine))),
        _canonical(as_app_sees(encode_settings(theirs))),
    ]


async def test_refused_paths_touch_no_charger_and_call_no_service(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    seen = forbid_charger_writes(monkeypatch)
    service_calls = (
        async_mock_service(hass, "switch", "turn_on")
        + async_mock_service(hass, "switch", "turn_off")
        + async_mock_service(hass, "number", "set_value")
    )
    before = stored(hass)
    switch_before = hass.states.get("switch.charger_a").state
    body = settings_payload(before, amps=16)["settings"]

    answers = [
        # an invalid replacement, a stale revision, an unsupported payload version, an unknown
        # action, and a settings action with no fields at all
        await post(client, "webhook-a", {**settings_payload(before), "settings": {**body, "amps": "16"}}),
        await post(client, "webhook-a", {"version": 1, "action": "settings", "expected_revision": 7, "settings": body}),
        await post(client, "webhook-a", {"version": 99, "action": "dashboard", "api_version": 1}),
        await post(client, "webhook-a", {"version": 1, "action": "nonsense"}),
        await post(client, "webhook-a", {"version": 1, "action": "settings"}),
    ]

    # An unregistered route is Home Assistant's own refusal, before any of this integration runs, and
    # its answer is deliberately content-free: a bare 200 that says nothing about which ids exist.
    unknown = await client.post(
        "/api/webhook/webhook-missing",
        json={"version": 1, "action": "settings", "expected_revision": 0, "settings": body},
    )
    assert unknown.status == 200 and await unknown.text() == ""

    # The two version/action refusals keep their exact answers, text included.
    assert [answer["error"] for _, answer in (answers[2], answers[3])] == [
        "Unsupported payload version",
        "Unsupported action",
    ]

    statuses = [status for status, _ in answers]
    assert not [status for status in statuses if status == 200]
    assert set(statuses) <= {400, 404, 409}
    assert [answer["ok"] for _, answer in answers] == [False] * len(answers)
    # Nothing reached the charger, no service ran, and no record moved.
    assert seen == []
    assert service_calls == []
    assert hass.states.get("switch.charger_a").state == switch_before
    assert stored(hass) == before and stored(hass, entry.entry_id).revision == 0


async def test_no_webhook_answer_leaks_a_credential_record_or_price_document(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    client = await hass_client_no_auth()
    socket = await hass_ws_client(hass)
    before = stored(hass)
    body = settings_payload(before, amps=16)["settings"]

    answers = [
        await post(client, "webhook-a", settings_payload(before, amps=16)),
        await post(client, "webhook-a", {**settings_payload(before), "settings": {**body, "amps": "16"}}),
        await post(client, "webhook-a", {"version": 1, "action": "settings", "expected_revision": 7, "settings": body}),
        await post(client, "webhook-a", settings_payload(before, area_id="SE9")),
    ]
    # The two failure modes that answer with a record while something went wrong server-side.
    with pytest.MonkeyPatch.context() as persistence:
        break_persistence(hass, persistence)
        answers.append(await post(client, "webhook-a", settings_payload(stored(hass), amps=18)))

    async def failing_reconcile(_self, _committed):
        raise RuntimeError("the calculation exploded")

    monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_reconcile)
    answers.append(await post(client, "webhook-a", settings_payload(stored(hass), amps=20)))

    for status, answer in answers:
        assert 200 <= status < 600
        audit_privacy(answer, "webhook")
        blob = _canonical(answer)
        assert "webhook-a" not in blob
        assert "webhook_id" not in blob and "storage" not in blob
        assert "Traceback" not in blob and "RuntimeError" not in blob
        assert "disk said no" not in blob and "exploded" not in blob
        # No charger entity id travels either.
        assert "switch.charger" not in _canonical(answer)


EXPECTED_HTTP = {
    "success": 200,
    "invalid": 400,
    "conflict": 409,
    "persistence_failure": 500,
    "reconcile_failure": 500,
}

#: What the revision must be afterwards: a refusal writes nothing, a commit advances it once, and a
#: committed-but-unreconciled write still advanced it (the reconcile failure is not a rollback).
EXPECTED_REVISION = {
    "success": 1,
    "invalid": 0,
    "conflict": 1,
    "persistence_failure": 0,
    "reconcile_failure": 1,
}


async def equip_transport_pair(hass: HomeAssistant) -> tuple[Any, Any]:
    """Two entries in identical state: one driven over the socket, one over its webhook."""
    socket_entry = await setup_charger(hass)
    hook_entry = await setup_charger(
        hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b"
    )
    return socket_entry, hook_entry


@pytest.mark.parametrize("case", sorted(EXPECTED_HTTP))
async def test_websocket_and_webhook_agree_on_identical_state_and_input(
    hass: HomeAssistant,
    hass_ws_client,
    hass_client_no_auth,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    socket_entry, hook_entry = await equip_transport_pair(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    socket = await hass_ws_client(hass)
    client = await hass_client_no_auth()

    # Identical stored state on both entries, and identical inputs below.
    if case == "conflict":
        for entry_id in (socket_entry.entry_id, hook_entry.entry_id):
            await store.async_update(entry_id, mutate=lambda s: replace(s, amps=10))
    if case == "persistence_failure":
        break_persistence(hass, monkeypatch)
    if case == "reconcile_failure":

        async def failing_reconcile(_self, _committed):
            raise RuntimeError("the calculation exploded")

        monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_reconcile)

    current = stored(hass, socket_entry.entry_id)
    assert _canonical(encode_settings(stored(hass, hook_entry.entry_id))) == _canonical(
        encode_settings(current)
    )
    body = settings_payload(current, amps=16)["settings"]
    if case == "invalid":
        body = {**body, "amps": "16"}
    revision = 0 if case != "conflict" else current.revision - 1

    frame = await ws_call(socket, update_settings_message(socket_entry.entry_id, revision, body))
    status, answer = await post(
        client,
        "webhook-b",
        {"version": 1, "action": "settings", "expected_revision": revision, "settings": body},
    )

    # The socket frame succeeded and carries the contract envelope; the webhook adds only its routing
    # field. Strip the transport's own wrapper and HTTP status and the two answers are the same value.
    assert frame["success"] is True
    socket_envelope = frame["result"]
    hook_envelope = {key: value for key, value in answer.items() if key != "action"}
    # The one documented difference: the webhook leaves out what the app cannot read yet.
    assert "departure_date" in socket_envelope["settings"] and "departure_date" not in hook_envelope["settings"]
    assert hook_envelope == {**socket_envelope, "settings": as_app_sees(socket_envelope["settings"])}
    assert socket_envelope["api_version"] == SETTINGS_API_VERSION == 1
    assert status == EXPECTED_HTTP[case]
    assert socket_envelope["ok"] is (case == "success")
    # Identical inputs against identical state left the two records identical too.
    assert _canonical(encode_settings(stored(hass, socket_entry.entry_id))) == _canonical(
        encode_settings(stored(hass, hook_entry.entry_id))
    )
    assert stored(hass, socket_entry.entry_id).revision == EXPECTED_REVISION[case]


async def test_the_plain_actions_keep_their_own_behaviour(hass: HomeAssistant, hass_client_no_auth) -> None:
    await setup_charger(hass)
    client = await hass_client_no_auth()

    stopped = await post(client, "webhook-a", {"version": 1, "action": "stop"})
    unknown_version = await post(
        client, "webhook-a", {"version": 99, "action": "dashboard", "api_version": 1}
    )
    unknown_action = await post(client, "webhook-a", {"version": 1, "action": "nonsense"})

    # A plain action, an unsupported payload version and an unknown action answer their own way --
    # and none of them carries a settings envelope.
    assert stopped[0] == 200 and stopped[1]["ok"] is True and stopped[1]["action"] == "stop"
    assert unknown_version == (400, {"ok": False, "error": "Unsupported payload version"})
    assert unknown_action == (400, {"ok": False, "error": "Unsupported action"})
    for answer in (stopped[1], unknown_version[1], unknown_action[1]):
        assert "settings_api_version" not in answer
        assert "settings" not in answer


# --- the typed pause and the resume, through the boundary the socket's actions use ----------


async def _auto_charging(hass: HomeAssistant, entry_id: str = "entry_a") -> None:
    """One already-set-up charger whose record is Auto's and which is charging right now."""
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        entry_id,
        mutate=lambda settings: replace(settings, area_id=SE4, amps=16, phases=3),
    )
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()


async def test_a_stop_with_a_typed_choice_is_a_pause_and_the_dashboard_offers_the_resume(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    client = await hass_client_no_auth()

    # The status states the two axes: this charger is charging and Auto owns it, so the immediate
    # command is a Stop -- while the *automatic* axis offers its own pause, with the choices a pause
    # would accept. The typed choice is what makes a Stop a pause rather than an immediate command.
    status_before = await webhook_dashboard(client, "webhook-a")
    assert status_before["control"]["immediate_action"] == "stop"
    assert status_before["control"]["automatic_action"] == "pause"
    assert PAUSE_UNTIL_RESUMED in status_before["control"]["pause_choices"]
    before = stored(hass, entry.entry_id)

    _, answer = await post(
        client, "webhook-a", {"version": 1, "action": "stop", "choice": PAUSE_UNTIL_RESUMED}
    )

    assert answer == {"ok": True, "action": "stop"}
    record = stored(hass, entry.entry_id)
    # Exactly one typed intent, stored as the record's own pause -- one write.
    assert record.pause.choice == PAUSE_UNTIL_RESUMED
    assert record.pause.expires_at is None
    assert record.revision == before.revision + 1

    # The pause is now in force, so the automatic axis offers the resume -- and the immediate axis still
    # reads the charger itself, which the pause stopped (its own report, set here): `start` is offered
    # beside the Resume.
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    after = await webhook_dashboard(client, "webhook-a")
    assert _axes_of(after["control"]) == {
        "immediate_action": "start",
        "immediate_action_reason": None,
        "automatic_action": "resume",
        "automatic_action_reason": None,
        "pause_choices": [],
    }


async def test_an_immediate_stop_stores_no_pause_at_all(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    client = await hass_client_no_auth()
    stopped = async_mock_service(hass, "switch", "turn_off")
    before = stored(hass, entry.entry_id)

    _, answer = await post(client, "webhook-a", {"version": 1, "action": "stop"})

    # The immediate Stop is what it always was: the charger is stopped once and nothing is suspended,
    # so Auto may take the charge over again at its next authoritative event. Nothing is stored either:
    # a stop is not a statement about who plans, so the record is untouched.
    assert answer == {"ok": True, "action": "stop"}
    assert len(stopped) == 1
    record = stored(hass, entry.entry_id)
    assert record.pause.admitted is False
    assert record.revision == before.revision


async def test_resume_clears_the_pause_once(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    client = await hass_client_no_auth()
    stopped = async_mock_service(hass, "switch", "turn_off")

    await post(client, "webhook-a", {"version": 1, "action": "stop", "choice": PAUSE_UNTIL_RESUMED})
    paused = stored(hass, entry.entry_id)
    assert paused.pause.admitted is True

    _, answer = await post(client, "webhook-a", {"version": 1, "action": "resume"})

    assert answer == {"ok": True, "action": "resume"}
    resumed = stored(hass, entry.entry_id)
    assert resumed.pause.admitted is False
    # One write for the pause and one more for the resume, and no physical command beyond the pause's own
    # semantics: nothing Auto installed was applied, so a pause had nothing of its own to stop.
    assert resumed.revision == paused.revision + 1
    assert stopped == []

    # And the axes are back to the charger's own state -- charging, so immediate `stop`, with the
    # automatic pause offered beside it again now that no pause is stored.
    after = await webhook_dashboard(client, "webhook-a")
    assert after["control"]["immediate_action"] == "stop"
    assert after["control"]["automatic_action"] == "pause"


async def test_a_start_and_an_immediate_stop_change_neither_settings_nor_pause(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    before = stored(hass, entry.entry_id)

    # The two immediate actions change the charger and nothing else: not the pause, not a single field
    # of the record. An immediate action can therefore never be the thing that pauses a charger
    # without saying so.
    started = await post(client, "webhook-a", {"version": 1, "action": "start", "amps": 16})
    stopped = await post(client, "webhook-a", {"version": 1, "action": "stop"})
    await hass.async_block_till_done()

    assert started == (200, {"ok": True, "action": "start"})
    assert stopped == (200, {"ok": True, "action": "stop"})
    after = stored(hass, entry.entry_id)
    assert after == before
    assert after.pause.admitted is False
    # And the record's own revision never moved: neither action wrote anything at all.
    assert after.revision == before.revision


async def test_a_pause_the_boundary_refuses_changes_nothing(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    client = await hass_client_no_auth()
    service_calls = (
        async_mock_service(hass, "switch", "turn_on")
        + async_mock_service(hass, "switch", "turn_off")
    )
    before = stored(hass, entry.entry_id)

    # `next_period` needs a period of a *running* plan still ahead, and nothing is installed here: the
    # boundary refuses by its own code rather than resolving the choice to something else.
    status, answer = await post(
        client, "webhook-a", {"version": 1, "action": "stop", "choice": "next_period"}
    )

    assert status == 409 and answer == {"ok": False, "error": "invalid_pause"}
    assert stored(hass, entry.entry_id) == before
    assert service_calls == []

# --- the two axes in one `control` --------------------------------------------------------------


async def test_one_dashboard_response_reads_the_boundary_never_and_answers_one_moment(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two axes are answers about **one** capture, and this is the test that holds that true.

    The dashboard is captured once and then serialized; a serializer that reached for the boundary's
    live facts would publish a payload whose immediate half described one moment and whose automatic
    half described another -- a state that never existed. A poisoned live seam proves nothing reads it.
    """
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    executor = executor_for(hass, entry.entry_id)
    assert executor is not None
    first = executor.control_facts()

    def poisoned() -> Any:
        raise AssertionError("a live read of the boundary happened while serializing")

    monkeypatch.setattr(executor, "control_axes", poisoned)
    monkeypatch.setattr(executor, "immediate_decision", poisoned)
    monkeypatch.setattr(executor, "automatic_decision", poisoned)
    client = await hass_client_no_auth()
    dashboard = await webhook_dashboard(client, "webhook-a")

    axes = decide_axes(first)
    assert _axes_of(dashboard["control"]) == {
        "immediate_action": axes.immediate.action,
        "immediate_action_reason": axes.immediate.reason,
        "automatic_action": axes.automatic.action,
        "automatic_action_reason": axes.automatic.reason,
        "pause_choices": list(axes.automatic.choices),
    }
    assert (axes.immediate.action, axes.automatic.action) == ("stop", "pause")


async def test_an_idle_auto_charger_offers_start_now_and_pause_at_once(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The defect this split was raised for: an idle Auto charger must still offer Pause.

    A plan for later tonight is exactly what somebody wants to pause, and physical charging is not a
    precondition for storing that intent. So the immediate axis reads the charger (idle, so `start`)
    while the automatic axis reads the record (`pause`, with the choices a pause could be taken with,
    `until_resumed` among them). v13's single `primary_action` could only answer `start` here, which is
    why the installed app showed Start and no Pause.
    """
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    assert stored(hass, entry.entry_id).pause.admitted is False

    status = await webhook_dashboard(client, "webhook-a")

    assert status["api_version"] == 1
    assert _axes_of(status["control"]) == {
        "immediate_action": "start",
        "immediate_action_reason": None,
        "automatic_action": "pause",
        "automatic_action_reason": None,
        "pause_choices": [PAUSE_UNTIL_RESUMED],
    }


async def test_a_paused_idle_charger_offers_start_now_and_resume_at_once(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """While Auto is paused, Start now is still a truthful thing to offer -- and Resume is the other.

    One press of Start changes the charger and leaves the pause exactly where it is, so the two axes
    answer different questions about the same moment and neither is folded into the other.
    """
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    client = await hass_client_no_auth()
    await post(client, "webhook-a", {"version": 1, "action": "stop", "choice": PAUSE_UNTIL_RESUMED})
    # The charger's own report after the pause: whatever stopped it, it is not charging now.
    hass.states.async_set("switch.charger_a", "off")
    await hass.async_block_till_done()
    assert stored(hass, entry.entry_id).pause.admitted is True

    status = await webhook_dashboard(client, "webhook-a")

    assert _axes_of(status["control"]) == {
        "immediate_action": "start",
        "immediate_action_reason": None,
        "automatic_action": "resume",
        "automatic_action_reason": None,
        "pause_choices": [],
    }


async def test_a_charging_auto_charger_offers_both_axes_with_the_pause_choices_beside_the_pause(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Charging with no pause: `stop` and `pause` describe the same moment and mean different things.

    The immediate axis offers the command (stop the charger, planning untouched); the automatic axis
    offers Auto's own pause, with the choices it can honour now. Choices belong to that pause alone:
    neither an immediate `stop`, a `resume`, a `none` nor a `start` carries any.
    """
    entry = await setup_charger(hass)
    await _auto_charging(hass)
    client = await hass_client_no_auth()

    status = await webhook_dashboard(client, "webhook-a")

    control = status["control"]
    assert control["immediate_action"] == "stop" and control["immediate_action_reason"] is None
    assert control["automatic_action"] == "pause" and control["automatic_action_reason"] is None
    assert control["pause_choices"] == [PAUSE_UNTIL_RESUMED]
