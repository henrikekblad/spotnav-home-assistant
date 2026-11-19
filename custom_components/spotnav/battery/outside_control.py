"""Something other than SpotNav drives the home battery: notice it, log it, and only watch.

A battery under an aggregator (CheckWatt, Greenely, Tibber Grid Rewards) or another planner changes its mode, its
limits and its level without the person touching anything, usually from a cloud that tells Home Assistant nothing
(`plans/research_integrations/commercial_energy_gap_2026-11-14.md`, "Reading for SpotNav"). SpotNav must not read
that as its own plan failing, nor fight it. So the battery is marked **externally controlled**: the shadow plan
still runs as a benchmark, and the write path refuses (`facts.Management.state` is no longer `free`).

The signals, each judged on what the shell hands over (`Observation`):

* **Integration** (`integration:<domain>`): an integration that exists to run batteries is set up (`coexist.py`,
  strength `runs`), or a battery planner runs here (`planner:predbat`, `planner:emhass`). It lasts while it is there.
  An integration of strength `hint` (Tibber, Greenely) never marks by itself; it is named as the likely source
  when another signal marks.
* **Settings** (`settings_changed`): the battery's own mode settings or its control entities (the discharge limit,
  the remote-control mode) changed `SETTING_CHANGES` times within `SETTING_WINDOW` while SpotNav wrote none of them
  (one or two changes are a person trying a mode). A reading that is `unavailable` or `unknown` is no change: the
  next readable value is compared with the last readable one. Under the battery's own optimiser (`self_managed`)
  only mode changes count, since the optimiser may move its own limits.
* **Behaviour** (`grid_charge_behaviour`, `export_discharge_behaviour`): in plain self-consumption (`free`), the
  battery charges by more than `BEHAVIOUR_KW` while the site imports more than that, or discharges by more than
  that while the site exports more than that, for `BEHAVIOUR_S` in a row, and its level moved at least
  `BEHAVIOUR_SOC_PCT` the same way. Self-consumption does neither. The level's move guards against a battery reading
  whose sign is set the wrong way (it would show a night's discharge as a grid charge). A charge near the battery's
  own floor (`RESERVE_MARGIN_PCT`) is its own maintenance charge, not someone else's. Short frequency-reserve
  activations do not mark: they are too short.

The verdict (`verdict`) holds from the first signal; it ends `CLEAR_AFTER` after the last one (an integration's at
once when it is gone). An administrator may end one found by the battery's settings or behaviour at once
("Det var jag", `clear_by_person`): the changes counted so far are forgotten with it. Each stretch is one **occurrence**: its start and its end are each an event (`observe`),
which the shell logs once at INFO; nothing is raised in Repairs, as nothing there is for the person to do. `told`
marks an occurrence found by the battery's settings or behaviour (news); one from an integration or a planner the
person set up is not. Both are kept in the diagnostics and shown in what SpotNav detected. While SpotNav itself acts
on the battery (`spotnav_acting`, the write path's executor active or handing back) the settings and behaviour
signals are not judged.

State survives a restart (`as_dict`, `from_dict`), so a restart neither logs an occurrence again nor forgets it.
Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from .coexist import outside_integrations
from .shadow import parse_aware_datetime

#: How many changes of the battery's settings within `SETTING_WINDOW` mark it.
SETTING_CHANGES: Final = 3
SETTING_WINDOW: Final = timedelta(hours=24)
#: The least battery power and grid power (kW, each) that count as behaviour self-consumption does not show.
BEHAVIOUR_KW: Final = 1.0
#: How long in a row (s) such behaviour must last, and how far the level must move the same way meanwhile (points).
BEHAVIOUR_S: Final = 600.0
BEHAVIOUR_SOC_PCT: Final = 1.0
#: A charge this close above the battery's own floor (points) is its maintenance charge.
RESERVE_MARGIN_PCT: Final = 5.0
#: A battery reading older than this (s) is not judged.
MAX_AGE_S: Final = 120.0
#: How long after the last signal an occurrence ends.
CLEAR_AFTER: Final = timedelta(hours=48)
#: The occurrences kept for the diagnostics.
HISTORY_MAX: Final = 10

REASON_SETTINGS: Final = "settings_changed"
REASON_GRID_CHARGE: Final = "grid_charge_behaviour"
REASON_EXPORT_DISCHARGE: Final = "export_discharge_behaviour"
#: The reasons that are news, `Verdict.told` (the others are set up by the person).
TOLD_REASONS: Final = frozenset({REASON_SETTINGS, REASON_GRID_CHARGE, REASON_EXPORT_DISCHARGE})

_NO_READING: Final = frozenset({"", "unavailable", "unknown", "none"})


@dataclass(frozen=True, slots=True)
class Observation:
    """One look at the battery and the site."""

    now: datetime
    #: `facts.Management.state` and its settings (mode label -> state) before this module's verdict.
    management: str
    modes: Mapping[str, str | None] = field(default_factory=dict)
    #: The control entities' states (entity id -> state).
    controls: Mapping[str, str | None] = field(default_factory=dict)
    #: The battery's power (kW, + charging), the site's grid power (kW, + import), the level and its floor (%).
    power_kw: float | None = None
    grid_kw: float | None = None
    soc_pct: float | None = None
    reserve_soc_pct: float | None = None
    battery_age_s: float | None = None
    on_grid: bool | None = None
    measurement_ok: bool = True
    #: The domains of the set-up config entries, and a battery planner running here (`runtime.external_planner`).
    integrations: frozenset[str] = frozenset()
    planner: str | None = None
    #: SpotNav's own write path acts on the battery now.
    spotnav_acting: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    active: bool = False
    #: A stable code: `integration:<domain>`, `planner:<name>`, or one of the `REASON_*`.
    reason: str | None = None
    since: datetime | None = None
    #: The likely source's name (an integration's label), `None` when nothing names one.
    source: str | None = None
    #: This occurrence is news (found by settings or behaviour), not an integration the person set up.
    told: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "since": None if self.since is None else self.since.isoformat(),
            "source": self.source,
            "told": self.told,
        }


def _standing_reason(reason: str | None) -> bool:
    return reason is not None and reason.startswith(("integration:", "planner:"))


def _readable(state: str | None) -> bool:
    return state is not None and state.strip().lower() not in _NO_READING


class OutsideControlWatch:
    """The battery's outside-control signals, the verdict and its occurrences (see the module)."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}
        self._changes: deque[datetime] = deque()
        self._behaviour: tuple[str, datetime, float, datetime] | None = None
        self._last_signal: datetime | None = None
        self.verdict = Verdict()
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX)
        self.counts: dict[str, int] = {}

    # ---- one look

    def observe(self, obs: Observation) -> list[dict[str, Any]]:
        """One look; the events it gives (`started`, `ended`), each at most once per occurrence."""
        now = obs.now
        standing = self._standing(obs)
        signal: str | None = None
        if not obs.spotnav_acting:
            signal = self._settings(obs) or self._behaviour_signal(obs)
        else:
            # SpotNav's own writes are no one else's; the baselines restart once it lets go.
            self._last.clear()
            self._behaviour = None
        if signal is not None:
            self._last_signal = now
            self.counts[signal] = self.counts.get(signal, 0) + 1
        hint = next((row.label for row in outside_integrations(obs.integrations) if row.strength == "hint"), None)
        events: list[dict[str, Any]] = []
        verdict = self.verdict
        if verdict.active:
            recent = self._last_signal is not None and now - self._last_signal < CLEAR_AFTER
            if standing is None and not recent:
                # Nothing stands and the battery showed nothing for `CLEAR_AFTER` (a set-up integration that is
                # gone ends it at once, unless the battery itself showed something lately).
                events.append(self._end(now))
            elif standing is not None and _standing_reason(verdict.reason) and verdict.reason != standing[0]:
                self.verdict = Verdict(True, standing[0], verdict.since, standing[1], verdict.told)
            return events
        if standing is not None:
            self.verdict = Verdict(True, standing[0], now, standing[1], told=False)
        elif signal is not None:
            self.verdict = Verdict(True, signal, now, hint, told=signal in TOLD_REASONS)
        else:
            return events
        events.append({"at": now.isoformat(), "kind": "started"} | self.verdict.as_dict())
        return events

    @property
    def clearable(self) -> bool:
        """Whether a person may clear the verdict (`clear_by_person`): one from the battery's settings or behaviour,
        never one from an integration or a planner (that lasts while it is there)."""
        return self.verdict.active and not _standing_reason(self.verdict.reason)

    def clear_by_person(self, now: datetime) -> dict[str, Any] | None:
        """An administrator's "it was me": the occurrence ends now, logged as cleared by a person, and the changes
        counted so far are forgotten, so the same old changes do not mark again (new ones are counted afresh). The
        `ended` event; `None` when there is nothing a person may clear."""
        if not self.clearable:
            return None
        self._changes.clear()
        self._behaviour = None
        return self._end(now, cleared_by="person")

    def _end(self, now: datetime, cleared_by: str | None = None) -> dict[str, Any]:
        verdict = self.verdict
        record = verdict.as_dict() | {"ended": now.isoformat()}
        if cleared_by is not None:
            record["cleared_by"] = cleared_by
        self.history.append(record)
        self.verdict = Verdict()
        self._last_signal = None
        return {"at": now.isoformat(), "kind": "ended"} | record

    @staticmethod
    def _standing(obs: Observation) -> tuple[str, str] | None:
        """A planner or an integration that runs batteries, with its name: it marks while it is there."""
        if obs.planner is not None:
            return f"planner:{obs.planner}", {"predbat": "Predbat", "emhass": "EMHASS"}.get(obs.planner, obs.planner)
        for row in outside_integrations(obs.integrations):
            if row.strength == "runs":
                return f"integration:{row.domain}", row.label
        return None

    def _settings(self, obs: Observation) -> str | None:
        now = obs.now
        while self._changes and now - self._changes[0] > SETTING_WINDOW:
            self._changes.popleft()
        current: dict[str, str | None] = {f"mode:{label}": state for label, state in obs.modes.items()}
        if obs.management != "self_managed":
            current |= {f"control:{entity_id}": state for entity_id, state in obs.controls.items()}
        changed = False
        for key, state in current.items():
            if not _readable(state):
                continue
            assert state is not None
            before = self._last.get(key)
            self._last[key] = state
            if before is not None and before != state:
                self._changes.append(now)
                changed = True
        if changed and len(self._changes) >= SETTING_CHANGES:
            self._changes.clear()
            return REASON_SETTINGS
        return None

    def _behaviour_signal(self, obs: Observation) -> str | None:
        kind = self._behaviour_kind(obs)
        if kind is None:
            self._behaviour = None
            return None
        assert obs.soc_pct is not None
        last = self._behaviour
        if last is None or last[0] != kind or (obs.now - last[3]).total_seconds() > MAX_AGE_S:
            # A new stretch: another kind, or no look for a while (the readings in between are not known).
            self._behaviour = (kind, obs.now, obs.soc_pct, obs.now)
            return None
        _kind, since, soc_then, _seen = last
        self._behaviour = (kind, since, soc_then, obs.now)
        moved = obs.soc_pct - soc_then if kind == REASON_GRID_CHARGE else soc_then - obs.soc_pct
        if (obs.now - since).total_seconds() >= BEHAVIOUR_S and moved >= BEHAVIOUR_SOC_PCT:
            # One signal per stretch: the next needs a new stretch.
            self._behaviour = (kind, obs.now, obs.soc_pct, obs.now)
            return kind
        return None

    @staticmethod
    def _behaviour_kind(obs: Observation) -> str | None:
        if obs.management != "free" or obs.on_grid is False or not obs.measurement_ok:
            return None
        if obs.power_kw is None or obs.grid_kw is None or obs.soc_pct is None:
            return None
        if obs.battery_age_s is None or obs.battery_age_s > MAX_AGE_S:
            return None
        if obs.power_kw > BEHAVIOUR_KW and obs.grid_kw > BEHAVIOUR_KW:
            floor = obs.reserve_soc_pct or 0.0
            return None if obs.soc_pct <= floor + RESERVE_MARGIN_PCT else REASON_GRID_CHARGE
        if obs.power_kw < -BEHAVIOUR_KW and obs.grid_kw < -BEHAVIOUR_KW:
            return REASON_EXPORT_DISCHARGE
        return None

    # ---- storage and what it shows

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.as_dict(),
            "last_signal": None if self._last_signal is None else self._last_signal.isoformat(),
            "changes": [moment.isoformat() for moment in self._changes],
            "history": list(self.history),
            "counts": dict(self.counts),
        }

    @classmethod
    def from_dict(cls, data: Any) -> OutsideControlWatch:
        watch = cls()
        if not isinstance(data, Mapping):
            return watch
        verdict = data.get("verdict")
        if isinstance(verdict, Mapping) and verdict.get("active") is True:
            since = parse_aware_datetime(verdict.get("since"))
            reason = verdict.get("reason")
            source = verdict.get("source")
            if since is not None and isinstance(reason, str):
                watch.verdict = Verdict(
                    True, reason, since, source if isinstance(source, str) else None, verdict.get("told") is True
                )
        watch._last_signal = parse_aware_datetime(data.get("last_signal"))
        changes = data.get("changes")
        for raw in changes if isinstance(changes, list) else []:
            moment = parse_aware_datetime(raw)
            if moment is not None:
                watch._changes.append(moment)
        history = data.get("history")
        for row in history if isinstance(history, list) else []:
            if isinstance(row, Mapping):
                watch.history.append(dict(row))
        counts = data.get("counts")
        if isinstance(counts, Mapping):
            watch.counts = {
                str(key): value for key, value in counts.items() if isinstance(value, int) and not isinstance(value, bool)
            }
        return watch
