"""The on-site test of a Sigenergy plant's own import limit (`tools/sigen_import_limit_test.py`): what it refuses to
start on, that it puts back every value it wrote in the hand-back order, that a dry run writes nothing, and how it
judges a load step (which phase took the load, held per phase or only in total)."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any

import pytest

TOOLS = Path(__file__).parent.parent / "tools"


def _tool() -> Any:
    spec = importlib.util.spec_from_file_location("sigen_import_limit_test", TOOLS / "sigen_import_limit_test.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


tool = _tool()
ENTITIES = tool.default_entities("sigen_plant")


class FakeHA:
    """Home Assistant's states as a dict; a service call sets the entity's state as the integration would."""

    def __init__(self, states: dict[str, str], *, spotnav: dict[str, Any] | None = None, dry_run: bool = False):
        self.values = dict(states)
        self.writes: list[str] = []
        self.dry_run = dry_run
        self.spotnav = spotnav or {"battery_control_enabled": True, "battery_control_shadow_only": True}

    def states(self) -> dict[str, dict[str, Any]]:
        return {entity: {"entity_id": entity, "state": value, "attributes": {}} for entity, value in self.values.items()}

    def get(self, path: str) -> Any:
        if path.startswith("/api/config/config_entries"):
            return [{"entry_id": "site", "title": "My Home", "state": "loaded"}]
        return {"data": {"config": self.spotnav}}

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        if self.dry_run:
            raise RuntimeError("dry run")
        entity = data["entity_id"]
        self.writes.append(f"{entity}={data.get('value', data.get('option', service))}")
        if domain == "number":
            self.values[entity] = str(data["value"])
        elif domain == "switch":
            self.values[entity] = "on" if service == "turn_on" else "off"
            if entity == ENTITIES["remote_ems"]:
                self.values[ENTITIES["control_mode"]] = "Command Charging (Grid First)" if service == "turn_on" \
                    else "unavailable"
        else:
            self.values[entity] = data["option"]


def _plant(**changes: str) -> dict[str, str]:
    states = {
        ENTITIES["import_limit"]: "13.8", ENTITIES["charge_limit"]: "20.6", ENTITIES["discharge_limit"]: "23.04",
        ENTITIES["remote_ems"]: "off", ENTITIES["control_mode"]: "unavailable", ENTITIES["phase_control"]: "on",
        ENTITIES["work_mode"]: "ai_mode", ENTITIES["battery"]: "-0.8", ENTITIES["soc"]: "40",
        ENTITIES["total_target"]: "0.0",
    }
    for phase in "abc":
        states[ENTITIES[f"grid_{phase}"]] = "0.3"
        states[ENTITIES[f"plant_{phase}"]] = "0.1"
        states[ENTITIES[f"target_{phase}"]] = "0.0"
    states.update({ENTITIES[role]: value for role, value in changes.items()})
    return states


def _run(ha: FakeHA, limit_kw: float = 6.0) -> Any:
    out = io.StringIO()
    return tool.Run(ha, dict(ENTITIES), limit_kw, 0.01, 4.0, [], csv.writer(out), out)


def test_a_plant_as_found_on_the_owner_s_site_may_be_tested_and_is_snapshotted() -> None:
    found = tool.check(_run(FakeHA(_plant())), force_spotnav=False)
    assert found["problems"] == []
    assert (found["snapshot"]["import_limit"], found["snapshot"]["charge_limit"]) == (13.8, 20.6)
    assert found["snapshot"]["remote_ems"] == "off" and found["snapshot"]["control_mode"] == "Maximum Self Consumption"


@pytest.mark.parametrize(
    ("changes", "spotnav", "problem"),
    [
        ({"remote_ems": "on"}, None, "Remote EMS is already on"),
        ({"phase_control": "off"}, None, "independent phase power control"),
        ({"target_b": "-1.0"}, None, "not 0"),
        ({"total_target": "2.0"}, None, "not 0"),
        ({"import_limit": "unavailable"}, None, "grid_import_limitation reads"),
        ({"soc": "97"}, None, "too full"),
        # 1.4 kW on the grid and 0.2 kW from the plant on B: 1.6 kW of house, within 0.5 kW of 2 kW.
        ({"grid_b": "1.4", "plant_b": "0.2"}, None, "phase B already draws"),
        ({}, {"battery_control_enabled": True, "battery_control_shadow_only": False}, "SpotNav's battery control"),
    ],
)
def test_the_test_does_not_start_where_something_else_controls_or_could_act(
    changes: dict[str, str], spotnav: dict[str, Any] | None, problem: str
) -> None:
    found = tool.check(_run(FakeHA(_plant(**changes), spotnav=spotnav)), force_spotnav=False)
    assert any(problem in text for text in found["problems"]), found["problems"]


def test_the_restore_puts_back_every_written_value_in_the_hand_back_order(quick: None) -> None:
    ha = FakeHA(_plant())
    run = _run(ha)
    snapshot = tool.check(run, force_spotnav=False)["snapshot"]
    run.set("import_limit", 6.0)
    run.set("charge_limit", 0.0)
    run.set("remote_ems", "on")
    run.set("control_mode", "Command Charging (Grid First)")
    run.set("charge_limit", 4.0)
    before = len(ha.writes)
    assert tool.restore(run, snapshot) is True
    assert ha.writes[before:] == [
        f"{ENTITIES['control_mode']}=Maximum Self Consumption", f"{ENTITIES['remote_ems']}=turn_off",
        f"{ENTITIES['charge_limit']}=20.6", f"{ENTITIES['import_limit']}=13.8",
    ]
    assert ha.values[ENTITIES["import_limit"]] == "13.8" and ha.values[ENTITIES["remote_ems"]] == "off"


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool, "READBACK_S", 0.05)
    monkeypatch.setattr(tool, "RESTORE_RETRY_S", 0.0)
    monkeypatch.setattr(tool, "RESTORE_DEADLINE_S", 2.0)


class StuckMode(FakeHA):
    """The control mode select takes no write (the integration logs an error)."""

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        if domain == "select":
            self.writes.append(f"{data['entity_id']}=refused")
            return
        super().call(domain, service, data)


def test_a_mode_that_will_not_take_is_given_up_after_three_tries_and_remote_ems_still_goes_off(quick: None) -> None:
    ha = StuckMode(_plant(**{"remote_ems": "on", "control_mode": "Command Charging (Grid First)"}))
    run = _run(ha)
    snapshot = {"import_limit": 13.8, "charge_limit": 20.6, "remote_ems": "off", "control_mode": "Maximum Self Consumption",
                "work_mode": "ai_mode"}
    assert tool.restore(run, snapshot, force=True) is True
    assert [w for w in ha.writes if w.endswith("=refused")] == [f"{ENTITIES['control_mode']}=refused"] * 3
    assert ha.values[ENTITIES["remote_ems"]] == "off" and ha.values[ENTITIES["import_limit"]] == "13.8"


def test_the_mode_is_skipped_only_where_remote_ems_reads_off_and_an_unread_one_is_tried_again(quick: None) -> None:
    ha = FakeHA(_plant(remote_ems="unavailable"))
    run = _run(ha)
    snapshot = {"import_limit": 13.8, "charge_limit": 20.6, "remote_ems": "off", "control_mode": "Maximum Self Consumption",
                "work_mode": "ai_mode"}
    assert tool.restore(run, snapshot, force=True) is False, "nothing written while Remote EMS cannot be read"
    assert ha.writes == []
    ha.values[ENTITIES["remote_ems"]] = "off"
    assert tool.restore(run, snapshot, force=True) is True
    assert not any(w.startswith(ENTITIES["control_mode"]) for w in ha.writes)


def test_the_snapshot_is_kept_on_disk_and_restores_on_its_own(tmp_path: Path, quick: None) -> None:
    path = tool.snapshot_path(tmp_path / "run.csv")
    assert path.name == "run.snapshot.json"
    snapshot = {"import_limit": 13.8, "charge_limit": 20.6, "remote_ems": "off", "control_mode": "Maximum Self Consumption",
                "work_mode": "ai_mode"}
    tool.save_snapshot(path, snapshot, dict(ENTITIES))
    ha = FakeHA(_plant(import_limit="6.0", charge_limit="4.0", remote_ems="on",
                       control_mode="Command Charging (Grid First)"))
    kept = json.loads(path.read_text(encoding="utf-8"))
    assert tool.restore(_run(ha), kept["snapshot"], force=True) is True
    assert ha.values[ENTITIES["control_mode"]] == "unavailable" and ha.values[ENTITIES["import_limit"]] == "13.8"
    assert ha.values[ENTITIES["charge_limit"]] == "20.6"


def test_every_stop_signal_is_ignored_while_the_restore_runs(quick: None) -> None:
    import signal as signal_module
    seen: dict[int, Any] = {}

    class Watching(FakeHA):
        def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
            seen.update({sig: signal_module.getsignal(sig) for sig in tool.STOP_SIGNALS})
            super().call(domain, service, data)

    ha = Watching(_plant(remote_ems="on", control_mode="Command Charging (Grid First)"))
    snapshot = {"import_limit": 13.8, "charge_limit": 20.6, "remote_ems": "off", "control_mode": "Maximum Self Consumption",
                "work_mode": "ai_mode"}
    assert tool.restore(_run(ha), snapshot, force=True) is True
    assert set(seen) >= {signal_module.SIGINT, signal_module.SIGTERM}
    assert all(handler is signal_module.SIG_IGN for handler in seen.values())
    assert signal_module.getsignal(signal_module.SIGINT) is not signal_module.SIG_IGN


@pytest.mark.parametrize(
    "args",
    [["--abort-phase-kw", "4.7"], ["--abort-phase-kw", "0"], ["--charge-kw", "6"], ["--limit-kw", "3", "--charge-kw", "4"],
     ["--low-charge-kw", "6.5"]],
)
def test_settings_that_could_load_a_phase_toward_the_fuse_are_refused(args: list[str], tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        tool.main(["--url", "http://localhost:1", "--token-file", str(token), *args])
    assert stop.value.code == 2


def test_a_dry_run_writes_nothing() -> None:
    ha = tool.HomeAssistant("http://localhost:1", "token", dry_run=True)
    with pytest.raises(RuntimeError, match="dry run"):
        ha.call("number", "set_value", {"entity_id": ENTITIES["import_limit"], "value": 6.0})
    assert ha.writes == []


def _sample(t: float, step: str, grid: tuple[float, float, float], plant: tuple[float, float, float]) -> Any:
    return tool.Sample(t, step, "", grid, plant, {})


def _window(run: Any, step: str, before: tuple, during: tuple, charge_kw: float) -> Any:
    """Five samples before the load (every 5 s), twelve with it (60 s)."""
    for i in range(4):
        run.samples.append(_sample(100.0 + 5 * i, step, *before))
    for i in range(13):
        run.samples.append(_sample(120.0 + 5 * i, step, *during))
    return tool.Window(step, 120.0, 180.0, charge_kw=charge_kw)


def test_a_load_held_at_a_third_of_the_limit_passes_and_its_phase_is_found_from_the_house_jump() -> None:
    run = _run(FakeHA(_plant()))
    # 2026-11-18 20:57: 2.4 kW on L1 under a 6 kW limit; the plant put out 0.33 kW on L1 and charged on L2 and L3.
    window = _window(run, "binding", ((1.65, 1.65, 1.65), (-1.45, -1.45, -1.45)),
                     ((2.0, 2.0, 2.0), (0.33, -1.82, -1.96)), 4.0)
    result = tool.judge(run, window)
    assert result["verdict"] == "PASS" and result["phase"] == "A" and result["load_kw"] == pytest.approx(2.13, abs=0.01)


def test_a_phase_over_a_third_of_the_limit_fails() -> None:
    run = _run(FakeHA(_plant()))
    window = _window(run, "binding", ((1.65, 1.65, 1.65), (-1.45, -1.45, -1.45)),
                     ((2.6, 1.7, 1.7), (-1.0, -1.45, -1.45)), 4.0)
    assert tool.judge(run, window)["verdict"] == "FAIL"


@pytest.mark.parametrize(
    ("during", "verdict"),
    [
        # Even charge of 1 kW, 2.4 kW on L2: L2 at 3.0 kW with 4.6 kW in all: the plant limits only the total.
        (((0.6, 3.0, 0.6), (-0.33, -0.33, -0.33)), "FAIL: the plant limits only the total, not each phase"),
        # The plant puts out on L2 and holds it at 2 kW while the total is under the limit: held per phase.
        (((0.6, 2.0, 0.6), (-0.33, 0.67, -0.33)), "PASS"),
        # A load too small to take L2 over 2 kW even with an even charge: nothing shown.
        (((0.6, 1.2, 0.6), (-0.33, -0.33, -0.33)), "not judged (the load did not take its phase"),
    ],
)
def test_the_under_limit_step_tells_a_per_phase_limit_from_a_total_one(during: tuple, verdict: str) -> None:
    run = _run(FakeHA(_plant()))
    window = _window(run, "under", ((0.6, 0.6, 0.6), (-0.33, -0.33, -0.33)), during, 1.0)
    assert tool.judge(run, window)["verdict"].startswith(verdict)


def test_the_under_limit_step_is_not_judged_where_the_total_reaches_the_limit() -> None:
    run = _run(FakeHA(_plant()))
    # A 4.5 kW load: the total would pass 6 kW, so the total limit alone explains the hold.
    window = _window(run, "under", ((0.6, 0.6, 0.6), (-0.33, -0.33, -0.33)),
                     ((2.0, 2.0, 2.0), (-1.0, 3.1, -1.0)), 1.0)
    assert tool.judge(run, window)["verdict"].startswith("not judged (the total reached the limit")


def test_a_step_the_plant_could_not_be_read_in_is_left_to_the_person() -> None:
    run = _run(FakeHA(_plant()))
    for i in range(20):
        run.samples.append(tool.Sample(100.0 + 5 * i, "stopped", "", (None, None, None), (None, None, None), {}))
    result = tool.judge(run, tool.Window("stopped", 120.0, 180.0))
    assert result["verdict"].startswith("not judged")


def test_the_guard_restores_when_a_phase_stays_over_the_abort_level() -> None:
    ha = FakeHA(_plant())
    run = _run(ha)
    run.guarding = True
    run.sample()
    ha.values[ENTITIES["grid_b"]] = "4.5"
    run.sample()
    with pytest.raises(tool.Stop, match="over 4.0 kW"):
        run.sample()


def test_a_load_that_never_took_its_phase_over_a_third_of_the_limit_is_not_judged_in_any_step() -> None:
    run = _run(FakeHA(_plant()))
    # 0.3 kW on A with a 1.5 kW charge: 0.6 + 0.5 kW, far under 2 kW; nothing needed holding.
    window = _window(run, "binding", ((0.4, 0.4, 0.4), (-0.5, -0.5, -0.5)), ((0.7, 0.4, 0.4), (-0.5, -0.5, -0.5)), 1.5)
    assert tool.judge(run, window)["verdict"] == tool.NOT_PUSHED


def _stopped(run: Any, *, grid_during: tuple[float, float, float], meter_during=None, writes: int = 0) -> Any:
    """The writes-stopped window, `sigen` still read: 2.4 kW on A under a 4 kW charge and a 6 kW limit."""
    for i in range(4):
        run.samples.append(tool.Sample(100.0 + 5 * i, "stopped", "", (1.65, 1.65, 1.65), (-1.45, -1.45, -1.45), {},
                                       (1.65, 1.65, 1.65)))
    plant = (0.33, -1.82, -1.96) if max(grid_during) <= 2.1 else (-1.0, -1.45, -1.45)
    for i in range(13):
        run.samples.append(tool.Sample(120.0 + 5 * i, "stopped", "", grid_during, plant, {},
                                       meter_during or grid_during))
    return tool.Window("stopped", 120.0, 180.0, stopped=True, charge_kw=4.0, writes=writes)


def test_the_stopped_step_is_judged_on_the_plant_s_grid_sensor_like_the_others() -> None:
    run = _run(FakeHA(_plant()))
    result = tool.judge(run, _stopped(run, grid_during=(2.0, 2.0, 2.0)))
    assert result["verdict"] == "PASS" and result["phase"] == "A" and result["writes_in_window"] == 0
    run = _run(FakeHA(_plant()))
    assert tool.judge(run, _stopped(run, grid_during=(2.6, 1.7, 1.7)))["verdict"] == "FAIL"


def test_the_stopped_step_is_not_judged_when_anything_was_written_in_its_window() -> None:
    run = _run(FakeHA(_plant()))
    result = tool.judge(run, _stopped(run, grid_during=(2.0, 2.0, 2.0), writes=1))
    assert result["verdict"].startswith("not judged (something was written")


def test_an_independent_meter_when_given_must_agree() -> None:
    run = _run(FakeHA(_plant()))
    run.meter = ("sensor.p1_l1", "sensor.p1_l2", "sensor.p1_l3")
    assert tool.judge(run, _stopped(run, grid_during=(2.0, 2.0, 2.0)))["verdict"] == "PASS"
    run = _run(FakeHA(_plant()))
    run.meter = ("sensor.p1_l1", "sensor.p1_l2", "sensor.p1_l3")
    result = tool.judge(run, _stopped(run, grid_during=(2.0, 2.0, 2.0), meter_during=(3.0, 1.5, 1.5)))
    assert result["verdict"].startswith("FAIL: the independent meter does not agree")


def test_the_stopped_step_counts_toward_recording_the_fact(capsys: pytest.CaptureFixture[str]) -> None:
    run = _run(FakeHA(_plant()))
    tool.summary(run, [{"step": "binding", "verdict": "PASS"}, {"step": "under", "verdict": "PASS"},
                       {"step": "stopped", "verdict": "PASS"}], True)
    assert "RESULT: PASS. " in capsys.readouterr().out


def test_a_person_s_yes_is_only_a_labelled_fallback(capsys: pytest.CaptureFixture[str]) -> None:
    run = _run(FakeHA(_plant()))
    results = [
        {"step": "binding", "verdict": "PASS"}, {"step": "under", "verdict": "PASS"},
        {"step": "stopped", "verdict": "not judged (the plant could not be read: use the app or a meter)",
         "verdict_from_person": "y"},
    ]
    tool.summary(run, results, True)
    out = capsys.readouterr().out
    assert "FALLBACK only" in out and "RESULT: PASS while writing only" in out
    assert "RESULT: PASS. " not in out


def test_the_meter_reads_power_or_current_and_is_given_per_phase() -> None:
    assert tool.parse_meter("a=sensor.x,b=sensor.y,c=sensor.z") == ("sensor.x", "sensor.y", "sensor.z")
    assert tool.parse_meter("a=sensor.x,b=sensor.y") is None
    assert tool.meter_kw({"state": "10", "attributes": {"unit_of_measurement": "A"}}) == pytest.approx(2.3)
    assert tool.meter_kw({"state": "1500", "attributes": {"unit_of_measurement": "W"}}) == pytest.approx(1.5)
    assert tool.meter_kw({"state": "unavailable", "attributes": {"unit_of_measurement": "A"}}) is None


def _scripted(monkeypatch: pytest.MonkeyPatch, run: Any, phases: dict[str, str]) -> None:
    """The steps without waiting: each load step lands on the phase given, every prompt answered at once."""
    def load_step(run: Any, step: str, *, stopped: bool = False) -> dict[str, Any]:
        result = {"step": step, "phase": phases.get(step, "A"), "load_kw": 2.4, "verdict": "PASS"}
        run.results.append(result)
        return result

    monkeypatch.setattr(tool, "load_step", load_step)
    monkeypatch.setattr(run, "settle", lambda seconds: None)
    monkeypatch.setattr(run, "wait", lambda prompt, least_s=0.0: "")
    monkeypatch.setattr(run, "marker", lambda name: 0.0)
    monkeypatch.setattr(tool.Run, "wait_available", lambda self, role, wait_s: True)


def _args(**changes: Any) -> Any:
    import argparse
    values = {"limit_kw": 6.0, "charge_kw": 4.0, "low_charge_kw": 1.0, "second_phase": False, "sensor_loss": False,
              "skip_stopped": True} | changes
    return argparse.Namespace(**values)


@pytest.mark.parametrize(("second", "verdict"), [("B", "PASS"), ("A", "not judged (the load landed on the same phase")])
def test_the_under_limit_step_may_be_repeated_on_a_second_phase(
    monkeypatch: pytest.MonkeyPatch, quick: None, second: str, verdict: str
) -> None:
    run = _run(FakeHA(_plant()))
    _scripted(monkeypatch, run, {"binding": "A", "under": "A", "under2": second})
    tool.steps(run, _args(second_phase=True))
    results = {r["step"]: r["verdict"] for r in run.results}
    assert results["under2"].startswith(verdict)


def test_the_sensor_loss_step_only_asks_and_writes_nothing_of_its_own(monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    ha = FakeHA(_plant())
    run = _run(ha)
    _scripted(monkeypatch, run, {})
    tool.steps(run, _args())
    without = list(ha.writes)
    ha2 = FakeHA(_plant())
    run2 = _run(ha2)
    _scripted(monkeypatch, run2, {})
    tool.steps(run2, _args(sensor_loss=True))
    assert any(r["step"] == "sensor_loss" for r in run2.results)
    # Only the charge back at 4 kW the stopped step writes anyway.
    assert ha2.writes == [*without, f"{ENTITIES['charge_limit']}=4.0"]
