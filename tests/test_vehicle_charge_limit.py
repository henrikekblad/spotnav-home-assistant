"""The `set_charge_limit` action: writing a vehicle's charge limit through the vehicle's own integration.

It is a `number.set_value` on the entity detection found, never anything that wakes the car,
so `ok` means "the write happened", not "the car is at that limit". The target entity is
resolved, not guessed, and every refusal is one message with no device id in it.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.spotnav.const import (
    DOMAIN,
)
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
    async_setup_decisions,
)
from custom_components.spotnav.vehicles.vehicle_charge_limit import (
    CHARGE_LIMIT_MIN_INTERVAL_S,
    INVALID_PERCENT,
    VehicleChargeLimitLimiter,
    async_set_charge_limit,
    limiter_for,
)
from custom_components.spotnav.vehicles.vehicle_discovery import (
    vehicle_charge_limit_entity_id,
)
from custom_components.spotnav.vehicles.vehicle_refresh import UNKNOWN_VEHICLE

from .helpers import webhook_dashboard
from .world import one_charger, vehicle_device
from custom_components.spotnav.runtime import domain_data


def _test_config_entry(hass: HomeAssistant) -> str:
    """One throwaway config entry so test-owned devices can be registered."""
    entry_id = "charge_limit_devices"
    if hass.config_entries.async_get_entry(entry_id) is None:
        MockConfigEntry(domain="test", entry_id=entry_id).add_to_hass(hass)
    return entry_id


def _entity(
    hass: HomeAssistant,
    *,
    device_id: str,
    domain: str,
    object_id: str,
    state: str,
    attributes: dict | None = None,
) -> str:
    entry = er.async_get(hass).async_get_or_create(
        domain,
        "test",
        f"{device_id}_{object_id}",
        device_id=device_id,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entry.entity_id, state, attributes or {})
    return entry.entity_id


def _gadget(hass: HomeAssistant, *, unique_id: str, name: str) -> str:
    """A battery reading with no range signal: not a vehicle, so nothing may be written to it."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=_test_config_entry(hass),
        identifiers={("test", unique_id)},
        name=name,
    )
    _entity(
        hass,
        device_id=device.id,
        domain="sensor",
        object_id="battery_level",
        state="88",
        attributes={"device_class": "battery", "unit_of_measurement": "%"},
    )
    return device.id


async def _post(client, webhook_id: str, payload: dict[str, Any]):
    return await client.post(f"/api/webhook/{webhook_id}", json=payload)


async def _dashboard(client, webhook_id: str) -> dict:
    return await webhook_dashboard(client, webhook_id)


async def _set(client, webhook_id: str, vehicle_id: object, percent: object):
    return await _post(
        client,
        webhook_id,
        {
            "version": 1,
            "action": "set_charge_limit",
            "vehicle_id": vehicle_id,
            "percent": percent,
        },
    )


def test_a_detected_vehicle_resolves_to_its_charge_limit_entity(
    hass: HomeAssistant,
) -> None:
    """The entity the resolver names is the entity written: one percent `number` with a range topping out at 100."""
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")

    assert vehicle_charge_limit_entity_id(hass, vehicle["device_id"]) == vehicle["limits"][0]


def test_two_candidates_resolve_to_nothing_until_a_human_chooses(
    hass: HomeAssistant,
) -> None:
    """Two limits are a choice only a human can make, so nothing is chosen.

    A highest-value rule would silently write the wrong limit (AC vs DC, say). Until
    `confirm_charge_limit` records the choice, the answer is the single "no vehicle" refusal.
    """
    for unique_id, limits in (
        ("high_second", (("first_limit", "60"), ("second_limit", "95"))),
        ("high_first", (("first_limit", "70"), ("second_limit", "65"))),
        ("equal", (("first_limit", "80"), ("second_limit", "80"))),
    ):
        vehicle = vehicle_device(
            hass, unique_id=unique_id, name=unique_id, limits=limits
        )

        assert vehicle_charge_limit_entity_id(hass, vehicle["device_id"]) is None, unique_id


def test_a_limit_that_is_not_reporting_a_value_is_not_a_second_candidate(
    hass: HomeAssistant,
) -> None:
    """A candidate is a live charge-limit reading, so an asleep one (`0` means "I do not know") is no choice.

    That leaves a car with two limit entities exactly one limit to resolve.
    """
    vehicle = vehicle_device(
        hass,
        unique_id="one_live",
        name="One live",
        limits=(("first_limit", "80"), ("second_limit", "0")),
    )

    assert vehicle_charge_limit_entity_id(hass, vehicle["device_id"]) == vehicle["limits"][0]


def test_a_vehicle_with_no_live_limit_value_has_nothing_to_write_to(
    hass: HomeAssistant,
) -> None:
    """A limit not reporting a value (`unavailable`, or zero) is not one to write to; no limit-shaped number at all gives the same answer."""
    asleep = vehicle_device(
        hass, unique_id="asleep", name="Asleep", limits=(("charge_limit", "unavailable"),)
    )
    zero = vehicle_device(hass, unique_id="zero", name="Zero", limits=(("charge_limit", "0"),))
    none_at_all = vehicle_device(hass, unique_id="no_limit", name="No limit", limits=())

    assert vehicle_charge_limit_entity_id(hass, asleep["device_id"]) is None
    assert vehicle_charge_limit_entity_id(hass, zero["device_id"]) is None
    assert vehicle_charge_limit_entity_id(hass, none_at_all["device_id"]) is None


async def test_every_refusal_is_the_one_answer_none(
    hass: HomeAssistant,
) -> None:
    """Every refusal is one result (`None`), so the action cannot ask what exists: unknown id, non-vehicle, dismissed vehicle, non-string id."""
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    gadget = _gadget(hass, unique_id="door", name="Back door")
    await async_setup_decisions(hass)
    await domain_data(hass).decision_store.async_dismiss(DECISION_DOMAIN_VEHICLE, vehicle["device_id"])

    assert vehicle_charge_limit_entity_id(hass, "device-that-does-not-exist") is None
    assert vehicle_charge_limit_entity_id(hass, gadget) is None
    assert vehicle_charge_limit_entity_id(hass, vehicle["device_id"]) is None
    assert vehicle_charge_limit_entity_id(hass, None) is None
    assert vehicle_charge_limit_entity_id(hass, 42) is None
    assert vehicle_charge_limit_entity_id(hass, "") is None


def test_the_limiter_refuses_under_the_interval_and_allows_after() -> None:
    """The interval and what is left of it, with an injected clock and no sleeping."""
    now = [1000.0]
    limiter = VehicleChargeLimitLimiter(clock=lambda: now[0])

    # Never written: nothing to wait for.
    assert limiter.retry_after_s("vehicle") is None
    limiter.note_write("vehicle")
    assert limiter.retry_after_s("vehicle") == CHARGE_LIMIT_MIN_INTERVAL_S
    now[0] += 20.0
    assert limiter.retry_after_s("vehicle") == CHARGE_LIMIT_MIN_INTERVAL_S - 20.0
    now[0] += CHARGE_LIMIT_MIN_INTERVAL_S
    assert limiter.retry_after_s("vehicle") is None


async def test_the_interval_is_counted_from_the_last_successful_write(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Only successful writes move the rate-limit window; a refused request is not an attempt."""
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")
    now = [1000.0]
    domain_data(hass).charge_limit_limiter = VehicleChargeLimitLimiter(clock=lambda: now[0])
    client = await hass_client_no_auth()
    remaining = CHARGE_LIMIT_MIN_INTERVAL_S - 59.0

    assert (await _set(client, "webhook-a", vehicle["device_id"], 70)).status == 200
    assert len(calls) == 1
    now[0] += 59.0
    # Refused on the value (over 100 %, under the entity's minimum) and on the interval: three refusals, and the window stays where the successful write left it.
    assert (await _set(client, "webhook-a", vehicle["device_id"], 101)).status == 400
    assert (await _set(client, "webhook-a", vehicle["device_id"], 40)).status == 400
    assert (await _set(client, "webhook-a", vehicle["device_id"], 70)).status == 429
    assert limiter_for(hass).retry_after_s(vehicle["device_id"]) == remaining
    assert len(calls) == 1
    now[0] += 1.0
    assert (await _set(client, "webhook-a", vehicle["device_id"], 70)).status == 200
    assert len(calls) == 2


async def test_the_interval_is_per_vehicle_not_for_the_whole_instance(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    first = vehicle_device(hass, unique_id="ev6", name="EV6")
    second = vehicle_device(hass, unique_id="zoe", name="Zoe")
    await one_charger(hass)
    async_mock_service(hass, "number", "set_value")
    client = await hass_client_no_auth()

    assert (await _set(client, "webhook-a", first["device_id"], 70)).status == 200
    # Two cars are two integrations: writing one is no reason to refuse the other.
    assert (await _set(client, "webhook-a", second["device_id"], 70)).status == 200
    assert (await _set(client, "webhook-a", first["device_id"], 70)).status == 429


async def test_a_write_reaches_exactly_the_resolved_entity_once(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """One blocking call on the resolved entity with the exact value asked for, so `ok` means the write has happened.

    The answer carries only the shared shape, with no entity id or value echo.
    """
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")

    response = await _set(await hass_client_no_auth(), "webhook-a", vehicle["device_id"], 80)

    assert response.status == 200
    assert await response.json() == {"ok": True, "action": "set_charge_limit"}
    assert len(calls) == 1
    assert calls[0].data["value"] == 80.0
    assert calls[0].data["entity_id"] == vehicle["limits"][0]


async def test_a_percent_a_limit_cannot_be_set_to_is_a_400(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """One refusal and no call for every bad value: `0`, `101`, a bool, non-numeric text, or a missing value.

    Requests this integration can see will be refused are answered here so the vehicle's integration is not spent on them.
    """
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")
    client = await hass_client_no_auth()

    for bad in (0, 101, True, "eighty", None):
        response = await _set(client, "webhook-a", vehicle["device_id"], bad)
        assert response.status == 400, bad
        assert await response.json() == {"ok": False, "error": INVALID_PERCENT}, bad

    # NaN and infinity are not representable in JSON, so the parser refuses such a body: still a 400 and no call, with a message about the body.
    for not_a_number in (float("nan"), float("inf"), float("-inf")):
        response = await _set(client, "webhook-a", vehicle["device_id"], not_a_number)
        assert response.status == 400, not_a_number

    # Where a value does arrive (direct call or lenient reader), a non-finite number is not a value a limit can hold.
    for not_a_number in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match=INVALID_PERCENT):
            await async_set_charge_limit(hass, vehicle["device_id"], not_a_number)

    assert calls == []


async def test_a_percent_outside_the_entitys_own_range_is_a_400(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The entity's own `min`/`max` bound the request: with a 50-100 range, 49 is refused and 50 is written."""
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")
    client = await hass_client_no_auth()

    below = await _set(client, "webhook-a", vehicle["device_id"], 49)
    assert below.status == 400
    assert await below.json() == {"ok": False, "error": INVALID_PERCENT}
    assert calls == []

    assert (await _set(client, "webhook-a", vehicle["device_id"], 50)).status == 200
    assert calls[0].data["value"] == 50.0


async def test_an_unknown_vehicle_is_refused_the_same_single_way(
    hass: HomeAssistant, hass_client_no_auth, caplog
) -> None:
    """One message, no call, and nothing about the attempt in the log.

    Same message and constant as the refresh action's refusal, naming no device. `caplog`
    is checked because the claim is about what a log can leak.
    """
    vehicle_device(hass, unique_id="ev6", name="EV6")
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")
    client = await hass_client_no_auth()

    with caplog.at_level(logging.DEBUG):
        response = await _set(client, "webhook-a", "device-that-does-not-exist", 80)

    assert response.status == 400
    assert await response.json() == {"ok": False, "error": UNKNOWN_VEHICLE}
    assert calls == []
    ours = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.spotnav")
    ]
    assert all("device-that-does-not-exist" not in message for message in ours)


async def test_a_second_write_within_the_interval_is_a_429_with_when_to_retry(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")
    client = await hass_client_no_auth()

    first = await _set(client, "webhook-a", vehicle["device_id"], 80)
    second = await _set(client, "webhook-a", vehicle["device_id"], 90)

    assert first.status == 200
    # 429, not the 400 of a malformed or unknown request: the request was fine and the vehicle exists but cannot be written yet.
    assert second.status == 429
    body = await second.json()
    assert body["ok"] is False
    # How long to wait, in the body and the standard header, so the control reads as limited rather than broken.
    assert body["retry_after_s"] == 60
    assert second.headers["Retry-After"] == "60"
    # The refused request cost the vehicle's integration nothing.
    assert len(calls) == 1


async def test_two_vehicles_are_written_independently(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Writing A writes A's entity and nothing of B's."""
    first = vehicle_device(hass, unique_id="ev6", name="EV6")
    second = vehicle_device(hass, unique_id="zoe", name="Zoe")
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")
    client = await hass_client_no_auth()

    assert (await _set(client, "webhook-a", first["device_id"], 71)).status == 200
    assert (await _set(client, "webhook-a", second["device_id"], 62)).status == 200

    assert [call.data["entity_id"] for call in calls] == [
        first["limits"][0],
        second["limits"][0],
    ]
    assert [call.data["value"] for call in calls] == [71.0, 62.0]


async def test_only_the_numbers_own_setter_is_ever_called(
    hass: HomeAssistant, hass_client_no_auth, monkeypatch
) -> None:
    """The limit is written through the vehicle's own `number` setter, blocking, and nothing wakes the car.

    Every service call the request makes is recorded, as in the refresh action's equivalent test.
    """
    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    await one_charger(hass)
    async_mock_service(hass, "number", "set_value")
    # A brand's force-update service, registered as the real one would be: it must never be called.
    brand_calls = async_mock_service(hass, "kia_uvo", "force_update")
    called: list[tuple[str, str, Any]] = []
    original = type(hass.services).async_call

    async def spy(registry, domain, service, *args, **kwargs):
        called.append((domain, service, kwargs.get("blocking")))
        return await original(registry, domain, service, *args, **kwargs)

    monkeypatch.setattr(type(hass.services), "async_call", spy)

    response = await _set(await hass_client_no_auth(), "webhook-a", vehicle["device_id"], 80)

    assert response.status == 200
    # Exactly one call, the blocking setter; nothing that wakes a car.
    assert called == [("number", "set_value", True)]
    assert brand_calls == []


async def test_the_capability_is_advertised_literally(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """A literal `true` in the dashboard's capabilities.

    Like `refresh_vehicle` and `target_stop`: an integration that does not advertise it cannot set a limit,
    and an app may offer the control only on this literal.
    """
    await one_charger(hass)

    dashboard = await _dashboard(await hass_client_no_auth(), "webhook-a")
    capabilities = dashboard["charger"]["capabilities"]

    assert capabilities["set_charge_limit"] is True
    assert capabilities["refresh_vehicle"] is True
    assert capabilities["target_stop"] is True
    assert capabilities["current_limit"] is False


async def test_a_write_to_an_unresolved_two_limit_vehicle_is_refused(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Two limits and nobody has chosen: the one refusal and no call, indistinguishable from an id that names nothing."""
    vehicle = vehicle_device(
        hass, unique_id="two", name="Two", limits=(("limit_a", "80"), ("limit_b", "100"))
    )
    await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")

    response = await _set(await hass_client_no_auth(), "webhook-a", vehicle["device_id"], 80)

    assert response.status == 400
    assert await response.json() == {"ok": False, "error": UNKNOWN_VEHICLE}
    assert calls == []


async def test_a_confirmed_limit_is_the_one_written_to(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The human's choice decides where the write goes, not the value.

    `limits[0]` reports the lower value; a highest-wins rule would never write it. Both the
    service that records the choice and the write that obeys it are exercised, down to the
    value that reached Home Assistant.
    """
    vehicle = vehicle_device(
        hass, unique_id="two", name="Two", limits=(("limit_a", "80"), ("limit_b", "100"))
    )
    await one_charger(hass)
    await hass.services.async_call(
        DOMAIN,
        "confirm_charge_limit",
        {"device_id": vehicle["device_id"], "entity_id": vehicle["limits"][0]},
        blocking=True,
    )
    calls = async_mock_service(hass, "number", "set_value")

    response = await _set(await hass_client_no_auth(), "webhook-a", vehicle["device_id"], 70)

    assert response.status == 200
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == vehicle["limits"][0]
    assert calls[0].data["value"] == 70.0
    # The ceiling the app reads is the confirmed entity's, so what it offers and what a write reaches agree.
    dashboard = await _dashboard(await hass_client_no_auth(), "webhook-a")
    assert [vehicle["max_percent"] for vehicle in dashboard["vehicles"]] == [80.0]


async def test_a_confirmation_that_is_not_a_charge_limit_records_nothing(
    hass: HomeAssistant,
) -> None:
    """A typo, another device's entity, or a sensor of this device is refused.

    Only a live charge-limit candidate of this device is recorded; nothing is stored and
    the device stays unresolved, with one warning in the log naming neither entity nor device.
    """
    vehicle = vehicle_device(
        hass, unique_id="two", name="Two", limits=(("limit_a", "80"), ("limit_b", "100"))
    )
    other = vehicle_device(
        hass, unique_id="other", name="Other", limits=(("charge_limit", "90"),)
    )
    await one_charger(hass)

    for entity_id in (other["limits"][0], vehicle["soc"], "number.does_not_exist"):
        await hass.services.async_call(
            DOMAIN,
            "confirm_charge_limit",
            {"device_id": vehicle["device_id"], "entity_id": entity_id},
            blocking=True,
        )

    assert domain_data(hass).decision_store.confirmed_payload(
        DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, vehicle["device_id"]
    ) is None
    assert vehicle_charge_limit_entity_id(hass, vehicle["device_id"]) is None


async def test_unconfirming_leaves_the_choice_unresolved_again(
    hass: HomeAssistant,
) -> None:
    """A choice can be taken back, and only that choice: the device's state-of-charge confirmation is a different decision domain and is untouched."""
    vehicle = vehicle_device(
        hass, unique_id="two", name="Two", limits=(("limit_a", "80"), ("limit_b", "100"))
    )
    await one_charger(hass)
    confirmed = vehicle["limits"][0]
    await hass.services.async_call(
        DOMAIN,
        "confirm_charge_limit",
        {"device_id": vehicle["device_id"], "entity_id": confirmed},
        blocking=True,
    )
    store = domain_data(hass).decision_store
    await store.async_confirm(
        DECISION_DOMAIN_VEHICLE, vehicle["device_id"], {"soc_entity_id": vehicle["soc"]}
    )
    assert vehicle_charge_limit_entity_id(hass, vehicle["device_id"]) == confirmed

    await hass.services.async_call(
        DOMAIN,
        "unconfirm_charge_limit",
        {"device_id": vehicle["device_id"]},
        blocking=True,
    )

    assert vehicle_charge_limit_entity_id(hass, vehicle["device_id"]) is None
    assert (
        store.confirmed_payload(DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, vehicle["device_id"])
        is None
    )
    # The state-of-charge decision of the same device is untouched.
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, vehicle["device_id"]) == {
        "soc_entity_id": vehicle["soc"]
    }


# ------------------------------------------------- the card's command: `spotnav/write_charge_limit`


def _ws_set(charger_id: object, vehicle_id: object, percent: object, **extra: Any) -> dict[str, Any]:
    return {
        "type": "spotnav/write_charge_limit",
        "api_version": 1,
        "charger_id": charger_id,
        "vehicle_id": vehicle_id,
        "percent": percent,
        **extra,
    }


async def test_the_card_sets_the_limit_through_the_same_write_as_the_app(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """One blocking `number.set_value` on the resolved entity, answered by the command's own envelope."""
    from .world import admin, ws_call

    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    entry = await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")

    answer = (await ws_call(await admin(hass, hass_ws_client), _ws_set(entry.entry_id, vehicle["device_id"], 80)))[
        "result"
    ]

    assert answer == {"api_version": 1, "ok": True, "error": None, "retry_after_s": None}
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": vehicle["limits"][0], "value": 80.0}


async def test_the_card_is_refused_what_the_app_is_refused(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """An unknown car, a percent the limit cannot take, and a write too soon: stable codes, nothing written."""
    from .world import admin, ws_call

    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    gadget = _gadget(hass, unique_id="phone", name="Phone")
    entry = await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")
    socket = await admin(hass, hass_ws_client)

    for vehicle_id, percent in ((gadget, 80), ("no-such-car", 80), (vehicle["device_id"], 0), (vehicle["device_id"], True)):
        refused = (await ws_call(socket, _ws_set(entry.entry_id, vehicle_id, percent)))["result"]
        assert refused == {"api_version": 1, "ok": False, "error": "spotnav_invalid_value", "retry_after_s": None}
    assert calls == []

    assert (await ws_call(socket, _ws_set(entry.entry_id, vehicle["device_id"], 80)))["result"]["ok"] is True
    soon = (await ws_call(socket, _ws_set(entry.entry_id, vehicle["device_id"], 70)))["result"]
    assert soon == {"api_version": 1, "ok": False, "error": "spotnav_too_soon", "retry_after_s": 60}
    assert len(calls) == 1

    unknown = (await ws_call(socket, _ws_set("no-such-charger", vehicle["device_id"], 70)))["result"]
    assert unknown["ok"] is False and unknown["error"] == "spotnav_unknown_charger"


async def test_only_an_administrator_may_set_the_limit(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    """As every other vehicle write from the card: a non-admin is refused before anything is resolved."""
    from .world import non_admin, ws_call

    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    entry = await one_charger(hass)
    calls = async_mock_service(hass, "number", "set_value")

    denied = (
        await ws_call(
            await non_admin(hass, hass_ws_client, hass_read_only_access_token),
            _ws_set(entry.entry_id, vehicle["device_id"], 80),
        )
    )["result"]

    assert denied == {"api_version": 1, "ok": False, "error": "spotnav_not_admin", "retry_after_s": None}
    assert calls == []


async def test_another_version_of_the_command_is_refused(hass: HomeAssistant, hass_ws_client) -> None:
    from .world import admin, ws_call

    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    entry = await one_charger(hass)
    reply = await ws_call(await admin(hass, hass_ws_client), _ws_set(entry.entry_id, vehicle["device_id"], 80, api_version=2))
    assert reply["success"] is False
    assert reply["error"]["code"] == "spotnav_unsupported_api_version"


def _limit_attributes(hass: HomeAssistant, entity_id: str, **attributes: Any) -> None:
    state = hass.states.get(entity_id)
    assert state is not None
    hass.states.async_set(entity_id, state.state, {**state.attributes, **attributes})


def test_the_range_is_the_limit_entitys_own_min_max_and_step(hass: HomeAssistant) -> None:
    """A Kia-shaped limit, 50-100 % in tens: the slider offers exactly what the write accepts."""
    from custom_components.spotnav.vehicles.vehicle_charge_limit import charge_limit_range

    vehicle = vehicle_device(hass, unique_id="niro", name="Niro")
    _limit_attributes(hass, vehicle["limits"][0], step=10)

    assert charge_limit_range(hass, vehicle["device_id"]) == {"min": 50.0, "max": 100.0, "step": 10.0}


def test_a_limit_without_a_step_moves_in_whole_percent(hass: HomeAssistant) -> None:
    from custom_components.spotnav.vehicles.vehicle_charge_limit import charge_limit_range

    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")

    assert charge_limit_range(hass, vehicle["device_id"]) == {"min": 50.0, "max": 100.0, "step": 1.0}


def test_a_range_from_zero_starts_at_its_first_step_above_it(hass: HomeAssistant) -> None:
    """A limit is never 0 % (the write refuses it), so the lowest stop is the first one above."""
    from custom_components.spotnav.vehicles.vehicle_charge_limit import charge_limit_range

    vehicle = vehicle_device(hass, unique_id="ev6", name="EV6")
    _limit_attributes(hass, vehicle["limits"][0], min=0, step=5)

    assert charge_limit_range(hass, vehicle["device_id"]) == {"min": 5.0, "max": 100.0, "step": 5.0}


def test_a_percent_picker_ranges_over_its_options(hass: HomeAssistant) -> None:
    """A `select` limit: from its lowest option to its highest, in the gap between neighbours."""
    from custom_components.spotnav.vehicles.vehicle_charge_limit import charge_limit_range

    vehicle = vehicle_device(hass, unique_id="zoe", name="Zoe", limits=())
    _entity(
        hass,
        device_id=vehicle["device_id"],
        domain="select",
        object_id="charge_limit",
        state="80",
        attributes={"options": ["100", "50", "60", "70", "80", "90"]},
    )

    assert charge_limit_range(hass, vehicle["device_id"]) == {"min": 50.0, "max": 100.0, "step": 10.0}


def test_no_range_without_a_limit_to_write(hass: HomeAssistant) -> None:
    from custom_components.spotnav.vehicles.vehicle_charge_limit import charge_limit_range

    two = vehicle_device(hass, unique_id="two", name="Two", limits=(("a_limit", "60"), ("b_limit", "80")))

    assert charge_limit_range(hass, two["device_id"]) is None
    assert charge_limit_range(hass, "no-such-car") is None


async def test_the_vehicle_row_carries_the_limits_range(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    """Additive on the dashboard's vehicle rows, for the app and the card alike; `null` with nothing to write."""
    from .world import admin, ws_call

    vehicle = vehicle_device(hass, unique_id="niro", name="Niro")
    _limit_attributes(hass, vehicle["limits"][0], step=10)
    entry = await one_charger(hass)

    rows = (await _dashboard(await hass_client_no_auth(), "webhook-a"))["vehicles"]
    row = next(row for row in rows if row["id"] == vehicle["device_id"])
    assert row["charge_limit_range"] == {"min": 50.0, "max": 100.0, "step": 10.0}

    card = (
        await ws_call(
            await admin(hass, hass_ws_client),
            {"type": "spotnav/get_dashboard", "api_version": 1, "charger_id": entry.entry_id},
        )
    )["result"]
    card_row = next(row for row in card["vehicles"] if row["id"] == vehicle["device_id"])
    assert card_row["charge_limit_range"] == {"min": 50.0, "max": 100.0, "step": 10.0}
