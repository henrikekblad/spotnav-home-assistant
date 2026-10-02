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
  `phase_liveness` (usable only when `fresh` or `confirmed_unchanged`). The car's delivered current
  comes from `SiteCapacityController.charger_measured_current`. The battery comes from
  `battery_aggregate_power`, usable only when fresh, with `battery_configured` set from whether an
  aggregate entity exists, never inferred from the reading itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.core import callback, HomeAssistant

from ..const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_PHASE_WIRING,
    CONF_SOLAR_PRIORITY,
    DEFAULT_MIN_CURRENT_A,
    DEFAULT_SOLAR_PRIORITY,
    DOMAIN,
    ENTRY_TYPE_SITE,
)
from ..planning.auto_settings import AutoSettingsStore, STRATEGY_HYBRID, STRATEGY_SOLAR
from ..runtime import charger_data, preview_for, site_controller_for
from ..site.site_capacity import PhaseName, PHASES
from ..site.site_capacity_controller import SiteCapacityController
from ..site.solar_surplus import SolarConfig, SolarController, SolarObservation, SolarVerdict
from .auto_execution import AutoExecutor
from .controller import ChargingController


_LOGGER = logging.getLogger(__name__)

#: Greppable token carried by every solar start/stop/state-transition log line.
SOLAR_SURPLUS_LOG_TOKEN: Final = "SOLAR_SURPLUS"

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
    """Whether this charger's site can run solar execution at all: a loaded site in
    `derived_phase_current` mode, the one that produces the signed power and voltage readings
    `_build_observation` needs.
    """
    site = site_controller_for_charger(hass, charger_entry_id)
    if site is None:
        return False
    return site.config.get("measurement_mode") == "derived_phase_current"


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


def _car_phases(site: SiteCapacityController, charger_entry_id: str) -> tuple[PhaseName, ...]:
    """This charger's wired phases from the site's `CONF_PHASE_WIRING` (as
    `SiteCapacityController._build_requests` reads them). Empty when a single-phase
    charger's phase is unknown, which `site/solar_surplus.py` treats as no basis.
    """
    wiring: dict[str, Any] = (site.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}
    if wiring.get("phases", 3) == 3:
        return PHASES
    phase = wiring.get("phase")
    return (phase,) if phase else ()


def _build_observation(
    site: SiteCapacityController, charger_entry_id: str, *, now: float
) -> SolarObservation:
    """One tick's `SolarObservation` from the site's computed result plus this charger's
    measured current.
    """
    result = site.result
    signed_grid_w: dict[PhaseName, float | None] = {
        phase: _liveness_gated(result.phase_liveness, result.phase_signed_active_power_diagnostic_w, phase)
        for phase in PHASES
    }
    voltage_v: dict[PhaseName, float | None] = {
        phase: _liveness_gated(result.phase_liveness, result.phase_voltage_v, phase) for phase in PHASES
    }

    car_phases = _car_phases(site, charger_entry_id)
    measured = site.charger_measured_current(charger_entry_id)
    car_delivered_a: dict[PhaseName, float | None] = {
        phase: (None if measured is None else measured.get(phase).value) for phase in car_phases
    }

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
        self._solar: SolarController | None = None
        self._site: SiteCapacityController | None = None
        self._site_unsub: Callable[[], None] | None = None
        self._state: SolarExecutionState | None = None
        self._logged: tuple[Any, ...] | None = None
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
        observation = _build_observation(site, self._charger_entry_id, now=self._now())
        verdict = self._solar.observe(observation)
        held_by_plan = settings.strategy == STRATEGY_HYBRID and self._controller.plan_window_active_now
        if held_by_plan:
            # Arbitration: the state machine saw this observation (its timers keep ticking) but its
            # verdict is not carried out while a plan window owns the charger.
            pass
        else:
            await self._apply_verdict(verdict)
        self._update_state(verdict, site, held_by_plan=held_by_plan)
        self._log_transition(verdict)
        self._log_hybrid_handoff(held_by_plan)
        if settings.strategy == STRATEGY_HYBRID:
            await self._async_recalculate_hybrid_preview()
        # This method is asynchronous (`_apply_verdict` awaits the executor's lock), so it finishes
        # after the site's own notify already rendered the previous `solar_surplus_snapshot`; this
        # asks the site to render again (see
        # `SiteCapacityController.notify_solar_surplus_changed`).
        site.notify_solar_surplus_changed()

    def _build_controller(self, site: SiteCapacityController) -> SolarController:
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
        solar = SolarController(SolarConfig(**config_kwargs))
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
        self, verdict: SolarVerdict, site: SiteCapacityController, *, held_by_plan: bool = False
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
        signature: tuple[Any, ...] = (verdict.state, verdict.action, verdict.reason)
        if signature == self._logged:
            return
        self._logged = signature
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
