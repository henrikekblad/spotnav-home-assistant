"""Load balancing with the site's measurement lost: once it has been stale, missing or unusable for
`MEASUREMENT_LOST_GRACE_S` on a phase a charger may draw on, every charger load balancing governs that draws above its
minimum current is set to that minimum (never paused or stopped), until the measurement is back.

The pure check (`regulator.measurement_lost`) on real site results, then the site controller with the real regulator,
explicit passes and hand-moved clocks (`test_lb_start_credit._halo`): a Charge Amps HALO on OCPP behind a 20 A fuse.
"""

from __future__ import annotations

from dataclasses import replace

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.site.regulator import MEASUREMENT_LOST_GRACE_S, measurement_lost
from custom_components.spotnav.site.site_capacity import (
    PHASES,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PhaseValue,
    SiteCalculationConfig,
    calculate_site_capacity,
)
from custom_components.spotnav.site.site_capacity_controller import LIMIT_CAUSE_MEASUREMENT_LOST

from .test_lb_start_credit import _halo, _site_a
from .world import set_derived_site_entities

MAX_AGE = 120.0
FRESH = 5.0


def _derived(
    *, age: float | None = FRESH, report_age: float | None = None, l2_power: float | None = 2300.0
) -> DerivedPhaseMeasurement:
    """A derived site importing 10 A on every phase; L2's active power and every phase's age as given."""

    def phase(power: float | None) -> DerivedPhaseInput:
        return DerivedPhaseInput(
            active_power_w=PhaseValue(power, age, report_age_s=report_age),
            reactive_power_var=PhaseValue(0.0, age, report_age_s=report_age),
            voltage_v=PhaseValue(230.0, age, report_age_s=report_age),
        )

    return DerivedPhaseMeasurement(l1=phase(2300.0), l2=phase(l2_power), l3=phase(2300.0))


def _result(derived: DerivedPhaseMeasurement):
    return calculate_site_capacity(
        SiteCalculationConfig(
            main_fuse_a=20.0,
            safety_margin_a=0.0,
            measurement_mode="derived_phase_current",  # type: ignore[arg-type]
            max_age_s=MAX_AGE,
            derived=derived,
        ),
        [],
    )


def _lost(result, phases=PHASES):
    return measurement_lost(site_result=result, phases=phases, max_age_s=MAX_AGE)


# -- the check


def test_a_fresh_measurement_is_not_lost() -> None:
    assert _lost(_result(_derived())) is None


def test_a_reading_older_than_the_maximum_age_is_lost_as_stale() -> None:
    lost = _lost(_result(_derived(age=MAX_AGE + 1.0)))
    assert lost is not None and lost.kind == "phase_stale" and lost.phase == "L1"


def test_a_reading_merely_unchanged_while_the_meter_still_reports_is_not_lost() -> None:
    assert _lost(_result(_derived(age=MAX_AGE + 300.0, report_age=FRESH))) is None


def test_a_missing_value_is_lost_as_missing() -> None:
    lost = _lost(_result(_derived(l2_power=None)))
    assert lost is not None and lost.kind == "phase_value_missing" and lost.phase == "L2"


def test_a_value_of_unknown_age_is_lost_as_age_unknown() -> None:
    result = _result(_derived())
    lost = _lost(replace(result, phase_age_s={**result.phase_age_s, "L3": None}))
    assert lost is not None and lost.kind == "phase_age_unknown" and lost.phase == "L3"


def test_an_unusable_signed_reading_where_the_direction_is_known_is_lost_as_unusable() -> None:
    result = _result(_derived())
    lost = _lost(replace(result, phase_signed_active_power_reason={**result.phase_signed_active_power_reason, "L2": "invalid"}))
    assert lost is not None and lost.kind == "phase_unusable" and lost.phase == "L2"


def test_only_the_phases_the_charger_may_draw_on_count() -> None:
    result = _result(_derived(l2_power=None))
    assert _lost(result, ("L1",)) is None
    assert _lost(result, ("L2",)) is not None


def test_a_direct_sites_unconfirmed_sign_is_the_direction_unknown_not_a_lost_measurement() -> None:
    reading = DirectPhaseMeasurement(*(PhaseValue(10.0, FRESH) for _ in PHASES))
    result = calculate_site_capacity(
        SiteCalculationConfig(
            main_fuse_a=20.0,
            safety_margin_a=0.0,
            measurement_mode="direct_phase_current",  # type: ignore[arg-type]
            max_age_s=MAX_AGE,
            direct=reading,
            direct_signed=reading,
            direct_sign_confirmed=frozenset(),
        ),
        [],
    )
    assert result.phase_signed_active_power_reason["L1"] == "missing"
    assert _lost(result) is None


# -- the site controller


async def _charging_at_16(hass: HomeAssistant, monkeypatch, entry_id: str):
    """The HALO charging at 16 A written by load balancing, on a fresh site with room."""
    halo = await _halo(hass, monkeypatch, entry_id, l1=1.0, l2=1.0, l3=1.0)
    await halo.start(16)
    halo.switch("on")
    halo.reads(16.0)
    _site_a(hass, halo.site, 17.0, 17.0, 17.0)
    await hass.async_block_till_done()
    await halo.tick()
    halo.configure.clear()
    return halo


def _amps(halo) -> list[int]:
    return [int(value.split(",")[0].split(".")[1]) for value in halo.written()]


def _meter_reads(hass: HomeAssistant, halo, amps: float) -> None:
    """Every one of the site meter's entities reports again, each phase at `amps`."""
    set_derived_site_entities(hass, halo.site, 0.0)
    _site_a(hass, halo.site, amps, amps, amps)


async def _lose_for(halo, seconds: float, step: float = 30.0) -> None:
    """Passes every `step` seconds for `seconds`, the site's readings untouched."""
    elapsed = 0.0
    while elapsed < seconds:
        halo.advance(step)
        await halo.tick()
        elapsed += step


async def test_a_stale_site_holds_the_charger_for_the_grace_then_steps_it_to_its_minimum(
    hass: HomeAssistant, monkeypatch, freezer: FrozenDateTimeFactory
) -> None:
    halo = await _charging_at_16(hass, monkeypatch, "lost_stale")
    cid = halo.charger.entry_id
    freezer.tick(MAX_AGE + 10.0)
    await halo.tick()  # the measurement is found lost
    assert halo.controller.result.state == "stale_measurements"
    await _lose_for(halo, MEASUREMENT_LOST_GRACE_S - 30.0)
    assert halo.written() == [], "nothing is written within the grace"
    assert halo.controller.load_balancing_limit(cid)[1] != LIMIT_CAUSE_MEASUREMENT_LOST

    await _lose_for(halo, 30.0)
    assert _amps(halo) == [6], "stepped to its minimum once the grace is over"
    assert halo.turn_off == [], "not paused or stopped"
    assert halo.controller.load_balancing_limit(cid) == (6, LIMIT_CAUSE_MEASUREMENT_LOST)
    log = halo.log()[-1]
    assert (log["outcome"], log["to_a"], log["detail"]) == ("wrote", 6, "measurement_lost")
    snapshot = halo.controller.measurement_lost_snapshot()
    assert snapshot["steps_to_minimum"] == 1 and snapshot["last_step_at"] is not None
    assert cid in snapshot["lost_for_s"]

    # Held there: not written again while the measurement stays lost.
    await _lose_for(halo, 120.0)
    assert _amps(halo) == [6]
    assert halo.controller.measurement_lost_snapshot()["steps_to_minimum"] == 1


async def test_a_missing_site_reading_steps_the_charger_to_its_minimum(hass: HomeAssistant, monkeypatch) -> None:
    halo = await _charging_at_16(hass, monkeypatch, "lost_missing")
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(f"sensor.{halo.site}_power_{phase}", "unavailable")
    await hass.async_block_till_done()
    await halo.tick()
    assert halo.controller.result.state == "missing_measurements"
    await _lose_for(halo, MEASUREMENT_LOST_GRACE_S)
    assert _amps(halo) == [6]
    assert halo.turn_off == []


async def test_a_charger_at_its_minimum_is_not_written(
    hass: HomeAssistant, monkeypatch, freezer: FrozenDateTimeFactory
) -> None:
    halo = await _halo(hass, monkeypatch, "lost_at_minimum", l1=1.0, l2=1.0, l3=1.0)
    await halo.start(6)
    halo.switch("on")
    halo.reads(6.0)
    _site_a(hass, halo.site, 7.0, 7.0, 7.0)
    await hass.async_block_till_done()
    await halo.tick()
    halo.configure.clear()
    freezer.tick(MAX_AGE + 10.0)
    await halo.tick()
    await _lose_for(halo, MEASUREMENT_LOST_GRACE_S + 60.0)
    assert halo.written() == []
    assert halo.turn_off == []


async def test_a_charger_load_balancing_does_not_govern_is_not_written(
    hass: HomeAssistant, monkeypatch, freezer: FrozenDateTimeFactory
) -> None:
    halo = await _charging_at_16(hass, monkeypatch, "lost_not_governed")
    # A charger whose current cannot be set: load balancing only measures it.
    monkeypatch.setattr(ChargingController, "is_commandable", property(lambda self: False))
    freezer.tick(MAX_AGE + 10.0)
    await halo.tick()
    await _lose_for(halo, MEASUREMENT_LOST_GRACE_S + 60.0)
    assert halo.written() == []
    assert halo.controller.measurement_lost_snapshot()["steps_to_minimum"] == 0


async def test_a_measurement_back_lets_the_regulator_raise_again_by_its_own_rules(
    hass: HomeAssistant, monkeypatch, freezer: FrozenDateTimeFactory
) -> None:
    halo = await _charging_at_16(hass, monkeypatch, "lost_back")
    cid = halo.charger.entry_id
    freezer.tick(MAX_AGE + 10.0)
    await halo.tick()
    await _lose_for(halo, MEASUREMENT_LOST_GRACE_S)
    assert _amps(halo) == [6]

    # The meter reads again, the car at its minimum.
    halo.reads(6.0)
    _meter_reads(hass, halo, 7.0)
    await hass.async_block_till_done()
    await halo.tick()
    assert halo.controller.measurement_lost_snapshot()["lost_for_s"] == {}
    assert halo.controller.load_balancing_limit(cid)[1] != LIMIT_CAUSE_MEASUREMENT_LOST
    assert _amps(halo) == [6], "no jump straight back to the old current"

    # The regulator's own damped raise, once its dwell has passed.
    for _ in range(3):
        halo.advance(30.0)
        freezer.tick(30.0)
        halo.reads(float(_amps(halo)[-1]))
        _meter_reads(hass, halo, 1.0 + _amps(halo)[-1])
        await hass.async_block_till_done()
        await halo.tick()
    assert _amps(halo)[-1] > 6, "raised again once the measurement is back"
    assert halo.turn_off == []
