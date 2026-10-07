"""Automatic charge periods as a setting: the record, its one-time migration, the two transports and the entity.

* The record: `max_periods` is `None` (automatic, the default) or a number 1 to 8, stored under `periods`. A
  record from before it (with `max_periods` and no `periods`) is read as automatic, once: the next save
  writes `periods`, and a number a person sets after that stays.
* The wire: `max_periods` is `null` for automatic. The webhook gives an app that does not read
  `auto_periods` the effective number (8) in its place, and that app's replacement echoing 8 keeps automatic.
* The entity: a select, `auto` or `1` to `8`; the number an older release created is removed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.api.settings import decode_settings, encode_settings, replacement_mutator
from custom_components.spotnav.api.webhook import APP_READS_AUTO_PERIODS
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.planning.auto_settings import (
    AutoSettings,
    AutoSettingsError,
    AutoSettingsStore,
)

from .messages import read_settings_message, update_settings_message
from .world import admin, call, entity_id, go_auto, settings_of, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")


def body(**changes: Any) -> dict[str, Any]:
    encoded = encode_settings(AutoSettings(area_id="SE4", amps=10, phases=3))
    return {key: value for key, value in encoded.items() if key != "revision"} | changes


# ------------------------------------------------------------------------------- the record


def test_automatic_is_the_default_and_round_trips() -> None:
    plain = AutoSettings()
    assert plain.max_periods is None
    stored = plain.validated().as_dict()
    assert stored["periods"] is None and "max_periods" not in stored
    assert AutoSettings.from_stored(stored).max_periods is None
    numbered = replace(plain, max_periods=3).validated()
    assert numbered.as_dict()["periods"] == 3
    assert AutoSettings.from_stored(numbered.as_dict()).max_periods == 3


def test_a_record_from_before_automatic_periods_is_read_as_automatic() -> None:
    """The one-time migration: an older release's number becomes automatic."""
    legacy = AutoSettings().validated().as_dict()
    del legacy["periods"]
    legacy["max_periods"] = 3
    assert AutoSettings.from_stored(legacy).max_periods is None
    # Once written again, the number a person chose stays.
    chosen = replace(AutoSettings.from_stored(legacy), max_periods=2).validated().as_dict()
    assert AutoSettings.from_stored(chosen).max_periods == 2


@pytest.mark.parametrize("value", [0, 9, True, "auto", 2.5])
def test_a_stored_count_out_of_range_is_refused(value: Any) -> None:
    raw = AutoSettings().validated().as_dict()
    raw["periods"] = value
    with pytest.raises(AutoSettingsError) as refused:
        AutoSettings.from_stored(raw)
    assert refused.value.code == "invalid_periods"


async def test_the_store_migrates_once_and_keeps_a_later_number(hass: HomeAssistant) -> None:
    class Memory:
        def __init__(self, document: dict[str, Any]) -> None:
            self.document = document

        async def async_load(self) -> dict[str, Any]:
            return self.document

        async def async_save(self, document: dict[str, Any]) -> None:
            self.document = document

    legacy = AutoSettings(revision=4, area_id="SE4", amps=10).validated().as_dict()
    del legacy["periods"]
    legacy["max_periods"] = 4
    memory = Memory({"schema": 1, "chargers": {"entry_a": {"settings": legacy, "proposal": None}}})
    store = AutoSettingsStore(hass, store=memory)  # type: ignore[arg-type]
    await store.async_load()
    assert store.settings("entry_a").max_periods is None
    await store.async_update("entry_a", mutate=lambda settings: replace(settings, max_periods=5))
    assert memory.document["chargers"]["entry_a"]["settings"]["periods"] == 5

    reloaded = AutoSettingsStore(hass, store=memory)  # type: ignore[arg-type]
    await reloaded.async_load()
    assert reloaded.settings("entry_a").max_periods == 5


# ------------------------------------------------------------------------------- the codec


def test_the_wire_says_null_for_automatic() -> None:
    assert encode_settings(AutoSettings())["max_periods"] is None
    assert decode_settings(body(max_periods=None)).max_periods is None
    assert decode_settings(body(max_periods=6)).max_periods == 6


@pytest.mark.parametrize("value", ["auto", 0, 9, 1.5, True])
def test_anything_else_is_refused_on_the_wire(value: Any) -> None:
    with pytest.raises(AutoSettingsError) as refused:
        decode_settings(body(max_periods=value))
    assert refused.value.code == "invalid_periods"


def test_an_older_apps_echo_of_the_effective_number_keeps_automatic() -> None:
    current = AutoSettings(area_id="SE4", amps=10)
    echoed = replacement_mutator(decode_settings(body(max_periods=8)), keep_auto_periods=True)(current)
    assert echoed.max_periods is None
    moved = replacement_mutator(decode_settings(body(max_periods=3)), keep_auto_periods=True)(current)
    assert moved.max_periods == 3
    # A client that reads automatic periods means 8 when it says 8.
    assert replacement_mutator(decode_settings(body(max_periods=8)))(current).max_periods == 8
    # A stored number is a number: an older app's 8 replaces it.
    numbered = replace(current, max_periods=2)
    assert replacement_mutator(decode_settings(body(max_periods=8)), keep_auto_periods=True)(numbered).max_periods == 8


# ------------------------------------------------------------------------- the two transports


async def test_the_card_sets_automatic_and_an_older_app_sees_the_number_and_keeps_it(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    from .helpers import webhook_dashboard

    entry = await setup_charger(hass)
    socket = await admin(hass, hass_ws_client)
    client = await hass_client_no_auth()
    await go_auto(hass, max_periods=2)

    written = await ws_call(
        socket, update_settings_message(entry.entry_id, settings_of(hass, entry.entry_id).revision, body(max_periods=None))
    )
    assert written["result"]["ok"] is True and written["result"]["settings"]["max_periods"] is None
    assert settings_of(hass, entry.entry_id).max_periods is None

    dashboard = await webhook_dashboard(client, "webhook-a")
    assert dashboard["settings"]["max_periods"] == 8

    app_body = {key: value for key, value in dashboard["settings"].items() if key not in ("revision", "fiscal_included")}
    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "settings", "expected_revision": dashboard["settings"]["revision"],
            "settings": app_body,
        },
    )
    answer = await response.json()
    assert response.status == 200 and answer["ok"] is True and answer["settings"]["max_periods"] == 8
    assert settings_of(hass, entry.entry_id).max_periods is None
    read = await ws_call(socket, read_settings_message(entry.entry_id))
    assert read["result"]["settings"]["max_periods"] is None

    # Opting in reads it, and writes it.
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": [APP_READS_AUTO_PERIODS]},
    )
    opted = await response.json()
    assert opted["settings"]["max_periods"] is None
    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "settings", "expected_revision": opted["settings"]["revision"],
            "settings": {**app_body, "max_periods": 8}, "reads": [APP_READS_AUTO_PERIODS],
        },
    )
    assert (await response.json())["settings"]["max_periods"] == 8
    assert settings_of(hass, entry.entry_id).max_periods == 8


# ------------------------------------------------------------------------------- the entity


async def test_the_select_offers_automatic_and_every_number(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    await go_auto(hass)
    periods = entity_id(hass, entry.entry_id, "charge_periods", "select")
    state = hass.states.get(periods)
    assert state.state == "auto"
    assert state.attributes["options"] == ["auto", "1", "2", "3", "4", "5", "6", "7", "8"]

    await call(hass, "select", "select_option", {"entity_id": periods, "option": "3"})
    assert settings_of(hass, entry.entry_id).max_periods == 3
    assert hass.states.get(periods).state == "3"
    await call(hass, "select", "select_option", {"entity_id": periods, "option": "auto"})
    assert settings_of(hass, entry.entry_id).max_periods is None


async def test_the_older_number_entity_is_removed(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("number", DOMAIN, "entry_a_maximum_periods")
    entry = await setup_charger(hass)
    assert registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_maximum_periods") is None
