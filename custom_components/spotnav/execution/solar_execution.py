"""Solar surplus mode: execution.

* Authority stays in the Auto layer. This module never calls a charger directly: every start, stop
  and modulation goes through `AutoExecutor.async_solar_start`/`async_solar_stop`/
  `async_solar_set_current`, which take the charger's execution lock and call
  `ChargingController`. It owns no lock and issues no service call.
* The fast tick comes from site capacity. `SolarExecutionCoordinator` subscribes to the site's
  `SiteCapacityController.add_listener` and evaluates the pure `SolarController`
  (`site/solar_surplus.py`) on each of its recomputes. No timer is created here.
* The observation adapter (`_build_observation`) reads signed grid power and voltage per phase from
  `SiteCapacityResult`'s diagnostic fields (never discarded merely for age) and gates them by
  `phase_liveness` (usable only when `fresh` or `confirmed_unchanged`). A direct site (current per
  phase only) instead reads the meter's total grid power from the site controller (unknown unless fresh),
  splits it over the charger's phases at the nominal voltage and caps it by each phase's fuse headroom. The car's delivered current
  comes from `SiteCapacityController.charger_measured_current`. The battery comes from
  `battery_aggregate_power`, usable only when fresh, with `battery_configured` set from whether an
  aggregate entity exists, never inferred from the reading itself.
* A direct site whose phase measurement is incomplete (a phase with no value, e.g. an inverter in
  standby) still runs on the total grid power: a phase that reads keeps its own fuse headroom, and a phase
  that does not caps the charger at what it draws there now, or the minimum current if that is more,
  whatever the total says: a netted total can export while the unread phase imports heavily (a
  three-phase inverter against a one-phase load), so current never rises on a phase that cannot be read.
* A single-phase charger whose phase is not known is reckoned on a stand-in phase (`L1`): the total does not
  depend on the phase, the fuse cap is the lowest of all three phases' caps, and its draw the largest
  phase its own measured current reads.
* What keeps solar from a basis is named (`SolarBasis`): the total grid power not set or unreadable, an
  unreadable battery, the charger's own current not set or unreadable (solar then runs blind, at the
  minimum current only), and the phases of an incomplete site measurement it runs without.
* Several chargers on one site share the surplus in the site's charger order (priority, then the order
  they joined): each coordinator gathers the other members' facts (`SolarExecutionCoordinator.
  share_member`) and hands its own controller the split (`site/solar_surplus.py`'s
  `priority_adjust_w`) on top of its own reckoning.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant

from ..const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_MAIN_FUSE_A,
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_SAFETY_MARGIN_A,
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_GRID_POWER_SOURCE,
    CONF_PHASE_WIRING,
    CONF_SOLAR_PRIORITY,
    DEFAULT_MIN_CURRENT_A,
    DEFAULT_SOLAR_PRIORITY,
    DOMAIN,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DIRECT,
)
from ..planning.phases import effective_phases
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..planning.auto_settings import AutoSettingsStore, STRATEGY_HYBRID, STRATEGY_SOLAR
from ..runtime import charger_data, preview_for, site_controller_for
from ..site.measurement_problem import UNHEALTHY_STATES
from ..site.measurement_source import grid_power_source_from_dict, source_from_dict
from ..site.site_capacity import PhaseName, PHASES, charger_order_key
from ..site.site_capacity_controller import SiteCapacityController
from ..site.solar_capability import solar_capability
from ..site.solar_surplus import (
    SolarConfig,
    SolarController,
    SolarObservation,
    SolarShareMember,
    SolarVerdict,
    priority_adjust_w,
    surplus_breakdown,
)
from .auto_execution import AutoExecutor, pause_blocks_execution
from .controller import ChargingController


_LOGGER = logging.getLogger(__name__)

#: Greppable token carried by every solar start/stop/state-transition log line.
SOLAR_SURPLUS_LOG_TOKEN: Final = "SOLAR_SURPLUS"

#: After the integration loads, the charger's own measured current gets this long (seconds) to report
#: before its absence is warned about: on every restart it is unavailable for a moment, and a warning
#: about a state that is gone seconds later is noise.
MEASUREMENT_WARNING_GRACE_S: Final = 120.0

#: A running charger whose car draws this much (amps) below what solar asked of it takes less than it
#: is offered, and the surplus it leaves goes on to the chargers after it in the site's order.
TAKES_LESS_MARGIN_A: Final = 2.0
#: How long (seconds) after a start a charger's draw is left to settle before it counts as taking less:
#: a car ramps up over several seconds.
TAKES_LESS_GRACE_S: Final = 60.0

#: Token for hybrid arbitration handoff logs.
HYBRID_LOG_TOKEN: Final = "HYBRID"



def site_controller_for_charger(hass: HomeAssistant, charger_entry_id: str) -> SiteCapacityController | None:
    """The live `SiteCapacityController` for the site this charger belongs to, or `None`.

    Scans site entries whose stored `charger_entry_ids` names this charger. Readiness is whether a
    controller instance exists in `hass.data`, not `ConfigEntryState.LOADED`: the site's setup rebinds
    solar execution before the entry is marked loaded. A charger with no site, or whose site is not set
    up yet, answers `None`.
    """
    for site_entry in hass.config_entries.async_entries(DOMAIN):
        if site_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE:
            continue
        members = site_entry.data.get(CONF_CHARGER_ENTRY_IDS) or []
        if charger_entry_id not in members:
            continue
        return site_controller_for(hass, site_entry.entry_id)
    return None


def site_supports_solar(hass: HomeAssistant, charger_entry_id: str) -> bool:
    """Whether this charger's site can run solar execution at all: a loaded site that knows the grid's
    signed power, either per phase (`derived_phase_current` mode, with its voltages) or as the meter's
    total on a site that reports only current (`site/solar_capability.py`).
    """
    site = site_controller_for_charger(hass, charger_entry_id)
    if site is None:
        return False
    return solar_capability(
        site.config.get("measurement_mode"), site.config.get(CONF_GRID_POWER_SOURCE)
    ).capable


@dataclass(frozen=True, slots=True)
class SolarBasis:
    """What keeps solar from a full basis, named for the status (`planning/status_compose.py`).

    `problem` is why there is no basis at all: `grid_power_not_set` or `grid_power_unreadable` (a direct
    site's total grid power, `problem_entity` its entity) or `battery_unreadable`. `charger_current` is
    `not_set` or `unreadable` (`charger_current_entity`) while the charger's own measured current is
    unknown, so solar runs blind at the minimum current. `site_incomplete_phases` are the phases of an
    unusable site measurement that solar runs without, on the total grid power.
    """

    problem: str | None = None
    problem_entity: str | None = None
    charger_current: str | None = None
    charger_current_entity: str | None = None
    site_incomplete_phases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SolarExecutionState:
    """Live solar-execution facts for one charger, for its Auto state.

    `active_control_active` is the site's active-control opt-in: with it unset, site capacity never
    writes a requested current and solar runs on/off at the start current only.
    """

    state: str
    action: str
    reason: str
    requested_a: float | None
    net_grid_w: float | None
    export_w: float | None
    car_w: float | None
    battery_w: float | None
    available_w: float | None
    available_a: float | None
    priority_effective: str | None
    active_control_active: bool
    #: `True` while this charger is on `hybrid` and an installed plan window is active: the plan
    #: owns the charger at full current, and this verdict was computed but never applied.
    held_by_plan: bool = False
    #: `True` while this charger is on `hybrid` and its remaining need is `<= 0` (`plan_hybrid`'s
    #: `satisfied`).
    satisfied: bool = False
    #: What keeps solar from a full basis, named.
    basis: SolarBasis = field(default_factory=SolarBasis)


def solar_execution_state(hass: HomeAssistant, charger_entry_id: str) -> SolarExecutionState | None:
    """The latest `SolarExecutionState` for this charger, or `None` when no coordinator has
    produced one (no site, solar unsupported, or no verdict yet).
    """
    data = charger_data(hass, charger_entry_id)
    if data is None or data.solar is None:
        return None
    return data.solar.state


def _liveness_gated(
    phase_liveness: dict[PhaseName, str | None],
    diagnostic: dict[PhaseName, float | None],
    phase: PhaseName,
) -> float | None:
    """One phase's diagnostic reading, usable only when its liveness is `fresh` or
    `confirmed_unchanged`. Applied here, never in `site/site_capacity.py`, which must not
    change how the protection layer grades signed power.
    """
    if phase_liveness.get(phase) not in ("fresh", "confirmed_unchanged"):
        return None
    return diagnostic.get(phase)


def _wiring(site: SiteCapacityController, charger_entry_id: str) -> dict[str, Any]:
    return (site.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}


def _car_phases(site: SiteCapacityController, charger_entry_id: str) -> tuple[PhaseName, ...]:
    """This charger's wired phases from the site's `CONF_PHASE_WIRING` (as
    `SiteCapacityController._build_requests` reads them). Empty when a single-phase
    charger's phase is unknown (`_phase_unknown`).
    """
    wiring = _wiring(site, charger_entry_id)
    if wiring.get("phases", 3) == 3:
        return PHASES
    phase = wiring.get("phase")
    return (phase,) if phase else ()


def _phase_unknown(site: SiteCapacityController, charger_entry_id: str) -> bool:
    """A single-phase charger whose phase is not set in the site's wiring."""
    wiring = _wiring(site, charger_entry_id)
    return wiring.get("phases", 3) != 3 and not wiring.get("phase")


#: The phase a single-phase charger of unknown phase is reckoned on (`_build_observation`).
STAND_IN_PHASE: Final[PhaseName] = "L1"


#: The phase voltage assumed for a site with no voltage readings (direct measurement), the Nordic
#: nominal one.
NOMINAL_PHASE_VOLTAGE_V: Final = 230.0
#: The voltage between two phases that `NOMINAL_PHASE_VOLTAGE_V` goes with (a TN network).
NOMINAL_VOLTAGE_BETWEEN_PHASES_V: Final = 400.0


def nominal_phase_voltage_v(voltage_between_phases_v: float, car_phases: int) -> float:
    """The per-phase voltage the surplus reasons with on a direct site. Three-phase power is
    `sqrt(3) x U x I`, which at the nominal 230 V per phase (400 V between phases) is figured as
    `3 x 230 V x I`; at another voltage between phases (230 V on an IT network) the per-phase voltage
    scales with it. One phase is 230 V on either network.
    """
    if car_phases != 3:
        return NOMINAL_PHASE_VOLTAGE_V
    return NOMINAL_PHASE_VOLTAGE_V * voltage_between_phases_v / NOMINAL_VOLTAGE_BETWEEN_PHASES_V


def _split_total(
    total_w: float | None, car_phases: tuple[PhaseName, ...]
) -> dict[PhaseName, float | None]:
    """The meter's total grid power as the per-phase figures the surplus reasons with. The phases are
    settled summed, so the total is spread evenly over the phases the charger uses (three: a third
    each; one: the whole total on that phase) and the other phases carry nothing; the sum is the
    total either way. An unknown total, or no known phase, is unknown on every phase.
    """
    if total_w is None or not car_phases:
        return {phase: None for phase in PHASES}
    share = total_w / len(car_phases)
    return {phase: (share if phase in car_phases else 0.0) for phase in PHASES}


def _fuse_limit_a(site: SiteCapacityController) -> float | None:
    """The most current a phase may carry: the main fuse less the safety margin; `None` with no fuse."""
    main_fuse_a = site.config.get(CONF_MAIN_FUSE_A)
    if main_fuse_a is None:
        return None
    return float(main_fuse_a) - float(site.config.get(CONF_SAFETY_MARGIN_A, 0.0))


def _phase_headroom_a(result: Any, phase: PhaseName, fuse_limit_a: float | None) -> float | None:
    """One phase's fuse headroom: the site's own, or, while the site's measurement as a whole is unusable,
    the fuse limit less this phase's own reading when that is fresh. `None` when the phase does not read."""
    headroom = result.phase_headroom_a.get(phase)
    if headroom is not None:
        return headroom
    if fuse_limit_a is None or result.phase_liveness.get(phase) not in ("fresh", "confirmed_unchanged"):
        return None
    measured = result.measured_phase_current_a.get(phase)
    return None if measured is None else fuse_limit_a - measured


def _fuse_caps(
    result: Any,
    delivered_a: dict[PhaseName, float | None],
    *,
    fuse_limit_a: float | None,
    min_current_a: float,
    measurement_unusable: bool,
) -> dict[PhaseName, float | None]:
    """The most current the charger may draw on each phase of `delivered_a`: what it draws there now
    (nothing when unknown) plus the phase's fuse headroom. A phase with no reading caps it at what it draws
    now, or the minimum current if that is more, even while the total exports: the total is netted over the
    phases, and a three-phase inverter's export says nothing about one phase's import, so current never rises
    on a phase that cannot be read. `None` with no fuse to reckon by, and `None` for a phase with no headroom
    while the site's measurement is not unusable (a site that is off or not configured gets no cap).
    """
    caps: dict[PhaseName, float | None] = {}
    for phase, delivered in delivered_a.items():
        draw = 0.0 if delivered is None else delivered
        headroom = _phase_headroom_a(result, phase, fuse_limit_a)
        if headroom is not None:
            caps[phase] = draw + headroom
        elif fuse_limit_a is None or not measurement_unusable:
            caps[phase] = None
        else:
            caps[phase] = max(min_current_a, draw)
    return caps


def _limited_to(
    car_phases: tuple[PhaseName, ...], limit: int | None, measured: Any
) -> tuple[PhaseName, ...]:
    """The phases a charge uses when the car takes fewer than the charger is wired for: the `limit`
    phases the charger is delivering most on now (the first ones when it delivers nothing yet).
    """
    if limit is None or limit >= len(car_phases):
        return car_phases

    def delivered(phase: PhaseName) -> float:
        value = None if measured is None else measured.get(phase).value
        return value if isinstance(value, (int, float)) else 0.0

    ranked = sorted(car_phases, key=lambda phase: (-delivered(phase), car_phases.index(phase)))
    return tuple(phase for phase in car_phases if phase in ranked[:limit])


def _delivered_any_phase(measured: Any) -> float | None:
    """A single-phase charger of unknown phase: the largest phase its measured current reads, `None`
    when no phase reads."""
    if measured is None:
        return None
    values = [measured.get(phase).value for phase in PHASES]
    numbers = [value for value in values if isinstance(value, (int, float))]
    return max(numbers) if numbers else None


def _build_observation(
    site: SiteCapacityController,
    charger_entry_id: str,
    *,
    now: float,
    effective_phases: int | None = None,
) -> SolarObservation:
    """One tick's `SolarObservation` from the site's computed result plus this charger's
    measured current.

    A derived site reads the signed power and voltage of every phase. A direct site reads the
    meter's total grid power (`SiteCapacityController.grid_total_reading`, unknown unless fresh),
    splits it over the charger's phases at the nominal voltage and caps the result by each phase's
    fuse headroom (`_fuse_caps`, which also covers an incomplete phase measurement). `effective_phases`
    is what the charge uses (`planning/phases.py`): a car on fewer phases than the charger is wired for
    draws on that many of them. A single-phase charger of unknown phase is reckoned on `STAND_IN_PHASE`.
    """
    result = site.result
    measured = site.charger_measured_current(charger_entry_id)
    phase_unknown = _phase_unknown(site, charger_entry_id)
    car_delivered_a: dict[PhaseName, float | None]
    if phase_unknown:
        car_phases: tuple[PhaseName, ...] = (STAND_IN_PHASE,)
        car_delivered_a = {STAND_IN_PHASE: _delivered_any_phase(measured)}
    else:
        car_phases = _limited_to(_car_phases(site, charger_entry_id), effective_phases, measured)
        car_delivered_a = {
            phase: (None if measured is None else measured.get(phase).value) for phase in car_phases
        }

    phase_cap_a: dict[PhaseName, float | None] | None = None
    if site.config.get("measurement_mode") == MEASUREMENT_MODE_DIRECT:
        total_w, _state = site.grid_total_reading()
        signed_grid_w: dict[PhaseName, float | None] = _split_total(total_w, car_phases)
        nominal_v = nominal_phase_voltage_v(
            stored_voltage_between_phases_v(site.config), len(car_phases)
        )
        voltage_v: dict[PhaseName, float | None] = {phase: nominal_v for phase in PHASES}
        cap_delivered = (
            {phase: car_delivered_a[STAND_IN_PHASE] for phase in PHASES} if phase_unknown else car_delivered_a
        )
        caps = _fuse_caps(
            result,
            cap_delivered,
            fuse_limit_a=_fuse_limit_a(site),
            min_current_a=float(_wiring(site, charger_entry_id).get("min_current_a", DEFAULT_MIN_CURRENT_A)),
            measurement_unusable=result.state in UNHEALTHY_STATES,
        )
        if phase_unknown:
            values = list(caps.values())
            phase_cap_a = {
                STAND_IN_PHASE: None if any(cap is None for cap in values) else min(values)  # type: ignore[type-var]
            }
        else:
            phase_cap_a = caps
    else:
        signed_grid_w = {
            phase: _liveness_gated(
                result.phase_liveness, result.phase_signed_active_power_diagnostic_w, phase
            )
            for phase in PHASES
        }
        voltage_v = {
            phase: _liveness_gated(result.phase_liveness, result.phase_voltage_v, phase)
            for phase in PHASES
        }
        if phase_unknown:
            # Which phase the car is on is unknown: the mean of the phases that read.
            known = [value for value in voltage_v.values() if value is not None]
            voltage_v = {**voltage_v, STAND_IN_PHASE: sum(known) / len(known) if known else None}

    battery_reading = site.battery_aggregate_power()
    battery_configured = battery_reading is not None
    battery_w: float | None = None
    if (
        battery_reading is not None
        and battery_reading.value is not None
        and battery_reading.problem is None
        and battery_reading.age_s is not None
        and battery_reading.age_s <= result.max_age_s
    ):
        battery_w = battery_reading.value

    return SolarObservation(
        now=now,
        signed_grid_w=signed_grid_w,
        voltage_v=voltage_v,
        car_delivered_a=car_delivered_a,
        battery_w=battery_w,
        car_phases=car_phases,
        battery_configured=battery_configured,
        phase_cap_a=phase_cap_a,
    )


def _source_entity(source_data: Any, phases: tuple[PhaseName, ...]) -> str | None:
    """The entity a measured-current source reads the charger's phases from (its first one)."""
    source = source_from_dict(source_data)
    if source is None:
        return None
    if source.entity_id:
        return source.entity_id
    entity_ids = source.entity_ids or {}
    for phase in (*phases, *PHASES):
        if entity_ids.get(phase):
            return entity_ids[phase]
    return None


def solar_basis(
    site: SiteCapacityController, charger_entry_id: str, observation: SolarObservation
) -> SolarBasis:
    """`SolarBasis` for one charger's observation."""
    problem: str | None = None
    problem_entity: str | None = None
    incomplete: tuple[str, ...] = ()
    direct = site.config.get("measurement_mode") == MEASUREMENT_MODE_DIRECT
    if direct:
        total_w, state = site.grid_total_reading()
        if state == "not_configured":
            problem = "grid_power_not_set"
        elif total_w is None:
            problem = "grid_power_unreadable"
            source = grid_power_source_from_dict(site.config.get(CONF_GRID_POWER_SOURCE))
            problem_entity = None if source is None else source.power
        else:
            measurement = site.measurement_problem
            if measurement is not None:
                incomplete = tuple(item.phase for item in measurement.phases)
    if problem is None and observation.battery_configured and observation.battery_w is None:
        problem = "battery_unreadable"
        problem_entity = site.config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
    charger_current: str | None = None
    charger_current_entity: str | None = None
    if any(observation.car_delivered_a.get(phase) is None for phase in observation.car_phases):
        source_data = _wiring(site, charger_entry_id).get(CONF_MEASURED_CURRENT_SOURCE)
        if site.charger_measured_current(charger_entry_id) is None:
            charger_current = "not_set"
        else:
            charger_current = "unreadable"
            charger_current_entity = _source_entity(source_data, observation.car_phases)
    return SolarBasis(
        problem=problem,
        problem_entity=problem_entity,
        charger_current=charger_current,
        charger_current_entity=charger_current_entity,
        site_incomplete_phases=incomplete,
    )


def _adopt_running(solar: SolarController, *, now: float) -> None:
    """Seed a fresh `SolarController` as already `on`, because the charger is already charging under
    solar (a restart found it mid-charge, or a manual Start left it running): adopt it rather than stop
    it, so a restart does not cycle the contactor.

    `SolarController` has no public seeding entry point and a fresh one starts at `off`, where it can
    only `hold`. This is the narrowest reach into its attributes, done once before the first
    `observe()`, seeding what a real start transition leaves behind so `min_on_s` and `stale_grace_s`
    behave as if it had started under this coordinator.
    """
    solar._state = "on"  # noqa: SLF001 -- see this function's own docstring
    solar._on_since = now
    solar._arming_since = None
    solar._disarming_since = None
    solar._last_stop_at = None
    solar._last_requested_a = None
    solar._stale_since = None


def _satisfied_state(site: SiteCapacityController) -> SolarExecutionState:
    """The `SolarExecutionState` a hybrid charger reports while its need is satisfied: no
    verdict was computed, so verdict-shaped fields read as inert "off, holding" and
    `satisfied` tells the two apart.
    """
    return SolarExecutionState(
        state="off",
        action="hold",
        reason="hybrid_satisfied",
        requested_a=None,
        net_grid_w=None,
        export_w=None,
        car_w=None,
        battery_w=None,
        available_w=None,
        available_a=None,
        priority_effective=None,
        active_control_active=bool(site.config.get(CONF_ACTIVE_CONTROL_ENABLED, False)),
        held_by_plan=False,
        satisfied=True,
    )


class SolarExecutionCoordinator:
    """One instance per charger, driven by its site's recompute notifications.

    Holds the pure `SolarController` (rebuilt when the site changes or the strategy leaves and returns
    to `solar`), the site subscription, and the latest `SolarExecutionState`. Every charger action is
    delegated to `AutoExecutor`; this class only computes a verdict and asks for it to be carried out.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        charger_entry_id: str,
        controller: ChargingController,
        executor: AutoExecutor,
        store: AutoSettingsStore,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hass = hass
        self._charger_entry_id = charger_entry_id
        self._controller = controller
        self._executor = executor
        self._store = store
        self._now = now
        # When this coordinator first evaluated, for the start-up grace of the missing-measurement
        # warning (set on that first evaluation so an injected clock is the one it reads).
        self._born_at: float | None = None
        self._solar: SolarController | None = None
        self._site: SiteCapacityController | None = None
        self._site_unsub: Callable[[], None] | None = None
        self._state: SolarExecutionState | None = None
        self._logged: tuple[Any, ...] | None = None
        # The missing-measurement warning is said once per coordinator, not on every blind transition.
        self._warned_missing = False
        # Previous tick's `held_by_plan`, so the handoff is logged once per transition.
        self._logged_held_by_plan: bool | None = None
        # Same, for the satisfied transition.
        self._logged_satisfied: bool | None = None
        # Coalescing guard: `_on_site_update` fires often, but an evaluation awaits
        # `AutoExecutor`'s lock and `SolarController` is not reentrant, so a trigger arriving mid-
        # evaluation is dropped; the next site recompute retries.
        self._evaluating = False

    @property
    def state(self) -> SolarExecutionState | None:
        return self._state

    def async_start(self) -> None:
        """(Re)subscribe to this charger's site and evaluate once immediately.

        Idempotent; always rebinds to whatever `site_controller_for_charger` finds now. Config entries load
        in no guaranteed order, so a charger commonly sets up before its site: the site's setup rebinds
        member chargers afterwards, which also picks up a site reload that built a fresh controller. No
        site yet is not an error; `site_supports_solar` tells "will never run" from "not wired up yet".
        """
        self.async_stop()
        site = site_controller_for_charger(self._hass, self._charger_entry_id)
        self._site = site
        if site is None:
            return
        self._site_unsub = site.add_listener(self._on_site_update)
        self._on_site_update()

    def async_stop(self) -> None:
        """Drop the subscription. Never touches the charger."""
        if self._site_unsub is not None:
            self._site_unsub()
            self._site_unsub = None
        self._site = None

    @callback
    def _on_site_update(self) -> None:
        if self._evaluating:
            return
        self._evaluating = True
        self._hass.async_create_task(self._async_evaluate_guarded())

    async def _async_evaluate_guarded(self) -> None:
        try:
            await self._async_evaluate()
        finally:
            self._evaluating = False

    async def _async_evaluate(self) -> None:
        site = self._site
        if site is None:
            return
        settings = self._store.settings(self._charger_entry_id)
        if settings.strategy not in (STRATEGY_SOLAR, STRATEGY_HYBRID):
            # Neither solar nor hybrid: go dormant; the active strategy owns the charger.
            if (
                self._state is not None
                and self._state.state in ("on", "disarming")
                and not self._state.held_by_plan
            ):
                # Solar itself was driving this charge (never while `held_by_plan`, when a plan window
                # was running it).
                await self._executor.async_solar_stop()
            self._solar = None
            self._state = None
            site.notify_solar_surplus_changed()
            return
        hybrid_satisfied = settings.strategy == STRATEGY_HYBRID and self._hybrid_satisfied()
        if hybrid_satisfied:
            # Hybrid must stop at target: the need is met (`plan_hybrid`'s `satisfied`), and
            # running on surplus would take `car_first` sun from the house battery or spend evening
            # grid energy nobody asked for.
            if (
                self._state is not None
                and self._state.state in ("on", "disarming")
                and not self._state.held_by_plan
            ):
                await self._executor.async_solar_stop()
            self._solar = None
            self._state = _satisfied_state(site)
            self._log_hybrid_satisfied(True)
            await self._async_recalculate_hybrid_preview()
            site.notify_solar_surplus_changed()
            return
        if settings.strategy == STRATEGY_HYBRID:
            self._log_hybrid_satisfied(False)

        if self._solar is None:
            self._solar = self._build_controller(site)
        observation = _build_observation(
            site,
            self._charger_entry_id,
            now=self._now(),
            effective_phases=effective_phases(self._hass, self._charger_entry_id),
        )
        share_adjust_w = self._share_adjust_w(site, observation)
        if share_adjust_w:
            observation = replace(observation, share_adjust_w=share_adjust_w)
        if self._unmeasured_start_allowed(site, now=observation.now):
            observation = replace(observation, unmeasured_start_allowed=True)
        verdict = self._solar.observe(observation)
        held_by_plan = settings.strategy == STRATEGY_HYBRID and self._controller.plan_window_active_now
        if held_by_plan:
            # Arbitration: the state machine saw this observation (its timers keep ticking) but its
            # verdict is not carried out while a plan window owns the charger.
            pass
        else:
            await self._apply_verdict(verdict)
        self._update_state(
            verdict, site, held_by_plan=held_by_plan, basis=solar_basis(site, self._charger_entry_id, observation)
        )
        self._log_transition(verdict)
        self._log_hybrid_handoff(held_by_plan)
        if settings.strategy == STRATEGY_HYBRID:
            await self._async_recalculate_hybrid_preview()
        # This method is asynchronous (`_apply_verdict` awaits the executor's lock), so it finishes
        # after the site's own notify already rendered the previous `solar_surplus_snapshot`; this
        # asks the site to render again (see
        # `SiteCapacityController.notify_solar_surplus_changed`).
        site.notify_solar_surplus_changed()

    def solar_activity(self, site: SiteCapacityController) -> tuple[str, float | None] | None:
        """This charger's solar state on `site` and since when it has been charging, `None` when it takes
        no part (another site, or no solar controller: not on `solar` or `hybrid`)."""
        if self._site is not site or self._solar is None or self._state is None or self._state.held_by_plan:
            return None
        return self._state.state, self._solar.on_since

    def _unmeasured_start_allowed(self, site: SiteCapacityController, *, now: float) -> bool:
        """Whether a blind start (no reading of this charger's own current) may go ahead: no other charger
        on the site is arming, except one after this one in the site's order (it yields to this one), and
        none started under solar less than `TAKES_LESS_GRACE_S` ago (its draw is not in the grid reading
        yet). Two chargers never claim the same export at once."""
        members = list(site.config.get(CONF_CHARGER_ENTRY_IDS) or [])
        if self._charger_entry_id not in members:
            return False
        own_key = charger_order_key(
            site.charger_priority(self._charger_entry_id), members.index(self._charger_entry_id), self._charger_entry_id
        )
        for order, charger_entry_id in enumerate(members):
            if charger_entry_id == self._charger_entry_id:
                continue
            data = charger_data(self._hass, charger_entry_id)
            coordinator = None if data is None else data.solar
            activity = None if coordinator is None else coordinator.solar_activity(site)
            if activity is None:
                continue
            state, on_since = activity
            if state == "arming":
                peer_key = charger_order_key(site.charger_priority(charger_entry_id), order, charger_entry_id)
                peer_measured = coordinator is not None and coordinator.state is not None and (
                    coordinator.state.basis.charger_current is None
                )
                # A peer that reads its own current never yields; a blind one after this one does.
                if peer_measured or peer_key < own_key:
                    return False
            elif state in ("on", "disarming") and (on_since is None or now - on_since < TAKES_LESS_GRACE_S):
                return False
        return True

    def share_member(self, site: SiteCapacityController, *, order: int) -> SolarShareMember | None:
        """This charger's place in its site's surplus split, or `None` when it takes no part: not on
        this site's controller, not on `solar` or `hybrid`, paused, a `hybrid` charger inside a plan
        window or short of nothing, a vehicle known to be unplugged, or no usable reading of its own
        draw. A charger left out is house load to the others.
        """
        if self._site is not site:
            return None
        settings = self._store.settings(self._charger_entry_id)
        if settings.strategy not in (STRATEGY_SOLAR, STRATEGY_HYBRID) or pause_blocks_execution(settings):
            return None
        if settings.strategy == STRATEGY_HYBRID and (
            self._controller.plan_window_active_now or self._hybrid_satisfied()
        ):
            return None
        if self._controller.adapter.vehicle_connected() is False:
            return None
        now = self._now()
        observation = _build_observation(
            site,
            self._charger_entry_id,
            now=now,
            effective_phases=effective_phases(self._hass, self._charger_entry_id),
        )
        return self._member(site, observation, order=order, now=now)

    def _member(
        self, site: SiteCapacityController, observation: SolarObservation, *, order: int, now: float
    ) -> SolarShareMember | None:
        solar = self._solar
        config = solar.config if solar is not None else self._solar_config(site)
        breakdown = surplus_breakdown(observation, config.priority)
        if breakdown is None:
            return None
        watts_per_a = len(observation.car_phases) * breakdown.mean_voltage_v
        running = solar is not None and solar.running
        takes_less = False
        if (
            running
            and solar is not None
            and solar.last_requested_a is not None
            and solar.on_since is not None
            and now - solar.on_since >= TAKES_LESS_GRACE_S
        ):
            takes_less = breakdown.car_w < (solar.last_requested_a - TAKES_LESS_MARGIN_A) * watts_per_a
        assert config.start_a is not None and config.stop_a is not None
        return SolarShareMember(
            charger_entry_id=self._charger_entry_id,
            priority=site.charger_priority(self._charger_entry_id),
            order=order,
            car_w=breakdown.car_w,
            watts_per_a=watts_per_a,
            running=running,
            start_a=config.start_a,
            stop_a=config.stop_a,
            min_current_a=config.min_current_a,
            max_current_a=config.max_current_a,
            takes_less=takes_less,
        )

    def _share_adjust_w(self, site: SiteCapacityController, observation: SolarObservation) -> float:
        """What the site's priority order moves to or from this charger this tick
        (`SolarObservation.share_adjust_w`); 0.0 when no other charger on the site takes part."""
        own: SolarShareMember | None = None
        members: list[SolarShareMember] = []
        for order, charger_entry_id in enumerate(site.config.get(CONF_CHARGER_ENTRY_IDS) or []):
            if charger_entry_id == self._charger_entry_id:
                own = self._member(site, observation, order=order, now=observation.now)
                member = own
            else:
                data = charger_data(self._hass, charger_entry_id)
                coordinator = None if data is None else data.solar
                member = None if coordinator is None else coordinator.share_member(site, order=order)
            if member is not None:
                members.append(member)
        if own is None or len(members) < 2 or self._solar is None:
            return 0.0
        breakdown = surplus_breakdown(observation, self._solar.config.priority)
        if breakdown is None:
            return 0.0
        return priority_adjust_w(self._charger_entry_id, breakdown.available_w, members)

    def _solar_config(self, site: SiteCapacityController) -> SolarConfig:
        priority = site.config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY)
        wiring: dict[str, Any] = (site.config.get(CONF_PHASE_WIRING) or {}).get(
            self._charger_entry_id
        ) or {}
        min_current_a = float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))
        config_kwargs: dict[str, Any] = {"priority": priority, "min_current_a": min_current_a}
        # A charge starts at the charger's own start minimum (a profile may set it above 6 A); once running it may still
        # go down to `min_current_a`.
        start_a = max(min_current_a, self._controller.adapter.min_start_current_a)
        if start_a > min_current_a:
            config_kwargs["start_a"] = start_a
        # No per-charger "max current for solar" setting exists; `wiring.get("max_current_a")`
        # is a hook for one, and otherwise `SolarConfig`'s default (16.0 A) applies.
        max_current_a = wiring.get("max_current_a")
        if max_current_a is not None:
            config_kwargs["max_current_a"] = float(max_current_a)
        return SolarConfig(**config_kwargs)

    def _build_controller(self, site: SiteCapacityController) -> SolarController:
        solar = SolarController(self._solar_config(site))
        if self._controller.charging:
            _adopt_running(solar, now=self._now())
        return solar

    async def _apply_verdict(self, verdict: SolarVerdict) -> None:
        if verdict.action == "start":
            assert verdict.requested_a is not None
            await self._executor.async_solar_start(int(verdict.requested_a))
        elif verdict.action == "stop":
            await self._executor.async_solar_stop()
        elif verdict.action == "set_current":
            assert verdict.requested_a is not None
            await self._executor.async_solar_set_current(int(verdict.requested_a))
        # hold: nothing to do.

    def _update_state(
        self,
        verdict: SolarVerdict,
        site: SiteCapacityController,
        *,
        held_by_plan: bool = False,
        basis: SolarBasis | None = None,
    ) -> None:
        self._state = SolarExecutionState(
            state=verdict.state,
            action=verdict.action,
            reason=verdict.reason,
            requested_a=verdict.requested_a,
            net_grid_w=verdict.net_grid_w,
            export_w=verdict.export_w,
            car_w=verdict.car_w,
            battery_w=verdict.battery_w,
            available_w=verdict.available_w,
            available_a=verdict.available_a,
            priority_effective=verdict.priority_effective,
            active_control_active=bool(site.config.get(CONF_ACTIVE_CONTROL_ENABLED, False)),
            held_by_plan=held_by_plan,
            basis=basis if basis is not None else SolarBasis(),
        )

    def _log_hybrid_handoff(self, held_by_plan: bool) -> None:
        """INFO, once per real handoff transition, under the token HYBRID."""
        if held_by_plan == self._logged_held_by_plan:
            return
        self._logged_held_by_plan = held_by_plan
        _LOGGER.debug(
            "%s charger %s: %s",
            HYBRID_LOG_TOKEN,
            self._charger_entry_id,
            "an active plan window now owns the charger; solar is held"
            if held_by_plan
            else "no active plan window; solar owns the charger again",
        )

    def _hybrid_satisfied(self) -> bool:
        """Whether the remaining need is already `<= 0` (`plan_hybrid`'s `satisfied`), read from
        the last `HybridChargerState` `auto_controller._compute` published (at most one tick
        behind), never recalculated here.
        """
        data = charger_data(self._hass, self._charger_entry_id)
        state = None if data is None else data.hybrid_state
        return state is not None and state.state == "satisfied"

    def _log_hybrid_satisfied(self, satisfied: bool) -> None:
        """INFO, once per real transition, like `_log_hybrid_handoff`."""
        if satisfied == self._logged_satisfied:
            return
        self._logged_satisfied = satisfied
        _LOGGER.debug(
            "%s charger %s: %s",
            HYBRID_LOG_TOKEN,
            self._charger_entry_id,
            "the target is already met; solar stands down"
            if satisfied
            else "the target is no longer met; solar resumes",
        )

    async def _async_recalculate_hybrid_preview(self) -> None:
        """Reuse this coordinator's fast tick to trigger hybrid replanning: nothing else notifies the
        Auto preview when delivered energy or the forecast changes.

        Unconditional: deciding here whether a replan trigger fired would duplicate
        `hybrid_needs_replan`. The recomputation is cheap and the executor skips unchanged plans, so
        recalculating every tick is safe.
        """
        preview = preview_for(self._hass, self._charger_entry_id)
        if preview is not None:
            await preview.async_recalculate()

    def _log_transition(self, verdict: SolarVerdict) -> None:
        """INFO, once per actual change, under the `SOLAR_SURPLUS` token (this runs on every site
        recompute, far more often than the state changes).
        """
        if self._born_at is None:
            self._born_at = self._now()
        signature: tuple[Any, ...] = (verdict.state, verdict.action, verdict.reason)
        if signature == self._logged:
            return
        warn_missing = verdict.reason == "charger_measurement_missing" and not self._warned_missing
        if warn_missing and self._now() - self._born_at < MEASUREMENT_WARNING_GRACE_S:
            # Still starting up: say nothing and remember nothing, so the next tick after the grace
            # warns if the measurement is still missing.
            return
        self._logged = signature
        if warn_missing:
            self._warned_missing = True
            _LOGGER.warning(
                "Solar on charger %s runs blind: the charger's own measured current is missing, so a charge "
                "starts only at the minimum current when the export covers it, and stays there only while "
                "the grid shows no real import (it is stopped when it does). Set the "
                "charger's measured current source in the site wiring.",
                self._charger_entry_id,
            )
        _LOGGER.debug(
            "%s charger %s: state=%s action=%s reason=%s requested_a=%s available_a=%s "
            "available_w=%s net_grid_w=%s car_w=%s battery_w=%s priority=%s",
            SOLAR_SURPLUS_LOG_TOKEN,
            self._charger_entry_id,
            verdict.state,
            verdict.action,
            verdict.reason,
            verdict.requested_a,
            verdict.available_a,
            verdict.available_w,
            verdict.net_grid_w,
            verdict.car_w,
            verdict.battery_w,
            verdict.priority_effective,
        )


def async_setup_solar_execution(
    hass: HomeAssistant,
    charger_entry_id: str,
    controller: ChargingController,
    executor: AutoExecutor,
    store: AutoSettingsStore,
) -> SolarExecutionCoordinator:
    """Build and start one charger's `SolarExecutionCoordinator`; the entry stops it on unload.

    Synchronous: `async_start` only subscribes and schedules its first evaluation.
    """
    coordinator = SolarExecutionCoordinator(hass, charger_entry_id, controller, executor, store)
    coordinator.async_start()
    return coordinator


def async_rebind_solar_execution(hass: HomeAssistant, charger_entry_ids: list[str]) -> None:
    """(Re)bind every loaded coordinator among `charger_entry_ids` to its site, right now.

    See `SolarExecutionCoordinator.async_start` for why. Ids with no coordinator are skipped.
    """
    for charger_entry_id in charger_entry_ids:
        data = charger_data(hass, charger_entry_id)
        if data is not None and data.solar is not None:
            data.solar.async_start()
