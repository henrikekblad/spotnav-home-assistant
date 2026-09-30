"""Cross-language contract fixtures for the settings envelope (`tests/fixtures/settings/v1/`).

The card's Settings popover and the app speak this one contract, and fixtures written by
hand are how a contract drifts, so every fixture here is the real `spotnav/update_settings`
command's own JSON output for the shapes a client must tell apart -- a successful write, a stale
revision refused as `revision_conflict`, an invalid body refused by the store's own validation, and a
body that still carries a retired key refused as `unknown_field` -- never a hand-built payload.

Set `SPOTNAV_WRITE_FIXTURES=1` to (re)write the committed files; the default run only compares, so a
payload change cannot slip past this test without the fixture moving with it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import AutoSettings
from tests.world import setup_charger
from tests.messages import update_settings_message
from tests.world import admin, ws_call
from tests.test_strategy_schema import body_of
from custom_components.spotnav.runtime import domain_data

pytestmark = pytest.mark.usefixtures("offline_relay")

SETTINGS_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "settings" / "v1"


async def _fresh(hass: HomeAssistant, entry_id: str = "entry_a") -> AutoSettings:
    """The same charger's settings, reset to a clean installation's defaults.

    A fixture is a state, and a state must not depend on the order the fixtures are built in.
    """
    store = domain_data(hass).auto_store
    assert store is not None
    return await store.async_update(entry_id, mutate=lambda _current: AutoSettings())


async def _state_success(hass: HomeAssistant, entry: Any, client: Any) -> dict[str, Any]:
    """A valid write at the current revision: the committed record, with the strategy applied."""
    before = await _fresh(hass, entry.entry_id)
    frame = await ws_call(
        client,
        update_settings_message(
            entry.entry_id,
            before.revision,
            body_of(before, area_id="SE4", amps=16, phases=3, strategy="solar"),
        ),
    )
    assert frame["success"] is True
    assert frame["result"]["ok"] is True
    return frame["result"]


async def _state_revision_conflict(hass: HomeAssistant, entry: Any, client: Any) -> dict[str, Any]:
    """A stale revision: refused, with the *current* record traveling back."""
    before = await _fresh(hass, entry.entry_id)
    frame = await ws_call(
        client,
        update_settings_message(entry.entry_id, before.revision + 1, body_of(before, amps=16)),
    )
    assert frame["success"] is True
    assert frame["result"]["ok"] is False and frame["result"]["error"] == "revision_conflict"
    return frame["result"]


async def _state_refusal_invalid_strategy(
    hass: HomeAssistant, entry: Any, client: Any
) -> dict[str, Any]:
    """An invalid body: a strategy outside the vocabulary, refused by the store."""
    before = await _fresh(hass, entry.entry_id)
    frame = await ws_call(
        client,
        update_settings_message(
            entry.entry_id, before.revision, body_of(before, strategy="not_a_real_strategy")
        ),
    )
    assert frame["success"] is True
    assert frame["result"]["ok"] is False and frame["result"]["error"] == "invalid_strategy"
    return frame["result"]


async def _state_refusal_unknown_field(
    hass: HomeAssistant, entry: Any, client: Any
) -> dict[str, Any]:
    """A body that still carries a retired key: refused by name, and the record that stands travels
    back unchanged."""
    before = await _fresh(hass, entry.entry_id)
    frame = await ws_call(
        client,
        update_settings_message(
            entry.entry_id, before.revision, {**body_of(before), "allow_estimated_prices": False}
        ),
    )
    assert frame["success"] is True
    result = frame["result"]
    assert result["ok"] is False and result["error"] == "unknown_field"
    assert result["settings"]["revision"] == before.revision, "nothing was committed"
    return result


Builder = Callable[[HomeAssistant, Any, Any], Awaitable[dict[str, Any]]]

#: The backend-owned settings fixtures the card and the app decode.
SETTINGS_FIXTURES: Final[dict[str, Builder]] = {
    "success.json": _state_success,
    "revision_conflict.json": _state_revision_conflict,
    "refusal_invalid_strategy.json": _state_refusal_invalid_strategy,
    "refusal_unknown_field.json": _state_refusal_unknown_field,
}


async def test_the_committed_settings_fixtures_are_the_commands_own_output(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """Every settings fixture equals what the real command produces, or this test fails."""
    SETTINGS_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    entry = await setup_charger(hass)
    client = await admin(hass, hass_ws_client)

    produced: dict[str, Any] = {}
    for name, build in SETTINGS_FIXTURES.items():
        produced[name] = await build(hass, entry, client)

    write = os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1"
    if write:
        for name, payload in produced.items():
            (SETTINGS_FIXTURE_DIR / name).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return

    committed = sorted(path.name for path in SETTINGS_FIXTURE_DIR.glob("*.json"))
    assert committed == sorted(SETTINGS_FIXTURES)
    for name, payload in produced.items():
        stored_fixture = json.loads((SETTINGS_FIXTURE_DIR / name).read_text(encoding="utf-8"))
        assert stored_fixture == payload, f"{name} no longer matches the command's own output"
