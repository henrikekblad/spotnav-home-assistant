"""The camera's pictures: cropping, colour signatures, thumbnails (Pillow), and the stored reference pictures.

The image functions are blocking and run in an executor (`hass.async_add_executor_job`).

**Reference pictures** are kept per charger in Home Assistant's private storage, beside its `.storage` records:
the files in `.storage/spotnav_camera/<charger id>/` (never `www/`, never a media folder), the list in the
store `spotnav.camera_references.<charger id>`. Each is the camera's picture cropped with the frame as it was
when it was taken, scaled to at most `MAX_SIDE` pixels, with the camera it came from, the frame, when it was
taken and its colour signature (`colour_signature`). A car has at most one picture of each kind (`day`,
`night`); a new one replaces the old. They are only ever read by SpotNav, sent only to the chosen AI Task
entity, and removed with the charger.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.util import dt as dt_util
from PIL import Image, ImageOps

from ..const import DOMAIN
from .camera_rule import PICTURE_DAY, PICTURE_KINDS, Signature
from .camera_settings import crop_box, Frame

_LOGGER = logging.getLogger(__name__)

#: A cropped picture (sent to the AI Task, or kept as a reference) is scaled to at most this many pixels a side.
MAX_SIDE: Final = 768
#: A thumbnail for the settings is at most this many pixels a side.
THUMBNAIL_SIDE: Final = 240
JPEG_QUALITY: Final = 85
#: The middle of a cropped picture the colour signature is taken from (a fraction of each side): the car, not
#: the ground around it.
SIGNATURE_MIDDLE: Final = 0.6
#: A picture whose pixels differ this little between red, green and blue on average has no colour (infrared).
GREY_SPREAD: Final = 0.015
_SIGNATURE_PIXELS: Final = 24
_VEHICLE_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STORE_VERSION: Final = 1
_STORE_PREFIX: Final = f"{DOMAIN}.camera_references"
_FOLDER: Final = f"{DOMAIN}_camera"


# --------------------------------------------------------------------------- image functions (blocking)


def _open(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as raw:
        return ImageOps.exif_transpose(raw).convert("RGB")


def _jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def picture_size(data: bytes) -> tuple[int, int]:
    """A picture's width and height in pixels."""
    with Image.open(io.BytesIO(data)) as raw:
        return raw.size


def as_jpeg(data: bytes) -> bytes:
    """A camera's picture as a JPEG (a camera may send another format), at its own size."""
    with Image.open(io.BytesIO(data)) as raw:
        if raw.format == "JPEG":
            return data
    return _jpeg(_open(data))


def crop_jpeg(data: bytes, frame: Frame | None) -> bytes:
    """The picture cropped to `frame` (`None`: the whole picture), at most `MAX_SIDE` a side, as a JPEG."""
    image = _open(data)
    cropped = image.crop(crop_box(frame, *image.size))
    cropped.thumbnail((MAX_SIDE, MAX_SIDE))
    return _jpeg(cropped)


def thumbnail_jpeg(data: bytes, side: int = THUMBNAIL_SIDE) -> bytes:
    image = _open(data)
    image.thumbnail((side, side))
    return _jpeg(image)


def colour_signature(data: bytes, kind: str = PICTURE_DAY) -> Signature:
    """The average colour of the middle of a cropped picture (red, green, blue, each 0-1, two decimals), or `None`
    for a picture whose colour says nothing: a night picture, or one without any colour at all (an infrared
    picture is grey all over; a daylight picture of a white or black car still has some colour around it)."""
    if kind != PICTURE_DAY:
        return None
    image = _open(data)
    whole = image.resize((_SIGNATURE_PIXELS, _SIGNATURE_PIXELS))
    if _spread(_pixels(whole)) < GREY_SPREAD:
        return None
    width, height = image.size
    margin_x = width * (1 - SIGNATURE_MIDDLE) / 2
    margin_y = height * (1 - SIGNATURE_MIDDLE) / 2
    middle = image.crop(
        (int(margin_x), int(margin_y), max(int(width - margin_x), int(margin_x) + 1), max(int(height - margin_y), int(margin_y) + 1))
    ).resize((_SIGNATURE_PIXELS, _SIGNATURE_PIXELS))
    pixels = _pixels(middle)
    count = len(pixels)
    return (
        round(sum(pixel[0] for pixel in pixels) / count / 255, 2),
        round(sum(pixel[1] for pixel in pixels) / count / 255, 2),
        round(sum(pixel[2] for pixel in pixels) / count / 255, 2),
    )


def _pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    raw = image.tobytes()
    return [(raw[index], raw[index + 1], raw[index + 2]) for index in range(0, len(raw), 3)]


def _spread(pixels: list[tuple[int, int, int]]) -> float:
    """How far red, green and blue lie apart, on average over the pixels (0: grey all over)."""
    return sum(max(pixel) - min(pixel) for pixel in pixels) / len(pixels) / 255


# --------------------------------------------------------------------------- the stored reference pictures


@dataclass(frozen=True, slots=True)
class Reference:
    """One reference picture of one car, as stored."""

    vehicle_id: str
    kind: str
    taken_at: datetime
    camera_entity_id: str
    frame: Frame | None
    signature: Signature

    @property
    def file_name(self) -> str:
        return f"{self.vehicle_id}_{self.kind}.jpg"

    def as_stored(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "kind": self.kind,
            "taken_at": self.taken_at.isoformat(),
            "camera_entity_id": self.camera_entity_id,
            "frame": None if self.frame is None else self.frame.as_dict(),
            "signature": None if self.signature is None else list(self.signature),
        }

    def as_wire(self) -> dict[str, Any]:
        """What a client sees: the kind, when it was taken and whether it has colour (never the picture)."""
        return {"kind": self.kind, "taken_at": self.taken_at.isoformat(), "colour": self.signature is not None}

    @classmethod
    def from_stored(cls, raw: Any) -> Reference | None:
        try:
            signature = raw["signature"]
            taken_at = dt_util.parse_datetime(raw["taken_at"])
            if (
                not valid_vehicle_id(raw["vehicle_id"])
                or raw["kind"] not in PICTURE_KINDS
                or taken_at is None
                or not isinstance(raw["camera_entity_id"], str)
            ):
                return None
            return cls(
                vehicle_id=raw["vehicle_id"],
                kind=raw["kind"],
                taken_at=taken_at,
                camera_entity_id=raw["camera_entity_id"],
                frame=None if raw["frame"] is None else Frame.from_wire(raw["frame"]),
                signature=None if signature is None else (float(signature[0]), float(signature[1]), float(signature[2])),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def valid_vehicle_id(value: Any) -> bool:
    """A vehicle id that can name a file (a device id is hex)."""
    return isinstance(value, str) and _VEHICLE_ID.match(value) is not None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


class ReferenceStore:
    """One charger's reference pictures (see the module docstring)."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_PREFIX}.{entry_id}")
        self.folder = Path(hass.config.path(STORAGE_DIR, _FOLDER, entry_id))
        self._references: dict[tuple[str, str], Reference] = {}

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        items = raw.get("references") if isinstance(raw, dict) else None
        self._references = {}
        for item in items if isinstance(items, list) else []:
            reference = Reference.from_stored(item)
            if reference is not None:
                self._references[(reference.vehicle_id, reference.kind)] = reference

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: its reference pictures go with it."""
        await Store(hass, _STORE_VERSION, f"{_STORE_PREFIX}.{entry_id}").async_remove()
        folder = Path(hass.config.path(STORAGE_DIR, _FOLDER, entry_id))
        await hass.async_add_executor_job(shutil.rmtree, folder, True)

    def references(self, vehicle_id: str, camera_entity_id: str | None = None) -> list[Reference]:
        """A car's reference pictures, day first; with `camera_entity_id` only those taken by that camera."""
        return [
            self._references[(vehicle_id, kind)]
            for kind in PICTURE_KINDS
            if (vehicle_id, kind) in self._references
            and (camera_entity_id is None or self._references[(vehicle_id, kind)].camera_entity_id == camera_entity_id)
        ]

    def all(self) -> list[Reference]:
        return list(self._references.values())

    def path(self, reference: Reference) -> Path:
        return self.folder / reference.file_name

    async def async_read(self, reference: Reference) -> bytes | None:
        return await self._hass.async_add_executor_job(_read, self.path(reference))

    async def async_put(self, reference: Reference, picture: bytes) -> None:
        """Keep `picture` as the car's reference of its kind, replacing the one before."""
        await self._hass.async_add_executor_job(_write, self.path(reference), picture)
        self._references[(reference.vehicle_id, reference.kind)] = reference
        await self._async_save()

    async def async_delete(self, vehicle_id: str, kind: str | None) -> int:
        """Remove a car's reference of `kind`, or every one with `None`: how many were removed."""
        gone = [
            reference
            for (car, picture_kind), reference in self._references.items()
            if car == vehicle_id and (kind is None or picture_kind == kind)
        ]
        for reference in gone:
            del self._references[(reference.vehicle_id, reference.kind)]
            await self._hass.async_add_executor_job(self.path(reference).unlink, True)
        if gone:
            await self._async_save()
        return len(gone)

    async def _async_save(self) -> None:
        await self._store.async_save({"references": [reference.as_stored() for reference in self._references.values()]})
