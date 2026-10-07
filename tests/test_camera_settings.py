"""A charger's camera for vehicle identification, as a settings field: `identify_camera`.

`null` (the default: no camera) or `{camera_entity_id, ai_task_entity_id, frame}`. Optional on the wire, withheld
from the paired app until it asks (`reads`), kept by a replacement that leaves it out, refused as
`invalid_camera`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api.settings import (
    decode_settings,
    encode_settings,
    OPTIONAL_SETTINGS_KEYS,
    replacement_mutator,
)
from custom_components.spotnav.api.webhook import APP_UNREAD_SETTINGS
from custom_components.spotnav.planning.auto_settings import AutoSettings, AutoSettingsError
from custom_components.spotnav.vehicles.camera_settings import CameraSettings, Frame

from .messages import update_settings_message
from .world import admin, settings_of, setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

CAMERA = {"camera_entity_id": "camera.norr", "ai_task_entity_id": "ai_task.ollama", "frame": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.5}}


def body(**changes: Any) -> dict[str, Any]:
    encoded = encode_settings(AutoSettings(area_id="SE4", amps=10, phases=3))
    return {key: value for key, value in encoded.items() if key != "revision"} | changes


def test_no_camera_is_the_default_and_is_not_stored() -> None:
    assert AutoSettings().identify_camera is None
    assert "identify_camera" not in AutoSettings().as_dict()
    assert encode_settings(AutoSettings())["identify_camera"] is None
    assert "identify_camera" in OPTIONAL_SETTINGS_KEYS and "identify_camera" in APP_UNREAD_SETTINGS


def test_a_camera_is_stored_and_read_back() -> None:
    chosen = replace(AutoSettings(area_id="SE4"), identify_camera=CameraSettings.from_wire(CAMERA)).validated()
    stored = chosen.as_dict()
    assert stored["identify_camera"] == CAMERA
    assert AutoSettings.from_stored(stored) == chosen
    assert encode_settings(chosen)["identify_camera"] == CAMERA


def test_an_unreadable_stored_camera_loses_only_the_camera() -> None:
    stored = AutoSettings(area_id="SE4", amps=12).as_dict() | {"identify_camera": {"camera_entity_id": 3}}
    read = AutoSettings.from_stored(stored)
    assert read.identify_camera is None and read.amps == 12


@pytest.mark.parametrize(
    "value",
    [
        {**CAMERA, "camera_entity_id": "sensor.norr"},
        {**CAMERA, "ai_task_entity_id": ""},
        {**CAMERA, "frame": {"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}},
        {"camera_entity_id": "camera.norr"},
        "camera.norr",
    ],
)
def test_the_wire_refuses_a_camera_that_is_not_one(value: Any) -> None:
    with pytest.raises(AutoSettingsError) as error:
        decode_settings(body(identify_camera=value))
    assert error.value.code == "invalid_camera"


def test_a_replacement_that_leaves_it_out_keeps_it_and_null_takes_it_away() -> None:
    current = replace(AutoSettings(area_id="SE4", amps=10, phases=3), identify_camera=CameraSettings.from_wire(CAMERA))
    sent = body(amps=16)
    sent.pop("identify_camera")
    kept = replacement_mutator(decode_settings(sent), keep_identify_camera=True)(current).validated()
    assert kept.amps == 16 and kept.identify_camera == current.identify_camera
    cleared = replacement_mutator(decode_settings(body(identify_camera=None)))(current).validated()
    assert cleared.identify_camera is None
    assert decode_settings(body(identify_camera=CAMERA)).identify_camera == CameraSettings(
        "camera.norr", "ai_task.ollama", Frame(0.1, 0.2, 0.4, 0.5)
    )


async def test_the_card_writes_it_and_an_older_app_neither_sees_nor_clears_it(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    entry = await setup_charger(hass)
    socket = await admin(hass, hass_ws_client)
    client = await hass_client_no_auth()
    written = await ws_call(
        socket,
        update_settings_message(entry.entry_id, settings_of(hass, entry.entry_id).revision, body(identify_camera=CAMERA)),
    )
    assert written["result"]["ok"] is True
    assert written["result"]["settings"]["identify_camera"] == CAMERA

    app_body = body(amps=16)
    app_body.pop("identify_camera")
    response = await client.post(
        "/api/webhook/webhook-a",
        json={
            "version": 1, "action": "settings",
            "expected_revision": settings_of(hass, entry.entry_id).revision, "settings": app_body,
        },
    )
    answer = await response.json()
    assert response.status == 200 and "identify_camera" not in answer["settings"]
    assert settings_of(hass, entry.entry_id).identify_camera == CameraSettings.from_wire(CAMERA)

    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "dashboard", "api_version": 1, "reads": ["identify_camera"]},
    )
    assert (await response.json())["settings"]["identify_camera"] == CAMERA
