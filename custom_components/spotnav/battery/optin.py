"""The brand's entities that ship disabled, and enabling them on the person's confirm.

Some integrations ship entities SpotNav writes or reads disabled in the entity registry (Sigenergy's `sigen`:
`disabled_by=integration`; `solax_modbus`'s battery size). When the person turns "Styr batteriet" on, the card lists
the disabled ones and asks; only on that confirm does `async_enable` clear `disabled_by` for exactly those entities and
reload the integration's config entry once (Home Assistant would do it itself 30 s later; the person should not wait
for that). The verification then runs as usual. Turning control off leaves them enabled. Nothing here runs without
the confirm, and never over a webhook.

What is listed is the brand's control entities (`ENABLE_KEYS`) and every entity its adapter reads (`read_matches`,
walked from the adapter itself: its facts, reserves, modes, power per phase and the rest). Before control is on, the
battery plan still needs the battery's size and floor: when one of those sits in a disabled entity, the status's
`enable_facts` names just those (`disabled_fact_entities`), and the person's confirm enables only them
(`async_enable_facts`), never a control.

`ENABLE_KEYS` is each adapter's list of control entities as `(domain, unique-id key)`, the keys `commands.py`'s roles
match (a test keeps the two together), plus what the per-phase control needs.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import CONF_BATTERY_AGGREGATE_POWER_ENTITY
from .adapters import ADAPTERS, Adapter, Match, _goodwe, _key, _tesla
from .facts import BatteryFacts

_LOGGER = logging.getLogger(__name__)

#: Per brand, the entities the control needs enabled: `(domain, key)`.
ENABLE_KEYS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    "sigenergy": (
        ("switch", "plant_remote_ems_enable"),
        ("select", "plant_remote_ems_control_mode"),
        ("number", "plant_ess_max_charging_limit"),
        ("number", "plant_ess_max_discharging_limit"),
        # Read by SpotNav (the plant's own import limit under a grid charge).
        ("number", "plant_grid_maximum_import_limitation"),
        # The per-phase control: the switch for independent phase control and each phase's fixed target.
        ("switch", "plant_independent_phase_power_control_enable"),
        ("number", "plant_phase_a_active_power_fixed_target"),
        ("number", "plant_phase_b_active_power_fixed_target"),
        ("number", "plant_phase_c_active_power_fixed_target"),
        # The plant's total active power target (40001): acts in the same mode; kept at 0, and read by the plain
        # control so a value left there never runs.
        ("number", "plant_active_power_fixed_target"),
        # Read: the plant's own power per phase (`adapters.SIGENERGY.plant_phase_power`, disabled by default), which
        # the per-phase control needs and which lets a grid charge from idle count the battery's cover per phase.
        ("sensor", "plant_phase_a_active_power"),
        ("sensor", "plant_phase_b_active_power"),
        ("sensor", "plant_phase_c_active_power"),
        # Read by the small-fuse grid charge (`ImportLimitPhases`): the grid sensor's status and the Output Type
        # (L1/L2/L3/N), both disabled by default; without them the plant's per-phase import limit is never relied on.
        ("sensor", "plant_grid_sensor_status"),
        ("sensor", "inverter_output_type"),
    ),
    "solaredge": (
        ("number", "storage_command_timeout"),
        ("number", "storage_charge_limit"),
        ("number", "storage_discharge_limit"),
        ("select", "storage_command_mode"),
        ("select", "storage_control_mode"),
        ("select", "ac_charge_policy"),
    ),
    "huawei": (
        ("sensor", "forcible_charge_discharge_write"),
        ("number", "storage_maximum_discharging_power"),
        ("number", "storage_maximum_charging_power"),
    ),
    "solax": (
        ("number", "remotecontrol_autorepeat_duration"),
        ("number", "remotecontrol_active_power"),
        ("select", "remotecontrol_power_control"),
        ("button", "remotecontrol_trigger"),
    ),
    # The Solinteg plugin of `solax_modbus` (enabled by default but for the three phase numbers).
    "solinteg": (
        ("select", "working_mode"),
        ("number", "battery_charge_discharge_power_target"),
        # The per-phase control: the AC power scheduling mode, Grid Unbalanced Output (read, never written) and each
        # phase's AC power.
        ("select", "ems_acctrl_mode"),
        ("select", "grid_unbalanced_output"),
        ("number", "ems_acctrl_phase_a_power"),
        ("number", "ems_acctrl_phase_b_power"),
        ("number", "ems_acctrl_phase_c_power"),
    ),
    # Nothing ships disabled in `ferroamp`; the sensor of the last request is the read-back.
    "ferroamp": (("sensor", "last_cmd"),),
    # `victron_gx` and `victron_mqtt` enable all of these by default; listed for a person who disabled one.
    "victron": (
        ("number", "system_ess_max_charge_power"),
        ("number", "system_ess_max_inverter_power_limit"),
        ("number", "system_ac_power_set_point"),
        # The charge per phase (external control): the ESS mode, both flags, a setpoint per phase, and the power on
        # each Multi's AC output and AC input.
        ("select", "system_ess_mode"),
        ("switch", "vebus_hub4_disable_charge"),
        ("switch", "vebus_hub4_disable_feed_in"),
        ("number", "vebus_ac_power_setpoint_l1"),
        ("number", "vebus_ac_power_setpoint_l2"),
        ("number", "vebus_ac_power_setpoint_l3"),
        ("sensor", "vebus_inverter_output_power_l1"),
        ("sensor", "vebus_inverter_output_power_l2"),
        ("sensor", "vebus_inverter_output_power_l3"),
        ("sensor", "vebus_inverter_input_power_l1"),
        ("sensor", "vebus_inverter_input_power_l2"),
        ("sensor", "vebus_inverter_input_power_l3"),
    ),
    # GoodWe's HACS fork: the eco numbers exist only there; core's entities are enabled by default.
    "goodwe": (
        ("select", "operation_mode"),
        ("number", "eco_mode_power"),
        ("number", "eco_mode_soc"),
        ("number", "battery_discharge_depth"),
    ),
    # Tesla's reserve and grid-charging switch are enabled by default; listed so a person's own disable is offered.
    # The battery's size is disabled by default, and without it no grid charge runs (the number of Powerwalls is
    # not known, `commands.fixed_rate_kw`).
    "tesla": (
        ("number", "backup_reserve_percent"),
        ("switch", "components_disallow_charge_from_grid_with_solar_installed"),
        ("sensor", "total_pack_energy"),
    ),
    # `marstek_modbus` ships every control entity disabled. The RS485 control switch is also read by the adapter's
    # mode rule (someone else's force commands).
    "marstek": (
        ("number", "max_discharge_power"),
        ("switch", "rs485_control_mode"),
        ("number", "set_charge_power"),
        ("select", "force_mode"),
    ),
}

#: Where a brand's integration keeps the key in the unique id (`adapters._goodwe`, `_tesla`); `_key` for the rest.
_BUILDERS: Final[Mapping[str, Callable[..., Match]]] = {"goodwe": _goodwe, "tesla": _tesla}

#: The reasons an entity is disabled that a person's confirm may lift (not the config entry's or Home Assistant's).
_LIFTABLE: Final = (er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER)

#: What a fact the plan needs is (`fact_needs`): the battery's size and its own floor.
NEED_SIZE: Final = "size"
NEED_FLOOR: Final = "floor"
NEED_RATING: Final = "rating"


def adapter_of(brand: str | None) -> Adapter | None:
    """The adapter whose brand is `brand`, `None` for none."""
    return next((adapter for adapter in ADAPTERS if adapter.brand == brand), None)


def _walk(value: Any) -> Iterator[Match]:
    """Every `Match` in an adapter's value: in a mapping's values, a tuple's items and a dataclass's fields."""
    if isinstance(value, Match):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk(item)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for item in dataclasses.fields(value):
            yield from _walk(getattr(value, item.name))


def read_matches(adapter: Adapter) -> list[Match]:
    """Every entity the adapter reads (or names), as its `Match`es in the dataclass's order, without repeats: its facts,
    modes, grid status, controls, plant maximum, reserves, power per phase, cell temperatures, packs and heater. Taken
    from the adapter itself, so a new brand or key is never left out (a test walks every adapter). The heater's time
    slots (`PreheatSpec.slot`, 3 x 30 numbers only ever read in shadow) are keys, not `Match`es, and are not enabled."""
    found: list[Match] = []
    for match in _walk(adapter):
        if match not in found:
            found.append(match)
    return found


def _control_matches(brand: str) -> list[Match]:
    build = _BUILDERS.get(brand, _key)
    return [build(key, domain) for domain, key in ENABLE_KEYS.get(brand, ())]


def _matches(brand: str) -> list[Match]:
    """The brand's control entities (`ENABLE_KEYS`) first, then every entity its adapter reads."""
    adapter = adapter_of(brand)
    found = _control_matches(brand)
    for match in [] if adapter is None else read_matches(adapter):
        if match not in found:
            found.append(match)
    return found


def _scope(hass: HomeAssistant, config: Mapping[str, Any]) -> tuple[er.RegistryEntry, list[er.RegistryEntry]] | None:
    """The site's battery entity and the entities of its config entry on its platform; `None` without one."""
    registry = er.async_get(hass)
    entity_id = config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
    battery = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
    if battery is None or battery.config_entry_id is None:
        return None
    entries = [
        entry
        for entry in er.async_entries_for_config_entry(registry, battery.config_entry_id)
        if entry.platform == battery.platform
    ]
    return battery, entries


def _pick(match: Match, battery: er.RegistryEntry, entries: list[er.RegistryEntry]) -> er.RegistryEntry | None:
    """The same pick as `facts.find_entity`: the one on the battery's device, else the only one."""
    candidates = sorted(
        (entry for entry in entries if match.matches(entry.domain, str(entry.unique_id or ""))),
        key=lambda entry: entry.entity_id,
    )
    own = [entry for entry in candidates if battery.device_id is not None and entry.device_id == battery.device_id]
    return own[0] if own else (candidates[0] if len(candidates) == 1 else None)


def _disabled(
    battery: er.RegistryEntry, entries: list[er.RegistryEntry], matches: list[Match]
) -> list[er.RegistryEntry]:
    """The picked entity of each match that is disabled and may be enabled, in order, each once."""
    found: list[er.RegistryEntry] = []
    for match in matches:
        chosen = _pick(match, battery, entries)
        if chosen is not None and chosen.disabled_by in _LIFTABLE and chosen not in found:
            found.append(chosen)
    return found


def disabled_control_entities(
    hass: HomeAssistant, config: Mapping[str, Any], brand: str | None
) -> tuple[str | None, list[er.RegistryEntry]]:
    """The config entry of the site's battery and the brand's entities in it that are disabled (and may be enabled):
    the control entities in the order of `ENABLE_KEYS`, then the ones its adapter reads (`read_matches`); `(None, [])`
    when the brand has neither, the site has no battery entity or nothing is disabled. Registry reads only."""
    if brand is None or (brand not in ENABLE_KEYS and adapter_of(brand) is None):
        return None, []
    scope = _scope(hass, config)
    if scope is None:
        return None, []
    battery, entries = scope
    return battery.config_entry_id, _disabled(battery, entries, _matches(brand))


def disabled_fact_entities(
    hass: HomeAssistant, config: Mapping[str, Any], facts: BatteryFacts
) -> tuple[str | None, list[er.RegistryEntry], list[str]]:
    """Before control is on: the disabled entities that hold a fact the battery plan needs and does not have, and
    which facts (`NEED_SIZE`, `NEED_FLOOR`, `NEED_RATING`). The size when the facts have none and its entity is disabled (or, for a
    brand that states it in amp-hours, the amp-hours and the voltage are both there and one is disabled); the floor
    likewise from the reserves. Never a control. `(None, [], [])` when nothing would change. Registry reads only."""
    adapter = adapter_of(facts.brand)
    scope = None if adapter is None else _scope(hass, config)
    if adapter is None or scope is None:
        return None, [], []
    battery, entries = scope
    found: list[er.RegistryEntry] = []
    needs: list[str] = []

    def add(need: str, matches: list[Match]) -> None:
        more = [entry for entry in _disabled(battery, entries, matches) if entry not in found]
        if more:
            found.extend(more)
            needs.append(need)

    if facts.capacity_kwh is None:
        size = adapter.facts.get("capacity")
        if size is not None and _pick(size, battery, entries) is not None:
            add(NEED_SIZE, [size])
        elif all(role in adapter.facts for role in ("capacity_ah", "voltage")):
            pair = [adapter.facts["capacity_ah"], adapter.facts["voltage"]]
            if all(_pick(match, battery, entries) is not None for match in pair):
                add(NEED_SIZE, pair)
    if facts.reserve_soc_pct is None:
        add(NEED_FLOOR, [source.match for source in adapter.reserves])
    if facts.rated_charge_kw is None or facts.rated_discharge_kw is None:
        # The rated charge and discharge power, read directly or as the BMS' currents times the battery voltage.
        direct = [adapter.facts[role] for role in ("rated_charge", "rated_discharge") if role in adapter.facts]
        amps = [adapter.facts[role] for role in ("charge_current", "discharge_current") if role in adapter.facts]
        volts = adapter.facts.get("voltage")
        if volts is not None and amps:
            direct.extend([*amps, volts])
        add(NEED_RATING, [match for match in direct if _pick(match, battery, entries) is not None])
    if not found:
        return None, [], []
    return battery.config_entry_id, found, needs


def _block(hass: HomeAssistant, entry_id: str, entities: list[er.RegistryEntry]) -> dict[str, Any]:
    entry = hass.config_entries.async_get_entry(entry_id)
    return {
        "integration": (entry.title if entry is not None and entry.title else entities[0].platform),
        "entities": [
            {"entity_id": entity.entity_id, "name": entity.name or entity.original_name or entity.entity_id}
            for entity in entities
        ],
    }


def describe(hass: HomeAssistant, config: Mapping[str, Any], brand: str | None) -> dict[str, Any] | None:
    """What the card asks about: the integration and the disabled entities, `None` when there are none."""
    entry_id, entities = disabled_control_entities(hass, config, brand)
    if entry_id is None or not entities:
        return None
    return _block(hass, entry_id, entities)


def describe_facts(hass: HomeAssistant, config: Mapping[str, Any], facts: BatteryFacts) -> dict[str, Any] | None:
    """The card's one line before control is on (`enable_facts`): as `describe`, and `needs` the facts they hold;
    `None` when nothing the plan needs is disabled."""
    entry_id, entities, needs = disabled_fact_entities(hass, config, facts)
    if entry_id is None or not entities:
        return None
    return _block(hass, entry_id, entities) | {"needs": needs}


async def _async_enable(hass: HomeAssistant, entry_id: str | None, entities: list[er.RegistryEntry]) -> list[str]:
    if entry_id is None or not entities:
        return []
    registry = er.async_get(hass)
    for entity in entities:
        registry.async_update_entity(entity.entity_id, disabled_by=None)
    _LOGGER.info("SpotNav battery: enabled %s, reloading the integration", [e.entity_id for e in entities])
    await hass.config_entries.async_reload(entry_id)
    return [entity.entity_id for entity in entities]


async def async_enable(hass: HomeAssistant, config: Mapping[str, Any], brand: str | None) -> list[str]:
    """Enable exactly the disabled entities `disabled_control_entities` finds, then reload their config entry once
    (only when something was enabled). Answers the entity ids enabled. The caller has the person's confirm."""
    entry_id, entities = disabled_control_entities(hass, config, brand)
    return await _async_enable(hass, entry_id, entities)


async def async_enable_facts(hass: HomeAssistant, config: Mapping[str, Any], facts: BatteryFacts) -> list[str]:
    """Enable exactly the fact entities `disabled_fact_entities` finds (never a control), then reload their config
    entry once. Answers the entity ids enabled. The caller has the person's confirm."""
    entry_id, entities, _needs = disabled_fact_entities(hass, config, facts)
    return await _async_enable(hass, entry_id, entities)
