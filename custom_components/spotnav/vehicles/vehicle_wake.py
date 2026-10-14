"""Waking a car for a reading: the one opt-in exception to "never wake the car" (`vehicle_refresh.py`).

A car's integration often answers `homeassistant.update_entity` from its cloud's cache, and a sleeping car's cache
can be hours old (an EV6 on kia_uvo read 86 % for a car that had been full at 100 % for hours). A person may turn
on "Wake the car for a reading" for one car (`vehicle_properties.KEY_WAKE`, off by default); SpotNav then asks the
car itself to report, through its integration's own wake, at the two moments a charge's end waits for the car's
level: the target's confirmation (only an estimate says the target is reached) and a plan charge the car ended by
itself (`car_ended_waiting`). Never on a person's own Refresh, never anywhere else.

Which integrations: only those whose wake was read in their own source (`WAKE_METHODS`). An integration not in it
offers no wake, and the car's settings show no row for it.

How often: at most once per occasion (one charge's end) and never within `WAKE_MIN_INTERVAL_S` of the last wake of
the same car, whatever the result. The last wake is stored (`DECISION_DOMAIN_VEHICLE_WAKE`) before the call is
made, so a restart, or a restart loop, cannot wake the car again inside the interval. It is also what diagnostics
show: when, through which integration, and the result.

Never an error: a wake that fails, times out or cannot be made is logged and noted; SpotNav's own decisions go on
from whatever reading comes (the estimate with its margin, as without a wake).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..runtime import domain_data
from .discovery_decisions import DECISION_DOMAIN_VEHICLE_WAKE
from .vehicle_discovery import vehicle_reading_entity_ids, vehicle_soc_entity_id
from .vehicle_properties import stored_properties


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WakeMethod:
    """How one integration wakes a car: a service called with the car's `device_id`, or the car's own wake
    button (`button.press` on the device's button whose translation key is `button_key`)."""

    service: tuple[str, str] | None = None
    button_key: str | None = None


#: The integrations whose wake was read in their source, and nothing else:
#: * `kia_uvo` (Hyundai-Kia-Connect/kia_uvo, `services.yaml` and `services.py` at v3.17.0): `force_update` with an
#:   optional `device_id`, which picks the account's coordinator and asks the cars of that account to report
#:   (`force_refresh_all_vehicles_states`), then publishes the answer.
#: * `tesla_fleet`, `teslemetry`, `tessie` (Home Assistant core, `button.py`): a "wake" button per car
#:   (unique id `<vin>-wake`, translation key `wake`) that wakes the car; the integration's next read of the car
#:   (asked straight after, `vehicle_refresh`) then reports it awake.
WAKE_METHODS: Final[dict[str, WakeMethod]] = {
    "kia_uvo": WakeMethod(service=("kia_uvo", "force_update")),
    "tesla_fleet": WakeMethod(button_key="wake"),
    "teslemetry": WakeMethod(button_key="wake"),
    "tessie": WakeMethod(button_key="wake"),
}

#: Minimum time between two wakes of one car, whatever started them and whatever they answered. A charge's end
#: needs one answer; half an hour keeps a target's re-asks, a car ending the charge shortly after and a restart
#: from waking it again, and stays far inside the makers' daily request limits (kia_uvo: some 30 a day in Europe).
WAKE_MIN_INTERVAL_S: Final = 30 * 60.0

#: How long a wake is waited for before it counts as timed out. A car answers in one to three minutes; five keeps
#: the re-read that follows inside the target's wait for an asked reading (`target_stop.ESTIMATE_CONFIRM_WAIT_S`,
#: ten minutes), so a woken car's level still decides the stop.
WAKE_TIMEOUT_S: Final = 5 * 60.0

#: Results, as stored and shown in diagnostics.
RESULT_ASKED: Final = "asked"  # the call was made and has not answered (or Home Assistant stopped meanwhile)
RESULT_OK: Final = "ok"
RESULT_FAILED: Final = "failed"
RESULT_TIMEOUT: Final = "timeout"
#: Results of a wake not made (not stored).
SKIP_OFF: Final = "off"
SKIP_UNSUPPORTED: Final = "unsupported"
SKIP_TOO_SOON: Final = "too_soon"
SKIP_SAME_OCCASION: Final = "same_occasion"
SKIP_BUSY: Final = "busy"

_IN_FLIGHT: Final = "spotnav_vehicle_wake_in_flight"


@dataclass(frozen=True, slots=True)
class WakeTarget:
    """One car's wake as it would be made now: the integration, and the service call."""

    integration: str
    domain: str
    service: str
    data: dict[str, Any]


def _integration_of(hass: HomeAssistant, vehicle_id: str) -> str | None:
    """The integration the car's level comes from: its state-of-charge entity's platform, else that of the first of
    its reading entities owned by an integration in `WAKE_METHODS`."""
    registry = er.async_get(hass)
    soc = vehicle_soc_entity_id(hass, vehicle_id)
    entry = None if soc is None else registry.async_get(soc)
    if entry is not None:
        return entry.platform
    for entity_id in vehicle_reading_entity_ids(hass, vehicle_id) or ():
        other = registry.async_get(entity_id)
        if other is not None and other.platform in WAKE_METHODS:
            return other.platform
    return None


def wake_target(hass: HomeAssistant, vehicle_id: object) -> WakeTarget | None:
    """How this car would be woken, or `None` when its integration offers no wake SpotNav knows."""
    if not isinstance(vehicle_id, str) or not vehicle_id or dr.async_get(hass).async_get(vehicle_id) is None:
        return None
    integration = _integration_of(hass, vehicle_id)
    method = None if integration is None else WAKE_METHODS.get(integration)
    if integration is None or method is None:
        return None
    if method.service is not None:
        return WakeTarget(integration, method.service[0], method.service[1], {"device_id": vehicle_id})
    for entry in er.async_entries_for_device(er.async_get(hass), vehicle_id):
        if (
            entry.domain == "button"
            and entry.platform == integration
            and entry.disabled_by is None
            and (entry.translation_key == method.button_key or entry.unique_id.endswith(f"-{method.button_key}"))
        ):
            return WakeTarget(integration, "button", "press", {"entity_id": entry.entity_id})
    return None


def wake_supported(hass: HomeAssistant, vehicle_id: object) -> bool:
    """Whether this car's integration offers a wake SpotNav knows (the setting's row is shown only then)."""
    return wake_target(hass, vehicle_id) is not None


def last_wake(hass: HomeAssistant, vehicle_id: str) -> dict[str, Any] | None:
    """The last wake of this car as stored: `{"at", "integration", "occasion", "result"}`, or `None`."""
    store = domain_data(hass).decision_store
    return None if store is None else store.confirmed_payload(DECISION_DOMAIN_VEHICLE_WAKE, vehicle_id)


def _last_at(record: dict[str, Any] | None) -> datetime | None:
    at = None if record is None else record.get("at")
    return dt_util.parse_datetime(at) if isinstance(at, str) else None


def wake_skip_reason(hass: HomeAssistant, vehicle_id: str, occasion: str) -> str | None:
    """Why this car is not woken for `occasion` now, or `None` when it may be."""
    if not stored_properties(hass, vehicle_id).wake_for_reading:
        return SKIP_OFF
    if wake_target(hass, vehicle_id) is None:
        return SKIP_UNSUPPORTED
    if vehicle_id in hass.data.setdefault(_IN_FLIGHT, set()):
        return SKIP_BUSY
    record = last_wake(hass, vehicle_id)
    if record is not None and record.get("occasion") == occasion:
        return SKIP_SAME_OCCASION
    last = _last_at(record)
    if last is not None and (dt_util.utcnow() - last).total_seconds() < WAKE_MIN_INTERVAL_S:
        return SKIP_TOO_SOON
    return None


async def _note(hass: HomeAssistant, vehicle_id: str, record: dict[str, Any]) -> None:
    store = domain_data(hass).decision_store
    if store is not None:
        await store.async_confirm(DECISION_DOMAIN_VEHICLE_WAKE, vehicle_id, record)


async def async_wake_vehicle(hass: HomeAssistant, vehicle_id: str, occasion: str) -> str:
    """Ask the car itself to report, when it may be (`wake_skip_reason`); return the result or the reason it was not.

    Never raises. The record is stored before the call so an interrupted wake still counts against the interval.
    """
    try:
        reason = wake_skip_reason(hass, vehicle_id, occasion)
        if reason is not None:
            if reason not in (SKIP_OFF, SKIP_UNSUPPORTED):
                _LOGGER.debug("Not waking a car for a reading: %s", reason)
            return reason
        target = wake_target(hass, vehicle_id)
        assert target is not None
        in_flight: set[str] = hass.data.setdefault(_IN_FLIGHT, set())
        in_flight.add(vehicle_id)
    except Exception as err:  # noqa: BLE001 - a wake is never SpotNav's error
        _LOGGER.debug("Could not decide on waking a car: %s", type(err).__name__)
        return RESULT_FAILED
    record = {
        "at": dt_util.utcnow().isoformat(),
        "integration": target.integration,
        "occasion": occasion,
        "result": RESULT_ASKED,
    }
    try:
        await _note(hass, vehicle_id, record)
        _LOGGER.info("Waking a car for a reading through %s", target.integration)
        try:
            async with asyncio.timeout(WAKE_TIMEOUT_S):
                await hass.services.async_call(target.domain, target.service, dict(target.data), blocking=True)
            result = RESULT_OK
        except TimeoutError:
            result = RESULT_TIMEOUT
        except Exception as err:  # noqa: BLE001 - another integration's failure is not ours to raise
            _LOGGER.info("Waking a car through %s failed: %s", target.integration, type(err).__name__)
            result = RESULT_FAILED
        await _note(hass, vehicle_id, {**record, "result": result})
    except Exception as err:  # noqa: BLE001 - a wake is never SpotNav's error
        _LOGGER.debug("Waking a car failed: %s", type(err).__name__)
        result = RESULT_FAILED
    finally:
        in_flight.discard(vehicle_id)
    if result == RESULT_TIMEOUT:
        _LOGGER.info("Waking a car through %s did not answer within %.0f s", target.integration, WAKE_TIMEOUT_S)
    return result


def wake_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """Every car's last wake, by vehicle id, with whether it is turned on and supported now."""
    store = domain_data(hass).decision_store
    if store is None:
        return {}
    out: dict[str, Any] = {}
    for vehicle_id in store.confirmed_ids(DECISION_DOMAIN_VEHICLE_WAKE):
        out[vehicle_id] = {
            "last": last_wake(hass, vehicle_id),
            "enabled": stored_properties(hass, vehicle_id).wake_for_reading,
            "supported": wake_supported(hass, vehicle_id),
        }
    return out
