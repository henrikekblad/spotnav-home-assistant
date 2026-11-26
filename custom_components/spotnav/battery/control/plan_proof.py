"""The plan's own commands as the check of a new key: what a command the plant obeyed shows is credited to the key's
report, and past the deadline the plan stands aside for the routine at a moment that suits it."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .. import gate
from ..executor import StepResult
from ..monitor import Command
from ..plan_model import ACTION_SELF_CONSUME
from ..verify import MAX_REPORTS
from .const import ASIDE_GAP_S, ASIDE_S, _ASIDE_ENDS, _PLAN_KINDS, _PLAN_PROVES

_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


class PlanProof:
    """The plan's commands credited as the check. A mixin of `BatteryControl`, on its state."""

    # ---- the plan's own commands as the check of a new key

    def _note_key(self, now: datetime) -> None:
        """Remember when the firmware key read now was first read (`key_since`)."""
        key = self.firmware_key()
        if self._key_run is None or self._key_run[0] != key:
            self._key_run = (key, now)
        if not gate.firmware_known(key) or gate.parse_time(self.key_since.get(key)) is not None:
            return  # (a stored time that cannot be read is no time: the key is noted afresh)
        kept = sorted({**self.key_since, key: now.isoformat()}.items(), key=lambda item: item[1])
        self.key_since = dict(kept[-MAX_REPORTS:])

    def _key_since(self) -> datetime | None:
        return gate.parse_time(self.key_since.get(self.firmware_key()))

    def _credit_since(self) -> datetime | None:
        """From when a command of the plan's counts for the key now: written after the key was first read, and
        while it has been read without a break in this run (`_key_run`). `None`: no command counts."""
        since, run = self._key_since(), self._key_run
        if since is None or run is None or run[0] != self.firmware_key():
            return None
        return max(since, run[1])

    def _credit_plan(self, result: StepResult | None, now: datetime) -> None:
        """Count what a step of the plan's showed for the report of the key now (see the module). Called only for a
        step control made for the plan: never for the routine's, a hardware test's or a test action's."""
        executor = self.executor
        key = self.firmware_key()
        since = self._credit_since()
        if result is None or executor is None or since is None or not gate.firmware_known(key):
            self._plan_wrote = None
            return
        for event in result.events:
            kind = event.get("kind")
            row: dict[str, Any] | None = None
            mode = variant = None
            if kind == "proved":
                detail = event.get("detail") if isinstance(event.get("detail"), Mapping) else {}
                command = Command.from_dict(detail.get("command"))
                mode = _PLAN_PROVES.get(str(event.get("reason")))
                if mode is None or command is None or not command.written or command.at < since:
                    continue  # a dry run, or a command written before this key was read (without a break)
                if command.kind != _PLAN_KINDS[mode]:
                    continue  # not this action's command
                report = self.control_report()
                if mode == "charge" and gate.charge_by_phases(report):
                    continue  # the balanced charge says nothing where a charge is allowed only per phase
                if mode == "hold" and executor.control.variant_names():
                    variant = event.get("variant")
                    if not isinstance(variant, str) or variant != self.hold_variant():
                        continue  # not the hold control runs on
                row = {
                    "action": event.get("reason"), "rate_kw": command.rate_kw, "before_kw": command.before_kw,
                    "commanded_at": command.at.isoformat(), "expected_kw": detail.get("expected_kw"),
                    "readings": [dict(item) for item in detail.get("readings") or () if isinstance(item, Mapping)],
                }
            elif kind == "handed_back":
                if event.get("clean") is True and self._plan_wrote == key:
                    mode = gate.MODE_NORMAL
                    row = {"action": ACTION_SELF_CONSUME, "written": list(event.get("written") or ()),
                           "reason": event.get("reason")}
                self._plan_wrote = None
            elif kind == "restore_failed":
                self._plan_wrote = None
            if mode is None or row is None:
                continue
            proved = gate.plan_proved(
                self.reports.get(key) if isinstance(self.reports.get(key), dict) else None, mode, key=key,
                brand=executor.control.brand, firmware=self.facts.firmware, now=now, variant=variant, detail=row,
            )
            if proved is not None:
                self._keep_report(key, proved)
                self._log([{"at": now.isoformat(), "kind": "plan_proved", "reason": mode, "key": key}])
                _LOGGER.info("SpotNav battery control: the plan's own command showed %s on %s", mode, key)
        if any(not op.restore for op in result.ops):
            self._plan_wrote = key

    def _deadline_due(self, now: datetime) -> bool:
        """Whether the key now has had its week (`gate.deadline_passed`) and the routine still has a mode control
        needs to check on it: one of its own (`gate.needs_run`) or the capability per phase. An expiry is no reason
        (it only lifts a cap, and waits for the plan to be idle as before)."""
        if not self.firmware_known() or not gate.deadline_passed(self._key_since(), now):
            return False
        report = self.report()
        return gate.needs_run(report, self._supported_modes()) or self._per_phase_due(report)

    def _plan_aside(self, now: datetime, options: gate.Options) -> bool:
        """Whether the plan stands aside at this look for a check past its deadline (see the module). It begins to
        only at a moment `gate.suitable_ahead` takes, at most once per `ASIDE_GAP_S` and `gate.RETRY_GAP_S` after
        the routine's last run, and ends after `ASIDE_S` (the routine, once started, runs in the plan's place by
        itself) or as soon as the routine waits for something that moment did not have (`_ASIDE_ENDS`: a car began
        to charge). While it stands aside the plan's slot is none, so the executor hands the battery back."""
        executor = self.executor
        if executor is None or not options.writes or self.trial is not None or not self._deadline_due(now):
            self._aside_until = None
            return False
        if self._aside_until is not None:
            if now < self._aside_until and self.waiting_for not in _ASIDE_ENDS:
                return True
            self._aside_until = None
            self._log([{"at": now.isoformat(), "kind": "plan_aside_ended", "reason": self.waiting_for or "moment"}])
            return False
        if executor.state.phase != "active":
            # Idle: the plan leaves the battery alone and the routine starts as ever. Handing back or blocked:
            # nothing of the plan's to step aside.
            return False
        last = gate.parse_time((self.report() or {}).get("last_run_at"))
        for at, gap in ((last, gate.RETRY_GAP_S), (self._aside_at, ASIDE_GAP_S)):
            if at is not None and (now - at).total_seconds() < gap:
                return False
        # The plant reads as taken because the plan holds it (`preview` sees SpotNav's own Remote EMS): no blocker
        # here. Once handed back, the routine's own start reads it afresh (`gate.suitable`).
        moment = replace(self._moment(now, executor), blocker=None)
        if gate.suitable_ahead(moment) is not None:
            return False
        self._aside_at = now
        self._aside_until = now + timedelta(seconds=ASIDE_S)
        self._log([{"at": now.isoformat(), "kind": "plan_aside", "reason": self.firmware_key()}])
        _LOGGER.info("SpotNav battery control: the plan stands aside for the check of %s", self.firmware_key())
        return True

    def _plan_proof_block(self) -> dict[str, Any] | None:
        """For diagnostics: since when the key now is read, what the plan's own commands have shown on it and
        whether the routine now goes ahead of the plan."""
        try:
            since = self._key_since()
            now = dt_util.now()
            return {
                "key_since": dict(self.key_since),
                "shown_by_plan": list(gate.plan_shown(self.report())),
                "deadline_at": None if since is None else (
                    since + timedelta(seconds=gate.PLAN_PROOF_DEADLINE_S)
                ).isoformat(),
                "ahead_of_plan": self._deadline_due(now),
                "aside_until": None if self._aside_until is None else self._aside_until.isoformat(),
                "aside_at": None if self._aside_at is None else self._aside_at.isoformat(),
            }
        except Exception:  # noqa: BLE001 - diagnostics must never fail on one block
            _LOGGER.debug("plan_proof_block: ignored failure (diagnostics must never fail on one block)", exc_info=True)
            return None
