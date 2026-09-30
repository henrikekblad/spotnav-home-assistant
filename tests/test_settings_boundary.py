"""The exact commit boundary in `AutoPlannerController.async_apply_settings()`.

Deterministic doubles only at the reconcile seam: a real store, a real controller and a patched
`_reconcile`, so what these tests pin is the classification a transport depends on -- pre-commit
refusals keep their own code, and anything raised after the store returned is a committed write.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_controller import (
    SETTINGS_RECONCILE_FAILED_CODE,
    AutoPlannerController,
    SettingsReconcileError,
)
from custom_components.spotnav.planning.auto_settings import (
    AutoSettingsError,
)


from tests.harness import Harness
from tests.relay import Clock, StubTransport, serve


@pytest.fixture
async def harness(hass: HomeAssistant) -> Harness:
    """A real repository, manager, store and controller, with only the transport and clock doubled."""
    transport = StubTransport()
    # The relay answers the catalogue, today and tomorrow: a reconcile needs prices to calculate from.
    serve(transport)
    session = Harness(hass, transport, Clock())
    await session.start()
    return session


@pytest.mark.asyncio
async def test_a_pre_commit_refusal_is_not_wrapped_and_writes_nothing(harness: Harness):
    controller = await harness.auto("entry-a")
    before = harness.store.settings("entry-a")

    with pytest.raises(AutoSettingsError) as refusal:
        await controller.async_apply_settings(
            mutate=lambda settings: replace(settings, amps=32), expected_revision=99
        )
    assert refusal.value.code == "revision_conflict"
    assert not isinstance(refusal.value, SettingsReconcileError)
    assert harness.store.settings("entry-a") == before


@pytest.mark.asyncio
async def test_an_error_from_reconcile_is_a_committed_write(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    controller = await harness.auto("entry-a")
    before = harness.store.settings("entry-a")
    seen: list[int] = []

    async def failing_reconcile(_self, committed):
        seen.append(committed.revision)
        # The same *type* pre-commit validation and CAS use: after the store returned it must still
        # be classified as a committed write, because the record really is stored.
        raise AutoSettingsError("invalid_mode", "reconcile said no")

    monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_reconcile)
    with pytest.raises(SettingsReconcileError) as failure:
        await controller.async_apply_settings(
            mutate=lambda settings: replace(settings, amps=32), expected_revision=before.revision
        )

    assert failure.value.code == SETTINGS_RECONCILE_FAILED_CODE
    assert seen == [before.revision + 1]
    assert failure.value.settings.revision == before.revision + 1
    assert failure.value.settings.amps == 32
    # Committed: the store holds the new record, and the message carries no exception prose.
    assert harness.store.settings("entry-a") == failure.value.settings
    assert "reconcile said no" not in str(failure.value)


@pytest.mark.asyncio
async def test_a_later_mutation_cannot_change_what_a_failed_write_reports(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    controller = await harness.auto("entry-a")
    # `harness.auto()` already stored the settings, so the current revision is the one to name.
    start_revision = harness.store.settings("entry-a").revision
    assert start_revision >= 1
    release = asyncio.Event()
    entered = asyncio.Event()
    original = AutoPlannerController._reconcile

    async def reconcile_by_revision(instance, committed):
        if committed.revision == start_revision + 1:
            # A: committed, then held inside reconcile until B has moved the record on.
            entered.set()
            await release.wait()
            raise RuntimeError("calculation exploded")
        await original(instance, committed)

    monkeypatch.setattr(AutoPlannerController, "_reconcile", reconcile_by_revision)
    held = asyncio.create_task(
        controller.async_apply_settings(
            mutate=lambda settings: replace(settings, amps=12), expected_revision=start_revision
        )
    )
    await entered.wait()

    # B commits revision 2 for the same charger while A is still inside reconcile.
    committed_b = await controller.async_apply_settings(
        mutate=lambda settings: replace(settings, amps=32), expected_revision=start_revision + 1
    )
    assert committed_b.settings_revision == start_revision + 2  # a snapshot, not a record

    release.set()
    with pytest.raises(SettingsReconcileError) as failure:
        await held

    # A reports its own revision-1 record: not `not_committed`, and not B's revision-2 record.
    assert failure.value.settings.revision == start_revision + 1
    assert failure.value.settings.amps == 12
    assert harness.store.settings("entry-a").revision == start_revision + 2
    assert harness.store.settings("entry-a").amps == 32


@pytest.mark.asyncio
async def test_a_retry_at_the_current_revision_reconciles_for_real(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    controller = await harness.auto("entry-a")
    start_revision = harness.store.settings("entry-a").revision
    calls: list[int] = []
    original = AutoPlannerController._reconcile

    async def failing_once(instance, committed):
        calls.append(committed.revision)
        if len(calls) == 1:
            raise RuntimeError("first reconcile failed")
        await original(instance, committed)

    monkeypatch.setattr(AutoPlannerController, "_reconcile", failing_once)
    with pytest.raises(SettingsReconcileError) as failure:
        await controller.async_apply_settings(
            mutate=lambda settings: replace(settings, amps=16), expected_revision=start_revision
        )
    committed = failure.value.settings
    assert committed.amps == 16

    # The documented recovery: the same replacement at the revision the failure returned. This is an
    # ordinary CAS update, and the reconcile it performs is a real one -- not a permanently inert card.
    snapshot = await controller.async_apply_settings(
        mutate=lambda settings: replace(settings, amps=16), expected_revision=committed.revision
    )
    assert calls == [start_revision + 1, start_revision + 2]
    assert snapshot.settings_revision == start_revision + 2
    assert harness.store.settings("entry-a").amps == 16


def test_the_reconcile_failure_is_a_stable_home_assistant_error():
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.spotnav.planning.auto_controller import SETTINGS_RECONCILE_FAILED_CODE

    assert issubclass(SettingsReconcileError, HomeAssistantError)
    assert SettingsReconcileError.code == SETTINGS_RECONCILE_FAILED_CODE
