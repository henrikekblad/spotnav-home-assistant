"""The battery's own heater (`battery/preheat.py`, `plans/v3_sigen_preheat.md`): the facts read, and the battery
section's line about a cold battery whose preheating is off."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from custom_components.spotnav.battery.facts import EntityFact, battery_facts
from custom_components.spotnav.battery.preheat import SHOW_AFTER, PreheatWatch

SIGEN = json.loads((Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8"))["sigenergy"]
CELLS = "sensor.sigen_plant_ess_average_cell_temperature"
ENABLE = "switch.sigen_plant_ess_preheating_enable"
T0 = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)


def _row(entity_id: str, key: str, state: str | None, disabled: bool = False) -> dict[str, Any]:
    return {"entity_id": entity_id, "unique_id": f"ENTRY_sigen_plant_{key}", "device": "battery", "state": state,
            "unit": None, "device_class": None, "disabled": disabled}


def _heater(enable: str | None = "off", *, disabled: bool = False, slots: int = 1) -> list[dict[str, Any]]:
    """The `sigen` integration's preheating entities (`switch.py`, `select.py`, `number.py`), named as it names them."""
    rows = [
        _row(ENABLE, "plant_ess_preheating_enable", enable, disabled),
        _row("switch.sigen_plant_ess_preheating_advance_enable", "plant_ess_preheating_advance_enable", None, True),
        _row("select.sigen_plant_ess_preheating_mode", "plant_ess_preheating_mode", None, True),
        _row("number.sigen_plant_ess_preheating_reserved_soc", "plant_ess_preheating_reserved_soc", None, True),
    ]
    for n in range(1, slots + 1):
        for field in ("start_time", "end_time", "target_power"):
            key = f"plant_ess_preheating_tou_{n}_{field}"
            rows.append(_row(f"number.sigen_{key}", key, None, True))
    return rows


def _facts(cell: str | None = "2.4", heater: list[dict[str, Any]] | None = None):
    entities = []
    for row in [*SIGEN["entities"], *(_heater() if heater is None else heater)]:
        state = cell if row["entity_id"] == CELLS else row["state"]
        entities.append(EntityFact(
            entity_id=row["entity_id"], platform="sigen", unique_id=row["unique_id"], device_id=row["device"],
            config_entry_id="entry", disabled=row["disabled"], device_class=row["device_class"], state=state,
            unit=row["unit"],
        ))
    battery = next(entity for entity in entities if entity.entity_id == SIGEN["battery"])
    return battery_facts(battery, entities, manufacturer="Sigenergy", power_kw=0.0)


# --- the facts ---------------------------------------------------------------------------------------------------


def test_the_cells_temperature_is_read_live_and_the_heaters_switch_and_schedule_are_found() -> None:
    facts = _facts()
    assert facts.cell_temperature_c == 2.4
    heater = facts.preheat
    assert heater is not None and heater.enable_entity == ENABLE and heater.state == "off"
    assert heater.device_id == "battery"
    assert heater.entities["mode"] == "select.sigen_plant_ess_preheating_mode"
    assert heater.entities["advance"] == "switch.sigen_plant_ess_preheating_advance_enable"
    assert heater.entities["slot_1_power"] == "number.sigen_plant_ess_preheating_tou_1_target_power"
    assert "slot_2_start" not in heater.entities
    assert {"mode", "advance", "slot_1_start"} <= set(heater.disabled)
    assert facts.as_dict()["preheat"]["state"] == "off"
    json.dumps(facts.as_dict())


@pytest.mark.parametrize(
    ("state", "disabled", "expected"),
    [("on", False, "on"), ("off", False, "off"), (None, True, "disabled"), ("unavailable", False, "unavailable")],
)
def test_the_switch_reads_on_off_disabled_or_unavailable(state: str | None, disabled: bool, expected: str) -> None:
    facts = _facts(heater=_heater(state, disabled=disabled))
    assert facts.preheat is not None and facts.preheat.state == expected


def test_no_switch_no_heater_and_no_reading_no_temperature() -> None:
    assert _facts(heater=[]).preheat is None
    assert _facts(cell="unavailable").cell_temperature_c is None


# --- the line ----------------------------------------------------------------------------------------------------


def _run(watch: PreheatWatch, facts, *, minutes: int, start: datetime = T0, controlling: bool = True):
    changes = []
    now = start
    for _ in range(minutes + 1):
        change = watch.look(now, facts, controlling)
        if change is not None:
            changes.append(change)
        now += timedelta(minutes=1)
    return changes, now


def test_a_cold_battery_with_preheating_off_shows_the_line_after_half_an_hour_and_clears_when_turned_on() -> None:
    watch = PreheatWatch()
    facts = _facts("2.4")
    changes, now = _run(watch, facts, minutes=int(SHOW_AFTER.total_seconds() // 60) - 1)
    assert changes == [] and watch.status(facts) is None, "a passing reading does not nag"
    changes, now = _run(watch, facts, minutes=1, start=now)
    assert changes == [("shown", "off")]
    assert watch.status(facts) == {"code": "off", "cell_c": 2.4, "entity_id": ENABLE, "device_id": "battery"}
    on = _facts("2.4", _heater("on"))
    assert watch.look(now, on, True) == ("cleared", "off")
    assert watch.status(on) is None
    assert watch.counts == {"shown_off": 1}


def test_a_disabled_switch_says_so_and_an_unavailable_one_is_only_counted() -> None:
    watch = PreheatWatch()
    disabled = _facts("1.0", _heater(None, disabled=True))
    changes, _now = _run(watch, disabled, minutes=40)
    assert changes == [("shown", "disabled")] and watch.status(disabled)["code"] == "disabled"

    watch = PreheatWatch()
    unreadable = _facts("1.0", _heater("unavailable"))
    changes, _now = _run(watch, unreadable, minutes=90)
    assert changes == [] and watch.status(unreadable) is None
    assert watch.counts == {"unreadable": 1}


def test_only_while_spotnav_runs_the_battery_and_only_when_cold() -> None:
    watch = PreheatWatch()
    changes, _now = _run(watch, _facts("2.0"), minutes=60, controlling=False)
    assert changes == []
    changes, _now = _run(PreheatWatch(), _facts("6.0"), minutes=60)
    assert changes == [], "6 °C is not cold"
    changes, _now = _run(PreheatWatch(), _facts(None), minutes=60)
    assert changes == [], "no reading, no line"


def test_the_line_stays_while_cells_warm_to_seven_degrees_and_clears_from_there() -> None:
    watch = PreheatWatch()
    changes, now = _run(watch, _facts("4.0"), minutes=31)
    assert changes == [("shown", "off")]
    assert watch.look(now, _facts("6.5"), True) is None, "warming cells do not flicker the line"
    assert watch.look(now, _facts("7.0"), True) == ("cleared", "off")
    # Cold again: half an hour before it shows again.
    changes, _now = _run(watch, _facts("4.0"), minutes=29, start=now)
    assert changes == []


def test_control_turned_off_clears_the_line_at_once() -> None:
    watch = PreheatWatch()
    facts = _facts("3.0")
    _changes, now = _run(watch, facts, minutes=31)
    assert watch.look(now, facts, False) == ("cleared", "off")
