"""Numbers the options form shows are rounded for display: no `28.749999999999996`."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_YIELD_CEILING_A, default_yield_ceiling_a
from custom_components.spotnav.flows.labels import display_number

from .helpers import make_site_entry, set_current_sensor


@pytest.mark.parametrize(
    ("value", "shown"),
    [(28.749999999999996, 28.7), (30.0, 30), (16, 16), (6.25, 6.2), (0.0, 0), (12.04, 12)],
)
def test_display_number_keeps_one_decimal_and_drops_a_zero_fraction(value, shown) -> None:
    result = display_number(value)
    assert result == shown
    assert str(result) == str(shown)


async def test_the_yield_ceiling_default_is_shown_rounded_and_stays_the_derived_default(
    hass: HomeAssistant,
) -> None:
    entry = make_site_entry(hass, entry_id="ceiling_display", charger_entry_ids=[], main_fuse_a=25)
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.ceiling_display_{phase}", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    marker = next(key for key in result["data_schema"].schema if key == CONF_YIELD_CEILING_A)
    assert marker.default() == 28.7
    assert str(marker.default()) == "28.7"
    # The unrounded value is what a site uses when it never customised the ceiling.
    assert default_yield_ceiling_a(25) == pytest.approx(28.75)


async def test_submitting_the_rounded_default_is_not_a_customised_ceiling(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="ceiling_untouched", charger_entry_ids=[], main_fuse_a=25)
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.ceiling_untouched_{phase}", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Ceiling",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": False,
            "change_measurement": True,
            "max_age_s": 120,
            "yield_stepping_enabled": True,
            CONF_YIELD_CEILING_A: 28.7,
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    flow = hass.config_entries.options._progress[result["flow_id"]]
    assert flow._pending_yield_ceiling_customized is False
