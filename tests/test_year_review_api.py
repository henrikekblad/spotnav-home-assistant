"""`spotnav/get_year_review` and `spotnav/year_review_seen` (`api/year_review.py`): each person's banner is their own
and goes for good, the Support row stays, nothing is offered out of the window unless the service forced it, and the
answer has exactly the contract's keys (the fixtures in `tests/fixtures/year_review/v1/`, `tests/test_year_review.py`)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.spotnav.runtime import site_data
from custom_components.spotnav.year_review.compute import ANSWER_KEYS, REVIEW_KEYS
from custom_components.spotnav.year_review.runtime import SiteYearReview

UTC = timezone.utc
DECEMBER = datetime(2026, 12, 15, 9, 0, tzinfo=UTC)
JULY = datetime(2026, 7, 4, 9, 0, tzinfo=UTC)


async def _site(hass: HomeAssistant) -> SiteYearReview:
    """The site, set up on the real clock (the sockets connect after it, before the clock moves)."""
    from tests.world import setup_charger_and_site

    await hass.config.async_set_time_zone("Europe/Stockholm")
    _charger, site = await setup_charger_and_site(hass, "entry_year")
    assert site is not None
    data = site_data(hass, site.entry_id)
    assert data is not None and data.year_review is not None
    return data.year_review


async def _ask(client: Any, kind: str, **fields: Any) -> dict[str, Any]:
    await client.send_json_auto_id({"type": f"spotnav/{kind}", "api_version": 1, **fields})
    return await client.receive_json()


async def test_each_person_s_banner_goes_when_they_open_or_close_it_and_the_row_stays(
    hass: HomeAssistant, freezer: Any, hass_ws_client: Any, hass_read_only_access_token: str
) -> None:
    review = await _site(hass)
    # Connected before the clock moves: the tokens are issued now.
    admin = await hass_ws_client(hass)
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    freezer.move_to(DECEMBER)
    before = await _ask(admin, "get_year_review", site_id=review.entry_id)
    assert before["success"] and before["result"]["state"] == "none" and before["result"]["review"] is None
    await review.async_run(2026)

    answer = (await _ask(admin, "get_year_review", charger_id="entry_year"))["result"]
    assert tuple(answer) == ANSWER_KEYS and tuple(answer["review"]) == REVIEW_KEYS
    assert answer["year"] == 2026 and answer["state"] == "ready" and answer["banner"] and answer["row"]

    seen = await _ask(admin, "year_review_seen", site_id=review.entry_id, year=2026, action="viewed")
    assert seen["result"] == {"ok": True}
    assert (await _ask(admin, "get_year_review", site_id=review.entry_id))["result"]["banner"] is False
    # Another person still has theirs, until they close it.
    assert (await _ask(reader, "get_year_review", site_id=review.entry_id))["result"]["banner"] is True
    await _ask(reader, "year_review_seen", site_id=review.entry_id, year=2026, action="dismissed")
    answer = (await _ask(reader, "get_year_review", site_id=review.entry_id))["result"]
    assert answer["banner"] is False and answer["row"] is True and answer["review"] is not None


async def test_out_of_the_window_nothing_is_offered_unless_the_service_forced_it(
    hass: HomeAssistant, freezer: Any, hass_ws_client: Any
) -> None:
    review = await _site(hass)
    client = await hass_ws_client(hass)
    freezer.move_to(JULY)
    await review.async_run(2026)
    answer = (await _ask(client, "get_year_review", site_id=review.entry_id))["result"]
    assert answer["state"] == "ready" and answer["banner"] is False and answer["row"] is False
    await review.async_show(None, reset=True)
    answer = (await _ask(client, "get_year_review", site_id=review.entry_id))["result"]
    assert answer["banner"] is True and answer["row"] is True


async def test_refusals_carry_their_codes(hass: HomeAssistant, freezer: Any, hass_ws_client: Any) -> None:
    review = await _site(hass)
    client = await hass_ws_client(hass)
    freezer.move_to(DECEMBER)
    await client.send_json_auto_id({"type": "spotnav/get_year_review", "api_version": 2, "site_id": review.entry_id})
    assert (await client.receive_json())["error"]["code"] == "spotnav_unsupported_api_version"
    assert (await _ask(client, "get_year_review", site_id="nope"))["error"]["code"] == "spotnav_unknown_site"
    bad = await _ask(client, "year_review_seen", site_id=review.entry_id, year=2026, action="liked")
    assert bad["error"]["code"] == "spotnav_invalid_request"
    bad = await _ask(client, "year_review_seen", site_id=review.entry_id, year="2026", action="viewed")
    assert bad["error"]["code"] == "spotnav_invalid_request"
    await hass.config_entries.async_unload(review.entry_id)
    await hass.async_block_till_done()
    assert (await _ask(client, "get_year_review", site_id=review.entry_id))["error"]["code"] == "spotnav_no_site"
