"""The debug bundle: its shape, the log ring buffer, redaction and the admin-only command."""

from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

from custom_components.spotnav.debug_bundle import (
    async_build_debug_bundle,
    redact_bundle,
    REDACTED,
    Scrubber,
)
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.log_buffer import (
    attach_log_buffer,
    MAX_DEBUG_RECORDS,
    MAX_RECORDS,
    SPOTNAV_LOGGER,
)
from custom_components.spotnav.runtime import domain_data

from .world import admin, non_admin, setup_charger_and_site, ws_call


async def test_bundle_has_every_section(hass: HomeAssistant) -> None:
    charger, site = await setup_charger_and_site(hass)
    bundle = await async_build_debug_bundle(hass)

    assert bundle["bundle_version"] == 3
    for key in ("versions", "related_integrations", "price_data", "sites", "chargers", "log"):
        assert key in bundle
    versions = bundle["versions"]
    assert {"spotnav", "card_bundle_hash", "home_assistant", "python", "installation_type"} <= set(versions)
    assert versions["spotnav"]
    assert [s["entry_id"] for s in bundle["sites"]] == [site.entry_id]
    assert bundle["sites"][0]["diagnostics"]["result"] is not None
    assert "capability" in bundle["sites"][0]["diagnostics"]
    section = bundle["chargers"][0]
    assert section["entry_id"] == charger.entry_id
    assert section["diagnostics"]["entry_type"] == "charger"
    assert "dashboard" in section and "status" in section
    assert "plan" in section["dashboard"] and "strategy" in section["dashboard"]
    assert "commands" in section["diagnostics"]["controller"]["adapter"]
    # Each fact once: the price data at the top only, the rest inside the entry's diagnostics or dashboard.
    for gone in ("plan_and_auto", "command_log"):
        assert gone not in section
    for gone in ("result", "capability", "price_data"):
        assert gone not in bundle["sites"][0]
    assert "price_data" not in section["diagnostics"] and "price_data" not in bundle["sites"][0]["diagnostics"]
    assert "intervals" not in section["dashboard"]["prices"]
    json.dumps(bundle)


async def test_site_entry_diagnostics_carry_the_bundle(hass: HomeAssistant) -> None:
    charger, site = await setup_charger_and_site(hass)

    site_dump = await async_get_config_entry_diagnostics(hass, site)
    charger_dump = await async_get_config_entry_diagnostics(hass, charger)

    assert site_dump["debug_bundle"]["chargers"][0]["entry_id"] == charger.entry_id
    assert "debug_bundle" not in charger_dump
    assert "debug_bundle" not in site_dump["debug_bundle"]["sites"][0]["diagnostics"]


async def test_a_charger_without_a_site_carries_the_bundle(hass: HomeAssistant) -> None:
    charger, _ = await setup_charger_and_site(hass, site=False)
    dump = await async_get_config_entry_diagnostics(hass, charger)
    assert dump["debug_bundle"]["sites"] == []


def test_ring_buffer_is_bounded_and_split_by_level() -> None:
    buffer = attach_log_buffer()
    logger = logging.getLogger(f"{SPOTNAV_LOGGER}.test_ring")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        for index in range(MAX_RECORDS + 50):
            logger.info("info %s", index)
        for index in range(MAX_DEBUG_RECORDS + 20):
            logger.debug("debug %s", index)
        logger.warning("careful")
        records = buffer.records()
        debug = buffer.debug_records()
    finally:
        logger.setLevel(previous)
        logging.getLogger(SPOTNAV_LOGGER).removeHandler(buffer)

    assert len(records) == MAX_RECORDS
    assert records[-1]["message"] == "careful" and records[-1]["level"] == "WARNING"
    assert records[0]["message"] == "info 51"  # 300 kept of 351: the oldest dropped
    assert len(debug) == MAX_DEBUG_RECORDS
    assert all(r["level"] == "DEBUG" for r in debug)
    assert debug[-1]["message"] == f"debug {MAX_DEBUG_RECORDS + 19}"


def test_ring_buffer_takes_only_spotnav_loggers_and_leaves_levels_alone() -> None:
    buffer = attach_log_buffer()
    spotnav = logging.getLogger(SPOTNAV_LOGGER)
    level_before = spotnav.level
    try:
        logging.getLogger("some.other.integration").warning("not ours")
        logging.getLogger(f"{SPOTNAV_LOGGER}.execution").error("ours")
        names = [r["logger"] for r in buffer.records()]
        assert spotnav.level == level_before
    finally:
        spotnav.removeHandler(buffer)
    assert names == [f"{SPOTNAV_LOGGER}.execution"]


def test_attaching_again_replaces_the_buffer() -> None:
    first = attach_log_buffer()
    second = attach_log_buffer()
    try:
        handlers = [h for h in logging.getLogger(SPOTNAV_LOGGER).handlers if type(h) is type(first)]
        assert handlers == [second]
    finally:
        logging.getLogger(SPOTNAV_LOGGER).removeHandler(second)


def test_redaction_by_key_and_by_text() -> None:
    scrubber = Scrubber(secrets=["super-secret-hook"], names=["Alice Example"], places=[59.329323, 18.068581])
    raw = {
        "webhook_id": "super-secret-hook",
        "nested": {"access_token": "abc", "latitude": 59.329323, "longitude": 18.068581, "ok": 1},
        "text": "POST /api/webhook/super-secret-hook by Alice Example at 59.329323 Authorization: Bearer abc.def",
        "entity": "sensor.kept_entity_id",
        "list": [{"password": "pw"}],
    }
    out = redact_bundle(raw, scrubber)
    assert out["webhook_id"] == REDACTED
    assert out["nested"] == {"access_token": REDACTED, "latitude": 59.3, "longitude": 18.1, "ok": 1}
    assert "super-secret-hook" not in out["text"] and "Alice" not in out["text"]
    assert "59.329323" not in out["text"] and "abc.def" not in out["text"]
    assert out["entity"] == "sensor.kept_entity_id"
    assert out["list"] == [{"password": REDACTED}]


async def test_known_secrets_never_appear_in_the_bundle(hass: HomeAssistant) -> None:
    hass.config.latitude = 59.329323
    hass.config.longitude = 18.068581
    charger, site = await setup_charger_and_site(hass)
    secret = "webhook-" + charger.entry_id
    hass.config_entries.async_update_entry(
        charger, data={**charger.data, "pairing_secret": "pair-secret-xyz", "access_token": "tok-123"}
    )
    user = await hass.auth.async_create_user("Zebediah Quux")
    buffer = attach_log_buffer()
    domain_data(hass).log_buffer = buffer
    try:
        logging.getLogger(f"{SPOTNAV_LOGGER}.test_secret").warning(
            "calling /api/webhook/%s for Zebediah Quux with Bearer tok-123 at %s", secret, hass.config.latitude
        )
        bundle = await async_build_debug_bundle(hass)
        dump = await async_get_config_entry_diagnostics(hass, site)
    finally:
        logging.getLogger(SPOTNAV_LOGGER).removeHandler(buffer)

    for text in (json.dumps(bundle), json.dumps(dump)):
        for forbidden in (secret, "pair-secret-xyz", "tok-123", "Zebediah", "59.329323", "18.068581"):
            assert forbidden not in text, forbidden
    assert user.name == "Zebediah Quux"
    # Entity ids are kept.
    assert charger.data["charge_control"] in json.dumps(bundle)
    assert any("test_secret" in r["logger"] for r in bundle["log"]["records"])


async def test_command_returns_the_bundle_to_an_admin(hass: HomeAssistant, hass_ws_client) -> None:
    charger, _ = await setup_charger_and_site(hass)
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(client, {"type": "spotnav/get_debug_bundle", "api_version": 1})

    assert frame["success"] is True
    result = frame["result"]
    assert result["ok"] is True and result["error"] is None
    assert result["bundle"]["chargers"][0]["entry_id"] == charger.entry_id


async def test_command_refuses_a_non_admin_and_another_version(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    await setup_charger_and_site(hass)

    denied = await ws_call(
        await non_admin(hass, hass_ws_client, hass_read_only_access_token),
        {"type": "spotnav/get_debug_bundle", "api_version": 1},
    )
    assert denied["result"] == {
        "api_version": 1, "ok": False, "error": "spotnav_not_admin", "bundle": None,
    }

    wrong = await ws_call(await admin(hass, hass_ws_client), {"type": "spotnav/get_debug_bundle", "api_version": 9})
    assert wrong["success"] is False
    assert wrong["error"]["code"] == "spotnav_unsupported_api_version"


async def test_bundle_states_every_measurement_entity_as_home_assistant_holds_it(hass: HomeAssistant) -> None:
    """Every entity the site's measurement reads is in the bundle with its raw state, unit, classes,
    attribute names (values only of the attributes read), the three timestamps and its integration."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "tibber", "home1_rt_currentL1", suggested_object_id="pulse_l1")
    for phase, value in (("1", "4.467"), ("2", "-2.445"), ("3", "-2.756")):
        hass.states.async_set(
            f"sensor.pulse_l{phase}",
            value,
            {"unit_of_measurement": "A", "device_class": "current", "state_class": "measurement", "friendly_name": f"L{phase}"},
        )
    hass.states.async_set(
        "sensor.eq_current",
        "11.4",
        {"state_currentL1": 11.4, "state_currentL2": -4.1, "state_currentL3": -4.9, "access_token": "tok-meter-1"},
    )
    hass.states.async_set("sensor.eq_import", "0.5", {"unit_of_measurement": "kW", "device_class": "power"})
    hass.states.async_set("sensor.eq_export", "1.2", {"unit_of_measurement": "kW", "device_class": "power"})
    charger, site = await setup_charger_and_site(
        hass,
        direct_entities={"L1": "sensor.pulse_l1", "L2": "sensor.pulse_l2", "L3": "sensor.pulse_l3"},
        phase_wiring={
            "entry_a": {
                "phases": 3,
                "measured_current_source": {
                    "kind": "attributes",
                    "entity_id": "sensor.eq_current",
                    "attributes": {"L1": "state_currentL1", "L2": "state_currentL2", "L3": "state_currentL3"},
                    "attribute_unit_override": "A",
                    "trust_entity_unit_for_attributes": False,
                },
            }
        },
        extra_data={"grid_power_source": {"power": "sensor.eq_import", "power_export": "sensor.eq_export"}},
    )

    bundle = await async_build_debug_bundle(hass)
    entities = bundle["sites"][0]["measurement_entities"]

    assert set(entities) == {
        "sensor.pulse_l1",
        "sensor.pulse_l2",
        "sensor.pulse_l3",
        "sensor.eq_current",
        "sensor.eq_import",
        "sensor.eq_export",
    }
    l2 = entities["sensor.pulse_l2"]
    assert l2["roles"] == ["direct_L2"]
    assert l2["state"] == "-2.445"
    assert (l2["unit"], l2["device_class"], l2["state_class"]) == ("A", "current", "measurement")
    assert l2["attribute_names"] == ["device_class", "friendly_name", "state_class", "unit_of_measurement"]
    assert l2["attributes_read"] == {}
    state = hass.states.get("sensor.pulse_l2")
    assert l2["last_changed"] == state.last_changed.isoformat()
    assert l2["last_reported"] == state.last_reported.isoformat()
    assert l2["last_updated"] == state.last_updated.isoformat()
    assert entities["sensor.pulse_l1"]["platform"] == "tibber"
    assert entities["sensor.pulse_l3"]["platform"] is None
    assert entities["sensor.eq_import"]["roles"] == ["grid_power"]
    assert entities["sensor.eq_export"]["roles"] == ["grid_power_export"]
    equalizer = entities["sensor.eq_current"]
    assert equalizer["roles"] == ["charger_measured_current:entry_a"]
    assert equalizer["attributes_read"] == {"state_currentL1": 11.4, "state_currentL2": -4.1, "state_currentL3": -4.9}
    # A secret-looking attribute is named, never valued.
    assert "access_token" in equalizer["attribute_names"]
    assert "tok-meter-1" not in json.dumps(bundle)


async def test_a_secret_in_a_read_attribute_is_redacted_by_key(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.odd", "1", {"token": "tok-in-attribute"})
    charger, site = await setup_charger_and_site(
        hass,
        site_current_source={
            "kind": "attributes",
            "entity_id": "sensor.odd",
            "attributes": {"L1": "token", "L2": "token", "L3": "token"},
            "attribute_unit_override": "A",
            "trust_entity_unit_for_attributes": False,
        },
    )

    bundle = await async_build_debug_bundle(hass)

    item = bundle["sites"][0]["measurement_entities"]["sensor.odd"]
    assert item["roles"] == ["site_current_source"]
    assert item["attributes_read"] == {"token": REDACTED}
    assert "tok-in-attribute" not in json.dumps(bundle)
