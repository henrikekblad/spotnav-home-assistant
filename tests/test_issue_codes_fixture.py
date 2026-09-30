"""The complete set of state/reason codes the dashboard can emit, pinned for the card.

`frontend/test/issue-codes.test.ts` reads the committed file and fails when any code has no
translation or no classification, so "the backend reported something this card does not know
yet" cannot happen for a code this release emits. Every list below is enumerated from the real
source constants (never hand-typed), and the committed file must equal what the code produces.

Set `SPOTNAV_WRITE_FIXTURES=1` to (re)write the file; the default run only compares.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, get_args

from custom_components.spotnav.execution import auto_execution
from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.auto_controller import AutoReason, AutoState
from custom_components.spotnav.vehicles.capability import HealthState
from custom_components.spotnav.planning.hybrid_plan import HybridReason
from custom_components.spotnav.pricing.price_refresh import RefreshReason, RefreshState
from custom_components.spotnav.site.solar_surplus import SolarReason
from custom_components.spotnav.planning.status_compose import STATUS_CODES, STATUS_TONES

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codes" / "dashboard_codes.json"


def _sorted(values: Any) -> list[str]:
    return sorted(str(value) for value in values)


def produced_codes() -> dict[str, Any]:
    return {
        "planning_states": _sorted(get_args(AutoState)),
        "planning_reasons": _sorted(get_args(AutoReason)),
        "price_states": _sorted(get_args(RefreshState)),
        "price_reasons": _sorted(get_args(RefreshReason)),
        "control_reasons": _sorted(
            [
                auto_execution.CONTROL_NO_SETTINGS,
                auto_execution.CONTROL_PAUSE_UNSETTLED,
                auto_execution.CONTROL_ACTION_PENDING,
            ]
        ),
        "execution_errors": _sorted(
            [
                auto_execution.EXECUTION_INSTALL_FAILED,
                auto_execution.EXECUTION_PLAN_UNUSABLE,
                auto_execution.EXECUTION_PAUSE_STOP_FAILED,
                auto_execution.EXECUTION_PAUSE_CLEAR_FAILED,
                auto_execution.EXECUTION_RECONCILE_FAILED,
                auto_execution.EXECUTION_ACTION_UNAVAILABLE,
                auto_execution.EXECUTION_ACTION_FAILED,
            ]
        ),
        "strategy_reasons": _sorted(
            [dashboard_api.STRATEGY_SOLAR_REASON, dashboard_api.STRATEGY_HYBRID_REASON]
        ),
        "active_control_reasons": _sorted(
            [f"site_measurement_{state}" for state in get_args(HealthState) if state != "healthy"]
            + ["duplicate_membership", "no_commandable_charger"]
        ),
        "solar_reasons": _sorted(get_args(SolarReason)),
        "hybrid_reasons": _sorted(get_args(HybridReason)),
        # Every code of the composed `status` block, its tone (blocking ones are always `blocking`; a
        # code may never be worded by a client that does not list it) and its param names. Params are
        # typed facts, never text.
        "status_tones": list(STATUS_TONES),
        "status_codes": {
            code: {"tone": tone, "params": sorted(params)}
            for code, (tone, params) in sorted(STATUS_CODES.items())
        },
    }


def test_committed_codes_equal_what_the_code_produces() -> None:
    produced = produced_codes()
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == produced
