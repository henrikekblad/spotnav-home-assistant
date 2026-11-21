"""The beta tester mode (`beta.py`), its command (`spotnav/beta`), the battery's shadow-only default per recipe and the
hardware test's runner (`hardware_test.py`) against fake engines."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav import beta as beta_module, hardware_test, support_report
from custom_components.spotnav.battery import commands, gate
from custom_components.spotnav.beta import (
    BETA_DAYS,
    Beta,
    BetaState,
    expire,
    learning_gate_open,
    night_time,
    parse_state,
    switched,
    view,
)
from custom_components.spotnav.debug_bundle import async_build_debug_bundle
from custom_components.spotnav.hardware_test import HardwareTest, allowance, result_of
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.site.charger_test_hold import ChargerTestHold
from custom_components.spotnav.support_report import ReportResult

from .world import admin, non_admin, setup_charger_and_site, ws_call

T0 = datetime.fromisoformat("2026-11-19T12:00:00+01:00")


# ---- the state, pure


def test_ticking_starts_a_period_and_it_ends_by_itself_after_fourteen_days() -> None:
    state = switched(BetaState(), True, T0)
    assert state.on and state.started_at == T0 and state.ends_at() == T0 + timedelta(days=BETA_DAYS)
    assert expire(state, T0 + timedelta(days=13)) == state
    ended = expire(state, T0 + timedelta(days=BETA_DAYS))
    assert not ended.on and ended.completed and ended.ended_at == T0 + timedelta(days=BETA_DAYS)
    # The card's one line: the period ran out, nothing is sent any more.
    assert view(ended)["ended"] is True and view(state)["ended"] is False


def test_unticking_ends_the_period_at_once_without_completing_it() -> None:
    state = switched(switched(BetaState(), True, T0), False, T0 + timedelta(days=2))
    assert not state.on and not state.completed and state.ended_at == T0 + timedelta(days=2)
    assert view(state)["ended"] is False
    again = switched(state, True, T0 + timedelta(days=3))
    assert again.on and again.started_at == T0 + timedelta(days=3)


def test_a_stored_state_is_read_strictly() -> None:
    assert parse_state(None) == BetaState()
    assert parse_state({"on": True}) == BetaState(), "on without a start is off"
    state = parse_state({"on": True, "started_at": T0.isoformat(), "email": "not an address", "completed": "yes"})
    assert state.on and state.email is None and not state.completed
    assert parse_state(state.as_stored()) == state


def test_each_installation_reports_at_a_night_minute_of_its_own() -> None:
    times = {night_time(f"{seed:012x}" + "0" * 52) for seed in range(0, 2**40, 2**31 + 7)}
    assert len(times) > 50
    assert all(1 <= hour < 5 and 0 <= minute < 60 and 0 <= second < 60 for hour, minute, second in times)
    assert night_time("not hex") == (1, 0, 0)


# ---- the mode on Home Assistant


_SENT = ReportResult(ref="SN-BETA")


class _Sender:
    def __init__(self, result: ReportResult | Exception = _SENT) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result

    async def __call__(self, hass: HomeAssistant, message: str, email: str | None, client: Any, *, kind: str) -> Any:
        self.calls.append({"message": message, "email": email, "kind": kind})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _beta(hass: HomeAssistant, sender: _Sender) -> Beta:
    beta = Beta(hass, sender=sender)
    await beta.async_load()
    domain_data(hass).beta = beta
    return beta


async def test_the_daily_report_goes_once_a_night_while_on_and_a_failure_is_skipped(hass: HomeAssistant) -> None:
    sender = _Sender()
    beta = await _beta(hass, sender)
    assert not beta.state.on and not learning_gate_open(hass, None)
    now = dt_util.now()
    assert await beta.async_daily(now) is None and sender.calls == [], "nothing is sent while off"
    await beta.async_set(on=True, email="me@example.com")
    assert learning_gate_open(hass, "site") is True
    assert await beta.async_daily(now) == "SN-BETA"
    assert sender.calls == [{"message": "", "email": "me@example.com", "kind": "beta_daily"}]
    assert await beta.async_daily(now + timedelta(minutes=5)) is None, "one a night"
    sender.result = ReportResult(error="spotnav_report_rate_limited", retry_after_s=3600)
    assert await beta.async_daily(now + timedelta(days=1)) is None
    assert len(sender.calls) == 2
    # The failed night is not tried again; the next night sends the next one.
    assert await beta.async_daily(now + timedelta(days=1, minutes=30)) is None and len(sender.calls) == 2
    sender.result = RuntimeError("bundle failed")
    assert await beta.async_daily(now + timedelta(days=2)) is None
    await beta.async_set(on=False)
    assert await beta.async_daily(now + timedelta(days=3)) is None and len(sender.calls) == 3
    assert not learning_gate_open(hass, None), "an early untick closes the learning gates again"
    await beta.async_stop()


async def test_the_period_ends_by_itself_and_keeps_the_learning_gates_open(hass: HomeAssistant, hass_storage) -> None:
    started = dt_util.now() - timedelta(days=BETA_DAYS, minutes=1)
    hass_storage[beta_module.STORE_KEY] = {
        "version": 1, "key": beta_module.STORE_KEY,
        "data": {"on": True, "started_at": started.isoformat(), "email": "me@example.com"},
    }
    sender = _Sender()
    beta = await _beta(hass, sender)
    assert not beta.state.on and beta.state.completed
    assert await beta.async_daily(dt_util.now()) is None and sender.calls == []
    assert learning_gate_open(hass, None) is True, "control a period started stays on"
    await beta.async_stop()


async def test_the_timer_and_a_report_in_flight_end_at_the_stop(hass: HomeAssistant) -> None:
    release = asyncio.Event()

    async def slow(*_args: Any, **_kwargs: Any) -> ReportResult:
        await release.wait()
        return ReportResult(ref="SN-SLOW")

    beta = Beta(hass, sender=slow)
    await beta.async_load()
    await beta.async_set(on=True)
    beta._on_night(dt_util.now())  # noqa: SLF001 - the timer's callback
    await asyncio.sleep(0)
    assert beta._task is not None and not beta._task.done()  # noqa: SLF001
    await beta.async_stop()
    assert beta._task is None and beta._timer_cancel is None  # noqa: SLF001


# ---- the command


async def test_the_command_is_for_administrators_and_sets_the_box_and_the_address(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    client = await admin(hass, hass_ws_client)
    frame = await ws_call(client, {"type": "spotnav/beta", "api_version": 1})
    result = frame["result"]
    assert result["ok"] and result["beta"] == {
        "on": False, "started_at": None, "ends_at": None, "ended": False, "email": None
    }
    assert result["test"] is None
    frame = await ws_call(client, {"type": "spotnav/beta", "api_version": 1, "action": "set",
                                   "changes": {"email": "nope"}})
    assert frame["result"]["error"] == "spotnav_report_invalid_email"
    frame = await ws_call(client, {"type": "spotnav/beta", "api_version": 1, "action": "set",
                                   "changes": {"on": "yes"}})
    assert frame["result"]["error"] == "spotnav_invalid_value"
    frame = await ws_call(client, {"type": "spotnav/beta", "api_version": 1, "action": "set", "site_id": site.entry_id,
                                   "changes": {"on": True, "email": " me@example.com "}})
    result = frame["result"]
    assert result["ok"] and result["beta"]["on"] and result["beta"]["email"] == "me@example.com"
    assert result["beta"]["ends_at"] is not None
    # The site's test: nothing on this site to test (no battery, no heat control).
    assert result["test"]["state"] == "idle" and result["test"]["blocked"] == "nothing_to_test"
    frame = await ws_call(client, {"type": "spotnav/beta", "api_version": 1, "action": "start_test",
                                   "site_id": site.entry_id})
    assert frame["result"]["error"] == "spotnav_test_blocked"
    frame = await ws_call(client, {"type": "spotnav/beta", "api_version": 1, "action": "send_test",
                                   "site_id": site.entry_id})
    assert frame["result"]["error"] == "spotnav_no_test"
    reader = await non_admin(hass, hass_ws_client, hass_read_only_access_token)
    frame = await ws_call(reader, {"type": "spotnav/beta", "api_version": 1})
    assert frame["result"]["error"] == "spotnav_not_admin" and frame["result"]["beta"] is None
    frame = await ws_call(client, {"type": "spotnav/beta", "api_version": 2})
    assert frame["error"]["code"] == "spotnav_unsupported_api_version"
    # The bundle says the mode is on and that an address was given, never the address.
    bundle = await async_build_debug_bundle(hass)
    assert bundle["beta"]["on"] is True and bundle["beta"]["email_given"] is True
    assert "me@example.com" not in str(bundle) and bundle["hardware_test"] is None


# ---- the battery's shadow-only default


def test_shadow_only_is_off_by_default_only_for_a_field_tested_recipe_or_a_beta_tester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No recipe is field tested yet: Sigenergy's flips once the owner's on-site tests are rerun.
    for brand in ("sigenergy", "victron", "solinteg", "ferroamp", "goodwe", "tesla", "solaredge", "huawei", "solax", None):
        assert commands.field_tested(brand) is False, brand
    tested = replace(commands.SIGENERGY_CONTROL, field_tested=True)
    monkeypatch.setattr(commands, "CONTROLS", (tested, *commands.CONTROLS[1:]))
    assert commands.field_tested("sigenergy") is True, "the recipe's field decides, no brand branch"
    assert gate.shadow_default(field_tested=True, beta=False) is False
    assert gate.shadow_default(field_tested=False, beta=True) is False
    assert gate.shadow_default(field_tested=False, beta=False) is True
    # A stored value is kept whatever the default (a site that set it keeps it).
    assert gate.Options.from_config({gate.CONF_SHADOW_ONLY: True}, shadow_default=False).shadow_only is True
    assert gate.Options.from_config({gate.CONF_SHADOW_ONLY: False}, shadow_default=True).shadow_only is False
    assert gate.Options.from_config({}, shadow_default=False).shadow_only is False
    assert gate.Options.from_config({}).shadow_only is True


# ---- the hardware test's runner, against fake engines


class _FakeBattery:
    """Steps through the four modes, one each time the runner reads the progress."""

    def __init__(self, outcomes: dict[str, str] | None = None, refusal: str | None = None, slow: bool = False) -> None:
        self.refusal = refusal
        self.outcomes = outcomes or {"hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed",
                                     "normal": "restored"}
        self.done: dict[str, str] = {}
        self.running = False
        self.stopped: list[str] = []
        self.slow = slow

    def hardware_test_refusal(self) -> str | None:
        return self.refusal

    def firmware_key(self) -> str:
        return "SPC117 | sigen | 2.0.3"

    async def async_hardware_test_start(self) -> str | None:
        self.running = True
        return None

    async def async_hardware_test_stop(self, reason: str) -> None:
        self.stopped.append(reason)
        self.running = False

    def _report(self) -> dict[str, Any]:
        return {"modes": {mode: {"mode": mode, "outcome": outcome} for mode, outcome in self.done.items()}}

    def hardware_test_progress(self) -> dict[str, Any]:
        if self.running and not self.slow:
            for mode in ("hold", "charge", "discharge_house", "normal"):
                if mode not in self.done:
                    self.done[mode] = self.outcomes[mode]
                    if self.outcomes[mode] not in ("obeyed", "restored"):
                        for later in ("charge", "discharge_house"):
                            self.done.setdefault(later, "skipped")
                        self.done["normal"] = "restored"
                    break
            else:
                self.running = False
            if len(self.done) == 4:
                self.running = False
        current = next((mode for mode in ("hold", "charge", "discharge_house", "normal") if mode not in self.done), None)
        return {"running": self.running, "mode": current, "stage": "read", "report": self._report(),
                "holding": self.running, "firmware_key": self.firmware_key()}


class _FakeHeat:
    def __init__(self, refusal: str | None = None, slow: bool = False) -> None:
        self.refusal = refusal
        self.test: dict[str, Any] | None = None
        self.slow = slow

    def test_refusal(self, _now: datetime) -> str | None:
        return self.refusal

    def async_test_start(self, now: datetime) -> str | None:
        self.test = {"started_at": now.isoformat(), "phase": "hold", "engaged_at": now.isoformat(),
                     "released_at": None, "outcome": None}
        return None

    def async_test_stop(self, why: str) -> None:
        if self.test is not None and self.test["phase"] != "done":
            self.test.update(phase="done", outcome=f"aborted:{why}")

    def test_view(self) -> dict[str, Any] | None:
        if self.test is not None and not self.slow and self.test["phase"] != "done":
            self.test.update(phase="done", released_at=dt_util.now().isoformat(), outcome="obeyed")
        return None if self.test is None else dict(self.test)


class _FakeCharger:
    """A charger the test's hold reads and stops (`site/charger_test_hold.py`)."""

    def __init__(self, on: bool = False) -> None:
        self.adapter = SimpleNamespace(platform="chargeamps", path=SimpleNamespace(kind="switch"))
        self.ocpp_target = None
        self.charging = self.charge_control_on = on
        self.control_confirmed_off = not on
        self.start_pending = self.stop_settling = False
        self.stops = self.rearms = 0

    async def async_stop(self, **_kwargs: Any) -> None:
        self.stops += 1

    async def async_rearm(self) -> None:
        self.rearms += 1


async def _runner(
    hass: HomeAssistant, battery: Any, heat: Any, chargers: dict[str, _FakeCharger] | None = None
) -> HardwareTest:
    chargers = chargers or {}
    site = SimpleNamespace(entry_id="site_1", battery_control=battery, heat_control=heat, hass=hass,
                           config={"charger_entry_ids": list(chargers)})
    site.charger_test_hold = ChargerTestHold(site, controller_for=chargers.get)  # type: ignore[arg-type]
    runner = HardwareTest(hass, site)  # type: ignore[arg-type]
    await runner.async_load()
    return runner


async def _finished(runner: HardwareTest) -> None:
    assert runner._task is not None  # noqa: SLF001
    await runner._task  # noqa: SLF001


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware_test, "POLL_S", 0.0)


def test_a_test_runs_once_a_day_and_once_more_after_an_update_or_a_firmware_change() -> None:
    last = {"at": T0.isoformat(), "version": "3.0.0", "firmware_key": "fw1"}
    assert allowance(None, T0, "3.0.0", "fw1") == (None, None)
    assert allowance(last, T0 + timedelta(hours=3), "3.0.0", "fw1") == ("once_a_day", T0 + timedelta(hours=24))
    assert allowance(last, T0 + timedelta(hours=24), "3.0.0", "fw1") == (None, None)
    assert allowance(last, T0 + timedelta(hours=3), "3.0.1", "fw1") == (None, None)
    assert allowance(last, T0 + timedelta(hours=3), "3.0.0", "fw2") == (None, None)
    assert result_of([{"status": "passed"}, {"status": "not_judgeable"}]) == "partial", "an unjudged step is no pass"
    assert result_of([{"status": "passed"}, {"status": "failed"}]) == "failed"


def test_the_result_says_when_a_part_did_not_run() -> None:
    skipped = {"part": "battery", "step": "battery", "status": "skipped", "reason": "needs:outside_control"}
    ran = [{"part": "heat", "step": "engage", "status": "passed"}, {"part": "heat", "step": "release", "status": "passed"}]
    assert result_of([skipped, *ran]) == "partial"
    assert result_of([skipped, {"part": "heat", "step": "heat", "status": "skipped"}]) == "skipped"
    assert result_of([skipped, {"part": "heat", "step": "engage", "status": "failed"}]) == "failed"
    # A step skipped inside a part that ran (a mode after a failure, a release with nothing engaged) is no part skipped.
    assert result_of([{"part": "battery", "step": "charge", "status": "skipped"}, *ran]) == "passed"
    assert result_of([]) == "skipped"


async def test_a_partial_or_skipped_test_leaves_the_day_s_slot(hass: HomeAssistant) -> None:
    battery = _FakeBattery(refusal="needs:outside_control:settings_changed")
    runner = await _runner(hass, battery, _FakeHeat())
    assert await runner.async_start() is None
    await _finished(runner)
    view = runner.view()
    assert view["result"] == "partial" and view["blocked"] is None and view["next_at"] is None
    battery.refusal = None
    assert await runner.async_start() is None, "the battery ready again: the test runs again the same day"
    await _finished(runner)
    assert runner.view()["result"] == "passed" and runner.view()["blocked"] == "once_a_day"


async def test_a_kept_test_from_before_is_judged_again_at_the_load(hass: HomeAssistant, hass_storage) -> None:
    now = dt_util.now()
    hass_storage[hardware_test.store_key("site_1")] = {"version": 1, "key": hardware_test.store_key("site_1"), "data": {
        "last": {"at": now.isoformat(), "version": "3.0.0", "firmware_key": None},
        "record": {"started_at": now.isoformat(), "finished_at": now.isoformat(), "result": "passed", "steps": [
            {"part": "battery", "step": "battery", "status": "skipped", "reason": "needs:outside_control"},
            {"part": "heat", "step": "engage", "status": "passed", "reason": None}]},
        "sent_ref": None}}
    runner = await _runner(hass, _FakeBattery(), _FakeHeat())
    assert runner.record is not None and runner.record["result"] == "partial"
    assert runner.view()["blocked"] is None


async def test_a_passed_test_shows_every_step_and_is_offered_to_send_when_beta_is_off(hass: HomeAssistant) -> None:
    runner = await _runner(hass, _FakeBattery(), _FakeHeat())
    assert runner.view()["blocked"] is None
    assert await runner.async_start() is None
    await _finished(runner)
    view = runner.view()
    assert view["state"] == "done" and view["result"] == "passed" and view["sent_ref"] is None
    assert [(step["part"], step["step"], step["status"]) for step in view["steps"]] == [
        ("battery", "hold", "passed"), ("battery", "charge", "passed"), ("battery", "discharge", "passed"),
        ("battery", "hand_back", "passed"), ("heat", "engage", "passed"), ("heat", "release", "passed"),
    ]
    assert view["blocked"] == "once_a_day" and view["next_at"] is not None
    assert await runner.async_start() == "once_a_day"
    assert runner.record is not None and runner.record["result"] == "passed"


async def test_a_failed_step_ends_the_other_part_and_is_sent_by_itself_under_beta(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def send(_hass: HomeAssistant, message: str, email: str | None, client: Any, *, kind: str) -> ReportResult:
        sent.append(kind)
        return ReportResult(ref="SN-TEST")

    monkeypatch.setattr(support_report, "async_send_report", send)
    await _beta(hass, _Sender())
    await domain_data(hass).beta.async_set(on=True)  # type: ignore[union-attr]
    heat = _FakeHeat(slow=True)
    runner = await _runner(hass, _FakeBattery({"hold": "ignored", "charge": "obeyed", "discharge_house": "obeyed",
                                               "normal": "restored"}), heat)
    assert await runner.async_start() is None
    await _finished(runner)
    view = runner.view()
    assert view["result"] == "failed" and view["sent_ref"] == "SN-TEST" and sent == ["beta_test"]
    statuses = {(step["part"], step["step"]): (step["status"], step["reason"]) for step in view["steps"]}
    assert statuses[("battery", "hold")] == ("failed", None)
    assert statuses[("battery", "charge")] == ("skipped", None)
    assert statuses[("heat", "engage")] == ("passed", None)
    assert statuses[("heat", "release")] == ("aborted", "after_failure")
    assert heat.test is not None and heat.test["outcome"] == "aborted:after_failure"
    # The relay reads the bundle's top-level result.
    assert hardware_test.latest_record(hass) is None, "a runner on no loaded site is not in the bundle"
    await domain_data(hass).beta.async_stop()  # type: ignore[union-attr]


async def test_the_deadline_hands_everything_back(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware_test, "MAX_TEST", timedelta(seconds=0))
    battery = _FakeBattery(slow=True)
    runner = await _runner(hass, battery, _FakeHeat(slow=True))
    assert await runner.async_start() is None
    await _finished(runner)
    assert battery.stopped == ["hardware_test_deadline"]
    view = runner.view()
    assert {step["status"] for step in view["steps"] if step["part"] == "battery"} == {"aborted"}
    assert view["result"] == "passed", "a deadline is no failure of the plant"


async def test_parts_that_are_not_there_or_not_ready(hass: HomeAssistant) -> None:
    runner = await _runner(hass, _FakeBattery(refusal="no_battery_control"), _FakeHeat(refusal="not_enabled"))
    assert runner.view()["blocked"] == "nothing_to_test"
    assert await runner.async_start() == "nothing_to_test"
    runner = await _runner(hass, _FakeBattery(refusal="needs:ai_takeover_ack"), _FakeHeat())
    assert await runner.async_start() is None
    await _finished(runner)
    steps = runner.view()["steps"]
    assert steps[0] == {"part": "battery", "step": "battery", "status": "skipped", "reason": "needs:ai_takeover_ack"}
    assert runner.view()["result"] == "partial"


async def test_nothing_to_test_comes_before_the_days_slot(hass: HomeAssistant) -> None:
    """A site where nothing can be tested says so, even when a test ran today: the day's slot is no reason there."""
    battery, heat = _FakeBattery(), _FakeHeat()
    runner = await _runner(hass, battery, heat)
    assert await runner.async_start() is None
    await _finished(runner)
    assert runner.view()["blocked"] == "once_a_day"
    battery.refusal, heat.refusal = "no_battery_control", "not_enabled"
    assert runner.view()["blocked"] == "nothing_to_test"
    assert await runner.async_start() == "nothing_to_test"


async def test_a_running_test_is_ended_at_the_unload(hass: HomeAssistant) -> None:
    battery = _FakeBattery(slow=True)
    runner = await _runner(hass, battery, _FakeHeat(slow=True))
    assert await runner.async_start() is None
    await asyncio.sleep(0)
    assert runner.view()["state"] == "running"
    await runner.async_shutdown()
    assert battery.stopped == ["hardware_test_stopped"]
    assert runner.record is not None and runner.record["stopped"] == "unload"


async def test_a_charging_car_blocks_a_test_with_a_battery_part(hass: HomeAssistant) -> None:
    car = _FakeCharger(on=True)
    runner = await _runner(hass, _FakeBattery(), _FakeHeat(), {"car": car})
    assert runner.view()["blocked"] == "car_charging"
    assert await runner.async_start() == "car_charging"
    assert runner.site.charger_test_hold.active is False
    # Without a battery part to run, a car charging is no reason.
    heat_only = await _runner(hass, _FakeBattery(refusal="no_battery_control"), _FakeHeat(), {"car": car})
    assert heat_only.view()["blocked"] is None
    car.charging = car.charge_control_on = False
    assert runner.view()["blocked"] is None


async def test_the_chargers_are_held_while_the_battery_part_runs_and_let_go_by_every_path(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ran(runner: HardwareTest, end: Any) -> list[bool]:
        car = runner.site.charger_test_hold._controller("car")  # noqa: SLF001
        seen: list[bool] = []
        battery = runner.site.battery_control
        progress = battery.hardware_test_progress

        def watched() -> dict[str, Any]:
            seen.append(runner.site.charger_test_hold.holds("car"))
            return progress()

        battery.hardware_test_progress = watched
        assert await runner.async_start() is None
        assert runner.site.charger_test_hold.holds("car") is True
        runner.site.charger_test_hold.note_refused("car")
        await end(runner)
        await hass.async_block_till_done()
        assert runner.site.charger_test_hold.active is False, "let go"
        assert car.rearms == (0 if end is _unload else 1)
        return seen

    async def _done(runner: HardwareTest) -> None:
        await _finished(runner)

    async def _stopped(runner: HardwareTest) -> None:
        await asyncio.sleep(0)
        await runner.async_stop("stopped")

    async def _unload(runner: HardwareTest) -> None:
        await asyncio.sleep(0)
        await runner.async_shutdown()

    seen = await ran(await _runner(hass, _FakeBattery(), _FakeHeat(), {"car": _FakeCharger()}), _done)
    assert seen and all(seen), "held at every look of the battery part"
    failed = _FakeBattery({"hold": "ignored", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"})
    await ran(await _runner(hass, failed, _FakeHeat(slow=True), {"car": _FakeCharger()}), _done)
    await ran(await _runner(hass, _FakeBattery(slow=True), _FakeHeat(slow=True), {"car": _FakeCharger()}), _stopped)
    await ran(await _runner(hass, _FakeBattery(slow=True), _FakeHeat(slow=True), {"car": _FakeCharger()}), _unload)
    monkeypatch.setattr(hardware_test, "MAX_TEST", timedelta(seconds=0))
    await ran(await _runner(hass, _FakeBattery(slow=True), _FakeHeat(slow=True), {"car": _FakeCharger()}), _done)


async def test_a_charge_begun_during_the_test_is_stopped(hass: HomeAssistant) -> None:
    car = _FakeCharger()
    battery = _FakeBattery(slow=True)
    runner = await _runner(hass, battery, _FakeHeat(slow=True), {"car": car})
    assert await runner.async_start() is None
    car.charging = car.charge_control_on = True
    for _ in range(5):
        await asyncio.sleep(0)
    await hass.async_block_till_done()
    assert car.stops == 1
    await runner.async_stop("stopped")


@pytest.mark.parametrize("end", ["done", "stop", "unload", "deadline"])
async def test_an_ocpp_charger_s_ceiling_is_0_a_during_the_battery_part_and_set_back_by_every_path(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    ceiling = "number.halo_charger_maximum_current"
    hass.states.async_set(ceiling, "16", {"min": 0, "max": 32})
    written: list[float] = []

    async def set_value(call: Any) -> None:
        written.append(float(call.data["value"]))
        hass.states.async_set(ceiling, str(float(call.data["value"])), {"min": 0, "max": 32})

    hass.services.async_register("number", "set_value", set_value)
    ocpp = _FakeCharger()
    ocpp.ocpp_target = object()
    ocpp.ocpp_controls = SimpleNamespace(station_maximum_entity=ceiling)
    if end == "deadline":
        monkeypatch.setattr(hardware_test, "MAX_TEST", timedelta(seconds=0))
    slow = end != "done"
    runner = await _runner(hass, _FakeBattery(slow=slow), _FakeHeat(slow=slow), {"ocpp": ocpp})
    assert await runner.async_start() is None
    await hass.async_block_till_done()
    if end in ("done", "deadline"):
        await _finished(runner)
    elif end == "stop":
        await runner.async_stop("stopped")
    else:
        await runner.async_shutdown()
    await hass.async_block_till_done()
    assert written[0] == 0.0 and written[-1] == 16.0, written
    assert hass.states.get(ceiling).state == "16.0"
    assert runner.site.charger_test_hold.ceilings_kept == []


async def test_a_battery_part_that_does_not_start_lets_the_chargers_go_at_once(hass: HomeAssistant) -> None:
    class _Refusing(_FakeBattery):
        async def async_hardware_test_start(self) -> str | None:
            return "verifying"

    runner = await _runner(hass, _Refusing(), _FakeHeat(slow=True), {"car": _FakeCharger()})
    assert await runner.async_start() is None
    for _ in range(5):
        await asyncio.sleep(0)
    assert runner.site.charger_test_hold.active is False
    await runner.async_stop("stopped")


# ---- the card's contract: `spotnav/beta`'s answers (`tests/fixtures/beta/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them)

FIXTURES = Path(__file__).parent / "fixtures" / "beta"


def _write_or_compare(name: str, answer: dict[str, Any]) -> None:
    path = FIXTURES / name
    text = json.dumps(answer, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text)


def _answer(beta: Beta, runner: HardwareTest | None, **extra: Any) -> dict[str, Any]:
    return {"api_version": 1, "ok": True, "error": None, "beta": view(beta.state),
            "test": None if runner is None else runner.view(), "ref": None, "retry_after_s": None, **extra}


async def test_the_card_s_fixtures(hass: HomeAssistant, freezer, monkeypatch: pytest.MonkeyPatch) -> None:
    freezer.move_to("2026-11-19T20:15:00+01:00")
    monkeypatch.setattr(hardware_test, "_current_version", lambda _hass, fallback: "3.0.0")

    async def version(_self: HardwareTest) -> str:
        return "3.0.0"

    monkeypatch.setattr(HardwareTest, "_async_version", version)
    beta = await _beta(hass, _Sender())
    idle = await _runner(hass, _FakeBattery(), _FakeHeat())
    _write_or_compare("off_idle.json", _answer(beta, idle))
    await beta.async_set(on=True, email="me@example.com")
    running = await _runner(hass, _FakeBattery(slow=True), _FakeHeat(slow=True))
    assert await running.async_start() is None
    await asyncio.sleep(0)
    _write_or_compare("on_running.json", _answer(beta, running))
    await running.async_stop("stopped")

    async def send(_hass: HomeAssistant, message: str, email: str | None, client: Any, *, kind: str) -> ReportResult:
        return ReportResult(ref="SN-7KQ4")

    monkeypatch.setattr(support_report, "async_send_report", send)
    failed = await _runner(hass, _FakeBattery({"hold": "obeyed", "charge": "partial", "discharge_house": "obeyed",
                                               "normal": "restored"}), _FakeHeat(refusal="safety:room_cold"))
    assert await failed.async_start() is None
    await _finished(failed)
    _write_or_compare("on_done_failed.json", _answer(beta, failed))
    partial = await _runner(hass, _FakeBattery(refusal="needs:outside_control:settings_changed"), _FakeHeat())
    assert await partial.async_start() is None
    await _finished(partial)
    _write_or_compare("on_done_partial.json", _answer(beta, partial))
    freezer.move_to("2026-12-04T03:00:00+01:00")
    _write_or_compare("ended.json", _answer(beta, None))
    await beta.async_stop()
