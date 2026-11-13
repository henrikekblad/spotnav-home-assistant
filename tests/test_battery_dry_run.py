"""The ledger and the monitor run dry beside the shadow hold: read-back possible or not, a control changed outside
SpotNav, the monitor's baseline, and nothing ever counted as a failure."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.spotnav.battery.adapters import SIGENERGY
from custom_components.spotnav.battery.dry_run import HoldDryRun, site_cap_kw
from custom_components.spotnav.battery.facts import BatteryFacts, ControlStatus, Management
from custom_components.spotnav.battery.hold import HoldDecision
from custom_components.spotnav.battery.ledger import Ledger
from custom_components.spotnav.battery.monitor import GRACE_S, READING_GAP_S, Monitor
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LIMIT = "number.sigen_plant_ess_max_discharging_limit"
WRITE = HoldDecision("would_write", "battery_discharges_into_grid_charge", LIMIT, 0.0, "23.04", {})
RESTORE = HoldDecision("would_restore", "car_not_charging_from_grid", LIMIT, 23.04, "23.04", {})


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def facts(current: str | None = "23.04", power_kw: float = -4.6, available: bool = True) -> BatteryFacts:
    assert SIGENERGY.hold is not None
    return BatteryFacts(
        power_kw=power_kw,
        soc_pct=60.0,
        rated_charge_kw=23.04,
        rated_discharge_kw=23.04,
        management=Management("free", "self_consumption"),
        hold=ControlStatus(
            SIGENERGY.hold, LIMIT, found=True, enabled=True, available=available, current=current, step=0.01
        ),
    )


def observe(dry: HoldDryRun, seconds: float, battery: BatteryFacts, held: bool = True):
    return dry.observe(battery, held, at(seconds), age_s=5.0, headroom_a=10.0, volts_between_phases=400.0)


def test_a_would_be_hold_adopts_the_value_found_and_says_a_read_back_is_possible() -> None:
    dry = HoldDryRun(Ledger(), Monitor())

    decision, entries = dry.decided(WRITE, facts(), at(0))

    assert decision.detail["read_back"] == "possible" and entries == []
    assert dry.ledger.owned(LIMIT) == "23.04"
    assert dry.monitor.watch is not None and dry.monitor.watch.command.written is False


def test_a_control_that_reads_nothing_could_never_confirm_a_write() -> None:
    dry = HoldDryRun(Ledger(), Monitor())

    decision, _ = dry.decided(WRITE, facts(current="unavailable", available=False), at(0))

    assert decision.detail["read_back"] == "not_possible:no_reading"
    assert dry.ledger.claim(LIMIT) is None


def test_a_service_control_has_no_entity_to_read_back() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    battery = replace(facts(), hold=replace(facts().hold, entity_id=None))  # type: ignore[arg-type]

    decision, _ = dry.decided(WRITE, battery, at(0))

    assert decision.detail["read_back"] == "not_possible:no_entity"


def test_the_monitor_shows_the_baseline_once_and_counts_nothing() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(WRITE, facts(), at(0))

    logged = []
    for i in range(5):
        logged += observe(dry, GRACE_S + i * READING_GAP_S, facts())

    [entry] = logged
    assert entry.action == "would_judge" and entry.reason == "baseline_no_effect"
    assert entry.detail["dry_run"] is True and len(entry.detail["readings"]) == 3
    assert dry.monitor.counters("2026-11-13") == {"writes": 0, "no_effect": 0, "partial": 0}


def test_a_hold_control_changed_while_nothing_writes_it_is_logged_and_its_restore_skipped() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(WRITE, facts(), at(0))

    assert observe(dry, 10, facts(current="5.00")) == []
    [entry] = [e for e in observe(dry, 10 + MIN_DIVERGENCE_S, facts(current="5.00")) if e.action == "changed_outside"]

    assert entry.reason == "hold_control_changed_outside_spotnav" and entry.read == "5.00"
    assert entry.detail["owned"] == "23.04" and entry.detail["notify"] is False
    decision, judged = dry.decided(RESTORE, facts(current="5.00"), at(400))
    assert decision.detail["restore"] == "skipped:changed_outside_spotnav"
    assert dry.ledger.claim(LIMIT) is None
    assert [e.reason for e in judged] == ["not_judgeable:too_few_readings"]
    assert dry.ledger.counters("2026-11-13")["shadow_external"] == 1


def test_an_unavailable_control_during_a_hold_is_no_change() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(WRITE, facts(), at(0))

    for seconds in range(10, 1000, 10):
        assert [e for e in observe(dry, seconds, facts(current="unavailable")) if e.action == "changed_outside"] == []

    decision, _ = dry.decided(RESTORE, facts(), at(1000))
    assert "restore" not in decision.detail


def test_a_restore_inside_the_grace_says_the_monitor_could_not_judge() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(WRITE, facts(), at(0))

    _decision, [entry] = dry.decided(RESTORE, facts(), at(120))

    assert entry.action == "would_judge" and entry.reason == "not_judgeable:ended_within_grace"


def test_no_claim_or_watch_outlives_the_hold() -> None:
    dry = HoldDryRun(Ledger(), Monitor())
    dry.decided(WRITE, facts(), at(0))

    entries = observe(dry, 10, facts(), held=False)

    assert dry.ledger.claim(LIMIT) is None and dry.monitor.watch is None
    assert [e.reason for e in entries] == ["not_judgeable:ended_within_grace"]


def test_the_fuse_headroom_as_kw() -> None:
    assert site_cap_kw(10.0, 400.0) == 10.0 * 3**0.5 * 0.4
    assert site_cap_kw(None, 400.0) is None and site_cap_kw(-3.0, 400.0) == 0.0
