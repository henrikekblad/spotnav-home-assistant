"""What an area's price source is, and the one thing the person can do when it has none they can keep.

The choice itself is [choose_provider] (`providers/__init__.py`): Home Assistant's PVPC integration for Spain's
PVPC, the Octopus Energy integration's Agile rates for a Great Britain region, Home Assistant's Nord Pool
integration for an area Nord Pool trades, the person's ENTSO-E key for an area the platform publishes. An area that
gets none of these has no source: nothing is fetched for it (the SpotNav relay delivers no prices since 2.0), so it
carries a **notice** naming the action that gives it one ([source_notice]). Nothing else is a notice: a source
that is there but failing (Nord Pool unreachable, ENTSO-E asking to wait) stays what it is today, `stale` or
`degraded`, because there is nothing to do but wait.

| Area | State | Notice |
|---|---|---|
| PVPC | a `pvpc_hourly_pricing` entry loaded | none |
| PVPC | an entry, not loaded | `pvpc_not_loaded` |
| PVPC | no entry | `add_pvpc` |
| GB region | an Agile meter in the region | none |
| GB region | an Agile meter in one other region, read for it | `octopus_region_mismatch` `{region}` |
| GB region | an `octopus_energy` entry, not loaded | `octopus_not_loaded` |
| GB region | the integration loaded, no Agile meter for the region | `octopus_no_agile` |
| GB region | no entry | `add_octopus` |
| Nord Pool area | a `nordpool` entry loaded, or a usable ENTSO-E key | none |
| Nord Pool area | a `nordpool` entry, not loaded | `nordpool_not_loaded` |
| Nord Pool area | no entry, the ENTSO-E key rejected | `entsoe_key_rejected` |
| Nord Pool area | no entry, no key | `add_nordpool` |
| ENTSO-E area | a usable key | none |
| ENTSO-E area | the key rejected | `entsoe_key_rejected` |
| ENTSO-E area | no key | `add_entsoe_key` |
| any other | | `source_missing` |

The notice shows as soon as the area has no source, also while it still holds today's prices (kept from before
2.0); only the Repairs issue waits for prices to have stopped (`price_device.py`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

#: A config entry of an integration a provider reads: loaded, there but not loaded (failed, disabled, still
#: starting), or none.
EntryState = Literal["loaded", "not_loaded", "none"]

#: An ENTSO-E key: none stored, or the stored key's state.
KeyState = Literal["none", "ok", "unchecked", "rejected"]

#: Every notice code, in the order of the table above.
NOTICE_CODES: Final = (
    "pvpc_not_loaded",
    "add_pvpc",
    "octopus_region_mismatch",
    "octopus_not_loaded",
    "octopus_no_agile",
    "add_octopus",
    "nordpool_not_loaded",
    "entsoe_key_rejected",
    "add_nordpool",
    "add_entsoe_key",
    "source_missing",
)
NoticeCode = Literal[
    "pvpc_not_loaded",
    "add_pvpc",
    "octopus_region_mismatch",
    "octopus_not_loaded",
    "octopus_no_agile",
    "add_octopus",
    "nordpool_not_loaded",
    "entsoe_key_rejected",
    "add_nordpool",
    "add_entsoe_key",
    "source_missing",
]


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """What decides an area's source and notice, read at one moment."""

    nordpool_route: bool = False
    nordpool_entry: EntryState = "none"
    entsoe_route: bool = False
    entsoe_key: KeyState = "none"
    pvpc_route: bool = False
    pvpc_entry: EntryState = "none"
    octopus_route: bool = False
    octopus_entry: EntryState = "none"
    #: Whether an Agile meter's rates are read for the area (its own region's, or the only region's).
    octopus_meter: bool = False
    #: The meter's region when it is not the area's own.
    octopus_region: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderNotice:
    """One notice: a stable code and its parameters (plain strings)."""

    code: NoticeCode
    params: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """An area's price source now: the provider's key, name and address (`None` with no source at all), and the
    notice (`None` when there is nothing to do)."""

    key: str | None
    name: str | None
    url: str | None
    notice: ProviderNotice | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "url": self.url,
            "notice": None if self.notice is None else self.notice.as_dict(),
        }


NO_SOURCE: Final = SourceStatus(key=None, name=None, url=None, notice=ProviderNotice("source_missing"))


def source_notice(facts: SourceFacts) -> ProviderNotice | None:
    """The table of the module, row by row."""
    if facts.pvpc_route:
        if facts.pvpc_entry == "loaded":
            return None
        return ProviderNotice("pvpc_not_loaded" if facts.pvpc_entry == "not_loaded" else "add_pvpc")
    if facts.octopus_route:
        if facts.octopus_meter:
            if facts.octopus_region is None:
                return None
            return ProviderNotice("octopus_region_mismatch", MappingProxyType({"region": facts.octopus_region}))
        if facts.octopus_entry == "not_loaded":
            return ProviderNotice("octopus_not_loaded")
        if facts.octopus_entry == "loaded":
            return ProviderNotice("octopus_no_agile")
        return ProviderNotice("add_octopus")
    key_usable = facts.entsoe_route and facts.entsoe_key in ("ok", "unchecked")
    if facts.nordpool_route:
        if facts.nordpool_entry == "loaded" or key_usable:
            return None
        if facts.nordpool_entry == "not_loaded":
            return ProviderNotice("nordpool_not_loaded")
        if facts.entsoe_route and facts.entsoe_key == "rejected":
            return ProviderNotice("entsoe_key_rejected")
        return ProviderNotice("add_nordpool")
    if facts.entsoe_route:
        if key_usable:
            return None
        return ProviderNotice("entsoe_key_rejected" if facts.entsoe_key == "rejected" else "add_entsoe_key")
    return ProviderNotice("source_missing")


def entry_state(hass: HomeAssistant | None, domain: str) -> EntryState:
    """Whether an integration has a loaded config entry, one that is not loaded, or none."""
    if hass is None:
        return "none"
    entries = hass.config_entries.async_entries(domain)
    if not entries:
        return "none"
    return "loaded" if any(entry.state is ConfigEntryState.LOADED for entry in entries) else "not_loaded"
