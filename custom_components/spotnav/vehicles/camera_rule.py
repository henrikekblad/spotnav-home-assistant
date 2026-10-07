"""What the camera's answer counts for, when it is asked, and what it is asked: pure, no Home Assistant.

The camera is one source of evidence for vehicle identification (`identification.py`). It is asked only while
the cars' own reports have not decided, and its answer:

* **decides alone** only when it is high confidence, the picture now has colour (never at night: an infrared
  picture is grey or evenly tinted) nearest the named car's reference colour, every candidate left has a
  reference picture, and the car
  it names is visibly different in colour from every other candidate (`distinct_cars`, from the colour
  signature stored with each reference picture when it was taken; a picture without colour, an infrared night
  picture, says nothing about colour);
* otherwise only **orders the question's buttons**: the car it names first;
* names no car (`none`, or a car that is no candidate) and counts for nothing.

It never switches away from a person's answer: it is only asked, and only applied, while no one has answered
and nothing has decided.

Timing (`may_query`): at most one query per plug-in, plus one retry after an error, and only in the window
before the question is asked; a query that takes longer than `QUERY_TIMEOUT_S` is ignored (the question is
asked as without a camera).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: A query (snapshot, crop and the AI Task's answer) that takes longer than this is ignored.
QUERY_TIMEOUT_S: Final = 30.0
#: One query, and one retry after an error (not after a timeout: a slow model is slow again).
QUERY_ATTEMPTS: Final = 2

CONFIDENCE_HIGH: Final = "high"
CONFIDENCE_MEDIUM: Final = "medium"
CONFIDENCE_LOW: Final = "low"
CONFIDENCES: Final = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)
#: The model's answer for no car, or none of the reference cars.
ANSWER_NONE: Final = "none"

#: Two reference pictures differ in colour when their average colours (red, green, blue, each 0-1) are at least
#: this far apart: black and white, red and silver do; dark blue and dark grey do not.
COLOUR_DISTINCT: Final = 0.25

#: A reference picture's average colour (red, green, blue, each 0-1), or `None` for a picture without colour.
type Signature = tuple[float, float, float] | None

PICTURE_DAY: Final = "day"
PICTURE_NIGHT: Final = "night"
PICTURE_KINDS: Final = (PICTURE_DAY, PICTURE_NIGHT)


@dataclass(frozen=True, slots=True)
class CameraVerdict:
    """What the camera's answer counts for: the car it decides (`None`: it decides nothing) and the car the
    question shows first (`None`: the order stays the evidence's)."""

    decides: str | None = None
    prefers: str | None = None


def colour_distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.dist(first, second)


def distinct_cars(first: Sequence[Signature], second: Sequence[Signature]) -> bool:
    """Whether two cars' reference pictures differ in colour: every coloured picture of one is at least
    `COLOUR_DISTINCT` from every coloured picture of the other, and each car has at least one."""
    ours = [signature for signature in first if signature is not None]
    theirs = [signature for signature in second if signature is not None]
    if not ours or not theirs:
        return False
    return all(colour_distance(a, b) >= COLOUR_DISTINCT for a in ours for b in theirs)


def camera_verdict(
    answer: str | None,
    confidence: str | None,
    candidates: Sequence[str],
    references: Mapping[str, Sequence[Signature]],
    now: Signature,
) -> CameraVerdict:
    """What the camera's `answer` (a candidate, or `None` for none) at `confidence` counts for among the
    `candidates` the cars' own evidence left, given each car's reference pictures' signatures (a car missing
    from `references`, or with an empty list, has no reference picture). `now` is the colour of the picture now
    (`None` at night: an infrared picture has none, so the camera never decides then), and it must be nearest
    the named car's reference colour among the candidates: a model that names the red car for a white crop does
    not decide."""
    if answer is None or answer not in candidates:
        return CameraVerdict()
    if confidence != CONFIDENCE_HIGH or now is None:
        return CameraVerdict(prefers=answer)
    if nearest_car(now, candidates, references) != answer:
        return CameraVerdict(prefers=answer)
    if any(not references.get(car) for car in candidates):
        # A car the camera cannot recognise may be the one standing there.
        return CameraVerdict(prefers=answer)
    if all(distinct_cars(references[answer], references[other]) for other in candidates if other != answer):
        return CameraVerdict(decides=answer, prefers=answer)
    return CameraVerdict(prefers=answer)


def nearest_car(now: tuple[float, float, float], candidates: Sequence[str], references: Mapping[str, Sequence[Signature]]) -> str | None:
    """The candidate whose nearest coloured reference picture is nearest `now`, or `None` when that is not one car
    (a tie, or no coloured picture)."""
    distances = {
        car: min((colour_distance(now, signature) for signature in references.get(car, ()) if signature is not None), default=None)
        for car in candidates
    }
    known = sorted((distance, car) for car, distance in distances.items() if distance is not None)
    if not known or (len(known) > 1 and known[0][0] == known[1][0]):
        return None
    return known[0][1]


def may_query(*, attempts: int, failed: bool, waiting: bool, remaining: int, elapsed_s: float, window_s: float) -> bool:
    """Whether to ask the camera now: while waiting (no one answered, nothing decided, no question yet) with two
    or more candidates left, inside the window before the question, for the first time or once more after an
    error."""
    if not waiting or remaining < 2 or elapsed_s >= window_s:
        return False
    if attempts == 0:
        return True
    return failed and attempts < QUERY_ATTEMPTS


# --------------------------------------------------------------------------- the question to the AI Task


def labels_for(candidates: Sequence[str]) -> dict[str, str]:
    """The neutral name each candidate goes by in the question (`car_1`, `car_2`, ...): no device id, name or
    plate leaves Home Assistant."""
    return {f"car_{index}": car for index, car in enumerate(candidates, start=1)}


def answer_structure(labels: Sequence[str]) -> dict[str, Any]:
    """The `structure` of `ai_task.generate_data`: one of the labels or `none`, and a confidence."""
    return {
        "vehicle": {
            "description": "The reference car that is the car in the last picture, or none.",
            "required": True,
            "selector": {"select": {"options": [*labels, ANSWER_NONE]}},
        },
        "confidence": {
            "description": "How sure the answer is.",
            "required": True,
            "selector": {"select": {"options": list(CONFIDENCES)}},
        },
    }


def instructions_for(pictures: Sequence[tuple[str, str]]) -> str:
    """The question, for reference pictures given as (label, kind) in the order they are attached; the picture
    of the parking spot now is attached last."""
    listing = "; ".join(
        f"picture {index}: {label} ({'daylight' if kind == PICTURE_DAY else 'night, may be infrared without colour'})"
        for index, (label, kind) in enumerate(pictures, start=1)
    )
    return (
        "The pictures come from one camera that watches one parking spot at a car charger. "
        f"The first {len(pictures)} are reference pictures of known cars parked at that spot: {listing}. "
        "The last picture is the parking spot now. Which reference car is the car in the last picture? "
        "Compare the shape, roof line, windows, lights and wheels, not the colour (light and infrared change it). "
        f"Answer {ANSWER_NONE} if there is no car in the last picture or none of the reference cars matches. "
        "Say how sure you are: high, medium or low."
    )


def parse_answer(data: Any, labels: Mapping[str, str]) -> tuple[str | None, str | None]:
    """The AI Task's `data` as (the candidate it names or `None`, its confidence or `None`). Anything it cannot
    read is no answer."""
    if not isinstance(data, Mapping):
        return None, None
    vehicle = data.get("vehicle")
    confidence = data.get("confidence")
    confidence = confidence.strip().lower() if isinstance(confidence, str) else None
    if confidence not in CONFIDENCES:
        confidence = None
    if not isinstance(vehicle, str):
        return None, confidence
    return labels.get(vehicle.strip()), confidence
