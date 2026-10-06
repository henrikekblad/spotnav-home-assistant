"""Adversarial review of the camera branch: failing tests prove defects."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.vehicles import camera_identification as ci
from custom_components.spotnav.runtime import charger_data

from . import test_camera_identification as base
from .test_camera_identification import FakeCamera, Garage, picture
from .test_camera_identification import garage as garage  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures("offline_relay")


class Bedroom(FakeCamera):
    _attr_name = "Bedroom"
    _attr_unique_id = "camera_bedroom"

    def __init__(self) -> None:
        super().__init__()
        self.entity_id = "camera.bedroom"
        self.picture = picture((10, 200, 10))


async def test_the_app_webhook_cannot_read_a_camera_that_is_not_the_chargers(
    garage: Garage, hass: HomeAssistant, hass_client_no_auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The webhook id is a bearer credential bound to one charger (and reachable from outside the home,
    local_only=False). It must not hand out a snapshot of any other camera in the house."""
    real = base.setup_test_component_platform

    def with_bedroom(hass: HomeAssistant, domain: str, entities: list[Any], **kw: Any) -> Any:
        if domain == "camera":
            entities = [*entities, Bedroom()]
        return real(hass, domain, entities, **kw)

    monkeypatch.setattr(base, "setup_test_component_platform", with_bedroom)
    await garage.start(references=False)
    assert hass.states.get("camera.bedroom") is not None
    client = await hass_client_no_auth()
    response = await client.post(
        "/api/webhook/webhook-a",
        json={"version": 1, "action": "camera_snapshot", "camera_entity_id": "camera.bedroom"},
    )
    body = await response.json()
    assert response.status == 400, f"the bedroom camera's picture went out over the charger's webhook: {body.get('picture', {}).get('width')}"


async def test_a_query_cancelled_while_the_crop_is_written_leaves_no_picture_behind(
    garage: Garage, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unplug (session closed) or the 30 s timeout cancelling the query while the crop is being written:
    the executor finishes writing, but `temporary` is never bound, so the finally never unlinks it."""
    await garage.start()
    camera = charger_data(hass, garage.entry_id).camera
    written = threading.Event()
    real = ci._write_temporary

    def slow_write(folder: Any, data: bytes) -> Any:
        path = real(folder, data)
        written.set()
        time.sleep(0.3)
        return path

    monkeypatch.setattr(ci, "_write_temporary", slow_write)
    task = hass.async_create_task(camera.async_ask(list(garage.world.cars.values())))
    await hass.async_add_executor_job(written.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_add_executor_job(time.sleep, 0.4)
    folder = camera.references.folder / "tmp"
    left = await hass.async_add_executor_job(lambda: list(folder.glob("*")))
    for item in left:
        item.unlink()
    assert left == [], f"crop left in storage: {left}"


async def test_a_crop_left_by_a_crash_is_swept_at_start(garage: Garage, hass: HomeAssistant) -> None:
    """A hard stop mid-query (power cut, OOM) leaves the crop; nothing sweeps tmp/ when the charger loads."""
    await garage.start()
    camera = charger_data(hass, garage.entry_id).camera
    folder = camera.references.folder / "tmp"
    await hass.async_add_executor_job(lambda: (folder.mkdir(parents=True, exist_ok=True), (folder / "query_dead.jpg").write_bytes(b"x")))
    await hass.config_entries.async_reload(garage.entry_id)
    await hass.async_block_till_done()
    left = await hass.async_add_executor_job(lambda: list(folder.glob("*")))
    for item in left:
        item.unlink()
    assert left == [], f"crop from a crashed query still stored: {left}"
