"""The battery control shell's reads and writes without a site: a stored control read by its entity, and one write as
its service call with the reason it was not taken."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from homeassistant.core import HomeAssistant

from ...util import error_text
from ..commands import (
    FOLLOWS_MAX,
    service_call,
)
from ..executor import (
    Executor,
    Op,
    RoleRead,
)
from .const import (
    CALL_TIMEOUT_S,
)
from .helpers import _float, _state_of


_LOGGER = logging.getLogger(__package__)  # the shell's one logger, whichever of its modules logs


def stored_reads(hass: HomeAssistant, executor: Executor) -> dict[str, RoleRead]:
    """Each control the executor wrote, read by its stored entity (no registry lookup)."""
    reads: dict[str, RoleRead] = {}
    for name, entity_id in executor.state.entities.items():
        state = hass.states.get(entity_id)
        reads[name] = RoleRead(entity_id=entity_id, state=_state_of(executor.control, name, state),
                               found=state is not None, enabled=state is not None, device_id=executor.state.device_id)
    return reads


def resolve_follows_max(hass: HomeAssistant, op: Op) -> Op | None:
    """A write of `FOLLOWS_MAX` (the restore of a number found following its own maximum, `Role.follows_max`) as the
    entity's maximum now, which the integration takes as "follow the maximum" again; any other write as it is.
    `None` when the entity shows no maximum: the write is not made."""
    if op.method != "number" or op.value != FOLLOWS_MAX:
        return op
    state = None if op.entity_id is None else hass.states.get(op.entity_id)
    maximum = None if state is None else _float(state.attributes.get("max"))
    return None if maximum is None else replace(op, value=maximum)


async def _async_call(hass: HomeAssistant, op: Op, executor: Executor) -> str | None:
    """One write as a service call; why it was not taken (`_failure`), `None` when it was."""
    role = executor.control.role(op.role)
    resolved = resolve_follows_max(hass, op)
    if resolved is None:
        _LOGGER.warning("Battery control: %s shows no maximum to follow; it was not written", op.role)
        return "service_failed"
    op = resolved
    domain, service, data = service_call(
        op.method, op.entity_id, op.value, device_id=op.device_id,  # type: ignore[arg-type]
        services=role.services, watchdog_s=executor.watchdog_s, device_field=role.device_field, duration=role.lease,
        power_field=role.power_field, duration_field=role.duration_field,
    )
    try:
        await _async_service(hass, domain, service, data)
    except Exception as err:  # noqa: BLE001 - any error (a Modbus library's too) is a write not taken
        _LOGGER.warning("Battery control: %s.%s for %s failed: %s", domain, service, op.role, _error_text(err))
        return _failure(err)
    return None


async def _async_service(hass: HomeAssistant, domain: str, service: str, data: dict[str, Any]) -> None:
    """One write's service call, given up on after `CALL_TIMEOUT_S` (`TimeoutError`, a write not taken)."""
    async with asyncio.timeout(CALL_TIMEOUT_S):
        await hass.services.async_call(domain, service, data, blocking=True)


def _failure(err: Exception) -> str:
    """Why a write's call was not taken: `call_timeout` when it was given up on (a slow integration, which the
    executor and a verification take as the moment's), else `service_failed`."""
    return "call_timeout" if isinstance(err, TimeoutError) else "service_failed"


def _error_text(err: Exception) -> str:
    """A failed write's error as logged; a call given up on says so (its `TimeoutError` has no text)."""
    if isinstance(err, TimeoutError):
        return f"no answer within {CALL_TIMEOUT_S:.0f} s"
    return error_text(err)
