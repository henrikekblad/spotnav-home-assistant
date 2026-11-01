"""A start's credit when the home battery changes in the same seconds (`_credited_measurements`, `_battery_fall_credit`).

The HALO world of `tests/test_lb_start_credit.py`, yield stepping off so a start is not sent at the car's minimum: the
house draws 1 A per phase, the car starts at 16 A and draws 10 A on each phase, and its own reading still shows 0 A.
What the battery stopped (or began) drawing since the start is the car's (or not), on fresh readings then and now; with
the battery's power alone, nothing says on which phases it fell, and no phase is credited a fall that was not there.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import (
    CONF_BATTERY_PER_PHASE_SOURCE,
    CONF_PHASE_WIRING,
    CONF_YIELD_STEPPING_ENABLED,
)

from .helpers import set_current_sensor
from .test_balancing_retry import _battery_entity, _battery_w
from .test_lb_start_credit import _halo, _Halo, _site_a
from .world import separate_entities_source

_V = 230.0
_CAR_A = 10.0


async def _started(
    hass: HomeAssistant,
    monkeypatch,
    entry_id: str,
    battery_a: tuple[float, float, float],
    *,
    battery_w: float | None = None,
    per_phase: bool = False,
    seen: bool = False,
) -> _Halo:
    """The site with the battery charging `battery_a` per phase, the car started (`seen`: begun by itself, seen on at
    the next pass) and its control not yet on."""
    watts = sum(battery_a) * _V if battery_w is None else battery_w
    hass.states.async_set(_battery_entity(entry_id), str(round(watts)), {"unit_of_measurement": "W"})
    site = [1.0 + amps for amps in battery_a]
    halo = await _halo(hass, monkeypatch, entry_id, l1=site[0], l2=site[1], l3=site[2], battery_entity=_battery_entity(entry_id))
    halo.controller.config[CONF_YIELD_STEPPING_ENABLED] = False
    if per_phase:
        halo.controller.config[CONF_BATTERY_PER_PHASE_SOURCE] = separate_entities_source(
            f"sensor.{entry_id}_bat_l1", f"sensor.{entry_id}_bat_l2", f"sensor.{entry_id}_bat_l3"
        )
        _battery_phases(halo, battery_a)
    await hass.async_block_till_done()
    await halo.tick()
    await halo.start()
    assert halo.turn_on
    if seen:
        # Not one of the starts this site sent a reservation for: seen on at a pass, against the readings before it.
        halo.controller._end_start_credit(halo.charger.entry_id)  # noqa: SLF001
        await halo.tick()
    return halo


def _battery_phases(halo: _Halo, amps: tuple[float, float, float]) -> None:
    entry_id = halo.controller.entry_id.removesuffix("_site")
    for phase, value in zip(("l1", "l2", "l3"), amps, strict=True):
        set_current_sensor(halo.hass, f"sensor.{entry_id}_bat_{phase}", abs(value))


async def _car_on(halo: _Halo, battery_a: tuple[float, float, float], *, battery_w: float | None = None) -> dict:
    """The car on the site at `_CAR_A` per phase, the battery now at `battery_a`; the credit of that pass."""
    halo.switch("on")
    watts = sum(battery_a) * _V if battery_w is None else battery_w
    _battery_w(halo, watts)
    if CONF_BATTERY_PER_PHASE_SOURCE in halo.controller.config:
        _battery_phases(halo, battery_a)
    _site_a(halo.hass, halo.site, *(1.0 + _CAR_A + amps for amps in battery_a))
    await halo.hass.async_block_till_done()
    await halo.tick()
    return halo.controller.start_credit_snapshot()[halo.charger.entry_id]["credited_a"]


def _close(credited: dict, phase: str, amps: float) -> bool:
    return credited.get(phase, 0.0) == pytest.approx(amps, abs=0.05)


async def test_a_single_phase_battery_that_stops_is_not_split_over_the_other_phases(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The battery charged 8 A on L1 alone and stops as the car comes on: the site rose 2/10/10 A. The car drew 10 A
    on each phase; an equal split of the battery's fall would have credited L2 and L3 with 12.7 A."""
    halo = await _started(hass, monkeypatch, "cbsingle", (8.0, 0.0, 0.0))
    credited = await _car_on(halo, (0.0, 0.0, 0.0))
    assert all(_close(credited, phase, _CAR_A) for phase in ("L1", "L2", "L3")), credited


async def test_a_battery_read_per_phase_credits_each_phase_its_own_fall(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _started(hass, monkeypatch, "cbphase", (6.0, 2.0, 0.0), per_phase=True)
    credited = await _car_on(halo, (0.0, 0.0, 0.0))
    assert all(_close(credited, phase, _CAR_A) for phase in ("L1", "L2", "L3")), credited


async def test_a_battery_that_begins_to_charge_as_the_car_starts_is_not_the_car(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The battery rises to 3 A per phase with the car: the site rose 13 A, of which the car's is 10."""
    halo = await _started(hass, monkeypatch, "cbrising", (0.0, 0.0, 0.0))
    credited = await _car_on(halo, (3.0, 3.0, 3.0))
    assert all(_close(credited, phase, _CAR_A) for phase in ("L1", "L2", "L3")), credited


@pytest.mark.parametrize("stale", ["at_the_start", "now"])
async def test_no_fresh_battery_reading_credits_the_rise_alone(hass: HomeAssistant, monkeypatch, stale: str) -> None:
    """A battery reading that is not usable at the start or now says nothing of its fall: the site's rise alone, as
    before (L1, where the battery's 8 A stopped, shows a rise of 2 A)."""
    halo = await _started(hass, monkeypatch, f"cbstale{stale.replace('_', '')}", (8.0, 0.0, 0.0))
    if stale == "at_the_start":
        credit = halo.controller._start_credits[halo.charger.entry_id]  # noqa: SLF001
        assert credit.battery_w is not None
        halo.controller._start_credits[halo.charger.entry_id] = type(credit)(  # noqa: SLF001
            credit.amps, credit.since, credit.baseline_a, credit.others_a, credit.seq, None, None
        )
        credited = await _car_on(halo, (0.0, 0.0, 0.0))
    else:
        credited = await _car_on(halo, (0.0, 0.0, 0.0))
        halo.hass.states.async_set(_battery_entity("cbstalenow"), "unavailable")
        await halo.hass.async_block_till_done()
        await halo.tick()
        credited = halo.controller.start_credit_snapshot()[halo.charger.entry_id]["credited_a"]
    assert _close(credited, "L1", 2.0) and _close(credited, "L2", _CAR_A) and _close(credited, "L3", _CAR_A), credited


async def test_a_start_seen_at_a_pass_takes_the_battery_from_before_that_pass(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _started(hass, monkeypatch, "cbseen", (8.0, 0.0, 0.0), seen=True)
    credited = await _car_on(halo, (0.0, 0.0, 0.0))
    assert halo.controller._start_credits[halo.charger.entry_id].battery_w == pytest.approx(1840.0)  # noqa: SLF001
    assert all(_close(credited, phase, _CAR_A) for phase in ("L1", "L2", "L3")), credited


async def test_a_single_phase_charger_is_credited_the_whole_fall_only_as_the_site_shows_it(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A single-phase charger on L1 and a battery on L2 alone that stops (6 A): L1 rose 10 A, L2 fell 6 A. The car drew
    10 A, not 10 + 2 by an equal split of the fall, nor 10 + 6 by the whole fall on its phase."""
    entry_id = "cbonephase"
    hass.states.async_set(_battery_entity(entry_id), str(round(6.0 * _V)), {"unit_of_measurement": "W"})
    halo = await _halo(hass, monkeypatch, entry_id, l1=1.0, l2=7.0, l3=1.0, battery_entity=_battery_entity(entry_id))
    controller = halo.controller
    controller.config[CONF_YIELD_STEPPING_ENABLED] = False
    wiring = dict(controller.config[CONF_PHASE_WIRING])
    wiring[halo.charger.entry_id] = {**wiring[halo.charger.entry_id], "phases": 1, "phase": "L1"}
    controller.config[CONF_PHASE_WIRING] = wiring
    await halo.tick()
    await halo.start()
    assert halo.turn_on
    halo.switch("on")
    _battery_w(halo, 0.0)
    _site_a(hass, halo.site, 1.0 + _CAR_A, 1.0, 1.0)
    await hass.async_block_till_done()
    await halo.tick()
    credited = controller.start_credit_snapshot()[halo.charger.entry_id]["credited_a"]
    assert _close(credited, "L1", _CAR_A) and set(credited) == {"L1"}, credited
