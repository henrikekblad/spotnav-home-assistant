"""The camera as one source of vehicle identification, on a real charger with a fake camera and a fake AI Task.

The fake camera shows a picture the test chooses; the fake AI Task entity recognises a car the way a model that
compares pictures would: the reference picture identical to the crop of the parking spot. It answers through
Home Assistant's own `ai_task.generate_data`, with the attachments resolved from SpotNav's media source.

* Between cars of different colours, a high-confidence answer decides; between similar cars it only orders the
  question's buttons; a person's answer always wins; one query per plug-in (one retry after an error); a slow
  model is ignored; nothing is asked of the camera when the cars' own reports decide.
* The commands: snapshot, frame, reference pictures (take, read, delete), over the WebSocket and the webhook.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.ai_task import AITaskEntity, AITaskEntityFeature, GenDataTask, GenDataTaskResult
from homeassistant.components.camera import Camera
from homeassistant.components.conversation import ChatLog
from homeassistant.config_entries import ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from PIL import Image
from pytest_homeassistant_custom_component.common import (
    CLIENT_ID,
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
    setup_test_component_platform,
)

from custom_components.spotnav.runtime import charger_data, domain_data
from custom_components.spotnav.vehicles.camera_rule import QUERY_TIMEOUT_S
from custom_components.spotnav.vehicles.camera_media import _LENT
from custom_components.spotnav.vehicles.camera_settings import CameraSettings, Frame
from custom_components.spotnav.vehicles.identification import ASK_AFTER_S, METHOD_ASSUMED, METHOD_CAMERA, METHOD_PLUG_SENSOR

from .test_vehicle_identification import World
from .world import non_admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

RED = (190, 30, 30)
WHITE = (235, 235, 230)
DARK_BLUE = (25, 30, 70)
DARK_GREY = (38, 40, 52)
EMPTY = (90, 90, 90)
FRAME = {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}


def picture(car: tuple[int, int, int], ground: tuple[int, int, int] = (60, 60, 60)) -> bytes:
    """The camera's picture: the ground on the left, the parking spot (and what stands there) on the right."""
    image = Image.new("RGB", (640, 240), ground)
    image.paste(Image.new("RGB", (320, 240), car), (320, 0))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


class FakeCamera(Camera):
    _attr_name = "Norr"
    _attr_unique_id = "camera_norr"

    def __init__(self) -> None:
        super().__init__()
        self.entity_id = "camera.norr"
        self.picture = picture(EMPTY)
        self.fail = False

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        if self.fail:
            raise HomeAssistantError("no picture")
        return self.picture


class FakeModel(AITaskEntity):
    """Recognises the reference picture that is the same picture as the crop of the parking spot."""

    _attr_name = "Local"
    _attr_unique_id = "ai_task_local"
    _attr_supported_features = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS

    def __init__(self) -> None:
        super().__init__()
        self.entity_id = "ai_task.local"
        self.confidence = "high"
        self.errors: list[Exception] = []
        self.hold: asyncio.Event | None = None
        self.tasks: list[dict[str, Any]] = []
        self.asked = asyncio.Event()

    async def _async_generate_data(self, task: GenDataTask, chat_log: ChatLog) -> GenDataTaskResult:
        attachments = [
            {"mime_type": item.mime_type, "path": Path(item.path), "data": Path(item.path).read_bytes()}
            for item in task.attachments or []
        ]
        self.tasks.append({"instructions": task.instructions, "structure": task.structure, "attachments": attachments})
        self.asked.set()
        if self.hold is not None:
            await self.hold.wait()
        if self.errors:
            raise self.errors.pop(0)
        *references, now = attachments
        labels = re.findall(r"picture \d+: (car_\d+)", task.instructions)
        answer = next((label for label, item in zip(labels, references) if item["data"] == now["data"]), "none")
        return GenDataTaskResult(conversation_id=chat_log.conversation_id, data={"vehicle": answer, "confidence": self.confidence})


class Garage:
    """The World of `test_vehicle_identification.py` (Kia, the current car, and Tesla), with a camera and a model."""

    def __init__(self, world: World, hass: HomeAssistant, hass_ws_client: Any, admin_user: Any) -> None:
        self.admin_user = admin_user
        self.world = world
        self.hass = hass
        self.hass_ws_client = hass_ws_client
        self.camera = FakeCamera()
        self.model = FakeModel()

    async def start(self, *, colours: tuple[tuple[int, int, int], tuple[int, int, int]] = (RED, WHITE), references: bool = True) -> Garage:
        hass = self.hass

        async def setup_entry(hass: HomeAssistant, entry: Any) -> bool:
            await hass.config_entries.async_forward_entry_setups(entry, ["camera", "ai_task"])
            return True

        assert await async_setup_component(hass, "homeassistant", {})
        mock_integration(hass, MockModule("test", async_setup_entry=setup_entry))
        mock_platform(hass, "test.config_flow")
        setup_test_component_platform(hass, "camera", [self.camera], from_config_entry=True)
        setup_test_component_platform(hass, "ai_task", [self.model], from_config_entry=True)
        devices = MockConfigEntry(domain="test")
        devices.add_to_hass(hass)
        assert await hass.config_entries.async_setup(devices.entry_id)
        await hass.async_block_till_done()
        await self.world.start()
        await domain_data(hass).auto_store.async_update(
            self.entry_id,
            mutate=lambda settings: replace(
                settings, identify_camera=CameraSettings("camera.norr", "ai_task.local", Frame.from_wire(FRAME))
            ),
        )
        # Signed in on the plug-in's day: a token made before the clock moved would have expired.
        refresh = await hass.auth.async_create_refresh_token(self.admin_user, CLIENT_ID)
        self.socket = await self.hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
        if references:
            for name, colour in zip(("Kia", "Tesla"), colours, strict=True):
                self.camera.picture = picture(colour)
                taken = await self.ws("take_reference_picture", vehicle_id=self.world.cars[name], kind="day")
                assert taken["ok"] is True, taken
            self.camera.picture = picture(EMPTY)
        return self

    @property
    def entry_id(self) -> str:
        return self.world.entry.entry_id

    async def ws(self, command: str, **fields: Any) -> dict[str, Any]:
        reply = await ws_call(self.socket, {"type": f"spotnav/{command}", "api_version": 1, "charger_id": self.entry_id, **fields})
        return reply["result"]

    async def settle(self) -> None:
        await self.hass.async_block_till_done(wait_background_tasks=True)

    def car_parks(self, colour: tuple[int, int, int]) -> None:
        self.camera.picture = picture(colour)

    @property
    def block(self) -> dict[str, Any]:
        return self.world.identifier.dashboard()

    def camera_evidence(self) -> dict[str, Any] | None:
        return next((item["camera"] for item in self.block["evidence"] if "camera" in item), None)


class _DevicesFlow(ConfigFlow):
    """The fake camera's and model's integration."""


@pytest.fixture
async def garage(hass: HomeAssistant, freezer: Any, hass_ws_client: Any, hass_admin_user: Any) -> Any:
    with mock_config_flow("test", _DevicesFlow):
        yield Garage(World(hass, freezer), hass, hass_ws_client, hass_admin_user)


# --------------------------------------------------------------------------------- the decision


async def test_between_cars_of_different_colours_a_sure_answer_decides_without_asking(garage: Garage) -> None:
    await garage.start()
    garage.car_parks(WHITE)
    await garage.world.plug_in()
    await garage.settle()
    world = garage.world
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    assert world.identifier.method == METHOD_CAMERA
    assert garage.camera_evidence() == {"entity_id": "camera.norr", "answer": world.cars["Tesla"], "confidence": "high", "used": True}
    await world.later(ASK_AFTER_S + 30)
    assert world.sent() == [], "nothing to ask"
    assert len(garage.model.tasks) == 1


async def test_the_model_gets_the_crop_and_the_references_and_no_name_and_the_crop_is_deleted(
    garage: Garage, hass: HomeAssistant
) -> None:
    await garage.start()
    garage.car_parks(RED)
    await garage.world.plug_in()
    await garage.settle()
    task = garage.model.tasks[0]
    assert [item["mime_type"] for item in task["attachments"]] == ["image/jpeg"] * 3
    references, crop = task["attachments"][:2], task["attachments"][2]
    camera = charger_data(hass, garage.entry_id).camera
    assert [item["path"] for item in references] == [
        camera.references.path(camera.references.references(garage.world.cars[name])[0]) for name in ("Kia", "Tesla")
    ]
    assert "/.storage/spotnav_camera/" in str(crop["path"]) and not crop["path"].exists(), "the temporary crop is gone"
    with Image.open(io.BytesIO(crop["data"])) as image:
        assert image.size == (320, 240), "only the parking spot"
    for name, car in garage.world.cars.items():
        assert name not in task["instructions"] and car not in task["instructions"]
    assert set(task["structure"].schema) == {"vehicle", "confidence"}
    assert hass.data.get(_LENT) == {}, "nothing stays lent"
    assert garage.world.settings.target.vehicle_id == garage.world.cars["Kia"]
    assert garage.world.identifier.method == METHOD_CAMERA


async def test_between_similar_cars_the_camera_only_puts_its_car_first(garage: Garage) -> None:
    await garage.start(colours=(DARK_BLUE, DARK_GREY))
    garage.car_parks(DARK_GREY)
    await garage.world.plug_in()
    await garage.settle()
    world = garage.world
    assert world.settings.target.vehicle_id == world.cars["Kia"], "no switch between similar cars"
    assert world.identifier.method == METHOD_ASSUMED
    assert garage.camera_evidence()["used"] is True
    await world.later(ASK_AFTER_S + 5)
    question = world.sent()[0]
    assert [action["title"] for action in question["data"]["actions"]] == ["Tesla", "Kia"], "the camera's car first"
    assert [item["vehicle_id"] for item in garage.block["candidates"]] == [world.cars["Tesla"], world.cars["Kia"]]


async def test_a_less_sure_answer_only_orders_the_buttons(garage: Garage) -> None:
    await garage.start()
    garage.model.confidence = "medium"
    garage.car_parks(WHITE)
    await garage.world.plug_in()
    await garage.settle()
    assert garage.world.settings.target.vehicle_id == garage.world.cars["Kia"]
    await garage.world.later(ASK_AFTER_S + 5)
    assert [a["title"] for a in garage.world.sent()[0]["data"]["actions"]] == ["Tesla", "Kia"]


async def test_a_car_without_a_reference_picture_keeps_the_camera_from_deciding(garage: Garage) -> None:
    await garage.start()
    await garage.ws("delete_reference_picture", vehicle_id=garage.world.cars["Tesla"], kind=None)
    garage.car_parks(RED)
    await garage.world.plug_in()
    await garage.settle()
    assert garage.world.identifier.method == METHOD_ASSUMED
    assert len(garage.model.tasks[0]["attachments"]) == 2, "only Kia's picture and the crop"


async def test_no_car_at_the_spot_counts_for_nothing(garage: Garage) -> None:
    await garage.start()
    await garage.world.plug_in()
    await garage.settle()
    assert garage.camera_evidence() == {"entity_id": "camera.norr", "answer": "none", "confidence": "high", "used": False}
    await garage.world.later(ASK_AFTER_S + 5)
    assert [a["title"] for a in garage.world.sent()[0]["data"]["actions"]] == ["Kia", "Tesla"]
    assert len(garage.model.tasks) == 1, "one query per plug-in"


async def test_a_persons_answer_wins_over_a_late_camera(garage: Garage) -> None:
    await garage.start()
    garage.model.hold = asyncio.Event()
    garage.car_parks(WHITE)
    await garage.world.plug_in()
    await garage.hass.async_block_till_done()
    assert await garage.world.identifier.async_answer(garage.world.cars["Kia"])
    garage.model.hold.set()
    await garage.settle()
    assert garage.world.settings.target.vehicle_id == garage.world.cars["Kia"]
    assert garage.world.identifier.method == "answered"
    assert garage.camera_evidence() is None or garage.camera_evidence()["used"] is False


async def test_the_cars_own_report_decides_and_the_camera_is_not_asked(garage: Garage) -> None:
    await garage.start()
    garage.world.car_says("Tesla", "plug", "on")
    await garage.world.plug_in()
    await garage.settle()
    assert garage.world.identifier.method == METHOD_PLUG_SENSOR
    assert garage.model.tasks == []


async def test_one_retry_after_an_error_and_none_after_that(garage: Garage) -> None:
    await garage.start()
    garage.model.errors = [HomeAssistantError("model down")]
    garage.car_parks(WHITE)
    await garage.world.plug_in()
    await garage.settle()
    assert len(garage.model.tasks) == 2
    assert garage.world.identifier.method == METHOD_CAMERA

    await garage.world.unplug()
    await garage.world.later(300)
    garage.model.errors = [HomeAssistantError("model down"), HomeAssistantError("still down")]
    await garage.world.plug_in()
    await garage.settle()
    await garage.world.later(60)
    await garage.settle()
    assert len(garage.model.tasks) == 4, "a new plug-in, one query and one retry"
    assert garage.camera_evidence()["answer"] is None


async def test_a_slow_model_is_ignored_and_the_question_asked_as_without_a_camera(garage: Garage) -> None:
    await garage.start()
    garage.model.hold = asyncio.Event()
    garage.car_parks(WHITE)
    await garage.world.plug_in()
    await garage.model.asked.wait()
    await garage.world.later(QUERY_TIMEOUT_S - 1)
    assert garage.camera_evidence() is None, "still waiting for the model"
    await garage.world.later(2)
    await garage.settle()
    assert garage.camera_evidence() == {"entity_id": "camera.norr", "answer": None, "confidence": None, "used": False}
    await garage.world.later(ASK_AFTER_S + 5)
    assert [a["title"] for a in garage.world.sent()[0]["data"]["actions"]] == ["Kia", "Tesla"]
    assert len(garage.model.tasks) == 1, "no retry after a timeout"
    garage.model.hold.set()


async def test_a_camera_that_gives_no_picture_is_no_evidence(garage: Garage) -> None:
    await garage.start()
    garage.camera.fail = True
    await garage.world.plug_in()
    await garage.settle()
    await garage.world.later(ASK_AFTER_S + 5)
    assert garage.world.sent(), "asked as without a camera"
    assert garage.model.tasks == []


# --------------------------------------------------------------------------------- the commands


async def test_the_snapshot_frame_and_reference_commands(garage: Garage, hass: HomeAssistant) -> None:
    await garage.start(references=False)
    kia = garage.world.cars["Kia"]
    garage.car_parks(RED)
    snap = await garage.ws("camera_snapshot")
    assert snap["ok"] is True and snap["picture"]["content_type"] == "image/jpeg"
    assert (snap["picture"]["width"], snap["picture"]["height"]) == (640, 240)
    assert base64.b64decode(snap["picture"]["data"]) == garage.camera.picture
    assert (await garage.ws("camera_snapshot", camera_entity_id="camera.none"))["error"] == "spotnav_no_camera"

    saved = await garage.ws("save_camera_frame", frame={"x": 0.25, "y": 0.0, "w": 0.75, "h": 1.0})
    assert saved["identify_camera"]["frame"] == {"x": 0.25, "y": 0.0, "w": 0.75, "h": 1.0}
    assert garage.world.settings.identify_camera.camera_entity_id == "camera.norr", "only the frame changed"
    assert (await garage.ws("save_camera_frame", frame={"x": 0.9, "y": 0, "w": 0.5, "h": 1}))["error"] == "spotnav_invalid_value"

    taken = await garage.ws("take_reference_picture", vehicle_id=kia, kind="day")
    assert taken["ok"] is True and taken["references"][0]["kind"] == "day" and taken["references"][0]["colour"] is True
    taken = await garage.ws("take_reference_picture", vehicle_id=kia, kind="night")
    assert [item["kind"] for item in taken["references"]] == ["day", "night"]
    thumb = await garage.ws("reference_picture", vehicle_id=kia, kind="day")
    assert thumb["picture"]["width"] <= 240 and base64.b64decode(thumb["picture"]["data"])[:2] == b"\xff\xd8"
    assert (await garage.ws("take_reference_picture", vehicle_id="unknown", kind="day"))["error"] == "spotnav_invalid_value"
    assert (await garage.ws("take_reference_picture", vehicle_id=kia, kind="dusk"))["error"] == "spotnav_invalid_value"
    garage.camera.fail = True
    assert (await garage.ws("take_reference_picture", vehicle_id=kia, kind="day"))["error"] == "spotnav_no_picture"
    garage.camera.fail = False

    deleted = await garage.ws("delete_reference_picture", vehicle_id=kia, kind="night")
    assert [item["kind"] for item in deleted["references"]] == ["day"]
    deleted = await garage.ws("delete_reference_picture", vehicle_id=kia, kind=None)
    assert deleted["references"] == []
    assert (await garage.ws("reference_picture", vehicle_id=kia, kind="day"))["error"] == "spotnav_invalid_value"


async def test_the_commands_are_an_administrators(
    garage: Garage, hass: HomeAssistant, hass_ws_client: Any, hass_read_only_user: Any
) -> None:
    await garage.start(references=False)
    refresh = await hass.auth.async_create_refresh_token(hass_read_only_user, CLIENT_ID)
    socket = await non_admin(hass, hass_ws_client, hass.auth.async_create_access_token(refresh))
    reply = await ws_call(socket, {"type": "spotnav/camera_snapshot", "api_version": 1, "charger_id": garage.entry_id})
    assert reply["result"] == {"api_version": 1, "ok": False, "error": "spotnav_not_admin"}
    reply = await ws_call(socket, {"type": "spotnav/camera_snapshot", "api_version": 2, "charger_id": garage.entry_id})
    assert reply["error"]["code"] == "spotnav_unsupported_api_version"


async def test_the_app_has_the_same_commands_and_the_dashboard_lists_the_choices(
    garage: Garage, hass: HomeAssistant, hass_client_no_auth: Any
) -> None:
    await garage.start()
    client = await hass_client_no_auth()

    async def post(action: str, **fields: Any) -> tuple[int, dict[str, Any]]:
        response = await client.post("/api/webhook/webhook-a", json={"version": 1, "action": action, **fields})
        return response.status, await response.json()

    status, snap = await post("camera_snapshot")
    assert status == 200 and snap["action"] == "camera_snapshot" and snap["picture"]["width"] == 640
    status, saved = await post("save_camera_frame", frame=FRAME)
    assert status == 200 and saved["identify_camera"]["frame"] == FRAME
    tesla = garage.world.cars["Tesla"]
    status, taken = await post("take_reference_picture", vehicle_id=tesla, kind="night")
    assert status == 200 and [item["kind"] for item in taken["references"]] == ["day", "night"]
    status, thumb = await post("reference_picture", vehicle_id=tesla, kind="night")
    assert status == 200 and thumb["picture"]["content_type"] == "image/jpeg"
    status, refused = await post("take_reference_picture", vehicle_id=tesla, kind="dusk")
    assert status == 400 and refused["error"] == "spotnav_invalid_value"
    status, deleted = await post("delete_reference_picture", vehicle_id=tesla, kind="night")
    assert status == 200 and [item["kind"] for item in deleted["references"]] == ["day"]

    status, dashboard = await post("dashboard", api_version=1)
    block = dashboard["camera_identification"]
    assert block["cameras"] == [{"entity_id": "camera.norr", "name": "Norr"}]
    assert block["ai_tasks"] == [{"entity_id": "ai_task.local", "name": "Local"}]
    assert [item["kind"] for item in block["references"][tesla]] == ["day"]
    assert "identify_camera" not in dashboard["settings"], "withheld from an app that does not ask for it"


async def test_without_a_camera_or_an_ai_task_nothing_is_offered(hass: HomeAssistant, freezer: Any) -> None:
    world = World(hass, freezer)
    await world.start()
    from custom_components.spotnav.api.dashboard import _camera_identification

    assert _camera_identification(hass, world.entry.entry_id, world.settings) is None
