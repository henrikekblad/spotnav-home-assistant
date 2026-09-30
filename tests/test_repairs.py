"""Outstanding decisions as Home Assistant Repairs issues (`repairs.py`, kept in step with `resolution.resolve_required`).

Covers the issue per kind and its text, deletion when a decision is made or dismissed (via a service, where the
sync is wired in), idempotency, and the fix flow: candidates, recording, dismissal option and abort when the decision is gone.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav import repairs
from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.vehicles.choices import DISMISS_VEHICLE_CHOICE
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
)
from custom_components.spotnav.repairs import (
    _RESYNC_DEBOUNCE_S,
    async_arm_resolution_sync,
    async_sync_resolution_repairs,
    issue_id_for,
)

from .helpers import add_ambiguous_vehicle_device
from .world import one_charger, vehicle_device
from custom_components.spotnav.runtime import domain_data


@pytest.fixture(autouse=True)
def _isolated_issue_registry(issue_registry):
    """Give every test its own issue registry, so an issue created in one test is never visible in the next."""
    return issue_registry


def _issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    """This integration's issues by issue id."""
    return {
        issue_id: entry
        for (domain, issue_id), entry in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


# The shape `vehicle_device` gives a charge-limit number; a state update must carry the same attributes to stay limit-shaped.
_LIMIT_ATTRIBUTES = {"unit_of_measurement": "%", "min": 50, "max": 100}


def _set_limit_value(hass: HomeAssistant, entity_id: str, percent: str) -> None:
    """Give a charge-limit number a live value, keeping the shape discovery reads."""
    hass.states.async_set(entity_id, percent, dict(_LIMIT_ATTRIBUTES))


async def _advance_past_the_debounce(hass: HomeAssistant) -> None:
    """Let a scheduled sync run with one jump past the whole delay; the test is that a sync runs, not how often the timer rescheduled."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=_RESYNC_DEBOUNCE_S + 1))
    await hass.async_block_till_done()


async def test_an_ambiguous_state_of_charge_becomes_one_issue(
    hass: HomeAssistant,
) -> None:
    """One issue whose id says which decision and whose text names the device only.

    Candidate entity ids are deliberately not part of the issue: the picker showing them is one click away.
    """
    device_id, soc_entity, health_entity = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )

    await async_sync_resolution_repairs(hass)

    issues = _issues(hass)
    assert list(issues) == [issue_id_for("soc", device_id)]
    issue = issues[issue_id_for("soc", device_id)]
    assert issue.translation_key == "vehicle_soc_needs_decision"
    assert issue.translation_placeholders == {"device": "Ambiguous car"}
    assert issue.is_fixable is True
    assert issue.severity == ir.IssueSeverity.WARNING
    # Neither candidate is anywhere a person reads, and the id carries the device.
    assert soc_entity not in str(issue.translation_placeholders)
    assert health_entity not in str(issue.translation_placeholders)
    assert device_id in issue.issue_id


async def test_an_ambiguous_charge_limit_becomes_its_own_issue(
    hass: HomeAssistant,
) -> None:
    """The other kind has its own issue and id; two devices, two decisions, no collision."""
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    soc_device, _soc_entity, _health_entity = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )

    await async_sync_resolution_repairs(hass)

    issues = _issues(hass)
    assert sorted(issues) == sorted(
        [issue_id_for("charge_limit", limit_device["device_id"]), issue_id_for("soc", soc_device)]
    )
    limit_issue = issues[issue_id_for("charge_limit", limit_device["device_id"])]
    assert limit_issue.translation_key == "vehicle_charge_limit_needs_decision"
    assert limit_issue.translation_placeholders == {"device": "Two-limit car"}


async def test_resolving_through_a_service_deletes_that_issue_and_no_other(
    hass: HomeAssistant,
) -> None:
    """The sync is wired into every mutation (no manual sync here).

    Resolving the state-of-charge decision with `confirm_vehicle_soc` removes its issue; the
    charge-limit issue for another device stays.
    """
    await one_charger(hass)
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    soc_device, soc_entity, _health = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )
    await async_sync_resolution_repairs(hass)
    assert len(_issues(hass)) == 2

    await hass.services.async_call(
        DOMAIN,
        "confirm_vehicle_soc",
        {"device_id": soc_device, "entity_id": soc_entity},
        blocking=True,
    )

    assert list(_issues(hass)) == [issue_id_for("charge_limit", limit_device["device_id"])]


async def test_a_dismissed_device_has_no_issue_and_dismissal_removes_one(
    hass: HomeAssistant,
) -> None:
    """A dismissal answers the state-of-charge decision: a device dismissed before the sync gets no issue, and dismissing one with an issue removes it."""
    await one_charger(hass)
    already_dismissed, _soc, _health = add_ambiguous_vehicle_device(
        hass, unique_id="dismissed_car", name="Not a car"
    )
    live_device, _live_soc, _live_health = add_ambiguous_vehicle_device(
        hass, unique_id="live_car", name="Live car"
    )
    await domain_data(hass).decision_store.async_dismiss(DECISION_DOMAIN_VEHICLE, already_dismissed)

    await async_sync_resolution_repairs(hass)

    assert list(_issues(hass)) == [issue_id_for("soc", live_device)]

    await hass.services.async_call(
        DOMAIN, "dismiss_vehicle", {"device_id": live_device}, blocking=True
    )

    assert _issues(hass) == {}


async def test_the_sync_is_idempotent(hass: HomeAssistant) -> None:
    """Running the sync twice creates nothing twice and deletes nothing it should keep; whole entries compare equal, so nothing flickers in the UI."""
    add_ambiguous_vehicle_device(hass, unique_id="car_soc", name="Ambiguous car")
    vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )

    await async_sync_resolution_repairs(hass)
    first = _issues(hass)
    await async_sync_resolution_repairs(hass)

    assert len(first) == 2
    assert _issues(hass) == first


async def test_the_fix_flow_offers_the_live_candidates_and_records_the_choice(
    hass: HomeAssistant,
) -> None:
    """The dropdown is what discovery found and the pick is stored.

    Driven through the repair flow manager, so it exercises the platform lookup, `async_create_fix_flow`,
    the form and the recording as a real *Fix* press does.
    """
    await one_charger(hass)
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    await async_sync_resolution_repairs(hass)
    issue_id = issue_id_for("charge_limit", limit_device["device_id"])
    manager = repairs_flow_manager(hass)
    assert manager is not None

    form = await manager.async_init(DOMAIN, data={"issue_id": issue_id})

    assert form["type"] == "form"
    assert form["step_id"] == "pick"
    field = next(iter(form["data_schema"].schema))
    assert field.schema == "choice"
    options = form["data_schema"].schema[field].config["options"]
    assert [option["value"] for option in options] == sorted(limit_device["limits"])

    result = await manager.async_configure(
        form["flow_id"], {"choice": limit_device["limits"][0]}
    )

    assert result["type"] == "create_entry"
    assert domain_data(hass).decision_store.confirmed_payload(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, limit_device["device_id"]
    ) == {"charge_limit_entity_id": limit_device["limits"][0]}
    # The issue went with it: the flow synced.
    assert _issues(hass) == {}


async def test_the_fix_flow_aborts_when_the_decision_is_already_gone(
    hass: HomeAssistant,
) -> None:
    """A stale dropdown is refused honestly: the device is resolved behind the issue's back and the flow says nothing is left to decide."""
    await one_charger(hass)
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    await async_sync_resolution_repairs(hass)
    issue_id = issue_id_for("charge_limit", limit_device["device_id"])
    await domain_data(hass).decision_store.async_confirm(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
        limit_device["device_id"],
        {"charge_limit_entity_id": limit_device["limits"][0]},
    )
    assert issue_id in _issues(hass)

    result = await repairs_flow_manager(hass).async_init(
        DOMAIN, data={"issue_id": issue_id}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "no_longer_needs_decision"


async def test_the_state_of_charge_flow_offers_the_dismissal_and_dismisses(
    hass: HomeAssistant,
) -> None:
    """The same two answers as the config flow's step, in one dropdown.

    Choosing the dismissal must record a dismissal, not a confirmation, and its value is the sentinel that step uses.
    """
    await one_charger(hass)
    device_id, soc_entity, health_entity = add_ambiguous_vehicle_device(
        hass, unique_id="car_soc", name="Ambiguous car"
    )
    await async_sync_resolution_repairs(hass)
    manager = repairs_flow_manager(hass)
    assert manager is not None
    form = await manager.async_init(
        DOMAIN, data={"issue_id": issue_id_for("soc", device_id)}
    )
    field = next(iter(form["data_schema"].schema))
    options = form["data_schema"].schema[field].config["options"]
    # Both candidates, and the dismissal, in that order.
    assert [option["value"] for option in options] == [
        *sorted([soc_entity, health_entity]),
        DISMISS_VEHICLE_CHOICE,
    ]

    result = await manager.async_configure(
        form["flow_id"], {"choice": DISMISS_VEHICLE_CHOICE}
    )

    assert result["type"] == "create_entry"
    store = domain_data(hass).decision_store
    # Dismissed, and nothing confirmed: different decisions.
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, device_id) is True
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id) is None
    assert _issues(hass) == {}


async def test_a_relevant_state_change_re_syncs_the_issues(
    hass: HomeAssistant,
) -> None:
    """An ambiguity that appears after a sync still becomes an issue.

    A car integration may populate its charge-limit numbers seconds after startup: both are `unavailable` at
    first (no issue); when they come live, two state changes and one debounced sync create it.
    """
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "unavailable"), ("limit_b", "unavailable")),
    )
    cancel = await async_arm_resolution_sync(hass)
    try:
        await async_sync_resolution_repairs(hass)
        assert _issues(hass) == {}

        _set_limit_value(hass, limit_device["limits"][0], "80")
        _set_limit_value(hass, limit_device["limits"][1], "100")
        await _advance_past_the_debounce(hass)

        assert list(_issues(hass)) == [
            issue_id_for("charge_limit", limit_device["device_id"])
        ]
    finally:
        cancel()


async def test_an_irrelevant_state_change_does_not_sync(
    hass: HomeAssistant,
) -> None:
    """Only the domains discovery reads trigger a sync; a switch does not, and a relevant change right after shows the silence was the filter."""
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "unavailable"), ("limit_b", "unavailable")),
    )
    cancel = await async_arm_resolution_sync(hass)
    try:
        await async_sync_resolution_repairs(hass)
        assert _issues(hass) == {}

        hass.states.async_set("switch.charger_a", "on")
        await _advance_past_the_debounce(hass)
        assert _issues(hass) == {}

        _set_limit_value(hass, limit_device["limits"][0], "80")
        _set_limit_value(hass, limit_device["limits"][1], "100")
        await _advance_past_the_debounce(hass)

        assert list(_issues(hass)) == [
            issue_id_for("charge_limit", limit_device["device_id"])
        ]
    finally:
        cancel()


async def test_a_registry_change_re_syncs_the_issues(
    hass: HomeAssistant,
) -> None:
    """A registry change alters what discovery sees with no state event: removing the device from the registry removes its issue."""
    limit_device = vehicle_device(
        hass,
        unique_id="car_limit",
        name="Two-limit car",
        limits=(("limit_a", "80"), ("limit_b", "100")),
    )
    cancel = await async_arm_resolution_sync(hass)
    try:
        await async_sync_resolution_repairs(hass)
        assert list(_issues(hass)) == [
            issue_id_for("charge_limit", limit_device["device_id"])
        ]

        dr.async_get(hass).async_remove_device(limit_device["device_id"])
        await _advance_past_the_debounce(hass)

        assert _issues(hass) == {}
    finally:
        cancel()


async def test_a_burst_of_relevant_changes_is_one_sync(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The domain filter and the debounce are checked by counting syncs on the single function the trigger calls.

    A switch change is dropped by the filter; a burst of four `sensor`/`number` changes runs one sync, not four.
    """
    syncs: list[str] = []

    async def spy(_hass: HomeAssistant) -> None:
        syncs.append("synced")

    monkeypatch.setattr(repairs, "async_sync_resolution_repairs", spy)
    cancel = await async_arm_resolution_sync(hass)
    try:
        hass.states.async_set("switch.charger_a", "on")
        await _advance_past_the_debounce(hass)
        assert syncs == []

        for entity_id in ("sensor.a", "sensor.b", "number.c", "number.d"):
            hass.states.async_set(entity_id, "1")
        await _advance_past_the_debounce(hass)

        assert len(syncs) == 1
    finally:
        cancel()
