"""What a target's completion tells the phone: the level the stop was taken on, never an estimate called reached.

The field case of 12 October: the Mazda's charge was stopped on an estimate from delivered energy of 89.98 % with a
target of 85 %, and the phone said "the target 85 % was reached"; the car later read 87 %. Now the completion record
carries the level, whether it was read or estimated and the age of the reading, and the notice says which:

* a reading at or above the target: the target, and what the car shows;
* an estimate: the level, rounded and called an estimate, beside the target;
* a reading below the target (a car that ended the charge just short of it): the level, below the target.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.notifications.messages import compose
from custom_components.spotnav.texts import languages

from tests.test_plan_car_ended import _charging, _later, Car
from custom_components.spotnav.execution.plan_car_ended import PLAN_CAR_IDLE_S


def _complete(language: str, **facts: Any) -> str:
    """The message, with the no-break space before a percent sign written as a plain one."""
    message = compose("charge_complete", "Garage", {"reason": "target", "target_percent": 85, **facts}, language)[1]
    return message.replace("\u00a0", " ")


@pytest.mark.parametrize(
    ("language", "facts", "message"),
    [
        ("sv", {"level_percent": 86, "basis": "reading"}, "Laddningen är klar: målet 85 % är nått (bilen visar 86 %)."),
        ("en", {"level_percent": 86, "basis": "reading"}, "Charging complete: the target 85% was reached (the car shows 86%)."),
        ("sv", {"level_percent": 85, "basis": "reading"}, "Laddningen är klar: målet 85 % är nått (bilen visar 85 %)."),
        (
            "sv",
            {"level_percent": 89.9847697183152, "basis": "estimate"},
            "Laddningen stoppades vid 90 % (uppskattat), målet är 85 %.",
        ),
        (
            "en",
            {"level_percent": 89.9847697183152, "basis": "estimate"},
            "Charging stopped at 90% (estimated), the target is 85%.",
        ),
        ("sv", {"level_percent": 84, "basis": "reading"}, "Bilen slutade ladda vid 84 %, strax under målet 85 %."),
        ("en", {"level_percent": 84, "basis": "reading"}, "The car stopped charging at 84%, just below the target 85%."),
        # An estimate below the target (a car that ended the charge within the estimate's margin) is an estimate.
        ("sv", {"level_percent": 83.2, "basis": "estimate"}, "Laddningen stoppades vid 83 % (uppskattat), målet är 85 %."),
        # No level known: the target alone, as before.
        ("sv", {}, "Laddningen är klar: målet 85 % är nått."),
        ("en", {"level_percent": None, "basis": "reading"}, "Charging complete: the target 85% was reached."),
    ],
)
def test_a_targets_completion_says_what_the_stop_was_taken_on(language: str, facts: dict, message: str) -> None:
    assert _complete(language, **facts) == message


def test_every_language_words_every_target_completion() -> None:
    for language in languages():
        for facts in (
            {"level_percent": 86, "basis": "reading"},
            {"level_percent": 90.4, "basis": "estimate"},
            {"level_percent": 84, "basis": "reading"},
        ):
            message = _complete(language, **facts)
            assert "85" in message and "{" not in message and len(message) < 120, (language, message)


async def test_a_car_that_ended_just_below_the_target_is_recorded_with_its_level_and_basis(
    hass: HomeAssistant, freezer: Any
) -> None:
    car = Car(84.0, estimated=False)
    controller, halo, _ = await _charging(hass, freezer, car, target=85.0)
    halo.amps(0.0)
    await _later(hass, freezer, PLAN_CAR_IDLE_S + 30)
    completion = controller.completion_record
    assert completion is not None and completion["reason"] == "target"
    assert completion["soc_percent"] == 84.0 and completion["basis"] == "reading"
    assert completion["reading_age_s"] == 6 * 3600.0
    facts = {"level_percent": completion["soc_percent"], "basis": completion["basis"]}
    assert _complete("sv", **facts) == "Bilen slutade ladda vid 84 %, strax under målet 85 %."
    await controller.async_shutdown()


async def test_a_car_that_ended_on_an_estimate_is_recorded_as_an_estimate(hass: HomeAssistant, freezer: Any) -> None:
    car = Car(83.0, estimated=True)
    controller, halo, _ = await _charging(hass, freezer, car, target=85.0)
    halo.amps(0.0)
    await _later(hass, freezer, PLAN_CAR_IDLE_S + 30)
    completion = controller.completion_record
    assert completion is not None and completion["reason"] == "target" and completion["basis"] == "estimate"
    facts = {"level_percent": completion["soc_percent"], "basis": completion["basis"]}
    assert _complete("en", **facts) == "Charging stopped at 83% (estimated), the target is 85%."
    await controller.async_shutdown()
