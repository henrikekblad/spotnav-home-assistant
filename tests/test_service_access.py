"""Who may call SpotNav's services.

The six decision services change the integration's configuration and are admin services, the same
rule as the card's `spotnav/choose_vehicle_soc`. `send_test_notification` messages the household's
phones and is an admin service too. A call with no user behind it (an automation) is allowed.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.vehicles.discovery_decisions import (
    DECISION_DOMAIN_VEHICLE,
    DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT,
)

from .world import one_charger, vehicle_device


ADMIN_SERVICES = (
    "dismiss_vehicle",
    "undismiss_vehicle",
    "unconfirm_vehicle",
    "unconfirm_charge_limit",
    "confirm_charge_limit",
    "confirm_vehicle_soc",
)


def _data(service: str, vehicle: dict[str, Any]) -> dict[str, str]:
    if service == "confirm_charge_limit":
        return {"device_id": vehicle["device_id"], "entity_id": vehicle["limits"][0]}
    if service == "confirm_vehicle_soc":
        return {"device_id": vehicle["device_id"], "entity_id": vehicle["soc"]}
    return {"device_id": vehicle["device_id"]}


async def _two_limit_vehicle(hass: HomeAssistant) -> dict[str, Any]:
    vehicle = vehicle_device(
        hass, unique_id="two", name="Two", limits=(("limit_a", "80"), ("limit_b", "100"))
    )
    await one_charger(hass)
    return vehicle


@pytest.mark.parametrize("service", ADMIN_SERVICES)
async def test_a_user_who_is_not_an_administrator_is_refused(
    hass: HomeAssistant, hass_read_only_user: Any, service: str
) -> None:
    """Every decision service refuses a non-administrator and records nothing."""
    vehicle = await _two_limit_vehicle(hass)
    store = domain_data(hass).decision_store

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            service,
            _data(service, vehicle),
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, vehicle["device_id"]) is False
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE, vehicle["device_id"]) is None
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, vehicle["device_id"]) is None


async def test_an_administrator_and_an_automation_may_decide(
    hass: HomeAssistant, hass_admin_user: Any
) -> None:
    """An administrator's call is recorded, and so is one with no user behind it."""
    vehicle = await _two_limit_vehicle(hass)
    store = domain_data(hass).decision_store
    admin = Context(user_id=hass_admin_user.id)

    await hass.services.async_call(
        DOMAIN, "confirm_charge_limit", _data("confirm_charge_limit", vehicle), blocking=True, context=admin
    )
    assert store.confirmed_payload(DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, vehicle["device_id"]) == {
        "charge_limit_entity_id": vehicle["limits"][0]
    }
    await hass.services.async_call(
        DOMAIN, "dismiss_vehicle", _data("dismiss_vehicle", vehicle), blocking=True, context=admin
    )
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, vehicle["device_id"]) is True

    await hass.services.async_call(
        DOMAIN, "undismiss_vehicle", _data("undismiss_vehicle", vehicle), blocking=True
    )
    assert store.is_dismissed(DECISION_DOMAIN_VEHICLE, vehicle["device_id"]) is False


async def test_the_test_notification_is_for_administrators(
    hass: HomeAssistant, hass_read_only_user: Any, hass_admin_user: Any
) -> None:
    """A non-administrator is refused before anything is sent; an administrator reaches the service.

    With no app registered the administrator's call ends in the service's own refusal, not `Unauthorized`.
    """
    await one_charger(hass)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "send_test_notification",
            {},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    with pytest.raises(ServiceValidationError) as refusal:
        await hass.services.async_call(
            DOMAIN,
            "send_test_notification",
            {},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
    assert refusal.value.translation_key == "no_push_registered"
