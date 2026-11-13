"""Shadow mode's book: parts stay in shadow, the log is bounded and the book survives a round trip."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.spotnav.battery import shadow
from custom_components.spotnav.battery.shadow import (
    LOG_MAX_AGE,
    LOG_MAX_ENTRIES,
    PART_HOLD_FOR_CAR,
    PARTS,
    ShadowBook,
    ShadowEntry,
)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _entry(at: datetime, reason: str = "test") -> ShadowEntry:
    return ShadowEntry(at, PART_HOLD_FOR_CAR, "would_write", reason, "number.limit", 0.0, {"power_kw": -4.6})


def test_every_part_starts_in_shadow_and_writes_nothing() -> None:
    book = ShadowBook()

    assert all(book.state(part) == "shadow" for part in PARTS)
    assert not any(book.writes(part) for part in PARTS)


def test_no_part_can_be_turned_on_in_this_release() -> None:
    book = ShadowBook()

    with pytest.raises(ValueError):
        book.set_state(PART_HOLD_FOR_CAR, "on")
    with pytest.raises(KeyError):
        book.set_state("no_such_part", "shadow")


def test_a_stored_on_loads_as_shadow() -> None:
    book = ShadowBook.from_dict({"parts": {PART_HOLD_FOR_CAR: "on"}}, NOW)

    assert book.state(PART_HOLD_FOR_CAR) == "shadow" and not book.writes(PART_HOLD_FOR_CAR)


def test_all_back_to_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow, "ON_REACHABLE", True)
    book = ShadowBook()
    book.set_state(PART_HOLD_FOR_CAR, "on")
    assert book.writes(PART_HOLD_FOR_CAR)

    assert book.all_to_shadow() == [PART_HOLD_FOR_CAR]
    assert book.state(PART_HOLD_FOR_CAR) == "shadow" and book.all_to_shadow() == []


def test_the_log_keeps_at_most_its_count() -> None:
    book = ShadowBook()
    for minute in range(LOG_MAX_ENTRIES + 25):
        book.record(_entry(NOW + timedelta(minutes=minute), f"r{minute}"))

    entries = book.entries
    assert len(entries) == LOG_MAX_ENTRIES
    assert entries[0].reason == "r25" and entries[-1].reason == f"r{LOG_MAX_ENTRIES + 24}"


def test_the_log_forgets_what_is_older_than_its_age() -> None:
    book = ShadowBook()
    book.record(_entry(NOW - LOG_MAX_AGE - timedelta(minutes=1), "old"))
    book.record(_entry(NOW, "new"))

    assert [entry.reason for entry in book.entries] == ["new"]


def test_the_book_survives_a_round_trip_through_json() -> None:
    book = ShadowBook()
    book.set_memory(PART_HOLD_FOR_CAR, {"held": True, "snapshot": "23.04"})
    book.record(_entry(NOW - timedelta(minutes=2), "first"))
    book.record(_entry(NOW, "second"))

    stored = json.loads(json.dumps(book.as_dict()))
    loaded = ShadowBook.from_dict(stored, NOW)

    assert loaded.as_dict() == book.as_dict()
    assert loaded.memory(PART_HOLD_FOR_CAR) == {"held": True, "snapshot": "23.04"}


def test_a_restart_prunes_what_aged_out_while_it_was_down() -> None:
    book = ShadowBook()
    book.record(_entry(NOW, "kept"))

    loaded = ShadowBook.from_dict(book.as_dict(), NOW + LOG_MAX_AGE + timedelta(seconds=1))

    assert loaded.entries == []


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"parts": "x", "memory": 3, "log": {}},
        {"log": [{"at": "2026-11-13T12:00:00", "part": PART_HOLD_FOR_CAR, "action": "would_write", "reason": "naive"}]},
        {"log": [{"at": NOW.isoformat(), "part": "gone", "action": "would_write", "reason": "x"}]},
        {"log": [{"at": NOW.isoformat(), "part": PART_HOLD_FOR_CAR, "action": "wrote", "reason": "x"}]},
        {"log": ["text"]},
    ],
)
def test_broken_storage_loads_an_empty_book(data: object) -> None:
    book = ShadowBook.from_dict(data, NOW)

    assert book.entries == [] and book.state(PART_HOLD_FOR_CAR) == "shadow"
