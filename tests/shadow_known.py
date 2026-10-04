"""Disagreements between the charge-ownership core's shadow and today's code that are explained: real differences of
today's code (step 1 copies today's rules, and fixes nothing in today's code), each named with its event sequence.

`explain(record)` returns the name of the explanation, or `None` for a disagreement nobody explained (the test then
fails, `conftest.ownership_shadow_agrees`).

`explain_drift(test, record)` does the same for a drift: a change of today's owner or person intent that no event
explained. The only ones explained are tests that set today's state directly (a private field, or the settings
store), where no event is fed by design; each is named with the test, exactly what moved, and why. A drift anywhere
else is a place today's code changes ownership without telling the core, and fails the test.
"""

from __future__ import annotations

from typing import Any, Final


def explain(record: dict[str, Any]) -> str | None:
    return None


#: (core, today) of one compared field: `owner` a name, `manual` a dict or None, `span_pause` a choice or None.
_Moved = dict[str, tuple[Any, Any]]
_STOP_UNTIL_RESUMED: Final = "until_resumed"

#: The test (its function name, without parameters) -> (each drift it may cause, why).
KNOWN_DRIFTS: Final[dict[str, tuple[tuple[_Moved, ...], str]]] = {
    "test_manual_start_is_on_even_under_solar": (
        ({"owner": ("none", "person")},),
        "sets `controller._charge_origin = 'manual'` directly to label a charge for the grid-charging sensor",
    ),
    "test_power_is_an_attribute_when_known": (
        ({"owner": ("none", "person")},),
        "sets `controller._charge_origin = 'manual'` directly to label a charge for the grid-charging sensor",
    ),
    "test_solar_surplus_charge_is_off": (
        ({"owner": ("none", "solar")},),
        "sets `controller._charge_origin = 'solar'` directly to label a charge for the grid-charging sensor",
    ),
    "test_restart_adoption_does_not_cycle_the_contactor_and_still_stops_normally": (
        ({"owner": ("charger_self", "solar")},),
        "`world.solar_setup(charge_origin_at_setup=...)` sets `controller._charge_origin` directly after setup, "
        "standing in for the origin a restart reads back",
    ),
    "test_the_committed_control_fixtures_are_the_serializers_own_output": (
        (
            {"span_pause": (None, "until_tomorrow")},
            {"span_pause": ("until_tomorrow", None)},
            {"manual": ({"action": "start", "scope": "plug_in"}, None)},
        ),
        "each control fixture is built by writing the settings store's pause directly (one fresh charger each, "
        "in turn), not through a person's action",
    ),
    "test_a_waiting_proposal_is_dropped_when_the_pause_is_still_persisted": (
        ({"span_pause": (None, _STOP_UNTIL_RESUMED)},),
        "writes a stored pause straight into the settings store to build a state the admission path cannot reach",
    ),
}


def explain_drift(test: str, record: dict[str, Any]) -> str | None:
    known = KNOWN_DRIFTS.get(test)
    if known is None:
        return None
    moved = {name: (values.get("core"), values.get("today")) for name, values in record.get("fields", {}).items()}
    allowed, _why = known
    return f"{test}" if moved in allowed else None
