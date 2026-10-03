"""First-run defaults: a new charger starts with a working card instead of an empty form.

Written once, when a charger has no settings a person has ever saved (`AutoSettingsStore.async_seed`),
and recorded as `suggested` so the card can say the values came from Home Assistant and not from a
person. Nothing here overwrites a saved value or runs again after a value is cleared.

* area: the relay catalogue's area for Home Assistant's country. A country with one area gets it.
  A country with several (SE, NO, DK today) gets the area whose reference point is nearest to Home
  Assistant's configured coordinates; see `AREA_REFERENCE_POINTS`. No country, a country the
  catalogue does not list, a multi-area country without a table, or coordinates far from every
  reference point (Home Assistant's unset default is in California) leaves the area empty.
* phases are not a setting any more: the charger's wiring (the site's, else the charger flow's answer) and
  the vehicle's onboard charger decide them (`planning/phases.py`).
* amps: the charger's own maximum when an entity states one, else 16; never above the site's main
  fuse minus its safety margin, and left empty if that leaves less than the charger's minimum.
* fiscal figures are not defaulted here: the catalogue's suggestions resolve during calculation.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_CHARGER_CURRENT_ENTITIES,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CHARGER_PHASES,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_PHASE_WIRING,
    CONF_SAFETY_MARGIN_A,
    DOMAIN,
    ENTRY_TYPE_SITE,
)
from ..execution.controller import (
    CURRENT_RANGE_MIN_A,
    CURRENT_RANGE_SOURCE_DEFAULT,
    ChargingController,
)
from ..pricing.relay_contract import AreaCatalogue
from ..runtime import domain_data
from .auto_settings import AutoSettings

_LOGGER = logging.getLogger(__name__)

#: Current assumed for a charger that states no maximum of its own.
UNKNOWN_CHARGER_AMPS: Final = 16
#: A position farther than this from every reference point of the country is not trusted
#: (Sweden is about 1,500 km long and its points are at most about 250 km apart).
MAX_REFERENCE_DISTANCE_KM: Final = 400.0

#: Approximate bidding-zone locations as (latitude, longitude) points, per area id; the nearest
#: point within a country decides. The points are the larger towns of each zone, placed where the
#: zone borders run between them, so the answer is right in and around those towns and within about
#: 50 km of a border it may fall in the neighbouring zone. Not a survey of the borders: the card
#: shows the choice as a suggestion to check. Only countries the relay lists with several areas
#: need an entry, and an area with no entry here is never chosen by location.
AREA_REFERENCE_POINTS: Final[dict[str, tuple[tuple[float, float], ...]]] = {
    # Sweden: SE1 Norrbotten, SE2 the rest of the north (Västerbotten, Jämtland, Västernorrland),
    # SE3 the middle including Stockholm and Gothenburg, SE4 the south (Skåne, Blekinge, Halland,
    # Kronoberg, Kalmar).
    "SE1": (
        (65.58, 22.15),  # Luleå
        (67.86, 20.23),  # Kiruna
        (67.13, 20.66),  # Gällivare
        (65.84, 24.14),  # Haparanda
        (65.32, 21.48),  # Piteå
        (65.85, 23.14),  # Kalix
    ),
    "SE2": (
        (63.83, 20.26),  # Umeå
        (62.39, 17.31),  # Sundsvall
        (63.18, 14.64),  # Östersund
        (63.29, 18.72),  # Örnsköldsvik
        (64.75, 20.95),  # Skellefteå
        (64.60, 18.67),  # Lycksele
        (63.17, 17.27),  # Sollefteå
    ),
    "SE3": (
        (59.33, 18.07),  # Stockholm
        (59.86, 17.64),  # Uppsala
        (59.61, 16.55),  # Västerås
        (59.27, 15.21),  # Örebro
        (58.41, 15.62),  # Linköping
        (58.59, 16.19),  # Norrköping
        (57.71, 11.97),  # Göteborg
        (57.72, 12.94),  # Borås
        (57.78, 14.16),  # Jönköping
        (59.38, 13.50),  # Karlstad
        (60.61, 15.63),  # Falun
        (60.67, 17.14),  # Gävle
        (57.64, 18.29),  # Visby
        (58.28, 12.29),  # Trollhättan
        (58.39, 13.85),  # Skövde
        (61.00, 14.54),  # Mora
    ),
    "SE4": (
        (55.60, 13.00),  # Malmö
        (55.70, 13.19),  # Lund
        (56.05, 12.69),  # Helsingborg
        (56.03, 14.16),  # Kristianstad
        (56.16, 15.59),  # Karlskrona
        (56.88, 14.81),  # Växjö
        (56.66, 16.36),  # Kalmar
        (56.67, 12.86),  # Halmstad
        (57.11, 12.25),  # Varberg
        (55.43, 13.82),  # Ystad
        (56.83, 13.94),  # Ljungby
        (56.91, 12.49),  # Falkenberg
    ),
    # Norway: NO1 the east (Oslo, Innlandet, Vestfold), NO2 the south and Rogaland, NO3 Trøndelag
    # and the Møre coast, NO4 Nordland and north, NO5 Vestland.
    "NO1": (
        (59.91, 10.75),  # Oslo
        (61.11, 10.47),  # Lillehammer
        (60.79, 11.07),  # Hamar
        (59.27, 10.41),  # Tønsberg
        (59.13, 11.39),  # Halden
    ),
    "NO2": (
        (58.15, 8.00),  # Kristiansand
        (58.97, 5.73),  # Stavanger
        (59.21, 9.61),  # Skien
        (59.41, 5.27),  # Haugesund
        (58.46, 8.77),  # Arendal
    ),
    "NO3": (
        (63.43, 10.40),  # Trondheim
        (62.74, 7.16),  # Molde
        (62.47, 6.15),  # Ålesund
        (63.11, 7.73),  # Kristiansund
        (62.57, 11.38),  # Røros
        (64.01, 11.50),  # Steinkjer
    ),
    "NO4": (
        (67.28, 14.40),  # Bodø
        (69.65, 18.96),  # Tromsø
        (68.44, 17.43),  # Narvik
        (69.97, 23.27),  # Alta
        (66.31, 14.14),  # Mo i Rana
        (70.66, 23.68),  # Hammerfest
        (69.73, 30.04),  # Kirkenes
    ),
    "NO5": (
        (60.39, 5.32),  # Bergen
        (61.45, 5.85),  # Førde
        (60.63, 6.42),  # Voss
        (61.23, 7.10),  # Sogndal
    ),
    # Denmark: DK1 Jutland and Funen, DK2 Zealand, Lolland-Falster and Bornholm; the Great Belt
    # between Funen and Zealand is the border.
    "DK1": (
        (57.05, 9.92),  # Aalborg
        (56.16, 10.21),  # Aarhus
        (55.48, 8.45),  # Esbjerg
        (55.40, 10.39),  # Odense
        (55.49, 9.47),  # Fredericia
        (57.44, 10.54),  # Frederikshavn
        (56.46, 8.49),  # Holstebro
    ),
    "DK2": (
        (55.68, 12.57),  # København
        (55.64, 12.08),  # Roskilde
        (55.23, 11.76),  # Næstved
        (54.77, 11.87),  # Nykøbing Falster
        (55.10, 14.70),  # Rønne
        (56.03, 12.61),  # Helsingør
        (55.33, 11.14),  # Korsør
        (55.72, 11.72),  # Holbæk
    ),
}


def _distance_km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Great-circle distance, the haversine formula."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lon_b - lon_a)
    chord = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(chord))


def suggest_area(
    catalogue: AreaCatalogue | None,
    country: str | None,
    latitude: float | None,
    longitude: float | None,
) -> str | None:
    """The catalogue area id for this country and position, or `None` when nothing trustworthy fits."""
    if catalogue is None or not isinstance(country, str) or not country.strip():
        return None
    code = country.strip().upper()
    candidates = [entry.id for entry in catalogue.areas if code in (name.upper() for name in entry.countries)]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    best: tuple[float, str] | None = None
    for area_id in candidates:
        for point_lat, point_lon in AREA_REFERENCE_POINTS.get(area_id, ()):
            distance = _distance_km(latitude, longitude, point_lat, point_lon)
            if best is None or distance < best[0]:
                best = (distance, area_id)
    if best is None or best[0] > MAX_REFERENCE_DISTANCE_KM:
        return None
    return best[1]


def site_for_charger(hass: HomeAssistant, charger_entry_id: str) -> ConfigEntry | None:
    """The site entry that lists this charger, loaded or not."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE:
            continue
        if charger_entry_id in (entry.data.get(CONF_CHARGER_ENTRY_IDS) or []):
            return entry
    return None


def wired_phases(site: ConfigEntry | None, charger_entry_id: str) -> int | None:
    if site is None:
        return None
    wiring = (site.data.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id)
    phases = wiring.get("phases") if isinstance(wiring, dict) else None
    return phases if phases in (1, 3) and not isinstance(phases, bool) else None


def charger_phases_from_entry(data: Any) -> int | None:
    """The phases a charger's own entry says: what the flow recorded, else three when it has exactly
    three current entities (one per phase). `None` when nothing says.
    """
    phases = data.get(CONF_CHARGER_PHASES)
    if phases in (1, 3) and not isinstance(phases, bool):
        return phases
    entities = data.get(CONF_CHARGER_CURRENT_ENTITIES)
    return 3 if isinstance(entities, (list, tuple)) and len(entities) == 3 else None


def _site_limit_a(site: ConfigEntry | None) -> int | None:
    """The main fuse minus the safety margin, whole amps, or `None` when there is no site fuse."""
    if site is None:
        return None
    fuse = site.data.get(CONF_MAIN_FUSE_A)
    margin = site.data.get(CONF_SAFETY_MARGIN_A, 0.0)
    if isinstance(fuse, bool) or not isinstance(fuse, (int, float)) or not math.isfinite(fuse):
        return None
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin):
        margin = 0.0
    return math.floor(fuse - margin)


def first_run_defaults(
    *,
    catalogue: AreaCatalogue | None,
    country: str | None,
    latitude: float | None,
    longitude: float | None,
    charger_max_a: int | None,
    site_limit_a: int | None,
) -> tuple[AutoSettings, tuple[str, ...]]:
    """The defaults as a settings record, and which of area and amps they filled in."""
    area = suggest_area(catalogue, country, latitude, longitude)
    amps: int | None = charger_max_a if charger_max_a is not None else UNKNOWN_CHARGER_AMPS
    if site_limit_a is not None:
        amps = min(amps, site_limit_a)
    if amps < CURRENT_RANGE_MIN_A:
        amps = None
    suggested = tuple(
        name
        for name, value in (("area", area), ("amps", amps))
        if value is not None
    )
    return AutoSettings(area_id=area, amps=amps), suggested


async def async_seed_first_run(
    hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController, preview: Any
) -> bool:
    """Give a never-configured charger its defaults, then let the preview pick them up.

    Returns whether anything was written. Never raises for a store that refuses: a charger without
    defaults is the state it would have been in anyway.
    """
    store = domain_data(hass).auto_store
    manager = domain_data(hass).price_refresh
    if store is None:
        return False
    catalogue = None if manager is None else manager.catalogue_snapshot().catalogue
    if store.settings(entry.entry_id).revision != 0:
        return await _async_suggest_missing_area(hass, entry, store, catalogue, preview)
    site = site_for_charger(hass, entry.entry_id)
    current_range = controller.current_range()
    charger_max = (
        None if current_range["source"] == CURRENT_RANGE_SOURCE_DEFAULT else int(current_range["max_a"])
    )
    settings, suggested = first_run_defaults(
        catalogue=catalogue,
        country=hass.config.country,
        latitude=hass.config.latitude,
        longitude=hass.config.longitude,
        charger_max_a=charger_max,
        site_limit_a=_site_limit_a(site),
    )
    if not suggested:
        return False
    try:
        written = await store.async_seed(entry.entry_id, settings, suggested)
    except Exception as err:  # noqa: BLE001 - defaults are a convenience, never a setup failure
        _LOGGER.warning("Storing first-run defaults failed: %s", type(err).__name__)
        return False
    if written and preview is not None:
        await preview.async_settings_seeded()
    return written


async def _async_suggest_missing_area(
    hass: HomeAssistant, entry: ConfigEntry, store: Any, catalogue: AreaCatalogue | None, preview: Any
) -> bool:
    """Suggest the area on the first successful catalogue read while it is still unset, whatever the
    settings' revision and only while no person has saved a setting since first run: first-run defaults written while the relay was unreachable have no area, and
    nothing else would ever fill it in.
    """
    # `suggested` is emptied by a person's own save, so an area a person cleared stays cleared.
    if (
        catalogue is None
        or store.settings(entry.entry_id).area_id is not None
        or not store.suggested(entry.entry_id)
    ):
        return False
    area = suggest_area(catalogue, hass.config.country, hass.config.latitude, hass.config.longitude)
    if area is None:
        return False
    try:
        written = await store.async_suggest_area(entry.entry_id, area)
    except Exception as err:  # noqa: BLE001 - a suggestion is a convenience, never a setup failure
        _LOGGER.warning("Storing the suggested area failed: %s", type(err).__name__)
        return False
    if written and preview is not None:
        await preview.async_settings_seeded()
    return written
