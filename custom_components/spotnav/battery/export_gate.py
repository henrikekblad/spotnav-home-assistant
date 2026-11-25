"""What the export limit's shell keeps beside its executor: the day's curtail stretches, the gap it bridges, the
check that comes before a brand's first ordinary curtail, and each firmware's first curtail. Pure.

* **Stretches a day** (`CURTAIL_CHANGES_A_DAY`): a curtail stretch is two writes (four where the brand's limit has a
  switch that enables it), setting registers nobody states the wear of. At most six stretches start in a local day;
  a seventh waits for the next day (`stretches_spent`). The hand-back of a running one is never held back.
* **Short gaps are bridged** (`BRIDGE_GAP_S`): a price that turns positive for at most half an hour between two
  negative stretches leaves the limit at 0 (`bridges`), which saves a stretch's writes for the price of that gap's
  small income. Only while a curtail runs, only where the prices held show the next negative slot starting within
  half an hour of the last negative one, and never while the battery's plan sells (a limit at 0 would stop the sale:
  it is handed back first).
* **The check** (`check_needed`): where no source states that 0 means no export (`BrandControl.zero_stated` false:
  Solis, GoodWe, SolaX), the verification's `curtail` mode runs once by itself at the next moment the site exports at
  least `CHECK_MIN_EXPORT_KW`, whatever the price, and ordinary curtails wait for it to pass on the plant's firmware
  (`ExportGate.checks`, kept per key). One that could not be judged is tried again after `CHECK_RETRY_S`; one that
  failed holds the limit back until the person switches the rule off and on again.
* **The first curtail** on each key is logged with its verdict (`first_curtail`, the log's `export_first_curtail`),
  for the beta reports: the recipe is marked field tested per brand once one or two sites curtailed cleanly.
* **The migration** (`migrated`): done once per site (see `control.BatteryControl._migrate_export`).

State survives a restart (`as_dict`, `from_dict`); a check that ran when Home Assistant stopped does not (the
executor's own stored snapshot is handed back first, as ever).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from .export_curtail import curtails
from .shadow import parse_aware_datetime

#: A positive gap between two negative stretches of at most this long is bridged (s).
BRIDGE_GAP_S: Final = 1800.0
#: How far apart the prices ahead are looked at (s): finer than any price slot.
_BRIDGE_STEP_S: Final = 300.0
#: The most the last negative look lies before the gap's start (s).
_LOOK_SLACK_S: Final = 60.0
#: Curtail stretches that may start in a local day, per site.
CURTAIL_CHANGES_A_DAY: Final = 6
#: The check runs only while the site exports at least this (kW): less could not be judged soundly.
CHECK_MIN_EXPORT_KW: Final = 1.0
#: A check that could not be judged is tried again no sooner than this (s).
CHECK_RETRY_S: Final = 3600.0
#: Keys kept (checks, first curtails).
MAX_KEYS: Final = 6

#: The check's outcomes that let ordinary curtails run, and those that hold them back.
CHECK_PASSED: Final = "obeyed"
CHECK_FAILED: Final = frozenset({"ignored", "partial"})


def bridges(
    now: datetime, last_negative_at: datetime | None, sell_at: Callable[[datetime], float | None]
) -> bool:
    """Whether a curtail that runs stays through a positive price now: the price was negative at most `BRIDGE_GAP_S`
    ago and turns negative again no later than `BRIDGE_GAP_S` after it last was."""
    if last_negative_at is None or now < last_negative_at:
        return False
    # The last look that saw a negative price is up to a look before the gap began.
    until = last_negative_at + timedelta(seconds=BRIDGE_GAP_S + _LOOK_SLACK_S)
    at = now
    while at <= until:
        if curtails(sell_at(at)):
            return True
        at += timedelta(seconds=_BRIDGE_STEP_S)
    return curtails(sell_at(until))


@dataclass(slots=True)
class ExportGate:
    """The export limit's own state beside its executor's."""

    #: The site's one-time migration from the battery opt-in was decided.
    migrated: bool = False
    #: The rule's switch as last seen (its turning on lifts a block and forgets a failed check).
    seen_on: bool = False
    #: The local day counted and the stretches started in it.
    day: str | None = None
    stretches: int = 0
    #: When the sell price was last seen below zero.
    last_negative_at: datetime | None = None
    #: Per key, the check's last outcome and when (`{"outcome", "at"}`).
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: When a check last started.
    check_tried_at: datetime | None = None
    #: Per key, the first ordinary curtail, the site's grid power before it (kW, + import) and its verdict
    #: (`{"at", "export_before_kw", "verdict"}`; `running` until it ends).
    first: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The gap the bridge was last decided for (its `last_negative_at`) and the answer; not stored.
    _bridge_for: datetime | None = None
    _bridge: bool = False

    # --- the day's stretches ---

    def _roll(self, day: str) -> None:
        if self.day != day:
            self.day, self.stretches = day, 0

    def stretch_started(self, day: str) -> None:
        self._roll(day)
        self.stretches += 1

    def stretches_spent(self, day: str) -> bool:
        self._roll(day)
        return self.stretches >= CURTAIL_CHANGES_A_DAY

    # --- the gap ---

    def bridged(self, now: datetime, sell_at: Callable[[datetime], float | None]) -> bool:
        """Whether a running curtail stays through the positive price now (`bridges`), decided once per gap from the
        prices held when it began."""
        last = self.last_negative_at
        if last is None:
            return False
        if self._bridge_for != last:
            self._bridge_for, self._bridge = last, bridges(now, last, sell_at)
        return self._bridge and now <= last + timedelta(seconds=BRIDGE_GAP_S + _LOOK_SLACK_S)

    # --- the check ---

    def check_outcome(self, key: str) -> str | None:
        row = self.checks.get(key)
        outcome = None if row is None else row.get("outcome")
        return outcome if isinstance(outcome, str) else None

    def check_passed(self, key: str) -> bool:
        return self.check_outcome(key) == CHECK_PASSED

    def check_failed(self, key: str) -> bool:
        return self.check_outcome(key) in CHECK_FAILED

    def check_needed(self, key: str, *, zero_stated: bool) -> bool:
        """Whether ordinary curtails still wait for the check on this key."""
        return not zero_stated and not self.check_passed(key)

    def check_due(self, key: str, now: datetime, grid_kw: float | None) -> bool:
        """Whether a check may start now: none passed or failed on the key, the last try long enough ago, and the
        site exports enough to judge it."""
        if self.check_passed(key) or self.check_failed(key):
            return False
        if self.check_tried_at is not None and 0 <= (now - self.check_tried_at).total_seconds() < CHECK_RETRY_S:
            return False
        return grid_kw is not None and grid_kw <= -CHECK_MIN_EXPORT_KW

    def check_started(self, now: datetime) -> None:
        self.check_tried_at = now

    def keep_check(self, key: str, outcome: str | None, now: datetime) -> None:
        self.checks[key] = {"outcome": outcome, "at": now.isoformat()}
        _bound(self.checks)

    def forget_failed(self) -> None:
        """The person switched the rule on again: a failed check is tried again."""
        self.checks = {key: row for key, row in self.checks.items() if row.get("outcome") not in CHECK_FAILED}
        self.check_tried_at = None

    # --- the first curtail ---

    def first_started(self, key: str, now: datetime, before_kw: float | None = None) -> bool:
        """Note an ordinary curtail's start; whether it is the key's first."""
        if key in self.first:
            return False
        self.first[key] = {
            "at": now.isoformat(), "export_before_kw": None if before_kw is None else round(before_kw, 3),
            "verdict": "running",
        }
        _bound(self.first)
        return True

    def first_ended(self, key: str, verdict: str) -> bool:
        """The key's first curtail ended with `verdict`; whether this was that first one (still `running`)."""
        row = self.first.get(key)
        if row is None or row.get("verdict") != "running":
            return False
        row["verdict"] = verdict
        return True

    # --- storage ---

    def as_dict(self) -> dict[str, Any]:
        return {
            "migrated": self.migrated,
            "seen_on": self.seen_on,
            "day": self.day,
            "stretches": self.stretches,
            "last_negative_at": None if self.last_negative_at is None else self.last_negative_at.isoformat(),
            "checks": {key: dict(row) for key, row in self.checks.items()},
            "check_tried_at": None if self.check_tried_at is None else self.check_tried_at.isoformat(),
            "first": {key: dict(row) for key, row in self.first.items()},
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ExportGate:
        if not isinstance(raw, Mapping):
            return cls()
        day, stretches = raw.get("day"), raw.get("stretches")
        return cls(
            migrated=raw.get("migrated") is True,
            seen_on=raw.get("seen_on") is True,
            day=day if isinstance(day, str) else None,
            stretches=stretches if isinstance(stretches, int) and not isinstance(stretches, bool) and stretches >= 0 else 0,
            last_negative_at=parse_aware_datetime(raw.get("last_negative_at")),
            checks=_rows(raw.get("checks")),
            check_tried_at=parse_aware_datetime(raw.get("check_tried_at")),
            first=_rows(raw.get("first")),
        )


def _rows(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return {}
    rows = {str(key): dict(row) for key, row in raw.items() if isinstance(row, Mapping)}
    _bound(rows)
    return rows


def _bound(rows: dict[str, dict[str, Any]]) -> None:
    for key in list(rows)[: max(len(rows) - MAX_KEYS, 0)]:
        del rows[key]
