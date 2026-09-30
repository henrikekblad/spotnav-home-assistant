"""The webhook `dashboard` action's charger identity and capabilities."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_WEBHOOK_ID

from .helpers import make_entry, setup_two_chargers, webhook_dashboard


async def test_dashboard_reports_distinct_charger_id_and_name_per_entry(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """Each charger's own webhook must report its own stable entry_id and title."""
    entry_a, entry_b, *_ = await setup_two_chargers(hass)
    client = await hass_client_no_auth()

    dashboard_a = await webhook_dashboard(client, "webhook-a")
    dashboard_b = await webhook_dashboard(client, "webhook-b")

    assert dashboard_a["charger"]["charger_id"] == entry_a.entry_id
    assert dashboard_b["charger"]["charger_id"] == entry_b.entry_id
    assert dashboard_a["charger"]["charger_id"] != dashboard_b["charger"]["charger_id"]

    assert dashboard_a["charger"]["charger_name"] == "Charger A"
    assert dashboard_b["charger"]["charger_name"] == "Charger B"


async def test_dashboard_charger_name_follows_config_entry_title(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """charger_name must reflect the config entry's current title, not a fixed label."""
    hass.states.async_set("switch.renamed_charger", "off")
    entry = make_entry(
        hass,
        entry_id="entry_renamed",
        charge_control="switch.renamed_charger",
        current_limit=None,
        webhook_id="webhook-renamed",
        title="Garage charger",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    dashboard = await webhook_dashboard(client, "webhook-renamed")
    assert dashboard["charger"]["charger_name"] == "Garage charger"

    hass.config_entries.async_update_entry(entry, title="Driveway charger")
    await hass.async_block_till_done()

    after_rename = await webhook_dashboard(client, "webhook-renamed")
    assert after_rename["charger"]["charger_name"] == "Driveway charger"


async def test_dashboard_capabilities_current_limit_reflects_configuration(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """capabilities.current_limit must be true only for an entry with a current-limit entity."""
    hass.states.async_set("switch.with_limit", "off")
    hass.states.async_set("switch.without_limit", "off")
    hass.states.async_set("number.with_limit_current", "10", {"min": 6, "max": 32})

    entry_with_limit = make_entry(
        hass,
        entry_id="entry_with_limit",
        charge_control="switch.with_limit",
        current_limit="number.with_limit_current",
        webhook_id="webhook-with-limit",
        title="Has current limit",
    )
    assert await hass.config_entries.async_setup(entry_with_limit.entry_id)
    await hass.async_block_till_done()

    entry_without_limit = make_entry(
        hass,
        entry_id="entry_without_limit",
        charge_control="switch.without_limit",
        current_limit=None,
        webhook_id="webhook-without-limit",
        title="No current limit",
    )
    assert await hass.config_entries.async_setup(entry_without_limit.entry_id)
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    with_limit = await webhook_dashboard(client, "webhook-with-limit")
    without_limit = await webhook_dashboard(client, "webhook-without-limit")

    assert with_limit["charger"]["capabilities"]["current_limit"] is True
    assert without_limit["charger"]["capabilities"]["current_limit"] is False

    # The server's own abilities are constant, and the retired schedule capability is gone.
    for dashboard in (with_limit, without_limit):
        capabilities = dashboard["charger"]["capabilities"]
        assert capabilities["refresh_vehicle"] is True
        assert capabilities["set_charge_limit"] is True
        assert capabilities["target_stop"] is True
        assert "explicit_schedule" not in capabilities


async def test_dashboard_carries_the_phase_detection_and_the_instance_facts(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """What the app reads beyond the dashboard is part of the one answer."""
    entry_a, entry_b, *_ = await setup_two_chargers(hass)
    client = await hass_client_no_auth()

    dashboard = await webhook_dashboard(client, "webhook-a")

    assert dashboard["api_version"] == 1
    assert dashboard["ok"] is True and dashboard["action"] == "dashboard"
    assert "detected_phases" in dashboard and "phase_detection" in dashboard
    assert dashboard["strategy_options"] == ["cheapest"]
    assert dashboard["chargers"] == [
        {"id": entry_a.entry_id, "name": "Charger A"},
        {"id": entry_b.entry_id, "name": "Charger B"},
    ]
    # Null when nothing has ever been requested/configured -- never defaulted to 0.
    assert dashboard["live"]["requested_current_a"] is None
    assert dashboard["live"]["setpoint_current_a"] is None
    assert dashboard["live"]["charging"] is False
    assert dashboard["plan"]["installed"] is None


async def test_dashboard_response_never_leaks_webhook_id_or_other_secrets(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    """The dashboard payload must not contain any entry's webhook_id, in any field."""
    entry_a, entry_b, *_ = await setup_two_chargers(hass)
    client = await hass_client_no_auth()

    webhook_id_a = entry_a.data[CONF_WEBHOOK_ID]
    webhook_id_b = entry_b.data[CONF_WEBHOOK_ID]

    dashboard_a = await webhook_dashboard(client, "webhook-a")

    serialized = repr(dashboard_a)
    assert webhook_id_a not in serialized
    assert webhook_id_b not in serialized
