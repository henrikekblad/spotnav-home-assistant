"""The site-settings command: `spotnav/update_site_settings` (`api/site_settings.py`).

One admin-only, compare-and-set command over one site entry's `solar_priority` and
`solar_forecast` -- see the module's own docstring for the design. These tests pin: a
successful write (and that the running site controller actually picks the new values up, since
this integration's site entries carry no update listener and only an explicit reload refreshes
them), a stale `expected` refused as `conflict`, every `invalid_value` shape (a bad priority, a
forecast id outside the current choices, and any key besides the two this command owns --
`active_control_enabled` above all, which must never be written here), `no_site` and
`unavailable` for a charger this command cannot resolve to a live site, and `not_admin` for a
caller with no administrative authority -- through the *real* transport, since "admin only" is a
fact about the whole handshake and not something a direct function call can prove.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import (
    CONF_ACTIVE_CONTROL_ENABLED,
    CONF_SOLAR_FORECAST_ENTRIES,
    CONF_SOLAR_PRIORITY,
    SOLAR_PRIORITY_BATTERY_FIRST,
    SOLAR_PRIORITY_CAR_FIRST,
)
from custom_components.spotnav.api.site_settings import (
    ERROR_CONFLICT,
    ERROR_INVALID_VALUE,
    ERROR_NO_SITE,
    ERROR_UNAVAILABLE,
    SITE_SETTINGS_API_VERSION,
)
from custom_components.spotnav.api.common import ERROR_NOT_ADMIN
from custom_components.spotnav.api import site_settings as site_settings_module

from .helpers import make_entry
from .world import admin, non_admin, ws_call
from .world import forecast_entry, setup_site_with_charger
from .messages import update_site_settings_message

_REQUEST_IDS = itertools.count(1)


@pytest.fixture(autouse=True)
def _forecast_capable_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """`roof` is the one Energy-dashboard-capable domain these tests offer, exactly the way
    `test_hybrid_config_flow.py` fixes the same discovery for the options flow: this is about
    this command's own wiring, not a second proof of the cross-integration loader.
    """

    async def _fake(_hass: HomeAssistant) -> frozenset[str]:
        return frozenset({"roof"})

    monkeypatch.setattr(site_settings_module, "async_forecast_capable_domains", _fake)


async def test_a_successful_write_is_re_read_after_the_site_reloads(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """The common case: a stale-free write changes exactly the two fields, once, and the answer
    is the site controller's own post-reload truth -- not the values the caller sent."""
    roof = forecast_entry(hass, entry_id="roof_a", title="Roof A")
    charger, site = await setup_site_with_charger(hass)
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(
        client,
        update_site_settings_message(
            charger_id=charger.entry_id,
            expected={"solar_priority": SOLAR_PRIORITY_CAR_FIRST, "solar_forecast": []},
            changes={
                "solar_priority": SOLAR_PRIORITY_BATTERY_FIRST,
                "solar_forecast": [roof.entry_id],
            },
        ),
    )

    assert frame["success"] is True
    result = frame["result"]
    assert result["ok"] is True and result["error"] is None
    assert result["api_version"] == SITE_SETTINGS_API_VERSION == 1
    assert result["restore"] is None, "a solar write restores nothing"
    site_block = result["site"]
    assert site_block["solar_priority"] == SOLAR_PRIORITY_BATTERY_FIRST
    assert site_block["solar_forecast"]["selected"] == [roof.entry_id]
    assert {choice["id"] for choice in site_block["solar_forecast"]["choices"]} == {roof.entry_id}
    assert site_block["writable"] is True

    # The write reached the config entry (not merely the in-memory reply)...
    reloaded = hass.config_entries.async_get_entry(site.entry_id)
    assert reloaded is not None
    assert reloaded.data[CONF_SOLAR_PRIORITY] == SOLAR_PRIORITY_BATTERY_FIRST
    assert reloaded.data[CONF_SOLAR_FORECAST_ENTRIES] == [roof.entry_id]
    # ...and the *running* site controller actually reloaded with it: a second, independent
    # dashboard read (not the command's own cached reply) agrees.
    from custom_components.spotnav.api import dashboard as dashboard_api

    capture = dashboard_api.capture_dashboard(
        hass, charger, forecast_domains=frozenset({"roof"})
    )
    fresh = dashboard_api.serialize_site(capture.site, can_act=True)
    assert fresh["solar_priority"] == SOLAR_PRIORITY_BATTERY_FIRST
    assert fresh["solar_forecast"]["selected"] == [roof.entry_id]


async def test_a_stale_expected_is_refused_as_a_conflict_and_writes_nothing(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, site = await setup_site_with_charger(
        hass, solar_priority=SOLAR_PRIORITY_CAR_FIRST
    )
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(
        client,
        update_site_settings_message(
            charger_id=charger.entry_id,
            # The card last saw `battery_first`, but the record is actually `car_first`.
            expected={"solar_priority": SOLAR_PRIORITY_BATTERY_FIRST},
            changes={"solar_priority": SOLAR_PRIORITY_BATTERY_FIRST},
        ),
    )

    result = frame["result"]
    assert result["ok"] is False and result["error"] == ERROR_CONFLICT
    # The current record travels back, so the two cards on this site converge from the answer.
    assert result["site"]["solar_priority"] == SOLAR_PRIORITY_CAR_FIRST

    unchanged = hass.config_entries.async_get_entry(site.entry_id)
    assert unchanged.data[CONF_SOLAR_PRIORITY] == SOLAR_PRIORITY_CAR_FIRST


@pytest.mark.parametrize(
    "changes",
    [
        {"solar_priority": "not_a_real_choice"},
        {"solar_forecast": ["not_a_configured_entry"]},
        {"solar_forecast": [123]},
        {"solar_forecast": "not_even_a_list"},
        {"active_control_enabled": "yes"},
        {"unknown_field": "whatever"},
        {},
    ],
    ids=[
        "bad_priority",
        "unknown_forecast_id",
        "non_string_forecast_id",
        "forecast_not_a_list",
        "active_control_enabled_must_be_a_boolean",
        "unknown_key",
        "empty_changes",
    ],
)
async def test_invalid_changes_are_refused_and_write_nothing(
    hass: HomeAssistant, hass_ws_client, changes: dict[str, Any]
) -> None:
    charger, site = await setup_site_with_charger(hass)
    before = dict(hass.config_entries.async_get_entry(site.entry_id).data)
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(
        client, update_site_settings_message(charger_id=charger.entry_id, expected={}, changes=changes)
    )

    result = frame["result"]
    assert result["ok"] is False and result["error"] == ERROR_INVALID_VALUE
    after = dict(hass.config_entries.async_get_entry(site.entry_id).data)
    assert after == before, "an invalid request writes nothing, active_control_enabled included"


async def test_active_control_enabled_is_never_written_even_when_named_alone(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """The one hard guarantee: this command never flips the read-only switch, whatever a caller
    asks for -- refused as `invalid_value`, not silently dropped and answered as success."""
    charger, site = await setup_site_with_charger(hass, active_control_enabled=False)
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(
        client,
        update_site_settings_message(
            charger_id=charger.entry_id,
            expected={},
            changes={"active_control_enabled": True, "solar_priority": SOLAR_PRIORITY_BATTERY_FIRST},
        ),
    )

    result = frame["result"]
    assert result["ok"] is False and result["error"] == ERROR_INVALID_VALUE
    stored = hass.config_entries.async_get_entry(site.entry_id).data
    assert stored.get(CONF_ACTIVE_CONTROL_ENABLED, False) is False
    assert stored.get(CONF_SOLAR_PRIORITY, SOLAR_PRIORITY_CAR_FIRST) == SOLAR_PRIORITY_CAR_FIRST


async def test_a_charger_with_no_site_is_refused_as_no_site(
    hass: HomeAssistant, hass_ws_client
) -> None:
    hass.states.async_set("switch.lone", "off")
    lone = make_entry(
        hass,
        entry_id="lone",
        charge_control="switch.lone",
        current_limit=None,
        webhook_id="webhook-lone",
        title="lone",
    )
    assert await hass.config_entries.async_setup(lone.entry_id)
    await hass.async_block_till_done()
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(client, update_site_settings_message(charger_id=lone.entry_id, expected={}, changes={}))

    result = frame["result"]
    assert result["ok"] is False and result["error"] == ERROR_NO_SITE
    assert result["site"] is None


async def test_an_unknown_charger_is_refused_as_unavailable(
    hass: HomeAssistant, hass_ws_client
) -> None:
    # A loaded charger somewhere in this `hass`, purely so the domain (and this command) is set
    # up at all -- the request itself names an id nothing here has.
    await setup_site_with_charger(hass)
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(
        client, update_site_settings_message(charger_id="no-such-charger", expected={}, changes={})
    )

    result = frame["result"]
    assert result["ok"] is False and result["error"] == ERROR_UNAVAILABLE
    assert result["site"] is None


async def test_a_non_admin_is_refused_as_not_admin_and_writes_nothing(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    charger, site = await setup_site_with_charger(hass)
    before = dict(hass.config_entries.async_get_entry(site.entry_id).data)
    client = await non_admin(hass, hass_ws_client, hass_read_only_access_token)

    frame = await ws_call(
        client,
        update_site_settings_message(
            charger_id=charger.entry_id,
            expected={},
            changes={"solar_priority": SOLAR_PRIORITY_BATTERY_FIRST},
        ),
    )

    # Deliberately a *result* frame (not a transport-level "unauthorized" error): every refusal
    # of this contract, `not_admin` included, is the same envelope shape as a success.
    assert frame["success"] is True
    result = frame["result"]
    assert result["ok"] is False and result["error"] == ERROR_NOT_ADMIN
    assert result["site"] is None
    after = dict(hass.config_entries.async_get_entry(site.entry_id).data)
    assert after == before


async def test_an_unsupported_version_is_the_stable_refusal(
    hass: HomeAssistant, hass_ws_client
) -> None:
    charger, _site = await setup_site_with_charger(hass)
    client = await admin(hass, hass_ws_client)

    frame = await ws_call(
        client,
        update_site_settings_message(charger_id=charger.entry_id, expected={}, changes={}, api_version=3),
    )

    assert frame["success"] is False
    assert frame["error"]["code"] == "spotnav_unsupported_api_version"
