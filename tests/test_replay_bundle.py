"""The bundle replay tool (`tools/replay_bundle.py`) on a synthetic bundle built like tests/test_core_replay.py's."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from custom_components.spotnav.core import events as ev

spec = importlib.util.spec_from_file_location("replay_bundle", Path(__file__).parent.parent / "tools" / "replay_bundle.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

AT = "2026-10-04T22:00:00+00:00"
STOPPED = {"owner": "none", "manual": {"action": "stop", "scope": "plug_in"}, "span_pause": None}


def _record(event, *, core=(), today=(), result=None, pre=None, after=None):
    record = {"at": AT, "event": event.to_dict(), "core": [{"kind": kind} for kind in core], "today": list(today)}
    if result is not None:
        record["result"] = result.to_dict()
    if pre is not None:
        record["pre"] = pre
    if after is not None:
        record["today_after"] = after
    return record


def _records(*, today_stop=True) -> list[dict]:
    return [
        _record(
            ev.PersonStop(connected=True),
            core=["stop"],
            today=["stop"] if today_stop else [],
            result=ev.CommandResult(command="stop", reason="person", executed=True),
            pre={"version": 1, "plugged": True, "owner": "plan"},
            after=STOPPED,
        ),
        _record(ev.Unplug(previous=True), after={"owner": "none", "manual": None, "span_pause": None}),
    ]


def _bundle() -> dict:
    drift = {"at": AT, "fields": {"owner": {"core": "plan", "today": "none"}}, "after": "person_stop"}
    disagreement = {"at": AT, "where": "person_stop", "queued": False, "fields": {"owner": {"core": "plan", "today": "none"}},
                    "commands": {"core": ["stop"], "today": []}, "events": [{"event": {"kind": "person_stop"}}]}
    shadow = {"counts": {"events": 2, "disagreements": 1, "drift": 1}, "session": {}, "disagreements": [disagreement],
              "drift": [drift], "events": _records()}
    return {
        "bundle_version": 5,
        "chargers": [
            {"entry_id": "id_a", "title": "Garage", "diagnostics": {"controller": {"ownership_shadow": shadow}}},
            {"entry_id": "id_b", "title": "Old", "diagnostics": {"controller": {}}},
        ],
    }


def test_a_charger_with_a_shadow_replays_and_shows_its_records() -> None:
    results = tool.analyse(_bundle())
    garage, old = results
    assert garage["events"] == 2 and garage["differs_from_recording"] == [] and garage["differs_from_today"] == []
    text = tool.render(results)
    assert "Garage (id_a)" in text and "disagreements recorded: 1" in text and "drift recorded: 1" in text
    assert "core ['stop'], today []" in text
    assert "decides differently from the recording: no" in text


def test_a_charger_without_the_shadow_block_gets_a_clear_message() -> None:
    assert tool.analyse(_bundle())[1]["shadow"] is False
    assert "No ownership shadow" in tool.render(tool.analyse(_bundle()))



def test_a_difference_from_todays_code_is_reported() -> None:
    bundle = _bundle()
    bundle["chargers"][0]["diagnostics"]["controller"]["ownership_shadow"]["events"] = _records(today_stop=False)
    result = tool.analyse(bundle)[0]
    assert [item["index"] for item in result["differs_from_today"]] == [0]
    assert "the core disagrees with the code that ran, on replay: yes" in tool.render([result])


def test_charger_selection_by_name_or_id(tmp_path, capsys) -> None:
    bundle = _bundle()
    assert [r["entry_id"] for r in tool.analyse(bundle, "garage")] == ["id_a"]
    assert [r["entry_id"] for r in tool.analyse(bundle, "id_b")] == ["id_b"]
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    assert tool.main([str(path), "--charger", "nobody"]) == 2
    capsys.readouterr()


def test_the_command_line_prints_json_and_sets_the_exit_code(tmp_path, capsys) -> None:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle()), encoding="utf-8")
    assert tool.main([str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bundle_version"] == 5 and data["chargers"][0]["events"] == 2
    broken = _bundle()
    broken["chargers"][0]["diagnostics"]["controller"]["ownership_shadow"]["events"] = _records(today_stop=False)
    path.write_text(json.dumps(broken), encoding="utf-8")
    assert tool.main([str(path)]) == 1
    capsys.readouterr()
    assert tool.main([str(tmp_path / "missing.json")]) == 2
