"""Enabling the brand's disabled control entities on the person's confirm (`battery/optin.py`): each adapter's list,
nothing changed without the confirm, exactly those entities enabled and the entry reloaded once, an entity already
enabled left alone, and no blocking call."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api.battery_control import (
    ERROR_ENABLE_FAILED,
    async_battery_control_answer,
)
from custom_components.spotnav.battery import optin
from custom_components.spotnav.battery.adapters import ADAPTERS
from custom_components.spotnav.battery.commands import CONTROLS
from custom_components.spotnav.const import CONF_BATTERY_AGGREGATE_POWER_ENTITY
from custom_components.spotnav.runtime import site_controller_for
from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

INTEGRATION = er.RegistryEntryDisabler.INTEGRATION
SIGEN_KEYS = [
    "plant_remote_ems_enable", "plant_remote_ems_control_mode", "plant_ess_max_charging_limit",
    "plant_ess_max_discharging_limit", "plant_grid_maximum_import_limitation",
    "plant_independent_phase_power_control_enable", "plant_phase_a_active_power_fixed_target",
    "plant_phase_b_active_power_fixed_target", "plant_phase_c_active_power_fixed_target",
    "plant_active_power_fixed_target", "plant_phase_a_active_power", "plant_phase_b_active_power",
    "plant_phase_c_active_power", "plant_grid_sensor_status", "inverter_output_type",
]


def _uid(brand: str, key: str) -> str:
    """A unique id as the brand's integration builds it (GoodWe: `goodwe-<key>-<serial>`, Tesla: `<site id>-<key>`)."""
    return {"goodwe": f"goodwe-{key}-SN1", "tesla": f"1234-{key}"}.get(brand, f"PLANT_{key}")


def _plant(
    hass: HomeAssistant, brand: str, *, enabled: frozenset[str] = frozenset(), disabled_by: Any = INTEGRATION
) -> tuple[str, dict[str, str]]:
    """A plant of the brand's integration with every control entity of `optin.ENABLE_KEYS` in the registry, disabled
    except the keys in `enabled`; answers the battery entity and the entity id per key."""
    # The last platform: Victron's first is in Home Assistant core, whose library is not installed here.
    platform = next(control.adapter.platforms[-1] for control in CONTROLS if control.brand == brand)
    owner = MockConfigEntry(domain=platform, entry_id="PLANT", title="The plant")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=owner.entry_id, identifiers={(platform, "plant")})
    registry = er.async_get(hass)
    battery = registry.async_get_or_create(
        "sensor", platform, _uid(brand, "battery_power"), device_id=device.id, config_entry=owner,
        suggested_object_id="plant_battery_power",
    )
    found: dict[str, str] = {}
    for domain, key in optin.ENABLE_KEYS[brand]:
        entry = registry.async_get_or_create(
            domain, platform, _uid(brand, key), device_id=device.id, config_entry=owner,
            suggested_object_id=f"plant_{key}", disabled_by=None if key in enabled else disabled_by,
        )
        found[key] = entry.entity_id
    return battery.entity_id, found


def _config(battery: str) -> dict[str, Any]:
    return {CONF_BATTERY_AGGREGATE_POWER_ENTITY: battery}


@pytest.mark.parametrize("control", CONTROLS, ids=lambda control: control.brand)
def test_every_roles_entity_is_in_the_adapters_list(control: Any) -> None:
    keys = optin.ENABLE_KEYS[control.brand]
    for role in control.roles:
        assert any(role.match.matches(domain, _uid(control.brand, key)) for domain, key in keys), role.name


@pytest.mark.parametrize("control", CONTROLS, ids=lambda control: control.brand)
async def test_the_lists_per_adapter_in_order(hass: HomeAssistant, control: Any) -> None:
    battery, found = _plant(hass, control.brand)
    entry_id, entities = optin.disabled_control_entities(hass, _config(battery), control.brand)
    assert entry_id == "PLANT"
    assert [entry.entity_id for entry in entities] == list(found.values()), control.brand


def test_the_sigenergy_list_is_kept() -> None:
    assert [key for _domain, key in optin.ENABLE_KEYS["sigenergy"]] == SIGEN_KEYS


async def test_an_enabled_entity_and_a_brand_without_a_list_are_left_out(hass: HomeAssistant) -> None:
    battery, found = _plant(hass, "sigenergy", enabled=frozenset({"plant_ess_max_discharging_limit"}))
    _entry, entities = optin.disabled_control_entities(hass, _config(battery), "sigenergy")
    assert found["plant_ess_max_discharging_limit"] not in [entry.entity_id for entry in entities]
    assert len(entities) == len(SIGEN_KEYS) - 1
    # A brand whose entities are not in this entry (Enphase's adapter reads, none of it here) finds none.
    assert optin.disabled_control_entities(hass, _config(battery), "enphase")[1] == []
    assert optin.disabled_control_entities(hass, _config(battery), "no_such_brand") == (None, [])
    assert optin.disabled_control_entities(hass, _config(battery), None) == (None, [])
    assert optin.disabled_control_entities(hass, {}, "sigenergy") == (None, [])


async def test_an_entity_disabled_by_the_integration_entry_is_not_lifted(hass: HomeAssistant) -> None:
    battery, _found = _plant(hass, "sigenergy", disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY)
    assert optin.disabled_control_entities(hass, _config(battery), "sigenergy")[1] == []


async def _site(hass: HomeAssistant, battery: str):
    _charger, site = await setup_charger_and_site(hass, battery_aggregate_power_entity=battery)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    return site, controller.battery_control


async def test_the_status_lists_them_and_nothing_changes_without_the_confirm(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    battery, found = _plant(hass, "sigenergy")
    site, control = await _site(hass, battery)
    reloads: list[str] = []

    async def reload(entry_id: str) -> bool:
        reloads.append(entry_id)
        return True

    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    status = control.status()
    assert status["enable_entities"]["integration"] == "The plant"
    assert [row["entity_id"] for row in status["enable_entities"]["entities"]] == list(found.values())
    # Turning control on, a read and a refused attempt change nothing in the registry.
    await async_battery_control_answer(hass, site, {"battery_control_enabled": True}, admin=True)
    await async_battery_control_answer(hass, site, None, admin=True)
    for forbidden in (
        {"admin": False, "over_webhook": False},
        {"admin": True, "over_webhook": True},
    ):
        answer = await async_battery_control_answer(hass, site, None, enable_entities=True, **forbidden)
        assert answer["ok"] is False
    registry = er.async_get(hass)
    assert all(registry.async_get(entity_id).disabled_by is INTEGRATION for entity_id in found.values())
    assert reloads == []


async def test_the_confirm_enables_exactly_those_entities_and_reloads_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    battery, found = _plant(hass, "sigenergy", enabled=frozenset({"plant_ess_max_discharging_limit"}))
    other = er.async_get(hass).async_get_or_create(
        "number", "sigen", "PLANT_plant_grid_point_maximum_export_limitation", config_entry=hass.config_entries.async_get_entry("PLANT"),
        suggested_object_id="plant_export", disabled_by=INTEGRATION,
    )
    site, control = await _site(hass, battery)
    reloads: list[str] = []
    monkeypatch.setattr(hass.config_entries, "async_reload", lambda entry_id: _reload(reloads, entry_id))

    async def no_executor(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a blocking call")

    with monkeypatch.context() as scoped:
        scoped.setattr(hass, "async_add_executor_job", no_executor)
        answer = await async_battery_control_answer(
            hass, site, {"battery_control_enabled": True}, admin=True, enable_entities=True
        )
    assert answer["ok"] is True
    assert answer["status"]["options"]["battery_control_enabled"] is True
    registry = er.async_get(hass)
    for key, entity_id in found.items():
        assert registry.async_get(entity_id).disabled_by is None, key
    # Not in the list: left disabled.
    assert registry.async_get(other.entity_id).disabled_by is INTEGRATION
    assert reloads == ["PLANT"]
    assert control.enable_entities() is None
    # A second confirm finds nothing and reloads nothing.
    assert await control.async_enable_entities() == []
    assert reloads == ["PLANT"]


async def _reload(reloads: list[str], entry_id: str) -> bool:
    reloads.append(entry_id)
    return True


async def test_turning_control_off_leaves_them_enabled(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    battery, found = _plant(hass, "sigenergy")
    site, _control = await _site(hass, battery)
    reloads: list[str] = []
    monkeypatch.setattr(hass.config_entries, "async_reload", lambda entry_id: _reload(reloads, entry_id))
    await async_battery_control_answer(hass, site, {"battery_control_enabled": True}, admin=True, enable_entities=True)
    await async_battery_control_answer(hass, site, {"battery_control_enabled": False}, admin=True)
    registry = er.async_get(hass)
    assert all(registry.async_get(entity_id).disabled_by is None for entity_id in found.values())


async def test_a_failed_reload_is_an_error_and_writes_no_options(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    battery, _found = _plant(hass, "huawei")
    site, _control = await _site(hass, battery)

    async def broken(_entry_id: str) -> bool:
        raise RuntimeError("reload failed")

    monkeypatch.setattr(hass.config_entries, "async_reload", broken)
    answer = await async_battery_control_answer(
        hass, site, {"battery_control_enabled": True}, admin=True, enable_entities=True
    )
    assert answer["error"] == ERROR_ENABLE_FAILED
    assert site.data.get("battery_control_enabled") is not True


async def test_core_goodwe_without_the_hacs_numbers_gets_one_note_in_the_checklist(hass: HomeAssistant) -> None:
    battery, found = _plant(hass, "goodwe", enabled=frozenset(key for _d, key in optin.ENABLE_KEYS["goodwe"]))
    registry = er.async_get(hass)
    registry.async_remove(found["eco_mode_power"])
    registry.async_remove(found["eco_mode_soc"])
    _s, control = await _site(hass, battery)
    needs = control.status()["needs"]
    assert needs.count("needs_hacs_integration:mletenay/home-assistant-goodwe-inverter") == 1
    assert not any(need.startswith("control_not_found") for need in needs)


async def test_a_victron_max_inverter_power_of_0_is_told_in_the_checklist(hass: HomeAssistant) -> None:
    """"Max inverter power" 0 on the GX keeps the battery from ever discharging: the person is told, SpotNav never
    writes it."""
    battery, found = _plant(hass, "victron", enabled=frozenset(key for _domain, key in optin.ENABLE_KEYS["victron"]))
    _site_entry, control = await _site(hass, battery)
    hass.states.async_set(found["system_ess_max_inverter_power_limit"], "0")
    assert "inverter_power_zero" in control.status()["needs"]
    hass.states.async_set(found["system_ess_max_inverter_power_limit"], "-1")
    control._look_id += 1
    assert "inverter_power_zero" not in control.status()["needs"]
    hass.states.async_set(found["system_ess_max_inverter_power_limit"], "unknown")
    control._look_id += 1
    assert "inverter_power_zero" not in control.status()["needs"]
    # A sale left standing in the grid setpoint, which SpotNav holds no snapshot of.
    hass.states.async_set(found["system_ac_power_set_point"], "-2500")
    control._look_id += 1
    assert "grid_setpoint_selling" in control.status()["needs"]
    hass.states.async_set(found["system_ac_power_set_point"], "-20")
    control._look_id += 1
    assert "grid_setpoint_selling" not in control.status()["needs"]


async def test_a_victron_plant_is_read_at_its_multis_and_its_phase_caps_count_the_house_at_its_peak(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell on a Victron plant (`commands.VICTRON_CONTROL`): the charge per phase needs nothing but the
    recipe's own entities, the plant's power per phase is the load behind each Multi less the power on its input,
    and each phase's HA-down cap is reckoned with the house at its peak, as Sigenergy's: the Multi's own dead-man
    is the maker's word only, not yet seen on a plant, so the recipe does not state it."""
    from custom_components.spotnav.battery import gate
    from custom_components.spotnav.battery.commands import VICTRON_CONTROL
    from custom_components.spotnav.battery.control import BatteryControl
    from homeassistant.util import dt as dt_util

    battery, found = _plant(hass, "victron", enabled=frozenset(key for _domain, key in optin.ENABLE_KEYS["victron"]))
    site, control = await _site(hass, battery)
    states = {
        "system_ess_max_charge_power": "-1", "system_ess_max_inverter_power_limit": "-1",
        "system_ac_power_set_point": "50", "system_ess_mode": "phase_compensation_enabled",
        "vebus_hub4_disable_charge": "off", "vebus_hub4_disable_feed_in": "off",
        **{f"vebus_ac_power_setpoint_l{n}": "0" for n in (1, 2, 3)},
        **{f"vebus_inverter_output_power_l{n}": str(100 * n) for n in (1, 2, 3)},
        **{f"vebus_inverter_input_power_l{n}": str(1000 + 100 * n) for n in (1, 2, 3)},
    }
    for key, state in states.items():
        hass.states.async_set(found[key], state)
    monkeypatch.setattr(BatteryControl, "_phase_headroom_a", lambda self: (10.0, 10.0, 10.0))
    monkeypatch.setattr(BatteryControl, "_grid_phase_kw", lambda self: (1.0, 1.0, 1.0))
    monkeypatch.setattr(BatteryControl, "_cars_allowed_a", lambda self, **_kwargs: 0.0)
    control.facts = control._read_facts()
    assert control.facts.brand == "victron"

    assert control._phase_missing_now() is None
    now = dt_util.now()
    inputs = control.inputs(now, VICTRON_CONTROL.with_phases(True), slot=None)
    assert inputs.phase_behind_kw == pytest.approx((0.1, 0.2, 0.3))
    assert inputs.phase_point_kw == pytest.approx((1.1, 1.2, 1.3))
    assert inputs.plant_phase_kw == pytest.approx((-1.0, -1.0, -1.0))
    assert set(inputs.reads) >= {"ess_mode", "phase_l3_target", "phase_l1_load", "phase_l2_input"}
    fuse_a = control._main_fuse_a()
    assert fuse_a is not None
    volts = 400.0 / 3 ** 0.5
    caps = control._phase_caps_kw(now)
    assert not gate.silence_guards(VICTRON_CONTROL.phase_control.silence_fallback_s)
    assert caps == pytest.approx(gate.grid_charge_phase_caps_kw(
        fuse_a, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=control._house_peak_kw(now),
        house_phase_peaks_a=control._phase_peaks(now) or None,
    ))
    assert all(cap < (fuse_a - 2.0) * volts / 1000 for cap in caps), "never the fuse less the margin"

    # A sensor that reads nothing: no charge per phase.
    hass.states.async_set(found["vebus_inverter_output_power_l2"], "unavailable")
    control._look_id += 1
    assert control._phase_missing_now() == "plant_phase_power"
    assert control.inputs(now, VICTRON_CONTROL.with_phases(True), slot=None).plant_phase_kw is None


# The entities the adapters read (`optin.read_matches`) and the plan's facts before control is on (`enable_facts`).

#: The adapter's fields that hold no entity; every other field is walked for `Match`es. A new field must be named in
#: one of the two, so a new kind of read cannot be left out of what the confirm enables.
_NO_ENTITY_FIELDS = {"brand", "label", "platforms", "manufacturer", "plant_phase_sign"}
_ENTITY_FIELDS = {
    "facts", "modes", "grid_status", "hold", "grid_charge", "plant_max", "reserves", "plant_phase_power",
    "cell_temperature", "soc_packs", "preheat",
}


def _named_matches(adapter: Any) -> list[Any]:
    """Every `Match` an adapter carries, field by field as each field's type holds it (independent of the walker)."""
    found: list[Any] = list(adapter.facts.values())
    found += [rule.match for rule in adapter.modes]
    found += [] if adapter.grid_status is None else [adapter.grid_status[0]]
    found += [spec.match for spec in (adapter.hold, adapter.grid_charge) if spec is not None and spec.match is not None]
    found += [] if adapter.plant_max is None else [adapter.plant_max]
    found += [source.match for source in adapter.reserves]
    found += list(adapter.plant_phase_power or ())
    found += list(adapter.cell_temperature)
    for pack in adapter.soc_packs:
        found += [pack.soc] + ([] if pack.remaining is None else [pack.remaining])
    heater = adapter.preheat
    if heater is not None:
        found += [m for m in (heater.enable, heater.mode, heater.advance, heater.reserved_soc) if m is not None]
    return found


def test_the_adapter_fields_are_all_known() -> None:
    import dataclasses

    from custom_components.spotnav.battery.adapters import Adapter

    assert {field.name for field in dataclasses.fields(Adapter)} == _NO_ENTITY_FIELDS | _ENTITY_FIELDS


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda adapter: adapter.brand)
def test_every_entity_an_adapter_reads_can_be_enabled(adapter: Any) -> None:
    listed = optin._matches(adapter.brand)
    for match in _named_matches(adapter):
        assert match in listed, (adapter.brand, match.pattern.pattern)
    assert set(optin.read_matches(adapter)) == set(_named_matches(adapter))


def _solax(hass: HomeAssistant, *, capacity_disabled: Any = INTEGRATION) -> tuple[str, dict[str, str]]:
    """A SolaX plant of `solax_modbus` with its level and its size (`battery_install_capacity`, disabled by default)."""
    owner = MockConfigEntry(domain="solax_modbus", entry_id="SOLAX", title="SolaX")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("solax_modbus", "inverter")}, manufacturer="SolaX Power"
    )
    registry = er.async_get(hass)
    battery = registry.async_get_or_create(
        "sensor", "solax_modbus", "SolaX_battery_power_charge", device_id=device.id, config_entry=owner,
        suggested_object_id="solax_battery_power",
    )
    found: dict[str, str] = {}
    for domain, key, disabled_by in (
        ("sensor", "battery_capacity", None),
        ("sensor", "battery_install_capacity", capacity_disabled),
        ("select", "remotecontrol_power_control", INTEGRATION),
    ):
        entry = registry.async_get_or_create(
            domain, "solax_modbus", f"SolaX_{key}", device_id=device.id, config_entry=owner,
            suggested_object_id=f"solax_{key}", disabled_by=disabled_by,
        )
        found[key] = entry.entity_id
    hass.states.async_set(found["battery_capacity"], "55", {"unit_of_measurement": "%"})
    return battery.entity_id, found


async def test_solax_a_disabled_size_is_listed_and_enabled_alone_on_the_confirm(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    battery, found = _solax(hass)
    site, control = await _site(hass, battery)
    control.facts = control._read_facts()
    assert control.facts.brand == "solax" and control.facts.capacity_kwh is None
    reloads: list[str] = []
    monkeypatch.setattr(hass.config_entries, "async_reload", lambda entry_id: _reload(reloads, entry_id))

    status = control.status()
    assert status["enable_facts"] == {
        "integration": "SolaX",
        "entities": [{"entity_id": found["battery_install_capacity"], "name": found["battery_install_capacity"]}],
        "needs": ["size"],
    }
    # The opt-in's list asks for the control and the size in one confirm.
    assert [row["entity_id"] for row in status["enable_entities"]["entities"]] == [
        found["remotecontrol_power_control"], found["battery_install_capacity"],
    ]
    # Never over the webhook, never without an administrator.
    for forbidden in ({"admin": False, "over_webhook": False}, {"admin": True, "over_webhook": True}):
        answer = await async_battery_control_answer(hass, site, None, enable_facts=True, **forbidden)
        assert answer["ok"] is False
    assert reloads == []

    async def no_executor(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a blocking call")

    with monkeypatch.context() as scoped:
        scoped.setattr(hass, "async_add_executor_job", no_executor)
        answer = await async_battery_control_answer(hass, site, None, admin=True, enable_facts=True)
    assert answer["ok"] is True
    registry = er.async_get(hass)
    assert registry.async_get(found["battery_install_capacity"]).disabled_by is None
    # Only the fact: the control stays disabled until the opt-in's confirm.
    assert registry.async_get(found["remotecontrol_power_control"]).disabled_by is INTEGRATION
    assert reloads == ["SOLAX"]
    assert answer["status"]["enable_facts"] is None
    assert await control.async_enable_facts() == []
    assert reloads == ["SOLAX"]


async def test_nothing_is_listed_when_the_facts_are_enabled(hass: HomeAssistant) -> None:
    battery, found = _solax(hass, capacity_disabled=None)
    _site_entry, control = await _site(hass, battery)
    hass.states.async_set(found["battery_install_capacity"], "10", {"unit_of_measurement": "kWh"})
    control.facts = control._read_facts()
    assert control.facts.capacity_kwh == pytest.approx(10.0)
    assert control.status()["enable_facts"] is None


async def test_a_size_read_elsewhere_lists_nothing(hass: HomeAssistant) -> None:
    from dataclasses import replace

    battery, _found = _solax(hass)
    _site_entry, control = await _site(hass, battery)
    facts = replace(control._read_facts(), capacity_kwh=12.0)
    assert optin.describe_facts(hass, _config(battery), facts) is None


async def test_the_size_on_the_batterys_device_is_picked_else_the_only_one(hass: HomeAssistant) -> None:
    battery, found = _solax(hass)
    registry = er.async_get(hass)
    owner = hass.config_entries.async_get_entry("SOLAX")
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id="SOLAX", identifiers={("solax_modbus", "other")}, manufacturer="SolaX Power"
    )
    second = registry.async_get_or_create(
        "sensor", "solax_modbus", "Other_battery_install_capacity", device_id=other.id, config_entry=owner,
        suggested_object_id="other_capacity", disabled_by=INTEGRATION,
    )
    _site_entry, control = await _site(hass, battery)
    facts = control._read_facts()
    _entry, entities, needs = optin.disabled_fact_entities(hass, _config(battery), facts)
    assert [entity.entity_id for entity in entities] == [found["battery_install_capacity"]] and needs == ["size"]
    # Two candidates and none on the battery's device: neither can be told, none is listed.
    registry.async_update_entity(found["battery_install_capacity"], device_id=other.id)
    assert optin.disabled_fact_entities(hass, _config(battery), facts)[1] == []
    registry.async_remove(second.entity_id)
    _entry, entities, _needs = optin.disabled_fact_entities(hass, _config(battery), facts)
    assert [entity.entity_id for entity in entities] == [found["battery_install_capacity"]]
