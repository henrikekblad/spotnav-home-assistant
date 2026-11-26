"""The caps a grid charge and an export run under: the HA-down cap from the main fuse, the learned house peak, the cars
and a heat raise, the plan's cap and its rule, the learning hold, and the export's cap."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from ...const import CONF_CHARGER_ENTRY_IDS, CONF_MAIN_FUSE_A
from ...core.electric import phase_volts
from ...planning.grid_voltage import stored_voltage_between_phases_v
from ...site.phase_peaks import MIN_READY_DAYS, Learned
from .. import gate
from ..commands import plant_phases, BrandControl, control_for
from ..executor import HEADROOM_MARGIN_A
from ..outside_control import RESERVE_MARGIN_PCT
from ..plan_model import ACTION_EXPORT_DISCHARGE, ACTION_GRID_CHARGE
from ..runtime import read_plant_entity
from .const import HA_DOWN_OFFER_GAIN_KW, HEAT_RAISE_KW, RULE_CHARGE_POWER_ENTITY, RULE_NO_GRID_CHARGE
from .helpers import _accept_applies, _float

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class Caps:
    """The HA-down caps. A mixin of `BatteryControl`, on its state."""

    def _main_fuse_a(self) -> float | None:
        fuse_a = self.site.config.get(CONF_MAIN_FUSE_A)
        return float(fuse_a) if isinstance(fuse_a, (int, float)) and not isinstance(fuse_a, bool) else None

    def _grid_charge_cap_kw(
        self, now: datetime, options: gate.Options, *, peaks: Mapping[str, float] | None | bool = True,
        held_off: bool = False, no_cars: bool = False,
    ) -> float | None:
        """The HA-down grid charge cap (`gate.grid_charge_cap_kw`) on the site's readings now. `peaks`: the house's
        per-phase peaks to reckon with instead of the ones in use now (`_phase_peaks`), `None` for the conservative
        rule; for the status, which says what the cap would be either way. `held_off`: a person's hardware test
        runs, and the chargers its hold keeps stopped count as it says (`_cars_allowed_a`). `no_cars`: the chargers
        and SpotNav's own heat counted as drawing nothing, for the status's offer only (`_ha_down_status`). The
        person's acceptance of the HA-down risk is the options' (`accept_ha_down`)."""
        if peaks is True:
            peaks = self._phase_peaks(now)
        control = control_for(self.facts.control_brand)
        guards = options.import_limit_acts and self._import_limit_refusal() is None
        # Held per phase: every reading the recipe's fact needs, read again now (each start and each look).
        per_phase = self._import_limit_phase_refusal() is None
        import_limit_kw = self._import_limit_kw(control) if guards or per_phase else None
        return gate.grid_charge_cap_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config), margin_a=HEADROOM_MARGIN_A,
            cars_a=0.0 if no_cars else self._cars_allowed_a(held_off=held_off) + self._heat_standing_a(),
            house_peak_kw=self._house_peak_kw(now), plant_phases=self._plant_phases(control), import_limit_kw=import_limit_kw,
            import_limit_acts=options.import_limit_acts, house_phase_peaks_a=peaks or None,
            import_limit_per_phase=per_phase,
            # The per-phase rule and the accepted HA-down risk: the site's own safety margin as the person set it.
            phase_margin_a=self._site_margin_a(),
            accept_ha_down=options.accept_ha_down and _accept_applies(control),
            expiry_lifts=self._expiry_lifts(control, phased=False),
            own_import_a=self._own_import_a(control),
        )

    def _own_import_a(self, control: BrandControl | None, *, live: bool = False) -> float:
        """What the plant may import by itself on each phase it spreads that over (A, `commands.AutonomousImport`):
        read from its entity where it has one, else the recipe's figure; neither known: infinite (no grid charge, no
        hold start). For the HA-down caps whatever the trigger (the SoC drifts while Home Assistant is down); `live`
        (the executor's room now): only while the trigger may fire (`below_reserve`: the SoC within
        `outside_control.RESERVE_MARGIN_PCT` of the reserve, or not read; so too for the HA-down caps where the recipe says
        its top-up can only start that low, `AutonomousImport.near_reserve_only`). 0 for a recipe that declares none."""
        spec = None if control is None else control.autonomous_import
        if control is None or spec is None:
            return 0.0
        if (live or spec.near_reserve_only) and spec.trigger == "below_reserve":
            soc = self.facts.soc_pct
            reserve = _float(read_plant_entity(self.hass, self.site.config, spec.reserve)) if spec.reserve else None
            reserve = reserve if reserve is not None else self.facts.reserve_soc_pct
            if soc is not None and reserve is not None and soc > reserve + RESERVE_MARGIN_PCT:
                return 0.0
        kw: float | None = None
        if spec.entity is not None:
            read = _float(read_plant_entity(self.hass, self.site.config, spec.entity))
            kw = None if read is None else read * spec.scale
        if kw is None:
            kw = spec.kw
        if kw is None or not math.isfinite(kw):
            return math.inf
        phases = min(max(int(spec.phases or self._plant_phases(control)), 1), 3)
        return max(kw, 0.0) * 1000.0 / (phases * phase_volts(stored_voltage_between_phases_v(self.site.config)))

    def _learned(self, now: datetime) -> Learned | None:
        """The site's learned per-phase house peaks (`site/phase_peaks.py`), `None` when it keeps none."""
        peaks = getattr(self.site, "phase_peaks", None)
        if peaks is None:
            return None
        try:
            return peaks.learned(now.timestamp())
        except Exception:  # noqa: BLE001 - without them the conservative rule counts
            self._look_failed("phase_peaks")
            return None

    def _phase_peaks(self, now: datetime) -> dict[str, float] | None:
        """The per-phase peaks the grid charge cap uses, with the further margin of the days counted so far
        (`Learned.phase_peaks`); `None` while too few days count (the conservative rule)."""
        learned = self._learned(now)
        return None if learned is None else learned.phase_peaks()

    def _cap_status(self, now: datetime, options: gate.Options) -> dict[str, Any]:
        """The grid charge cap and what it rests on, for the status and the diagnostics. `rule`: `learned` (the
        house's per-phase peaks, the learning done), `learning` (those peaks with the further margin of the days
        counted so far), `conservative` (too few days: 4 kW on one phase), `import_limit` (no cap of this kind),
        `accepted` (the person accepted the HA-down risk: the fuse with an empty house, `gate.ha_down_accepted_cap_kw`)
        or `error`. `cap_conservative_kw` and `cap_learned_kw` say what the HA-down cap would be by the conservative
        rule and by the learned peaks without the further margin, the acceptance left out (`None` where there is no
        cap of this kind, or no peaks). `import_limit`: the plant's own import limit against the most that guards the
        fuse (`_import_limit_status`). `ha_down`: the acceptance's offer (`_ha_down_status`)."""
        learned = self._learned(now)
        without: float | None = None
        done: float | None = None
        import_limit: dict[str, Any] | None = None
        # What the HA-down rule itself gives, whether or not the person accepted the risk.
        plain = replace(options, accept_ha_down=False)
        try:
            import_limit = self._import_limit_status(now, options)
            cap: float | None = self._grid_charge_cap_kw(now, options)
            if cap is not None:
                without = self._grid_charge_cap_kw(now, plain, peaks=None)
                if learned is not None and learned.peaks_a is not None:
                    done = self._grid_charge_cap_kw(
                        now, plain, peaks=dict(zip(gate.PHASE_NAMES, learned.peaks_a, strict=True))
                    )
            failed = False
        except Exception:  # noqa: BLE001 - the status must never fail on it
            _LOGGER.debug("cap_status: ignored failure (the status must never fail on it)", exc_info=True)
            cap, failed = None, True
        return {
            "cap_kw": None if cap is None else round(cap, 2),
            "rule": self._cap_rule(options, cap, failed, learned),
            "main_fuse_a": self._main_fuse_a(),
            "cap_conservative_kw": None if without is None else round(without, 2),
            "cap_learned_kw": None if done is None else round(done, 2),
            "learned": None if learned is None else learned.as_dict(),
            "import_limit": import_limit,
            "ha_down": self._ha_down_status(now, options),
        }

    def _cap_rule(self, options: gate.Options, cap: float | None, failed: bool, learned: Any) -> str:
        """Which rule a grid charge cap rests on (`_cap_status`'s `rule`)."""
        if failed:
            return "error"
        if cap is None:
            return "import_limit"
        if options.accept_ha_down and _accept_applies(control_for(self.facts.control_brand)):
            return "accepted"
        usable = learned is not None and learned.phase_peaks() is not None
        return "conservative" if not usable else "learned" if learned is not None and learned.ready else "learning"

    def plan_grid_charge_cap(self, now: datetime) -> tuple[float | None, str]:
        """The grid charge cap the battery plan plans under, `(cap_kw, rule)`: the one control obeys right now
        (`_charge_limits`, per phase where it runs; read as the executor is, never changing it), so the plan and the
        command can never disagree. `cap_kw` `None` with the rule `import_limit` is no cap of this kind (the plant's
        own import limit guards the fuse); a cap that cannot be worked out is 0 with the rule `error`, never no cap."""
        try:
            control = control_for(self.facts.control_brand)
            if control is not None and not control.supports(ACTION_GRID_CHARGE):
                return 0.0, RULE_NO_GRID_CHARGE  # a limits-only recipe: control never charges from the grid
            options = self.options()
            executor = self.executor
            phased = executor is not None and executor.control.phased
            cap, caps = self._charge_limits(now, options, phased=phased)
            rule = self._cap_rule(options, cap, False, self._learned(now))
            # Never more than the plant's own charge power entity takes, as `_control_slot`: its own rule where it
            # is what holds the charge.
            bounded = self._within_charge_power(cap)
            if bounded is not None and (cap is None or bounded < cap):
                cap, rule = bounded, RULE_CHARGE_POWER_ENTITY
            # Where control runs no grid charge at all, the plan plans none: as `_control_slot` and `gate.restricted`.
            if caps is None and gate.charge_by_phases(self.control_report()):
                return 0.0, RULE_NO_GRID_CHARGE  # the charge was shown only per phase: the balanced one is never run
            fixed = self._fixed_charge_kw()
            if fixed is not None and (not math.isfinite(fixed) or (cap is not None and fixed > cap)):
                return 0.0, RULE_NO_GRID_CHARGE  # a plant that charges at its own rate: as planned or not at all
            if cap is not None and cap < gate.MIN_RATE_KW:
                return 0.0, RULE_NO_GRID_CHARGE  # under the least rate control commands it does self-consumption
            return cap, rule
        except Exception:  # noqa: BLE001 - an unknown cap allows no grid charge
            _LOGGER.debug("plan_grid_charge_cap: ignored failure (no grid charge is planned)", exc_info=True)
            return 0.0, "error"

    def _ha_down_status(self, now: datetime, options: gate.Options) -> dict[str, Any]:
        """Whether accepting the HA-down risk (`battery_control_accept_ha_down`) would change anything here, for the
        card's administrator-only line: `offered` (battery control is on, the plant's recipe can charge from the
        grid, neither import-limit rule holds, no recipe's own dead-man stands in, and the HA-down cap with no car or
        heat counted, per phase where a charge per phase runs, leaves at least `HA_DOWN_OFFER_GAIN_KW` less than the
        fuse allows with an empty house, at most the battery's rated charge), `held_kw` (that HA-down cap) and
        `empty_house_kw` (what the fuse allows with an empty house). Read the same whether or not it is accepted, so
        the line stays while it changes something; never raises (`offered` false)."""
        block: dict[str, Any] = {"offered": False, "held_kw": None, "empty_house_kw": None}
        try:
            control = control_for(self.facts.control_brand)
            if control is None or not control.supports(ACTION_GRID_CHARGE) or not _accept_applies(control):
                return block
            # A command's own measured expiry stands in for the cap: nothing to accept.
            if self._expiry_lifts(control, phased=True) or self._expiry_lifts(control, phased=False):
                return block
            plain = replace(options, accept_ha_down=False)
            phase_path = self._phase_path_open()
            executor = self.executor
            held = self._charge_limits(
                now, plain, phased=executor is not None and executor.control.phased, no_cars=True
            )[0]
            if held is None:
                return block
            empty = gate.ha_down_accepted_cap_kw(
                self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
                margin_a=self._site_margin_a(), plant_phases=self._plant_phases(control),
                own_import_a=self._own_import_a(control),
            )
            if executor is not None and executor.control.phased and self._phased_wanted():
                # A charge per phase may use each phase's fuse less the margin.
                empty = max(empty, sum(self._phase_caps_kw(now, accept=True, no_cars=True)))
            rated = self.facts.max_charge_kw
            most = empty if rated is None or not math.isfinite(rated) or rated <= 0 else min(empty, rated)
            block.update(held_kw=round(held, 2), empty_house_kw=round(empty, 2))
            # Where the plant's own per-phase hold can guard the fuse, that is the way: no risk is offered instead.
            block["offered"] = options.enabled and not phase_path and most - held >= HA_DOWN_OFFER_GAIN_KW
        except Exception:  # noqa: BLE001 - the status must never fail on it
            self._look_failed("ha_down_status")
            block["offered"] = False
        return block

    def learning_hold(self, now: datetime) -> dict[str, Any] | None:
        """The learning of the house's per-phase peaks while it holds the grid charge back, for the house card's status
        line: `{what: "phase_peaks", days_left, cap_kw}` (`cap_kw` 0 when no grid charge can run), `None` when it
        holds nothing back: grid charging not allowed, the learning done, the plant's import limit guarding the
        fuse, the cap of the days counted so far (the conservative rule, or the learned peaks with their further
        margin) still at least the battery's own rated charge, or that further margin costing nothing (the cap the
        same without it: the house itself is what leaves no more). `days_left`: to the next step, the first usable
        days while the conservative rule counts, the end of the learning after that."""
        self._look_id += 1
        options = self.options()
        if ACTION_GRID_CHARGE not in self.allowed(options):
            return None
        learned = self._learned(now)
        status = self._cap_status(now, options)
        cap = status["cap_kw"]
        if learned is None or learned.ready or status["rule"] not in ("conservative", "learning") or cap is None:
            return None
        # A charge per phase runs under the phases' own caps, which may leave more than the balanced one. Read as the
        # executor's control is now: a status read changes nothing.
        executor = self.executor
        effective = self._charge_limits(now, options, phased=executor is not None and executor.control.phased)[0]
        cap = cap if effective is None else max(cap, round(effective, 2))
        rated = self.facts.max_charge_kw
        if rated is not None and cap >= rated:
            return None
        days_left = learned.days_left
        if status["rule"] == "conservative":
            days_left = max(MIN_READY_DAYS - learned.days_spread, 1)
        elif status["cap_learned_kw"] is not None and cap >= status["cap_learned_kw"]:
            return None
        return {
            "what": "phase_peaks",
            "days_left": days_left,
            "cap_kw": 0.0 if cap < gate.MIN_RATE_KW else cap,
        }

    def _export_cap_kw(self) -> float:
        """The fuse rule on the export side (`gate.export_discharge_cap_kw`) on the site's phases now."""
        volts = stored_voltage_between_phases_v(self.site.config)
        exports = self._phase_export_a(volts)
        control = control_for(self.facts.control_brand)
        phases = self._plant_phases(control)
        executor = self.executor
        share_a = 0.0
        battery_kw = self.facts.power_kw
        if (
            executor is not None and executor.state.phase == "active"
            and executor.state.action == ACTION_EXPORT_DISCHARGE and battery_kw is not None and battery_kw < 0
        ):
            # What the battery measurably discharges, at most what it was told: never more of the export than its own.
            share_a = min(executor.state.rate_kw, -battery_kw) * 1000.0 / (phases * phase_volts(volts))
        return gate.export_discharge_cap_kw(
            self._main_fuse_a(), volts, margin_a=HEADROOM_MARGIN_A, phase_export_a=exports, battery_share_a=share_a,
            plant_phases=phases,
        )

    def _phase_export_a(self, volts_between_phases: float) -> list[float] | None:
        """Each phase's export now (A, 0 where it imports), `None` when a phase is not known."""
        signed = list(self.site.result.phase_signed_active_power_w.values())
        if not signed or not all(value is not None and math.isfinite(value) for value in signed):
            return None
        volts = phase_volts(volts_between_phases)
        return [max(-float(value), 0.0) / volts for value in signed]  # type: ignore[arg-type]

    def _cars_allowed_a(self, *, without: str | None = None, held_off: bool = False) -> float:
        """What the site's chargers may draw per phase if nothing writes them again (A, summed): each one's last
        current (`charger_allowed_a`), `ASSUMED_CURRENT_A` when it is not known; infinite (no grid charge) when the
        chargers cannot be read. `without`: a charger left out (`import_limit_guards`).

        `held_off` (only while a person's hardware test runs, `_async_verify`): a charger the test's hold keeps
        stopped counts 0 A where it cannot draw with Home Assistant gone (`ChargerTestHold.counts_nothing`: the hold
        on, and either an OCPP charger out of service by its own report and not charging, or the charger read
        stopped with its stop a stored setting that holds every later session off). Every other charger keeps its
        current, an Easee and one on buttons among them: their stop ends the session only, and the next plug-in may
        charge with nobody there to stop it."""
        from ...execution.controller import ASSUMED_CURRENT_A  # noqa: PLC0415 - the charger side imports the site

        hold = getattr(self.site, "charger_test_hold", None) if held_off and self._hw_run else None
        try:
            total = 0.0
            for charger_entry_id in self.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                if charger_entry_id == without:
                    continue
                if hold is not None and hold.counts_nothing(charger_entry_id):
                    continue
                amps = self.site.charger_allowed_a(charger_entry_id)
                total += float(ASSUMED_CURRENT_A) if amps is None else max(amps, 0.0)
        except Exception:  # noqa: BLE001 - unknown: no grid charge on the HA-down rule
            self._look_failed("cars_allowed")
            return math.inf
        return total

    def _heat_standing_a(self) -> float:
        """What SpotNav itself adds to the house and leaves standing if Home Assistant stops (A, on each phase): a
        hot-water boost SpotNav or the button started that may run (`HotWater.standing_boost_kw`, its learned power,
        all on one phase as its worst case) and a raise held (`HEAT_RAISE_KW`). The learned peak holds them only
        as far as the window saw them; after some weeks of live days they are counted twice, the safe side.
        Infinite (no grid charge) when it cannot be read."""
        try:
            kw = 0.0
            shadow = getattr(self.site, "heat_shadow", None)
            hot_water = None if shadow is None else getattr(shadow, "hot_water", None)
            if hot_water is not None:
                kw += hot_water.standing_boost_kw()
            heat = getattr(self.site, "heat_control", None)
            if heat is not None and heat.raised:
                kw += HEAT_RAISE_KW
        except Exception:  # noqa: BLE001 - unknown: no grid charge on the HA-down rule
            self._look_failed("heat_standing")
            return math.inf
        if kw <= 0:
            return 0.0
        return kw * 1000.0 / phase_volts(stored_voltage_between_phases_v(self.site.config))

    def _plant_phases(self, control: BrandControl | None) -> int:
        """The phases the plant spreads a grid charge over: its Output Type (Solis: the inverter's own type) when
        it shows one, else the brand's (`commands.plant_phases`)."""
        if control is None:
            return 1
        state = read_plant_entity(self.hass, self.site.config, control.output_type)
        return plant_phases(control, state)

    def _house_peak_kw(self, now: datetime) -> float | None:
        """The house forecast's p90 for the running hour (kW), read once an hour; `None` when it has none."""
        hour = now.astimezone(dt_util.UTC).replace(minute=0, second=0, microsecond=0)
        if self._peak is not None and self._peak[0] == hour:
            return self._peak[1]
        value: float | None = None
        try:
            view = self.site.house_forecast.band_view(now)
            for row in view.get("hours") or []:
                if row[0] == hour.isoformat() and isinstance(row[3], (int, float)) and math.isfinite(row[3]):
                    value = float(row[3])
                    break
        except Exception:  # noqa: BLE001 - without a band the fixed margin counts
            _LOGGER.debug("SpotNav battery control: no house p90 for the hour", exc_info=True)
        self._peak = (hour, value)
        return value

    def _held_cars_have_room(self, now: datetime, held: list[str]) -> bool:
        """Whether every charger load balancing holds back has a departure with room (`gate.departure_has_room`): its
        remaining energy need against the time left at its planned power. The need is what its plan still expects
        before the departure plus what the running window is already behind (`import_target_shortfall_kwh`), as the
        site's import target reckons a departure (`ImportTargetHold._target_shortfall`)."""
        if not held:
            return False
        from ...runtime import controller_for, preview_for  # noqa: PLC0415 - the charger side imports the site
        from ...site.import_target import member_w, plan_remaining_kwh  # noqa: PLC0415

        for charger_entry_id in held:
            charger = controller_for(self.hass, charger_entry_id)
            plan = None if charger is None else getattr(charger, "plan", None)
            if plan is None or not plan.departure or not getattr(plan, "auto_owned", False):
                return False
            departure = dt_util.parse_datetime(plan.departure)
            if departure is None or departure.tzinfo is None:
                return False
            windows = plan.windows
            window_h = sum(max(0.0, (end - start).total_seconds()) for start, end in windows) / 3600.0
            if plan.energy_kwh and window_h > 0:
                power_kw = plan.energy_kwh / window_h
            else:
                power_kw = plan.power_kw if plan.power_kw else member_w(plan.phases, plan.amps) / 1000.0
            need = plan_remaining_kwh(windows, power_kw, now, departure)
            preview = preview_for(self.hass, charger_entry_id)
            if preview is not None:
                need += max(preview.import_target_shortfall_kwh(), 0.0)
            if not gate.departure_has_room(departure, now, need, power_kw):
                return False
        return True
