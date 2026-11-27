"""The year review over the webhook (`api/year_review.py`, the twins of `spotnav/get_year_review` and
`spotnav/year_review_seen`): the paired phone is the person, by its `app_id`, each phone's banner is its own and
apart from the card users', the same rules decide the banner and the Support row, and the answers are pinned under
`tests/fixtures/webhook/` (`SPOTNAV_WRITE_FIXTURES=1` rewrites them) for the app's tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_WEBHOOK_ID
from custom_components.spotnav.notifications.phones import app_key
from custom_components.spotnav.runtime import site_data
from custom_components.spotnav.year_review.compute import ANSWER_KEYS, REVIEW_KEYS
from custom_components.spotnav.year_review.runtime import SiteYearReview

from .test_notification_phones import APP_ID, OTHER_APP_ID
from .test_webhook_parity import _pinned, post
from .world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

UTC = timezone.utc
DECEMBER = datetime(2026, 12, 15, 9, 0, tzinfo=UTC)
JULY = datetime(2026, 7, 4, 9, 0, tzinfo=UTC)
START = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
HOOK = "webhook-entry_year"


async def _site(hass: HomeAssistant, freezer: Any) -> tuple[Any, SiteYearReview]:
    """The site, set up on a fixed day (the pinned review counts from it)."""
    freezer.move_to(START)
    await hass.config.async_set_time_zone("Europe/Stockholm")
    _charger, site = await setup_charger_and_site(hass, "entry_year")
    assert site is not None
    data = site_data(hass, site.entry_id)
    assert data is not None and data.year_review is not None
    return site, data.year_review


async def _get(client: Any, hook: str = HOOK, **fields: Any) -> tuple[int, dict[str, Any]]:
    return await post(client, hook, {"action": "get_year_review", **fields})


async def _seen(client: Any, hook: str = HOOK, **fields: Any) -> tuple[int, dict[str, Any]]:
    return await post(client, hook, {"action": "year_review_seen", **fields})


async def test_the_answer_is_the_commands_and_each_phone_s_banner_is_its_own(
    hass: HomeAssistant, freezer: Any, hass_client_no_auth: Any
) -> None:
    site, review = await _site(hass, freezer)
    client = await hass_client_no_auth()
    freezer.move_to(DECEMBER)
    await review.async_run(2026)

    status, answer = await _get(client, app_id=APP_ID)
    assert status == 200
    assert answer == {"ok": True, "action": "get_year_review", **review.answer(None)}
    assert tuple(k for k in answer if k not in ("ok", "action")) == ANSWER_KEYS
    assert tuple(answer["review"]) == REVIEW_KEYS
    assert answer["year"] == 2026 and answer["state"] == "ready" and answer["banner"] and answer["row"]
    _pinned("get_year_review_success.json", answer)
    # The site webhook answers the same for its own site.
    assert await _get(client, site.data[CONF_WEBHOOK_ID], app_id=APP_ID) == (200, answer)

    status, seen = await _seen(client, app_id=APP_ID, year=2026, seen_action="viewed")
    assert (status, seen) == (200, {"ok": True, "action": "year_review_seen"})
    _pinned("year_review_seen_success.json", seen)
    status, after = await _get(client, app_id=APP_ID)
    assert after["banner"] is False and after["row"] is True and after["review"] is not None
    _pinned("get_year_review_viewed.json", after)
    # Kept per phone, by its hashed id, apart from the card's users: another phone and every user still have theirs.
    assert review.store.phones == {app_key(APP_ID): {"year": 2026, "viewed": True, "dismissed": False}}
    assert review.store.users == {}
    assert APP_ID not in str(review.store._snapshot())
    assert (await _get(client, app_id=OTHER_APP_ID))[1]["banner"] is True
    assert (await _get(client))[1]["banner"] is True
    assert review.answer("anyone")["banner"] is True
    await _seen(client, site.data[CONF_WEBHOOK_ID], app_id=OTHER_APP_ID, year=2026, seen_action="dismissed")
    assert (await _get(client, app_id=OTHER_APP_ID))[1]["banner"] is False
    # And a user's state is never a phone's.
    review.store.mark("anna", 2026, "viewed")
    assert review.answer("anna")["banner"] is False
    assert review.answer(app_key(APP_ID))["banner"] is True


async def test_a_phone_that_saw_an_older_year_is_offered_this_one(
    hass: HomeAssistant, freezer: Any, hass_client_no_auth: Any
) -> None:
    _site_entry, review = await _site(hass, freezer)
    client = await hass_client_no_auth()
    freezer.move_to(DECEMBER)
    await review.async_run(2026)
    status, seen = await _seen(client, app_id=APP_ID, year=2025, seen_action="dismissed")
    assert status == 200 and seen["ok"] is True
    assert (await _get(client, app_id=APP_ID))[1]["banner"] is True
    await _seen(client, app_id=APP_ID, year=2026, seen_action="viewed")
    assert (await _get(client, app_id=APP_ID))[1]["banner"] is False
    assert review.store.phones[app_key(APP_ID)] == {"year": 2026, "viewed": True, "dismissed": False}


async def test_out_of_the_window_nothing_is_offered_unless_the_service_forced_it(
    hass: HomeAssistant, freezer: Any, hass_client_no_auth: Any
) -> None:
    _site_entry, review = await _site(hass, freezer)
    client = await hass_client_no_auth()
    freezer.move_to(JULY)
    await review.async_run(2026)
    status, answer = await _get(client, app_id=APP_ID)
    assert answer["state"] == "ready" and answer["banner"] is False and answer["row"] is False
    await _seen(client, app_id=APP_ID, year=2026, seen_action="viewed")
    # The service's showing forgets who saw it, the phones too.
    await review.async_show(None, reset=True)
    assert review.store.phones == {}
    status, answer = await _get(client, app_id=APP_ID)
    assert answer["banner"] is True and answer["row"] is True


async def test_the_seen_state_of_phones_is_kept(hass: HomeAssistant) -> None:
    from custom_components.spotnav.year_review.store import YearReviewStore

    store = YearReviewStore(hass, None)
    store.mark(app_key(APP_ID), 2026, "dismissed", phone=True)
    store.mark("anna", 2026, "viewed")
    again = YearReviewStore(hass, None)
    again.restore(store._snapshot())
    assert again.seen(app_key(APP_ID), 2026, phone=True) == (False, True)
    assert again.seen(app_key(APP_ID), 2026) == (False, False)
    assert again.seen("anna", 2026) == (True, False) and again.seen("anna", 2026, phone=True) == (False, False)
    # A store from before the phones reads as no phone having seen anything.
    old = {k: v for k, v in store._snapshot().items() if k != "phones"}
    older = YearReviewStore(hass, None)
    older.restore(old)
    assert older.phones == {} and older.seen("anna", 2026) == (True, False)


async def test_refusals_carry_their_codes(hass: HomeAssistant, freezer: Any, hass_client_no_auth: Any) -> None:
    site, review = await _site(hass, freezer)
    await setup_charger_and_site(hass, "entry_alone", site=False)
    client = await hass_client_no_auth()
    freezer.move_to(DECEMBER)
    for action in ("get_year_review", "year_review_seen"):
        status, body = await post(client, HOOK, {"action": action, "api_version": 2})
        assert (status, body) == (400, {"ok": False, "error": "spotnav_unsupported_api_version", "action": action})
        # A charger in no site has no review.
        status, body = await post(client, "webhook-entry_alone", {"action": action, "app_id": APP_ID})
        assert (status, body) == (400, {"ok": False, "error": "spotnav_no_site", "action": action})
        if action == "get_year_review":
            _pinned("get_year_review_no_site.json", body)
    for request in (
        {"year": 2026, "seen_action": "liked", "app_id": APP_ID},
        {"year": "2026", "seen_action": "viewed", "app_id": APP_ID},
        {"year": 2026, "seen_action": "viewed"},
        {"year": 2026, "seen_action": "viewed", "app_id": "short"},
        # `action` routes the body: the choice is `seen_action`.
        {"year": 2026, "app_id": APP_ID},
    ):
        status, body = await _seen(client, **request)
        assert (status, body) == (400, {"ok": False, "error": "spotnav_invalid_request", "action": "year_review_seen"})
    _pinned("year_review_seen_invalid.json", body)
    status, body = await _get(client, app_id="short")
    assert (status, body) == (400, {"ok": False, "error": "spotnav_invalid_request", "action": "get_year_review"})
    assert review.store.phones == {}
    # A body's `site_id` or `charger_id` is never read: the webhook is its own charger's site's.
    status, body = await _get(client, app_id=APP_ID, site_id="nope", charger_id="nope")
    assert status == 200 and body["site_id"] == site.entry_id
    assert await hass.config_entries.async_unload(site.entry_id)
    await hass.async_block_till_done()
    status, body = await _get(client, app_id=APP_ID)
    assert (status, body) == (400, {"ok": False, "error": "spotnav_no_site", "action": "get_year_review"})
