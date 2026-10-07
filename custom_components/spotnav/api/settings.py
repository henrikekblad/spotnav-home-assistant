"""The one canonical settings contract: one public value, one strict codec, two transports.

`AutoSettings` is a storage record and `AutoSettingsStore` its only writer; this module is the
public face, so nothing here serializes `as_dict()` or accepts `from_stored()` publicly.

* Full replacement, never a patch: "absent" has exactly one meaning, and the client states every
  value it wants.
* Two additive exceptions, `departure_date` and `departure_weekdays`: a replacement body may leave either out (an
  older client), and then the stored value is kept; `departure_date: null` clears the date. Every other key is
  still required.
* A third, `notifications` (which phones hear about which events, `notifications/settings.py`): left
  out, the stored choice is kept. Its `available` list (the notify services that exist now, with the
  phones' names) is read-only: a body may echo it and it is ignored.
* A fourth, `fill_to_limit` ("Fill": the manual need is the battery's room at each calculation): left out, the
  stored choice is kept, unless the same body moves `requested_kwh` (a client that does not know the field chose
  an amount, and an amount is not "fill"); then it is cleared.
* Two more, `vehicle_ids` (the vehicles that can charge here, `null` for every detected one; it also limits
  the vehicle this charger plans for) and `identify_mode` (`automatic`, `ask` or `off`: how the plugged-in
  vehicle is found): left out, the stored values are kept. The target follows the vehicle: it is the car's
  own (`vehicle_properties`), and a body that switches `target.vehicle_id` and leaves `target.target_percent`
  as it was gets the new vehicle's target (`vehicles/vehicle_target.py`).
* And `identify_camera` (`null`, or the camera, AI Task entity and frame that help tell which car is plugged in,
  `vehicles/camera_settings.py`): left out, the stored value is kept; a bad value is `invalid_camera`.
* One read-only fact, `fiscal_included`: the fiscal components (`vat`, `tax`, `transfer`) the selected area's
  published price already contains (contract v2's `included`). They are locked as "included in the price" and
  nothing is added for them, whatever the overrides say. A body may echo it; it is never stored.
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
from datetime import date, datetime, time
from typing import Any, Callable, Final, Mapping

from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant
from homeassistant.util import dt as dt_util

from ..planning.auto_controller import SettingsReconcileError
from ..planning.auto_settings import (
    ALL_WEEKDAYS,
    AreaAutoSettings,
    AutoSettings,
    AutoSettingsError,
    FiscalOverride,
    IDENTIFY_AUTOMATIC,
    IDENTIFY_MODES,
    MAX_PERIODS,
    PauseIntent,
    SettingsCode,
    STORED_STRATEGIES,
    TargetSocIntent,
)
from ..notifications.settings import NotificationSettings, NotificationSettingsError
from ..notifications.targets import available_targets, encoded_available
from ..planning.phases import effective_phases
from ..runtime import controller_for, domain_data, preview_for
from ..vehicles import vehicle_properties
from ..vehicles.camera_settings import CameraSettings, CameraSettingsError
from .common import ERROR_CHARGER_UNLOADED, lookup_charger, send_unsupported_version


#: The wire contract's own version, separate from the storage schema, the dashboard's and the
#: site settings'.
SETTINGS_API_VERSION: Final = 1

#: Every key of the settings record, without `revision`.
SETTINGS_KEYS: Final = frozenset(
    {
        "strategy",
        "area_id",
        "overrides",
        "phases",
        "amps",
        "requested_kwh",
        "fill_to_limit",
        "max_periods",
        "departure_enabled",
        "departure_time",
        "departure_date",
        "departure_weekdays",
        "driver",
        "target",
        "notifications",
        "vehicle_ids",
        "identify_mode",
        "identify_camera",
    }
)

#: Keys a replacement body may leave out (added after the first release of this contract). Absent means "keep
#: what is stored": a client that does not know the field must not clear what another one set. `null` clears.
OPTIONAL_SETTINGS_KEYS: Final = frozenset(
    {
        "departure_date", "departure_weekdays", "phases", "notifications", "fill_to_limit", "vehicle_ids",
        "identify_mode", "identify_camera",
    }
)

#: `phases` joins them since the phases a charge uses stopped being a setting (the charger's wiring and the
#: vehicle's onboard charger decide them): an older client still sends it, a newer one may leave it out,
#: and either way it is ignored. A response carries the effective value in it, for the older clients.
#: The keys a replacement body must carry; the rest of `SETTINGS_KEYS` may be left out.
REQUIRED_SETTINGS_KEYS: Final = SETTINGS_KEYS - OPTIONAL_SETTINGS_KEYS

#: Facts a record carries that no body states: echoed back by a client, they are accepted and ignored.
READ_ONLY_SETTINGS_KEYS: Final = frozenset({"fiscal_included"})

SETTINGS_RESPONSE_KEYS: Final = SETTINGS_KEYS | {"revision"} | READ_ONLY_SETTINGS_KEYS

#: The settings' fiscal components, in record order, and the names contract v2's `included` gives them.
FISCAL_COMPONENT_NAMES: Final = (("vat", "vat"), ("tax", "tax"), ("transfer", "grid_fee"))

OVERRIDE_KEYS: Final = frozenset({"area_id", "vat", "tax", "transfer"})
FISCAL_KEYS: Final = frozenset({"enabled", "value"})
TARGET_KEYS: Final = frozenset({"vehicle_id", "target_percent"})

#: The read-only pause observation: the persisted record, or three nulls.
PAUSE_KEYS: Final = frozenset({"choice", "admitted_at", "expires_at"})

SETTINGS_ENVELOPE_KEYS: Final = frozenset({"api_version", "ok", "error", "settings", "pause"})

WALL_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

NOT_A_BODY: Final[SettingsCode] = "unknown_field"


def _refuse(code: SettingsCode, message: str) -> None:
    raise AutoSettingsError(code, message)


def _object(
    raw: Any, keys: frozenset[str], what: str, optional: frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    """An exact-shape object: every key present, none unknown, nothing coerced.

    `optional` keys may be left out and are not unknown when present.
    """
    if not isinstance(raw, Mapping):
        _refuse(NOT_A_BODY, f"{what} must be an object")
    missing = keys - set(raw)
    if missing:
        _refuse("missing_field", f"{what} is missing {sorted(missing)}")
    unknown = set(raw) - keys - optional
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


def departure_date_from_wire(value: Any) -> date | None:
    """`"YYYY-MM-DD"` as a calendar date, `null` as no date, or a refusal. Never a datetime, never coerced."""
    if value is None:
        return None
    if not isinstance(value, str) or not ISO_DATE.match(value):
        _refuse("invalid_departure", "departure_date must be YYYY-MM-DD or null")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _refuse("invalid_departure", "departure_date is not a calendar date")
    raise AssertionError("unreachable")  # pragma: no cover


def departure_date_to_wire(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def departure_weekdays_from_wire(value: Any) -> tuple[int, ...]:
    """A list of ISO weekday numbers, 1 (Monday) to 7 (Sunday), as a sorted tuple, or a refusal.

    At least one, no repeats, whole numbers only (a boolean is not one), nothing coerced.
    """
    if not isinstance(value, list) or not value:
        _refuse("invalid_departure", "departure_weekdays must be a non-empty list of weekdays")
    days = []
    for day in value:
        if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7:
            _refuse("invalid_departure", "departure_weekdays holds whole numbers from 1 (Monday) to 7 (Sunday)")
        days.append(day)
    if len(set(days)) != len(days):
        _refuse("invalid_departure", "departure_weekdays must not repeat a weekday")
    return tuple(sorted(days))


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


def fiscal_included_for(hass: HomeAssistant, settings: AutoSettings | None) -> tuple[str, ...]:
    """The fiscal components the record's selected area already includes in its price, from the held
    catalogue; none without an area, a catalogue, or a v2 `included` list."""
    repository = domain_data(hass).price_repository
    if settings is None or settings.area_id is None or repository is None:
        return ()
    entry = repository.catalogue_snapshot().area(settings.area_id)
    return included_components(entry)


def included_components(entry: Any) -> tuple[str, ...]:
    """`vat`, `tax` and `transfer`, in record order, for each part an area's published price includes."""
    if entry is None:
        return ()
    return tuple(component for component, name in FISCAL_COMPONENT_NAMES if name in entry.included)


def encode_notifications(
    notifications: NotificationSettings, available: tuple[tuple[str, str], ...] = ()
) -> dict[str, Any]:
    """The `notifications` field: the stored choice and the read-only `available` services."""
    return {**notifications.as_dict(), "available": encoded_available(available)}


def decode_notifications(raw: Any) -> NotificationSettings:
    try:
        return NotificationSettings.from_dict(raw)
    except NotificationSettingsError as err:
        _refuse("invalid_notifications", str(err))
    raise AssertionError("unreachable")  # pragma: no cover


def encode_settings(
    settings: AutoSettings,
    phases: int | None = None,
    included: tuple[str, ...] = (),
    available: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """The public value: every user-owned planning input, and the record's revision.

    `phases` is the effective phases a charge uses (`planning/phases.py`), which is what the key carries
    for an older client; the stored field is only an older release's leftover and is never read.
    `included` is the read-only `fiscal_included` (see the module docstring), `available` the notify
    services `notifications.available` lists.
    """
    return {
        "revision": settings.revision,
        "strategy": strategy_of(settings),
        "area_id": settings.area_id,
        "overrides": [encoded_override(item) for item in settings.overrides],
        "phases": phases,
        "amps": settings.amps,
        "requested_kwh": settings.requested_kwh,
        "fill_to_limit": settings.fill_to_limit,
        "max_periods": settings.max_periods,
        "departure_enabled": settings.departure_enabled,
        "departure_time": departure_to_wire(settings.departure),
        "departure_date": departure_date_to_wire(settings.departure_date),
        "departure_weekdays": list(settings.departure_weekdays),
        "driver": settings.driver,
        "target": encoded_target(settings.target),
        "fiscal_included": list(included),
        "notifications": encode_notifications(settings.notifications, available),
        "vehicle_ids": None if settings.vehicle_ids is None else list(settings.vehicle_ids),
        "identify_mode": settings.identify_mode,
        "identify_camera": None if settings.identify_camera is None else settings.identify_camera.as_dict(),
    }


def vehicle_ids_from_wire(value: Any) -> tuple[str, ...] | None:
    """`null`, or a non-empty list of different vehicle ids, or a refusal."""
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        _refuse("invalid_vehicles", "vehicle_ids must be null or a non-empty list of vehicle ids")
    return tuple(value)


def camera_from_wire(value: Any) -> CameraSettings | None:
    """`null` (no camera) or `{camera_entity_id, ai_task_entity_id, frame}`, or a refusal (`invalid_camera`)."""
    try:
        return CameraSettings.from_wire(value)
    except CameraSettingsError as err:
        _refuse("invalid_camera", str(err))
    return None


def _enum(value: Any, allowed: tuple[str, ...], code: SettingsCode) -> Any:
    if not isinstance(value, str) or value not in allowed:
        _refuse(code, f"{value!r} is not one of {allowed}")
    return value


def decode_settings(raw: Any) -> AutoSettings:
    """A full replacement, decoded to a validated `AutoSettings`, or a refusal by stable code.

    Every key is required and every type exact; the result passes the same `AutoSettings.validated()`
    the store uses. The pause is never part of a body (see `replacement_mutator`).
    """
    stored = _object(raw, REQUIRED_SETTINGS_KEYS, "settings", OPTIONAL_SETTINGS_KEYS | READ_ONLY_SETTINGS_KEYS)
    if "fiscal_included" in stored and not isinstance(stored["fiscal_included"], list):
        _refuse("invalid_fiscal", "fiscal_included is read-only, and a list when it is echoed")
    overrides = stored["overrides"]
    if not isinstance(overrides, list):
        _refuse("invalid_area", "overrides must be a list")
    strategy = stored["strategy"]
    if not isinstance(strategy, str) or strategy not in STORED_STRATEGIES:
        _refuse("invalid_strategy", f"strategy must be one of {STORED_STRATEGIES}")
    if stored.get("phases") is not None:
        # Accepted and ignored (see `OPTIONAL_SETTINGS_KEYS`): checked for what it is, never stored.
        if _whole(stored["phases"], "phases", "invalid_phases") not in (1, 3):
            _refuse("invalid_phases", "phases must be 1 or 3 when it is set")
    return AutoSettings(
        revision=0,
        strategy=strategy,
        area_id=_text_or_none(stored["area_id"], "area_id", "invalid_area"),
        overrides=tuple(_override(item) for item in overrides),
        amps=None if stored["amps"] is None else _whole(stored["amps"], "amps", "invalid_amps"),
        requested_kwh=_finite(stored["requested_kwh"], "requested_kwh", "invalid_energy"),
        fill_to_limit=(
            _boolean(stored["fill_to_limit"], "fill_to_limit", "invalid_energy") if "fill_to_limit" in stored else False
        ),
        # `null` is automatic periods; a number is a hard cap (1 to 8, `AutoSettings.validated`).
        max_periods=(
            None if stored["max_periods"] is None else _whole(stored["max_periods"], "max_periods", "invalid_periods")
        ),
        departure_enabled=_boolean(
            stored["departure_enabled"], "departure_enabled", "invalid_departure"
        ),
        departure=departure_from_wire(stored["departure_time"]),
        departure_date=departure_date_from_wire(stored.get("departure_date")),
        departure_weekdays=(
            departure_weekdays_from_wire(stored["departure_weekdays"])
            if "departure_weekdays" in stored
            else ALL_WEEKDAYS
        ),
        driver=_enum(stored["driver"], ("manual_kwh", "target_soc"), "invalid_driver"),
        target=_target(stored["target"]),
        notifications=(
            decode_notifications(stored["notifications"]) if "notifications" in stored else NotificationSettings()
        ),
        vehicle_ids=vehicle_ids_from_wire(stored.get("vehicle_ids")),
        identify_mode=(
            _enum(stored["identify_mode"], IDENTIFY_MODES, "invalid_vehicles")
            if "identify_mode" in stored
            else IDENTIFY_AUTOMATIC
        ),
        identify_camera=camera_from_wire(stored.get("identify_camera")),
    ).validated()


def replacement_mutator(
    replacement: AutoSettings,
    *,
    keep_departure_date: bool = False,
    keep_departure_weekdays: bool = False,
    keep_notifications: bool = False,
    keep_fill_to_limit: bool = False,
    keep_vehicle_ids: bool = False,
    keep_identify_mode: bool = False,
    keep_identification: bool = False,
    keep_identify_camera: bool = False,
    keep_camera_choice: bool = False,
    keep_auto_periods: bool = False,
    vehicle_target: Callable[[str | None], float | None] | None = None,
) -> Callable[[AutoSettings], AutoSettings]:
    """A full replacement expressed as the store's own mutation hook.

    Every planning field comes from the replacement and the pause exactly as stored, in one atomic
    carry-through, so an edit never clears, admits or re-times a pause (including an expired but
    uncleared one). The store owns the revision increment and the final validation.

    `keep_camera_choice` (the paired app) keeps the stored camera and AI Task entity and takes only the frame.
    `keep_identification` keeps the identification fields (`vehicle_ids`, `identify_mode`, `identify_camera`).
    `keep_auto_periods` (an app that does not read automatic periods, which was shown `MAX_PERIODS` in their
    place) keeps automatic periods when the body echoes that number; any other number is the person's.
    Whatever the body
    says, a switch of the target vehicle that leaves the percent as it was takes the new vehicle's own target
    (`vehicle_target`, the car's property): the target follows the car.
    """
    keep_vehicle_ids = keep_vehicle_ids or keep_identification
    keep_identify_mode = keep_identify_mode or keep_identification
    keep_identify_camera = keep_identify_camera or keep_identification

    def mutate(current: AutoSettings) -> AutoSettings:
        kept: dict[str, Any] = {}
        if keep_departure_date:
            # The body did not mention `departure_date`: an older client, which leaves it as it is.
            kept["departure_date"] = current.departure_date
        if keep_departure_weekdays:
            kept["departure_weekdays"] = current.departure_weekdays
        if keep_notifications:
            kept["notifications"] = current.notifications
        if keep_fill_to_limit:
            # Kept only while the amount stays: a body that moves it chose an amount, which is not "fill".
            kept["fill_to_limit"] = current.fill_to_limit and replacement.requested_kwh == current.requested_kwh
        if keep_vehicle_ids:
            kept["vehicle_ids"] = current.vehicle_ids
        if keep_identify_mode:
            kept["identify_mode"] = current.identify_mode
        if keep_identify_camera:
            kept["identify_camera"] = current.identify_camera
        elif keep_camera_choice:
            # The paired app may move the frame, never choose the camera or the AI Task entity (an administrator's).
            chosen_camera = current.identify_camera
            sent = replacement.identify_camera
            kept["identify_camera"] = (
                None if chosen_camera is None else replace(chosen_camera, frame=None if sent is None else sent.frame)
            )
        if keep_auto_periods and current.max_periods is None and replacement.max_periods == MAX_PERIODS:
            kept["max_periods"] = None
        updated = replace(replacement, pause=current.pause, **kept)
        chosen = updated.target
        remembered = None if vehicle_target is None else vehicle_target(chosen.vehicle_id)
        if (
            chosen.vehicle_id != current.target.vehicle_id
            and chosen.target_percent == current.target.target_percent
            and remembered is not None
        ):
            # Only the car changed: plan for its own target, not the one the other car had.
            updated = updated.with_target_vehicle(chosen.vehicle_id, remembered)
        return updated

    return mutate


def settings_version_of(value: Any) -> int | None:
    """The settings version a request named, or `None` when this release does not speak it."""
    if isinstance(value, bool):
        return None
    return value if value == SETTINGS_API_VERSION else None


def encode_pause(intent: PauseIntent) -> dict[str, Any]:
    """The typed pause observation: what the persisted record contains, or three nulls.

    Deliberately the stored intent, not the clock's view: this is what the execution gate obeys. A manual
    pause (a person's Start or Stop) adds `action` (`start`/`stop`) and `scope` (`plug_in`/`next_plug_in`);
    every other pause is the three keys it always was.
    """
    return intent.as_dict()


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


def settings_envelope(
    settings: AutoSettings,
    phases: int | None = None,
    included: tuple[str, ...] = (),
    available: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """The transport-neutral success answer: the committed record and its pause, no exception text."""
    return {
        "api_version": SETTINGS_API_VERSION,
        "ok": True,
        "error": None,
        "settings": encode_settings(settings, phases, included, available),
        "pause": encode_pause(settings.pause),
    }


def settings_failure(
    code: str,
    settings: AutoSettings | None,
    phases: int | None = None,
    included: tuple[str, ...] = (),
    available: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """The transport-neutral refusal: a stable code beside the settings that still stand.

    A conflict carries the current record so a caller can retry against it. `settings` is `None` only
    when nothing is readable (an unresolvable entry). Typed `str` because entry-level refusals carry
    `spotnav_*` codes.
    """
    return {
        "api_version": SETTINGS_API_VERSION,
        "ok": False,
        "error": code,
        "settings": None if settings is None else encode_settings(settings, phases, included, available),
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


def _refuse_camera_choice(stored: CameraSettings | None, sent: CameraSettings | None) -> None:
    """The paired app may echo the camera and move its frame; choosing the camera or the AI Task entity (where the
    pictures go) is an administrator's, in the card."""
    same = (stored is None and sent is None) or (
        stored is not None
        and sent is not None
        and stored.camera_entity_id == sent.camera_entity_id
        and stored.ai_task_entity_id == sent.ai_task_entity_id
    )
    if not same:
        _refuse("invalid_camera", "the camera and the AI Task entity are chosen by an administrator")


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
    from_app: bool = False,
    reads_auto_periods: bool = True,
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
    if from_app and isinstance(replacement, Mapping) and "identify_camera" in replacement:
        _refuse_camera_choice(store.settings(entry_id).identify_camera, decoded.identify_camera)
    mutate = replacement_mutator(
        decoded,
        keep_departure_date=isinstance(replacement, Mapping) and "departure_date" not in replacement,
        keep_departure_weekdays=isinstance(replacement, Mapping) and "departure_weekdays" not in replacement,
        keep_notifications=isinstance(replacement, Mapping) and "notifications" not in replacement,
        keep_fill_to_limit=isinstance(replacement, Mapping) and "fill_to_limit" not in replacement,
        keep_vehicle_ids=isinstance(replacement, Mapping) and "vehicle_ids" not in replacement,
        keep_identify_mode=isinstance(replacement, Mapping) and "identify_mode" not in replacement,
        keep_identify_camera=isinstance(replacement, Mapping) and "identify_camera" not in replacement,
        keep_camera_choice=from_app,
        keep_auto_periods=not reads_auto_periods,
        vehicle_target=lambda vehicle_id: vehicle_properties.stored_properties(hass, vehicle_id).target_percent,
    )
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
    connection.send_result(msg["id"], settings_envelope(settings, effective_phases(hass, msg["charger_id"]), fiscal_included_for(hass, settings), available_targets(hass)))


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
        connection.send_result(
            msg["id"], settings_failure(
                failure.code, failure.settings, effective_phases(hass, entry_id),
                fiscal_included_for(hass, failure.settings), available_targets(hass),
            )
        )
        return
    except SettingsNotCommitted as failure:
        # Nothing written, old record stands: a contract envelope (successful frame) since the
        # request itself was valid.
        connection.send_result(
            msg["id"], settings_failure(
                failure.code, failure.settings, effective_phases(hass, entry_id),
                fiscal_included_for(hass, failure.settings), available_targets(hass),
            )
        )
        return
    except AutoSettingsError as refusal:
        store = domain_data(hass).auto_store
        current = None if store is None else store.settings(entry_id)
        if current is None:
            connection.send_error(msg["id"], refusal.code, "Settings were refused")
            return
        connection.send_result(
            msg["id"], settings_failure(
                refusal.code, current, effective_phases(hass, entry_id), fiscal_included_for(hass, current),
                available_targets(hass),
            )
        )
        return
    connection.send_result(msg["id"], settings_envelope(settings, effective_phases(hass, msg["charger_id"]), fiscal_included_for(hass, settings), available_targets(hass)))


@callback
def async_setup_settings_api(hass: HomeAssistant) -> None:
    """Register both settings commands once for the domain, not once per config entry."""
    websocket_api.async_register_command(hass, websocket_get_settings)
    websocket_api.async_register_command(hass, websocket_update_settings)
