"""The one canonical settings contract: one public value, one strict codec, two transports.

`AutoSettings` is a storage record and `AutoSettingsStore` its only writer; this module is the
public face, so nothing here serializes `as_dict()` or accepts `from_stored()` publicly.

* Full replacement, never a patch: "absent" has exactly one meaning, and the client states every
  value it wants.
* `revision` is not part of the body: the client names it in `expected_revision`, and a body
  carrying `revision` is refused as an unknown field rather than overriding the compare-and-set.
* Absence is not zero: a fiscal component is off, on with a value, or on with none; `null` never
  becomes `0` and nothing is clamped, coerced or inferred.
* Every refusal is a stable code (`AutoSettingsError.code`); the message is for the log only.

The body carries planning inputs only. A pause is an execution intent with a physical stop behind
it, so a replacement carries the stored intent through untouched (pause transitions belong to the
execution action boundary). Vehicle properties are written with `update_vehicle`.

The mutation both transports call follows the codec.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime, time
from typing import Any, Callable, Final, Mapping

from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant
from homeassistant.util import dt as dt_util

from ..planning.auto_controller import SettingsReconcileError
from ..planning.auto_settings import (
    AreaAutoSettings,
    AutoSettings,
    AutoSettingsError,
    FiscalOverride,
    PauseIntent,
    SettingsCode,
    STORED_STRATEGIES,
    TargetSocIntent,
)
from ..runtime import controller_for, domain_data, preview_for
from .common import ERROR_CHARGER_UNLOADED, lookup_charger, send_unsupported_version


#: The wire contract's own version, separate from the storage schema, the dashboard's and the
#: site settings'.
SETTINGS_API_VERSION: Final = 1

#: The keys a replacement body must carry, exactly; without `revision`.
SETTINGS_KEYS: Final = frozenset(
    {
        "strategy",
        "area_id",
        "overrides",
        "phases",
        "amps",
        "requested_kwh",
        "max_periods",
        "departure_enabled",
        "departure_time",
        "driver",
        "target",
    }
)

SETTINGS_RESPONSE_KEYS: Final = SETTINGS_KEYS | {"revision"}

OVERRIDE_KEYS: Final = frozenset({"area_id", "vat", "tax", "transfer"})
FISCAL_KEYS: Final = frozenset({"enabled", "value"})
TARGET_KEYS: Final = frozenset({"vehicle_id", "target_percent"})

#: The read-only pause observation: the persisted record, or three nulls.
PAUSE_KEYS: Final = frozenset({"choice", "admitted_at", "expires_at"})

SETTINGS_ENVELOPE_KEYS: Final = frozenset({"api_version", "ok", "error", "settings", "pause"})

WALL_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

NOT_A_BODY: Final[SettingsCode] = "unknown_field"


def _refuse(code: SettingsCode, message: str) -> None:
    raise AutoSettingsError(code, message)


def _object(raw: Any, keys: frozenset[str], what: str) -> Mapping[str, Any]:
    """An exact-shape object: every key present, none unknown, nothing coerced."""
    if not isinstance(raw, Mapping):
        _refuse(NOT_A_BODY, f"{what} must be an object")
    missing = keys - set(raw)
    if missing:
        _refuse("missing_field", f"{what} is missing {sorted(missing)}")
    unknown = set(raw) - keys
    if unknown:
        # A `revision` in a replacement body lands here on purpose.
        _refuse("unknown_field", f"{what} has unknown fields {sorted(unknown)}")
    return raw


def _finite(value: Any, what: str, code: SettingsCode = "invalid_number") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(code, f"{what} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        _refuse(code, f"{what} must be finite")
    return number


def _whole(value: Any, what: str, code: SettingsCode = "invalid_number") -> int:
    number = _finite(value, what, code)
    if number != int(number):
        _refuse(code, f"{what} must be a whole number")
    return int(number)


def _boolean(value: Any, what: str, code: SettingsCode = "invalid_number") -> bool:
    if not isinstance(value, bool):
        # `bool("false")` is `True`, so a string is refused by name.
        _refuse(code, f"{what} must be a boolean")
    return value


def _text_or_none(value: Any, what: str, code: SettingsCode) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _refuse(code, f"{what} must be a string or null")
    return value


def _fiscal(raw: Any, what: str) -> FiscalOverride:
    stored = _object(raw, FISCAL_KEYS, what)
    return FiscalOverride(
        enabled=_boolean(stored["enabled"], f"{what}.enabled", "invalid_fiscal"),
        value=None
        if stored["value"] is None
        else _finite(stored["value"], f"{what}.value", "invalid_fiscal"),
    ).validated(what)


def _override(raw: Any) -> AreaAutoSettings:
    stored = _object(raw, OVERRIDE_KEYS, "an area override")
    area_id = stored["area_id"]
    if not isinstance(area_id, str):
        _refuse("invalid_area", "an area override needs an area id")
    return AreaAutoSettings(
        area_id=area_id,
        vat=_fiscal(stored["vat"], "vat"),
        tax=_fiscal(stored["tax"], "tax"),
        transfer=_fiscal(stored["transfer"], "transfer"),
    ).validated()


def _target(raw: Any) -> TargetSocIntent:
    stored = _object(raw, TARGET_KEYS, "target")
    return TargetSocIntent(
        vehicle_id=_text_or_none(stored["vehicle_id"], "vehicle_id", "invalid_target"),
        target_percent=None
        if stored["target_percent"] is None
        else _finite(stored["target_percent"], "target_percent", "invalid_target"),
    ).validated()


def departure_from_wire(value: Any) -> time:
    """`"HH:MM"` as an area-local wall time, or a refusal."""
    if not isinstance(value, str):
        _refuse("invalid_departure", "departure_time must be a wall time string")
    match = WALL_TIME.match(value)
    if match is None:
        _refuse("invalid_departure", "departure_time must be HH:MM")
    return time(int(match.group(1)), int(match.group(2)))


def departure_to_wire(departure: time) -> str:
    """A wall time as the dashboard already spells it."""
    return f"{departure.hour:02d}:{departure.minute:02d}"


def encoded_fiscal(fiscal: FiscalOverride) -> dict[str, Any]:
    return {"enabled": fiscal.enabled, "value": fiscal.value}


def encoded_override(override: AreaAutoSettings) -> dict[str, Any]:
    return {
        "area_id": override.area_id,
        "vat": encoded_fiscal(override.vat),
        "tax": encoded_fiscal(override.tax),
        "transfer": encoded_fiscal(override.transfer),
    }


def encoded_target(target: TargetSocIntent) -> dict[str, Any]:
    return {"vehicle_id": target.vehicle_id, "target_percent": target.target_percent}


def strategy_of(settings: AutoSettings) -> str:
    return settings.strategy


def encode_settings(settings: AutoSettings) -> dict[str, Any]:
    """The public value: every user-owned planning input, and the record's revision."""
    return {
        "revision": settings.revision,
        "strategy": strategy_of(settings),
        "area_id": settings.area_id,
        "overrides": [encoded_override(item) for item in settings.overrides],
        "phases": settings.phases,
        "amps": settings.amps,
        "requested_kwh": settings.requested_kwh,
        "max_periods": settings.max_periods,
        "departure_enabled": settings.departure_enabled,
        "departure_time": departure_to_wire(settings.departure),
        "driver": settings.driver,
        "target": encoded_target(settings.target),
    }


def _enum(value: Any, allowed: tuple[str, ...], code: SettingsCode) -> Any:
    if not isinstance(value, str) or value not in allowed:
        _refuse(code, f"{value!r} is not one of {allowed}")
    return value


def decode_settings(raw: Any) -> AutoSettings:
    """A full replacement, decoded to a validated `AutoSettings`, or a refusal by stable code.

    Every key is required and every type exact; the result passes the same `AutoSettings.validated()`
    the store uses. The pause is never part of a body (see `replacement_mutator`).
    """
    stored = _object(raw, SETTINGS_KEYS, "settings")
    overrides = stored["overrides"]
    if not isinstance(overrides, list):
        _refuse("invalid_area", "overrides must be a list")
    strategy = stored["strategy"]
    if not isinstance(strategy, str) or strategy not in STORED_STRATEGIES:
        _refuse("invalid_strategy", f"strategy must be one of {STORED_STRATEGIES}")
    return AutoSettings(
        revision=0,
        strategy=strategy,
        area_id=_text_or_none(stored["area_id"], "area_id", "invalid_area"),
        overrides=tuple(_override(item) for item in overrides),
        phases=None
        if stored["phases"] is None
        else _whole(stored["phases"], "phases", "invalid_phases"),
        amps=None if stored["amps"] is None else _whole(stored["amps"], "amps", "invalid_amps"),
        requested_kwh=_finite(stored["requested_kwh"], "requested_kwh", "invalid_energy"),
        max_periods=_whole(stored["max_periods"], "max_periods", "invalid_periods"),
        departure_enabled=_boolean(
            stored["departure_enabled"], "departure_enabled", "invalid_departure"
        ),
        departure=departure_from_wire(stored["departure_time"]),
        driver=_enum(stored["driver"], ("manual_kwh", "target_soc"), "invalid_driver"),
        target=_target(stored["target"]),
    ).validated()


def replacement_mutator(replacement: AutoSettings) -> Callable[[AutoSettings], AutoSettings]:
    """A full replacement expressed as the store's own mutation hook.

    Every planning field comes from the replacement and the pause exactly as stored, in one atomic
    carry-through, so an edit never clears, admits or re-times a pause (including an expired but
    uncleared one). The store owns the revision increment and the final validation.
    """

    def mutate(current: AutoSettings) -> AutoSettings:
        return replace(replacement, pause=current.pause)

    return mutate


def settings_version_of(value: Any) -> int | None:
    """The settings version a request named, or `None` when this release does not speak it."""
    if isinstance(value, bool):
        return None
    return value if value == SETTINGS_API_VERSION else None


def encode_pause(intent: PauseIntent) -> dict[str, Any]:
    """The typed pause observation: what the persisted record contains, or three nulls.

    Deliberately the stored intent, not the clock's view: this is what the execution gate obeys.
    """
    return {
        "choice": intent.choice,
        "admitted_at": None if intent.admitted_at is None else intent.admitted_at.isoformat(),
        "expires_at": None if intent.expires_at is None else intent.expires_at.isoformat(),
    }


def _pause_instant(value: Any, what: str) -> datetime | None:
    """A wire instant: an offset-bearing ISO string, or null. Never a naive value."""
    if value is None:
        return None
    if not isinstance(value, str):
        _refuse("invalid_pause", f"pause.{what} must be an ISO instant or null")
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        _refuse("invalid_pause", f"pause.{what} must be an offset-bearing ISO instant")
    return parsed


def expected_revision_from(raw: Any) -> int:
    """The revision a client says it edited: non-negative, whole, and never a boolean."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        _refuse("invalid_number", "expected_revision must be a non-negative whole number")
    return raw


def settings_envelope(settings: AutoSettings) -> dict[str, Any]:
    """The transport-neutral success answer: the committed record and its pause, no exception text."""
    return {
        "api_version": SETTINGS_API_VERSION,
        "ok": True,
        "error": None,
        "settings": encode_settings(settings),
        "pause": encode_pause(settings.pause),
    }


def settings_failure(code: str, settings: AutoSettings | None) -> dict[str, Any]:
    """The transport-neutral refusal: a stable code beside the settings that still stand.

    A conflict carries the current record so a caller can retry against it. `settings` is `None` only
    when nothing is readable (an unresolvable entry). Typed `str` because entry-level refusals carry
    `spotnav_*` codes.
    """
    return {
        "api_version": SETTINGS_API_VERSION,
        "ok": False,
        "error": code,
        "settings": None if settings is None else encode_settings(settings),
        "pause": None if settings is None else encode_pause(settings.pause),
    }


# The settings mutation, shared by the WebSocket commands and the scoped webhook, so the two
# cannot drift.
#
# * The controller is the only writer: settings go through
#   `AutoPlannerController.async_apply_settings`, which subscribes, recalculates and reconciles.
#   The store's own lock owns the compare-and-set.
# * Refusals are stable codes (`AutoSettingsError.code` for the body and revision, `spotnav_*` for
#   the entry), never an exception's prose.
# * A conflict carries the current record so a client can retry.


_LOGGER = logging.getLogger(__name__)

#: Raised when the domain is not set up at all.
ERROR_SETTINGS_UNAVAILABLE: Final = "spotnav_settings_unavailable"

# Re-exported from `auto_controller`, where the commit boundary lives, for one obvious import name.

#: A write that did not reach the store: nothing changed and the old revision still stands.
ERROR_SETTINGS_NOT_COMMITTED: Final = "spotnav_settings_not_committed"


class SettingsNotCommitted(Exception):
    """A write that failed before the store returned, carrying the old record that still stands.

    An operational failure of our own persistence, not a bad request. Callers tell the two apart:

    * `SettingsNotCommitted`   -> nothing was written; `settings` is the old current record;
    * `SettingsReconcileError` -> the replacement is durable; `settings` is the new committed one.

    Both travel as the settings envelope with a stable code and no prose.
    """

    code = ERROR_SETTINGS_NOT_COMMITTED

    def __init__(self, settings: AutoSettings) -> None:
        super().__init__(self.code)
        self.settings = settings


class SettingsRefusal(Exception):
    """An entry- or installation-level refusal carrying a spotnav_* code.

    Not `AutoSettingsError`, which names a problem in a settings value (the store vocabulary).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _settings_version(msg: dict[str, Any]) -> int | None:
    """Which settings version this request speaks, or `None` for the stable refusal."""
    return settings_version_of(msg.get("api_version"))


def _refuse_amps_above_charger_range(hass: HomeAssistant, entry_id: str, decoded: AutoSettings) -> None:
    """Refuse `amps` above what this charger can be asked for, with the existing code.

    Enforced for every transport, not only the card's `current_range`; the contract's 80 A bound is
    judged first. A charger with no stated ceiling gets the everyday default.
    """
    if decoded.amps is None:
        return
    charger = controller_for(hass, entry_id)
    if charger is None:
        return
    if decoded.amps > charger.current_range()["max_a"]:
        raise AutoSettingsError("invalid_amps", "amps is above the charger's own maximum current")


async def async_get_settings(hass: HomeAssistant, entry_id: Any) -> AutoSettings:
    """One charger's canonical settings, or a stable-code refusal.

    A never-configured charger reads as the record's defaults at revision 0, not as an error.
    """
    _entry, failure = lookup_charger(hass, entry_id)
    if failure is not None:
        raise SettingsRefusal(failure)
    store = domain_data(hass).auto_store
    if store is None:
        raise SettingsRefusal(ERROR_SETTINGS_UNAVAILABLE)
    return store.settings(entry_id)


async def async_update_settings(
    hass: HomeAssistant,
    entry_id: Any,
    *,
    expected_revision: Any,
    replacement: Any,
) -> AutoSettings:
    """Replace one charger's settings, at a revision the caller names, through its controller.

    The body is decoded and validated before anything is touched. The mutation goes through
    `AutoPlannerController.async_apply_settings`; the store's lock does the compare-and-set and the
    committed record is what this returns. One edit is one store mutation, one revision and at most
    one reconcile, and never admits or clears a pause.
    """
    _entry, failure = lookup_charger(hass, entry_id)
    if failure is not None:
        raise SettingsRefusal(failure)
    store = domain_data(hass).auto_store
    if store is None:
        raise SettingsRefusal(ERROR_SETTINGS_UNAVAILABLE)
    revision = expected_revision_from(expected_revision)
    decoded = decode_settings(replacement)
    mutate = replacement_mutator(decoded)
    _refuse_amps_above_charger_range(hass, entry_id, decoded)
    controller = preview_for(hass, entry_id)
    if controller is None:
        # Loaded but no Auto controller: readable, not writable.
        raise SettingsRefusal(ERROR_CHARGER_UNLOADED)
    try:
        await controller.async_apply_settings(mutate=mutate, expected_revision=revision)
    except AutoSettingsError:
        # Refused by validation or the compare-and-set before the store returned: nothing written.
        raise
    except SettingsReconcileError:
        # The controller's exact boundary: the write committed and this is the committed record
        # (not re-read; a later mutation may already have moved the revision).
        raise
    except Exception:
        raise SettingsNotCommitted(store.settings(entry_id)) from None
    return store.settings(entry_id)


@websocket_api.websocket_command(
    {
        "type": "spotnav/get_settings",
        # Version is judged in the handler so a wrong one gets this contract's stable code.
        "api_version": object,
        "charger_id": str,
    }
)
@websocket_api.async_response
async def websocket_get_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Read one charger's canonical settings. Every authenticated user may read."""
    if _settings_version(msg) is None:
        send_unsupported_version(connection, msg, SETTINGS_API_VERSION)
        return
    try:
        settings = await async_get_settings(hass, msg.get("charger_id"))
    except SettingsRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, "That charger is not available")
        return
    connection.send_result(msg["id"], settings_envelope(settings))


@websocket_api.websocket_command(
    {
        "type": "spotnav/update_settings",
        "api_version": object,
        "charger_id": str,
        "expected_revision": object,
        "settings": object,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_update_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replace one charger's settings. An administrative operation.

    The envelope is transport-neutral (`api_version`/`ok`/`error`/`settings`/`pause`); a conflict
    answers with the current record.
    """
    if _settings_version(msg) is None:
        send_unsupported_version(connection, msg, SETTINGS_API_VERSION)
        return
    entry_id = msg.get("charger_id")
    try:
        settings = await async_update_settings(
            hass,
            entry_id,
            expected_revision=msg.get("expected_revision"),
            replacement=msg.get("settings"),
        )
    except SettingsRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, "That charger is not available")
        return
    except SettingsReconcileError as failure:
        connection.send_result(msg["id"], settings_failure(failure.code, failure.settings))
        return
    except SettingsNotCommitted as failure:
        # Nothing written, old record stands: a contract envelope (successful frame) since the
        # request itself was valid.
        connection.send_result(msg["id"], settings_failure(failure.code, failure.settings))
        return
    except AutoSettingsError as refusal:
        store = domain_data(hass).auto_store
        current = None if store is None else store.settings(entry_id)
        if current is None:
            connection.send_error(msg["id"], refusal.code, "Settings were refused")
            return
        connection.send_result(msg["id"], settings_failure(refusal.code, current))
        return
    connection.send_result(msg["id"], settings_envelope(settings))


@callback
def async_setup_settings_api(hass: HomeAssistant) -> None:
    """Register both settings commands once for the domain, not once per config entry."""
    websocket_api.async_register_command(hass, websocket_get_settings)
    websocket_api.async_register_command(hass, websocket_update_settings)
