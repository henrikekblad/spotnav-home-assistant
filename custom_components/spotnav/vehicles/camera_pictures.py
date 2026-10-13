"""The camera's pictures: cropping, colour signatures, thumbnails (Pillow), and the stored reference pictures.

The image functions are blocking and run in an executor (`hass.async_add_executor_job`).

**Reference pictures** are kept per charger in Home Assistant's private storage, beside its `.storage` records:
the files in `.storage/spotnav_camera/<charger id>/` (never `www/`, never a media folder), the list in the
store `spotnav.camera_references.<charger id>`. Each is the camera's whole picture, scaled to at most `WHOLE_SIDE`
pixels, with the camera it came from, when it was taken and its colour signature (`reference_signature`) as
cropped with the frame drawn now; it is cropped with the frame drawn at the time it is compared, so drawing the
frame again never makes a picture useless. A signature taken in an earlier release's way (`SIGNATURE_VERSION`) is
taken anew from the stored picture at load. A car has at most one picture of each kind (`day`,
`night`); a new one replaces the old. They are only ever read by SpotNav, sent only to the chosen AI Task
entity, and removed with the charger.
"""

from __future__ import annotations

import io
import logging
import os
import re
import secrets
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
from .camera_settings import crop_box, Frame, normalised

_LOGGER = logging.getLogger(__name__)

#: A cropped picture (sent to the AI Task, or kept as a reference) is scaled to at most this many pixels a side.
MAX_SIDE: Final = 768
#: A reference picture is kept whole, at most this many pixels on its long side, and cropped when it is compared.
WHOLE_SIDE: Final = 1600
#: A thumbnail for the settings is at most this many pixels a side.
THUMBNAIL_SIDE: Final = 240
JPEG_QUALITY: Final = 85
#: The middle of a cropped picture the colour signature is taken from (a fraction of each side): the car, not
#: the ground around it.
SIGNATURE_MIDDLE: Final = 0.6
#: What the colour signature means; a stored signature of another version is taken anew from its picture at load.
#: 1: the middle's average colour. 2: the average colour of what in the middle differs from the ground
#: (`_car_colour`).
SIGNATURE_VERSION: Final = 2
#: `_car_colour`: the ground is the colour most of the crop's edge (`GROUND_EDGE` pixels deep in the
#: `_SIGNATURE_PIXELS` grid) shares, within `GROUND_DISTANCE` (red, green, blue, each 0-1); it counts only when
#: at least `GROUND_SHARE` of the edge has it. A middle pixel at least `GROUND_DISTANCE` from it is the car, and
#: the car counts when at least `CAR_SHARE` of the middle is car.
GROUND_EDGE: Final = 2
GROUND_DISTANCE: Final = 0.15
GROUND_SHARE: Final = 0.6
CAR_SHARE: Final = 0.15
#: `has_colour`: a pixel this saturated (HSV), and not near black, is coloured; a picture with this share of
#: coloured pixels has colour. An infrared picture, grey or evenly tinted, stays near 0.1 saturation.
COLOURED_SATURATION: Final = 0.25
COLOURED_VALUE: Final = 0.15
COLOURED_SHARE: Final = 0.05
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


def scaled_jpeg(data: bytes, side: int = WHOLE_SIDE) -> bytes:
    """The whole picture, at most `side` pixels on its long side, as a JPEG."""
    image = _open(data)
    image.thumbnail((side, side))
    return _jpeg(image)


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


def _average(pixels: list[tuple[int, int, int]]) -> tuple[float, float, float]:
    """The average colour of `pixels` (red, green, blue, each 0-1, two decimals)."""
    count = len(pixels)
    return (
        round(sum(pixel[0] for pixel in pixels) / count / 255, 2),
        round(sum(pixel[1] for pixel in pixels) / count / 255, 2),
        round(sum(pixel[2] for pixel in pixels) / count / 255, 2),
    )


def _near(first: tuple[int, int, int], second: tuple[int, int, int], limit: float) -> bool:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2 + (first[2] - second[2]) ** 2 < limit


def _ground(edge: list[tuple[int, int, int]]) -> tuple[int, int, int] | None:
    """The ground's colour: the colour most of the crop's edge shares (the edge pixel with the most others within
    `GROUND_DISTANCE`, averaged with them), or `None` when less than `GROUND_SHARE` of the edge shares one colour
    (the car reaches the edge, or the ground is no one colour)."""
    limit = (GROUND_DISTANCE * 255) ** 2
    best: list[tuple[int, int, int]] = []
    for pixel in edge:
        alike = [other for other in edge if _near(pixel, other, limit)]
        if len(alike) > len(best):
            best = alike
    if len(best) < GROUND_SHARE * len(edge):
        return None
    count = len(best)
    return (
        round(sum(pixel[0] for pixel in best) / count),
        round(sum(pixel[1] for pixel in best) / count),
        round(sum(pixel[2] for pixel in best) / count),
    )


def _car_colour(image: Image.Image) -> tuple[float, float, float]:
    """The colour of the car in a cropped picture (red, green, blue, each 0-1, two decimals): the average of what in
    the middle differs from the ground, so a car on a large patch of paving is its own colour, not the paving's.

    The ground is the colour most of the crop's edge shares (`_ground`); a middle pixel at least `GROUND_DISTANCE`
    from it is the car (paint, windows, wheels, its shadow). When there is no one ground colour, or less than
    `CAR_SHARE` of the middle is car (the empty spot), it is the middle's average colour: the empty spot reads as
    its ground, clearly apart from a coloured car.
    """
    grid = image.resize((_SIGNATURE_PIXELS, _SIGNATURE_PIXELS))
    pixels = _pixels(grid)
    side = _SIGNATURE_PIXELS
    low = round(side * (1 - SIGNATURE_MIDDLE) / 2)
    high = max(side - low, low + 1)
    edge = [
        pixels[y * side + x]
        for y in range(side)
        for x in range(side)
        if min(x, y, side - 1 - x, side - 1 - y) < GROUND_EDGE
    ]
    middle = [pixels[y * side + x] for y in range(low, high) for x in range(low, high)]
    ground = _ground(edge)
    if ground is None:
        return _average(middle)
    limit = (GROUND_DISTANCE * 255) ** 2
    car = [pixel for pixel in middle if not _near(pixel, ground, limit)]
    return _average(car) if len(car) >= CAR_SHARE * len(middle) else _average(middle)


def has_colour(image: Image.Image) -> bool:
    """Whether a picture shows colour, rather than an infrared night picture.

    A pixel is coloured when its saturation (HSV: (max - min) / max) is at least `COLOURED_SATURATION` and it is not
    near black (value at least `COLOURED_VALUE`, where saturation is noise). An infrared picture is grey, or, from a
    camera without an infrared-cut filter, evenly tinted (purple or pink at a saturation near 0.1): few or none of
    its pixels reach that. A daylight scene has coloured things in it (paint, grass, signs, lights). The picture has
    colour when at least `COLOURED_SHARE` of its pixels are coloured. A daylight crop of only a white or grey car on
    grey ground may read as no colour: the camera then only orders the buttons, which is the safe side.
    """
    pixels = _pixels(image.resize((_SIGNATURE_PIXELS, _SIGNATURE_PIXELS)))
    coloured = 0
    for pixel in pixels:
        top = max(pixel)
        if top >= COLOURED_VALUE * 255 and (top - min(pixel)) / top >= COLOURED_SATURATION:
            coloured += 1
    return coloured >= COLOURED_SHARE * len(pixels)


def reference_signature(data: bytes, kind: str) -> Signature:
    """A reference picture's colour signature: the car's colour (`_car_colour`) for a daylight picture (the person
    says it is daylight, so a white car keeps its white), `None` for a night picture."""
    return _car_colour(_open(data)) if kind == PICTURE_DAY else None


def colour_signature(data: bytes) -> Signature:
    """The colour of the picture now: the car's colour (`_car_colour`), or `None` when the picture shows no colour
    (`has_colour`: an infrared night picture, grey or tinted)."""
    image = _open(data)
    return _car_colour(image) if has_colour(image) else None


def _pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    raw = image.tobytes()
    return [(raw[index], raw[index + 1], raw[index + 2]) for index in range(0, len(raw), 3)]


# --------------------------------------------------------------------------- the stored reference pictures


@dataclass(frozen=True, slots=True)
class Reference:
    """One reference picture of one car, as stored."""

    vehicle_id: str
    kind: str
    taken_at: datetime
    camera_entity_id: str
    #: What the stored picture was cropped with: `None` for the whole picture (every picture taken now), which is
    #: cropped with the frame drawn at the time it is compared. A picture an earlier release cropped when it was
    #: taken compares only while that frame stands.
    frame: Frame | None
    signature: Signature
    #: The frame `signature` was taken with (the colour of the picture cropped with it).
    signature_frame: Frame | None = None
    #: What `signature` means (`SIGNATURE_VERSION` when it was taken); one of an earlier version is taken anew from
    #: the stored picture, and says nothing until then.
    signature_version: int = SIGNATURE_VERSION

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
            "signature_frame": None if self.signature_frame is None else self.signature_frame.as_dict(),
            "signature_version": self.signature_version,
        }

    def as_wire(self) -> dict[str, Any]:
        """What a client sees: the kind, when it was taken and whether it has colour. Never the picture."""
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
            frame = normalised(None if raw["frame"] is None else Frame.from_wire(raw["frame"]))
            # An earlier release took the signature with the picture's own frame.
            signature_frame = raw.get("signature_frame", raw["frame"])
            # An earlier release stored no version: its signature is the middle's average colour.
            signature_version = raw.get("signature_version", 1)
            if not isinstance(signature_version, int) or isinstance(signature_version, bool):
                return None
            return cls(
                vehicle_id=raw["vehicle_id"],
                kind=raw["kind"],
                taken_at=taken_at,
                camera_entity_id=raw["camera_entity_id"],
                frame=frame,
                signature=None if signature is None else (float(signature[0]), float(signature[1]), float(signature[2])),
                signature_frame=normalised(None if signature_frame is None else Frame.from_wire(signature_frame)),
                signature_version=signature_version,
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def valid_vehicle_id(value: Any) -> bool:
    """A vehicle id that can name a file (a device id is hex)."""
    return isinstance(value, str) and _VEHICLE_ID.match(value) is not None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    # A name of its own: two pictures of one kind written at once never share a half-written file.
    temporary = path.with_name(f"{path.stem}.{secrets.token_hex(6)}.tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _sweep(folder: Path) -> None:
    """Remove what a write cut short left: the query crops in `tmp/` and half-written reference pictures."""
    shutil.rmtree(folder / "tmp", True)
    if folder.is_dir():
        for stray in folder.glob("*.tmp"):
            stray.unlink(missing_ok=True)


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
        # A crop left behind by a query that never finished (a power cut, a crash) goes now.
        await self._hass.async_add_executor_job(_sweep, self.folder)
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

    async def async_update(self, reference: Reference) -> None:
        """Keep a reference's new facts (its signature), the picture as it is."""
        self._references[(reference.vehicle_id, reference.kind)] = reference
        await self._async_save()

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
