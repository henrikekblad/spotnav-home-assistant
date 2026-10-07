"""The camera for vehicle identification, the pure parts: the frame and its crop, what an answer counts for,
when the camera is asked, and the question it is asked."""

from __future__ import annotations

import pytest

from custom_components.spotnav.vehicles.camera_rule import (
    answer_structure,
    camera_verdict,
    CameraVerdict,
    COLOUR_DISTINCT,
    distinct_cars,
    instructions_for,
    labels_for,
    may_query,
    nearest_car,
    parse_answer,
    QUERY_ATTEMPTS,
)
from custom_components.spotnav.vehicles.camera_settings import (
    CameraSettings,
    CameraSettingsError,
    crop_box,
    Frame,
    FRAME_MIN_SIZE,
    normalised,
    same_frame,
)

RED = (0.7, 0.1, 0.1)
WHITE = (0.9, 0.9, 0.9)
DARK_BLUE = (0.1, 0.12, 0.25)
DARK_GREY = (0.15, 0.15, 0.17)

# --------------------------------------------------------------------------------- the frame and the crop


def test_a_frame_crops_the_pixels_it_covers() -> None:
    assert crop_box(Frame(0.25, 0.5, 0.5, 0.25), 1536, 432) == (384, 216, 1152, 324)
    assert crop_box(None, 1536, 432) == (0, 0, 1536, 432), "no frame is the whole picture"
    assert crop_box(Frame(0.0, 0.0, 1.0, 1.0), 640, 480) == (0, 0, 640, 480)


def test_a_crop_rounds_outwards_and_stays_inside_the_picture() -> None:
    left, top, right, bottom = crop_box(Frame(0.333, 0.333, 0.334, 0.334), 100, 10)
    assert (left, top) == (33, 3) and (right, bottom) == (67, 7), "the frame is never cut short"
    assert crop_box(Frame(0.95, 0.95, 0.05, 0.05), 10, 10) == (9, 9, 10, 10), "at least one pixel"
    assert crop_box(Frame(0.9999, 0.0, 0.0001, 1.0), 3, 3)[2] == 3


def test_a_frame_is_inside_the_picture_and_not_a_slip_of_the_finger() -> None:
    assert Frame.from_wire({"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.6}) == Frame(0.1, 0.2, 0.5, 0.6)
    assert Frame.from_wire({"x": 0.12345678, "y": 0, "w": 0.5, "h": 1}).x == 0.1235, "four decimals"
    for bad in (
        {"x": 0.6, "y": 0, "w": 0.5, "h": 0.5},
        {"x": 0, "y": 0, "w": FRAME_MIN_SIZE / 2, "h": 0.5},
        {"x": -0.1, "y": 0, "w": 0.5, "h": 0.5},
        {"x": 0, "y": 0, "w": 0.5, "h": 1.5},
        {"x": True, "y": 0, "w": 0.5, "h": 0.5},
        {"x": float("nan"), "y": 0, "w": 0.5, "h": 0.5},
        {"x": "0", "y": 0, "w": 0.5, "h": 0.5},
        {"x": 0, "y": 0, "w": 0.5},
        {"x": 0, "y": 0, "w": 0.5, "h": 0.5, "z": 1},
        [0, 0, 1, 1],
    ):
        with pytest.raises(CameraSettingsError):
            Frame.from_wire(bad)


def test_the_camera_settings_name_a_camera_and_an_ai_task_entity_or_the_default() -> None:
    raw = {"camera_entity_id": "camera.norr", "ai_task_entity_id": None, "frame": {"x": 0, "y": 0, "w": 1, "h": 1}}
    settings = CameraSettings.from_wire(raw)
    assert settings == CameraSettings("camera.norr", None, None), "the whole picture is stored as no frame"
    assert settings.as_dict() == {**raw, "frame": None}
    framed = CameraSettings.from_wire({**raw, "frame": {"x": 0.1, "y": 0, "w": 0.5, "h": 1}})
    assert framed is not None and framed.frame == Frame(0.1, 0.0, 0.5, 1.0)
    assert CameraSettings.from_wire(None) is None
    for bad in (
        {**raw, "camera_entity_id": "image.norr"},
        {**raw, "camera_entity_id": "camera."},
        {**raw, "ai_task_entity_id": "conversation.ollama"},
        {**raw, "frame": {"x": 0, "y": 0, "w": 0, "h": 0}},
        {"camera_entity_id": "camera.norr"},
        "camera.norr",
    ):
        with pytest.raises(CameraSettingsError):
            CameraSettings.from_wire(bad)


# --------------------------------------------------------------------------------- what the answer counts for


def test_cars_differ_only_when_every_coloured_picture_of_one_is_far_from_the_other() -> None:
    assert distinct_cars([RED], [WHITE])
    assert not distinct_cars([DARK_BLUE], [DARK_GREY]), "the owner's two dark cars"
    assert not distinct_cars([None], [WHITE]), "a night picture has no colour"
    assert not distinct_cars([], [WHITE])
    assert distinct_cars([RED, None], [WHITE]), "a night picture beside a day picture changes nothing"
    assert not distinct_cars([RED, DARK_GREY], [DARK_BLUE]), "one similar pair is enough to doubt"
    near = (RED[0] - COLOUR_DISTINCT + 0.01, RED[1], RED[2])
    assert not distinct_cars([RED], [near])


def test_a_high_confidence_answer_between_cars_of_different_colours_decides() -> None:
    references = {"ev6": [RED], "tesla": [WHITE]}
    assert camera_verdict("ev6", "high", ["ev6", "tesla"], references, RED) == CameraVerdict(decides="ev6", prefers="ev6")


def test_at_night_the_camera_never_decides_alone() -> None:
    references = {"ev6": [RED], "tesla": [WHITE]}
    assert camera_verdict("ev6", "high", ["ev6", "tesla"], references, None) == CameraVerdict(prefers="ev6")


def test_between_similar_cars_the_camera_only_orders_the_buttons() -> None:
    references = {"ev6": [DARK_BLUE, None], "ioniq": [DARK_GREY]}
    assert camera_verdict("ev6", "high", ["ev6", "ioniq"], references, DARK_BLUE) == CameraVerdict(prefers="ev6")


def test_a_lower_confidence_only_orders_the_buttons() -> None:
    references = {"ev6": [RED], "tesla": [WHITE]}
    assert camera_verdict("tesla", "medium", ["ev6", "tesla"], references, WHITE) == CameraVerdict(prefers="tesla")
    assert camera_verdict("tesla", None, ["ev6", "tesla"], references, WHITE) == CameraVerdict(prefers="tesla")


def test_a_car_without_a_reference_picture_keeps_the_camera_from_deciding() -> None:
    references = {"ev6": [RED], "tesla": [WHITE], "zoe": []}
    assert camera_verdict("ev6", "high", ["ev6", "tesla", "zoe"], references, RED) == CameraVerdict(prefers="ev6")
    assert camera_verdict("ev6", "high", ["ev6", "tesla"], references, RED).decides == "ev6", (
        "a car the evidence ruled out does not count"
    )


def test_no_car_or_a_car_that_is_no_candidate_counts_for_nothing() -> None:
    references = {"ev6": [RED], "tesla": [WHITE]}
    assert camera_verdict(None, "high", ["ev6", "tesla"], references, RED) == CameraVerdict()
    assert camera_verdict("volvo", "high", ["ev6", "tesla"], references, RED) == CameraVerdict()


def test_a_sure_answer_decides_only_when_the_colour_now_is_nearest_the_named_car() -> None:
    references = {"ev6": [RED], "tesla": [WHITE]}
    assert camera_verdict("ev6", "high", ["ev6", "tesla"], references, (0.85, 0.85, 0.82)) == CameraVerdict(prefers="ev6")
    assert nearest_car((0.6, 0.15, 0.1), ["ev6", "tesla"], references) == "ev6"
    assert nearest_car((0.5, 0.5, 0.5), ["ev6"], {"ev6": [None]}) is None


# --------------------------------------------------------------------------------- when it is asked


def test_the_camera_is_asked_once_per_plug_in_and_once_more_only_after_an_error() -> None:
    ask = dict(waiting=True, remaining=2, elapsed_s=5.0, window_s=180.0)
    assert may_query(attempts=0, failed=False, **ask)
    assert not may_query(attempts=1, failed=False, **ask), "an answer, or a timeout, is the plug-in's one query"
    assert may_query(attempts=1, failed=True, **ask), "one retry after an error"
    assert not may_query(attempts=QUERY_ATTEMPTS, failed=True, **ask)


def test_the_camera_is_not_asked_once_decided_answered_or_asked_or_with_one_car_left() -> None:
    assert not may_query(attempts=0, failed=False, waiting=False, remaining=2, elapsed_s=5, window_s=180)
    assert not may_query(attempts=0, failed=False, waiting=True, remaining=1, elapsed_s=5, window_s=180)
    assert not may_query(attempts=0, failed=False, waiting=True, remaining=2, elapsed_s=180, window_s=180), (
        "only before the question"
    )


# --------------------------------------------------------------------------------- the question


def test_the_question_names_no_car_and_lists_the_pictures_in_order() -> None:
    labels = labels_for(["device-a", "device-b"])
    assert labels == {"car_1": "device-a", "car_2": "device-b"}
    text = instructions_for([("car_1", "day"), ("car_1", "night"), ("car_2", "day")])
    assert "picture 1: car_1 (daylight); picture 2: car_1 (night" in text and "picture 3: car_2 (daylight)" in text
    assert "The first 3 are reference pictures" in text and "not the colour" in text and "Answer none" in text
    structure = answer_structure(list(labels))
    assert structure["vehicle"]["selector"] == {"select": {"options": ["car_1", "car_2", "none"]}}
    assert structure["confidence"]["selector"] == {"select": {"options": ["high", "medium", "low"]}}
    assert structure["vehicle"]["required"] and structure["confidence"]["required"]


def test_an_answer_it_cannot_read_is_no_answer() -> None:
    labels = {"car_1": "device-a", "car_2": "device-b"}
    assert parse_answer({"vehicle": "car_2", "confidence": "high"}, labels) == ("device-b", "high")
    assert parse_answer({"vehicle": " car_1 ", "confidence": "Medium"}, labels) == ("device-a", "medium")
    assert parse_answer({"vehicle": "none", "confidence": "high"}, labels) == (None, "high")
    assert parse_answer({"vehicle": "car_9", "confidence": "sure"}, labels) == (None, None)
    assert parse_answer("car_1", labels) == (None, None)
    assert parse_answer({"vehicle": 1}, labels) == (None, None)


def test_frames_that_crop_the_same_picture_are_the_same() -> None:
    assert normalised(Frame(0.0, 0.0, 1.0, 1.0)) is None and normalised(Frame(0.0005, 0, 0.9995, 1)) is None
    assert same_frame(None, Frame(0, 0, 1, 1))
    assert same_frame(Frame(0.1, 0.2, 0.5, 0.5), Frame(0.1004, 0.2, 0.5, 0.5))
    assert not same_frame(Frame(0.1, 0.2, 0.5, 0.5), Frame(0.12, 0.2, 0.5, 0.5))
    assert not same_frame(None, Frame(0.1, 0.2, 0.5, 0.5))
