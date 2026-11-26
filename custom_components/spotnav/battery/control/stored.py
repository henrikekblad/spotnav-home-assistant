"""The hand-back of a stored state's snapshots without a site (a removed site, an orphaned snapshot): only restores, on
the plant's own reads."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...const import DOMAIN
from ..commands import control_for, export_control_for
from ..executor import ExecInputs, ExecState, Executor
from ..ledger import Ledger
from ..monitor import Monitor
from ..runtime import read_battery_facts, read_export_facts, read_role_reads
from .const import HAND_BACK_DEADLINE_S, HAND_BACK_LOOK_S, ORPHANED
from .io import _async_call, stored_reads


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.battery_control.{entry_id}"


async def async_hand_back_stored(
    hass: HomeAssistant, config: dict[str, Any], data: dict[str, Any], *, deadline_s: float | None = None
) -> bool:
    """Hand back the snapshots a stored state holds (the battery's, then the export limit's) without a site: only
    restores, on the plant's own reads. `data` is updated in place; answers whether anything is still held."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (HAND_BACK_DEADLINE_S if deadline_s is None else deadline_s)
    held = False
    for key in ("battery", "export"):
        part = data if key == "battery" else data.get("export")
        if not isinstance(part, dict):
            continue
        state = ExecState.from_dict(part.get("state"))
        control = (control_for if key == "battery" else export_control_for)(state.brand)
        if control is None or not state.holds_snapshot:
            # A snapshot whose brand has no recipe in this release cannot be written back: it is still held.
            held = held or state.holds_snapshot
            continue
        executor = Executor(control, Ledger.from_dict(part.get("ledger")), Monitor.from_dict(part.get("monitor")), state)
        executor.restart(dt_util.now())
        failed: tuple[str, ...] = ()
        timed_out: tuple[str, ...] = ()
        while executor.state.phase == "restoring" and loop.time() < deadline:
            if key == "battery":
                facts = read_battery_facts(hass, config, None)
                reads = {**stored_reads(hass, executor), **read_role_reads(hass, config, control)}
            else:
                facts, reads, _control = read_export_facts(hass, config)
                reads = {**stored_reads(hass, executor), **reads}
            result = executor.step(ExecInputs(
                now=dt_util.now(), facts=facts, reads=reads, measurement_ok=False, stopping="removed", failed=failed,
                timed_out=timed_out,
            ))
            whys = {op.role: await _async_call(hass, op, executor) for op in result.ops if op.restore}
            failed = tuple(role for role, why in whys.items() if why is not None)
            timed_out = tuple(role for role, why in whys.items() if why == "call_timeout")
            if executor.state.phase != "restoring":
                break
            await asyncio.sleep(0 if result.ops else HAND_BACK_LOOK_S)
        part |= {"state": executor.state.as_dict(), "ledger": executor.ledger.as_dict(),
                 "monitor": executor.monitor.as_dict()}
        held = held or executor.state.holds_snapshot
    # Snapshots set aside at a start because their brand had no recipe (`BatteryControl._set_aside`), each as its own
    # part: handed back where the recipe is there again, kept otherwise.
    kept = []
    for orphan in _orphans(data.get(ORPHANED)):
        wrapped = orphan if orphan["key"] == "battery" else {"export": orphan}
        if await async_hand_back_stored(hass, config, wrapped, deadline_s=max(deadline - loop.time(), 0.0)):
            kept.append(orphan)
    if kept:
        data[ORPHANED] = kept
    else:
        data.pop(ORPHANED, None)
    return held or bool(kept)


def _orphans(raw: Any) -> list[dict[str, Any]]:
    """The stored orphans that are well formed (`ORPHANED`): a known key and a state that is a mapping (anything
    else holds no snapshot)."""
    return [
        dict(row) for row in (raw if isinstance(raw, list) else [])
        if isinstance(row, dict) and row.get("key") in ("battery", "export") and isinstance(row.get("state"), dict)
    ]
