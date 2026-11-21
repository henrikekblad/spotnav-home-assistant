"""The hold of a site's chargers during a person's hardware test (`site/charger_test_hold.py`).

While the test's battery part runs every charger of the site is held stopped: no start of any kind goes out, a charge a
charger begins by itself is stopped (bounded), and the home battery's HA-down cap counts a held charger at 0 A only
where its stop is a stored setting it keeps without Home Assistant and it reads stopped now. The hold ends by itself
at its deadline, and the plans whose start it refused are armed again at its release.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.execution.charger_profiles import PATH_SWITCH, PROFILES, profile_for
from custom_components.spotnav.site import charger_test_hold
from custom_components.spotnav.site.charger_test_hold import MAX_STOPS, ChargerTestHold, stop_kept_without_ha

from .helpers import install_schedule
from .test_replug import _open_window, _switch_controller


class _Charger:
    """What the hold reads of a charger: its kind, whether it reads stopped, and the stops it is sent."""

    def __init__(self, platform: str | None = "chargeamps", kind: str = PATH_SWITCH, *, on: bool = False,
                 ocpp: bool = False) -> None:
        self.adapter = SimpleNamespace(platform=platform, path=SimpleNamespace(kind=kind))
        self.ocpp_target = object() if ocpp else None
        self.charging = on
        self.charge_control_on = on
        self.control_confirmed_off = not on
        self.start_pending = False
        self.stop_settling = False
        self.stops = 0
        self.rearms = 0

    async def async_stop(self, **_kwargs: Any) -> None:
        self.stops += 1

    async def async_rearm(self) -> None:
        self.rearms += 1


def _hold(hass: HomeAssistant, chargers: dict[str, _Charger]) -> ChargerTestHold:
    site = SimpleNamespace(entry_id="site_1", hass=hass, config={"charger_entry_ids": list(chargers)})
    return ChargerTestHold(site, controller_for=chargers.get)  # type: ignore[arg-type]


def test_only_a_stored_stop_on_the_profiles_own_path_is_kept_without_home_assistant() -> None:
    kept = sorted(key for key, profile in PROFILES.items() if profile.stop_kept_without_ha)
    assert kept == ["chargeamps", "garo_wallbox", "myenergi"]
    for key in kept:
        profile = profile_for(key)
        assert profile is not None and profile.start_stop is not None
        assert stop_kept_without_ha(_Charger(key, profile.start_stop.kind)) is True
    # A session stop is not: OCPP, Easee's pause, buttons, a pause switch, go-e's force state.
    for key in ("easee", "zaptec", "wallbox", "goecharger_api2", "sigen_ac", "lektrico", "abb_terra_ac", "peblar"):
        profile = profile_for(key)
        assert profile is not None and profile.start_stop is not None
        assert stop_kept_without_ha(_Charger(key, profile.start_stop.kind)) is False, key
    assert stop_kept_without_ha(_Charger(None, PATH_SWITCH)) is False, "an OCPP charger has no platform"
    assert stop_kept_without_ha(_Charger("chargeamps", PATH_SWITCH, ocpp=True)) is False
    assert stop_kept_without_ha(_Charger("chargeamps", "buttons")) is False, "another path than the profile's"
    broken = _Charger()
    broken.adapter = None
    assert stop_kept_without_ha(broken) is False


async def test_a_held_charger_counts_nothing_only_while_held_and_read_stopped(hass: HomeAssistant) -> None:
    kept, ocpp = _Charger(), _Charger(None, ocpp=True)
    hold = _hold(hass, {"kept": kept, "ocpp": ocpp})
    assert hold.counts_nothing("kept") is False, "no hold"
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    assert hold.counts_nothing("kept") is True
    assert hold.counts_nothing("ocpp") is False, "an OCPP charger can begin a charge with Home Assistant gone"
    assert hold.counts_nothing("other") is False, "not the site's"
    kept.start_pending = True
    assert hold.counts_nothing("kept") is False, "a Start of ours on its way"
    kept.start_pending = False
    kept.control_confirmed_off = False
    assert hold.counts_nothing("kept") is False, "not read stopped (unreadable, or on)"
    kept.control_confirmed_off = True
    hold.release()
    assert hold.counts_nothing("kept") is False and hold.active is False


async def test_the_hold_ends_by_itself_at_its_deadline(hass: HomeAssistant) -> None:
    hold = _hold(hass, {"kept": _Charger()})
    hold.begin(dt_util.utcnow() - timedelta(seconds=1))
    assert hold.active is False and hold.holds("kept") is False and hold.counts_nothing("kept") is False


async def test_a_charge_begun_under_the_hold_is_stopped_a_bounded_number_of_times(
    hass: HomeAssistant, freezer: Any
) -> None:
    on = _Charger(on=True)
    hold = _hold(hass, {"on": on, "off": _Charger()})
    assert hold.charging_now() == ["on"]
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert on.stops == 1
    hold.observe()
    await hass.async_block_till_done()
    assert on.stops == 1, "not again within the gap"
    for _ in range(MAX_STOPS + 2):
        freezer.tick(charger_test_hold.STOP_GAP_S + 1)
        hold.observe()
        await hass.async_block_till_done()
    assert on.stops == MAX_STOPS
    assert hold.counts_nothing("on") is False, "a charger that charges is counted"
    hold.release()
    freezer.tick(charger_test_hold.STOP_GAP_S + 1)
    hold.observe()
    await hass.async_block_till_done()
    assert on.stops == MAX_STOPS, "nothing after the release"


async def test_the_release_arms_again_the_plans_whose_start_was_refused(hass: HomeAssistant) -> None:
    refused, quiet = _Charger(), _Charger()
    hold = _hold(hass, {"refused": refused, "quiet": quiet})
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    hold.note_refused("refused")
    names = hold.release()
    assert names == ["refused"] and hold.release() == []
    await hold.async_rearm_refused(names)
    assert (refused.rearms, quiet.rearms) == (1, 0)


async def test_no_start_of_any_kind_goes_out_while_the_site_holds_the_charger(hass: HomeAssistant) -> None:
    controller, plug, starts, _stops = await _switch_controller(hass)
    held = [True]
    refused: list[int] = []
    controller.set_test_hold(lambda: held[0], refused=lambda: refused.append(1))
    await plug.set(True)
    await install_schedule(controller, _open_window())
    await hass.async_block_till_done()
    assert starts == [] and refused, "the plan's window does not start"
    assert await controller.async_start(cause="solar") is False and starts == []
    assert await controller.async_start(manual=True) is False and starts == []
    assert controller.control_confirmed_off is True
    held[0] = False
    await controller.async_rearm()
    await hass.async_block_till_done()
    assert len(starts) == 1, "armed again at the release, the open window starts"
    assert controller.control_confirmed_off is False
    await controller.async_shutdown()


async def test_a_failing_hold_probe_holds_nothing(hass: HomeAssistant) -> None:
    controller, plug, starts, _stops = await _switch_controller(hass)
    controller.set_test_hold(lambda: 1 / 0)
    await plug.set(True)
    assert await controller.async_start(cause="solar") is True and len(starts) == 1
    await controller.async_shutdown()


# --- an OCPP charger taken out of service -----------------------------------------------------------------------------

SWITCH = "switch.halo_charger_availability"


class _Ocpp(_Charger):
    """An OCPP charger of charge point `halo_charger`."""

    def __init__(self, *, on: bool = False) -> None:
        super().__init__(None, PATH_SWITCH, on=on, ocpp=False)
        self.ocpp_target = SimpleNamespace(charge_point_id="halo_charger", connector_id=1)


def _availability(hass: HomeAssistant, state: str = "on", *, reports: bool = True, raises: bool = False) -> list[str]:
    """The `ocpp` integration's station availability switch, registered as it registers it, and `switch.turn_on/off`
    as the charge point answers: it reports itself (un)available (`reports`), or not; the call may fail."""
    er.async_get(hass).async_get_or_create(
        "switch", "ocpp", "switch.ocpp.halo_charger.availability", suggested_object_id="halo_charger_availability"
    )
    hass.states.async_set(SWITCH, state)
    sent: list[str] = []

    def handler(name: str, to: str):
        async def handle(call: Any) -> None:
            sent.append(name)
            if raises:
                raise HomeAssistantError("not connected")
            if reports:
                hass.states.async_set(SWITCH, to)

        return handle

    hass.services.async_register("switch", "turn_off", handler("off", "off"))
    hass.services.async_register("switch", "turn_on", handler("on", "on"))
    return sent


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(charger_test_hold, "CONFIRM_S", 0.0)


async def test_the_station_availability_switch_is_found_by_the_charge_point_s_id(hass: HomeAssistant) -> None:
    _availability(hass)
    assert charger_test_hold.station_availability_entity(hass, _Ocpp()) == SWITCH
    assert charger_test_hold.station_availability_entity(hass, _Charger()) is None
    other = _Ocpp()
    other.ocpp_target = SimpleNamespace(charge_point_id="another", connector_id=1)
    assert charger_test_hold.station_availability_entity(hass, other) is None


async def test_an_ocpp_charger_out_of_service_counts_nothing_and_is_set_back(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    sent = _availability(hass)
    ocpp = _Ocpp()
    hold = _hold(hass, {"ocpp": ocpp})
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert sent == ["off"] and hold.counts_nothing("ocpp") is True
    # Kept before the command went out, so a restart in the middle finds it.
    assert hass_storage[charger_test_hold.store_key("site_1")]["data"]["unavailable"] == {"ocpp": {"entity_id": SWITCH}}
    ocpp.charging = True
    assert hold.counts_nothing("ocpp") is False, "charging after all: counted"
    ocpp.charging = False
    hold.release()
    assert hold.counts_nothing("ocpp") is False
    await hold.async_restore_availability()
    assert sent == ["off", "on"] and hass.states.get(SWITCH).state == "on"
    assert hass_storage[charger_test_hold.store_key("site_1")]["data"]["unavailable"] == {}


async def test_an_ocpp_charger_that_does_not_report_itself_unavailable_stays_counted(hass: HomeAssistant) -> None:
    sent = _availability(hass, reports=False)
    hold = _hold(hass, {"ocpp": _Ocpp()})
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert sent == ["off"] and hold.counts_nothing("ocpp") is False
    hold.release()
    await hold.async_close()


async def test_an_ocpp_charger_already_out_of_service_or_without_the_switch_is_left_alone(hass: HomeAssistant) -> None:
    sent = _availability(hass, state="off")
    hold = _hold(hass, {"ocpp": _Ocpp()})
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert sent == [] and hold.counts_nothing("ocpp") is False and hold.unavailable_kept == []
    hold.release()
    other = _Ocpp()
    other.ocpp_target = SimpleNamespace(charge_point_id="no_switch", connector_id=1)
    hold = _hold(hass, {"other": other})
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert sent == [] and hold.counts_nothing("other") is False
    hold.release()


async def test_an_ocpp_charger_left_out_of_service_by_a_restart_is_set_back_at_the_next_start(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: Any
) -> None:
    hass_storage[charger_test_hold.store_key("site_1")] = {
        "version": 1, "key": charger_test_hold.store_key("site_1"),
        "data": {"unavailable": {"ocpp": {"entity_id": SWITCH}}},
    }
    sent = _availability(hass, state="off")
    hass.states.async_set(SWITCH, "unavailable")  # the charge point not connected yet
    hold = _hold(hass, {"ocpp": _Ocpp()})
    await hold.async_load()
    await hass.async_block_till_done()
    assert sent == [] and hold.unavailable_kept == ["ocpp"]
    hass.states.async_set(SWITCH, "off")
    freezer.tick(charger_test_hold.RESTORE_RETRY_S + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert sent == ["on"] and hold.unavailable_kept == []
    assert hass_storage[charger_test_hold.store_key("site_1")]["data"]["unavailable"] == {}
    await hold.async_close()
