"""A planned charge that does not fit under the ceiling by its departure (`site/does_not_fit.py`): the block from the
import target's status (pure), and the charger's dashboard on a real site whose target makes the car miss its
departure (`tests/test_import_target_hold.py`'s tight night) with each `if_not_ready`.

The dashboard's blocks are the card's contract fixtures (`tests/fixtures/does_not_fit/ask.json`, `hold.json`,
`release.json`; `SPOTNAV_WRITE_FIXTURES=1` rewrites them)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.demand_cost import HourCeiling
from custom_components.spotnav.site.does_not_fit import (
    BLOCK_KEYS,
    CHOICE_KEYS,
    does_not_fit,
    peak_cost_minor,
    site_does_not_fit,
)
from custom_components.spotnav.site.site_capacity_controller import ImportTargetStatus, TargetRisk

from .test_import_target_hold import _HOUR, _tight_night

DEPARTURE = "2026-11-23T06:00:00+00:00"
FIXTURES = Path(__file__).parent / "fixtures" / "does_not_fit"


def _write_or_compare(name: str, block: dict) -> None:
    path = FIXTURES / name
    text = json.dumps(block, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text)


def _status(**changes) -> ImportTargetStatus:
    base = ImportTargetStatus(
        target_kw=6.0,
        set_kw=6.0,
        hour_kw=5.2,
        held_a=7.0,
        paused_until=None,
        released_for=None,
        house_over=False,
        release_until=DEPARTURE,
        release_peak_kw=12.0,
    )
    return replace(base, **changes)


def _risk(choice: str = "ask", need: float | None = 30.0) -> TargetRisk:
    return TargetRisk(departure=DEPARTURE, missing_kwh=18.04, target_kw=6.0, choice=choice, need_kwh=need)


def test_a_charge_that_fits_has_no_block() -> None:
    assert does_not_fit(None, ceiling="import_target") is None
    # Held this hour, but the departure is met: nothing to choose.
    assert does_not_fit(_status(), ceiling="import_target") is None


def test_a_short_charge_offers_every_choice() -> None:
    block = does_not_fit(_status(at_risk=_risk()), ceiling="import_target", peak_cost=4500.0)
    assert block is not None and tuple(block) == BLOCK_KEYS
    assert block["departure"] == DEPARTURE
    assert (block["short_kwh"], block["deliverable_kwh"]) == (18.0, 12.0)
    assert (block["ceiling"], block["ceiling_kw"], block["chosen"]) == ("import_target", 6.0, None)
    assert all(tuple(choice) == CHOICE_KEYS for choice in block["choices"])
    assert block["choices"] == [
        {"code": "takes_what_fits", "kwh": 12.0, "peak_kw": None, "cost": None},
        {"code": "later", "kwh": None, "peak_kw": None, "cost": None},
        {"code": "accept_peak", "kwh": None, "peak_kw": 12.0, "cost": 4500.0},
    ]


def test_hold_is_said_as_chosen_and_offers_the_rest() -> None:
    block = does_not_fit(_status(at_risk=_risk("hold", need=None)), ceiling="power_level")
    assert block is not None and block["chosen"] == "takes_what_fits" and block["ceiling"] == "power_level"
    assert block["deliverable_kwh"] is None
    assert [choice["code"] for choice in block["choices"]] == ["later", "accept_peak"]
    # No demand charge: the peak has no price.
    assert block["choices"][1]["cost"] is None


def test_a_set_aside_is_the_peak_accepted_with_nothing_left_to_choose() -> None:
    block = does_not_fit(_status(released_for=DEPARTURE, released_automatically=True), ceiling="import_target")
    assert block is not None and block["chosen"] == "accept_peak" and block["choices"] == []
    assert block["short_kwh"] is None and block["departure"] == DEPARTURE
    # A person's Start set aside until the unplug: no departure.
    unplug = does_not_fit(_status(released_for="until_unplug:2026-11-22T20:00:00+00:00"), ceiling="import_target")
    assert unplug is not None and unplug["departure"] is None


def _site(status: ImportTargetStatus | None, *, target: float | None = 6.0, hours: dict | None = None) -> SimpleNamespace:
    now = datetime(2026, 11, 22, 17, 20, tzinfo=timezone.utc)
    state = None if hours is None else SimpleNamespace(hours=hours)
    return SimpleNamespace(
        import_target_status=lambda _charger: status,
        config={} if target is None else {"import_target_kw": target},
        demand_ceiling=SimpleNamespace(state=state),
        demand=SimpleNamespace(now=lambda: now, tracker=SimpleNamespace(tz="Europe/Stockholm")),
    )


def test_a_shadow_ceiling_alone_shows_nothing() -> None:
    # A demand charge's soft ceiling is planned against in shadow only: with no hold this hour there is no block,
    # however far the car's charge is over it.
    hour = datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)
    assert site_does_not_fit(_site(None, target=None, hours={hour: HourCeiling(kw=5.0, price=45.0)}), "c") is None


def test_the_peak_is_priced_from_the_running_hours_soft_ceiling() -> None:
    hour = datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)
    site = _site(_status(at_risk=_risk()), hours={hour: HourCeiling(kw=5.0, price=45.0 / 3)})
    # 7 kW above the month's threshold at 15 kr/kW: 105 kr, in öre.
    assert peak_cost_minor(site, 12.0) == 10500
    block = site_does_not_fit(site, "c")
    assert block is not None and block["choices"][-1]["cost"] == 10500
    # No demand charge, or no figure for this hour: no price.
    assert peak_cost_minor(_site(None), 12.0) is None
    assert peak_cost_minor(_site(None, hours={}), 12.0) is None


async def _dashboard(hass: HomeAssistant, world) -> dict:
    from custom_components.spotnav.api import dashboard as dashboard_api

    return dashboard_api.serialize_dashboard(dashboard_api.capture_dashboard(hass, world.halo.charger), can_act=True)


async def test_the_dashboard_says_it_while_the_plan_asks(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "dnfask")
        await world.run(2)
        block = (await _dashboard(hass, world))["does_not_fit"]
        _write_or_compare("ask.json", block)
        assert block["chosen"] is None and block["ceiling"] == "import_target" and block["ceiling_kw"] == 6.0
        assert block["short_kwh"] > 0 and block["deliverable_kwh"] is not None
        assert block["departure"] == world.departure.astimezone(timezone.utc).isoformat()
        assert [choice["code"] for choice in block["choices"]] == ["takes_what_fits", "later", "accept_peak"]
        assert block["choices"][0]["kwh"] == block["deliverable_kwh"]


async def test_the_dashboard_says_hold_as_chosen(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "dnfhold", choice="hold")
        await world.run(10)
        block = (await _dashboard(hass, world))["does_not_fit"]
        _write_or_compare("hold.json", block)
        assert block["chosen"] == "takes_what_fits"
        assert [choice["code"] for choice in block["choices"]] == ["later", "accept_peak"]


async def test_the_dashboard_says_release_as_the_peak_accepted(hass: HomeAssistant, monkeypatch) -> None:
    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _tight_night(hass, monkeypatch, frozen, "dnfrelease", choice="release")
        await world.run(5)
        block = (await _dashboard(hass, world))["does_not_fit"]
        _write_or_compare("release.json", block)
        assert block["chosen"] == "accept_peak" and block["choices"] == []


async def test_a_charge_that_fits_has_no_dashboard_block(hass: HomeAssistant, monkeypatch) -> None:
    from .test_import_target_hold import _charging, _world

    with freeze_time(_HOUR + timedelta(seconds=5)) as frozen:
        world = await _world(hass, monkeypatch, frozen, "dnffits")
        await world.step()
        await _charging(world)
        await world.run(5)
        assert "does_not_fit" not in await _dashboard(hass, world)
