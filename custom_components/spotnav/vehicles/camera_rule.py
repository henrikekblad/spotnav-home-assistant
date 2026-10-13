"""What the camera's answer counts for, when it is asked, and what it is asked: pure, no Home Assistant.

The camera is one source of evidence for vehicle identification (`identification.py`). It is asked only while
the cars' own reports have not decided, and its answer:

* **decides alone** only when it is high confidence, the picture now has colour (never at night: an infrared
  picture is grey or evenly tinted) nearest the named car's reference colour, every candidate left has a
  reference picture, and the car
  it names is visibly different in colour from every other candidate (`distinct_cars`, from the colour
  signature stored with each reference picture when it was taken; a picture without colour, an infrared night
  picture, says nothing about colour);

Colour is compared between two pictures of one camera and one frame (`colour_distance`): where they differ is
where the cars differ (the ground, plants, posts and a time stamp stay and cancel out), and there their average
colours are compared. So a dark blue car on paving is far from the empty spot, and the same car parked a little
further on is near itself.
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
from functools import lru_cache
from dataclasses import dataclass
from typing import Any, Final

#: A query (snapshot, crop and the AI Task's answer, including loading the model) that takes longer than this is
#: ignored. A 7B vision model on a home server answered in about 20 s, and in just over 30 s when it first had to be
#: loaded again; two minutes leaves room for that and still ends before the question at three minutes.
QUERY_TIMEOUT_S: Final = 120.0
#: One query, and one retry after an error (not after a timeout: a slow model is slow again).
QUERY_ATTEMPTS: Final = 2

CONFIDENCE_HIGH: Final = "high"
CONFIDENCE_MEDIUM: Final = "medium"
CONFIDENCE_LOW: Final = "low"
CONFIDENCES: Final = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)
#: The model's answer for no car, or none of the reference cars.
ANSWER_NONE: Final = "none"

#: Two pictures differ in colour when what differs between them is at least this far apart in average colour (red,
#: green, blue, each 0-1; `colour_distance`): black and white, red and silver do; dark blue and dark grey do not.
COLOUR_DISTINCT: Final = 0.25

#: A picture's colour signature: the crop shrunk to `GRID` x `GRID` pixels, each the average colour of its area, as
#: red, green, blue bytes row by row; `None` for a picture without colour.
type Signature = bytes | None
GRID: Final = 32
#: `colour_distance`: a pixel differs between two pictures when its colours are at least `CHANGED` apart (after
#: evening out the light); two pictures with less than `CHANGED_SHARE` of their pixels differing are the same.
CHANGED: Final = 0.12
CHANGED_SHARE: Final = 0.03
#: `colour_distance`: what differs is only the same thing moved (a car parked a little further on) when its colour
#: is found within `MOVED_REACH` pixels in the other picture (half of what differs within `MOVED` of its colour).
MOVED_REACH: Final = 3
MOVED: Final = 0.15
#: `_light`: the light of one picture is evened out to the other's when at least `LIGHT_SHARE` of the pixels agree
#: on how much lighter or darker it is (within `_LIGHT_STEP` on a log scale): the ground, which stays; the factor
#: stays within `LIGHT_RANGE`.
LIGHT_SHARE: Final = 0.25
LIGHT_RANGE: Final = (0.5, 2.0)
_LIGHT_STEP: Final = 0.05
#: A pixel darker than this (the sum of red, green and blue, 0-765) says nothing about the light.
_LIGHT_DARK: Final = 30

PICTURE_DAY: Final = "day"
PICTURE_NIGHT: Final = "night"
PICTURE_KINDS: Final = (PICTURE_DAY, PICTURE_NIGHT)


@dataclass(frozen=True, slots=True)
class CameraVerdict:
    """What the camera's answer counts for: the car it decides (`None`: it decides nothing) and the car the
    question shows first (`None`: the order stays the evidence's)."""

    decides: str | None = None
    prefers: str | None = None


def _pixels(signature: bytes) -> list[tuple[int, int, int]]:
    return [(signature[index], signature[index + 1], signature[index + 2]) for index in range(0, len(signature), 3)]


def _light(first: list[tuple[int, int, int]], second: list[tuple[int, int, int]]) -> float:
    """How much lighter `second` is than `first`: the factor most pixels agree on (the ground, which stays when a car
    comes or goes), or 1 when too few agree (nothing stays: no factor can be told from a change of car)."""
    bins: dict[int, list[float]] = {}
    for one, other in zip(first, second, strict=True):
        light_one, light_other = sum(one), sum(other)
        if light_one > _LIGHT_DARK and light_other > _LIGHT_DARK:
            ratio = math.log(light_other / light_one)
            bins.setdefault(round(ratio / _LIGHT_STEP), []).append(ratio)
    if not bins:
        return 1.0
    peak = max(bins, key=lambda key: (len(bins[key]), -abs(key)))
    agree = [ratio for key in (peak - 1, peak, peak + 1) for ratio in bins.get(key, ())]
    if len(agree) < LIGHT_SHARE * len(first):
        return 1.0
    return min(max(math.exp(sum(agree) / len(agree)), LIGHT_RANGE[0]), LIGHT_RANGE[1])


def _unmoved(first: list[tuple[int, int, int]], second: list[tuple[int, int, int]], changed: list[int], side: int) -> float:
    """How far what differs in `first` is from the nearest colour within `MOVED_REACH` pixels in `second` (the median
    over what differs, 0-1): small when the same colours only moved."""
    nearest = []
    for index in changed:
        row, column = divmod(index, side)
        red, green, blue = first[index]
        nearest.append(
            min(
                (red - other[0]) ** 2 + (green - other[1]) ** 2 + (blue - other[2]) ** 2
                for y in range(max(0, row - MOVED_REACH), min(side, row + MOVED_REACH + 1))
                for x in range(max(0, column - MOVED_REACH), min(side, column + MOVED_REACH + 1))
                for other in (second[y * side + x],)
            )
        )
    nearest.sort()
    return math.sqrt(nearest[len(nearest) // 2]) / 255


@lru_cache(maxsize=64)
def colour_distance(first: bytes, second: bytes) -> float:
    """How different in colour two pictures of one camera and frame are (0-1, three decimals).

    The second picture's light is evened out to the first's (`_light`). The pixels whose colours differ by at least
    `CHANGED` are where the cars differ; what stays (the ground, plants, a post, a time stamp) cancels out. With
    less than `CHANGED_SHARE` of the pixels differing the pictures are the same (0). Otherwise it is the distance
    between the two pictures' average colours over the pixels that differ: a car against the empty spot, or one car
    against another. When what differs is only the same colours moved a little (`_unmoved` within `MOVED` both ways:
    a car parked a little further on, or a longer car of the same colour) it is that smaller distance instead.
    Pictures of different sizes cannot be compared: 0, the safe side (not different).
    """
    if len(first) != len(second) or not first:
        return 0.0
    ours = _pixels(first)
    side = math.isqrt(len(ours))
    if side * side != len(ours):
        return 0.0
    light = _light(ours, _pixels(second))
    theirs = [
        (min(255, round(red / light)), min(255, round(green / light)), min(255, round(blue / light)))
        for red, green, blue in _pixels(second)
    ]
    limit = (CHANGED * 255) ** 2
    changed = [
        index
        for index, (one, other) in enumerate(zip(ours, theirs, strict=True))
        if (one[0] - other[0]) ** 2 + (one[1] - other[1]) ** 2 + (one[2] - other[2]) ** 2 >= limit
    ]
    if len(changed) < CHANGED_SHARE * len(ours):
        return 0.0
    count = len(changed) * 255
    distance = math.dist(
        [sum(ours[index][channel] for index in changed) / count for channel in range(3)],
        [sum(theirs[index][channel] for index in changed) / count for channel in range(3)],
    )
    if distance >= COLOUR_DISTINCT:
        moved = max(_unmoved(ours, theirs, changed, side), _unmoved(theirs, ours, changed, side))
        if moved < MOVED:
            distance = moved
    return round(distance, 3)


def distinct_cars(first: Sequence[Signature], second: Sequence[Signature]) -> bool:
    """Whether two cars' reference pictures differ in colour: every coloured picture of one is at least
    `COLOUR_DISTINCT` from every coloured picture of the other, and each car has at least one."""
    ours = [signature for signature in first if signature is not None]
    theirs = [signature for signature in second if signature is not None]
    if not ours or not theirs:
        return False
    return all(colour_distance(a, b) >= COLOUR_DISTINCT for a in ours for b in theirs)


#: Why the camera's answer did or did not decide alone (`camera_reason`), for the diagnostics.
REASON_DECIDED: Final = "decided"
REASON_ANSWER_NONE: Final = "answer_none"
REASON_CONFIDENCE_LOW: Final = "confidence_low"
REASON_NIGHT: Final = "night"
REASON_COLOUR_MISMATCH: Final = "colour_mismatch"
REASON_MISSING_REFERENCE: Final = "missing_reference"
REASON_SIMILAR_COLOUR: Final = "similar_colour"


def camera_reason(
    answer: str | None,
    confidence: str | None,
    candidates: Sequence[str],
    references: Mapping[str, Sequence[Signature]],
    now: Signature,
) -> tuple[str, float | None]:
    """Why the camera's `answer` (a candidate, or `None` for none) at `confidence` does or does not decide alone among
    the `candidates` the cars' own evidence left, given each car's reference pictures' signatures (a car missing
    from `references`, or with an empty list, has no reference picture) and `now`, the colour of the picture now
    (`None` at night: an infrared picture has none). With `similar_colour`, the colour distance from the named car
    to the nearest other car (`None` when one of them has no coloured picture); else `None`.

    In order: `answer_none` (no car, or one that is no candidate: it counts for nothing), `confidence_low` (not
    "high"), `night`, `colour_mismatch` (another car is nearer the colour now than the named car: a model that
    names the red car for a white crop does not decide), `missing_reference` (a car the camera cannot recognise
    may be the one standing there), `similar_colour` (another car is not clearly different in colour, or is as
    near the colour now), else `decided`.
    """
    if answer is None or answer not in candidates:
        return REASON_ANSWER_NONE, None
    if confidence != CONFIDENCE_HIGH:
        return REASON_CONFIDENCE_LOW, None
    if now is None:
        return REASON_NIGHT, None
    distances = _distances_now(now, candidates, references)
    mine = distances.get(answer)
    if mine is None or any(distance < mine for distance in distances.values()):
        return REASON_COLOUR_MISMATCH, None
    if any(not references.get(car) for car in candidates):
        return REASON_MISSING_REFERENCE, None
    others = [other for other in candidates if other != answer]
    # Another car as near the colour now (both the same as the picture now) is a car of similar colour.
    tied = any(distances.get(other) == mine for other in others)
    if not tied and all(distinct_cars(references[answer], references[other]) for other in others):
        return REASON_DECIDED, None
    ours = [signature for signature in references[answer] if signature is not None]
    between = [
        colour_distance(a, b)
        for other in others
        for a in ours
        for b in references[other]
        if b is not None
    ]
    return REASON_SIMILAR_COLOUR, (round(min(between), 3) if between else None)


def camera_verdict(
    answer: str | None,
    confidence: str | None,
    candidates: Sequence[str],
    references: Mapping[str, Sequence[Signature]],
    now: Signature,
) -> CameraVerdict:
    """What the camera's `answer` counts for (`camera_reason` says why): it decides alone only for `decided`, names
    no car for `answer_none`, and otherwise only puts its car first."""
    reason, _ = camera_reason(answer, confidence, candidates, references, now)
    if reason == REASON_ANSWER_NONE:
        return CameraVerdict()
    if reason == REASON_DECIDED:
        return CameraVerdict(decides=answer, prefers=answer)
    return CameraVerdict(prefers=answer)


def nearest_car(now: bytes, candidates: Sequence[str], references: Mapping[str, Sequence[Signature]]) -> str | None:
    """The candidate whose nearest coloured reference picture is nearest `now`, or `None` when that is not one car
    (a tie, or no coloured picture)."""
    known = sorted((distance, car) for car, distance in _distances_now(now, candidates, references).items())
    if not known or (len(known) > 1 and known[0][0] == known[1][0]):
        return None
    return known[0][1]


def _distances_now(now: bytes, candidates: Sequence[str], references: Mapping[str, Sequence[Signature]]) -> dict[str, float]:
    """Each candidate's nearest coloured reference picture's distance from `now` (a car without one is left out)."""
    distances = {
        car: min((colour_distance(now, signature) for signature in references.get(car, ()) if signature is not None), default=None)
        for car in candidates
    }
    return {car: distance for car, distance in distances.items() if distance is not None}


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
            "description": "The reference car that is the car in picture 1, or none.",
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
    """The question, for reference pictures given as (label, kind) in the order they are attached after the picture
    of the parking spot now, which goes first: a vision model asked about "the last picture" was seen to answer none
    when an empty-spot reference came right before it, and to name the right car with the picture now first."""
    listing = "; ".join(
        f"picture {index}: {label} ({'daylight' if kind == PICTURE_DAY else 'night, may be infrared without colour'})"
        for index, (label, kind) in enumerate(pictures, start=2)
    )
    return (
        "The pictures come from one camera that watches one parking spot at a car charger. "
        "Picture 1 is the parking spot now. "
        f"Pictures 2 to {len(pictures) + 1} are reference pictures of known cars parked at that spot: {listing}. "
        "Which reference car is the car in picture 1? "
        "Compare the shape, roof line, windows, lights and wheels, not the colour (light and infrared change it). "
        f"Answer {ANSWER_NONE} if there is no car in picture 1 or none of the reference cars matches. "
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
