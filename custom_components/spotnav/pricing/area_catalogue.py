"""The area catalogue bundled with the integration (`pricing/areas.json`), and each area's provider routes.

The file is generated, never edited by hand: `tools/sync_areas.py` writes it from the relay's own area table
(`spotnav-relay/cmd/spotnav-areas`). It is metadata only (zones, currencies, fiscal suggestions and the codes an
area is asked for by), never a price. Its shape is the relay's contract-v2 area list, read by the same
`parse_catalogue(version=2)`, plus one object per area that parser ignores:

    "routes": {"nordpool": "GER",
               "entsoe": {"eic": "10Y1001A1001A82H", "sequence": "1", "expected_sequences": ["1", "2"],
                          "half_hourly": false},
               "cet_market_tz": "Europe/Berlin"}

* `nordpool`: Nord Pool's delivery area code; absent for an area Nord Pool does not trade.
* `entsoe`: the ENTSO-E area code and the sequence policy its documents are read by.
* `cet_market_tz`: the market calendar of a provider that answers by the CET delivery day (Nord Pool, and
  ENTSO-E asked the same way). `market_tz` stays the relay's calendar.

The `source` every entry states is a placeholder the v2 contract requires; where prices come from is the
provider's to say. The repository answers this catalogue while it holds none from the relay, so the area
picker has its list without the network. Nothing reads the routes yet.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from homeassistant.core import HomeAssistant

from .. import time_zones
from .relay_contract import AreaCatalogue, parse_catalogue, RelayParseError

_LOGGER = logging.getLogger(__name__)

#: The generated file, next to this module.
BUNDLED_PATH: Final = Path(__file__).with_name("areas.json")

#: The contract version the file is written in.
BUNDLED_VERSION: Final = 2


@dataclass(frozen=True, slots=True)
class EntsoeRoute:
    """An area's address on the ENTSO-E platform and the sequence policy its documents are read by.

    `sequence` names the classification sequence published for a zone that carries several (DE-LU, AT);
    `None` means one answer is expected. `expected_sequences` is every label such a zone's documents may
    carry. `half_hourly` admits thirty-minute periods (Ireland's SEM only).
    """

    eic: str
    sequence: str | None
    expected_sequences: tuple[str, ...]
    half_hourly: bool


@dataclass(frozen=True, slots=True)
class AreaRoutes:
    """How one area is asked for at each provider."""

    nordpool: str | None
    entsoe: EntsoeRoute | None
    cet_market_tz: str


@dataclass(frozen=True, slots=True)
class BundledCatalogue:
    """The bundled area list and its routes, keyed by area id."""

    catalogue: AreaCatalogue
    routes: Mapping[str, AreaRoutes]


def _fail(message: str) -> None:
    raise RelayParseError("invalid_field", f"the bundled area catalogue: {message}")


def _code(raw: Any, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        _fail(f"{what} must be a non-empty string")
    return raw


def _entsoe_route(raw: Any, what: str) -> EntsoeRoute:
    if not isinstance(raw, dict):
        _fail(f"{what} must be an object")
    sequence = raw.get("sequence")
    if sequence is not None:
        sequence = _code(sequence, f"{what}.sequence")
    expected = raw.get("expected_sequences", [])
    if not isinstance(expected, list):
        _fail(f"{what}.expected_sequences must be a list")
    expected_codes = tuple(_code(item, f"{what}.expected_sequences") for item in expected)
    if sequence is not None and sequence not in expected_codes:
        _fail(f"{what}: the sequence is not among the expected ones")
    half_hourly = raw.get("half_hourly", False)
    if not isinstance(half_hourly, bool):
        _fail(f"{what}.half_hourly must be true or false")
    return EntsoeRoute(
        eic=_code(raw.get("eic"), f"{what}.eic"),
        sequence=sequence,
        expected_sequences=expected_codes,
        half_hourly=half_hourly,
    )


def _routes(raw: Any, area_id: str) -> AreaRoutes:
    what = f"area {area_id!r} routes"
    if not isinstance(raw, dict):
        _fail(f"{what} must be an object")
    nordpool = raw.get("nordpool")
    if nordpool is not None:
        nordpool = _code(nordpool, f"{what}.nordpool")
    entsoe = None if raw.get("entsoe") is None else _entsoe_route(raw["entsoe"], f"{what}.entsoe")
    cet_market_tz = _code(raw.get("cet_market_tz"), f"{what}.cet_market_tz")
    if time_zones.zone(cet_market_tz) is None:
        _fail(f"{what}: {cet_market_tz!r} is not a known timezone")
    return AreaRoutes(nordpool=nordpool, entsoe=entsoe, cet_market_tz=cet_market_tz)


def parse_bundled(document: Any) -> BundledCatalogue:
    """The bundled file as a v2 catalogue plus its routes; any fault refuses the whole file.

    Stricter than a relay list, which skips a damaged entry: this file ships with the integration, so a
    fault is a packaging error (`tests/test_area_catalogue.py` catches it), and the caller then works as if
    there were no bundled catalogue at all.
    """
    catalogue = parse_catalogue(document, version=BUNDLED_VERSION)
    raw_areas = document["areas"]
    if len(catalogue.areas) != len(raw_areas):
        _fail("an entry was refused")
    routes: dict[str, AreaRoutes] = {}
    for raw in raw_areas:
        area_id = raw["id"]
        routes[area_id] = _routes(raw.get("routes"), area_id)
    return BundledCatalogue(catalogue=catalogue, routes=MappingProxyType(routes))


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


async def async_load_bundled(hass: HomeAssistant, path: Path = BUNDLED_PATH) -> BundledCatalogue | None:
    """Read and parse the bundled file off the loop; `None` (logged) when it is missing or refused."""
    try:
        document = await hass.async_add_executor_job(_read, path)
    except (OSError, ValueError) as err:
        _LOGGER.warning("The bundled area catalogue could not be read: %s", type(err).__name__)
        return None
    # Every zone it names, resolved off the loop before the parser reads them.
    await time_zones.async_resolve_document(document)
    if isinstance(document, dict) and isinstance(document.get("areas"), list):
        await time_zones.async_resolve(
            area["routes"]["cet_market_tz"]
            for area in document["areas"]
            if isinstance(area, dict)
            and isinstance(area.get("routes"), dict)
            and isinstance(area["routes"].get("cet_market_tz"), str)
        )
    try:
        return parse_bundled(document)
    except (RelayParseError, KeyError, TypeError) as err:
        _LOGGER.warning("The bundled area catalogue was refused: %s", err)
        return None
