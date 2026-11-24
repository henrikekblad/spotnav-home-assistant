"""The site's combined "not ready in time" notice (`notifications/site_notice.py`): one notice per phone across the cars'
departures, the spa's baths and the sauna's sessions, the three choices that free the most as buttons, once per
deadline, not at night, taken off once nothing asks; a car alone told by its own notifier as before; the spa's and the
sauna's buttons; and the pool's season notice."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.spotnav.notifications.heat_settings import HeatNotifications
from custom_components.spotnav.notifications.site_notice import SiteNotice
from custom_components.spotnav.site.not_ready import car_item, deadline_item, site_block

#: 2026-11-02 in Stockholm (UTC+1): 13:00, 22:30 and the next morning's 07:00.
_DAY = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
_NIGHT = datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)
_MORNING = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
_BATH = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)  # 19:00 local
_DEPARTURE = datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)  # 07:30 local
_SITE = "site1"


class _Car:
    """A charger's notifier as the site notice sees it."""

    def __init__(self, name: str, targets: tuple[str, ...] = ("mobile_app_pixel",)) -> None:
        self.name = name
        self.targets = targets
        self.notifications_url = "/lovelace/ev"
        self.push = None
        self.may = True
        self.holds = False
        self.told: str | None = None
        self.own: list[Any] = []
        self.sent = 0

    def fit_may_tell(self) -> bool:
        return self.may

    def hold_for_site(self, holds: bool) -> None:
        self.holds = holds

    def does_not_fit(self, block: Any, recheck: Any = None) -> str | None:
        self.own.append(block)
        return None

    def mark_fit_told(self, departure: str) -> None:
        self.told = departure

    def fit_targets(self, _now: datetime) -> tuple[str, ...]:
        return self.targets

    def fit_sent(self, _now: datetime) -> None:
        self.sent += 1


@dataclass
class _NotReady:
    at: datetime
    cause: str = "import_target"
    later: datetime | None = None


@dataclass
class _Part:
    not_ready: _NotReady | None = None
    taken: list[str] = field(default_factory=list)

    @property
    def plan(self) -> Any:
        return self

    def take_what_fits(self, at: datetime) -> None:
        self.taken.append(f"fits {at.isoformat()}")

    def allow_peak(self, at: datetime) -> None:
        self.taken.append(f"peak {at.isoformat()}")

    def move_later(self) -> tuple[datetime | None, None]:
        self.taken.append("later")
        return None, None


class _Shadow:
    def __init__(self) -> None:
        self.spa = _Part(_NotReady(_BATH, later=_BATH + timedelta(hours=1)))
        self.sauna = _Part()
        self.looks = 0

    def request_look(self) -> None:
        self.looks += 1


class _Site:
    def __init__(self) -> None:
        self.pool_spa = _Shadow()
        self.released: list[tuple[str, int]] = []

    def async_release_import_target(self, charger: str, *, for_departure: int, source: str) -> None:
        self.released.append((charger, for_departure))


def _car_block(departure: datetime = _DEPARTURE, short: float = 4.0, later: datetime | None = None) -> dict:
    return {
        "departure": departure.isoformat(),
        "short_kwh": short,
        "deliverable_kwh": 12.0,
        "ceiling": "import_target",
        "ceiling_kw": 6.0,
        "set_kw": 6.0,
        "chosen": None,
        "choices": [
            {"code": "takes_what_fits", "kwh": 12.0, "peak_kw": None, "cost": None, "departure": None},
            {"code": "later", "kwh": None, "peak_kw": None, "cost": None, "departure": None if later is None else later.isoformat()},
            {"code": "accept_peak", "kwh": None, "peak_kw": 11.0, "cost": None, "departure": None},
        ],
    }


def _bath(at: datetime = _BATH, cause: str = "import_target", short_kwh: float = 6.0) -> dict:
    return {"at": at.isoformat(), "cause": cause, "reach_c": 35.0, "short_c": 3.0, "short_kwh": short_kwh, "later": (at + timedelta(hours=1)).isoformat()}


class _World:
    def __init__(self, hass: HomeAssistant, cars: dict[str, _Car], heat_targets: tuple[str, ...] = ("mobile_app_ipad",)) -> None:
        self.hass = hass
        self.cars = cars
        self.site = _Site()
        self.rechecks = 0
        self.heat = HeatNotifications(targets=heat_targets)
        self.notice = SiteNotice(
            hass, _SITE, site=self.site, notifier_of=cars.get, heat_notifications=lambda: self.heat, recheck=self._recheck
        )
        self.notice.async_start()
        self.car_blocks: dict[str, dict | None] = {}
        self.baths: list[dict] = []
        self.pixel = async_mock_service(hass, "notify", "mobile_app_pixel")
        self.ipad = async_mock_service(hass, "notify", "mobile_app_ipad")

    def _recheck(self) -> None:
        self.rechecks += 1
        self.tell()

    def block(self) -> dict | None:
        items = [car_item(charger, self.cars[charger].name, blk) for charger, blk in self.car_blocks.items()]
        items += [deadline_item("spa", "Spa", bath) for bath in self.baths]
        return site_block(items, ["heat", *(f"charger:{charger}" for charger in self.cars), "spa", "sauna"])

    def tell(self) -> dict[str, str]:
        blocks = {charger: self.car_blocks.get(charger) for charger in self.cars}
        return self.notice.update(self.block(), blocks)


def _sent(calls: list[Any]) -> list[dict]:
    return [call.data for call in calls if call.data.get("message") != "clear_notification"]


def _cleared(calls: list[Any]) -> list[dict]:
    return [call.data for call in calls if call.data.get("message") == "clear_notification"]


async def _world(hass: HomeAssistant, **cars: _Car) -> _World:
    from custom_components.spotnav import texts

    await texts.async_load(hass)
    await hass.config.async_set_time_zone("Europe/Stockholm")
    return _World(hass, cars or {"garage": _Car("Garage")})


async def test_a_car_and_a_bath_are_one_notice_per_phone_with_the_three_choices_that_free_the_most(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        world.cars["garage"].targets = ("mobile_app_pixel", "mobile_app_ipad")
        world.car_blocks["garage"] = _car_block(later=_DEPARTURE + timedelta(hours=1))
        world.baths = [_bath()]
        assert world.tell() == {"garage": "told"}
        await hass.async_block_till_done()
        pixel, ipad = _sent(world.pixel), _sent(world.ipad)
        # Each phone once, though the ipad follows both the car and the spa.
        assert len(pixel) == 1 and len(ipad) == 1
        sent = pixel[0]
        assert sent["title"] == "SpotNav · Not ready in time"
        assert sent["message"] == "Spa 19:00: reaches 35 °C\nGarage 07:30: 4 kWh short under the 6.0 kW import target"
        assert sent["data"]["tag"] == f"spotnav_site_{_SITE}_not_ready"
        assert sent["data"]["url"] == "/lovelace/ev#spotnav-not-ready"
        actions = sent["data"]["actions"]
        assert len(actions) == 3, "Android shows three"
        stamp = int(_BATH.timestamp())
        assert actions[0]["action"].startswith(f"SPOTNAV_NOT_READY_PEAK_{_SITE}_")
        assert actions[0]["title"] == "Allow the peak"
        # The bath frees more than the car; at a tie what costs nothing first.
        assert actions[1] == {
            "action": f"SPOTNAV_LATER_BATH_{_SITE}_{stamp}_{int((_BATH + timedelta(hours=1)).timestamp())}",
            "title": "Spa: Later bath (20:00)",
        }
        assert actions[2] == {"action": f"SPOTNAV_SPA_TAKE_WHAT_FITS_{_SITE}_{stamp}", "title": "Spa: Take what fits (≈ 35 °C)"}
        # The car's own notice is off; its plan_at_risk is held behind this one; its departure counts as told.
        car = world.cars["garage"]
        assert car.own == [None] and car.holds and car.told == _DEPARTURE.isoformat() and car.sent == 1


async def test_a_car_alone_is_told_by_its_own_notifier_as_before(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        block = _car_block()
        world.car_blocks["garage"] = block
        world.tell()
        await hass.async_block_till_done()
        assert _sent(world.pixel) == [] and _sent(world.ipad) == []
        car = world.cars["garage"]
        assert car.own == [block] and not car.holds
        # A bath joins: the site's notice takes over, the car's own is taken off.
        world.baths = [_bath()]
        assert world.tell() == {"garage": "told"}
        assert car.own[-1] is None and car.holds
        # The bath is answered: the car alone stays in the site's notice (never told again on its own).
        world.baths = []
        world.tell()
        await hass.async_block_till_done()
        assert len(_sent(world.pixel)) == 1 and car.own[-1] is None and car.holds
        # The car fits: taken off, and the car is its notifier's again.
        world.car_blocks["garage"] = None
        assert world.tell() == {"garage": "cleared"}
        await hass.async_block_till_done()
        assert _cleared(world.pixel)[0]["data"]["tag"] == f"spotnav_site_{_SITE}_not_ready"
        assert not car.holds and car.own[-1] is None


async def test_once_per_deadline_and_a_new_deadline_tells_it_again(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        world.baths = [_bath()]
        world.tell()
        world.baths = [_bath(short_kwh=9.0)]  # it grows: no news
        world.tell()
        await hass.async_block_till_done()
        assert len(_sent(world.ipad)) == 1
        # A car joins: told again under the same tag, both listed.
        world.car_blocks["garage"] = _car_block()
        world.tell()
        await hass.async_block_till_done()
        sent = _sent(world.ipad)
        assert len(sent) == 2 and sent[1]["data"]["tag"] == sent[0]["data"]["tag"]
        assert sent[1]["message"].count("\n") == 1
        # It all fits: taken off every phone it is on.
        world.baths, world.car_blocks["garage"] = [], None
        world.tell()
        await hass.async_block_till_done()
        assert len(_cleared(world.ipad)) == 1 and len(_cleared(world.pixel)) == 1
        # The same bath back: not told again.
        world.baths = [_bath()]
        world.tell()
        await hass.async_block_till_done()
        assert len(_sent(world.ipad)) == 2


async def test_at_night_nothing_is_told_until_seven_if_it_still_stands(hass: HomeAssistant) -> None:
    with freeze_time(_NIGHT) as frozen:
        world = await _world(hass)
        world.car_blocks["garage"] = _car_block()
        world.baths = [_bath(at=_MORNING + timedelta(hours=10))]
        world.tell()
        await hass.async_block_till_done()
        assert _sent(world.pixel) == [] and _sent(world.ipad) == []
        assert world.cars["garage"].holds, "the car's own plan_at_risk waits behind it"
        frozen.move_to(_MORNING)
        async_fire_time_changed(hass, _MORNING)
        await hass.async_block_till_done()
        assert world.rechecks == 1
        assert len(_sent(world.pixel)) == 1 and len(_sent(world.ipad)) == 1


async def test_at_night_one_whose_deadlines_are_before_seven_is_never_told(hass: HomeAssistant) -> None:
    with freeze_time(_NIGHT) as frozen:
        world = await _world(hass)
        world.baths = [_bath(at=_NIGHT + timedelta(hours=2))]
        world.tell()
        frozen.move_to(_MORNING)
        async_fire_time_changed(hass, _MORNING)
        await hass.async_block_till_done()
        assert world.rechecks == 0 and _sent(world.ipad) == []


async def test_a_car_away_or_held_by_a_stop_is_left_out(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        world.cars["garage"].may = False
        world.car_blocks["garage"] = _car_block()
        world.baths = [_bath()]
        world.tell()
        await hass.async_block_till_done()
        assert _sent(world.pixel) == [], "only the car's phones, and the car is left out"
        assert _sent(world.ipad)[0]["message"] == "Spa 19:00: reaches 35 °C"
        assert not world.cars["garage"].holds


async def test_no_phone_for_the_spa_tells_nothing(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        world.heat = HeatNotifications(targets=("mobile_app_ipad",), events=("pool_season",))
        world.baths = [_bath()]
        world.tell()
        await hass.async_block_till_done()
        assert _sent(world.ipad) == [] and world.notice.on_phones == ()


async def test_two_cars_and_a_bath_offer_three_buttons_of_the_six_choices(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass, garage=_Car("Garage"), carport=_Car("Carport"))
        world.car_blocks = {"garage": _car_block(short=2.0), "carport": _car_block(_DEPARTURE + timedelta(hours=1), short=8.0)}
        world.baths = [_bath(cause="fuse")]
        world.tell()
        await hass.async_block_till_done()
        sent = _sent(world.pixel)
        assert len(sent) == 1, "one notice for the phone that follows both cars"
        titles = [action["title"] for action in sent[0]["data"]["actions"]]
        assert titles == ["Allow the peak", "Carport: Take what fits (≈ 12 kWh)", "Spa: Later bath (20:00)"]


async def test_the_spa_buttons_take_the_choice_for_the_bath_told_only(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        stamp = int(_BATH.timestamp())
        part = world.site.pool_spa.spa
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_SPA_TAKE_WHAT_FITS_{_SITE}_{stamp + 3600}"})
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_LATER_BATH_{_SITE}_{stamp}_{stamp + 7200}"})
        await hass.async_block_till_done()
        assert part.taken == [], "another bath, another later time: refused"
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_LATER_BATH_{_SITE}_{stamp}_{stamp + 3600}"})
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_SPA_TAKE_WHAT_FITS_{_SITE}_{stamp}"})
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_SPA_PEAK_{_SITE}_{stamp}"})
        await hass.async_block_till_done()
        assert part.taken == ["later", f"fits {_BATH.isoformat()}", f"peak {_BATH.isoformat()}"]
        assert world.site.pool_spa.looks == 3
        # Another site's press is not this one's.
        hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_SPA_TAKE_WHAT_FITS_other_{stamp}"})
        await hass.async_block_till_done()
        assert len(part.taken) == 3


async def test_the_sites_peak_sets_the_ceiling_aside_for_every_item_told(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        world.car_blocks["garage"] = _car_block()
        world.baths = [_bath()]
        world.tell()
        await hass.async_block_till_done()
        peak = _sent(world.pixel)[0]["data"]["actions"][0]["action"]
        hass.bus.async_fire("mobile_app_notification_action", {"action": peak[:-1] + "9"})
        await hass.async_block_till_done()
        assert world.site.released == [], "a notice no longer standing"
        hass.bus.async_fire("mobile_app_notification_action", {"action": peak})
        await hass.async_block_till_done()
        assert world.site.released == [("garage", int(_DEPARTURE.timestamp()))]
        assert world.site.pool_spa.spa.taken == [f"peak {_BATH.isoformat()}"]


async def test_the_told_deadlines_are_kept_across_a_restart(hass: HomeAssistant, hass_storage: dict) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        world.baths = [_bath()]
        world.tell()
        await hass.async_block_till_done()
        world.notice.async_shutdown()
        await hass.async_block_till_done()
        again = SiteNotice(hass, _SITE, site=world.site, notifier_of=world.cars.get, heat_notifications=lambda: world.heat, recheck=lambda: None)
        # Saved after its delay.
        async_fire_time_changed(hass, _DAY + timedelta(seconds=5))
        await hass.async_block_till_done()
        await again.async_load()
        assert again.on_phones == ("mobile_app_ipad",)
        again.update(world.block(), {"garage": None})
        await hass.async_block_till_done()
        assert len(_sent(world.ipad)) == 1, "not told again after the restart"


async def test_the_pool_season_is_told_in_the_daytime_with_its_button(hass: HomeAssistant, monkeypatch) -> None:
    world = await _world(hass)
    world.heat = HeatNotifications(targets=("mobile_app_ipad",))
    assert not world.notice.pool_season(_NIGHT)
    assert world.notice.pool_season(_DAY)
    await hass.async_block_till_done()
    sent = _sent(world.ipad)
    assert sent[0]["message"] == "The water has been around 10 °C for three days: time to close the pool season?"
    assert sent[0]["data"]["actions"] == [{"action": f"SPOTNAV_POOL_SEASON_{_SITE}", "title": "Close the season"}]
    world.heat = HeatNotifications(targets=("mobile_app_ipad",), events=("plan_at_risk",))
    assert not world.notice.pool_season(_DAY), "not wanted"
    # The button closes the season as the card's switch does.
    asked: list[dict] = []
    monkeypatch.setattr(
        "custom_components.spotnav.api.pool_spa.pool_answer", lambda _hass, _entry, msg: asked.append(msg) or {"ok": True}
    )
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda _entry_id: object())
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"SPOTNAV_POOL_SEASON_{_SITE}"})
    await hass.async_block_till_done()
    assert asked == [{"action": "season", "open": False}]


# -- through the running site


async def test_on_the_site_a_car_and_a_bath_are_told_together_and_the_cars_button_still_works(
    hass: HomeAssistant, monkeypatch
) -> None:
    from homeassistant.util import dt as dt_util

    from custom_components.spotnav.site import not_ready

    from .test_import_target_hold import _HOUR, _told, _tight_night

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "combined")
        await world.run(2)
        charger = world.halo.charger.entry_id
        told = _told(world)
        assert len(told) == 1 and told[0]["data"]["tag"] == f"spotnav_{charger}_plan_at_risk", "a car alone: as before"
        bath = dt_util.utcnow() + timedelta(hours=5)
        item = deadline_item("spa", "Spa", _bath(at=bath))
        monkeypatch.setattr(not_ready, "deadline_items", lambda _site: [dict(item, choices=[dict(c) for c in item["choices"]])])
        site = world.halo.controller
        site._tell_target_risk()  # noqa: SLF001
        await hass.async_block_till_done()
        told = _told(world)
        assert len(told) == 2
        combined = told[1]
        assert combined["data"]["tag"] == f"spotnav_site_{site.entry_id}_not_ready"
        assert combined["message"].splitlines()[0].startswith("HALO 14:30: ")
        # The car's own notice is taken off the phone.
        assert any(
            call.data.get("message") == "clear_notification" and call.data["data"]["tag"] == f"spotnav_{charger}_plan_at_risk"
            for call in world.calls
        )
        actions = [action["action"] for action in combined["data"]["actions"]]
        take = next(action for action in actions if action.startswith("SPOTNAV_TAKE_WHAT_FITS_"))
        assert take == f"SPOTNAV_TAKE_WHAT_FITS_{charger}_{int(world.departure.timestamp())}"
        hass.bus.async_fire("mobile_app_notification_action", {"action": take})
        await hass.async_block_till_done()
        await world.run(1)
        status = site.import_target_status(charger)
        assert status is not None and status.at_risk is not None and status.at_risk.choice == "hold"
        # Nothing asks any more once the bath is answered too: taken off.
        monkeypatch.setattr(not_ready, "deadline_items", lambda _site: [])
        site._tell_target_risk()  # noqa: SLF001
        await hass.async_block_till_done()
        assert world.calls[-1].data == {"message": "clear_notification", "data": {"tag": combined["data"]["tag"]}}


async def test_a_plan_at_risk_held_behind_the_sites_notice_is_told_once_it_lets_go(hass: HomeAssistant) -> None:
    from custom_components.spotnav.runtime import charger_data

    from .test_notifications import _charger

    with freeze_time(_DAY):
        await hass.config.async_set_time_zone("Europe/Stockholm")
        entry, calls = await _charger(hass, events=("plan_at_risk",))
        notifier = charger_data(hass, entry.entry_id).notifier
        notifier.hold_for_site(True)
        assert notifier._fit_holds_back()  # noqa: SLF001
        notifier._at_risk_behind_target = {"departure": "07:30"}  # noqa: SLF001
        notifier._at_risk_now = True  # noqa: SLF001
        # Its own notice going down keeps it while the site's holds it.
        notifier.does_not_fit(None)
        assert notifier._at_risk_behind_target is not None  # noqa: SLF001
        notifier.hold_for_site(False)
        await hass.async_block_till_done()
        assert [call.data["data"]["tag"] for call in calls] == [f"spotnav_{entry.entry_id}_plan_at_risk"]
        assert notifier._at_risk_behind_target is None  # noqa: SLF001


def test_the_pool_shadow_tells_the_season_once_a_season_and_only_once_heard() -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.pool.runtime import PoolSpaShadow

    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    days = {(now.date() - timedelta(days=offset)).isoformat(): [8.0, 9.5] for offset in range(1, 4)}
    heard: list[bool] = [False]
    told: list[datetime] = []

    def pool_season(moment: datetime) -> bool:
        told.append(moment)
        return heard[0]

    shadow = SimpleNamespace(
        water_days=days, season_hinted=None, site=SimpleNamespace(site_notice=SimpleNamespace(pool_season=pool_season)),
        _local=lambda moment: moment,
    )
    pool = SimpleNamespace(season_open=True)
    PoolSpaShadow._tell_season(shadow, now, pool)  # noqa: SLF001
    assert len(told) == 1 and shadow.season_hinted is None, "no phone heard it: asked again later"
    heard[0] = True
    PoolSpaShadow._tell_season(shadow, now, pool)  # noqa: SLF001
    PoolSpaShadow._tell_season(shadow, now + timedelta(hours=1), pool)  # noqa: SLF001
    assert len(told) == 2 and shadow.season_hinted == 2026, "once a season"
    # A closed season says nothing.
    shadow.season_hinted = None
    PoolSpaShadow._tell_season(shadow, now, SimpleNamespace(season_open=False))  # noqa: SLF001
    assert len(told) == 2


async def test_a_car_dropping_out_of_the_sites_notice_is_its_notifiers_again(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass, garage=_Car("Garage"), carport=_Car("Carport"))
        world.car_blocks = {"garage": _car_block(), "carport": _car_block(_DEPARTURE + timedelta(hours=1))}
        world.baths = [_bath()]
        world.tell()
        carport = world.cars["carport"]
        assert carport.holds and carport.own == [None]
        # The carport's driver answers; the garage and the bath still stand: the carport is told as alone (nothing to
        # ask) and no longer held behind the site's notice.
        world.car_blocks["carport"] = None
        world.tell()
        assert not carport.holds and carport.own[-1] is None and len(carport.own) == 2
        assert world.cars["garage"].holds


async def test_a_car_told_alone_is_taken_off_when_it_answers_as_a_bath_stops_fitting(hass: HomeAssistant) -> None:
    with freeze_time(_DAY):
        world = await _world(hass)
        world.car_blocks["garage"] = _car_block()
        world.tell()
        car = world.cars["garage"]
        assert car.own == [world.car_blocks["garage"]]
        # At once: the car's choice made, the spa no longer fits. The car's own notice gets its "nothing asks".
        world.car_blocks["garage"] = None
        world.baths = [_bath()]
        world.tell()
        assert car.own[-1] is None and not car.holds


async def test_a_held_plan_at_risk_is_told_once_its_car_drops_out_of_the_sites_notice(hass: HomeAssistant) -> None:
    from custom_components.spotnav import texts
    from custom_components.spotnav.runtime import charger_data

    from .test_notifications import _charger

    with freeze_time(_DAY) as frozen:
        await texts.async_load(hass)
        await hass.config.async_set_time_zone("Europe/Stockholm")
        entry, _calls = await _charger(hass, events=("plan_at_risk",))
        notifier = charger_data(hass, entry.entry_id).notifier
        world = _World(hass, {entry.entry_id: notifier})
        calls = world.pixel  # the phone's service, mocked again by the world
        world.car_blocks[entry.entry_id] = _car_block()
        world.baths = [_bath()]
        world.tell()
        await hass.async_block_till_done()
        assert notifier._fit_holds_back()  # noqa: SLF001
        tag = f"spotnav_{entry.entry_id}_plan_at_risk"
        # The plan cannot be met for reasons of its own as well: held behind the site's notice.
        notifier._at_risk_behind_target = {"departure": "07:30"}  # noqa: SLF001
        notifier._at_risk_now = True  # noqa: SLF001
        # The car's ceiling question is answered (past the repeat rule, as for its own notice); the bath still stands:
        # its own plan_at_risk is told now.
        frozen.tick(timedelta(minutes=16))
        world.car_blocks[entry.entry_id] = None
        world.tell()
        await hass.async_block_till_done()
        assert not notifier._fit_holds_back()  # noqa: SLF001
        assert [call.data["data"]["tag"] for call in calls if call.data.get("message") != "clear_notification"][-1] == tag
        assert notifier._at_risk_behind_target is None  # noqa: SLF001
