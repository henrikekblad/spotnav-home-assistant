"""A charger's own measured-current sensors, offered for its site wiring (`site/charger_current_offer.py`):
found in the registry even while their integration ships them disabled, applied with the site editor's
`apply_detection` (which enables what the integration disabled, never what a person did), and named in the
charger editor's line about its measured current."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import CONF_MEASURED_CURRENT_SOURCE, CONF_PHASE_WIRING
from custom_components.spotnav.execution.charger_profiles import _PROFILES, profile_for
from custom_components.spotnav.site.charger_current_offer import own_current_sensors, sensors_text
from custom_components.spotnav.site.site_join import MEASURED_SOURCE_DECLINED

from .charger_shapes import _e, AMPS, register_shape, Shape, SHAPES
from .helpers import make_entry, make_site_entry
from .messages import get_message, update_entity_config_message
from .world import admin, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

PEBLAR_PHASES = ["sensor.peblar_current_phase_1", "sensor.peblar_current_phase_2", "sensor.peblar_current_phase_3"]


def _name_phases(hass: HomeAssistant) -> None:
    """The names the Peblar integration gives its phase sensors (the shapes register none)."""
    registry = er.async_get(hass)
    for number, entity_id in enumerate(PEBLAR_PHASES, start=1):
        registry.async_update_entity(entity_id, original_name=f"Current phase {number}")


async def _charger_in_site(hass: HomeAssistant, platform: str, charge_control: str, *, wiring: dict | None = None):
    register_shape(hass, SHAPES[platform])
    charger = make_entry(
        hass,
        entry_id="entry_a",
        charge_control=charge_control,
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    site = make_site_entry(
        hass, entry_id="site_a", charger_entry_ids=["entry_a"], phase_wiring={"entry_a": wiring or {"phases": 3}}
    )
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    return charger, site


def _site_block(result: dict) -> dict:
    return result["result"]["config"]["site"]


def _source_field(result: dict) -> dict:
    return next(f for f in result["result"]["config"]["fields"] if f["field"] == "measured_current_source")


async def test_a_peblar_with_disabled_phase_sensors_is_offered_them(hass: HomeAssistant, hass_ws_client) -> None:
    _charger, _site = await _charger_in_site(hass, "peblar", "switch.peblar_charge")
    _name_phases(hass)
    client = await admin(hass, hass_ws_client)

    result = await ws_call(client, get_message("entry_a"))

    [offer] = _site_block(result)["detection"]["chargers"]
    assert offer == {
        "id": "charger_current:entry_a",
        "charger_name": "Garage",
        "integration": "Peblar",
        "sensors": "Current phase 1–3",
        "entity_ids": PEBLAR_PHASES,
        "disabled_entities": PEBLAR_PHASES,
    }
    field = _source_field(result)
    assert field["value"] is None
    assert field["own_sensors"] == {"integration": "Peblar", "sensors": "Current phase 1–3"}


async def test_use_enables_the_sensors_and_wires_them(hass: HomeAssistant, hass_ws_client) -> None:
    _charger, site = await _charger_in_site(hass, "peblar", "switch.peblar_charge")
    hass.config_entries.async_update_entry(
        site, data={**site.data, CONF_PHASE_WIRING: {"entry_a": {"phases": 3, MEASURED_SOURCE_DECLINED: True}}}
    )
    client = await admin(hass, hass_ws_client)

    result = await ws_call(
        client,
        update_entity_config_message("entry_a", scope="site", changes={"apply_detection": "charger_current:entry_a"}),
    )

    assert result["result"]["ok"] is True, result
    wiring = hass.config_entries.async_get_entry("site_a").data[CONF_PHASE_WIRING]["entry_a"]
    assert wiring[CONF_MEASURED_CURRENT_SOURCE]["kind"] == "separate_entities"
    assert wiring[CONF_MEASURED_CURRENT_SOURCE]["entity_ids"] == dict(zip(("L1", "L2", "L3"), PEBLAR_PHASES))
    assert MEASURED_SOURCE_DECLINED not in wiring
    registry = er.async_get(hass)
    assert all(registry.async_get(entity_id).disabled_by is None for entity_id in PEBLAR_PHASES)
    # Wired now: nothing more to offer, and the charger editor names the source.
    assert _site_block(result)["detection"]["chargers"] == []
    after = await ws_call(client, get_message("entry_a"))
    assert _source_field(after)["value"] == ", ".join(PEBLAR_PHASES)
    assert _source_field(after)["own_sensors"] is None


async def test_a_sensor_a_person_disabled_is_never_offered_or_enabled(hass: HomeAssistant, hass_ws_client) -> None:
    await _charger_in_site(hass, "peblar", "switch.peblar_charge")
    registry = er.async_get(hass)
    registry.async_update_entity(PEBLAR_PHASES[1], disabled_by=er.RegistryEntryDisabler.USER)
    client = await admin(hass, hass_ws_client)

    result = await ws_call(client, get_message("entry_a"))
    assert _site_block(result)["detection"]["chargers"] == []
    assert _source_field(result)["own_sensors"] is None

    refused = await ws_call(
        client,
        update_entity_config_message("entry_a", scope="site", changes={"apply_detection": "charger_current:entry_a"}),
    )
    assert refused["result"]["ok"] is False
    assert registry.async_get(PEBLAR_PHASES[1]).disabled_by == er.RegistryEntryDisabler.USER
    assert registry.async_get(PEBLAR_PHASES[0]).disabled_by == er.RegistryEntryDisabler.INTEGRATION


async def test_an_easee_is_offered_its_current_sensors_attributes(hass: HomeAssistant, hass_ws_client) -> None:
    """Easee's `current` sensor (shipped disabled) carries the phases as attributes: the same offer, the same Use."""
    await _charger_in_site(hass, "easee", "sensor.easee_status")
    client = await admin(hass, hass_ws_client)

    [offer] = _site_block(await ws_call(client, get_message("entry_a")))["detection"]["chargers"]
    assert offer["integration"] == "Easee" and offer["disabled_entities"] == ["sensor.easee_current"]

    result = await ws_call(
        client, update_entity_config_message("entry_a", scope="site", changes={"apply_detection": offer["id"]})
    )
    assert result["result"]["ok"] is True, result
    source = hass.config_entries.async_get_entry("site_a").data[CONF_PHASE_WIRING]["entry_a"][
        CONF_MEASURED_CURRENT_SOURCE
    ]
    assert source["kind"] == "attributes" and source["entity_id"] == "sensor.easee_current"
    assert source["attributes"] == {"L1": "state_inCurrentT3", "L2": "state_inCurrentT4", "L3": "state_inCurrentT5"}
    assert er.async_get(hass).async_get("sensor.easee_current").disabled_by is None


async def test_a_one_phase_charger_on_a_chosen_phase_reads_its_first_sensor_there(hass: HomeAssistant) -> None:
    devices = register_shape(hass, SHAPES["peblar"])
    found = own_current_sensors(hass, devices["device_id"], phases=1, phase="L2")
    assert found is not None
    assert dict(found.source.entity_ids or {}) == {"L2": PEBLAR_PHASES[0]}


@pytest.mark.parametrize(
    "platform",
    sorted(
        name
        for name, shape in SHAPES.items()
        if shape.expect.get("current_entities")
        and (profile := profile_for(shape.platform)) is not None
        and profile.current_sensor_keys
    ),
)
async def test_every_profile_that_names_current_sensors_is_offered_them(hass: HomeAssistant, platform: str) -> None:
    shape = SHAPES[platform]
    devices = register_shape(hass, shape)
    found = own_current_sensors(hass, devices["device_id"])
    assert found is not None, platform
    assert list(found.entity_ids) == shape.expect["current_entities"]
    assert found.integration == profile_for(shape.platform).name


@pytest.mark.parametrize(
    "platform", [profile.platform for profile in _PROFILES if len(profile.current_sensor_keys) == 3]
)
async def test_every_per_phase_profile_offers_its_sensors_while_they_are_disabled(
    hass: HomeAssistant, platform: str
) -> None:
    """Each profile's three keys, registered disabled by the integration (no state yet), as a unique-id tail
    (or head, for a key-first integration): all three are found, and all three are to be enabled."""
    profile = profile_for(platform)
    assert profile is not None
    shape = Shape(
        platform,
        "SER1",
        tuple(
            _e("sensor", f"{key}-SER1" if profile.key_first else f"SER1_{key}", key, "9.9", AMPS, disabled=True)
            for key in profile.current_sensor_keys
        ),
        {},
    )
    found = own_current_sensors(hass, register_shape(hass, shape)["device_id"])
    assert found is not None
    assert len(found.entity_ids) == 3 and found.disabled_entity_ids == found.entity_ids
    assert found.source.kind == "separate_entities"


@pytest.mark.parametrize(
    ("names", "text"),
    [
        (("Current phase 1", "Current phase 2", "Current phase 3"), "Current phase 1–3"),
        (("L1 current", "L2 current", "L3 current"), "L1 current, L2 current, L3 current"),
        (("Current",), "Current"),
        (("Current 1", "Current 3", "Current 2"), "Current 1, Current 3, Current 2"),
    ],
)
def test_sensors_are_named_for_a_sentence(names: tuple[str, ...], text: str) -> None:
    assert sensors_text(names) == text
