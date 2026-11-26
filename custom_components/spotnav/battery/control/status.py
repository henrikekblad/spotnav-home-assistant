"""What the battery control stores and shows: its log, the store's data, the checklist of what the person still has to
do, a restore to make by hand, the entities to enable, the card's status and the diagnostics' snapshot."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .. import gate, optin
from ..commands import FOLLOWS_MAX, NORMAL, control_for
from ..executor import restore_order, Executor, taken_over
from ..mirrored import NEED_MIRRORED_READ_ONLY, READ_ONLY_MIRRORED
from ..plan_model import DEFAULT_CYCLE_COST, sell_margin_minor_per_kwh
from ..runtime import read_role_reads
from .const import NEED_HARDWARE_TEST_FIRST, NEED_RECIPE_BLOCKED, NEED_RESTORE_BY_HAND, NEED_TESTER_SIGNUP, ORPHANED
from .helpers import _float

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class Status:
    """The store and what is shown. A mixin of `BatteryControl`, on its state."""

    def _curtailing(self) -> bool:
        """Whether SpotNav holds the site's export limit at 0 now (or is handing it back)."""
        export = self.export_executor
        return export is not None and export.state.phase in ("active", "restoring")

    def write_budget(self) -> dict[str, dict[str, int]]:
        """The day's writes per class (`commands.WriteClass`) for the diagnostics: the brand's cap, what is used
        (the settings' count holds the export limit's writes too) and what is left."""
        executor = self.executor
        if executor is None:
            return {}
        today = dt_util.now().date().isoformat()
        state = executor.state
        fresh = state.day == today
        used = {
            "setting": (state.writes_today if fresh else 0) + self._export_writes_today(dt_util.now()),
            "setpoint": state.setpoint_writes_today if fresh else 0,
        }
        return {
            name: {"cap": executor.control.write_cap(name), "used": count,
                   "left": max(executor.control.write_cap(name) - count, 0)}
            for name, count in used.items()
        }

    # ---- storage and what it shows

    def _log(self, events: Any) -> None:
        for event in events:
            self.log.append(dict(event))
            _LOGGER.debug("Battery control: %s %s", event.get("kind"), event.get("reason"))

    async def _async_save(self) -> None:
        """Store the state when it differs from what was stored last (a look that changed nothing writes nothing)."""
        data = self._data()
        if data == self._saved:
            return
        await self._store.async_save(data)
        self._saved = data

    def _data(self) -> dict[str, Any]:
        executor = self.executor
        data: dict[str, Any] = {
            "reports": dict(self.reports),
            "gate": {"key": self._key, "enabled_seen": self._enabled_seen, "took_from_ai": self.took_from_ai,
                     "notes": [dict(note) for note in self.notes], "hardware_brands": list(self.hardware_brands),
                     "hardware_failed": list(self.hardware_failed), "carry_after": self.carry_after,
                     "key_since": dict(self.key_since),
                     "grandfather_checked": list(self.grandfather_checked),
                     "tester": None if self.tester is None else dict(self.tester),
                     "control_codes": {brand: dict(code) for brand, code in self.control_codes.items()}},
        }
        if executor is not None:
            data |= {
                "state": executor.state.as_dict(),
                "ledger": executor.ledger.as_dict(),
                "monitor": executor.monitor.as_dict(),
            }
        elif not self._started:
            # The start has not made the executor yet (it failed and waits to retry, or was cut short): what the
            # store holds stays for the next start, or a snapshot not handed back would be written away.
            data |= {name: self._stored[name] for name in ("state", "ledger", "monitor") if name in self._stored}
        export = self.export_executor
        if export is not None:
            data["export"] = {
                "state": export.state.as_dict(), "ledger": export.ledger.as_dict(), "monitor": export.monitor.as_dict(),
            }
        elif not self._started and "export" in self._stored:
            data["export"] = self._stored["export"]
        if self._started:
            # The export limit's own gate (the migration, the day's stretches, the checks), executor or not.
            data["export"] = {**(data.get("export") or {}), "gate": self.export.gate.as_dict()}
        if self._orphaned:
            data[ORPHANED] = [dict(orphan) for orphan in self._orphaned]
        return data

    def _needs(self) -> list[str]:
        """What the person can do before control can run, one code each (the card's checklist)."""
        needs: list[str] = []
        facts = self.facts
        control = control_for(facts.control_brand)
        if control is None:
            if facts.battery_entity_id is None:
                return []
            # Mirrored from another Home Assistant (`mirrored.py`): read only, whatever the brand's recipe.
            return [NEED_MIRRORED_READ_ONLY] if facts.read_only == READ_ONLY_MIRRORED else ["no_battery_control"]
        reads = read_role_reads(self.hass, self.site.config, control)
        if not self.firmware_known():
            needs.append("firmware_unknown")
        for role in control.roles:
            read = reads.get(role.name)
            if read is None or not read.found:
                if role.name in control.hacs_roles:
                    # Core's integration lacks it: one note the person can act on, not one line per control.
                    need = f"needs_hacs_integration:{control.hacs_repo}"
                    if need not in needs:
                        needs.append(need)
                elif not role.optional:
                    needs.append(f"control_not_found:{role.name}")
            elif role.method != "service" and not read.enabled:
                needs.append(f"control_disabled:{role.name}")
        # A setting of the person's on the plant that keeps an action from ever running ("Max inverter power" 0 on a
        # Victron GX: the battery never discharges). Read only: SpotNav never writes it.
        executor = self.executor
        if executor is None or executor.control.brand != control.brand:
            executor = Executor(control)
        needs.extend(executor.told_problems(reads))
        management = facts.management
        if management.state == "externally_controlled":
            needs.append(f"outside_control:{management.reason}")
        elif taken_over(facts, control) or self._takeover_withdrawn(self.options()):
            if not self.options().ai_takeover_ack:
                needs.append("ai_takeover_ack")
        elif control.strategy_unread and not self.options().ai_takeover_ack:
            needs.append("strategy_ack")
        elif management.state == "self_managed" and not (self.acting()):
            needs.append(f"self_managed:{management.reason}")
        restore = self._restore_by_hand()
        if restore is not None:
            needs.append(restore)
        if self.recipe_blocked():
            needs.append(NEED_RECIPE_BLOCKED)
        else:
            due = self.hardware_test_due()
            if due == NEED_HARDWARE_TEST_FIRST and self.tester_signup_needed():
                # The first test waits for the person's sign-up as a tester: that is the step to take.
                due = NEED_TESTER_SIGNUP
            if due is not None:
                needs.append(due)
        return needs

    def _restore_by_hand(self) -> str | None:
        """The checklist's line where a hand-back could not write some roles back (`restore_failed`): each entity and
        the value to set by hand, in the hand-back's order (`restore_by_hand:<entity_id>=<value>|...`). It clears by
        itself once they read so (the executor tries them again, `executor.BLOCKED_RESTORE_S`)."""
        executor = self.executor
        if executor is None or executor.state.phase != "blocked" or not executor.state.unrestored:
            return None
        state = executor.state
        normal = NORMAL.get(executor.control.brand, {})
        order = [name for name in restore_order(executor.control, state.action) if name in state.unrestored]
        order += [name for name in state.unrestored if name not in order]
        rows = []
        for name in order:
            entity_id = state.entities.get(name)
            value = state.restore_values.get(name, normal.get(name))
            if entity_id is not None and value == FOLLOWS_MAX:
                # Found following its own maximum (`Role.follows_max`): the number to set is that maximum, as the
                # hand-back writes it (`resolve_follows_max`); the recipe's word stays only where none is shown.
                shown = self.hass.states.get(entity_id)
                maximum = None if shown is None else _float(shown.attributes.get("max"))
                value = value if maximum is None else maximum
            if entity_id is not None and value is not None:
                text = ("on" if value else "off") if isinstance(value, bool) else (
                    f"{value:g}" if isinstance(value, float) else str(value)
                )
                rows.append(f"{entity_id}={text}")
        return f"{NEED_RESTORE_BY_HAND}:{'|'.join(rows)}" if rows else None

    def _settings_device(self) -> str | None:
        """The device that holds the first control the person must enable (`control_disabled`), so the card can open
        its page; `None` when none is disabled."""
        control = control_for(self.facts.control_brand)
        if control is None:
            return None
        reads = read_role_reads(self.hass, self.site.config, control)
        for role in control.roles:
            read = reads.get(role.name)
            if read is not None and read.found and role.method != "service" and not read.enabled:
                return read.device_id
        return None

    def enable_entities(self) -> dict[str, Any] | None:
        """The brand's control entities that are disabled in Home Assistant, for the card's opt-in dialog (`optin`);
        `None` when none is."""
        return optin.describe(self.hass, self.site.config, self.facts.control_brand)

    def enable_facts(self) -> dict[str, Any] | None:
        """The disabled entities that hold a fact the battery plan needs and lacks (its size, its floor), for the
        card's one line (`optin.describe_facts`), shown with control off as on: the plan needs them either way. `None`
        when none is."""
        return optin.describe_facts(self.hass, self.site.config, self.facts)

    def clear_outside_control(self) -> bool:
        """An administrator's "it was me" for the outside-control verdict (`BatteryShadow.clear_outside_by_person`):
        `False` when none a person may clear stands (none, or an integration's or a planner's)."""
        battery_shadow = getattr(self.site, "battery_shadow", None)
        if battery_shadow is None or not battery_shadow.clear_outside_by_person(dt_util.now()):
            return False
        self.facts = self._read_facts()
        return True

    async def async_enable_entities(self) -> list[str]:
        """Enable the control entities `enable_entities` lists and reload their integration once; only ever called
        on the person's confirm (an administrator, never over the webhook). Answers the entity ids enabled."""
        return await optin.async_enable(self.hass, self.site.config, self.facts.control_brand)

    async def async_enable_facts(self) -> list[str]:
        """Enable the fact entities `enable_facts` lists (never a control) and reload their integration once; only
        ever called on the person's confirm (an administrator, never over the webhook)."""
        enabled = await optin.async_enable_facts(self.hass, self.site.config, self.facts)
        if enabled:
            self.facts = self._read_facts()
        return enabled

    def _line(self, options: gate.Options) -> str:
        """The one line under the switch, as a stable code (the card words it)."""
        state = self.state(options)
        report = self.report()
        executor = self.executor
        if state == "verifying":
            return "checking_after_update" if self._reverifying() else "checking"
        if state == "failed":
            mode = gate.failed_mode(report)
            outcome = None if mode is None else (report or {}).get("modes", {}).get(mode, {}).get("outcome")
            return f"failed:{mode or 'not_judgeable'}:{outcome or 'attempts'}"
        if state == "waiting":
            return f"waiting:{self.waiting_for or 'moment'}"
        if state == "on" and executor is not None and executor.state.phase == "blocked":
            return f"blocked:{executor.state.blocked}"
        if state == "on" and "ai_takeover_ack" in self._needs():
            return "watching:ai_mode"
        if state == "on" and "strategy_ack" in self._needs():
            return "watching:strategy"
        if state == "on" and self._reverifying() and self._check_due(self.report()):
            # Control runs on what passed before the update; the check of the new key waits for its moment.
            return f"waiting_after_update:{self.waiting_for or 'moment'}"
        return state

    def status(self) -> dict[str, Any]:
        """What the card's battery section shows (once it has one) and diagnostics: the options, the state and its
        line, the report for the plant's firmware, the actions allowed, the routine's progress, what is missing and
        the notes."""
        self._look_id += 1
        options = self.options()
        verification = self.verification
        current = None if verification is None else verification.current
        return {
            # Whether the site's battery facts name a battery: without one a client hides its battery card (additive).
            "battery_found": bool(self.facts.battery_entity_id),
            # Set when SpotNav may only read it (`facts.BatteryFacts.read_only`, `remote_homeassistant` for one mirrored
            # from another Home Assistant), `null` otherwise (additive).
            "battery_read_only": self.facts.read_only,
            # As stored: a recipe still waiting for its hardware test runs as shadow only whatever the switch says,
            # and the checklist says why (`hardware_test_first`).
            "options": self._stored_options().as_dict(),
            "state": self.state(options),
            "line": self._line(options),
            "key": self.firmware_key(),
            "report": self.report(),
            "allowed_actions": sorted(self.allowed(options)),
            "verification": None if verification is None else {
                "started_at": verification.started_at.isoformat(),
                "mode": None if current is None else current.mode,
                "stage": verification.stage,
                "modes": [result.mode for result in verification.results],
                "done": [result.mode for result in verification.results if result.outcome is not None],
            },
            "waiting_for": self.waiting_for,
            "needs": self._needs(),
            "device_id": self._settings_device(),
            "enable_entities": self.enable_entities(),
            "enable_facts": self.enable_facts(),
            "notes": [dict(note) for note in self.notes],
            "preheat": self.preheat.status(self.facts),
            "trial": None if self.trial is None else self.trial.as_dict(),
            "grid_charge_cap": self._cap_status(dt_util.now(), options),
            "per_phase": self._per_phase_block(dt_util.now(), options),
            "plan_costs": self._plan_costs(),
            # The tester stage for a recipe that takes a control code (`tester_view`), `null` for every other.
            "tester": self.tester_view(),
        }

    def wear_default(self) -> float:
        """The area currency's own wear per kWh each way (the SEK figure while no area or currency is known): what
        bounds a written wear (`gate.valid_change`)."""
        try:
            from ..plan_runtime import site_price_source_of  # noqa: PLC0415 - it imports this module

            source = site_price_source_of(self.hass, self.site)
            area = None if source is None else source.area
            return gate.wear_default(None if area is None else area.currency)[0]
        except Exception:  # noqa: BLE001 - a bound must never fail the write
            self._look_failed("wear_default")
            return DEFAULT_CYCLE_COST

    def _plan_costs(self) -> dict[str, Any] | None:
        """The plan's two figures in the site's currency for the card and the app: the wear per kWh each way (the
        site's own, `gate.CONF_WEAR`, else the currency's default, also sent as `wear_default_minor_per_kwh`) and
        Balanced's selling margin per kWh, in the minor unit; `None` while no price area is known."""
        try:
            from ..plan_runtime import site_price_source_of  # noqa: PLC0415 - it imports this module

            source = site_price_source_of(self.hass, self.site)
            area = None if source is None else source.area
            if area is None or not area.major_unit or not area.minor_unit:
                return None
            entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
            wear, _unknown = gate.site_wear(self.site.config if entry is None else entry.data, area.currency)
            default, _unknown = gate.wear_default(area.currency)
            return {
                "wear_minor_per_kwh": wear,
                "wear_default_minor_per_kwh": default,
                "balanced_margin_minor_per_kwh": sell_margin_minor_per_kwh(area.currency),
                "major_unit": area.major_unit,
                "minor_unit": area.minor_unit,
            }
        except Exception:  # noqa: BLE001 - the status must never fail on it
            self._look_failed("plan_costs")
            return None

    def _per_phase_block(self, now: datetime, options: gate.Options) -> dict[str, Any] | None:
        try:
            return self._per_phase_status(now, options)
        except Exception:  # noqa: BLE001 - the status must never fail on it
            self._look_failed("per_phase_status")
            return None

    def snapshot(self) -> dict[str, Any]:
        executor = self.executor
        try:
            status: dict[str, Any] | None = self.status()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("snapshot: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            status = None
        return {
            "status": status,
            "brand": None if executor is None else executor.control.brand,
            "executor": None if executor is None else executor.as_dict(),
            "export": None if self.export_executor is None else self.export_executor.as_dict(),
            "export_gate": self.export.snapshot(),
            "verification": None if self.verification is None else self.verification.report(),
            "reports": dict(self.reports),
            # The steps control runs on an earlier key's answer while the routine checks the key now, each with the
            # key it came from (`control_report`).
            "carried_from": self._carried_block(),
            # When the plant last failed a check that left no failed report: nothing older is carried.
            "carry_after": self.carry_after,
            # What the plan's own commands showed on the key now, and its deadline (`_credit_plan`, `_plan_aside`).
            "plan_proof": self._plan_proof_block(),
            "log": list(self.log),
            "preheat": self.preheat.as_dict(),
            "write_budget": self._write_budget_block(),
            "phase_backfill": self._backfill_status(),
            "expiry": self._expiry_block(),
            # The precise import-limit refusals (the card is sent `not_supported` for `not_trusted`).
            "import_limit_refusal": self._import_limit_refusals(),
            # Snapshots kept because this version has no recipe for their brand (`_set_aside`).
            "orphaned": [
                {"key": orphan["key"], **{name: (orphan.get("state") or {}).get(name)
                                          for name in ("brand", "entities", "restore_values")}}
                for orphan in self._orphaned
            ],
        }

    def _carried_block(self) -> dict[str, str] | None:
        try:
            return self.carried_from()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("carried_block: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None

    def _write_budget_block(self) -> dict[str, Any] | None:
        try:
            return self.write_budget()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("write_budget_block: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None

    def _backfill_status(self) -> dict[str, Any] | None:
        """What reading the house's per-phase peaks back from the recorder gave (`site/phase_backfill.py`)."""
        try:
            return self.site.phase_peaks.backfill_status()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("backfill_status: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None
