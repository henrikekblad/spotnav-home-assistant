"""What is still left to set up for one charger: the card's "Get started" rows.

Each row is computed from state the integration already holds, never guessed, and goes away by itself once
the thing is done. The optional ones can also be dismissed ("Not needed"); a dismissal is kept for the whole
installation in the decision store (`vehicles/discovery_decisions.py`, domain `setup`), so it holds in every
browser and for every charger.

* `vehicle`: no vehicle is set up for the charger, so a target in percent and the range lack the car's battery
  size and consumption. Dismissible (a household without a car integration charges by kWh). In place of
  `car_level`, never beside it.
* `car_level`: the plan charges to a charge level (%), a vehicle is set up, but nothing reports its level. Not
  dismissible: the plan cannot work without it.
* `site`: the charger has no site, so there is no grid meter for load balancing or solar. Dismissible.
* `solar_forecast`: the charger's site can measure solar surplus but no solar forecast source is chosen for it.
  Dismissible.
* `phase_switching`: the charger can switch phases and its site can do solar (the setting is offered), but phase
  switching for solar is off. Dismissible.
* `phone`: no phone is chosen for this charger's notifications, and the paired SpotNav app does not take them
  either (no push registration). Shown also with no phone at all: the notification settings say how to get one,
  through either app. Dismissible.
* `app`: the SpotNav app has never been used with this installation (no charger's webhook has had a request
  from it, and no charger holds its push registration). Dismissible.

Prices are not a row: the price provider notice (`pricing/providers/notices.py`) already asks for what an
area without a source of its own needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .planning.auto_settings import DRIVER_TARGET_SOC, AutoSettings
from .runtime import charger_data, domain_data

SETUP_VEHICLE: Final = "vehicle"
SETUP_CAR_LEVEL: Final = "car_level"
SETUP_SITE: Final = "site"
SETUP_SOLAR_FORECAST: Final = "solar_forecast"
SETUP_PHASE_SWITCHING: Final = "phase_switching"
SETUP_PHONE: Final = "phone"
SETUP_APP: Final = "app"
#: Every row, in the order the card lists them.
SETUP_ITEMS: Final = (
    SETUP_VEHICLE,
    SETUP_CAR_LEVEL,
    SETUP_SITE,
    SETUP_SOLAR_FORECAST,
    SETUP_PHASE_SWITCHING,
    SETUP_PHONE,
    SETUP_APP,
)
#: The rows a person may answer "Not needed" to.
DISMISSIBLE_ITEMS: Final = frozenset(
    {SETUP_VEHICLE, SETUP_SITE, SETUP_SOLAR_FORECAST, SETUP_PHASE_SWITCHING, SETUP_PHONE, SETUP_APP}
)

#: The decision store's domain for dismissed rows, and for the fact that the app has been used.
DECISION_DOMAIN_SETUP: Final = "setup"
DECISION_DOMAIN_SETUP_DONE: Final = "setup_done"


@dataclass(frozen=True, slots=True)
class SetupItem:
    """One row still left: what it is and whether it can be dismissed."""

    id: str
    dismissible: bool

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "dismissible": self.dismissible}


def _dismissed(hass: HomeAssistant, item: str) -> bool:
    store = domain_data(hass).decision_store
    return store is not None and store.is_dismissed(DECISION_DOMAIN_SETUP, item)


def app_used(hass: HomeAssistant) -> bool:
    """Whether the SpotNav app has been used with this installation: remembered, or a push registration now."""
    store = domain_data(hass).decision_store
    if store is not None and store.confirmed_payload(DECISION_DOMAIN_SETUP_DONE, SETUP_APP) is not None:
        return True
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = charger_data(hass, entry.entry_id)
        if data is not None and data.push is not None and data.push.registration is not None:
            return True
    return False


async def async_note_app_used(hass: HomeAssistant) -> None:
    """The app spoke to a charger's webhook: remember it once, so the `app` row never shows again."""
    store = domain_data(hass).decision_store
    if store is None or store.confirmed_payload(DECISION_DOMAIN_SETUP_DONE, SETUP_APP) is not None:
        return
    await store.async_confirm(DECISION_DOMAIN_SETUP_DONE, SETUP_APP, {"at": dt_util.utcnow().isoformat()})


async def async_dismiss(hass: HomeAssistant, item: str) -> bool:
    """Keep a dismissible row away for the installation; `False` for a row that cannot be dismissed."""
    store = domain_data(hass).decision_store
    if item not in DISMISSIBLE_ITEMS or store is None:
        return False
    await store.async_dismiss(DECISION_DOMAIN_SETUP, item)
    return True


def capture_setup(
    hass: HomeAssistant,
    entry_id: str,
    *,
    settings: AutoSettings | None,
    has_vehicle: bool,
    has_soc_source: bool,
    has_site: bool,
    site_solar_capable: bool,
    has_solar_forecast: bool,
    phase_switching: bool | None,
) -> tuple[SetupItem, ...]:
    """The rows still left for this charger, in `SETUP_ITEMS` order. `phase_switching` is the setting's value,
    `None` while it is not offered (`api/dashboard.py:capture_phase_switching`)."""
    data = charger_data(hass, entry_id)
    push = None if data is None else data.push
    app_notifies = push is not None and push.registration is not None
    left: list[str] = []
    if not has_vehicle:
        left.append(SETUP_VEHICLE)
    elif settings is not None and settings.driver == DRIVER_TARGET_SOC and not has_soc_source:
        left.append(SETUP_CAR_LEVEL)
    if not has_site:
        left.append(SETUP_SITE)
    elif site_solar_capable and not has_solar_forecast:
        left.append(SETUP_SOLAR_FORECAST)
    if phase_switching is False:
        left.append(SETUP_PHASE_SWITCHING)
    if settings is not None and not settings.notifications.targets and not app_notifies:
        left.append(SETUP_PHONE)
    if not app_used(hass):
        left.append(SETUP_APP)
    return tuple(
        SetupItem(item, item in DISMISSIBLE_ITEMS)
        for item in left
        if item not in DISMISSIBLE_ITEMS or not _dismissed(hass, item)
    )


def serialize_setup(items: tuple[SetupItem, ...]) -> dict[str, Any]:
    """The dashboard's additive `setup` block: the rows still left (empty when nothing is)."""
    return {"items": [item.as_dict() for item in items]}
