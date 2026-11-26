"""The battery control shell's small helpers: a reading as a number, a role's state as the executor reads it, and what
a recipe's expiry and the HA-down acceptance mean for a plant."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.core import State

from ..commands import (
    BrandControl,
    Expiry,
    answered,
)
from ..plan_model import (
    ACTION_GRID_CHARGE,
)


def _least(first: float | None, second: float | None) -> float | None:
    """The smaller of two bounds, either missing (`None`) or not positive counting as no bound."""
    bounds = [value for value in (first, second) if value is not None and value > 0]
    return min(bounds) if bounds else None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _state_of(control: BrandControl, name: str, state: State | None) -> str | None:
    """A stored control's state as its role reads it (`commands.answered`); `None` when the entity is gone."""
    if state is None:
        return None
    try:
        role = control.role(name)
    except KeyError:
        return state.state
    if role.compose is not None:
        # Read back from its parts too (`commands.Role.parts`), which only the registry lookup finds: no evidence.
        return None
    return answered(role, state.state, state.attributes)


def _card_refusal(refusal: str | None) -> str | None:
    """An import-limit refusal as the card is sent it: `not_trusted` (`commands.ImportLimitTrust`) as `not_supported`,
    a code the card knows; the precise code is in the diagnostics (`snapshot`)."""
    return "not_supported" if refusal == "not_trusted" else refusal


def _grid_charge_expiry(control: BrandControl | None, *, phased: bool) -> Expiry | None:
    """The expiry of a grid charge on this control: the phase spec's for a charge per phase (`phased`; `None` for a
    brand without one), else the recipe's own for the grid charge (`expiry_by_action`, then `expiry`: never the
    phase spec's, which has nothing to end a balanced command)."""
    if control is None:
        return None
    base = control.with_phases(False)
    if phased:
        return None if base.phase_control is None else base.phase_control.expiry
    return base.expiry_by_action.get(ACTION_GRID_CHARGE, base.expiry)


def _accept_applies(control: BrandControl | None) -> bool:
    """Whether the person's acceptance of the HA-down risk (`battery_control_accept_ha_down`) may lift the cap on this
    recipe: never for a slow control or one whose charge SpotNav cannot set (`BrandControl.slow`, `fixed_charge`:
    Tesla, a cloud in 30-minute blocks), since the live clamp cannot lower its charge within seconds. A hold that may
    itself charge from the grid there (`hold_charges_without`) stays under the HA-down cap with it."""
    return control is not None and not (control.slow or control.fixed_charge)
