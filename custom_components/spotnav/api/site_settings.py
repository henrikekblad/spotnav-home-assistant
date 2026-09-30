"""The one authenticated, admin-only site-settings command: `spotnav/update_site_settings`.

A compare-and-set over one site config entry's stored data, reusing the dashboard contract's own
`site` capture and serialization.

* The charger names the site (`charger_id`); the server resolves it, never a site entry id.
* A stale write is a `conflict`: `expected` carries the values (a subset of
  `solar_priority`/`solar_forecast`) the card last saw, and the current `site` block travels back.
* One write, only the fields this command owns. `changes` names `solar_priority` and/or
  `solar_forecast` (validated against the site options flow's vocabulary and
  `dashboard_api.solar_forecast_choices`), or `active_control_enabled` alone (one write, one
  meaning). That one is applied in place on the running `SiteCapacityController`
  (`async_enable_active_control` / `async_disable_active_control`), persisted without a reload,
  and a disable answers with a `restore` outcome (`not_needed | restored | failed`, per charger).
  Any other key is refused. Active load balancing is WebSocket-only: the webhook secret may write
  solar settings but never the fuse-protection switch.
* Refusals are stable codes in the success envelope shape: `not_admin`, `no_site`, `conflict`,
  `invalid_value`, `unavailable`, `active_control_unavailable` (may be turned off, never newly on)
  and `confirmation_failed` (the read-back state was not the requested one; the previous value
  was kept).
* The answer is always a re-read: the `site` block is captured after the write settles, including
  the reload it triggers (site entries have no update listener), never from what the caller sent.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_SOLAR_PRIORITY,
    DEFAULT_SOLAR_PRIORITY,
    SOLAR_PRIORITY_CHOICES,
)
from ..planning.hybrid_forecast import async_forecast_capable_domains
from ..runtime import site_controller_for
from ..site.site_capacity_controller import (
    ENABLE_ALREADY,
    ENABLE_ENABLED,
    RestoreReport,
    SiteCapacityController,
    SiteControllerClosed,
)
from .common import ERROR_NOT_ADMIN, ERROR_UNSUPPORTED_VERSION, is_admin, send_unsupported_version
from .dashboard import (
    capture_site,
    DashboardFailure,
    resolve_charger_request,
    serialize_site,
    site_binding,
    solar_forecast_choices,
)


_LOGGER = logging.getLogger(__name__)

#: This contract's own version, separate from the dashboard's and the settings'.
SITE_SETTINGS_API_VERSION: Final = 1

ERROR_NO_SITE: Final = "spotnav_no_site"
ERROR_CONFLICT: Final = "spotnav_conflict"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"
ERROR_UNAVAILABLE: Final = "spotnav_site_unavailable"
ERROR_ACTIVE_CONTROL_UNAVAILABLE: Final = "spotnav_active_control_unavailable"
ERROR_CONFIRMATION_FAILED: Final = "spotnav_confirmation_failed"
#: The webhook's secret may write solar settings but never touch active load balancing.
ERROR_NOT_PERMITTED_OVER_WEBHOOK: Final = "spotnav_not_permitted_over_webhook"


_SOLAR_PRIORITY: Final = "solar_priority"
_SOLAR_FORECAST: Final = "solar_forecast"
_ACTIVE_CONTROL: Final = CONF_ACTIVE_CONTROL_ENABLED
_WRITABLE_FIELDS: Final = (_SOLAR_PRIORITY, _SOLAR_FORECAST, _ACTIVE_CONTROL)


class SiteSettingsRefusal(Exception):
    """A stable-code refusal, carrying the `site` block to answer with.

    `site` is `None` only when no site was resolved (`no_site`) or admin was refused before any
    charger was read (`not_admin`); every other refusal carries a freshly re-read block.
    """

    def __init__(
        self,
        code: str,
        site: dict[str, Any] | None = None,
        restore: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.site = site
        # A restore an earlier off transition already ran travels even with a refusal.
        self.restore = restore


def _current_values(entry: ConfigEntry) -> tuple[str, tuple[str, ...]]:
    """The two solar fields' stored values, read as `dashboard_api.capture_site` reads them (entry data)."""
    priority = entry.data.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY)
    forecast = tuple(entry.data.get(CONF_SOLAR_FORECAST_ENTRIES, []) or ())
    return str(priority), forecast


async def _site_block(
    hass: HomeAssistant,
    charger_entry_id: str,
    *,
    forecast_domains: frozenset[str],
    active_control_writable: bool = True,
) -> dict[str, Any] | None:
    """The `site` block this command answers with: `capture_site` re-read now and serialized as the
    dashboard does (`writable` is always `True`; only an admin reaches this far).
    """
    site = capture_site(hass, charger_entry_id, forecast_domains=forecast_domains)
    return serialize_site(site, can_act=True, active_control_writable=active_control_writable)


def _persisted_active_control(entry: ConfigEntry) -> bool:
    """The stored opt-in, read from the entry's data."""
    return bool(entry.data.get(_ACTIVE_CONTROL, False))


def serialize_restore(hass: HomeAssistant, report: RestoreReport) -> dict[str, Any]:
    """The `restore` block: overall outcome and, per member charger, what was found and done.

    `from_a` is what the charger was read at, `to_a` the authoritative current it returns to (either
    may be `null`); `code` is present only when that charger's restore failed.
    """
    chargers = []
    for item in report.chargers:
        entry = hass.config_entries.async_get_entry(item.charger_entry_id)
        chargers.append(
            {
                "charger_id": item.charger_entry_id,
                "charger_name": None if entry is None else entry.title,
                "outcome": item.restore.outcome,
                "code": item.restore.code,
                "from_a": item.restore.from_a,
                "to_a": item.restore.to_a,
            }
        )
    return {"outcome": report.outcome, "chargers": chargers}


def _live(hass: HomeAssistant, site_entry: ConfigEntry, controller: SiteCapacityController) -> bool:
    """Whether this exact controller is still the loaded site's running one."""
    return (
        site_entry.state is ConfigEntryState.LOADED
        and site_controller_for(hass, site_entry.entry_id) is controller
    )


async def async_update_site_settings(
    hass: HomeAssistant,
    charger_id: Any,
    *,
    expected: Any,
    changes: Any,
    active_control_writable: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve, validate, compare-and-set, write at most once, and answer `(site, restore)`: the re-read
    `site` block and (a disable only) the `restore` block. Raises `SiteSettingsRefusal` for every
    refusal; the websocket handler turns a raise or return into the wire envelope.

    Everything from the compare onward runs under the controller's `transition_lock`, so admins'
    calls and solar writes cannot interleave with a reload; a call that finds its controller replaced
    while waiting starts again.
    """
    resolved = resolve_charger_request(hass, {"charger_id": charger_id})
    if isinstance(resolved, DashboardFailure):
        raise SiteSettingsRefusal(ERROR_UNAVAILABLE)
    charger_entry_id = resolved.entry_id

    site_entry = site_binding(hass, charger_entry_id)
    if site_entry is None:
        raise SiteSettingsRefusal(ERROR_NO_SITE)

    forecast_domains = await async_forecast_capable_domains(hass)

    if not active_control_writable and any(
        isinstance(named, dict) and _ACTIVE_CONTROL in named for named in (changes, expected)
    ):
        # The webhook carrier (`active_control_writable=False`): naming active load balancing at
        # all is refused before anything is compared.
        raise SiteSettingsRefusal(
            ERROR_NOT_PERMITTED_OVER_WEBHOOK,
            await _site_block(
                hass,
                charger_entry_id,
                forecast_domains=forecast_domains,
                active_control_writable=False,
            ),
        )

    for _attempt in range(3):
        controller = site_controller_for(hass, site_entry.entry_id)
        if controller is None or site_entry.state is not ConfigEntryState.LOADED:
            raise SiteSettingsRefusal(ERROR_UNAVAILABLE)
        async with controller.transition_lock:
            if not _live(hass, site_entry, controller):
                continue
            return await _update_locked(
                hass,
                charger_entry_id,
                site_entry,
                controller,
                forecast_domains=forecast_domains,
                expected=expected,
                changes=changes,
                active_control_writable=active_control_writable,
            )
    raise SiteSettingsRefusal(ERROR_UNAVAILABLE)


async def _update_locked(
    hass: HomeAssistant,
    charger_entry_id: str,
    site_entry: ConfigEntry,
    controller: SiteCapacityController,
    *,
    forecast_domains: frozenset[str],
    expected: Any,
    changes: Any,
    active_control_writable: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The compare, the one write and the re-read, with `controller.transition_lock` held."""

    async def _site() -> dict[str, Any] | None:
        return await _site_block(
            hass,
            charger_entry_id,
            forecast_domains=forecast_domains,
            active_control_writable=active_control_writable,
        )

    async def _refuse(code: str, restore: dict[str, Any] | None = None) -> None:
        raise SiteSettingsRefusal(code, await _site(), restore)

    if not isinstance(changes, dict) or not changes:
        await _refuse(ERROR_INVALID_VALUE)
    unknown_fields = set(changes) - set(_WRITABLE_FIELDS)
    if unknown_fields:
        # A name this command does not own is refused, never dropped or written.
        await _refuse(ERROR_INVALID_VALUE)

    current_priority, current_forecast = _current_values(site_entry)

    if not isinstance(expected, dict):
        await _refuse(ERROR_INVALID_VALUE)
    if _SOLAR_PRIORITY in expected and expected[_SOLAR_PRIORITY] != current_priority:
        await _refuse(ERROR_CONFLICT)
    if _SOLAR_FORECAST in expected:
        expected_forecast = expected[_SOLAR_FORECAST]
        if not isinstance(expected_forecast, list) or tuple(expected_forecast) != current_forecast:
            await _refuse(ERROR_CONFLICT)

    if _ACTIVE_CONTROL in changes:
        return await _change_active_control(
            hass,
            charger_entry_id,
            site_entry,
            controller,
            forecast_domains=forecast_domains,
            expected=expected,
            changes=changes,
        )
    if _ACTIVE_CONTROL in expected:
        expected_active = expected[_ACTIVE_CONTROL]
        if not isinstance(expected_active, bool):
            await _refuse(ERROR_INVALID_VALUE)
        if expected_active != controller.active_control_enabled:
            await _refuse(ERROR_CONFLICT)

    new_priority = current_priority
    if _SOLAR_PRIORITY in changes:
        candidate_priority = changes[_SOLAR_PRIORITY]
        if candidate_priority not in SOLAR_PRIORITY_CHOICES:
            await _refuse(ERROR_INVALID_VALUE)
        new_priority = candidate_priority

    new_forecast = current_forecast
    if _SOLAR_FORECAST in changes:
        candidate_forecast = changes[_SOLAR_FORECAST]
        if not isinstance(candidate_forecast, list) or not all(
            isinstance(item, str) and item for item in candidate_forecast
        ):
            await _refuse(ERROR_INVALID_VALUE)
        valid_ids = {
            entry_id
            for entry_id, _ in solar_forecast_choices(hass, forecast_domains, current_forecast)
        }
        if any(item not in valid_ids for item in candidate_forecast):
            await _refuse(ERROR_INVALID_VALUE)
        new_forecast = tuple(candidate_forecast)

    if (new_priority, new_forecast) != (current_priority, current_forecast):
        updated_data = dict(site_entry.data)
        updated_data[CONF_SOLAR_PRIORITY] = new_priority
        updated_data[CONF_SOLAR_FORECAST_ENTRIES] = list(new_forecast)
        # `entry.data`, not `entry.options`: every other site field lives in `entry.data`, so
        # writing options would be invisible to the flow and the controller. One write, once.
        hass.config_entries.async_update_entry(site_entry, data=updated_data)
        # Site entries have no update listener, so reload explicitly (as
        # `SiteCapacityOptionsFlow._save_site_details` does) and await it, so the `site` block
        # below is captured after the reload settles.
        await hass.config_entries.async_reload(site_entry.entry_id)

    return await _site(), None


async def _change_active_control(
    hass: HomeAssistant,
    charger_entry_id: str,
    site_entry: ConfigEntry,
    controller: SiteCapacityController,
    *,
    forecast_domains: frozenset[str],
    expected: dict[str, Any],
    changes: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Turn the site's active load balancing on or off, in place, and persist it. Lock held.

    The running controller changes first and the entry is persisted second: a crash between leaves
    "on" over a controller that was turned off (balancing resumes after a restart) or "off" over one
    turned on (nothing written), never "off" over a charger left throttled. The accepted state is read
    back (stored value and controller must both match); otherwise the previous state is restored and
    `confirmation_failed` answers.
    """

    async def _refuse(code: str, restore: dict[str, Any] | None = None) -> None:
        raise SiteSettingsRefusal(
            code,
            await _site_block(hass, charger_entry_id, forecast_domains=forecast_domains),
            restore,
        )

    desired = changes[_ACTIVE_CONTROL]
    if set(changes) != {_ACTIVE_CONTROL} or not isinstance(desired, bool):
        await _refuse(ERROR_INVALID_VALUE)
    if _ACTIVE_CONTROL in expected:
        expected_active = expected[_ACTIVE_CONTROL]
        if not isinstance(expected_active, bool):
            await _refuse(ERROR_INVALID_VALUE)
        if expected_active != controller.active_control_enabled:
            await _refuse(ERROR_CONFLICT)

    previous_live = controller.active_control_enabled
    previous_stored = _persisted_active_control(site_entry)
    restore: dict[str, Any] | None = None

    try:
        if desired:
            outcome = await controller.async_enable_active_control()
            if outcome not in (ENABLE_ENABLED, ENABLE_ALREADY):
                # A site that cannot be actively controlled is never newly turned on.
                await _refuse(ERROR_ACTIVE_CONTROL_UNAVAILABLE)
        else:
            restore = serialize_restore(hass, await controller.async_disable_active_control())
    except SiteControllerClosed:
        raise SiteSettingsRefusal(ERROR_UNAVAILABLE) from None

    if not _live(hass, site_entry, controller):
        # Unloaded or reloaded meanwhile: keep the stored value (a reload rebuilds from it) and
        # report the site unavailable, never "disabled".
        raise SiteSettingsRefusal(ERROR_UNAVAILABLE, None, restore)

    if previous_stored != desired:
        updated_data = dict(site_entry.data)
        updated_data[_ACTIVE_CONTROL] = desired
        hass.config_entries.async_update_entry(site_entry, data=updated_data)

    if _persisted_active_control(site_entry) != desired or controller.active_control_enabled != desired:
        # Read back, it is not what was asked: keep the previously confirmed value everywhere.
        if previous_stored != _persisted_active_control(site_entry):
            hass.config_entries.async_update_entry(
                site_entry, data={**site_entry.data, _ACTIVE_CONTROL: previous_stored}
            )
        try:
            if previous_live and not controller.active_control_enabled:
                await controller.async_enable_active_control()
            elif not previous_live and controller.active_control_enabled:
                await controller.async_disable_active_control()
        except SiteControllerClosed:
            pass
        await _refuse(ERROR_CONFIRMATION_FAILED, restore)

    return (
        await _site_block(hass, charger_entry_id, forecast_domains=forecast_domains),
        restore,
    )


def _envelope(site: dict[str, Any] | None, restore: dict[str, Any] | None) -> dict[str, Any]:
    """The success answer: this contract's version, the re-read `site` block and the `restore`."""
    return {
        "api_version": SITE_SETTINGS_API_VERSION,
        "ok": True,
        "error": None,
        "site": site,
        "restore": restore,
    }


def _failure(
    code: str, site: dict[str, Any] | None, restore: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The refusal: a stable code and no prose, in the same envelope shape as a success."""
    return {
        "api_version": SITE_SETTINGS_API_VERSION,
        "ok": False,
        "error": code,
        "site": site,
        "restore": restore,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_site_settings",
        # Version is judged in the handler so a wrong one gets this contract's stable code.
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("expected"): object,
        vol.Optional("changes"): object,
    }
)
@websocket_api.async_response
async def websocket_update_site_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Update one site's solar priority and/or hybrid forecast sources, or its active load-balancing
    opt-in. Admin only.

    Every outcome, `not_admin` included, answers through `connection.send_result` in one envelope
    shape, so a client has one code path.
    """
    version = msg.get("api_version")
    if isinstance(version, bool) or version != SITE_SETTINGS_API_VERSION:
        send_unsupported_version(connection, msg, SITE_SETTINGS_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _failure(ERROR_NOT_ADMIN, None))
        return
    try:
        site, restore = await async_update_site_settings(
            hass,
            msg.get("charger_id"),
            expected=msg.get("expected"),
            changes=msg.get("changes"),
        )
    except SiteSettingsRefusal as refusal:
        connection.send_result(msg["id"], _failure(refusal.code, refusal.site, refusal.restore))
        return
    connection.send_result(msg["id"], _envelope(site, restore))


async def async_webhook_update_site_settings(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """`update_site_settings` over the webhook: solar priority and forecast sources only.

    Same core, compare-and-set and envelope as the WebSocket command; the charger is the webhook's
    own entry. Naming `active_control_enabled` is `spotnav_not_permitted_over_webhook` and nothing is
    written. Returns `(http_status, body)`.
    """
    version = payload.get("api_version")
    if version is not None and (isinstance(version, bool) or version != SITE_SETTINGS_API_VERSION):
        return 400, _failure(ERROR_UNSUPPORTED_VERSION, None)
    try:
        site, restore = await async_update_site_settings(
            hass,
            entry.entry_id,
            expected=payload.get("expected"),
            changes=payload.get("changes"),
            active_control_writable=False,
        )
    except SiteSettingsRefusal as refusal:
        status = {
            ERROR_CONFLICT: 409,
            ERROR_NOT_PERMITTED_OVER_WEBHOOK: 403,
        }.get(refusal.code, 400)
        return status, _failure(refusal.code, refusal.site)
    return 200, _envelope(site, restore)


@callback
def async_setup_site_settings_api(hass: HomeAssistant) -> None:
    """Register this contract's one command once for the domain, not once per config entry."""
    websocket_api.async_register_command(hass, websocket_update_site_settings)
