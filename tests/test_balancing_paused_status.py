"""The status while load balancing holds the plan's charge back inside its window: `balancing_paused`, worded by the
card and by an app that reads it, and the line an app released before it already words in its place."""

from __future__ import annotations

from custom_components.spotnav.api.webhook import _for_app, APP_READS_BALANCING_PAUSED
from custom_components.spotnav.planning.status_compose import STATUS_CODES


def _body(cause: str = "battery_shares_fuse") -> dict:
    return {
        "ok": True,
        "status": {
            "tone": "normal",
            "lines": [
                {"code": "balancing_paused", "params": {"retry_at": "2026-10-09T02:30:22+00:00", "cause": cause}},
                {"code": "plan_energy", "params": {"kwh": 13.0}},
            ],
        },
    }


def test_the_line_and_its_read() -> None:
    assert STATUS_CODES["balancing_paused"] == ("normal", ("retry_at", "cause"))
    assert APP_READS_BALANCING_PAUSED == "balancing_paused"


def test_an_older_app_gets_the_limited_line_at_nothing() -> None:
    body = _body()
    older = _for_app(body, {"action": "dashboard", "reads": ["min_soc"]})
    assert older["status"]["lines"] == [
        {"code": "load_balancing_limited", "params": {"limit_a": 0, "phase": None, "cause": "battery_shares_fuse"}},
        {"code": "plan_energy", "params": {"kwh": 13.0}},
    ]
    assert body["status"]["lines"][0]["code"] == "balancing_paused", "a copy"


def test_an_app_that_reads_it_gets_it() -> None:
    body = _body("house_consumption")
    assert _for_app(body, {"action": "dashboard", "reads": ["balancing_paused"]}) == body
