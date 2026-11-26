"""Each role of a brand's control found among the plant's entities, with its reading."""

from __future__ import annotations

from collections.abc import Sequence

from ..commands import BrandControl, answered
from ..facts import SHARED_PLATFORMS, EntityFact, find_entity
from .state import RoleRead


def role_reads(control: BrandControl, entities: Sequence[EntityFact], battery: EntityFact) -> dict[str, RoleRead]:
    """Each role's entity among the battery integration's entities, found by the integration's unique-id key. On a
    shared platform (`facts.SHARED_PLATFORMS`) a role that is written is found only on the battery's own device."""
    reads: dict[str, RoleRead] = {}
    shared = battery.platform in SHARED_PLATFORMS
    written = {role.name for role in control.roles if role.method != "sensor"}
    for role in (*control.roles, *(role for role, _value, _why in control.watched)):
        found = find_entity(role.match, entities, battery, own_device_only=shared and role.name in written)
        if found is None:
            continue
        state = answered(role, found.state, found.attributes)
        if role.compose is not None:
            # Read back from more than its own entity (`commands.Role.parts`): no part, no reading.
            parts = tuple(find_entity(match, entities, battery) for match in role.parts)
            state = role.compose(state, tuple(None if part is None else part.state for part in parts))
        reads[role.name] = RoleRead(
            entity_id=found.entity_id, state=state, found=True,
            enabled=not found.disabled, step=found.step,
            device_id=found.device_id,
        )
    return reads
