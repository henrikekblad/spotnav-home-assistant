"""The camera's pictures: the crop, the colour signature, and the reference pictures kept in private storage."""

from __future__ import annotations

import io
import random
from datetime import datetime, timezone
from pathlib import Path

from homeassistant.core import HomeAssistant
from PIL import Image

from custom_components.spotnav.vehicles.camera_pictures import (
    as_jpeg,
    colour_signature,
    reference_signature,
    crop_jpeg,
    MAX_SIDE,
    picture_size,
    Reference,
    ReferenceStore,
    thumbnail_jpeg,
)
from custom_components.spotnav.vehicles.camera_rule import distinct_cars
from custom_components.spotnav.vehicles.camera_settings import Frame


def picture(width: int, height: int, colour: tuple[int, int, int], *, spot: tuple[int, int, int] | None = None, fmt: str = "JPEG") -> bytes:
    """A camera picture: one colour, with an optional differently coloured parking spot in its right half."""
    image = Image.new("RGB", (width, height), colour)
    if spot is not None:
        image.paste(Image.new("RGB", (width // 2, height), spot), (width // 2, 0))
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


def test_the_crop_is_the_frame_scaled_down_as_a_jpeg() -> None:
    snapshot = picture(1536, 432, (40, 40, 40), spot=(200, 30, 30))
    crop = crop_jpeg(snapshot, Frame(0.5, 0.0, 0.5, 1.0))
    assert crop[:2] == b"\xff\xd8"
    assert picture_size(crop) == (MAX_SIDE, 432 * MAX_SIDE // 768)
    with Image.open(io.BytesIO(crop)) as image:
        red, green, blue = image.convert("RGB").getpixel((10, 10))
    assert red > 150 and green < 80, "only the parking spot"
    assert picture_size(crop_jpeg(picture(320, 200, (0, 0, 0)), None)) == (320, 200), "a small picture keeps its size"


def test_a_camera_that_sends_another_format_gives_a_jpeg() -> None:
    png = picture(64, 48, (10, 200, 10), fmt="PNG")
    assert as_jpeg(png)[:2] == b"\xff\xd8"
    jpeg = picture(64, 48, (10, 200, 10))
    assert as_jpeg(jpeg) is jpeg
    assert max(picture_size(thumbnail_jpeg(picture(1000, 500, (1, 2, 3))))) == 240


def test_a_daylight_reference_keeps_its_colour_and_a_night_reference_has_none() -> None:
    red = reference_signature(picture(200, 100, (60, 70, 60), spot=(180, 30, 30)), "day")
    white = reference_signature(picture(200, 100, (60, 70, 60), spot=(235, 235, 230)), "day")
    assert red is not None and white is not None and distinct_cars([red], [white]), "a white car is a colour too"
    assert reference_signature(picture(200, 100, (60, 70, 60), spot=(180, 30, 30)), "night") is None
    blue = reference_signature(picture(200, 100, (25, 30, 70)), "day")
    grey_blue = reference_signature(picture(200, 100, (38, 40, 52)), "day")
    assert blue is not None and grey_blue is not None and not distinct_cars([blue], [grey_blue])


def noisy(colour: tuple[int, int, int], spread: int, seed: int = 1) -> bytes:
    """A picture of one colour with sensor noise (each channel moved by up to `spread`), as a camera gives it."""
    rng = random.Random(seed)
    image = Image.new("RGB", (160, 120))
    image.putdata(
        [tuple(max(0, min(255, value + rng.randint(-spread, spread))) for value in colour) for _ in range(160 * 120)]
    )
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def test_an_infrared_picture_has_no_colour_grey_or_tinted() -> None:
    assert colour_signature(noisy((120, 120, 120), 12)) is None, "grey infrared with noise"
    assert colour_signature(noisy((128, 118, 134), 10)) is None, "purple tint, no infrared-cut filter"
    assert colour_signature(noisy((150, 128, 140), 10)) is None, "pink tint"
    assert colour_signature(noisy((12, 12, 14), 6)) is None, "a dark night, noise only"


def test_daylight_has_colour_even_dim() -> None:
    assert colour_signature(noisy((25, 30, 70), 6)) is not None, "a dark blue car at dusk"
    assert colour_signature(noisy((170, 40, 35), 10)) is not None, "a red car"
    assert colour_signature(picture(200, 100, (60, 70, 60), spot=(180, 30, 30))) is not None, "a red car on grey ground"
    assert colour_signature(picture(200, 100, (70, 110, 60), spot=(235, 235, 230))) is not None, "white car beside grass"


def test_the_signature_is_taken_from_the_middle_of_the_crop() -> None:
    image = Image.new("RGB", (100, 100), (20, 160, 20))
    image.paste(Image.new("RGB", (60, 60), (200, 20, 20)), (20, 20))
    out = io.BytesIO()
    image.save(out, format="PNG")
    signature = reference_signature(out.getvalue(), "day")
    assert signature is not None and signature[0] > 0.7 and signature[1] < 0.15


async def test_reference_pictures_are_kept_privately_replaced_by_kind_and_removed(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    store = ReferenceStore(hass, "entry_a")
    assert "/.storage/" in str(store.folder) and "www" not in store.folder.parts
    taken = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    day = Reference("car1", "day", taken, "camera.norr", Frame(0.1, 0.0, 0.8, 1.0), (0.7, 0.1, 0.1))
    night = Reference("car1", "night", taken, "camera.norr", None, None)
    await store.async_put(day, b"day-1")
    await store.async_put(night, b"night")
    await store.async_put(day, b"day-2")
    assert [item.kind for item in store.references("car1")] == ["day", "night"]
    assert await store.async_read(day) == b"day-2", "a new picture replaces the old"
    path = store.path(day)
    assert path.parent == store.folder and oct(path.stat().st_mode & 0o777) == "0o600"
    assert store.references("car1", "camera.other") == [], "only the chosen camera's pictures compare"

    again = ReferenceStore(hass, "entry_a")
    await again.async_load()
    assert again.references("car1") == [day, night], "kept across a restart"
    assert day.as_wire() == {"kind": "day", "taken_at": "2026-10-07T12:00:00+00:00", "colour": True, "stale": False}

    assert await again.async_delete("car1", "night") == 1
    assert not Path(again.path(night)).exists()
    assert await again.async_delete("car1", None) == 1
    assert again.references("car1") == []

    await store.async_put(day, b"x")
    await ReferenceStore.async_remove_stored(hass, "entry_a")
    assert not store.folder.exists()
    gone = ReferenceStore(hass, "entry_a")
    await gone.async_load()
    assert gone.references("car1") == []
