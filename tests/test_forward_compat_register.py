"""A later release writes the register its count reads on the energy baseline and on an open session; going back to
this release reads both and ignores the field, instead of dropping the count or the session."""

from __future__ import annotations

from datetime import datetime

from custom_components.spotnav.planning.auto_settings import EnergyBaseline
from custom_components.spotnav.sessions.model import ChargeSession

from .sessions_helpers import session, UTC


def test_a_baseline_with_a_later_releases_register_is_read() -> None:
    baseline = EnergyBaseline.from_stored(
        {"register_kwh": 100.0, "departure_key": "no_deadline", "register_entity": "sensor.lifetime"}
    )
    assert baseline.register_kwh == 100.0


def test_an_open_session_with_a_later_releases_register_is_read() -> None:
    stored = session(datetime(2026, 11, 1, 1, 0, tzinfo=UTC)).as_dict()
    stored["register_entity"] = "sensor.lifetime"
    assert ChargeSession.from_dict(stored) is not None
