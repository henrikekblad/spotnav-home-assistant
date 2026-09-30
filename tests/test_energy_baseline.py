"""`manual_kwh`'s delivered-energy baseline (`auto_controller._manual_kwh_remaining`,
`auto_settings.EnergyBaseline`).

`requested_kwh` must be reduced by energy already delivered, or a recompute during an active
window queues a second full-need proposal and buys the same energy twice. The charger's cumulative
energy register (`CONF_ENERGY_REGISTER_ENTITY`) is baselined once per departure occurrence
(`AutoPlannerController._departure_key`) and persisted beside settings
(`AutoSettingsStore.energy_baseline`) so a restart cannot lose it. `_energy_for` is the one place
both `cheapest` and `hybrid` get the remaining need.

Built on `test_auto_execution.py`'s `Session` harness (real `ChargingController`, `AutoExecutor`
and `AutoPlannerController`; recording doubles for the wire, clock and window timers), plus a real
`MockConfigEntry` because `_read_energy_register` reads the entry's data.
"""

from __future__ import annotations

from datetime import time

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.auto_settings import AutoSettingsStore
from custom_components.spotnav.planning.planner import power_kw

from .relay import serve
from .relay import Clock
from .harness import Session

ENERGY_ENTITY = "sensor.entry_a_energy_register"


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """`test_auto_execution.py`'s own fixture of the same name is `autouse` only within that
    module; importing its *fixtures* here does not import its autouse-ness, so it is
    reproduced verbatim -- see that module's own docstring for why the controller's shared
    plan rules (`validate_plan`'s "end time must be in the future") need it."""
    now = clock()

    def frozen(*_args, **_kwargs):
        return now

    monkeypatch.setattr(dt_util, "utcnow", frozen)


def _set_register(hass: HomeAssistant, kwh: float | None) -> None:
    if kwh is None:
        hass.states.async_remove(ENERGY_ENTITY)
        return
    hass.states.async_set(
        ENERGY_ENTITY,
        str(kwh),
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )


def _wire_energy_register(session: Session) -> None:
    """`ChargingController.energy_register_entity_id` resolves once at that controller's own
    construction (the explicit override, or -- for an OCPP charger -- one
    `ocpp_identity.energy_register_entity_for` finds automatically), never re-read from a
    config entry afterwards. `Session`'s own bare `ChargingController` is built directly, with
    no config entry behind it at all, so the override is set on the already-built controller
    directly -- the generic-charger case this whole module is about; the OCPP auto-resolution
    itself is `tests/test_ocpp_012_compatibility.py`'s registry-shaped fixtures' job.
    """
    session.controller.energy_register_entity_id = ENERGY_ENTITY


async def test_a_fresh_baseline_is_established_on_the_first_compute(session: Session) -> None:
    _wire_energy_register(session)
    _set_register(session.hass, 100.0)
    serve(session.transport, flat=True)

    await session.set_auto(requested_kwh=10.0, departure=time(20, 0))

    baseline = session.store.energy_baseline(session.entry_id)
    assert baseline is not None
    assert baseline.register_kwh == 100.0
    assert session.controller.plan is not None, "the full request -- nothing delivered yet"


async def test_delivered_energy_reduces_the_remaining_need_not_bought_twice(
    session: Session,
) -> None:
    """The core regression: a recompute *while a window is charging* must not re-request the
    full original need -- confirmed, by hand, to fail against the code before this fix (see
    the module docstring): reverting `_energy_for`'s `manual_kwh` branch to
    `return settings.requested_kwh` makes the assertions below fail.
    """
    _wire_energy_register(session)
    _set_register(session.hass, 0.0)
    serve(session.transport, flat=True)

    requested_kwh = 10.0
    await session.set_auto(requested_kwh=requested_kwh, amps=10, phases=3, departure=time(20, 0))
    installed = session.executor.applied
    assert installed is not None
    power = power_kw(10, 3)  # ~6.928 kW
    one_slot_kwh = power * 0.25  # `planner.STEP_MINUTES`'s own slot length

    # Four recomputes, one every 15 minutes across the one-hour window -- as frequent as
    # `hybrid`'s own periodic preview recompute would make them in practice (never as sparse
    # as a single mid-window snapshot) -- each reading a register updated to reflect exactly
    # what the still-charging window has delivered by that instant. `window_charging_now()`
    # holds every one of these as pending; only the last, freshest one is what actually
    # applies at the boundary.
    delivered_so_far = 0.0
    for _ in range(4):
        session.clock.advance(minutes=15)
        delivered_so_far += power * 0.25
        _set_register(session.hass, delivered_so_far)
        recomputed = await session.preview.async_recalculate()
        assert session.executor.window_charging_now() is True, "still inside the original window"
        remaining_at_recompute = requested_kwh - delivered_so_far
        assert recomputed.proposal is not None
        assert recomputed.proposal.delivered_kwh <= remaining_at_recompute + one_slot_kwh, (
            "a plan computed mid-window must not still be sized for the whole original request"
        )

    # The window ends exactly where the last recompute already expected it to.
    await session.boundary()

    baseline = session.store.energy_baseline(session.entry_id)
    assert baseline is not None
    # The baseline itself never moves within one epoch -- only the *reading against it*
    # does -- so it still reads the register's value from when this departure occurrence
    # began, not any of the intermediate readings the recomputes above saw.
    assert baseline.register_kwh == pytest.approx(0.0)
    # The whole day's plans -- the original window plus whatever was installed to follow it
    # -- must not add up to materially more than the original request: never the double
    # charge this fix exists to prevent.
    total_planned = delivered_so_far + (
        0.0 if session.executor.applied is None else session.executor.applied.plan.energy_kwh
    )
    assert total_planned <= requested_kwh + one_slot_kwh, (
        "the whole day's plans must not add up to materially more than the original request"
    )


async def test_the_baseline_survives_a_restart(session: Session) -> None:
    _wire_energy_register(session)
    _set_register(session.hass, 5.0)
    serve(session.transport, flat=True)
    await session.set_auto(requested_kwh=10.0, departure=time(20, 0))

    reloaded = AutoSettingsStore(session.hass)
    await reloaded.async_load()

    baseline = reloaded.energy_baseline(session.entry_id)
    assert baseline is not None
    assert baseline.register_kwh == 5.0


async def test_the_baseline_resets_on_a_new_departure(session: Session) -> None:
    _wire_energy_register(session)
    _set_register(session.hass, 0.0)
    serve(session.transport, flat=True)
    await session.set_auto(requested_kwh=10.0, departure=time(20, 0))
    first = session.store.energy_baseline(session.entry_id)
    assert first is not None

    _set_register(session.hass, 3.0)  # some energy delivered under the old departure
    await session.set_auto(requested_kwh=10.0, departure=time(21, 0))  # a new departure

    second = session.store.energy_baseline(session.entry_id)
    assert second is not None
    assert second.departure_key != first.departure_key
    assert second.register_kwh == 3.0, "re-baselined at the register's current reading"
    proposal = session.preview.snapshot().proposal
    assert proposal is not None and proposal.delivered_kwh >= 10.0 - 0.5, (
        "the full request again -- nothing delivered yet toward the new departure"
    )


async def test_the_baseline_resets_once_the_departure_passes(session: Session) -> None:
    _wire_energy_register(session)
    _set_register(session.hass, 0.0)
    serve(session.transport, flat=True)
    await session.set_auto(requested_kwh=10.0, departure=time(20, 0))
    first = session.store.energy_baseline(session.entry_id)
    assert first is not None

    _set_register(session.hass, 4.0)
    session.clock.advance(hours=20)  # now well past today's 20:00 departure
    await session.preview.async_recalculate()

    second = session.store.energy_baseline(session.entry_id)
    assert second is not None
    assert second.departure_key != first.departure_key, "a new occurrence, a fresh epoch"
    assert second.register_kwh == 4.0


async def test_a_missing_register_gives_no_subtraction(session: Session) -> None:
    _wire_energy_register(session)
    # No register entity at all in `hass.states` this time.
    serve(session.transport, flat=True)
    snapshot = await session.set_auto(requested_kwh=10.0, departure=time(20, 0))
    assert snapshot.proposal is not None
    assert snapshot.proposal.delivered_kwh >= 10.0 - 0.5, "the full request, never a guess"


async def test_a_meter_reset_gives_no_subtraction_then_resumes(session: Session) -> None:
    _wire_energy_register(session)
    _set_register(session.hass, 50.0)
    serve(session.transport, flat=True)
    await session.set_auto(requested_kwh=10.0, departure=time(20, 0))

    _set_register(session.hass, 2.0)  # a meter reset: lower than the stored baseline (50.0)
    reset_snapshot = await session.preview.async_recalculate()
    assert reset_snapshot.proposal is not None
    assert reset_snapshot.proposal.delivered_kwh >= 10.0 - 0.5, "no subtraction across a reset"

    baseline = session.store.energy_baseline(session.entry_id)
    assert baseline is not None and baseline.register_kwh == 2.0, "re-baselined at the new low"

    _set_register(session.hass, 3.0)  # a further 1 kWh delivered since the reset
    resumed_snapshot = await session.preview.async_recalculate()
    assert resumed_snapshot.proposal is not None
    assert resumed_snapshot.proposal.delivered_kwh <= 10.0 - 1.0 + 0.5, (
        "tracking resumes correctly from the post-reset baseline"
    )
