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

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

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


# --- an OCPP charger's station ceiling ----------------------------------------------------------------------------------

CEILING = "number.halo_charger_maximum_current"


class _Ocpp(_Charger):
    """An OCPP charger with the integration's station "Maximum Current" number."""

    def __init__(self, *, on: bool = False) -> None:
        super().__init__(None, PATH_SWITCH, on=on, ocpp=True)
        self.ocpp_controls = SimpleNamespace(station_maximum_entity=CEILING)


def _ceiling(hass: HomeAssistant, value: str = "16", *, accepts: bool = True) -> list[float]:
    """The number, and `number.set_value` as the `ocpp` integration answers it: the value taken, or an error raised
    when the charge point does not answer `Accepted`."""
    hass.states.async_set(CEILING, value, {"min": 0, "max": 32})
    written: list[float] = []

    async def set_value(call: Any) -> None:
        written.append(float(call.data["value"]))
        if not accepts:
            raise HomeAssistantError("ChargePointMaxProfile: Rejected")
        hass.states.async_set(CEILING, str(float(call.data["value"])), {"min": 0, "max": 32})

    hass.services.async_register("number", "set_value", set_value)
    return written


async def test_an_ocpp_charger_counts_nothing_only_once_its_ceiling_at_0_a_was_accepted_and_is_set_back(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    written = _ceiling(hass)
    ocpp = _Ocpp()
    hold = _hold(hass, {"ocpp": ocpp})
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert written == [0.0] and hold.counts_nothing("ocpp") is True
    # Kept before the 0 A went out, so a restart in the middle finds it.
    assert hass_storage[charger_test_hold.store_key("site_1")]["data"]["ceilings"]["ocpp"] == {
        "entity_id": CEILING, "previous": 16.0}
    # The person moves the slider during the test: no longer 0 A, counted again.
    hass.states.async_set(CEILING, "10", {"min": 0, "max": 32})
    assert hold.counts_nothing("ocpp") is False
    hass.states.async_set(CEILING, "0.0", {"min": 0, "max": 32})
    hold.release()
    assert hold.counts_nothing("ocpp") is False
    await hold.async_restore_ceilings()
    assert written == [0.0, 16.0] and hass.states.get(CEILING).state == "16.0"
    assert hass_storage[charger_test_hold.store_key("site_1")]["data"]["ceilings"] == {}


async def test_an_ocpp_ceiling_the_charge_point_refuses_leaves_the_charger_counted(hass: HomeAssistant) -> None:
    written = _ceiling(hass, accepts=False)
    hold = _hold(hass, {"ocpp": _Ocpp()})
    hold.begin(dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert written == [0.0] and hold.counts_nothing("ocpp") is False
    hold.release()
    await hold.async_close()


async def test_a_ceiling_left_at_0_a_by_a_restart_is_set_back_at_the_next_start_retried_until_taken(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: Any
) -> None:
    hass_storage[charger_test_hold.store_key("site_1")] = {
        "version": 1, "key": charger_test_hold.store_key("site_1"),
        "data": {"ceilings": {"ocpp": {"entity_id": CEILING, "previous": 13.0}}},
    }
    written = _ceiling(hass, "0")
    hass.states.async_set(CEILING, "unavailable")  # the integration not loaded yet
    hold = _hold(hass, {"ocpp": _Ocpp()})
    await hold.async_load()
    await hass.async_block_till_done()
    assert written == [] and hold.ceilings_kept == ["ocpp"]
    hass.states.async_set(CEILING, "0", {"min": 0, "max": 32})
    freezer.tick(charger_test_hold.RESTORE_RETRY_S + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert written == [13.0] and hold.ceilings_kept == []
    assert hass_storage[charger_test_hold.store_key("site_1")]["data"]["ceilings"] == {}
    await hold.async_close()


async def test_a_ceiling_that_read_no_value_is_set_back_to_its_maximum(hass: HomeAssistant) -> None:
    written = _ceiling(hass, "unknown")
    hold = _hold(hass, {"ocpp": _Ocpp()})
    hold._ceilings["ocpp"] = {"entity_id": CEILING, "previous": None}  # noqa: SLF001
    hass.states.async_set(CEILING, "0", {"min": 0, "max": 32})
    await hold.async_restore_ceilings()
    assert written == [32.0]
