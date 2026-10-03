"""The charge-session contract fixtures (`tests/fixtures/sessions/`), read with no copy by the card's tests.

The serializer's own output for a fixed set of sessions, so the card decodes what the backend really
sends (a solar charge, an estimated one, one with no price, one across midnight, an open one) and the
Android app can pin the same payloads. `SPOTNAV_WRITE_FIXTURES=1` rewrites them; the default run compares.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from custom_components.spotnav.api.sessions import SESSIONS_RESPONSE_KEYS, csv_filename, sessions_payload
from custom_components.spotnav.sessions.model import (
    ChargeSession,
    SOURCE_ESTIMATED,
    SOURCE_INTEGRATED,
    STARTED_HYBRID,
    STARTED_MANUAL,
    STARTED_PLAN_WINDOW,
    STARTED_SOLAR,
)
from custom_components.spotnav.sessions.summary import sessions_csv

from .sessions_helpers import session, STOCKHOLM, UTC

DIRECTORY = Path(__file__).resolve().parent / "fixtures" / "sessions"
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


def local(day: int, hour: int, minute: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=STOCKHOLM).astimezone(UTC)


def sessions() -> tuple[ChargeSession, ...]:
    return (
        replace(
            session(local(31, 22, 30, month=8), hours=2.5, energy=18.4, cost_minor=1210, reference_minor=1450,
                    started_by=STARTED_PLAN_WINDOW),
            vehicle_name="Volvo EX30",
        ),
        replace(
            session(local(2, 12), hours=3, energy=21.0, cost_minor=410, reference_minor=1020,
                    started_by=STARTED_SOLAR, solar=(19.0, 21.0)),
            vehicle_name="Volvo EX30", strategy="solar",
        ),
        session(local(14, 6), hours=4, energy=24.2, cost_minor=1630, reference_minor=1500,
                started_by=STARTED_HYBRID, solar=(3.0, 24.2)),
        session(local(20, 23), hours=2, energy=9.5, cost_minor=None, currency=None, source=SOURCE_ESTIMATED,
                started_by=STARTED_MANUAL),
        session(local(21, 7), hours=1.5, energy=11.0, cost_minor=455, reference_minor=610, source=SOURCE_INTEGRATED),
    )


def open_session() -> ChargeSession:
    return replace(session(local(22, 11, 30), energy=3.2, cost_minor=120), end=None, started_by=STARTED_MANUAL)


def payload() -> dict[str, Any]:
    return sessions_payload(
        sessions(), open_session(), charger_id="entry_a", zone=STOCKHOLM, now=NOW, limit=20
    )


def month_payload() -> dict[str, Any]:
    return sessions_payload(
        sessions(), open_session(), charger_id="entry_a", zone=STOCKHOLM, now=NOW, limit=20, month="2026-08"
    )


def csv_payload() -> dict[str, Any]:
    return {
        "api_version": 1,
        "format": "csv",
        "filename": csv_filename("entry_a", date(2026, 9, 1), date(2026, 9, 30)),
        "csv": sessions_csv(sessions(), STOCKHOLM, first=date(2026, 9, 1), last=date(2026, 9, 30)),
    }


def check(name: str, value: dict[str, Any]) -> None:
    path = DIRECTORY / name
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES"):
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, f"{name} is stale: rerun with SPOTNAV_WRITE_FIXTURES=1"


def test_the_history_answer_is_the_committed_fixture() -> None:
    answer = payload()

    assert list(answer) == list(SESSIONS_RESPONSE_KEYS)
    assert answer["this_month"]["sessions"] == 4 and answer["last_month"]["sessions"] == 1
    check("get_sessions.json", answer)


def test_the_answer_for_a_chosen_month_is_the_committed_fixture() -> None:
    answer = month_payload()

    assert list(answer) == list(SESSIONS_RESPONSE_KEYS)
    assert answer["month"] == "2026-08" and len(answer["month_days"]) == 31
    assert answer["month_summary"]["sessions"] == 1 and answer["available_months"] == ["2026-09", "2026-08"]
    check("get_sessions_month.json", answer)


def test_the_csv_answer_is_the_committed_fixture() -> None:
    check("get_sessions_csv.json", csv_payload())


def test_instants_are_home_assistants_local_time_with_their_offset() -> None:
    answer = payload()

    assert answer["sessions"][0]["start"].endswith("+02:00") or answer["sessions"][0]["start"].endswith("+01:00")
    assert answer["open"]["start"] == "2026-09-22T11:30:00+02:00"
    assert answer["open"]["end"] is None
