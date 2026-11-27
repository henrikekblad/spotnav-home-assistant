"""What an idle charger draws when its phases cannot be read (`execution/solar_execution.py: site_car_w`).

The field case, 2026-11-13: an OCPP charger (HALO) reports its L1/L2/L3 current as attributes of its current
sensor, and only while it charges. Idle after a restart (status Finishing) the sensor reads 0 A with no attributes,
so the site's chargers' draw was unknown and every hour since was kept without a house figure. What only measures
now counts such a charger as drawing nothing; the import target, which steers chargers, keeps what it had.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_MEASURED_CURRENT_SOURCE, CONF_MODE, MEASUREMENT_MODE_DERIVED, MODE_OCPP
from custom_components.spotnav.execution import solar_execution
from custom_components.spotnav.execution.solar_execution import IDLE_UNREAD_HOLD_S, site_car_w
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.site.demand_tracker import HourTracker
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict

from .helpers import make_entry, make_site_entry
from .world import controller_of, set_derived_site_entities

CURRENT = "sensor.halo_charger_connector_1_current_import"
STATUS = "sensor.halo_charger_connector_1_status_connector"
SWITCH = "switch.halo_charger_charge_control"
SITE_W = 600.0  # each phase


def _current(hass: HomeAssistant, amps: float, phases: dict[str, float] | None = None) -> None:
    attributes: dict[str, Any] = {"unit_of_measurement": "A"}
    if phases is not None:
        attributes.update(phases)
    hass.states.async_set(CURRENT, str(amps), attributes)


async def _world(hass: HomeAssistant, *, status: str, amps: float = 0.0, phases: dict[str, float] | None = None):
    """The owner's site: a derived site whose OCPP charger's phases are read as attributes of its current sensor."""
    hass.states.async_set(SWITCH, "off")
    hass.states.async_set(STATUS, status)
    _current(hass, amps, phases)
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    charger = make_entry(
        hass,
        entry_id="halo",
        charge_control=SWITCH,
        current_limit=None,
        webhook_id="webhook-halo",
        title="HALO Charger",
        ocpp_target=("halo_charger", 1),
        extra={CONF_MODE: MODE_OCPP},
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    site_entry = make_site_entry(
        hass,
        entry_id="my_home",
        main_fuse_a=20.0,
        charger_entry_ids=[charger.entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=set_derived_site_entities(hass, "my_home", SITE_W),
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="attributes",
                        entity_id=CURRENT,
                        attributes={"L1": "L1", "L2": "L2", "L3": "L3"},
                        attribute_unit_override="A",
                    )
                ),
            }
        },
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    return controller_of(hass, charger.entry_id), controller_of(hass, site_entry.entry_id)


def _observed_car_w(site, monkeypatch) -> float | None:
    """What the hour tracker keeps as the chargers' draw after one observation."""
    monkeypatch.setattr(site.demand, "due", lambda: True)
    site._observe_demand()
    running = site.demand.tracker.running_hour(dt_util.utcnow())
    assert running is not None
    return running.last_car_w


@pytest.mark.parametrize("status", ["Finishing", "Available", "Preparing", "SuspendedEV"])
async def test_an_idle_ocpp_charger_without_phase_attributes_draws_nothing(
    hass: HomeAssistant, monkeypatch, status: str
) -> None:
    charger, site = await _world(hass, status=status)
    assert charger.connection()[0] != "charging"

    assert site_car_w(hass, site, idle_unread_as_zero=True) == 0.0
    assert _observed_car_w(site, monkeypatch) == 0.0
    # The house figure is the grid's: 3 x 600 W.
    running = site.demand.tracker.running_hour(dt_util.utcnow())
    assert running.last_grid_w == pytest.approx(3 * SITE_W)


async def test_an_idle_charger_of_unknown_status_whose_current_reads_zero_draws_nothing(hass: HomeAssistant) -> None:
    charger, site = await _world(hass, status="unavailable")

    assert site_car_w(hass, site, idle_unread_as_zero=True) == 0.0
    # The current sensor itself unreadable too: only having just been seen idle says it is.
    hass.states.async_set(CURRENT, "unavailable")
    assert site_car_w(hass, site, idle_unread_as_zero=True) == 0.0
    # Never seen idle: nothing says it is.
    charger.idle_seen_at = None
    assert site_car_w(hass, site, idle_unread_as_zero=True) is None
    # Or reading a current: it may be drawing.
    _current(hass, 10.0)
    assert site_car_w(hass, site, idle_unread_as_zero=True) is None


async def test_only_ocpp_trusts_the_current_entity_state_of_a_charger_of_unknown_status(hass: HomeAssistant) -> None:
    """Another integration's state may be one phase or the neutral: a 0 A there proves nothing."""
    charger, site = await _world(hass, status="unavailable")
    charger.ocpp_target = None

    assert site_car_w(hass, site, idle_unread_as_zero=True) is None


async def test_a_charging_charger_without_phase_attributes_stays_unknown(hass: HomeAssistant, monkeypatch) -> None:
    _charger, site = await _world(hass, status="Charging", amps=16.0)

    assert site_car_w(hass, site, idle_unread_as_zero=True) is None
    assert _observed_car_w(site, monkeypatch) is None
    # Even a current sensor that still reads 0 A: the status says charging.
    _current(hass, 0.0)
    assert site_car_w(hass, site, idle_unread_as_zero=True) is None


async def test_a_charger_whose_phases_read_is_counted_as_before(hass: HomeAssistant) -> None:
    _charger, site = await _world(hass, status="Charging", amps=10.0, phases={"L1": 10.0, "L2": 10.0, "L3": 10.0})

    assert site_car_w(hass, site, idle_unread_as_zero=True) == pytest.approx(3 * 10.0 * 230.0)


async def test_the_import_target_keeps_an_unreadable_charger_unknown(hass: HomeAssistant) -> None:
    """The import target steers chargers: an idle charger it cannot read leaves its house figure unknown, as before."""
    _charger, site = await _world(hass, status="Finishing")

    assert site_car_w(hass, site) is None


async def test_the_fuse_protection_still_credits_nothing_for_an_unreadable_charger(hass: HomeAssistant) -> None:
    """Load balancing reads the charger's own phases, never this rule: an idle charger with no attributes still
    reads as unknown there."""
    charger, site = await _world(hass, status="Finishing")

    measured = site.charger_measured_current(charger.entry_id)
    assert measured is not None
    assert all(measured.get(phase).value is None for phase in ("L1", "L2", "L3"))


def _flapping_hour(hass: HomeAssistant, site, *, away: dict[str, str]) -> HourMean:
    """One hour of the tracker fed every 30 s with the site's grid and the chargers' draw, as `_observe_demand`
    reads them, while the charger drops off for 90 s in every 4 minutes (its entities `away` meanwhile): the field
    case of 2026-11-26, an OCPP charger with no car unavailable about 16 times an hour."""
    tracker = HourTracker(tz="Europe/Stockholm")
    start = datetime(2026, 11, 26, 23, 0, tzinfo=timezone.utc)
    present = {entity_id: hass.states.get(entity_id) for entity_id in away}
    for step in range(121):
        dropped = step % 8 >= 5
        for entity_id, state in present.items():
            if dropped:
                hass.states.async_set(entity_id, away[entity_id])
            else:
                hass.states.async_set(entity_id, state.state, state.attributes)
        car_w = site_car_w(hass, site, idle_unread_as_zero=True)
        tracker.observe(start + timedelta(seconds=30 * step), 1800.0, car_w, battery_w=0.0)
    assert len(tracker.hours) == 1
    return tracker.hours[0]


async def test_a_charger_with_no_car_that_flaps_unavailable_keeps_the_house(hass: HomeAssistant) -> None:
    """Last read idle (no car, 0 A), the charger still draws nothing while it is away: the hour keeps its house."""
    _charger, site = await _world(hass, status="Available")

    hour = _flapping_hour(hass, site, away={STATUS: "unavailable", CURRENT: "unavailable", SWITCH: "unavailable"})

    assert hour.house_kw == pytest.approx(1.8)
    assert hour.ex_battery_kw == pytest.approx(1.8)


async def test_a_charger_that_drops_off_while_charging_stays_a_gap(hass: HomeAssistant) -> None:
    """Seen charging when it dropped, it may be charging still: the hour keeps no house, as before."""
    _charger, site = await _world(hass, status="Charging", amps=10.0, phases={"L1": 10.0, "L2": 10.0, "L3": 10.0})

    hour = _flapping_hour(hass, site, away={STATUS: "unavailable", CURRENT: "unavailable", SWITCH: "unavailable"})

    assert hour.house_kw is None
    assert hour.ex_battery_kw is None


async def test_an_idle_charger_away_longer_than_the_hold_is_unknown(hass: HomeAssistant, monkeypatch) -> None:
    charger, site = await _world(hass, status="Available")
    assert site_car_w(hass, site, idle_unread_as_zero=True) == 0.0
    seen = charger.idle_seen_at
    assert seen is not None
    hass.states.async_set(STATUS, "unavailable")
    hass.states.async_set(CURRENT, "unavailable")
    assert site_car_w(hass, site, idle_unread_as_zero=True) == 0.0
    # Measured only: the import target, which steers chargers, keeps it unknown.
    assert site_car_w(hass, site) is None

    monkeypatch.setattr(solar_execution.time, "monotonic", lambda: seen + IDLE_UNREAD_HOLD_S + 1.0)
    assert site_car_w(hass, site, idle_unread_as_zero=True) is None
