"""The integration-wide services and the WebSocket read of what is still unresolved.

Registered once by `async_setup`, never per entry. Only the decision store is touched: no
charger, schedule, entity or other service call is made.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .repairs import (
    async_clear_vehicle_soc,
    async_record_vehicle_soc,
    async_sync_resolution_repairs,
)
from .runtime import charger_data, domain_data
from .vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
    DiscoveryDecisionStore,
)
from .vehicles.resolution import resolve_required
from .vehicles.vehicle_discovery import valid_charge_limit_entity_id, valid_soc_entity_id


_LOGGER = logging.getLogger(__name__)


def async_register_services(hass: HomeAssistant) -> None:
    """Register the dismissal and confirmation services and the resolution read, once for the domain.

    `unconfirm_vehicle` makes decisions symmetric: every recorded decision can be undone. Guarded by
    `has_service` so a repeat call is safe.
    """
    async def async_dismiss_vehicle(call: ServiceCall) -> None:
        await _async_set_dismissed(hass, call, dismissed=True)

    async def async_undismiss_vehicle(call: ServiceCall) -> None:
        await _async_set_dismissed(hass, call, dismissed=False)

    async def async_unconfirm_vehicle(call: ServiceCall) -> None:
        await _async_unconfirm_vehicle(hass, call)

    async def async_confirm_charge_limit(call: ServiceCall) -> None:
        await _async_confirm_charge_limit(hass, call)

    async def async_unconfirm_charge_limit(call: ServiceCall) -> None:
        await _async_unconfirm_charge_limit(hass, call)

    async def async_confirm_vehicle_soc(call: ServiceCall) -> None:
        await _async_confirm_vehicle_soc(hass, call)

    # Decisions recorded from an id alone; the two that name an entity are registered below.
    device_schema = vol.Schema({vol.Required("device_id"): cv.string})
    for service, handler in (
        ("dismiss_vehicle", async_dismiss_vehicle),
        ("undismiss_vehicle", async_undismiss_vehicle),
        ("unconfirm_vehicle", async_unconfirm_vehicle),
        ("unconfirm_charge_limit", async_unconfirm_charge_limit),
    ):
        if hass.services.has_service(DOMAIN, service):
            continue
        hass.services.async_register(DOMAIN, service, handler, schema=device_schema)

    decision_schema = vol.Schema(
        {
            vol.Required("device_id"): cv.string,
            vol.Required("entity_id"): cv.string,
        }
    )
    for service, handler in (
        ("confirm_charge_limit", async_confirm_charge_limit),
        ("confirm_vehicle_soc", async_confirm_vehicle_soc),
    ):
        if hass.services.has_service(DOMAIN, service):
            continue
        hass.services.async_register(DOMAIN, service, handler, schema=decision_schema)

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_TEST_NOTIFICATION):

        async def async_send_test_notification(call: ServiceCall) -> ServiceResponse:
            return await _async_send_test_notification(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_TEST_NOTIFICATION,
            async_send_test_notification,
            schema=vol.Schema({vol.Optional("charger"): cv.string}),
            supports_response=SupportsResponse.OPTIONAL,
        )

    websocket_api.async_register_command(hass, _async_ws_resolve_required)


#: A field test without unplugging the car: a test push to the paired app, a test message to the
#: chosen Companion phones. In Developer tools only, never in the card.
SERVICE_SEND_TEST_NOTIFICATION = "send_test_notification"


def _test_chargers(hass: HomeAssistant, charger: str | None) -> list[Any]:
    """The loaded charger entries a test goes to: all, or the one named by entry id or by one of its
    entities."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE and charger_data(hass, entry.entry_id) is not None
    ]
    if charger is None:
        return entries
    registered = er.async_get(hass).async_get(charger)
    entry_id = registered.config_entry_id if registered is not None else charger
    chosen = [entry for entry in entries if entry.entry_id == entry_id]
    if not chosen:
        raise ServiceValidationError(
            "No loaded SpotNav charger by that entry or entity id",
            translation_domain=DOMAIN,
            translation_key="unknown_test_charger",
        )
    return chosen


async def _async_send_test_notification(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Send every chosen charger's paired app a test push and its chosen phones a test message.

    Each charger's result is logged (never the relay ref) and answered; with no app registered on any of
    them it is refused after the phones were told.
    """
    results: dict[str, Any] = {}
    registered = False
    for entry in _test_chargers(hass, call.data.get("charger")):
        data = charger_data(hass, entry.entry_id)
        push = None if data is None else data.push
        notifier = None if data is None else data.notifier
        result = None if push is None else await push.async_send_test()
        notified = [] if notifier is None else await notifier.async_send_test()
        registered = registered or result is not None
        _LOGGER.info(
            "SpotNav test notification for %s: app push %s, Companion phones %s",
            entry.title,
            "not registered" if result is None else result,
            ", ".join(notified) or "none",
        )
        results[entry.entry_id] = {"name": entry.title, "push": result, "notified": notified}
    if not registered:
        raise ServiceValidationError(
            "No SpotNav charger has the app's instant notifications registered",
            translation_domain=DOMAIN,
            translation_key="no_push_registered",
        )
    return {"chargers": results}


@websocket_api.websocket_command({vol.Required("type"): "spotnav/resolve_required"})
@websocket_api.async_response
async def _async_ws_resolve_required(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer with every decision a human has still to make (see vehicles/resolution.py).

    One list of `{kind, device_id, name, candidate_entity_ids}` rows, sorted and read fresh on every
    call. `kind` names the resolving service: `soc` for `confirm_vehicle_soc`, `charge_limit` for
    `confirm_charge_limit`.
    """
    connection.send_result(
        msg["id"],
        {"decisions": [decision.as_dict() for decision in resolve_required(hass)]},
    )


def _decision_store_or_warn(
    hass: HomeAssistant, call: ServiceCall
) -> DiscoveryDecisionStore | None:
    """The integration-wide decision store, or `None` after a warning (defensive; setup runs first)."""
    store = domain_data(hass).decision_store
    if store is None:
        _LOGGER.warning(
            "SpotNav discovery decision store is not set up; ignoring %s", call.service
        )
    return store


async def _async_set_dismissed(hass: HomeAssistant, call: ServiceCall, *, dismissed: bool) -> None:
    """Record one dismissal decision, whatever the device id currently is.

    The id need not resolve to a real device: a dismissal may arrive before the vehicle's integration.
    """
    store = _decision_store_or_warn(hass, call)
    if store is None:
        return
    device_id = str(call.data["device_id"])
    if dismissed:
        await store.async_dismiss(DECISION_DOMAIN_VEHICLE, device_id)
    else:
        await store.async_undismiss(DECISION_DOMAIN_VEHICLE, device_id)
    # Keep the Repairs issues in step with the store.
    await async_sync_resolution_repairs(hass)


async def _async_unconfirm_vehicle(hass: HomeAssistant, call: ServiceCall) -> None:
    """Forget one confirmation decision, whatever the device id currently is.

    An id with nothing to forget is not an error. Dismissals are untouched, so this only makes a
    device ambiguous again.
    """
    store = _decision_store_or_warn(hass, call)
    if store is None:
        return
    await async_clear_vehicle_soc(hass, store, str(call.data["device_id"]))


async def _async_confirm_charge_limit(hass: HomeAssistant, call: ServiceCall) -> None:
    """Record which of a device's charge-limit entities a write should use.

    The AC/DC choice is a human's, only recorded here; the integration never labels a limit by name
    or value. A vehicle with one limit needs none of this. The entity must be one of the device's
    live charge-limit candidates right now (the same check the write resolves through); anything
    else is refused with one warning and records nothing.
    """
    store = _decision_store_or_warn(hass, call)
    if store is None:
        return
    device_id = str(call.data["device_id"])
    entity_id = str(call.data["entity_id"])
    if not valid_charge_limit_entity_id(hass, device_id, entity_id):
        # One refusal naming nothing, so the service cannot be used to list a device's entities.
        _LOGGER.warning(
            "Ignoring a charge-limit confirmation that is not a charge limit of that vehicle"
        )
        return
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
        device_id,
        {"charge_limit_entity_id": entity_id},
    )
    await async_sync_resolution_repairs(hass)


async def _async_unconfirm_charge_limit(hass: HomeAssistant, call: ServiceCall) -> None:
    """Forget a charge-limit confirmation, making that choice ambiguous again.

    An id with nothing to forget is not an error; dismissals and the state-of-charge confirmation
    are untouched.
    """
    store = _decision_store_or_warn(hass, call)
    if store is None:
        return
    await store.async_unconfirm(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, str(call.data["device_id"])
    )
    await async_sync_resolution_repairs(hass)


async def _async_confirm_vehicle_soc(hass: HomeAssistant, call: ServiceCall) -> None:
    """Record which of a device's battery sensors is its state of charge.

    Same idea as `_async_confirm_charge_limit`, for the reading everything else is built on. Until
    recorded, a device with two percent-shaped sensors is reported as one to decide about (see
    `vehicles/resolution.py`). The entity must be one of the device's current ambiguous candidates;
    anything else records nothing, with one warning that names nothing.
    """
    store = _decision_store_or_warn(hass, call)
    if store is None:
        return
    device_id = str(call.data["device_id"])
    entity_id = str(call.data["entity_id"])
    if not valid_soc_entity_id(hass, device_id, entity_id):
        # One refusal naming nothing, so the service cannot be used to list a device's entities.
        _LOGGER.warning(
            "Ignoring a state-of-charge confirmation that is not a candidate of that vehicle"
        )
        return
    await async_record_vehicle_soc(hass, store, device_id, entity_id)
