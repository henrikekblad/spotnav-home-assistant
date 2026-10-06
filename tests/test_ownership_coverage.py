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
from custom_components.spotnav.execution.ownership_coverage import (
    COVERAGE_FIELDS,
    OwnershipCoverage,
    SAVE_DELAY_S,
    STORE_VERSION,
    store_key,
)
from custom_components.spotnav.execution.ownership_shadow import CommandOutcome, OwnershipShadow

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
    assert set(coverage) == {"since", "version", "kinds", "unattributed"}
    assert coverage["since"] == T0.isoformat()
    assert set(coverage["kinds"]) == _kinds_in_code() == set(ev.EVENT_TYPES)
    for counts in coverage["kinds"].values():
        assert counts == {**dict.fromkeys(COVERAGE_FIELDS, 0), "first_seen": None, "last_seen": None}
    assert coverage["unattributed"] == dict.fromkeys(COVERAGE_FIELDS, 0)
    assert set(COVERAGE_FIELDS) == {"events", "compared", "disagreements", "drift", "errors"}
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
        "events": 2, "compared": 2, "disagreements": 0, "drift": 0, "errors": 0,
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
    await coverage.async_load("1.12.1")
    tally = coverage.as_dict()
    assert tally["since"] == "2026-09-01T00:00:00+00:00" and tally["version"] == "1.12.0"
    assert tally["kinds"]["plug_in"] == {
        **dict.fromkeys(COVERAGE_FIELDS, 0), "events": 8,
        "first_seen": "2026-09-02T00:00:00+00:00", "last_seen": T0.isoformat(),
    }
    assert tally["kinds"]["unplug"]["events"] == 0
    assert "retired_kind" not in tally["kinds"]
    assert tally["unattributed"]["errors"] == 4 and tally["unattributed"]["drift"] == 0


async def test_a_new_tally_names_the_version_that_started_it(hass: HomeAssistant) -> None:
    coverage = OwnershipCoverage(store=Store(hass, STORE_VERSION, store_key("entry_z")), now=lambda: T0)
    await coverage.async_load("1.12.1")
    assert coverage.as_dict()["version"] == "1.12.1" and coverage.as_dict()["since"] == T0.isoformat()
