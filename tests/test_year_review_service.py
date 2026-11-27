"""`spotnav.show_year_review` (`services.py`, `year_review/runtime.py`): administrators only, the review answered,
offered for a week, the seen states forgotten unless asked not to, and its words in every language."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.runtime import site_data
from custom_components.spotnav.year_review.runtime import FORCED_FOR, SiteYearReview

UTC = timezone.utc
#: A Saturday in July 2026, 11:00 in Stockholm: outside the window.
JULY = datetime(2026, 7, 4, 9, 0, tzinfo=UTC)
PACKAGE = Path(__file__).parents[1] / "custom_components" / "spotnav"


async def _site(hass: HomeAssistant, freezer: Any) -> SiteYearReview:
    from tests.world import setup_charger_and_site

    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to(JULY)
    _charger, site = await setup_charger_and_site(hass, "entry_year")
    assert site is not None
    data = site_data(hass, site.entry_id)
    assert data is not None and data.year_review is not None
    return data.year_review


async def _show(hass: HomeAssistant, data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN, "show_year_review", data or {}, blocking=True, return_response=True, **kwargs
    )


async def test_the_service_answers_the_review_and_offers_it_for_a_week(hass: HomeAssistant, freezer: Any) -> None:
    review = await _site(hass, freezer)
    review.store.mark("anna", 2026, "viewed")
    response = await _show(hass)
    assert response["site_id"] == review.entry_id and response["year"] == 2026
    assert response["review"]["through"] == "2026-07-03"
    assert [page["id"] for page in response["review"]["pages"]][0] == "cover"
    assert datetime.fromisoformat(response["forced_until"]) == JULY + FORCED_FOR
    # Everyone sees the banner again, out of the window.
    assert review.answer("anna")["banner"] is True and review.answer("anna")["row"] is True
    freezer.tick(FORCED_FOR + timedelta(seconds=1))
    assert review.answer("anna")["row"] is False


async def test_without_reset_who_saw_it_keeps_it_seen_and_a_past_year_is_reviewed_whole(
    hass: HomeAssistant, freezer: Any
) -> None:
    review = await _site(hass, freezer)
    review.store.mark("anna", 2025, "dismissed")
    response = await _show(hass, {"year": 2025, "reset": False})
    assert response["year"] == 2025 and response["review"]["through"] == "2025-12-31"
    assert review.answer("anna")["year"] == 2025 and review.answer("anna")["banner"] is False
    assert review.answer("bo")["banner"] is True


async def test_a_year_with_no_day_behind_it_is_refused_and_named(hass: HomeAssistant, freezer: Any) -> None:
    await _site(hass, freezer)
    with pytest.raises(ServiceValidationError) as raised:
        await _show(hass, {"year": 2027})
    assert raised.value.translation_key == "year_review_no_year"
    assert raised.value.translation_placeholders == {"year": "2027"}


async def test_only_an_administrator_or_an_automation_may_show_it(
    hass: HomeAssistant, freezer: Any, hass_read_only_user: Any, hass_admin_user: Any
) -> None:
    await _site(hass, freezer)
    with pytest.raises(Unauthorized):
        await _show(hass, context=Context(user_id=hass_read_only_user.id))
    await _show(hass, context=Context(user_id=hass_admin_user.id))
    await _show(hass)


async def test_without_a_site_the_call_is_refused(hass: HomeAssistant) -> None:
    from tests.world import setup_charger_and_site

    await setup_charger_and_site(hass, "entry_alone", site=False)
    with pytest.raises(ServiceValidationError) as raised:
        await _show(hass)
    assert raised.value.translation_key == "export_no_site"


def test_the_service_s_fields_are_described_and_named_in_every_language() -> None:
    services = yaml.safe_load((PACKAGE / "services.yaml").read_text(encoding="utf-8"))
    assert set(services["show_year_review"]["fields"]) == {"site", "year", "reset"}
    icons = json.loads((PACKAGE / "icons.json").read_text(encoding="utf-8"))
    assert icons["services"]["show_year_review"]["service"].startswith("mdi:")
    for path in sorted((PACKAGE / "translations").glob("*.json")):
        language = json.loads(path.read_text(encoding="utf-8"))
        block = language["services"]["show_year_review"]
        assert set(block["fields"]) == {"site", "year", "reset"}, path.name
        assert "{year}" in language["exceptions"]["year_review_no_year"]["message"], path.name
        words = set(re.findall(r"\w+", json.dumps(block, ensure_ascii=False).lower()))
        assert not words & {"smart", "ai", "we", "us", "vi", "oss"}, path.name
