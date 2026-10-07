import asyncio

from homeassistant.components.ai_task import AITaskEntity, AITaskEntityFeature, GenDataTask, GenDataTaskResult
from homeassistant.components.conversation import ChatLog
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN


class VisionTask(AITaskEntity):
    """Answers SpotNav's comparison with its first car, not sure enough to decide alone."""

    _attr_has_entity_name = True
    _attr_name = "AI Task"
    _attr_unique_id = "demo_vision_ai_task"
    _attr_device_info = DeviceInfo(identifiers={(DOMAIN, "ai_task")})
    _attr_supported_features = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS

    def __init__(self) -> None:
        self.entity_id = "ai_task.local_vision_model"

    async def _async_generate_data(self, task: GenDataTask, chat_log: ChatLog) -> GenDataTaskResult:
        await asyncio.sleep(2)
        return GenDataTaskResult(conversation_id=chat_log.conversation_id, data={"vehicle": "car_1", "confidence": "medium"})


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([VisionTask()])
