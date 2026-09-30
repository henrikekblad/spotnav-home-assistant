"""Builders for the planner's request and the scenario documents it is replayed against."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from custom_components.spotnav.planning.planner import CONTRACT_VERSION, FiscalChoice, PlanRequest
from custom_components.spotnav.pricing.relay_contract import parse_day


FIXTURE = Path(__file__).parent / "fixtures" / "planner" / "scenarios.json"


def build_request(body: dict[str, Any], documents: tuple[Any, ...]) -> PlanRequest:
    fiscal = body.get("fiscal")
    departure = body.get("departure")
    return PlanRequest(
        area_id=body["area_id"],
        timezone=body["timezone"],
        currency=body["currency"],
        major_unit=body.get("major_unit", ""),
        minor_unit=body.get("minor_unit", ""),
        documents=documents,
        now=datetime.fromisoformat(body["now"]),
        phases=body["phases"],
        amps=body["amps"],
        requested_kwh=body["requested_kwh"],
        consumption_kwh_per_10km=body["consumption_kwh_per_10km"],
        max_periods=body["max_periods"],
        departure=None if departure is None else time.fromisoformat(departure),
        fiscal=FiscalChoice(**fiscal) if fiscal else FiscalChoice(),
    ).validated()


def fixture_payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def parse_documents(bodies: list[dict[str, Any]]) -> tuple[Any, ...]:
    """The scenario's documents, through the same parser the repository uses."""
    parsed = []
    for body in bodies:
        # The fixture states the area and date the way the relay does, and they are
        # both kept: the parser checks that the document it was handed is the one it
        # was asked for, which is exactly the check that makes a scenario honest.
        document = dict(body)
        document["v"] = CONTRACT_VERSION
        parsed.append(
            parse_day(document, area_id=body["area"], day=date.fromisoformat(body["date"]))
        )
    return tuple(parsed)
