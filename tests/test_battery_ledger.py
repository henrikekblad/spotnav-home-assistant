"""The confirmed-write ledger: read-back rules, gaps that are no evidence, the divergence timer, the dry run and a
restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.spotnav.battery.ledger import READBACK_DEADLINE_S, Ledger, same
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LIMIT = "number.plant_max_discharge"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_a_number_is_the_same_within_less_than_one_step() -> None:
    assert same(0.0, "0.04", 0.1) is True
    assert same(0.0, "0.1", 0.1) is False
    assert same(4.8, "4.8") is True and same(4.8, "4.81") is False
    assert same("23.04", "23.040") is True
    assert same(True, "on") is True and same(False, "on") is False
    assert same("self_consumption", "self_consumption") is True and same("self_consumption", "ai_mode") is False


def test_no_reading_and_no_number_are_no_evidence() -> None:
    for read in (None, "unavailable", "unknown", "", "nan", "inf", "garbage"):
        assert same(0.0, read, 0.1) is None
    assert same(True, "unavailable") is None


def test_a_write_counts_only_when_it_reads_back() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)

    assert ledger.observe(LIMIT, "4.8", at(5)) is None
    assert ledger.verdict(LIMIT) == "pending" and ledger.owned(LIMIT) is None
    event = ledger.observe(LIMIT, "0.00", at(10))

    assert event is not None and event.kind == "confirmed" and event.owned == "0.00"
    assert ledger.owned(LIMIT) == "0.00" and ledger.verdict(LIMIT) == "owned"
    assert ledger.counters("2026-11-13")["confirmed"] == 1


def test_the_owned_value_is_the_value_read_back_not_the_value_sent() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.5, at(0), step=0.1)

    ledger.observe(LIMIT, "0.46", at(5))

    assert ledger.owned(LIMIT) == "0.46"
    # Later reads are compared with the read-back value.
    assert ledger.observe(LIMIT, "0.46", at(600)) is None and ledger.verdict(LIMIT) == "owned"


def test_a_write_that_never_reads_back_is_not_taken_and_nothing_is_owned() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)

    assert ledger.observe(LIMIT, "unavailable", at(30)) is None
    event = ledger.observe(LIMIT, "4.8", at(READBACK_DEADLINE_S))

    assert event is not None and event.kind == "unconfirmed" and event.notify is False
    assert ledger.claim(LIMIT) is None and ledger.owned(LIMIT) is None
    assert ledger.counters("2026-11-13")["unconfirmed"] == 1


def test_an_unavailable_read_never_counts_as_someone_elses_change() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)
    ledger.observe(LIMIT, "0", at(5))

    for seconds in range(10, 2000, 10):
        assert ledger.observe(LIMIT, "unavailable", at(seconds)) is None

    assert ledger.verdict(LIMIT) == "unavailable" and not ledger.given_up(LIMIT)
    assert ledger.observe(LIMIT, "0", at(2000)) is None and ledger.verdict(LIMIT) == "owned"


def test_a_divergence_counts_only_after_it_lasts_the_minimum() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)
    ledger.observe(LIMIT, "0", at(5))

    assert ledger.observe(LIMIT, "5", at(100)) is None and ledger.verdict(LIMIT) == "settling"
    assert ledger.observe(LIMIT, "6", at(100 + MIN_DIVERGENCE_S - 1)) is None
    event = ledger.observe(LIMIT, "6", at(100 + MIN_DIVERGENCE_S))

    assert event is not None and event.kind == "external" and event.owned == "0" and event.read == "6"
    assert event.notify is True
    assert ledger.given_up(LIMIT) and not ledger.may_write(LIMIT) and ledger.owned(LIMIT) is None
    assert ledger.counters("2026-11-13")["external"] == 1


def test_a_short_divergence_that_returns_is_no_change() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)
    ledger.observe(LIMIT, "0", at(5))

    ledger.observe(LIMIT, "5", at(100))
    ledger.observe(LIMIT, "0", at(200))
    assert ledger.observe(LIMIT, "5", at(300)) is None
    assert ledger.observe(LIMIT, "5", at(300 + MIN_DIVERGENCE_S - 1)) is None

    assert ledger.verdict(LIMIT) == "settling"


def test_a_gap_restarts_the_divergence_timer() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)
    ledger.observe(LIMIT, "0", at(5))

    ledger.observe(LIMIT, "5", at(100))
    ledger.observe(LIMIT, "unknown", at(200))
    assert ledger.observe(LIMIT, "5", at(100 + MIN_DIVERGENCE_S)) is None
    assert ledger.observe(LIMIT, "5", at(100 + 2 * MIN_DIVERGENCE_S)) is not None


def test_a_given_up_control_stays_given_up_until_the_parts_next_decision() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)
    ledger.observe(LIMIT, "0", at(5))
    ledger.observe(LIMIT, "5", at(10))
    ledger.observe(LIMIT, "5", at(10 + MIN_DIVERGENCE_S))

    # It never fights: back at the old value or not, the control stays given up.
    assert ledger.observe(LIMIT, "0", at(1000)) is None and ledger.given_up(LIMIT)
    ledger.intend(LIMIT, 0.0, at(2000), step=0.01)
    assert ledger.may_write(LIMIT) and ledger.verdict(LIMIT) == "pending"
    ledger.release(LIMIT)
    assert ledger.claim(LIMIT) is None and ledger.may_write(LIMIT)


def test_a_dry_run_adopts_the_value_found_and_never_notifies() -> None:
    ledger = Ledger()

    assert ledger.adopt(LIMIT, "23.04", at(0), step=0.01) is True
    assert ledger.owned(LIMIT) == "23.04" and ledger.claim(LIMIT).adopted  # type: ignore[union-attr]
    assert ledger.counters("2026-11-13")["confirmed"] == 0
    ledger.observe(LIMIT, "10", at(10))
    event = ledger.observe(LIMIT, "10", at(10 + MIN_DIVERGENCE_S))

    assert event is not None and event.kind == "external" and event.adopted and event.notify is False
    counts = ledger.counters("2026-11-13")
    assert counts["shadow_external"] == 1 and counts["external"] == 0


def test_a_control_that_reads_nothing_cannot_be_adopted() -> None:
    ledger = Ledger()

    assert ledger.adopt(LIMIT, "unavailable", at(0)) is False
    assert ledger.claim(LIMIT) is None


def test_the_ledger_survives_a_restart_without_its_divergence_timer() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)
    ledger.observe(LIMIT, "0", at(5))
    ledger.observe(LIMIT, "5", at(100))
    ledger.intend("select.plant_mode", "self_consumption", at(100))

    restored = Ledger.from_dict(ledger.as_dict())

    assert restored.owned(LIMIT) == "0" and restored.claim(LIMIT).step == 0.01  # type: ignore[union-attr]
    assert restored.claim(LIMIT).diverging_since is None  # type: ignore[union-attr]
    # The dwell starts again after the restart: a restart never shortens it.
    assert restored.observe(LIMIT, "5", at(100 + MIN_DIVERGENCE_S)) is None
    assert restored.verdict(LIMIT) == "settling"
    assert restored.verdict("select.plant_mode") == "pending"
    assert restored.counters("2026-11-13")["confirmed"] == 1


def test_a_pending_write_whose_deadline_passed_while_down_is_not_taken() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.01)

    restored = Ledger.from_dict(ledger.as_dict())
    event = restored.observe(LIMIT, "4.8", at(600))

    assert event is not None and event.kind == "unconfirmed"


def test_broken_storage_is_dropped() -> None:
    restored = Ledger.from_dict(
        {
            "claims": {
                LIMIT: {"entity_id": LIMIT, "intended": 0.0, "at": "not a time", "deadline": T0.isoformat()},
                "x": {"entity_id": "y", "intended": 1, "at": T0.isoformat(), "deadline": T0.isoformat()},
                "z": "nope",
            },
            "counters": {"2026-11-13": {"confirmed": -1, "external": 2, "bogus": 3}, "2026-11-12": "nope"},
        }
    )

    assert restored.as_dict()["claims"] == {}
    assert restored.counters("2026-11-13") == {"confirmed": 0, "unconfirmed": 0, "external": 2, "shadow_external": 0}
    assert Ledger.from_dict(None).as_dict() == {"claims": {}, "counters": {}}


def test_counters_keep_thirty_days() -> None:
    ledger = Ledger()
    for day in range(40):
        ledger.intend(LIMIT, 0.0, at(day * 86400), step=0.01)
        ledger.observe(LIMIT, "0", at(day * 86400 + 1))

    assert len(ledger.as_dict()["counters"]) == 30


# --- Sigenergy 2.0.1 shows a never-set limit as `unknown` -------------------------------------------------------------


def test_unknown_read_back_after_our_own_write_is_not_confirmed_yet_nor_someone_elses_change() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.001)

    for seconds in range(5, int(READBACK_DEADLINE_S), 5):
        assert ledger.observe(LIMIT, "unknown", at(seconds)) is None
        assert ledger.verdict(LIMIT) == "pending" and ledger.owned(LIMIT) is None
    # Never a success: at the deadline it is a write not taken, and no change by someone else.
    event = ledger.observe(LIMIT, "unknown", at(READBACK_DEADLINE_S))
    assert event is not None and event.kind == "unconfirmed"
    assert ledger.counters("2026-11-13") | {} == {"confirmed": 0, "unconfirmed": 1, "external": 0, "shadow_external": 0}


def test_unknown_after_a_confirmed_write_is_a_gap_never_a_change_by_someone_else() -> None:
    ledger = Ledger()
    ledger.intend(LIMIT, 0.0, at(0), step=0.001)
    ledger.observe(LIMIT, "0", at(5))

    for seconds in range(10, 2000, 10):
        assert ledger.observe(LIMIT, "unknown", at(seconds)) is None

    assert ledger.verdict(LIMIT) == "unavailable" and not ledger.given_up(LIMIT) and ledger.owned(LIMIT) == "0"


def test_unknown_is_never_adopted_as_a_baseline() -> None:
    ledger = Ledger()

    assert ledger.adopt(LIMIT, "unknown", at(0)) is False and ledger.claim(LIMIT) is None
