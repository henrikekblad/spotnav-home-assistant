"""What a look reads for the executors: the executor of each instance, the plant's facts and role reads (a stored entity
where the registry lookup fails), and the inputs of a look, of a hand-back and of the export limit."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from ...const import CONF_CHARGER_ENTRY_IDS, SOLAR_PRIORITY_CAR_FIRST
from ...site.priority import solar_priority
from ...planning.grid_voltage import stored_voltage_between_phases_v
from ...site.measurement_problem import UNHEALTHY_STATES
from .. import gate
from ..commands import ACTION_CURTAIL_EXPORT, BrandControl, Phases, control_for
from ..executor import ExecInputs, Executor, RoleRead
from ..export_curtail import curtails
from ..facts import BatteryFacts
from ..plan_model import ACTION_GRID_CHARGE, ACTION_HOLD, ACTION_SELF_CONSUME
from ..runtime import read_battery_facts, read_export_facts, read_role_reads
from .const import ACTING_GRACE_S, _UNSET
from .helpers import _state_of
from .io import stored_reads

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class Inputs:
    """The reads and an executor's inputs. A mixin of `BatteryControl`, on its state."""

    # ---- reads

    def _executor(self) -> Executor | None:
        if self.executor is None:
            self.facts = self._read_facts()
            control = control_for(self.facts.control_brand)
            if control is None:
                return None
            self.executor = Executor(control, watchdog_s=self._watchdog_s)
        return self.executor

    def _battery_power(self) -> tuple[float | None, float | None]:
        reading = self.site.battery_aggregate_power()
        if reading is None or reading.value is None:
            return None, None
        ages = [age for age in (reading.age_s, reading.report_age_s) if age is not None]
        return reading.value / 1000.0, min(ages) if ages else None

    def _read_facts(self) -> BatteryFacts:
        power_kw, _age = self._battery_power()
        return self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw, self.found_states()))

    def _with_outside(self, facts: BatteryFacts) -> BatteryFacts:
        """A battery the site's outside-control watch marks is another system's: every look refuses it."""
        battery_shadow = getattr(self.site, "battery_shadow", None)
        return facts if battery_shadow is None else battery_shadow.with_outside(facts)

    def found_states(self) -> dict[str, str]:
        """The states the battery's executor found before it first wrote, by entity id (`Executor.found_states`)."""
        executor = self.executor
        return {} if executor is None else executor.found_states()

    def acting(self) -> bool:
        """Whether this path acts on the battery or the export limit now (its writes are no one else's), and for
        `ACTING_GRACE_S` after a hand-back: a plant's sensors can show SpotNav's own last writes a poll or two late."""
        now = dt_util.now()
        for executor in (self.executor, self.export_executor):
            if executor is None:
                continue
            state = executor.state
            if state.phase in ("active", "restoring"):
                return True
            if state.released_at is not None and 0 <= (now - state.released_at).total_seconds() < ACTING_GRACE_S:
                return True
        return False

    def _export_executor(self) -> Executor | None:
        if self.export_executor is None:
            control = read_export_facts(self.hass, self.site.config)[2]
            if control is None:
                return None
            self.export_executor = Executor(control, watchdog_s=self._watchdog_s)
        return self.export_executor

    def _sell_now(self, now: datetime) -> float | None:
        if self._sell_source is not None:
            return self._sell_source(now)
        return self.site.battery_shadow.plan.sell_at(now)

    def export_inputs(
        self, now: datetime, control: BrandControl, *, stopping: str | None = None, action: str | None = None
    ) -> ExecInputs:
        """The export limit's look: the plant's facts and control, the site's measurement, the action (`action`, or
        from the sell price now), and the battery path's writes today (the cap is shared)."""
        facts, reads, _control = read_export_facts(self.hass, self.site.config)
        executor = self.export_executor
        if executor is not None:
            for name, entity_id in executor.state.entities.items():
                if name not in reads:
                    state = self.hass.states.get(entity_id)
                    reads[name] = RoleRead(entity_id=entity_id, state=_state_of(executor.control, name, state),
                                           found=state is not None, enabled=state is not None,
                                           device_id=executor.state.device_id)
        result = self.site.result
        headrooms = [value for value in result.phase_headroom_a.values() if value is not None]
        battery = self.executor
        # SpotNav's own battery control on the same plant: its Remote EMS is not the plant's own management, also
        # while it hands back (Remote EMS goes off last) or keeps a snapshot it could not restore yet.
        sibling_active = battery is not None and battery.state.brand == control.brand and (
            battery.state.phase in ("active", "restoring") or battery.state.holds_snapshot
        )
        if action is None:
            action = ACTION_CURTAIL_EXPORT if curtails(self._sell_now(now)) else ACTION_SELF_CONSUME
        return ExecInputs(
            now=now,
            facts=self._with_outside(facts),
            reads=reads,
            action=action,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            volts_between_phases=stored_voltage_between_phases_v(self.site.config),
            stopping=stopping,
            failed=self._export_failed,
            timed_out=self._export_timed_out,
            other_writes_today=0 if battery is None or battery.state.day != now.date().isoformat()
            else battery.state.writes_today,
            grid_import_kw=self.export.grid_kw(),
            sibling_active=sibling_active,
            takeover=self.options().ai_takeover_ack,
            excused_import_kw=self.export.excused_kw(),
        )

    def _grid_import_kw(self) -> float | None:
        """The site's grid power (kW, + import) from its signed phase powers, `None` when one is not known."""
        signed = list(self.site.result.phase_signed_active_power_w.values())
        if not signed or not all(value is not None and math.isfinite(value) for value in signed):
            return None
        return sum(signed) / 1000.0  # type: ignore[arg-type]

    def _reads(self, control: BrandControl) -> dict[str, RoleRead]:
        reads = read_role_reads(self.hass, self.site.config, control)
        executor = self.executor
        if executor is not None:
            # After a restart the stored entities are read even if the registry lookup fails.
            for name, entity_id in executor.state.entities.items():
                if name not in reads:
                    state = self.hass.states.get(entity_id)
                    reads[name] = RoleRead(entity_id=entity_id, state=_state_of(executor.control, name, state),
                                           found=state is not None, enabled=state is not None,
                                           device_id=executor.state.device_id)
        return reads

    def inputs(
        self, now: datetime, control: BrandControl, *, stopping: str | None = None, slot: Any = _UNSET,
    ) -> ExecInputs:
        """The battery's look with `slot` as the action (by default what control runs now)."""
        power_kw, age_s = self._battery_power()
        self.facts = facts = self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw, self.found_states()))
        result = self.site.result
        headrooms = [value for value in result.phase_headroom_a.values() if value is not None]
        try:
            grid_import_kw = self._grid_import_kw()
        except Exception:  # noqa: BLE001 - unknown: the grid charge then counts none of its own import back
            self._look_failed("grid_import")
            grid_import_kw = None
        # What the house needs beyond the sun now (kW): the grid's import less what the battery takes from it, for
        # judging a discharge to the house.
        house_deficit_kw = (
            None if grid_import_kw is None or facts.power_kw is None else max(grid_import_kw - facts.power_kw, 0.0)
        )
        config = self.site.config
        options = self.options()
        if slot is _UNSET:
            slot = self._control_slot(now, options)
        car_first, car_held = self._shedding(now, config)
        phase_caps: Phases | None = None
        hold_cap = 0.0
        if slot is not None and slot[0] == ACTION_HOLD and control.hold_surplus is not None:
            # A hold that follows the sun leaves a charge limit standing, a grid charge once the sun goes: the
            # balanced grid charge's HA-down cap bounds it (none where the plant's import limit guards the fuse).
            cap, _caps = self._charge_limits(now, options, phased=False)
            hold_cap = math.inf if cap is None else cap
        if slot is not None and slot[0] == ACTION_GRID_CHARGE:
            # The cap of the control that will run it, again: a slot capped for a charge per phase is never run as
            # the balanced one above the balanced cap (the control changes only while it holds nothing).
            cap, phase_caps = self._charge_limits(now, options, phased=control.phased)
            if cap is not None and slot[1] > cap:
                slot = (slot[0], cap, slot[2]) if cap >= gate.MIN_RATE_KW else (ACTION_SELF_CONSUME, 0.0, slot[2])
                phase_caps = phase_caps if cap >= gate.MIN_RATE_KW else None
        # A recipe whose targets per phase are the grid's reads the plant's power per phase at its own point: the
        # load behind the point less the power at it.
        grid_phases = self._grid_phases()
        behind, point = grid_phases if grid_phases is not None else (None, None)
        spec = control.phase_control
        if spec is not None and spec.behind is not None and spec.point is not None:
            plant_phase_kw = None if behind is None or point is None else (
                behind[0] - point[0], behind[1] - point[1], behind[2] - point[2]
            )
        else:
            plant_phase_kw = self._plant_phase_kw(now)
        return ExecInputs(
            now=now,
            facts=facts,
            reads=self._reads(control),
            phase_caps_kw=phase_caps,
            hold_charge_cap_kw=hold_cap,
            phase_headroom_a=self._phase_headroom_a(),
            plant_phase_kw=plant_phase_kw,
            plant_phases_mapped=self._phases_mapped(),
            phase_behind_kw=behind,
            phase_point_kw=point,
            grid_phase_kw=self._grid_phase_kw() if result.state not in UNHEALTHY_STATES else None,
            action=None if slot is None else slot[0],
            rate_kw=0.0 if slot is None else slot[1],
            slot_end=None if slot is None else slot[2],
            battery_age_s=age_s,
            measurement_ok=result.state not in UNHEALTHY_STATES and result.state != "not_configured",
            headroom_a=min(headrooms) if headrooms else None,
            charge_phases=self._plant_phases(control),
            output_balanced=control.balanced_output,
            volts_between_phases=stored_voltage_between_phases_v(config),
            grid_import_kw=grid_import_kw,
            # The fast shedding order (`gate.battery_yields`): the battery's grid charge before the car, the car
            # first when its departure has room; "battery first" backs the car off first.
            car_first=car_first,
            car_held=car_held,
            house_deficit_kw=house_deficit_kw,
            stopping=stopping,
            failed=self._failed,
            timed_out=self._timed_out,
            other_writes_today=self._export_writes_today(now),
            takeover=options.ai_takeover_ack,
            own_import_a=self._own_import_a(control_for(self.facts.control_brand), live=True),
        )

    def _shedding(self, now: datetime, config: dict[str, Any]) -> tuple[bool, bool]:
        """`(car_first, car_held)` for the executor: whether the battery's grid charge yields to a held car, and
        whether load balancing holds one back now. When the chargers cannot be read: the battery yields and no car
        counts as held (the cautious side: the grid charge never takes the car's room)."""
        try:
            held = [
                charger for charger in config.get(CONF_CHARGER_ENTRY_IDS) or []
                if self.site.load_balancing_limit(charger)[0] is not None
            ]
        except Exception:  # noqa: BLE001 - a part of the site that raises must not stop the battery's look
            self._look_failed("held_cars")
            return True, False
        car_first = solar_priority(config) == SOLAR_PRIORITY_CAR_FIRST
        room = False
        if held and car_first:
            try:
                room = self._held_cars_have_room(now, held)
            except Exception:  # noqa: BLE001 - no room known: the car is not shed first
                self._look_failed("held_cars_room")
        return gate.battery_yields(car_first, room), bool(held)

    def _hand_back_inputs(self, now: datetime, executor: Executor, stopping: str | None) -> ExecInputs:
        """The battery's look for a hand-back: the full one, or the fewest reads when a part of it raises (a hand-back
        must never wait for the rest of the site)."""
        try:
            return self.inputs(now, executor.control, stopping=stopping, slot=None)
        except Exception:  # noqa: BLE001 - handed back on what can still be read
            self._look_failed("hand_back_inputs")
            return self._minimal_inputs(now, executor, stopping)

    def _minimal_inputs(self, now: datetime, executor: Executor, stopping: str | None) -> ExecInputs:
        """The fewest reads a hand-back needs: the battery's facts (the last ones when they cannot be read), the
        controls' states and the reason; the site's measurement counts as unknown."""
        try:
            power_kw, age_s = self._battery_power()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            power_kw, age_s = None, None
        try:
            self.facts = facts = self._with_outside(read_battery_facts(self.hass, self.site.config, power_kw, self.found_states()))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            facts = self.facts
        try:
            reads = self._reads(executor.control)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            reads = stored_reads(self.hass, executor)
        try:
            takeover = self.options().ai_takeover_ack
        except Exception:  # noqa: BLE001
            _LOGGER.debug("minimal_inputs: ignored failure", exc_info=True)
            takeover = False
        return ExecInputs(
            now=now, facts=facts, reads=reads, battery_age_s=age_s, measurement_ok=False, stopping=stopping,
            failed=self._failed, timed_out=self._timed_out, takeover=takeover,
        )

    def _export_hand_back_inputs(self, now: datetime, executor: Executor, stopping: str | None) -> ExecInputs:
        """The export limit's look for a hand-back (no sell price needed), the fewest reads when a part raises."""
        try:
            return self.export_inputs(now, executor.control, stopping=stopping, action=ACTION_SELF_CONSUME)
        except Exception:  # noqa: BLE001 - handed back on what can still be read
            self._look_failed("export_hand_back_inputs")
        try:
            facts, reads, _control = read_export_facts(self.hass, self.site.config)
            facts = self._with_outside(facts)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("export_hand_back_inputs: ignored failure", exc_info=True)
            facts, reads = BatteryFacts(brand=executor.state.brand), {}
        reads = {**stored_reads(self.hass, executor), **reads}
        return ExecInputs(now=now, facts=facts, reads=reads, measurement_ok=False, stopping=stopping,
                          failed=self._export_failed, timed_out=self._export_timed_out)
