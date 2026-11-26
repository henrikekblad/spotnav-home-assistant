"""The plant's own import limit as the fuse's guard while Home Assistant is down: what refuses the rule (a sun source
that is not the plant's, a limit not trusted, the phases), the limit to write, the write with its read-back, and its
status."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from ...const import (
    CONF_BATTERY_AGGREGATE_POWER_ENTITY,
    CONF_CHARGER_ENTRY_IDS,
    CONF_CURRENT_CONTROL,
    CURRENT_CONTROL_POWER,
)
from ...core.electric import stored_safety_margin_a
from ...planning.grid_voltage import stored_voltage_between_phases_v
from ...site.energy_statistics import async_energy_prefs, energy_statistic_ids
from .. import gate
from ..commands import BrandControl, control_for
from ..executor import HEADROOM_MARGIN_A
from ..plan_model import ACTION_GRID_CHARGE, ACTION_SELF_CONSUME
from ..runtime import plant_entity_id, read_plant_entity
from .const import (
    FIRMWARE_STEPS,
    IMPORT_LIMIT_READ_BACK_S,
    IMPORT_LIMIT_READ_BACK_STEP_S,
    PHASE_PATH_STEPS,
    SOURCES_REFRESH_S,
)
from .helpers import _card_refusal, _float

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class ImportLimit:
    """The plant's own import limit. A mixin of `BatteryControl`, on its state."""

    def _import_limit_kw(self, control: BrandControl | None) -> float | None:
        """What the plant's own grid import limit reads now (kW), `None` when the brand has none, it is disabled or
        it was never set. Written only by an administrator's tap (`async_set_import_limit`), lowered, never raised."""
        if control is None:
            return None
        return _float(read_plant_entity(self.hass, self.site.config, control.import_limit))

    async def _async_read_sources(self, now: datetime) -> None:
        """Read, at most every `SOURCES_REFRESH_S`, whether the site has a sun source that is not the plant's own
        (the Energy dashboard's solar statistics against the battery's integration entry). Never raises: a read that
        fails refuses the import-limit rule until the next one."""
        if self._sources is not None and 0 <= (now - self._sources[0]).total_seconds() < SOURCES_REFRESH_S:
            return
        try:
            prefs = await async_energy_prefs(self.hass)
            solar = () if prefs is None else energy_statistic_ids(prefs)[0]
            reason = self._foreign_sun(solar)
        except Exception:  # noqa: BLE001 - unknown sources guard nothing
            self._look_failed("energy_sources")
            reason = "sources_unknown"
        self._sources = (now, reason)

    def _foreign_sun(self, statistic_ids: tuple[str, ...]) -> str | None:
        """`foreign_pv` when one of the Energy dashboard's solar statistics is not an entity of the battery's own
        integration entry (another inverter, a statistic that is no entity), else `None`."""
        if not statistic_ids:
            return None
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415 - kept beside its only use

        registry = er.async_get(self.hass)
        entity_id = self.site.config.get(CONF_BATTERY_AGGREGATE_POWER_ENTITY)
        battery = registry.async_get(entity_id) if isinstance(entity_id, str) and entity_id else None
        own = None if battery is None else battery.config_entry_id
        for statistic_id in statistic_ids:
            entry = registry.async_get(statistic_id)
            if own is None or entry is None or entry.config_entry_id != own:
                return "foreign_pv"
        return None

    def _import_limit_refusal(self, *, dc_charger: bool = True, action: str = ACTION_GRID_CHARGE) -> str | None:
        """Why the plant's own import limit cannot be the fuse's guard on this site whatever it reads, as a stable
        code; `None` when it can. The rule (`gate.import_limit_max_kw`) rests on the plant seeing every source and
        spreading its charge evenly over three phases, so it is refused where the recipe does not trust the limit here
        (`_import_limit_distrust`: `not_trusted`, not shown to act in that state; `firmware_not_tested`, another
        firmware than the one it was tested on; `firmware_unread`, the firmware never read), for a
        plant that is not three-phase
        (`plant_not_three_phase`: a single-phase source), a sun source that is not the plant's (`foreign_pv`: it
        lowers the total the plant sees while one phase carries more), sources not read yet (`sources_unknown`), and
        a charger run by power through the plant (`dc_charger`: Sigenergy's DC charger, which can also put out, V2G;
        `dc_charger` false leaves that one out, for that charger's own fuse cap)."""
        control = control_for(self.facts.control_brand)
        if control is None or control.import_limit is None:
            return "not_supported"
        distrust = self._import_limit_distrust(control, action)
        if distrust is not None:
            return distrust
        if self._plant_phases(control) != 3:
            return "plant_not_three_phase"
        if self._sources is None:
            return "sources_unknown"
        if self._sources[1] is not None:
            return self._sources[1]
        if dc_charger:
            for charger_entry_id in self.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                entry = self.hass.config_entries.async_get_entry(charger_entry_id)
                if entry is not None and entry.data.get(CONF_CURRENT_CONTROL) == CURRENT_CONTROL_POWER:
                    return "dc_charger"
        return None

    def _import_limit_distrust(self, control: BrandControl, action: str = ACTION_GRID_CHARGE) -> str | None:
        """Why the recipe does not trust the plant's own import limit here (`commands.ImportLimitTrust`), `None` when it
        does: `not_trusted` (marked untrusted, or not shown to act in the state the caller relies on, `action`: the
        recipe's grid charge for the battery's two rules, the plant's own mode for a charger's lift),
        `firmware_unread` (the recipe names its firmwares and the plant's was never read: fail closed) or
        `firmware_not_tested` (read, and not one it was tested on: the field test is to be run on it). The last two
        are steps on the per-phase path (`PHASE_PATH_STEPS`, owner decision 7): no HA-down acceptance is offered
        instead."""
        trust = control.import_limit_trust
        if trust is None or trust.untrusted or action not in trust.acts_in:
            return "not_trusted"
        if trust.firmware is None:
            return None
        if not self.firmware_known():
            return "firmware_unread"
        return None if self.firmware_key().split(" | ", 1)[0] in trust.firmware else "firmware_not_tested"

    def _site_margin_a(self) -> float:
        """The site's own safety margin (A per phase) as stored now, for the per-phase import-limit rule only."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        return stored_safety_margin_a(entry.data if entry is not None else self.site.config)

    def _import_limit_phase_refusal(self, *, assume_acts: bool = False) -> str | None:
        """Why the plant's own import limit cannot be taken as held **per phase** now (`commands.ImportLimitPhases`),
        as a stable code; `None` when it can. Fails closed: anything unread refuses. `not_supported` (the recipe states
        no such hold), `not_field_tested` (the owner's on-site test is not recorded in the recipe), every reason of the
        total rule (`_import_limit_refusal`: a plant not three-phase, another inverter's sun, the sources unread, a
        charger run through the plant), `output_type_unread` (the plant's Output Type reads nothing: the brand's
        default is not taken for it), `output_type_not_supported` (it reads other than the one the switch acts with,
        `ImportLimitPhases.output_type`: Sigenergy's L1/L2/L3/N, not L1/L2/L3), `phase_control_off` (the switch
        that shares the plant's power per phase reads anything but on, or nothing) and `grid_sensor_offline` (the
        plant's grid sensor, which the limit regulates on, reads anything but connected, or nothing), and last
        `not_said_to_act` (the site's own `battery_control_import_limit_acts` is off: the recipe's fact is one plant's
        test, so each site still says that its plant's limit acts; `assume_acts` leaves it out)."""
        control = control_for(self.facts.control_brand)
        spec = None if control is None else control.import_limit_phases
        if control is None or spec is None or control.import_limit is None:
            return "not_supported"
        if not spec.field_tested:
            return "not_field_tested"
        refusal = self._import_limit_refusal()
        if refusal is not None:
            return refusal
        output = read_plant_entity(self.hass, self.site.config, control.output_type)
        if output is None:
            return "output_type_unread"
        if str(output).lower() != spec.output_type:
            return "output_type_not_supported"
        if read_plant_entity(self.hass, self.site.config, spec.switch) != "on":
            return "phase_control_off"
        if read_plant_entity(self.hass, self.site.config, spec.grid_sensor) != spec.sensor_online:
            return "grid_sensor_offline"
        # Last, so a step the site still needs (or a reason that rules the hold out) is named before the person's word.
        if not assume_acts and not self.options().import_limit_acts:
            return "not_said_to_act"
        return None

    def _phase_path_open(self) -> bool:
        """Whether the plant's own per-phase hold is a way the person can take here: the recipe has it, field-tested,
        and nothing about the site rules it out whatever the person does (another inverter's sun, a plant that is not
        three-phase, a charger run through the plant, an Output Type it does not act with). What is left for the
        person (saying it acts, setting the limit, enabling an entity, turning the phase switch on) keeps it open. Where
        it is open the HA-down acceptance is not offered: the plant can guard the fuse without accepting a risk."""
        return self._import_limit_phase_refusal(assume_acts=True) in PHASE_PATH_STEPS

    def import_limit_target_kw(self) -> float | None:
        """The value the person's tap sets the plant's import limit to (`async_set_import_limit`): the most that keeps
        every phase under the fuse less the site's own safety margin when the plant holds each phase at a third of it
        (`gate.import_limit_phase_max_kw`), rounded down to 0.1 kW; `None` when no value guards or it is under the
        least rate a grid charge runs at."""
        most = gate.import_limit_phase_max_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
            margin_a=self._site_margin_a(), cars_a=0.0,
        )
        if most is None:
            return None
        target = math.floor(most * 10.0 + 1e-9) / 10.0
        return target if target >= gate.MIN_RATE_KW else None

    def import_limit_write_kw(self) -> float | None:
        """What the administrator's tap would write to the plant's import limit now: `import_limit_target_kw` when the
        limit reads more than keeps every phase under the fuse (`gate.import_limit_phase_max_kw`, unrounded);
        `None` when it already guards (only saying it acts is left), reads nothing (never written over), no value guards
        or the per-phase hold is no way here (`_phase_path_open`). The card shows this
        figure, so what it says is what is written."""
        control = control_for(self.facts.control_brand)
        if control is None or control.import_limit is None or not self._phase_path_open():
            return None
        if self._import_limit_phase_refusal(assume_acts=True) in FIRMWARE_STEPS:
            return None  # not trusted on this firmware yet: the field test first, nothing to write
        target = self.import_limit_target_kw()
        most = gate.import_limit_phase_max_kw(
            self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
            margin_a=self._site_margin_a(), cars_a=0.0,
        )
        current = self._import_limit_kw(control)
        # A limit that reads nothing is not written over: it may hide a lower value set on purpose (a power tariff)
        # until the integration has polled it.
        if target is None or most is None or current is None or 0.0 <= current <= most + 1e-6:
            return None
        return target

    async def async_set_import_limit(self) -> str | None:
        """The administrator's one tap on the per-phase import-limit line: lower the plant's own grid import limit to
        `import_limit_target_kw` when it reads more than guards (`import_limit_write_kw`; never raised: a lower limit only makes the plant more
        careful), read it back, and only then say that it acts (`battery_control_import_limit_acts`). A limit already
        at or under the target is left as it is. Answers a refusal code, `None` when done: `not_enabled` (battery
        control is off: nothing is written to the plant), `not_read` (the limit reads nothing yet: never written over),
        `shadow_only` (a write is needed while control only watches: nothing is written to the plant; saying the limit
        acts alone is allowed, it is SpotNav's own option), `not_supported` (no
        field-tested per-phase hold here), the site's own refusals of the rule (`_import_limit_refusal`,
        `output_type_not_supported`), `no_value` (no value guards the fuse), `entity_missing` (the limit's entity is
        not there or disabled), `unit_unknown` (it is not in kW), `out_of_range` (the target is outside the entity's
        own range), `write_failed` (the integration refused) and `not_read_back` (it did not read the target back
        within `IMPORT_LIMIT_READ_BACK_S`). Every write is logged."""
        if not self.options().enabled:
            return "not_enabled"  # nothing is written to the plant while battery control is off
        control = control_for(self.facts.control_brand)
        refusal = self._import_limit_phase_refusal(assume_acts=True)
        if control is None or control.import_limit is None or refusal not in PHASE_PATH_STEPS or refusal in FIRMWARE_STEPS:
            return refusal or "not_supported"
        target = self.import_limit_target_kw()
        if target is None:
            return "no_value"
        current = self._import_limit_kw(control)
        if current is None:
            return "not_read"
        if self.import_limit_write_kw() is not None:
            if not self.options().writes:
                # Shadow only: nothing is written to the plant (the limit also steers the plant's own modes).
                return "shadow_only"
            entity_id = plant_entity_id(self.hass, self.site.config, control.import_limit)
            state = None if entity_id is None else self.hass.states.get(entity_id)
            if entity_id is None or state is None:
                return "entity_missing"
            if state.attributes.get("unit_of_measurement") != "kW":
                return "unit_unknown"
            low, high = _float(state.attributes.get("min")), _float(state.attributes.get("max"))
            if (low is not None and target < low) or (high is not None and target > high):
                return "out_of_range"
            now = dt_util.now()
            try:
                await self.hass.services.async_call(
                    "number", "set_value", {"entity_id": entity_id, "value": target}, blocking=True
                )
            except Exception:  # noqa: BLE001 - the answer says so; nothing else changes
                _LOGGER.warning("SpotNav battery: setting the plant's import limit to %s kW failed", target, exc_info=True)
                self._log([{"at": now.isoformat(), "kind": "import_limit_write_failed", "reason": str(target)}])
                return "write_failed"
            read = None
            for _ in range(max(1, int(IMPORT_LIMIT_READ_BACK_S / IMPORT_LIMIT_READ_BACK_STEP_S))):
                read = self._import_limit_kw(control)
                if read is not None and read <= target + 1e-6:
                    break
                await asyncio.sleep(IMPORT_LIMIT_READ_BACK_STEP_S)
            read = self._import_limit_kw(control)
            self._log([{"at": now.isoformat(), "kind": "import_limit_set",
                        "reason": f"{current}->{target} read {read}"}])
            _LOGGER.info("SpotNav battery: the plant's import limit set from %s to %s kW (reads %s)", current, target, read)
            if read is not None and current is not None and read > current + 1e-6:
                _LOGGER.warning("SpotNav battery: the plant's import limit reads %s kW after setting %s, more than before (%s)",
                                read, target, current)
            if read is None or read > target + 1e-6:
                return "not_read_back"
        if not self.options().import_limit_acts:
            await self.async_update_options({gate.CONF_IMPORT_LIMIT_ACTS: True})
        return None

    def import_limit_guards(self, *, without_charger: str | None = None) -> bool:
        """Whether the plant's own import limit guards the fuse **now** (`gate.import_limit_holds`), for a charger
        that runs through the plant (`execution/chargers/sigenergy.py`): the person said it acts, it reads a value,
        the site's sources allow the rule, and that value keeps the worst phase under the fuse by the same fuse,
        margin and learned peaks as the battery's cap, with the **other** chargers' last currents (`without_charger`
        is left out: its own lifted current must not undo the lift) and what SpotNav itself leaves standing in the
        house (`_heat_standing_a`). Anything that cannot be read: `False`."""
        try:
            now = dt_util.now()
            options = self.options()
            # The charger's lift runs while the plant is in its own mode, not in the recipe's grid charge: the limit
            # must have been shown to act there (`commands.ImportLimitTrust.acts_in`).
            if not options.import_limit_acts or self._import_limit_refusal(
                dc_charger=False, action=ACTION_SELF_CONSUME
            ) is not None:
                return False
            value = self._import_limit_kw(control_for(self.facts.control_brand))
            peaks = getattr(self.site, "phase_peaks", None)
            if value is None or peaks is None:
                return False
            learned = peaks.learned(now.timestamp())  # raises: not guarded (never the cautious rule by default)
            return gate.import_limit_holds(
                value, self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config),
                margin_a=HEADROOM_MARGIN_A,
                cars_a=self._cars_allowed_a(without=without_charger) + self._heat_standing_a(),
                house_peak_kw=self._house_peak_kw(now), house_phase_peaks_a=learned.phase_peaks(),
            )
        except Exception:  # noqa: BLE001 - fail closed: the fuse cap stays
            self._look_failed("import_limit_guards")
            return False

    def _import_limit_status(self, now: datetime, options: gate.Options) -> dict[str, Any] | None:
        """The plant's own grid import limit against what keeps the worst phase under the fuse
        (`gate.import_limit_max_kw`), for the status: `value_kw` (what it reads), `max_kw` (the most that guards the
        fuse with the chargers' last currents as they are now), `suggested_kw` (the same with no car charging, rounded
        down to 0.1 kW: what the person may set by the total rule), `holds` (shown to act and at most
        `max_kw`: the rule in force) and `why` (why the site's sources refuse the rule whatever the limit reads,
        `_import_limit_refusal`). For a brand whose limit can be held per phase (`commands.ImportLimitPhases`) also
        `per_phase`: `max_kw` (`gate.import_limit_phase_max_kw`, `None` where no value guards), `set_kw` (what the
        administrator's tap writes, `import_limit_write_kw`; `None` when the limit already guards), `holds`, `why`
        (`_import_limit_phase_refusal`) and `entity_id` (the plant's entity a refusal names: the Output Type, the
        phase switch or the grid sensor status, disabled or not; `None` for any other); `holds` above is then true when either rule is in force. `None` for a brand
        without such a limit."""
        control = control_for(self.facts.control_brand)
        if control is None or control.import_limit is None:
            return None
        value = self._import_limit_kw(control)
        rule: dict[str, Any] = {
            "margin_a": HEADROOM_MARGIN_A, "house_peak_kw": self._house_peak_kw(now),
            "house_phase_peaks_a": self._phase_peaks(now),
        }
        fuse_a, volts = self._main_fuse_a(), stored_voltage_between_phases_v(self.site.config)
        # What SpotNav itself leaves standing in the house (a boost, a raise) counts as the chargers' currents do.
        cars_a = self._cars_allowed_a() + self._heat_standing_a()
        refusal = self._import_limit_refusal()
        most = gate.import_limit_max_kw(fuse_a, volts, cars_a=cars_a, **rule)
        free = gate.import_limit_max_kw(fuse_a, volts, cars_a=0.0, **rule)
        suggested = None if free is None else math.floor(free * 10.0 + 1e-9) / 10.0
        block: dict[str, Any] = {
            "value_kw": None if value is None else round(value, 3),
            "max_kw": None if most is None else round(most, 2),
            "suggested_kw": None if suggested is None or suggested < gate.MIN_RATE_KW else suggested,
            "holds": options.import_limit_acts and refusal is None
            and gate.import_limit_holds(value, fuse_a, volts, cars_a=cars_a, **rule),
            "why": _card_refusal(refusal),
        }
        spec = control.import_limit_phases
        if spec is None:
            return block
        phase_refusal = self._import_limit_phase_refusal()
        phase_margin_a = self._site_margin_a()
        phase_most = gate.import_limit_phase_max_kw(fuse_a, volts, margin_a=phase_margin_a, cars_a=cars_a)
        phase_holds = phase_refusal is None and gate.import_limit_phase_holds(
            value, fuse_a, volts, margin_a=phase_margin_a, cars_a=cars_a
        )
        # `holds`: the plant's limit guards the fuse now by either rule (the cap is lifted); `per_phase` says which.
        block["holds"] = block["holds"] or phase_holds
        # The plant's entity the person can fix for a refusal that names one (enable it, or turn the switch on).
        match = {"output_type_unread": control.output_type, "phase_control_off": spec.switch,
                 "grid_sensor_offline": spec.grid_sensor}.get(phase_refusal or "")
        block["per_phase"] = {
            "max_kw": None if phase_most is None else round(phase_most, 2), "holds": phase_holds,
            "why": _card_refusal(phase_refusal),
            # What the administrator's tap sets the limit to (`import_limit_target_kw`), the card's figure too.
            "set_kw": self.import_limit_write_kw(),
            "entity_id": None if match is None else plant_entity_id(self.hass, self.site.config, match),
        }
        return block

    def _import_limit_refusals(self) -> dict[str, str | None] | None:
        try:
            return {"total": self._import_limit_refusal(), "per_phase": self._import_limit_phase_refusal()}
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("import_limit_refusals: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None
