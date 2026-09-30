"""Tests for the integration-wide dismissed-candidate store.

Nothing here needs a config entry: the store is deliberately *not*
entry-scoped — one storage file for the whole integration — which is exactly
what several of these tests pin. The real
`homeassistant.helpers.storage.Store` is exercised against the test harness's
storage, the same way `tests/test_controller.py` exercises
`ChargingController`'s own store.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DiscoveryDecisionStore,
    async_setup_decisions,
)
from custom_components.spotnav.runtime import domain_data


async def test_dismiss_then_is_dismissed_then_undismiss(hass: HomeAssistant) -> None:
    """The whole round trip: clear, dismissed, clear again."""
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is False

    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True

    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, "device-a")
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is False


async def test_dismissals_are_scoped_by_domain_and_by_id(hass: HomeAssistant) -> None:
    """Dismissing one id, or one decision domain, never touches another."""
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-b")
    await store.async_dismiss("site_current", "device-a")

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-b") is True
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-c") is False
    # Same id, different decision domain: an entirely separate decision.
    assert store.is_dismissed("site_current", "device-a") is True
    assert store.is_dismissed("site_current", "device-b") is False

    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, "device-a")

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is False
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-b") is True
    assert store.is_dismissed("site_current", "device-a") is True


async def test_repeated_dismiss_and_undismiss_are_harmless_no_ops(hass: HomeAssistant) -> None:
    """Dismissing twice, or undismissing something already clear, neither
    raises nor leaves a duplicate behind in the persisted data.
    """
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")

    assert await store._store.async_load() == {
        "dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]},
        "confirmed": {},
    }

    # Undismissing something that was never dismissed, and twice over, is
    # equally harmless.
    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, "device-b")
    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, "device-a")
    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, "device-a")

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is False
    assert await store._store.async_load() == {"dismissed": {}, "confirmed": {}}


async def test_a_reloaded_store_remembers_what_was_dismissed(hass: HomeAssistant) -> None:
    """A fresh instance over the same storage (an app/HA restart) still knows.

    This is the whole point of storing the decision at all: a dismissal has to
    outlive the process that made it.
    """
    first = DiscoveryDecisionStore(hass)
    await first.async_load()
    await first.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")

    after_restart = DiscoveryDecisionStore(hass)
    await after_restart.async_load()

    assert after_restart.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True
    assert after_restart.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-b") is False


async def test_the_store_key_is_not_entry_scoped(hass: HomeAssistant) -> None:
    """One file for the whole integration, never one per config entry.

    Every config entry shares the same discovery decisions: a vehicle
    dismissed while configuring one charger must stay dismissed for all of
    them, and for entries that do not exist yet.
    """
    store = DiscoveryDecisionStore(hass)

    assert store._store.key == f"{DOMAIN}_discovery_decisions"
    assert not store._store.key.startswith(f"{DOMAIN}.")
    assert store._store.version == 1


async def test_malformed_stored_data_is_ignored_rather_than_crashing(hass: HomeAssistant) -> None:
    """Anything unreadable on disk reads as "nothing dismissed".

    Losing a dismissal is a much smaller problem than refusing to load at all
    or inventing a dismissal out of junk.
    """
    for junk in (
        "not-even-a-dict",
        {},
        {"dismissed": "not-a-dict"},
        {"dismissed": {DECISION_DOMAIN_VEHICLE: "not-a-list"}},
        {"dismissed": {DECISION_DOMAIN_VEHICLE: [None, 5, ""]}},
        {"unknown_future_key": {"whatever": True}},
    ):
        await Store(hass, 1, f"{DOMAIN}_discovery_decisions").async_save(junk)
        store = DiscoveryDecisionStore(hass)
        await store.async_load()

        assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is False

    # A partially unreadable list keeps the ids that are readable, and an
    # unknown future key alongside it is simply carried past untouched.
    await Store(hass, 1, f"{DOMAIN}_discovery_decisions").async_save(
        {"dismissed": {DECISION_DOMAIN_VEHICLE: [None, 5, "", "device-a"]}, "future": 1}
    )
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "") is False

    # And a save after that rewrites the map cleanly -- sorted, duplicate-free,
    # with the unusable entries gone -- while carrying the unrecognized
    # top-level key along untouched. (Dismissing "device-a" again would be a
    # no-op and write nothing at all, so this adds a second id.)
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-b")
    assert await store._store.async_load() == {
        "dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a", "device-b"]},
        "confirmed": {},
        "future": 1,
    }


async def test_reading_before_loading_is_simply_nothing_dismissed(hass: HomeAssistant) -> None:
    """`is_dismissed` is synchronous and safe before `async_load` has run.

    `discover_vehicles` is synchronous by design, so it can never await the
    store; before setup loads it, the honest answer is "nothing is dismissed".
    """
    store = DiscoveryDecisionStore(hass)

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is False


async def test_setup_publishes_one_shared_store_and_is_idempotent(hass: HomeAssistant) -> None:
    """`async_setup_decisions` creates, loads and publishes the one store.

    Called twice (the shape of a real HA startup that sets up the integration
    and then its entries), it must not create a second store or lose what is
    already dismissed.
    """
    assert domain_data(hass).decision_store is None

    store = await async_setup_decisions(hass)
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")

    again = await async_setup_decisions(hass)

    assert again is store
    assert domain_data(hass).decision_store is store
    assert domain_data(hass).decision_store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True


# --- Confirmed decisions (a decision with a payload, not just a flag) -----


async def test_confirm_then_confirmed_payload_then_unconfirm(hass: HomeAssistant) -> None:
    """The whole round trip for a decision that carries a payload."""
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") is None

    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE, "device-a", {"soc_entity_id": "sensor.example_ev_battery_level"}
    )
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") == {
        "soc_entity_id": "sensor.example_ev_battery_level"
    }
    # Scoped, exactly like a dismissal: another id, or another decision
    # domain, is a different decision.
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-b") is None
    assert store.confirmed_payload("site_current", "device-a") is None

    await store.async_unconfirm(DECISION_DOMAIN_VEHICLE, "device-a")
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") is None


async def test_confirming_again_replaces_the_payload(hass: HomeAssistant) -> None:
    """A second confirmation is the human changing their mind, not a tally."""
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    await store.async_confirm(DECISION_DOMAIN_VEHICLE, "device-a", {"soc_entity_id": "sensor.first"})
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, "device-a", {"soc_entity_id": "sensor.second"})

    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") == {
        "soc_entity_id": "sensor.second"
    }
    assert await store._store.async_load() == {
        "dismissed": {},
        "confirmed": {DECISION_DOMAIN_VEHICLE: {"device-a": {"soc_entity_id": "sensor.second"}}},
    }


async def test_confirm_and_dismiss_are_independent_decisions(hass: HomeAssistant) -> None:
    """Neither one silently clears the other.

    "Do not report this device" and "if it ever is a candidate, its state of
    charge comes from *this* entity" answer different questions, and are
    usually decided at different times. Coupling them would make one an
    implicit reversal of the other: confirming would quietly un-dismiss, and
    dismissing would silently throw away which entity the human picked. Kept
    independent, undismissing a device later uses the confirmation that is
    already there.
    """
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    await store.async_confirm(DECISION_DOMAIN_VEHICLE, "device-a", {"soc_entity_id": "sensor.chosen"})
    await store.async_dismiss(DECISION_DOMAIN_VEHICLE, "device-a")

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") == {
        "soc_entity_id": "sensor.chosen"
    }

    await store.async_undismiss(DECISION_DOMAIN_VEHICLE, "device-a")
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") == {
        "soc_entity_id": "sensor.chosen"
    }

    await store.async_unconfirm(DECISION_DOMAIN_VEHICLE, "device-a")
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is False
    assert await store._store.async_load() == {"dismissed": {}, "confirmed": {}}


async def test_a_confirmed_payload_survives_a_reload(hass: HomeAssistant) -> None:
    """Like a dismissal, a confirmation has to outlive the process."""
    first = DiscoveryDecisionStore(hass)
    await first.async_load()
    await first.async_confirm(DECISION_DOMAIN_VEHICLE, "device-a", {"soc_entity_id": "sensor.kept"})

    after_restart = DiscoveryDecisionStore(hass)
    await after_restart.async_load()

    assert after_restart.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") == {
        "soc_entity_id": "sensor.kept"
    }


async def test_an_old_shape_file_gains_confirmed_without_a_version_bump(
    hass: HomeAssistant,
) -> None:
    """A file written before `confirmed` existed loads, and grows the new key.

    `_async_save` copies unrecognized top-level keys through, so adding a decision kind needs no
    storage-version bump or rewrite.
    """
    await Store(hass, 1, f"{DOMAIN}_discovery_decisions").async_save(
        {"dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]}}
    )

    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    assert store._store.version == 1
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-b") is None

    await store.async_confirm(DECISION_DOMAIN_VEHICLE, "device-b", {"soc_entity_id": "sensor.b"})

    assert await store._store.async_load() == {
        "dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]},
        "confirmed": {DECISION_DOMAIN_VEHICLE: {"device-b": {"soc_entity_id": "sensor.b"}}},
    }


async def test_repeated_unconfirm_and_malformed_payloads(hass: HomeAssistant) -> None:
    """No-op unconfirms, and junk that never becomes a confirmation.

    A payload is a caller's data, so a malformed one is rejected loudly rather
    than stored; an *empty* one confirms nothing at all (a no-op), and a blank
    id has nothing to attach a decision to.
    """
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    await store.async_unconfirm(DECISION_DOMAIN_VEHICLE, "device-a")
    await store.async_unconfirm(DECISION_DOMAIN_VEHICLE, "device-a")
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, "device-a", {})
    await store.async_confirm(DECISION_DOMAIN_VEHICLE, "", {"soc_entity_id": "sensor.a"})

    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") is None
    assert await store._store.async_load() is None

    with pytest.raises(ValueError):
        await store.async_confirm(DECISION_DOMAIN_VEHICLE, "device-a", ["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await store.async_confirm(DECISION_DOMAIN_VEHICLE, "device-a", {1: "not-a-string-key"})

    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-a") is None


async def test_malformed_confirmed_data_is_ignored_rather_than_crashing(hass: HomeAssistant) -> None:
    """Anything unreadable in `confirmed` reads as "nothing confirmed" -- and
    the dismissals in the same file are still honoured.
    """
    # Each shape, and whether that shape still contains the dismissal.
    for junk, dismissal_readable in (
        ("not-a-dict", False),
        ({}, False),
        ({"dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]}, "confirmed": "not-a-dict"}, True),
        (
            {
                "dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]},
                "confirmed": {DECISION_DOMAIN_VEHICLE: "not-a-dict"},
            },
            True,
        ),
        (
            {
                "dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]},
                "confirmed": {DECISION_DOMAIN_VEHICLE: {"device-b": "not-a-dict"}},
            },
            True,
        ),
        (
            {
                "dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]},
                "confirmed": {DECISION_DOMAIN_VEHICLE: {"device-b": {}}},
            },
            True,
        ),
    ):
        await Store(hass, 1, f"{DOMAIN}_discovery_decisions").async_save(junk)
        store = DiscoveryDecisionStore(hass)
        await store.async_load()

        assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-b") is None
        assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is dismissal_readable

    # A usable confirmation in the same file is still read, next to a
    # dismissal -- and an unusable one beside it does not spoil it.
    await Store(hass, 1, f"{DOMAIN}_discovery_decisions").async_save(
        {
            "dismissed": {DECISION_DOMAIN_VEHICLE: ["device-a"]},
            "confirmed": {
                DECISION_DOMAIN_VEHICLE: {
                    "device-b": {"soc_entity_id": "sensor.b"},
                    "device-c": 5,
                }
            },
        }
    )
    store = DiscoveryDecisionStore(hass)
    await store.async_load()

    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-b") == {
        "soc_entity_id": "sensor.b"
    }
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, "device-c") is None
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, "device-a") is True
