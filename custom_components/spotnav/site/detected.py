"""What SpotNav detected: per kind of source, the entity chosen, why, and the near-misses with why they were not.

SpotNav finds most of what it reads by itself (fewest settings). When it finds the wrong thing, or nothing, a person
needs to see what it looked at; a silent miss cannot be explained (SEM's "Detected hardware" page and its issue #1137,
`plans/research_integrations/ha_ecosystem_gap_2026-11-14.md` idea 9). This module builds that list from what each
part already decided; it decides nothing itself and picks nothing anew where a part keeps its own choice.

The answer (`DETECTED_API_VERSION`) is a list of items in a fixed order (`KINDS`), each:

* `kind`: `chargers`, `meter`, `battery`, `solar_forecast`, `production`, `weather`, `outdoor_sensor`,
  `holiday_calendar`, `vehicles`;
* `status`: `found` (SpotNav picked it by itself), `configured` (set in the site's or a charger's settings),
  `ambiguous` (more than one fits and SpotNav will not guess) or `none`;
* `chosen`: the entity ids (or, for an integration, its entry's title) in use;
* `why`: a stable code for how it was chosen, `detail`: a few facts beside it;
* `near_misses`: `[{entity_id, name, reason}]`, each a stable reason code (see each builder).

Codes, not sentences: the card's settings can word them in every language later (no card UI in this release).
Pure: no Home Assistant import.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

DETECTED_API_VERSION: Final = 1

KINDS: Final[tuple[str, ...]] = (
    "chargers",
    "meter",
    "battery",
    "solar_forecast",
    "production",
    "weather",
    "outdoor_sensor",
    "holiday_calendar",
    "vehicles",
)

Status = Literal["found", "configured", "ambiguous", "none"]


@dataclass(frozen=True, slots=True)
class NearMiss:
    reason: str
    entity_id: str | None = None
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "name": self.name, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Detected:
    kind: str
    status: Status
    why: str
    chosen: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)
    near_misses: tuple[NearMiss, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "chosen": list(self.chosen),
            "why": self.why,
            "detail": dict(self.detail),
            "near_misses": [miss.as_dict() for miss in self.near_misses],
        }


def answer(items: Iterable[Detected]) -> dict[str, Any]:
    """The payload: every kind once, in `KINDS` order (a kind no builder gave reads `none`)."""
    by_kind = {item.kind: item for item in items}
    return {
        "api_version": DETECTED_API_VERSION,
        "items": [(by_kind.get(kind) or Detected(kind, "none", "not_looked_for")).as_dict() for kind in KINDS],
    }


# ---- chargers


@dataclass(frozen=True, slots=True)
class ChargerRow:
    """One SpotNav charger of the site: its entry's title, its integration and how it is driven."""

    title: str
    platform: str | None
    charge_control: str | None
    current_limit: str | None = None
    start_stop: str | None = None


@dataclass(frozen=True, slots=True)
class ChargerIntegration:
    """A set-up integration SpotNav has a profile for, with its profile's role (`charger_profiles.ROLE_*`) and
    whether a SpotNav charger already uses one of its entities."""

    domain: str
    title: str
    role: str
    used: bool


def chargers(rows: Sequence[ChargerRow], integrations: Sequence[ChargerIntegration]) -> Detected:
    """Near-miss reasons: `not_added_as_charger` (a charger integration no SpotNav charger uses: add it as a
    charger), `external_controller` (an integration that runs chargers itself, such as evcc), `measurement_only`,
    `unsupported` (a profile SpotNav knows but cannot drive)."""
    misses = []
    for item in integrations:
        if item.used or item.role == "excluded":
            continue
        reason = {"charger": "not_added_as_charger"}.get(item.role, item.role)
        misses.append(NearMiss(reason, None, item.title))
    if not rows:
        return Detected("chargers", "none", "no_charger_in_site", near_misses=tuple(misses))
    return Detected(
        "chargers",
        "configured",
        "site_member",
        chosen=tuple(row.charge_control or row.title for row in rows),
        detail={
            "chargers": [
                {
                    "title": row.title,
                    "platform": row.platform,
                    "charge_control": row.charge_control,
                    "current_limit": row.current_limit,
                    "start_stop": row.start_stop,
                }
                for row in rows
            ]
        },
        near_misses=tuple(misses),
    )


# ---- the grid meter


@dataclass(frozen=True, slots=True)
class MeterOffer:
    """A meter setup the site's detection offers (`site_detection.MeterCandidate`), reduced to what is shown."""

    title: str
    integration: str
    entity_ids: tuple[str, ...]
    disabled_entity_ids: tuple[str, ...] = ()


def meter(
    mode: str | None, entity_ids: Sequence[str], grid_power: str | None, phases: Mapping[str, Any],
    offers: Sequence[MeterOffer],
) -> Detected:
    """The site's measurement as set (load balancing's entities and the total grid power), and the other meter
    setups its detection offers. Near-miss reasons: `other_meter` (another setup that would work),
    `entities_disabled` (it needs entities its integration ships disabled)."""
    used = set(entity_ids) | ({grid_power} if grid_power else set())
    misses = []
    for offer in offers:
        if set(offer.entity_ids) <= used:
            continue
        reason = "entities_disabled" if offer.disabled_entity_ids else "other_meter"
        misses.append(NearMiss(reason, offer.entity_ids[0] if offer.entity_ids else None, f"{offer.title} ({offer.integration})"))
    detail = {"mode": mode, "grid_power": grid_power, "phases": dict(phases)}
    if not entity_ids and not grid_power:
        return Detected("meter", "none", "not_configured", detail=detail, near_misses=tuple(misses))
    return Detected(
        "meter", "configured", f"measurement:{mode or 'unknown'}", chosen=tuple(sorted(used)), detail=detail,
        near_misses=tuple(misses),
    )


# ---- the home battery


@dataclass(frozen=True, slots=True)
class BatteryOffer:
    title: str
    integration: str
    entity_id: str


def battery(facts: Mapping[str, Any], coexist: Mapping[str, Any] | None, offers: Sequence[BatteryOffer]) -> Detected:
    """`facts` as `battery.facts.BatteryFacts.as_dict`; `coexist` the brand's row of `battery.coexist`. Why:
    `adapter:<brand>` (an adapter reads its facts and controls) or `device_class` (an unknown brand: its level from a
    battery sensor on its device). Near-miss reasons: `other_battery` (another battery the detection offers),
    `fact_not_found:<role>` (a fact the brand's adapter knows but did not find), `control_<why>:<control>` (a control
    not usable: not found, disabled, unavailable)."""
    entity_id = facts.get("battery_entity_id")
    misses = [
        NearMiss("other_battery", offer.entity_id, f"{offer.title} ({offer.integration})")
        for offer in offers
        if offer.entity_id != entity_id
    ]
    if not entity_id:
        return Detected("battery", "none", "not_configured", near_misses=tuple(misses))
    sources = facts.get("sources") or {}
    brand = facts.get("brand")
    if brand is not None:
        for role in ("soc", "capacity", "rated_charge", "rated_discharge"):
            if role not in sources:
                misses.append(NearMiss(f"fact_not_found:{role}"))
    for name, control in (facts.get("controls") or {}).items():
        if not isinstance(control, Mapping) or control.get("method") == "service":
            continue
        why = None
        if not control.get("found"):
            why = "control_not_found"
        elif not control.get("enabled"):
            why = "control_disabled"
        elif not control.get("available"):
            why = "control_unavailable"
        if why is not None:
            misses.append(NearMiss(f"{why}:{name}", control.get("entity_id")))
    management = facts.get("management") or {}
    detail = {
        "integration": facts.get("integration"),
        "brand": brand,
        "label": facts.get("label"),
        "management": management.get("state"),
        "management_reason": management.get("reason"),
        "sources": dict(sources),
    }
    if coexist is not None:
        detail["coexistence"] = dict(coexist)
    if facts.get("read_only") is not None:
        # Read only (`battery/mirrored.py`): mirrored from another Home Assistant, and how its brand was found.
        detail["read_only"] = facts.get("read_only")
    mirror = facts.get("mirror")
    if isinstance(mirror, Mapping):
        detail["mirror"] = dict(mirror)
    return Detected(
        "battery", "configured", f"adapter:{brand}" if brand else "device_class", chosen=(entity_id,), detail=detail,
        near_misses=tuple(misses),
    )


# ---- the sun


def solar_forecast(configured: Sequence[str], capable: Mapping[str, str], status: str | None) -> Detected:
    """`configured`: the titles of the forecast entries the site reads; `capable`: every set-up entry that offers a
    solar forecast to the Energy dashboard, `{title: domain}`. Near-miss reason: `not_chosen`."""
    misses = tuple(
        NearMiss("not_chosen", None, f"{title} ({domain})") for title, domain in sorted(capable.items())
        if title not in configured
    )
    if not configured:
        return Detected("solar_forecast", "none", "not_configured", near_misses=misses)
    return Detected(
        "solar_forecast", "configured", "site_setting", chosen=tuple(configured), detail={"status": status},
        near_misses=misses,
    )


def production(statistic_ids: Sequence[str], status: str | None) -> Detected:
    """The Energy dashboard's solar production statistics, which the whole house load is figured with."""
    if not statistic_ids:
        return Detected("production", "none", "energy_dashboard_has_no_solar", detail={"status": status})
    return Detected(
        "production", "found", "energy_dashboard", chosen=tuple(statistic_ids), detail={"status": status}
    )


# ---- the weather, the outdoor sensor and the holidays


def weather(entity_ids: Iterable[str], chosen: str | None, default: str, status: str | None) -> Detected:
    """`chosen` as `house_forecast.pick_weather_entity` picked it. Near-miss reasons: `not_the_default` (beside
    `default`), `several_no_default` (more than one and none is the default: none is picked)."""
    found = sorted(entity_id for entity_id in set(entity_ids) if entity_id.startswith("weather."))
    if chosen is not None:
        why = "default_weather" if chosen == default else "only_weather"
        misses = tuple(NearMiss("not_the_default", entity_id) for entity_id in found if entity_id != chosen)
        return Detected("weather", "found", why, chosen=(chosen,), detail={"status": status}, near_misses=misses)
    if found:
        return Detected(
            "weather", "ambiguous", "several_no_default", detail={"status": status},
            near_misses=tuple(NearMiss("several_no_default", entity_id) for entity_id in found),
        )
    return Detected("weather", "none", "no_weather_entity", detail={"status": status})


@dataclass(frozen=True, slots=True)
class OutdoorOffer:
    """A heat pump outdoor sensor of `house_forecast.OUTDOOR_SENSORS`, enabled or not."""

    entity_id: str
    platform: str
    disabled: bool = False


def outdoor_sensor(offers: Sequence[OutdoorOffer], chosen: str | None, source: str | None) -> Detected:
    """`chosen` as `house_forecast.pick_outdoor_sensor` picked it (`source`: what the temperature is read from now,
    `sensor` or `weather`). Near-miss reasons: `disabled` (the integration's sensor is disabled),
    `several_heat_pumps` (more than one: which is meant cannot be told)."""
    disabled = tuple(NearMiss("disabled", offer.entity_id, offer.platform) for offer in offers if offer.disabled)
    enabled = [offer for offer in offers if not offer.disabled]
    detail = {"source": source}
    if chosen is not None:
        return Detected("outdoor_sensor", "found", "heat_pump_outdoor_sensor", chosen=(chosen,), detail=detail,
                        near_misses=disabled)
    if len(enabled) > 1:
        return Detected(
            "outdoor_sensor", "ambiguous", "several_heat_pumps", detail=detail,
            near_misses=tuple(NearMiss("several_heat_pumps", offer.entity_id, offer.platform) for offer in enabled)
            + disabled,
        )
    why = "weather_entity_used" if source == "weather" else "no_heat_pump_sensor"
    return Detected("outdoor_sensor", "none", why, detail=detail, near_misses=disabled)


def holiday_calendar(candidates: Mapping[str, str | None], chosen: str | None, country: str | None) -> Detected:
    """`candidates`: the Holiday integration's calendars with their country; `chosen` as
    `house_forecast.pick_holiday_calendar` picked it. Near-miss reasons: `other_country`, `several_none_for_country`."""
    detail = {"country": country}
    if chosen is not None:
        why = "home_country" if country and candidates.get(chosen) == country else "only_holiday_calendar"
        misses = tuple(
            NearMiss("other_country", entity_id, own) for entity_id, own in sorted(candidates.items()) if entity_id != chosen
        )
        return Detected("holiday_calendar", "found", why, chosen=(chosen,), detail=detail, near_misses=misses)
    if candidates:
        return Detected(
            "holiday_calendar", "ambiguous", "several_none_for_country", detail=detail,
            near_misses=tuple(
                NearMiss("several_none_for_country", entity_id, own) for entity_id, own in sorted(candidates.items())
            ),
        )
    return Detected("holiday_calendar", "none", "weekday_weekend_only", detail=detail)


# ---- the cars


@dataclass(frozen=True, slots=True)
class VehicleRow:
    name: str
    soc_entity_id: str | None


def vehicles(found: Sequence[VehicleRow], ambiguous: Sequence[tuple[str, Sequence[str]]]) -> Detected:
    """The car-like devices discovery found (with the level it reads) and those with several level readings it will
    not choose between (`ambiguous`: name, candidates). Near-miss reason: `several_soc_readings` (a Repairs issue
    asks the person)."""
    misses = tuple(
        NearMiss("several_soc_readings", entity_id, name) for name, candidates in ambiguous for entity_id in candidates
    )
    if not found:
        status: Status = "ambiguous" if ambiguous else "none"
        return Detected("vehicles", status, "no_vehicle_found", near_misses=misses)
    return Detected(
        "vehicles",
        "found",
        "vehicle_discovery",
        chosen=tuple(row.soc_entity_id or row.name for row in found),
        detail={"vehicles": [{"name": row.name, "soc_entity_id": row.soc_entity_id} for row in found]},
        near_misses=misses,
    )
