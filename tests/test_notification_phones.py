"""One notification section for the whole site, per phone (`notifications/phones.py`, `phone_store.py`,
`api/notification_phones.py`, spec `plans/v3_notifications_per_phone.md`): the migration from the per-charger and heat
choices, the routing (a phone's app or its Companion service, never both), the card's WebSocket commands, the app's
`phone_notifications` webhook action and the pairing's link by name."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.spotnav.api.pairing import PAIRING_WEBHOOK_ID, PairingRegister
from custom_components.spotnav.notifications import phone_store as phone_store_module
from custom_components.spotnav.notifications import push as push_module
from custom_components.spotnav.notifications.phones import (
    DEFAULT_PHONE_EVENTS,
    LEGACY_SHOWS,
    AppLink,
    LegacyCharger,
    LegacyHeat,
    Phone,
    PhoneError,
    ROUTE_APP,
    ROUTE_COMPANION,
    ROUTE_NONE,
    app_key,
    apply_charger_write,
    apply_push_register,
    charger_view,
    judge_changes,
    match_companion,
    migrate,
    route_of,
)
from custom_components.spotnav.notifications.settings import DEFAULT_EVENTS, NotificationSettings
from custom_components.spotnav.runtime import charger_data, domain_data

from .test_push import REF, FakeRelay, _pinned
from .world import setup_charger, ws_call

pytestmark = pytest.mark.usefixtures("offline_relay")

APP_ID = "aW5zdGFsbGVkLW9uY2UtcGVyLXBob25lLXNlY3JldA"
OTHER_APP_ID = "YW5vdGhlci1waG9uZXMtc2VjcmV0LWlkLWZvci10ZXN0"
REF2 = "ZmVkY2JhOTg3NjU0MzIxMDAxMjM0NTY3ODlhYmNkZWY"


def _ids() -> Any:
    counter = iter(range(1, 100))
    return lambda: f"ph_{next(counter):012x}"


# ------------------------------------------------------------------ the model


def test_a_new_phone_starts_with_the_defaults_and_off() -> None:
    phone = Phone(id="ph_000000000001", name="Pixel", notify="mobile_app_pixel")
    assert phone.events == DEFAULT_PHONE_EVENTS == (*DEFAULT_EVENTS, "pool_season")
    assert phone.on is False and phone.chargers is None
    assert not phone.wants("plan_stopped"), "off until someone turns it on"


def test_the_migration_follows_each_charger_with_the_union_of_its_events() -> None:
    chargers = [
        LegacyCharger("a", NotificationSettings(targets=("mobile_app_pixel",), events=("plan_stopped",))),
        LegacyCharger("b", NotificationSettings(targets=("mobile_app_pixel", "mobile_app_ipad"), events=("plugged_in",))),
    ]
    phones = migrate(chargers, [], {"mobile_app_pixel": "Pixel 8"}, ("a", "b", "c"), _ids())
    pixel, ipad = phones
    # Widened on purpose: the pixel now hears `plugged_in` from charger a too (nobody loses a notification).
    assert pixel.name == "Pixel 8" and pixel.on and pixel.events == ("plan_stopped", "plugged_in")
    assert pixel.chargers == ("a", "b")
    assert ipad.name == "ipad" and ipad.chargers == ("b",) and ipad.events == ("plugged_in",)


def test_the_migration_gives_every_charger_as_all_and_a_heat_only_phone_none() -> None:
    chargers = [
        LegacyCharger("a", NotificationSettings(targets=("mobile_app_pixel",), events=("plan_stopped",))),
        LegacyCharger("b", NotificationSettings(targets=("mobile_app_pixel",), events=("plan_stopped",))),
    ]
    heats = [LegacyHeat(("mobile_app_ipad", "mobile_app_pixel"), ("plan_at_risk", "pool_season"))]
    pixel, ipad = migrate(chargers, heats, {}, ("a", "b"), _ids())
    assert pixel.chargers is None, "it followed every charger: it follows them all, also one added later"
    assert pixel.events == ("plan_stopped", "plan_at_risk", "pool_season")
    assert ipad.chargers == () and ipad.events == ("plan_at_risk", "pool_season"), "only the heat loads"


def test_the_migration_makes_one_app_phone_per_wake_up_reference() -> None:
    chargers = [
        LegacyCharger("a", NotificationSettings(), REF, ("plan_stopped",)),
        LegacyCharger("b", NotificationSettings(), REF, ("charge_complete",)),
        LegacyCharger("c", NotificationSettings()),
    ]
    (app,) = migrate(chargers, [], {}, ("a", "b", "c"), _ids())
    assert app.app == AppLink(key=None, push_ref=REF, shows=LEGACY_SHOWS, via=("a", "b"))
    assert app.on and app.chargers == ("a", "b") and app.events == ("plan_stopped", "charge_complete")


def test_a_phone_with_both_hears_through_its_app_only_where_the_app_shows_it() -> None:
    both = Phone(
        id="ph_000000000001", name="Pixel", notify="mobile_app_pixel", app=AppLink(key=None, push_ref=REF), on=True
    )
    present = lambda _service: True  # noqa: E731
    assert route_of(both, ("plan_stopped",), present) == ROUTE_APP
    # An app before this release shows no bath: the spa's notice goes to Companion.
    assert route_of(both, ("heat_at_risk",), present) == ROUTE_COMPANION
    assert route_of(both, ("pool_season",), present) == ROUTE_COMPANION
    # Its instant notifications off: Companion; no Companion service either: nothing.
    unwoken = replace(both, app=AppLink(key=None, push_ref=None))
    assert route_of(unwoken, ("plan_stopped",), present) == ROUTE_COMPANION
    assert route_of(unwoken, ("plan_stopped",), lambda _service: False) == ROUTE_NONE


def test_an_older_clients_write_is_the_change_it_makes() -> None:
    every = ("a", "b")
    pixel = Phone(id="ph_000000000001", name="Pixel", notify="mobile_app_pixel", on=True, events=DEFAULT_PHONE_EVENTS)
    before = charger_view([pixel], "a", None)
    assert before.targets == ("mobile_app_pixel",) and before.events == DEFAULT_EVENTS

    def make(service: str) -> Phone:
        return Phone(id="ph_000000000009", name=service, notify=service)

    # Events changed: the remaining phone's charger events become them, its pool event kept.
    (changed,) = apply_charger_write(
        [pixel], "a", before, NotificationSettings(targets=("mobile_app_pixel",), events=("plugged_in",)), every, make
    )
    assert changed.events == ("plugged_in", "pool_season") and changed.chargers is None
    # A phone taken off follows the other charger only; a new one follows this one.
    out = apply_charger_write(
        [pixel], "a", before, NotificationSettings(targets=("mobile_app_ipad",), events=DEFAULT_EVENTS), every, make
    )
    assert out[0].chargers == ("b",) and out[0].on
    assert out[1].notify == "mobile_app_ipad" and out[1].on and out[1].chargers == ("a",)
    assert out[1].events == DEFAULT_EVENTS, "a client that knows the charger's events only chose those"


def test_an_older_apps_null_registration_clears_only_the_chargers_it_came_from() -> None:
    phones = apply_push_register([], "a", REF, ("plan_stopped",), ("a", "b"), _ids())
    phones = apply_push_register(phones, "b", REF, ("plan_stopped",), ("a", "b"), _ids())
    (app,) = phones
    assert app.app is not None and app.app.via == ("a", "b") and app.chargers is None
    (app,) = apply_push_register(phones, "a", None, (), ("a", "b"), _ids())
    assert app.app is not None and app.app.push_ref == REF and app.app.via == ("b",)
    (app,) = apply_push_register([app], "b", None, (), ("a", "b"), _ids())
    assert app.app is not None and app.app.push_ref is None


def test_a_change_is_judged_strictly() -> None:
    phone = Phone(id="ph_000000000001", name="Pixel", notify="mobile_app_pixel")
    assert judge_changes(phone, {"on": True, "chargers": ["a"]}, ("a", "b")).chargers == ("a",)
    assert judge_changes(phone, {"chargers": ["b", "a"]}, ("a", "b")).chargers is None
    for bad in ({"on": 1}, {"events": ["fire"]}, {"chargers": ["z"]}, {"chargers": ["a", "a"]}, {"name": "x"}, []):
        with pytest.raises(PhoneError):
            judge_changes(phone, bad, ("a", "b"))
    with pytest.raises(PhoneError):
        app_key("short")


# ------------------------------------------------------------------ the store, migrated at start


async def test_the_chargers_and_their_app_registration_migrate_once(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_charger(hass, title="Garage")
    store = domain_data(hass).auto_store
    await store.async_update(
        entry.entry_id,
        mutate=lambda settings: replace(
            settings, notifications=NotificationSettings(targets=("mobile_app_pixel",), events=("plan_stopped",))
        ),
    )
    hass_storage[f"spotnav.push.{entry.entry_id}"] = {
        "version": 1, "key": f"spotnav.push.{entry.entry_id}", "data": {"push_ref": REF, "events": ["charge_complete"]}
    }
    phones = domain_data(hass).phones
    phones._migrated = False  # noqa: SLF001 - as an installation from before this release starts
    await phones.async_migrate()
    companion, app = phones.phones
    assert companion.notify == "mobile_app_pixel" and companion.on and companion.events == ("plan_stopped",)
    assert app.app is not None and app.app.push_ref == REF and app.events == ("charge_complete",)
    assert hass_storage["spotnav.notification_phones"]["data"]["migrated"] is True
    # The charger's own record is left as it was, for a downgrade.
    assert store.settings(entry.entry_id).notifications.targets == ("mobile_app_pixel",)
    await phones.async_migrate()
    assert len(phones.phones) == 2, "once"


# ------------------------------------------------------------------ routing on a real charger


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> FakeRelay:
    fake = FakeRelay()
    monkeypatch.setattr(push_module, "async_get_clientsession", lambda _hass: fake)
    return fake


async def _charger(hass: HomeAssistant) -> Any:
    entry = await setup_charger(hass, title="Garage")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    return entry


async def test_a_phone_with_its_app_is_woken_and_never_also_told_on_companion(
    hass: HomeAssistant, relay: FakeRelay
) -> None:
    entry = await _charger(hass)
    calls = async_mock_service(hass, "notify", "mobile_app_pixel")
    phones = domain_data(hass).phones
    await phones.async_charger_write(entry.entry_id, NotificationSettings(targets=("mobile_app_pixel",), events=("charge_started",)))
    (pixel,) = phones.phones
    # The app on the same phone registers (an older app) and an administrator marks them as one phone.
    await charger_data(hass, entry.entry_id).push.async_register(push_module.PushRegistration(REF, ("charge_started",)))
    app = next(phone for phone in phones.phones if phone.app is not None)
    await phones.async_update(pixel.id, {"link": app.id})
    (one,) = phones.phones
    assert one.notify == "mobile_app_pixel" and one.app is not None and one.app.push_ref == REF

    hass.states.async_set("switch.charger_a", "on")
    await hass.async_block_till_done()
    assert relay.requests and calls == [], "woken through the app, not also through Companion"

    # The relay forgets the reference: from the next notice on the phone hears it on Companion.
    relay.status, relay.body = 404, {"error": "unknown_ref"}
    await charger_data(hass, entry.entry_id).push.async_send_test()
    await hass.async_block_till_done()
    (one,) = phones.phones
    assert one.app is not None and one.app.push_ref is None
    assert phones.recipients("charge_started", charger=entry.entry_id) == (("mobile_app_pixel",), ())


# ------------------------------------------------------------------ the card's commands


async def test_the_listing_shows_an_untouched_companion_phone_off_until_someone_turns_it_on(
    hass: HomeAssistant, hass_ws_client
) -> None:
    entry = await _charger(hass)
    MockConfigEntry(domain="mobile_app", data={"device_name": "Guest Pixel"}).add_to_hass(hass)
    async_mock_service(hass, "notify", "mobile_app_guest_pixel")
    client = await hass_ws_client(hass)
    answer = await ws_call(client, {"type": "spotnav/notification_phones", "api_version": 1})
    result = answer["result"]
    assert result["ok"] is True and result["loads"] == {"chargers": 1, "pool": False, "spa": False, "sauna": False}
    (guest,) = result["phones"]
    assert guest["id"] == "notify:mobile_app_guest_pixel" and guest["on"] is False
    assert guest["companion"] == {"service": "mobile_app_guest_pixel", "name": "Guest Pixel", "found": True}
    assert guest["events"] == list(DEFAULT_PHONE_EVENTS) and guest["route"] == "companion"
    assert result["chargers"] == [{"id": entry.entry_id, "name": "Garage"}]

    answer = await ws_call(
        client,
        {"type": "spotnav/update_notification_phone", "api_version": 1, "phone": guest["id"], "changes": {"on": True},
         "url": "/lovelace/home"},
    )
    (stored,) = answer["result"]["phones"]
    assert stored["on"] is True and stored["id"].startswith("ph_"), "stored on its first write"
    # The card's page is where a tap opens, for a charger that has no dashboard of its own.
    assert domain_data(hass).phones.url == "/lovelace/home"
    assert charger_data(hass, entry.entry_id).notifier.notifications_url == "/lovelace/home"
    assert domain_data(hass).phones.recipients("plan_stopped", charger=entry.entry_id)[0] == ("mobile_app_guest_pixel",)

    for phone, changes, code in (
        ("ph_000000000000", {"on": True}, "spotnav_unknown_phone"),
        (stored["id"], {"events": ["fire"]}, "spotnav_invalid_value"),
        (stored["id"], {"unlink": True}, "spotnav_invalid_value"),
        (stored["id"], {"on": False, "forget": True}, "spotnav_invalid_value"),
    ):
        answer = await ws_call(
            client, {"type": "spotnav/update_notification_phone", "api_version": 1, "phone": phone, "changes": changes}
        )
        assert answer["result"]["ok"] is False and answer["result"]["error"] == code

    answer = await ws_call(
        client,
        {"type": "spotnav/update_notification_phone", "api_version": 1, "phone": stored["id"], "changes": {"forget": True}},
    )
    (again,) = answer["result"]["phones"]
    assert again["id"] == "notify:mobile_app_guest_pixel" and again["on"] is False, "listed again, untouched"


async def test_only_an_administrator_reads_or_changes_the_phones(
    hass: HomeAssistant, hass_ws_client, hass_read_only_access_token: str
) -> None:
    await _charger(hass)
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    for payload in (
        {"type": "spotnav/notification_phones", "api_version": 1},
        {"type": "spotnav/update_notification_phone", "api_version": 1, "phone": "x", "changes": {"on": True}},
    ):
        answer = await ws_call(reader, payload)
        assert answer["result"]["ok"] is False and answer["result"]["error"] == "spotnav_not_admin"
        assert answer["result"]["phones"] == []


# ------------------------------------------------------------------ the app's own phone


async def _post(client: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = await client.post("/api/webhook/webhook-a", json={"version": 1, **payload})
    return response.status, await response.json()


async def test_the_app_reads_and_writes_only_its_own_phone(
    hass: HomeAssistant, hass_client_no_auth, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    ids = iter(("ph_00000000a001", "ph_00000000a002"))
    monkeypatch.setattr(phone_store_module, "new_phone_id", lambda: next(ids))
    entry = await _charger(hass)
    MockConfigEntry(domain="mobile_app", data={"device_name": "Pixel 8"}).add_to_hass(hass)
    async_mock_service(hass, "notify", "mobile_app_pixel_8")
    client = await hass_client_no_auth()

    # The first call makes its phone: on, the defaults, linked to the Companion phone of the same name.
    status, answer = await _post(client, {"action": "phone_notifications", "app_id": APP_ID, "name": "Pixel 8"})
    assert status == 200 and answer["ok"] is True and answer["action"] == "phone_notifications"
    phone = answer["phone"]
    assert phone["on"] is True and phone["events"] == list(DEFAULT_PHONE_EVENTS) and phone["chargers"] is None
    assert phone["companion"] == {"name": "Pixel 8"} and phone["instant"] is False
    assert answer["chargers"] == [{"id": entry.entry_id, "name": "Garage"}]
    _pinned("phone_notifications_success.json", answer)

    status, answer = await _post(
        client,
        {"action": "phone_notifications", "app_id": APP_ID, "push_ref": REF, "shows": ["plan_stopped"],
         "set": {"events": ["plan_stopped"]}},
    )
    assert status == 200 and answer["phone"]["events"] == ["plan_stopped"] and answer["phone"]["instant"] is True
    # Another app is another phone: it never sees nor changes this one.
    status, other = await _post(client, {"action": "phone_notifications", "app_id": OTHER_APP_ID, "set": {"on": False}})
    assert status == 200 and other["phone"]["id"] != phone["id"] and other["phone"]["on"] is False
    mine = domain_data(hass).phones.app_phone(app_key(APP_ID))
    assert mine is not None and mine.on and mine.events == ("plan_stopped",)
    # A phone that hears the event by its app is woken; its Companion service is left alone.
    assert domain_data(hass).phones.recipients("plan_stopped", charger=entry.entry_id) == ((), (REF,))

    for bad in ({"app_id": "short"}, {"app_id": APP_ID, "set": {"events": ["fire"]}}, {"app_id": APP_ID, "push_ref": "!"}):
        status, refused = await _post(client, {"action": "phone_notifications", **bad})
        assert status == 400 and refused == {"ok": False, "error": "invalid_phone_notifications", "action": "phone_notifications"}
    _pinned("phone_notifications_invalid.json", refused)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components"))
    assert APP_ID not in ours and REF not in ours, "the app's secret id and its reference are never logged"


async def test_an_updated_app_adopts_its_phone_from_before_by_its_reference(hass: HomeAssistant, hass_client_no_auth) -> None:
    entry = await _charger(hass)
    client = await hass_client_no_auth()
    status, _ = await _post(client, {"action": "push_register", "push_ref": REF2, "events": ["charge_complete"]})
    assert status == 200
    (before,) = domain_data(hass).phones.phones
    status, answer = await _post(client, {"action": "phone_notifications", "app_id": APP_ID, "push_ref": REF2})
    assert status == 200 and answer["phone"]["id"] == before.id
    (after,) = domain_data(hass).phones.phones
    assert after.app is not None and after.app.key == app_key(APP_ID) and after.app.via == ()
    assert "charge_complete" in after.events and after.follows(entry.entry_id)


async def test_a_pairing_with_an_app_id_makes_its_phone_linked_by_name(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _charger(hass)
    hass.config.external_url = "https://ha.example.com"
    domain_data(hass).pairing = PairingRegister(remote_interval_s=0.0)
    MockConfigEntry(domain="mobile_app", data={"device_name": "Pixel 8"}).add_to_hass(hass)
    async_mock_service(hass, "notify", "mobile_app_pixel_8")
    client = await hass_client_no_auth()
    response = await client.post(
        f"/api/webhook/{PAIRING_WEBHOOK_ID}",
        json={"version": 1, "action": "request", "code": "123456", "device": "Pixel 8", "app_id": APP_ID},
    )
    request_id = (await response.json())["request_id"]
    assert domain_data(hass).pairing.approve(request_id)
    response = await client.post(
        f"/api/webhook/{PAIRING_WEBHOOK_ID}", json={"version": 1, "action": "poll", "request_id": request_id}
    )
    assert (await response.json())["status"] == "approved"
    await hass.async_block_till_done()
    (phone,) = domain_data(hass).phones.phones
    assert phone.notify == "mobile_app_pixel_8" and phone.app is not None and phone.app.key == app_key(APP_ID)
    assert phone.on is True, "a pairing is an explicit act"


# ------------------------------------------------------------------ the app's phone found among the Companion phones


def test_the_app_finds_its_companion_phone_by_name_word_or_model_and_never_guesses() -> None:
    n6 = ("mobile_app_n6", ["N6"], "CPH2765")
    pixel = ("mobile_app_pixel_8", ["Pixel 8", "Anna's Pixel"], "Pixel 8")
    # The owner's phone: Android calls it "Henrik N6", Home Assistant "N6", the model is CPH2765.
    assert match_companion("Henrik N6", "CPH2765", [n6, pixel]) == "mobile_app_n6"
    assert match_companion("Henrik N6", None, [n6, pixel]) == "mobile_app_n6", "ends with the other as a word"
    assert match_companion("CPH2765", None, [n6, pixel]) == "mobile_app_n6", "an older app sends its model as its name"
    assert match_companion(" anna's  PIXEL ", None, [n6, pixel]) == "mobile_app_pixel_8", "a name a person gave it"
    assert match_companion("Kitchen tablet", "SM-X200", [n6, pixel]) is None
    assert match_companion("Henrik N16", None, [n6]) is None, "a word, not any ending"
    # Two phones of one model: no guess, unless one of them has the app's name.
    twins = [n6, ("mobile_app_anna_n6", ["Anna N6"], "CPH2765")]
    assert match_companion("Phone", "CPH2765", twins) is None
    assert match_companion("Anna N6", "CPH2765", twins) == "mobile_app_anna_n6"
    assert match_companion("Henrik N6", "CPH2765", [n6, ("mobile_app_henrik_n6_2", ["Henrik N6 2"], None)]) == "mobile_app_n6"
    assert match_companion(None, None, [n6]) is None


def _companion_phone(hass: HomeAssistant, device_name: str, model: str, *, name_by_user: str | None = None) -> None:
    entry = MockConfigEntry(domain="mobile_app", data={"device_name": device_name, "model": model, "manufacturer": "OPPO"})
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("mobile_app", entry.entry_id)},
        name=device_name,
        model=model,
        manufacturer="OPPO",
    )
    if name_by_user is not None:
        registry.async_update_device(device.id, name_by_user=name_by_user)
    async_mock_service(hass, "notify", f"mobile_app_{device_name.lower().replace(' ', '_')}")


async def test_the_owners_phone_links_on_a_later_call_and_a_split_stays_split(
    hass: HomeAssistant, hass_client_no_auth, hass_ws_client
) -> None:
    await _charger(hass)
    client = await hass_client_no_auth()
    # An app from before: its model as its name, while the Companion phone was not there yet.
    status, answer = await _post(client, {"action": "phone_notifications", "app_id": APP_ID, "name": "CPH2765"})
    assert status == 200 and answer["phone"]["companion"] is None
    _companion_phone(hass, "N6", "CPH2765")
    ws = await hass_ws_client(hass)
    listing = (await ws_call(ws, {"type": "spotnav/notification_phones", "api_version": 1}))["result"]["phones"]
    assert [phone["name"] for phone in listing] == ["CPH2765", "N6"], "two rows for one phone: what the owner saw"

    status, answer = await _post(
        client, {"action": "phone_notifications", "app_id": APP_ID, "name": "Henrik N6", "model": "CPH2765"}
    )
    assert status == 200 and answer["phone"]["companion"] == {"name": "N6"}
    (phone,) = domain_data(hass).phones.listing()
    assert phone.notify == "mobile_app_n6" and phone.name == "N6" and phone.on is True
    assert phone.app is not None and phone.app.key == app_key(APP_ID)

    # "Split up" sticks: no later call links them again.
    result = (
        await ws_call(
            ws, {"type": "spotnav/update_notification_phone", "api_version": 1, "phone": phone.id, "changes": {"unlink": True}}
        )
    )["result"]
    assert result["ok"] is True and len(result["phones"]) == 2
    for _ in range(2):
        status, answer = await _post(
            client, {"action": "phone_notifications", "app_id": APP_ID, "name": "Henrik N6", "model": "CPH2765"}
        )
        assert status == 200 and answer["phone"]["companion"] is None
    assert len(domain_data(hass).phones.listing()) == 2
    split = domain_data(hass).phones.app_phone(app_key(APP_ID))
    assert split is not None and split.apart is True and split.notify is None
    # Linked again by hand: one entry again.
    untouched = next(item for item in domain_data(hass).phones.listing() if item.notify == "mobile_app_n6")
    result = (
        await ws_call(
            ws,
            {"type": "spotnav/update_notification_phone", "api_version": 1, "phone": split.id,
             "changes": {"link": untouched.id}},
        )
    )["result"]
    assert result["ok"] is True and len(result["phones"]) == 1


async def test_two_companion_phones_of_one_model_link_neither(hass: HomeAssistant, hass_client_no_auth) -> None:
    await _charger(hass)
    _companion_phone(hass, "N6", "CPH2765")
    _companion_phone(hass, "Kids", "CPH2765")
    client = await hass_client_no_auth()
    status, answer = await _post(
        client, {"action": "phone_notifications", "app_id": APP_ID, "name": "Phone", "model": "CPH2765"}
    )
    assert status == 200 and answer["phone"]["companion"] is None
    assert len(domain_data(hass).phones.listing()) == 3


async def test_a_pairing_links_by_the_name_a_person_gave_the_companion_phone(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _charger(hass)
    hass.config.external_url = "https://ha.example.com"
    domain_data(hass).pairing = PairingRegister(remote_interval_s=0.0)
    _companion_phone(hass, "N6", "CPH2765", name_by_user="Henrik N6")
    _companion_phone(hass, "Kids", "CPH2765")
    client = await hass_client_no_auth()
    response = await client.post(
        f"/api/webhook/{PAIRING_WEBHOOK_ID}",
        json={"version": 1, "action": "request", "code": "123456", "device": "Henrik N6", "model": "CPH2765",
              "app_id": APP_ID},
    )
    request_id = (await response.json())["request_id"]
    assert domain_data(hass).pairing.approve(request_id)
    response = await client.post(
        f"/api/webhook/{PAIRING_WEBHOOK_ID}", json={"version": 1, "action": "poll", "request_id": request_id}
    )
    assert (await response.json())["status"] == "approved"
    await hass.async_block_till_done()
    (phone,) = domain_data(hass).phones.phones
    assert phone.notify == "mobile_app_n6" and phone.app is not None and phone.app.key == app_key(APP_ID)
