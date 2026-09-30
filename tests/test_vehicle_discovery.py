"""Automatic, read-only vehicle detection, from the device registry, entity registry and live state, plus dismissal and confirmation services."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav import async_setup
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
    async_setup_decisions,
)
from custom_components.spotnav.vehicles.vehicle_discovery import (
    discover_ambiguous_vehicles,
    discover_vehicles,
    vehicle_charge_limit_entity_id,
)

from .helpers import (
    add_ambiguous_vehicle_device,
    add_percent_battery_sensor,
    make_entry,
)
from custom_components.spotnav.runtime import domain_data

# The response's api_version, pinned deliberately: any change to the webhook contract must move this literal.


def _test_config_entry(hass: HomeAssistant) -> str:
    """One throwaway config entry so integration-owned devices can be registered."""
    entry_id = "test_vehicle_devices"
    if hass.config_entries.async_get_entry(entry_id) is None:
        MockConfigEntry(domain="test", entry_id=entry_id).add_to_hass(hass)
    return entry_id


def _device(hass: HomeAssistant, *, unique_id: str, name: str | None = None) -> str:
    """One plain HA device, as an integration (or the user) would create it."""
    entry = dr.async_get(hass).async_get_or_create(
        config_entry_id=_test_config_entry(hass),
        identifiers={("test", unique_id)},
        name=name or unique_id,
    )
    return entry.id


def _entity(
    hass: HomeAssistant,
    *,
    device_id: str,
    domain: str,
    object_id: str,
    state: str,
    attributes: dict | None = None,
) -> str:
    """One entity on `device_id`, registered and given a live state."""
    entry = er.async_get(hass).async_get_or_create(
        domain,
        "test",
        f"{device_id}_{object_id}",
        device_id=device_id,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entry.entity_id, state, attributes or {})
    return entry.entity_id


def _battery_percent(
    hass: HomeAssistant,
    device_id: str,
    *,
    object_id: str = "battery_level",
    percent: str = "50",
) -> str:
    return _entity(
        hass,
        device_id=device_id,
        domain="sensor",
        object_id=object_id,
        state=percent,
        attributes={"device_class": "battery", "unit_of_measurement": "%"},
    )


def _range_sensor(
    hass: HomeAssistant, device_id: str, *, object_id: str = "range", km: str = "240"
) -> str:
    """The companion signal detection actually keys off: remaining range."""
    return _entity(
        hass,
        device_id=device_id,
        domain="sensor",
        object_id=object_id,
        state=km,
        attributes={"device_class": "distance", "unit_of_measurement": "km"},
    )


def _charge_limit(
    hass: HomeAssistant,
    device_id: str,
    *,
    object_id: str,
    percent: str,
    minimum: int = 50,
    maximum: int = 100,
) -> str:
    return _entity(
        hass,
        device_id=device_id,
        domain="number",
        object_id=object_id,
        state=percent,
        attributes={"unit_of_measurement": "%", "min": minimum, "max": maximum},
    )


async def test_a_plain_battery_gadget_is_never_a_vehicle(hass: HomeAssistant) -> None:
    """A battery level alone says nothing: a door sensor's `%` battery, its low-battery binary sensor and a percent-shaped `number` add up to no vehicle."""
    device_id = _device(hass, unique_id="door_sensor", name="Back door")
    _battery_percent(hass, device_id, percent="87")
    _entity(
        hass,
        device_id=device_id,
        domain="binary_sensor",
        object_id="low_battery",
        state="off",
        attributes={"device_class": "battery"},
    )
    _entity(
        hass,
        device_id=device_id,
        domain="binary_sensor",
        object_id="opening",
        state="off",
        attributes={"device_class": "opening"},
    )
    _charge_limit(hass, device_id, object_id="target_humidity", percent="60", minimum=0)

    assert discover_vehicles(hass) == []


async def test_a_charging_binary_sensor_and_a_capacity_are_not_enough_either(
    hass: HomeAssistant,
) -> None:
    """A stationary home battery is not a vehicle: range distinguishes them, so a charging-state sensor is not an accepted companion signal."""
    device_id = _device(hass, unique_id="house_battery", name="House battery")
    _battery_percent(hass, device_id, percent="64")
    _entity(
        hass,
        device_id=device_id,
        domain="binary_sensor",
        object_id="battery_charging",
        state="on",
        attributes={"device_class": "battery_charging"},
    )
    _entity(
        hass,
        device_id=device_id,
        domain="sensor",
        object_id="stored_energy",
        state="12.5",
        attributes={"device_class": "energy_storage", "unit_of_measurement": "kWh"},
    )

    assert discover_vehicles(hass) == []


async def test_a_vehicle_with_a_range_sensor_is_detected(hass: HomeAssistant) -> None:
    """The battery-percent sensor plus a remaining-range sensor is a vehicle."""
    device_id = _device(hass, unique_id="car_one", name="Family car")
    _battery_percent(hass, device_id, percent="42.5")
    _range_sensor(hass, device_id, km="213")

    vehicles = discover_vehicles(hass)

    assert len(vehicles) == 1
    vehicle = vehicles[0]
    assert vehicle.id == device_id
    assert vehicle.name == "Family car"
    assert vehicle.soc_percent == 42.5
    assert vehicle.target_soc_percent_max is None
    assert vehicle.battery_capacity_kwh is None
    assert vehicle.capacity_source == "unknown"


async def test_a_charge_limit_is_reported_when_present_and_null_when_absent(
    hass: HomeAssistant,
) -> None:
    """`target_soc_percent_max` is the live value of the one writable limit.

    One limit-shaped `number` is unambiguous. Several are a choice only a human can
    make, so `None` ("no limit known") is reported until they do, rather than a
    highest value the write might not reach.
    """
    one_limit = _device(hass, unique_id="car_with_limit", name="Limited car")
    _battery_percent(hass, one_limit, percent="55")
    _range_sensor(hass, one_limit, km="300")
    _charge_limit(hass, one_limit, object_id="limit_a", percent="80")

    two_limits = _device(hass, unique_id="car_with_two_limits", name="Two-limit car")
    _battery_percent(hass, two_limits, percent="55")
    _range_sensor(hass, two_limits, km="300")
    _charge_limit(hass, two_limits, object_id="limit_a", percent="80")
    _charge_limit(hass, two_limits, object_id="limit_b", percent="100")

    without_limit = _device(hass, unique_id="car_without_limit", name="Open car")
    _battery_percent(hass, without_limit, percent="31")
    _range_sensor(hass, without_limit, km="150")

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert by_id[one_limit].target_soc_percent_max == 80.0
    assert by_id[two_limits].target_soc_percent_max is None
    assert by_id[without_limit].target_soc_percent_max is None


async def test_a_confirmation_resolves_the_ceiling_to_the_entity_a_human_chose(
    hass: HomeAssistant,
) -> None:
    """The human's choice decides the ceiling as well as the write: with a confirmation for the lower of two limits, the ceiling is that entity's value."""
    device_id = _device(hass, unique_id="car_two_limits", name="Two-limit car")
    _battery_percent(hass, device_id, percent="55")
    _range_sensor(hass, device_id, km="300")
    lower = _charge_limit(hass, device_id, object_id="limit_a", percent="80")
    _charge_limit(hass, device_id, object_id="limit_b", percent="100")
    store = await async_setup_decisions(hass)
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
        device_id,
        {"charge_limit_entity_id": lower},
    )

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert by_id[device_id].target_soc_percent_max == 80.0
    assert vehicle_charge_limit_entity_id(hass, device_id) == lower


async def test_a_stale_confirmation_is_ignored_and_the_ordinary_rule_decides_again(
    hass: HomeAssistant,
) -> None:
    """A confirmation naming a deleted entity counts as no confirmation (e.g. after an integration renames entities).

    The ordinary rule applies, here with one candidate left. The stored decision is
    left alone, so it applies again if the entity returns.
    """
    device_id = _device(hass, unique_id="car_two_limits", name="Two-limit car")
    _battery_percent(hass, device_id, percent="55")
    _range_sensor(hass, device_id, km="300")
    kept = _charge_limit(hass, device_id, object_id="limit_a", percent="80")
    deleted = _charge_limit(hass, device_id, object_id="limit_b", percent="100")
    store = await async_setup_decisions(hass)
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
        device_id,
        {"charge_limit_entity_id": deleted},
    )
    er.async_get(hass).async_remove(deleted)
    hass.states.async_remove(deleted)

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert by_id[device_id].target_soc_percent_max == 80.0
    assert vehicle_charge_limit_entity_id(hass, device_id) == kept
    # Still recorded: nothing rewrites or forgets a decision.
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, device_id) == {
        "charge_limit_entity_id": deleted
    }


async def test_a_dismissed_vehicle_resolves_no_limit_at_all(hass: HomeAssistant) -> None:
    """A dismissal wins over everything: even a single-limit device reports none and gets no write."""
    device_id = _device(hass, unique_id="car_one_limit", name="One-limit car")
    _battery_percent(hass, device_id, percent="55")
    _range_sensor(hass, device_id, km="300")
    _charge_limit(hass, device_id, object_id="limit_a", percent="80")
    store = await async_setup_decisions(hass)
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, device_id)

    assert discover_vehicles(hass) == []
    assert vehicle_charge_limit_entity_id(hass, device_id) is None


async def test_a_zero_charge_limit_is_not_a_charge_limit(hass: HomeAssistant) -> None:
    """A limit of 0 means "this car has not said" (asleep, or not polled since restart), so the ceiling is `None`, not 0.

    A car with one live limit and one asleep still reports the live one.
    """
    asleep = _device(hass, unique_id="car_asleep", name="Sleeping car")
    _battery_percent(hass, asleep, percent="41")
    _range_sensor(hass, asleep, km="220")
    _charge_limit(hass, asleep, object_id="limit_a", percent="0")
    _charge_limit(hass, asleep, object_id="limit_b", percent="0")

    half_awake = _device(hass, unique_id="car_half", name="Half-awake car")
    _battery_percent(hass, half_awake, percent="41")
    _range_sensor(hass, half_awake, km="220")
    _charge_limit(hass, half_awake, object_id="limit_a", percent="90")
    _charge_limit(hass, half_awake, object_id="limit_b", percent="0")

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert by_id[asleep].target_soc_percent_max is None
    assert by_id[half_awake].target_soc_percent_max == 90.0


async def test_capacity_is_detected_when_a_capacity_shaped_entity_exists(
    hass: HomeAssistant,
) -> None:
    """A capacity-shaped reading is `detected`; otherwise capacity stays `unknown`, never guessed."""
    detected = _device(hass, unique_id="car_capacity", name="Known car")
    _battery_percent(hass, detected, percent="70")
    _range_sensor(hass, detected, km="400")
    _entity(
        hass,
        device_id=detected,
        domain="sensor",
        object_id="battery_energy",
        state="64.0",
        attributes={"device_class": "energy_storage", "unit_of_measurement": "kWh"},
    )

    in_wh = _device(hass, unique_id="car_capacity_wh", name="Wh car")
    _battery_percent(hass, in_wh, percent="70")
    _range_sensor(hass, in_wh, km="400")
    _charge_limit(hass, in_wh, object_id="configured_capacity", percent="77", minimum=0)
    _entity(
        hass,
        device_id=in_wh,
        domain="number",
        object_id="capacity_wh",
        state="58000",
        attributes={"unit_of_measurement": "Wh", "min": 0, "max": 100000},
    )

    unknown = _device(hass, unique_id="car_capacity_unknown", name="Unknown car")
    _battery_percent(hass, unknown, percent="70")
    _range_sensor(hass, unknown, km="400")
    # A cumulative lifetime energy meter, far outside any plausible battery size, must not be taken as capacity.
    _entity(
        hass,
        device_id=unknown,
        domain="sensor",
        object_id="lifetime_energy",
        state="3500",
        attributes={"device_class": "energy", "unit_of_measurement": "kWh"},
    )

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert by_id[detected].battery_capacity_kwh == 64.0
    assert by_id[detected].capacity_source == "detected"
    assert by_id[in_wh].battery_capacity_kwh == 58.0
    assert by_id[in_wh].capacity_source == "detected"
    assert by_id[unknown].battery_capacity_kwh is None
    assert by_id[unknown].capacity_source == "unknown"
async def test_a_lifetime_energy_counter_is_never_read_as_a_battery_capacity(
    hass: HomeAssistant,
) -> None:
    """A lifetime meter (`device_class: energy`, `Wh`, `state_class: total`, 141456) falls inside `_CAPACITY_KWH_WINDOW` as 141.456 kWh but is not the pack size.

    A meter accumulates while a pack size is static, and `state_class` says which is
    which, so no brand or name matching is needed.
    """
    device_id = _device(hass, unique_id="car_lifetime_meter", name="Counter car")
    _battery_percent(hass, device_id, percent="52")
    _range_sensor(hass, device_id, km="260")
    _entity(
        hass,
        device_id=device_id,
        domain="sensor",
        object_id="total_energy_regeneration",
        state="141456",
        attributes={
            "device_class": "energy",
            "unit_of_measurement": "Wh",
            "state_class": "total",
        },
    )

    vehicle = {candidate.id: candidate for candidate in discover_vehicles(hass)}[device_id]

    assert vehicle.soc_percent == 52.0
    assert vehicle.battery_capacity_kwh is None
    assert vehicle.capacity_source == "unknown"


async def test_a_total_increasing_meter_is_excluded_inside_the_window_too(
    hass: HomeAssistant,
) -> None:
    """The other cumulative `state_class` spelling, at 50 kWh inside the window: only the `state_class` rule can reject it."""
    device_id = _device(hass, unique_id="car_total_increasing", name="Consumption car")
    _battery_percent(hass, device_id, percent="48")
    _range_sensor(hass, device_id, km="180")
    _entity(
        hass,
        device_id=device_id,
        domain="sensor",
        object_id="energy_consumption",
        state="50000",
        attributes={
            "device_class": "energy",
            "unit_of_measurement": "Wh",
            "state_class": "total_increasing",
        },
    )

    vehicle = {candidate.id: candidate for candidate in discover_vehicles(hass)}[device_id]

    assert vehicle.battery_capacity_kwh is None
    assert vehicle.capacity_source == "unknown"

async def test_a_genuine_capacity_is_still_detected_with_or_without_state_class(
    hass: HomeAssistant,
) -> None:
    """The `state_class` guard must not over-reach: static capacity readings (`energy_storage` sensor, energy-unit `number`) survive with `measurement` or no `state_class`."""
    cases = (
        # (unique_id, domain, object_id, state, attributes, expected kWh)
        (
            "car_storage_measured",
            "sensor",
            "battery_energy",
            "64.0",
            {
                "device_class": "energy_storage",
                "unit_of_measurement": "kWh",
                "state_class": "measurement",
            },
            64.0,
        ),
        (
            "car_storage_no_state_class",
            "sensor",
            "battery_energy",
            "58.0",
            {"device_class": "energy_storage", "unit_of_measurement": "kWh"},
            58.0,
        ),
        (
            "car_number_measured",
            "number",
            "capacity",
            "62000",
            {
                "unit_of_measurement": "Wh",
                "min": 0,
                "max": 100000,
                "state_class": "measurement",
            },
            62.0,
        ),
        (
            "car_number_no_state_class",
            "number",
            "capacity",
            "75000",
            {"unit_of_measurement": "Wh", "min": 0, "max": 100000},
            75.0,
        ),
    )
    expected: dict[str, float] = {}
    for unique_id, domain, object_id, state, attributes, kwh in cases:
        device_id = _device(hass, unique_id=unique_id, name=unique_id)
        _battery_percent(hass, device_id, percent="70")
        _range_sensor(hass, device_id, km="400")
        _entity(
            hass,
            device_id=device_id,
            domain=domain,
            object_id=object_id,
            state=state,
            attributes=attributes,
        )
        expected[device_id] = kwh

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    for device_id, kwh in expected.items():
        assert by_id[device_id].battery_capacity_kwh == kwh
        assert by_id[device_id].capacity_source == "detected"


async def test_a_real_capacity_beats_a_cumulative_meter_on_the_same_device(
    hass: HomeAssistant,
) -> None:
    """Same tier and window: only the `state_class` rule tells the meter from the capacity, in either entity-id order and across tiers."""
    device_ids: dict[str, str] = {}
    for suffix, capacity_attributes, capacity_object_id, meter_object_id in (
        (
            # Same tier: an energy-class capacity reading, both id orders.
            "capacity_id_first",
            {"device_class": "energy", "unit_of_measurement": "kWh"},
            "a_capacity",
            "z_meter",
        ),
        (
            "meter_id_first",
            {"device_class": "energy", "unit_of_measurement": "kWh"},
            "a_meter",
            "z_capacity",
        ),
        (
            # Cross-tier: an energy_storage capacity, whatever the meter is.
            "storage_capacity",
            {"device_class": "energy_storage", "unit_of_measurement": "kWh"},
            "capacity",
            "a_meter",
        ),
    ):
        device_id = _device(hass, unique_id=f"car_mixed_{suffix}", name=f"Mixed {suffix}")
        _battery_percent(hass, device_id, percent="60")
        _range_sensor(hass, device_id, km="300")
        _entity(
            hass,
            device_id=device_id,
            domain="sensor",
            object_id=capacity_object_id,
            state="77",
            attributes=capacity_attributes,
        )
        _entity(
            hass,
            device_id=device_id,
            domain="sensor",
            object_id=meter_object_id,
            state="141456",
            attributes={
                "device_class": "energy",
                "unit_of_measurement": "Wh",
                "state_class": "total",
            },
        )
        device_ids[suffix] = device_id

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    for suffix, device_id in device_ids.items():
        assert by_id[device_id].battery_capacity_kwh == 77.0, suffix
        assert by_id[device_id].capacity_source == "detected", suffix


async def test_a_vehicle_with_an_unreadable_soc_is_left_out_entirely(
    hass: HomeAssistant,
) -> None:
    """An unreadable state of charge (`unavailable`, `unknown`, non-numeric, out of range) leaves the device out rather than guessing."""
    for index, percent in enumerate(("unavailable", "unknown", "not-a-number", "140")):
        device_id = _device(hass, unique_id=f"unreadable_{index}", name=f"Unreadable {index}")
        _battery_percent(hass, device_id, percent=percent)
        _range_sensor(hass, device_id, km="200")

    assert discover_vehicles(hass) == []


async def test_two_plausible_battery_sensors_are_never_guessed_between(
    hass: HomeAssistant,
) -> None:
    """Two battery-percent sensors on one device (traction and 12 V, say) are ambiguous, so the device is left out."""
    device_id = _device(hass, unique_id="two_batteries", name="Two batteries")
    _battery_percent(hass, device_id, object_id="pack_one", percent="66")
    _battery_percent(hass, device_id, object_id="pack_two", percent="98")
    _range_sensor(hass, device_id, km="250")

    assert discover_vehicles(hass) == []


async def test_a_dismissed_vehicle_is_excluded_and_returns_when_undismissed(
    hass: HomeAssistant,
) -> None:
    """A dismissal hides a detected vehicle and undismissing brings it back; the device itself is untouched."""
    device_id = _device(hass, unique_id="car_dismissed", name="Dismissed car")
    _battery_percent(hass, device_id, percent="44")
    _range_sensor(hass, device_id, km="220")

    assert [vehicle.id for vehicle in discover_vehicles(hass)] == [device_id]

    store = await async_setup_decisions(hass)
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, device_id)

    assert discover_vehicles(hass) == []

    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, device_id)

    assert [vehicle.id for vehicle in discover_vehicles(hass)] == [device_id]


async def test_dismissing_one_vehicle_never_hides_another(hass: HomeAssistant) -> None:
    """Dismissals are per device id, never per scan."""
    dismissed = _device(hass, unique_id="car_dismiss_me", name="Dismiss me")
    _battery_percent(hass, dismissed, percent="30")
    _range_sensor(hass, dismissed, km="120")

    kept = _device(hass, unique_id="car_keep_me", name="Keep me")
    _battery_percent(hass, kept, percent="77")
    _range_sensor(hass, kept, km="380")

    store = await async_setup_decisions(hass)
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, dismissed)

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert dismissed not in by_id
    assert by_id[kept].name == "Keep me"
    assert by_id[kept].soc_percent == 77.0


async def test_the_dismiss_services_change_what_is_reported(
    hass: HomeAssistant
) -> None:
    """The two services are registered and take effect, end to end through a real charger entry and webhook `status` call."""
    device_id = _device(hass, unique_id="car_service", name="Service car")
    _battery_percent(hass, device_id, percent="58")
    _range_sensor(hass, device_id, km="260")

    hass.states.async_set("switch.dismiss_service_charger", "off")
    entry = make_entry(
        hass,
        entry_id="entry_dismiss_service",
        charge_control="switch.dismiss_service_charger",
        current_limit=None,
        webhook_id="webhook-dismiss-service",
        title="Dismiss service charger",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


    async def reported_vehicle_ids() -> list[str]:
        return [vehicle.id for vehicle in discover_vehicles(hass)]

    assert await reported_vehicle_ids() == [device_id]

    await hass.services.async_call(
        "spotnav", "dismiss_vehicle", {"device_id": device_id}, blocking=True
    )

    assert await reported_vehicle_ids() == []

    await hass.services.async_call(
        "spotnav", "undismiss_vehicle", {"device_id": device_id}, blocking=True
    )

    assert await reported_vehicle_ids() == [device_id]


async def test_the_dismiss_services_accept_an_unknown_device_id(
    hass: HomeAssistant
) -> None:
    """Dismissing ahead of a device appearing (or with no vehicle signals) is recorded without raising and applies once it shows up."""
    hass.states.async_set("switch.unknown_id_charger", "off")
    entry = make_entry(
        hass,
        entry_id="entry_unknown_id",
        charge_control="switch.unknown_id_charger",
        current_limit=None,
        webhook_id="webhook-unknown-id",
        title="Unknown id charger",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "spotnav", "dismiss_vehicle", {"device_id": "never-seen-device"}, blocking=True
    )
    await hass.services.async_call(
        "spotnav", "undismiss_vehicle", {"device_id": "never-seen-device"}, blocking=True
    )
    # A real device with nothing vehicle-like on it is equally harmless.
    plain_device_id = _device(hass, unique_id="not_a_vehicle", name="Not a vehicle")
    await hass.services.async_call(
        "spotnav", "dismiss_vehicle", {"device_id": plain_device_id}, blocking=True
    )

    store = domain_data(hass).decision_store
    assert store is not None
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "never-seen-device") is False
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, plain_device_id) is True


async def test_the_unconfirm_service_reverses_a_resolution(
    hass: HomeAssistant
) -> None:
    """`unconfirm_vehicle` undoes a wrong pick by hand, end to end through the webhook `status` call."""
    device_id, soc_entity_id, _health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_unconfirm_service", name="Unconfirm service car"
    )
    store = await async_setup_decisions(hass)
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc_entity_id})

    hass.states.async_set("switch.unconfirm_service_charger", "off")
    entry = make_entry(
        hass,
        entry_id="entry_unconfirm_service",
        charge_control="switch.unconfirm_service_charger",
        current_limit=None,
        webhook_id="webhook-unconfirm-service",
        title="Unconfirm service charger",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


    assert [vehicle.id for vehicle in discover_vehicles(hass)] == [device_id]
    assert discover_ambiguous_vehicles(hass) == []

    await hass.services.async_call(
        "spotnav", "unconfirm_vehicle", {"device_id": device_id}, blocking=True
    )

    assert discover_vehicles(hass) == []
    assert [candidate.id for candidate in discover_ambiguous_vehicles(hass)] == [device_id]
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) is None

    # Calling it again, or with an id that resolves to nothing, is not an error.
    await hass.services.async_call(
        "spotnav", "unconfirm_vehicle", {"device_id": device_id}, blocking=True
    )
    await hass.services.async_call(
        "spotnav", "unconfirm_vehicle", {"device_id": "never-seen-device"}, blocking=True
    )

    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) is None


async def test_the_unconfirm_service_leaves_a_dismissal_alone(hass: HomeAssistant) -> None:
    """Unconfirming leaves a dismissal of the same device as it was."""
    device_id, soc_entity_id, _health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_unconfirm_dismissed", name="Dismissed and unconfirmed car"
    )
    assert await async_setup(hass, {})
    store = domain_data(hass).decision_store
    assert store is not None
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc_entity_id})
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, device_id)

    await hass.services.async_call(
        "spotnav", "unconfirm_vehicle", {"device_id": device_id}, blocking=True
    )

    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) is None
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, device_id) is True

    # ...and the other way around, through the dismissal service.
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc_entity_id})
    await hass.services.async_call(
        "spotnav", "undismiss_vehicle", {"device_id": device_id}, blocking=True
    )

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, device_id) is False
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) == {
        "soc_entity_id": soc_entity_id
    }


def _ambiguous_vehicle(hass: HomeAssistant, *, unique_id: str, name: str) -> tuple[str, str, str]:
    """A vehicle with two percent-shaped battery sensors (state of charge and battery health): `(device, soc, health)`."""
    return add_ambiguous_vehicle_device(hass, unique_id=unique_id, name=name)


async def test_an_ambiguous_device_is_reported_as_ambiguous_not_as_a_vehicle(
    hass: HomeAssistant,
) -> None:
    """Two plausible battery sensors: excluded from `vehicles` and surfaced as ambiguous instead."""
    device_id, soc, health = _ambiguous_vehicle(hass, unique_id="car_ambiguous", name="Ambiguous car")

    assert discover_vehicles(hass) == []

    ambiguous = discover_ambiguous_vehicles(hass)

    assert len(ambiguous) == 1
    assert ambiguous[0].id == device_id
    assert ambiguous[0].name == "Ambiguous car"
    assert ambiguous[0].candidate_entity_ids == sorted([soc, health])


async def test_confirming_one_sensor_resolves_the_device(hass: HomeAssistant) -> None:
    """A human's choice replaces the ambiguity: the confirmed entity's value is reported and the device stops being ambiguous."""
    device_id, soc, health = _ambiguous_vehicle(hass, unique_id="car_resolved", name="Resolved car")

    store = await async_setup_decisions(hass)
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc})

    by_id = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert by_id[device_id].soc_percent == 42.0
    assert by_id[device_id].name == "Resolved car"
    assert discover_ambiguous_vehicles(hass) == []

    # Confirming the other one instead reads the other one's value.
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": health})

    assert {v.id: v for v in discover_vehicles(hass)}[device_id].soc_percent == 96.0


async def test_a_confirmed_sensor_that_becomes_unreadable_excludes_the_device(
    hass: HomeAssistant,
) -> None:
    """Never a silent fallback to guessing among the other candidates."""
    device_id, soc, health = _ambiguous_vehicle(hass, unique_id="car_asleep", name="Sleeping car")

    store = await async_setup_decisions(hass)
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc})

    assert [v.id for v in discover_vehicles(hass)] == [device_id]

    for state in ("unavailable", "unknown", "not-a-number", "140"):
        hass.states.async_set(soc, state, {"device_class": "battery", "unit_of_measurement": "%"})

        # Not reported, and not reported with the other sensor's 96 either.
        assert discover_vehicles(hass) == []
        # Still confirmed, so not ambiguous either: an unreadable state is temporary (a sleeping car).
        assert discover_ambiguous_vehicles(hass) == []

    hass.states.async_set(soc, "55", {"device_class": "battery", "unit_of_measurement": "%"})
    assert [v.id for v in discover_vehicles(hass)] == [device_id]

    # A confirmation naming a nonexistent entity is equally unusable and never guessed around.
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": "sensor.gone_forever"}
    )
    assert discover_vehicles(hass) == []


async def test_a_dismissed_ambiguous_device_is_in_neither_list(hass: HomeAssistant) -> None:
    """Dismissal wins over everything, confirmation included."""
    device_id, soc, _health = _ambiguous_vehicle(hass, unique_id="car_not_mine", name="Not my car")

    store = await async_setup_decisions(hass)
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc})
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, device_id)

    assert discover_vehicles(hass) == []
    assert discover_ambiguous_vehicles(hass) == []

    # ...and undismissing brings it back with the confirmation intact.
    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, device_id)

    assert [v.id for v in discover_vehicles(hass)] == [device_id]


def _remove_entity(hass: HomeAssistant, entity_id: str) -> None:
    """An entity gone from both the entity registry and the state machine, as after an integration renames its entities."""
    er.async_get(hass).async_remove(entity_id)
    hass.states.async_remove(entity_id)


async def test_a_confirmation_whose_entity_is_gone_makes_the_device_ambiguous_again(
    hass: HomeAssistant,
) -> None:
    """A stale confirmation is not obeyed: once the confirmed entity is deleted, the device returns to being an ambiguity rather than vanishing."""
    device_id, soc_entity_id, health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_renamed", name="Renamed car"
    )
    store = await async_setup_decisions(hass)
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc_entity_id})

    assert [vehicle.id for vehicle in discover_vehicles(hass)] == [device_id]

    # The integration is updated and its entity ids change: the confirmed id no longer exists and two different battery sensors are reported.
    _remove_entity(hass, soc_entity_id)
    replacement_entity_id = add_percent_battery_sensor(
        hass, device_id, object_id="pack_c", percent="48"
    )

    assert discover_vehicles(hass) == []

    ambiguous = discover_ambiguous_vehicles(hass)

    assert [candidate.id for candidate in ambiguous] == [device_id]
    assert ambiguous[0].candidate_entity_ids == sorted([health_entity_id, replacement_entity_id])


async def test_a_registered_but_unreadable_confirmed_entity_never_becomes_ambiguous_again(
    hass: HomeAssistant,
) -> None:
    """A sleeping car is not a stale confirmation: the chosen entity still exists, so the decision stands and nothing re-asks."""
    device_id, soc_entity_id, _health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_asleep_confirmed", name="Sleeping confirmed car"
    )
    store = await async_setup_decisions(hass)
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc_entity_id})

    for state in ("unavailable", "unknown", "not-a-number", "140"):
        hass.states.async_set(
            soc_entity_id, state, {"device_class": "battery", "unit_of_measurement": "%"}
        )

        assert er.async_get(hass).async_get(soc_entity_id) is not None
        assert discover_vehicles(hass) == []
        assert discover_ambiguous_vehicles(hass) == []

    hass.states.async_set(
        soc_entity_id, "61", {"device_class": "battery", "unit_of_measurement": "%"}
    )

    assert [vehicle.id for vehicle in discover_vehicles(hass)] == [device_id]


async def test_a_stale_confirmation_is_kept_and_applies_again_when_the_entity_returns(
    hass: HomeAssistant,
) -> None:
    """A stale confirmation is ignored, never deleted: if the entity returns under the same id, the decision applies again."""
    device_id, soc_entity_id, health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_returning", name="Returning car"
    )
    store = await async_setup_decisions(hass)
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": soc_entity_id})

    # The entity is gone and a differently-named one took its place, so the device is an ambiguity again...
    _remove_entity(hass, soc_entity_id)
    replacement_entity_id = add_percent_battery_sensor(
        hass, device_id, object_id="pack_c", percent="48"
    )

    assert [candidate.id for candidate in discover_ambiguous_vehicles(hass)] == [device_id]
    # ...while the human's decision is untouched; only its usability changed.
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) == {
        "soc_entity_id": soc_entity_id
    }
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, device_id) is False

    # Then the update is rolled back: an entity with the confirmed id exists and reads normally again.
    _remove_entity(hass, replacement_entity_id)

    assert add_ambiguous_vehicle_device(hass, unique_id="car_returning", name="Returning car") == (
        device_id,
        soc_entity_id,
        health_entity_id,
    )

    # Reported from the chosen reading with nothing to redo, and not ambiguous.
    vehicles = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    assert vehicles[device_id].soc_percent == 42.0
    assert discover_ambiguous_vehicles(hass) == []


async def test_a_stale_confirmation_on_a_single_sensor_device_detects_normally(
    hass: HomeAssistant,
) -> None:
    """Stale is not excluded: with the confirmed entity gone and one battery sensor left, the ordinary path reads that sensor."""
    device_id, _soc_entity_id, health_entity_id = add_ambiguous_vehicle_device(
        hass, unique_id="car_single_now", name="Single sensor car"
    )
    store = await async_setup_decisions(hass)
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE, device_id, {"soc_entity_id": health_entity_id}
    )

    _remove_entity(hass, health_entity_id)

    vehicles = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}

    # 42, from the sensor that still exists: not the stale 96 and not excluded.
    assert vehicles[device_id].soc_percent == 42.0
    assert discover_ambiguous_vehicles(hass) == []
