"""The status while the plan waits for the car's level after the car ended its charge short of the target:
`car_ended_waiting`, worded by the card and by an app that reads it, and the line an app released before it already
words in its place (`waiting_for_vehicle_update`)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.spotnav.api.dashboard import _car_ended_facts
from custom_components.spotnav.api.webhook import _for_app, APP_READS_CAR_ENDED_WAITING
from custom_components.spotnav.planning.status_compose import STATUS_CODES


def _body() -> dict:
    return {
        "ok": True,
        "status": {
            "tone": "normal",
            "lines": [{"code": "car_ended_waiting", "params": {"soc_percent": 86.2, "estimated": True}}],
        },
    }


def test_the_line_and_its_read() -> None:
    assert STATUS_CODES["car_ended_waiting"] == ("normal", ("soc_percent", "estimated"))
    assert APP_READS_CAR_ENDED_WAITING == "car_ended_waiting"


def test_an_older_app_gets_the_wait_for_the_cars_level_it_already_words() -> None:
    body = _body()
    older = _for_app(body, {"action": "dashboard", "reads": ["min_soc", "balancing_paused"]})
    assert older["status"]["lines"] == [{"code": "waiting_for_vehicle_update", "params": {}}]
    assert body["status"]["lines"][0]["code"] == "car_ended_waiting", "a copy"


def test_an_app_that_reads_it_gets_it() -> None:
    body = _body()
    assert _for_app(body, {"action": "dashboard", "reads": ["car_ended_waiting"]}) == body


def test_a_target_raised_above_the_one_the_car_ended_at_waits_no_more() -> None:
    wait = {"soc_percent": 86.2, "estimated": True, "target_percent": 91.0}
    settings = SimpleNamespace(target=SimpleNamespace(target_percent=91.0))
    assert _car_ended_facts(wait, settings) == {
        "car_ended_waiting": True,
        "car_ended_soc_percent": 86.2,
        "car_ended_estimated": True,
    }
    raised = SimpleNamespace(target=SimpleNamespace(target_percent=95.0))
    assert _car_ended_facts(wait, raised) == {}
    assert _car_ended_facts(None, settings) == {}
