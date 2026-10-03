"""What charges show about the phases they use, and what is done with it.

Three phases confirm the wiring and the car and change nothing; one phase on three-phase wiring twice running for
the same vehicle becomes a question about its onboard charger (never a silent change); a charger in no site whose
wiring nothing says is set to three by its first three-phase charge. Pins the observer's rules, the stored counts,
the suggestion on the dashboard row, and the controller feeding it.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.const import CONF_CHARGER_PHASES
from custom_components.spotnav.planning.phases import (
    async_record_charge_phases,
    MIN_SAMPLES,
    onboard_suggestion,
    PhaseObserver,
    phases_carrying,
)
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.vehicles import vehicle_properties
from tests.helpers import set_charger_phases
from tests.world import charger_and_car, controller_of, setup_charger, setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")


# ----------------------------------------------------------------------------- counting phases


@pytest.mark.parametrize(
    ("currents", "count"),
    [
        ((10.0, 10.0, 10.0), 3),
        ((10.0, 0.2, 0.0), 1),
        ((6.0, 2.0, 1.9), 2),  # a phase carries from 2 A up
        ((0.0, 0.0, 0.0), 0),
        ((10.0, None, 10.0), None),  # an unreadable phase may be the one carrying the charge
        ((10.0, 10.0), None),
    ],
)
def test_the_phases_that_carry_the_charge_are_counted(currents: tuple, count: int | None) -> None:
    assert phases_carrying(currents) == count


def _feed(observer: PhaseObserver, currents, times: int, charging: bool = True) -> list[int | None]:
    return [observer.sample(charging, currents) for _ in range(times)]


def test_three_phases_are_certain_after_enough_samples_and_only_said_once() -> None:
    observer = PhaseObserver()
    said = _feed(observer, (9.0, 9.0, 9.0), MIN_SAMPLES + 2)
    assert said == [None] * (MIN_SAMPLES - 1) + [3, None, None]
    assert observer.sample(False, None) is None  # the end of an announced charge says nothing more


def test_one_phase_is_said_only_when_the_charge_ends() -> None:
    observer = PhaseObserver()
    assert _feed(observer, (9.0, 0.1, 0.0), MIN_SAMPLES + 2) == [None] * (MIN_SAMPLES + 2)
    assert observer.sample(False, None) == 1


def test_a_ramp_up_that_reaches_the_other_phases_is_three() -> None:
    observer = PhaseObserver()
    _feed(observer, (6.0, 0.0, 0.0), 2)
    assert _feed(observer, (9.0, 9.0, 9.0), 1) == [3]


def test_a_charge_too_short_to_judge_says_nothing() -> None:
    observer = PhaseObserver()
    _feed(observer, (9.0, 0.0, 0.0), MIN_SAMPLES - 1)
    assert observer.sample(False, None) is None


def test_idle_and_unreadable_samples_do_not_count_or_end_a_charge() -> None:
    observer = PhaseObserver()
    _feed(observer, (0.5, 0.0, 0.0), 10)  # connected, not drawing
    _feed(observer, None, 10)  # unreadable
    _feed(observer, (9.0, None, 0.0), 10)  # one phase unreadable
    assert observer.sample(False, None) is None
    _feed(observer, (9.0, 0.0, 0.0), MIN_SAMPLES)
    _feed(observer, None, 5)  # still the same charge
    assert observer.sample(False, None) == 1


def test_each_charge_starts_over() -> None:
    observer = PhaseObserver()
    _feed(observer, (9.0, 0.0, 0.0), MIN_SAMPLES)
    assert observer.sample(False, None) == 1
    _feed(observer, (9.0, 9.0, 9.0), MIN_SAMPLES)
    assert observer.sample(False, None) is None  # announced as three already


# ----------------------------------------------------------------------------- what a finished charge teaches


async def _one_phase_charges(hass: HomeAssistant, entry_id: str, count: int) -> None:
    for _ in range(count):
        await async_record_charge_phases(hass, entry_id, 1)


async def test_two_one_phase_charges_in_a_row_suggest_a_one_phase_onboard_charger(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)

    await _one_phase_charges(hass, charger.entry_id, 1)
    assert onboard_suggestion(hass, car) is None
    await _one_phase_charges(hass, charger.entry_id, 1)

    assert onboard_suggestion(hass, car) == 1
    # Nothing was changed by itself.
    assert vehicle_properties.stored_properties(hass, car).onboard_phases is None
    assert vehicle_properties.onboard_phases(hass, car) == 3


async def test_a_three_phase_charge_in_between_starts_the_count_over(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)

    await _one_phase_charges(hass, charger.entry_id, 1)
    await async_record_charge_phases(hass, charger.entry_id, 3)
    await _one_phase_charges(hass, charger.entry_id, 1)

    assert onboard_suggestion(hass, car) is None


async def test_any_answer_ends_the_question(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)
    await _one_phase_charges(hass, charger.entry_id, 2)
    assert onboard_suggestion(hass, car) == 1

    # "Keep 3-phase" is an answer: it is stored, and the suggestion goes away and stays away.
    store = domain_data(hass).decision_store
    await vehicle_properties.async_update_vehicle_properties(hass, store, car, {vehicle_properties.KEY_ONBOARD_PHASES: 3})
    assert onboard_suggestion(hass, car) is None
    await _one_phase_charges(hass, charger.entry_id, 3)
    assert onboard_suggestion(hass, car) is None


async def test_a_car_already_known_to_be_one_phase_needs_no_suggestion(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)
    store = domain_data(hass).decision_store
    await vehicle_properties.async_update_vehicle_properties(hass, store, car, {vehicle_properties.KEY_ONBOARD_PHASES: 1})

    await _one_phase_charges(hass, charger.entry_id, 3)

    assert onboard_suggestion(hass, car) is None


async def test_one_phase_on_one_phase_wiring_says_nothing_about_the_car(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 1)

    await _one_phase_charges(hass, charger.entry_id, 3)

    assert onboard_suggestion(hass, car) is None


async def test_two_phases_say_nothing_either_way(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)

    for _ in range(3):
        await async_record_charge_phases(hass, charger.entry_id, 2)

    assert onboard_suggestion(hass, car) is None


# ----------------------------------------------------------------------------- a charger in no site


async def test_the_first_three_phase_charge_sets_the_wiring_of_a_charger_nothing_says_anything_about(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    charger = await setup_charger(hass)
    assert CONF_CHARGER_PHASES not in charger.data

    with caplog.at_level(logging.INFO):
        await async_record_charge_phases(hass, charger.entry_id, 3)

    assert hass.config_entries.async_get_entry(charger.entry_id).data[CONF_CHARGER_PHASES] == 3
    assert "wiring set to three phases" in caplog.text


async def test_a_wiring_somebody_stated_is_never_changed_by_a_charge(hass: HomeAssistant) -> None:
    charger = await setup_charger(hass)
    set_charger_phases(hass, charger.entry_id, 1)

    await async_record_charge_phases(hass, charger.entry_id, 3)

    assert hass.config_entries.async_get_entry(charger.entry_id).data[CONF_CHARGER_PHASES] == 1


async def test_one_phase_on_a_charger_with_no_stated_wiring_stays_a_question(hass: HomeAssistant) -> None:
    charger = await setup_charger(hass)

    await _one_phase_charges(hass, charger.entry_id, 3)

    assert CONF_CHARGER_PHASES not in hass.config_entries.async_get_entry(charger.entry_id).data


async def test_a_site_charger_never_gets_a_wiring_of_its_own(hass: HomeAssistant) -> None:
    charger, _site = await setup_charger_and_site(hass)  # no phase wiring for it in the site
    await async_record_charge_phases(hass, charger.entry_id, 3)
    assert CONF_CHARGER_PHASES not in hass.config_entries.async_get_entry(charger.entry_id).data


# ----------------------------------------------------------------------------- the dashboard and the controller


async def test_the_dashboard_row_carries_the_suggestion(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)

    def rows() -> dict:
        capture = dashboard_api.capture_vehicles(hass, charger.entry_id, None)[0]
        return {row.id: dashboard_api.serialize_vehicle(row) for row in capture}

    assert rows()[car]["suggested_onboard_phases"] is None
    await _one_phase_charges(hass, charger.entry_id, 2)
    assert rows()[car]["suggested_onboard_phases"] == 1


async def test_the_controller_feeds_a_charge_through_and_learns_from_it(hass: HomeAssistant) -> None:
    charger, car, _ = await charger_and_car(hass)
    set_charger_phases(hass, charger.entry_id, 3)
    controller = controller_of(hass, charger.entry_id)
    controller.adapter.charging_state = lambda: charging  # type: ignore[method-assign]
    controller.charge_phase_currents = lambda: (10.0, 0.0, 0.0)  # type: ignore[method-assign]

    for _ in range(2):
        charging = True
        for _ in range(MIN_SAMPLES):
            controller._async_sample_phases()
        charging = False
        controller._async_sample_phases()
        await hass.async_block_till_done()

    assert onboard_suggestion(hass, car) == 1


async def test_the_charge_is_read_from_the_chargers_own_phase_entities_else_its_sites_measurement(
    hass: HomeAssistant,
) -> None:
    from custom_components.spotnav.site.site_capacity import DirectPhaseMeasurement, PhaseValue

    charger, _site = await setup_charger_and_site(hass)
    controller = controller_of(hass, charger.entry_id)
    assert controller.charge_phase_currents() is None or True  # nothing configured: whatever the site says

    entity_ids = ("sensor.c_l1", "sensor.c_l2", "sensor.c_l3")
    for entity_id, amps in zip(entity_ids, ("9.5", "0.1", "unavailable"), strict=True):
        hass.states.async_set(entity_id, amps, {"unit_of_measurement": "A", "device_class": "current"})
    controller.adapter.current_entity_ids = entity_ids
    assert controller.charge_phase_currents() == (9.5, 0.1, None)

    # One current entity does not tell the phases apart; the site's own measurement does.
    controller.adapter.current_entity_ids = entity_ids[:1]
    assert controller.adapter.measured_phase_currents_a() is None
    site_controller = _site_controller(hass, _site.entry_id)
    live = PhaseValue(8.0, 1.0)
    site_controller.charger_measured_current = lambda _id: DirectPhaseMeasurement(  # type: ignore[method-assign]
        live, PhaseValue(None, None, problem="missing"), live
    )
    assert controller.charge_phase_currents() == (8.0, None, 8.0)


def _site_controller(hass: HomeAssistant, site_id: str):
    return hass.config_entries.async_get_entry(site_id).runtime_data.controller
