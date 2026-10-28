"""What the installation remembers about its electricity price entries, beside the entries themselves.

One domain store (`spotnav.price_sources`), loaded at the integration's setup:

* `migrated`: whether the one-time move of the chargers' own area and fees to price entries has run to its end
  (`pricing/price_setup.py:async_migrate_price_sources`). Set only once every group has its entry and every
  pointer is written, so an interrupted run starts again and reuses what it made.
* `duplicates`: per area, the price entries that hold the same area with different fees, found by the migration
  (decision A: neither is edited). The card asks once whether to use one (`answered` records its answer).
* `wire`: per charger, the source its settings record was last answered from and the highest `revision` it
  carried. The wire revision is the charger's own plus its price entry's write counter
  (`price_source.wire_revision`); when the source moves (a pointer written, an entry added or removed, the
  migration), that sum can fall. The charger then gets an offset that puts it above the highest revision
  answered before, so the card's and the app's revision only grows.
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ..runtime import domain_data

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.price_sources"
#: Seconds a change to the revision ledger waits before it is saved (several reads at once save once).
_SAVE_DELAY_S: Final = 1


def _whole(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class PriceSourceStore:
    """The migration marker, the same-area duplicates and the wire revision ledger (see the module)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.migrated = False
        self._duplicates: list[dict[str, Any]] = []
        self._wire: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        self.migrated = stored.get("migrated") is True
        duplicates = stored.get("duplicates")
        if isinstance(duplicates, list):
            self._duplicates = [
                {
                    "area_id": item["area_id"],
                    "entry_ids": [str(entry_id) for entry_id in item.get("entry_ids") or []],
                    "answered": item.get("answered") is True,
                }
                for item in duplicates
                if isinstance(item, dict) and isinstance(item.get("area_id"), str)
            ]
        wire = stored.get("wire")
        if isinstance(wire, dict):
            self._wire = {
                str(charger_id): {
                    "source": item.get("source") if isinstance(item.get("source"), str) else None,
                    "offset": _whole(item.get("offset")),
                    "high": _whole(item.get("high")),
                }
                for charger_id, item in wire.items()
                if isinstance(item, dict)
            }

    def _data(self) -> dict[str, Any]:
        return {"migrated": self.migrated, "duplicates": self._duplicates, "wire": self._wire}

    async def async_save(self) -> None:
        await self._store.async_save(self._data())

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data, _SAVE_DELAY_S)

    # ------------------------------------------------------------ the migration

    async def async_mark_migrated(self) -> None:
        self.migrated = True
        await self.async_save()

    def duplicates(self) -> list[dict[str, Any]]:
        """Every recorded pair (or more) of same-area price entries with different fees, as copies."""
        return [{**item, "entry_ids": list(item["entry_ids"])} for item in self._duplicates]

    async def async_record_duplicates(self, area_id: str, entry_ids: list[str]) -> None:
        """Record that these price entries hold `area_id` with different fees; joined to the area's record."""
        for item in self._duplicates:
            if item["area_id"] == area_id:
                merged = sorted(set(item["entry_ids"]) | set(entry_ids))
                if merged == item["entry_ids"]:
                    return
                item["entry_ids"] = merged
                break
        else:
            self._duplicates.append({"area_id": area_id, "entry_ids": sorted(set(entry_ids)), "answered": False})
        await self.async_save()

    async def async_answer_duplicates(self, area_id: str) -> bool:
        """The card's question for this area was answered: it is not asked again. False when none is recorded."""
        for item in self._duplicates:
            if item["area_id"] == area_id:
                item["answered"] = True
                await self.async_save()
                return True
        return False

    # ------------------------------------------------------------ the wire revision

    @callback
    def wire_offset(self, charger_id: str, source_id: str | None, settings_revision: int, write_revision: int) -> int:
        """What the charger's wire revision adds to its own: the source's write counter, plus the offset that keeps
        it above every revision answered before when the source moved (see the module)."""
        record = self._wire.get(charger_id)
        if record is None:
            # First seen: nothing answered before is known, so the plain sum stands.
            record = self._wire[charger_id] = {
                "source": source_id,
                "offset": 0,
                "high": settings_revision + write_revision,
            }
            self._schedule_save()
        elif record["source"] != source_id:
            # The new source's counter starts anywhere: lift the sum just above the highest revision answered.
            record["source"] = source_id
            record["offset"] = max(0, record["high"] + 1 - settings_revision - write_revision)
            self._schedule_save()
        added = write_revision + record["offset"]
        if settings_revision + added > record["high"]:
            record["high"] = settings_revision + added
            self._schedule_save()
        return added

    @callback
    def forget(self, charger_id: str) -> None:
        """A charger removed for good: its ledger goes with it."""
        if self._wire.pop(charger_id, None) is not None:
            self._schedule_save()


async def async_setup_price_store(hass: HomeAssistant) -> PriceSourceStore:
    """Load the installation's one store; a second call returns it."""
    data = domain_data(hass)
    if data.price_store is None:
        store = PriceSourceStore(hass)
        await store.async_load()
        data.price_store = store
    return data.price_store


def price_duplicates(hass: HomeAssistant, *, unanswered_only: bool = True) -> list[dict[str, Any]]:
    """The recorded same-area price entries with different fees (`[{area_id, entry_ids, answered}]`), by default
    only those the card has not asked about yet. Entries removed since are left out, and a record left with fewer
    than two is not a question any more."""
    store = domain_data(hass).price_store
    if store is None:
        return []
    found: list[dict[str, Any]] = []
    for item in store.duplicates():
        if unanswered_only and item["answered"]:
            continue
        alive = [entry_id for entry_id in item["entry_ids"] if hass.config_entries.async_get_entry(entry_id)]
        if len(alive) >= 2:
            found.append({**item, "entry_ids": alive})
    return found
