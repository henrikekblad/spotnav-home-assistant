"""Where a charger's price area and fees come from: one answer every reader asks for.

`price_source_for(hass, charger_id)` names the source, in this order:

1. the charger entry's own pointer (`CONF_PRICE_ENTRY_ID` in its entry data) to an electricity price entry;
2. else its site entry's pointer;
3. else the only electricity price entry, when there is exactly one;
4. else the charger's own Auto settings, as before there were price entries: its `area_id` and that area's
   override row, with no entry id. `None` when those name no area (the "area" an Auto plan misses).

A pointer to an entry that is gone, disabled or has no area is passed over, never an error. An entry counts while
it is enabled and its setup has not failed; it need not be loaded yet, so a charger set up before its price entry
at start-up prices with the right fees from its first calculation. The area and fees are read from the entry's
data, never from its live device.

`effective_settings` is what readers use: the charger's settings with `area_id` and that area's override row taken
from the source, every other row (the fees of areas the charger used before, which price its old sessions) as
stored. With no price entry it is the stored record itself, so nothing changes for an installation without one.

`PriceSource.revision` names the source's area and fees (a digest, so equal fees are equal revisions across a
restart). A price entry's revision enters a plan's identity and the executor's checks (`plan_revision`), so a fee
change on the price entry replans every charger that uses it; the charger's own fees are already covered by its
settings revision, so the fallback adds nothing there and every identity stays what it was.

A price entry that is added, changed, disabled or removed is announced with `SIGNAL_PRICE_SOURCES` (and its own
`signal_price_source(entry_id)`); a consumer resolves again and acts when its source moved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_PRICE_AREA,
    CONF_PRICE_ENTRY_ID,
    DOMAIN,
    ENTRY_TYPE_PRICE,
    ENTRY_TYPE_SITE,
)
from ..planning.auto_settings import AreaAutoSettings, AutoSettings, FiscalOverride
from ..runtime import domain_data
from .relay_contract import AreaEntry

if TYPE_CHECKING:
    from ..planning.planner import FiscalChoice


#: How a source was found: the charger's pointer, its site's, the only price entry, or the charger's own settings.
VIA_CHARGER: Final = "charger"
VIA_SITE: Final = "site"
VIA_ONLY: Final = "only"
VIA_OWN: Final = "own"

#: Sent with the price entry's id whenever any electricity price entry is added, changed, disabled or removed.
SIGNAL_PRICE_SOURCES: Final = f"{DOMAIN}_price_sources"

#: A price entry's own fees, as a price entry stores them.
FEE_COMPONENTS: Final = ("vat", "tax", "transfer")

#: States in which an entry's data is not to be trusted as a price source.
_FAILED_STATES: Final = (
    ConfigEntryState.SETUP_ERROR,
    ConfigEntryState.MIGRATION_ERROR,
    ConfigEntryState.FAILED_UNLOAD,
)


def signal_price_source(entry_id: str) -> str:
    """The signal one price entry's own change is sent on."""
    return f"{DOMAIN}_price_source_{entry_id}"


@callback
def async_price_sources_changed(hass: HomeAssistant, entry_id: str) -> None:
    """Tell every consumer that this price entry was added, changed, disabled or removed."""
    async_dispatcher_send(hass, signal_price_source(entry_id), entry_id)
    async_dispatcher_send(hass, SIGNAL_PRICE_SOURCES, entry_id)


@dataclass(frozen=True, slots=True)
class PriceSource:
    """One charger's price area and fees, and where they come from."""

    #: The electricity price entry, or `None` for the charger's own settings.
    entry_id: str | None
    via: str
    area_id: str
    #: The source's fees, as one area's override row (`fee figures in the area's minor unit`).
    overrides: AreaAutoSettings
    #: The area's catalogue entry, or `None` while the catalogue does not list it.
    area: AreaEntry | None
    revision: str

    @property
    def fiscal(self) -> FiscalChoice | None:
        """The fees resolved for the area exactly as a plan resolves them, or `None` when they cannot be."""
        if self.area is None:
            return None
        from ..planning.auto_controller import fiscal_choice_for  # noqa: PLC0415 - the planner imports this module

        return fiscal_choice_for(AutoSettings(area_id=self.area_id, overrides=(self.overrides,)), self.area)

    @property
    def currency(self) -> str | None:
        return None if self.area is None else self.area.currency

    @property
    def major_unit(self) -> str | None:
        return None if self.area is None else self.area.major_unit

    @property
    def minor_unit(self) -> str | None:
        return None if self.area is None else self.area.minor_unit

    @property
    def plan_revision(self) -> str | None:
        """What a plan's identity carries: a price entry's revision, `None` for the charger's own settings (its
        settings revision already covers them)."""
        return None if self.entry_id is None else self.revision

    def diagnostics(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id, "area_id": self.area_id, "revision": self.revision, "via": self.via}


def source_revision(overrides: AreaAutoSettings) -> str:
    """A digest of an area and its fees: equal for equal fees, different when the area or a fee moves."""
    canonical = json.dumps(overrides.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def fees_from_data(area_id: str, data: Any) -> AreaAutoSettings:
    """A price entry's stored fees as one area's override row. A missing or unreadable fee is off."""
    fees: dict[str, FiscalOverride] = {}
    for component in FEE_COMPONENTS:
        try:
            fees[component] = FiscalOverride.from_stored(data.get(component), component)
        except Exception:  # noqa: BLE001 - a stored fee that cannot be read adds nothing
            fees[component] = FiscalOverride()
    return AreaAutoSettings(area_id=area_id, **fees)


def _usable(entry: ConfigEntry | None) -> bool:
    """An electricity price entry that may be a source: enabled, not failed, with an area."""
    return (
        entry is not None
        and entry.domain == DOMAIN
        and entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_PRICE
        and entry.disabled_by is None
        and entry.state not in _FAILED_STATES
        and bool(entry.data.get(CONF_PRICE_AREA))
    )


def price_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every electricity price entry that may be a source."""
    return [entry for entry in hass.config_entries.async_entries(DOMAIN) if _usable(entry)]


def _pointed(hass: HomeAssistant, entry: ConfigEntry | None) -> ConfigEntry | None:
    pointer = None if entry is None else entry.data.get(CONF_PRICE_ENTRY_ID)
    if not isinstance(pointer, str) or not pointer:
        return None
    target = hass.config_entries.async_get_entry(pointer)
    return target if _usable(target) else None


def _site_of(hass: HomeAssistant, charger_id: str) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE and charger_id in (
            entry.data.get(CONF_CHARGER_ENTRY_IDS) or []
        ):
            return entry
    return None


def _area(hass: HomeAssistant, area_id: str) -> AreaEntry | None:
    repository = domain_data(hass).price_repository
    return None if repository is None else repository.catalogue_snapshot().area(area_id)


def _from_entry(hass: HomeAssistant, entry: ConfigEntry, via: str) -> PriceSource:
    area_id = str(entry.data[CONF_PRICE_AREA])
    overrides = fees_from_data(area_id, entry.data)
    return PriceSource(
        entry_id=entry.entry_id,
        via=via,
        area_id=area_id,
        overrides=overrides,
        area=_area(hass, area_id),
        revision=source_revision(overrides),
    )


def entry_source_for(hass: HomeAssistant, charger_id: str) -> PriceSource | None:
    """The electricity price entry the charger prices with (its pointer, its site's, the only entry), or `None`
    when none applies and its own settings decide."""
    charger = hass.config_entries.async_get_entry(charger_id)
    pointed = _pointed(hass, charger)
    if pointed is not None:
        return _from_entry(hass, pointed, VIA_CHARGER)
    pointed = _pointed(hass, _site_of(hass, charger_id))
    if pointed is not None:
        return _from_entry(hass, pointed, VIA_SITE)
    entries = price_entries(hass)
    if len(entries) == 1:
        return _from_entry(hass, entries[0], VIA_ONLY)
    return None


def price_source_for(
    hass: HomeAssistant, charger_id: str, settings: AutoSettings | None = None
) -> PriceSource | None:
    """The charger's price source (see the module), or `None` when nothing names an area.

    `settings` is the charger's own record when the caller holds it already (the fallback reads it); otherwise the
    settings store's.
    """
    source = entry_source_for(hass, charger_id)
    if source is not None:
        return source
    if settings is None:
        store = domain_data(hass).auto_store
        settings = None if store is None else store.settings(charger_id)
    if settings is None or not settings.area_id:
        return None
    overrides = settings.override_for(settings.area_id)
    return PriceSource(
        entry_id=None,
        via=VIA_OWN,
        area_id=settings.area_id,
        overrides=overrides,
        area=_area(hass, settings.area_id),
        revision=source_revision(overrides),
    )


def with_source(settings: AutoSettings, source: PriceSource | None) -> AutoSettings:
    """`settings` with the source's area and that area's fees; every other area's row as stored.

    The charger's own settings (or none) leave the record exactly as it is.
    """
    if source is None or source.entry_id is None:
        return settings
    rows = tuple(row for row in settings.overrides if row.area_id != source.area_id) + (source.overrides,)
    return replace(settings, area_id=source.area_id, overrides=tuple(sorted(rows, key=lambda row: row.area_id)))


def effective_settings(hass: HomeAssistant, charger_id: str, settings: AutoSettings) -> AutoSettings:
    """The charger's settings as every reader of area and fees must see them (see the module)."""
    return with_source(settings, entry_source_for(hass, charger_id))


def plan_revision_for(hass: HomeAssistant, charger_id: str) -> str | None:
    """The price revision a plan for this charger is built on now (`PriceSource.plan_revision`)."""
    source = entry_source_for(hass, charger_id)
    return None if source is None else source.plan_revision
