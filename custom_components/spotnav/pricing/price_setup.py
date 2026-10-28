"""Electricity price entries SpotNav makes and points at itself: the first install, the one-time migration, a card
write with no price entry yet, and the pointers a removed entry leaves.

Every entry is made through the price flow's import step (`flows/price.py:async_step_import`), judged and named as
the form would, and only while `price_setup_lock` is held, so two chargers set up at once make one entry.

* **First install** (`async_ensure_first_price_entry`): once the migration has run and no electricity price entry
  exists, a charger's or site's setup makes one from the area Home Assistant's location suggests
  (`planning/first_run.py:suggest_area`) and the fees the price flow preselects for it. No question is asked;
  without a suggestion nothing is made (the card asks for the area, and that write makes it: below).
* **A card write** (`async_price_entry_from_settings`): a write that sets the area of a charger that prices with
  its own settings, while no price entry exists, makes one from that area and its row, and points at it.
* **Migration** (`async_migrate_price_sources`, once per installation, after start): each charger's area and that
  area's row are its effective fees; the chargers are grouped by them. A price entry holding exactly that area and
  those fees is reused, else one is made. A site whose chargers all landed in one group points at it; the chargers
  of a site that disagree point at theirs themselves, as does a charger without a site. A price entry with the
  same area but other fees is never edited: the group gets its own, and the pair is recorded for the card's
  question (`price_store.py`). The chargers' own settings are left as they were (a rollback reads them). The
  marker is set only at the end, so an interrupted run starts again and reuses what it made.
* **Removal** (`async_drop_pointers`): a price entry that is removed takes every pointer to it along.

Pointers are written in the entries' data (`CONF_PRICE_ENTRY_ID`), on a site when the charger is on one, else on
the charger. Neither entry reloads for it; every consumer is told (`async_price_sources_changed`) and resolves again.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import callback, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from ..const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_PRICE_AREA,
    CONF_PRICE_ENTRY_ID,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    ENTRY_TYPE_PRICE,
    ENTRY_TYPE_SITE,
)
from ..planning.auto_settings import AreaAutoSettings, AutoSettings
from ..planning.first_run import site_for_charger, suggest_area
from ..runtime import domain_data, entry_kind
from .price_points import default_fee_mode, FEE_OFF, fee_mode, fee_override, included
from .price_source import async_price_sources_changed, FEE_COMPONENTS, fees_from_data, price_entries, wire_revision
from .relay_contract import AreaCatalogue, AreaEntry

_LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers


def _entries(hass: HomeAssistant, kind: str) -> list[ConfigEntry]:
    return [entry for entry in hass.config_entries.async_entries(DOMAIN) if entry_kind(entry) == kind]


def any_price_entry(hass: HomeAssistant) -> bool:
    """Whether any electricity price entry exists, loaded, disabled or not."""
    return bool(_entries(hass, ENTRY_TYPE_PRICE))


def effective_fees(area: AreaEntry, overrides: AreaAutoSettings) -> AreaAutoSettings:
    """An area's fees as a price entry stores them: a fee the area's price holds is off, an off fee keeps no
    figure. Two sets of fees that price alike compare equal."""
    fees = {}
    for component in FEE_COMPONENTS:
        fee = getattr(overrides, component)
        fees[component] = (
            fee_override(FEE_OFF, None) if included(area, component) else fee_override(fee_mode(fee), fee.value)
        )
    return AreaAutoSettings(area_id=area.id, **fees)


def _sort_key(fees: AreaAutoSettings) -> str:
    return json.dumps(fees.as_dict(), sort_keys=True)


async def _async_catalogue(hass: HomeAssistant) -> AreaCatalogue | None:
    manager = domain_data(hass).price_refresh
    if manager is None:
        return None
    await manager.async_ensure_catalogue()
    return manager.catalogue_snapshot().catalogue


async def async_create_price_entry(hass: HomeAssistant, fees: AreaAutoSettings) -> ConfigEntry | None:
    """A new electricity price entry for these area and fees, through the price flow's import step; `None` when
    the step refused them (logged)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={CONF_PRICE_AREA: fees.area_id, **{name: getattr(fees, name).as_dict() for name in FEE_COMPONENTS}},
    )
    if result["type"] is FlowResultType.CREATE_ENTRY:
        return result["result"]
    _LOGGER.warning("An electricity price for %s could not be added: %s", fees.area_id, result.get("reason"))
    return None


@callback
def _write_pointer(hass: HomeAssistant, entry: ConfigEntry, price_entry_id: str | None) -> bool:
    """Store (or with `None` drop) an entry's pointer; whether anything changed."""
    if entry.data.get(CONF_PRICE_ENTRY_ID) == price_entry_id:
        return False
    data = {key: value for key, value in entry.data.items() if key != CONF_PRICE_ENTRY_ID}
    if price_entry_id is not None:
        data[CONF_PRICE_ENTRY_ID] = price_entry_id
    hass.config_entries.async_update_entry(entry, data=data)
    return True


@callback
def point_charger(hass: HomeAssistant, charger_id: str, price_entry_id: str) -> None:
    """Point the charger's site at the price entry, or the charger itself when it has no site."""
    holder = site_for_charger(hass, charger_id) or hass.config_entries.async_get_entry(charger_id)
    if holder is not None and _write_pointer(hass, holder, price_entry_id):
        async_price_sources_changed(hass, price_entry_id)


@callback
def _point_everything(hass: HomeAssistant, price_entry_id: str) -> None:
    """Every site, and every charger without one, at the price entry (the installation's first)."""
    sited: set[str] = set()
    for site in _entries(hass, ENTRY_TYPE_SITE):
        sited.update(site.data.get(CONF_CHARGER_ENTRY_IDS) or [])
        _write_pointer(hass, site, price_entry_id)
    for charger in _entries(hass, ENTRY_TYPE_CHARGER):
        if charger.entry_id not in sited:
            _write_pointer(hass, charger, price_entry_id)
    async_price_sources_changed(hass, price_entry_id)


# ------------------------------------------------------------------ first install


async def async_ensure_first_price_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The installation's first electricity price entry, made from Home Assistant's location, when there is none
    yet and the migration has run (before it, the migration decides). `None` when nothing was made."""
    store = domain_data(hass).price_store
    if store is None or not store.migrated or any_price_entry(hass) or not hass.config.country:
        return None
    catalogue = await _async_catalogue(hass)
    async with domain_data(hass).price_setup_lock:
        return await _async_first_price_entry(hass, catalogue)


async def _async_first_price_entry(hass: HomeAssistant, catalogue: AreaCatalogue | None) -> ConfigEntry | None:
    """The first install's entry, with `price_setup_lock` held."""
    if any_price_entry(hass) or not (_entries(hass, ENTRY_TYPE_CHARGER) or _entries(hass, ENTRY_TYPE_SITE)):
        return None
    config = hass.config
    area_id = suggest_area(catalogue, config.country, config.latitude, config.longitude)
    area = None if area_id is None or catalogue is None else catalogue.area(area_id)
    if area is None:
        return None
    created = await async_create_price_entry(hass, effective_fees(area, _suggested(area)))
    if created is not None:
        _point_everything(hass, created.entry_id)
    return created


def _suggested(area: AreaEntry) -> AreaAutoSettings:
    """The fees the price flow preselects for an area: its suggestion where it has one, else nothing."""
    return AreaAutoSettings(
        area_id=area.id,
        **{component: fee_override(default_fee_mode(area, component), None) for component in FEE_COMPONENTS},
    )


# ------------------------------------------------------------------ a card write


async def async_price_entry_from_settings(
    hass: HomeAssistant, charger_id: str, before: AutoSettings, after: AutoSettings
) -> ConfigEntry | None:
    """After a settings write committed for a charger that prices with its own settings: when it set the area and
    no electricity price entry exists, one is made from that area and its row, and the charger (or its site)
    points at it. `None` when nothing was made."""
    if after.area_id is None or after.area_id == before.area_id or any_price_entry(hass):
        return None
    catalogue = await _async_catalogue(hass)
    async with domain_data(hass).price_setup_lock:
        if any_price_entry(hass):
            return None
        area = None if catalogue is None else catalogue.area(after.area_id)
        if area is None:
            return None
        created = await async_create_price_entry(hass, effective_fees(area, after.override_for(after.area_id)))
        if created is not None:
            point_charger(hass, charger_id, created.entry_id)
        return created


# ------------------------------------------------------------------ removal


@callback
def async_drop_pointers(hass: HomeAssistant, price_entry_id: str) -> None:
    """A price entry is gone: no site or charger keeps pointing at it."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_PRICE_ENTRY_ID) == price_entry_id:
            _write_pointer(hass, entry, None)


# ------------------------------------------------------------------ migration


async def async_migrate_price_sources(hass: HomeAssistant) -> bool:
    """Move the chargers' own area and fees to price entries, once (see the module). True when the installation is
    migrated after the call; False while it cannot run yet (no settings store, no area list)."""
    data = domain_data(hass)
    store, auto = data.price_store, data.auto_store
    if store is None or auto is None:
        return False
    if store.migrated:
        return True
    catalogue = await _async_catalogue(hass)
    if catalogue is None:
        return False
    async with data.price_setup_lock:
        if store.migrated:
            return True
        chargers = [entry.entry_id for entry in _entries(hass, ENTRY_TYPE_CHARGER)]
        # Each charger's revision as answered now, before any source moves: a moved source then answers above it.
        for charger_id in chargers:
            wire_revision(hass, charger_id, auto.settings(charger_id).revision)
        groups = _groups(auto, catalogue, chargers)
        chosen: dict[str, str] = {}
        made: dict[AreaAutoSettings, str] = {}
        for key in sorted(groups, key=_sort_key):
            entry_id = _reused(hass, catalogue, key) or await _async_made(hass, key)
            if entry_id is None:
                continue
            made[key] = entry_id
            for charger_id in groups[key]:
                chosen[charger_id] = entry_id
        _write_pointers(hass, chargers, groups, chosen)
        for key, entry_id in made.items():
            await _async_record_duplicates(hass, catalogue, key, entry_id)
        for entry_id in set(chosen.values()):
            async_price_sources_changed(hass, entry_id)
        await store.async_mark_migrated()
        _LOGGER.debug("Electricity prices migrated: %s", chosen)
        # An installation whose chargers named no area still gets its first entry from the location.
        await _async_first_price_entry(hass, catalogue)
    return True


def _groups(auto: Any, catalogue: AreaCatalogue, chargers: Iterable[str]) -> dict[AreaAutoSettings, list[str]]:
    """The chargers by their effective area and fees; one with no area, or an area the list lacks, in none."""
    groups: dict[AreaAutoSettings, list[str]] = {}
    for charger_id in chargers:
        settings = auto.settings(charger_id)
        area = None if settings.area_id is None else catalogue.area(settings.area_id)
        if area is None:
            continue
        groups.setdefault(effective_fees(area, settings.override_for(area.id)), []).append(charger_id)
    return groups


def _entry_fees(catalogue: AreaCatalogue, entry: ConfigEntry) -> AreaAutoSettings | None:
    area = catalogue.area(str(entry.data.get(CONF_PRICE_AREA)))
    return None if area is None else effective_fees(area, fees_from_data(area.id, entry.data))


def _reused(hass: HomeAssistant, catalogue: AreaCatalogue, key: AreaAutoSettings) -> str | None:
    """The first price entry holding exactly this area and these fees."""
    for entry in price_entries(hass):
        if _entry_fees(catalogue, entry) == key:
            return entry.entry_id
    return None


async def _async_made(hass: HomeAssistant, key: AreaAutoSettings) -> str | None:
    created = await async_create_price_entry(hass, key)
    return None if created is None else created.entry_id


@callback
def _write_pointers(
    hass: HomeAssistant, chargers: list[str], groups: dict[AreaAutoSettings, list[str]], chosen: dict[str, str]
) -> None:
    """A site whose chargers with an area all have one entry points at it; otherwise those chargers point at theirs
    themselves (D3). A charger without a site points at its own."""
    with_area = {charger_id for members in groups.values() for charger_id in members}
    sited: set[str] = set()
    for site in _entries(hass, ENTRY_TYPE_SITE):
        members = [charger_id for charger_id in site.data.get(CONF_CHARGER_ENTRY_IDS) or [] if charger_id in chargers]
        sited.update(members)
        named = [charger_id for charger_id in members if charger_id in with_area]
        targets = {chosen.get(charger_id) for charger_id in named}
        if len(targets) == 1 and None not in targets:
            _write_pointer(hass, site, targets.pop())
            continue
        for charger_id in named:
            if charger_id in chosen:
                _write_pointer(hass, hass.config_entries.async_get_entry(charger_id), chosen[charger_id])
    for charger_id in chargers:
        if charger_id not in sited and charger_id in chosen:
            _write_pointer(hass, hass.config_entries.async_get_entry(charger_id), chosen[charger_id])


async def _async_record_duplicates(
    hass: HomeAssistant, catalogue: AreaCatalogue, key: AreaAutoSettings, entry_id: str
) -> None:
    """Record the group's entry beside every price entry of the same area with other fees (decision A)."""
    others = [
        entry.entry_id
        for entry in price_entries(hass)
        if entry.entry_id != entry_id
        and str(entry.data.get(CONF_PRICE_AREA)) == key.area_id
        and _entry_fees(catalogue, entry) != key
    ]
    store = domain_data(hass).price_store
    if others and store is not None:
        await store.async_record_duplicates(key.area_id, [entry_id, *others])


async def async_start_price_migration(hass: HomeAssistant) -> None:
    """Run the migration once Home Assistant has started; while the area list cannot be read, again as soon as it
    can."""
    if await async_migrate_price_sources(hass):
        return
    manager = domain_data(hass).price_refresh
    store = domain_data(hass).price_store
    if manager is None or store is None or store.migrated:
        return
    removers: list[Any] = []

    @callback
    def _on_catalogue(snapshot: Any) -> None:
        if snapshot.catalogue is None or not removers:
            return
        for remove in removers:
            remove()
        removers.clear()
        hass.async_create_task(async_start_price_migration(hass), "spotnav price migration")

    removers.append(manager.add_catalogue_listener(_on_catalogue))
