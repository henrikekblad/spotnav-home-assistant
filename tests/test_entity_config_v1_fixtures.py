"""Cross-language contract fixtures for `spotnav/get_entity_config` and
`spotnav/update_entity_config` (v1), written by the real handlers -- never hand-built.

Set `SPOTNAV_WRITE_FIXTURES=1` to (re)write the committed files; the default
run only compares, so a payload change cannot slip past without the fixture moving with it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_GRID_POWER_SOURCE, MEASUREMENT_MODE_DERIVED
from tests.helpers import add_ambiguous_vehicle_device
from tests.test_vehicle_soc_command import set_message
from tests.messages import get_message, register, update_entity_config_message
from tests.world import setup_charger_and_site
from tests.world import admin, non_admin, ws_call

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "entity_config" / "v1"


async def _get_direct(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "get_direct")
    return (await ws_call(await admin(hass, ws), get_message(charger.entry_id)))["result"]


async def _get_direct_total(hass, ws, _token) -> dict[str, Any]:
    """A direct site (per-phase current only) that carries the meter's total grid power as an
    import/export pair: the two `grid_power_source_*` fields populated, and the detected Tibber Pulse
    listing its total as `grid_power` and `grid_power_export` rows with no phase."""
    from tests.site_registry import materialize
    from tests.test_site_detection import tibber_pulse_with_production

    made = materialize(hass, tibber_pulse_with_production())
    direct = {phase: made[f"tibber_current_l{n}"] for phase, n in zip(("L1", "L2", "L3"), "123")}
    charger, _ = await setup_charger_and_site(
        hass,
        "get_direct_total",
        direct_entities=direct,
        extra_data={
            CONF_GRID_POWER_SOURCE: {
                "power": made["tibber_power"],
                "power_export": made["tibber_power_production"],
            },
            # The Pulse's currents are signed, as its detection applies them.
            "site_current_signed": True,
        },
    )
    return (await ws_call(await admin(hass, ws), get_message(charger.entry_id)))["result"]


async def _get_derived(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(
        hass, "get_derived", measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities={}
    )
    return (await ws_call(await admin(hass, ws), get_message(charger.entry_id)))["result"]


async def _get_detected(hass, ws, _token) -> dict[str, Any]:
    """A derived site read from a slow integration, beside detected meters, a battery and a device that
    balances load by itself: the `measurement`, `warnings` and `detection` blocks, all populated."""
    from tests.site_registry import materialize
    from tests.test_site_detection import easee_equalizer, huawei_solar, sma_with_inverter, solaredge_modbus_multi

    made = materialize(hass, solaredge_modbus_multi())
    materialize(hass, huawei_solar())
    materialize(hass, easee_equalizer())
    materialize(hass, sma_with_inverter())
    derived = {}
    for phase, letter in zip(("L1", "L2", "L3"), "abc"):
        power = made[f"se_m1_power_{letter}"]
        voltage = made[f"se_m1_voltage_{letter}"]
        hass.states.async_set(power, "2300", {"unit_of_measurement": "W"})
        hass.states.async_set(voltage, "230", {"unit_of_measurement": "V"})
        derived[phase] = {"power": power, "voltage": voltage}
    charger, _ = await setup_charger_and_site(
        hass, "get_detected", measurement_mode=MEASUREMENT_MODE_DERIVED, derived_entities=derived
    )
    return (await ws_call(await admin(hass, ws), get_message(charger.entry_id)))["result"]


async def _get_no_site(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "get_lone", site=False)
    return (await ws_call(await admin(hass, ws), get_message(charger.entry_id)))["result"]


async def _success_charger(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "ok_charger")
    limit = register(hass, "number", "ok_limit", "Charge current")
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id,
            scope="charger",
            expected={"current_limit": ""},
            changes={"current_limit": limit},
        ),
    )
    assert frame["result"]["ok"] is True
    return frame["result"]


async def _success_current_limit_none(hass, ws, _token) -> dict[str, Any]:
    """The answer to choosing "None" for the current limit: `none.chosen` true, nothing effective."""
    charger, _ = await setup_charger_and_site(hass, "ok_none")
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id,
            scope="charger",
            expected={"current_limit": ""},
            changes={"current_limit": "none"},
        ),
    )
    assert frame["result"]["ok"] is True
    return frame["result"]


async def _success_site(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "ok_site")
    battery = register(hass, "sensor", "ok_battery", "Battery power", device_class="power")
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id,
            scope="site",
            expected={"main_fuse_a": 25, "max_age_s": 120},
            changes={"main_fuse_a": 32, "max_age_s": 60, "battery_aggregate_power_entity": battery},
        ),
    )
    assert frame["result"]["ok"] is True
    return frame["result"]


async def _conflict(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "conflict")
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id, scope="site", expected={"main_fuse_a": 16}, changes={"main_fuse_a": 32}
        ),
    )
    assert frame["result"]["error"] == "spotnav_conflict"
    return frame["result"]


async def _field_errors_charger(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "err_charger")
    await setup_charger_and_site(hass, "err_other", site=False)
    sensor = register(hass, "sensor", "err_sensor")
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id,
            scope="charger",
            changes={
                "charge_control": sensor,
                "current_limit": "number.ghost",
                "vehicle_soc": sensor,
            },
        ),
    )
    assert frame["result"]["error"] == "spotnav_invalid_value"
    return frame["result"]


async def _field_errors_in_use(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "use_charger")
    await setup_charger_and_site(hass, "use_other", site=False)
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id, scope="charger", changes={"charge_control": "switch.use_other_control"}
        ),
    )
    assert frame["result"]["field_errors"][0]["code"] == "charge_control_in_use"
    return frame["result"]


async def _field_errors_site(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "err_site")
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id,
            scope="site",
            changes={
                "main_fuse_a": 0,
                "measurement_mode": "bogus",
                "direct_L1": "sensor.ghost",
                "direct_L2": "",
                "battery_aggregate_power_entity": "switch.err_site_control",
            },
        ),
    )
    assert frame["result"]["error"] == "spotnav_invalid_value"
    return frame["result"]


async def _derived_requirements(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "req_derived")
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(
            charger.entry_id, scope="site", changes={"measurement_mode": MEASUREMENT_MODE_DERIVED}
        ),
    )
    assert len(frame["result"]["field_errors"]) == 6
    return frame["result"]


async def _no_site(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "none_site", site=False)
    frame = await ws_call(
        await admin(hass, ws),
        update_entity_config_message(charger.entry_id, scope="site", changes={"main_fuse_a": 32}),
    )
    assert frame["result"]["error"] == "spotnav_no_site"
    return frame["result"]


async def _not_admin(hass, ws, token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "not_admin")
    frame = await ws_call(
        await non_admin(hass, ws, token),
        update_entity_config_message(charger.entry_id, scope="site", changes={"main_fuse_a": 32}),
    )
    assert frame["result"]["error"] == "spotnav_not_admin"
    return frame["result"]


def _stable_device_id(payload: Any, device_id: str, stable: str) -> Any:
    """The registry's device ids are random per run; a committed fixture names one stably."""
    text = json.dumps(payload)
    assert device_id in text
    return json.loads(text.replace(device_id, stable))


async def _vehicle_get(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "veh_get")
    device_id, _soc, _health = add_ambiguous_vehicle_device(hass, unique_id="car-a", name="Volvo")
    frame = await ws_call(await admin(hass, ws), get_message(charger.entry_id))
    return _stable_device_id(frame["result"], device_id, "vehicle_a")


async def _vehicle_confirmed(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "veh_ok")
    device_id, soc, _health = add_ambiguous_vehicle_device(hass, unique_id="car-a", name="Volvo")
    frame = await ws_call(
        await admin(hass, ws), set_message(charger.entry_id, device_id, soc)
    )
    assert frame["result"]["ok"] is True
    return _stable_device_id(frame["result"], device_id, "vehicle_a")


async def _vehicle_cleared(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "veh_clear")
    device_id, soc, _health = add_ambiguous_vehicle_device(hass, unique_id="car-a", name="Volvo")
    client = await admin(hass, ws)
    await ws_call(client, set_message(charger.entry_id, device_id, soc))
    frame = await ws_call(client, set_message(charger.entry_id, device_id, None))
    assert frame["result"]["ok"] is True
    return _stable_device_id(frame["result"], device_id, "vehicle_a")


async def _vehicle_refused(hass, ws, _token) -> dict[str, Any]:
    charger, _ = await setup_charger_and_site(hass, "veh_bad")
    device_id, _soc, _health = add_ambiguous_vehicle_device(hass, unique_id="car-a", name="Volvo")
    frame = await ws_call(
        await admin(hass, ws), set_message(charger.entry_id, device_id, "sensor.ghost")
    )
    assert frame["result"]["field_errors"] == [{"field": "vehicle_soc", "code": "entity_not_found"}]
    return _stable_device_id(frame["result"], device_id, "vehicle_a")


Builder = Callable[..., Awaitable[dict[str, Any]]]

ENTITY_CONFIG_V1_FIXTURES: Final[dict[str, Builder]] = {
    "get_direct.json": _get_direct,
    "get_direct_total.json": _get_direct_total,
    "get_derived.json": _get_derived,
    "get_detected.json": _get_detected,
    "get_no_site.json": _get_no_site,
    "success_charger.json": _success_charger,
    "success_current_limit_none.json": _success_current_limit_none,
    "success_site.json": _success_site,
    "conflict.json": _conflict,
    "field_errors_charger.json": _field_errors_charger,
    "field_errors_in_use.json": _field_errors_in_use,
    "field_errors_site.json": _field_errors_site,
    "derived_requirements.json": _derived_requirements,
    "no_site.json": _no_site,
    "not_admin.json": _not_admin,
    "vehicle_soc_get.json": _vehicle_get,
    "vehicle_soc_confirmed.json": _vehicle_confirmed,
    "vehicle_soc_cleared.json": _vehicle_cleared,
    "vehicle_soc_refused.json": _vehicle_refused,
}


async def test_the_committed_entity_config_v1_fixtures_are_the_commands_own_output(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    """Every entity-config v1 fixture equals what the real commands produce, or this fails."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    produced = {
        name: await builder(hass, hass_ws_client, hass_read_only_access_token)
        for name, builder in ENTITY_CONFIG_V1_FIXTURES.items()
    }
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        for name, payload in produced.items():
            (FIXTURE_DIR / name).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return
    committed = sorted(path.name for path in FIXTURE_DIR.glob("*.json"))
    assert committed == sorted(ENTITY_CONFIG_V1_FIXTURES)
    for name, payload in produced.items():
        stored = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
        assert stored == payload, f"{name} no longer matches the command's own output"
