"""SENEC through `senec` (marq24 `32eed20`, 2026.11.3): read only. The ids are `senec.<entry title>_<key>` with a title the
person chose, so every key is found by its suffix inside the battery's own config entry.

The registry (the local entry) is the fixture `senec` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import SENEC, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry


def _entity(title: str, key: str, state: str, unit: str | None = None, domain: str = "sensor", disabled: bool = False) -> EntityFact:
    return EntityFact(
        entity_id=f"{domain}.{key}", platform="senec", unique_id=f"senec.{title}_{key}".lower(), device_id="dev",
        config_entry_id="entry", state=state, unit=unit, disabled=disabled,
    )


def test_senec_is_read_only() -> None:
    assert adapter_for("senec", "SENEC GmbH") is SENEC
    assert all(control.brand != "senec" for control in commands.CONTROLS)
    assert SENEC.hold is None and SENEC.grid_charge is None


def test_it_reads_level_counters_and_leaves_the_size_and_ratings_empty() -> None:
    facts = _facts("senec")

    assert facts.brand == "senec"
    assert facts.soc_pct == 64.0
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (3200.5, 2900.2)
    # No route states a size: the health sensor is a percent, not a capacity.
    assert facts.capacity_kwh is None
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.sources["soc"] == "sensor.senec_battery_charge_percent"


def test_the_reserve_is_read_once_its_disabled_sensor_is_enabled() -> None:
    assert _facts("senec").reserve_soc_pct is None
    facts = _facts("senec", **{"sensor.senec_spare_capacity": {"disabled": False}})

    assert facts.reserve_soc_pct == 20.0


@pytest.mark.parametrize("state, expected", [
    ("OWN CONSUMPTION", "free"), ("EIGENVERBRAUCH", "free"), ("AUTOCONSUMO", "free"), ("DISCHARGE", "free"),
    ("MAINTENANCE CHARGE", "self_managed"), ("MAN. SAFETY CHARGE", "self_managed"), ("EMERGENCY CHARGE", "self_managed"),
    ("EQUALIZATION: CHARGE", "self_managed"),
    # Another language reads UNKNOWN in the integration: no reading, so the mode is unknown and never free.
    ("UNKNOWN", "unknown"), ("unavailable", "unknown"),
])
def test_only_the_own_consumption_states_are_free(state: str, expected: str) -> None:
    facts = _facts("senec", **{"sensor.senec_system_state": {"state": state}})

    assert facts.management.state == expected


def test_the_load_battery_switch_on_is_someone_elses_charge() -> None:
    facts = _facts("senec", **{"switch.senec_safe_charge": {"state": "on"}})

    assert facts.management.state == "self_managed"
    assert "Load Battery:on" in facts.management.reason


def test_an_entry_without_a_system_state_has_no_mode_to_read() -> None:
    entities = [_entity("senec_openapi", "battery_state_power", "800", "W"), _entity("senec_openapi", "battery_charge_percent", "50", "%")]

    facts = battery_facts(entities[0], entities, manufacturer="SENEC GmbH")

    assert facts.soc_pct == 50.0 and facts.management.state == "unknown"


def test_a_title_with_spaces_is_matched_by_the_suffix() -> None:
    title = "My Senec"
    entities = [
        _entity(title, "battery_state_power", "800", "W"),
        _entity(title, "battery_charge_percent", "71", "%"),
        _entity(title, "system_state", "OWN CONSUMPTION"),
    ]

    facts = battery_facts(entities[0], entities, manufacturer="SENEC GmbH")

    assert facts.soc_pct == 71.0 and facts.management.state == "free"


def test_the_webapi_has_its_own_level_and_counters_by_the_integrations_names() -> None:
    title = "senec_webapi"
    entities = [
        _entity(title, "accuexport_now", "0.8", "kW"),
        _entity(title, "accuimport_now", "0", "kW"),
        _entity(title, "acculevel_now", "55", "%"),
        _entity(title, "accuexport_total", "1200", "kWh"),
        _entity(title, "accuimport_total", "1000", "kWh"),
    ]

    facts = battery_facts(entities[0], entities, manufacturer="SENEC GmbH")

    assert facts.soc_pct == 55.0
    # Charged is the integration's "Battery charged" (`accuexport_total`).
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (1200.0, 1000.0)


def test_the_local_signed_power_is_charge_positive_and_the_other_sensors_are_not_the_battery() -> None:
    r = Registry()
    r.device("s", model="SENEC.Home V3 hybrid", manufacturer="SENEC GmbH", entry="se")
    for key in ("battery_charge_power", "battery_discharge_power", "battery_state_power", "grid_state_power"):
        r.add("senec", f"s_{key}", f"senec.senec_{key}", device_class="power", unit="W", device="s", entry="se")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.s_battery_state_power"
    assert battery.inverted is False and battery.discharge_entity_id is None


def test_the_webapis_two_sensors_are_the_pair_charge_first() -> None:
    r = Registry()
    r.device("w", model="SENEC.Home", manufacturer="SENEC GmbH", entry="we")
    for key in ("accuexport_now", "accuimport_now", "gridimport_now"):
        r.add("senec", f"w_{key}", f"senec.senec_webapi_{key}", device_class="power", unit="kW", device="w", entry="we")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.w_accuexport_now"
    assert battery.discharge_entity_id == "sensor.w_accuimport_now"


def test_the_coexistence_row_names_the_cloud_and_no_aggregator() -> None:
    row = coexistence_for(platform="senec")

    assert row is not None and row.brand == "senec" and "SENEC.Cloud" in row.own_optimiser and row.aggregators == ()
