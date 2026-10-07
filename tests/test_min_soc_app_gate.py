"""The car's minimum charge level for an app released before it: the webhook leaves the vehicle rows' `min_percent`
out, and says the ordinary charging line in place of `min_soc_charging`, unless the request reads `min_soc`."""

from __future__ import annotations

from custom_components.spotnav.api.webhook import _for_app, _vehicle_answer_for_app, APP_READS_MIN_SOC


def _body() -> dict:
    return {
        "ok": True,
        "status": {
            "tone": "normal",
            "lines": [
                {"code": "min_soc_charging", "params": {"percent": 30}},
                {"code": "plan_energy", "params": {"kwh": 12.0}},
            ],
        },
        "vehicles": [
            {"id": "a", "name": "Niro", "target_percent": 80.0, "min_percent": 30},
            {"id": "b", "name": "EV6", "target_percent": None, "min_percent": None},
        ],
    }


def test_the_read_is_named_min_soc() -> None:
    assert APP_READS_MIN_SOC == "min_soc"


def test_an_older_app_gets_the_charging_line_and_no_minimum_level() -> None:
    body = _body()
    older = _for_app(body, {"action": "dashboard", "reads": ["solar_no_car_status"]})
    assert older["status"]["lines"] == [
        {"code": "charging_now", "params": {"until": None}},
        {"code": "plan_energy", "params": {"kwh": 12.0}},
    ]
    assert [sorted(row) for row in older["vehicles"]] == [["id", "name", "target_percent"]] * 2
    assert body["vehicles"][0]["min_percent"] == 30, "a copy"
    assert body["status"]["lines"][0]["code"] == "min_soc_charging", "a copy"


def test_an_app_that_reads_it_gets_everything() -> None:
    body = _body()
    assert _for_app(body, {"action": "dashboard", "reads": ["min_soc"]}) == body


def test_the_update_vehicle_answer_is_gated_the_same_way() -> None:
    answer = {"ok": True, "vehicle": {"id": "a", "target_percent": 80.0, "min_percent": 30}}
    assert _vehicle_answer_for_app(answer, {"reads": []}) == {"ok": True, "vehicle": {"id": "a", "target_percent": 80.0}}
    assert _vehicle_answer_for_app(answer, {"reads": ["min_soc"]}) == answer
    assert _vehicle_answer_for_app({"ok": False, "vehicle": None}, {}) == {"ok": False, "vehicle": None}
