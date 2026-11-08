"""`spotnav/send_debug_info`: the debug bundle sent to SpotNav's developer, against a fake relay."""

from __future__ import annotations

import gzip
import hashlib
import json
import threading

import pytest
from aiohttp import ClientError
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id

from custom_components.spotnav import support_report
from custom_components.spotnav.notifications.push import DEFAULT_BASE_URL
from custom_components.spotnav.support_report import INSTALL_SALT, REPORT_PATH

from .world import admin, non_admin, setup_charger_and_site, ws_call

URL = f"{DEFAULT_BASE_URL}{REPORT_PATH}"


def _send(**fields) -> dict:
    return {"type": "spotnav/send_debug_info", "api_version": 1, "message": "It stopped at 03:00", **fields}


def _sent_body(aioclient_mock) -> dict:
    assert aioclient_mock.call_count == 1
    _method, url, data, headers = aioclient_mock.mock_calls[0]
    assert str(url) == URL
    assert headers["Content-Type"] == "application/json"
    assert headers["Content-Encoding"] == "gzip"
    return json.loads(gzip.decompress(data))


async def test_an_admin_sends_the_bundle_and_gets_the_reference(
    hass: HomeAssistant, hass_ws_client, aioclient_mock
) -> None:
    charger, _ = await setup_charger_and_site(hass)
    aioclient_mock.post(URL, status=201, json={"ref": "SN-7KQ4"})

    frame = await ws_call(
        await admin(hass, hass_ws_client),
        _send(email=" me@example.com ", client={"card_version": "2.5.0", "user_agent": "x" * 2000, "extra": "no"}),
    )

    assert frame["success"] is True
    assert frame["result"] == {"api_version": 1, "ok": True, "error": None, "ref": "SN-7KQ4", "retry_after_s": None}
    body = _sent_body(aioclient_mock)
    assert set(body) == {"version", "install", "message", "email", "spotnav_version", "ha_version", "bundle"}
    assert body["version"] == 1
    assert body["message"] == "It stopped at 03:00"
    assert body["email"] == "me@example.com"
    assert body["ha_version"] == HA_VERSION
    assert body["spotnav_version"] == body["bundle"]["versions"]["spotnav"]
    # The same bundle as the download, with the card's own block kept to its known keys.
    assert body["bundle"]["chargers"][0]["entry_id"] == charger.entry_id
    assert body["bundle"]["client"] == {"card_version": "2.5.0", "user_agent": "x" * 512}
    # The installation is a salted hash; the instance id itself is never sent.
    raw = await instance_id.async_get(hass)
    assert body["install"] == hashlib.sha256(f"{raw}{INSTALL_SALT}".encode()).hexdigest()
    assert len(body["install"]) == 64
    assert raw not in json.dumps(body)


async def test_no_email_is_sent_as_null(hass: HomeAssistant, hass_ws_client, aioclient_mock) -> None:
    await setup_charger_and_site(hass)
    aioclient_mock.post(URL, status=201, json={"ref": "SN-ABCD"})

    frame = await ws_call(await admin(hass, hass_ws_client), _send(email=""))

    assert frame["result"]["ref"] == "SN-ABCD"
    body = _sent_body(aioclient_mock)
    assert body["email"] is None
    assert "client" not in body["bundle"]


async def test_a_non_admin_is_refused_and_nothing_is_sent(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str, aioclient_mock
) -> None:
    await setup_charger_and_site(hass)
    aioclient_mock.post(URL, status=201, json={"ref": "SN-ABCD"})

    frame = await ws_call(await non_admin(hass, hass_ws_client, hass_read_only_access_token), _send())

    assert frame["success"] is False
    assert frame["error"]["code"] == "unauthorized"
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"message": "   "}, "spotnav_report_invalid_message"),
        ({"message": "x" * 4001}, "spotnav_report_invalid_message"),
        ({"message": 7}, "spotnav_report_invalid_message"),
        ({"email": "not an address"}, "spotnav_report_invalid_email"),
        ({"email": "a@" + "b" * 250 + ".se"}, "spotnav_report_invalid_email"),
    ],
)
async def test_a_bad_message_or_address_is_refused_before_anything_is_sent(
    hass: HomeAssistant, hass_ws_client, aioclient_mock, fields: dict, error: str
) -> None:
    await setup_charger_and_site(hass)

    frame = await ws_call(await admin(hass, hass_ws_client), _send(**fields))

    assert frame["result"] == {"api_version": 1, "ok": False, "error": error, "ref": None, "retry_after_s": None}
    assert aioclient_mock.call_count == 0


async def test_another_version_is_refused(hass: HomeAssistant, hass_ws_client, aioclient_mock) -> None:
    await setup_charger_and_site(hass)
    frame = await ws_call(await admin(hass, hass_ws_client), _send(api_version=2))
    assert frame["success"] is False
    assert frame["error"]["code"] == "spotnav_unsupported_api_version"
    assert aioclient_mock.call_count == 0


def test_control_characters_are_stripped_but_newlines_kept() -> None:
    assert support_report.clean_message("a\x00b\r\nc\x1b[0m\td") == "ab\nc[0m d"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"status": 429, "json": {"retry_after_s": 3600}}, ("spotnav_report_rate_limited", 3600)),
        (
            {"status": 429, "json": {"error": "rate_limited", "retry_after_s": 10}, "headers": {"Retry-After": "10"}},
            ("spotnav_report_rate_limited", 10),
        ),
        ({"status": 429, "text": "", "headers": {"Retry-After": "120"}}, ("spotnav_report_rate_limited", 120)),
        ({"status": 413, "text": "too large"}, ("spotnav_report_too_large", None)),
        ({"status": 400, "text": "bad"}, ("spotnav_report_rejected", None)),
        ({"status": 503, "text": ""}, ("spotnav_report_server", None)),
        ({"status": 201, "json": {"ref": "nonsense"}}, ("spotnav_report_server", None)),
        ({"exc": ClientError()}, ("spotnav_report_network", None)),
        ({"exc": TimeoutError()}, ("spotnav_report_network", None)),
    ],
)
async def test_each_refusal_is_one_stable_code(
    hass: HomeAssistant, hass_ws_client, aioclient_mock, response: dict, expected: tuple
) -> None:
    await setup_charger_and_site(hass)
    aioclient_mock.post(URL, **response)

    frame = await ws_call(await admin(hass, hass_ws_client), _send())

    error, wait = expected
    assert frame["success"] is True
    assert frame["result"] == {"api_version": 1, "ok": False, "error": error, "ref": None, "retry_after_s": wait}


async def test_a_body_over_the_limit_is_not_sent(
    hass: HomeAssistant, hass_ws_client, aioclient_mock, monkeypatch
) -> None:
    await setup_charger_and_site(hass)
    monkeypatch.setattr(support_report, "MAX_BODY_BYTES", 10)

    frame = await ws_call(await admin(hass, hass_ws_client), _send())

    assert frame["result"]["error"] == "spotnav_report_too_large"
    assert aioclient_mock.call_count == 0


async def test_the_body_is_encoded_and_compressed_off_the_event_loop(
    hass: HomeAssistant, hass_ws_client, aioclient_mock, monkeypatch
) -> None:
    await setup_charger_and_site(hass)
    aioclient_mock.post(URL, status=201, json={"ref": "SN-ABCD"})
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = support_report.encode_report

    def encode(payload):
        threads.append(threading.get_ident())
        return real(payload)

    monkeypatch.setattr(support_report, "encode_report", encode)

    frame = await ws_call(await admin(hass, hass_ws_client), _send())

    assert frame["result"]["ok"] is True
    assert threads and loop_thread not in threads
