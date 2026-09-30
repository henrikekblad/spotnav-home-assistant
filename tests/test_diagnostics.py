"""Tests for the diagnostics platform: redaction and useful content for
both a charger and a site config entry.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics

from .helpers import make_entry, make_site_entry, set_current_sensor


async def test_charger_diagnostics_redacts_the_webhook_id(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.diag_charger", "off")
    entry = make_entry(
        hass, entry_id="charger_diag", charge_control="switch.diag_charger",
        current_limit=None, webhook_id="super-secret-webhook", title="Diag charger",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry_type"] == "charger"
    assert diagnostics["config"]["webhook_id"] == "**REDACTED**"
    assert "super-secret-webhook" not in str(diagnostics)
    assert diagnostics["controller"]["charge_control"] == "switch.diag_charger"
    assert diagnostics["controller"]["charging"] is False


async def test_charger_diagnostics_never_includes_a_cleartext_webhook_url(
    hass: HomeAssistant,
) -> None:
    """Diagnostics never generate or include a webhook/pairing URL, which would leak the secret."""
    hass.states.async_set("switch.diag_charger2", "off")
    entry = make_entry(
        hass, entry_id="charger_diag2", charge_control="switch.diag_charger2",
        current_limit=None, webhook_id="another-secret", title="Diag charger 2",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "webhook_url" not in diagnostics
    assert "another-secret" not in str(diagnostics)


async def test_site_diagnostics_includes_result_capability_and_membership(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="site_diag", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.site_diag_l1", 5)
    set_current_sensor(hass, "sensor.site_diag_l2", 5)
    set_current_sensor(hass, "sensor.site_diag_l3", 5)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry_type"] == "site"
    assert diagnostics["result"]["state"] == "observing"
    assert diagnostics["capability"]["site_measurement"]["health"] == "healthy"
    assert diagnostics["membership_conflicts"] == []
    # All three explicitly distinct headroom numbers must be present and
    # separately labeled -- a diagnostics dump is exactly where "which
    # number is which" matters most for comparing against a real
    # installation.
    assert "measured_margin_a" in diagnostics["result"]
    assert "calculated_headroom_after_ev_credit_a" in diagnostics["result"]
    assert "estimated_headroom_if_battery_yields_a" in diagnostics["result"]
    assert diagnostics["result"]["phase_headroom_a"] == diagnostics["result"]["measured_margin_a"]
    # Diagnostic-only liveness fields (see `classify_phase_liveness`).
    assert "phase_report_age_s" in diagnostics["result"]
    assert "phase_liveness" in diagnostics["result"]
    # Diagnostic-only signed active power per phase, not an available charging current.
    assert "phase_signed_active_power_w" in diagnostics["result"]
    assert "phase_signed_active_power_age_s" in diagnostics["result"]
    assert "phase_signed_active_power_reason" in diagnostics["result"]


async def test_site_diagnostics_includes_the_regulator_decision_per_charger(
    hass: HomeAssistant,
) -> None:
    """Best-effort load balancing (site/regulator.py) -- a separate,
    diagnostic-only decision from "result" above, keyed by charger.
    """
    hass.states.async_set("switch.diag_regulator_charger", "off")
    charger = make_entry(
        hass, entry_id="charger_diag_regulator", charge_control="switch.diag_regulator_charger",
        current_limit=None, webhook_id="webhook-diag-regulator", title="Diag regulator",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    entry = make_site_entry(
        hass, entry_id="site_diag_regulator", charger_entry_ids=[charger.entry_id]
    )
    set_current_sensor(hass, "sensor.site_diag_regulator_l1", 5)
    set_current_sensor(hass, "sensor.site_diag_regulator_l2", 5)
    set_current_sensor(hass, "sensor.site_diag_regulator_l3", 5)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    decision = diagnostics["regulator"][charger.entry_id]
    # This site is in direct mode (no P/Q/V) -- the regulator always
    # resolves to "derived_mode_required" there, matching every other
    # signed-power diagnostic in this codebase.
    assert decision["reason"] == "derived_mode_required"
    assert decision["proposed_current_a"] is None
    assert "basis" in decision


async def test_site_diagnostics_config_has_no_secrets_to_redact_but_stays_safe(
    hass: HomeAssistant,
) -> None:
    """A site entry holds no webhook of its own -- diagnostics for it must
    still work (no KeyError/crash) and never expose one it doesn't have.
    """
    entry = make_site_entry(hass, entry_id="site_diag_nosecret", charger_entry_ids=[])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "webhook_id" not in diagnostics["config"]
