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
* `demand_grid_power`: a demand-charge component or the site's import target is set, but the charger's site is a
  direct one without the meter's total grid power, so its hours cannot be measured. Not dismissible: the place is
  the site's Total grid power (or removing what was set).
* `grid_power_sign`: the charger's site reads its grid power with the sign the wrong way round, as far as the
  chargers' own steps show (`site/grid_sign.py`): when a charger's draw changed, the grid moved the other way.
  Not dismissible; it goes by itself once the steps agree again or the setting is changed. The row carries the one
  site option its button writes (`field`, `grid_power_inverted`) and the value it writes (`value`); nothing is
  written unless a person presses it.
* `grid_current_sign`: the same for a direct site's signed phase currents (`site_current_signed`, written off).
* `phase_switching`: the charger can switch phases and its site can do solar (the setting is offered), but phase
  switching for solar is off. Dismissible.
* `phone`: no phone is chosen for this charger's notifications, and the paired SpotNav app does not take them
  either (no push registration). Shown also with no phone at all: the notification settings say how to get one,
  through either app. Dismissible.
* `app`: the SpotNav app has never been used with this installation (no charger's or site's webhook has had a
  request from it, and no charger holds its push registration). Dismissible.

A site's own rows (`capture_site_setup`, the shared settings popover's "Get started", with or without a charger):
`charger` (the site has no charger; dismissible: a home with a battery or heat alone does not need one), `price`
(no electricity price applies to the site; not dismissible), the grid sign rows, `solar_forecast` and
`demand_grid_power` as above, `battery` (a battery is found but SpotNav's battery control is off; dismissible) and
`heat` (a heat pump is found but no heat settings are made; dismissible) and, for a site without a charger, `app`
as above (SpotNav 3.0: the app pairs with a home too; a site with a charger gets it through the charger's rows). The
other charger rows are not among them.

The charger card's rows (the dashboard's `setup`, `with_site_rows`) are the charger's own rows and, for a charger in
a site, the site's rows a charger's own cannot have: `battery` and `heat` (`SITE_ROWS_FOR_CHARGER`). The rows both
compute (the grid sign rows, `solar_forecast`, `demand_grid_power`) are the charger's, once; `charger` never
applies to a charger, `app` is the charger's own, and `price` is never one: a charger card that shows prices must
not ask for a price, and a charger without any price has its own notice. A dismissal holds for the installation, so a "Not
needed" in either card holds in both.

Prices are not a charger row: the price provider notice (`pricing/providers/notices.py`) already asks for what an
area without a source of its own needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_GRID_POWER_INVERTED,
    CONF_GRID_POWER_SOURCE,
    CONF_HEAT_METER_ENTITY,
    CONF_MEASUREMENT_MODE,
    CONF_SITE_CURRENT_SIGNED,
    CONF_SOLAR_FORECAST_ENTRIES,
)
from .planning.auto_settings import DRIVER_TARGET_SOC, AutoSettings
from .runtime import domain_data
from .site.grid_sign import KIND_CURRENT, KIND_POWER
from .site.import_target import stored_import_target_kw

SETUP_VEHICLE: Final = "vehicle"
SETUP_CAR_LEVEL: Final = "car_level"
SETUP_SITE: Final = "site"
SETUP_SOLAR_FORECAST: Final = "solar_forecast"
SETUP_DEMAND_GRID_POWER: Final = "demand_grid_power"
SETUP_GRID_POWER_SIGN: Final = "grid_power_sign"
SETUP_GRID_CURRENT_SIGN: Final = "grid_current_sign"
SETUP_PHASE_SWITCHING: Final = "phase_switching"
SETUP_PHONE: Final = "phone"
SETUP_APP: Final = "app"
SETUP_CHARGER: Final = "charger"
SETUP_PRICE: Final = "price"
SETUP_BATTERY: Final = "battery"
SETUP_HEAT: Final = "heat"
#: Every row, in the order the card lists them.
SETUP_ITEMS: Final = (
    SETUP_VEHICLE,
    SETUP_CAR_LEVEL,
    SETUP_SITE,
    SETUP_GRID_POWER_SIGN,
    SETUP_GRID_CURRENT_SIGN,
    SETUP_SOLAR_FORECAST,
    SETUP_DEMAND_GRID_POWER,
    SETUP_PHASE_SWITCHING,
    SETUP_PHONE,
    SETUP_APP,
)
#: A site's own rows, in the order the popover lists them.
SITE_SETUP_ITEMS: Final = (
    SETUP_CHARGER,
    SETUP_PRICE,
    SETUP_GRID_POWER_SIGN,
    SETUP_GRID_CURRENT_SIGN,
    SETUP_SOLAR_FORECAST,
    SETUP_DEMAND_GRID_POWER,
    SETUP_BATTERY,
    SETUP_HEAT,
    SETUP_APP,
)
#: The site's rows the charger card shows too (`with_site_rows`).
SITE_ROWS_FOR_CHARGER: Final = (SETUP_BATTERY, SETUP_HEAT)
#: The charger card's rows with the site's, in the order the card lists them.
CHARGER_CARD_ITEMS: Final = (
    SETUP_VEHICLE,
    SETUP_CAR_LEVEL,
    SETUP_SITE,
    SETUP_GRID_POWER_SIGN,
    SETUP_GRID_CURRENT_SIGN,
    SETUP_SOLAR_FORECAST,
    SETUP_DEMAND_GRID_POWER,
    SETUP_BATTERY,
    SETUP_HEAT,
    SETUP_PHASE_SWITCHING,
    SETUP_PHONE,
    SETUP_APP,
)
#: The rows a person may answer "Not needed" to.
DISMISSIBLE_ITEMS: Final = frozenset(
    {
        SETUP_VEHICLE,
        SETUP_SITE,
        SETUP_SOLAR_FORECAST,
        SETUP_PHASE_SWITCHING,
        SETUP_PHONE,
        SETUP_APP,
        SETUP_CHARGER,
        SETUP_BATTERY,
        SETUP_HEAT,
    }
)

#: The decision store's domain for dismissed rows, and for the fact that the app has been used.
DECISION_DOMAIN_SETUP: Final = "setup"
DECISION_DOMAIN_SETUP_DONE: Final = "setup_done"


#: The grid sign rows (`site/grid_sign.py`): per reading the step test checks, its row and the site option it names.
GRID_SIGN_ROWS: Final = {
    KIND_POWER: (SETUP_GRID_POWER_SIGN, CONF_GRID_POWER_INVERTED),
    KIND_CURRENT: (SETUP_GRID_CURRENT_SIGN, CONF_SITE_CURRENT_SIGNED),
}


@dataclass(frozen=True, slots=True)
class SetupItem:
    """One row still left: what it is and whether it can be dismissed; a grid sign row also the site option its
    button writes and the value."""

    id: str
    dismissible: bool
    field: str | None = None
    value: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        found: dict[str, Any] = {"id": self.id, "dismissible": self.dismissible}
        if self.field is not None:
            found["field"] = self.field
            found["value"] = self.value
        return found


@dataclass(frozen=True, slots=True)
class GridSignRow:
    """A grid sign warning standing on the charger's site: its row, the site option and the value that undoes it."""

    id: str
    field: str
    value: bool


def grid_sign_rows(site_controller: Any) -> tuple[GridSignRow, ...]:
    """The grid sign warnings the site's step test raised, each with the value its option would be set to: the
    other one of what it was counted under."""
    if site_controller is None:
        return ()
    found: list[GridSignRow] = []
    for kind, (item, field) in GRID_SIGN_ROWS.items():
        flag = site_controller.grid_sign_flag(kind)
        if flag is not None and site_controller.grid_sign_warned(kind):
            found.append(GridSignRow(item, field, not flag))
    return tuple(found)


def _dismissed(hass: HomeAssistant, item: str) -> bool:
    store = domain_data(hass).decision_store
    return store is not None and store.is_dismissed(DECISION_DOMAIN_SETUP, item)


def app_used(hass: HomeAssistant) -> bool:
    """Whether the SpotNav app has been used with this installation: remembered, or a push registration now."""
    store = domain_data(hass).decision_store
    if store is not None and store.confirmed_payload(DECISION_DOMAIN_SETUP_DONE, SETUP_APP) is not None:
        return True
    phones = domain_data(hass).phones
    return phones is not None and any(phone.app is not None for phone in phones.phones)


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
    demand_needs_grid_power: bool = False,
    grid_sign: tuple[GridSignRow, ...] = (),
) -> tuple[SetupItem, ...]:
    """The rows still left for this charger, in `SETUP_ITEMS` order. `phase_switching` is the setting's value,
    `None` while it is not offered (`api/dashboard.py:capture_phase_switching`). `demand_needs_grid_power`: a
    demand-charge component or an import target is set and the site cannot measure it (`api/grid_tariff.py`).
    `grid_sign`: the site's grid sign warnings (`grid_sign_rows`)."""
    phones = domain_data(hass).phones
    left: list[str] = []
    if not has_vehicle:
        left.append(SETUP_VEHICLE)
    elif settings is not None and settings.driver == DRIVER_TARGET_SOC and not has_soc_source:
        left.append(SETUP_CAR_LEVEL)
    if not has_site:
        left.append(SETUP_SITE)
    else:
        left += [row.id for row in grid_sign]
        if site_solar_capable and not has_solar_forecast:
            left.append(SETUP_SOLAR_FORECAST)
    if has_site and demand_needs_grid_power:
        left.append(SETUP_DEMAND_GRID_POWER)
    if phase_switching is False:
        left.append(SETUP_PHASE_SWITCHING)
    if settings is not None and phones is not None and not phones.has_recipient(entry_id):
        # No phone that is on follows this charger by a route that exists (`notifications/phone_store.py`).
        left.append(SETUP_PHONE)
    if not app_used(hass):
        left.append(SETUP_APP)
    writes = {row.id: row for row in grid_sign}
    return tuple(
        SetupItem(
            item,
            item in DISMISSIBLE_ITEMS,
            writes[item].field if item in writes else None,
            writes[item].value if item in writes else None,
        )
        for item in left
        if item not in DISMISSIBLE_ITEMS or not _dismissed(hass, item)
    )


def capture_site_setup(hass: HomeAssistant, site: ConfigEntry) -> tuple[SetupItem, ...]:
    """The rows still left for a site (see the module), in `SITE_SETUP_ITEMS` order, dismissed rows left out."""
    # Imported here: these modules import the API layer, which imports this one.
    from .api.heat import stored_config  # noqa: PLC0415
    from .battery.gate import Options  # noqa: PLC0415
    from .heat.config import PATH_NONE  # noqa: PLC0415
    from .heat.detect import read_heat_pumps  # noqa: PLC0415
    from .pricing.price_source import site_price_source  # noqa: PLC0415
    from .runtime import site_controller_for  # noqa: PLC0415
    from .site.solar_capability import solar_capability  # noqa: PLC0415

    data = site.data
    controller = site_controller_for(hass, site.entry_id)
    left: list[str] = []
    if not data.get(CONF_CHARGER_ENTRY_IDS):
        left.append(SETUP_CHARGER)
    source = site_price_source(hass, site.entry_id)
    if source is None:
        left.append(SETUP_PRICE)
    signs = grid_sign_rows(controller)
    left += [row.id for row in signs]
    capability = solar_capability(data.get(CONF_MEASUREMENT_MODE), data.get(CONF_GRID_POWER_SOURCE))
    if capability.capable and not data.get(CONF_SOLAR_FORECAST_ENTRIES):
        left.append(SETUP_SOLAR_FORECAST)
    tariff = None if source is None else source.overrides.grid_tariff
    if not capability.capable and (
        (tariff is not None and bool(tariff.demand)) or stored_import_target_kw(data) is not None
    ):
        left.append(SETUP_DEMAND_GRID_POWER)
    battery = controller is not None and bool(controller.battery_shadow.facts.battery_entity_id)
    if battery and not Options.from_config(data).enabled:
        left.append(SETUP_BATTERY)
    heat = stored_config(site)
    if (
        heat.control_path == PATH_NONE
        and not heat.indoor_sensors
        and not heat.zones
        and not data.get(CONF_HEAT_METER_ENTITY)
        and read_heat_pumps(hass)
    ):
        left.append(SETUP_HEAT)
    if not data.get(CONF_CHARGER_ENTRY_IDS) and not app_used(hass):
        left.append(SETUP_APP)
    writes = {row.id: row for row in signs}
    return tuple(
        SetupItem(
            item,
            item in DISMISSIBLE_ITEMS,
            writes[item].field if item in writes else None,
            writes[item].value if item in writes else None,
        )
        for item in left
        if item not in DISMISSIBLE_ITEMS or not _dismissed(hass, item)
    )


def with_site_rows(charger: tuple[SetupItem, ...], site: tuple[SetupItem, ...]) -> tuple[SetupItem, ...]:
    """The charger card's rows: the charger's own and the site's `SITE_ROWS_FOR_CHARGER` (see the module), each
    once, in `CHARGER_CARD_ITEMS` order."""
    own = {item.id for item in charger}
    rows = list(charger) + [item for item in site if item.id in SITE_ROWS_FOR_CHARGER and item.id not in own]
    return tuple(sorted(rows, key=lambda item: CHARGER_CARD_ITEMS.index(item.id)))


def serialize_setup(items: tuple[SetupItem, ...]) -> dict[str, Any]:
    """The dashboard's additive `setup` block: the rows still left (empty when nothing is)."""
    return {"items": [item.as_dict() for item in items]}
