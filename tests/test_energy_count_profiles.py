"""The right energy count for every charger: each profile names its lifetime register, its per-session and other
counters, and, where the integration has no lifetime register, the power sensor SpotNav counts the energy from.

Before, the generic rule took the first `energy`/`total_increasing` sensor on the device: a per-session counter, a
daily one, or one phase of three. A detected charger stored with such a register gets the right one at setup (or
none, and its power is counted), unless a person chose it.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed, MockConfigEntry

from custom_components.spotnav.const import (
    CONF_ENERGY_REGISTER_CHOSEN,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENERGY_REGISTER_IS_SESSION,
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
)
from custom_components.spotnav.execution import charger_profiles
from custom_components.spotnav.execution.power_energy import (
    CHARGER_POWER_MAX_AGE_S,
    integrated_energy_unique_id,
    power_max_age_s,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.planning.auto_controller import advance_register
from custom_components.spotnav.planning.auto_settings import EnergyBaseline
from custom_components.spotnav.planning.planner import power_kw

from .charger_helpers import detected_config
from .charger_shapes import E, ENERGY_WH, register_shape, SHAPES
from .messages import register, update_entity_config_message
from .world import admin, controller_of, ws_call

#: Per fixed profile: what the generic rule took (first energy/total_increasing sensor on the device, in the
#: order the integration registers them), and what detection takes now.
FIXED = {
    "goecharger": ("sensor.goecharger_current_session_charged_energy", "sensor.goecharger_energy_total", None),
    "wattpilot": ("sensor.wattpilot_wh", "sensor.wattpilot_eto", None),
    "hypervolt_charger": (
        "sensor.hypervolt_charger_session_energy_total_increasing",
        None,
        "sensor.hypervolt_charger_session_power",
    ),
    "alfen_modbus": (
        "sensor.alfen_modbus_real_energy_delivered_l1",
        "sensor.alfen_modbus_real_energy_delivered_sum",
        None,
    ),
    "myenergi": ("sensor.myenergi_energy_total", None, "sensor.myenergi_ct1_power"),
    "chargepoint": ("sensor.chargepoint_session_energy_kwh", None, "sensor.chargepoint_session_power_kw"),
    "ohme": ("sensor.ohme_energy", None, "sensor.ohme_power"),
    "heidelberg_energy_control": (
        "sensor.heidelberg_energy_control_session_energy",
        "sensor.heidelberg_energy_control_total_energy",
        None,
    ),
}


def _as_before(profile: charger_profiles.PlatformProfile) -> charger_profiles.PlatformProfile:
    """The profile as it was before it named its energy sensors."""
    return replace(
        profile,
        energy_keys=(),
        session_energy_keys=(),
        excluded_energy_keys=(),
        energy_power_keys=(),
        energy_state_classes=("total_increasing",),
    )


@pytest.mark.parametrize("platform", sorted(FIXED))
async def test_the_generic_rule_took_a_wrong_register_and_the_profile_now_takes_the_lifetime_one(
    hass: HomeAssistant, platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong, lifetime, power = FIXED[platform]
    ids = register_shape(hass, SHAPES[platform])

    found = detect_charger(hass, ids["device_id"])
    assert found.energy_register == lifetime
    assert found.energy_power_entity == power

    monkeypatch.setitem(charger_profiles.PROFILES, platform, _as_before(charger_profiles.PROFILES[platform]))
    assert detect_charger(hass, ids["device_id"]).energy_register == wrong


@pytest.mark.parametrize("state_class", ["total", "total_increasing"])
async def test_abbs_session_energy_is_never_its_lifetime_register_in_either_integration(
    hass: HomeAssistant, state_class: str
) -> None:
    """One ABB integration registers `energy_delivered` as `total`, the other as `total_increasing` (which the
    generic rule took); both reset it at every session."""
    shape = SHAPES["abb_terra_ac"]
    energy = E(
        "sensor", "ABB1_energy_delivered", "energy_delivered", "12500", {**ENERGY_WH, "state_class": state_class},
        translation_key=True,
    )
    entities = tuple(energy if entity.key == "energy_delivered" else entity for entity in shape.entities)
    ids = register_shape(hass, replace(shape, entities=entities))

    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register is None
    assert found.energy_power_entity == "sensor.abb_terra_ac_active_power"


async def test_a_lifetime_register_wins_over_a_power_sensor(hass: HomeAssistant) -> None:
    """Wattpilot has a power sensor too, but a lifetime register: the power is not counted."""
    ids = register_shape(hass, SHAPES["wattpilot"])

    found = detect_charger(hass, ids["device_id"])

    assert found.energy_register == "sensor.wattpilot_eto" and found.energy_power_entity is None


async def test_a_power_sensor_that_measures_no_power_is_not_counted(hass: HomeAssistant) -> None:
    shape = SHAPES["chargepoint"]
    entities = tuple(
        E("sensor", entity.unique_id, entity.key, "7.2", {"unit_of_measurement": "kWh", "device_class": "energy"})
        if entity.key == "session_power_kw"
        else entity
        for entity in shape.entities
    )
    ids = register_shape(hass, replace(shape, entities=entities))

    assert detect_charger(hass, ids["device_id"]).energy_power_entity is None


# ------------------------------------------------------------------ the stored register at setup


async def _detected(hass: HomeAssistant, platform: str, **extra: Any) -> MockConfigEntry:
    ids = register_shape(hass, SHAPES[platform])
    found = detect_charger(hass, ids["device_id"])
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
        "webhook_id": f"hook-{platform}",
        **detected_config(found),
        **extra,
    }
    entry = MockConfigEntry(domain=DOMAIN, entry_id=f"det_{platform}", title=platform, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_stored_register_of_one_phase_is_replaced_by_the_lifetime_one_at_setup(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.energy_register")

    entry = await _detected(
        hass, "alfen_modbus", **{CONF_ENERGY_REGISTER_ENTITY: "sensor.alfen_modbus_real_energy_delivered_l1"}
    )

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == "sensor.alfen_modbus_real_energy_delivered_sum"
    assert controller_of(hass, entry.entry_id).energy_register_entity_id == "sensor.alfen_modbus_real_energy_delivered_sum"
    assert "is not the charger's lifetime register; using sensor.alfen_modbus_real_energy_delivered_sum" in caplog.text


async def test_a_stored_session_register_of_a_charger_without_a_lifetime_one_gives_way_to_its_power(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.spotnav.energy_register")

    entry = await _detected(hass, "myenergi", **{CONF_ENERGY_REGISTER_ENTITY: "sensor.myenergi_energy_total"})

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == ""
    controller = controller_of(hass, entry.entry_id)
    integrated = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, integrated_energy_unique_id(entry.entry_id))
    assert integrated is not None
    assert controller.energy_count() == {
        "source": "charger_power",
        "register": integrated,
        "power": "sensor.myenergi_ct1_power",
    }
    assert "and the charger has none; it is no longer used" in caplog.text


async def test_a_register_chosen_in_the_entity_editor_is_kept_from_then_on(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await _detected(hass, "alfen_modbus")
    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == "sensor.alfen_modbus_real_energy_delivered_sum"
    client = await admin(hass, hass_ws_client)

    answer = await ws_call(
        client,
        update_entity_config_message(
            entry.entry_id,
            scope="charger",
            expected={"energy_register_entity": "sensor.alfen_modbus_real_energy_delivered_sum"},
            changes={"energy_register_entity": "sensor.alfen_modbus_real_energy_delivered_l1"},
        ),
    )
    assert answer["result"]["ok"] is True
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_ENERGY_REGISTER_CHOSEN] is True

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == "sensor.alfen_modbus_real_energy_delivered_l1"
    assert controller_of(hass, entry.entry_id).energy_register_entity_id == "sensor.alfen_modbus_real_energy_delivered_l1"


async def test_automatic_in_the_entity_editor_ends_the_choice(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await _detected(
        hass,
        "alfen_modbus",
        **{
            CONF_ENERGY_REGISTER_ENTITY: "sensor.alfen_modbus_real_energy_delivered_l1",
            CONF_ENERGY_REGISTER_CHOSEN: True,
        },
    )
    client = await admin(hass, hass_ws_client)

    answer = await ws_call(
        client,
        update_entity_config_message(
            entry.entry_id,
            scope="charger",
            expected={"energy_register_entity": "sensor.alfen_modbus_real_energy_delivered_l1"},
            changes={"energy_register_entity": ""},
        ),
    )
    assert answer["result"]["ok"] is True
    await hass.async_block_till_done()

    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_ENERGY_REGISTER_CHOSEN not in entry.data
    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == "sensor.alfen_modbus_real_energy_delivered_sum"


async def test_a_session_register_taken_knowingly_when_the_charger_was_added_is_kept(hass: HomeAssistant) -> None:
    entry = await _detected(
        hass,
        "chargepoint",
        **{
            CONF_ENERGY_REGISTER_ENTITY: "sensor.chargepoint_session_energy_kwh",
            CONF_ENERGY_REGISTER_IS_SESSION: True,
        },
    )

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == "sensor.chargepoint_session_energy_kwh"
    assert controller_of(hass, entry.entry_id).energy_count()["source"] == "register"


async def test_another_integrations_meter_is_never_replaced(hass: HomeAssistant) -> None:
    meter = register(hass, "sensor", "wallbox_meter", "Wallbox meter", device_class="energy")

    entry = await _detected(hass, "myenergi", **{CONF_ENERGY_REGISTER_ENTITY: meter})

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == meter
    assert controller_of(hass, entry.entry_id).energy_count() == {"source": "register", "register": meter, "power": None}


# ------------------------------------------------------------------ energy from the charger's own power


async def test_the_chargers_own_power_is_counted_but_never_read_as_a_smart_plug(hass: HomeAssistant) -> None:
    entry = await _detected(hass, "chargepoint")
    controller = controller_of(hass, entry.entry_id)

    assert entry.data[CONF_ENERGY_REGISTER_ENTITY] == ""
    assert controller.energy_count()["source"] == "charger_power"
    assert controller.power_entity_id is None
    assert controller.measured_power_w() is None
    assert controller.charge_progress_facts().power_mode is False


async def test_a_cloud_polled_power_in_kilowatts_is_counted_between_polls_and_not_over_a_gap(
    hass: HomeAssistant, freezer
) -> None:
    entry = await _detected(hass, "chargepoint")
    energy_id = controller_of(hass, entry.entry_id).energy_register_entity_id
    power = "sensor.chargepoint_session_power_kw"
    attributes = {"device_class": "power", "unit_of_measurement": "kW", "state_class": "measurement"}

    def energy() -> float:
        return float(hass.states.get(energy_id).state)

    async def poll(value: str) -> None:
        # The integration writes the state at every poll (three minutes by default), changed or not.
        hass.states.async_set(power, value, attributes, force_update=True)
        await hass.async_block_till_done()

    async def wait(seconds: int) -> None:
        for _ in range(seconds // 30):
            freezer.tick(30)
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

    await poll("7.2")
    start = energy()
    for _ in range(3):
        await wait(180)
        await poll("7.2")
    # Nine minutes at 7.2 kW, seen at the 30-second ticks between the polls.
    assert energy() - start == pytest.approx(7.2 * 9 / 60, abs=0.13)

    counted = energy()
    hass.states.async_set(power, "unavailable", attributes)
    await hass.async_block_till_done()
    await wait(600)
    await poll("7.2")
    # Nothing over the ten minutes the integration said it could not read the charger.
    assert energy() == pytest.approx(counted, abs=1e-6)


def test_a_chargers_own_power_stands_for_one_slow_poll_and_a_plugs_for_the_measurement_age() -> None:
    assert power_max_age_s(120.0, smart_plug=True) == 120.0
    assert power_max_age_s(120.0, smart_plug=False) == CHARGER_POWER_MAX_AGE_S
    assert CHARGER_POWER_MAX_AGE_S > timedelta(minutes=10).total_seconds()


# ------------------------------------------------------------------ a register that resets in the middle of a charge


def _count_until(need_kwh: float, poll_s: int, register_at: Any) -> tuple[float, float]:
    """Readings every `poll_s` of a charge at 11.085 kW, taken into Auto's count (`advance_register`) until the count
    says the need is delivered: `(true energy then, count then)`. `register_at(true_kwh)` is what the register shows."""
    start = datetime(2026, 10, 31, 23, 50, tzinfo=timezone.utc)
    first = register_at(0.0)
    baseline = EnergyBaseline(
        register_kwh=first, departure_key="k", last_register_kwh=first, last_register_at=start, charge_mark_s=0.0
    )
    true_kwh, moment = 0.0, start
    while true_kwh < need_kwh * 4:
        moment += timedelta(seconds=poll_s)
        true_kwh += 11.085 * poll_s / 3600
        step = advance_register(
            baseline,
            register_at(true_kwh),
            moment,
            max_kw=power_kw(63, 3),
            plugged_in_at=None,
            charged_s=(moment - start).total_seconds(),
            changed_at=moment,
        )
        baseline = step.baseline
        if step.accepted and step.delivered_kwh >= need_kwh:
            return true_kwh, step.delivered_kwh
    raise AssertionError("the count never reached the need")


@pytest.mark.parametrize("poll_s", [10, 60, 300])
def test_a_daily_or_session_counter_that_resets_mid_charge_stops_at_most_one_reading_late(poll_s: int) -> None:
    """What the count did before 2.2 with the zappi's daily counter (back to 0 at midnight) or a session counter
    that starts again: the drop is held for two readings over two minutes, then counted on from zero, so only
    what was delivered between the last reading before the reset and the reset itself is lost."""
    reading_kwh = 11.085 * poll_s / 3600

    def daily(true_kwh: float) -> float:
        return 40.0 + true_kwh if true_kwh < 10.0 else true_kwh - 10.0

    stopped, counted = _count_until(30.5, poll_s, daily)

    assert counted >= 30.5
    assert stopped - 30.5 <= 2 * reading_kwh + 1e-9


def test_one_phase_of_three_counts_a_third_and_runs_the_charge_far_past_its_need() -> None:
    """Alfen Modbus before 2.2: the register taken was L1's alone."""
    stopped, counted = _count_until(30.5, 10, lambda true_kwh: 500.0 + true_kwh / 3)

    assert stopped == pytest.approx(3 * 30.5, abs=0.1)
