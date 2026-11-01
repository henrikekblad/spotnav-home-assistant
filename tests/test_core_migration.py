"""`core.migration.migrate_from_keys`: a charger's record as the logic before the core kept it (today's ownership keys,
no `charge_session`), read once into the core's session from the stored dict alone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from custom_components.spotnav.core.migration import migrate_from_keys
from custom_components.spotnav.core.session import CarEnded, CarEndedWait, ChargeSession, ManualPause

AT = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)
PLAN = {"start": "2026-10-30T22:00:00+00:00", "end": "2026-10-31T01:00:00+00:00", "amps": 10}


def _keys(**changes: Any) -> dict[str, Any]:
    """Today's keys of a charger with nothing going on, as the previous logic saved them."""
    record: dict[str, Any] = {
        "plan": None,
        "requested_current_a": None,
        "plan_charge": False,
        "charge_origin": None,
        "plugged_in_at": None,
        "target_stop": None,
        "top_off_until": None,
        "car_ended_at": None,
        "car_ended_soc": None,
        "car_ended_vehicle": None,
        "car_ended_wait": None,
        "balancing_pause": None,
        "hold": {"held": False, "overridden": False},
        "adapter_memory": {},
    }
    record.update(changes)
    return record


@dataclass(frozen=True)
class _Pause:
    """The execution boundary's stored pause (`PauseIntent`), as much of it as the migration reads."""

    manual: bool = False
    action: str | None = None
    scope: str | None = None
    admitted: bool = False
    choice: str | None = None


def test_nothing_going_on_is_an_empty_session() -> None:
    assert migrate_from_keys({}) == ChargeSession()
    assert migrate_from_keys(_keys()) == ChargeSession()


@pytest.mark.parametrize(
    ("origin", "plan_charge", "owner"),
    [
        ("plan_window", True, "plan"),
        ("manual", False, "person"),
        ("solar", False, "solar"),
        ("other", False, "charge_now"),
        ("min_soc", False, "min_soc"),
        (None, False, "none"),
        ("unknown", False, "none"),
        (42, False, "none"),
    ],
)
def test_each_owner(origin: Any, plan_charge: bool, owner: str) -> None:
    session = migrate_from_keys(_keys(charge_origin=origin, plan_charge=plan_charge, plan=PLAN))
    assert session.owner == owner


def test_the_plans_charge_past_its_last_window_is_a_top_off() -> None:
    keys = _keys(charge_origin="plan_window", plan_charge=True, plan=PLAN, top_off_until=AT.isoformat())
    assert migrate_from_keys(keys).owner == "top_off"
    assert migrate_from_keys({**keys, "plan": None}).owner == "plan", "a top-off needs the plan it finishes"
    assert migrate_from_keys({**keys, "top_off_until": "not a time"}).owner == "plan"


@pytest.mark.parametrize(
    ("pause", "paused_origin"),
    [
        ({"origin": "plan_window", "plan_charge": True}, "plan"),
        ({"origin": "manual", "plan_charge": False}, "person"),
        ({"origin": None, "plan_charge": False}, None),
    ],
)
def test_a_balancing_pause(pause: dict[str, Any], paused_origin: str | None) -> None:
    session = migrate_from_keys(_keys(balancing_pause=pause))
    assert session.balancing_paused and session.paused_origin == paused_origin
    assert session.owner == "none", "the charge it paused is not running"


def test_a_held_session_and_a_persons_override_of_it() -> None:
    assert migrate_from_keys(_keys(hold={"held": True, "overridden": False})).held
    overridden = migrate_from_keys(_keys(hold={"held": True, "overridden": True}))
    assert overridden.held and overridden.overridden
    assert not migrate_from_keys(_keys(hold="garbled")).held


def test_a_car_ended_record_with_its_wait() -> None:
    session = migrate_from_keys(
        _keys(
            car_ended_at=AT.isoformat(),
            car_ended_soc=78.5,
            car_ended_vehicle="ev6",
            car_ended_wait={"target": 80.0, "level": 78.5, "estimated": True},
        )
    )
    assert session.car_ended == CarEnded(
        at=AT, soc=78.5, vehicle="ev6", wait=CarEndedWait(target=80.0, level=78.5, estimated=True)
    )


def test_a_car_ended_record_storage_cannot_vouch_for_is_read_as_far_as_it_goes() -> None:
    assert migrate_from_keys(_keys(car_ended_at="2026-10-30T21:00:00")).car_ended is None, "no time zone"
    session = migrate_from_keys(
        _keys(car_ended_at=AT.isoformat(), car_ended_soc=True, car_ended_vehicle=5, car_ended_wait={"target": 150})
    )
    assert session.car_ended == CarEnded(at=AT)


def test_a_persons_stop_an_older_release_stored() -> None:
    assert migrate_from_keys(_keys(person_stopped=True)).manual == ManualPause("stop", "plug_in")


def test_the_person_intent_is_the_boundarys_stored_pause() -> None:
    start = _Pause(manual=True, action="start", scope="plug_in")
    assert migrate_from_keys(_keys(person_stopped=True), start).manual == ManualPause("start", "plug_in")
    span = migrate_from_keys(_keys(), _Pause(admitted=True, choice="until_resumed"))
    assert span.manual is None and span.span_pause == "until_resumed"
    assert migrate_from_keys(_keys(), _Pause(manual=True, action="start", scope="nowhere")).manual is None


def test_what_a_restart_sees_again_is_not_read() -> None:
    session = migrate_from_keys(_keys(charge_origin="plan_window", plan_charge=True, plan=PLAN))
    assert session.plugged is None and not session.held_for_safety and session.pending == ()
