"""Auto settings: what a person chose, stored once for the whole installation.

One HA `Store` document keyed by charger config-entry id, holding a frozen typed
record per charger. It validates, stores and hands out settings; nothing here calculates.

* The store owns intent, not live state: `target_soc` stores which vehicle and what
  target; the live reading and capacity arrive with the proposal calculation.
* Fiscal values have three states: off, on with an explicit override, or on using the
  area catalogue's suggestion. `None` is not zero.
* Overrides live per area inside the charger's record, so switching SE4 to FI never
  applies Swedish öre values in Finland, and switching back finds the SE4 choice.
* Stored JSON is untrusted: every record is read in exactly the shape `as_dict` writes
  (all keys, none unknown, no coercion) and refused by name otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from typing import Any, Callable, Final, Literal

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..runtime import domain_data


_LOGGER = logging.getLogger(__name__)

#: Stored document schema, also HA's storage envelope version.
STORAGE_VERSION: Final = 1

STORAGE_KEY: Final = f"{DOMAIN}_auto_settings"
#: Schemas this release reads and writes.
SUPPORTED_SCHEMAS: Final = (1,)
EnergyDriver = Literal["manual_kwh", "target_soc"]

#: What a person may mean by "stop for now"; resolved to an instant (or none) at admission.
PauseChoice = Literal["next_period", "until_tomorrow", "until_resumed"]
PAUSE_NEXT_PERIOD: Final = "next_period"
PAUSE_UNTIL_TOMORROW: Final = "until_tomorrow"
PAUSE_UNTIL_RESUMED: Final = "until_resumed"
PAUSE_CHOICES: Final = (PAUSE_NEXT_PERIOD, PAUSE_UNTIL_TOMORROW, PAUSE_UNTIL_RESUMED)

DRIVER_MANUAL_KWH: Final = "manual_kwh"
DRIVER_TARGET_SOC: Final = "target_soc"

#: Charging strategy: `cheapest` (price planner), `solar` (surplus only) or `hybrid`
#: (cheapest, holding back grid energy for forecast sun).
STRATEGY_CHEAPEST: Final = "cheapest"
STRATEGY_SOLAR: Final = "solar"
STRATEGY_HYBRID: Final = "hybrid"
STORED_STRATEGIES: Final = (STRATEGY_CHEAPEST, STRATEGY_SOLAR, STRATEGY_HYBRID)

#: Visible defaults for values that are not safety-sensitive. Phases, current, area,
#: vehicle, capacity and fiscal figures have none: they are `None` or off.
DEFAULT_REQUESTED_KWH: Final = 20.0
DEFAULT_CONSUMPTION_KWH_PER_10KM: Final = 2.0
DEFAULT_MAX_PERIODS: Final = 1
DEFAULT_DEPARTURE: Final = time(8, 0)

SettingsCode = Literal[
    "invalid_driver",
    "invalid_area",
    "invalid_phases",
    "invalid_amps",
    "invalid_energy",
    "invalid_periods",
    "invalid_departure",
    "invalid_fiscal",
    "invalid_target",
    "invalid_number",
    "unknown_field",
    "missing_field",
    "invalid_proposal",
    "invalid_pause",
    "invalid_strategy",
    "revision_conflict",
    "invalid_energy_baseline",
]


class AutoSettingsError(ValueError):
    """A settings value that cannot be stored as it stands.

    [code] is the program-facing fact; the message is for the log. Nothing here clamps.
    """

    def __init__(self, code: SettingsCode, message: str) -> None:
        super().__init__(message)
        self.code: SettingsCode = code


def _refuse(code: SettingsCode, message: str) -> None:
    raise AutoSettingsError(code, message)


def _exact_shape(raw: Any, expected: frozenset[str], code: SettingsCode, what: str) -> dict[str, Any]:
    """A stored record carrying exactly these keys, or a refusal naming the wrong shape.

    `as_dict` is the only writer and emits every key, so a missing or unknown key means
    the record is not this schema's. Which keys are required is defined by `as_dict` itself.
    """
    if not isinstance(raw, dict):
        _refuse(code, f"{what} must be a stored object")
    missing = expected - set(raw)
    if missing:
        # Counted, not listed: a log line about stored data must not quote it.
        _refuse("missing_field", f"{what} is missing {len(missing)} field(s) of its shape")
    unknown = set(raw) - expected
    if unknown:
        _refuse("unknown_field", f"{what} has {len(unknown)} field(s) this release does not know")
    return raw


def _finite(value: Any, code: SettingsCode, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(code, f"{what} must be a number")
    number = float(value)
    if not math.isfinite(number):
        _refuse(code, f"{what} must be finite")
    return number


#: States and reasons a stored proposal may carry; it is only written for a calculated plan
#: (see `auto_controller.AutoState`).
PROPOSAL_STATES: Final = ("proposal_ready", "proposal_unpriced")
PROPOSAL_REASONS: Final = (
    "ready",
    "unpriced",
    # Price-wait outcomes: energy bought before publication, and charging without prices.
    "buying_before_publication",
    "charging_without_prices",
)
#: Which stored reasons each unpriced flag admits.
_PROPOSAL_REASONS_BY_FLAG: Final = {
    False: ("ready", "buying_before_publication"),
    True: ("unpriced", "charging_without_prices"),
}


def _stored_count(value: Any, what: str) -> int:
    """A stored whole number: not a boolean, not a string, not a negative."""
    if isinstance(value, bool) or not isinstance(value, int):
        _refuse("invalid_proposal", f"a stored proposal's {what} must be a whole number")
    if value < 0:
        _refuse("invalid_proposal", f"a stored proposal's {what} must not be negative")
    return value


def _stored_number(value: Any, what: str, *, minimum: float | None = None) -> float:
    """A stored number: not a boolean, not a string, finite, and inside any stated floor."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("invalid_proposal", f"a stored proposal's {what} must be a number")
    number = float(value)
    if not math.isfinite(number):
        _refuse("invalid_proposal", f"a stored proposal's {what} must be finite")
    if minimum is not None and number < minimum:
        _refuse("invalid_proposal", f"a stored proposal's {what} must not be below {minimum}")
    return number


def _stored_instants(values: Any, what: str) -> tuple[datetime, ...]:
    """A stored list of aware ISO instants, or a refusal."""
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        _refuse("invalid_proposal", f"a stored proposal's {what} must be a list of timestamps")
    parsed: list[datetime] = []
    for item in values:
        moment = dt_util.parse_datetime(item)
        if moment is None or moment.tzinfo is None:
            _refuse("invalid_proposal", f"a stored proposal's {what} must be aware timestamps")
        parsed.append(moment)
    return tuple(parsed)


def _positive(value: Any, code: SettingsCode, what: str) -> float:
    number = _finite(value, code, what)
    if number <= 0:
        _refuse(code, f"{what} must be positive")
    return number


@dataclass(frozen=True, slots=True)
class FiscalOverride:
    """One cost component: off, or on with an explicit value, or on with none.

    `enabled=True, value=None` means "use the area catalogue's suggestion"; it must not
    be stored as zero, because zero is a figure somebody typed.
    """

    enabled: bool = False
    value: float | None = None

    def validated(self, what: str) -> FiscalOverride:
        if not isinstance(self.enabled, bool):
            _refuse("invalid_fiscal", f"{what}.enabled must be a boolean")
        if self.value is None:
            return self
        number = _finite(self.value, "invalid_fiscal", f"{what}.value")
        if number < 0:
            _refuse("invalid_fiscal", f"{what}.value must not be negative")
        return FiscalOverride(enabled=self.enabled, value=number)

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "value": self.value}

    @classmethod
    def from_stored(cls, raw: Any, what: str) -> FiscalOverride:
        """This component as stored, or a refusal, never a coerced boolean.

        `bool("false")` is `True`, and "on" makes a fee part of a price somebody is charged.
        """
        stored = _exact_shape(raw, frozenset({"enabled", "value"}), "invalid_fiscal", what)
        enabled = stored["enabled"]
        if not isinstance(enabled, bool):
            _refuse("invalid_fiscal", f"{what}.enabled must be a stored boolean")
        return cls(enabled=enabled, value=stored["value"]).validated(what)


@dataclass(frozen=True, slots=True)
class AreaAutoSettings:
    """One area's fiscal overrides, inside one charger's record.

    Per area because fee figures are in that area's currency; carrying a value across an
    area change would apply öre in Finland.
    """

    area_id: str
    vat: FiscalOverride = FiscalOverride()
    tax: FiscalOverride = FiscalOverride()
    transfer: FiscalOverride = FiscalOverride()

    def validated(self) -> AreaAutoSettings:
        if not isinstance(self.area_id, str) or not self.area_id:
            _refuse("invalid_area", "an area override needs an area id")
        return AreaAutoSettings(
            area_id=self.area_id,
            vat=self.vat.validated(f"vat for {self.area_id}"),
            tax=self.tax.validated(f"tax for {self.area_id}"),
            transfer=self.transfer.validated(f"transfer for {self.area_id}"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "vat": self.vat.as_dict(),
            "tax": self.tax.as_dict(),
            "transfer": self.transfer.as_dict(),
        }

    @classmethod
    def from_stored(cls, raw: Any) -> AreaAutoSettings:
        """This area override as stored, or a refusal: no field is invented."""
        stored = _exact_shape(
            raw,
            frozenset({"area_id", "vat", "tax", "transfer"}),
            "invalid_area",
            "a stored area override",
        )
        return cls(
            area_id=stored["area_id"],
            vat=FiscalOverride.from_stored(stored["vat"], "vat"),
            tax=FiscalOverride.from_stored(stored["tax"], "tax"),
            transfer=FiscalOverride.from_stored(stored["transfer"], "transfer"),
        ).validated()


@dataclass(frozen=True, slots=True)
class TargetSocIntent:
    """What a person chose about charging to a state of charge, and nothing live.

    Which vehicle and what target. The vehicle's current state of charge is absent: it is
    not intent, and storing it would bump the revision whenever the car reported.
    """

    vehicle_id: str | None = None
    target_percent: float | None = None

    def validated(self) -> TargetSocIntent:
        if self.vehicle_id is not None and (not isinstance(self.vehicle_id, str) or not self.vehicle_id):
            _refuse("invalid_target", "vehicle_id must be a non-empty string or absent")
        target = self.target_percent
        if target is not None:
            target = _finite(target, "invalid_target", "target_percent")
            if not 0 <= target <= 100:
                _refuse("invalid_target", "target_percent must be between 0 and 100")
        return TargetSocIntent(vehicle_id=self.vehicle_id, target_percent=target)

    def as_dict(self) -> dict[str, Any]:
        return {"vehicle_id": self.vehicle_id, "target_percent": self.target_percent}

    @classmethod
    def from_stored(cls, raw: Any) -> TargetSocIntent:
        """This target as stored, or a refusal: an absent field is not "the default"."""
        stored = _exact_shape(
            raw,
            frozenset({"vehicle_id", "target_percent"}),
            "invalid_target",
            "a stored target",
        )
        return cls(
            vehicle_id=stored["vehicle_id"], target_percent=stored["target_percent"]
        ).validated()


@dataclass(frozen=True, slots=True)
class PauseIntent:
    """A manual override that suspends Auto's execution, and the instant it ends.

    Choices: until the next planned period, until tomorrow (next local midnight in the
    market zone), or until resumed. A bounded choice is resolved by the execution boundary
    to one aware instant at admission, so expiry is a comparison against the clock and a
    restart resumes the same decision. `admitted_at` records when it began.
    """

    choice: PauseChoice | None = None
    admitted_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def admitted(self) -> bool:
        """Whether a pause is admitted at all, whatever its expiry now says."""
        return self.choice is not None

    def ended_at(self, now: datetime) -> bool:
        """Whether this pause's own expiry instant has been reached."""
        return self.expires_at is not None and now >= self.expires_at

    def is_active_at(self, now: datetime) -> bool:
        """Whether this pause still suspends execution at [now].

        An expired-but-uncleared intent is not active; the record is cleared when the execution
        boundary next settles it (`AutoExecutor.async_settle_pause`).
        """
        return self.admitted and not self.ended_at(now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "admitted_at": None if self.admitted_at is None else self.admitted_at.isoformat(),
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
        }

    @classmethod
    def from_stored(cls, raw: Any) -> PauseIntent:
        """A stored pause intent, or a refusal: every field checked, none coerced."""
        stored = _exact_shape(
            raw,
            frozenset(cls().as_dict()),
            "invalid_pause",
            "a stored pause intent",
        )
        choice = stored["choice"]
        if choice is not None and choice not in PAUSE_CHOICES:
            _refuse("invalid_pause", "a stored pause choice is not one this release knows")
        return cls(
            choice=choice,
            admitted_at=_stored_instant(stored["admitted_at"], "admitted_at"),
            expires_at=_stored_instant(stored["expires_at"], "expires_at"),
        ).validated()

    def validated(self) -> PauseIntent:
        """The same intent, or a refusal naming what cannot be true of a pause."""
        if self.choice is not None and self.choice not in PAUSE_CHOICES:
            _refuse("invalid_pause", f"{self.choice!r} is not a pause choice this release knows")
        for moment, what in ((self.admitted_at, "admitted_at"), (self.expires_at, "expires_at")):
            if moment is not None and moment.tzinfo is None:
                _refuse("invalid_pause", f"a pause's {what} must be timezone-aware")
        if self.choice is None:
            if self.expires_at is not None or self.admitted_at is not None:
                _refuse("invalid_pause", "an absent pause carries no instants")
            return self
        if self.choice == PAUSE_UNTIL_RESUMED:
            if self.expires_at is not None:
                # "Until I resume" has no expiry; storing one would silently end it.
                _refuse("invalid_pause", "an indefinite pause cannot carry an expiry")
            return self
        if self.expires_at is None:
            _refuse("invalid_pause", f"a {self.choice} pause needs the instant it ends at")
        if self.admitted_at is not None and self.expires_at <= self.admitted_at:
            _refuse("invalid_pause", "a pause must end after it was admitted")
        return self


def _stored_instant(value: Any, what: str) -> datetime | None:
    """A stored timestamp, or `None`, or a refusal: never a naive datetime."""
    if value is None:
        return None
    if not isinstance(value, str):
        _refuse("invalid_pause", f"a stored {what} must be a timestamp string or null")
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        _refuse("invalid_pause", f"a stored {what} must be a timezone-aware timestamp")
    return parsed


@dataclass(frozen=True, slots=True)
class AutoSettings:
    """One charger's Auto settings, as stored. Frozen, so a revision is a new value.

    Fields that could steer a charge have no default unless it cannot be unsafe: energy,
    period cap and departure do; phases, current, area, vehicle and fiscal figures do not.
    """

    revision: int = 0
    area_id: str | None = None
    overrides: tuple[AreaAutoSettings, ...] = ()
    phases: int | None = None
    amps: int | None = None
    requested_kwh: float = DEFAULT_REQUESTED_KWH
    max_periods: int = DEFAULT_MAX_PERIODS
    departure_enabled: bool = True
    departure: time = DEFAULT_DEPARTURE
    #: Suspends automatic execution; a paused charger keeps calculating. Whether it is
    #: still in force is answered by `pause.is_active_at(now)`.
    pause: PauseIntent = field(default_factory=PauseIntent)
    driver: EnergyDriver = DRIVER_MANUAL_KWH
    target: TargetSocIntent = field(default_factory=TargetSocIntent)
    #: What the system optimizes for (`STORED_STRATEGIES`).
    strategy: str = STRATEGY_CHEAPEST

    @property
    def execution_paused(self) -> bool:
        """Whether a pause is admitted, as the stored record states it.

        Whether it is still in force needs a clock and is not answered here.
        """
        return self.pause.admitted

    def override_for(self, area_id: str) -> AreaAutoSettings:
        """This area's overrides, or an all-off record when it has none.

        An all-off override means "add nothing", which is right for an area with no stored choice.
        """
        for overrides in self.overrides:
            if overrides.area_id == area_id:
                return overrides
        return AreaAutoSettings(area_id=area_id)

    def with_override(self, overrides: AreaAutoSettings) -> AutoSettings:
        """The same settings with one area's overrides replaced, others untouched."""
        kept = tuple(item for item in self.overrides if item.area_id != overrides.area_id)
        return replace(self, overrides=kept + (overrides.validated(),))

    def missing_for_auto(self) -> tuple[str, ...]:
        """Which Auto inputs are still absent, as stable names in a stable order.

        Fiscal components are not here: a missing catalogue suggestion is resolved during
        calculation and reported as its own reason.
        """
        missing: list[str] = []
        if not self.area_id:
            missing.append("area")
        if self.phases not in (1, 3):
            missing.append("phases")
        if not isinstance(self.amps, int) or isinstance(self.amps, bool) or self.amps <= 0:
            missing.append("amps")
        if self.driver == DRIVER_TARGET_SOC:
            if not self.target.vehicle_id:
                missing.append("vehicle")
            if self.target.target_percent is None:
                missing.append("target_percent")
        return tuple(missing)

    def validated(self) -> AutoSettings:
        """The same settings, or a refusal naming the first thing that cannot be stored.

        No clamping: a caller asking for 9 periods gets an error, not a different stored setting.
        """
        if self.driver not in (DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC):
            _refuse("invalid_driver", f"{self.driver!r} is not an energy driver this release knows")
        if self.strategy not in STORED_STRATEGIES:
            _refuse("invalid_strategy", f"{self.strategy!r} is not a strategy this release can store")
        if self.area_id is not None and (not isinstance(self.area_id, str) or not self.area_id):
            _refuse("invalid_area", "area_id must be a non-empty string or absent")
        if self.phases is not None and self.phases not in (1, 3):
            _refuse("invalid_phases", "phases must be 1 or 3 when it is set")
        if self.amps is not None:
            if isinstance(self.amps, bool) or not isinstance(self.amps, int) or not 1 <= self.amps <= 80:
                _refuse("invalid_amps", "amps must be a whole number of amperes when it is set")
        if isinstance(self.max_periods, bool) or not isinstance(self.max_periods, int) or not 1 <= self.max_periods <= 8:
            _refuse("invalid_periods", "max_periods must be a whole number between 1 and 8")
        _positive(self.requested_kwh, "invalid_energy", "requested_kwh")
        if not isinstance(self.departure, time) or self.departure.tzinfo is not None:
            _refuse("invalid_departure", "departure must be a local wall time")
        if not isinstance(self.departure_enabled, bool):
            _refuse("invalid_departure", "the departure switch must be a boolean")
        if not isinstance(self.execution_paused, bool):
            _refuse("invalid_pause", "execution_paused must be a boolean")
        seen: set[str] = set()
        for overrides in self.overrides:
            overrides.validated()
            if overrides.area_id in seen:
                _refuse("invalid_area", f"{overrides.area_id!r} has two override records")
            seen.add(overrides.area_id)
        if self.target is None:
            _refuse("invalid_target", "the target record must be present, even when empty")
        if self.pause is None:
            _refuse("invalid_pause", "the pause record must be present, even when absent")
        return replace(
            self,
            requested_kwh=float(self.requested_kwh),
            overrides=tuple(sorted(self.overrides, key=lambda item: item.area_id)),
            target=self.target.validated(),
            pause=self.pause.validated(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "area_id": self.area_id,
            "overrides": [item.as_dict() for item in self.overrides],
            "phases": self.phases,
            "amps": self.amps,
            "requested_kwh": self.requested_kwh,
            "max_periods": self.max_periods,
            "departure_enabled": self.departure_enabled,
            "departure": f"{self.departure.hour:02d}:{self.departure.minute:02d}",
            "pause": self.pause.as_dict(),
            "driver": self.driver,
            "target": self.target.as_dict(),
            "strategy": self.strategy,
        }

    @classmethod
    def from_stored(cls, raw: Any) -> AutoSettings:
        """A stored record, or a refusal; the caller logs it once and uses the defaults.

        Unknown fields are refused: a newer release may mean something different by them.
        """
        stored = _exact_shape(
            raw, frozenset(cls().as_dict()), "unknown_field", "a stored settings record"
        )
        revision = stored["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            _refuse("invalid_number", "a stored revision must be a non-negative whole number")
        stop = stored["departure"]
        if not isinstance(stop, str):
            _refuse("invalid_departure", "a stored departure must be a wall time string")
        try:
            departure = time.fromisoformat(stop)
        except ValueError:
            _refuse("invalid_departure", "a stored departure must be a wall time")
        if departure.tzinfo is not None:
            _refuse("invalid_departure", "a stored departure must be an area-local wall time")
        overrides = stored["overrides"]
        if not isinstance(overrides, list):
            _refuse("invalid_area", "stored overrides must be a list")
        return cls(
            revision=revision,
            area_id=stored["area_id"],
            overrides=tuple(AreaAutoSettings.from_stored(item) for item in overrides),
            phases=stored["phases"],
            amps=stored["amps"],
            requested_kwh=stored["requested_kwh"],
            max_periods=stored["max_periods"],
            departure_enabled=stored["departure_enabled"],
            departure=departure,
            pause=PauseIntent.from_stored(stored["pause"]),
            driver=stored["driver"],
            target=TargetSocIntent.from_stored(stored["target"]),
            strategy=stored["strategy"],
        ).validated()


@dataclass(frozen=True, slots=True)
class StoredProposal:
    """A proposal summary, kept so a restart can say what the last calculation produced.

    Counts, timestamps, money and period bounds only; never the interval array or raw documents.
    """

    calculated_at: datetime
    state: str
    reason: str | None
    settings_revision: int
    area_id: str
    slots_needed: int
    priced_slots: int
    unpriced_slots: int
    delivered_kwh: float
    estimated_cost: float
    distance_mil: float
    period_starts: tuple[str, ...]
    period_ends: tuple[str, ...] = ()
    price_identity: str | None = None
    unpriced: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "calculated_at": self.calculated_at.isoformat(),
            "state": self.state,
            "reason": self.reason,
            "settings_revision": self.settings_revision,
            "area_id": self.area_id,
            "slots_needed": self.slots_needed,
            "priced_slots": self.priced_slots,
            "unpriced_slots": self.unpriced_slots,
            "delivered_kwh": self.delivered_kwh,
            "estimated_cost": self.estimated_cost,
            "distance_mil": self.distance_mil,
            "period_starts": list(self.period_starts),
            "period_ends": list(self.period_ends),
            "price_identity": self.price_identity,
            "unpriced": self.unpriced,
        }

    @classmethod
    def from_stored(cls, raw: Any) -> StoredProposal:
        """A stored proposal summary, or a refusal: every field is checked, none coerced.

        Read strictly (`True` is not `1`, counts agree with each other and with the periods,
        instants are aware, state, reason and unpriced flag agree) so a dump never shows a
        number no calculation produced.
        """
        stored = _exact_shape(
            raw,
            frozenset(
                cls(
                    calculated_at=datetime.min,
                    state="",
                    reason=None,
                    settings_revision=0,
                    area_id="",
                    slots_needed=0,
                    priced_slots=0,
                    unpriced_slots=0,
                    delivered_kwh=0.0,
                    estimated_cost=0.0,
                    distance_mil=0.0,
                    period_starts=(),
                    period_ends=(),
                ).as_dict()
            ),
            "unknown_field",
            "a stored proposal",
        )
        stamp = stored["calculated_at"]
        if not isinstance(stamp, str):
            _refuse("invalid_proposal", "a stored proposal needs a calculation time string")
        calculated_at = dt_util.parse_datetime(stamp)
        if calculated_at is None or calculated_at.tzinfo is None:
            _refuse("invalid_proposal", "a stored proposal needs an aware calculation time")
        slots_needed = _stored_count(stored["slots_needed"], "slots_needed")
        priced_slots = _stored_count(stored["priced_slots"], "priced_slots")
        unpriced_slots = _stored_count(stored["unpriced_slots"], "unpriced_slots")
        if priced_slots + unpriced_slots != slots_needed:
            _refuse("invalid_proposal", "a stored proposal's slot counts must agree")
        unpriced = stored["unpriced"]
        if not isinstance(unpriced, bool):
            _refuse("invalid_proposal", "a stored proposal's unpriced flag must be a boolean")
        if unpriced != (unpriced_slots > 0):
            _refuse("invalid_proposal", "a stored proposal's unpriced flag must match its counts")
        state = stored["state"]
        reason = stored["reason"]
        expected_state = "proposal_unpriced" if unpriced else "proposal_ready"
        if state not in PROPOSAL_STATES or state != expected_state:
            _refuse("invalid_proposal", "a stored proposal's state must match its unpriced flag")
        if reason not in PROPOSAL_REASONS or reason not in _PROPOSAL_REASONS_BY_FLAG[unpriced]:
            _refuse("invalid_proposal", "a stored proposal's reason must match its unpriced flag")
        area_id = stored["area_id"]
        if not isinstance(area_id, str) or not area_id:
            _refuse("invalid_proposal", "a stored proposal needs an area id")
        identity = stored["price_identity"]
        if identity is not None and not isinstance(identity, str):
            _refuse("invalid_proposal", "a stored proposal's price identity must be text or null")
        starts = _stored_instants(stored["period_starts"], "period_starts")
        ends = _stored_instants(stored["period_ends"], "period_ends")
        if len(starts) != len(ends):
            _refuse("invalid_proposal", "a stored proposal needs one end per period start")
        previous_end: datetime | None = None
        for start, end in zip(starts, ends):
            if end <= start:
                _refuse("invalid_proposal", "a stored proposal's periods must end after they start")
            if previous_end is not None and start < previous_end:
                _refuse("invalid_proposal", "a stored proposal's periods must be ordered")
            previous_end = end
        if slots_needed == 0:
            if starts:
                _refuse("invalid_proposal", "a stored proposal with no slots cannot have periods")
        elif not starts or len(starts) > slots_needed:
            _refuse("invalid_proposal", "a stored proposal's periods must fit its slot count")
        return cls(
            calculated_at=calculated_at,
            state=state,
            reason=reason,
            settings_revision=_stored_count(stored["settings_revision"], "settings_revision"),
            area_id=area_id,
            slots_needed=slots_needed,
            priced_slots=priced_slots,
            unpriced_slots=unpriced_slots,
            delivered_kwh=_stored_number(stored["delivered_kwh"], "delivered_kwh", minimum=0.0),
            estimated_cost=_stored_number(stored["estimated_cost"], "estimated_cost"),
            distance_mil=_stored_number(stored["distance_mil"], "distance_mil", minimum=0.0),
            period_starts=tuple(item.isoformat() for item in starts),
            period_ends=tuple(item.isoformat() for item in ends),
            price_identity=identity,
            unpriced=unpriced,
        )


@dataclass(frozen=True, slots=True)
class EnergyBaseline:
    """`manual_kwh`'s delivered-energy baseline: the charger's cumulative energy register
    reading when a plan began counting toward the current departure occurrence.

    It keeps `auto_controller._energy_for` from buying already-delivered energy twice.
    Stored beside `settings`/`proposal` (see `AutoSettingsStore._document`), not in
    `AutoSettings`: it is not a setting and a write must not look like a settings edit.

    `register_kwh` is `None` when no baseline could be captured for the current
    `departure_key`; that reads as "cannot subtract" (and hybrid may not credit forecast
    sun), not zero. `departure_key` is the resolved departure instant (ISO text) or the
    sentinel `"no_deadline"`; a different key starts a fresh epoch.
    """

    register_kwh: float | None
    departure_key: str

    def as_dict(self) -> dict[str, Any]:
        return {"register_kwh": self.register_kwh, "departure_key": self.departure_key}

    @classmethod
    def from_stored(cls, raw: Any) -> EnergyBaseline:
        stored = _exact_shape(
            raw, frozenset({"register_kwh", "departure_key"}), "invalid_energy_baseline",
            "a stored energy baseline",
        )
        register_kwh = stored["register_kwh"]
        if register_kwh is not None:
            register_kwh = _finite(register_kwh, "invalid_energy_baseline", "register_kwh")
        departure_key = stored["departure_key"]
        if not isinstance(departure_key, str) or not departure_key:
            _refuse("invalid_energy_baseline", "a stored energy baseline needs a departure key")
        return cls(register_kwh=register_kwh, departure_key=departure_key)


#: Settings a first-run default may fill in (see `planning/first_run.py`).
SUGGESTIBLE_FIELDS: Final = ("area", "phases", "amps")


@dataclass(slots=True)
class _Entry:
    settings: AutoSettings
    proposal: StoredProposal | None = None
    energy_baseline: EnergyBaseline | None = None
    #: Which settings were filled in by first-run defaults and not yet confirmed by an edit.
    suggested: tuple[str, ...] = ()


class AutoSettingsStore:
    """The one Auto settings document for this installation, and its atomic editor.

    Every mutation is a read-modify-write of one in-memory document followed by one
    whole-file save under a single lock. `expected_revision` adds compare-and-set: an
    edit built on a stale revision is refused.
    """

    def __init__(
        self, hass: HomeAssistant, *, store: Store[dict[str, Any]] | None = None
    ) -> None:
        """The one document for this installation, and its persistence seam.

        `store` is `None` in production (HA's own `Store`); tests pass a double that can fail.
        """
        self._store: Store[dict[str, Any]] = (
            store
            if store is not None
            else Store(hass, STORAGE_VERSION, STORAGE_KEY)
        )

        self._entries: dict[str, _Entry] = {}
        self._stored: dict[str, Any] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Read the file once. Bad content is ignored, never fatal.

        A missing file, other schema, malformed record, unknown field or invalid enum value
        each logs one warning and leaves that entry at its defaults.
        """
        async with self._lock:
            if self._loaded:
                return
            # Set only after the read returns, so an unreadable file leaves the store unloaded.
            raw = await self._store.async_load()
            self._loaded = True
            if raw is None:
                return

            if not isinstance(raw, dict) or raw.get("schema") not in SUPPORTED_SCHEMAS:
                _LOGGER.warning("Ignoring stored Auto settings: unsupported schema version")
                return
            self._stored = dict(raw)
            entries = raw.get("chargers")
            if not isinstance(entries, dict):
                _LOGGER.warning("Ignoring stored Auto settings: no charger records")
                return
            for entry_id, record in entries.items():
                if not isinstance(entry_id, str) or not entry_id:
                    _LOGGER.warning("Ignoring a stored Auto settings record with no entry id")
                    continue
                if not isinstance(record, dict) or set(record) - {
                    "settings", "proposal", "energy_baseline", "suggested",
                }:
                    _LOGGER.warning("Ignoring a malformed stored Auto settings record")
                    continue
                try:
                    settings = AutoSettings.from_stored(record.get("settings"))
                except AutoSettingsError as err:
                    _LOGGER.warning("Ignoring stored Auto settings for one charger: %s", err.code)
                    continue
                proposal = None
                if record.get("proposal") is not None:
                    try:
                        proposal = StoredProposal.from_stored(record["proposal"])
                    except AutoSettingsError as err:
                        _LOGGER.warning("Ignoring a stored Auto proposal: %s", err.code)
                energy_baseline = None
                if record.get("energy_baseline") is not None:
                    try:
                        energy_baseline = EnergyBaseline.from_stored(record["energy_baseline"])
                    except AutoSettingsError as err:
                        _LOGGER.warning("Ignoring a stored energy baseline: %s", err.code)
                stored_suggested = record.get("suggested")
                suggested: tuple[str, ...] = ()
                if isinstance(stored_suggested, list) and all(
                    item in SUGGESTIBLE_FIELDS for item in stored_suggested
                ):
                    suggested = tuple(stored_suggested)
                self._entries[entry_id] = _Entry(
                    settings=settings,
                    proposal=proposal,
                    energy_baseline=energy_baseline,
                    suggested=suggested,
                )

    def settings(self, entry_id: str) -> AutoSettings:
        """This charger's settings, or the defaults for a charger with no record.

        The defaults lack safety-critical inputs, so calculation waits until they are configured.
        """
        entry = self._entries.get(entry_id)
        return AutoSettings() if entry is None else entry.settings

    def proposal(self, entry_id: str) -> StoredProposal | None:
        entry = self._entries.get(entry_id)
        return None if entry is None else entry.proposal

    def energy_baseline(self, entry_id: str) -> EnergyBaseline | None:
        entry = self._entries.get(entry_id)
        return None if entry is None else entry.energy_baseline

    def suggested(self, entry_id: str) -> tuple[str, ...]:
        """Settings filled in by first-run defaults that no edit has confirmed yet."""
        entry = self._entries.get(entry_id)
        return () if entry is None else entry.suggested

    def entry_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def async_update(
        self,
        entry_id: str,
        *,
        mutate: Callable[[AutoSettings], AutoSettings] | None = None,
        expected_revision: int | None = None,
        proposal: StoredProposal | None = None,
        clear_proposal: bool = False,
        energy_baseline: EnergyBaseline | None = None,
        clear_energy_baseline: bool = False,
        confirm: bool = False,
    ) -> AutoSettings:
        """The one way settings change: validate, bump the revision, save, return.

        A settings change bumps the revision; a proposal-only or baseline-only write does not.
        `expected_revision` refuses an edit built on a revision that has since moved. `confirm`
        marks the change as a person's edit, which clears the first-run `suggested` marker; system
        writes (pause expiry, proposals) leave it.
        """
        async with self._lock:
            current = self.settings(entry_id)
            if expected_revision is not None and current.revision != expected_revision:
                _refuse(
                    "revision_conflict",
                    f"settings are at revision {current.revision}, not {expected_revision}",
                )
            if mutate is None:
                updated = current
            else:
                updated = mutate(current).validated()
                updated = replace(updated, revision=current.revision + 1)
            existing = self._entries.get(entry_id)
            # A fresh record, never a mutated one (see `_async_commit`).
            entry = _Entry(
                settings=updated,
                proposal=None if existing is None else existing.proposal,
                energy_baseline=None if existing is None else existing.energy_baseline,
                suggested=(
                    ()
                    if existing is None or (confirm and mutate is not None)
                    else existing.suggested
                ),
            )
            if clear_proposal:
                entry.proposal = None
            elif proposal is not None:
                entry.proposal = proposal
            if clear_energy_baseline:
                entry.energy_baseline = None
            elif energy_baseline is not None:
                entry.energy_baseline = energy_baseline
            await self._async_commit(entry_id, entry)
            return updated

    async def async_seed(
        self, entry_id: str, settings: AutoSettings, suggested: tuple[str, ...]
    ) -> bool:
        """Write first-run defaults for a charger nobody has configured, once.

        Only a charger with no record, or one never edited (revision 0), is seeded; anything a
        person has saved (even clearing a value) has a revision above zero and is never touched.
        Returns whether it wrote.
        """
        async with self._lock:
            existing = self._entries.get(entry_id)
            if existing is not None and existing.settings.revision != 0:
                return False
            if any(name not in SUGGESTIBLE_FIELDS for name in suggested):
                _refuse("unknown_field", "a first-run default names a setting it may not fill")
            seeded = replace(settings.validated(), revision=1)
            entry = _Entry(
                settings=seeded,
                proposal=None if existing is None else existing.proposal,
                energy_baseline=None if existing is None else existing.energy_baseline,
                suggested=tuple(suggested),
            )
            await self._async_commit(entry_id, entry)
            return True

    async def async_remove(self, entry_id: str) -> bool:
        """Forget one charger's Auto state, and only that charger's.

        `False` when there was nothing to forget. Saves before changing memory (see `_async_commit`).
        """
        async with self._lock:
            if entry_id not in self._entries:
                return False
            candidate = {
                existing_id: entry
                for existing_id, entry in self._entries.items()
                if existing_id != entry_id
            }
            document = self._document(candidate)
            await self._store.async_save(document)
            self._entries = candidate
            self._stored = document
            return True

    async def _async_commit(self, entry_id: str, entry: _Entry) -> None:
        """Persist a candidate record, and only then make it the in-memory truth.

        A failed save leaves settings, proposal and revision as the last successful write left
        them, so the store never holds a change that would vanish at restart. The whole document
        is written each time because chargers share one file; the lock prevents lost fields.
        """
        candidate = dict(self._entries)
        candidate[entry_id] = entry
        document = self._document(candidate)
        await self._store.async_save(document)
        self._entries = candidate
        self._stored = document

    def _document(self, entries: dict[str, _Entry]) -> dict[str, Any]:
        """The whole stored file, from one set of records."""
        return {
            "schema": STORAGE_VERSION,
            "chargers": {
                entry_id: {
                    "settings": entry.settings.as_dict(),
                    "proposal": None if entry.proposal is None else entry.proposal.as_dict(),
                    "energy_baseline": (
                        None
                        if entry.energy_baseline is None
                        else entry.energy_baseline.as_dict()
                    ),
                    "suggested": list(entry.suggested),
                }
                for entry_id, entry in entries.items()
            },
        }


async def async_setup_auto_settings(hass: HomeAssistant) -> AutoSettingsStore:
    """Create the one store for this installation and read it once.

    Domain-scoped: settings belong to the installation, not a charger entry. A second call
    returns the existing store; unloading an entry never touches it.
    """
    data = domain_data(hass)
    if data.auto_store is not None:
        return data.auto_store
    store = AutoSettingsStore(hass)
    # Load before publishing, so an unreadable file fails setup instead of leaving a half-loaded store.
    await store.async_load()
    data.auto_store = store
    return store
