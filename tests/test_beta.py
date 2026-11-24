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


async def test_the_daily_report_goes_once_a_night_while_on_and_a_failure_is_skipped(hass: HomeAssistant, freezer) -> None:
    # A fixed midday: from the real clock the steps of a day and half an hour can cross a local midnight or the
    # night's timer, and the count of reports then depends on the hour the suite runs.
    freezer.move_to("2026-07-01 12:00:00+00:00")
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
        #: Why the moment does not suit the battery steps now (`BatteryControl.hardware_test_moment`).
        self.moment: str | None = None
        self.started = 0
        #: What the plant's recipe waits for (`BatteryControl.hardware_test_due`): `hardware_test_first` for a brand's
        #: first pass on the plant.
        self.due: str | None = None

    def hardware_test_refusal(self) -> str | None:
        return self.refusal

    def hardware_test_moment(self, _now: datetime) -> str | None:
        return self.moment

    def hardware_test_due(self) -> str | None:
        return self.due

    def firmware_key(self) -> str:
        return "SPC117 | sigen | 2.0.3"

    async def async_hardware_test_start(self) -> str | None:
        self.running = True
        self.started += 1
        self.done = {}
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


async def test_parts_there_but_none_ready_is_not_nothing_to_test(hass: HomeAssistant) -> None:
    """A battery waiting for an acknowledgement and a heat path that is busy: there is something to test, just not
    now, so the card does not tell the person to turn control on."""
    runner = await _runner(hass, _FakeBattery(refusal="needs:ai_takeover_ack"), _FakeHeat(refusal="busy"))
    assert runner.view()["blocked"] == "not_ready"
    assert await runner.async_start() == "not_ready"


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


async def test_a_charging_car_makes_a_test_with_a_battery_part_wait_and_it_runs_by_itself_once_it_stopped(
    hass: HomeAssistant,
) -> None:
    car = _FakeCharger(on=True)
    battery = _FakeBattery()
    runner = await _runner(hass, battery, _FakeHeat(), {"car": car})
    assert runner.view()["blocked"] is None, "pressing Start is all the person does"
    assert await runner.async_start() is None
    assert runner.view()["state"] == "waiting" and runner.view()["blocked"] == "waiting"
    assert runner.waiting is not None and runner.waiting["why"] == "car_charging"
    assert runner.site.charger_test_hold.active is False and battery.started == 0
    runner.look_waiting(dt_util.now())
    assert battery.started == 0
    car.charging = car.charge_control_on = False
    runner.look_waiting(dt_util.now())
    assert runner.waiting is None and runner.running
    await _finished(runner)
    assert runner.record is not None and runner.record["result"] == "passed"
    # Without a battery part to run, a car charging is no reason to wait.
    car.charging = car.charge_control_on = True
    heat_only = await _runner(hass, _FakeBattery(refusal="no_battery_control"), _FakeHeat(), {"car": car})
    assert await heat_only.async_start() is None and heat_only.waiting is None
    await _finished(heat_only)


async def test_a_test_waits_for_a_moment_that_suits_its_battery_steps_and_says_when_it_is_likely(
    hass: HomeAssistant, freezer: Any,
) -> None:
    morning = dt_util.start_of_local_day() + timedelta(hours=11)
    freezer.move_to(morning)
    battery = _FakeBattery()
    battery.moment = "battery_not_discharging"  # the sun covers the house: a hold could not be judged
    runner = await _runner(hass, battery, _FakeHeat())
    assert await runner.async_start() is None
    view = runner.view()
    assert view["state"] == "waiting" and view["next_at"] == (morning + timedelta(hours=6)).isoformat()
    assert battery.started == 0 and not runner.running
    # A second press is refused while it waits; the person's stop ends the wait.
    assert await runner.async_start() == "waiting"
    freezer.move_to(morning + timedelta(hours=7))
    assert runner.view()["next_at"] is None, "in the evening: soon"
    battery.moment = None
    runner.look_waiting(dt_util.now())
    assert runner.running
    await _finished(runner)
    assert battery.started == 1 and runner.record is not None and runner.record["result"] == "passed"


async def test_a_wait_that_finds_no_moment_gives_up_without_asking_anything(
    hass: HomeAssistant, freezer: Any,
) -> None:
    freezer.move_to("2026-11-19T11:00:00+01:00")
    battery = _FakeBattery()
    battery.moment = "soc_out_of_range"
    runner = await _runner(hass, battery, _FakeHeat())
    assert await runner.async_start() is None
    freezer.tick(hardware_test.WAIT_MAX + timedelta(minutes=1))
    runner.look_waiting(dt_util.now())
    assert runner.waiting is None and battery.started == 0
    record = runner.record
    assert record is not None and record["result"] == "skipped"
    assert record["steps"] == [{"part": "battery", "step": "battery", "status": "skipped", "reason": "no_suitable_moment"}]
    # It leaves the day's slot: Start can be pressed again.
    assert runner.view()["blocked"] is None


async def test_a_step_that_could_not_be_judged_runs_again_by_itself_at_the_next_suitable_moment(
    hass: HomeAssistant,
) -> None:
    battery = _FakeBattery({"hold": "obeyed", "charge": "not_judgeable:grid_charge_cap",
                            "discharge_house": "obeyed", "normal": "restored"})
    runner = await _runner(hass, battery, _FakeHeat(refusal="no_battery_control"))
    assert await runner.async_start() is None
    await _finished(runner)
    # Rescheduled, no prompt: the record says what could not be judged, and the test waits for the next moment.
    assert runner.record is not None and runner.record["result"] == "partial"
    assert runner.waiting is not None and runner.waiting["retry"] == 1 and runner.view()["state"] == "waiting"
    for retry in range(2, hardware_test.MAX_RETRIES + 1):
        runner.look_waiting(dt_util.now())
        await _finished(runner)
        assert runner.waiting is not None and runner.waiting["retry"] == retry
    runner.look_waiting(dt_util.now())
    await _finished(runner)
    # Bounded: after `MAX_RETRIES` runs again it stops by itself.
    assert runner.waiting is None and battery.started == hardware_test.MAX_RETRIES + 1
    # A charge judged on a later run ends the retries.
    battery.outcomes["charge"] = "obeyed"
    runner.waiting = None
    runner._schedule(dt_util.now(), "3.0.0", retry=1, why="not_judgeable")  # noqa: SLF001
    runner.look_waiting(dt_util.now())
    await _finished(runner)
    assert runner.waiting is None and runner.record is not None and runner.record["result"] == "passed"


async def test_a_waiting_test_goes_on_waiting_after_a_restart(hass: HomeAssistant) -> None:
    battery = _FakeBattery()
    battery.moment = "battery_not_discharging"
    runner = await _runner(hass, battery, _FakeHeat())
    assert await runner.async_start() is None
    await runner.async_shutdown()
    await hass.async_block_till_done()
    # The battery's integration loads after SpotNav: the wait is kept a while, then given up if it never comes.
    absent = _FakeBattery(refusal="no_battery_control")
    again = await _runner(hass, absent, _FakeHeat(refusal="no_battery_control"))
    again.look_waiting(dt_util.now())
    assert again.waiting is not None and again.waiting["why"] == "starting"
    again.look_waiting(dt_util.now() + hardware_test.LOAD_GRACE + timedelta(seconds=1))
    assert again.waiting is None
    await again.async_shutdown()
    again = await _runner(hass, battery, _FakeHeat())
    assert again.waiting is None, "given up"
    assert await again.async_start() is None
    assert again.waiting is not None and again.view()["state"] == "waiting"
    await again.async_stop("stopped")
    assert again.waiting is None and again.view()["state"] in ("idle", "done")


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
async def test_an_ocpp_charger_is_out_of_service_during_the_battery_part_and_set_back_by_every_path(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.spotnav.site import charger_test_hold

    monkeypatch.setattr(charger_test_hold, "CONFIRM_S", 0.0)
    switch = "switch.halo_charger_availability"
    er.async_get(hass).async_get_or_create(
        "switch", "ocpp", "switch.ocpp.halo_charger.availability", suggested_object_id="halo_charger_availability"
    )
    hass.states.async_set(switch, "on")
    sent: list[str] = []

    def handler(to: str):
        async def handle(call: Any) -> None:
            sent.append(to)
            hass.states.async_set(switch, to)

        return handle

    hass.services.async_register("switch", "turn_off", handler("off"))
    hass.services.async_register("switch", "turn_on", handler("on"))
    ocpp = _FakeCharger()
    ocpp.ocpp_target = SimpleNamespace(charge_point_id="halo_charger", connector_id=1)
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
    assert sent and sent[0] == "off" and sent[-1] == "on", sent
    assert hass.states.get(switch).state == "on"
    assert runner.site.charger_test_hold.unavailable_kept == []


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


async def test_on_a_stop_the_chargers_are_let_go_only_after_the_battery_was_handed_back(hass: HomeAssistant) -> None:
    """The re-review (2026-11-21): on a cancel the battery part's own `finally` let the chargers go before the run
    handed the battery back."""
    order: list[str] = []
    battery = _FakeBattery(slow=True)
    stop = battery.async_hardware_test_stop

    async def stopped(reason: str) -> None:
        order.append(f"battery:{reason}")
        await stop(reason)

    battery.async_hardware_test_stop = stopped  # type: ignore[method-assign]
    runner = await _runner(hass, battery, _FakeHeat(slow=True), {"car": _FakeCharger()})
    hold = runner.site.charger_test_hold
    release = hold.release

    def released() -> list[str]:
        order.append("chargers")
        return release()

    hold.release = released  # type: ignore[method-assign]
    assert await runner.async_start() is None
    await asyncio.sleep(0)
    await runner.async_stop("stopped")
    assert order and order[0].startswith("battery:"), order
    assert "chargers" in order and hold.active is False


class _Phones:
    """The phone store as the notice reads it: the Companion services on for SpotNav's notices."""

    url = "/dashboard-spotnav/0"

    def __init__(self, services: tuple[str, ...]) -> None:
        self.services = services

    def companion_on(self) -> tuple[str, ...]:
        return self.services


def _phones(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    from custom_components.spotnav.notifications import phone_store as store_module  # noqa: PLC0415

    sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(call: Any) -> None:
        sent.append((call.service, dict(call.data)))

    for service in ("mobile_app_anna", "mobile_app_olle"):
        hass.services.async_register("notify", service, notify)
    monkeypatch.setattr(store_module, "phone_store", lambda _hass: _Phones(("mobile_app_anna", "mobile_app_olle")))
    return sent


async def test_a_brands_first_pass_waits_for_waking_hours_and_tells_each_phone_once_with_stop(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hardware_test.NOTIFICATION_ACTION_EVENT == "mobile_app_notification_action"
    sent = _phones(hass, monkeypatch)
    night = dt_util.start_of_local_day() + timedelta(hours=23)
    freezer.move_to(night)
    battery = _FakeBattery()
    battery.due = "hardware_test_first"
    runner = await _runner(hass, battery, _FakeHeat(refusal="no_battery_control"))
    assert await runner.async_start() is None
    # The night suits the battery, but not a recipe's first writes: it waits for the morning, nothing told yet.
    assert runner.waiting is not None and runner.waiting["why"] == "not_waking_hours" and battery.started == 0
    assert runner.view()["next_at"] == (night + timedelta(hours=18)).isoformat()
    runner.look_waiting(dt_util.now())
    assert battery.started == 0 and sent == []
    freezer.move_to(night + timedelta(hours=10))  # 09:00
    runner.look_waiting(dt_util.now())
    assert runner.running
    await _finished(runner)
    await hass.async_block_till_done()
    told = [(service, data) for service, data in sent if data.get("message") != "clear_notification"]
    assert sorted(service for service, _data in told) == ["mobile_app_anna", "mobile_app_olle"]
    _service, data = told[0]
    assert "running now (about 10 min)" in data["message"]
    assert data["data"]["actions"] == [{"action": "SPOTNAV_HARDWARE_TEST_STOP_site_1", "title": "Stop the test"}]
    # Taken off the phones at the run's end.
    cleared = [service for service, data in sent if data.get("message") == "clear_notification"]
    assert sorted(cleared) == ["mobile_app_anna", "mobile_app_olle"]


async def test_the_automatic_check_and_a_later_test_tell_no_phone_and_wait_at_any_hour(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _phones(hass, monkeypatch)
    freezer.move_to(dt_util.start_of_local_day() + timedelta(hours=23))
    battery = _FakeBattery()  # the brand passed on this plant before: `hardware_test_due` None
    runner = await _runner(hass, battery, _FakeHeat(refusal="no_battery_control"))
    assert await runner.async_start() is None and runner.running
    await _finished(runner)
    await hass.async_block_till_done()
    assert sent == []


async def test_stop_on_a_phone_ends_the_first_pass(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: Any) -> None:
    sent = _phones(hass, monkeypatch)
    freezer.move_to(dt_util.start_of_local_day() + timedelta(hours=10))
    battery = _FakeBattery(slow=True)
    battery.due = "hardware_test_first"
    runner = await _runner(hass, battery, _FakeHeat(refusal="no_battery_control"))
    assert await runner.async_start() is None and runner.running
    await hass.async_block_till_done()
    assert len(sent) == 2
    # Another site's button is not this test's.
    hass.bus.async_fire("mobile_app_notification_action", {"action": "SPOTNAV_HARDWARE_TEST_STOP_site_2"})
    await hass.async_block_till_done()
    assert runner.running
    hass.bus.async_fire("mobile_app_notification_action", {"action": "SPOTNAV_HARDWARE_TEST_STOP_site_1"})
    await hass.async_block_till_done()
    assert not runner.running and battery.stopped == ["hardware_test_stopped"]
    assert runner.record is not None and runner.record["stopped"] == "phone"
    assert [data["message"] for _service, data in sent[2:]] == ["clear_notification", "clear_notification"]

