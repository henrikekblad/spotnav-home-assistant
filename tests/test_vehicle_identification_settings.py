"""Which car is plugged in: the settings a charger keeps for it.

* `vehicle_ids`: the vehicles that can charge at this charger, `None` for every detected vehicle (the
  default); a list has at least one and no repeats.
* `identify_mode`: `automatic` (the default), `ask` or `off`. With one vehicle nothing is identified.
* Both are optional on the wire and withheld from the paired app (`APP_UNREAD_SETTINGS`); a
  replacement that leaves them out keeps them. The target a car keeps is the car's own property
  (`test_vehicle_targets.py`), not a field of this record.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.settings import (
    OPTIONAL_SETTINGS_KEYS,
    decode_settings,
    encode_settings,
    replacement_mutator,
)
from custom_components.spotnav.api.webhook import APP_UNREAD_SETTINGS
from custom_components.spotnav.planning.auto_settings import (
    AutoSettings,
    AutoSettingsError,
    IDENTIFY_AUTOMATIC,
    IDENTIFY_MODES,
    TargetSocIntent,
)

from .messages import read_settings_message, update_settings_message
from .world import admin, settings_of, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

NEW_KEYS = ("vehicle_ids", "identify_mode")


def body(**changes: Any) -> dict[str, Any]:
    encoded = encode_settings(AutoSettings(area_id="SE4", amps=10, phases=3))
    return {key: value for key, value in encoded.items() if key != "revision"} | changes


def legacy_body(**changes: Any) -> dict[str, Any]:
    """What an app that does not know the two fields sends."""
    sent = body(**changes)
    for key in NEW_KEYS:
        sent.pop(key, None)
    return sent


# ------------------------------------------------------------------------------- the record


def test_the_defaults_are_every_vehicle_and_automatic() -> None:
    plain = AutoSettings()
    assert plain.vehicle_ids is None
    assert plain.identify_mode == IDENTIFY_AUTOMATIC == "automatic"
    assert IDENTIFY_MODES == ("automatic", "ask", "off")
    assert not hasattr(plain, "vehicle_targets"), "a car's target is the car's own"
    stored = plain.as_dict()
    for key in NEW_KEYS:
        assert key not in stored, "additive: an untouched record is stored as before"


def test_both_fields_are_stored_once_set_and_read_back() -> None:
    chosen = replace(AutoSettings(area_id="SE4", amps=10), vehicle_ids=("car-b", "car-a"), identify_mode="ask").validated()
    assert chosen.vehicle_ids == ("car-a", "car-b"), "kept in a stable order"
    stored = chosen.as_dict()
    assert stored["vehicle_ids"] == ["car-a", "car-b"]
    assert stored["identify_mode"] == "ask"
    assert AutoSettings.from_stored(stored) == chosen


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"vehicle_ids": ()}, "invalid_vehicles"),
        ({"vehicle_ids": ("car-a", "car-a")}, "invalid_vehicles"),
        ({"vehicle_ids": ("",)}, "invalid_vehicles"),
        ({"vehicle_ids": (3,)}, "invalid_vehicles"),
        ({"identify_mode": "sometimes"}, "invalid_vehicles"),
        ({"identify_mode": None}, "invalid_vehicles"),
    ],
)
def test_validation_refuses_by_name(changes: dict[str, Any], code: str) -> None:
    with pytest.raises(AutoSettingsError) as error:
        replace(AutoSettings(), **changes).validated()
    assert error.value.code == code


# ------------------------------------------------------------------------------- the codec


def test_the_wire_names_both_and_marks_them_optional() -> None:
    for key in NEW_KEYS:
        assert key in OPTIONAL_SETTINGS_KEYS
    encoded = encode_settings(AutoSettings())
    assert encoded["vehicle_ids"] is None
    assert encoded["identify_mode"] == "automatic"
    assert "vehicle_targets" not in encoded
    decoded = decode_settings(body(vehicle_ids=["car-a", "car-b"], identify_mode="off"))
    assert decoded.vehicle_ids == ("car-a", "car-b")
    assert decoded.identify_mode == "off"
    plain = decode_settings(legacy_body())
    assert plain.vehicle_ids is None and plain.identify_mode == "automatic"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"vehicle_ids": []}, "invalid_vehicles"),
        ({"vehicle_ids": "car-a"}, "invalid_vehicles"),
        ({"vehicle_ids": ["car-a", None]}, "invalid_vehicles"),
        ({"identify_mode": "Automatic"}, "invalid_vehicles"),
        ({"identify_mode": 1}, "invalid_vehicles"),
    ],
)
def test_the_wire_refuses_by_name(changes: dict[str, Any], code: str) -> None:
    with pytest.raises(AutoSettingsError) as error:
        decode_settings(body(**changes))
    assert error.value.code == code


def test_a_replacement_that_leaves_them_out_keeps_them() -> None:
    current = replace(
        AutoSettings(area_id="SE4", amps=10, phases=3), vehicle_ids=("car-a", "car-b"), identify_mode="ask"
    ).validated()
    kept = replacement_mutator(decode_settings(legacy_body(amps=16)), keep_identification=True)(current).validated()
    assert kept.amps == 16
    assert kept.vehicle_ids == ("car-a", "car-b") and kept.identify_mode == "ask"


# ------------------------------------------------------------------------- the two transports


def test_the_app_does_not_read_them_yet() -> None:
    for key in NEW_KEYS:
        assert key in APP_UNREAD_SETTINGS


async def test_the_card_writes_them_and_an_older_app_neither_sees_nor_clears_them(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    from .helpers import webhook_dashboard
    from .world import setup_charger

    entry = await setup_charger(hass)
    socket = await admin(hass, hass_ws_client)
    client = await hass_client_no_auth()
    before = settings_of(hass, entry.entry_id)

    written = await ws_call(
        socket,
        update_settings_message(
            entry.entry_id,
            before.revision,
            body(
                vehicle_ids=["car-a", "car-b"],
                identify_mode="ask",
                driver="target_soc",
                target={"vehicle_id": "car-a", "target_percent": 80},
            ),
        ),
    )
    result = written["result"]
    assert result["ok"] is True
    assert result["settings"]["vehicle_ids"] == ["car-a", "car-b"]
    assert result["settings"]["identify_mode"] == "ask"

    dashboard = await webhook_dashboard(client, "webhook-a")
    for key in NEW_KEYS:
        assert key not in dashboard["settings"]
    app_body = legacy_body(amps=16, driver="target_soc", target={"vehicle_id": "car-b", "target_percent": 80})
    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "settings",
            "expected_revision": settings_of(hass, entry.entry_id).revision, "settings": app_body,
        },
    )
    answer = await response.json()
    assert response.status == 200 and answer["ok"] is True
    for key in NEW_KEYS:
        assert key not in answer["settings"]
    stored = settings_of(hass, entry.entry_id)
    assert stored.vehicle_ids == ("car-a", "car-b") and stored.identify_mode == "ask"
    read = await ws_call(socket, read_settings_message(entry.entry_id))
    assert read["result"]["settings"]["identify_mode"] == "ask"

    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": list(NEW_KEYS)},
    )
    opted = (await response.json())["settings"]
    assert opted["identify_mode"] == "ask" and opted["vehicle_ids"] == ["car-a", "car-b"]
