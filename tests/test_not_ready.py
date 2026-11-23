"""What will not be ready in time across every deadline (`site/not_ready.py`): the cars' departures under the ceiling
and the spa's baths and the sauna's sessions in one site block, each choice ranked by what it frees."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.spotnav.site.not_ready import car_item, deadline_item, site_block

_DEPARTURE = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
_BATH = datetime(2026, 11, 2, 19, 0, tzinfo=timezone(timedelta(hours=1)))


def _car(departure: datetime = _DEPARTURE, *, short: float = 4.0, chosen: str | None = None, later: datetime | None = None) -> dict:
    return {
        "departure": departure.isoformat(),
        "short_kwh": short,
        "deliverable_kwh": 12.0,
        "ceiling": "import_target",
        "ceiling_kw": 6.0,
        "set_kw": 6.0,
        "chosen": chosen,
        "choices": [
            {"code": "takes_what_fits", "kwh": 12.0, "peak_kw": None, "cost": None, "departure": None},
            {"code": "later", "kwh": None, "peak_kw": None, "cost": None, "departure": None if later is None else later.isoformat()},
            {"code": "accept_peak", "kwh": None, "peak_kw": 11.0, "cost": 4500.0, "departure": None},
        ],
    }


def _bath(at: datetime = _BATH, *, cause: str = "import_target", short_kwh: float = 6.0, later: datetime | None = None) -> dict:
    return {
        "at": at.isoformat(), "cause": cause, "reach_c": 35.0, "short_c": 3.0, "short_kwh": short_kwh,
        "later": None if later is None else later.isoformat(),
    }


def test_nothing_short_is_no_block() -> None:
    assert site_block([None, car_item("a", "Garage", None), deadline_item("spa", "Spa", None)]) is None
    # A car whose peak was accepted: nothing short, no item.
    accepted = {**_car(), "short_kwh": None, "chosen": "accept_peak", "choices": []}
    assert car_item("a", "Garage", accepted) is None


def test_a_car_and_a_bath_are_one_block_in_deadline_order() -> None:
    block = site_block([car_item("a", "Garage", _car()), deadline_item("spa", "Spa", _bath())])
    assert block is not None
    # The bath (18:00 UTC) comes before the car's departure the next morning; instants in UTC.
    assert [item["kind"] for item in block["items"]] == ["spa", "car"]
    assert block["items"][0]["deadline"] == "2026-11-02T18:00:00+00:00"
    assert block["items"][0]["reach_c"] == 35.0 and block["items"][1]["short_c"] is None


def test_the_peak_frees_every_item_under_the_ceiling_and_is_ranked_once_first() -> None:
    block = site_block(
        [car_item("a", "Garage", _car(later=_DEPARTURE + timedelta(hours=1))), deadline_item("spa", "Spa", _bath(later=_BATH + timedelta(hours=1)))],
        ["heat", "charger:a", "spa"],
    )
    assert block is not None
    ranked = block["ranked"]
    assert ranked[0] == {"load_id": None, "code": "accept_peak", "frees_kwh": 10.0}
    assert [entry["code"] for entry in ranked].count("accept_peak") == 1
    # Then the most freed: the bath's 6 kWh before the car's 4; at a tie what costs nothing first (later).
    assert [(entry["load_id"], entry["code"]) for entry in ranked[1:]] == [
        ("spa", "later"), ("spa", "takes_what_fits"), ("a", "later"), ("a", "takes_what_fits"),
    ]
    # Each item's own accept_peak says what the one press frees.
    assert all(choice["frees_kwh"] == 10.0 for item in block["items"] for choice in item["choices"] if choice["code"] == "accept_peak")


def test_at_a_tie_the_load_lower_in_the_priority_list_gives_way_first() -> None:
    """The owner's default: the booked bath gives way before the car (the bath stands below the cars)."""
    items = [car_item("a", "Garage", _car(short=6.0)), deadline_item("spa", "Spa", _bath(short_kwh=6.0, cause="fuse"))]
    block = site_block(items, ["heat", "charger:a", "spa"])
    assert block is not None
    takes = [entry["load_id"] for entry in block["ranked"] if entry["code"] == "takes_what_fits"]
    assert takes == ["spa", "a"]
    # With the spa moved above the car, the car gives way first.
    block = site_block(items, ["heat", "spa", "charger:a"])
    assert [entry["load_id"] for entry in block["ranked"] if entry["code"] == "takes_what_fits"] == ["a", "spa"]


def test_the_fuse_and_the_rate_offer_no_peak() -> None:
    for cause in ("fuse", "rate"):
        item = deadline_item("sauna", "Bastu", _bath(cause=cause))
        assert [choice["code"] for choice in item["choices"]] == ["takes_what_fits"]
        block = site_block([item])
        assert [entry["code"] for entry in block["ranked"]] == ["takes_what_fits"]


def test_a_later_without_a_time_is_no_choice_and_a_chosen_car_is_not_ranked() -> None:
    item = car_item("a", "Garage", _car())
    assert [choice["code"] for choice in item["choices"]] == ["takes_what_fits", "accept_peak"]
    chosen = car_item("b", "Carport", _car(chosen="takes_what_fits"))
    block = site_block([chosen, deadline_item("spa", "Spa", _bath(cause="rate"))])
    assert block is not None and len(block["items"]) == 2
    assert {entry["load_id"] for entry in block["ranked"]} == {"spa"}
    # The car that chose takes part in no peak: the bath is under no ceiling either.
    assert all(entry["code"] != "accept_peak" for entry in block["ranked"])
