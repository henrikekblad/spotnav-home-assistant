"""The composed status block: a table over every precedence rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.status_compose import (
    STATUS_CODES,
    HybridFacts,
    LoadBalancingFacts,
    PlanningFacts,
    ProposalFacts,
    SiteMeasurementFacts,
    SocFacts,
    SolarFacts,
    StatusFacts,
    TargetFacts,
    compose_status,
)

from custom_components.spotnav.util import aware_iso

from .helpers import setup_two_chargers, webhook_dashboard

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)


def at(hours: float) -> datetime:
    return NOW + timedelta(hours=hours)


def iso(hours: float) -> str:
    return at(hours).isoformat()


def planning(state: str = "proposal_ready", reason: str = "ready", **kw: Any) -> PlanningFacts:
    return PlanningFacts(state=state, reason=reason, **kw)


def proposal(**kw: Any) -> ProposalFacts:
    defaults: dict[str, Any] = dict(
        periods=((at(2), at(3)), (at(3), at(4))),
        identity="p1",
        planned_kwh=20.0,
        requested_kwh=20.0,
        cost=12.5,
        currency="SEK",
        distance_mil=10.0,
    )
    defaults.update(kw)
    return ProposalFacts(**defaults)


def base(**kw: Any) -> StatusFacts:
    defaults: dict[str, Any] = dict(
        now=NOW, has_settings=True, strategy="cheapest", planning=planning(), price_state="ready", usable_price_rows=96
    )
    defaults.update(kw)
    return StatusFacts(**defaults)


def codes(block: dict[str, Any]) -> list[str]:
    return [line["code"] for line in block["lines"]]


PLANNED = [
    {"code": "auto_planned", "params": {"start": iso(2)}},
    {"code": "plan_energy", "params": {"kwh": 20.0}},
    {"code": "plan_cost", "params": {"amount_minor": 1250, "currency": "SEK"}},
    {"code": "plan_distance", "params": {"mil": 10.0}},
]

CASES: list[tuple[str, StatusFacts, str, list[dict[str, Any]]]] = [
    (
        "starting up: one line, whatever else is not yet known",
        base(charger_available=False, charger_problem="control_missing", strategy="hybrid", starting_up=True),
        "normal",
        [{"code": "starting_up", "params": {}}],
    ),
    ("idle: a plan that says nothing", base(relation_applied=True, proposal=proposal(planned_kwh=None, requested_kwh=None)), "normal", []),
    ("planned and applied", base(relation_applied=True, proposal=proposal()), "normal", PLANNED),
    (
        "planned without cost or distance",
        base(relation_applied=True, proposal=proposal(cost=None, distance_mil=None)),
        "normal",
        [PLANNED[0], PLANNED[1]],
    ),
    (
        "installed schedule only",
        base(installed_periods=((at(5), at(6)),)),
        "normal",
        [{"code": "auto_installed", "params": {"start": iso(5)}}],
    ),
    (
        "pending proposal beside an installed plan, queued for a boundary",
        base(
            relation_applied=False,
            pending_identity="p1",
            proposal=proposal(),
            installed_periods=((at(-1), at(-0.5)),),
        ),
        "normal",
        [
            {"code": "auto_installed", "params": {"start": iso(-1)}},
            {"code": "proposal_pending", "params": {"installs_at": None, "waits_for": None}},
        ],
    ),
    (
        "pending proposal while a window is charging: it installs when that window ends",
        base(
            charging=True,
            relation_applied=False,
            pending_identity="p1",
            proposal=proposal(),
            installed_periods=((at(-1), at(1)), (at(1), at(2))),
        ),
        "normal",
        [
            {"code": "charging_now", "params": {"until": iso(1)}},
            {"code": "plan_energy", "params": {"kwh": 20.0}},
            {"code": "plan_cost", "params": {"amount_minor": 1250, "currency": "SEK"}},
            {"code": "proposal_pending", "params": {"installs_at": iso(1), "waits_for": "window_end"}},
        ],
    ),
    (
        "proposal pending, not queued",
        base(relation_applied=False, proposal=proposal()),
        "normal",
        [{"code": "proposal_pending", "params": {"installs_at": None, "waits_for": None}}, *PLANNED[1:]],
    ),
    (
        "charging now inside a period",
        base(
            charging=True,
            installed_periods=((at(-1), at(1)),),
            relation_applied=True,
            proposal=proposal(),
        ),
        "normal",
        [
            {"code": "charging_now", "params": {"until": iso(1)}},
            PLANNED[1],
            PLANNED[2],
        ],
    ),
    (
        "charging with no period keeps no end",
        base(charging=True),
        "normal",
        [{"code": "charging_now", "params": {"until": None}}],
    ),
    (
        "paused with an end",
        base(paused=True, pause_until=at(10), pause_choice="until_tomorrow", relation_applied=True, proposal=proposal()),
        "normal",
        [{"code": "paused", "params": {"until": iso(10), "choice": "until_tomorrow", "action": None, "ends": None}}],
    ),
    (
        "paused until resumed, charging by hand",
        base(paused=True, charging=True, pause_choice="until_resumed"),
        "normal",
        [
            {"code": "paused", "params": {"until": None, "choice": "until_resumed", "action": None, "ends": None}},
            {"code": "charging_now", "params": {"until": None}},
        ],
    ),
    (
        "waiting for tomorrow's prices",
        base(waiting_for_tomorrow=True),
        "normal",
        [{"code": "waiting_for_tomorrow", "params": {}}],
    ),
    (
        "waiting for publication with a time",
        base(planning=planning("waiting_for_publication", "publication_pending", publication_at=at(15))),
        "normal",
        [{"code": "waiting_for_publication", "params": {"publication_at": iso(15)}}],
    ),
    (
        "waiting for publication past the expected time names no time: the prices may come any moment",
        base(planning=planning("waiting_for_publication", "publication_pending", publication_at=at(-0.5))),
        "normal",
        [{"code": "waiting_for_publication", "params": {"publication_at": None}}],
    ),
    (
        "waiting for publication at exactly the expected time names no time either",
        base(planning=planning("waiting_for_publication", "publication_pending", publication_at=at(0))),
        "normal",
        [{"code": "waiting_for_publication", "params": {"publication_at": None}}],
    ),
    (
        "waiting for publication without a time",
        base(planning=planning("waiting_for_publication", "publication_pending")),
        "normal",
        [{"code": "waiting_for_publication", "params": {"publication_at": None}}],
    ),
    (
        "waiting for history names the weekday, the saving and the weeks behind it",
        base(
            planning=planning(
                "waiting_for_publication",
                "waiting_for_history",
                publication_at=at(15),
                history_weekday=7,
                history_percent=30,
                history_weeks=4,
            )
        ),
        "normal",
        [{"code": "waiting_for_history", "params": {"weekday": 7, "percent": 30, "weeks": 4}}],
    ),
    (
        "buying before publication",
        base(planning=planning(reason="buying_before_publication", must_buy_now_kwh=4.5), proposal=proposal(), relation_applied=True),
        "normal",
        [{"code": "buying_before_publication", "params": {"kwh": 4.5}}],
    ),
    (
        "charging without prices",
        base(
            planning=planning("proposal_unpriced", "charging_without_prices"),
            proposal=proposal(unpriced=True),
            relation_applied=True,
        ),
        "notice",
        [
            {"code": "charging_without_prices", "params": {}},
            {"code": "plan_energy", "params": {"kwh": 20.0}},
        ],
    ),
    (
        "unpriced plan is a notice fact",
        base(relation_applied=True, proposal=proposal(unpriced=True)),
        "notice",
        [*PLANNED, {"code": "unpriced", "params": {}}],
    ),
    (
        "no plan at all",
        base(planning=planning("planning_unavailable", "no_prices_yet")),
        "normal",
        [{"code": "no_plan", "params": {}}],
    ),
    (
        "nothing to charge",
        base(planning=planning("nothing_to_charge", "already_at_target")),
        "normal",
        [{"code": "nothing_to_charge", "params": {}}],
    ),
    (
        "a target charge that delivered the need waits for the car to report its new level",
        base(planning=planning("nothing_to_charge", "waiting_for_vehicle_update")),
        "normal",
        [{"code": "waiting_for_vehicle_update", "params": {}}],
    ),
    (
        "a period installed with no settings record",
        base(has_settings=False, installed_periods=((at(1), at(2)),)),
        "normal",
        [{"code": "auto_installed", "params": {"start": iso(1)}}],
    ),
    (
        "solar charging",
        base(strategy="solar", solar=SolarFacts("on", "surplus", 9.0), planning=planning("planning_unavailable", "solar_running")),
        "normal",
        [{"code": "solar_charging", "params": {"requested_a": 9.0}}],
    ),
    ("solar arming", base(strategy="solar", solar=SolarFacts("arming")), "normal", [{"code": "solar_arming", "params": {}}]),
    (
        "solar off, the car ended the charge at its own limit",
        base(strategy="solar", solar=SolarFacts("off", "vehicle_full")),
        "normal",
        [{"code": "solar_vehicle_full", "params": {}}],
    ),
    (
        "solar off, the car stopped charging short of its limit and is tried again later",
        base(strategy="solar", solar=SolarFacts("off", "car_stopped", retry_at=NOW + timedelta(minutes=30))),
        "normal",
        [{"code": "solar_car_stopped", "params": {"time": (NOW + timedelta(minutes=30)).isoformat()}}],
    ),
    (
        "solar off, something else ended the charge: an ordinary off",
        base(strategy="solar", solar=SolarFacts("off", "charger_stopped")),
        "normal",
        [{"code": "solar_waiting_for_sun", "params": {}}],
    ),
    ("solar disarming", base(strategy="solar", solar=SolarFacts("disarming")), "normal", [{"code": "solar_disarming", "params": {}}]),
    (
        "solar off, no reading while stopped",
        base(strategy="solar", solar=SolarFacts("off", "no_basis_stopped")),
        "normal",
        [{"code": "solar_no_reading_stopped", "params": {}}],
    ),
    (
        "solar off, no reading yet",
        base(strategy="solar", solar=SolarFacts("off", "no_basis_off")),
        "normal",
        [{"code": "solar_no_reading_waiting", "params": {}}],
    ),
    (
        "solar off with no car while the surplus covers the start minimum: plug the car in",
        base(strategy="solar", solar=SolarFacts("off", "no_car", available_a=7.2, available_w=4968.0, start_a=6.0)),
        "normal",
        [{"code": "solar_no_car_surplus", "params": {"surplus_kw": 4.97}}],
    ),
    (
        "solar off with no car, the surplus exactly at the start minimum",
        base(strategy="solar", solar=SolarFacts("off", "no_car", available_a=6.0, available_w=1380.0, start_a=6.0)),
        "normal",
        [{"code": "solar_no_car_surplus", "params": {"surplus_kw": 1.38}}],
    ),
    (
        "solar off with no car and too little surplus to start: no car, not waiting for sun",
        base(strategy="solar", solar=SolarFacts("off", "no_car", available_a=3.0, available_w=690.0, start_a=6.0)),
        "normal",
        [{"code": "solar_no_car", "params": {}}],
    ),
    (
        "solar off with no car and no surplus reckoned yet",
        base(strategy="solar", solar=SolarFacts("off", "no_car")),
        "normal",
        [{"code": "solar_no_car", "params": {}}],
    ),
    (
        "solar off with no car and no basis: the last surplus is not trusted",
        base(
            strategy="solar",
            solar=SolarFacts(
                "off", "no_car", available_a=9.0, available_w=6210.0, start_a=6.0,
                basis_problem="grid_power_unreadable", basis_entity="sensor.grid",
            ),
        ),
        "notice",
        [
            {"code": "solar_no_car", "params": {}},
            {"code": "solar_no_grid_power", "params": {"entity": "sensor.grid", "entity_name": "sensor.grid"}},
        ],
    ),
    (
        "solar off, watching for sun",
        base(strategy="solar", solar=SolarFacts("off", "off_no_surplus")),
        "normal",
        [{"code": "solar_waiting_for_sun", "params": {}}],
    ),
    ("solar unknown", base(strategy="solar", solar=SolarFacts("unknown")), "normal", [{"code": "solar_unknown", "params": {}}]),
    (
        "solar cannot run on this site",
        base(strategy="solar", planning=planning("planning_unavailable", "solar_execution_unavailable"), solar=SolarFacts("unknown")),
        "blocking",
        [{"code": "solar_unavailable", "params": {}}],
    ),
    (
        "hybrid with a forecast and a window",
        base(
            strategy="hybrid",
            hybrid=HybridFacts("waiting_for_sun_within_slack", 12.0, 5.0, True),
            proposal=proposal(),
        ),
        "normal",
        [
            {
                "code": "hybrid_grid",
                "params": {"grid_kwh": 12.0, "credit_kwh": 5.0, "window_start": iso(2), "window_end": iso(4)},
            }
        ],
    ),
    (
        "hybrid, no credit, no window",
        base(strategy="hybrid", hybrid=HybridFacts("last_call_no_slack", 12.0, 0.0, True)),
        "normal",
        [{"code": "hybrid_grid", "params": {"grid_kwh": 12.0, "credit_kwh": None, "window_start": None, "window_end": None}}],
    ),
    (
        "hybrid without a forecast source",
        base(strategy="hybrid", hybrid=HybridFacts("no_forecast_cheapest", 20.0, 0.0, False)),
        "normal",
        [{"code": "hybrid_no_forecast", "params": {}}],
    ),
    ("hybrid waiting for price data", base(strategy="hybrid", hybrid=HybridFacts("no_price_data")), "normal", [{"code": "hybrid_no_price_data", "params": {}}]),
    ("hybrid need met", base(strategy="hybrid", hybrid=HybridFacts("satisfied_need_met", 0.0)), "normal", [{"code": "hybrid_satisfied", "params": {}}]),
    (
        "a person's Stop pauses Auto, the sun's too: the pause takes the hybrid headline",
        base(strategy="hybrid", hybrid=HybridFacts("satisfied_need_met", 0.0), paused=True,
             pause_choice="manual", pause_action="stop", pause_scope="plug_in"),
        "normal",
        [{"code": "paused", "params": {"until": None, "choice": "manual", "action": "stop", "ends": "unplug"}}],
    ),
    (
        "a person's Stop on a charger that cannot say when a car is plugged in: resuming Auto ends it",
        base(paused=True, pause_choice="manual", pause_action="stop", pause_scope="plug_in", reports_plug_in=False),
        "normal",
        [{"code": "paused", "params": {"until": None, "choice": "manual", "action": "stop", "ends": "resume"}}],
    ),
    (
        "a person's Stop with no car plugged in lasts through the next plug-in",
        base(paused=True, pause_choice="manual", pause_action="stop", pause_scope="next_plug_in"),
        "normal",
        [{"code": "paused", "params": {"until": None, "choice": "manual", "action": "stop", "ends": "next_plug_in"}}],
    ),
    (
        "a person's Start under solar: the pause headline, and the charge that runs",
        base(strategy="solar", solar=SolarFacts("off", "paused"), paused=True, charging=True,
             pause_choice="manual", pause_action="start", pause_scope="plug_in"),
        "normal",
        [
            {"code": "paused", "params": {"until": None, "choice": "manual", "action": "start", "ends": "unplug"}},
            {"code": "charging_now", "params": {"until": None}},
        ],
    ),
    (
        "hybrid waiting for tomorrow's prices keeps the wait line",
        base(
            strategy="hybrid",
            hybrid=HybridFacts("last_call_no_slack", 12.0, 0.0, True),
            planning=planning("waiting_for_publication", "publication_pending", publication_at=at(15)),
        ),
        "normal",
        [
            {"code": "hybrid_grid", "params": {"grid_kwh": 12.0, "credit_kwh": None, "window_start": None, "window_end": None}},
            {"code": "waiting_for_publication", "params": {"publication_at": iso(15)}},
        ],
    ),
    (
        "solar waiting for history keeps the wait line",
        base(
            strategy="solar",
            solar=SolarFacts("unknown"),
            planning=planning(
                "waiting_for_publication", "waiting_for_history", history_weekday=2, history_percent=12, history_weeks=4
            ),
        ),
        "normal",
        [
            {"code": "solar_unknown", "params": {}},
            {"code": "waiting_for_history", "params": {"weekday": 2, "percent": 12, "weeks": 4}},
        ],
    ),
    (
        "hybrid buying before publication keeps the buy line",
        base(
            strategy="hybrid",
            hybrid=HybridFacts(None, None),
            planning=planning("proposal_ready", "buying_before_publication", must_buy_now_kwh=3.5),
        ),
        "normal",
        [{"code": "hybrid_unknown", "params": {}}, {"code": "buying_before_publication", "params": {"kwh": 3.5}}],
    ),
    ("hybrid unknown", base(strategy="hybrid", hybrid=HybridFacts(None, None)), "normal", [{"code": "hybrid_unknown", "params": {}}]),
    (
        "target SoC: capacity missing",
        base(planning=planning("planning_unavailable", "target_capacity_unknown"), soc=SocFacts(("capacity",))),
        "blocking",
        [{"code": "target_soc_unknown", "params": {"missing": ["capacity"]}}],
    ),
    (
        "target SoC: reading missing",
        base(planning=planning("planning_unavailable", "target_soc_unknown"), soc=SocFacts(("soc",))),
        "blocking",
        [{"code": "target_soc_unknown", "params": {"missing": ["soc"]}}],
    ),
    (
        "target SoC: both missing",
        base(planning=planning("planning_unavailable", "target_soc_unknown"), soc=SocFacts(("soc", "capacity", "vehicle"))),
        "blocking",
        [{"code": "target_soc_unknown", "params": {"missing": ["soc", "capacity"]}}],
    ),
    (
        "target SoC: no soc block falls back to the reason",
        base(planning=planning("planning_unavailable", "target_capacity_unknown")),
        "blocking",
        [{"code": "target_soc_unknown", "params": {"missing": ["capacity"]}}],
    ),
    (
        "load balancing limits a charge in active mode",
        base(
            charging=True,
            load_balancing=LoadBalancingFacts("capacity_limited", True, 10.0, "L2"),
            load_balancing_capable=True,
        ),
        "notice",
        [
            {"code": "charging_now", "params": {"until": None}},
            {"code": "load_balancing_limited", "params": {"limit_a": 10.0, "phase": "L2", "cause": None}},
        ],
    ),
    (
        "load balancing names the battery sharing the fuse, even when the allocation itself is not limited",
        base(
            charging=True,
            load_balancing=LoadBalancingFacts(
                "below_minimum_current", True, 11.0, "L2", "battery_shares_fuse"
            ),
            load_balancing_capable=True,
        ),
        "notice",
        [
            {"code": "charging_now", "params": {"until": None}},
            {
                "code": "load_balancing_limited",
                "params": {"limit_a": 11.0, "phase": "L2", "cause": "battery_shares_fuse"},
            },
        ],
    ),
    (
        "load balancing names house consumption",
        base(
            charging=True,
            load_balancing=LoadBalancingFacts(
                "capacity_limited", True, 11.0, "L2", "house_consumption"
            ),
            load_balancing_capable=True,
        ),
        "notice",
        [
            {"code": "charging_now", "params": {"until": None}},
            {
                "code": "load_balancing_limited",
                "params": {"limit_a": 11.0, "phase": "L2", "cause": "house_consumption"},
            },
        ],
    ),
    (
        "load balancing while observing limits nothing",
        base(
            charging=True,
            load_balancing=LoadBalancingFacts("capacity_limited", False, 10.0, "L2"),
            load_balancing_capable=True,
        ),
        "normal",
        [{"code": "charging_now", "params": {"until": None}}],
    ),
    (
        "load balancing capable but no summary",
        base(load_balancing_capable=True, waiting_for_tomorrow=True),
        "notice",
        [{"code": "waiting_for_tomorrow", "params": {}}, {"code": "load_balancing_unavailable", "params": {}}],
    ),
    (
        "a charger whose own scheduler holds the charge says so, and only while it is not charging",
        base(held_by_charger=True, waiting_for_tomorrow=True),
        "notice",
        [{"code": "waiting_for_tomorrow", "params": {}}, {"code": "held_by_charger", "params": {}}],
    ),
    (
        "a charger whose own enable switch is off says so, and only while it is not charging",
        base(charger_disabled=True, waiting_for_tomorrow=True),
        "notice",
        [{"code": "waiting_for_tomorrow", "params": {}}, {"code": "charger_disabled", "params": {}}],
    ),
    (
        "a charger that kept charging after the stops sent under a person's Stop is a blocking line, then the charge",
        base(ignores_person_stop=True, charging=True, paused=True, pause_choice="manual", pause_action="stop"),
        "blocking",
        [{"code": "charger_ignores_stop", "params": {}}, {"code": "charging_now", "params": {"until": None}}],
    ),
    (
        "a charge that is running is not disabled, whatever a stale switch says",
        base(charger_disabled=True, charging=True),
        "normal",
        [{"code": "charging_now", "params": {"until": None}}],
    ),
    (
        "a charge that is running is not held, whatever a stale status says",
        base(held_by_charger=True, charging=True),
        "normal",
        [{"code": "charging_now", "params": {"until": None}}],
    ),
    (
        "stale prices are a notice fact",
        base(price_state="stale", price_reason="fetch_failed", waiting_for_tomorrow=True),
        "notice",
        [{"code": "waiting_for_tomorrow", "params": {}}, {"code": "price_data_stale", "params": {"reason": "fetch_failed"}}],
    ),
    (
        "unavailable prices with usable rows are a notice fact",
        base(price_state="unavailable", price_reason="x", usable_price_rows=4, waiting_for_tomorrow=True),
        "notice",
        [{"code": "waiting_for_tomorrow", "params": {}}, {"code": "price_data_degraded", "params": {"reason": "x"}}],
    ),
    (
        "settings missing",
        base(planning=planning("incomplete_settings", "settings_missing", missing=("area", "amps"))),
        "notice",
        [{"code": "settings_incomplete", "params": {"reason": "settings_missing", "missing": ["area", "amps"]}}],
    ),
    (
        "area unknown",
        base(planning=planning("incomplete_settings", "area_unknown")),
        "notice",
        [{"code": "settings_incomplete", "params": {"reason": "area_unknown", "missing": []}}],
    ),
    (
        "area unknown as a refusal",
        base(planning=planning("planning_unavailable", "area_unknown")),
        "blocking",
        [{"code": "planning_unavailable", "params": {"reason": "area_unknown"}}],
    ),
    (
        "price horizon missing",
        base(planning=planning("planning_unavailable", "insufficient_price_horizon")),
        "blocking",
        [{"code": "price_horizon_missing", "params": {}}],
    ),
    (
        "price horizon while waiting for tomorrow is normal life",
        base(
            planning=planning("planning_unavailable", "insufficient_price_horizon"),
            waiting_for_tomorrow=True,
            installed_periods=((at(1), at(2)),),
            departure_enabled=True,
        ),
        "normal",
        [{"code": "auto_installed", "params": {"start": iso(1)}}],
    ),
    (
        "price horizon with a deadline and no unfinished plan stays blocking",
        base(
            planning=planning("planning_unavailable", "insufficient_price_horizon"),
            waiting_for_tomorrow=True,
            departure_enabled=True,
        ),
        "blocking",
        [{"code": "price_horizon_missing", "params": {}}],
    ),
    (
        "planning error",
        base(planning=planning("error", "unexpected_failure")),
        "blocking",
        [{"code": "planning_error", "params": {"reason": "unexpected_failure"}}],
    ),
    (
        "charger unavailable and invalid prices, both said",
        base(charger_available=False, price_state="invalid", price_reason="bad"),
        "blocking",
        [
            {"code": "charger_unavailable", "params": {"problem": None, "entity": None}},
            {"code": "price_data_invalid", "params": {"reason": "bad"}},
        ],
    ),
    (
        "no usable price at all",
        base(price_state="unavailable", price_reason="none", usable_price_rows=0),
        "blocking",
        [{"code": "price_data_unavailable", "params": {"reason": "none"}}],
    ),
    (
        "blocking and drawing current, no period",
        base(charging=True, planning=planning("incomplete_settings", "settings_missing")),
        "notice",
        [
            {"code": "settings_incomplete", "params": {"reason": "settings_missing", "missing": []}},
            {"code": "charging_now", "params": {"until": None}},
        ],
    ),
    (
        "blocking and drawing current inside an installed period",
        base(
            charging=True,
            installed_periods=((at(-1), at(1)),),
            planning=planning("planning_unavailable", "area_unknown"),
        ),
        "blocking",
        [
            {"code": "planning_unavailable", "params": {"reason": "area_unknown"}},
            {"code": "charging_now", "params": {"until": iso(1)}},
        ],
    ),
    (
        "blocking and idle shows no charging line",
        base(planning=planning("planning_unavailable", "area_unknown")),
        "blocking",
        [{"code": "planning_unavailable", "params": {"reason": "area_unknown"}}],
    ),
    (
        "a reading-based stop follows the headline as a fact",
        base(planning=planning("nothing_to_charge", "already_at_target"), target=TargetFacts(81.4, "reading", 12.0)),
        "normal",
        [
            {"code": "nothing_to_charge", "params": {}},
            {"code": "target_reached", "params": {"soc_percent": 81.4, "basis": "reading", "reading_age_s": 12}},
        ],
    ),
    (
        "an estimate-based stop names its basis and its anchor's age, before the notices",
        base(
            planning=planning("nothing_to_charge", "already_at_target"),
            price_state="stale",
            target=TargetFacts(81.0, "estimate", 1800.4),
        ),
        "notice",
        [
            {"code": "nothing_to_charge", "params": {}},
            {"code": "target_reached", "params": {"soc_percent": 81.0, "basis": "estimate", "reading_age_s": 1800}},
            {"code": "price_data_stale", "params": {"reason": None}},
        ],
    ),
    (
        "a stop record without an age is still worded",
        base(planning=planning("nothing_to_charge", "already_at_target"), target=TargetFacts(80.0, None, None)),
        "normal",
        [
            {"code": "nothing_to_charge", "params": {}},
            {"code": "target_reached", "params": {"soc_percent": 80.0, "basis": "reading", "reading_age_s": None}},
        ],
    ),
    (
        "a stop record without a percent says nothing",
        base(planning=planning("nothing_to_charge", "already_at_target"), target=TargetFacts(None, "reading", 5.0)),
        "normal",
        [{"code": "nothing_to_charge", "params": {}}],
    ),
    (
        "a target that cannot be checked is a notice beside the headline",
        base(relation_applied=True, proposal=proposal(), target=TargetFacts(unverifiable_reason="reading_unusable")),
        "notice",
        [*PLANNED, {"code": "target_unverifiable", "params": {"reason": "reading_unusable"}}],
    ),
    (
        "a stop record wins over an unverifiable reason",
        base(planning=planning("nothing_to_charge", "already_at_target"), target=TargetFacts(80.0, "reading", 1.0, "no_source")),
        "normal",
        [
            {"code": "nothing_to_charge", "params": {}},
            {"code": "target_reached", "params": {"soc_percent": 80.0, "basis": "reading", "reading_age_s": 1}},
        ],
    ),
    (
        "blocking hides the target facts",
        base(planning=planning("error", "boom"), target=TargetFacts(81.0, "reading", 1.0)),
        "blocking",
        [{"code": "planning_error", "params": {"reason": "boom"}}],
    ),
    (
        "settings missing beside a price fault keeps the stronger tone",
        base(
            planning=planning("incomplete_settings", "settings_missing", missing=("area",)),
            price_state="invalid",
            price_reason="bad",
        ),
        "blocking",
        [
            {"code": "price_data_invalid", "params": {"reason": "bad"}},
            {"code": "settings_incomplete", "params": {"reason": "settings_missing", "missing": ["area"]}},
        ],
    ),
    (
        "first-run defaults are one neutral line after the headline",
        base(suggested=("area", "phases", "amps"), waiting_for_tomorrow=True),
        "normal",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "settings_suggested", "params": {"fields": ["area", "phases", "amps"]}},
        ],
    ),
    (
        "first-run defaults are not said while settings are still missing",
        base(
            suggested=("phases",),
            planning=planning("incomplete_settings", "settings_missing", missing=("area",)),
        ),
        "notice",
        [{"code": "settings_incomplete", "params": {"reason": "settings_missing", "missing": ["area"]}}],
    ),
    (
        "a car waiting for the next window says when charging starts, ahead of the plan's own sentence",
        base(hold_until=at(2), installed_periods=((at(2), at(3)),)),
        "normal",
        [{"code": "held_until_window", "params": {"time": iso(2)}}],
    ),
    (
        "a charge that runs is never held, whatever the hold says",
        base(hold_until=at(2), charging=True),
        "normal",
        [{"code": "charging_now", "params": {"until": None}}],
    ),
    (
        "a person's override of the hold is a notice beside the charge",
        base(hold_overridden=True, charging=True),
        "notice",
        [{"code": "charging_now", "params": {"until": None}}, {"code": "hold_overridden", "params": {}}],
    ),
    (
        "a manual need counted without the register says how, beside the plan",
        base(
            waiting_for_tomorrow=True,
            planning=PlanningFacts(
                state="waiting_for_prices", reason="no_prices_yet", energy_basis="kept", remaining_kwh=6.46
            ),
        ),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "remaining_need_estimated", "params": {"kwh": 6.5, "basis": "kept"}},
        ],
    ),
    (
        "a manual need capped at the battery's room says so beside the plan",
        base(
            waiting_for_tomorrow=True,
            planning=PlanningFacts(
                state="waiting_for_prices", reason="no_prices_yet", energy_basis="register", remaining_kwh=43.5,
                room_kwh=3.44,
            ),
        ),
        "normal",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "need_limited_by_room", "params": {"kwh": 3.4}},
        ],
    ),
    (
        "fill says the battery is charged until full, with the room now, and no estimate beside it",
        base(
            waiting_for_tomorrow=True,
            planning=PlanningFacts(
                state="waiting_for_prices", reason="no_prices_yet", energy_basis="kept", remaining_kwh=9.47,
                room_kwh=9.47, fill="battery",
            ),
        ),
        "normal",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "filling_to_limit", "params": {"kwh": 9.5}},
        ],
    ),
    (
        "fill without a known room says the stored amount is planned instead",
        base(
            waiting_for_tomorrow=True,
            planning=PlanningFacts(
                state="waiting_for_prices", reason="no_prices_yet", energy_basis="register", remaining_kwh=12.0,
                fill="unknown_room",
            ),
        ),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "fill_room_unknown", "params": {"kwh": 12.0}},
        ],
    ),
    (
        "a full battery says nothing about a cap of nothing",
        base(
            planning=PlanningFacts(state="nothing_to_charge", reason="already_at_target", room_kwh=0.0),
        ),
        "normal",
        [{"code": "nothing_to_charge", "params": {}}],
    ),
    (
        "a charge to the car's own limit says the car ends it",
        base(charging=True, vehicle_limit_percent=100.0),
        "normal",
        [
            {"code": "charging_now", "params": {"until": None}},
            {"code": "charging_to_vehicle_limit", "params": {"percent": 100}},
        ],
    ),
    (
        "a top-off past the last window says the car charges until it is full, and that it ends it",
        base(charging=True, vehicle_limit_percent=100.0, top_off_until=NOW + timedelta(minutes=45)),
        "normal",
        [
            {"code": "topping_off", "params": {"until": aware_iso(NOW + timedelta(minutes=45))}},
            {"code": "charging_to_vehicle_limit", "params": {"percent": 100}},
        ],
    ),
    (
        "a top-off beside a strategy headline is a fact line after the car's own limit",
        base(
            charging=True,
            vehicle_limit_percent=80.0,
            top_off_until=NOW + timedelta(minutes=30),
            hybrid=HybridFacts(reason="satisfied_need_met"),
        ),
        "normal",
        [
            {"code": "hybrid_satisfied", "params": {}},
            {"code": "charging_to_vehicle_limit", "params": {"percent": 80}},
            {"code": "topping_off", "params": {"until": aware_iso(NOW + timedelta(minutes=30))}},
        ],
    ),
    (
        "a pause says nothing about the car's own limit",
        base(paused=True, vehicle_limit_percent=90.0),
        "normal",
        [{"code": "paused", "params": {"until": None, "choice": None, "action": None, "ends": None}}],
    ),
    (
        "a site measurement with phases that read nothing names them and their entities",
        base(
            waiting_for_tomorrow=True,
            site_measurement=SiteMeasurementFacts(
                no_value_phases=("L2", "L3"),
                no_value_entities=("sensor.pulse_current_l2", "sensor.pulse_current_l3"),
                max_age_s=120.0,
            ),
        ),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {
                "code": "site_measurement_problem",
                "params": {
                    "no_value_phases": ["L2", "L3"],
                    "no_value_entities": ["sensor.pulse_current_l2", "sensor.pulse_current_l3"],
                    "stale_phases": [],
                    "max_age_s": 120.0,
                },
            },
        ],
    ),
    (
        "a stale phase says it is older than the maximum age",
        base(
            waiting_for_tomorrow=True,
            site_measurement=SiteMeasurementFacts(stale_phases=("L1",), max_age_s=120.0),
        ),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {
                "code": "site_measurement_problem",
                "params": {"no_value_phases": [], "no_value_entities": [], "stale_phases": ["L1"], "max_age_s": 120.0},
            },
        ],
    ),
    (
        "an inverter's sensors gone unavailable together are named as the meter in standby",
        base(
            waiting_for_tomorrow=True,
            site_measurement=SiteMeasurementFacts(
                no_value_phases=("L1",),
                no_value_entities=("sensor.solax_grid_current_l1",),
                max_age_s=120.0,
                unavailable_entities=("sensor.solax_grid_current_l1",),
                inverter=True,
            ),
        ),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {
                "code": "site_meter_unavailable",
                "params": {
                    "entities": ["sensor.solax_grid_current_l1"],
                    "cause": "inverter_standby",
                    "entity_names": ["sensor.solax_grid_current_l1"],
                },
            },
        ],
    ),
    (
        "a meter that reports export as a negative current on a site reading it unsigned names the fix",
        base(
            waiting_for_tomorrow=True,
            site_measurement=SiteMeasurementFacts(
                no_value_phases=("L2", "L3"),
                no_value_entities=("sensor.pulse_l2", "sensor.pulse_l3"),
                max_age_s=120.0,
                negative_phases=("L2", "L3"),
            ),
        ),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "site_current_negative", "params": {"phases": ["L2", "L3"]}},
        ],
    ),
    (
        "a meter whose every phase is unavailable says so without guessing at an inverter",
        base(
            waiting_for_tomorrow=True,
            site_measurement=SiteMeasurementFacts(
                no_value_phases=("L1", "L2", "L3"),
                no_value_entities=("sensor.meter",),
                unavailable_entities=("sensor.meter",),
            ),
        ),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "site_meter_unavailable", "params": {"entities": ["sensor.meter"], "cause": "meter_unavailable", "entity_names": ["sensor.meter"]}},
        ],
    ),
    (
        "solar off for want of a total grid power says to set it, in place of no usable reading",
        base(strategy="solar", solar=SolarFacts("off", "no_basis_off", basis_problem="grid_power_not_set")),
        "notice",
        [{"code": "solar_no_grid_power", "params": {"entity": None, "entity_name": None}}],
    ),
    (
        "solar off for an unreadable total grid power names its entity",
        base(
            strategy="solar",
            solar=SolarFacts(
                "off", "no_basis_stopped", basis_problem="grid_power_unreadable", basis_entity="sensor.net"
            ),
        ),
        "notice",
        [{"code": "solar_no_grid_power", "params": {"entity": "sensor.net", "entity_name": "sensor.net"}}],
    ),
    (
        "solar holding on in its grace with an unreadable battery keeps its headline and says why",
        base(
            strategy="solar",
            solar=SolarFacts(
                "on", "no_basis_grace", requested_a=6.0, basis_problem="battery_unreadable", basis_entity="sensor.bat"
            ),
        ),
        "notice",
        [
            {"code": "solar_charging", "params": {"requested_a": 6.0}},
            {"code": "solar_battery_unreadable", "params": {"entity": "sensor.bat", "entity_name": "sensor.bat"}},
        ],
    ),
    (
        "solar without the charger's own current says to choose it, and the site phases it runs without",
        base(
            strategy="solar",
            solar=SolarFacts(
                "off", "charger_measurement_missing", charger_current="not_set", site_incomplete_phases=("L1",)
            ),
        ),
        "notice",
        [
            {"code": "solar_waiting_for_sun", "params": {}},
            {"code": "solar_charger_current_missing", "params": {"entity": None, "entity_name": None}},
            {"code": "solar_site_incomplete", "params": {"phases": ["L1"]}},
        ],
    ),
    (
        "solar with an unreadable charger current names its entity",
        base(
            strategy="solar",
            charging=True,
            solar=SolarFacts(
                "on",
                "charger_measurement_missing",
                requested_a=6.0,
                charger_current="unreadable",
                charger_current_entity="sensor.easee_current",
            ),
        ),
        "notice",
        [
            {"code": "solar_charging", "params": {"requested_a": 6.0}},
            {
                "code": "solar_charger_current_missing",
                "params": {"entity": "sensor.easee_current", "entity_name": "sensor.easee_current"},
            },
        ],
    ),
    (
        "an unreadable charger current while the charger is not charging says nothing",
        base(
            strategy="solar",
            solar=SolarFacts(
                "off", None, charger_current="unreadable", charger_current_entity="sensor.halo_current"
            ),
        ),
        "normal",
        [{"code": "solar_waiting_for_sun", "params": {}}],
    ),
    (
        "an unreadable charger current with a start pending is named by its friendly name",
        base(
            strategy="solar",
            solar=SolarFacts(
                "arming",
                None,
                charger_current="unreadable",
                charger_current_entity="sensor.halo_current",
                charger_current_entity_name="HALO current",
            ),
        ),
        "notice",
        [
            {"code": "solar_arming", "params": {}},
            {
                "code": "solar_charger_current_missing",
                "params": {"entity": "sensor.halo_current", "entity_name": "HALO current"},
            },
        ],
    ),
    (
        "a charger that is the same physical charger as another entry says which",
        base(waiting_for_tomorrow=True, duplicate_chargers=("Garage Easee",)),
        "notice",
        [
            {"code": "waiting_for_tomorrow", "params": {}},
            {"code": "duplicate_charger", "params": {"other": "Garage Easee"}},
        ],
    ),
    (
        "a target plan short of its departure charges now and warns what the car reaches by then",
        base(
            charging=True,
            installed_periods=((at(-0.5), at(1.5)),),
            relation_applied=True,
            planning=planning(departure_at=at(1.5), expected_soc_percent=44.4615),
            proposal=proposal(
                periods=((at(-0.5), at(1.5)),), planned_kwh=7.36, requested_kwh=11.7867, short_of_deadline=True
            ),
        ),
        "notice",
        [
            {"code": "charging_now", "params": {"until": iso(1.5)}},
            {"code": "plan_energy", "params": {"kwh": 7.36}},
            {"code": "plan_cost", "params": {"amount_minor": 1250, "currency": "SEK"}},
            {
                "code": "departure_shortfall",
                "params": {"kwh": 7.36, "requested_kwh": 11.79, "soc_percent": 44.5, "departure": iso(1.5)},
            },
        ],
    ),
    (
        "a manual need short of its departure says the energy, with no state of charge",
        base(
            relation_applied=True,
            planning=planning(),
            proposal=proposal(periods=((at(2), at(4)),), planned_kwh=7.36, requested_kwh=12.0, short_of_deadline=True),
        ),
        "notice",
        [
            {"code": "auto_planned", "params": {"start": iso(2)}},
            {"code": "plan_energy", "params": {"kwh": 7.36}},
            {"code": "plan_cost", "params": {"amount_minor": 1250, "currency": "SEK"}},
            {"code": "plan_distance", "params": {"mil": 10.0}},
            {
                "code": "departure_shortfall",
                "params": {"kwh": 7.36, "requested_kwh": 12.0, "soc_percent": None, "departure": iso(4)},
            },
        ],
    ),
    (
        "identifying the car: its line goes ahead of the plan",
        base(relation_applied=True, proposal=proposal(), identification="waiting"),
        "normal",
        [{"code": "identifying_vehicle", "params": {}}, *PLANNED],
    ),
    (
        "asking which car: its line goes ahead of a pause",
        base(paused=True, identification="asking"),
        "normal",
        [
            {"code": "asking_vehicle", "params": {}},
            {"code": "paused", "params": {"until": None, "choice": None, "action": None, "ends": None}},
        ],
    ),
    (
        "the car's minimum charge level charges it: its line leads in place of the plan's",
        base(relation_applied=True, proposal=proposal(), charging=True, min_soc_percent=30.0),
        "normal",
        [{"code": "min_soc_charging", "params": {"percent": 30}}],
    ),
    (
        "under solar the floor's line leads in place of the sun's",
        base(strategy="solar", charging=True, min_soc_percent=45.0,
             solar=SolarFacts(state="on", reason="surplus", requested_a=6)),
        "normal",
        [{"code": "min_soc_charging", "params": {"percent": 45}}],
    ),
    (
        "a pause wins over the floor",
        base(paused=True, min_soc_percent=30.0),
        "normal",
        [{"code": "paused", "params": {"until": None, "choice": None, "action": None, "ends": None}}],
    ),
    (
        "a decided car says nothing",
        base(relation_applied=True, proposal=proposal(), identification="decided"),
        "normal",
        PLANNED,
    ),
    (
        "a blocking block stays alone while the car is identified",
        base(identification="asking", planning=planning("incomplete_settings", "settings_missing")),
        "notice",
        [{"code": "settings_incomplete", "params": {"reason": "settings_missing", "missing": []}}],
    ),
    (
        "starting up stays alone while the car is identified",
        base(starting_up=True, identification="waiting"),
        "normal",
        [{"code": "starting_up", "params": {}}],
    ),
    (
        "blocking beats a pause and a charge",
        base(paused=True, charging=True, planning=planning("incomplete_settings", "settings_missing")),
        "notice",
        [
            {"code": "settings_incomplete", "params": {"reason": "settings_missing", "missing": []}},
            {"code": "charging_now", "params": {"until": None}},
        ],
    ),
]


@pytest.mark.parametrize(("name", "facts", "tone", "lines"), CASES, ids=[case[0] for case in CASES])
def test_the_status_block_for_every_rule(name: str, facts: StatusFacts, tone: str, lines: list[dict[str, Any]]) -> None:
    block = compose_status(facts)
    assert block == {"tone": tone, "lines": lines}, name


def test_every_line_names_only_declared_params_and_every_code_is_reachable() -> None:
    seen: set[str] = set()
    for _, facts, _, _ in CASES:
        for line in compose_status(facts)["lines"]:
            assert line["code"] in STATUS_CODES
            assert set(line["params"]) == set(STATUS_CODES[line["code"]][1])
            seen.add(line["code"])
    assert seen == set(STATUS_CODES), sorted(set(STATUS_CODES) - seen)


def test_a_line_that_is_blocking_is_never_composed_with_normal_life() -> None:
    for _, facts, tone, _ in CASES:
        block = compose_status(facts)
        # settings_incomplete is composed with the blocking conditions but is only a notice.
        blocking = [
            line
            for line in block["lines"]
            if STATUS_CODES[line["code"]][0] == "blocking" or line["code"] == "settings_incomplete"
        ]
        assert (block["tone"] == "blocking") == any(
            STATUS_CODES[line["code"]][0] == "blocking" for line in blocking
        )
        if blocking:
            # The one exception: a trailing charging_now while the charger draws current.
            rest = [line["code"] for line in block["lines"][len(blocking) :]]
            assert rest in ([], ["charging_now"]) and facts.charging == bool(rest)


async def test_the_dashboard_and_the_webhook_carry_the_same_block(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    entry_a, *_ = await setup_two_chargers(hass)
    client = await hass_client_no_auth()
    dashboard = await webhook_dashboard(client, "webhook-a")
    capture = dashboard_api.capture_dashboard(hass, entry_a)
    direct = dashboard_api.serialize_dashboard(capture, can_act=False)
    assert dashboard["status"] == direct["status"]
    assert set(dashboard["status"]) == {"tone", "lines"}
