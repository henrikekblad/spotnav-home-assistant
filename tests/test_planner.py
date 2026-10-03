"""The pure planner, executed against its portable scenario fixtures.

Nothing here touches Home Assistant, a network, a service or a timer: the planner
is arithmetic, so the whole suite is arithmetic. The scenarios live in
`tests/fixtures/planner/scenarios.json` and are checked by a schema validator
first, so a misspelled expected field fails loudly instead of passing vacuously.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.planner import (
    CONTRACT_VERSION,
    STEP_MINUTES,
    DEFAULT_TARGET_SOC_PERCENT,
    ENERGY_SLIDER_MAX_KWH,
    ENERGY_SLIDER_MIN_KWH,
    FiscalChoice,
    PlanRequest,
    PlannerInputError,
    TargetEnergyRequest,
    calculate_plan,
    price_gap,
    effective_minor_per_kwh,
    effective_target_soc_percent,
    energy_per_slot_kwh,
    power_kw,
    resolve_departure,
    resolve_target_energy,
    slots_needed,
)
from .planning import build_request, fixture_payload, parse_documents

FIXTURE = Path(__file__).parent / "fixtures" / "planner" / "scenarios.json"

#: Every key a scenario may carry, anywhere. A key outside these sets is a typo, and
#: a typo in an expectation is a scenario that proves nothing.
TOP_LEVEL_KEYS = {"schema", "schema_version", "contract_version", "tolerance", "provenance", "scenarios"}
SCENARIO_KEYS = {"name", "documents", "request", "expect", "note"}
REQUEST_KEYS = {
    "area_id", "timezone", "currency", "major_unit", "minor_unit", "now", "phases", "amps",
    "requested_kwh", "consumption_kwh_per_10km", "max_periods", "departure",
    "fiscal",
}
FISCAL_KEYS = {
    "tax_enabled", "tax_minor_per_kwh", "transfer_enabled", "transfer_minor_per_kwh",
    "vat_enabled", "vat_percent",
}
DOCUMENT_KEYS = {"area", "date", "tz", "res", "start", "unit", "prices", "fx", "fx_date"}
EXACT_KEYS = {
    "slots_needed", "priced_slots", "unpriced_slots", "period_count", "unpriced",
    "currency", "major_unit", "minor_unit", "periods", "slot_starts", "local_hours",
    "local_offsets", "slot_source_days", "departure_instant",
}
FLOAT_KEYS = {
    "requested_kwh", "power_kw", "delivered_kwh", "distance_mil", "estimated_cost",
    "local_major_per_kwhs", "effective_minor_per_kwhs",
}
LIST_KEYS = {"periods", "slot_starts", "local_hours", "local_offsets", "slot_source_days",
             "local_major_per_kwhs", "effective_minor_per_kwhs"}
EXPECT_KEYS = EXACT_KEYS | FLOAT_KEYS | {"reason"}


#: The provenance block, exactly. A misspelled key here would otherwise sit unnoticed.
PROVENANCE_KEYS = {"android_commit", "relay_commit", "ha_base_commit", "note"}

#: Everything a success scenario must state. This is the fixture's side of the result
#: contract: identity, the energy and power figures, the money, the shape of the plan and
#: the known/estimated split. A scenario with `reason: null` and nothing else is refused,
#: because it would pass without pinning anything at all.
REQUIRED_SUCCESS_KEYS = {
    "reason",
    "currency",
    "major_unit",
    "minor_unit",
    "slots_needed",
    "priced_slots",
    "unpriced_slots",
    "unpriced",
    "power_kw",
    "requested_kwh",
    "delivered_kwh",
    "distance_mil",
    "estimated_cost",
    "period_count",
    "periods",
    "slot_starts",
}

#: Every request field `PlanRequest` carries except the two that have a documented default.
REQUIRED_REQUEST_KEYS = {
    "area_id",
    "timezone",
    "currency",
    "major_unit",
    "minor_unit",
    "now",
    "phases",
    "amps",
    "requested_kwh",
    "consumption_kwh_per_10km",
    "max_periods",
}

#: Every document field a relay day document has, ignoring the optional rate table.
REQUIRED_DOCUMENT_KEYS = {"area", "date", "tz", "res", "start", "unit", "prices"}

#: Per-slot lists that must all be as long as the slot list when present.
OPTIONAL_REQUEST_KEYS = {"departure", "fiscal"}

#: A day document's optional fields: the rate table the relay attaches when it has one.
OPTIONAL_DOCUMENT_KEYS = {"fx", "fx_date"}

#: The fiscal block's full shape, in either direction.
CHOICE_KEYS = FISCAL_KEYS
CHOICE_FLAGS = {
    "tax_enabled": "tax_minor_per_kwh",
    "transfer_enabled": "transfer_minor_per_kwh",
    "vat_enabled": "vat_percent",
}

#: What a success must assert *besides* `reason`: a scenario that pins nothing is not a test.
SUBSTANTIVE_EXPECT_KEYS = EXPECT_KEYS - {"reason"}

#: Per-slot arrays that must all be as long as the slot list, where a scenario states them.
SLOT_LISTS = ("slot_starts", "local_hours", "local_offsets", "slot_source_days", "local_major_per_kwhs", "effective_minor_per_kwhs")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_iso(text: Any) -> bool:
    """A parseable, **offset-bearing** ISO instant.

    `fromisoformat` happily parses `2026-09-12 18:00` and the result is naive, which is
    exactly the input the planner refuses; a fixture must not be able to state an instant
    without saying whose clock it is on.
    """
    if not isinstance(text, str):
        return False
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return False
    return moment.tzinfo is not None


#: The provenance block, exactly: a misspelled commit key must not slip through.


def validate(payload: dict[str, Any]) -> None:
    """Refuse a fixture that is not exactly the shape the runner understands.

    Proportionate, not exhaustive: a golden scenario pins the behaviour it is *named*
    for, so a focused scenario may state only the fields it needs — while a scenario that
    states nothing beyond `reason: null` is refused. The structural completeness of every
    successful result is not this gate's job; it is asserted for every scenario by the
    invariant tests below, which is the right place for a property of all plans.

    Everything else here is exactness: the key sets, the request and document fields, the
    types, and the coherence of any per-slot arrays a scenario does state.
    """
    assert set(payload) == TOP_LEVEL_KEYS, f"unexpected top-level keys: {sorted(set(payload) - TOP_LEVEL_KEYS)}"
    assert payload["schema"] == "elpris-auto-planner-scenarios"
    assert payload["schema_version"] == 1
    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["tolerance"] == 1e-6
    provenance = payload["provenance"]
    assert isinstance(provenance, dict) and set(provenance) == PROVENANCE_KEYS, "provenance keys must be exact"
    for key in PROVENANCE_KEYS:
        assert isinstance(provenance[key], str) and provenance[key], f"provenance.{key} must be a non-empty string"
    assert isinstance(payload["scenarios"], list) and payload["scenarios"], "the fixture must carry scenarios"
    names = [scenario["name"] for scenario in payload["scenarios"]]
    assert len(names) == len(set(names)), "scenario names must be unique"

    for scenario in payload["scenarios"]:
        where = scenario.get("name", "?")
        assert isinstance(scenario, dict) and set(scenario) <= SCENARIO_KEYS, f"{where}: unexpected scenario keys"
        assert {"name", "documents", "request", "expect"} <= set(scenario), f"{where}: incomplete scenario"
        assert isinstance(scenario["name"], str) and scenario["name"], f"{where}: name"

        assert isinstance(scenario["documents"], list) and scenario["documents"], f"{where}: needs at least one document"
        for document in scenario["documents"]:
            assert isinstance(document, dict), f"{where}: a document must be an object"
            assert set(document) <= REQUIRED_DOCUMENT_KEYS | OPTIONAL_DOCUMENT_KEYS, f"{where}: unexpected document keys"
            assert REQUIRED_DOCUMENT_KEYS <= set(document), f"{where}: incomplete document"
            assert document["res"] in (15, 60), f"{where}: res must be 15 or 60"
            assert _is_iso(document["start"]), f"{where}: start must be an offset-bearing ISO timestamp"
            assert isinstance(document["date"], str), f"{where}: date must be a string"
            try:
                date.fromisoformat(document["date"])
            except ValueError:
                raise AssertionError(f"{where}: date must be an ISO calendar date") from None
            for key in ("area", "tz", "unit"):
                assert isinstance(document[key], str) and document[key], f"{where}: {key}"
            assert document["unit"] == "EUR/kWh", f"{where}: unit"
            prices = document["prices"]
            assert isinstance(prices, list) and prices, f"{where}: prices must be a non-empty array"
            assert all(_is_number(value) for value in prices), f"{where}: prices must be finite numbers"
            if "fx" in document:
                rates = document["fx"]
                assert isinstance(rates, dict) and rates, f"{where}: fx must be a non-empty object"
                for currency, rate in rates.items():
                    assert isinstance(currency, str) and currency, f"{where}: fx keys are currency codes"
                    assert _is_number(rate) and rate > 0, f"{where}: fx rates must be positive finite numbers"
                # A calendar date, like the document's own: `fromisoformat` would parse it
                # as a naive midnight, which is not an instant and must not be one here.
                assert "fx_date" in document, f"{where}: fx_date is required when fx is stated"
                try:
                    date.fromisoformat(document["fx_date"])
                except (ValueError, TypeError):
                    raise AssertionError(f"{where}: fx_date must be an ISO calendar date") from None

        request = scenario["request"]
        assert isinstance(request, dict) and set(request) <= REQUEST_KEYS, f"{where}: unexpected request keys"
        assert REQUIRED_REQUEST_KEYS <= set(request), f"{where}: missing request fields {sorted(REQUIRED_REQUEST_KEYS - set(request))}"
        assert request["phases"] in (1, 3), f"{where}: phases must be 1 or 3"
        assert isinstance(request["amps"], int) and not isinstance(request["amps"], bool) and request["amps"] > 0, f"{where}: amps"
        assert isinstance(request["max_periods"], int) and not isinstance(request["max_periods"], bool) and 1 <= request["max_periods"] <= 8, f"{where}: max_periods"
        assert _is_number(request["requested_kwh"]) and request["requested_kwh"] > 0, f"{where}: requested_kwh"
        assert _is_number(request["consumption_kwh_per_10km"]) and request["consumption_kwh_per_10km"] > 0, f"{where}: consumption"
        assert _is_iso(request["now"]), f"{where}: now must be an aware ISO instant"
        for key in ("area_id", "timezone", "currency", "major_unit", "minor_unit"):
            assert isinstance(request[key], str) and request[key], f"{where}: {key}"
        if "departure" in request and request["departure"] is not None:
            # `null` is the documented way of saying "no departure", and the schema allows
            # the key to be absent as well; anything else must be a real wall time.
            assert isinstance(request["departure"], str), f"{where}: departure must be a wall time string or null"
            time.fromisoformat(request["departure"])
        if "fiscal" in request:
            fiscal = request["fiscal"]
            assert isinstance(fiscal, dict) and set(fiscal) == CHOICE_KEYS, f"{where}: the fiscal block must state every flag and value"
            for flag, value in CHOICE_FLAGS.items():
                assert _is_bool(fiscal[flag]), f"{where}: {flag} must be a boolean"
                if fiscal[flag]:
                    assert _is_number(fiscal[value]), f"{where}: {value} is required when {flag} is set"
                else:
                    assert fiscal[value] is None, f"{where}: {value} must be null when {flag} is clear"

        expect = scenario["expect"]
        assert isinstance(expect, dict) and set(expect) <= EXPECT_KEYS, f"{where}: unexpected expect keys {sorted(set(expect) - EXPECT_KEYS)}"
        assert "reason" in expect, f"{where}: every scenario must state its reason"
        assert expect["reason"] is None or isinstance(expect["reason"], str), f"{where}: reason must be a string or null"
        if expect["reason"] is not None:
            extra = set(expect) - {"reason"}
            assert not extra, f"{where}: a refusal must not carry plan fields {sorted(extra)}"
        else:
            assert set(expect) & SUBSTANTIVE_EXPECT_KEYS, f"{where}: a successful scenario must assert something besides its reason"
        for key, value in expect.items():
            if key == "reason":
                continue
            if key == "unpriced":
                assert _is_bool(value), f"{where}: unpriced must be a boolean"
            elif key in ("currency", "major_unit", "minor_unit"):
                assert isinstance(value, str) and value, f"{where}: {key}"
            elif key == "departure_instant":
                assert _is_iso(value), f"{where}: departure_instant must be an ISO instant"
            elif key in LIST_KEYS:
                assert isinstance(value, list) and value, f"{where}: {key} must be a non-empty list"
                if key in ("slot_starts", "local_offsets", "slot_source_days"):
                    assert all(isinstance(item, str) for item in value), f"{where}: {key} must be strings"
                else:
                    assert all(_is_number(item) or isinstance(item, list) for item in value), f"{where}: {key} contents"
            else:
                assert _is_number(value), f"{where}: {key} must be a finite number"
        present = [key for key in SLOT_LISTS if key in expect]
        if len(present) > 1:
            lengths = {key: len(expect[key]) for key in present}
            assert len(set(lengths.values())) == 1, f"{where}: per-slot arrays disagree in length {lengths}"
        if "slot_starts" in expect and "slots_needed" in expect:
            assert len(expect["slot_starts"]) == expect["slots_needed"], f"{where}: one start per needed slot"


def local_of(moment: datetime, tz: str) -> datetime:
    return moment.astimezone(dt_util.get_time_zone(tz))


def check(scenario: dict[str, Any], tolerance: float) -> None:
    """Every expectation in one scenario, checked by kind."""
    where = scenario["name"]
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)
    expect = scenario["expect"]

    assert result.reason == expect["reason"], f"{where}: reason"
    if expect["reason"] is not None:
        assert not result.has_plan and result.periods == () and result.slots == ()
        return

    assert result.has_plan and result.version == CONTRACT_VERSION
    assert (result.area_id, result.timezone) == (request.area_id, request.timezone)

    for key, value in expect.items():
        if key == "reason":
            continue
        if key == "periods":
            assert [[start.isoformat(), end.isoformat()] for start, end in result.periods] == value, where
        elif key == "slot_starts":
            assert [slot.start.isoformat() for slot in result.slots] == value, where
        elif key == "local_hours":
            assert [local_of(slot.start, request.timezone).hour for slot in result.slots] == value, where
        elif key == "local_offsets":
            assert [local_of(slot.start, request.timezone).strftime("%z") for slot in result.slots] == value, where
        elif key == "slot_source_days":
            assert [slot.source for slot in result.slots] == value, where
        elif key == "period_count":
            assert len(result.periods) == value, where
        elif key == "departure_instant":
            # The deadline the plan was actually built against, as an instant. Every
            # period and every selected slot is checked against it, so a wrong
            # departure resolution cannot hide behind a plausible-looking plan.
            deadline = datetime.fromisoformat(value)
            for _, end in result.periods:
                assert end <= deadline, f"{where}: a period ends after the deadline"
            assert max(slot.end for slot in result.slots) <= deadline, where
        elif key in ("local_major_per_kwhs", "effective_minor_per_kwhs"):
            attribute = key[: -len("s")]
            assert len(result.slots) == len(value), where
            for slot, expected in zip(result.slots, value, strict=True):
                assert getattr(slot, attribute) == pytest.approx(expected, abs=tolerance), where
        else:
            actual = getattr(result, key)
            if key in EXACT_KEYS:
                assert actual == value, f"{where}: {key}"
            else:
                assert actual == pytest.approx(value, abs=tolerance), f"{where}: {key}"


@pytest.mark.parametrize("scenario", fixture_payload()["scenarios"], ids=lambda item: item["name"])
def test_every_scenario_matches_its_expectations(scenario: dict[str, Any]) -> None:
    payload = fixture_payload()
    validate(payload)
    assert scenario["request"]["area_id"] == scenario["documents"][0]["area"]
    check(scenario, payload["tolerance"])


def mutated(**changes: Any) -> dict[str, Any]:
    """A deep copy of the fixture with the first scenario's fields replaced."""
    copy = json.loads(json.dumps(fixture_payload()))
    copy["scenarios"][0].update(changes)
    return copy


def refuses(payload: dict[str, Any]) -> bool:
    """Whether the gate rejects a payload, without caring which assertion fired."""
    try:
        validate(payload)
    except (AssertionError, KeyError, ValueError, TypeError):
        return True
    return False


def test_the_schema_gate_refuses_every_class_of_undocumented_fixture() -> None:
    """Each way of writing too little, or the wrong thing, must fail — and say which.

    Every case is checked through [refuses] rather than `pytest.raises`, so a gate that
    stops rejecting one of them fails with the name of the case instead of a bare
    "did not raise".
    """
    good = fixture_payload()
    validate(good)
    first = good["scenarios"][0]
    request = first["request"]
    document = first["documents"][0]
    expect = first["expect"]
    refusal = next(s for s in good["scenarios"] if s["expect"]["reason"] is not None)

    def with_scenario(**changes: Any) -> dict[str, Any]:
        return mutated(**changes)

    def with_request(**changes: Any) -> dict[str, Any]:
        return mutated(request={**request, **changes})

    def with_document(**changes: Any) -> dict[str, Any]:
        return mutated(documents=[{**document, **changes}])

    cases: list[tuple[str, dict[str, Any]]] = []

    unknown_top = json.loads(json.dumps(good))
    unknown_top["scenariosX"] = []
    cases.append(("an unknown top-level key", unknown_top))
    unknown_provenance = json.loads(json.dumps(good))
    unknown_provenance["provenance"]["android"] = "x"
    cases.append(("a misspelled provenance key", unknown_provenance))
    renamed_version = json.loads(json.dumps(good))
    renamed_version["contract_version"] = CONTRACT_VERSION + 1
    cases.append(("a contract version the planner does not implement", renamed_version))
    repeated = json.loads(json.dumps(good))
    repeated["scenarios"].append(json.loads(json.dumps(first)))
    cases.append(("a duplicated scenario name", repeated))

    cases.append(("an unknown scenario key", with_scenario(unexpected="y")))
    cases.append(("a typo inside an expectation", with_scenario(expect={**expect, "estimateed_slots": 1})))
    cases.append(("a success that asserts nothing but its reason", with_scenario(expect={"reason": None})))
    cases.append(("an expectation with no reason at all", with_scenario(expect={"estimated_cost": 1.0})))
    cases.append(("a scenario with no documents", with_scenario(documents=[])))
    cases.append(("a scenario with no expectation", with_scenario(expect={})))

    cases.append(("a missing request field", mutated(request={k: v for k, v in request.items() if k != "amps"})))
    cases.append(("amps as a string", with_request(amps="10")))
    cases.append(("amps as a boolean", with_request(amps=True)))
    cases.append(("phases of two", with_request(phases=2)))
    cases.append(("max_periods of zero", with_request(max_periods=0)))
    cases.append(("max_periods of nine", with_request(max_periods=9)))
    cases.append(("a naive calculation instant", with_request(now="2026-09-12T18:00:00")))
    cases.append(("zero consumption", with_request(consumption_kwh_per_10km=0)))
    cases.append(("an unknown request key", with_request(amperes=10)))

    cases.append(("an empty price array", with_document(prices=[])))
    cases.append(("a boolean where a price belongs", with_document(prices=[True, 1.0])))
    cases.append(("a resolution the relay cannot publish", with_document(res=30)))
    cases.append(("a zero fx rate", with_document(fx={"SEK": 0}, fx_date="2026-09-12")))
    cases.append(("an unknown document key", with_document(sources="relay")))

    cases.append(("a fiscal block missing its flags", with_request(fiscal={"vat_enabled": True})))
    cases.append(("tax enabled with no value", with_request(fiscal={
        "tax_enabled": True, "tax_minor_per_kwh": None, "transfer_enabled": False,
        "transfer_minor_per_kwh": None, "vat_enabled": False, "vat_percent": None,
    })))
    cases.append(("a value beside a flag that is clear", with_request(fiscal={
        "tax_enabled": False, "tax_minor_per_kwh": 0.0, "transfer_enabled": False,
        "transfer_minor_per_kwh": None, "vat_enabled": False, "vat_percent": None,
    })))

    cases.append(("unpriced as an integer", with_scenario(expect={**expect, "unpriced": 1})))
    cases.append(("a monetary value as a string", with_scenario(expect={**expect, "estimated_cost": "0.5"})))
    cases.append(("per-slot arrays of different lengths", with_scenario(
        expect={**expect, "slot_starts": expect["slot_starts"][:1]},
    )))

    copy = json.loads(json.dumps(good))
    for scenario in copy["scenarios"]:
        if scenario["name"] == refusal["name"]:
            scenario["expect"]["estimated_cost"] = 0.0
    cases.append(("a plan field on a refusal", copy))

    # Nothing above is accepted, and each failure names its case.
    for label, payload in cases:
        assert refuses(payload), f"the gate accepted a malformed fixture: {label}"


def test_the_resolution_boundary_is_where_the_contract_puts_it() -> None:
    """15/60 are the contract's, 30 is nobody's, and a forged document raises."""
    from dataclasses import replace as dataclass_replace

    from custom_components.spotnav.planning.planner import PlanningSlot, planning_slots
    from custom_components.spotnav.pricing.relay_contract import RelayParseError

    scenario = next(s for s in fixture_payload()["scenarios"] if s["name"] == "hourly_document_expands_to_quarters")
    hourly = scenario["documents"][0]
    quarter = next(s for s in fixture_payload()["scenarios"] if s["name"] == "power_single_phase_whole_slot_overdelivery")["documents"][0]

    # 1. Both resolutions the relay publishes are accepted and expand exactly: one slot
    #    per quarter hour, four per hour, on the same absolute instants.
    quarter_document = parse_documents([quarter])[0]
    hourly_document = parse_documents([hourly])[0]
    quarter_slots = planning_slots((quarter_document,), currency="EUR", timezone=quarter["tz"])
    hourly_slots = planning_slots((hourly_document,), currency="EUR", timezone=hourly["tz"])
    assert all(isinstance(slot, PlanningSlot) for slot in quarter_slots)
    assert len(quarter_slots) == len(quarter["prices"])
    assert len(hourly_slots) == 4 * len(hourly["prices"])
    assert hourly_slots[0].start == hourly_document.start_instant
    assert hourly_slots[1].start - hourly_slots[0].start == timedelta(minutes=15)
    assert hourly_slots[0].local_major_per_kwh == hourly_slots[3].local_major_per_kwh

    # 2. The public path refuses anything else, as a relay-contract error, before the
    #    planner is ever involved.
    with pytest.raises(RelayParseError) as contract:
        parse_documents([{**quarter, "res": 30}])
    assert contract.value.code == "invalid_resolution"

    # 3. A caller that forges a document anyway gets an input error, not a plan result:
    #    there is no `unsupported_resolution` data state, because the relay cannot
    #    publish such a document in the first place.
    # 20 minutes is the interesting forgery: it is not a resolution the relay contract
    # allows, and it cannot be laid out on the 15-minute grid at all. (30 minutes is
    # representable — two slots per row — so the planner accepts it by arithmetic even
    # though the relay never publishes one, which is why this case uses 20.)
    forged = dataclass_replace(quarter_document, resolution_minutes=20)
    request = build_request(scenario["request"], (forged,))
    with pytest.raises(PlannerInputError) as forged_error:
        calculate_plan(request)
    assert forged_error.value.code == "unsupported_resolution"

    # 4. A missing rate stays a *data* reason, because a valid non-EUR document can
    #    genuinely have no usable rate, and a caller must be able to show that.
    no_rate = build_request(
        {**scenario["request"], "currency": "SEK", "major_unit": "kr", "minor_unit": "\u00f6re"},
        parse_documents([quarter]),
    )
    assert calculate_plan(no_rate).reason == "missing_fx_rate"


# --------------------------- restored: the pure contract the damage removed


def replace_request(request: PlanRequest, **changes: Any) -> PlanRequest:
    """The same request with fields replaced, for the refusal cases below."""
    from dataclasses import replace

    return replace(request, **changes)


def test_power_and_whole_slot_arithmetic() -> None:
    assert power_kw(16, 1) == pytest.approx(3.68, abs=1e-9)
    assert power_kw(16, 3) == pytest.approx(11.085125168440815, abs=1e-9)
    assert energy_per_slot_kwh(10, 1) == pytest.approx(0.575, abs=1e-12)
    # Whole slots, and never fewer than one: 1.0 kWh at 0.575 per slot needs two.
    assert slots_needed(1.0, 10, 1) == 2
    assert slots_needed(0.1, 10, 1) == 1
    assert slots_needed(1.15, 10, 1) == 2


def test_the_fiscal_order_is_minor_unit_then_vat_last() -> None:
    fiscal = FiscalChoice(
        tax_enabled=True,
        tax_minor_per_kwh=60,
        transfer_enabled=True,
        transfer_minor_per_kwh=30,
        vat_enabled=True,
        vat_percent=25,
    ).validated()
    assert effective_minor_per_kwh(1.0, fiscal) == pytest.approx(237.5, abs=1e-9)
    # Nothing enabled is the plain major-to-minor conversion.
    assert effective_minor_per_kwh(1.0, FiscalChoice()) == pytest.approx(100.0, abs=1e-9)
    # Zero and absent stay different facts: a present zero adds nothing, a clear flag
    # with a value beside it contributes nothing either.
    assert effective_minor_per_kwh(1.0, FiscalChoice(tax_enabled=True, tax_minor_per_kwh=0).validated()) == 100.0
    assert effective_minor_per_kwh(1.0, FiscalChoice(tax_minor_per_kwh=60).validated()) == 100.0


def test_target_soc_resolution_covers_every_case_the_spec_names() -> None:
    # Reported capacity with a stored target, and a remembered capacity beside it.
    reported = resolve_target_energy(TargetEnergyRequest(soc_percent=40, capacity_kwh=60, stored_target_percent=80))
    assert reported.reason == "ok" and reported.kwh == pytest.approx(24.0)
    assert reported.target_percent == 80 and reported.effective_capacity_kwh == 60
    remembered = resolve_target_energy(TargetEnergyRequest(soc_percent=40, capacity_kwh=45.5, stored_target_percent=80))
    assert remembered.reason == "ok" and remembered.kwh == pytest.approx(18.2)

    # Unknown and non-positive capacity are unavailable, never zero.
    for capacity in (None, 0, -1):
        unknown = resolve_target_energy(TargetEnergyRequest(soc_percent=40, capacity_kwh=capacity))
        assert unknown.reason == "unknown_capacity" and unknown.kwh is None
    # A non-finite capacity is not "unknown", it is a bad number and is refused.
    with pytest.raises(PlannerInputError) as bad_capacity:
        resolve_target_energy(TargetEnergyRequest(soc_percent=40, capacity_kwh=float("nan")))
    assert bad_capacity.value.code == "invalid_number"

    # The default is used only when the caller asks for it, and a vehicle maximum caps
    # the stored choice without rewriting it.
    assert effective_target_soc_percent(None, None, apply_default_target=True) == DEFAULT_TARGET_SOC_PERCENT
    with pytest.raises(PlannerInputError) as missing:
        effective_target_soc_percent(None, None, apply_default_target=False)
    assert missing.value.code == "invalid_target_soc"
    assert effective_target_soc_percent(90, 75.4, apply_default_target=False) == 75
    assert effective_target_soc_percent(90, None, apply_default_target=False) == 90

    # Already at or above target is its own answer: the 1 kWh slider minimum must not
    # turn a full car into a plan.
    for soc in (80, 85, 100):
        full = resolve_target_energy(
            TargetEnergyRequest(soc_percent=soc, capacity_kwh=60, stored_target_percent=80, whole_kwh_input=True)
        )
        assert full.reason == "already_at_target" and full.kwh == 0.0

    # A whole-kWh input rounds up and stays inside the slider's range.
    rounded = resolve_target_energy(
        TargetEnergyRequest(soc_percent=30, capacity_kwh=60, stored_target_percent=80, whole_kwh_input=True)
    )
    assert rounded.kwh == pytest.approx(30.0)
    tiny = resolve_target_energy(
        TargetEnergyRequest(soc_percent=79.5, capacity_kwh=10, stored_target_percent=80, whole_kwh_input=True)
    )
    assert tiny.reason == "ok" and tiny.kwh == float(ENERGY_SLIDER_MIN_KWH)
    huge = resolve_target_energy(
        TargetEnergyRequest(soc_percent=0, capacity_kwh=1000, stored_target_percent=100, whole_kwh_input=True)
    )
    assert huge.kwh == float(ENERGY_SLIDER_MAX_KWH)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"phases": 2}, "invalid_phases"),
        ({"phases": 0}, "invalid_phases"),
        ({"amps": 0}, "invalid_amps"),
        ({"amps": -16}, "invalid_amps"),
        ({"requested_kwh": 0}, "invalid_energy"),
        ({"requested_kwh": -1}, "invalid_energy"),
        ({"requested_kwh": float("nan")}, "invalid_number"),
        ({"requested_kwh": float("inf")}, "invalid_number"),
        ({"consumption_kwh_per_10km": 0}, "invalid_consumption"),
        ({"consumption_kwh_per_10km": -2}, "invalid_consumption"),
        ({"max_periods": 0}, "invalid_periods"),
        ({"max_periods": 9}, "invalid_periods"),
        ({"timezone": "Mars/Olympus"}, "timezone_mismatch"),
        ({"departure": "20:00+00:00"}, "invalid_departure"),
    ],
)
def test_bad_input_is_refused_rather_than_repaired(changes: dict[str, Any], code: str) -> None:
    scenario = next(s for s in fixture_payload()["scenarios"] if s["name"] == "power_single_phase_whole_slot_overdelivery")
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    if code == "invalid_departure":
        changes = {"departure": time.fromisoformat(changes["departure"])}
    with pytest.raises(PlannerInputError) as caught:
        replace_request(request, **changes).validated()
    assert caught.value.code == code


def test_a_naive_calculation_instant_and_a_cross_area_document_are_refused() -> None:
    scenario = next(s for s in fixture_payload()["scenarios"] if s["name"] == "power_single_phase_whole_slot_overdelivery")
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))

    with pytest.raises(PlannerInputError) as naive:
        replace_request(request, now=datetime(2026, 9, 12, 18, 0)).validated()
    assert naive.value.code == "invalid_timestamp"

    with pytest.raises(PlannerInputError) as other_area:
        replace_request(request, area_id="NO1").validated()
    assert other_area.value.code == "area_mismatch"

    with pytest.raises(PlannerInputError) as other_zone:
        replace_request(request, timezone="Europe/Berlin").validated()
    assert other_zone.value.code == "timezone_mismatch"

    # Enabling a component without an explicit value is invalid, not an invented zero;
    # a present zero is a real rate.
    with pytest.raises(PlannerInputError) as no_value:
        FiscalChoice(tax_enabled=True).validated()
    assert no_value.value.code == "invalid_fiscal"
    with pytest.raises(PlannerInputError) as no_vat:
        FiscalChoice(vat_enabled=True).validated()
    assert no_vat.value.code == "invalid_fiscal"
    assert FiscalChoice(vat_enabled=True, vat_percent=0).validated().vat_percent == 0.0
    assert FiscalChoice(tax_enabled=True, tax_minor_per_kwh=0).validated().tax_minor_per_kwh == 0.0


def resolved_departure(now: str, departure: time, tz: str = "Europe/Stockholm") -> datetime:
    """A deadline resolved from `now`, with the first slot derived the same way the planner does."""
    moment = datetime.fromisoformat(now)
    zone = dt_util.get_time_zone(tz)
    local = moment.astimezone(zone)
    floored = local - timedelta(minutes=local.minute % 15, seconds=local.second, microseconds=local.microsecond)
    first = floored if floored >= local else floored + timedelta(minutes=15)
    return resolve_departure(moment, tz, departure, first).astimezone(timezone.utc)


def test_the_resolved_deadline_for_every_dst_departure_case() -> None:
    """The six instants the task states, each derived from the policy, in UTC.

    Stockholm's transitions are 2026-03-29 (01:59+01:00 -> 03:00+02:00) and
    2025-10-26 (02:59+02:00 -> 02:00+01:00).
    """
    assert resolved_departure("2026-09-12T18:00:00+02:00", time(20, 0)) == datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)
    assert resolved_departure("2026-03-29T01:30:00+01:00", time(3, 30)) == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
    assert resolved_departure("2026-03-29T01:30:00+01:00", time(2, 30)) == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert resolved_departure("2025-10-26T01:00:00+02:00", time(2, 30)) == datetime(2025, 10, 26, 0, 30, tzinfo=timezone.utc)
    assert resolved_departure("2025-10-26T01:00:00+02:00", time(3, 30)) == datetime(2025, 10, 26, 2, 30, tzinfo=timezone.utc)

    # A departure already passed rolls to the next *local calendar date*: on the 23-hour
    # day 04:00 is 02:00Z, where adding 24 elapsed hours would have answered 03:00Z.
    assert resolved_departure("2026-03-28T23:00:00+01:00", time(4, 0)) == datetime(2026, 3, 29, 2, 0, tzinfo=timezone.utc)
    assert resolved_departure("2026-03-28T23:00:00+01:00", time(4, 0)) != datetime(2026, 3, 29, 3, 0, tzinfo=timezone.utc)
    # And on the 25-hour day 04:00 is 03:00Z, where elapsed hours would have said 02:00Z.
    assert resolved_departure("2025-10-25T23:00:00+02:00", time(4, 0)) == datetime(2025, 10, 26, 3, 0, tzinfo=timezone.utc)
    assert resolved_departure("2025-10-25T23:00:00+02:00", time(4, 0)) != datetime(2025, 10, 26, 2, 0, tzinfo=timezone.utc)


def test_the_spring_plan_ends_by_the_real_deadline_and_takes_the_latest_pair() -> None:
    """03:30 local must never mean 04:30, and equal costs choose the latest slots."""
    scenario = next(s for s in fixture_payload()["scenarios"] if s["name"] == "spring_day_92_slots_skips_the_missing_hour")
    request = build_request(scenario["request"], parse_documents(scenario["documents"]))
    result = calculate_plan(request)
    deadline = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)

    assert resolve_departure(request.now, request.timezone, request.departure, request.now).astimezone(timezone.utc) == deadline
    assert [slot.start.isoformat() for slot in result.slots] == [
        "2026-03-29T01:00:00+00:00",
        "2026-03-29T01:15:00+00:00",
    ]
    assert max(slot.end for slot in result.slots) <= deadline
    assert [slot.start.astimezone(dt_util.get_time_zone(request.timezone)).hour for slot in result.slots] == [3, 3]


def test_equal_cost_chooses_the_chronologically_latest_slots() -> None:
    """Flat prices, one period: the ordering picks the latest slots of the 24-hour horizon, not an earlier tie."""
    payload = fixture_payload()
    base = next(s for s in payload["scenarios"] if s["name"] == "power_single_phase_whole_slot_overdelivery")
    today, tomorrow = base["documents"][0], base["documents"][1]
    flat = [
        {**today, "prices": [1.0] * len(today["prices"])},
        {**tomorrow, "prices": [1.0] * len(tomorrow["prices"])},
    ]
    request = build_request(
        {**base["request"], "requested_kwh": 0.5, "now": "2026-09-12T18:00:00+02:00"},
        parse_documents(flat),
    )
    single = calculate_plan(request)
    assert [slot.start.isoformat() for slot in single.slots] == ["2026-09-13T15:45:00+00:00"]
    assert single.unpriced_slots == 0

    pair = calculate_plan(replace_request(request, requested_kwh=1.0))
    assert [slot.start.isoformat() for slot in pair.slots] == [
        "2026-09-13T15:30:00+00:00",
        "2026-09-13T15:45:00+00:00",
    ]
    assert len(pair.periods) == 1



# ------------------------------------ the 24-hour horizon of a request without a deadline


def day_body(day: str, prices: list[float], tz: str = "Europe/Stockholm", start: str | None = None) -> dict[str, Any]:
    """One 15-minute day document, exactly as the relay publishes it."""
    return {
        "area": "SE4",
        "date": day,
        "tz": tz,
        "res": 15,
        "start": start if start is not None else f"{day}T00:00:00+02:00",
        "unit": "EUR/kWh",
        "prices": prices,
    }


def no_deadline_request(
    *,
    now: str,
    documents: list[dict[str, Any]],
    requested_kwh: float,
    amps: int = 16,
    phases: int = 3,
    max_periods: int = 1,
) -> PlanRequest:
    """A request with **no departure**: the case whose horizon geometry is being pinned."""
    return build_request(
        {
            "area_id": "SE4",
            "timezone": "Europe/Stockholm",
            "currency": "EUR",
            "major_unit": "€",
            "minor_unit": "cent",
            "now": now,
            "phases": phases,
            "amps": amps,
            "requested_kwh": requested_kwh,
            "consumption_kwh_per_10km": 2.0,
            "max_periods": max_periods,
            "departure": None,
        },
        parse_documents(documents),
    )


def floor_to_slot(moment: datetime) -> datetime:
    """The quarter-hour boundary [calculate_plan] itself starts from."""
    floored = moment - timedelta(
        minutes=moment.minute % STEP_MINUTES, seconds=moment.second, microseconds=moment.microsecond
    )
    return floored if floored >= moment else floored + timedelta(minutes=STEP_MINUTES)


def horizon_of(request: PlanRequest) -> datetime:
    """The request's own 24-hour boundary, derived here from its own instant and nothing else."""
    return floor_to_slot(request.now) + timedelta(hours=24)


def test_a_windows_length_request_plans_inside_the_horizon_with_no_extra_duration() -> None:
    """Exactly 24 hours of published quarter-hours, and a request needing four hours of them.

    The horizon is the *end* of the window, so the candidate list is the published day and nothing
    beyond it (asking for `horizon + duration` would refuse this outright).
    """
    request = no_deadline_request(
        now="2026-09-12T00:00:00+02:00",
        documents=[day_body("2026-09-12", [100.0 - index for index in range(96)])],
        requested_kwh=42.0,
    )
    result = calculate_plan(request)

    assert result.reason is None
    assert len(result.slots) == 16, "42 kWh at 16 A three-phase is sixteen quarter-hours"
    assert result.unpriced_slots == 0
    boundary = horizon_of(request)
    assert max(slot.end for slot in result.slots) <= boundary, "every chosen slot ends inside the horizon"
    assert result.horizon[0].start == datetime.fromisoformat("2026-09-12T00:00:00+02:00")
    assert all(row.end <= boundary for row in result.horizon)


def test_the_field_case_plans_from_late_evening_without_estimating() -> None:
    """The observed shape: 22:00 local, 42 kWh, sixteen quarter-hours, tomorrow published to midnight.

    Prices cover about 26 hours -- more than 24, less than 24 plus the charging duration -- and that is
    enough, because the window ends at the horizon rather than at horizon-plus-duration.
    """
    request = no_deadline_request(
        now="2026-09-12T22:00:00+02:00",
        documents=[day_body("2026-09-12", [1.0] * 96), day_body("2026-09-13", [2.0] * 96)],
        requested_kwh=42.0,
    )
    result = calculate_plan(request)

    assert result.reason is None, "a complete following day is enough for a four-hour request"
    assert len(result.slots) == 16
    assert result.unpriced_slots == 0, "no estimated slot is needed here"
    boundary = horizon_of(request)
    assert boundary == datetime.fromisoformat("2026-09-13T22:00:00+02:00")
    assert max(slot.end for slot in result.slots) <= boundary
    assert all(row.end <= boundary for row in result.horizon)
    assert max(row.end for row in result.horizon) == boundary, "the window ends exactly at the horizon"
    # The horizon is the calculation's own capture, in instant order, and every row keeps the date it
    # really belongs to: the rows the clock runs into after local midnight are the *next* date's. That is
    # exactly why a client may not read a date's role out of the position of a row among the rows.
    days = [row.day.isoformat() for row in result.horizon]
    assert set(days) == {"2026-09-12", "2026-09-13"}
    assert days.index("2026-09-13") == 8, "22:00 to midnight is the 12th's own two hours, eight quarters"
    assert result.horizon[8].start == datetime.fromisoformat("2026-09-13T00:00:00+02:00")


def a_window_with_a_missing_day_inside_it() -> list[dict[str, Any]]:
    """A 22:00 window whose *middle* is unpublished: 2026-09-12 is absent between two published days.

    The last published day is complete, so the fallback profile knows every wall clock this gap needs
    -- which is exactly the difference between a slot that can be estimated and one that cannot.
    """
    return [day_body("2026-09-11", [1.0] * 96), day_body("2026-09-13", [1.0] * 96)]


def test_a_missing_slot_inside_the_window_is_named() -> None:
    """One unpublished quarter-hour inside the window, estimation disabled: the named reason."""
    request = no_deadline_request(
        now="2026-09-11T22:00:00+02:00",
        documents=a_window_with_a_missing_day_inside_it(),
        requested_kwh=42.0,
    )
    result = calculate_plan(request)

    assert result.reason == "insufficient_price_horizon"
    assert result.slots == ()
    assert result.horizon == (), "a refused calculation projects no window"


def test_a_missing_slot_is_never_filled_from_another_day() -> None:
    """The estimated fallback is retired: an unpublished slot is a named gap, and `price_gap` says where.

    What to do about the gap (wait, buy what cannot wait, charge without prices) is `price_wait`'s
    decision; the planner only ever chooses among published prices.
    """
    request = no_deadline_request(
        now="2026-09-11T22:00:00+02:00",
        documents=a_window_with_a_missing_day_inside_it(),
        requested_kwh=42.0,
    )
    gap = price_gap(request)

    assert gap is not None
    assert gap.missing_from == datetime.fromisoformat("2026-09-12T00:00:00+02:00")
    assert len(gap.known) == 8, "the eight quarter-hours between 22:00 and midnight are published"
    assert gap.deadline == horizon_of(request)


def test_energy_that_cannot_fit_the_window_is_refused_rather_than_truncated() -> None:
    """More quarter-hours than the window holds: a named refusal, never a shorter plan.

    100 kWh at 6 A single phase needs about 290 quarter-hours, and the window holds 96.
    """
    request = no_deadline_request(
        now="2026-09-12T00:00:00+02:00",
        documents=[day_body("2026-09-12", [1.0] * 96)],
        requested_kwh=100.0,
        amps=6,
        phases=1,
    )
    result = calculate_plan(request)

    assert result.reason == "insufficient_price_horizon"
    assert result.slots == ()
    assert result.requested_kwh == 100.0, "the request is reported as it was made, not as it would fit"
    assert result.delivered_kwh == 0.0
    assert result.slots_needed == 0


def test_the_last_usable_slot_ends_exactly_at_the_horizon_and_never_after_it() -> None:
    """A request that needs the whole window fills it to the boundary and stops there."""
    request = no_deadline_request(
        now="2026-09-12T00:00:00+02:00",
        documents=[day_body("2026-09-12", [1.0] * 96)],
        requested_kwh=33.12,  # 96 quarter-hours at 6 A single phase
        amps=6,
        phases=1,
        max_periods=8,
    )
    result = calculate_plan(request)

    boundary = horizon_of(request)
    assert result.reason is None
    assert len(result.slots) == 96
    assert max(slot.end for slot in result.slots) == boundary
    assert all(slot.end <= boundary for slot in result.slots)
    assert result.horizon[-1].end == boundary


# ---------------------------------------------------- DST: elapsed instants, never wall clocks


def test_a_spring_forward_window_is_96_elapsed_quarter_hours() -> None:
    """The 24-hour window on a 23-hour local day: 96 instants, uniform in elapsed time.

    The published day holds 92 quarter-hours and the following day holds the rest, and the window lands
    on exactly those instants without estimation -- which is only true if the grid is elapsed time.
    """
    spring = next(
        scenario for scenario in fixture_payload()["scenarios"]
        if scenario["name"] == "spring_day_92_slots_skips_the_missing_hour"
    )["documents"]
    documents = [spring[0], day_body("2026-03-30", [1.0] * 96)]
    request = no_deadline_request(
        now="2026-03-29T00:00:00+01:00", documents=documents, requested_kwh=42.0
    )
    result = calculate_plan(request)

    boundary = horizon_of(request)
    assert result.reason is None
    rows = result.horizon
    assert len(rows) == 96
    assert result.unpriced_slots == 0, "every instant of the window is a published one"
    # Measured in UTC, because that is what "elapsed" means: two aware instants in the same zone
    # subtract as wall clocks, which is precisely the confusion this test exists to prevent.
    elapsed = [row.start.astimezone(timezone.utc) for row in rows]
    assert all(
        elapsed[index + 1] - elapsed[index] == timedelta(minutes=STEP_MINUTES) for index in range(95)
    )
    assert rows[-1].end.astimezone(timezone.utc) == boundary.astimezone(timezone.utc)
    assert boundary == datetime.fromisoformat("2026-03-30T01:00:00+02:00"), "24 elapsed hours later"
    # The wall clock is *not* uniform: the hour that does not exist locally is skipped by an
    # hour-wide step, which is exactly why the grid cannot be a wall-clock one.
    wall_steps = {rows[index + 1].start.replace(tzinfo=None) - rows[index].start.replace(tzinfo=None) for index in range(95)}
    assert timedelta(minutes=75) in wall_steps
    # The local offset moves inside the window, which is what makes the elapsed grid different from a
    # wall-clock one; the hour that does not exist locally is simply absent from the timeline.
    offsets = {row.start.astimezone(dt_util.get_time_zone("Europe/Stockholm")).utcoffset() for row in rows}
    assert offsets == {timedelta(hours=1), timedelta(hours=2)}
    local_hours = [row.start.astimezone(dt_util.get_time_zone("Europe/Stockholm")).hour for row in rows]
    assert 2 not in local_hours, "02:00 local never happens on the spring day"


def test_an_autumn_window_is_96_elapsed_quarter_hours_and_repeats_a_local_hour() -> None:
    """The same 24-hour window on a 25-hour local day, where one wall clock happens twice."""
    autumn = next(
        scenario for scenario in fixture_payload()["scenarios"]
        if scenario["name"] == "autumn_day_100_slots_repeats_the_hour"
    )["documents"]
    request = no_deadline_request(
        now="2025-10-26T00:00:00+02:00", documents=[autumn[0]], requested_kwh=42.0
    )
    result = calculate_plan(request)

    zone = dt_util.get_time_zone("Europe/Stockholm")
    assert result.reason is None
    rows = result.horizon
    assert len(rows) == 96
    assert result.unpriced_slots == 0, "the 25-hour day holds every instant of the window"
    elapsed = [row.start.astimezone(timezone.utc) for row in rows]
    assert all(
        elapsed[index + 1] - elapsed[index] == timedelta(minutes=STEP_MINUTES) for index in range(95)
    )
    assert rows[-1].end.astimezone(timezone.utc) == horizon_of(request).astimezone(timezone.utc)
    offsets = {row.start.astimezone(zone).utcoffset() for row in rows}
    assert offsets == {timedelta(hours=1), timedelta(hours=2)}
    local = [(row.start.hour, row.start.minute) for row in rows]
    assert len(set(local)) < len(local), "at least one wall clock occurs twice in the window"
    assert elapsed[-1] - elapsed[0] == timedelta(hours=23, minutes=45)
    assert all(slot.end <= horizon_of(request) for slot in result.slots)


def test_the_no_deadline_calculation_is_repeatable_and_leaves_its_inputs_alone() -> None:
    """The same request calculates the same plan twice, and no document is touched by either run."""
    documents = [day_body("2026-09-12", [1.0] * 96), day_body("2026-09-13", [2.0] * 96)]
    request = no_deadline_request(
        now="2026-09-12T22:00:00+02:00", documents=documents, requested_kwh=42.0
    )
    prices_before = tuple(document.prices for document in request.documents)
    first = calculate_plan(request)
    second = calculate_plan(request)

    assert first == second
    assert tuple(document.prices for document in request.documents) == prices_before
    assert [(slot.start, slot.end) for slot in first.slots] == [(slot.start, slot.end) for slot in second.slots]
    assert [row.start for row in first.horizon] == [row.start for row in second.horizon]


def test_the_nominal_power_a_card_shows_is_this_functions_own_arithmetic() -> None:
    """The card mirrors this arithmetic term for term, and both sides assert the same literals.

    `frontend/src/settings.ts::nominalPowerKw` is written as `230 * amps / 1000` on one phase and
    `sqrt(3) * 400 * amps / 1000` on three, which is exactly what this function computes, and
    `frontend/test/settings.test.ts` asserts these same values. Exact equality is deliberate: it is one
    expression written twice, not two similar ones -- so a change on either side fails a test instead
    of quietly showing the reader a different number from the one the planner used. The `230 x 3`
    shortcut the card must *not* use is asserted separately, because at ordinary currents the two
    disagree.
    """
    assert power_kw(1, 1) == 0.23
    assert power_kw(6, 1) == 1.38
    assert power_kw(16, 1) == 3.68
    assert power_kw(80, 1) == 18.4
    assert power_kw(1, 3) == 0.6928203230275509
    assert power_kw(6, 3) == 4.156921938165306
    assert power_kw(16, 3) == 11.085125168440815
    assert power_kw(80, 3) == 55.42562584220407
    # The reviewed three-phase form is not the shortcut a card may invent for itself.
    assert power_kw(16, 3) != 230.0 * 3 * 16 / 1000
