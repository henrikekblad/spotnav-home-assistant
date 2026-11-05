"""A charger whose SpotNav entry is deleted for good is handed back as SpotNav found it, as far as SpotNav knows
(`execution/charger_handback.py`): the current it carried before SpotNav first wrote one, when that was read, and a
stop of SpotNav's own undone. A person's stop, a stop outside SpotNav and an unknown current are left as they are.
An unload or a reload hands nothing back, and a write that fails is logged and does not hold the removal up."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.spotnav.const import CURRENT_CONTROL_NUMBER, DOMAIN
from custom_components.spotnav.execution import charger_handback
from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED
from custom_components.spotnav.runtime import executor_for, preview_for

from tests.helpers import make_entry
from tests.world import controller_of

pytestmark = pytest.mark.usefixtures("offline_relay")

NUMBER = "number.charger_limit"
SWITCH = "switch.charger_a"
ENTRY = "entry_a"


class Charger:
    """A generic charger whose switch and current number follow what is asked of them, as a real one's do, and the
    calls each took."""

    def __init__(self, hass: HomeAssistant, entry: Any, maximum: float) -> None:
        self.hass = hass
        self.entry = entry
        self.maximum = maximum
        self.turn_on: list[ServiceCall] = []
        self.turn_off: list[ServiceCall] = []
        self.set_value: list[ServiceCall] = []
        hass.services.async_register("switch", "turn_on", self._on)
        hass.services.async_register("switch", "turn_off", self._off)
        hass.services.async_register("number", "set_value", self._set)

    async def _on(self, call: ServiceCall) -> None:
        self.turn_on.append(call)
        self.hass.states.async_set(SWITCH, "on")

    async def _off(self, call: ServiceCall) -> None:
        self.turn_off.append(call)
        self.hass.states.async_set(SWITCH, "off")

    async def _set(self, call: ServiceCall) -> None:
        self.set_value.append(call)
        self.number(str(call.data["value"]))

    def number(self, value: str) -> None:
        self.hass.states.async_set(
            NUMBER, value, {"unit_of_measurement": "A", "min": 6, "max": self.maximum, "step": 1}
        )

    @property
    def controller(self) -> Any:
        return controller_of(self.hass, self.entry.entry_id)

    def forget_calls(self) -> None:
        self.turn_on.clear()
        self.turn_off.clear()
        self.set_value.clear()

    async def remove(self) -> None:
        self.forget_calls()
        await self.hass.config_entries.async_remove(self.entry.entry_id)
        await self.hass.async_block_till_done()
        assert self.hass.config_entries.async_get_entry(self.entry.entry_id) is None

    @property
    def values(self) -> list[float]:
        return [call.data["value"] for call in self.set_value]


async def _charger(
    hass: HomeAssistant,
    *,
    switch: str = "off",
    amps: str = "10",
    maximum: float = 16,
    current: bool = True,
    extra: dict[str, Any] | None = None,
) -> Charger:
    """A generic charger with a switch and (opted in) a current number, set up through Home Assistant."""
    hass.states.async_set(SWITCH, switch)
    hass.states.async_set(NUMBER, amps, {"unit_of_measurement": "A", "min": 6, "max": maximum, "step": 1})
    entry = make_entry(
        hass,
        entry_id=ENTRY,
        charge_control=SWITCH,
        current_limit=NUMBER,
        webhook_id="webhook-a",
        title="Garage",
        current_control=CURRENT_CONTROL_NUMBER if current else "",
        extra=extra,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # After setup: the core `switch` and `number` components register their own handlers while it runs.
    return Charger(hass, entry, maximum)


async def _spotnav_charges(charger: Charger, amps: int = 14) -> None:
    """SpotNav starts a charge at `amps` (the sun's), its first write of the charger's current."""
    assert await charger.controller.async_start(amps, cause="solar")
    await charger.hass.async_block_till_done()
    assert charger.values == [amps]


async def _plan_window_ends(charger: Charger) -> None:
    controller = charger.controller
    async with controller._lock:  # noqa: SLF001 - the plan's own stop, as a window's end sends it
        assert await controller._automatic_stop_locked("the window's end")  # noqa: SLF001
    await charger.hass.async_block_till_done()


async def _solar_stands_down(charger: Charger) -> None:
    await charger.controller.async_stop(urgent=False)
    await charger.hass.async_block_till_done()


async def _balancing_pauses(charger: Charger) -> None:
    await charger.controller.async_stop(balancing=True, urgent=False)
    await charger.hass.async_block_till_done()


async def _strategy_changes(charger: Charger) -> None:
    await charger.controller.async_stop(clear_schedule=True, urgent=False)
    await charger.hass.async_block_till_done()


# ------------------------------------------------------------------------------------------------ the current


async def test_removal_gives_back_the_current_the_charger_had_before_spotnav(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await charger.remove()
    assert charger.values == [10], "one write, of the value read before SpotNav's first"
    assert charger.turn_on == [], "the charger was charging: nothing to start"


async def test_later_writes_do_not_change_what_is_given_back(hass: HomeAssistant, freezer: Any) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    for amps in (8, 12):
        freezer.tick(120)
        await charger.controller.async_apply_regulated_current(amps, must_lower=False)
        await hass.async_block_till_done()
    assert charger.values[-1] == 12
    await charger.remove()
    assert charger.values == [10]


async def test_a_current_that_already_reads_the_value_is_not_written(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    charger.number("10")  # the person set it back in the charger's own app
    await hass.async_block_till_done()
    await charger.remove()
    assert charger.values == []


async def test_an_unknown_current_before_spotnav_is_left_as_it_is(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    # 0 A is no current a charge runs at: what the person meant by it cannot be told.
    charger = await _charger(hass, amps="0")
    await _spotnav_charges(charger, 14)
    with caplog.at_level(logging.INFO):
        await charger.remove()
    assert charger.values == []
    assert "is not known, so its current is left as it is" in caplog.text


async def test_a_current_spotnav_never_wrote_is_left_as_it_is(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    charger.number("12")
    await charger.remove()
    assert charger.values == []


async def test_a_current_spotnav_may_not_write_is_left_alone(hass: HomeAssistant) -> None:
    """Without the opt-in SpotNav never wrote the current, so there is nothing of ours to undo."""
    charger = await _charger(hass, current=False)
    await charger.remove()
    assert charger.values == []


async def test_an_installation_from_before_the_record_writes_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """SpotNav may have written to it before this release: what it reads now is not the person's."""
    hass_storage[f"{DOMAIN}.{ENTRY}"] = {"version": 1, "minor_version": 1, "key": f"{DOMAIN}.{ENTRY}", "data": {}}
    hass_storage[f"{DOMAIN}.{ENTRY}"]["data"] = {"plan": None, "requested_current_a": 14}
    charger = await _charger(hass, amps="14")
    await _spotnav_charges(charger, 12)
    await charger.remove()
    assert charger.values == []


async def test_the_recorded_current_survives_a_restart(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    assert await hass.config_entries.async_reload(charger.entry.entry_id)
    await hass.async_block_till_done()
    charger.forget_calls()
    assert charger.controller.adapter.pre_current.amps == 10
    await charger.remove()
    assert charger.values == [10]


async def test_a_flash_stored_current_is_written_once(hass: HomeAssistant) -> None:
    """A charger that keeps its current in flash (`WritePolicy.flash_stored`) takes the one hand-back write."""
    charger = await _charger(hass, switch="on", amps="8", maximum=32, extra={"charger_platform": "alfen_wallbox"})
    await _spotnav_charges(charger, 16)
    await charger.remove()
    assert charger.values == [8]


async def test_a_failed_write_is_logged_and_the_removal_goes_on(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await _solar_stands_down(charger)

    async def refuse(_call: ServiceCall) -> None:
        raise HomeAssistantError("the charger said no")

    hass.services.async_register("number", "set_value", refuse)
    with caplog.at_level(logging.WARNING):
        await charger.remove()
    assert len(charger.turn_on) == 1, "the start still went out"
    assert "current could not be set back to 10 A" in caplog.text or "current was not set back to 10 A" in caplog.text


# ---------------------------------------------------------------------------------------------------- the stop


@pytest.mark.parametrize("stop", [_plan_window_ends, _solar_stands_down, _balancing_pauses, _strategy_changes])
async def test_a_stop_of_spotnavs_own_is_undone(hass: HomeAssistant, stop: Any) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await stop(charger)
    assert hass.states.get(SWITCH).state == "off"
    assert charger.controller._own_stop is True  # noqa: SLF001
    await charger.remove()
    assert [call.data["entity_id"] for call in charger.turn_on] == [SWITCH]
    assert charger.values == [10]


async def test_a_persons_stop_through_the_card_is_not_undone(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await executor_for(hass, ENTRY).async_manual_stop()
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "off"
    assert charger.controller._own_stop is False  # noqa: SLF001 - the person's, not SpotNav's
    await charger.remove()
    assert charger.turn_on == []
    assert charger.values == [10], "the current is still given back"


async def test_a_pause_the_person_picks_keeps_a_stop_of_spotnavs_as_theirs(hass: HomeAssistant) -> None:
    """SpotNav had stopped the charger; then the person paused SpotNav: what keeps it stopped is now theirs."""
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await _solar_stands_down(charger)
    preview = preview_for(hass, ENTRY)
    assert preview is not None
    await preview.async_pause(PAUSE_UNTIL_RESUMED)
    await hass.async_block_till_done()
    assert charger.controller._own_stop is False  # noqa: SLF001
    await charger.remove()
    assert charger.turn_on == []


async def test_a_switch_turned_off_outside_spotnav_is_not_undone(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    hass.states.async_set(SWITCH, "off")  # the charger's own app
    await hass.async_block_till_done()
    await charger.remove()
    assert charger.turn_on == []


async def test_a_stop_of_spotnavs_someone_then_started_and_stopped_is_not_undone(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await _solar_stands_down(charger)
    assert charger.controller._own_stop is True  # noqa: SLF001
    hass.states.async_set(SWITCH, "on")  # someone started it outside SpotNav
    await hass.async_block_till_done()
    assert charger.controller._own_stop is False  # noqa: SLF001
    hass.states.async_set(SWITCH, "off")  # and stopped it again
    await hass.async_block_till_done()
    await charger.remove()
    assert charger.turn_on == []


async def test_a_stop_of_spotnavs_own_survives_a_restart(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await _plan_window_ends(charger)
    assert await hass.config_entries.async_reload(charger.entry.entry_id)
    await hass.async_block_till_done()
    await charger.remove()
    assert [call.data["entity_id"] for call in charger.turn_on] == [SWITCH]


async def test_a_charger_already_on_needs_nothing(hass: HomeAssistant) -> None:
    charger = await _charger(hass, switch="on", amps="16")
    await charger.remove()
    assert charger.turn_on == [] and charger.values == []


async def test_a_charger_never_stopped_by_spotnav_stays_off(hass: HomeAssistant) -> None:
    """Off when SpotNav was set up, and never stopped by it: not SpotNav's to turn on."""
    charger = await _charger(hass, switch="off")
    await charger.remove()
    assert charger.turn_on == []


async def test_an_older_record_counts_a_balancing_pause_as_spotnavs_own(hass: HomeAssistant, hass_storage) -> None:
    """A record from before the flag was kept says it through the facts it had: load balancing's pause."""
    hass_storage[f"{DOMAIN}.{ENTRY}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{ENTRY}",
        "data": {"plan": None, "balancing_pause": {"origin": "solar", "plan_charge": False}},
    }
    charger = await _charger(hass, switch="off")
    await charger.remove()
    assert [call.data["entity_id"] for call in charger.turn_on] == [SWITCH]


# ------------------------------------------------------------------------------------------------- the rest


async def test_a_reload_or_an_unload_hands_nothing_back(hass: HomeAssistant) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await _solar_stands_down(charger)
    charger.forget_calls()
    assert await hass.config_entries.async_reload(charger.entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(charger.entry.entry_id)
    await hass.async_block_till_done()
    assert charger.turn_on == [] and charger.values == []


async def test_a_charger_another_entry_still_drives_is_left_to_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)
    await _solar_stands_down(charger)
    monkeypatch.setattr(charger_handback, "duplicates_of", lambda _hass, _entry: ["another entry"])
    await charger.remove()
    assert charger.turn_on == [] and charger.values == []


async def test_a_hand_back_that_fails_never_holds_the_removal_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    charger = await _charger(hass, amps="10")
    await _spotnav_charges(charger, 14)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no adapter")

    monkeypatch.setattr(charger_handback, "build_adapter", broken)
    with caplog.at_level(logging.WARNING):
        await charger.remove()
    assert "its charger could not be handed back" in caplog.text
    assert charger.turn_on == [] and charger.values == []
