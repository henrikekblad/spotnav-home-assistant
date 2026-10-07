"""Re-review of the camera fixes (8726c30): failing tests prove what is left."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.runtime import charger_data
from custom_components.spotnav.vehicles import camera_identification as ci
from custom_components.spotnav.vehicles.camera_pictures import colour_signature
from custom_components.spotnav.vehicles.identification import METHOD_CAMERA

from .test_camera_identification import RED, WHITE, Garage, picture  # noqa: F401
from .test_camera_identification import garage as garage  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_a_sure_answer_the_crops_colour_contradicts_does_not_decide(garage: Garage) -> None:
    """Daylight, a white car parks, the model says the red car with high confidence. The crop's own colour
    (already computed for the night rule) plainly matches the other car's reference, yet the camera decides."""
    await garage.start()
    garage.model.answer = "car_1"  # Kia, the red car
    garage.car_parks(WHITE)
    await garage.world.plug_in()
    await garage.settle()
    assert garage.world.identifier.method != METHOD_CAMERA, "decided the red car for a white crop"


async def test_a_full_picture_frame_is_the_same_as_no_frame(garage: Garage) -> None:
    """References taken with no frame (`null`, the whole picture) turn stale when the frame is saved as the
    whole picture, though every crop is the same."""
    await garage.start(references=False)
    assert (await garage.ws("save_camera_frame", frame=None))["ok"] is True
    kia = garage.world.cars["Kia"]
    garage.car_parks(RED)
    assert (await garage.ws("take_reference_picture", vehicle_id=kia, kind="day"))["ok"] is True
    saved = await garage.ws("save_camera_frame", frame={"x": 0, "y": 0, "w": 1, "h": 1})
    assert saved["ok"] is True
    refs = charger_data(garage.hass, garage.entry_id).camera.wire_references(kia)
    assert len(refs) == 1, "the same crop: the picture still compares"


async def test_a_reference_write_cut_short_leaves_no_partial_picture(garage: Garage, hass: HomeAssistant) -> None:
    """`_write` now uses `<car>_<kind>.<hex>.tmp`; a crash between write and replace leaves it in the references
    folder, and the load sweeps only `tmp/`."""
    await garage.start()
    camera = charger_data(hass, garage.entry_id).camera
    stray = camera.references.folder / "abc_day.0123456789ab.tmp"
    await hass.async_add_executor_job(stray.write_bytes, b"x")
    await hass.config_entries.async_reload(garage.entry_id)
    await hass.async_block_till_done()
    exists = await hass.async_add_executor_job(stray.exists)
    if exists:
        await hass.async_add_executor_job(stray.unlink)
    assert not exists, "a half-written reference picture stays until the charger is removed"


async def test_a_second_cancel_while_the_crop_is_removed_leaves_it(
    garage: Garage, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout cancels during the write; the unplug cancels again while the finally awaits the write."""
    await garage.start()
    camera = charger_data(hass, garage.entry_id).camera
    started = threading.Event()
    real = ci._write_temporary

    def slow_write(path: Any, data: bytes) -> Any:
        started.set()
        time.sleep(0.3)
        return real(path, data)

    monkeypatch.setattr(ci, "_write_temporary", slow_write)
    task = hass.async_create_task(camera.async_ask(list(garage.world.cars.values())))
    await hass.async_add_executor_job(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_add_executor_job(time.sleep, 1.5)  # the crop and the two references' crops are written
    folder = camera.references.folder / "tmp"
    left = await hass.async_add_executor_job(lambda: list(folder.glob("*")) if folder.exists() else [])
    for item in left:
        item.unlink()
    assert left == [], f"crop left in storage: {left}"


def test_a_tinted_infrared_picture_counts_as_colour() -> None:
    """A camera without an IR-cut filter gives a faintly purple night picture: it passes for daylight."""
    import io

    from PIL import Image

    image = Image.new("RGB", (320, 240), (128, 118, 134))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    assert colour_signature(out.getvalue()) is None, "a tinted grey night picture is taken as colour"
