"""After an update the plan's own commands show the steps of the check on the new key (owner rule 2026-11-26): a
command the plan wrote there and the monitor saw act counts as that mode passed, a hand-back that left every control
as found as the hand-back passed, each marked `via: plan`. Credit only (a failure seen by the monitor is never
written to the report), never for an expiry, the capability per phase, the export limit, a test's rows or anything
in shadow. The routine's own answer replaces a plan's. What is still open a week after the key was first read is
checked ahead of the plan, at a moment that suits the routine by every other rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import control as control_module, gate
from custom_components.spotnav.battery.control import BatteryControl
from custom_components.spotnav.battery.executor import StepResult
from custom_components.spotnav.battery.monitor import Command
from tests.test_battery_control import FIRMWARE, _looks, _options, _site, _started
from tests.test_battery_reverify import NEW_FIRMWARE, _report, _soon

pytestmark = pytest.mark.usefixtures("offline_relay")

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
#: Looks (10 s apart) that hold the monitor's grace and its three readings a minute apart.
PROVING_LOOKS = 50


def _plan_row(report: dict[str, Any] | None, mode: str) -> dict[str, Any]:
    return ((report or {}).get("modes") or {}).get(mode) or {}


# --- the rule (`gate.plan_proved`, `gate.merge_report`) ------------------------------------------------------------


def test_hold_and_the_hand_back_shown_by_the_plan_pass_the_key_as_the_routine_would() -> None:
    report = gate.plan_proved(None, "hold", key="B | sigen 1", brand="sigenergy", firmware="B", now=T0)
    assert report is not None and gate.report_status(report) == "pending" and report["attempts"] == 0
    assert report["modes"]["hold"]["outcome"] == "obeyed" and report["modes"]["hold"]["via"] == gate.VIA_PLAN
    report = gate.plan_proved(report, "normal", key="B | sigen 1", brand="sigenergy", firmware="B", now=T0)
    assert report is not None and gate.report_status(report) == "passed"
    assert report["modes"]["normal"] == {"mode": "normal", "outcome": "restored", "via": "plan",
                                         "decided_at": T0.isoformat()}
    assert gate.plan_shown(report) == ("hold", "normal")
    # The routine still owes the modes the plan has not shown, and only those.
    assert gate.modes_to_run(report) == ("charge", "discharge_house") and gate.needs_run(report)
    for mode in ("charge", "discharge_house"):
        report = gate.plan_proved(report, mode, key="B | sigen 1", brand="sigenergy", firmware="B", now=T0)
    assert report is not None and gate.modes_to_run(report) == () and not gate.needs_run(report)
    options = gate.Options(enabled=True, shadow_only=False)
    assert gate.allowed_actions(report, options) >= {"hold", "grid_charge", "discharge_house"}


def test_the_plan_never_shows_an_expiry_the_capability_per_phase_or_the_export_limit() -> None:
    for mode in ("expiry", "expiry:L1", gate.MODE_PER_PHASE, "curtail", "curtail_export", "export_discharge"):
        assert gate.plan_proved(None, mode, key="B | sigen 1", brand="sigenergy", firmware="B", now=T0) is None, mode


def test_the_plan_changes_no_row_the_routine_decided_and_never_writes_a_failure() -> None:
    for outcome in ("obeyed", "ignored", "partial", "aborted:no_effect"):
        report = _report("B | sigen 1", hold=outcome)
        assert gate.plan_proved(report, "hold", key="B | sigen 1", brand="sigenergy", firmware="B", now=T0) is None
    # A step the routine could not judge is open: the plan may show it.
    report = _report("B | sigen 1", charge="not_judgeable:grid_charge_cap")
    shown = gate.plan_proved(report, "charge", key="B | sigen 1", brand="sigenergy", firmware="B", now=T0)
    assert shown is not None and shown["modes"]["charge"]["via"] == "plan" and shown["attempts"] == 1
    assert set(gate.PLAN_MODES.values()) <= gate.PASSING


def test_the_routines_answer_replaces_the_plans_pass_also_when_it_is_a_failure() -> None:
    key = "B | sigen 1"
    shown = None
    for mode in ("hold", "normal", "charge"):
        shown = gate.plan_proved(shown, mode, key=key, brand="sigenergy", firmware="B", now=T0)
    assert shown is not None
    run = {"brand": "sigenergy", "started_at": T0.isoformat(), "modes": {
        "hold": {"mode": "hold", "outcome": "ignored"},
        "charge": {"mode": "charge", "outcome": "obeyed"},
        # Nothing of the run's was handed back: no answer, the plan's stands.
        "normal": {"mode": "normal", "outcome": "not_judgeable:nothing_written"},
    }}
    merged = gate.merge_report(shown, run, key=key, attempts=1, now=T0)
    assert merged["modes"]["hold"] == {"mode": "hold", "outcome": "ignored"}
    assert merged["modes"]["charge"] == {"mode": "charge", "outcome": "obeyed"}
    assert merged["modes"]["normal"]["via"] == "plan" and gate.report_status(merged) == "failed"
    # And the routine's own decided row is kept against a later run, as before.
    again = gate.merge_report(merged, {"modes": {"charge": {"mode": "charge", "outcome": "ignored"}}}, key=key,
                              attempts=2, now=T0)
    assert again["modes"]["charge"]["outcome"] == "obeyed"


def test_a_hold_shown_by_the_plan_names_its_variant_and_keeps_the_variants_tried() -> None:
    report = {"key": "B", "brand": "x", "attempts": 1, "modes": {"hold": {
        "outcome": "not_judgeable:battery_not_discharging", "variants": [{"variant": "first", "outcome": "ignored"}],
    }}}
    shown = gate.plan_proved(report, "hold", key="B", brand="x", firmware="B", now=T0, variant="second")
    assert shown is not None and gate.hold_variant(shown) == "second"
    assert shown["modes"]["hold"]["variants"] == [
        {"variant": "first", "outcome": "ignored"}, {"variant": "second", "outcome": "obeyed"},
    ]


def test_a_mode_the_plan_showed_on_the_new_key_counts_there_and_the_open_ones_stay_carried() -> None:
    old = _report("A | sigen 1")
    shown = gate.plan_proved(None, "hold", key="B | sigen 1", brand="sigenergy", firmware="B", now=T0)
    carried = gate.control_report(shown, {"A | sigen 1": old, "B | sigen 1": shown}, key="B | sigen 1", brand="sigenergy")
    assert carried is not None
    assert carried["carried_from"] == dict.fromkeys(("charge", "discharge_house", "normal"), "A | sigen 1")
    assert carried["modes"]["hold"]["via"] == "plan" and "via" not in carried["modes"]["charge"]


def test_the_deadline_is_a_week_from_when_the_key_was_first_read() -> None:
    assert gate.PLAN_PROOF_DEADLINE_S == 7 * 86400.0
    assert not gate.deadline_passed(None, T0)
    assert not gate.deadline_passed(T0 - timedelta(days=6, hours=23), T0)
    assert gate.deadline_passed(T0 - timedelta(days=7), T0)


def test_the_plan_stands_aside_only_at_a_moment_that_suits_the_routine_by_every_other_rule() -> None:
    def moment(**changes: Any) -> gate.Moment:
        base: dict[str, Any] = {
            "soc_pct": 50.0, "power_kw": 0.0, "fresh": True, "measurement_ok": True, "headroom_a": 10.0,
            "car_charging": False, "blocker": None, "executor_idle": False, "house_deficit_kw": 1.5,
        }
        return gate.Moment(**(base | changes))

    # The plan holds the battery (its own power says nothing): the house's need stands in for the discharge.
    assert gate.suitable(moment()) == "executor_busy" and gate.suitable_ahead(moment()) is None
    assert gate.suitable_ahead(moment(power_kw=3.0)) is None
    assert gate.suitable_ahead(moment(house_deficit_kw=0.1)) == "battery_not_discharging"
    assert gate.suitable_ahead(moment(house_deficit_kw=None)) == "stale_data"
    assert gate.suitable_ahead(moment(soc_pct=90.0)) == "soc_out_of_range"
    assert gate.suitable_ahead(moment(soc_pct=10.0)) == "soc_out_of_range"
    assert gate.suitable_ahead(moment(headroom_a=2.0)) == "headroom_too_small"
    assert gate.suitable_ahead(moment(car_charging=True)) == "car_charging"
    assert gate.suitable_ahead(moment(fresh=False)) == "stale_data"
    assert gate.suitable_ahead(moment(power_kw=None)) == "stale_data"
    assert gate.suitable_ahead(moment(blocker="management_unknown")) == "management_unknown"


# --- on a site -----------------------------------------------------------------------------------------------------


def _no_routine(monkeypatch: pytest.MonkeyPatch) -> None:
    """No moment suits the routine: only the plan's own commands can show anything."""
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")


def _kinds(control: BatteryControl, kind: str) -> list[dict[str, Any]]:
    return [row for row in control.log if row["kind"] == kind]


async def test_the_plans_commands_on_the_new_key_pass_its_check_one_step_at_a_time(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _no_routine(monkeypatch)
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plan: dict[str, Any] = {"slot": ("hold", 0.0, None)}
    control = await _started(hass, site, controller, plan_source=lambda _now: plan["slot"])
    old_key = control.firmware_key()
    now = await _looks(control, _soon(), PROVING_LOOKS)
    # On the key whose check passed nothing is added: its rows are the routine's.
    assert _kinds(control, "plan_proved") == [] and "via" not in _plan_row(control.report(), "hold")

    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    now = await _looks(control, now, PROVING_LOOKS)
    key = control.firmware_key()
    assert key != old_key and control.state() == "on" and control.executor.state.phase == "active"
    # The hold was written on the old key: what it shows says nothing of the new one.
    assert control.report() is None and _kinds(control, "plan_proved") == []
    plan["slot"] = None
    now = await _looks(control, now, 12)
    # Nor does its hand-back: nothing was written on the new key.
    assert control.executor.state.phase == "idle" and control.report() is None

    plan["slot"] = ("hold", 0.0, None)
    now = await _looks(control, now, 20)
    assert control.report() is None, "inside the monitor's grace nothing is shown"
    now = await _looks(control, now, PROVING_LOOKS)
    hold = _plan_row(control.report(), "hold")
    assert hold["outcome"] == "obeyed" and hold["via"] == "plan" and hold["before_kw"] == -2.0
    assert len(hold["readings"]) == 3 and gate.report_status(control.report()) == "pending"
    assert control.state() == "on"
    assert control.snapshot()["carried_from"] == dict.fromkeys(("charge", "discharge_house", "normal"), old_key)

    plan["slot"] = None
    now = await _looks(control, now, 12)
    normal = _plan_row(control.report(), "normal")
    assert normal["outcome"] == "restored" and normal["via"] == "plan" and normal["written"]
    assert gate.report_status(control.report()) == "passed" and control.report()["attempts"] == 0
    assert control.snapshot()["plan_proof"]["shown_by_plan"] == ["hold", "normal"]
    # The charge and the discharge still run on the old key's answer.
    assert control.snapshot()["carried_from"] == dict.fromkeys(("charge", "discharge_house"), old_key)
    assert "grid_charge" in control.allowed()

    plan["slot"] = ("grid_charge", 2.0, None)
    now = await _looks(control, now, PROVING_LOOKS)
    charge = _plan_row(control.report(), "charge")
    assert charge["outcome"] == "obeyed" and charge["via"] == "plan" and charge["expected_kw"] == 2.0
    plan["slot"] = None
    now = await _looks(control, now, 12)
    plan["slot"] = ("discharge_house", 3.0, None)
    now = await _looks(control, now, PROVING_LOOKS)
    assert _plan_row(control.report(), "discharge_house")["via"] == "plan"
    # Every step the routine would run is shown: nothing is carried and nothing is left for it.
    assert [row["reason"] for row in _kinds(control, "plan_proved")] == ["hold", "normal", "charge", "discharge_house"]
    assert not gate.needs_run(control.report(), control._supported_modes())
    assert control.snapshot()["carried_from"] is None and control.status()["line"] == "on"
    assert _kinds(control, "verify_started") == []
    assert {"hold", "grid_charge", "discharge_house"} <= control.allowed()

    # Kept over a restart, with when the key was first read.
    await control.async_shutdown()
    again = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await again.async_start()
    assert gate.plan_shown(again.report()) == ("hold", "normal", "charge", "discharge_house")
    assert again.key_since[key] == control.key_since[key] and again.state() == "on"
    await again.async_shutdown()


async def test_a_command_the_plant_does_not_obey_is_counted_by_the_monitor_and_never_written_to_the_report(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _no_routine(monkeypatch)
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("hold", 0.0, None))
    now = await _looks(control, _soon(), 3)
    await control.async_stop()
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    plant.obeys = False
    now = await _looks(control, now + timedelta(minutes=10), PROVING_LOOKS)
    assert [row["reason"] for row in _kinds(control, "monitor")] == ["no_effect"]
    assert control.executor.monitor.counters(now.date().isoformat())["no_effect"] == 1
    report = control.report()
    assert report is None or all(gate.mode_verdict(row.get("outcome")) != "fail" for row in report["modes"].values())
    assert gate.report_status(report) in ("none", "pending") and _kinds(control, "plan_proved") == []


async def test_nothing_is_shown_in_shadow_only(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _no_routine(monkeypatch)
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("hold", 0.0, None))
    now = await _looks(control, _soon(), 3)
    await control.async_stop()
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    _options(hass, site, shadow_only=True)
    calls = len(plant.calls)
    await _looks(control, now + timedelta(minutes=10), PROVING_LOOKS)
    assert control.state() == "shadow_only" and len(plant.calls) == calls
    assert control.report() is None and _kinds(control, "plan_proved") == []


def _proved(action: str, at: datetime, *, variant: str | None = None, written: bool = True) -> dict[str, Any]:
    command = Command("hold" if action == "hold" else "charge", at, 2.0, before_kw=-2.0, written=written)
    return {"at": at.isoformat(), "kind": "proved", "reason": action, "variant": variant, "detail": {
        "command": command.as_dict(), "expected_kw": 2.0, "readings": [{"verdict": "ok"}] * 3,
    }}


async def test_only_the_plans_own_command_on_this_key_in_the_mode_control_runs_is_counted(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _no_routine(monkeypatch)
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    now = await _looks(control, _soon(), 2)
    old_key = control.firmware_key()
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    now = await _looks(control, now, 2)
    state = control.executor.state

    def credit(*events: dict[str, Any]) -> dict[str, Any] | None:
        control._credit_plan(StepResult(state, events=events), now)
        return control.report()

    # Written before the key was read, a dry run, a sale, the export limit, an action with no mode of its own.
    assert credit(_proved("grid_charge", now - timedelta(hours=1))) is None
    assert credit(_proved("grid_charge", now, written=False)) is None
    for action in ("export_discharge", "curtail_export", "self_consume", "per_phase", "expiry"):
        assert credit(_proved(action, now)) is None, action
    # A command of another kind than the action's (one still watched from before).
    wrong = _proved("hold", now)
    wrong["detail"]["command"]["kind"] = "charge"
    assert credit(wrong) is None
    # The key was first read long ago and is read again only now (a downgrade, a restart): what was written before
    # that was written on another key, or is no longer known to be the plan's.
    first_read = control.key_since[control.firmware_key()]
    control.key_since[control.firmware_key()] = (now - timedelta(days=30)).isoformat()
    assert control._key_run == (control.firmware_key(), control._key_run[1]) and control._key_run[1] <= now
    assert credit(_proved("grid_charge", control._key_run[1] - timedelta(minutes=5))) is None
    control._key_run = (old_key, now - timedelta(hours=1))
    assert credit(_proved("grid_charge", now)) is None, "the key noted at the last look is another"
    await _looks(control, now, 1)
    assert control._key_run[0] == control.firmware_key() and control._key_run[1] >= now
    assert credit(_proved("grid_charge", now - timedelta(seconds=1))) is None
    control._key_run = (control.firmware_key(), now - timedelta(hours=1))
    control.key_since[control.firmware_key()] = first_read
    # A stored time that cannot be read is no time: nothing counts until the key is noted afresh, at the next look.
    control.key_since[control.firmware_key()] = "soon"
    assert credit(_proved("grid_charge", now)) is None
    await _looks(control, now, 1)
    assert gate.parse_time(control.key_since[control.firmware_key()]) is not None
    control._key_run = (control.firmware_key(), now - timedelta(hours=1))
    control.key_since[control.firmware_key()] = first_read
    # A hand-back of something the plan did not write on this key (a test's, the routine's, an old key's).
    assert credit({"at": now.isoformat(), "kind": "handed_back", "reason": "plan", "written": ["x"], "clean": True}) is None
    # One that did not leave everything as found.
    control._plan_wrote = control.firmware_key()
    assert credit({"at": now.isoformat(), "kind": "handed_back", "reason": "plan", "written": ["x"], "clean": False}) is None
    assert control._plan_wrote is None

    # Where the charge is allowed only per phase the balanced charge shows nothing.
    control.reports[old_key]["modes"]["charge"]["via"] = "per_phase"
    control.reports[old_key]["modes"][gate.MODE_PER_PHASE] = {"mode": gate.MODE_PER_PHASE, "outcome": "obeyed"}
    assert gate.charge_by_phases(control.control_report())
    assert credit(_proved("grid_charge", now)) is None
    del control.reports[old_key]["modes"]["charge"]["via"]
    report = credit(_proved("grid_charge", now))
    assert report is not None and report["modes"]["charge"]["via"] == "plan"
    assert gate.MODE_PER_PHASE not in report["modes"]

    # A hold only in the variant control runs on (a brand with several).
    monkeypatch.setattr(type(control.executor.control), "variant_names", lambda self: ("first", "second"))
    control.reports[old_key]["modes"]["hold"]["variant"] = "second"
    assert "hold" not in credit(_proved("hold", now, variant="first"))["modes"]
    assert "hold" not in credit(_proved("hold", now))["modes"]
    hold = credit(_proved("hold", now, variant="second"))["modes"]["hold"]
    assert hold["via"] == "plan" and hold["variant"] == "second"
    await control.async_shutdown()


async def test_a_test_action_and_a_hardware_test_show_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _no_routine(monkeypatch)
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    now = await _looks(control, _soon(), 2)
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    now = await _looks(control, now, 2)
    answer = await control.async_trial("hold", None, 20.0, force=False)
    assert answer.get("refusal") is None, answer
    now = await _looks(control, dt_util.now() + timedelta(seconds=10), PROVING_LOOKS)
    assert control.trial is not None and control.executor.state.phase == "active"
    assert any(row["kind"] == "proved" for row in control.log), "the monitor saw the hold act"
    control.trial = None
    await _looks(control, now, 12)
    # Neither the test's hold nor its hand-back counts for the report.
    assert control.executor.state.phase == "idle" and control.report() is None
    assert _kinds(control, "plan_proved") == []
    await control.async_shutdown()


async def test_the_routine_runs_only_what_the_plan_has_not_shown_and_its_answer_stands(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plan: dict[str, Any] = {"slot": None}
    control = await _started(hass, site, controller, plan_source=lambda _now: plan["slot"])
    now = await _looks(control, _soon(), 2)
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    original = gate.suitable
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    plan["slot"] = ("hold", 0.0, None)
    now = await _looks(control, now, PROVING_LOOKS)
    plan["slot"] = None
    now = await _looks(control, now, 12)
    assert gate.plan_shown(control.report()) == ("hold", "normal")
    monkeypatch.setattr(gate, "suitable", original)
    now = await _looks(control, now, 400)
    started = _kinds(control, "verify_started")
    assert len(started) == 1 and "hold" not in started[0]["modes"]
    assert {"charge", "discharge_house"} <= set(started[0]["modes"])
    report = control.report()
    assert gate.plan_shown(report) == ("hold",), "the run's own hand-back is the routine's answer for normal"
    assert report["modes"]["normal"]["outcome"] == "restored" and "via" not in report["modes"]["normal"]
    assert report["modes"]["charge"]["outcome"] == "obeyed" and "via" not in report["modes"]["charge"]
    assert control.state() == "on" and control.snapshot()["carried_from"] is None
    await control.async_shutdown()


async def _busy_site(hass, monkeypatch, hass_storage, *, age: timedelta):
    """A site whose plan never lets go of the battery (a hold), on a new key first read `age` ago, with the hold
    and nothing else shown by the plan."""
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plan: dict[str, Any] = {"slot": None}
    control = await _started(hass, site, controller, plan_source=lambda _now: plan["slot"])
    now = await _looks(control, _soon(), 2)
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    plan["slot"] = ("hold", 0.0, None)
    now = await _looks(control, now, PROVING_LOOKS)
    key = control.firmware_key()
    assert gate.plan_shown(control.report()) == ("hold",) and _kinds(control, "verify_started") == []
    control.key_since[key] = (now - age).isoformat()
    return control, plan, now


async def test_before_the_week_is_out_the_routine_waits_for_the_plan(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, plan, now = await _busy_site(hass, monkeypatch, hass_storage, age=timedelta(days=6, hours=20))
    await _looks(control, now, 120)
    assert control.executor.state.phase == "active" and control.executor.state.action == "hold"
    assert _kinds(control, "plan_aside") == [] and _kinds(control, "verify_started") == []
    assert control.status()["line"] == "waiting_after_update:executor_busy"
    assert control.snapshot()["plan_proof"]["ahead_of_plan"] is False
    await control.async_shutdown()


async def test_after_a_week_the_routine_goes_ahead_of_the_plan_for_what_is_still_open(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, plan, now = await _busy_site(hass, monkeypatch, hass_storage, age=timedelta(days=7, hours=1))
    assert control.snapshot()["plan_proof"]["ahead_of_plan"] is True
    now = await _looks(control, now, 12)
    assert len(_kinds(control, "plan_aside")) == 1
    started = _kinds(control, "verify_started")
    assert len(started) == 1 and control.state() == "verifying"
    # The plan's hold stood aside: its hand-back showed normal, and the run tries only what is still open.
    assert set(started[0]["modes"]) >= {"charge", "discharge_house"} and "hold" not in started[0]["modes"]
    now = await _looks(control, now, 400)
    assert control.verification is None and not gate.needs_run(control.report(), control._supported_modes())
    assert gate.report_status(control.report()) == "passed" and control.state() == "on"
    # The plan has the battery again, and nothing more steps it aside.
    assert control.executor.state.phase == "active" and control.executor.state.action == "hold"
    assert len(_kinds(control, "plan_aside")) == 1 and len(_kinds(control, "verify_started")) == 1
    assert control.snapshot()["plan_proof"]["ahead_of_plan"] is False
    await control.async_shutdown()


async def test_past_the_deadline_the_plan_keeps_the_battery_while_the_moment_does_not_suit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, plan, now = await _busy_site(hass, monkeypatch, hass_storage, age=timedelta(days=30))
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    now = await _looks(control, now, 60)
    # Control never stops for the deadline: the plan runs on what passed before.
    assert control.state() == "on" and control.executor.state.action == "hold"
    assert _kinds(control, "plan_aside") == [] and _kinds(control, "verify_started") == []
    assert "grid_charge" in control.allowed()
    await control.async_shutdown()


async def test_a_moment_that_passes_ends_the_stand_aside_and_it_is_not_tried_again_within_the_gap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, plan, now = await _busy_site(hass, monkeypatch, hass_storage, age=timedelta(days=8))
    # The moment suits while the plan holds the battery, and no longer once it is handed back.
    monkeypatch.setattr(gate, "suitable", lambda moment: None if moment.executor_idle and moment.power_kw == -2.0
                        and control._aside_until is None else "battery_not_discharging")
    now = await _looks(control, now, 3)
    assert len(_kinds(control, "plan_aside")) == 1 and control.executor.state.phase != "active"
    now = await _looks(control, now, int(control_module.ASIDE_S // 10) + 6)
    assert len(_kinds(control, "plan_aside_ended")) == 1 and _kinds(control, "verify_started") == []
    assert control.state() == "on" and control.executor.state.action == "hold"
    now = await _looks(control, now, 200)
    assert len(_kinds(control, "plan_aside")) == 1, "not again within the gap"
    # Nor every hour for good where the routine keeps not starting: each is a hand-back and a new start.
    now = await _looks(control, now + timedelta(seconds=gate.RETRY_GAP_S), 30)
    assert len(_kinds(control, "plan_aside")) == 1 and control.executor.state.action == "hold"
    now = await _looks(control, now + timedelta(seconds=control_module.ASIDE_GAP_S), 3)
    assert len(_kinds(control, "plan_aside")) == 2
    await control.async_shutdown()


async def test_a_car_that_begins_to_charge_ends_the_stand_aside_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, plan, now = await _busy_site(hass, monkeypatch, hass_storage, age=timedelta(days=8))
    # The moment suits while the plan holds the battery; handed back, the routine finds a car charging.
    monkeypatch.setattr(gate, "suitable", lambda moment: (
        ("car_charging" if _kinds(control, "plan_aside") else None) if moment.executor_idle else "executor_busy"
    ))
    now = await _looks(control, now, 1)
    assert len(_kinds(control, "plan_aside")) == 1
    now = await _looks(control, now, 8)
    ended = _kinds(control, "plan_aside_ended")
    assert len(ended) == 1 and ended[0]["reason"] == "car_charging", "well inside the five minutes"
    assert _kinds(control, "verify_started") == []
    for _ in range(30):
        now = await _looks(control, now, 1)
        if control.executor.state.action == "hold":
            break
    assert control.state() == "on" and control.executor.state.action == "hold", "the plan has the battery again"
    assert len(_kinds(control, "plan_aside")) == 1
    await control.async_shutdown()


async def test_once_everything_is_shown_the_deadline_changes_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, plan, now = await _busy_site(hass, monkeypatch, hass_storage, age=timedelta(days=30))
    key = control.firmware_key()
    report = control.reports[key]
    for mode in ("normal", "charge", "discharge_house"):
        report = gate.plan_proved(report, mode, key=key, brand="sigenergy", firmware=NEW_FIRMWARE, now=now)
    control.reports[key] = report
    now = await _looks(control, now, 30)
    assert control.executor.state.action == "hold" and _kinds(control, "plan_aside") == []
    assert _kinds(control, "verify_started") == [] and control.status()["line"] == "on"
    await control.async_shutdown()
