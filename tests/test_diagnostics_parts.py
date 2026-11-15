"""A site part whose snapshot fails leaves its block empty; the rest of the diagnostics is still given."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.battery.runtime import BatteryShadow
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.loads.runtime import SwitchLoadShadow
from custom_components.spotnav.site.solar_correction_shadow import SiteSolarCorrection

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_a_failing_part_snapshot_leaves_only_its_block_empty(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None

    def broken(_self: Any) -> Any:
        raise RuntimeError("no snapshot")

    for part in (BatteryShadow, SwitchLoadShadow, SiteSolarCorrection):
        monkeypatch.setattr(part, "snapshot", broken)

    diagnostics = await async_get_config_entry_diagnostics(hass, site)

    assert diagnostics["battery"] is None and diagnostics["switch_load"] is None
    assert diagnostics["solar_correction"] is None and diagnostics["entry_type"] == "site"
