"""The battery control's shell, pinned before its write path is split: for Sigenergy on the fake Home Assistant of
`test_battery_control`, the order of the store's saves and the plant's service calls, look by look, for a normal
enter and stop (the snapshot is stored before the first write), a write the options no longer allow, calls that
fail, a start on a stored snapshot, and the hand-back of a removed site's stored snapshot
(`fixtures/battery/control_trace.json`).

A save is recorded as the stored keys that changed since the save before (the executor's state one level down),
never their values: times are the wall clock's.

Regenerate only on purpose: `SPOTNAV_RECORD_GOLDEN=1 pytest tests/test_battery_control_golden.py`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery.control import BatteryControl, store_key
from tests import battery_patch
from tests.test_battery_control import _hold, _looks, _options, _site, _started, _stored_hold

pytestmark = pytest.mark.usefixtures("offline_relay")

FIXTURE = Path(__file__).parent / "fixtures" / "battery" / "control_trace.json"
RECORD = bool(os.environ.get("SPOTNAV_RECORD_GOLDEN"))


class _Calls(list):  # type: ignore[type-arg]
    """The plant's service calls, each also a row of the shared log."""

    def __init__(self, log: list[Any]) -> None:
        super().__init__()
        self._log = log

    def append(self, call: Any) -> None:
        super().append(call)
        domain, service, data = call
        self._log.append(["call", domain, service, json.loads(json.dumps(data, sort_keys=True, default=str))])


def _changed(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        if key == "state" and isinstance(after, dict):
            before = before if isinstance(before, dict) else {}
            keys += [f"state.{name}" for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
        else:
            keys.append(key)
    return keys


class _Recorder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
        self.log: list[Any] = []
        self._last: dict[str, Any] = {}
        recorder = self

        class Recording(Store):  # type: ignore[type-arg]
            """The shell's store, each save and removal also a row of the shared log."""

            async def async_save(self, data: Any) -> None:
                if self.key == key:
                    copy = json.loads(json.dumps(data, default=str))
                    recorder.log.append(["save", _changed(recorder._last, copy)])
                    recorder._last = copy
                await super().async_save(data)

            async def async_remove(self) -> None:
                if self.key == key:
                    recorder.log.append(["remove"])
                await super().async_remove()

        # The shell's own name for the class: stores made from here on are recorded (never `Store` itself, which
        # the storage fixture patches).
        battery_patch.patch(monkeypatch, "Store", Recording)

    def watch(self, plant: Any) -> None:
        plant.calls = _Calls(self.log)

    def mark(self, what: str) -> None:
        self.log.append(["--", what])

    async def looks(self, control: BatteryControl, start: Any, count: int) -> Any:
        now = start
        for _ in range(count):
            self.mark("look")
            now = await _looks(control, now, 1)
        return now


def _check(name: str, log: list[Any]) -> None:
    stored = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
    if RECORD:
        stored[name] = log
        FIXTURE.write_text(json.dumps(stored, indent=1, sort_keys=True) + "\n")
        return
    assert json.dumps(log, sort_keys=True) == json.dumps(stored[name], sort_keys=True), name


async def test_an_enter_stores_the_snapshot_before_its_first_write_and_a_stop_hands_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    recorder = _Recorder(monkeypatch, store_key(site.entry_id))
    recorder.watch(plant)
    control = await _started(hass, site, controller, plan_source=_hold)
    await recorder.looks(control, dt_util.now(), 6)
    recorder.mark("stop")
    await control.async_stop()
    first_call = next(index for index, row in enumerate(recorder.log) if row[0] == "call")
    assert any(row[0] == "save" and "state.snapshot" in row[1] for row in recorder.log[:first_call])
    _check("enter_and_stop", recorder.log)


async def test_a_write_the_options_no_longer_allow_becomes_a_hand_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    recorder = _Recorder(monkeypatch, store_key(site.entry_id))
    recorder.watch(plant)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = await recorder.looks(control, dt_util.now(), 6)
    recorder.mark("shadow only")
    _options(hass, site, shadow_only=True)
    await recorder.looks(control, now, 6)
    recorder.mark("stop")
    await control.async_stop()
    _check("options_changed", recorder.log)


async def test_calls_that_fail_are_retried_and_then_given_up_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    recorder = _Recorder(monkeypatch, store_key(site.entry_id))
    recorder.watch(plant)
    control = await _started(hass, site, controller, plan_source=_hold)
    plant.raises = True
    now = await recorder.looks(control, dt_util.now(), 6)
    recorder.mark("the plant takes writes again")
    plant.raises = False
    await recorder.looks(control, now, 6)
    recorder.mark("stop")
    # The restore waits for its next attempt: the stop gives up on it at once and leaves it stored.
    battery_patch.patch(monkeypatch, "HAND_BACK_DEADLINE_S", 0.05)
    await control.async_stop()
    _check("failing_calls", recorder.log)


async def test_a_start_on_a_stored_snapshot_hands_it_back_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _stored_hold(hass, monkeypatch, hass_storage)
    recorder = _Recorder(monkeypatch, store_key(site.entry_id))
    recorder.watch(plant)
    control = BatteryControl(hass, controller, plan_source=_hold)
    recorder.mark("start")
    await control.async_start()
    await recorder.looks(control, dt_util.now(), 6)
    recorder.mark("shutdown")
    await control.async_shutdown()
    _check("stored_start", recorder.log)


async def test_a_removed_sites_stored_snapshot_is_handed_back_and_the_store_removed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, _controller, plant = await _stored_hold(hass, monkeypatch, hass_storage)
    recorder = _Recorder(monkeypatch, store_key(site.entry_id))
    recorder.watch(plant)
    battery_patch.patch(monkeypatch, "HAND_BACK_DEADLINE_S", 30.0)
    battery_patch.patch(monkeypatch, "HAND_BACK_LOOK_S", 0.01)
    await BatteryControl.async_remove_stored(hass, site.entry_id, dict(site.data))
    assert any(row[0] == "call" for row in recorder.log)
    _check("removed_site", recorder.log)
