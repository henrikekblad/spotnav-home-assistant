"""The "does not fit" notice on the phones (`ChargerNotifier.does_not_fit`, plan step 3b): its words and buttons, told
once per departure in the daytime only (a night's one at 07:00 if it still stands), taken off once it no longer
stands, and the buttons' presses calling the person's choice for the departure told."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.notifications.messages import Money, does_not_fit_choice_text, does_not_fit_text
from custom_components.spotnav.notifications.notifier import is_quiet, quiet_end
from custom_components.spotnav.runtime import charger_data

from .test_import_target_hold import _HOUR, _block, _tight_night, _told
from .test_notifications import _charger

#: 2026-11-02 in Stockholm (UTC+1): 22:30 at night, the next morning's 07:00 and a departure at 07:30.
_NIGHT = datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)
_MORNING = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
_DEPARTURE = datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)
_DAY = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


# -- the words


def test_the_notice_names_the_ceiling_and_what_is_short_in_swedish_and_english() -> None:
    assert does_not_fit_text("Garage", "sv", time="07:00", short_kwh=4.0, ceiling_kw=6.0, set_kw=6.0)[1] == (
        "Bilen blir inte klar till 07:00 under effektmålet 6,0 kW – 4 kWh saknas."
    )
    assert does_not_fit_text("Garage", "en", time="07:00", short_kwh=4.0, ceiling_kw=6.0, set_kw=6.0)[1] == (
        "The car will not be ready by 07:00 under the 6.0 kW import target – 4 kWh short."
    )
    # The month's peak lifts the hour: both are named.
    assert does_not_fit_text("Garage", "en", time="07:00", short_kwh=4.0, ceiling_kw=7.5, set_kw=6.0)[1] == (
        "The car will not be ready by 07:00 under the month's peak of 7.5 kW (import target 6.0 kW) – 4 kWh short."
    )
    # No time: by the departure.
    assert does_not_fit_text("Garage", "sv", time=None, short_kwh=4.0, ceiling_kw=6.0)[1] == (
        "Bilen blir inte klar till avresan under effektmålet 6,0 kW – 4 kWh saknas."
    )


def test_the_buttons_say_the_choices_with_their_figures() -> None:
    money = Money(10500, "SEK")
    assert does_not_fit_choice_text("sv", "takes_what_fits", kwh=12.0) == "Ta det som ryms (≈ 12 kWh)"
    assert does_not_fit_choice_text("sv", "accept_peak", peak_kw=12.0, cost=money) == "Tillåt toppen (≈ 12 kW, ≈ 105 kr)"
    assert does_not_fit_choice_text("en", "takes_what_fits", kwh=12.0) == "Take what fits (≈ 12 kWh)"
    assert does_not_fit_choice_text("en", "accept_peak", peak_kw=12.0, cost=money) == "Allow the peak (≈ 12 kW, ≈ 105 kr)"
    # A figure not known is left out.
    assert does_not_fit_choice_text("en", "accept_peak", peak_kw=12.0) == "Allow the peak (≈ 12 kW)"
    assert does_not_fit_choice_text("en", "takes_what_fits") == "Take what fits"


async def test_the_quiet_hours_are_the_night_in_local_time(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    assert is_quiet(_NIGHT)  # 22:30
    assert is_quiet(_MORNING - timedelta(minutes=1))  # 06:59
    assert not is_quiet(_MORNING)  # 07:00
    assert not is_quiet(_DAY)  # 13:00
    assert not is_quiet(datetime(2026, 11, 2, 20, 59, tzinfo=timezone.utc))  # 21:59
    assert quiet_end(_NIGHT) == _MORNING
    assert quiet_end(_MORNING - timedelta(hours=3)) == _MORNING


# -- when it is told


async def _phone(hass: HomeAssistant) -> tuple[Any, list[Any]]:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    entry, calls = await _charger(hass, events=("plan_at_risk",))
    return charger_data(hass, entry.entry_id).notifier, calls


def _sent(calls: list[Any]) -> list[dict]:
    return [call.data for call in calls if call.data.get("message") != "clear_notification"]


def _cleared(calls: list[Any]) -> list[dict]:
    return [call.data for call in calls if call.data.get("message") == "clear_notification"]


class _Site:
    """What the site gives the notifier on each recompute: the block standing now, and itself to ask again."""

    def __init__(self, notifier: Any, block: dict | None) -> None:
        self.notifier = notifier
        self.block = block

    def tell(self) -> str | None:
        return self.notifier.does_not_fit(self.block, recheck=self.tell)


async def test_in_the_daytime_it_is_told_at_once_and_once_per_departure(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        notifier, calls = await _phone(hass)
        site = _Site(notifier, _block(_DEPARTURE.isoformat()))
        assert site.tell() == "told"
        assert site.tell() is None
        await hass.async_block_till_done()
        sent = _sent(calls)
        assert len(sent) == 1
        assert sent[0]["message"] == "The car will not be ready by 07:30 under the 6.0 kW import target – 4 kWh short."
        assert [action["title"] for action in sent[0]["data"]["actions"]] == [
            "Take what fits (≈ 12 kWh)",
            "Allow the peak (≈ 12 kW)",  # no currency known in this test: no price
        ]
        # It stops standing: taken off; back for the same departure: not told again.
        site.block = None
        assert site.tell() == "cleared"
        site.block = _block(_DEPARTURE.isoformat(), short=6.0)
        assert site.tell() is None
        await hass.async_block_till_done()
        assert len(_sent(calls)) == 1 and len(_cleared(calls)) == 1
        # Another departure is news.
        site.block = _block((_DEPARTURE + timedelta(days=1)).isoformat())
        assert site.tell() == "told"


async def test_a_choice_made_anywhere_takes_it_off_the_phones(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        notifier, calls = await _phone(hass)
        site = _Site(notifier, _block(_DEPARTURE.isoformat()))
        assert site.tell() == "told"
        # Chosen in the card (or the app): the block says so, nothing is asked.
        site.block = {**_block(_DEPARTURE.isoformat()), "chosen": "takes_what_fits"}
        assert site.tell() == "cleared"
        await hass.async_block_till_done()
        assert _cleared(calls)[0]["data"]["tag"] == _sent(calls)[0]["data"]["tag"]


async def test_at_night_nothing_is_told_until_seven_if_it_still_stands(hass: HomeAssistant) -> None:
    with freeze_time(_NIGHT) as frozen:
        notifier, calls = await _phone(hass)
        site = _Site(notifier, _block(_DEPARTURE.isoformat()))
        assert site.tell() is None
        await hass.async_block_till_done()
        assert _sent(calls) == [], "no notice at night"
        assert notifier._fit_holds_back(), "nor the plan's own at-risk notice in its place"  # noqa: SLF001
        frozen.move_to(_MORNING)
        async_fire_time_changed(hass, _MORNING)
        await hass.async_block_till_done()
        sent = _sent(calls)
        assert len(sent) == 1 and sent[0]["message"].startswith("The car will not be ready by 07:30 ")


async def test_at_night_one_that_ends_before_seven_is_never_told(hass: HomeAssistant) -> None:
    with freeze_time(_NIGHT) as frozen:
        notifier, calls = await _phone(hass)
        site = _Site(notifier, _block(_DEPARTURE.isoformat()))
        site.tell()
        site.block = None
        site.tell()
        assert not notifier._fit_holds_back()  # noqa: SLF001
        frozen.move_to(_MORNING)
        async_fire_time_changed(hass, _MORNING)
        await hass.async_block_till_done()
        assert calls == []


async def test_at_night_a_departure_before_seven_is_never_told(hass: HomeAssistant) -> None:
    with freeze_time(_NIGHT) as frozen:
        notifier, calls = await _phone(hass)
        site = _Site(notifier, _block((_MORNING - timedelta(minutes=30)).isoformat()))
        assert site.tell() is None
        assert notifier._fit_timer is None  # noqa: SLF001 - nothing waits for the morning
        frozen.move_to(_MORNING)
        async_fire_time_changed(hass, _MORNING)
        await hass.async_block_till_done()
        assert calls == []


async def test_no_phone_chosen_tells_nothing(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        await hass.config.async_set_time_zone("Europe/Stockholm")
        entry, calls = await _charger(hass, targets=(), events=("plan_at_risk",))
        notifier = charger_data(hass, entry.entry_id).notifier
        assert notifier.does_not_fit(_block(_DEPARTURE.isoformat())) is None
        await hass.async_block_till_done()
        assert calls == []


async def test_the_departure_told_is_kept_across_a_restart(hass: HomeAssistant, hass_storage) -> None:
    with freeze_time(_DAY):
        notifier, _calls = await _phone(hass)
        assert notifier.does_not_fit(_block(_DEPARTURE.isoformat())) == "told"
        await hass.async_block_till_done()
        record = next(value["data"] for key, value in hass_storage.items() if key.startswith("spotnav.notified_plan."))
        assert record["does_not_fit"] == {"departure": _DEPARTURE.isoformat(), "phones": ["mobile_app_pixel"]}


# -- the buttons, on a real site whose import target makes the car miss its departure


async def test_take_what_fits_from_the_phone_holds_this_departure_and_takes_the_notice_off(
    hass: HomeAssistant, monkeypatch
) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "phonetake")
        await world.run(2)
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        told = _told(world)
        assert len(told) == 1
        take = told[0]["data"]["actions"][0]["action"]
        assert take == f"SPOTNAV_TAKE_WHAT_FITS_{charger}_{int(world.departure.timestamp())}"
        hass.bus.async_fire("mobile_app_notification_action", {"action": take})
        await hass.async_block_till_done()
        await world.run(2)
        assert controller.import_target_snapshot()["takes_what_fits"] == {charger: world.departure.isoformat()}
        assert world.car_a == 7.0, "still held under the target"
        assert world.calls[-1].data["message"] == "clear_notification"
        assert len(_told(world)) == 1


async def test_a_take_what_fits_press_for_another_departure_is_refused(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.site.site_capacity_controller import ERROR_STALE_DEPARTURE

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "phonestale")
        await world.run(2)
        charger = world.halo.charger.entry_id
        controller = world.halo.controller
        stale = int(world.departure.timestamp()) - 3600
        assert controller.async_take_what_fits(charger, for_departure=stale) == ERROR_STALE_DEPARTURE
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_TAKE_WHAT_FITS_{charger}_{stale}"})
        await hass.async_block_till_done()
        assert controller.import_target_snapshot()["takes_what_fits"] == {}
        assert dt_util.utcnow() < world.departure
