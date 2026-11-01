"""A charger's energy register can change while a kWh need is being counted: the upgrade replaces a stored per-session
counter by the lifetime register at setup, a person picks another one, or the energy integrated from a power sensor
appears or goes. Two registers count from different origins (a session counter at 5 kWh, a lifetime one at 6989 kWh),
so a count never runs across them: on another entity it carries what it counted and starts again from the new
register's first reading.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import CONF_ENERGY_REGISTER_ENTITY, DOMAIN
from custom_components.spotnav.planning.auto_controller import counted_kwh, follow_register
from custom_components.spotnav.planning.auto_settings import (
    AutoSettings,
    AutoSettingsStore,
    EnergyBaseline,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUPPORTED_SCHEMAS,
)

from .harness import Session
from .relay import Clock, serve

SESSION_COUNTER = "sensor.charger_session_energy"
LIFETIME = "sensor.charger_lifetime_energy"


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    now = clock()
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: now)


def _reading(hass: HomeAssistant, entity_id: str, kwh: float) -> None:
    hass.states.async_set(
        entity_id, str(kwh), {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}
    )


async def _counting_on_the_session_counter(session: Session) -> None:
    started = session.clock()
    session.hass.states.async_set(session.charge_control, "on")
    session.controller.charging_seconds = lambda: (session.clock() - started).total_seconds()  # type: ignore[method-assign]
    session.controller.energy_register_entity_id = SESSION_COUNTER
    _reading(session.hass, SESSION_COUNTER, 0.0)
    _reading(session.hass, LIFETIME, 6989.3)
    serve(session.transport, flat=True)
    await session.set_auto(requested_kwh=30.0, amps=16, phases=3, departure=time(20, 0))
    session.clock.advance(minutes=30)
    _reading(session.hass, SESSION_COUNTER, 5.0)
    await session.hass.async_block_till_done()


async def test_a_register_swapped_mid_count_carries_the_count_and_never_counts_across_the_two(
    session: Session,
) -> None:
    await _counting_on_the_session_counter(session)
    counted = (await session.preview.async_recalculate()).delivered_kwh
    assert counted == pytest.approx(5.0)

    # What setup does when it replaces the stored session counter with the lifetime register.
    session.controller.energy_register_entity_id = LIFETIME
    await session.preview.async_recalculate()
    session.clock.advance(minutes=1)
    _reading(session.hass, LIFETIME, 6989.4)
    await session.hass.async_block_till_done()
    session.clock.advance(minutes=1)
    _reading(session.hass, LIFETIME, 6989.6)
    await session.hass.async_block_till_done()

    snapshot = await session.preview.async_recalculate()
    baseline = session.store.energy_baseline(session.entry_id)
    assert baseline.met_at is None, "the need is not met by the distance between two registers"
    assert snapshot.delivered_kwh == pytest.approx(5.0 + 0.3, abs=1e-6)
    assert snapshot.remaining_kwh == pytest.approx(30.0 - 5.3, abs=1e-6)


async def test_a_register_that_cannot_be_read_yet_after_the_swap_counts_from_its_first_reading(
    session: Session,
) -> None:
    await _counting_on_the_session_counter(session)
    await session.preview.async_recalculate()
    session.hass.states.async_remove(LIFETIME)

    session.controller.energy_register_entity_id = LIFETIME
    await session.preview.async_recalculate()
    session.clock.advance(minutes=1)
    _reading(session.hass, LIFETIME, 6989.3)
    await session.hass.async_block_till_done()
    await session.preview.async_recalculate()
    session.clock.advance(minutes=1)
    _reading(session.hass, LIFETIME, 6989.5)
    await session.hass.async_block_till_done()

    snapshot = await session.preview.async_recalculate()
    assert session.store.energy_baseline(session.entry_id).met_at is None
    assert snapshot.delivered_kwh == pytest.approx(5.2, abs=1e-6)


def test_follow_register_carries_the_count_and_takes_a_baseline_with_no_register_as_it_is() -> None:
    start = datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc)
    counting = EnergyBaseline(
        register_kwh=0.0, departure_key="k", last_register_kwh=5.0, carried_kwh=1.0, register_entity=SESSION_COUNTER,
        rejected_kwh=7.0, rejected_at=start, pending_drop_kwh=0.1, pending_drop_at=start, pending_drop_count=1,
    )

    moved = follow_register(counting, LIFETIME, 6989.3, start, charged_s=600.0)

    assert (moved.register_entity, moved.register_kwh, moved.last_register_kwh) == (LIFETIME, 6989.3, 6989.3)
    assert moved.carried_kwh == pytest.approx(6.0) and counted_kwh(moved) == pytest.approx(6.0)
    assert (moved.rejected_kwh, moved.pending_drop_kwh, moved.pending_drop_count) == (None, None, 0)
    assert follow_register(moved, LIFETIME, 6990.0, start, charged_s=700.0) is moved
    unnamed = EnergyBaseline(register_kwh=3.0, departure_key="k", last_register_kwh=4.0)
    assert follow_register(unnamed, LIFETIME, 6989.3, start, charged_s=0.0) == EnergyBaseline(
        register_kwh=3.0, departure_key="k", last_register_kwh=4.0, register_entity=LIFETIME
    )


async def test_a_count_stored_before_registers_were_named_is_taken_as_the_entrys_register_when_loaded(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="det_old", data={CONF_ENERGY_REGISTER_ENTITY: SESSION_COUNTER}
    )
    entry.add_to_hass(hass)
    settings = AutoSettings().as_dict()
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "data": {
            "schema": max(SUPPORTED_SCHEMAS),
            "chargers": {
                "det_old": {
                    "settings": settings,
                    "energy_baseline": {"register_kwh": 0.0, "departure_key": "k", "last_register_kwh": 5.0},
                }
            },
        },
    }
    store = AutoSettingsStore(hass)
    await store.async_load()

    assert store.energy_baseline("det_old").register_entity == SESSION_COUNTER


async def test_the_stored_count_after_a_swap_is_the_calculations_own(session: Session) -> None:
    """The swap's save and the calculation's own save are made in order: the record left is the newer one."""
    await _counting_on_the_session_counter(session)
    await session.preview.async_recalculate()

    session.controller.energy_register_entity_id = LIFETIME
    await session.set_auto(requested_kwh=40.0, amps=16, phases=3, departure=time(20, 0))
    await session.hass.async_block_till_done()

    stored = session.store.energy_baseline(session.entry_id)
    assert stored.register_entity == LIFETIME
    assert stored.delivered_kwh == pytest.approx(5.0)
    assert stored.remaining_kwh == pytest.approx(35.0)


async def test_a_swap_seen_by_the_register_watcher_never_overwrites_a_newer_count(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher's save is a task: it saves the count as it stands when it runs, so a calculation that saved a
    newer one first is kept. (Home Assistant starts a task eagerly, which mostly hides the order; here it starts
    late, as a task that has to wait does.)"""
    await _counting_on_the_session_counter(session)
    loop = session.hass.loop
    monkeypatch.setattr(session.hass, "async_create_task", lambda target, *_args, **_kwargs: loop.create_task(target))
    await session.preview.async_recalculate()
    preview = session.preview
    session.controller.energy_register_entity_id = LIFETIME

    # The watcher sees the swap (and queues its save); a calculation saves a newer count before that save runs.
    preview._arm_energy_watch(session.store.settings(session.entry_id))
    newer = replace(preview._live_baseline, remaining_kwh=12.0)
    await preview._save_baseline(newer)
    await session.hass.async_block_till_done()

    assert session.store.energy_baseline(session.entry_id).remaining_kwh == 12.0
