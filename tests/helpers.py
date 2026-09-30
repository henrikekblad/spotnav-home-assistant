"""Shared config-entry helpers for the SpotNav charging control tests, plus
shared device/entity fixtures for tests that need a specific device shape.
"""


from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.spotnav.const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_BATTERY_PER_PHASE_SOURCE,
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CURRENT_LIMIT,
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_MAX_AGE_S,
    CONF_REGULATOR_DEADBAND_A,
    CONF_REGULATOR_DWELL_S,
    CONF_MEASUREMENT_MODE,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CONF_PHASE_WIRING,
    CONF_SAFETY_MARGIN_A,
    CONF_SITE_CURRENT_SOURCE,
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_CURRENT_CONTROL,
    CONF_SITE_ENABLED,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_SOLAR_PRIORITY,
    CONF_WEBHOOK_ID,
    CONF_YIELD_CEILING_A,
    CONF_YIELD_STEPPING_ENABLED,
    DEFAULT_MAX_AGE_S,
    DEFAULT_REGULATOR_DEADBAND_A,
    DEFAULT_REGULATOR_DWELL_S,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DIRECT,
    MODE_GENERIC,
)


def make_entry(
    hass: HomeAssistant,
    *,
    entry_id: str,
    charge_control: str,
    current_limit: str | None,
    webhook_id: str,
    title: str,
    current_control: str = "",
    ocpp_target: tuple[str, int] | None = None,
    unique_id: str | None = None,
    extra: dict | None = None,
) -> MockConfigEntry:
    # Explicit entry_id: MockConfigEntry otherwise derives one from ulid_now(),
    # which can collide when two entries are created under frozen test time.
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
        CONF_MODE: MODE_GENERIC,
        CONF_CHARGE_CONTROL: charge_control,
        CONF_CURRENT_LIMIT: current_limit or "",
        CONF_WEBHOOK_ID: webhook_id,
    }
    if current_control:
        # Written only when a caller actually asks for a control mode, so every
        # existing entry's data stays exactly what it was.
        data[CONF_CURRENT_CONTROL] = current_control
    if ocpp_target is not None:
        # The explicit OCPP connector target the config flow stores: charge point id and connector.
        data[CONF_OCPP_CHARGE_POINT_ID], data[CONF_OCPP_CONNECTOR_ID] = ocpp_target
    if extra:
        # Any other stored key (a detected charger's platform, control path, status sensor).
        data.update(extra)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        title=title,
        data=data,
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)
    return entry


async def setup_two_chargers(hass: HomeAssistant):
    """Set up two independent charger config entries.

    Returns the two entries plus the mocked ``switch.turn_on``/``switch.turn_off``
    call logs so tests can assert which charger a command actually reached.
    """
    hass.states.async_set("switch.charger_a", "off")
    hass.states.async_set("switch.charger_b", "off")

    # entry_b is intentionally added only after entry_a has finished setup: the
    # first config entry set up for a domain also bootstraps the component
    # itself, which in turn sets up every entry already registered for that
    # domain (see ConfigEntries.async_setup's "Setup Component if not set up
    # yet" branch). Adding both entries up front before either is set up would
    # make the first async_setup() call load both at once, which is realistic
    # only for entries that already existed at HA startup. Adding entry_b
    # afterwards exercises the more common case: a second charger added while
    # HA is already running.
    entry_a = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Charger A",
    )
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    entry_b = make_entry(
        hass,
        entry_id="entry_charger_b",
        charge_control="switch.charger_b",
        current_limit=None,
        webhook_id="webhook-b",
        title="Charger B",
    )
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    # The mocks come *after* both entries are set up, and that order is the point: this
    # integration forwards the `switch` (and `number`, `select`, `time`) platforms, so Home
    # Assistant loads those core integrations during setup -- and a core `EntityComponent`
    # registers its own `switch.turn_on` handler, which would replace a mock registered
    # before it. A mock is a stand-in for the charger's own integration, which on a real
    # installation is loaded first and does not change later.
    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")

    return entry_a, entry_b, turn_on_calls, turn_off_calls


def make_ocpp_config_entry(hass: HomeAssistant, *, entry_id: str) -> MockConfigEntry:
    """A stand-in for the real `ocpp` custom integration's own config entry.

    Only used so device_registry has a config_entry_id to attach charger
    devices to; it is registered with hass but never set up (there is no
    real `ocpp` component installed in tests).
    """
    entry = MockConfigEntry(domain="ocpp", entry_id=entry_id, title="OCPP")
    entry.add_to_hass(hass)
    return entry


def create_ocpp_charger_device(
    hass: HomeAssistant,
    *,
    ocpp_entry: MockConfigEntry,
    device_unique_id: str,
    switch_object_id: str,
    voltage_attributes: dict | None = None,
    current_attributes: dict | None = None,
) -> str:
    """Register one OCPP-style device with a charge-control switch and,
    optionally, voltage/current sensors carrying the given extra attributes
    (on top of a `device_class`), mimicking how the real `ocpp` integration
    links its entities to one charger device.

    Returns the switch entity_id to use as `charge_control`.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    device = device_registry.async_get_or_create(
        config_entry_id=ocpp_entry.entry_id,
        identifiers={("ocpp", device_unique_id)},
        name=device_unique_id,
    )

    switch_entry = entity_registry.async_get_or_create(
        "switch",
        "ocpp",
        f"{device_unique_id}_charge_control",
        device_id=device.id,
        config_entry=ocpp_entry,
        suggested_object_id=switch_object_id,
    )
    hass.states.async_set(switch_entry.entity_id, "off")

    if voltage_attributes is not None:
        voltage_entry = entity_registry.async_get_or_create(
            "sensor",
            "ocpp",
            f"{device_unique_id}_voltage",
            device_id=device.id,
            config_entry=ocpp_entry,
            suggested_object_id=f"{switch_object_id}_voltage",
        )
        hass.states.async_set(
            voltage_entry.entity_id,
            "230",
            {"device_class": "voltage", "unit_of_measurement": "V", **voltage_attributes},
        )

    if current_attributes is not None:
        current_entry = entity_registry.async_get_or_create(
            "sensor",
            "ocpp",
            f"{device_unique_id}_current",
            device_id=device.id,
            config_entry=ocpp_entry,
            suggested_object_id=f"{switch_object_id}_current",
        )
        hass.states.async_set(
            current_entry.entity_id,
            "0",
            {"device_class": "current", "unit_of_measurement": "A", **current_attributes},
        )

    return switch_entry.entity_id


def create_ocpp_switch_and_number(
    hass: HomeAssistant,
    *,
    ocpp_entry: MockConfigEntry,
    device_unique_id: str,
    switch_object_id: str,
    number_object_id: str | None = None,
) -> tuple[str, str | None]:
    """Register an OCPP-style device with a charge-control switch and,
    optionally, a current-limit number entity (unit A), mimicking the
    entities offered by the OCPP-guided config flow step.

    Returns (switch_entity_id, number_entity_id_or_None).
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    device = device_registry.async_get_or_create(
        config_entry_id=ocpp_entry.entry_id,
        identifiers={("ocpp", device_unique_id)},
        name=device_unique_id,
    )

    switch_entry = entity_registry.async_get_or_create(
        "switch",
        "ocpp",
        f"{device_unique_id}_charge_control",
        device_id=device.id,
        config_entry=ocpp_entry,
        suggested_object_id=switch_object_id,
    )
    hass.states.async_set(switch_entry.entity_id, "off")

    number_entity_id = None
    if number_object_id is not None:
        number_entry = entity_registry.async_get_or_create(
            "number",
            "ocpp",
            f"{device_unique_id}_current_limit",
            device_id=device.id,
            config_entry=ocpp_entry,
            suggested_object_id=number_object_id,
        )
        hass.states.async_set(
            number_entry.entity_id, "16", {"unit_of_measurement": "A", "min": 6, "max": 32}
        )
        number_entity_id = number_entry.entity_id

    return switch_entry.entity_id, number_entity_id


def make_site_entry(
    hass: HomeAssistant,
    *,
    entry_id: str,
    main_fuse_a: float = 25.0,
    safety_margin_a: float = 1.0,
    charger_entry_ids: list[str] | None = None,
    phase_wiring: dict | None = None,
    direct_entities: dict | None = None,
    site_current_source: dict | None = None,
    measurement_mode: str = MEASUREMENT_MODE_DIRECT,
    derived_entities: dict | None = None,
    site_enabled: bool = True,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    battery_aggregate_power_entity: str | None = None,
    battery_per_phase_source: dict | None = None,
    active_control_enabled: bool = False,
    yield_stepping_enabled: bool = False,
    yield_ceiling_a: float | None = None,
    solar_priority: str | None = None,
    solar_forecast_entries: list[str] | None = None,
    title: str = "Site",
    extra_data: dict | None = None,
) -> MockConfigEntry:
    """A site config entry, direct measurement mode by default with `sensor.<entry_id>_l1..l3`.

    `site_current_source` (a serialized `PhaseMeasurementSource`) takes precedence over
    `direct_entities`. `measurement_mode=MEASUREMENT_MODE_DERIVED` with `derived_entities` builds a
    derived-mode entry, for which the controller ignores both.
    """
    charger_entry_ids = charger_entry_ids or []
    data: dict = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_SITE,
        CONF_SITE_ENABLED: site_enabled,
        CONF_MAIN_FUSE_A: main_fuse_a,
        CONF_SAFETY_MARGIN_A: safety_margin_a,
        CONF_MEASUREMENT_MODE: measurement_mode,
        CONF_CHARGER_ENTRY_IDS: charger_entry_ids,
        CONF_PHASE_WIRING: phase_wiring or {},
        CONF_DIRECT_ENTITIES: direct_entities
        or {
            "L1": f"sensor.{entry_id}_l1",
            "L2": f"sensor.{entry_id}_l2",
            "L3": f"sensor.{entry_id}_l3",
        },
        CONF_DERIVED_ENTITIES: derived_entities or {},
        CONF_MAX_AGE_S: max_age_s,
        # The create path writes the damping numbers at their defaults; absent reads the same.
        CONF_REGULATOR_DEADBAND_A: DEFAULT_REGULATOR_DEADBAND_A,
        CONF_REGULATOR_DWELL_S: DEFAULT_REGULATOR_DWELL_S,
    }
    if site_current_source is not None:
        data[CONF_SITE_CURRENT_SOURCE] = site_current_source
    if battery_aggregate_power_entity is not None:
        data[CONF_BATTERY_AGGREGATE_POWER_ENTITY] = battery_aggregate_power_entity
    if battery_per_phase_source is not None:
        data[CONF_BATTERY_PER_PHASE_SOURCE] = battery_per_phase_source
    if active_control_enabled:
        # Only written when a test opts in; absent means off.
        data[CONF_ACTIVE_CONTROL_ENABLED] = True
    if yield_stepping_enabled:
        # Same opt-in convention: absent means off.
        data[CONF_YIELD_STEPPING_ENABLED] = True
    if yield_ceiling_a is not None:
        # Only stored when customised, matching the options flow's "only persist a customized
        # ceiling" rule; otherwise the 1.15x-fuse default applies (`default_yield_ceiling_a`).
        data[CONF_YIELD_CEILING_A] = yield_ceiling_a
    if solar_priority is not None:
        # Only written when named; otherwise the default (`car_first`) applies.
        data[CONF_SOLAR_PRIORITY] = solar_priority
    if solar_forecast_entries is not None:
        # Only written when named; otherwise no forecast sources are configured.
        data[CONF_SOLAR_FORECAST_ENTRIES] = solar_forecast_entries
    if extra_data:
        # Keys a test sets directly (sign options, optional sources): stored exactly as given.
        data.update(extra_data)
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id, title=title, data=data)
    entry.add_to_hass(hass)
    return entry


def set_current_sensor(hass: HomeAssistant, entity_id: str, amps: float | str) -> None:
    """Set a plain current sensor's state, matching what a real grid-current
    sensor would report (state + `unit_of_measurement` attribute)."""
    hass.states.async_set(entity_id, str(amps), {"unit_of_measurement": "A"})


def add_percent_battery_sensor(
    hass: HomeAssistant, device_id: str, *, object_id: str, percent: str
) -> str:
    """Register one percent-shaped `device_class: battery` sensor on an existing
    device and give it a live state. Returns its entity id.

    Registered exactly as an integration's own entity would be, because
    `vehicle_discovery` reasons about *devices*, not bare states.
    """
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "test",
        f"{device_id}_{object_id}",
        device_id=device_id,
        suggested_object_id=object_id,
    )
    hass.states.async_set(
        entry.entity_id, percent, {"device_class": "battery", "unit_of_measurement": "%"}
    )
    return entry.entity_id


def add_ambiguous_vehicle_device(
    hass: HomeAssistant,
    *,
    unique_id: str,
    name: str,
    soc_percent: str = "42",
    health_percent: str = "96",
    range_km: str = "310",
) -> tuple[str, str, str]:
    """A vehicle-shaped device with *two* percent-shaped battery sensors.

    The shape this exists for: an EV integration exposing two percent battery
    readings whose keys say nothing about which is the state of charge (neutral
    names on purpose: a `health` or `target` key would be ranked out) -- which
    automatic detection refuses to guess between. Returns `(device_id, soc_entity_id, health_entity_id)`.

    Idempotent per `unique_id`: calling it again after its entities have been
    removed recreates the *same* device and entity ids, which is how a test
    can stand in for an integration reload or a restored backup.
    """
    entry_id = "ambiguous_vehicle_devices"
    if hass.config_entries.async_get_entry(entry_id) is None:
        MockConfigEntry(domain="test", entry_id=entry_id).add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry_id,
        identifiers={("test", unique_id)},
        name=name,
    )
    soc_entity_id = add_percent_battery_sensor(
        hass, device.id, object_id="pack_a", percent=soc_percent
    )
    health_entity_id = add_percent_battery_sensor(
        hass, device.id, object_id="pack_b", percent=health_percent
    )

    range_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "test",
        f"{device.id}_range",
        device_id=device.id,
        suggested_object_id="range",
    )
    hass.states.async_set(
        range_entry.entity_id,
        range_km,
        {"device_class": "distance", "unit_of_measurement": "km"},
    )
    return device.id, soc_entity_id, health_entity_id


def future_window() -> tuple[str, str]:
    """A schedule window that is valid under ChargingController's own rules:

    in the future, but within the seven-day horizon it enforces.
    """
    start = dt_util.utcnow() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    return start.isoformat(), end.isoformat()


async def webhook_dashboard(client, webhook_id: str) -> dict:
    """The one webhook read: the dashboard payload for the charger this webhook is bound to."""
    response = await client.post(
        f"/api/webhook/{webhook_id}",
        json={"version": 1, "action": "dashboard", "api_version": 1},
    )
    body = await response.json()
    assert body["ok"] is True, body
    return body


async def install_schedule(controller, payload: dict) -> None:
    """Install a plan on a bare charging controller, as Auto would: `payload` names `start`/`end`
    (or `periods`), `amps` and optionally a target, and the plan is stamped with Auto's metadata.

    The one door into a controller is `async_install`; nothing builds a plan from a webhook payload
    any more, so a test that wants a plan builds the typed value itself.
    """
    from custom_components.spotnav.execution.controller import ChargingPlan

    periods = payload.get("periods") or [{"start": payload["start"], "end": payload["end"]}]
    plan = ChargingPlan(
        start=str(periods[0]["start"]),
        end=str(periods[-1]["end"]),
        amps=int(payload["amps"]),
        phases=int(payload.get("phases", 3)),
        power_kw=payload.get("power_kw"),
        energy_kwh=payload.get("energy_kwh"),
        price_area=payload.get("price_area"),
        periods=[{"start": str(item["start"]), "end": str(item["end"])} for item in periods],
        target_soc_percent=payload.get("target_soc_percent"),
        vehicle_id=payload.get("vehicle_id"),
        auto_identity="0123456789abcdef0123456789abcdef",
        auto_settings_revision=1,
        auto_price_identity="test-price-identity",
    )
    controller.validate_plan(plan)
    await controller.async_install(plan)
