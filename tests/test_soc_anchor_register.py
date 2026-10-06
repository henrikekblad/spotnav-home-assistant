"""A reading anchored while the energy register could not be read still carries the charge forward.

The field case: Home Assistant restarts, the car's state is set again (its `last_updated` is the restart,
so it reads as a fresh reading) before the charger's integration has its register. The anchor taken then
has no register and, before this, never produced an estimate: 5.5 kWh were delivered and the planner
saw the same 89 % again. Now the first readable register value becomes the anchor's baseline (energy
delivered before it is not credited, never invented), and a value the restart only re-announced does not
replace an anchor that has a register.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.target_stop import SocReading
from custom_components.spotnav.vehicles.soc_estimate import (
    CHARGE_EFFICIENCY,
    resolve_soc,
    SocAnchor,
    SocReader,
)

CAPACITY = 77.0
RESTART = datetime(2026, 10, 6, 10, 29, 36, tzinfo=timezone.utc)


def _reading(value: float | None, age_s: float) -> SocReading:
    return SocReading(
        soc_percent=value, source="vehicle", entity_id="sensor.ev6_battery", vehicle_id="car", age_s=age_s
    )


def _resolve(reading: SocReading | None, anchor: SocAnchor | None, register: float | None, now: datetime):
    return resolve_soc(
        reading=reading, anchor=anchor, register_kwh=register, capacity_kwh=CAPACITY, now=now, vehicle_id="car"
    )


def _expected(soc: float, kwh: float) -> float:
    return soc + kwh * CHARGE_EFFICIENCY / CAPACITY * 100.0


def test_an_anchor_without_a_register_takes_the_first_readable_value_as_its_baseline() -> None:
    # The restart: the car's 89 % is set again at once (fresh), the OCPP register has no state yet.
    first = _resolve(_reading(89.0, 2.0), None, None, RESTART + timedelta(seconds=2))
    assert first.anchor is not None and first.anchor.register_kwh is None
    # The register reads a minute later: it becomes the baseline, the reading stands as it is.
    now = RESTART + timedelta(minutes=1)
    second = _resolve(_reading(89.0, 62.0), first.anchor, 6800.5, now)
    assert second.anchor is not None and second.anchor.register_kwh == 6800.5
    assert second.anchor.read_at == first.anchor.read_at and second.anchor.soc_percent == 89.0
    assert second.reading is not None and second.reading.soc_percent == 89.0 and not second.reading.estimated
    # The plan's window delivers 5.5 kWh at about 11 kW; the cloud says nothing new.
    now = RESTART + timedelta(minutes=46)
    third = _resolve(_reading(89.0, 46 * 60.0), second.anchor, 6806.0, now)
    assert third.reading is not None and third.reading.estimated
    assert third.reading.soc_percent == pytest.approx(_expected(89.0, 5.5))


def test_a_baseline_taken_late_never_credits_what_came_before_it() -> None:
    anchor = SocAnchor(89.0, None, RESTART, "vehicle", "car")
    # Delivered meanwhile but unseen: only what the register counts from its first value is added.
    now = RESTART + timedelta(minutes=20)
    baselined = _resolve(_reading(89.0, 1200.0), anchor, 6803.0, now)
    assert baselined.anchor is not None and baselined.anchor.register_kwh == 6803.0
    assert baselined.reading is not None and baselined.reading.soc_percent == 89.0
    later = _resolve(_reading(89.0, 1800.0), baselined.anchor, 6806.0, RESTART + timedelta(minutes=30))
    assert later.reading is not None and later.reading.soc_percent == pytest.approx(_expected(89.0, 3.0))


def test_a_register_that_goes_backwards_after_the_baseline_gives_no_estimate() -> None:
    anchor = SocAnchor(89.0, None, RESTART, "vehicle", "car")
    baselined = _resolve(_reading(89.0, 120.0), anchor, 6800.0, RESTART + timedelta(minutes=2))
    stale = _reading(89.0, 3600.0)
    reset = _resolve(stale, baselined.anchor, 2.0, RESTART + timedelta(hours=1))
    assert reset.reading == stale, "a meter reset is never negative delivery"


def test_a_value_the_restart_only_reannounced_keeps_the_anchor_that_has_a_register() -> None:
    # Before the restart: 89 % read at 09:00 with the register at 6800.
    kept = SocAnchor(89.0, 6800.0, RESTART - timedelta(hours=1, minutes=30), "vehicle", "car")
    # The restart sets the same 89 % again (fresh by its timestamp) while the register is unknown.
    restored = _resolve(_reading(89.0, 1.0), kept, None, RESTART + timedelta(seconds=1))
    assert restored.anchor == kept
    # The register reads again; 5.5 kWh were delivered since the real reading.
    now = RESTART + timedelta(minutes=46)
    after = _resolve(_reading(89.0, 46 * 60.0), restored.anchor, 6805.5, now)
    assert after.reading is not None and after.reading.estimated
    assert after.reading.soc_percent == pytest.approx(_expected(89.0, 5.5))


def test_a_new_value_after_a_restart_is_still_a_new_reading() -> None:
    kept = SocAnchor(80.0, 6800.0, RESTART - timedelta(hours=2), "vehicle", "car")
    moved = _resolve(_reading(89.0, 1.0), kept, None, RESTART + timedelta(seconds=1))
    assert moved.anchor is not None and moved.anchor.soc_percent == 89.0
    assert moved.reading is not None and moved.reading.soc_percent == 89.0


# ------------------------------------------------------------------- the reader, through Home Assistant


REGISTER = "sensor.halo_energy_active_import_register"
CAR = "sensor.ev6_battery"


def _car(hass: HomeAssistant, value: str) -> None:
    hass.states.async_set(CAR, value, {"device_class": "battery", "unit_of_measurement": "%"})


def _register(hass: HomeAssistant, value: str) -> None:
    hass.states.async_set(
        REGISTER, value,
        {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"},
    )


async def test_the_field_case_through_the_reader(hass: HomeAssistant) -> None:
    """Restart with the register unavailable at first, then readable, then a delivery: the estimate rises."""
    from custom_components.spotnav.execution.target_stop import resolve_soc_reading

    with freeze_time(RESTART) as frozen:
        reader = SocReader(
            hass,
            "entry-halo",
            raw_reader=lambda vehicle_id: resolve_soc_reading(hass, vehicle_id=vehicle_id, entity_id=CAR),
            register_entity_id=lambda: REGISTER,
            charge_control=lambda: None,
            remembered_capacity=lambda _vehicle: CAPACITY,
        )
        await reader.async_load()
        _register(hass, "unavailable")
        _car(hass, "89")
        first = reader.read("car")
        assert first is not None and first.soc_percent == 89.0 and not first.estimated

        frozen.tick(timedelta(minutes=1))
        _register(hass, "6800.5")
        assert reader.read("car").soc_percent == 89.0  # type: ignore[union-attr]

        # Later: the window delivered 5.5 kWh, the car's sensor never changed.
        frozen.tick(timedelta(minutes=45))
        _register(hass, "6806.0")
        after = reader.read("car")
        assert after is not None and after.estimated
        assert after.soc_percent == pytest.approx(_expected(89.0, 5.5), abs=0.01)
        reader.async_shutdown()
