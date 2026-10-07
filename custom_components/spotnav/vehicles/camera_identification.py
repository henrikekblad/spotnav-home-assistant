"""One charger's camera for vehicle identification: snapshots, reference pictures, and the AI Task query.

Off until a person chooses a camera (`AutoSettings.identify_camera`); offered only when Home Assistant has a
camera and an AI Task entity that takes attachments (`offered`). Pictures never leave Home Assistant except to
the chosen AI Task entity (with a local model, they stay at home), and none goes in a notification.

* **Snapshot** (`async_snapshot`): one picture from the camera (`camera.async_get_image`), for the frame
  editor.
* **Reference pictures** (`async_take_reference`): the camera's picture now, cropped with the charger's frame,
  kept as a car's `day` or `night` reference (`camera_pictures.ReferenceStore`) with its colour signature.
* **The query** (`async_ask`): a snapshot cropped with the frame, and the candidates' reference pictures from
  the same camera, go to `ai_task.generate_data` with a structured answer (one of the cars or `none`, and a
  confidence). The pictures are attachments by media-source id (`camera_media.py`): the crop as a temporary
  file in SpotNav's private storage, deleted after the call, and the references as their stored files. What
  the answer counts for is `camera_rule.camera_verdict`'s, applied by `identification.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from ..planning.auto_settings import AutoSettingsStore
from .camera_media import lend, take_back
from .camera_pictures import (
    as_jpeg,
    colour_signature,
    reference_signature,
    crop_jpeg,
    picture_size,
    Reference,
    ReferenceStore,
    thumbnail_jpeg,
)
from .camera_rule import answer_structure, instructions_for, labels_for, parse_answer, Signature
from .camera_settings import CameraSettings, normalised, same_frame

_LOGGER = logging.getLogger(__name__)

CAMERA_DOMAIN: Final = "camera"
AI_TASK_DOMAIN: Final = "ai_task"
AI_TASK_SERVICE: Final = "generate_data"
#: `AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS`: what the query needs.
AI_TASK_FEATURES: Final = 1 | 2
#: How long a camera may take to give a picture.
SNAPSHOT_TIMEOUT_S: Final = 10
TASK_NAME: Final = "SpotNav: which car is at the charger"


class CameraUnavailable(Exception):
    """No camera is chosen, or it gave no picture. `code` says which (`no_camera`, `no_picture`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Snapshot:
    jpeg: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class CameraAnswer:
    """The AI Task's answer: the candidate it names (`None`: none, or nothing readable) and its confidence."""

    vehicle_id: str | None
    confidence: str | None
    #: The colour of the picture now (`None` at night: infrared shows none, and colour then tells no car apart).
    now: Signature = None


def _entity_choices(hass: HomeAssistant, domain: str, features: int = 0) -> list[dict[str, str]]:
    found = []
    for state in hass.states.async_all(domain):
        if features and int(state.attributes.get("supported_features") or 0) & features != features:
            continue
        found.append({"entity_id": state.entity_id, "name": state.name})
    return sorted(found, key=lambda item: (item["name"].casefold(), item["entity_id"]))


def camera_choices(hass: HomeAssistant) -> list[dict[str, str]]:
    return _entity_choices(hass, CAMERA_DOMAIN)


def ai_task_choices(hass: HomeAssistant) -> list[dict[str, str]]:
    """AI Task entities that can answer with data and take pictures."""
    return _entity_choices(hass, AI_TASK_DOMAIN, AI_TASK_FEATURES)


def offered(hass: HomeAssistant) -> bool:
    """Whether Home Assistant has what camera identification needs: a camera and an AI Task entity."""
    return bool(camera_choices(hass)) and bool(ai_task_choices(hass))


def _write_temporary(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o600)
    return path


def _crop_and_colour(jpeg: bytes, frame: Any) -> tuple[bytes, Signature]:
    """The crop, and its colour (`None` for an infrared picture)."""
    crop = crop_jpeg(jpeg, frame)
    return crop, colour_signature(crop)


class CameraIdentification:
    """One charger's camera (see the module docstring)."""

    def __init__(self, hass: HomeAssistant, entry_id: str, store: AutoSettingsStore) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = store
        self.references = ReferenceStore(hass, entry_id)
        # One reference picture at a time at this charger: two taps never interleave a snapshot and its write.
        self._taking = asyncio.Lock()

    async def async_load(self) -> None:
        await self.references.async_load()

    def settings(self) -> CameraSettings | None:
        return self._store.settings(self._entry_id).identify_camera

    # ------------------------------------------------------------------ what others read

    def usable(self, vehicle_id: str) -> list[Reference]:
        """A car's reference pictures that compare with the picture now: taken by the chosen camera with the frame
        drawn now. One cropped with an earlier frame is stale until it is taken again."""
        settings = self.settings()
        if settings is None:
            return []
        return [
            item
            for item in self.references.references(vehicle_id, settings.camera_entity_id)
            if same_frame(item.frame, settings.frame)
        ]

    def reference_signatures(self, vehicles: Sequence[str]) -> dict[str, list[Signature]]:
        """Each car's usable reference pictures' colour signatures (a car without one: `[]`)."""
        return {car: [item.signature for item in self.usable(car)] for car in vehicles}

    def ready_for(self, vehicles: Sequence[str]) -> bool:
        """Whether a query among `vehicles` can say anything: a camera is chosen and one of them has a reference."""
        settings = self.settings()
        if settings is None or self._hass.states.get(settings.camera_entity_id) is None:
            return False
        return any(self.usable(car) for car in vehicles)

    def wire_references(self, vehicle_id: str) -> list[dict[str, Any]]:
        """A car's reference pictures from the chosen camera, as a client sees them (`stale` when cropped with
        another frame)."""
        settings = self.settings()
        if settings is None:
            return []
        return [
            item.as_wire(stale=not same_frame(item.frame, settings.frame))
            for item in self.references.references(vehicle_id, settings.camera_entity_id)
        ]

    def block(self, vehicles: Sequence[str]) -> dict[str, Any] | None:
        """The dashboard's `camera_identification`: the cameras and AI Task entities to choose from, and each
        car's reference pictures from the chosen camera. `None` when nothing is offered and nothing is chosen."""
        settings = self.settings()
        if settings is None and not offered(self._hass):
            return None
        return {
            "cameras": camera_choices(self._hass),
            "ai_tasks": ai_task_choices(self._hass),
            "references": {car: self.wire_references(car) for car in vehicles},
        }

    def diagnostics(self) -> dict[str, Any]:
        """The camera, the AI Task entity, the frame and which reference pictures each car has: never a picture."""
        settings = self.settings()
        return {
            "settings": None if settings is None else settings.as_dict(),
            "references": [
                {**item.as_wire(), "camera_entity_id": item.camera_entity_id}
                for car in sorted({reference.vehicle_id for reference in self.references.all()})
                for item in self.references.references(car)
            ],
        }

    # ------------------------------------------------------------------ pictures

    async def async_snapshot(self, camera_entity_id: str | None = None) -> Snapshot:
        """One picture from `camera_entity_id` (default the chosen camera), as a JPEG with its size."""
        if camera_entity_id is None:
            settings = self.settings()
            if settings is None:
                raise CameraUnavailable("no_camera")
            camera_entity_id = settings.camera_entity_id
        if not camera_entity_id.startswith(f"{CAMERA_DOMAIN}.") or self._hass.states.get(camera_entity_id) is None:
            raise CameraUnavailable("no_camera")
        from homeassistant.components.camera import async_get_image

        try:
            image = await async_get_image(self._hass, camera_entity_id, timeout=SNAPSHOT_TIMEOUT_S)
            jpeg = await self._hass.async_add_executor_job(as_jpeg, image.content)
            width, height = await self._hass.async_add_executor_job(picture_size, jpeg)
        except (HomeAssistantError, OSError, ValueError) as err:
            _LOGGER.debug("SpotNav got no picture from the camera: %s", type(err).__name__)
            raise CameraUnavailable("no_picture") from None
        return Snapshot(jpeg, width, height)

    async def async_take_reference(self, vehicle_id: str, kind: str) -> Reference:
        """The camera's picture now, cropped with the frame, kept as the car's `kind` reference."""
        async with self._taking:
            return await self._async_take(vehicle_id, kind)

    async def _async_take(self, vehicle_id: str, kind: str) -> Reference:
        settings = self.settings()
        if settings is None:
            raise CameraUnavailable("no_camera")
        snapshot = await self.async_snapshot(settings.camera_entity_id)
        crop = await self._hass.async_add_executor_job(crop_jpeg, snapshot.jpeg, settings.frame)
        signature = await self._hass.async_add_executor_job(reference_signature, crop, kind)
        reference = Reference(
            vehicle_id=vehicle_id,
            kind=kind,
            taken_at=dt_util.utcnow().replace(microsecond=0),
            camera_entity_id=settings.camera_entity_id,
            frame=normalised(settings.frame),
            signature=signature,
        )
        await self.references.async_put(reference, crop)
        return reference

    async def async_reference_thumbnail(self, vehicle_id: str, kind: str) -> bytes | None:
        settings = self.settings()
        camera = None if settings is None else settings.camera_entity_id
        for reference in self.references.references(vehicle_id, camera):
            if reference.kind == kind:
                data = await self.references.async_read(reference)
                return None if data is None else await self._hass.async_add_executor_job(thumbnail_jpeg, data)
        return None

    # ------------------------------------------------------------------ the query

    async def async_ask(self, candidates: Sequence[str]) -> CameraAnswer:
        """Ask the AI Task which of `candidates` stands at the charger now. Raises on any failure; the caller
        bounds the time (`camera_rule.QUERY_TIMEOUT_S`)."""
        settings = self.settings()
        if settings is None:
            raise CameraUnavailable("no_camera")
        camera = settings.camera_entity_id
        labelled = labels_for([car for car in candidates if self.usable(car)])
        if not labelled:
            raise CameraUnavailable("no_reference")
        snapshot = await self.async_snapshot(camera)
        crop, now = await self._hass.async_add_executor_job(_crop_and_colour, snapshot.jpeg, settings.frame)
        # Named before it is written, so a query cancelled meanwhile still removes it once the write is done.
        temporary = self.references.folder / "tmp" / f"query_{secrets.token_hex(8)}.jpg"
        tokens: list[str] = []
        write = self._hass.async_add_executor_job(_write_temporary, temporary, crop)
        try:
            await asyncio.shield(write)
            attachments = []
            pictures: list[tuple[str, str]] = []
            for label, car in labelled.items():
                for reference in self.usable(car):
                    media_id, token = lend(self._hass, self.references.path(reference))
                    tokens.append(token)
                    attachments.append({"media_content_id": media_id, "media_content_type": "image/jpeg"})
                    pictures.append((label, reference.kind))
            media_id, token = lend(self._hass, temporary)
            tokens.append(token)
            attachments.append({"media_content_id": media_id, "media_content_type": "image/jpeg"})
            data: dict[str, Any] = {
                "task_name": TASK_NAME,
                "instructions": instructions_for(pictures),
                "structure": answer_structure(list(labelled)),
                "attachments": attachments,
            }
            if settings.ai_task_entity_id is not None:
                data["entity_id"] = settings.ai_task_entity_id
            response = await self._hass.services.async_call(
                AI_TASK_DOMAIN, AI_TASK_SERVICE, data, blocking=True, return_response=True
            )
        finally:
            for token in tokens:
                take_back(self._hass, token)
            # Removed once the write is done, from its own callback: no further cancel can skip it.
            write.add_done_callback(lambda _done: self._hass.async_add_executor_job(temporary.unlink, True))
        vehicle_id, confidence = parse_answer((response or {}).get("data"), labelled)
        return CameraAnswer(vehicle_id, confidence, now)
