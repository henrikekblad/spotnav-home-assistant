"""The confirmed-write ledger: which control values SpotNav owns, and when someone else changed one.

The rule (Predbat's `control_ledger.py`, `plans/research_integrations/predbat_lowhanging_2026-11-13.md` item 1): a
write proves nothing; a write that reads back proves the device took it, and only then can a later difference mean
that somebody else changed it.

* `intend` records a write SpotNav makes. It counts (`confirmed`) only when the entity reads back the intended value,
  within the entity's step, before `READBACK_DEADLINE_S`; otherwise it is `unconfirmed` and nothing is owned.
* The owned value is the value **read back**, never the value sent, so every later comparison is between two reads
  of the same entity (same unit, same rounding).
* A read of `unavailable`/`unknown`, or one that is no number where a number is owned, is no evidence: it never
  counts as somebody else's change. It also restarts the divergence timer, so a gap is never part of the dwell.
* A read that differs from the owned value must keep differing for `MIN_DIVERGENCE_S` before it is "changed outside
  SpotNav". Then SpotNav gives that control up (`given_up`): it does not write it again, and does not write its
  snapshot back over the other system's value, until the part's next decision (`intend` or `release`). It never
  fights.
* Only a confirmed write may lead to a notification (`LedgerEvent.notify`); in this release nothing notifies, the
  events go to counters and the shadow log.

In shadow nothing is written, so nothing can be confirmed. `adopt` is the dry run: the value the control read when
the part decided becomes the baseline, as if it had been written and read back. That tells what shadow can tell:
whether the control can be read back at all (a service call or a control that reads `unavailable` could never be
confirmed), and whether its value changes while nothing writes it, which is another system driving the battery and
would have made SpotNav give the control up. It cannot tell whether the device would have accepted the write.

Pure: no Home Assistant import; times are aware datetimes from the caller. State survives a restart (`as_dict`,
`from_dict`); a divergence timer does not, so a restart never shortens the dwell.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from ..core.parse import number_from_state
from .shadow import MIN_DIVERGENCE_S, parse_aware_datetime

#: How long a write may take to read back before it counts as not taken.
READBACK_DEADLINE_S: Final = 60.0
#: Days of counters kept.
COUNTER_DAYS: Final = 30
#: Numbers closer than this are equal when the entity names no step.
_EXACT: Final = 1e-6
_NO_READING: Final = frozenset({"", "unavailable", "unknown", "none"})

Verdict = Literal["pending", "owned", "settling", "external", "unavailable"]
EventKind = Literal["confirmed", "unconfirmed", "external"]
#: What the counters count; `shadow_external` is a change seen during a dry run.
COUNTERS: Final[tuple[str, ...]] = ("confirmed", "unconfirmed", "external", "shadow_external")

Intended = float | str | bool


def _reading(read: str | None) -> str | None:
    """The read, or `None` when it is no reading."""
    if read is None or read.strip().lower() in _NO_READING:
        return None
    return read


def same(expected: Intended, read: str | None, step: float | None = None) -> bool | None:
    """Whether `read` is `expected`: a number within less than one `step` (the device may round either way), a
    switch's on/off, or the same text. `None` when the read is no evidence (no reading, or no number where one is
    expected)."""
    text = _reading(read)
    if text is None:
        return None
    if isinstance(expected, bool):
        lowered = text.lower()
        if lowered in ("on", "true"):
            return expected
        if lowered in ("off", "false"):
            return not expected
        return None
    if isinstance(expected, (int, float)):
        value = number_from_state(text)
        if value is None:
            return None
        if step is not None and step > 0:
            return abs(value - float(expected)) < step
        return abs(value - float(expected)) <= _EXACT
    expected_number = number_from_state(expected)
    if expected_number is not None:
        # An owned value is a read of the entity: compare two numeric reads as numbers.
        return same(expected_number, text, step)
    return text == expected


@dataclass(frozen=True, slots=True)
class Claim:
    """One control SpotNav wrote (or, in a dry run, adopted) and what it found since."""

    entity_id: str
    intended: Intended
    at: datetime
    deadline: datetime
    step: float | None = None
    #: The value read back: what SpotNav owns. `None` while the write is pending.
    owned: str | None = None
    owned_at: datetime | None = None
    diverging_since: datetime | None = None
    #: Changed outside SpotNav: the control is given up until the part's next decision.
    given_up: bool = False
    #: A dry run (shadow): the value found was adopted, nothing was written.
    adopted: bool = False
    verdict: Verdict = "pending"
    last_read: str | None = None

    def as_dict(self) -> dict[str, Any]:
        def iso(moment: datetime | None) -> str | None:
            return None if moment is None else moment.isoformat()

        return {
            "entity_id": self.entity_id,
            "intended": self.intended,
            "at": self.at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "step": self.step,
            "owned": self.owned,
            "owned_at": iso(self.owned_at),
            "diverging_since": iso(self.diverging_since),
            "given_up": self.given_up,
            "adopted": self.adopted,
            "verdict": self.verdict,
            "last_read": self.last_read,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Claim | None:
        """A stored claim; a divergence timer is not carried over a restart."""
        if not isinstance(data, Mapping):
            return None
        entity_id, intended = data.get("entity_id"), data.get("intended")
        at, deadline = parse_aware_datetime(data.get("at")), parse_aware_datetime(data.get("deadline"))
        if not isinstance(entity_id, str) or not isinstance(intended, (bool, int, float, str)) or at is None or deadline is None:
            return None
        if isinstance(intended, float) and not math.isfinite(intended):
            return None
        step, owned, verdict, last_read = data.get("step"), data.get("owned"), data.get("verdict"), data.get("last_read")
        owned_text = owned if isinstance(owned, str) else None
        given_up = data.get("given_up") is True
        return cls(
            entity_id=entity_id,
            intended=intended,
            at=at,
            deadline=deadline,
            step=float(step) if isinstance(step, (int, float)) and not isinstance(step, bool) and step > 0 else None,
            owned=owned_text,
            owned_at=parse_aware_datetime(data.get("owned_at")) if owned_text is not None else None,
            given_up=given_up,
            adopted=data.get("adopted") is True,
            verdict=(
                "external" if given_up
                else "pending" if owned_text is None
                else verdict if verdict in ("owned", "unavailable") else "owned"
            ),
            last_read=last_read if isinstance(last_read, str) else None,
        )


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    kind: EventKind
    entity_id: str
    at: datetime
    intended: Intended
    #: The value owned (for `external`, the value SpotNav owned before).
    owned: str | None
    read: str | None
    adopted: bool
    #: Whether a user may be told (only ever after a confirmed write); nothing notifies in this release.
    notify: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entity_id": self.entity_id,
            "at": self.at.isoformat(),
            "intended": self.intended,
            "owned": self.owned,
            "read": self.read,
            "adopted": self.adopted,
            "notify": self.notify,
        }


class Ledger:
    """The claims by entity and the counters per local day."""

    def __init__(self) -> None:
        self._claims: dict[str, Claim] = {}
        self._counters: dict[str, dict[str, int]] = {}

    # --- the claims ---

    def claim(self, entity_id: str) -> Claim | None:
        return self._claims.get(entity_id)

    def entity_ids(self) -> list[str]:
        """The controls claimed now."""
        return sorted(self._claims)

    def verdict(self, entity_id: str) -> Verdict | None:
        claim = self._claims.get(entity_id)
        return None if claim is None else claim.verdict

    def owned(self, entity_id: str) -> str | None:
        claim = self._claims.get(entity_id)
        return None if claim is None or claim.given_up else claim.owned

    def given_up(self, entity_id: str) -> bool:
        claim = self._claims.get(entity_id)
        return claim is not None and claim.given_up

    def may_write(self, entity_id: str) -> bool:
        """Whether SpotNav may write the control now: not once it was changed outside SpotNav, until the part's
        next decision."""
        return not self.given_up(entity_id)

    def intend(
        self, entity_id: str, value: Intended, now: datetime, *, step: float | None = None,
        deadline_s: float = READBACK_DEADLINE_S,
    ) -> None:
        """A write SpotNav makes now (the part's decision): any earlier claim on the control is replaced."""
        self._claims[entity_id] = Claim(entity_id, value, now, now + timedelta(seconds=deadline_s), step)

    def adopt(self, entity_id: str, read: str | None, now: datetime, *, step: float | None = None) -> bool:
        """The dry run of a write: the value the control reads now becomes the baseline, as if written and read
        back. `False` (and no claim) when the read is no reading: such a control could not be confirmed."""
        text = _reading(read)
        if text is None:
            self._claims.pop(entity_id, None)
            return False
        self._claims[entity_id] = Claim(
            entity_id, text, now, now, step, owned=text, owned_at=now, adopted=True, verdict="owned", last_read=text
        )
        return True

    def release(self, entity_id: str) -> Claim | None:
        """The part's next decision gives the control back (a restore, the part stops): the claim ends."""
        return self._claims.pop(entity_id, None)

    def observe(self, entity_id: str, read: str | None, now: datetime) -> LedgerEvent | None:
        """A read of the control; an event when a write is confirmed, not taken, or the control was changed outside
        SpotNav."""
        claim = self._claims.get(entity_id)
        if claim is None:
            return None
        if claim.owned is None:
            return self._pending(claim, read, now)
        if claim.given_up:
            self._claims[entity_id] = replace(claim, last_read=read)
            return None
        match = same(claim.owned, read, claim.step)
        if match is None:
            self._claims[entity_id] = replace(claim, diverging_since=None, verdict="unavailable", last_read=read)
            return None
        if match:
            self._claims[entity_id] = replace(claim, diverging_since=None, verdict="owned", last_read=read)
            return None
        since = claim.diverging_since or now
        if (now - since).total_seconds() < MIN_DIVERGENCE_S:
            self._claims[entity_id] = replace(claim, diverging_since=since, verdict="settling", last_read=read)
            return None
        self._claims[entity_id] = replace(
            claim, diverging_since=None, given_up=True, verdict="external", last_read=read
        )
        self._count(now, "shadow_external" if claim.adopted else "external")
        return LedgerEvent(
            "external", entity_id, now, claim.intended, claim.owned, read, claim.adopted, notify=not claim.adopted
        )

    def give_up(self, entity_id: str, read: str | None, now: datetime, intended: Intended) -> LedgerEvent:
        """The control was found changed just before a write (the caller compared it with what was expected): it is
        someone else's now, given up at once, without the dwell a later read needs."""
        claim = self._claims.get(entity_id)
        owned = None if claim is None else claim.owned
        adopted = claim is not None and claim.adopted
        if claim is not None:
            self._claims[entity_id] = replace(
                claim, diverging_since=None, given_up=True, verdict="external", last_read=read
            )
        self._count(now, "shadow_external" if adopted else "external")
        return LedgerEvent("external", entity_id, now, intended, owned, read, adopted, notify=not adopted)

    def _pending(self, claim: Claim, read: str | None, now: datetime) -> LedgerEvent | None:
        match = same(claim.intended, read, claim.step)
        if match:
            self._claims[claim.entity_id] = replace(claim, owned=read, owned_at=now, verdict="owned", last_read=read)
            self._count(now, "confirmed")
            return LedgerEvent("confirmed", claim.entity_id, now, claim.intended, read, read, claim.adopted)
        if now >= claim.deadline:
            del self._claims[claim.entity_id]
            self._count(now, "unconfirmed")
            return LedgerEvent("unconfirmed", claim.entity_id, now, claim.intended, None, read, claim.adopted)
        self._claims[claim.entity_id] = replace(claim, last_read=read)
        return None

    # --- the counters ---

    def _count(self, now: datetime, key: str) -> None:
        day = now.date().isoformat()
        counts = self._counters.setdefault(day, dict.fromkeys(COUNTERS, 0))
        counts[key] = counts.get(key, 0) + 1
        if len(self._counters) > COUNTER_DAYS:
            for old in sorted(self._counters)[:-COUNTER_DAYS]:
                del self._counters[old]

    def counters(self, day: str) -> dict[str, int]:
        return dict.fromkeys(COUNTERS, 0) | self._counters.get(day, {})

    # --- storage ---

    def as_dict(self) -> dict[str, Any]:
        return {
            "claims": {entity_id: claim.as_dict() for entity_id, claim in sorted(self._claims.items())},
            "counters": {day: dict(counts) for day, counts in sorted(self._counters.items())},
        }

    @classmethod
    def from_dict(cls, data: Any) -> Ledger:
        """A ledger from storage; broken rows are dropped."""
        ledger = cls()
        if not isinstance(data, Mapping):
            return ledger
        claims = data.get("claims")
        if isinstance(claims, Mapping):
            for entity_id, row in claims.items():
                claim = Claim.from_dict(row)
                if claim is not None and claim.entity_id == entity_id:
                    ledger._claims[entity_id] = claim
        counters = data.get("counters")
        if isinstance(counters, Mapping):
            for day, counts in sorted(counters.items())[-COUNTER_DAYS:]:
                if isinstance(day, str) and isinstance(counts, Mapping):
                    ledger._counters[day] = {
                        key: value for key, value in counts.items()
                        if key in COUNTERS and isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    }
        return ledger
