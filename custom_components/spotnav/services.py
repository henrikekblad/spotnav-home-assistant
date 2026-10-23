"""The integration-wide services and the WebSocket read of what is still unresolved.

Registered once by `async_setup`, never per entry. Only the decision store is touched: no
charger, schedule, entity or other service call is made.

The rule is that a service which changes configuration, or acts on the household, is an admin service: a
call from a user who is not an administrator is refused (`Unauthorized`). The six decision services
change the integration's configuration, the same rule as the card's `spotnav/choose_vehicle_soc`;
`send_test_notification` messages the household's phones and names the chargers, an administrator's
field test. A call with no user behind it (an automation fired by a trigger) is allowed. Nothing inside
SpotNav calls them: the Repairs flow, the config flow, the card's WebSocket commands and the phones'
notification answers reach the decision store without a service call.

`get_prices` only reads (an electricity price entry's prices, which every user's dashboard can show
anyway) and changes nothing, so it is open to every user. It answers and does nothing else
(`SupportsResponse.ONLY`).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from . import time_zones
from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .repairs import (
    async_clear_vehicle_soc,
    async_record_vehicle_soc,
    async_sync_resolution_repairs,
)
from .pricing.price_points import resolution_minutes, rounded
from .runtime import charger_data, domain_data, price_data, PriceData
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

    `unconfirm_vehicle` makes decisions symmetric: every recorded decision can be undone. The decision
    services and the test notification are admin-only (see the module docstring). Guarded by `has_service` so a repeat call is safe.
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
        async_register_admin_service(hass, DOMAIN, service, handler, schema=device_schema)

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
        async_register_admin_service(hass, DOMAIN, service, handler, schema=decision_schema)

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_TEST_NOTIFICATION):

        async def async_send_test_notification(call: ServiceCall) -> ServiceResponse:
            return await _async_send_test_notification(hass, call)

        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_SEND_TEST_NOTIFICATION,
            async_send_test_notification,
            schema=vol.Schema({vol.Optional("charger"): cv.string}),
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GET_PRICES):

        async def async_get_prices(call: ServiceCall) -> ServiceResponse:
            return _get_prices(hass, call)

        # Read-only, so not an admin service (see the module docstring).
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_PRICES,
            async_get_prices,
            schema=GET_PRICES_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    websocket_api.async_register_command(hass, _async_ws_resolve_required)


#: An electricity price entry's prices with fees, for an automation or a script to work with.
SERVICE_GET_PRICES = "get_prices"

GET_PRICES_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("device_id"): cv.string,
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
    }
)


def _price_refusal(key: str, message: str) -> ServiceValidationError:
    return ServiceValidationError(message, translation_domain=DOMAIN, translation_key=key)


def _price_entry(hass: HomeAssistant, call: ServiceCall) -> tuple[str, PriceData]:
    """The electricity price entry a call names by entry id or device id, or the only one there is."""
    entry_id = call.data.get("config_entry_id")
    device_id = call.data.get("device_id")
    candidates: list[str]
    if entry_id:
        candidates = [entry_id]
    elif device_id:
        device = dr.async_get(hass).async_get(device_id)
        candidates = [] if device is None else sorted(device.config_entries)
    else:
        candidates = [entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)]
    found = [(candidate, data) for candidate in candidates if (data := price_data(hass, candidate)) is not None]
    if len(found) == 1:
        return found[0]
    if not found:
        raise _price_refusal("price_entry_unknown", "No loaded SpotNav electricity price by that id")
    raise _price_refusal("price_entry_required", "Name the electricity price: there is more than one")


def _instant(value: Any) -> Any:
    """An aware instant in UTC; a time without a zone is Home Assistant's own local time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(value)


def _get_prices(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """The prices of one electricity price entry, modelled on Home Assistant's `nordpool.get_prices`:
    the area's list of `{start, end, price, spot_price}` (major unit per kWh, `price` with fees and VAT)
    under the area's id, beside the currency, unit and resolution. Default span: today, and tomorrow once
    published. Only what the installation holds is read; nothing is fetched."""
    entry_id, data = _price_entry(hass, call)
    device = data.device
    area = device.area
    span = device.default_span()
    if area is None or span is None or device.fiscal is None:
        raise _price_refusal("price_unavailable", "This electricity price has no prices yet")
    start = _instant(call.data["start"]) if "start" in call.data else span[0]
    end = _instant(call.data["end"]) if "end" in call.data else span[1]
    if end <= start:
        raise _price_refusal("price_range_invalid", "The end must be after the start")
    repository = domain_data(hass).price_repository
    points = () if repository is None else device.points_between(start, end, repository)
    zone = time_zones.zone(area.tz) or dt_util.UTC
    return {
        "config_entry_id": entry_id,
        "area": area.id,
        "currency": area.currency,
        "unit": f"{area.major_unit}/kWh",
        "resolution_minutes": resolution_minutes(points),
        "tomorrow_published": device.tomorrow_published,
        area.id: [
            {
                "start": point.start.astimezone(zone).isoformat(),
                "end": point.end.astimezone(zone).isoformat(),
                "price": rounded(point.price),
                "spot_price": rounded(point.spot),
            }
            for point in points
        ],
    }


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
