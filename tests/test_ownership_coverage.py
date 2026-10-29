"""The ownership shadow's cumulative coverage (`execution/ownership_coverage.py`): per charger and per event kind of
the core, how many events it decided, compared, disagreed on, found drift at or failed on, and when it first and
last saw each, kept across restarts (written debounced, never at every event) until the entry is removed."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav import core as core_package
from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.session import ChargeSession
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.ownership_coverage import (
    COVERAGE_FIELDS,
    OwnershipCoverage,
    SAVE_DELAY_S,
    STORE_VERSION,
    store_key,
)
from custom_components.spotnav.execution.ownership_shadow import CommandOutcome, OwnershipShadow

from .pause_world import pause_world
from .relay import FakeScheduler
from .world import controller_of, setup_charger

T0 = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)


def _kinds_in_code() -> set[str]:
    """Every `Event` subclass anywhere in `core/`, by its kind."""
    kinds: set[str] = set()
    for info in pkgutil.iter_modules(core_package.__path__):
        module = importlib.import_module(f"{core_package.__name__}.{info.name}")
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, ev.Event) and cls is not ev.Event:
                kinds.add(cls.kind)
    return kinds


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


# ---------------------------------------------------------------------------------------------- the tally itself


def test_every_event_kind_of_the_core_is_listed_with_zeros() -> None:
    coverage = OwnershipCoverage(now=lambda: T0).as_dict()
    assert set(coverage) == {"since", "version", "kinds", "unattributed", "previous"}
    assert coverage["since"] == T0.isoformat() and coverage["previous"] is None
    assert set(coverage["kinds"]) == _kinds_in_code() == set(ev.EVENT_TYPES)
    for kind, counts in coverage["kinds"].items():
        triggers = {"triggers": {}} if kind == "window_start" else {}
        assert counts == {**dict.fromkeys(COVERAGE_FIELDS, 0), "first_seen": None, "last_seen": None, **triggers}
    assert coverage["unattributed"] == dict.fromkeys(COVERAGE_FIELDS, 0)
    assert set(COVERAGE_FIELDS) == {
        "events", "compared", "disagreements", "drift", "errors",
        "mirror_drift", "written_back", "verdict_differs", "failures",
    }
    json.dumps(coverage)


def test_the_shadow_counts_each_event_under_its_kind() -> None:
    clock = Clock()
    shadow = OwnershipShadow(ChargeSession, now=clock)
    shadow.feed(ev.CarEnded())
    clock.now = T0 + timedelta(minutes=5)
    shadow.feed(ev.CarEnded())
    shadow.feed(ev.PlugIn(previous=False))
    kinds = shadow.diagnostics()["coverage"]["kinds"]
    assert kinds["car_ended"] == {
        **dict.fromkeys(COVERAGE_FIELDS, 0), "events": 2, "compared": 2,
        "first_seen": T0.isoformat(), "last_seen": (T0 + timedelta(minutes=5)).isoformat(),
    }
    assert kinds["plug_in"]["events"] == 1 and kinds["plug_in"]["first_seen"] == kinds["plug_in"]["last_seen"]
    assert kinds["unplug"]["events"] == 0 and kinds["unplug"]["first_seen"] is None


def test_a_feed_inside_another_is_decided_but_compared_by_the_outer_one() -> None:
    shadow = OwnershipShadow(ChargeSession, now=lambda: T0)
    outer = shadow.begin()
    shadow.feed(ev.ChargerReportedOff())
    shadow.end(outer, ev.CarEnded())
    kinds = shadow.diagnostics()["coverage"]["kinds"]
    assert kinds["charger_reported_off"]["events"] == 1 and kinds["charger_reported_off"]["compared"] == 0
    assert kinds["car_ended"]["events"] == 1 and kinds["car_ended"]["compared"] == 1


@pytest.mark.shadow_disagreement_expected
def test_disagreements_drift_and_errors_are_counted_under_the_event_that_met_them() -> None:
    state = {"today": ChargeSession(owner="plan")}
    shadow = OwnershipShadow(lambda: state["today"], now=lambda: T0)
    shadow.session = state["today"]
    # Today's code says it stopped, but its owner stays the plan's: the core says nobody's.
    shadow.end(shadow.begin(), ev.WindowEnd(), legacy=("stop",), outcome=CommandOutcome(True))
    # Today's owner moved with no event fed: the next feed finds it.
    state["today"] = ChargeSession(owner="solar")
    shadow.feed(ev.SolarStart())
    coverage = shadow.diagnostics()["coverage"]
    assert coverage["kinds"]["window_end"]["disagreements"] == 1
    assert coverage["kinds"]["solar_start"]["drift"] == 1
    assert coverage["kinds"]["window_end"]["drift"] == 0

    def broken() -> ChargeSession:
        raise RuntimeError("today's state could not be read")

    failing = OwnershipShadow(broken, now=lambda: T0)
    failing.feed(ev.Unplug())
    failing.check(("owner",), "unplug_settled")
    coverage = failing.diagnostics()["coverage"]
    assert coverage["kinds"]["unplug"]["errors"] >= 1
    assert coverage["kinds"]["unplug"]["events"] == 1
    # A comparison with no event of its own (the boundary's later check) belongs to no kind.
    assert coverage["unattributed"]["errors"] == 1


def test_a_comparison_with_no_event_of_its_own_is_unattributed() -> None:
    shadow = OwnershipShadow(ChargeSession, now=lambda: T0)
    shadow.check(("owner",), "unplug_settled")
    coverage = shadow.diagnostics()["coverage"]
    assert coverage["unattributed"]["compared"] == 1
    assert all(counts["compared"] == 0 for counts in coverage["kinds"].values())


def test_a_failing_tally_never_breaks_the_shadow() -> None:
    class Broken(OwnershipCoverage):
        def add(self, kind: str | None, field: str) -> None:
            raise RuntimeError("no tally")

    shadow = OwnershipShadow(ChargeSession, now=lambda: T0, coverage=Broken(now=lambda: T0))
    shadow.feed(ev.CarEnded())
    assert shadow.counts["events"] == 1 and shadow.counts["errors"] == 0


# ---------------------------------------------------------------------------------------------- kept on disk


async def test_writes_are_debounced_and_wait_for_the_stored_tally(
    hass: HomeAssistant, hass_storage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key("entry_x"))
    scheduled: list[float] = []
    real = store.async_delay_save

    def spy(data_func: Any, delay: float = 0) -> None:
        scheduled.append(delay)
        real(data_func, delay)

    monkeypatch.setattr(store, "async_delay_save", spy)
    coverage = OwnershipCoverage(store=store)
    coverage.add("plug_in", "events")
    assert scheduled == [], "nothing is written before the stored tally is read"
    await coverage.async_load("1.12.1")
    assert scheduled == [SAVE_DELAY_S]
    for _ in range(100):
        coverage.add("plug_in", "events")
    assert scheduled == [SAVE_DELAY_S], "one write waits, however many events come"
    assert store_key("entry_x") not in hass_storage
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY_S + 1))
    await hass.async_block_till_done()
    written = hass_storage[store_key("entry_x")]["data"]
    assert written["kinds"]["plug_in"]["events"] == 101 and written["version"] == "1.12.1"
    coverage.add("unplug", "events")
    assert scheduled == [SAVE_DELAY_S, SAVE_DELAY_S]


async def test_the_stored_tally_is_added_to_and_a_damaged_one_is_read_as_far_as_it_goes(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    hass_storage[store_key("entry_y")] = {
        "version": STORE_VERSION,
        "key": store_key("entry_y"),
        "data": {
            "since": "2026-09-01T00:00:00+00:00",
            "version": "1.12.0",
            "kinds": {
                "plug_in": {"events": 7, "compared": "x", "first_seen": "2026-09-02T00:00:00+00:00", "last_seen": 3},
                "unplug": "nonsense",
                "retired_kind": {"events": 2},
            },
            "unattributed": {"errors": 4, "drift": -1},
        },
    }
    coverage = OwnershipCoverage(store=Store(hass, STORE_VERSION, store_key("entry_y")), now=lambda: T0)
    coverage.add("plug_in", "events")
    await coverage.async_load("1.12.0")
    tally = coverage.as_dict()
    assert tally["since"] == "2026-09-01T00:00:00+00:00" and tally["version"] == "1.12.0"
    assert tally["kinds"]["plug_in"] == {
        **dict.fromkeys(COVERAGE_FIELDS, 0), "events": 8,
        "first_seen": "2026-09-02T00:00:00+00:00", "last_seen": T0.isoformat(),
    }
    assert tally["kinds"]["unplug"]["events"] == 0
    assert "retired_kind" not in tally["kinds"]
    assert tally["unattributed"]["errors"] == 4 and tally["unattributed"]["drift"] == 0


async def test_another_release_begins_a_new_tally_and_keeps_the_one_before(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The gate before the owner moves to the core reads one release: its tally begins when the release does, and the
    one before is kept under `previous`, one period back (the one before that is dropped)."""
    old = {
        "since": "2026-09-01T00:00:00+00:00",
        "version": "1.12.0",
        "kinds": {"plug_in": {"events": 7, "disagreements": 1}},
        "unattributed": {"errors": 2},
        "previous": {"since": "2026-08-01T00:00:00+00:00", "version": "1.11.0"},
    }
    hass_storage[store_key("entry_v")] = {"version": STORE_VERSION, "key": store_key("entry_v"), "data": old}
    coverage = OwnershipCoverage(store=Store(hass, STORE_VERSION, store_key("entry_v")), now=lambda: T0)
    coverage.add("unplug", "events")
    await coverage.async_load("1.13.0")
    tally = coverage.as_dict()
    assert (tally["since"], tally["version"]) == (T0.isoformat(), "1.13.0")
    assert tally["kinds"]["plug_in"]["events"] == 0 and tally["kinds"]["plug_in"]["disagreements"] == 0
    assert tally["kinds"]["unplug"]["events"] == 1, "what was counted meanwhile is this release's"
    assert tally["previous"] == {key: value for key, value in old.items() if key != "previous"}
    await coverage.async_flush()

    # The same release again: added to, its previous period kept.
    again = OwnershipCoverage(store=Store(hass, STORE_VERSION, store_key("entry_v")), now=lambda: T0)
    await again.async_load("1.13.0")
    assert again.as_dict()["kinds"]["unplug"]["events"] == 1
    assert again.as_dict()["previous"]["version"] == "1.12.0"
    # A version that cannot be read changes nothing.
    unknown = OwnershipCoverage(store=Store(hass, STORE_VERSION, store_key("entry_v")), now=lambda: T0)
    await unknown.async_load(None)
    assert unknown.as_dict()["version"] == "1.13.0" and unknown.as_dict()["kinds"]["unplug"]["events"] == 1


@pytest.mark.shadow_disagreement_expected
def test_mirror_drift_write_backs_differing_verdicts_and_failures_are_counted_under_their_event() -> None:
    state = {"today": ChargeSession(plugged=True, held=True)}
    written = {"changed": False}
    shadow = OwnershipShadow(
        lambda: state["today"],
        now=lambda: T0,
        owned_fields=("held",),
        drives=True,
        writer=lambda _session: written["changed"],
    )
    shadow.session = state["today"]
    # A field the core owns that today's state reads otherwise: under the kind of the event that met it.
    state["today"] = state["today"].with_changes(held=False)
    shadow.feed(ev.ConnectionUnknown())
    state["today"] = state["today"].with_changes(held=True)
    # Today's owner fields overridden by the core's owner.
    written["changed"] = True
    shadow.feed(ev.PlanDropped())
    written["changed"] = False
    # Today's rule would have chosen otherwise, inside a feed and outside any.
    token = shadow.begin()
    shadow.choose("window_start", True, False)
    shadow.end(token, ev.WindowStart(trigger=ev.TRIGGER_PLUG_IN, window_open=False))
    shadow.choose("nowhere", True, False)
    # A decision that could not be taken.
    shadow.failed("verdict", "rearm", RuntimeError("no"))
    shadow.failed("facts", None, RuntimeError("no"))
    coverage = shadow.diagnostics()["coverage"]
    kinds = coverage["kinds"]
    assert kinds["connection_unknown"]["mirror_drift"] == 1
    assert kinds["plan_dropped"]["written_back"] == 1
    assert kinds["window_start"]["verdict_differs"] == 1
    assert kinds["rearm"]["failures"] == 1
    assert coverage["unattributed"]["verdict_differs"] == 1 and coverage["unattributed"]["failures"] == 1
    assert sum(counts["mirror_drift"] for counts in kinds.values()) == shadow.counts["mirror_drift"] == 1


@pytest.mark.shadow_disagreement_expected  # the core asks for starts no code here sends
def test_a_window_start_is_counted_by_what_started_it() -> None:
    shadow = OwnershipShadow(ChargeSession, now=lambda: T0)
    for trigger in (ev.TRIGGER_TIMER, ev.TRIGGER_PLUG_IN, ev.TRIGGER_PLUG_IN, ev.TRIGGER_RETRY):
        shadow.feed(ev.WindowStart(trigger=trigger, window_open=False))
    window_start = shadow.diagnostics()["coverage"]["kinds"]["window_start"]
    assert window_start["events"] == 4
    assert window_start["triggers"] == {"timer": 1, "plug_in": 2, "retry": 1}
    assert "triggers" not in shadow.diagnostics()["coverage"]["kinds"]["plug_in"]


async def test_the_triggers_and_the_new_counts_are_kept_on_disk(hass: HomeAssistant) -> None:
    store = Store(hass, STORE_VERSION, store_key("entry_t"))
    coverage = OwnershipCoverage(store=store, now=lambda: T0)
    await coverage.async_load("1.13.0")
    coverage.add_trigger("window_start", "timer")
    coverage.add("rearm", "failures")
    await coverage.async_flush()
    again = OwnershipCoverage(store=Store(hass, STORE_VERSION, store_key("entry_t")), now=lambda: T0)
    again.add_trigger("window_start", "timer")
    await again.async_load("1.13.0")
    kinds = again.as_dict()["kinds"]
    assert kinds["window_start"]["triggers"] == {"timer": 2} and kinds["rearm"]["failures"] == 1


async def test_a_new_tally_names_the_version_that_started_it(hass: HomeAssistant) -> None:
    coverage = OwnershipCoverage(store=Store(hass, STORE_VERSION, store_key("entry_z")), now=lambda: T0)
    await coverage.async_load("1.12.1")
    assert coverage.as_dict()["version"] == "1.12.1" and coverage.as_dict()["since"] == T0.isoformat()


async def test_the_tally_survives_a_restart(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.plug.set(False)
    before = world.controller.ownership_shadow.diagnostics()["coverage"]
    assert before["kinds"]["restart"]["events"] == 1 and before["kinds"]["unplug"]["events"] >= 1
    assert before["version"]
    restarted = await world.restart()
    after = restarted.controller.ownership_shadow.diagnostics()["coverage"]
    assert after["since"] == before["since"] and after["version"] == before["version"]
    assert after["kinds"]["restart"]["events"] == 2
    assert after["kinds"]["restart"]["first_seen"] == before["kinds"]["restart"]["first_seen"]
    for kind, counts in before["kinds"].items():
        assert after["kinds"][kind]["events"] >= counts["events"], kind
    await restarted.shutdown()


async def test_the_tally_survives_home_assistants_own_stop(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    restarted = await world.ha_restart()
    assert restarted.controller.ownership_shadow.diagnostics()["coverage"]["kinds"]["restart"]["events"] == 2
    await restarted.shutdown()


async def test_removing_the_entry_removes_the_tally(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    entry = await setup_charger(hass, entry_id="cover_a")
    assert controller_of(hass, entry.entry_id).ownership_shadow.coverage.as_dict()["kinds"]["restart"]["events"] == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[store_key(entry.entry_id)]["data"]["kinds"]["restart"]["events"] == 1
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert store_key(entry.entry_id) not in hass_storage


# ---------------------------------------------------------------------------------------------- read out


async def test_diagnostics_carry_the_tally_beside_the_shadow(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass, entry_id="cover_b")
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    shadow = diagnostics["controller"]["ownership_shadow"]
    assert {"counts", "session", "disagreements", "drift", "events"} <= set(shadow)
    coverage = shadow["coverage"]
    assert set(coverage) == {"since", "version", "kinds", "unattributed", "previous"}
    assert set(coverage["kinds"]) == set(ev.EVENT_TYPES)
    assert coverage["kinds"]["restart"]["events"] == 1
    assert coverage["version"]
    json.dumps(diagnostics)
