"""The canonical mutation path: one function, full replacement, compare-and-set.

These tests drive the real store (and its real lock) with the same `replacement_mutator` the two
transports use, so the concurrency, conflict and persistence behaviour they pin is the behaviour a
WebSocket command and the charger webhook get. No HA service, no relay and no controller are involved:
the authority transitions themselves are covered where the controller's own seams are tested.
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import (
    AutoSettings,
    AutoSettingsError,
    AutoSettingsStore,
)
from custom_components.spotnav.api.settings import (
    SETTINGS_KEYS,
    decode_settings,
    encode_settings,
    expected_revision_from,
    replacement_mutator,
)


class FlakyStore:
    """A persistence double whose next save can be made to fail, deterministically."""

    def __init__(self, payload=None) -> None:
        self.payload = payload
        self.saves = 0
        self.fail_next = False

    async def async_load(self):
        return self.payload

    async def async_save(self, data):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the disk said no")
        self.saves += 1
        self.payload = data


@pytest.fixture
def store(hass: HomeAssistant) -> AutoSettingsStore:
    return AutoSettingsStore(hass)


def replacement_for(settings: AutoSettings, **changes) -> AutoSettings:
    """A decoded full replacement, exactly as a transport would hand it over."""
    encoded = encode_settings(settings)
    # `revision` is named apart and `fiscal_included` is read-only: neither is a body key.
    body = {key: value for key, value in encoded.items() if key not in ("revision", "fiscal_included")}
    body.update(changes)
    assert set(body) == SETTINGS_KEYS
    return decode_settings(body)


async def test_a_first_read_is_defaults_at_revision_zero(store: AutoSettingsStore):
    await store.async_load()
    settings = store.settings("entry_a")
    assert settings.revision == 0
    assert settings.area_id is None and settings.phases is None and settings.amps is None
    assert set(encode_settings(settings)) == SETTINGS_KEYS | {"revision", "fiscal_included"}


async def test_a_full_replacement_increments_once_and_survives_a_reload(hass: HomeAssistant):
    store = AutoSettingsStore(hass)
    await store.async_load()
    replacement = replacement_for(store.settings("entry_a"), area_id="SE4", amps=16, phases=3)

    committed = await store.async_update(
        "entry_a",
        mutate=replacement_mutator(replacement),
        expected_revision=expected_revision_from(0),
    )
    assert committed.revision == 1
    # The phases in the body are accepted and ignored: nothing of it is stored.
    assert (committed.area_id, committed.amps, committed.phases) == (
        "SE4",
        16,
        None,
    )

    reopened = AutoSettingsStore(hass)
    await reopened.async_load()
    assert reopened.settings("entry_a") == committed


async def test_a_stale_revision_is_refused_and_writes_nothing(store: AutoSettingsStore):
    await store.async_load()
    replacement = replacement_for(store.settings("entry_a"), amps=10)
    await store.async_update("entry_a", mutate=replacement_mutator(replacement), expected_revision=0)
    before = store.settings("entry_a")

    with pytest.raises(AutoSettingsError) as refusal:
        await store.async_update(
            "entry_a",
            mutate=replacement_mutator(replacement_for(before, amps=32)),
            expected_revision=0,
        )
    assert refusal.value.code == "revision_conflict"
    assert store.settings("entry_a") == before
    assert store.settings("entry_a").revision == 1
    assert store.settings("entry_a").amps == 10


async def test_two_concurrent_edits_at_one_revision_leave_exactly_one_winner(store: AutoSettingsStore):
    await store.async_load()
    base = store.settings("entry_a")
    first = replacement_for(base, amps=10)
    second = replacement_for(base, amps=32)

    results = await asyncio.gather(
        store.async_update("entry_a", mutate=replacement_mutator(first), expected_revision=0),
        store.async_update("entry_a", mutate=replacement_mutator(second), expected_revision=0),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, AutoSettings)]
    conflicts = [item for item in results if isinstance(item, AutoSettingsError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "revision_conflict"
    # One increment, and the stored record is the winner's own replacement.
    assert successes[0].revision == 1
    assert store.settings("entry_a") == successes[0]
    assert store.settings("entry_a").amps in {10, 32}


async def test_two_chargers_stay_isolated_in_the_shared_document(store: AutoSettingsStore):
    await store.async_load()
    await store.async_update(
        "entry_a",
        mutate=replacement_mutator(replacement_for(store.settings("entry_a"), amps=10)),
        expected_revision=0,
    )
    await store.async_update(
        "entry_b",
        mutate=replacement_mutator(replacement_for(store.settings("entry_b"), amps=32)),
        expected_revision=0,
    )
    assert store.settings("entry_a").amps == 10
    assert store.settings("entry_b").amps == 32
    assert store.settings("entry_a").revision == 1
    assert store.settings("entry_b").revision == 1


async def test_a_failed_persistence_leaves_the_old_record_current(hass: HomeAssistant):
    flaky = FlakyStore()
    store = AutoSettingsStore(hass, store=flaky)
    await store.async_load()
    flaky.fail_next = True

    with pytest.raises(RuntimeError):
        await store.async_update(
            "entry_a",
            mutate=replacement_mutator(replacement_for(store.settings("entry_a"), amps=32)),
            expected_revision=0,
        )
    # Nothing committed: revision 0, defaults, and the next valid edit still starts from there.
    assert store.settings("entry_a").revision == 0
    assert store.settings("entry_a").amps is None
    committed = await store.async_update(
        "entry_a",
        mutate=replacement_mutator(replacement_for(store.settings("entry_a"), amps=10)),
        expected_revision=0,
    )
    assert committed.revision == 1 and committed.amps == 10
