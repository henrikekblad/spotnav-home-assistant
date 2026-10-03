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

    assert bundle["bundle_version"] == 1
    for key in ("versions", "related_integrations", "price_data", "sites", "chargers", "log"):
        assert key in bundle
    versions = bundle["versions"]
    assert {"spotnav", "card_bundle_hash", "home_assistant", "python", "installation_type"} <= set(versions)
    assert versions["spotnav"]
    assert [s["entry_id"] for s in bundle["sites"]] == [site.entry_id]
    assert bundle["sites"][0]["result"] is not None
    assert "capability" in bundle["sites"][0]
    section = bundle["chargers"][0]
    assert section["entry_id"] == charger.entry_id
    assert section["diagnostics"]["entry_type"] == "charger"
    assert "dashboard" in section and "status" in section and "plan_and_auto" in section
    assert "command_log" in section
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
