"""The pairing handshake: a code a person compares, approved in Home Assistant.

The endpoint is public and unauthenticated, so these tests are mostly about
what it refuses and what it hands over: an approval can be re-fetched for a minute, unknown and expired requests are indistinguishable, nothing
sensitive reaches a log, and no path creates a config entry.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.api.pairing import (
    MAX_PENDING_REQUESTS,
    PAIRING_WEBHOOK_ID,
    PairingRegister,
)
from custom_components.spotnav.vehicles.charger_inventory import charger_pairing_payload

from .helpers import make_entry
from custom_components.spotnav.runtime import domain_data

EXTERNAL_URL = "https://ha.example.com"
CODE = "123456"


class Clock:
    """A clock the test moves by hand: expiry is asserted, never slept through."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


async def _loaded(hass: HomeAssistant) -> None:
    """Bring the integration up: which is what registers the pairing endpoint."""
    hass.config.external_url = EXTERNAL_URL
    hass.states.async_set("switch.charger_a", "off")
    async_mock_service(hass, "switch", "turn_on")
    entry = make_entry(
        hass,
        entry_id="entry_charger_a",
        charge_control="switch.charger_a",
        current_limit=None,
        webhook_id="webhook-a",
        title="Garage",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # These tests make many requests from one address; the address limit has its own test.
    domain_data(hass).pairing = PairingRegister(remote_interval_s=0.0)


def _register(hass: HomeAssistant) -> PairingRegister:
    return domain_data(hass).pairing


async def _post(client, body) -> tuple[int, dict]:
    response = await client.post(f"/api/webhook/{PAIRING_WEBHOOK_ID}", json=body)
    return response.status, await response.json()


async def _request(client, *, code: str = CODE, device: str = "Pixel 8") -> str:
    status, body = await _post(
        client, {"version": 1, "action": "request", "device": device, "code": code}
    )
    assert status == 200
    assert body["ok"] is True
    return body["request_id"]


async def _poll(client, request_id: str) -> dict:
    status, body = await _post(client, {"version": 1, "action": "poll", "request_id": request_id})
    assert status == 200
    return body


async def _configure_only_flow(hass: HomeAssistant, option: str) -> dict:
    """Decide the single request that is waiting, the way a person would."""
    flows = [flow for flow in hass.config_entries.flow.async_progress() if flow["handler"] == DOMAIN]
    assert len(flows) == 1
    return await hass.config_entries.flow.async_configure(flows[0]["flow_id"], user_input={"next_step_id": option})

# --- the register: expiry, the cap, and re-delivery --------------------------


def test_a_request_is_open_and_pending() -> None:
    register = PairingRegister(now=Clock())

    record = register.open(CODE, "Pixel 8")

    assert record is not None
    assert register.take(record.request_id) == ("pending", record)
    assert register.pending_count() == 1


def test_an_approval_can_be_fetched_again_for_a_minute_after_first_delivery() -> None:
    clock = Clock()
    register = PairingRegister(now=clock)
    record = register.open(CODE, "Pixel 8")

    assert register.approve(record.request_id) is True

    assert register.take(record.request_id) == ("approved", record)
    assert register.pending_count() == 0
    # The first response may have been lost: the same id gets the same verdict.
    clock.advance(59.0)
    assert register.take(record.request_id) == ("approved", record)
    # After the window it is gone, and says nothing about that.
    clock.advance(2.0)
    assert register.take(record.request_id) == ("expired", None)


def test_a_denial_can_be_fetched_again_for_a_minute_after_first_delivery() -> None:
    clock = Clock()
    register = PairingRegister(now=clock)
    record = register.open(CODE, "Pixel 8")

    assert register.deny(record.request_id) is True

    assert register.take(record.request_id)[0] == "denied"
    clock.advance(30.0)
    assert register.take(record.request_id)[0] == "denied"
    clock.advance(31.0)
    assert register.take(record.request_id)[0] == "expired"


def test_a_request_expires_after_five_minutes() -> None:
    clock = Clock()
    register = PairingRegister(now=clock)
    record = register.open(CODE, "Pixel 8")

    clock.advance(299.0)
    assert register.take(record.request_id)[0] == "pending"

    clock.advance(2.0)
    assert register.take(record.request_id) == ("expired", None)
    # Dropped rather than left to accumulate.
    assert register.pending_count() == 0


def test_an_approval_that_arrives_too_late_approves_nothing() -> None:
    clock = Clock()
    register = PairingRegister(now=clock)
    record = register.open(CODE, "Pixel 8")

    clock.advance(301.0)

    assert register.approve(record.request_id) is False
    assert register.take(record.request_id)[0] == "expired"


def test_an_unknown_request_is_expired() -> None:
    register = PairingRegister(now=Clock())

    assert register.take("never-issued") == ("expired", None)
    assert register.approve("never-issued") is False
    assert register.deny("never-issued") is False


def test_the_cap_holds_and_expired_requests_free_it_again() -> None:
    clock = Clock()
    register = PairingRegister(now=clock)

    records = [register.open(CODE, f"phone {index}") for index in range(MAX_PENDING_REQUESTS)]

    assert all(record is not None for record in records)
    assert register.open(CODE, "one too many") is None
    assert register.pending_count() == MAX_PENDING_REQUESTS

    # A stranger cannot hold the endpoint: waiting out the timeout releases it.
    clock.advance(301.0)
    assert register.open(CODE, "later") is not None


def test_a_request_id_is_random_not_a_counter() -> None:
    register = PairingRegister(now=Clock())

    first = register.open(CODE, "a")
    second = register.open(CODE, "b")

    assert first is not None and second is not None
    assert first.request_id != second.request_id
    assert not first.request_id.isdigit()
    assert len(first.request_id) >= 32

# --- the endpoint ------------------------------------------------------------


async def test_a_request_is_recorded_and_answered(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _loaded(hass)

    request_id = await _request(await hass_client_no_auth())

    assert _register(hass).pending_count() == 1
    assert _register(hass).describe(request_id).code == CODE
    assert _register(hass).describe(request_id).device == "Pixel 8"


async def test_a_poll_before_a_decision_is_pending(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()

    assert await _poll(client, await _request(client)) == {"status": "pending"}


async def test_an_approved_request_delivers_the_pairing_payload_again_to_the_same_request(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    request_id = await _request(client)
    assert _register(hass).approve(request_id) is True

    answer = await _poll(client, request_id)

    assert answer["status"] == "approved"
    assert answer["url"] == EXTERNAL_URL
    # Exactly what the pairing entity shows: id, name and this charger's own
    # webhook id, and the only place a webhook id crosses to the app.
    assert answer["chargers"] == charger_pairing_payload(hass)
    assert answer["chargers"][0]["webhook"] == "webhook-a"
    # A lost response must not waste the approval: the same id gets the same payload again.
    assert await _poll(client, request_id) == answer
    # Another id never saw an approval.
    assert await _poll(client, "never-issued") == {"status": "expired"}


async def test_a_denial_is_reported_again_to_the_same_request(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    request_id = await _request(client)
    assert _register(hass).deny(request_id) is True

    assert await _poll(client, request_id) == {"status": "denied"}
    assert await _poll(client, request_id) == {"status": "denied"}
    assert await _poll(client, "never-issued") == {"status": "expired"}


async def test_an_unknown_request_is_indistinguishable_from_an_expired_one(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    clock = Clock()
    domain_data(hass).pairing = PairingRegister(now=clock, remote_interval_s=0.0)
    timed_out = await _request(client)
    clock.advance(301.0)

    expired = await _poll(client, timed_out)
    unknown = await _poll(client, "never-issued")

    assert expired == {"status": "expired"}
    assert unknown == expired


async def test_malformed_and_unsupported_bodies_are_refused(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    url = f"/api/webhook/{PAIRING_WEBHOOK_ID}"

    bodies = [
        {"version": 2, "action": "request", "code": CODE},
        {"version": "1", "action": "request", "code": CODE},
        {"version": 1, "action": "something-else"},
        {"version": 1},
        {"version": 1, "action": "request", "device": "Pixel 8"},
        {"version": 1, "action": "request", "code": "12345"},
        {"version": 1, "action": "request", "code": 123456},
        {"version": 1, "action": "poll"},
        {"version": 1, "action": "poll", "request_id": 42},
        [1, 2, 3],
    ]
    for body in bodies:
        response = await client.post(url, json=body)
        assert response.status == 400, body
        assert (await response.json())["ok"] is False, body

    # Not JSON at all, which must be refused rather than raised.
    response = await client.post(url, data="not json")
    assert response.status == 400
    assert (await response.json())["ok"] is False


async def test_a_request_past_the_cap_is_refused_with_a_reason(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    for _ in range(MAX_PENDING_REQUESTS):
        await _request(client)

    status, body = await _post(
        client, {"version": 1, "action": "request", "device": "Pixel 8", "code": CODE}
    )

    # A refusal the app reads as a refusal: no request id, and a reason a human
    # reading a curl output can act on.
    assert status == 400
    assert body["ok"] is False
    assert "too many" in body["error"].lower()


async def test_a_second_request_from_one_address_is_refused_until_the_interval_has_passed(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _loaded(hass)
    clock = Clock()
    domain_data(hass).pairing = PairingRegister(now=clock)
    client = await hass_client_no_auth()
    await _request(client)

    response = await client.post(
        f"/api/webhook/{PAIRING_WEBHOOK_ID}",
        json={"version": 1, "action": "request", "device": "Pixel 8", "code": CODE},
    )
    body = await response.json()
    assert response.status == 429
    assert body["ok"] is False and "request_id" not in body
    assert body["retry_after_s"] == 10 and response.headers["Retry-After"] == "10"
    assert "wait 10 seconds" in body["error"]
    assert _register(hass).pending_count() == 1

    clock.advance(10.0)
    await _request(client)
    assert _register(hass).pending_count() == 2


# --- approving, by hand, in Home Assistant ----------------------------------


async def test_approving_in_the_flow_delivers_the_payload(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    request_id = await _request(client)
    await hass.async_block_till_done()

    result = await _configure_only_flow(hass, "pair_approve")

    assert (result["type"], result["reason"]) == ("abort", "pairing_approved")
    answer = await _poll(client, request_id)
    assert answer["status"] == "approved"
    assert answer["chargers"] == charger_pairing_payload(hass)


async def test_denying_in_the_flow_reports_a_denial(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    await _request(client)
    await hass.async_block_till_done()

    result = await _configure_only_flow(hass, "pair_deny")

    assert (result["type"], result["reason"]) == ("abort", "pairing_denied")


async def test_no_path_creates_a_config_entry(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    before = [entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)]

    # Approved...
    await _request(client)
    await hass.async_block_till_done()
    assert (await _configure_only_flow(hass, "pair_approve"))["type"] == "abort"
    # ...denied...
    await _request(client)
    await hass.async_block_till_done()
    assert (await _configure_only_flow(hass, "pair_deny"))["type"] == "abort"
    # ...and a request that has already run out of time have the same answer.
    clock = Clock()
    domain_data(hass).pairing = PairingRegister(now=clock, remote_interval_s=0.0)
    await _request(client)
    await hass.async_block_till_done()
    assert (await _configure_only_flow(hass, "pair_approve"))["type"] == "abort"

    assert [entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)] == before


async def test_nothing_sensitive_is_logged(hass: HomeAssistant, hass_client_no_auth, caplog) -> None:
    await _loaded(hass)
    client = await hass_client_no_auth()
    caplog.set_level("DEBUG")

    request_id = await _request(client)
    assert _register(hass).approve(request_id) is True
    await _poll(client, request_id)
    await _post(client, {"version": 1, "action": "something-else"})

    logged = caplog.text
    # The code, the request id and the charger's own webhook id: none of them,
    # at debug either.
    assert CODE not in logged
    assert request_id not in logged
    assert "webhook-a" not in logged
    # The payload's own values: the charger's config entry id, and the six-digit
    # code that is only ever meant for a person to read on a screen.
    assert "entry_charger_a" not in logged
    assert '"id"' not in logged
    # (Home Assistant's own webhook component logs "Handling webhook POST
    # payload for <id>" at debug for every webhook -- pre-existing, and about a
    # fixed, public id that grants nothing. This module adds nothing to it,
    # which is what the assertions above are for.)


async def test_an_approval_is_not_spent_while_the_instance_has_no_address(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """An approval must not be delivered badly.

    Home Assistant cannot always say what its own address is -- most likely
    just after a restart, before the instance URL is known. Answering
    "approved" with a blank address would spend a person's approval on a
    payload the app refuses, which is exactly what it must not do: the
    approval stays waiting, and the next poll gets it.
    """
    await _loaded(hass)
    client = await hass_client_no_auth()
    request_id = await _request(client)
    await _configure_only_flow(hass, "pair_approve")

    with patch(
        "custom_components.spotnav.api.pairing.instance_base_url", return_value=""
    ):
        assert await _poll(client, request_id) == {"status": "pending"}

    # Still there, and still approved: nothing was consumed.
    answer = await _poll(client, request_id)
    assert answer["status"] == "approved"
    assert answer["url"]
