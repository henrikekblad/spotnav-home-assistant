"""First-run defaults: area by location and amps from the site and the charger."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import (
        CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_PHASE_WIRING,
    CONF_SAFETY_MARGIN_A,
    DOMAIN,
    ENTRY_TYPE_SITE,
)
from custom_components.spotnav.execution.controller import (
    CURRENT_RANGE_SOURCE_CURRENT_LIMIT,
    CURRENT_RANGE_SOURCE_DEFAULT,
    current_range_dict,
)
from custom_components.spotnav.planning.auto_settings import AutoSettings, AutoSettingsStore
from custom_components.spotnav.planning.first_run import (
    AREA_REFERENCE_POINTS,
    async_seed_first_run,
    first_run_defaults,
    suggest_area,
)
from custom_components.spotnav.pricing.relay_contract import parse_catalogue
from custom_components.spotnav.runtime import domain_data

pytestmark = pytest.mark.first_run_defaults

#: The areas the relay publishes today, in its order.
AREAS = {
    "SE1": "SE", "SE2": "SE", "SE3": "SE", "SE4": "SE",
    "NO1": "NO", "NO2": "NO", "NO3": "NO", "NO4": "NO", "NO5": "NO",
    "DK1": "DK", "DK2": "DK", "FI": "FI", "DE-LU": "DE", "EE": "EE",
}


def _catalogue(areas: dict[str, str] = AREAS):
    def entry(area_id: str, country: str) -> dict[str, Any]:
        countries = ["DE", "LU"] if area_id == "DE-LU" else [country]
        return {
            "id": area_id, "eic": f"eic-{area_id}", "countries": countries, "name": area_id,
            "tz": "Europe/Stockholm", "currency": "EUR", "major_unit": "€", "minor_unit": "cent",
        }

    return parse_catalogue(
        {"v": 1, "generated": "2026-09-22T00:00:00+00:00", "areas": [entry(k, v) for k, v in areas.items()]}
    )


CATALOGUE = _catalogue()


@pytest.mark.parametrize(
    ("place", "country", "lat", "lon", "expected"),
    [
        ("Kiruna", "SE", 67.86, 20.23, "SE1"),
        ("Luleå", "SE", 65.58, 22.15, "SE1"),
        ("Sundsvall", "SE", 62.39, 17.31, "SE2"),
        ("Umeå", "SE", 63.83, 20.26, "SE2"),
        ("Stockholm", "SE", 59.33, 18.07, "SE3"),
        ("Gothenburg", "SE", 57.71, 11.97, "SE3"),
        ("Malmö", "SE", 55.60, 13.00, "SE4"),
        ("Växjö", "SE", 56.88, 14.81, "SE4"),
        ("Oslo", "NO", 59.91, 10.75, "NO1"),
        ("Kristiansand", "NO", 58.15, 8.00, "NO2"),
        ("Trondheim", "NO", 63.43, 10.40, "NO3"),
        ("Tromsø", "NO", 69.65, 18.96, "NO4"),
        ("Bergen", "NO", 60.39, 5.32, "NO5"),
        ("Aarhus", "DK", 56.16, 10.21, "DK1"),
        ("Copenhagen", "DK", 55.68, 12.57, "DK2"),
        ("Helsinki (one area)", "FI", 60.17, 24.94, "FI"),
        ("Tallinn (one area)", "EE", 59.44, 24.75, "EE"),
        ("Berlin (one area)", "DE", 52.52, 13.40, "DE-LU"),
        ("Luxembourg (one of two countries)", "LU", 49.61, 6.13, "DE-LU"),
        ("lower-case country", "se", 59.33, 18.07, "SE3"),
    ],
)
def test_the_area_follows_the_country_and_the_position(place, country, lat, lon, expected) -> None:
    assert suggest_area(CATALOGUE, country, lat, lon) == expected, place


def test_a_single_area_country_needs_no_position() -> None:
    assert suggest_area(CATALOGUE, "FI", None, None) == "FI"


@pytest.mark.parametrize(
    ("country", "lat", "lon"),
    [
        (None, 59.33, 18.07),
        ("", 59.33, 18.07),
        ("XX", 59.33, 18.07),  # not in the catalogue
        ("IT", 41.9, 12.5),  # a country the relay does not list
        ("SE", None, None),  # several areas and no position
        ("SE", 32.87, -117.23),  # Home Assistant's unset default position
        ("NO", 0.0, 0.0),
    ],
)
def test_nothing_trustworthy_leaves_the_area_empty(country, lat, lon) -> None:
    assert suggest_area(CATALOGUE, country, lat, lon) is None


def test_no_catalogue_leaves_the_area_empty() -> None:
    assert suggest_area(None, "SE", 59.33, 18.07) is None


def test_a_multi_area_country_without_a_table_is_left_empty() -> None:
    catalogue = _catalogue({"IT-N": "IT", "IT-S": "IT"})
    assert suggest_area(catalogue, "IT", 45.46, 9.19) is None


def test_every_zone_the_relay_publishes_for_a_split_country_has_reference_points() -> None:
    for area_id, country in AREAS.items():
        if sum(1 for other in AREAS.values() if other == country) > 1:
            assert AREA_REFERENCE_POINTS.get(area_id), area_id


@pytest.mark.parametrize(
    ("charger_max", "site_limit", "amps"),
    [
        (None, None, 16),  # nothing known: the everyday 16 A
        (32, None, 32),  # the charger states its maximum
        (32, 24, 24),  # never above fuse minus margin
        (None, 10, 10),  # the unknown 16 is capped too
        (10, 25, 10),
        (None, 5, None),  # below the charger's minimum: left empty
    ],
)
def test_amps(charger_max, site_limit, amps) -> None:
    settings, suggested = first_run_defaults(
        catalogue=CATALOGUE, country="SE", latitude=59.33, longitude=18.07,
        charger_max_a=charger_max, site_limit_a=site_limit,
    )
    assert (settings.amps, settings.area_id) == (amps, "SE3")
    # The phases are not defaulted: the charger's wiring and the car decide them.
    assert settings.phases is None
    assert suggested == tuple(name for name, value in (("area", "SE3"), ("amps", amps)) if value is not None)


def test_nothing_is_defaulted_beyond_area_and_amps() -> None:
    settings, _ = first_run_defaults(
        catalogue=CATALOGUE, country="SE", latitude=59.33, longitude=18.07,
        charger_max_a=None, site_limit_a=None,
    )
    defaults = AutoSettings()
    assert settings.requested_kwh == defaults.requested_kwh
    assert settings.overrides == () and settings.driver == defaults.driver
    assert settings.strategy == defaults.strategy and settings.pause == defaults.pause


# --- the store and the glue ---------------------------------------------------------------------


class _Manager:
    def __init__(self, catalogue) -> None:
        self._catalogue = catalogue

    def catalogue_snapshot(self):
        return SimpleNamespace(catalogue=self._catalogue)


class _Controller:
    def __init__(self, maximum: int | None) -> None:
        self._range = (
            current_range_dict(32, CURRENT_RANGE_SOURCE_DEFAULT)
            if maximum is None
            else current_range_dict(maximum, CURRENT_RANGE_SOURCE_CURRENT_LIMIT)
        )

    def current_range(self):
        return self._range


class _Preview:
    def __init__(self) -> None:
        self.seeded = 0

    async def async_settings_seeded(self) -> None:
        self.seeded += 1


async def _setup(
    hass, *, country="SE", latitude=67.86, longitude=20.23, catalogue=CATALOGUE, entry_data=None
):
    hass.config.country = country
    hass.config.latitude = latitude
    hass.config.longitude = longitude
    store = AutoSettingsStore(hass, store=SimpleNamespace(async_load=_none, async_save=_save))
    await store.async_load()
    data = domain_data(hass)
    data.auto_store = store
    data.price_refresh = _Manager(catalogue)
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data if entry_data is not None else {}, entry_id="charger_a"
    )
    entry.add_to_hass(hass)
    return store, entry


async def _none():
    return None


async def _save(_data):
    return None


async def test_a_new_charger_is_seeded_once_and_marked(hass) -> None:
    store, entry = await _setup(hass)
    preview = _Preview()
    assert await async_seed_first_run(hass, entry, _Controller(None), preview)
    settings = store.settings(entry.entry_id)
    assert (settings.area_id, settings.phases, settings.amps) == ("SE1", None, 16)
    assert store.suggested(entry.entry_id) == ("area", "amps")
    assert preview.seeded == 1
    assert settings.missing_for_auto() == ()
    # Seeded once: a second start finds a record and writes nothing.
    assert not await async_seed_first_run(hass, entry, _Controller(None), preview)
    assert preview.seeded == 1


async def test_the_charger_maximum_and_the_site_shape_the_defaults(hass) -> None:
    store, entry = await _setup(hass, country="NO", latitude=60.39, longitude=5.32)
    MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SITE,
            CONF_CHARGER_ENTRY_IDS: [entry.entry_id],
            CONF_PHASE_WIRING: {entry.entry_id: {"phases": 1, "phase": "L2"}},
            CONF_MAIN_FUSE_A: 20.0,
            CONF_SAFETY_MARGIN_A: 2.5,
        },
    ).add_to_hass(hass)
    await async_seed_first_run(hass, entry, _Controller(32), None)
    settings = store.settings(entry.entry_id)
    # The site's wiring is not copied into the settings: the planner reads it from the site.
    assert (settings.area_id, settings.phases, settings.amps) == ("NO5", None, 17)


async def test_no_country_seeds_amps_but_no_area(hass) -> None:
    store, entry = await _setup(hass, country=None)
    await async_seed_first_run(hass, entry, _Controller(None), None)
    settings = store.settings(entry.entry_id)
    assert (settings.area_id, settings.phases, settings.amps) == (None, None, 16)
    assert store.suggested(entry.entry_id) == ("amps",)
    assert settings.missing_for_auto() == ("area",)


async def test_a_value_a_person_saved_is_never_overwritten_or_reapplied(hass) -> None:
    store, entry = await _setup(hass)
    await store.async_update(
        entry.entry_id,
        mutate=lambda current: AutoSettings(area_id="FI", phases=1, amps=10),
        confirm=True,
    )
    assert not await async_seed_first_run(hass, entry, _Controller(None), None)
    assert store.settings(entry.entry_id).area_id == "FI"

    # Clearing a value is a save too: it stays cleared.
    await store.async_update(
        entry.entry_id, mutate=lambda current: AutoSettings(area_id=None), confirm=True
    )
    assert not await async_seed_first_run(hass, entry, _Controller(None), None)
    assert store.settings(entry.entry_id).area_id is None


async def test_a_record_nobody_edited_is_still_seeded(hass) -> None:
    """Proposal or baseline writes can create a revision-0 record; it has no person's choice in it."""
    store, entry = await _setup(hass)
    await store.async_update(entry.entry_id)
    assert store.settings(entry.entry_id).revision == 0
    assert await async_seed_first_run(hass, entry, _Controller(None), None)


async def test_saving_settings_clears_the_marker_but_system_writes_do_not(hass) -> None:
    store, entry = await _setup(hass)
    await async_seed_first_run(hass, entry, _Controller(None), None)
    await store.async_update(entry.entry_id, mutate=lambda current: current)  # a system write
    assert store.suggested(entry.entry_id) == ("area", "amps")
    await store.async_update(entry.entry_id, mutate=lambda current: current, confirm=True)
    assert store.suggested(entry.entry_id) == ()


async def test_the_marker_survives_a_restart(hass) -> None:
    saved: dict[str, Any] = {}

    async def load():
        return saved.get("doc")

    async def save(document):
        saved["doc"] = document

    hass.config.country, hass.config.latitude, hass.config.longitude = "SE", 55.6, 13.0
    store = AutoSettingsStore(hass, store=SimpleNamespace(async_load=load, async_save=save))
    await store.async_load()
    assert await store.async_seed("a", AutoSettings(area_id="SE4", amps=16), ("area", "amps"))
    reopened = AutoSettingsStore(hass, store=SimpleNamespace(async_load=load, async_save=save))
    await reopened.async_load()
    assert reopened.suggested("a") == ("area", "amps")
    assert reopened.settings("a").area_id == "SE4"


async def test_a_store_written_before_the_marker_existed_still_loads(hass) -> None:
    saved: dict[str, Any] = {}

    async def load():
        return saved.get("doc")

    async def save(document):
        saved["doc"] = document

    store = AutoSettingsStore(hass, store=SimpleNamespace(async_load=load, async_save=save))
    await store.async_load()
    await store.async_update("a", mutate=lambda current: AutoSettings(area_id="SE4", phases=3, amps=16), confirm=True)
    for record in saved["doc"]["chargers"].values():
        del record["suggested"]
    reopened = AutoSettingsStore(hass, store=SimpleNamespace(async_load=load, async_save=save))
    await reopened.async_load()
    assert reopened.settings("a").area_id == "SE4"
    assert reopened.suggested("a") == ()
