"""A charger's phase switching between one and three phases, as the charging controller keeps it.

Pure: no Home Assistant imports, every clock is passed in. The solar controller decides a switch
(`site/solar_surplus.py`); the charging controller carries it out through the adapter's `PhaseSwitch` and keeps
what follows here, per plug-in:

* `switched_phases`: 1 while the charger is forced to one phase by us, else `None` (on the phases it is wired for).
  It is what the charger's read-back confirmed, never what was only asked for: a switch to one phase counts once it
  is confirmed, a switch to three at once (three is the conservative answer for the sun's current and for the site's
  reckoning both).
* The settle window: for `SETTLE_S` after a switch was sent the charge may show nothing (the charger interrupts it
  itself), and nothing that would read that as the car ending the charge, a start that did not take, a phase
  measurement or a new phase decision acts on it (`settling`).
* The confirmation: the read-back must show the new phases within `CONFIRM_S`. If it does not, switching is given
  up for this plug-in (`given_up`): the charge goes on, by the rules without switching, on whatever phases the
  read-back shows (unknown: three).
* The switch budget's count and the last switch, for the diagnostics and a rebuilt solar controller; all of it
  starts afresh with a new plug-in (`new_plug_in`), never on a passing reading of no car.

`switched_phases`, the count, the last switch and the give-up are stored with the charger's record (`to_store`,
`from_store`), so a restart knows the charger is forced to one phase; at the restore the read-back wins over the
stored value (`reconcile`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

#: For this long (seconds) after a switch the charge is settling: not charging is not the car ending it.
SETTLE_S: Final = 90.0
#: The read-back must show a switch within this long (seconds), or switching is given up for the plug-in.
CONFIRM_S: Final = 30.0

#: The key the charger's record keeps this under (only while there is something to keep).
STORE_KEY: Final = "phase_switching"


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(slots=True)
class PhaseSwitchState:
    """One charger's phase switching, per plug-in (module docstring)."""

    switched_phases: Literal[1] | None = None
    switches: int = 0
    last_switch_at: datetime | None = None
    given_up: bool = False
    #: Switches given up since this controller started (diagnostics only, in memory).
    given_up_count: int = 0
    #: Since when a switch settles (in memory: a restart does not wait it out).
    sent_at: datetime | None = None
    #: The phases a switch on its way asked for, and by when the read-back must show them (in memory).
    confirming: Literal[1, 3] | None = None
    confirm_by: datetime | None = None

    def settling(self, now: datetime) -> bool:
        """Whether a switch sent less than `SETTLE_S` ago settles."""
        return self.sent_at is not None and now < self.sent_at + timedelta(seconds=SETTLE_S)

    def settle_left_s(self, now: datetime) -> float:
        """How long (seconds) the settle still runs, 0 when it does not."""
        if self.sent_at is None:
            return 0.0
        return max(0.0, (self.sent_at + timedelta(seconds=SETTLE_S) - now).total_seconds())

    def sent(self, phases: Literal[1, 3], now: datetime, *, counted: bool) -> None:
        """A switch to `phases` went out: it settles and waits for its confirmation. `counted` against the budget."""
        self.sent_at = now
        self.confirming = phases
        self.confirm_by = now + timedelta(seconds=CONFIRM_S)
        if phases == 3:
            # Three at once, before the read-back: the conservative reckoning for the sun's current and the site's.
            self.switched_phases = None
        if counted:
            self.switches += 1
            self.last_switch_at = now

    def counted_attempt(self, now: datetime) -> None:
        """A decided switch that did not go out: it spends the budget all the same."""
        self.switches += 1
        self.last_switch_at = now

    def confirm(self, read_back: int | None, now: datetime) -> Literal["confirmed", "given_up"] | None:
        """Look at the read-back for a switch on its way: `confirmed` once it shows the phases asked for, `given_up`
        once `CONFIRM_S` passed without (`give_up`), `None` while waiting or with nothing on its way."""
        target = self.confirming
        if target is None:
            return None
        if read_back == target:
            self.switched_phases = 1 if target == 1 else None
            self.confirming = None
            self.confirm_by = None
            return "confirmed"
        if self.confirm_by is not None and now >= self.confirm_by:
            self.give_up(read_back)
            return "given_up"
        return None

    def give_up(self, read_back: int | None) -> None:
        """Switching is over for this plug-in: the charge is on what the read-back shows (unknown: three)."""
        if not self.given_up:
            # One give-up per plug-in: setting three back after it does not count again.
            self.given_up_count += 1
        self.given_up = True
        self.confirming = None
        self.confirm_by = None
        self.switched_phases = 1 if read_back == 1 else None

    def reconcile(self, read_back: int | None) -> bool:
        """The read-back wins over what is kept, while no switch is on its way and it says 1 or 3. Returns whether
        `switched_phases` changed."""
        if self.confirming is not None or read_back not in (1, 3):
            return False
        switched: Literal[1] | None = 1 if read_back == 1 else None
        if switched == self.switched_phases:
            return False
        self.switched_phases = switched
        return True

    def new_plug_in(self) -> None:
        """A new plug-in (or an unplug): the whole budget again, and switching no longer given up. What the charger
        is set to stays: nothing is sent at an unplug, so a charger forced to one phase is still forced."""
        self.switches = 0
        self.last_switch_at = None
        self.given_up = False

    def facts(self, now: datetime, *, allowed: bool) -> dict[str, Any]:
        """For the diagnostics."""
        return {
            "allowed": allowed,
            "switched_phases": self.switched_phases,
            "switches_this_plug_in": self.switches,
            "last_switch_at": None if self.last_switch_at is None else self.last_switch_at.isoformat(),
            "settling": self.settling(now),
            "given_up": self.given_up,
            "given_up_count": self.given_up_count,
        }

    def to_store(self) -> dict[str, Any] | None:
        """What the charger's record keeps, `None` when there is nothing to keep (a charger that never switched)."""
        if self.switched_phases is None and self.switches == 0 and not self.given_up:
            return None
        return {
            "switched_phases": self.switched_phases,
            "switches": self.switches,
            "last_switch_at": None if self.last_switch_at is None else self.last_switch_at.isoformat(),
            "given_up": self.given_up,
        }

    @classmethod
    def from_store(cls, record: Any) -> PhaseSwitchState:
        """Read back what `to_store` kept; storage is untrusted, so a field of the wrong kind reads as absent."""
        if not isinstance(record, dict):
            return cls()
        switches = record.get("switches")
        return cls(
            switched_phases=1 if record.get("switched_phases") == 1 else None,
            switches=switches if isinstance(switches, int) and not isinstance(switches, bool) and switches > 0 else 0,
            last_switch_at=_parse_aware(record.get("last_switch_at")),
            given_up=record.get("given_up") is True,
        )
