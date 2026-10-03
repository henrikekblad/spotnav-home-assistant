"""The two settings WebSocket commands, over Home Assistant's real authenticated socket.

Permissions are exercised by the real `require_admin` boundary and the real auth handshake -- no
handler is called directly for a permission claim. Answers are checked in two layers: the transport
frame (`success` / `result` / HA error) and, separately, the settings envelope inside `result`.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.spotnav.planning.auto_controller import AutoPlannerController
from custom_components.spotnav.planning.auto_settings import AutoSettings
from custom_components.spotnav.api.common import ERROR_UNSUPPORTED_VERSION
from custom_components.spotnav.api.settings import (
    SETTINGS_API_VERSION,
    SETTINGS_KEYS,
    SETTINGS_RESPONSE_KEYS,
    encode_settings as _encode_settings,
)
from tests.helpers import make_site_entry
from tests.world import setup_charger
from .world import admin, non_admin, ws_call
from .messages import (
    audit_privacy,
    break_persistence,
    forbid_charger_writes,
    read_settings_message,
    stored,
    update_settings_message,
)
from custom_components.spotnav.runtime import domain_data

def encode_settings(settings, phases=3):
    """The wire record as it reads for a charger wired for three phases (the phases a charge uses are
    the server's to fill in, not a stored setting)."""
    return _encode_settings(settings, phases)


pytestmark = pytest.mark.usefixtures("offline_relay")

CURRENT_LIMIT = "number.charger_a_limit"


#: Home Assistant refuses a second message on one socket that reuses an id, so every call gets its
#: own -- the id is not part of this contract, it is part of the socket's.
_message_ids = itertools.count(1)


def body_of(settings: AutoSettings, **changes: Any) -> dict[str, Any]:
    """A full replacement body: the canonical value without its revision, with changes applied."""
    encoded = encode_settings(settings)
    # `revision` is named apart and `fiscal_included` is read-only: neither is a body key.
    body = {key: value for key, value in encoded.items() if key not in ("revision", "fiscal_included")}
    body.update(changes)
    assert set(body) == SETTINGS_KEYS
    return body


async def test_a_non_admin_reads_the_canonical_defaults_at_revision_zero(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    entry = await setup_charger(hass)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    message = read_settings_message(entry.entry_id)
    frame = await ws_call(client, message)

    # The transport frame first, then the contract value inside it.
    assert frame["success"] is True and frame["id"] == message["id"]
    envelope = frame["result"]
    assert set(envelope) == {"api_version", "ok", "error", "settings", "pause"}
    # The settings contract's own version, as a literal.
    assert envelope["api_version"] == SETTINGS_API_VERSION == 1
    assert envelope["ok"] is True and envelope["error"] is None
    assert envelope["settings"] == encode_settings(AutoSettings())
    assert envelope["pause"] == {"choice": None, "admitted_at": None, "expires_at": None}
    assert envelope["settings"]["revision"] == 0
    assert set(envelope["settings"]) == SETTINGS_RESPONSE_KEYS


async def test_a_later_read_returns_the_exact_stored_record(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    committed = await store.async_update(
        entry.entry_id,
        mutate=lambda settings: replace(settings, area_id="SE4", amps=16, phases=3),
    )
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    frame = await ws_call(client, read_settings_message(entry.entry_id))
    assert frame["success"] is True
    assert frame["result"]["settings"] == encode_settings(committed)
    assert frame["result"]["settings"]["revision"] == committed.revision == 1


async def test_an_admin_full_replacement_commits_once_and_returns_the_envelope(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    body = body_of(stored(hass), area_id="SE4", amps=16, phases=3)

    message = update_settings_message(entry.entry_id, 0, body)
    frame = await ws_call(client, message)

    assert frame["success"] is True and frame["id"] == message["id"]
    envelope = frame["result"]
    assert envelope["api_version"] == SETTINGS_API_VERSION == 1
    assert envelope["ok"] is True and envelope["error"] is None
    assert envelope["settings"] == encode_settings(stored(hass))
    # One increment, and the replacement's own values -- a full replacement, not a patch.
    assert envelope["settings"]["revision"] == 1
    assert envelope["settings"]["amps"] == 16 and envelope["settings"]["phases"] == 3
    assert envelope["settings"]["strategy"] == "cheapest"


async def test_a_non_admin_update_is_refused_by_the_admin_boundary_and_changes_nothing(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    entry = await setup_charger(hass)
    before = stored(hass)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    message = update_settings_message(entry.entry_id, 0, body_of(before, amps=16))
    frame = await ws_call(client, message)

    # Home Assistant's own boundary answers, not the handler: an error frame with its stable code.
    assert frame["success"] is False and frame["id"] == message["id"]
    assert frame["error"]["code"] == "unauthorized"
    assert "result" not in frame
    assert stored(hass) == before


async def test_an_unsupported_version_is_refused_by_both_commands(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    before = stored(hass)
    client = await admin(hass, hass_ws_client)

    for message in (
        # Any version but 1 is refused, and so is a boolean that merely equals it.
        read_settings_message(entry.entry_id, api_version=5),
        update_settings_message(entry.entry_id, 0, body_of(before), api_version=2),
        read_settings_message(entry.entry_id, api_version="1"),
        read_settings_message(entry.entry_id, api_version=True),
        # A request that does not state a version has not asked this contract anything.
        {"id": 4, "type": "spotnav/get_settings", "charger_id": entry.entry_id},
    ):
        frame = await ws_call(client, message)
        assert frame["success"] is False, message
        assert frame["error"]["code"] == ERROR_UNSUPPORTED_VERSION
        assert "result" not in frame
    assert stored(hass) == before


async def test_entry_refusals_are_stable_and_mutate_no_charger(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger = await setup_charger(hass)
    unloaded = await setup_charger(
        hass, entry_id="entry_c", webhook_id="webhook-c", charge_control="switch.charger_c"
    )
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=[charger.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    foreign = MockConfigEntry(domain="other_component", data={})
    foreign.add_to_hass(hass)
    assert await hass.config_entries.async_unload(unloaded.entry_id)
    await hass.async_block_till_done()

    before = stored(hass)
    client = await admin(hass, hass_ws_client)
    cases = [
        ("entry_missing", "spotnav_unknown_charger"),
        (foreign.entry_id, "spotnav_unknown_charger"),
        (site.entry_id, "spotnav_site_not_charger"),
        (unloaded.entry_id, "spotnav_charger_unloaded"),
    ]
    for entry_id, code in cases:
        read = await ws_call(client, read_settings_message(entry_id))
        assert read["success"] is False, entry_id
        assert read["error"]["code"] == code
        assert "result" not in read

        update = await ws_call(client, update_settings_message(entry_id, 0, body_of(before, amps=16)))
        assert update["success"] is False, entry_id
        assert update["error"]["code"] == code

    assert stored(hass) == before, "no refused id mutated anything"
    assert stored(hass, unloaded.entry_id).revision == 0


async def test_an_invalid_replacement_returns_the_envelope_and_writes_nothing(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    before = stored(hass)
    client = await admin(hass, hass_ws_client)
    broken = body_of(before, amps=16)
    broken["amps"] = "16"  # a string posing as a number

    frame = await ws_call(client, update_settings_message(entry.entry_id, 0, broken))

    assert frame["success"] is True, frame  # a settings refusal is a contract value
    envelope = frame["result"]
    assert envelope["api_version"] == SETTINGS_API_VERSION
    assert envelope["ok"] is False and envelope["error"] == "invalid_amps"
    assert envelope["settings"] == encode_settings(before)
    assert stored(hass) == before

    missing = body_of(before, amps=16)
    del missing["max_periods"]
    frame = await ws_call(client, update_settings_message(entry.entry_id, 0, missing))
    assert "result" in frame, frame
    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == "missing_field"
    assert stored(hass) == before


async def test_a_stale_revision_returns_the_conflict_envelope_with_the_current_record(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    current = await store.async_update(entry.entry_id, mutate=lambda s: replace(s, amps=10))
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(client, update_settings_message(entry.entry_id, 0, body_of(current, amps=32)))

    assert frame["success"] is True
    envelope = frame["result"]
    assert envelope["api_version"] == SETTINGS_API_VERSION
    assert envelope["ok"] is False and envelope["error"] == "revision_conflict"
    # The current record travels back, so a client can retry against what is actually stored.
    assert envelope["settings"] == encode_settings(current)
    assert envelope["settings"]["revision"] == 1
    assert stored(hass).amps == 10


async def test_a_post_commit_reconcile_failure_returns_the_committed_record(
    hass: HomeAssistant, hass_ws_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    before = stored(hass)

    async def failing_reconcile(_self, _committed):
        raise RuntimeError("the calculation exploded")

    monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_reconcile)
    frame = await ws_call(client, update_settings_message(entry.entry_id, 0, body_of(before, amps=16)))

    assert frame["success"] is True
    envelope = frame["result"]
    assert envelope["api_version"] == SETTINGS_API_VERSION
    assert envelope["ok"] is False
    assert envelope["error"] == "spotnav_settings_reconcile_failed"
    # Committed, so the record that travels back is the new one -- not prose, and not a rollback.
    committed = stored(hass)
    assert committed.revision == 1 and committed.amps == 16
    assert envelope["settings"] == encode_settings(committed)
    assert "exploded" not in str(frame)


async def test_two_chargers_stay_isolated_through_the_socket(
    hass: HomeAssistant, hass_ws_client
) -> None:
    first = await setup_charger(hass)
    second = await setup_charger(
        hass, entry_id="entry_b", webhook_id="webhook-b", charge_control="switch.charger_b"
    )
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(
        client, update_settings_message(first.entry_id, 0, body_of(stored(hass), amps=16, phases=3))
    )
    assert frame["result"]["ok"] is True
    assert stored(hass, first.entry_id).amps == 16
    assert stored(hass, second.entry_id).revision == 0
    assert stored(hass, second.entry_id).amps is None
    assert encode_settings(stored(hass, second.entry_id)) == encode_settings(AutoSettings())

    frame = await ws_call(client, read_settings_message(second.entry_id))
    assert "result" in frame, frame
    assert frame["result"]["settings"] == encode_settings(AutoSettings())


async def test_refused_paths_touch_no_charger_and_call_no_service(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    unloaded = await setup_charger(
        hass, entry_id="entry_c", webhook_id="webhook-c", charge_control="switch.charger_c"
    )
    site = make_site_entry(hass, entry_id="site_a", charger_entry_ids=[entry.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    assert await hass.config_entries.async_unload(unloaded.entry_id)
    await hass.async_block_till_done()

    charger_calls = forbid_charger_writes(monkeypatch)
    switch_on = async_mock_service(hass, "switch", "turn_on")
    switch_off = async_mock_service(hass, "switch", "turn_off")
    number_set = async_mock_service(hass, "number", "set_value")
    homeassistant_on = async_mock_service(hass, "homeassistant", "turn_on")
    homeassistant_off = async_mock_service(hass, "homeassistant", "turn_off")

    before = stored(hass)
    store = domain_data(hass).auto_store
    assert store is not None
    current = await store.async_update(entry.entry_id, mutate=lambda s: replace(s, amps=10))

    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    writer = await admin(hass, hass_ws_client)
    broken = body_of(current, amps=16)
    broken["amps"] = "16"

    # Every refused route: unauthorized, unsupported version, each entry code, invalid body, stale CAS.
    await ws_call(reader, update_settings_message(entry.entry_id, current.revision, body_of(current, amps=16)))
    await ws_call(writer, update_settings_message(entry.entry_id, 2, body_of(current, amps=16)))
    await ws_call(writer, update_settings_message("entry_missing", 0, body_of(current, amps=16)))
    await ws_call(writer, update_settings_message(site.entry_id, 0, body_of(current, amps=16)))
    await ws_call(writer, update_settings_message(unloaded.entry_id, 0, body_of(current, amps=16)))
    await ws_call(writer, update_settings_message(entry.entry_id, current.revision, broken))
    await ws_call(writer, update_settings_message(entry.entry_id, 0, body_of(current, amps=32)))

    assert charger_calls == []
    assert switch_on == [] and switch_off == [] and number_set == []
    assert homeassistant_on == [] and homeassistant_off == []
    assert stored(hass, entry.entry_id) == current
    assert stored(hass, entry.entry_id).amps == 10
    assert stored(hass).revision == before.revision + 1

#: Material that must never appear anywhere in a settings answer: charger-side identifiers, the
#: webhook that authorizes them, storage keys, credentials, exception prose and price documents.
BANNED_TEXT = (
    "webhook-a",
    "webhook-b",
    "webhook-c",
    "switch.charger_a",
    "switch.charger_b",
    "switch.charger_c",
    "number.charger_a_limit",
    "charging_current",
    "spotnav_auto_settings",
    "store_key",
    "access_token",
    "Bearer",
    "Traceback",
    "exploded",
)
BANNED_KEYS = frozenset(
    {
        "intervals",
        "interval_count",
        "effective_price",
        "raw_price",
        "fallback_price",
        "resolutions_minutes",
        "provenance",
    }
)


async def test_no_answer_leaks_credentials_entity_ids_or_price_documents(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass, current_limit=CURRENT_LIMIT)
    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    writer = await admin(hass, hass_ws_client)
    frames = [
        await ws_call(reader, read_settings_message(entry.entry_id)),
        await ws_call(writer, update_settings_message(entry.entry_id, 0, body_of(stored(hass), amps=16))),
        # A conflict, an invalid body and an unsupported version are answers too.
        await ws_call(writer, update_settings_message(entry.entry_id, 0, body_of(stored(hass), amps=32))),
        await ws_call(writer, update_settings_message(entry.entry_id, 1, {**body_of(stored(hass)), "amps": "x"})),
        await ws_call(writer, read_settings_message(entry.entry_id, api_version=99)),
        await ws_call(writer, update_settings_message("entry_missing", 0, body_of(stored(hass), amps=16))),
    ]

    async def failing_reconcile(_self, _committed):
        raise RuntimeError("the calculation exploded")

    monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_reconcile)
    frames.append(
        await ws_call(writer, update_settings_message(entry.entry_id, 1, body_of(stored(hass), amps=16)))
    )

    for frame in frames:
        audit_privacy(frame, "frame")
    # And the settings value really is the settings value: the fourteen public keys, nothing else.
    envelope = frames[0]["result"]
    assert set(envelope["settings"]) == SETTINGS_RESPONSE_KEYS


async def test_the_settings_answer_carries_the_settings_version(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """The one drift this contract must never have: another contract's version in a settings envelope."""
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    frames = [
        await ws_call(client, read_settings_message(entry.entry_id)),
        await ws_call(client, update_settings_message(entry.entry_id, 0, body_of(stored(hass), amps=16))),
        await ws_call(client, update_settings_message(entry.entry_id, 0, body_of(stored(hass), amps=32))),
    ]
    for frame in frames:
        if frame["success"] is True:
            assert frame["result"]["api_version"] == SETTINGS_API_VERSION == 1, frame


async def test_a_persistence_failure_is_the_envelope_in_a_successful_frame(
    hass: HomeAssistant, hass_ws_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    before = stored(hass)
    break_persistence(hass, monkeypatch)

    frame = await ws_call(client, update_settings_message(entry.entry_id, 0, body_of(before, amps=16)))

    # A valid request whose write never reached storage is an operational failure, so it is answered
    # as a contract envelope in a *successful* frame -- not as a WebSocket error frame.
    assert frame["success"] is True
    envelope = frame["result"]
    assert envelope["api_version"] == SETTINGS_API_VERSION
    assert envelope["ok"] is False
    assert envelope["error"] == "spotnav_settings_not_committed"
    # Nothing was written, so the record that travels back is the old current one.
    assert envelope["settings"] == encode_settings(before)
    assert stored(hass) == before and stored(hass).revision == 0
    assert "disk said no" not in str(frame)
