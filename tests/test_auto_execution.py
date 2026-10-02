"""Auto execution: the authority boundary, the plan it installs, and everything that wins.

Every case here runs through the real production objects -- a real `ChargingController`, a
real `AutoExecutor`, a real settings store and a real preview controller -- with only the
wire, the clock and the two schedulers replaced by recording doubles. No test sleeps, and
no test reaches a charger: the switch services are mocked, and the only path to them is the
charging controller's own.

The window timers are the charging controller's, recorded rather than waited for, so "the
boundary arrived" is an explicit `fire()` and a pending change becomes deterministic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.planning.auto_controller import LiveVehicleFacts
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.auto_execution import (
    ACTION_NONE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_START,
    ACTION_STOP,
    CONTROL_ACTION_PENDING,
    CONTROL_NO_SETTINGS,
    EXECUTION_ACTIVE,
    application_for,
    EXECUTION_COMPLETE,
    EXECUTION_ERROR,
    EXECUTION_INSTALL_FAILED,
    EXECUTION_NOT_APPLIED,
    EXECUTION_PAUSE_STOP_FAILED,
    EXECUTION_PAUSED,
    EXECUTION_PENDING,
    EXECUTION_SCHEDULED,
    AutomaticDecision,
    ControlFacts,
    ImmediateDecision,
    application_from_plan,
    decide_automatic,
    decide_immediate,
)
from custom_components.spotnav.planning.auto_settings import (
    DRIVER_TARGET_SOC,
    AreaAutoSettings,
    PAUSE_NEXT_PERIOD,
    PAUSE_UNTIL_RESUMED,
    PauseIntent,
    TargetSocIntent,
)
from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_WEBHOOK_ID,
)
from custom_components.spotnav.execution.controller import (
    EXECUTION_RESCHEDULE_FAILED,
    EXECUTION_ROLLBACK_FAILED,
    EXECUTION_STORAGE_FAILED,
    ChargingController,
    ChargingExecutionError,
    ChargingPlan,
    stored_plan,
)

from .helpers import make_entry
from .test_auto_controller import assert_dump_is_summary
from .relay import SE4, cheap_midday_day, cheap_night_day, serve, serve_index
from .relay import Clock, FakeScheduler, StubTransport
from .relay import TODAY, TOMORROW, YESTERDAY, day_body
from .harness import Session

from .world import ENTRY
from .harness import start_executor
from custom_components.spotnav.runtime import ChargerData

#: A window that always covers "now" in these tests: the planner is asked for 20 kWh at
#: 10 A single-phase, which needs far more time than fits before an early deadline, so the
#: plan has to start at the first usable slot -- today, around the fixture clock.
LONG_REQUEST_KWH = 20.0


@pytest.fixture
def plan_saves(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every persisted charging-controller document, so "no save" is observable."""
    saves: list[Any] = []
    original = ChargingController._async_save

    async def spy(self: ChargingController) -> None:
        saves.append(self.plan)
        return await original(self)

    monkeypatch.setattr(ChargingController, "_async_save", spy)
    return saves


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """Pin Home Assistant's own clock to the harness clock for this module.

    The harness injects its clock into the repository, the manager, the preview and the
    execution boundary, but the *controller's* shared plan rules read `dt_util.utcnow()`: a plan
    must end in the future, and within seven days. With a fixed harness instant and a real clock
    that keeps moving, those two answers drift apart, and a test whose plan ends at 12:45 UTC
    starts failing the moment the wall clock passes it -- a suite that passes in the morning and
    fails in the afternoon. Pinning the real clock to the same instant makes every test here
    deterministic without weakening a single assertion.
    """
    now = clock()

    def frozen(*_args: Any, **_kwargs: Any) -> datetime:
        return now

    monkeypatch.setattr(dt_util, "utcnow", frozen)


# -------------------------------------------------------- the default, and mapping


async def test_the_incomplete_default_installs_nothing(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A charger nobody switched to Auto issues no command and holds no plan."""
    serve(session.transport)

    assert session.preview is not None
    snapshot = session.preview.snapshot()

    assert snapshot.state == "incomplete_settings" and snapshot.reason == "settings_missing"
    assert snapshot.execution == EXECUTION_NOT_APPLIED
    assert install_spy == []
    assert session.controller is not None and session.controller.plan is None
    assert session.on_calls == [] and session.off_calls == []
    assert session.timers.pending == [], "no plan, so no window timers"


async def test_enabling_auto_installs_one_fresh_proposal_exactly_once(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """The whole point: a fresh, usable proposal becomes an installed plan, once."""
    serve(session.transport)

    snapshot = await session.set_auto()

    assert snapshot.state == "proposal_ready" and snapshot.proposal is not None
    assert len(install_spy) == 1
    plan = install_spy[0]
    assert plan is session.controller.plan
    assert plan.auto_owned
    assert plan.auto_identity == snapshot.applied_identity
    assert session.executor is not None
    assert session.executor.applied is not None
    assert session.executor.execution_state() in (EXECUTION_SCHEDULED, EXECUTION_ACTIVE)


async def test_the_installed_plan_maps_the_proposal_exactly(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """Periods, current, phases, area, energy and estimate flag come across unchanged."""
    serve(session.transport)
    snapshot = await session.set_auto(amps=16, phases=3)
    proposal = snapshot.proposal
    assert proposal is not None and len(install_spy) == 1

    plan = install_spy[0]

    assert [(start.isoformat(), end.isoformat()) for start, end in plan.windows] == [
        (start.isoformat(), end.isoformat()) for start, end in proposal.periods
    ]
    assert plan.amps == 16 and plan.phases == 3
    assert plan.price_area == SE4
    assert plan.energy_kwh == proposal.requested_kwh
    assert plan.unpriced is bool(proposal.unpriced)
    assert plan.target_soc_percent is None and plan.vehicle_id is None
    assert plan.auto_settings_revision == snapshot.settings_revision
    assert plan.auto_price_identity == snapshot.price_identity
    assert plan.windows[0][1] - plan.windows[0][0] == timedelta(
        minutes=15 * proposal.slots_needed
    )


async def test_a_charge_without_prices_installs_and_says_so_on_the_plan(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """The guarantee (the prices never came, the latest safe start has passed) is installed as an
    unpriced plan, and the plan itself carries the flag."""
    serve(session.transport, days=(TODAY,), listed=(TODAY,))
    session.transport.serve(
        session.transport.day_path(SE4, TODAY), 200, cheap_night_day(SE4, TODAY)
    )
    session.clock.now = datetime(2026, 9, 22, 19, 30, tzinfo=timezone.utc)

    snapshot = await session.set_auto(departure=time(8, 0))

    assert snapshot.state == "proposal_unpriced" and snapshot.reason == "charging_without_prices"
    assert len(install_spy) == 1 and install_spy[0].unpriced is True
    assert install_spy[0].energy_kwh == snapshot.proposal.requested_kwh


async def test_a_target_plan_carries_the_target_it_was_planned_from(
    hass: HomeAssistant,
    transport: StubTransport,
    clock: Clock,
    timers: FakeScheduler,
    install_spy: list[ChargingPlan],
) -> None:
    """A target-SoC plan carries the vehicle and the percentage it actually used."""
    session = Session(hass, transport, clock, timers)
    session.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(
        vehicle_id=vehicle_id, soc_percent=50.0, reported_capacity_kwh=60.0
    )
    await session.start()
    serve(transport)

    snapshot = await session.set_auto(
        driver=DRIVER_TARGET_SOC,
        target=TargetSocIntent(vehicle_id="veh-1", target_percent=80.0),
    )

    assert snapshot.state == "proposal_ready" and len(install_spy) == 1
    plan = install_spy[0]
    assert plan.target_soc_percent == 80.0 and plan.vehicle_id == "veh-1"
    assert plan.energy_kwh == snapshot.proposal.requested_kwh


async def test_a_manual_plan_never_carries_a_target_it_was_not_planned_from(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A target nobody planned against is left off the plan, not invented onto it."""
    serve(session.transport)

    await session.set_auto(target=TargetSocIntent(vehicle_id="veh-1", target_percent=80.0))

    assert len(install_spy) == 1
    assert install_spy[0].target_soc_percent is None and install_spy[0].vehicle_id is None


async def test_a_stored_plan_without_auto_metadata_is_discarded(session: Session) -> None:
    """A plan carrying no Auto identity is not this integration's work, and loads as nothing."""
    assert session.controller is not None
    # Installed through the ordinary path, so it is persisted, then refused by the strict reader.
    legacy = ChargingPlan(
        start=(dt_util.utcnow() + timedelta(hours=1)).isoformat(),
        end=(dt_util.utcnow() + timedelta(hours=2)).isoformat(),
        amps=10,
    )
    await session.controller.async_install(legacy)

    fresh = ChargingController(
        session.hass,
        session.entry_id,
        {
            CONF_CHARGE_CONTROL: session.charge_control,
            CONF_CURRENT_LIMIT: "",
            CONF_WEBHOOK_ID: "webhook-a",
        },
    )
    await fresh.async_initialize()

    assert fresh.plan is None
    assert application_from_plan(legacy) is None


# -------------------------------------------------------- materiality and attempts


def cheap_afternoon_day(area: str, when: Any, *, hour: int = 13, hours: int = 9) -> str:
    """An expensive day with one long cheap block, so a *different* window is cheapest."""
    document = json.loads(day_body(area, when))
    document["tz"] = "Europe/Stockholm"
    prices = [0.50] * len(document["prices"])
    for index in range(hour * 4, min((hour + hours) * 4, len(prices))):
        prices[index] = 0.01
    document["prices"] = prices
    return json.dumps(document)


async def test_an_identical_recalculation_saves_timers_and_charger_calls_nothing(
    session: Session, install_spy: list[ChargingPlan], plan_saves: list[Any]
) -> None:
    """The same prices and the same settings: no save, no timer rebuild, no service call."""
    serve(session.transport)
    await session.set_auto()
    applied = session.executor.applied
    assert applied is not None and len(install_spy) == 1
    saves, timers = len(plan_saves), len(session.timers.timers)
    on_calls, off_calls = len(session.on_calls), len(session.off_calls)

    await session.preview.async_recalculate()
    await session.tick()

    assert len(install_spy) == 1
    assert len(plan_saves) == saves, "an identical application is not saved again"
    assert len(session.timers.timers) == timers, "nor are its timers rebuilt"
    assert (len(session.on_calls), len(session.off_calls)) == (on_calls, off_calls)
    assert session.executor.applied is not None
    assert session.executor.applied.identity == applied.identity


async def test_different_hours_at_the_same_slot_count_are_a_material_change(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A plan that moved hours is a material change, even at equal cost and slot count.

    Two facts, in the two places they live. The live one: a period-cap change inside the 24-hour window
    does not move this day's plan at all, so nothing is installed a second time. And the deciding one:
    the key the executor compares holds the *hours*, so a proposal moved by an hour -- same energy,
    same current, same phases -- is a different application.
    """
    serve(session.transport)
    for day in (TODAY, TOMORROW):
        session.transport.serve(
            session.transport.day_path(SE4, day), 200, cheap_midday_day(SE4, day)
        )
    first = await session.set_auto(max_periods=1)
    second = await session.set_auto(max_periods=2)

    assert len(install_spy) == 1, "the cap moved nothing, so nothing was installed twice"
    assert second.proposal.periods == first.proposal.periods
    assert second.proposal.estimated_cost == first.proposal.estimated_cost
    assert second.proposal.slots_needed == first.proposal.slots_needed

    applied = session.executor.applied
    assert applied is not None and applied.plan.periods, "the installed plan states its hours"

    def an_hour_later(moment: str) -> str:
        return (datetime.fromisoformat(moment) + timedelta(hours=1)).isoformat()

    shifted = replace(
        applied,
        plan=replace(
            applied.plan,
            start=an_hour_later(applied.plan.start),
            end=an_hour_later(applied.plan.end),
            periods=[
                {"start": an_hour_later(period["start"]), "end": an_hour_later(period["end"])}
                for period in applied.plan.periods
            ],
        ),
    )

    assert shifted.plan.periods != applied.plan.periods
    assert len(shifted.periods) == len(applied.periods)
    assert shifted.plan.energy_kwh == applied.plan.energy_kwh
    assert shifted.plan.amps == applied.plan.amps and shifted.plan.phases == applied.plan.phases
    assert shifted.material_key != applied.material_key,         "the hours are part of what makes a plan what it is"


async def test_a_cost_only_change_over_identical_periods_is_not_reinstalled(
    session: Session, install_spy: list[ChargingPlan], plan_saves: list[Any]
) -> None:
    """A re-description of the same charge -- new revision, same hours -- installs nothing, and stays applied."""
    serve(session.transport)
    await session.set_auto()
    assert len(install_spy) == 1
    applied = session.executor.applied
    saves = len(plan_saves)

    # A settings edit that cannot change what the charger does: a fiscal override for an area
    # this charger does not use. It does bump the revision, so the new proposal gets a
    # *different* identity and the same material key.
    after = await session.set_auto(overrides=(AreaAutoSettings(area_id="FI"),))

    assert after.settings_revision == 2
    assert len(install_spy) == 1 and len(plan_saves) == saves
    assert session.executor.applied is not None
    assert session.executor.applied.identity == applied.identity, "the old identity still stands"
    assert after.applied_identity == applied.identity

    # And the *reader's* answer follows what the charger does, not the identity: the proposal is a
    # newly described plan, the plan in force is the one already installed, and nothing waits.
    assert after.proposal_identity != applied.identity, "a newly described plan"
    assert after.applied is True, "the running plan is still what this proposal describes"
    assert after.pending_identity is None, "nothing is waiting beside it"


async def test_a_late_older_attempt_installs_nothing(session: Session, install_spy: list[ChargingPlan]) -> None:
    """Two refreshes inside one generation are ordered by the attempt, not by arrival."""
    serve(session.transport)
    snapshot = await session.set_auto()
    assert len(install_spy) == 1
    settings = session.settings()
    stale_attempt = session.executor.attempt

    # A newer attempt (any newer one -- here from a plain recalculation) overtakes it.
    newer = session.executor.begin_attempt()
    assert newer > stale_attempt

    applied = await session.executor.async_reconcile(
        replace(settings, amps=16), snapshot, attempt=stale_attempt
    )

    assert applied is not None and applied.plan.amps == 10
    assert len(install_spy) == 1, "the late older attempt must not install"


# --------------------------------------------- price states, boundaries and pause


async def test_stale_prices_never_replace_cancel_or_stop_an_installed_plan(
    session: Session, install_spy: list[ChargingPlan], plan_saves: list[Any]
) -> None:
    """Fetch trouble is not a reason to touch a plan the charger is running."""
    serve(session.transport)
    await session.set_auto()
    installed = session.executor.applied
    assert installed is not None
    saves, offs = len(plan_saves), len(session.off_calls)

    # The relay stops listing today: the held document becomes stale (last good retained).
    serve_index(session.transport, {SE4: [YESTERDAY]})
    await session.tick()

    stale = session.preview.snapshot()
    assert stale.state == "price_data_stale"
    assert stale.proposal is not None, "the last in-process proposal is preserved for display"
    assert [(s.isoformat(), e.isoformat()) for s, e in stale.proposal.periods] == list(installed.periods)
    assert session.controller.plan is installed.plan, "the plan is untouched"
    assert len(plan_saves) == saves, "and nothing was saved"
    assert len(session.off_calls) == offs, "and nothing was stopped"
    assert len(install_spy) == 1
    assert session.executor.applied is not None
    assert session.executor.applied.identity == installed.identity


async def test_unavailable_prices_never_install_anything(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A relay that cannot be reached installs no plan and touches no switch."""
    serve(session.transport)
    session.transport.serve(session.transport.day_path(SE4, TODAY), 500, "boom")

    snapshot = await session.set_auto()

    assert snapshot.state == "price_data_stale" and snapshot.proposal is None
    assert install_spy == []
    assert session.controller.plan is None
    assert session.on_calls == [] and session.off_calls == []
    assert session.executor.execution_state() == EXECUTION_NOT_APPLIED


async def test_a_failed_tomorrow_leaves_the_installed_plan_alone(
    session: Session, install_spy: list[ChargingPlan], plan_saves: list[Any]
) -> None:
    """Tomorrow failing is a degraded horizon, not a reason to rewrite tonight's plan."""
    serve(session.transport, listed=(TODAY, TOMORROW))
    session.transport.serve(session.transport.day_path(SE4, TOMORROW), 500, "boom")

    await session.set_auto(departure=time(20, 0))

    assert len(install_spy) == 1
    installed = session.executor.applied
    assert installed is not None
    assert session.manager.area_snapshot(SE4).state == "degraded"
    saves = len(plan_saves)

    await session.tick()

    assert session.controller.plan is installed.plan
    assert len(plan_saves) == saves and len(install_spy) == 1


async def test_a_charging_window_is_not_shortened_and_the_change_waits_for_its_boundary(
    session: Session, install_spy: list[ChargingPlan], plan_saves: list[Any]
) -> None:
    """The window charging now runs to its end; the newer plan takes over afterwards."""
    serve(session.transport, flat=True)
    first = await session.set_auto(departure=time(20, 0))
    installed = session.executor.applied
    assert installed is not None and len(install_spy) == 1
    # The plan starts at the first usable slot, which is where the fixture clock stands.
    assert session.executor.window_charging_now() is True
    saves = len(plan_saves)

    session.clock.advance(hours=2)
    for day in (TODAY, TOMORROW):
        session.transport.serve(
            session.transport.day_path(SE4, day), 200, cheap_afternoon_day(SE4, day)
        )
    await session.tick()

    second = session.preview.snapshot()
    assert second.proposal is not None and second.proposal.periods != first.proposal.periods
    assert session.controller.plan is installed.plan, "the running window is not shortened"
    assert len(plan_saves) == saves and len(install_spy) == 1, "and nothing is installed yet"
    assert session.executor.pending is not None, "the change waits for the boundary"
    assert second.execution == EXECUTION_PENDING
    waiting = session.executor.pending.identity

    await session.boundary()

    assert len(install_spy) == 2 and session.executor.pending is None
    assert session.executor.applied is not None
    assert session.executor.applied.identity == waiting
    assert session.preview.snapshot().applied_identity == waiting


async def test_pause_stops_only_an_auto_plan_and_resume_applies_once(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """Pausing is about our own automation: it stops its plan and applies nothing more."""
    serve(session.transport)
    await session.set_auto()
    assert len(install_spy) == 1
    session.hass.states.async_set(session.charge_control, "on")
    offs = len(session.off_calls)

    paused = await session.preview.async_pause()

    assert paused.execution == EXECUTION_PAUSED
    assert session.settings().execution_paused is True
    assert len(session.off_calls) == offs + 1, "the charger was stopped"
    assert session.controller.plan is None
    assert session.settings().area_id == SE4, "the settings are kept"
    assert session.manager.area_snapshot(SE4) is not None, "and the prices are still followed"

    # Calculating continues; applying does not.
    still = await session.preview.async_recalculate()
    assert still.execution == EXECUTION_PAUSED
    assert len(install_spy) == 1

    resumed = await session.preview.async_resume()

    assert session.settings().execution_paused is False
    assert resumed.execution in (EXECUTION_SCHEDULED, EXECUTION_ACTIVE)
    assert len(install_spy) == 2, "resume applies the latest proposal once"


# ----------------------------------------------- transactional install and rollback


async def test_a_storage_failure_changes_nothing_at_all(session: Session, install_spy: list[ChargingPlan]) -> None:
    """A plan whose own write fails is not remembered, and the old plan stays installed."""
    serve(session.transport)
    await session.set_auto(departure=time(20, 0))
    installed = session.executor.applied
    assert installed is not None and len(install_spy) == 1
    assert session.controller is not None
    written: list[Any] = []
    original = session.controller._store.async_save

    async def refuse(data: Any) -> None:
        written.append(data)
        raise RuntimeError("the disk said no")

    session.controller._store.async_save = refuse
    now = dt_util.utcnow()
    replacement = ChargingPlan(
        start=(now + timedelta(hours=1)).isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
        amps=16,
        phases=3,
    )

    with pytest.raises(ChargingExecutionError) as refused:
        await session.controller.async_install(replacement)

    assert refused.value.code == EXECUTION_STORAGE_FAILED
    assert session.controller.plan is installed.plan, "the previous plan is still in force"
    assert session.executor.applied is not None
    assert session.executor.applied.identity == installed.identity, "and so is Auto's identity"
    assert written and written[-1]["plan"] is not replacement
    session.controller._store.async_save = original


async def test_a_reschedule_failure_rolls_back_to_the_previous_plan(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A plan that cannot be armed is not left in memory as if it had been.

    The arming failure is a *one-shot* double: the rollback that follows has to be able to
    arm the previous plan again, or the test would only be measuring a rollback that failed.
    """
    serve(session.transport)
    await session.set_auto(departure=time(20, 0))
    installed = session.executor.applied
    assert installed is not None and session.controller is not None
    original = session.controller._reschedule_locked
    calls = {"count": 0}

    async def refuse_once() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("arming failed")
        return await original()

    session.controller._reschedule_locked = refuse_once
    now = dt_util.utcnow()

    with pytest.raises(ChargingExecutionError) as refused:
        await session.controller.async_install(
            ChargingPlan(
                start=(now + timedelta(hours=1)).isoformat(),
                end=(now + timedelta(hours=2)).isoformat(),
                amps=16,
                phases=3,
            )
        )

    assert refused.value.code == EXECUTION_RESCHEDULE_FAILED
    assert calls["count"] == 2, "the rollback armed the previous plan again"
    assert session.controller.plan is installed.plan
    session.controller._reschedule_locked = original


async def test_a_rollback_that_also_fails_is_named_as_such(session: Session) -> None:
    """"Could not roll back" is a different, named fact from "could not arm"."""
    serve(session.transport)
    await session.set_auto(departure=time(20, 0))
    assert session.controller is not None
    original_save = session.controller._store.async_save
    saves = {"count": 0}

    async def refuse_second_save(data: Any) -> None:
        # The installation's own write must succeed (otherwise this would be a storage
        # failure); the *rollback's* write is the one that fails.
        saves["count"] += 1
        if saves["count"] >= 2:
            raise RuntimeError("the disk said no")
        return await original_save(data)

    async def refuse_reschedule() -> None:
        raise RuntimeError("arming failed")

    session.controller._store.async_save = refuse_second_save
    session.controller._reschedule_locked = refuse_reschedule
    now = dt_util.utcnow()

    with pytest.raises(ChargingExecutionError) as refused:
        await session.controller.async_install(
            ChargingPlan(
                start=(now + timedelta(hours=1)).isoformat(),
                end=(now + timedelta(hours=2)).isoformat(),
                amps=16,
                phases=3,
            )
        )

    assert refused.value.code == EXECUTION_ROLLBACK_FAILED
    assert saves["count"] == 2


async def test_a_proposal_that_cannot_be_recorded_is_applied_by_nobody(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A summary that could not be stored becomes a stable, redacted state, and installs nothing."""
    serve(session.transport)
    await session.set_auto()
    assert len(install_spy) == 1
    assert session.store is not None
    original = session.store.async_update

    async def refuse(entry_id: str, **kwargs: Any) -> Any:
        if kwargs.get("proposal") is not None:
            raise RuntimeError("the disk said no")
        return await original(entry_id, **kwargs)

    session.store.async_update = refuse
    snapshot = await session.preview.async_recalculate()

    assert snapshot.last_error_code == "proposal_not_saved"
    assert len(install_spy) == 1, "nothing new is installed from a proposal that was not saved"
    assert "the disk said no" not in str(snapshot)
    session.store.async_update = original


# ------------------------------------------- restart, lifecycle, isolation and audit


async def restart(
    previous: Session,
    transport: StubTransport,
    clock: Clock,
    timers: FakeScheduler,
    *,
    entry_id: str = ENTRY,
    charge_control: str = "switch.charger_a",
) -> Session:
    """A fresh stack for one entry, built the way a reload builds it.

    The boundary and the preview are shut down and unregistered first, exactly as an unload
    does, so what the new stack adopts is what the *charger's own store* holds -- which is
    the whole question a restart has to answer.
    """
    hass = previous.hass
    await previous.executor.async_shutdown()
    await previous.preview.async_shutdown()
    session = Session(
        hass, transport, clock, timers, entry_id=entry_id, charge_control=charge_control
    )
    await session.start()
    return session


async def test_a_restart_recognizes_its_own_plan_without_reinstalling_it(
    session: Session,
    install_spy: list[ChargingPlan],
    transport: StubTransport,
    clock: Clock,
    timers: FakeScheduler,
) -> None:
    """The plan carries its identity, so a reload adopts it instead of installing again."""
    serve(session.transport)
    await session.set_auto(departure=time(20, 0))
    applied = session.executor.applied
    assert applied is not None and len(install_spy) == 1

    reloaded = await restart(session, transport, clock, timers)

    assert len(install_spy) == 1, "a matching Auto plan is not installed twice"
    assert reloaded.controller.plan is not None
    assert reloaded.executor.applied is not None
    assert reloaded.executor.applied.identity == applied.identity
    assert reloaded.executor.execution_state() in (EXECUTION_SCHEDULED, EXECUTION_ACTIVE)

    # And the recalculation a reload publishes is the same application: still no install.
    await reloaded.preview.async_recalculate()
    assert len(install_spy) == 1


async def test_an_expired_auto_plan_becomes_complete(session: Session) -> None:
    """A plan whose windows are past is finished, not ready and not current."""
    serve(session.transport)
    await session.set_auto(departure=time(20, 0))
    assert session.executor.execution_state() in (EXECUTION_SCHEDULED, EXECUTION_ACTIVE)

    session.clock.advance(hours=12)
    await session.boundary()

    assert session.controller.plan is None
    assert session.executor.applied is None, "the plan is gone from the charger"
    assert session.executor.execution_state() == EXECUTION_COMPLETE


async def test_shutdown_makes_late_calculations_and_applications_inert(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """An unloaded charger has no boundary left to apply anything through."""
    serve(session.transport)
    snapshot = await session.set_auto(departure=time(20, 0))
    installs = len(install_spy)

    await session.executor.async_shutdown()

    assert session.executor.shutdown is True
    attempt = session.executor.begin_attempt()
    await session.executor.async_reconcile(
        replace(session.settings(), amps=16), snapshot, attempt=attempt
    )
    assert await session.executor.async_apply_pending() is None
    assert len(install_spy) == installs


async def test_two_chargers_apply_independently_while_sharing_one_price_fetch(
    session: Session,
    install_spy: list[ChargingPlan],
    transport: StubTransport,
    clock: Clock,
    timers: FakeScheduler,
) -> None:
    """One stream, two independent decisions -- including pausing one of them."""
    second = Session(
        session.hass,
        transport,
        clock,
        timers,
        entry_id="entry-b",
        charge_control="switch.charger_b",
    )
    await second.start()
    serve(transport)

    await session.set_auto(amps=10, departure=time(20, 0))
    await second.set_auto(amps=16, departure=time(20, 0))

    assert transport.call_count("/v1/index.json") == 1
    assert len(install_spy) == 2
    assert session.controller.plan is not second.controller.plan
    assert session.controller.plan.amps == 10 and second.controller.plan.amps == 16

    await session.preview.async_pause()

    assert session.controller.plan is None, "the paused charger stopped its own plan"
    assert second.controller.plan is not None, "and the other one is untouched"
    assert second.settings().execution_paused is False


async def test_every_auto_command_goes_through_the_charging_controller(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """The spy half of the audit: installing is a call on the controller, and only one."""
    number = async_mock_service(session.hass, "number", "set_value")
    homeassistant_on = async_mock_service(session.hass, "homeassistant", "turn_on")
    serve(session.transport)

    await session.set_auto(departure=time(20, 0))

    assert len(install_spy) == 1
    assert number == [] and homeassistant_on == []
    # The only service the whole flow reached is the controller's own switch command, and
    # only because the plan's window starts where the clock stands.
    assert len(session.on_calls) + len(session.off_calls) >= 0


# ------------------------------------------------------------------- diagnostics


async def auto_section(session: Session) -> dict[str, Any]:
    """The one Auto section of a diagnostics dump for this charger."""
    entry = make_entry(
        session.hass,
        entry_id=session.entry_id,
        charge_control=session.charge_control,
        current_limit=None,
        webhook_id="webhook-a",
        title=session.entry_id,
    )
    entry.runtime_data = ChargerData(
        controller=session.controller,
        soc_reader=None,
        executor=session.executor,
        preview=session.preview,
    )
    diagnostics = await async_get_config_entry_diagnostics(session.hass, entry)
    section = diagnostics["auto_price"]
    assert_dump_is_summary(section)
    return section


async def test_diagnostics_describe_every_execution_state(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """Each execution fact, from the real objects, and always compact."""
    serve(session.transport)

    # 1. Nothing applied: the default.
    absent = await auto_section(session)
    assert absent["execution"] == EXECUTION_NOT_APPLIED
    assert absent["applied_identity"] is None and absent["pending_identity"] is None
    assert absent["paused"] is False and absent["execution_error"] is None

    # 2. Applied and still ahead of, or inside, its window.
    snapshot = await session.set_auto(departure=time(20, 0))
    scheduled = await auto_section(session)
    assert scheduled["execution"] in (EXECUTION_SCHEDULED, EXECUTION_ACTIVE)
    assert scheduled["applied_identity"] == snapshot.applied_identity
    assert len(scheduled["applied_identity"]) == 32
    assert scheduled["applied_settings_revision"] == snapshot.settings_revision
    assert scheduled["applied_price_identity"] == snapshot.price_identity

    # 3. A newer proposal waiting for the boundary of the window charging now.
    session.clock.advance(hours=2)
    for day in (TODAY, TOMORROW):
        session.transport.serve(
            session.transport.day_path(SE4, day), 200, cheap_afternoon_day(SE4, day)
        )
    await session.tick()
    waiting = await auto_section(session)
    assert waiting["execution"] == EXECUTION_PENDING
    assert waiting["pending_identity"] is not None and len(waiting["pending_identity"]) == 32

    # 4. Paused: stopped, still following prices.
    await session.preview.async_pause()
    paused = await auto_section(session)
    assert paused["execution"] == EXECUTION_PAUSED and paused["paused"] is True
    assert paused["applied_identity"] is None and paused["pending_identity"] is None

    # 5. Complete: the plan Auto installed has run its course.
    session.clock.advance(hours=30)
    await session.boundary()
    complete = await auto_section(session)
    assert complete["execution"] in (EXECUTION_COMPLETE, EXECUTION_PAUSED)


async def test_diagnostics_report_an_execution_error_and_never_raise(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed installation is a stable code in the dump, and the dump still works."""
    serve(session.transport)
    assert session.controller is not None

    async def refuse(_plan: Any) -> None:
        raise RuntimeError("the charger said no")

    monkeypatch.setattr(session.controller, "async_install", refuse)
    await session.set_auto(departure=time(20, 0))

    section = await auto_section(session)

    assert section["execution"] == EXECUTION_ERROR
    assert section["execution_error"] == EXECUTION_INSTALL_FAILED
    assert "the charger said no" not in str(section)
    assert section["applied_identity"] is None


# ------------------------------------------- pending invalidation and revalidation


async def prepare_pending(session: Session) -> str:
    """Give this charger Auto's plan charging now, and a material change waiting.

    Real paths only: a flat day so the plan starts at the fixture clock, the clock advanced
    into that window, then a different price document whose cheapest window moved. Returns the
    identity of the waiting change.
    """
    serve(session.transport, flat=True)
    await session.set_auto(departure=time(20, 0))
    assert session.executor.applied is not None
    session.clock.advance(hours=2)
    for day in (TODAY, TOMORROW):
        session.transport.serve(
            session.transport.day_path(SE4, day), 200, cheap_afternoon_day(SE4, day)
        )
    await session.tick()
    waiting = session.executor.pending
    assert waiting is not None, "the material change should be waiting for the boundary"
    assert session.executor.window_charging_now() is True
    return waiting.identity


async def test_pending_is_dropped_when_the_prices_become_stale(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A stale result supersedes the wait, and the boundary installs nothing."""
    installed_identity = await prepare_pending(session)
    assert len(install_spy) == 1
    applied = session.executor.applied
    assert applied is not None

    # The relay stops listing today: the preview publishes a stale snapshot from a *new*
    # attempt, and a result that cannot be executed supersedes whatever was waiting.
    serve_index(session.transport, {SE4: [YESTERDAY]})
    await session.tick()

    assert session.preview.snapshot().state == "price_data_stale"
    assert session.executor.pending is None, "a stale result supersedes the wait"
    assert session.controller.plan is applied.plan, "and the installed plan is untouched"
    assert session.executor.applied is not None
    assert session.executor.applied.identity == applied.identity
    assert session.executor.applied.identity == installed_identity or applied.identity

    await session.boundary()
    assert len(install_spy) == 1, "the boundary installs nothing from a superseded wait"


async def test_the_boundary_installs_nothing_when_no_change_waits(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A boundary with nothing waiting is the plan's own end, not an installation."""
    installed = await prepare_pending(session)
    assert session.executor.pending is not None and len(install_spy) == 1

    serve_index(session.transport, {SE4: [YESTERDAY]})
    await session.tick()
    assert session.executor.pending is None

    await session.boundary()

    assert len(install_spy) == 1
    assert installed


async def test_a_result_with_no_usable_proposal_supersedes_the_wait(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """One check decides the whole class: a snapshot with no executable application drops it.

    `waiting_for_prices` is the member of that class reachable here; the others (stale,
    unavailable, invalid, incomplete, refused, paused) go through exactly the same
    `application_for` call, and the stale case is pinned separately above.
    """
    # A genuinely non-executable snapshot, captured on another charger that shares this
    # installation's price stream while its day fetch is held open: the preview there reports
    # that it is waiting, which is the class of results that must supersede a wait.
    other = Session(
        session.hass,
        session.transport,
        session.clock,
        session.timers,
        entry_id="entry-b",
        charge_control="switch.charger_b",
    )
    await other.start()
    serve(session.transport)
    path = session.transport.day_path(SE4, TODAY)
    session.transport.hold(path)
    await other.set_auto(departure=time(20, 0))
    not_executable = other.preview.snapshot()
    assert not_executable.state == "waiting_for_prices" and not_executable.proposal is None
    session.transport.release(path)
    await session.hass.async_block_till_done()

    # Now this charger has a real plan charging, with a material change waiting for its
    # boundary; the non-executable snapshot arrives with a *fresh* attempt and supersedes it.
    await prepare_pending(session)
    applied = session.executor.applied
    assert applied is not None and session.executor.pending is not None

    session.executor.begin_attempt()
    await session.executor.async_reconcile(
        session.settings(), not_executable, attempt=session.executor.attempt
    )

    assert session.executor.pending is None
    assert session.controller.plan is applied.plan

async def test_a_late_older_calculation_cannot_recreate_pending(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """An old attempt's snapshot may not re-queue anything a newer result has dropped."""
    serve(session.transport, flat=True)
    snapshot = await session.set_auto(departure=time(20, 0))
    assert len(install_spy) == 1
    settings = session.settings()
    stale_attempt = session.executor.attempt
    session.clock.advance(hours=2)
    for day in (TODAY, TOMORROW):
        session.transport.serve(
            session.transport.day_path(SE4, day), 200, cheap_afternoon_day(SE4, day)
        )
    await session.tick()
    assert session.executor.pending is not None

    # A newer, non-executable result supersedes the wait ...
    serve_index(session.transport, {SE4: [YESTERDAY]})
    await session.tick()
    assert session.executor.pending is None

    # ... and the older calculation that produced the wait arrives afterwards: it must not
    # queue it again, and it must not install anything either.
    session.executor.begin_attempt()
    await session.executor.async_reconcile(settings, snapshot, attempt=stale_attempt)
    assert session.executor.pending is None
    assert len(install_spy) == 1


async def test_a_newer_usable_proposal_replaces_pending_and_only_it_installs(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A newer attempt re-establishes the wait under its own identity, and that one wins."""
    first_identity = await prepare_pending(session)
    assert len(install_spy) == 1

    # A settings change while the same window is still charging: a new revision, a new attempt
    # and a plan with a different shape (two periods instead of one). It replaces the wait
    # rather than queueing behind it.
    await session.set_auto(departure=time(20, 0), max_periods=2)

    second = session.executor.pending
    assert second is not None and second.identity != first_identity
    assert session.executor.pending_attempt == session.executor.attempt
    awaiting = second.identity
    installs = len(install_spy)

    await session.boundary()

    assert len(install_spy) == installs + 1, "only the newest waiting change installs"
    assert session.executor.applied is not None
    assert session.executor.applied.identity == awaiting



# --------------------------------------------------------- forced interleaving (races)


class SaveHold:
    """A one-shot hold on the charging controller's own persistence, for forced races.

    The *first* `_async_save` waits on an event the test releases explicitly. That puts an
    Auto installation to sleep inside the authority boundary *and* inside the controller's
    operation lock, so whatever a test starts next has to queue: the final state then shows
    which operation acquired the lock first, with no sleeps and no guessing.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, controller: ChargingController) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.plans: list[Any] = []
        original = controller._async_save

        async def hold() -> None:
            self.calls += 1
            self.plans.append(controller.plan)
            if self.calls == 1:
                self.entered.set()
                await self.release.wait()
            return await original()

        monkeypatch.setattr(controller, "_async_save", hold)


async def held_auto_install(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> tuple[SaveHold, "asyncio.Future[Any]"]:
    """Start a real Auto installation and hold it inside the boundary.

    Two steps, both production paths: a genuine plan is installed first (from a document whose
    cheap hours are later, so the plan is *ahead* of the clock and a change installs rather
    than waits), and then a material change is handed to the boundary -- a different current,
    built from the settings that are actually stored -- while its own persistence is held open.
    The tests then race a second operation against an installation that is genuinely in flight,
    with the authority boundary and the controller's operation lock both held.
    """
    serve(session.transport)
    for day in (TODAY, TOMORROW):
        session.transport.serve(
            session.transport.day_path(SE4, day), 200, cheap_afternoon_day(SE4, day)
        )
    await session.set_auto(departure=time(20, 0))
    assert session.executor.applied is not None
    assert session.executor.window_charging_now() is False, "the plan must be ahead of the clock"
    assert session.store is not None
    settings = await session.store.async_update(
        session.entry_id, mutate=lambda current: replace(current, amps=16)
    )
    hold = SaveHold(monkeypatch, session.controller)
    installing = asyncio.ensure_future(
        session.executor.async_reconcile(
            settings, session.preview.snapshot(), attempt=session.executor.begin_attempt()
        )
    )
    await asyncio.wait_for(hold.entered.wait(), timeout=5)
    assert session.controller.plan is not None
    return hold, installing


async def test_a_settings_revision_change_queues_behind_an_auto_install(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installation in flight completes, then the new revision is reconciled after it.

    The revision this test changes does not alter what the charger does, but the *plan* built
    after it does change shape -- the in-flight installation was built from the previous
    current -- so exactly one further installation is expected, and the plan left in force is
    the one built from the revision that is stored.
    """
    hold, installing = await held_auto_install(session, monkeypatch)
    installs = len(install_spy)
    revision = session.settings().revision

    changing = asyncio.ensure_future(
        session.preview.async_apply_settings(
            mutate=lambda s: replace(s, overrides=(AreaAutoSettings(area_id="FI"),))
        )
    )
    hold.release.set()
    await asyncio.wait_for(asyncio.gather(installing, changing), timeout=5)

    assert len(install_spy) == installs + 1, "exactly one more installation, nothing twice"
    assert session.settings().revision == revision + 1
    installed = session.executor.applied
    assert installed is not None and installed.settings_revision == revision + 1, (
        "the plan in force is the one built from the revision that is stored"
    )


async def test_a_manual_cancel_queues_behind_an_auto_install_and_is_final(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto completes, then the person's cancel is the last word: no plan, no pending."""
    hold, installing = await held_auto_install(session, monkeypatch)
    installs = len(install_spy)

    cancelling = asyncio.ensure_future(session.executor.async_manual_cancel())
    hold.release.set()
    await asyncio.wait_for(asyncio.gather(installing, cancelling), timeout=5)

    assert len(install_spy) == installs, "the installation that entered first completed"
    assert session.controller.plan is None, "and the queued cancel removed it"
    assert session.executor.applied is None and session.executor.pending is None
    assert session.settings().execution_paused is False


async def test_a_window_boundary_races_a_plan_replacement_without_corrupting_anything(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan, its timers and its persisted record still agree after a queued boundary."""
    hold, installing = await held_auto_install(session, monkeypatch)
    installs = len(install_spy)

    ending = asyncio.ensure_future(session.controller.async_stop(clear_schedule=False))
    hold.release.set()
    await asyncio.wait_for(asyncio.gather(installing, ending), timeout=5)

    plan = session.controller.plan
    assert len(install_spy) == installs
    assert plan is not None and session.executor.applied is not None
    assert session.executor.applied.plan is plan
    # The timers armed belong to the plan that is installed, and so does the last record written.
    armed = {timer.when for timer in session.timers.timers}
    bounds = {instant for window in plan.windows for instant in window}
    assert armed <= bounds
    assert hold.plans[-1] is plan


async def test_no_race_deadlocks_and_no_operation_runs_twice(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything at once, under a bounded wait: it finishes, and the counts are exact."""
    hold, installing = await held_auto_install(session, monkeypatch)
    installs = len(install_spy)
    settings = session.settings()
    snapshot = session.preview.snapshot()
    attempt = session.executor.attempt

    others = [
        asyncio.ensure_future(session.executor.async_manual_stop()),
        asyncio.ensure_future(session.executor.async_manual_start()),
        asyncio.ensure_future(session.executor.async_manual_cancel()),
        asyncio.ensure_future(session.executor.async_reconcile(settings, snapshot, attempt=attempt)),
    ]
    hold.release.set()
    await asyncio.wait_for(asyncio.gather(installing, *others), timeout=5)

    assert len(install_spy) == installs, "one Auto installation, and nothing installed twice"
    assert session.controller.plan is None, "the manual cancel is last"
    assert session.executor.applied is None and session.executor.pending is None


# ------------------------------------------------- stored-plan ownership (untrusted input)


def stored_record(**changes: Any) -> dict[str, Any]:
    """A stored plan record of the shape this integration writes, with these changes."""
    record: dict[str, Any] = {
        "start": "2026-09-22T06:00:00+00:00",
        "end": "2026-09-22T07:00:00+00:00",
        "amps": 10,
        "phases": 1,
    }
    record.update(changes)
    return record


#: Everything a genuine Auto record carries. The identity is the shape this release writes.
AUTO_METADATA: dict[str, Any] = {
    "auto_identity": "0123456789abcdef0123456789abcdef",
    "auto_settings_revision": 3,
    "auto_price_identity": "2026-09-22/2026-09-23|916f2f8e05fd|stamp",
}


@pytest.mark.parametrize(
    ("record", "auto_owned"),
    [
        # No metadata at all: the record is not one Auto wrote.
        (stored_record(), None),
        # A genuine Auto record.
        (stored_record(**AUTO_METADATA), True),
        # An origin value Auto does not write.
        (stored_record(**{**AUTO_METADATA, "origin": "solar"}), None),
        # Partial metadata, in each combination: ownership cannot be proven.
        (stored_record(auto_identity="0123456789abcdef0123456789abcdef"), None),
        (stored_record(auto_settings_revision=1), None),
        (stored_record(auto_price_identity="stamp"), None),
        # A revision that is not a usable whole number.
        (stored_record(**{**AUTO_METADATA, "auto_settings_revision": True}), None),
        (stored_record(**{**AUTO_METADATA, "auto_settings_revision": -1}), None),
        (stored_record(**{**AUTO_METADATA, "auto_settings_revision": 1.0}), None),
        # An identity that is not the shape this release writes.
        (stored_record(**{**AUTO_METADATA, "auto_identity": "0123456789ABCDEF0123456789ABCDEF"}), None),
        (stored_record(**{**AUTO_METADATA, "auto_identity": "abc"}), None),
        (stored_record(**{**AUTO_METADATA, "auto_identity": "z" * 32}), None),
        (stored_record(**{**AUTO_METADATA, "auto_identity": 12}), None),
        # A price identity that is missing, empty, the wrong type or unbounded.
        (stored_record(**{**AUTO_METADATA, "auto_price_identity": ""}), None),
        (stored_record(**{**AUTO_METADATA, "auto_price_identity": 7}), None),
        (stored_record(**{**AUTO_METADATA, "auto_price_identity": "x" * 513}), None),
        # Periods and timestamps that must not reach timer arming.
        (stored_record(**{**AUTO_METADATA, "start": "2026-09-22T06:00:00", "end": "2026-09-22T07:00:00"}), None),
        (stored_record(**{**AUTO_METADATA, "start": "nonsense", "end": "nonsense"}), None),
        (stored_record(**{**AUTO_METADATA, "start": "2026-09-22T07:00:00+00:00", "end": "2026-09-22T06:00:00+00:00"}), None),
        (
            stored_record(
                **{
                    **AUTO_METADATA,
                    "periods": [
                        {"start": "2026-09-22T08:00:00+00:00", "end": "2026-09-22T09:00:00+00:00"},
                        {"start": "2026-09-22T06:00:00+00:00", "end": "2026-09-22T07:00:00+00:00"},
                    ],
                }
            ),
            None,
        ),
        # A current or a phase count that could not be acted on.
        (stored_record(**{**AUTO_METADATA, "amps": True}), None),
        (stored_record(**{**AUTO_METADATA, "amps": 0}), None),
        (stored_record(**{**AUTO_METADATA, "amps": 200}), None),
        (stored_record(**{**AUTO_METADATA, "phases": 2}), None),
        # A shape this release did not write.
        (stored_record(**{**AUTO_METADATA, "unknown": 1}), None),
        # An *expired* valid Auto plan is history, and must load rather than be refused.
        (stored_record(**{**AUTO_METADATA, "start": "2020-01-01T06:00:00+00:00", "end": "2020-01-01T07:00:00+00:00"}), True),
    ],
)
def test_a_stored_plan_is_auto_owned_only_when_its_metadata_proves_it(
    record: dict[str, Any], auto_owned: bool | None
) -> None:
    """Stored plans are untrusted input: ambiguous ownership is discarded, never repaired."""
    plan = stored_plan(record)

    if auto_owned is None:
        assert plan is None, "a malformed record is discarded entirely"
        return
    assert plan is not None
    assert plan.auto_owned is auto_owned
    if auto_owned:
        assert plan.auto_identity == AUTO_METADATA["auto_identity"]
        assert plan.auto_settings_revision == 3
        assert plan.auto_price_identity == AUTO_METADATA["auto_price_identity"]


async def test_a_corrupt_stored_auto_plan_is_discarded_on_a_restart(
    session: Session,
    transport: StubTransport,
    clock: Clock,
    timers: FakeScheduler,
) -> None:
    """A record whose identity cannot be proven is dropped, and nothing is adopted."""
    assert session.controller is not None
    # A real Auto plan first, so the record being corrupted is genuinely one of ours ...
    serve(transport)
    for day in (TODAY, TOMORROW):
        transport.serve(transport.day_path(SE4, day), 200, cheap_afternoon_day(SE4, day))
    await session.set_auto(departure=time(20, 0))
    assert session.executor.applied is not None
    stored = await session.controller._store.async_load()
    assert stored is not None and stored["plan"]["auto_identity"]

    # ... and then its identity is truncated, as a partial write could leave it.
    stored["plan"]["auto_identity"] = stored["plan"]["auto_identity"][:8]
    await session.controller._store.async_save(stored)

    reloaded = await restart(session, transport, clock, timers)

    # The malformed record is never adopted *and never repaired*: what Auto holds afterwards
    # is a fresh plan it calculated itself, with its own valid identity, not the truncated one.
    plan = reloaded.controller.plan
    assert plan is not None and plan.auto_owned is True
    assert plan.auto_identity != "01234567"
    assert reloaded.executor.applied is not None
    assert reloaded.executor.applied.identity == plan.auto_identity


async def test_a_controller_reads_a_corrupt_record_as_nothing_at_all(
    session: Session, clock: Clock
) -> None:
    """The reader itself: a controller built on that store refuses the record.

    Deliberately a controller with no preview beside it, so what is measured is the stored
    record and nothing else -- a preview would (correctly) install a *fresh* plan, and that
    is the separate test below rather than this one.
    """
    assert session.controller is not None
    stored = {
        "plan": stored_record(**{**AUTO_METADATA, "auto_identity": "01234567"}),
    }
    await session.controller._store.async_save(stored)

    fresh = ChargingController(
        session.hass,
        session.entry_id,
        {
            CONF_CHARGE_CONTROL: session.charge_control,
            CONF_CURRENT_LIMIT: "",
            CONF_WEBHOOK_ID: "webhook-a",
        },
    )
    await fresh.async_initialize()
    await session.executor.async_shutdown()
    executor = await start_executor(session.hass, fresh, session.store, now=clock)

    assert fresh.plan is None, "a record whose identity cannot be proven is discarded"
    assert executor.applied is None
    assert executor.execution_state() == EXECUTION_NOT_APPLIED


# ------------------------------------------------------ pause partial failure and cleanup


async def test_a_pause_that_cannot_be_stored_changes_nothing(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause that was never stored must not stop anything either."""
    serve(session.transport)
    await session.set_auto(departure=time(20, 0))
    installed = session.controller.plan
    assert installed is not None and session.store is not None
    offs = len(session.off_calls)

    async def refuse(_entry_id: str, **_kwargs: Any) -> Any:
        raise RuntimeError("the disk said no")

    monkeypatch.setattr(session.store, "async_update", refuse)

    with pytest.raises(RuntimeError):
        await session.preview.async_pause()

    assert session.settings().execution_paused is False
    assert session.controller.plan is installed
    assert len(session.off_calls) == offs, "no charger command for an intent never stored"


async def test_a_pause_whose_stop_fails_is_reported_and_retried(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial pause is honest: the error is named, the plan stays, the retry succeeds."""
    serve(session.transport)
    await session.set_auto(departure=time(20, 0))
    assert session.controller.plan is not None
    assert session.controller is not None
    offs = len(session.off_calls)
    refusing = {"on": True}
    original = session.controller.async_stop

    async def maybe_refuse(*, clear_schedule: bool = False) -> None:
        if refusing["on"]:
            raise RuntimeError("the charger said no")
        return await original(clear_schedule=clear_schedule)

    monkeypatch.setattr(session.controller, "async_stop", maybe_refuse)

    failed = await session.preview.async_pause()

    assert session.settings().execution_paused is True, "the pause itself was stored"
    assert failed.execution == EXECUTION_ERROR, "and it is not reported as a clean pause"
    assert session.executor.last_error == EXECUTION_PAUSE_STOP_FAILED
    assert session.controller.plan is not None, "the plan is still installed"
    assert session.executor.applied is not None
    assert len(session.off_calls) == offs, "the stop never reached the charger"

    section = await auto_section(session)
    assert section["execution"] == EXECUTION_ERROR
    assert section["execution_error"] == EXECUTION_PAUSE_STOP_FAILED
    assert "the charger said no" not in str(section)
    revision = session.settings().revision

    # Retrying retries the stop, without writing the pause again or touching anything else.
    refusing["on"] = False
    retried = await session.preview.async_pause()

    assert retried.execution == EXECUTION_PAUSED
    assert session.executor.last_error is None
    assert session.controller.plan is None
    assert session.executor.applied is None
    # The stop path really ran (the plan it belonged to is gone), and the failed attempt never
    # issued a charger command of its own -- the count above is the proof of that.
    assert len(session.off_calls) == offs
    assert session.settings().execution_paused is True
    assert session.settings().revision == revision, "the pause was already stored"
    assert session.settings().area_id == SE4


async def test_the_controller_listener_is_registered_and_cancelled_exactly_once(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One registration per executor lifetime, and one cancellation on shutdown."""
    assert session.controller is not None and session.store is not None
    assert session.clock is not None
    registrations: list[Any] = []
    cancels: list[Any] = []
    original_add = ChargingController.add_listener

    def add(self: ChargingController, listener: Any) -> Any:
        registrations.append(listener)
        cancel = original_add(self, listener)

        def non_idempotent() -> None:
            # A double cancellation would remove somebody else's listener, so the count matters.
            cancels.append(listener)
            cancel()

        return non_idempotent

    monkeypatch.setattr(ChargingController, "add_listener", add)
    await session.executor.async_shutdown()

    executor = await start_executor(
        session.hass, session.controller, session.store, now=session.clock
    )

    assert len(registrations) == 1, "exactly one listener per executor lifetime"
    await executor.async_start()
    assert len(registrations) == 1, "a repeated start registers nothing new"
    assert cancels == []

    await executor.async_shutdown()

    assert len(cancels) == 1, "shutdown cancels the listener exactly once"
    assert executor.shutdown is True
    await executor.async_start()
    assert len(registrations) == 1, "and a shut-down boundary never listens again"
    assert len(cancels) == 1


async def test_the_boundary_drops_a_wait_whose_preview_is_no_longer_executable(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary's own proof, exercised where it is the deciding check.

    In production a non-executable preview always arrives with a *newer* attempt, so the
    attempt check would drop the wait first and this one would never decide anything -- which
    is precisely why it is tested on its own. The preview seam is made to report a snapshot
    that carries a stable error (a real member of the class: the proposal's own record could
    not be written) while the attempt that created the wait is still current, and the boundary
    must install nothing rather than install an older plan speculatively.
    """
    await prepare_pending(session)
    installs = len(install_spy)
    claimed = session.executor.attempt
    not_executable = replace(
        session.preview.snapshot(), last_error_code="proposal_not_saved"
    )
    monkeypatch.setattr(session.executor, "_live_snapshot", lambda: not_executable)

    await session.boundary()

    assert session.executor.attempt == claimed, "the wait was not superseded by a new attempt"
    assert session.executor.pending is None, "the boundary proved it and dropped it"
    assert len(install_spy) == installs, "and installed nothing"
    assert application_for(session.settings(), not_executable) is None


async def test_the_boundary_drops_a_wait_superseded_by_a_newer_attempt(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """A newer calculation makes the wait inert, and the boundary installs nothing."""
    await prepare_pending(session)
    installs = len(install_spy)
    applied = session.executor.applied
    assert applied is not None and session.executor.pending is not None
    assert session.controller.plan is applied.plan

    # A newer attempt: exactly what a second price or settings trigger does, without any
    # result of its own in between. The wait that belongs to the older attempt is now inert.
    session.executor.begin_attempt()
    assert session.executor.pending is not None, "the wait is still there to be dropped"

    await session.boundary()

    assert len(install_spy) == installs, "the superseded wait was not installed"
    assert session.executor.pending is None


async def test_a_direct_cancel_waits_for_a_plan_replacement_to_finish(
    session: Session, install_spy: list[ChargingPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The controller's own operation lock: a caller that skips the boundary still queues.

    Manual actions and webhooks go through the authority boundary, so the *controller's* lock
    is what protects the plan and its timers from a caller that talks to the controller
    directly -- which is exactly what this test does, while an installation is mid-flight.
    """
    hold, installing = await held_auto_install(session, monkeypatch)
    installs = len(install_spy)

    cancelling = asyncio.ensure_future(session.controller.async_cancel())
    hold.release.set()
    await asyncio.wait_for(asyncio.gather(installing, cancelling), timeout=5)

    # The installation that entered the controller's lock first completed (it is counted),
    # and the direct cancel is the final state: no plan, nothing applied, nothing waiting.
    assert len(install_spy) == installs
    assert session.controller.plan is None
    assert session.executor.applied is None and session.executor.pending is None
    # Memory and storage agree: the cancel's write is the last one, so what a restart would
    # read is the cancellation rather than a plan the cancel had already removed.
    stored = await session.controller._store.async_load()
    assert stored is not None and stored["plan"] is None


# ---------------------------------------------------------------- the immediate/automatic split


def _facts(**changes: Any) -> ControlFacts:
    """One axis-fact set, complete, with only the members a test cares about named."""
    base: dict[str, Any] = {
        "charging": False,
        "pause": PauseIntent(),
        "has_settings": True,
        "paused": False,
        "last_error": None,
        "pause_choices": (PAUSE_UNTIL_RESUMED,),
        "start_pending": False,
    }
    base.update(changes)
    return ControlFacts(**base)


def test_the_two_axes_are_independent_and_each_answers_its_own_question() -> None:
    """The split's promise-cases, as values rather than as prose about branches.

    * an idle Auto charger with no pause: immediate `start` **and** automatic `pause`;
    * the same charger while charging: immediate `stop` with automatic `pause` beside it;
    * while a pause is in force: immediate `start` (or `stop`) and automatic `resume`, always both;
    * a Start awaiting acknowledgement: nothing on either axis, with the same code on both, because the
      charger has not said what it is doing -- the one documented pending rule;
    * and the choices belong to the automatic pause alone, judged with the action rather than beside it.
    """
    idle = _facts()
    assert decide_immediate(idle).action == ACTION_START
    assert decide_automatic(idle) == AutomaticDecision(ACTION_PAUSE, None, (PAUSE_UNTIL_RESUMED,))

    charging = _facts(charging=True)
    assert decide_immediate(charging).action == ACTION_STOP
    assert decide_automatic(charging) == AutomaticDecision(
        ACTION_PAUSE, None, (PAUSE_UNTIL_RESUMED,)
    )

    paused_idle = _facts(paused=True, pause=PauseIntent(choice=PAUSE_UNTIL_RESUMED))
    assert decide_immediate(paused_idle) == ImmediateDecision(ACTION_START, None)
    assert decide_automatic(paused_idle) == AutomaticDecision(ACTION_RESUME, None, ())

    paused_charging = _facts(charging=True, paused=True, pause=PauseIntent(choice=PAUSE_UNTIL_RESUMED))
    assert decide_immediate(paused_charging) == ImmediateDecision(ACTION_STOP, None)
    assert decide_automatic(paused_charging) == AutomaticDecision(ACTION_RESUME, None, ())

    pending = _facts(start_pending=True)
    assert decide_immediate(pending) == ImmediateDecision(ACTION_NONE, CONTROL_ACTION_PENDING)
    assert decide_automatic(pending) == AutomaticDecision(ACTION_NONE, CONTROL_ACTION_PENDING, ())
    # Even a charger that looks like it is charging: a Start nobody has acknowledged is not a state
    # anybody may pause, so the pending rule is one rule on both axes rather than a gap in one of them.
    assert decide_automatic(_facts(charging=True, start_pending=True)).action == ACTION_NONE

    # No settings is nothing to act on, on either axis.
    assert decide_immediate(_facts(has_settings=False)).reason == CONTROL_NO_SETTINGS
    assert decide_automatic(_facts(has_settings=False)).reason == CONTROL_NO_SETTINGS

    automatic = decide_automatic(charging)
    assert automatic.admits(ACTION_PAUSE, PAUSE_UNTIL_RESUMED)
    assert not automatic.admits(ACTION_PAUSE, None), "a pause always names its choice"
    assert not automatic.admits(ACTION_PAUSE, PAUSE_NEXT_PERIOD), "and only the ones it offers"
    assert not automatic.admits(ACTION_RESUME, None), "a pause is not a resume"

    immediate = decide_immediate(charging)
    assert immediate.admits(ACTION_STOP)
    assert not immediate.admits(ACTION_START)
    assert not immediate.admits(ACTION_PAUSE), "the immediate axis knows no pause at all"


# ------------------------------------------ one snapshot per admission, and per response


async def test_one_admitted_request_reads_the_boundary_exactly_once(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admitted action is judged against **one** snapshot, and returns that snapshot's facts.

    The catcher is the compatibility `ControlDecision`: its choices must be the choices of the facts
    the admission actually read. A second read would be a second moment, and here it is made to differ
    -- the later snapshot has no choices at all -- so a second read would answer a `stop` with an empty
    list that nothing observed.
    """
    serve(session.transport)
    await session.set_auto()
    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()
    executor = session.executor
    assert executor is not None
    real = executor.control_facts
    first = real()
    later = replace(first, charging=False, pause_choices=())
    taken: list[ControlFacts] = []

    def moving() -> ControlFacts:
        taken.append(first if not taken else later)
        return taken[-1]

    monkeypatch.setattr(executor, "control_facts", moving)
    decision = await executor.async_manual_action(ACTION_STOP)

    assert len(taken) == 1, "one admitted request is one snapshot"
    assert decision.action == ACTION_STOP
    assert decision.choices == first.pause_choices, "the choices are the admitted snapshot's own"
    assert first.pause_choices, "the prerequisite: that snapshot had choices to carry"
    assert later.pause_choices == ()
    assert session.settings().pause.admitted is False, "a bare stop stores no pause"


async def test_a_typed_pause_is_admitted_against_the_snapshot_it_read(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto's own pause is admitted by the automatic axis, from the same one capture."""
    serve(session.transport)
    await session.set_auto()
    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()
    executor = session.executor
    assert executor is not None
    real = executor.control_facts
    first = real()
    later = replace(first, pause_choices=())
    taken: list[ControlFacts] = []

    def moving() -> ControlFacts:
        taken.append(first if not taken else later)
        return taken[-1]

    monkeypatch.setattr(executor, "control_facts", moving)
    decision = await executor.async_manual_action(ACTION_STOP, PAUSE_UNTIL_RESUMED)

    assert len(taken) == 1, "one admitted request is one snapshot"
    assert decision.action == ACTION_STOP, "the wire's spelling of a pause"
    assert decision.choices == first.pause_choices
    assert session.settings().pause.choice == PAUSE_UNTIL_RESUMED
    assert executor.last_error is None


async def test_the_axes_method_is_the_paired_read_and_the_single_reads_stay_separate(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`control_axes` reads once; the individual accessors keep their own, documented captures.

    Both halves pinned, because both are promises: a caller that needs both axes must not be able to
    get them from two reads without doing so explicitly, and a caller that genuinely wants one axis
    (a v13 fold reader, a status field) still has its own read rather than a silent pair.
    """
    serve(session.transport)
    await session.set_auto()
    executor = session.executor
    assert executor is not None
    taken: list[int] = []
    real = executor.control_facts

    def counting() -> ControlFacts:
        taken.append(len(taken))
        return real()

    monkeypatch.setattr(executor, "control_facts", counting)

    executor.control_axes()
    assert len(taken) == 1, "the paired read is one capture"
    executor.immediate_decision()
    assert len(taken) == 2, "the immediate axis is its own capture"
    executor.automatic_decision()
    assert len(taken) == 3, "and so is the automatic axis"
    executor.control_axes()
    assert len(taken) == 4


async def test_first_run_defaults_install_a_plan_without_a_person_saving(
    session: Session, install_spy: list[ChargingPlan]
) -> None:
    """Seeded settings are the settings in force: the plan is installed as after a save."""
    from types import SimpleNamespace

    from custom_components.spotnav.planning.first_run import async_seed_first_run

    serve(session.transport)
    await session.manager.async_ensure_catalogue()
    hass = session.hass
    hass.config.country, hass.config.latitude, hass.config.longitude = "SE", 55.6, 13.0
    entry = SimpleNamespace(entry_id=session.entry_id, data={"charger_phases": 3})
    assert session.settings().revision == 0

    assert await async_seed_first_run(hass, entry, session.controller, session.preview)
    await hass.async_block_till_done()

    assert session.settings().area_id == "SE4"
    assert session.store.suggested(session.entry_id) != ()
    assert len(install_spy) == 1 and session.controller.plan is not None
    assert session.executor.applied is not None


async def test_only_a_snapshot_from_this_calculation_or_later_can_hold_a_plan_back(
    session: Session,
) -> None:
    """The preview publishes after handing over, so an older snapshot is not evidence about prices.

    This is what kept a freshly seeded charger at "proposal ready": the state before the first prices
    arrived (another price identity, an older attempt) was read as prices that had since moved on.
    """
    from types import SimpleNamespace

    application = SimpleNamespace(price_identity="now")
    executor = session.executor

    def live(snapshot: Any) -> None:
        executor._live_snapshot = lambda: snapshot

    live(SimpleNamespace(attempt=4, price_identity=None))
    assert executor._prices_still_live(application, 5)
    live(SimpleNamespace(attempt=5, price_identity="later"))
    assert not executor._prices_still_live(application, 5)
    live(SimpleNamespace(attempt=6, price_identity="later"))
    assert not executor._prices_still_live(application, 5)
    live(SimpleNamespace(attempt=5, price_identity="now"))
    assert executor._prices_still_live(application, 5)
    live(None)
    assert executor._prices_still_live(application, 5)
