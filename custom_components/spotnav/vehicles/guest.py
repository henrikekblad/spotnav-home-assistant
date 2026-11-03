"""A guest car: a visitor's car, planned by kWh with no vehicle, and only once a person said so.

* **Chosen by a person only**: an answer "Guest car" to the question, or the guest chosen in the card's or the app's
  car choice (`identification.py`, the answer `GUEST_VEHICLE_ID`). Nothing switches to a guest by itself.
* **In a charger's settings** a guest plan is the target vehicle `GUEST_VEHICLE_ID` with the driver `manual_kwh`, the
  guest amount and no fill (`with_guest`). `resolve_target_vehicle` resolves it to no vehicle, so nothing of a car is
  read, kept or learned for it: no level, target, minimum level, charge limit, plug or position sensor, energy per
  percent, learned power or wake. The charger's own strategy and departure apply.
* **What the settings were before** (`GuestPrevious`: the driver, the amount, fill and the target) is kept with the
  plug-in's identification record, so a restart while the guest is plugged in keeps the guest, and it comes back at
  the unplug or when a person chooses a household car (`restored`).
* **The guest amount** is one for the whole installation, `DEFAULT_GUEST_KWH` until someone changes it, and the last
  change wins: the Guest tab, or the amount changed in the plan dialog during a guest plan. Stored in the decision
  store (`discovery_decisions.DECISION_DOMAIN_GUEST`).
* **Delivered**: the core stops the charge and holds the charger until the unplug (`core/ownership.py`,
  `guest_met`); a person's Start still charges.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final

from homeassistant.core import HomeAssistant

from ..const import GUEST_VEHICLE_ID
from ..planning.auto_settings import AutoSettings, DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC, TargetSocIntent
from ..runtime import domain_data
from ..util import finite_number
from .discovery_decisions import DECISION_DOMAIN_GUEST

#: The guest amount until someone changes it.
DEFAULT_GUEST_KWH: Final = 10.0
#: The guest amount's range (kWh), whole kWh.
GUEST_KWH_MIN: Final = 1.0
GUEST_KWH_MAX: Final = 150.0
_KEY: Final = "guest"
_AMOUNT: Final = "amount_kwh"


def valid_amount(value: Any) -> bool:
    """A guest amount: a number of kWh within the range (a boolean is not one)."""
    number = finite_number(value)
    return number is not None and not isinstance(value, bool) and GUEST_KWH_MIN <= number <= GUEST_KWH_MAX


def guest_amount_kwh(hass: HomeAssistant) -> float:
    """The guest amount the next guest plan starts from: the last one set, else `DEFAULT_GUEST_KWH`."""
    store = domain_data(hass).decision_store
    payload = None if store is None else store.confirmed_payload(DECISION_DOMAIN_GUEST, _KEY)
    amount = None if payload is None else payload.get(_AMOUNT)
    return float(amount) if valid_amount(amount) else DEFAULT_GUEST_KWH


async def async_set_guest_amount(hass: HomeAssistant, kwh: Any) -> float:
    """Remember the guest amount (the last change wins). A value out of range raises `ValueError`."""
    if not valid_amount(kwh):
        raise ValueError("invalid guest amount")
    store = domain_data(hass).decision_store
    if store is None:
        raise ValueError("no decision store")
    if float(kwh) != guest_amount_kwh(hass):
        await store.async_confirm(DECISION_DOMAIN_GUEST, _KEY, {_AMOUNT: float(kwh)})
    return guest_amount_kwh(hass)


def is_guest(settings: AutoSettings | None) -> bool:
    """Whether these settings plan for a guest car."""
    return settings is not None and settings.target.vehicle_id == GUEST_VEHICLE_ID


@dataclass(frozen=True, slots=True)
class GuestPrevious:
    """A charger's settings before its guest plan: what comes back when the guest leaves."""

    driver: str
    requested_kwh: float
    fill_to_limit: bool
    vehicle_id: str | None
    target_percent: float | None

    @classmethod
    def of(cls, settings: AutoSettings) -> GuestPrevious:
        return cls(
            driver=settings.driver,
            requested_kwh=settings.requested_kwh,
            fill_to_limit=settings.fill_to_limit,
            vehicle_id=settings.target.vehicle_id,
            target_percent=settings.target.target_percent,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "requested_kwh": self.requested_kwh,
            "fill_to_limit": self.fill_to_limit,
            "vehicle_id": self.vehicle_id,
            "target_percent": self.target_percent,
        }

    @classmethod
    def from_stored(cls, raw: Any) -> GuestPrevious | None:
        """What was stored, or `None` for anything else (the charger then keeps the guest's driver and amount)."""
        if not isinstance(raw, dict):
            return None
        driver, kwh, fill = raw.get("driver"), finite_number(raw.get("requested_kwh")), raw.get("fill_to_limit")
        vehicle, percent = raw.get("vehicle_id"), raw.get("target_percent")
        if (
            driver not in (DRIVER_MANUAL_KWH, DRIVER_TARGET_SOC)
            or kwh is None
            or kwh <= 0
            or not isinstance(fill, bool)
            or not (vehicle is None or (isinstance(vehicle, str) and vehicle and vehicle != GUEST_VEHICLE_ID))
            or not (percent is None or (finite_number(percent) is not None and 0 <= float(percent) <= 100))
        ):
            return None
        return cls(driver, float(kwh), fill, vehicle, None if percent is None else float(percent))


def with_guest(settings: AutoSettings, kwh: float) -> AutoSettings:
    """The same settings planning `kwh` for a guest car: no vehicle, no target, no fill."""
    return replace(
        settings,
        driver=DRIVER_MANUAL_KWH,
        requested_kwh=float(kwh),
        fill_to_limit=False,
        target=TargetSocIntent(vehicle_id=GUEST_VEHICLE_ID, target_percent=None),
    )


def restored(settings: AutoSettings, previous: GuestPrevious | None) -> AutoSettings:
    """The settings the guest found, back: its driver, amount, fill and target. Without a record of them (lost) the
    guest's driver and amount stay and only the vehicle goes, so the charger plans for its own car again."""
    if previous is None:
        return replace(settings, target=TargetSocIntent(vehicle_id=None, target_percent=settings.target.target_percent))
    return replace(
        settings,
        driver=previous.driver,
        requested_kwh=previous.requested_kwh,
        fill_to_limit=previous.fill_to_limit,
        target=TargetSocIntent(vehicle_id=previous.vehicle_id, target_percent=previous.target_percent),
    )
