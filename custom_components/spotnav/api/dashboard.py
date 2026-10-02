"""The read-only WebSocket contract for the bundled SpotNav card and the app (API v1).

* `spotnav/list_chargers`: which chargers exist and what each can do;
* `spotnav/get_dashboard`: one charger's whole coherent view (settings, planning state, market,
  prices, plan, live facts, control, strategy, site, vehicles, composed status). The webhook's
  `dashboard` action answers the same payload for its own charger.

Rules:

* The backend stays the only authority: handlers capture what the repository, manager and
  controllers already hold; they fetch, plan and infer nothing.
* One response is one observation: everything is captured first from immutable values and the
  serializers only format it, so a racing settings write or price refresh cannot mix two states.
* No secrets: no webhook ids/URLs, owner ids, pairing URIs, entity ids, tokens or exception text.
* Nothing here acts: no service call, charger command, schedule installation or settings write.
* Serialization is pure, so tests pin the contract without opening a socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Final, Iterable, Sequence

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CURRENT_LIMIT,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENTRY_TYPE,
    CONF_MAIN_FUSE_A,
    CONF_MEASUREMENT_MODE,
    MEASUREMENT_MODE_DIRECT,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_SOLAR_PRIORITY,
    DEFAULT_SOLAR_PRIORITY,
    DOMAIN,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DERIVED,
)
from ..entity import fiscal_view
from ..execution import auto_execution as execution
from ..execution.auto_execution import (
    AutoExecutor,
    ControlFacts,
    EXECUTION_NOT_APPLIED,
    pause_blocks_execution,
)
from ..execution.charge_progress import ChargeProgress, NOT_OBSERVED
from ..execution.controller import (
    ChargingController,
    CURRENT_RANGE_DEFAULT_MAX_A,
    current_range_dict,
    CURRENT_RANGE_SOURCE_DEFAULT,
)
from ..planning.auto_controller import AutoSnapshot, fiscal_choice_for
from ..planning.auto_settings import (
    AutoSettings,
    AutoSettingsStore,
    DEFAULT_CONSUMPTION_KWH_PER_10KM,
    FiscalOverride,
    PauseIntent,
    STRATEGY_CHEAPEST,
)
from ..planning.hybrid_forecast import async_forecast_capable_domains
from ..planning.planner import chart_intervals, ChartInterval
from ..planning.status_compose import (
    compose_status,
    HybridFacts,
    LoadBalancingFacts,
    PlanningFacts,
    ProposalFacts,
    SocFacts,
    SolarFacts,
    StatusFacts,
    TargetFacts,
)
from ..planning.strategy_options import strategy_options_for
from ..pricing.price_refresh import AreaPriceSnapshot, PriceRefreshManager
from ..pricing.price_repository import CatalogueSnapshot
from ..runtime import (
    charger_data,
    controller_for,
    domain_data,
    executor_for,
    preview_for,
    site_controller_for,
)
from ..site.phase_detection import (
    async_detect_phases,
    PhaseDetectionResult,
    UNKNOWN as UNKNOWN_PHASES,
)
from ..site.site_capacity_controller import SiteCapacityController
from ..util import aware_iso, finite_number
from ..vehicles import vehicle_properties
from ..vehicles.charger_inventory import charger_entries
from ..vehicles.soc_estimate import CHARGE_EFFICIENCY, target_need_kwh
from ..vehicles.vehicle_discovery import discover_vehicles, resolve_target_vehicle
from .common import (
    ERROR_CHARGER_REQUIRED,
    ERROR_CHARGER_UNLOADED,
    ERROR_SITE_NOT_CHARGER,
    ERROR_UNKNOWN_CHARGER,
    ERROR_UNSUPPORTED_VERSION,
    is_admin,
    lookup_charger,
    unsupported_version_text,
)
from .settings import encode_pause, encode_settings, strategy_of


DASHBOARD_API_VERSION: Final = 1

#: The control vocabulary and decision rules live beside the execution boundary that enforces
#: them (`auto_execution.decide_axes`) and are re-exported for clients.
ACTION_START: Final = execution.ACTION_START
ACTION_STOP: Final = execution.ACTION_STOP
ACTION_RESUME: Final = execution.ACTION_RESUME
ACTION_NONE: Final = execution.ACTION_NONE
#: The automatic axis's action name: a `pause` in the contract, a `stop` with a typed choice on the wire.
ACTION_PAUSE: Final = execution.ACTION_PAUSE
CONTROL_NO_SETTINGS: Final = execution.CONTROL_NO_SETTINGS
CONTROL_PAUSE_UNSETTLED: Final = execution.CONTROL_PAUSE_UNSETTLED

STRATEGY_SOLAR: Final = "solar"
STRATEGY_HYBRID: Final = "hybrid"
STRATEGY_SOLAR_REASON: Final = "needs_solar_surplus_measurement"
STRATEGY_HYBRID_REASON: Final = "needs_solar_and_price_control"


#: Documented ceiling for one `get_dashboard` response (bytes of JSON), measured by the tests:
#: two days of 15-minute rows is the largest honest answer.
MAX_RESPONSE_BYTES: Final = 256 * 1024

#: Capability facts every charger advertises, all of them, in this order. The first six are about
#: this charger's configuration and adapter (`execution/chargers/`); the last three are the
#: server's abilities and always `true`. `current_limit`: the charger has a dynamic current limit
#: (a configured number, or a service path); `set_current`: SpotNav may write a current to it (the
#: person opted in and its adapter can); `regulated_current`: load balancing may write it during a
#: session (false for a flash-stored setting, which is written at a session start only).
CAPABILITY_KEYS: Final = (
    "auto_price",
    "current_limit",
    "set_current",
    "regulated_current",
    "target_soc",
    "load_balancing",
    "refresh_vehicle",
    "set_charge_limit",
    "target_stop",
)


def _count(value: Any) -> int | None:
    """A whole number, or `None`, for a value a caller declared may be absent."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()



@dataclass(frozen=True, slots=True)
class DashboardFailure:

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class CapturedCharger:

    charger_id: str
    charger_name: str
    available: bool
    capabilities: tuple[tuple[str, bool], ...]

    def capability_map(self) -> dict[str, bool]:
        return dict(self.capabilities)


@dataclass(frozen=True, slots=True)
class CapturedLive:
    """The charger's own live facts, read once.

    `measured_current_a` is `None`: there is no charger-level measurement, and a setpoint is not one.
    """

    charging: bool
    schedule_active: bool
    requested_current_a: int | None
    setpoint_current_a: int | None
    measured_current_a: float | None
    #: The charger's own scheduler or load balancer holds the charge (Easee's waiting statuses).
    held_by_charger: bool = False
    #: The charger's own enable switch is off, so it cannot start.
    charger_disabled: bool = False
    #: The next window's start while a charge is held back for it, and whether a person overrode it.
    hold_until: datetime | None = None
    hold_overridden: bool = False


@dataclass(frozen=True, slots=True)
class CapturedExecution:

    state: str
    error: str | None
    applied_identity: str | None
    pending_identity: str | None
    pending_attempt: int | None
    #: Whether a manual pause still suspends execution at this instant (a lapsed bounded pause
    #: reads `False` before the boundary settles the record); `None` with no boundary and no settings.
    paused: bool | None
    #: A manual Start accepted but not yet reported charging by the charger; not a claim it is charging.
    start_pending: bool = False


@dataclass(frozen=True, slots=True)
class CapturedTarget:
    """The target-stop facts of one observation: the current stop record, or why no target can be
    checked right now.
    """

    stop_soc_percent: float | None = None
    stop_basis: str | None = None
    stop_reading_age_s: float | None = None
    unverifiable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CapturedSite:
    """One charger's compact load-balancing summary."""

    site_name: str
    state: str
    reason: str
    measured_margin_a: tuple[tuple[str, float | None], ...]
    proposed_current_a: float | None
    limiting_phase: str | None
    active_control_enabled: bool
    #: The site's measurement mode (`const.MEASUREMENT_MODE_DIRECT`/`_DERIVED` or `"unavailable"`);
    #: the strategy block reads it to decide whether `solar` and `hybrid` are available.
    measurement_mode: str
    #: Count of `CONF_CHARGER_ENTRY_IDS`, for "applies to all N chargers on this site" wording.
    charger_count: int
    #: Solar priority (`const.CONF_SOLAR_PRIORITY`) and hybrid's forecast sources
    #: (`const.CONF_SOLAR_FORECAST_ENTRIES`) from the site entry's data.
    solar_priority: str
    solar_forecast_selected: tuple[str, ...]
    #: Every config entry `update_site_settings` would accept for `solar_forecast`, as
    #: `(entry_id, title)`, plus any already-selected id that no longer qualifies.
    solar_forecast_choices: tuple[tuple[str, str], ...]
    #: Active load balancing's availability from `SiteCapacityController.capability_snapshot`;
    #: `active_control_enabled` above is the opt-in, this is whether it could do anything.
    active_control_available: bool
    #: The stable reason `active_control_available` is `False`, from the snapshot's `blocking_reasons`.
    active_control_reason: str | None
    #: This charger's row of `SiteCapacityController.solar_surplus_snapshot` / `.hybrid_snapshot`.
    solar_state: dict[str, Any] | None
    hybrid_state: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class CapturedSoc:
    """The state of charge a target is planned against, as the `soc` block states it.

    Every field may be `None`: a source that reports nothing right now (a sleeping car) is still a source.
    """

    value: float | None
    age_s: float | None
    source: str | None
    estimated: bool
    target_percent: float | None
    need_kwh: float | None
    capacity_kwh: float | None
    vehicle_name: str | None
    vehicle_id: str | None = None
    vehicle_choices: tuple[tuple[str, str], ...] = ()
    vehicle_max_percent: float | None = None


@dataclass(frozen=True, slots=True)
class CapturedVehicle:
    """One candidate vehicle as the root `vehicles` list states it.

    `capacity_kwh` is the resolved figure and `capacity_source` says which; `consumption_kwh_per_10km`
    is the vehicle's own, else the planner's default.
    """

    id: str
    name: str
    soc_entity_id: str | None
    capacity_kwh: float | None
    capacity_source: str | None
    consumption_kwh_per_10km: float | None
    max_percent: float | None
    soc_percent: float | None = None


@dataclass(frozen=True, slots=True)
class CapturedSummary:
    """The names a person would recognise the setup by, read once: friendly names, never entity ids.

    `current_path` is `change_configuration`, `number`, `easee_dynamic_limit` or `none` (also when the
    person has not opted in to SpotNav setting the current). `site` is `None` without a site.
    """

    start_stop_name: str | None
    current_path: str
    current_entity_name: str | None
    energy_name: str | None
    energy_automatic: bool
    site: tuple[float | None, str, str | None] | None
    vehicle_sensor_names: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class CapturedDashboard:
    """One coherent observation of one charger, captured before anything is serialized: every field is
    a value read once, so the pure serializers cannot mix two observations.
    """

    generated_at: datetime
    charger: CapturedCharger
    settings: AutoSettings | None
    snapshot: AutoSnapshot | None
    catalogue: CatalogueSnapshot | None
    area_entry: Any
    area: AreaPriceSnapshot | None
    days: tuple[Any, ...]
    plan: Any
    live: CapturedLive
    execution: CapturedExecution
    #: Pause choices this charger can honour now, in product order; empty with no boundary.
    pause_choices: tuple[str, ...]
    intervals: tuple[ChartInterval, ...] | None
    site: CapturedSite | None
    #: The vehicle-side observation (`execution/charge_progress.py`); defaults to `NOT_OBSERVED`.
    charge_progress: ChargeProgress = NOT_OBSERVED
    #: The current this charger can be asked for, `{min_a, max_a, source}` (`current_range`).
    current_range: dict[str, Any] = field(
        default_factory=lambda: current_range_dict(
            CURRENT_RANGE_DEFAULT_MAX_A, CURRENT_RANGE_SOURCE_DEFAULT
        )
    )
    soc: CapturedSoc | None = None
    vehicles: tuple[CapturedVehicle, ...] = ()
    target_vehicle_id: str | None = None
    target: CapturedTarget | None = None
    phase_result: PhaseDetectionResult = UNKNOWN_PHASES
    #: Every charger config entry `(id, name)` in creation order, so a paired app learns about
    #: chargers added, renamed or removed. No webhook id, no URL.
    chargers: tuple[tuple[str, str], ...] = ()
    strategy_options: tuple[str, ...] = ()
    #: Settings first-run defaults filled in that no edit has confirmed (`AutoSettingsStore.suggested`).
    suggested: tuple[str, ...] = ()
    summary: CapturedSummary | None = None


def capture_target(controller: ChargingController | None) -> CapturedTarget | None:
    if controller is None:
        return None
    record = controller.target_stop_record
    if record is not None:
        return CapturedTarget(
            stop_soc_percent=finite_number(record.get("soc_percent")),
            stop_basis="estimate" if record.get("basis") == "estimate" else "reading",
            stop_reading_age_s=finite_number(record.get("reading_age_s")),
        )
    plan = controller.plan
    if plan is None or plan.target_soc_percent is None:
        return CapturedTarget()
    reading = controller.target_reading()
    if reading is None:
        return CapturedTarget(unverifiable_reason="no_source")
    if reading.soc_percent is None:
        return CapturedTarget(unverifiable_reason="reading_unusable")
    return CapturedTarget()



_CHARGER_FAILURE_TEXT: Final = {
    ERROR_UNKNOWN_CHARGER: "No such charger",
    ERROR_SITE_NOT_CHARGER: "That id is a site, not a charger",
    ERROR_CHARGER_UNLOADED: "That charger is not loaded",
}


def resolve_charger_request(hass: HomeAssistant, payload: dict[str, Any]) -> ConfigEntry | DashboardFailure:
    """The charger a request names, or a stable refusal.

    Four answers: a missing id is a bad request, a *site* id is the wrong kind of entry, an unknown id
    does not disclose whether another integration owns it, and a known charger that is not loaded says so.
    """
    charger_id = payload.get("charger_id")
    if not isinstance(charger_id, str) or not charger_id:
        return DashboardFailure(ERROR_CHARGER_REQUIRED, "A charger_id is required")
    entry, code = lookup_charger(hass, charger_id)
    if entry is None:
        return DashboardFailure(code, _CHARGER_FAILURE_TEXT[code])
    return entry


def _site_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return sorted(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE
            and entry.state is ConfigEntryState.LOADED
        ),
        key=lambda entry: (entry.title.casefold(), entry.entry_id),
    )


def site_binding(hass: HomeAssistant, charger_entry_id: str) -> ConfigEntry | None:
    """The loaded site entry that lists this charger, if any (configured membership, not health)."""
    for entry in _site_entries(hass):
        members = entry.data.get(CONF_CHARGER_ENTRY_IDS) or []
        if charger_entry_id in members:
            return entry
    return None


def capabilities_for(
    hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController | None
) -> tuple[tuple[str, bool], ...]:
    """What this charger can do right now, from what is loaded and configured (facts, not name guesses).

    `auto_price`: the Auto preview exists; `current_limit`: a current-limit entity is configured, or
    the adapter sets a current some other way; `set_current` and `regulated_current`: what the
    charger's adapter supports now (see `CAPABILITY_KEYS`); `target_soc`: `target_soc_capable`;
    `load_balancing`: a loaded site lists it; the last three are the server's abilities.
    """
    adapter_capabilities = None if controller is None else controller.adapter.capabilities
    set_current = bool(adapter_capabilities is not None and adapter_capabilities.set_current)
    return (
        ("auto_price", preview_for(hass, entry.entry_id) is not None),
        ("current_limit", bool(controller is not None and controller.current_limit) or set_current),
        ("set_current", set_current),
        (
            "regulated_current",
            bool(adapter_capabilities is not None and adapter_capabilities.regulated_current),
        ),
        ("target_soc", target_soc_capable(hass, entry.entry_id)),
        ("load_balancing", site_binding(hass, entry.entry_id) is not None),
        ("refresh_vehicle", True),
        ("set_charge_limit", True),
        ("target_stop", True),
    )


def target_soc_capable(hass: HomeAssistant, entry_id: str) -> bool:
    """Whether a target state of charge can be planned and enforced for this charger now.

    The same reader the planner uses: a state-of-charge source resolves (value not required) and a
    battery size is known. With no vehicle selected, any discovered vehicle that reports its pack size
    qualifies, so the mode can be offered in order to select one.
    """
    data = charger_data(hass, entry_id)
    reader = None if data is None else data.soc_reader
    if reader is None:
        return False
    store = domain_data(hass).auto_store
    stored = None if store is None else store.settings(entry_id).target.vehicle_id
    vehicle_id, _ = resolve_target_vehicle(hass, stored)
    if vehicle_id is None:
        if reader.has_source(None) and reader.capacity_kwh(None) is not None:
            return True
        return any(
            candidate.battery_capacity_kwh
            or vehicle_properties.stored_properties(hass, candidate.id).capacity_kwh
            for candidate in discover_vehicles(hass)
        )
    return reader.has_source(vehicle_id) and reader.capacity_kwh(vehicle_id) is not None


def capture_soc(
    hass: HomeAssistant, entry_id: str, settings: AutoSettings | None
) -> CapturedSoc | None:
    """The `soc` block, read once; `None` when no source resolves for this charger."""
    data = charger_data(hass, entry_id)
    reader = None if data is None else data.soc_reader
    if reader is None or settings is None:
        return None
    vehicle_id, candidates = resolve_target_vehicle(hass, settings.target.vehicle_id)
    choose = vehicle_id is None and len(candidates) > 1
    if not reader.has_source(vehicle_id) and not choose:
        return None
    max_percent = reader.vehicle_max_percent(vehicle_id)
    reading = reader.read(vehicle_id)
    capacity = reader.capacity_kwh(vehicle_id)
    target = settings.target.target_percent
    value = None if reading is None else reading.soc_percent
    need = None
    if value is not None and target is not None:
        _, need = target_need_kwh(
            soc_percent=value,
            capacity_kwh=capacity,
            target_percent=target,
            vehicle_max_percent=max_percent,
        )
    return CapturedSoc(
        value=value,
        age_s=None if reading is None else reading.age_s,
        source=None if reading is None else reading.source,
        estimated=bool(reading is not None and reading.estimated),
        target_percent=target,
        need_kwh=need,
        capacity_kwh=capacity,
        vehicle_name=reader.vehicle_name(vehicle_id),
        vehicle_id=vehicle_id,
        vehicle_choices=tuple((c.id, c.name) for c in candidates) if len(candidates) > 1 else (),
        vehicle_max_percent=max_percent,
    )


def capture_vehicles(
    hass: HomeAssistant, entry_id: str, settings: AutoSettings | None
) -> tuple[tuple[CapturedVehicle, ...], str | None]:
    """The root `vehicles` and `target_vehicle_id` for one charger, read once.

    Every vehicle the planner could choose. Capacity follows `SocReader.capacity_with_source`;
    consumption is the vehicle's own else the planner's default.
    """
    data = charger_data(hass, entry_id)
    reader = None if data is None else data.soc_reader
    stored = None if settings is None else settings.target.vehicle_id
    target_id, candidates = resolve_target_vehicle(hass, stored)
    rows: list[CapturedVehicle] = []
    for choice in candidates:
        own = vehicle_properties.stored_properties(hass, choice.id)
        if reader is not None:
            capacity, source = reader.capacity_with_source(choice.id)
            max_percent = reader.vehicle_max_percent(choice.id)
            reading = reader.read(choice.id)
            soc_percent = None if reading is None else reading.soc_percent
        else:
            capacity, source, max_percent = own.capacity_kwh, ("stored" if own.capacity_kwh else None), None
            soc_percent = None
        rows.append(
            CapturedVehicle(
                id=choice.id,
                name=choice.name,
                soc_entity_id=choice.selected_entity_id,
                capacity_kwh=capacity,
                capacity_source=source,
                consumption_kwh_per_10km=(
                    own.consumption_kwh_per_10km
                    if own.consumption_kwh_per_10km is not None
                    else DEFAULT_CONSUMPTION_KWH_PER_10KM
                ),
                max_percent=max_percent,
                soc_percent=soc_percent,
            )
        )
    return tuple(rows), target_id


#: How the adapter's current path kinds read in the summary.
_CURRENT_PATH_CODES: Final = {
    "ocpp": "change_configuration",
    "number": "number",
    "service": "easee_dynamic_limit",
}


def _entity_name(hass: HomeAssistant, entity_id: Any) -> str | None:
    """An entity's friendly name (its state's name, else the registry's), or `None`; never its id."""
    if not isinstance(entity_id, str) or not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is not None:
        return state.name
    registered = er.async_get(hass).async_get(entity_id)
    if registered is None:
        return None
    return registered.name or registered.original_name or None


def capture_summary(
    hass: HomeAssistant, entry: ConfigEntry, vehicles: Iterable[CapturedVehicle]
) -> CapturedSummary:
    """The setup in words (see `CapturedSummary`), from the same facts the card's Charger and Site
    summaries read: the adapter's start/stop and current paths, the stored energy register, the
    site's fuse, stored measurement mode and battery sensor.
    """
    controller = controller_for(hass, entry.entry_id)
    start_stop_id: Any = None
    current_path = "none"
    current_entity_id: Any = None
    if controller is not None:
        adapter = controller.adapter
        description = adapter.describe()
        entity_ids = description["start_stop"].get("entity_ids") or []
        start_stop_id = entity_ids[0] if entity_ids else None
        if adapter.current_enabled:
            current_path = _CURRENT_PATH_CODES.get(description["current"]["kind"], "none")
        current_entity_id = description["current"].get("entity_id")
    else:
        current_entity_id = entry.data.get(CONF_CURRENT_LIMIT)
    if not start_stop_id:
        start_stop_id = entry.data.get(CONF_CHARGE_CONTROL)
    energy_id = entry.data.get(CONF_ENERGY_REGISTER_ENTITY) or None
    site_entry = site_binding(hass, entry.entry_id)
    site = None
    if site_entry is not None:
        fuse = finite_number(site_entry.data.get(CONF_MAIN_FUSE_A))
        site = (
            fuse,
            str(site_entry.data.get(CONF_MEASUREMENT_MODE, MEASUREMENT_MODE_DIRECT)),
            _entity_name(hass, site_entry.data.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY) or None),
        )
    return CapturedSummary(
        start_stop_name=_entity_name(hass, start_stop_id),
        current_path=current_path,
        current_entity_name=_entity_name(hass, current_entity_id),
        energy_name=_entity_name(hass, energy_id),
        energy_automatic=energy_id is None,
        site=site,
        vehicle_sensor_names=tuple(
            (vehicle.id, _entity_name(hass, vehicle.soc_entity_id)) for vehicle in vehicles
        ),
    )


def capture_charger(hass: HomeAssistant, entry: ConfigEntry) -> CapturedCharger:
    """One charger as the contract states it, captured now."""
    controller = controller_for(hass, entry.entry_id)
    return CapturedCharger(
        charger_id=entry.entry_id,
        charger_name=entry.title,
        available=controller is not None,
        capabilities=capabilities_for(hass, entry, controller),
    )


def capture_chargers(hass: HomeAssistant) -> tuple[CapturedCharger, ...]:
    """Every charger entry, never a site, ordered by case-folded name then entry id (a stable tie-break
    for equal names).
    """
    entries = sorted(
        charger_entries(hass),
        key=lambda entry: (entry.title.casefold(), entry.entry_id),
    )
    return tuple(capture_charger(hass, entry) for entry in entries)


def solar_forecast_choices(
    hass: HomeAssistant, domains: frozenset[str], selected_ids: Iterable[str]
) -> tuple[tuple[str, str], ...]:
    """Every config entry `update_site_settings` accepts for `solar_forecast`, as `(entry_id, title)`.

    The entries whose integration implements the Energy dashboard's solar-forecast platform
    (`domains`), plus any already-selected id that no longer qualifies so a stored choice stays
    selectable. The same listing the site options flow's picker offers, read independently.
    """
    options: list[tuple[str, str]] = [
        (entry.entry_id, entry.title or entry.entry_id)
        for entry in hass.config_entries.async_entries()
        if entry.domain in domains
    ]
    known = {entry_id for entry_id, _ in options}
    for entry_id in selected_ids:
        if entry_id in known:
            continue
        stored_entry = hass.config_entries.async_get_entry(entry_id)
        options.append((entry_id, stored_entry.title if stored_entry is not None else entry_id))
        known.add(entry_id)
    return tuple(options)


def _active_control_reason(controller: SiteCapacityController) -> str | None:
    """The stable reason active load balancing is unavailable, or `None` when it is available.

    Reuses `SiteCapacityController.capability_snapshot` and adds no opinion of its own: when neither
    blocking reason applies but it is still unavailable, the missing condition is "no
    site-controllable charger", reported by elimination.
    """
    load_balancing = controller.capability_snapshot.load_balancing
    if load_balancing.active_available:
        return None
    if load_balancing.blocking_reasons:
        return str(load_balancing.blocking_reasons[0])
    return "no_commandable_charger"


def capture_site(
    hass: HomeAssistant,
    charger_entry_id: str,
    *,
    forecast_domains: frozenset[str] = frozenset(),
) -> CapturedSite | None:
    """This charger's compact load-balancing summary, or `None` when it has no site.

    `forecast_domains` (`hybrid_forecast.async_forecast_capable_domains`) is an async read the caller
    performs first and passes in, since this capture is synchronous.
    """
    entry = site_binding(hass, charger_entry_id)
    if entry is None:
        return None
    controller = site_controller_for(hass, entry.entry_id)
    if controller is None:
        return None
    result = controller.result
    allocation = next(
        (
            candidate
            for candidate in result.allocations
            if candidate.charger_entry_id == charger_entry_id
        ),
        None,
    )
    members = entry.data.get(CONF_CHARGER_ENTRY_IDS) or []
    solar_priority = entry.data.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY)
    selected_forecast_ids = tuple(entry.data.get(CONF_SOLAR_FORECAST_ENTRIES, []) or ())
    solar_snapshot = controller.solar_surplus_snapshot.get(charger_entry_id)
    hybrid_snapshot = controller.hybrid_snapshot.get(charger_entry_id)
    return CapturedSite(
        site_name=entry.title,
        state=str(result.state),
        reason=str(result.reason),
        measured_margin_a=tuple(
            (str(phase), finite_number(margin))
            for phase, margin in result.measured_margin_a.items()
        ),
        proposed_current_a=None if allocation is None else finite_number(allocation.proposed_current_a),
        limiting_phase=None if allocation is None else _text(allocation.limiting_phase),
        # The running controller's opt-in, which every gate reads and `update_site_settings` changes.
        active_control_enabled=controller.active_control_enabled,
        measurement_mode=str(result.measurement_mode),
        charger_count=len(members),
        solar_priority=str(solar_priority),
        solar_forecast_selected=selected_forecast_ids,
        solar_forecast_choices=solar_forecast_choices(
            hass, forecast_domains, selected_forecast_ids
        ),
        active_control_available=controller.capability_snapshot.load_balancing.active_available,
        active_control_reason=_active_control_reason(controller),
        solar_state=dict(solar_snapshot) if solar_snapshot is not None else None,
        hybrid_state=dict(hybrid_snapshot) if hybrid_snapshot is not None else None,
    )


def _held_documents(
    repository: Any, area_id: str | None, tz: str | None, now: datetime
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """The day snapshots the repository holds for this area, today then tomorrow; a day with no
    document is absent, so a chart never invents a tomorrow nobody published.
    """
    if repository is None or area_id is None or tz is None:
        return ((), ())
    zone = dt_util.get_time_zone(tz)
    if zone is None:
        return ((), ())
    today = now.astimezone(zone).date()
    documents: list[Any] = []
    held: list[Any] = []
    for day in (today, today + timedelta(days=1)):
        snapshot = repository.day_snapshot(area_id, day)
        held.append(snapshot)
        if snapshot.document is not None:
            documents.append(snapshot.document)
    return (tuple(documents), tuple(held))


def capture_dashboard(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    forecast_domains: frozenset[str] = frozenset(),
) -> CapturedDashboard:
    """Read this charger's whole state once.

    Everything a response can carry is read here: the settings record, the preview snapshot, the
    catalogue snapshot, the held documents, the controller's and execution boundary's facts and the
    site summary. `forecast_domains` is passed straight to `capture_site`.
    """
    entry_id = entry.entry_id
    store: AutoSettingsStore | None = domain_data(hass).auto_store
    manager: PriceRefreshManager | None = domain_data(hass).price_refresh
    preview = preview_for(hass, entry_id)
    executor: AutoExecutor | None = executor_for(hass, entry_id)
    repository = domain_data(hass).price_repository
    controller = controller_for(hass, entry_id)

    now = dt_util.utcnow()
    settings = None if store is None else store.settings(entry_id)
    snapshot = None if preview is None else preview.snapshot()
    catalogue = None if repository is None else repository.catalogue_snapshot()
    area_id = None if settings is None else settings.area_id
    area_entry = None if catalogue is None or area_id is None else catalogue.area(area_id)

    fiscal = (
        None
        if settings is None or area_entry is None
        else fiscal_choice_for(settings, area_entry)
    )
    documents, days = _held_documents(
        repository, area_id, None if area_entry is None else area_entry.tz, now
    )
    intervals = (
        None
        if fiscal is None or area_entry is None or not documents
        else chart_intervals(
            documents, currency=area_entry.currency, fiscal=fiscal
        )
    )

    vehicles, target_vehicle_id = capture_vehicles(hass, entry_id, settings)
    return CapturedDashboard(
        generated_at=now,
        charger=capture_charger(hass, entry),
        settings=settings,
        snapshot=snapshot,
        catalogue=catalogue,
        area_entry=area_entry,
        area=None if manager is None or area_id is None else manager.area_snapshot(area_id),
        days=days,
        plan=None if controller is None else controller.plan,
        live=CapturedLive(
            charging=bool(controller is not None and controller.charging),
            schedule_active=bool(controller is not None and controller.plan is not None),
            requested_current_a=None if controller is None else controller.requested_current_a,
            setpoint_current_a=None if controller is None else controller.setpoint_current_a,
            measured_current_a=None,
            held_by_charger=bool(controller is not None and controller.held_by_charger),
            charger_disabled=bool(controller is not None and controller.charger_disabled),
            hold_until=None if controller is None else controller.hold_until,
            hold_overridden=bool(controller is not None and controller.hold_overridden),
        ),
        execution=CapturedExecution(
            state=EXECUTION_NOT_APPLIED if executor is None else executor.execution_state(),
            error=None if executor is None else executor.last_error,
            applied_identity=None if snapshot is None else snapshot.applied_identity,
            pending_identity=None if snapshot is None else snapshot.pending_identity,
            pending_attempt=None if executor is None else executor.pending_attempt,
            paused=(
                None
                if settings is None
                else (settings.execution_paused if executor is None else executor.paused)
            ),
            start_pending=bool(executor is not None and executor.manual_start_pending),
        ),
        intervals=intervals,
        pause_choices=() if executor is None else executor.pause_choices(),
        site=capture_site(hass, entry_id, forecast_domains=forecast_domains),
        charge_progress=NOT_OBSERVED if controller is None else controller.charge_progress,
        current_range=(
            current_range_dict(CURRENT_RANGE_DEFAULT_MAX_A, CURRENT_RANGE_SOURCE_DEFAULT)
            if controller is None
            else controller.current_range()
        ),
        soc=capture_soc(hass, entry_id, settings),
        vehicles=vehicles,
        target_vehicle_id=target_vehicle_id,
        target=capture_target(controller),
        phase_result=(
            UNKNOWN_PHASES
            if controller is None
            else async_detect_phases(hass, controller.charge_control)
        ),
        chargers=tuple((charger.entry_id, charger.title) for charger in charger_entries(hass)),
        strategy_options=tuple(strategy_options_for(hass, entry_id)),
        suggested=() if store is None else store.suggested(entry_id),
        summary=capture_summary(hass, entry, vehicles),
    )


def serialize_capabilities(charger: CapturedCharger) -> dict[str, bool]:
    known = charger.capability_map()
    return {key: bool(known.get(key, False)) for key in CAPABILITY_KEYS}


def serialize_charger(charger: CapturedCharger) -> dict[str, Any]:
    return {
        "charger_id": charger.charger_id,
        "charger_name": charger.charger_name,
        "available": charger.available,
        "capabilities": serialize_capabilities(charger),
    }


def serialize_charger_list(chargers: Sequence[CapturedCharger]) -> dict[str, Any]:
    """`spotnav/list_chargers`, as a value: a version and an ordered list."""
    return {
        "api_version": DASHBOARD_API_VERSION,
        "chargers": [serialize_charger(charger) for charger in chargers],
    }


def _fiscal_component(
    component: FiscalOverride, suggestion: float | None, unit: str | None
) -> dict[str, Any]:
    policy, effective, source = fiscal_view(component, suggestion)
    return {
        "policy": policy,
        "explicit_value": finite_number(component.value),
        "suggested_value": finite_number(suggestion),
        "effective_value": finite_number(effective),
        "value_source": source,
        "unit": unit,
    }


def serialize_fiscal(settings: AutoSettings, area_entry: Any | None) -> dict[str, Any] | None:
    """The three fiscal components for the selected market, or `None` without a market.

    The three states are preserved as stored: an explicit value is never invented, a literal zero is
    a value, and a market with no suggestion is its own state, not a zero.
    """
    if area_entry is None:
        return None
    overrides = settings.override_for(area_entry.id)
    minor = _text(area_entry.minor_unit)
    money_unit = None if minor is None else f"{minor}/kWh"
    return {
        "vat": _fiscal_component(overrides.vat, area_entry.vat_percent, "%"),
        "tax": _fiscal_component(overrides.tax, area_entry.suggested_tax, money_unit),
        "transfer": _fiscal_component(
            overrides.transfer, area_entry.suggested_grid_fee, money_unit
        ),
    }


def serialize_settings(capture: CapturedDashboard) -> dict[str, Any] | None:
    """The canonical settings record (`settings_contract.encode_settings`), or `None` when the
    installation has no record for the charger.

    No defaults are fabricated; what the market suggests and what is in effect is the `fiscal` block.
    """
    settings = capture.settings
    if settings is None:
        return None
    return encode_settings(settings)


def serialize_planning(capture: CapturedDashboard) -> dict[str, Any] | None:
    """What the calculation produced and what the charger is doing about it."""
    snapshot = capture.snapshot
    if snapshot is None:
        return None
    return {
        "state": snapshot.state,
        "reason": snapshot.reason,
        "settings_revision": snapshot.settings_revision,
        "generation": snapshot.generation,
        "calculated_at": aware_iso(snapshot.calculated_at),
        "historical": not snapshot.in_process,
        "missing": list(snapshot.missing),
        "price_state": _text(snapshot.price_state),
        "price_identity": _text(snapshot.price_identity),
        "today": _text(snapshot.today),
        "tomorrow": _text(snapshot.tomorrow),
        "execution_state": capture.execution.state,
        "execution_reason": _text(capture.execution.error),
        "applied": snapshot.applied,
        "applied_identity": _text(capture.execution.applied_identity),
        "pending_identity": _text(capture.execution.pending_identity),
        "pending_attempt": capture.execution.pending_attempt,
        "execution_paused": capture.execution.paused,
        # `waiting`, `buy_now` or `guarantee` while the plan needs prices not yet published
        # (`price_wait.decide`), else null.
        "price_wait": snapshot.price_wait,
        "publication_at": aware_iso(snapshot.publication_at),
        "must_buy_now_kwh": finite_number(snapshot.must_buy_kwh),
    }


def serialize_market(capture: CapturedDashboard) -> dict[str, Any] | None:
    """The held catalogue's state and the selected area's own facts, or `None` without one.

    Everything about the area is `None` when none is selected or it is not in the held catalogue;
    nothing is inferred from the area id.
    """
    catalogue = capture.catalogue
    if catalogue is None:
        return None
    entry = capture.area_entry
    return {
        "catalogue_state": catalogue.state,
        "catalogue_fetched_at": aware_iso(catalogue.fetched_at),
        "catalogue_attempt_error": _text(catalogue.attempt_error),
        "area_id": None if entry is None else entry.id,
        "area_name": None if entry is None else _text(entry.name),
        "countries": None if entry is None else list(entry.countries),
        "timezone": None if entry is None else _text(entry.tz),
        "currency": None if entry is None else _text(entry.currency),
        "major_unit": None if entry is None else _text(entry.major_unit),
        "minor_unit": None if entry is None else _text(entry.minor_unit),
        "suggested_vat_percent": None if entry is None else finite_number(entry.vat_percent),
        "suggested_tax": None if entry is None else finite_number(entry.suggested_tax),
        "suggested_grid_fee": None if entry is None else finite_number(entry.suggested_grid_fee),
    }


def _utc(moment: Any) -> datetime | None:
    """An aware instant in UTC, or `None` when there is nothing honest to compare."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        return None
    return moment.astimezone(dt_util.UTC)


def _spans(periods: Iterable[Any]) -> tuple[tuple[datetime, datetime], ...]:
    """Aware instants for a sequence of periods, in either shape the backend uses."""
    spans: list[tuple[datetime, datetime]] = []
    for period in periods:
        if hasattr(period, "start"):
            start, end = _utc(period.start), _utc(period.end)
        else:
            start, end = _utc(period[0]), _utc(period[1])
        if start is not None and end is not None:
            spans.append((start, end))
    return tuple(spans)


def _overlaps(
    start: datetime | None, end: datetime | None, spans: tuple[tuple[datetime, datetime], ...]
) -> bool:
    """Whether any of these periods overlaps this half-open interval."""
    if start is None or end is None:
        return False
    return any(start < span_end and span_start < end for span_start, span_end in spans)


def _proposal_spans(capture: CapturedDashboard) -> tuple[tuple[datetime, datetime], ...]:
    """The periods of a *real* proposal: a refusal has none, and shades nothing."""
    proposal = None if capture.snapshot is None else capture.snapshot.proposal
    if proposal is None or proposal.reason is not None or not proposal.periods:
        return ()
    return _spans(proposal.periods)


def _installed_spans(capture: CapturedDashboard) -> tuple[tuple[datetime, datetime], ...]:
    return () if capture.plan is None else _spans(capture.plan.windows)


def _row(
    *,
    start: datetime | None,
    end: datetime | None,
    day: Any,
    raw_price: float | None,
    effective_price: float | None,
    proposal_spans: tuple[tuple[datetime, datetime], ...],
    installed_spans: tuple[tuple[datetime, datetime], ...],
) -> dict[str, Any]:
    """One chart row with exactly these keys. `raw_price` is the relay's published figure; zero is a price."""
    start_utc, end_utc = _utc(start), _utc(end)
    duration = None if start_utc is None or end_utc is None else int((end_utc - start_utc).total_seconds() // 60)
    return {
        "start": aware_iso(start),
        "end": aware_iso(end),
        "day": _day(day),
        "duration_minutes": duration,
        "raw_price": finite_number(raw_price),
        "effective_price": finite_number(effective_price),
        "proposal_planned": _overlaps(start_utc, end_utc, proposal_spans),
        "installed_planned": _overlaps(start_utc, end_utc, installed_spans),
    }


def _horizon_row(
    row: Any,
    *,
    proposal_spans: tuple[tuple[datetime, datetime], ...],
    installed_spans: tuple[tuple[datetime, datetime], ...],
) -> dict[str, Any]:
    """One row from the proposal's own captured horizon (a `planner.HorizonInterval`)."""
    return _row(
        start=row.start,
        end=row.end,
        day=row.day,
        raw_price=row.published_major_per_kwh * 100,
        effective_price=row.effective_minor_per_kwh,
        proposal_spans=proposal_spans,
        installed_spans=installed_spans,
    )


def _published_row(
    row: ChartInterval,
    *,
    proposal_spans: tuple[tuple[datetime, datetime], ...],
    installed_spans: tuple[tuple[datetime, datetime], ...],
) -> dict[str, Any]:
    """One row from a held published interval, with no proposal fact to overlay."""
    return _row(
        start=row.start,
        end=row.end,
        day=row.day,
        raw_price=row.raw_minor_per_kwh,
        effective_price=row.effective_minor_per_kwh,
        proposal_spans=proposal_spans,
        installed_spans=installed_spans,
    )


def _quarters_of(row: ChartInterval) -> list[ChartInterval]:
    """One published interval as consecutive quarter-hours at its own price, by elapsed instant."""
    zone = row.start.tzinfo
    quarters: list[ChartInterval] = []
    utc_start = row.utc_start
    while utc_start < row.utc_end:
        utc_end = min(utc_start + timedelta(minutes=15), row.utc_end)
        quarters.append(
            replace(
                row,
                start=utc_start.astimezone(zone),
                end=utc_end.astimezone(zone),
                utc_start=utc_start,
                utc_end=utc_end,
            )
        )
        utc_start = utc_end
    return quarters


def serialize_intervals(capture: CapturedDashboard) -> list[dict[str, Any]]:
    """The rows a card draws: every held published interval of the days the chart shows.

    The repository's held documents are the base, so a proposal never truncates the chart. The
    proposal's captured horizon (`proposal.horizon`) is overlaid by instant, using its per-slot facts,
    and never removes a held row. `proposal_planned` and `installed_planned` say whether an interval
    belongs to the proposal or to the schedule the charger is running.

    Rows are keyed and ordered by UTC instant, never local wall time: a spring-forward night has no
    row for the skipped hour and an autumn night keeps both repeats distinct with their own offsets.
    """
    proposal = None if capture.snapshot is None else capture.snapshot.proposal
    if proposal is not None and (proposal.reason is not None or not proposal.periods):
        proposal = None
    proposal_spans = _proposal_spans(capture)
    installed_spans = _installed_spans(capture)

    horizon_by_instant: dict[datetime, Any] = {}
    if proposal is not None:
        for row in proposal.horizon:
            horizon_by_instant.setdefault(row.utc_start, row)

    merged: dict[datetime, dict[str, Any]] = {}
    for published in capture.intervals or ():
        horizon_row = horizon_by_instant.get(published.utc_start)
        if horizon_row is not None:
            merged[published.utc_start] = _horizon_row(
                horizon_row, proposal_spans=proposal_spans, installed_spans=installed_spans
            )
            continue
        if any(published.utc_start < instant < published.utc_end for instant in horizon_by_instant):
            # A coarser published interval only partly covered by the proposal's grid is drawn as
            # quarters, each keeping the published price where the proposal says nothing.
            for quarter in _quarters_of(published):
                if quarter.utc_start not in horizon_by_instant:
                    merged[quarter.utc_start] = _published_row(
                        quarter, proposal_spans=proposal_spans, installed_spans=installed_spans
                    )
            continue
        merged[published.utc_start] = _published_row(
            published, proposal_spans=proposal_spans, installed_spans=installed_spans
        )
    for instant, horizon_row in horizon_by_instant.items():
        if instant in merged:
            continue
        merged[instant] = _horizon_row(
            horizon_row, proposal_spans=proposal_spans, installed_spans=installed_spans
        )
    return [merged[instant] for instant in sorted(merged)]


def serialize_prices(capture: CapturedDashboard) -> dict[str, Any]:
    """The area's held price data and the chart rows built from it. Always an object.

    Without a market every leaf is `null` and `intervals` is empty, so the section never vanishes.
    """
    area = capture.area
    days = capture.days
    held = list(days) + [None, None]
    today, tomorrow = held[0], held[1]
    snapshot = capture.snapshot
    rows = serialize_intervals(capture)
    resolutions = _held_resolutions(capture)
    return {
        "area_id": None if area is None else area.area_id,
        "state": None if area is None else area.state,
        "reason": None if area is None else area.reason,
        "today": _day(None if today is None else today.day),
        "tomorrow": _day(None if tomorrow is None else tomorrow.day),
        "today_state": None if today is None else today.state,
        "tomorrow_state": None if tomorrow is None else tomorrow.state,
        "today_source": None if today is None else _text(today.source),
        "tomorrow_source": None if tomorrow is None else _text(tomorrow.source),
        "today_fetched_at": aware_iso(None if today is None else today.fetched_at),
        "tomorrow_fetched_at": aware_iso(None if tomorrow is None else tomorrow.fetched_at),
        "today_attempt_at": aware_iso(None if today is None else today.attempt_at),
        "tomorrow_attempt_at": aware_iso(None if tomorrow is None else tomorrow.attempt_at),
        "today_attempt_error": None if today is None else _text(today.attempt_error),
        "tomorrow_attempt_error": None if tomorrow is None else _text(tomorrow.attempt_error),
        "index_state": None if area is None else area.index_state,
        "index_revision": None if area is None else area.index_revision,
        "waiting_for_tomorrow": None if area is None else area.waiting_for_tomorrow,
        "resolution_minutes": _resolution_of(capture),
        "resolutions_minutes": resolutions,
        "priced_slots": None if snapshot is None else snapshot.priced_slots,
        "unpriced_slots": None if snapshot is None else snapshot.unpriced_slots,
        "unpriced": None if snapshot is None else snapshot.unpriced,
        "interval_count": len(rows),
        "intervals": rows,
    }


def _held_resolutions(capture: CapturedDashboard) -> list[int]:
    """The sorted set of resolutions the held documents were published in (15 or 60 minutes; two held
    days can disagree, so every row also carries its own duration).
    """
    return sorted(
        {
            day.document.resolution_minutes
            for day in capture.days
            if day.document is not None
        }
    )


def _resolution_of(capture: CapturedDashboard) -> int | None:
    """The one resolution both held days share, or `None` when they differ or none is held."""
    found = _held_resolutions(capture)
    return found[0] if len(found) == 1 else None


def serialize_plan(capture: CapturedDashboard) -> dict[str, Any]:
    """The proposal and the installed schedule, each from its own source, and how they relate.

    Always an object; `proposal` and `installed` are nullable leaves. They are separate because a
    newer proposal can wait beside an older installed plan, and borrowing one's cost or energy for the
    other would misdescribe the charger. `proposal` is only the preview's result (with its settings
    revision, and no amps or phases once the stored settings moved on); `installed` is only the
    controller's plan. `relation.applied` is `AutoExecutor.materially_applied`: `True` when the
    proposal's charger behaviour is already in force even under a different identity, `False` when
    materially different, `null` when there is no Auto snapshot. `applied_identity` names the Auto plan
    installed; `pending_identity` names `proposal.identity` only when that proposal is queued for a
    window boundary. Readers must not re-derive equivalence from identities, cost or text.
    `active_period_index` belongs to `installed` alone.
    """
    return {
        "proposal": _proposal_section(capture),
        "installed": _installed_section(capture),
        "relation": {
            "applied": None if capture.snapshot is None else capture.snapshot.applied,
            "applied_identity": _text(capture.execution.applied_identity),
            "pending_identity": _text(capture.execution.pending_identity),
        },
        # Null until something tracks delivered charge; a guess dressed as progress is worse.
        "delivered_kwh": None,
        "remaining_kwh": None,
    }


def _proposal_section(capture: CapturedDashboard) -> dict[str, Any] | None:
    """The preview's proposal, or `None`. A refusal is not a proposal: it lives in
    `planning.state`/`planning.reason`, so a card cannot draw an empty plan as a plan.
    """
    snapshot = capture.snapshot
    proposal = None if snapshot is None else snapshot.proposal
    if proposal is None or proposal.reason is not None or not proposal.periods:
        return None
    currency = None if capture.area_entry is None else _text(capture.area_entry.currency)
    cost = finite_number(proposal.estimated_cost)
    # Amps and phases are the settings the proposal was calculated with; absent once the stored
    # record has moved on.
    current = snapshot.settings_revision
    settings = capture.settings
    same_generation = settings is not None and settings.revision == current
    return {
        "identity": None if snapshot is None else _text(snapshot.proposal_identity),
        "settings_revision": current,
        "periods": [{"start": aware_iso(start), "end": aware_iso(end)} for start, end in proposal.periods],
        "amps": _count(settings.amps) if same_generation else None,
        "phases": _count(settings.phases) if same_generation else None,
        "power_kw": finite_number(proposal.power_kw),
        "requested_kwh": finite_number(proposal.requested_kwh),
        "planned_kwh": finite_number(proposal.delivered_kwh),
        "cost": None if cost is None or currency is None else {"value": cost, "currency": currency},
        "distance_mil": finite_number(proposal.distance_mil),
        "unpriced": proposal.unpriced,
        "unpriced_slots": proposal.unpriced_slots,
        "priced_slots": proposal.priced_slots,
    }


def _installed_section(capture: CapturedDashboard) -> dict[str, Any] | None:
    """The schedule the charger is running, or `None` when it is running none."""
    plan = capture.plan
    if plan is None:
        return None
    now = _utc(capture.generated_at)
    active = None
    for index, (start, end) in enumerate(plan.windows):
        start, end = _utc(start), _utc(end)
        if start is not None and end is not None and now is not None and start <= now < end:
            active = index
            break
    return {
        "identity": _text(plan.auto_identity),
        "periods": [
            {"start": aware_iso(start), "end": aware_iso(end)} for start, end in plan.windows
        ],
        "amps": _count(plan.amps),
        "phases": _count(plan.phases),
        "power_kw": finite_number(plan.power_kw),
        "active_period_index": active,
    }


def serialize_live(capture: CapturedDashboard) -> dict[str, Any]:
    """The charger's own live facts. Unavailable readings are `null`, never zero; the current setpoint
    is reported under its own name, not as a measurement.
    """
    live = capture.live
    return {
        "charging": live.charging,
        "schedule_active": live.schedule_active,
        "requested_current_a": _count(live.requested_current_a),
        "setpoint_current_a": _count(live.setpoint_current_a),
        "measured_current_a": finite_number(live.measured_current_a),
    }


def serialize_dashboard(
    capture: CapturedDashboard, *, can_act: bool, active_control_writable: bool | None = None
) -> dict[str, Any]:
    """One dashboard response, exactly these sections, in this order.

    `can_act` is the caller's administrative authority; `active_control_writable` says whether this
    carrier may also switch active load balancing (the webhook never may).
    """
    return {
        "api_version": DASHBOARD_API_VERSION,
        "generated_at": aware_iso(capture.generated_at),
        "charger": serialize_charger(capture.charger),
        "settings": serialize_settings(capture),
        "fiscal": None
        if capture.settings is None
        else serialize_fiscal(capture.settings, capture.area_entry),
        "planning": serialize_planning(capture),
        "market": serialize_market(capture),
        "prices": serialize_prices(capture),
        "plan": serialize_plan(capture),
        "live": serialize_live(capture),
        "strategy": serialize_strategy(capture),
        "strategy_options": list(capture.strategy_options),
        "strategy_state": serialize_strategy_state(capture),
        "control": serialize_control(capture, can_act=can_act),
        "charge_progress": capture.charge_progress.as_dict(),
        "site": serialize_site(
            capture.site, can_act=can_act, active_control_writable=active_control_writable
        ),
        "current_range": dict(capture.current_range),
        "soc": serialize_soc(capture.soc),
        "vehicles": [serialize_vehicle(vehicle) for vehicle in capture.vehicles],
        "target_vehicle_id": _text(capture.target_vehicle_id),
        **capture.phase_result.as_fields(),
        "chargers": [{"id": entry_id, "name": name} for entry_id, name in capture.chargers],
        "status": serialize_status(capture),
        "summary": serialize_summary(capture.summary),
    }


def serialize_summary(summary: CapturedSummary | None) -> dict[str, Any] | None:
    """The `summary` block: the setup in words, for a client that shows it without the entity editor.

    `charger`: `start_stop_name`, `current_path` (`change_configuration`, `number`,
    `easee_dynamic_limit`, `none`), `current_entity_name`, `energy_name` (`null` while the energy
    register is found automatically) and `energy_automatic`. `site` is `null` without a site, else
    `main_fuse_a`, `measurement_mode` and `battery_name`. `vehicles` maps a vehicle id to
    `soc_sensor_name`. Names are friendly names, never entity ids; every name may be `null`.
    """
    if summary is None:
        return None
    return {
        "charger": {
            "start_stop_name": summary.start_stop_name,
            "current_path": summary.current_path,
            "current_entity_name": summary.current_entity_name,
            "energy_name": summary.energy_name,
            "energy_automatic": summary.energy_automatic,
        },
        "site": None
        if summary.site is None
        else {
            "main_fuse_a": summary.site[0],
            "measurement_mode": summary.site[1],
            "battery_name": summary.site[2],
        },
        "vehicles": {
            vehicle_id: {"soc_sensor_name": name} for vehicle_id, name in summary.vehicle_sensor_names
        },
    }


def serialize_strategy(capture: CapturedDashboard) -> dict[str, Any]:
    """What the system optimizes: the selected strategy and every strategy row with its availability.

    `cheapest` is always runnable; `solar` and `hybrid` only when the site has derived measurement
    (`MEASUREMENT_MODE_DERIVED`), the signal both need, the same condition `select.AutoStrategySelect`
    uses; otherwise they carry the stable reason.
    """
    selected = None if capture.settings is None else strategy_of(capture.settings)
    supports_solar_and_hybrid = (
        capture.site is not None and capture.site.measurement_mode == MEASUREMENT_MODE_DERIVED
    )
    if supports_solar_and_hybrid:
        rows = [
            {"strategy": strategy, "available": True, "reason": None}
            for strategy in (STRATEGY_CHEAPEST, STRATEGY_SOLAR, STRATEGY_HYBRID)
        ]
        return {"selected": selected, "available": rows}
    rows = []
    if selected is not None:
        rows.append({"strategy": selected, "available": True, "reason": None})
    # Placeholder rows only for non-selected strategies; the selected row already states it.
    for strategy, reason in (
        (STRATEGY_SOLAR, STRATEGY_SOLAR_REASON),
        (STRATEGY_HYBRID, STRATEGY_HYBRID_REASON),
    ):
        if strategy != selected:
            rows.append({"strategy": strategy, "available": False, "reason": reason})
    return {"selected": selected, "available": rows}


def control_facts(capture: CapturedDashboard) -> ControlFacts:
    """The captured facts in the shape the canonical decision rule reads. This contract decides
    nothing; a missing settings record is stated as such, not defaulted.
    """
    settings = capture.settings
    return ControlFacts(
        charging=capture.live.charging,
        pause=PauseIntent() if settings is None else settings.pause,
        has_settings=settings is not None,
        paused=capture.execution.paused is True,
        last_error=capture.execution.error,
        pause_choices=capture.pause_choices,
        start_pending=capture.execution.start_pending,
    )


def serialize_control(capture: CapturedDashboard, *, can_act: bool) -> dict[str, Any]:
    """The control facts: two axes, both derived from this one capture and nothing live.

    The immediate axis is what the charger may be told to do now; the automatic axis is what may
    happen to Auto's execution. Both come from `execution.decide_axes`, so they describe one moment.
    `pause_choices` sits beside the automatic `pause` only: a bare immediate `stop` takes no choice,
    `resume` has nothing to choose, `none` accepts nothing. The other keys are the persisted pause
    observation, the execution gate, the failure code and this connection's administrative authority.
    """
    settings = capture.settings
    axes = execution.decide_axes(control_facts(capture))
    return {
        "immediate_action": axes.immediate.action,
        "immediate_action_reason": axes.immediate.reason,
        "automatic_action": axes.automatic.action,
        "automatic_action_reason": axes.automatic.reason,
        "pause_choices": list(axes.automatic.choices),
        "pause": None if settings is None else encode_pause(settings.pause),
        "pause_blocks_execution": False if settings is None else pause_blocks_execution(settings),
        "execution_error": _text(capture.execution.error),
        "can_act": bool(can_act),
    }


def serialize_strategy_state(capture: CapturedDashboard) -> dict[str, Any] | None:
    """The `strategy_state`: what the selected strategy is doing, in its own shape; `None` for
    `cheapest` (or no settings record), which has nothing beyond `planning`/`plan`.

    Read from `capture.site`'s `solar_state`/`hybrid_state`, never recomputed. A selected strategy
    with no site answers with that snapshot's "nothing observed yet" shape.
    """
    selected = None if capture.settings is None else strategy_of(capture.settings)
    site = capture.site
    if selected == STRATEGY_SOLAR:
        state = (site.solar_state if site is not None else None) or {}
        return {
            "state": _text(state.get("state")) or "unknown",
            "reason": _text(state.get("reason")),
            "available_w": finite_number(state.get("available_w")),
            "requested_a": finite_number(state.get("requested_a")),
            "priority_effective": _text(state.get("priority_effective")),
        }
    if selected == STRATEGY_HYBRID:
        state = (site.hybrid_state if site is not None else None) or {}
        return {
            "state": _text(state.get("state")) or "unknown",
            "reason": _text(state.get("reason")),
            "grid_kwh": finite_number(state.get("grid_kwh")),
            "credit_kwh": finite_number(state.get("credit_kwh")),
            "slack_kwh": finite_number(state.get("slack_kwh")),
            "plan_window_active": bool(state.get("plan_window_active")),
            "forecast_configured": bool(state.get("forecast_sources")),
        }
    return None


def serialize_site(
    site: CapturedSite | None, *, can_act: bool, active_control_writable: bool | None = None
) -> dict[str, Any] | None:
    """The `site`: `None` for a charger with no resolved site; else the site's name (never an entry
    id), its charger count, solar priority and forecast choices as `update_site_settings` reads and
    writes them, active load balancing's read-only availability/enabled/reason, and whether this
    connection may call that command.

    `active_control.writable` defaults to `can_act` and is `False` over the webhook, whose secret may
    write solar settings but never the fuse-protection switch.
    """
    if site is None:
        return None
    if active_control_writable is None:
        active_control_writable = can_act
    return {
        "name": site.site_name,
        "charger_count": site.charger_count,
        "solar_priority": site.solar_priority,
        "solar_forecast": {
            "selected": list(site.solar_forecast_selected),
            "choices": [
                {"id": entry_id, "title": title}
                for entry_id, title in site.solar_forecast_choices
            ],
        },
        "active_control": {
            "available": site.active_control_available,
            "enabled": site.active_control_enabled,
            "reason": site.active_control_reason,
            "writable": bool(active_control_writable),
        },
        "writable": bool(can_act),
    }


def serialize_soc(soc: CapturedSoc | None) -> dict[str, Any] | None:
    """The `soc` block: the state of charge a target is planned against, and how sure it is.

    `missing` lists what a target lacks: `"capacity"` (write it with `update_vehicle`), `"soc"` (the
    source reports nothing and cannot be estimated) and `"vehicle"` while none is chosen. `value` is a
    percentage (one decimal), `age_s` the reading's age (of the fresh reading an estimate was carried
    from), `source` is `charger` or `vehicle`, `estimated` is true when derived from delivered energy,
    `need_kwh` the wall energy the target needs; every field is `null` when unknown.

    For the card's slider it also states the backend's formula (`soc_estimate.target_need_kwh`):
    `(min(target, floor(vehicle_max_percent)) - value) x capacity_kwh / efficiency`, zero at or below
    `value`. `efficiency` is `soc_estimate.CHARGE_EFFICIENCY`; `vehicle_max_percent` is the vehicle's
    own charge limit; `vehicles` lists every candidate `{id, name}` when more than one exists.
    """
    if soc is None:
        return None
    value = finite_number(soc.value)
    age = finite_number(soc.age_s)
    return {
        "value": None if value is None else round(value, 1),
        "age_s": None if age is None else int(age),
        "source": soc.source,
        "estimated": soc.estimated,
        "target_percent": finite_number(soc.target_percent),
        "need_kwh": None if finite_number(soc.need_kwh) is None else round(soc.need_kwh, 2),
        "capacity_kwh": finite_number(soc.capacity_kwh),
        "vehicle_name": _text(soc.vehicle_name),
        "vehicle_id": _text(soc.vehicle_id),
        "vehicle_max_percent": finite_number(soc.vehicle_max_percent),
        "efficiency": CHARGE_EFFICIENCY,
        "vehicles": [{"id": vid, "name": name} for vid, name in soc.vehicle_choices],
        "missing": [
            name
            for name, absent in (
                ("soc", value is None),
                ("capacity", finite_number(soc.capacity_kwh) is None),
                ("vehicle", soc.vehicle_id is None and bool(soc.vehicle_choices)),
            )
            if absent
        ],
    }


def serialize_vehicle(vehicle: CapturedVehicle) -> dict[str, Any]:
    """One entry of the root `vehicles`: `capacity_source` is `reported` (by the vehicle, not editable),
    `stored` (a person's answer) or `null` (missing).
    """
    return {
        "id": vehicle.id,
        "name": _text(vehicle.name),
        "soc_entity_id": _text(vehicle.soc_entity_id),
        "capacity_kwh": finite_number(vehicle.capacity_kwh),
        "capacity_source": vehicle.capacity_source if finite_number(vehicle.capacity_kwh) is not None else None,
        "consumption_kwh_per_10km": finite_number(vehicle.consumption_kwh_per_10km),
        "max_percent": finite_number(vehicle.max_percent),
        "soc_percent": finite_number(vehicle.soc_percent),
    }


def status_facts(capture: CapturedDashboard) -> StatusFacts:
    """The captured moment as the typed facts `status_compose.compose_status` reads; nothing live, and
    every decision is the composer's.
    """
    settings = capture.settings
    snapshot = capture.snapshot
    planning = None
    if snapshot is not None:
        planning = PlanningFacts(
            state=snapshot.state,
            reason=snapshot.reason,
            missing=tuple(snapshot.missing),
            publication_at=_utc(snapshot.publication_at),
            must_buy_now_kwh=finite_number(snapshot.must_buy_kwh),
            history_weekday=None if snapshot.history is None else snapshot.history.weekday,
            history_percent=None if snapshot.history is None else snapshot.history.percent,
            history_weeks=None if snapshot.history is None else snapshot.history.weeks,
        )
    proposal = None
    section = _proposal_section(capture)
    if section is not None and snapshot is not None:
        source = snapshot.proposal
        cost = section["cost"]
        proposal = ProposalFacts(
            periods=_spans(source.periods),
            identity=section["identity"],
            planned_kwh=section["planned_kwh"],
            requested_kwh=section["requested_kwh"],
            cost=None if cost is None else cost["value"],
            currency=None if cost is None else cost["currency"],
            distance_mil=section["distance_mil"],
            unpriced=section["unpriced"],
        )
    area = capture.area
    state = serialize_strategy_state(capture)
    strategy = None if settings is None else strategy_of(settings)
    solar = hybrid = None
    if state is not None and strategy == STRATEGY_SOLAR:
        solar = SolarFacts(state=state["state"], reason=state["reason"], requested_a=state["requested_a"])
    elif state is not None and strategy == STRATEGY_HYBRID:
        hybrid = HybridFacts(
            reason=state["reason"],
            grid_kwh=state["grid_kwh"],
            credit_kwh=state["credit_kwh"],
            forecast_configured=state["forecast_configured"],
        )
    soc = serialize_soc(capture.soc)
    site = capture.site
    pause = None if settings is None else settings.pause
    return StatusFacts(
        now=capture.generated_at,
        charger_available=capture.charger.available,
        has_settings=settings is not None,
        suggested=capture.suggested,
        strategy=strategy,
        departure_enabled=bool(settings is not None and settings.departure_enabled),
        planning=planning,
        price_state=None if area is None else area.state,
        price_reason=None if area is None else _text(area.reason),
        usable_price_rows=len(capture.intervals or ()),
        waiting_for_tomorrow=None if area is None else area.waiting_for_tomorrow,
        prices_unpriced=None if snapshot is None else snapshot.unpriced,
        charging=capture.live.charging,
        held_by_charger=capture.live.held_by_charger,
        charger_disabled=capture.live.charger_disabled,
        hold_until=None if capture.live.hold_until is None else _utc(capture.live.hold_until),
        hold_overridden=capture.live.hold_overridden,
        paused=capture.execution.paused is True,
        pause_until=None if pause is None else _utc(pause.expires_at),
        pause_choice=None if pause is None else pause.choice,
        installed_periods=_installed_spans(capture),
        proposal=proposal,
        relation_applied=None if snapshot is None else snapshot.applied,
        pending_identity=_text(capture.execution.pending_identity),
        solar=solar,
        hybrid=hybrid,
        soc=None if soc is None else SocFacts(missing=tuple(soc["missing"])),
        target=None
        if capture.target is None
        else TargetFacts(
            stop_soc_percent=capture.target.stop_soc_percent,
            stop_basis=capture.target.stop_basis,
            stop_reading_age_s=capture.target.stop_reading_age_s,
            unverifiable_reason=capture.target.unverifiable_reason,
        ),
        load_balancing_capable=bool(capture.charger.capability_map().get("load_balancing")),
        load_balancing=None
        if site is None
        else LoadBalancingFacts(
            state=site.state,
            active_control_enabled=site.active_control_enabled,
            proposed_current_a=finite_number(site.proposed_current_a),
            limiting_phase=_text(site.limiting_phase),
        ),
    )


def serialize_status(capture: CapturedDashboard) -> dict[str, Any]:
    """The composed status block `{tone, lines: [{code, params}]}`: typed facts, never prose."""
    return compose_status(status_facts(capture))


def _unsupported_version() -> DashboardFailure:
    return DashboardFailure(ERROR_UNSUPPORTED_VERSION, unsupported_version_text(DASHBOARD_API_VERSION))


def _dashboard_version(msg: dict[str, Any]) -> int | None:
    """The dashboard version this request speaks, or `None` for the stable refusal (a missing version
    is refused too).
    """
    version = msg.get("api_version")
    if isinstance(version, bool) or version != DASHBOARD_API_VERSION:
        return None
    return DASHBOARD_API_VERSION


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/list_chargers",
        # Unvalidated so a wrong version gets this contract's stable code, not vol's generic error.
        vol.Optional("api_version"): object,
    }
)
@websocket_api.async_response
async def websocket_list_chargers(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Which chargers exist and what each can do. Reads memory, and nothing else."""
    if _dashboard_version(msg) is None:
        failure = _unsupported_version()
        connection.send_error(msg["id"], failure.code, failure.message)
        return
    connection.send_result(msg["id"], serialize_charger_list(capture_chargers(hass)))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_dashboard",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
    }
)
@websocket_api.async_response
async def websocket_get_dashboard(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One charger's whole view, captured once and then serialized.

    The Energy dashboard's solar-forecast domains are read first (an `await`) because
    `capture_dashboard` is synchronous.
    """
    failure = None if _dashboard_version(msg) is not None else _unsupported_version()
    if failure is None:
        resolved = resolve_charger_request(hass, msg)
        failure = resolved if isinstance(resolved, DashboardFailure) else None
    if failure is not None:
        connection.send_error(msg["id"], failure.code, failure.message)
        return
    capture = capture_dashboard(
        hass, resolved, forecast_domains=await async_forecast_capable_domains(hass)  # type: ignore[arg-type]
    )
    connection.send_result(msg["id"], serialize_dashboard(capture, can_act=is_admin(connection)))


async def async_webhook_dashboard(
    hass: HomeAssistant, entry: ConfigEntry, api_version: Any
) -> dict[str, Any] | DashboardFailure:
    """The webhook twin of `spotnav/get_dashboard` for one charger: same capture and serializer, same
    refusal for another version. The holder may act on this charger (`can_act` true) and write solar
    settings, but not switch active load balancing (`site.active_control.writable` false).
    """
    if isinstance(api_version, bool) or api_version != DASHBOARD_API_VERSION:
        return _unsupported_version()
    capture = capture_dashboard(
        hass, entry, forecast_domains=await async_forecast_capable_domains(hass)
    )
    return serialize_dashboard(capture, can_act=True, active_control_writable=False)


@callback
def async_setup_dashboard_api(hass: HomeAssistant) -> None:
    """Register both commands once for the domain, not once per config entry."""
    websocket_api.async_register_command(hass, websocket_list_chargers)
    websocket_api.async_register_command(hass, websocket_get_dashboard)
