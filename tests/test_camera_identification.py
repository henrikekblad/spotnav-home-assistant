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
from homeassistant.util import dt as dt_util
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
BLUE = (40, 90, 200)
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


def same_picture(first: bytes, second: bytes) -> bool:
    """Two JPEGs of the same scene: the same size and nearly the same average colour (encoding moves a little)."""
    def average(data: bytes) -> tuple[tuple[int, int], tuple[float, ...]]:
        with Image.open(io.BytesIO(data)) as image:
            small = image.convert("RGB").resize((8, 8))
            raw = small.tobytes()
            return image.size, tuple(sum(raw[channel::3]) / 64 for channel in range(3))

    (size_a, colour_a), (size_b, colour_b) = average(first), average(second)
    return size_a == size_b and max(abs(a - b) for a, b in zip(colour_a, colour_b)) < 6


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
        #: A label to answer whatever the pictures show (a model that is sure, rightly or not).
        self.answer: str | None = None

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
        now, *references = attachments
        labels = re.findall(r"picture \d+: (car_\d+)", task.instructions)
        answer = self.answer or next(
            (label for label, item in zip(labels, references) if same_picture(item["data"], now["data"])), "none"
        )
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

    async def start(self, *, colours: tuple[tuple[int, int, int], tuple[int, int, int]] = (RED, BLUE), references: bool = True) -> Garage:
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
async def garage(hass: HomeAssistant, freezer: Any, hass_ws_client: Any, hass_admin_user: Any, tmp_path: Path) -> Any:
    # The pictures go to this test's own folder, never the shared test configuration (tests run in parallel).
    hass.config.config_dir = str(tmp_path)
    with mock_config_flow("test", _DevicesFlow):
        yield Garage(World(hass, freezer), hass, hass_ws_client, hass_admin_user)


# --------------------------------------------------------------------------------- the decision


async def test_between_cars_of_different_colours_a_sure_answer_decides_without_asking(garage: Garage) -> None:
    await garage.start()
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    world = garage.world
    assert world.settings.target.vehicle_id == world.cars["Tesla"]
    assert world.identifier.method == METHOD_CAMERA
    assert garage.camera_evidence() == {"entity_id": "camera.norr", "answer": world.cars["Tesla"], "confidence": "high", "used": True}
    await world.later(ASK_AFTER_S + 30)
    assert world.sent() == [], "nothing to ask"
    assert len(garage.model.tasks) == 1
    diagnostics = world.identifier.diagnostics()
    assert diagnostics["evidence"][-1]["camera"]["used"] is True
    assert diagnostics["camera"]["settings"]["camera_entity_id"] == "camera.norr"
    assert [item["kind"] for item in diagnostics["camera"]["references"]] == ["day", "day"]
    assert "data" not in str(diagnostics["camera"]), "never a picture"


async def test_the_model_gets_the_crop_and_the_references_and_no_name_and_the_crop_is_deleted(
    garage: Garage, hass: HomeAssistant
) -> None:
    await garage.start()
    garage.car_parks(RED)
    await garage.world.plug_in()
    await garage.settle()
    task = garage.model.tasks[0]
    assert [item["mime_type"] for item in task["attachments"]] == ["image/jpeg"] * 3
    crop, references = task["attachments"][0], task["attachments"][1:]
    for item in [*references, crop]:
        assert "/.storage/spotnav_camera/entry_a/tmp/" in str(item["path"]) and not item["path"].exists(), (
            "every picture is a crop with the frame now, gone after the call"
        )
        with Image.open(io.BytesIO(item["data"])) as image:
            assert image.size == (320, 240), "only the parking spot, the references as the snapshot"
    camera = charger_data(hass, garage.entry_id).camera
    for name in ("Kia", "Tesla"):
        stored = camera.references.path(camera.references.references(garage.world.cars[name])[0])
        with Image.open(stored) as image:
            assert image.size == (640, 240), "the reference is kept whole"
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
    garage.car_parks(BLUE)
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
    garage.car_parks(BLUE)
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
    garage.car_parks(BLUE)
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
    garage.car_parks(BLUE)
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
    night = await garage.ws("reference_picture", vehicle_id=kia, kind="night")
    assert (night["picture"]["width"], night["picture"]["height"]) == (thumb["picture"]["width"], thumb["picture"]["height"]), (
        "day and night come cropped with the same frame, at the same size"
    )
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
    assert block["ai_tasks"] == [{"entity_id": "ai_task.local", "name": "Local", "model": None}]
    assert [item["kind"] for item in block["references"][tesla]] == ["day"]
    assert "identify_camera" not in dashboard["settings"], "withheld from an app that does not ask for it"


async def test_without_a_camera_or_an_ai_task_nothing_is_offered(hass: HomeAssistant, freezer: Any) -> None:
    world = World(hass, freezer)
    await world.start()
    from custom_components.spotnav.api.dashboard import _camera_identification

    assert _camera_identification(hass, world.entry.entry_id, world.settings) is None


# --------------------------------------------------------------------------------- after the review


async def test_at_night_the_camera_only_orders_the_buttons_however_sure(garage: Garage) -> None:
    await garage.start()
    garage.model.answer = "car_2"
    garage.car_parks((120, 120, 120))
    garage.camera.picture = picture((150, 150, 150), ground=(120, 120, 120))
    await garage.world.plug_in()
    await garage.settle()
    assert garage.world.identifier.method == METHOD_ASSUMED, "an infrared picture has no colour to tell cars apart"
    assert garage.camera_evidence()["used"] is True
    await garage.world.later(ASK_AFTER_S + 5)
    assert [a["title"] for a in garage.world.sent()[0]["data"]["actions"]] == ["Tesla", "Kia"]


async def test_a_redrawn_frame_keeps_every_reference_and_crops_it_anew(garage: Garage) -> None:
    await garage.start()
    camera = charger_data(garage.hass, garage.entry_id).camera
    kia, tesla = garage.world.cars["Kia"], garage.world.cars["Tesla"]
    before = camera.references.references(kia)[0].signature
    saved = await garage.ws("save_camera_frame", frame={"x": 0.25, "y": 0.0, "w": 0.75, "h": 1.0})
    assert saved["ok"] is True
    after = camera.references.references(kia)[0]
    assert after.signature != before, "its colour is taken anew with the new frame (a third is ground now)"
    assert after.signature_frame == saved_frame(saved)
    assert "stale" not in camera.block([kia])["references"][kia][0]
    thumb = await garage.ws("reference_picture", vehicle_id=kia, kind="day")
    assert (thumb["picture"]["width"], thumb["picture"]["height"]) == (240, 120), "the crop with the frame now"
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    assert len(garage.model.tasks) == 1 and garage.world.settings.target.vehicle_id == tesla


def saved_frame(answer: dict[str, Any]) -> Any:
    from custom_components.spotnav.vehicles.camera_settings import Frame

    return Frame.from_wire(answer["identify_camera"]["frame"])


async def test_a_picture_an_earlier_release_cropped_is_dropped_once_the_frame_differs(
    garage: Garage, hass: HomeAssistant
) -> None:
    from custom_components.spotnav.vehicles.camera_pictures import Reference

    await garage.start(references=False)
    camera = charger_data(hass, garage.entry_id).camera
    kia = garage.world.cars["Kia"]
    old = Reference(kia, "day", dt_util.utcnow(), "camera.norr", Frame.from_wire(FRAME), (0.7, 0.1, 0.1), Frame.from_wire(FRAME))
    await camera.references.async_put(old, picture(RED))
    assert camera.usable(kia) == [old], "it compares while its frame stands"
    await garage.ws("save_camera_frame", frame={"x": 0.25, "y": 0.0, "w": 0.75, "h": 1.0})
    assert camera.references.references(kia) == [], "its whole picture is gone: it is dropped"


async def test_a_cars_own_report_corrects_the_camera_and_a_persons_answer_outranks_both(garage: Garage) -> None:
    await garage.start()
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    world = garage.world
    assert world.settings.target.vehicle_id == world.cars["Tesla"] and world.identifier.method == METHOD_CAMERA
    world.car_says("Kia", "plug", "on")
    await world.later(5)
    assert world.settings.target.vehicle_id == world.cars["Kia"], "the car's own sensor outranks the camera"
    assert world.identifier.method == METHOD_PLUG_SENSOR
    world.car_says("Kia", "plug", "off")
    world.car_says("Tesla", "plug", "on")
    await world.later(60)
    assert world.settings.target.vehicle_id == world.cars["Kia"], "one correction, then it holds"


async def test_the_camera_car_reporting_itself_unplugged_hands_over_to_the_other(garage: Garage) -> None:
    await garage.start()
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    world = garage.world
    world.car_says("Tesla", "location", "not_home")
    await world.later(5)
    assert world.settings.target.vehicle_id == world.cars["Kia"]
    assert world.identifier.method == "location"


async def test_after_a_persons_answer_the_cars_reports_change_nothing(garage: Garage) -> None:
    await garage.start()
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    world = garage.world
    assert await world.identifier.async_answer(world.cars["Tesla"])
    world.car_says("Kia", "plug", "on")
    await world.later(5)
    assert world.settings.target.vehicle_id == world.cars["Tesla"] and world.identifier.method == "answered"


async def test_two_taps_at_once_keep_one_whole_picture(garage: Garage, hass: HomeAssistant) -> None:
    await garage.start(references=False)
    kia = garage.world.cars["Kia"]
    garage.car_parks(RED)
    camera = charger_data(hass, garage.entry_id).camera
    first, second = await asyncio.gather(camera.async_take_reference(kia, "day"), camera.async_take_reference(kia, "day"))
    assert first.taken_at <= second.taken_at
    files = await hass.async_add_executor_job(lambda: sorted(path.name for path in camera.references.folder.iterdir()))
    assert files == [f"{kia}_day.jpg"], "no half-written file left"


async def test_the_app_may_move_the_frame_but_never_choose_the_camera_or_the_ai_task(
    garage: Garage, hass: HomeAssistant, hass_client_no_auth: Any
) -> None:
    from custom_components.spotnav.api.settings import encode_settings

    await garage.start(references=False)
    client = await hass_client_no_auth()

    async def send(camera: Any) -> tuple[int, dict[str, Any]]:
        current = garage.world.settings
        body = {key: value for key, value in encode_settings(current).items() if key != "revision"}
        body["identify_camera"] = camera
        response = await client.post(
            "/api/webhook/webhook-a",
            json={"version": 1, "action": "settings", "expected_revision": current.revision, "settings": body,
                  "reads": ["identify_camera"]},
        )
        return response.status, await response.json()

    for other in (
        {"camera_entity_id": "camera.bedroom", "ai_task_entity_id": "ai_task.local", "frame": None},
        {"camera_entity_id": "camera.norr", "ai_task_entity_id": "ai_task.cloud", "frame": None},
        {"camera_entity_id": "camera.norr", "ai_task_entity_id": None, "frame": None},
        None,
    ):
        status, answer = await send(other)
        assert status == 400 and answer["error"] == "invalid_camera", other
    assert garage.world.settings.identify_camera.camera_entity_id == "camera.norr"
    moved = {"camera_entity_id": "camera.norr", "ai_task_entity_id": "ai_task.local", "frame": {"x": 0.25, "y": 0.0, "w": 0.75, "h": 1.0}}
    status, answer = await send(moved)
    assert status == 200 and answer["settings"]["identify_camera"] == moved
    response = await client.post(
        "/api/webhook/webhook-a", json={"version": 1, "action": "camera_snapshot", "camera_entity_id": "camera.norr"}
    )
    assert response.status == 400, "the app names no camera"


async def test_a_white_car_shows_no_colour_and_the_camera_only_orders(garage: Garage) -> None:
    await garage.start(colours=(RED, WHITE))
    garage.car_parks(WHITE)
    await garage.world.plug_in()
    await garage.settle()
    assert garage.world.identifier.method == METHOD_ASSUMED, "no colour to check the answer against: the safe side"
    assert garage.camera_evidence()["used"] is True


async def test_a_sure_answer_against_the_colour_now_only_orders(garage: Garage) -> None:
    await garage.start()
    garage.model.answer = "car_1"
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    assert garage.world.identifier.method == METHOD_ASSUMED, "the red car named for a blue crop"
    await garage.world.later(ASK_AFTER_S + 5)
    assert [a["title"] for a in garage.world.sent()[0]["data"]["actions"]] == ["Kia", "Tesla"]


async def test_an_ai_task_entity_names_the_model_of_its_subentry(hass: HomeAssistant) -> None:
    from homeassistant.config_entries import ConfigSubentryData
    from homeassistant.helpers import entity_registry as er

    from custom_components.spotnav.vehicles.camera_identification import ai_task_model

    entry = MockConfigEntry(
        domain="ollama",
        subentries_data=[
            ConfigSubentryData(data={"model": "qwen3-vl:4b-instruct"}, subentry_type="ai_task_data", title="Ollama AI Task", unique_id=None),
            ConfigSubentryData(data={"chat_model": "gpt-5-mini"}, subentry_type="ai_task_data", title="Cloud", unique_id=None),
        ],
    )
    entry.add_to_hass(hass)
    first, second = list(entry.subentries)
    registry = er.async_get(hass)
    registry.async_get_or_create("ai_task", "ollama", "a", config_entry=entry, config_subentry_id=first, suggested_object_id="ollama_ai_task")
    registry.async_get_or_create("ai_task", "ollama", "b", config_entry=entry, config_subentry_id=second, suggested_object_id="cloud")
    registry.async_get_or_create("ai_task", "ollama", "c", config_entry=entry, suggested_object_id="plain")
    assert ai_task_model(hass, "ai_task.ollama_ai_task") == "qwen3-vl:4b-instruct"
    assert ai_task_model(hass, "ai_task.cloud") == "gpt-5-mini"
    assert ai_task_model(hass, "ai_task.plain") is None
    assert ai_task_model(hass, "ai_task.unknown") is None


async def test_a_frame_changed_in_the_settings_takes_the_colours_anew_before_the_query(garage: Garage, hass: HomeAssistant) -> None:
    await garage.start()
    camera = charger_data(hass, garage.entry_id).camera
    kia = garage.world.cars["Kia"]
    wider = Frame(0.25, 0.0, 0.75, 1.0)
    await domain_data(hass).auto_store.async_update(
        garage.entry_id,
        mutate=lambda settings: replace(settings, identify_camera=replace(settings.identify_camera, frame=wider)),
    )
    assert camera.reference_signatures([kia])[kia] == [None], "a colour taken with another frame says nothing"
    garage.car_parks(BLUE)
    await garage.world.plug_in()
    await garage.settle()
    assert camera.references.references(kia)[0].signature_frame == wider, "taken anew, lazily, before the query"
    assert garage.world.identifier.method == METHOD_CAMERA
