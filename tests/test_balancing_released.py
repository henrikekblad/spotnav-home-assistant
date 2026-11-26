"""A charge load balancing holds back when load balancing stops governing its charger: turned off, the charger taken
off the site, or the site removed. No regulator resumes it any more, so a charge still wanted is given back as the
regulator's resume gives it back (`async_battery_probe_start`), and one no longer wanted is forgotten
(`forget_balancing_pause`); the core decides both, as for the regulator's own resume and drop."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_CHARGER_ENTRY_IDS, MEASUREMENT_MODE_DERIVED
from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED
from custom_components.spotnav.runtime import controller_for, executor_for, site_controller_for

from .test_lb_start_credit import _halo, _Halo, _site_a


@pytest.fixture(params=[True], ids=["core"])
def drives(request: pytest.FixtureRequest) -> bool:
    return request.param


async def _held_back(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, name: str) -> _Halo:
    """The plan's charge, then the site far over its fuse: load balancing pauses it."""
    halo = await _halo(hass, monkeypatch, name)
    await halo.start()
    halo.switch("on")
    halo.reads(10.0)
    _site_a(hass, halo.site, 28.0, 27.0, 26.0)
    await hass.async_block_till_done()
    await halo.tick()
    assert len(halo.turn_off) == 1
    halo.switch("off")
    halo.reads(0.0)
    await hass.async_block_till_done()
    assert halo.cc.paused_by_balancing and halo.cc.paused_charge_origin == "plan_window"
    return halo


def _given_back(halo: _Halo, starts: int, drives: bool) -> None:
    assert len(halo.turn_on) == starts + 1, "given back"
    assert halo.cc.charge_origin == "plan_window" and not halo.cc.paused_by_balancing
    assert halo.cc.paused_charge_origin is None
    if drives:
        session = halo.cc.session_runner.session
        assert (session.owner, session.balancing_paused, session.paused_origin) == ("plan", False, None)


async def _turn_off(halo: _Halo) -> None:
    async with halo.controller.transition_lock:
        await halo.controller.async_disable_active_control()
    await halo.hass.async_block_till_done()


async def test_turning_load_balancing_off_gives_back_the_charge_it_held(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    halo = await _held_back(hass, monkeypatch, "off")
    starts = len(halo.turn_on)
    await _turn_off(halo)
    _given_back(halo, starts, drives)


async def test_turning_load_balancing_off_forgets_a_charge_no_longer_wanted(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """Auto paused until resumed: the plan's charge load balancing held back at its start is not wanted any more."""
    halo = await _halo(hass, monkeypatch, "unwanted")
    await executor_for(hass, halo.charger.entry_id).async_pause(PAUSE_UNTIL_RESUMED)
    _site_a(hass, halo.site, 28.0, 27.0, 26.0)
    await hass.async_block_till_done()
    assert await halo.cc.async_start(16, cause="plan_window") is False
    assert halo.cc.paused_by_balancing and halo.cc.paused_charge_origin == "plan_window"
    starts = len(halo.turn_on)
    await _turn_off(halo)
    assert len(halo.turn_on) == starts, "nothing started"
    assert not halo.cc.paused_by_balancing and halo.cc.paused_charge_origin is None
    assert not halo.cc.session_runner.session.balancing_paused
    kinds = [record["event"]["kind"] for record in halo.cc.session_runner.diagnostics()["events"]]
    assert kinds[-1] == "balancing_dropped"


async def test_a_charger_taken_off_the_site_gets_back_the_charge_its_load_balancing_held(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    halo = await _held_back(hass, monkeypatch, "left")
    starts = len(halo.turn_on)
    site_entry = hass.config_entries.async_get_entry(halo.controller.entry_id)
    data = site_entry.data
    result = await hass.config_entries.options.async_init(site_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "main_fuse_a": data["main_fuse_a"],
            "safety_margin_a": data["safety_margin_a"],
            "measurement_mode": MEASUREMENT_MODE_DERIVED,
            "charger_entry_ids": [],
            "active_control_enabled": True,
            "max_age_s": data["max_age_s"],
        },
    )
    for _ in range(6):
        if result["type"] == "create_entry":
            break
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    assert site_entry.data["charger_entry_ids"] == []
    _given_back(halo, starts, drives)


async def test_removing_the_site_gives_back_the_charge_its_load_balancing_held(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    halo = await _held_back(hass, monkeypatch, "removed")
    starts = len(halo.turn_on)
    await hass.config_entries.async_remove(halo.controller.entry_id)
    await hass.async_block_till_done()
    _given_back(halo, starts, drives)


async def test_a_reload_of_the_site_leaves_the_charge_to_its_regulator(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """An unload is no end of load balancing: the reloaded site governs the charger again and resumes it by its own
    rules."""
    halo = await _held_back(hass, monkeypatch, "reload")
    starts = len(halo.turn_on)
    await hass.config_entries.async_reload(halo.controller.entry_id)
    await hass.async_block_till_done()
    assert len(halo.turn_on) == starts
    assert halo.cc.paused_by_balancing and halo.cc.paused_charge_origin == "plan_window"


async def test_disabling_the_site_gives_back_the_charge_its_load_balancing_held(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """A disabled site does not come back by itself: no regulator resumes the charge it held, so it is given back as at
    a removal."""
    halo = await _held_back(hass, monkeypatch, "disabled")
    starts = len(halo.turn_on)
    await hass.config_entries.async_set_disabled_by(halo.controller.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    _given_back(halo, starts, drives)


async def test_disabling_a_charger_hands_back_the_charge_load_balancing_held_and_its_site_leaves_it_out(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """A disabled charger is handed back as at a removal: the stop load balancing made is SpotNav's own, so the charger
    is started again. While it is disabled its site governs it no more; enabled again, the site takes it on again."""
    halo = await _held_back(hass, monkeypatch, "charger_disabled")
    site_id = halo.controller.entry_id
    charger_id = halo.charger.entry_id
    starts = len(halo.turn_on)
    await hass.config_entries.async_set_disabled_by(charger_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert len(halo.turn_on) == starts + 1, "handed back: started again"
    site = site_controller_for(hass, site_id)
    assert site is not None and site is not halo.controller, "the site was set up again"
    assert charger_id not in site.config[CONF_CHARGER_ENTRY_IDS]
    assert all(request.charger_entry_id != charger_id for request in site._build_requests())  # noqa: SLF001
    assert charger_id in hass.config_entries.async_get_entry(site_id).data[CONF_CHARGER_ENTRY_IDS], "still listed"

    await hass.config_entries.async_set_disabled_by(charger_id, None)
    await hass.async_block_till_done()
    again = site_controller_for(hass, site_id)
    assert again is not None and again is not site
    assert charger_id in again.config[CONF_CHARGER_ENTRY_IDS]
    assert controller_for(hass, charger_id) is not None


async def test_a_reload_of_a_charger_leaves_it_to_its_site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    halo = await _held_back(hass, monkeypatch, "charger_reload")
    starts = len(halo.turn_on)
    assert await hass.config_entries.async_reload(halo.charger.entry_id)
    await hass.async_block_till_done()
    assert len(halo.turn_on) == starts, "nothing handed back"
    assert site_controller_for(hass, halo.controller.entry_id) is halo.controller, "the site was not set up again"
