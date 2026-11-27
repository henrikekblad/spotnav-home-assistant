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

`export_day` writes one site's day record to the configuration folder (`day_export.py`): an administrator's, as
it writes a file; nothing runs unless it is called.

`battery_control_test` and `battery_control_settings` are the owner's, for the on-site tests of the home battery
control (`battery/control/`, plan `v3_battery_control_optin.md`), administrators only and in Developer tools only:
the first runs one action through the battery's executor with every rule for some minutes (refused unless the
site's battery control is turned on; while "shadow only" or before the battery check passed only with `force_test`),
the second writes the site's `battery_control_*` options until the card
has the switch. The card's own command is `spotnav/battery_control` (`api/battery_control.py`).

`show_year_review` works a site's year review out now (`year_review/runtime.py`), offers it in the cards for a week
and answers its figures (so a fresh install can see them in Developer tools); an administrator's, as it changes what
every person's cards show.

`get_prices` only reads (an electricity price entry's prices, which every user's dashboard can show
anyway) and changes nothing, so it is open to every user. It answers and does nothing else
(`SupportsResponse.ONLY`).
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from . import time_zones
from .api.battery_control import async_setup_battery_control_api
from .battery import gate
from .battery.control import TRIAL_MAX_MIN, TrialRefused
from .battery.plan_model import MODES
from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .day_export import archive_path, write_record
from .day_record import day_record
from .repairs import (
    async_clear_vehicle_soc,
    async_record_vehicle_soc,
    async_sync_resolution_repairs,
)
from .pricing.price_points import resolution_minutes, rounded
from .runtime import charger_data, domain_data, price_data, PriceData, site_controller_for, site_data
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

    if not hass.services.has_service(DOMAIN, SERVICE_EXPORT_DAY):

        async def async_export_day(call: ServiceCall) -> ServiceResponse:
            return await _async_export_day(hass, call)

        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_EXPORT_DAY,
            async_export_day,
            schema=EXPORT_DAY_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_BATTERY_CONTROL_TEST):

        async def async_battery_control_test(call: ServiceCall) -> ServiceResponse:
            return await _async_battery_control_test(hass, call)

        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_BATTERY_CONTROL_TEST,
            async_battery_control_test,
            schema=BATTERY_CONTROL_TEST_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_BATTERY_CONTROL_SETTINGS):

        async def async_battery_control_settings(call: ServiceCall) -> ServiceResponse:
            return await _async_battery_control_settings(hass, call)

        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_BATTERY_CONTROL_SETTINGS,
            async_battery_control_settings,
            schema=BATTERY_CONTROL_SETTINGS_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SHOW_YEAR_REVIEW):

        async def async_show_year_review(call: ServiceCall) -> ServiceResponse:
            return await _async_show_year_review(hass, call)

        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_SHOW_YEAR_REVIEW,
            async_show_year_review,
            schema=SHOW_YEAR_REVIEW_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    websocket_api.async_register_command(hass, _async_ws_resolve_required)
    async_setup_battery_control_api(hass)


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
        candidates = [] if device is None else [device.config_entry_id]
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
                # Before SpotNav's fees: the all-in tariff for GB Agile, Spain's PVPC and NL-ZONNEPLAN.
                "spot_price": rounded(point.spot),
            }
            for point in points
        ],
    }


#: One site's local day as SpotNav decided and measured it, written to `<config>/spotnav_archive` (`day_record.py`).
SERVICE_EXPORT_DAY = "export_day"

EXPORT_DAY_SCHEMA = vol.Schema(
    {
        vol.Optional("site"): cv.string,
        vol.Optional("date"): cv.date,
    }
)


def _export_refusal(key: str, message: str) -> ServiceValidationError:
    return ServiceValidationError(message, translation_domain=DOMAIN, translation_key=key)


def _export_site(hass: HomeAssistant, site: str | None) -> Any:
    """The loaded site entry a call names by entry id or by its device id, or the only one there is."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE and site_controller_for(hass, entry.entry_id) is not None
    ]
    if site:
        device = dr.async_get(hass).async_get(site)
        ids = {site} if device is None else set(device.config_entries)
        entries = [entry for entry in entries if entry.entry_id in ids]
        if not entries:
            raise _export_refusal("export_site_unknown", "No loaded SpotNav site by that id")
    if not entries:
        raise _export_refusal("export_no_site", "There is no loaded SpotNav site to export")
    if len(entries) > 1:
        raise _export_refusal("export_site_required", "Name the site: there is more than one")
    return entries[0]


async def _async_export_day(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Write one site's day record (default: yesterday in the site's zone) and answer where and how much."""
    entry = _export_site(hass, call.data.get("site"))
    controller = site_controller_for(hass, entry.entry_id)
    now = dt_util.utcnow()
    tz = controller.demand.tracker.tz
    today = now.astimezone(time_zones.require_zone(tz)).date()
    day = call.data.get("date") or today - timedelta(days=1)
    if day > today:
        raise _export_refusal("export_date_future", "The day has not begun yet")
    integration = await async_get_integration(hass, DOMAIN)
    version = None if integration.version is None else integration.version.string
    record = day_record(hass, entry, controller, day, now, version=version)
    path = archive_path(hass.config.config_dir, entry.entry_id, day.isoformat())
    try:
        size = await hass.async_add_executor_job(write_record, path, record)
    except OSError as err:
        _LOGGER.warning("SpotNav could not write the day record %s: %s", path, err)
        raise HomeAssistantError(
            f"The day record could not be written: {err}",
            translation_domain=DOMAIN,
            translation_key="export_write_failed",
            translation_placeholders={"path": str(path)},
        ) from err
    _LOGGER.info("SpotNav wrote the day record %s (%d bytes)", path, size)
    return {
        "path": str(path),
        "site_id": entry.entry_id,
        "date": day.isoformat(),
        "bytes": size,
        "complete": record["complete"],
        "counts": record["counts"],
        "missing": record["missing"],
    }


#: The owner's on-site test of the home battery control: one action through the executor for some minutes.
SERVICE_BATTERY_CONTROL_TEST = "battery_control_test"
SERVICE_BATTERY_CONTROL_SETTINGS = "battery_control_settings"

BATTERY_CONTROL_TEST_SCHEMA = vol.Schema(
    {
        vol.Optional("site"): cv.string,
        vol.Required("action"): vol.In(
            ["hold", "grid_charge", "discharge_house", "export_discharge", "curtail_export", "stop"]
        ),
        vol.Optional("power"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=100.0)),
        vol.Optional("minutes", default=15): vol.All(vol.Coerce(float), vol.Range(min=1, max=TRIAL_MAX_MIN)),
        vol.Optional("force_test", default=False): cv.boolean,
    }
)

BATTERY_CONTROL_SETTINGS_SCHEMA = vol.Schema(
    {vol.Optional("site"): cv.string} | {vol.Optional(key): cv.boolean for key in gate.DEFAULTS}
    | {vol.Optional(gate.CONF_MODE): vol.In(MODES)}
)


def _battery_refusal(reason: str) -> ServiceValidationError:
    return ServiceValidationError(
        f"The battery control refused: {reason}",
        translation_domain=DOMAIN,
        translation_key="battery_control_refused",
        translation_placeholders={"reason": reason},
    )


async def _async_battery_control_test(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Run (or `stop`) the owner's test action on a site's battery or export limit; answers what started."""
    entry = _export_site(hass, call.data.get("site"))
    controller = site_controller_for(hass, entry.entry_id)
    try:
        return await controller.battery_control.async_trial(
            call.data["action"], call.data.get("power"), call.data["minutes"], force=call.data["force_test"]
        )
    except TrialRefused as refusal:
        raise _battery_refusal(refusal.code) from None


async def _async_battery_control_settings(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Write a site's battery control options (as the card's switch will) and answer its status."""
    entry = _export_site(hass, call.data.get("site"))
    controller = site_controller_for(hass, entry.entry_id)
    changes = {key: value for key, value in call.data.items() if key in gate.WRITABLE}
    if not changes:
        raise _battery_refusal("no_change")
    return await controller.battery_control.async_update_options(changes)


#: The year's review now (`year_review/runtime.py`): worked out, offered in the cards for a week, its figures answered.
SERVICE_SHOW_YEAR_REVIEW = "show_year_review"

SHOW_YEAR_REVIEW_SCHEMA = vol.Schema(
    {
        vol.Optional("site"): cv.string,
        vol.Optional("year"): vol.All(vol.Coerce(int), vol.Range(min=2000, max=2200)),
        vol.Optional("reset", default=True): cv.boolean,
    }
)


async def _async_show_year_review(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Work a site's year review out now (default: the year the cards would show), offer its banner and Support row
    for a week whatever the date, forget who saw it (`reset`, on by default), and answer the review."""
    from .year_review.runtime import YearReviewRefusal  # noqa: PLC0415 - it imports the API

    entry = _export_site(hass, call.data.get("site"))
    data = site_data(hass, entry.entry_id)
    review = None if data is None else data.year_review
    if review is None:
        raise _export_refusal("year_review_not_loaded", "The SpotNav site's year review is not loaded")
    year = call.data.get("year")
    if year is None:
        year = review.default_year()
    try:
        return await review.async_show(year, call.data["reset"])
    except YearReviewRefusal as refusal:
        if refusal.code == "not_loaded":
            raise _export_refusal("year_review_not_loaded", f"No year review for {year}: {refusal}") from None
        raise ServiceValidationError(
            f"No year review for {year}: {refusal}",
            translation_domain=DOMAIN,
            translation_key="year_review_no_year",
            translation_placeholders={"year": str(year)},
        ) from None
    except HomeAssistantError:
        raise
    except Exception as err:
        _LOGGER.exception("SpotNav site %s: the year review of %s could not be worked out", entry.entry_id, year)
        raise HomeAssistantError(
            f"The year review of {year} could not be worked out: {err}",
            translation_domain=DOMAIN,
            translation_key="year_review_failed",
            translation_placeholders={"year": str(year)},
        ) from err


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
