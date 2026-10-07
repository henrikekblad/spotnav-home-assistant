from homeassistant.components.camera import Camera
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, PARKED
from .scene import render


class DrivewayCamera(Camera):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_unique_id = "demo_vision_driveway"
    _attr_device_info = DeviceInfo(identifiers={(DOMAIN, "camera")})

    def __init__(self) -> None:
        super().__init__()
        self.entity_id = "camera.driveway_camera"

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return await self.hass.async_add_executor_job(render, PARKED["vehicle"])


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([DrivewayCamera()])
