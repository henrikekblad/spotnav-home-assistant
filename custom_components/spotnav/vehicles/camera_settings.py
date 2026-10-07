"""A charger's camera for vehicle identification: which camera, which AI Task entity, and the frame.

Pure values, no Home Assistant: the settings record keeps one (`AutoSettings.identify_camera`, `None` while
the camera is not used), and `crop_box` turns the frame into the pixels a picture is cropped to.

* `camera_entity_id` is a `camera.*` entity;
* `ai_task_entity_id` an `ai_task.*` entity, or `None` for Home Assistant's default AI Task entity;
* `frame` the parking spot as fractions of the picture (`x`, `y` its top left corner, `w`, `h` its size, each
  0-1, at least `FRAME_MIN_SIZE` wide and high and inside the picture), or `None` for the whole picture.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

#: A frame narrower or lower than this (a fraction of the picture) is a slip of the finger, not a parking spot.
FRAME_MIN_SIZE: Final = 0.05
#: Rounding a frame's fractions to this many decimals keeps a stored frame stable across a round trip.
FRAME_DECIMALS: Final = 4
_FRAME_KEYS: Final = frozenset({"x", "y", "w", "h"})
_CAMERA_KEYS: Final = frozenset({"camera_entity_id", "ai_task_entity_id", "frame"})
#: Rounding slack for a frame that ends exactly at the picture's edge.
_EDGE_SLACK: Final = 1e-6


class CameraSettingsError(ValueError):
    """A camera setting that cannot be stored; the message is for the log."""


def _fraction(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CameraSettingsError(f"frame.{what} must be a number")
    if value < 0 or value > 1:
        raise CameraSettingsError(f"frame.{what} must be between 0 and 1")
    return round(float(value), FRAME_DECIMALS)


@dataclass(frozen=True, slots=True)
class Frame:
    """The parking spot in the camera's picture, as fractions of its width and height."""

    x: float
    y: float
    w: float
    h: float

    @classmethod
    def from_wire(cls, raw: Any) -> Frame:
        """A frame from a client or the store, or `CameraSettingsError`."""
        if not isinstance(raw, dict) or set(raw) != _FRAME_KEYS:
            raise CameraSettingsError("a frame is {x, y, w, h}")
        frame = cls(*(_fraction(raw[key], key) for key in ("x", "y", "w", "h")))
        if frame.w < FRAME_MIN_SIZE or frame.h < FRAME_MIN_SIZE:
            raise CameraSettingsError(f"a frame is at least {FRAME_MIN_SIZE} of the picture each way")
        if frame.x + frame.w > 1 + _EDGE_SLACK or frame.y + frame.h > 1 + _EDGE_SLACK:
            raise CameraSettingsError("a frame lies inside the picture")
        return frame

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


#: The whole picture: what a camera without a frame is cropped to.
WHOLE_PICTURE: Final = Frame(0.0, 0.0, 1.0, 1.0)
#: Frames this close (each fraction) crop the same picture.
FRAME_TOLERANCE: Final = 0.001


def normalised(frame: Frame | None) -> Frame | None:
    """`frame`, or `None` when it is the whole picture (the same crop either way)."""
    if frame is None or all(
        abs(mine - whole) <= FRAME_TOLERANCE
        for mine, whole in zip((frame.x, frame.y, frame.w, frame.h), (0.0, 0.0, 1.0, 1.0), strict=True)
    ):
        return None
    return frame


def same_frame(first: Frame | None, second: Frame | None) -> bool:
    """Whether two frames crop the same picture: the whole picture either way, or within `FRAME_TOLERANCE`."""
    first, second = normalised(first), normalised(second)
    if first is None or second is None:
        return first is second
    return all(
        abs(a - b) <= FRAME_TOLERANCE
        for a, b in zip((first.x, first.y, first.w, first.h), (second.x, second.y, second.w, second.h), strict=True)
    )


def crop_box(frame: Frame | None, width: int, height: int) -> tuple[int, int, int, int]:
    """The pixels `frame` covers in a `width` x `height` picture: (left, top, right, bottom), right and bottom
    exclusive, inside the picture and at least one pixel each way. `None` is the whole picture."""
    if width < 1 or height < 1:
        raise CameraSettingsError("a picture has at least one pixel")
    frame = frame or WHOLE_PICTURE
    left = min(max(int(math.floor(frame.x * width + _EDGE_SLACK)), 0), width - 1)
    top = min(max(int(math.floor(frame.y * height + _EDGE_SLACK)), 0), height - 1)
    right = min(max(int(math.ceil((frame.x + frame.w) * width - _EDGE_SLACK)), left + 1), width)
    bottom = min(max(int(math.ceil((frame.y + frame.h) * height - _EDGE_SLACK)), top + 1), height)
    return left, top, right, bottom


def _entity(value: Any, domain: str, what: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"{domain}.") or len(value) <= len(domain) + 1:
        raise CameraSettingsError(f"{what} must be a {domain} entity")
    return value


@dataclass(frozen=True, slots=True)
class CameraSettings:
    """One charger's camera for identification."""

    camera_entity_id: str
    #: `None`: Home Assistant's default AI Task entity.
    ai_task_entity_id: str | None = None
    #: `None`: the whole picture.
    frame: Frame | None = None

    @classmethod
    def from_wire(cls, raw: Any) -> CameraSettings | None:
        """`null` (no camera), or `{camera_entity_id, ai_task_entity_id, frame}`; else `CameraSettingsError`."""
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) != _CAMERA_KEYS:
            raise CameraSettingsError("identify_camera is null or {camera_entity_id, ai_task_entity_id, frame}")
        ai_task = raw["ai_task_entity_id"]
        return cls(
            camera_entity_id=_entity(raw["camera_entity_id"], "camera", "camera_entity_id"),
            ai_task_entity_id=None if ai_task is None else _entity(ai_task, "ai_task", "ai_task_entity_id"),
            # The whole picture is stored as no frame: the same crop, one way of saying it.
            frame=normalised(None if raw["frame"] is None else Frame.from_wire(raw["frame"])),
        )

    def validated(self) -> CameraSettings:
        """The same settings, checked as a client's would be."""
        checked = CameraSettings.from_wire(self.as_dict())
        assert checked is not None
        return checked

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_entity_id": self.camera_entity_id,
            "ai_task_entity_id": self.ai_task_entity_id,
            "frame": None if self.frame is None else self.frame.as_dict(),
        }
