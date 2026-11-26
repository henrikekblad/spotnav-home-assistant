"""A role's values as the executor judges them: how near a reading must be, a limit never set, whether a write raises,
and what a hand-back writes back and in which order."""

from __future__ import annotations

import math
from typing import Final

from ..commands import FOLLOWS_MAX, BrandControl, Native, Role, reading
from ..facts import BatteryFacts
from .state import RoleRead


def _tolerance(role: Role, read: RoleRead) -> float | None:
    """How near a read must be to a value to count as it: the role's own figure where it has one
    (`Role.tolerance`), else the entity's step, else the role's."""
    return role.tolerance if role.tolerance is not None else (read.step or role.step)


def _rating(facts: BatteryFacts, kind: str | None) -> float | None:
    if kind == "charge":
        return facts.rated_charge_kw
    if kind == "discharge":
        return facts.rated_discharge_kw
    if kind == "export":
        # A never-set export limit holds none: the most the plant can put out is the most it can export.
        return facts.plant_max_kw
    return None


#: At or above this a power limit reads the register's all-ones "never set" value (4294967.295), not a limit.
_NEVER_SET: Final = 4_000_000.0


def _unset(role: Role, state: str | None) -> bool:
    """Whether a number reads the register's "never set" value (Sigenergy's all-ones U32, shown as `unknown` by
    2.0.1 and as 4294967.295 before): it cannot be written back, so the rating is (research §1.4). A real value, even
    at or above the rating, is written back exactly."""
    if role.unset_as_rating is None:
        return False
    if state is not None and state.strip().lower() == "unknown":
        return role.unknown_is_unset
    try:
        value = float(state) / role.scale if state is not None else None
    except ValueError:
        return False
    return value is not None and value >= _NEVER_SET


def _rises(target: Native | None, read: RoleRead | None) -> bool:
    """Whether a number role's target is above what it reads now (`BrandControl.rising_first`)."""
    if read is None or isinstance(target, bool) or not isinstance(target, int | float):
        return False
    try:
        now = float(read.state) if read.state is not None else None
    except ValueError:
        return False
    return now is not None and math.isfinite(now) and float(target) > now


def _restore_value(role: Role, state: str | None, facts: BatteryFacts) -> Native | None:
    """What a restore writes back for a role found reading `state`; `None` when that cannot be known."""
    if role.method == "number":
        if _unset(role, state):
            rating = _rating(facts, role.unset_as_rating)
            return None if rating is None else round(rating * role.scale, 6)
        text = reading(role, state)
        if role.follows_max is not None and text == FOLLOWS_MAX:
            # Found following its own maximum: written back as the maximum then (`control.resolve_follows_max`).
            return FOLLOWS_MAX
        try:
            value = None if text is None else float(text)
        except ValueError:
            return None
        if value is not None and role.restored_below is not None and value < role.restored_below[0]:
            # A sale running when it was found is never the person's value (`Role.restored_below`).
            return role.restored_below[1]
        if value is not None and role.unknown_is_unset and value / role.scale >= _NEVER_SET:
            # The register's "never set" shown as a number (Sigenergy before 2.0.1) with no rating to write in its
            # place: no value to hand back, as when it reads `unknown`.
            return None
        if value is not None and isinstance(role.no_limit, int | float) and value < role.no_limit:
            # Below the maker's "no limit" there is no value (`Role.no_limit`): nothing to hand back.
            return None
        return value
    return reading(role, state)


def restore_order(control: BrandControl, action: str | None) -> tuple[str, ...]:
    """The order a hand-back writes the roles in: for a hold or a grid charge the roles that let the battery cover
    a peak first (`BrandControl.peak_first`), then the brand's restore order."""
    first = control.peak_first.get(action or "", ())
    return (*first, *(name for name in control.restore_order if name not in first))
