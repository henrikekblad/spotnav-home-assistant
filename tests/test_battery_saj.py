"""SAJ H2, HS2 and Ampere.StoragePro through `saj_h2_modbus` (stanus74 `3f5cd84`, v3.1.3): read only. The ids are
`<entry name>_<camelCase key>` with a name the person chose, so every key is the lower-cased suffix.

The registry is the fixture `saj` in `tests/fixtures/battery/brands.json`.
"""

from __future__ import annotations

import pytest

from custom_components.spotnav.battery import commands
from custom_components.spotnav.battery.adapters import SAJ, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.facts import EntityFact, battery_facts

from tests.test_battery_facts import _facts
from tests.test_site_detection import Registry


def test_saj_is_read_only_and_only_the_h2_integration_has_an_adapter() -> None:
    assert adapter_for("saj_h2_modbus", "SAJ Electric") is SAJ
    # A PV-only integration of another maker and core `saj` have no battery.
    assert adapter_for("saj_modbus", "SAJ Electric") is None and adapter_for("saj", "SAJ Electric") is None
    assert all(control.brand != "saj" for control in commands.CONTROLS)
    assert SAJ.hold is None and SAJ.grid_charge is None


def test_it_reads_the_inverters_level_and_leaves_size_ratings_floor_and_firmware_empty() -> None:
    facts = _facts("saj")

    assert facts.brand == "saj"
    # `batEnergyPercent`, not the BMS block's `Bat1SOC`.
    assert facts.soc_pct == 64.0 and facts.sources["soc"] == "sensor.saj_battery_energy_percent"
    # `BatCapcity` has no unit (likely Ah, unconfirmed), the limits are permille of an unread power, and the depth
    # register is not known to be a depth or a level.
    assert facts.capacity_kwh is None and facts.estimates == ()
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.reserve_soc_pct is None
    assert facts.firmware is None


def test_the_counters_are_read_once_their_disabled_sensors_are_enabled() -> None:
    assert _facts("saj").charged_total_kwh is None
    facts = _facts(
        "saj",
        **{
            "sensor.saj_battery_total_charge": {"disabled": False},
            "sensor.saj_battery_total_discharge": {"disabled": False},
        },
    )

    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (4100.5, 3800.2)


@pytest.mark.parametrize(
    "state, expected",
    [("0", "free"), ("1", "self_managed"), ("2", "self_managed"), ("3", "self_managed"), ("10", "self_managed"),
     ("12", "self_managed"), ("unavailable", "unknown")],
)
def test_only_self_use_is_the_free_mode(state: str, expected: str) -> None:
    facts = _facts("saj", **{"sensor.saj_app_mode": {"state": state}})

    assert facts.management.state == expected


def test_the_app_mode_number_is_not_the_mode_that_is_read() -> None:
    facts = _facts("saj", **{"number.saj_app_mode": {"state": "3"}})

    assert facts.management.state == "free"


def test_a_name_the_person_chose_is_matched_by_the_suffix() -> None:
    def entity(key: str, state: str, domain: str = "sensor") -> EntityFact:
        return EntityFact(
            entity_id=f"{domain}.x_{key}", platform="saj_h2_modbus", unique_id=f"Garage Roof_{key}", device_id="d",
            config_entry_id="e", state=state, unit="%" if key == "batEnergyPercent" else None,
        )

    entities = [entity("batEnergyPercent", "55"), entity("AppMode", "0"), entity("Bat1SOC", "50")]

    facts = battery_facts(entities[0], entities, manufacturer="SAJ Electric")

    assert facts.soc_pct == 55.0 and facts.management.state == "free"


def test_the_battery_power_is_the_aggregate_read_discharge_positive_sign_unverified_on_hardware() -> None:
    r = Registry()
    r.device("inv", model="H2-5K-S2", manufacturer="SAJ Electric", entry="sj")
    for key in ("fast_batteryPower", "BatPower", "batteryPower", "totalgridPower"):
        r.add("saj_h2_modbus", f"inv_{key.lower()}", f"SAJ_{key}", device_class="power", unit="W", device="inv", entry="sj")

    [battery] = r.detect().batteries

    assert battery.entity_id == "sensor.inv_batterypower"
    assert battery.inverted is True and battery.discharge_entity_id is None


def test_the_coexistence_row_names_the_app_and_tibber_as_unverified() -> None:
    row = coexistence_for(platform="saj_h2_modbus")

    assert row is not None and row.brand == "saj" and "AI saving" in row.own_optimiser
    assert row.aggregators == ("Tibber, named for SAJ [U]",)
