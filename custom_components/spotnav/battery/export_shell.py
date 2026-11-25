"""The export limit's side of the battery control shell: the site's own switch, its gates and what the card shows.

"No export at negative prices" is one switch per site (`gate.CONF_EXPORT_CURTAIL`, the card's Solar section), for a
site with or without a battery. The battery opt-in and shadow only say nothing about it. `ExportShell` sits on the
site's `BatteryControl` (which keeps the export limit's executor, its inputs and its writes) and decides, each look,
what that executor is asked for:

* **Whether anything may be written** (`state`): `off` (the switch), `no_control` (no brand with an export control is
  found), `beta_only` (the brand's export recipe has not run on a real plant, `BrandControl.field_tested`, and the
  site is not a beta tester's, `beta.learning_gate_open`: nothing is written, the rule's shadow still counts), else
  `on`. An export recipe never waits for the battery recipe's hardware test or control code.
* **The check first** where no source states that 0 means no export (`BrandControl.zero_stated`): the
  verification's `curtail` mode, once per firmware key, by itself at the next moment the site exports
  (`export_gate.ExportGate`); ordinary curtails wait for it.
* **The action** (`_action`): `curtail_export` while the sell price now is below zero; kept through a positive gap
  of at most half an hour (`export_gate.bridges`), unless the battery's plan sells then (a limit at 0 would stop the
  sale); at most `export_gate.CURTAIL_CHANGES_A_DAY` stretches start in a day.
* **A change by someone else** blocks the executor (`changed_outside_spotnav`); for the export limit that block ends
  with the day (nothing is written again that day). A limit not obeyed, or a write not taken, stays blocked until the
  person switches the rule off and on (`_transitions`).
* **The one-time migration** (`migrate`): before the switch, the battery opt-in armed the export limit, and only
  Sigenergy had one (`commands.EXPORT_ARMED_BY_BATTERY`). A site of that brand whose battery control writes when the
  switch arrives gets it turned on once, so nothing stops by an update. Every other site starts with it off.
* **What the card shows** (`status`, in the site settings view's `export_curtail` block): whether a control is found,
  the switch, the 30 days' would-be saving from the rule's shadow (`export_curtail.CurtailShadow.totals`), and at
  most one thing the person can act on (`need`). A plant only watched (its own optimiser) is logged, not shown.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from ..beta import learning_gate_open
from ..const import CONF_CHARGER_ENTRY_IDS, CONF_GRID_POWER_SOURCE, CONF_MEASUREMENT_MODE
from ..core.electric import phase_volts
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..site.measurement_problem import UNHEALTHY_STATES
from ..site.solar_capability import solar_capability
from . import gate
from .commands import (
    ACTION_CURTAIL_EXPORT,
    EXPORT_ARMED_BY_BATTERY,
    BrandControl,
    reading,
)
from .executor import Executor
from .export_curtail import TALLY_DAYS, curtails
from .export_gate import CHECK_FAILED, CURTAIL_CHANGES_A_DAY, ExportGate
from .facts import BatteryFacts
from .monitor import CURTAIL_GRACE_S, READING_GAP_S, Capability
from .plan_model import ACTION_EXPORT_DISCHARGE, ACTION_SELF_CONSUME
from .runtime import export_control_found, read_export_facts
from .verify import Sample, Timing, Verification

if TYPE_CHECKING:
    from .control import BatteryControl

_LOGGER = logging.getLogger(__name__)

#: The check's timing: the monitor's own grace for a curtail, then three readings a minute apart.
CHECK_TIMING: Final = Timing(settle_s=CURTAIL_GRACE_S, readings=3, reading_gap_s=READING_GAP_S)

#: What the card may be told the person can act on (`ExportShell.status`'s `need`).
NEED_DISABLED: Final = "control_disabled"
NEED_LIMIT_UNSET: Final = "limit_unset"
NEED_NOT_OBEYED: Final = "not_obeyed"
NEED_NOT_TAKEN: Final = "not_taken"
NEED_BETA: Final = "beta_only"
NEED_GRID_READING: Final = "no_grid_reading"


class ExportShell:
    """The export limit's gates on one site's `BatteryControl`."""

    def __init__(self, control: BatteryControl) -> None:
        self.c = control
        self.gate = ExportGate()
        #: The running check (`export_gate`), not stored: a restart ends it and hands back what it held.
        self.verification: Verification | None = None
        #: Why an ordinary curtail waits now (`check`, `check_failed`, `stretches_spent`), `None` when it does not.
        self.waiting: str | None = None
        self._key: str | None = None

    def load(self, raw: Any) -> None:
        self.gate = ExportGate.from_dict(raw)

    # ---- what is there

    def recipe(self) -> BrandControl | None:
        """The site's export control: the executor's, else the one its entities name now."""
        executor = self.c.export_executor
        if executor is not None:
            return executor.control
        return read_export_facts(self.c.hass, self.c.site.config)[2]

    def state(self, options: gate.Options | None = None) -> str:
        """`off`, `no_control`, `beta_only` or `on` (see the module)."""
        c = self.c
        options = c._stored_options() if options is None else options
        if not options.export_writes:
            return "off"
        control = self.recipe()
        if control is None:
            return "no_control"
        if not control.field_tested and not learning_gate_open(c.hass, c.site.entry_id):
            return "beta_only"
        return "on"

    def key(self, control: BrandControl, facts: BatteryFacts) -> str:
        """What a check is kept under: the brand, the plant's firmware and its integration's version, and the
        recipe's revision; the last one read while the firmware reads nothing."""
        if facts.firmware or self._key is None or not self._key.startswith(f"{control.brand} | "):
            version = self.c._integration_version(facts.integration)
            self._key = (
                f"{control.brand} | {gate.firmware_key(facts.firmware, facts.integration, version)}"
                f" r{control.recipe_rev}"
            )
        return self._key

    # ---- the migration

    def migrate(self, now: datetime) -> bool:
        """Once per site (see the module); whether the switch was turned on by it."""
        if self.gate.migrated:
            return False
        c = self.c
        entry = c.hass.config_entries.async_get_entry(c.site.entry_id)
        if entry is None:
            return False
        self.gate.migrated = True
        if gate.CONF_EXPORT_CURTAIL in entry.data:
            return False
        if c.facts.control_brand not in EXPORT_ARMED_BY_BATTERY or not c.options().writes:
            return False
        c.hass.config_entries.async_update_entry(entry, data={**entry.data, gate.CONF_EXPORT_CURTAIL: True})
        c._log([{"at": now.isoformat(), "kind": "export_migrated", "reason": "battery_control_wrote"}])
        _LOGGER.info("SpotNav export limit: turned on for this site, where the battery control already ran it")
        return True

    # ---- one look

    async def async_look(self, now: datetime, options: gate.Options) -> None:
        """The export limit's part of a look (never during a test action of its own or the start-up wait)."""
        c = self.c
        if self.migrate(now):
            options = c.options()
        self._transitions(options, now)
        state = self.state(options)
        if state != "on":
            self.verification = None
            self.waiting = None
            await c._async_export_release(now, "export_off" if state == "off" else f"export_{state}")
            return
        executor = c._export_executor()
        if executor is None:
            return
        self._day_passed(executor, now)
        facts = read_export_facts(c.hass, c.site.config)[0]
        key = self.key(executor.control, facts)
        if self.verification is not None or self._check_starts(now, executor, key):
            await self._async_check(now, executor, key)
            return
        action = self._action(now, executor, key)
        before = self._before_kw()
        result = await c._async_export_step(now, action, allowed=True)
        if result is not None:
            self._after(now, executor, key, result.events, before_kw=before, check=False)

    def _transitions(self, options: gate.Options, now: datetime) -> None:
        """The switch turned on (a block lifted, a failed check forgotten so it runs again) or off."""
        on = options.export_writes
        if on and not self.gate.seen_on:
            executor = self.c.export_executor
            if executor is not None:
                executor.reset()
            self.gate.forget_failed()
            self.c._log([{"at": now.isoformat(), "kind": "export_enabled", "reason": "switch"}])
        elif not on and self.gate.seen_on:
            self.c._log([{"at": now.isoformat(), "kind": "export_disabled", "reason": "switch"}])
        self.gate.seen_on = on

    def _day_passed(self, executor: Executor, now: datetime) -> None:
        """A limit someone else changed is left alone for the day: the block ends with it."""
        state = executor.state
        if (
            state.phase == "blocked" and (state.blocked or "").startswith("changed_outside_spotnav")
            and not state.holds_snapshot and state.released_at is not None
            and state.released_at.astimezone(now.tzinfo).date() < now.date()
        ):
            executor.reset()
            self.c._log([{"at": now.isoformat(), "kind": "export_unblocked", "reason": "next_day"}])

    def _measurement_ok(self) -> bool:
        result = self.c.site.result
        return result.state not in UNHEALTHY_STATES and result.state != "not_configured"

    def grid_kw(self) -> float | None:
        """The site's grid power now (kW, + import): its signed phase powers summed, else (a site that measures
        currents and has the meter's total grid power) that total; `None` when neither is known. What the export
        limit is taken on and judged by."""
        c = self.c
        grid = c._grid_import_kw()
        if grid is None:
            from ..execution.solar_execution import site_net_grid_w  # noqa: PLC0415 - it imports the site

            watts = site_net_grid_w(c.site)
            grid = None if watts is None else watts / 1000.0
        return grid if grid is None or math.isfinite(grid) else None

    def _before_kw(self) -> float | None:
        try:
            return self.grid_kw() if self._measurement_ok() else None
        except Exception:  # noqa: BLE001 - not known: nothing is judged on it
            _LOGGER.debug("export limit: the site's grid power could not be read", exc_info=True)
            return None

    def _plan_sells(self, now: datetime) -> bool:
        """Whether the battery sells now or its plan asks it to: the export limit is never held at 0 under a sale."""
        c = self.c
        battery = c.executor
        if battery is not None and battery.state.phase == "active" and battery.state.action == ACTION_EXPORT_DISCHARGE:
            return True
        try:
            slot = c._plan_now(now)
        except Exception:  # noqa: BLE001 - no plan read: nothing is known to sell
            _LOGGER.debug("export limit: the battery plan could not be read", exc_info=True)
            return False
        return slot is not None and slot[0] == ACTION_EXPORT_DISCHARGE

    def _action(self, now: datetime, executor: Executor, key: str) -> str:
        """What the executor is asked for now (see the module)."""
        c = self.c
        gate_ = self.gate
        active = executor.state.phase == "active"
        day = now.date().isoformat()
        self.waiting = None
        if curtails(c._sell_now(now)):
            gate_.last_negative_at = now
            if active:
                return ACTION_CURTAIL_EXPORT
            if gate_.check_needed(key, zero_stated=executor.control.zero_stated):
                self.waiting = "check_failed" if gate_.check_failed(key) else "check"
                return ACTION_SELF_CONSUME
            if gate_.stretches_spent(day):
                self.waiting = "stretches_spent"
                return ACTION_SELF_CONSUME
            return ACTION_CURTAIL_EXPORT
        if active and not self._plan_sells(now) and gate_.bridged(now, c._sell_now):
            return ACTION_CURTAIL_EXPORT
        return ACTION_SELF_CONSUME

    def _after(
        self, now: datetime, executor: Executor, key: str, events: Any, *, before_kw: float | None, check: bool
    ) -> None:
        """Count a stretch that started and note the key's first ordinary curtail and how it ended."""
        gate_ = self.gate
        for event in events:
            if event.get("kind") == "enter":
                gate_.stretch_started(now.date().isoformat())
                if not check:
                    gate_.first_started(key, now, before_kw)
            elif event.get("kind") == "hand_back" and not check:
                reason = str(event.get("reason"))
                verdict = "clean" if reason == "curtail_ended" else reason
                if gate_.first_ended(key, verdict):
                    self.c._log([{
                        "at": now.isoformat(), "kind": "export_first_curtail", "reason": key, "verdict": verdict,
                        "brand": executor.control.brand, "export_before_kw": gate_.first[key].get("export_before_kw"),
                    }])

    # ---- the check

    def _check_starts(self, now: datetime, executor: Executor, key: str) -> bool:
        gate_ = self.gate
        if not gate_.check_needed(key, zero_stated=executor.control.zero_stated):
            return False
        state = executor.state
        if state.phase != "idle" or state.holds_snapshot or gate_.stretches_spent(now.date().isoformat()):
            return False
        if self._plan_sells(now):
            return False
        return gate_.check_due(key, now, self._before_kw())

    async def _async_check(self, now: datetime, executor: Executor, key: str) -> None:
        """One look of the check: the verification's `curtail` mode on the export limit's executor, judged on the
        site's grid power, then handed back."""
        c = self.c
        verification = self.verification
        grid = self._before_kw()
        if verification is None:
            facts = read_export_facts(c.hass, c.site.config)[0]
            verification = self.verification = Verification(
                executor, now, firmware=facts.firmware, modes=("curtail",), timing=CHECK_TIMING
            )
            self.gate.check_started(now)
            c._log([{"at": now.isoformat(), "kind": "export_check_started", "reason": key}])
            _LOGGER.info("SpotNav export limit: checking that the limit stops the export (%s)", key)
        sample = Sample(grid, None if grid is None else 0.0)
        action, _rate = verification.command(now, sample, None)
        result = await c._async_export_step(now, action, allowed=True)
        if result is None:
            self.verification = None
            return
        self._after(now, executor, key, result.events, before_kw=grid, check=True)
        verification.after(now, result, sample, Capability())
        if not verification.done:
            return
        self.verification = None
        outcome = (verification.report()["modes"].get("curtail") or {}).get("outcome")
        if outcome == "obeyed" or outcome in CHECK_FAILED:
            self.gate.keep_check(key, outcome, now)
        c._log([{"at": now.isoformat(), "kind": "export_check_done", "reason": key, "outcome": outcome}])
        _LOGGER.info("SpotNav export limit: the check ended (%s)", outcome)

    # ---- the switch turned off

    async def async_switched_off(self) -> None:
        """The switch was turned off: what is held is handed back before the write's answer."""
        self.verification = None
        if self.c.export_executor is not None:
            await self.c._async_export_until_handed_back("export_off")

    # ---- inputs the executor's look needs

    def excused_kw(self) -> float | None:
        """What the site's battery charges and its chargers draw now (kW), where read: an import up to it under a
        curtail is theirs (`monitor`'s `import_rose`). `None` when nothing of it is read."""
        c = self.c
        total = 0.0
        known = False
        try:
            power_kw, _age = c._battery_power()
            if power_kw is not None and math.isfinite(power_kw):
                total += max(power_kw, 0.0)
                known = True
        except Exception:  # noqa: BLE001 - not read: nothing is excused for it
            _LOGGER.debug("export limit: the battery's power could not be read", exc_info=True)
        try:
            volts = phase_volts(stored_voltage_between_phases_v(c.site.config))
            for charger in c.site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
                measured = c.site.charger_measured_current(charger)
                if measured is None:
                    continue
                amps = [value.value for value in (measured.l1, measured.l2, measured.l3) if value.value is not None]
                if amps:
                    total += sum(max(float(amp), 0.0) for amp in amps) * volts / 1000.0
                    known = True
        except Exception:  # noqa: BLE001 - not read: nothing is excused for them
            _LOGGER.debug("export limit: the chargers' draw could not be read", exc_info=True)
        return total if known else None

    # ---- what it shows

    def _saving(self) -> dict[str, Any]:
        """The last 30 days' export at a negative sell price and what it cost, from the rule's shadow (counted
        whether the switch is on or off), with the area's money units where known."""
        c = self.c
        totals = c.site.battery_shadow.curtail.totals()
        major = minor = None
        try:
            from .plan_runtime import site_price_source_of  # noqa: PLC0415 - it imports the control

            source = site_price_source_of(c.hass, c.site)
            area = None if source is None else source.area
            if area is not None:
                major, minor = area.major_unit or None, area.minor_unit or None
        except Exception:  # noqa: BLE001 - the figure is shown without a unit
            _LOGGER.debug("export limit: the price area could not be read", exc_info=True)
        return {
            "days": TALLY_DAYS,
            "kwh": round(totals["kwh"], 2),
            "minor": round(totals["saving"], 1),
            # The part on days the limit could not have been run (watch-only, an entity missing).
            "blocked_minor": round(totals["blocked_saving"], 1),
            "major_unit": major,
            "minor_unit": minor,
        }

    def _need(
        self, control: BrandControl, reads: Any, state: str, options: gate.Options
    ) -> tuple[str | None, str | None]:
        """The one thing the person can act on, and the entity it is about: see the module."""
        for name in control.enter_order:
            read = reads.get(name)
            if read is not None and read.found and not read.enabled:
                return NEED_DISABLED, read.entity_id
        limit = reads.get("export_limit")
        role = control.role("export_limit")
        if (
            limit is not None and role.unknown_is_unset and reading(role, limit.state) is None
            and (limit.state or "").strip().lower() == "unknown"
        ):
            return NEED_LIMIT_UNSET, limit.entity_id
        if not options.export_writes:
            return None, None
        if state == "beta_only":
            return NEED_BETA, None
        executor = self.c.export_executor
        blocked = "" if executor is None or executor.state.phase != "blocked" else (executor.state.blocked or "")
        failed = self._key is not None and self.gate.check_failed(self._key)
        if blocked.startswith("not_obeyed") or failed:
            absent = [name for name in control.hacs_roles if name not in reads]
            if absent and control.hacs_repo:
                # The limit acts only where the inverter has it enabled, and only the custom integration can do that.
                return f"needs_hacs_integration:{control.hacs_repo}", None
            return NEED_NOT_OBEYED, None
        if blocked.startswith("write_not_taken"):
            return NEED_NOT_TAKEN, None
        config = self.c.site.config
        if not solar_capability(config.get(CONF_MEASUREMENT_MODE), config.get(CONF_GRID_POWER_SOURCE)).capable:
            # The site measures no signed grid power at all: nothing could judge the limit, so it is never taken.
            return NEED_GRID_READING, None
        return None, None

    def status(self) -> dict[str, Any]:
        """The site settings view's `export_curtail` block (`api/site_view.py`, `docs/api.md`)."""
        c = self.c
        _facts, reads, control = read_export_facts(c.hass, c.site.config)
        found = export_control_found(reads, control)
        options = c._stored_options()
        if control is None or not found:
            return {
                "found": False, "enabled": options.export_curtail, "brand": None, "state": "no_control", "beta": False,
                "need": None, "entity_id": None, "saving": self._saving(),
            }
        state = self.state(options)
        need, entity_id = self._need(control, reads, state, options)
        return {
            "found": True,
            "enabled": options.export_curtail,
            "brand": control.brand,
            "state": state,
            # The brand's export recipe has not run on a real plant: it writes on a beta tester's site only.
            "beta": not control.field_tested,
            "need": need,
            "entity_id": entity_id,
            "saving": self._saving(),
        }

    def snapshot(self) -> dict[str, Any]:
        """For the diagnostics (and so the nightly beta report): the gate's state, the checks and first curtails."""
        try:
            state: str | None = self.state()
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            state = None
        verification = self.verification
        return {
            "state": state,
            "waiting": self.waiting,
            "key": self._key,
            "stretches_a_day": CURTAIL_CHANGES_A_DAY,
            "check_running": None if verification is None else verification.report(),
            **self.gate.as_dict(),
        }
