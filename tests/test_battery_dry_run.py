"""The ledger and the monitor run dry beside the shadow hold: read-back possible or not per role the executor's hold
would write, a control changed outside SpotNav, the monitor's baseline, and nothing ever counted as a failure."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL
from custom_components.spotnav.battery.dry_run import HoldDryRun, site_cap_kw
from custom_components.spotnav.battery.executor import RoleRead
from custom_components.spotnav.battery.facts import BatteryFacts, Management
from custom_components.spotnav.battery.hold import HoldDecision
from custom_components.spotnav.battery.ledger import Ledger
from custom_components.spotnav.battery.monitor import GRACE_S, READING_GAP_S, Monitor
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S
from tests.battery_control_fakes import SIGENERGY_STATES, FakeBattery

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LIMIT = "number.sigenergy_discharge_limit"
SWITCH = "switch.sigenergy_remote_ems"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def reads(**states: str) -> dict[str, RoleRead]:
    return FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES) | states).reads()


def write(**states: str) -> HoldDecision:
    """The executor's Sigenergy hold as the part logs it, with each role's read."""
    found = reads(**states)
    rows: list[dict[str, Any]] = [
        {"role": role, "entity_id": found[role].entity_id, "value": value, "read": found[role].state}
        for role, value in (("discharge_limit", 0.0), ("remote_ems", True), ("control_mode", "Maximum Self Consumption"))
    ]
    return HoldDecision("would_write", "battery_discharges_into_grid_charge", LIMIT, {}, rows[0]["read"], {"writes": rows})


RESTORE = HoldDecision("would_restore", "car_not_charging_from_grid", LIMIT, {"discharge_limit": 23.04}, "23.04", {})


def facts(power_kw: float = -4.6) -> BatteryFacts:
    return BatteryFacts(
        brand="sigenergy",
        power_kw=power_kw,
        soc_pct=60.0,
        rated_charge_kw=23.04,
        rated_discharge_kw=23.04,
        management=Management("free", "self_consumption"),
    )


def observe(dry: HoldDryRun, seconds: float, found: dict[str, RoleRead] | None = None, held: bool = True):
    return dry.observe(
        facts(), reads() if found is None else found, held, at(seconds), age_s=5.0, headroom_a=10.0,
        volts_between_phases=400.0,
    )


def test_a_would_be_hold_adopts_each_role_found_and_says_a_read_back_is_possible() -> None:
    dry = HoldDryRun(Ledger(), Monitor())

    decision, entries = dry.decided(write(), facts(), at(0))

    assert decision.detail["read_back"] == "possible" and entries == []
    # The mode select is unavailable until Remote EMS is on: it can be read back only after a real write.
    assert decision.detail["read_back_roles"] == {"discharge_limit": "possible", "remote_ems": "possible", "control_mode": "idle"}
    assert dry.ledger.owned(LIMIT) == "23.04" and dry.ledger.owned(SWITCH) == "off"
    assert dry.monitor.watch is not None and dry.monitor.watch.command.written is False


def test_a_sigenergy_limit_that_reads_unknown_is_unset_and_never_adopted() -> None:
    dry = HoldDryRun(Ledger(), Monitor())

    decision, _ = dry.decided(write(discharge_limit="unknown"), facts(), at(0))

    assert decision.detail["read_back_roles"]["discharge_limit"] == "unset"
    assert decision.detail["read_back"] == "possible" and dry.ledger.claim(LIMIT) is None
    # Later reads of a value there are not someone else's change: nothing was owned.
    for seconds in range(10, 1000, 10):
        assert [e for e in observe(dry, seconds, reads(discharge_limit="23.04")) if e.action == "changed_outside"] == []


def test_a_role_that_reads_nothing_could_never_confirm_a_write() -> None:
    dry = HoldDryRun(Ledger(), Monitor())

    decision, _ = dry.decided(write(discharge_limit="unavailable"), facts(), at(0))

    assert decision.detail["read_back"] == "not_possible:no_reading:discharge_limit"
    assert dry.ledger.claim(LIMIT) is None


def test_a_role_without_an_entity_has_nothing_to_read_back() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    decision = write()
    rows = [dict(row) for row in decision.detail["writes"]]
    rows[0]["entity_id"] = None

    decided, _ = dry.decided(replace(decision, detail={"writes": rows}), facts(), at(0))

    assert decided.detail["read_back"] == "not_possible:no_entity:discharge_limit"


def test_the_monitor_shows_the_baseline_once_and_counts_nothing() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(write(), facts(), at(0))

    logged = []
    for i in range(5):
        logged += observe(dry, GRACE_S + i * READING_GAP_S)

    [entry] = logged
    assert entry.action == "would_judge" and entry.reason == "baseline_no_effect"
    assert entry.detail["dry_run"] is True and len(entry.detail["readings"]) == 3
    assert dry.monitor.counters("2026-11-13") == {"writes": 0, "no_effect": 0, "partial": 0}


def test_a_role_changed_while_nothing_writes_it_is_logged_and_its_restore_skipped() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(write(), facts(), at(0))

    assert observe(dry, 10, reads(discharge_limit="5.00")) == []
    [entry] = [
        e for e in observe(dry, 10 + MIN_DIVERGENCE_S, reads(discharge_limit="5.00")) if e.action == "changed_outside"
    ]

    assert entry.reason == "hold_control_changed_outside_spotnav" and entry.read == "5.00" and entry.entity_id == LIMIT
    assert entry.detail["owned"] == "23.04" and entry.detail["notify"] is False
    decision, judged = dry.decided(RESTORE, facts(), at(400))
    assert decision.detail["restore"] == "skipped:changed_outside_spotnav" and decision.detail["skipped"] == [LIMIT]
    assert dry.ledger.entity_ids() == []
    assert [e.reason for e in judged] == ["not_judgeable:too_few_readings"]
    assert dry.ledger.counters("2026-11-13")["shadow_external"] == 1


def test_an_unavailable_or_unknown_role_during_a_hold_is_no_change() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(write(), facts(), at(0))

    for seconds in range(10, 1000, 10):
        gap = "unavailable" if seconds % 20 else "unknown"
        found = reads(discharge_limit=gap, remote_ems=gap)
        assert [e for e in observe(dry, seconds, found) if e.action == "changed_outside"] == []
    # A role no longer found at all is no evidence either.
    assert [e for e in observe(dry, 1000, {}) if e.action == "changed_outside"] == []

    decision, _ = dry.decided(RESTORE, facts(), at(1010))
    assert "restore" not in decision.detail


def test_a_restore_inside_the_grace_says_the_monitor_could_not_judge() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(write(), facts(), at(0))

    _decision, [entry] = dry.decided(RESTORE, facts(), at(120))

    assert entry.action == "would_judge" and entry.reason == "not_judgeable:ended_within_grace"


def test_no_claim_or_watch_outlives_the_hold() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(write(), facts(), at(0))

    entries = observe(dry, 10, held=False)

    assert dry.ledger.entity_ids() == [] and dry.monitor.watch is None
    assert [e.reason for e in entries] == ["not_judgeable:ended_within_grace"]


def test_the_fuse_headroom_as_kw() -> None:
    assert site_cap_kw(10.0, 400.0) == 10.0 * 3**0.5 * 0.4
    assert site_cap_kw(None, 400.0) is None and site_cap_kw(-3.0, 400.0) == 0.0
