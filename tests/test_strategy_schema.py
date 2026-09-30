"""The stored `strategy` field, end to end: storage, the settings contract and the WebSocket command.

A charger runs one strategy -- `cheapest`, `solar` or `hybrid` -- stored on its settings record and
written by the one settings contract (`spotnav/update_settings`, a full replacement that names the
strategy). This module pins:

* the vocabulary, which storage and the wire share exactly (`STORED_STRATEGIES`): the two can never
  drift, because a record storage could hold that no write could produce (or the reverse) would be a
  second source of truth;
* the round trip of every strategy through the pure codec, and its permanent refusal of any other
  spelling (`invalid_strategy`, nothing written);
* the same over the real settings WebSocket command, including that a refused spelling writes nothing;
* that a replacement carries the stored pause through, whatever strategy it writes.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import (
    STORED_STRATEGIES,
    STRATEGY_CHEAPEST,
    AutoSettings,
    AutoSettingsError,
    PauseIntent,
)
from custom_components.spotnav.api.settings import (
    SETTINGS_API_VERSION,
    SETTINGS_KEYS,
    decode_settings,
    encode_settings,
    replacement_mutator,
    settings_envelope,
    strategy_of,
)
from tests.world import setup_charger
from tests.messages import read_settings_message, stored, update_settings_message
from tests.world import admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

SOLAR = "solar"
HYBRID = "hybrid"


def body_of(settings: AutoSettings, **changes: Any) -> dict[str, Any]:
    """A replacement body: the canonical value without its revision, changes applied."""
    encoded = encode_settings(settings)
    body = {key: value for key, value in encoded.items() if key != "revision"}
    body.update(changes)
    assert set(body) == SETTINGS_KEYS
    return body


def test_storage_and_the_wire_share_one_strategy_vocabulary() -> None:
    assert STORED_STRATEGIES == (STRATEGY_CHEAPEST, SOLAR, HYBRID)
    assert strategy_of(AutoSettings()) == STRATEGY_CHEAPEST


def test_every_strategy_round_trips_and_any_other_spelling_is_refused() -> None:
    settings = AutoSettings(area_id="SE4", amps=10, phases=3)
    body = body_of(settings)
    assert body["strategy"] == STRATEGY_CHEAPEST

    for strategy in STORED_STRATEGIES:
        decoded = decode_settings(body_of(settings, strategy=strategy))
        assert decoded.strategy == strategy
        assert encode_settings(decoded)["strategy"] == strategy
        assert settings_envelope(decoded)["settings"]["strategy"] == strategy

    for bad in ("smart", "", None, 1):
        with pytest.raises(AutoSettingsError) as refusal:
            decode_settings(body_of(settings, strategy=bad))
        assert refusal.value.code == "invalid_strategy"


def test_a_replacement_writes_the_strategy_and_keeps_the_stored_pause() -> None:
    pause = PauseIntent(choice="until_resumed")
    current = replace(AutoSettings(area_id="SE4", amps=10, phases=3, revision=2), pause=pause)
    replacement = decode_settings(body_of(current, strategy=SOLAR))

    committed = replacement_mutator(replacement)(current)

    assert committed.strategy == SOLAR, "the decoded value reached the store"
    assert committed.pause == pause, "and the pause is not the body's to touch"


async def test_the_settings_command_round_trips_every_strategy(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """The real WebSocket command: read, then write each strategy in turn, then a refusal."""
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)
    before = stored(hass)

    read = await ws_call(client, read_settings_message(entry.entry_id))
    assert read["success"] is True
    assert read["result"] == settings_envelope(before)
    assert read["result"]["settings"]["strategy"] == STRATEGY_CHEAPEST
    assert read["result"]["api_version"] == SETTINGS_API_VERSION == 1

    body = body_of(before, amps=16, phases=1, area_id="SE4", departure_enabled=False)
    frame = await ws_call(client, update_settings_message(entry.entry_id, before.revision, body))
    assert frame["result"]["ok"] is True
    current = stored(hass)
    assert current.amps == 16 and current.strategy == STRATEGY_CHEAPEST
    assert current.revision == before.revision + 1

    for strategy in (SOLAR, HYBRID):
        frame = await ws_call(
            client,
            update_settings_message(entry.entry_id, current.revision, body_of(current, strategy=strategy)),
        )
        assert frame["result"]["ok"] is True
        after = stored(hass)
        assert after.strategy == strategy
        assert after.revision == current.revision + 1
        current = after

    # An unknown spelling has no stored representation at all.
    frame = await ws_call(
        client,
        update_settings_message(entry.entry_id, current.revision, body_of(current, strategy="smart")),
    )
    assert frame["result"]["ok"] is False
    assert frame["result"]["error"] == "invalid_strategy"
    assert stored(hass) == current, "nothing was written"
